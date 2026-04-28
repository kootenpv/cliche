"""End-to-end cache-freshness tests for an installed cliche binary.

Each test mutates the same package directory and expects the next subprocess
invocation of the installed binary to discover the new @cli function. Tests
share one install (module-scoped fixture) to avoid paying ~3 s of pip per
test, and run in definition order — each layers its mutation on top of the
previous test's state.

What's covered:
    1. baseline               — pre-mutation sanity
    2. add @cli to existing   — mtime drift on a known file
    3. add a new .py file     — file the cache has never seen, same dir
    4. add a new subpackage   — file inside a new directory entirely

The C fast-fail dispatcher tracks per-file mtimes (clichec.c:cache_is_fresh)
and exits 64 on drift; the shell wrapper then falls through to the Python
launcher, which rescans and rewrites the cache. For brand-new files that
clichec doesn't know about yet, dispatch falls through on the unknown-command
path. Either way the user-observable contract is the same: the new function
is reachable on the very next invocation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


PKG_NAME = "cliche_freshness_pkg"
BINARY = "nc_freshness_bin"

_INITIAL_CLI_SRC = (
    "from cliche import cli\n"
    "\n"
    "@cli\n"
    "def hello():\n"
    '    """Greeter."""\n'
    '    return {"v": 1}\n'
)


@pytest.fixture(scope="module")
def freshness_env(tmp_path_factory):
    """Install a one-function package once; tests mutate it in place."""
    work = tmp_path_factory.mktemp("freshness")
    pkg_dir = work / PKG_NAME
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "cli.py").write_text(_INITIAL_CLI_SRC)

    install = subprocess.run(
        [sys.executable, "-m", "cliche.install", "install",
         BINARY, "-d", str(pkg_dir),
         "--no-autocomplete", "--force"],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        pytest.fail(
            f"install failed ({install.returncode}):\n"
            f"stdout: {install.stdout}\nstderr: {install.stderr}"
        )

    try:
        yield BINARY, pkg_dir
    finally:
        subprocess.run(
            [sys.executable, "-m", "cliche.install", "uninstall", BINARY],
            capture_output=True, text=True,
        )


def _bump_mtime(path: Path) -> None:
    """Force mtime forward by one second. The C freshness check uses a 1ms
    tolerance, but on a fast filesystem a write that follows immediately
    after the cache build can land within that window — make the drift
    unambiguous so the test isn't subtly flaky."""
    now = time.time() + 1
    os.utime(path, (now, now))


def _run(binary: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([binary, *args], capture_output=True, text=True)


def test_freshness_baseline(freshness_env):
    """Pre-mutation: hello() returns v:1 and lives at the package root."""
    binary, _ = freshness_env
    r = _run(binary, "hello")
    assert r.returncode == 0, f"baseline hello failed:\n{r.stderr}"
    assert json.loads(r.stdout) == {"v": 1}


def test_freshness_add_function_to_existing_file(freshness_env):
    """Appending a @cli to cli.py must invalidate the cache via mtime drift."""
    binary, pkg_dir = freshness_env
    cli_py = pkg_dir / "cli.py"
    cli_py.write_text(
        cli_py.read_text()
        + "\n@cli\n"
          "def goodbye():\n"
          '    """Farewell."""\n'
          '    return {"v": 2}\n'
    )
    _bump_mtime(cli_py)

    r = _run(binary, "goodbye")
    assert r.returncode == 0, f"goodbye not discovered:\n{r.stderr}"
    assert json.loads(r.stdout) == {"v": 2}

    r = _run(binary, "hello")
    assert r.returncode == 0, f"hello regressed after edit:\n{r.stderr}"
    assert json.loads(r.stdout) == {"v": 1}


def test_freshness_add_new_file_in_same_dir(freshness_env):
    """A brand-new .py next to cli.py must be picked up. clichec's freshness
    check only knows about files already in the cache, so this exercises the
    unknown-command fall-through into the Python rescan path."""
    binary, pkg_dir = freshness_env
    (pkg_dir / "extra.py").write_text(
        "from cliche import cli\n"
        "\n"
        "@cli\n"
        "def extra_cmd():\n"
        '    """Added in a brand-new file."""\n'
        '    return {"v": 3}\n'
    )

    r = _run(binary, "extra_cmd")
    assert r.returncode == 0, f"extra_cmd not discovered:\n{r.stderr}"
    assert json.loads(r.stdout) == {"v": 3}


def test_freshness_add_subpackage(freshness_env):
    """A new subdirectory with __init__.py + a @cli-bearing module must be
    picked up the same way — the rescan walks the whole package tree."""
    binary, pkg_dir = freshness_env
    sub_dir = pkg_dir / "sub"
    sub_dir.mkdir()
    (sub_dir / "__init__.py").write_text("")
    (sub_dir / "mod.py").write_text(
        "from cliche import cli\n"
        "\n"
        "@cli\n"
        "def deep_cmd():\n"
        '    """Inside a brand-new subpackage."""\n'
        '    return {"v": 4}\n'
    )

    r = _run(binary, "deep_cmd")
    assert r.returncode == 0, f"deep_cmd not discovered:\n{r.stderr}"
    assert json.loads(r.stdout) == {"v": 4}
