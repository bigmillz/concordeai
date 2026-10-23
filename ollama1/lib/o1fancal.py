"""Fan calibration (6b505, per the owner: "a 2-3 min query, 10 min, then a 20-30 min burn", steady levels, no
hunting, quiet for the household). Started from the admin panel's Fans card (ollama1-fancal.service, root); its
Abort button starts ollama1-fancal-abort.service, which stops it at once.

What it does, about 40 minutes in all, on the server's own Ollama (127.0.0.1:11434, as root) and the graphics card:

  short   150 s    a real model query (the model in config.json's fan_cal_model, else gemma4:12b), fans at
                   100%: the baseline. Every temperature the fan service watches gets a CEILING: MARGIN_C (10 C)
                   under its limit, or, for a sensor already within 13 C of its limit at 100%, its own 100% reading
                   plus NEAR_C (3 C). A sensor within NEAR_C of its limit even at 100% ends the run: nothing to gain.
  long     600 s    the same query, going on; the fans step DOWN the ladder (80, 65, 50, 40, 30%), 120 s a
                   step, while every temperature stays under its ceiling. The first step that doesn't goes back up
                   one and never down again; the rest of the 10 minutes holds that level (checked again each 120 s).
                   The level in force at the end is the QUERY level.
  burn    1500 s    tools/gpu-burn.sh (llama-bench on the same model's file: the card flat out, the processor near
                   idle), from the query level, 300 s a block, only ever UP a step when a block fails; it ends at the
                   first block that passes after 25 minutes (at most 30 minutes; then one step up from where it is).
                   That level is the BURN level.

A block fails at once when a reading reaches its ceiling and isn't falling after its first 60 s, and at its end
when the second half's peak or where the readings are heading (project(): the rise of three equal windows, as a
first-order system settles) is at or over the ceiling. The levels are steps of a fixed ladder, held for minutes:
the fans never follow the temperature.

The result, in /var/lib/ollama1/fan-calibration.json (lib/o1work.py reads it): the QUIET level = the query level
+ MARGIN_PCT (10 points), and the HEAVY level = the burn level + MARGIN_PCT, at least the quiet one; both 30-100.
The fan service works at the quiet level and moves to the heavy one only when a temperature comes within 12 C of
its limit, back when all are 20 C under (lib/o1work.py). Why this target is in NOTES.md, 6b505.

Safety: it stops at once (and saves nothing) on the panel's Abort or a stop of the unit; the fan service's
over-limit override (any temperature at its limit; the fans are at 100% then anyway); a new hardware-error record in
the kernel log (an NVIDIA Xid, a machine check, a PCIe AER error, an amdgpu ring timeout); the fan service no
longer reporting; a request reaching the gateway (the owner's work comes first); the load failing or the card not
getting busy. gpu-burn.sh's own 90 C stop for an NVIDIA card (105 C junction for AMD) stays. While it holds a
level the fan service shows phase "tuning"; the hold file is refreshed every 2 s and ignored when 10 s old, so a
dead run can't keep the fans low. The auto sleep and the Sleep button say no while the unit runs (o1sleep
BUSY_UNITS) and a logind inhibitor is held.

It refuses to start while the server is serving (a request in flight, or one in the last 2 minutes), while a
download, update, setup.sh, another burn or a hold lock (6b499) is running, when the fan service isn't running or
is still measuring its fans, when the model isn't installed, when llama-bench isn't there, or with no reading from
the card. Its files hold numbers and sensor names only, never what a model was asked or answered.
"""
import glob
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time

import o1common
import o1fan
import o1idle
import o1ollama
import o1sleep
import o1work
from o1common import read_json_safe, write_json_atomic

SHORT_S, LONG_S, BURN_S, BURN_MAX_S = 150, 600, 1500, 1800
LONG_BLOCK_S, BURN_BLOCK_S = 120, 300
MIN_JUDGE_S = 60                  # a partial block at a stage's end is judged only when it is this long
BREACH_GRACE_S = 60               # a reading over its ceiling fails a block at once only after this long into it
LADDER = (100, 80, 65, 50, 40, 30)
FULL = 100
MARGIN_C = 10                     # every temperature this far under its limit (its ceiling) ...
NEAR_C = 3                        # ... or, already near its limit at 100%, at most this over its 100% reading
MARGIN_PCT = 10                   # added to each level found (a warmer room, dust, an older paste)
PROJECT_CAP_C = 8                 # the most project() adds to the latest window
NOISE_C = 0.5                     # a window-to-window change this small is a sensor's jitter, not a trend
BUSY_OVER = 50                    # the card's busy percent is counted from its first reading over this ...
BUSY_GRACE_S = 90                 # ... which must come within this long of a stage's start (a model load, shaders)
BUSY_MIN_PCT = 30                 # ... and the average from there must be at least this
TICK_S = 2
QUIET_BEFORE_S = 120              # no request through the gateway for this long before a start
ERRORS_EVERY_S = 30               # the kernel log is looked at this often
LOG_EVERY_S = 10                  # a sample of every temperature this often goes into the log file
DEFAULT_MODEL = "gemma4:12b"      # the burn tools' default model too
MODEL_RX = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
OLLAMA = "http://127.0.0.1:11434"
UNIT = "ollama1-fancal.service"
STAGES = (("short", "a short model query", SHORT_S), ("long", "a longer model query", LONG_S),
          ("burn", "a graphics-card burn", BURN_S))
STAGE_TEXT = {k: t for k, t, _s in STAGES}
HW_ERRORS = (("NVRM: Xid", "an NVIDIA Xid error"), ("Hardware Error", "a machine-check (hardware) error"),
             ("AER:", "a PCIe error (AER)"), ("amdgpu", "an amdgpu ring timeout or reset"))
HW_RX = re.compile(r"mce: \[Hardware Error\]|amdgpu.*(ring.*timeout|gpu reset)|NVRM: Xid|AER:", re.I)
PROMPT = ("Write a long, practical guide to keeping a vegetable garden through a whole year: soil, sowing, "
          "watering, pests, harvest and storage. Go through every month in turn, in detail. Do not stop early.")


def progress_path():
    return o1common.p("/run/ollama1/fancal.json")


def abort_path():
    return o1common.p("/run/ollama1/fancal-abort")


def burn_log_path():
    return o1common.p("/run/ollama1/fancal-burn.log")


def result_path():
    return o1common.p(o1work.CAL_FILE)


def last_path():
    return o1common.p("/var/lib/ollama1/fan-calibration-last.json")


def log_path():
    return o1common.p("/var/lib/ollama1/fan-calibration-log.json")


def up(level):
    """The next ladder level above `level` (100 stays 100)."""
    return min([x for x in LADDER if x > level] or [FULL])


def down(level):
    """The next ladder level below `level`, or None."""
    return max([x for x in LADDER if x < level] or [0]) or None


def margin(level):
    return max(o1work.WORK_MIN_PCT, min(FULL, level + MARGIN_PCT))


# ---- judging a stretch of readings ---------------------------------------------------------------------------------

def project(values):
    """Where a temperature series is heading: the means of its last three equal windows (m1, m2, m3), d1 = m2 - m1,
    d2 = m3 - m2. Settled or falling (d2 within NOISE_C): m3. Only just started rising (d1 within NOISE_C): m3 + 2 d2.
    Rising but slowing (d2 < d1): m3 plus the rest of a geometric series, d2^2 / (d1 - d2), as a first-order system
    settles. Rising as fast or faster: not known, so m3 + PROJECT_CAP_C. Never more than that."""
    vals = [v for v in values if v is not None]
    if len(vals) < 6:
        return max(vals) if vals else None
    k = len(vals) // 3
    w = vals[len(vals) - 3 * k:]
    m1, m2, m3 = (sum(w[i * k:(i + 1) * k]) / float(k) for i in range(3))
    d1, d2 = m2 - m1, m3 - m2
    if d2 <= NOISE_C:
        return m3 + max(0.0, d2)                     # settled (a sensor's own jitter is about this much)
    if d1 <= NOISE_C:
        extra = 2 * d2                               # it has only started rising: two more windows of it
    elif d1 > d2:
        extra = d2 * d2 / (d1 - d2)
    else:
        extra = PROJECT_CAP_C
    return m3 + min(PROJECT_CAP_C, extra)


def smooth(vals, k=3):
    """A moving average of `k` readings (6 s at the 2 s tick): a processor's Tctl jumps several degrees for a second
    or two with every boost, which is not the heat the fans are for."""
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i - k + 1):i + 1]
        out.append(sum(w) / float(len(w)))
    return out


def ceiling_for(limit, base):
    """A sensor's ceiling from its limit and its peak with the fans at 100% (None: not seen then); None when it is
    within NEAR_C of its limit even at 100% (the run ends: no quieter level can keep it under)."""
    top = limit - MARGIN_C
    if base is None or base <= top - NEAR_C:
        return top
    if base >= limit - NEAR_C:
        return None
    return base + NEAR_C


class Search:
    """The plan, driven by step(t, temps, busy) every TICK_S (t: seconds since the start; temps: the fan service's
    [{"label", "c", "limit"}]; busy: the card's percent or None). `stage` and `level` say what should run; `done`
    with `result` (or `error`) when it has finished. Pure: no clock, no files."""

    def __init__(self):
        self.stage, self.stage_at, self.level, self.block_at = "short", 0.0, FULL, 0.0
        self.block = {}                    # label -> [readings] in this block
        self.limits, self.ceil = {}, {}
        self.banned = 0                    # a level that failed: nothing at or under it again
        self.passed = set()                # levels that passed a block (this stage's load)
        self.last_ok = None                # the last judgement
        self.descending = True
        self.found = {}                    # "query", "burn"
        self.blocks = []                   # one summary per judged block
        self.busy_sum = self.busy_n = 0
        self.done, self.result, self.error = False, None, None

    def ceiling(self, label):
        c = self.ceil.get(label)
        return c if c is not None else self.limits[label] - MARGIN_C

    # -- the stretch at one level -------------------------------------------------------------------------------
    def new_block(self, t, level=None):
        if level is not None:
            self.level = level
        self.block_at, self.block = t, {}

    def judge(self, t):
        """Judge the block so far: True when every temperature's second-half peak and projection are under its
        ceiling. Recorded in `blocks`."""
        rows = []
        for label, vals in self.block.items():
            if not vals:
                continue
            half = smooth(vals)[len(vals) // 2:]
            peak, proj, top = max(half), project(vals), self.ceiling(label)
            rows.append({"label": label, "limit": self.limits[label], "ceiling": round(top, 1), "peak": round(peak, 1),
                         "projected": round(proj, 1), "mean": round(sum(vals) / len(vals), 1),
                         "room": round(top - max(peak, proj), 1)})
        rows.sort(key=lambda r: r["room"])
        ok = bool(rows) and rows[0]["room"] > 0
        self.blocks.append({"stage": self.stage, "pct": self.level, "s": int(t - self.block_at), "ok": ok,
                            "worst": rows[0]["label"] if rows else None, "temps": rows[:24]})
        self.last_ok = ok
        return ok

    def breached(self, t):
        """A reading at or over its ceiling, past the block's first BREACH_GRACE_S, and not falling (the last 30 s
        no lower than the 30 s before): the level is not enough, no need to wait for the block's end."""
        if t - self.block_at < BREACH_GRACE_S:
            return False
        n = max(1, int(30 / TICK_S))
        for label, raw in self.block.items():
            vals = smooth(raw)
            if len(vals) >= 2 * n and vals[-1] >= self.ceiling(label) and max(vals[-n:]) >= max(vals[-2 * n:-n]):
                return True
        return False

    def after(self, ok, t):
        """What a judgement means: a pass goes down a step (while still descending) or holds; a failure goes up a
        step for good."""
        if ok:
            self.passed.add(self.level)
            nxt = down(self.level) if self.descending else None
            self.new_block(t, nxt if nxt is not None and nxt > self.banned else None)
            return
        self.banned = max(self.banned, self.level)
        self.passed.discard(self.level)
        self.descending = False
        self.new_block(t, up(self.level))

    # -- the stages -------------------------------------------------------------------------------------------------
    def busy(self, t, b):
        """The load must reach the card: a reading over BUSY_OVER within BUSY_GRACE_S of the stage's start, and an
        average of BUSY_MIN_PCT from there on."""
        b = o1work.number(b)
        if b is not None and (self.busy_n or b > BUSY_OVER):
            self.busy_sum, self.busy_n = self.busy_sum + b, self.busy_n + 1
        el = t - self.stage_at
        if el >= BUSY_GRACE_S and not self.busy_n:
            return "the graphics card never got busy: %s did not reach it" % STAGE_TEXT[self.stage]
        if el >= BUSY_GRACE_S + 60 and self.busy_n >= 10 and self.busy_sum / float(self.busy_n) < BUSY_MIN_PCT:
            return "the graphics card was only %d%% busy during %s" % (self.busy_sum // self.busy_n,
                                                                        STAGE_TEXT[self.stage])
        return None

    def to_stage(self, t, stage, level):
        self.stage, self.stage_at, self.busy_sum, self.busy_n = stage, t, 0, 0
        self.passed, self.last_ok = set(), None
        self.new_block(t, level)

    def fail(self, why):
        self.done, self.error = True, why
        return why

    def finish(self):
        q = self.found["query"]
        b = max(self.found["burn"], q)
        quiet = margin(q)
        self.result = {"quiet_pct": quiet, "heavy_pct": max(quiet, margin(b)), "found": {"query": q, "burn": b}}
        self.done = True

    def step(self, t, temps, busy):
        """One tick. Returns None, or the reason the run must stop (also in `error`)."""
        if self.done:
            return self.error
        for r in temps:
            label, c, limit = r.get("label"), o1work.number(r.get("c")), o1work.number(r.get("limit"))
            if isinstance(label, str) and c is not None and limit is not None:
                self.limits[label] = limit
                self.block.setdefault(label, []).append(c)
        why = self.busy(t, busy)
        if why:
            return self.fail(why)
        el, bl = t - self.stage_at, t - self.block_at
        if self.stage == "short":
            if el >= SHORT_S:
                self.judge(t)
                for label, vals in self.block.items():
                    top = ceiling_for(self.limits[label], max(max(vals), project(vals)))
                    if top is None:
                        return self.fail("even with the fans at 100%% %s reached %.0f C of its %d C limit: no quieter "
                                         "level can keep it under; nothing was changed"
                                         % (label, max(vals), self.limits[label]))
                    self.ceil[label] = top
                self.to_stage(t, "long", down(FULL))
            return None
        block_len = LONG_BLOCK_S if self.stage == "long" else BURN_BLOCK_S
        if bl >= block_len or self.breached(t):
            ok = self.judge(t)
            self.after(ok, t)
            if self.stage == "burn" and ok and el >= BURN_S:
                self.found["burn"] = self.level
                self.finish()
                return None
        if (self.stage == "long" and el >= LONG_S) or (self.stage == "burn" and el >= BURN_MAX_S):
            if t - self.block_at >= MIN_JUDGE_S:
                ok = self.judge(t)
                if not ok:
                    self.after(False, t)
                elif self.level not in self.passed:
                    self.passed.add(self.level)
            level = self.level if self.level in self.passed else up(self.level)
            if self.stage == "long":
                self.found["query"] = level
                self.to_stage(t, "burn", level)
            else:
                self.found["burn"] = level
                self.finish()
        return None

    def left(self, t):
        """Seconds left, roughly (the burn may run up to 5 minutes longer)."""
        el = t - self.stage_at
        rest = {"short": LONG_S + BURN_S, "long": BURN_S, "burn": 0}[self.stage]
        own = {"short": SHORT_S, "long": LONG_S, "burn": BURN_S}[self.stage]
        return int(max(0, own - el) + rest)


# ---- the loads -------------------------------------------------------------------------------------------------------

class QueryLoad:
    """A real model query, over and over (a new one when an answer ends), on its own thread, until stop(). `error`
    is set when Ollama fails three times in a row. Only counts are kept, never the text."""

    def __init__(self, model, base=OLLAMA, timeout=180):
        self.model, self.base, self.timeout = model, base, timeout
        self.stop_ev = threading.Event()
        self.error, self.answers, self.tokens, self.fails = None, 0, 0, 0
        self.conn = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _one(self):
        conn = o1ollama.connect(self.base, timeout=self.timeout)
        self.conn = conn
        try:
            body = {"model": self.model, "prompt": PROMPT, "stream": True, "keep_alive": "10m",
                    "options": {"num_predict": 4096, "temperature": 0.8}}
            conn.request("POST", "/api/generate", body=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            if r.status != 200:
                raise RuntimeError("Ollama answered %d" % r.status)
            while not self.stop_ev.is_set():
                line = r.readline(1 << 20)
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    raise RuntimeError("Ollama: %s" % str(obj["error"])[:80])
                if obj.get("done"):
                    self.answers += 1
                    self.tokens += int(obj.get("eval_count") or 0)
                    break
            self.fails = 0
        finally:
            self.conn = None
            conn.close()

    def _run(self):
        while not self.stop_ev.is_set():
            try:
                self._one()
            except Exception as e:
                if self.stop_ev.is_set():
                    break
                self.fails += 1
                if self.fails >= 3:
                    self.error = "the model query failed: %s" % (str(e)[:100] or type(e).__name__)
                    break
                self.stop_ev.wait(2)

    def poll(self):
        return self.error

    def stop(self):
        self.stop_ev.set()
        c = self.conn
        if c is not None and c.sock is not None:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)        # wakes the read; Ollama ends the answer when we go
            except OSError:
                pass
        self.thread.join(5)


def children(pid, proc="/proc"):
    """Every descendant of `pid` (from /proc/*/stat), for stopping a load whose programs left its process group
    (GNU timeout makes a group of its own)."""
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


class BurnLoad:
    """tools/gpu-burn.sh (llama-bench on the model's file) in a session of its own. poll(): None while it runs, the
    reason when it ended by itself (its own heat stop, an idle card, a hardware error). stop(): the whole tree."""

    def __init__(self, model, script=None, minutes=31, env=None):
        here = os.path.dirname(os.path.realpath(__file__))
        self.script = script or os.path.join(here, "..", "tools", "gpu-burn.sh")
        state = o1common.p("/var/lib/ollama1")
        e = dict(os.environ, XDG_CACHE_HOME=state + "/gpu-cache", __GL_SHADER_DISK_CACHE_PATH=state + "/gpu-cache",
                 O1_GPU_BURN_LOG=burn_log_path(), O1_BURN_REPORT="30")
        e.update(env or {})
        try:
            os.makedirs(state + "/gpu-cache", exist_ok=True)
        except OSError:
            pass
        self.out = open(burn_log_path() + ".out", "w")
        self.p = subprocess.Popen(["bash", self.script, str(minutes), model], stdout=self.out,
                                  stderr=subprocess.STDOUT, env=e, start_new_session=True, close_fds=True)
        self.stopped = False

    def poll(self):
        if self.stopped or self.p.poll() is None:
            return None
        try:
            with open(burn_log_path() + ".out") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            lines = []
        verdict = [ln for ln in lines if ln.startswith(("FAIL", "ABORTED", "PASSED"))]
        last = (verdict or lines or ["it ended with status %d" % self.p.returncode])[-1]
        return "the burn stopped by itself: %s" % last[:150]

    def stop(self):
        self.stopped = True
        if self.p.poll() is None:
            pids = [self.p.pid] + children(self.p.pid)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            try:
                self.p.wait(5)
            except subprocess.TimeoutExpired:
                pass
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            pass
        self.out.close()


# ---- what is around it ------------------------------------------------------------------------------------------------

def model_name(cfg=None):
    """config.json's fan_cal_model, else DEFAULT_MODEL (a name of the shape gpu-burn.sh accepts)."""
    if cfg is None:
        cfg = read_json_safe(o1common.Paths.config, {}, max_bytes=1 << 20)
    m = (cfg or {}).get("fan_cal_model") if isinstance(cfg, dict) else None
    return m if isinstance(m, str) and MODEL_RX.match(m) else DEFAULT_MODEL


def find_bench(environ=None, home_glob="/home/*/llama-bench/*/llama-bench", root_glob="/root/llama-bench/*/llama-bench"):
    """llama-bench where gpu-burn.sh looks for it, or None."""
    env = os.environ if environ is None else environ
    b = env.get("O1_LLAMA_BENCH")
    if b:
        return b if os.access(b, os.X_OK) else None
    for b in sorted(glob.glob(home_glob)) + sorted(glob.glob(root_glob)):
        if os.access(b, os.X_OK):
            return b
    return None


def manifest(model, models=None):
    """The model's manifest file, where gpu-burn.sh reads it."""
    name, _, tag = model.partition(":")
    return os.path.join(models or o1common.Paths.models, "manifests", "registry.ollama.ai", "library", name,
                        tag or "latest")


def installed(model, base=OLLAMA):
    """True / False from Ollama's /api/tags; None when it doesn't answer."""
    try:
        st, data = o1ollama.call(base, "GET", "/api/tags", timeout=10)
    except Exception:
        return None
    if st != 200 or not isinstance(data, dict):
        return None
    names = {m.get("name") for m in data.get("models") or [] if isinstance(m, dict)}
    return model in names or (":" not in model and model + ":latest" in names)


def hw_errors(since, run=subprocess.run):
    """The kinds of hardware-error records in the kernel log since `since` (epoch): [] when none (or no journal)."""
    try:
        r = run(["journalctl", "-k", "-b", "0", "--no-pager", "-q", "--since", "@%d" % int(since)],
                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    kinds = []
    for line in (r.stdout or "").splitlines():
        if HW_RX.search(line):
            k = next((t for pat, t in HW_ERRORS if pat.lower() in line.lower()), "a hardware error")
            if k not in kinds:
                kinds.append(k)
    return kinds


def own_busy(reasons):
    """busy_reasons() without this unit's own entry (it is running while it asks)."""
    mine = dict(o1sleep.BUSY_UNITS).get(UNIT)
    return [r for r in reasons if r != mine]


def refusal(model, fan_status=None, activity=None, busy=None, tools=None, is_installed=None, bench=None,
            gpu_busy=None, now=None, models_dir=None):
    """Why it must not start now, or None. Each argument a callable (the tests fake them)."""
    now = time.time() if now is None else now
    st = (fan_status or o1fan.read_status)()
    if not st:
        return "the fan service isn't running (sudo setup.sh --fans on)"
    if st.get("phase") == "calibrating":
        return "the fan service is still measuring its fans; try again in a minute"
    if st.get("phase") == "hot":
        return "a temperature is at its limit now"
    if not any(isinstance(t, dict) and t.get("label") == "CPU" for t in st.get("temps") or []):
        return "the fan service shows no processor temperature to judge by"
    act = (activity or o1idle.read_activity)(now)
    if act.get("inflight") is None:
        return "couldn't tell whether the server is serving (the gateway isn't reporting)"
    if act["inflight"] > 0:
        return "the server is answering a request"
    if now - (act.get("last") or 0) < QUIET_BEFORE_S:
        return "the server answered a request %d s ago; it starts after %d quiet minutes" % (
            max(0, now - act["last"]), QUIET_BEFORE_S // 60)
    why = own_busy((busy or o1sleep.busy_reasons)())
    if why:
        return "not now: " + "; ".join(why)
    running = (tools or o1idle.tools_running)()
    if running is None:
        return "couldn't look for running tools"
    if running:
        return "%s is running" % sorted(running)[0]
    have = (is_installed or installed)(model)
    if have is None:
        return "Ollama isn't answering"
    if not have or not os.path.exists(manifest(model, models_dir)):
        return "%s isn't installed (sudo ollama1-models add %s, or set fan_cal_model in config.json)" % (model, model)
    if not (bench or find_bench)():
        return "llama-bench isn't installed: the burn needs it in ~/llama-bench/<release>/ (see tools/gpu-burn.sh)"
    if o1work.number((gpu_busy or o1work.Gpu().busy)()) is None:
        return "no reading from the graphics card"
    return None


# ---- the run ----------------------------------------------------------------------------------------------------------

class Runner:
    """One calibration. Every outside thing is an argument, so the tests drive it on a fake clock with fake loads."""

    def __init__(self, model=None, clock=time.monotonic, wall=time.time, wait=None, log=print, fan_status=None,
                 gpu_busy=None, activity=None, errors=None, query=None, burn=None, unload=None, refuse=None,
                 stop=None):
        self.model = model or model_name()
        self.clock, self.wall, self.log = clock, wall, log
        self.stop = stop or threading.Event()
        self.wait = wait or self.stop.wait
        self.fan_status = fan_status or o1fan.read_status
        self.gpu_busy = gpu_busy or o1work.Gpu().busy
        self.activity = activity or o1idle.read_activity
        self.errors = errors or hw_errors
        self.query = query or (lambda m: QueryLoad(m))
        self.burn = burn or (lambda m: BurnLoad(m))
        self.unload = unload or (lambda m: o1ollama.call(OLLAMA, "POST", "/api/generate",
                                                         {"model": m, "keep_alive": 0}, timeout=8))
        self.refuse = refuse if refuse is not None else (lambda: refusal(self.model))
        self.search = Search()
        self.load = None
        self.started = None
        self.samples = []
        self.state, self.reason = "running", ""
        self.why_stop = None

    # -- files ---------------------------------------------------------------------------------------------------------
    def hold(self, pct):
        write_json_atomic(o1fan.tuning_path(), {"at": int(self.wall()), "pct": pct}, mode=0o644)

    def unhold(self):
        try:
            os.unlink(o1fan.tuning_path())
        except OSError:
            pass

    def progress(self, t, temps=None, busy=None):
        s = self.search
        rows = []
        for r in temps or []:
            label = r.get("label")
            if label in s.limits:
                rows.append({"label": label, "c": r.get("c"), "limit": r.get("limit"),
                             "ceiling": round(s.ceiling(label), 1)})
        rows.sort(key=lambda r: r["ceiling"] - (r["c"] or 0))
        st = {"at": int(self.wall()), "state": self.state, "started": self.started, "model": self.model,
              "stage": s.stage, "stage_text": STAGE_TEXT[s.stage], "pct": s.level,
              "stage_left": int(max(0, {"short": SHORT_S, "long": LONG_S, "burn": BURN_S}[s.stage] - (t - s.stage_at))),
              "left": s.left(t) if self.state == "running" else 0, "gpu_busy": o1work.number(busy),
              "temps": rows[:4], "blocks": [{k: b[k] for k in ("stage", "pct", "s", "ok", "worst")} for b in s.blocks[-16:]],
              "found": dict(s.found), "reason": self.reason[:200]}
        if s.result and self.state == "done":
            st["result"] = dict(s.result)
        write_json_atomic(progress_path(), st, mode=0o644)
        return st

    # -- the loop ------------------------------------------------------------------------------------------------------
    def check(self, t, st):
        """Why it must stop now, or None: the safety stops."""
        if self.stop.is_set():
            return self.why_stop or "stopped"
        if not st:
            return "the fan service stopped reporting"
        if st.get("phase") == "hot" or st.get("hot"):
            return "a temperature reached its limit: %s" % "; ".join(st.get("hot") or ["the fans are at 100%"])[:150]
        for r in st.get("temps") or []:
            c, lim = o1work.number(r.get("c")), o1work.number(r.get("limit"))
            if c is not None and lim is not None and c >= lim:
                return "%s reached its %d C limit" % (r.get("label"), lim)
        act = self.activity(self.wall())
        if act.get("inflight"):
            return "a request reached the server: stopped so it is answered at full speed"
        if t - self.errors_at >= ERRORS_EVERY_S:
            self.errors_at = t
            kinds = self.errors(self.started)
            if kinds:
                return "a new hardware-error record in the kernel log: " + ", ".join(kinds)
        if self.load is not None:
            why = self.load.poll()
            if why:
                return why
        return None

    def switch(self, stage):
        """The load each stage needs: the query for short and long (the same one goes on), the burn for burn."""
        if stage == "burn":
            if self.load is not None:
                self.load.stop()
            self.hold(self.search.level)                 # (each step can take seconds: the hold stays fresh)
            try:
                self.unload(self.model)                  # the burn loads its own copy: the card's memory back first
            except Exception:
                pass
            self.hold(self.search.level)
            self.load = self.burn(self.model)
        elif self.load is None:
            self.load = self.query(self.model)

    def run(self):
        self.started = int(self.wall())
        why = self.refuse()
        if why:
            self.state, self.reason = "refused", why
            self.log("refused: " + why)
            self.end(0)
            return 2
        self.log("calibrating the fans with %s: about %d minutes" % (self.model, (SHORT_S + LONG_S + BURN_S) // 60))
        t0 = self.clock()
        self.errors_at = 0.0
        stage = None
        t = 0.0
        last_log = -1e9
        try:
            self.hold(FULL)
            self.switch("short")
            while True:
                t = self.clock() - t0
                st = self.fan_status()
                temps = [r for r in (st or {}).get("temps") or [] if isinstance(r, dict)]
                busy = self.gpu_busy()
                why = self.check(t, st)
                if why:
                    self.state, self.reason = ("aborted" if self.stop.is_set() else "failed"), why
                    break
                why = self.search.step(t, temps, busy)
                if why:
                    self.state, self.reason = "failed", why
                    break
                if self.search.done:
                    self.state = "done"
                    break
                if self.search.stage != stage:
                    if stage is not None:
                        self.log("%s done; now %s at %d%%" % (STAGE_TEXT[stage], STAGE_TEXT[self.search.stage],
                                                               self.search.level))
                    stage = self.search.stage
                    self.switch(stage)
                self.hold(self.search.level)
                if t - last_log >= LOG_EVERY_S:
                    last_log = t
                    self.samples.append({"t": int(t), "stage": stage, "pct": self.search.level,
                                         "busy": o1work.number(busy),
                                         "c": {r["label"]: r.get("c") for r in temps if isinstance(r.get("label"), str)}})
                self.progress(t, temps, busy)
                self.wait(TICK_S)
        finally:
            if self.load is not None:
                self.load.stop()
            self.unhold()
            try:
                self.unload(self.model)
            except Exception:
                pass
        return self.end(t)

    def end(self, t):
        s = self.search
        if self.state == "done":
            res = dict(s.result, v=1, at=int(self.wall()), model=self.model, margin_pct=MARGIN_PCT,
                       margin_c=MARGIN_C, took_s=int(t),
                       ceilings=[{"label": k, "limit": s.limits[k], "ceiling": round(v, 1)} for k, v in s.ceil.items()],
                       blocks=s.blocks[-60:])
            write_json_atomic(result_path(), res, mode=0o644)
            self.log("done: quiet %d%%, heavy %d%% (found %d%% under the query, %d%% under the burn)"
                     % (res["quiet_pct"], res["heavy_pct"], res["found"]["query"], res["found"]["burn"]))
        elif self.state != "refused":
            self.log("%s: %s" % (self.state, self.reason))
        st = self.progress(t)
        write_json_atomic(last_path(), st, mode=0o644)
        if self.samples:
            write_json_atomic(log_path(), {"at": int(self.wall()), "model": self.model, "state": self.state,
                                           "samples": self.samples[-400:]}, mode=0o644)
        try:
            os.unlink(abort_path())
        except OSError:
            pass
        return 0 if self.state == "done" else 1


# ---- reading it back (the panel, `status`) -----------------------------------------------------------------------------

def read_progress(now=None):
    """The progress file; a "running" one not refreshed for 15 s is marked "stalled" (the run died)."""
    st = read_json_safe(progress_path(), None, max_bytes=65536)
    if not isinstance(st, dict) or not isinstance(st.get("at"), int):
        return None
    if st.get("state") == "running" and abs((time.time() if now is None else now) - st["at"]) > 15:
        st = dict(st, state="stalled")
    return st


def read_last():
    st = read_json_safe(last_path(), None, max_bytes=65536)
    return st if isinstance(st, dict) else None


def running(now=None):
    st = read_progress(now)
    return bool(st and st.get("state") == "running")


def stop_post(log=print):
    """ExecStopPost: after any end (a kill too) the hold file goes, and a progress file still saying "running" says
    the run ended without finishing."""
    try:
        os.unlink(o1fan.tuning_path())
    except OSError:
        pass
    st = read_json_safe(progress_path(), None, max_bytes=65536)
    if isinstance(st, dict) and st.get("state") == "running":
        st.update(state="aborted", at=int(time.time()), left=0,
                  reason="stopped from the panel" if os.path.exists(abort_path()) else "the run ended without finishing")
        write_json_atomic(progress_path(), st, mode=0o644)
        write_json_atomic(last_path(), st, mode=0o644)
        log("ended without finishing: " + st["reason"])
    try:
        os.unlink(abort_path())
    except OSError:
        pass
    return 0


def request_abort(systemctl=None):
    """The panel's Abort (ollama1-fancal-abort.service, root): a mark that says who stopped it, then a stop of the
    unit (SIGTERM, then the whole control group)."""
    try:
        write_json_atomic(abort_path(), {"at": int(time.time())}, mode=0o644)
    except OSError:
        pass
    run = systemctl or (lambda: subprocess.run(["systemctl", "stop", UNIT], timeout=60).returncode)
    return run()


def status_text(now=None):
    """`ollama1-fancal status`."""
    out = []
    cal = o1work.read_calibration()
    lv = o1work.levels()
    if cal:
        out.append("calibrated %s: quiet %d%%, heavy %d%% (with %s; found %s%% under the query, %s%% under the burn)"
                   % (time.strftime("%Y-%m-%d %H:%M", time.localtime(cal["at"])), cal["quiet_pct"], cal["heavy_pct"],
                      cal.get("model", "?"), (cal.get("found") or {}).get("query", "?"),
                      (cal.get("found") or {}).get("burn", "?")))
    else:
        out.append("not calibrated")
    out.append("in force: quiet %d%%, heavy %d%% (%s)" % (lv["quiet"], lv["heavy"], lv["source"]))
    st = read_progress(now) or read_last()
    if st:
        line = "last run: %s" % st.get("state")
        if st.get("state") == "running":
            line += ", %s at %s%%, about %d min left" % (st.get("stage_text"), st.get("pct"), (st.get("left") or 0) // 60)
        if st.get("reason"):
            line += ": " + st["reason"]
        out.append(line)
    return "\n".join(out)


def main(argv, log=print):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        print(status_text())
        return 0
    if os.geteuid() != 0 and not os.environ.get("OLLAMA1_PREFIX"):
        log("run it as root (it is started from the admin panel: ollama1-fancal.service)")
        return 1
    if cmd == "abort":
        return request_abort()
    if cmd == "stop-post":
        return stop_post(log)
    if cmd != "run":
        log("usage: ollama1-fancal status|run|abort|stop-post")
        return 2
    r = Runner(log=log)

    def term(*_):
        r.why_stop = "stopped from the panel" if os.path.exists(abort_path()) else "stopped by the system"
        r.stop.set()
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    try:
        os.unlink(abort_path())
    except OSError:
        pass
    with o1sleep.Inhibit("the fans are being calibrated"):
        return r.run()
