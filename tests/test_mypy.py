"""Mypy type-check coverage.

Every @cli pattern our docs recommend is represented as a tiny probe below.
A session-scoped fixture writes all probes to a temp directory and runs
mypy ONCE over the whole set — each test then just looks up its probe's
error lines and asserts. This mirrors the test_e2e trick: mypy's startup
is the dominant cost, so spawning it per-test costs 10× what batching costs.

A separate class pins the *known limitation* where a user-defined callable
is used as an annotation (the "custom type callables" shorthand). That
shape is documented as mypy-unfriendly; this test freezes the expectation
so the docs' warning stays accurate.

Skipped entirely when mypy isn't installed, so the suite remains runnable
without `.[test]` extras.
"""
import importlib.util

import pytest


_HAS_MYPY = importlib.util.find_spec("mypy") is not None

pytestmark = pytest.mark.skipif(not _HAS_MYPY, reason="mypy not installed")


# ---------------------------------------------------------------------------
# Probes: name → source. Names become filenames; keep them snake_case.
# Each probe is an island (its own module) so any error from one can't leak
# into another test's results.
# ---------------------------------------------------------------------------

CLEAN_PROBES: dict[str, str] = {
    "primitives": """
        from cliche import cli

        @cli
        def f(a: int, b: str, c: bool = False, d: float = 1.0) -> None:
            print(a, b, c, d)
    """,
    "path_and_union_none": """
        from pathlib import Path
        from cliche import cli

        @cli
        def f(p: Path, q: Path | None = None) -> None:
            print(p.name, q)
    """,
    "date_and_datetime": """
        from datetime import date, datetime
        from cliche import cli

        @cli
        def f(d: date, t: datetime) -> None:
            print(d.isoformat(), t.isoformat())
    """,
    "container_elements": """
        from pathlib import Path
        from cliche import cli

        @cli
        def a(nums: tuple[int, ...]) -> None:
            print(sum(nums))

        @cli
        def b(paths: tuple[Path, ...]) -> None:
            for p in paths:
                print(p.name)

        @cli
        def c(items: tuple[int, ...] = ()) -> None:
            print(items)
    """,
    "dict_parameters": """
        from cliche import cli

        @cli
        def f(tags: dict[str, int] = {}) -> None:
            for k, v in tags.items():
                print(k, v)
    """,
    "bool_defaults": """
        from cliche import cli

        @cli
        def on(verbose: bool = False) -> None:
            print(verbose)

        @cli
        def off(use_cache: bool = True) -> None:
            print(use_cache)
    """,
    "enum_positional_and_default": """
        from enum import Enum
        from cliche import cli

        class Mode(Enum):
            FAST = "fast"
            SAFE = "safe"

        @cli
        def pick(m: Mode) -> None:
            print(m.value)

        @cli
        def run(m: Mode = Mode.SAFE) -> None:
            print(m.value)
    """,
    "grouped_subcommands": """
        from cliche import cli

        @cli("math")
        def add(a: int, b: int) -> None:
            print(a + b)

        @cli("math")
        def mul(a: int, b: int) -> None:
            print(a * b)

        @cli
        def ping() -> None:
            print("pong")
    """,
    "async_function": """
        import asyncio
        from cliche import cli

        @cli
        async def delay(seconds: float, message: str = "done") -> None:
            await asyncio.sleep(seconds)
            print(message)
    """,
    "return_auto_printed": """
        from cliche import cli

        @cli
        def f(a: int, b: int) -> dict[str, int]:
            return {"sum": a + b}
    """,
}


KNOWN_BAD_PROBES: dict[str, tuple[str, str]] = {
    # name -> (source, expected substring in error)
    "callable_shorthand": (
        """
        from cliche import cli

        def Port(s: str) -> int:
            return int(s)

        @cli
        def serve(port: Port) -> None:
            print(port)
        """,
        "not valid as a type",
    ),
}


# ---------------------------------------------------------------------------
# Session-scoped batching: one mypy run, results indexed by filename.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mypy_results(_background_warmups) -> dict[str, list[str]]:
    """Result of the session-start mypy warmup. Blocks only if not yet done.

    The actual run lives in conftest._warmup_mypy so it can fire at session
    start in a worker thread, overlapping with the conftest install. Same
    return shape as before; tests don't care that the work moved.
    """
    return _background_warmups["mypy"].result()


def _check_clean(results: dict[str, list[str]], name: str) -> None:
    errs = results[name]
    if errs:
        pytest.fail(f"mypy flagged {name}:\n" + "\n".join(errs))


# ---------------------------------------------------------------------------
# Tests — one per probe, thin lookups over the batched result.
# ---------------------------------------------------------------------------

class TestRecommendedPatterns:
    """Each pattern in our docs must type-check cleanly."""

    def test_primitives(self, mypy_results):               _check_clean(mypy_results, "primitives")
    def test_path_and_union_none(self, mypy_results):      _check_clean(mypy_results, "path_and_union_none")
    def test_date_and_datetime(self, mypy_results):        _check_clean(mypy_results, "date_and_datetime")
    def test_container_elements(self, mypy_results):       _check_clean(mypy_results, "container_elements")
    def test_dict_parameters(self, mypy_results):          _check_clean(mypy_results, "dict_parameters")
    def test_bool_defaults(self, mypy_results):            _check_clean(mypy_results, "bool_defaults")
    def test_enum_positional_and_default(self, mypy_results): _check_clean(mypy_results, "enum_positional_and_default")
    def test_grouped_subcommands(self, mypy_results):      _check_clean(mypy_results, "grouped_subcommands")
    def test_async_function(self, mypy_results):           _check_clean(mypy_results, "async_function")
    def test_return_auto_printed(self, mypy_results):      _check_clean(mypy_results, "return_auto_printed")


class TestKnownLimitations:
    """Patterns our docs warn about — mypy must still flag them."""

    def test_callable_shorthand_is_flagged(self, mypy_results):
        errs = mypy_results["callable_shorthand"]
        expected = KNOWN_BAD_PROBES["callable_shorthand"][1]
        assert errs, (
            "mypy accepted `port: Port` — README warns this should be flagged. "
            "If mypy's rules relaxed, update the docs."
        )
        assert any(expected in e for e in errs), (
            f"expected mypy's '{expected}' message, got:\n" + "\n".join(errs)
        )
