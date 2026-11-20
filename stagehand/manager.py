"""
The Manager coordinates Stagehand's work: periodically checking for new
episodes, searching for them, and retrieving search results.
"""
from __future__ import annotations

import asyncio
import gc
import html
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import notifiers, providers, retrievers, searchers, web
from .config import config
from .logger import getLogger
from .retrievers import RetrieverAbortedHard, RetrieverError, RetrieverSoftError
from .searchers import SearchResult
from .toolbox import FutureProgress, singleton, spawn
from .toolbox.config import get_default
from .tvdb import TVDB, Episode, Series
from .utils import fixsep

log = getLogger('stagehand.manager')

# How often to check providers for updated series metadata.
TVDB_UPDATE_INTERVAL = 4 * 60 * 60


@dataclass
class Paths:
    """
    Locations of Stagehand's data, config, cache, and log files.
    """
    data: str
    config: str
    db: str
    webcache: str
    logs: str

    def create(self) -> None:
        os.makedirs(self.webcache, exist_ok=True)
        os.makedirs(self.logs, exist_ok=True)
        os.makedirs(os.path.dirname(self.config), exist_ok=True)


class Manager:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._retrieve_queue_event = asyncio.Event()
        # A list of episodes to be retrieved.
        # [(Episode, [SearchResult, ...]), (Episode, [SearchResult, ...])]
        self._retrieve_queue: list[tuple[Episode, list[SearchResult]]] = []
        # Like _retrieve_queue, but these are the episodes actively being processed.
        # The first SearchResult is the one being retrieved.
        self._retrieve_queue_active: list[tuple[Episode, list[SearchResult]]] = []
        # Maps Episode objects to Tasks for active downloads.
        self._retrieve_tasks: dict[Episode, asyncio.Task[None]] = {}
        self._retrieve_progress: dict[Episode, FutureProgress] = {}
        self._next_episode_check_timer: asyncio.TimerHandle | None = None
        self._tvdb_update_timer: asyncio.TimerHandle | None = None

        # If the config schema changed (new version of Stagehand installed?)
        # then write out a new config file.
        config.save(paths.config, force=not os.path.exists(paths.config))

        log.info('watching %s for changes', config.filename)
        config.watch()
        config.autosave = True
        config.signals['reloaded'].connect(self._load_config)

        self.tvdb = TVDB(paths.db)


    async def start(self) -> None:
        """
        Starts the Stagehand manager, which starts all plugins and
        schedules tasks for
        """
        await self._load_config()

        spawn(self._check_update_tvdb(), name='tvdb-update')

        self._schedule_next_episode_check(skip_current_hour=False)
        await self.check_new_episodes()

        # Start all plugins in parallel
        await asyncio.gather(searchers.start(self), retrievers.start(self),
                                  notifiers.start(self), providers.start(self))

        # Resume downloading any episodes we aborted by adding to retrieve queue.
        log.info('checking all episodes to see if any need resuming')
        for series in self.tvdb.series:
            for ep in series.episodes:
                if ep.ready and ep.search_result:
                    self._retrieve_queue.append((ep, [ep.search_result]))
            # For a large number of series, this loop (which involves reading
            # all episodes from the database) can take some time.  So yield
            # back to the main loop so we don't starve other tasks.
            await asyncio.sleep(0)

        # Start the retrieve queue processor
        self._retrieve_queue_processor()
        self._retrieve_queue_event.set()
        log.info('stagehand started, waiting for next new episodes check')



    async def _load_config(self, changed: list[str] | None = None) -> None:
        if changed is not None:
            # if changed is given (even if it's an empty list), it means we've
            # been invoked from the config reloaded signal.
            log.info('config file changed; reloading')
            if 'retrievers.parallel' in changed:
                # Might have increased the number of parallel downloads allowed.
                # Wake up retrieve queue manager.
                self._retrieve_queue_event.set()
        await self._load_series_from_config()



    def _schedule_next_episode_check(self, skip_current_hour: bool = True) -> None:
        try:
            hours = sorted(int(h) for h in config.searchers.hours.split(','))
        except ValueError:
            log.warning('invalid searchers.hours config value (%s), using default', config.searchers.hours)
            hours = sorted(int(h) for h in get_default(config.searchers.hours).split(','))

        # Pick a random minute of the hour for the next check.
        randmin = random.randint(0, 59)
        t = datetime.now().replace(second=0, microsecond=0)
        # Which future hours are eligible?
        next_hour = [h for h in hours if h > t.hour or (not skip_current_hour and h == t.hour and randmin > t.minute)]
        if next_hour:
            next = t.replace(hour=next_hour[0], minute=randmin)
        else:
            # No schedulable hour in the future for the current day, so advance to tomorrow.
            tmrw = t + timedelta(days=1)
            next = tmrw.replace(hour=hours[0], minute=randmin)

        log.info('scheduling next episode check for %s', next)
        if self._next_episode_check_timer:
            self._next_episode_check_timer.cancel()
        loop = asyncio.get_running_loop()
        delta = next.timestamp() - time.time()
        self._next_episode_check_timer = loop.call_at(loop.time() + delta, self._scheduled_episode_check)

    def _scheduled_episode_check(self) -> None:
        self._next_episode_check_timer = None
        self.check_new_episodes(reschedule=True)


    @property
    def retrieve_queue(self) -> list[tuple[Episode, list[SearchResult]]]:
        """
        The current retrieve queue, in the form:
        [(Episode, [SearchResult, ...]), (Episode, [SearchResult, ...])]

        The first element of this queue is probably active, but you should
        call :meth:`get_episode_retrieve_task` to be sure.
        """
        return self._retrieve_queue_active + self._retrieve_queue



    async def _check_update_tvdb(self) -> None:
        try:
            servertime = self.tvdb.get_last_updated()
            if servertime and time.time() - float(servertime) > 60*60*24:
                await self.tvdb.sync()
                # FIXME: if count, need to go through all episodes and mark future episodes as STATUS_NEED
        finally:
            self._tvdb_update_timer = asyncio.get_running_loop().call_later(
                TVDB_UPDATE_INTERVAL, lambda: spawn(self._check_update_tvdb(), name='tvdb-update'))


    async def _add_series_to_db(self, id: str, fast: bool = False) -> Series | None:
        log.info('adding new series %s to database', id)
        series = await self.tvdb.add_series_by_id(id, fast=fast)
        if not series:
            log.error('provider did not know about %s', id)
            return None
        log.debug('found series %s (%s) on provider', id, series.name)

        # Initialize status for all old episodes as STATUS_IGNORE.  Note that
        # episodes with no airdate can be future episodes, so we mustn't
        # set those to ignore.
        # XXX: changed weeks
        cutoff = datetime.now() - timedelta(weeks=0)
        for ep in series.episodes:
            if ep.airdate and ep.airdate < cutoff:
                ep.status = ep.STATUS_IGNORE

        return series


    async def add_series(self, id: str) -> Series | None:
        """
        Add new series by id, or return existing series if already added.
        """
        series = self.tvdb.get_series_by_id(id)
        if not series:
            series = await self._add_series_to_db(id, fast=True)
            if not series:
                return None
            if not self.tvdb.get_config_for_series(id, series):
                identifier = 'date' if series.has_genre('talk show', 'news') else 'epcode'
                config.series.append(config.series(id=id, path=fixsep(series.name), identifier=identifier))
        return series


    def delete_series(self, id: str) -> None:
        series = self.tvdb.get_series_by_id(id)
        if not series:
            return
        # Delete from config before we delete from database, since accessing
        # series.cfg indirectly needs the dbrow.
        try:
            config.series.remove(series.cfg)
        except ValueError:
            pass
        self.tvdb.delete_series(series)


    async def _load_series_from_config(self) -> None:
        """
        Ensure all the TV series in the config are included in the DB.
        """
        seen = set()
        for cfg in config.series:
            try:
                series = self.tvdb.get_series_by_id(cfg.id)
            except ValueError as e:
                log.error('malformed config: %s', e)
                continue

            if not series:
                log.info('discovered new series %s in config; adding to database.', cfg.id)
                try:
                    series = await self._add_series_to_db(cfg.id)
                except Exception:
                    log.exception('failed to add series %s', cfg.id)

                if not series:
                    # Could not be added to DB, probably because it doesn't exist.
                    # _add_series_to_db() will have logged an error about it.
                    continue

            if cfg.path == get_default(cfg.path):
                # Set the path based on the show name explicitly to make the
                # config file more readable.
                cfg.path = fixsep(series.name)

            if cfg.provider != series.provider.NAME:
                if not cfg.provider:
                    cfg.provider = get_default(cfg.provider)
                try:
                    await series.change_provider(cfg.provider)
                except ValueError as e:
                    log.error('invalid config: %s', e.args[0])

            # Add all ids for this series to the seen list.
            seen.update(series.ids)

        # Check the database for series that aren't in the config.  This indicates the
        # DB is out of sync with config.  Log an error, and mark the series as
        # ignored.
        for series in self.tvdb.series:
            if series.id not in seen:
                log.error('series %s (%s) in database but not config; ignoring', series.id, series.name)
                self.tvdb.ignore_series_by_id(series.id)
            # Access episodes as well just to warm the cache
            _ = series.episodes

    def get_episode_retrieve_task(self, ep: Episode) -> asyncio.Task[None] | None:
        """
        Returns the Future object for the given Episode object if it is
        currently being retrieved, otherwise returns None.
        """
        return self._retrieve_tasks.get(ep)


    def get_episode_retrieve_progress(self, ep: Episode) -> FutureProgress | None:
        """
        Returns the progress object for the given Episode if it is currently
        being retrieved, otherwise returns None.
        """
        return self._retrieve_progress.get(ep)


    def is_episode_queued_for_retrieval(self, ep: Episode) -> bool:
        """
        Is the given episode currently queued for retrieval?
        """
        return any(ep == qep for qep, _ in self.retrieve_queue)


    def cancel_episode_retrieval(self, ep: Episode) -> None:
        # If the episode is in the pending queue, remove it.
        for qep, results in self._retrieve_queue[:]:
            if ep == qep:
                self._retrieve_queue.remove((qep, results))

        # If the episode is being actively downloaded, abort it.
        task = self.get_episode_retrieve_task(ep)
        if task:
            task.cancel()


    def commit(self) -> None:
        """
        Saves config and database state.  Called on shutdown.
        """
        log.info('shutting down')
        for timer in (self._next_episode_check_timer, self._tvdb_update_timer):
            if timer:
                timer.cancel()
        config.save(self.paths.config)
        self.tvdb.commit()

    def close(self) -> None:
        """
        Saves state and releases resources.  The Manager can't be used
        afterward.
        """
        self.commit()
        config.watch(False)
        self.tvdb.close()


    def _notify_web_retriever_progress(self, progress: FutureProgress | None = None) -> None:
        """
        Issues a notification to web clients about the state of the current
        download queue.
        """
        queue = []
        for ep, _ in self.retrieve_queue:
            p = self.get_episode_retrieve_progress(ep)
            info = None
            if p:
                info = (p.percentage, f'{p.pos / 1024 / 1024:.1f}', f'{p.max / 1024 / 1024:.1f}', int(p.speed / 1024))
            queue.append((ep.series.id, ep.code, info))
        web.notify('dlprogress', queue=queue, replace=True, universal=True)



    @singleton
    async def check_new_episodes(self, only: list[Series] | None = None, force_next: bool = False,
                                 reschedule: bool = False) -> tuple[dict[Series, list[Episode]], list[Episode]]:
        log.info('checking for new episodes and availability')
        # Get a list of all episodes that are ready for retrieval, building a list by series.
        need: dict[Series, list[Episode]] = {}
        for series in self.tvdb.series:
            if only and series not in only:
                continue
            needlist = []
            for ep in series.episodes:
                # TODO: force_next: if True, force-add the first STATUS_NEED/NONE episode
                # for the latest season regardless of airdate.  (Gives the user a way
                # to force an update if airdate is not correct on tvdb.)
                if ep.ready and not ep.series.cfg.paused:
                    airdate = ep.airdatetime.strftime('%Y-%m-%d %H:%M') if ep.airdatetime else 'unknown air date'
                    log.debug('need %s %s (%s): %s', series.name, ep.code, airdate, ep.name)
                    if self.is_episode_queued_for_retrieval(ep):
                        log.debug('episode is already queued for retrieval, skipping')
                        log.debug('retrieve queue is %s', self.retrieve_queue)
                    else:
                        needlist.append(ep)
            if needlist:
                need[series] = needlist

        # XXX find a better place for this
        gc.collect()
        if gc.garbage:
            log.warning('uncollectable garbage exists: %s', gc.garbage)

        found: list[Episode] = []
        if not need:
            log.info('no new episodes; we are all up to date')
        elif not config.searchers.enabled:
            log.error('episodes require fetching but no searchers are enabled')
        else:
            found = await self._search_and_retrieve_needed_episodes(need)

        if reschedule:
            self._schedule_next_episode_check()
        return need, found


    async def _search_and_retrieve_needed_episodes(self, need: dict[Series, list[Episode]]) -> list[Episode]:
        """
        Go through each series' need list and do a search for the required episodes,
        retrieving them if available.
        """
        episodes_found: list[Episode] = []
        for series, episodes in need.items():
            log.info('searching for %d episode(s) of %s', len(episodes), series.name)
            results = await searchers.search(series, episodes)
            if results:
                # We have results, so add them to the retrieve queue and start
                # the retriever coroutine (which is a no-op if it's already
                # running, due to POLICY_SINGLETON).
                #
                # FIXME: need a way to cache results for a given episode, so that if we
                # restart, we have a way to resume downloads without full searching.
                for ep, ep_results in results.items():
                    # XXX: sanity check: the given episode should not have been searched
                    # for if it was already queued for retrieval.  But I've seen cases
                    # where episodes existed multiple times in the retrieve queue, and it's
                    # not clear how.
                    if self.is_episode_queued_for_retrieval(ep):
                        log.error('BUG: searched for episode %s which is already in retrieve queue %s', ep,
                                   self.retrieve_queue)
                        continue
                    for r in ep_results:
                        log.debug2('result %s (%dM) (%s)', r.filename, r.size / 1048576.0, r.searcher)
                    log.debug('adding %s %s to retrieve queue with %d results',
                              series.name, ep.code, len(ep_results))
                episodes_found.extend(results.keys())
                self._retrieve_queue.extend(results.items())
                self._retrieve_queue_event.set()

        need_count = sum(len(eps) for eps in need.values())
        log.info('new episode check finished, found %d/%d episodes', len(episodes_found), need_count)
        return episodes_found


    @singleton
    async def _retrieve_queue_processor(self) -> None:
        while True:
            try:
                await self._do_retrieve_queue_processor()
            except Exception:
                log.exception('retrieve queue processor aborted, respawning')
                await asyncio.sleep(1)
            else:
                break


    async def _do_retrieve_queue_processor(self) -> None:
        """
        This coroutine lives forever and monitors the retrieve queue.  If new episodes
        are found on the retrieve queue, it starts as many parallel downloads are allowed
        by config.retrievers.parallel.

        The processor can be "woken up" by setting _retrieve_queue_event.  This
        event should be set when the queue is changed.
        """
        retrieved: list[Episode] = []
        active: dict[asyncio.Future[Any], tuple[Episode, list[SearchResult]]] = {}
        log.debug('starting retrieve queue processor')
        while True:
            # Check the retrieve queue.  If the number of active downloads is less than
            # the user-configured parallel limit, then pop from the queue.
            ep = None
            if len(active) < config.retrievers.parallel:
                # Before popping, sort retrieve queue so that result sets with
                # older episodes appear first.
                self._retrieve_queue.sort(key=lambda item: (item[0].airdate or datetime.now(), item[0].code))
                try:
                    ep, ep_results = self._retrieve_queue.pop(0)
                except IndexError:
                    # Tried to pop on an empty queue.  We might be done a batch now,
                    # so do notifications if so.
                    if retrieved and not active:
                        self._notify_web_retriever_progress()
                        spawn(notifiers.notify(retrieved), name='notify')
                        retrieved = []
                else:
                    log.debug2('popped %s from retrieve queue', ep)

            # If ep is not None, then we have a free download slot and a queued episode.
            if ep:
                # Sanity check.
                if ep.status == ep.STATUS_HAVE:
                    log.error('BUG: scheduled to retrieve %s %s but it is already STATUS_HAVE',
                              ep.series.name, ep.code)
                    continue
                # Check to see if the episode exists locally.
                elif ep.filename and os.path.exists(os.path.join(ep.season.path, ep.filename)):
                    # The episode filename exists.  Do we need to resume?
                    if ep.search_result:
                        # Yes, there is a search result for this episode, so move it to the
                        # front of the result list so it will be tried first.
                        if ep.search_result in ep_results:
                            ep_results.remove(ep.search_result)
                        ep_results = [ep.search_result] + ep_results
                        log.info('resuming download from last search result')
                    else:
                        # XXX: should we move it out of the way and try again?
                        log.error('retriever was scheduled to fetch %s but it already exists, skipping',
                                  ep.filename)
                        continue

                # Add the episode to the active list, start the download by calling _get_episode(),
                # and continue to process additional episodes from the queue (if possible).
                log.debug2('spawning episode retrieval task')
                self._retrieve_queue_active.append((ep, ep_results))
                task = asyncio.create_task(self._get_episode(ep, ep_results))
                active[task] = ep, ep_results
                continue

            # If we're here, then we've either filled up all the download slots or we have
            # exhausted the queue.  Wait now for any of the active downloads to finish, or
            # for the processor event to force us to pick up newly enqueued episodes.
            event_waiter = asyncio.create_task(self._retrieve_queue_event.wait())
            tasks: set[asyncio.Future[Any]] = {*active.keys(), event_waiter}
            log.debug2('retrieve queue processor waiting for any of %d tasks', len(tasks))
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                event_waiter.cancel()
            log.debug2('retrieve queue processor woke up, done=%d pending=%d', len(done), len(pending))
            # Just blindly clear the retrieve queue event.  The point of the
            # event is to wake us up, which we now are.  If it's already unset,
            # then this is a no-op.
            self._retrieve_queue_event.clear()

            for done_task in done:
                if done_task not in active:
                    # This task must have been the retrieve queue event.  Ignore.
                    continue

                # A download finished (or errored out).  Remove it from the active lists.
                ep, ep_results = active.pop(done_task)
                self._retrieve_queue_active.remove((ep, ep_results))
                # Now that the episode has been removed from the retrieve
                # queue, send an updated notification so the web client sees
                # the epsiode has been removed from the dlprogress list.
                self._notify_web_retriever_progress()
                try:
                    # Accessing result() here will raise any exceptions that
                    # might have occurred in the task.  If it doesn't raise
                    # and returns True then retrieval was successful.  If it
                    # doesn't return True then none of the retrievers could
                    # get the episode.
                    if done_task.result():
                        retrieved.append(ep)
                except RetrieverAbortedHard:
                    # Result was aborted by user, so do nothing.
                    pass
                except Exception as e:
                    # Some other non-abort related error occured, so log it now.
                    strerror = str(e).split('\n')[-1]
                    log.error('download failed: %s', strerror)
                    msg = 'Download failed with an unrecoverable error: %s.' + \
                          ' Intervention is needed.  Check the logs for more details.'
                    web.notify('alert', title='Download Failed', text=msg % strerror, type='error')


    async def _get_episode(self, ep: Episode, ep_results: list[SearchResult]) -> bool:
        """
        Initiate the retriever plugin for the given search result.

        :returns: True if the episode was successfully retrieved, or False if no
                  retrievers were capable of fetching any of the search results.
        :raises: RetrieverHardError if a more serious error occurred with one
                 of the retrievers.
        """
        if not os.path.isdir(ep.season.path):
            # TODO: handle failure
            os.makedirs(ep.season.path)

        log.info('starting retrieval of %s %s', ep.series.name, ep.code)
        web.notify('alert', title='Episode Download',
                   html=f'Starting retrieval of {html.escape(ep.series.name)} {ep.code}.<br/><br/>'
                        'Check progress of <a href="{{root}}/downloads/">active downloads</a>.')

        for search_result in ep_results:
            # Determine name of target file based on naming preferences.
            if config.naming.rename:
                ext = os.path.splitext(search_result.filename)[-1]
                target = ep.preferred_path + ext.lower()
            else:
                target = os.path.join(ep.season.path, search_result.filename)

            ep.search_result = search_result
            ep.filename = os.path.basename(target)
            try:
                # Inner try block catches any RetrieverError so it can properly
                # clean up any partially fetched file, and then reraises so the
                # outer try block can handle more specific exceptions.
                try:
                    # retrieve() ensures that only RetrieverError is raised
                    progress = FutureProgress()
                    task = asyncio.create_task(retrievers.retrieve(progress, search_result, target, ep))
                    progress.connect(self._notify_web_retriever_progress)
                    self._retrieve_tasks[ep] = task
                    self._retrieve_progress[ep] = progress
                    await task
                except RetrieverError:
                    ep.filename = ep.search_result = None
                    if os.path.exists(target):
                        # TODO: handle permission problem
                        log.debug('deleting failed/aborted attempt %s', target)
                        os.unlink(target)
                    # Reraise for outer try block
                    raise
            except RetrieverSoftError:
                # Soft error (including soft abort): try another result.
                continue
            else:
                # TODO: notify per episode (as well as batches)
                log.info('successfully retrieved %s %s', ep.series.name, ep.code)
                #log.debug('not really')
                ep.status = ep.STATUS_HAVE
                return True
            finally:
                # ep may not be in _retrieve_tasks if e.g. retrieve() raises
                # an exception.
                self._retrieve_tasks.pop(ep, None)
                self._retrieve_progress.pop(ep, None)

        return False
