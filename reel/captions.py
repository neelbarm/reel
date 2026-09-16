"""Caption parsing, and the lower-third banner that renders them.

Spec syntax:  "0-4=Import a folder;4-9=Press Auto-Mix"
Times are output seconds by default. Prefix a cue with `in:` to give times on
the *input* timeline instead - handy when you read them off the original
recording and let reel map them through the edit:

    --captions "in:12.5-18=Auto-Mix picks the crossfades"

A path to a `.srt` file works too.

Banners are rasterised to RGBA PNGs by `reel.textrender` and composited with
ffmpeg's `overlay` filter, not `drawtext`. `drawtext` only exists in ffmpeg
builds linked against libfreetype, which many are not - and doing the layout
in Python means we know the exact text width, so the box can be sized around
the text with real padding and genuinely rounded corners.
"""

import os
import re

from .textrender import Canvas, Font

SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
SPEC_CUE_RE = re.compile(
    r"^\s*(?P<scope>in:)?\s*(?P<start>[\d.:]+)\s*-\s*(?P<end>[\d.:]+)\s*=\s*(?P<text>.*)$",
    re.S,
)

# Banner design, as fractions of the output frame height unless noted.
FONT_SCALE = 0.052
MARGIN_B = 0.050
MAX_WIDTH = 0.88        # fraction of frame width the banner may occupy
BOX_RGBA = (8, 10, 14, 178)
TEXT_RGBA = (255, 255, 255, 255)
HAIRLINE_RGBA = (255, 255, 255, 34)
LINE_GAP = 0.28         # extra leading between wrapped lines, x font size


class CaptionError(Exception):
    """Raised when a caption spec or .srt file cannot be parsed."""


class Caption(object):
    """One cue. `input_times` marks times that still need mapping."""

    __slots__ = ("start", "end", "text", "input_times")

    def __init__(self, start, end, text, input_times=False):
        self.start = float(start)
        self.end = float(end)
        self.text = text
        self.input_times = bool(input_times)

    def as_dict(self):
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "input_times": self.input_times,
        }

    def __eq__(self, other):
        return (
            isinstance(other, Caption)
            and abs(self.start - other.start) < 1e-6
            and abs(self.end - other.end) < 1e-6
            and self.text == other.text
            and self.input_times == other.input_times
        )

    def __repr__(self):
        return "Caption(%.2f, %.2f, %r%s)" % (
            self.start, self.end, self.text, ", input" if self.input_times else "",
        )


# ------------------------------------------------------------------ parse --

def parse_timecode(text):
    """Pure: '4', '4.5', '1:02', '00:01:02.5' -> seconds."""
    text = text.strip()
    if not text:
        raise CaptionError("Empty timecode.")
    parts = text.split(":")
    if len(parts) > 3:
        raise CaptionError("Bad timecode %r." % text)
    total = 0.0
    try:
        for part in parts:
            total = total * 60.0 + float(part)
    except ValueError:
        raise CaptionError("Bad timecode %r." % text)
    return total


def _split_cues(spec):
    """Pure: split on `;`, honouring a `\\;` escape inside caption text."""
    out, buf, i = [], [], 0
    while i < len(spec):
        ch = spec[i]
        if ch == "\\" and i + 1 < len(spec) and spec[i + 1] == ";":
            buf.append(";")
            i += 2
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [c for c in out if c.strip()]


def parse_spec(spec):
    """Pure: the inline `--captions` grammar -> [Caption]."""
    captions = []
    for chunk in _split_cues(spec):
        m = SPEC_CUE_RE.match(chunk)
        if not m:
            raise CaptionError(
                "Could not parse caption %r.\n"
                'Expected "START-END=TEXT", e.g. "0-4=Import a folder;4-9=Press Auto-Mix"'
                % chunk.strip()
            )
        text = m.group("text").strip()
        if not text:
            raise CaptionError("Caption %r has no text." % chunk.strip())
        start = parse_timecode(m.group("start"))
        end = parse_timecode(m.group("end"))
        if end <= start:
            raise CaptionError(
                "Caption %r ends (%.2f) at or before it starts (%.2f)." % (text, end, start)
            )
        captions.append(Caption(start, end, text, input_times=bool(m.group("scope"))))
    return captions


def parse_srt(text, input_times=False):
    """Pure: SRT text -> [Caption]. Blank-line separated, index optional."""
    captions = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = 1 if (lines[0].strip().isdigit() and len(lines) > 1) else 0
        m = SRT_TIME_RE.search(lines[idx] if idx < len(lines) else "")
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        body = " ".join(ln.strip() for ln in lines[idx + 1 :]).strip()
        body = re.sub(r"<[^>]+>", "", body)
        if body and end > start:
            captions.append(Caption(start, end, body, input_times=input_times))
    if not captions:
        raise CaptionError("No cues found in the .srt file.")
    return captions


def load_captions(spec):
    """Spec string or path to a .srt -> [Caption]."""
    if not spec:
        return []
    candidate = os.path.expanduser(spec)
    if candidate.lower().endswith(".srt") or (os.path.isfile(candidate) and "=" not in spec):
        if not os.path.isfile(candidate):
            raise CaptionError("Caption file not found: %s" % spec)
        with open(candidate, "r") as fh:
            # .srt times are wall-clock on the source, so treat them as input
            # times and let the plan map them onto the edit.
            return parse_srt(fh.read(), input_times=True)
    return parse_spec(spec)


def resolve_times(captions, plan=None):
    """Pure: map `in:` cues onto the output timeline using `plan`."""
    out = []
    for cue in captions:
        if cue.input_times and plan is not None:
            start = plan.output_time_of(cue.start)
            end = plan.output_time_of(cue.end)
            if end <= start:
                end = start + 0.6
            out.append(Caption(start, end, cue.text, input_times=False))
        else:
            out.append(Caption(cue.start, cue.end, cue.text, input_times=False))
    return out


# ------------------------------------------------------------------ draw --

def wrap_text(font, text, px, max_width):
    """Pure: greedy word wrap using real glyph advances."""
    words = text.split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        trial = current + " " + word
        if font.text_width(trial, px) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def banner_layout(font, text, frame_w, frame_h):
    """Pure: -> (lines, font_px, box_w, box_h, box_x, box_y, radius, pad_x)."""
    font_px = max(15, int(round(frame_h * FONT_SCALE)))
    pad_x = int(round(font_px * 1.15))
    pad_y = int(round(font_px * 0.60))
    max_text = max(60, int(frame_w * MAX_WIDTH) - 2 * pad_x)
    lines = wrap_text(font, text, font_px, max_text)
    ascent, descent = font.line_metrics(font_px)
    line_h = ascent + descent
    step = line_h + font_px * LINE_GAP
    text_w = max(font.text_width(line, font_px) for line in lines)
    box_w = int(round(min(frame_w * MAX_WIDTH, text_w + 2 * pad_x)))
    box_h = int(round(step * (len(lines) - 1) + line_h + 2 * pad_y))
    box_x = int(round((frame_w - box_w) / 2.0))
    box_y = int(round(frame_h - frame_h * MARGIN_B - box_h))
    radius = max(4, int(round(box_h * 0.24)))
    return lines, font_px, box_w, box_h, box_x, box_y, radius, pad_x


def render_banner(font, text, frame_w, frame_h, out_png):
    """Rasterise one caption banner. -> (png_path, overlay_x, overlay_y)."""
    lines, font_px, box_w, box_h, box_x, box_y, radius, _pad_x = banner_layout(
        font, text, frame_w, frame_h
    )
    canvas = Canvas(box_w, box_h)
    canvas.rounded_rect(0, 0, box_w, box_h, radius, BOX_RGBA)
    canvas.fill_rect(radius, 0, box_w - 2 * radius, max(1, box_h // 90 + 1), HAIRLINE_RGBA)

    ascent, descent = font.line_metrics(font_px)
    line_h = ascent + descent
    step = line_h + font_px * LINE_GAP
    top = (box_h - (step * (len(lines) - 1) + line_h)) / 2.0
    for i, line in enumerate(lines):
        width = font.text_width(line, font_px)
        canvas.draw_text(
            font, line, font_px, (box_w - width) / 2.0, top + i * step + ascent, TEXT_RGBA
        )
    canvas.to_png(out_png)
    return out_png, box_x, box_y


# --------------------------------------------------- storyboard timestamps --

BADGE_RGBA = (8, 10, 14, 190)


def render_badge(font, text, frame_h, out_png):
    """A small timestamp chip for a storyboard tile. -> (png, x, y)."""
    font_px = max(12, int(round(frame_h * 0.062)))
    pad_x = int(round(font_px * 0.72))
    pad_y = int(round(font_px * 0.42))
    ascent, descent = font.line_metrics(font_px)
    text_w = font.text_width(text, font_px)
    box_w = int(round(text_w + 2 * pad_x))
    box_h = int(round(ascent + descent + 2 * pad_y))
    radius = max(3, int(round(box_h * 0.28)))
    canvas = Canvas(box_w, box_h)
    canvas.rounded_rect(0, 0, box_w, box_h, radius, BADGE_RGBA)
    canvas.draw_text(font, text, font_px, pad_x, (box_h - ascent - descent) / 2.0 + ascent)
    canvas.to_png(out_png)
    inset = max(8, int(round(frame_h * 0.035)))
    return out_png, inset, inset


_FONT_CACHE = {}


def get_font(path):
    """Parsed fonts are cheap but not free; one per path is plenty."""
    font = _FONT_CACHE.get(path)
    if font is None:
        font = Font(path)
        _FONT_CACHE[path] = font
    return font
