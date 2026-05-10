"""Japanese line-break (kinsoku) helpers.

Tk's built-in word-wrap algorithm doesn't know about Japanese typography
rules ("don't start a line with 。", etc.), so we manually wrap text and
insert explicit ``\\n``s. The auto-inserted newlines are also TAGGED in
the host tk.Text widget so we can later distinguish them from
user-typed Returns when reading the widget back.

Public surface:
  - constants ``KINSOKU_NO_BREAK_BEFORE`` / ``KINSOKU_NO_BREAK_AFTER``:
    sets of characters that must not begin (resp. end) a line.
  - ``WORD_JOINER`` (U+2060): legacy invisible marker. Old project files
    and a brief intermediate edit-mode iteration used these; we strip
    them on read so they never accumulate.
  - ``KINSOKU_NL_TAG``: tk.Text tag name applied to auto-wrap newlines.
  - ``wrap_kinsoku(text, font, max_width_px)``: returns ``text`` with
    ``\\n``s inserted at kinsoku-respecting break points.
  - ``tag_kinsoku_newlines(body, wrapped, wrapped_parts)`` /
    ``strip_kinsoku_newlines(body)``: pair of helpers that work with
    a tk.Text widget so user-typed ``\\n``s round-trip on save while
    auto-wrap ``\\n``s are dropped.
  - ``strip_word_joiners(text)``: drops any U+2060 sanitizer.
"""

from __future__ import annotations

# ── Character classes ────────────────────────────────────────────────────────
KINSOKU_NO_BREAK_BEFORE = set("。、．，！？)）」』〕｝!?,.…・ー")
KINSOKU_NO_BREAK_AFTER = set("(（「『〔｛")

# U+2060 (WORD JOINER): zero-width "no-break here" character. We never
# emit these any more, but older transcripts/projects may carry them as
# remnants of an earlier edit-mode wrap experiment, so we strip on read.
WORD_JOINER = "⁠"


# ── Word-joiner sanitizer ────────────────────────────────────────────────────
def strip_word_joiners(text: str) -> str:
    """Remove any U+2060 markers that may have been written into earlier
    project files when we briefly tried using them for edit-mode wrap."""
    return text.replace(WORD_JOINER, "") if WORD_JOINER in text else text


# ── tk.Text tag for auto-wrap newlines ───────────────────────────────────────
# DESIGN NOTE: We considered embedding marker characters (e.g. U+200B + \n)
# instead of using tags. Marker chars get separated when users edit around
# them, which silently breaks the strip step. Tags survive arbitrary edits
# because Tk shrinks/extends them with the underlying text — no manual
# bookkeeping needed. The tradeoff is that we make a few extra Tcl calls
# per save (acceptable: typical segments are < 1000 chars).
KINSOKU_NL_TAG = "_kinsoku_nl"


def strip_kinsoku_newlines(body) -> str:
    """Read the widget's text, removing only the auto-wrap ``\\n``s we
    tagged in ``tag_kinsoku_newlines``. User-typed Returns survive."""
    text = body.get("1.0", "end-1c")
    if "\n" not in text:
        return text
    ranges = body.tag_ranges(KINSOKU_NL_TAG)
    if not ranges:
        return text
    excluded: set[int] = set()
    for i in range(0, len(ranges), 2):
        try:
            s_off = int(body.count("1.0", ranges[i], "chars")[0])
            e_off = int(body.count("1.0", ranges[i + 1], "chars")[0])
        except (TypeError, IndexError, ValueError):
            continue
        excluded.update(range(s_off, e_off))
    return "".join(c for i, c in enumerate(text) if i not in excluded)


def tag_kinsoku_newlines(body, wrapped: str, wrapped_parts: list) -> None:
    """After we've inserted ``wrapped`` (= ``wrapped_parts`` joined with
    user ``\\n``s), tag every kinsoku-inserted ``\\n`` so a later
    ``strip_kinsoku_newlines`` knows which to drop on save.

    ``wrapped_parts`` is the per-paragraph kinsoku-wrapped pieces — their
    lengths define where the user-typed ``\\n`` boundaries land in the
    assembled ``wrapped`` string.
    """
    body.tag_remove(KINSOKU_NL_TAG, "1.0", "end")
    if "\n" not in wrapped:
        return
    user_nl_offsets: set[int] = set()
    cursor = 0
    for part in wrapped_parts[:-1]:
        cursor += len(part)
        user_nl_offsets.add(cursor)
        cursor += 1  # the user \n itself
    for offset, ch in enumerate(wrapped):
        if ch == "\n" and offset not in user_nl_offsets:
            body.tag_add(
                KINSOKU_NL_TAG,
                f"1.0+{offset}c",
                f"1.0+{offset + 1}c",
            )


# ── Manual line-wrap with kinsoku rules ──────────────────────────────────────
def wrap_kinsoku(text: str, font, max_width_px: int) -> str:
    """Wrap ``text`` into multiple lines respecting Japanese kinsoku rules.

    Returns text with explicit ``\\n`` inserted; the caller should set the
    Text widget's wrap mode to ``'none'`` so Tk doesn't add its own breaks.
    """
    if not text or max_width_px <= 0:
        return text
    lines: list[str] = []
    current: list[str] = []
    width = 0
    for c in text:
        if c == "\n":
            lines.append("".join(current))
            current = []
            width = 0
            continue
        cw = font.measure(c)
        if current and width + cw > max_width_px:
            # Want to break before c. Check kinsoku.
            if c in KINSOKU_NO_BREAK_BEFORE and len(current) >= 2:
                # Push last char of current to next line, keep punctuation with it
                last = current.pop()
                lines.append("".join(current))
                current = [last, c]
                width = font.measure(last) + cw
            elif current[-1] in KINSOKU_NO_BREAK_AFTER and len(current) >= 2:
                last = current.pop()
                lines.append("".join(current))
                current = [last, c]
                width = font.measure(last) + cw
            else:
                lines.append("".join(current))
                current = [c]
                width = cw
        else:
            current.append(c)
            width += cw
    if current:
        lines.append("".join(current))
    return "\n".join(lines)
