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
            assert 'mybin = "mypkg._cliche:main"' in content

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
        # cwd must be a foreign dir (tempdir), not the workdir.
        assert kw.get("cwd") == tempfile.gettempdir()
        assert kw.get("cwd") != str(pkg)

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


class TestSubdirLayoutForeignCwd:
    """Real install of a subdir-layout package, then invocation from a foreign
    cwd. The commit 9b82daf bug only manifested when the binary was run from
    outside the workdir — Python's add-cwd-to-sys.path fallback masked it
    otherwise. This test intentionally runs the binary from tempdir.
    """

    BINARY_NAME = "nc_subdir_bin"  # unique so it can't collide

    @pytest.fixture(scope="class")
    def subdir_binary(self, tmp_path_factory):
        """Build a subdir-layout workdir, install, yield (binary, workdir),
        uninstall at teardown. One-shot — too expensive per test."""
        work = tmp_path_factory.mktemp("nc_subdir")
        pkg = work / "subpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text('"""Subdir package."""\n')
        (pkg / "cli.py").write_text(
            "from cliche import cli\n"
            "\n"
            "@cli\n"
            "def ping():\n"
            "    return {'pong': True}\n"
        )

        install_cmd = [
            _sys.executable, "-m", "cliche.install", "install",
            self.BINARY_NAME, "-d", str(work),
            "-p", "subpkg",  # force subdir-layout by naming a subdir that exists
            "--no-autocomplete", "--force",
        ]
        result = subprocess.run(install_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(
                f"subdir install failed ({result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        try:
            yield self.BINARY_NAME, work
        finally:
            subprocess.run(
                [_sys.executable, "-m", "cliche.install", "uninstall", self.BINARY_NAME],
                capture_output=True, text=True,
            )

    def test_binary_works_from_foreign_cwd(self, subdir_binary):
        """Run the binary from tempfile.gettempdir() — a different filesystem
        location than the workdir. The subdir-layout pyproject template must
        NOT hard-code a flat-layout `package-dir = {"subpkg" = "."}` mapping
        (which only worked when Python's cwd was the workdir)."""
        binary, work = subdir_binary
        foreign_cwd = tempfile.gettempdir()
        assert foreign_cwd != str(work), "test assumption: foreign_cwd must differ from workdir"

        result = subprocess.run(
            [binary, "ping"],
            capture_output=True, text=True, cwd=foreign_cwd,
        )
        assert result.returncode == 0, (
            f"binary failed from foreign cwd (rc={result.returncode})\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert '"pong"' in result.stdout
        assert "true" in result.stdout.lower()

    def test_pyproject_omits_flat_layout_package_dir(self, subdir_binary):
        """Paranoid check: the generated pyproject for subdir layout must not
        contain the flat-layout `package-dir` mapping. Covers the exact
        regression path — a future refactor could mistakenly reintroduce it."""
        _, work = subdir_binary
        content = (work / "pyproject.toml").read_text()
        assert 'package-dir' not in content
        assert 'packages = ["subpkg"]' in content


class TestFlatRenamedLayoutForeignCwd:
    """Flat layout where the directory name DIFFERS from the package name.
    This is the `solidsnake/` dir installed as `tovermunt` pattern: pyproject
    carries `package-dir = {"tovermunt" = "."}` so pip maps tovermunt to the
    workdir. The generated _cliche.py must still bootstrap cleanly when
    invoked directly (`python _cliche.py`), since a plain sys.path insert
    can't help — it would add the workdir's parent, and there's no
    `parent/tovermunt/` there to import.
    """

    BINARY_NAME = "nc_renamed_bin"

    @pytest.fixture(scope="class")
    def renamed_binary(self, tmp_path_factory):
        work = tmp_path_factory.mktemp("nc_renamed") / "dirname_mismatch"
        work.mkdir()
        (work / "__init__.py").write_text('"""Renamed flat package."""\n')
        (work / "cli.py").write_text(
            "from cliche import cli\n"
            "\n"
            "@cli\n"
            "def ping():\n"
            "    return {'pong': True}\n"
        )

        install_cmd = [
            _sys.executable, "-m", "cliche.install", "install",
            self.BINARY_NAME, "-d", str(work),
            "-p", "nc_renamed_pkg",
            "--no-autocomplete", "--force",
        ]
        result = subprocess.run(install_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(
                f"renamed install failed ({result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        try:
            yield self.BINARY_NAME, work
        finally:
            subprocess.run(
                [_sys.executable, "-m", "cliche.install", "uninstall", self.BINARY_NAME],
                capture_output=True, text=True,
            )

    def test_binary_works_from_foreign_cwd(self, renamed_binary):
        binary, work = renamed_binary
        result = subprocess.run(
            [binary, "ping"],
            capture_output=True, text=True, cwd=tempfile.gettempdir(),
        )
        assert result.returncode == 0, (
            f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert '"pong"' in result.stdout

    def test_direct_cliche_py_execution_from_foreign_cwd(self, renamed_binary):
        """`python _cliche.py ping` from outside the workdir must resolve
        the package under its declared name even though it's not a subdir
        of anything on the default sys.path. The generated bootstrap does
        this via `importlib.util.spec_from_file_location`."""
        _, work = renamed_binary
        cliche_py = work / "_cliche.py"
        assert cliche_py.exists()
        result = subprocess.run(
            [_sys.executable, str(cliche_py), "ping"],
            capture_output=True, text=True, cwd=tempfile.gettempdir(),
        )
        assert result.returncode == 0, (
            f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert '"pong"' in result.stdout

    def test_pyproject_has_package_dir_mapping(self, renamed_binary):
        _, work = renamed_binary
        content = (work / "pyproject.toml").read_text()
        # Flat layout + rename: mapping must be present so pip knows where pkg lives.
        assert '"nc_renamed_pkg" = "."' in content
