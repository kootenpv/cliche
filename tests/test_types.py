"""Tests for cliche.types.{DateArg, DateTimeArg, DateUtcArg, DateTimeUtcArg}.

Focus: parsing grammar, type relationships, lazy-default wiring, CLI-side
argparse integration. Stdlib-only — no external deps.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from cliche.types import DateArg, DateTimeArg, DateTimeUtcArg, DateUtcArg


# --------- Type relationships ---------

class TestSubclassing:
    def test_datearg_is_a_date(self):
        d = DateArg("2026-04-22")
        assert isinstance(d, date)
        assert isinstance(d, DateArg)
        assert not isinstance(d, datetime)

    def test_dateutcarg_is_a_date(self):
        d = DateUtcArg("2026-04-22")
        assert isinstance(d, date)
        assert isinstance(d, DateUtcArg)

    def test_datetimearg_is_a_datetime(self):
        d = DateTimeArg("2026-04-22T10:00:00")
        assert isinstance(d, datetime)
        assert isinstance(d, date)
        assert isinstance(d, DateTimeArg)

    def test_datetimeutcarg_is_a_datetime(self):
        d = DateTimeUtcArg("2026-04-22T10:00:00")
        assert isinstance(d, datetime)
        assert isinstance(d, DateTimeUtcArg)

    def test_date_arithmetic_still_works(self):
        d = DateArg("2026-04-22") + timedelta(days=5)
        assert d == date(2026, 4, 27)


# --------- Fixed-date parsing ---------

class TestFixedDateInput:
    @pytest.mark.parametrize("s,expected", [
        ("2026-04-22", date(2026, 4, 22)),
        ("2026-01-01", date(2026, 1, 1)),
        ("2000-12-31", date(2000, 12, 31)),
        ("20260422",   date(2026, 4, 22)),
        ("20000101",   date(2000, 1, 1)),
    ])
    def test_datearg_iso_and_compact(self, s, expected):
        assert DateArg(s) == expected

    @pytest.mark.parametrize("cls", [DateArg, DateUtcArg])
    @pytest.mark.parametrize("bad", ["yesterdaz", "2026/04/22", "20260", "not a date", ""])
    def test_bad_date_raises(self, cls, bad):
        with pytest.raises(ValueError):
            cls(bad)


class TestFixedDateTimeInput:
    def test_iso_without_tz(self):
        assert DateTimeArg("2026-04-22T10:30:00") == datetime(2026, 4, 22, 10, 30, 0)

    def test_iso_with_space_separator(self):
        assert DateTimeArg("2026-04-22 10:30:00") == datetime(2026, 4, 22, 10, 30, 0)

    def test_iso_with_tz_offset(self):
        got = DateTimeArg("2026-04-22T10:00:00+01:00")
        assert got.tzinfo is not None
        assert got.utcoffset() == timedelta(hours=1)

    def test_iso_with_tz_offset_compact_form(self):
        # Python 3.11+ accepts both "+01:00" and "+0100"
        got = DateTimeArg("2026-04-22T10:00:00+0100")
        assert got.utcoffset() == timedelta(hours=1)

    def test_iso_with_z_suffix(self):
        got = DateTimeArg("2026-04-22T10:00:00Z")
        assert got.tzinfo == timezone.utc

    def test_compact_datetime(self):
        assert DateTimeArg("20260422T103000") == datetime(2026, 4, 22, 10, 30, 0)

    def test_compact_date_becomes_midnight(self):
        assert DateTimeArg("20260422") == datetime(2026, 4, 22, 0, 0, 0)

    @pytest.mark.parametrize("bad", ["now-ish", "2026-04-22T", "10:00:00", ""])
    def test_bad_datetime_raises(self, bad):
        with pytest.raises(ValueError):
            DateTimeArg(bad)


# --------- Relative grammar ---------

class TestRelativeDate:
    def _today(self):
        return date.today()

    def _utc_today(self):
        return datetime.now(timezone.utc).date()

    def test_today_local(self):
        assert DateArg("today") == self._today()

    def test_yesterday_local(self):
        assert DateArg("yesterday") == self._today() - timedelta(days=1)

    def test_tomorrow_local(self):
        assert DateArg("tomorrow") == self._today() + timedelta(days=1)

    def test_case_insensitive(self):
        assert DateArg("TODAY") == DateArg("today") == DateArg("Today")

    @pytest.mark.parametrize("s,delta", [
        ("+1d", 1), ("-1d", -1),
        ("+7d", 7), ("-7d", -7),
        ("+0d", 0), ("-0d", 0),
        ("+365d", 365), ("-365d", -365),
    ])
    def test_relative_days_local(self, s, delta):
        assert DateArg(s) == self._today() + timedelta(days=delta)

    def test_today_utc(self):
        assert DateUtcArg("today") == self._utc_today()

    def test_yesterday_utc(self):
        assert DateUtcArg("yesterday") == self._utc_today() - timedelta(days=1)


class TestRelativeDateTime:
    def _now_local(self):
        return datetime.now()

    def _now_utc(self):
        return datetime.now(timezone.utc)

    def test_now_local_is_naive(self):
        got = DateTimeArg("now")
        assert got.tzinfo is None
        # Within a few seconds of system now
        assert abs((self._now_local() - got).total_seconds()) < 5

    def test_now_utc_is_aware(self):
        got = DateTimeUtcArg("now")
        assert got.tzinfo is not None
        assert got.utcoffset() == timedelta(0)
        assert abs((self._now_utc() - got).total_seconds()) < 5

    @pytest.mark.parametrize("s,hours", [("+1h", 1), ("-1h", -1), ("+24h", 24)])
    def test_relative_hours(self, s, hours):
        got = DateTimeArg(s)
        delta = got - self._now_local()
        assert abs(delta.total_seconds() - hours * 3600) < 5

    @pytest.mark.parametrize("s,mins", [("+5m", 5), ("-10m", -10), ("+120m", 120)])
    def test_relative_minutes(self, s, mins):
        got = DateTimeArg(s)
        delta = got - self._now_local()
        assert abs(delta.total_seconds() - mins * 60) < 5

    def test_today_on_datetime_is_midnight(self):
        got = DateTimeArg("today")
        assert got.hour == got.minute == got.second == got.microsecond == 0

    def test_yesterday_on_datetime_is_midnight_prev_day(self):
        got = DateTimeArg("yesterday")
        assert got.date() == date.today() - timedelta(days=1)
        assert got.hour == got.minute == got.second == 0


# --------- Passing through existing date/datetime objects ---------

class TestPassThrough:
    def test_datearg_from_date_instance(self):
        orig = date(2026, 4, 22)
        got = DateArg(orig)
        assert got == orig
        assert isinstance(got, DateArg)

    def test_datetimearg_from_datetime_instance(self):
        orig = datetime(2026, 4, 22, 10, 30, 0)
        got = DateTimeArg(orig)
        assert got == orig
        assert isinstance(got, DateTimeArg)

    def test_preserves_tzinfo(self):
        orig = datetime(2026, 4, 22, 10, 30, 0, tzinfo=timezone.utc)
        got = DateTimeUtcArg(orig)
        assert got.tzinfo == timezone.utc

    def test_rejects_non_str_non_date(self):
        with pytest.raises(TypeError):
            DateArg(123)
        with pytest.raises(TypeError):
            DateTimeArg(3.14)


# --------- AST recognition of lazy defaults ---------

class TestLazyArgDetection:
    """Verify the AST scanner flags DateArg("today") etc. as lazy."""

    def _extract(self, source: str):
        from cliche.main import extract_cli_functions
        from pathlib import Path
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(source)
            path = f.name
        functions, _ = extract_cli_functions(
            source, Path(path), Path(path).parent, return_tree=True,
        )
        return functions

    def test_datearg_today_is_flagged_lazy(self):
        funcs = self._extract(
            "from cliche import cli, DateArg\n"
            "from datetime import date\n"
            "@cli\n"
            "def foo(day: date = DateArg('today')): ...\n"
        )
        assert len(funcs) == 1
        params = funcs[0]['parameters']
        day = next(p for p in params if p['name'] == 'day')
        assert day.get('lazy_arg') == {'cls': 'DateArg', 'arg': 'today'}

    def test_datetimeutcarg_now_is_flagged_lazy(self):
        funcs = self._extract(
            "from cliche import cli, DateTimeUtcArg\n"
            "from datetime import datetime\n"
            "@cli\n"
            "def foo(when: datetime = DateTimeUtcArg('now')): ...\n"
        )
        params = funcs[0]['parameters']
        when = next(p for p in params if p['name'] == 'when')
        assert when.get('lazy_arg') == {'cls': 'DateTimeUtcArg', 'arg': 'now'}

    def test_non_arg_call_not_flagged(self):
        funcs = self._extract(
            "from cliche import cli\n"
            "from datetime import date\n"
            "@cli\n"
            "def foo(day: date = date(2026, 4, 22)): ...\n"
        )
        params = funcs[0]['parameters']
        day = next(p for p in params if p['name'] == 'day')
        assert 'lazy_arg' not in day

    def test_arg_call_with_variable_not_flagged(self):
        funcs = self._extract(
            "from cliche import cli, DateArg\n"
            "x = 'today'\n"
            "@cli\n"
            "def foo(day = DateArg(x)): ...\n"
        )
        params = funcs[0]['parameters']
        day = next(p for p in params if p['name'] == 'day')
        # Not a string constant → not recognised as lazy, falls back to eager.
        assert 'lazy_arg' not in day

    def test_iso_literal_still_lazy(self):
        # Any string constant is lazy-eligible; re-evaluation of a fixed date
        # produces the same value, which is harmless.
        funcs = self._extract(
            "from cliche import cli, DateArg\n"
            "from datetime import date\n"
            "@cli\n"
            "def foo(day: date = DateArg('2026-04-22')): ...\n"
        )
        params = funcs[0]['parameters']
        day = next(p for p in params if p['name'] == 'day')
        assert day.get('lazy_arg') == {'cls': 'DateArg', 'arg': '2026-04-22'}
