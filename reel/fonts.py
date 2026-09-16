"""Locate a usable TTF/OTF font at runtime.

`drawtext` needs a real font file on disk. macOS, Linux and the various
Homebrew layouts all put them somewhere different, so we probe a candidate
list instead of hardcoding one path. `--font` always wins.
"""

import os

# Ordered best-first. Bold faces come first because captions burned over a
# screen recording need the weight to stay readable after GIF quantisation.
FONT_CANDIDATES = (
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/System/Library/Fonts/SFNSRounded.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    # Linux / CI
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    # Homebrew fontconfig
    "/opt/homebrew/share/fonts/DejaVuSans.ttf",
)


class FontNotFound(Exception):
    """Raised when no usable font file could be located."""


def find_font(override=None):
    """Return a path to a font file, or raise FontNotFound.

    `override` (from `--font`) is checked first and must exist.
    """
    if override:
        path = os.path.expanduser(override)
        if not os.path.isfile(path):
            raise FontNotFound(
                "--font %r does not exist. Pass a path to a .ttf/.ttc/.otf file." % override
            )
        return path
    for candidate in FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    raise FontNotFound(
        "No font file found for captions. Tried:\n  "
        + "\n  ".join(FONT_CANDIDATES)
        + "\nPass one explicitly with --font /path/to/Font.ttf"
    )


def available_fonts():
    """Every candidate that actually exists on this machine (for diagnostics)."""
    return [c for c in FONT_CANDIDATES if os.path.isfile(c)]
