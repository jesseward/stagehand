from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..config import config
from ..logger import getLogger
from .base import NotifierBase
from .xbmc_config import config as modconfig

if TYPE_CHECKING:
    from ..tvdb import Episode

__all__ = ['Notifier', 'modconfig']

log = getLogger('stagehand.notifiers.xbmc')


class Notifier(NotifierBase):
    """
    Asks XBMC/Kodi (via its JSON-RPC TCP interface) to rescan its library
    when new episodes are retrieved.
    """
    def __init__(self) -> None:
        super().__init__()
        self._rpcver = 0
        self._rpcreader: asyncio.StreamReader | None = None
        self._rpcwriter: asyncio.StreamWriter | None = None

    async def _jsonrpc(self, method: str, params: Any) -> Any:
        assert self._rpcreader is not None and self._rpcwriter is not None
        request = {
            'jsonrpc': '2.0',
            'method': method,
            'params': params,
            'id': 1
        }
        log.debug2('issuing JSON-RPC method=%s params=%s', method, params)
        self._rpcwriter.write(json.dumps(request).encode('utf-8'))
        await self._rpcwriter.drain()
        data = await asyncio.wait_for(self._rpcreader.read(65536), timeout=5)
        try:
            return json.loads(data)['result']
        except (ValueError, KeyError):
            log.error('unexpected response from JSON-RPC: %s...', data[:1000])
            return None


    async def _wait_for_idle(self, max_wait: float = 120) -> bool:
        """
        Waits until XBMC is idle (i.e. not scanning library).

        :returns: True if XBMC is idle, False if it isn't (due to timeout), or
                  None if JSON-RPC isn't working.
        """
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_wait:
            result = await self._jsonrpc('XBMC.GetInfoBooleans', {'booleans': ['library.isscanning']})
            if not result or result.get('library.isscanning') is not True:
                return True
            log.debug2('XBMC busy scanning, waiting')
            await asyncio.sleep(1)
        return False


    async def _send_notification(self, title: str, msg: str) -> Any:
        return await self._jsonrpc('GUI.ShowNotification', [title, msg])

    async def _update_library(self, path: str = '') -> Any:
        path = path + '/' if path and not path.endswith('/') else path
        return await self._jsonrpc('VideoLibrary.scan', [path])

    async def _do_notify(self, episodes: Sequence[Episode]) -> None:
        self._rpcreader, self._rpcwriter = await asyncio.open_connection(str(modconfig.hostname),
                                                                         int(modconfig.tcp_port))
        try:
            await self._do_notify_connected(episodes)
        finally:
            self._rpcwriter.close()
            await self._rpcwriter.wait_closed()

    async def _do_notify_connected(self, episodes: Sequence[Episode]) -> None:
        # Determine JSON-RPC API version used by XBMC.
        result = await self._jsonrpc('JSONRPC.Version', [])
        try:
            self._rpcver = int(result['version']['major'])
        except (KeyError, TypeError, ValueError):
            self._rpcver = 0
        log.debug('rpc version %d', self._rpcver)

        # We need to disable notifications as they can precede
        # responses to methods we invoke and we don't handle that (nor do
        # we need to).
        notifications = {
            'Application': False,
            'GUI': False,
            'System': False,
            'Player': False,
            'AudioLibrary': False,
            'VideoLibrary': False,
            'Other': False
        }
        await self._jsonrpc('JSONRPC.SetConfiguration', {'notifications': notifications})

        # Get a list of all series directories for new episodes.  We
        # _could_ add the season directory, except that if the series isn't
        # one XBMC yet knows about, it won't discover new series metadata.
        # If it does already know about it, it will pick up the episode in the
        # season directory properly.  But there doesn't seem to be a way to
        # distinguish this.  VideoLibrary.GetTVShows isn't foolproof because
        # XBMC doesn't provide the path to the series.
        dirs = {ep.series.path for ep in episodes}

        # Translate local path to XBMC path.
        if modconfig.tvdir:
            # Normalize local and remote paths so they have trailing slash
            frm = os.path.normpath(os.path.expanduser(config.misc.tvdir)) + '/'
            to = os.path.normpath(modconfig.tvdir) + '/'
            dirs = {to + d[len(frm):] if d.startswith(frm) else d for d in dirs}

        await self._wait_for_idle()
        if modconfig.individual:
            # Issue an update for each path.
            for d in sorted(dirs):
                await self._update_library(d)
                await self._wait_for_idle()
        else:
            await self._update_library()

        if modconfig.notify:
            if len(episodes) == 1:
                msg = f'New episode for {episodes[0].series.name} available.'
            else:
                msg = f'{len(episodes)} new episodes added to library.'
            await self._send_notification('New TV Episodes', msg)

        log.debug('updated library with %d episodes', len(episodes))

    async def _notify(self, episodes: Sequence[Episode]) -> None:
        try:
            await self._do_notify(episodes)
        except TimeoutError:
            log.error('timed out waiting for XBMC server')
        except OSError as e:
            log.error('unable to connect to XBMC server: %s', e)
        else:
            log.info('sent xbmc notification')
