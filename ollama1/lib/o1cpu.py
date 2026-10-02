"""CPU figures for the admin panel (and for the console dashboard, later):
model, cores, governor and driver, frequency (per core and average), the
package temperature, utilisation (overall and per core), load, package power
where a readable source exists, and a plain throttle note.

Read-only and stdlib only. Everything comes from /sys and /proc, through one
bounded reader that never follows a symlink in the last component and never
blocks on a FIFO or a device. Every source that is missing or garbled turns
into None (a quiet blank in the panel), never an exception. Numbers are
parsed as numbers, rejected when they are NaN or outside a sane range, and
the one piece of text from the machine (the CPU model) is cleaned before it
leaves this module. The hwmon is found by its NAME (k10temp, coretemp,
zenpower), never by its number: the numbers change from boot to boot.

    probe = CpuProbe()
    snap = probe.snapshot()      # a plain dict, JSON-safe; see snapshot()
"""
import math
import os
import re
import stat
import time

import o1stats

MAX_CORES = 1024                    # cores read and reported; more are counted but not listed
READ_LIMIT = 4096                   # one sysfs value
CPUINFO_LIMIT = 4 << 20             # /proc/cpuinfo on a very large machine
TEMP_RANGE = (-30.0, 150.0)         # degrees C that can be real
MHZ_RANGE = (1.0, 15000.0)          # a CPU core in MHz
WATTS_MAX = 2000.0
HWMON_NAMES = ("k10temp", "coretemp", "zenpower")
TEMP_WARN_C = 80.0                  # the panel's colours; the stability test aborts at 95
TEMP_HOT_C = 90.0
SLOW_UTIL_PCT = 80.0                # busy this much ...
SLOW_FRACTION = 0.5                 # ... and under this share of the rated top speed: say so
THROTTLE_MEMORY_S = 300             # a thermal-throttle count that rose this recently
MODEL_LIMIT = 80
_BAD_TEXT = frozenset("<>&\"'`\\")
_WORD = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")


# ---- reading and parsing ---------------------------------------------------------------

def read_text(path, limit=READ_LIMIT):
    """The first `limit` bytes of a regular file as text, or None. A symlink
    as the last component, a FIFO, a device or a directory is refused (the
    folders on the way may be symlinks: /sys/class/hwmon/* always are)."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as f:
            fd = None
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def number(text, lo, hi, scale=1.0):
    """text -> float / scale, or None if it isn't a finite number in [lo, hi]."""
    try:
        v = float((text or "").strip().split()[0]) / scale
    except (ValueError, IndexError, OverflowError):
        return None
    if math.isnan(v) or math.isinf(v) or v < lo or v > hi:
        return None
    return v


def clean_text(s, limit=MODEL_LIMIT):
    """Text from the machine, made harmless: printable characters only, none
    of the ones HTML and shells care about, one line, bounded."""
    s = "".join(ch for ch in str(s or "") if ch.isprintable() and ch not in _BAD_TEXT)
    return " ".join(s.split())[:limit] or None


def word(text):
    """A short identifier (a governor, a driver) or None."""
    t = (text or "").strip()
    return t if _WORD.match(t) else None


def parse_cpuinfo(text):
    """{"model", "threads", "cores", "mhz": [per processor]} from /proc/cpuinfo.
    Any of them may be None / empty (virtual machines and ARM leave gaps)."""
    model = None
    procs = 0
    mhz = []
    pairs = set()
    phys = core = None
    for line in (text or "").splitlines() + [""]:
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not line.strip():                         # a blank line ends one processor's block
            if phys is not None and core is not None:
                pairs.add((phys, core))
            phys = core = None
            continue
        if key == "processor":
            procs += 1
        elif key in ("model name", "Model Name") and model is None:
            model = clean_text(val)
        elif key == "cpu MHz":
            v = number(val, *MHZ_RANGE)
            mhz.append(v)
        elif key == "physical id":
            phys = val
        elif key == "core id":
            core = val
    return {"model": model, "threads": procs or None, "cores": len(pairs) or None, "mhz": mhz[:MAX_CORES]}


def parse_stat(text):
    """{"cpu": (busy, total), "cpu0": ..., ...} in jiffies from /proc/stat, and
    the running-process count. Bad lines are skipped."""
    out = {}
    running = None
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "procs_running":
            try:
                running = int(parts[1])
            except (ValueError, IndexError):
                pass
            continue
        if not parts[0].startswith("cpu") or len(out) > MAX_CORES:      # "cpu" plus MAX_CORES cores
            continue
        try:
            vals = [int(x) for x in parts[1:9]]
        except ValueError:
            continue
        if len(vals) < 4 or min(vals) < 0:
            continue
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        out[parts[0]] = (total - idle, total)
    return out, running


def utilisation(prev, cur):
    """Busy % between two (busy, total) pairs: None when there is no earlier
    reading, nothing elapsed (a division by zero otherwise) or a counter went
    backwards; always within 0..100."""
    if not prev or not cur:
        return None
    db, dt = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0 or db < 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * db / dt)), 1)


def _cpu_number(name):
    m = re.match(r"^cpu(\d+)$", name)
    return int(m.group(1)) if m else None


# ---- the probe -------------------------------------------------------------------------

class CpuProbe:
    """Holds what needs remembering between looks: the last /proc/stat (for
    utilisation), the highest temperature seen, the last energy reading and
    the thermal-throttle count. `sys_root` / `proc_root` default to
    o1stats.SYS / PROC at the time of each look, so tests can point them at
    fixture trees."""

    def __init__(self, sys_root=None, proc_root=None, clock=time.monotonic):
        self._sys, self._proc = sys_root, proc_root
        self.clock = clock
        self.prev_stat = None
        self.peak_c = None
        self.prev_energy = None
        self.throttle_total = None
        self.throttle_rose = None
        self.static = (0, None)
        self.snapshot()                              # primes utilisation and the throttle count

    @property
    def sys(self):
        return self._sys or o1stats.SYS

    @property
    def proc(self):
        return self._proc or o1stats.PROC

    # -- the machine --------------------------------------------------------------------
    def cpu_dirs(self):
        """[(n, path)] of the cpuN folders, in numeric order, capped."""
        base = self.sys + "/devices/system/cpu"
        try:
            names = os.listdir(base)
        except OSError:
            return [], 0
        found = sorted((n, nm) for nm in names for n in [_cpu_number(nm)] if n is not None)
        return [(n, base + "/" + nm) for n, nm in found[:MAX_CORES]], len(found)

    def identity(self, now):
        """Model, cores and threads: rarely change, so read every 5 minutes."""
        t, val = self.static
        if val is not None and now - t < 300:
            return val
        info = parse_cpuinfo(read_text(self.proc + "/cpuinfo", CPUINFO_LIMIT))
        val = {"model": info["model"], "cores": info["cores"], "threads": info["threads"]}
        self.static = (now, val)
        return val

    def hwmon(self):
        """(chip name, directory) of the CPU's temperature chip, found by
        name; None if there isn't one (a virtual machine, most ARM boards)."""
        base = self.sys + "/class/hwmon"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        found = {}
        for d in names:
            n = (read_text(base + "/" + d + "/name") or "").strip()
            if n in HWMON_NAMES and n not in found:
                found[n] = base + "/" + d
        for n in HWMON_NAMES:
            if n in found:
                return n, found[n]
        return None

    def temperature(self):
        """{"c", "label", "chip", "sensors": {label: c}, "peak_c"} or None."""
        hw = self.hwmon()
        if not hw:
            return None
        chip, d = hw
        sensors = {}
        first = None
        try:
            files = sorted(os.listdir(d))
        except OSError:
            return None
        for f in files:
            m = re.match(r"^temp(\d+)_input$", f)
            if not m or len(sensors) >= 16:
                continue
            v = number(read_text(d + "/" + f), *TEMP_RANGE, scale=1000.0)
            if v is None:
                continue
            label = clean_text(read_text(d + "/temp%s_label" % m.group(1)), 24) or ("temp" + m.group(1))
            sensors.setdefault(label, round(v, 1))
            if first is None:
                first = label
        if not sensors:
            return None
        # the die temperature if the chip gives it; Tctl can carry an offset
        # on some Ryzen models. Else Intel's package, else the first sensor
        label = next((k for k in ("Tdie", "Tctl", "Package id 0") if k in sensors), first)
        c = sensors[label]
        if self.peak_c is None or c > self.peak_c:
            self.peak_c = c
        return {"c": c, "label": label, "chip": chip, "sensors": sensors, "peak_c": self.peak_c}

    def frequency(self, dirs):
        """Per-core MHz (None for a core that gives none), the driver, the
        governor and the min / max / rated speeds, from cpufreq; /proc/cpuinfo's
        'cpu MHz' only if cpufreq gave nothing at all."""
        cores = [number(read_text(p + "/cpufreq/scaling_cur_freq"), *MHZ_RANGE, scale=1000.0) for _, p in dirs]
        if not any(c is not None for c in cores):
            fallback = parse_cpuinfo(read_text(self.proc + "/cpuinfo", CPUINFO_LIMIT))["mhz"]
            cores = fallback if any(c is not None for c in fallback) else []
        first = next((p for _, p in dirs if os.path.isdir(p + "/cpufreq")), None)
        out = {"driver": None, "governor": None, "min_mhz": None, "max_mhz": None, "rated_mhz": None}
        if first:
            cf = first + "/cpufreq/"
            out["driver"] = word(read_text(cf + "scaling_driver"))
            out["governor"] = word(read_text(cf + "scaling_governor"))
            out["min_mhz"] = number(read_text(cf + "scaling_min_freq"), *MHZ_RANGE, scale=1000.0)
            out["max_mhz"] = number(read_text(cf + "scaling_max_freq"), *MHZ_RANGE, scale=1000.0)
            out["rated_mhz"] = number(read_text(cf + "cpuinfo_max_freq"), *MHZ_RANGE, scale=1000.0)
        live = [c for c in cores if c is not None]
        out["avg_mhz"] = round(sum(live) / len(live)) if live else None
        out["hi_mhz"] = round(max(live)) if live else None
        out["lo_mhz"] = round(min(live)) if live else None
        out["cores"] = [round(c) if c is not None else None for c in cores]
        for k in ("min_mhz", "max_mhz", "rated_mhz"):
            out[k] = round(out[k]) if out[k] is not None else None
        return out

    def load(self):
        """{"avg": [1, 5, 15 minutes], "running", "tasks"} from /proc/loadavg."""
        parts = (read_text(self.proc + "/loadavg") or "").split()
        avg = [number(x, 0.0, 1e6) for x in parts[:3]]
        out = {"avg": [round(x, 2) for x in avg] if len(parts) >= 3 and None not in avg else None,
               "running": None, "tasks": None}
        if len(parts) >= 4 and "/" in parts[3]:
            r, _, t = parts[3].partition("/")
            if r.isdigit() and t.isdigit():
                out["running"], out["tasks"] = int(r), int(t)
        return out

    def usage(self):
        """(overall %, [per-core %]) since the last look."""
        cur, _running = parse_stat(read_text(self.proc + "/stat", 1 << 20))
        prev, self.prev_stat = self.prev_stat, cur
        if not cur or prev is None:
            return None, []
        cores = sorted((n, k) for k in cur for n in [_cpu_number(k)] if n is not None)
        return utilisation(prev.get("cpu"), cur.get("cpu")), \
            [utilisation(prev.get(k), cur.get(k)) for _, k in cores]

    def power(self, now):
        """Package watts from an energy counter we are allowed to read: RAPL
        (root only on current kernels) or the amd_energy chip. None when
        there is none, or on the first look."""
        paths = [self.sys + "/class/powercap/intel-rapl:0"]
        energy = None
        rng = None
        for base in paths:
            e = number(read_text(base + "/energy_uj"), 0, 2 ** 64)
            if e is not None:
                energy, rng = e, number(read_text(base + "/max_energy_range_uj"), 0, 2 ** 64)
                break
        if energy is None:
            try:
                names = sorted(os.listdir(self.sys + "/class/hwmon"))
            except OSError:
                names = []
            for d in names:
                base = self.sys + "/class/hwmon/" + d
                if (read_text(base + "/name") or "").strip() == "amd_energy":
                    try:
                        files = sorted(os.listdir(base))
                    except OSError:
                        files = []
                    for f in files:
                        if re.match(r"^energy\d+_label$", f) and (read_text(base + "/" + f) or "").startswith("Esocket"):
                            energy = number(read_text(base + "/" + f.replace("_label", "_input")), 0, 2 ** 64)
                            if energy is not None:
                                break
                    break
        prev, self.prev_energy = self.prev_energy, (energy, now) if energy is not None else None
        if energy is None or prev is None or now <= prev[1]:
            return None
        d = energy - prev[0]
        if d < 0:
            if not rng:
                return None
            d += rng + 1
        w = d / 1e6 / (now - prev[1])
        return round(w, 1) if 0 <= w <= WATTS_MAX else None

    def throttle_count(self, dirs):
        total = None
        for _, p in dirs[:64]:
            for f in ("core_throttle_count", "package_throttle_count"):
                v = number(read_text(p + "/thermal_throttle/" + f), 0, 2 ** 62)
                if v is not None:
                    total = (total or 0) + int(v)
        return total

    # -- the whole picture --------------------------------------------------------------
    def snapshot(self):
        """Everything above in one JSON-safe dict. Every key is always
        present; an unknown value is None (or an empty list)."""
        now = self.clock()
        dirs, listed = self.cpu_dirs()
        ident = self.identity(now)
        freq = self.frequency(dirs)
        util, per_core = self.usage()
        temp = self.temperature()
        load = self.load()
        watts = self.power(now)
        count = self.throttle_count(dirs)
        if count is not None and self.throttle_total is not None and count > self.throttle_total:
            self.throttle_rose = now
        if count is not None:
            self.throttle_total = count
        note = None
        if self.throttle_rose is not None and now - self.throttle_rose < THROTTLE_MEMORY_S:
            note = "the CPU slowed itself down because of heat in the last few minutes"
        elif (util is not None and util >= SLOW_UTIL_PCT and freq["avg_mhz"] and freq["rated_mhz"]
              and freq["avg_mhz"] < SLOW_FRACTION * freq["rated_mhz"]):
            note = "busy but running at well under its top speed (heat, a power limit or the power plan)"
        threads = ident["threads"] or listed or (len(per_core) or None)
        return {"model": ident["model"], "cores": ident["cores"], "threads": threads,
                "driver": freq["driver"], "governor": freq["governor"],
                "freq": {k: freq[k] for k in ("min_mhz", "max_mhz", "rated_mhz", "avg_mhz", "hi_mhz", "lo_mhz", "cores")},
                "temp": temp, "util_pct": util, "util_cores": per_core,
                "load": load["avg"], "running": load["running"], "tasks": load["tasks"],
                "power_w": watts, "throttle": note,
                "thresholds": {"warn_c": TEMP_WARN_C, "hot_c": TEMP_HOT_C}}
