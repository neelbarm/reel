"""Actually run ffmpeg: progress streaming, GIF two-pass, MP4, storyboards."""

from __future__ import print_function

import contextlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from . import graph as graphmod
from . import ui
from .probe import ffmpeg_bin


class RenderError(Exception):
    """Raised when an ffmpeg render pass fails."""


class AnalysisError(RenderError):
    """Raised when the analysis pass fails."""


def quote(arg):
    """Shell-quote for --dry-run output that can be pasted straight back."""
    arg = str(arg)
    if arg and all(c.isalnum() or c in "-_./=:@+," for c in arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


def log_cmd(cmd, prefix="$"):
    print("  %s %s" % (ui.dim(prefix), ui.dim(" ".join(quote(a) for a in cmd))))


def _pump(stream, sink):
    for raw in iter(stream.readline, b""):
        sink.append(raw.decode("utf-8", "replace"))
    stream.close()


def run_ffmpeg(cmd, total_seconds=0.0, label="render", verbose=False, dry_run=False,
               show_progress=True):
    """Run one ffmpeg pass, streaming `-progress pipe:1` into a progress bar."""
    if verbose or dry_run:
        log_cmd(cmd)
    if dry_run:
        return ""

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_lines = []
    err_thread = threading.Thread(target=_pump, args=(proc.stderr, err_lines))
    err_thread.daemon = True
    err_thread.start()

    bar = ui.Progress(total_seconds, label=label) if (show_progress and total_seconds) else None
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not bar:
                continue
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                value = line.split("=", 1)[1]
                try:
                    micros = float(value)
                except ValueError:
                    continue
                # ffmpeg's out_time_ms is actually microseconds; both keys are
                # the same unit in practice.
                bar.update(micros / 1000000.0)
            elif line == "progress=end" :
                bar.update(total_seconds)
    finally:
        proc.stdout.close()
        proc.wait()
        err_thread.join(timeout=2)

    stderr = "".join(err_lines)
    if bar:
        bar.finish()
    if proc.returncode != 0:
        tail = "\n".join(l for l in stderr.strip().splitlines() if l.strip())[-1800:]
        raise RenderError("ffmpeg failed (%s pass):\n%s" % (label, tail))
    return stderr


@contextlib.contextmanager
def staged_output(path, dry_run=False):
    """Yield a sibling temp path that is moved onto `path` only on success.

    ffmpeg writes its container header the moment it starts, so a failed
    pass - or a Ctrl-C halfway through a slow GIF - would otherwise leave a
    truncated file exactly where the user expects a finished one, and would
    have already clobbered the previous take.
    """
    if dry_run:
        yield path
        return
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(
        prefix=".reel-", suffix=os.path.splitext(path)[1] or ".tmp", dir=directory
    )
    os.close(fd)
    try:
        yield tmp
    except BaseException:
        # BaseException, not Exception: KeyboardInterrupt is the whole point.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    else:
        os.replace(tmp, path)


def _common_input(threads, path, extra_inputs=()):
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-stats_period", "0.15",
        "-progress", "pipe:1",
        "-threads", str(int(threads)),
        "-i", path,
    ]
    for extra in extra_inputs:
        cmd += ["-i", extra]
    return cmd


# -------------------------------------------------------------------- GIF --

def build_gif_commands(src, out_path, base_graph, palette_path, threads=2,
                       max_colors=256, dither="sierra2_4a", bayer_scale=3,
                       loop=0, overlay_pngs=()):
    """Pure-ish: the two argv lists for the palettegen/paletteuse passes.

    Overlay PNGs occupy inputs 1..N, so the palette lands on input N+1.
    """
    overlay_pngs = list(overlay_pngs)
    pass1_graph = graphmod.palettegen_graph(
        base_graph, out_label="vout", palette_label="pal", max_colors=max_colors
    )
    pass1 = _common_input(threads, src, overlay_pngs) + [
        "-filter_complex", pass1_graph,
        "-map", "[pal]",
        "-frames:v", "1",
        "-y", palette_path,
    ]
    pass2_graph = graphmod.paletteuse_graph(
        base_graph, out_label="vout", palette_input="%d:v" % (len(overlay_pngs) + 1),
        dither=dither, bayer_scale=bayer_scale, final_label="gif",
    )
    pass2 = _common_input(threads, src, overlay_pngs + [palette_path]) + [
        "-filter_complex", pass2_graph,
        "-map", "[gif]",
        "-an",
        "-loop", str(int(loop)),
        "-f", "gif",
        "-y", out_path,
    ]
    return pass1, pass2


def render_gif(src, out_path, base_graph, total_seconds, threads=2, max_colors=256,
               dither="sierra2_4a", bayer_scale=3, loop=0, verbose=False, dry_run=False,
               overlay_pngs=()):
    workdir = tempfile.mkdtemp(prefix="reel-palette-")
    palette_path = os.path.join(workdir, "palette.png")
    try:
        with staged_output(out_path, dry_run) as staged:
            pass1, pass2 = build_gif_commands(
                src, staged, base_graph, palette_path, threads, max_colors,
                dither, bayer_scale, loop, overlay_pngs,
            )
            run_ffmpeg(pass1, total_seconds, "palette", verbose, dry_run)
            run_ffmpeg(pass2, total_seconds, "gif", verbose, dry_run)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -------------------------------------------------------------------- MP4 --

def build_mp4_command(src, out_path, base_graph, threads=2, crf=20, preset="veryfast",
                      overlay_pngs=()):
    return _common_input(threads, src, list(overlay_pngs)) + [
        "-filter_complex", base_graph,
        "-map", "[vout]",
        "-an",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-y", out_path,
    ]


def render_mp4(src, out_path, base_graph, total_seconds, threads=2, crf=20,
               preset="veryfast", verbose=False, dry_run=False, overlay_pngs=()):
    with staged_output(out_path, dry_run) as staged:
        cmd = build_mp4_command(src, staged, base_graph, threads, crf, preset, overlay_pngs)
        run_ffmpeg(cmd, total_seconds, "mp4", verbose, dry_run)


# ------------------------------------------------------------- storyboard --

SHEET_BG = "0x0E1116"


def grid_for(count, max_cols=4):
    """Pure: how many columns/rows for `count` thumbnails."""
    count = max(1, int(count))
    cols = min(max_cols, int(math.ceil(math.sqrt(count))))
    cols = max(1, cols)
    rows = int(math.ceil(count / float(cols)))
    return cols, rows


def build_frame_command(src, timestamp, out_png, thumb_width, badge=None, threads=2):
    """Pure-ish: fast-seek one frame, scale it, composite the timestamp badge.

    `badge` is (png_path, x, y) or None.
    """
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-threads", str(int(threads)),
        "-ss", graphmod.fmt(max(0.0, timestamp)),
        "-i", src,
    ]
    chain = "scale=%d:-2:flags=lanczos" % int(thumb_width)
    if badge:
        png, bx, by = badge
        cmd += ["-i", png]
        cmd += [
            "-filter_complex",
            "[0:v]%s[t];[t][1:v]overlay=x=%d:y=%d[out]" % (chain, int(bx), int(by)),
            "-map", "[out]",
        ]
    else:
        cmd += ["-vf", chain]
    cmd += ["-frames:v", "1", "-y", out_png]
    return cmd


def build_tile_command(pattern, out_path, cols, rows, margin=20, padding=14,
                       color=SHEET_BG, threads=2):
    return [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-threads", str(int(threads)),
        "-framerate", "1",
        "-i", pattern,
        "-filter_complex",
        "tile=%dx%d:margin=%d:padding=%d:color=%s" % (cols, rows, margin, padding, color),
        "-frames:v", "1",
        "-y", out_path,
    ]


def render_storyboard(src, out_path, timestamps, fontfile, thumb_width=420,
                      max_cols=4, threads=2, verbose=False, dry_run=False,
                      on_frame=None, thumb_height=None):
    """Extract each keyframe with its badge, then tile them into a sheet."""
    from .captions import get_font, render_badge
    from . import ui as uimod

    font = get_font(fontfile)
    cols, rows = grid_for(len(timestamps), max_cols)
    workdir = tempfile.mkdtemp(prefix="reel-sheet-")
    try:
        badge_h = thumb_height or int(thumb_width * 0.6)
        for i, t in enumerate(timestamps):
            png = os.path.join(workdir, "f_%03d.png" % (i + 1))
            badge = render_badge(
                font, uimod.clock(t), badge_h, os.path.join(workdir, "b_%03d.png" % (i + 1))
            )
            cmd = build_frame_command(src, t, png, thumb_width, badge, threads)
            run_ffmpeg(cmd, 0.0, "frame", verbose, dry_run, show_progress=False)
            if on_frame:
                on_frame(i + 1, len(timestamps))
        if dry_run:
            log_cmd(
                build_tile_command(
                    os.path.join(workdir, "f_%03d.png"), out_path, cols, rows, threads=threads
                )
            )
            return cols, rows
        made = sorted(f for f in os.listdir(workdir) if f.startswith("f_"))
        if not made:
            raise RenderError("Could not extract any frames for the storyboard.")
        cols, rows = grid_for(len(made), max_cols)
        with staged_output(out_path, dry_run) as staged:
            run_ffmpeg(
                build_tile_command(
                    os.path.join(workdir, "f_%03d.png"), staged, cols, rows, threads=threads
                ),
                0.0, "sheet", verbose, dry_run, show_progress=False,
            )
        return cols, rows
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
