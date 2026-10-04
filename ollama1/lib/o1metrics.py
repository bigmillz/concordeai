"""The dashboard's sampler: one tick a second, straight from /proc and
/sys (plus the gateway's counts-only snapshot file), with an hour of
history for the charts. Slow sources (the tunnel, the network address,
timers) are refreshed every 10-60 s. Nothing here sees a prompt or an
answer: the gateway's snapshot has none, and nothing else is read.
"""
import glob
import json
import os
import re
import time
import urllib.request
from collections import deque

import o1stats
from o1common import Paths, read_json
from o1stats import PROC, SYS

HISTORY = 3600
SERIES = ("tps", "gpu_busy", "vram_used", "gpu_power", "ram_used", "io_read", "io_write",
          "net_rx", "net_tx", "cpu_total", "active",
          "gpu_temp", "cpu_temp", "cpu_mhz", "fan_rpm")      # the last four: the graphical panel (6b380)
DISK_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+)$")


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _int(path):
    v = _read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_proc_stat(text):
    """{"cpu": (busy, total), "cpu0": ..., ...} in jiffies."""
    out = {}
    for line in (text or "").splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        try:
            vals = [int(x) for x in parts[1:9]]
        except ValueError:
            continue
        if len(vals) < 4:
            continue
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        out[parts[0]] = (total - idle, total)
    return out


def cpu_percent(prev, cur):
    """(total %, [per-core %]) between two parse_proc_stat() results."""
    def pct(k):
        if k not in prev or k not in cur:
            return None
        db, dt = cur[k][0] - prev[k][0], cur[k][1] - prev[k][1]
        return round(100.0 * db / dt, 1) if dt > 0 else 0.0
    cores = sorted((k for k in cur if k != "cpu"), key=lambda k: int(k[3:]))
    return pct("cpu"), [pct(k) for k in cores]


def parse_diskstats(text):
    """Whole disks only (no partitions, dm or md, so nothing counts twice):
    total bytes read and written."""
    rd = wr = 0
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) < 10 or not DISK_RE.match(p[2]):
            continue
        try:
            rd += int(p[5]) * 512
            wr += int(p[9]) * 512
        except ValueError:
            continue
    return rd, wr


def parse_rtt(metrics_text):
    """cloudflared's Prometheus metrics -> the mean smoothed QUIC RTT (ms)."""
    vals = []
    for line in (metrics_text or "").splitlines():
        if line.startswith("quic_client_smoothed_rtt") or (not vals and line.startswith("quic_client_latest_rtt")):
            try:
                vals.append(float(line.rsplit(None, 1)[1]))
            except (ValueError, IndexError):
                pass
    return round(sum(vals) / len(vals), 1) if vals else None


def cpu_temp():
    """k10temp's Tctl (or zenpower / coretemp), in degrees C."""
    for h in glob.glob(SYS + "/class/hwmon/hwmon*"):
        name = _read(h + "/name")
        if name in ("k10temp", "zenpower", "coretemp"):
            for lab in sorted(glob.glob(h + "/temp*_label")):
                if _read(lab) in ("Tctl", "Tdie", "Package id 0"):
                    v = _int(lab.replace("_label", "_input"))
                    if v is not None:
                        return round(v / 1000.0, 1)
            v = _int(h + "/temp1_input")
            if v is not None:
                return round(v / 1000.0, 1)
    return None


def gpu_clocks(card_dev):
    """sclk and mclk in MHz from amdgpu hwmon (freqN_label / freqN_input)."""
    out = {}
    for lab in glob.glob((card_dev or "") + "/hwmon/hwmon*/freq*_label"):
        name = _read(lab)
        v = _int(lab.replace("_label", "_input"))
        if name in ("sclk", "mclk") and v:
            out[name + "_mhz"] = round(v / 1e6)
    return out


def ollama_cgroup():
    base = SYS + "/fs/cgroup/system.slice/ollama.service"
    cur = _int(base + "/memory.current")
    mx = _read(base + "/memory.max")
    return {"current": cur, "max": int(mx) if mx and mx.isdigit() else None}


class Sampler:
    def __init__(self, history=HISTORY, clock=time.time, tunnel_port=8439):
        self.clock = clock
        self.series = {k: deque(maxlen=history) for k in SERIES}
        self.prev = None
        self.slow = {}
        self.tunnel_port = tunnel_port

    def _slow(self, key, every, fn):
        now = self.clock()
        t, v = self.slow.get(key, (0, None))
        if now - t >= every or key not in self.slow:
            try:
                v = fn()
            except Exception:
                v = None
            self.slow[key] = (now, v)
        return v

    def _tunnel(self):
        t = o1stats.tunnel(self.tunnel_port)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/metrics" % self.tunnel_port, timeout=1.5) as r:
                t["rtt_ms"] = parse_rtt(r.read(1 << 20).decode("utf-8", "replace"))
        except Exception:
            t["rtt_ms"] = None
        return t

    def tick(self):
        now = self.clock()
        stat = parse_proc_stat(_read(PROC + "/stat", ""))
        disk = parse_diskstats(_read(PROC + "/diskstats", ""))
        net = self._slow("net", 15, o1stats.network) or {}
        nname = net.get("name") or "br0"
        rx, tx = _int(SYS + "/class/net/%s/statistics/rx_bytes" % nname), _int(SYS + "/class/net/%s/statistics/tx_bytes" % nname)
        cur = {"t": now, "stat": stat, "disk": disk, "net": (rx or 0, tx or 0)}
        cpu_total, cores = (None, [])
        io = {"read_bps": None, "write_bps": None}
        netrate = {"rx_bps": None, "tx_bps": None}
        if self.prev:
            dt = max(1e-3, now - self.prev["t"])
            cpu_total, cores = cpu_percent(self.prev["stat"], stat)
            io = {"read_bps": max(0, disk[0] - self.prev["disk"][0]) / dt,
                  "write_bps": max(0, disk[1] - self.prev["disk"][1]) / dt}
            netrate = {"rx_bps": max(0, cur["net"][0] - self.prev["net"][0]) / dt,
                       "tx_bps": max(0, cur["net"][1] - self.prev["net"][1]) / dt}
        self.prev = cur
        g = o1stats.gpu()
        if g:
            g.update(gpu_clocks(o1stats.gpu_card()))
        mi = o1stats.meminfo()
        mem = {"total": mi.get("MemTotal"), "available": mi.get("MemAvailable"),
               "swap_total": mi.get("SwapTotal"), "swap_free": mi.get("SwapFree")}
        gw = o1stats.gateway_snapshot()
        st = {
            "time": now,
            "host": os.uname().nodename,
            "uptime": o1stats.uptime(),
            "gw": gw,
            "gpu": g,
            "cpu": dict({"total": cpu_total, "cores": cores, "temp": self._slow("ctemp", 2, cpu_temp),
                         "load": o1stats.loadavg()}, **(self._slow("cclock", 2, cpu_clock) or {})),
            "mem": mem,
            "ollama_cg": ollama_cgroup(),
            "disks": self._slow("disks", 10, o1stats.disks) or [],
            "raid": self._slow("raid", 5, o1stats.raid) or [],
            "io": io,
            "net": dict(net, **netrate),
            "tunnel": self._slow("tunnel", 10, self._tunnel) or {},
            "updates": self._slow("updates", 30, o1stats.updates) or {},
            "pairing": o1stats.pairing_window(),
            "power": self._slow("power", 5, power_state),
            "fan": read_json(os.path.join(Paths.run, "fan.json")),          # the fan service's status (6b416)
            "activity": read_json(os.path.join(Paths.stats_dir, "activity.json")),
            "idle": self._slow("idle", 5, lambda: read_json(os.path.join(Paths.run, "idle.json"))),
        }
        tot = mem.get("total") or 0
        vals = {
            "tps": ((gw or {}).get("tps") or {}).get("current"),
            "gpu_busy": (g or {}).get("busy_pct"),
            "vram_used": (g or {}).get("vram_used"),
            "gpu_power": (g or {}).get("power_w"),
            "ram_used": (tot - (mem.get("available") or 0)) if tot else None,
            "io_read": io["read_bps"], "io_write": io["write_bps"],
            "net_rx": netrate["rx_bps"], "net_tx": netrate["tx_bps"],
            "cpu_total": cpu_total,
            "active": (gw or {}).get("active"),
            "gpu_temp": ((g or {}).get("temps") or {}).get("junction") or ((g or {}).get("temps") or {}).get("edge"),
            "cpu_temp": st["cpu"]["temp"],
            "cpu_mhz": st["cpu"].get("mhz"),
            "fan_rpm": (g or {}).get("fan_rpm"),
        }
        for k, v in vals.items():
            self.series[k].append(v)
        st["series"] = {k: list(v) for k, v in self.series.items()}
        return st


def cpu_clock(probe=[None]):
    """{"mhz": average, "max_mhz": the top speed} from cpufreq (o1cpu), or
    None where there is none (a virtual machine). The probe is made once."""
    import o1cpu
    if probe[0] is None:
        probe[0] = o1cpu.CpuProbe()
    dirs, _n = probe[0].cpu_dirs()
    f = probe[0].frequency(dirs)
    if f.get("avg_mhz") is None:
        return None
    return {"mhz": f["avg_mhz"], "max_mhz": f.get("max_mhz") or f.get("rated_mhz")}


def power_state():
    """The power sampler's live watts and its once-a-minute summary (kWh,
    cost and the price tier now). Counts only."""
    live = read_json(os.path.join(Paths.run, "power", "now.json")) or {}
    summ = read_json(os.path.join(Paths.run, "power", "summary.json")) or {}
    if not live and not summ:
        return None
    fresh = time.time() - (live.get("t") or 0) < 60
    return {"watts": live.get("watts") if fresh else None, "src": live.get("src") if fresh else None,
            "kwh_24h": summ.get("kwh_24h"), "cost_24h": summ.get("cost_24h"), "symbol": summ.get("symbol", "$"),
            "badge": summ.get("badge"), "price": summ.get("price"),
            "currency": summ.get("currency"), "priced": summ.get("priced"), "since": summ.get("since"),
            "windows": summ.get("windows") if isinstance(summ.get("windows"), dict) else None}


def font_state(path="/run/ollama1/dash-font.json"):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
