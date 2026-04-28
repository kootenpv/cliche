"""Parity tests: clichec output must match the Python launcher byte-for-byte
(modulo ANSI colour and trailing whitespace) on every fast-fail surface it
serves.

The point isn't to reverify what Python does — it's a regression detector.
When run.py's help text changes, this test fails until the C launcher
catches up. When clichec drifts, same thing in the other direction.

Surface covered (must match byte-for-byte):
  - top-level `--help` / `-h` / bare invocation
  - unknown top-level command (stderr message)
  - `<cmd> --llm-help`
  - `<group> <cmd> --llm-help`
  - shell completion (set-equal candidate lists)

Content-only parity (rendering format may diverge, information must not):
  - `<cmd> --help` / `<group> <cmd> --help` — argparse owns formatting,
    so we only assert the same param names / defaults / choices appear.

Surface deliberately excluded:
  - top-level `--llm-help`     — clichec defers to Python (env-info snapshot
                                  with Python interpreter / pip / autocomplete
                                  state is awkward to reproduce in C).
  - signatures referencing pydantic — clichec defers (no field schema in cache).
  - real dispatch                   — clichec always defers.

## Speed

This module pre-runs every (Python, clichec) pair concurrently in one
session-scoped fixture (mirroring conftest.py:cli_results), so the per-test
overhead is a dict lookup instead of two subprocess spawns. Adding a new
case to PARITY_ARGV / CONTENT_PARITY_ARGV / COMPLETION_CASES costs the
fixture one extra subprocess each (parallelised), not one per test * 2.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# The fixture package's import name (the binary name comes from conftest's
# BINARY_NAME — we read it via the cli_binary fixture).
PKG_NAME = "cliche_test"

# argv lists with predictable, parity-friendly output. Each entry is
# (test_id, argv) — argv is what gets appended to the binary name. The
# test_id is also used to skip Python paths in single-test runs via -k.
PARITY_ARGV = [
    # top-level help variants
    ("top_help_long",      ["--help"]),
    ("top_help_short",     ["-h"]),
    ("top_bare",           []),
    # unknown command — stderr message must match exactly. Two flavours:
    #   - far-from-everything: no suggestion line emitted
    #   - close-to-something:  Levenshtein finds a near match, both sides
    #     emit the same `Did you mean: <prog> [group] <name>?` line
    ("unknown_cmd_dashed",        ["definitely-not-a-command"]),
    ("unknown_cmd_under",         ["definitely_not_a_command"]),
    ("unknown_cmd_close_top",     ["echo-dat"]),       # → echo-date
    ("unknown_cmd_close_bool",    ["with-cach"]),      # → with-cache
    ("unknown_cmd_close_grouped", ["mall"]),           # close to math/add
    # per-command llm-help (covers primitives, defaults, bool flags, groups)
    ("cmd_llm_help_date",         ["echo-date", "--llm-help"]),
    ("cmd_llm_help_with_verbose", ["with-verbose", "--llm-help"]),
    ("cmd_llm_help_with_cache",   ["with-cache", "--llm-help"]),
    ("cmd_llm_help_int_list",     ["echo-int-list", "--llm-help"]),
    ("cmd_llm_help_enum_default", ["echo-enum-default", "--llm-help"]),
    ("subcmd_llm_help_add",       ["math", "add", "--llm-help"]),
    ("subcmd_llm_help_mul",       ["math", "mul", "--llm-help"]),
]

# Each entry's `must_contain` is the lowest-common-denominator token set —
# things both renders MUST surface. Casing-sensitive tokens that argparse
# spells differently from clichec (e.g. metavar `A` vs argparse's `a`) are
# excluded; we test the *information*, not the formatting.
CONTENT_PARITY_ARGV = [
    ("cmd_help_date",         ["echo-date", "--help"],    {"date"}),
    ("cmd_help_dict",         ["echo-dict", "--help"],    {"--tags"}),
    ("cmd_help_with_cache",   ["with-cache", "--help"],   {"--no-use-cache", "--name", "Default"}),
    ("cmd_help_with_verbose", ["with-verbose", "--help"], {"--verbose"}),
    ("cmd_help_enum_default", ["echo-enum-default", "--help"],
                              {"--color", "RED", "GREEN", "BLUE", "Color", "Default"}),
    ("subcmd_help_add",       ["math", "add", "--help"],  {"int"}),
]

COMPLETION_CASES = [
    # Tab completion fires on every keystroke; this matrix asserts that the
    # candidate sets match Python's argcomplete output for the same partial
    # command line. Each entry is (test_id, comp_line) — comp_point defaults
    # to len(comp_line) since we always test "completing at the end".
    # `<bin>` placeholder is substituted at run-time with the actual binary name.
    ("complete_top",            "<bin> "),
    ("complete_top_prefix",     "<bin> ec"),                       # echo-* prefix
    ("complete_subcmd",         "<bin> math "),
    ("complete_subcmd_prefix",  "<bin> math a"),
    ("complete_pos_enum",       "<bin> echo-enum "),               # positional Color
    ("complete_pos_enum_prefix","<bin> echo-enum-default --color R"),
    ("complete_flag_names",     "<bin> with-cache --"),            # flag names
    ("complete_flag_value",     "<bin> echo-enum-default --color "),
    ("complete_subcmd_flag",    "<bin> math add --"),
]


# ---------------------------------------------------------------------------
# Session fixtures: build clichec, prime the cache, pre-run every parity case.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def clichec_binary():
    """Build clichec once per session. Skip the whole module if no compiler."""
    from cliche._clichec import ensure_built
    p = ensure_built(verbose=False)
    if not p:
        pytest.skip("clichec could not be built (no C compiler present)")
    return str(p)


@pytest.fixture(scope="session")
def primed_cache(cli_binary):
    """Run the binary once so the cache file exists, then return its path.

    clichec is invoked directly (not through the wrapper) — the test owns
    the `cache_path` argv. We locate the cache file by globbing the
    XDG_CACHE_HOME tree for the freshest match on the package name.
    """
    subprocess.run(
        [cli_binary, "--llm-help"], capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or
                      os.path.expanduser("~/.cache"))
    candidates = sorted((cache_home / "cliche").glob(f"{PKG_NAME}_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    assert candidates, (
        f"no cache file found at {cache_home}/cliche/{PKG_NAME}_*.json — "
        f"did the binary fail to run?"
    )
    return str(candidates[0])


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _normalize(s: str) -> str:
    """Strip ANSI colour and trim trailing whitespace per line.

    Trailing whitespace is normalised because argparse occasionally emits
    spaces at line ends that are invisible to humans but break exact-match
    comparison. Colour stripping is belt-and-braces — we already pass
    NO_COLOR=1 into both invocations, but a bug in either renderer that
    emitted unconditional escapes would otherwise be invisible here.
    """
    s = _ANSI_RE.sub("", s)
    return "\n".join(line.rstrip() for line in s.splitlines())


import sys


def _run_python(cli_binary: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Invoke the canonical Python launcher directly, bypassing whatever
    shim is currently installed on disk.

    Auto-apply (cliche/launcher.py:_maybe_self_upgrade_shim) replaces the
    pip-generated Python shim with the fast-shim wrapper on first run, so
    invoking `cli_binary` directly would route through clichec — defeating
    the parity test's whole point of comparing the two renderers. We always
    spawn `python -c` with the launcher import so the Python path is the
    Python path, regardless of what's at the binary's filesystem location.
    """
    bin_name = os.path.basename(cli_binary)
    code = (
        f"import sys; sys.argv[0] = {bin_name!r}; "
        f"from cliche.launcher import launch_{PKG_NAME}; "
        f"launch_{PKG_NAME}()"
    )
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )


def _run_clichec(clichec: str, cache_path: str, prog_name: str,
                 argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [clichec, cache_path, PKG_NAME, *argv],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1", "CLICHEC_PROG": prog_name},
    )


def _run_complete(binary: str, comp_line: str) -> str:
    """Argcomplete-style completion via the canonical Python launcher.

    Same reasoning as _run_python: auto-apply replaces the on-disk shim with
    a fast-shim, so invoking the binary directly would route through clichec
    and the test would be measuring C-vs-C. We invoke the Python launcher
    explicitly via `python -c` to keep the Python leg always Python.
    """
    bin_name = os.path.basename(binary)
    code = (
        f"import sys; sys.argv[0] = {bin_name!r}; "
        f"from cliche.launcher import launch_{PKG_NAME}; "
        f"launch_{PKG_NAME}()"
    )
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
    tmp.close()
    try:
        env = {**os.environ,
               "_ARGCOMPLETE": "1",
               "_ARGCOMPLETE_IFS": "\n",
               "_ARGCOMPLETE_STDOUT_FILENAME": tmp.name,
               "COMP_LINE": comp_line,
               "COMP_POINT": str(len(comp_line)),
               "NO_COLOR": "1"}
        subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=5)
        with open(tmp.name) as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _run_complete_clichec(clichec: str, cache_path: str, prog_name: str,
                          comp_line: str) -> str:
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
    tmp.close()
    try:
        env = {**os.environ,
               "_ARGCOMPLETE": "1",
               "_ARGCOMPLETE_IFS": "\n",
               "_ARGCOMPLETE_STDOUT_FILENAME": tmp.name,
               "COMP_LINE": comp_line,
               "COMP_POINT": str(len(comp_line)),
               "NO_COLOR": "1",
               "CLICHEC_PROG": prog_name}
        subprocess.run([clichec, cache_path, PKG_NAME], env=env,
                       capture_output=True, text=True, timeout=5)
        with open(tmp.name) as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@pytest.fixture(scope="session")
def parity_results(cli_binary, clichec_binary, primed_cache):
    """Pre-run every parity invocation concurrently.

    Each test then looks up its (kind, test_id) result instead of paying for
    two subprocess spawns at test time. Same shape as conftest.py:cli_results.
    Cuts module wall-clock from ~2s to ~0.5s while keeping one pytest test
    per behaviour (so `pytest -k <name>` still selects single cases).

    Result map keys:
      ("py",        test_id) → CompletedProcess from running the binary
      ("c",         test_id) → CompletedProcess from running clichec
      ("py_comp",   test_id) → completion candidates string (Python)
      ("c_comp",    test_id) → completion candidates string (clichec)
    """
    jobs: list[tuple[tuple[str, str], callable]] = []

    for tid, argv in PARITY_ARGV:
        jobs.append((("py", tid), lambda av=argv: _run_python(cli_binary, av)))
        jobs.append((("c",  tid), lambda av=argv: _run_clichec(
            clichec_binary, primed_cache, cli_binary, av)))

    for tid, argv, _toks in CONTENT_PARITY_ARGV:
        jobs.append((("py", tid), lambda av=argv: _run_python(cli_binary, av)))
        jobs.append((("c",  tid), lambda av=argv: _run_clichec(
            clichec_binary, primed_cache, cli_binary, av)))

    for tid, comp_line in COMPLETION_CASES:
        line = comp_line.replace("<bin>", cli_binary)
        jobs.append((("py_comp", tid), lambda ln=line: _run_complete(
            cli_binary, ln)))
        jobs.append((("c_comp",  tid), lambda ln=line: _run_complete_clichec(
            clichec_binary, primed_cache, cli_binary, ln)))

    def _exec(item):
        key, fn = item
        return key, fn()

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(_exec, jobs))


# ---------------------------------------------------------------------------
# Tests — pure dict lookups + assertions (no subprocess work here).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("test_id,argv", PARITY_ARGV, ids=[t[0] for t in PARITY_ARGV])
def test_clichec_matches_python(parity_results, test_id, argv):
    """Strict byte-parity. Drift in colour-stripped, trailing-ws-normalized
    stdout/stderr or exit code fails the test."""
    py = parity_results[("py", test_id)]
    c  = parity_results[("c",  test_id)]

    if c.returncode == 64:
        # clichec consciously deferred — that's correct behaviour for paths
        # outside its declared coverage (pydantic, etc).
        pytest.skip(f"[{test_id}] clichec deferred to Python (rc=64)")

    py_out = _normalize(py.stdout)
    c_out  = _normalize(c.stdout)
    py_err = _normalize(py.stderr)
    c_err  = _normalize(c.stderr)

    assert py.returncode == c.returncode, (
        f"[{test_id}] exit-code drift: python={py.returncode} clichec={c.returncode}\n"
        f"--- python stderr ---\n{py.stderr}\n--- clichec stderr ---\n{c.stderr}"
    )
    assert py_out == c_out, (
        f"[{test_id}] stdout drift\n"
        f"--- python ---\n{py_out}\n"
        f"--- clichec ---\n{c_out}"
    )
    assert py_err == c_err, (
        f"[{test_id}] stderr drift\n"
        f"--- python ---\n{py_err}\n"
        f"--- clichec ---\n{c_err}"
    )


@pytest.mark.parametrize("test_id,argv,must_contain",
                         CONTENT_PARITY_ARGV,
                         ids=[t[0] for t in CONTENT_PARITY_ARGV])
def test_clichec_command_help_content(parity_results, test_id, argv, must_contain):
    """`<cmd> --help` is rendered by clichec without copying argparse's exact
    formatting. We only assert that:
      - exit code matches Python's
      - both sides surface every required token (param names, choice values,
        the literal `Default`, etc) — the help carries the same information

    Add a new fixture to CONTENT_PARITY_ARGV when you add a feature whose
    help-output rendering you want to lock in.
    """
    py = parity_results[("py", test_id)]
    c  = parity_results[("c",  test_id)]

    if c.returncode == 64:
        pytest.skip(f"[{test_id}] clichec deferred to Python (rc=64)")

    assert py.returncode == c.returncode == 0, (
        f"[{test_id}] non-zero exit: python={py.returncode} clichec={c.returncode}\n"
        f"py_err={py.stderr}\nc_err={c.stderr}"
    )
    py_out = _normalize(py.stdout)
    c_out  = _normalize(c.stdout)
    missing_py = [t for t in must_contain if t not in py_out]
    missing_c  = [t for t in must_contain if t not in c_out]
    assert not missing_py, f"[{test_id}] python missing tokens: {missing_py}\n{py_out}"
    assert not missing_c,  f"[{test_id}] clichec missing tokens: {missing_c}\n{c_out}"


@pytest.mark.parametrize("test_id,comp_line", COMPLETION_CASES,
                         ids=[c[0] for c in COMPLETION_CASES])
def test_clichec_completion_matches_python(parity_results, test_id, comp_line):
    """Both renderers must produce the same candidate set (set-equal, not
    necessarily same order — argcomplete + bash sort downstream anyway).

    Mismatch here usually means one side knows about a param shape the other
    doesn't (new annotation type, lazy_arg, etc.). The test makes drift
    obvious so we don't ship a CLI where `<bin> mds <tab>` completes nothing
    but `<bin>` itself completes correctly.
    """
    py = parity_results[("py_comp", test_id)]
    c  = parity_results[("c_comp",  test_id)]
    py_set = set(s for s in py.split("\n") if s)
    c_set  = set(s for s in c.split("\n") if s)
    if py_set != c_set:
        only_py = py_set - c_set
        only_c  = c_set - py_set
        pytest.fail(
            f"[{test_id}] completion drift\n"
            f"  only-in-python ({len(only_py)}): {sorted(only_py)[:10]}\n"
            f"  only-in-clichec ({len(only_c)}): {sorted(only_c)[:10]}\n"
        )


def test_auto_apply_on_cliche_install(tmp_path_factory, clichec_binary):
    """End-to-end: `cliche install <name>` writes a fast-shim wrapper, and
    a subsequent `pip install -e .` overwrite is auto-healed on first run.

    Covers two real correctness concerns the parity tests can't (they bypass
    the wrapper):
      1. cliche install's auto-apply branch (install.py end-of-install) is
         actually wired up.
      2. cliche/launcher._maybe_self_upgrade_shim recovers the fast-shim
         after pip resets it.

    Skip when no compiler — fast-shim never gets installed in that
    environment regardless, and the auto-apply branch is correctly a no-op
    (so there's nothing to assert about).
    """
    import shutil
    import sys
    from cliche._clichec import is_fast_shim, WRAPPER_MARKER

    work = tmp_path_factory.mktemp("autoapply")
    pkg_dir = work / "ap_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(
        "from cliche import cli\n"
        "@cli\n"
        "def hello(name: str = 'world'):\n"
        "    print(f'hi {name}')\n"
    )
    bin_name = "ap_test_bin"

    install_cmd = [
        sys.executable, "-m", "cliche.install", "install", bin_name,
        "-d", str(pkg_dir), "--no-autocomplete", "--force",
    ]
    r = subprocess.run(install_cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"install failed: {r.stdout}\n{r.stderr}"

    try:
        target = shutil.which(bin_name)
        assert target, f"{bin_name} not on PATH after install"

        # 1. cliche install auto-apply: shim should already be a fast-shim.
        assert is_fast_shim(target), (
            f"cliche install did NOT auto-apply the fast-shim wrapper at {target}\n"
            f"head:\n{Path(target).read_text()[:300]}"
        )

        # 2. Self-healing: simulate pip overwriting the shim with its stock
        # Python script, then run the binary once and verify auto-upgrade.
        canonical_python_shim = (
            f"#!{sys.executable}\n"
            "# -*- coding: utf-8 -*-\n"
            "import re, sys\n"
            "from cliche.launcher import launch_ap_pkg\n"
            "if __name__ == \"__main__\":\n"
            "    sys.argv[0] = re.sub(r\"(-script\\.pyw|\\.exe)?$\", \"\", sys.argv[0])\n"
            "    sys.exit(launch_ap_pkg())\n"
        )
        Path(target).write_text(canonical_python_shim)
        Path(target).chmod(0o755)
        assert WRAPPER_MARKER not in Path(target).read_text(), (
            "test sanity: stock shim still has fast-shim marker"
        )

        # First run goes through Python; on the way out the launcher should
        # have rewritten the shim back to a fast-shim wrapper.
        r2 = subprocess.run([target, "--help"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1"})
        assert r2.returncode == 0, f"binary failed: {r2.stderr}"
        assert is_fast_shim(target), (
            "self-upgrade did NOT rewrite the Python shim back to a fast-shim "
            f"after a single invocation\nhead:\n{Path(target).read_text()[:300]}"
        )
    finally:
        # Clean up. uninstall handles both the entry point and the shim file.
        subprocess.run(
            [sys.executable, "-m", "cliche.install", "uninstall", bin_name,
             "-d", str(pkg_dir)],
            capture_output=True, text=True,
        )


def test_wrapper_falls_through_on_unexpected_exit(tmp_path, clichec_binary):
    """The fast-shim wrapper must trust ONLY rc=0 (success) and rc=1 (handled
    error like unknown-command). Every other code — `64` (defer),
    `139` (SIGSEGV), `137` (SIGKILL), `127` (clichec missing) — has to fall
    through to the Python launcher so a misbehaving clichec never bricks the
    binary.

    Constructed test: render the wrapper template, point it at a fake clichec
    that exits with whatever rc we choose, and a fake "Python" that just
    prints PYTHON_RAN. The wrapper's case statement decides which one we see.
    """
    import sys
    import stat
    from cliche._clichec import render_wrapper

    fake_clichec = tmp_path / "fake_clichec"
    fake_python  = tmp_path / "fake_python"
    cache_file   = tmp_path / "fake_cache.json"
    cache_file.write_text("{}")  # any nonempty file; existence is what the wrapper checks

    # Stand-in for `python -c '...launch_pkg...'`. The wrapper invokes us as
    # "$PYTHON" -c '...'  — we ignore the -c argument and just signal that
    # the fall-through ran. Exit 0 so the wrapper itself returns 0.
    fake_python.write_text("#!/bin/sh\necho PYTHON_RAN\nexit 0\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)

    def make_wrapper(rc: int) -> Path:
        # `fake_clichec` exits with `rc` and prints CLICHEC_RAN_<rc> to
        # stdout so we can tell which side answered.
        fake_clichec.write_text(f"#!/bin/sh\necho CLICHEC_RAN_{rc}\nexit {rc}\n")
        fake_clichec.chmod(fake_clichec.stat().st_mode | stat.S_IEXEC)
        wrapper_path = tmp_path / f"wrapper_rc{rc}"
        wrapper_path.write_text(render_wrapper(
            binary_name="fakebin",
            package_name="fakepkg",
            pkg_dir=str(tmp_path),
            python_exe=str(fake_python),
            clichec=str(fake_clichec),
            cache_hash="aaaaaaaa",
        ))
        wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC)
        return wrapper_path

    # Override the cache-file path the wrapper computes so it points at our
    # stand-in (the template uses XDG_CACHE_HOME/cliche/<pkg>_<hash>.json).
    cache_dir = tmp_path / "cliche"
    cache_dir.mkdir()
    (cache_dir / "fakepkg_aaaaaaaa.json").write_text("{}")
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path)}

    cases = [
        # (clichec rc, expected wrapper rc, side that should answer)
        (0,    0, "CLICHEC"),  # clichec handled it, success
        (1,    1, "CLICHEC"),  # clichec handled it, error (e.g. unknown-cmd)
        (64,   0, "PYTHON"),   # explicit defer — expected fall-through
        (139,  0, "PYTHON"),   # SIGSEGV-like exit — must NOT brick binary
        (137,  0, "PYTHON"),   # SIGKILL/OOM — same
        (127,  0, "PYTHON"),   # not-found-ish — same
    ]
    for rc_in, rc_expected, who in cases:
        w = make_wrapper(rc_in)
        r = subprocess.run([str(w)], capture_output=True, text=True, env=env)
        assert r.returncode == rc_expected, (
            f"clichec rc={rc_in}: wrapper returned {r.returncode}, "
            f"expected {rc_expected}\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
        )
        assert who in r.stdout or who in r.stderr or f"{who}_RAN" in r.stdout, (
            f"clichec rc={rc_in}: expected {who} side to answer, "
            f"got stdout={r.stdout!r}"
        )


def test_clichec_is_faster_than_python(cli_binary, clichec_binary, primed_cache):
    """Sanity: clichec should be visibly faster on a fast-fail path.

    Not a precise timing assertion (CI noise) — just enough to catch the
    "we accidentally deleted the C path" regression where everything
    silently goes through Python and parity passes but the speed win is
    gone. Margin is generous: clichec must beat 4× the Python time.

    Deliberately NOT routed through `parity_results`: we want the timing
    to be measured fresh, not on cached subprocess output.
    """
    import time

    def _med(times):
        s = sorted(times)
        return s[len(s) // 2]

    py_times, c_times = [], []
    for _ in range(5):
        t = time.perf_counter()
        _run_python(cli_binary, ["--help"])
        py_times.append(time.perf_counter() - t)
        t = time.perf_counter()
        _run_clichec(clichec_binary, primed_cache, cli_binary, ["--help"])
        c_times.append(time.perf_counter() - t)

    py_med = _med(py_times)
    c_med  = _med(c_times)
    assert c_med * 4 < py_med, (
        f"clichec ({c_med*1000:.1f}ms) is not 4× faster than python "
        f"({py_med*1000:.1f}ms) — did the fast path break?"
    )
