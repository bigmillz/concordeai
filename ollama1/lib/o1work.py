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

  COOL_S       60 s    when the work ends the fans fall 100% -> 20% and the lights
                       red -> orange -> yellow -> white over this, both linear in time
  RISE_S       5 s     the lights' way from white to red when work starts

Deep idle (6b434, per the owner: no more dimming; the lights stay at 100% brightness always):

  IDLE_DEEP_S  300 s   the fans: idle (counted from the end of the ramp, from the start, or
                       from a wake) this long, then, over BLUE_S (30 s) together:
  BLUE_S       30 s      the fans fall from the idle 20% to FAN_DEEP_PCT (10%), a straight
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
import time

import o1gpu

GPU_BUSY_PCT = 50                 # the card over this (strictly) is work
GPU_CONFIRM_S = 1.5               # ... for this long in a row; and at/under it this long in a row to end it
CLOCK_SLACK_S = 1e-6              # a sample due at exactly 1.5 s that a float clock puts a hair early still counts
CPU_HOT_C, CPU_COOL_C = 60, 55    # the processor's temperature: work from 60 C, until it is under 55 C (fans only)
COOL_S = 60                       # the cool-down: fans 100 -> 20%, lights red -> white
RISE_S = 5.0                      # the lights: white to red
IDLE_PCT = 20                     # the fans' idle level
IDLE_DEEP_S = 300.0               # the fans: idle this long, then they go deeper (and the lights blue, following them)
BLUE_S = 30.0                     # ... over this long (both)
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

    def busy(self):
        if not self.vendor and time.monotonic() - self.at > 60:
            self.at = time.monotonic()
            self.vendor = o1gpu.detect().get("vendor")
        return o1gpu.usage(self.vendor)["busy_pct"] if self.vendor else None


def probes():
    """The one probe both services read: the card's busy percent."""
    return {"gpu_busy": Gpu().busy}


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
