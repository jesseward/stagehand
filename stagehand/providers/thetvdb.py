from __future__ import annotations

import itertools
import json
import logging
import time
import urllib.parse
from typing import Any

from ..toolbox import db
from ..toolbox.net import download
from .base import ProviderBase, ProviderError, ProviderSearchResultBase, SeriesDict, parse_airtime

__all__ = ['Provider']

log = logging.getLogger('stagehand.providers.thetvdb')


class ProviderSearchResult(ProviderSearchResultBase):
    @property
    def pid(self) -> str:
        # tvdb_id is a plain numeric string; id may be prefixed like "series-NNNN"
        raw = self._attrs.get('tvdb_id') or self._attrs.get('id', '')
        return str(raw).split('-')[-1]

    @property
    def overview(self) -> str | None:
        return self._attrs.get('overview')

    @property
    def started(self) -> str | None:
        return self._attrs.get('first_air_time')

    @property
    def banner(self) -> str | None:
        return self._attrs.get('image_url')


class Provider(ProviderBase):
    NAME = 'thetvdb'
    NAME_PRINTABLE = 'TheTVDB'
    IDATTR = 'thetvdbid'
    CACHEATTR = 'thetvdbcache'

    # Tokens are valid for 1 month; trim a bit to be safe.
    TOKEN_LIFETIME_SECONDS = 29 * 24 * 3600

    API_URL = 'https://api4.thetvdb.com/v4'

    def __init__(self, tvdb: Any) -> None:
        super().__init__(tvdb)
        self._apikey = '17b1544f-5e79-4145-9e61-db722dd252c3'
        self._token: str | None = None
        self._token_time = 0.0

        tvdb.register_object_type_attrs('series',
            thetvdbid=(str, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
            thetvdbcache=(dict, db.ATTR_SIMPLE),
        )
        tvdb.register_object_type_attrs('episode',
            thetvdbid=(str, db.ATTR_SEARCHABLE),
        )

    async def _rawapi(self, path: str, token: str | None = None, method: str = 'GET',
                      body: str | None = None) -> tuple[int, Any]:
        url = self.API_URL + path
        headers = {
            'Accept': 'application/json',
        }
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if body:
            headers['Content-Type'] = 'application/json'
        status, data = await download(url, retry=4, method=method, headers=headers, data=body)
        return status, json.loads(data) if data else None

    async def _login(self) -> str:
        body = json.dumps({'apikey': self._apikey})
        status, response = await self._rawapi('/login', method='POST', body=body)
        if status == 200 and response and response.get('data', {}).get('token'):
            return str(response['data']['token'])
        log.error('thetvdb login failed: %s', response)
        raise ProviderError('thetvdb API login failed')

    async def _api(self, path: str, method: str = 'GET', body: str | None = None) -> tuple[int, Any]:
        """
        Invokes an API method, logging in or refreshing the token if necessary.
        """
        now = time.time()
        if not self._token or now - self._token_time > Provider.TOKEN_LIFETIME_SECONDS:
            # Acquire a new token
            self._token = await self._login()
            self._token_time = now

        status, response = await self._rawapi(path, self._token, method, body)
        log.debug('API %s returned status %d', path, status)
        if status == 401:
            if self._token_time == now:
                raise ProviderError('thetvDB API refused token')
            else:
                # Token was refused before expiry.  Clear token and recurse to cause relogin.
                self._token = None
                status, response = await self._api(path, method, body)
        elif status != 200:
            log.debug('API %s returned status %d', path, status)
        return status, response


    async def search(self, name: str) -> list[ProviderSearchResultBase]:
        results: list[ProviderSearchResultBase] = []
        quoted = urllib.parse.quote(name.replace('-', ' ').replace('_', ' '))
        log.info('searching TheTVDB for %s', name)
        status, response = await self._api('/search?query=' + quoted + '&type=series')
        if status == 200:
            if 'data' not in response:
                log.warning('data element missing from response')
            else:
                for result in response['data']:
                    results.append(ProviderSearchResult(self, result))
        return results


    async def get_series(self, id: str) -> SeriesDict:
        log.debug('retrieving series data for %s', id)
        if not self.get_last_updated():
            # DB doesn't know about server time.  Set to current time so that
            # subsequent calls to get_changed_series_ids() have a reference
            # point.
            self.db.set_metadata('thetvdb::servertime', int(time.time()))

        series: SeriesDict = {'episodes': []}
        log.info('fetching series %s from TheTVDB', id)

        # Use the extended endpoint to get genres, airsTime, and remoteIds,
        # with short=true to skip the heavy artworks/characters payload.
        status, response = await self._api(f'/series/{id}/extended?short=true')
        if status != 200:
            return series
        elif 'data' not in response:
            log.warning('data element missing from response')
            return series

        data = response['data']

        try:
            series['runtime'] = int(data['averageRuntime'])
        except (ValueError, KeyError, TypeError):
            pass

        airtime = parse_airtime(data.get('airsTime'))
        if airtime:
            series['airtime'] = airtime

        # Get any existing series and see if we need to fetch banner data.
        existing = self.db.get_series_by_id(f"thetvdb:{data['id']}")
        missing = not existing or not existing.banner_data
        image_url = data.get('image')
        if missing and image_url:
            log.debug('refresh series banner %s', image_url)
            status, banner_data = await download(image_url, retry=3)
            if status == 200 and banner_data:
                series['banner_data'] = banner_data
            else:
                log.error('banner download failed for series %s', data.get('name', data['id']))

        from ..tvdb import Series
        status_obj = data.get('status') or {}
        status_name = (status_obj.get('name') or '').lower()
        if status_name.startswith('cont'):        # continuing
            series_status = Series.STATUS_RUNNING
        elif status_name.startswith('on'):        # on hiatus
            series_status = Series.STATUS_SUSPENDED
        elif status_name.startswith('end'):       # ended
            series_status = Series.STATUS_ENDED
        elif status_name.startswith('upcom'):     # upcoming
            series_status = Series.STATUS_RUNNING
        else:
            series_status = Series.STATUS_UNKNOWN

        # Extract IMDB id from remoteIds list
        imdbid = None
        for remote in data.get('remoteIds') or []:
            if remote.get('sourceName', '').upper() == 'IMDB':
                imdbid = remote.get('id')
                break

        genres = [g['name'].strip().lower() for g in (data.get('genres') or []) if g.get('name')]

        series.update({
            'id': str(data['id']),
            'name': data.get('name'),
            'poster': image_url,
            'banner': image_url,
            'overview': data.get('overview'),
            'genres': genres,
            'started': data.get('firstAired'),
            'status': series_status,
            'imdbid': imdbid,
        })

        # Iterate over all pages of episodes.
        for page in itertools.count(0):
            ep_status, ep_response = await self._api(f'/series/{id}/episodes/default?page={page}')
            if ep_status != 200:
                break
            elif 'data' not in ep_response:
                log.warning('data element missing from episodes response')
                break

            episodes = ep_response['data'].get('episodes') or []
            for episode in episodes:
                try:
                    series['episodes'].append({
                        'id': str(episode['id']),
                        'name': episode.get('name'),
                        'season': int(episode['seasonNumber']),
                        'episode': int(episode['number']),
                        'airdate': episode.get('aired'),
                        'overview': episode.get('overview'),
                    })
                except Exception as e:
                    log.exception('failed to extract episode details: %s %s', e, episode)

            links = ep_response.get('links') or {}
            if not links.get('next'):
                break

        return series


    async def get_changed_series_ids(self) -> list[str] | None:
        servertime = self.get_last_updated()
        if not servertime:
            # No servertime stored, so there must not be any series in db.
            return None
        now = int(time.time())

        # Grab all series ids currently in the DB.
        series = {o[self.IDATTR] for o in self.db.query(type='series', attrs=[self.IDATTR])}

        # The updates endpoint only supports lookback of up to ~3 months.
        # If we're beyond that window, refresh everything.
        max_lookback = 60 * 60 * 24 * 85  # ~85 days, safely under 3 months
        if now - servertime > max_lookback:
            log.warning("haven't updated in over 85 days, returning all series")
            self.db.set_metadata('thetvdb::servertime', now)
            return list(series)

        ids = []
        path: str | None = f'/updates?since={servertime}&type=series'
        while path:
            status, response = await self._api(path)
            if status == 200:
                if 'data' not in response:
                    log.warning('data element missing from response')
                    break
                elif response['data']:
                    for result in response['data']:
                        ids.append(str(result['recordId']))
                # Follow pagination
                links = response.get('links') or {}
                next_url = links.get('next')
                if next_url:
                    # Strip base URL, keep just the path+query
                    path = next_url.replace(self.API_URL, '')
                else:
                    path = None
            else:
                log.warning('updates API returned status %d', status)
                break

        self.db.set_metadata('thetvdb::servertime', now)
        log.debug('set servertime %s', now)
        return ids


    def get_last_updated(self) -> int:
        return int(self.db.get_metadata('thetvdb::servertime', 0))
