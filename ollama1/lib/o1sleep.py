"""Sleep (suspend) for ollama1: when it's allowed, the sleep/wake record,
and the health check after waking.

The admin panel's Sleep button and the root helper both ask busy_reasons()
first. Sleep is refused while any of these runs: a model pull or library
sync, an update (the panel's, the Ollama updater, apt's daily jobs), or
setup.sh. A RAID resync is allowed: the md driver pauses it and carries on
after waking.
"""
import fcntl
import fnmatch
import json
import os
import subprocess
import time
import urllib.request

from o1common import Paths, read_json, write_json_atomic

SETUP_LOCK = "/run/ollama1-setup.lock"
BUSY_UNITS = [
    ("ollama1-pull@*.service", "a model is being downloaded"),
    ("ollama1-models-sync.service", "the model library is being synced"),
    ("ollama1-update-now.service", "updates are being applied"),
    ("ollama1-update-ollama.service", "Ollama is being updated"),
    ("apt-daily.service", "the daily update check is running"),
    ("apt-daily-upgrade.service", "security updates are being installed"),
]


def _active_units(patterns):
    try:
        r = subprocess.run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active,activating,reloading"]
                           + patterns, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


class Inhibit:
    """Hold a logind inhibitor (sleep and the power button, mode block) while
    a long job runs: a model pull or library sync, updates. The holder reads
    a pipe only this process writes: when the job ends, however it ends
    (even SIGKILL), the holder gets EOF and exits, and the inhibitor with
    it. A no-op where systemd-inhibit isn't there, when not root, and in
    tests (unless `exe` is given)."""

    def __init__(self, why, exe=None):
        self.why = why
        self.exe = exe
        self.p = None

    def __enter__(self):
        import shutil
        exe = self.exe or shutil.which("systemd-inhibit")
        if exe and (self.exe or (os.geteuid() == 0 and not os.environ.get("OLLAMA1_PREFIX"))):
            try:
                self.p = subprocess.Popen([exe, "--what=sleep:handle-power-key", "--mode=block", "--who=ollama1",
                                           "--why=" + self.why, "sh", "-c", "cat >/dev/null"],
                                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
            except OSError:
                self.p = None
        return self

    def __exit__(self, *a):
        if self.p is not None:
            try:
                self.p.stdin.close()             # EOF: the holder exits
            except OSError:
                pass
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                import signal
                try:
                    os.killpg(self.p.pid, signal.SIGTERM)
                except OSError:
                    pass
        return False


def library_lock():
    return os.path.join(Paths.run, "library", "sync.lock")


def setup_running(lock=None):
    """True if setup.sh holds its lock (it keeps fd 9 on it); with another
    lock file, true if anything holds that one."""
    lock = lock or SETUP_LOCK
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def busy_reasons(active_units=_active_units, setup_lock=None):
    """Why the desktop must not sleep now ([] = it may)."""
    reasons = []
    active = active_units([p for p, _ in BUSY_UNITS])
    if active is None:
        return ["systemd could not be asked what is running"]
    for pattern, why in BUSY_UNITS:
        if any(fnmatch.fnmatchcase(u, pattern) for u in active):
            reasons.append(why)
    if setup_running(setup_lock):
        reasons.append("setup.sh is running")
    lib_why = dict(BUSY_UNITS)["ollama1-models-sync.service"]
    if lib_why not in reasons and setup_running(library_lock()):   # sudo ollama1-models sync
        reasons.append(lib_why)
    return reasons


def raid_resyncing(mdstat="/proc/mdstat"):
    try:
        text = open(mdstat).read()
    except OSError:
        return False
    return any(w in text for w in ("resync", "recovery", "reshape", "check ="))


# ---- the record --------------------------------------------------------------

def sleep_file():
    return os.path.join(Paths.state, "sleep.json")


def record(**kw):
    st = read_json(sleep_file(), {}) or {}
    st.update(kw)
    os.makedirs(Paths.state, exist_ok=True)
    write_json_atomic(sleep_file(), st, mode=0o644)
    return st


def last():
    """{"last_sleep", "last_wake", "resume_check": {"ok", "detail", "at"}} or {}."""
    st = read_json(sleep_file(), {})
    return st if isinstance(st, dict) else {}


# ---- after waking -------------------------------------------------------------

def ollama_ok(base="http://127.0.0.1:11434", timeout=60):
    """Ollama answers /api/version and /api/ps within `timeout` s."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            for path in ("/api/version", "/api/ps"):
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    json.loads(r.read() or b"{}")
            return True
        except (OSError, ValueError):
            time.sleep(2)
    return False


def gpu_ok(sys_root=None):
    """The amdgpu device is back: VRAM and busy figures readable."""
    import glob
    root = sys_root or os.environ.get("OLLAMA1_SYS", "/sys")
    for dev in glob.glob(root + "/class/drm/card*/device"):
        try:
            total = int(open(dev + "/mem_info_vram_total").read().strip())
            int(open(dev + "/gpu_busy_percent").read().strip())
            if total > 0:
                return True
        except (OSError, ValueError):
            continue
    return False


def resume_check(ollama=ollama_ok, gpu=gpu_ok, restart=None, log=print):
    """After waking: if Ollama or the GPU isn't healthy, restart Ollama and
    the tunnel. Returns the record written."""
    restart = restart or (lambda units: subprocess.run(["systemctl", "restart"] + units))
    o, g = ollama(), gpu()
    if o and g:
        detail = "Ollama and the GPU answered"
        ok = True
    else:
        why = ", ".join(x for x, bad in (("Ollama didn't answer", not o), ("the GPU didn't report", not g)) if bad)
        log("resume: %s; restarting Ollama and the tunnel" % why)
        restart(["ollama.service", "ollama1-tunnel.service"])
        ok = ollama()
        detail = "%s; restarted Ollama and the tunnel; Ollama %s" % (why, "answers now" if ok else "STILL NOT ANSWERING")
    log("resume: " + detail)
    return record(resume_check={"ok": ok, "detail": detail, "at": int(time.time())})
