"""What counts as "working" for the server (6b385, moved here in 6b395 so the fan
service and the lights share ONE definition; the load average dropped in 6b401).

  * a request in flight (the gateway's activity file): at once;
  * a long job running (stability-test.sh, ram_model_test.py and the others
    o1idle.scan_tools knows): at once;
  * the graphics card at GPU_BUSY_PCT (15%) or more, averaged over GPU_WINDOW_S (6 s);
  * the processors at CPU_BUSY_PCT (40%) or more, averaged over CPU_WINDOW_S (10 s),
    from /proc/stat deltas: user + nice + system + irq + softirq over every field,
    so time waiting on a disk (iowait) and idle time do not count.

Once a card or processor signal has made it work, it stays working until that
value has been under its threshold for RELEASE_S (6 s).

The 1-minute load average is NOT used here any more: it counts tasks blocked
on a disk (a RAID check, boot-time work, apt, snapd) and lags by a minute, so
it kept the fans at 100% for minutes with nothing running. Auto sleep keeps
its own load rule (o1idle.decide); that is a different decision and untouched.

The averages are taken at the poll rate of whoever calls working() (the fan
service and the lights both look every 2 s); a window needs samples spanning
nearly all of it (within a second) before it says anything, so the first
seconds after a start say nothing.
"""
import collections
import time

import o1gpu
import o1idle

GPU_BUSY_PCT, GPU_WINDOW_S = 15, 6
CPU_BUSY_PCT, CPU_WINDOW_S = 40, 10
RELEASE_S = 6                    # under its threshold this long before a card or processor signal lets go
SPAN_SLACK_S = 1.0               # a window must span its length less this
KEEP_SLACK_S = 0.5               # and keeps samples this much older (a tick that came a little late)


class Gpu:
    """The card's busy percent, found once and looked for again until there is one."""

    def __init__(self):
        self.vendor, self.at = None, -1e9

    def busy(self):
        if not self.vendor and time.monotonic() - self.at > 60:
            self.at = time.monotonic()
            self.vendor = o1gpu.detect().get("vendor")
        return o1gpu.usage(self.vendor)["busy_pct"] if self.vendor else None


def cpu_times(proc="/proc"):
    """(busy, total) jiffies of the whole machine from /proc/stat's "cpu" line: busy is user + nice + system +
    irq + softirq; total is every field of user..steal, so iowait and idle are in the total and not in busy.
    None when it can't be read."""
    try:
        with open(proc + "/stat") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == "cpu":
                    v = [int(x) for x in parts[1:9]]
                    v += [0] * (8 - len(v))
                    user, nice, system, idle, iowait, irq, softirq, steal = v
                    return user + nice + system + irq + softirq, sum(v)
    except (OSError, ValueError):
        pass
    return None


def probes():
    gpu = Gpu()
    return {"inflight": lambda: o1idle.read_activity(time.time())["inflight"], "gpu_busy": gpu.busy,
            "tools": o1idle.tools_running, "cpu": cpu_times}


class Work:
    """`probes` supplies inflight (an int, or None when it can't be told), gpu_busy (percent), tools (a
    list) and cpu ((busy, total) jiffies); `clock` is monotonic; /proc is walked for tools at most every
    `tools_every` s."""

    def __init__(self, probes, clock=time.monotonic, tools_every=6):
        self.p, self.clock, self.tools_every = probes, clock, tools_every
        self.tools_at, self.tools = -1e9, []
        self.gpu_s = collections.deque()          # (t, percent)
        self.cpu_s = collections.deque()          # (t, busy, total)
        self.on = {"gpu": False, "cpu": False}
        self.below = {"gpu": None, "cpu": None}
        self.gpu_avg = self.cpu_avg = None

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

    @staticmethod
    def _trim(q, now, window):
        while q and q[0][0] < now - window - KEEP_SLACK_S:
            q.popleft()
        return bool(q) and q[-1][0] - q[0][0] >= window - SPAN_SLACK_S

    def _gpu(self, now):
        g = self._probe("gpu_busy")
        if isinstance(g, (int, float)) and not isinstance(g, bool):
            if self.gpu_s and self.gpu_s[-1][0] >= now:
                self.gpu_s.pop()
            self.gpu_s.append((now, float(g)))
        if not self._trim(self.gpu_s, now, GPU_WINDOW_S):
            return None
        return sum(v for _t, v in self.gpu_s) / len(self.gpu_s)

    def _cpu(self, now):
        c = self._probe("cpu")
        if isinstance(c, (tuple, list)) and len(c) == 2 and all(isinstance(x, (int, float)) for x in c):
            if self.cpu_s and self.cpu_s[-1][0] >= now:
                self.cpu_s.pop()
            self.cpu_s.append((now, c[0], c[1]))
        if not self._trim(self.cpu_s, now, CPU_WINDOW_S):
            return None
        (_t0, b0, t0), (_t1, b1, t1) = self.cpu_s[0], self.cpu_s[-1]
        if t1 <= t0 or b1 < b0:
            return None
        return 100.0 * (b1 - b0) / (t1 - t0)

    def _latch(self, name, avg, threshold, now):
        """On at the threshold; off only when it has been under it for RELEASE_S."""
        if avg is None:
            return self.on[name]
        if avg >= threshold:
            self.on[name], self.below[name] = True, None
        elif self.on[name]:
            if self.below[name] is None:
                self.below[name] = now
            elif now - self.below[name] >= RELEASE_S:
                self.on[name], self.below[name] = False, None
        return self.on[name]

    def working(self, now=None):
        """(working?, the reasons: which signal made it work)."""
        now = self.clock() if now is None else now
        why = []
        n = self._probe("inflight")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            why.append("a request is running")
        t = self._tools(now)
        if t:
            why.append("running " + ", ".join(sorted(str(x) for x in t)))
        self.gpu_avg = self._gpu(now)
        if self._latch("gpu", self.gpu_avg, GPU_BUSY_PCT, now):
            why.append("the card is %d%% busy" % round(self.gpu_avg if self.gpu_avg is not None else GPU_BUSY_PCT))
        self.cpu_avg = self._cpu(now)
        if self._latch("cpu", self.cpu_avg, CPU_BUSY_PCT, now):
            why.append("processors %d%% busy" % round(self.cpu_avg if self.cpu_avg is not None else CPU_BUSY_PCT))
        return bool(why), why
