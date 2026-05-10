"""Visual theme constants (colors and palettes) for the Noto desktop app.

This module is the single source of truth for the app's color values. Both
the main UI in app.py and any future widgets should import from here so a
re-skin only requires editing this file.

Why module-level globals (not a dict / dataclass)?
  - These values are stable for the life of the process; CTk widget options
    and tk.Text bg/fg arguments take strings directly, so there's nothing to
    gain from a wrapper object.
  - Keeping the names public (no leading underscore) makes their use across
    modules grammatically correct.
"""

# Light-theme background palette ----------------------------------------------
BG_LEFT = "#FFFFFF"   # left sidebar panel
BG_RIGHT = "#F1F5F9"  # main content background (cards area, hover for buttons)
BG_CARD = "#F8FAFC"   # default card body background
BG_CARD_H = "#EFF6FF" # card background while hovered (light accent blue)

# Accent (interactive) colors -------------------------------------------------
ACCENT = "#3B82F6"      # primary blue, used for active focus / buttons
ACCENT_HOV = "#2563EB"  # primary blue on hover (deeper)

# Borders ---------------------------------------------------------------------
BORDER = "#E2E8F0"      # default 1px border for cards / inputs
BORDER_H = "#93C5FD"    # card border while hovered (medium accent blue)

# Text colors -----------------------------------------------------------------
TEXT = "#1E293B"        # primary body text
TEXT_MUTED = "#94A3B8"  # secondary / hint text

# Speaker pill palette --------------------------------------------------------
# 15 distinct colours — modern Tailwind palette, one per display-name label.
SPEAKER_COLORS = [
    "#3B82F6",  # blue
    "#10B981",  # emerald
    "#F59E0B",  # amber
    "#EF4444",  # red
    "#8B5CF6",  # violet
    "#06B6D4",  # cyan
    "#F97316",  # orange
    "#EC4899",  # pink
    "#6366F1",  # indigo
    "#14B8A6",  # teal
    "#84CC16",  # lime
    "#A855F7",  # purple
    "#0EA5E9",  # sky
    "#D946EF",  # fuchsia
    "#78716C",  # warm-gray
]
