"""Locate / build the C fast-fail launcher (`clichec`).

The native launcher lives at ``cliche/clichec.c`` in this package. We compile
it on demand into ``$XDG_CACHE_HOME/cliche/clichec-<cliche_version>`` so version
bumps invalidate stale binaries in place. ``fast-shim`` then writes a shell
wrapper that exec's this binary and falls back to the Python launcher when
the C side returns 64 ("I can't handle this").

Failure modes are deliberately silent: if there's no C compiler, or the build
fails, ``binary_path()`` returns None and the caller writes a Python-only
shim — same behaviour as before this module existed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def source_path() -> Path:
    return Path(__file__).parent / "clichec.c"


def _state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "cliche"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _binary_name() -> str:
    try:
        from cliche import __version__ as v
    except ImportError:
        v = "unknown"
    return f"clichec-{v}"


def _bundled_binary() -> Path | None:
    """Path to a clichec binary shipped inside the wheel, or None.

    Platform-specific wheels (e.g. linux-x86_64, macos-arm64) include a
    pre-compiled `cliche/_bin/clichec` so users on those platforms get the
    fast path without needing a C compiler. The pure `py3-none-any`
    fallback wheel ships the .c source only — `ensure_built()` then
    compiles on first use.
    """
    p = Path(__file__).parent / "_bin" / "clichec"
    try:
        if p.exists() and os.access(p, os.X_OK):
            return p
    except OSError:
        pass
    return None


def _user_compile_target() -> Path:
    """Where `build()` writes the user-compiled clichec binary.

    Lives under XDG_CACHE_HOME so it survives Python venv churn (`pip install`
    inside a fresh venv won't trigger another compile if the binary still
    matches the cliche version). Always a writable location, never the
    package's own directory — that's read-only on system installs.
    Co-located with the dispatch JSON cache used by clichec itself.
    """
    return _state_dir() / _binary_name()


def binary_path() -> Path:
    """Path to a usable clichec binary, preferring a wheel-bundled one.

    Used by `install_fast_shim` to embed an absolute clichec path into the
    shell wrapper. Returning the bundled path when present means the
    wrapper points directly at the binary inside site-packages (no
    XDG_CACHE_HOME indirection), surviving `XDG_CACHE_HOME` changes. Falling
    back to the user-cache target lets the same logic work for the pure
    fallback wheel where the user compiled clichec themselves.
    """
    bundled = _bundled_binary()
    if bundled is not None:
        return bundled
    return _user_compile_target()


def _find_cc() -> str | None:
    return (os.environ.get("CC")
            or shutil.which("cc")
            or shutil.which("gcc")
            or shutil.which("clang"))


def build(verbose: bool = False) -> Path | None:
    """Compile clichec.c → user-cache target. Returns the path on success.

    Idempotent: if the user-cache binary already exists and is newer than
    the source, skip. Returns None on any failure (no compiler, source
    missing, compile error). Always writes to the user cache, never the
    bundled location — package directories are read-only on system installs.
    """
    src = source_path()
    if not src.exists():
        return None
    out = _user_compile_target()
    try:
        if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
            return out
    except OSError:
        pass
    cc = _find_cc()
    if not cc:
        if verbose:
            print("clichec: no C compiler found (set CC or install cc/gcc/clang)",
                  file=sys.stderr)
        return None
    cmd = [cc, "-std=c99", "-O2", "-Wall", "-Wextra",
           "-o", str(out), str(src)]
    if verbose:
        print("clichec build:", " ".join(cmd), file=sys.stderr)
    try:
        r = subprocess.run(cmd, capture_output=not verbose, text=True)
    except OSError as e:
        if verbose:
            print(f"clichec: compile failed to launch: {e}", file=sys.stderr)
        return None
    if r.returncode != 0:
        if verbose and r.stderr:
            print(r.stderr, file=sys.stderr)
        return None
    return out


def ensure_built(verbose: bool = False) -> Path | None:
    """Return path to a usable clichec binary, preferring bundled-then-built.

    Resolution order:
      1. wheel-bundled binary at `cliche/_bin/clichec` — instant, no compile
      2. user-cache binary at `$XDG_CACHE_HOME/cliche/clichec-<ver>` — survives
         Python env churn
      3. compile from `clichec.c` into the user cache, return that
      4. None on failure (no compiler available)

    Steps 1–3 short-circuit as soon as one succeeds, so the cost on a wheel
    user is one `Path.exists()` + `os.access()` call.
    """
    bundled = _bundled_binary()
    if bundled is not None:
        return bundled
    out = _user_compile_target()
    if out.exists() and os.access(out, os.X_OK):
        try:
            if out.stat().st_mtime >= source_path().stat().st_mtime:
                return out
        except OSError:
            pass
    return build(verbose=verbose)


WRAPPER_MARKER = "# cliche fast-shim wrapper"


def render_wrapper(binary_name: str, package_name: str, pkg_dir: str,
                   python_exe: str, clichec: str, cache_hash: str) -> str:
    """Render the shell wrapper that runs clichec then falls back to Python.

    The wrapper:
      1. Computes the cache file path from XDG_CACHE_HOME + pkg + dir-hash.
      2. Tries clichec; if exit code != 64, exits with that code (handled).
      3. Falls through to the Python launcher with the original argv.

    Layout decisions:
      - Hash-encodes pkg_dir at install time (matches runtime.py's
        ``_get_cache_path``); editable-install moves invalidate the wrapper,
        but the C side just falls through, so worst case is loss of the
        fast-fail acceleration until ``cliche fast-shim`` is rerun.
      - Embeds absolute paths (clichec, python) for predictability — if either
        moves, the wrapper falls through to system PATH lookup.
    """
    return f"""#!/bin/sh
{WRAPPER_MARKER} for {binary_name} (cliche-installed)
PKG="{package_name}"
PKG_DIR_HASH="{cache_hash}"
CLICHEC="{clichec}"
PYTHON="{python_exe}"
CACHE_HOME="${{XDG_CACHE_HOME:-$HOME/.cache}}"
CACHE_FILE="$CACHE_HOME/cliche/${{PKG}}_${{PKG_DIR_HASH}}.json"
if [ -x "$CLICHEC" ] && [ -f "$CACHE_FILE" ]; then
    # CLICHEC_PROG carries the wrapper's filename so error messages and help
    # text say "mathlib" rather than the unrelated path of the C binary.
    # Done via env because POSIX sh can't override argv[0] across exec.
    CLICHEC_PROG="${{0##*/}}" "$CLICHEC" "$CACHE_FILE" "$PKG" "$@"
    rc=$?
    # Only honour rc=0 (success) and rc=1 (handled error like unknown-command).
    # Anything else — 64 (defer), 139 (SIGSEGV), 134 (SIGABRT), 137 (OOM-kill),
    # or any other unexpected code — falls through to the Python launcher.
    # That keeps the binary working even if clichec hits a bug or new env
    # quirk; the Python path is the canonical execution surface.
    case $rc in
        0|1) exit $rc ;;
    esac
fi
# Fallback: full Python launcher (handles dispatch + anything clichec deferred).
# We rewrite sys.argv[0] so run.py sees the binary name (e.g. "scd_solo")
# rather than "-c" — single-command-dispatch (run.py:~2104) keys on
# `prog_name == single_func_name`, which only works when sys.argv[0] is the
# binary path. POSIX sh has no `exec -a NAME`, so we do the override inside
# the Python -c snippet.
exec "$PYTHON" -c "import sys; sys.argv[0] = '$0'; from cliche.launcher import launch_${{PKG}}; launch_${{PKG}}()" "$@"
"""


def _resolve_installed_binary(binary_name: str) -> str | None:
    """Find the installed shim for `binary_name`, even when the venv's bin
    dir isn't on the caller's PATH.

    `shutil.which` only sees PATH; running `cliche install` from outside an
    activated venv (e.g. `python -m cliche.install`) means the freshly-
    written shim at `<venv>/bin/<name>` is invisible to which() and
    auto-apply silently fails. We try shutil.which first (covers system
    installs and activated venvs), then fall back to sysconfig's scripts
    path (covers fresh-venv installs without activation).
    """
    found = shutil.which(binary_name)
    if found:
        return found
    import sysconfig
    scripts = sysconfig.get_path("scripts")
    if scripts:
        candidate = Path(scripts) / binary_name
        if candidate.exists():
            return str(candidate)
    return None


def install_fast_shim(binary_name: str, package_name: str, pkg_dir: str,
                      verbose: bool = False,
                      target_path: str | None = None) -> tuple[bool, str]:
    """Replace the pip-generated console-script for `binary_name` with a shell
    wrapper that exec's clichec first, falling back to the Python launcher.

    `target_path` lets callers pin the exact shim file to rewrite. Used by
    `cliche.launcher._maybe_self_upgrade_shim` which already knows the
    canonical path via `sys.argv[0]` — without pinning, a `shutil.which`
    fallback could match a same-named shim in a different venv on PATH and
    rewrite the wrong file. (Real bug observed: a fresh-venv install of
    `clout` ran the launcher inside the new venv's Python, but
    `shutil.which("clout")` resolved to a system-wide editable install
    on PATH and the upgrade overwrote that one instead.)

    Returns ``(ok, message)``. On failure the existing shim is left untouched.
    """
    import hashlib

    target = target_path or _resolve_installed_binary(binary_name)
    if not target:
        return False, f"binary '{binary_name}' not on PATH — install it first"

    # Backup the original Python shim so we can recover or recover python_exe.
    try:
        original = Path(target).read_text()
    except OSError as e:
        return False, f"cannot read existing shim {target}: {e}"

    # Extract the python interpreter from the shebang of the existing shim.
    python_exe = sys.executable
    first_line = original.splitlines()[0] if original else ""
    if first_line.startswith("#!") and "python" in first_line:
        python_exe = first_line[2:].strip().split()[0]

    clichec = ensure_built(verbose=verbose)
    if not clichec:
        return False, "clichec binary unavailable (no C compiler?)"

    cache_hash = hashlib.md5(pkg_dir.encode()).hexdigest()[:8]
    wrapper = render_wrapper(
        binary_name=binary_name,
        package_name=package_name,
        pkg_dir=pkg_dir,
        python_exe=python_exe,
        clichec=str(clichec),
        cache_hash=cache_hash,
    )
    tmp = Path(target).with_suffix(".cliche-tmp")
    try:
        tmp.write_text(wrapper)
        tmp.chmod(0o755)
        os.replace(tmp, target)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, f"failed to write wrapper {target}: {e}"
    return True, f"wrote fast-shim wrapper at {target} (clichec={clichec})"


def is_fast_shim(path: str | Path) -> bool:
    """True if the file at `path` is a cliche-written fast-shim wrapper.

    Used by `cliche ls` to fill the SHIM column. We deliberately do NOT
    expose a `restore_python_shim` counterpart: pip's natural reinstall
    flow (`pip install --force-reinstall <pkg>`) produces a stock Python
    shim and serves as the canonical undo if a user ever wants to
    revert. The launcher's auto-upgrade then re-applies on the next run,
    so opting out *permanently* requires the `CLICHE_NO_FAST_SHIM=1`
    env flag instead — see `cliche.launcher._maybe_self_upgrade_shim`
    and `cliche.install.install`.
    """
    try:
        with open(path) as f:
            head = f.read(512)
        return WRAPPER_MARKER in head
    except OSError:
        return False
