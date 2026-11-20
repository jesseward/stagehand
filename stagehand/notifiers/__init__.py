from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..config import config
from ..utils import invoke_plugins, load_plugins
from .base import NotifierError

if TYPE_CHECKING:
    from ..manager import Manager
    from ..tvdb import Episode

__all__ = ['NotifierError', 'start', 'notify', 'plugins', 'broken_plugins']

log = logging.getLogger('stagehand.notifiers')
plugins, broken_plugins = load_plugins('notifiers', ['email', 'xbmc', 'web'])


async def start(manager: Manager) -> None:
    """
    Called when the manager is starting.
    """
    await invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load notifier plugin %s: %s', name, error)


async def notify(episodes: Sequence[Episode], skip: tuple[str, ...] = ()) -> None:
    """
    Notifies all enabled notifier plugins about newly retrieved episodes.
    """
    for name in config.notifiers.enabled:
        if name not in plugins or name in skip:
            continue
        notifier = plugins[name].Notifier()
        try:
            await notifier.notify(episodes)
        except NotifierError as e:
            log.error('notifier %s failed: %s', name, e.args[0])
        except Exception:
            log.exception('notifier failed with unhandled error')
