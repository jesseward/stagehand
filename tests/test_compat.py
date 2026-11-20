"""
Compatibility with data written by older versions of Stagehand.
"""
import pickle
from datetime import datetime, timedelta

from stagehand.searchers.base import SearchResult
from stagehand.toolbox import dateutils, db


def legacy_pickle(obj):
    """
    Pickles obj as if its dateutils classes lived in kaa.base.dateutils, as
    they did in Stagehand versions that used kaa.
    """
    data = pickle.dumps(obj, 2)
    return data.replace(b'stagehand.toolbox.dateutils', b'kaa.base.dateutils')


def test_unpickle_kaa_timezones():
    dt = datetime(2014, 3, 1, 20, 0, tzinfo=dateutils.TZAny('-0500'))
    data = legacy_pickle(dt)
    assert b'kaa.base.dateutils' in data
    loaded = db.dbunpickle(data, {'kaa.base.dateutils': 'stagehand.toolbox.dateutils'})
    assert loaded == dt
    assert loaded.utcoffset() == timedelta(hours=-5)
    assert isinstance(loaded.tzinfo, dateutils.TZAny)

    for tz in (dateutils.TZUTC(), dateutils.TZLocal()):
        loaded = db.dbunpickle(legacy_pickle(datetime(2014, 3, 1, tzinfo=tz)),
                               {'kaa.base.dateutils': 'stagehand.toolbox.dateutils'})
        assert type(loaded.tzinfo) is type(tz)
        assert loaded.utcoffset() is not None


def test_unpickle_search_result():
    """SearchResults are pickled into the episode table."""
    from stagehand.searchers.easynews import Searcher
    result = SearchResult(Searcher())
    result.filename = 'Show.s01e01.mkv'
    result.size = 123
    result.date = datetime(2014, 3, 1, tzinfo=dateutils.TZAny('+0100'))
    data = legacy_pickle(result)
    loaded = db.dbunpickle(data, {'kaa.base.dateutils': 'stagehand.toolbox.dateutils'})
    assert loaded.filename == 'Show.s01e01.mkv'
    assert loaded.searcher == 'easynews'
    assert loaded.date.utcoffset() == timedelta(hours=1)


def test_tzany_offsets():
    assert dateutils.TZAny('+0530').utcoffset(None) == timedelta(hours=5, minutes=30)
    assert dateutils.TZAny(-3600).utcoffset(None) == timedelta(hours=-1)
    assert dateutils.TZAny().tzname(None) == '+0000'
