"""ffmpeg/ffprobe discovery and container inspection."""

import json
import os
import shutil
import subprocess

# Homebrew on Apple Silicon is not always on PATH for GUI-launched shells.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")


class FFmpegMissing(Exception):
    """Raised when ffmpeg or ffprobe cannot be located."""


class ProbeError(Exception):
    """Raised when ffprobe cannot make sense of the input."""


def _which(name):
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def require_tools():
    """Return (ffmpeg_path, ffprobe_path) or raise a helpful FFmpegMissing."""
    ffmpeg = _which("ffmpeg")
    ffprobe = _which("ffprobe")
    missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
    if missing:
        raise FFmpegMissing(
            "reel needs %s on your PATH.\n\n"
            "  macOS:  brew install ffmpeg\n"
            "  Debian: sudo apt install ffmpeg\n\n"
            "Looked on PATH and in: %s"
            % (" and ".join(missing), ", ".join(_EXTRA_BIN_DIRS))
        )
    return ffmpeg, ffprobe


def ffmpeg_bin():
    return require_tools()[0]


def ffprobe_bin():
    return require_tools()[1]


class MediaInfo(object):
    """The handful of container facts the rest of reel actually needs."""

    def __init__(self, path, duration, width, height, fps, codec, pix_fmt, has_audio, nb_frames):
        self.path = path
        self.duration = duration
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.has_audio = has_audio
        self.nb_frames = nb_frames

    def as_dict(self):
        return {
            "path": self.path,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "codec": self.codec,
            "pix_fmt": self.pix_fmt,
            "has_audio": self.has_audio,
            "nb_frames": self.nb_frames,
        }

    def __repr__(self):
        return "<MediaInfo %dx%d %.2fs %s>" % (self.width, self.height, self.duration, self.codec)


def parse_rate(text):
    """Parse an ffprobe rational like '30000/1001' into a float."""
    if not text:
        return 0.0
    text = text.strip()
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            num = float(num)
            den = float(den)
        except ValueError:
            return 0.0
        return num / den if den else 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_probe_json(payload, path="<input>"):
    """Pure: ffprobe JSON dict -> MediaInfo. Separated out so it is testable."""
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    video = None
    has_audio = False
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and video is None and not stream.get("disposition", {}).get("attached_pic"):
            video = stream
        elif kind == "audio":
            has_audio = True
    if video is None:
        raise ProbeError("%s has no video stream." % path)

    duration = video.get("duration") or fmt.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
    nb_frames = video.get("nb_frames")
    try:
        nb_frames = int(nb_frames)
    except (TypeError, ValueError):
        nb_frames = int(duration * fps) if fps else 0

    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps or 30.0,
        codec=video.get("codec_name") or "?",
        pix_fmt=video.get("pix_fmt") or "?",
        has_audio=has_audio,
        nb_frames=nb_frames,
    )


def probe(path, verbose=False):
    """Run ffprobe on `path` and return MediaInfo."""
    if not os.path.isfile(path):
        raise ProbeError("Input file not found: %s" % path)
    ffprobe = ffprobe_bin()
    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    if verbose:
        from .render import log_cmd

        log_cmd(cmd)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise ProbeError(
            "ffprobe failed on %s:\n%s" % (path, proc.stderr.decode("utf-8", "replace").strip())
        )
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProbeError("Could not parse ffprobe output for %s: %s" % (path, exc))
    return parse_probe_json(payload, path)


def duration_of(path):
    """Convenience for tests / assertions on rendered output."""
    return probe(path).duration
