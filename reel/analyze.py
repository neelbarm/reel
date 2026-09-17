"""Find the boring parts.

Two signals, both from ffmpeg, both harvested from one decode pass:

* `freezedetect` - stretches where consecutive frames are (near) identical.
  On a screen recording that is you reading, typing nothing, waiting on a
  build, or having walked away.
* `select='gt(scene,T)'` + `showinfo` - frames whose content differs enough
  from the previous one to count as a cut. On a screen recording that is you
  switching app, opening a new view, or a page finishing its render.

Everything in here that parses text is a pure function so it can be tested
without spawning ffmpeg.
"""

import re
import subprocess

from .probe import ffmpeg_bin

# ffmpeg formats these with "%.6g" (av_ts_make_time_string), so allow an
# exponent as well as plain decimals, and accept both the "key: value" log
# form and the "key=value" form the metadata filter prints.
_NUM = r"(-?[\d.]+(?:[eE][-+]?\d+)?)"
FREEZE_START_RE = re.compile(r"lavfi\.freezedetect\.freeze_start[:=]\s*" + _NUM)
FREEZE_DURATION_RE = re.compile(r"lavfi\.freezedetect\.freeze_duration[:=]\s*" + _NUM)
FREEZE_END_RE = re.compile(r"lavfi\.freezedetect\.freeze_end[:=]\s*" + _NUM)
PTS_TIME_RE = re.compile(r"\bpts_time:\s*" + _NUM)


class Freeze(object):
    """A stretch of wall-clock input time where nothing moved."""

    __slots__ = ("start", "end")

    def __init__(self, start, end):
        self.start = float(start)
        self.end = float(end)

    @property
    def duration(self):
        return max(0.0, self.end - self.start)

    def as_dict(self):
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
        }

    def __eq__(self, other):
        return (
            isinstance(other, Freeze)
            and abs(self.start - other.start) < 1e-6
            and abs(self.end - other.end) < 1e-6
        )

    def __repr__(self):
        return "Freeze(%.3f, %.3f)" % (self.start, self.end)


def parse_freezedetect(text, duration=None):
    """Pure: freezedetect log text -> [Freeze].

    freezedetect emits `freeze_start`, then `freeze_duration` + `freeze_end`
    when the freeze breaks. A freeze that runs to EOF never gets its `_end`,
    so we close it at `duration` when we know it.
    """
    freezes = []
    pending = None
    pending_end = None
    for line in text.splitlines():
        m = FREEZE_START_RE.search(line)
        if m:
            if pending is not None:
                tail = _tail_end(pending, pending_end, duration)
                if tail is not None:
                    freezes.append(Freeze(pending, tail))
            pending = float(m.group(1))
            pending_end = None
            continue
        m = FREEZE_END_RE.search(line)
        if m and pending is not None:
            freezes.append(Freeze(pending, float(m.group(1))))
            pending = None
            pending_end = None
            continue
        m = FREEZE_DURATION_RE.search(line)
        if m and pending is not None:
            # Only a fallback end for a freeze that never gets its freeze_end
            # because the stream ended mid-freeze. A real freeze_end wins, and
            # this must NOT move `duration`: the clamp below is what keeps a
            # freeze inside a container whose duration says otherwise.
            pending_end = pending + float(m.group(1))
    if pending is not None:
        tail = _tail_end(pending, pending_end, duration)
        if tail is not None and tail > pending:
            freezes.append(Freeze(pending, tail))
    # freezedetect can report a start at a negative/0-ish epsilon; clamp.
    cleaned = []
    for f in freezes:
        start = max(0.0, f.start)
        end = f.end if duration is None else min(f.end, duration)
        if end - start > 1e-3:
            cleaned.append(Freeze(start, end))
    cleaned.sort(key=lambda f: f.start)
    return merge_freezes(cleaned)


def _tail_end(start, pending_end, duration):
    """Where to close a freeze that never reported its own `freeze_end`."""
    if pending_end is not None:
        return pending_end
    if duration is not None and duration > start:
        return duration
    return None


def merge_freezes(freezes, max_gap=0.35):
    """Pure: join freezes separated by less than `max_gap`.

    freezedetect restarts its run whenever a single frame wobbles above the
    noise floor, so one genuinely dead minute often arrives as six reports.
    Merging them keeps the timeline honest and the edit plan small.
    """
    out = []
    for f in sorted(freezes, key=lambda x: x.start):
        if out and f.start - out[-1].end <= max_gap:
            if f.end > out[-1].end:
                out[-1] = Freeze(out[-1].start, f.end)
        else:
            out.append(Freeze(f.start, f.end))
    return out


def parse_showinfo_times(text):
    """Pure: showinfo log text -> sorted list of pts_time floats."""
    times = []
    for line in text.splitlines():
        if "showinfo" not in line and "pts_time" not in line:
            continue
        m = PTS_TIME_RE.search(line)
        if m:
            try:
                times.append(float(m.group(1)))
            except ValueError:
                pass
    times.sort()
    return times


def dedupe_scenes(times, min_gap=0.6, duration=None):
    """Pure: collapse scene hits that are within `min_gap` of each other.

    A single app switch often trips the scene detector on three consecutive
    frames; we only want one cut marker out of that.
    """
    out = []
    for t in sorted(times):
        if t <= 1e-6:
            continue
        if duration is not None and t >= duration - 1e-6:
            continue
        if out and t - out[-1] < min_gap:
            continue
        out.append(t)
    return out


def build_analysis_filter(scene_threshold, freeze_noise, freeze_min, scale_width=320):
    """Pure: the single filterchain that produces both signals in one pass.

    Detection runs on a downscaled copy - a 1080p screen recording decodes far
    faster at 320px wide and neither signal cares about the extra pixels.
    """
    parts = []
    if scale_width:
        parts.append("scale=%d:-2:flags=fast_bilinear" % int(scale_width))
    parts.append(
        "freezedetect=noise=%s:duration=%s" % (_num(freeze_noise), _num(freeze_min))
    )
    parts.append("select='gt(scene,%s)'" % _num(scene_threshold))
    parts.append("showinfo")
    return ",".join(parts)


def _num(value):
    """Format a float compactly and deterministically for a filter argument."""
    text = ("%.6f" % float(value)).rstrip("0").rstrip(".")
    return text if text else "0"


def build_analysis_cmd(path, scene_threshold, freeze_noise, freeze_min, threads=2, scale_width=320):
    """Pure-ish: the argv for the analysis pass (no execution)."""
    return [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-threads", str(int(threads)),
        "-i", path,
        "-an",
        "-sn",
        "-map", "0:v:0",
        "-vf", build_analysis_filter(scene_threshold, freeze_noise, freeze_min, scale_width),
        "-f", "null",
        "-",
    ]


class Analysis(object):
    """Everything `reel inspect` knows about a recording."""

    def __init__(self, info, freezes, scenes):
        self.info = info
        self.freezes = freezes
        self.scenes = scenes

    @property
    def duration(self):
        return self.info.duration

    def idle_total(self):
        return sum(f.duration for f in self.freezes)

    def lead_in(self):
        """Dead time at the very start, or 0.0."""
        for f in self.freezes:
            if f.start <= 0.25:
                return f.end
            break
        return 0.0

    def lead_out(self):
        """Dead time at the very end, or 0.0."""
        if not self.freezes:
            return 0.0
        last = self.freezes[-1]
        if last.end >= self.duration - 0.35:
            return max(0.0, self.duration - last.start)
        return 0.0

    def as_dict(self):
        return {
            "media": self.info.as_dict(),
            "freezes": [f.as_dict() for f in self.freezes],
            "scenes": [round(t, 3) for t in self.scenes],
            "idle_total": round(self.idle_total(), 3),
            "lead_in": round(self.lead_in(), 3),
            "lead_out": round(self.lead_out(), 3),
            "active_total": round(max(0.0, self.duration - self.idle_total()), 3),
        }


def analyze(
    info,
    scene_threshold=0.28,
    freeze_noise=0.0001,
    freeze_min=0.9,
    threads=2,
    scale_width=320,
    verbose=False,
    dry_run=False,
):
    """Run the single analysis pass and return an Analysis."""
    from .render import log_cmd, AnalysisError

    cmd = build_analysis_cmd(
        info.path, scene_threshold, freeze_noise, freeze_min, threads, scale_width
    )
    if verbose or dry_run:
        log_cmd(cmd)
    # The analysis pass only reads, so it runs even under --dry-run: that way
    # the plan and filtergraph we print are the real ones.
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise AnalysisError(
            "ffmpeg analysis pass failed on %s:\n%s"
            % (info.path, "\n".join(stderr.strip().splitlines()[-12:]))
        )
    freezes = parse_freezedetect(stderr, info.duration)
    scenes = dedupe_scenes(parse_showinfo_times(stderr), duration=info.duration)
    return Analysis(info, freezes, scenes)
