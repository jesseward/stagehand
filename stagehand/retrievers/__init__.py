from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..config import config
from ..utils import invoke_plugins, load_plugins
from .base import (
    RetrieverAborted,
    RetrieverAbortedHard,
    RetrieverAbortedSoft,
    RetrieverError,
    RetrieverHardError,
    RetrieverSoftError,
)

if TYPE_CHECKING:
    from ..manager import Manager
    from ..searchers.base import SearchResult
    from ..toolbox import FutureProgress
    from ..tvdb import Episode

__all__ = ['RetrieverAborted', 'RetrieverAbortedHard', 'RetrieverAbortedSoft', 'RetrieverError',
           'RetrieverHardError', 'RetrieverSoftError', 'start', 'retrieve', 'plugins', 'broken_plugins']

log = logging.getLogger('stagehand.retrievers')

plugins, broken_plugins = load_plugins('retrievers', ['http'])


async def start(manager: Manager) -> None:
    """
    Called when the manager is starting.
    """
    await invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load retriever plugin %s: %s', name, error)


async def retrieve(progress: FutureProgress, result: SearchResult, outfile: str, episode: Episode,
                   skip: tuple[str, ...] = ()) -> None:
    """
    Given a SearchResult object, retrieve the file using retriever plugins that
    support the result type.

    :raises RetrieverError: if none of the retrievers could fetch the result
    """
    tried = set()
    always = [name for name in plugins if plugins[name].Retriever.ALWAYS_ENABLED]
    suppressed_exceptions: list[Exception] = []
    for name in list(config.retrievers.enabled) + always:
        if name not in plugins or name in skip or result.type not in plugins[name].Retriever.SUPPORTED_TYPES \
           or name in tried:
            continue

        tried.add(name)
        retriever = plugins[name].Retriever()
        try:
            await retriever.retrieve(progress, episode, result, outfile)
        except RetrieverAbortedSoft as e:
            # Happens when the retriever itself aborts the download, e.g. because
            # the file failed to meet the resolution requirements.
            log.info('retriever %s aborted transfer of %s: %s', name, result.filename, e.args[0])
            raise
        except asyncio.CancelledError:
            # Happens when something outside (like the manager) wants to abort
            # retrieval of this episode altogether.
            log.info('transfer of %s was permanently aborted', result.filename)
            raise RetrieverAbortedHard('aborted') from None
        except RetrieverError as e:
            log.error('retriever %s failed to retrieve %s: %s', name, result.filename, e.args[0])
            suppressed_exceptions.append(e)
        except Exception as e:
            log.exception('retriever failed with unhandled error')
            suppressed_exceptions.append(e)
        else:
            # Retrieved without error
            return

    if not tried:
        raise RetrieverSoftError(f'No enabled retriever found for the given result ({result.type})')

    # If there are any RetrieverHardErrors or non-RetrieverErrors, reraise the
    # first such exception so at least we have something meaningful to pass up
    # to the manager.
    important = [e for e in suppressed_exceptions
                 if isinstance(e, RetrieverHardError) or not isinstance(e, RetrieverError)]
    if important:
        raise important[0]
    raise RetrieverSoftError('No retriever plugins were able to fetch the file')
