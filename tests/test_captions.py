"""Caption parsing, layout, and the TrueType rasteriser behind it."""

import os
import tempfile
import unittest

from reel.captions import (
    Caption,
    CaptionError,
    banner_layout,
    get_font,
    load_captions,
    overlapping_pairs,
    parse_spec,
    parse_srt,
    parse_timecode,
    render_badge,
    render_banner,
    resolve_times,
    wrap_text,
)
from reel.fonts import FontNotFound, find_font
from reel.plan import Plan, Segment
from reel.textrender import Canvas, Font

SRT = """1
00:00:02,000 --> 00:00:05,500
Import a folder

2
00:00:06,000 --> 00:00:09,000
<i>Press Auto-Mix</i>
"""


class TestSpecParsing(unittest.TestCase):
    def test_two_cues(self):
        cues = parse_spec("0-4=Import a folder;4-9=Press Auto-Mix")
        self.assertEqual(
            cues,
            [Caption(0, 4, "Import a folder"), Caption(4, 9, "Press Auto-Mix")],
        )

    def test_in_prefix_marks_input_times(self):
        cues = parse_spec("in:12.5-18=Later")
        self.assertTrue(cues[0].input_times)
        self.assertAlmostEqual(cues[0].start, 12.5)

    def test_semicolon_can_be_escaped(self):
        cues = parse_spec(r"0-2=one\;two")
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "one;two")

    def test_timecodes(self):
        self.assertAlmostEqual(parse_timecode("4.5"), 4.5)
        self.assertAlmostEqual(parse_timecode("1:02"), 62.0)
        self.assertAlmostEqual(parse_timecode("00:01:02.5"), 62.5)

    def test_bad_specs_raise(self):
        for spec in ("nonsense", "4-0=backwards", "0-2="):
            with self.assertRaises(CaptionError):
                parse_spec(spec)


class TestSrt(unittest.TestCase):
    def test_reads_cues_and_strips_markup(self):
        cues = parse_srt(SRT)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 2.0)
        self.assertAlmostEqual(cues[0].end, 5.5)
        self.assertEqual(cues[1].text, "Press Auto-Mix")

    def test_empty_srt_raises(self):
        with self.assertRaises(CaptionError):
            parse_srt("not a subtitle file")

    def test_a_bom_does_not_eat_the_first_cue(self):
        # Subtitle editors love a UTF-8 BOM; the stray U+FEFF used to make
        # cue 1's index line unparseable and the cue vanished silently.
        path = self.write_srt(u"\ufeff" + SRT, encoding="utf-8")
        try:
            cues = load_captions(path)
            self.assertEqual(len(cues), 2)
            self.assertEqual(cues[0].text, "Import a folder")
        finally:
            os.unlink(path)

    def test_crlf_and_multi_line_cues(self):
        text = (
            "1\r\n00:00:01,000 --> 00:00:04,000\r\nFirst line\r\nsecond line\r\n"
            "\r\n2\r\n00:00:05,000 --> 00:00:07,000\r\nCafé ☕\r\n"
        )
        path = self.write_srt(text, encoding="utf-8")
        try:
            cues = load_captions(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "First line second line")
        self.assertEqual(cues[1].text, u"Café ☕")

    def test_a_non_utf8_srt_gives_a_caption_error_not_a_traceback(self):
        path = self.write_srt(
            u"1\n00:00:00,000 --> 00:00:02,000\nCafé\n", encoding="latin-1"
        )
        try:
            with self.assertRaises(CaptionError):
                load_captions(path)
        finally:
            os.unlink(path)

    def write_srt(self, text, encoding="utf-8"):
        fh = tempfile.NamedTemporaryFile("wb", suffix=".srt", delete=False)
        try:
            fh.write(text.encode(encoding))
        finally:
            fh.close()
        return fh.name

    def test_load_captions_from_a_file_uses_input_times(self):
        with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False) as fh:
            fh.write(SRT)
            path = fh.name
        try:
            cues = load_captions(path)
            self.assertTrue(all(c.input_times for c in cues))
        finally:
            os.unlink(path)


class TestResolveTimes(unittest.TestCase):
    def test_input_times_are_mapped_through_the_plan(self):
        plan = Plan([Segment(2.0, 6.0, 1.0), Segment(6.0, 14.0, 8.0)], 14.0)
        cues = resolve_times([Caption(4.0, 6.0, "x", input_times=True)], plan)
        self.assertAlmostEqual(cues[0].start, 2.0)
        self.assertAlmostEqual(cues[0].end, 4.0)
        self.assertFalse(cues[0].input_times)

    def test_output_times_pass_through_untouched(self):
        cues = resolve_times([Caption(1.0, 3.0, "x")], None)
        self.assertAlmostEqual(cues[0].start, 1.0)

    def test_degenerate_mapping_still_gives_a_visible_window(self):
        plan = Plan([Segment(0.0, 1.0, 1.0)], 20.0)
        cues = resolve_times([Caption(15.0, 16.0, "x", input_times=True)], plan)
        self.assertGreater(cues[0].end, cues[0].start)


class TestOverlapDetection(unittest.TestCase):
    def test_touching_cues_do_not_count_as_overlapping(self):
        cues = [Caption(0, 4, "a"), Caption(4, 9, "b")]
        self.assertEqual(overlapping_pairs(cues), [])

    def test_overlapping_cues_are_reported_in_time_order(self):
        cues = [Caption(2, 7, "second"), Caption(0, 5, "first")]
        clashes = overlapping_pairs(cues)
        self.assertEqual(len(clashes), 1)
        self.assertEqual((clashes[0][0].text, clashes[0][1].text), ("first", "second"))


class TestFontLookup(unittest.TestCase):
    def test_override_must_exist(self):
        with self.assertRaises(FontNotFound):
            find_font("/definitely/not/a/font.ttf")

    def test_finds_something_on_this_machine(self):
        self.assertTrue(os.path.isfile(find_font()))


class TestRasteriser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = get_font(find_font())

    def test_font_parsed(self):
        self.assertIsInstance(self.font, Font)
        self.assertGreater(self.font.units_per_em, 0)
        self.assertGreater(self.font.num_glyphs, 100)

    def test_width_grows_with_text_and_size(self):
        narrow = self.font.text_width("ab", 30)
        wide = self.font.text_width("abcdef", 30)
        self.assertGreater(wide, narrow)
        self.assertGreater(self.font.text_width("ab", 60), narrow * 1.8)

    def test_a_glyph_has_ink(self):
        w, h, _left, _top, cover = self.font.glyph_bitmap("M", 40)
        self.assertGreater(w, 4)
        self.assertGreater(h, 10)
        self.assertGreater(sum(cover), 20.0)

    def test_space_has_no_ink_but_advances(self):
        w, h, _l, _t, cover = self.font.glyph_bitmap(" ", 40)
        self.assertEqual(sum(cover), 0.0)
        self.assertGreater(self.font.text_width(" ", 40), 0)

    def test_png_round_trip_has_a_valid_signature(self):
        canvas = Canvas(30, 20)
        canvas.rounded_rect(0, 0, 30, 20, 5, (10, 10, 10, 200))
        path = os.path.join(tempfile.mkdtemp(), "x.png")
        canvas.to_png(path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertGreater(os.path.getsize(path), 50)

    def test_rounded_corners_are_transparent(self):
        canvas = Canvas(40, 40)
        canvas.rounded_rect(0, 0, 40, 40, 12, (0, 0, 0, 255))
        self.assertEqual(canvas.buf[3], 0)            # top-left alpha
        centre = (20 * 40 + 20) * 4
        self.assertEqual(canvas.buf[centre + 3], 255)  # middle alpha


class TestBannerLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font = get_font(find_font())

    def test_banner_sits_inside_the_frame(self):
        lines, px, bw, bh, bx, by, radius, _pad = banner_layout(
            self.font, "Press Auto-Mix", 960, 600
        )
        self.assertEqual(lines, ["Press Auto-Mix"])
        self.assertGreater(px, 15)
        self.assertLessEqual(bx + bw, 960)
        self.assertLessEqual(by + bh, 600)
        self.assertGreater(bx, 0)
        self.assertGreater(radius, 1)

    def test_long_text_wraps_instead_of_overflowing(self):
        text = "This is a deliberately long caption that cannot fit on one line at all"
        lines, _px, bw, _bh, _bx, _by, _r, _p = banner_layout(self.font, text, 960, 600)
        self.assertGreater(len(lines), 1)
        self.assertLessEqual(bw, 960)

    def test_a_caption_taller_than_the_frame_stays_on_screen(self):
        # An essay in a small frame wraps past the frame height; a negative
        # y made overlay clip the first lines off the top of the GIF.
        text = "supercalifragilistic " * 12
        _lines, _px, bw, _bh, bx, by, _r, _p = banner_layout(self.font, text, 320, 200)
        self.assertGreaterEqual(by, 0)
        self.assertGreaterEqual(bx, 0)
        self.assertLessEqual(bx + bw, 320)

    def test_wrap_respects_the_measured_width(self):
        lines = wrap_text(self.font, "alpha beta gamma delta", 30, 60)
        self.assertGreater(len(lines), 1)

    def test_render_banner_writes_a_png_positioned_near_the_bottom(self):
        out = os.path.join(tempfile.mkdtemp(), "b.png")
        path, x, y = render_banner(self.font, "Hello world", 960, 600, out)
        self.assertTrue(os.path.isfile(path))
        self.assertGreater(y, 400)
        self.assertGreaterEqual(x, 0)

    def test_render_badge_is_small_and_inset(self):
        out = os.path.join(tempfile.mkdtemp(), "badge.png")
        path, x, y = render_badge(self.font, "00:12", 260, out)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(x, y)
        self.assertGreater(x, 4)


if __name__ == "__main__":
    unittest.main()
