"""Output staging. Pure file plumbing - no ffmpeg needed."""

import os
import shutil
import tempfile
import unittest

from reel.render import staged_output


class TestStagedOutput(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="reel-staged-")
        self.target = os.path.join(self.workdir, "out.gif")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def leftovers(self):
        return [f for f in os.listdir(self.workdir) if f.startswith(".reel-")]

    def test_success_moves_the_staged_file_into_place(self):
        with staged_output(self.target) as staged:
            self.assertNotEqual(staged, self.target)
            with open(staged, "w") as fh:
                fh.write("finished")
        with open(self.target) as fh:
            self.assertEqual(fh.read(), "finished")
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_render_leaves_the_previous_take_alone(self):
        with open(self.target, "w") as fh:
            fh.write("the good one")
        with self.assertRaises(RuntimeError):
            with staged_output(self.target) as staged:
                with open(staged, "w") as fh:
                    fh.write("half a gif")
                raise RuntimeError("ffmpeg fell over")
        with open(self.target) as fh:
            self.assertEqual(fh.read(), "the good one")
        self.assertEqual(self.leftovers(), [])

    def test_keyboard_interrupt_writes_nothing_and_cleans_up(self):
        with self.assertRaises(KeyboardInterrupt):
            with staged_output(self.target) as staged:
                with open(staged, "w") as fh:
                    fh.write("truncated")
                raise KeyboardInterrupt()
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.leftovers(), [])

    def test_staged_name_keeps_the_extension(self):
        with staged_output(self.target) as staged:
            self.assertTrue(staged.endswith(".gif"))
            self.assertEqual(os.path.dirname(staged), self.workdir)

    def test_dry_run_stages_nothing(self):
        with staged_output(self.target, dry_run=True) as staged:
            self.assertEqual(staged, self.target)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
