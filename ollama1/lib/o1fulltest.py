"""The full-power test (6b661, per the owner: "full GPU, full CPU, full fan, everything"; no disk stress).

Started from the admin panel's Controls tab as ollama1-fulltest@<seconds>.service (root). For up to five minutes:

  the graphics card   tools/gpu-burn.sh: llama-bench on an installed model's file, the card flat out
  the processor       stress-ng on every thread (--cpu 0, matrix maths, results verified), at nice 10 so the
                      card's one feeding thread keeps up; no disk, io or memory-hog stressors, nothing written but
                      this module's small files in /run/ollama1 and /var/lib/ollama1
  the fans and pump   100%, pump extreme: the fan service's hold file (/run/ollama1/fancal-hold.json, the one the fan
                      calibration uses), refreshed every 2 s; the fan service drops it 10 s after the last refresh

It refuses, and once running stops at once, on what this module checks itself (the page only asks):
  - a length over MAX_S (300 s) or not one of LENGTHS;
  - the night (NIGHT_START..NIGHT_END local time; the server's clock is the owner's, America/New_York on Ollama1);
  - the gateway serving: a request in flight or queued, or (to start) one in the last QUIET_BEFORE_S;
  - another full test (a lock), the fan calibration, a burn or stability test, a download or update;
  - the processor's Tctl at CPU_ABORT_C, the card at GPU_ABORT_C, the coolant at COOLANT_ABORT_C;
  - an NVMe or PCIe error (AER) in the kernel log, or another hardware-error record (an NVIDIA Xid, a machine check);
  - the fan service not reporting (the fans would not be under control).
A logind inhibitor is held while it runs, and only then. Whatever ends it (the end, Stop, a crash, a kill), the
unit's ExecStopPost (`stop-post`) takes the hold file away, so the fan service's normal curve comes back within
seconds; the processor's governor and the card's limits are never changed, so there is nothing else to put back.

Files: progress /run/ollama1/fulltest.json (every 2 s: the live readings, the countdown, the peaks so far), the
last result /var/lib/ollama1/fulltest-last.json (the result card: peaks, sustained clocks, throttling, kernel-log
errors, pass or fail), stress-ng's last lines /var/lib/ollama1/fulltest-cpu.txt. Numbers and sensor names only.
"""
import fcntl
import glob
import os
import re
import signal
import statistics
import subprocess
import threading
import time

import o1common
import o1fan
import o1idle
import o1sleep
import o1stats
from o1common import read_json_safe, write_json_atomic

LENGTHS = (60, 120, 180, 240, 300)
MAX_S = 300                       # the hard cap, whatever the unit's instance says
CPU_ABORT_C = 90                  # the processor's Tctl
GPU_ABORT_C = 85                  # the card's chip (NVIDIA) or edge (AMD) temperature
COOLANT_ABORT_C = 40              # the cooler's liquid
NIGHT_START, NIGHT_END = 23 * 60, 8 * 60     # minutes of the local day; the window wraps midnight
QUIET_BEFORE_S = 120              # no request through the gateway for this long before a start
TICK_S = 2
ERRORS_EVERY_S = 4                # the kernel log is read this often
WARMUP_S = 30                     # the sustained figures leave out the first seconds (the model loading, the ramp)
GPU_BUSY_MIN = 50                 # the card's average after WARMUP_S under this: its load did not run
FAN_STALE_FAIL_S = 10             # the fan service's status this old: the fans aren't under control
DEFAULT_MODEL = "gemma4:12b"
MODEL_RX = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
UNIT_GLOB = "ollama1-fulltest@*.service"
STOP_UNIT = "ollama1-fulltest-stop.service"

# the kernel log: what ends the test at once, and what each line is called on the result card
ERROR_KINDS = (
    (re.compile(r"\bnvme\d*\b.*\b(timeout|timed out|reset|abort|error|fail|dead|not ready)", re.I), "NVMe"),
    (re.compile(r"\bAER\b|PCIe Bus Error|pcieport .*(error|fail)|BadTLP|BadDLLP|Receiver Error", re.I), "PCIe"),
    (re.compile(r"NVRM: Xid", re.I), "NVIDIA Xid"),
    (re.compile(r"mce: \[Hardware Error\]|Machine check", re.I), "machine check"),
    (re.compile(r"amdgpu.*(ring.*timeout|gpu reset)", re.I), "amdgpu reset"),
)
# nvidia-smi's clocks_event_reasons.active bits: (bit, short name, counts against a pass)
GPU_REASONS = (
    (0x0000000000000004, "power limit", False),          # normal at full load: the card at its power limit
    (0x0000000000000008, "hardware slowdown", True),
    (0x0000000000000020, "thermal (software)", True),
    (0x0000000000000040, "thermal (hardware)", True),
    (0x0000000000000080, "power brake", True),
)
NV_FIELDS = ("utilization.gpu", "clocks.sm", "clocks.max.sm", "power.draw", "power.limit", "temperature.gpu",
             "clocks_event_reasons.active")


def p(path):
    return o1common.p(path)


def progress_path():
    return p("/run/ollama1/fulltest.json")


def last_path():
    return p("/var/lib/ollama1/fulltest-last.json")


def abort_path():
    return p("/run/ollama1/fulltest-stop")


def lock_path():
    return p("/run/ollama1/fulltest.lock")


def cpu_out_path():
    return p("/run/ollama1/fulltest-cpu.out")


def cpu_keep_path():
    return p("/var/lib/ollama1/fulltest-cpu.txt")


def burn_out_path():
    return p("/run/ollama1/fulltest-gpu.out")


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def length_of(arg):
    """The seconds an instance name asks for, or None: only one of LENGTHS (and never over MAX_S)."""
    s = str(arg or "")
    if not re.fullmatch(r"[0-9]{2,3}", s):
        return None
    n = int(s)
    return n if n in LENGTHS and n <= MAX_S else None


def is_night(now=None, localtime=time.localtime):
    t = localtime(time.time() if now is None else now)
    m = t.tm_hour * 60 + t.tm_min
    return m >= NIGHT_START or m < NIGHT_END


# ---- readings ---------------------------------------------------------------------------------------------------------

def _f(s):
    try:
        s = s.strip()
        return None if not s or s.startswith("[") or s.lower().startswith("n/a") else float(s)
    except (ValueError, AttributeError):
        return None


def read_gpu(run=subprocess.run):
    """{busy, mhz, max_mhz, watts, limit_w, c, reasons: [names], bad: [names]} from one nvidia-smi call; on another
    card the kit's own reading (busy, temperature, watts); None without one."""
    try:
        r = run(["nvidia-smi", "--query-gpu=" + ",".join(NV_FIELDS), "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
        line = (r.stdout or "").strip().splitlines()[0] if r.returncode == 0 and (r.stdout or "").strip() else ""
    except (OSError, subprocess.SubprocessError):
        line = ""
    parts = [x.strip() for x in line.split(",")] if line else []
    if len(parts) == len(NV_FIELDS):
        busy, mhz, maxmhz, watts, limit, c = (_f(x) for x in parts[:6])
        try:
            bits = int(parts[6], 16)
        except ValueError:
            bits = 0
        reasons = [name for bit, name, _bad in GPU_REASONS if bits & bit]
        bad = [name for bit, name, isbad in GPU_REASONS if bits & bit and isbad]
        return {"busy": busy, "mhz": mhz, "max_mhz": maxmhz, "watts": watts, "limit_w": limit, "c": c,
                "reasons": reasons, "bad": bad}
    try:
        g = o1stats.gpu() or {}
    except Exception:
        g = {}
    if not g:
        return None
    t = g.get("temps") or {}
    return {"busy": num(g.get("busy_pct")), "mhz": num(g.get("sclk_mhz")), "max_mhz": None,
            "watts": num(g.get("power_w")), "limit_w": num(g.get("power_cap_w")),
            "c": num(t.get("edge")) if num(t.get("edge")) is not None else num(t.get("junction")),
            "reasons": [], "bad": []}


def cpu_times(path="/proc/stat"):
    """(busy jiffies, total jiffies) of all processors together, or None."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("cpu "):
                    v = [int(x) for x in line.split()[1:9]]
                    idle = v[3] + (v[4] if len(v) > 4 else 0)
                    return sum(v) - idle, sum(v)
    except (OSError, ValueError):
        pass
    return None


def cpu_mhz(sysroot="/sys"):
    """(average, lowest, highest) of the cores' current clocks, MHz."""
    vals = []
    for f in glob.glob(sysroot + "/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            with open(f) as fh:
                vals.append(int(fh.read().strip()) / 1000.0)
        except (OSError, ValueError):
            continue
    if not vals:
        return None, None, None
    return round(sum(vals) / len(vals)), round(min(vals)), round(max(vals))


def cpu_tctl(sysroot="/sys"):
    """The processor's control temperature (k10temp Tctl, else coretemp's package), C."""
    for d in glob.glob(sysroot + "/class/hwmon/hwmon*"):
        try:
            with open(d + "/name") as f:
                name = f.read().strip()
        except OSError:
            continue
        if name in ("k10temp", "coretemp"):
            try:
                with open(d + "/temp1_input") as f:
                    return int(f.read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
    return None


def read_fans(status=None):
    """{"phase", "pct", "fans": [{label, rpm}], "pump_rpm", "coolant_c"} from the fan service, or None."""
    st = (status or o1fan.read_status)()
    if not isinstance(st, dict):
        return None
    fans, pump = [], None
    for o in st.get("outputs") or []:
        if not isinstance(o, dict):
            continue
        rpm = num(o.get("rpm"))
        if o.get("label") == "Pump":
            pump = rpm
        else:
            fans.append({"label": str(o.get("label") or "")[:30], "rpm": rpm})
    aio = st.get("aio") if isinstance(st.get("aio"), dict) else {}
    ast = aio.get("status") if isinstance(aio.get("status"), dict) else aio
    if pump is None:
        pump = num(ast.get("pump_rpm"))
    return {"phase": str(st.get("phase") or "")[:16], "pct": num(st.get("pct")), "fans": fans[:8],
            "pump_rpm": pump, "coolant_c": num(ast.get("coolant_c"))}


def gateway_load(now, activity=None, snapshot=None):
    """(in flight, queued, seconds since the last request); None for what couldn't be told."""
    act = (activity or o1idle.read_activity)(now)
    snap = (snapshot or o1stats.gateway_snapshot)() or {}
    queued = snap.get("queued") if not snap.get("stale") else None
    queued = queued if isinstance(queued, int) and not isinstance(queued, bool) else None
    since = now - act["last"] if act.get("last") else None
    return act.get("inflight"), queued, since


def kernel_errors(since, run=subprocess.run):
    """[(kind, line)] of the kernel-log records since `since` (epoch) that end the test; [] when none (or no journal)."""
    try:
        r = run(["journalctl", "-k", "-b", "0", "--no-pager", "-q", "-o", "cat", "--since", "@%d" % int(since)],
                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        for rx, kind in ERROR_KINDS:
            if rx.search(line):
                out.append((kind, line.strip()[:160]))
                break
    return out


# ---- refusing -----------------------------------------------------------------------------------------------------------

def own_busy(reasons):
    mine = dict(o1sleep.BUSY_UNITS).get(UNIT_GLOB)
    return [r for r in reasons if r != mine]


def refusal(seconds, now=None, localtime=time.localtime, activity=None, snapshot=None, busy=None, tools=None,
            fans=None, progress=None):
    """Why it must not start now, or None. Every outside thing is an argument (the tests fake them)."""
    now = time.time() if now is None else now
    if seconds is None:
        return "the length must be 1 to 5 minutes"
    if is_night(now, localtime):
        return "not at night (23:00 to 08:00): it is loud"
    st = (progress or read_progress)(now)
    if st and st.get("state") == "running":
        return "a full-power test is already running"
    inflight, queued, since = gateway_load(now, activity, snapshot)
    if inflight is None:
        return "couldn't tell whether the server is serving (the gateway isn't reporting)"
    if inflight > 0 or (queued or 0) > 0:
        return "the server is answering a request"
    if since is not None and since < QUIET_BEFORE_S:
        return "the server answered a request %d s ago; try again in %d minutes" % (max(0, since), QUIET_BEFORE_S // 60)
    reasons = own_busy((busy or o1sleep.busy_reasons)())
    if reasons:
        return reasons[0]
    running = [t for t in ((tools or o1idle.tools_running)() or [])]
    if running:
        return "%s is running" % running[0]
    f = (fans or read_fans)()
    if not f:
        return "the fan service isn't running: the fans couldn't be held at 100%"
    if f.get("phase") == "hot":
        return "a temperature is at its limit now"
    if f.get("phase") == "calibrating":
        return "the fan service is still measuring its fans; try again in a minute"
    return None


# ---- the loads ----------------------------------------------------------------------------------------------------------

def children(pid, proc="/proc"):
    kids = {}
    for st in glob.glob(proc + "/[0-9]*/stat"):
        try:
            with open(st) as f:
                raw = f.read()
            ppid = int(raw[raw.rindex(")") + 2:].split()[1])
            kids.setdefault(ppid, []).append(int(st.split("/")[-2]))
        except (OSError, ValueError, IndexError):
            continue
    out, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in out:
                out.append(k)
                todo.append(k)
    return out


class Load:
    """A program in a session of its own; poll() is its exit status once it has ended (None while it runs);
    stop() ends its whole tree."""

    def __init__(self, argv, out_path, env=None):
        self.out_path = out_path
        self.out = open(out_path, "w")
        self.p = subprocess.Popen(argv, stdout=self.out, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                                  close_fds=True, cwd=os.path.dirname(out_path))

    def poll(self):
        return self.p.poll()

    def tail(self, n=20):
        try:
            with open(self.out_path, errors="replace") as f:
                return [ln.rstrip() for ln in f.read().splitlines() if ln.strip()][-n:]
        except OSError:
            return []

    def stop(self):
        if self.p.poll() is None:
            pids = [self.p.pid] + children(self.p.pid)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for pid in pids:
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        pass
                try:
                    self.p.wait(5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            self.out.close()
        except OSError:
            pass


def cpu_argv(seconds, temp_dir):
    """stress-ng on every thread: matrix maths, verified; nothing that touches a disk (no hdd, io, vm stressors)."""
    return ["nice", "-n", "10", "stress-ng", "--temp-path", temp_dir, "--cpu", "0", "--cpu-method", "matrixprod",
            "--verify", "-t", "%ds" % (seconds + 5), "--metrics-brief"]


def cpu_load(seconds, temp_dir):
    return Load(cpu_argv(seconds, temp_dir), cpu_out_path())


def gpu_load(seconds, model):
    here = os.path.dirname(os.path.realpath(__file__))
    script = os.path.join(here, "..", "tools", "gpu-burn.sh")
    state = p("/var/lib/ollama1")
    try:
        os.makedirs(state + "/gpu-cache", exist_ok=True)
    except OSError:
        pass
    env = dict(os.environ, XDG_CACHE_HOME=state + "/gpu-cache", __GL_SHADER_DISK_CACHE_PATH=state + "/gpu-cache",
               O1_GPU_BURN_LOG=p("/run/ollama1/fulltest-gpu.log"), O1_BURN_SECONDS=str(seconds + 10),
               O1_BURN_REPORT="30", O1_BURN_TICK="5")
    return Load(["bash", script, str(seconds // 60 + 2), model], burn_out_path(), env=env)


def model_name(cfg=None, installed=None):
    """The model the card's burn runs on: config.json's fan_cal_model if set, else DEFAULT_MODEL."""
    if cfg is None:
        cfg = read_json_safe(o1common.Paths.config, {}, max_bytes=1 << 20)
    m = (cfg or {}).get("fan_cal_model") if isinstance(cfg, dict) else None
    return m if isinstance(m, str) and MODEL_RX.match(m) else DEFAULT_MODEL


# ---- the run ------------------------------------------------------------------------------------------------------------

def median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals)) if vals else None


def peak(vals):
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def summarize(samples, seconds, errors, cpu_verdict, gpu_note, state, reason):
    """The result card's figures and its one line: (summary dict)."""
    after = [s for s in samples if s["t"] >= min(WARMUP_S, seconds / 3)] or samples
    g = lambda k, rows=samples: [((r.get("gpu") or {}).get(k)) for r in rows]          # noqa: E731
    c = lambda k, rows=samples: [((r.get("cpu") or {}).get(k)) for r in rows]          # noqa: E731
    f = lambda k, rows=samples: [((r.get("fan") or {}).get(k)) for r in rows]          # noqa: E731
    seen, bad = [], []
    for r in after:
        for name in (r.get("gpu") or {}).get("reasons") or []:
            if name not in seen:
                seen.append(name)
        for name in (r.get("gpu") or {}).get("bad") or []:
            if name not in bad:
                bad.append(name)
    fan_peak = None
    for r in samples:
        for x in (r.get("fan") or {}).get("fans") or []:
            if num(x.get("rpm")) is not None:
                fan_peak = max(fan_peak or 0, x["rpm"])
    busy_after = [v for v in g("busy", after) if v is not None]
    gpu_busy_avg = round(sum(busy_after) / len(busy_after)) if busy_after else None
    out = {
        "peak": {"gpu_c": peak(g("c")), "gpu_w": peak(g("watts")), "gpu_mhz": peak(g("mhz")),
                 "cpu_c": peak(c("c")), "cpu_mhz": peak(c("mhz_max")), "coolant_c": peak(f("coolant_c")),
                 "pump_rpm": peak(f("pump_rpm")), "fan_rpm": fan_peak},
        "sustained": {"gpu_mhz": median(g("mhz", after)), "gpu_w": median(g("watts", after)),
                      "gpu_busy": gpu_busy_avg, "cpu_mhz": median(c("mhz", after)),
                      "cpu_busy": median(c("busy", after))},
        "limit_w": peak(g("limit_w")),
        "throttle": {"gpu": seen, "gpu_bad": bad,
                     "cpu": (peak(c("c")) or 0) >= CPU_ABORT_C - 5},
        "errors": [{"kind": k, "line": ln} for k, ln in errors[:8]],
        "cpu": cpu_verdict, "gpu": gpu_note,
    }
    fails = []
    if errors:
        fails.append("%d kernel-log error(s): %s" % (len(errors), ", ".join(sorted({k for k, _ln in errors}))))
    if bad:
        fails.append("the card slowed itself: " + ", ".join(bad))
    if cpu_verdict and cpu_verdict != "ok":
        fails.append("processor: " + cpu_verdict)
    if gpu_note:
        fails.append("graphics card: " + gpu_note)
    if state == "failed" and reason and reason not in " ".join(fails):
        fails.insert(0, reason)
    if state == "stopped":
        out["verdict"], out["line"] = "stopped", "Stopped: %s" % reason
    elif fails:
        out["verdict"], out["line"] = "fail", "Fail: " + "; ".join(fails)
    else:
        out["verdict"], out["line"] = "pass", "Pass: %d s at full power, no errors, no thermal slowdown" % int(
            samples[-1]["t"] if samples else 0)
    return out


class Runner:
    """One test. Every outside thing is an argument, so the tests drive it on a fake clock with fake loads."""

    def __init__(self, seconds, clock=time.monotonic, wall=time.time, wait=None, log=print, gpu=None, cpu=None,
                 fans=None, gateway=None, errors=None, loads=None, refuse=None, stop=None, localtime=time.localtime):
        self.seconds = min(int(seconds or 0), MAX_S)
        self.clock, self.wall, self.log = clock, wall, log
        self.stop = stop or threading.Event()
        self.wait = wait or self.stop.wait
        self.gpu = gpu or read_gpu
        self.cpu = cpu or CpuReader()
        self.fans = fans or read_fans
        self.gateway = gateway or (lambda now: gateway_load(now))
        self.errors = errors or kernel_errors
        self.loads = loads or self.real_loads
        self.refuse = refuse if refuse is not None else (lambda: refusal(length_of(seconds)))
        self.localtime = localtime
        self.state, self.reason = "running", ""
        self.why_stop = None
        self.samples = []
        self.started = None
        self.cpu_load = self.gpu_load = None
        self.found_errors = []
        self.tmp = None

    def real_loads(self):
        self.tmp = os.path.join(os.environ.get("TMPDIR") or "/tmp", "o1ft.%d" % os.getpid())
        os.makedirs(self.tmp, exist_ok=True)
        return cpu_load(self.seconds, self.tmp), gpu_load(self.seconds, model_name())

    def hold(self):
        write_json_atomic(o1fan.tuning_path(), {"at": int(self.wall()), "pct": 100, "by": "fulltest"}, mode=0o644)

    def unhold(self):
        try:
            os.unlink(o1fan.tuning_path())
        except OSError:
            pass

    def progress(self, t, sample=None, result=None):
        left = max(0, int(self.seconds - t)) if self.state == "running" else 0
        st = {"at": int(self.wall()), "state": self.state, "started": self.started, "seconds": self.seconds,
              "left": left, "t": int(t), "now": sample, "reason": self.reason[:200],
              "limits": {"cpu_c": CPU_ABORT_C, "gpu_c": GPU_ABORT_C, "coolant_c": COOLANT_ABORT_C}}
        if result is not None:
            st["result"] = result
        elif self.samples:
            st["peak"] = summarize(self.samples, self.seconds, [], None, None, "running", "")["peak"]
        write_json_atomic(progress_path(), st, mode=0o644)
        return st

    def check(self, t, s):
        """Why it must end now: (state, reason) or None."""
        if self.stop.is_set():
            return "stopped", self.why_stop or "stopped"
        g, c, f = s.get("gpu") or {}, s.get("cpu") or {}, s.get("fan")
        if num(c.get("c")) is not None and c["c"] >= CPU_ABORT_C:
            return "failed", "the processor reached %d C (stop at %d C)" % (c["c"], CPU_ABORT_C)
        if num(g.get("c")) is not None and g["c"] >= GPU_ABORT_C:
            return "failed", "the graphics card reached %d C (stop at %d C)" % (g["c"], GPU_ABORT_C)
        if f and num(f.get("coolant_c")) is not None and f["coolant_c"] >= COOLANT_ABORT_C:
            return "failed", "the coolant reached %.0f C (stop at %d C)" % (f["coolant_c"], COOLANT_ABORT_C)
        if not f:
            return "failed", "the fan service stopped reporting: the fans aren't under control"
        inflight, queued, _since = self.gateway(self.wall())
        if inflight is None:
            return "stopped", "the gateway stopped reporting: stopped so a request isn't kept waiting"
        if inflight > 0 or (queued or 0) > 0:
            return "stopped", "a request reached the server: stopped so it is answered at full speed"
        if self.found_errors:
            kinds = sorted({k for k, _ln in self.found_errors})
            return "failed", "kernel log: %s error" % " and ".join(kinds)
        if is_night(self.wall(), self.localtime):
            return "stopped", "it is night now (23:00)"
        return None

    def run(self):
        self.started = int(self.wall())
        why = self.refuse()
        if why:
            self.state, self.reason = "refused", why
            self.log("refused: " + why)
            return self.end(0, None, None)
        self.log("full-power test: %d s, the card, every processor thread, the fans and pump at 100%%" % self.seconds)
        t0 = self.clock()
        t = 0.0
        errors_at = -1e9
        cpu_verdict, gpu_note = None, None
        try:
            self.hold()
            self.cpu_load, self.gpu_load = self.loads()
            while True:
                t = self.clock() - t0
                self.hold()
                s = {"t": round(t, 1), "gpu": self.gpu(), "cpu": self.cpu(), "fan": self.fans()}
                self.samples.append(s)
                if t - errors_at >= ERRORS_EVERY_S:
                    errors_at = t
                    self.found_errors = self.errors(self.started)
                end = self.check(t, s)
                if end:
                    self.state, self.reason = end
                    break
                rc = self.cpu_load.poll() if self.cpu_load else None
                if rc is not None and t < self.seconds - 2:
                    self.state, self.reason = "failed", "stress-ng ended early (status %d)" % rc
                    break
                rc = self.gpu_load.poll() if self.gpu_load else None
                if rc is not None and t < self.seconds - 2:
                    last = [ln for ln in self.gpu_load.tail(8) if ln.startswith(("FAIL", "ABORTED", "PASSED"))]
                    gpu_note = "the card's load ended early: %s" % ((last or self.gpu_load.tail(1) or ["status %d" % rc])[-1])[:150]
                    self.gpu_load = None
                if t >= self.seconds:
                    self.state = "done"
                    break
                self.progress(t, s)
                self.wait(TICK_S)
        finally:
            for ld in (self.cpu_load, self.gpu_load):
                if ld is not None:
                    try:
                        ld.stop()
                    except Exception:
                        pass
            self.unhold()
            if self.tmp:
                subprocess.run(["rm", "-rf", "--one-file-system", self.tmp], timeout=30)
        self.found_errors = self.errors(self.started) or self.found_errors
        if self.cpu_load is not None:
            cpu_verdict = self.cpu_verdict()
        return self.end(t, cpu_verdict, gpu_note)

    def cpu_verdict(self):
        """stress-ng's own check: "ok", or what failed."""
        lines = self.cpu_load.tail(40)
        failed = [int(m.group(1)) for ln in lines for m in [re.search(r"failed: (\d+)", ln)] if m]
        if failed and max(failed) > 0:
            verdict = "%d stressor(s) failed verification" % max(failed)
        elif any("passed:" in ln for ln in lines) or self.state != "done":
            verdict = "ok"
        else:
            verdict = "stress-ng said nothing about its checks"
        if verdict != "ok":
            try:
                with open(cpu_keep_path(), "w") as f:
                    f.write("\n".join(lines[-20:]) + "\n")
            except OSError:
                pass
        return verdict

    def end(self, t, cpu_verdict, gpu_note):
        result = None
        if self.state != "refused":
            if self.state == "done":
                busy = [((s.get("gpu") or {}).get("busy")) for s in self.samples if s["t"] >= WARMUP_S]
                busy = [b for b in busy if b is not None]
                if busy and sum(busy) / len(busy) < GPU_BUSY_MIN and not gpu_note:
                    gpu_note = "only %d%% busy on average: its load did not run" % (sum(busy) / len(busy))
            result = summarize(self.samples, self.seconds, self.found_errors, cpu_verdict, gpu_note, self.state,
                               self.reason)
            self.log("%s (%s)" % (result["line"], self.state))
        st = self.progress(t, self.samples[-1] if self.samples else None, result)
        write_json_atomic(last_path(), st, mode=0o644)
        try:
            os.unlink(abort_path())
        except OSError:
            pass
        return 0 if result and result["verdict"] == "pass" else 1


class CpuReader:
    """busy % since the last call, clocks and Tctl."""

    def __init__(self, sysroot="/sys"):
        self.sysroot = sysroot
        self.prev = cpu_times()

    def __call__(self):
        cur = cpu_times()
        busy = None
        if cur and self.prev and cur[1] > self.prev[1]:
            busy = round(100.0 * (cur[0] - self.prev[0]) / (cur[1] - self.prev[1]))
        self.prev = cur
        avg, lo, hi = cpu_mhz(self.sysroot)
        c = cpu_tctl(self.sysroot)
        return {"busy": busy, "mhz": avg, "mhz_min": lo, "mhz_max": hi, "c": round(c, 1) if c is not None else None}


# ---- reading it back, stopping it ---------------------------------------------------------------------------------------

def read_progress(now=None):
    """The progress file; a "running" one not refreshed for 15 s is "stalled" (the run died)."""
    st = read_json_safe(progress_path(), None, max_bytes=131072)
    if not isinstance(st, dict) or not isinstance(st.get("at"), int):
        return None
    if st.get("state") == "running" and abs((time.time() if now is None else now) - st["at"]) > 15:
        st = dict(st, state="stalled")
    return st


def read_last():
    st = read_json_safe(last_path(), None, max_bytes=131072)
    return st if isinstance(st, dict) else None


def running(now=None):
    st = read_progress(now)
    return bool(st and st.get("state") == "running")


def stop_post(log=print):
    """ExecStopPost: after any end (a kill too) the fans' hold goes, and a progress file still saying "running" says
    the run ended without finishing."""
    try:
        d = read_json_safe(o1fan.tuning_path(), None, max_bytes=4096)
        if isinstance(d, dict) and d.get("by") == "fulltest":          # never the fan calibration's own hold
            os.unlink(o1fan.tuning_path())
    except OSError:
        pass
    st = read_json_safe(progress_path(), None, max_bytes=131072)
    if isinstance(st, dict) and st.get("state") == "running":
        st.update(state="stopped", at=int(time.time()), left=0,
                  reason="stopped from the panel" if os.path.exists(abort_path()) else "the run ended without finishing")
        st["result"] = {"verdict": "stopped", "line": "Stopped: " + st["reason"]}
        write_json_atomic(progress_path(), st, mode=0o644)
        write_json_atomic(last_path(), st, mode=0o644)
        log("ended without finishing: " + st["reason"])
    try:
        os.unlink(abort_path())
    except OSError:
        pass
    return 0


def request_stop(systemctl=None):
    """The panel's Stop (ollama1-fulltest-stop.service, root): a mark that says who stopped it, then a stop of the
    running instance (SIGTERM, then its whole control group)."""
    try:
        write_json_atomic(abort_path(), {"at": int(time.time())}, mode=0o644)
    except OSError:
        pass
    run = systemctl or (lambda: subprocess.run(["systemctl", "stop", UNIT_GLOB], timeout=60).returncode)
    return run()


def main(argv, log=print):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        st = read_progress() or read_last()
        print("no full-power test yet" if not st else "%s%s" % (
            st.get("state"), (": " + ((st.get("result") or {}).get("line") or st.get("reason") or "")) if st else ""))
        return 0
    if os.geteuid() != 0 and not os.environ.get("OLLAMA1_PREFIX"):
        log("run it as root (it is started from the admin panel: ollama1-fulltest@<seconds>.service)")
        return 1
    if cmd == "stop":
        return request_stop()
    if cmd == "stop-post":
        return stop_post(log)
    if cmd != "run" or len(argv) < 3:
        log("usage: ollama1-fulltest status|run SECONDS|stop|stop-post")
        return 2
    secs = length_of(argv[2])
    if secs is None:
        log("refused: the length must be one of %s seconds" % ", ".join(str(x) for x in LENGTHS))
        return 2
    os.makedirs(os.path.dirname(lock_path()), exist_ok=True)
    lock = open(lock_path(), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("refused: a full-power test is already running")
        return 2
    r = Runner(secs, log=log)

    def term(*_):
        r.why_stop = "stopped from the panel" if os.path.exists(abort_path()) else "stopped by the system"
        r.stop.set()
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    try:
        os.unlink(abort_path())
    except OSError:
        pass
    with o1sleep.Inhibit("a full-power test is running"):
        return r.run()
