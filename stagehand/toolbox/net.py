# -----------------------------------------------------------------------------
# net.py - Miscellaneous network helper functions
# -----------------------------------------------------------------------------
# Copyright 2014 Jason Tackaberry
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
# -----------------------------------------------------------------------------
"""
HTTP download helpers built on a shared :class:`aiohttp.ClientSession`.
"""
from __future__ import annotations

__all__ = ['download', 'DownloadError', 'get_session', 'close_session']

import asyncio
import base64
import io
import logging
import os
import re
from typing import IO, Any

import aiohttp

from .core import FutureProgress

log = logging.getLogger('http')

CHUNK_SIZE = 64 * 1024
USER_AGENT = 'Stagehand'
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None


class DownloadError(Exception):
    """
    Raised by :func:`download` when ``noraise`` is False and the download
    could not be completed.
    """
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class _HTTPStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f'status {status}')
        self.status = status


def get_session() -> aiohttp.ClientSession:
    """
    Returns the shared client session for the running loop, creating it if
    necessary.  Sharing a session allows connections to be pooled.
    """
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        _session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT, headers={'User-Agent': USER_AGENT})
        _session_loop = loop
    return _session


async def close_session() -> None:
    """
    Closes the shared client session.  Call this on shutdown.
    """
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _basic_auth_header(user: str, password: str) -> str:
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode('ascii')


async def _download(url: str, target: str | os.PathLike[str] | IO[bytes] | None, resume: bool,
                    progress: FutureProgress | None, method: str = 'GET',
                    headers: dict[str, str] | None = None, data: Any = None,
                    auth: tuple[str, str] | None = None) -> tuple[int, bytes | int]:
    """
    Performs a single download attempt.

    :returns: (status, body) where body is the response bytes if target is
              None, or otherwise the number of bytes written to target.
    """
    buffer: io.BytesIO | None = None
    if target is None:
        buffer = io.BytesIO()
        fp: IO[bytes] = buffer
        resume = False
    elif isinstance(target, (str, os.PathLike)):
        fp = open(target, 'ab+' if resume else 'wb')  # noqa: SIM115 (closed in finally)
    else:
        fp = target

    try:
        headers = dict(headers or {})
        if auth:
            headers['Authorization'] = _basic_auth_header(*auth)
        expected_pos = 0
        if resume:
            expected_pos = fp.seek(0, io.SEEK_END)
            if expected_pos > 0:
                headers['Range'] = f'bytes={expected_pos}-'
        else:
            fp.seek(0)
            fp.truncate()

        log.info('fetching %s', url)
        async with get_session().request(method, url, headers=headers, data=data) as response:
            content_range = response.headers.get('Content-Range', '')
            if response.status == 416 and expected_pos > 0:
                # If the server tells us the full size and that's what we
                # already have, the file is already complete.
                m = re.match(r'bytes \*/(\d+)', content_range)
                if m and int(m.group(1)) == expected_pos:
                    return 416, 0
                raise _HTTPStatusError(416)
            if response.status >= 300:
                raise _HTTPStatusError(response.status)

            m = re.search(r'bytes +(\d+).*/(\d+|\*)', content_range)
            if m:
                pos = int(m.group(1))
                if progress:
                    progress.set(pos=pos, max=int(m.group(2)) if m.group(2) != '*' else None)
                if pos < expected_pos:
                    fp.seek(pos)
                    fp.truncate()
            elif expected_pos > 0:
                # Server didn't respond with a Content-Range header and we are
                # trying to resume, so we have to assume the server doesn't
                # support resume.  Simulate a range not satisfiable error to
                # restart the request without a Range header.
                raise _HTTPStatusError(416)
            elif progress:
                progress.set(max=response.content_length or 0)

            written = 0
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                fp.write(chunk)
                written += len(chunk)
                if progress:
                    progress.update(diff=len(chunk))

            return response.status, buffer.getvalue() if buffer is not None else written
    finally:
        if fp is not target and buffer is None:
            fp.close()


async def download(url: str, target: str | os.PathLike[str] | IO[bytes] | None = None, resume: bool = True,
                   retry: int = 0, progress: FutureProgress | None = None, noraise: bool = True,
                   retry_delay: float = 1.0, **kwargs: Any) -> tuple[int, Any]:
    """
    Downloads the given URL, either into memory or to a file.

    :param url: the URL to fetch
    :param target: if None, the response body is returned; otherwise a
                   filename or writable binary file object to write to
    :param resume: if True and target is a non-empty file, attempt to resume
                   the download using a Range request
    :param retry: number of times to retry on connection errors or 5xx
                  responses
    :param progress: optional progress object updated as data is received
    :param noraise: if True (default), failures are logged and reported via
                    the returned status rather than raised
    :param retry_delay: initial delay in seconds between retries, which
                        doubles after each attempt
    :param kwargs: passed to the request (``method``, ``headers``, ``data``,
                   ``auth``)
    :returns: (status, body) where body is the response bytes (target is
              None) or the number of bytes written (target is given), or None
              if the download failed.  Status is 0 if no HTTP response was
              received.  A status of 416 when resuming means the file was
              already complete.
    :raises DownloadError: if noraise is False and the download failed.
    """
    attempt = 0
    while True:
        status = 0
        try:
            return await _download(url, target, resume, progress, **kwargs)
        except _HTTPStatusError as e:
            status = e.status
            if status == 416 and resume:
                # Server reported range not satisfiable and we're trying to
                # resume.  Retry without resuming, without counting against
                # the retry counter.
                resume = False
                continue
            errmsg = str(e)
            if not 500 <= status < 600:
                # Not a temporary error that we can retry.
                if noraise:
                    return status, None
                raise DownloadError(errmsg, status) from e
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            errmsg = str(e) or type(e).__name__

        if attempt >= retry:
            if noraise:
                log.warning('download of %s failed: %s', url, errmsg)
                return status, None
            raise DownloadError(errmsg, status)

        log.warning('download failed (%d retries left): %s', retry - attempt, errmsg)
        await asyncio.sleep(retry_delay * 2 ** attempt)
        attempt += 1
