"""
Characterization tests for the TV database: adding series from the metadata
providers, merging episodes across providers, and the Series/Episode model.
"""
import asyncio
from datetime import datetime

import provider_data
import pytest

from stagehand.config import config
from stagehand.tvdb import Episode, Series


@pytest.fixture
async def show(tvdb, fake_http):
    """Adds "Test Show" via TVmaze, which is then matched up on TheTVDB."""
    provider_data.install(fake_http)
    config.series.append(config.series(id='tvmaze:100', path='Test_Show', provider='tvmaze'))
    series = await tvdb.add_series_by_id('tvmaze:100', fast=False)
    assert series is not None
    return tvdb.get_series_by_id('tvmaze:100')


async def test_search_results(tvdb, fake_http):
    provider_data.install(fake_http)
    results = await tvdb.search('Test Show', provider='thetvdb')
    assert [r.id for r in results] == ['thetvdb:200', 'thetvdb:999']
    assert results[0].name == 'Test Show'
    assert results[0].year == '2020'
    assert results[0].started == '2020-01-05'

    results = await tvdb.search('Test Show', provider='tvmaze')
    assert [r.id for r in results] == ['tvmaze:100']
    assert results[0].imdb == 'tt0000123'


def test_add_series_matches_across_providers(show):
    assert show.name == 'Test Show'
    assert show.provider.NAME == 'tvmaze'
    assert sorted(show.ids) == ['thetvdb:200', 'tvmaze:100']
    assert show.id == 'tvmaze:100'
    assert show.runtime == 30
    assert show.status == Series.STATUS_ENDED
    assert show.imdbid == 'tt0000123'
    assert show.has_genre('comedy')
    assert not show.has_genre('horror')
    assert show.cfg.path == 'Test_Show'


def test_episodes_merged(show):
    eps = show.episodes
    codes = [ep.code for ep in eps]
    # Season 0 specials are included from any provider.
    assert codes == ['s00e01', 's01e01', 's01e02', 's01e03']
    ep = show.get_episode_by_code('s01e02')
    assert ep.name == 'The Second One'
    assert sorted(ep.ids) == ['thetvdb:2002', 'tvmaze:1002']
    assert ep.airdate == datetime(2020, 1, 12)
    assert ep.status == Episode.STATUS_NONE
    assert show.get_episode_by_code('1x3').name == 'Finale'
    assert show.get_episode_by_code('s09e09') is None


def test_tvmaze_airtime(show):
    assert show.airtime == '20:00'
    assert show.get_episode_by_code('s01e01').airdatetime == datetime(2020, 1, 5, 20, 0)


async def test_change_provider(show):
    await show.change_provider('thetvdb')
    assert show.provider.NAME == 'thetvdb'
    ep = show.get_episode_by_code('s01e02')
    # TheTVDB's name for the episode now takes precedence.
    assert ep.name == 'Second One'
    assert show.airtime == '20:00'
    with pytest.raises(ValueError):
        await show.change_provider('bogus')


def test_episode_status_and_files(show):
    ep = show.get_episode_by_code('s01e01')
    assert ep.aired
    # Aired more than 14 days ago, so it's obsolete unless explicitly needed.
    assert ep.obsolete
    assert not ep.ready
    ep.status = Episode.STATUS_NEED
    assert show.get_episode_by_code('s01e01').status == Episode.STATUS_NEED
    assert not ep.obsolete
    assert ep.ready
    ep.status = Episode.STATUS_NEED_FORCED
    assert ep.ready
    assert ep.path is None
    ep.filename = 'Test_Show-s01e01-Pilot.mkv'
    assert ep.path.endswith('Test_Show/s1/Test_Show-s01e01-Pilot.mkv')


def test_preferred_filename(show):
    ep = show.get_episode_by_code('s01e02')
    assert ep.preferred_filename == 'Test_Show-s01e02-The_Second_One'
    show.cfg.identifier = 'date'
    try:
        assert ep.preferred_filename == 'Test_Show-2020.01.12-The_Second_One'
    finally:
        show.cfg.identifier = 'epcode'


def test_season_path(show):
    season = show.seasons[1]
    assert season.number == 1
    assert season.path.endswith('Test_Show/s1')
    show.cfg.flat = True
    try:
        assert season.path == show.path
    finally:
        show.cfg.flat = False


def test_delete_series(show, tvdb):
    tvdb.delete_series(show)
    assert tvdb.get_series_by_id('tvmaze:100') is None
    assert tvdb.series == []


def test_episode_and_series_hashable(show):
    ep = show.get_episode_by_code('s01e01')
    assert {ep: 1}[ep] == 1
    assert {show: 1}[show] == 1
    season = show.seasons[1]
    assert {season: 1}[season] == 1


def test_get_conflicts_matches_fuzzy_names(tvdb):
    p = tvdb.providers
    pseries = {
        p['tvmaze']: {'episodes': [
            {'id': '1', 'name': 'The Beginning', 'season': 1, 'episode': 1, 'airdate': '2020-01-01'},
            {'id': '2', 'name': 'Middle', 'season': 1, 'episode': 2, 'airdate': '2020-01-08'},
        ]},
        p['thetvdb']: {'episodes': [
            {'id': 'a', 'name': 'Beginning', 'season': 1, 'episode': 1, 'airdate': '2020-01-01'},
            {'id': 'b', 'name': 'Something Else Entirely', 'season': 1, 'episode': 2, 'airdate': '2020-01-08'},
        ]},
    }
    n_real_conflicts, conflicts, matches, unmatched = tvdb._get_conflicts(pseries)
    matched_ids = sorted(ep['id'] for eps in matches.values() for _, ep in eps)
    assert matched_ids == ['1', 'a']
    assert n_real_conflicts == 1
    conflicted = sorted(ep['id'] for eps in conflicts.values() for _, ep in eps)
    assert conflicted == ['2', 'b']


async def test_sync_refreshes_changed_series(show, tvdb, fake_http):
    tvdb.set_metadata('tvmaze::servertime', 1)
    tvdb.set_metadata('thetvdb::servertime', 2 ** 40)  # far future: nothing changed
    renamed = dict(provider_data.TVMAZE_SHOW, name='Test Show Renamed')
    fake_http.routes.insert(0, (__import__('re').compile(r'api\.tvmaze\.com/shows/100$'), (200, renamed)))
    fake_http.add(r'api\.tvmaze\.com/updates/shows', (200, {'100': 1000, '555': 1}))
    count = await tvdb.sync()
    assert count == 1
    assert tvdb.get_series_by_id('tvmaze:100').name == 'Test Show Renamed'


async def test_series_objects_survive_cache_purge(show, tvdb):
    """
    References to Series objects held elsewhere (e.g. by the web layer) must
    stay current after the series cache is rebuilt.
    """
    tvdb.purge_caches()
    assert tvdb.get_series_by_id('tvmaze:100') is show
    ep = tvdb.get_series_by_id('tvmaze:100').get_episode_by_code('s01e01')
    ep.status = Episode.STATUS_IGNORE
    assert show.get_episode_by_code('s01e01').status == Episode.STATUS_IGNORE


async def test_update_tasks_are_untracked_when_done(tvdb, fake_http, caplog):
    provider_data.install(fake_http)
    await tvdb.add_series_by_id('tvmaze:100', fast=True)
    # Let the follow-up (full) update spawned by the fast path finish.
    for _ in range(50):
        if not tvdb._update_tasks_by_id:
            break
        await asyncio.sleep(0.01)
    assert tvdb._update_tasks_by_id == {}
    assert 'unexpectedly active' not in caplog.text
