"""Shared test fixtures for cliche.

The e2e fixture below installs the `tests/cliche_test/` package ONCE at the
start of the session (copied to a tempdir so the source tree isn't mutated by
`install`, which rewrites pyproject.toml), yields the binary name to every test
that asks for it, and uninstalls + rm's the tempdir at session teardown.
"""
import shutil
import subprocess
import sys
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
