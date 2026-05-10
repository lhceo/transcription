"""py2app build configuration for Noto (transcription desktop app).

Build with:
    python setup.py py2app

Or use scripts/build_app.sh which also cleans up and ensures dependencies.

Output: dist/Noto.app
"""
from setuptools import setup
import os
import sys

APP_NAME = "Noto"
APP_VERSION = "1.0.0"
APP_BUNDLE_ID = "co.lionheart.noto"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

APP = ["app.py"]

# Data files copied into Contents/Resources/<dest>/
DATA_FILES = []

# Bundled ffmpeg (downloaded via scripts/download_ffmpeg.sh)
_ffmpeg = os.path.join(REPO_ROOT, "bundled", "ffmpeg", "ffmpeg")
if os.path.exists(_ffmpeg):
    DATA_FILES.append(("bundled/ffmpeg", [_ffmpeg]))
else:
    print(
        "WARN: bundled/ffmpeg/ffmpeg is missing. Run "
        "`bash scripts/download_ffmpeg.sh` before building.",
        file=sys.stderr,
    )

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleVersion": APP_VERSION,
        "CFBundleShortVersionString": APP_VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",  # macOS Monterey+
        "NSHumanReadableCopyright": "Lionheart",
        # Tell macOS we're an aqua app, not background-only
        "LSUIElement": False,
    },
    # Whole packages — include every submodule recursively. Necessary for
    # ML libraries that use lazy / dynamic imports.
    "packages": [
        "customtkinter",
        "mlx_whisper",
        "mlx",
        "pyannote",
        "mutagen",
        "PIL",
        "keyring",  # macOS Keychain access for HF Token storage
    ],
    # Specific modules that py2app's static analysis may miss.
    "includes": [
        "transcribe_core",
        "tkinter",
        "tkinter.font",
        "tkinter.filedialog",
        "tkinter.messagebox",
    ],
    # Trim things we don't use to keep the .app smaller.
    "excludes": [
        "matplotlib",
        "jupyter",
        "pytest",
        "test",
        "tests",
        "IPython",
    ],
    "optimize": 1,
}

setup(
    app=APP,
    name=APP_NAME,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
