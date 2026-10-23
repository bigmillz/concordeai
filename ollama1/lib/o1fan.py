"""Fan levels for the server (6b385; the levels of 6b421, per the owner): the graphics
card's fan and every fan header the motherboard's chip lets the kit control (the
case fans, the CPU/radiator fans) follow what the server is doing, so it is cool
when it works and quiet, and easy on the bearings, when it does not:

  working     100%   the card over 50% busy, or the processor at 60 C or more (the level is a
                     setting, 6b495, or the fan calibration's quiet level, which steps up to its
                     heavy level when a temperature nears its limit, 6b505: see lib/o1work.py)
  tuning       N%    while the fan calibration (lib/o1fancal.py) holds a test level: the level
                     it asks for, refreshed every 2 s in /run/ollama1/fancal-hold.json; a file
                     older than 10 s is ignored, so a dead calibration can't keep the fans low
  hold100      100%  from the moment that ends, for 30 s (6b460, per the owner)
  ramp     100->20%  then a straight line down over 90 s, in 2% steps
  idle20       20%   from 120 s after the work ended (and from the start)
  deepen   20->10%   after 300 s of idle (6b434, per the owner; the lights go white to
                     blue at the same moment: they read this phase from the status file,
                     6b449), a straight line over 90 s, in 1% steps
  deep         10%   while deeply idle. Only outputs that can run that low go to 10%: an
                     output's own floor wins (the lowest level it was found to spin at, the
                     fan the stall check kept at 20%, a pump or fixed header kept at 100%,
                     one that reads no rpm, left to its own control, the cooler's pump rules).
                     Deep idle is left at once, back to 20%, when any temperature is within
                     10 C of its limit, the CPU is at 50 C or more or the card's junction at
                     60 C or more (and not entered again until all are 3 C lower); any
                     override, the 60 C CPU or the card's work, raises the fans at once.

Work at any time, the ramp included, goes back to 100% at once; when it ends
again the ramp starts again from 100%. A level is pwm = round(percent * 255 /
100): 20% is 51, 50% is 128. The service always holds the outputs while it
runs; it gives them back to their own control (the BIOS's automatic) whenever
it stops, for any reason.

What counts as work is lib/o1work.py's, shared with the lights (which follow the
card only):

  * the card's busy percent over 50% for 1.5 s in a row (two 2 s polls in a
    row), ending when it has been at or under 50% for 1.5 s in a row: one
    sample over 50% does not start a one-minute ramp;
  * the processor's temperature (k10temp Tctl/Tdie, the "CPU" reading below)
    at 60 C or more, ending under 55 C.

The processors' utilisation, a request in flight and running tools or setup.sh /
apt-get / unattended-upgrade are NOT work any more: only heat or the card's load
turns the fans up.

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
  * a temperature at its limit forces 100% regardless of load, until it is 10 C
    under it: CPU 80 C; graphics card junction 90, memory 95, edge 85; NVMe 70;
    DIMMs (jc42) 70; every input of the motherboard's chip (by its label): 70,
    CPUTIN/PECI/TSI 85, VRM/MOS 90, chipset/PCH 80, any label it doesn't know
    70. A sensor that reads 0 or less (-128 is an unplugged input), or 120 or
    more, is disconnected or stuck: ignored, and let go if it was hot;
  * after a wake amdgpu may have reset its fan to automatic: every tick checks
    the outputs it holds and writes them again at the current level.

The state file and the status file hold numbers and sensor names only; never
anything anyone asked.
"""
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import o1aio
import o1common
import o1cpu
import o1sleep
import o1work
from o1common import read_json_safe, write_json_atomic

POLL_S = 2
HOLD_S = o1work.FAN_HOLD_S       # when the work ends, the fans stay at 100% this long (6b460) ...
RAMP_S = o1work.FAN_RAMP_S       # ... then a straight ramp down to the idle level over this long
QUANT_PCT = 2                    # the ramp is written in whole steps of this many percent
FULL_PCT, LOW_PCT = 100, o1work.IDLE_PCT
work_level = o1work.work_level      # the setting, shared with the lights (6b495)
DEEP_PCT = o1work.FAN_DEEP_PCT   # the deep-idle level (an output's own floor wins)
STEP_PCT = 10                    # a stalled output is raised by this much
SETTLE_S = 6                     # a level is judged by rpm only after this long
PUMP_RATIO = 0.6                 # at 20% still this share of its 100% rpm: a pump or a fixed header
HYST_C = 10                      # a temperature override ends this far under its limit
STATUS_STALE_S = 15              # a status file older than this: the service isn't running
LIMIT_CPU_C, LIMIT_GPU_C, LIMIT_NVME_C = 80, 90, 70
LIMIT_GPU_EDGE_C, LIMIT_GPU_MEM_C = 85, 95
LIMIT_BOARD_C = 70               # a motherboard chip input: system, auxiliary and any label it does not know
LIMIT_CPUTIN_C = 85              # CPUTIN, PECI and TSI: the chip's own view of the CPU
LIMIT_VRM_C = 90                 # a label with VRM or MOS in it
LIMIT_CHIPSET_C = 80             # a label with CHIPSET or PCH in it
LIMIT_DIMM_C = 70                # jc42: the memory modules
LIMIT_GPU_NV_C = 85               # an NVIDIA card's one (chip) temperature, from nvidia-smi (6b505; the burn stops at 90)
TUNING_FRESH_S = 10               # the calibration's hold file is followed only while it is this fresh
TEMP_LOW_C, TEMP_HIGH_C = 0, 120  # at or under, or at or over, these a sensor is disconnected or stuck: ignored
CALL_JOIN_S = 6                  # the cooler's thread is given this long to finish a call at a stop
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


def tuning_path():
    return o1common.p("/run/ollama1/fancal-hold.json")


def read_tuning(path=None, now=None):
    """The level the fan calibration holds the fans at (6b505), or None: none, not fresh (TUNING_FRESH_S), or not a
    whole percent from 20 to 100. Only root can write /run/ollama1 (0755 root)."""
    d = read_json_safe(path or tuning_path(), None, max_bytes=4096)
    if not isinstance(d, dict):
        return None
    pct, at = d.get("pct"), d.get("at")
    if isinstance(pct, bool) or not isinstance(pct, int) or not LOW_PCT <= pct <= FULL_PCT:
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    if abs((time.time() if now is None else now) - at) > TUNING_FRESH_S:
        return None
    return pct


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

def plausible(c):
    """A temperature a working sensor can show. 0 or less, 120 or more, and the -128 an unplugged
    input reads, are a disconnected or stuck sensor: ignored, never a reason for 100%."""
    return TEMP_LOW_C < c < TEMP_HIGH_C


def board_limit(label):
    """The limit for a motherboard chip input, from its label."""
    up = (label or "").upper()
    if re.search(r"VRM|\bMOS", up):
        return LIMIT_VRM_C
    if "CHIPSET" in up or "PCH" in up:
        return LIMIT_CHIPSET_C
    if up.startswith(("CPU", "PECI")) or "TSI" in up:
        return LIMIT_CPUTIN_C
    return LIMIT_BOARD_C


def _chip_temps(hw):
    """[(number, label, celsius)] of a hwmon chip's temp*_input files, raw (unplausible ones too).
    A file that can't be read is left out: no reading is not a reading."""
    out = []
    for f in sorted(glob.glob(glob.escape(hw) + "/temp*_input")):
        m = re.fullmatch(r".*/temp(\d+)_input", f)
        c = o1cpu.number(o1cpu.read_text(f), -1000.0, 1000.0, scale=1000.0)
        if m and c is not None:
            label = (o1cpu.read_text("%s/temp%s_label" % (hw, m.group(1))) or "").strip() or "temp" + m.group(1)
            out.append((int(m.group(1)), label, c))
    return out


def _nvme_live(hw):
    """An NVMe drive's controller state is "live". A drive that dropped off (seen after a wake: "controller is down",
    then "dead") hangs every temperature read in an admin command the kernel only gives up on after a minute,
    and a read stuck in the kernel blocked the next suspend: "1 tasks refusing to freeze" (6b487)."""
    st = (o1cpu.read_text(hw + "/device/state") or "").strip()
    return st in ("", "live")          # "": no state file (an older kernel or a test tree) - read as before


def read_temps(sysroot=None):
    """[(key, label, celsius or None, limit)]: every sensor that can force the fans to 100%:
    the CPU, the graphics card's junction, memory and edge, each NVMe drive, the DIMMs (jc42) and every
    input of the motherboard's chip. celsius is None for a disconnected or stuck sensor (see
    plausible()). Labels the kit doesn't know are watched with the board limit, not ignored."""
    base = (sysroot or _sys()) + "/class/hwmon"
    out = []
    try:
        dirs = sorted(os.listdir(base))
    except OSError:
        return out
    for d in dirs:
        hw = base + "/" + d
        name = (o1cpu.read_text(hw + "/name") or "").strip()
        if not (name in CPU_CHIPS or name in ("amdgpu", "nvme", "jc42") or SUPER_IO_RX.match(name)):
            continue
        if name == "nvme" and not _nvme_live(hw):
            continue                   # a drive that dropped off is not a hot drive, and must not be asked (6b487)
        sensors = _chip_temps(hw)
        by_label = {}
        for _n, label, c in sensors:
            by_label.setdefault(label, c)

        def add(key, label, c, limit):
            out.append((key, label, c if c is not None and plausible(c) else None, limit))
        if name in CPU_CHIPS:
            vals = [by_label[k] for k in CPU_LABELS if k in by_label]
            if vals:
                good = [c for c in vals if plausible(c)]
                add("cpu:" + d, "CPU", max(good) if good else None, LIMIT_CPU_C)
        elif name == "amdgpu":
            for label, show, limit in (("junction", "GPU junction", LIMIT_GPU_C), ("mem", "GPU memory", LIMIT_GPU_MEM_C),
                                       ("edge", "GPU edge", LIMIT_GPU_EDGE_C)):
                if label in by_label:
                    add("gpu-%s:%s" % (label, d), show, by_label[label], limit)
        elif name == "nvme":
            if "Composite" in by_label:
                add("nvme:" + d, "NVMe", by_label["Composite"], LIMIT_NVME_C)
        elif name == "jc42":
            for n, _label, c in sensors:
                add("dimm%d:%s" % (n, d), "DIMM", c, LIMIT_DIMM_C)
        else:
            for n, label, c in sensors:
                add("board%d:%s" % (n, d), "%s %s" % (name, label) if label.startswith("temp") else label,
                    c, board_limit(label))
    seen = {}
    for i, row in enumerate(out):                            # two sensors with one name: told apart by number
        seen.setdefault(row[1], []).append(i)
    for label, idx in seen.items():
        if len(idx) > 1:
            for n, i in enumerate(idx, 1):
                k, _l, c, limit = out[i]
                out[i] = (k, "%s %d" % (label, n), c, limit)
    return out


def summarize_temps(rows):
    """From [{"label","c","limit"}]: the highest reading and the sensor closest to its limit."""
    if not rows:
        return None, None
    hottest = max(rows, key=lambda r: r["c"])
    closest = min(rows, key=lambda r: r["limit"] - r["c"])
    return ({"label": hottest["label"], "c": hottest["c"]},
            {"label": closest["label"], "c": closest["c"], "limit": closest["limit"],
             "margin": round(closest["limit"] - closest["c"], 1)})


# ---- the machine ------------------------------------------------------------------

class Fan:
    """One tick every POLL_S seconds. `probes` supplies gpu_busy (the card's busy percent, or None); the
    processor's temperature comes from the sensors (read_temps); `clock` is monotonic (the levels must not
    jump); `wall` stamps the status file."""

    def __init__(self, probes, clock=time.monotonic, wall=time.time, log=print, io=None, sysroot=None,
                 boot_id=read_boot_id, aio=None, aio_inline=False, work_pct=None, levels=None):
        self.p, self.clock, self.wall, self.log = probes, clock, wall, log
        self.work_pct = FULL_PCT if work_pct is None else work_pct    # a fixed level (6b495; the tests) ...
        self.heavy_pct, self.level_source = self.work_pct, "default" if work_pct is None else "setting"
        self.levels = levels           # ... or o1work.Levels(): the calibration's quiet and heavy levels (6b505)
        self.stepped, self.step_at, self.step_why = False, None, ""   # at the heavy level, since when, why
        self.cool_top = None           # the level when the work ended: the hold and the ramp start from it
        self.tuning = None             # the calibration's test level, while it holds one
        self.aio, self.aio_inline = aio, aio_inline      # the liquid cooler, if any (lib/o1aio.py)
        self.plock = threading.Lock()
        self.io = io or SysfsIO()
        self.sysroot = sysroot
        self.boot_id = boot_id
        self.orig = {}                 # key -> {"enable": int, "pwm": int | None}: as it was before the kit touched it
        self.learned = {}              # lkey -> {"rpm100", "min_pct", "always100", "pump_checked"}
        self.engaged = False           # True while the kit holds any output
        self.was_working = False
        self.cool_from = None          # when the work last ended: the ramp is counted from here
        self.hot = {}                  # temperature key -> label, while over its limit
        self.born = self.clock()       # the start, or the last wake: the idle clock counts from here at the earliest
        self.woke_at = 0               # the last wake's stamp (6b468): in the status, so the lights can tell old words from new
        self.calib_at = None           # the last time it was measuring at full speed
        self.deep_block = False        # a temperature keeps deep idle off (see deep_guard)
        self.deep_start = None         # when the fall to the deep level began (None: not in deep idle)
        self.deep_again = False        # deep idle was left for heat: the next fall starts from 20% when it is entered
        self.failed = set()
        self.level = {}                # key -> the percent it was last set to
        self.since = {}                # key -> when that level was set
        self.checked = {}              # key -> the level whose rpm was judged
        self.phase, self.pct, self.why, self.left = "idle20", LOW_PCT, "idle", 0
        self.exact, self.status_level = None, LOW_PCT   # the unrounded level (6b466) and the one the status shows
        self.seen = None               # what it last said it controls
        self.kinds = self.said_kinds = ()   # which triggers hold it working (the log says when that changes)
        self.gpu = o1work.GpuTrigger()
        self.cpu = o1work.CpuTrigger()
        self._load_state()
        if self.aio:
            self.aio.attach(self.learned, self._persist, self.log)

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
                if v.get("no_deep") is True:
                    learned[k]["no_deep"] = True
        self.learned = learned
        orig = st.get("orig")
        if isinstance(orig, dict) and orig and st.get("boot") and st.get("boot") == self.boot_id():
            good = {}
            for k, v in orig.items():
                if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("enable"), int):
                    good[k] = {"enable": v["enable"], "pwm": v["pwm"] if isinstance(v.get("pwm"), int) else None}
            if good:
                self.orig, self.engaged = good, True
                self.cool_from = self.clock()          # found still held: the ramp from 100%, in case it was working
                self.log("found fans still held by an earlier run: %d output%s, put back when it stops"
                         % (len(good), "" if len(good) == 1 else "s"))
        self._persist()                                # another boot's originals go: the hardware is as it was

    def _persist(self):
        with self.plock:                                   # the cooler's thread saves what it learns too
            if not self.orig and not self.learned:
                try:
                    os.unlink(state_path())
                except OSError:
                    pass
                return
            os.makedirs(os.path.dirname(state_path()), exist_ok=True)
            write_json_atomic(state_path(), {"v": 2, "boot": self.boot_id(), "orig": self.orig,
                                             "learned": self.learned}, mode=0o600)

    # -- what counts as working -----------------------------------------------------
    def _gpu_busy(self):
        try:
            return self.p["gpu_busy"]()
        except Exception:
            return None

    def working(self, now=None, temps=None):
        """(working?, the reasons): the card over 50% (debounced) or the processor at 60 C (lib/o1work.py)."""
        now = self.clock() if now is None else now
        temps = read_temps(self.sysroot) if temps is None else temps
        why, self.kinds = [], ()
        if self.gpu.update(now, self._gpu_busy()):
            why.append("the card is %d%% busy" % round(self.gpu.pct or 0))
            self.kinds += ("card",)
        if self.cpu.update(c for key, _l, c, _lim in temps if key.startswith("cpu:")):
            why.append("CPU %.0f C" % self.cpu.c)
            self.kinds += ("cpu",)
        return bool(why), why

    def overheated(self, temps=None):
        """(hot?, the reasons): a sensor over its limit stays hot until HYST_C under it."""
        for key, label, c, limit in (read_temps(self.sysroot) if temps is None else temps):
            if c is None:
                self.hot.pop(key, None)              # disconnected or stuck: never a reason, and not a stuck one
            elif c >= limit:
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
        return o1work.fan_level(L, pct, LOW_PCT)

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
        if pct < LOW_PCT and (rpm == 0 or rpm < min_rpm_of(o, self.io)):
            L["no_deep"] = True                  # it stalls below the idle level: held at 20% in deep idle
            self.log("%s: %d rpm at %d%%, stalled: deep idle keeps it at %d%%" % (o.label, rpm, pct, LOW_PCT))
            self._persist()
            return
        if rpm == 0 or rpm < min_rpm_of(o, self.io):
            L["min_pct"] = min(FULL_PCT, pct + STEP_PCT)
            if L["min_pct"] >= FULL_PCT:
                L["always100"] = True
            self.log("%s: %d rpm at %d%%, stalled: its lowest level is now %d%%" % (o.label, rpm, pct, L["min_pct"]))
            self._persist()

    # -- deep idle --------------------------------------------------------------------
    def deep_guard(self, temps):
        """True while a temperature keeps deep idle off: any sensor within DEEP_LIMIT_MARGIN_C of its limit, the CPU
        at DEEP_CPU_C, the card's junction at DEEP_GPU_C, or no reading at all (no sensors, or the CPU's or the
        junction's reads nothing plausible: it can't be shown to be cool). Once on, it stays on until every figure
        is DEEP_REENTER_C lower (a hysteresis)."""
        m = o1work.DEEP_REENTER_C if self.deep_block else 0
        block = not temps
        for key, _label, c, limit in temps:
            cpu, junction = key.startswith("cpu:"), key.startswith(("gpu-junction:", "gpu-nvidia:"))
            if c is None:
                block = block or cpu or junction
                continue
            if c >= limit - o1work.DEEP_LIMIT_MARGIN_C - m or (cpu and c >= o1work.DEEP_CPU_C - m) or \
                    (junction and c >= o1work.DEEP_GPU_C - m):
                block = True
        self.deep_block = block
        return block

    def idle_base(self):
        """When the idle began: HOLD_S + RAMP_S (120 s) after the work ended, or the start or wake, or the end of the measuring."""
        base = self.born
        if self.cool_from is not None:
            base = max(base, self.cool_from + HOLD_S + RAMP_S)
        if self.calib_at is not None:
            base = max(base, self.calib_at)
        return base

    def deep_level(self, now):
        """(phase, pct, why, seconds left) for an idle fan whose deep idle may begin: 20% until IDLE_DEEP_S of idle, then
        a straight line to DEEP_PCT over BLUE_S, then DEEP_PCT; the 20% idle level while a temperature says no."""
        base = self.idle_base()
        if self.deep_block or now - base < o1work.IDLE_DEEP_S:
            if self.deep_start is not None:                      # left deep idle for heat: the next fall starts over
                self.deep_start, self.deep_again = None, True
            return "idle20", LOW_PCT, "idle", 0
        if self.deep_start is None:
            self.deep_start = now if self.deep_again else base + o1work.IDLE_DEEP_S
        d = min(1.0, max(0.0, (now - self.deep_start) / o1work.BLUE_S))
        if d >= 1.0:
            return "deep", DEEP_PCT, "deep idle", 0
        self.exact = LOW_PCT - (LOW_PCT - DEEP_PCT) * d              # the lights follow this exact figure (6b466)
        pct = int(round(self.exact))
        return "deepen", pct, "idle, going quieter", int(o1work.BLUE_S * (1.0 - d) + 0.999)

    # -- one tick --------------------------------------------------------------------
    def _nv_temps(self):
        """An NVIDIA card's chip temperature as a sensor row (6b505), from the probe the busy reading fills: [] when
        there is none (another card, no probe, no fresh reading). It counts for the override and deep idle as an AMD
        card's junction does."""
        try:
            c = o1work.number(self.p["gpu_temp"]()) if "gpu_temp" in self.p else None
        except Exception:
            c = None
        return [("gpu-nvidia:0", "GPU", c, LIMIT_GPU_NV_C)] if c is not None and plausible(c) else []

    def heavy_guard(self, temps, now):
        """True while the fans work at the heavy level (6b505): from the moment any temperature is within
        STEP_UP_MARGIN_C of its limit, until every one is STEP_DOWN_MARGIN_C under its limit and STEP_MIN_S have
        passed. Only when the heavy level is above the quiet one (a calibration found two)."""
        if self.heavy_pct <= self.work_pct:
            self.stepped, self.step_at = False, None
            return False
        near = [(label, c, limit) for _k, label, c, limit in temps
                if c is not None and c >= limit - o1work.STEP_UP_MARGIN_C]
        if near and not self.stepped:
            label, c, limit = max(near, key=lambda r: r[1] - r[2])
            self.stepped, self.step_at = True, now
            self.step_why = "%s %.0f C (limit %d)" % (label, c, limit)
            self.log("heavy level %d%%: %s" % (self.heavy_pct, self.step_why))
        elif self.stepped and not near and now - self.step_at >= o1work.STEP_MIN_S and all(
                c is None or c < limit - o1work.STEP_DOWN_MARGIN_C for _k, _l, c, limit in temps):
            self.stepped, self.step_at = False, now
            self.log("quiet level %d%%: every temperature %d C under its limit" % (self.work_pct,
                                                                                   o1work.STEP_DOWN_MARGIN_C))
        return self.stepped

    def tick(self):
        now = self.clock()
        if self.levels is not None:
            try:
                L = self.levels()
                self.work_pct, self.heavy_pct, self.level_source = L["quiet"], L["heavy"], L["source"]
            except Exception:
                pass                                             # keep the last levels
        temps = read_temps(self.sysroot)
        working, wwhy = self.working(now, temps)
        temps = temps + self._nv_temps()
        hot, hwhy = self.overheated(temps)
        self.deep_guard(temps)
        self.tuning = read_tuning(now=self.wall())
        heavy = self.heavy_guard(temps, now)
        level = self.heavy_pct if heavy else self.work_pct
        outs = find_outputs(self.sysroot)
        usable = [o for o in outs if o.key not in self.failed]
        text = controlling_text(usable)
        if text != self.seen:
            self.log("controlling " + text)
            self.seen = text
        if working:
            self.was_working, self.cool_from, self.cool_top = True, None, None
        elif self.was_working:
            self.was_working, self.cool_from = False, now          # the work just ended: the ramp starts now
            self.cool_top = max(level, self.tuning or 0)           # ... from the level it worked at
        top = self.cool_top if self.cool_top is not None else self.work_pct
        age = None if self.cool_from is None else now - self.cool_from
        left = 0
        self.exact = None                                        # set where the level is not a whole step
        if hot:
            phase, pct, why = "hot", FULL_PCT, "too warm: " + "; ".join(hwhy)
        elif self.tuning is not None:
            phase, pct, why = "tuning", self.tuning, "fan calibration: testing %d%%" % self.tuning
        elif working:
            phase, pct, why = "working", level, "; ".join(wwhy) + ("; heavy level: " + self.step_why if heavy else "")
        elif any(o.lkey not in self.learned for o in usable) or (self.aio and self.aio.needs_calibration()):
            phase, pct, why = "calibrating", FULL_PCT, "measuring each fan at full speed (once)"
            self.calib_at = now
        elif age is not None and age < HOLD_S:
            phase, pct, why, left = "hold100", top, "the work ended", int(HOLD_S - age + 0.999)
        elif age is not None and age < HOLD_S + RAMP_S:
            pct = ramp_pct(age - HOLD_S, top)
            self.exact = ramp_exact(age - HOLD_S, top)
            phase, why, left = "ramp", "the work ended", int(HOLD_S + RAMP_S - age + 0.999)
        else:
            phase, pct, why, left = self.deep_level(now)
        if phase in ("hot", "tuning", "working", "calibrating", "hold100", "ramp"):
            if phase == "hot":                                   # anything that raises the fans drops deep idle
                if self.deep_start is not None:
                    self.deep_again = True                       # (heat: the next fall starts over from 20%)
            else:
                self.deep_again = False                          # (work: a whole new idle)
            self.deep_start = None
        self.apply(outs, pct, now)
        self.sample(usable, now)
        if self.aio:
            self.aio.set_phase(phase, pct)
            if self.aio_inline:
                self.aio.step(now)
        if phase != self.phase or (phase in ("hot", "tuning") and why != self.why) or (
                phase == "working" and self.kinds != self.said_kinds):
            self.log("%s: %s" % (phase, why))
            self.said_kinds = self.kinds
        self.phase, self.pct, self.why, self.left = phase, pct, why, left
        self.status_level = round(self.exact, 2) if self.exact is not None else pct   # (6b466) the lights' exact figure
        self.write_status(usable, temps)
        return phase, why

    def shutdown(self):
        """A stop: everything held goes back as it was, and the cooler is left on its safe curve."""
        if self.engaged:
            self.release()
        if self.aio and self.aio.active():
            self.aio.safe_exit()

    def wake(self):
        """After a suspend: the cooler may have lost what it was told; send it all again. And (6b468, per the owner:
        "make sure they go back to white when coming out of sleep") a wake is the end of any work and the start of a
        fresh idle: no 100% hold, 20% (idle20), the idle clock again from now, so the lights follow the fans back to
        white and then to blue on the normal curve. Work or heat found at the next tick still takes precedence.
        `woke` in the status is the sleep hook's stamp (sleep.json last_wake), so a status written before the wake
        (a tick that ran first, or one left on /run across the sleep) is known as old."""
        self.born, self.deep_start, self.deep_again = self.clock(), None, False      # the idle clock starts again
        self.was_working, self.cool_from, self.calib_at = False, None, None          # no hold, no ramp, no measuring clock
        st = o1sleep.last().get("last_wake")
        self.woke_at = st if isinstance(st, int) and not isinstance(st, bool) else int(self.wall())
        if self.aio:
            self.aio.reset()

    # -- status ------------------------------------------------------------------------
    def notes(self, o):
        L = self.learned.get(o.lkey)
        if L is None:
            return None, ""
        if L["always100"]:
            return L["min_pct"], "kept at 100%: a pump or a fixed header"
        if not L["rpm100"]:
            return None, "no rpm at 100%: left to its own control except while working"
        if L.get("no_deep"):
            return L["min_pct"], "stalls under %d%%: kept at %d%% in deep idle" % (LOW_PCT, LOW_PCT)
        return L["min_pct"], ""

    def unconnected(self, o, rpm):
        """True for a header with nothing on it: its rpm read nothing when measured at 100% (what is learned) and
        reads nothing now. One that ever reads a fan again shows again. The card's fan is never hidden."""
        L = self.learned.get(o.lkey)
        return o.kind == "case" and L is not None and not L["rpm100"] and not rpm

    def write_status(self, outs, temps=None):
        live = snapshot(outs, self.io, self.sysroot, temps)
        aio = self.aio.snapshot() if self.aio else None
        pump_rpm = aio["pump_rpm"] if aio and aio.get("found") and aio.get("state") == "controlling" else None
        rows, gone, pump = [], [], None
        for u in (aio or {}).get("unconnected") or []:           # a cooler fan port with nothing on it (6b435)
            gone.append({"label": u["label"], "chip": "cooler", "pwm": u.get("pct")})
        for row, o in zip(live["outputs"], outs):
            row["min_pct"], row["note"] = self.notes(o)
            if self.unconnected(o, row["rpm"]):
                gone.append({"label": o.label, "chip": o.chip, "pwm": row["pwm"]})
                continue
            rows.append((row, o))
        if pump_rpm is not None:                               # the cooler's own steady reading, not the header's tach
            fixed = [(o.n, row, o) for row, o in rows if o.kind == "case" and (self.learned.get(o.lkey) or {}).get("always100")]
            if fixed:
                _n, row, o = min(fixed, key=lambda t: t[0])
                pump = o.label
                row["tach_raw"], row["rpm"], row["label"] = row["rpm"], pump_rpm, "Pump"
                row["note"] = "the cooler's pump (its own rpm reading); the header's tach is in tach_raw"
        shown = [o for _row, o in rows]
        st = {"at": int(self.wall()), "woke": self.woke_at, "phase": self.phase, "pct": self.pct, "level": self.status_level, "why": self.why,
              "hold_left": self.left,
              "controlling": controlling_text(shown), "outputs": [row for row, _o in rows], "temps": live["temps"],
              "unconnected": gone, "pump": pump,
              "aio": aio,
              "hottest": live["hottest"], "closest": live["closest"], "hot": sorted(self.hot.values()),
              # the working levels (6b505): the lights are red at "work"; "levels" says where they came from
              "work": self.work_pct, "heavy": self.heavy_pct, "levels": self.level_source, "stepped": self.stepped}
        st["line"] = status_line(st)
        try:
            os.makedirs(os.path.dirname(status_path()), exist_ok=True)
            write_json_atomic(status_path(), st, mode=0o644)
        except OSError:
            pass


# ---- status text --------------------------------------------------------------------

def snapshot(outs, io=None, sysroot=None, temps=None):
    """Live readings: each output's setting and rpm, and the temperatures (`temps`: read_temps() rows already read,
    with an NVIDIA card's; else read now)."""
    io = io or SysfsIO()
    rows = []
    for o in outs:
        rows.append({"label": o.label, "chip": o.chip, "enable": _int(io.read(o.enable)),
                     "pwm": _int(io.read(o.pwm)), "rpm": rpm_of(o, io)})
    temps = [{"label": label, "c": round(c, 1), "limit": limit}
             for _k, label, c, limit in (read_temps(sysroot) if temps is None else temps) if c is not None]
    hottest, closest = summarize_temps(temps)
    return {"outputs": rows, "temps": temps, "hottest": hottest, "closest": closest}


def _rpm_summary(rows):
    gpu = [r["rpm"] for r in rows if r["label"] == "GPU fan" and r["rpm"] is not None]
    case = [r["rpm"] for r in rows if r["label"] != "GPU fan" and r["rpm"]]
    parts = []
    if gpu:
        parts.append("GPU fan %d rpm" % gpu[0])
    if case:
        parts.append("case fans up to %d rpm" % max(case))
    return parts


def ramp_exact(t, top=FULL_PCT):
    """The level `t` seconds into the ramp before it is rounded to steps (what the lights follow, 6b466)."""
    return top - (top - LOW_PCT) * min(max(t, 0), RAMP_S) / float(RAMP_S)


def ramp_pct(t, top=FULL_PCT):
    """The level `t` seconds into the ramp: a straight line from the work level (`top`) to LOW_PCT over RAMP_S, in
    whole steps of QUANT_PCT, never under LOW_PCT."""
    p = top - (top - LOW_PCT) * min(max(t, 0), RAMP_S) / RAMP_S
    return max(LOW_PCT, min(top, int(round(p / QUANT_PCT)) * QUANT_PCT))


def present(rows, st):
    """The live rows of a snapshot as the status shows them: headers with nothing on them are left out (unless they
    read a fan now), and the header the cooler's pump is on is "Pump", with the cooler's own rpm."""
    st = st or {}
    gone = {u.get("label") for u in (st.get("unconnected") or []) if isinstance(u, dict)}
    a = st.get("aio") or {}
    out = []
    for r in rows:
        r = dict(r)
        if r["label"] in gone and not r.get("rpm"):
            continue
        if st.get("pump") and r["label"] == st["pump"] and a.get("pump_rpm") is not None:
            r["tach_raw"], r["rpm"], r["label"] = r["rpm"], a["pump_rpm"], "Pump"
        out.append(r)
    return out


def phase_text(st):
    """The phase in words: working / hold100 / ramp NN% / idle20 / deepen NN% / deep (and the two that force 100%)."""
    ph = st.get("phase")
    if ph in ("ramp", "deepen"):
        return "%s %d%%" % (ph, st.get("pct", 0))
    return ph or "?"


def temp_summary(st):
    """"hottest GPU junction 61 C, closest to its limit: NVMe 58 of 70 C", from a status dict."""
    hot, near = st.get("hottest"), st.get("closest")
    if not hot or not near:
        return ""
    return "hottest %s %.0f C, closest to its limit: %s %.0f of %d C" % (hot["label"], hot["c"], near["label"],
                                                                         near["c"], near["limit"])


def status_line(st):
    """One line for the admin panel's CPU card and the top of `status`."""
    ph = st.get("phase")
    pct = st.get("pct", 0)
    if ph in ("working", "hot", "calibrating", "tuning"):
        head = "Fans: %d%% (%s)" % (pct, st.get("why") or ph)
    elif ph == "hold100":
        head = "Fans: 100%% for %d s more, then down to 20%%" % st.get("hold_left", 0)
    elif ph == "ramp":
        head = "Fans: ramping down, %d%% (20%% in %d s)" % (pct, st.get("hold_left", 0))
    elif ph == "deepen":
        head = "Fans: going quieter, %d%% (%d%% in %d s)" % (pct, DEEP_PCT, st.get("hold_left", 0))
    elif ph == "deep":
        head = "Fans: %d%% (deep idle)" % pct
    else:
        head = "Fans: 20% (idle)"
    rpm = _rpm_summary(st.get("outputs") or [])
    t = temp_summary(st)
    c = o1aio.aio_text(st.get("aio"))
    return head + ("  -  " + ", ".join(rpm) if rpm else "") + ("  -  " + t if t else "") + ("  -  " + c if c else "")


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
    return line[:400] if isinstance(line, str) and line else None


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
    rows = present(live["outputs"], st)
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
        hot, near = live["hottest"], live["closest"]
        out.append("temps: highest %s %.0f C; closest to its limit: %s %.0f C of %d (%.0f %s)"
                   % (hot["label"], hot["c"], near["label"], near["c"], near["limit"], abs(near["margin"]),
                      "under" if near["margin"] >= 0 else "OVER"))
        out.append("sensors: " + ", ".join("%s %.0f/%d" % (t["label"], t["c"], t["limit"]) for t in live["temps"]))
    else:
        out.append("temps: none read")
    a = (st or {}).get("aio")
    if a and a.get("found"):
        out.append("cooler: %s - %s" % (a.get("name"), a.get("state")))
        if a.get("coolant_c") is not None:
            out.append("  coolant %.1f C%s   pump %s%s" % (
                a["coolant_c"], " (>= %d C: pump extreme, fans 100%%)" % o1aio.COOLANT_HOT_C if a.get("coolant_hot") else "",
                a.get("pump_mode") or "?", " %d rpm" % a["pump_rpm"] if a.get("pump_rpm") is not None else ""))
        for f in a.get("fans", []):
            out.append("  cooler fan %d  " % f["n"] + "  ".join(x for x in (
                "%d%%" % f["pct"] if f.get("pct") is not None else "?%",
                "%d rpm" % f["rpm"] if f.get("rpm") else "",
                "min %d%%" % f["min_pct"] if f.get("min_pct") is not None else "") if x))
    elif a and a.get("state") and a["state"] != "not looked for yet":
        out.append("cooler: %s" % a["state"])
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
    cooler = o1aio.safe_exit_if_controlled(log=log)
    fan = Fan({}, io=io, sysroot=sysroot, boot_id=boot_id, log=log)
    if not fan.engaged:
        return cooler
    return fan.release() and cooler


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


def apt_install_liquidctl():
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    try:
        r = subprocess.run(["apt-get", "install", "-y", "liquidctl"], env=env, capture_output=True, text=True,
                           timeout=900)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def aio_setup(sysroot=None, which=None, installer=None, log=print):
    """Install liquidctl, only when a Corsair Hydro cooler is on USB and it isn't installed. Never fatal.
    Returns True when a cooler is there and liquidctl is (now) installed."""
    which = which or shutil.which
    if not o1aio.usb_present(sysroot):
        log("no Corsair Hydro liquid cooler on USB: nothing to install for one")
        return False
    if which("liquidctl"):
        log("a Corsair Hydro liquid cooler is on USB; liquidctl is installed: it is controlled too")
        return True
    if (installer or apt_install_liquidctl)():
        log("a Corsair Hydro liquid cooler is on USB: installed liquidctl (apt); it is controlled too")
        return True
    log("a Corsair Hydro liquid cooler is on USB but liquidctl could not be installed (sudo apt install liquidctl); "
        "the case fans are controlled, the cooler is not")
    return False


def setup(choice, systemctl=run_systemctl, sysroot=None, log=print, modules=None, aio=None):
    """setup.sh's step. on: the old unit out, the chip's module loaded (and kept at boot
    only when a chip showed up), the service enabled and started. off: stopped (which puts
    the fans back), disabled, the boot-time module entry removed."""
    conf = o1common.p(MODULES_CONF)
    if choice == "on":
        release_old_unit(systemctl, sysroot=sysroot, log=log)
        chip = (modules or load_modules)(sysroot=sysroot, log=log)
        (aio or aio_setup)(sysroot=sysroot, log=log)
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


def aio_loop(aio, stop, log=print):
    """The cooler's own thread: liquidctl calls are slow (a USB transaction, a subprocess), and the poll loop must
    keep pinging the watchdog whatever they do."""
    while not stop.is_set():
        try:
            aio.step(time.monotonic())
        except Exception as e:                                    # never stop; say what kind, not what it held
            log("cooler step failed: %s" % type(e).__name__)
        stop.wait(1)


def run_service(log=print):
    aio = o1aio.Aio(log=log)
    fan = Fan(probes(), log=log, aio=aio, levels=o1work.Levels())
    stop, poke, woke = threading.Event(), threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGUSR1, lambda *_: (woke.set(), poke.set()))     # the sleep hook: a wake, look now
    worker = threading.Thread(target=aio_loop, args=(aio, stop, log), daemon=True)
    worker.start()
    first = True
    while not stop.is_set():
        if woke.is_set():
            woke.clear()
            fan.wake()
        service_step(fan, log=log, first=first)
        first = False
        poke.wait(POLL_S)
        poke.clear()
    worker.join(timeout=CALL_JOIN_S)                               # a call in flight ends (its own timeout)
    fan.shutdown()
    return 0
