"""
Asynchronous web jobs and notifications.

Some API calls take a long time (e.g. searching for a series).  Rather than
block the HTTP request, such handlers are decorated with :func:`webcoroutine`,
which starts a job and responds immediately with a job id.  The web client
then polls ``/api/jobs`` to retrieve the job's result, along with any
notifications queued for its session.
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Concatenate

from aiohttp import web

from .context import LOGLEVEL, SESSION, json_response

__all__ = ['AsyncWeb', 'AsyncWebJob', 'asyncweb', 'notify', 'notify_after', 'routes', 'webcoroutine']

log = logging.getLogger('stagehand.web.jobs')

routes = web.RouteTableDef()


@dataclass(eq=False)
class AsyncWebJob:
    session: str | None
    id: int | None
    timestamp: float = field(default_factory=time.time)
    finished: bool = False
    error: dict[str, str] | None = None
    result: Any = None
    # For notifications: sessions which have seen this notification.
    seen: set[str] = field(default_factory=set)
    # For notifications: if True, re-deliver to the session until it expires.
    universal: bool = False

    def notify(self, ntype: str, **kwargs: Any) -> None:
        asyncweb.notify(ntype, session=self.session, id=self.id, **kwargs)

    def notify_after(self, ntype: str, **kwargs: Any) -> None:
        asyncweb.notify_after(ntype, session=self.session, **kwargs)

    def finish(self, future: asyncio.Future[Any]) -> None:
        try:
            self.result = future.result()
        except asyncio.CancelledError:
            self.error = {'message': 'Cancelled'}
        except Exception as e:
            args = ', '.join(str(s) for s in e.args)
            self.error = {'message': f'{e.__class__.__name__}: {args}'}
            log.exception('webcoroutine exception')
        self.finished = True


class AsyncWeb:
    job_timeout = 1800
    notification_timeout = 60

    def __init__(self) -> None:
        self._job_queue: dict[str | None, list[AsyncWebJob]] = {}
        self._notification_queue: dict[str | None, list[AsyncWebJob]] = {}
        # Begin job ids at the current timestamp to prevent conflicting
        # job ids between instance restarts.
        self._next_id = itertools.count(int(time.time())).__next__
        self._cleanup_timer: asyncio.TimerHandle | None = None
        # Strong references to running jobs.
        self._tasks: set[asyncio.Task[Any]] = set()

    def _schedule_cleanup(self) -> None:
        if self._cleanup_timer is None:
            try:
                self._cleanup_timer = asyncio.get_running_loop().call_later(60, self._cleanup)
            except RuntimeError:
                pass

    def _cleanup(self) -> None:
        self._cleanup_timer = None
        now = time.time()
        for queue, timeout in ((self._job_queue, self.job_timeout),
                               (self._notification_queue, self.notification_timeout)):
            for session, jobs in list(queue.items()):
                jobs[:] = [job for job in jobs if now - job.timestamp <= timeout]
                if not jobs:
                    log.debug('purging timed out jobs for session %s', session)
                    del queue[session]
        if self._job_queue or self._notification_queue:
            # More cleanup needed, restart timer.
            self._schedule_cleanup()

    def new_job(self, session: str) -> AsyncWebJob:
        job = AsyncWebJob(session=session, id=self._next_id())
        self._job_queue.setdefault(session, []).append(job)
        self._schedule_cleanup()
        return job

    def watch_job(self, task: asyncio.Task[Any], job: AsyncWebJob) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(job.finish)

    def pop_finished_jobs(self, session: str | None, jobs: str = '') -> dict[str, list[Any]]:
        """
        Returns (and removes from the queue) the results of the given
        finished jobs, and any pending notifications for the session.

        :param jobs: comma-separated list of job ids the client is waiting on
        """
        response: dict[str, list[Any]] = {'jobs': [], 'notifications': []}
        if not session:
            return response
        wanted = {int(j) for j in (jobs or '').split(',') if j.strip().isdigit()}

        if session in self._job_queue:
            remaining = []
            for job in self._job_queue[session]:
                if job.finished and job.id in wanted:
                    if job.error:
                        response['jobs'].append({'id': job.id, 'error': job.error})
                    else:
                        response['jobs'].append({'id': job.id, 'result': job.result})
                else:
                    remaining.append(job)
            if remaining:
                self._job_queue[session] = remaining
            else:
                del self._job_queue[session]

        # Get all notifications for this session.
        if session in self._notification_queue:
            remaining = []
            for n in self._notification_queue[session]:
                if n.id is None or n.id in wanted:
                    response['notifications'].append(n.result)
                else:
                    remaining.append(n)
            if remaining:
                self._notification_queue[session] = remaining
            else:
                del self._notification_queue[session]

        # Get all global notifications not seen by this session.
        now = time.time()
        global_notifications = self._notification_queue.get(None, [])
        for n in global_notifications:
            if session not in n.seen or n.universal:
                response['notifications'].insert(0, n.result)
                n.seen.add(session)
        global_notifications[:] = [n for n in global_notifications
                                   if now - n.timestamp <= self.notification_timeout]
        return response

    def notify(self, ntype: str, *, session: str | None = None, id: int | None = None, replace: bool = False,
               universal: bool = False, **kwargs: Any) -> None:
        """
        Issue a notification for one or more web clients.

        :param ntype: the type of this notification (e.g. ``alert``).  The
                      web client will hook callbacks for certain types.
        :param session: the session id the notification applies to, or None
                        for all sessions
        :param id: the job id the notification applies to, or None if no job is
                   involved
        :param replace: if True, replace all existing notifications of this type
                        for the given session with the new notification
        :param universal: if True, do not track which sessions have seen the notification,
                          and show it each time the client polls, until the notification
                          expires.  Useful to ensure multiple windows from the same
                          browser session receive the notification.
        """
        n = AsyncWebJob(session=session, id=id, universal=universal)
        n.result = {'_ntype': ntype, '_nid': self._next_id(), **kwargs}
        if replace and session in self._notification_queue:
            # Remove any notification for this type from the queue.
            self._notification_queue[session] = [job for job in self._notification_queue[session]
                                                 if job.result['_ntype'] != ntype]
        self._notification_queue.setdefault(session, []).append(n)
        self._schedule_cleanup()

    def notify_after(self, ntype: str, timeout: float = 0, **kwargs: Any) -> None:
        """
        Like :meth:`notify` but issues the notification after the given
        timeout, so that it is delivered on a subsequent request.
        """
        asyncio.get_running_loop().call_later(timeout, functools.partial(self.notify, ntype, **kwargs))


asyncweb = AsyncWeb()
notify = asyncweb.notify
notify_after = asyncweb.notify_after

JobHandler = Callable[Concatenate[web.Request, AsyncWebJob, ...], Coroutine[Any, Any, Any]]


def webcoroutine(interval: int = 1000, blockfor: float = 0) -> Callable[[JobHandler], Callable[[web.Request], Awaitable[web.Response]]]:
    """
    Decorates a request handler to run as an asynchronous job.

    The decorated coroutine function receives the request, the
    :class:`AsyncWebJob`, and the URL match parameters as keyword
    arguments.  Its return value is delivered to the client as the job result.

    :param interval: the polling interval (ms) the client should use
    :param blockfor: seconds to wait for the job before responding; if the job
                     finishes in this time, the result is included directly
    """
    def decorator(func: JobHandler) -> Callable[[web.Request], Awaitable[web.Response]]:
        @functools.wraps(func)
        async def wrapper(request: web.Request) -> web.Response:
            session = request[SESSION]
            job = asyncweb.new_job(session)
            response: dict[str, Any] = {'jobid': job.id, 'interval': interval, 'pending': False}

            task = asyncio.create_task(func(request, job, **request.match_info))
            done, _ = await asyncio.wait({task}, timeout=blockfor)
            if task in done:
                job.finish(task)
            else:
                # The job will finish asynchronously and the client will
                # have to pick up the result by querying /api/jobs
                log.debug("webcoroutine didn't finish in %.1f seconds, result will come asynchronously", blockfor)
                asyncweb.watch_job(task, job)
                response['pending'] = True

            # Get job results for all supplied job ids plus this new one.
            jobs = request.query.get('jobs', '') + f',{job.id}'
            response.update(asyncweb.pop_finished_jobs(session, jobs))
            log.debug('webcoroutine response: %s', response)
            return json_response(response)
        return wrapper
    return decorator


@routes.get('/api/jobs')
async def jobs(request: web.Request) -> web.Response:
    response = asyncweb.pop_finished_jobs(request[SESSION], request.query.get('jobs', ''))
    # This gets executed quite frequently by the client, so lower the log level
    # to reduce spamminess.
    request[LOGLEVEL] = logging.DEBUG
    return json_response(response)
