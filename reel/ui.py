"""Terminal presentation: colour, tables, the timeline bar, the progress bar.

Everything degrades cleanly. If stdout is not a TTY, or NO_COLOR is set, or
TERM is `dumb`, every escape sequence collapses to an empty string and the
layout still lines up because widths are measured on the plain text.
"""

from __future__ import print_function

import os
import shutil
import sys
import time

_ENABLED = None


def color_enabled(stream=None):
    """True when it is polite to emit ANSI escapes."""
    global _ENABLED
    if _ENABLED is not None:
        return _ENABLED
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("REEL_FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "") in ("dumb", ""):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def set_color(enabled):
    """Force colour on/off (used by --no-color and by the tests)."""
    global _ENABLED
    _ENABLED = enabled


def _c(code):
    def wrap(text):
        if not color_enabled():
            return str(text)
        return "\033[%sm%s\033[0m" % (code, text)

    return wrap


# A small, deliberate palette. 256-colour so it looks the same in Terminal.app,
# iTerm and VS Code.
accent = _c("38;5;44")      # teal - the brand colour
accent_dim = _c("38;5;30")
active = _c("38;5;78")      # green - motion
idle = _c("38;5;244")       # grey - dead air
cutmark = _c("38;5;214")    # amber - scene change
bold = _c("1")
dim = _c("2")
white = _c("38;5;255")
warn = _c("38;5;214")
bad = _c("38;5;203")
good = _c("38;5;78")


def term_width(default=88):
    try:
        return max(60, min(120, shutil.get_terminal_size((default, 24)).columns))
    except Exception:
        return default


def human_duration(seconds):
    """0.0 -> '0:00.0', 471.08 -> '7:51.1', 3661 -> '1:01:01.0'."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    rest = seconds - hours * 3600 - minutes * 60
    if hours:
        return "%d:%02d:%04.1f" % (hours, minutes, rest)
    return "%d:%04.1f" % (minutes, rest)


def clock(seconds):
    """mm:ss, for chapter markers."""
    seconds = max(0, int(round(float(seconds))))
    hours = seconds // 3600
    if hours:
        return "%d:%02d:%02d" % (hours, (seconds % 3600) // 60, seconds % 60)
    return "%02d:%02d" % (seconds // 60, seconds % 60)


def human_size(num_bytes):
    try:
        num = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(num)
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f GB" % num


def header(subtitle=None, version=None):
    """The compact branded banner printed at the top of every command."""
    from . import __version__

    mark = accent("▌") + bold(accent("reel"))
    ver = dim(version or __version__)
    line = "%s %s" % (mark, ver)
    if subtitle:
        line += "  %s  %s" % (dim("│"), white(subtitle))
    print(line)


def rule(width=None, char="─"):
    print(dim(char * (width or term_width())))


def kv(label, value, width=14):
    print("  %s  %s" % (dim(label.rjust(width)), value))


def section(title):
    print()
    print(bold(white(title)))


def table(headers, rows, aligns=None, indent="  "):
    """Aligned plain table. `aligns` is a list of 'l'/'r' per column."""
    if not rows:
        return
    cols = len(headers)
    aligns = aligns or ["l"] * cols
    widths = [len(str(h)) for h in headers]
    plain_rows = [[str(c) for c in row] for row in rows]
    for row in plain_rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(row[i]))

    def lay(cells, painter=None):
        out = []
        last = len(cells) - 1
        for i, cell in enumerate(cells):
            if i == last and aligns[i] != "r":
                pad = cell  # no trailing padding; ANSI codes defeat rstrip()
            else:
                pad = cell.rjust(widths[i]) if aligns[i] == "r" else cell.ljust(widths[i])
            out.append(painter(pad) if painter else pad)
        return indent + "  ".join(out)

    print(lay([str(h) for h in headers], lambda s: dim(s)))
    for row in plain_rows:
        print(lay(row))


# ---------------------------------------------------------------- timeline --

BLOCK_ACTIVE = "█"   # full block
BLOCK_IDLE = "░"     # light shade
BLOCK_CUT = "│"      # box-drawing vertical


def timeline_cells(duration, freezes, scenes, cols=80):
    """Pure: -> a list of 'active' | 'idle' | 'cut' of length `cols`."""
    cols = max(8, int(cols))
    cells = ["active"] * cols
    if duration <= 0:
        return cells
    for f in freezes or ():
        start, end = (f[0], f[1]) if isinstance(f, (tuple, list)) else (f.start, f.end)
        lo = int(max(0.0, start) / duration * cols)
        hi = int(min(duration, end) / duration * cols + 0.5)
        for i in range(max(0, lo), min(cols, max(hi, lo + 1))):
            cells[i] = "idle"
    for t in scenes or ():
        i = int(float(t) / duration * cols)
        if 0 <= i < cols:
            cells[i] = "cut"
    return cells


def render_timeline(duration, freezes, scenes, cols=None, ascii_only=False):
    """The one-line visual summary of a recording."""
    cols = cols or max(40, term_width() - 8)
    cells = timeline_cells(duration, freezes, scenes, cols)
    glyphs = {"active": "#", "idle": ".", "cut": "|"} if ascii_only else {
        "active": BLOCK_ACTIVE,
        "idle": BLOCK_IDLE,
        "cut": BLOCK_CUT,
    }
    painters = {"active": active, "idle": idle, "cut": cutmark}
    out = []
    for cell in cells:
        out.append(painters[cell](glyphs[cell]))
    return "".join(out), glyphs


def print_timeline(duration, freezes, scenes, ascii_only=False):
    cols = max(40, term_width() - 4)
    bar, glyphs = render_timeline(duration, freezes, scenes, cols, ascii_only)
    print("  " + bar)
    ticks = "  " + "0:00".ljust(cols - len(human_duration(duration))) + human_duration(duration)
    print(dim(ticks))
    print(
        "  %s %s   %s %s   %s %s"
        % (
            active(glyphs["active"]),
            dim("active"),
            idle(glyphs["idle"]),
            dim("idle / frozen"),
            cutmark(glyphs["cut"]),
            dim("scene cut"),
        )
    )


# ---------------------------------------------------------------- progress --

SMOOTH = " ▏▎▍▌▋▊▉█"


class Progress(object):
    """A smooth one-line progress bar with an ETA, drawn on stderr."""

    def __init__(self, total, label="render", width=None, stream=None, enabled=None):
        self.total = float(total) if total and total > 0 else 0.0
        self.label = label
        self.stream = stream or sys.stderr
        self.width = width or max(18, min(42, term_width() - 42))
        self.start_time = time.time()
        self.value = 0.0
        self._last_draw = 0.0
        if enabled is None:
            try:
                enabled = self.stream.isatty()
            except Exception:
                enabled = False
        self.enabled = bool(enabled)
        self._drawn = False

    def _bar(self, frac):
        filled = frac * self.width
        whole = int(filled)
        rest = filled - whole
        bar = "█" * whole
        if whole < self.width:
            bar += SMOOTH[int(rest * (len(SMOOTH) - 1))]
            bar += " " * (self.width - whole - 1)
        return bar[: self.width]

    def update(self, value):
        self.value = max(self.value, float(value))
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_draw < 0.08 and self.value < self.total:
            return
        self._last_draw = now
        frac = min(1.0, self.value / self.total) if self.total else 0.0
        elapsed = now - self.start_time
        if frac > 0.01 and frac < 1.0:
            eta = elapsed * (1 - frac) / frac
            eta_text = "eta %s" % _short(eta)
        elif frac >= 1.0:
            eta_text = "in  %s" % _short(elapsed)
        else:
            eta_text = "eta   --"
        line = "  %s %s%s%s %s  %s" % (
            dim(self.label.ljust(9)),
            dim("▕"),
            accent(self._bar(frac)),
            dim("▏"),
            white("%3d%%" % int(frac * 100)),
            dim(eta_text),
        )
        self.stream.write("\r\033[K" + line)
        self.stream.flush()
        self._drawn = True

    def finish(self, note=None):
        if self.total:
            self.update(self.total)
        if self.enabled and self._drawn:
            self.stream.write("\n")
            self.stream.flush()
        elif note:
            print(note)


def _short(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%4.1fs" % seconds
    return "%d:%02d" % (int(seconds // 60), int(seconds % 60))


# ----------------------------------------------------------------- summary --

def summary(rows, title="done"):
    """The closing block: aligned label/value pairs under a rule."""
    print()
    print("  " + good("●") + " " + bold(white(title)))
    width = max(len(label) for label, _ in rows) if rows else 0
    for label, value in rows:
        print("    %s  %s" % (dim(label.rjust(width)), value))
    print()


def arrow(before, after):
    return "%s %s %s" % (white(before), accent_dim("→"), bold(white(after)))


def info(message):
    print("  %s %s" % (accent("›"), message))


def warning(message):
    print("  %s %s" % (warn("!"), message), file=sys.stderr)


def error(message):
    print("%s %s" % (bad("✖ reel:"), message), file=sys.stderr)
