"""What makes the server's fans and lights work, defined ONCE for both services
(6b385, moved here in 6b395; rewritten in 6b421, per the owner). The fan service
(lib/o1fan.py) and the lights (lib/o1leds.py) both read these numbers, so they
agree on when the work starts and how long the cool-down takes.

  the card        busy over GPU_BUSY_PCT (50%; sysfs gpu_busy_percent, strictly
                  over) for GPU_CONFIRM_S (1.5 s) in a row starts the work; at
                  or under it for GPU_CONFIRM_S in a row ends it. Every sample on
                  the other side restarts the count, so one blip over 50% (or one
                  dip under it) changes nothing. At the fans' 2 s poll that is two
                  samples in a row; at the lights' 0.25 s sampling, seven. No card
                  reading counts as not busy. Fans AND lights.
  the processor   its temperature (k10temp Tctl/Tdie, the reading lib/o1fan.py's
                  read_temps gives as "CPU") at CPU_HOT_C (60 C) or more starts the
                  work; under CPU_COOL_C (55 C) ends it (the 5 C gap keeps it from
                  flapping). FANS ONLY: the lights follow the card alone.

Nothing else is work any more: not the processors' utilisation, not a request in
flight, not a running tool or setup.sh / apt-get / unattended-upgrade (a burn test
or a model answering shows up as the card over 50%, which is what heats it).

  COOL_S       60 s    the lights' red -> orange -> yellow -> white, linear in time, ONLY with no fan
                       service (6b466: with one, the lights follow the fans' percentage, 100% red
                       to 20% white, so they land with the fans' FAN_RAMP_S)
  FAN_HOLD_S   30 s    when the work ends the fans stay at 100% this long (6b460), then
  FAN_RAMP_S   90 s    fall in a straight line 100% -> 20% over this
  RISE_S       5 s     the lights' way from white to red when work starts

Deep idle (6b434, per the owner: no more dimming; the lights stay at 100% brightness always):

  IDLE_DEEP_S  300 s   the fans: idle (counted from the end of the ramp, from the start, or
                       from a wake) this long, then, over BLUE_S (90 s) together:
  BLUE_S       90 s      the fans fall from the idle 20% to FAN_DEEP_PCT (10%), a straight
                         line, and stay there; the lights go from white to BLUE, a straight
                         line in RGB and in time, and stay blue at 100%. The lights take the
                         moment from the fan service's status file (its phase deepen / deep,
                         6b449: never blue while the fans are at the idle 20%); only without
                         a fan service do they count IDLE_DEEP_S of white themselves
  FAN_DEEP_PCT 10%       only for outputs that can run that low; every output's own floor wins
  WAKE_FADE_S  2 s     work while the lights are blue (or part way): back to white in this
                       long (full blue; proportionally less from part way), only THEN the
                       5 s rise to red. Work while white: the rise starts at once. The fans
                       go to 100% at once and deep idle is dropped.
  deep idle's temperature guard (fans): out of deep idle, back to 20%, when any monitored
  temperature is within DEEP_LIMIT_MARGIN_C (10 C) of its limit, or the CPU is at
  DEEP_CPU_C (50 C) or more, or the card's junction at DEEP_GPU_C (60 C) or more; back in
  only when all are DEEP_REENTER_C (3 C) under those figures. Any safety override
  (the over-limit "hot" one, the 60 C CPU trigger, the card trigger) raises the fans at once.
  The lights follow: the fans back at 20% take them from blue to white (WAKE_FADE_S).
"""
import os
import json
import time

import o1common
import o1gpu
import o1stats

GPU_BUSY_PCT = 50                 # the card over this (strictly) is work
GPU_CONFIRM_S = 1.5               # ... for this long in a row; and at/under it this long in a row to end it
CLOCK_SLACK_S = 1e-6              # a sample due at exactly 1.5 s that a float clock puts a hair early still counts
CPU_HOT_C, CPU_COOL_C = 60, 55    # the processor's temperature: work from 60 C, until it is under 55 C (fans only)
COOL_S = 60                       # the lights' cool-down, red -> white, only with no fan service (6b466)
def work_level(path=None):
    """The fans' level while working and in the hold after it: config.json's fan_work_pct (6b495, per the owner:
    quieter; 30-100), else 100. Heat and the one-time calibration always go to 100%. The lights are red at it."""
    try:
        with open(path or "/etc/ollama1/config.json", encoding="utf-8") as f:
            v = json.load(f).get("fan_work_pct")
    except (OSError, ValueError, AttributeError):
        return 100
    if isinstance(v, bool) or not isinstance(v, int):
        return 100
    return max(30, min(100, v))


# ---- the fan calibration's levels (6b505, per the owner: quiet, steady, from measured temperatures) -------------
# The calibration (lib/o1fancal.py, the panel's Calibrate fans) finds two working levels and saves them in
# CAL_FILE. While working the fans sit at the QUIET one; when any temperature comes within STEP_UP_MARGIN_C of its
# limit they go to the HEAVY one at once, and back to quiet only when every temperature is STEP_DOWN_MARGIN_C under
# its limit (8 C lower) and the heavy level has held STEP_MIN_S. Two steady levels with a wide band between them:
# the fans never follow the temperature up and down. The over-limit override (100%) is unchanged and always wins.
CAL_FILE = "/var/lib/ollama1/fan-calibration.json"
WORK_MIN_PCT = 30                 # the lowest working level, set by hand or calibrated
STEP_UP_MARGIN_C = 12             # a temperature this close to its limit: quiet -> heavy, at once
STEP_DOWN_MARGIN_C = 20           # every temperature this far under its limit (and STEP_MIN_S at heavy): heavy -> quiet
STEP_MIN_S = 180


def _pct(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and WORK_MIN_PCT <= v <= 100 else None


def read_calibration(path=None):
    """The calibration's result, checked: {"quiet_pct", "heavy_pct", "at", ...} or None (none, or not usable)."""
    d = o1common.read_json_safe(path or o1common.p(CAL_FILE), None, max_bytes=262144)
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    q, h, at = _pct(d.get("quiet_pct")), _pct(d.get("heavy_pct")), d.get("at")
    if q is None or h is None or h < q or not isinstance(at, int) or isinstance(at, bool):
        return None
    return d


def levels(cfg_path=None, cal_path=None):
    """The working levels: {"quiet", "heavy", "source"}. The calibration's (source "calibration") unless
    config.json's fan_calibration is "off" or there is none; then fan_work_pct for both ("setting"), else 100
    ("default"). fan_work_pct stays the manual override: with fan_calibration "off" it is what applies."""
    cfg_path = cfg_path or o1common.p("/etc/ollama1/config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg = cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("fan_calibration") != "off":
        cal = read_calibration(cal_path)
        if cal:
            return {"quiet": cal["quiet_pct"], "heavy": cal["heavy_pct"], "source": "calibration"}
    w = work_level(cfg_path)
    return {"quiet": w, "heavy": w, "source": "setting" if "fan_work_pct" in cfg else "default"}


class Levels:
    """levels(), read again only when config.json or the calibration file changed (a stat of each per call), so a
    new calibration or a changed setting applies without a restart."""

    def __init__(self, cfg_path=None, cal_path=None):
        self.cfg_path = cfg_path or o1common.p("/etc/ollama1/config.json")
        self.cal_path = cal_path or o1common.p(CAL_FILE)
        self.key, self.val = None, None

    def _sig(self, path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def __call__(self):
        key = (self._sig(self.cfg_path), self._sig(self.cal_path))
        if key != self.key or self.val is None:
            self.key, self.val = key, levels(self.cfg_path, self.cal_path)
        return self.val


FAN_HOLD_S = 30                   # the fans: 100% for this long after the work ends (6b460) ...
FAN_RAMP_S = 90                   # ... then a straight fall 100 -> 20% over this long
RISE_S = 5.0                      # the lights: white to red
IDLE_PCT = 20                     # the fans' idle level
IDLE_DEEP_S = 300.0               # the fans: idle this long, then they go deeper (and the lights blue, following them)
BLUE_S = 90.0                     # ... over this long (both; 6b460)
FAN_DEEP_PCT = 10                 # the fans' deep-idle level (an output's own floor wins)
BLUE = (0, 0, 255)                # the lights' deep-idle colour: pure blue (see README, Lights)
WAKE_FADE_S = 2.0                 # blue back to white in this long when work starts (proportional from part way)
DEEP_LIMIT_MARGIN_C = 10          # deep idle needs every temperature this far under its limit ...
DEEP_CPU_C = 50                   # ... the CPU under this ...
DEEP_GPU_C = 60                   # ... and the card's junction under this
DEEP_REENTER_C = 3                # after leaving, all of those must be this much lower again


def fan_level(L, pct, low=IDLE_PCT):
    """The percent an output with learned record `L` gets while the fans' level is `pct`: its own floor wins.
    min_pct is the stall floor (it starts at the idle level, which only says 20% was assumed fine); an output
    that stalled below the idle level (`no_deep`) is held at the idle level instead of the deep one."""
    floor = L["min_pct"]
    if floor <= low and L.get("no_deep") is True:
        floor = low
    elif floor <= low:
        floor = 0
    return min(100, max(pct, floor))


class Gpu:
    """The card's busy percent, found once and looked for again until there is one."""

    def __init__(self):
        self.vendor, self.at = None, -1e9
        self.full, self.full_at = None, -1e9      # the last full NVIDIA reading (its chip temperature, 6b505)

    def busy(self):
        if not self.vendor and time.monotonic() - self.at > 60:
            self.at = time.monotonic()
            self.vendor = o1gpu.detect().get("vendor")
        if not self.vendor:
            return None
        if self.vendor == "nvidia" and os.geteuid() == 0:
            # one nvidia-smi call gives the whole reading; the gateway's and the panel's sandboxes read it
            # from here (6b488, 6b500: the panel's gauges too)
            full = o1stats.gpu_nvidia()
            if full:
                self.full, self.full_at = full, time.monotonic()
            if full and full.get("busy_pct") is not None:
                o1gpu.publish(dict(full, vram_used_bytes=full["vram_used"], vram_total_bytes=full["vram_total"]))
                return full["busy_pct"]
        u = o1gpu.usage(self.vendor)
        return u["busy_pct"]

    def temp(self):
        """An NVIDIA card's one (chip) temperature from the last busy() reading, or None (none under 10 s old, or
        another card: an AMD card's sensors are hwmon files, which lib/o1fan.py reads itself). 6b505: the fans
        watch the NVIDIA card's heat too."""
        if not self.full or time.monotonic() - self.full_at > 10:
            return None
        return number((self.full.get("temps") or {}).get("edge"))


def probes():
    """The probes both services read: the card's busy percent (and, for the fans, an NVIDIA card's temperature)."""
    g = Gpu()
    return {"gpu_busy": g.busy, "gpu_temp": g.temp}


def number(v):
    """A percent reading as a float, or None (no reading: a bool, a string, NaN, an error's None)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


class GpuTrigger:
    """The card's work trigger, debounced: `update(now, percent)` on every sample, True while working.
    `since` is when the state last changed (None until it first does)."""

    def __init__(self):
        self.on = False
        self.run_from = None             # the first sample, in a row, on the other side of the line
        self.since = None
        self.pct = None                  # the last reading (None: none)

    def update(self, now, pct):
        self.pct = number(pct)
        busy = self.pct is not None and self.pct > GPU_BUSY_PCT
        if busy == self.on:
            self.run_from = None
        else:
            if self.run_from is None or now < self.run_from:
                self.run_from = now
            if now - self.run_from >= GPU_CONFIRM_S - CLOCK_SLACK_S:
                self.on, self.run_from, self.since = busy, None, now
        return self.on


class CpuTrigger:
    """The processor's temperature trigger (the fans only): `update(readings)` with the CPU sensors'
    readings in C (None for a disconnected or stuck sensor). Work from CPU_HOT_C; it ends under CPU_COOL_C.
    No CPU sensor at all keeps the state; one that went implausible lets go (a stuck sensor is never a
    reason for 100%, the same rule as the overheat override)."""

    def __init__(self):
        self.on = False
        self.c = None

    def update(self, readings):
        readings = list(readings)
        if not readings:
            return self.on                       # no sensor this time: nothing new
        good = [c for c in readings if c is not None]
        self.c = max(good) if good else None
        if self.c is None:
            self.on = False
        elif self.c >= CPU_HOT_C:
            self.on = True
        elif self.on and self.c < CPU_COOL_C:
            self.on = False
        return self.on
