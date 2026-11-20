"""
The embedded web server: application factory, middleware, and static files.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import logging
import mimetypes
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING

from aiohttp import web

from .context import DATA_PATH, LOGLEVEL, MANAGER, SESSION, SHUTDOWN, TEMPLATES, Templates, root_path

if TYPE_CHECKING:
    from ..manager import Manager

__all__ = ['WebServer', 'create_app']

log = logging.getLogger('stagehand.web')
httplog = logging.getLogger('stagehand.http')

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
SESSION_COOKIE = 'stagehand.session'
NO_CACHE = 'max-age=0,no-cache,no-store'


@web.middleware
async def logging_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """
    Logs each request to the stagehand.http logger.  Handlers may change the
    log level for the request by setting ``request[LOGLEVEL]``.
    """
    t0 = time.perf_counter()
    status, size = 500, '-'
    try:
        response = await handler(request)
        status = response.status
        if response.body_length:
            size = str(response.body_length)
        elif isinstance(response, web.Response) and response.body is not None:
            size = str(len(response.body))  # type: ignore[arg-type]
        return response
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        httplog.log(request.get(LOGLEVEL, logging.INFO), '%s "%s %s HTTP/%d.%d" %s %s %.02fms',
                    request.remote, request.method, request.path_qs, *request.version, status, size,
                    (time.perf_counter() - t0) * 1000)


class DigestAuth:
    """
    HTTP Digest authentication (RFC 2617, without qop) as a middleware.

    Nonces are timestamps authenticated with a per-process secret, and expire
    after a day.
    """
    REALM = 'Secure Area'
    NONCE_LIFETIME = 24 * 3600

    def __init__(self, user: str, password: str) -> None:
        self._user = user
        self._password = password
        self._secret = secrets.token_bytes(32)

    def _make_nonce(self, timestamp: int | None = None) -> str:
        ts = str(timestamp if timestamp is not None else int(time.time()))
        mac = hmac.new(self._secret, ts.encode(), hashlib.sha256).hexdigest()
        return f'{ts}/{mac}'

    def _valid_nonce(self, nonce: str) -> bool:
        ts, _, _ = nonce.partition('/')
        if not ts.isdigit() or time.time() - int(ts) > self.NONCE_LIFETIME:
            return False
        return hmac.compare_digest(nonce, self._make_nonce(int(ts)))

    def check(self, request: web.Request) -> bool:
        header = request.headers.get('Authorization', '')
        if not header.lower().startswith('digest '):
            return False
        parts = (f.strip().split('=', 1) for f in header[7:].split(',') if '=' in f)
        params = {k.lower(): v.strip('"') for k, v in parts}
        try:
            if params['username'] != self._user or not self._valid_nonce(params['nonce']):
                return False

            def md5(s: str) -> str:
                return hashlib.md5(s.encode(), usedforsecurity=False).hexdigest()

            ha1 = md5(f"{self._user}:{params['realm']}:{self._password}")
            ha2 = md5(f"{request.method}:{params['uri']}")
            expected = md5(f"{ha1}:{params['nonce']}:{ha2}")
            return hmac.compare_digest(params['response'], expected)
        except KeyError:
            return False

    @web.middleware
    async def middleware(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if self.check(request):
            return await handler(request)
        # TODO: advertise qop and support cnonce
        raise web.HTTPUnauthorized(
            headers={'WWW-Authenticate': f'Digest realm="{self.REALM}", nonce="{self._make_nonce()}", algorithm=MD5'},
            text='<html><body>Authorization Required</body></html>', content_type='text/html')


@web.middleware
async def session_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """
    Ensures every client has a session id cookie, which is used to deliver
    job results and notifications to the right browser.
    """
    session = request.cookies.get(SESSION_COOKIE)
    new_session = not session
    request[SESSION] = session or secrets.token_hex(16)
    response = await handler(request)
    if new_session:
        response.set_cookie(SESSION_COOKIE, request[SESSION], path=root_path(request) or '/')
    return response


@web.middleware
async def cache_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """
    Prevents clients from caching JSON responses unless the handler set a
    Cache-Control header explicitly.
    """
    response = await handler(request)
    if response.content_type == 'application/json' and 'Cache-Control' not in response.headers:
        response.headers['Cache-Control'] = NO_CACHE
    return response


async def static(request: web.Request) -> web.Response:
    """
    Serves static files from the data directory.

    Pre-compressed files (``*.gz``) are served with ``Content-Encoding:
    gzip``, or decompressed on the fly if the client doesn't support gzip.
    """
    root = os.path.realpath(os.path.join(request.app[DATA_PATH], 'web'))
    path = os.path.realpath(os.path.join(root, request.match_info['filename']))
    if not path.startswith(root + os.sep):
        raise web.HTTPForbidden(text='Access denied.')
    try:
        st = os.stat(path)
    except OSError:
        raise web.HTTPNotFound(text='File does not exist.') from None
    if not os.path.isfile(path):
        raise web.HTTPNotFound(text='File does not exist.')

    mtime = datetime.fromtimestamp(int(st.st_mtime), UTC)
    headers = {'Last-Modified': format_datetime(mtime, usegmt=True)}
    ims = request.headers.get('If-Modified-Since')
    if ims:
        try:
            if parsedate_to_datetime(ims.split(';')[0]) >= mtime:
                raise web.HTTPNotModified(headers=headers)
        except (TypeError, ValueError):
            pass

    content_type, encoding = mimetypes.guess_type(path)
    with open(path, 'rb') as f:
        body = f.read()
    if encoding == 'gzip':
        if 'gzip' in request.headers.get('Accept-Encoding', ''):
            headers['Content-Encoding'] = 'gzip'
        else:
            body = gzip.decompress(body)
    return web.Response(body=body, headers=headers, content_type=content_type or 'application/octet-stream')


def create_app(manager: Manager, data_path: str, *, user: str | None = None, password: str | None = None,
               debug: bool = False) -> web.Application:
    """
    Creates the aiohttp application for the web interface.

    :param manager: the Manager instance, available to handlers as
                    ``request.app[MANAGER]``
    :param data_path: the directory holding static data (templates, images,
                      etc.) in a ``web`` subdirectory
    :param user: if given, require HTTP digest authentication with this
                 user name and the given password
    :param debug: if True, templates are reloaded on each request
    """
    from . import api, jobs
    from . import app as pages

    middlewares: list[web.types.Middleware] = [logging_middleware]  # type: ignore[name-defined]
    if user:
        middlewares.append(DigestAuth(user, password or '').middleware)
    middlewares += [session_middleware, cache_middleware]

    application = web.Application(middlewares=middlewares)
    application[MANAGER] = manager
    application[DATA_PATH] = data_path
    application[SHUTDOWN] = asyncio.Event()
    application[TEMPLATES] = Templates(os.path.join(data_path, 'web'), debug=debug)
    application.router.add_get('/static/{filename:.*}', static)
    application.add_routes(pages.routes)
    application.add_routes(api.routes)
    application.add_routes(jobs.routes)
    return application


class WebServer:
    """
    Runs the web application, allowing it to be stopped and restarted (e.g.
    when the web configuration changes).
    """
    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self.app: web.Application | None = None
        self.host = ''
        self.port = 0

    async def start(self, manager: Manager, data_path: str, *, host: str = '', port: int = 8088,
                    user: str | None = None, password: str | None = None, debug: bool = False) -> web.Application:
        """
        Starts the web server.

        :raises OSError: if the server could not listen on the given address
        """
        await self.stop()
        app = create_app(manager, data_path, user=user, password=password, debug=debug)
        runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await runner.setup()
        site = web.TCPSite(runner, host or None, port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self.app, self.host, self.port = runner, app, host, port
        log.info('started webserver at http://%s:%s/', host or socket.gethostname(), port)
        return app

    async def stop(self) -> None:
        if self._runner:
            log.warning('stopping web server')
            await self._runner.cleanup()
            self._runner = self.app = None

    @property
    def running(self) -> bool:
        return self._runner is not None
