"""
Shared definitions for the web application: typed application/request keys
and response helpers.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import bottle
from aiohttp import web

from ..config import config

if TYPE_CHECKING:
    from ..manager import Manager  # noqa: F401  (used in the AppKey type below)

if TYPE_CHECKING:
    pass

__all__ = ['MANAGER', 'DATA_PATH', 'SHUTDOWN', 'SESSION', 'LOGLEVEL', 'Templates', 'TEMPLATES', 'json_response', 'root_path',
           'render']

# Application keys
MANAGER = web.AppKey['Manager']('manager')
DATA_PATH = web.AppKey('data_path', str)
SHUTDOWN = web.AppKey('shutdown', asyncio.Event)
TEMPLATES = web.AppKey['Templates']('templates')

# Request keys
SESSION = web.RequestKey('session', str)
#: Handlers may lower the log level of their request (e.g. for frequent polls).
LOGLEVEL = web.RequestKey('loglevel', int)


def script_json(data: Any) -> str:
    """
    Serializes data as JSON that is safe to embed in an HTML <script> block.
    """
    return json.dumps(data).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def json_response(data: Any, **kwargs: Any) -> web.Response:
    return web.json_response(data, dumps=json.dumps, **kwargs)


def root_path(request: web.Request) -> str:
    """
    Returns the root URL path of the application, which is non-empty when
    behind a reverse proxy (see ``config.web.proxied_root``).
    """
    return str(config.web.proxied_root) if 'X-Forwarded-Host' in request.headers else ''


class Templates:
    """
    Renders Bottle SimpleTemplate templates from the data directory.
    """
    def __init__(self, path: str, debug: bool = False) -> None:
        self._lookup = [path]
        self._debug = debug
        self._cache: dict[str, bottle.SimpleTemplate] = {}

    def get(self, name: str) -> bottle.SimpleTemplate:
        tpl = None if self._debug else self._cache.get(name)
        if tpl is None:
            tpl = self._cache[name] = bottle.SimpleTemplate(name=name, lookup=self._lookup)
        return tpl

    def render(self, name: str, **kwargs: Any) -> str:
        return str(self.get(name).render(**kwargs))


def render(request: web.Request, name: str, context: dict[str, Any] | None = None) -> web.Response:
    """
    Renders a template to an HTML response.  All templates have these
    variables available, in addition to the given context:

        * manager: the Manager instance
        * config: the global config object
        * json: serializes to JSON that is safe inside <script>
        * root: the root URL path for the application
        * pending_jobs: finished jobs and notifications for the session
    """
    from .jobs import asyncweb
    kwargs = dict(context or {})
    kwargs.update({
        'manager': request.app[MANAGER],
        'config': config,
        'json': script_json,
        'root': root_path(request),
        'pending_jobs': asyncweb.pop_finished_jobs(request.get(SESSION)),
    })
    html = request.app[TEMPLATES].render(name, **kwargs)
    return web.Response(text=html, content_type='text/html')
