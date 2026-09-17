"""Turn an analysis into an edit plan.

Everything here is a pure function of numbers. No ffmpeg, no I/O - which is
what makes the interesting part of reel testable in milliseconds.

The plan is a contiguous list of Segments covering the kept region of the
input. Each segment carries a speed multiplier; `speed == 0` never appears
because cut segments are dropped rather than kept at infinite speed.
"""

KIND_ACTIVE = "active"
KIND_IDLE = "idle"


class Segment(object):
    """A slice of input time, played back at `speed`."""

    __slots__ = ("start", "end", "speed", "kind")

    def __init__(self, start, end, speed=1.0, kind=KIND_ACTIVE):
        self.start = float(start)
        self.end = float(end)
        self.speed = float(speed)
        self.kind = kind

    @property
    def in_duration(self):
        return max(0.0, self.end - self.start)

    @property
    def out_duration(self):
        return self.in_duration / self.speed if self.speed else 0.0

    def as_dict(self):
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speed": round(self.speed, 4),
            "kind": self.kind,
            "in_duration": round(self.in_duration, 3),
            "out_duration": round(self.out_duration, 3),
        }

    def __eq__(self, other):
        return (
            isinstance(other, Segment)
            and abs(self.start - other.start) < 1e-6
            and abs(self.end - other.end) < 1e-6
            and abs(self.speed - other.speed) < 1e-9
            and self.kind == other.kind
        )

    def __repr__(self):
        return "Segment(%.3f, %.3f, speed=%g, kind=%r)" % (
            self.start,
            self.end,
            self.speed,
            self.kind,
        )


class Plan(object):
    """The full edit: what to keep, how fast, and what got thrown away."""

    def __init__(self, segments, source_duration, head_trim=0.0, tail_trim=0.0, dropped=()):
        self.segments = list(segments)
        self.source_duration = float(source_duration)
        self.head_trim = float(head_trim)
        self.tail_trim = float(tail_trim)
        self.dropped = list(dropped)

    @property
    def output_duration(self):
        return sum(s.out_duration for s in self.segments)

    @property
    def kept_input_duration(self):
        return sum(s.in_duration for s in self.segments)

    @property
    def speedup(self):
        out = self.output_duration
        return (self.source_duration / out) if out > 1e-6 else 0.0

    @property
    def savings(self):
        """Fraction of the original runtime removed, 0..1."""
        if self.source_duration <= 1e-6:
            return 0.0
        return max(0.0, 1.0 - self.output_duration / self.source_duration)

    def output_time_of(self, input_time):
        """Map a time on the input timeline to a time on the output timeline.

        Used by `in:`-prefixed captions. Input times that landed inside a
        dropped or trimmed region snap to the nearest kept boundary.
        """
        t = float(input_time)
        elapsed = 0.0
        for seg in self.segments:
            if t < seg.start:
                return elapsed
            if t <= seg.end:
                return elapsed + (t - seg.start) / (seg.speed or 1.0)
            elapsed += seg.out_duration
        return elapsed

    def as_dict(self):
        return {
            "source_duration": round(self.source_duration, 3),
            "output_duration": round(self.output_duration, 3),
            "head_trim": round(self.head_trim, 3),
            "tail_trim": round(self.tail_trim, 3),
            "speedup": round(self.speedup, 3),
            "savings": round(self.savings, 4),
            "segments": [s.as_dict() for s in self.segments],
            "dropped": [
                {"start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3)}
                for a, b in self.dropped
            ],
        }

    def __repr__(self):
        return "Plan(%d segs, %.2fs -> %.2fs)" % (
            len(self.segments),
            self.source_duration,
            self.output_duration,
        )


def _subtract(window, holes):
    """Pure: (start, end) minus a sorted list of (start, end) -> list of gaps."""
    lo, hi = window
    out = []
    cursor = lo
    for h_lo, h_hi in holes:
        h_lo = max(h_lo, lo)
        h_hi = min(h_hi, hi)
        if h_hi <= h_lo:
            continue
        if h_lo > cursor:
            out.append((cursor, h_lo))
        cursor = max(cursor, h_hi)
    if cursor < hi:
        out.append((cursor, hi))
    return out


def merge_tiny(segments, min_duration=0.30):
    """Pure: absorb segments shorter than `min_duration` into a neighbour.

    A 4-frame "active" blip between two freezes is a cursor twitch, not
    content. Left alone it produces a stutter in the output and an extra
    concat input for nothing.
    """
    if not segments:
        return []
    segs = [Segment(s.start, s.end, s.speed, s.kind) for s in segments]
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i, seg in enumerate(segs):
            if seg.in_duration >= min_duration:
                continue
            prev_seg = segs[i - 1] if i > 0 else None
            next_seg = segs[i + 1] if i + 1 < len(segs) else None
            # Prefer whichever neighbour is longer; it dominates the look.
            if prev_seg and (not next_seg or prev_seg.in_duration >= next_seg.in_duration):
                prev_seg.end = seg.end
            elif next_seg:
                next_seg.start = seg.start
            else:
                continue
            del segs[i]
            changed = True
            break
    return coalesce(segs)


def coalesce(segments):
    """Pure: join adjacent segments that share a speed and kind."""
    out = []
    for seg in segments:
        if seg.in_duration <= 1e-6:
            continue
        if (
            out
            and out[-1].kind == seg.kind
            and abs(out[-1].speed - seg.speed) < 1e-9
            and abs(out[-1].end - seg.start) < 1e-6
        ):
            out[-1].end = seg.end
        else:
            out.append(Segment(seg.start, seg.end, seg.speed, seg.kind))
    return out


def build_plan(
    duration,
    freezes,
    idle_min=1.5,
    idle_speed=8.0,
    cut_idle=False,
    speed=1.0,
    trim_head=True,
    trim_tail=True,
    head_pad=0.25,
    tail_pad=0.25,
    min_segment=0.30,
    start=None,
    end=None,
):
    """Pure: (duration, freezes) -> Plan.

    `freezes` is any sequence of objects with `.start`/`.end`, or of
    (start, end) pairs.

    Rules, in order:
      1. Honour explicit --start/--end.
      2. Trim leading and trailing dead time (keeping `*_pad` seconds of it so
         the GIF does not begin mid-gesture).
      3. Idle stretches >= idle_min in the middle get `idle_speed` (or get
         dropped with cut_idle=True).
      4. Idle stretches shorter than idle_min are just part of the action.
      5. Everything else plays at `speed`.
      6. Merge away sub-`min_segment` slivers, then coalesce.
    """
    duration = float(duration)
    holes = []
    for f in freezes or ():
        if isinstance(f, (tuple, list)):
            holes.append((float(f[0]), float(f[1])))
        else:
            holes.append((float(f.start), float(f.end)))
    holes.sort()

    lo = 0.0 if start is None else max(0.0, float(start))
    hi = duration if end is None else min(duration, float(end))
    if hi <= lo:
        hi = duration

    head_trim = 0.0
    tail_trim = 0.0
    if trim_head and start is None:
        for h_lo, h_hi in holes:
            if h_lo <= 0.25 and h_hi > lo:
                new_lo = max(lo, h_hi - head_pad)
                if new_lo < hi:
                    head_trim = new_lo - lo
                    lo = new_lo
            break
    if trim_tail and end is None and holes:
        h_lo, h_hi = holes[-1]
        if h_hi >= duration - 0.35 and h_lo < hi:
            new_hi = min(hi, h_lo + tail_pad)
            if new_hi > lo:
                tail_trim = hi - new_hi
                hi = new_hi

    if hi - lo < min_segment:
        # The trims ate the whole recording - a capture that is dead from end
        # to end, where the lead-in freeze *is* the lead-out freeze. Keep the
        # original window and let the idle rules speed it up instead of
        # shipping a quarter-second of the last frame.
        lo = 0.0 if start is None else max(0.0, float(start))
        hi = duration if end is None else min(duration, float(end))
        if hi <= lo:
            hi = duration
        head_trim = tail_trim = 0.0

    # Idle stretches that survive inside the kept window.
    idle = []
    for h_lo, h_hi in holes:
        a = max(h_lo, lo)
        b = min(h_hi, hi)
        if b - a >= idle_min:
            idle.append((a, b))

    dropped = []
    segments = []
    if cut_idle:
        for a, b in idle:
            dropped.append((a, b))
        for a, b in _subtract((lo, hi), idle):
            segments.append(Segment(a, b, speed, KIND_ACTIVE))
    else:
        marks = []
        for a, b in idle:
            marks.append((a, b, KIND_IDLE))
        for a, b in _subtract((lo, hi), idle):
            marks.append((a, b, KIND_ACTIVE))
        marks.sort()
        for a, b, kind in marks:
            factor = idle_speed if kind == KIND_IDLE else speed
            segments.append(Segment(a, b, factor, kind))

    segments = merge_tiny(segments, min_segment)
    if not segments:
        # Degenerate input (all freeze, or a window smaller than min_segment):
        # keep the whole thing rather than emitting an empty graph.
        segments = [Segment(lo, hi if hi > lo else duration, speed, KIND_ACTIVE)]
        dropped = []
        head_trim = tail_trim = 0.0

    return Plan(segments, duration, head_trim, tail_trim, dropped)
