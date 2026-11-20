from __future__ import annotations

import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

if TYPE_CHECKING:
    from ..tvdb import TVDB


class EpisodeDict(TypedDict, total=False):
    """Episode metadata as returned by a provider."""
    id: str
    name: str | None
    season: int
    episode: int
    airdate: str | None    # YYYY-MM-DD
    overview: str | None


class SeriesDict(TypedDict, total=False):
    """Series metadata as returned by a provider."""
    id: str
    name: str | None
    poster: str | None
    banner: str | None
    banner_data: bytes
    overview: str | None
    genres: list[str]
    started: str | None    # YYYY-MM-DD
    status: int
    imdbid: str | None
    runtime: int
    airtime: str           # HH:MM (24 hour)
    episodes: list[EpisodeDict]


class ProviderError(Exception):
    pass


class ProviderBase:
    """
    Base class for TV metadata providers.
    """
    NAME: ClassVar[str]
    NAME_PRINTABLE: ClassVar[str]
    # The database attribute holding this provider's id for series/episodes.
    IDATTR: ClassVar[str]
    # The database attribute holding this provider's cached series dict.
    CACHEATTR: ClassVar[str]

    def __init__(self, db: TVDB) -> None:
        self.db = db

    async def search(self, name: str) -> list[ProviderSearchResultBase]:
        raise NotImplementedError

    async def get_series(self, id: str) -> SeriesDict:
        raise NotImplementedError

    async def get_changed_series_ids(self) -> list[str] | None:
        """
        Return a list of provider ids of series that have changed since
        last invocation.
        """
        raise NotImplementedError

    def get_last_updated(self) -> int:
        """
        Return a timestamp since last synced with server.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f'<{self.__class__.__module__}.Provider {self.NAME}>'


class ProviderSearchResultBase:
    def __init__(self, provider: ProviderBase, attrs: dict[str, Any]) -> None:
        self.provider = provider
        self._attrs = attrs

    @property
    def pid(self) -> str:
        raise NotImplementedError

    @property
    def id(self) -> str:
        return self.provider.NAME + ':' + self.pid

    @property
    def name(self) -> str | None:
        return self._attrs.get('name')

    @property
    def names(self) -> Iterator[str]:
        if self.name:
            yield self.name

    @property
    def overview(self) -> str | None:
        raise NotImplementedError

    @property
    def imdb(self) -> str | None:
        return None

    @property
    def started(self) -> str | None:
        raise NotImplementedError

    @property
    def year(self) -> str | None:
        started = self.started
        if started and len(started.split('-')) == 3:
            return started.split('-')[0]
        return started


def parse_airtime(value: str | None) -> str | None:
    """
    Parses an air time from a provider, which may be in 24 hour (``20:00``)
    or 12 hour (``8:00 PM``) format.

    :returns: the time as HH:MM in 24 hour format, or None if it couldn't be
              parsed
    """
    if not value:
        return None
    for fmt in ('%H:%M', '%I:%M %p', '%I:%M%p'):
        try:
            return time.strftime('%H:%M', time.strptime(value.strip(), fmt))
        except ValueError:
            pass
    return None
