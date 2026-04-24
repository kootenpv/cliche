#!/usr/bin/env python3
"""
Install CLI tools from a project directory.

Usage:
    cliche install <name>           Install CLI tool with given name
    cliche uninstall <name>         Remove installed CLI tool
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


# Shell rc files and the argcomplete invocation each one expects. argcomplete's
# `register-python-argcomplete` auto-detects shell from $0 for bash/zsh but
# needs `--shell fish` for fish. Fish also uses `| source` instead of `eval`.
# `register-python-argcomplete` is on PATH at shell startup (ships with
# argcomplete); no hard-coded absolute path so the snippet survives venv moves.
_TAG = "cliche: autocomplete for"  # marker substring for cleanup

# Each line is wrapped so a broken/missing argcomplete (stale shebang, renamed
# Python, uninstalled package) can't spam the user's shell on every startup:
#   - `command -v register-python-argcomplete >/dev/null` skips the eval when
#     the binary isn't on PATH at all.
#   - `2>/dev/null` hides any startup noise from the eval itself (bad
#     interpreter, import errors) so a new shell stays clean.
# Users who want to see errors can run the command directly.
_SHELL_RC_LINES = {
    "~/.bashrc":
        'command -v register-python-argcomplete >/dev/null && '
        'eval "$(register-python-argcomplete {name} 2>/dev/null)"  # ' + _TAG + ' {name}',
    "~/.zshrc":
        'command -v register-python-argcomplete >/dev/null && '
        'eval "$(register-python-argcomplete {name} 2>/dev/null)"  # ' + _TAG + ' {name}',
    "~/.config/fish/config.fish":
        'if type -q register-python-argcomplete; '
        'register-python-argcomplete --shell fish {name} 2>/dev/null | source; '
        'end  # ' + _TAG + ' {name}',
}


def _register_autocomplete(name: str) -> list[str]:
    """Append the argcomplete hook to each shell rc that exists.

    Idempotent: skips rc files that already contain the exact line. Returns the
    list of rc paths we touched. Never creates new rc files — only appends to
    ones the user already has, so we don't clutter $HOME for shells the user
    doesn't use.
    """
    touched = []
    for rc, line_fmt in _SHELL_RC_LINES.items():
        line = line_fmt.format(name=name)
        path = Path(os.path.expanduser(rc))
        if not path.exists():
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        if line in content:
            continue
        suffix = "" if content.endswith("\n") or not content else "\n"
        try:
            with open(path, "a") as f:
                f.write(f"{suffix}{line}\n")
            touched.append(str(path))
        except OSError:
            pass
    return touched


def _unregister_autocomplete(name: str) -> list[str]:
    """Remove the argcomplete hook for `name` from each shell rc.

    Matches any line containing `register-python-argcomplete <name>`, so lines
    written by older cliche versions, fish-form lines, and lines written
    by the old cliche are all cleaned up.
    """
    touched = []
    pattern = re.compile(
        rf'^.*register-python-argcomplete\s+(?:--shell\s+\S+\s+)?{re.escape(name)}\b.*$\n?',
        re.MULTILINE,
    )
    for rc in _SHELL_RC_LINES:
        path = Path(os.path.expanduser(rc))
        if not path.exists():
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        new_content = pattern.sub('', content)
        if new_content != content:
            try:
                path.write_text(new_content)
                touched.append(str(path))
            except OSError:
                pass
    return touched


def _subprocess_env_with_writable_cache() -> dict:
    """Return a subprocess env that guarantees uv/pip have a writable cache dir.

    uv's cache resolution is: UV_CACHE_DIR > $XDG_CACHE_HOME/uv > ~/.cache/uv.
    In sandboxed / read-only-home environments (Codex, some CI, Nix, etc.) the
    default locations can be unwritable, which causes `Could not acquire lock /
    Read-only file system` errors that abort the install before it even starts.

    We probe the default cache dir with a tiny test write; if that fails, we
    redirect XDG_CACHE_HOME to a tmp subdir for this subprocess only (our own
    process env is untouched). Silent when the default works.
    """
    env = dict(os.environ)

    # Resolve the dir uv would actually use
    if "UV_CACHE_DIR" in env:
        cache_dir = Path(env["UV_CACHE_DIR"])
    elif "XDG_CACHE_HOME" in env:
        cache_dir = Path(env["XDG_CACHE_HOME"]) / "uv"
    else:
        cache_dir = Path(os.path.expanduser("~/.cache")) / "uv"

    # Probe
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        probe = cache_dir / f".cliche_write_probe.{os.getpid()}"
        probe.touch()
        probe.unlink()
        return env
    except OSError:
        pass

    # Default is unwritable — redirect to tmp
    tmp_cache = Path(tempfile.gettempdir()) / "cliche_cache"
    tmp_cache.mkdir(parents=True, exist_ok=True)
    env["XDG_CACHE_HOME"] = str(tmp_cache)
    env.pop("UV_CACHE_DIR", None)  # let XDG_CACHE_HOME take effect
    print(f"note: default cache dir ({cache_dir}) is not writable; "
          f"using {tmp_cache}/uv for this install.", file=sys.stderr)
    return env


def _cliche_entry_target(package_name: str) -> str:
    """Entry-point target we write for cliche-managed binaries.

    Points at a cliche-owned launcher (not `{pkg}._cliche:main` directly) so
    sys.path can be cleaned BEFORE the shim resolves the user package — which
    defends against leading-colon-PYTHONPATH shadowing and similar sys.path
    pollution that would otherwise be unfixable from inside `_cliche.py`.
    See `cliche/launcher.py` for the full rationale.
    """
    return f"cliche.launcher:launch_{package_name}"


def _parse_cliche_entry(value: str) -> str | None:
    """Return the package name if `value` is a cliche-managed entry target.

    Accepts both historical forms so `ls` / `uninstall` / migration paths
    continue to recognise old installs:
      - `{pkg}._cliche:main`            (pre-launcher)
      - `cliche.launcher:launch_{pkg}`  (current; routes through launcher)

    Returns None if `value` is neither.
    """
    value = value.strip()
    if value.endswith("._cliche:main"):
        head = value.split(":")[0]
        return head[: -len("._cliche")] if head.endswith("._cliche") else None
    prefix = "cliche.launcher:launch_"
    if value.startswith(prefix):
        return value[len(prefix):] or None
    return None


_LEGACY_CLICHE_MODULE_MARKER = '"""CLI entry point generated by cliche."""'


def _any_legacy_entry_still_references(directory: Path, package_name: str) -> bool:
    """True if pyproject/setup still has a `{pkg}._cliche:main` script entry.

    Used to defer legacy-`_cliche.py` cleanup until every binary has been
    migrated — otherwise a package with multiple binaries would have its
    `_cliche.py` removed after the first binary migrates, leaving the
    still-legacy second binary's shim pointing at a vanished module.
    """
    target = f"{package_name}._cliche:main"
    for fname in ("pyproject.toml", "setup.cfg", "setup.py"):
        f = directory / fname
        if not f.exists():
            continue
        try:
            if target in f.read_text():
                return True
        except OSError:
            continue
    return False


def _remove_legacy_cliche_module(directory: Path, package_name: str = "") -> None:
    """Remove a stale `_cliche.py` left behind by older cliche versions.

    Prior cliche generated a `_cliche.py` trampoline inside every installed
    package. The launcher-based entry point makes that file redundant — the
    shim routes through `cliche.launcher:launch_{pkg}` and calls
    `run_package_cli` directly. We only delete files that still carry our
    generation marker so we never touch user-authored code.

    When `package_name` is supplied we also check that no [project.scripts]
    entry still targets `{pkg}._cliche:main`; if any do, we defer the
    removal so multi-binary packages don't break mid-migration.
    """
    cliche_file = directory / "_cliche.py"
    if not cliche_file.exists():
        return
    try:
        head = cliche_file.read_text().splitlines()[:3]
    except OSError:
        return
    if not any(_LEGACY_CLICHE_MODULE_MARKER in line for line in head):
        return
    if package_name:
        project_root = directory
        if not (project_root / "pyproject.toml").exists() and (project_root.parent / "pyproject.toml").exists():
            project_root = project_root.parent
        if _any_legacy_entry_still_references(project_root, package_name):
            return
    try:
        cliche_file.unlink()
        print(f"Removed legacy {cliche_file}")
    except OSError:
        pass


def _get_package_name_from_pyproject(content: str) -> str | None:
    """Extract package name from pyproject.toml content."""
    match = re.search(r'^name\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    return match.group(1) if match else None


def _get_package_name_from_setup_cfg(content: str) -> str | None:
    """Extract package name from setup.cfg content."""
    match = re.search(r'^name\s*=\s*(.+)$', content, re.MULTILINE)
    return match.group(1).strip() if match else None


def _has_setup_py_entry_points(directory: Path) -> bool:
    """Check if setup.py exists and defines entry_points."""
    setup_py = directory / "setup.py"
    if not setup_py.exists():
        return False
    content = setup_py.read_text()
    return "entry_points" in content and "console_scripts" in content


def _ensure_cliche_in_setup_py(content: str) -> tuple[str, bool]:
    """Ensure `cliche` appears in the setup.py `install_requires=[...]`.

    Returns (new_content, changed). Handles three shapes:
      - `install_requires=[...]` with or without trailing comma
      - absent `install_requires=` entirely → inserts one before the
        closing `)` of the setup() call
      - already present → no-op

    Symmetric to the pyproject dependency-injection path; keeps the project's
    declared deps honest so `pip install <pkg>` works on a fresh venv.
    """
    if '"cliche"' in content or "'cliche'" in content:
        return content, False

    ir_re = re.compile(r'(install_requires\s*=\s*\[)([^\]]*)\]')
    m = ir_re.search(content)
    if m:
        existing = m.group(2).rstrip().rstrip(',').rstrip()
        new_deps = f'{existing}, "cliche"' if existing else '"cliche"'
        return ir_re.sub(lambda _: f"{m.group(1)}{new_deps}]", content, count=1), True

    # No install_requires at all — inject one before the closing `)` of
    # `setup(...)`. Match the LAST `)` at line start or with leading whitespace
    # to avoid accidentally matching `)` in nested arguments.
    close_re = re.compile(r'^(\s*)\)\s*$', re.MULTILINE)
    closes = list(close_re.finditer(content))
    if closes:
        m2 = closes[-1]
        indent = m2.group(1) or "    "
        # Trim any trailing comma on the preceding token so we emit a clean
        # `install_requires=["cliche"],\n)` block.
        insert = f'{indent}install_requires=["cliche"],\n'
        return content[: m2.start()] + insert + content[m2.start():], True

    return content, False


def _update_setup_py(directory: Path, binary_name: str, package_name: str) -> bool:
    """Update setup.py entry_points with the CLI entry point. Returns True if successful."""
    setup_py = directory / "setup.py"
    if not setup_py.exists():
        return False

    content = setup_py.read_text()
    target = _cliche_entry_target(package_name)
    entry_point = f"'{binary_name} = {target}'"

    # Check for an existing entry for this binary. It may already point at
    # our generated `_cliche:main` (nothing to do) — OR it may still point
    # at an older target like `pkg.__init__:cli.main` (old-cliche idiom) or
    # `pkg.cli:main` (hand-rolled). In those migration cases we must REPLACE
    # the target, otherwise the pip-installed shim keeps invoking the old
    # target and cliche's `_cliche.py` is never actually called.
    existing_entry_re = re.compile(
        rf'([\'"]){re.escape(binary_name)}\s*=\s*([^\'"]+)\1'
    )
    m = existing_entry_re.search(content)
    if m:
        current_target = m.group(2).strip()
        if current_target == target:
            # Still check the dependency even if the entry point was already right.
            content, dep_changed = _ensure_cliche_in_setup_py(content)
            if dep_changed:
                setup_py.write_text(content)
                print(f"Updated {setup_py}: added 'cliche' to install_requires")
            else:
                print(f"Entry point '{binary_name}' already points at {target}")
            return True
        # Migrate: rewrite the target in place, preserving the quote style.
        new_line = f"{m.group(1)}{binary_name} = {target}{m.group(1)}"
        content = content[: m.start()] + new_line + content[m.end() :]
        content, _ = _ensure_cliche_in_setup_py(content)
        setup_py.write_text(content)
        print(f"Updated {setup_py}: '{binary_name}' {current_target} → {target}")
        return True

    # Try to add to existing entry_points
    # Pattern: entry_points={'console_scripts': [...]}
    pattern = r"(entry_points\s*=\s*\{\s*['\"]console_scripts['\"]\s*:\s*\[)([^\]]*)\]"
    match = re.search(pattern, content)
    if match:
        existing = match.group(2).rstrip().rstrip(',')
        if existing:
            new_entries = f"{existing}, {entry_point}"
        else:
            new_entries = entry_point
        content = re.sub(pattern, f"\\g<1>{new_entries}]", content)
        content, _ = _ensure_cliche_in_setup_py(content)
        setup_py.write_text(content)
        print(f"Updated {setup_py}")
        return True

    return False


def _update_setup_cfg(directory: Path, binary_name: str, package_name: str) -> Path:
    """Update setup.cfg with the CLI entry point for legacy projects."""
    setup_cfg = directory / "setup.cfg"
    if not setup_cfg.exists():
        raise FileNotFoundError(
            f"expected setup.cfg at {setup_cfg} for legacy-project entry-point "
            f"registration but it does not exist. This usually means the project "
            f"was mis-classified as legacy — a modern pyproject.toml with a "
            f"[project] table is the canonical place for [project.scripts]."
        )
    content = setup_cfg.read_text()

    entry_point = f"{binary_name} = {_cliche_entry_target(package_name)}"

    if "[options.entry_points]" in content:
        # Check if this binary already exists — update in place
        existing_pattern = rf'^(\s*){re.escape(binary_name)}\s*=.*$'
        if re.search(existing_pattern, content, re.MULTILINE):
            content = re.sub(existing_pattern, f'\\1{entry_point}', content, flags=re.MULTILINE)
        elif "console_scripts =" in content:
            # Add to existing console_scripts
            pattern = r'(console_scripts\s*=\s*\n)'
            if re.search(pattern, content):
                content = re.sub(pattern, f'\\1    {entry_point}\n', content)
            else:
                # Single line format - convert to multiline
                pattern = r'(console_scripts\s*=\s*)([^\n]+)'
                match = re.search(pattern, content)
                if match:
                    existing = match.group(2).strip()
                    content = re.sub(
                        pattern,
                        f'\\1\n    {existing}\n    {entry_point}',
                        content
                    )
        else:
            # Add console_scripts to existing entry_points section
            content = re.sub(
                r'(\[options\.entry_points\])',
                f'\\1\nconsole_scripts =\n    {entry_point}',
                content
            )
    else:
        # Add new section
        content = content.rstrip() + f'\n\n[options.entry_points]\nconsole_scripts =\n    {entry_point}\n'

    content, dep_changed = _ensure_cliche_in_setup_cfg(content)

    setup_cfg.write_text(content)
    print(f"Updated {setup_cfg}"
          + (" (+ 'cliche' added to install_requires)" if dep_changed else ""))
    return setup_cfg


def _ensure_cliche_in_setup_cfg(content: str) -> tuple[str, bool]:
    """Ensure `cliche` appears in the setup.cfg `install_requires`.

    Handles both shapes of `install_requires`:
      - multiline `install_requires =\\n    pkg1\\n    pkg2` under [options]
      - inline `install_requires = pkg1, pkg2` (rare but legal)

    Creates an `[options]` section with `install_requires` if absent.
    """
    if re.search(r'(^|\n)\s*cliche\b', content):
        return content, False

    # Multiline variant under [options]: `install_requires =\n    pkg\n    ...`
    multiline_re = re.compile(
        r'(^install_requires\s*=\s*\n)((?:[ \t]+\S[^\n]*\n)*)',
        re.MULTILINE,
    )
    m = multiline_re.search(content)
    if m:
        header, body = m.group(1), m.group(2)
        # Match the indent of the first existing line; default to 4 spaces.
        indent_match = re.match(r'([ \t]+)', body) if body else None
        indent = indent_match.group(1) if indent_match else "    "
        new_body = body + f"{indent}cliche\n"
        return content[: m.start()] + header + new_body + content[m.end():], True

    # Inline variant: `install_requires = pkg1, pkg2`
    inline_re = re.compile(r'^(install_requires\s*=\s*)([^\n]+)$', re.MULTILINE)
    m = inline_re.search(content)
    if m:
        existing = m.group(2).rstrip().rstrip(',').rstrip()
        new_line = f"{m.group(1)}{existing}, cliche" if existing else f"{m.group(1)}cliche"
        return content[: m.start()] + new_line + content[m.end():], True

    # No install_requires anywhere. Add it under [options], creating the
    # section if needed.
    if re.search(r'^\[options\]\s*$', content, re.MULTILINE):
        content = re.sub(
            r'(^\[options\]\s*\n)',
            r'\1install_requires =\n    cliche\n',
            content,
            count=1,
            flags=re.MULTILINE,
        )
        return content, True

    content = content.rstrip() + "\n\n[options]\ninstall_requires =\n    cliche\n"
    return content, True


def _update_pyproject_toml(directory: Path, binary_name: str, package_name: str,
                           is_subdir_package: bool = False) -> Path:
    """Update or create pyproject.toml with the CLI entry point.

    `is_subdir_package` drives the layout section: when True, omit the
    `package-dir` mapping (setuptools auto-discovers `{package_name}/`);
    when False, use `package-dir = {"pkg" = "."}` for flat layout. Mismatch
    here is the classic ModuleNotFoundError-after-install footgun.
    """
    pyproject = directory / "pyproject.toml"
    setup_cfg = directory / "setup.cfg"
    has_setup_cfg = setup_cfg.exists()

    if pyproject.exists():
        content = pyproject.read_text()
        # Use existing package name if defined
        existing_name = _get_package_name_from_pyproject(content)
        if existing_name:
            package_name = existing_name
        # Heal a stale flat-layout mapping left behind by a prior install
        # (dir name == package name, no subdir at the time, then a subdir
        # got added later). Only touch the exact template we generate —
        # never rewrite a hand-authored pyproject.toml.
        if is_subdir_package and re.search(
            rf'package-dir\s*=\s*\{{[^}}]*"{re.escape(package_name)}"\s*=\s*"\."', content
        ):
            content = re.sub(
                rf'\n\[tool\.setuptools\]\npackage-dir\s*=\s*\{{[^}}]*\}}\npackages\s*=\s*\["{re.escape(package_name)}"\]',
                f'\n[tool.setuptools]\npackages = ["{package_name}"]',
                content,
            )
    elif is_subdir_package:
        # Subdir layout: setuptools auto-discovers {package_name}/.
        content = f'''[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[tool.setuptools]
packages = ["{package_name}"]
'''
        print(f"Created {pyproject}")
    else:
        # Flat layout: map the package name to the current directory.
        content = f'''[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[tool.setuptools]
package-dir = {{"{package_name}" = "."}}
packages = ["{package_name}"]
'''
        print(f"Created {pyproject}")

    # Entry point line
    entry_point = f'{binary_name} = "{_cliche_entry_target(package_name)}"'

    # Add/update [project.scripts] section. We must scope the "does this
    # binary already exist?" check and any in-place update to the
    # [project.scripts] table only — otherwise a `binary_name = ...` line
    # in an unrelated table like [tool.setuptools.package-data] gets
    # clobbered (e.g. `metadate = ["_cscanner.c"]` → `metadate = "..."`).
    if "[project.scripts]" in content:
        # Carve out the [project.scripts] table: everything from its
        # header up to the next TOML section header (or EOF).
        section_re = re.compile(
            r'(\[project\.scripts\]\s*\n)((?:(?!^\[).*\n)*)',
            re.MULTILINE,
        )
        m = section_re.search(content)
        if m:
            header, body = m.group(1), m.group(2)
            entry_re = re.compile(
                rf'^{re.escape(binary_name)}\s*=.*$', re.MULTILINE
            )
            if entry_re.search(body):
                new_body = entry_re.sub(entry_point, body)
            else:
                # Append to the section (preserve any trailing blank line).
                new_body = body.rstrip("\n") + f"\n{entry_point}\n"
                if body.endswith("\n\n"):
                    new_body += "\n"
            content = content[: m.start()] + header + new_body + content[m.end() :]
        else:
            # Section is present as a literal substring but our regex
            # couldn't locate it (odd formatting) — just append the line
            # after the header.
            content = re.sub(
                r'(\[project\.scripts\])',
                f'\\1\n{entry_point}',
                content,
            )
    else:
        # Add new section before [tool.*] sections or at end
        scripts_section = f'\n[project.scripts]\n{entry_point}\n'
        if "[tool." in content:
            content = re.sub(r'(\n\[tool\.)', f'{scripts_section}\\1', content, count=1)
        else:
            content = content.rstrip() + "\n" + scripts_section

    # Add cliche to dependencies if not present.
    # Skip this if setup.cfg exists to avoid conflicts, OR if the project
    # being installed IS cliche itself (self-dep would be a PyPI upload error
    # and is just wrong metadata either way).
    if not has_setup_cfg and package_name != "cliche":
        deps_pattern = r'(dependencies\s*=\s*\[)([^\]]*)\]'
        match = re.search(deps_pattern, content)
        if match:
            existing_deps = match.group(2)
            # Check if cliche is already in the dependencies list
            if '"cliche"' not in existing_deps and "'cliche'" not in existing_deps:
                # Strip both whitespace AND a trailing comma — multiline TOML
                # arrays commonly end with a trailing comma (`"pytz>=2023.3",\n`),
                # which would otherwise produce `",\n, "cliche"` (double
                # comma → TOML parse error).
                existing_deps_stripped = existing_deps.rstrip().rstrip(",").rstrip()
                if existing_deps_stripped:
                    new_deps = f'{existing_deps_stripped}, "cliche"'
                else:
                    new_deps = '"cliche"'
                content = re.sub(deps_pattern, f'\\1{new_deps}]', content)
    # If setup.cfg is also present, _update_setup_cfg already auto-injects
    # `cliche` on its side — no action needed here.

    pyproject.write_text(content)
    print(f"Updated {pyproject}")
    return pyproject


def _find_package_dir(directory: Path, package_name: str, auto_init: bool = False) -> Path:
    """Find where to put _cliche.py — subdirectory package or the root.

    If `auto_init` is True and a subdir matching the package name exists without
    `__init__.py` **and contains at least one .py file**, create the marker
    `__init__.py` and treat it as subdir layout. The .py-file guard prevents a
    subtler footgun: an empty or stale subdir (leftover from a prior run, or a
    mkdir typo) would otherwise silently promote to subdir layout while the
    user's real .py files sit in the workdir root, leading to "0 commands"
    CLIs and ModuleNotFoundError at runtime.
    """
    subdir = directory / package_name
    if subdir.is_dir():
        if (subdir / "__init__.py").exists():
            return subdir
        if auto_init and any(subdir.glob("*.py")):
            # Create a marker __init__.py so the subdir is a real package and the
            # flat-layout ambiguity is resolved in favor of subdir layout.
            (subdir / "__init__.py").write_text(AUTO_INIT_MARKER + "\n")
            print(f"Created {subdir / '__init__.py'}  "
                  f"(subdir matched package name AND contains .py files; "
                  f"treating as subdir layout)")
            return subdir

    # src layout
    src_subdir = directory / "src" / package_name
    if src_subdir.is_dir() and (src_subdir / "__init__.py").exists():
        return src_subdir

    # Flat layout (directory itself is the package)
    return directory


def _get_package_name_from_setup_py(content: str) -> str | None:
    """Extract package name from setup.py content."""
    match = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", content)
    return match.group(1) if match else None


def _editable_source_dir(pkg_name: str) -> Path | None:
    """Return the editable source dir for a pip-installed package, if it's editable."""
    try:
        import importlib.metadata as im
        dist = im.distribution(pkg_name)
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            data = json.loads(direct_url)
            if data.get("dir_info", {}).get("editable"):
                url = data.get("url", "")
                if url.startswith("file://"):
                    return Path(url[len("file://"):])
    except Exception:
        pass
    return None


def _all_entry_points(binary_name: str) -> list[dict]:
    """Return every package claiming `binary_name` as a console_scripts entry.

    Two dists may both declare the same script (different `pip install -e`
    runs for unrelated projects with colliding names). pip writes the shim
    twice, last-install-wins on disk, but the metadata of both persists.
    Callers that need to disambiguate (uninstall) can iterate this; callers
    that just need "some" entry (install's collision check) use
    _existing_entry_point which picks the LIVE one.
    """
    out: list[dict] = []
    try:
        from importlib.metadata import entry_points
    except ImportError:
        entry_points = None

    if entry_points is not None:
        for ep in entry_points(group="console_scripts"):
            if ep.name != binary_name:
                continue
            pkg = _parse_cliche_entry(ep.value)
            if pkg is None:
                continue
            out.append({
                "pkg": pkg,
                "mode": "pip",
                "source_dir": _editable_source_dir(pkg),
                "env_path": None,
            })

    for entry in _uv_tool_cliche_entries():
        if entry["binary"] != binary_name:
            continue
        src = None
        try:
            env_path = Path(entry["env_path"])
            for sp in (env_path / "lib").glob("python*/site-packages"):
                for d in sp.glob(f"{entry['pkg']}*.dist-info"):
                    direct_url = d / "direct_url.json"
                    if direct_url.exists():
                        data = json.loads(direct_url.read_text())
                        url = data.get("url", "")
                        if url.startswith("file://"):
                            src = Path(url[len("file://"):])
                            break
                if src:
                    break
        except Exception:
            pass
        out.append({
            "pkg": entry["pkg"],
            "mode": "tool",
            "source_dir": src,
            "env_path": entry["env_path"],
        })
    return out


def _existing_entry_point(binary_name: str) -> dict | None:
    """Single-entry lookup — prefers the package whose shim file is LIVE on
    disk. Falls back to the first match when the shim owner is unknown (e.g.
    no shim file, or the shim came from a pre-cliche install).

    Keeps backwards compatibility with callers that don't care about
    duplicates (install's collision check, zombie detection).
    """
    entries = _all_entry_points(binary_name)
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]
    live = _shim_owner(binary_name)
    if live:
        for e in entries:
            if e["pkg"] == live:
                return e
    return entries[0]


def _zombie_metadata_path(binary_name: str) -> Path | None:
    """If `binary_name` maps to a console_scripts entry whose backing metadata
    is an .egg-info or .dist-info outside the managed site-packages (i.e. it's
    only visible because PYTHONPATH / a .pth file adds its parent to sys.path),
    return that metadata directory. Else None.

    These are the entries that `pip/uv uninstall` can't touch — we need to edit
    the metadata files directly or the entry point will resurrect on every
    `cliche ls`.
    """
    try:
        import importlib.metadata as im
    except ImportError:
        return None

    for ep in im.entry_points(group="console_scripts"):
        if ep.name != binary_name:
            continue
        dist = ep.dist
        if dist is None:
            continue
        # `dist._path` is the dist-info / egg-info dir for file-system dists.
        # (Private attr, but it's the only reliable way to find this across
        # PathDistribution subclasses.) Fall back to `locate_file` if missing.
        path = getattr(dist, "_path", None)
        if path is None:
            try:
                # Probe via a known file inside the metadata dir.
                probe = dist.locate_file("METADATA")
                path = Path(probe).parent if probe else None
            except Exception:
                path = None
        if path is None:
            continue
        return Path(path)
    return None


def _strip_entry_from_egg_info(meta_path: Path, binary_name: str) -> bool:
    """Remove `binary_name = ...` from entry_points.txt in an .egg-info dir.

    Returns True iff the line was found and removed. Leaves other entries
    intact. Never deletes the .egg-info dir itself — there may be other
    binaries we don't own, and the parent project still needs it.
    """
    if not meta_path.name.endswith(".egg-info"):
        # dist-info format isn't a simple INI — don't risk corrupting it.
        return False
    entry_file = meta_path / "entry_points.txt"
    if not entry_file.exists():
        return False
    try:
        content = entry_file.read_text()
    except OSError:
        return False
    # Match `name = anything` anchored to line start, optional leading whitespace
    pattern = re.compile(
        rf'^\s*{re.escape(binary_name)}\s*=.*$\n?',
        re.MULTILINE,
    )
    new_content, n = pattern.subn('', content)
    if n == 0:
        return False
    try:
        entry_file.write_text(new_content)
    except OSError:
        return False
    return True


def _print_zombie_diagnostic(binary_name: str, meta_path: Path | None) -> None:
    """Explain why the uninstall didn't actually remove the entry point."""
    print(f"\nerror: '{binary_name}' is still a registered entry point after uninstall.",
          file=sys.stderr)
    if meta_path is None:
        print(f"       (couldn't resolve the backing metadata — run "
              f"`cliche ls` and look for dup lines.)", file=sys.stderr)
        return
    print(f"       The binary is declared in: {meta_path}", file=sys.stderr)
    print(f"       pip/uv can't uninstall this because it's not a proper install —",
          file=sys.stderr)
    print(f"       most likely a stale .egg-info discovered via PYTHONPATH or a .pth file.",
          file=sys.stderr)
    print(f"", file=sys.stderr)
    entry_file = meta_path / "entry_points.txt"
    if entry_file.exists():
        print(f"       Fix: remove the `{binary_name} = ...` line from:", file=sys.stderr)
        print(f"            {entry_file}", file=sys.stderr)
    else:
        print(f"       Fix: remove (or move off PYTHONPATH) the directory:", file=sys.stderr)
        print(f"            {meta_path}", file=sys.stderr)


def _shim_owner(binary_name: str) -> str | None:
    """Return the package whose `_cliche:main` the on-disk shim dispatches to.

    When two packages declare the same `[project.scripts]` name, pip writes the
    shim file twice — last install wins on disk. Both dist-infos still list the
    entry, so `importlib.metadata.entry_points()` returns duplicates. This
    helper reads the live shim to disambiguate: the one whose import line
    matches this package is the one actually invoked when the user types the
    command.
    """
    path = shutil.which(binary_name)
    if not path:
        return None
    try:
        content = Path(path).read_text(errors="replace")
    except OSError:
        return None
    # Generated shims carry `from <pkg>._cliche import main`. Anything else
    # (e.g. uv tool shims, which re-exec a binary in an isolated env) we
    # treat as "unknown" — duplicate detection just skips those rows.
    m = re.search(r'from\s+([A-Za-z_][A-Za-z0-9_]*)\._cliche\s+import\s+main', content)
    return m.group(1) if m else None


def _uv_tool_cliche_entries() -> list[dict]:
    """Parse `uv tool list --show-paths` and filter to cliche-owned tools.

    Identifies ours by reading the binary shim: it imports `<pkg>._cliche`.
    """
    uv_path = shutil.which("uv")
    if not uv_path:
        return []
    try:
        proc = subprocess.run(
            [uv_path, "tool", "list", "--show-paths"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    entries = []
    current = None
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            current = None
            continue
        if line.startswith("- "):
            # "- <binary> (<binary_path>)"
            if current is None:
                continue
            m = re.match(r"-\s+(\S+)\s+\((.+)\)$", line)
            if not m:
                continue
            binary, binary_path = m.group(1), m.group(2)
            # Check shim to confirm it's one of ours, and extract the real
            # import name from the shim directly. uv tool reports the PEP-503
            # normalised distribution name (`wk-tool`, hyphenated, lowercase)
            # which can differ from the import name (`wk_tool`) on several
            # axes — dashes for underscores, folded case. Reading the shim
            # avoids guessing the back-conversion.
            try:
                shim = Path(binary_path).read_text()
            except OSError:
                continue
            # Current form: `from cliche.launcher import launch_{pkg}`.
            # Pre-launcher form: `from {pkg}._cliche import main`.
            pkg_match = (
                re.search(r'from\s+cliche\.launcher\s+import\s+launch_([A-Za-z_][A-Za-z0-9_]*)', shim)
                or re.search(r'from\s+([A-Za-z_][A-Za-z0-9_]*)\._cliche\s+import', shim)
            )
            if not pkg_match:
                continue
            pkg = pkg_match.group(1)
            entries.append({
                "binary": binary,
                "pkg": pkg,
                "ver": current["ver"],
                "env_path": current["env_path"],
                "binary_path": binary_path,
            })
        else:
            # "<pkg> v<ver> (<env_path>)"
            m = re.match(r"(\S+)\s+v(\S+)\s+\((.+)\)$", line)
            if m:
                current = {"pkg": m.group(1), "ver": m.group(2), "env_path": m.group(3)}
            else:
                current = None
    return entries


def _validate_binary_name(name: str) -> None:
    """Reject obviously broken binary names. Exits non-zero on failure."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        print(f"error: invalid binary name {name!r}.", file=sys.stderr)
        print(f"       A binary name should look like `mytool` or `my-tool` — "
              f"letters/digits/underscores/dashes, not a path.", file=sys.stderr)
        sys.exit(1)
    # Shell binaries and `[project.scripts]` keys both accept leading digits
    # (e.g. `1one`); only the Python import name needs to start with a letter
    # or underscore — that's enforced separately by _validate_package_name.
    if not re.match(r'^[A-Za-z0-9_][A-Za-z0-9_-]*$', name):
        print(f"error: invalid binary name {name!r}.", file=sys.stderr)
        print(f"       Must contain only letters, digits, `_`, or `-`.", file=sys.stderr)
        sys.exit(1)


def _validate_package_name(package_name: str) -> None:
    """Reject package names that won't work as Python import names / pyproject.toml
    `[project].name`. Exits non-zero on failure with a clear diagnostic."""
    if not package_name or package_name in (".", "..") or "/" in package_name:
        print(f"error: invalid package (import) name {package_name!r}.", file=sys.stderr)
        print(f"       A package name should look like `my_tool` — letters/digits/"
              f"underscores, not a path.", file=sys.stderr)
        sys.exit(1)
    # Python identifier rules: start with letter/underscore, then letters/digits/underscores.
    # pyproject.toml `[project].name` also allows `-` and `.` but those don't work
    # as Python import names (which is what cliche needs).
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', package_name):
        print(f"error: invalid package (import) name {package_name!r}.", file=sys.stderr)
        print(f"       Must start with a letter/underscore and contain only letters, "
              f"digits, or `_` (no dashes, no dots — this is the Python import name).",
              file=sys.stderr)
        print(f"       Override with `-p <valid_name>` when running install.", file=sys.stderr)
        sys.exit(1)


def _cliche_source_dir() -> Path | None:
    """Return the source dir of cliche itself, for --with-editable in uv tool install.
    Falls back to None if cliche is installed non-editably from PyPI (uv can resolve it)."""
    try:
        import cliche as _nc
        if _nc.__file__:
            return Path(_nc.__file__).parent.parent
    except Exception:
        pass
    return None


def install(name: str, module_dir: str = None, no_pip: bool = False,
            package_name: str = None, force: bool = False, tool: bool = False,
            no_autocomplete: bool = False, **kwargs):
    """
    Install a CLI tool by creating a pip-installable package.

    The current directory becomes the package. Creates __init__.py and _cliche.py
    here, along with pyproject.toml.

    Args:
        name: Name of the CLI binary to create
        module_dir: Directory to use as the package (default: cwd)
        no_pip: Skip running pip install -e . (just generate files)
        package_name: Python import name (default: existing config, else directory name).
                      Use this when the binary name should differ from the import name.
        force: Install even if a CLI with the same binary name already exists.
    """
    directory = Path(module_dir) if module_dir else Path.cwd()

    # Defensive: refuse to clobber cliche's own primary entry. A user running
    # `cliche install cliche -p cliche` (or equivalent where the binary name
    # IS `cliche`) would rewrite `[project.scripts]` to
    # `cliche = "cliche.launcher:launch_cliche"`, masking the real
    # `cliche = "cliche.install:main_cli"` and breaking the CLI. Aliases like
    # `cliche install sdm -p cliche` are fine — they add a separate binary
    # name whose launcher path handles self-aliasing correctly.
    if name == "cliche" and package_name == "cliche":
        print(
            "error: refusing to install the binary name `cliche` against the "
            "`cliche` package — that would overwrite cliche's own "
            "[project.scripts] entry.",
            file=sys.stderr,
        )
        print(
            "       use a different binary name to add an alias: "
            "`cliche install myalias -p cliche`.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Detect project type
    setup_py = directory / "setup.py"
    setup_cfg = directory / "setup.cfg"
    pyproject = directory / "pyproject.toml"
    has_setup_py_entry_points = _has_setup_py_entry_points(directory)
    # A project is "legacy" ONLY if it has setup.py / setup.cfg AND no
    # modern pyproject.toml `[project]` table. A setup.py alongside a
    # PEP-621 pyproject is a common modern pattern (C extensions, custom
    # build steps) — that's NOT legacy, and writing into a non-existent
    # setup.cfg would crash.
    has_pep621_pyproject = (
        pyproject.exists()
        and re.search(r'^\[project\]', pyproject.read_text(), re.MULTILINE) is not None
    )
    is_legacy_project = (setup_cfg.exists() or setup_py.exists()) and not has_pep621_pyproject

    # Resolve package name. Priority:
    #   1. Explicit --package-name flag
    #   2. Existing config (setup.py > setup.cfg > pyproject.toml)
    #   3. Directory name (matches Python convention: import name == dir name)
    if not package_name:
        if setup_py.exists():
            package_name = _get_package_name_from_setup_py(setup_py.read_text())
        if not package_name and setup_cfg.exists():
            package_name = _get_package_name_from_setup_cfg(setup_cfg.read_text())
        if not package_name and pyproject.exists():
            package_name = _get_package_name_from_pyproject(pyproject.read_text())
        if not package_name:
            package_name = directory.name.replace("-", "_")

    # Validate names BEFORE writing any files, so we fail fast with a clear
    # error instead of later producing a broken pyproject.toml that uv/pip
    # chokes on with a cryptic "Not a valid package or extra name" message.
    _validate_binary_name(name)
    _validate_package_name(package_name)

    # Guard against silently clobbering another tool that already owns this binary name.
    existing = _existing_entry_point(name)
    if existing and existing["pkg"] != package_name and not force:
        where = existing.get("source_dir") or existing.get("env_path") or "?"
        print(f"error: a CLI named '{name}' is already installed.", file=sys.stderr)
        print(f"       current:    {name} → {existing['pkg']}  ({where})", file=sys.stderr)
        print(f"       requested:  {name} → {package_name}  ({directory})", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"To replace it:   cliche uninstall {name}  &&  cliche install {name}", file=sys.stderr)
        print(f"Or force overlap: cliche install {name} --force", file=sys.stderr)
        print(f"(two packages sharing a binary leaves one pointing nowhere on PATH — see `cliche ls`)", file=sys.stderr)
        sys.exit(1)

    # Find the actual package directory (might be a subdirectory).
    # auto_init=True: if a subdir named {package_name} exists without __init__.py,
    # promote it to a real package (writes our marker) rather than silently falling
    # through to flat layout — which Python's namespace-package machinery would
    # then shadow, leaving `pkg.__file__ == None` at runtime.
    pkg_dir = _find_package_dir(directory, package_name, auto_init=True)
    is_subdir_package = pkg_dir != directory

    # Pre-flight: flat layout + a sibling file named `{package_name}.py`
    # shadows the package at import time — `import {pkg}` resolves to the
    # module, not the package, and `import {pkg}._cliche` fails with
    # "'{pkg}' is not a package". The install would technically "succeed"
    # (pip exits 0) and only break when the binary is run. Fail loudly now,
    # before writing __init__.py / _cliche.py / amending pyproject.toml.
    if not is_subdir_package:
        shadow = directory / f"{package_name}.py"
        if shadow.exists():
            print(
                f"\nerror: a file named '{package_name}.py' lives alongside this package dir\n"
                f"       ({shadow}).\n"
                f"       With a flat layout, that file shadows the package — `import "
                f"{package_name}` would resolve to the module, not the package, and "
                f"`{name}` would fail on every invocation.\n\n"
                f"Fix: rename it to something else, e.g.:\n"
                f"    git mv {shadow.name} commands.py   # or cli.py, main.py — anything but {shadow.name}\n"
                f"then re-run `cliche install {name}`.\n\n"
                f"(Alternative: move your code into a subdirectory "
                f"`{package_name}/{package_name}/` and let cliche treat it as "
                f"a subdir-layout package — the name-collision disappears because "
                f"the .py file is then one level deeper than `{package_name}/`.)",
                file=sys.stderr,
            )
            sys.exit(1)

    # Create __init__.py if it doesn't exist (only for flat layout)
    if not is_subdir_package:
        init_file = directory / "__init__.py"
        if not init_file.exists():
            init_file.write_text('"""Package created by cliche."""\n')
            print(f"Created {init_file}")

    print(f"Package: {package_name}")
    print(f"Binary: {name}")
    if is_subdir_package:
        print(f"Package dir: {pkg_dir}")

    # Update config files based on project type
    if has_setup_py_entry_points:
        # setup.py with entry_points takes precedence
        if not _update_setup_py(directory, name, package_name):
            print("Warning: Could not update setup.py entry_points automatically")
            print(f"         Add manually: '{name} = {_cliche_entry_target(package_name)}'")
    elif is_legacy_project:
        _update_setup_cfg(directory, name, package_name)
    else:
        _update_pyproject_toml(directory, name, package_name,
                               is_subdir_package=is_subdir_package)

    # No `_cliche.py` is generated. Entry points route through
    # `cliche.launcher:launch_{pkg}`, which calls `run_package_cli` directly.
    # Migration: older cliche versions wrote a `_cliche.py` into the user
    # package; remove it if it still carries our generation marker AND no
    # other `[project.scripts]` binary still targets the legacy entry (a
    # multi-binary package might have unmigrated siblings that still need
    # the file).
    _remove_legacy_cliche_module(pkg_dir, package_name)

    # Run editable install
    if not no_pip:
        uv_path = shutil.which("uv")
        sub_env = _subprocess_env_with_writable_cache()
        if tool:
            if not uv_path:
                print("error: --tool requires uv (https://docs.astral.sh/uv/). Install uv first or drop --tool.",
                      file=sys.stderr)
                sys.exit(1)
            cmd = [uv_path, "tool", "install", "--force", "--editable", str(directory)]
            # cliche itself must be available inside the tool's isolated env.
            # If we're running from an editable checkout, pass that path so uv
            # doesn't need to resolve 'cliche' from PyPI.
            nc_src = _cliche_source_dir()
            if nc_src and (nc_src / "pyproject.toml").exists():
                cmd += ["--with-editable", str(nc_src)]
            print(f"\nRunning {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=False, env=sub_env)
        elif uv_path:
            print("\nRunning uv pip install -e .  (editable, into current Python env)")
            result = subprocess.run(
                [uv_path, "pip", "install", "--python", sys.executable, "-e", str(directory)],
                capture_output=False, env=sub_env,
            )
        else:
            print("\nRunning pip install -e .  (editable, into current Python env)")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", str(directory)],
                capture_output=False, env=sub_env,
            )
        if result.returncode != 0:
            print("Warning: install failed", file=sys.stderr)
            return

        # Post-install sanity: catch layout-shadowing bugs (namespace-package
        # collisions, sibling `{pkg}.py` stealing the import, wrong src-layout
        # mapping, etc.) that would otherwise surface as a cryptic runtime error.
        #
        # For --tool installs, sys.executable is the CALLER's Python, not the
        # isolated tool venv, so importing `{package_name}` there would be
        # testing the wrong environment. Invoke the binary shim instead — it
        # runs through the tool venv's Python and does the real import chain.
        if tool:
            binary_path = shutil.which(name)
            if not binary_path:
                print(f"\nerror: tool install reported success but '{name}' is "
                      f"not on PATH (expected under ~/.local/bin/).", file=sys.stderr)
                sys.exit(1)
            # Run from a foreign, freshly-created, empty cwd. Two historical
            # failure modes are neutralised at once:
            #  1. Flat-layout package-dir with a matching subdir only "worked"
            #     when the shim was invoked from inside the workdir — every
            #     other cwd crashed with ModuleNotFoundError. A foreign cwd
            #     exposes that at install time.
            #  2. A file named `{package_name}.py` sitting in /tmp (or anywhere
            #     a polluted sys.path lands — e.g. a PYTHONPATH leading-colon
            #     that puts CWD on sys.path) would shadow the real package
            #     during the probe. Using a fresh empty subdir means even if
            #     something drags the probe cwd onto sys.path, the dir has no
            #     `.py` files that could steal the import.
            # We also strip empty entries from PYTHONPATH in the probe's env:
            # Python interprets an empty entry as CWD at import time, which
            # would re-introduce the same shadowing window the empty cwd
            # was meant to close.
            probe_cwd = Path(tempfile.mkdtemp(prefix="cliche-probe-"))
            probe_env = os.environ.copy()
            pp = probe_env.get("PYTHONPATH")
            if pp is not None:
                cleaned_pp = os.pathsep.join(p for p in pp.split(os.pathsep) if p)
                if cleaned_pp:
                    probe_env["PYTHONPATH"] = cleaned_pp
                else:
                    probe_env.pop("PYTHONPATH", None)
            try:
                probe = subprocess.run(
                    [binary_path, "--help"],
                    capture_output=True, text=True,
                    cwd=str(probe_cwd), env=probe_env,
                )
            finally:
                shutil.rmtree(probe_cwd, ignore_errors=True)
        else:
            # Pip-mode probe: verify the package is importable AND the
            # launcher closure constructs. `run_package_cli` isn't called
            # here — constructing the launcher only validates the import
            # chain, which is what the sanity check is testing.
            probe = subprocess.run(
                [sys.executable, "-c",
                 f"import {package_name} as _m; "
                 f"assert _m.__file__, 'package resolved as namespace (no __init__.py)'; "
                 f"from cliche.launcher import launch_{package_name}; "
                 f"assert callable(launch_{package_name}); "
                 f"print(_m.__file__)"],
                capture_output=True, text=True,
            )
        if probe.returncode != 0:
            print(f"\nerror: install completed but the binary won't import cleanly:", file=sys.stderr)
            print(probe.stderr.strip(), file=sys.stderr)
            print(f"\nLikely cause: something in {directory} is shadowing the package "
                  f"named '{package_name}'. Common culprits:", file=sys.stderr)
            print(f"  - a file named `{package_name}.py` alongside the package dir", file=sys.stderr)
            print(f"  - a subdir `{package_name}/` with missing `__init__.py`", file=sys.stderr)
            print(f"  - conflicting `src/{package_name}/` layout", file=sys.stderr)
            print(f"  - package_name mismatch with actual on-disk structure", file=sys.stderr)
            sys.exit(1)

    print(f"\nInstalled '{name}' successfully!")
    if tool:
        print(f"Installed as an isolated uv tool. Binary is on PATH via ~/.local/bin/{name}.")
        print(f"Manage via: uv tool {{list,upgrade,uninstall}}  (or `cliche uninstall {name}`).")

    # Shell autocomplete: argcomplete is already a dep and run.py already handles
    # the _ARGCOMPLETE env var. All we need is the shell-side eval line.
    if not no_autocomplete:
        touched = _register_autocomplete(name)
        if touched:
            print(f"Autocomplete registered in: {', '.join(touched)}")
            print(f"  (open a new shell or `source` the rc file to activate)")
        else:
            # Either every rc already had the line, or no rc exists. Both are fine.
            pass

    print(f"You can now run: {name} --help")


AUTO_INIT_MARKER = '"""Package created by cliche."""'


def _pristine_pyproject(package_name: str) -> str:
    """The exact pyproject.toml we generate on a fresh flat-layout install,
    AFTER a later uninstall strips its entry point and empty [project.scripts]."""
    return (
        f'[build-system]\n'
        f'requires = ["setuptools>=61.0"]\n'
        f'build-backend = "setuptools.build_meta"\n'
        f'\n'
        f'[project]\n'
        f'name = "{package_name}"\n'
        f'version = "0.1.0"\n'
        f'requires-python = ">=3.10"\n'
        f'dependencies = ["cliche"]\n'
        f'\n'
        f'[tool.setuptools]\n'
        f'package-dir = {{"{package_name}" = "."}}\n'
        f'packages = ["{package_name}"]\n'
    )


def _remove_runtime_cache(package_name: str, pkg_dir: Path) -> Path | None:
    """Delete the runtime cache file keyed by (pkg_name, hash(pkg_dir))."""
    import hashlib
    cache_home = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    cache_dir = Path(cache_home) / "cliche"
    dir_hash = hashlib.md5(str(pkg_dir).encode()).hexdigest()[:8]
    cache_file = cache_dir / f"{package_name}_{dir_hash}.json"
    if cache_file.exists():
        cache_file.unlink()
        return cache_file
    return None


def _remove_egg_info(directory: Path, package_name: str) -> list[Path]:
    """Remove stale *.egg-info dirs left behind by editable installs."""
    removed = []
    candidates = list(directory.glob(f"{package_name}.egg-info")) + \
                 list(directory.glob(f"{package_name.replace('_', '-')}.egg-info"))
    for p in candidates:
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(p)
    return removed


def _strip_empty_project_scripts(pyproject: Path) -> bool:
    """If [project.scripts] has no entries left, remove the empty section. Returns True if modified."""
    content = pyproject.read_text()
    # Match the header followed by only blank lines until the next section or EOF
    new_content = re.sub(
        r'\n+\[project\.scripts\]\s*\n(?=\n\[|\Z)',
        '\n',
        content,
    )
    if new_content != content:
        pyproject.write_text(new_content)
        return True
    return False


def uninstall(name: str, module_dir: str = None, pkg: str = None, **kwargs):
    """
    Uninstall a CLI tool and clean up cliche-owned artifacts.

    Removes: pip package, entry point, _cliche.py, runtime cache, *.egg-info,
    empty [project.scripts] section, and auto-generated __init__.py (only if
    it still matches our marker content). User-written code is never touched.

    Args:
        name: Name of the CLI binary to remove
        module_dir: Directory containing the package (default: cwd)
        pkg: Disambiguate when two packages declare the same binary name.
             Pass the Python import name (the IMPORT column in `cliche ls`)
             of the one you want to remove. Required when there's ambiguity.
    """
    # FIRST: resolve the binary via the entry-point / uv-tool registry. This is
    # the authoritative "who owns this binary" lookup — NOT whatever pyproject
    # happens to be in cwd. Prevents the footgun where running uninstall from a
    # different directory would happily uninstall that directory's package.
    matches = _all_entry_points(name)
    if len(matches) > 1:
        if pkg is None:
            # Before giving up, try to infer from cwd: if exactly one candidate
            # owns a source_dir that contains (or equals) the current directory,
            # that's almost certainly what the user means.
            try:
                cwd = Path.cwd().resolve()
            except Exception:
                cwd = None
            cwd_matches = []
            if cwd is not None:
                for m in matches:
                    sd = m.get("source_dir")
                    if not sd:
                        continue
                    try:
                        sd_resolved = Path(sd).resolve()
                    except Exception:
                        continue
                    if cwd == sd_resolved or sd_resolved in cwd.parents:
                        cwd_matches.append(m)
            if len(cwd_matches) == 1:
                pkg = cwd_matches[0]["pkg"]
                print(f"note: inferred --pkg {pkg} from cwd ({cwd}).", file=sys.stderr)
            else:
                # Ambiguous: two dists claim the same binary. Don't pick for the
                # user — show the options and exit. Surgical uninstall is always
                # opt-in via --pkg.
                print(f"error: multiple packages declare the binary '{name}':", file=sys.stderr)
                for m in matches:
                    live = _shim_owner(name)
                    marker = "  (LIVE — runs when you type the binary)" if m["pkg"] == live else "  (masked)"
                    print(f"       - {m['pkg']}{marker}", file=sys.stderr)
                print(f"", file=sys.stderr)
                print(f"Pick one with --pkg:", file=sys.stderr)
                for m in matches:
                    print(f"    cliche uninstall {name} --pkg {m['pkg']}", file=sys.stderr)
                sys.exit(1)
        filtered = [m for m in matches if m["pkg"] == pkg]
        if not filtered:
            print(f"error: --pkg {pkg!r} matches no dist claiming binary '{name}'.", file=sys.stderr)
            print(f"       Candidates: {', '.join(m['pkg'] for m in matches)}", file=sys.stderr)
            sys.exit(1)
        registry = filtered[0]
    elif matches:
        registry = matches[0]
        # If --pkg was given for an unambiguous entry, enforce it matches — so
        # `uninstall foo --pkg wrong` fails loudly instead of removing foo.
        if pkg is not None and registry["pkg"] != pkg:
            print(f"error: binary '{name}' is owned by '{registry['pkg']}', not '{pkg}'.",
                  file=sys.stderr)
            sys.exit(1)
    else:
        registry = None
    uv_path = shutil.which("uv")
    tool_entry = None
    directory = None

    if registry is None:
        # Legacy fallback: explicit -d passed, or give up with a helpful error.
        if module_dir is None:
            print(f"error: no CLI named '{name}' is installed via cliche.", file=sys.stderr)
            print(f"       run `cliche ls` to see what's installed.", file=sys.stderr)
            sys.exit(1)
        directory = Path(module_dir)
    else:
        # Defensive: refuse to uninstall the `cliche` binary itself via this
        # path. Aliases that happen to be backed by the cliche package (e.g.
        # `cliche install myalias -p cliche`) ARE removable — only the primary
        # `cliche` binary is protected, because uninstalling it would leave
        # `cliche uninstall` / `cliche ls` inoperable on the same machine.
        if name == "cliche":
            print(f"error: refusing to uninstall the `cliche` binary via `cliche uninstall`.", file=sys.stderr)
            print(f"       use `pip uninstall cliche` or `uv tool uninstall cliche`.",
                  file=sys.stderr)
            sys.exit(1)
        if registry["mode"] == "tool":
            tool_entry = {"binary": name, "pkg": registry["pkg"],
                          "env_path": registry["env_path"], "ver": "?"}
        # Prefer editable source dir (lets us clean up generated files); else
        # use explicit -d; else try cwd as a last resort; else skip cleanup.
        if registry["source_dir"]:
            directory = registry["source_dir"]
        elif module_dir:
            directory = Path(module_dir)
        else:
            # Fallback: direct_url.json can be missing in edge cases (stale
            # metadata, mismatched interpreters, installs done by older
            # cliche versions). If cwd has a pyproject.toml naming our
            # package, it's almost certainly the source dir.
            cwd = Path.cwd()
            cwd_pyproject = cwd / "pyproject.toml"
            if cwd_pyproject.exists():
                cwd_pkg = _get_package_name_from_pyproject(cwd_pyproject.read_text())
                if cwd_pkg == registry["pkg"]:
                    directory = cwd
                    print(f"(editable source not recorded in dist metadata; "
                          f"falling back to cwd {cwd} — pyproject.toml matches)",
                          file=sys.stderr)
        # else: directory stays None; skip local-file cleanup below.

    # Detect project type (only if we have a local dir)
    setup_cfg = (directory / "setup.cfg") if directory else None
    pyproject = (directory / "pyproject.toml") if directory else None
    is_legacy_project = bool(setup_cfg and setup_cfg.exists())

    # Package name: registry wins; else read from config; else dir name.
    if registry:
        package_name = registry["pkg"]
    elif is_legacy_project:
        package_name = _get_package_name_from_setup_cfg(setup_cfg.read_text()) \
            or directory.name.replace("-", "_")
    elif pyproject and pyproject.exists():
        package_name = _get_package_name_from_pyproject(pyproject.read_text()) \
            or directory.name.replace("-", "_")
    else:
        package_name = directory.name.replace("-", "_") if directory else name

    print(f"Uninstalling package: {package_name}")
    sub_env = _subprocess_env_with_writable_cache()
    if tool_entry and uv_path:
        # `uv tool uninstall` expects the TOOL / package name, not the binary.
        # The tool name equals the Python package name, so pass package_name.
        # (Early versions of this code passed the binary name — that silently
        # no-ops because uv's tool registry is keyed on package, not script.)
        print(f"(detected uv-tool install: {tool_entry['env_path']})")
        subprocess.run(
            [uv_path, "tool", "uninstall", package_name],
            capture_output=False, env=sub_env,
        )
    elif uv_path:
        subprocess.run(
            [uv_path, "pip", "uninstall", "--python", sys.executable, package_name],
            capture_output=False, env=sub_env,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", package_name],
            capture_output=False, env=sub_env,
        )

    # Zombie check: the pip/uv uninstall may say "not installed" (stale
    # egg-info on PYTHONPATH, orphaned dist-info, etc.) while the entry
    # point is still reachable via importlib.metadata. Re-query; if the
    # binary is still listed, try to surgically strip it from the backing
    # metadata so the user isn't lied to.
    still_there = _existing_entry_point(name)
    if still_there is not None:
        zombie_path = _zombie_metadata_path(name)
        if zombie_path is not None:
            removed = _strip_entry_from_egg_info(zombie_path, name)
            if removed:
                cleaned_zombie = f"zombie entry '{name}' in {zombie_path}"
                print(f"note: removed {cleaned_zombie}")
                # Re-check after the strip — if the egg-info is now empty of
                # console_scripts, the user might want to delete the whole dir,
                # but we leave that call to them.
            else:
                _print_zombie_diagnostic(name, zombie_path)
                sys.exit(1)
        else:
            _print_zombie_diagnostic(name, None)
            sys.exit(1)

    cleaned = []
    init_file = None  # set only when we have a local dir

    # Local-dir cleanup only happens when we know the source (editable install
    # or explicit -d). Non-editable installs have no files to tidy up here.
    if directory is not None:
        # Entry point from config
        if is_legacy_project and setup_cfg:
            content = setup_cfg.read_text()
            pattern = rf'^\s*{re.escape(name)}\s*=.*\n?'
            new_content = re.sub(pattern, '', content, flags=re.MULTILINE)
            if new_content != content:
                setup_cfg.write_text(new_content)
                cleaned.append(f"entry '{name}' from {setup_cfg}")
        elif pyproject and pyproject.exists():
            content = pyproject.read_text()
            pattern = rf'^{re.escape(name)}\s*=.*\n?'
            new_content = re.sub(pattern, '', content, flags=re.MULTILINE)
            if new_content != content:
                pyproject.write_text(new_content)
                cleaned.append(f"entry '{name}' from {pyproject}")
            # Strip now-empty [project.scripts] section
            if _strip_empty_project_scripts(pyproject):
                cleaned.append(f"empty [project.scripts] in {pyproject}")
            # If pyproject is now byte-identical to our fresh-install template
            # (no user edits, no other entry points, no extra deps), delete it.
            if pyproject.read_text() == _pristine_pyproject(package_name):
                pyproject.unlink()
                cleaned.append(f"{pyproject} (auto-generated, no user edits)")

        # Find the actual package dir (flat, subdir, or src layout)
        pkg_dir = _find_package_dir(directory, package_name)

        # _cliche.py (lives in pkg_dir, which for flat layout == directory)
        cliche_file = pkg_dir / "_cliche.py"
        if cliche_file.exists():
            cliche_file.unlink()
            cleaned.append(str(cliche_file))

        # Auto-generated __init__.py (only if content still matches our marker)
        init_file = pkg_dir / "__init__.py"
        if init_file.exists():
            try:
                content = init_file.read_text().strip()
                if content == AUTO_INIT_MARKER:
                    init_file.unlink()
                    cleaned.append(f"{init_file} (auto-generated)")
            except OSError:
                pass

        # Runtime cache
        cache_file = _remove_runtime_cache(package_name, pkg_dir)
        if cache_file:
            cleaned.append(f"runtime cache {cache_file}")

        # *.egg-info
        for egg in _remove_egg_info(directory, package_name):
            cleaned.append(str(egg))

        # __pycache__ in the package dir (Python bytecode — always regenerable)
        pycache = pkg_dir / "__pycache__"
        if pycache.is_dir():
            shutil.rmtree(pycache)
            cleaned.append(str(pycache))

    # Shell autocomplete: strip the hook from every rc we know about. Safe to
    # run even for tool-less / non-autocomplete installs — it's a no-op when
    # no matching line exists.
    for rc in _unregister_autocomplete(name):
        cleaned.append(f"autocomplete hook in {rc}")

    print()
    if cleaned:
        print("Cleaned up:")
        for item in cleaned:
            print(f"  - {item}")
    # Advise on anything we intentionally did NOT touch
    leftover_hints = []
    if init_file is not None and init_file.exists():
        leftover_hints.append(f"{init_file} (has your code — left intact)")
    if leftover_hints:
        print("\nLeft intentionally (inspect if you want a pristine dir):")
        for item in leftover_hints:
            print(f"  - {item}")

    print(f"\nUninstalled '{name}'")


LLM_GUIDE = """\
# cliche — LLM guide for building CLI tools

## Overview
cliche turns any Python package into a fast CLI by scanning for @cli-decorated
functions via AST (not imports), caching results by mtime, and lazy-importing only
the module of the function actually invoked. Startup stays sub-100ms even for large
packages.

## Minimal workflow
    cd my_project/                         # a dir with .py files
    cliche install mytool              # creates __init__.py, pyproject.toml,
                                           # runs `pip install -e .`
    mytool <command> [args...]             # run any @cli function
    cliche uninstall mytool            # remove the entry point
    cliche ls                          # show every CLI installed via cliche
    cliche migrate                     # re-align existing installs with the
                                           # current cliche entry-point format

DO NOT pre-create pyproject.toml or __init__.py yourself — `install` generates
them. If pyproject.toml already exists, `install` edits it in place (adds
[project.scripts] and dependencies). Just run `install` in any directory
with .py files. Cliche writes NO `.py` files into your package; the entry
point routes through `cliche.launcher:launch_<pkg>` (a cliche-owned trampoline
that cleans sys.path before importing the user package).

install flags: `-p PKG` (import name if ≠ dir), `-t` (isolated uv tool venv),
`-f` (force over existing binary name).

Always invoke the installed binary — e.g. `mytool add 2 3`.
If install fails, fix the install, don't work around it.

## Simplest recipe (use this unless you have a reason not to)
    cd <your-project-dir>                  # a directory with a real name
    # write ONE .py file at top level (no subdirectories, no pre-made pyproject)
    # with `from cliche import cli` and `@cli` on your functions
    cliche install <binary_name>       # that's it — do NOT pass `.` anywhere

Do NOT create a subdirectory matching the package name yourself — `install`
handles layout detection. Do NOT pass `.` as a binary name or `--package-name`
(it's not a valid Python identifier and will be rejected).

## Install modes
  cliche install mytool            # editable install into the current Python env
  cliche install mytool --tool     # isolated venv via `uv tool install` (requires uv)

Use `--tool` when you don't want the CLI to pollute your active Python env (e.g.
you're installing a CLI globally, not as part of a project you're developing).
Each --tool CLI lives in its own venv under ~/.local/share/uv/tools/<pkg>/;
manage them with `uv tool {list,upgrade,uninstall}` or `cliche uninstall`.
`cliche ls` shows MODE = edit / site / tool so you can tell them apart.

IMPORTANT — two distinct names:
  BINARY NAME  = what you type in the shell      (positional arg to `install`)
  IMPORT NAME  = what you use in `from X import` (defaults to CURRENT DIR name,
                 override with `--package-name / -p`)

They are NOT the same thing. Examples:

  # dir = my_project/, want both names to match:
  cliche install my_project
    → shell: `my_project ...`   python: `from my_project.cli import x`

  # dir = claude_compress/, want short binary but keep import readable:
  cliche install clompress --package-name claude_compress
    → shell: `clompress ...`    python: `from claude_compress.cli import x`

If you installed with mismatched names by accident, `cliche uninstall <binary>`
then reinstall with `--package-name` set correctly.

After `install`, the binary `mytool` is on PATH. Editing source files does NOT
require reinstall — the cache auto-updates on each invocation via mtime checks.

## The @cli decorator
    from cliche import cli

    @cli
    def hello(name: str, excited: bool = False):
        '''Greet someone.'''
        print(f"Hello {name}{'!' if excited else '.'}")

`@cli` is a no-op at runtime; detection is purely AST-based. That means:
  - The decorator MUST appear literally as `@cli` or `@cli("group")` in source.
  - Aliasing (`c = cli; @c`) will NOT be detected.
  - `@some.cli` or `@some.cli("group")` IS detected (attribute form).
  - Only the FIRST @cli decorator per function is used.

## Grouping into subcommands
    @cli("db")
    def migrate(): ...

    @cli("db")
    def seed(): ...

Invoked as:  `mytool db migrate`  /  `mytool db seed`
Ungrouped `@cli` functions are top-level:  `mytool hello Bob --excited`

## Parameter syntax (auto-mapped to argparse)
Signature element                   → CLI form
----------------------------------- -------------------------------------------
`x: str` (no default)               positional, required
`x: str = "a"` (has default)        `--x VALUE`
`flag: bool = False`                `--flag`        (store_true)
`flag: bool = True`                 `--no-flag`     (store_false; see gotcha #3)
`x: int`, `x: float`                argparse coerces via `type=int` / `type=float`
`p: Path`                           any str value, arrives as pathlib.Path
`p: Path = Path("/tmp")`            `--p VALUE`, default = Path("/tmp")
`p: Path | None = None`             `--p VALUE`, default None (union w/ None)
`p: Path | str`                     union handler picks the most-specific mapped type (Path)
`when: date`                        positional, accepts YYYY-MM-DD strictly
`ts: datetime`                      positional, accepts ISO-8601 (bare date too)
`items: tuple[int, ...] = ()`       `--items 1 2 3`   (preferred; each element → int)
`items: tuple[Path, ...] = ()`      `--items a b c`   (preferred; each element → Path)
`items: tuple[Path, ...]`           positional, nargs='+'  (each element → Path)
`items: list[int] = []`             `--items 1 2 3`   (works; see mutable-default gotcha)
`tags: dict[str, int] = {}`         `--tags a=1 b=2`  (KEY=VALUE pairs, K/V coerced)
`tags: dict[str, Path] = {}`        same but values coerced to Path
`cfg: MyBaseModel` (pydantic)       each BaseModel field becomes its own `--<field>` flag
`cfg: MyBaseModel | None = None`    same; union with None forwards through
`p: MyCallable` (user-defined)      argparse calls MyCallable(token); value is its return
`mode: MyEnum`                      positional with choices
`mode: MyEnum.V = MyEnum.FOO`       `--mode FOO` (choices auto-populated)
`*args`, `**kwargs`                 IGNORED
`self`, `cls`                       IGNORED (methods auto-instantiate the class)

Underscores in param names become dashes on the CLI (`dry_run` → `--dry-run`);
both forms are accepted internally. Short flags (e.g. `-n` for `--name`) are
auto-generated when unambiguous.

Gotchas:
  - `None` as a default is fine — the arg is just omitted from the call when unset.
  - Type annotations are parsed from source text, NOT evaluated. That said, the
    following shapes ARE recognised and get real coercion at argparse time:
      * primitives: `str`, `int`, `float`, `bool`
      * `Path` / `pathlib.Path` (+ `Optional[Path]`, `Path | None`, `Path | str`)
      * `date`, `datetime` (ISO parsing)
      * container elements: `list[T]`, `tuple[T, ...]` — each element coerced to T
      * `dict[K, V]` — key/value coerced per annotation
      * enums (Python `Enum` subclasses and protobuf `*_pb2` enums)
      * pydantic `BaseModel` subclasses — fields become individual flags
    Aliased imports (`import pathlib as p; x: p.Path`) are NOT resolved.
    `list[CustomType]` / `dict[K, CustomType]` fall back to `str` for the unknown
    side (the known side still coerces).
  - Defaults are ALSO parsed from source, not evaluated. Recognised literal
    forms: strings, numbers, True/False/None, tuples/lists of literals, enum
    member access (`Mode.FAST`), and Path call-form with a string literal
    (`Path("/tmp")`, `pathlib.Path('/etc')`). Computed expressions
    (`str(DEFAULT_DB)`, `Path.home()`, `os.getenv(...)`, `Path("/x") / "sub"`,
    `MY_CONST + 1`) are stored verbatim as a STRING and silently become a
    bogus default. For computed defaults, use a sentinel (`""` or `None`)
    and resolve inside the function:
        def cmd(db_path: str = ""):
            db_path = db_path or str(DEFAULT_DB)
  - Return values that are non-None are auto-printed as `json.dumps(result, indent=2)`,
    falling back to `print(result)` if not JSON-serializable.
  - `async def` functions are supported and run via `asyncio.run`.
  - For variadic collection parameters, PREFER `tuple[T, ...] = ()` over
    `list[T] = []`. Both work identically on the CLI (each invocation is a
    fresh process, so Python's mutable-default gotcha doesn't cross
    invocations), but the tuple form (a) sidesteps the classic footgun when
    the same function is later called from non-CLI Python code, (b)
    communicates read-only intent, and (c) doesn't trip `ruff B006` /
    `flake8-bugbear`. Use `list[T] = []` only when the body really needs to
    mutate the collection.

## Enums
Both Python `Enum` classes and protobuf `*_pb2.py` enums are auto-detected:
  - Python: any class inheriting from `Enum` in scanned files.
  - Protobuf: enum values parsed from `_pb2.py` files.

The values populate argparse `choices=`. At invoke time, the string CLI value is
converted to the actual enum member by `getattr(EnumClass, value)`.

The type annotation must literally contain the enum class name (e.g. `Exchange`,
`Exchange.V`, `list[Exchange.V]`). Aliased imports are NOT resolved.

Enum defaults written in qualified form (`color: Color = Color.RED`) are
handled correctly — the `Color.` prefix is stripped before the member lookup,
so invoking the command with no flag produces the enum member, not a str.

## Dict parameters (`dict[K, V]`)
    @cli
    def run(tags: dict[str, int] = {}):
        print(tags)

Invoke with one flag and whitespace-separated KEY=VALUE pairs:
    mytool run --tags alpha=1 beta=2 gamma=3
    → {'alpha': 1, 'beta': 2, 'gamma': 3}

Or repeat the flag — entries accumulate:
    mytool run --tags alpha=1 --tags beta=2

Key and value types come from the annotation (`str`, `int`, `float`, `bool`,
`Path`, `pathlib.Path`). Unknown types fall back to `str`. The first `=` per
pair splits — the rest is the value, so `url=https://x.com/?q=1` works.
Missing `=` produces `argument --tags: expected KEY=VALUE, got '…'`.

Note: `bool` as a value type is a footgun (`bool("False") == True`); prefer
str/int/float/Path for dict values.

## Pydantic `BaseModel` parameters
Annotating a parameter with a `BaseModel` subclass expands each field into
its own CLI flag:

    from pydantic import BaseModel
    from cliche import cli

    class Config(BaseModel):
        host: str
        port: int = 8080
        tls: bool = False

    @cli
    def serve(cfg: Config):
        print(cfg.model_dump())

Invocation:
    mytool serve --host acme.local --port 9000 --tls   # all fields
    mytool serve --host acme.local                     # port+tls defaulted

Required fields (no default) become required flags; pydantic runs full
validation when the model is constructed, so bad types produce a clear
`error: failed to construct Config for --cfg: …` and exit code 2.

The type annotation must name the class directly (`cfg: Config` or
`cfg: Config | None = None`); aliased imports aren't resolved.

## Custom type callables
If you annotate a parameter with a callable (function or class) defined in
the same module, `cliche` passes it to argparse as `type=`. argparse
invokes the callable on each token and wraps any `ValueError` /
`argparse.ArgumentTypeError` into a clean `argument <name>: <msg>` error
BEFORE your function runs.

    import argparse
    from cliche import cli

    def Port(s: str) -> int:
        n = int(s)
        if not (1 <= n <= 65535):
            raise argparse.ArgumentTypeError(f"port out of range: {n}")
        return n

    def NonEmpty(s: str) -> str:
        if not s:
            raise ValueError("must be non-empty")
        return s

    @cli
    def serve(port: Port, host: NonEmpty = "localhost"):
        print(f"{host}:{port}")

    # mytool serve 70000         → argument port: port out of range: 70000
    # mytool serve 80 --host ""  → argument --host: invalid NonEmpty value: ''

This is the escape hatch for single-field validation that primitives can't
express (range checks, URL parsing, semver parsing, etc.). Reach for a
pydantic `BaseModel` when you need a cluster of related fields or
cross-field constraints.

The callable must be a simple identifier in the same module — parameterised
forms (`list[Port]`, `Port | None`) don't trigger this path. Enum classes
and pydantic `BaseModel` subclasses are intentionally skipped here since
they have their own dedicated handling with richer semantics.

Note: writing a callable where a type is expected makes mypy / pyright
unhappy (`Variable 'Port' is not valid as a type`). Runtime is unaffected;
if you lint strictly, ignore that specific line or use a pydantic model.

## Docstrings
The first line of the docstring becomes the command help summary. `:param name:
description` lines are parsed into per-argument help text.

    @cli
    def deploy(env: str, dry_run: bool = False):
        '''Deploy the service.

        :param env: target environment (prod/stage)
        :param dry_run: skip actual deploy
        '''

## Built-in global flags (always available)
    -h, --help        Standard help
    --cli             Show CLI + Python version info
    --llm-help             Print compact LLM-friendly help (all commands + enums)
    --pdb             Drop into (i)pdb post-mortem on exception
    --pip [args]      Run pip from the CLI's Python env (e.g. `mytool --pip list`)
    --pyspy N         Profile for N seconds, write speedscope JSON
    --timing          Print detailed startup / parse timings to stderr
    --skip-gen        Skip cache regeneration this invocation

Note: `mytool --llm-help` is the canonical way an LLM can discover every command,
signature, default, and enum. Prefer it over `--help` for machine consumption.

## Layout rules
  - Flat layout:   `my_project/foo.py` + `my_project/__init__.py`  →  package = dir name.
  - Subdir layout: `my_project/mypkg/__init__.py`                  →  package = `mypkg`.
  - src layout:    `my_project/src/mypkg/__init__.py`              →  package = `mypkg`.

Every `.py` file under the package is scanned (recursively). Skipped: `.git`,
`__pycache__`, `venv`, `.venv`, `env`, `.env`, `node_modules`, any dir starting
with `.`. Cliche writes no code into your package — the entry point routes
through a cliche-owned launcher.

## Caching
Cache lives at `$XDG_CACHE_HOME/cliche/<pkg>_<hash>.json` (default
`~/.cache/cliche/`). Per-file mtime check; full AST reparse only on changed
files; parallel parse when >4 files change.

To force a clean rebuild, delete the cache file or touch the source files.

## Common gotchas (bite-order)
  1. Forgetting `from cliche import cli` — the import isn't strictly required
     for detection (AST-based) but IS required at runtime so Python doesn't
     NameError on the decorator.
  2. Using `@cli()` with no args — this is treated as `@cli()` call form; works,
     but prefer bare `@cli` for ungrouped functions.
  3. A bool param with default `True` becomes `--no-NAME`, not `--NAME`. There is
     no way to "set it to True" on the CLI because it's already the default.
  4. List/tuple positionals consume the REST of argv (nargs='+'/'*'), so put them
     last in the signature.
  5. Pick ONE: `return` OR `print(...)`, never both. A non-None return value
     is auto-printed as JSON — if you also call `print(...)`, the user sees
     the output twice. For simple CLI functions, just `return` the result.
  6. Functions named with reserved CLI words (e.g. `help`) will shadow `--help`
     handling. Pick another name or wrap in `@cli("group")`.
  7. `self`-methods: cliche instantiates the owning class with zero args. If
     `__init__` requires args, it will fail — use plain functions instead.
  8. Editable install (`pip install -e .`) is the default. If you move the
     project directory, reinstall to update the entry-point path.
  9. After renaming/removing a function, run the CLI once to refresh the cache
     (or delete the cache file).

## End-to-end example
    # my_tool/commands.py
    from cliche import cli
    from enum import Enum

    class Mode(Enum):
        FAST = "fast"
        SAFE = "safe"

    @cli
    def greet(name: str, shout: bool = False):
        '''Greet the user.

        :param name: who to greet
        :param shout: uppercase the output
        '''
        msg = f"hello {name}"
        print(msg.upper() if shout else msg)

    @cli("db")
    async def migrate(mode: Mode = Mode.SAFE, steps: int = 1):
        '''Run DB migrations.'''
        return {"ran": steps, "mode": mode.value}

Then:
    cliche install my_tool
    my_tool greet Alice --shout
    my_tool db migrate --mode FAST --steps 3
    my_tool --llm-help         # discover everything
"""


def _tilde_home(p: str) -> str:
    """Abbreviate $HOME prefix to ~ for display.

    Keeps the PATH column in `cliche ls` readable by collapsing the
    universally-repeated user-home prefix. Only matches a full path-segment
    boundary (so `/home/pascal` never partially-matches something like
    `/home/pascal2/...`).
    """
    if not p or p == "?":
        return p
    home = os.path.expanduser("~")
    if home and home != "/" and (p == home or p.startswith(home + os.sep)):
        return "~" + p[len(home):]
    return p


def list_installed():
    """List CLI tools installed via cliche (detected by the `_cliche.py` entry point)."""
    import hashlib
    import json
    import os
    from importlib.metadata import PackageNotFoundError, entry_points, version

    cache_home = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    cache_dir = Path(cache_home) / "cliche"

    rows = []
    for ep in entry_points(group="console_scripts"):
        pkg_name = _parse_cliche_entry(ep.value)
        if pkg_name is None:
            continue

        # Resolve package dir
        try:
            mod = __import__(pkg_name)
            pkg_path = Path(mod.__file__).parent if mod.__file__ else None
        except Exception:
            pkg_path = None

        # Version
        try:
            ver = version(pkg_name)
        except PackageNotFoundError:
            ver = "?"

        # Editable install? A package can have MULTIPLE concurrent metadata
        # records (a stale .egg-info on PYTHONPATH AND a fresh .dist-info from
        # `uv pip install -e .`) — importlib's `distribution(name)` returns
        # only the first. Iterate `distributions(name=name)` and accept the
        # editable signal from ANY of them, so the table shows `edit` when the
        # user's most recent install was editable, regardless of leftover
        # egg-info metadata competing in sys.path.
        editable = False
        try:
            import importlib.metadata as im
            for dist in im.distributions(name=pkg_name):
                direct_url = dist.read_text("direct_url.json")
                if direct_url:
                    data = json.loads(direct_url)
                    if data.get("dir_info", {}).get("editable"):
                        editable = True
                        break
                # uv's PEP 660 editable installs drop a `__editable__.<pkg>-<ver>.pth`
                # next to the dist-info instead of writing direct_url.json.
                # Detect by the presence of that .pth sibling.
                meta_path = getattr(dist, "_path", None)
                if meta_path:
                    parent = Path(meta_path).parent
                    if any(parent.glob(f"__editable__.{pkg_name}-*.pth")):
                        editable = True
                        break
        except Exception:
            pass

        # Source dir alive?
        exists = pkg_path is not None and pkg_path.exists()

        # Count @cli commands from the runtime cache, if present
        n_cmds = None
        if pkg_path:
            dir_hash = hashlib.md5(str(pkg_path).encode()).hexdigest()[:8]
            cache_file = cache_dir / f"{pkg_name}_{dir_hash}.json"
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text())
                    n_cmds = sum(len(f.get("functions", []))
                                 for f in data.get("files", {}).values())
                except Exception:
                    pass

        rows.append({
            "binary": ep.name,
            "pkg": pkg_name,
            "ver": ver,
            "mode": "edit" if editable else "site",
            "exists": exists,
            "n_cmds": n_cmds,
            "path": _tilde_home(str(pkg_path)) if pkg_path else "?",
        })

    # uv-tool-installed CLIs (live in isolated venvs, not in this Python env)
    for t in _uv_tool_cliche_entries():
        # Resolve source dir from the tool's editable install metadata
        env_path = Path(t["env_path"])
        src_dir = None
        try:
            for de in (env_path / "lib").glob("python*/site-packages"):
                sp = de
                dist_info_dirs = list(sp.glob(f"{t['pkg']}*.dist-info"))
                for d in dist_info_dirs:
                    direct_url = d / "direct_url.json"
                    if direct_url.exists():
                        data = json.loads(direct_url.read_text())
                        url = data.get("url", "")
                        if url.startswith("file://"):
                            src_dir = url[len("file://"):]
                            break
                if src_dir:
                    break
        except Exception:
            pass

        exists = bool(src_dir) and Path(src_dir).exists()
        n_cmds = None
        if src_dir:
            dir_hash = hashlib.md5(src_dir.encode()).hexdigest()[:8]
            cache_file = cache_dir / f"{t['pkg']}_{dir_hash}.json"
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text())
                    n_cmds = sum(len(f.get("functions", []))
                                 for f in data.get("files", {}).values())
                except Exception:
                    pass

        rows.append({
            "binary": t["binary"],
            "pkg": t["pkg"],
            "ver": t["ver"],
            "mode": "tool",
            "exists": exists if src_dir else True,  # tool env itself exists
            "n_cmds": n_cmds,
            "path": _tilde_home(src_dir or t["env_path"]),
        })

    if not rows:
        print("No cliche-installed CLIs found.")
        return

    rows.sort(key=lambda r: r["binary"])

    # Per-binary: which pkg's shim is actually on disk? This drives the
    # LIVE / MASKED column below. Cached once per distinct binary name because
    # _shim_owner reads the shim file (cheap but not free).
    live_owners: dict[str, str | None] = {}
    for bin_name in {r["binary"] for r in rows}:
        live_owners[bin_name] = _shim_owner(bin_name)

    # STATUS:
    #   ok     = single-row binary, nothing conflicting (the common case)
    #   LIVE   = this row WINS a duplicate — typing the binary invokes it
    #   MASKED = this row LOSES a duplicate — another pkg's code runs instead
    # LIVE and ok render green; MASKED renders yellow (see STATUS_COLOR).
    from collections import Counter
    binary_counts = Counter(r["binary"] for r in rows)
    masked = 0
    for r in rows:
        if binary_counts[r["binary"]] == 1:
            r["status"] = "ok"
        else:
            live = live_owners.get(r["binary"])
            if live == r["pkg"]:
                r["status"] = "LIVE"
            else:
                r["status"] = "MASKED"
                masked += 1

    # Fancy table: unicode box chars + optional ANSI color on STATUS. No extra
    # deps. Color turns off automatically when stdout isn't a TTY (piped / CI).
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def paint(s: str, code: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if use_color else s

    STATUS_COLOR = {
        "ok":     "32",   # green (healthy single-row)
        "LIVE":   "32",   # green (wins a duplicate)
        "MASKED": "33",   # yellow (loses a duplicate — runs someone else's code)
    }

    def colored_status(s: str, width: int) -> str:
        padded = f"{s:<{width}}"
        code = STATUS_COLOR.get(s)
        return paint(padded, code) if code else padded

    # Display-mode mapping: internal "tool" → "uv-tool" so the table column
    # matches the footer vocabulary ("3 uv-tool") and the user's mental model.
    # We don't rename the internal key — callers like uninstall's tool-vs-pip
    # branch still rely on "tool".
    display_mode = {"tool": "uv-tool"}
    for r in rows:
        r["_mode_display"] = display_mode.get(r["mode"], r["mode"])

    # Column widths (raw text, no ANSI — we pad BEFORE colorising)
    headers = ("BINARY", "IMPORT", "VER", "MODE", "CMDS", "STATUS", "PATH")
    widths = {
        "BINARY": max(len("BINARY"), max(len(r["binary"]) for r in rows)),
        "IMPORT": max(len("IMPORT"), max(len(r["pkg"]) for r in rows)),
        "VER":    max(len("VER"),    max(len(r["ver"]) for r in rows)),
        "MODE":   max(len("MODE"),   max(len(r["_mode_display"]) for r in rows)),
        "CMDS":   max(len("CMDS"),   4),
        "STATUS": max(len("STATUS"), max(len(r["status"]) for r in rows)),
        "PATH":   max(len("PATH"),   max(len(r["path"]) for r in rows)),
    }

    # Auto-fit to terminal width: if the total would overflow, shrink PATH only,
    # truncating from the LEFT (the tail of a path is the informative part —
    # `/very/long/prefix/foo` truncates to `…/prefix/foo`). All other columns
    # keep their natural widths; we never squeeze BINARY/IMPORT/etc.
    term_width = shutil.get_terminal_size((120, 24)).columns
    # Per-cell overhead: " value " (2 spaces) + the leading '│'; plus one
    # trailing '│' at the end. 7 cells → 7 bars + 7*2 padding + 1 trailing.
    overhead = len(headers) + len(headers) * 2 + 1
    non_path = sum(w for k, w in widths.items() if k != "PATH")
    available_for_path = term_width - overhead - non_path
    MIN_PATH = 20  # below this, just overflow — PATH column becomes unreadable anyway
    if available_for_path < widths["PATH"] and available_for_path >= MIN_PATH:
        widths["PATH"] = available_for_path

    def fit_path(p: str, w: int) -> str:
        """Left-truncate p to width w, marking with `…` when cut."""
        if len(p) <= w:
            return p
        if w <= 1:
            return "…"
        return "…" + p[-(w - 1):]

    # Unicode box drawing — light, single-line. Works in every modern terminal.
    V = "│"
    H = "─"
    TL, TR, BL, BR = "┌", "┐", "└", "┘"
    T, B, L, R, X = "┬", "┴", "├", "┤", "┼"

    def hr(left: str, mid: str, right: str) -> str:
        return left + mid.join(H * (widths[h] + 2) for h in headers) + right

    top = hr(TL, T, TR)
    sep = hr(L, X, R)
    bot = hr(BL, B, BR)

    def fmt_header() -> str:
        # Pad first, then colorise the padded text — ANSI codes don't count
        # toward visible width, so padding AFTER colorising would misalign.
        cells = [f" {paint(f'{h:<{widths[h]}}', '1')} " for h in headers]
        return V + V.join(cells) + V

    def fmt_row(r: dict) -> str:
        cmds = "?" if r["n_cmds"] is None else str(r["n_cmds"])
        path = fit_path(r["path"], widths["PATH"])
        cells = [
            f" {r['binary']:<{widths['BINARY']}} ",
            f" {r['pkg']:<{widths['IMPORT']}} ",
            f" {r['ver']:<{widths['VER']}} ",
            f" {r['_mode_display']:<{widths['MODE']}} ",
            f" {cmds:>{widths['CMDS']}} ",
            f" {colored_status(r['status'], widths['STATUS'])} ",
            f" {path:<{widths['PATH']}} ",
        ]
        return V + V.join(cells) + V

    print(top)
    print(fmt_header())
    print(sep)
    for r in rows:
        print(fmt_row(r))
    print(bot)

    # Footer: one-line totals + a conditional MASKED hint when relevant.
    total = len(rows)
    by_mode = {m: sum(1 for r in rows if r["mode"] == m) for m in ("edit", "site", "tool")}
    print()
    print(f"{total} tool(s): {by_mode['edit']} editable, {by_mode['site']} site, "
          f"{by_mode['tool']} uv-tool, {masked} masked. "
          f"CMDS = @cli count from runtime cache ({cache_dir}); '?' = cache not yet created.")
    if masked:
        print("MASKED = typing this binary runs a different package's code (or no shim "
              "exists). Uninstall the masked entry to clean up the duplicate.")


# ---------------------------------------------------------------------------
# Migration registry
#
# Each migration is a small record: a stable `id`, a one-line `summary`, a
# `needs(install) -> bool` predicate that is side-effect-free, and an
# `apply(install) -> (ok, message)` action. Appending a new migration is
# the only extension point — `cliche migrate` iterates the registry and
# applies whichever migrations say they're needed for each installed CLI,
# so it stays a no-op on already-migrated installs and naturally handles
# future transformations that we haven't shipped yet.
#
# Invariants:
#   - `needs()` NEVER mutates state; it only reads install metadata.
#   - `apply()` must be idempotent on re-run (a migration that has already
#     been applied should either be filtered out by `needs()` or be safely
#     re-appliable).
#   - `id` is stable — it appears in dry-run output and logs; renaming
#     breaks user mental models.
# ---------------------------------------------------------------------------

@dataclass
class Migration:
    id: str
    summary: str                                                  # one line
    needs: "Callable[[dict], bool]"                               # predicate
    apply: "Callable[[dict], tuple[bool, str]]"                   # mutator


def _apply_launcher_entry(install: dict) -> tuple[bool, str]:
    """Reinstall the CLI in place so its entry-point target is rewritten to
    `cliche.launcher:launch_{pkg}` and any legacy `_cliche.py` orphan is
    removed by `install()`'s `_remove_legacy_cliche_module` step.

    No-op when the install is already on the launcher format — the
    migration's `needs()` filters those out before we get here.
    """
    src = install.get("source_dir")
    if not src or not Path(src).exists():
        return False, f"source dir missing: {src!r}"
    cmd = ["cliche", "install", install["binary"], "-p", install["pkg"],
           "--force", "--no-autocomplete"]
    if install["mode"] == "uv-tool":
        cmd.append("--tool")
    result = subprocess.run(cmd, cwd=src)
    if result.returncode != 0:
        return False, f"`cliche install` exit {result.returncode}"
    return True, "ok"


MIGRATIONS: list[Migration] = [
    Migration(
        id="launcher-entry",
        summary=(
            "Route shim through `cliche.launcher:launch_{pkg}` instead of "
            "`{pkg}._cliche:main`; removes orphan _cliche.py from the "
            "package dir. Defends against sys.path shadow-imports."
        ),
        needs=lambda install: install.get("entry_value", "").endswith("._cliche:main"),
        apply=_apply_launcher_entry,
    ),
    # Future migrations: append here. Each gets its own `id`, a pure
    # `needs()` predicate, and an idempotent `apply()`.
]


def _enumerate_cliche_installs(only: str | None = None) -> list[dict]:
    """Return every cliche-managed CLI on the system as a dict of raw facts.

    Fields:
      binary       — binary name on PATH
      pkg          — Python import name of the user package
      mode         — "pip" | "uv-tool"
      entry_value  — raw `[project.scripts]` target (e.g. `cliche.launcher:launch_foo`)
      source_dir   — best-effort path to the project dir we can re-install from
      shim_text    — uv-tool only; contents of the binary shim script

    `needs()` predicates in `MIGRATIONS` pattern-match on these fields.
    Keeping the fields raw-and-factual means adding a new migration
    doesn't require adding a new pre-computed boolean to every install.
    """
    import json as _json
    from importlib.metadata import entry_points

    def _resolve_source_dir(pkg: str) -> str | None:
        src = _editable_source_dir(pkg)
        if src is not None:
            return str(src)
        try:
            mod = __import__(pkg)
            if not getattr(mod, "__file__", None):
                return None
            here = Path(mod.__file__).parent
        except Exception:
            return None
        cur = here
        for _ in range(6):
            if any((cur / marker).exists()
                   for marker in ("pyproject.toml", "setup.py", "setup.cfg")):
                return str(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
        return str(here)

    installs: list[dict] = []

    for ep in entry_points(group="console_scripts"):
        pkg = _parse_cliche_entry(ep.value)
        if pkg is None:
            continue
        if only is not None and ep.name != only:
            continue
        installs.append({
            "binary": ep.name,
            "pkg": pkg,
            "mode": "pip",
            "entry_value": ep.value,
            "source_dir": _resolve_source_dir(pkg),
            "shim_text": "",
        })

    for entry in _uv_tool_cliche_entries():
        if only is not None and entry["binary"] != only:
            continue
        try:
            shim_text = Path(entry["binary_path"]).read_text()
        except OSError:
            shim_text = ""
        # uv-tool installs don't expose `entry_value` via importlib.metadata
        # from our interpreter — we synthesise it from the shim so migrations
        # can write `needs()` against one field regardless of install mode.
        if f"launch_{entry['pkg']}" in shim_text:
            entry_value = f"cliche.launcher:launch_{entry['pkg']}"
        elif f"{entry['pkg']}._cliche" in shim_text:
            entry_value = f"{entry['pkg']}._cliche:main"
        else:
            entry_value = ""
        src_dir = None
        try:
            env_path = Path(entry["env_path"])
            for site_packages in (env_path / "lib").glob("python*/site-packages"):
                for dist_info in site_packages.glob(f"{entry['pkg']}*.dist-info"):
                    direct_url = dist_info / "direct_url.json"
                    if direct_url.exists():
                        data = _json.loads(direct_url.read_text())
                        url = data.get("url", "")
                        if url.startswith("file://"):
                            src_dir = url[len("file://"):]
                            break
                if src_dir:
                    break
        except Exception:
            pass
        installs.append({
            "binary": entry["binary"],
            "pkg": entry["pkg"],
            "mode": "uv-tool",
            "entry_value": entry_value,
            "source_dir": src_dir,
            "shim_text": shim_text,
        })

    return installs


def migrate(only: str | None = None, dry_run: bool = False, yes: bool = False) -> int:
    """Apply every registered migration to every install that needs it.

    Drives `MIGRATIONS`: each install is matched against every migration's
    `needs()` predicate. Installs with no pending migrations are skipped
    silently (the no-op case when nothing has changed since the last run).
    Installs with one or more pending migrations get each applied in
    registry order. Returns non-zero on any migration failure.
    """
    installs = _enumerate_cliche_installs(only)

    if only is not None and not installs:
        print(f"error: no cliche-installed CLI named {only!r}.", file=sys.stderr)
        print("       run `cliche ls` to see what's installed.", file=sys.stderr)
        return 1

    # Build the plan: per-install list of migrations that apply.
    plan: list[tuple[dict, list[Migration]]] = []
    for inst in installs:
        needed = [m for m in MIGRATIONS if m.needs(inst)]
        if needed:
            plan.append((inst, needed))

    if not plan:
        print(
            f"Nothing to migrate: all {len(installs)} cliche-installed CLI(s) "
            f"are already up to date on the {len(MIGRATIONS)} registered "
            f"migration(s)."
        )
        return 0

    migration_summary = ", ".join(m.id for m in MIGRATIONS)
    print(
        f"Registered migrations: {len(MIGRATIONS)} ({migration_summary})\n"
        f"Installs needing at least one migration: {len(plan)} of {len(installs)}\n"
    )

    bin_w = max(len(inst["binary"]) for inst, _ in plan)
    id_w = max(len(m.id) for m in MIGRATIONS)
    for inst, needed in plan:
        src = inst.get("source_dir") or "<unknown>"
        print(f"  {inst['binary']:<{bin_w}}  pkg={inst['pkg']:<14} "
              f"mode={inst['mode']:<8} src={src}")
        for m in needed:
            print(f"    - [{m.id:<{id_w}}] {m.summary}")
    print()

    if dry_run:
        print("(--dry-run: no changes made)")
        return 0

    if not yes:
        try:
            resp = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    successes = 0
    total = sum(len(ms) for _, ms in plan)
    failures: list[tuple[str, str, str]] = []
    for inst, needed in plan:
        for m in needed:
            print(f"\n-- applying [{m.id}] to {inst['binary']} ({inst['mode']}) --")
            ok, msg = m.apply(inst)
            if ok:
                successes += 1
            else:
                failures.append((inst["binary"], m.id, msg))

    print(f"\nDone. Applied {successes}/{total} migration(s).")
    if failures:
        print("Failures:")
        for binary, mig_id, msg in failures:
            print(f"  {binary}  [{mig_id}]  {msg}")
        return 2
    return 0


def main_cli():
    """Entry point for the cliche command."""
    import argparse

    parser = argparse.ArgumentParser(description="Install or uninstall cliche CLI tools")
    # Early --version short-circuit: print just the cliche version and
    # exit, so scripts can do `cliche --version` without triggering the
    # subcommand parser's "required" logic.
    if "--version" in sys.argv[1:]:
        try:
            from cliche import __version__ as _v
        except ImportError:
            _v = "unknown"
        print(_v)
        return
    parser.add_argument(
        "--llm-help",
        action="store_true",
        help="Print an LLM-oriented guide to building tools with cliche, then exit",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Install subcommand
    install_parser = subparsers.add_parser("install", help="Install a CLI tool")
    install_parser.add_argument("name", help="Name of the CLI command to create")
    install_parser.add_argument("--module-dir", "-d", help="Project directory (default: current directory)")
    install_parser.add_argument("--no-pip", action="store_true", help="Skip pip install -e . (just generate files)")
    install_parser.add_argument(
        "--package-name", "-p",
        help="Python import name (default: directory name). Use when the binary name should differ from the import name.",
    )
    install_parser.add_argument(
        "--force", "-f", action="store_true",
        help="Install even if another CLI with this binary name already exists (not recommended).",
    )
    install_parser.add_argument(
        "--tool", "-t", action="store_true",
        help="Use `uv tool install --editable` so the CLI lives in its own isolated venv (requires uv).",
    )
    install_parser.add_argument(
        "--no-autocomplete", action="store_true",
        help="Skip appending the argcomplete hook to ~/.bashrc / ~/.zshrc / ~/.config/fish/config.fish.",
    )

    # Uninstall subcommand
    uninstall_parser = subparsers.add_parser("uninstall", help="Uninstall a CLI tool")
    uninstall_parser.add_argument("name", help="Name of the CLI command to remove")
    uninstall_parser.add_argument("--module-dir", "-d", help="Project directory (default: current directory)")
    uninstall_parser.add_argument(
        "--pkg", "-p",
        help="Disambiguate when two packages declare the same binary name "
             "(pass the Python import name from the IMPORT column in `cliche ls`).",
    )

    # ls subcommand (list installed tools)
    subparsers.add_parser("ls", help="List CLI tools installed via cliche in this Python environment")

    # migrate subcommand (apply registered migrations to existing CLIs)
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Apply any registered cliche migrations to existing CLI installs",
        description=(
            "Walks `cliche ls`, matches each install against a registry of\n"
            "cliche migrations, and applies whichever migrations still need\n"
            "to run on that install. Already-migrated installs are silently\n"
            "skipped, so `cliche migrate` is safe to run repeatedly — the\n"
            "second run on an up-to-date system is a no-op.\n\n"
            "Each migration is a small record (id, summary, needs, apply).\n"
            "When cliche ships a new migration, it's appended to the registry\n"
            "and the next `cliche migrate` run picks it up without any\n"
            "change to this command. To see which migrations would run on\n"
            "which install without touching anything, use `--dry-run`.\n\n"
            "Currently registered:\n"
            "  - launcher-entry:\n"
            "      Rewrites [project.scripts] from `{pkg}._cliche:main` to\n"
            "      `cliche.launcher:launch_{pkg}` and removes the legacy\n"
            "      `_cliche.py` trampoline from the package dir (only when\n"
            "      that file still carries cliche's generation marker —\n"
            "      user-authored files are left alone). Closes a shadow-\n"
            "      import footgun where a stray `{pkg}.py` on sys.path (e.g.\n"
            "      a leading-colon PYTHONPATH that puts CWD on the path) can\n"
            "      silently beat the real installed package during import.\n\n"
            "Migration is opt-in — unmigrated CLIs keep working. You'd run\n"
            "this when you want to adopt the defense on existing installs,\n"
            "or when a future cliche release adds a new migration that you\n"
            "want to apply across the board."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    migrate_parser.add_argument(
        "only", nargs="?",
        help="Binary name to migrate (default: migrate every legacy install).",
    )
    migrate_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be migrated without making any changes.",
    )
    migrate_parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

    args = parser.parse_args()

    if args.llm_help:
        print(LLM_GUIDE)
        return

    if args.command == "install":
        install(args.name, module_dir=args.module_dir, no_pip=args.no_pip,
                package_name=args.package_name, force=args.force, tool=args.tool,
                no_autocomplete=args.no_autocomplete)
    elif args.command == "uninstall":
        uninstall(args.name, module_dir=args.module_dir, pkg=args.pkg)
    elif args.command == "ls":
        list_installed()
    elif args.command == "migrate":
        sys.exit(migrate(only=args.only, dry_run=args.dry_run, yes=args.yes))
    else:
        parser.print_help()


if __name__ == "__main__":
    main_cli()
