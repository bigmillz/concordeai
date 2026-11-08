"""ollama1-netwatch (6b524): the escalation state machine on a fake clock (decide() is pure), whole runs against a
fake machine (fake probes, a fake command runner, a fake sysfs and /proc), the repairs of its own half-done steps,
the reboot rules, the tunnel, the pause file, and that it never runs a command that changes a setting."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import o1netwatch as N

BID = "0123456789abcdef" * 2           # boot ids, made at run time (the repo scan flags a 32-hex literal)
BID2 = "fedcba9876543210" * 2
MIN = 60


def obs(mono, online=False, wall=None, boot_id=BID, tunnel="up", backend="nm", radio=True, connection="home",
        driver_reload=False, driver_ok=True, paused=False, guard=False, trial=False, busy=(), pending_bound=None):
    o = {"boot_id": boot_id, "mono": mono, "wall": 1_800_000_000 + mono if wall is None else wall, "online": online,
         "probes": {"gateway": {"ip": "192.0.2.1", "ok": online}, "1.1.1.1": {"ok": online}, "8.8.8.8": {"ok": online}},
         "tunnel": tunnel if online else "unknown (offline)",
         "wifi": {"iface": "wlan0", "backend": backend, "connection": connection if online else None, "radio": radio},
         "cfg": {"driver_reload": driver_reload},
         "driver": ({"ok": True, "pci": "0000:05:00.0", "driver": "mt7921e", "bound": True} if driver_ok
                    else {"ok": False, "why": "no driver is bound"}),
         "paused": paused, "guard_tripped": guard, "trial": trial, "busy": list(busy)}
    if pending_bound is not None:
        o["driver"]["pending_bound"] = pending_bound
    return o


def walk(start_mono, minutes, st=None, **kw):
    """Run decide() once a minute for `minutes` minutes offline; [(minute, action)] and the last state."""
    st = st or {}
    out = []
    for m in range(minutes + 1):
        st, acts, held = N.decide(st, obs(start_mono + m * MIN, **kw))
        out += [(m, a["do"]) for a in acts]
    return out, st


class TestLadder(unittest.TestCase):
    BOOTED = 3 * 3600            # well past the 30 min after boot

    def test_order_and_times(self):
        acts, st = walk(self.BOOTED, 25)
        self.assertEqual(acts, [(3, "reconnect"), (6, "radio"), (8, "reconnect"), (13, "reconnect"), (18, "reconnect"),
                                (20, "reboot"), (21, "radio"), (23, "reconnect")])   # until the reboot comes
        self.assertEqual(st["reboots_since_online"], 1)
        self.assertEqual(st["last_reboot_wall"], 1_800_000_000 + self.BOOTED + 20 * MIN)

    def test_driver_step_only_when_asked_and_identified(self):
        acts, _ = walk(self.BOOTED, 21, driver_reload=True)
        self.assertEqual([a for a in acts if a[1] in ("driver", "reboot")], [(10, "driver"), (20, "reboot")])
        acts, _ = walk(self.BOOTED, 21, driver_reload=True, driver_ok=False)
        self.assertNotIn("driver", [a for _, a in acts])
        acts, _ = walk(self.BOOTED, 21)
        self.assertNotIn("driver", [a for _, a in acts])

    def test_driver_action_names_the_checked_device(self):
        st = {}
        for m in range(11):
            st, acts, _ = N.decide(st, obs(self.BOOTED + m * MIN, driver_reload=True))
        self.assertEqual(acts, [{"do": "driver", "why": "offline for 10 min", "pci": "0000:05:00.0", "driver": "mt7921e"}])

    def test_steps_in_order_even_after_a_gap(self):
        """A machine that ran no check for 30 min (stalled) does not jump straight to a reboot."""
        st, _, _ = N.decide({}, obs(self.BOOTED))
        seq = []
        for m in (30, 31, 32, 33):
            st, acts, _ = N.decide(st, obs(self.BOOTED + m * MIN))
            seq += [a["do"] for a in acts]
        self.assertEqual(seq, ["reconnect", "radio", "reboot"])

    def test_online_resets_the_episode(self):
        acts, st = walk(self.BOOTED, 7)
        self.assertEqual([a for _, a in acts], ["reconnect", "radio"])
        st, acts, _ = N.decide(st, obs(self.BOOTED + 8 * MIN, online=True))
        self.assertEqual(acts, [])
        self.assertIsNone(st["offline_since"])
        self.assertEqual(st["done"], {})
        self.assertEqual(st["back_online_after_s"], 8 * MIN)
        acts, _ = walk(self.BOOTED + 9 * MIN, 4, st=st)
        self.assertEqual(acts, [(3, "reconnect")])          # a new episode starts from the bottom

    def test_isp_down_lan_up_is_online(self):
        o = obs(self.BOOTED + 30 * MIN, online=True)
        o["probes"]["1.1.1.1"]["ok"] = o["probes"]["8.8.8.8"]["ok"] = False
        st, acts, _ = N.decide({"boot_id": BID, "offline_since": self.BOOTED}, o)
        self.assertEqual(acts, [])

    def test_soft_steps_repeat_while_the_reboot_is_held(self):
        acts, _ = walk(0, 29)                         # booted just now: no reboot before 30 min of uptime
        names = [a for _, a in acts]
        self.assertNotIn("reboot", names)
        self.assertEqual(acts, [(3, "reconnect"), (6, "radio"), (8, "reconnect"), (13, "reconnect"),
                                (18, "reconnect"), (21, "radio"), (23, "reconnect"), (28, "reconnect")])
        acts, _ = walk(0, 31)
        self.assertEqual(acts[-1], (30, "reboot"))


class TestRebootRules(unittest.TestCase):
    def due(self, st, mono, **kw):
        """Offline for 25 min at `mono`, every softer step done: does a reboot come?"""
        st = dict(st, boot_id=kw.get("boot_id", BID), offline_since=mono - 25 * MIN,
                  done={"reconnect": mono - 60, "radio": mono - 60})
        st, acts, held = N.decide(st, obs(mono, **kw))
        return [a["do"] for a in acts], held

    def test_not_within_30_min_of_boot(self):
        self.assertEqual(self.due({}, 29 * MIN), ([], "reboot held: less than 30 min since boot"))
        self.assertEqual(self.due({}, 30 * MIN)[0], ["reboot"])

    def test_not_twice_in_two_hours(self):
        wall = 1_900_000_000
        st = {"last_reboot_wall": wall - 3600, "reboots_since_online": 0}
        acts, held = self.due(st, 45 * MIN, wall=wall)
        self.assertEqual(acts, [])
        self.assertIn("less than 2 h ago", held)
        self.assertEqual(self.due(st, 45 * MIN, wall=wall + 3601)[0], ["reboot"])

    def test_uptime_proves_the_spacing_whatever_the_clock(self):
        """A clock that went back (a dead clock battery, no NTP offline): the last reboot looks like the future."""
        st = {"last_reboot_wall": 1_900_000_000, "reboots_since_online": 1}
        self.assertEqual(self.due(st, 45 * MIN, wall=1_000_000_000)[0], [])
        self.assertEqual(self.due(st, 2 * 3600, wall=1_000_000_000)[0], ["reboot"])

    def test_backoff_after_reboots_that_did_not_help(self):
        self.assertEqual([N.spacing(n) // 3600 for n in range(7)], [2, 2, 4, 8, 12, 12, 12])
        wall = 1_900_000_000
        st = {"last_reboot_wall": wall - 3 * 3600, "reboots_since_online": 2}     # two reboots, still offline
        acts, held = self.due(st, 45 * MIN, wall=wall)
        self.assertEqual(acts, [])
        self.assertIn("less than 4 h ago", held)
        self.assertEqual(self.due(st, 45 * MIN, wall=wall + 3600)[0], ["reboot"])

    def test_online_clears_the_backoff(self):
        st = {"boot_id": BID, "reboots_since_online": 3, "last_reboot_wall": 1}
        st, _, _ = N.decide(st, obs(5000, online=True))
        self.assertEqual(st["reboots_since_online"], 0)
        self.assertEqual(st["last_reboot_wall"], 1)          # "never twice in 2 h" still counts it

    def test_never_after_the_boot_loop_guard_tripped(self):
        acts, held = self.due({}, 5 * 3600, guard=True)
        self.assertEqual(acts, [])
        self.assertIn("boot-loop guard", held)

    def test_never_during_the_trial(self):
        acts, held = self.due({}, 5 * 3600, trial=True)
        self.assertEqual(acts, [])
        self.assertIn("trial", held)

    def test_not_while_busy(self):
        acts, held = self.due({}, 5 * 3600, busy=["setup.sh is running"])
        self.assertEqual(acts, [])
        self.assertEqual(held, "reboot held: busy: setup.sh is running")

    def test_a_new_boot_keeps_the_reboot_record_only(self):
        st = {"boot_id": BID, "offline_since": 10, "done": {"reconnect": 20}, "last_reboot_wall": 5,
              "last_reboot_boot": BID, "last_reboot_mono": 4, "reboots_since_online": 2, "wifi_connection": "home",
              "tunnel_down_since": 3, "radio_pending": True, "unbound": {"pci": "0000:05:00.0", "driver": "mt7921e"}}
        n = N.new_boot(st, BID2)
        self.assertEqual(n, {"boot_id": BID2, "last_reboot_wall": 5, "last_reboot_boot": BID, "reboots_since_online": 2,
                             "wifi_connection": "home", "radio_pending": True})

    def test_a_reboot_that_did_not_happen_is_not_asked_again_at_once(self):
        """Asked for in this boot and the server is still up: uptime proves nothing about it."""
        acts, st = walk(5 * 3600, 20)
        self.assertEqual(acts[-1], (20, "reboot"))
        acts2, _ = walk(5 * 3600 + 22 * MIN, 60, st=st)
        self.assertNotIn("reboot", [a for _, a in acts2])
        acts3, _ = walk(5 * 3600 + 20 * MIN + 2 * 3600, 1, st=st)
        self.assertIn("reboot", [a for _, a in acts3])


class TestTunnel(unittest.TestCase):
    def test_restart_after_ten_minutes_then_every_thirty(self):
        st, out = {}, []
        for m in range(0, 75):
            st, acts, _ = N.decide(st, obs(10_000 + m * MIN, online=True, tunnel="down"))
            out += [m for a in acts if a["do"] == "tunnel_restart"]
        self.assertEqual(out, [10, 40, 70])

    def test_not_counted_while_offline(self):
        st = {}
        for m in range(0, 8):
            st, _, _ = N.decide(st, obs(10_000 + m * MIN, online=True, tunnel="down"))
        st, _, _ = N.decide(st, obs(10_000 + 8 * MIN))                  # offline a minute: the count starts again
        hits = []
        for m in range(9, 25):
            st, acts, _ = N.decide(st, obs(10_000 + m * MIN, online=True, tunnel="down"))
            hits += [m for a in acts if a["do"] == "tunnel_restart"]
        self.assertEqual(hits, [19])

    def test_off_or_up_is_left_alone(self):
        for t in ("up", "off"):
            st = {}
            for m in range(0, 40):
                st, acts, _ = N.decide(st, obs(10_000 + m * MIN, online=True, tunnel=t))
                self.assertEqual(acts, [])


class TestRepairsAndPause(unittest.TestCase):
    def test_pause_means_status_only(self):
        st = {}
        for m in range(0, 60):
            st, acts, held = N.decide(st, obs(5 * 3600 + m * MIN, paused=True))
            self.assertEqual(acts, [])
        self.assertIn("netwatch.off", held)
        for m in range(0, 40):
            st, acts, _ = N.decide(st, obs(5 * 3600 + m * MIN, online=True, tunnel="down", paused=True))
            self.assertEqual(acts, [])

    def test_its_own_radio_is_switched_on_again_even_paused(self):
        st, acts, _ = N.decide({"boot_id": BID, "radio_pending": True}, obs(5000, online=True, radio=False, paused=True))
        self.assertEqual([a["do"] for a in acts], ["radio_on"])

    def test_a_radio_found_off_while_offline(self):
        st, acts, _ = N.decide({}, obs(5000, radio=False))
        self.assertEqual([a["do"] for a in acts], ["radio_on"])
        _, acts, _ = N.decide({}, obs(5000, online=True, radio=False))     # online: someone's choice, left alone
        self.assertEqual(acts, [])
        _, acts, _ = N.decide({}, obs(5000, radio=False, paused=True))
        self.assertEqual(acts, [])
        _, acts, _ = N.decide({}, obs(5000, radio=False, backend="networkd"))   # a link down is not a radio
        self.assertEqual(acts, [])

    def test_its_own_unbound_card_is_bound_again(self):
        st = {"boot_id": BID, "unbound": {"pci": "0000:05:00.0", "driver": "mt7921e"}}
        _, acts, _ = N.decide(st, obs(5000, online=True, pending_bound=False, paused=True))
        self.assertEqual(acts, [{"do": "driver_bind", "pci": "0000:05:00.0", "driver": "mt7921e",
                                 "why": "the card was left without its driver"}])
        st2, acts, _ = N.decide(st, obs(5000, online=True, pending_bound=True))
        self.assertEqual(acts, [])
        self.assertIsNone(st2["unbound"])


class FakeRun:
    """Answers the read-only commands like a NetworkManager machine; records every argv."""

    def __init__(self, devices="wlan0:wifi:connected\nlo:loopback:unmanaged\n", active="home:802-11-wireless:wlan0\n",
                 radio="enabled", tunnel_enabled="enabled", tunnel_active="active", fail=()):
        self.calls = []
        self.devices, self.active, self.radio = devices, active, radio
        self.tunnel_enabled, self.tunnel_active = tunnel_enabled, tunnel_active
        self.fail = set(fail)
        self.ping_ok = False

    def __call__(self, argv, timeout=30):
        self.calls.append(list(argv))
        key = " ".join(argv)
        for f in self.fail:
            if key.startswith(f):
                return 1, ""
        if argv[0] == "ping":
            return (0 if self.ping_ok else 1), ""
        if argv[:2] == ["nmcli", "-t"] and "device" in argv:
            return 0, self.devices
        if argv[:2] == ["nmcli", "-t"] and "--active" in argv:
            return 0, self.active
        if argv[:2] == ["nmcli", "-t"]:
            return 0, "home:802-11-wireless\nwired:802-3-ethernet\n"
        if argv == ["nmcli", "radio", "wifi"]:
            return 0, self.radio + "\n"
        if argv[:2] == ["systemctl", "is-enabled"]:
            return 0, self.tunnel_enabled + "\n"
        if argv[:2] == ["systemctl", "is-active"]:
            return 0, self.tunnel_active + "\n"
        return 0, ""

    def changing(self):
        """The calls that are not one of the read-only questions."""
        ro = (["ping"], ["nmcli", "-t"], ["nmcli", "radio", "wifi"], ["systemctl", "is-enabled"],
              ["systemctl", "is-active"], ["networkctl", "--no-pager"])
        return [c for c in self.calls if not any(c[:len(r)] == r and (r != ["nmcli", "radio", "wifi"] or len(c) == 3)
                                                 for r in ro)]


class Machine:
    """A fake /proc and /sys under a temp folder, and OLLAMA1_PREFIX for /etc/ollama1 and /var/lib/ollama1."""

    def __init__(self, tc):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1nw-"))
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.proc = os.path.join(self.dir, "proc")
        self.sys = os.path.join(self.dir, "sys")
        os.makedirs(os.path.join(self.proc, "sys", "kernel", "random"))
        os.makedirs(os.path.join(self.proc, "net"))
        self.write("proc/sys/kernel/random/boot_id", BID + "\n")
        self.route("wlan0", "0102000C")           # 12.0.2.1, little-endian hex below
        self.card()
        for k, v in (("OLLAMA1_PROC", self.proc), ("OLLAMA1_SYS", self.sys)):
            old = os.environ.get(k)
            os.environ[k] = v
            tc.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old is not None else os.environ.pop(k, None))
        for f in (N.STATE, N.PAUSE, N.TRIAL, N.BOOTGUARD, N.ACTIVITY, "/etc/ollama1/config.json"):
            try:
                os.unlink(N.p(f))
            except FileNotFoundError:
                pass

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def route(self, iface, gw_hex="0102000C"):
        # 192.0.2.1 is C0.00.02.01; /proc/net/route writes it little-endian: 010200C0
        self.write("proc/net/route", "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                   "%s\t00000000\t010200C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
                   "%s\t000200C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n" % (iface, iface))

    def card(self, pci="0000:05:00.0", driver="mt7921e", module="mt7921e", extra_net=()):
        dev = os.path.join(self.sys, "devices", "pci0000:00", "0000:00:01.2", pci)
        os.makedirs(os.path.join(dev, "net", "wlan0"), exist_ok=True)
        for n in extra_net:
            os.makedirs(os.path.join(dev, "net", n), exist_ok=True)
        os.makedirs(os.path.join(self.sys, "bus", "pci", "drivers", driver), exist_ok=True)
        os.makedirs(os.path.join(self.sys, "module", module), exist_ok=True)
        os.makedirs(os.path.join(self.sys, "bus", "pci", "devices"), exist_ok=True)
        for link, target in ((os.path.join(dev, "subsystem"), os.path.join(self.sys, "bus", "pci")),
                             (os.path.join(dev, "driver"), os.path.join(self.sys, "bus", "pci", "drivers", driver)),
                             (os.path.join(self.sys, "bus", "pci", "drivers", driver, "module"),
                              os.path.join(self.sys, "module", module)),
                             (os.path.join(self.sys, "bus", "pci", "devices", pci), dev)):
            if not os.path.lexists(link):
                os.symlink(target, link)
        net = os.path.join(self.sys, "class", "net", "wlan0")
        os.makedirs(os.path.join(net, "wireless"), exist_ok=True)
        if not os.path.lexists(os.path.join(net, "device")):
            os.symlink(dev, os.path.join(net, "device"))
        self.pci_dev = dev
        return dev


def system(run, online=False, clock=None, wall=None, http=None):
    t = {"mono": 5 * 3600.0, "wall": 1_900_000_000.0}
    s = N.System(run=run, connect=lambda ip, port, timeout=3.0: online, http=http or (lambda url, timeout=3.0: 200),
                 clock=clock or (lambda: t["mono"]), wall=wall or (lambda: t["wall"]))
    return s, t


class TestRuns(unittest.TestCase):
    """Whole runs, a fresh 'process' each minute (state only through the file), against the fake machine."""

    def setUp(self):
        self.m = Machine(self)
        self.lines = []

    def runs(self, run, minutes, online=False, start=5 * 3600.0):
        s, t = system(run, online=online)
        t["mono"] = start
        out = []
        for _ in range(minutes):
            st = N.run_once(s, log=self.lines.append, sleep=lambda x: None)
            out.append(st)
            t["mono"] += 60
            t["wall"] += 60
        return out

    def test_gateway_from_proc(self):
        self.assertEqual(N.parse_route(open(os.path.join(self.m.proc, "net", "route")).read()), ("wlan0", "192.0.2.1"))
        self.assertEqual(N.parse_route("Iface\tDestination\n"), (None, None))

    def test_offline_escalation_end_to_end(self):
        run = FakeRun()
        states = self.runs(run, 21)
        ch = run.changing()
        self.assertEqual(ch, [["nmcli", "-w", "45", "connection", "up", "id", "home"],
                              ["nmcli", "radio", "wifi", "off"], ["nmcli", "radio", "wifi", "on"],
                              ["nmcli", "-w", "45", "connection", "up", "id", "home"],
                              ["nmcli", "-w", "45", "connection", "up", "id", "home"],
                              ["nmcli", "-w", "45", "connection", "up", "id", "home"],
                              ["systemctl", "start", "--no-block", "ollama1-reboot.service"]])
        st = json.load(open(N.p(N.STATE)))
        self.assertEqual([h["do"] for h in st["history"]], ["reconnect", "radio", "reconnect", "reconnect", "reconnect", "reboot"])
        self.assertTrue(all(h["ok"] for h in st["history"]))
        self.assertEqual(st["status"]["online"], False)
        self.assertEqual(st["status"]["tunnel"], "unknown (offline)")
        self.assertFalse(st["radio_pending"])
        self.assertTrue(any("OFFLINE" in l for l in self.lines))
        self.assertTrue(any(l.startswith("netwatch: reboot (offline for 20 min)") for l in self.lines))
        self.assertEqual(oct(os.stat(N.p(N.STATE)).st_mode & 0o777), "0o640")

    def test_the_connection_remembered_from_online(self):
        run = FakeRun()
        self.runs(run, 1, online=True)
        run.active = ""                               # offline: NetworkManager has no active Wi-Fi connection now
        s, t = system(run)
        for i in range(4):
            t["mono"] = 6 * 3600 + i * 60
            N.run_once(s, log=self.lines.append, sleep=lambda x: None)
        self.assertIn(["nmcli", "-w", "45", "connection", "up", "id", "home"], run.changing())

    def test_reboot_falls_back_to_systemctl_reboot(self):
        run = FakeRun(fail=("systemctl start --no-block ollama1-reboot.service",))
        self.runs(run, 21)
        self.assertEqual(run.changing()[-2:], [["systemctl", "start", "--no-block", "ollama1-reboot.service"],
                                               ["systemctl", "reboot"]])

    def test_a_reboot_is_saved_before_it_is_asked(self):
        run = FakeRun()
        seen = {}

        def spy(argv, timeout=30):
            if argv[:3] == ["systemctl", "start", "--no-block"]:
                seen["state"] = json.load(open(N.p(N.STATE)))
            return run(argv, timeout)
        self.runs(spy, 21)
        self.assertEqual(seen["state"]["reboots_since_online"], 1)
        self.assertIn("reboot", seen["state"]["done"])

    def test_radio_killed_half_way_is_repaired_next_run(self):
        class Killed(Exception):
            pass
        run = FakeRun()

        def dies(argv, timeout=30):
            if argv == ["nmcli", "radio", "wifi", "on"]:
                raise Killed()
            return run(argv, timeout)
        s, t = system(dies)
        with self.assertRaises(Killed):
            for i in range(8):
                t["mono"] = 5 * 3600 + i * 60
                N.run_once(s, log=self.lines.append, sleep=lambda x: None)
        self.assertTrue(json.load(open(N.p(N.STATE)))["radio_pending"])
        run.radio = "disabled"
        s, t = system(run, online=True)
        N.run_once(s, log=self.lines.append, sleep=lambda x: None)
        self.assertEqual(run.changing()[-1], ["nmcli", "radio", "wifi", "on"])
        self.assertFalse(json.load(open(N.p(N.STATE)))["radio_pending"])

    def test_pause_file(self):
        os.makedirs(os.path.dirname(N.p(N.PAUSE)), exist_ok=True)
        open(N.p(N.PAUSE), "w").close()
        run = FakeRun()
        states = self.runs(run, 40)
        self.assertEqual(run.changing(), [])
        self.assertTrue(states[-1]["status"]["paused"])

    def test_tunnel_restart_end_to_end(self):
        run = FakeRun(tunnel_active="activating")
        self.runs(run, 12, online=True)
        self.assertEqual(run.changing(), [["systemctl", "restart", "ollama1-tunnel.service"]])

    def test_tunnel_ready_endpoint(self):
        run = FakeRun()
        for code, want in ((200, "up"), (503, "down"), (None, "up")):
            s, _ = system(run, online=True, http=lambda url, timeout=3.0, c=code: c)
            self.assertEqual(s.tunnel(), want)
        s, _ = system(FakeRun(tunnel_enabled="disabled"), online=True)
        self.assertEqual(s.tunnel(), "off")
        self.assertEqual(N.metrics_port("tunnel: x\nmetrics: 127.0.0.1:9123\n"), 9123)
        self.assertEqual(N.metrics_port("metrics: 0.0.0.0:9123\n"), N.METRICS_DEFAULT)

    def test_networkd_backend(self):
        run = FakeRun(devices="wlan0:wifi:unmanaged\n")
        unit = N.p("/run/systemd/system/netplan-wpa-wlan0.service")
        os.makedirs(os.path.dirname(unit), exist_ok=True)
        open(unit, "w").close()
        self.addCleanup(os.unlink, unit)
        self.m.write("sys/class/net/wlan0/flags", "0x1003\n")
        self.runs(run, 7)
        self.assertEqual(run.changing(), [["networkctl", "reconfigure", "wlan0"],
                                          ["systemctl", "restart", "netplan-wpa-wlan0.service"],
                                          ["ip", "link", "set", "dev", "wlan0", "down"],
                                          ["ip", "link", "set", "dev", "wlan0", "up"],
                                          ["networkctl", "reconfigure", "wlan0"]])

    def test_driver_rebind_through_sysfs(self):
        os.makedirs(os.path.dirname(N.p(N.CONFIG)), exist_ok=True)
        with open(N.p(N.CONFIG), "w") as f:
            json.dump({"netwatch_driver_reload": True}, f)
        run = FakeRun()
        self.runs(run, 11)
        drv = os.path.join(self.m.sys, "bus", "pci", "drivers", "mt7921e")
        self.assertEqual(open(os.path.join(drv, "unbind")).read(), "0000:05:00.0")
        self.assertEqual(open(os.path.join(drv, "bind")).read(), "0000:05:00.0")
        st = json.load(open(N.p(N.STATE)))
        self.assertEqual(st["history"][-1]["do"], "driver")
        self.assertIsNone(st["unbound"])

    def test_driver_identification_refusals(self):
        s, _ = system(FakeRun())
        d = s.driver("wlan0")
        self.assertEqual((d["ok"], d["pci"], d["driver"], d["module"]), (True, "0000:05:00.0", "mt7921e", "mt7921e"))
        self.assertEqual(s.driver(None)["ok"], False)
        self.m.card(extra_net=("wlan1",))
        self.assertIn("also carries wlan1", s.driver("wlan0")["why"])

    def test_driver_identification_not_pci(self):
        m = self.m
        dev = os.path.join(m.sys, "devices", "platform", "usbwifi")
        os.makedirs(dev)
        link = os.path.join(m.sys, "class", "net", "wlan0", "device")
        os.unlink(link)
        os.symlink(dev, link)
        s, _ = system(FakeRun())
        self.assertIn("not a PCI device", s.driver("wlan0")["why"])

    def test_unbound_card_repaired(self):
        run = FakeRun()
        os.makedirs(os.path.dirname(N.p(N.STATE)), exist_ok=True)
        with open(N.p(N.STATE), "w") as f:
            json.dump({"boot_id": BID, "unbound": {"pci": "0000:05:00.0", "driver": "mt7921e"}}, f)
        os.unlink(os.path.join(self.m.pci_dev, "driver"))
        self.assertEqual(N.repair(system(run)[0], log=self.lines.append), 0)
        drv = os.path.join(self.m.sys, "bus", "pci", "drivers", "mt7921e")
        self.assertEqual(open(os.path.join(drv, "bind")).read(), "0000:05:00.0")

    def test_busy_reasons(self):
        import time
        os.makedirs(os.path.dirname(N.p(N.ACTIVITY)), exist_ok=True)
        with open(N.p(N.ACTIVITY), "w") as f:
            json.dump({"inflight": 2, "at": time.time(), "last": time.time()}, f)
        self.assertEqual(N.busy_reasons(), ["the gateway is serving 2 requests"])
        with open(N.p(N.ACTIVITY), "w") as f:
            json.dump({"inflight": 2, "at": time.time() - 600}, f)       # stale: says nothing
        self.assertEqual(N.busy_reasons(), [])
        os.makedirs(os.path.join(self.m.proc, "4242"))
        self.m.write("proc/4242/comm", "dpkg\n")
        self.assertEqual(N.busy_reasons(), ["packages are being installed (dpkg)"])

    def test_guard_and_trial_files_are_read(self):
        os.makedirs(os.path.dirname(N.p(N.BOOTGUARD)), exist_ok=True)
        with open(N.p(N.BOOTGUARD), "w") as f:
            json.dump({"tripped_boot": BID}, f)
        s, _ = system(FakeRun())
        o = s.observe({})
        self.assertTrue(o["guard_tripped"])
        self.assertFalse(o["trial"])
        with open(N.p(N.BOOTGUARD), "w") as f:
            json.dump({"tripped_boot": BID2}, f)
        self.assertFalse(s.observe({})["guard_tripped"])

    def test_status_renders(self):
        run = FakeRun()
        self.runs(run, 5)
        text = N.render_status(json.load(open(N.p(N.STATE))))
        self.assertIn("Network watch: OFFLINE for", text)
        self.assertIn("gateway      192.0.2.1 no answer", text)
        self.assertIn("reconnect", text)


class TestNeverAConfigChange(unittest.TestCase):
    def test_the_source(self):
        src = open(os.path.join(U.LIB, "o1netwatch.py")).read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        code = re.sub(r'(?s)""".*?"""', "", code)
        for word in ("netplan apply", "netplan set", "connection modify", "connection add", "connection delete",
                     '"modify"', '"add"', '"delete"', '"device", "connect"', "ufw", "nft ", "sshd", "iptables",
                     "cloudflared.yml\", \"w", "rfkill", "modprobe", "rmmod", "/etc/netplan", "NetworkManager/system-connections"):
            self.assertNotIn(word, code, word)
        # every command that can change anything is one of these
        argvs = re.findall(r'\["(nmcli|networkctl|ip|systemctl)", (.*?)\](?=,\s*\d+\s*\))', code)
        allowed = {
            ("nmcli", '"-w", "45", "connection", "up", "id", name'), ("nmcli", '"radio", "wifi", "off"'),
            ("nmcli", '"radio", "wifi", "on"'), ("networkctl", '"reconfigure", iface'),
            ("networkctl", '"reconfigure", w["iface"]'), ("ip", '"link", "set", "dev", w["iface"], "down"'),
            ("ip", '"link", "set", "dev", w["iface"], "up"'), ("systemctl", '"restart", unit'),
            ("systemctl", '"restart", TUNNEL_UNIT'), ("systemctl", '"start", "--no-block", REBOOT_UNIT'),
            ("systemctl", '"reboot"'),
            # read-only ones
            ("nmcli", '"-t", "-f", "DEVICE,TYPE,STATE", "device", "status"'),
            ("nmcli", '"-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"'),
            ("nmcli", '"-t", "-f", "NAME,TYPE", "connection", "show"'), ("nmcli", '"radio", "wifi"'),
            ("networkctl", '"--no-pager", "--no-legend", "list"'), ("systemctl", '"is-enabled", TUNNEL_UNIT'),
            ("systemctl", '"is-active", TUNNEL_UNIT'),
        }
        self.assertEqual(set(argvs) - allowed, set())
        self.assertIn('"restart", unit', code)
        self.assertIn('unit = "netplan-wpa-%s.service" % iface', code)    # the only unit restarted by name

    def test_units(self):
        unit = open(os.path.join(U.SYSTEMD, "ollama1-netwatch.service")).read()
        for line in ("Type=oneshot\n", "ExecStart=/usr/local/lib/ollama1/bin/ollama1-netwatch run\n",
                     "ReadWritePaths=-/var/lib/ollama1\n", "ProtectSystem=strict\n", "NoNewPrivileges=yes\n"):
            self.assertIn(line, unit)
        for bad in ("\nPrivateTmp=yes", "\nProtectKernelTunables=yes", "\nPrivateNetwork=yes", "[Install]"):
            self.assertNotIn(bad, unit)
        timer = open(os.path.join(U.SYSTEMD, "ollama1-netwatch.timer")).read()
        for line in ("OnBootSec=2min\n", "OnUnitActiveSec=1min\n", "WantedBy=timers.target"):
            self.assertIn(line, timer)

    def test_commands(self):
        env = dict(os.environ)
        for args, rc, want in ((["status"], 0, "Network watch"), (["--help"], 0, "netwatch.off"),
                               (["nonsense"], 1, "ollama1-netwatch")):
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-netwatch")] + args, env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, rc, r.stderr)
            self.assertIn(want, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
