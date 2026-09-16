"""Shared fixture plumbing for the tests."""

import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "fixture.mov")
MAKE_FIXTURE = os.path.join(ROOT, "scripts", "make_fixture.sh")

# What scripts/make_fixture.sh builds, in seconds. The integration tests
# assert against these with a 0.5s tolerance.
LEAD_IN = (0.0, 3.0)
MOTION = (3.0, 7.0)
FREEZE = (7.0, 12.0)
SCENE_TWO = (12.0, 15.0)
LEAD_OUT = (15.0, 17.0)
DURATION = 17.0
TOLERANCE = 0.5


def have_ffmpeg():
    try:
        from reel.probe import require_tools

        require_tools()
        return True
    except Exception:
        return False


def ensure_fixture():
    """Build tests/fixtures/fixture.mov once, then reuse it."""
    if os.path.isfile(FIXTURE) and os.path.getsize(FIXTURE) > 1024:
        return FIXTURE
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    subprocess.check_call(["bash", MAKE_FIXTURE, FIXTURE])
    return FIXTURE


needs_ffmpeg = unittest.skipUnless(have_ffmpeg(), "ffmpeg/ffprobe not available")
