"""The liquid-cooler backend of ollama1-fan (6b388): a fake `liquidctl` program that
records its calls and prints JSON, the policy per phase, the pump at extreme only
when it is needed, the coolant override and its hysteresis, rate limiting and
de-duplication, failures, the safe curve on the way out, no liquidctl, and the
setup wiring."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1aio
import o1fan
from test_fan import FanCase, T0

DEVICE = "Corsair H115i Platinum"
FAKE = '''#!%s
import os, sys
d = os.environ["FAKE_LC_DIR"]
args = sys.argv[1:]
open(d + "/calls", "a").write(" ".join(args) + "\\n")
if os.path.exists(d + "/fail") and "list" not in args:
    sys.stderr.write("device not responding")
    sys.exit(1)
if "list" in args:
    sys.stdout.write(open(d + "/list.json").read())
elif "status" in args:
    sys.stdout.write(open(d + "/status.json").read())
'''


class AioCase(FanCase):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp(prefix="o1aio-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.exe = os.path.join(self.d, "liquidctl")
        open(self.exe, "w").write(FAKE % sys.executable)
        os.chmod(self.exe, os.stat(self.exe).st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, FAKE_LC_DIR=self.d)
        self.list([{"description": DEVICE, "vendor_id": 0x1b1c, "product_id": 0x0c17}])
        self.now = T0
        self.times = []                       # (clock, args) of every call
        self.coolant = 31.0
        self.fan_rpm = {}                     # fan -> function(pct) -> rpm
        self.pump_rpm = 2400
        self.marker = o1aio.state_path()
        if os.path.exists(self.marker):
            os.unlink(self.marker)
        self.addCleanup(lambda: os.path.exists(self.marker) and os.unlink(self.marker))
        self.persisted = []
        self.aio = self.make()

    def run_fake(self, cmd, **kw):
        self.times.append((self.now, cmd[1:]))
        return subprocess.run(cmd, env=self.env, **kw)

    def make(self, **kw):
        a = o1aio.Aio(binary=kw.pop("binary", self.exe), log=self.lines.append, run=self.run_fake, **kw)
        a.attach({}, lambda: self.persisted.append(1), self.lines.append)
        return a

    def list(self, devs):
        open(os.path.join(self.d, "list.json"), "w").write(json.dumps(devs))

    def calls(self):
        p = os.path.join(self.d, "calls")
        return open(p).read().splitlines() if os.path.exists(p) else []

    def sets(self):
        return [c for c in self.calls() if " set " in " " + c]

    def last_pct(self, n):
        for _t, args in reversed(self.times):
            if args[-3:-1] == ["fan%d" % n, "speed"] or (len(args) >= 4 and args[-3] == "fan%d" % n and args[-2] == "speed"):
                return int(args[-1])
            if "set" in args and "fan%d" % n in args and "speed" in args and len(args) == args.index("speed") + 2:
                return int(args[-1])
        return None

    def spin(self):
        fans = []
        for n in (1, 2):
            pct = self.last_pct(n)
            fn = self.fan_rpm.get(n, lambda p: p * 10)
            fans.append({"key": "Fan %d speed" % n, "value": fn(pct) if pct is not None else 800, "unit": "rpm"})
        rows = [{"key": "Liquid temperature", "value": self.coolant, "unit": "°C"}] + fans + \
               [{"key": "Pump speed", "value": self.pump_rpm, "unit": "rpm"}]
        open(os.path.join(self.d, "status.json"), "w").write(json.dumps([{"description": DEVICE, "status": rows}]))

    def ago(self, t, phase=None, pct=None):
        self.now = t
        if phase:
            self.aio.set_phase(phase, pct)
        self.spin()
        self.aio.step(t)

    def go(self, fan, t):
        """The fan service's own tick (FanCase.run_for): the cooler's fans spin first, on the same clock."""
        self.now = t
        self.spin()
        return super().go(fan, t)

    def settle(self, phase="idle20", pct=20):
        """The first start: measured at 100% (calibrating), then the phase."""
        self.ago(T0, "calibrating", 100)
        self.ago(T0 + 7)
        self.ago(T0 + 13)
        self.ago(T0 + 14, phase, pct)
        self.ago(T0 + 20)
        return T0 + 20


class TestPolicy(AioCase):
    def test_the_first_start_measures_the_fans_at_100_with_the_pump_balanced(self):
        self.ago(T0, "calibrating", 100)
        self.assertEqual(self.aio.applied, {1: 100, 2: 100})
        self.assertEqual(self.aio.applied_pump, "balanced")
        self.assertTrue(self.aio.needs_calibration())
        self.ago(T0 + 7)
        self.ago(T0 + 13)
        self.assertFalse(self.aio.needs_calibration())
        self.assertEqual(self.aio.learned["aio/fan1"]["rpm100"], 1000)
        self.assertTrue(self.persisted)
        c = self.calls()
        self.assertEqual(c[0], "list --json")
        self.assertIn("--match %s initialize" % DEVICE, c)

    def test_each_phase_maps_to_fans_and_pump(self):
        t = self.settle()
        for phase, pct, fans, pump in (("working", 100, 100, "extreme"), ("hot", 100, 100, "extreme"),
                                       ("ramp", 50, 50, "balanced"),
                                       ("idle20", 20, 20, "quiet"), ("calibrating", 100, 100, "balanced")):
            with self.subTest(phase):
                t += 6
                self.ago(t, phase, pct)
                self.assertEqual(self.aio.applied, {1: fans, 2: fans}, phase)
                self.assertEqual(self.aio.applied_pump, pump, phase)

    def test_the_pump_is_extreme_only_while_working_or_hot_and_the_ramp_starts_balanced(self):
        t = self.settle()
        for phase, pct in (("ramp", 100), ("ramp", 50), ("idle20", 20), ("calibrating", 100)):
            t += 6
            self.ago(t, phase, pct)
            self.assertNotEqual(self.aio.applied_pump, "extreme", phase)
        for phase in ("working", "hot"):
            t += 6
            self.ago(t, phase, 100)
            self.assertEqual(self.aio.applied_pump, "extreme", phase)

    def test_hot_coolant_forces_the_pump_and_the_fans_until_5_under(self):
        t = self.settle()
        self.coolant = 39.9
        t += 6
        self.ago(t)
        self.assertEqual((self.aio.applied_pump, self.aio.applied), ("quiet", {1: 20, 2: 20}))
        for c, hot in ((40.0, True), (36.0, True), (35.0, True), (34.9, False)):
            self.coolant = c
            t += 6
            self.ago(t)
            self.assertEqual(self.aio.coolant_hot, hot, c)
            self.assertEqual(self.aio.applied_pump, "extreme" if hot else "quiet", c)
            self.assertEqual(self.aio.applied, {1: 100, 2: 100} if hot else {1: 20, 2: 20}, c)
        self.assertTrue(any("coolant 40 C" in l for l in self.lines))

    def test_an_implausible_coolant_reading_is_not_trusted(self):
        t = self.settle()
        for c in (0, -128, 120, 255):
            self.coolant = c
            t += 6
            self.ago(t)
            self.assertFalse(self.aio.coolant_hot, c)
        self.coolant = 45
        t += 6
        self.ago(t)
        self.assertTrue(self.aio.coolant_hot)
        self.coolant = 255                                  # stuck: the state is kept, not released by nonsense
        t += 6
        self.ago(t)
        self.assertTrue(self.aio.coolant_hot)


class TestLearning(AioCase):
    def test_a_stalled_cooler_fan_is_raised_in_steps_and_remembered(self):
        self.fan_rpm[2] = lambda p: 0 if p < 40 else p * 10
        t = self.settle()
        for _ in range(8):
            t += 7
            self.ago(t)
        self.assertEqual(self.aio.learned["aio/fan2"]["min_pct"], 40)
        self.assertEqual(self.aio.applied, {1: 20, 2: 40})
        self.assertEqual(self.aio.learned["aio/fan1"]["min_pct"], 20)
        self.assertEqual(len([l for l in self.lines if "cooler fan 2" in l and "stalled" in l]), 2)

    def test_a_fan_with_no_rpm_at_100_stays_at_100(self):
        self.fan_rpm[1] = lambda p: 0
        t = self.settle()
        self.assertEqual(self.aio.applied, {1: 100, 2: 20})


class TestRateLimit(AioCase):
    def test_status_is_read_at_most_every_5_seconds(self):
        self.settle()
        for s in range(1, 61):
            self.ago(T0 + 20 + s)
        stamps = [t for t, a in self.times if a[-2:] == ["status", "--json"]]
        self.assertTrue(all(b - a >= 5 for a, b in zip(stamps, stamps[1:])), stamps)
        self.assertLessEqual(len([t for t in stamps if t > T0 + 20]), 13)

    def test_an_unchanged_target_sends_nothing(self):
        t = self.settle()
        before = len(self.sets())
        for s in range(1, 40):
            self.ago(t + s)
        self.assertEqual(len(self.sets()), before)

    def test_a_changing_target_is_sent_no_more_often_than_every_5_seconds(self):
        t = self.settle()
        self.times.clear()
        for s in range(1, 41):                              # flapping every second
            self.ago(t + s, *(("working", 100) if s % 2 else ("idle20", 20)))
        rounds = sorted({when for when, a in self.times if "set" in a})
        self.assertTrue(rounds)
        self.assertTrue(all(b - a >= 5 for a, b in zip(rounds, rounds[1:])), rounds)

    def test_during_the_ramp_the_cooler_fans_follow_the_same_percent_at_most_every_5_s(self):
        t = self.settle("working", 100)
        self.times.clear()
        pcts = []
        C = o1fan.RAMP_S
        for s in range(0, 3 * C // 2 + 1):                   # the fan service's phase every second, the ramp's % in 2% steps
            p = o1fan.ramp_pct(s) if s <= C else 20
            self.ago(t + s, "ramp" if s < C else "idle20", p)
            pcts.append(self.aio.applied[1])
        sets = [(when, a) for when, a in self.times if "set" in a]
        rounds = sorted({when for when, a in sets})
        self.assertTrue(all(b - a >= 5 for a, b in zip(rounds, rounds[1:])), rounds)
        fan1 = [int(a[-1]) for _w, a in sets if a[-3:-1] == ["fan1", "speed"]]
        self.assertTrue(all(b < a for a, b in zip(fan1, fan1[1:])))              # only on change, only down
        self.assertTrue(all(v % 2 == 0 for v in fan1))
        self.assertEqual(fan1[-1], 20)
        self.assertLessEqual(len(fan1), 13)                                       # about every 5 s, not every 2%
        pumps = [a[-1] for _w, a in sets if a[-3:-1] == ["pump", "mode"]]
        self.assertEqual(pumps, ["balanced", "quiet"])                           # balanced in the ramp, quiet at idle
        self.assertEqual(pcts[0], 100)

    def test_a_cooler_fan_floor_holds_in_the_ramp(self):
        self.fan_rpm[2] = lambda p: 0 if p < 40 else p * 10
        t = self.settle("working", 100)
        for s in range(0, o1fan.RAMP_S + 20, 1):
            self.ago(t + 10 + s, "ramp", o1fan.ramp_pct(s))
        self.assertEqual(self.aio.applied[2], 40)
        self.assertEqual(self.aio.applied[1], 20)

    def test_only_what_changed_is_sent(self):
        t = self.settle()
        self.times.clear()
        self.ago(t + 6, "ramp", 50)                        # fans 50, pump quiet -> balanced
        words = [" ".join(a) for _t, a in self.times if "set" in a]
        self.assertEqual(sorted(words), sorted(["--match %s set fan1 speed 50" % DEVICE,
                                                "--match %s set fan2 speed 50" % DEVICE,
                                                "--match %s set pump mode balanced" % DEVICE]))
        self.times.clear()
        self.ago(t + 12, "ramp", 50)
        self.assertEqual([a for _t, a in self.times if "set" in a], [])
        self.ago(t + 18, "idle20", 20)                        # a quiet pump again: only the fans' 20 and the pump
        self.assertIn("set pump mode quiet", " ".join(" ".join(a) for _t, a in self.times).replace("--match %s " % DEVICE, ""))

    def test_a_single_changed_part_is_the_only_one_sent(self):
        t = self.settle("calibrating", 100)                 # fans 100, pump balanced
        self.times.clear()
        self.ago(t + 6, "working", 100)                     # the pump only
        self.assertEqual([" ".join(a[2:]) for _t, a in self.times if "set" in a], ["set pump mode extreme"])

    def test_a_raised_fan_is_sent_alone(self):
        self.fan_rpm[2] = lambda p: 0 if p < 40 else p * 10
        t = self.settle()
        mark = len(self.times)                              # (the fake fans read what was last sent from the history)
        for s in range(1, 30):                              # fan 2 stalled at 20% and is raised: only its own command
            self.ago(t + 7 * s)
        sent = [" ".join(a[2:]) for _t, a in self.times[mark:] if "set" in a]
        self.assertTrue(sent)
        self.assertTrue(all(c.startswith("set fan2 speed") for c in sent), sent)

    def test_a_wake_sends_everything_again(self):
        t = self.settle()
        n = len([c for c in self.calls() if c.endswith("initialize")])
        self.aio.reset()
        self.times.clear()
        self.ago(t + 6)
        self.assertEqual(len([c for c in self.calls() if c.endswith("initialize")]), n + 1)
        self.assertEqual(len([1 for _t, a in self.times if "set" in a]), 3)


class TestFailures(AioCase):
    def test_one_failure_then_a_success_starts_the_count_again(self):
        t = self.settle()
        open(os.path.join(self.d, "fail"), "w").close()
        for _ in range(4):
            t += 6
            self.ago(t)
        self.assertEqual(self.aio.fails, 4)
        os.unlink(os.path.join(self.d, "fail"))
        t += 6
        self.ago(t)
        self.assertEqual(self.aio.fails, 0)
        self.assertFalse(self.aio.disabled)

    def test_five_in_a_row_leaves_the_cooler_on_its_safe_curve_and_stops_trying(self):
        t = self.settle()
        self.assertTrue(os.path.exists(self.marker))
        open(os.path.join(self.d, "fail"), "w").close()
        for _ in range(5):
            t += 6
            self.ago(t)
        self.assertTrue(self.aio.disabled)
        self.assertTrue(any("failed 5 times in a row" in l for l in self.lines))
        self.assertIn("keeps failing", self.aio.snapshot()["state"])
        n = len(self.calls())
        for _ in range(5):
            t += 6
            self.ago(t)
        self.assertEqual(len(self.calls()), n)                 # no more calls

    def test_the_case_fans_carry_on_when_the_cooler_fails(self):
        open(os.path.join(self.d, "fail"), "w").close()
        f = self.fan(aio=self.aio, aio_inline=True)
        self.p["gpu_busy"] = 100
        self.run_for(f, 60, step=2)
        self.assertTrue(self.aio.disabled)
        self.assertEqual(f.phase, "working")
        self.assertTrue(self.tree.at(255))

    def test_an_unreadable_status_counts_as_a_failure(self):
        t = self.settle()
        open(os.path.join(self.d, "status.json"), "w").write("not json")
        t += 6
        self.now = t
        self.aio.step(t)
        self.assertEqual(self.aio.fails, 1)

    def test_a_call_that_never_returns_is_a_failure_not_a_hang(self):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        a = o1aio.Aio(binary=self.exe, log=self.lines.append, run=hang)
        a.match = DEVICE
        a.exe = self.exe
        self.assertEqual(a.call(["status", "--json"]), (False, ""))
        self.assertEqual(a.fails, 1)

    def test_every_call_has_a_timeout_under_the_watchdog(self):
        seen = []

        def spy(cmd, **kw):
            seen.append(kw.get("timeout"))
            return subprocess.run(cmd, env=self.env, **kw)
        self.run_fake = spy
        a = self.make()
        a.set_phase("calibrating", 100)
        a.step(T0)
        a.safe_exit()
        self.assertTrue(seen)
        self.assertTrue(all(t is not None and t < 10 for t in seen), seen)


class TestExit(AioCase):
    CURVE = "25 30 35 60 45 100"

    def test_the_safe_curve_and_a_balanced_pump(self):
        t = self.settle()
        self.times.clear()
        self.assertTrue(self.aio.safe_exit())
        words = sorted(" ".join(a).replace("--match %s " % DEVICE, "") for _t, a in self.times if "set" in a)
        self.assertEqual(words, sorted(["set fan1 speed " + self.CURVE, "set fan2 speed " + self.CURVE,
                                        "set pump mode balanced"]))
        self.assertFalse(os.path.exists(self.marker))

    def test_a_clean_stop_does_it_through_the_fan_service(self):
        f = self.fan(aio=self.aio, aio_inline=True)
        self.run_for(f, 60, step=2)
        self.assertTrue(os.path.exists(self.marker))
        self.times.clear()
        f.shutdown()
        self.assertTrue(any("set pump mode balanced" in " ".join(a) for _t, a in self.times))
        self.assertTrue(self.tree.as_before())

    def test_after_a_crash_stop_post_does_it_from_the_marker(self):
        self.settle()
        self.assertTrue(os.path.exists(self.marker))             # the service dies here
        self.times.clear()
        self.assertTrue(o1aio.safe_exit_if_controlled(log=self.lines.append, run=self.run_fake, which=lambda n: self.exe))
        words = sorted(" ".join(a).replace("--match %s " % DEVICE, "") for _t, a in self.times if "set" in a)
        self.assertEqual(words, sorted(["set fan1 speed " + self.CURVE, "set fan2 speed " + self.CURVE,
                                        "set pump mode balanced"]))
        self.assertFalse(os.path.exists(self.marker))
        self.times.clear()
        self.assertTrue(o1aio.safe_exit_if_controlled(log=self.lines.append, run=self.run_fake, which=lambda n: self.exe))
        self.assertEqual(self.times, [])                          # nothing under our control: nothing to do

    def test_the_fan_service_stop_post_covers_the_cooler(self):
        self.settle()
        calls = []
        real = o1aio.safe_exit_if_controlled
        o1aio.safe_exit_if_controlled = lambda **kw: calls.append(1) or True
        try:
            self.up()
            o1fan.restore_after_stop({}, sysroot=self.tree.root, boot_id=lambda: self.boot, log=self.lines.append)
        finally:
            o1aio.safe_exit_if_controlled = real
        self.assertEqual(calls, [1])


class TestAbsent(AioCase):
    def test_no_liquidctl_is_one_log_line_and_no_calls(self):
        a = o1aio.Aio(log=self.lines.append, run=self.run_fake, which=lambda n: None)
        a.attach({}, lambda: None, self.lines.append)
        for s in range(0, 50, 5):
            a.step(T0 + s)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len([l for l in self.lines if "liquidctl is not installed" in l]), 1)
        self.assertEqual(a.snapshot()["state"], "liquidctl is not installed")
        self.assertFalse(a.needs_calibration())

    def test_it_is_found_when_it_is_installed_later(self):
        have = []
        a = o1aio.Aio(log=self.lines.append, run=self.run_fake, which=lambda n: have[0] if have else None)
        a.attach({}, lambda: None, self.lines.append)
        a.step(T0)
        have.append(self.exe)
        a.step(T0 + 100)                                          # not yet: it looks again every 5 minutes
        self.assertIsNone(a.match)
        self.now = T0 + 301
        self.spin()
        a.step(T0 + 301)
        self.assertEqual(a.match, DEVICE)

    def test_found_on_the_path(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = self.d + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        a = o1aio.Aio(log=self.lines.append, run=self.run_fake)
        a.step(T0)
        self.assertEqual(a.match, DEVICE)

    def test_no_cooler_is_said_once_and_looked_for_again_only_every_5_minutes(self):
        self.list([{"description": "Some other device", "vendor_id": 1}])
        a = self.make()
        for s in range(0, 60, 5):
            a.step(T0 + s)
        self.assertEqual(len([c for c in self.calls() if c == "list --json"]), 1)
        self.assertEqual(len([l for l in self.lines if "no Corsair Hydro liquid cooler found" in l]), 1)
        self.assertFalse(a.active())

    def test_a_list_that_fails_is_not_fatal(self):
        open(os.path.join(self.d, "list.json"), "w").write("garbage")
        a = self.make()
        a.step(T0)
        self.assertIsNone(a.match)
        self.assertIn("liquidctl list failed", a.snapshot()["state"])

    def test_the_fan_service_without_a_cooler_is_what_it_was(self):
        f = self.fan()
        self.assertIsNone(f.aio)
        self.run_for(f, 30)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("cooler", o1fan.render_status(o1fan.read_status(now=1_800_000_000),
                                                       o1fan.snapshot([], sysroot=self.tree.root)))


class TestStatus(AioCase):
    def test_the_status_and_the_admin_line_show_coolant_pump_and_fans(self):
        f = self.fan(aio=self.aio, aio_inline=True)
        self.run_for(f, 60, step=2)
        self.run_for(f, 60, step=2)
        st = o1fan.read_status(now=1_800_000_000)
        a = st["aio"]
        self.assertEqual((a["name"], a["state"], a["coolant_c"], a["pump_mode"]), (DEVICE, "controlling", 31.0, "quiet"))
        self.assertEqual([x["n"] for x in a["fans"]], [1, 2])
        self.assertIn("cooler 31 C, pump quiet 2400 rpm, fans 200/200 rpm", o1fan.panel_line(now=1_800_000_000))
        text = o1fan.render_status(st, o1fan.snapshot([], sysroot=self.tree.root))
        self.assertIn("cooler: %s - controlling" % DEVICE, text)
        self.assertIn("coolant 31.0 C   pump quiet 2400 rpm", text)
        self.assertRegex(text, r"cooler fan 1  20%  200 rpm  min 20%")

    def test_a_hot_coolant_is_called_out(self):
        self.coolant = 42
        f = self.fan(aio=self.aio, aio_inline=True)
        self.run_for(f, 60, step=2)
        text = o1fan.render_status(o1fan.read_status(now=1_800_000_000), o1fan.snapshot([], sysroot=self.tree.root))
        self.assertIn("(>= 40 C: pump extreme, fans 100%)", text)
        self.assertEqual(self.aio.applied_pump, "extreme")

    def test_the_cooler_is_measured_before_the_fans_settle_at_20(self):
        f = self.fan(aio=self.aio, aio_inline=True)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "calibrating")       # the cooler's fans measured too
        self.run_for(f, 60, step=2)
        self.assertEqual(f.phase, "idle20")
        self.assertIn("aio/fan1", f.learned)

    def test_a_wake_resets_the_cooler(self):
        f = self.fan(aio=self.aio, aio_inline=True)
        self.run_for(f, 60, step=2)
        n = len([c for c in self.calls() if c.endswith("initialize")])
        f.wake()
        self.run_for(f, 12, step=2)
        self.assertEqual(len([c for c in self.calls() if c.endswith("initialize")]), n + 1)


class TestParse(unittest.TestCase):
    def test_the_status_is_read_loosely(self):
        st = o1aio.parse_status(json.dumps([{"status": [
            {"key": "Liquid temperature", "value": 29.5, "unit": "°C"}, {"key": "Fan 1 speed", "value": 640, "unit": "rpm"},
            {"key": "Fan 2 speed", "value": 650}, {"key": "Pump speed", "value": 2750}, {"key": "Pump mode", "value": "Balanced"},
            {"key": "Firmware version", "value": "1.2.3"}, {"nokey": 1}, "junk"]}]))
        self.assertEqual(st, {"coolant_c": 29.5, "fans": {1: 640, 2: 650}, "pump_rpm": 2750, "pump_mode": "balanced"})
        self.assertEqual(o1aio.parse_status("[]")["fans"], {})
        self.assertEqual(o1aio.parse_status('[{"status": null}]')["coolant_c"], None)
        with self.assertRaises(ValueError):
            o1aio.parse_status("nope")

    def test_aio_text(self):
        self.assertEqual(o1aio.aio_text(None), "")
        self.assertEqual(o1aio.aio_text({"found": True, "state": "liquidctl keeps failing"}), "")
        self.assertEqual(o1aio.aio_text({"found": True, "state": "controlling", "coolant_c": 31.2, "pump_mode": "quiet",
                                         "pump_rpm": 2400, "fans": [{"rpm": 520}, {"rpm": 530}]}),
                         "cooler 31 C, pump quiet 2400 rpm, fans 520/530 rpm")


class TestSetup(FanCase):
    def usb(self, vendor, product):
        d = "%s/bus/usb/devices/1-%s" % (self.tree.root, product)
        os.makedirs(d, exist_ok=True)
        open(d + "/idVendor", "w").write(vendor + "\n")
        open(d + "/idProduct", "w").write(product + "\n")

    def test_the_usb_ids(self):
        self.assertFalse(o1aio.usb_present(self.tree.root))
        self.usb("1b1c", "1b2d")                       # a Corsair keyboard
        self.usb("1234", "0c17")                       # not Corsair
        self.assertFalse(o1aio.usb_present(self.tree.root))
        self.usb("1b1c", "0c17")                       # the H115i Platinum
        self.assertTrue(o1aio.usb_present(self.tree.root))

    def test_every_hydro_platinum_pro_id_listed_counts(self):
        for pid in ("0c15", "0c17", "0c18", "0c19", "0c29", "0c2a", "0c2b"):
            with self.subTest(pid):
                shutil.rmtree(self.tree.root + "/bus", True)
                self.usb("1b1c", pid)
                self.assertTrue(o1aio.usb_present(self.tree.root))

    def test_liquidctl_is_installed_only_with_a_cooler_and_only_if_missing(self):
        calls = []
        self.assertFalse(o1fan.aio_setup(self.tree.root, which=lambda n: None, installer=lambda: calls.append(1) or True,
                                         log=self.lines.append))
        self.assertEqual(calls, [])                    # no cooler: nothing installed
        self.usb("1b1c", "0c17")
        self.assertTrue(o1fan.aio_setup(self.tree.root, which=lambda n: "/usr/bin/liquidctl",
                                        installer=lambda: calls.append(1) or True, log=self.lines.append))
        self.assertEqual(calls, [])                    # already there
        self.assertTrue(o1fan.aio_setup(self.tree.root, which=lambda n: None, installer=lambda: calls.append(1) or True,
                                        log=self.lines.append))
        self.assertEqual(calls, [1])
        self.assertFalse(o1fan.aio_setup(self.tree.root, which=lambda n: None, installer=lambda: False,
                                         log=self.lines.append))
        self.assertTrue(any("could not be installed" in l for l in self.lines))

    def test_setup_on_runs_the_cooler_step_and_off_does_not(self):
        conf = o1fan.o1common.p(o1fan.MODULES_CONF)
        self.addCleanup(lambda: os.path.exists(conf) and os.unlink(conf))
        ran = []
        o1fan.setup("on", systemctl=lambda *a: True, sysroot=self.tree.root, log=self.lines.append,
                    modules=lambda **kw: False, aio=lambda **kw: ran.append(1))
        self.assertEqual(ran, [1])
        o1fan.setup("off", systemctl=lambda *a: True, sysroot=self.tree.root, log=self.lines.append,
                    aio=lambda **kw: ran.append(1))
        self.assertEqual(ran, [1])

    def test_the_plan_line_mentions_the_cooler(self):
        r = subprocess.run(["bash", "-c", ". %s; fans_plan on" % __import__("shlex").quote(os.path.join(U.LIB, "setuplib.sh"))],
                           capture_output=True, text=True)
        self.assertIn("Corsair Hydro liquid cooler", r.stdout)
        self.assertIn("liquidctl", r.stdout)

    def test_the_unit_has_what_liquidctl_needs(self):
        u = open(os.path.join(U.KIT, "systemd", "ollama1-fan.service")).read()
        for line in ("MemoryMax=192M", "RestrictAddressFamilies=AF_UNIX AF_NETLINK", "RuntimeDirectory=liquidctl",
                     "RuntimeDirectoryPreserve=yes", "WatchdogSec=10"):
            self.assertIn("\n" + line + "\n", u, line)
        self.assertNotIn("PrivateDevices", u)           # the USB device nodes stay


if __name__ == "__main__":
    unittest.main()
