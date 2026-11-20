"""
Stagehand: a manager for your favourite TV series.
"""
from importlib.metadata import PackageNotFoundError, version

from . import logger as _logger  # noqa: F401  (registers the DEBUG2 log level)

try:
    __version__ = version('stagehand')
except PackageNotFoundError:  # pragma: no cover  (running from an uninstalled checkout)
    __version__ = '0.0.0+unknown'
