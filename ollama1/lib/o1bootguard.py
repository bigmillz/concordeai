"""Panic-reboot and the boot-loop guard (6b524). stdlib only, like o1logguard: tools/install-watchdogs.sh puts it on a
running server without replacing any library the rest of the kit is using.

THE SECOND LAYER. The hardware watchdog (6b399, o1watchdog) resets the board when the system disk stops answering;
it is blind to a kernel that is wedged while a little of userspace still runs. The kernel can watch itself:

  kernel.panic = 10                 a panic reboots after 10 s instead of hanging for ever
  kernel.softlockup_panic = 1       a CPU stuck in the kernel for 20 s (2 x watchdog_thresh) panics
  kernel.hardlockup_panic = 1       a CPU stuck with interrupts off (the NMI watchdog) panics; only when the NMI
                                    watchdog is on (/proc/sys/kernel/nmi_watchdog = 1), else it is not set
  kernel.panic_on_oops = 1          an oops (a kernel bug, often in a driver) panics instead of limping on
  kernel.hung_task_panic = 1        a task stuck in uninterruptible sleep (D state) panics...
  kernel.hung_task_timeout_secs = 600   ...after 10 minutes, not the usual 2: a slow disk never trips it

The values are in /etc/ollama1/panic-sysctl.conf (sysctl syntax, the owner may edit it) and are NOT in
/etc/sysctl.d: systemd-sysctl applies that folder early in every boot, before anything can look at how the last boots
went, so a driver that oopses at every boot would panic the machine before the guard below could stop the loop (and
any later `sysctl --system` would set them again). Only this program applies them, after it has decided.

THE BOOT-LOOP GUARD. ollama1-bootguard.service (a oneshot early in boot, before sysinit.target) records each boot
(its boot id and when it started) in /var/lib/ollama1/bootguard.json. With 3 or more boots within 60 minutes it
does not apply the panic values: it writes the safe ones (those from before the install, saved in
/var/lib/ollama1/panic-before.json; the kernel's own defaults when that is missing), logs one clear line and marks
this boot as tripped, so the box stays up and reachable instead of rebooting in a loop. ollama1-netwatch does not
reboot a tripped boot either. The next boot that is not part of a loop arms again by itself.

THE TRIAL (tools/install-watchdogs.sh --arm-rollback). The installer writes /var/lib/ollama1/watchdogs-trial.json
and a transient timer that rolls everything back after 20 minutes unless the owner confirms. A transient timer
does not survive a reboot, so a boot that finds the trial file from an EARLIER boot (the box restarted during the
trial) applies the safe values and starts ollama1-watchdogs-rollback.service.
"""
import json
import os
import re
import subprocess
import tempfile
import time

PREFIX = os.environ.get("OLLAMA1_PREFIX", "").rstrip("/")


def p(path):
    return PREFIX + path


def proc():
    return os.environ.get("OLLAMA1_PROC", "/proc")


CONF = "/etc/ollama1/panic-sysctl.conf"
STATE = "/var/lib/ollama1/bootguard.json"
BEFORE = "/var/lib/ollama1/panic-before.json"
TRIAL = "/var/lib/ollama1/watchdogs-trial.json"
ROLLBACK_UNIT = "ollama1-watchdogs-rollback.service"

LOOP_BOOTS = 3                 # this many boots...
LOOP_WINDOW_S = 3600           # ...within this long, and the panic settings stay off for this boot
KEEP_S = 7 * 86400             # boots older than this are forgotten
KEEP_MAX = 50

# The keys this program may write, nothing else. (lowest, highest) for a value taken from the config file.
KEYS = ("kernel.panic", "kernel.softlockup_panic", "kernel.hardlockup_panic", "kernel.panic_on_oops",
        "kernel.hung_task_panic", "kernel.hung_task_timeout_secs")
RANGES = {
    "kernel.panic": (5, 600),                       # 0 would hang after a panic; a negative value reboots at once
    "kernel.softlockup_panic": (0, 1),
    "kernel.hardlockup_panic": (0, 1),
    "kernel.panic_on_oops": (0, 1),
    "kernel.hung_task_panic": (0, 1),
    "kernel.hung_task_timeout_secs": (300, 86400),  # never under 5 min: a slow disk must not panic the box
}
# The kernel's (and Ubuntu's) own defaults: the safe values when nothing was saved before the install.
KERNEL_DEFAULTS = {"kernel.panic": 0, "kernel.softlockup_panic": 0, "kernel.hardlockup_panic": 0,
                   "kernel.panic_on_oops": 0, "kernel.hung_task_panic": 0, "kernel.hung_task_timeout_secs": 120}
INT_RX = re.compile(r"^-?\d{1,9}$")
BOOT_ID_RX = re.compile(r"^[0-9a-f-]{32,36}$")


# ---- small file helpers ------------------------------------------------------------------------------

def _read(path, limit=65536):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def read_json(path, default=None):
    t = _read(path, 1 << 20)
    if t is None:
        return default
    try:
        d = json.loads(t)
    except ValueError:
        return default
    return d if isinstance(d, dict) else default


def write_json(path, obj, mode=0o644):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- the kernel's side -----------------------------------------------------------------------------

def key_path(key):
    return os.path.join(proc(), "sys", *key.split("."))


def read_key(key):
    t = _read(key_path(key))
    t = t.strip() if t is not None else None
    return int(t) if t is not None and INT_RX.match(t) else None


def nmi_watchdog_on():
    """kernel.hardlockup_panic only means something while the NMI watchdog runs."""
    return read_key("kernel.nmi_watchdog") == 1


def available(key):
    if not os.path.exists(key_path(key)):
        return False
    if key == "kernel.hardlockup_panic":
        return nmi_watchdog_on()
    return True


def current():
    return {k: read_key(k) for k in KEYS}


def write_key(key, value):
    """Write one value and read it back. Returns None when it took, else why not."""
    if key not in KEYS:
        return "not a key this program writes"
    path = key_path(key)
    if not os.path.exists(path):
        return "not in this kernel"
    try:
        with open(path, "w") as f:
            f.write("%d\n" % int(value))
    except OSError as e:
        return e.strerror or "write failed"
    got = read_key(key)
    return None if got == int(value) else "reads back as %s" % got


# ---- the config file and the saved values ----------------------------------------------------------------

def parse_conf(text):
    """sysctl syntax: `key = value`, comments with # or ;. Returns ({key: int}, [notes]). Unknown keys, values
    that are not whole numbers and values out of range are left out, each with a note."""
    out, notes = {}, []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        key, sep, val = line.partition("=")
        key, val = key.strip().replace("/", "."), val.strip()
        if not sep or key not in KEYS:
            notes.append("line %d: %s is not one of the panic settings; ignored" % (n, key[:40] or "?"))
            continue
        if not INT_RX.match(val):
            notes.append("line %d: %s needs a whole number; ignored" % (n, key))
            continue
        lo, hi = RANGES[key]
        v = int(val)
        if not lo <= v <= hi:
            notes.append("line %d: %s = %d is outside %d..%d; ignored" % (n, key, v, lo, hi))
            continue
        out[key] = v
    return out, notes


def load_conf(path=None):
    t = _read(path or p(CONF))
    if t is None:
        return None, ["%s is missing: no panic setting is applied" % (path or CONF)]
    return parse_conf(t)


def safe_values():
    """What the machine had before the install (or the kernel's defaults for anything not saved)."""
    saved = read_json(p(BEFORE), {}) or {}
    vals = saved.get("values") if isinstance(saved.get("values"), dict) else {}
    out = {}
    for k in KEYS:
        v = vals.get(k)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) and -1 <= v <= 86400 else KERNEL_DEFAULTS[k]
    return out


def snapshot(now=None):
    """Save the values the machine has now, once: a second install must not save the panic values as 'before'."""
    path = p(BEFORE)
    if os.path.exists(path):
        return False, read_json(path, {}) or {}
    data = {"saved_at": int(now if now is not None else time.time()), "values": current()}
    write_json(path, data, mode=0o644)
    return True, data


def targets(conf, tripped):
    """{key: value} to write for this boot. Tripped (or no config): the safe values. Otherwise the config's value
    for each key it sets (and is available here), the safe value for the rest."""
    safe = safe_values()
    out = {}
    for k in KEYS:
        if not os.path.exists(key_path(k)):
            continue
        if tripped or conf is None or k not in conf or not available(k):
            out[k] = safe[k]
        else:
            out[k] = conf[k]
    return out


def apply(values, log=print):
    errors = []
    for k, v in values.items():
        why = write_key(k, v)
        if why:
            errors.append("%s: %s" % (k, why))
            log("bootguard: could not set %s = %s (%s)" % (k, v, why))
    return errors


def restore(log=print):
    """--uninstall: the saved values back (the kernel's defaults where none were saved), the saved file removed."""
    vals = safe_values()
    errors = apply({k: v for k, v in vals.items() if os.path.exists(key_path(k))}, log=log)
    if not errors:
        try:
            os.unlink(p(BEFORE))
        except FileNotFoundError:
            pass
    return vals, errors


# ---- the boot record --------------------------------------------------------------------------------------

def boot_id():
    t = (_read(os.path.join(proc(), "sys", "kernel", "random", "boot_id")) or "").strip()
    return t if BOOT_ID_RX.match(t) else None


def uptime():
    t = (_read(os.path.join(proc(), "uptime")) or "").split()
    try:
        return float(t[0])
    except (IndexError, ValueError):
        return None


def record_boot(state, bid, boot_at, now):
    """Add this boot (once per boot id) and forget old ones. Returns the new list."""
    boots = []
    for b in state.get("boots", []) if isinstance(state.get("boots"), list) else []:
        if isinstance(b, dict) and isinstance(b.get("at"), (int, float)) and isinstance(b.get("id"), str):
            if abs(now - b["at"]) <= KEEP_S:
                boots.append({"id": b["id"], "at": int(b["at"])})
    if not any(b["id"] == bid for b in boots):
        boots.append({"id": bid, "at": int(boot_at)})
    return boots[-KEEP_MAX:]


def boots_in_window(boots, now, window=LOOP_WINDOW_S):
    """Boots that started within `window` of now. A boot dated ahead of now counts too (a clock that went back,
    a dead clock battery): when the clock is in doubt the guard errs towards keeping the box up."""
    return [b for b in boots if abs(now - b["at"]) <= window]


def decide(state, bid, boot_at, now):
    """(new state, tripped, [boots in the window]). A boot that tripped stays tripped when the service runs again."""
    boots = record_boot(state, bid, boot_at, now)
    recent = boots_in_window(boots, now)
    tripped = len(recent) >= LOOP_BOOTS or state.get("tripped_boot") == bid
    new = dict(state)
    new["boots"] = boots
    if tripped:
        new["tripped_boot"] = bid
    return new, tripped, recent


def is_tripped(state=None, bid=None):
    """For ollama1-netwatch: did the guard trip in this boot?"""
    state = state if state is not None else read_json(p(STATE), {}) or {}
    bid = bid or boot_id()
    return bool(bid) and state.get("tripped_boot") == bid


def trial():
    return read_json(p(TRIAL), None)


def fmt_time(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def run(log=print, wall=time.time, systemctl=None):
    """ollama1-bootguard.service. Never fails the boot: every problem is a line in the journal and in the state."""
    now = wall()
    bid = boot_id() or "unknown-%d" % int(now)
    up = uptime()
    boot_at = now - up if up is not None else now
    state = read_json(p(STATE), {}) or {}
    new, tripped, recent = decide(state, bid, boot_at, now)
    conf, notes = load_conf()
    for n in notes:
        log("bootguard: " + n)
    tr = trial()
    rollback = bool(tr and tr.get("boot_id") and tr.get("boot_id") != bid)
    if tripped:
        why = ("%d boots within %d minutes (%s): the panic settings are OFF for this boot so the server stays up; "
               "netwatch will not reboot it either. The next boot that is not part of a loop arms them again."
               % (len(recent), LOOP_WINDOW_S // 60, ", ".join(fmt_time(b["at"]) for b in recent)))
        mode = "safe"
    elif rollback:
        why = ("the server restarted during the install's trial (armed %s, never confirmed): the safe values, and the "
               "rollback starts now" % fmt_time(tr.get("armed_at") or now))
        mode = "safe"
    elif conf is None:
        why = "no %s: the safe values" % CONF
        mode = "safe"
    else:
        why = "%d boot(s) in the last %d minutes: the panic settings are on" % (len(recent), LOOP_WINDOW_S // 60)
        mode = "panic"
    vals = targets(conf, mode == "safe")
    errors = apply(vals, log=log)
    skipped = sorted(k for k in (conf or {}) if not available(k))
    new.update({"version": 1, "boot_id": bid, "at": int(now), "mode": mode, "why": why, "values": vals,
                "errors": errors, "not_available": skipped, "boots_in_window": len(recent)})
    write_json(p(STATE), new)
    log(("bootguard: TRIPPED: " if tripped else "bootguard: ") + why)
    if "kernel.hardlockup_panic" in (conf or {}) and not available("kernel.hardlockup_panic"):
        log("bootguard: kernel.hardlockup_panic not set: the NMI watchdog is off in this kernel")
    if rollback:
        (systemctl or _systemctl)("start", "--no-block", ROLLBACK_UNIT)
    return 0


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def plan(conf_path=None):
    """For --dry-run: (key, now, would be) for every key, as the next run would set it (no boot recorded), from
    the installed config or the one given (the kit's, before it is installed)."""
    conf, notes = load_conf(conf_path)
    state = read_json(p(STATE), {}) or {}
    tripped = is_tripped(state)
    vals = targets(conf, tripped)
    return [(k, read_key(k), vals.get(k)) for k in KEYS], notes, tripped


def render_status(now=None):
    now = time.time() if now is None else now
    st = read_json(p(STATE), {}) or {}
    bid = boot_id()
    lines = []
    if not st:
        lines.append("Boot guard: has not run yet (sudo systemctl status ollama1-bootguard)")
    elif st.get("boot_id") != bid:
        lines.append("Boot guard: has NOT run in this boot (last ran %s)" % fmt_time(st.get("at") or 0))
    elif st.get("tripped_boot") == bid:
        lines.append("Boot guard: TRIPPED this boot: the panic settings are off")
    else:
        lines.append("Boot guard: %s" % ("panic settings on" if st.get("mode") == "panic" else "safe values (%s)" % st.get("why", "")))
    if st.get("why"):
        lines.append("  why          " + st["why"])
    boots = st.get("boots") or []
    if boots:
        lines.append("  last boots   " + ", ".join(fmt_time(b["at"]) for b in boots[-5:]))
    cur = current()
    conf, notes = load_conf()
    lines.append("  kernel now   " + ", ".join("%s=%s" % (k.split(".", 1)[1], "-" if cur[k] is None else cur[k]) for k in KEYS))
    if conf is not None:
        lines.append("  config       " + ", ".join("%s=%s" % (k.split(".", 1)[1], v) for k, v in sorted(conf.items())))
    for n in notes:
        lines.append("  note         " + n)
    if not nmi_watchdog_on():
        lines.append("  note         the NMI watchdog is off: kernel.hardlockup_panic is not set")
    if st.get("errors"):
        lines.append("  errors       " + "; ".join(st["errors"]))
    tr = trial()
    if tr:
        lines.append("  trial        armed %s, rollback at %s unless confirmed" % (
            fmt_time(tr.get("armed_at") or 0), fmt_time(tr.get("deadline") or 0)))
    return "\n".join(lines)
