"""
Shared fixtures for the Stagehand test suite.

Tests are written as native ``async def`` functions; pytest-asyncio runs each
in its own event loop (``asyncio_mode = "auto"`` in pyproject.toml).
"""
import json
import os
import re

import pytest

import stagehand.logger  # noqa: F401  (registers the DEBUG2 log level)
from stagehand.config import config


@pytest.fixture(autouse=True)
async def _close_http_session():
    """
    Closes the shared aiohttp session that download() creates, since each
    test runs in its own event loop.
    """
    yield
    from stagehand.toolbox.net import close_session
    await close_session()


def make_async(fn):
    """
    Wraps a plain function so that it can be awaited.
    """
    async def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


class FakeHTTP:
    """
    A fake for ``stagehand.toolbox.net.download``.

    Routes are (regexp, response) pairs matched against the requested URL,
    where response is either a (status, body) tuple or a callable taking the
    URL and kwargs and returning one.  Bodies that are dicts or lists are
    JSON-encoded.
    """
    def __init__(self):
        self.routes = []
        self.requests = []

    def add(self, pattern, response):
        self.routes.append((re.compile(pattern), response))

    def __call__(self, url, target=None, *args, **kwargs):
        self.requests.append((url, kwargs))
        for pattern, response in self.routes:
            if pattern.search(url):
                if callable(response):
                    response = response(url, kwargs)
                status, body = response
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode('utf8')
                elif isinstance(body, str):
                    body = body.encode('utf8')
                return status, body
        return 404, None


@pytest.fixture
def fake_http(monkeypatch):
    """
    Replaces the download() function in every module that imported it.
    """
    http = FakeHTTP()
    fake = make_async(http)
    import stagehand.providers.thetvdb
    import stagehand.providers.tvmaze
    import stagehand.searchers.easynews
    for mod in (stagehand.providers.thetvdb, stagehand.providers.tvmaze, stagehand.searchers.easynews):
        monkeypatch.setattr(mod, 'download', fake)
    return http


@pytest.fixture
def clean_config(tmp_path):
    """
    Ensures the global config object is reset to defaults after the test.
    """
    snapshot = tmp_path / 'config-snapshot'
    while len(config.series):
        config.series.pop()
    config.save(str(snapshot), force=True)
    filename = config.filename
    yield config
    while len(config.series):
        config.series.pop()
    config.load(str(snapshot))
    config.filename = filename


@pytest.fixture
def tvdb(tmp_path, clean_config):
    from stagehand.tvdb import TVDB
    db = TVDB(str(tmp_path / 'tv.db'))
    yield db
    db.lazy_commit = 0
    db.close()


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), 'fixtures')


@pytest.fixture
def paths(tmp_path):
    from stagehand.main import find_data_path
    from stagehand.manager import Paths
    return Paths(
        data=find_data_path(),
        webcache=str(tmp_path / 'cache'),
        logs=str(tmp_path / 'logs'),
        config=str(tmp_path / 'config'),
        db=str(tmp_path / 'tv.db'),
    )


@pytest.fixture
async def manager(paths, clean_config, fake_http):
    """
    A Manager (not started) whose metadata providers are served by
    provider_data.
    """
    import provider_data

    from stagehand.manager import Manager
    provider_data.install(fake_http)
    mgr = Manager(paths)
    yield mgr
    config.autosave = False
    mgr.tvdb.lazy_commit = 0
    if mgr.tvdb._db is not None:  # may have been closed by the test
        mgr.close()
