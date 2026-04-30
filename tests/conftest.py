"""Shared test fixtures for cliche.

Two coordinated session-level optimisations live here:

1. `_background_warmups` (autouse) — fires the slow, fully-independent
   subprocess work (mypy, clichec build) on a worker thread at session
   start, so it overlaps with the conftest install and the rest of setup
   instead of blocking later test files. See module-doc below.

2. `real_installs` — ONE batched `pip install -e ...` covering every
   real-install fixture used by the suite (cliche_test, test_install's 4
   variants, test_cache_freshness's package). Three separate pip
   invocations collapse into one — pip startup cost is paid once, not
   thrice. Per-file fixtures (`cli_binary`, test_install's `_real_installs`,
   test_cache_freshness's `freshness_env`) are now thin views over this.
"""
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest


BINARY_NAME = "nc_test_bin"  # unique so it can't collide with real tools

# Tool-level blurb baked into the `main` fixture's pyproject.toml after
# `cliche install` generates it. Distinctive enough to grep for in --help and
# --llm-help parity tests; the C and Python launchers must surface it
# identically (both pull it from the cache as `description`).
PARITY_PYPROJECT_DESCRIPTION = (
    "Cliche test fixture — exercises the help/llm-help/parity surfaces."
)


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
# pip-mutating fixtures (real_installs below) CANNOT go here — concurrent pip
# races on .pth/dist-info. cli_binary-dependent fixtures CANNOT go here — they
# need the install to land first.
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
    the futures start running while `real_installs`'s pip is happening.
    """
    pool = ThreadPoolExecutor(max_workers=2)
    mypy_dir = tmp_path_factory.mktemp("mypy_probes")
    futures = {
        "mypy": pool.submit(_warmup_mypy, mypy_dir),
        "clichec": pool.submit(_warmup_clichec),
    }
    yield futures
    pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Unified real-install fixture.
#
# Three separate pip-mutating session/module fixtures used to do their own
# `pip install -e`:
#   - conftest.cli_binary     (nc_test_bin / cliche_test)
#   - test_install._real_installs (4 variants)
#   - test_cache_freshness.freshness_env (nc_freshness_bin)
#
# Sequential pip startup * 3 ≈ 1.5 s of fixture wall-clock. Concurrent pip is
# unsafe (races on .pth / dist-info), so the only sound speedup is to merge
# them into ONE batched `pip install -e workA workB ...` command. That's what
# `real_installs` does. Per-file fixtures become thin views over its result.
#
# real_installs ALSO launches the post-install warmups (cli_results pre-run,
# parity warmup) on a background thread pool — they need the binary to be
# installed but can run concurrently with subsequent test execution.
# ---------------------------------------------------------------------------


def _warmup_cli_results(cli_binary: str) -> dict:
    """Pre-run every subprocess call from E2E_ARGV_MATRIX concurrently."""
    sys.path.insert(0, str(Path(__file__).parent))
    from e2e_matrix import E2E_ARGV_MATRIX

    def _one(item):
        key, argv = item
        return key, subprocess.run(
            [cli_binary, *argv], capture_output=True, text=True,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(_one, E2E_ARGV_MATRIX.items()))


def _warmup_parity(cli_binary: str, clichec_future: Future) -> tuple[str | None, dict]:
    """Wait for the clichec build, then run the parity warmup. Returns
    (cache_path, results)."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_clichec_parity import compute_parity_warmup
    clichec = clichec_future.result()
    return compute_parity_warmup(cli_binary, clichec)


# CLI sources used by the synthetic packages. These intentionally live next to
# the install matrix below so a `pytest -k <something>` run still has all the
# context in one place.
_PING_CLI_SRC = (
    "from cliche import cli\n"
    "\n"
    "@cli\n"
    "def ping():\n"
    "    return {'pong': True}\n"
)

_SCD_SINGLE_BINARY = "scd_solo"
_SCD_MULTI_BINARY = "scd_multi"

_SCD_SINGLE_SRC = (
    "from cliche import cli\n"
    "\n"
    "@cli\n"
    f"def {_SCD_SINGLE_BINARY}(name: str, times: int = 1):\n"
    "    for _ in range(times):\n"
    "        print(f'hi {name}')\n"
)

_SCD_MULTI_SRC = (
    "from cliche import cli\n"
    "\n"
    "@cli\n"
    "def solo(name: str):\n"
    "    print(f'hi {name}')\n"
    "\n"
    "@cli\n"
    "def other(x: int):\n"
    "    print(x * 2)\n"
)

_FRESHNESS_INITIAL_SRC = (
    "from cliche import cli\n"
    "\n"
    "@cli\n"
    "def hello():\n"
    '    """Greeter."""\n'
    '    return {"v": 1}\n'
)


# All real-install variants in one place. `kind` decides workdir layout;
# `probe_args`, when present, runs that argv against the installed binary at
# fixture setup time so the result is a free dict-lookup later.
_REAL_INSTALL_SPECS: dict[str, dict] = {
    # The conftest e2e package — copied from tests/cliche_test/, NOT
    # synthesised from a string. Kept first because it's the most-used.
    "main": {
        "binary": BINARY_NAME,
        "pkg": "cliche_test",
        "kind": "copytree",
        "source": Path(__file__).parent / "cliche_test",
    },
    # test_cache_freshness — module-level mutations during tests, but the
    # initial install is shared with everyone else's pip step.
    "freshness": {
        "binary": "nc_freshness_bin",
        "pkg": "cliche_freshness_pkg",
        "kind": "subdir",
        "src": _FRESHNESS_INITIAL_SRC,
    },
    # test_install layout variants — these need probes (binary invoked from a
    # foreign cwd to verify the layout choice).
    "layout_subdir": {
        "binary": "nc_subdir_bin",
        "pkg": "subpkg",
        "kind": "subdir",
        "src": _PING_CLI_SRC,
        "probe_args": ["ping"],
    },
    "layout_renamed_flat": {
        "binary": "nc_renamed_bin",
        "pkg": "nc_renamed_pkg",
        "kind": "renamed_flat",
        "src": _PING_CLI_SRC,
        "probe_args": ["ping"],
    },
    # test_install single-command-dispatch variants — no probe (the tests
    # themselves invoke the binary, with arg matrices that vary per test).
    "scd_single": {
        "binary": _SCD_SINGLE_BINARY,
        "pkg": None,  # default: dir basename
        "kind": "flat",
        "src": _SCD_SINGLE_SRC,
    },
    "scd_multi": {
        "binary": _SCD_MULTI_BINARY,
        "pkg": None,
        "kind": "flat",
        "src": _SCD_MULTI_SRC,
    },
}


def _make_workdir(tmp_path_factory, name: str, spec: dict) -> Path:
    """Materialise the per-variant workdir on disk."""
    if spec["kind"] == "copytree":
        # Pre-existing package source on disk — copy it so install can rewrite
        # pyproject.toml in place without touching the repo's source of truth.
        work_root = tmp_path_factory.mktemp(f"inst_{name}")
        work = work_root / spec["source"].name
        shutil.copytree(spec["source"], work)
        return work
    if spec["kind"] == "subdir":
        # workdir/<pkg>/{__init__,cli}.py
        work = tmp_path_factory.mktemp(f"inst_{name}")
        inner = work / spec["pkg"]
        inner.mkdir()
        (inner / "__init__.py").write_text(f'"""{name} package."""\n')
        (inner / "cli.py").write_text(spec["src"])
        return work
    if spec["kind"] == "renamed_flat":
        # Flat layout where workdir basename ≠ pkg name. The mismatch is the
        # whole point of this variant (test_install tests it explicitly).
        work = tmp_path_factory.mktemp(f"inst_{name}") / "dirname_mismatch"
        work.mkdir()
        (work / "__init__.py").write_text(f'"""{name} renamed flat package."""\n')
        (work / "cli.py").write_text(spec["src"])
        return work
    # flat (scd) — package dir IS the workdir
    work = tmp_path_factory.mktemp(f"inst_{name}")
    (work / "__init__.py").write_text(f'"""{name} package."""\n')
    (work / "cli.py").write_text(spec["src"])
    return work


@pytest.fixture(scope="session")
def real_installs(tmp_path_factory, _background_warmups) -> Iterator[dict]:
    """ONE batched pip install of every real-install fixture in the suite,
    plus background launch of binary-dependent warmups.

    Phases:
      1. Workdirs in parallel (cheap, just `mkdir` + `write_text`).
      2. `cliche.install install --no-pip` per variant in parallel — each
         boots one Python interpreter; n parallel ≈ one serial.
      3. ONE `pip install -e workA workB ...` for ALL variants — pip's
         startup / resolver cost is paid once, not n times. Dominant saving.
      4. Probes for variants whose tests want a pre-recorded invocation.
      5. Launch cli_results + parity warmups on a thread pool — they need
         the binary to be installed (so can't run before phase 3) but can
         run concurrently with whatever tests fire next. Stored under
         `["_warmups"]` for the per-file fixtures to .result().
      6. Parallel uninstalls + pool shutdown at teardown.

    Returns dict: per-variant entries (keyed by spec name) plus `_warmups`
    holding the post-install futures. Embedding the warmups inside this
    fixture (rather than a separate autouse one) means they fire only when
    real_installs is actually needed — `pytest tests/test_mypy.py` skips
    them entirely.
    """
    workdirs: dict[str, Path] = {
        name: _make_workdir(tmp_path_factory, name, spec)
        for name, spec in _REAL_INSTALL_SPECS.items()
    }

    # Phase 1: cliche file-gen (no pip). Each variant boots one Python.
    def _file_gen(name: str):
        spec = _REAL_INSTALL_SPECS[name]
        cmd = [sys.executable, "-m", "cliche.install", "install",
               spec["binary"], "-d", str(workdirs[name])]
        if spec.get("pkg"):
            cmd += ["-p", spec["pkg"]]
        cmd += ["--no-autocomplete", "--force", "--no-pip"]
        return name, subprocess.run(cmd, capture_output=True, text=True)

    with ThreadPoolExecutor(max_workers=len(_REAL_INSTALL_SPECS)) as pool:
        file_gen_results = dict(pool.map(_file_gen, _REAL_INSTALL_SPECS.keys()))

    for name, result in file_gen_results.items():
        if result.returncode != 0:
            pytest.fail(
                f"{name} pyproject generation failed ({result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

    # Inject a [project].description into the `main` fixture's pyproject so
    # the parity warmup primes a cache that carries it. test_clichec_parity
    # asserts both renderers surface this line identically.
    main_pyproject = workdirs["main"] / "pyproject.toml"
    main_pyproject.write_text(
        main_pyproject.read_text().replace(
            'version = "0.1.0"',
            f'version = "0.1.0"\ndescription = "{PARITY_PYPROJECT_DESCRIPTION}"',
            1,
        )
    )

    # Phase 2: ONE pip install -e for every workdir.
    uv_path = shutil.which("uv")
    if uv_path:
        pip_cmd = [uv_path, "pip", "install", "--python", sys.executable]
    else:
        pip_cmd = [sys.executable, "-m", "pip", "install"]
    for work in workdirs.values():
        pip_cmd += ["-e", str(work)]
    pip_result = subprocess.run(pip_cmd, capture_output=True, text=True)
    if pip_result.returncode != 0:
        pytest.fail(
            f"batched editable install failed ({pip_result.returncode}):\n"
            f"stdout: {pip_result.stdout}\nstderr: {pip_result.stderr}"
        )

    # Phase 3: probes — only the variants that asked for one.
    def _probe(name: str):
        spec = _REAL_INSTALL_SPECS[name]
        return name, subprocess.run(
            [spec["binary"], *spec["probe_args"]],
            capture_output=True, text=True, cwd=tempfile.gettempdir(),
        )

    probe_names = [n for n, s in _REAL_INSTALL_SPECS.items() if "probe_args" in s]
    if probe_names:
        with ThreadPoolExecutor(max_workers=len(probe_names)) as pool:
            probes = dict(pool.map(_probe, probe_names))
    else:
        probes = {}

    results: dict = {
        name: {
            "spec": _REAL_INSTALL_SPECS[name],
            "binary": _REAL_INSTALL_SPECS[name]["binary"],
            "work": workdirs[name],
            "install": file_gen_results[name],
            "probe": probes.get(name),
        }
        for name in _REAL_INSTALL_SPECS
    }

    # Phase 4: kick off binary-dependent warmups on a background thread pool.
    # These run concurrently with whatever test runs next (typically
    # test_cache_freshness's tests, since it's alphabetically first among the
    # files that need real_installs).
    warmup_pool = ThreadPoolExecutor(max_workers=2)
    cli_binary = _REAL_INSTALL_SPECS["main"]["binary"]
    results["_warmups"] = {
        "cli_results": warmup_pool.submit(_warmup_cli_results, cli_binary),
        "parity": warmup_pool.submit(_warmup_parity, cli_binary,
                                     _background_warmups["clichec"]),
    }

    try:
        yield results
    finally:
        warmup_pool.shutdown(wait=False)
        # Parallel teardown across all variants.
        def _uninstall(name: str):
            subprocess.run(
                [sys.executable, "-m", "cliche.install", "uninstall",
                 _REAL_INSTALL_SPECS[name]["binary"]],
                capture_output=True, text=True,
            )
        with ThreadPoolExecutor(max_workers=len(_REAL_INSTALL_SPECS)) as pool:
            list(pool.map(_uninstall, _REAL_INSTALL_SPECS.keys()))


@pytest.fixture(scope="session")
def cli_binary(real_installs) -> str:
    """Binary name of the e2e fixture package. Install lifecycle is owned by
    `real_installs` — this fixture is just a thin view."""
    return real_installs["main"]["binary"]


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
def cli_results(real_installs) -> dict:
    """Result of the post-install e2e pre-run. Blocks only if not yet done.

    The work runs on a background thread fired by `real_installs` itself, so
    by the time the first test_e2e test asks for it the future is typically
    already resolved.
    """
    return real_installs["_warmups"]["cli_results"].result()
