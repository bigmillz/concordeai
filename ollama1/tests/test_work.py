"""What counts as "working" (lib/o1work.py, 6b401), shared by the fan service and the
lights: a request and a running tool at once; the card at 15% averaged over 6 s;
the processors at 40% averaged over 10 s from /proc/stat deltas without iowait;
a 6 s hysteresis; and no load average. A fake /proc/stat and a fake clock."""
import os
import shutil
import tempfile
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1work

CPUS = 16
HZ = 100


class Machine:
    """A fake /proc/stat of a 16-thread machine, advanced a second at a time."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="o1work-")
        os.makedirs(self.root + "/proc")
        self.c = dict(user=1000, nice=0, system=500, idle=100000, iowait=0, irq=0, softirq=0, steal=0)
        self.t = 5000.0
        self.gpu = 0
        self.inflight = 0
        self.tools = []
        self.loadavg = 0.1
        self.write()
        self.work = o1work.Work({"inflight": lambda: self.inflight, "gpu_busy": lambda: self.gpu,
                                 "tools": lambda: self.tools, "cpu": lambda: o1work.cpu_times(self.root + "/proc"),
                                 "loadavg": lambda: self.loadavg},
                                clock=lambda: self.t, tools_every=0)

    def write(self):
        c = self.c
        open(self.root + "/proc/stat", "w").write(
            "cpu  %(user)d %(nice)d %(system)d %(idle)d %(iowait)d %(irq)d %(softirq)d %(steal)d 0 0\n"
            "cpu0 1 2 3 4 5 6 7 8 0 0\nintr 1\nprocs_running 1\n" % c)

    def tick(self, dt=1.0, **frac):
        """Advance `dt` s with these shares of the whole machine (user=.45 ...; the rest idle), then ask."""
        jiffies = CPUS * HZ * dt
        used = 0.0
        for k, f in frac.items():
            self.c[k] += int(f * jiffies)
            used += f
        self.c["idle"] += int((1 - used) * jiffies)
        self.t += dt
        self.write()
        return self.work.working()

    def run(self, seconds, dt=1.0, **frac):
        out = []
        for _ in range(int(seconds / dt)):
            out.append(self.tick(dt, **frac))
        return out


class TestCpu(unittest.TestCase):
    def setUp(self):
        self.m = Machine()
        self.addCleanup(shutil.rmtree, self.m.root, True)
        self.m.work.working()                                   # the first sample

    def test_the_stat_line_busy_is_user_nice_system_irq_softirq_and_total_is_every_field(self):
        open(self.m.root + "/proc/stat", "w").write("cpu  100 10 50 800 40 5 5 3 0 0\ncpu0 1 2 3 4 5 6 7 8 0 0\n")
        self.assertEqual(o1work.cpu_times(self.m.root + "/proc"), (170, 1013))
        open(self.m.root + "/proc/stat", "w").write("cpu  1 2 3 4\n")                  # an old kernel's short line
        self.assertEqual(o1work.cpu_times(self.m.root + "/proc"), (6, 10))
        open(self.m.root + "/proc/stat", "w").write("cpu0 1 2 3 4\n")
        self.assertIsNone(o1work.cpu_times(self.m.root + "/proc"))
        open(self.m.root + "/proc/stat", "w").write("cpu  a b\n")
        self.assertIsNone(o1work.cpu_times(self.m.root + "/proc"))
        self.assertIsNone(o1work.cpu_times(self.m.root + "/nonexistent"))

    def test_a_boot_like_load_average_with_idle_processors_stays_idle(self):
        self.m.loadavg = 5.2                                    # the old rule called this working for minutes
        self.m.gpu = 0
        out = self.m.run(180, user=0.02, system=0.01)
        self.assertEqual({w for w, _ in out}, {False})
        self.assertEqual(self.m.work.cpu_avg and round(self.m.work.cpu_avg), 3)

    def test_the_load_average_is_not_a_probe_any_more(self):
        self.assertNotIn("loadavg", o1work.probes())
        self.m.loadavg = 99
        self.assertEqual(self.m.tick(1, user=0.0)[0], False)

    def test_a_3_second_burst_of_100_percent_does_not_trigger(self):
        out = self.m.run(3, user=0.9, system=0.1)
        out += self.m.run(40)
        self.assertEqual({w for w, _ in out}, {False})

    def test_10_seconds_of_45_percent_does(self):
        out = self.m.run(12, user=0.45)
        self.assertEqual([w for w, _ in out[:6]], [False] * 6)
        self.assertTrue(out[10][0])                             # by the 10th second
        why = out[-1][1]
        self.assertEqual(why, ["processors 45% busy"])

    def test_35_percent_never_does(self):
        self.assertEqual({w for w, _ in self.m.run(120, user=0.35)}, {False})

    def test_40_percent_exactly_does(self):
        self.assertTrue(self.m.run(12, user=0.40)[-1][0])

    def test_time_waiting_on_a_disk_is_not_work(self):
        out = self.m.run(120, user=0.04, system=0.03, iowait=0.85)           # D-state tasks: a RAID check, an apt burst
        self.assertEqual({w for w, _ in out}, {False})

    def test_iowait_does_not_count_even_when_it_is_all_there_is(self):
        self.assertEqual({w for w, _ in self.m.run(60, iowait=1.0)}, {False})

    def test_steal_is_not_busy_but_nice_system_irq_and_softirq_are(self):
        self.assertFalse(self.m.run(30, steal=0.6)[-1][0])
        for k in ("nice", "system", "irq", "softirq"):
            with self.subTest(k):
                m = Machine()
                self.addCleanup(shutil.rmtree, m.root, True)
                m.work.working()
                self.assertTrue(m.run(12, **{k: 0.5})[-1][0], k)

    def test_it_lets_go_6_seconds_after_the_value_is_under_the_threshold(self):
        self.m.run(15, user=0.8)
        self.assertTrue(self.m.work.on["cpu"])
        under = None
        for i in range(40):
            w, _ = self.m.tick(1, user=0.0)
            if self.m.work.cpu_avg is not None and self.m.work.cpu_avg < 40 and under is None:
                under = i
            if under is not None and i == under + 5:
                self.assertTrue(w, "still working 5 s after it fell under 40%")
            if under is not None and i == under + 6:
                self.assertFalse(w, "idle 6 s after")
                break
        else:
            self.fail("never let go")

    def test_a_dip_shorter_than_6_s_keeps_it_working(self):
        self.m.run(15, user=0.8)
        self.m.run(8, user=0.0)                                 # the average falls under 40 ...
        for _ in range(3):
            self.m.tick(1, user=1.0)                            # ... and comes back before 6 s
        self.assertTrue(self.m.work.working()[0])
        self.assertTrue(self.m.work.on["cpu"])

    def test_the_first_seconds_say_nothing(self):
        m = Machine()
        self.addCleanup(shutil.rmtree, m.root, True)
        out = m.run(5, user=1.0)
        self.assertEqual({w for w, _ in out}, {False})

    def test_a_poll_every_2_seconds_works_the_same(self):
        out = self.m.run(30, dt=2.0, user=0.45)
        self.assertTrue(out[-1][0])
        self.assertFalse(out[1][0])
        m = Machine()
        self.addCleanup(shutil.rmtree, m.root, True)
        m.work.working()
        self.assertEqual({w for w, _ in m.run(60, dt=2.0, user=0.1)}, {False})

    def test_a_cpu_probe_that_fails_or_goes_backwards_is_not_work(self):
        w = o1work.Work({"cpu": lambda: 1 / 0}, clock=lambda: 1.0, tools_every=0)
        self.assertEqual(w.working(1.0), (False, []))
        t = [0.0]
        vals = iter([(1000, 2000), (10, 3000)] + [(10, 4000)] * 20)
        w = o1work.Work({"cpu": lambda: next(vals)}, clock=lambda: t[0], tools_every=0)
        for _ in range(12):
            t[0] += 1
            self.assertEqual(w.working(t[0])[0], False)


class TestGpu(unittest.TestCase):
    def setUp(self):
        self.m = Machine()
        self.addCleanup(shutil.rmtree, self.m.root, True)

    def test_14_percent_sustained_is_not_work_and_16_is(self):
        for pct, works in ((14, False), (16, True), (15, True)):
            with self.subTest(pct):
                m = Machine()
                self.addCleanup(shutil.rmtree, m.root, True)
                m.gpu = pct
                out = m.run(30)
                self.assertEqual(out[-1][0], works)
                if works:
                    self.assertTrue(out[6][0])                    # within about 6 s
                    self.assertFalse(out[1][0])                   # not on the first samples
                    self.assertEqual(out[-1][1], ["the card is %d%% busy" % pct])

    def test_it_is_an_average_over_6_seconds(self):
        out = []
        for g in [0, 30] * 12:                                    # 15% on average
            self.m.gpu = g
            out.append(self.m.tick(1)[0])
        self.assertTrue(out[-1])
        m = Machine()
        self.addCleanup(shutil.rmtree, m.root, True)
        for g in [0, 0, 0, 0, 0, 0, 100, 0, 0, 0, 0, 0, 0, 0]:    # one second at 100%: 14% of a 7-sample window
            m.gpu = g
            self.assertFalse(m.tick(1)[0])

    def test_one_sample_says_nothing(self):
        self.m.gpu = 100
        self.assertFalse(self.m.tick(1)[0])

    def test_it_lets_go_6_seconds_after_the_value_is_under_15(self):
        self.m.gpu = 60
        self.m.run(12)
        self.assertTrue(self.m.work.on["gpu"])
        self.m.gpu = 0
        under = None
        for i in range(30):
            w, _ = self.m.tick(1)
            if self.m.work.gpu_avg is not None and self.m.work.gpu_avg < 15 and under is None:
                under = i
            if under is not None and i == under + 5:
                self.assertTrue(w)
            if under is not None and i == under + 6:
                self.assertFalse(w)
                break
        else:
            self.fail("never let go")

    def test_no_card_is_fine(self):
        self.m.gpu = None
        self.assertEqual({w for w, _ in self.m.run(30)}, {False})


class TestImmediate(unittest.TestCase):
    def setUp(self):
        self.m = Machine()
        self.addCleanup(shutil.rmtree, self.m.root, True)

    def test_a_request_is_work_at_once(self):
        self.m.inflight = 1
        self.assertEqual(self.m.tick(1), (True, ["a request is running"]))
        self.m.inflight = 0
        self.assertEqual(self.m.tick(1)[0], False)

    def test_a_running_tool_is_work_at_once_and_says_which(self):
        self.m.tools = ["stability-test.sh"]
        self.assertEqual(self.m.tick(1), (True, ["running stability-test.sh"]))
        self.m.tools = ["ram_model_test.py", "stability-test.sh"]
        self.assertEqual(self.m.tick(1)[1], ["running ram_model_test.py, stability-test.sh"])
        self.m.tools = []
        self.assertFalse(self.m.tick(1)[0])

    def test_which_signal_made_it_work(self):
        self.m.inflight, self.m.gpu = 1, 62
        self.m.tools = ["stability-test.sh"]
        out = self.m.run(12, user=0.71)
        self.assertEqual(out[-1][1], ["a request is running", "running stability-test.sh", "the card is 62% busy",
                                      "processors 71% busy"])

    def test_a_probe_that_raises_is_not_work(self):
        def boom():
            raise OSError("x")
        w = o1work.Work({k: boom for k in ("inflight", "gpu_busy", "tools", "cpu")}, clock=lambda: 1.0, tools_every=0)
        self.assertEqual(w.working(1.0), (False, []))

    def test_a_bool_is_not_a_request_or_a_percentage(self):
        w = o1work.Work({"inflight": lambda: True, "gpu_busy": lambda: True}, clock=lambda: 1.0, tools_every=0)
        self.assertEqual(w.working(1.0), (False, []))

    def test_the_probes_the_services_build(self):
        self.assertEqual(set(o1work.probes()), {"inflight", "gpu_busy", "tools", "cpu"})


if __name__ == "__main__":
    unittest.main()
