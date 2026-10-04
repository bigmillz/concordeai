"""The server's hardware watchdog (6b399). stdlib only.

The failure this answers: the system NVMe drops off the bus, the machine stays up
from memory (it pings, nothing on disk can be read, every program gives
"Input/output error", SSH resets) and only a power cycle brings it back. A
hardware watchdog does that power cycle by itself: the chipset's timer
(sp5100_tco on an AMD board, wdat_wdt where ACPI describes it) is armed with a
timeout of 60 s and must be petted before it runs out, or the board resets.
softdog is never used: a frozen kernel cannot run it.

What decides whether it is petted (every 10 s): a real health probe, run in a
thread that is given 8 s, so a read stuck on a dead drive cannot stop the loop.
The probe does three things on the root filesystem and the models mount:
  token    write a small token to a file on the root filesystem, fsync it, drop
           it from the page cache and read it back
  models   stat a file on /srv/models (when something is mounted there) and read
           4 KB of it
  rootread read 4 KB of a real file on the root filesystem that is not in the
           page cache (posix_fadvise DONTNEED first, a different offset each time)
Two probes in a row that fail or time out (about 20 s) and the service STOPS
petting, for good until it is restarted: the board resets itself within the
timeout. It writes what failed to the journal (best effort: the journal may be
dead too) and to /run/ollama1/watchdog.json (a tmpfs, it is there until the
reset).

How the device is closed matters. Writing the magic character 'V' and closing
disarms the timer; closing any other way leaves it armed (a crash, a kill, a
bug). So 'V' is written in exactly three places: a deliberate stop of a healthy
service (SIGTERM), a pause before sleep, and nothing else. Whatever else ends
the process leaves the timer running and the board resets.

Sleep (the hook in config/ollama1-sleep-hook): `ollama1-watchdog pause` before
suspend closes the device cleanly and keeps it closed; `resume` after waking
reopens it, but not until a probe passes (the drive may take a few seconds to
come back), or 90 s after the wake at the latest, so a drive that never comes
back still ends in a reset. A system that is merely slow is not tripped: two
probes in a row must fail, and a probe has 8 s (a load of 20 or more is slow, not
dead). A clock jump (the machine slept without the hook) clears the count.
"""
import errno
import fcntl
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import o1common

MODULES = ("sp5100_tco", "wdat_wdt")     # hardware ones only; never softdog
MODULES_CONF = "/etc/modules-load.d/ollama1-watchdog.conf"
UNIT = "ollama1-watchdog.service"

TIMEOUTS = (60, 90, 30)                  # asked for in this order; the first the chip accepts
PET_EVERY_S = 10
PROBE_LIMIT_S = 8
FAILS_TO_STOP = 2
RESUME_HOLD_S = 90                       # after a wake: open at the first passing probe, or at this age
JUMP_S = 25                              # the clock that counts sleep ran this much ahead of the one that doesn't
RESCAN_S = 30                            # no device: look again this often
STALE_S = 40                             # a status file older than this is not being written
PAUSE_WAIT_S = 5                         # how long `pause` waits for the service to let go

MAGIC_CLOSE = b"V"                       # write it, then close: the driver stops the timer
PET = b"\0"                              # any other byte is a keepalive

# <linux/watchdog.h>: _IOWR('W', 6, int) and _IOR('W', 7, int)
WDIOC_SETTIMEOUT = 0xC0045706
WDIOC_GETTIMEOUT = 0x80045707


def status_path():
    return o1common.Paths.watchdog_status


def token_path():
    return o1common.Paths.watchdog_token


def _sys():
    return os.environ.get("OLLAMA1_SYS", "/sys")


def _dev():
    return os.environ.get("OLLAMA1_DEV", "/dev")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


# ---- the device ------------------------------------------------------------------------------------

def is_software(identity):
    """softdog calls itself 'Software Watchdog'. A frozen kernel cannot run it, so it protects nothing here."""
    return "software" in (identity or "").lower()


def find_device(sysroot=None, devroot=None):
    """The first hardware watchdog as {name, dev, identity, driver, skipped}, or None. A software one is
    skipped (and named in `skipped`); a watchdog whose identity cannot be read is not trusted."""
    sysroot = sysroot or _sys()
    devroot = devroot or _dev()
    base = os.path.join(sysroot, "class", "watchdog")
    try:
        names = sorted((n for n in os.listdir(base) if re.fullmatch(r"watchdog\d+", n)), key=lambda n: int(n[8:]))
    except OSError:
        return None
    skipped = []
    for n in names:
        ident = _read(os.path.join(base, n, "identity"))
        if not ident or is_software(ident):
            skipped.append(ident or n)
            continue
        driver = ""
        try:
            driver = os.path.basename(os.readlink(os.path.join(base, n, "device", "driver"))).replace("-", "_")
        except OSError:
            pass
        dev = os.path.join(devroot, n)
        if not os.path.exists(dev):
            skipped.append(n + " (no device node)")
            continue
        return {"name": n, "dev": dev, "identity": ident, "driver": driver, "skipped": skipped}
    return None


def sysfs_facts(name, sysroot=None):
    """What the kernel says about a watchdog (readable without root)."""
    base = os.path.join(sysroot or _sys(), "class", "watchdog", name)
    out = {}
    for k in ("identity", "timeout", "state", "nowayout", "status", "timeleft"):
        v = _read(os.path.join(base, k))
        if v is not None:
            out[k] = v
    return out


class RealHandle:
    """An open /dev/watchdogN. close() is a plain close: it leaves the timer armed. The only way to disarm
    is write(MAGIC_CLOSE) and then close()."""

    def __init__(self, fd):
        self.fd = fd

    def write(self, data):
        os.write(self.fd, data)

    def set_timeout(self, seconds):
        out = fcntl.ioctl(self.fd, WDIOC_SETTIMEOUT, struct.pack("i", int(seconds)))
        return struct.unpack("i", out[:4])[0]

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def open_real(path):
    return RealHandle(os.open(path, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)))


# ---- the probes ------------------------------------------------------------------------------------

class ProbeConfig:
    def __init__(self, token=None, models=None, root_files=None, ismount=None):
        self.ismount = ismount or os.path.ismount
        self.token = token or token_path()
        self.models = models if models is not None else o1common.Paths.models
        self.root_files = root_files or [os.path.realpath(sys.executable), "/bin/sh", "/etc/os-release"]
        self.n = 0


def _drop_cache(fd):
    if hasattr(os, "posix_fadvise"):
        os.posix_fadvise(fd, 0, 0, getattr(os, "POSIX_FADV_DONTNEED", 4))


def check_token(cfg):
    """Write, fsync, drop from the cache, read back: the write path of the root filesystem works."""
    cfg.n += 1
    want = ("ollama1-watchdog %d %d\n" % (os.getpid(), cfg.n)).encode()
    fd = os.open(cfg.token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, want)
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(cfg.token, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        _drop_cache(fd)
        got = os.read(fd, 256)
    finally:
        os.close(fd)
    if got != want:
        raise OSError(errno.EIO, "the token read back is not the token written")
    return "written, synced and read back"


def check_models(cfg):
    """Something is mounted on the models folder: one of its entries can be statted (and, if it is a file,
    4 KB of it read). Nothing mounted there (no models drive, or it is being moved): nothing to check."""
    if not cfg.ismount(cfg.models):
        return "not a mount point: skipped"
    entry = None
    with os.scandir(cfg.models) as it:
        for e in it:
            entry = e.path
            break
    target = entry or cfg.models
    st = os.stat(target)
    if entry and os.path.isfile(entry) and st.st_size >= 4096:
        fd = os.open(entry, os.O_RDONLY)
        try:
            _drop_cache(fd)
            if len(os.read(fd, 4096)) < 4096:
                raise OSError(errno.EIO, "short read")
        finally:
            os.close(fd)
        return "statted and read 4 KB"
    return "statted"


def check_rootread(cfg):
    """4 KB of a real file on the root filesystem, at a different place each time, not from the page cache."""
    for path in cfg.root_files:
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            size = os.fstat(fd).st_size
            if size <= 0:
                continue
            blocks = max(1, size // 4096)
            off = (cfg.n * 7919 % blocks) * 4096 if size > 4096 else 0
            _drop_cache(fd)
            data = os.pread(fd, 4096, off)
            if not data:
                raise OSError(errno.EIO, "read nothing at offset %d of %s" % (off, path))
            return "read %d bytes of %s" % (len(data), os.path.basename(path))
        finally:
            os.close(fd)
    raise OSError(errno.ENOENT, "no file to read on the root filesystem")


CHECKS = (("token", check_token), ("models", check_models), ("rootread", check_rootread))


def real_probe(cfg):
    """One probe: every check, each timed. Never raises: a failure is a result."""
    t0 = time.monotonic()
    out = []
    for name, fn in CHECKS:
        t1 = time.monotonic()
        try:
            detail, ok = fn(cfg), True
        except OSError as e:
            detail, ok = "%s: %s" % (errno.errorcode.get(e.errno, "error"), e.strerror or e), False
        except Exception as e:                                   # a bug in a check is a failure, said by its type
            detail, ok = type(e).__name__, False
        out.append({"name": name, "ok": ok, "took_s": round(time.monotonic() - t1, 3), "detail": detail})
    return {"ok": all(c["ok"] for c in out), "took_s": round(time.monotonic() - t0, 3), "checks": out}


class ProbeRunner:
    """Runs a probe in a daemon thread and waits at most `limit` s for it. A probe still stuck from an earlier
    turn is not joined and not repeated (a pile of threads on a dead drive helps nobody): the turn fails at once,
    and when it finally ends its result is thrown away."""

    def __init__(self, fn, limit=PROBE_LIMIT_S):
        self.fn, self.limit = fn, limit
        self.thread = None
        self.box = None
        self.done = None

    def stuck(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.stuck():
            return False
        box, done = {}, threading.Event()

        def work():
            try:
                box["r"] = self.fn()
            except BaseException as e:                           # never lets a thread die silently
                box["r"] = {"ok": False, "took_s": 0.0, "checks": [{"name": "probe", "ok": False, "took_s": 0.0,
                                                                    "detail": type(e).__name__}]}
            done.set()
        self.box, self.done = box, done
        self.thread = threading.Thread(target=work, name="probe", daemon=True)
        self.thread.start()
        return True

    def collect(self, interrupt=None):
        """The result of the probe `start` began, or a failure that says it timed out or is still stuck."""
        if self.done is None:
            return fail_result("still stuck from an earlier turn", self.limit)
        t0 = time.monotonic()
        while not self.done.is_set():
            left = self.limit - (time.monotonic() - t0)
            if left <= 0 or (interrupt is not None and interrupt()):
                break
            self.done.wait(min(0.05, left))
        if self.done.is_set():
            return self.box["r"]
        return fail_result("no answer after %g s" % self.limit, self.limit)

    def run(self, interrupt=None):
        if not self.start():
            return fail_result("an earlier probe is still stuck", 0.0)
        return self.collect(interrupt)


def fail_result(why, took):
    return {"ok": False, "took_s": round(took, 3), "checks": [{"name": "probe", "ok": False, "took_s": round(took, 3),
                                                                "detail": why}]}


def summary_of(res):
    """What failed, in one line, from a probe result."""
    bad = [c for c in res.get("checks", []) if not c.get("ok")]
    return "; ".join("%s: %s" % (c["name"], c.get("detail", "")) for c in bad) or "ok"


# ---- the state machine -------------------------------------------------------------------------------

def boottime_clock():
    """Time that also counts while the machine sleeps (Linux). Elsewhere the plain monotonic clock."""
    c = getattr(time, "CLOCK_BOOTTIME", None)
    if c is None:
        return time.monotonic
    return lambda: time.clock_gettime(c)


class Watchdog:
    """Opens the device, sets the timeout, and pets it only while probes pass.

    states: starting/holding (device not open yet), petting, tripped (stopped petting; the board resets),
    paused (closed cleanly for sleep), no-device, stopped."""

    def __init__(self, probe, opener=open_real, find=find_device, clock=time.monotonic, wall=time.time,
                 boottime=None, log=print, notify=None, write_status=None, interval=PET_EVERY_S,
                 fails_to_stop=FAILS_TO_STOP, resume_hold=RESUME_HOLD_S, wait=None, probe_limit=PROBE_LIMIT_S,
                 runner=None):
        self.probe = probe
        self.runner = runner or ProbeRunner(probe, probe_limit)
        self.opener, self.find = opener, find
        self.clock, self.wall = clock, wall
        self.boottime = boottime or boottime_clock()
        self.log = log
        self.notify = notify or (lambda msg: None)
        self.write_status_fn = write_status or write_status_file
        self.base_interval = interval
        self.interval = interval
        self.fails_to_stop = fails_to_stop
        self.resume_hold = resume_hold
        self.poke = threading.Event()
        self.ev_stop, self.ev_pause, self.ev_resume = threading.Event(), threading.Event(), threading.Event()
        self.handle = None
        self.dev = None
        self.state = "starting"
        self.timeout = None
        self.fails = 0
        self.last_pet = None
        self.last_probe = None
        self.last_fail = None
        self.last_ok = None
        self.tripped_at = None
        self.hold_until = self.clock()
        self.next_scan = 0.0
        self.message = ""
        self.stopped_clean = None
        self.reasons_logged = set()
        self._wait = wait or (lambda t: (self.poke.wait(t), self.poke.clear()))
        self._last_boot = self.boottime()
        self._last_mono = self.clock()

    # -- requests (signal handlers only set events) --

    def request_stop(self):
        self.ev_stop.set()
        self.poke.set()

    def request_pause(self):
        self.ev_pause.set()
        self.poke.set()

    def request_resume(self):
        self.ev_resume.set()
        self.poke.set()

    @property
    def armed(self):
        return self.handle is not None

    @property
    def petting(self):
        return self.armed and self.state == "petting"

    def _interrupt_pending(self):
        return self.ev_stop.is_set() or self.ev_pause.is_set()

    # -- the device --

    def _open(self, now):
        """Open the device and set the timeout. False (with the reason in self.message) when it cannot be."""
        try:
            h = self.opener(self.dev["dev"])
        except OSError as e:
            if e.errno == errno.EBUSY:
                self.message = ("%s is held by another program (systemd's RuntimeWatchdogSec, or another "
                                "watchdog daemon)" % self.dev["dev"])
            else:
                self.message = "cannot open %s: %s" % (self.dev["dev"], e.strerror or e)
            self._log_once("open", self.message)
            return False
        got = None
        for t in TIMEOUTS:
            try:
                got = h.set_timeout(t)
                break
            except OSError:
                continue
        if got is None:
            self.message = "the chip would not take a timeout of 60, 90 or 30 s; it keeps its own"
            self._log_once("timeout", self.message)
            got = 0
        self.timeout = got or None
        if self.timeout:
            self.interval = max(2, min(self.base_interval, self.timeout // 3))
        self.handle = h
        self.state = "petting"
        self.log("watchdog: armed %s, timeout %s s, pet every %g s while the health probe passes" % (
            self.dev["dev"], self.timeout or "?", self.interval))
        return True

    def _disarm(self):
        """The clean close: the magic character, then close. The only place the timer is stopped."""
        h, self.handle = self.handle, None
        if h is None:
            return
        try:
            h.write(MAGIC_CLOSE)
        finally:
            h.close()

    def _abandon(self):
        """Let go of the device without disarming it: the timer keeps running."""
        h, self.handle = self.handle, None
        if h is not None:
            try:
                h.close()
            except OSError:
                pass

    def _pet(self, now):
        try:
            self.handle.write(PET)
        except OSError as e:
            self.message = "writing to the watchdog failed: %s" % (e.strerror or e)
            self._log_once("pet", self.message)
            return False
        self.last_pet = self.wall()
        return True

    def _log_once(self, key, text):
        if key not in self.reasons_logged:
            self.reasons_logged.add(key)
            self.log("watchdog: " + text)

    # -- the turn --

    def _check_jump(self):
        b, m = self.boottime(), self.clock()
        ahead = (b - self._last_boot) - (m - self._last_mono)
        self._last_boot, self._last_mono = b, m
        if ahead > JUMP_S:
            self.log("watchdog: the machine slept %d s without the sleep hook; failure count cleared" % ahead)
            if self.state != "tripped":
                self.fails = 0

    def handle_requests(self):
        now = self.clock()
        if self.ev_resume.is_set():
            self.ev_resume.clear()
            if self.state == "paused":
                self.state = "holding"
                self.hold_until = now + self.resume_hold
                self.fails = 0
                self.message = ""
                self.log("watchdog: resumed after sleep; waiting for the system disk to answer (at most %d s)" % self.resume_hold)
        if self.ev_pause.is_set():
            self.ev_pause.clear()
            if self.state == "tripped":
                self.log("watchdog: sleep asked while the watchdog is tripped; it stays armed")
            elif self.state != "paused":
                self._disarm()
                self.state = "paused"
                self.message = "paused for sleep"
                self.log("watchdog: closed cleanly for sleep")
        if self.ev_stop.is_set():
            return False
        return True

    def tick(self):
        """One turn: a probe, then a pet if it passed. Returns nothing; everything is in the attributes."""
        now = self.clock()
        self._check_jump()
        if not self.handle_requests():
            return
        if self.state == "paused":
            self._status()
            return
        if self.dev is None:
            if now >= self.next_scan:
                self.next_scan = now + RESCAN_S
                self.dev = self.find()
                if self.dev is None:
                    self.state = "no-device"
                    self.message = "no hardware watchdog (only a software one counts: it cannot run on a frozen kernel)"
                    self._log_once("nodev", self.message)
                    self.hold_until = now
            if self.dev is None:
                self._status()
                return
            self.state = "starting"
            self.message = ""
        started = self.runner.start()
        res = self.runner.collect(self._interrupt_pending) if started else fail_result("an earlier probe is still stuck", 0.0)
        if self._interrupt_pending():                      # a pause or a stop came in while waiting: act on it first
            if not self.handle_requests():
                return
            if self.state == "paused":
                self._status()
                return
        self.last_probe = dict(res, at=self.wall())
        ok = bool(res.get("ok"))
        if ok:
            self.fails = 0 if self.state != "tripped" else self.fails
            self.last_ok = self.wall()
        else:
            self.fails += 1
            self.last_fail = {"at": self.wall(), "what": summary_of(res)}
            self.log("watchdog: probe failed (%d in a row): %s" % (self.fails, self.last_fail["what"]))
        if self.handle is None and self.state in ("starting", "holding", "no-device"):
            if ok or now >= self.hold_until:
                self._open(now)
        if self.handle is not None:
            if self.state == "petting" and self.fails >= self.fails_to_stop:
                self.state = "tripped"
                self.tripped_at = self.wall()
                self.message = ("stopped petting after %d failed probes in a row (%s); the board resets within the timeout"
                                % (self.fails, self.last_fail["what"] if self.last_fail else "?"))
                self.log("watchdog: " + self.message)
            if self.state == "petting" and ok:
                self._pet(now)
        self._status()
        self.notify("WATCHDOG=1\nSTATUS=%s" % self.state)

    # -- status --

    def snapshot(self):
        return {
            "version": 1, "pid": os.getpid(), "at": self.wall(),
            "device": self.dev["dev"] if self.dev else None,
            "identity": self.dev["identity"] if self.dev else None,
            "driver": self.dev["driver"] if self.dev else None,
            "state": self.state, "armed": self.armed, "petting": self.petting,
            "timeout_s": self.timeout, "interval_s": self.interval, "fails": self.fails,
            "fails_to_stop": self.fails_to_stop, "last_pet": self.last_pet, "last_ok": self.last_ok,
            "last_probe": self.last_probe, "last_fail": self.last_fail, "tripped_at": self.tripped_at,
            "message": self.message, "clean_stop": self.stopped_clean,
        }

    def _status(self):
        try:
            self.write_status_fn(self.snapshot())
        except Exception as e:                            # the status file is for people; the loop goes on
            self._log_once("status", "cannot write the status file: %s" % type(e).__name__)

    # -- running --

    def run(self):
        """The loop. Ends at a stop request (then `shutdown`). Anything else that ends it, a bug included,
        leaves the timer armed: the exception goes to the caller and nothing here closes with 'V'."""
        first = True
        while True:
            t0 = self.clock()
            if not self.handle_requests():
                break
            try:
                self.tick()
            except OSError as e:                          # a failing system call in a turn is not a reason to stop
                self.log("watchdog: turn failed: %s" % (errno.errorcode.get(e.errno, "error")))
            if first:
                self.notify("READY=1")
                first = False
            if self.ev_stop.is_set():
                break
            self._wait(max(0.0, self.interval - (self.clock() - t0)))
        self.shutdown()
        return 0

    def shutdown(self):
        """SIGTERM (a `systemctl stop`). A healthy service disarms the timer; one that is failing, or tripped,
        leaves it armed, so stopping a sick machine's watchdog does not save it from the reset."""
        healthy = self.state == "petting" and self.fails == 0 and self.last_probe is not None and self.last_probe.get("ok")
        if self.handle is None:
            self.stopped_clean = True
            self.state = "stopped"
            self.message = "stopped"
        elif healthy:
            self._disarm()
            self.stopped_clean = True
            self.state = "stopped"
            self.message = "stopped cleanly; the timer is disarmed"
            self.log("watchdog: stopped; disarmed")
        else:
            self._abandon()
            self.stopped_clean = False
            self.state = "stopped"
            self.message = "stopped while not healthy; the timer is left armed and the board will reset"
            self.log("watchdog: " + self.message)
        self._status()


def write_status_file(st, path=None):
    path = path or status_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    o1common.write_json_atomic(path, st, mode=0o644)


# ---- reading it back (no root) -------------------------------------------------------------------------

def read_status(path=None):
    return o1common.read_json(path or status_path(), None)


def ago(t, now):
    if t is None:
        return "never"
    s = max(0, int(now - t))
    if s < 90:
        return "%d s ago" % s
    if s < 5400:
        return "%d min ago" % (s // 60)
    return "%.1f h ago" % (s / 3600)


def status_verdict(st, now=None, facts=None):
    """(word, ok) for the first line. ok is False for anything that needs a look."""
    now = time.time() if now is None else now
    if not st:
        return "not running (no status file)", False
    state = st.get("state")
    old = now - (st.get("at") or 0) > STALE_S
    if state == "stopped":
        if st.get("clean_stop"):
            return "stopped cleanly (the timer is disarmed)", True
        return "STOPPED while not healthy: the timer is armed and the board will reset", False
    if old and state != "paused":
        return "NOT UPDATING (the status is %s old: the service is hung or the machine is)" % ago(st.get("at"), now).replace(" ago", ""), False
    if state == "petting":
        return "petting, healthy", True
    if state == "tripped":
        return "STOPPED PETTING: the board will reset itself", False
    if state == "paused":
        return "paused for sleep (disarmed)", True
    if state in ("starting", "holding"):
        return "waiting to arm (the system disk has not answered yet)", True
    if state == "no-device":
        return "NO HARDWARE WATCHDOG on this machine", False
    return str(state), False


def render_status(st, now=None, facts=None):
    """The text of `ollama1-watchdog status`."""
    now = time.time() if now is None else now
    word, ok = status_verdict(st, now)
    lines = ["Hardware watchdog: " + word]
    if not st:
        lines.append("  sudo systemctl status ollama1-watchdog   (setup.sh --watchdog on installs it)")
        if facts:
            lines.append("  the kernel has %s (%s), timeout %s s" % (facts.get("name", "a watchdog"),
                                                                       facts.get("identity", "?"), facts.get("timeout", "?")))
        return "\n".join(lines)
    dev = st.get("device")
    if dev:
        lines.append("  device       %s (%s%s), timeout %s s, pet every %g s" % (
            dev, st.get("identity") or "?", (", driver " + st["driver"]) if st.get("driver") else "",
            st.get("timeout_s") or "?", st.get("interval_s") or 0))
    else:
        lines.append("  device       none")
    lines.append("  armed        %s" % ("yes" if st.get("armed") else "no"))
    lines.append("  petting      %s   (last pet %s)" % ("yes" if st.get("petting") else "NO", ago(st.get("last_pet"), now)))
    lp = st.get("last_probe")
    if lp:
        lines.append("  last probe   %s, %s in %.2f s" % (ago(lp.get("at"), now), "passed" if lp.get("ok") else "FAILED", lp.get("took_s") or 0))
        for c in lp.get("checks", []):
            lines.append("      %-9s %-4s %s" % (c.get("name"), "ok" if c.get("ok") else "FAIL", c.get("detail", "")))
    else:
        lines.append("  last probe   none yet")
    lines.append("  failures     %d in a row (it stops petting at %d)" % (st.get("fails") or 0, st.get("fails_to_stop") or FAILS_TO_STOP))
    if st.get("last_fail"):
        lf = st["last_fail"]
        lines.append("  last failure %s: %s" % (ago(lf.get("at"), now), lf.get("what")))
    if st.get("tripped_at"):
        lines.append("  tripped      %s: nothing pets it now; the reset comes within %s s of the last pet (%s)" % (
            ago(st["tripped_at"], now), st.get("timeout_s") or "?", ago(st.get("last_pet"), now)))
    if st.get("message"):
        lines.append("  note         " + st["message"])
    if facts and facts.get("nowayout") == "1":
        lines.append("  WARNING      the driver is set nowayout: a clean stop does not disarm the timer, so stopping "
                     "the service (or sleeping) resets the machine")
    return "\n".join(lines)


# ---- loading the driver, setup, and the commands -------------------------------------------------------------

def wait_for_device(find=find_device, sleep=time.sleep, tries=15):
    for _ in range(tries):
        d = find()
        if d:
            return d
        sleep(0.2)
    return None


def load_modules(run=subprocess.run, find=find_device, log=print, wait=wait_for_device):
    """Best effort, never fatal: when no hardware watchdog is showing, load the chipset's driver
    (sp5100_tco, else wdat_wdt; never softdog). Returns the module name that provided one, or None."""
    d = find()
    if d:
        return d["driver"] if d["driver"] in MODULES else None
    for m in MODULES:
        try:
            r = run(["modprobe", m], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                log("modprobe %s: %s" % (m, ((r.stderr or "").strip()[:200] or "failed")))
                continue
        except (OSError, subprocess.SubprocessError) as e:
            log("modprobe %s: %s" % (m, type(e).__name__))
            continue
        d = wait(find=find)
        if d:
            log("loaded %s: %s" % (m, d["dev"]))
            return m
        log("%s loaded but no hardware watchdog appeared" % m)
    log("no hardware watchdog could be started (the BIOS may have its watchdog or the TCO timer off; see the README)")
    return None


def run_systemctl(*args):
    try:
        r = subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def setup(choice, systemctl=run_systemctl, log=print, load=None, find=find_device):
    """setup.sh's step. on: the driver loaded, kept at boot only when a hardware watchdog showed up, the
    service enabled and restarted. off: stopped (a clean stop disarms the timer), disabled, the boot entry
    removed. Never fatal."""
    conf = o1common.p(MODULES_CONF)
    if choice == "on":
        mod = (load or load_modules)(find=find, log=log)
        dev = find()
        if dev and (mod or dev["driver"] in MODULES):
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            with open(conf, "w") as f:
                f.write("# ollama1-watchdog: the chipset's hardware watchdog\n%s\n" % (mod or dev["driver"]))
        elif os.path.exists(conf):
            os.unlink(conf)
        ok = systemctl("enable", UNIT) and systemctl("restart", UNIT)
        if not dev:
            log("the watchdog: no hardware watchdog on this machine yet; the service is installed and looks again every "
                "%d s (see the README: BIOS, sp5100_tco)" % RESCAN_S)
        else:
            log("the watchdog: %s" % ("on (%s)" % dev["dev"] if ok else "the service did not start (journalctl -u ollama1-watchdog)"))
        return 0
    systemctl("disable", "--now", UNIT)
    if os.path.exists(conf):
        os.unlink(conf)
    log("the watchdog: off (a clean stop disarms the timer)")
    return 0


def sleep_signal(action, systemctl_kill=None, status=read_status, sleep=time.sleep, now=time.monotonic,
                 log=print, active=None):
    """`pause` / `resume`, run by the sleep hook as root. pause: ask the service to close the device cleanly and
    wait (at most 5 s) until its status says it has let go. resume: ask it to reopen. Never fails the hook."""
    sig = "USR2" if action == "pause" else "USR1"
    kill = systemctl_kill or (lambda s: run_systemctl("kill", "--kill-whom=main", "--signal=" + s, UNIT))
    if (active or (lambda: run_systemctl("is-active", "--quiet", UNIT)))() is False:
        return 0                                           # not running: nothing to pause or resume
    if not kill(sig):
        log("watchdog: could not signal the service")
        return 1
    if action != "pause":
        return 0
    t0 = now()
    while now() - t0 < PAUSE_WAIT_S:
        st = status()
        if st and (st.get("state") == "paused" or not st.get("armed")):
            return 0
        sleep(0.1)
    log("watchdog: the service did not confirm the pause within %d s" % PAUSE_WAIT_S)
    return 1


def sd_notify(message, env=None, factory=socket.socket):
    path = (os.environ if env is None else env).get("NOTIFY_SOCKET")
    if not path:
        return False
    try:
        s = factory(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(message.encode("ascii", "replace"), "\0" + path[1:] if path.startswith("@") else path)
        finally:
            s.close()
        return True
    except OSError:
        return False


class AsyncLog:
    """The journal may be dead or stuck (that is the failure this service is for), and a print into a full pipe
    blocks. Lines go to a queue and a daemon thread prints them: the loop never waits for the journal."""

    def __init__(self, out=None):
        self.q = queue.Queue(maxsize=200)
        self.out = out or (lambda line: print(line, flush=True))
        threading.Thread(target=self._pump, name="log", daemon=True).start()

    def __call__(self, line):
        try:
            self.q.put_nowait(line)
        except queue.Full:
            pass

    def _pump(self):
        while True:
            line = self.q.get()
            try:
                self.out(line)
            except Exception:
                pass


def run_service(log=None):
    """ollama1-watchdog.service. Returns the exit code; an unexpected error exits 70 and leaves the timer armed."""
    log = log or AsyncLog()
    cfg = ProbeConfig()
    wd = Watchdog(lambda: real_probe(cfg), log=log, notify=sd_notify)
    signal.signal(signal.SIGTERM, lambda *_: wd.request_stop())
    signal.signal(signal.SIGINT, lambda *_: wd.request_stop())
    signal.signal(signal.SIGUSR2, lambda *_: wd.request_pause())     # the sleep hook: close cleanly, stay closed
    signal.signal(signal.SIGUSR1, lambda *_: wd.request_resume())    # after waking: reopen once a probe passes
    try:
        return wd.run()
    except BaseException as e:                                       # a bug: no 'V', the timer stays armed
        log("watchdog: crashed (%s); the timer is left armed" % type(e).__name__)
        time.sleep(0.2)
        return 70
