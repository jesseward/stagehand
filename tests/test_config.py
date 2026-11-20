
"""
Characterization tests for the config file format.  Users' existing config
files must continue to load unchanged, so these pin down the on-disk format.
"""
import pathlib
import textwrap

from stagehand.config import config
from stagehand.toolbox.config import get_default, get_description


def test_defaults(clean_config):
    assert config.web.port == 8088
    assert get_default(config.web.port) == 8088
    assert config.naming.code_style == 's{season:02}e{episode:02}'
    assert config.retrievers.parallel == 1
    assert 'Maximum number of episodes' in get_description(config.retrievers.parallel)


def test_plugin_configs_attached(clean_config):
    # Plugins attach their own config groups at import time.
    assert config.searchers.easynews.retries == 5
    assert config.notifiers.email.port == 587
    assert config.notifiers.xbmc.tcp_port == 9090


def test_save_and_load_roundtrip(clean_config, tmp_path):
    path = str(tmp_path / 'config')
    config.web.port = 9999
    config.misc.tvdir = '/srv/tv'
    config.searchers.enabled.append('easynews')
    config.series.append(config.series(id='tvmaze:100', path='Test_Show', quality='SD'))
    config.save(path, force=True)

    text = pathlib.Path(path).read_text()
    assert 'web.port = 9999' in text
    assert 'misc.tvdir = /srv/tv' in text
    assert 'searchers.enabled[+] = easynews' in text
    assert 'series[+].id = tvmaze:100' in text

    # Reset values and reload from disk.
    config.web.port = 1
    config.misc.tvdir = ''
    config.searchers.enabled.pop()
    config.series.pop()
    config.load(path)

    assert config.web.port == 9999
    assert config.misc.tvdir == '/srv/tv'
    assert list(config.searchers.enabled) == ['easynews']
    assert len(config.series) == 1
    assert config.series[0].id == 'tvmaze:100'
    assert config.series[0].quality == 'SD'
    assert config.series[0].provider == 'thetvdb'


def test_load_hand_written_file(clean_config, tmp_path):
    path = tmp_path / 'config'
    path.write_text(textwrap.dedent('''\
        searchers.enabled[+] = easynews
        searchers.easynews.username = alice
        searchers.easynews.password = s3cret
        series[+].id = thetvdb:200
        series[ ].paused = True
        series[+].id = tvmaze:100
    '''))
    config.load(str(path))
    assert config.searchers.easynews.username == 'alice'
    assert config.searchers.easynews.password == 's3cret'
    assert [s.id for s in config.series] == ['thetvdb:200', 'tvmaze:100']
    assert config.series[0].paused == True  # noqa: E712 (VarProxy wraps bools as ints)
    assert config.series[1].paused == False  # noqa: E712


def test_password_is_scrambled_on_disk(clean_config, tmp_path):
    path = str(tmp_path / 'config')
    config.web.password = 'hunter2'
    config.save(path, force=True)
    text = pathlib.Path(path).read_text()
    assert 'hunter2' not in text
    assert 'web.password = {scrambled}' in text

    config.web.password = ''
    config.load(path)
    assert config.web.password == 'hunter2'


def test_monitor_called_on_change(clean_config):
    changes = []

    def cb(name, old, new):
        changes.append((name, old, new))

    config.web.add_monitor(cb)
    try:
        config.web.port = 1234
    finally:
        config.web.remove_monitor(cb)
    assert ('web.port', 8088, 1234) in changes


def test_invalid_type_rejected(clean_config):
    import pytest
    with pytest.raises((ValueError, TypeError)):
        config.web.port = 'not a number'
