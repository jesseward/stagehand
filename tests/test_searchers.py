"""
Tests for search result parsing, matching and ranking.
"""
import provider_data
import pytest

from stagehand import searchers
from stagehand.config import config
from stagehand.searchers.base import SearcherBase, SearchResult
from stagehand.searchers.easynews import Searcher as EasynewsSearcher

RSS = '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Easynews</title>
{items}
</channel>
</rss>'''

ITEM = '''<item>
  <title>{subject}</title>
  <pubDate>{date}</pubDate>
  <enclosure url="https://members.easynews.com/dl/{hash}/{filename}" length="{size}" type="video/x-matroska" />
</item>'''


def rss(*items):
    return RSS.format(items='\n'.join(ITEM.format(**item) for item in items))


def item(filename, size, hash='0' * 32, subject='subject', date='Sun, 12 Jan 2020 23:00:00 -0500'):
    return dict(filename=filename, size=size, hash=hash, subject=subject, date=date)


@pytest.fixture
def searcher():
    return SearcherBase()


def test_parse_hsize(searcher):
    assert searcher._parse_hsize(123) == 123
    assert searcher._parse_hsize('1.5 GiB') == int(1.5 * 1024 ** 3)
    assert searcher._parse_hsize('700 MB') == 700 * 1000 * 1000
    assert searcher._parse_hsize('1,024') == 1024
    assert searcher._parse_hsize('') == 0


@pytest.mark.parametrize('title, apostrophe, expected', [
    ('The Office (US)', SearcherBase.CLEAN_APOSTROPHE_LEAVE, 'Office'),
    ("Grey's Anatomy", SearcherBase.CLEAN_APOSTROPHE_REMOVE, 'Greys Anatomy'),
    ("Grey's Anatomy", SearcherBase.CLEAN_APOSTROPHE_REGEXP, "(Greys|Grey's) Anatomy"),
    ('Law & Order: SVU', SearcherBase.CLEAN_APOSTROPHE_LEAVE, 'Law Order SVU'),
])
def test_clean_title(searcher, title, apostrophe, expected):
    assert searcher.clean_title(title, apostrophe=apostrophe) == expected


@pytest.fixture
async def show(tvdb, fake_http):
    provider_data.install(fake_http)
    config.series.append(config.series(id='tvmaze:100', path='Test_Show', provider='tvmaze', quality='HD'))
    await tvdb.add_series_by_id('tvmaze:100', fast=False)
    return tvdb.get_series_by_id('tvmaze:100')


def test_is_name_for_episode(searcher, show):
    ep = show.get_episode_by_code('s01e02')
    assert searcher._is_name_for_episode('Test.Show.S01E02.720p.mkv', ep)
    assert searcher._is_name_for_episode('test_show_1x02.avi', ep)
    assert searcher._is_name_for_episode('Test.Show.2020.01.12.mkv', ep)
    assert not searcher._is_name_for_episode('Test.Show.S01E03.720p.mkv', ep)
    assert not searcher._is_name_for_episode('Other.Show.S01E02.mkv', ep)


@pytest.fixture
def easynews_creds(clean_config):
    config.searchers.easynews.username = 'user'
    config.searchers.easynews.password = 'pass'
    config.searchers.enabled.append('easynews')


async def test_easynews_search_ranks_results(show, fake_http, easynews_creds):
    mb = 1024 * 1024
    fake_http.add(r'members\.easynews\.com/global5', (200, rss(
        item('Test.Show.S01E02.720p.HDTV.x264-GRP.mkv', 700 * mb, hash='a' * 32),
        item('Test.Show.S01E02.1080p.WEB-DL.DD5.1.H.264.mkv', 1400 * mb, hash='b' * 32),
        item('Test.Show.S01E02.wmv', 900 * mb, hash='c' * 32),
        item('Test.Show.S01E02.480p.mkv', 10 * mb, hash='d' * 32),
        item('Test.Show.S01E03.1080p.x264.mkv', 1000 * mb, hash='e' * 32),
        item('random.bin', 1000 * mb, hash='f' * 32, subject='Test Show s01e03 [1/1]'),
    )))
    ep2 = show.get_episode_by_code('s01e02')
    ep3 = show.get_episode_by_code('s01e03')
    results = await searchers.search(show, [ep2, ep3])

    # wmv is disqualified, the tiny file is below the minimum size.
    names = [r.filename for r in results[ep2]]
    assert names == ['Test.Show.S01E02.1080p.WEB-DL.DD5.1.H.264.mkv', 'Test.Show.S01E02.720p.HDTV.x264-GRP.mkv']
    best = results[ep2][0]
    assert best.searcher == 'easynews'
    assert best.quality == 'HD'
    assert best.size == 1400 * mb
    assert best.date.utcoffset().total_seconds() == -5 * 3600
    assert best.url.startswith('https://members.easynews.com/dl/bbbb')

    # Episode 3 matched by filename first, then subject.
    assert [r.filename for r in results[ep3]] == ['Test.Show.S01E03.1080p.x264.mkv', 'random.bin']

    # The request used the configured credentials and episode codes.
    url, kwargs = [r for r in fake_http.requests if 'easynews' in r[0]][0]
    assert kwargs['auth'] == ('user', 'pass')
    assert 's01e02' in url and 's01e03' in url


async def test_easynews_retriever_data_and_equality(easynews_creds):
    s = EasynewsSearcher()
    a = SearchResult(s, filename='x.mkv', url='https://h/dl/' + 'a' * 32 + '/x.mkv', size=1)
    b = SearchResult(s, filename='x.mkv', url='https://other/dl/' + 'a' * 32 + '/y.mkv', size=1)
    c = SearchResult(s, filename='x.mkv', url='https://h/dl/' + 'b' * 32 + '/x.mkv', size=1)
    assert a == b
    assert a != c
    data = await a.get_retriever_data()
    assert data['url'] == a.url
    assert data['username'] == 'user'


async def test_search_result_pickle_excludes_cached_data():
    import pickle
    s = EasynewsSearcher()
    r = SearchResult(s, filename='x.mkv', url='u', size=1)
    r._rdata = {'secret': 1}
    r2 = pickle.loads(pickle.dumps(r))
    assert r2.filename == 'x.mkv'
    assert r2._rdata is NotImplemented


async def test_search_with_no_enabled_searchers_returns_empty(show, clean_config):
    ep = show.get_episode_by_code('s01e02')
    assert await searchers.search(show, [ep]) == {}
