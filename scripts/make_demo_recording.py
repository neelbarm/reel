#!/usr/bin/env python3
"""Render a realistic 22-second "screen recording" of a terminal session.

Used when a machine cannot grant screen-recording permission but we still
want to exercise reel against something that behaves like a real capture:
character-by-character typing, bursts of output, long dead pauses while a
command runs, and a hard cut to a different app.

Frames are rasterised with reel's own TrueType renderer and assembled with
the ffmpeg concat demuxer, so each state carries its true on-screen
duration - exactly the shape freezedetect is built to find.

    python3 scripts/make_demo_recording.py out.mov
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reel.fonts import find_font                      # noqa: E402
from reel.probe import ffmpeg_bin                     # noqa: E402
from reel.textrender import Canvas, Font              # noqa: E402

W, H = 1280, 800
MONO_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)

TERM_BG = (13, 16, 23, 255)
TERM_CHROME = (24, 28, 38, 255)
GREEN = (126, 231, 135, 255)
BLUE = (108, 168, 255, 255)
GREY = (139, 148, 158, 255)
WHITE = (230, 237, 243, 255)
AMBER = (232, 179, 72, 255)

BROWSER_BG = (248, 249, 251, 255)
BROWSER_CHROME = (226, 230, 238, 255)
INK = (24, 28, 38, 255)
INK_DIM = (110, 118, 132, 255)
ACCENT = (46, 130, 230, 255)


def mono_font():
    for path in MONO_CANDIDATES:
        if os.path.isfile(path):
            try:
                return Font(path)
            except Exception:
                continue
    return Font(find_font())


def terminal_canvas(font, title="neel@air — reel"):
    c = Canvas(W, H, TERM_BG)
    c.fill_rect(0, 0, W, 46, TERM_CHROME)
    for i, rgba in enumerate(((255, 95, 87, 255), (254, 188, 46, 255), (40, 200, 64, 255))):
        c.fill_rect(22 + i * 22, 17, 13, 13, rgba)
        c.rounded_rect(22 + i * 22, 17, 13, 13, 6, rgba)
    c.draw_text(font, title, 17, 120, 29, GREY)
    return c


def browser_canvas(font):
    c = Canvas(W, H, BROWSER_BG)
    c.fill_rect(0, 0, W, 78, BROWSER_CHROME)
    for i, rgba in enumerate(((255, 95, 87, 255), (254, 188, 46, 255), (40, 200, 64, 255))):
        c.rounded_rect(22 + i * 22, 20, 13, 13, 6, rgba)
    c.rounded_rect(120, 14, W - 160, 34, 9, (255, 255, 255, 255))
    c.draw_text(font, "localhost:8000/preview.html", 16, 140, 36, INK_DIM)
    c.fill_rect(0, 78, W, 3, (0, 0, 0, 14))
    return c


class Recorder(object):
    """Collects (png, seconds-on-screen) states for the concat demuxer."""

    def __init__(self, workdir):
        self.workdir = workdir
        self.states = []

    def snap(self, canvas, seconds):
        path = os.path.join(self.workdir, "s_%04d.png" % len(self.states))
        canvas.to_png(path)
        self.states.append((path, float(seconds)))

    def write_list(self):
        path = os.path.join(self.workdir, "list.txt")
        with open(path, "w") as fh:
            for png, seconds in self.states:
                fh.write("file '%s'\nduration %.4f\n" % (png, seconds))
            fh.write("file '%s'\n" % self.states[-1][0])  # concat needs the tail repeated
        return path


def type_line(rec, canvas, font, text, x, y, size, rgba, per_char=0.075, hold=0.0):
    pen = x
    for ch in text:
        pen += canvas.draw_text(font, ch, size, pen, y, rgba)
        rec.snap(canvas, per_char)
    if hold:
        rec.snap(canvas, hold)
    return pen


def main(out_path):
    font = mono_font()
    workdir = tempfile.mkdtemp(prefix="reel-demo-")
    try:
        rec = Recorder(workdir)
        size = 21
        line_h = 30
        y = 100

        term = terminal_canvas(font)
        term.draw_text(font, "➜", size, 40, y, GREEN)
        term.draw_text(font, "demos", size, 70, y, BLUE)
        term.draw_text(font, "$", size, 150, y, GREY)
        rec.snap(term, 2.6)  # lead-in dead time: the recording started early

        type_line(rec, term, font, " reel cut raw.mov -o demo.gif", 165, y, size, WHITE,
                  per_char=0.08, hold=0.55)

        y += line_h * 1.6
        for text, rgba, dwell in (
            ("▌ reel 0.1.0  │  raw.mov", BLUE, 0.22),
            ("          source  1918x968  h264  60 fps  7:51.1", GREY, 0.3),
            ("", GREY, 0.1),
            ("Edit plan", WHITE, 0.25),
            ("  00:07  01:40   93.1s  1x     93.1s", GREY, 0.18),
            ("  01:40  03:36  116.0s  8x     14.5s", GREY, 0.18),
        ):
            if text:
                term.draw_text(font, text, size, 40, y, rgba)
            y += line_h
            rec.snap(term, dwell)

        term.draw_text(font, "  palette  ▕", size, 40, y, GREY)
        rec.snap(term, 0.4)
        # The long wait: nothing moves at all while ffmpeg chews through it.
        rec.snap(term, 5.2)

        bar_x = 40 + font.text_width("  palette  ▕", size)
        for step in range(10):
            term.fill_rect(bar_x + step * 14, y - 15, 13, 16, ACCENT)
            rec.snap(term, 0.11)
        term.draw_text(font, "  100%", size, bar_x + 150, y, WHITE)
        y += line_h * 1.4
        rec.snap(term, 0.5)

        term.draw_text(font, "  ● rendered", size, 40, y, GREEN)
        y += line_h
        rec.snap(term, 0.35)
        term.draw_text(font, "    duration  7:51.1 → 1:47.6", size, 40, y, WHITE)
        y += line_h
        rec.snap(term, 0.35)
        term.draw_text(font, "     savings  77% shorter (4.4x)", size, 40, y, AMBER)
        rec.snap(term, 1.0)

        # Hard cut to a different app - this is the scene change.
        page = browser_canvas(font)
        page.draw_text(font, "demo.gif", 38, 60, 175, INK)
        rec.snap(page, 0.5)
        page.rounded_rect(60, 210, W - 120, 380, 14, (18, 22, 30, 255))
        rec.snap(page, 0.45)
        for i in range(6):
            page.fill_rect(90 + i * 190, 260 + (i % 3) * 40, 150, 90, (60 + i * 22, 90, 190, 255))
            rec.snap(page, 0.2)
        page.draw_text(font, "77% shorter, 12 fps, 960px wide", 22, 60, 640, INK_DIM)
        rec.snap(page, 0.6)

        # Reading pause: dead air again.
        rec.snap(page, 4.2)

        page.rounded_rect(60, 680, 220, 52, 10, ACCENT)
        page.draw_text(font, "Post to LinkedIn", 20, 88, 713, (255, 255, 255, 255))
        rec.snap(page, 1.1)
        rec.snap(page, 2.0)  # trailing dead time

        total = sum(d for _, d in rec.states)
        list_path = rec.write_list()
        subprocess.check_call([
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
            "-threads", "2",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart", "-f", "mov", "-y", out_path,
        ])
        print("wrote %s  (%d states, %.1fs)" % (out_path, len(rec.states), total))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "raw-session.mov")
