from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Any

from ..toolbox import db
from ..toolbox.net import download
from .base import ProviderBase, ProviderSearchResultBase, SeriesDict, parse_airtime

__all__ = ['Provider']

log = logging.getLogger('stagehand.providers.tvmaze')


class ProviderSearchResult(ProviderSearchResultBase):
    @property
    def pid(self) -> str:
        return str(self._attrs.get('id'))

    @property
    def overview(self) -> str | None:
        return self._attrs.get('summary')

    @property
    def imdb(self) -> str | None:
        return (self._attrs.get('externals') or {}).get('imdb')

    @property
    def started(self) -> str | None:
        return self._attrs.get('premiered')

    @property
    def banner(self) -> str | None:
        return None

    @property
    def poster(self) -> str | None:
        return (self._attrs.get('image') or {}).get('original')


class Provider(ProviderBase):
    NAME = 'tvmaze'
    NAME_PRINTABLE = 'TVmaze'
    IDATTR = 'tvmazeid'
    CACHEATTR = 'tvmazecache'
    HOSTNAME = 'https://api.tvmaze.com'

    def __init__(self, tvdb: Any) -> None:
        super().__init__(tvdb)
        tvdb.register_object_type_attrs('series',
            tvmazeid=(str, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
            tvmazecache=(dict, db.ATTR_SIMPLE),
        )
        tvdb.register_object_type_attrs('episode',
            tvmazeid=(str, db.ATTR_SEARCHABLE),
        )

    async def _api(self, path: str) -> tuple[int, Any]:
        status, data = await download(self.HOSTNAME + path, retry=4)
        log.debug('API %s returned status %d', path, status)
        return status, json.loads(data) if data else None

    async def search(self, name: str) -> list[ProviderSearchResultBase]:
        results: list[ProviderSearchResultBase] = []
        quoted = urllib.parse.quote(name.replace('-', ' ').replace('_', ' '))
        log.info('searching TVmaze for %s', name)
        status, response = await self._api('/search/shows?q=' + quoted)
        if status == 200:
            if not isinstance(response, list):
                log.warning('response malformed (expected list, got %s)', type(response))
            else:
                results.extend(ProviderSearchResult(self, result['show']) for result in response)
        return results

    async def get_series(self, id: str) -> SeriesDict:
        log.debug('retrieving series data for %s', id)
        if not self.get_last_updated():
            # DB doesn't know about server time.  Set to current time so that
            # subsequent calls to get_changed_series_ids() have a reference
            # point.
            self.db.set_metadata('tvmaze::servertime', int(time.time()))

        series: SeriesDict = {'episodes': []}
        log.info('fetching series %s from TVmaze', id)
        status, response = await self._api(f'/shows/{id}')
        if status != 200:
            return series
        elif 'id' not in response:
            log.warning('id element missing from response')
            return series

        if response.get('runtime'):
            series['runtime'] = int(response['runtime'])
        airtime = parse_airtime((response.get('schedule') or {}).get('time'))
        if airtime:
            series['airtime'] = airtime

        # Get any existing series and see if we need to fetch banner data.
        existing = self.db.get_series_by_id(f"tvmaze:{response['id']}")
        missing = not existing or not existing.banner_data
        image_url = (response.get('image') or {}).get('original')
        if image_url and missing:
            log.debug('refresh series banner %s', image_url)
            status, banner_data = await download(image_url, retry=3)
            if status == 200 and banner_data:
                series['banner_data'] = banner_data
            else:
                log.error('banner download failed for series %s', response.get('name', response['id']))

        from ..tvdb import Series
        status_str = (response.get('status') or '').lower()
        if status_str.startswith('run'):
            series_status = Series.STATUS_RUNNING
        elif status_str.startswith('end'):
            series_status = Series.STATUS_ENDED
        else:
            series_status = Series.STATUS_UNKNOWN

        series.update({
            'id': str(response['id']),
            'name': response.get('name'),
            'poster': image_url,
            'overview': response.get('summary'),
            'genres': [g.strip().lower() for g in response.get('genres') or [] if g],
            'started': response.get('premiered'),
            'status': series_status,
            'imdbid': (response.get('externals') or {}).get('imdb'),
        })

        status, response = await self._api(f'/shows/{id}/episodes')
        if status == 200 and isinstance(response, list):
            for episode in response:
                try:
                    series['episodes'].append({
                        'id': str(episode['id']),
                        'name': episode.get('name'),
                        'season': int(episode['season']),
                        'episode': int(episode['number']),
                        'airdate': episode.get('airdate') or None,
                        'overview': episode.get('summary'),
                    })
                except (KeyError, TypeError, ValueError):
                    # Typically specials which have no episode number.
                    log.debug('skipping episode with incomplete details: %s', episode)
        else:
            log.warning('bad response for episodes list (status=%d type=%s)', status, type(response))

        return series

    async def get_changed_series_ids(self) -> list[str] | None:
        servertime = self.get_last_updated()
        if not servertime:
            # No servertime stored, so there must not be any series in db.
            return None

        now = int(time.time())
        ids = []
        status, response = await self._api('/updates/shows')
        if status == 200 and isinstance(response, dict):
            ids = [id for id, timestamp in response.items() if timestamp > servertime]
            self.db.set_metadata('tvmaze::servertime', now)
            log.debug('set servertime %s', now)
        else:
            log.warning('bad response for changed series (status=%d type=%s)', status, type(response))
        return ids

    def get_last_updated(self) -> int:
        return int(self.db.get_metadata('tvmaze::servertime', 0))
