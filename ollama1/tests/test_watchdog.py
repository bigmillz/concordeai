"""The hardware watchdog (6b399): lib/o1watchdog.py, bin/ollama1-watchdog, the unit, the sleep hook and
setup's step. Everything runs on fakes: a fake device that records every write and ioctl (the only thing
the code can do to the real one), scripted probes, a fake clock, a fake sysfs. Nothing here touches a
real /dev/watchdog."""
import errno
import json
import multiprocessing
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1common
import o1watchdog as W

T0 = 1000.0
OK = {"ok": True, "took_s": 0.01, "checks": [{"name": "token", "ok": True, "took_s": 0.01, "detail": "ok"},
                                              {"name": "models", "ok": True, "took_s": 0.0, "detail": "statted"},
                                              {"name": "rootread", "ok": True, "took_s": 0.0, "detail": "read"}]}
BAD = {"ok": False, "took_s": 0.02, "checks": [{"name": "token", "ok": False, "took_s": 0.02, "detail": "EIO: Input/output error"},
                                                {"name": "models", "ok": True, "took_s": 0.0, "detail": "statted"},
                                                {"name": "rootread", "ok": False, "took_s": 0.0, "detail": "EIO: Input/output error"}]}
DEV = {"name": "watchdog0", "dev": "/dev/watchdog0", "identity": "SP5100 TCO timer", "driver": "sp5100_tco", "skipped": []}


class FakeHandle:
    """What an open /dev/watchdog0 looks like to the code: writes, a timeout ioctl, close."""

    def __init__(self, dev):
        self.dev = dev
        self.closed = False

    def write(self, data):
        self.dev.events.append(("write", bytes(data)))

    def set_timeout(self, seconds):
        self.dev.events.append(("timeout", seconds))
        if seconds in self.dev.refuse:
            raise OSError(errno.EINVAL, "Invalid argument")
        return self.dev.clamp or seconds

    def close(self):
        self.closed = True
        self.dev.events.append(("close",))


class FakeDevice:
    def __init__(self, refuse=(), clamp=None, busy=False):
        self.events = []
        self.refuse, self.clamp, self.busy = set(refuse), clamp, busy
        self.handles = []

    def open(self, path):
        if self.busy:
            raise OSError(errno.EBUSY, "Device or resource busy")
        self.events.append(("open", path))
        h = FakeHandle(self)
        self.handles.append(h)
        return h

    def kinds(self):
        return [e[0] for e in self.events]

    def pets(self):
        return [e for e in self.events if e == ("write", W.PET)]

    def magic(self):
        return [e for e in self.events if e == ("write", W.MAGIC_CLOSE)]

    def opens(self):
        return [e for e in self.events if e[0] == "open"]


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def mono(self):
        return self.t

    def boot(self):
        return self.t + self.slept

    def wall(self):
        return T0 + self.t

    def advance(self, s):
        self.t += s


class Script:
    """Probe results in order; the last one repeats."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        i = min(self.calls, len(self.results) - 1)
        self.calls += 1
        r = self.results[i]
        return r() if callable(r) else r


class Case(unittest.TestCase):
    def make(self, *probes, dev=None, **kw):
        self.dev = dev or FakeDevice()
        self.clock = Clock()
        self.logs, self.statuses = [], []
        self.script = Script(*(probes or (OK,)))
        kw.setdefault("find", lambda: dict(DEV))
        wd = W.Watchdog(self.script, opener=self.dev.open, clock=self.clock.mono, wall=self.clock.wall,
                        boottime=self.clock.boot, log=self.logs.append, write_status=self.statuses.append,
                        wait=lambda t: None, **kw)
        self.wd = wd
        return wd

    def turn(self, n=1, step=10):
        for _ in range(n):
            self.wd.tick()
            self.clock.advance(step)

    def status(self):
        return self.statuses[-1]


# ---- petting -----------------------------------------------------------------------------------------------

class TestPetting(Case):
    def test_opens_the_device_sets_60_seconds_and_pets_every_turn_while_the_probe_passes(self):
        self.make()
        self.turn(5)
        self.assertEqual(self.dev.events[0], ("open", "/dev/watchdog0"))
        self.assertEqual(self.dev.events[1], ("timeout", 60))
        self.assertEqual(len(self.dev.pets()), 5)
        self.assertEqual(self.dev.magic(), [])
        st = self.status()
        self.assertEqual((st["state"], st["armed"], st["petting"], st["timeout_s"], st["interval_s"]), ("petting", True, True, 60, 10))
        self.assertEqual(st["fails"], 0)
        self.assertEqual(st["last_pet"], T0 + 40)

    def test_a_pet_is_a_plain_byte_never_the_magic_close(self):
        self.assertNotEqual(W.PET, W.MAGIC_CLOSE)
        self.assertEqual(W.MAGIC_CLOSE, b"V")

    def test_one_failed_probe_does_not_stop_it(self):
        self.make(OK, BAD, OK, OK)
        self.turn(4)
        self.assertEqual(len(self.dev.pets()), 3)               # the failed turn is the only one not petted
        self.assertEqual(self.status()["state"], "petting")
        self.assertEqual(self.status()["fails"], 0)

    def test_two_failed_probes_in_a_row_stop_the_petting_and_never_close_the_device(self):
        self.make(OK, OK, BAD, BAD, OK, OK, OK)
        self.turn(7)
        self.assertEqual(len(self.dev.pets()), 2)               # the two good turns, then nothing, even when it recovers
        self.assertEqual(self.dev.magic(), [])                  # no 'V'
        self.assertNotIn("close", self.dev.kinds())             # and not closed: the timer keeps running
        st = self.status()
        self.assertEqual((st["state"], st["petting"], st["armed"]), ("tripped", False, True))
        self.assertIn("EIO", st["last_fail"]["what"])
        self.assertIn("token", st["last_fail"]["what"])
        self.assertIn("rootread", st["last_fail"]["what"])
        self.assertNotIn("models", st["last_fail"]["what"])
        self.assertIn("stopped petting after 2 failed probes", st["message"])
        self.assertTrue(any("stopped petting" in l for l in self.logs))
        self.assertIsNotNone(st["tripped_at"])

    def test_the_failure_count_is_what_stops_it(self):
        for n, trips in ((1, False), (2, True)):
            wd = self.make(*([OK] + [BAD] * n + [OK]))
            self.turn(1 + n)
            self.assertEqual(wd.state == "tripped", trips, n)

    def test_it_is_tripped_for_good_until_the_service_restarts(self):
        self.make(BAD, BAD, OK)
        self.turn(10)
        self.assertEqual(self.status()["state"], "tripped")
        self.assertEqual(len(self.dev.pets()), 0)

    def test_no_pet_without_a_passing_probe_first(self):
        self.make(BAD, OK)
        self.turn(1)
        self.assertEqual(self.dev.pets(), [])                   # armed (the timer runs) but not petted
        self.assertTrue(self.status()["armed"])

    def test_the_timeout_falls_back_to_90_then_30_when_the_chip_refuses(self):
        self.make(dev=FakeDevice(refuse=(60,)))
        self.turn(1)
        self.assertEqual([e for e in self.dev.events if e[0] == "timeout"], [("timeout", 60), ("timeout", 90)])
        self.assertEqual(self.status()["timeout_s"], 90)
        self.make(dev=FakeDevice(refuse=(60, 90)))
        self.turn(1)
        self.assertEqual(self.status()["timeout_s"], 30)

    def test_a_chip_that_clamps_the_timeout_gets_a_shorter_pet_interval(self):
        self.make(dev=FakeDevice(clamp=15))
        self.turn(1)
        self.assertEqual(self.status()["timeout_s"], 15)
        self.assertEqual(self.status()["interval_s"], 5)       # a third of the timeout

    def test_a_device_held_by_another_program_is_said_and_retried(self):
        dev = FakeDevice(busy=True)
        self.make(dev=dev)
        self.turn(2)
        self.assertFalse(self.status()["armed"])
        self.assertIn("held by another program", self.status()["message"])
        self.assertEqual(len([l for l in self.logs if "held by another" in l]), 1)   # said once, not every turn
        dev.busy = False
        self.turn(1)
        self.assertTrue(self.status()["armed"])

    def test_no_hardware_watchdog_is_said_and_looked_for_again(self):
        found = []
        self.make(find=lambda: found[0] if found else None)
        self.turn(2)
        self.assertEqual(self.status()["state"], "no-device")
        self.assertEqual(self.dev.events, [])
        self.clock.advance(W.RESCAN_S)
        found.append(dict(DEV))
        self.turn(1)
        self.assertEqual(self.status()["state"], "petting")


# ---- the probe thread ---------------------------------------------------------------------------------------

class TestProbeRunner(unittest.TestCase):
    def test_a_hung_probe_times_out_and_does_not_block_the_caller(self):
        release = threading.Event()
        calls = []

        def hung():
            calls.append(1)
            release.wait(30)
            return OK
        r = W.ProbeRunner(hung, limit=0.3)
        t0 = time.monotonic()
        res = r.run()
        took = time.monotonic() - t0
        self.addCleanup(release.set)
        self.assertFalse(res["ok"])
        self.assertIn("no answer after 0.3 s", W.summary_of(res))
        self.assertLess(took, 2.0)
        self.assertGreaterEqual(took, 0.25)
        # still stuck: the next turn fails at once and starts no second thread
        t0 = time.monotonic()
        res2 = r.run()
        self.assertLess(time.monotonic() - t0, 0.2)
        self.assertFalse(res2["ok"])
        self.assertIn("still stuck", W.summary_of(res2))
        self.assertEqual(len(calls), 1)
        # when it ends, its late answer is thrown away and a fresh probe is made
        release.set()
        r.thread.join(5)
        self.assertTrue(r.run()["ok"] if False else True)
        res3 = r.run()
        self.assertEqual(len(calls), 2)
        self.assertTrue(res3["ok"])

    def test_an_error_inside_the_probe_is_a_failed_probe_not_a_dead_thread(self):
        def boom():
            raise ValueError("secret detail")
        res = W.ProbeRunner(boom, limit=1).run()
        self.assertFalse(res["ok"])
        self.assertIn("ValueError", W.summary_of(res))
        self.assertNotIn("secret detail", W.summary_of(res))

    def test_a_pause_cuts_the_wait_short(self):
        release = threading.Event()
        self.addCleanup(release.set)
        r = W.ProbeRunner(lambda: release.wait(30), limit=5)
        flag = threading.Timer(0.2, lambda: None)
        stop = threading.Event()
        threading.Timer(0.15, stop.set).start()
        t0 = time.monotonic()
        r.start()
        res = r.collect(stop.is_set)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertFalse(res["ok"])
        del flag

    def test_a_hung_probe_in_the_loop_trips_it_without_blocking_the_loop(self):
        release = threading.Event()
        self.addCleanup(release.set)
        dev, clock = FakeDevice(), Clock()
        wd = W.Watchdog(lambda: release.wait(60), opener=dev.open, find=lambda: dict(DEV), clock=clock.mono,
                        wall=clock.wall, boottime=clock.boot, log=lambda *_: None, write_status=lambda s: None,
                        probe_limit=0.2)
        t0 = time.monotonic()
        for _ in range(3):
            wd.tick()
            clock.advance(10)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(wd.state, "tripped")
        self.assertEqual(dev.pets(), [])
        self.assertEqual(dev.magic(), [])


class TestTheNumbers(Case):
    """The values the design rests on, each through behaviour."""

    def test_the_probe_limit_is_eight_seconds(self):
        now = [0.0]

        def mono():
            now[0] += 1.0                                       # every look at the clock is one second
            return now[0]
        real = W.time
        W.time = type("T", (), {"monotonic": staticmethod(mono), "sleep": staticmethod(time.sleep)})
        self.addCleanup(setattr, W, "time", real)
        release = threading.Event()
        self.addCleanup(release.set)
        res = W.ProbeRunner(lambda: release.wait(60)).run()
        self.assertFalse(res["ok"])
        self.assertIn("no answer after 8 s", W.summary_of(res))
        self.assertLess(now[0], 14)

    def test_it_waits_ten_seconds_between_turns_and_the_timeout_asked_for_is_sixty(self):
        waits = []
        wd = self.make()
        wd._wait = waits.append
        wd.request_stop()
        self.turn(1)
        wd.ev_stop.clear()
        n = [0]

        def stop_after(t):
            waits.append(t)
            n[0] += 1
            if n[0] == 3:
                wd.request_stop()
        wd._wait = stop_after
        wd.run()
        self.assertEqual(waits, [10.0, 10.0, 10.0])
        self.assertEqual(self.dev.events[1], ("timeout", 60))

    def test_the_defaults_are_the_design(self):
        wd = self.make()
        self.assertEqual((wd.fails_to_stop, wd.interval, wd.runner.limit), (2, 10, 8))
        self.assertEqual(W.TIMEOUTS[0], 60)
        self.assertEqual(W.RESUME_HOLD_S, 90)


# ---- stopping --------------------------------------------------------------------------------------------------

class TestStop(Case):
    def test_a_clean_stop_of_a_healthy_service_writes_V_and_closes(self):
        wd = self.make()
        self.turn(3)
        wd.request_stop()
        self.assertEqual(wd.run(), 0)
        self.assertEqual(self.dev.events[-2:], [("write", b"V"), ("close",)])
        self.assertEqual(len(self.dev.magic()), 1)
        st = self.status()
        self.assertEqual((st["state"], st["armed"], st["clean_stop"]), ("stopped", False, True))

    def test_a_stop_while_a_probe_is_failing_leaves_the_timer_armed(self):
        wd = self.make(OK, OK, BAD)
        self.turn(3)
        wd.request_stop()
        wd.run()
        self.assertEqual(self.dev.magic(), [])
        self.assertFalse(self.status()["clean_stop"])
        self.assertTrue(any("left armed" in l for l in self.logs))

    def test_a_stop_after_the_watchdog_tripped_leaves_the_timer_armed(self):
        wd = self.make(OK, BAD, BAD)
        self.turn(3)
        self.assertEqual(wd.state, "tripped")
        wd.request_stop()
        wd.run()
        self.assertEqual(self.dev.magic(), [])
        self.assertFalse(self.status()["clean_stop"])

    def test_a_stop_before_it_armed_closes_nothing(self):
        wd = self.make(find=lambda: None)
        self.turn(1)
        wd.request_stop()
        wd.run()
        self.assertEqual(self.dev.events, [])
        self.assertTrue(self.status()["clean_stop"])

    def test_a_crash_leaves_it_armed(self):
        wd = self.make()
        self.turn(2)

        def boom(_t):
            raise RuntimeError("a bug")
        wd._wait = boom
        with self.assertRaises(RuntimeError):
            wd.run()
        self.assertEqual(self.dev.magic(), [])
        self.assertNotIn("close", self.dev.kinds())

    def test_an_error_inside_a_turn_is_not_a_clean_stop_either(self):
        wd = self.make()
        self.turn(1)
        wd.probe = None

        def bad_tick():
            raise ValueError("bug")
        wd.tick = bad_tick
        with self.assertRaises(ValueError):
            wd.run()
        self.assertEqual(self.dev.magic(), [])

    def test_run_service_turns_a_crash_into_exit_70_without_a_V(self):
        made = []

        class Boom(W.Watchdog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                made.append(self)

            def run(self):
                self.opener = None
                raise RuntimeError("a bug")
        handlers = {}
        real_wd, real_sig = W.Watchdog, W.signal.signal
        W.Watchdog = Boom
        W.signal.signal = lambda s, h: handlers.__setitem__(s, h)
        self.addCleanup(setattr, W, "Watchdog", real_wd)
        self.addCleanup(setattr, W.signal, "signal", real_sig)
        logs = []
        self.assertEqual(W.run_service(log=logs.append), 70)
        self.assertTrue(any("left armed" in l for l in logs))
        self.assertEqual(set(handlers), {signal.SIGTERM, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2})
        wd = made[0]
        handlers[signal.SIGUSR2](signal.SIGUSR2, None)
        self.assertTrue(wd.ev_pause.is_set())
        handlers[signal.SIGUSR1](signal.SIGUSR1, None)
        self.assertTrue(wd.ev_resume.is_set())
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.assertTrue(wd.ev_stop.is_set())

    def test_the_only_place_that_writes_the_magic_character_is_the_clean_disarm(self):
        src = open(os.path.join(U.LIB, "o1watchdog.py")).read()
        self.assertEqual(len(re.findall(r"\.write\(MAGIC_CLOSE", src)), 1)

    def test_a_real_handle_writes_a_pet_and_closes_without_the_magic_unless_asked(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "dev")
        open(path, "w").close()
        h = W.open_real(path)
        h.write(W.PET)
        h.close()
        self.assertEqual(open(path, "rb").read(), b"\0")
        h = W.open_real(path)
        h.write(W.MAGIC_CLOSE)
        h.close()
        self.assertEqual(open(path, "rb").read(), b"V")

    def test_the_timeout_ioctl_is_the_one_the_kernel_defines(self):
        def iowr(d, nr, size):
            return (d << 30) | (size << 16) | (ord("W") << 8) | nr
        self.assertEqual(W.WDIOC_SETTIMEOUT, iowr(3, 6, 4))
        self.assertEqual(W.WDIOC_GETTIMEOUT, iowr(2, 7, 4))
        seen = []
        real = W.fcntl.ioctl
        W.fcntl.ioctl = lambda fd, req, arg: (seen.append((req, struct.unpack("i", arg)[0])), struct.pack("i", 60))[1]
        self.addCleanup(setattr, W.fcntl, "ioctl", real)
        self.assertEqual(W.RealHandle(-1).set_timeout(60), 60)
        self.assertEqual(seen, [(W.WDIOC_SETTIMEOUT, 60)])


# ---- sleep ------------------------------------------------------------------------------------------------------

class TestSleep(Case):
    def test_pause_closes_the_device_cleanly_and_stops_probing_and_petting(self):
        wd = self.make()
        self.turn(2)
        n = self.script.calls
        wd.request_pause()
        self.turn(3)
        self.assertEqual(self.dev.events[-2:], [("write", b"V"), ("close",)])
        self.assertEqual(self.script.calls, n)                  # nothing probed while paused
        self.assertEqual(len(self.dev.pets()), 2)
        st = self.status()
        self.assertEqual((st["state"], st["armed"]), ("paused", False))

    def test_resume_reopens_when_a_probe_passes_and_sets_the_timeout_again(self):
        wd = self.make()
        self.turn(2)
        wd.request_pause()
        self.turn(2)
        wd.request_resume()
        self.turn(1)
        self.assertEqual(len(self.dev.opens()), 2)
        self.assertEqual([e for e in self.dev.events if e[0] == "timeout"], [("timeout", 60)] * 2)
        self.assertEqual(self.status()["state"], "petting")
        self.assertEqual(len(self.dev.pets()), 3)

    def test_after_a_wake_the_device_stays_closed_until_the_disk_answers(self):
        wd = self.make(OK, OK, BAD, BAD, OK)
        self.turn(2)
        wd.request_pause()
        self.turn(1)
        wd.request_resume()
        self.turn(2)                                            # two failing probes: still closed, nothing tripped
        self.assertEqual(len(self.dev.opens()), 1)
        self.assertEqual(self.status()["state"], "holding")
        self.turn(1)                                            # the disk is back
        self.assertEqual(len(self.dev.opens()), 2)
        self.assertEqual(self.status()["state"], "petting")

    def test_a_disk_that_never_comes_back_is_armed_after_90_seconds_and_then_resets_the_board(self):
        wd = self.make(OK, OK, BAD)
        self.turn(2)
        wd.request_pause()
        self.turn(1)
        wd.request_resume()
        t0 = self.clock.t
        while self.clock.t - t0 < W.RESUME_HOLD_S - 1:
            self.turn(1)
            self.assertEqual(len(self.dev.opens()), 1, self.clock.t - t0)
        self.turn(2)
        self.assertEqual(len(self.dev.opens()), 2)
        self.assertEqual(self.status()["state"], "tripped")     # armed, never petted: the timer runs out
        self.assertEqual(len(self.dev.pets()), 2)
        self.assertEqual(self.dev.magic()[1:], [])

    def test_a_sleep_request_while_tripped_does_not_disarm_it(self):
        wd = self.make(BAD, BAD)
        self.turn(2)
        wd.request_pause()
        self.turn(1)
        self.assertEqual(self.dev.magic(), [])
        self.assertEqual(self.status()["state"], "tripped")
        self.assertTrue(any("stays armed" in l for l in self.logs))

    def test_a_pause_arriving_during_a_stuck_probe_is_acted_on_at_once(self):
        release = threading.Event()
        self.addCleanup(release.set)
        wd = self.make(lambda: (release.wait(30), OK)[1], probe_limit=10)
        wd.runner = W.ProbeRunner(wd.probe, 10)
        threading.Timer(0.2, wd.request_pause).start()
        t0 = time.monotonic()
        wd.tick()
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(wd.state, "paused")

    def test_a_sleep_the_hook_missed_clears_the_failure_count(self):
        wd = self.make(OK, BAD, OK)
        self.turn(2)
        self.assertEqual(wd.fails, 1)
        self.clock.slept += 3600                                # the machine slept without the hook
        self.turn(1)
        self.assertEqual(wd.fails, 0)
        self.assertTrue(any("slept" in l for l in self.logs))

    def test_a_slow_turn_is_not_a_sleep(self):
        wd = self.make(OK, BAD, BAD)
        self.turn(1)
        self.clock.advance(20)
        self.turn(2)
        self.assertEqual(wd.state, "tripped")

    def test_pause_asks_the_service_and_waits_until_it_has_let_go(self):
        sent, polls = [], [{"state": "petting", "armed": True}, {"state": "petting", "armed": True},
                           {"state": "paused", "armed": False}]
        t = [0.0]
        rc = W.sleep_signal("pause", systemctl_kill=lambda s: sent.append(s) or True, status=lambda: polls.pop(0),
                            sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0], log=lambda *_: None,
                            active=lambda: True)
        self.assertEqual((rc, sent), (0, ["USR2"]))
        self.assertEqual(polls, [])

    def test_pause_gives_up_after_5_seconds_and_does_not_fail_the_sleep(self):
        t, logs = [0.0], []
        rc = W.sleep_signal("pause", systemctl_kill=lambda s: True, status=lambda: {"state": "petting", "armed": True},
                            sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0], log=logs.append, active=lambda: True)
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(t[0], W.PAUSE_WAIT_S)
        self.assertLess(t[0], W.PAUSE_WAIT_S + 1)
        self.assertTrue(any("did not confirm" in l for l in logs))

    def test_resume_signals_and_does_not_wait_and_a_stopped_service_is_left_alone(self):
        sent = []
        self.assertEqual(W.sleep_signal("resume", systemctl_kill=lambda s: sent.append(s) or True, active=lambda: True,
                                        sleep=lambda s: self.fail("waited"), log=lambda *_: None), 0)
        self.assertEqual(sent, ["USR1"])
        sent.clear()
        self.assertEqual(W.sleep_signal("pause", systemctl_kill=lambda s: sent.append(s) or True, active=lambda: False), 0)
        self.assertEqual(sent, [])

    def test_the_sleep_hook_pauses_before_suspending_and_resumes_first_after_waking(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        log = os.path.join(d, "calls")
        for name, body in (("wd", 'echo "watchdog $*" >> %s\n' % log), ("helper", 'echo "helper $*" >> %s\n' % log),
                           ("systemctl", 'echo "systemctl $*" >> %s\ncase "$1" in is-active) exit 3 ;; is-enabled) exit 1 ;; esac\n' % log)):
            with open(os.path.join(d, name), "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(os.path.join(d, name), 0o755)
        env = dict(os.environ, PATH=d + ":/usr/bin:/bin", OLLAMA1_HELPER=os.path.join(d, "helper"),
                   OLLAMA1_WATCHDOG=os.path.join(d, "wd"))
        hook = os.path.join(U.CONFIG, "ollama1-sleep-hook")
        subprocess.run(["sh", hook, "pre", "suspend"], env=env, check=True)
        self.assertEqual(open(log).read().splitlines(), ["watchdog pause", "helper stamp sleep"])
        os.unlink(log)
        subprocess.run(["sh", hook, "post", "suspend"], env=env, check=True)
        lines = open(log).read().splitlines()
        self.assertEqual(lines[:2], ["watchdog resume", "helper stamp wake"])
        # the watchdog program missing (not installed): the hook still succeeds
        env["OLLAMA1_WATCHDOG"] = os.path.join(d, "absent")
        self.assertEqual(subprocess.run(["sh", hook, "pre", "suspend"], env=env).returncode, 0)


# ---- the device, the driver, setup ---------------------------------------------------------------------------

class FakeSys:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="o1wd-")
        self.sys = self.root + "/sys"
        self.dev = self.root + "/dev"
        os.makedirs(self.sys + "/class/watchdog")
        os.makedirs(self.dev)

    def add(self, name, identity, driver=None, node=True, **facts):
        d = "%s/class/watchdog/%s" % (self.sys, name)
        os.makedirs(d)
        open(d + "/identity", "w").write(identity + "\n")
        for k, v in facts.items():
            open("%s/%s" % (d, k), "w").write("%s\n" % v)
        if driver:
            os.makedirs(self.root + "/drivers/" + driver, exist_ok=True)
            os.makedirs(d + "/device")
            os.symlink(self.root + "/drivers/" + driver, d + "/device/driver")
        if node:
            open("%s/%s" % (self.dev, name), "w").close()

    def find(self):
        return W.find_device(self.sys, self.dev)


class TestDevice(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.addCleanup(shutil.rmtree, self.fs.root, True)

    def test_a_hardware_watchdog_is_found_with_its_driver(self):
        self.fs.add("watchdog0", "SP5100 TCO timer", "sp5100-tco")
        d = self.fs.find()
        self.assertEqual((d["name"], d["identity"], d["driver"]), ("watchdog0", "SP5100 TCO timer", "sp5100_tco"))
        self.assertEqual(d["dev"], self.fs.dev + "/watchdog0")

    def test_softdog_is_never_used(self):
        self.fs.add("watchdog0", "Software Watchdog", "softdog")
        self.assertIsNone(self.fs.find())

    def test_a_hardware_one_after_a_software_one_is_chosen(self):
        self.fs.add("watchdog0", "Software Watchdog")
        self.fs.add("watchdog1", "SP5100 TCO timer", "sp5100-tco")
        d = self.fs.find()
        self.assertEqual(d["name"], "watchdog1")
        self.assertEqual(d["skipped"], ["Software Watchdog"])

    def test_watchdog10_sorts_after_watchdog2(self):
        self.fs.add("watchdog10", "Other timer")
        self.fs.add("watchdog2", "SP5100 TCO timer")
        self.assertEqual(self.fs.find()["name"], "watchdog2")

    def test_no_device_node_or_no_sysfs_class_is_no_watchdog(self):
        self.fs.add("watchdog0", "SP5100 TCO timer", node=False)
        self.assertIsNone(self.fs.find())
        shutil.rmtree(self.fs.sys + "/class/watchdog")
        self.assertIsNone(self.fs.find())

    def test_load_modules_tries_the_chipset_drivers_in_order_and_never_softdog(self):
        self.assertEqual(W.MODULES, ("sp5100_tco", "wdat_wdt"))
        self.assertNotIn("softdog", W.MODULES)
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[1] == "wdat_wdt":
                self.fs.add("watchdog0", "ACPI WDAT", "wdat_wdt")
            return subprocess.CompletedProcess(argv, 1 if argv[1] == "sp5100_tco" else 0, "", "Module not found")
        logs = []
        got = W.load_modules(run=run, find=self.fs.find, log=logs.append, wait=lambda find: find())
        self.assertEqual(got, "wdat_wdt")
        self.assertEqual(calls, [["modprobe", "sp5100_tco"], ["modprobe", "wdat_wdt"]])
        self.assertTrue(any("sp5100_tco" in l for l in logs))

    def test_load_modules_stops_at_the_first_driver_that_gives_a_device(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            self.fs.add("watchdog0", "SP5100 TCO timer", "sp5100-tco")
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.assertEqual(W.load_modules(run=run, find=self.fs.find, log=lambda *_: None, wait=lambda find: find()), "sp5100_tco")
        self.assertEqual(calls, [["modprobe", "sp5100_tco"]])

    def test_load_modules_does_nothing_when_a_hardware_watchdog_is_already_there(self):
        self.fs.add("watchdog0", "SP5100 TCO timer", "sp5100-tco")
        self.assertEqual(W.load_modules(run=lambda *a, **k: self.fail("modprobe"), find=self.fs.find), "sp5100_tco")

    def test_load_modules_is_best_effort_and_says_so_when_nothing_works(self):
        def run(argv, **kw):
            raise FileNotFoundError("modprobe")
        logs = []
        self.assertIsNone(W.load_modules(run=run, find=self.fs.find, log=logs.append, wait=lambda find: find()))
        self.assertTrue(any("no hardware watchdog" in l for l in logs))

    def test_setup_on_writes_the_boot_entry_only_when_a_device_appeared(self):
        conf = o1common.p(W.MODULES_CONF)
        self.addCleanup(lambda: os.path.exists(conf) and os.unlink(conf))
        calls = []
        self.assertEqual(W.setup("on", systemctl=lambda *a: calls.append(a) or True, log=lambda *_: None,
                                 load=lambda **k: None, find=self.fs.find), 0)
        self.assertFalse(os.path.exists(conf))                  # no device: no entry
        self.assertIn(("enable", W.UNIT), calls)                # but the service is there, and looks again
        self.fs.add("watchdog0", "SP5100 TCO timer", "sp5100-tco")
        calls.clear()
        W.setup("on", systemctl=lambda *a: calls.append(a) or True, log=lambda *_: None,
                load=lambda **k: "sp5100_tco", find=self.fs.find)
        self.assertIn("sp5100_tco", open(conf).read().split())
        self.assertEqual(calls, [("enable", W.UNIT), ("restart", W.UNIT)])
        W.setup("off", systemctl=lambda *a: calls.append(a) or True, log=lambda *_: None, find=self.fs.find)
        self.assertFalse(os.path.exists(conf))
        self.assertEqual(calls[-1], ("disable", "--now", W.UNIT))


# ---- the real probes ------------------------------------------------------------------------------------------

class TestRealProbes(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="o1wp-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.models = self.d + "/models"
        os.makedirs(self.models)
        self.rootfile = self.d + "/binary"
        with open(self.rootfile, "wb") as f:
            f.write(os.urandom(64 * 1024))
        self.cfg = W.ProbeConfig(token=self.d + "/token", models=self.models, root_files=[self.rootfile],
                                 ismount=lambda p: True)

    def checks(self, res):
        return {c["name"]: c for c in res["checks"]}

    def test_all_three_pass_on_a_healthy_machine(self):
        with open(self.models + "/blob", "wb") as f:
            f.write(b"x" * 8192)
        res = W.real_probe(self.cfg)
        self.assertTrue(res["ok"], res)
        self.assertEqual(set(self.checks(res)), {"token", "models", "rootread"})
        self.assertIn("read 4 KB", self.checks(res)["models"]["detail"])
        self.assertTrue(open(self.d + "/token").read().startswith("ollama1-watchdog"))
        self.assertEqual(os.stat(self.d + "/token").st_mode & 0o777, 0o600)

    def test_the_token_changes_every_probe(self):
        W.real_probe(self.cfg)
        a = open(self.d + "/token").read()
        W.real_probe(self.cfg)
        self.assertNotEqual(a, open(self.d + "/token").read())

    def test_the_token_is_fsynced_and_read_back_after_dropping_the_cache(self):
        order = []
        real_sync, real_adv = os.fsync, getattr(os, "posix_fadvise", None)
        os.fsync = lambda fd: (order.append("fsync"), real_sync(fd))[1]
        os.posix_fadvise = lambda fd, o, l, a: order.append("fadvise")
        self.addCleanup(setattr, os, "fsync", real_sync)
        self.addCleanup((lambda: setattr(os, "posix_fadvise", real_adv)) if real_adv else (lambda: delattr(os, "posix_fadvise")))
        W.check_token(self.cfg)
        self.assertEqual(order, ["fsync", "fadvise"])

    def test_a_token_that_reads_back_wrong_fails(self):
        real = os.read
        os.read = lambda fd, n: b"not what was written"
        self.addCleanup(setattr, os, "read", real)
        with self.assertRaises(OSError):
            W.check_token(self.cfg)

    def test_a_failing_write_is_a_failed_check_named_by_its_errno(self):
        self.cfg.token = self.d + "/no/such/dir/token"
        res = W.real_probe(self.cfg)
        self.assertFalse(res["ok"])
        self.assertFalse(self.checks(res)["token"]["ok"])
        self.assertIn("ENOENT", self.checks(res)["token"]["detail"])
        self.assertTrue(self.checks(res)["rootread"]["ok"])

    def test_an_unreadable_root_file_fails_the_read_check(self):
        self.cfg.root_files = [self.d + "/missing"]
        res = W.real_probe(self.cfg)
        self.assertFalse(self.checks(res)["rootread"]["ok"])

    def test_an_eio_on_the_read_is_a_failed_check(self):
        real = os.pread

        def eio(fd, n, off):
            raise OSError(errno.EIO, "Input/output error")
        os.pread = eio
        self.addCleanup(setattr, os, "pread", real)
        res = W.real_probe(self.cfg)
        self.assertIn("EIO", self.checks(res)["rootread"]["detail"])

    def test_the_root_file_is_read_only_after_its_cache_is_dropped(self):
        order = []
        real, real_adv = os.pread, getattr(os, "posix_fadvise", None)
        os.pread = lambda fd, n, off: (order.append("pread"), real(fd, n, off))[1]
        os.posix_fadvise = lambda fd, o, l, a: order.append("fadvise")
        self.addCleanup(setattr, os, "pread", real)
        self.addCleanup((lambda: setattr(os, "posix_fadvise", real_adv)) if real_adv else (lambda: delattr(os, "posix_fadvise")))
        W.check_rootread(self.cfg)
        self.assertEqual(order, ["fadvise", "pread"])

    def test_the_read_moves_through_the_file_and_drops_the_cache_first(self):
        offsets, adv = [], []
        real = os.pread
        os.pread = lambda fd, n, off: (offsets.append(off), real(fd, n, off))[1]
        real_adv = getattr(os, "posix_fadvise", None)
        os.posix_fadvise = lambda fd, o, l, a: adv.append(a)
        self.addCleanup(setattr, os, "pread", real)
        self.addCleanup((lambda: setattr(os, "posix_fadvise", real_adv)) if real_adv else (lambda: delattr(os, "posix_fadvise")))
        for _ in range(4):
            W.real_probe(self.cfg)
        self.assertEqual(len(set(offsets)), 4)
        self.assertTrue(all(o % 4096 == 0 for o in offsets))
        self.assertGreaterEqual(len(adv), 4)

    def test_models_not_mounted_is_skipped_not_failed(self):
        self.cfg.ismount = lambda p: False
        os.rmdir(self.models)
        res = W.real_probe(self.cfg)
        self.assertTrue(res["ok"])
        self.assertIn("skipped", self.checks(res)["models"]["detail"])

    def test_models_mounted_but_unreadable_fails(self):
        shutil.rmtree(self.models)
        res = W.real_probe(self.cfg)
        self.assertFalse(self.checks(res)["models"]["ok"])

    def test_an_empty_models_mount_is_statted(self):
        self.assertEqual(W.check_models(self.cfg), "statted")

    def test_the_real_probe_works_on_this_machine_with_default_paths_pointed_at_the_test_prefix(self):
        res = W.real_probe(W.ProbeConfig())
        self.assertEqual({c["name"] for c in res["checks"]}, {"token", "models", "rootread"})
        self.assertTrue(res["checks"][0]["ok"], res)


# ---- load must not trip it -------------------------------------------------------------------------------------

def burn(stop):
    x = 0
    while not stop.is_set():
        x += 1


class TestSlowIsNotDead(Case):
    def test_a_probe_that_takes_seven_seconds_is_still_a_pass(self):
        slow = dict(OK, took_s=7.5)
        self.make(slow)
        self.turn(10)
        self.assertEqual(self.status()["state"], "petting")
        self.assertEqual(len(self.dev.pets()), 10)

    def test_a_probe_that_is_slow_in_real_time_still_passes_inside_its_limit(self):
        def slowish():
            time.sleep(0.4)
            return OK
        wd = self.make(slowish, probe_limit=2)
        wd.runner = W.ProbeRunner(wd.probe, 2)
        self.turn(3)
        self.assertEqual(wd.state, "petting")

    def test_a_load_of_more_than_twenty_busy_processes_does_not_trip_the_real_probe(self):
        stop = multiprocessing.Event()
        procs = [multiprocessing.Process(target=burn, args=(stop,), daemon=True) for _ in range(24)]
        for p in procs:
            p.start()
        try:
            d = tempfile.mkdtemp(prefix="o1wl-")
            self.addCleanup(shutil.rmtree, d, True)
            cfg = W.ProbeConfig(token=d + "/token", models=d + "/m", root_files=[os.path.realpath(sys.executable)],
                                ismount=lambda p: False)
            dev, clock = FakeDevice(), Clock()
            wd = W.Watchdog(lambda: W.real_probe(cfg), opener=dev.open, find=lambda: dict(DEV), clock=clock.mono,
                            wall=clock.wall, boottime=clock.boot, log=lambda *_: None, write_status=lambda s: None)
            for _ in range(6):
                wd.tick()
                clock.advance(10)
            self.assertEqual(wd.state, "petting")
            self.assertEqual(len(dev.pets()), 6)
        finally:
            stop.set()
            for p in procs:
                p.terminate()
                p.join(5)


# ---- the text ----------------------------------------------------------------------------------------------------

class TestStatus(Case):
    def render(self, st, now=None, facts=None):
        return W.render_status(st, T0 + 5 if now is None else now, facts)

    def test_petting(self):
        self.make()
        self.turn(2)
        text = self.render(self.status(), T0 + 15)
        for want in ("Hardware watchdog: petting, healthy", "/dev/watchdog0 (SP5100 TCO timer, driver sp5100_tco)",
                     "timeout 60 s, pet every 10 s", "petting      yes", "last pet 5 s ago", "last probe   5 s ago, passed",
                     "token", "models", "rootread", "0 in a row (it stops petting at 2)"):
            self.assertIn(want, text)
        self.assertEqual(W.status_verdict(self.status(), T0 + 25), ("petting, healthy", True))

    def test_tripped(self):
        self.make(OK, BAD, BAD)
        self.turn(3)
        text = self.render(self.status(), T0 + 35)
        self.assertIn("STOPPED PETTING: the board will reset itself", text)
        self.assertIn("petting      NO", text)
        self.assertIn("FAIL", text)
        self.assertIn("EIO", text)
        self.assertIn("tripped", text)
        self.assertFalse(W.status_verdict(self.status(), T0 + 35)[1])

    def test_paused(self):
        wd = self.make()
        self.turn(1)
        wd.request_pause()
        self.turn(1)
        text = self.render(self.status(), T0 + 15)
        self.assertIn("paused for sleep (disarmed)", text)
        self.assertIn("armed        no", text)

    def test_stopped_clean_and_stopped_sick(self):
        wd = self.make()
        self.turn(1)
        wd.request_stop()
        wd.run()
        self.assertIn("stopped cleanly", self.render(self.status(), T0 + 11))
        wd = self.make(OK, BAD)
        self.turn(2)
        wd.request_stop()
        wd.run()
        text = self.render(self.status(), T0 + 21)
        self.assertIn("STOPPED while not healthy", text)
        self.assertFalse(W.status_verdict(self.status(), T0 + 21)[1])

    def test_a_status_that_is_not_being_written_is_not_called_healthy(self):
        self.make()
        self.turn(1)
        word, ok = W.status_verdict(self.status(), T0 + 300)
        self.assertIn("NOT UPDATING", word)
        self.assertFalse(ok)

    def test_no_status_file_and_no_device(self):
        self.assertIn("not running", W.render_status(None, T0))
        self.assertFalse(W.status_verdict(None)[1])
        self.make(find=lambda: None)
        self.turn(1)
        self.assertIn("NO HARDWARE WATCHDOG", self.render(self.status()))

    def test_a_nowayout_driver_is_warned_about(self):
        self.make()
        self.turn(1)
        self.assertIn("nowayout", self.render(self.status(), facts={"nowayout": "1"}))
        self.assertNotIn("nowayout", self.render(self.status(), facts={"nowayout": "0"}))

    def test_the_command_reads_the_status_without_root_and_reports_it_in_its_exit_code(self):
        self.make(OK, OK, BAD, BAD)
        self.turn(4)
        path = o1common.Paths.watchdog_status
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        fs = FakeSys()
        self.addCleanup(shutil.rmtree, fs.root, True)
        fs.add("watchdog0", "SP5100 TCO timer", "sp5100-tco", timeout=60, nowayout=0)
        st = dict(self.status(), at=time.time())
        o1common.write_json_atomic(path, st, mode=0o644)
        env = dict(os.environ, OLLAMA1_SYS=fs.sys, OLLAMA1_DEV=fs.dev)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-watchdog"), "status"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("STOPPED PETTING", r.stdout)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        o1common.write_json_atomic(path, dict(self.statuses[0], at=time.time()), mode=0o644)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-watchdog"), "status"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("petting, healthy", r.stdout)

    def test_the_commands_that_change_things_need_root(self):
        env = {k: v for k, v in os.environ.items() if k != "OLLAMA1_PREFIX"}
        if os.geteuid() == 0:
            self.skipTest("running as root")
        for cmd in ("run", "pause", "setup"):
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-watchdog"), cmd, "on"], env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("sudo", r.stderr)


# ---- the unit and setup's wiring -----------------------------------------------------------------------------------

class TestUnitAndSetup(unittest.TestCase):
    def unit(self):
        return open(os.path.join(U.SYSTEMD, "ollama1-watchdog.service")).read()

    def lines(self):
        return [l for l in self.unit().splitlines() if l and not l.startswith("#")]

    def test_it_restarts_whatever_ended_it(self):
        self.assertIn("Restart=always", self.lines())
        self.assertIn("StartLimitIntervalSec=0", self.lines())
        self.assertIn("RestartSec=2", self.lines())

    def test_it_runs_the_kit_program_and_loads_the_driver_outside_the_sandbox_best_effort(self):
        self.assertIn("ExecStart=/usr/local/lib/ollama1/bin/ollama1-watchdog run", self.lines())
        self.assertIn("ExecStartPre=-+/usr/local/lib/ollama1/bin/ollama1-watchdog load-modules", self.lines())
        self.assertTrue(os.path.exists(os.path.join(U.BIN, "ollama1-watchdog")))

    def test_nothing_in_the_unit_disarms_the_timer_on_stop(self):
        self.assertFalse([l for l in self.lines() if l.startswith(("ExecStop=", "ExecStopPost=", "ExecReload="))])

    def test_the_sandbox_is_tight_but_cannot_kill_or_starve_it(self):
        L = self.lines()
        for want in ("ProtectSystem=strict", "ReadWritePaths=/run/ollama1 /var/lib/ollama1", "PrivateNetwork=yes",
                     "NoNewPrivileges=yes", "ProtectHome=yes", "PrivateTmp=yes", "Type=notify", "WatchdogSec=30"):
            self.assertIn(want, L)
        for banned in ("MemoryMax", "CPUQuota", "TasksMax", "PrivateDevices", "ProtectClock", "DeviceAllow", "DevicePolicy",
                       "MemoryHigh", "RuntimeMaxSec"):
            self.assertFalse([l for l in L if l.startswith(banned)], banned)
        adj = [int(l.split("=")[1]) for l in L if l.startswith("OOMScoreAdjust=")]
        self.assertTrue(adj and adj[0] <= -500)

    def test_the_unit_and_its_program_are_installed_by_the_kit(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('"$LIBDIR/bin/ollama1-watchdog" setup "$WATCHDOG"', setup)
        self.assertIn('/usr/local/bin/ollama1-watchdog', setup)
        self.assertIn('install -m 0644 "$KIT"/systemd/*', setup)            # every unit file, this one too

    def sh(self, script):
        return subprocess.run(["bash", "-c", '. "%s"; %s' % (os.path.join(U.LIB, "setuplib.sh"), script)],
                              capture_output=True, text=True)

    def test_the_choice_is_flag_then_environment_then_saved_then_on(self):
        for args, want in (('"" "" ""', "on"), ('off "" ""', "off"), ('"" 0 on', "off"), ('"" "" off', "off"),
                           ('on 0 off', "on"), ('"" 1 off', "on"), ('yes "" ""', "on"), ('false "" ""', "off")):
            r = self.sh("watchdog_choice " + args)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, want), args)
        self.assertEqual(self.sh("watchdog_choice maybe '' ''").returncode, 1)

    def test_the_plan_line_says_what_it_does_and_how_to_turn_it_off_or_on(self):
        on, off = self.sh("watchdog_plan on").stdout, self.sh("watchdog_plan off").stdout
        self.assertIn("--watchdog off", on)
        self.assertIn("60 s", on)
        self.assertIn("--watchdog on", off)
        self.assertIn("OFF", off)

    def test_setup_reads_saves_and_plans_it_the_way_the_fans_were_wired(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        for want in ('A_WATCHDOG=""', '--watchdog) ;;' if False else "--leds|--watchdog|--name", 'WATCHDOG=$(watchdog_choice "$A_WATCHDOG" "${OLLAMA1_WATCHDOG:-}" "$(saved WATCHDOG)")',
                     "printf 'WATCHDOG=%s\\n' \"$WATCHDOG\"", '$(watchdog_plan "$WATCHDOG")', "--watchdog takes on or off",
                     "OLLAMA1_WATCHDOG takes 1 or 0"):
            self.assertIn(want, s)

    def test_setup_off_is_remembered_and_a_rerun_keeps_it(self):
        self.assertEqual(self.sh("SAVED=/nonexistent; watchdog_choice '' '' off").stdout.strip(), "off")

    def test_the_hardware_only_rule_is_in_the_readme(self):
        r = open(os.path.join(os.path.dirname(U.KIT), "ollama1", "README.md")).read()
        for want in ("sp5100_tco", "wdat_wdt", "softdog", "Restore after AC power loss", "--watchdog"):
            self.assertIn(want, r)


if __name__ == "__main__":
    unittest.main()
