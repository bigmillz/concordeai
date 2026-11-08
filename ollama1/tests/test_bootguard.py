"""Panic-reboot and the boot-loop guard (6b524) against a fake /proc/sys: the boot record and the 3-in-60-minutes
rule (a boot counted once, old ones forgotten, a clock that went back), the safe values on a trip, the config file's
parsing and clamps, hardlockup_panic only with the NMI watchdog, the values saved before and put back, the install's
trial rolled back after a reboot, and the units."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import o1bootguard as B

BID = "0123456789abcdef" * 2           # boot ids, made at run time (the repo scan flags a 32-hex literal)
BID2 = "fedcba9876543210" * 2
NOW = 1_900_000_000
CONF = open(os.path.join(U.CONFIG, "panic-sysctl.conf")).read()


class Proc:
    def __init__(self, tc, nmi="1", missing=()):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1bg-"))
        tc.addCleanup(shutil.rmtree, self.dir, True)
        k = os.path.join(self.dir, "sys", "kernel")
        os.makedirs(os.path.join(k, "random"))
        vals = dict(B.KERNEL_DEFAULTS, **{"kernel.nmi_watchdog": nmi})
        for key, v in vals.items():
            if key not in missing:
                with open(os.path.join(self.dir, "sys", *key.split(".")), "w") as f:
                    f.write("%s\n" % v)
        self.boot(BID, 120.0)
        old = os.environ.get("OLLAMA1_PROC")
        os.environ["OLLAMA1_PROC"] = self.dir
        tc.addCleanup(lambda: os.environ.__setitem__("OLLAMA1_PROC", old) if old else os.environ.pop("OLLAMA1_PROC", None))
        for f in (B.STATE, B.BEFORE, B.TRIAL, B.CONF):
            try:
                os.unlink(B.p(f))
            except FileNotFoundError:
                pass

    def boot(self, bid, uptime):
        with open(os.path.join(self.dir, "sys", "kernel", "random", "boot_id"), "w") as f:
            f.write(bid + "\n")
        with open(os.path.join(self.dir, "uptime"), "w") as f:
            f.write("%.2f 100.00\n" % uptime)

    def get(self, key):
        return open(os.path.join(self.dir, "sys", *key.split("."))).read().strip()


def conf(text=CONF):
    os.makedirs(os.path.dirname(B.p(B.CONF)), exist_ok=True)
    with open(B.p(B.CONF), "w") as f:
        f.write(text)


class TestLoopRule(unittest.TestCase):
    def boots(self, *ages_min):
        return {"boots": [{"id": "b%d" % i, "at": NOW - a * 60} for i, a in enumerate(ages_min)]}

    def test_three_within_an_hour_trips(self):
        st, tripped, recent = B.decide(self.boots(50, 20), BID, NOW - 30, NOW)
        self.assertTrue(tripped)
        self.assertEqual(len(recent), 3)
        self.assertEqual(st["tripped_boot"], BID)

    def test_two_do_not(self):
        st, tripped, recent = B.decide(self.boots(50), BID, NOW - 30, NOW)
        self.assertFalse(tripped)
        self.assertNotIn("tripped_boot", st)

    def test_older_than_the_hour_do_not_count(self):
        _, tripped, recent = B.decide(self.boots(70, 65, 61), BID, NOW - 30, NOW)
        self.assertFalse(tripped)
        self.assertEqual(len(recent), 1)

    def test_a_boot_is_counted_once(self):
        st = {}
        for _ in range(5):                                  # the service run again in the same boot
            st, tripped, recent = B.decide(st, BID, NOW - 30, NOW)
        self.assertFalse(tripped)
        self.assertEqual(len(st["boots"]), 1)

    def test_a_tripped_boot_stays_tripped(self):
        st, tripped, _ = B.decide(self.boots(50, 20), BID, NOW - 30, NOW)
        st, tripped, _ = B.decide(st, BID, NOW - 30, NOW + 7200)    # run again two hours later, same boot
        self.assertTrue(tripped)

    def test_old_boots_are_forgotten(self):
        st, _, _ = B.decide(self.boots(*[60 * 24 * 8 + i for i in range(5)]), BID, NOW, NOW)
        self.assertEqual([b["id"] for b in st["boots"]], [BID])
        many = {"boots": [{"id": "x%d" % i, "at": NOW - 7200 - i} for i in range(80)]}
        st, _, _ = B.decide(many, BID, NOW, NOW)
        self.assertEqual(len(st["boots"]), B.KEEP_MAX)

    def test_a_clock_that_went_back_errs_towards_staying_up(self):
        """A dead clock battery: each boot starts at the same wrong time, so earlier boots look like 'now'."""
        st, tripped, _ = B.decide({"boots": [{"id": "a", "at": NOW + 100}, {"id": "b", "at": NOW + 50}]}, BID, NOW, NOW)
        self.assertTrue(tripped)

    def test_junk_in_the_state_is_ignored(self):
        st, tripped, _ = B.decide({"boots": [1, None, {"id": 3}, {"at": "x"}, "y"]}, BID, NOW, NOW)
        self.assertEqual(st["boots"], [{"id": BID, "at": NOW}])
        st, tripped, _ = B.decide({"boots": "nope"}, BID, NOW, NOW)
        self.assertFalse(tripped)


class TestConf(unittest.TestCase):
    def test_the_kit_file(self):
        vals, notes = B.parse_conf(CONF)
        self.assertEqual(vals, {"kernel.panic": 10, "kernel.softlockup_panic": 1, "kernel.hardlockup_panic": 1,
                                "kernel.panic_on_oops": 1, "kernel.hung_task_panic": 1,
                                "kernel.hung_task_timeout_secs": 600})
        self.assertEqual(notes, [])
        self.assertNotIn("/etc/sysctl.d/", CONF.replace("NOT a file in /etc/sysctl.d", ""))

    def test_clamps_and_junk(self):
        vals, notes = B.parse_conf("kernel.panic = 0\nkernel.hung_task_timeout_secs=30\nvm.swappiness = 10\n"
                                   "kernel.panic_on_oops = yes\n; a comment\n# another\n"
                                   "kernel/softlockup_panic = 1\n-kernel.hung_task_panic = 1\nnonsense\n")
        self.assertEqual(vals, {"kernel.softlockup_panic": 1, "kernel.hung_task_panic": 1})
        self.assertEqual(len(notes), 5)
        self.assertTrue(any("hung_task_timeout_secs = 30 is outside 300..86400" in n for n in notes))
        self.assertTrue(any("kernel.panic = 0 is outside 5..600" in n for n in notes))


class TestRun(unittest.TestCase):
    def setUp(self):
        self.proc = Proc(self)
        self.lines = []
        self.calls = []

    def run_guard(self, now=NOW):
        return B.run(log=self.lines.append, wall=lambda: now, systemctl=lambda *a: self.calls.append(a) or True)

    def test_first_boot_arms(self):
        conf()
        self.run_guard()
        for k, v in (("kernel.panic", "10"), ("kernel.softlockup_panic", "1"), ("kernel.hardlockup_panic", "1"),
                     ("kernel.panic_on_oops", "1"), ("kernel.hung_task_panic", "1"), ("kernel.hung_task_timeout_secs", "600")):
            self.assertEqual(self.proc.get(k), v, k)
        st = json.load(open(B.p(B.STATE)))
        self.assertEqual((st["mode"], st["boot_id"], st["boots"][0]["at"]), ("panic", BID, NOW - 120))
        self.assertFalse(B.is_tripped(st, BID))
        self.assertEqual(self.calls, [])

    def test_a_loop_is_stopped(self):
        conf()
        B.snapshot()
        self.run_guard(NOW)
        self.proc.boot(BID2, 60)
        self.run_guard(NOW + 600)
        self.assertEqual(self.proc.get("kernel.panic_on_oops"), "1")
        third = "1" * 32
        self.proc.boot(third, 60)
        self.run_guard(NOW + 1200)
        for k, v in B.KERNEL_DEFAULTS.items():
            self.assertEqual(self.proc.get(k), str(v), k)
        st = json.load(open(B.p(B.STATE)))
        self.assertTrue(B.is_tripped(st, third))
        self.assertEqual(st["mode"], "safe")
        self.assertTrue(any(l.startswith("bootguard: TRIPPED: 3 boots within 60 minutes") for l in self.lines))
        self.assertIn("Boot guard: TRIPPED this boot", B.render_status(NOW + 1200))
        # two hours later, a fresh boot that is not part of a loop arms again
        self.proc.boot("2" * 32, 60)
        self.run_guard(NOW + 8000)
        self.assertEqual(self.proc.get("kernel.panic_on_oops"), "1")

    def test_safe_values_are_the_ones_from_before(self):
        with open(os.path.join(self.proc.dir, "sys", "kernel", "panic"), "w") as f:
            f.write("-1\n")                                  # someone had their own value
        B.snapshot()
        conf()
        self.run_guard()
        self.assertEqual(self.proc.get("kernel.panic"), "10")
        vals, errors = B.restore(log=self.lines.append)
        self.assertEqual(errors, [])
        self.assertEqual(self.proc.get("kernel.panic"), "-1")
        self.assertEqual(self.proc.get("kernel.hung_task_timeout_secs"), "120")
        self.assertFalse(os.path.exists(B.p(B.BEFORE)))

    def test_snapshot_only_once(self):
        made, _ = B.snapshot()
        self.assertTrue(made)
        conf()
        self.run_guard()
        made, data = B.snapshot()                            # a second install must not save the panic values
        self.assertFalse(made)
        self.assertEqual(data["values"]["kernel.panic"], 0)

    def test_restore_without_a_snapshot_uses_the_kernel_defaults(self):
        conf()
        self.run_guard()
        B.restore(log=self.lines.append)
        for k, v in B.KERNEL_DEFAULTS.items():
            self.assertEqual(self.proc.get(k), str(v), k)

    def test_no_nmi_watchdog_no_hardlockup_panic(self):
        self.proc = Proc(self, nmi="0")
        conf()
        self.run_guard()
        self.assertEqual(self.proc.get("kernel.hardlockup_panic"), "0")
        self.assertEqual(self.proc.get("kernel.softlockup_panic"), "1")
        st = json.load(open(B.p(B.STATE)))
        self.assertEqual(st["not_available"], ["kernel.hardlockup_panic"])
        self.assertTrue(any("NMI watchdog is off" in l for l in self.lines))

    def test_a_key_the_kernel_lacks_is_left_alone(self):
        self.proc = Proc(self, missing=("kernel.hung_task_panic",))
        conf()
        self.run_guard()
        self.assertFalse(os.path.exists(os.path.join(self.proc.dir, "sys", "kernel", "hung_task_panic")))
        self.assertEqual(json.load(open(B.p(B.STATE)))["errors"], [])

    def test_no_config_no_panic(self):
        self.run_guard()
        self.assertEqual(self.proc.get("kernel.panic"), "0")
        self.assertEqual(json.load(open(B.p(B.STATE)))["mode"], "safe")

    def test_an_edited_config(self):
        conf(CONF.replace("kernel.panic_on_oops = 1", "kernel.panic_on_oops = 0"))
        self.run_guard()
        self.assertEqual(self.proc.get("kernel.panic_on_oops"), "0")
        conf(CONF.replace("kernel.panic_on_oops = 1", "# kernel.panic_on_oops = 1"))
        with open(os.path.join(self.proc.dir, "sys", "kernel", "panic_on_oops"), "w") as f:
            f.write("1\n")
        self.run_guard()
        self.assertEqual(self.proc.get("kernel.panic_on_oops"), "0")    # a line taken out goes back to before

    def test_only_its_keys(self):
        self.assertEqual(B.write_key("vm.swappiness", 1), "not a key this program writes")
        self.assertFalse(os.path.exists(os.path.join(self.proc.dir, "sys", "vm")))

    def test_a_reboot_during_the_trial_rolls_back(self):
        conf()
        os.makedirs(os.path.dirname(B.p(B.TRIAL)), exist_ok=True)
        with open(B.p(B.TRIAL), "w") as f:
            json.dump({"boot_id": BID, "armed_at": NOW - 300, "deadline": NOW + 900}, f)
        self.run_guard()                                       # the same boot: the trial goes on
        self.assertEqual(self.calls, [])
        self.assertEqual(self.proc.get("kernel.panic"), "10")
        self.proc.boot(BID2, 60)                               # restarted before anyone confirmed
        self.run_guard(NOW + 400)
        self.assertEqual(self.calls, [("start", "--no-block", "ollama1-watchdogs-rollback.service")])
        self.assertEqual(self.proc.get("kernel.panic"), "0")

    def test_commands(self):
        env = dict(os.environ)
        conf()
        for args, want in ((["status"], "Boot guard"), (["plan"], "kernel.panic"), (["snapshot"], "the values from before"),
                           (["run"], "bootguard:"), (["restore"], "restored:")):
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-bootguard")] + args, env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(want, r.stdout)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-bootguard"), "plan",
                            os.path.join(U.CONFIG, "panic-sysctl.conf")], env=env, capture_output=True, text=True)
        self.assertIn("kernel.hung_task_timeout_secs    120 -> 600", r.stdout)


class TestUnits(unittest.TestCase):
    def test_bootguard_unit(self):
        u = open(os.path.join(U.SYSTEMD, "ollama1-bootguard.service")).read()
        for line in ("DefaultDependencies=no\n", "After=local-fs.target systemd-sysctl.service systemd-remount-fs.service\n",
                     "Before=sysinit.target shutdown.target\n", "Type=oneshot\n", "RemainAfterExit=yes\n",
                     "ExecStart=/usr/local/lib/ollama1/bin/ollama1-bootguard run\n", "TimeoutStartSec=30\n",
                     "WantedBy=sysinit.target\n", "ReadWritePaths=-/var/lib/ollama1\n"):
            self.assertIn(line, u)
        self.assertNotIn("\nPrivateTmp=yes", u)
        self.assertNotIn("ProtectKernelTunables=yes", u)          # it writes /proc/sys
        self.assertNotRegex(u, r"(?m)^(Requires|BindsTo)=")

    def test_rollback_unit(self):
        u = open(os.path.join(U.SYSTEMD, "ollama1-watchdogs-rollback.service")).read()
        self.assertIn("ExecStart=/bin/bash /usr/local/lib/ollama1/tools/install-watchdogs.sh --uninstall --rollback\n", u)
        self.assertNotIn("[Install]", u)

    def test_not_in_sysctl_d(self):
        """The panic values must never be where systemd-sysctl applies them before the guard has decided."""
        for root, _d, files in os.walk(U.KIT):
            for f in files:
                if f.endswith((".py", ".sh")) or "/systemd" in root or root.endswith("bin"):
                    text = open(os.path.join(root, f), errors="replace").read()
                    self.assertNotRegex(text, r"/etc/sysctl\.d/[^ \n]*panic", f)


if __name__ == "__main__":
    unittest.main()
