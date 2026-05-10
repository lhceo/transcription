#!/usr/bin/env bash
# End-to-end .app build script.
#   - ensures bundled/ffmpeg/ffmpeg exists (downloads if missing)
#   - cleans previous build/ and dist/ directories
#   - runs py2app
#
# Run from the repository root:
#   bash scripts/build_app.sh
#
# Requires the project venv to have py2app installed:
#   ~/transcription/venv/bin/pip install py2app
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-$HOME/transcription/venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: Python interpreter not found at $PYTHON" >&2
    echo "Set PYTHON env var to the venv's python:" >&2
    echo "  PYTHON=/path/to/venv/bin/python bash scripts/build_app.sh" >&2
    exit 1
fi

echo "==> Using Python: $PYTHON"
"$PYTHON" --version

# 1. Make sure ffmpeg is present
if [ ! -x "$REPO_ROOT/bundled/ffmpeg/ffmpeg" ]; then
    echo "==> Bundled ffmpeg missing; downloading"
    bash "$REPO_ROOT/scripts/download_ffmpeg.sh"
fi

# 2. Make sure py2app is installed
if ! "$PYTHON" -c "import py2app" 2>/dev/null; then
    echo "==> Installing py2app into venv"
    "$PYTHON" -m pip install --upgrade py2app
fi

# 3. Clean previous artifacts
echo "==> Cleaning previous build/ and dist/"
rm -rf "$REPO_ROOT/build" "$REPO_ROOT/dist"

# 4. Build
echo "==> Running py2app"
"$PYTHON" setup.py py2app

# 5. Done
APP="$REPO_ROOT/dist/Noto.app"
if [ -d "$APP" ]; then
    echo
    echo "==> Build complete: $APP"
    du -sh "$APP" 2>/dev/null || true
    echo
    echo "Open the app:"
    echo "  open '$APP'"
fi
