"""
Logging customizations: a DEBUG2 level for very verbose output, and shorter
logger names in log records.
"""
import logging
from typing import Any

DEBUG2 = 5
logging.DEBUG2 = DEBUG2  # type: ignore[attr-defined]
logging.addLevelName(DEBUG2, 'DEBUG2')

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'


class Logger(logging.Logger):
    def makeRecord(self, name: str, *args: Any, **kwargs: Any) -> logging.LogRecord:
        name = name.removeprefix('stagehand.')
        return super().makeRecord(name, *args, **kwargs)

    def debug2(self, msg: object, *args: object, **kwargs: object) -> None:
        if self.isEnabledFor(DEBUG2):
            self._log(DEBUG2, msg, args, **kwargs)  # type: ignore[arg-type]


logging.setLoggerClass(Logger)


def setup(level: int = logging.WARNING) -> None:
    """
    Configures the root logger.  Called by the application entry point.
    """
    logging.basicConfig(format=LOG_FORMAT, level=level)


def getLogger(name: str) -> Logger:
    """
    Typed equivalent of :func:`logging.getLogger` that exposes ``debug2()``.
    """
    log = logging.getLogger(name)
    assert isinstance(log, Logger), 'stagehand.logger must be imported before loggers are created'
    return log
