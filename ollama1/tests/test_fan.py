"""Fans at full speed while the server works (6b385): the service's machine on a
fake sysfs tree and a fake clock (working -> 255 and manual, the 60 s hold, a
new request resetting it, the temperature override and its hysteresis,
unwritable outputs, originals across a restart, a wake), the status text, the
modules and setup steps, the unit file, the setup.sh and sleep-hook wiring."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1fan

NCT_ENABLE = {1: 5, 2: 5, 3: 1, 4: 0, 5: 5, 6: 5, 7: 5}      # as the chip had them
NCT_PWM = {1: 90, 2: 90, 3: 120, 4: 255, 5: 90, 6: 90, 7: 90}


class Tree:
    """A fake /sys/class/hwmon: the card (amdgpu), the motherboard's chip (nct6797),
    the CPU, an NVMe drive."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="o1fan-sys-")
        self.base = self.root + "/class/hwmon"
        self.gpu = self.put("hwmon0", "amdgpu", {"pwm1": 80, "pwm1_enable": 2, "fan1_input": 1200,
                                                 "temp1_input": 45000, "temp1_label": "edge",
                                                 "temp2_input": 52000, "temp2_label": "junction"})
        files = {}
        for n in range(1, 8):
            files.update({"pwm%d" % n: NCT_PWM[n], "pwm%d_enable" % n: NCT_ENABLE[n], "fan%d_input" % n: 500 + n})
        self.nct = self.put("hwmon1", "nct6797", files)
        self.cpu = self.put("hwmon2", "k10temp", {"temp1_input": 41000, "temp1_label": "Tctl"})
        self.nvme = self.put("hwmon3", "nvme", {"temp1_input": 38000, "temp1_label": "Composite"})

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

    def temp(self, which, c, file="temp1_input"):
        self.set({"cpu": self.cpu, "nvme": self.nvme, "gpu": self.gpu}[which], file, int(c * 1000))

    def full(self):
        """True when every output is manual at 255."""
        return (self.get(self.gpu, "pwm1_enable") == 1 and self.get(self.gpu, "pwm1") == 255 and
                all(self.get(self.nct, "pwm%d_enable" % n) == 1 and self.get(self.nct, "pwm%d" % n) == 255
                    for n in range(1, 8)))

    def as_before(self):
        return (self.get(self.gpu, "pwm1_enable") == 2 and
                all(self.get(self.nct, "pwm%d_enable" % n) == NCT_ENABLE[n] for n in range(1, 8)) and
                self.get(self.nct, "pwm3") == NCT_PWM[3])           # the manual one: its value too (an auto output's
                                                                     # value file is the driver's to move)


class Clock:
    def __init__(self):
        self.t = 1000.0

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
        for f in (o1fan.state_path(), o1fan.status_path()):
            if os.path.exists(f):
                os.unlink(f)
        self.addCleanup(lambda: [os.path.exists(f) and os.unlink(f) for f in (o1fan.state_path(), o1fan.status_path())])
        self.clock = Clock()
        self.p = {"inflight": 0, "gpu_busy": 0, "tools": [], "loadavg": 0.1}
        self.lines = []
        self.boot = "boot-1"

    def fan(self, **kw):
        probes = {k: (lambda k=k: self.p[k]) for k in self.p}
        kw.setdefault("io", RecordingIO())
        return o1fan.Fan(probes, clock=self.clock, wall=lambda: 1_800_000_000, log=self.lines.append,
                         sysroot=self.tree.root, boot_id=lambda: self.boot, tools_every=0, **kw)

    def run_for(self, fan, seconds, step=1):
        """Tick every `step` s for `seconds`; the mode after each tick."""
        modes = []
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            modes.append(fan.tick()[0])
        return modes


class TestWorking(FanCase):
    def test_a_request_puts_every_output_at_255_in_manual(self):
        f = self.fan()
        self.p["inflight"] = 1
        self.assertEqual(f.tick()[0], "full")
        self.assertTrue(self.tree.full())
        self.assertIn("controlling GPU fan + 7 case/CPU fan outputs", self.lines)

    def test_idle_changes_nothing(self):
        f = self.fan()
        f.io.writes.clear()
        self.run_for(f, 120)
        self.assertEqual(f.io.writes, [])
        self.assertTrue(self.tree.as_before())
        self.assertEqual(f.mode, "auto")

    def test_each_source_of_work_counts_and_its_limit(self):
        for name, busy, quiet in (("inflight", 1, 0), ("gpu_busy", 10, 9), ("tools", ["stability-test.sh"], []),
                                  ("loadavg", 1.6, 1.5)):
            with self.subTest(name):
                self.setUp()
                f = self.fan()
                self.p[name] = quiet
                self.assertEqual(f.tick()[0], "auto", name)
                self.p[name] = busy
                self.assertEqual(f.tick()[0], "full", name)
                self.assertTrue(self.tree.full())

    def test_a_probe_that_fails_is_not_work(self):
        f = o1fan.Fan({"inflight": lambda: 1 / 0, "gpu_busy": lambda: None, "tools": lambda: None},
                      clock=self.clock, wall=lambda: 1_800_000_000, log=self.lines.append, sysroot=self.tree.root,
                      boot_id=lambda: self.boot)
        self.assertEqual(f.tick()[0], "auto")

    def test_never_a_value_below_255_while_holding_and_only_manual(self):
        f = self.fan()
        self.p["inflight"] = 1
        self.run_for(f, 10)
        self.p["inflight"] = 0
        self.run_for(f, 100)
        for path, value in f.io.writes:
            if path.endswith("_enable"):
                self.assertIn(value, ("1", "2", "5", "0"), path)
            else:
                self.assertIn(value, ("255", "120"), path)           # 120: the manual output's own value, put back
        before_restore = [w for w in f.io.writes if w[1] in ("1", "255")]
        self.assertTrue(before_restore)


class TestHold(FanCase):
    def test_59_s_is_still_full_and_61_s_is_back_exactly_as_it_was(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()                                  # t=1000: working
        self.p["inflight"] = 0
        self.clock.t = 1059
        self.assertEqual(f.tick()[0], "hold")
        self.assertTrue(self.tree.full())
        self.clock.t = 1061
        self.assertEqual(f.tick()[0], "auto")
        self.assertTrue(self.tree.as_before())
        self.assertFalse(os.path.exists(o1fan.state_path()))

    def test_exactly_60_s_is_over(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.p["inflight"] = 0
        self.clock.t = 1060
        self.assertEqual(f.tick()[0], "auto")

    def test_the_hold_counts_down_in_the_status(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.p["inflight"] = 0
        self.clock.t = 1018
        f.tick()
        st = o1fan.read_status(now=1_800_000_000)
        self.assertEqual((st["mode"], st["hold_left"]), ("hold", 42))

    def test_a_new_request_during_the_hold_starts_it_again(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.p["inflight"] = 0
        self.clock.t = 1040
        self.assertEqual(f.tick()[0], "hold")
        self.p["inflight"] = 1
        self.assertEqual(f.tick()[0], "full")
        self.p["inflight"] = 0
        self.clock.t = 1040 + 59
        self.assertEqual(f.tick()[0], "hold")
        self.assertTrue(self.tree.full())
        self.clock.t = 1040 + 61
        self.assertEqual(f.tick()[0], "auto")
        self.assertTrue(self.tree.as_before())

    def test_working_all_along_never_lets_go(self):
        f = self.fan()
        self.p["gpu_busy"] = 99
        modes = self.run_for(f, 300, step=2)
        self.assertEqual(set(modes), {"full"})
        self.assertTrue(self.tree.full())


class TestOverride(FanCase):
    def test_a_hot_part_forces_full_with_nothing_running(self):
        for which, limit, file in (("cpu", 80, "temp1_input"), ("gpu", 90, "temp2_input"), ("nvme", 70, "temp1_input")):
            with self.subTest(which):
                self.setUp()
                f = self.fan()
                self.tree.temp(which, limit - 0.5, file)
                self.assertEqual(f.tick()[0], "auto")
                self.tree.temp(which, limit, file)
                mode, why = f.tick()
                self.assertEqual(mode, "full")
                self.assertIn("too warm", why)
                self.assertTrue(self.tree.full())

    def test_it_lets_go_ten_degrees_under_the_limit_and_not_before(self):
        f = self.fan()
        self.tree.temp("cpu", 83)
        self.assertEqual(f.tick()[0], "full")
        self.tree.temp("cpu", 79)                # under the limit, not 10 under
        self.assertEqual(f.tick()[0], "full")
        self.tree.temp("cpu", 70.0)              # exactly 10 under is not under yet
        self.assertEqual(f.tick()[0], "full")
        self.tree.temp("cpu", 69.9)
        self.assertEqual(f.tick()[0], "auto")
        self.assertTrue(self.tree.as_before())
        self.tree.temp("cpu", 75)                # cooler than the limit but never over it again: stays auto
        self.assertEqual(f.tick()[0], "auto")

    def test_after_it_lets_go_the_normal_rule_follows(self):
        f = self.fan()
        self.tree.temp("gpu", 95, "temp2_input")
        f.tick()
        self.p["loadavg"] = 5
        self.tree.temp("gpu", 80, "temp2_input")
        self.assertEqual(f.tick()[0], "full")    # hysteresis, and working too
        self.tree.temp("gpu", 40, "temp2_input")
        self.assertEqual(f.tick()[0], "full")    # the load still says working
        self.p["loadavg"] = 0.1
        self.clock.t += 61
        self.assertEqual(f.tick()[0], "auto")

    def test_a_sensor_that_stops_answering_keeps_its_state(self):
        f = self.fan()
        self.tree.temp("cpu", 85)
        f.tick()
        os.unlink(self.tree.cpu + "/temp1_input")
        self.assertEqual(f.tick()[0], "full")


class TestSkipAndFailures(FanCase):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anything")
    def test_an_output_it_cannot_write_is_left_alone(self):
        os.chmod(self.tree.nct + "/pwm3_enable", 0o444)
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.assertIn("controlling GPU fan + 6 case/CPU fan outputs", self.lines)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm3_enable"), 1)         # untouched (it was 1 already)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm3"), 120)              # and its value was not written
        self.assertEqual(self.tree.get(self.tree.nct, "pwm2"), 255)

    def test_a_write_that_fails_is_skipped_and_said_once(self):
        class Fussy(RecordingIO):
            def write(self, path, value):
                if path.endswith("pwm5"):
                    raise OSError(5, "I/O error")
                super().write(path, value)
        f = self.fan(io=Fussy())
        self.p["inflight"] = 1
        self.run_for(f, 6)
        self.assertEqual(sum(1 for l in self.lines if "can't write hwmon1/pwm5" in l), 1)
        self.assertEqual(self.tree.get(self.tree.nct, "pwm5_enable"), 1)         # the enable did change: restore must undo it
        self.p["inflight"] = 0
        self.clock.t += 100
        f.tick()
        self.assertTrue(self.tree.as_before())
        self.assertEqual(self.tree.get(self.tree.nct, "pwm5_enable"), 5)

    def test_no_chip_at_all_is_not_an_error(self):
        shutil.rmtree(self.tree.base)
        os.makedirs(self.tree.base)
        f = self.fan()
        self.p["inflight"] = 1
        self.assertEqual(f.tick()[0], "full")
        self.assertIn("controlling no fan outputs it can write", self.lines)


class TestRestart(FanCase):
    def test_originals_are_saved_before_anything_changes(self):
        self.p["inflight"] = 1

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

    def test_a_restart_in_the_middle_of_a_hold_puts_back_the_originals_not_the_manual_ones(self):
        a = self.fan()
        self.p["inflight"] = 1
        a.tick()
        self.p["inflight"] = 0
        self.clock.t += 10
        a.tick()                                              # holding; the service dies here (no shutdown)
        self.assertTrue(self.tree.full())
        b = self.fan()                                        # the restart
        self.assertTrue(b.engaged)
        self.assertIn("found fans left at full speed by an earlier run: 8 outputs, put back after the hold", self.lines)
        self.assertEqual(self.run_for(b, 30)[-1], "hold")     # a minute of hold from the restart
        self.assertTrue(self.tree.full())
        self.assertEqual(self.run_for(b, 40)[-1], "auto")
        self.assertTrue(self.tree.as_before())
        self.assertFalse(os.path.exists(o1fan.state_path()))

    def test_a_restart_while_working_keeps_the_originals_of_the_first_run(self):
        a = self.fan()
        self.p["inflight"] = 1
        a.tick()
        b = self.fan()
        b.tick()
        self.p["inflight"] = 0
        self.run_for(b, 70)
        self.assertTrue(self.tree.as_before())

    def test_another_boot_means_the_hardware_is_as_it_was(self):
        a = self.fan()
        self.p["inflight"] = 1
        a.tick()
        self.boot = "boot-2"                                  # rebooted: the chip is back to its own settings
        for n in range(1, 8):
            self.tree.set(self.tree.nct, "pwm%d_enable" % n, 5)
        self.tree.set(self.tree.gpu, "pwm1_enable", 2)
        self.p["inflight"] = 0
        b = self.fan()
        self.assertFalse(b.engaged)
        self.assertFalse(os.path.exists(o1fan.state_path()))
        b.io.writes.clear()
        self.run_for(b, 100)
        self.assertEqual(b.io.writes, [])

    def test_a_clean_stop_puts_everything_back(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        f.shutdown()
        self.assertTrue(self.tree.as_before())
        self.assertFalse(os.path.exists(o1fan.state_path()))

    def test_stop_post_after_a_clean_stop_restores_and_after_a_crash_leaves_it_full(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.assertTrue(o1fan.restore_after_stop({"SERVICE_RESULT": "exit-code"}, sysroot=self.tree.root,
                                                 boot_id=lambda: self.boot, log=self.lines.append))
        self.assertTrue(self.tree.full())                    # a crash: never slower than it was
        self.assertTrue(os.path.exists(o1fan.state_path()))
        o1fan.restore_after_stop({"SERVICE_RESULT": "success"}, sysroot=self.tree.root, boot_id=lambda: self.boot,
                                 log=self.lines.append)
        self.assertTrue(self.tree.as_before())
        self.assertFalse(os.path.exists(o1fan.state_path()))
        self.assertTrue(o1fan.restore_after_stop({"SERVICE_RESULT": "success"}, sysroot=self.tree.root,
                                                 boot_id=lambda: self.boot, log=self.lines.append))     # again: nothing

    def test_a_restore_that_fails_is_kept_and_tried_again(self):
        class Fussy(RecordingIO):
            fail = False

            def write(inner, path, value):                     # noqa: N805
                if inner.fail and path.endswith("pwm1_enable"):
                    raise OSError(16, "busy")
                super().write(path, value)
        f = self.fan(io=Fussy())
        self.p["inflight"] = 1
        f.tick()
        self.p["inflight"] = 0
        f.io.fail = True
        self.clock.t += 100
        f.tick()
        self.assertTrue(os.path.exists(o1fan.state_path()))
        self.assertEqual(self.tree.get(self.tree.gpu, "pwm1_enable"), 1)
        f.io.fail = False
        self.clock.t += 2
        f.tick()
        self.assertTrue(self.tree.as_before())
        self.assertFalse(os.path.exists(o1fan.state_path()))


class TestWake(FanCase):
    def test_after_a_wake_that_reset_the_card_it_is_written_again(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.tree.set(self.tree.gpu, "pwm1_enable", 2)        # amdgpu on resume
        self.tree.set(self.tree.gpu, "pwm1", 80)
        for n in range(1, 8):
            self.tree.set(self.tree.nct, "pwm%d_enable" % n, 5)
        f.tick()
        self.assertTrue(self.tree.full())

    def test_it_is_written_again_in_the_hold_too_but_the_saved_originals_stay_the_first_ones(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.p["inflight"] = 0
        self.tree.set(self.tree.gpu, "pwm1_enable", 2)
        self.clock.t += 20
        self.assertEqual(f.tick()[0], "hold")
        self.assertTrue(self.tree.full())
        self.clock.t += 100
        f.tick()
        self.assertTrue(self.tree.as_before())

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


class TestStatus(FanCase):
    def snap(self):
        return o1fan.snapshot(o1fan.find_outputs(self.tree.root), sysroot=self.tree.root)

    def test_full_says_why_and_shows_each_fan(self):
        f = self.fan()
        self.p["inflight"] = 1
        self.p["gpu_busy"] = 80
        f.tick()
        text = o1fan.render_status(o1fan.read_status(now=1_800_000_000), self.snap())
        self.assertIn("mode: full - a request is running; the graphics card is busy (80%)", text)
        self.assertIn("controlling: GPU fan + 7 case/CPU fan outputs", text)
        self.assertRegex(text, r"GPU fan\s+manual\s+pwm 255/255\s+1200 rpm")
        self.assertRegex(text, r"case/CPU fan 3\s+manual\s+pwm 255/255\s+503 rpm")
        self.assertIn("temps: GPU junction 52 C, CPU 41 C, NVMe 38 C", text)

    def test_hold_and_auto_and_a_service_that_is_not_running(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.p["inflight"] = 0
        self.clock.t += 18
        f.tick()
        self.assertIn("mode: hold 42s", o1fan.render_status(o1fan.read_status(now=1_800_000_000), self.snap()))
        self.clock.t += 100
        f.tick()
        text = o1fan.render_status(o1fan.read_status(now=1_800_000_000), self.snap())
        self.assertIn("mode: auto", text)
        self.assertRegex(text, r"GPU fan\s+auto\s+pwm")
        self.assertIn("isn't running", o1fan.render_status(None, self.snap()))

    def test_the_status_file_goes_stale(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.assertIsNotNone(o1fan.read_status(now=1_800_000_000 + 10))
        self.assertIsNone(o1fan.read_status(now=1_800_000_000 + 16))
        self.assertIsNone(o1fan.panel_line(now=1_800_000_000 + 16))

    def test_the_panel_line(self):
        f = self.fan()
        self.p["inflight"] = 1
        f.tick()
        self.assertEqual(o1fan.panel_line(now=1_800_000_000),
                         "Fans: 100% (a request is running)  -  GPU fan 1200 rpm, case fans up to 507 rpm")
        self.p["inflight"] = 0
        self.clock.t += 30
        f.tick()
        self.assertIn("Fans: 100% for 30 s more, to cool down", o1fan.panel_line(now=1_800_000_000))
        self.clock.t += 100
        f.tick()
        self.assertTrue(o1fan.panel_line(now=1_800_000_000).startswith("Fans: automatic"))

    def test_the_status_command_needs_no_root_and_reads_the_tree(self):
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-fan"), "status"], capture_output=True,
                           text=True, env=dict(os.environ, OLLAMA1_SYS=self.tree.root, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mode: the service isn't running", r.stdout)
        self.assertIn("GPU fan", r.stdout)
        self.assertIn("temps: GPU junction 52 C, CPU 41 C", r.stdout)

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
        self.assertIn("\nExecStopPost=/usr/local/lib/ollama1/bin/ollama1-fan stop-post\n", u)
        self.assertIn("\nRestart=always\n", u)
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

    def test_the_plan_line(self):
        self.assertIn("Fans ON", self.bash("fans_plan on")[1])
        self.assertIn("Fans OFF", self.bash("fans_plan off")[1])

    def test_the_flag_is_in_the_help_and_a_bad_value_is_refused(self):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--help"], capture_output=True, text=True)
        self.assertIn("--fans on|off", r.stdout)
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
