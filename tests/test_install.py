"""Tests for install-time layout decisions and pyproject generation.

Regressions caught via the ralph loop:
- `_update_pyproject_toml` previously hard-coded flat-layout `package-dir`
  even when `_find_package_dir` had decided subdir layout — the generated
  pyproject pointed setuptools at the wrong directory, so the installed
  binary only worked when invoked from inside the workdir (cwd-as-sys.path
  saved it). Now branches on `is_subdir_package`.
- `_find_package_dir(auto_init=True)` used to promote any matching subdir
  (even empty, e.g. a mkdir typo) to subdir layout. Now requires at least
  one .py inside the subdir before promoting.
"""
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

import sys as _sys

import cliche.install  # noqa: F401 — ensures submodule is in sys.modules
from cliche.install import (
    AUTO_INIT_MARKER,
    _find_package_dir,
    _update_pyproject_toml,
    install,
)

# `cliche.install` at the attribute level was rebound to the function by
# `cliche/__init__.py`'s `from cliche.install import install`. Grab the real
# submodule out of sys.modules for mock.patch.object targets.
install_mod = _sys.modules["cliche.install"]


class TestUpdatePyprojectTomlLayout:
    def test_flat_layout_writes_package_dir_mapping(self, tmp_path):
        _update_pyproject_toml(tmp_path, "mybin", "mypkg", is_subdir_package=False)
        content = (tmp_path / "pyproject.toml").read_text()
        # Flat layout needs package-dir so setuptools maps the pkg to cwd.
        assert '"mypkg" = "."' in content
        assert 'packages = ["mypkg"]' in content

    def test_subdir_layout_omits_package_dir_mapping(self, tmp_path):
        _update_pyproject_toml(tmp_path, "mybin", "mypkg", is_subdir_package=True)
        content = (tmp_path / "pyproject.toml").read_text()
        # Subdir layout lets setuptools auto-discover ./mypkg/.
        assert '"mypkg" = "."' not in content
        assert 'package-dir' not in content
        assert 'packages = ["mypkg"]' in content

    def test_entry_point_is_written_in_both_layouts(self, tmp_path):
        for is_subdir in (True, False):
            d = tmp_path / ("sub" if is_subdir else "flat")
            d.mkdir()
            _update_pyproject_toml(d, "mybin", "mypkg", is_subdir_package=is_subdir)
            content = (d / "pyproject.toml").read_text()
            # Entry routes through the cliche-owned launcher, not directly at
            # `{pkg}._cliche:main` — the launcher cleans sys.path before
            # resolving the user package, closing the shadow-on-invoke window.
            assert 'mybin = "cliche.launcher:launch_mypkg"' in content

    def test_existing_flat_mapping_healed_when_subdir_detected(self, tmp_path):
        """If pyproject already has flat `package-dir = {pkg = "."}` but we
        now detect subdir layout, the mapping should be rewritten."""
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\n'
            'requires = ["setuptools>=61.0"]\n'
            'build-backend = "setuptools.build_meta"\n'
            '\n'
            '[project]\n'
            'name = "mypkg"\n'
            'version = "0.1.0"\n'
            'dependencies = []\n'
            '\n'
            '[tool.setuptools]\n'
            'package-dir = {"mypkg" = "."}\n'
            'packages = ["mypkg"]\n'
        )
        _update_pyproject_toml(tmp_path, "mybin", "mypkg", is_subdir_package=True)
        content = (tmp_path / "pyproject.toml").read_text()
        assert 'package-dir' not in content
        assert 'packages = ["mypkg"]' in content


class TestFindPackageDirAutoInit:
    def test_matching_subdir_with_init_returns_subdir(self, tmp_path):
        sub = tmp_path / "mypkg"
        sub.mkdir()
        (sub / "__init__.py").write_text("")
        assert _find_package_dir(tmp_path, "mypkg") == sub

    def test_empty_subdir_does_not_promote_with_auto_init(self, tmp_path):
        """An empty subdir matching the package name must fall through to
        flat layout — promoting it would silently misplace the user's .py
        files that live at the workdir root."""
        (tmp_path / "mypkg").mkdir()
        result = _find_package_dir(tmp_path, "mypkg", auto_init=True)
        assert result == tmp_path  # flat
        # and no __init__.py was created as a side-effect
        assert not (tmp_path / "mypkg" / "__init__.py").exists()

    def test_populated_subdir_promotes_with_auto_init(self, tmp_path):
        """A subdir that contains at least one .py file is legitimate
        subdir-layout; auto_init promotes it and writes the marker."""
        sub = tmp_path / "mypkg"
        sub.mkdir()
        (sub / "ops.py").write_text("# some code\n")
        result = _find_package_dir(tmp_path, "mypkg", auto_init=True)
        assert result == sub
        init = sub / "__init__.py"
        assert init.exists()
        assert AUTO_INIT_MARKER in init.read_text()

    def test_no_subdir_returns_flat(self, tmp_path):
        assert _find_package_dir(tmp_path, "mypkg") == tmp_path


class TestToolInstallProbe:
    """The --tool install probe must (a) invoke the binary shim, not sys.executable,
    because sys.executable isn't the isolated tool venv; and (b) set cwd to a
    foreign dir so Python's add-cwd-to-sys.path can't mask layout-shadowing bugs.

    Both were real regressions: (a) commit 91953b1 — pip-mode probe ran under
    sys.executable even for --tool installs, producing false-fails. (b) commit
    9b82daf — subdir-layout bugs were masked because the probe ran from the
    workdir where cwd-on-sys.path resolved the import anyway.
    """

    def _make_pkg(self, tmp_path, name="toolpkg"):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "ops.py").write_text(
            "from cliche import cli\n"
            "@cli\n"
            "def hello():\n"
            "    return {'ok': True}\n"
        )
        return pkg

    def _install_with_mocks(self, pkg_dir, binary_name, *, tool, binary_on_path=True):
        """Run install() with subprocess.run / shutil.which / _existing_entry_point
        mocked so we don't actually invoke pip/uv or touch the user's env.
        Returns the list of subprocess.run calls made during install."""
        calls: list[tuple] = []

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

        def fake_which(name):
            if name == "uv":
                return "/usr/local/bin/uv"
            if name == binary_name:
                return f"/home/test/.local/bin/{binary_name}" if binary_on_path else None
            if name == "pip":
                return "/usr/bin/pip"
            return None

        with mock.patch.object(install_mod.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(install_mod.shutil, "which", side_effect=fake_which), \
             mock.patch.object(install_mod, "_existing_entry_point", return_value=None), \
             mock.patch.object(install_mod, "_cliche_source_dir", return_value=None):
            install(
                binary_name, module_dir=str(pkg_dir),
                package_name=pkg_dir.name, tool=tool, no_autocomplete=True,
            )
        return calls

    def test_tool_probe_uses_binary_shim_with_foreign_cwd(self, tmp_path):
        pkg = self._make_pkg(tmp_path)
        calls = self._install_with_mocks(pkg, "toolbin", tool=True)

        # Find the probe call (distinguished by the `cwd` kwarg and --help argv).
        probe_calls = [(argv, kw) for argv, kw in calls if argv[-1] == "--help"]
        assert len(probe_calls) == 1, f"expected 1 probe call, got {len(probe_calls)}: {calls}"
        argv, kw = probe_calls[0]
        # First positional must be the binary shim path (from shutil.which(name)),
        # NOT sys.executable. This is the "use the tool venv's Python" fix.
        assert argv[0] == "/home/test/.local/bin/toolbin"
        assert sys.executable not in argv
        # cwd must be a freshly-created subdir of the OS tempdir, not the
        # workdir and not the bare tempdir itself. Using a fresh subdir
        # closes a second class of false-positive: if a stray `{pkg}.py`
        # happens to sit in /tmp (common during local dev and CI), a probe
        # run from /tmp directly would see the shadow and fail a good
        # install.
        probe_cwd = kw.get("cwd")
        assert probe_cwd != str(pkg)
        assert probe_cwd != tempfile.gettempdir()
        assert probe_cwd.startswith(tempfile.gettempdir())

    def test_pip_mode_probe_uses_sys_executable_not_binary_shim(self, tmp_path):
        pkg = self._make_pkg(tmp_path, name="pippkg")
        calls = self._install_with_mocks(pkg, "pipbin", tool=False)

        # Pip-mode probe uses `python -c '...import pkg...'`, not the binary.
        probe_calls = [(argv, kw) for argv, kw in calls
                       if len(argv) >= 2 and argv[0] == sys.executable and argv[1] == "-c"]
        assert len(probe_calls) == 1, f"expected 1 sys.executable -c probe, got: {calls}"
        argv, _ = probe_calls[0]
        # The probe script must import the package — catches namespace-package
        # shadowing where `_m.__file__` ends up None.
        assert "import pippkg" in argv[2]
        assert "__file__" in argv[2]

    def test_tool_install_errors_when_binary_missing_from_path(self, tmp_path):
        """uv tool install succeeded but shutil.which(name) returns None —
        must error with a clear message instead of silently skipping the probe."""
        pkg = self._make_pkg(tmp_path, name="gonepkg")
        with pytest.raises(SystemExit):
            self._install_with_mocks(pkg, "gonebin", tool=True, binary_on_path=False)


# ---------------------------------------------------------------------------
# Session-scoped batching for the real-install tests.
#
# Both layout-variant test classes below need a full `cliche install` +
# `binary ping` + `cliche uninstall` cycle. Done per-class (the old shape),
# each install costs ~450 ms and they run serially — dominating wall time
# for this file. One shared session fixture runs both installs + both
# probes concurrently, cutting wall time roughly in half. Same pattern as
# `cli_results` in conftest.py / test_e2e.py.
#
# Tests then index into the pre-computed dict. Keeping two test classes
# preserves `pytest -k` filtering by behaviour.
# ---------------------------------------------------------------------------

_PING_CLI_SRC = (
    "from cliche import cli\n"
    "\n"
    "@cli\n"
    "def ping():\n"
    "    return {'pong': True}\n"
)

_LAYOUT_SPECS = {
    "subdir": {
        "binary": "nc_subdir_bin",
        "pkg": "subpkg",
        "layout": "subdir",  # workdir/<pkg>/{__init__,cli}.py
    },
    "renamed_flat": {
        "binary": "nc_renamed_bin",
        "pkg": "nc_renamed_pkg",
        "layout": "renamed_flat",  # flat, workdir basename ≠ pkg name
    },
}


@pytest.fixture(scope="session")
def layout_installs(tmp_path_factory):
    """Install each layout fixture package and pre-run its foreign-cwd probe.

    The key speedup: `cliche install --no-pip` is run per-workdir (cheap —
    just file generation and layout detection), then ONE `pip install -e .`
    invocation installs both workdirs in a single call. That amortises pip's
    startup (the dominant cost per install) across the two packages, the
    same way test_mypy batches every probe into one mypy process and
    test_e2e batches every argv into one session fixture.

    Returns `{variant: {spec, work, install, probe}}`. Teardown uninstalls
    in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _make_workdir(name: str, spec: dict):
        if spec["layout"] == "subdir":
            work = tmp_path_factory.mktemp(f"inst_{name}")
            inner = work / spec["pkg"]
            inner.mkdir()
            (inner / "__init__.py").write_text('"""Subdir package."""\n')
            (inner / "cli.py").write_text(_PING_CLI_SRC)
        else:  # renamed_flat
            work = tmp_path_factory.mktemp(f"inst_{name}") / "dirname_mismatch"
            work.mkdir()
            (work / "__init__.py").write_text('"""Renamed flat package."""\n')
            (work / "cli.py").write_text(_PING_CLI_SRC)
        return work

    # Phase 1: generate pyproject/__init__ for every variant (no pip yet).
    workdirs: dict = {}
    file_gen_results: dict = {}
    for name, spec in _LAYOUT_SPECS.items():
        work = _make_workdir(name, spec)
        workdirs[name] = work
        file_gen_results[name] = subprocess.run(
            [_sys.executable, "-m", "cliche.install", "install",
             spec["binary"], "-d", str(work), "-p", spec["pkg"],
             "--no-autocomplete", "--force", "--no-pip"],
            capture_output=True, text=True,
        )
        if file_gen_results[name].returncode != 0:
            pytest.fail(
                f"{name} pyproject generation failed "
                f"({file_gen_results[name].returncode}):\n"
                f"stdout: {file_gen_results[name].stdout}\n"
                f"stderr: {file_gen_results[name].stderr}"
            )

    # Phase 2: ONE pip install -e workA -e workB. This is the win vs. two
    # sequential `cliche install` calls — pip boots once, resolves
    # dependencies once, and installs both packages together.
    uv_path = shutil.which("uv")
    if uv_path:
        pip_cmd = [uv_path, "pip", "install", "--python", _sys.executable]
    else:
        pip_cmd = [_sys.executable, "-m", "pip", "install"]
    for work in workdirs.values():
        pip_cmd += ["-e", str(work)]
    pip_result = subprocess.run(pip_cmd, capture_output=True, text=True)
    if pip_result.returncode != 0:
        pytest.fail(
            f"batched editable install failed ({pip_result.returncode}):\n"
            f"stdout: {pip_result.stdout}\nstderr: {pip_result.stderr}"
        )

    # Phase 3: pre-run every probe concurrently — these are quick subprocesses
    # with no shared resource contention, so a thread pool is pure win.
    def _probe(name: str):
        spec = _LAYOUT_SPECS[name]
        return name, subprocess.run(
            [spec["binary"], "ping"],
            capture_output=True, text=True, cwd=tempfile.gettempdir(),
        )

    with ThreadPoolExecutor(max_workers=len(_LAYOUT_SPECS)) as pool:
        probes = dict(pool.map(_probe, _LAYOUT_SPECS.keys()))

    results = {
        name: {
            "spec": _LAYOUT_SPECS[name],
            "work": workdirs[name],
            "install": file_gen_results[name],  # kept for parity; non-fatal info only
            "probe": probes[name],
        }
        for name in _LAYOUT_SPECS
    }

    try:
        yield results
    finally:
        def _uninstall(name: str):
            subprocess.run(
                [_sys.executable, "-m", "cliche.install", "uninstall",
                 _LAYOUT_SPECS[name]["binary"]],
                capture_output=True, text=True,
            )
        with ThreadPoolExecutor(max_workers=len(_LAYOUT_SPECS)) as pool:
            list(pool.map(_uninstall, _LAYOUT_SPECS.keys()))


class TestSubdirLayoutForeignCwd:
    """Real install of a subdir-layout package, then invocation from a foreign
    cwd. The commit 9b82daf bug only manifested when the binary was run from
    outside the workdir — Python's add-cwd-to-sys.path fallback masked it
    otherwise. This test intentionally runs the binary from tempdir.
    """

    def test_binary_works_from_foreign_cwd(self, layout_installs):
        """Run the binary from tempfile.gettempdir() — a different filesystem
        location than the workdir. The subdir-layout pyproject template must
        NOT hard-code a flat-layout `package-dir = {"subpkg" = "."}` mapping
        (which only worked when Python's cwd was the workdir)."""
        r = layout_installs["subdir"]
        foreign_cwd = tempfile.gettempdir()
        assert foreign_cwd != str(r["work"]), "test assumption: foreign_cwd must differ from workdir"
        probe = r["probe"]
        assert probe.returncode == 0, (
            f"binary failed from foreign cwd (rc={probe.returncode})\n"
            f"stdout: {probe.stdout}\nstderr: {probe.stderr}"
        )
        assert '"pong"' in probe.stdout
        assert "true" in probe.stdout.lower()

    def test_pyproject_omits_flat_layout_package_dir(self, layout_installs):
        """Paranoid check: the generated pyproject for subdir layout must not
        contain the flat-layout `package-dir` mapping. Covers the exact
        regression path — a future refactor could mistakenly reintroduce it."""
        content = (layout_installs["subdir"]["work"] / "pyproject.toml").read_text()
        assert 'package-dir' not in content
        assert 'packages = ["subpkg"]' in content


class TestFlatRenamedLayoutForeignCwd:
    """Flat layout where the directory name DIFFERS from the package name.
    This is the `solidsnake/` dir installed as `tovermunt` pattern: pyproject
    carries `package-dir = {"tovermunt" = "."}` so pip maps tovermunt to the
    workdir. The binary must still resolve the package under its declared
    name when invoked from a foreign cwd — the launcher cleans sys.path and
    then `cliche.runtime.run_package_cli` imports the package via the
    editable-install wiring.
    """

    def test_binary_works_from_foreign_cwd(self, layout_installs):
        probe = layout_installs["renamed_flat"]["probe"]
        assert probe.returncode == 0, (
            f"rc={probe.returncode} stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )
        assert '"pong"' in probe.stdout

    def test_no_cliche_py_written_to_user_package(self, layout_installs):
        """Cliche does not leave a `_cliche.py` trampoline inside the user
        package any more — the shim now routes through `cliche.launcher`
        and calls `run_package_cli` directly. The user's install dir
        should contain their own code plus `__init__.py` / `pyproject.toml`
        and nothing else cliche-authored."""
        work = layout_installs["renamed_flat"]["work"]
        assert not (work / "_cliche.py").exists()

    def test_pyproject_has_package_dir_mapping(self, layout_installs):
        work = layout_installs["renamed_flat"]["work"]
        content = (work / "pyproject.toml").read_text()
        # Flat layout + rename: mapping must be present so pip knows where pkg lives.
        assert '"nc_renamed_pkg" = "."' in content


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


@pytest.fixture(scope="session")
def dispatch_installs(tmp_path_factory):
    """Session-scoped batched install for TestSingleCommandDispatch.

    Mirrors `layout_installs`'s two-phase pattern: file-gen each variant via
    `cliche.install --no-pip`, then ONE editable pip install for both. Saves
    ~500 ms per test vs. each test running its own install + uninstall.
    """
    def _file_gen(name, src, binary):
        work = tmp_path_factory.mktemp(f"scd_{name}")
        (work / "__init__.py").write_text('"""scd test pkg."""\n')
        (work / "cli.py").write_text(src)
        result = subprocess.run(
            [_sys.executable, "-m", "cliche.install", "install",
             binary, "-d", str(work), "--no-autocomplete", "--force", "--no-pip"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"{name} file-gen failed ({result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return work

    single_work = _file_gen("single", _SCD_SINGLE_SRC, _SCD_SINGLE_BINARY)
    multi_work = _file_gen("multi", _SCD_MULTI_SRC, _SCD_MULTI_BINARY)

    uv_path = shutil.which("uv")
    if uv_path:
        pip_cmd = [uv_path, "pip", "install", "--python", _sys.executable]
    else:
        pip_cmd = [_sys.executable, "-m", "pip", "install"]
    pip_cmd += ["-e", str(single_work), "-e", str(multi_work)]
    pip_result = subprocess.run(pip_cmd, capture_output=True, text=True)
    if pip_result.returncode != 0:
        pytest.fail(
            f"batched dispatch install failed ({pip_result.returncode}):\n"
            f"stdout: {pip_result.stdout}\nstderr: {pip_result.stderr}"
        )

    try:
        yield {"single": _SCD_SINGLE_BINARY, "multi": _SCD_MULTI_BINARY}
    finally:
        for binary in (_SCD_SINGLE_BINARY, _SCD_MULTI_BINARY):
            subprocess.run(
                [_sys.executable, "-m", "cliche.install", "uninstall", binary],
                capture_output=True, text=True,
            )


class TestSingleCommandDispatch:
    """Regression: when a CLI has exactly ONE `@cli` function and that
    function's CLI-normalized name equals the binary name (e.g. binary
    `csv_stats` with function `csv_stats`), invoking `<binary> <args>` must
    dispatch directly to the function — without requiring the user to repeat
    the binary name as a subcommand.

    Found via ralph (freeform_csv_stats task, 2026-04-25): qwen wrote a
    natural single-function CLI and got `Unknown command: <first arg>` when
    the first positional was being parsed as a subcommand selector instead of
    as the function's first argument. Patched in run.py:main() to detect the
    single-command case before falling through to the unknown-command branch.

    Multi-command CLIs are deliberately NOT affected — this dispatch only
    triggers when there is exactly one ungrouped @cli function and its name
    matches the binary.
    """

    def test_single_command_invocation_without_subcommand(self, dispatch_installs):
        """`<binary> alice` must dispatch to the @cli function — not error
        with 'Unknown command: alice' as it did before the patch."""
        binary = dispatch_installs["single"]
        # Direct invocation: positional first, no subcommand.
        r = subprocess.run([binary, "alice"], capture_output=True, text=True)
        assert r.returncode == 0, (
            f"direct invocation failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.stdout.strip() == "hi alice"

        # With a flag.
        r = subprocess.run([binary, "bob", "--times", "2"],
                           capture_output=True, text=True)
        assert r.returncode == 0, (
            f"flagged invocation failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.stdout.strip().splitlines() == ["hi bob", "hi bob"]

    def test_multi_command_still_requires_explicit_subcommand(self, dispatch_installs):
        """Sanity guard: the patch must NOT trigger for multi-command CLIs.
        With two @cli functions, the user still has to name which one to run."""
        binary = dispatch_installs["multi"]
        # Calling without a subcommand should error (no implicit dispatch).
        r = subprocess.run([binary, "alice"], capture_output=True, text=True)
        assert r.returncode != 0, (
            f"multi-cmd CLI must not implicit-dispatch on 'alice' — "
            f"there are two commands. stdout={r.stdout!r}"
        )
        # Explicit form still works.
        r = subprocess.run([binary, "solo", "alice"],
                           capture_output=True, text=True)
        assert r.returncode == 0, (
            f"explicit subcommand failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert r.stdout.strip() == "hi alice"
