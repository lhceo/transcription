#!/usr/bin/env bash
# Download a static ffmpeg binary for macOS Apple Silicon and place it under
# bundled/ffmpeg/ so the Noto app can ship it inside the .app bundle.
#
# Usage: bash scripts/download_ffmpeg.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="$REPO_ROOT/bundled/ffmpeg"
mkdir -p "$DEST_DIR"

if [ -x "$DEST_DIR/ffmpeg" ]; then
    echo "ffmpeg already present at $DEST_DIR/ffmpeg"
    "$DEST_DIR/ffmpeg" -version | head -1 || true
    exit 0
fi

# evermeet.cx ships static, signed (ad-hoc) builds of ffmpeg for macOS arm64.
# Their /getrelease/zip endpoint always serves the latest stable build.
URL="https://evermeet.cx/ffmpeg/getrelease/zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading ffmpeg from $URL ..."
curl -fL "$URL" -o "$TMP/ffmpeg.zip"

echo "Extracting ..."
unzip -j "$TMP/ffmpeg.zip" -d "$DEST_DIR"
chmod +x "$DEST_DIR/ffmpeg"

echo
echo "Installed to $DEST_DIR/ffmpeg"
"$DEST_DIR/ffmpeg" -version | head -1
