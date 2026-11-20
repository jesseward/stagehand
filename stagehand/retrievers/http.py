from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from ..toolbox.net import download
from .base import RetrieverAbortedSoft, RetrieverBase, RetrieverError

if TYPE_CHECKING:
    from ..searchers.base import SearchResult
    from ..toolbox import FutureProgress
    from ..tvdb import Episode

__all__ = ['Retriever']

log = logging.getLogger('stagehand.retrievers.http')

# Begin verifying the file once this many bytes have been downloaded.
VERIFY_AFTER_BYTES = 512 * 1024
VERIFY_INTERVAL = 1.0


class Retriever(RetrieverBase):
    NAME = 'http'
    PRINTABLE_NAME = 'HTTP'
    SUPPORTED_TYPES = ('http',)
    ALWAYS_ENABLED = True

    async def _verify_while_downloading(self, progress: FutureProgress, episode: Episode, result: SearchResult,
                                        outfile: str, task: asyncio.Task[Any]) -> tuple[Any, ...] | None:
        """
        Periodically checks the partially downloaded file, cancelling the
        download task if it fails verification.

        :returns: the reason for cancelling the download, or None if the file
                  was verified (or couldn't be).
        """
        while not task.done():
            await asyncio.sleep(VERIFY_INTERVAL)
            if progress.pos < VERIFY_AFTER_BYTES:
                continue
            try:
                verified = await self.verify_result_file(episode, result, outfile)
            except RetrieverError as e:
                log.info('cancelling download: %s', e)
                task.cancel()
                return e.args
            if verified is not False or progress.percentage >= 100:
                # Either verified ok, or no ability to get metadata.  Either
                # way, we're done checking.
                return None
        return None

    def _download_progress_cb(self, ep: Episode, progress: FutureProgress) -> None:
        log.debug('[%s %s] %d KB/s, %d KB / %d KB', ep.series.name, ep.code, progress.speed / 1024,
                  progress.pos / 1024, progress.max / 1024)

    async def _retrieve(self, progress: FutureProgress, episode: Episode, result: SearchResult,
                        outfile: str) -> None:
        """
        Retrieve the given SearchResult object.
        """
        rdata = await result.get_retriever_data()
        if not rdata.get('url'):
            raise RetrieverError('Searcher did not provide a URL')

        opts: dict[str, Any] = {}
        if 'username' in rdata:
            opts['auth'] = (str(rdata['username']), str(rdata.get('password', '')))
        if 'retry' in rdata:
            opts['retry'] = int(rdata['retry'])

        # Before we start fetching, initialize progress.
        if result.size:
            progress.set(0, result.size, 0)

        log.debug('fetching %s', rdata['url'])
        progress.connect(self._download_progress_cb, episode)
        task = asyncio.create_task(download(rdata['url'], outfile, progress=progress, **opts))
        verifier = asyncio.create_task(self._verify_while_downloading(progress, episode, result, outfile, task))
        try:
            status, written = await task
        except asyncio.CancelledError:
            if verifier.done() and (reason := verifier.result()):
                raise RetrieverAbortedSoft(*reason) from None
            raise
        finally:
            verifier.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await verifier
            progress.disconnect(self._download_progress_cb)

        if status == 416:
            log.info('file already fully retrieved')
        elif status not in (200, 206):
            raise RetrieverError(f'download failed (status {status})')
