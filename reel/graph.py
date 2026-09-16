"""Plan -> ffmpeg filtergraph string.

One pure function does the whole edit. Each kept segment becomes a
`trim` + `setpts` branch, the branches go through a single `concat`, and the
result gets the shared tail chain (crop, scale, fps, captions, pixel format).

Building the graph as a string - rather than shelling out per segment and
stitching files afterwards - means the entire edit is one ffmpeg invocation,
one decode, and no intermediate files.
"""

from .plan import Segment  # noqa: F401  (re-exported for convenience)


class GraphError(Exception):
    """Raised when the requested graph cannot be built."""


def fmt(value):
    """Pure: compact, deterministic float formatting for filter arguments."""
    text = ("%.3f" % float(value)).rstrip("0").rstrip(".")
    return text if text else "0"


def parse_crop(spec):
    """Pure: 'x:y:w:h' -> the ffmpeg `crop=w:h:x:y` filter string."""
    if not spec:
        return None
    parts = [p.strip() for p in str(spec).split(":")]
    if len(parts) != 4:
        raise GraphError("--crop wants x:y:w:h (four numbers), got %r" % spec)
    try:
        x, y, w, h = [int(round(float(p))) for p in parts]
    except ValueError:
        raise GraphError("--crop wants four numbers, got %r" % spec)
    if w <= 0 or h <= 0:
        raise GraphError("--crop width and height must be positive, got %r" % spec)
    if x < 0 or y < 0:
        raise GraphError("--crop x and y must not be negative, got %r" % spec)
    # Even dimensions keep yuv420p happy for the MP4 path.
    w -= w % 2
    h -= h % 2
    return "crop=%d:%d:%d:%d" % (w, h, x, y)


def scale_filter(width, height=None, flags="lanczos"):
    """Pure: a scale filter that always lands on even dimensions."""
    if not width and not height:
        return None
    w = int(width) if width else -2
    if w > 0:
        w -= w % 2
    h = int(height) if height else -2
    if h > 0:
        h -= h % 2
    return "scale=%d:%d:flags=%s" % (w, h, flags)


def segment_chain(segment, index, source="0:v"):
    """Pure: one segment -> `[0:v]trim=...,setpts=...[s0]`."""
    speed = float(segment.speed)
    if speed <= 0:
        raise GraphError("Segment speed must be > 0, got %r" % speed)
    if speed == 1.0:
        pts = "setpts=PTS-STARTPTS"
    else:
        pts = "setpts=(PTS-STARTPTS)/%s" % fmt(speed)
    return "[%s]trim=start=%s:end=%s,%s[s%d]" % (
        source,
        fmt(segment.start),
        fmt(segment.end),
        pts,
        index,
    )


class Overlay(object):
    """One RGBA image composited onto the edit for a window of output time.

    Captions and storyboard badges both become these. `stream` is the ffmpeg
    input label the PNG arrives on (`"1:v"`, `"2:v"`, ...).
    """

    __slots__ = ("stream", "x", "y", "start", "end")

    def __init__(self, stream, x, y, start=None, end=None):
        self.stream = stream
        self.x = x
        self.y = y
        self.start = start
        self.end = end

    def filter_string(self):
        parts = ["overlay=x=%s:y=%s" % (fmt(self.x), fmt(self.y))]
        if self.start is not None and self.end is not None:
            parts.append("enable='between(t,%s,%s)'" % (fmt(self.start), fmt(self.end)))
        return ":".join(parts)

    def __repr__(self):
        return "Overlay(%r, %s, %s, %s..%s)" % (
            self.stream, self.x, self.y, self.start, self.end
        )


def build_filtergraph(
    plan,
    width=960,
    height=None,
    fps=12,
    crop=None,
    overlays=(),
    pix_fmt=None,
    out_label="vout",
    source="0:v",
    scale_flags="lanczos",
):
    """Pure: Plan (+ render options) -> a complete `-filter_complex` string.

    `overlays` is a sequence of `Overlay`; keeping the text rasterisation out
    of here leaves this module purely about time and pixels.
    """
    segments = list(plan.segments)
    if not segments:
        raise GraphError("Edit plan has no segments; nothing to render.")

    chains = [segment_chain(seg, i, source) for i, seg in enumerate(segments)]

    if len(segments) == 1:
        head_label = "s0"
    else:
        inputs = "".join("[s%d]" % i for i in range(len(segments)))
        chains.append("%sconcat=n=%d:v=1:a=0[cat]" % (inputs, len(segments)))
        head_label = "cat"

    tail = []
    crop_filter = parse_crop(crop) if isinstance(crop, str) else crop
    if crop_filter:
        tail.append(crop_filter)
    scale = scale_filter(width, height, scale_flags)
    if scale:
        tail.append(scale)
    tail.append("setsar=1")
    if fps:
        tail.append("fps=%s" % fmt(fps))

    overlays = list(overlays)
    if not overlays:
        if pix_fmt:
            tail.append("format=%s" % pix_fmt)
        chains.append("[%s]%s[%s]" % (head_label, ",".join(tail), out_label))
        return ";".join(chains)

    chains.append("[%s]%s[base]" % (head_label, ",".join(tail)))
    current = "base"
    for i, ov in enumerate(overlays):
        last = i == len(overlays) - 1
        label = out_label if (last and not pix_fmt) else "ov%d" % i
        chains.append("[%s][%s]%s[%s]" % (current, ov.stream, ov.filter_string(), label))
        current = label
    if pix_fmt:
        chains.append("[%s]format=%s[%s]" % (current, pix_fmt, out_label))
    return ";".join(chains)


def palettegen_graph(base_graph, out_label="vout", palette_label="pal", max_colors=256,
                     stats_mode="diff"):
    """Pure: extend the edit graph with a `palettegen` tail (GIF pass 1)."""
    return "%s;[%s]palettegen=max_colors=%d:stats_mode=%s[%s]" % (
        base_graph,
        out_label,
        int(max_colors),
        stats_mode,
        palette_label,
    )


def paletteuse_graph(base_graph, out_label="vout", palette_input="1:v",
                     dither="sierra2_4a", bayer_scale=3, final_label="gif"):
    """Pure: extend the edit graph with `paletteuse` (GIF pass 2)."""
    opts = ["dither=%s" % dither]
    if dither.startswith("bayer"):
        opts.append("bayer_scale=%d" % int(bayer_scale))
    opts.append("diff_mode=rectangle")
    return "%s;[%s][%s]paletteuse=%s[%s]" % (
        base_graph,
        out_label,
        palette_input,
        ":".join(opts),
        final_label,
    )
