"""Quiet hours (6b601): the window and its settings file, the night's shorter auto-sleep minutes (only the
timing value), the fans' capped working level with the heavy level and the overheat override kept, the lights
and the cooler's lights off, OpenRGB leaving the Corsair cooler to liquidctl, the gateway's 2-minute keep_alive,
its `quiet` view and its refusal of downloads at night, and the afternoon update/reboot times."""
import json
import os
import re
import shutil
import tempfile
import time
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1aio
import o1idle
import o1leds
import o1quiet
import test_fan as TF
import test_gateway as TG

QFILE = o1quiet.QUIET_FILE


def pwm(pct):
    return int(pct * 255 / 100 + 0.5)                      # as the fan service writes a percent


def at(h, m=0):
    """A struct_time-returning localtime for a fixed clock time (the date doesn't matter here)."""
    return lambda _t=None: time.struct_time((2026, 11, 11, h, m, 0, 2, 315, 0))


def write_quiet(obj):
    with open(QFILE, "w") as f:
        f.write(json.dumps(obj))
    o1quiet._cache["val"] = None


def restore_quiet():
    write_quiet({"enabled": False})


# ---- the window and the file ------------------------------------------------------------------------------

class TestWindow(unittest.TestCase):
    def test_the_default_night_crosses_midnight(self):
        cfg = o1quiet.clean({})
        for h, m, want in ((22, 59, False), (23, 0, True), (0, 0, True), (3, 30, True), (7, 59, True),
                           (8, 0, False), (14, 0, False)):
            with self.subTest("%02d:%02d" % (h, m)):
                self.assertIs(o1quiet.is_quiet(0, cfg, at(h, m)), want)

    def test_a_daytime_window_and_start_equal_end(self):
        self.assertTrue(o1quiet.in_window(13 * 60, "12:00", "14:00"))
        self.assertFalse(o1quiet.in_window(14 * 60, "12:00", "14:00"))
        for minute in range(0, 1440, 7):
            self.assertFalse(o1quiet.in_window(minute, "06:00", "06:00"))

    def test_turned_off_is_never_quiet(self):
        cfg = o1quiet.clean({"enabled": False})
        self.assertFalse(o1quiet.is_quiet(0, cfg, at(2)))
        self.assertIsNone(o1quiet.keep_alive(0, cfg, at(2)))
        self.assertIsNone(o1quiet.fan_cap(0, cfg, at(2)))
        self.assertFalse(o1quiet.lights_off(0, cfg, at(2)))
        self.assertEqual(o1quiet.sleep_minutes(30, 0, cfg, at(2)), 30)

    def test_each_bad_value_falls_back_on_its_own(self):
        cfg = o1quiet.clean({"enabled": "yes", "start": "24:00", "end": "7:00", "sleep_minutes": 2,
                             "fan_pct": 10, "keep_alive_s": True, "lights_off": 1, "other": "x"})
        self.assertEqual(cfg, o1quiet.DEFAULTS)
        cfg = o1quiet.clean({"start": "22:30", "end": "06:45", "sleep_minutes": 15, "fan_pct": 40,
                             "keep_alive_s": 300, "lights_off": False})
        self.assertEqual((cfg["start"], cfg["end"], cfg["sleep_minutes"], cfg["fan_pct"], cfg["keep_alive_s"],
                          cfg["lights_off"]), ("22:30", "06:45", 15, 40, 300, False))
        self.assertEqual(o1quiet.clean(["not", "a", "dict"]), o1quiet.DEFAULTS)

    def test_the_file_is_read_again_when_it_changes_and_a_broken_one_is_the_defaults(self):
        self.addCleanup(restore_quiet)
        write_quiet({"enabled": True, "sleep_minutes": 20})
        self.assertEqual(o1quiet.read_config()["sleep_minutes"], 20)
        with open(QFILE, "w") as f:
            f.write("{not json")
        o1quiet._cache["val"] = None
        self.assertEqual(o1quiet.read_config(), o1quiet.DEFAULTS)
        os.unlink(QFILE)
        o1quiet._cache["val"] = None
        self.assertEqual(o1quiet.read_config(), o1quiet.DEFAULTS)       # no file: on, 23:00 to 08:00

    def test_the_night_values(self):
        cfg = o1quiet.clean({})
        self.assertEqual(o1quiet.sleep_minutes(30, 0, cfg, at(1)), 12)
        self.assertEqual(o1quiet.sleep_minutes(10, 0, cfg, at(1)), 10)       # a shorter day setting stays
        self.assertEqual(o1quiet.sleep_minutes(30, 0, cfg, at(12)), 30)
        self.assertEqual(o1quiet.keep_alive(0, cfg, at(1)), "120s")
        self.assertIsNone(o1quiet.keep_alive(0, cfg, at(12)))
        self.assertEqual(o1quiet.fan_cap(0, cfg, at(1)), 35)
        self.assertTrue(o1quiet.lights_off(0, cfg, at(1)))
        self.assertFalse(o1quiet.lights_off(0, o1quiet.clean({"lights_off": False}), at(1)))

    def test_the_view_has_exactly_its_six_fields(self):
        v = o1quiet.view(0, o1quiet.clean({}), at(2))
        self.assertEqual(sorted(v), ["enabled", "end", "now", "sleep_minutes", "start", "tz"])
        self.assertEqual((v["enabled"], v["start"], v["end"], v["now"], v["sleep_minutes"]),
                         (True, "23:00", "08:00", True, 12))
        self.assertIsInstance(v["tz"], str)

    def test_the_zone_name(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(o1quiet.tz_name(d), "")
        os.symlink("/usr/share/zoneinfo/America/New_York", os.path.join(d, "localtime"))
        self.assertEqual(o1quiet.tz_name(d), "America/New_York")
        with open(os.path.join(d, "timezone"), "w") as f:
            f.write("Europe/Lisbon\n")
        self.assertEqual(o1quiet.tz_name(d), "Europe/Lisbon")
        with open(os.path.join(d, "timezone"), "w") as f:
            f.write("../../etc/passwd x\n")
        self.assertEqual(o1quiet.tz_name(d), "America/New_York")       # nonsense in the file: the link decides


# ---- auto sleep: only the minutes ---------------------------------------------------------------------------

class TestIdleMinutes(unittest.TestCase):
    def setUp(self):
        o1idle.write_config({"enabled": True, "minutes": 30})
        self.addCleanup(o1idle.write_config, {"enabled": False, "minutes": 30})
        self.addCleanup(restore_quiet)
        self.slept = []
        self.t = 1_800_000_000.0
        boot = self.t - 13 * 60                                  # up (and idle) 13 minutes
        with open(o1idle.activity_file(), "w") as f:            # the gateway: fresh, nothing in flight
            json.dump({"at": self.t, "last": 0, "inflight": 0}, f)
        self.probes = {"supported": lambda: True, "wake": lambda: [], "boot_time": lambda: boot,
                       "loadavg": lambda: 0.1, "gpu_present": lambda: True, "gpu_busy": lambda: 0,
                       "busy": lambda: [], "tools": lambda: [], "inhibitors": lambda: []}

    def tick(self):
        i = o1idle.Idle(self.probes, suspend=lambda: self.slept.append(1) or 0, log=lambda l: None,
                        clock=lambda: self.t, mono=lambda: 100.0)
        return i.tick()

    def test_at_night_13_idle_minutes_are_enough_and_by_day_they_are_not(self):
        write_quiet({"enabled": True, "start": "00:00", "end": "23:59"})          # quiet now, whatever the clock
        sleep, why = self.tick()
        self.assertTrue(sleep, why)
        self.assertEqual(why, "idle 13 minutes")
        d = json.load(open(o1idle.idle_file()))
        self.assertEqual((d["minutes"], d["count_minutes"], d["night"]), (30, 12, True))   # the setting unchanged
        self.assertEqual(o1idle.read_config(), {"enabled": True, "minutes": 30})
        write_quiet({"enabled": False})
        sleep, why = self.tick()
        self.assertFalse(sleep)
        self.assertEqual(why, "idle 13 of 30 minutes")
        d = json.load(open(o1idle.idle_file()))
        self.assertEqual((d["count_minutes"], d["night"]), (30, False))

    def test_the_night_never_turns_auto_sleep_on_or_skips_a_rule(self):
        write_quiet({"enabled": True, "start": "00:00", "end": "23:59"})
        o1idle.write_config({"enabled": False, "minutes": 30})
        self.assertEqual(self.tick(), (False, "auto sleep is off"))
        o1idle.write_config({"enabled": True, "minutes": 30})
        self.probes["inhibitors"] = lambda: ["a download"]
        self.assertFalse(self.tick()[0])
        self.assertEqual(self.slept, [])

    def test_a_broken_quiet_module_keeps_the_day_minutes(self):
        orig = o1quiet.sleep_minutes
        o1quiet.sleep_minutes = lambda *a, **k: 1 / 0
        self.addCleanup(setattr, o1quiet, "sleep_minutes", orig)
        self.assertEqual(self.tick(), (False, "idle 13 of 30 minutes"))


# ---- the fans -----------------------------------------------------------------------------------------------

class TestNightFans(TF.FanCase):
    def test_by_day_the_working_level_is_the_days(self):
        f = self.up(work_pct=50, night_cap=lambda: None)
        self.busy(f)
        self.assertTrue(self.tree.at(pwm(50)))
        self.assertFalse(f.night)

    def test_at_night_the_working_level_is_capped(self):
        f = self.up(work_pct=50, night_cap=lambda: 35)
        self.busy(f)
        self.assertTrue(self.tree.at(pwm(35)))
        self.assertTrue(f.night)
        self.assertTrue(json.load(open(TF.o1fan.status_path()))["night"])

    def test_a_temperature_near_its_limit_raises_them_to_the_days_level_at_once(self):
        f = self.up(work_pct=50, night_cap=lambda: 35)
        self.busy(f)
        self.tree.temp("gpu", 80, "temp2_input")               # the junction 10 C under its 90: within 12
        self.go(f, self.clock.t + 1)
        self.assertTrue(self.tree.at(pwm(50)))
        self.assertTrue(f.stepped)

    def test_the_overheat_override_is_still_100(self):
        f = self.up(work_pct=50, night_cap=lambda: 35)
        self.tree.temp("nvme", 70)
        self.assertEqual(self.go(f, self.clock.t + 1)[0], "hot")
        self.assertTrue(self.tree.at(255))

    def test_the_cap_never_goes_under_the_lowest_working_level_or_raises_a_low_day_level(self):
        f = self.up(work_pct=50, night_cap=lambda: 5)
        self.busy(f)
        self.assertTrue(self.tree.at(pwm(TF.o1fan.o1work.WORK_MIN_PCT)))
        self.setUp()
        g = self.up(work_pct=30, night_cap=lambda: 35)          # a day level under the cap: left as it is
        self.busy(g)
        self.assertTrue(self.tree.at(pwm(30)))
        self.assertFalse(g.night)

    def test_a_failing_or_odd_cap_is_the_day(self):
        for cap in (lambda: 1 / 0, lambda: "35", lambda: True):
            with self.subTest(cap=cap):
                self.setUp()
                f = self.up(work_pct=50, night_cap=cap)
                self.busy(f)
                self.assertTrue(self.tree.at(pwm(50)))

    def test_morning_gives_the_day_level_back(self):
        state = {"cap": 35}
        f = self.up(work_pct=50, night_cap=lambda: state["cap"])
        self.busy(f)
        self.assertTrue(self.tree.at(pwm(35)))
        state["cap"] = None
        self.go(f, self.clock.t + 1)
        self.assertTrue(self.tree.at(pwm(50)))


# ---- the lights ---------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.connected, self.shown, self.devices, self.notes = True, [], [], []

    def open(self):
        self.connected = True

    def close(self):
        pass

    def poll(self):
        return False

    def show(self, color):
        self.shown.append(color)


class TestLightsOff(unittest.TestCase):
    def lights(self, dark):
        self.lines = []
        c = FakeClient()
        L = o1leds.Leds({"gpu_busy": lambda: 0}, client=c, clock=lambda: 1000.0, wall=lambda: 1_800_000_000,
                          log=self.lines.append, status=False, fans=lambda: None, dark=dark)
        return L, c

    def test_at_night_the_lights_are_off_and_by_day_on(self):
        state = {"dark": True}
        L, c = self.lights(lambda: state["dark"])
        L.tick()
        self.assertEqual(c.shown[-1], o1leds.OFF)
        self.assertIn("quiet hours: the lights are off", self.lines)
        state["dark"] = False
        L.next_poll = 0
        L.tick()
        self.assertNotEqual(c.shown[-1], o1leds.OFF)
        self.assertIn("quiet hours: the lights are back on", self.lines)

    def test_a_failing_check_leaves_them_on(self):
        L, c = self.lights(lambda: 1 / 0)
        L.tick()
        self.assertNotEqual(c.shown[-1], o1leds.OFF)


# ---- OpenRGB leaves the Corsair cooler to liquidctl ---------------------------------------------------------

class TestHandOff(unittest.TestCase):
    def settings(self, det):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "OpenRGB", "OpenRGB.json")
        os.makedirs(os.path.dirname(p))
        with open(p, "w") as f:
            json.dump({"Detectors": {"detectors": det}, "Other": {"kept": 1}}, f)
        return p

    def test_the_hydro_detectors_are_turned_off_and_nothing_else(self):
        p = self.settings({"Corsair Hydro H115i Platinum": True, "Corsair Hydro Series": True,
                           "MSI Mystic Light MS_7C35": True, "Corsair Dominator Platinum": True})
        lines = []
        names = o1leds.leave_hydro_to_liquidctl(p, which=lambda n: "/usr/bin/liquidctl", log=lines.append)
        self.assertEqual(names, ["Corsair Hydro H115i Platinum", "Corsair Hydro Series"])
        d = json.load(open(p))
        self.assertEqual(d["Detectors"]["detectors"], {"Corsair Hydro H115i Platinum": False,
                                                       "Corsair Hydro Series": False,
                                                       "MSI Mystic Light MS_7C35": True,
                                                       "Corsair Dominator Platinum": True})
        self.assertEqual(d["Other"], {"kept": 1})
        self.assertEqual(o1leds.leave_hydro_to_liquidctl(p, which=lambda n: "/x"), [])      # already done
        self.assertTrue(any("left to liquidctl" in l for l in lines))

    def test_without_liquidctl_or_a_file_nothing_changes(self):
        p = self.settings({"Corsair Hydro H115i Platinum": True})
        before = open(p).read()
        self.assertEqual(o1leds.leave_hydro_to_liquidctl(p, which=lambda n: None), [])
        self.assertEqual(open(p).read(), before)
        self.assertEqual(o1leds.leave_hydro_to_liquidctl(p + ".missing", which=lambda n: "/x"), [])
        with open(p, "w") as f:
            f.write("[1, 2]")
        self.assertEqual(o1leds.leave_hydro_to_liquidctl(p, which=lambda n: "/x"), [])

    def test_openrgb_still_starts_when_the_hand_off_fails(self):
        calls, lines = [], []
        o1leds.run_openrgb(log=lines.append, execv=lambda p, a: calls.append(a),
                           hand_off=lambda **k: 1 / 0, which=lambda n: "/usr/bin/" + n,
                           run=lambda *a, **k: type("R", (), {"stdout": "--server-host", "stderr": "",
                                                              "returncode": 0})())
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("couldn't check OpenRGB" in l for l in lines))


# ---- the cooler's own lights --------------------------------------------------------------------------------

class TestCoolerLights(unittest.TestCase):
    def setUp(self):
        self.cls = next(v for v in vars(o1aio).values()
                        if isinstance(v, type) and hasattr(v, "safe_exit") and hasattr(v, "lights"))

    def make(self, dark, ok=True):
        self.cmds = []

        def run(cmd, **kw):
            self.cmds.append(cmd[1:])
            return type("R", (), {"returncode": 0 if ok else 1, "stdout": "", "stderr": "" if ok else "boom"})()
        a = self.cls(binary="/usr/bin/liquidctl", log=lambda l: None, run=run, dark=dark)
        a.exe, a.match = "/usr/bin/liquidctl", "H115i"
        return a

    def test_off_at_night_white_in_the_morning_once_each(self):
        state = {"dark": True}
        a = self.make(lambda: state["dark"])
        a.lights(0)
        a.lights(10)
        self.assertEqual(self.cmds, [["--match", "H115i", "set", "led", "color", "off"]])
        state["dark"] = False
        a.lights(20)
        a.lights(30)
        self.assertEqual(self.cmds[1:], [["--match", "H115i", "set", "led", "color", "fixed", "ffffff"]])

    def test_a_cooler_never_darkened_here_is_left_alone_by_day(self):
        a = self.make(lambda: False)
        a.lights(0)
        self.assertEqual(self.cmds, [])

    def test_a_failing_lights_command_never_counts_toward_the_safe_curve(self):
        a = self.make(lambda: True, ok=False)
        for t in (0, 1, 2):
            a.lights(t)
        self.assertEqual(len(self.cmds), 1)                       # retried only after LED_RETRY_S
        self.assertEqual(a.fails, 0)
        a.lights(o1aio.LED_RETRY_S + 1)
        self.assertEqual(len(self.cmds), 2)
        self.assertEqual(a.fails, 0)
        self.assertFalse(a.disabled)


# ---- the gateway --------------------------------------------------------------------------------------------

def setUpModule():
    TG.setUpModule()


def tearDownModule():
    TG.tearDownModule()
    restore_quiet()


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.addCleanup(restore_quiet)

    def chat(self):
        before = len(TG.G["stub"].calls)
        st, data, _ = TG.call("POST", "/api/chat", {"model": "small:8b", "stream": False, "keep_alive": "3h",
                                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 200, data)
        return [c[2] for c in TG.G["stub"].calls[before:] if c[1] == "/api/chat"][-1]

    def test_at_night_a_model_is_kept_two_minutes_and_by_day_the_clients_ask_is_still_dropped(self):
        write_quiet({"enabled": True, "start": "00:00", "end": "23:59"})
        self.assertEqual(self.chat().get("keep_alive"), "120s")
        write_quiet({"enabled": False})
        self.assertNotIn("keep_alive", self.chat())

    def test_the_views_carry_quiet(self):
        write_quiet({"enabled": True, "start": "00:00", "end": "23:59", "sleep_minutes": 15})
        for path in ("/v1/sleep-config", "/v1/whoami"):
            st, data, _ = TG.call("GET", path)
            self.assertEqual(st, 200, data)
            q = json.loads(data)["quiet"]
            self.assertEqual((q["enabled"], q["start"], q["end"], q["now"], q["sleep_minutes"]),
                             (True, "00:00", "23:59", True, 15), path)

    def test_downloads_wait_for_the_morning_and_removals_do_not(self):
        write_quiet({"enabled": True, "start": "00:00", "end": "23:59"})
        st, data, _ = TG.call("POST", "/v1/models/apply",
                              {"plan": "recommended", "add": ["new:4b"], "remove": [], "seen": "0" * 64})
        self.assertEqual(st, 409, data)
        self.assertEqual(json.loads(data)["code"], "quiet_hours")
        self.assertIn("until 23:59", json.loads(data)["error"])
        st, data, _ = TG.call("POST", "/v1/models/apply",
                              {"plan": "recommended", "add": [], "remove": ["x:1b"], "seen": "0" * 64})
        self.assertNotEqual(json.loads(data).get("code"), "quiet_hours")


# ---- the afternoon timings and the retired unit -------------------------------------------------------------

class TestTimings(unittest.TestCase):
    def test_the_update_reboot_and_the_ollama_update_are_in_the_afternoon(self):
        uu = open(os.path.join(U.CONFIG, "52ollama1-unattended-upgrades")).read()
        self.assertIn('Unattended-Upgrade::Automatic-Reboot-Time "15:00";', uu)
        self.assertNotIn('"04:00"', uu)
        timer = open(os.path.join(U.SYSTEMD, "ollama1-update-ollama.timer")).read()
        self.assertIn("OnCalendar=Sun *-*-* 15:30:00", timer)
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('= "15:00" ] || die "unattended-upgrades did not pick up the reboot time"', setup)

    def test_setup_retires_the_hand_made_cpu_unit_with_a_copy_and_only_resets_boosting_drivers(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        block = setup[setup.index("if [ -f /etc/systemd/system/ollama1-cpu-perf.service ]"):]
        block = block[:block.index("\nfi\n")]
        cp = block.index("cp -p /etc/systemd/system/ollama1-cpu-perf.service /var/backups/ollama1/")
        self.assertLess(cp, block.index("rm -f /etc/systemd/system/ollama1-cpu-perf.service"))
        self.assertRegex(block, r"amd-pstate-epp\|intel_pstate\)")
        self.assertNotIn("acpi-cpufreq", block)

    def test_nothing_here_touches_the_network_ssh_the_tunnel_or_the_firewall(self):
        src = open(os.path.join(U.LIB, "o1quiet.py")).read()
        for word in ("cloudflared", "sshd", "nft", "ufw", "netplan", "systemctl", "subprocess"):
            self.assertNotIn(word, re.sub(r'""".*?"""', "", src, flags=re.S), word)



# ---- the installer: backup, install, automatic rollback, --uninstall -----------------------------------------

class TestInstaller(unittest.TestCase):
    SCRIPT = os.path.join(U.TOOLS, "install-quiet.sh")

    def setUp(self):
        import subprocess
        self.sp = subprocess
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1qi-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "root")
        self.bin = os.path.join(self.dir, "bin")
        self.calls = os.path.join(self.dir, "calls")
        os.makedirs(self.bin)
        for rel, text in (("usr/local/lib/ollama1/lib/o1fan.py", "OLD FAN\n"),
                          ("usr/local/lib/ollama1/bin/ollama1-gateway", "OLD GATEWAY\n"),
                          ("etc/systemd/system/ollama1-update-ollama.timer", "OnCalendar=Sun *-*-* 03:30:00\n"),
                          ("etc/apt/apt.conf.d/52ollama1-unattended-upgrades", 'Automatic-Reboot-Time "04:00";\n'),
                          ("etc/systemd/system/ollama1-cpu-perf.service", "[Service]\nExecStart=/bin/true\n")):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        self.fake(down="")

    def fake(self, down):
        """A systemctl that logs its calls; `down` names a unit that never becomes active."""
        with open(os.path.join(self.bin, "systemctl"), "w") as f:
            f.write('#!/bin/sh\necho "$*" >>"$FAKE_CALLS"\n'
                    'if [ "$1" = is-active ] && [ -n "$FAKE_DOWN" ] && [ "$3" = "$FAKE_DOWN" ]; then exit 3; fi\n'
                    'exit 0\n')
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        self.down = down

    def run_it(self, *args):
        env = dict(os.environ, O1_QUIET_TESTROOT=self.root, FAKE_CALLS=self.calls, FAKE_DOWN=self.down,
                   PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        return self.sp.run(["bash", self.SCRIPT] + list(args), env=env, capture_output=True, text=True, timeout=120)

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def calls_made(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_it_installs_keeps_a_copy_retires_the_cpu_unit_and_touches_nothing_else(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("o1quiet.py", os.listdir(os.path.join(self.root, "usr/local/lib/ollama1/lib")))
        self.assertNotEqual(self.read("usr/local/lib/ollama1/lib/o1fan.py"), "OLD FAN\n")
        self.assertIn("15:30:00", self.read("etc/systemd/system/ollama1-update-ollama.timer"))
        self.assertIn('"15:00"', self.read("etc/apt/apt.conf.d/52ollama1-unattended-upgrades"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/systemd/system/ollama1-cpu-perf.service")))
        backups = os.listdir(os.path.join(self.root, "var/backups/ollama1"))
        self.assertIn("ollama1-cpu-perf.service.bak", backups)
        self.assertTrue(any(b.startswith("quiet-") and b.endswith(".tar") for b in backups))
        restarted = [c.split()[1] for c in self.calls_made() if c.startswith("restart ")]
        self.assertEqual(set(restarted), {"ollama1-gateway.service", "ollama1-admin.service", "ollama1-idle.service",
                                          "ollama1-fan.service", "ollama1-openrgb.service", "ollama1-leds.service",
                                          "ollama1-update-ollama.timer"})
        joined = " ".join(self.calls_made())
        for word in ("ssh", "tunnel", "cloudflared", "nft", "ufw", "networkd", "ollama.service", "suspend", "reboot"):
            self.assertNotIn(word, joined, word)

    def test_uninstall_puts_every_file_back(self):
        self.assertEqual(self.run_it().returncode, 0)
        r = self.run_it("--uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read("usr/local/lib/ollama1/lib/o1fan.py"), "OLD FAN\n")
        self.assertEqual(self.read("usr/local/lib/ollama1/bin/ollama1-gateway"), "OLD GATEWAY\n")
        self.assertIn("03:30:00", self.read("etc/systemd/system/ollama1-update-ollama.timer"))
        self.assertIn('"04:00"', self.read("etc/apt/apt.conf.d/52ollama1-unattended-upgrades"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc/systemd/system/ollama1-cpu-perf.service")))
        self.assertIn("enable ollama1-cpu-perf.service", self.calls_made())

    def test_a_service_that_does_not_come_back_rolls_everything_back_by_itself(self):
        self.fake(down="ollama1-fan.service")
        r = self.run_it()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ROLLING BACK", r.stdout)
        self.assertEqual(self.read("usr/local/lib/ollama1/lib/o1fan.py"), "OLD FAN\n")
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc/systemd/system/ollama1-cpu-perf.service")))

    def test_uninstall_without_a_backup_changes_nothing(self):
        r = self.run_it("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was changed", r.stdout)
        self.assertEqual(self.read("usr/local/lib/ollama1/lib/o1fan.py"), "OLD FAN\n")

if __name__ == "__main__":
    unittest.main()
