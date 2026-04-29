#!/usr/bin/env python3
"""Maintainer-only release helper — gitignored, not shipped.

Usage:
    python deploy.py bump {patch,minor,major}      [--commit]
    python deploy.py deploy                        [--test] [--no-tag] [--no-push] [--remote-host HOST]
    python deploy.py release {patch,minor,major} -m MSG  [--test] [--no-push] [--remote-host HOST]

`bump` delegates to `uv version --bump <part>`, which rewrites the
`version = "..."` line in pyproject.toml (single source of truth —
`__version__` is read via importlib.metadata at runtime). Pre-1.0 a `minor`
bump is allowed to be breaking; post-1.0 follow semver strictly.

`deploy` builds every platform wheel and runs `uv publish` on the already-
bumped version. The build step delegates to `scripts/build_all_wheels.sh`
which produces:
  - cliche-X.Y.Z-py3-none-manylinux2014_x86_64.whl   (native)
  - cliche-X.Y.Z-py3-none-manylinux2014_aarch64.whl  (cross-compile + qemu)
  - cliche-X.Y.Z-py3-none-macosx_11_0_arm64.whl      (remote on the Mac)
  - cliche-X.Y.Z-py3-none-any.whl                    (fallback w/o binary)
Pass `--remote-host HOST` (or set REMOTE_HOST) to enable the macos-arm64
build via ssh+rsync to a Mac. Without it, that wheel is silently skipped
and macOS users fall back to compile-on-install.
Pass `--test` to route uv publish to TestPyPI. By default it also tags
`vX.Y.Z` and pushes it; opt out with `--no-tag` / `--no-push`.

`release` is the one-shot: bump + single commit including your staged files
+ tag + build + publish + push. Use when you want staged changes and the
version bump to land in the same commit. Build runs before the commit so a
broken build fails fast; publish runs after tag, so on publish failure you
can `git reset HEAD~1 && git tag -d vX.Y.Z` locally and retry.
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
LLMS_TXT = ROOT / "llms.txt"
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


def _refresh_llms_txt() -> bool:
    """Regenerate llms.txt from `cliche --llm-help`. Returns True if it changed.

    Run before any commit-creating release step so the checked-in file (and the
    sdist/wheel that pick it up via pyproject.toml) stays in sync with the CLI.
    Falls back gracefully if `cliche` isn't on PATH — better to release without
    refreshing than to block on a missing dev install.
    """
    cliche_bin = shutil.which("cliche")
    if not cliche_bin:
        print("warning: `cliche` not on PATH; skipping llms.txt refresh", file=sys.stderr)
        return False
    result = subprocess.run([cliche_bin, "--llm-help"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"warning: `cliche --llm-help` exited {result.returncode}; skipping llms.txt refresh",
              file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return False
    new = result.stdout
    old = LLMS_TXT.read_text() if LLMS_TXT.exists() else None
    if old == new:
        return False
    LLMS_TXT.write_text(new)
    print(f"refreshed llms.txt ({len(new)} bytes)")
    return True


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
        _refresh_llms_txt()
        to_add = [str(PYPROJECT)]
        if LLMS_TXT.exists():
            to_add.append(str(LLMS_TXT))
        _run(["git", "add", *to_add])
        _run(["git", "commit", "-m", f"release: {after}"])
        _run(["git", "tag", f"v{after}"])
        print(f"committed + tagged v{after} (push with: git push && git push --tags)")


def _build_all_wheels(remote_host: str | None) -> list[Path]:
    """Run scripts/build_all_wheels.sh and return the produced wheel paths.

    Always builds:
      - linux-x86_64 (native cc)
      - linux-aarch64 (cross-compile + qemu smoke-test, IFF the toolchain
        is installed locally — the script auto-skips when it's missing)
      - py3-none-any  (fallback, no bundled binary)

    Builds macos-arm64 only when `remote_host` is provided (or REMOTE_HOST
    is set in the env). Without it, the macOS wheel is omitted and macOS
    users fall back to compile-on-install via the py3-none-any wheel.

    The script itself handles dist/ cleanup and order (platform wheels
    first because they consume the py3-none-any source via `wheel tags
    --remove`; the fallback any-wheel is built last so it survives in
    dist/). We just invoke and verify.
    """
    script = ROOT / "scripts" / "build_all_wheels.sh"
    if not script.exists():
        sys.exit(f"error: {script} missing — wheel build script is required")
    env = os.environ.copy()
    # Empty string = explicit skip (vs the default `mba` which means "use it").
    # Drop REMOTE_HOST from the env entirely so the build script's "skipping"
    # branch fires cleanly — passing an empty REMOTE_HOST would still make
    # the script attempt ssh to "" and fail noisily.
    if remote_host:
        env["REMOTE_HOST"] = remote_host
    else:
        env.pop("REMOTE_HOST", None)
        print("note: macos-arm64 wheel will NOT be built (--remote-host empty)",
              file=sys.stderr)
        print("      macOS users will fall back to compile-on-install via py3-none-any",
              file=sys.stderr)
    print("$", str(script), flush=True)
    subprocess.run([str(script)], check=True, env=env)
    wheels = sorted((ROOT / "dist").glob("cliche-*.whl"))
    if not wheels:
        sys.exit("error: build script ran but produced no wheels in dist/")
    print(f"built {len(wheels)} wheel(s):")
    for w in wheels:
        print(f"  {w.name}  ({w.stat().st_size} bytes)")
    return wheels


def _twine_check(wheels: list[Path]) -> None:
    """Run `twine check` on every built wheel as a metadata sanity gate.

    Twine validates the wheel's Description (long_description rendering,
    classifier list, project URLs) and would catch the kind of mistake
    that publishes successfully but renders broken on the project's
    PyPI page. Cheap (<1s) and a hard line before publish.

    Prefers a standalone `twine` binary on PATH; falls back to `python -m
    twine` so this works in environments where twine was pip-installed
    into a venv but the entrypoint script isn't on PATH (common with
    pyenv/conda layouts).
    """
    twine_bin = shutil.which("twine")
    cmd = [twine_bin, "check", *map(str, wheels)] if twine_bin \
        else [sys.executable, "-m", "twine", "check", *map(str, wheels)]
    print("$", " ".join(cmd), flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("error: twine check failed — fix wheel metadata before publishing")


def _publish(uv: str, test: bool) -> None:
    """Run `uv publish`, bridging ~/.pypirc → UV_PUBLISH_* env vars."""
    publish_cmd = [uv, "publish"]
    if test:
        publish_cmd += ["--publish-url", "https://test.pypi.org/legacy/"]

    # Env vars already set win — don't clobber them.
    env = os.environ.copy()
    if "UV_PUBLISH_TOKEN" not in env and "UV_PUBLISH_PASSWORD" not in env:
        # twine renamed the test section from "pypitest" to "testpypi"; accept both.
        candidates = ["testpypi", "pypitest"] if test else ["pypi"]
        for section in candidates:
            user, password = _load_pypirc_token(section)
            if password:
                if user == "__token__" or password.startswith("pypi-"):
                    env["UV_PUBLISH_TOKEN"] = password
                else:
                    env["UV_PUBLISH_USERNAME"] = user or ""
                    env["UV_PUBLISH_PASSWORD"] = password
                print(f"(using credentials from ~/.pypirc [{section}])")
                break

    print("$", " ".join(publish_cmd), flush=True)
    subprocess.run(publish_cmd, check=True, env=env)


def cmd_deploy(args: argparse.Namespace) -> None:
    uv = _require_uv()
    version = _read_version()

    # Guarantee llms.txt on disk (and therefore in the wheel/sdist) matches the
    # current `cliche --llm-help` output. `deploy` runs against an already-
    # committed state, so if the refresh changes anything the user must commit
    # it before publishing — refusing here is safer than silently shipping a
    # snapshot whose committed copy on GitHub disagrees with the wheel on PyPI.
    if _refresh_llms_txt():
        sys.exit(
            "error: llms.txt was out of date and has been regenerated. "
            "commit it before deploying:\n"
            "    git add llms.txt && git commit -m 'refresh llms.txt'\n"
            "then re-run `python deploy.py deploy`."
        )

    # Multi-platform build: produces py3-none-any (fallback) plus per-arch
    # wheels for the platforms we ship binaries for. Replaces the old single
    # `uv build` which only emitted the py3-none-any.
    wheels = _build_all_wheels(args.remote_host)
    _twine_check(wheels)
    _publish(uv, args.test)

    tag = f"v{version}"
    if not args.no_tag:
        # Tolerate: tag may already exist if `bump --commit` created it.
        _run(["git", "tag", tag], check=False)
    if not args.no_push:
        _run(["git", "push"], check=False)
        _run(["git", "push", "origin", tag], check=False)

    target = "TestPyPI" if args.test else "PyPI"
    print(f"done: published {version} ({len(wheels)} wheels) to {target}")


def cmd_release(args: argparse.Namespace) -> None:
    """One-shot: bump + staged files into a single commit + tag + build + publish + push."""
    uv = _require_uv()

    # 0. Refuse if there are unstaged changes to tracked files — the release
    #    commit only picks up what's staged + the pyproject.toml bump, and
    #    silently leaving other edits behind would be surprising. Untracked
    #    files are allowed (user decides what to add).
    dirty = subprocess.run(["git", "diff", "--quiet"]).returncode
    if dirty:
        sys.exit("error: unstaged changes in tracked files. "
                 "stage them (git add) or stash/revert before releasing.")

    # Also require at least one staged change. `release` is for bundling real
    # work with the version bump; if nothing is staged, the user wants
    # `bump --commit` + `deploy` instead.
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode
    if not staged:
        sys.exit("error: nothing staged. `release` bundles staged changes into the release commit. "
                 "either stage files (git add) or use `bump --commit` + `deploy` for a plain version-only release.")

    # 1. Refresh llms.txt from `cliche --llm-help` and stage it immediately,
    #    so the snapshot shipped on PyPI tracks this release and the file
    #    isn't left as a phantom unstaged change in the working tree.
    if _refresh_llms_txt() and LLMS_TXT.exists():
        _run(["git", "add", str(LLMS_TXT)])

    # 2. Bump version in pyproject.toml.
    before = _read_version()
    _run([uv, "version", "--bump", args.part, "--frozen"])
    version = _read_version()
    tag = f"v{version}"
    print(f"bumped: {before} → {version}")

    # 3. Build now — fail fast if the tree is broken, before touching git.
    #    The build picks up the freshly-written llms.txt from disk and runs
    #    the multi-platform orchestrator (py3-none-any + linux-x86_64 +
    #    optional linux-aarch64 cross-compile + optional macos-arm64 over ssh).
    wheels = _build_all_wheels(args.remote_host)
    _twine_check(wheels)

    # 4. Stage pyproject.toml alongside whatever the user already staged,
    #    then single commit + tag. Without -m, fall back to the same
    #    `release: X.Y.Z` shape `bump --commit` uses, so a version-only
    #    release reads consistently in `git log` regardless of which path
    #    produced it.
    _run(["git", "add", str(PYPROJECT)])
    commit_msg = f"{args.message} (v{version})" if args.message else f"release: {version}"
    _run(["git", "commit", "-m", commit_msg])
    _run(["git", "tag", tag])

    # 5. Publish. If this fails, the commit/tag exist locally but not on
    #    remote — roll back with: git tag -d vX.Y.Z && git reset HEAD~1
    try:
        _publish(uv, args.test)
    except subprocess.CalledProcessError:
        print(f"\npublish failed. to roll back locally:", file=sys.stderr)
        print(f"    git tag -d {tag} && git reset --soft HEAD~1", file=sys.stderr)
        sys.exit(1)

    # 6. Push commit + tag.
    if not args.no_push:
        _run(["git", "push"])
        _run(["git", "push", "origin", tag])

    target = "TestPyPI" if args.test else "PyPI"
    print(f"done: released {version} to {target}")


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

    # `mba` is the maintainer's Apple Silicon laptop reachable over LAN
    # (configured in ~/.ssh/config). Anyone else maintaining the project
    # would override via --remote-host or the REMOTE_HOST env var. Set to
    # the literal string "" to disable the macos-arm64 build entirely.
    DEFAULT_REMOTE = os.environ.get("REMOTE_HOST", "mba")

    d = sub.add_parser("deploy", help="build all wheels + `uv publish` (+ tag + push)")
    d.add_argument("--test", action="store_true", help="Publish to TestPyPI instead of PyPI")
    d.add_argument("--no-tag", action="store_true", help="Skip `git tag vX.Y.Z`")
    d.add_argument("--no-push", action="store_true", help="Skip `git push` / tag push")
    d.add_argument("--remote-host", default=DEFAULT_REMOTE,
                   help=("ssh host for macos-arm64 wheel build (default: %(default)s; "
                         "pass empty string to skip)."))
    d.set_defaults(func=cmd_deploy)

    r = sub.add_parser(
        "release",
        help="One-shot: bump + single commit with staged files + tag + build + publish + push",
    )
    r.add_argument("part", choices=["patch", "minor", "major"])
    r.add_argument(
        "-m", "--message", default=None,
        help=(
            "Commit message; the version suffix `(vX.Y.Z)` is appended "
            "automatically. Omit to default to the bare `release: X.Y.Z` "
            "form (matches what `bump --commit` produces)."
        ),
    )
    r.add_argument("--test", action="store_true", help="Publish to TestPyPI instead of PyPI")
    r.add_argument("--no-push", action="store_true", help="Skip `git push` / tag push")
    r.add_argument("--remote-host", default=DEFAULT_REMOTE,
                   help=("ssh host for macos-arm64 wheel build (default: %(default)s; "
                         "pass empty string to skip)."))
    r.set_defaults(func=cmd_release)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
