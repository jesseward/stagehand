"""
HTML page handlers.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from aiohttp import web

from ..config import config
from ..toolbox.config import get_description
from ..utils import episode_status_icon_info
from .context import MANAGER, json_response, render
from .settings import rename_example

routes = web.RouteTableDef()


def page(path: str, template: str) -> None:
    """
    Registers a handler for a page that needs no context beyond the defaults.
    """
    async def handler(request: web.Request) -> web.Response:
        return render(request, template)
    handler.__name__ = template.replace('/', '_').removesuffix('.tmpl')
    routes.get(path)(handler)


page('/', 'home.tmpl')
page('/tv/', 'tv/library.tmpl')
page('/tv/add', 'tv/add.tmpl')
page('/tv/upcoming', 'tv/upcoming.tmpl')
page('/settings/searchers', 'settings/searchers.tmpl')
page('/settings/retrievers', 'settings/retrievers.tmpl')
page('/settings/notifiers', 'settings/notifiers.tmpl')
page('/log/', 'log/application.tmpl')
page('/log/web', 'log/web.tmpl')


@routes.get('/tv/{id}')
async def tv_show(request: web.Request) -> web.Response:
    tvdb = request.app[MANAGER].tvdb
    series = tvdb.get_series_by_id(request.match_info['id'])
    if not series:
        raise web.HTTPNotFound(text='Invalid show.')
    return render(request, 'tv/show.tmpl', {'series': series, 'providers': tvdb.providers.values()})


@routes.get('/downloads/')
async def downloads(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    weeks_arg = request.query.get('weeks', '')
    weeks = int(weeks_arg) if weeks_arg.isdigit() else 1
    status = request.query.get('status') or 'have'

    # Construct a list of episodes, sorted by air date, that are either needed
    # or match the criteria for inclusion (based on status and weeks).  Episodes
    # in the download queue aren't included as they're displayed separately.
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # The most recent past Sunday (or today, if today is Sunday)
    sunday = today if today.weekday() == 6 else today - timedelta(days=today.weekday() + 1)
    episodes = []
    for s in manager.tvdb.series:
        if s.cfg.paused:
            # Don't show episodes for paused series, even if they are needed.
            continue
        for ep in s.episodes:
            if ep.status != ep.STATUS_NEED_FORCED and (not ep.aired or manager.is_episode_queued_for_retrieval(ep)):
                continue
            icon, title = episode_status_icon_info(ep)
            week: int | None
            if ep.airdate:
                # week 0 is anything on or after sunday
                week = (max(0, (sunday - ep.airdate).days) + 6) // 7
                if (icon in ('ignore', 'have') and week >= weeks) or (icon == 'ignore' and status == 'have'):
                    continue
            else:
                # Episode is STATUS_NEED_FORCED without an airdate.
                week = None
            episodes.append((ep, icon, title, week))
    # For episodes without an airdate, just use 1900-01-01 for sorting
    # purposes, so they sorted last.
    episodes.sort(key=lambda i: (i[0].airdatetime or datetime(1900, 1, 1), i[0].name), reverse=True)
    return render(request, 'downloads/downloads.tmpl', {'weeks': weeks, 'status': status, 'episodes': episodes})


@routes.get('/settings/')
async def settings_general(request: web.Request) -> web.Response:
    return render(request, 'settings/general.tmpl', {
        'desc': lambda x: get_description(x).replace('\n\n', '<br/><br/>'),
        'rename_example': rename_example(config.misc.tvdir, config.naming.separator, config.naming.season_dir_format,
                                         config.naming.code_style, config.naming.episode_format),
    })


@routes.get('/settings/rename_example')
async def settings_rename_example(request: web.Request) -> web.Response:
    q = request.query
    return json_response(rename_example(config.misc.tvdir, q.get('separator', ''), q.get('season_dir_format', ''),
                                        q.get('code_style', ''), q.get('episode_format', '')))
