"""Shared test fixtures for cliche.

The e2e fixture below installs the `tests/cliche_test/` package ONCE at the
start of the session (copied to a tempdir so the source tree isn't mutated by
`install`, which rewrites pyproject.toml), yields the binary name to every test
that asks for it, and uninstalls + rm's the tempdir at session teardown.
"""
import shutil
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest


BINARY_NAME = "nc_test_bin"  # unique so it can't collide with real tools


@pytest.fixture(autouse=True)
def reset_raw_mode():
    """RAW_MODE is a module-level global — reset between tests."""
    from cliche import run
    original = run.RAW_MODE
    yield
    run.RAW_MODE = original


# ---------------------------------------------------------------------------
# Background warmups — fire independent slow setup work at session start so it
# overlaps with the conftest install + everything else, instead of blocking on
# the first test that needs it. Only fixtures that share NO state with anything
# else qualify (they have to run safely in a worker thread without coordination
# with the main pytest thread):
#
#   - mypy: own subprocess, own tmpdir, own cache. Saves ~0.5s.
#   - clichec build: gcc to a fixed path nobody else touches. Saves ~0.05s.
#
# pip-mutating fixtures (test_install._real_installs, test_cache_freshness)
# CANNOT go here — concurrent pip races on .pth/dist-info. cli_binary-dependent
# fixtures CANNOT go here — they need the install to land first.
# ---------------------------------------------------------------------------


def _warmup_mypy(tmp_path: Path) -> dict[str, list[str]]:
    """Run mypy once over every probe in test_mypy. Same logic as the fixture
    in test_mypy.py — duplicated here so the work can fire from session start
    without waiting for the test_mypy module to be imported by collection."""
    import importlib.util
    import textwrap
    if importlib.util.find_spec("mypy") is None:
        return {}

    sys.path.insert(0, str(Path(__file__).parent))
    from test_mypy import CLEAN_PROBES, KNOWN_BAD_PROBES

    probes: dict[str, str] = {
        **CLEAN_PROBES,
        **{name: src for name, (src, _) in KNOWN_BAD_PROBES.items()},
    }
    files: list[str] = []
    for name, source in probes.items():
        p = tmp_path / f"{name}.py"
        p.write_text(textwrap.dedent(source).lstrip() + "\n")
        files.append(str(p))

    proc = subprocess.run(
        [sys.executable, "-m", "mypy",
         "--no-error-summary",
         "--follow-imports=silent",
         "--cache-dir", str(tmp_path / ".mypy_cache"),
         *files],
        capture_output=True, text=True,
    )

    by_name: dict[str, list[str]] = {name: [] for name in probes}
    for line in proc.stdout.splitlines():
        for name in probes:
            prefix = str(tmp_path / f"{name}.py") + ":"
            if line.startswith(prefix):
                by_name[name].append(line)
                break
    return by_name


def _warmup_clichec() -> str | None:
    """Build clichec once; returns its path or None if no compiler."""
    from cliche._clichec import ensure_built
    p = ensure_built(verbose=False)
    return str(p) if p else None


@pytest.fixture(scope="session", autouse=True)
def _background_warmups(tmp_path_factory) -> Iterator[dict[str, Future]]:
    """Kick off independent slow work at session start; tests look up futures.

    `autouse=True` is the whole point: without it, this fixture would only
    fire when the first test asked for `mypy_results` or `clichec_binary`,
    which is the same time as before — defeating the parallelism. With
    autouse, pytest sets it up before the first test's other fixtures, so
    the futures start running while `cli_binary`'s install is happening.
    """
    pool = ThreadPoolExecutor(max_workers=2)
    mypy_dir = tmp_path_factory.mktemp("mypy_probes")
    futures = {
        "mypy": pool.submit(_warmup_mypy, mypy_dir),
        "clichec": pool.submit(_warmup_clichec),
    }
    yield futures
    pool.shutdown(wait=False)


@pytest.fixture(scope="session")
def cli_binary(tmp_path_factory):
    """Install the e2e fixture package once per session, tear down at the end.

    Yields the binary name (a str). Tests run the binary via subprocess — see
    the `run_cli` helper below. Install runs into the current Python env
    (editable); uninstall removes everything cliche placed. The tmpdir is
    rm'd by pytest's tmp_path_factory machinery.
    """
    fixture_src = Path(__file__).parent / "cliche_test"
    assert fixture_src.is_dir(), f"fixture package missing at {fixture_src}"

    work_dir = tmp_path_factory.mktemp("nc_e2e")
    pkg_dir = work_dir / "cliche_test"
    # Copy sources to tempdir so `install` can mutate pyproject.toml etc
    # without touching the repo's source of truth.
    shutil.copytree(fixture_src, pkg_dir)

    install_cmd = [
        sys.executable, "-m", "cliche.install", "install",
        BINARY_NAME, "-d", str(pkg_dir), "--no-autocomplete",
        "--force",  # clobber any stale install from a prior crashed session
    ]
    result = subprocess.run(install_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(
            f"install failed ({result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    try:
        yield BINARY_NAME
    finally:
        subprocess.run(
            [sys.executable, "-m", "cliche.install", "uninstall", BINARY_NAME],
            capture_output=True, text=True,
        )


@pytest.fixture(scope="session")
def run_cli(cli_binary):
    """Return a callable: run_cli(*args) -> CompletedProcess.

    Session-scoped because it closes over cli_binary. Each test still gets a
    fresh subprocess, so there's no cross-test state leak via this helper.
    """
    def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            [cli_binary, *args],
            capture_output=True, text=True, check=check,
        )
    return _run


@pytest.fixture(scope="session")
def cli_results(cli_binary):
    """Pre-run every subprocess call from E2E_ARGV_MATRIX concurrently.

    Tests then look up their case by key — no per-test subprocess latency.
    Drops e2e wall-clock from ~3s to ~1s while keeping one pytest test per
    behaviour (readable output, `pytest -k <name>` still works).

    The matrix is imported lazily so conftest doesn't take a hard dep on the
    e2e test file.
    """
    from concurrent.futures import ThreadPoolExecutor
    # Sibling module in tests/ — import directly, not via the `tests` package
    # (pyproject.toml treats `tests/` as a top-level dir, not an importable
    # package, since there's no `tests` entry in [tool.setuptools]).
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from e2e_matrix import E2E_ARGV_MATRIX

    def _one(item):
        key, argv = item
        return key, subprocess.run(
            [cli_binary, *argv], capture_output=True, text=True,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(_one, E2E_ARGV_MATRIX.items()))
    return results
