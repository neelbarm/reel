"""Argument validation and the preview page. No ffmpeg needed."""

import os
import shutil
import tempfile
import unittest

from reel.cli import build_parser, check_crop_fits, check_edit_args, output_size
from reel.graph import GraphError
from reel.preview import write_preview


def cut_args(argv):
    """Parse a `cut` command line the way main() would."""
    return build_parser().parse_args(["cut", "in.mov"] + argv)


class TestCropBounds(unittest.TestCase):
    def test_a_crop_inside_the_frame_is_fine(self):
        self.assertIsNone(check_crop_fits("0:120:1280:560", 1280, 800))

    def test_a_crop_bigger_than_the_frame_is_rejected(self):
        with self.assertRaises(GraphError) as caught:
            check_crop_fits("0:0:5000:5000", 640, 400)
        self.assertIn("640x400", str(caught.exception))

    def test_a_crop_that_pokes_out_of_the_frame_is_rejected(self):
        # ffmpeg would silently slide this back inside and crop a different
        # region than the one that was asked for.
        with self.assertRaises(GraphError):
            check_crop_fits("600:380:200:200", 640, 400)

    def test_unknown_source_size_is_not_second_guessed(self):
        self.assertIsNone(check_crop_fits("0:0:200:200", 0, 0))

    def test_syntax_errors_still_raise(self):
        with self.assertRaises(GraphError):
            check_crop_fits("1:2:3", 640, 400)


class TestEditArgValidation(unittest.TestCase):
    def check(self, argv, duration=17.0):
        check_edit_args(cut_args(argv), duration)

    def test_defaults_pass(self):
        self.check([])

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.check(["--start", "12", "--end", "4"])

    def test_start_past_the_end_of_the_recording_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.check(["--start", "99"])

    def test_negative_start_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.check(["--start", "-5"])

    def test_zero_speeds_are_rejected(self):
        for argv in (["--speed", "0"], ["--idle-speed", "0"], ["--speed", "-2"]):
            with self.assertRaises(SystemExit):
                self.check(argv)

    def test_palette_size_must_be_in_range(self):
        for argv in (["--colors", "1"], ["--colors", "300"]):
            with self.assertRaises(SystemExit):
                self.check(argv)
        self.check(["--colors", "4"])

    def test_zero_width_and_fps_mean_source_defaults(self):
        self.check(["--width", "0", "--fps", "0"])
        with self.assertRaises(SystemExit):
            self.check(["--width", "-960"])


class TestOutputSize(unittest.TestCase):
    class Info(object):
        width = 1280
        height = 800

    def test_scales_and_rounds_to_even(self):
        self.assertEqual(output_size(self.Info(), None, 961), (960, 600))

    def test_crop_changes_the_aspect(self):
        self.assertEqual(output_size(self.Info(), "0:0:640:400", 320), (320, 200))


class TestPreviewPage(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="reel-preview-")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_page_is_written_as_utf8_whatever_the_locale(self):
        media = os.path.join(self.workdir, u"café demo.gif")
        with open(media, "wb") as fh:
            fh.write(b"not really a gif")
        out = write_preview(
            media,
            title=u"Café ☕ demo",
            chapters=[("00:01", u"Présentation \U0001F680")],
        )
        with open(out, "rb") as fh:
            html = fh.read().decode("utf-8")
        self.assertIn(u"Café ☕ demo", html)
        self.assertIn(u"Présentation \U0001F680", html)
        self.assertIn(u"café demo.gif", html)


if __name__ == "__main__":
    unittest.main()
