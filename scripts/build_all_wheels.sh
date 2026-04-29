#!/usr/bin/env bash
# Build every cliche wheel into dist/.
#
# Order matters: platform-specific wheels are built first because each one
# uses `wheel tags --remove` to consume its py3-none-any source. The
# fallback py3-none-any wheel is built LAST so it's the only any-tagged
# wheel left in dist/.
#
# Currently builds locally on this Linux host:
#   - linux-x86_64       (native compile)
#   - linux-aarch64      (cross-compile, optional — skipped if toolchain absent)
#   - py3-none-any       (no binary, compile-on-install fallback)
#
# To also build macos-arm64 on a remote Mac (e.g. `ssh localmac`), see the
# `REMOTE_HOST` envvar block below. Set REMOTE_HOST=localmac to enable;
# unset to skip.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p dist
rm -f dist/cliche-*.whl

# ----------------------------------------------------------------------
# Platform wheels (each produces ONE wheel, removing the any-tagged source)
# ----------------------------------------------------------------------

echo "[1] linux-x86_64 (native)"
./scripts/build_one_wheel.sh linux-x86_64

if command -v aarch64-linux-gnu-gcc >/dev/null; then
    echo "[2] linux-aarch64 (cross-compile)"
    ./scripts/build_one_wheel.sh linux-aarch64
else
    echo "[2] linux-aarch64 — toolchain missing, skipping (install with: "
    echo "    sudo pacman -S aarch64-linux-gnu-gcc qemu-user-static qemu-user-static-binfmt)"
fi

if [ -n "${REMOTE_HOST:-}" ]; then
    echo "[3] macos-arm64 (remote on $REMOTE_HOST)"
    REMOTE_TMP="/tmp/cliche-build-$$"
    trap 'ssh "$REMOTE_HOST" "rm -rf $REMOTE_TMP" 2>/dev/null || true' EXIT
    ssh "$REMOTE_HOST" "mkdir -p $REMOTE_TMP"
    rsync -a --delete \
        --exclude '__pycache__' --exclude '*.pyc' \
        --exclude '.git' --exclude '.venv' --exclude '.pytest_cache' \
        --exclude '.mypy_cache' --exclude '.codex' \
        --exclude 'dist' --exclude 'build' --exclude '*.egg-info' \
        --exclude 'cliche/_bin/clichec' \
        ./ "$REMOTE_HOST:$REMOTE_TMP/"
    ssh "$REMOTE_HOST" "cd $REMOTE_TMP && ./scripts/build_one_wheel.sh macos-arm64"
    rsync -a "$REMOTE_HOST:$REMOTE_TMP/dist/cliche-"*"macosx_11_0_arm64.whl" dist/
else
    echo "[3] macos-arm64 — REMOTE_HOST not set, skipping"
fi

# ----------------------------------------------------------------------
# Fallback py3-none-any (last — no platform-build will consume it)
# ----------------------------------------------------------------------

echo "[fallback] py3-none-any (no bundled binary, compile-on-install)"
mkdir -p cliche/_bin
find cliche/_bin -type f ! -name '.gitkeep' -delete
PY="${PYTHON:-$(command -v python3 || command -v python)}"
"$PY" -m build --wheel --no-isolation 2>&1 | grep -E "Successfully built" | sed 's/^/  /'

echo
echo "all wheels in dist/:"
ls -la dist/cliche-*.whl
