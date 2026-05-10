"""On-disk format of ``.transcription`` project files.

Single source of truth for how project files are versioned and migrated.
When the schema changes incompatibly, bump :data:`PROJECT_FILE_VERSION`
and add a branch to :func:`migrate_project_data`. The ``app.py`` load
path will automatically refuse files newer than the running app and
attempt to forward-migrate older files.

Public surface:
  - :data:`PROJECT_FILE_VERSION`
  - :func:`migrate_project_data(data, from_version) -> dict`
  - :func:`clean_legacy_join_text(text) -> str`
"""

from __future__ import annotations

import re

from transcribe_core import _join_segment_text


# Collapses runs of 2+ newlines into a single newline. Used as a defensive
# data sanitizer on load: an earlier version of the edit-mode Return-key
# handler could append spurious \n's at the cursor position, accumulating
# into \n\n or \n\n\n sequences in saved segment text. Those manifest as
# wide visual gaps when the file is reopened. Collapsing them at load time
# heals existing data and makes the bug self-correcting.
_MULTI_NL = re.compile(r"\n{2,}")


def collapse_multi_newlines(text: str) -> str:
    """Collapse 2+ consecutive newlines into a single newline."""
    return _MULTI_NL.sub("\n", text) if "\n\n" in text else text


# .transcription project file schema version. Bump deliberately when the
# on-disk shape changes incompatibly, and add a branch in migrate_project_data.
PROJECT_FILE_VERSION = 1


def migrate_project_data(data: dict, from_version: int) -> dict:
    """Bring a project file forward to the current schema. Currently we only
    have version 1, so this is essentially identity; the structure is in
    place so future format changes can land without rewriting load logic.

    Returns the migrated dict (may be the same object). Raises ValueError
    on an irrecoverable mismatch.
    """
    v = from_version
    if v == PROJECT_FILE_VERSION:
        return data
    if v < 1:
        # Treat legacy / version-less files as v1.
        v = 1
    # Future migrations would chain here, e.g.:
    #   if v == 1: data = _migrate_v1_to_v2(data); v = 2
    if v != PROJECT_FILE_VERSION:
        raise ValueError(
            f"unhandled project schema version {from_version} "
            f"(supported: {PROJECT_FILE_VERSION})"
        )
    return data


def clean_legacy_join_text(text: str) -> str:
    """Strip the legacy U+3000 join character that older transcripts /
    projects used between merged Whisper segments, re-joining with smart
    spacing (handled by ``transcribe_core._join_segment_text``)."""
    if "　" not in text:
        return text
    parts = text.split("　")
    out = parts[0]
    for p in parts[1:]:
        out = _join_segment_text(out, p)
    return out
