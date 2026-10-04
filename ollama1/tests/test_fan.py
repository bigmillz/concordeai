"""Fan levels that follow the server's work (6b385, 6b386): the service's machine
on a fake sysfs tree (fans whose rpm follows their pwm) and a fake clock.
Working 100%, then 100% for 60 s, 50% for 60 s, then 20%; a new request at any
time; a stalled fan raised to the lowest level that spins; a probable pump kept
at 100%; an output with no rpm left alone; originals restored after any exit;
the watchdog; the temperature override; a wake; the status text; the unit and
the setup wiring."""
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1fan

NCT_ENABLE = {1: 5, 2: 5, 3: 1, 4: 0, 5: 5, 6: 5, 7: 5}      # as the chip had them
NCT_PWM = {1: 90, 2: 90, 3: 120, 4: 255, 5: 90, 6: 90, 7: 90}
T0 = 1000.0


def spins(pwm):
    return int(pwm * 10)                                      # 255 -> 2550 rpm, 51 (20%) -> 510


class Tree:
    """A fake /sys/class/hwmon: the card (amdgpu), the motherboard's chip (nct6797),
    the CPU, an NVMe drive. spin() sets each fan's rpm from its pwm, as a fan would."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="o1fan-sys-")
        self.base = self.root + "/class/hwmon"
        self.gpu = self.put("hwmon0", "amdgpu", {"pwm1": 80, "pwm1_enable": 2, "fan1_input": 1200,
                                                 "temp1_input": 45000, "temp1_label": "edge",
                                                 "temp2_input": 52000, "temp2_label": "junction",
                                                 "temp3_input": 60000, "temp3_label": "mem"})
        files = {}
        for n in range(1, 8):
            files.update({"pwm%d" % n: NCT_PWM[n], "pwm%d_enable" % n: NCT_ENABLE[n], "fan%d_input" % n: 500 + n})
        files.update({"temp1_input": 34000, "temp1_label": "SYSTIN", "temp2_input": 38000, "temp2_label": "CPUTIN",
                      "temp3_input": -128000, "temp3_label": "AUXTIN0"})          # an unplugged input reads -128
        self.nct = self.put("hwmon1", "nct6797", files)
        self.cpu = self.put("hwmon2", "k10temp", {"temp1_input": 41000, "temp1_label": "Tctl"})
        self.nvme = self.put("hwmon3", "nvme", {"temp1_input": 38000, "temp1_label": "Composite"})
        self.dimm1 = self.put("hwmon4", "jc42", {"temp1_input": 31000})
        self.dimm2 = self.put("hwmon5", "jc42", {"temp1_input": 32000})
        self.fn = {}                                          # (dir, n) -> rpm as a function of pwm

    def put(self, d, name, files):
        p = "%s/%s" % (self.base, d)
        os.makedirs(p)
        open(p + "/name", "w").write(name + "\n")
        for k, v in files.items():
            open("%s/%s" % (p, k), "w").write("%s\n" % v)
        return p

    def get(self, d, f):
        return int(open("%s/%s" % (d, f)).read().strip())

    def set(self, d, f, v):
        open("%s/%s" % (d, f), "w").write("%s\n" % v)

    def spin(self):
        for d, nums in ((self.gpu, (1,)), (self.nct, range(1, 8))):
            for n in nums:
                self.set(d, "fan%d_input" % n, self.fn.get((d, n), spins)(self.get(d, "pwm%d" % n)))

    def temp_in(self, d, c, file="temp1_input"):
        self.set(d, file, int(c * 1000))

    def temp(self, which, c, file="temp1_input"):
        self.set({"cpu": self.cpu, "nvme": self.nvme, "gpu": self.gpu}[which], file, int(c * 1000))

    def at(self, pwm, skip=()):
        """True when every output (but `skip`, nct numbers) is manual at `pwm`."""
        return (self.get(self.gpu, "pwm1_enable") == 1 and self.get(self.gpu, "pwm1") == pwm and
                all(self.get(self.nct, "pwm%d_enable" % n) == 1 and self.get(self.nct, "pwm%d" % n) == pwm
                    for n in range(1, 8) if n not in skip))

    def as_before(self):
        return (self.get(self.gpu, "pwm1_enable") == 2 and
                all(self.get(self.nct, "pwm%d_enable" % n) == NCT_ENABLE[n] for n in range(1, 8)) and
                self.get(self.nct, "pwm3") == NCT_PWM[3])           # the manual one: its value too (an auto output's
                                                                     # value file is the driver's to move)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class RecordingIO(o1fan.SysfsIO):
    def __init__(self):
        self.writes = []

    def write(self, path, value):
        self.writes.append((path, value))
        super().write(path, value)


class FanCase(unittest.TestCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(shutil.rmtree, self.tree.root, True)
        self.clean()
        self.addCleanup(self.clean)
        self.clock = Clock()
        self.p = {"inflight": 0, "gpu_busy": 0, "tools": [], "loadavg": 0.1}
        self.lines = []
        self.boot = "boot-1"

    def clean(self):
        for f in (o1fan.state_path(), o1fan.status_path()):
            if os.path.exists(f):
                os.unlink(f)

    def fan(self, **kw):
        probes = {k: (lambda k=k: self.p[k]) for k in self.p}
        kw.setdefault("io", RecordingIO())
        return o1fan.Fan(probes, clock=self.clock, wall=lambda: 1_800_000_000, log=self.lines.append,
                         sysroot=self.tree.root, boot_id=lambda: self.boot, tools_every=0, **kw)

    def go(self, fan, t):
        """The clock to `t`, the fans spin as their pwm says, one tick: (phase, why)."""
        self.clock.t = t
        self.tree.spin()
        return fan.tick()

    def run_for(self, fan, seconds, step=1):
        end = self.clock.t + seconds
        phases = []
        while self.clock.t < end:
            phases.append(self.go(fan, self.clock.t + step)[0])
        return phases

    def up(self, **kw):
        """A fan service started on a quiet server and run long enough to measure and settle at 20%."""
        f = self.fan(**kw)
        self.run_for(f, 40)
        self.assertEqual(f.phase, "idle20")
        return f

    def learned(self):
        return json.load(open(o1fan.state_path()))["learned"]


class TestLevels(FanCase):
    def test_the_first_start_measures_each_fan_at_full_speed_then_goes_to_20(self):
        f = self.fan()
        self.assertEqual(self.go(f, T0 + 1)[0], "calibrating")
        self.assertTrue(self.tree.at(255))
        self.assertEqual(self.go(f, T0 + 5)[0], "calibrating")           # not settled: 6 s
        self.assertEqual(self.go(f, T0 + 7)[0], "calibrating")           # 6 s settled: measured on this tick
        self.assertEqual(self.go(f, T0 + 8)[0], "idle20")
        self.assertTrue(self.tree.at(51))                                # 20% = 51
        L = self.learned()
        self.assertEqual(L["nct6797/pwm2"]["rpm100"], 2550)
        self.assertEqual(L["amdgpu/pwm1"]["rpm100"], 2550)
        self.assertIn("controlling GPU fan + 7 case/CPU fan outputs", self.lines)

    def test_what_was_learned_is_not_measured_again_after_a_restart(self):
        self.up()
        self.lines.clear()
        g = self.fan()
        self.assertEqual(self.go(g, self.clock.t + 1)[0], "hold100")      # found held: the sequence from its start
        self.assertEqual([l for l in self.lines if l.startswith("calibrating")], [])

    def test_idle_holds_20_percent_and_writes_nothing_more(self):
        f = self.up()
        f.io.writes.clear()
        self.run_for(f, 60)
        self.assertEqual(f.io.writes, [])
        self.assertTrue(self.tree.at(51))

    def test_a_request_goes_to_100_in_manual(self):
        f = self.up()
        self.p["inflight"] = 1
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "working")
        self.assertTrue(self.tree.at(255))

    def test_each_source_of_work_counts_and_its_limit(self):
        for name, busy, quiet in (("inflight", 1, 0), ("gpu_busy", 10, 9), ("tools", ["stability-test.sh"], []),
                                  ("loadavg", 1.6, 1.5)):
            with self.subTest(name):
                self.setUp()
                f = self.up()
                self.p[name] = quiet
                self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20", name)
                self.p[name] = busy
                self.assertEqual(self.go(f, self.clock.t + 1)[0], "working", name)
                self.assertTrue(self.tree.at(255))

    def test_a_probe_that_fails_is_not_work(self):
        f = o1fan.Fan({"inflight": lambda: 1 / 0, "gpu_busy": lambda: None, "tools": lambda: None},
                      clock=self.clock, wall=lambda: 1_800_000_000, log=self.lines.append, sysroot=self.tree.root,
                      boot_id=lambda: self.boot)
        self.run_for(f, 20)
        self.assertEqual(f.phase, "idle20")

    def test_the_levels_are_round_pct_255_over_100(self):
        self.assertEqual([o1fan.pwm_of(p) for p in (20, 30, 40, 50, 100)], [51, 77, 102, 128, 255])


class TestSequence(FanCase):
    """After the work ends: 100% for 60 s, 50% for 60 s, then 20%."""

    def after_work(self, seconds):
        f = self.up()
        self.p["inflight"] = 1
        t = self.clock.t + 1
        self.go(f, t)
        self.p["inflight"] = 0
        return f, t, self.go(f, t + seconds)[0]

    def test_exact_boundaries(self):
        for dt, phase, pwm in ((1, "hold100", 255), (59, "hold100", 255), (60, "hold50", 128), (61, "hold50", 128),
                               (119, "hold50", 128), (120, "idle20", 51), (121, "idle20", 51)):
            with self.subTest(dt):
                self.setUp()
                f, t, got = self.after_work(dt)
                self.assertEqual(got, phase, dt)
                self.assertTrue(self.tree.at(pwm), dt)

    def test_a_new_request_in_the_50_phase_goes_back_to_100_and_starts_over(self):
        f, t, _ = self.after_work(90)
        self.assertEqual(f.phase, "hold50")
        self.assertTrue(self.tree.at(128))
        self.p["inflight"] = 1
        self.assertEqual(self.go(f, t + 91)[0], "working")
        self.assertTrue(self.tree.at(255))
        self.p["inflight"] = 0
        self.assertEqual(self.go(f, t + 91 + 59)[0], "hold100")
        self.assertEqual(self.go(f, t + 91 + 60)[0], "hold50")
        self.assertEqual(self.go(f, t + 91 + 119)[0], "hold50")
        self.assertEqual(self.go(f, t + 91 + 120)[0], "idle20")
        self.assertTrue(self.tree.at(51))

    def test_a_new_request_in_the_100_hold_starts_the_sequence_again(self):
        f, t, _ = self.after_work(40)
        self.p["inflight"] = 1
        self.go(f, t + 41)
        self.p["inflight"] = 0
        self.assertEqual(self.go(f, t + 41 + 59)[0], "hold100")
        self.assertEqual(self.go(f, t + 41 + 60)[0], "hold50")

    def test_working_all_along_never_lets_go(self):
        f = self.up()
        self.p["gpu_busy"] = 99
        self.assertEqual(set(self.run_for(f, 300, step=2)), {"working"})
        self.assertTrue(self.tree.at(255))

    def test_the_countdown_is_in_the_status(self):
        f, t, _ = self.after_work(18)
        st = o1fan.read_status(now=1_800_000_000)
        self.assertEqual((st["phase"], st["hold_left"], st["pct"]), ("hold100", 42, 100))
        self.go(f, t + 70)
        st = o1fan.read_status(now=1_800_000_000)
        self.assertEqual((st["phase"], st["hold_left"], st["pct"]), ("hold50", 50, 50))

    def test_only_the_levels_are_ever_written(self):
        f = self.fan()
        self.run_for(f, 30)
        self.p["inflight"] = 1
        self.run_for(f, 10)
        self.p["inflight"] = 0
        self.run_for(f, 200)
        f.shutdown()
        for path, value in f.io.writes:
            if path.endswith("_enable"):
                self.assertIn(value, ("1", "2", "5", "0"), path)
            else:
                self.assertIn(value, ("255", "128", "51", "120"), path)    # 120: the manual output's own value, put back


class TestOverride(FanCase):
    def test_a_hot_part_forces_full_with_nothing_running(self):
        for which, limit, file in (("cpu", 80, "temp1_input"), ("gpu", 90, "temp2_input"), ("nvme", 70, "temp1_input")):
            with self.subTest(which):
                self.setUp()
                f = self.up()
                self.tree.temp(which, limit - 0.5, file)
                self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")
                self.tree.temp(which, limit, file)
                phase, why = self.go(f, self.clock.t + 1)
                self.assertEqual(phase, "hot")
                self.assertIn("too warm", why)
                self.assertTrue(self.tree.at(255))

    def test_it_lets_go_ten_degrees_under_the_limit_and_not_before(self):
        f = self.up()
        self.tree.temp("cpu", 83)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")
        self.tree.temp("cpu", 79)                # under the limit, not 10 under
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")
        self.tree.temp("cpu", 70.0)              # exactly 10 under is not under yet
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")
        self.tree.temp("cpu", 69.9)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")
        self.assertTrue(self.tree.at(51))
        self.tree.temp("cpu", 75)                # cooler than the limit but never over it again: stays at 20%
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")

    def test_after_it_lets_go_the_normal_rule_follows(self):
        f = self.up()
        self.tree.temp("gpu", 95, "temp2_input")
        self.go(f, self.clock.t + 1)
        self.p["loadavg"] = 5
        self.tree.temp("gpu", 40, "temp2_input")
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "working")
        self.p["loadavg"] = 0.1
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hold100")

    def test_a_sensor_that_stops_answering_keeps_its_state(self):
        f = self.up()
        self.tree.temp("cpu", 85)
        self.go(f, self.clock.t + 1)
        os.unlink(self.tree.cpu + "/temp1_input")
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")


class TestTempSources(FanCase):
    """Every temperature that can force 100%: the chip's inputs by label, the DIMMs, the card's memory and edge."""

    def board(self, n, label, c):
        self.tree.set(self.tree.nct, "temp%d_input" % n, int(c * 1000))
        if label is not None:
            self.tree.set(self.tree.nct, "temp%d_label" % n, label)

    def check(self, setter, limit, name):
        """Below the limit nothing; at it, 100% with nothing running; 9.9 under still; 10.1 under, let go."""
        f = self.up()
        setter(limit - 0.1)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20", name)
        setter(limit)
        phase, why = self.go(f, self.clock.t + 1)
        self.assertEqual(phase, "hot", name)
        self.assertIn("(limit %d)" % limit, why)
        self.assertTrue(self.tree.at(255), name)
        setter(limit - 9.9)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot", name)
        setter(limit - 10.1)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20", name)

    def test_the_chips_inputs_by_label(self):
        for label, limit in (("SYSTIN", 70), ("CPUTIN", 85), ("AUXTIN1", 70), ("PECI Agent 0", 85), ("TSI0_TEMP", 85),
                             ("VRM", 90), ("MOSFET", 90), ("Chipset", 80), ("PCH_CHIP_TEMP", 80),
                             ("Mystery sensor", 70), (None, 70)):
            with self.subTest(label):
                self.setUp()
                self.board(5, label or "temp5", 30)
                if label is None:
                    os.unlink(self.tree.nct + "/temp5_label")
                self.check(lambda c: self.board(5, None, c), limit, label)

    def test_the_cpu_input_is_not_held_to_the_board_limit(self):
        self.setUp()
        f = self.up()
        self.board(2, None, 75)                                    # CPUTIN at 75 is fine (85); SYSTIN at 75 is not
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")
        self.board(1, None, 75)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")

    def test_the_dimms(self):
        for d in ("dimm1", "dimm2"):
            with self.subTest(d):
                self.setUp()
                self.check(lambda c: self.tree.temp_in(getattr(self.tree, d), c), 70, d)

    def test_the_cards_memory_and_edge(self):
        self.check(lambda c: self.tree.temp("gpu", c, "temp3_input"), 95, "mem")
        self.setUp()
        self.check(lambda c: self.tree.temp("gpu", c, "temp1_input"), 85, "edge")
        self.setUp()
        self.check(lambda c: self.tree.temp("gpu", c, "temp2_input"), 90, "junction")

    def test_a_disconnected_or_stuck_sensor_is_ignored(self):
        for c in (0, -0.5, -128, 120, 125, 255, 1000):
            with self.subTest(c):
                self.setUp()
                f = self.up()
                for setter in (lambda v: self.board(5, "SYSTIN", v), lambda v: self.tree.temp_in(self.tree.dimm1, v),
                               lambda v: self.tree.temp("gpu", v, "temp3_input"), lambda v: self.tree.temp("cpu", v),
                               lambda v: self.tree.temp("nvme", v)):
                    setter(c)
                self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20", c)
                self.assertTrue(self.tree.at(51))
                self.assertEqual(f.hot, {})

    def test_a_sensor_that_sticks_while_hot_is_let_go(self):
        f = self.up()
        self.board(5, "SYSTIN", 75)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")
        self.board(5, None, 255)                                   # stuck at an implausible value
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")
        self.board(5, None, -128)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")

    def test_a_plausible_reading_just_inside_the_range_counts(self):
        f = self.up()
        self.board(5, "SYSTIN", 119.9)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")
        self.board(5, None, 0.5)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "idle20")

    def test_a_zero_reading_is_not_shown_as_a_temperature(self):
        self.board(1, None, 0)
        names = [t["label"] for t in o1fan.snapshot([], sysroot=self.tree.root)["temps"]]
        self.assertNotIn("SYSTIN", names)
        self.assertNotIn("AUXTIN0", names)
        self.board(1, None, 0.5)
        names = [t["label"] for t in o1fan.snapshot([], sysroot=self.tree.root)["temps"]]
        self.assertIn("SYSTIN", names)

    def test_two_sensors_with_one_name_are_numbered(self):
        names = [t[1] for t in o1fan.read_temps(self.tree.root)]
        self.assertIn("DIMM 1", names)
        self.assertIn("DIMM 2", names)
        self.assertNotIn("DIMM", names)

    def test_the_summary_names_the_sensor_closest_to_its_limit(self):
        f = self.up()
        self.board(1, None, 56)                                    # SYSTIN: 14 under its 70
        self.go(f, self.clock.t + 1)
        st = o1fan.read_status(now=1_800_000_000)
        self.assertEqual(st["closest"], {"label": "SYSTIN", "c": 56.0, "limit": 70, "margin": 14.0})
        self.assertEqual(st["hottest"]["label"], "GPU memory")
        self.assertIn("closest to its limit: SYSTIN 56 of 70 C", o1fan.panel_line(now=1_800_000_000))
        text = o1fan.render_status(st, o1fan.snapshot(o1fan.find_outputs(self.tree.root), sysroot=self.tree.root))
        self.assertIn("closest to its limit: SYSTIN 56 C of 70 (14 under)", text)
        for part in ("CPU 41/80", "GPU junction 52/90", "GPU memory 60/95", "GPU edge 45/85", "NVMe 38/70", "DIMM 1 31/70",
                     "CPUTIN 38/85"):
            self.assertIn(part, text)
        self.assertNotIn("AUXTIN0", text)                          # unplugged: not shown

    def test_the_label_decides_the_limit(self):
        for label, limit in (("SYSTIN", 70), ("CPUTIN", 85), ("PECI Agent 0", 85), ("TSI0_TEMP", 85), ("VRM MOS", 90),
                             ("MOSFET 2", 90), ("CMOS", 70), ("Chipset", 80), ("PCH", 80), ("AUXTIN3", 70), ("x", 70)):
            self.assertEqual(o1fan.board_limit(label), limit, label)


class TestStall(FanCase):
    """A low level is checked against the fan's rpm and never trusted."""

    def test_a_stalled_output_is_raised_in_steps_to_the_lowest_level_that_spins(self):
        self.tree.fn[(self.tree.nct, 2)] = lambda pwm: 0 if pwm < 100 else spins(pwm)     # stalls under 40%
        f = self.up()
        self.assertEqual(self.tree.get(self.tree.nct, "pwm2"), 102)      # 20% stalled, 30% stalled, 40% spins
        self.assertEqual(self.tree.get(self.tree.nct, "pwm1"), 51)       # the others stay at 20%
        self.assertEqual(self.learned()["nct6797/pwm2"]["min_pct"], 40)
        said = [l for l in self.lines if l.startswith("case/CPU fan 2:") and "stalled" in l]
        self.assertEqual(len(said), 2)
        self.assertIn("its lowest level is now 40%", said[-1])
        self.run_for(f, 60)
        self.assertEqual(len([l for l in self.lines if "stalled" in l]), 2)        # not again
        g = self.fan()                                                   # a restart remembers it
        self.run_for(g, 140)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm2"), 102)
        self.assertEqual(len([l for l in self.lines if "stalled" in l]), 2)

    def test_the_floor_applies_at_50_too_and_full_is_still_255(self):
        self.tree.fn[(self.tree.nct, 2)] = lambda pwm: 0 if pwm < 120 else spins(pwm)    # stalls under ~47%: floor 50%
        f = self.up()
        self.assertEqual(self.tree.get(self.tree.nct, "pwm2"), 128)
        self.p["inflight"] = 1
        self.go(f, self.clock.t + 1)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm2"), 255)

    def test_an_output_under_its_own_minimum_counts_as_stalled(self):
        self.tree.set(self.tree.nct, "fan4_min", 900)
        self.up()
        self.assertEqual(self.learned()["nct6797/pwm4"]["min_pct"], 40)      # 510, 770 rpm under 900; 1020 is over
        self.assertEqual(self.tree.get(self.tree.nct, "pwm4"), 102)

    def test_a_fan_that_never_gets_going_ends_up_at_100_for_good(self):
        self.tree.fn[(self.tree.nct, 6)] = lambda pwm: 0 if pwm < 255 else spins(pwm)
        f = self.up()
        self.run_for(f, 60)
        L = self.learned()["nct6797/pwm6"]
        self.assertTrue(L["always100"])
        self.assertEqual(self.tree.get(self.tree.nct, "pwm6"), 255)

    def test_a_probable_pump_is_kept_at_100(self):
        self.tree.fn[(self.tree.nct, 3)] = lambda pwm: 2000          # the same at any pwm
        f = self.up()
        self.assertEqual(self.tree.get(self.tree.nct, "pwm3"), 255)
        self.assertTrue(self.learned()["nct6797/pwm3"]["always100"])
        pump = [l for l in self.lines if "a pump or a fixed header" in l]
        self.assertEqual(len(pump), 1)
        self.assertIn("case/CPU fan 3", pump[0])
        self.assertEqual(self.tree.get(self.tree.nct, "pwm1"), 51)
        self.run_for(f, 30)
        self.assertEqual(len([l for l in self.lines if "a pump" in l]), 1)
        st = o1fan.read_status(now=1_800_000_000)
        row = [r for r in st["outputs"] if r["label"] == "case/CPU fan 3"][0]
        self.assertIn("pump", row["note"])

    def test_60_percent_of_the_rpm_is_the_pump_line(self):
        for rpm, pump in ((1529, False), (1530, True)):               # 60% of 2550 is 1530
            with self.subTest(rpm):
                self.setUp()
                self.tree.fn[(self.tree.nct, 3)] = lambda pwm, rpm=rpm: spins(pwm) if pwm == 255 else rpm
                self.up()
                self.assertEqual(self.learned()["nct6797/pwm3"]["always100"], pump)

    def test_an_output_with_no_rpm_at_100_is_full_when_working_and_untouched_otherwise(self):
        self.tree.fn[(self.tree.nct, 5)] = lambda pwm: 0
        f = self.up()
        self.assertEqual(self.tree.get(self.tree.nct, "pwm5_enable"), 5)       # as the chip had it
        self.assertEqual(self.tree.get(self.tree.nct, "pwm1"), 51)
        self.p["inflight"] = 1
        self.go(f, self.clock.t + 1)
        self.assertEqual((self.tree.get(self.tree.nct, "pwm5_enable"), self.tree.get(self.tree.nct, "pwm5")), (1, 255))
        self.p["inflight"] = 0
        self.run_for(f, 130)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm5_enable"), 5)
        self.assertEqual(f.phase, "idle20")
        f.shutdown()
        self.assertTrue(self.tree.as_before())

    def test_no_rpm_reading_at_all_is_the_same(self):
        for n in range(1, 8):
            self.tree.fn[(self.tree.nct, n)] = lambda pwm: 0
        self.up()
        self.assertEqual(self.tree.get(self.tree.nct, "pwm1_enable"), 5)
        self.assertEqual(self.tree.get(self.tree.gpu, "pwm1"), 51)

    def test_the_status_shows_the_lowest_level_of_each_fan(self):
        self.tree.fn[(self.tree.nct, 2)] = lambda pwm: 0 if pwm < 100 else spins(pwm)
        self.up()
        text = o1fan.render_status(o1fan.read_status(now=1_800_000_000), self.snap())
        self.assertRegex(text, r"case/CPU fan 2\s+manual\s+pwm 102/255\s+1020 rpm\s+min 40%")
        self.assertRegex(text, r"case/CPU fan 1\s+manual\s+pwm 51/255\s+510 rpm\s+min 20%")

    def snap(self):
        return o1fan.snapshot(o1fan.find_outputs(self.tree.root), sysroot=self.tree.root)


class TestSkipAndFailures(FanCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anything")
    def test_an_output_it_cannot_write_is_left_alone(self):
        os.chmod(self.tree.nct + "/pwm3_enable", 0o444)
        self.up()
        self.assertIn("controlling GPU fan + 6 case/CPU fan outputs", self.lines)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm3_enable"), 1)         # untouched (it was 1 already)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm3"), 120)              # and its value was not written
        self.assertEqual(self.tree.get(self.tree.nct, "pwm2"), 51)

    def test_a_write_that_fails_is_skipped_and_said_once(self):
        class Fussy(RecordingIO):
            def write(self, path, value):
                if path.endswith("pwm5"):
                    raise OSError(5, "I/O error")
                super().write(path, value)
        f = self.fan(io=Fussy())
        self.run_for(f, 12)
        self.assertEqual(sum(1 for l in self.lines if "can't write hwmon1/pwm5" in l), 1)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm5_enable"), 1)         # the enable did change: restore must undo it
        f.shutdown()
        self.assertTrue(self.tree.as_before())
        self.assertEqual(self.tree.get(self.tree.nct, "pwm5_enable"), 5)

    def test_no_chip_at_all_is_not_an_error(self):
        shutil.rmtree(self.tree.base)
        os.makedirs(self.tree.base)
        f = self.fan()
        self.p["inflight"] = 1
        self.assertEqual(f.tick()[0], "working")
        self.assertIn("controlling no fan outputs it can write", self.lines)


class TestRestart(FanCase):
    def test_originals_are_saved_before_anything_changes(self):
        class Spy(RecordingIO):
            seen = None

            def write(inner, path, value):                     # noqa: N805
                if inner.seen is None:
                    inner.seen = json.load(open(o1fan.state_path()))
                super().write(path, value)
        f = self.fan(io=Spy())
        f.tick()
        self.assertEqual(f.io.seen["orig"]["hwmon0/pwm1"], {"enable": 2, "pwm": None})
        self.assertEqual(f.io.seen["orig"]["hwmon1/pwm3"], {"enable": 1, "pwm": 120})
        self.assertEqual(f.io.seen["boot"], "boot-1")
        self.assertEqual(oct(os.stat(o1fan.state_path()).st_mode & 0o777), "0o600")

    def test_a_clean_stop_puts_everything_back_and_keeps_what_was_learned(self):
        f = self.up()
        f.shutdown()
        self.assertTrue(self.tree.as_before())
        st = json.load(open(o1fan.state_path()))
        self.assertEqual(st["orig"], {})
        self.assertIn("amdgpu/pwm1", st["learned"])

    def test_stop_post_restores_after_any_exit_a_crash_included(self):
        for result in ("success", "exit-code", "signal", "watchdog", "timeout"):
            with self.subTest(result):
                self.setUp()
                self.up()                                                  # the service dies here, fans at 20%
                self.assertTrue(self.tree.at(51))
                self.assertTrue(o1fan.restore_after_stop({"SERVICE_RESULT": result}, sysroot=self.tree.root,
                                                         boot_id=lambda: self.boot, log=self.lines.append))
                self.assertTrue(self.tree.as_before(), result)
                self.assertTrue(o1fan.restore_after_stop({"SERVICE_RESULT": result}, sysroot=self.tree.root,
                                                         boot_id=lambda: self.boot, log=self.lines.append))   # again: nothing

    def test_a_restart_after_a_kill_takes_over_the_saved_originals(self):
        a = self.up()                                                      # killed: no shutdown, no stop-post
        b = self.fan()
        self.assertTrue(b.engaged)
        self.assertIn("found fans still held by an earlier run: 8 outputs, put back when it stops", self.lines)
        self.assertEqual(self.go(b, self.clock.t + 1)[0], "hold100")       # the sequence from its start, in case it was working
        self.assertTrue(self.tree.at(255))
        self.run_for(b, 125)
        self.assertEqual(b.phase, "idle20")
        b.shutdown()
        self.assertTrue(self.tree.as_before())                             # the first run's originals, not the manual ones
        self.assertEqual(a.orig, b.orig or a.orig)

    def test_another_boot_means_the_hardware_is_as_it_was_but_what_was_learned_stays(self):
        self.up()
        self.boot = "boot-2"                                  # rebooted: the chip is back to its own settings
        for n in range(1, 8):
            self.tree.set(self.tree.nct, "pwm%d_enable" % n, 5)
        self.tree.set(self.tree.gpu, "pwm1_enable", 2)
        b = self.fan()
        self.assertFalse(b.engaged)
        st = json.load(open(o1fan.state_path()))
        self.assertEqual(st["orig"], {})
        self.assertIn("nct6797/pwm2", st["learned"])
        self.assertEqual(self.go(b, self.clock.t + 1)[0], "idle20")        # known: no new measuring
        self.assertEqual(b.pct, 20)

    def test_a_restore_that_fails_is_kept_and_tried_again(self):
        class Fussy(RecordingIO):
            fail = False

            def write(inner, path, value):                     # noqa: N805
                if inner.fail and path.endswith("pwm1_enable"):
                    raise OSError(16, "busy")
                super().write(path, value)
        f = self.up(io=Fussy())
        f.io.fail = True
        self.assertFalse(f.release())
        self.assertEqual(self.tree.get(self.tree.gpu, "pwm1_enable"), 1)
        self.assertTrue(json.load(open(o1fan.state_path()))["orig"])
        f.io.fail = False
        self.assertTrue(f.release())
        self.assertTrue(self.tree.as_before())


class TestWake(FanCase):
    def test_after_a_wake_that_reset_the_card_the_current_level_is_written_again(self):
        f = self.up()
        for pwm, setup in ((51, None), (255, "inflight"), (128, "hold50")):
            if setup == "inflight":
                self.p["inflight"] = 1
                self.go(f, self.clock.t + 1)
            elif setup == "hold50":
                self.p["inflight"] = 0
                self.go(f, self.clock.t + 61)
            self.tree.set(self.tree.gpu, "pwm1_enable", 2)        # amdgpu on resume
            self.tree.set(self.tree.gpu, "pwm1", 80)
            for n in range(1, 8):
                self.tree.set(self.tree.nct, "pwm%d_enable" % n, 5)
            self.go(f, self.clock.t + 1)
            self.assertTrue(self.tree.at(pwm), pwm)

    def test_the_sleep_hook_tells_the_service_to_look(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        log = os.path.join(d, "calls")
        for name, body in (("systemctl", 'echo "$*" >> %s\ncase "$1" in is-active) exit "${ACTIVE:-0}" ;; '
                                          'is-enabled) exit 1 ;; esac\n' % log), ("helper", "exit 0\n")):
            with open(os.path.join(d, name), "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(os.path.join(d, name), 0o755)
        hook = os.path.join(U.CONFIG, "ollama1-sleep-hook")
        env = dict(os.environ, PATH=d + ":/usr/bin:/bin", OLLAMA1_HELPER=os.path.join(d, "helper"))
        subprocess.run(["sh", hook, "post", "suspend"], env=env, check=True)
        self.assertIn("kill --kill-whom=main --signal=USR1 ollama1-fan.service", open(log).read())
        os.unlink(log)
        subprocess.run(["sh", hook, "post", "suspend"], env=dict(env, ACTIVE="3"), check=True)
        self.assertNotIn("USR1", open(log).read())
        os.unlink(log)
        subprocess.run(["sh", hook, "pre", "suspend"], env=env, check=True)
        self.assertFalse(os.path.exists(log))


class TestWatchdog(FanCase):
    def receiver(self):
        d = tempfile.mkdtemp(prefix="o1n", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "n.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(path)
        s.settimeout(5)
        self.addCleanup(s.close)
        return s, path

    def test_notify_reaches_the_socket_and_is_a_no_op_outside_systemd(self):
        s, path = self.receiver()
        self.assertTrue(o1fan.sd_notify("READY=1", env={"NOTIFY_SOCKET": path}))
        self.assertEqual(s.recv(100), b"READY=1")
        self.assertFalse(o1fan.sd_notify("READY=1", env={}))
        self.assertFalse(o1fan.sd_notify("READY=1", env={"NOTIFY_SOCKET": "/nonexistent/n.sock"}))

    def test_an_abstract_socket_is_addressed_with_a_leading_nul(self):
        seen = []

        class S:
            def __init__(self, *a):
                pass

            def sendto(self, data, addr):
                seen.append((data, addr))

            def close(self):
                pass
        self.assertTrue(o1fan.sd_notify("WATCHDOG=1", env={"NOTIFY_SOCKET": "@run/x"}, factory=S))
        self.assertEqual(seen, [(b"WATCHDOG=1", "\0run/x")])

    def test_every_turn_of_the_loop_pings_even_when_the_tick_fails(self):
        sent = []
        f = self.fan()
        o1fan.service_step(f, notify=sent.append, log=self.lines.append, first=True)
        self.assertTrue(sent[0].startswith("READY=1\nWATCHDOG=1\n"))
        o1fan.service_step(f, notify=sent.append, log=self.lines.append)
        self.assertTrue(sent[1].startswith("WATCHDOG=1\n"))
        self.assertNotIn("READY", sent[1])

        def boom():
            raise RuntimeError("x")
        f.tick = boom
        o1fan.service_step(f, notify=sent.append, log=self.lines.append)
        self.assertEqual(len(sent), 3)
        self.assertIn("tick failed: RuntimeError", self.lines)

    def test_the_unit_has_the_watchdog_and_restores_after_any_exit(self):
        u = open(os.path.join(U.KIT, "systemd", "ollama1-fan.service")).read()
        for line in ("Type=notify", "NotifyAccess=main", "WatchdogSec=10", "Restart=always",
                     "ExecStopPost=/usr/local/lib/ollama1/bin/ollama1-fan stop-post"):
            self.assertIn("\n" + line + "\n", u, line)

    def test_the_service_runs_notifies_and_puts_the_fans_back_on_sigterm(self):
        s, path = self.receiver()
        env = dict(os.environ, OLLAMA1_SYS=self.tree.root, NOTIFY_SOCKET=path, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, os.path.join(U.BIN, "ollama1-fan"), "run"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            msgs = []
            while not any("READY=1" in m for m in msgs):
                msgs.append(s.recv(200).decode())
            self.assertTrue(any("WATCHDOG=1" in m for m in msgs))
            self.assertEqual(self.tree.get(self.tree.gpu, "pwm1_enable"), 1)           # held while it runs
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, out)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertTrue(self.tree.as_before())


class TestStatus(FanCase):
    def snap(self):
        return o1fan.snapshot(o1fan.find_outputs(self.tree.root), sysroot=self.tree.root)

    def text(self):
        return o1fan.render_status(o1fan.read_status(now=1_800_000_000), self.snap())

    def test_working_says_why_and_shows_each_fan(self):
        f = self.up()
        self.p["inflight"] = 1
        self.p["gpu_busy"] = 80
        self.go(f, self.clock.t + 1)
        self.go(f, self.clock.t + 1)
        text = self.text()
        self.assertIn("level: 100%  phase: working - a request is running; the graphics card is busy (80%)", text)
        self.assertIn("controlling: GPU fan + 7 case/CPU fan outputs", text)
        self.assertRegex(text, r"GPU fan\s+manual\s+pwm 255/255\s+2550 rpm\s+min 20%")
        self.assertRegex(text, r"case/CPU fan 3\s+manual\s+pwm 255/255\s+2550 rpm")
        self.assertIn("temps: highest GPU memory 60 C; closest to its limit: NVMe 38 C of 70 (32 under)", text)

    def test_the_phases_in_words(self):
        f = self.up()
        self.assertIn("level: 20%  phase: idle20", self.text())
        self.p["inflight"] = 1
        self.go(f, self.clock.t + 1)
        self.p["inflight"] = 0
        t = self.clock.t
        self.go(f, t + 18)
        self.assertIn("level: 100%  phase: hold100 42s", self.text())
        self.go(f, t + 70)
        self.assertIn("level: 50%  phase: hold50 50s", self.text())
        self.assertIn("isn't running", o1fan.render_status(None, self.snap()))

    def test_the_status_file_goes_stale(self):
        self.up()
        self.assertIsNotNone(o1fan.read_status(now=1_800_000_000 + 10))
        self.assertIsNone(o1fan.read_status(now=1_800_000_000 + 16))
        self.assertIsNone(o1fan.panel_line(now=1_800_000_000 + 16))

    def test_the_panel_line(self):
        f = self.up()
        self.assertEqual(o1fan.panel_line(now=1_800_000_000), "Fans: 20% (idle)  -  GPU fan 510 rpm, case fans up to 510 rpm"
                         "  -  hottest GPU memory 60 C, closest to its limit: NVMe 38 of 70 C")
        self.p["inflight"] = 1
        self.go(f, self.clock.t + 1)
        self.go(f, self.clock.t + 1)
        self.assertEqual(o1fan.panel_line(now=1_800_000_000),
                         "Fans: 100% (a request is running)  -  GPU fan 2550 rpm, case fans up to 2550 rpm"
                         "  -  hottest GPU memory 60 C, closest to its limit: NVMe 38 of 70 C")
        self.p["inflight"] = 0
        t = self.clock.t
        self.go(f, t + 30)
        self.assertIn("Fans: 100% for 30 s more, then 50%", o1fan.panel_line(now=1_800_000_000))
        self.go(f, t + 90)
        self.assertIn("Fans: 50% for 30 s more, then 20%", o1fan.panel_line(now=1_800_000_000))

    def test_the_status_command_needs_no_root_and_reads_the_tree(self):
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-fan"), "status"], capture_output=True,
                           text=True, env=dict(os.environ, OLLAMA1_SYS=self.tree.root, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("level: the service isn't running", r.stdout)
        self.assertIn("GPU fan", r.stdout)
        self.assertIn("temps: highest GPU memory 60 C; closest to its limit: NVMe 38 C of 70 (32 under)", r.stdout)

    def test_the_other_commands_refuse_without_root(self):
        if os.geteuid() == 0:
            self.skipTest("root")
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-fan"), "run"], capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "OLLAMA1_PREFIX"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("sudo", r.stderr)


class TestModulesAndSetup(FanCase):
    def test_the_module_is_loaded_when_no_chip_shows_and_never_fatally(self):
        shutil.rmtree(self.tree.nct)
        calls = []

        def appears(cmd, **kw):
            calls.append(cmd)
            self.tree.nct = self.tree.put("hwmon1", "nct6797", {"pwm1": 90, "pwm1_enable": 5})
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.assertTrue(o1fan.load_modules(run=appears, sysroot=self.tree.root, log=self.lines.append))
        self.assertEqual(calls, [["modprobe", "nct6775"]])
        calls.clear()
        self.assertTrue(o1fan.load_modules(run=appears, sysroot=self.tree.root, log=self.lines.append))
        self.assertEqual(calls, [])                           # a chip is there: no modprobe
        shutil.rmtree(self.tree.nct)

        def fails(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, "", "modprobe: ERROR: could not insert 'nct6775': No such device")

        self.assertFalse(o1fan.load_modules(run=fails, sysroot=self.tree.root, log=self.lines.append))
        self.assertTrue(any("could not insert" in l for l in self.lines))

        def missing(cmd, **kw):
            raise FileNotFoundError(cmd[0])
        self.assertFalse(o1fan.load_modules(run=missing, sysroot=self.tree.root, log=self.lines.append))

    def test_setup_on_enables_the_service_and_keeps_the_module_only_if_the_chip_loads(self):
        conf = o1fan.o1common.p(o1fan.MODULES_CONF)
        self.addCleanup(lambda: os.path.exists(conf) and os.unlink(conf))
        calls = []

        def systemctl(*a):
            calls.append(a)
            return True
        self.assertEqual(o1fan.setup("on", systemctl=systemctl, sysroot=self.tree.root, log=self.lines.append,
                                     modules=lambda **kw: True), 0)
        self.assertIn(("enable", "ollama1-fan.service"), calls)
        self.assertIn(("restart", "ollama1-fan.service"), calls)
        self.assertIn("nct6775", open(conf).read())
        self.assertEqual(o1fan.setup("on", systemctl=systemctl, sysroot=self.tree.root, log=self.lines.append,
                                     modules=lambda **kw: False), 0)
        self.assertFalse(os.path.exists(conf))                # no chip: no boot-time entry
        self.assertEqual(o1fan.setup("on", systemctl=lambda *a: False, sysroot=self.tree.root, log=self.lines.append,
                                     modules=lambda **kw: False), 1)

    def test_setup_off_stops_the_service_and_drops_the_module_entry(self):
        conf = o1fan.o1common.p(o1fan.MODULES_CONF)
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        open(conf, "w").write("nct6775\n")
        calls = []
        self.assertEqual(o1fan.setup("off", systemctl=lambda *a: calls.append(a) or True, sysroot=self.tree.root,
                                     log=self.lines.append), 0)
        self.assertEqual(calls, [("disable", "--now", "ollama1-fan.service")])
        self.assertFalse(os.path.exists(conf))

    def test_the_hand_made_full_speed_always_unit_is_replaced(self):
        unit = o1fan.o1common.p("/etc/systemd/system/" + o1fan.OLD_UNIT)
        os.makedirs(os.path.dirname(unit), exist_ok=True)
        open(unit, "w").write("[Service]\nExecStart=/bin/sh -c 'echo 1 > pwm1_enable'\n")
        self.addCleanup(lambda: os.path.exists(unit) and os.unlink(unit))
        self.tree.set(self.tree.gpu, "pwm1_enable", 1)        # the old unit left it manual
        self.tree.set(self.tree.gpu, "pwm1", 255)
        calls = []
        self.assertTrue(o1fan.release_old_unit(lambda *a: calls.append(a) or True, sysroot=self.tree.root,
                                               log=self.lines.append))
        self.assertEqual(calls, [("disable", "--now", o1fan.OLD_UNIT), ("daemon-reload",)])
        self.assertFalse(os.path.exists(unit))
        self.assertEqual(self.tree.get(self.tree.gpu, "pwm1_enable"), 2)       # given back to the driver
        self.assertFalse(o1fan.release_old_unit(lambda *a: True, sysroot=self.tree.root, log=self.lines.append))


class TestWiring(unittest.TestCase):
    def read(self, *p):
        return open(os.path.join(U.KIT, *p)).read()

    def test_the_unit(self):
        u = self.read("systemd", "ollama1-fan.service")
        self.assertIn("\nExecStartPre=-+/usr/local/lib/ollama1/bin/ollama1-fan load-modules\n", u)   # outside the sandbox
        self.assertIn("\nExecStart=/usr/local/lib/ollama1/bin/ollama1-fan run\n", u)
        self.assertIn("\nStartLimitIntervalSec=0\n", u)                      # never gives up: a dead service means stuck fans
        for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/run/ollama1 /var/lib/ollama1",
                     "ProtectHome=yes", "PrivateTmp=yes", "PrivateNetwork=yes", "ProtectKernelModules=yes",
                     "ProtectKernelLogs=yes", "ProtectControlGroups=yes", "RestrictAddressFamilies=AF_UNIX",
                     "LimitCORE=0", "MemoryMax=64M", "RestrictNamespaces=yes", "SystemCallArchitectures=native"):
            self.assertIn("\n" + line + "\n", u, line)
        self.assertNotIn("\nProtectKernelTunables", u)                        # it writes /sys
        self.assertNotIn("IPAddressAllow", u)
        self.assertIn("\nWantedBy=multi-user.target\n", u)

    def test_setup_wiring(self):
        s = self.read("setup.sh")
        self.assertIn('ln -sfn "$LIBDIR/bin/ollama1-fan" /usr/local/bin/ollama1-fan', s)
        self.assertIn("printf 'FANS=%s\\n' \"$FANS\"", s)
        self.assertIn('FANS=$(fans_choice "$A_FANS" "${OLLAMA1_FANS:-}" "$(saved FANS)")', s)
        a = s.index('step "Fans"')
        self.assertLess(s.index('step "Graphics card tuning"'), a)
        self.assertLess(a, s.index('step "Cloudflare Tunnel and Access"'))
        self.assertIn('"$LIBDIR/bin/ollama1-fan" setup "$FANS"', s[a:s.index('step "Cloudflare Tunnel and Access"')])

    def bash(self, script):
        r = subprocess.run(["bash", "-c", ". %s; %s" % (shlex.quote(os.path.join(U.LIB, "setuplib.sh")), script)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def test_the_choice_is_on_unless_turned_off_and_a_saved_off_is_kept(self):
        for args, want in ((['"" "" ""'], "on"), (['on "" ""'], "on"), (['off "" ""'], "off"), (['"" 0 ""'], "off"),
                           (['"" 1 off'], "on"), (['"" "" off'], "off"), (['"" "" on'], "on"), (['off 1 on'], "off")):
            self.assertEqual(self.bash("fans_choice " + " ".join(args)), (0, want), args)
        self.assertEqual(self.bash('fans_choice "" "" maybe')[0], 1)
        self.assertEqual(self.bash('fans_choice maybe "" ""')[0], 1)

    def test_the_plan_line_names_the_levels(self):
        on = self.bash("fans_plan on")[1]
        self.assertIn("Fans ON", on)
        self.assertIn("100%", on)
        self.assertIn("50%", on)
        self.assertIn("20%", on)
        self.assertIn("Fans OFF", self.bash("fans_plan off")[1])

    def test_the_flag_is_in_the_help_and_a_bad_value_is_refused(self):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--help"], capture_output=True, text=True)
        self.assertIn("--fans on|off", r.stdout)
        self.assertIn("then 20%", r.stdout)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--fans", "maybe", "--plan"], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--fans takes on or off", r.stdout)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--fans"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_the_panel_shows_the_line(self):
        a = self.read("bin", "ollama1-admin")
        self.assertIn('st["fan"] = o1fan.panel_line()', a)
        self.assertIn('id="fanline"', a)
        self.assertIn("fl.textContent=s.fan||''", a)


if __name__ == "__main__":
    unittest.main()
