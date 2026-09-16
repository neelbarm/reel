"""The filtergraph builder: a pure function from plan to string."""

import unittest

from reel.graph import (
    GraphError,
    Overlay,
    build_filtergraph,
    palettegen_graph,
    paletteuse_graph,
    parse_crop,
    scale_filter,
    segment_chain,
)
from reel.plan import KIND_ACTIVE, KIND_IDLE, Plan, Segment

# The plan reel derives from the synthetic fixture.
KNOWN_PLAN = Plan(
    [
        Segment(2.75, 7.0, 1.0, KIND_ACTIVE),
        Segment(7.0, 12.0, 8.0, KIND_IDLE),
        Segment(12.0, 15.25, 1.0, KIND_ACTIVE),
    ],
    source_duration=17.0,
    head_trim=2.75,
    tail_trim=1.75,
)

EXPECTED = (
    "[0:v]trim=start=2.75:end=7,setpts=PTS-STARTPTS[s0];"
    "[0:v]trim=start=7:end=12,setpts=(PTS-STARTPTS)/8[s1];"
    "[0:v]trim=start=12:end=15.25,setpts=PTS-STARTPTS[s2];"
    "[s0][s1][s2]concat=n=3:v=1:a=0[cat];"
    "[cat]scale=960:-2:flags=lanczos,setsar=1,fps=12[vout]"
)


class TestFiltergraph(unittest.TestCase):
    def test_known_plan_produces_the_expected_string(self):
        self.assertEqual(build_filtergraph(KNOWN_PLAN, width=960, fps=12), EXPECTED)

    def test_single_segment_skips_concat(self):
        plan = Plan([Segment(0, 5, 1.0)], 5.0)
        graph = build_filtergraph(plan, width=480, fps=10)
        self.assertNotIn("concat", graph)
        self.assertEqual(
            graph,
            "[0:v]trim=start=0:end=5,setpts=PTS-STARTPTS[s0];"
            "[s0]scale=480:-2:flags=lanczos,setsar=1,fps=10[vout]",
        )

    def test_crop_comes_before_scale(self):
        graph = build_filtergraph(KNOWN_PLAN, width=640, fps=12, crop="10:20:300:200")
        tail = graph.rsplit(";", 1)[1]
        self.assertLess(tail.index("crop="), tail.index("scale="))
        self.assertIn("crop=300:200:10:20", tail)

    def test_pix_fmt_is_appended_for_the_mp4_path(self):
        graph = build_filtergraph(KNOWN_PLAN, pix_fmt="yuv420p")
        self.assertTrue(graph.endswith("format=yuv420p[vout]"))

    def test_odd_width_is_rounded_down_to_even(self):
        self.assertIn("scale=960:-2", build_filtergraph(KNOWN_PLAN, width=961))

    def test_empty_plan_is_an_error(self):
        with self.assertRaises(GraphError):
            build_filtergraph(Plan([], 10.0))

    def test_zero_speed_is_an_error(self):
        with self.assertRaises(GraphError):
            segment_chain(Segment(0, 1, 0.0), 0)


class TestOverlays(unittest.TestCase):
    def test_overlays_chain_after_the_base_and_land_on_vout(self):
        overlays = [
            Overlay("1:v", 40, 500, 0.0, 4.0),
            Overlay("2:v", 40, 500, 4.0, 9.0),
        ]
        graph = build_filtergraph(KNOWN_PLAN, width=960, fps=12, overlays=overlays)
        self.assertIn("[cat]scale=960:-2:flags=lanczos,setsar=1,fps=12[base]", graph)
        self.assertIn("[base][1:v]overlay=x=40:y=500:enable='between(t,0,4)'[ov0]", graph)
        self.assertIn("[ov0][2:v]overlay=x=40:y=500:enable='between(t,4,9)'[vout]", graph)

    def test_pix_fmt_after_overlays_gets_its_own_link(self):
        graph = build_filtergraph(
            KNOWN_PLAN, overlays=[Overlay("1:v", 0, 0, 0, 2)], pix_fmt="yuv420p"
        )
        self.assertTrue(graph.endswith("[ov0]format=yuv420p[vout]"))

    def test_overlay_without_a_window_has_no_enable(self):
        self.assertEqual(Overlay("1:v", 5, 6).filter_string(), "overlay=x=5:y=6")


class TestPalette(unittest.TestCase):
    def test_palettegen_tail(self):
        graph = palettegen_graph("BASE[vout]", max_colors=128)
        self.assertTrue(graph.endswith("[vout]palettegen=max_colors=128:stats_mode=diff[pal]"))

    def test_paletteuse_reads_the_right_input(self):
        graph = paletteuse_graph("BASE", palette_input="3:v", dither="bayer", bayer_scale=2)
        self.assertIn("[vout][3:v]paletteuse=dither=bayer:bayer_scale=2", graph)

    def test_paletteuse_without_bayer_has_no_bayer_scale(self):
        self.assertNotIn("bayer_scale", paletteuse_graph("BASE", dither="sierra2_4a"))


class TestHelpers(unittest.TestCase):
    def test_crop_reorders_to_ffmpeg_order(self):
        self.assertEqual(parse_crop("10:20:640:480"), "crop=640:480:10:20")

    def test_crop_forces_even_dimensions(self):
        self.assertEqual(parse_crop("0:0:641:481"), "crop=640:480:0:0")

    def test_bad_crop_specs(self):
        for spec in ("1:2:3", "a:b:c:d", "0:0:0:100", "-1:0:10:10"):
            with self.assertRaises(GraphError):
                parse_crop(spec)

    def test_scale_filter_handles_height_only(self):
        self.assertEqual(scale_filter(None, 540), "scale=-2:540:flags=lanczos")

    def test_no_scale_when_both_are_none(self):
        self.assertIsNone(scale_filter(None, None))


if __name__ == "__main__":
    unittest.main()
