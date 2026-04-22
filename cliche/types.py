"""CLI-friendly date/datetime types for @cli parameter defaults.

Recommended usage::

    from datetime import date, datetime
    from cliche.types import DateArg, DateTimeArg, DateUtcArg, DateTimeUtcArg

    @cli
    def report(
        day:  date     = DateUtcArg("today"),      # UTC today, fresh each run
        when: datetime = DateTimeUtcArg("now"),    # UTC now, fresh each run
    ):
        ...

Why four classes:
  - DateArg / DateTimeArg      — local-clock based ("today" = local date).
  - DateUtcArg / DateTimeUtcArg — UTC-based ("today" = UTC date).

Prefer UTC for coordinated data pipelines (no DST, no machine-locale surprise).
Use local-clock variants for interactive-shell or day-of-work tooling.

Grammar (all classes):
  - "today", "yesterday", "tomorrow"            (date-only)
  - "now"                                       (datetime-only)
  - "+Nd" / "-Nd"                               (N days from today)
  - "+Nh" / "-Nh"                               (N hours from now, datetime-only)
  - "+Nm" / "-Nm"                               (N minutes from now, datetime-only)
  - "YYYY-MM-DD"                                (ISO-8601 date)
  - "YYYYMMDD"                                  (compact date)
  - "YYYY-MM-DDTHH:MM:SS[+HHMM]" etc.           (ISO-8601 datetime, w/ optional tz)
  - "YYYYMMDDTHHMMSS"                           (compact datetime)

No external dependencies — pure stdlib.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

__all__ = ["DateArg", "DateTimeArg", "DateUtcArg", "DateTimeUtcArg"]


_REL_DAYS_RE = re.compile(r"^([+-])(\d+)d$")
_REL_HOURS_RE = re.compile(r"^([+-])(\d+)h$")
_REL_MINUTES_RE = re.compile(r"^([+-])(\d+)m$")
_COMPACT_DATE_RE = re.compile(r"^\d{8}$")
_COMPACT_DT_RE = re.compile(r"^\d{8}T\d{6}$")


def _today_local() -> date:
    return date.today()


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _now_local() -> datetime:
    return datetime.now()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(s: str, today_fn) -> date:
    s = s.strip()
    lower = s.lower()
    if lower == "today":
        return today_fn()
    if lower == "yesterday":
        return today_fn() - timedelta(days=1)
    if lower == "tomorrow":
        return today_fn() + timedelta(days=1)
    m = _REL_DAYS_RE.match(lower)
    if m:
        n = int(m.group(2))
        return today_fn() + timedelta(days=n if m.group(1) == "+" else -n)
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    if _COMPACT_DATE_RE.match(s):
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"not a valid date: {s!r}")


def _parse_datetime(s: str, now_fn) -> datetime:
    s = s.strip()
    lower = s.lower()
    if lower == "now":
        return now_fn()
    if lower == "today":
        d = now_fn()
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    if lower == "yesterday":
        d = now_fn()
        return d.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    if lower == "tomorrow":
        d = now_fn()
        return d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    m = _REL_DAYS_RE.match(lower)
    if m:
        n = int(m.group(2))
        return now_fn() + timedelta(days=n if m.group(1) == "+" else -n)
    m = _REL_HOURS_RE.match(lower)
    if m:
        n = int(m.group(2))
        return now_fn() + timedelta(hours=n if m.group(1) == "+" else -n)
    m = _REL_MINUTES_RE.match(lower)
    if m:
        n = int(m.group(2))
        return now_fn() + timedelta(minutes=n if m.group(1) == "+" else -n)
    # ISO-8601 datetime: accept both space and T separators, with optional tz.
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        pass
    # Compact YYYYMMDDTHHMMSS
    if _COMPACT_DT_RE.match(s):
        return datetime.strptime(s, "%Y%m%dT%H%M%S")
    # Compact YYYYMMDD → midnight of that day
    if _COMPACT_DATE_RE.match(s):
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"not a valid datetime: {s!r}")


def _date_new(cls, args, today_fn):
    """Shared constructor for DateArg / DateUtcArg.

    Accepts three calling shapes:
      - cls(year, month, day)   — the base `date` call pattern; emitted by
        `date + timedelta` and other date arithmetic. We must pass through
        unchanged or arithmetic breaks.
      - cls(date_instance)       — pass-through.
      - cls(string)              — parse per the module grammar.
    """
    if len(args) == 3 and all(isinstance(a, int) for a in args):
        return date.__new__(cls, *args)
    if len(args) != 1:
        raise TypeError(f"{cls.__name__} expects (str) or (year, month, day); got {len(args)} args")
    s = args[0]
    if isinstance(s, date) and not isinstance(s, datetime):
        return date.__new__(cls, s.year, s.month, s.day)
    if not isinstance(s, str):
        raise TypeError(f"{cls.__name__} expects str or date, got {type(s).__name__}")
    d = _parse_date(s, today_fn)
    return date.__new__(cls, d.year, d.month, d.day)


def _datetime_new(cls, args, kwargs, now_fn):
    """Shared constructor for DateTimeArg / DateTimeUtcArg.

    Accepts:
      - cls(year, month, day, [hour, minute, second, microsecond, tzinfo])
        — the base `datetime` call pattern; needed for arithmetic.
      - cls(datetime_instance)   — pass-through.
      - cls(string)              — parse.
    """
    if args and isinstance(args[0], int):
        return datetime.__new__(cls, *args, **kwargs)
    if len(args) != 1 or kwargs:
        raise TypeError(
            f"{cls.__name__} expects (str) or (year, month, day, ...); got args={args} kwargs={kwargs}"
        )
    s = args[0]
    if isinstance(s, datetime):
        return datetime.__new__(
            cls, s.year, s.month, s.day, s.hour, s.minute,
            s.second, s.microsecond, s.tzinfo,
        )
    if not isinstance(s, str):
        raise TypeError(f"{cls.__name__} expects str or datetime, got {type(s).__name__}")
    d = _parse_datetime(s, now_fn)
    return datetime.__new__(
        cls, d.year, d.month, d.day, d.hour, d.minute,
        d.second, d.microsecond, d.tzinfo,
    )


class DateArg(date):
    """A `date` that parses strings per the module grammar, using local time
    for relative expressions ("today" = local date).

    Intended primary use is as a default value in @cli signatures::

        day: date = DateArg("today")
    """
    __slots__ = ()

    def __new__(cls, *args):
        return _date_new(cls, args, _today_local)


class DateUtcArg(date):
    """Like DateArg but "today" / "yesterday" / "-Nd" resolve against UTC."""
    __slots__ = ()

    def __new__(cls, *args):
        return _date_new(cls, args, _today_utc)


class DateTimeArg(datetime):
    """A `datetime` that parses strings per the module grammar, using local
    naive time for relative expressions ("now" = datetime.now())."""
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        return _datetime_new(cls, args, kwargs, _now_local)


class DateTimeUtcArg(datetime):
    """Like DateTimeArg but "now" / "-Nh" resolve against UTC (tz-aware)."""
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        return _datetime_new(cls, args, kwargs, _now_utc)


# Set of class-name strings the AST scanner recognises as "lazy-callable
# default" helpers. When a @cli parameter's default is `Call(Name(X), [Constant])`
# and X is in this set, cliche defers construction until dispatch time
# AND uses X as the argparse type converter (overriding the annotation-based
# inference), so rich grammar works on both sides of the CLI boundary.
LAZY_ARG_CLASSES = {"DateArg", "DateTimeArg", "DateUtcArg", "DateTimeUtcArg"}
