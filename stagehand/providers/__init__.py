from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..utils import invoke_plugins, load_plugins
from .base import ProviderError

if TYPE_CHECKING:
    from ..manager import Manager

__all__ = ['ProviderError', 'start', 'plugins', 'broken_plugins']

log = logging.getLogger('stagehand.providers')

plugins, broken_plugins = load_plugins('providers', ['thetvdb', 'tvmaze'])


async def start(manager: Manager) -> None:
    """
    Called when the manager is starting.
    """
    await invoke_plugins(plugins, 'start', manager)
    for name, error in broken_plugins.items():
        log.warning('failed to load provider plugin %s: %s', name, error)
