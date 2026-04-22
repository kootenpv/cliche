#!/usr/bin/env python3
"""Maintainer-only release helper — gitignored, not shipped.

Usage:
    python deploy.py bump {patch,minor,major}      [--commit]
    python deploy.py deploy                        [--test] [--no-tag] [--no-push]

`bump` delegates to `uv version --bump <part>`, which rewrites the
`version = "..."` line in pyproject.toml (single source of truth —
`__version__` is read via importlib.metadata at runtime). Pre-1.0 a `minor`
bump is allowed to be breaking; post-1.0 follow semver strictly.

`deploy` runs `uv build` and `uv publish` on the already-bumped version.
Pass `--test` to route to TestPyPI. By default it also tags `vX.Y.Z` and
pushes it; opt out with `--no-tag` / `--no-push`.
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _require_uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        sys.exit("error: `uv` not found on PATH. install with: pipx install uv "
                 "(or see https://docs.astral.sh/uv/)")
    return uv


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


def _load_pypirc_token(section: str) -> tuple[str | None, str | None]:
    """Read ~/.pypirc and return (username, password) for the given section.

    uv publish doesn't read ~/.pypirc (twine does). Bridge it by exporting
    UV_PUBLISH_TOKEN / UV_PUBLISH_USERNAME / UV_PUBLISH_PASSWORD so the user's
    existing twine config keeps working.
    """
    pypirc = Path.home() / ".pypirc"
    if not pypirc.exists():
        return None, None
    cp = configparser.ConfigParser()
    try:
        cp.read(pypirc)
    except configparser.Error:
        return None, None
    if section not in cp:
        return None, None
    return cp[section].get("username"), cp[section].get("password")


def _read_version() -> str:
    """Read version from pyproject.toml without invoking uv (works even if
    uv momentarily removes / rewrites the file)."""
    m = VERSION_RE.search(PYPROJECT.read_text())
    if not m:
        sys.exit("error: no `version = \"...\"` line found in pyproject.toml")
    return m.group(1)


def cmd_bump(args: argparse.Namespace) -> None:
    uv = _require_uv()
    before = _read_version()
    # --frozen: just rewrite pyproject.toml, do NOT re-lock the project.
    # cliche is a library and does not commit a uv.lock; without this,
    # even a plain `uv version --bump` would synthesize one as a side effect.
    _run([uv, "version", "--bump", args.part, "--frozen"])
    after = _read_version()
    print(f"bumped: {before} → {after}")
    if args.commit:
        _run(["git", "add", str(PYPROJECT)])
        _run(["git", "commit", "-m", f"release: {after}"])
        _run(["git", "tag", f"v{after}"])
        print(f"committed + tagged v{after} (push with: git push && git push --tags)")


def cmd_deploy(args: argparse.Namespace) -> None:
    uv = _require_uv()
    version = _read_version()

    # Clean any stale dist/ so an old wheel can't slip into the upload.
    dist = ROOT / "dist"
    if dist.exists():
        for f in dist.iterdir():
            f.unlink()

    _run([uv, "build"])

    publish_cmd = [uv, "publish"]
    if args.test:
        publish_cmd += ["--publish-url", "https://test.pypi.org/legacy/"]

    # Bridge ~/.pypirc → UV_PUBLISH_* env vars (uv doesn't read .pypirc itself).
    # Env vars already set win — don't clobber them.
    env = os.environ.copy()
    if "UV_PUBLISH_TOKEN" not in env and "UV_PUBLISH_PASSWORD" not in env:
        section = "testpypi" if args.test else "pypi"
        user, password = _load_pypirc_token(section)
        if password:
            if user == "__token__" or password.startswith("pypi-"):
                env["UV_PUBLISH_TOKEN"] = password
            else:
                env["UV_PUBLISH_USERNAME"] = user or ""
                env["UV_PUBLISH_PASSWORD"] = password
            print(f"(using credentials from ~/.pypirc [{section}])")

    print("$", " ".join(publish_cmd), flush=True)
    subprocess.run(publish_cmd, check=True, env=env)

    tag = f"v{version}"
    if not args.no_tag:
        # Tolerate: tag may already exist if `bump --commit` created it.
        _run(["git", "tag", tag], check=False)
    if not args.no_push:
        _run(["git", "push"], check=False)
        _run(["git", "push", "origin", tag], check=False)

    target = "TestPyPI" if args.test else "PyPI"
    print(f"done: published {version} to {target}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Maintainer-only release helper (bump + deploy via uv).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bump", help="Bump patch/minor/major via `uv version --bump`")
    b.add_argument("part", choices=["patch", "minor", "major"])
    b.add_argument("--commit", action="store_true",
                   help="Also `git add pyproject.toml && git commit && git tag vX.Y.Z`")
    b.set_defaults(func=cmd_bump)

    d = sub.add_parser("deploy", help="`uv build` + `uv publish` (+ tag + push)")
    d.add_argument("--test", action="store_true", help="Publish to TestPyPI instead of PyPI")
    d.add_argument("--no-tag", action="store_true", help="Skip `git tag vX.Y.Z`")
    d.add_argument("--no-push", action="store_true", help="Skip `git push` / tag push")
    d.set_defaults(func=cmd_deploy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
