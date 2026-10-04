"""What counts as "working" for the server (6b385, moved here in 6b395 so the fan
service and the lights share ONE definition): what auto sleep already counts as
busy (lib/o1idle.py), read with the same probes and the same limits:

  * a request in flight (the gateway's activity file);
  * the graphics card at GPU_IDLE_PCT or more;
  * a long job running (stability-test.sh and the like: o1idle.tools_running);
  * a 1-minute load average above LOAD_BUSY.

The code here is moved from lib/o1fan.py unchanged; the fan's behaviour and tests
did not change.
"""
import time

import o1gpu
import o1idle


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


class Work:
    """`probes` supplies inflight (an int, or None when it can't be told), gpu_busy, tools (a
    list) and loadavg; `clock` is monotonic; /proc is walked at most every `tools_every` s."""

    def __init__(self, probes, clock=time.monotonic, tools_every=6):
        self.p, self.clock, self.tools_every = probes, clock, tools_every
        self.tools_at, self.tools = -1e9, []

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
