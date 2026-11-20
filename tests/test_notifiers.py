"""
Tests for the notifier plugins.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from stagehand.notifiers.email import Notifier as EmailNotifier


def episode(name, code, series='Test Show'):
    return SimpleNamespace(series=SimpleNamespace(name=series), code=code, name=name,
                           airdatetime=datetime(2020, 1, 5, 20, 0), overview=f'About {name}.')


def test_email_message():
    eps = [episode('Pilot', 's01e01'), episode('Second', 's01e02')]
    msg = EmailNotifier.build_message(eps, 'stagehand@example.com', ['a@example.com', 'b@example.com'])
    assert msg['Subject'] == '[stagehand] downloaded 2 episodes'
    assert msg['To'] == 'a@example.com, b@example.com'
    body = msg.get_content()
    assert '01: Test Show s01e01 Pilot' in body
    assert 'About Second.' in body


@pytest.fixture
async def fake_xbmc():
    """
    A fake XBMC/Kodi JSON-RPC server speaking newline-free JSON over TCP.
    """
    received = []

    async def handle(reader, writer):
        buf = b''
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf += data
            try:
                request = json.loads(buf)
            except ValueError:
                continue
            buf = b''
            received.append(request)
            method = request['method']
            if method == 'XBMC.GetInfoBooleans':
                result = {'library.isscanning': False}
            elif method == 'JSONRPC.Version':
                result = {'version': {'major': 12, 'minor': 0, 'patch': 0}}
            else:
                result = 'OK'
            writer.write(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    yield SimpleNamespace(port=port, received=received)
    server.close()
    await server.wait_closed()


async def test_xbmc_notify(fake_xbmc, clean_config, monkeypatch):
    from stagehand.notifiers import xbmc
    cfg = xbmc.modconfig
    clean_config.misc.tvdir = '/srv/tv'
    for name, value in (('hostname', '127.0.0.1'), ('tcp_port', fake_xbmc.port), ('tvdir', '/media/tv'),
                        ('individual', True), ('notify', True)):
        setattr(cfg, name, value)
    eps = [SimpleNamespace(series=SimpleNamespace(name='Test Show', path='/srv/tv/Test_Show'))]
    await xbmc.Notifier()._do_notify(eps)

    methods = [r['method'] for r in fake_xbmc.received]
    assert methods[:2] == ['JSONRPC.Version', 'JSONRPC.SetConfiguration']
    scans = [r['params'] for r in fake_xbmc.received if r['method'] == 'VideoLibrary.scan']
    # Local path is remapped to the XBMC path.
    assert scans == [['/media/tv/Test_Show/']]
    notes = [r['params'] for r in fake_xbmc.received if r['method'] == 'GUI.ShowNotification']
    assert notes == [['New TV Episodes', 'New episode for Test Show available.']]
