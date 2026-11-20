"""
Tests for toolbox.net.download against a local aiohttp server.
"""
import io

import pytest
from aiohttp import web

from stagehand.toolbox import FutureProgress
from stagehand.toolbox.net import DownloadError, close_session, download

PAYLOAD = bytes(range(256)) * 1024  # 256 KiB


def ranged(request, body):
    """Minimal Range support, as served by most file hosts."""
    rng = request.headers.get('Range')
    if not rng:
        return web.Response(body=body)
    start = int(rng.removeprefix('bytes=').rstrip('-'))
    if start >= len(body):
        return web.Response(status=416, headers={'Content-Range': f'bytes */{len(body)}'})
    return web.Response(status=206, body=body[start:],
                        headers={'Content-Range': f'bytes {start}-{len(body) - 1}/{len(body)}'})


@pytest.fixture
async def server(aiohttp_server):
    state = {'flaky': 0, 'requests': []}

    async def file(request):
        state['requests'].append(dict(request.headers))
        return ranged(request, PAYLOAD)

    async def norange(request):
        state['requests'].append(dict(request.headers))
        return web.Response(body=PAYLOAD)

    async def flaky(request):
        state['flaky'] += 1
        if state['flaky'] < 3:
            return web.Response(status=503)
        return web.Response(text='finally')

    async def auth(request):
        if request.headers.get('Authorization') != 'Basic dXNlcjpwYXNz':
            return web.Response(status=401)
        return web.Response(text='secret')

    async def echo(request):
        return web.json_response({'method': request.method, 'body': await request.text(),
                                  'x': request.headers.get('X-Test')})

    app = web.Application()
    app.router.add_get('/file', file)
    app.router.add_get('/norange', norange)
    app.router.add_get('/flaky', flaky)
    app.router.add_get('/auth', auth)
    app.router.add_route('*', '/echo', echo)
    srv = await aiohttp_server(app)
    srv.state = state
    yield srv
    await close_session()


async def test_download_to_memory(server):
    status, body = await download(str(server.make_url('/file')))
    assert status == 200
    assert body == PAYLOAD


async def test_download_to_file_with_progress(server, tmp_path):
    target = tmp_path / 'out.bin'
    progress = FutureProgress()
    status, written = await download(str(server.make_url('/file')), target, progress=progress)
    assert (status, written) == (200, len(PAYLOAD))
    assert target.read_bytes() == PAYLOAD
    assert progress.pos == progress.max == len(PAYLOAD)
    assert progress.percentage == 100


async def test_resume(server, tmp_path):
    target = tmp_path / 'out.bin'
    target.write_bytes(PAYLOAD[:1000])
    progress = FutureProgress()
    status, written = await download(str(server.make_url('/file')), target, progress=progress)
    assert status == 206
    assert written == len(PAYLOAD) - 1000
    assert target.read_bytes() == PAYLOAD
    assert server.state['requests'][-1]['Range'] == 'bytes=1000-'
    assert progress.pos == len(PAYLOAD)


async def test_resume_already_complete(server, tmp_path):
    target = tmp_path / 'out.bin'
    target.write_bytes(PAYLOAD)
    assert await download(str(server.make_url('/file')), target) == (416, 0)
    assert target.read_bytes() == PAYLOAD


async def test_resume_unsupported_restarts(server, tmp_path):
    target = tmp_path / 'out.bin'
    target.write_bytes(b'garbage')
    status, written = await download(str(server.make_url('/norange')), target)
    assert (status, written) == (200, len(PAYLOAD))
    assert target.read_bytes() == PAYLOAD
    # First request asked for a range, the retry didn't.
    assert 'Range' in server.state['requests'][0]
    assert 'Range' not in server.state['requests'][1]


async def test_no_resume_truncates(server, tmp_path):
    target = tmp_path / 'out.bin'
    target.write_bytes(b'x' * 10)
    await download(str(server.make_url('/file')), target, resume=False)
    assert target.read_bytes() == PAYLOAD


async def test_download_to_file_object(server):
    buf = io.BytesIO()
    status, written = await download(str(server.make_url('/file')), buf, resume=False)
    assert status == 200 and buf.getvalue() == PAYLOAD


async def test_retry_on_server_error(server):
    status, body = await download(str(server.make_url('/flaky')), retry=3, retry_delay=0)
    assert (status, body) == (200, b'finally')
    assert server.state['flaky'] == 3


async def test_retries_exhausted(server):
    assert await download(str(server.make_url('/flaky')), retry=1, retry_delay=0) == (503, None)


async def test_client_error_not_retried(server):
    assert await download(str(server.make_url('/missing')), retry=5, retry_delay=0) == (404, None)
    with pytest.raises(DownloadError) as exc:
        await download(str(server.make_url('/missing')), noraise=False)
    assert exc.value.status == 404


async def test_connection_error(unused_tcp_port):
    assert await download(f'http://127.0.0.1:{unused_tcp_port}/', retry_delay=0) == (0, None)
    with pytest.raises(DownloadError):
        await download(f'http://127.0.0.1:{unused_tcp_port}/', noraise=False)


async def test_auth(server):
    assert (await download(str(server.make_url('/auth'))))[0] == 401
    assert await download(str(server.make_url('/auth')), auth=('user', 'pass')) == (200, b'secret')


async def test_method_headers_and_data(server):
    import json
    status, body = await download(str(server.make_url('/echo')), method='POST', data='hello',
                                  headers={'X-Test': 'yes'})
    assert json.loads(body) == {'method': 'POST', 'body': 'hello', 'x': 'yes'}
