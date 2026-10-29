import unittest

from app.run_time_estimate import RunTimeEstimate


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RunTimeEstimateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.estimate = RunTimeEstimate(clock=self.clock)

    def test_first_completed_unit_learns_timing_including_initial_wait(self):
        self.estimate.start(0, 5)
        self.clock.advance(12)
        self.assertIsNone(self.estimate.remaining_seconds)
        result = self.estimate.observe(1, 5)
        self.assertEqual(result.remaining_seconds, 48)
        self.assertEqual(result.elapsed_seconds, 12)

    def test_unit_time_smoothly_adapts_to_slow_and_fast_measurements(self):
        self.estimate.start(0, 10)
        self.clock.advance(10)
        self.estimate.observe(1, 10)
        self.clock.advance(20)
        slow = self.estimate.observe(2, 10)
        self.assertAlmostEqual(slow.remaining_seconds, 8 * 13)
        self.clock.advance(3)
        fast = self.estimate.observe(3, 10)
        self.assertAlmostEqual(fast.remaining_seconds, 7 * 10)

    def test_duplicate_progress_does_not_discard_wait_or_fabricate_rate(self):
        self.estimate.start(0, 5)
        for _ in range(3):
            self.clock.advance(2)
            self.assertIsNone(self.estimate.observe(0, 5).remaining_seconds)
        self.clock.advance(2)
        self.assertEqual(self.estimate.observe(1, 5).remaining_seconds, 32)
        for _ in range(3):
            self.clock.advance(2)
            self.estimate.observe(1, 5)
        self.clock.advance(2)
        self.assertEqual(self.estimate.observe(2, 5).remaining_seconds, 24)

    def test_stalled_last_unit_stays_pending_and_extends_estimate(self):
        self.estimate.start(0, 2)
        self.clock.advance(10)
        self.estimate.observe(1, 2)
        self.clock.advance(100)
        stalled = self.estimate.observe(1, 2)
        self.assertEqual(stalled.remaining_seconds, 100)
        self.assertTrue(stalled.running)
        self.assertEqual(stalled.done, 1)
        self.clock.advance(5)
        self.assertGreater(self.estimate.remaining_seconds, stalled.remaining_seconds)

    def test_batched_progress_uses_time_per_completed_unit(self):
        self.estimate.start(0, 12)
        self.clock.advance(15)
        self.assertEqual(self.estimate.observe(3, 12).remaining_seconds, 45)

    def test_total_change_reuses_observed_pace_without_inventing_progress(self):
        self.estimate.start(0, 4)
        self.clock.advance(10)
        self.estimate.observe(1, 4)
        self.assertEqual(self.estimate.observe(1, 8).remaining_seconds, 70)
        self.assertEqual(self.estimate.observe(1, 3).remaining_seconds, 20)

    def test_counter_reset_relearns_pace_but_preserves_run_elapsed(self):
        self.estimate.start(0, 5)
        self.clock.advance(12)
        self.estimate.observe(2, 5)
        self.clock.advance(3)
        reset = self.estimate.observe(0, 5)
        self.assertIsNone(reset.remaining_seconds)
        self.assertEqual(reset.elapsed_seconds, 15)
        self.clock.advance(4)
        self.assertEqual(self.estimate.observe(1, 5).remaining_seconds, 16)

    def test_finish_freezes_elapsed_and_retains_incomplete_counts(self):
        self.estimate.start(0, 5)
        self.clock.advance(8)
        self.estimate.observe(1, 5)
        stopped = self.estimate.finish()
        self.assertFalse(stopped.running)
        self.assertEqual(stopped.done, 1)
        self.assertIsNone(stopped.remaining_seconds)
        self.clock.advance(50)
        self.assertEqual(self.estimate.observe(5, 5), stopped)
        self.assertEqual(self.estimate.finish(), stopped)

    def test_all_units_do_not_end_lifecycle_and_restart_clears_old_timing(self):
        self.estimate.start(0, 1)
        self.clock.advance(10)
        complete = self.estimate.observe(1, 1)
        self.assertTrue(complete.running)
        self.assertEqual(complete.remaining_seconds, 0)
        self.estimate.finish()
        self.clock.advance(100)
        new_run = self.estimate.start(0, 5)
        self.assertEqual(new_run.elapsed_seconds, 0)
        self.assertIsNone(new_run.remaining_seconds)
        self.clock.advance(2)
        self.assertEqual(self.estimate.observe(1, 5).remaining_seconds, 8)

    def test_unknown_total_and_zero_time_observations_remain_finite(self):
        self.estimate.start(0, 0)
        self.assertIsNone(self.estimate.observe(1, 0).remaining_seconds)
        self.clock.advance(6)
        self.assertIsNone(self.estimate.observe(2, 0).remaining_seconds)
        self.assertEqual(self.estimate.observe(2, 5).remaining_seconds, 18)
        self.estimate.reset()
        reset = self.estimate.snapshot()
        self.assertEqual((reset.running, reset.done, reset.total), (False, 0, 0))
        self.assertEqual(reset.elapsed_seconds, 0)
        self.assertIsNone(reset.remaining_seconds)


if __name__ == "__main__":
    unittest.main()
