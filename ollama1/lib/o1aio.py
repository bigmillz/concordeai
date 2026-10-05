"""An optional liquid-cooler backend for ollama1-fan (6b388): a Corsair Hydro
Platinum / Pro XT / Elite cooler (an H115i Platinum is USB 1b1c:0c17) has its
fans and its pump on the cooler's own USB controller, not on the motherboard's
fan headers, so the Super-IO outputs never reach them. liquidctl talks to it.

Nothing here is required: no liquidctl, or no cooler, is one log line and the
case fans carry on. Every call to liquidctl is a subprocess with a timeout
(never fatal); the cooler's status is read at most every STATUS_S seconds (it
is a USB transaction) and a command is sent only when its target changes, and
never more often than every MIN_WRITE_S seconds.

The policy follows the fans' phases (lib/o1fan.py):

  phase                 fans            pump
  working, hot          100%            extreme    (the card over 50%, the CPU at 60 C, a sensor at its limit)
  calibrating           100%            balanced   (first start: measuring rpm)
  ramp                  the fans' %, falling from 100 to 20 over 60 s    balanced
                        (from the moment the work ends; there is no hold at 100% any more, 6b421)
  idle20                20%, or higher  quiet
                        for a fan that stalls (the same learning as the case fans)

A coolant at COOLANT_HOT_C or more forces the pump to extreme and the fans to
100% until it is COOLANT_HYST_C under that, so the pump is at full speed only
while it is needed. A fan that reads no rpm at 100% is kept at 100%: its low
levels can't be checked.

Safe without us: when the service stops for any reason (and when liquidctl
fails FAILS_MAX times in a row) the pump is set to balanced and each fan to a
conservative coolant-temperature curve (SAFE_CURVE) that the cooler follows by
itself. A marker file says the cooler is under our control, so ExecStopPost
puts it back after a crash too.

Unverified against a real cooler (written from liquidctl's documentation): the
status keys ("Liquid temperature", "Fan N speed", "Pump speed", ...) are read
loosely, and a missing one is just not shown.
"""
import json
import os
import re
import shutil
import subprocess
import threading

import o1common
from o1common import read_json_safe, write_json_atomic

STATUS_S = 5                     # the cooler's status at most this often
MIN_WRITE_S = 5                  # and a changed target is sent no more often than this
DETECT_S = 300                   # looking again for liquidctl / a cooler
CALL_TIMEOUT_S = 4               # every liquidctl call (the service's watchdog is 10 s)
FAILS_MAX = 5                    # this many failed calls in a row: the cooler is left on its safe curve
COOLANT_HOT_C, COOLANT_HYST_C = 40, 5
SETTLE_S = 6
SAFE_PUMP = "balanced"
SAFE_CURVE = ((25, 30), (35, 60), (45, 100))     # (coolant C, fan %): 30% @ 25 C, 60% @ 35 C, 100% @ 45 C
VENDOR_CORSAIR = 0x1b1c
# Corsair Hydro Platinum / Pro XT / Elite Capellix products (the H115i Platinum is 0c17). From liquidctl's
# documentation, from memory: `liquidctl list` is the authority, and the service accepts a cooler by liquidctl's
# own description at run time. These only decide whether setup installs liquidctl.
HYDRO_IDS = frozenset(0x1b1c0000 | p for p in (0x0c15, 0x0c17, 0x0c18, 0x0c19, 0x0c29, 0x0c2a, 0x0c2b, 0x0c2c,
                                               0x0c2d, 0x0c2e, 0x0c32, 0x0c33, 0x0c34, 0x0c35, 0x0c36))
HYDRO_RX = re.compile(r"hydro|\bH\d{2,3}i\b", re.I)


def state_path():
    return o1common.p("/var/lib/ollama1/fan-aio.json")


def usb_present(sysroot=None):
    """True when a Corsair Hydro cooler is on USB (sysfs idVendor/idProduct): what makes setup install liquidctl."""
    base = (sysroot or os.environ.get("OLLAMA1_SYS", "/sys")) + "/bus/usb/devices"
    try:
        names = os.listdir(base)
    except OSError:
        return False
    for n in names:
        try:
            v = int(open("%s/%s/idVendor" % (base, n)).read().strip(), 16)
            pid = int(open("%s/%s/idProduct" % (base, n)).read().strip(), 16)
        except (OSError, ValueError):
            continue
        if (v << 16 | pid) in HYDRO_IDS:
            return True
    return False


def parse_status(text):
    """liquidctl's `status --json` -> {"coolant_c", "fans": {n: rpm}, "pump_rpm", "pump_mode"}, loosely: a key that
    isn't there is None / empty."""
    out = {"coolant_c": None, "fans": {}, "pump_rpm": None, "pump_mode": None}
    data = json.loads(text)
    dev = data[0] if isinstance(data, list) and data else data
    for row in (dev.get("status") if isinstance(dev, dict) else None) or []:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            continue
        key, val = row["key"].lower().strip(), row.get("value")
        num = val if isinstance(val, (int, float)) and not isinstance(val, bool) else None
        m = re.fullmatch(r"fan\s*(\d+)\s*speed", key)
        if m and num is not None:
            out["fans"][int(m.group(1))] = int(num)
        elif key in ("liquid temperature", "coolant temperature") and num is not None:
            out["coolant_c"] = float(num)
        elif key == "pump speed" and num is not None:
            out["pump_rpm"] = int(num)
        elif key == "pump mode" and isinstance(val, str):
            out["pump_mode"] = val.lower()
    return out


class Aio:
    """One cooler. `step(now)` is the whole machine (a thread calls it every second in the service; the tests call it
    on a fake clock); `set_phase` says what the fans are doing."""

    def __init__(self, binary=None, log=print, run=subprocess.run, which=shutil.which):
        self.binary, self.log, self.run, self.which = binary, log, run, which
        self.lock = threading.Lock()
        self.learned, self.persist = {}, (lambda: None)
        self.phase, self.pct = "idle20", 20
        self.exe = None                  # the liquidctl found
        self.match = None                # the cooler's description, for --match
        self.name = None
        self.state = "not looked for yet"
        self.detect_at = -1e9
        self.status_at = -1e9
        self.write_at = -1e9
        self.status = {"coolant_c": None, "fans": {}, "pump_rpm": None, "pump_mode": None}
        self.coolant_hot = False
        self.applied_pump = None
        self.applied = {}                # fan number -> percent last sent
        self.since = {}                  # fan number -> when that was sent
        self.checked = {}
        self.fails = 0
        self.disabled = False
        self.said = set()
        self.initialized = False

    def attach(self, learned, persist, log):
        """Share the fan service's learned table (and how to save it) and its log."""
        self.learned, self.persist, self.log = learned, persist, log

    def say(self, key, line):
        if key not in self.said:
            self.said.add(key)
            self.log(line)

    # -- input from the service ---------------------------------------------------------
    def set_phase(self, phase, pct):
        with self.lock:
            self.phase, self.pct = phase, pct

    def reset(self):
        """After a wake: the cooler may have lost its settings, so initialize and send everything again."""
        with self.lock:
            self.initialized = False
            self.applied_pump, self.applied, self.since, self.checked = None, {}, {}, {}
            self.write_at = -1e9
            self.status_at = -1e9

    def active(self):
        return self.match is not None and not self.disabled

    def needs_calibration(self):
        """True while a fan of the cooler has no rpm measured at 100% yet (the first start only)."""
        with self.lock:
            return self.active() and any("aio/fan%d" % n not in self.learned for n in self.status["fans"])

    # -- liquidctl ----------------------------------------------------------------------
    def _exe(self):
        return self.binary or self.which("liquidctl")

    def call(self, args, match=True, timeout=CALL_TIMEOUT_S):
        """(ok, stdout). Never raises; a failure counts toward FAILS_MAX when it was a call to a known cooler."""
        cmd = [self.exe] + (["--match", self.match] if match and self.match else []) + args
        try:
            r = self.run(cmd, capture_output=True, text=True, timeout=timeout)
            ok, out = r.returncode == 0, r.stdout or ""
            why = (r.stderr or "").strip()[:120]
        except (OSError, subprocess.SubprocessError) as e:
            ok, out, why = False, "", type(e).__name__
        if match:
            if ok:
                self.fails = 0
            else:
                self.fails += 1
                self.log("liquidctl %s failed (%s): %d of %d" % (" ".join(args[:2]), why or "no reason", self.fails,
                                                                 FAILS_MAX))
        return ok, out

    def detect(self, now):
        self.detect_at = now
        self.exe = self._exe()
        if not self.exe:
            self.state = "liquidctl is not installed"
            self.say("nolc", "liquidctl is not installed: a liquid cooler, if there is one, is not controlled")
            return False
        try:
            r = self.run([self.exe, "list", "--json"], capture_output=True, text=True, timeout=CALL_TIMEOUT_S)
            devs = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            devs = None
        if not isinstance(devs, list):
            self.state = "liquidctl list failed"
            self.say("listfail", "liquidctl list failed: no liquid cooler is controlled (tried again later)")
            return False
        for d in devs:
            desc = d.get("description") if isinstance(d, dict) else None
            if isinstance(desc, str) and (d.get("vendor_id") == VENDOR_CORSAIR or "corsair" in desc.lower()) \
                    and HYDRO_RX.search(desc):
                self.match, self.name, self.state = desc, desc, "controlling"
                self.log("liquid cooler: %s (through liquidctl)" % desc)
                return True
        self.state = "no liquid cooler found"
        self.say("nocooler", "no Corsair Hydro liquid cooler found by liquidctl")
        return False

    # -- the machine --------------------------------------------------------------------
    def poll(self, now):
        self.status_at = now
        ok, out = self.call(["status", "--json"])
        if not ok:
            return
        try:
            st = parse_status(out)
        except (ValueError, AttributeError, TypeError):
            self.fails += 1
            self.log("liquidctl status gave something unreadable: %d of %d" % (self.fails, FAILS_MAX))
            return
        with self.lock:
            self.status = st
        c = st["coolant_c"]
        if c is not None and 0 < c < 120:
            if c >= COOLANT_HOT_C:
                if not self.coolant_hot:
                    self.log("coolant %.0f C: pump extreme and fans 100%% until it is under %d C"
                             % (c, COOLANT_HOT_C - COOLANT_HYST_C))
                self.coolant_hot = True
            elif self.coolant_hot and c < COOLANT_HOT_C - COOLANT_HYST_C:
                self.coolant_hot = False

    def plan(self):
        """(pump mode, {fan: percent}) for now."""
        with self.lock:
            phase, fans = self.phase, sorted(self.status["fans"]) or [1, 2]
        if self.coolant_hot or phase in ("working", "hot"):
            pump = "extreme"
        elif phase in ("ramp", "calibrating"):
            pump = "balanced"
        else:
            pump = "quiet"
        base = 100 if (self.coolant_hot or phase in ("working", "hot", "calibrating")) else \
            self.pct if phase == "ramp" else 20
        out = {}
        for n in fans:
            L = self.learned.get("aio/fan%d" % n)
            if base >= 100 or L is None or not L["rpm100"]:
                out[n] = 100                       # (no rpm at 100%: its low levels can't be checked)
            else:
                out[n] = min(100, max(base, L["min_pct"]))
        return pump, out

    def write(self, now, pump, fans):
        """Only what changed. Fans first when going up is not needed: each is its own command."""
        self.write_at = now
        if not self.initialized:
            ok, _ = self.call(["initialize"])
            if not ok:
                return
            self.initialized = True
        for n, pct in sorted(fans.items()):
            if self.applied.get(n) != pct:
                ok, _ = self.call(["set", "fan%d" % n, "speed", str(pct)])
                if not ok:
                    return
                self.applied[n], self.since[n] = pct, now
                self.checked.pop(n, None)
        if self.applied_pump != pump:
            ok, _ = self.call(["set", "pump", "mode", pump])
            if not ok:
                return
            self.applied_pump = pump
        self.mark(True)

    def learn(self, now):
        """The same learning as the case fans: rpm at 100%, and a fan that stalls at a low level is raised."""
        with self.lock:
            rpms = dict(self.status["fans"])
        for n, rpm in rpms.items():
            pct, since = self.applied.get(n), self.since.get(n)
            if pct is None or since is None or now - since < SETTLE_S:
                continue
            lk, L = "aio/fan%d" % n, self.learned.get("aio/fan%d" % n)
            if pct >= 100:
                if L is None:
                    self.learned[lk] = {"rpm100": rpm or None, "min_pct": 20, "always100": False, "pump_checked": True}
                    self.log("cooler fan %d: %s at 100%%" % (n, "%d rpm" % rpm if rpm else "no rpm"))
                    self.persist()
                elif rpm and (not L["rpm100"] or abs(rpm - L["rpm100"]) > 0.15 * L["rpm100"]):
                    L["rpm100"] = rpm
                    self.persist()
            elif self.checked.get(n) != pct:
                self.checked[n] = pct
                if L and L["rpm100"] and rpm == 0:
                    L["min_pct"] = min(100, pct + 10)
                    self.log("cooler fan %d: 0 rpm at %d%%, stalled: its lowest level is now %d%%" % (n, pct, L["min_pct"]))
                    self.persist()

    def mark(self, controlled):
        """The marker ExecStopPost reads: is the cooler under our control?"""
        try:
            if controlled:
                write_json_atomic(state_path(), {"controlled": True, "match": self.match}, mode=0o600)
            elif os.path.exists(state_path()):
                os.unlink(state_path())
        except OSError:
            pass

    def step(self, now):
        """One turn: find the cooler if needed, read its status if it is due, send what changed."""
        if self.disabled:
            return
        if self.match is None:
            if now - self.detect_at >= DETECT_S and not self.detect(now):
                return
            if self.match is None:
                return
        if self.fails >= FAILS_MAX:
            self.fail_safe()
            return
        if now - self.status_at >= STATUS_S:
            self.poll(now)
        if self.fails >= FAILS_MAX:
            self.fail_safe()
            return
        self.learn(now)
        pump, fans = self.plan()
        if (pump != self.applied_pump or fans != {n: self.applied.get(n) for n in fans}) \
                and now - self.write_at >= MIN_WRITE_S:
            self.write(now, pump, fans)
            if self.fails >= FAILS_MAX:
                self.fail_safe()

    def fail_safe(self):
        self.log("liquidctl failed %d times in a row: the cooler is left on its safe curve, the case fans go on"
                 % FAILS_MAX)
        self.safe_exit()
        self.disabled, self.state = True, "liquidctl keeps failing: left on its safe curve"

    # -- leaving ---------------------------------------------------------------------------
    def safe_exit(self):
        """The cooler safe without us: the pump balanced and each fan on a coolant-temperature curve it follows by itself."""
        if not self.exe or not self.match:
            return False
        fans = sorted(self.status["fans"]) or [1, 2]
        flat = [str(x) for pair in SAFE_CURVE for x in pair]
        ok = True
        for n in fans:
            ok = self.call(["set", "fan%d" % n, "speed"] + flat)[0] and ok
        ok = self.call(["set", "pump", "mode", SAFE_PUMP])[0] and ok
        self.applied_pump, self.applied = None, {}
        if ok:
            self.mark(False)
        else:
            self.log("couldn't put the cooler on its safe curve")
        return ok

    # -- for the status --------------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            st = dict(self.status)
            fans = []
            for n in sorted(st["fans"]):
                L = self.learned.get("aio/fan%d" % n) or {}
                fans.append({"n": n, "rpm": st["fans"][n], "pct": self.applied.get(n), "min_pct": L.get("min_pct")})
            return {"found": self.match is not None, "name": self.name, "state": self.state,
                    "coolant_c": st["coolant_c"], "coolant_hot": self.coolant_hot, "pump_rpm": st["pump_rpm"],
                    "pump_mode": self.applied_pump or st["pump_mode"], "fans": fans}


def safe_exit_if_controlled(log=print, run=subprocess.run, which=shutil.which):
    """ExecStopPost: when the marker says the cooler was under our control, put it on its safe curve (whatever
    the way the service ended). True when there was nothing to do or it was done."""
    d = read_json_safe(state_path(), None, max_bytes=4096)
    if not isinstance(d, dict) or d.get("controlled") is not True or not isinstance(d.get("match"), str):
        return True
    aio = Aio(log=log, run=run, which=which)
    aio.exe, aio.match = aio._exe(), d["match"]
    if not aio.exe:
        return False
    ok, out = aio.call(["status", "--json"])
    if ok:
        try:
            aio.status = parse_status(out)
        except (ValueError, AttributeError, TypeError):
            pass
    return aio.safe_exit()


def aio_text(a):
    """The cooler in a few words for the admin line: 'cooler 31 C, pump quiet 2400 rpm, fans 520 rpm'."""
    if not a or not a.get("found") or a.get("state") != "controlling":
        return ""
    parts = []
    if a.get("coolant_c") is not None:
        parts.append("cooler %.0f C" % a["coolant_c"])
    pump = ("pump %s" % a["pump_mode"] if a.get("pump_mode") else "pump") + (" %d rpm" % a["pump_rpm"] if a.get("pump_rpm") is not None else "")
    parts.append(pump)
    rpms = [str(f["rpm"]) for f in a.get("fans", []) if f.get("rpm") is not None]
    if rpms:
        parts.append("fans %s rpm" % "/".join(rpms))
    return ", ".join(parts)
