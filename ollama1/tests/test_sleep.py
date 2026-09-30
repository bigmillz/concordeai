"""Sleep: the power button drop-in, when sleeping is refused, the root
helper's fixed action, the sleep hook's records and the check after
waking."""
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import o1sleep
from o1common import Paths


class TestLogind(unittest.TestCase):
    def test_dropin(self):
        t = open(os.path.join(U.KIT, "config", "logind-ollama1.conf")).read()
        keys = dict(l.split("=", 1) for l in t.splitlines() if "=" in l and not l.startswith("#"))
        self.assertIn("[Login]", t)
        self.assertEqual(keys, {"HandlePowerKey": "suspend", "HandlePowerKeyLongPress": "poweroff"})

    def test_setup_reloads_logind_without_a_restart(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn("/etc/systemd/logind.conf.d/ollama1.conf", s)
        self.assertIn("systemctl kill -s HUP systemd-logind", s)
        self.assertNotIn("restart systemd-logind", s)
        self.assertIn("/usr/lib/systemd/system-sleep/ollama1", s)


class TestBusy(unittest.TestCase):
    def reasons(self, active, lock=None):
        return o1sleep.busy_reasons(active_units=lambda pats: list(active), setup_lock=lock or "/nonexistent")

    def test_free(self):
        self.assertEqual(self.reasons([]), [])

    def test_each_busy_kind(self):
        cases = {"ollama1-pull@0123456789ab.service": "downloaded", "ollama1-models-sync.service": "synced",
                 "ollama1-update-now.service": "updates", "ollama1-update-ollama.service": "Ollama is being updated",
                 "apt-daily.service": "update check", "apt-daily-upgrade.service": "security updates"}
        for unit, word in cases.items():
            r = self.reasons([unit])
            self.assertEqual(len(r), 1, unit)
            self.assertIn(word, r[0])

    def test_other_units_dont_count(self):
        self.assertEqual(self.reasons(["ollama.service", "ollama1-gateway.service", "mdcheck_start.service"]), [])

    def test_systemd_unreachable_refuses(self):
        self.assertTrue(o1sleep.busy_reasons(active_units=lambda p: None, setup_lock="/nonexistent"))

    def test_setup_lock(self):
        d = tempfile.mkdtemp()
        try:
            lock = os.path.join(d, "setup.lock")
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT)
            self.assertEqual(self.reasons([], lock), [])
            fcntl.flock(fd, fcntl.LOCK_EX)           # setup.sh holding it
            self.assertEqual(self.reasons([], lock), ["setup.sh is running"])
            os.close(fd)
        finally:
            shutil.rmtree(d)

    def test_raid_resync_is_allowed(self):
        d = tempfile.mkdtemp()
        try:
            md = os.path.join(d, "mdstat")
            with open(md, "w") as f:
                f.write("md127 : active raid1 sdb1[1] sda1[0]\n      [=>....]  resync =  9.1% (1/2) finish=500min\n")
            self.assertTrue(o1sleep.raid_resyncing(md))
            self.assertEqual(self.reasons([]), [])
        finally:
            shutil.rmtree(d)


class HelperFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1sleep-")
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.dir, "systemctl.log")
        self.active = os.path.join(self.dir, "active")
        open(self.active, "w").close()
        sc = os.path.join(self.bin, "systemctl")
        with open(sc, "w") as f:
            f.write('#!/bin/sh\necho "$*" >> "%s"\n'
                    'if [ "$1" = list-units ]; then cat "%s"; fi\nexit 0\n' % (self.log, self.active))
        os.chmod(sc, os.stat(sc).st_mode | stat.S_IEXEC)
        for f in (os.path.join(Paths.state, "sleep.json"), Paths.helper_status):
            if os.path.exists(f):
                os.unlink(f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def env(self):
        return dict(os.environ, OLLAMA1_PREFIX=U.PREFIX, PATH=self.bin + ":" + os.environ["PATH"],
                    OLLAMA1_HELPER=os.path.join(U.BIN, "ollama1-helper"))

    def helper(self, *args):
        return subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-helper")] + list(args),
                              env=self.env(), capture_output=True, text=True, timeout=30)

    def calls(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []


class TestHelperSleep(HelperFixture):
    def test_sleeps_when_free(self):
        r = self.helper("sleep")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("suspend", self.calls())
        self.assertEqual(json.load(open(Paths.helper_status))["sleep"]["result"], "ok")

    def test_refuses_while_pulling(self):
        with open(self.active, "w") as f:
            f.write("ollama1-pull@0123456789ab.service loaded active running pull\n")
        r = self.helper("sleep")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("suspend", self.calls())
        st = json.load(open(Paths.helper_status))["sleep"]
        self.assertEqual(st["result"], "refused")
        self.assertIn("downloaded", st["detail"])

    def test_takes_no_argument(self):
        self.assertNotEqual(self.helper("sleep", "now").returncode, 0)
        self.assertNotIn("suspend", self.calls())

    def test_hook_records_and_checks(self):
        hook = os.path.join(U.KIT, "config", "ollama1-sleep-hook")
        env = self.env()
        env["OLLAMA1_HELPER"] = os.path.join(self.bin, "helper")
        with open(env["OLLAMA1_HELPER"], "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(U.BIN, "ollama1-helper")))
        os.chmod(env["OLLAMA1_HELPER"], 0o755)
        self.assertEqual(subprocess.run(["sh", hook, "pre", "suspend"], env=env).returncode, 0)
        rec = json.load(open(os.path.join(Paths.state, "sleep.json")))
        self.assertIn("last_sleep", rec)
        self.assertNotIn("start --no-block ollama1-resume-check.service", self.calls())
        self.assertEqual(subprocess.run(["sh", hook, "post", "suspend"], env=env).returncode, 0)
        rec = json.load(open(os.path.join(Paths.state, "sleep.json")))
        self.assertGreaterEqual(rec["last_wake"], rec["last_sleep"])
        self.assertIn("start --no-block ollama1-resume-check.service", self.calls())


class TestResumeCheck(unittest.TestCase):
    def test_healthy(self):
        restarted = []
        rec = o1sleep.resume_check(ollama=lambda: True, gpu=lambda: True, restart=restarted.append,
                                   log=lambda *_: None)
        self.assertEqual(restarted, [])
        self.assertTrue(rec["resume_check"]["ok"])

    def test_unhealthy_restarts_ollama_and_tunnel(self):
        restarted = []
        answers = iter([False, True])
        rec = o1sleep.resume_check(ollama=lambda: next(answers), gpu=lambda: True, restart=restarted.append,
                                   log=lambda *_: None)
        self.assertEqual(restarted, [["ollama.service", "ollama1-tunnel.service"]])
        self.assertTrue(rec["resume_check"]["ok"])
        self.assertIn("restarted", rec["resume_check"]["detail"])

    def test_gpu_gone_restarts_too(self):
        restarted = []
        rec = o1sleep.resume_check(ollama=lambda: True, gpu=lambda: False, restart=restarted.append,
                                   log=lambda *_: None)
        self.assertEqual(len(restarted), 1)
        self.assertIn("GPU", rec["resume_check"]["detail"])

    def test_gpu_probe_reads_sysfs(self):
        d = tempfile.mkdtemp()
        try:
            dev = os.path.join(d, "class/drm/card1/device")
            os.makedirs(dev)
            self.assertFalse(o1sleep.gpu_ok(d))
            open(os.path.join(dev, "mem_info_vram_total"), "w").write("17163091968\n")
            open(os.path.join(dev, "gpu_busy_percent"), "w").write("0\n")
            self.assertTrue(o1sleep.gpu_ok(d))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()


class TestInhibitor(unittest.TestCase):
    """Long jobs hold a logind inhibitor for sleep and the power button, and
    let it go when they end."""

    def test_held_then_released(self):
        import o1sleep
        import tempfile
        import time
        d = tempfile.mkdtemp(prefix="o1inh-")
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "systemd-inhibit")
        log = os.path.join(d, "args")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\necho "$$ $*" >"%s"\nexec sleep 300\n' % log)
        os.chmod(fake, 0o755)
        with o1sleep.Inhibit("the model library is being synced", exe=fake) as inh:
            for _ in range(50):
                if os.path.exists(log) and os.path.getsize(log):
                    break
                time.sleep(0.05)
            pid, args = open(log).read().split(" ", 1)
            self.assertIn("--what=sleep:handle-power-key", args)
            self.assertIn("--mode=block", args)
            self.assertIn("--why=the model library is being synced", args)
            self.assertIsNone(inh.p.poll())
        self.assertIsNotNone(inh.p.poll())                 # gone with the job
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid), 0)

    def test_where_it_is_held(self):
        def src(*p):
            with open(os.path.join(U.KIT, *p)) as f:
                return f.read()
        self.assertIn('held("updates are being applied", update_now)', src("bin", "ollama1-helper"))
        self.assertIn('held("a model is being downloaded", pull, args[1])', src("bin", "ollama1-helper"))
        self.assertIn('with L.Lock(), o1sleep.Inhibit("the model library is being synced"):', src("bin", "ollama1-models"))
        self.assertIn('with o1sleep.Inhibit("Ollama is being updated"):', src("bin", "ollama1-update-ollama"))
        setup = src("setup.sh")
        self.assertIn("setsid systemd-inhibit --what=sleep:handle-power-key --mode=block", setup)
        self.assertIn("trap 'kill -TERM -- \"-$INHIBIT_PID\"", setup)
        self.assertLess(setup.index("systemd-inhibit --what"), setup.index('step "'))
