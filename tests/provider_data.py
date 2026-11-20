"""
Canned provider API responses describing a small fictional series, "Test
Show", as seen by both TheTVDB (v4 API) and TVmaze.

The two providers mostly agree, but TheTVDB has an extra special (season 0)
and names one episode slightly differently, which exercises the fuzzy
episode matching logic.
"""

TVMAZE_ID = '100'
THETVDB_ID = '200'

TVMAZE_SHOW = {
    'id': 100,
    'name': 'Test Show',
    'runtime': 30,
    'schedule': {'time': '20:00', 'days': ['Sunday']},
    'image': None,
    'status': 'Ended',
    'genres': ['Comedy', 'Drama'],
    'premiered': '2020-01-05',
    'externals': {'imdb': 'tt0000123'},
    'summary': '<p>A show about tests.</p>',
}

TVMAZE_EPISODES = [
    {'id': 1001, 'name': 'Pilot', 'season': 1, 'number': 1, 'airdate': '2020-01-05', 'summary': 'First.'},
    {'id': 1002, 'name': 'The Second One', 'season': 1, 'number': 2, 'airdate': '2020-01-12', 'summary': 'Second.'},
    {'id': 1003, 'name': 'Finale', 'season': 1, 'number': 3, 'airdate': '2020-01-19', 'summary': 'Third.'},
]

TVMAZE_SEARCH = [
    {'score': 10, 'show': TVMAZE_SHOW},
]

THETVDB_LOGIN = {'status': 'success', 'data': {'token': 'TOKEN'}}

THETVDB_SEARCH = {
    'data': [
        {
            'id': 'series-200',
            'tvdb_id': '200',
            'name': 'Test Show',
            'first_air_time': '2020-01-05',
            'overview': 'A show about tests.',
            'image_url': None,
        },
        {
            'id': 'series-999',
            'tvdb_id': '999',
            'name': 'Unrelated Show',
            'first_air_time': '1999-01-01',
            'overview': 'Not it.',
        },
    ]
}

THETVDB_SERIES = {
    'data': {
        'id': 200,
        'name': 'Test Show',
        'averageRuntime': 30,
        'airsTime': '20:00',
        'image': None,
        'status': {'name': 'Ended'},
        'genres': [{'name': 'Comedy'}, {'name': 'Drama'}],
        'remoteIds': [{'sourceName': 'IMDB', 'id': 'tt0000123'}],
        'firstAired': '2020-01-05',
        'overview': 'A show about tests.',
    }
}

THETVDB_EPISODES = {
    'data': {
        'episodes': [
            {'id': 2000, 'name': 'Behind the Scenes', 'seasonNumber': 0, 'number': 1, 'aired': '2020-01-20',
             'overview': 'Special.'},
            {'id': 2001, 'name': 'Pilot', 'seasonNumber': 1, 'number': 1, 'aired': '2020-01-05', 'overview': None},
            {'id': 2002, 'name': 'Second One', 'seasonNumber': 1, 'number': 2, 'aired': '2020-01-12',
             'overview': 'Second (tvdb).'},
            {'id': 2003, 'name': 'Finale', 'seasonNumber': 1, 'number': 3, 'aired': '2020-01-19', 'overview': 'Third.'},
        ]
    },
    'links': {'next': None},
}


def install(http):
    """
    Installs routes for both providers into a FakeHTTP instance.
    """
    http.add(r'api4\.thetvdb\.com/v4/login', (200, THETVDB_LOGIN))
    http.add(r'api4\.thetvdb\.com/v4/search\?query=Test%20Show', (200, THETVDB_SEARCH))
    http.add(r'api4\.thetvdb\.com/v4/series/200/extended', (200, THETVDB_SERIES))
    http.add(r'api4\.thetvdb\.com/v4/series/200/episodes/default\?page=0', (200, THETVDB_EPISODES))
    http.add(r'api\.tvmaze\.com/search/shows\?q=Test%20Show', (200, TVMAZE_SEARCH))
    http.add(r'api\.tvmaze\.com/shows/100/episodes', (200, TVMAZE_EPISODES))
    http.add(r'api\.tvmaze\.com/shows/100$', (200, TVMAZE_SHOW))
