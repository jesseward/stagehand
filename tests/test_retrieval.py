"""
End-to-end tests of episode retrieval: Manager -> retrievers -> HTTP
retriever -> download, against a local HTTP server.
"""
import asyncio
import base64
import os

import pytest
from aiohttp import web

from stagehand import media
from stagehand.config import config
from stagehand.retrievers import http as http_retriever
from stagehand.retrievers.base import RetrieverError
from stagehand.searchers.base import SearchResult
from stagehand.searchers.easynews import Searcher, modconfig
from stagehand.tvdb import Episode

FILE = os.urandom(300 * 1024)
HASH = 'a' * 32


@pytest.fixture
async def fileserver(aiohttp_server):
    state = {'hits': 0, 'release': None}
    expected_auth = 'Basic ' + base64.b64encode(b'user:pass').decode()

    async def handler(request):
        state['hits'] += 1
        if request.headers.get('Authorization') != expected_auth:
            return web.Response(status=401)
        if request.match_info['name'] == 'slow.mkv':
            # Send part of the file, then wait until the test releases us.
            response = web.StreamResponse(headers={'Content-Length': str(len(FILE))})
            await response.prepare(request)
            await response.write(FILE[:64 * 1024])
            state['release'] = asyncio.Event()
            await state['release'].wait()
            await response.write(FILE[64 * 1024:])
            return response
        return web.Response(body=FILE, content_type='video/x-matroska')

    app = web.Application()
    app.router.add_get('/dl/{hash}/{name}', handler)
    server = await aiohttp_server(app)
    server.state = state
    return server


@pytest.fixture
async def show(manager, tmp_path):
    config.misc.tvdir = str(tmp_path / 'tv')
    modconfig.username = 'user'
    modconfig.password = 'pass'
    modconfig.retries = 0
    await manager.add_series('tvmaze:100')
    return manager.tvdb.get_series_by_id('tvmaze:100')


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch):
    monkeypatch.setattr(media, 'ffprobe_path', lambda: None)


def result_for(server, name='Test.Show.S01E01.720p.mkv'):
    result = SearchResult(Searcher())
    result.filename = name
    result.size = len(FILE)
    result.quality = 'HD'
    result.url = str(server.make_url(f'/dl/{HASH}/{name}'))
    return result


async def test_retrieve_episode(manager, show, fileserver):
    ep = show.get_episode_by_code('s01e01')
    ep.status = Episode.STATUS_NEED
    assert await manager._get_episode(ep, [result_for(fileserver)]) is True

    assert ep.status == Episode.STATUS_HAVE
    assert ep.filename
    with open(ep.path, 'rb') as f:
        assert f.read() == FILE
    assert manager.get_episode_retrieve_task(ep) is None


async def test_failed_result_falls_back_to_next(manager, show, fileserver):
    ep = show.get_episode_by_code('s01e02')
    bad = result_for(fileserver)
    bad.url = str(fileserver.make_url('/nope'))
    good = result_for(fileserver, 'Test.Show.S01E02.720p.mkv')
    assert await manager._get_episode(ep, [bad, good]) is True
    assert ep.status == Episode.STATUS_HAVE
    assert ep.filename.endswith('.mkv')


async def test_all_results_fail(manager, show, fileserver):
    ep = show.get_episode_by_code('s01e03')
    bad = result_for(fileserver)
    bad.url = str(fileserver.make_url('/nope'))
    assert await manager._get_episode(ep, [bad]) is False
    assert ep.filename is None
    assert ep.status != Episode.STATUS_HAVE
    # No partial files left behind
    assert not [f for f in os.listdir(ep.season.path) if f.endswith('.mkv')]


async def test_bad_credentials(manager, show, fileserver):
    modconfig.password = 'wrong'
    ep = show.get_episode_by_code('s01e01')
    assert await manager._get_episode(ep, [result_for(fileserver)]) is False


async def test_verification_failure_aborts_download(manager, show, fileserver, monkeypatch):
    """
    A file that fails verification while downloading is aborted (soft
    abort), and the next result is tried.
    """
    monkeypatch.setattr(media, 'ffprobe_path', lambda: '/usr/bin/ffprobe')
    monkeypatch.setattr(http_retriever, 'VERIFY_AFTER_BYTES', 1)
    monkeypatch.setattr(http_retriever, 'VERIFY_INTERVAL', 0.01)

    async def probe(path):
        return media.MediaInfo(1300.0, [media.VideoTrack(640, 360)], [media.AudioTrack('eng')])
    monkeypatch.setattr(media, 'probe', probe)

    ep = show.get_episode_by_code('s01e01')
    slow = result_for(fileserver, 'slow.mkv')
    assert await manager._get_episode(ep, [slow]) is False
    assert ep.filename is None
    assert not os.path.exists(os.path.join(ep.season.path, 'slow.mkv'))
    if fileserver.state['release']:
        fileserver.state['release'].set()


async def test_cancel_retrieval(manager, show, fileserver):
    ep = show.get_episode_by_code('s01e01')
    task = asyncio.create_task(manager._get_episode(ep, [result_for(fileserver, 'slow.mkv')]))
    for _ in range(100):
        if fileserver.state['release']:
            break
        await asyncio.sleep(0.01)
    progress = manager.get_episode_retrieve_progress(ep)
    assert progress is not None and progress.pos > 0
    manager.cancel_episode_retrieval(ep)
    with pytest.raises(RetrieverError):
        await task
    fileserver.state['release'].set()
    assert ep.filename is None
