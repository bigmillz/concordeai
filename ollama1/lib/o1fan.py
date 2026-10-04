"""Fan levels for the server (6b385, changed in 6b386): the graphics card's fan
and every fan header the motherboard's chip lets the kit control follow what
the server is doing, so it is cool when it works and quiet, and easy on the
bearings, when it does not:

  working     100%   a request, the card, a long job or the load says so
  hold100     100%   for 60 s after the work ends
  hold50       50%   for the next 60 s
  idle20       20%   from 120 s after the work ended (and from the start)

A new request at any time goes back to 100% and starts the sequence again.
A level is pwm = round(percent * 255 / 100): 20% is 51, 50% is 128. The
service always holds the outputs while it runs; it gives them back to their
own control (the BIOS's automatic) whenever it stops, for any reason.

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

Nothing is known about what is plugged into each header, so a low level is
checked and never trusted:

  * the first start measures each output's rpm at 100% (6 s), and remembers
    it in the state file;
  * 6 s after an output is set to 20% (or any low level) its rpm is read: 0,
    or under its fanN_min, means it stalled, and it is raised in steps of 10%
    (30, 40, 50 ...) to the lowest level that spins; that floor is remembered
    per output and logged once;
  * an output whose rpm is still 60% or more of its 100% rpm when asked for
    20% is a probable pump or a fixed header: it is set to 100% for good and
    logged;
  * an output that reads no rpm at 100% (nothing connected, or no reading) is
    only ever set to 100% when working, and left to its own control otherwise.

Fail-safe:
  * before the first change, each output's pwmN_enable (and its pwm when it was
    manual) is saved to /var/lib/ollama1/fan.json, with the boot id. A clean
    stop (SIGTERM), ExecStopPost after ANY exit (a crash, a kill, a watchdog
    restart), and the next start all put the outputs back as they were; a
    boot id from another boot means the machine restarted and the hardware is
    already as it was. Before the service runs at boot, the BIOS's control is
    what is in force;
  * the service tells systemd it is alive on every poll (sd_notify,
    WatchdogSec=10): a loop that hangs is restarted, and restored;
  * a temperature over its limit (CPU 80 C, graphics card junction 90 C, NVMe
    70 C) forces 100% regardless of load, until it is 10 C under the limit;
  * after a wake amdgpu may have reset its fan to automatic: every tick checks
    the outputs it holds and writes them again at the current level.

The state file and the status file hold numbers and sensor names only; never
anything anyone asked.
"""
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import o1common
import o1cpu
import o1work
from o1common import read_json_safe, write_json_atomic

POLL_S = 2
HOLD100_S = 60                   # 100% for this long after the work ends
HOLD50_S = 60                    # then 50% for this long, then idle
FULL_PCT, HALF_PCT, LOW_PCT = 100, 50, 20
STEP_PCT = 10                    # a stalled output is raised by this much
SETTLE_S = 6                     # a level is judged by rpm only after this long
PUMP_RATIO = 0.6                 # at 20% still this share of its 100% rpm: a pump or a fixed header
HYST_C = 10                      # a temperature override ends this far under its limit
STATUS_STALE_S = 15              # a status file older than this: the service isn't running
TOOLS_EVERY_S = 6                # /proc is walked at most this often (lib/o1work.py)
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


def pwm_of(pct):
    """The pwm value of a level: round(pct * 255 / 100), halves up (20% is 51, 50% is 128)."""
    return int(pct * 255 / 100 + 0.5)


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
        self.key = "%s/pwm%d" % (os.path.basename(hwmon), n)     # this boot's number: for the saved originals
        self.lkey = "%s/pwm%d" % (chip, n)                       # the chip's name: for what is learned about it
        self.pwm = "%s/pwm%d" % (hwmon, n)
        self.enable = self.pwm + "_enable"
        self.rpm = "%s/fan%d_input" % (hwmon, n)
        self.rpm_min = "%s/fan%d_min" % (hwmon, n)

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


def min_rpm_of(o, io):
    """The output's own minimum (fanN_min), 0 when it has none."""
    v = o1cpu.number(io.read(o.rpm_min), 0, 100000)
    return int(v) if v else 0


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
    monotonic (the levels must not jump); `wall` stamps the status file."""

    def __init__(self, probes, clock=time.monotonic, wall=time.time, log=print, io=None, sysroot=None,
                 boot_id=read_boot_id, tools_every=TOOLS_EVERY_S):
        self.p, self.clock, self.wall, self.log = probes, clock, wall, log
        self.io = io or SysfsIO()
        self.sysroot = sysroot
        self.boot_id = boot_id
        self.tools_every = tools_every
        self.orig = {}                 # key -> {"enable": int, "pwm": int | None}: as it was before the kit touched it
        self.learned = {}              # lkey -> {"rpm100", "min_pct", "always100", "pump_checked"}
        self.engaged = False           # True while the kit holds any output
        self.last_work = None
        self.hot = {}                  # temperature key -> label, while over its limit
        self.failed = set()
        self.level = {}                # key -> the percent it was last set to
        self.since = {}                # key -> when that level was set
        self.checked = {}              # key -> the level whose rpm was judged
        self.phase, self.pct, self.why, self.left = "idle20", LOW_PCT, "idle", 0
        self.seen = None               # what it last said it controls
        self.work = o1work.Work(probes, clock, tools_every)
        self._load_state()

    # -- the saved originals and what was learned ------------------------------------
    def _load_state(self):
        st = read_json_safe(state_path(), None, max_bytes=65536)
        if not isinstance(st, dict):
            return
        learned = {}
        for k, v in (st.get("learned") or {}).items() if isinstance(st.get("learned"), dict) else []:
            if isinstance(k, str) and isinstance(v, dict) and "rpm100" in v:
                learned[k] = {"rpm100": v["rpm100"] if isinstance(v["rpm100"], int) else None,
                              "min_pct": v["min_pct"] if isinstance(v.get("min_pct"), int) else LOW_PCT,
                              "always100": v.get("always100") is True, "pump_checked": v.get("pump_checked") is True}
        self.learned = learned
        orig = st.get("orig")
        if isinstance(orig, dict) and orig and st.get("boot") and st.get("boot") == self.boot_id():
            good = {}
            for k, v in orig.items():
                if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("enable"), int):
                    good[k] = {"enable": v["enable"], "pwm": v["pwm"] if isinstance(v.get("pwm"), int) else None}
            if good:
                self.orig, self.engaged = good, True
                self.last_work = self.clock()          # found still held: the whole sequence from the start
                self.log("found fans still held by an earlier run: %d output%s, put back when it stops"
                         % (len(good), "" if len(good) == 1 else "s"))
        self._persist()                                # another boot's originals go: the hardware is as it was

    def _persist(self):
        if not self.orig and not self.learned:
            try:
                os.unlink(state_path())
            except OSError:
                pass
            return
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        write_json_atomic(state_path(), {"v": 2, "boot": self.boot_id(), "orig": self.orig, "learned": self.learned},
                          mode=0o600)

    # -- what counts as working -----------------------------------------------------
    def working(self, now=None):
        """(working?, the reasons): lib/o1work.py, the probes and limits auto sleep uses."""
        return self.work.working(self.clock() if now is None else now)

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

    def target(self, o, pct):
        """The percent this output gets while the level is `pct`; None = leave it to its own control."""
        if pct >= FULL_PCT:
            return FULL_PCT
        L = self.learned.get(o.lkey)
        if L is None or L["always100"]:
            return FULL_PCT
        if not L["rpm100"]:
            return None                      # no rpm at 100%: nothing to check a low level against
        return min(FULL_PCT, max(pct, L["min_pct"]))

    def apply(self, outs, pct, now):
        """Every output to its level for `pct`, checked again every tick (a wake may have reset it)."""
        plan = [(o, self.target(o, pct)) for o in outs if o.key not in self.failed]
        fresh = []
        for o, tp in plan:
            if tp is None or o.key in self.orig:
                continue
            en = _int(self.io.read(o.enable))
            if en is None:
                self.failed.add(o.key)
                continue
            self.orig[o.key] = {"enable": en, "pwm": _int(self.io.read(o.pwm)) if en == 1 else None}
            fresh.append(o)
        if fresh:
            self._persist()                      # the originals are on disk before anything changes
        for o, tp in plan:
            if tp is None:
                if o.key in self.orig:
                    self.release_one(o)
                continue
            if o.key not in self.orig:
                continue
            try:
                if _int(self.io.read(o.enable)) != 1:
                    self._w(o.enable, 1)
                want = pwm_of(tp)
                if _int(self.io.read(o.pwm)) != want:
                    self._w(o.pwm, want)
            except OSError as e:
                if o in fresh and _int(self.io.read(o.enable)) == self.orig[o.key]["enable"]:
                    del self.orig[o.key]         # nothing changed: nothing to put back
                    self._persist()
                self._fail(o, e)
                continue
            if self.level.get(o.key) != tp:
                self.level[o.key], self.since[o.key] = tp, now
                self.checked.pop(o.key, None)
        self.engaged = bool(self.orig)

    def _restore(self, key, was, o):
        if was["enable"] == 1 and was["pwm"] is not None:
            self._w(o.pwm, was["pwm"])           # it was manual: its value back
        self._w(o.enable, was["enable"])

    def release_one(self, o):
        was = self.orig.get(o.key)
        try:
            self._restore(o.key, was, o)
        except OSError:
            return
        del self.orig[o.key]
        self.level.pop(o.key, None)
        self._persist()
        self.engaged = bool(self.orig)

    def release(self):
        """Every held output back as it was; what is kept is only what was learned."""
        by_key = {o.key: o for o in find_outputs(self.sysroot)}
        left = {}
        for key, was in self.orig.items():
            o = by_key.get(key)
            if o is None:
                continue                         # the output is gone: nothing to put back
            try:
                self._restore(key, was, o)
            except OSError:
                left[key] = was
        self.orig = left
        self.level.clear()
        if left:
            self.log("couldn't put %d output%s back yet; trying again" % (len(left), "" if len(left) == 1 else "s"))
        else:
            self.engaged = False
        self._persist()
        return not left

    # -- what the rpm says ---------------------------------------------------------------
    def sample(self, outs, now):
        """Judge each output's rpm once its level has had SETTLE_S seconds."""
        for o in outs:
            tp = self.level.get(o.key)
            if tp is None or o.key not in self.orig or now - self.since.get(o.key, now) < SETTLE_S:
                continue
            if tp >= FULL_PCT:
                self.measure(o)
            elif self.checked.get(o.key) != tp:
                self.checked[o.key] = tp
                self.judge(o, tp)

    def measure(self, o):
        """The rpm at 100%: the first reading is remembered (even none), later ones only when it moved a lot."""
        rpm = rpm_of(o, self.io)
        L = self.learned.get(o.lkey)
        if L is None:
            self.learned[o.lkey] = {"rpm100": rpm, "min_pct": LOW_PCT, "always100": False, "pump_checked": False}
            if rpm:
                self.log("%s: %d rpm at 100%%" % (o.label, rpm))
            else:
                self.log("%s: no rpm at 100%%: left to its own control except while working" % o.label)
            self._persist()
        elif rpm and (not L["rpm100"] or abs(rpm - L["rpm100"]) > 0.15 * L["rpm100"]):
            L["rpm100"] = rpm
            self._persist()

    def judge(self, o, pct):
        """A low level has settled: a pump or a fixed header, or a stall, or fine."""
        L = self.learned.get(o.lkey)
        rpm = rpm_of(o, self.io)
        if not L or not L["rpm100"] or rpm is None:
            return
        if pct <= LOW_PCT and not L["pump_checked"]:
            L["pump_checked"] = True
            if rpm >= PUMP_RATIO * L["rpm100"]:
                L["always100"] = True
                self.log("%s: still %d rpm of %d at %d%%: a pump or a fixed header, kept at 100%%"
                         % (o.label, rpm, L["rpm100"], pct))
                self._persist()
                return
            self._persist()
        if rpm == 0 or rpm < min_rpm_of(o, self.io):
            L["min_pct"] = min(FULL_PCT, pct + STEP_PCT)
            if L["min_pct"] >= FULL_PCT:
                L["always100"] = True
            self.log("%s: %d rpm at %d%%, stalled: its lowest level is now %d%%" % (o.label, rpm, pct, L["min_pct"]))
            self._persist()

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
        age = None if self.last_work is None else now - self.last_work
        left = 0
        if hot:
            phase, pct, why = "hot", FULL_PCT, "too warm: " + "; ".join(hwhy)
        elif working:
            phase, pct, why = "working", FULL_PCT, "; ".join(wwhy)
        elif any(o.lkey not in self.learned for o in usable):
            phase, pct, why = "calibrating", FULL_PCT, "measuring each fan at full speed (once)"
        elif age is not None and age < HOLD100_S:
            phase, pct, why, left = "hold100", FULL_PCT, "the work ended", int(HOLD100_S - age + 0.999)
        elif age is not None and age < HOLD100_S + HOLD50_S:
            phase, pct, why, left = "hold50", HALF_PCT, "the work ended", int(HOLD100_S + HOLD50_S - age + 0.999)
        else:
            phase, pct, why = "idle20", LOW_PCT, "idle"
        self.apply(outs, pct, now)
        self.sample(usable, now)
        if phase != self.phase or (phase in ("working", "hot") and why != self.why):
            self.log("%s: %s" % (phase, why))
        self.phase, self.pct, self.why, self.left = phase, pct, why, left
        self.write_status(usable)
        return phase, why

    def shutdown(self):
        """A stop: everything held goes back as it was."""
        if self.engaged:
            self.release()

    # -- status ------------------------------------------------------------------------
    def notes(self, o):
        L = self.learned.get(o.lkey)
        if L is None:
            return None, ""
        if L["always100"]:
            return L["min_pct"], "kept at 100%: a pump or a fixed header"
        if not L["rpm100"]:
            return None, "no rpm at 100%: left to its own control except while working"
        return L["min_pct"], ""

    def write_status(self, outs):
        live = snapshot(outs, self.io, self.sysroot)
        for row, o in zip(live["outputs"], outs):
            row["min_pct"], row["note"] = self.notes(o)
        st = {"at": int(self.wall()), "phase": self.phase, "pct": self.pct, "why": self.why, "hold_left": self.left,
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


def phase_text(st):
    """The phase in words: working / hold100 Ns / hold50 Ns / idle20 (and the two that force 100%)."""
    ph = st.get("phase")
    if ph in ("hold100", "hold50"):
        return "%s %ds" % (ph, st.get("hold_left", 0))
    return ph or "?"


def status_line(st):
    """One line for the admin panel's CPU card and the top of `status`."""
    ph = st.get("phase")
    pct = st.get("pct", 0)
    if ph in ("working", "hot", "calibrating"):
        head = "Fans: %d%% (%s)" % (pct, st.get("why") or ph)
    elif ph == "hold100":
        head = "Fans: 100%% for %d s more, then 50%%" % st.get("hold_left", 0)
    elif ph == "hold50":
        head = "Fans: 50%% for %d s more, then 20%%" % st.get("hold_left", 0)
    else:
        head = "Fans: 20% (idle)"
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
        out.append("level: the service isn't running (the fans are as the driver and BIOS leave them: automatic)")
    else:
        why = st.get("why")
        out.append("level: %d%%  phase: %s%s" % (st.get("pct", 0), phase_text(st), " - " + why if why else ""))
        out.append("controlling: " + st.get("controlling", "?"))
    meta = {r.get("label"): r for r in (st or {}).get("outputs", []) if isinstance(r, dict)}
    rows = live["outputs"]
    for r in rows:
        m = {1: "manual", 2: "auto"}.get(r["enable"], "chip control %s" % r["enable"])
        extra = meta.get(r["label"], {})
        tail = ""
        if extra.get("min_pct") is not None:
            tail += "  min %d%%" % extra["min_pct"]
        if extra.get("note"):
            tail += "  (%s)" % extra["note"]
        out.append("  %-18s %-16s pwm %s/255  %s%s" % (r["label"], m, "?" if r["pwm"] is None else r["pwm"],
                                                       "%d rpm" % r["rpm"] if r["rpm"] is not None else "no rpm reading",
                                                       tail))
    if not rows:
        out.append("  no fan outputs found (a motherboard chip needs: sudo modprobe nct6775)")
    if live["temps"]:
        out.append("temps: " + ", ".join("%s %.0f C" % (t["label"], t["c"]) for t in live["temps"]))
    else:
        out.append("temps: none read")
    if st is not None and st.get("hot"):
        out.append("too warm: " + "; ".join(st["hot"]))
    return "\n".join(out)


# ---- probes the service uses: lib/o1work.py (shared with the lights) ---------------------

Gpu = o1work.Gpu
probes = o1work.probes


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
    """ExecStopPost: after ANY exit (a clean stop, a crash, a kill, the watchdog) put
    anything still held back as it was. A fan left at 20% by a dead service would be
    the one outcome that is not acceptable."""
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

def sd_notify(message, env=None, factory=socket.socket):
    """Tell systemd (NOTIFY_SOCKET, a datagram socket; a leading @ is an abstract one). No-op
    outside systemd. Returns True when it was sent."""
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


def service_step(fan, notify=sd_notify, log=print, first=False):
    """One turn of the poll loop: a tick, then the watchdog's ping (also when the tick failed:
    a loop that runs is alive; one that hangs is what WatchdogSec=10 is for)."""
    try:
        fan.tick()
    except Exception as e:                                        # never stop; say what kind, not what it held
        log("tick failed: %s" % type(e).__name__)
    notify(("READY=1\n" if first else "") + "WATCHDOG=1\nSTATUS=%s" % phase_text({"phase": fan.phase,
                                                                                    "hold_left": fan.left}))


def run_service(log=print):
    fan = Fan(probes(), log=log)
    stop, poke = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGUSR1, lambda *_: poke.set())          # the sleep hook: a wake, look now
    first = True
    while not stop.is_set():
        service_step(fan, log=log, first=first)
        first = False
        poke.wait(POLL_S)
        poke.clear()
    fan.shutdown()
    return 0
