from datetime import UTC, datetime, timedelta

import pytest

from stagehand import utils
from stagehand.config import config
from stagehand.toolbox import dateutils
from stagehand.web.settings import rename_example


@pytest.fixture
def separator():
    old = config.naming.separator
    config.naming.separator = '_'
    yield
    config.naming.separator = old


def test_fixsep_replaces_problem_characters(separator):
    assert utils.fixsep('What If?: Part 1/2') == 'What_If_Part_1_2'
    assert utils.fixsep('a   b') == 'a_b'


def test_fixsep_non_path_keeps_slashes(separator):
    assert utils.fixsep('a/b c', path=False) == 'a/b_c'


def test_fixquotes():
    assert utils.fixquotes('“Hi” ‘there’… —') == '"Hi" \'there\'... --'
    assert utils.fixquotes(None) is None
    assert utils.fixquotes('') == ''


def test_remove_stop_words():
    assert utils.remove_stop_words('The Office of a Man') == 'Office of Man'


def test_name_to_url_segment():
    assert utils.name_to_url_segment('The Office (US)') == 'office_us'
    assert utils.name_to_url_segment('Law & Order') == 'law_and_order'


class FakeSeason:
    def __init__(self, number):
        self.number = number


class FakeEpisode:
    STATUS_NONE, STATUS_NEED, STATUS_HAVE, STATUS_NEED_FORCED, STATUS_IGNORE = range(5)

    def __init__(self, status, aired=True, obsolete=False, season=1):
        self.status = status
        self.aired = aired
        self.obsolete = obsolete
        self.season = FakeSeason(season)


@pytest.mark.parametrize('kwargs, expected', [
    (dict(status=FakeEpisode.STATUS_NEED_FORCED), 'need-forced'),
    (dict(status=FakeEpisode.STATUS_HAVE), 'have'),
    (dict(status=FakeEpisode.STATUS_IGNORE), 'ignore'),
    (dict(status=FakeEpisode.STATUS_NONE, obsolete=True), 'ignore'),
    (dict(status=FakeEpisode.STATUS_NONE, season=0), 'ignore'),
    (dict(status=FakeEpisode.STATUS_NONE, aired=True), 'need'),
    (dict(status=FakeEpisode.STATUS_NEED, aired=False), 'future'),
])
def test_episode_status_icon_info(kwargs, expected):
    icon, title = utils.episode_status_icon_info(FakeEpisode(**kwargs))
    assert icon == expected
    assert title


def test_rename_example_ok():
    r = rename_example('/tv', '.', 's{season:02}', 's{season:02}e{episode:02}', '{show}-{code}-{title}')
    assert r == {'example': '/tv/s02/Dead.Like.Me-s02e04-The.Shallow.End'}


def test_rename_example_bad_identifier():
    r = rename_example('/tv', '.', 's{season}', '{bogus}', '{show}')
    assert 'bogus' in r['error']


def test_from_rfc822_with_offset():
    dt = dateutils.from_rfc822('Mon, 20 Nov 1995 19:12:08 -0500')
    assert dt.utcoffset() == timedelta(hours=-5)
    assert dt.astimezone(UTC).replace(tzinfo=None) == datetime(1995, 11, 21, 0, 12, 8)


def test_from_rfc822_without_timezone_is_utc():
    dt = dateutils.from_rfc822('Mon, 20 Nov 1995 19:12:08')
    assert dt.utcoffset() == timedelta(0)


def test_from_rfc822_garbage():
    assert dateutils.from_rfc822('not a date') is None


