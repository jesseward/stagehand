"""
JSON API handlers used by the web client.
"""
from __future__ import annotations

import html
import logging
import mimetypes
import os
import time
from typing import Any

from aiohttp import web

from ..tvdb import Episode, Series
from ..utils import episode_status_icon_info
from .context import MANAGER, SHUTDOWN, json_response
from .jobs import AsyncWebJob, webcoroutine

log = logging.getLogger('stagehand.web.api')

routes = web.RouteTableDef()


def get_series(request: web.Request, id: str) -> Series:
    series = request.app[MANAGER].tvdb.get_series_by_id(id)
    if not series:
        raise web.HTTPNotFound(text='Invalid show.')
    return series


# Note: the literal routes below must be registered before /api/shows/{id}
# routes with the same shape.

@routes.get('/api/shows/search')
@webcoroutine(interval=500)
async def show_search(request: web.Request, job: AsyncWebJob) -> dict[str, Any]:
    t0 = time.perf_counter()
    results = await request.app[MANAGER].tvdb.search(request.query.get('name', ''))
    job.notify('alert', title='Search results', text=f'Search took {time.perf_counter() - t0:.3f}s')
    return {'results': [{
        'id': r.id,
        'name': r.name,
        'overview': r.overview,
        'year': r.year,
        'imdb': r.imdb,
        'provider': r.provider.NAME,
        'started': r.started,
    } for r in results]}


@routes.get('/api/shows/check')
@webcoroutine(interval=500)
async def show_check(request: web.Request, job: AsyncWebJob) -> dict[str, int]:
    id = request.query.get('id')
    only = [get_series(request, id)] if id else None
    need, found = await request.app[MANAGER].check_new_episodes(only=only)
    return {'need': sum(len(eps) for eps in need.values()), 'found': len(found)}


@routes.put('/api/shows/{id}')
@webcoroutine()
async def show_add(request: web.Request, job: AsyncWebJob, id: str) -> dict[str, Any]:
    job.notify('alert', title='Adding series', text='Retrieving episode information for this series ...')
    try:
        series = await request.app[MANAGER].add_series(id)
        if not series:
            raise LookupError(f'No series found for {id}')
    except Exception as e:
        log.exception('failed to add series')
        job.notify_after('alert', title='Failed to add series', text=str(e), timeout=2)
    else:
        job.notify_after('alert', title='Series added', text=f'Added series {series.name} to database.', timeout=2)
    return {}


@routes.delete('/api/shows/{id}')
@webcoroutine()
async def show_delete(request: web.Request, job: AsyncWebJob, id: str) -> dict[str, Any]:
    name = get_series(request, id).name
    request.app[MANAGER].delete_series(id)
    # Notify the session about the removal, but do it via a timer so that
    # the notification happens on the next page load rather than in response
    # to the API call.
    job.notify_after('alert', title='Series Deleted', html=f'Series <b>{html.escape(name)}</b> was removed from the database')
    return {}


@routes.get('/api/shows/{id}/banner')
async def show_banner(request: web.Request) -> web.Response:
    series = get_series(request, request.match_info['id'])
    if not series.banner_data:
        raise web.HTTPNotFound(text='Invalid show, or no banner for this show.')
    mimetype, _ = mimetypes.guess_type(series.banner or '')
    return web.Response(body=series.banner_data, content_type=mimetype or 'application/octet-stream',
                        headers={'Cache-Control': f'max-age={3600 * 24 * 7}'})


@routes.post('/api/shows/{id}/provider')
@webcoroutine()
async def show_provider(request: web.Request, job: AsyncWebJob, id: str) -> None:
    series = get_series(request, id)
    provider = (await request.post()).get('provider')
    if provider:
        try:
            await series.change_provider(str(provider))
        except Exception as e:
            job.notify_after('alert', title='Series Provider', text=f'Failed to change provider: {e}')
            raise web.HTTPNotFound(text='Invalid provider.') from e
        series.cfg.provider = str(provider)


@routes.post('/api/shows/{id}/refresh')
@webcoroutine()
async def show_refresh(request: web.Request, job: AsyncWebJob, id: str) -> None:
    await get_series(request, id).refresh()


@routes.post('/api/shows/{id}/settings')
async def show_settings(request: web.Request) -> web.Response:
    series = get_series(request, request.match_info['id'])
    settings = await request.post()

    def get(name: str) -> str:
        return str(settings.get(name, ''))

    series.cfg.quality = get('quality')
    series.cfg.path = get('path')
    series.cfg.search_string = get('search_string')
    series.cfg.language = get('language')
    series.cfg.paused = get('paused') == 'true'
    series.cfg.flat = get('flat') == 'true'
    series.cfg.identifier = get('identifier')
    # TODO: if pausing a series that has queued episodes, remove them and
    # notify user.
    return json_response({})


@routes.get('/api/shows/{id}/overview')
async def show_overview(request: web.Request) -> web.Response:
    series = get_series(request, request.match_info['id'])
    return json_response({'overview': series.overview})


@routes.get('/api/shows/{id}/{code}/overview')
async def show_episode_overview(request: web.Request) -> web.Response:
    series = get_series(request, request.match_info['id'])
    ep = series.get_episode_by_code(request.match_info['code'])
    return json_response({'overview': ep.overview if ep else 'Episode not found'})


STATUS_ACTIONS = {
    'need': Episode.STATUS_NEED,
    'ignore': Episode.STATUS_IGNORE,
    'delete': Episode.STATUS_IGNORE,
}


@routes.post('/api/shows/{id}/episodes/{epcode}/status')
@webcoroutine()
async def show_episodes_status(request: web.Request, job: AsyncWebJob, id: str, epcode: str) -> dict[str, Any]:
    manager = request.app[MANAGER]
    series = get_series(request, id)
    eps = [series.get_episode_by_code(code) for code in epcode.split(',')]
    if None in eps:
        raise web.HTTPNotFound(text='Unknown episode for this show.')

    action = request.query.get('value', '')
    try:
        status_val = STATUS_ACTIONS[action]
    except KeyError:
        raise web.HTTPNotFound(text='Invalid status code.') from None

    statuses = {}
    do_check_new_episodes = False
    for ep in eps:
        if ep.status == Episode.STATUS_HAVE and action != 'delete':
            # Asked to ignore or retrieve an episode we already have.  Do nothing.
            pass
        elif status_val == Episode.STATUS_NEED and ep.season.number == 0:
            # Special case: user scheduled a special episode for download.  Normally
            # a special episode set as STATUS_NEED is ignored.  So we set to NEED_FORCED
            # instead.
            ep.status = Episode.STATUS_NEED_FORCED
        else:
            ep.status = status_val
            # Clear any stored search result
            ep.search_result = None

        if ep.status == Episode.STATUS_IGNORE:
            manager.cancel_episode_retrieval(ep)
        elif (ep.status == Episode.STATUS_NEED and ep.aired) or ep.status == Episode.STATUS_NEED_FORCED:
            # Episode either forced or marked as needed and is aired.  Ask the manager to do a search.
            do_check_new_episodes = True
        if action == 'delete' and ep.filename and os.path.isfile(ep.path):
            try:
                os.unlink(ep.path)
            except OSError as e:
                job.notify('alert', title='Delete Episode', text=f'Failed to delete {ep.path}: {e}', type='error')
            else:
                job.notify('alert', title='Delete Episode', text=f'{ep.filename} deleted')
                ep.filename = None

        statuses[ep.code] = episode_status_icon_info(ep)

    if do_check_new_episodes:
        # Runs in the background (singleton task).
        manager.check_new_episodes()
    return {'statuses': statuses}


@routes.get('/api/shutdown')
async def shutdown(request: web.Request) -> web.Response:
    log.warning('shutdown requested via web API')
    request.app[SHUTDOWN].set()
    return json_response({})


@routes.get('/api/pid')
async def pid(request: web.Request) -> web.Response:
    return json_response({'pid': os.getpid()})
