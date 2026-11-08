"""ollama1-watchdog-doctor (6b524): read-only. After a hang, on the next boot, it says whether the hardware watchdog
(6b399, o1watchdog) could have reset the box, and what the previous boot's journal says happened. stdlib only.

It changes nothing: it reads sysfs, /proc, the modprobe files, the watchdog's status file and statvfs, and runs only
journalctl and `systemctl is-enabled / is-active / show`. It never opens /dev/watchdog* (an open arms the timer).

The ways the 6b399 watchdog can fail to reset a hung box, read from its code (lib/o1watchdog.py), and how a check
after the next boot tells each one (REASONS below, in this order):

  driver      sp5100_tco did not load, or loaded and found the board's timer off ("Watchdog hardware is disabled",
              no MMIO address): no /dev/watchdog0, the service sits in "no-device" and never arms.
              -> sysfs /sys/class/watchdog, the TCO lines in this boot's and the last boot's kernel log.
  blacklist   Ubuntu's kernel packages blacklist every watchdog module, and systemd-modules-load honours that, so
              /etc/modules-load.d/ollama1-watchdog.conf may load nothing; only the service's own modprobe
              (ExecStartPre) does. A service that never started never loaded it.
              -> "blacklist sp5100_tco" in /etc/modprobe.d, /usr/lib/modprobe.d, /lib/modprobe.d.
  service     the service stopped, disabled, or failing before it opens the device.
              -> is-enabled, is-active, NRestarts; "armed" in the last boot's ollama1-watchdog lines.
  holder      another program holds /dev/watchdog0 (systemd's RuntimeWatchdogSec, a watchdog daemon): the open
              fails with EBUSY and the service never arms.
              -> who has /dev/watchdog* open in /proc/*/fd; systemd's RuntimeWatchdogUSec.
  paused      closed cleanly for a suspend ('V') and never reopened. The pause has NO time limit: only the sleep
              hook's `post` reopens it, so a resume that hangs, or a wake whose hooks never run, leaves the timer
              off for good. The 2026-10-24 hang began with an s2idle suspend.
              -> the last boot's ollama1-watchdog lines: "closed cleanly for sleep" with no "resumed" after it;
                 "PM: suspend entry" with no "PM: suspend exit" after it in the kernel log.
  probe       the probe kept passing on a box that was useless: it only proves the disks answer. It runs as root,
              so it can still write into the root filesystem's reserved blocks (5%) when everyone else sees a full
              disk; a GPU driver flooding the log, a dead tunnel or SSH, or a kernel wedged on some CPUs but not
              all are invisible to it, and it pets on.
              -> no "probe failed" in the last boot; the NVRM/soft-lockup/no-space counts; the root reserve.
  clean_stop  nowayout is off (needed for sleep), so 'V' disarms: a `systemctl stop` of the healthy service, which
              every shutdown and reboot does, disarms it, and a shutdown that then hangs (a driver that won't
              unload) is never reset.
              -> "stopped; disarmed" in the last boot's lines, a shutdown that started and never finished.
  no_reset    the timer ran out and the board did not reset (firmware, BIOS option). Unproven until the drill in
              the README is done once; after a watchdog reset the driver reports WDIOF_CARDRESET in bootstatus.
              -> sysfs bootstatus, "Last reboot was caused by watchdog" style lines.
  not_a_hang  there was no hang: a power cut, or the box stayed up with no network (Wi-Fi lost). The journal then
              goes on until the button press, often with Wi-Fi disconnect lines.
              -> how the last boot ended, when its last line was, the Wi-Fi lines.
"""
import json
import os
import re
import subprocess
import time

PREFIX = os.environ.get("OLLAMA1_PREFIX", "").rstrip("/")


def p(path):
    return PREFIX + path


def proc():
    return os.environ.get("OLLAMA1_PROC", "/proc")


def sysfs():
    return os.environ.get("OLLAMA1_SYS", "/sys")


WATCHDOG_STATUS = "/run/ollama1/watchdog.json"
UNIT = "ollama1-watchdog.service"
MODULE = "sp5100_tco"
MODPROBE_DIRS = ("/etc/modprobe.d", "/usr/lib/modprobe.d", "/lib/modprobe.d", "/run/modprobe.d")
WDIOF_CARDRESET = 0x0020
TAIL = 40
MAX_LINES = 400

REASONS = (
    ("driver", "the driver did not load, or the board's timer is off"),
    ("blacklist", "the module is blacklisted, so only the service's own modprobe loads it"),
    ("service", "the service was stopped, disabled or failing"),
    ("holder", "another program holds /dev/watchdog0"),
    ("paused", "it was closed for a suspend and never reopened (the pause has no time limit)"),
    ("probe", "the probe kept passing on a useless box (it only tests the disks; root writes into the reserve)"),
    ("clean_stop", "a stop during a shutdown disarmed it, then the shutdown hung"),
    ("no_reset", "the timer ran out but the board did not reset (unproven until the drill)"),
    ("not_a_hang", "there was no hang: a power cut, or up with no network"),
)

# kernel log patterns, counted in the last boot
SIGNS = (
    ("nvrm_assert", re.compile(r"NVRM: nvAssert|nvAssertFailed")),
    ("gsp_reset", re.compile(r"GPU_IN_FULLCHIP_RESET|GSP.*(?:fail|error|timeout)|RmInitAdapter failed", re.I)),
    ("xid", re.compile(r"NVRM: Xid")),
    ("fallen_off_bus", re.compile(r"fallen off the bus", re.I)),
    ("soft_lockup", re.compile(r"soft lockup", re.I)),
    ("hard_lockup", re.compile(r"hard LOCKUP|Watchdog detected hard LOCKUP", re.I)),
    ("rcu_stall", re.compile(r"rcu.*(?:stall|self-detected)", re.I)),
    ("hung_task", re.compile(r"blocked for more than \d+ seconds")),
    ("oops", re.compile(r"\bOops\b|BUG: unable to handle|general protection fault")),
    ("panic", re.compile(r"Kernel panic - not syncing")),
    ("no_space", re.compile(r"No space left on device", re.I)),
    ("nvme_error", re.compile(r"nvme\d*.*(?:timeout|I/O error|controller is down|resetting controller)", re.I)),
    ("wifi_drop", re.compile(r"CTRL-EVENT-DISCONNECTED|deauthenticat|disassociat|Connection to AP .* lost|"
                             r"beacon loss|link becomes not ready|carrier lost", re.I)),
    ("suspend_entry", re.compile(r"PM: suspend entry")),
    ("suspend_exit", re.compile(r"PM: suspend exit")),
    ("wd_not_stop", re.compile(r"watchdog did not stop")),
)
KERNEL_GREP = ("NVRM|GPU_IN_FULLCHIP_RESET|fallen off the bus|soft lockup|hard LOCKUP|rcu.*stall|blocked for more than|"
               "Oops|BUG: unable|general protection|Kernel panic|No space left|nvme|PM: suspend|watchdog|"
               "sp5100|TCO|CTRL-EVENT-DISCONNECTED|deauthenticat|disassociat|beacon loss|not ready")


def _read(path, limit=65536):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return None


def run_cmd(argv, timeout=60):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout or ""
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""


# ---- parsing (pure) --------------------------------------------------------------------------------------------

def parse_tco(text):
    """The sp5100_tco / watchdog lines of a kernel log."""
    out = {"seen": False, "disabled": False, "no_mmio": False, "busy_io": False, "initialized": None,
           "fired": False, "did_not_stop": 0, "lines": []}
    for line in (text or "").splitlines():
        low = line.lower()
        if "sp5100" not in low and "tco" not in low and "watchdog" not in low:
            continue
        if "sp5100" in low or "tco" in low:
            out["seen"] = True
        if len(out["lines"]) < 12:
            out["lines"].append(line.strip()[:200])
        if "watchdog hardware is disabled" in low:
            out["disabled"] = True
        if "failed to find mmio" in low or "failed to reserve mmio" in low or "mmio address, giving up" in low:
            out["no_mmio"] = True
        if "already in use" in low and ("sp5100" in low or "tco" in low):
            out["busy_io"] = True
        m = re.search(r"initialized\.?\s+heartbeat=(\d+)\s*sec\s*\(nowayout=(\d)\)", line)
        if m:
            out["initialized"] = {"heartbeat": int(m.group(1)), "nowayout": int(m.group(2))}
        if re.search(r"caused by (?:the )?watchdog|watchdog (?:has )?fired|last reboot was .*watchdog", low):
            out["fired"] = True
        if "watchdog did not stop" in low:
            out["did_not_stop"] += 1
    return out


def count_signs(text):
    counts = {k: 0 for k, _ in SIGNS}
    last = {}
    for line in (text or "").splitlines():
        for k, rx in SIGNS:
            if rx.search(line):
                counts[k] += 1
                last[k] = line.strip()[:200]
    return counts, last


def parse_watchdog_lines(text):
    """What the 6b399 service said in a boot (journalctl -u ollama1-watchdog)."""
    out = {"armed": False, "paused_last": False, "probe_failed": 0, "tripped": False, "stopped_disarmed": False,
           "no_device": False, "held_by_other": False, "crashed": False, "last": None}
    state = None
    for line in (text or "").splitlines():
        if "watchdog:" not in line and "ollama1-watchdog" not in line:
            continue
        out["last"] = line.strip()[:200]
        if "watchdog: armed" in line:
            out["armed"] = True
            state = "armed"
        if "closed cleanly for sleep" in line:
            state = "paused"
        if "resumed after sleep" in line:
            state = "resumed"
        if "probe failed" in line:
            out["probe_failed"] += 1
        if "stopped petting" in line:
            out["tripped"] = True
        if "stopped; disarmed" in line:
            out["stopped_disarmed"] = True
        if "no hardware watchdog" in line:
            out["no_device"] = True
        if "held by another program" in line:
            out["held_by_other"] = True
        if "crashed" in line:
            out["crashed"] = True
    out["paused_last"] = state == "paused"
    return out


SHUTDOWN_RX = re.compile(r"systemd-shutdown|Journal stopped|Reached target (?:System Reboot|System Power Off|"
                         r"System Halt|reboot\.target|poweroff\.target)|System is (?:rebooting|powering down)|"
                         r"reboot: (?:Restarting|Power down)")
SHUTDOWN_BEGIN_RX = re.compile(r"Stopping session-|Stopped target (?:Graphical|Multi-User)|Reached target Shutdown|"
                               r"Stopped target .*Network|System is going down|The system will reboot|"
                               r"Stopping .*ollama1-watchdog")


def how_it_ended(tail, signs, wd):
    """(kind, sentence) for the previous boot, from its last lines, the kernel counts and the watchdog's lines."""
    tail = tail or ""
    if SHUTDOWN_RX.search(tail):
        return "clean", "it was shut down or rebooted on purpose (the shutdown is in the journal)"
    if signs.get("panic"):
        return "panic", "the kernel panicked"
    if signs.get("suspend_entry", 0) > signs.get("suspend_exit", 0):
        return "suspend", ("it went into a suspend and the journal never saw it come out (PM: suspend entry without "
                           "an exit): a hang in sleep or in the resume, with the watchdog closed for the sleep")
    if SHUTDOWN_BEGIN_RX.search(tail):
        return "shutdown_hang", ("a shutdown began and never finished: a hang on the way down (the watchdog was "
                                 "stopped cleanly, so nothing could reset it)")
    if wd.get("tripped"):
        return "tripped", "the watchdog stopped petting, so the board should have reset by itself"
    return "abrupt", ("it ended abruptly, with no shutdown recorded: a freeze, a power cut or a reset. (The journal "
                      "is written to disk every few minutes, so the very last lines of a freeze are often lost.)")


def parse_boots(text):
    """`journalctl --list-boots -o json` (or the plain table) -> [{idx, first, last}], newest last."""
    out = []
    t = (text or "").strip()
    if t.startswith("["):
        try:
            for b in json.loads(t):
                out.append({"idx": int(b.get("index", 0)), "first": b.get("first_entry"), "last": b.get("last_entry")})
            return out
        except (ValueError, TypeError, AttributeError):
            out = []
    for line in t.splitlines():
        m = re.match(r"^\s*(-?\d+)\s+[0-9a-f]{32}\s+(.+?)\s{1,}((?:[A-Z][a-z]{2} )?\d{4}-\d{2}-\d{2} [\d:]+(?: [A-Z+0-9-]+)?)\s*$",
                     line)
        if m:
            out.append({"idx": int(m.group(1)), "first": m.group(2).strip(), "last": m.group(3).strip()})
    return out


def blacklisted(text_by_file, module=MODULE):
    hits = []
    for path, text in sorted(text_by_file.items()):
        for line in (text or "").splitlines():
            f = line.split("#", 1)[0].split()
            if len(f) >= 2 and f[0] == "blacklist" and f[1].replace("-", "_") == module:
                hits.append(path)
            if len(f) >= 3 and f[0] == "install" and f[1].replace("-", "_") == module and f[2] in ("/bin/false", "/bin/true"):
                hits.append(path + " (install %s)" % f[2])
    return sorted(set(hits))


# ---- gathering (read-only) -----------------------------------------------------------------------------------------

def watchdog_devices():
    base = os.path.join(sysfs(), "class", "watchdog")
    out = []
    try:
        names = sorted(n for n in os.listdir(base) if re.fullmatch(r"watchdog\d+", n))
    except OSError:
        return out
    for n in names:
        d = {"name": n}
        for k in ("identity", "timeout", "state", "nowayout", "status", "bootstatus", "timeleft"):
            v = _read(os.path.join(base, n, k))
            if v is not None:
                d[k] = v
        try:
            d["driver"] = os.path.basename(os.path.realpath(os.path.join(base, n, "device", "driver")))
        except OSError:
            pass
        out.append(d)
    return out


def module_params(module=MODULE):
    base = os.path.join(sysfs(), "module", module)
    if not os.path.isdir(base):
        return None
    out = {}
    try:
        for k in os.listdir(os.path.join(base, "parameters")):
            v = _read(os.path.join(base, "parameters", k))
            if v is not None:
                out[k] = v
    except OSError:
        pass
    return out


def holders(names=("watchdog", "watchdog0")):
    """[(pid, comm)] of the processes that have /dev/watchdog or /dev/watchdog0 open (root sees them all)."""
    want = {"/dev/" + n for n in names}
    out = []
    try:
        pids = sorted((d for d in os.listdir(proc()) if d.isdigit()), key=int)
    except OSError:
        return out
    for pid in pids:
        fd_dir = os.path.join(proc(), pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target in want:
                out.append((int(pid), _read(os.path.join(proc(), pid, "comm"), 64) or "?"))
                break
    return out


def modprobe_files():
    out = {}
    for d in MODPROBE_DIRS:
        try:
            names = os.listdir(p(d))
        except OSError:
            continue
        for n in names:
            if n.endswith(".conf"):
                t = _read(os.path.join(p(d), n), 1 << 20)
                if t and MODULE.replace("_", "") in t.replace("_", "").replace("-", ""):
                    out[os.path.join(d, n)] = t
    return out


def root_reserve():
    try:
        s = os.statvfs(p("/") or "/")
    except OSError:
        return None
    return {"free": s.f_bavail * s.f_frsize, "reserve": max(0, s.f_bfree - s.f_bavail) * s.f_frsize,
            "size": s.f_blocks * s.f_frsize}


def gather(run=run_cmd, prev="-1"):
    """Everything the report needs. Only reads. `prev` is the boot to look back at (-1 the one before this)."""
    def jc(*args):
        rc, out = run(["journalctl", "--no-pager", "-q"] + list(args), 120)
        return out if rc == 0 else ""

    def kernel(boot, pattern, n):
        out = jc("-k", "-b", boot, "-g", pattern, "--case-sensitive=false", "-n", str(n), "-o", "short-iso")
        if out.strip():
            return out
        rx = re.compile(pattern, re.I)           # a journalctl without pattern matching: filter here
        return "\n".join(l for l in jc("-k", "-b", boot, "-n", str(n * 5), "-o", "short-iso").splitlines() if rx.search(l))

    def sc(*args):
        rc, out = run(["systemctl"] + list(args), 20)
        return out.strip()

    r = {"devices": watchdog_devices(), "params": module_params(), "holders": holders(),
         "blacklist": blacklisted(modprobe_files()), "reserve": root_reserve()}
    r["status"] = None
    t = _read(p(WATCHDOG_STATUS), 1 << 20)
    if t:
        try:
            r["status"] = json.loads(t)
        except ValueError:
            pass
    r["enabled"] = sc("is-enabled", UNIT) or "?"
    r["active"] = sc("is-active", UNIT) or "?"
    r["restarts"] = sc("show", "-p", "NRestarts", "--value", UNIT)
    r["runtime_watchdog"] = sc("show", "-p", "RuntimeWatchdogUSec", "--value")
    r["tco_now"] = parse_tco(kernel("0", "sp5100|TCO|watchdog", MAX_LINES))
    r["boots"] = parse_boots(jc("--list-boots", "-o", "json") or jc("--list-boots"))[-5:]
    r["prev"] = prev
    r["has_prev"] = bool(jc("-b", prev, "-n", "1").strip())
    if r["has_prev"]:
        r["tail"] = jc("-b", prev, "-n", str(TAIL), "-o", "short-iso")
        kern = kernel(prev, KERNEL_GREP, MAX_LINES * 10)
        r["tco_prev"] = parse_tco(kern)
        r["signs"], r["signs_last"] = count_signs(kern)
        r["wd_prev"] = parse_watchdog_lines(jc("-b", prev, "-u", UNIT, "-n", "200", "-o", "short-iso"))
        r["netwatch_prev"] = jc("-b", prev, "-u", "ollama1-netwatch.service", "-n", "10", "-o", "short-iso")
        r["ended"] = how_it_ended(r["tail"], r["signs"], r["wd_prev"])
    r["pstore"] = pstore_files()
    return r


def pstore_files():
    """Crash dumps the kernel left in pstore (systemd-pstore moves them to /var/lib/systemd/pstore)."""
    out = []
    for d in (p("/var/lib/systemd/pstore"), os.path.join(sysfs(), "fs", "pstore")):
        try:
            for root, _dirs, files in os.walk(d):
                for f in files:
                    out.append(os.path.join(root, f))
        except OSError:
            pass
    return sorted(out)[-10:]


# ---- the verdicts --------------------------------------------------------------------------------------------------

def verdicts(r):
    """{reason id: (word, evidence)}. word: LIKELY, possible, ruled out, unknown, or a fact for now."""
    v = {}
    hw = [d for d in r["devices"] if "software" not in (d.get("identity") or "").lower()]
    tco = r.get("tco_now") or {}
    tcp = r.get("tco_prev") or {}
    wd = r.get("wd_prev") or {}
    signs = r.get("signs") or {}
    ended = (r.get("ended") or ("?", ""))[0]
    if tco.get("disabled") or tco.get("no_mmio") or tcp.get("disabled") or tcp.get("no_mmio"):
        v["driver"] = ("LIKELY", "the kernel says the board's timer is off or has no address (see the TCO lines)")
    elif not hw:
        v["driver"] = ("LIKELY", "no hardware watchdog in /sys/class/watchdog now")
    elif r.get("has_prev") and wd.get("no_device"):
        v["driver"] = ("LIKELY", "the last boot's service said it found no hardware watchdog")
    else:
        v["driver"] = ("ruled out", "%s is there now (%s)" % (hw[0]["name"], hw[0].get("identity", "?")))
    if r["blacklist"]:
        v["blacklist"] = ("possible" if not hw else "a fact, harmless now",
                          "blacklisted in %s; only the service's own modprobe loads it" % ", ".join(r["blacklist"]))
    else:
        v["blacklist"] = ("ruled out", "no blacklist line for %s" % MODULE)
    if r["enabled"] != "enabled" or r["active"] != "active":
        v["service"] = ("LIKELY", "now: %s, %s" % (r["enabled"], r["active"]))
    elif r.get("has_prev") and not wd.get("armed"):
        v["service"] = ("possible", "the last boot's journal has no 'armed' line from the service")
    else:
        v["service"] = ("ruled out" if r.get("has_prev") else "unknown",
                        "enabled and active now%s" % ("; it armed in the last boot" if wd.get("armed") else ""))
    others = [h for h in r["holders"] if "ollama1" not in h[1] and "python" not in h[1]]
    rw = (r.get("runtime_watchdog") or "").strip()
    if others or (rw and rw not in ("0", "infinity", "0s", "")) or wd.get("held_by_other"):
        v["holder"] = ("LIKELY", "held by %s; systemd RuntimeWatchdogUSec=%s" % (
            ", ".join("%s (%d)" % (c, pid) for pid, c in others) or "another program in the last boot", rw or "?"))
    else:
        v["holder"] = ("ruled out", "no other program has it open; RuntimeWatchdogUSec=%s" % (rw or "?"))
    if not r.get("has_prev"):
        for k in ("paused", "probe", "clean_stop", "not_a_hang"):
            v[k] = ("unknown", "the previous boot's journal is not there (journalctl --list-boots)")
    else:
        if wd.get("paused_last") or ended == "suspend":
            v["paused"] = ("LIKELY", "the last boot %s" % (
                "ended in a suspend it never came out of" if ended == "suspend" else
                "closed the watchdog for a sleep and never reopened it"))
        elif signs.get("suspend_entry"):
            v["paused"] = ("possible", "%d suspend(s) in the last boot, each with a resume" % signs["suspend_entry"])
        else:
            v["paused"] = ("ruled out", "no suspend in the last boot")
        heavy = [k for k in ("nvrm_assert", "gsp_reset", "soft_lockup", "rcu_stall", "hung_task", "no_space") if signs.get(k)]
        if wd.get("armed") and not wd.get("probe_failed") and not wd.get("paused_last") and ended not in ("clean",):
            v["probe"] = ("LIKELY" if heavy else "possible",
                          "armed, and not one probe failed%s" % ((": while the kernel logged " + ", ".join(heavy)) if heavy else ""))
        elif wd.get("probe_failed"):
            v["probe"] = ("ruled out", "%d probe(s) failed in the last boot%s" % (
                wd["probe_failed"], "; it tripped" if wd.get("tripped") else ""))
        else:
            v["probe"] = ("unknown", "the watchdog's own lines don't say")
        if ended == "shutdown_hang" or (wd.get("stopped_disarmed") and ended != "clean"):
            v["clean_stop"] = ("LIKELY", "the service stopped cleanly (disarmed) and the shutdown never finished")
        else:
            v["clean_stop"] = ("ruled out", "no unfinished shutdown")
        if ended == "clean":
            v["not_a_hang"] = ("LIKELY", "the last boot was shut down or rebooted on purpose")
        elif signs.get("wifi_drop") and ended == "abrupt":
            boots = r.get("boots") or []
            v["not_a_hang"] = ("possible", "%d Wi-Fi disconnect line(s); its last line is from %s" % (
                signs["wifi_drop"], boots[-2].get("last") if len(boots) > 1 else "?"))
        else:
            v["not_a_hang"] = ("unknown", "a power cut and a freeze look the same in the journal")
    boot = 0
    for d in hw:
        try:
            boot |= int(d.get("bootstatus", "0"), 0)
        except ValueError:
            pass
    if boot & WDIOF_CARDRESET or tco.get("fired"):
        v["no_reset"] = ("ruled out", "the driver says this boot came from a watchdog reset: the board does reset")
    else:
        v["no_reset"] = ("unknown", "never proven on this board: do the drill in the README once")
    return v


def fmt_bytes(n):
    for unit, size in (("GiB", 1 << 30), ("MiB", 1 << 20)):
        if n >= size:
            return "%.1f %s" % (n / size, unit)
    return "%d B" % n


def render(r, now=None):
    now = time.time() if now is None else now
    L = ["Watchdog doctor (read-only)", ""]
    L.append("THE DRIVER AND THE TIMER")
    hw = r["devices"]
    if not hw:
        L.append("  no watchdog in /sys/class/watchdog: nothing can reset this box by hardware")
    for d in hw:
        L.append("  %s: %s, driver %s, timeout %s s, state %s, nowayout %s, bootstatus %s" % (
            d["name"], d.get("identity", "?"), d.get("driver", "?"), d.get("timeout", "?"), d.get("state", "?"),
            d.get("nowayout", "?"), d.get("bootstatus", "?")))
    if r["params"] is None:
        L.append("  module %s: not loaded" % MODULE)
    else:
        L.append("  module %s: loaded; %s" % (MODULE, ", ".join("%s=%s" % kv for kv in sorted(r["params"].items())) or "no parameters"))
    if r["holders"]:
        L.append("  /dev/watchdog held by: " + ", ".join("%s (pid %d)" % (c, pid) for pid, c in r["holders"]))
    else:
        L.append("  /dev/watchdog held by: nobody (run it with sudo to see every process)")
    L.append("  systemd RuntimeWatchdogUSec: %s" % (r.get("runtime_watchdog") or "?"))
    if r["blacklist"]:
        L.append("  blacklisted in: " + ", ".join(r["blacklist"]))
    st = r.get("status") or {}
    L.append("  ollama1-watchdog: %s, %s, %s restart(s); status: %s%s" % (
        r["enabled"], r["active"], r.get("restarts") or "?", st.get("state", "no status file"),
        (" (" + st["message"] + ")") if st.get("message") else ""))
    tco = r.get("tco_now") or {}
    L.append("  TCO in this boot's kernel log: %s" % _tco_words(tco))
    for line in tco.get("lines", [])[:6]:
        L.append("      " + line)
    res = r.get("reserve")
    if res:
        L.append("  root filesystem: %s free for users, %s more that only root can write (the watchdog's probe is root)"
                 % (fmt_bytes(res["free"]), fmt_bytes(res["reserve"])))
    L.append("")
    L.append("THE PREVIOUS BOOT" + ("" if r.get("prev", "-1") == "-1" else " (journalctl -b %s)" % r["prev"]))
    if not r.get("has_prev"):
        L.append("  not in the journal (is /var/log/journal there? journalctl --list-boots)")
    else:
        boots = r.get("boots") or []
        back = -int(r.get("prev", "-1")) + 1
        if len(boots) >= back:
            L.append("  it ran from %s to %s; this boot began %s" % (boots[-back].get("first"), boots[-back].get("last"),
                                                                    boots[-1].get("first")))
        kind, words = r["ended"]
        L.append("  how it ended: " + words)
        signs = r.get("signs") or {}
        shown = ["%s %d%s" % (k, n, "+" if n >= MAX_LINES * 10 else "") for k, n in signs.items() if n]
        L.append("  kernel log: " + (", ".join(shown) if shown else "nothing of note"))
        for k in ("gsp_reset", "nvrm_assert", "soft_lockup", "hung_task", "panic", "no_space", "wifi_drop"):
            if (r.get("signs_last") or {}).get(k):
                L.append("      last %s: %s" % (k, r["signs_last"][k]))
        wd = r.get("wd_prev") or {}
        L.append("  ollama1-watchdog then: %s" % ", ".join(w for w in (
            "armed" if wd.get("armed") else "NEVER ARMED",
            ("%d probe(s) failed" % wd["probe_failed"]) if wd.get("probe_failed") else "no probe failed",
            "TRIPPED" if wd.get("tripped") else "",
            "LEFT PAUSED FOR SLEEP" if wd.get("paused_last") else "",
            "stopped cleanly (disarmed)" if wd.get("stopped_disarmed") else "",
            "found no device" if wd.get("no_device") else "",
            "the device was held by another program" if wd.get("held_by_other") else "") if w))
        tcp = r.get("tco_prev") or {}
        L.append("  TCO in that boot's kernel log: %s" % _tco_words(tcp))
        if r.get("netwatch_prev", "").strip():
            L.append("  ollama1-netwatch then (last lines):")
            for line in r["netwatch_prev"].strip().splitlines()[-5:]:
                L.append("      " + line[:200])
        L.append("  its last lines:")
        for line in (r.get("tail") or "").strip().splitlines()[-15:]:
            L.append("      " + line[:200])
    if r.get("pstore"):
        L.append("  crash dumps (pstore): " + ", ".join(r["pstore"][-4:]))
    L.append("")
    L.append("WHY THE WATCHDOG MAY NOT HAVE RESET IT")
    v = verdicts(r)
    for key, title in REASONS:
        word, ev = v.get(key, ("unknown", ""))
        L.append("  %-10s %-11s %s: %s" % (key, word, title, ev))
    return "\n".join(L)


def _tco_words(t):
    if not t.get("seen") and not t.get("lines"):
        return "no sp5100/TCO line"
    w = []
    if t.get("initialized"):
        w.append("initialized, heartbeat %d s, nowayout %d" % (t["initialized"]["heartbeat"], t["initialized"]["nowayout"]))
    if t.get("disabled"):
        w.append("WATCHDOG HARDWARE IS DISABLED (the BIOS has the timer off)")
    if t.get("no_mmio"):
        w.append("NO MMIO ADDRESS (the driver gave up)")
    if t.get("busy_io"):
        w.append("its I/O address is taken")
    if t.get("fired"):
        w.append("the last reset was the watchdog's")
    if t.get("did_not_stop"):
        w.append("'watchdog did not stop' x%d (closed without the magic close: armed on purpose)" % t["did_not_stop"])
    return "; ".join(w) or "lines seen, nothing decisive"


def worst(r):
    """1 when something says the watchdog cannot work now (no device, not enabled or active, held by another)."""
    v = verdicts(r)
    return 1 if any(v[k][0] == "LIKELY" for k in ("driver", "service", "holder")) else 0
