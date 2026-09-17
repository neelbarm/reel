"""The `reel` command line."""

from __future__ import print_function

import argparse
import json
import os
import shutil
import sys
import tempfile

from . import __version__, analyze as analyzemod, graph as graphmod, preview as previewmod
from . import ui
from .captions import (
    CaptionError, get_font, load_captions, overlapping_pairs, render_banner,
    resolve_times,
)
from .fonts import FontNotFound, find_font, available_fonts
from .textrender import FontError
from .plan import KIND_IDLE, build_plan
from .probe import FFmpegMissing, ProbeError, probe, require_tools
from .render import RenderError, render_gif, render_mp4, render_storyboard

GIF_EXTS = (".gif",)
VIDEO_EXTS = (".mp4", ".m4v", ".mov")


# --------------------------------------------------------------- plumbing --

def _detect_args(parser):
    g = parser.add_argument_group("detection")
    g.add_argument("--scene-threshold", type=float, default=0.28, metavar="T",
                   help="scene-change sensitivity, 0..1 (default: 0.28; lower = more cuts)")
    g.add_argument("--freeze-noise", type=float, default=0.0001, metavar="N",
                   help="how still counts as frozen (default: 0.0001; raise it for\n                         noisy captures, lower it if typing reads as idle)")
    g.add_argument("--freeze-min", type=float, default=0.9, metavar="S",
                   help="shortest freeze worth reporting, seconds (default: 0.9)")
    g.add_argument("--detect-width", type=int, default=320, metavar="PX",
                   help="downscale width for the detection pass (default: 320; 0 = native)")


def _global_args(parser):
    g = parser.add_argument_group("global")
    g.add_argument("--threads", type=int, default=2, metavar="N",
                   help="ffmpeg threads per pass (default: 2)")
    g.add_argument("--dry-run", action="store_true", help="print ffmpeg commands, run nothing")
    g.add_argument("--verbose", action="store_true", help="print every ffmpeg command")
    g.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    g.add_argument("--font", metavar="PATH", help="TTF/OTF to use for burned-in text")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reel",
        description="Turn raw screen recordings into tight, polished demo GIFs and MP4s.",
        epilog="Docs: README.md   |   reel <command> --help",
    )
    parser.add_argument("--version", action="version", version="reel %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("inspect", help="analyse a recording and print its timeline")
    p.add_argument("input")
    p.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    p.add_argument("--ascii", action="store_true", help="ASCII timeline (#/./|) instead of blocks")
    _detect_args(p)
    _global_args(p)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("cut", help="plan and render the edit (the main command)")
    p.add_argument("input")
    p.add_argument("-o", "--output", action="append", default=[], metavar="PATH",
                   help="output file; repeat for both a .gif and a .mp4")
    e = p.add_argument_group("edit")
    e.add_argument("--idle-min", type=float, default=1.5, metavar="S",
                   help="idle stretches at least this long get handled (default: 1.5)")
    e.add_argument("--idle-speed", type=float, default=8.0, metavar="X",
                   help="speed factor for idle stretches (default: 8)")
    e.add_argument("--cut-idle", action="store_true", help="drop idle stretches entirely")
    e.add_argument("--speed", type=float, default=1.0, metavar="X",
                   help="speed factor for the active parts (default: 1)")
    e.add_argument("--no-trim", action="store_true", help="keep leading/trailing dead time")
    e.add_argument("--start", type=float, metavar="S", help="force the in point (input seconds)")
    e.add_argument("--end", type=float, metavar="S", help="force the out point (input seconds)")
    e.add_argument("--min-segment", type=float, default=0.30, metavar="S",
                   help="absorb kept segments shorter than this (default: 0.30)")
    r = p.add_argument_group("look")
    r.add_argument("--fps", type=float, default=12.0, help="output frame rate (default: 12)")
    r.add_argument("--width", type=int, default=960, help="output width in px (default: 960)")
    r.add_argument("--crop", metavar="x:y:w:h", help="crop the source before scaling")
    r.add_argument("--captions", metavar="SPEC",
                   help='"0-4=Do a thing;4-9=Do the next" or a path to a .srt')
    r.add_argument("--colors", type=int, default=256, help="GIF palette size (default: 256)")
    r.add_argument("--dither", default="sierra2_4a",
                   choices=["sierra2_4a", "floyd_steinberg", "bayer", "none"],
                   help="GIF dithering (default: sierra2_4a)")
    r.add_argument("--bayer-scale", type=int, default=3, help="bayer_scale when --dither bayer")
    r.add_argument("--loop", type=int, default=0, help="GIF loop count, 0 = forever")
    r.add_argument("--crf", type=int, default=20, help="H.264 quality for MP4 (default: 20)")
    r.add_argument("--preset", default="veryfast", help="x264 preset (default: veryfast)")
    r.add_argument("--preview", action="store_true",
                   help="also write preview.html next to the output")
    _detect_args(p)
    _global_args(p)
    p.set_defaults(func=cmd_cut)

    p = sub.add_parser("storyboard", help="contact sheet of keyframes")
    p.add_argument("input")
    p.add_argument("-o", "--output", default=None, metavar="PATH",
                   help="output PNG (default: <input>.sheet.png)")
    p.add_argument("--every", type=float, default=None, metavar="S",
                   help="evenly spaced frames every S seconds instead of scene cuts")
    p.add_argument("--max-frames", type=int, default=12, help="cap the number of tiles (default: 12)")
    p.add_argument("--cols", type=int, default=4, help="max columns (default: 4)")
    p.add_argument("--thumb-width", type=int, default=420, help="thumbnail width (default: 420)")
    _detect_args(p)
    _global_args(p)
    p.set_defaults(func=cmd_storyboard)

    p = sub.add_parser("chapters", help="print scene cuts as chapter markers")
    p.add_argument("input")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--plain", action="store_true", help="no header, just the markers")
    _detect_args(p)
    _global_args(p)
    p.set_defaults(func=cmd_chapters)

    p = sub.add_parser("preview", help="write preview.html for a finished GIF/MP4")
    p.add_argument("input", help="a .gif or .mp4 produced by reel (or anything else)")
    p.add_argument("-o", "--output", default=None, metavar="PATH",
                   help="output HTML (default: preview.html next to the input)")
    p.add_argument("--title", default=None)
    p.add_argument("--captions", metavar="SPEC", help="chapter list for the page")
    _global_args(p)
    p.set_defaults(func=cmd_preview)

    return parser


def _setup(args, need_tools=True):
    if getattr(args, "no_color", False):
        ui.set_color(False)
    if need_tools:
        require_tools()


def _analysis(args, info):
    return analyzemod.analyze(
        info,
        scene_threshold=args.scene_threshold,
        freeze_noise=args.freeze_noise,
        freeze_min=args.freeze_min,
        threads=args.threads,
        scale_width=args.detect_width,
        verbose=args.verbose,
        dry_run=args.dry_run,
    )


def output_size(info, crop=None, width=960):
    """Pure-ish: the exact pixel size the edit will render at.

    Captions are rasterised at this size, so it has to match what the
    filtergraph produces - hence the same even-dimension rounding.
    """
    src_w, src_h = info.width, info.height
    if crop:
        parts = [p.strip() for p in str(crop).split(":")]
        if len(parts) == 4:
            try:
                src_w = int(round(float(parts[2])))
                src_h = int(round(float(parts[3])))
            except ValueError:
                pass
            src_w -= src_w % 2
            src_h -= src_h % 2
    if not src_w or not src_h:
        return 960, 540
    out_w = int(width) if width else src_w
    out_w -= out_w % 2
    out_h = int(round(src_h * out_w / float(src_w)))
    out_h -= out_h % 2
    return max(2, out_w), max(2, out_h)


def check_crop_fits(crop, width, height):
    """Raise unless the crop rectangle is fully inside a width x height frame.

    ffmpeg is no help here: a too-big `crop` dies with "Invalid too big or non
    positive size", and a crop that only pokes out on one side is silently
    slid back inside the frame, so you get a different region than you asked
    for. Both are worth catching before the analysis decode.
    """
    if not crop:
        return
    spec = graphmod.parse_crop(crop)
    if not width or not height:
        return
    w, h, x, y = [int(v) for v in spec[len("crop="):].split(":")]
    if x + w > width or y + h > height:
        raise graphmod.GraphError(
            "--crop %s does not fit inside the %dx%d source: it needs %dx%d.\n"
            "Pass x:y:w:h with x+w <= %d and y+h <= %d."
            % (crop, width, height, x + w, y + h, width, height)
        )


def check_edit_args(args, duration):
    """Cheap sanity checks on the numeric flags, before anything expensive."""
    def bad(message):
        raise SystemExit("reel: " + message)

    if args.start is not None:
        if args.start < 0:
            bad("--start must not be negative.")
        if duration and args.start >= duration:
            bad("--start %g is at or past the end of this %s recording."
                % (args.start, ui.human_duration(duration)))
    if args.end is not None and args.end <= 0:
        bad("--end must be greater than 0.")
    if args.start is not None and args.end is not None and args.end <= args.start:
        bad("--end %g must be later than --start %g." % (args.end, args.start))
    if args.speed <= 0:
        bad("--speed must be greater than 0 (1 is normal speed).")
    if args.idle_speed <= 0:
        bad("--idle-speed must be greater than 0 (use --cut-idle to drop idle).")
    if args.fps < 0:
        bad("--fps must not be negative (0 keeps the source frame rate).")
    if args.width < 0:
        bad("--width must not be negative (0 keeps the source width).")
    if not 4 <= args.colors <= 256:
        bad("--colors must be between 4 and 256, got %d." % args.colors)


def _media_line(info):
    return "%dx%d  %s  %.4g fps  %s" % (
        info.width, info.height, info.codec, info.fps, ui.human_duration(info.duration)
    )


# ------------------------------------------------------------------ spans --

def spans(duration, freezes):
    """Pure: -> [(start, end, 'active'|'idle')] covering the whole input."""
    out = []
    cursor = 0.0
    for f in freezes:
        start, end = f.start, f.end
        if start > cursor + 1e-3:
            out.append((cursor, start, "active"))
        out.append((max(cursor, start), end, "idle"))
        cursor = max(cursor, end)
    if cursor < duration - 1e-3:
        out.append((cursor, duration, "active"))
    return out


# --------------------------------------------------------------- commands --

def cmd_inspect(args):
    _setup(args)
    info = probe(args.input, args.verbose)
    if args.as_json:
        analysis = _analysis(args, info)
        payload = analysis.as_dict()
        payload["spans"] = [
            {"start": round(a, 3), "end": round(b, 3), "kind": k,
             "duration": round(b - a, 3)}
            for a, b, k in spans(info.duration, analysis.freezes)
        ]
        print(json.dumps(payload, indent=2))
        return 0

    ui.header(os.path.basename(args.input))
    ui.kv("source", _media_line(info))
    print()
    analysis = _analysis(args, info)
    ui.print_timeline(info.duration, analysis.freezes, analysis.scenes, args.ascii)

    ui.section("Segments")
    rows = []
    for a, b, kind in spans(info.duration, analysis.freezes):
        rows.append([
            ui.clock(a),
            ui.clock(b),
            "%5.1fs" % (b - a),
            ui.active("active") if kind == "active" else ui.idle("idle"),
        ])
    ui.table(["start", "end", "len", "kind"], rows, aligns=["r", "r", "r", "l"])

    if analysis.scenes:
        ui.section("Scene cuts")
        print("  " + "  ".join(ui.cutmark(ui.clock(t)) for t in analysis.scenes))

    idle_total = analysis.idle_total()
    pct = (idle_total / info.duration * 100.0) if info.duration else 0.0
    ui.summary(
        [
            ("runtime", ui.human_duration(info.duration)),
            ("active", "%s  %s" % (ui.human_duration(info.duration - idle_total),
                                   ui.dim("(%.0f%%)" % (100 - pct)))),
            ("idle", "%s  %s" % (ui.human_duration(idle_total), ui.dim("(%.0f%%)" % pct))),
            ("lead-in", ui.human_duration(analysis.lead_in())),
            ("lead-out", ui.human_duration(analysis.lead_out())),
            ("scene cuts", str(len(analysis.scenes))),
        ],
        title="analysis",
    )
    return 0


def _outputs_for(args):
    outs = list(args.output)
    if not outs:
        base = os.path.splitext(os.path.basename(args.input))[0]
        outs = [base + ".gif"]
    return outs


def cmd_cut(args):
    _setup(args)
    info = probe(args.input, args.verbose)
    outputs = _outputs_for(args)
    for path in outputs:
        ext = os.path.splitext(path)[1].lower()
        if ext not in GIF_EXTS + VIDEO_EXTS:
            raise SystemExit(
                "reel: don't know how to write %r. Use a .gif or .mp4 output path." % path
            )

    # Validate everything cheap before spending a full decode on analysis:
    # a typo in --crop should not cost a minute on a seven-minute capture.
    check_crop_fits(args.crop, info.width, info.height)
    check_edit_args(args, info.duration)
    raw_cues = load_captions(args.captions) if args.captions else []
    font = get_font(find_font(args.font)) if raw_cues else None

    ui.header(os.path.basename(args.input))
    ui.kv("source", _media_line(info))
    ui.kv("outputs", "  ".join(ui.white(os.path.basename(o)) for o in outputs))
    print()

    analysis = _analysis(args, info)
    plan = build_plan(
        info.duration,
        analysis.freezes,
        idle_min=args.idle_min,
        idle_speed=args.idle_speed,
        cut_idle=args.cut_idle,
        speed=args.speed,
        trim_head=not args.no_trim,
        trim_tail=not args.no_trim,
        min_segment=args.min_segment,
        start=args.start,
        end=args.end,
    )

    _print_plan(plan, args)

    cues = []
    overlays = []
    overlay_pngs = []
    workdir = None
    if raw_cues:
        cues = resolve_times(raw_cues, plan)
        clashes = overlapping_pairs(cues)
        if clashes:
            ui.warning(
                "%d caption pair(s) are on screen at once (%r over %r); "
                "banners share the same spot and will stack."
                % (len(clashes), clashes[0][1].text, clashes[0][0].text)
            )
        out_w, out_h = output_size(info, args.crop, args.width)
        workdir = tempfile.mkdtemp(prefix="reel-captions-")
        for i, cue in enumerate(cues):
            png, x, y = render_banner(
                font, cue.text, out_w, out_h, os.path.join(workdir, "cap_%03d.png" % i)
            )
            overlay_pngs.append(png)
            overlays.append(graphmod.Overlay("%d:v" % (i + 1), x, y, cue.start, cue.end))
        ui.info("rasterised %d caption banner(s) at %dx%d" % (len(cues), out_w, out_h))
        print()

    base_graph = graphmod.build_filtergraph(
        plan,
        width=args.width,
        fps=args.fps,
        crop=args.crop,
        overlays=overlays,
        pix_fmt=None,
    )
    if args.verbose or args.dry_run:
        ui.section("Filtergraph")
        print("  " + ui.dim(base_graph.replace(";", ";\n  ")))
        print()

    total = plan.output_duration
    written = []
    try:
        for path in outputs:
            _render_one(args, plan, path, base_graph, overlays, overlay_pngs, total)
            written.append(path)
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    if args.dry_run:
        ui.info("dry run: nothing written.")
        return 0

    rows = [("duration", ui.arrow(ui.human_duration(info.duration),
                                  ui.human_duration(plan.output_duration)))]
    rows.append(("savings", "%s %s" % (
        ui.good("%.0f%% shorter" % (plan.savings * 100)),
        ui.dim("(%.1fx)" % plan.speedup) if plan.speedup else "")))
    rows.append(("look", "%dpx wide  %g fps  %d colors" % (args.width, args.fps, args.colors)))
    for path in written:
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        rows.append((os.path.splitext(path)[1].lstrip(".") or "out",
                     "%s  %s" % (ui.white(path), ui.dim(ui.human_size(size)))))

    preview_path = None
    if args.preview and written:
        preview_path = _write_preview_for(written[0], info, plan, cues, args)
        rows.append(("preview", ui.white(preview_path)))

    ui.summary(rows, title="rendered")
    return 0


def _render_one(args, plan, path, base_graph, overlays, overlay_pngs, total):
    """Render a single output path, picking the codec path from its suffix."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    if os.path.splitext(path)[1].lower() in GIF_EXTS:
        render_gif(
            args.input, path, base_graph, total, threads=args.threads,
            max_colors=args.colors, dither=args.dither,
            bayer_scale=args.bayer_scale, loop=args.loop,
            verbose=args.verbose, dry_run=args.dry_run,
            overlay_pngs=overlay_pngs,
        )
    else:
        mp4_graph = graphmod.build_filtergraph(
            plan, width=args.width, fps=args.fps, crop=args.crop,
            overlays=overlays, pix_fmt="yuv420p",
        )
        render_mp4(
            args.input, path, mp4_graph, total, threads=args.threads,
            crf=args.crf, preset=args.preset,
            verbose=args.verbose, dry_run=args.dry_run,
            overlay_pngs=overlay_pngs,
        )


def _write_preview_for(media_path, info, plan, cues, args):
    chapters = [(ui.clock(c.start), c.text) for c in cues]
    size = os.path.getsize(media_path) if os.path.isfile(media_path) else 0
    pills = [
        ("source", ui.human_duration(info.duration)),
        ("output", ui.human_duration(plan.output_duration)),
        ("saved", "%.0f%%" % (plan.savings * 100)),
        ("size", ui.human_size(size)),
        ("fps", "%g" % args.fps),
        ("width", "%dpx" % args.width),
    ]
    # Strip colour: these land in HTML, not a terminal.
    pills = [(k, _plain(v)) for k, v in pills]
    return previewmod.write_preview(
        media_path,
        title=os.path.splitext(os.path.basename(media_path))[0],
        pills=pills,
        chapters=chapters,
        max_width=max(520, min(1100, args.width + 40)),
    )


def _plain(text):
    out = []
    skip = False
    for ch in str(text):
        if ch == "\033":
            skip = True
            continue
        if skip:
            if ch == "m":
                skip = False
            continue
        out.append(ch)
    return "".join(out)


def _print_plan(plan, args):
    ui.section("Edit plan")
    rows = []
    for seg in plan.segments:
        rows.append([
            ui.clock(seg.start),
            ui.clock(seg.end),
            "%5.1fs" % seg.in_duration,
            (ui.idle("%gx" % seg.speed) if seg.kind == KIND_IDLE
             else ui.active("%gx" % seg.speed)),
            "%5.1fs" % seg.out_duration,
        ])
    ui.table(["in", "out", "len", "speed", "→ len"], rows,
             aligns=["r", "r", "r", "l", "r"])
    notes = []
    if plan.head_trim > 0.05:
        notes.append("trimmed %s of lead-in" % ui.human_duration(plan.head_trim))
    if plan.tail_trim > 0.05:
        notes.append("trimmed %s of lead-out" % ui.human_duration(plan.tail_trim))
    if plan.dropped:
        dropped = sum(b - a for a, b in plan.dropped)
        notes.append("dropped %s of idle across %d gap(s)"
                     % (ui.human_duration(dropped), len(plan.dropped)))
    for note in notes:
        ui.info(note)
    print()


def cmd_storyboard(args):
    _setup(args)
    info = probe(args.input, args.verbose)
    out_path = args.output or (os.path.splitext(os.path.basename(args.input))[0] + ".sheet.png")

    ui.header(os.path.basename(args.input))
    ui.kv("source", _media_line(info))

    if args.every:
        step = max(0.25, float(args.every))
        times = []
        t = step / 2.0
        while t < info.duration and len(times) < args.max_frames:
            times.append(t)
            t += step
        origin = "every %gs" % step
    else:
        analysis = _analysis(args, info)
        times = [0.35] + list(analysis.scenes)
        origin = "%d scene cut(s)" % len(analysis.scenes)
        if len(times) > args.max_frames:
            stride = len(times) / float(args.max_frames)
            times = [times[int(i * stride)] for i in range(args.max_frames)]
    if not times:
        times = [info.duration * f for f in (0.05, 0.3, 0.55, 0.8)]
        origin = "evenly spaced (no cuts found)"

    ui.kv("frames", "%d  %s" % (len(times), ui.dim(origin)))
    print()

    font_path = find_font(args.font)
    progress = ui.Progress(len(times), label="frames")

    def on_frame(done, total):
        progress.update(done)

    thumb_h = int(round(args.thumb_width * (info.height / float(info.width or 1)))) or 240
    cols, rows = render_storyboard(
        args.input, out_path, times, font_path,
        thumb_width=args.thumb_width, max_cols=args.cols, threads=args.threads,
        verbose=args.verbose, dry_run=args.dry_run, on_frame=on_frame,
        thumb_height=thumb_h,
    )
    progress.finish()
    if args.dry_run:
        return 0
    size = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
    ui.summary(
        [
            ("grid", "%d x %d" % (cols, rows)),
            ("sheet", "%s  %s" % (ui.white(out_path), ui.dim(ui.human_size(size)))),
        ],
        title="storyboard",
    )
    return 0


def cmd_chapters(args):
    _setup(args)
    info = probe(args.input, args.verbose)
    analysis = _analysis(args, info)
    marks = [0.0] + list(analysis.scenes)
    if args.as_json:
        print(json.dumps(
            [{"index": i + 1, "time": round(t, 3), "label": "Scene %d" % (i + 1)}
             for i, t in enumerate(marks)],
            indent=2,
        ))
        return 0
    if not args.plain:
        ui.header(os.path.basename(args.input))
        ui.kv("source", _media_line(info))
        print()
    for i, t in enumerate(marks):
        line = "%s - Scene %d" % (ui.clock(t), i + 1)
        print(line if args.plain else "  " + ui.cutmark(ui.clock(t)) +
              ui.dim(" - ") + ui.white("Scene %d" % (i + 1)))
    if not args.plain:
        print()
    return 0


def cmd_preview(args):
    # No ffmpeg needed: this only writes an HTML page. ffprobe is used below
    # if it happens to be there, purely to add the duration/size pills.
    _setup(args, need_tools=False)
    if not os.path.isfile(args.input):
        raise SystemExit("reel: no such file: %s" % args.input)
    chapters = []
    if args.captions:
        chapters = [(ui.clock(c.start), c.text) for c in load_captions(args.captions)]
    size = os.path.getsize(args.input)
    pills = [("size", ui.human_size(size))]
    try:
        info = probe(args.input, args.verbose)
        pills.insert(0, ("duration", ui.human_duration(info.duration)))
        pills.append(("frame", "%dx%d" % (info.width, info.height)))
    except (ProbeError, FFmpegMissing):
        pass
    out = previewmod.write_preview(
        args.input, args.output, title=args.title, pills=pills, chapters=chapters
    )
    ui.header(os.path.basename(args.input))
    ui.summary([("preview", ui.white(out))], title="preview")
    return 0


# ------------------------------------------------------------------- main --

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.func(args) or 0
    except FFmpegMissing as exc:
        ui.error(str(exc))
        return 127
    except FontNotFound as exc:
        ui.error(str(exc))
        found = available_fonts()
        if found:
            ui.warning("fonts reel can see: %s" % ", ".join(found[:3]))
        return 2
    except (ProbeError, CaptionError, FontError, graphmod.GraphError, RenderError) as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        ui.error("interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
