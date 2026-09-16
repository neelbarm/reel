"""The edit planner. Pure arithmetic - no ffmpeg, no files."""

import unittest

from reel.analyze import Freeze
from reel.plan import KIND_ACTIVE, KIND_IDLE, Segment, build_plan, coalesce, merge_tiny

# The synthetic fixture's shape: dead 0-3, motion 3-7, frozen 7-12,
# second scene 12-15, dead 15-17.
FIXTURE_FREEZES = [Freeze(0.0, 3.0), Freeze(7.0, 12.0), Freeze(15.0, 17.0)]


class TestBuildPlan(unittest.TestCase):
    def plan(self, **kwargs):
        return build_plan(17.0, FIXTURE_FREEZES, **kwargs)

    def test_trims_both_ends_and_speeds_the_middle(self):
        plan = self.plan()
        self.assertEqual(len(plan.segments), 3)
        self.assertAlmostEqual(plan.segments[0].start, 2.75)
        self.assertEqual(plan.segments[0].kind, KIND_ACTIVE)
        self.assertEqual(plan.segments[1].kind, KIND_IDLE)
        self.assertAlmostEqual(plan.segments[1].speed, 8.0)
        self.assertAlmostEqual(plan.segments[2].end, 15.25)

    def test_output_is_much_shorter_than_the_source(self):
        plan = self.plan()
        self.assertAlmostEqual(plan.output_duration, 4.25 + 5.0 / 8 + 3.25, places=6)
        self.assertLess(plan.output_duration, 9.0)
        self.assertGreater(plan.savings, 0.4)

    def test_cut_idle_drops_the_frozen_stretch(self):
        plan = self.plan(cut_idle=True)
        self.assertEqual([s.kind for s in plan.segments], [KIND_ACTIVE, KIND_ACTIVE])
        self.assertEqual(len(plan.dropped), 1)
        self.assertAlmostEqual(plan.output_duration, 4.25 + 3.25, places=6)

    def test_no_trim_keeps_the_dead_ends(self):
        plan = self.plan(trim_head=False, trim_tail=False)
        self.assertAlmostEqual(plan.segments[0].start, 0.0)
        self.assertAlmostEqual(plan.segments[-1].end, 17.0)
        self.assertEqual(plan.head_trim, 0.0)

    def test_idle_min_above_the_freeze_leaves_it_at_1x(self):
        plan = self.plan(idle_min=30.0)
        self.assertTrue(all(s.speed == 1.0 for s in plan.segments))

    def test_explicit_window_wins_over_trimming(self):
        plan = self.plan(start=5.0, end=10.0)
        self.assertAlmostEqual(plan.segments[0].start, 5.0)
        self.assertAlmostEqual(plan.segments[-1].end, 10.0)
        self.assertEqual(plan.head_trim, 0.0)

    def test_global_speed_applies_to_active_parts(self):
        plan = self.plan(speed=2.0)
        actives = [s for s in plan.segments if s.kind == KIND_ACTIVE]
        self.assertTrue(all(abs(s.speed - 2.0) < 1e-9 for s in actives))

    def test_all_freeze_input_still_produces_a_segment(self):
        plan = build_plan(10.0, [Freeze(0.0, 10.0)])
        self.assertEqual(len(plan.segments), 1)
        self.assertGreater(plan.output_duration, 0)

    def test_no_freezes_is_one_untouched_segment(self):
        plan = build_plan(12.0, [])
        self.assertEqual(plan.segments, [Segment(0.0, 12.0, 1.0, KIND_ACTIVE)])
        self.assertAlmostEqual(plan.savings, 0.0)

    def test_accepts_plain_tuples(self):
        plan = build_plan(17.0, [(0.0, 3.0), (7.0, 12.0)])
        self.assertAlmostEqual(plan.segments[0].start, 2.75)


class TestTimeMapping(unittest.TestCase):
    def test_input_time_maps_through_the_speed_changes(self):
        plan = build_plan(17.0, FIXTURE_FREEZES)
        # Start of the kept region.
        self.assertAlmostEqual(plan.output_time_of(2.75), 0.0, places=6)
        # Midway through the 1x opening segment.
        self.assertAlmostEqual(plan.output_time_of(5.0), 2.25, places=6)
        # After the 8x stretch: 4.25s of 1x plus 5s/8.
        self.assertAlmostEqual(plan.output_time_of(12.0), 4.25 + 0.625, places=6)

    def test_times_before_the_in_point_clamp_to_zero(self):
        plan = build_plan(17.0, FIXTURE_FREEZES)
        self.assertAlmostEqual(plan.output_time_of(0.5), 0.0)

    def test_times_past_the_out_point_clamp_to_the_end(self):
        plan = build_plan(17.0, FIXTURE_FREEZES)
        self.assertAlmostEqual(plan.output_time_of(99.0), plan.output_duration, places=6)


class TestMergeTiny(unittest.TestCase):
    def test_a_sliver_is_absorbed_by_its_longer_neighbour(self):
        segs = [
            Segment(0, 4, 1.0, KIND_ACTIVE),
            Segment(4, 4.1, 8.0, KIND_IDLE),
            Segment(4.1, 5, 1.0, KIND_ACTIVE),
        ]
        merged = merge_tiny(segs, 0.3)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].end, 5.0)

    def test_long_segments_are_untouched(self):
        segs = [Segment(0, 4, 1.0), Segment(4, 9, 8.0, KIND_IDLE)]
        self.assertEqual(merge_tiny(segs, 0.3), segs)

    def test_coalesce_joins_matching_neighbours(self):
        joined = coalesce([Segment(0, 2, 1.0), Segment(2, 5, 1.0)])
        self.assertEqual(joined, [Segment(0, 5, 1.0)])


if __name__ == "__main__":
    unittest.main()
