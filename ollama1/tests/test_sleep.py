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


class FakeTime:
    """A clock that only moves when the check sleeps."""
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestResumeCheck(unittest.TestCase):
    def check(self, ollama, gpu=lambda: True, models=lambda: True, needed=True, ft=None, **kw):
        ft = ft or FakeTime()
        restarted = []
        rec = o1sleep.resume_check(ollama=ollama, gpu=gpu, models=models, gpu_needed=lambda: needed,
                                   restart=restarted.append, log=lambda *_: None, sleep=ft.sleep, clock=ft.clock, **kw)
        return rec["resume_check"], restarted, ft

    def test_healthy(self):
        rc, restarted, ft = self.check(lambda: True)
        self.assertEqual(restarted, [])
        self.assertTrue(rc["ok"])
        self.assertEqual(ft.t, 0)                      # no waiting when all is well

    def test_unhealthy_restarts_ollama_and_tunnel(self):
        answers = iter([False, False, True])           # the first look, the check before restarting, after it
        rc, restarted, _ = self.check(lambda: next(answers))
        self.assertEqual(restarted, [["ollama.service", "ollama1-tunnel.service"]])
        self.assertTrue(rc["ok"])
        self.assertIn("restarted", rc["detail"])
        self.assertEqual(rc["restarts"], 1)

    def test_gpu_gone_restarts_too(self):
        rc, restarted, ft = self.check(lambda: True, gpu=lambda: False)
        self.assertEqual(len(restarted), 1)
        self.assertIn("GPU", rc["detail"])
        self.assertIn("still missing: the graphics card", rc["detail"])
        self.assertEqual((rc["gpu_ready"], rc["models_ready"], rc["waited_s"]), (False, True, 90))
        self.assertLessEqual(ft.t, 90 + 3)             # the wait is bounded

    def test_a_card_that_comes_back_is_waited_for_before_ollama_is_restarted(self):
        # the report: Ollama restarted at once after a wake, before the driver was back, and stayed down
        ft = FakeTime()
        events = []

        def gpu():
            return ft.t >= 40                          # the driver is back after 40 s

        def ollama():
            events.append(("ollama", ft.t, gpu()))
            return gpu() and "restart" in [e[0] for e in events]

        restarted = []
        rec = o1sleep.resume_check(ollama=ollama, gpu=gpu, models=lambda: True, gpu_needed=lambda: True,
                                   restart=lambda u: (restarted.append(u), events.append(("restart", ft.t, gpu()))),
                                   log=lambda *_: None, sleep=ft.sleep, clock=ft.clock)["resume_check"]
        self.assertTrue(rec["ok"])
        restart_at = [e for e in events if e[0] == "restart"][0]
        self.assertTrue(restart_at[2], "Ollama was restarted before the card was back")
        self.assertGreaterEqual(restart_at[1], 40)
        self.assertIn("were back after", rec["detail"])
        self.assertEqual((rec["gpu_ready"], rec["models_ready"]), (True, True))

    def test_the_models_drive_is_waited_for_too(self):
        ft = FakeTime()
        seen = []

        def restart(units):
            seen.append((ft.t, o1sleep.models_ok is not None))
        rec = o1sleep.resume_check(ollama=lambda: bool(seen), gpu=lambda: True, models=lambda: ft.t >= 30,
                                   gpu_needed=lambda: True, restart=restart, log=lambda *_: None,
                                   sleep=ft.sleep, clock=ft.clock)["resume_check"]
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["models_ready"])
        self.assertGreaterEqual(seen[0][0], 30)        # restarted only once the drive was back

    def test_it_was_only_slow_nothing_restarted(self):
        answers = iter([False, True])                  # not at the first look, answering after the (instant) wait
        rc, restarted, _ = self.check(lambda: next(answers))
        self.assertEqual(restarted, [])
        self.assertTrue(rc["ok"])
        self.assertIn("without a restart", rc["detail"])

    def test_the_restart_is_tried_once_more_when_ollama_still_doesnt_answer(self):
        answers = {"restarts": 0}
        restarted = []

        def restart(units):
            restarted.append(units)

        def ollama():
            return len(restarted) >= 2                 # only the second restart brings it back
        ft = FakeTime()
        rec = o1sleep.resume_check(ollama=ollama, gpu=lambda: True, models=lambda: True, gpu_needed=lambda: True,
                                   restart=restart, log=lambda *_: None, sleep=ft.sleep, clock=ft.clock)["resume_check"]
        self.assertEqual(len(restarted), 2)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["restarts"], 2)
        self.assertIn("restarted Ollama and the tunnel again", rec["detail"])

    def test_a_failure_says_why(self):
        ft = FakeTime()
        rc, restarted, _ = self.check(lambda: False, gpu=lambda: False, models=lambda: False, ft=ft)
        self.assertFalse(rc["ok"])
        self.assertEqual(len(restarted), 2)            # one retry, no more
        self.assertIn("STILL NOT ANSWERING", rc["detail"])
        self.assertIn("still missing: the graphics card, the models drive", rc["detail"])
        self.assertEqual(rc["restarts"], 2)
        self.assertEqual(rc["at"] > 0, True)
        self.assertLessEqual(ft.t, o1sleep.CHECK_BUDGET_S + 90 + 3)   # bounded: nothing waits for ever

    def test_no_card_expected_is_not_a_failure(self):
        # a machine without an AMD card: the missing card is no reason to wait or restart
        rc, restarted, ft = self.check(lambda: True, gpu=lambda: False, needed=False)
        self.assertTrue(rc["ok"])
        self.assertEqual(restarted, [])
        self.assertEqual(ft.t, 0)

    def test_the_budget_stops_a_check_that_is_taking_too_long(self):
        ft = FakeTime()
        calls = []

        def restart(units):
            calls.append(units)
            ft.t += 500                               # a restart that takes a long time
        rec = o1sleep.resume_check(ollama=lambda: False, gpu=lambda: True, models=lambda: True,
                                   gpu_needed=lambda: True, restart=restart, log=lambda *_: None,
                                   sleep=ft.sleep, clock=ft.clock)["resume_check"]
        self.assertEqual(len(calls), 1)
        self.assertIn("out of time before restart 2", rec["detail"])

    def test_gpu_expected_reads_the_pci_bus(self):
        d = tempfile.mkdtemp()
        try:
            self.assertFalse(o1sleep.gpu_expected(d))
            dev = os.path.join(d, "bus/pci/devices/0000:03:00.0")
            os.makedirs(dev)
            open(os.path.join(dev, "class"), "w").write("0x030000\n")
            open(os.path.join(dev, "vendor"), "w").write("0x1002\n")
            self.assertTrue(o1sleep.gpu_expected(d))
            open(os.path.join(dev, "vendor"), "w").write("0x10de\n")
            self.assertFalse(o1sleep.gpu_expected(d))
        finally:
            shutil.rmtree(d)

    def test_models_probe(self):
        d = tempfile.mkdtemp()
        try:
            self.assertFalse(o1sleep.models_ok(d))     # an empty, unmounted mount point
            self.assertFalse(o1sleep.models_ok(os.path.join(d, "missing")))
            open(os.path.join(d, "blob"), "w").write("x")
            self.assertTrue(o1sleep.models_ok(d))
        finally:
            shutil.rmtree(d)

    def test_the_unit_waits_for_the_card_tune_and_has_time_for_it(self):
        u = open(os.path.join(U.KIT, "systemd", "ollama1-resume-check.service")).read()
        self.assertIn("After=ollama.service ollama1-gpu-tune.service", u)
        self.assertRegex(u, r"TimeoutStartSec=(1[0-9]|[2-9][0-9])min")

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
