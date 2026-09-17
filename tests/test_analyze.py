"""Parsers for the two ffmpeg detection signals. No ffmpeg needed."""

import unittest

from reel.analyze import (
    Freeze,
    build_analysis_filter,
    dedupe_scenes,
    merge_freezes,
    parse_freezedetect,
    parse_showinfo_times,
)

FREEZE_LOG = """
[Parsed_freezedetect_1 @ 0x7f8] lavfi.freezedetect.freeze_start: 7.03333
[Parsed_freezedetect_1 @ 0x7f8] lavfi.freezedetect.freeze_duration: 4.93333
[Parsed_freezedetect_1 @ 0x7f8] lavfi.freezedetect.freeze_end: 11.9667
[Parsed_freezedetect_1 @ 0x7f8] lavfi.freezedetect.freeze_start: 15.0333
"""

SHOWINFO_LOG = """
[Parsed_showinfo_3 @ 0x600] n:   0 pts:   9000 pts_time:3        duration:300
[Parsed_showinfo_3 @ 0x600] n:   1 pts:  36000 pts_time:12       duration:300
[Parsed_showinfo_3 @ 0x600] n:   2 pts:  36900 pts_time:12.3     duration:300
"""


class TestFreezeParsing(unittest.TestCase):
    def test_pairs_start_with_end(self):
        freezes = parse_freezedetect(FREEZE_LOG, duration=17.0)
        self.assertEqual(len(freezes), 2)
        self.assertAlmostEqual(freezes[0].start, 7.03333, places=4)
        self.assertAlmostEqual(freezes[0].end, 11.9667, places=4)

    def test_freeze_running_to_eof_is_closed_at_duration(self):
        freezes = parse_freezedetect(FREEZE_LOG, duration=17.0)
        self.assertAlmostEqual(freezes[1].start, 15.0333, places=4)
        self.assertAlmostEqual(freezes[1].end, 17.0, places=4)

    def test_no_duration_means_unterminated_freeze_is_dropped(self):
        freezes = parse_freezedetect(FREEZE_LOG, duration=None)
        self.assertEqual(len(freezes), 1)

    def test_empty_log(self):
        self.assertEqual(parse_freezedetect("", duration=10.0), [])

    def test_clamps_to_duration(self):
        log = "lavfi.freezedetect.freeze_start: 1\nlavfi.freezedetect.freeze_end: 99\n"
        freezes = parse_freezedetect(log, duration=10.0)
        self.assertAlmostEqual(freezes[0].end, 10.0)

    def test_freeze_duration_cannot_stretch_the_clamp(self):
        # A container whose duration is short or wrong (VFR captures do this)
        # used to let freeze_duration push the clamp out with it, and the
        # freeze came back longer than the recording.
        log = (
            "lavfi.freezedetect.freeze_start: 1\n"
            "lavfi.freezedetect.freeze_duration: 99\n"
            "lavfi.freezedetect.freeze_end: 100\n"
        )
        freezes = parse_freezedetect(log, duration=10.0)
        self.assertAlmostEqual(freezes[0].end, 10.0)

    def test_freeze_cut_off_by_eof_uses_its_reported_duration(self):
        log = (
            "lavfi.freezedetect.freeze_start: 1\n"
            "lavfi.freezedetect.freeze_duration: 3\n"
        )
        freezes = parse_freezedetect(log, duration=10.0)
        self.assertEqual(len(freezes), 1)
        self.assertAlmostEqual(freezes[0].end, 4.0)

    def test_metadata_filter_style_and_exponents_parse(self):
        # ffmpeg formats these with "%.6g", so an exponent is legal syntax,
        # and the `metadata` filter prints key=value rather than "key: value".
        log = (
            "lavfi.freezedetect.freeze_start=1e-05\n"
            "lavfi.freezedetect.freeze_end=4.5\n"
        )
        freezes = parse_freezedetect(log, duration=10.0)
        self.assertEqual(len(freezes), 1)
        self.assertAlmostEqual(freezes[0].start, 1e-05)
        self.assertAlmostEqual(freezes[0].end, 4.5)


class TestMergeFreezes(unittest.TestCase):
    def test_touching_freezes_become_one(self):
        merged = merge_freezes([Freeze(0, 5), Freeze(5, 9), Freeze(20, 22)])
        self.assertEqual(merged, [Freeze(0, 9), Freeze(20, 22)])

    def test_gap_larger_than_max_is_kept(self):
        merged = merge_freezes([Freeze(0, 5), Freeze(6, 9)], max_gap=0.35)
        self.assertEqual(len(merged), 2)

    def test_nested_freeze_does_not_shorten_the_merge(self):
        merged = merge_freezes([Freeze(0, 9), Freeze(2, 4)])
        self.assertEqual(merged, [Freeze(0, 9)])


class TestSceneParsing(unittest.TestCase):
    def test_reads_pts_time(self):
        self.assertEqual(parse_showinfo_times(SHOWINFO_LOG), [3.0, 12.0, 12.3])

    def test_showinfo_exponent_times_parse(self):
        self.assertEqual(
            parse_showinfo_times("[showinfo] n:0 pts:0 pts_time:1.5e+01 duration:300"),
            [15.0],
        )

    def test_dedupe_collapses_neighbours_and_drops_edges(self):
        self.assertEqual(dedupe_scenes([3.0, 12.0, 12.3], duration=17.0), [3.0, 12.0])

    def test_dedupe_drops_zero_and_final_frame(self):
        self.assertEqual(dedupe_scenes([0.0, 5.0, 17.0], duration=17.0), [5.0])


class TestAnalysisFilter(unittest.TestCase):
    def test_single_pass_chain_contains_both_detectors(self):
        chain = build_analysis_filter(0.28, 0.0001, 0.9, scale_width=320)
        self.assertEqual(
            chain,
            "scale=320:-2:flags=fast_bilinear,"
            "freezedetect=noise=0.0001:duration=0.9,"
            "select='gt(scene,0.28)',showinfo",
        )

    def test_scale_can_be_disabled(self):
        self.assertTrue(build_analysis_filter(0.3, 0.001, 1.0, scale_width=0).startswith("freeze"))


if __name__ == "__main__":
    unittest.main()
