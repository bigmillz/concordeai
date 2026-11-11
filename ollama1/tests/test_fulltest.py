#!/usr/bin/env python3
"""The full-power test (6b661): the length rule and the 5-minute cap, the night, every refusal, the run on a fake
clock with fake loads (the fans' hold while it runs and gone after, every safety stop: Tctl, the card, the coolant,
an NVMe or PCIe error, a request, the fan service, Stop), the result card's verdict, the kernel-log and nvidia-smi
readers, stop-post, and the unit files. No real hardware, no real load."""
import os
import subprocess
import threading
import time
import unittest

import o1test_util as U
import o1fan
import o1fulltest as F
import o1sleep
from o1common import read_json_safe, write_json_atomic

DAY = time.struct_time((2026, 11, 11, 14, 0, 0, 2, 315, 0))      # 14:00, for the runs (not night)


def at(h, m):
    return lambda _t: time.struct_time((2026, 11, 11, h, m, 0, 2, 315, 0))


class FakeLoad:
    def __init__(self, lines=("stress-ng: info: passed: 32: cpu (32)", "stress-ng: info: failed: 0")):
        self.rc, self.stopped, self.lines = None, False, list(lines)

    def poll(self):
        return self.rc

    def tail(self, n=20):
        return self.lines[-n:]

    def stop(self):
        self.stopped = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def wait(self, s):
        self.t += s


def reading(gpu_c=60, cpu_c=70, coolant=30.0, busy=99, reasons=("power limit",), bad=()):
    return {"gpu": {"busy": busy, "mhz": 2700, "max_mhz": 3100, "watts": 290, "limit_w": 300, "c": gpu_c,
                    "reasons": list(reasons), "bad": list(bad)},
            "cpu": {"busy": 100, "mhz": 4300, "mhz_min": 4100, "mhz_max": 4500, "c": cpu_c},
            "fan": {"phase": "tuning", "pct": 100, "fans": [{"label": "fan 2", "rpm": 1500}], "pump_rpm": 2800,
                    "coolant_c": coolant}}


class RunnerCase(unittest.TestCase):
    def setUp(self):
        for f in (F.progress_path(), F.last_path(), F.abort_path(), o1fan.tuning_path()):
            if os.path.exists(f):
                os.unlink(f)
        self.clock = Clock()
        self.loads = (FakeLoad(), FakeLoad(["ollama1 gpu burn"]))
        self.held = []

    def runner(self, seconds=60, script=None, gateway=(0, 0, 999), errors=None, refuse=None):
        script = script or (lambda t: reading())
        r = F.Runner(seconds, clock=self.clock, wall=lambda: 1_800_000_000 + self.clock.t, log=lambda *_: None,
                     gpu=lambda: script(self.clock.t)["gpu"], cpu=lambda: script(self.clock.t)["cpu"],
                     fans=lambda: script(self.clock.t)["fan"], gateway=lambda now: gateway,
                     errors=errors or (lambda since: []), loads=lambda: self.loads,
                     refuse=refuse if refuse is not None else (lambda: None), localtime=lambda _t: DAY)
        orig = r.hold

        def hold():
            orig()
            self.held.append(read_json_safe(o1fan.tuning_path(), None))
        r.hold = hold

        def wait(s):
            self.assertTrue(os.path.exists(o1fan.tuning_path()))        # the fans are held while it runs
            self.clock.wait(s)
        r.wait = wait
        return r


class TestLengthAndNight(unittest.TestCase):
    def test_lengths(self):
        for ok in (60, 120, 180, 240, 300):
            self.assertEqual(F.length_of(str(ok)), ok)
        for bad in ("301", "600", "30", "0", "", None, "60;id", " 60", "6O", "1200", "-60"):
            self.assertIsNone(F.length_of(bad), bad)

    def test_never_over_five_minutes(self):
        self.assertEqual(F.Runner(900).seconds, 300)
        self.assertEqual(F.MAX_S, 300)
        self.assertEqual(max(F.LENGTHS), 300)

    def test_night(self):
        for (h, m), night in (((22, 59), False), ((23, 0), True), ((3, 0), True), ((7, 59), True), ((8, 0), False),
                              ((14, 0), False)):
            self.assertEqual(F.is_night(0, at(h, m)), night, (h, m))


class TestRefusal(unittest.TestCase):
    def why(self, **kw):
        base = dict(now=1_800_000_000, localtime=at(14, 0),
                    activity=lambda now: {"last": now - 600, "inflight": 0, "fresh": True},
                    snapshot=lambda: {"queued": 0, "stale": False}, busy=lambda: [], tools=lambda: [],
                    fans=lambda: {"phase": "idle20"}, progress=lambda now: None)
        base.update(kw)
        return F.refusal(base.pop("seconds", 60), **base)

    def test_quiet_day_starts(self):
        self.assertIsNone(self.why())

    def test_each_refusal(self):
        cases = (
            (dict(seconds=None), "1 to 5 minutes"),
            (dict(localtime=at(23, 30)), "not at night"),
            (dict(localtime=at(6, 0)), "not at night"),
            (dict(progress=lambda now: {"state": "running"}), "already running"),
            (dict(activity=lambda now: {"last": 0, "inflight": None, "fresh": False}), "couldn't tell"),
            (dict(activity=lambda now: {"last": now, "inflight": 1, "fresh": True}), "answering a request"),
            (dict(snapshot=lambda: {"queued": 2, "stale": False}), "answering a request"),
            (dict(activity=lambda now: {"last": now - 30, "inflight": 0, "fresh": True}), "30 s ago"),
            (dict(busy=lambda: ["the fans are being calibrated"]), "calibrated"),
            (dict(tools=lambda: ["quick-burn.sh"]), "quick-burn.sh is running"),
            (dict(fans=lambda: None), "fan service isn't running"),
            (dict(fans=lambda: {"phase": "hot"}), "at its limit"),
        )
        for kw, want in cases:
            with self.subTest(want):
                self.assertIn(want, self.why(**kw) or "")

    def test_its_own_unit_is_not_a_reason(self):
        mine = dict(o1sleep.BUSY_UNITS)[F.UNIT_GLOB]
        self.assertIsNone(self.why(busy=lambda: [mine]))


class TestRun(RunnerCase):
    def test_a_clean_run_passes_and_gives_the_fans_back(self):
        r = self.runner(60)
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state, "done")
        self.assertTrue(all(h and h["pct"] == 100 and h["by"] == "fulltest" for h in self.held))
        self.assertFalse(os.path.exists(o1fan.tuning_path()))           # the normal curve again
        self.assertTrue(self.loads[0].stopped and self.loads[1].stopped)
        last = read_json_safe(F.last_path(), None)
        res = last["result"]
        self.assertEqual(res["verdict"], "pass")
        self.assertTrue(res["line"].startswith("Pass"))
        self.assertEqual(res["peak"]["gpu_w"], 290)
        self.assertEqual(res["sustained"]["cpu_mhz"], 4300)
        self.assertEqual(res["throttle"]["gpu"], ["power limit"])
        self.assertEqual(res["errors"], [])
        self.assertLessEqual(self.clock.t, 62)

    def test_it_ends_at_its_length_and_never_past_five_minutes(self):
        r = self.runner(900)
        r.run()
        self.assertEqual(r.state, "done")
        self.assertLessEqual(self.clock.t, 302)

    def test_safety_stops(self):
        cases = (
            (lambda t: reading(cpu_c=90 if t > 10 else 70), "failed", "processor reached 90"),
            (lambda t: reading(gpu_c=85 if t > 10 else 60), "failed", "graphics card reached 85"),
            (lambda t: reading(coolant=40.0 if t > 10 else 30.0), "failed", "coolant reached 40"),
            (lambda t: dict(reading(), fan=None) if t > 10 else reading(), "failed", "fan service stopped reporting"),
        )
        for script, state, want in cases:
            with self.subTest(want):
                self.setUp()
                r = self.runner(120, script=script)
                self.assertEqual(r.run(), 1)
                self.assertEqual((r.state, want in r.reason), (state, True), r.reason)
                self.assertLess(self.clock.t, 20)
                self.assertFalse(os.path.exists(o1fan.tuning_path()))
                self.assertTrue(self.loads[0].stopped and self.loads[1].stopped)
                self.assertEqual(read_json_safe(F.last_path(), None)["result"]["verdict"], "fail")

    def test_kernel_errors_end_it(self):
        for line, kind in (("nvme nvme0: I/O 12 QID 3 timeout, aborting", "NVMe"),
                           ("pcieport 0000:00:01.1: AER: Corrected error received: 0000:2d:00.0", "PCIe")):
            with self.subTest(kind):
                self.setUp()
                r = self.runner(120, errors=lambda since, line=line, kind=kind: [(kind, line)] if self.clock.t > 8 else [])
                self.assertEqual(r.run(), 1)
                self.assertEqual(r.state, "failed")
                self.assertIn(kind, r.reason)
                res = read_json_safe(F.last_path(), None)["result"]
                self.assertEqual(res["errors"][0]["kind"], kind)
                self.assertTrue(res["line"].startswith("Fail"))

    def test_a_request_or_a_queue_stops_it(self):
        for gw in ((1, 0, 0), (0, 1, 0), (None, None, None)):
            with self.subTest(gw):
                self.setUp()
                r = self.runner(120, gateway=gw)
                r.run()
                self.assertEqual(r.state, "stopped")
                self.assertFalse(os.path.exists(o1fan.tuning_path()))
                self.assertEqual(read_json_safe(F.last_path(), None)["result"]["verdict"], "stopped")

    def test_stop_from_the_panel(self):
        r = self.runner(120)
        ev = r.stop
        orig = r.wait

        def wait(s):
            orig(s)
            if self.clock.t >= 10:
                r.why_stop = "stopped from the panel"
                ev.set()
        r.wait = wait
        r.run()
        self.assertEqual((r.state, r.reason), ("stopped", "stopped from the panel"))
        self.assertTrue(self.loads[0].stopped and self.loads[1].stopped)

    def test_refused_runs_nothing(self):
        r = self.runner(60, refuse=lambda: "not at night (23:00 to 08:00): it is loud")
        self.assertEqual(r.run(), 1)
        self.assertEqual(r.state, "refused")
        self.assertFalse(self.loads[0].stopped)
        self.assertEqual(self.held, [])
        self.assertIsNone(read_json_safe(F.last_path(), None).get("result"))

    def test_thermal_slowdown_fails_power_limit_does_not(self):
        r = self.runner(60, script=lambda t: reading(reasons=("power limit", "thermal (hardware)"),
                                                      bad=("thermal (hardware)",)))
        r.run()
        res = read_json_safe(F.last_path(), None)["result"]
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("thermal (hardware)", res["line"])

    def test_cpu_verification_failure_fails(self):
        self.loads = (FakeLoad(["stress-ng: fail: matrixprod verification failed", "stress-ng: info: failed: 2"]),
                      FakeLoad())
        r = self.runner(60)
        r.run()
        res = read_json_safe(F.last_path(), None)["result"]
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("2 stressor(s) failed verification", res["line"])

    def test_an_idle_card_fails(self):
        r = self.runner(60, script=lambda t: reading(busy=3))
        r.run()
        res = read_json_safe(F.last_path(), None)["result"]
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("its load did not run", res["line"])

    def test_progress_has_the_live_readings_and_the_countdown(self):
        r = self.runner(60)
        orig = r.wait
        seen = []

        def wait(s):
            seen.append(read_json_safe(F.progress_path(), None))
            orig(s)
        r.wait = wait
        r.run()
        mid = seen[5]
        self.assertEqual(mid["state"], "running")
        self.assertEqual(mid["left"], 60 - mid["t"])
        self.assertEqual(mid["now"]["gpu"]["watts"], 290)
        self.assertEqual(mid["now"]["fan"]["pump_rpm"], 2800)
        self.assertEqual(mid["limits"], {"cpu_c": 90, "gpu_c": 85, "coolant_c": 40})


class TestReaders(unittest.TestCase):
    def test_kernel_errors(self):
        log = "\n".join(["nvme nvme1: I/O 7 QID 2 timeout, reset controller",
                         "pcieport 0000:00:03.1: AER: Multiple Corrected error received",
                         "NVRM: Xid (PCI:0000:2d:00): 79, GPU has fallen off the bus.",
                         "nvme nvme0: 16/0/0 default/read/poll queues",          # a normal boot line
                         "usb 1-2: new full-speed USB device", "audit: type=1400"])
        run = lambda *a, **k: subprocess.CompletedProcess(a, 0, log, "")             # noqa: E731
        kinds = [k for k, _ln in F.kernel_errors(0, run=run)]
        self.assertEqual(kinds, ["NVMe", "PCIe", "NVIDIA Xid"])

    def test_nvidia_reading(self):
        out = "99, 2745, 3135, 291.40, 300.00, 71, 0x0000000000000044\n"
        run = lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")             # noqa: E731
        g = F.read_gpu(run=run)
        self.assertEqual((g["busy"], g["mhz"], g["watts"], g["limit_w"], g["c"]), (99, 2745, 291.4, 300, 71))
        self.assertEqual(g["reasons"], ["power limit", "thermal (hardware)"])
        self.assertEqual(g["bad"], ["thermal (hardware)"])

    def test_cpu_load_never_touches_a_disk(self):
        argv = F.cpu_argv(60, "/tmp/x")
        self.assertIn("--cpu", argv)
        self.assertEqual(argv[argv.index("--cpu") + 1], "0")                 # every thread
        for bad in ("--hdd", "--io", "--iomix", "--vm", "--vm-bytes", "--readahead", "--sync-file", "--aio"):
            self.assertNotIn(bad, argv)
        self.assertEqual(argv[:3], ["nice", "-n", "10"])

    def test_fans_from_the_fan_service(self):
        st = {"phase": "tuning", "pct": 100, "outputs": [{"label": "Pump", "rpm": 2800}, {"label": "case/CPU fan 2",
                                                                                          "rpm": 1500}],
              "aio": {"found": True, "pump_rpm": 2790, "coolant_c": 27.5}}
        f = F.read_fans(lambda: st)
        self.assertEqual((f["pump_rpm"], f["coolant_c"], f["fans"]), (2800, 27.5, [{"label": "case/CPU fan 2",
                                                                                     "rpm": 1500}]))
        self.assertIsNone(F.read_fans(lambda: None))


class TestStopPost(unittest.TestCase):
    def test_it_gives_the_fans_back_and_says_it_ended(self):
        write_json_atomic(o1fan.tuning_path(), {"at": int(time.time()), "pct": 100, "by": "fulltest"})
        write_json_atomic(F.progress_path(), {"at": int(time.time()), "state": "running", "seconds": 60, "left": 30})
        F.stop_post(log=lambda *_: None)
        self.assertFalse(os.path.exists(o1fan.tuning_path()))
        st = read_json_safe(F.progress_path(), None)
        self.assertEqual((st["state"], st["result"]["verdict"]), ("stopped", "stopped"))
        self.assertEqual(read_json_safe(F.last_path(), None)["state"], "stopped")

    def test_the_calibrations_hold_is_left_alone(self):
        write_json_atomic(o1fan.tuning_path(), {"at": int(time.time()), "pct": 50})
        F.stop_post(log=lambda *_: None)
        self.assertTrue(os.path.exists(o1fan.tuning_path()))
        os.unlink(o1fan.tuning_path())

    def test_the_fan_service_follows_the_hold(self):
        write_json_atomic(o1fan.tuning_path(), {"at": int(time.time()), "pct": 100, "by": "fulltest"})
        self.assertEqual(o1fan.read_tuning(), 100)
        os.unlink(o1fan.tuning_path())


class TestUnits(unittest.TestCase):
    def unit(self, name):
        with open(os.path.join(U.SYSTEMD, name)) as f:
            return f.read()

    def test_run_unit(self):
        u = self.unit("ollama1-fulltest@.service")
        self.assertIn("ExecStart=/usr/local/lib/ollama1/bin/ollama1-fulltest run %i", u)
        self.assertIn("ExecStopPost=/usr/local/lib/ollama1/bin/ollama1-fulltest stop-post", u)
        self.assertIn("KillMode=control-group", u)
        self.assertIn("IPAddressDeny=any", u)
        self.assertIn("ProtectSystem=strict", u)
        self.assertIn("ReadWritePaths=/run/ollama1 /var/lib/ollama1\n", u)
        self.assertNotIn("\n[Install]", u)
        t = int(u.split("TimeoutStartSec=")[1].split()[0])
        self.assertTrue(F.MAX_S < t <= 600, t)

    def test_stop_unit_and_busy(self):
        self.assertIn("ExecStart=/usr/local/lib/ollama1/bin/ollama1-fulltest stop", self.unit("ollama1-fulltest-stop.service"))
        self.assertIn(F.UNIT_GLOB, dict(o1sleep.BUSY_UNITS))

    def test_stop_stops_every_instance(self):
        calls = []
        F.request_stop(systemctl=lambda: calls.append(1) or 0)
        self.assertTrue(os.path.exists(F.abort_path()))
        self.assertEqual(calls, [1])
        os.unlink(F.abort_path())


if __name__ == "__main__":
    unittest.main()
