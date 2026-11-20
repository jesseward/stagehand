"""
Command line entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import importlib.resources
import logging
import os
import signal
import sys
from typing import Any, NoReturn

import aiohttp

from . import __version__, logger, web
from .config import config
from .manager import Manager, Paths
from .toolbox import singleton, spawn
from .toolbox.net import close_session
from .toolbox.utils import daemonize, tempfile
from .web.context import SHUTDOWN

log = logger.getLogger('stagehand')

# Ports to try (after the configured port) if the web server can't listen.
FALLBACK_PORTS = [*range(8088, 8100), *range(18088, 18100)]


class FullHelpParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        sys.stderr.write(f'error: {message}\n')
        self.print_help()
        sys.exit(2)


def find_data_path() -> str:
    """
    Returns the directory holding Stagehand's bundled static data.
    """
    path = importlib.resources.files('stagehand') / 'data'
    if not path.is_dir():
        sys.exit(f"Error: couldn't find static data at {path}")
    return str(path)


def build_path(*parts: str) -> str:
    return os.path.expandvars(os.path.expanduser(os.path.join(*parts)))


def default_paths(datadir: str | None = None) -> Paths:
    if datadir and not os.path.isdir(datadir):
        raise FileNotFoundError(datadir)
    cache = build_path(os.getenv('XDG_CACHE_HOME', '~/.cache'), 'stagehand')
    cfgdir = build_path(os.getenv('XDG_CONFIG_HOME', '~/.config'), 'stagehand')
    return Paths(
        data=datadir or find_data_path(),
        webcache=os.path.join(cache, 'web'),
        logs=os.path.join(cache, 'logs'),
        config=os.path.join(cfgdir, 'config'),
        db=os.path.join(cfgdir, 'tv.db'),
    )


def port_file() -> str:
    return tempfile('stagehand', 'port')


async def start_web_server(mgr: Manager, args: argparse.Namespace) -> bool:
    """
    Starts the web server, trying a number of ports until one works.  Now
    that the manager is instantiated, the config file will have been loaded
    and config.web.port will be the user-configured value (if any).

    :returns: True if the server started.
    """
    kwargs: dict[str, Any] = {'debug': len(args.verbose) >= 2}
    if config.web.username:
        kwargs['user'] = str(config.web.username)
        kwargs['password'] = str(config.web.password)
    host = str(config.web.bind_address or '')

    for port in [args.port or int(config.web.port), *FALLBACK_PORTS]:
        try:
            await web.server.start(mgr, mgr.paths.data, host=host, port=port, **kwargs)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EADDRINUSE):
                log.error('could not start webserver on port %d: %s', port, e)
            elif e.errno == errno.EADDRNOTAVAIL:
                log.error('could not start webserver on interface %s: %s', host, e)
                break
            else:
                raise
        else:
            # Server started successfully.  Store current port for service control.
            with open(port_file(), 'w') as f:
                f.write(f'{port}\n')
            return True

    log.critical('failed to start webserver; edit %s or restart with different arguments', mgr.paths.config)
    return False


async def call_rest_api(path: str) -> bytes | None:
    """
    Calls the REST API of an already running Stagehand instance.

    :returns: the response body, or None if no instance is running.
    """
    try:
        with open(port_file()) as f:
            port = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None

    middlewares = []
    if config.web.username:
        middlewares.append(aiohttp.DigestAuthMiddleware(str(config.web.username), str(config.web.password)))
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with (aiohttp.ClientSession(timeout=timeout, middlewares=tuple(middlewares)) as session,
                    session.get(f'http://localhost:{port}{path}') as response):
            data = await response.read()
            if response.status != 200:
                log.error('request failed (status %s): %s', response.status, data.decode(errors='replace'))
            return data
    except (aiohttp.ClientConnectionError, TimeoutError):
        return None


def configure_logging(args: argparse.Namespace, paths: Paths) -> None:
    logger.setup(logging.INFO)
    formatter = logging.Formatter(logger.LOG_FORMAT)
    root = logging.getLogger()
    handler: logging.Handler = logging.FileHandler(os.path.join(paths.logs, 'stagehand.log'))
    handler.setFormatter(formatter)
    root.addHandler(handler)

    httplog = logging.getLogger('stagehand.http')
    handler = logging.FileHandler(os.path.join(paths.logs, 'http.log'))
    handler.setFormatter(formatter)
    httplog.addHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [HTTP] %(message)s'))
    httplog.addHandler(handler)
    httplog.propagate = False

    levels = {'stagehand': logging.INFO, 'stagehand.http': logging.INFO, 'config': logging.INFO}
    nverbose = len(args.verbose)
    if args.quiet:
        levels['stagehand'] = logging.CRITICAL
    elif nverbose == 1:
        levels.update({'stagehand': logging.DEBUG, 'config': logging.DEBUG, 'stagehand.web': logging.INFO})
    elif nverbose >= 2:
        levels.update({'stagehand': logger.DEBUG2, 'config': logging.DEBUG})
        if nverbose >= 3:
            levels.update({'stagehand.web': logger.DEBUG2, 'stagehand.http': logging.DEBUG})
        else:
            levels['stagehand.web'] = logging.DEBUG
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


async def run(paths: Paths, args: argparse.Namespace) -> Manager:
    """
    Runs Stagehand until it is asked to stop (by signal or web API).
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    mgr = Manager(paths)

    def reload() -> None:
        log.warning('received SIGHUP: purging in-memory caches and reloading config')
        config.load()
        mgr.tvdb.purge_caches()
        mgr.check_new_episodes()

    def resync() -> None:
        log.warning('received SIGUSR1: refreshing TV metadata')
        spawn(mgr.tvdb.sync(), name='resync')

    @singleton
    def web_config_changed(*_: Any) -> None:
        """
        Invoked when any value in the config.web group changes.

        This is decorated with @singleton so that if multiple variables in
        the config.web group change, the server only restarts once.
        """
        log.warning('restarting webserver due to changed configuration')
        spawn(restart_web(), name='web-restart')

    async def restart_web() -> None:
        if stop.is_set():
            return
        if await start_web_server(mgr, args):
            watch_shutdown()
        else:
            stop.set()

    def watch_shutdown() -> None:
        # The web API can request shutdown via the app's shutdown event.
        app = web.server.app
        if app is not None:
            spawn(_wait_and_set(app[SHUTDOWN], stop), name='web-shutdown-watch')

    loop.add_signal_handler(signal.SIGHUP, reload)
    loop.add_signal_handler(signal.SIGUSR1, resync)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    config.web.add_monitor(web_config_changed)
    try:
        if not await start_web_server(mgr, args):
            return mgr
        watch_shutdown()
        spawn(mgr.start(), name='manager-start')
        await stop.wait()
        log.info('stopping')
    finally:
        config.web.remove_monitor(web_config_changed)
        for sig in (signal.SIGHUP, signal.SIGUSR1, signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        await web.server.stop()
        mgr.close()
        await close_session()
        try:
            os.unlink(port_file())
        except OSError:
            pass
    return mgr


async def _wait_and_set(event: asyncio.Event, target: asyncio.Event) -> None:
    await event.wait()
    target.set()


def main(argv: list[str] | None = None) -> int:
    p = FullHelpParser(prog='stagehand')
    p.add_argument('-q', '--quiet', dest='quiet', action='store_true',
                   help='disable all logging')
    p.add_argument('-v', '--verbose', dest='verbose', action='append_const', default=[], const=1,
                   help='log more detail (twice or thrice logs even more)')
    p.add_argument('-b', '--bg', dest='background', action='store_true',
                   help='run stagehand in the background (daemonize)')
    p.add_argument('-p', '--port', dest='port', action='store', type=int, default=0,
                   help=f'port the embedded webserver listens on (default is {config.web.port})')
    p.add_argument('-d', '--data', dest='data', action='store', metavar='PATH',
                   help="path to Stagehand's static data directory")
    p.add_argument('-s', '--stop', dest='stop', action='store_true',
                   help='stop a currently running instance of Stagehand')
    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = p.parse_args(argv)

    paths = default_paths(args.data)
    if os.path.exists(paths.config):
        config.load(paths.config)
    if config.misc.logdir != config.misc.logdir.default:
        # If logdir configurable is non-default, then apply it.
        paths.logs = build_path(str(config.misc.logdir))
    paths.create()
    configure_logging(args, paths)

    if args.stop:
        if asyncio.run(call_rest_api('/api/shutdown')) is None:
            log.info('Stagehand is not running.')
        return 0

    # Make sure Stagehand isn't already running.
    if asyncio.run(call_rest_api('/api/pid')) is not None:
        log.error('Stagehand is already running')
        return 1

    if args.background:
        daemonize(chdir=None)

    log.info('starting Stagehand %s', __version__)
    asyncio.run(run(paths, args))
    return 0


if __name__ == '__main__':
    sys.exit(main())
