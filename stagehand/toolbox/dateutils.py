"""
Date and time helpers.
"""
from __future__ import annotations

__all__ = ['from_rfc822', 'TZAny', 'TZUTC', 'TZLocal']

import calendar
import email.utils
from datetime import UTC, datetime, timedelta, timezone, tzinfo


def from_rfc822(date: str) -> datetime | None:
    """
    Robust parser for an RFC822 style date.

    Out-of-range fields (which are surprisingly common in the wild) are
    clamped to sane values rather than rejected.

    :param date: an RFC822 style date (e.g. `"Mon, 20 Nov 1995 19:12:08 -0500"`)
    :returns: a timezone-aware datetime object, or None if the date could not
              be parsed.  If the date has no timezone, UTC is assumed.
    """
    parsed = email.utils.parsedate_tz(date)
    if not parsed:
        return None

    def clamp(val: int, minval: int, maxval: int) -> int:
        return min(max(val, minval), maxval)

    year = parsed[0] + 2000 if parsed[0] < 1900 else parsed[0]
    month = clamp(parsed[1], 1, 12)
    day = clamp(parsed[2], 1, calendar.monthrange(year, month)[1])
    hour = clamp(parsed[3], 0, 23)
    minute = clamp(parsed[4], 0, 59)
    sec = clamp(parsed[5], 0, 59)
    tzoffset = parsed[9]

    if tzoffset is None:
        return datetime(year, month, day, hour, minute, sec, tzinfo=UTC)
    elif abs(tzoffset) >= 24 * 3600:
        # Timezone is +/- 24h, which is not valid.  Adjust the date with the
        # given crazy offset and treat the timezone as UTC.
        return datetime(year, month, day, hour, minute, sec, tzinfo=UTC) + timedelta(seconds=tzoffset)
    return datetime(year, month, day, hour, minute, sec, tzinfo=timezone(timedelta(seconds=tzoffset)))


class TZAny(tzinfo):
    """
    A fixed-offset timezone.

    .. deprecated::
       New code uses :class:`datetime.timezone`.  This class remains only so
       that search results pickled into existing databases by older versions
       of Stagehand can still be loaded.
    """
    def __init__(self, tz: str | float | None = None) -> None:
        self._sign = 1
        self._hours = self._mins = 0
        if isinstance(tz, str):
            self._sign = -1 if tz[0] == '-' else 1
            idx = 1 if tz[0] in ('+', '-') else 0
            self._mins = int(tz[-2:])
            self._hours = int(tz[idx:len(tz) - 2])
        elif isinstance(tz, (int, float)):
            self._sign = -1 if tz < 0 else 1
            self._hours, rem = divmod(int(abs(tz)), 3600)
            self._mins = rem // 60
        elif tz is not None:
            raise ValueError('tz must be +HHMM or number in seconds')

    def __repr__(self) -> str:
        return f"<TZAny '{self.tzname(None)}'>"

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return '{}{:02d}{:02d}'.format('-' if self._sign < 0 else '+', int(self._hours), int(self._mins))

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._sign * timedelta(hours=self._hours, minutes=self._mins)


class TZUTC(tzinfo):
    """
    .. deprecated:: Retained for unpickling legacy data; use :data:`datetime.UTC`.
    """
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return 'UTC'


class TZLocal(tzinfo):
    """
    .. deprecated:: Retained for unpickling legacy data; use ``datetime.astimezone()``.
    """
    def _local(self, dt: datetime | None) -> tzinfo:
        tz = (dt or datetime.now()).replace(tzinfo=None).astimezone().tzinfo
        assert tz is not None
        return tz

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return self._local(dt).utcoffset(dt)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return self._local(dt).dst(dt)

    def tzname(self, dt: datetime | None) -> str | None:
        return self._local(dt).tzname(dt)
