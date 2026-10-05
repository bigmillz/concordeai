"""Graphics-card tuning (6b361, reshaped in 6b420): the card's highest power limit on an AMD Navi 21 card
(RX 6800, 6800 XT, 6900 XT, 6950 XT), and, only when asked, a memory-clock raise and a core-clock raise.

What is set (amdgpu sysfs). The card is found by its PCI vendor and device
ids under /sys/bus/pci/devices, never by its cardN number, which can change
from one boot to the next:

  power    (always) hwmon power1_cap = power1_cap_max: the highest limit the
           driver itself reports for this card (microwatts). Never above it.
           Measured harmless on a 6900 XT: as fast as stock.
  memory   (opt-in, `on --memory N`, N whole MHz 0..75, default 0 = off)
           pp_od_clk_voltage "m 1 <clock>" then "c": the memory clock's top
           state raised by N in the driver's units, never past the OD_RANGE
           MCLK maximum. On one 6900 XT the +75 bump made answers 2.4x slower
           (no kernel error), which is why it is off by default.
  core     (opt-in, `on --core N`, N whole MHz 0..150, default 0 = off,
           experimental) "s 1 <clock>" then "c": the shader clock's top state
           raised by N above the card's current stock top, never past the
           OD_RANGE SCLK maximum, read back and verified. It helps long prompts
           more than answers, and may crash the card.

Voltages are never touched. The clocks need the kernel's overdrive switch
(amdgpu.ppfeaturemask with OD_BIT, a GRUB drop-in from setup.sh, so a
reboot); on Navi 21 the power limit above the default usually needs it too.

Safety: each part is checked on its own, in order (power, memory, core): 60 s
of answers on the card while it watches the kernel log for amdgpu errors and
the temperatures. Any amdgpu error or a temperature at or past its limit (105
C junction, 100 C while the core is raised, 100 C memory) puts EVERYTHING back
to stock at once and records why; it stays at stock, across reboots, until the
admin runs `ollama1-gpu-tune on` again. Slow answers are judged more carefully
(6b372): a first slow reading starts a repeat, the baseline and the tuned
state measured again, alternating twice; only a slowdown that repeats (each
alternation and the medians) reverts. What it reverts depends on the part: a
power raise that is slower than stock puts everything back; a memory or core
raise that is slower (the core also on prompt reading, measured with ~2000
token prompts) goes back ALONE and the power limit stays ("memory +25 MHz was
slower, back to stock; power limit kept"). Before measuring it waits up to
60 s for the card to be quiet (busy under 10%, no other model loading). If
anything made a reading unreliable the check changes nothing and runs again
next boot, and an experimental clock is never left applied unverified: only
what passed (and the harmless power limit) stays on. Every boot also reads
the kernel log of the boot the tuning last ran in, and goes back to stock if
that boot logged an amdgpu error.

State: version 2 (6b420) keeps what was asked (`wanted`, `memory`, `core`),
what passed (`checked_parts`) and each clock's own revert. A file from before
(no `version`) has its `reverted` record dropped when wanted is on, so the new
rules judge the power limit afresh (`migrated` keeps the old reason).

The state file holds numbers, times, a model name and a reason line; never
anything anyone asked.
"""
import glob
import json
import os
import re
import subprocess
import time

import o1common
import o1ollama

VENDOR_AMD = "0x1002"
# Navi 21 (SIENNA_CICHLID): the device ids amdgpu binds to it.
NAVI21 = frozenset(("0x73a0", "0x73a1", "0x73a2", "0x73a3", "0x73a5", "0x73a8", "0x73a9", "0x73ab", "0x73ac",
                    "0x73ad", "0x73ae", "0x73af", "0x73bf"))
OD_BIT = 0x4000                 # PP_OVERDRIVE_MASK in amdgpu.ppfeaturemask
MEMORY_MAX_MHZ = 75             # the most `--memory N` may ask for (the driver's units, x2 for GDDR6's effective rate)
CORE_MAX_MHZ = 150              # the most `--core N` may ask for, above the card's current stock top clock
CORE_JUNCTION_MAX_C = 100       # while the core clock is raised the check's limit is 100 C junction, not 105
PROMPT_RUNS = 2                 # long prompts read per prompt-reading measurement
PROMPT_WORDS = 1800             # about 2000 tokens in, 8 out
CLOCK_PARTS = {"memory": {"max": MEMORY_MAX_MHZ}, "core": {"max": CORE_MAX_MHZ}}
STATE_VERSION = 2               # 2 (6b420): power limit by default, the memory clock opt-in and judged apart
JUNCTION_MAX_C = 105            # the stability test's limit too
MEM_MAX_C = 100
CHECK_SECONDS = 60
STOCK_SECONDS = 20
SLOWER_LIMIT = 0.95             # tuned answers under 95% of stock's speed: back to stock
# A slow reading alone never reverts (6b372): stock and tuned are measured again, alternating,
# and the slowdown must repeat; a reading taken while something else was going on is no reading.
CONFIRM_ROUNDS = 2              # stock/tuned alternations after a slow first reading
QUIET_BUSY_PCT = 10             # before measuring, the card must be this quiet (or nothing is measured or raised)
QUIET_POLLS = 30                # looked at every 2 s: up to 60 s of waiting for it
BUSY_OTHER_PCT = 35             # the card already this busy before the load starts: someone else is using it
GRUB_DROPIN = "/etc/default/grub.d/97-amdgpu-overdrive.cfg"
OLLAMA = "http://127.0.0.1:11434"
# what the kernel logs when the card hangs, resets or faults
ERROR_RX = re.compile(r"(amdgpu|\[drm).*?(ring \S+ timeout|GPU reset|GPU hang|VM fault|page fault|PROTECTION_FAULT)",
                      re.I)
# what the kernel logs when a drive misbehaves: reads and writes then crawl, and so does everything on the box
NVME_ERROR_RX = re.compile(r"((nvme\d|nvme nvme).*?(timeout|I/O error|controller is down|not ready|Disabling device|"
                           r"reset|Removing after probe failure|abort|Unmapped Host Error)|"
                           r"(blk_update_request|Buffer I/O error).*?dev nvme)", re.I)
LEVEL_FILE = "power_dpm_force_performance_level"


def _sys():
    return os.environ.get("OLLAMA1_SYS", "/sys")


def state_path():
    return o1common.p("/var/lib/ollama1/gpu-tune.json")


def _int(v):
    try:
        return int(str(v).strip(), 0)
    except (TypeError, ValueError):
        return None


class SysfsIO:
    """Reads and writes sysfs files. A write is one write() call, as sysfs wants."""

    def read(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def write(self, path, value):
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(fd, value.encode("ascii"))
        finally:
            os.close(fd)


# ---- reading the card ------------------------------------------------------------

def parse_od(text):
    """pp_od_clk_voltage as Navi 21 prints it ->
    {"mclk": {0: lo, 1: hi}, "range": {"SCLK": (lo, hi), "MCLK": (lo, hi)}, "odd": bool}.
    "odd" means an OD_MCLK line this doesn't know (an older card's "0: 300MHz 800mV"
    shape): the memory clock is then left alone."""
    out = {"mclk": {}, "sclk": {}, "range": {}, "odd": False, "sclk_odd": False}
    section = None
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"[A-Z_]+:", s):
            section = s[:-1]
            continue
        if section == "OD_SCLK":
            m = re.fullmatch(r"(\d+):\s*(\d+)\s*mhz", s, re.I)
            if m:
                out["sclk"][int(m.group(1))] = int(m.group(2))
            else:
                out["sclk_odd"] = True
        elif section == "OD_MCLK":
            m = re.fullmatch(r"(\d+):\s*(\d+)\s*mhz", s, re.I)
            if m:
                out["mclk"][int(m.group(1))] = int(m.group(2))
            else:
                out["odd"] = True
        elif section == "OD_RANGE":
            m = re.fullmatch(r"(SCLK|MCLK):\s*(\d+)\s*mhz\s+(\d+)\s*mhz", s, re.I)
            if m:
                out["range"][m.group(1).upper()] = (int(m.group(2)), int(m.group(3)))
    return out


def od_core_usable(od):
    """True when the core clock can be set: OD_SCLK has exactly states 0 and 1 in the plain shape, and OD_RANGE
    gives a sane SCLK range."""
    if not od or od.get("sclk_odd") or set(od.get("sclk") or {}) != {0, 1}:
        return False
    rng = od["range"].get("SCLK")
    return bool(rng) and 0 < rng[0] <= rng[1]


def od_usable(od):
    """True when the memory clock can be set: OD_MCLK has exactly states 0 and 1
    in the plain shape, and OD_RANGE gives a sane MCLK range."""
    if not od or od["odd"] or set(od["mclk"]) != {0, 1}:
        return False
    rng = od["range"].get("MCLK")
    return bool(rng) and 0 < rng[0] <= rng[1]


def parse_mhz(text, flag, maxv):
    """`--memory N` / `--core N` -> whole MHz 0..maxv, or ValueError (never clamped silently at the command line)."""
    try:
        n = int(str(text).strip(), 10)
    except (TypeError, ValueError):
        raise ValueError("%s takes a whole number of MHz, 0 to %d" % (flag, maxv))
    if not 0 <= n <= maxv:
        raise ValueError("%s takes 0 to %d MHz (0 = leave that clock at stock)" % (flag, maxv))
    return n


def parse_memory(text):
    return parse_mhz(text, "--memory", MEMORY_MAX_MHZ)


def parse_core(text):
    return parse_mhz(text, "--core", CORE_MAX_MHZ)


def clamp_mhz(n, maxv):
    """Whatever a state file held, as whole MHz 0..maxv."""
    return max(0, min(maxv, n)) if isinstance(n, int) and not isinstance(n, bool) else 0


def mclk_target(stock_max, range_max, bump, maxv=MEMORY_MAX_MHZ):
    """The clock to set (memory or core): stock + bump (0..maxv), never past the card's range.
    None when bump is 0 or that is no higher than stock."""
    bump = clamp_mhz(bump, maxv)
    if not stock_max or not range_max or bump <= 0:
        return None
    t = min(stock_max + bump, range_max)
    return t if t > stock_max else None


def power_target(pw):
    """power1_cap_max: the most the driver says this card may draw. None when it
    allows nothing above the default (overdrive off), or the numbers are missing."""
    mx, dflt = pw.get("max"), pw.get("default")
    if not mx or not dflt or mx <= dflt:
        return None
    if pw.get("min") and mx < pw["min"]:
        return None
    return mx


def feature_mask(current):
    """amdgpu.ppfeaturemask to boot with: the running one (the driver's default,
    or what is already set) with only the overdrive bit added."""
    v = _int(current)
    if v is None or v < 0:
        return None
    return (v | OD_BIT) & 0xFFFFFFFF


def grub_dropin(mask):
    return ("# ollama1 (setup.sh, 6b361): the kernel's overdrive switch for the graphics card's\n"
            "# memory clock (ollama1-gpu-tune). Only the overdrive bit (0x4000) is added to the\n"
            "# feature mask the driver was running with. Remove with: sudo setup.sh --no-gpu-tune\n"
            'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT amdgpu.ppfeaturemask=0x%08x"\n' % mask)


def running_mask():
    return _int((SysfsIO().read(_sys() + "/module/amdgpu/parameters/ppfeaturemask") or "").strip())


class Card:
    def __init__(self, dev, io):
        self.dev = dev
        self.io = io
        self.pci = os.path.basename(os.path.realpath(dev))
        self.device = (io.read(dev + "/device") or "").strip().lower()
        hw = [h for h in sorted(glob.glob(dev + "/hwmon/hwmon*")) if os.path.exists(h + "/power1_cap")]
        self.hwmon = hw[0] if hw else None

    def od_text(self):
        return self.io.read(self.dev + "/pp_od_clk_voltage")

    def od(self):
        t = self.od_text()
        return parse_od(t) if t is not None else None

    def power(self):
        out = {}
        if self.hwmon:
            for k, f in (("cap", "power1_cap"), ("default", "power1_cap_default"), ("max", "power1_cap_max"),
                         ("min", "power1_cap_min")):
                out[k] = _int(self.io.read(self.hwmon + "/" + f))
        return out

    def temps(self):
        """{"edge"|"junction"|"mem": whole degrees C} from the card's hwmon."""
        out = {}
        if not self.hwmon:
            return out
        for lab in glob.glob(self.hwmon + "/temp*_label"):
            name = (self.io.read(lab) or "").strip().lower()
            v = _int(self.io.read(lab[:-len("_label")] + "_input"))
            if name in ("edge", "junction", "mem") and v is not None:
                out[name] = v // 1000
        return out

    def level(self):
        return (self.io.read(self.dev + "/" + LEVEL_FILE) or "").strip()


def find_card(io=None):
    """(Card, None) for the first Navi 21 card by PCI address, else (None, why)."""
    io = io or SysfsIO()
    root = _sys()
    amd = False
    for dev in sorted(glob.glob(root + "/bus/pci/devices/*")):
        if not (io.read(dev + "/class") or "").strip().startswith("0x03"):
            continue                                 # not a display controller
        if (io.read(dev + "/vendor") or "").strip().lower() != VENDOR_AMD:
            continue
        amd = True
        if (io.read(dev + "/device") or "").strip().lower() in NAVI21:
            card = Card(dev, io)
            if card.hwmon or card.od_text() is not None:
                return card, None
    if amd:
        return None, "the AMD graphics card here isn't a Navi 21 (RX 6800/6900 series), so nothing is tuned"
    return None, "no AMD Navi 21 graphics card (RX 6800/6900 series) here, so nothing is tuned"


# ---- the kernel log, the boot, Ollama -----------------------------------------------

def journal_lines(args):
    """Kernel-log lines from journalctl, or None when it can't say."""
    try:
        r = subprocess.run([os.environ.get("OLLAMA1_JOURNALCTL", "journalctl"), "-k", "-q", "--no-pager", "-o", "cat"]
                           + list(args), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


def amdgpu_errors(lines):
    return [l.strip()[:200] for l in (lines or []) if ERROR_RX.search(l)]


def nvme_errors(lines):
    return [l.strip()[:200] for l in (lines or []) if NVME_ERROR_RX.search(l)]


def boot_id():
    try:
        with open(os.environ.get("OLLAMA1_PROC", "/proc") + "/sys/kernel/random/boot_id") as f:
            return f.read().strip().replace("-", "")
    except OSError:
        return ""


class Ollama:
    """The local Ollama, for the self-check's load (a fixed counting prompt)."""

    def __init__(self, base=OLLAMA):
        self.base = base

    def _get(self, path):
        try:
            st, body = o1ollama.call(self.base, "GET", path, timeout=10)
        except OSError:
            return None
        return body if st == 200 and isinstance(body, dict) else None

    def up(self):
        return self._get("/api/version") is not None

    def models(self):
        b = self._get("/api/tags")
        return [m for m in (b or {}).get("models", []) if isinstance(m, dict)]

    def loaded(self):
        b = self._get("/api/ps")
        return [m for m in (b or {}).get("models", []) if isinstance(m, dict)]

    def generate(self, model):
        """(tokens written, seconds writing them) for one answer, or None."""
        body = {"model": model, "prompt": "Count from 1 to 200 in words, one per line.", "stream": False,
                "keep_alive": "5m", "options": {"num_predict": 192, "temperature": 0, "seed": 1}}
        try:
            st, b = o1ollama.call(self.base, "POST", "/api/generate", body, timeout=300)
        except OSError:
            return None
        if st != 200 or not isinstance(b, dict):
            return None
        n, ns = b.get("eval_count"), b.get("eval_duration")
        if not isinstance(n, int) or not isinstance(ns, int) or n <= 0 or ns <= 0:
            return None
        return n, ns / 1e9


def _generate_prompt(self, model):
    """(prompt tokens read, seconds reading them) for one long prompt (~2000 tokens in, 8 out), or None. Each
    prompt starts differently, so Ollama's cache of an earlier one can't make the reading look free."""
    body = {"model": model, "prompt": "Run %d. " % time.time_ns() + "The quick brown fox jumps over the lazy dog. " * (PROMPT_WORDS // 9),
            "stream": False, "keep_alive": "5m", "options": {"num_predict": 8, "temperature": 0, "seed": 1}}
    try:
        st, b = o1ollama.call(self.base, "POST", "/api/generate", body, timeout=300)
    except OSError:
        return None
    if st != 200 or not isinstance(b, dict):
        return None
    n, ns = b.get("prompt_eval_count"), b.get("prompt_eval_duration")
    if not isinstance(n, int) or not isinstance(ns, int) or n <= 0 or ns <= 0:
        return None
    return n, ns / 1e9


Ollama.generate_prompt = _generate_prompt


def _embedding(name):
    return "embed" in (name or "").lower()


def pick_model(ollama, vram_bytes, prefer=None):
    """A model for the load: the one stock was measured with, else one already
    on the card, else the smallest installed that fits the card's memory with room."""
    names = {m.get("name") for m in ollama.models()}
    if prefer and prefer in names:
        return prefer
    for m in ollama.loaded():
        if not _embedding(m.get("name")) and m.get("name") in names and m.get("size_vram") \
                and m.get("size_vram") >= (m.get("size") or 0):
            return m.get("name")
    fit = [(m.get("size") or 0, m.get("name")) for m in ollama.models()
           if m.get("name") and not _embedding(m.get("name")) and m.get("size")
           and (not vram_bytes or m.get("size") <= 0.75 * vram_bytes)]
    return min(fit)[1] if fit else None


# ---- the tuner ---------------------------------------------------------------------

def read_state(path=None):
    """The saved state (any reader: the panel or dashboard may show it), {} if none."""
    s = o1common.read_json(path or state_path(), {})
    return s if isinstance(s, dict) else {}


def migrate_state(st):
    """The saved state in the format of this version. A file from before (no `version`) keeps what was asked
    (`wanted`) and the stock figures, but its `reverted` record is dropped: the old rule bumped the memory clock
    too, and on a card where that is what measured slower the whole tuning was put back. wanted=on now means the
    power limit alone, judged afresh. The old reason is kept as `migrated` for status."""
    if not isinstance(st, dict) or not st or st.get("version") == STATE_VERSION:
        return st
    st = dict(st)
    old = st.pop("reverted", None)
    st.pop("reverted_at", None)
    if old and st.get("wanted") == "on":
        st["migrated"] = {"from_version": st.get("version") or 1, "old_revert": str(old)[:200]}
    elif old:
        st["reverted"] = old                    # not asked for: an admin's off, or a state this doesn't understand: kept
    st.pop("checked", None)
    st.pop("check", None)
    st["memory"] = 0
    st["core"] = 0
    st["checked_parts"] = {}
    a = dict(st.get("applied") or {})
    st["applied"] = a
    st["version"] = STATE_VERSION
    return st


class Tuner:
    def __init__(self, io=None, ollama=None, journal=journal_lines, clock=time.time, mono=time.monotonic,
                 boot=boot_id, out=print, check_seconds=None, stock_seconds=None, pause=time.sleep):
        self.io = io or SysfsIO()
        self.pause = pause
        self.junction_max = JUNCTION_MAX_C
        self.noise = []                  # what made the last load's speed unreliable (see _load)
        self.load_failed = False
        self.ollama = ollama or Ollama()
        self.journal = journal
        self.clock = clock
        self.mono = mono
        self.boot = boot
        self.out = out
        self.check_seconds = CHECK_SECONDS if check_seconds is None else check_seconds
        self.stock_seconds = STOCK_SECONDS if stock_seconds is None else stock_seconds
        self.st = migrate_state(read_state())

    def save(self):
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        o1common.write_json_atomic(state_path(), self.st, mode=0o644)

    def say(self, line):
        self.out("ollama1-gpu-tune: " + line)

    # -- writing the card --
    def _od_write(self, card, cmds):
        """Write pp_od_clk_voltage commands. If the driver refuses them in "auto",
        try once in "manual"; the level always ends as "auto" when this changed it."""
        path = card.dev + "/pp_od_clk_voltage"
        changed = False
        try:
            try:
                for c in cmds:
                    self.io.write(path, c)
            except OSError:
                if card.level() == "manual":
                    raise
                self.io.write(card.dev + "/" + LEVEL_FILE, "manual")
                changed = True
                for c in cmds:
                    self.io.write(path, c)
        finally:
            if changed:
                try:
                    self.io.write(card.dev + "/" + LEVEL_FILE, "auto")
                except OSError:
                    pass

    def to_stock(self, card):
        """Back to the card's own values: the overdrive table's defaults, the default power limit."""
        notes = []
        if card.od_text() is not None:
            try:
                self._od_write(card, ["r", "c"])
            except OSError as e:
                notes.append("the memory clock couldn't be reset (%s)" % e.strerror)
        pw = card.power()
        if pw.get("default") and pw.get("cap") != pw["default"]:
            try:
                self.io.write(card.hwmon + "/power1_cap", str(pw["default"]))
            except OSError as e:
                notes.append("the power limit couldn't be reset (%s)" % e.strerror)
        return notes

    # ---- what is asked: power (always), memory and core (opt-in, in whole MHz) ----
    def mhz(self, part):
        """The bump asked for and still allowed (0: that clock stays at stock)."""
        if self.st.get(part + "_reverted"):
            return 0
        return clamp_mhz(self.st.get(part), CLOCK_PARTS[part]["max"])

    def memory_mhz(self):
        return self.mhz("memory")

    def passed(self, part):
        """The check of that part has passed for what is asked now."""
        done = self.st.get("checked_parts") or {}
        if part == "power":
            p = (self.st.get("applied") or {}).get("power_uw")
            return not p or done.get("power") == p
        return done.get(part) == self.mhz(part)

    def wanted_parts(self):
        """In check order: power, then each clock that is asked for and not reverted."""
        return ["power"] + [p for p in ("memory", "core") if self.mhz(p) > 0]

    def ready_parts(self):
        """What is applied when nothing is being checked: the power limit (harmless, measured), and each asked-for
        clock only once its own check has passed. An experimental clock is never left applied unverified (6b420:
        a deferred check once left the memory bump live on a card where it halves the speed)."""
        out = []
        for part in self.wanted_parts():
            if part != "power" and not self.passed(part):
                break
            out.append(part)
        return out

    def pending_part(self):
        for part in self.wanted_parts():
            if not self.passed(part):
                return part
        return None

    def apply_card(self, card, parts=None):
        """Set the power limit (the card's maximum) and the clocks asked for, among `parts` (default: the ready
        ones), each clamped to what the card reports. Returns {"power": uW set or None, "mclk": clock set or
        None, "sclk": clock set or None, "notes": [...]}."""
        parts = set(self.ready_parts() if parts is None else parts)
        res = {"power": None, "mclk": None, "sclk": None, "notes": []}
        st = self.st
        pw = card.power()
        if "power" in parts:
            # power: the most the driver allows, never above it
            want = power_target(pw)
            if want is None:
                if pw.get("max") and pw.get("default"):
                    res["notes"].append("the driver allows no more than the default power limit (%d W) yet; with "
                                        "overdrive on (a reboot after setup) it allows more" % (pw["default"] // 10 ** 6))
                else:
                    res["notes"].append("the card reports no power limits")
            else:
                if pw.get("cap") != want:
                    try:
                        self.io.write(card.hwmon + "/power1_cap", str(want))
                    except OSError as e:
                        res["notes"].append("the driver refused the power limit (%s)" % e.strerror)
                        want = None
                if want is not None:
                    res["power"] = want
        # the clocks: left alone unless asked for (6b420: +75 on one 6900 XT made it 2.4x slower)
        ask = {"mclk": self.mhz("memory") if "memory" in parts else 0, "sclk": self.mhz("core") if "core" in parts else 0}
        od = card.od()
        usable = {}
        if od is not None:
            usable = {"mclk": od_usable(od), "sclk": od_core_usable(od)}
        names = {"mclk": "memory", "sclk": "core"}
        for k in ("mclk", "sclk"):
            if ask[k] > 0:
                if od is None:
                    res["notes"].append("the %s clock needs the kernel's overdrive switch: reboot after setup" % names[k])
                elif not usable[k]:
                    res["notes"].append("this card's overdrive table isn't the shape this knows; the %s clock is left alone" % names[k])
                    ask[k] = 0
        keys = [k for k in ("mclk", "sclk") if usable.get(k)]
        if keys:
            applied = st.get("applied") or {}
            stock = st.setdefault("stock", {})
            cur = {k: od[k][1] for k in keys}
            need_reset = False
            for k in keys:
                if stock.get(k) is None:
                    if ask[k] > 0:
                        need_reset = True            # the driver's own value is stock: read it after a reset
                    else:
                        stock[k] = cur[k]            # nothing asked: what it reads is stock
                elif cur[k] != stock[k] and (ask[k] <= 0 or cur[k] != applied.get(k)):
                    need_reset = True                # set by something else, or a bump no longer asked for
            if need_reset:
                try:
                    self._od_write(card, ["r", "c"])
                except OSError as e:
                    if any(ask[k] <= 0 and cur[k] != stock.get(k) for k in keys):
                        res["notes"].append("the clocks couldn't be reset (%s)" % e.strerror)
                od = card.od() or od
                for k in keys:
                    cur[k] = stock[k] = od[k][1]
            cmds, targets = [], {}
            for k in keys:
                if ask[k] <= 0:
                    continue
                lo, hi = od["range"]["MCLK" if k == "mclk" else "SCLK"]
                t = mclk_target(stock[k], hi, ask[k], CLOCK_PARTS[names[k]]["max"])
                if t is None:
                    res["notes"].append("the card's %s clock range allows nothing above stock" % names[k])
                elif t < lo:
                    res["notes"].append("the %s clock target is outside the card's range; left alone" % names[k])
                else:
                    targets[k] = t
                    if cur[k] != t:
                        cmds.append("%s 1 %d" % ("m" if k == "mclk" else "s", t))
            if cmds:
                try:
                    self._od_write(card, cmds + ["c"])
                except OSError as e:
                    res["notes"].append("the driver refused the clock (%s)" % e.strerror)
                    targets = {}
            if targets:
                now = card.od() or {}
                for k, t in list(targets.items()):
                    if now.get(k, {}).get(1) != t:
                        res["notes"].append("the %s clock reads %s after setting %d" % (names[k], now.get(k, {}).get(1), t))
                        targets.pop(k)
            for k, t in targets.items():
                res[k] = t
        st.setdefault("stock", {})["power_uw"] = pw.get("default")
        st["applied"] = {"power_uw": res["power"], "mclk": res["mclk"], "sclk": res["sclk"], "at": int(self.clock()),
                         "boot": self.boot(), "card": card.pci, "device": card.device}
        return res

    def parts_phrase(self, res):
        """'power 332 W, memory stock, core +50 MHz' (a reverted part says so)."""
        stock = self.st.get("stock") or {}
        bits = ["power %d W" % (res["power"] // 10 ** 6) if res.get("power") else "power stock"]
        for part, key in (("memory", "mclk"), ("core", "sclk")):
            if res.get(key):
                bits.append("%s +%d MHz" % (part, res[key] - stock.get(key, res[key])))
            elif self.st.get(part + "_reverted"):
                bits.append("%s stock (reverted: it was slower)" % part)
            else:
                bits.append("%s stock" % part)
        return ", ".join(bits)

    def report_apply(self, res):
        """Says exactly what was set; a clock that was not asked for is not mentioned."""
        if res["power"]:
            self.say("power limit %d W (maximum)" % (res["power"] // 10 ** 6))
        for part, key in (("memory", "mclk"), ("core", "sclk")):
            if res[key]:
                self.say(self.clock_phrase(res, part, key))
        for n in res["notes"]:
            self.say(n)

    def clock_phrase(self, res, part, key):
        """'core +150 MHz (top 2529 -> 2679)'."""
        stock = self.st["stock"][key]
        return "%s +%d MHz (top %d -> %d%s)" % (part, res[key] - stock, stock, res[key],
                                                 ", %d MHz effective" % (res[key] * 2) if key == "mclk" else "")

    def part_revert(self, card, part, reason):
        """One clock goes back to stock, for good until `on --<part> N` asks again; the power limit and the other
        clock stay (a failed memory or core check never undoes a passing power check)."""
        mhz = clamp_mhz(self.st.get(part), CLOCK_PARTS[part]["max"])
        self.st[part + "_reverted"] = reason
        self.st[part + "_reverted_at"] = int(self.clock())
        cp = self.st.get("checked_parts") or {}
        cp.pop(part, None)
        self.st["checked_parts"] = cp
        notes = []
        try:
            res = self.apply_card(card, parts=self.ready_parts())      # the reset takes the clock off, the rest is set again
            notes += res["notes"]
        except OSError as e:
            notes.append("the clock couldn't be reset (%s)" % e.strerror)
        self.say("%s +%d MHz was slower, back to stock; power limit kept (%s)" % (part, mhz, reason))
        for n in notes:
            self.say(n)

    def revert(self, card, reason):
        """Everything goes back to stock at once (a real error, heat, a power raise that measured slower)."""
        self.st["reverted"] = reason
        self.st["reverted_at"] = int(self.clock())
        notes = self.to_stock(card) if card else []
        self.st["applied"] = {"power_uw": None, "mclk": None, "sclk": None, "at": int(self.clock()), "boot": self.boot()}
        self.st["checked_parts"] = {}
        self.say("back to stock: " + reason)
        for n in notes:
            self.say(n)
        self.say("it stays at stock (also after a reboot) until: sudo ollama1-gpu-tune on")

    def active(self):
        return self.st.get("wanted") == "on" and not self.st.get("reverted")

    def applied_values(self):
        a = self.st.get("applied") or {}
        return [a.get("power_uw"), a.get("mclk"), a.get("sclk")]

    # -- the self-check --
    def _load(self, card, model, seconds, since):
        """Answers for `seconds`, watching temperatures and the kernel log.
        -> (tokens, secs, answers, why-it-failed or None, max temps)."""
        tokens = secs = 0.0
        answers = 0
        hot = {}
        # Besides what ends the load, it notes (self.noise) what makes its speed no reading of the
        # card: the card already busy for someone else, another model loaded, a drive logging
        # errors, an answer that failed partway. A slow figure from such a load is never grounds
        # to revert (self_check).
        self.noise = []
        self.load_failed = False
        others = self._others(model) if model else set()
        busy = self._busy(card) if model else None
        if busy is not None and busy >= BUSY_OTHER_PCT:
            self.noise.append("the card was already %d%% busy before the load started" % busy)
        end = self.mono() + seconds
        while self.mono() < end:
            r = self.ollama.generate(model) if model else None
            if r:
                tokens += r[0]
                secs += r[1]
                answers += 1
            if model:
                new = self._others(model) - others
                if new:
                    self.noise.append("another model was loaded: %s" % ", ".join(sorted(new)))
                    others |= new
            t = card.temps()
            for k, v in t.items():
                hot[k] = max(hot.get(k, v), v)
            if t.get("junction") is not None and t["junction"] >= self.junction_max:
                return tokens, secs, answers, "the junction reached %d C" % t["junction"], hot
            if t.get("mem") is not None and t["mem"] >= MEM_MAX_C:
                return tokens, secs, answers, "the memory reached %d C" % t["mem"], hot
            if since is not None:
                lines = self.journal(["--since", "@%d" % since])
                errs = amdgpu_errors(lines)
                if errs:
                    return tokens, secs, answers, "the kernel logged: " + errs[0], hot
                for e in nvme_errors(lines)[:1]:
                    if not any(e in n for n in self.noise):
                        self.noise.append("the kernel logged a drive error: " + e)
            if not r and model:
                self.load_failed = True
                self.noise.append("an answer failed partway through")
                return tokens, secs, answers, None, hot      # Ollama didn't answer: stop, judged below
        return tokens, secs, answers, None, hot

    def _others(self, model):
        """The models loaded now other than the one measured."""
        try:
            return {m.get("name") for m in self.ollama.loaded() if m.get("name")} - {model}
        except (OSError, AttributeError):
            return set()

    def _busy(self, card):
        """The card's busy percent just before a load, the lowest of three looks (nothing of ours is
        running then), or None when it can't be read."""
        vals = []
        for i in range(3):
            v = _int(self.io.read(card.dev + "/gpu_busy_percent"))
            if v is None:
                return None
            vals.append(v)
            if i < 2:
                self.pause(0.3)
        return min(vals)

    def _prompt_rate(self, model):
        """Prompt reading, tokens/s over PROMPT_RUNS long prompts (the core clock helps this more than answering)."""
        tok = sec = 0.0
        for _ in range(PROMPT_RUNS):
            r = self.ollama.generate_prompt(model)
            if not r:
                return None
            tok += r[0]
            sec += r[1]
        return round(tok / sec, 2) if sec > 0 else None

    def _round(self, card, model, since, prompt=False):
        """One short measurement: (answers tokens/s or None, what made it unreliable, a hard stop or None,
        prompt tokens/s when asked)."""
        tokens, secs, answers, why, hot = self._load(card, model, self.stock_seconds, since)
        tps = round(tokens / secs, 2) if answers and secs > 0 else None
        ptps = self._prompt_rate(model) if prompt and not why and tps else None
        return tps, list(self.noise), why, ptps

    def confirm_slowdown(self, card, model, since, first, ref, first_noise, base, tune, base_name="stock",
                         prompt=False):
        """A slow first reading is not yet a reason to revert (6b372: 148.7 against a stock of 249.5 was
        measured while a drive and an engine were failing, and reverted a card that was fine). Measure the
        baseline and the tuned state again, alternating CONFIRM_ROUNDS times, in the same conditions.
        `first` and `ref` are (answers tokens/s, prompt tokens/s or None) of the first tuned load and of the
        baseline taken earlier; base() and tune() put the card in each state. With prompt=True (the core clock)
        prompt reading is measured too and counts like answering.
          ("defer", text)  anything made a reading unreliable (the first or a repeat): nothing is changed,
                           and the check runs again next boot;
          ("revert", text) the slowdown repeated (each alternation, and the medians), or a hard error
                           (amdgpu, temperature) turned up, saying what was measured;
          ("ok", text)     it didn't repeat: the first reading was an outlier.
        The tuned state is always left applied, whatever the outcome (a revert changes it after)."""
        import statistics
        stock, tuned, problems = [], [first], list(first_noise)
        try:
            for _ in range(CONFIRM_ROUNDS):
                base()
                s, noise, why, sp = self._round(card, model, since, prompt)
                problems += noise
                if why:
                    return "revert", why
                tune()
                t, noise, why, tp = self._round(card, model, since, prompt)
                problems += noise
                if why:
                    return "revert", why
                if s is None or t is None or (prompt and (sp is None or tp is None)):
                    problems.append("a repeat gave no answer")
                    break
                stock.append((s, sp))
                tuned.append((t, tp))
        finally:
            tune()

        def fmt(rows, i):
            return "/".join("%.1f" % r[i] for r in rows if r[i] is not None)
        shown = "tuned %s, %s %s tokens/s" % (fmt(tuned, 0), base_name, fmt(stock, 0))
        if prompt:
            shown += "; prompt reading tuned %s, %s %s tokens/s" % (fmt(tuned, 1), base_name, fmt(stock, 1))
        if ref and ref[0]:
            shown += " (first reading %.1f against the earlier %s %.1f)" % (first[0], base_name, ref[0])
        if problems:
            seen = []
            for p in problems:
                if p not in seen:
                    seen.append(p)
            return "defer", ("answers looked slower than %s (%s) but the measurement wasn't clean (%s), so "
                             "nothing was changed; it is checked again next boot" % (base_name, shown, "; ".join(seen[:3])))
        if len(stock) < CONFIRM_ROUNDS:
            return "defer", "answers looked slower than %s (%s) but the repeats didn't finish; checked again next boot" % (base_name, shown)

        def slow(t, s):
            return t is not None and s is not None and t < SLOWER_LIMIT * s
        which = []
        for i, what in ((0, "answers"), (1, "prompt reading")):
            if i == 1 and not prompt:
                continue
            pairs = sum(1 for s, t in zip(stock, tuned[1:]) if slow(t[i], s[i]))
            t_med = statistics.median(r[i] for r in tuned if r[i] is not None)
            s_med = statistics.median(r[i] for r in stock if r[i] is not None)
            if pairs == CONFIRM_ROUNDS and t_med < SLOWER_LIMIT * s_med:
                which.append("%s were slower than %s in %d of %d repeats (median tuned %.1f against median %s %.1f "
                             "tokens/s, limit %d%%)" % (what, base_name, pairs, CONFIRM_ROUNDS, t_med, base_name, s_med,
                                                        round(SLOWER_LIMIT * 100)))
        if which:
            return "revert", "; ".join(which) + " (" + shown + ")"
        t_med = statistics.median(r[0] for r in tuned)
        s_med = statistics.median(r[0] for r in stock)
        if ref and ref[0]:
            return "ok", ("the first reading was slow but didn't repeat: median tuned %.1f against median %s %.1f "
                          "tokens/s (%s)" % (t_med, base_name, s_med, shown))
        return "ok", "answers%s held up: median tuned %.1f against median %s %.1f tokens/s (%s)" % (
            " and prompt reading" if prompt else "", t_med, base_name, s_med, shown)

    def measure_stock(self, card):
        vram = _int(self.io.read(card.dev + "/mem_info_vram_total"))
        if not self.ollama.up():
            return
        model = pick_model(self.ollama, vram)
        if not model:
            return
        tokens, secs, answers, _, _ = self._load(card, model, self.stock_seconds, None)
        if answers and secs > 0:
            self.st["measure"] = {"model": model, "stock_tps": round(tokens / secs, 2), "at": int(self.clock())}
            self.say("stock speed with %s: %.1f tokens/s" % (model, tokens / secs))

    def stage(self, part):
        """(tuned parts, baseline callable, tuned callable, what the baseline is called) for one part's check."""
        tuned_parts = [p for p in self.ready_parts()]
        if part not in tuned_parts:
            tuned_parts.append(part)
        base_parts = [p for p in tuned_parts if p != part]
        return tuned_parts, base_parts

    def self_check(self, card, part="power"):
        """60 s of answers on the tuned card. For the power part: back to stock on any amdgpu error, a
        temperature at its limit, or answers slower than stock. For a clock (memory, core): the same errors and
        temperatures put everything back; slower answers (and, for the core, slower prompt reading) put only
        that clock back, the power limit and the other clock kept. Returns the result: passed, reverted
        (everything), part-reverted, deferred, no answer, no load."""
        tuned_parts, base_parts = self.stage(part)
        since = (self.st.get("applied") or {}).get("at") or int(self.clock())
        vram = _int(self.io.read(card.dev + "/mem_info_vram_total"))
        prefer = (self.st.get("measure") or {}).get("model")
        model = pick_model(self.ollama, vram, prefer) if self.ollama.up() else None
        self.junction_max = CORE_JUNCTION_MAX_C if "core" in tuned_parts else JUNCTION_MAX_C
        chk = {"at": int(self.clock()), "values": self.applied_values(), "part": part}
        if model:
            self.say("checking %s: %d s of answers with %s, watching the kernel log and temperatures"
                     % (part, self.check_seconds, model))
        else:
            self.say("no model is installed (or Ollama isn't answering), so the check runs without a load; "
                     "it runs again with one when there is a model")
        # written down first: if the machine stops during the load, the next boot sees the check never ended
        self.st["check"] = dict(chk, result="running", boot=self.boot())
        self.save()

        def stopped():
            self.st["check"] = dict(chk, result="reverted", note="the check was stopped before it ended")
            self.revert(card, self.st["check"]["note"])
            self.save()
        try:
            tokens, secs, answers, why, hot = self._load(card, model, self.check_seconds if model else 0, since)
        except KeyboardInterrupt:
            stopped()
            raise
        if not why:
            errs = amdgpu_errors(self.journal(["--since", "@%d" % since]))
            if errs:
                why = "the kernel logged: " + errs[0]
        for k, v in card.temps().items():
            hot[k] = max(hot.get(k, v), v)
        if not why and hot.get("junction", 0) >= self.junction_max:
            why = "the junction reached %d C" % hot["junction"]
        if not why and hot.get("mem", 0) >= MEM_MAX_C:
            why = "the memory reached %d C" % hot["mem"]
        tps = round(tokens / secs, 2) if answers and secs > 0 else None
        m = self.st.get("measure") or {}
        deferred = confirmed = part_slow = None
        slow_why = None
        if part == "power":
            ref = (m.get("stock_tps"), None) if m.get("model") == model else None
            base_name = "stock"
            base = lambda: self.to_stock(card)
        else:
            r = self.st.get("power_ref") or {}
            ref = (r.get("tps"), None) if r.get("model") == model and part == "memory" else None
            base_name = "power-only" if part == "memory" else "without the core raise"
            base = lambda: self.apply_card(card, parts=base_parts)
        tune = lambda: self.apply_card(card, parts=tuned_parts)
        core = part == "core"
        first_slow = bool(tps and ref and ref[0] and tps < SLOWER_LIMIT * ref[0])
        if not why and model and tps and (first_slow or core):
            # slow against the earlier figure: that alone is not enough (6b372); the core clock is always
            # compared with and without it, answers and prompt reading both (it helps prompts more)
            noise = list(self.noise)
            ptps = self._prompt_rate(model) if core else None
            self.say("%s measuring again, %d alternations of %s and tuned%s" % (
                "answers look slower than %s;" % base_name if first_slow else "the core clock is judged by", CONFIRM_ROUNDS,
                base_name, " (answers and prompt reading)" if core else ""))
            try:
                verdict, text = self.confirm_slowdown(card, model, since, (tps, ptps), ref, noise, base, tune,
                                                      base_name, prompt=core)
            except KeyboardInterrupt:
                stopped()
                raise
            if verdict == "revert":
                slow_why = text
            elif verdict == "defer":
                deferred = text
            else:
                confirmed = text
        chk.update(model=model, answers=answers, tps=tps, max_c=hot)
        if why:                                   # a real error or heat: everything goes back at once
            chk["result"] = "reverted"
            chk["note"] = why
            self.st["check"] = chk
            self.revert(card, why)
            return "reverted"
        if slow_why and part == "power":
            chk["result"] = "reverted"
            chk["note"] = slow_why
            self.st["check"] = chk
            self.revert(card, slow_why)
            return "reverted"
        if slow_why:                              # a clock alone: only it goes back
            chk["result"] = "part-reverted"
            chk["note"] = "%s: %s" % (part, slow_why)
            self.st["check"] = chk
            self.part_revert(card, part, slow_why)
            return "part-reverted"
        if deferred:
            chk["result"] = "deferred"           # not reverted, not passed: pending stays, so it runs again
            chk["note"] = deferred
            self.st["check"] = chk
            self.say(deferred)
            return "deferred"
        if model and answers:
            chk["result"] = "passed"
            cp = self.st.get("checked_parts") or {}
            cp[part] = (self.st.get("applied") or {}).get("power_uw") if part == "power" else self.mhz(part)
            self.st["checked_parts"] = cp
            if part == "power" and tps:
                self.st["power_ref"] = {"model": model, "tps": tps}
            line = "%s passed: %d answers, no amdgpu errors" % (part, answers)
            if tps:
                line += ", %.1f tokens/s" % tps
                if m.get("model") == model and m.get("stock_tps") and part == "power":
                    line += " (stock %.1f, %+.1f%%)" % (m["stock_tps"], 100.0 * (tps / m["stock_tps"] - 1))
            if hot.get("junction") is not None:
                line += "; hottest: junction %d C, memory %s C" % (hot["junction"], hot.get("mem", "?"))
            if confirmed:
                line += "; " + confirmed
            result = "passed"
        elif model:
            chk["result"] = result = "no answer"
            line = "Ollama gave no answer, so the load wasn't tested; it is checked again next boot"
        else:
            chk["result"] = result = "no load"
            line = "no amdgpu errors so far; the load check is still to do"
        chk["note"] = line
        self.st["check"] = chk
        self.say(line)
        return result

    def pending(self):
        a = self.applied_values()
        return any(a) and self.pending_part() is not None

    def check_model(self, card):
        vram = _int(self.io.read(card.dev + "/mem_info_vram_total"))
        prefer = (self.st.get("measure") or {}).get("model")
        return pick_model(self.ollama, vram, prefer) if self.ollama.up() else None

    def wait_quiet(self, card, model):
        """Before measuring: up to QUIET_POLLS x 2 s for the card to be quiet (busy under QUIET_BUSY_PCT) and no
        other model loading. -> (True, "") or (False, why). With no model to measure with there is nothing to wait for."""
        if not model:
            return True, ""
        others, why, told = None, "", False
        for i in range(QUIET_POLLS):
            busy = _int(self.io.read(card.dev + "/gpu_busy_percent"))
            now = self._others(model)
            why = ""
            if busy is not None and busy >= QUIET_BUSY_PCT:
                why = "the card is %d%% busy" % busy
            if others is not None and now != others:
                why = (why + "; " if why else "") + "another model is loading (%s)" % ", ".join(sorted(now ^ others))
            if not why and others is not None:      # quiet, and the loaded models held still between two looks
                return True, ""
            others = now
            if why and not told:
                self.say("waiting up to %d s for the card to be quiet: %s" % (QUIET_POLLS * 2, why))
                told = True
            self.pause(2)
        return False, why or "the card never got quiet"

    def drop_unproven(self, card):
        """Experimental clocks that have not passed go back to stock; the power limit stays."""
        return self.apply_card(card, parts=self.ready_parts())

    def run_checks(self, card):
        """Each part not yet proven, in order: power, memory, core. A part that fails alone is taken off and the
        next is tried; anything else stops here (and runs again next time). Nothing experimental is applied
        before the card is quiet, and none is left applied when a check could not be finished."""
        for _ in range(4):
            part = self.pending_part()
            if part is None:
                return
            model = self.check_model(card)
            quiet, why = self.wait_quiet(card, model)
            if not quiet:
                self.defer_unclean(card, part, "the card wasn't quiet (%s)" % why)
                return
            self.apply_card(card, parts=self.stage(part)[0])
            if part != "power":
                a = self.st.get("applied") or {}
                key = "mclk" if part == "memory" else "sclk"
                stock = (self.st.get("stock") or {}).get(key)
                if a.get(key):
                    self.say(self.clock_phrase({key: a[key]}, part, key))
            result = self.self_check(card, part)
            if result == "deferred" and part != "power":
                self.defer_unclean(card, part, None)
                return
            if result not in ("passed", "part-reverted"):
                if part != "power" and result != "reverted":
                    self.drop_unproven(card)          # no answer, no load: an unverified clock is not left on
                return

    def defer_unclean(self, card, part, why):
        """A check that could not be done cleanly: only what has passed stays applied (the power limit is harmless and
        stays); an experimental clock is not applied, and the next run says so and tries again."""
        if part != "power":
            self.drop_unproven(card)
            ask = self.mhz(part)
            reason = why or (self.st.get("check") or {}).get("note") or "the check wasn't clean"
            self.say("%s +%d not applied yet: %s; run it again when the server is idle" % (part, ask, reason))
        else:
            self.say("the power check was not done: %s; it runs again next time" % why)
            self.st["check"] = {"at": int(self.clock()), "part": part, "result": "deferred", "note": why}

    # -- the commands --
    def set_clocks(self, memory=None, core=None):
        """`on --memory N --core N`: what is asked is saved with the state; asking again clears an earlier revert of that clock."""
        for part, n in (("memory", memory), ("core", core)):
            if n is not None:
                self.st[part] = clamp_mhz(n, CLOCK_PARTS[part]["max"])
                self.st.pop(part + "_reverted", None)
                self.st.pop(part + "_reverted_at", None)
                (self.st.get("checked_parts") or {}).pop(part, None)

    def cmd_on(self, memory=None, core=None):
        self.st["wanted"] = "on"
        self.st["version"] = STATE_VERSION
        for k in ("reverted", "reverted_at", "migrated", "memory_reverted", "memory_reverted_at", "core_reverted",
                  "core_reverted_at"):
            self.st.pop(k, None)
        self.st["checked_parts"] = {}
        self.st.setdefault("memory", 0)
        self.st.setdefault("core", 0)
        self.set_clocks(memory, core)
        card, why = find_card(self.io)
        if not card:
            self.say(why)
            self.save()
            return 0
        self.to_stock(card)
        mdl = self.check_model(card)
        quiet, qwhy = self.wait_quiet(card, mdl)
        if quiet:
            self.measure_stock(card)
        else:
            self.say("stock speed not measured: the card wasn't quiet (%s)" % qwhy)
        res = self.apply_card(card, parts=["power"])
        self.report_apply(res)
        self.save()
        self.run_checks(card)
        self.save()
        return 0

    def cmd_off(self):
        self.st["wanted"] = "off"
        self.st.pop("reverted", None)
        card, why = find_card(self.io)
        if card:
            for n in self.to_stock(card):
                self.say(n)
            self.st["applied"] = {"power_uw": None, "mclk": None, "sclk": None, "at": int(self.clock()), "boot": self.boot()}
            self.say("off: the card is at its stock power limit, memory clock and core clock")
        else:
            self.say(why)
        self.save()
        return 0

    def cmd_apply(self, check=True):
        """The saved choice, now (and its check, if it hasn't passed for these values)."""
        if self.st.get("reverted"):
            self.say("kept at stock after: %s. To try again: sudo ollama1-gpu-tune on" % self.st["reverted"])
            return 0
        if self.st.get("wanted") != "on":
            self.say("off. To turn it on: sudo ollama1-gpu-tune on")
            return 0
        card, why = find_card(self.io)
        if not card:
            self.say(why)
            return 0
        res = self.apply_card(card)
        self.report_apply(res)
        self.save()
        if check and self.pending():
            self.run_checks(card)
            self.save()
        return 0

    def cmd_restore(self):
        """After a boot or a wake (ollama1-gpu-tune.service): first the kernel log of
        the boot it last ran in (or, after a wake, this boot since it was applied):
        an amdgpu error there puts the card back to stock for good. Then the saved
        choice, without a load (Ollama isn't up yet)."""
        card, why = find_card(self.io)
        if not card:
            self.say(why)
            return 0
        a = self.st.get("applied") or {}
        now = self.boot()
        chk = self.st.get("check") or {}
        if chk.get("result") == "running" and chk.get("boot") != now and not self.st.get("reverted"):
            chk.update(result="reverted", note="the check under load never ended: the machine stopped during it")
            self.revert(card, chk["note"])
        if any(self.applied_values()) and a.get("boot") and not self.st.get("reverted"):
            if a["boot"] != now:
                if self.st.get("boot_checked") != a["boot"]:
                    lines = self.journal(["-b", a["boot"]])
                    self.st["boot_checked"] = a["boot"]
                    if lines is None:
                        self.say("couldn't read the kernel log of the boot it last ran in")
                    errs = amdgpu_errors(lines)
                    if errs:
                        self.revert(card, "the boot it last ran in logged: " + errs[0])
            elif a.get("at"):
                errs = amdgpu_errors(self.journal(["--since", "@%d" % a["at"]]))
                if errs:
                    self.revert(card, "the kernel logged: " + errs[0])
        if self.active():
            res = self.apply_card(card)
            self.report_apply(res)
        elif self.st.get("reverted"):
            self.say("kept at stock after: %s" % self.st["reverted"])
        self.save()
        return 0

    def cmd_check_pending(self):
        """After Ollama has started (ollama1-gpu-tune-check.service): the load
        check, when the values set now haven't passed it yet."""
        if not self.active() or not self.pending():
            return 0
        card, _ = find_card(self.io)
        if not card:
            return 0
        for _ in range(60):
            if self.ollama.up():
                break
            time.sleep(1)
        self.run_checks(card)
        self.save()
        return 0

    def cmd_setup(self, choice, memory=None, core=None):
        """setup.sh: "off" turns it off; "on" turns it on the first time, else
        re-applies the saved choice (a revert or an admin's "off" is kept). --gpu-tune-memory / --gpu-tune-core
        change what is asked (and so clear an earlier revert of that clock)."""
        if choice == "off":
            return self.cmd_off()
        if choice == "force-on" or not self.st.get("wanted"):
            return self.cmd_on(memory, core)
        before = (self.mhz("memory"), self.mhz("core"))
        self.set_clocks(*(n if n is not None and clamp_mhz(n, CLOCK_PARTS[p]["max"]) != clamp_mhz(self.st.get(p), CLOCK_PARTS[p]["max"]) else None
                          for p, n in (("memory", memory), ("core", core))))
        if before != (self.mhz("memory"), self.mhz("core")):
            self.save()
        return self.cmd_apply()

    def status_lines(self):
        st = self.st
        out = []
        card, why = find_card(self.io)
        if st.get("reverted"):
            head = "kept at stock after: %s (sudo ollama1-gpu-tune on to try again)" % st["reverted"]
        else:
            head = {"on": "on", "off": "off"}.get(st.get("wanted"), "not set up")
        out.append("Graphics card tuning: " + head)
        if not card:
            out.append("  " + why)
            return out
        out.append("  Card: Navi 21 [%s] at %s" % (card.device, card.pci))
        mask = running_mask()
        od = card.od()
        if mask is not None and mask & OD_BIT and od is not None:
            out.append("  Overdrive: on in the kernel (amdgpu.ppfeaturemask 0x%08x)" % mask)
        else:
            out.append("  Overdrive: not on yet; the power limit above stock (and any clock raise) needs a reboot after setup")
        pw = card.power()
        if pw.get("cap"):
            out.append("  Power limit: now %d W; stock %s W, the card's maximum %s W"
                       % (pw["cap"] // 10 ** 6, pw["default"] // 10 ** 6 if pw.get("default") else "?",
                          pw["max"] // 10 ** 6 if pw.get("max") else "?"))
        stock = st.get("stock") or {}
        for part, key, rng, label, usable in (("memory", "mclk", "MCLK", "Memory clock", od_usable),
                                              ("core", "sclk", "SCLK", "Core clock", od_core_usable)):
            ask = clamp_mhz(st.get(part), CLOCK_PARTS[part]["max"])
            if od and usable(od):
                cur = od[key][1]
                line = "  %s: now %d%s; stock %s; the card allows up to %d" % (
                    label, cur, " (%d MHz effective)" % (cur * 2) if key == "mclk" else "", stock.get(key) or "?",
                    od["range"][rng][1])
                if st.get(part + "_reverted"):
                    line += "; its raise was reverted (slower), power limit kept: %s" % st[part + "_reverted"]
                elif ask:
                    line += "; asked: +%d MHz" % ask
                else:
                    line += "; raise off (on --%s N)" % part
                out.append(line)
            elif ask:
                out.append("  %s: asked +%d MHz, but the overdrive table isn't usable yet" % (label, ask))
        if st.get("migrated"):
            out.append("  From an older version: its revert (%s) was dropped; now the power limit alone is tried"
                       % st["migrated"].get("old_revert"))
        a = st.get("applied") or {}
        if a.get("at"):
            out.append("  Now: " + self.parts_phrase({"power": a.get("power_uw"), "mclk": a.get("mclk"), "sclk": a.get("sclk")}))
        t = card.temps()
        if t:
            out.append("  Temperatures now: " + ", ".join("%s %d C" % (k, v) for k, v in sorted(t.items())))
        chk = st.get("check")
        if chk:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(chk.get("at", 0)))
            out.append("  Last check (%s): %s" % (when, chk.get("note") or chk.get("result")))
        elif self.pending():
            out.append("  Last check: still to do (it runs once Ollama is up and a model is installed)")
        out.append("  Turn off: sudo ollama1-gpu-tune off")
        return out

    def cmd_status(self):
        for line in self.status_lines():
            self.out(line)
        return 0
