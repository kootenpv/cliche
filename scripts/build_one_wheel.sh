#!/usr/bin/env bash
# Build a single platform-tagged cliche wheel.
#
# Compiles cliche/clichec.c for the requested platform, drops the binary
# into cliche/_bin/clichec (which pyproject.toml force-includes), runs
# `python -m build --wheel`, then re-tags the resulting wheel from
# `py3-none-any` to `py3-none-<platform>` via `wheel tags`.
#
# Idempotent: cliche/_bin/ is wiped at start AND end so a stale binary
# from one platform can never sneak into the next platform's wheel.
#
# Usage:
#   scripts/build_one_wheel.sh <linux-x86_64|linux-aarch64|macos-arm64>
#
# Prerequisites per platform:
#   linux-x86_64    cc / gcc on PATH
#   linux-aarch64   aarch64-linux-gnu-gcc on PATH (Arch: pacman -S
#                   aarch64-linux-gnu-gcc qemu-user-static qemu-user-static-binfmt)
#   macos-arm64     Apple clang via Xcode CLT
#
# Plus everywhere: `pip install build wheel hatchling`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Prefer `python3` over `python`. macOS 12.3+ removed the `python`
# symlink (security hardening: Apple wants you to opt into a Python
# version explicitly), so a script that runs on both Linux + Mac has
# to spell it `python3` or detect at runtime. The PYTHON env var lets
# callers override (e.g. point at a venv interpreter for repeatable
# wheel-building under a specific environment).
PY="${PYTHON:-$(command -v python3 || command -v python)}"
if [ -z "$PY" ]; then
    echo "error: no python3 or python on PATH" >&2
    exit 2
fi

# Read the cliche package version from pyproject.toml and inject it into
# clichec at compile time via -DCLICHEC_VERSION=...  This keeps the C
# binary and the wheel's filename in lock-step automatically — every
# `deploy.py bump` rewrites the same `version = "..."` line and the next
# build picks it up. Falls back to "unknown" if the line is missing
# (preserves the build-from-tarball case where pyproject might be stripped).
VERSION=$(awk -F'"' '/^version *=/ {print $2; exit}' pyproject.toml)
: "${VERSION:=unknown}"
VERSION_FLAG="-DCLICHEC_VERSION=\"$VERSION\""

PLATFORM="${1:-}"
case "$PLATFORM" in
    linux-x86_64)
        # manylinux2014 = glibc 2.17. Statically linking libc means the
        # binary survives any glibc on the user's machine, at the cost of
        # ~1MB. Worth it: the manylinux tag's whole purpose is "compatible
        # with old enough glibc"; we just opt out of the constraint entirely.
        TAG="manylinux2014_x86_64"
        CC_CMD=(cc -std=c99 -O2 -Wall -Wextra -static
                "$VERSION_FLAG"
                -o cliche/_bin/clichec cliche/clichec.c)
        ;;
    linux-aarch64)
        TAG="manylinux2014_aarch64"
        CC_CMD=(aarch64-linux-gnu-gcc -std=c99 -O2 -Wall -Wextra -static
                "$VERSION_FLAG"
                -o cliche/_bin/clichec cliche/clichec.c)
        ;;
    macos-arm64)
        # macOS 11 (Big Sur) is the first version that natively supported
        # Apple Silicon, so it's the lowest sensible target — older macOS
        # never ran on arm64 hardware.
        TAG="macosx_11_0_arm64"
        CC_CMD=(cc -std=c99 -O2 -Wall -Wextra -arch arm64
                -mmacosx-version-min=11.0
                "$VERSION_FLAG"
                -o cliche/_bin/clichec cliche/clichec.c)
        ;;
    *)
        echo "usage: $0 {linux-x86_64|linux-aarch64|macos-arm64}" >&2
        exit 2
        ;;
esac

# Wipe any stale binary from a previous build (but preserve .gitkeep so
# pyproject.toml's force-include of cliche/_bin still resolves on the
# fallback py3-none-any build path, where no binary is dropped in).
mkdir -p cliche/_bin
find cliche/_bin -type f ! -name '.gitkeep' -delete

echo "  compile: ${CC_CMD[*]}"
"${CC_CMD[@]}"
chmod +x cliche/_bin/clichec

# Strip debug symbols + section names. Drops Linux static binaries from
# ~940 KB → ~640 KB; macOS arm64 from ~85 KB → ~50 KB. Strip flags differ:
#   - GNU strip (Linux x86_64): --strip-all is the maximum reduction
#   - aarch64-linux-gnu-strip: same flag set as GNU strip
#   - Apple llvm-strip (macOS): -x removes local symbols safely; -S strips debug
# We let `strip` find itself per platform: GNU on Linux, llvm-strip on macOS
# (both available alongside the matching compiler we just used).
case "$PLATFORM" in
    linux-x86_64)   STRIP_CMD=(strip --strip-all cliche/_bin/clichec) ;;
    linux-aarch64)  STRIP_CMD=(aarch64-linux-gnu-strip --strip-all cliche/_bin/clichec) ;;
    macos-arm64)    STRIP_CMD=(strip -x -S cliche/_bin/clichec) ;;
esac
"${STRIP_CMD[@]}" || echo "  warning: strip failed (binary still works, just larger)"

file cliche/_bin/clichec | sed 's/^/  /'

# Optional smoke-test for cross-compiled binaries that the host can run
# transparently via binfmt_misc. Doesn't fail the build if it errors —
# clichec deliberately exits non-zero on bogus argv (we feed it /dev/null
# as the cache file), and we just want to confirm the binary executes.
if [ "$PLATFORM" = "linux-aarch64" ] && command -v qemu-aarch64-static >/dev/null; then
    echo "  qemu smoke-test:"
    cliche/_bin/clichec /dev/null nopkg --help 2>&1 | head -1 | sed 's/^/    /' || true
fi

# Build a normal py3-none-any wheel (the binary in cliche/_bin/ is included
# via pyproject.toml's force-include block). --no-isolation reuses the
# current environment's hatchling install instead of provisioning a fresh
# build venv each call — cuts ~5s per wheel.
echo "  build wheel..."
"$PY" -m build --wheel --no-isolation 2>&1 | grep -E "^Successfully built|cliche-" | tail -2 | sed 's/^/  /'

# Retag from py3-none-any to py3-none-<platform>. --remove deletes the
# untagged source wheel so dist/ ends up with exactly one wheel per call.
WHEEL=$(ls -t dist/cliche-*-py3-none-any.whl | head -1)
echo "  retag $WHEEL -> $TAG"
"$PY" -m wheel tags --remove --platform-tag "$TAG" "$WHEEL" >/dev/null

# Always wipe the binary after build so nothing stale lingers in the
# source tree (also covered by .gitignore but belt-and-braces). Keep
# the .gitkeep so the dir survives for the fallback wheel build.
find cliche/_bin -type f ! -name '.gitkeep' -delete

OUT=$(ls -t dist/cliche-*-${TAG}.whl | head -1)
echo "  ok: $OUT  ($(wc -c < "$OUT") bytes)"
