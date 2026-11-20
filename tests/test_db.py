"""
Characterization tests for the object database (stagehand.toolbox.db).

The on-disk format, including pickled attributes, must remain readable by
newer versions so users don't lose their libraries.
"""
import pytest

from stagehand.toolbox import db


class Thing:
    """A picklable object stored in a simple attribute."""
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Thing) and other.value == self.value


@pytest.fixture
def database(tmp_path):
    d = db.Database(str(tmp_path / 'test.db'))
    d.register_inverted_index('keywords', min=2, max=40)
    d.register_object_type_attrs('show',
        name=(str, db.ATTR_SEARCHABLE | db.ATTR_INVERTED_INDEX, 'keywords'),
        year=(int, db.ATTR_SEARCHABLE | db.ATTR_INDEXED),
        extra=(dict, db.ATTR_SIMPLE),
        blob=(db.RAW_TYPE, db.ATTR_SIMPLE),
    )
    d.register_object_type_attrs('ep',
        title=(str, db.ATTR_SEARCHABLE),
        number=(int, db.ATTR_SEARCHABLE),
        obj=(Thing, db.ATTR_SIMPLE),
    )
    yield d
    d.close()


def test_add_and_get(database):
    row = database.add('show', name='The Office', year=2005, extra={'a': [1, 2]}, blob=b'\x00\x01')
    assert row['name'] == 'The Office'
    fetched = database.get(row)
    assert fetched['year'] == 2005
    assert fetched['extra'] == {'a': [1, 2]}
    assert fetched['blob'] == b'\x00\x01'
    assert fetched['type'] == 'show'


def test_query_by_attr_and_qexpr(database):
    for year in (2001, 2005, 2010):
        database.add('show', name='Show %d' % year, year=year)
    assert len(database.query(type='show')) == 3
    assert [r['year'] for r in database.query(type='show', year=2005)] == [2005]
    newer = database.query(type='show', year=db.QExpr('>=', 2005))
    assert sorted(r['year'] for r in newer) == [2005, 2010]
    assert database.query_one(type='show', year=1900) is None


def test_parent_child_and_delete_by_query(database):
    show = database.add('show', name='Parent', year=2000)
    for n in range(3):
        database.add('ep', parent=show, title='Ep %d' % n, number=n, obj=Thing(n))
    eps = database.query(type='ep', parent=show)
    assert sorted(e['number'] for e in eps) == [0, 1, 2]
    assert database.get(eps[0])['obj'] == Thing(eps[0]['number'])
    database.delete_by_query(parent=show)
    assert database.query(type='ep', parent=show) == []


def test_update(database):
    row = database.add('show', name='Old', year=2000)
    database.update(row, name='New', extra={'x': 1})
    row = database.get(row)
    assert row['name'] == 'New'
    assert row['extra'] == {'x': 1}


def test_inverted_index_keyword_search(database):
    database.add('show', name='Breaking Bad', year=2008)
    database.add('show', name='Better Call Saul', year=2015)
    results = database.query(keywords='saul')
    assert [r['name'] for r in results] == ['Better Call Saul']


def test_metadata(database):
    database.set_metadata('thing::servertime', 1234)
    assert int(database.get_metadata('thing::servertime')) == 1234
    assert database.get_metadata('missing', 'default') == 'default'


async def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / 'persist.db')
    d = db.Database(path)
    d.register_object_type_attrs('item', name=(str, db.ATTR_SEARCHABLE), payload=(list, db.ATTR_SIMPLE))
    d.add('item', name='a', payload=[1, 'two', {'three': 3}])
    d.close()

    d = db.Database(path)
    # Types are persisted in the database; no need to re-register.
    rows = d.query(type='item')
    assert len(rows) == 1
    assert rows[0]['payload'] == [1, 'two', {'three': 3}]
    d.close()


def test_delete(database):
    row = database.add('show', name='Gone', year=1)
    database.delete(row)
    assert database.query(type='show') == []


def test_update_searchable_attr_to_none(database):
    """Setting a column (searchable) attribute to None stores NULL."""
    obj = database.add('show', name='Show', year=2001)
    database.update(obj, year=None)
    obj = database.get(obj)
    assert obj['year'] is None
    assert database.query(type='show', year=2001) == []
    database.update(obj, year=2002)
    assert database.get(obj)['year'] == 2002


def test_update_simple_attr_to_none(database):
    obj = database.add('show', name='Show', extra={'a': 1})
    database.update(obj, extra=None)
    assert database.get(obj)['extra'] is None
