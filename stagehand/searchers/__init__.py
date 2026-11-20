from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from ..config import config
from ..logger import getLogger
from ..utils import invoke_plugins, load_plugins
from .base import SearcherError, SearchResult

if TYPE_CHECKING:
    from ..manager import Manager
    from ..tvdb import Episode, Series

__all__ = ['SearcherError', 'SearchResult', 'start', 'search', 'plugins', 'broken_plugins']

log = getLogger('stagehand.searchers')

plugins, broken_plugins = load_plugins('searchers', ['easynews'])

# For each quality type, the minimum and ideal MB per minute of runtime.
MB_PER_MIN = {
    'UHD': (30, 120),
    'HD': (10, 25),
    'SD': (2, 8),
    'Any': (2, 20),
}


async def start(manager: Manager) -> None:
    """
    Called when the manager is starting.
    """
    await invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load searcher plugin %s: %s', name, error)


async def search(series: Series, episodes: list[Episode],
                 skip: tuple[str, ...] = ()) -> dict[Episode, list[SearchResult]]:
    """
    Searches for the given episodes of a series using the enabled searcher
    plugins in order of preference, returning the results from the first
    plugin that finds anything.
    """
    try:
        earliest = min(ep.airdate for ep in episodes if ep.airdate)
    except ValueError:
        # Empty sequence: no eps had an airdate.
        earliest_str = None
    else:
        # Allow for episodes to be posted 10 days before the supposed air date.
        earliest_str = (earliest - timedelta(days=10)).strftime('%Y-%m-%d')

    min_mb, ideal_mb = MB_PER_MIN[series.cfg.quality or 'Any']
    runtime = series.runtime or 30
    min_size_bytes = runtime * min_mb * 1024 * 1024
    ideal_size_bytes = runtime * ideal_mb * 1024 * 1024

    tried = set()
    always = [name for name in plugins if plugins[name].Searcher.ALWAYS_ENABLED]
    for name in list(config.searchers.enabled) + always:
        if name not in plugins or name in skip or name in tried:
            continue
        tried.add(name)
        searcher = plugins[name].Searcher()
        try:
            results = await searcher.search(series, episodes, earliest_str, min_size_bytes, ideal_size_bytes,
                                            series.cfg.quality)
        except SearcherError as e:
            log.error('%s failed: %s', name, e.args[0])
        except Exception:
            log.exception('%s failed with unhandled error', name)
        else:
            # FIXME: if some episodes don't have results, need to try other searchers.
            if results:
                return results
            log.debug2('%s found no results', name)
    return {}
