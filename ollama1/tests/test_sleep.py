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
    it goes when they end, however they end (even SIGKILL or SIGHUP)."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="o1inh-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.fake = os.path.join(self.d, "systemd-inhibit")
        self.log = os.path.join(self.d, "args")
        # like the real one: take the options, then run the command (here in
        # place, so the holder's pid is the one logged)
        with open(self.fake, "w") as f:
            f.write('#!/bin/sh\nargs="$*"\nwhile [ "${1#--}" != "$1" ]; do shift; done\n'
                    'echo "$$ $args" >"%s"\nexec "$@"\n' % self.log)
        os.chmod(self.fake, 0o755)

    def holder(self):
        import time
        for _ in range(100):
            if os.path.exists(self.log) and os.path.getsize(self.log):
                break
            time.sleep(0.05)
        pid, args = open(self.log).read().split(" ", 1)
        return int(pid), args

    def gone(self, pid, wait=5.0):
        import time
        end = time.time() + wait
        while time.time() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return True
            except ChildProcessError:
                pass
            time.sleep(0.05)
        return False

    def test_held_then_released(self):
        import o1sleep
        with o1sleep.Inhibit("the model library is being synced", exe=self.fake) as inh:
            pid, args = self.holder()
            self.assertIn("--what=sleep:handle-power-key", args)
            self.assertIn("--mode=block", args)
            self.assertIn("--why=the model library is being synced", args)
            self.assertIsNone(inh.p.poll())
        self.assertIsNotNone(inh.p.poll())                 # gone with the job, on EOF
        self.assertTrue(self.gone(pid))

    def run_parent_and_kill(self, sig):
        import signal
        code = ("import sys, time; sys.path.insert(0, %r); import o1sleep\n"
                "with o1sleep.Inhibit('x', exe=%r):\n    print('ready', flush=True); time.sleep(60)\n"
                % (U.LIB, self.fake))
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        pid, _ = self.holder()
        p.send_signal(sig)
        p.wait(10)
        self.assertTrue(self.gone(pid), "the inhibitor outlived its parent (signal %d)" % sig)
        del signal

    def test_dies_with_a_killed_parent(self):
        import signal
        self.run_parent_and_kill(signal.SIGKILL)

    def test_dies_with_a_hung_up_parent(self):
        import signal
        self.run_parent_and_kill(signal.SIGHUP)

    def test_setup_holder_dies_with_setup(self):
        import signal
        lib = os.path.join(U.LIB, "setuplib.sh")
        script = ('die() { echo "DIE: $*"; exit 1; }; source "%s"; exec 9>"%s/lock"; '
                  'hold_inhibitor "setup.sh is running"; echo ready; sleep 60' % (lib, self.d))
        env = dict(os.environ, PATH=self.d + os.pathsep + os.environ["PATH"])
        p = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True, env=env)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        pid, args = self.holder()
        self.assertIn("--why=setup.sh is running", args)
        # it doesn't hold setup's lock (fd 9)
        import fcntl
        fd = os.open(os.path.join(self.d, "lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        p.send_signal(signal.SIGKILL)
        p.wait(10)
        self.assertTrue(self.gone(pid), "setup's inhibitor outlived setup")

    def test_where_it_is_held(self):
        def src(*p):
            with open(os.path.join(U.KIT, *p)) as f:
                return f.read()
        self.assertIn('held("updates are being applied", update_now)', src("bin", "ollama1-helper"))
        self.assertIn('held("a model is being downloaded", pull, args[1])', src("bin", "ollama1-helper"))
        self.assertIn('with L.Lock(), o1sleep.Inhibit("the model library is being synced"):', src("bin", "ollama1-models"))
        self.assertIn('with o1sleep.Inhibit("Ollama is being updated"):', src("bin", "ollama1-update-ollama"))
        setup = src("setup.sh")
        self.assertIn('hold_inhibitor "setup.sh is running"', setup)
        self.assertLess(setup.index('hold_inhibitor "setup.sh'), setup.index('step "'))
        with open(os.path.join(U.LIB, "setuplib.sh")) as f:
            lib = f.read()
        self.assertIn("sh -c 'while kill -0 \"$1\" 2>/dev/null; do sleep 2; done' sh \"$$\" </dev/null >/dev/null 2>&1 9>&- &", lib)
