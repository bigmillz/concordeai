"""Power and electricity cost for ollama1.

Where the watts come from, best first:
  1. a smart plug on the LAN (Shelly Gen1 /status, Shelly Gen2+
     /rpc/Switch.GetStatus, TP-Link Kasa's local protocol on port 9999,
     Tasmota "Status 8"). Its address must be a private LAN IP.
  2. an estimate: GPU power (amdgpu hwmon, or nvidia-smi) + CPU package
     power (RAPL energy counter, wraparound handled) + a 40 W baseline for
     the rest of the machine, divided by 90% PSU efficiency. Always labelled
     "estimate".
While the desktop sleeps it counts 3 W (from the sleep record). Any other
gap is unknown time: never counted as zero.

The root sampler (ollama1-power.service, every 10 s) keeps per-minute
energy under /var/lib/ollama1/energy/ (m-YYYY-MM-DD.csv by UTC day:
minute, Wh, source, seconds measured, tokens generated; a year and a month
kept, with daily totals kept for good in days.csv), the live reading in
/run/ollama1/power/now.json, and a summary for the dashboard. Nothing it
stores says anything about a request beyond the token count.
"""
import datetime
import glob
import ipaddress
import json
import os
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import o1stats
import o1tariff
from o1common import Paths, read_json, write_json_atomic

PLUG_TYPES = {
    "shelly1": "Shelly Gen1 (e.g. Plug S): GET /status",
    "shelly2": "Shelly Gen2 or later (Plus/Pro/Gen3): GET /rpc/Switch.GetStatus?id=0",
    "kasa": "TP-Link Kasa with energy monitoring (HS110, KP115...): local protocol, TCP 9999",
    "tasmota": "Tasmota: GET /cm?cmnd=Status 8",
}
MAX_PLUG_W = 5000
GAP_S = 30          # a longer gap between two readings is unknown time
TICK_S = 10
KEEP_DAYS = 400
SERIES_N = 360      # an hour of 10 s readings


def energy_dir():
    return os.path.join(Paths.state, "energy")


def run_dir():
    return os.path.join(Paths.run, "power")


def plug_file():
    return os.path.join(Paths.etc, "power-plug.json")


def tariff_file():
    """The price schedule. The panel saves it (as o1admin, so not in the
    root-only config.json: it isn't secret); root's CLI writes it too."""
    return os.path.join(Paths.admin_state, "tariff.json")


def load_schedule():
    s = read_json(tariff_file())
    if isinstance(s, dict):
        clean, errs = o1tariff.validate(s)
        if not errs:
            return clean
    return json.loads(json.dumps(o1tariff.DEFAULT))


def save_schedule(s, owner=None):
    clean, errs = o1tariff.validate(s)
    if errs:
        raise ValueError("; ".join(errs))
    os.makedirs(os.path.dirname(tariff_file()), exist_ok=True)
    write_json_atomic(tariff_file(), clean, mode=0o600)
    if owner:
        import pwd
        try:
            pw = pwd.getpwnam(owner)
            os.chown(tariff_file(), pw.pw_uid, pw.pw_gid)
        except (KeyError, PermissionError):
            pass
    return clean


# ---- smart plugs -----------------------------------------------------------------

class PlugError(Exception):
    pass


def lan_ip(host):
    """The host as a private LAN address, or PlugError. Only an IP literal:
    a name could resolve anywhere."""
    try:
        ip = ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        raise PlugError("the plug's address must be its LAN IP, e.g. 192.168.86.40")
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or not (ip.is_private or ip.is_link_local):
        raise PlugError("%s isn't a private LAN address" % ip)
    return str(ip)


def validate_plug(cfg):
    if not isinstance(cfg, dict) or cfg.get("type") not in PLUG_TYPES:
        raise PlugError("plug type must be one of: " + ", ".join(PLUG_TYPES))
    for k in cfg:
        if k not in ("type", "host", "user", "password"):
            raise PlugError("unknown plug setting %r" % k)
    out = {"type": cfg["type"], "host": lan_ip(cfg.get("host"))}
    if cfg.get("user") or cfg.get("password"):
        if cfg["type"] == "kasa":
            raise PlugError("Kasa's local protocol has no login")
        out["user"] = str(cfg.get("user") or "admin")
        out["password"] = str(cfg.get("password") or "")
    return out


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PlugError("the plug's reply has no power figure")
    if not 0 <= v <= MAX_PLUG_W:
        raise PlugError("the plug reported %r W" % v)
    return float(v)


def parse_shelly1(obj):
    meters = (obj or {}).get("meters") or (obj or {}).get("emeters")
    if not isinstance(meters, list) or not meters:
        raise PlugError("no meters in the Shelly reply")
    return _num(sum(_num(m.get("power")) for m in meters if isinstance(m, dict)))


def parse_shelly2(obj):
    return _num((obj or {}).get("apower"))


def parse_tasmota(obj):
    try:
        p = obj["StatusSNS"]["ENERGY"]["Power"]
    except (KeyError, TypeError):
        raise PlugError("no StatusSNS.ENERGY.Power in the Tasmota reply")
    return _num(sum(p) if isinstance(p, list) else p)


def parse_kasa(obj):
    try:
        rt = obj["emeter"]["get_realtime"]
    except (KeyError, TypeError):
        raise PlugError("no emeter reading in the Kasa reply")
    if rt.get("err_code"):
        raise PlugError("the Kasa plug answered error %r" % rt.get("err_code"))
    if "power_mw" in rt:           # newer hardware versions report milliwatts
        mw = rt["power_mw"]
        if isinstance(mw, bool) or not isinstance(mw, (int, float)):
            raise PlugError("the Kasa reply has no power figure")
        return _num(mw / 1000.0)
    return _num(rt.get("power"))


def kasa_encrypt(raw):
    key, out = 171, bytearray()
    for b in raw:
        key ^= b
        out.append(key)
    return bytes(out)


def kasa_decrypt(data):
    key, out = 171, bytearray()
    for b in data:
        out.append(key ^ b)
        key = b
    return bytes(out)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _http_json(url, user=None, password=None, timeout=2.0):
    handlers = [_NoRedirect()]
    if user is not None:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, user, password or "")
        handlers += [urllib.request.HTTPBasicAuthHandler(mgr), urllib.request.HTTPDigestAuthHandler(mgr)]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "ollama1-power"}), timeout=timeout) as r:
            return json.loads(r.read(1 << 16).decode("utf-8"))
    except urllib.error.HTTPError as e:
        e.close()
        raise PlugError("the plug answered HTTP %d%s" % (e.code, " (check the login)" if e.code == 401 else ""))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PlugError("the plug didn't answer: %s" % (getattr(e, "reason", None) or e))


def _kasa_query(host, port=9999, timeout=2.0):
    msg = json.dumps({"emeter": {"get_realtime": {}}}).encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(struct.pack(">I", len(msg)) + kasa_encrypt(msg))
            head = b""
            while len(head) < 4:
                chunk = s.recv(4 - len(head))
                if not chunk:
                    raise PlugError("the Kasa plug closed the connection")
                head += chunk
            n = struct.unpack(">I", head)[0]
            if n > 1 << 16:
                raise PlugError("the Kasa reply is too long")
            data = b""
            while len(data) < n:
                chunk = s.recv(n - len(data))
                if not chunk:
                    break
                data += chunk
    except OSError as e:
        raise PlugError("the Kasa plug didn't answer: %s" % e)
    try:
        return json.loads(kasa_decrypt(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PlugError("the Kasa reply isn't readable (newer Kasa firmware uses an encrypted protocol "
                        "this doesn't speak)")


def read_plug(cfg, timeout=2.0, http_port=80, kasa_port=9999, check=lan_ip):
    """Watts from a validated plug config. Raises PlugError. (Tests pass
    their own `check` and ports to reach fake plugs on 127.0.0.1.)"""
    host = check(cfg["host"])
    h = "[%s]" % host if ":" in host else host
    base = "http://%s:%d" % (h, http_port)
    user, pw = cfg.get("user"), cfg.get("password")
    t = cfg["type"]
    if t == "shelly1":
        return parse_shelly1(_http_json(base + "/status", user, pw, timeout))
    if t == "shelly2":
        return parse_shelly2(_http_json(base + "/rpc/Switch.GetStatus?id=0", user, pw, timeout))
    if t == "tasmota":
        q = {"cmnd": "Status 8"}
        if user is not None:
            q.update(user=user, password=pw or "")   # Tasmota's own API takes the login this way
        return parse_tasmota(_http_json(base + "/cm?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote),
                                        timeout=timeout))
    if t == "kasa":
        return parse_kasa(_kasa_query(host, kasa_port, timeout))
    raise PlugError("unknown plug type")


# ---- the estimate ------------------------------------------------------------------

class Rapl:
    """CPU package power from the RAPL energy counter (root only)."""

    def __init__(self, base=None, clock=time.monotonic):
        self.base = base or (o1stats.SYS + "/class/powercap/intel-rapl:0")
        self.clock = clock
        self.prev = None
        try:
            with open(self.base + "/max_energy_range_uj") as f:
                self.range = int(f.read())
        except (OSError, ValueError):
            self.range = None

    def read(self):
        try:
            with open(self.base + "/energy_uj") as f:
                return int(f.read())
        except (OSError, ValueError):
            return None

    def watts(self):
        e, t = self.read(), self.clock()
        prev, self.prev = self.prev, (e, t) if e is not None else None
        if e is None or prev is None or t <= prev[1]:
            return None
        return round(delta_uj(prev[0], e, self.range) / 1e6 / (t - prev[1]), 1)


def delta_uj(e0, e1, rng):
    """Energy between two counter readings; the counter wraps at rng."""
    if e1 >= e0:
        return e1 - e0
    if not rng:
        return 0
    return (rng - e0) + e1 + 1


def nvidia_power():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        return round(sum(float(x) for x in r.stdout.split() if x.strip()), 1) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def gpu_power():
    g = o1stats.gpu()
    if g and g.get("power_w") is not None:
        return g["power_w"]
    return nvidia_power()


def estimate(gpu_w, cpu_w, baseline_w=40.0, psu_eff=0.9):
    """Wall watts from the parts: (GPU + CPU + the rest) / PSU efficiency."""
    dc = (gpu_w or 0.0) + (cpu_w or 0.0) + baseline_w
    return round(dc / max(0.5, min(1.0, psu_eff)), 1)


# ---- per-minute energy -------------------------------------------------------------

class Meter:
    """Energy by minute: add(t0, t1, W) splits an interval at minute
    boundaries. Seconds without a reading stay unknown."""

    def __init__(self):
        self.buckets = {}

    def add(self, t0, t1, watts, src):
        if watts is None or t1 <= t0:
            return
        t = t0
        while t < t1:
            m = int(t // 60) * 60
            e = min(t1, m + 60)
            b = self.buckets.setdefault(m, {"wh": 0.0, "known": 0.0, "src": {}, "tokens": 0})
            b["wh"] += watts * (e - t) / 3600.0
            b["known"] += e - t
            b["src"][src] = b["src"].get(src, 0.0) + (e - t)
            t = e

    def tokens(self, t, n):
        if n > 0:
            m = int(t // 60) * 60
            b = self.buckets.setdefault(m, {"wh": 0.0, "known": 0.0, "src": {}, "tokens": 0})
            b["tokens"] += int(n)

    def take_complete(self, now):
        """Rows for minutes that have ended: (t, wh, src, known_s, tokens)."""
        cut = int(now // 60) * 60
        rows = []
        for m in sorted(k for k in self.buckets if k < cut):
            b = self.buckets.pop(m)
            src = max(b["src"].items(), key=lambda kv: kv[1])[0] if b["src"] else "none"
            rows.append((m, round(b["wh"], 5), src, int(round(min(60.0, b["known"]))), b["tokens"]))
        return rows


def _day_file(t):
    return os.path.join(energy_dir(), "m-%s.csv" % datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date())


def append_rows(rows):
    os.makedirs(energy_dir(), exist_ok=True)
    by_file = {}
    for r in rows:
        by_file.setdefault(_day_file(r[0]), []).append(r)
    for path, rs in by_file.items():
        new = not os.path.exists(path)
        with open(path, "a") as f:
            if new:
                os.fchmod(f.fileno(), 0o640)
            for t, wh, src, known, tok in rs:
                f.write("%d,%.5f,%s,%d,%d\n" % (t, wh, src, known, tok))


def read_rows(t0, t1):
    """Stored minutes with t0 <= t < t1, oldest first."""
    out = []
    d = datetime.datetime.fromtimestamp(t0, datetime.timezone.utc).date()
    end = datetime.datetime.fromtimestamp(max(t0, t1 - 1), datetime.timezone.utc).date()
    while d <= end:
        path = os.path.join(energy_dir(), "m-%s.csv" % d)
        try:
            with open(path) as f:
                for line in f:
                    p = line.strip().split(",")
                    if len(p) != 5:
                        continue
                    try:
                        t = int(p[0])
                        if t0 <= t < t1:
                            out.append((t, float(p[1]), p[2], int(p[3]), int(p[4])))
                    except ValueError:
                        continue
        except OSError:
            pass
        d += datetime.timedelta(days=1)
    out.sort()
    # a minute written twice (a restart inside it) counts once, the fuller one
    dedup = {}
    for r in out:
        if r[0] not in dedup or r[3] > dedup[r[0]][3]:
            dedup[r[0]] = r
    return [dedup[k] for k in sorted(dedup)]


def first_row_time():
    files = sorted(glob.glob(os.path.join(energy_dir(), "m-*.csv")))
    for path in files:
        try:
            with open(path) as f:
                line = f.readline()
            return int(line.split(",")[0])
        except (OSError, ValueError, IndexError):
            continue
    return None


def prune(now, keep_days=KEEP_DAYS):
    """Old minute files go, after their day's total is in days.csv."""
    cut = (datetime.datetime.fromtimestamp(now, datetime.timezone.utc) - datetime.timedelta(days=keep_days)).date()
    days_path = os.path.join(energy_dir(), "days.csv")
    have = set()
    try:
        with open(days_path) as f:
            have = {line.split(",")[0] for line in f}
    except OSError:
        pass
    for path in sorted(glob.glob(os.path.join(energy_dir(), "m-*.csv"))):
        day = os.path.basename(path)[2:12]
        try:
            d = datetime.date.fromisoformat(day)
        except ValueError:
            continue
        if d >= cut:
            continue
        if day not in have:
            t0 = int(datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp())
            rows = read_rows(t0, t0 + 86400)
            with open(days_path, "a") as f:
                f.write("%s,%.3f,%d,%d\n" % (day, sum(r[1] for r in rows), sum(r[3] for r in rows),
                                             sum(r[4] for r in rows)))
        os.unlink(path)


# ---- money ---------------------------------------------------------------------------

def price_rows(tariff, rows):
    """[(row, tier, price)] with each minute priced at its own tier."""
    tiers = tariff.tiers_for_minutes([r[0] for r in rows])
    return [(r, tier, tariff.price(tier)) for r, tier in zip(rows, tiers)]


def window(tariff, rows, t0, t1, first=None):
    """kWh, cost (by tier), measured and unknown hours, tokens, cost per 1k
    tokens, over [t0, t1)."""
    rs = [r for r in rows if t0 <= r[0] < t1]
    by = {}
    kwh = cost = 0.0
    known = tokens = 0
    priced = tariff.has_prices()
    src = {}
    for r, tier, p in price_rows(tariff, rs):
        k = r[1] / 1000.0
        kwh += k
        known += r[3]
        tokens += r[4]
        src[r[2]] = src.get(r[2], 0) + r[3]
        b = by.setdefault(tier, {"kwh": 0.0, "cost": 0.0 if priced else None})
        b["kwh"] += k
        if priced and p is not None:
            c = k * p
            b["cost"] += c
            cost += c
    start = max(t0, first) if first else t0
    span = max(0, t1 - start)
    out = {"kwh": round(kwh, 4), "cost": round(cost, 4) if priced else None,
           "by_tier": {t: {"kwh": round(v["kwh"], 4), "cost": None if v["cost"] is None else round(v["cost"], 4)}
                       for t, v in by.items()},
           "measured_h": round(known / 3600.0, 2), "unknown_h": round(max(0, span - known) / 3600.0, 2),
           "tokens": tokens, "sources_h": {k: round(v / 3600.0, 2) for k, v in src.items()}}
    out["cost_per_1k_tokens"] = round(cost / tokens * 1000, 5) if priced and tokens else None
    return out


def projection(tariff, rows, now, days=30, basis_days=28):
    """The next `days` days, from the average power in each hour of the week
    (local time) over the last `basis_days`, priced minute by minute."""
    rs = [r for r in rows if now - basis_days * 86400 <= r[0] < now and r[3] > 0]
    if not rs:
        return None
    slot_wh, slot_s = [0.0] * 168, [0] * 168
    for r in rs:
        lt = tariff.local(r[0])
        k = lt.weekday() * 24 + lt.hour
        slot_wh[k] += r[1]
        slot_s[k] += r[3]
    all_w = sum(slot_wh) * 3600.0 / max(1, sum(slot_s))
    slot_w = [slot_wh[k] * 3600.0 / slot_s[k] if slot_s[k] >= 600 else all_w for k in range(168)]
    start = int(now) - int(now) % 60 + 60
    minutes = [start + 60 * i for i in range(days * 1440)]
    tiers = tariff.tiers_for_minutes(minutes)
    by, kwh, cost = {}, 0.0, 0.0
    priced = tariff.has_prices()
    cache_h, slot = None, None
    for t, tier in zip(minutes, tiers):
        h = t - t % 1800
        if h != cache_h:
            cache_h = h
            lt = tariff.local(t)
            slot = lt.weekday() * 24 + lt.hour
        k = slot_w[slot] / 60.0 / 1000.0
        kwh += k
        b = by.setdefault(tier, {"kwh": 0.0, "cost": 0.0})
        b["kwh"] += k
        p = tariff.price(tier)
        if priced and p is not None:
            b["cost"] += k * p
            cost += k * p
    measured_days = round(sum(r[3] for r in rs) / 86400.0, 1)
    return {"days": days, "kwh": round(kwh, 2), "cost": round(cost, 2) if priced else None,
            "avg_w": round(all_w, 1), "basis_days": measured_days,
            "by_tier": {t: {"kwh": round(v["kwh"], 2), "cost": round(v["cost"], 2) if priced else None}
                        for t, v in by.items()}}


WINDOWS = (("1h", 3600), ("1d", 86400), ("1w", 7 * 86400), ("1m", 30 * 86400))


def summary(now=None, schedule=None, live=None):
    """Everything the panel shows (and a subset for the dashboard)."""
    now = now or time.time()
    schedule = schedule or load_schedule()
    tariff = o1tariff.Tariff(schedule)
    cut = int(now) - int(now) % 60          # whole minutes only: the one in progress isn't stored yet
    rows = read_rows(cut - 30 * 86400, cut)
    first = rows[0][0] if rows else None
    out = {"time": int(now), "schedule_id": o1tariff.schedule_id(schedule),
           "currency": schedule["currency"],
           "symbol": o1tariff.CURRENCY_SYMBOL.get(schedule["currency"], schedule["currency"] + " "),
           "mode": schedule["mode"], "priced": tariff.has_prices(),
           "badge": tariff.badge(now), "since": first,
           "windows": {k: window(tariff, rows, cut - s, cut, first) for k, s in WINDOWS},
           "projection": projection(tariff, rows, now)}
    if live is None:
        live = read_json(os.path.join(run_dir(), "now.json"))
    out["live"] = live if isinstance(live, dict) and now - live.get("t", 0) < 60 else None
    return out


def compact(s):
    """The dashboard's line: watts, source, the last 24 h, the tier now."""
    live = s.get("live") or {}
    d = s["windows"]["1d"]
    return {"t": s["time"], "watts": live.get("watts"), "src": live.get("src"),
            "kwh_24h": d["kwh"], "cost_24h": d["cost"], "symbol": s["symbol"],
            "badge": s["badge"]["text"] if s["mode"] == "tou" else None,
            "price": s["badge"]["price"]}


# ---- the sampler (root) ----------------------------------------------------------------

class Sampler:
    def __init__(self, cfg, clock=time.time, plug=None, rapl=None, gpu=gpu_power, sleep_rec=None,
                 tokens=None):
        self.cfg = cfg
        self.clock = clock
        self.plug = plug
        self.rapl = rapl or Rapl()
        self.gpu = gpu
        self.sleep_rec = sleep_rec or (lambda: {})
        self.tokens_fn = tokens or (lambda: ((o1stats.gateway_snapshot() or {}).get("totals") or {}).get("tokens"))
        self.meter = Meter()
        self.prev = None          # (t, watts, src)
        self.prev_tokens = None
        self.series = []
        self.plug_state = {"configured": bool(plug)}

    def measure(self):
        gpu_w, cpu_w = self.gpu(), self.rapl.watts()
        est = estimate(gpu_w, cpu_w, float(self.cfg.get("power_baseline_w", 40)),
                       float(self.cfg.get("power_psu_efficiency", 0.9)))
        parts = {"gpu_w": gpu_w, "cpu_w": cpu_w, "estimate_w": est,
                 "baseline_w": float(self.cfg.get("power_baseline_w", 40)),
                 "psu_efficiency": float(self.cfg.get("power_psu_efficiency", 0.9))}
        if self.plug:
            try:
                w = read_plug(self.plug)
                self.plug_state = {"configured": True, "ok": True, "type": self.plug["type"]}
                return w, "plug", parts
            except PlugError as e:
                self.plug_state = {"configured": True, "ok": False, "type": self.plug["type"], "error": str(e)[:160]}
        return est, "est", parts

    def tick(self):
        now = self.clock()
        watts, src, parts = self.measure()
        if self.prev:
            t0, w0, s0 = self.prev
            if now - t0 <= GAP_S:
                self.meter.add(t0, now, (w0 + watts) / 2.0 if s0 == src else watts, src)
            else:
                self.fill_sleep(t0, now)
        self.prev = (now, watts, src)
        tok = self.tokens_fn()
        if isinstance(tok, int):
            if self.prev_tokens is not None and tok >= self.prev_tokens:
                self.meter.tokens(now, tok - self.prev_tokens)
            self.prev_tokens = tok
        self.series = (self.series + [[int(now), watts]])[-SERIES_N:]
        live = dict(parts, t=int(now), watts=watts, src=src, plug=self.plug_state, series=self.series)
        return live

    def fill_sleep(self, t0, t1):
        """A gap covered by the sleep record counts at the sleep wattage."""
        rec = self.sleep_rec() or {}
        a, b = rec.get("last_sleep"), rec.get("last_wake")
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float)) and a < b):
            return
        if a < t0 - 2 * GAP_S or b > t1 + GAP_S:
            return
        self.meter.add(max(t0, a), min(t1, b), float(self.cfg.get("power_sleep_w", 3)), "sleep")


def run(cfg, stop=None):
    """The service loop."""
    import o1sleep
    plug = None
    p = read_json(plug_file())
    if isinstance(p, dict):
        try:
            plug = validate_plug(p)
        except PlugError as e:
            print("power: plug config ignored: %s" % e, flush=True)
    os.makedirs(run_dir(), exist_ok=True)
    s = Sampler(cfg, plug=plug, sleep_rec=o1sleep.last)
    last_flush = 0.0
    while not (stop and stop.is_set()):
        live = s.tick()
        write_json_atomic(os.path.join(run_dir(), "now.json"), live, mode=0o640, group="o1view")
        now = time.time()
        if now - last_flush >= 60:
            last_flush = now
            rows = s.meter.take_complete(now)
            if rows:
                append_rows(rows)
            try:
                write_json_atomic(os.path.join(run_dir(), "summary.json"), compact(summary(now, live=live)),
                                  mode=0o640, group="o1view")
            except (ValueError, OSError) as e:
                print("power: summary failed: %s" % e, flush=True)
            prune(now)
        time.sleep(TICK_S - (time.time() % TICK_S))
