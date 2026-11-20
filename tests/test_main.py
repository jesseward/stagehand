"""
Tests for the command line entry point: startup, the REST control API used
by --stop, and graceful shutdown.
"""
import argparse
import asyncio
import pathlib

import aiohttp
import pytest

from stagehand import main
from stagehand.config import config


@pytest.fixture
def portfile(tmp_path, monkeypatch):
    path = tmp_path / 'port'
    monkeypatch.setattr(main, 'port_file', lambda: str(path))
    return path


def args(port, **kwargs):
    return argparse.Namespace(port=port, verbose=[], quiet=False, background=False, data=None, stop=False,
                              **kwargs)


async def test_run_and_stop_via_api(paths, clean_config, fake_http, portfile, unused_tcp_port, monkeypatch):
    # Don't let the manager search or sync at startup.
    from stagehand.manager import Manager

    async def start(self):
        pass
    monkeypatch.setattr(Manager, 'start', start)

    assert await main.call_rest_api('/api/pid') is None  # nothing running yet
    runner = asyncio.create_task(main.run(paths, args(unused_tcp_port)))
    for _ in range(200):
        if portfile.exists():
            break
        await asyncio.sleep(0.01)
    assert portfile.read_text().strip() == str(unused_tcp_port)

    async with aiohttp.ClientSession() as session, session.get(f'http://127.0.0.1:{unused_tcp_port}/') as r:
        assert r.status == 200

    assert b'pid' in await main.call_rest_api('/api/pid')
    await main.call_rest_api('/api/shutdown')
    mgr = await asyncio.wait_for(runner, 5)
    assert mgr.paths == paths
    # Port file is removed on clean shutdown, and config saved.
    assert not portfile.exists()
    assert 'web.port' in pathlib.Path(paths.config).read_text()
    config.watch(False)


async def test_run_and_stop_with_digest_auth(paths, clean_config, fake_http, portfile, unused_tcp_port,
                                             monkeypatch):
    from stagehand.manager import Manager

    async def start(self):
        pass
    monkeypatch.setattr(Manager, 'start', start)
    config.web.username = 'admin'
    config.web.password = 'secret'
    runner = asyncio.create_task(main.run(paths, args(unused_tcp_port)))
    for _ in range(200):
        if portfile.exists():
            break
        await asyncio.sleep(0.01)
    async with aiohttp.ClientSession() as session, session.get(f'http://127.0.0.1:{unused_tcp_port}/') as r:
        assert r.status == 401
    # The control API authenticates with the configured credentials.
    await main.call_rest_api('/api/shutdown')
    await asyncio.wait_for(runner, 5)
    config.watch(False)


def test_default_paths(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    paths = main.default_paths()
    assert paths.config == str(tmp_path / 'cfg' / 'stagehand' / 'config')
    assert paths.db == str(tmp_path / 'cfg' / 'stagehand' / 'tv.db')
    assert paths.logs == str(tmp_path / 'cache' / 'stagehand' / 'logs')
    assert paths.data.endswith('data')
    with pytest.raises(FileNotFoundError):
        main.default_paths(str(tmp_path / 'missing'))


def test_version(capsys):
    with pytest.raises(SystemExit):
        main.main(['--version'])
    assert capsys.readouterr().out.startswith('stagehand ')
