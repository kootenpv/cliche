"""Console-script launcher for cliche-installed CLIs.

Every cliche-installed CLI is registered in `[project.scripts]` as

    <binary> = "cliche.launcher:launch_<pkg>"

rather than the historical

    <binary> = "<pkg>._cliche:main"

The change exists to defend against sys.path pollution that can shadow the
user package before `_cliche.py` loads. The most common trigger is a
leading-colon `PYTHONPATH`: Python treats empty sys.path entries as CWD at
import time, so a stray `<pkg>.py` anywhere the CLI is run from can be
resolved as `<pkg>` instead of the real installed package. When the shim
does `from <pkg>._cliche import main` directly, the resolution of `<pkg>`
happens before any cliche code runs — there is nowhere to intervene.

Routing through this launcher inverts that order. The shim does
`from cliche.launcher import launch_<pkg>`, which always resolves cleanly
because `cliche.launcher` lives in the same environment as the installed
CLI. PEP 562's module-level `__getattr__` manufactures the per-package
closure on demand; inside the closure we clean sys.path before importing
`<pkg>._cliche`, closing the shadowing window.

Kept deliberately small and stdlib-only so it is cheap to import as the
very first step of every cliche-installed binary.
"""
from __future__ import annotations


def _clean_sys_path() -> None:
    """Drop sys.path entries that point at CWD (plus explicit empty entries).

    Closes the "leading-colon PYTHONPATH" shadow footgun: bashrc lines like
    `export PYTHONPATH=$PYTHONPATH:/path` expand to `PYTHONPATH=:/path` when
    PYTHONPATH was previously unset, producing an empty entry. Python
    materialises empty entries as CWD on sys.path at startup (as a literal
    resolved path, not an empty string). A stray `<pkg>.py` in whatever
    directory the binary is invoked from then shadows the real installed
    package before any cliche code runs.

    Globally-installed CLIs have no legitimate reason to import Python
    files from the invoking CWD; dropping those entries aligns with
    `python -P` / `-I` semantics and resolves the footgun generically —
    whether the user's PYTHONPATH is misconfigured, a stale `.pth` file
    injected CWD, or anything else put CWD on the path.
    """
    import sys
    import os
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    cleaned = []
    for p in sys.path:
        if not p:
            continue  # empty string → CWD at import time; drop
        if cwd is not None:
            try:
                if os.path.abspath(p) == cwd:
                    continue  # literal CWD entry (the materialised case); drop
            except OSError:
                pass
        cleaned.append(p)
    sys.path[:] = cleaned


def _make_launcher(pkg: str):
    def _launch():
        _clean_sys_path()
        # Self-alias: `cliche install sdm -p cliche` registers `sdm` as an
        # alternate binary name for cliche itself. Scanning cliche's package
        # for `@cli` decorators would find none (cliche's real entry is the
        # argparse-based `cliche.install.main_cli`), yielding an empty CLI.
        # Dispatch there directly so the alias behaves like running `cliche`.
        if pkg == "cliche":
            from cliche.install import main_cli
            return main_cli()
        # Normal user package: skip the historical `{pkg}._cliche` hop and
        # call the runtime directly, so the user's package stays free of any
        # cliche-written .py file.
        from cliche.runtime import run_package_cli
        return run_package_cli(pkg)
    _launch.__name__ = f"launch_{pkg}"
    _launch.__qualname__ = f"launch_{pkg}"
    return _launch


def __getattr__(name: str):
    # pip / uv tool generates a shim that does `from cliche.launcher import
    # launch_<pkg>`. Module-level `__getattr__` (PEP 562, Python 3.7+)
    # manufactures the launcher lazily, so we don't need an entry registered
    # per package in this file.
    prefix = "launch_"
    if name.startswith(prefix):
        return _make_launcher(name[len(prefix):])
    raise AttributeError(name)
