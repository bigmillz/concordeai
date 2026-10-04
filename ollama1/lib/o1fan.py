"""Fans at full speed while the server works (6b385): the graphics
card's fan and every fan header the motherboard's chip lets the kit control go
to 100% whenever anything is running, stay there for one minute after the work
ends, and then go back to exactly the setting they had before (automatic).
Never at 100% all the time: that wears the bearings.

What counts as "working" is what auto sleep already counts as busy
(lib/o1idle.py), read with the same probes and the same limits:

  * a request in flight (the gateway's activity file);
  * the graphics card at GPU_IDLE_PCT or more;
  * a long job running (stability-test.sh and the like: o1idle.tools_running);
  * a 1-minute load average above LOAD_BUSY.

o1sleep.busy_reasons (a download, an update, a backup) is not used: those keep
the server awake but are not heat.

Which outputs (sysfs, found by chip name, never by hwmonN number):

  amdgpu          pwm1 (the card's fan): pwm1_enable 1 = manual, 2 = automatic
  nct67xx, it87.. pwm1..pwm7 (the motherboard's Super-IO chip; `modprobe
                  nct6775` brings the NCT6797D up): pwmN_enable 1 = manual,
                  5 (smart fan IV) and the rest are the chip's own control

An output the kit cannot write is skipped. Writing is always "manual, 255"
(the enable file first, then the value); the kit never writes a value below
255, except to put an output back as it was.

Fail-safe:
  * before the first change, each output's pwmN_enable (and its pwm when it was
    manual) is saved to /var/lib/ollama1/fan.json, with the boot id. A service
    that restarts, or crashes, in the middle of a hold finds it and puts the
    outputs back as they were, not as they are now. A different boot id means
    the machine restarted since: the hardware is already as it was, and the
    file is dropped;
  * a clean stop (SIGTERM) restores at once; a crash leaves the fans at 100%
    (ExecStopPost restores only after a clean stop), and the restart takes over;
  * a temperature over its limit (CPU 80 C, graphics card junction 90 C, NVMe
    70 C) forces 100% regardless of load, until it is 10 C under the limit;
  * after a wake amdgpu may have reset its fan to automatic: every tick checks
    the outputs it holds and writes them again.

The state file and the status file hold numbers and sensor names only; never
anything anyone asked.
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import o1common
import o1cpu
import o1gpu
import o1idle
from o1common import read_json_safe, write_json_atomic

POLL_S = 2
HOLD_S = 60                      # full speed for this long after the work ends
FULL = 255
HYST_C = 10                      # a temperature override ends this far under its limit
STATUS_STALE_S = 15              # a status file older than this: the service isn't running
TOOLS_EVERY_S = 6                # /proc is walked at most this often
LIMIT_CPU_C, LIMIT_GPU_C, LIMIT_NVME_C = 80, 90, 70
OLD_UNIT = "ollama1-gpu-fan.service"          # the hand-made full-speed-always experiment
MODULES_CONF = "/etc/modules-load.d/ollama1-fan.conf"
SUPER_IO_RX = re.compile(r"^(nct67\d\d|it8\d\d\d[a-z]?|f718\d\d[a-z]*|w836\d\d[a-z]*)$")   # the motherboard's fan chips
CPU_CHIPS = ("k10temp", "zenpower", "coretemp")
CPU_LABELS = ("Tctl", "Tdie", "Package id 0")


def _sys():
    return os.environ.get("OLLAMA1_SYS", "/sys")


def state_path():
    return o1common.p("/var/lib/ollama1/fan.json")


def status_path():
    return o1common.p("/run/ollama1/fan.json")


def read_boot_id():
    t = o1cpu.read_text("/proc/sys/kernel/random/boot_id")
    return (t or "").strip() or None


def _int(v):
    try:
        return int(str(v).strip(), 0)
    except (TypeError, ValueError):
        return None


class SysfsIO:
    """One write() per file, as sysfs wants."""

    def read(self, path):
        return o1cpu.read_text(path)

    def write(self, path, value):
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, value.encode("ascii"))
        finally:
            os.close(fd)


# ---- finding the outputs ----------------------------------------------------------

class Output:
    def __init__(self, hwmon, chip, kind, n):
        self.dir, self.chip, self.kind, self.n = hwmon, chip, kind, n
        self.key = "%s/pwm%d" % (os.path.basename(hwmon), n)
        self.pwm = "%s/pwm%d" % (hwmon, n)
        self.enable = self.pwm + "_enable"
        self.rpm = "%s/fan%d_input" % (hwmon, n)

    @property
    def label(self):
        return "GPU fan" if self.kind == "gpu" else "case/CPU fan %d" % self.n


def _writable(path):
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return bool(mode & 0o200) and os.access(path, os.W_OK)


def find_outputs(sysroot=None):
    """Every output the kit may control: [Output], found by chip name. An output
    whose pwm or pwm_enable file can't be written is left out."""
    base = (sysroot or _sys()) + "/class/hwmon"
    out = []
    try:
        dirs = sorted(os.listdir(base))
    except OSError:
        return out
    for d in dirs:
        hw = base + "/" + d
        name = (o1cpu.read_text(hw + "/name") or "").strip()
        kind = "gpu" if name == "amdgpu" else "case" if SUPER_IO_RX.match(name) else None
        if not kind:
            continue
        try:
            files = os.listdir(hw)
        except OSError:
            continue
        for f in sorted(files):
            m = re.fullmatch(r"pwm(\d+)", f)
            if not m or not 1 <= int(m.group(1)) <= 16:
                continue
            o = Output(hw, name, kind, int(m.group(1)))
            if _writable(o.pwm) and _writable(o.enable):
                out.append(o)
    return out


def controlling_text(outs):
    gpu = sum(1 for o in outs if o.kind == "gpu")
    case = len(outs) - gpu
    parts = []
    if gpu:
        parts.append("GPU fan" if gpu == 1 else "%d GPU fans" % gpu)
    if case:
        parts.append("%d case/CPU fan output%s" % (case, "" if case == 1 else "s"))
    return " + ".join(parts) if parts else "no fan outputs it can write"


def rpm_of(o, io):
    v = o1cpu.number(io.read(o.rpm), 0, 100000)
    return int(v) if v is not None else None


# ---- temperatures -----------------------------------------------------------------

def read_temps(sysroot=None):
    """[(key, label, celsius, limit)] for the CPU, the graphics card's junction and
    each NVMe drive: the sensors that can force the fans to 100%."""
    base = (sysroot or _sys()) + "/class/hwmon"
    out = []
    try:
        dirs = sorted(os.listdir(base))
    except OSError:
        return out
    for d in dirs:
        hw = base + "/" + d
        name = (o1cpu.read_text(hw + "/name") or "").strip()
        if name not in CPU_CHIPS and name not in ("amdgpu", "nvme"):
            continue
        sensors = {}
        for f in sorted(glob.glob(glob.escape(hw) + "/temp*_input")):
            m = re.fullmatch(r".*/temp(\d+)_input", f)
            c = o1cpu.number(o1cpu.read_text(f), *o1cpu.TEMP_RANGE, scale=1000.0)
            if m and c is not None:
                label = (o1cpu.read_text("%s/temp%s_label" % (hw, m.group(1))) or "").strip() or "temp" + m.group(1)
                sensors.setdefault(label, c)
        if name in CPU_CHIPS:
            vals = [sensors[k] for k in CPU_LABELS if k in sensors]
            if vals:
                out.append(("cpu:" + d, "CPU", max(vals), LIMIT_CPU_C))
        elif name == "amdgpu":
            label = "junction" if "junction" in sensors else "edge" if "edge" in sensors else None
            if label:
                out.append(("gpu:" + d, "GPU junction" if label == "junction" else "GPU edge", sensors[label],
                            LIMIT_GPU_C))
        elif name == "nvme":
            if "Composite" in sensors:
                out.append(("nvme:" + d, "NVMe", sensors["Composite"], LIMIT_NVME_C))
    return out


# ---- the machine ------------------------------------------------------------------

class Fan:
    """One tick every POLL_S seconds. `probes` supplies inflight (an int, or None
    when it can't be told), gpu_busy, tools (a list) and loadavg; `clock` is
    monotonic (the hold must not jump); `wall` stamps the status file."""

    def __init__(self, probes, clock=time.monotonic, wall=time.time, log=print, io=None, sysroot=None,
                 boot_id=read_boot_id, hold_s=HOLD_S, tools_every=TOOLS_EVERY_S):
        self.p, self.clock, self.wall, self.log = probes, clock, wall, log
        self.io = io or SysfsIO()
        self.sysroot = sysroot
        self.boot_id = boot_id
        self.hold_s = hold_s
        self.tools_every = tools_every
        self.orig = {}                 # key -> {"enable": int, "pwm": int | None}: as it was before the kit touched it
        self.engaged = False           # True while the kit holds any output
        self.last_work = None
        self.hot = {}                  # temperature key -> label, while over its limit
        self.failed = set()
        self.mode, self.why, self.left = "auto", "idle", 0
        self.seen = None               # what it last said it controls
        self.said = None
        self.tools_at, self.tools = -1e9, []
        self._load_state()

    # -- the saved originals ---------------------------------------------------------
    def _load_state(self):
        st = read_json_safe(state_path(), None, max_bytes=65536)
        if not isinstance(st, dict):
            return
        orig = st.get("orig")
        same_boot = bool(st.get("boot")) and st.get("boot") == self.boot_id()
        if not isinstance(orig, dict) or not orig or not same_boot:
            # another boot, or nothing in it: the hardware is as it was
            self._drop_state()
            return
        good = {}
        for k, v in orig.items():
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("enable"), int):
                good[k] = {"enable": v["enable"], "pwm": v["pwm"] if isinstance(v.get("pwm"), int) else None}
        if good:
            self.orig, self.engaged = good, True
            self.last_work = self.clock()        # a restart in the middle of a hold holds one more minute
            self.log("found fans left at full speed by an earlier run: %d output%s, put back after the hold"
                     % (len(good), "" if len(good) == 1 else "s"))
        else:
            self._drop_state()

    def _save_state(self):
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        write_json_atomic(state_path(), {"v": 1, "boot": self.boot_id(), "orig": self.orig}, mode=0o600)

    def _drop_state(self):
        try:
            os.unlink(state_path())
        except OSError:
            pass

    # -- what counts as working -----------------------------------------------------
    def _probe(self, name):
        try:
            return self.p[name]()
        except Exception:
            return None

    def _tools(self, now):
        """The long jobs running; /proc is walked at most every tools_every seconds."""
        if now - self.tools_at >= self.tools_every:
            self.tools_at = now
            self.tools = self._probe("tools") or []
        return self.tools

    def working(self, now=None):
        """(working?, the reasons): the probes auto sleep uses, with its limits."""
        now = self.clock() if now is None else now
        why = []
        n = self._probe("inflight")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            why.append("a request is running")
        g = self._probe("gpu_busy")
        if isinstance(g, (int, float)) and g >= o1idle.GPU_IDLE_PCT:
            why.append("the graphics card is busy (%d%%)" % g)
        t = self._tools(now)
        if t:
            why.append("running: " + ", ".join(sorted(str(x) for x in t)))
        la = self._probe("loadavg")
        if isinstance(la, (int, float)) and la > o1idle.LOAD_BUSY:
            why.append("the machine is busy (load %.1f)" % la)
        return bool(why), why

    def overheated(self):
        """(hot?, the reasons): a sensor over its limit stays hot until HYST_C under it."""
        for key, label, c, limit in read_temps(self.sysroot):
            if c >= limit:
                self.hot[key] = "%s %.0f C (limit %d)" % (label, c, limit)
            elif key in self.hot and c < limit - HYST_C:
                del self.hot[key]
        return bool(self.hot), sorted(self.hot.values())

    # -- writing the outputs ---------------------------------------------------------
    def _w(self, path, value):
        self.io.write(path, str(value))

    def _fail(self, o, e):
        self.failed.add(o.key)
        self.log("can't write %s (%s): left alone" % (o.key, getattr(e, "strerror", None) or type(e).__name__))

    def engage(self, outs):
        """Every output manual at 255, checked again every tick (a wake may have reset it)."""
        outs = [o for o in outs if o.key not in self.failed]
        fresh = []
        for o in outs:
            if o.key in self.orig:
                continue
            en = _int(self.io.read(o.enable))
            if en is None:
                self.failed.add(o.key)
                continue
            pwm = _int(self.io.read(o.pwm)) if en == 1 else None
            self.orig[o.key] = {"enable": en, "pwm": pwm}
            fresh.append(o)
        if fresh:
            self._save_state()                  # the originals are on disk before anything changes
        for o in outs:
            if o.key not in self.orig:
                continue
            try:
                if _int(self.io.read(o.enable)) != 1:
                    self._w(o.enable, 1)
                if _int(self.io.read(o.pwm)) != FULL:
                    self._w(o.pwm, FULL)
            except OSError as e:
                if o in fresh and _int(self.io.read(o.enable)) == self.orig[o.key]["enable"]:
                    del self.orig[o.key]        # nothing changed: nothing to put back
                    self._save_state()
                self._fail(o, e)
        self.engaged = bool(self.orig)
        if not self.orig:
            self._drop_state()

    def release(self):
        """Every held output back as it was; the state file goes when all are."""
        by_key = {o.key: o for o in find_outputs(self.sysroot)}
        left = {}
        for key, was in self.orig.items():
            o = by_key.get(key)
            if o is None:
                continue                         # the output is gone: nothing to put back
            try:
                if was["enable"] == 1 and was["pwm"] is not None:
                    self._w(o.pwm, was["pwm"])   # it was manual: its value back
                self._w(o.enable, was["enable"])
            except OSError:
                left[key] = was
        self.orig = left
        if left:
            self.log("couldn't put %d output%s back yet; trying again" % (len(left), "" if len(left) == 1 else "s"))
            self._save_state()
        else:
            self.engaged = False
            self._drop_state()
        return not left

    # -- one tick --------------------------------------------------------------------
    def tick(self):
        now = self.clock()
        working, wwhy = self.working(now)
        hot, hwhy = self.overheated()
        outs = find_outputs(self.sysroot)
        usable = [o for o in outs if o.key not in self.failed]
        text = controlling_text(usable)
        if text != self.seen:
            self.log("controlling " + text)
            self.seen = text
        if working:
            self.last_work = now
        if hot:
            mode, why, left = "full", "too warm: " + "; ".join(hwhy), 0
        elif working:
            mode, why, left = "full", "; ".join(wwhy), 0
        elif self.last_work is not None and now - self.last_work < self.hold_s:
            mode, why, left = "hold", "the work ended", int(self.hold_s - (now - self.last_work) + 0.999)
        else:
            mode, why, left = "auto", "idle", 0
        if mode in ("full", "hold"):
            self.engage(outs)
        elif self.engaged:
            self.release()
        if mode != self.mode or (mode == "full" and why != self.why):
            self.log({"full": "full speed: ", "hold": "holding full speed: ", "auto": "automatic: "}[mode] + why)
        self.mode, self.why, self.left = mode, why, left
        self.write_status(usable)
        return mode, why

    def shutdown(self):
        """A clean stop: everything held goes back as it was."""
        if self.engaged:
            self.release()

    # -- status ------------------------------------------------------------------------
    def write_status(self, outs):
        live = snapshot(outs, self.io, self.sysroot)
        st = {"at": int(self.wall()), "mode": self.mode, "why": self.why, "hold_left": self.left,
              "controlling": controlling_text(outs), "outputs": live["outputs"], "temps": live["temps"],
              "hot": sorted(self.hot.values())}
        st["line"] = status_line(st)
        try:
            os.makedirs(os.path.dirname(status_path()), exist_ok=True)
            write_json_atomic(status_path(), st, mode=0o644)
        except OSError:
            pass


# ---- status text --------------------------------------------------------------------

def snapshot(outs, io=None, sysroot=None):
    """Live readings: each output's setting and rpm, and the temperatures."""
    io = io or SysfsIO()
    rows = []
    for o in outs:
        rows.append({"label": o.label, "chip": o.chip, "enable": _int(io.read(o.enable)),
                     "pwm": _int(io.read(o.pwm)), "rpm": rpm_of(o, io)})
    temps = [{"label": label, "c": round(c, 1)} for _k, label, c, _l in read_temps(sysroot)]
    return {"outputs": rows, "temps": temps}


def _rpm_summary(rows):
    gpu = [r["rpm"] for r in rows if r["label"] == "GPU fan" and r["rpm"] is not None]
    case = [r["rpm"] for r in rows if r["label"] != "GPU fan" and r["rpm"]]
    parts = []
    if gpu:
        parts.append("GPU fan %d rpm" % gpu[0])
    if case:
        parts.append("case fans up to %d rpm" % max(case))
    return parts


def status_line(st):
    """One line for the admin panel's CPU card and the top of `status`."""
    mode = st.get("mode")
    if mode == "full":
        head = "Fans: 100%% (%s)" % (st.get("why") or "working")
    elif mode == "hold":
        head = "Fans: 100%% for %d s more, to cool down" % st.get("hold_left", 0)
    else:
        head = "Fans: automatic"
    rpm = _rpm_summary(st.get("outputs") or [])
    return head + ("  -  " + ", ".join(rpm) if rpm else "")


def read_status(path=None, now=None):
    """The service's status file, or None when it isn't fresh (the service is not running)."""
    st = read_json_safe(path or status_path(), None, max_bytes=16384)
    if not isinstance(st, dict) or not isinstance(st.get("at"), int):
        return None
    if abs((now if now is not None else time.time()) - st["at"]) > STATUS_STALE_S:
        return None
    return st


def panel_line(path=None, now=None):
    """The one status line, or None (no service, nothing to say) for the admin panel."""
    st = read_status(path, now)
    line = st.get("line") if st else None
    return line[:200] if isinstance(line, str) and line else None


def render_status(st, live):
    """The text of `ollama1-fan status`: `st` the service's status (or None), `live` a snapshot."""
    out = []
    if st is None:
        out.append("mode: the service isn't running (the fans are as the driver and BIOS leave them: automatic)")
    elif st["mode"] == "full":
        out.append("mode: full - %s" % st.get("why"))
    elif st["mode"] == "hold":
        out.append("mode: hold %ds - the work ended; full speed for one minute, then automatic" % st.get("hold_left", 0))
    else:
        out.append("mode: auto - nothing is running")
    if st is not None:
        out.append("controlling: " + st.get("controlling", "?"))
    rows = live["outputs"]
    for r in rows:
        m = {1: "manual", 2: "auto"}.get(r["enable"], "chip control %s" % r["enable"])
        out.append("  %-18s %-16s pwm %s/255  %s" % (r["label"], m, "?" if r["pwm"] is None else r["pwm"],
                                                    "%d rpm" % r["rpm"] if r["rpm"] is not None else "no rpm reading"))
    if not rows:
        out.append("  no fan outputs found (a motherboard chip needs: sudo modprobe nct6775)")
    if live["temps"]:
        out.append("temps: " + ", ".join("%s %.0f C" % (t["label"], t["c"]) for t in live["temps"]))
    else:
        out.append("temps: none read")
    if st is not None and st.get("hot"):
        out.append("too warm: " + "; ".join(st["hot"]))
    return "\n".join(out)


# ---- probes the service uses (the same ones auto sleep uses) -------------------------

class Gpu:
    """The card's busy percent, found once and looked for again until there is one."""

    def __init__(self):
        self.vendor, self.at = None, -1e9

    def busy(self):
        if not self.vendor and time.monotonic() - self.at > 60:
            self.at = time.monotonic()
            self.vendor = o1gpu.detect().get("vendor")
        return o1gpu.usage(self.vendor)["busy_pct"] if self.vendor else None


def probes():
    gpu = Gpu()
    return {"inflight": lambda: o1idle.read_activity(time.time())["inflight"], "gpu_busy": gpu.busy,
            "tools": o1idle.tools_running, "loadavg": o1idle.loadavg1}


# ---- the modules and setup -------------------------------------------------------------

def load_modules(run=subprocess.run, sysroot=None, log=print):
    """Bring the motherboard's fan chip up (nct6775 covers the NCT6797D) when no chip
    with fan outputs is showing. Best effort: never fatal. Returns True when one is there."""
    def board():
        return any(o.kind == "case" for o in find_outputs(sysroot))
    if board():
        return True
    try:
        r = run(["modprobe", "nct6775"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log("modprobe nct6775: %s" % ((r.stderr or "").strip()[:200] or "failed"))
    except (OSError, subprocess.SubprocessError) as e:
        log("modprobe nct6775: %s" % type(e).__name__)
    if board():
        return True
    log("no motherboard fan chip found (if the kernel log says ACPI resource conflict, see the README)")
    return False


def restore_after_stop(env=None, io=None, sysroot=None, boot_id=read_boot_id, log=print):
    """ExecStopPost: after a clean stop (SERVICE_RESULT=success) put anything still
    held back as it was; after a crash leave the fans at 100%, and the restart takes over."""
    env = os.environ if env is None else env
    if env.get("SERVICE_RESULT", "success") != "success":
        log("the service did not stop cleanly: fans left as they are for the restart to take over")
        return True
    fan = Fan({}, io=io, sysroot=sysroot, boot_id=boot_id, log=log)
    if not fan.engaged:
        return True
    return fan.release()


def run_systemctl(*args):
    try:
        r = subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def release_old_unit(systemctl=run_systemctl, io=None, sysroot=None, log=print):
    """The hand-made ollama1-gpu-fan.service (full speed always) is replaced by this:
    stopped, disabled, its file removed, and the card's fan given back to the driver."""
    path = o1common.p("/etc/systemd/system/" + OLD_UNIT)
    if not os.path.exists(path):
        return False
    systemctl("disable", "--now", OLD_UNIT)
    try:
        os.unlink(path)
    except OSError:
        pass
    systemctl("daemon-reload")
    io = io or SysfsIO()
    for o in find_outputs(sysroot):
        if o.kind == "gpu" and _int(io.read(o.enable)) == 1:
            try:
                io.write(o.enable, "2")          # the old unit left it manual
            except OSError:
                pass
    log("removed the old %s (full speed always); this service replaces it" % OLD_UNIT)
    return True


def setup(choice, systemctl=run_systemctl, sysroot=None, log=print, modules=None):
    """setup.sh's step. on: the old unit out, the chip's module loaded (and kept at boot
    only when a chip showed up), the service enabled and started. off: stopped (which puts
    the fans back), disabled, the boot-time module entry removed."""
    conf = o1common.p(MODULES_CONF)
    if choice == "on":
        release_old_unit(systemctl, sysroot=sysroot, log=log)
        chip = (modules or load_modules)(sysroot=sysroot, log=log)
        if chip:
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            with open(conf, "w") as f:
                f.write("# ollama1-fan: the motherboard's fan chip (NCT6797D and kin)\nnct6775\n")
        elif os.path.exists(conf):
            os.unlink(conf)
        ok = systemctl("enable", "ollama1-fan.service") and systemctl("restart", "ollama1-fan.service")
        log("the fans: %s" % ("on" if ok else "the service did not start (journalctl -u ollama1-fan)"))
        return 0 if ok else 1
    systemctl("disable", "--now", "ollama1-fan.service")
    if os.path.exists(conf):
        os.unlink(conf)
    log("the fans: off (automatic, as the driver and BIOS leave them)")
    return 0


# ---- the service ------------------------------------------------------------------------

def run_service(log=print):
    fan = Fan(probes(), log=log)
    stop, poke = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGUSR1, lambda *_: poke.set())          # the sleep hook: a wake, look now
    while not stop.is_set():
        try:
            fan.tick()
        except Exception as e:                                    # never stop; say what kind, not what it held
            log("tick failed: %s" % type(e).__name__)
        poke.wait(POLL_S)
        poke.clear()
    fan.shutdown()
    return 0
