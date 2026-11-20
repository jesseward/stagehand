"""
Tests for the aiohttp web interface: pages, the JSON API, async jobs,
static files and authentication.
"""
import asyncio
import gzip
import json

import aiohttp
import pytest

from stagehand.tvdb import Episode
from stagehand.web import create_app
from stagehand.web.context import SHUTDOWN
from stagehand.web.jobs import asyncweb
from stagehand.web.settings import rename_example


@pytest.fixture
async def show(manager):
    assert await manager.add_series('tvmaze:100') is not None
    # Fetch from the series cache, as the web handlers do.
    return manager.tvdb.get_series_by_id('tvmaze:100')


@pytest.fixture
async def client(aiohttp_client, manager, show):
    return await aiohttp_client(create_app(manager, manager.paths.data))


PAGES = [
    '/', '/tv/', '/tv/add', '/tv/upcoming', '/tv/tvmaze:100', '/downloads/', '/downloads/?weeks=4&status=all',
    '/settings/', '/settings/searchers', '/settings/retrievers', '/settings/notifiers', '/log/', '/log/web',
]


@pytest.mark.parametrize('path', PAGES)
async def test_pages_render(client, path):
    response = await client.get(path)
    text = await response.text()
    assert response.status == 200, text
    assert response.content_type == 'text/html'
    assert '</html>' in text
    # No CoffeeScript or jQuery remains.
    assert '.coffee' not in text
    assert 'jquery' not in text.lower()
    assert '$.' not in text


async def test_show_page_contents(client):
    text = await (await client.get('/tv/tvmaze:100')).text()
    assert 'Test Show' in text
    assert 'Pilot' in text


async def test_unknown_show_is_404(client):
    response = await client.get('/tv/tvmaze:99999')
    assert response.status == 404


async def test_session_cookie_is_set_once(client):
    response = await client.get('/')
    assert 'stagehand.session' in response.cookies
    response = await client.get('/')
    assert 'stagehand.session' not in response.cookies


async def test_static_files(client):
    response = await client.get('/static/css/stagehand.css')
    assert response.status == 200
    assert response.content_type == 'text/css'
    last_modified = response.headers['Last-Modified']

    response = await client.get('/static/css/stagehand.css', headers={'If-Modified-Since': last_modified})
    assert response.status == 304


async def test_static_rejects_traversal(client):
    response = await client.get('/static/../../config.py')
    assert response.status in (403, 404)
    response = await client.get('/static/%2e%2e/%2e%2e/config.py')
    assert response.status in (403, 404)


async def test_static_script(client):
    response = await client.get('/static/js/stagehand.js')
    assert response.status == 200
    assert 'class Stagehand' in await response.text()


async def test_static_missing(client):
    assert (await client.get('/static/nope.js')).status == 404


async def test_static_gzip(client, tmp_path, manager):
    import os
    import shutil
    data = tmp_path / 'data'
    shutil.copytree(manager.paths.data, data)
    with gzip.open(data / 'web' / 'test.txt.gz', 'wb') as f:
        f.write(b'hello world')
    app = create_app(manager, str(data))
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as c:
        r = await c.get('/static/test.txt.gz', headers={'Accept-Encoding': 'identity'}, auto_decompress=False)
        assert await r.read() == b'hello world'
        r = await c.get('/static/test.txt.gz', headers={'Accept-Encoding': 'gzip'}, auto_decompress=False)
        assert r.headers['Content-Encoding'] == 'gzip'
        assert gzip.decompress(await r.read()) == b'hello world'
    assert os.path.exists(data / 'web' / 'test.txt.gz')


async def test_api_overview(client):
    response = await client.get('/api/shows/tvmaze:100/overview')
    assert response.headers['Cache-Control'] == 'max-age=0,no-cache,no-store'
    assert 'tests' in (await response.json())['overview']

    response = await client.get('/api/shows/tvmaze:100/s01e01/overview')
    assert (await response.json()) == {'overview': 'First.'}
    response = await client.get('/api/shows/tvmaze:100/s09e09/overview')
    assert (await response.json()) == {'overview': 'Episode not found'}


async def test_api_settings(client, show):
    response = await client.post('/api/shows/tvmaze:100/settings', data={
        'quality': 'HD', 'path': 'Elsewhere', 'search_string': 'test show', 'language': 'en',
        'paused': 'true', 'flat': 'false', 'identifier': 'date',
    })
    assert response.status == 200
    assert show.cfg.quality == 'HD'
    assert show.cfg.path == 'Elsewhere'
    assert show.cfg.paused == True  # noqa: E712  (config bools are ints)
    assert show.cfg.flat == False  # noqa: E712
    assert show.cfg.identifier == 'date'


async def test_api_banner_missing(client):
    assert (await client.get('/api/shows/tvmaze:100/banner')).status == 404


async def poll_job(client, response):
    """
    Follows the async job protocol used by the web client until the job
    finishes, returning the job record.
    """
    data = await response.json()
    jobid = data['jobid']
    for _ in range(100):
        for job in data['jobs']:
            if job['id'] == jobid:
                return job, data
        await asyncio.sleep(0.01)
        data = await (await client.get('/api/jobs', params={'jobs': str(jobid)})).json()
    raise AssertionError('job never finished')


async def test_api_search_job(client):
    response = await client.get('/api/shows/search', params={'name': 'Test Show'})
    job, data = await poll_job(client, response)
    ids = [r['id'] for r in job['result']['results']]
    assert 'thetvdb:200' in ids
    alerts = [n for n in data['notifications'] if n['_ntype'] == 'alert']
    assert alerts and alerts[0]['title'] == 'Search results'


async def test_api_job_error_is_reported(client):
    response = await client.post('/api/shows/tvmaze:99999/refresh')
    job, _ = await poll_job(client, response)
    assert 'HTTPNotFound' in job['error']['message']


async def test_api_episode_status(client, show, manager, monkeypatch):
    checks = []
    monkeypatch.setattr(manager, 'check_new_episodes', lambda *a, **kw: checks.append(1))
    response = await client.post('/api/shows/tvmaze:100/episodes/s01e01,s01e02/status', params={'value': 'ignore'})
    job, _ = await poll_job(client, response)
    assert set(job['result']['statuses']) == {'s01e01', 's01e02'}
    assert show.get_episode_by_code('s01e01').status == Episode.STATUS_IGNORE
    assert not checks

    response = await client.post('/api/shows/tvmaze:100/episodes/s01e01/status', params={'value': 'need'})
    job, _ = await poll_job(client, response)
    assert 'error' not in job, job
    assert show.get_episode_by_code('s01e01').status == Episode.STATUS_NEED
    # The episode has aired, so the manager is asked to look for it.
    assert checks


async def test_api_episode_status_invalid(client):
    response = await client.post('/api/shows/tvmaze:100/episodes/s01e01/status', params={'value': 'bogus'})
    job, _ = await poll_job(client, response)
    assert 'error' in job


async def test_api_delete_show(client, manager):
    response = await client.delete('/api/shows/tvmaze:100')
    await poll_job(client, response)
    assert manager.tvdb.get_series_by_id('tvmaze:100') is None


async def test_api_pid_and_shutdown(client):
    assert 'pid' in await (await client.get('/api/pid')).json()
    event = client.app[SHUTDOWN]
    assert not event.is_set()
    assert (await client.get('/api/shutdown')).status == 200
    assert event.is_set()


async def test_rename_example():
    assert rename_example('/tv', '_', 'Season {season}', 's{season:02}e{episode:02}',
                          '{show}.{code}.{title}') == {'example': '/tv/Season 2/Dead_Like_Me.s02e04.The_Shallow_End'}
    assert 'error' in rename_example('/tv', '_', 'Season {bogus}', '', '')
    assert 'error' in rename_example('/tv', '_', 'Season {', '', '')


async def test_rename_example_endpoint(client):
    response = await client.get('/settings/rename_example', params={
        'separator': '.', 'season_dir_format': 'S{season}', 'code_style': '{season}x{episode:02}',
        'episode_format': '{show} {code}'})
    assert (await response.json())['example'].endswith('S2/Dead.Like.Me 2x04')


async def test_global_notifications_delivered_to_each_session(client):
    asyncweb.notify('alert', title='hello')
    r1 = await client.get('/api/jobs')
    assert any(n.get('title') == 'hello' for n in (await r1.json())['notifications'])
    # Same session doesn't see it again
    r2 = await client.get('/api/jobs')
    assert not any(n.get('title') == 'hello' for n in (await r2.json())['notifications'])


async def test_digest_auth(aiohttp_client, manager, show):
    client = await aiohttp_client(create_app(manager, manager.paths.data, user='admin', password='s3cret'))
    response = await client.get('/api/pid')
    assert response.status == 401
    assert response.headers['WWW-Authenticate'].startswith('Digest ')

    async with aiohttp.ClientSession(middlewares=(aiohttp.DigestAuthMiddleware('admin', 's3cret'),)) as session:
        async with session.get(client.make_url('/api/pid')) as r:
            assert r.status == 200
            assert 'pid' in json.loads(await r.text())
    async with aiohttp.ClientSession(middlewares=(aiohttp.DigestAuthMiddleware('admin', 'wrong'),)) as session:
        async with session.get(client.make_url('/api/pid')) as r:
            assert r.status == 401


async def test_web_server_start_stop(manager):
    from stagehand.web.server import WebServer
    server = WebServer()
    await server.start(manager, manager.paths.data, host='127.0.0.1', port=0)
    assert server.running
    await server.stop()
    assert not server.running
