"""Sleep (suspend) for ollama1: when it's allowed, the sleep/wake record,
and the health check after waking.

The admin panel's Sleep button and the root helper both ask busy_reasons()
first. Sleep is refused while any of these runs: a model pull or library
sync, a model set from the app, an update (the panel's, the Ollama updater,
apt's daily jobs), or setup.sh. A RAID resync is allowed: the md driver
pauses it and carries on after waking.
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
    ("ollama1-modelplan.service", "a model set from the app is being applied"),     # 6b410
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
    """Why the server must not sleep now ([] = it may)."""
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
        if dict(BUSY_UNITS)["ollama1-modelplan.service"] not in reasons:   # a set from the app holds it too: say it once
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
    """{"last_sleep", "last_wake", "pre_sleep": {...}, "resume_check": {"ok", "detail", "at"}} or {}."""
    st = read_json(sleep_file(), {})
    return st if isinstance(st, dict) else {}


# ---- before sleeping (6b446) ----------------------------------------------------
# A model left loaded on the card across a suspend came back answering junk
# ("<unused49>" over and over) after amdgpu logged "Failed to set manual fan
# control mode" and "Fence fallback timer expired on ring kiq" on the wake;
# unloading it and loading it again fixed it at once. So nothing stays loaded
# across a sleep: the sleep hook unloads every model before every suspend (the
# kit's own, the power button's, anyone's), and if Ollama won't let go, stops it.

OLLAMA_URL = "http://127.0.0.1:11434"
UNLOAD_TIMEOUT_S = 8      # one unload call (Ollama answers a keep_alive 0 at once)
UNLOAD_WAIT_S = 15        # the unloads and /api/ps showing nothing loaded, together
STOP_TIMEOUT_S = 30       # systemctl stop ollama.service (the stop goes on in systemd if this gives up). Worst
                          # case in all, about 5 + 15 + 2 x 8 + 5 + 30 = 71 s: under the 90 s systemd-sleep gives
                          # its hooks, with the watchdog's 5 s before it (the hook does this last)


def ollama_base():
    """The local Ollama. In a test (OLLAMA1_PREFIX set) only OLLAMA1_OLLAMA_URL, else a port nothing answers on:
    a test run must never unload the models of an Ollama running on the machine it runs on."""
    if os.environ.get("OLLAMA1_PREFIX"):
        return os.environ.get("OLLAMA1_OLLAMA_URL") or "http://127.0.0.1:1"
    return OLLAMA_URL


def _call(method, path, obj=None, timeout=10):
    import http.client
    import o1ollama
    try:
        return o1ollama.call(ollama_base(), method, path, obj, timeout=timeout)
    except (OSError, http.client.HTTPException, ValueError):
        return None, None


def loaded_models(call=_call):
    """The names of the models Ollama has loaded ([] = none), or None when it doesn't answer."""
    st, data = call("GET", "/api/ps", timeout=5)
    if st != 200 or not isinstance(data, dict):
        return None
    ms = data.get("models")
    out = []
    for m in ms if isinstance(ms, list) else []:
        n = (m.get("name") or m.get("model")) if isinstance(m, dict) else None
        if isinstance(n, str) and n and n not in out:
            out.append(n)
    return out


def unload(name, call=_call):
    """keep_alive 0 for one model: generate, else embed (an embedding model has no generate)."""
    for path, body in (("/api/generate", {"model": name, "keep_alive": 0}),
                       ("/api/embed", {"model": name, "input": [], "keep_alive": 0})):
        st, _ = call("POST", path, body, timeout=UNLOAD_TIMEOUT_S)
        if st == 200:
            return True
    return False


def unload_all(call=_call, sleep=time.sleep, clock=time.monotonic, wait_s=UNLOAD_WAIT_S):
    """Unload every loaded model and wait until /api/ps shows none, all within wait_s (and one last look).
    Returns (ok, how many were loaded, what happened)."""
    names = loaded_models(call)
    if names is None:
        return False, 0, "Ollama didn't say what is loaded"
    if not names:
        return True, 0, "nothing was loaded"
    end = clock() + wait_s
    for n in names:
        if clock() >= end:
            break
        unload(n, call)
    while True:
        left = loaded_models(call)
        if left == []:
            return True, len(names), "unloaded %d model%s" % (len(names), "" if len(names) == 1 else "s")
        if clock() >= end:
            break
        sleep(1)
    if left is None:
        return False, len(names), "Ollama stopped answering while %d model%s unloaded" % (
            len(names), " was" if len(names) == 1 else "s were")
    return False, len(names), "%d model%s still loaded after %d s" % (len(left), "" if len(left) == 1 else "s", wait_s)


def _ollama_active():
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "ollama.service"], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True                                  # can't tell: try to unload


def _stop_ollama():
    try:
        return subprocess.run(["systemctl", "stop", "ollama.service"], timeout=STOP_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def before_sleep(unload_fn=None, active=_ollama_active, stop=_stop_ollama, log=print):
    """From the sleep hook, before every suspend: nothing may stay loaded on the card. Unload every model; if
    that can't be confirmed, stop Ollama (the check after the wake starts it again). The record says which."""
    unload_fn = unload_fn or unload_all
    stopped = False
    if not active():
        ok, n, detail = True, 0, "Ollama wasn't running"
    else:
        ok, n, detail = unload_fn()
        if not ok:
            log("before sleep: %s; stopping Ollama" % detail)
            stopped = bool(stop())
            detail += "; stopped Ollama" if stopped else "; stopping Ollama failed too"
    log("before sleep: " + detail)
    return record(pre_sleep={"ok": ok, "unloaded": n, "stopped_ollama": stopped, "detail": detail,
                             "at": int(time.time())})


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


def gpu_expected(sys_root=None):
    """True when an AMD graphics card is on the PCI bus: then the driver owes us
    a card after waking. With none (a machine without one, or an NVIDIA card,
    which ollama1-wait-gpu handles) there is nothing to wait for."""
    import glob
    root = sys_root or os.environ.get("OLLAMA1_SYS", "/sys")
    for d in glob.glob(root + "/bus/pci/devices/*"):
        try:
            if (open(d + "/class").read().strip().startswith("0x03")
                    and open(d + "/vendor").read().strip() == "0x1002"):
                return True
        except OSError:
            continue
    return False


def models_ok(path=None):
    """The models drive is back: /srv/models can be listed, and is either a
    mount or holds something (an unmounted mount point is an empty folder)."""
    path = path or Paths.models
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return os.path.ismount(path) or bool(names)


WAIT_S = 90           # the most it waits for the card and the models drive after a wake
RETRY_S = 3
CHECK_BUDGET_S = 480  # the whole check, so a stuck one still ends (the unit allows 10 minutes)


def wait_ready(gpu, models, gpu_needed, wait_s=WAIT_S, sleep=time.sleep, clock=time.monotonic):
    """Wait, at most wait_s, for the card's driver and the models drive to be
    back after a wake. Returns {"gpu": bool, "models": bool, "waited": seconds}."""
    start = clock()
    state = {"gpu": True, "models": True}
    while True:
        state["gpu"] = bool(gpu()) or not gpu_needed()
        state["models"] = bool(models())
        if (state["gpu"] and state["models"]) or clock() - start >= wait_s:
            break
        sleep(RETRY_S)
    state["waited"] = int(clock() - start)
    return state


def _missing(st):
    return ", ".join(x for x, ok in (("the graphics card", st["gpu"]), ("the models drive", st["models"])) if not ok)


def resume_check(ollama=ollama_ok, gpu=gpu_ok, restart=None, log=print, models=models_ok, gpu_needed=gpu_expected,
                 sleep=time.sleep, clock=time.monotonic, wait_s=WAIT_S, pre=None, clear=None):
    """After waking: if Ollama or the GPU isn't healthy, wait (at most wait_s)
    for the card's driver and the models drive to be back, then restart Ollama
    and the tunnel, and once more if Ollama still doesn't answer. The record
    says what it saw: how long it waited, what was missing, how many restarts.
    (6b371: after a wake it once recorded "Ollama STILL NOT ANSWERING" having
    restarted Ollama straight away, while the card's driver was not back.)

    6b446: if the sleep hook had to stop Ollama (`pre`, the pre_sleep record, says so) it is started first. When
    the record doesn't show this sleep's hook leaving the card empty (it unloaded everything or stopped Ollama),
    whatever /api/ps shows once Ollama answers may be from before the sleep: it is unloaded (`clear`, default
    unload_all), and a model that won't go gets Ollama restarted. When it does, anything loaded now was loaded
    after the wake (a request from the relay's wake, say) and is left alone."""
    restart = restart or (lambda units: subprocess.run(["systemctl", "restart"] + units, timeout=300))
    clear = clear or unload_all
    pre = last().get("pre_sleep") if pre is None else pre
    pre = pre if isinstance(pre, dict) else {}
    slept_at = last().get("last_sleep")
    at = pre.get("at")
    emptied = (bool(pre.get("ok") or pre.get("stopped_ollama")) and isinstance(at, (int, float))
               and at >= (slept_at if isinstance(slept_at, (int, float)) else 0))
    t0 = clock()
    facts = {"restarts": 0}
    started = []
    if pre.get("stopped_ollama") and not pre.get("resumed"):
        log("resume: Ollama was stopped before the sleep; starting it")
        restart(["ollama.service"])
        started.append("Ollama was stopped before the sleep: started it")
        facts["started_ollama"] = True
    o, g = ollama(), (gpu() or not gpu_needed())
    if o and g:
        detail, ok = "Ollama and the GPU answered", True
    else:
        why = ", ".join(x for x, bad in (("Ollama didn't answer", not o), ("the GPU didn't report", not g)) if bad)
        parts = [why]
        log("resume: %s; waiting for the card and the models drive (at most %d s)" % (why, wait_s))
        st = wait_ready(gpu, models, gpu_needed, wait_s, sleep, clock)
        if st["gpu"] and st["models"]:
            parts.append("the card and the models drive were back after %d s" % st["waited"])
        else:
            parts.append("after %d s still missing: %s" % (st["waited"], _missing(st)))
        facts.update(waited_s=st["waited"], gpu_ready=st["gpu"], models_ready=st["models"])
        ok = False
        for attempt in (1, 2):
            if clock() - t0 > CHECK_BUDGET_S:
                parts.append("out of time before restart %d" % attempt)
                break
            if attempt == 2 and not (st["gpu"] and st["models"]):
                # the first restart didn't bring it back: one more bounded wait for what was missing
                st = wait_ready(gpu, models, gpu_needed, wait_s, sleep, clock)
                facts.update(gpu_ready=st["gpu"], models_ready=st["models"])
                parts.append("waited %d s more: %s" % (
                    st["waited"], "both back" if st["gpu"] and st["models"] else "still missing " + _missing(st)))
            if attempt == 1 and st["gpu"] and st["models"] and ollama():
                ok = True                       # it was only slow: nothing to restart
                parts.append("Ollama answers now without a restart")
                break
            log("resume: restarting Ollama and the tunnel (try %d)" % attempt)
            restart(["ollama.service", "ollama1-tunnel.service"])
            facts["restarts"] = attempt
            ok = ollama()
            parts.append("restarted Ollama and the tunnel%s: Ollama %s" % (
                "" if attempt == 1 else " again", "answers now" if ok else "STILL NOT ANSWERING"))
            if ok:
                break
        detail = "; ".join(parts)
    if ok and not emptied:
        detail = "; ".join([detail] + after_wake_clear(clear, restart, ollama, facts, log))
        ok = facts.get("cleared", True)
    detail = "; ".join(started + [detail])
    log("resume: " + detail)
    extra = {"pre_sleep": dict(pre, resumed=True)} if pre else {}
    return record(resume_check=dict(facts, ok=ok, detail=detail, at=int(time.time())), **extra)


def after_wake_clear(clear, restart, ollama, facts, log):
    """Nothing loaded from before the sleep: unload whatever /api/ps still shows; if that can't be confirmed,
    restart Ollama once. Returns the phrases for the record; sets facts["cleared"] (False: still not sure)."""
    try:
        ok, n, why = clear()
    except Exception as e:                           # never let this end the check without a record
        ok, n, why = False, 0, "the after-wake unload failed (%s)" % type(e).__name__
    if ok and not n:
        return []
    if ok:
        log("resume: %s that was still loaded after the wake" % why)
        facts["unloaded_after_wake"] = n
        return ["%s that %s still loaded after the wake" % (why, "was" if n == 1 else "were")]
    log("resume: %s; restarting Ollama" % why)
    restart(["ollama.service"])
    facts["restarts"] = facts.get("restarts", 0) + 1
    up = bool(ollama())
    facts["cleared"] = up
    return ["%s: restarted Ollama, %s" % (why, "it answers now" if up else "it is STILL NOT ANSWERING")]
