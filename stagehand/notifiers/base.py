from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..tvdb import Episode


class NotifierError(Exception):
    pass


class NotifierBase:
    async def _notify(self, episodes: Sequence[Episode]) -> None:
        raise NotImplementedError

    async def notify(self, episodes: Sequence[Episode]) -> None:
        await self._notify(episodes)
