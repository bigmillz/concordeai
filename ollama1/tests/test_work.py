"""What makes the fans and the lights work (lib/o1work.py, 6b421, per the owner): the card over 50%
for 1.5 s in a row (and at or under it 1.5 s in a row to end), the processor's temperature from
60 C until it is under 55 C, and the shared numbers (120 s cool-down, 5 s rise, 300 s then 40%)."""
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1work


def feed(trigger, samples, t0=100.0, step=0.25):
    """[(t, on)] after each sample."""
    out = []
    for i, v in enumerate(samples):
        t = t0 + i * step
        out.append((t, trigger.update(t, v)))
    return out


class TestNumbers(unittest.TestCase):
    def test_the_shared_numbers(self):
        self.assertEqual(o1work.GPU_BUSY_PCT, 50)
        self.assertEqual(o1work.GPU_CONFIRM_S, 1.5)
        self.assertEqual((o1work.CPU_HOT_C, o1work.CPU_COOL_C), (60, 55))
        self.assertEqual(o1work.COOL_S, 120)
        self.assertEqual(o1work.RISE_S, 5.0)
        self.assertEqual((o1work.IDLE_DIM_S, o1work.DIM_PCT, o1work.DIM_S), (300.0, 40, 10.0))

    def test_the_probes_the_services_build_read_only_the_card(self):
        self.assertEqual(set(o1work.probes()), {"gpu_busy"})

    def test_a_reading_is_a_real_number(self):
        for v, want in ((51, 51.0), (50.5, 50.5), (0, 0.0), (None, None), (True, None), ("80", None),
                        (float("nan"), None), ([80], None)):
            self.assertEqual(o1work.number(v), want, v)


class TestGpuTrigger(unittest.TestCase):
    def test_over_50_for_1_5_s_in_a_row_starts_it(self):
        g = o1work.GpuTrigger()
        out = feed(g, [100] * 8)                                   # 0, .25 ... 1.75 s
        self.assertEqual([on for _t, on in out], [False] * 6 + [True] * 2)      # on at the sample 1.5 s after the first
        self.assertEqual(g.since, 101.5)

    def test_50_exactly_is_not_over_it(self):
        g = o1work.GpuTrigger()
        self.assertFalse(any(on for _t, on in feed(g, [50] * 40)))
        g = o1work.GpuTrigger()
        self.assertTrue(feed(g, [50.1] * 7)[-1][1])

    def test_a_blip_shorter_than_1_5_s_does_nothing(self):
        g = o1work.GpuTrigger()
        out = feed(g, [100] * 5 + [0] + [100] * 5 + [0] * 4)       # 1 s, a dip, 1 s
        self.assertFalse(any(on for _t, on in out))

    def test_at_or_under_50_for_1_5_s_in_a_row_ends_it_and_a_dip_does_not(self):
        g = o1work.GpuTrigger()
        feed(g, [100] * 7)
        self.assertTrue(g.on)
        out = feed(g, [0] * 5 + [99] + [30] * 5 + [99], t0=200)     # dips of 1 s: still on
        self.assertTrue(all(on for _t, on in out))
        out = feed(g, [10] * 7, t0=300)
        self.assertEqual([on for _t, on in out], [True] * 6 + [False])
        self.assertEqual(g.since, 301.5)

    def test_two_samples_2_s_apart_are_enough_and_one_is_not(self):
        g = o1work.GpuTrigger()
        self.assertFalse(g.update(10.0, 90))
        self.assertTrue(g.update(12.0, 90))
        self.assertTrue(g.update(14.0, 0))                            # one low sample: still on
        self.assertFalse(g.update(16.0, 0))

    def test_no_reading_counts_as_not_busy(self):
        g = o1work.GpuTrigger()
        feed(g, [100] * 7)
        out = feed(g, [None] * 7, t0=200)
        self.assertFalse(out[-1][1])
        self.assertIsNone(g.pct)
        g = o1work.GpuTrigger()
        self.assertFalse(any(on for _t, on in feed(g, [True, "99", float("nan")] * 10)))

    def test_a_clock_that_steps_back_starts_the_count_again(self):
        g = o1work.GpuTrigger()
        g.update(100.0, 99)
        self.assertFalse(g.update(99.0, 99))                          # back in time: a new run from here
        self.assertFalse(g.update(100.0, 99))
        self.assertTrue(g.update(100.5, 99))


class TestCpuTrigger(unittest.TestCase):
    def test_60_c_starts_it_and_59_9_does_not(self):
        c = o1work.CpuTrigger()
        self.assertFalse(c.update([59.9]))
        self.assertTrue(c.update([60.0]))
        self.assertEqual(c.c, 60.0)

    def test_it_ends_under_55_not_at_it(self):
        c = o1work.CpuTrigger()
        c.update([70])
        for v in (59, 56, 55.0):
            self.assertTrue(c.update([v]), v)
        self.assertFalse(c.update([54.9]))
        self.assertFalse(c.update([58]))                              # not 60 again: stays off

    def test_the_hottest_of_several_counts(self):
        c = o1work.CpuTrigger()
        self.assertTrue(c.update([40, 61, None]))
        self.assertEqual(c.c, 61)

    def test_a_stuck_sensor_lets_go_and_no_sensor_keeps_the_state(self):
        c = o1work.CpuTrigger()
        c.update([70])
        self.assertTrue(c.update([]))
        self.assertFalse(c.update([None]))
        self.assertFalse(c.update([]))


if __name__ == "__main__":
    unittest.main()
