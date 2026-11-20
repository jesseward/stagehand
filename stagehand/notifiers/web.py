"""
Notifies users of the web interface when episodes have been retrieved.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .base import NotifierBase

if TYPE_CHECKING:
    from ..tvdb import Episode

__all__ = ['Notifier']


class Notifier(NotifierBase):
    async def _notify(self, episodes: Sequence[Episode]) -> None:
        from .. import web  # imported here to avoid an import cycle at plugin load time

        if len(episodes) > 5:
            web.notify('alert', title='Episodes retrieved', text=f'Downloaded {len(episodes)} episodes')
            return
        for ep in episodes:
            web.notify('alert', title='Episode retrieved', text=f'Downloaded {ep.series.name} {ep.code}')
