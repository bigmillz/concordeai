"""ollama1-watchdog-doctor (6b524): its parsing of sample kernel logs and journal text (the board's TCO lines, the
6b399 service's own lines, how a boot ended), its verdicts for each way the watchdog can fail to fire, and that it
only reads: a fake sysfs and /proc, a fake journalctl/systemctl that records every call."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import o1wddoctor as D

H = "2026-10-24T03:1%d:00+0000 testsrv "

TCO_OK = """2026-10-20T08:00:03+0000 testsrv kernel: sp5100_tco: SP5100/SB800 TCO WatchDog Timer Driver
2026-10-20T08:00:03+0000 testsrv kernel: sp5100-tco sp5100-tco: Using 0xfeb00000 for watchdog MMIO address
2026-10-20T08:00:03+0000 testsrv kernel: sp5100-tco sp5100-tco: initialized. heartbeat=60 sec (nowayout=0)
"""
TCO_DISABLED = """2026-10-20T08:00:03+0000 testsrv kernel: sp5100_tco: SP5100/SB800 TCO WatchDog Timer Driver
2026-10-20T08:00:03+0000 testsrv kernel: sp5100-tco sp5100-tco: Watchdog hardware is disabled
"""
TCO_NOMMIO = "2026-10-20T08:00:03+0000 testsrv kernel: sp5100-tco sp5100-tco: failed to find MMIO address, giving up.\n"
DID_NOT_STOP = "2026-10-20T09:00:00+0000 testsrv kernel: watchdog: watchdog0: watchdog did not stop!\n"

KERN_SUSPEND_FLOOD = "".join([
    H % 0 + "kernel: PM: suspend entry (s2idle)\n",
    H % 1 + "kernel: NVRM: nvAssertFailedNoLog: Assertion failed: 0 @ rs_server.c:1382\n" * 3,
    H % 2 + "kernel: NVRM: GPU0 _kgspIsBootloaderRunning: GPU_IN_FULLCHIP_RESET\n",
    H % 3 + "kernel: NVRM: nvAssertFailedNoLog: Assertion failed: 0 @ alloc_free.c:360\n" * 2,
])
KERN_RESUMED_FLOOD = KERN_SUSPEND_FLOOD + H % 4 + "kernel: PM: suspend exit\n" + \
    H % 5 + "kernel: NVRM: nvAssertFailedNoLog: Assertion failed: 0 @ rs_server.c:1382\n" + \
    H % 6 + "kernel: EXT4-fs warning: No space left on device\n" + \
    H % 7 + "kernel: watchdog: BUG: soft lockup - CPU#7 stuck for 23s! [nvidia-smi:4242]\n"
WD_ARMED = H % 0 + "ollama1-watchdog[812]: watchdog: armed /dev/watchdog0, timeout 60 s, pet every 10 s while the health probe passes\n"
WD_PAUSED = WD_ARMED + H % 1 + "ollama1-watchdog[812]: watchdog: closed cleanly for sleep\n"
WD_RESUMED = WD_PAUSED + H % 2 + "ollama1-watchdog[812]: watchdog: resumed after sleep; waiting for the system disk to answer (at most 90 s)\n" + \
    H % 3 + "ollama1-watchdog[812]: watchdog: armed /dev/watchdog0, timeout 60 s, pet every 10 s while the health probe passes\n"
WD_TRIPPED = WD_ARMED + H % 4 + "ollama1-watchdog[812]: watchdog: probe failed (1 in a row): token: EIO: Input/output error\n" + \
    H % 4 + "ollama1-watchdog[812]: watchdog: probe failed (2 in a row): token: EIO: Input/output error\n" + \
    H % 4 + "ollama1-watchdog[812]: watchdog: stopped petting after 2 failed probes in a row (token: EIO); the board resets within the timeout\n"
TAIL_CLEAN = H % 9 + "systemd[1]: Reached target System Reboot.\n" + H % 9 + "systemd-journald[400]: Journal stopped\n"
TAIL_ABRUPT = H % 9 + "ollama1-gateway[900]: served 1 request\n" + H % 9 + "kernel: NVRM: nvAssertFailedNoLog: x\n"
TAIL_SHUTDOWN = H % 9 + "systemd[1]: Stopped target Multi-User System.\n" + H % 9 + "systemd[1]: Stopping ollama1-watchdog.service\n"


class TestParsing(unittest.TestCase):
    def test_tco_lines(self):
        t = D.parse_tco(TCO_OK)
        self.assertEqual((t["seen"], t["disabled"], t["no_mmio"], t["initialized"]),
                         (True, False, False, {"heartbeat": 60, "nowayout": 0}))
        self.assertTrue(D.parse_tco(TCO_DISABLED)["disabled"])
        self.assertTrue(D.parse_tco(TCO_NOMMIO)["no_mmio"])
        self.assertEqual(D.parse_tco(TCO_OK + DID_NOT_STOP)["did_not_stop"], 1)
        self.assertEqual(D.parse_tco("")["seen"], False)
        self.assertIn("WATCHDOG HARDWARE IS DISABLED", D._tco_words(D.parse_tco(TCO_DISABLED)))
        self.assertIn("heartbeat 60 s, nowayout 0", D._tco_words(D.parse_tco(TCO_OK)))

    def test_the_service_lines(self):
        self.assertEqual(D.parse_watchdog_lines(WD_PAUSED)["paused_last"], True)
        r = D.parse_watchdog_lines(WD_RESUMED)
        self.assertEqual((r["armed"], r["paused_last"]), (True, False))
        r = D.parse_watchdog_lines(WD_TRIPPED)
        self.assertEqual((r["probe_failed"], r["tripped"]), (2, True))
        self.assertEqual(D.parse_watchdog_lines("")["armed"], False)

    def test_signs(self):
        c, last = D.count_signs(KERN_RESUMED_FLOOD)
        self.assertEqual((c["nvrm_assert"], c["gsp_reset"], c["suspend_entry"], c["suspend_exit"], c["no_space"],
                          c["soft_lockup"]), (6, 1, 1, 1, 1, 1))
        self.assertIn("rs_server.c:1382", last["nvrm_assert"])

    def test_how_it_ended(self):
        none = D.parse_watchdog_lines("")
        sus, _ = D.count_signs(KERN_SUSPEND_FLOOD)
        res, _ = D.count_signs(KERN_RESUMED_FLOOD)
        self.assertEqual(D.how_it_ended(TAIL_CLEAN, res, none)[0], "clean")
        self.assertEqual(D.how_it_ended(TAIL_ABRUPT, sus, none)[0], "suspend")
        self.assertEqual(D.how_it_ended(TAIL_ABRUPT, res, none)[0], "abrupt")
        self.assertEqual(D.how_it_ended(TAIL_SHUTDOWN, res, none)[0], "shutdown_hang")
        self.assertEqual(D.how_it_ended(TAIL_ABRUPT, res, D.parse_watchdog_lines(WD_TRIPPED))[0], "tripped")
        self.assertEqual(D.how_it_ended(TAIL_ABRUPT, dict(res, panic=1), none)[0], "panic")

    def test_boots(self):
        j = json.dumps([{"index": -1, "boot_id": "a" * 32, "first_entry": 1, "last_entry": 2},
                        {"index": 0, "boot_id": "b" * 32, "first_entry": 3, "last_entry": 4}])
        self.assertEqual(D.parse_boots(j), [{"idx": -1, "first": 1, "last": 2}, {"idx": 0, "first": 3, "last": 4}])
        text = ("IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n"
                " -1 %s Sat 2026-10-24 01:00:00 UTC Sat 2026-10-24 06:12:40 UTC\n"
                "  0 %s Sun 2026-11-08 09:00:00 UTC Sun 2026-11-08 09:30:00 UTC\n" % ("c" * 32, "d" * 32))
        b = D.parse_boots(text)
        self.assertEqual([x["idx"] for x in b], [-1, 0])
        self.assertEqual(b[0]["last"], "Sat 2026-10-24 06:12:40 UTC")

    def test_blacklist(self):
        files = {"/lib/modprobe.d/blacklist_linux_7.0.0-10-generic.conf": "# watchdog drivers\nblacklist sp5100_tco\nblacklist iTCO_wdt\n",
                 "/etc/modprobe.d/x.conf": "options sp5100_tco nowayout=0\n",
                 "/etc/modprobe.d/y.conf": "install sp5100-tco /bin/false\n"}
        self.assertEqual(D.blacklisted(files), ["/etc/modprobe.d/y.conf (install /bin/false)",
                                                "/lib/modprobe.d/blacklist_linux_7.0.0-10-generic.conf"])
        self.assertEqual(D.blacklisted({"/etc/modprobe.d/z.conf": "# blacklist sp5100_tco\n"}), [])


def report(**kw):
    r = {"devices": [{"name": "watchdog0", "identity": "SP5100 TCO timer", "timeout": "60", "state": "active",
                      "nowayout": "0", "bootstatus": "0", "driver": "sp5100-tco"}],
         "params": {"nowayout": "0", "heartbeat": "60"}, "holders": [(812, "ollama1-watchdo")], "blacklist": [],
         "reserve": {"free": 0, "reserve": 15 << 30, "size": 295 << 30}, "status": {"state": "petting"},
         "enabled": "enabled", "active": "active", "restarts": "0", "runtime_watchdog": "0",
         "tco_now": D.parse_tco(TCO_OK), "boots": [{"idx": -1, "first": "a", "last": "b"}, {"idx": 0, "first": "c", "last": "d"}],
         "has_prev": True, "tail": TAIL_ABRUPT, "tco_prev": D.parse_tco(TCO_OK), "netwatch_prev": "", "pstore": []}
    r.update(kw)
    kern = r.pop("kern", KERN_RESUMED_FLOOD)
    r["signs"], r["signs_last"] = D.count_signs(kern)
    r["wd_prev"] = D.parse_watchdog_lines(r.pop("wd", WD_RESUMED))
    r["ended"] = D.how_it_ended(r["tail"], r["signs"], r["wd_prev"])
    return r


class TestVerdicts(unittest.TestCase):
    def test_the_flood_with_a_passing_probe(self):
        """The 2026-10-24 shape: armed, resumed, the NVIDIA flood and a full disk, not one probe failed."""
        v = D.verdicts(report())
        self.assertEqual(v["probe"][0], "LIKELY")
        self.assertIn("nvrm_assert", v["probe"][1])
        self.assertEqual(v["paused"][0], "possible")
        for k in ("driver", "blacklist", "service", "holder", "clean_stop"):
            self.assertEqual(v[k][0], "ruled out", k)
        self.assertEqual(v["no_reset"][0], "unknown")
        self.assertEqual(D.worst(report()), 0)

    def test_left_paused_by_a_suspend(self):
        v = D.verdicts(report(kern=KERN_SUSPEND_FLOOD, wd=WD_PAUSED))
        self.assertEqual(v["paused"][0], "LIKELY")
        self.assertEqual(v["probe"][0], "unknown")

    def test_no_device(self):
        r = report(devices=[], tco_now=D.parse_tco(TCO_DISABLED), status={"state": "no-device"})
        v = D.verdicts(r)
        self.assertEqual(v["driver"][0], "LIKELY")
        self.assertEqual(D.worst(r), 1)

    def test_held_by_systemd(self):
        v = D.verdicts(report(holders=[(1, "systemd")], runtime_watchdog="30s"))
        self.assertEqual(v["holder"][0], "LIKELY")

    def test_service_off(self):
        self.assertEqual(D.verdicts(report(enabled="disabled", active="inactive"))["service"][0], "LIKELY")
        self.assertEqual(D.verdicts(report(wd=""))["service"][0], "possible")

    def test_shutdown_that_hung(self):
        v = D.verdicts(report(tail=TAIL_SHUTDOWN))
        self.assertEqual(v["clean_stop"][0], "LIKELY")

    def test_a_watchdog_reset_proves_the_board(self):
        r = report()
        r["devices"][0]["bootstatus"] = "32"
        self.assertEqual(D.verdicts(r)["no_reset"][0], "ruled out")

    def test_wifi_lost_box_up(self):
        kern = H % 1 + "kernel: wlan0: deauthenticating from 02:00:00:00:00:01 by local choice (Reason: 3=DEAUTH_LEAVING)\n"
        v = D.verdicts(report(kern=kern))
        self.assertEqual(v["not_a_hang"][0], "possible")

    def test_no_previous_boot(self):
        r = report(has_prev=False)
        v = D.verdicts(r)
        self.assertEqual(v["paused"][0], "unknown")
        self.assertIn("previous boot's journal is not there", D.render(r) + v["paused"][1])

    def test_render(self):
        text = D.render(report(kern=KERN_SUSPEND_FLOOD, wd=WD_PAUSED))
        for want in ("THE DRIVER AND THE TIMER", "watchdog0: SP5100 TCO timer", "nowayout 0", "THE PREVIOUS BOOT",
                     "how it ended: it went into a suspend", "LEFT PAUSED FOR SLEEP", "WHY THE WATCHDOG MAY NOT HAVE RESET IT",
                     "15.0 GiB more that only root can write"):
            self.assertIn(want, text)
        self.assertEqual(len([l for l in text.splitlines() if re.match(r"^  [a-z_]+ +(LIKELY|possible|ruled out|unknown|a fact)", l)]),
                         len(D.REASONS))


class TestReadOnly(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1wd-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        sysd, procd = os.path.join(self.dir, "sys"), os.path.join(self.dir, "proc")
        wd = os.path.join(sysd, "class", "watchdog", "watchdog0")
        os.makedirs(wd)
        for k, v in (("identity", "SP5100 TCO timer"), ("timeout", "60"), ("state", "active"), ("nowayout", "0"),
                     ("bootstatus", "0")):
            open(os.path.join(wd, k), "w").write(v + "\n")
        os.makedirs(os.path.join(sysd, "module", "sp5100_tco", "parameters"))
        open(os.path.join(sysd, "module", "sp5100_tco", "parameters", "nowayout"), "w").write("0\n")
        os.makedirs(os.path.join(procd, "812", "fd"))
        open(os.path.join(procd, "812", "comm"), "w").write("ollama1-watchdo\n")
        os.symlink("/dev/watchdog0", os.path.join(procd, "812", "fd", "3"))
        os.symlink("/dev/null", os.path.join(procd, "812", "fd", "0"))
        self.env = {}
        for k, v in (("OLLAMA1_SYS", sysd), ("OLLAMA1_PROC", procd)):
            self.env[k] = os.environ.get(k)
            os.environ[k] = v
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_gather_only_reads(self):
        calls = []

        def run(argv, timeout=60):
            calls.append(argv)
            if argv[0] == "journalctl":
                if "-u" in argv:
                    return 0, WD_RESUMED
                if "--list-boots" in argv:
                    return 0, "[]"
                if "-k" in argv and "0" in argv:
                    return 0, TCO_OK
                if "-k" in argv:
                    return 0, KERN_RESUMED_FLOOD
                return 0, TAIL_ABRUPT
            if argv[:2] == ["systemctl", "is-enabled"]:
                return 0, "enabled\n"
            if argv[:2] == ["systemctl", "is-active"]:
                return 0, "active\n"
            return 0, "0\n"
        r = D.gather(run=run)
        self.assertEqual(r["holders"], [(812, "ollama1-watchdo")])
        self.assertEqual(r["params"], {"nowayout": "0"})
        self.assertEqual(r["devices"][0]["identity"], "SP5100 TCO timer")
        self.assertEqual(r["ended"][0], "abrupt")
        for argv in calls:
            self.assertIn(argv[0], ("journalctl", "systemctl"))
            if argv[0] == "systemctl":
                self.assertIn(argv[1], ("is-enabled", "is-active", "show"))
        self.assertIn("THE PREVIOUS BOOT", D.render(r))

    def test_the_source_never_writes_or_opens_the_device(self):
        src = open(os.path.join(U.LIB, "o1wddoctor.py")).read()
        for bad in ("os.open(", '"w"', "'w'", '"a"', "O_WRONLY", "O_RDWR", "unlink", "rmtree", "os.write", "chmod",
                    '["modprobe"', '"start"', '"stop"', '"restart"', "--vacuum"):
            self.assertNotIn(bad, src, bad)
        self.assertNotRegex(src, r"open\([^)]*/dev/watchdog")

    def test_the_command(self):
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-watchdog-doctor"), "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("read-only", r.stdout)
        env = dict(os.environ, PATH="/nonexistent")          # no journalctl, no systemctl: still a report
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-watchdog-doctor")], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertIn("WHY THE WATCHDOG MAY NOT HAVE RESET IT", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-watchdog-doctor"), "--json"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertIn("verdicts", json.loads(r.stdout))


if __name__ == "__main__":
    unittest.main()
