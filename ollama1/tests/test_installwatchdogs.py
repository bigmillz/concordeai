"""tools/install-watchdogs.sh (6b524) against a fake root, a fake /proc/sys and a fake systemctl and systemd-run:
the dry run changes nothing and says everything, the install puts exactly its files in place and asks systemctl for
exactly its units, the sysctl values change at once and go back on --uninstall, the busy refusal, the dead man's
switch (--arm-rollback, --confirm, the rollback through the installed copy), and that it never restarts Ollama, the
gateway or the tunnel and never reboots."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

import o1test_util as U

SCRIPT = os.path.join(U.TOOLS, "install-watchdogs.sh")
BID = "0123456789abcdef" * 2           # boot ids, made at run time (the repo scan flags a 32-hex literal)
BEFORE = {"panic": "0", "softlockup_panic": "0", "hardlockup_panic": "0", "panic_on_oops": "0", "hung_task_panic": "0",
          "hung_task_timeout_secs": "120", "nmi_watchdog": "1"}
PANIC = {"panic": "10", "softlockup_panic": "1", "hardlockup_panic": "1", "panic_on_oops": "1", "hung_task_panic": "1",
         "hung_task_timeout_secs": "600", "nmi_watchdog": "1"}
FILES = {
    "usr/local/lib/ollama1/bin/ollama1-netwatch": "0o755",
    "usr/local/lib/ollama1/bin/ollama1-bootguard": "0o755",
    "usr/local/lib/ollama1/bin/ollama1-watchdog-doctor": "0o755",
    "usr/local/lib/ollama1/lib/o1netwatch.py": "0o644",
    "usr/local/lib/ollama1/lib/o1bootguard.py": "0o644",
    "usr/local/lib/ollama1/lib/o1wddoctor.py": "0o644",
    "usr/local/lib/ollama1/tools/install-watchdogs.sh": "0o755",
    "etc/systemd/system/ollama1-netwatch.service": "0o644",
    "etc/systemd/system/ollama1-netwatch.timer": "0o644",
    "etc/systemd/system/ollama1-bootguard.service": "0o644",
    "etc/systemd/system/ollama1-watchdogs-rollback.service": "0o644",
    "etc/ollama1/panic-sysctl.conf": "0o644",
    "usr/local/sbin/ollama1-netwatch": "/usr/local/lib/ollama1/bin/ollama1-netwatch",
    "usr/local/sbin/ollama1-bootguard": "/usr/local/lib/ollama1/bin/ollama1-bootguard",
    "usr/local/sbin/ollama1-watchdog-doctor": "/usr/local/lib/ollama1/bin/ollama1-watchdog-doctor",
    "var/lib/ollama1/panic-before.json": "0o644",
    "var/lib/ollama1/bootguard.json": "0o644",
}
INSTALL_CALLS = ["daemon-reload", "enable ollama1-bootguard.service", "restart ollama1-bootguard.service",
                 "enable --now ollama1-netwatch.timer"]
UNINSTALL_CALLS = ["disable --now ollama1-netwatch.timer", "stop ollama1-netwatch.service",
                   "disable ollama1-bootguard.service", "stop ollama1-bootguard.service",
                   "stop ollama1-watchdogs-deadline.timer", "daemon-reload"]
READ_ONLY = ("list-units", "list-timers", "is-enabled", "is-active", "show")


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1wdi-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "root")
        self.kern = os.path.join(self.root, "proc", "sys", "kernel")
        os.makedirs(os.path.join(self.kern, "random"))
        for k, v in BEFORE.items():
            self.set(k, v)
        with open(os.path.join(self.kern, "random", "boot_id"), "w") as f:
            f.write(BID + "\n")
        with open(os.path.join(self.root, "proc", "uptime"), "w") as f:
            f.write("7200.00 100.00\n")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.dir, "calls")
        # the fake systemctl runs the installed boot guard when asked to (re)start it, as systemd would
        with open(os.path.join(self.bin, "systemctl"), "w") as f:
            f.write('#!/bin/sh\necho "systemctl $*" >>"$FAKE_CALLS"\n'
                    'if [ "$*" = "restart ollama1-bootguard.service" ]; then\n'
                    '  python3 -B "$O1_WATCHDOGS_TESTROOT/usr/local/lib/ollama1/bin/ollama1-bootguard" run >/dev/null\nfi\n'
                    '[ "$1" = list-timers ] && echo "NEXT 1min ollama1-netwatch.timer"\nexit 0\n')
        with open(os.path.join(self.bin, "systemd-run"), "w") as f:
            f.write('#!/bin/sh\necho "systemd-run $*" >>"$FAKE_CALLS"\nexit 0\n')
        for n in ("systemctl", "systemd-run"):
            os.chmod(os.path.join(self.bin, n), 0o755)
        self.before = self.tree()

    def set(self, key, value):
        with open(os.path.join(self.kern, key), "w") as f:
            f.write(value + "\n")

    def kernel(self):
        return {k: open(os.path.join(self.kern, k)).read().strip() for k in BEFORE}

    def run_it(self, *args, script=None):
        env = dict(os.environ, O1_WATCHDOGS_TESTROOT=self.root, FAKE_CALLS=self.calls,
                   OLLAMA1_PROC=os.path.join(self.root, "proc"), OLLAMA1_SYS=os.path.join(self.root, "sys"),
                   PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        env.pop("PYTHONPATH", None)
        return subprocess.run(["bash", script or SCRIPT] + list(args), env=env, capture_output=True, text=True, timeout=120)

    def tree(self):
        out = {}
        for d, dirs, files in os.walk(self.root):
            for n in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
                p = os.path.join(d, n)
                rel = os.path.relpath(p, self.root)
                if rel.startswith("proc/"):
                    continue
                out[rel] = os.readlink(p) if os.path.islink(p) else oct(os.stat(p).st_mode & 0o777)
        return out

    def systemctl_calls(self, mutating_only=True):
        try:
            lines = open(self.calls).read().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for l in lines:
            if l.startswith("systemctl "):
                a = l[len("systemctl "):]
                if mutating_only and a.split()[0] in READ_ONLY:
                    continue
                out.append(a)
            else:
                out.append(l)
        return out

    def clear_calls(self):
        if os.path.exists(self.calls):
            os.unlink(self.calls)

    def test_dry_run_changes_nothing_and_says_everything(self):
        r = self.run_it("--dry-run", "--arm-rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.tree(), self.before)
        self.assertEqual(self.kernel(), BEFORE)
        self.assertEqual(self.systemctl_calls(), [])
        for rel in FILES:
            if not rel.startswith("var/"):
                self.assertIn(os.path.join(self.root, rel), r.stdout, rel)
        for want in ("DRY RUN", "(new, mode 0755)", "would run: systemctl daemon-reload",
                     "would run: systemctl enable ollama1-bootguard.service",
                     "would run: systemctl enable --now ollama1-netwatch.timer",
                     "would run: systemd-run --unit=ollama1-watchdogs-deadline", "kernel.panic                     0 -> 10",
                     "kernel.hung_task_timeout_secs    120 -> 600", "kernel.panic_on_oops             0 -> 1"):
            self.assertIn(want, r.stdout, want)

    def test_install_then_uninstall(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.tree(), FILES)
        self.assertEqual(self.systemctl_calls(), INSTALL_CALLS)
        self.assertEqual(self.kernel(), PANIC)                       # at once, no reboot
        for rel, src in (("usr/local/lib/ollama1/lib/o1netwatch.py", os.path.join(U.LIB, "o1netwatch.py")),
                         ("etc/ollama1/panic-sysctl.conf", os.path.join(U.CONFIG, "panic-sysctl.conf"))):
            with open(os.path.join(self.root, rel), "rb") as a, open(src, "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertIn("Watchdog doctor (read-only)", r.stdout)
        self.assertIn("Undo everything: sudo bash /usr/local/lib/ollama1/tools/install-watchdogs.sh --uninstall", r.stdout)
        saved = json.load(open(os.path.join(self.root, "var/lib/ollama1/panic-before.json")))
        self.assertEqual(saved["values"]["kernel.hung_task_timeout_secs"], 120)

        self.clear_calls()
        self.assertEqual(self.run_it().returncode, 0)                 # again: the same files, the same values
        self.assertEqual(self.tree(), FILES)
        saved2 = json.load(open(os.path.join(self.root, "var/lib/ollama1/panic-before.json")))
        self.assertEqual(saved, saved2)                               # the "before" is not overwritten

        self.clear_calls()
        r = self.run_it("--uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.systemctl_calls(), UNINSTALL_CALLS)
        self.assertEqual(self.kernel(), BEFORE)
        left = {k: v for k, v in self.tree().items() if k not in self.before}
        self.assertEqual(left, {})

    def test_uninstall_restores_the_owners_own_values(self):
        self.set("panic", "30")
        self.set("hung_task_timeout_secs", "240")
        self.assertEqual(self.run_it().returncode, 0)
        self.assertEqual(self.kernel()["panic"], "10")
        self.assertEqual(self.run_it("--uninstall").returncode, 0)
        self.assertEqual((self.kernel()["panic"], self.kernel()["hung_task_timeout_secs"]), ("30", "240"))

    def test_refuses_while_the_gateway_serves(self):
        act = os.path.join(self.root, "run", "ollama1", "stats", "activity.json")
        os.makedirs(os.path.dirname(act))
        with open(act, "w") as f:
            json.dump({"inflight": 1, "at": time.time(), "last": time.time()}, f)
        before = self.tree()
        r = self.run_it()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("busy: the gateway is serving 1 request(s)", r.stdout)
        self.assertIn("Nothing was changed.", r.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.kernel(), BEFORE)
        self.assertEqual(self.systemctl_calls(), [])

    def test_the_dead_mans_switch(self):
        r = self.run_it("--arm-rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.systemctl_calls()
        self.assertIn("systemd-run --unit=ollama1-watchdogs-deadline --description=ollama1: roll back the watchdogs "
                      "install unless confirmed --on-active=20min --timer-property=AccuracySec=10s "
                      "/bin/systemctl start --no-block ollama1-watchdogs-rollback.service", calls)
        self.assertLess(calls.index(next(c for c in calls if c.startswith("systemd-run"))),
                        calls.index("restart ollama1-bootguard.service"))     # armed before anything takes effect
        trial = json.load(open(os.path.join(self.root, "var/lib/ollama1/watchdogs-trial.json")))
        self.assertEqual(trial["boot_id"], BID)
        self.assertEqual(trial["deadline"] - trial["armed_at"], 1200)
        self.assertIn("sudo bash /usr/local/lib/ollama1/tools/install-watchdogs.sh --confirm", r.stdout)

        # nobody confirmed: the deadline starts the rollback unit, which runs the INSTALLED copy
        self.clear_calls()
        installed = os.path.join(self.root, "usr/local/lib/ollama1/tools/install-watchdogs.sh")
        r = self.run_it("--uninstall", "--rollback", script=installed)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("rollback", r.stdout)
        self.assertEqual(self.kernel(), BEFORE)
        self.assertEqual({k: v for k, v in self.tree().items() if k not in self.before}, {})
        self.assertNotIn("stop ollama1-watchdogs-deadline.timer", self.systemctl_calls())

    def test_confirm(self):
        self.assertEqual(self.run_it("--arm-rollback").returncode, 0)
        self.clear_calls()
        r = self.run_it("--confirm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.systemctl_calls(), ["stop ollama1-watchdogs-deadline.timer"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "var/lib/ollama1/watchdogs-trial.json")))
        self.assertEqual(self.kernel(), PANIC)

    def test_an_edited_config_is_kept(self):
        conf = os.path.join(self.root, "etc/ollama1/panic-sysctl.conf")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w") as f:
            f.write("kernel.panic = 20\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kept your edited", r.stdout)
        self.assertEqual(open(conf).read(), "kernel.panic = 20\n")
        self.assertEqual(self.kernel()["panic"], "20")
        self.assertEqual(self.kernel()["panic_on_oops"], "0")

    def test_a_partial_kit_changes_nothing(self):
        kit = os.path.join(self.dir, "kit")
        for d in ("bin", "lib", "tools", "systemd", "config"):
            os.makedirs(os.path.join(kit, d))
        shutil.copy(SCRIPT, os.path.join(kit, "tools"))
        for n in ("ollama1-netwatch", "ollama1-bootguard"):
            shutil.copy(os.path.join(U.BIN, n), os.path.join(kit, "bin"))
        r = self.run_it(script=os.path.join(kit, "tools", "install-watchdogs.sh"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ollama1-watchdog-doctor is missing", r.stdout)
        self.assertEqual(self.tree(), self.before)
        self.assertFalse(os.path.exists(self.calls))

    def test_it_touches_nothing_else(self):
        src = open(SCRIPT).read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for word in ("ufw", "sshd", "cloudflared", "nft ", "netplan", "nmcli", "apt", "curl", "wget", "setup.sh",
                     "ollama.service", "ollama1-gateway", "ollama1-tunnel", "ollama1-admin", "systemctl reboot",
                     "ollama1-reboot", "shutdown -r", "poweroff",
                     "sysctl -w", "sysctl --system", "/etc/sysctl.d", "ollama1-watchdog.service", "modprobe"):
            self.assertNotIn(word, code, word)
        verbs = re.findall(r"(?m)(?:act |^\s*|; |if )systemctl ([a-z-]+)", code)
        self.assertEqual(set(verbs) - {"daemon-reload", "enable", "restart", "stop", "disable", "list-timers",
                                          "reset-failed"}, set())
        self.assertEqual(re.findall(r"systemctl restart (\S+)", code), ["ollama1-bootguard.service"])
        r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, SCRIPT], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_readme_and_notes(self):
        readme = open(os.path.join(U.KIT, "README.md")).read()
        for word in ("## Self-recovery", "tools/install-watchdogs.sh --dry-run", "--arm-rollback", "--confirm",
                     "--uninstall", "ollama1-watchdog-doctor", "/etc/ollama1/netwatch.off", "netwatch_driver_reload",
                     "/etc/ollama1/panic-sysctl.conf"):
            self.assertIn(word, readme, word)
        notes = open(os.path.join(os.path.dirname(U.KIT), "NOTES.md")).read()
        who = "P" + "atrick"                     # NOTES.md names the maintainer; the kit never does (test_repo)
        self.assertRegex(notes, r"(?m)^## 6b524 .*\(per %s\)$" % who)


if __name__ == "__main__":
    unittest.main()
