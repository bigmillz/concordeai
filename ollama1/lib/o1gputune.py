"""Graphics-card tuning (6b361): the card's highest power limit and a small
memory-clock bump on an AMD Navi 21 card (RX 6800, 6800 XT, 6900 XT, 6950 XT),
for faster token generation. Writing an answer reads the whole model from the
card's memory for every token, so the memory clock is what sets the pace; the
power limit keeps the clocks up under load.

What is set (amdgpu sysfs). The card is found by its PCI vendor and device
ids under /sys/bus/pci/devices, never by its cardN number, which can change
from one boot to the next:

  power    hwmon power1_cap = power1_cap_max: the highest limit the driver
           itself reports for this card (microwatts). Never a value above it.
  memory   pp_od_clk_voltage "m 1 <clock>" then "c": the memory clock's top
           state raised by MCLK_BUMP in the driver's units, and never past the
           OD_RANGE MCLK maximum the driver reports. The driver's unit is the
           memory controller's clock; GDDR6's effective rate is twice it, so
           on a 6900 XT the stock 1000 is 2000 MHz effective, and +100 would be
           2200 (the card's OD_RANGE caps it lower: whatever it reports).

Core clocks and voltages are never touched. The memory clock needs the
kernel's overdrive switch (amdgpu.ppfeaturemask with OD_BIT, a GRUB drop-in
from setup.sh, so a reboot); on Navi 21 the power limit above the default
usually needs it too (without overdrive the driver reports max = default).

Safety: after the values are applied a self-check runs 60 s of answers on
the card (when a model is installed) while it watches the kernel log for
amdgpu errors and the card's junction and memory temperatures. Any error or a
temperature at or past its limit puts the card back to stock at once and
records why; it then stays at stock, across reboots, until the admin runs
`ollama1-gpu-tune on` again. Slow answers are judged more carefully (6b372): a
first slow reading starts a repeat, stock and tuned measured again,
alternating twice; only a slowdown that repeats (each alternation and the
medians) reverts, and the reason gives the figures. If anything made a
reading unreliable (a drive logging NVMe errors, another model loaded, the
card already busy for someone else, an answer that failed) nothing is changed
and the check runs again next boot. Every boot also reads the
kernel log of the boot the tuning last ran in, and goes back to stock if that
boot logged an amdgpu error.

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
MCLK_BUMP = 100                 # in the driver's units (x2 for GDDR6's effective rate)
JUNCTION_MAX_C = 105            # the stability test's limit too
MEM_MAX_C = 100
CHECK_SECONDS = 60
STOCK_SECONDS = 20
SLOWER_LIMIT = 0.95             # tuned answers under 95% of stock's speed: back to stock
# A slow reading alone never reverts (6b372): stock and tuned are measured again, alternating,
# and the slowdown must repeat; a reading taken while something else was going on is no reading.
CONFIRM_ROUNDS = 2              # stock/tuned alternations after a slow first reading
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
    out = {"mclk": {}, "range": {}, "odd": False}
    section = None
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"[A-Z_]+:", s):
            section = s[:-1]
            continue
        if section == "OD_MCLK":
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


def od_usable(od):
    """True when the memory clock can be set: OD_MCLK has exactly states 0 and 1
    in the plain shape, and OD_RANGE gives a sane MCLK range."""
    if not od or od["odd"] or set(od["mclk"]) != {0, 1}:
        return False
    rng = od["range"].get("MCLK")
    return bool(rng) and 0 < rng[0] <= rng[1]


def mclk_target(stock_max, range_max):
    """The memory clock to set: stock + MCLK_BUMP, never past the card's range.
    None when that is no higher than stock."""
    if not stock_max or not range_max:
        return None
    t = min(stock_max + MCLK_BUMP, range_max)
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


class Tuner:
    def __init__(self, io=None, ollama=None, journal=journal_lines, clock=time.time, mono=time.monotonic,
                 boot=boot_id, out=print, check_seconds=None, stock_seconds=None, pause=time.sleep):
        self.io = io or SysfsIO()
        self.pause = pause
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
        self.st = read_state()

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

    def apply_card(self, card):
        """Set the two values (clamped to what the card reports). Returns
        {"power": watts set or None, "mclk": clock set or None, "notes": [...]}."""
        res = {"power": None, "mclk": None, "notes": []}
        st = self.st
        # power: the most the driver allows, never above it
        pw = card.power()
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
        # memory clock: the driver's top state + MCLK_BUMP, inside OD_RANGE
        od = card.od()
        if od is None:
            res["notes"].append("the memory clock needs the kernel's overdrive switch: reboot after setup")
        elif not od_usable(od):
            res["notes"].append("this card's overdrive table isn't the shape this knows; the memory clock is left alone")
        else:
            cur = od["mclk"][1]
            if (st.get("applied") or {}).get("mclk") == cur and (st.get("stock") or {}).get("mclk"):
                stock = st["stock"]["mclk"]      # already set (after a wake, or a second run)
            else:
                try:                             # the driver's own value is stock: read it after a reset
                    self._od_write(card, ["r", "c"])
                except OSError:
                    pass
                od = card.od() or od
                cur = stock = od["mclk"][1]
                st.setdefault("stock", {})["mclk"] = stock
            lo, hi = od["range"]["MCLK"]
            target = mclk_target(stock, hi)
            if target is None:
                res["notes"].append("the card's memory clock range allows nothing above stock")
            elif target < lo:
                res["notes"].append("the memory clock target is outside the card's range; left alone")
            else:
                if cur != target:
                    try:
                        self._od_write(card, ["m 1 %d" % target, "c"])
                    except OSError as e:
                        res["notes"].append("the driver refused the memory clock (%s)" % e.strerror)
                        target = None
                if target is not None:
                    now = (card.od() or {}).get("mclk", {}).get(1)
                    if now != target:
                        res["notes"].append("the memory clock reads %s after setting %d" % (now, target))
                        target = None
                res["mclk"] = target
        st.setdefault("stock", {})["power_uw"] = pw.get("default")
        st["applied"] = {"power_uw": res["power"], "mclk": res["mclk"], "at": int(self.clock()),
                         "boot": self.boot(), "card": card.pci, "device": card.device}
        return res

    def report_apply(self, res):
        if res["power"]:
            self.say("power limit %d W (the card's maximum)" % (res["power"] // 10 ** 6))
        if res["mclk"]:
            self.say("memory clock %d (%d MHz effective), stock %d" % (res["mclk"], res["mclk"] * 2,
                                                                         self.st["stock"]["mclk"]))
        for n in res["notes"]:
            self.say(n)

    def revert(self, card, reason):
        self.st["reverted"] = reason
        self.st["reverted_at"] = int(self.clock())
        notes = self.to_stock(card) if card else []
        self.st["applied"] = {"power_uw": None, "mclk": None, "at": int(self.clock()), "boot": self.boot()}
        self.say("back to stock: " + reason)
        for n in notes:
            self.say(n)
        self.say("it stays at stock (also after a reboot) until: sudo ollama1-gpu-tune on")

    def active(self):
        return self.st.get("wanted") == "on" and not self.st.get("reverted")

    def applied_values(self):
        a = self.st.get("applied") or {}
        return [a.get("power_uw"), a.get("mclk")]

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
            if t.get("junction") is not None and t["junction"] >= JUNCTION_MAX_C:
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

    def _round(self, card, model, since):
        """One short measurement: (tokens/s or None, what made it unreliable, a hard stop or None)."""
        tokens, secs, answers, why, hot = self._load(card, model, self.stock_seconds, since)
        tps = round(tokens / secs, 2) if answers and secs > 0 else None
        return tps, list(self.noise), why

    def confirm_slowdown(self, card, model, since, first_tps, saved_stock, first_noise):
        """A slow first reading is not yet a reason to revert (6b372: 148.7 against a stock of 249.5 was
        measured while a drive and an engine were failing, and reverted a card that was fine). Measure
        stock and tuned again, alternating CONFIRM_ROUNDS times, in the same conditions:
          ("defer", text)  anything made a reading unreliable (the first or a repeat): nothing is changed,
                           and the check runs again next boot;
          ("revert", text) the slowdown repeated (each alternation, and the medians), or a hard error
                           (amdgpu, temperature) turned up: back to stock, saying what was measured;
          ("ok", text)     it didn't repeat: the first reading was an outlier.
        The tuned values are always left applied, whatever the outcome (a revert puts stock back after)."""
        import statistics
        stock, tuned, problems = [], [first_tps], list(first_noise)
        try:
            for _ in range(CONFIRM_ROUNDS):
                self.to_stock(card)
                s, noise, why = self._round(card, model, since)
                problems += noise
                if why:
                    return "revert", why
                self.apply_card(card)
                t, noise, why = self._round(card, model, since)
                problems += noise
                if why:
                    return "revert", why
                if s is None or t is None:
                    problems.append("a repeat gave no answer")
                    break
                stock.append(s)
                tuned.append(t)
        finally:
            self.apply_card(card)
        shown = "tuned %s, stock %s tokens/s (first reading %.1f against the earlier stock %.1f)" % (
            "/".join("%.1f" % x for x in tuned), "/".join("%.1f" % x for x in stock), first_tps, saved_stock)
        if problems:
            seen = []
            for p in problems:
                if p not in seen:
                    seen.append(p)
            return "defer", ("answers looked slower than stock (%s) but the measurement wasn't clean (%s), so "
                             "nothing was changed; it is checked again next boot" % (shown, "; ".join(seen[:3])))
        if len(stock) < CONFIRM_ROUNDS:
            return "defer", "answers looked slower than stock (%s) but the repeats didn't finish; checked again next boot" % shown
        slow_pairs = sum(1 for s, t in zip(stock, tuned[1:]) if t < SLOWER_LIMIT * s)
        t_med, s_med = statistics.median(tuned), statistics.median(stock)
        if slow_pairs == CONFIRM_ROUNDS and t_med < SLOWER_LIMIT * s_med:
            return "revert", ("answers were slower than stock in %d of %d repeats (median tuned %.1f against median "
                              "stock %.1f tokens/s, limit %d%%; %s)" % (slow_pairs, CONFIRM_ROUNDS, t_med, s_med,
                                                                         round(SLOWER_LIMIT * 100), shown))
        return "ok", ("the first reading was slow but didn't repeat: median tuned %.1f against median stock %.1f "
                      "tokens/s (%s)" % (t_med, s_med, shown))

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

    def self_check(self, card):
        """60 s of answers on the tuned card. Back to stock on any amdgpu error,
        a temperature at its limit, or answers slower than stock."""
        since = (self.st.get("applied") or {}).get("at") or int(self.clock())
        vram = _int(self.io.read(card.dev + "/mem_info_vram_total"))
        prefer = (self.st.get("measure") or {}).get("model")
        model = pick_model(self.ollama, vram, prefer) if self.ollama.up() else None
        chk = {"at": int(self.clock()), "values": self.applied_values()}
        if model:
            self.say("checking: %d s of answers with %s, watching the kernel log and temperatures"
                     % (self.check_seconds, model))
        else:
            self.say("no model is installed (or Ollama isn't answering), so the check runs without a load; "
                     "it runs again with one when there is a model")
        # written down first: if the machine stops during the load, the next boot sees the check never ended
        self.st["check"] = dict(chk, result="running", boot=self.boot())
        self.save()
        try:
            tokens, secs, answers, why, hot = self._load(card, model, self.check_seconds if model else 0, since)
        except KeyboardInterrupt:
            self.st["check"] = dict(chk, result="reverted", note="the check was stopped before it ended")
            self.revert(card, self.st["check"]["note"])
            self.save()
            raise
        if not why:
            errs = amdgpu_errors(self.journal(["--since", "@%d" % since]))
            if errs:
                why = "the kernel logged: " + errs[0]
        for k, v in card.temps().items():
            hot[k] = max(hot.get(k, v), v)
        if not why and hot.get("junction", 0) >= JUNCTION_MAX_C:
            why = "the junction reached %d C" % hot["junction"]
        if not why and hot.get("mem", 0) >= MEM_MAX_C:
            why = "the memory reached %d C" % hot["mem"]
        tps = round(tokens / secs, 2) if answers and secs > 0 else None
        m = self.st.get("measure") or {}
        deferred = confirmed = None
        if not why and tps and m.get("model") == model and m.get("stock_tps") \
                and tps < SLOWER_LIMIT * m["stock_tps"]:
            # slow against the stock figure taken earlier: that alone is not enough (6b372)
            noise = list(self.noise)
            self.say("answers look slower than stock (%.1f against %.1f tokens/s); measuring stock and tuned "
                     "again, %d alternations" % (tps, m["stock_tps"], CONFIRM_ROUNDS))
            try:
                verdict, text = self.confirm_slowdown(card, model, since, tps, m["stock_tps"], noise)
            except KeyboardInterrupt:
                self.st["check"] = dict(chk, result="reverted", note="the check was stopped before it ended")
                self.revert(card, self.st["check"]["note"])
                self.save()
                raise
            if verdict == "revert":
                why = text
            elif verdict == "defer":
                deferred = text
            else:
                confirmed = text
        chk.update(model=model, answers=answers, tps=tps, max_c=hot)
        if why:
            chk["result"] = "reverted"
            chk["note"] = why
            self.st["check"] = chk
            self.revert(card, why)
            return False
        if deferred:
            chk["result"] = "deferred"           # not reverted, not passed: pending() stays true, so it runs again
            chk["note"] = deferred
            self.st["check"] = chk
            self.say(deferred)
            return True
        if model and answers:
            chk["result"] = "passed"
            self.st["checked"] = self.applied_values()
            line = "passed: %d answers, no amdgpu errors" % answers
            if tps:
                line += ", %.1f tokens/s" % tps
                if m.get("model") == model and m.get("stock_tps"):
                    line += " (stock %.1f, %+.1f%%)" % (m["stock_tps"], 100.0 * (tps / m["stock_tps"] - 1))
            if hot.get("junction") is not None:
                line += "; hottest: junction %d C, memory %s C" % (hot["junction"], hot.get("mem", "?"))
            if confirmed:
                line += "; " + confirmed
        elif model:
            chk["result"] = "no answer"
            line = "Ollama gave no answer, so the load wasn't tested; it is checked again next boot"
        else:
            chk["result"] = "no load"
            line = "no amdgpu errors so far; the load check is still to do"
        chk["note"] = line
        self.st["check"] = chk
        self.say(line)
        return True

    def pending(self):
        a = self.applied_values()
        return any(a) and self.st.get("checked") != a

    # -- the commands --
    def cmd_on(self):
        self.st["wanted"] = "on"
        self.st.pop("reverted", None)
        self.st.pop("reverted_at", None)
        self.st.pop("checked", None)
        card, why = find_card(self.io)
        if not card:
            self.say(why)
            self.save()
            return 0
        self.to_stock(card)
        self.measure_stock(card)
        res = self.apply_card(card)
        self.report_apply(res)
        self.save()
        if res["power"] or res["mclk"]:
            self.self_check(card)
        self.save()
        return 0

    def cmd_off(self):
        self.st["wanted"] = "off"
        self.st.pop("reverted", None)
        card, why = find_card(self.io)
        if card:
            for n in self.to_stock(card):
                self.say(n)
            self.st["applied"] = {"power_uw": None, "mclk": None, "at": int(self.clock()), "boot": self.boot()}
            self.say("off: the card is at its stock power limit and memory clock")
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
            self.self_check(card)
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
        self.self_check(card)
        self.save()
        return 0

    def cmd_setup(self, choice):
        """setup.sh: "off" turns it off; "on" turns it on the first time, else
        re-applies the saved choice (a revert or an admin's "off" is kept)."""
        if choice == "off":
            return self.cmd_off()
        if choice == "force-on" or not self.st.get("wanted"):
            return self.cmd_on()
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
            out.append("  Overdrive: not on yet; the memory clock needs a reboot after setup")
        pw = card.power()
        if pw.get("cap"):
            out.append("  Power limit: now %d W; stock %s W, the card's maximum %s W"
                       % (pw["cap"] // 10 ** 6, pw["default"] // 10 ** 6 if pw.get("default") else "?",
                          pw["max"] // 10 ** 6 if pw.get("max") else "?"))
        if od and od_usable(od):
            stock = (st.get("stock") or {}).get("mclk")
            out.append("  Memory clock: now %d (%d MHz effective); stock %s; the card allows up to %d"
                       % (od["mclk"][1], od["mclk"][1] * 2, stock if stock else "?", od["range"]["MCLK"][1]))
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
