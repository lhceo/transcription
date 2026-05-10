#!/usr/bin/env bash
# Download a static ffmpeg binary for macOS Apple Silicon and place it under
# bundled/ffmpeg/ so the Noto app can ship it inside the .app bundle.
#
# This script is *deterministic*: it pins a specific upstream release of
# evermeet.cx's static ffmpeg build and verifies the SHA-256 of the
# extracted binary. A mismatch aborts with a clear message rather than
# silently shipping an unexpected binary.
#
# Updating to a newer ffmpeg version is a deliberate two-step:
#   1. set FFMPEG_VERSION to the desired tag (a release published on
#      https://evermeet.cx/ffmpeg/ ).
#   2. set FFMPEG_SHA256 to the new binary's hash; re-run the script.
#
# Usage:
#   bash scripts/download_ffmpeg.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="$REPO_ROOT/bundled/ffmpeg"
mkdir -p "$DEST_DIR"

# --- Pinned upstream ----------------------------------------------------------
FFMPEG_VERSION="8.1.1"
FFMPEG_URL="https://evermeet.cx/ffmpeg/ffmpeg-${FFMPEG_VERSION}.zip"
FFMPEG_SHA256="3a0ea97adddecfbf87b865da3bcbb321edfce4bab18a98ae1ba4ba9f0bd1f93a"

# --- Fast path: already present and correct -----------------------------------
if [ -x "$DEST_DIR/ffmpeg" ]; then
    actual="$(shasum -a 256 "$DEST_DIR/ffmpeg" | awk '{print $1}')"
    if [ "$actual" = "$FFMPEG_SHA256" ]; then
        echo "ffmpeg ${FFMPEG_VERSION} already present and SHA-256 verified."
        "$DEST_DIR/ffmpeg" -version | head -1
        exit 0
    fi
    echo "Existing ffmpeg has unexpected SHA-256; re-downloading."
fi

# --- Download -----------------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading ffmpeg ${FFMPEG_VERSION} from $FFMPEG_URL ..."
curl -fL "$FFMPEG_URL" -o "$TMP/ffmpeg.zip"

echo "Extracting ..."
unzip -j -o "$TMP/ffmpeg.zip" -d "$TMP/extract"

if [ ! -f "$TMP/extract/ffmpeg" ]; then
    echo "ERROR: zip did not contain a ffmpeg binary at the expected path." >&2
    ls -la "$TMP/extract" >&2
    exit 1
fi

actual="$(shasum -a 256 "$TMP/extract/ffmpeg" | awk '{print $1}')"
if [ "$actual" != "$FFMPEG_SHA256" ]; then
    echo "ERROR: SHA-256 mismatch for downloaded ffmpeg." >&2
    echo "  expected: $FFMPEG_SHA256" >&2
    echo "  actual:   $actual" >&2
    echo "Refusing to install untrusted binary." >&2
    exit 1
fi

# --- Install ------------------------------------------------------------------
mv "$TMP/extract/ffmpeg" "$DEST_DIR/ffmpeg"
chmod +x "$DEST_DIR/ffmpeg"

echo
echo "Installed to $DEST_DIR/ffmpeg (SHA-256 verified)"
"$DEST_DIR/ffmpeg" -version | head -1
