"""End-to-end against the synthetic fixture. Needs ffmpeg."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.helper import (
    DURATION,
    FREEZE,
    SCENE_TWO,
    TOLERANCE,
    ensure_fixture,
    needs_ffmpeg,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_reel(*args):
    """Run the CLI the way a user would: `python3 -m reel ...` from the repo."""
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "reel"] + list(args),
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


@needs_ffmpeg
class TestInspect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ensure_fixture()
        code, out, err = run_reel("inspect", cls.fixture, "--json")
        assert code == 0, err
        cls.data = json.loads(out)

    def test_duration_is_read_from_the_container(self):
        self.assertAlmostEqual(self.data["media"]["duration"], DURATION, delta=TOLERANCE)

    def test_finds_the_frozen_stretch(self):
        matches = [
            f for f in self.data["freezes"]
            if abs(f["start"] - FREEZE[0]) < TOLERANCE and abs(f["end"] - FREEZE[1]) < TOLERANCE
        ]
        self.assertEqual(
            len(matches), 1, "no freeze near %s in %s" % (FREEZE, self.data["freezes"])
        )

    def test_finds_the_leading_and_trailing_dead_time(self):
        self.assertAlmostEqual(self.data["lead_in"], 3.0, delta=TOLERANCE)
        self.assertAlmostEqual(self.data["lead_out"], 2.0, delta=TOLERANCE)

    def test_finds_the_scene_cut_into_the_second_scene(self):
        near = [t for t in self.data["scenes"] if abs(t - SCENE_TWO[0]) < TOLERANCE]
        self.assertTrue(near, "no scene cut near %ss in %s" % (SCENE_TWO[0], self.data["scenes"]))

    def test_spans_tile_the_whole_timeline(self):
        spans = self.data["spans"]
        self.assertAlmostEqual(spans[0]["start"], 0.0, delta=0.05)
        self.assertAlmostEqual(spans[-1]["end"], DURATION, delta=TOLERANCE)
        for a, b in zip(spans, spans[1:]):
            self.assertAlmostEqual(a["end"], b["start"], delta=0.05)

    def test_human_output_draws_a_timeline_and_a_legend(self):
        code, out, _err = run_reel("inspect", self.fixture, "--ascii")
        self.assertEqual(code, 0)
        self.assertIn("scene cut", out)
        self.assertIn("Segments", out)
        self.assertIn("#", out)


@needs_ffmpeg
class TestCut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ensure_fixture()
        cls.workdir = tempfile.mkdtemp(prefix="reel-test-")
        cls.gif = os.path.join(cls.workdir, "out.gif")
        cls.mp4 = os.path.join(cls.workdir, "out.mp4")
        code, out, err = run_reel(
            "cut", cls.fixture, "-o", cls.gif, "-o", cls.mp4, "--preview",
            "--captions", "0-2=First;in:13-14.5=Second",
        )
        assert code == 0, err + out
        cls.stdout = out

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def duration_of(self, path):
        from reel.probe import duration_of

        return duration_of(path)

    def test_gif_exists_and_is_not_empty(self):
        self.assertTrue(os.path.isfile(self.gif))
        self.assertGreater(os.path.getsize(self.gif), 1024)

    def test_gif_is_meaningfully_shorter_than_the_input(self):
        out = self.duration_of(self.gif)
        self.assertLess(out, DURATION * 0.75)
        self.assertGreater(out, 2.0)

    def test_mp4_matches_the_gif_length(self):
        self.assertAlmostEqual(self.duration_of(self.mp4), self.duration_of(self.gif), delta=0.3)

    def test_mp4_is_even_dimensioned_yuv420p(self):
        from reel.probe import probe

        info = probe(self.mp4)
        self.assertEqual(info.width % 2, 0)
        self.assertEqual(info.height % 2, 0)
        self.assertEqual(info.pix_fmt, "yuv420p")
        self.assertEqual(info.width, 960)

    def test_summary_reports_the_saving(self):
        self.assertIn("shorter", self.stdout)
        self.assertIn("rendered", self.stdout)

    def test_preview_html_is_written_next_to_the_output(self):
        page = os.path.join(self.workdir, "preview.html")
        self.assertTrue(os.path.isfile(page))
        with open(page) as fh:
            html = fh.read()
        self.assertIn("out.gif", html)
        self.assertIn("First", html)
        self.assertIn("Chapters", html)

    def test_cut_idle_is_shorter_still(self):
        target = os.path.join(self.workdir, "cut.gif")
        code, _out, err = run_reel("cut", self.fixture, "-o", target, "--cut-idle")
        self.assertEqual(code, 0, err)
        self.assertLess(self.duration_of(target), self.duration_of(self.gif))

    def test_dry_run_writes_nothing_but_prints_the_commands(self):
        target = os.path.join(self.workdir, "never.gif")
        code, out, _err = run_reel("cut", self.fixture, "-o", target, "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.isfile(target))
        self.assertIn("palettegen", out)
        self.assertIn("paletteuse", out)


@needs_ffmpeg
class TestOtherCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ensure_fixture()
        cls.workdir = tempfile.mkdtemp(prefix="reel-test-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_chapters_prints_markers(self):
        code, out, err = run_reel("chapters", self.fixture, "--plain")
        self.assertEqual(code, 0, err)
        lines = [l for l in out.strip().splitlines() if l.strip()]
        self.assertTrue(lines[0].startswith("00:00 - Scene 1"))
        self.assertGreaterEqual(len(lines), 2)

    def test_storyboard_writes_a_png(self):
        sheet = os.path.join(self.workdir, "sheet.png")
        code, _out, err = run_reel(
            "storyboard", self.fixture, "-o", sheet, "--every", "4", "--max-frames", "4"
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(sheet))
        from reel.probe import probe

        info = probe(sheet)
        self.assertGreater(info.width, 400)

    def test_preview_command_on_a_finished_gif(self):
        gif = os.path.join(self.workdir, "p.gif")
        code, _out, err = run_reel("cut", self.fixture, "-o", gif, "--width", "320")
        self.assertEqual(code, 0, err)
        page = os.path.join(self.workdir, "custom.html")
        code, _out, err = run_reel("preview", gif, "-o", page, "--title", "My demo")
        self.assertEqual(code, 0, err)
        with open(page) as fh:
            html = fh.read()
        self.assertIn("My demo", html)
        self.assertIn("p.gif", html)

    def test_unknown_output_extension_is_rejected(self):
        code, _out, err = run_reel("cut", self.fixture, "-o", "nope.avi")
        self.assertNotEqual(code, 0)
        self.assertIn("gif", (err + _out).lower())

    def test_a_failed_render_does_not_clobber_the_previous_output(self):
        target = os.path.join(self.workdir, "keep.mp4")
        with open(target, "w") as fh:
            fh.write("yesterday's good take")
        code, _out, _err = run_reel(
            "cut", self.fixture, "-o", target, "--preset", "not-a-real-preset",
            "--width", "320",
        )
        self.assertNotEqual(code, 0)
        with open(target) as fh:
            self.assertEqual(fh.read(), "yesterday's good take")
        leftovers = [f for f in os.listdir(self.workdir) if f.startswith(".reel-")]
        self.assertEqual(leftovers, [])

    def test_crop_outside_the_frame_is_rejected_before_rendering(self):
        code, out, err = run_reel(
            "cut", self.fixture, "-o", os.path.join(self.workdir, "crop.gif"),
            "--crop", "0:0:5000:5000",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("does not fit", err + out)

    def test_missing_input_fails_cleanly(self):
        code, _out, err = run_reel("inspect", "/no/such/file.mov")
        self.assertNotEqual(code, 0)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
