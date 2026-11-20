# -----------------------------------------------------------------------------
# core.py - core classes and functions
# -----------------------------------------------------------------------------
# Copyright 2010-2014 Dirk Meyer, Jason Tackaberry
#
# Originally from kaa.base, ported to Python 3 and asyncio by Jason Tackaberry
#
#
# This library is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version
# 2.1 as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301 USA
# -----------------------------------------------------------------------------
from __future__ import annotations

__all__ = ['Signal', 'Signals', 'FutureProgress', 'synchronized', 'singleton', 'spawn']

import asyncio
import functools
import inspect
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from typing import Any, ParamSpec, TypeVar

log = logging.getLogger('toolbox')

P = ParamSpec('P')
T = TypeVar('T')

# Strong references to fire-and-forget tasks, so they aren't garbage
# collected before they finish.  See the note in asyncio.create_task().
_background_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
    """
    Schedules a coroutine to run in the background on the running loop.

    Unlike a bare ``asyncio.create_task()``, a reference to the task is held
    until it completes, and any unhandled exception is logged rather than
    silently discarded.
    """
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_task_done)
    return task


def _background_task_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.error('background task %s failed', task.get_name(), exc_info=task.exception())


class Signal:
    """
    Create a Signal object to which callbacks can be connected and later
    invoked in sequence when the Signal is emitted.
    """
    __slots__ = '_callbacks', '_deferred_args', '_changed_cb', '_future'

    # Defines the maximum number of connections to a signal.  Attempting to
    # connect more than this many callbacks will trigger an exception.
    MAX_CONNECTIONS = 1000

    # Constants used for the action parameter for changed_cb.
    CONNECTED = 1
    DISCONNECTED = 2

    def __init__(self, changed_cb: Callable[[Signal, int], Any] | None = None) -> None:
        """
        :param changed_cb: corresponds to the :attr:`~Signal.changed_cb` property.
        """
        if changed_cb and not callable(changed_cb):
            raise TypeError('changed_cb must be callable')
        self._changed_cb = changed_cb
        self._callbacks: list[functools.partial[Any]] = []
        self._deferred_args: list[tuple[tuple[Any, ...], dict[str, Any]]] | None = None
        self._future: asyncio.Future[Any] | None = None

    @property
    def changed_cb(self) -> Callable[[Signal, int], Any] | None:
        """
        Callable to be invoked whenever a callback is connected to or
        disconnected from the Signal.

        .. describe:: def callback(signal, action)

           :param signal: the :class:`~Signal` object acted upon
           :param action: either ``Signal.CONNECTED`` or ``Signal.DISCONNECTED``
        """
        return self._changed_cb

    @changed_cb.setter
    def changed_cb(self, callback: Callable[[Signal, int], Any] | None) -> None:
        if callback and not callable(callback):
            raise TypeError('value must be callable')
        self._changed_cb = callback

    @property
    def callbacks(self) -> tuple[functools.partial[Any], ...]:
        """
        Tuple containing the callbacks connected to this signal.

        Because this value is a tuple, it cannot be manipulated directly.  Use
        :meth:`~Signal.connect` and :meth:`~Signal.disconnect` instead.
        """
        return tuple(self._callbacks)

    def __iter__(self) -> Iterator[functools.partial[Any]]:
        return iter(self._callbacks)

    def __len__(self) -> int:
        return len(self._callbacks)

    def __bool__(self) -> bool:
        return True

    def __contains__(self, func: object) -> bool:
        return any(cb.func == func for cb in self._callbacks)

    def _connect(self, callback: Callable[..., Any], args: tuple[Any, ...] = (),
                 kwargs: dict[str, Any] | None = None, once: bool = False, pos: int = -1) -> functools.partial[Any]:
        """
        Connects a new callback to the signal.  args and kwargs will be bound
        to the callback and merged with the args and kwargs passed during
        emit().  If once is True, the callback will be automatically
        disconnected after the next emit().
        """
        if not callable(callback):
            raise TypeError(f'callback must be callable, got {callback} instead.')

        if len(self._callbacks) >= Signal.MAX_CONNECTIONS:
            raise ValueError(f'Number of callbacks exceeds Signal.MAX_CONNECTIONS limit ({Signal.MAX_CONNECTIONS})')

        partial = functools.partial(callback, *args, **(kwargs or {}))
        partial._signal_once = once  # type: ignore[attr-defined]

        if pos == -1:
            pos = len(self._callbacks)

        self._callbacks.insert(pos, partial)
        self._changed(Signal.CONNECTED)

        if self._deferred_args:
            # Clear deferred args before emitting, in case callbacks do emit_deferred().
            deferred_args, self._deferred_args = self._deferred_args, None
            for dargs, dkwargs in deferred_args:
                self.emit(*dargs, **dkwargs)

        return partial

    def connect(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> functools.partial[Any]:
        """
        Connects the callback with the (optional) given arguments to be invoked
        when the signal is emitted.

        :return: a new :class:`functools.partial` object encapsulating the supplied
                 callable and arguments.
        """
        return self._connect(callback, args, kwargs)

    def connect_once(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> functools.partial[Any]:
        """
        Variant of :meth:`~Signal.connect` where the callback is automatically
        disconnected after one signal emission.
        """
        return self._connect(callback, args, kwargs, once=True)

    def connect_first(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> functools.partial[Any]:
        """
        Variant of :meth:`~Signal.connect` in which the given callback is
        inserted to the front of the callback list.
        """
        return self._connect(callback, args, kwargs, pos=0)

    def connect_first_once(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> functools.partial[Any]:
        """
        Variant of :meth:`~Signal.connect_once` in which the given callback is
        inserted to the front of the callback list.
        """
        return self._connect(callback, args, kwargs, once=True, pos=0)

    def _disconnect(self, callback: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> int:
        assert callable(callback)
        noargs = len(args) == len(kwargs) == 0
        new_callbacks = []
        for cb in self._callbacks:
            cbargs, cbkwargs = cb.args, cb.keywords or {}
            if (cb.func == callback and (noargs or (args, kwargs) == (cbargs, cbkwargs))) or cb == callback:
                # This matches what we want to disconnect.
                continue
            new_callbacks.append(cb)

        num_removed = len(self._callbacks) - len(new_callbacks)
        if num_removed:
            self._callbacks = new_callbacks
            self._changed(Signal.DISCONNECTED)
        return num_removed

    def _changed(self, action: int) -> None:
        """
        Called when a callback was connected or disconnected.

        :param action: Signal.CONNECTED or Signal.DISCONNECTED
        """
        if self._changed_cb:
            self._changed_cb(self, action)

    def disconnect(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> int:
        """
        Disconnects the given callback from the signal so that future emissions
        will not invoke that callback any longer.

        If neither args nor kwargs are specified, all instances of the given
        callback (regardless of what arguments they were originally connected with)
        will be disconnected.

        :param callback: either the callback originally connected, or the
                         :class:`functools.partial` object returned by
                         :meth:`~Signal.connect`.
        :return: the number of callbacks removed from the signal
        """
        return self._disconnect(callback, args, kwargs)

    def disconnect_all(self) -> None:
        """
        Disconnects all callbacks from the signal.
        """
        count = self.count()
        self._callbacks = []
        if count > 0:
            self._changed(Signal.DISCONNECTED)

    def emit(self, *args: Any, **kwargs: Any) -> bool:
        """
        Emits the signal, passing the given arguments callback connected to the signal.

        Callbacks may be coroutine functions, in which case the coroutine is
        scheduled as a background task on the running loop.

        :return: False if any of the callbacks returned False, and True otherwise.
        """
        retval = True
        for cb in self._callbacks[:]:
            if cb._signal_once:  # type: ignore[attr-defined]
                self.disconnect(cb)

            try:
                result = cb(*args, **kwargs)
                if inspect.iscoroutine(result):
                    spawn(result)
                elif result is False:
                    retval = False
            except Exception:
                log.exception('Exception while emitting signal')

        if self._future:
            if len(args) == 1:
                if isinstance(args[0], Exception):
                    self._future.set_exception(args[0])
                else:
                    self._future.set_result(args[0])
            else:
                self._future.set_result(args)
            self._future = None

        return retval

    def emit_deferred(self, *args: Any, **kwargs: Any) -> None:
        """
        Queues the emission until after the next callback is connected.

        This allows a signal to be 'primed' by its creator, and the handler
        that subsequently connects to it will be called with the given
        arguments.
        """
        if self._deferred_args is None:
            self._deferred_args = [(args, kwargs)]
        else:
            self._deferred_args.append((args, kwargs))

    def emit_when_handled(self, *args: Any, **kwargs: Any) -> bool | None:
        """
        Emits the signal if there are callbacks connected, or defers it until
        the first callback is connected.
        """
        if self.count():
            return self.emit(*args, **kwargs)
        self.emit_deferred(*args, **kwargs)
        return None

    def count(self) -> int:
        """
        Returns the number of callbacks connected to the signal.

        Equivalent to ``len(signal)``.
        """
        return len(self._callbacks)

    def future(self) -> asyncio.Future[Any]:
        """
        Returns a Future object that is done when the signal next emits.
        """
        if not self._future:
            self._future = asyncio.get_running_loop().create_future()
        return self._future


class Signals(dict[str, Signal]):
    """
    A collection of one or more Signal objects, which behaves like a dictionary.

    The initializer takes zero or more arguments, where each argument can be a:
        * dict (of name=Signal() pairs) or other Signals object
        * tuple/list of (name, Signal) tuples
        * str representing the name of the signal
    """
    def __init__(self, *signals: dict[str, Signal] | tuple[str, Signal] | list[Any] | str) -> None:
        super().__init__()
        for s in signals:
            if isinstance(s, dict):
                self.update(s)
            elif isinstance(s, str):
                self[s] = Signal()
            elif isinstance(s, (tuple, list)) and len(s) == 2:
                if isinstance(s[0], str) and isinstance(s[1], Signal):
                    self[s[0]] = s[1]
                else:
                    raise TypeError('With form (k, v), key must be string and v must be Signal')
            else:
                raise TypeError('signal key must be string')

    def __add__(self, signals: Any) -> Signals:
        return Signals(self, *signals)

    def add(self, *signals: Any) -> Signals:
        """
        Creates a new Signals object by merging all signals defined in
        self and the signals specified in the arguments.
        """
        return Signals(self, *signals)

    def subset(self, *names: str) -> Signals:
        """
        Returns a new Signals object by taking a subset of the supplied
        signal names.
        """
        return Signals(*[(k, self[k]) for k in names])

    def futures(self) -> list[asyncio.Future[Any]]:
        return [signal.future() for signal in self.values()]

    async def any(self) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        return await asyncio.wait(self.futures(), return_when=asyncio.FIRST_COMPLETED)

    async def all(self, return_exceptions: bool = True) -> list[Any]:
        return await asyncio.gather(*self.futures(), return_exceptions=return_exceptions)


class FutureProgress(Signal):
    """
    Tracks the progress of some operation (e.g. a download).  The signal is
    emitted (with the progress object as the argument) at most once per
    ``interval`` seconds as progress is updated.
    """
    __slots__ = 'start_time', '_last_emit_time', '_last_pos', 'pos', 'max', 'interval', '_speed'

    def __init__(self, max: float = 0, interval: float = 1) -> None:
        super().__init__()
        self.start_time = time.monotonic()
        self._last_emit_time = 0.0
        self._last_pos: float | None = None
        self.pos: float = 0
        self.max = max
        self.interval = interval
        self._speed: float | None = None

    def set(self, pos: float | None = None, max: float | None = None, speed: float | None = None) -> None:
        """
        Set new status. The new status is pos of max.
        """
        if max is not None:
            self.max = max
        if pos is not None:
            self.pos = pos
        if self.pos > self.max:
            self.max = self.pos
        if speed is not None:
            self._speed = speed

        now = time.monotonic()
        interval = now - self._last_emit_time
        if interval > self.interval:
            if speed is None and self._last_pos is not None:
                delta = self.pos - self._last_pos
                self._speed = delta / interval
            self._last_emit_time = now
            self._last_pos = self.pos
            self.emit(self)

    def update(self, diff: float = 1, speed: float | None = None) -> None:
        """
        Update position by the given difference.
        """
        self.set(self.pos + diff, speed=speed)

    def get_ascii_bar(self, width: int = 70) -> str:
        """
        Return a small ASCII art progressbar.
        """
        n = int((self.pos / self.max) * (width - 3)) if self.max else 0
        return '|' + ('=' * n + '>').ljust(width - 2) + '|'

    @property
    def elapsed(self) -> float:
        """
        Return time elapsed since the operation started.
        """
        return time.monotonic() - self.start_time

    @property
    def eta(self) -> float:
        """
        Estimated time left to complete the operation, assuming every step
        takes the same amount of time.
        """
        if not self.pos:
            return 0
        sec = self.elapsed / self.pos
        return sec * (self.max - self.pos)

    @property
    def percentage(self) -> float:
        """
        Return percentage of steps done.
        """
        return (self.pos * 100.0) / self.max if self.max else 0

    @property
    def speed(self) -> float:
        """
        The current speed of the operation as set by :meth:`set` or
        :meth:`update`.  Units are up to the caller.
        """
        return self._speed or 0


def synchronized(func: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    """
    A decorator that synchronizes multiple invocations of a coroutine function
    to ensure they are executed sequentially rather than in parallel.
    """
    # One lock per event loop, since asyncio primitives are bound to the loop
    # they're first used with.
    locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        lock = locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
        async with lock:
            return await func(*args, **kwargs)
    return wrapper


def singleton(func: Callable[P, Any]) -> Callable[P, Any]:
    """
    A decorator that ensures that only one active instance of the decorated
    function is allowed to exist at any given moment.

    If the decorated function is a coroutine function, invoking it returns
    the :class:`asyncio.Task` of the currently running instance, creating
    one if none is active.  Concurrent callers therefore share the result of
    a single invocation.

    Otherwise the function is scheduled to be called in the next event loop
    iteration, which allows multiple invocations to be coalesced into a
    single call.  In this case, the returned value is a Handle object.
    """
    if inspect.iscoroutinefunction(func):
        task: asyncio.Task[Any] | None = None

        @functools.wraps(func)
        def coro_wrapper(*args: P.args, **kwargs: P.kwargs) -> asyncio.Task[Any]:
            nonlocal task
            if task is None or task.done():
                task = spawn(func(*args, **kwargs), name=func.__qualname__)
            return task
        return coro_wrapper

    handle: asyncio.Handle | None = None

    def dofunc(*args: Any, **kwargs: Any) -> None:
        nonlocal handle
        handle = None
        func(*args, **kwargs)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> asyncio.Handle:
        nonlocal handle
        if handle is None:
            handle = asyncio.get_running_loop().call_soon(functools.partial(dofunc, *args, **kwargs))
        return handle
    return wrapper
