import asyncio
import pathlib

from conftest import make_async

from stagehand import searchers
from stagehand.config import config
from stagehand.searchers.base import SearchResult
from stagehand.searchers.easynews import Searcher as EasynewsSearcher
from stagehand.tvdb import Episode


def test_manager_writes_config_file(manager):
    text = pathlib.Path(manager.paths.config).read_text()
    assert 'web.port' in text


async def test_add_series(manager):
    series = await manager.add_series('tvmaze:100')
    assert series.name == 'Test Show'
    assert [cfg.id for cfg in config.series] == ['tvmaze:100']
    cfg = config.series[0]
    assert cfg.path == 'Test_Show'
    assert cfg.identifier == 'epcode'
    # Episodes that aired in the past are ignored on add.
    for ep in series.episodes:
        assert ep.status == Episode.STATUS_IGNORE
    # Adding the same series again returns the existing one.
    assert await manager.add_series('tvmaze:100') == series


async def test_delete_series(manager):
    await manager.add_series('tvmaze:100')
    manager.delete_series('tvmaze:100')
    assert len(config.series) == 0
    assert manager.tvdb.get_series_by_id('tvmaze:100') is None


async def test_check_new_episodes_queues_results(manager, monkeypatch):
    series = await manager.add_series('tvmaze:100')
    ep = series.get_episode_by_code('s01e02')
    ep.status = Episode.STATUS_NEED_FORCED
    config.searchers.enabled.append('easynews')

    result = SearchResult(EasynewsSearcher(), filename='Test.Show.S01E02.mkv', size=1, url='u')
    calls = []

    def fake_search(series, episodes, *args, **kwargs):
        calls.append((series, list(episodes)))
        return {episodes[0]: [result]}

    monkeypatch.setattr(searchers, 'search', make_async(fake_search))
    need, found = await manager.check_new_episodes()
    assert need == {series: [ep]}
    assert found == [ep]
    assert calls == [(series, [ep])]
    assert manager.is_episode_queued_for_retrieval(ep)
    assert manager.retrieve_queue == [(ep, [result])]

    # Already queued, so a second check doesn't search again.
    need, found = await manager.check_new_episodes()
    assert need == {}
    assert len(calls) == 1

    manager.cancel_episode_retrieval(ep)
    assert not manager.is_episode_queued_for_retrieval(ep)


async def test_check_new_episodes_skips_paused(manager, monkeypatch):
    series = await manager.add_series('tvmaze:100')
    series.get_episode_by_code('s01e02').status = Episode.STATUS_NEED_FORCED
    series.cfg.paused = True
    need, found = await manager.check_new_episodes()
    assert need == {}


async def test_schedule_next_episode_check(manager):
    config.searchers.hours = '3, 15'
    manager._schedule_next_episode_check()
    handle = manager._next_episode_check_timer
    delay = handle.when() - asyncio.get_running_loop().time()
    assert 0 < delay <= 24 * 3600 + 60
    # Rescheduling replaces (and cancels) the old timer.
    manager._schedule_next_episode_check()
    assert handle.cancelled()


async def test_schedule_with_invalid_hours_uses_default(manager):
    config.searchers.hours = 'garbage'
    manager._schedule_next_episode_check()
    assert manager._next_episode_check_timer is not None
