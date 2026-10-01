"""System figures for the console dashboard, ollama1-top and the admin
panel: the GPU (amdgpu sysfs/hwmon), CPU, memory, disks, the RAID mirror,
the tunnel, updates. Read-only, stdlib only. Plus the gateway's counts-only
snapshot. Nothing here ever sees a prompt or an answer.
"""
import glob
import json
import os
import subprocess
import time
import urllib.request

from o1common import Paths, p, read_json

SYS = os.environ.get("OLLAMA1_SYS", "/sys")      # tests point these at fixtures
PROC = os.environ.get("OLLAMA1_PROC", "/proc")


def _read(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return default


def _int(path):
    v = _read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def gpu_card():
    for dev in sorted(glob.glob(SYS + "/class/drm/card*/device")):
        if os.path.exists(dev + "/mem_info_vram_total"):
            return dev
    return None


_nv_cache = {"t": 0, "v": 0}


def gpu_vram_total():
    """Total VRAM in bytes: amdgpu sysfs, else nvidia-smi, else 0."""
    dev = gpu_card()
    if dev:
        return _int(dev + "/mem_info_vram_total") or 0
    if time.time() - _nv_cache["t"] < 300:
        return _nv_cache["v"]
    v = 0
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            v = int(r.stdout.split()[0]) << 20
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _nv_cache.update(t=time.time(), v=v)
    return v


def gpu():
    dev = gpu_card()
    if not dev:
        return None
    out = {
        "busy_pct": _int(dev + "/gpu_busy_percent"),
        "vram_used": _int(dev + "/mem_info_vram_used"),
        "vram_total": _int(dev + "/mem_info_vram_total"),
        "temps": {},
        "power_w": None, "power_cap_w": None, "fan_rpm": None, "fan_pct": None,
        "sclk_mhz": None,
    }
    hw = sorted(glob.glob(dev + "/hwmon/hwmon*"))
    if hw:
        h = hw[0]
        for lab in glob.glob(h + "/temp*_label"):
            name = _read(lab)
            val = _int(lab.replace("_label", "_input"))
            if name and val is not None:
                out["temps"][name] = round(val / 1000.0, 1)
        pw = _int(h + "/power1_average")
        if pw is None:
            pw = _int(h + "/power1_input")
        out["power_w"] = round(pw / 1e6, 1) if pw is not None else None
        cap = _int(h + "/power1_cap")
        out["power_cap_w"] = round(cap / 1e6) if cap else None
        out["fan_rpm"] = _int(h + "/fan1_input")
        pwm, pmax = _int(h + "/pwm1"), _int(h + "/pwm1_max") or 255
        out["fan_pct"] = round(100.0 * pwm / pmax) if pwm is not None else None
        f = _int(h + "/freq1_input")
        out["sclk_mhz"] = round(f / 1e6) if f else None
    return out


class CPU:
    """CPU busy % between two calls."""

    def __init__(self):
        self.prev = None

    def sample(self):
        line = (_read(PROC + "/stat", "") or "").split("\n", 1)[0].split()
        try:
            vals = [int(x) for x in line[1:]]
        except ValueError:
            return None
        if len(vals) < 4:
            return None
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals[:8])
        prev, self.prev = self.prev, (idle, total)
        if not prev or total == prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / float(total - prev[1])), 1)


def memory():
    info = {}
    for line in (_read(PROC + "/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        try:
            info[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return {"total": total, "used": total - avail, "available": avail}


def meminfo():
    """Bytes: MemTotal, MemAvailable, SwapTotal, SwapFree."""
    info = {}
    for line in (_read(PROC + "/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            try:
                info[k] = int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    return info


def ram_usage(info=None):
    """The server's memory in use, for the app's meter (GET /v1/usage):
    {"used_bytes", "total_bytes"}, each an int or None. In use is what
    Linux calls memory in use, MemTotal minus MemAvailable (not MemFree,
    which counts the cache as used); a value that can't be read, or that
    makes no sense, is None."""
    info = meminfo() if info is None else info
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    out = {"used_bytes": None, "total_bytes": None}
    if isinstance(total, int) and 0 < total < 1 << 50:
        out["total_bytes"] = total
        if isinstance(avail, int) and 0 <= avail <= total:
            out["used_bytes"] = total - avail
    return out


def ollama_oom_kills(unit="ollama.service"):
    """How many times the kernel's OOM killer has struck inside Ollama's
    cgroup (its MemoryMax); None if that can't be read."""
    path = SYS + "/fs/cgroup/system.slice/%s/memory.events" % unit
    for line in (_read(path, "") or "").splitlines():
        k, _, v = line.partition(" ")
        if k == "oom_kill":
            try:
                return int(v)
            except ValueError:
                return None
    return None


def loadavg():
    try:
        return [float(x) for x in (_read(PROC + "/loadavg", "") or "").split()[:3]]
    except ValueError:
        return None


def disks():
    out = []
    for label, mp in (("/", "/"), ("models", "/srv/models"), ("data", "/srv/data")):
        path = p(mp) if mp != "/" else (p("/") or "/")
        try:
            if mp != "/" and not os.path.ismount(path) and not os.environ.get("OLLAMA1_PREFIX"):
                out.append({"label": label, "mount": mp, "mounted": False})
                continue
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            out.append({"label": label, "mount": mp, "mounted": True, "total": total,
                        "used": total - st.f_bfree * st.f_frsize, "free": free})
        except OSError:
            out.append({"label": label, "mount": mp, "mounted": False})
    return out


def raid():
    """Arrays from /proc/mdstat: name, level, members up, resync progress."""
    text = _read(PROC + "/mdstat", "") or ""
    arrays = []
    cur = None
    for line in text.splitlines():
        if line and not line.startswith(" ") and " : " in line and line.startswith("md"):
            name, _, rest = line.partition(" : ")
            parts = rest.split()
            cur = {"name": name.strip(), "state": parts[0] if parts else "",
                   "level": next((x for x in parts if x.startswith("raid")), ""),
                   "members": "", "progress": None, "action": None, "finish": None}
            arrays.append(cur)
        elif cur is not None and "[" in line and "/" in line and "blocks" in line:
            seg = line.strip().split()[-1]
            if seg.startswith("[") and seg.endswith("]"):
                cur["members"] = seg[1:-1]
        elif cur is not None and ("resync" in line or "recovery" in line or "check" in line):
            for word in ("resync", "recovery", "check", "reshape"):
                if word in line:
                    cur["action"] = word
            try:
                cur["progress"] = float(line.split("=")[1].split("%")[0].strip())
            except (IndexError, ValueError):
                pass
            if "finish=" in line:
                cur["finish"] = line.split("finish=")[1].split()[0]
    for a in arrays:
        a["healthy"] = bool(a["members"]) and "_" not in a["members"]
    return arrays


def uptime():
    try:
        return int(float((_read(PROC + "/uptime", "0") or "0").split()[0]))
    except ValueError:
        return None


def tunnel(port=8439):
    """cloudflared's metrics /ready: 200 and readyConnections > 0 = up."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/ready" % port, timeout=1.5) as r:
            data = json.loads(r.read(4096).decode("utf-8") or "{}")
            return {"up": r.status == 200 and int(data.get("readyConnections", 0)) > 0,
                    "connections": int(data.get("readyConnections", 0))}
    except Exception:
        return {"up": False, "connections": 0}


_net_cache = {"t": 0, "v": None}


def _ipv4(ifname):
    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show", "dev", ifname],
                           capture_output=True, text=True, timeout=3)
        for link in json.loads(r.stdout or "[]"):
            for a in link.get("addr_info", []):
                if a.get("family") == "inet":
                    return "%s/%s" % (a.get("local"), a.get("prefixlen"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _port(ifname):
    base = SYS + "/class/net/" + ifname
    speed = _int(base + "/speed")
    return {"name": ifname, "state": _read(base + "/operstate", "?"),
            "carrier": _read(base + "/carrier") == "1",
            "speed_mbps": speed if speed and speed > 0 else None}


def network():
    """The LAN side: br0 (the bridge over both wired ports, which keeps the
    Raspberry Pi on the home LAN) with its address and each port's link,
    or the plain wired port if there is no bridge."""
    now = time.time()
    if _net_cache["v"] is not None and now - _net_cache["t"] < 15:
        return _net_cache["v"]
    out = None
    if os.path.isdir(SYS + "/class/net/br0"):
        members = sorted(os.listdir(SYS + "/class/net/br0/brif")) if os.path.isdir(
            SYS + "/class/net/br0/brif") else []
        out = dict(_port("br0"), bridge=True, address=_ipv4("br0"), ports=[_port(m) for m in members])
    else:
        for name in ("enp39s0",):
            if os.path.isdir(SYS + "/class/net/" + name):
                out = dict(_port(name), bridge=False, address=_ipv4(name), ports=[])
    _net_cache.update(t=now, v=out)
    return out


_timer_cache = {"t": 0, "v": None}


def next_timer(unit="ollama1-update-ollama.timer"):
    now = time.time()
    if now - _timer_cache["t"] < 60:
        return _timer_cache["v"]
    v = None
    try:
        r = subprocess.run(["systemctl", "show", "-P", "NextElapseUSecRealtime", unit],
                           capture_output=True, text=True, timeout=3)
        v = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    _timer_cache.update(t=now, v=v)
    return v


def sleep_record():
    """Last sleep / wake and the check after waking (o1sleep writes it)."""
    st = read_json(os.path.join(Paths.state, "sleep.json"), {})
    return st if isinstance(st, dict) else {}


def updates():
    out = {"last_unattended": None, "reboot_required": os.path.exists(Paths.reboot_required),
           "reboot_pkgs": [], "ollama": read_json(Paths.update_status, {}) or {},
           "next_ollama_update": next_timer(), "sleep": sleep_record()}
    try:
        out["last_unattended"] = int(os.stat(Paths.u_stamp).st_mtime)
    except OSError:
        pass
    pk = _read(Paths.reboot_pkgs)
    if pk:
        out["reboot_pkgs"] = sorted(set(pk.split()))[:20]
    return out


def gateway_snapshot():
    snap = read_json(Paths.stats)
    if not isinstance(snap, dict):
        return None
    snap["stale"] = time.time() - snap.get("time", 0) > 10
    return snap


def pairing_window():
    """The open window with its code, if this user may read it (root and
    the console dashboard's user can; the panel and ollama1-top can't)."""
    w = read_json(Paths.window)
    if isinstance(w, dict) and w.get("expires_at", 0) > time.time():
        return w
    return None


def collect(cpu_sampler=None, tunnel_port=8439):
    return {
        "time": int(time.time()),
        "gpu": gpu(),
        "cpu_pct": cpu_sampler.sample() if cpu_sampler else None,
        "load": loadavg(),
        "memory": memory(),
        "disks": disks(),
        "raid": raid(),
        "uptime": uptime(),
        "tunnel": tunnel(tunnel_port),
        "network": network(),
        "updates": updates(),
        "gateway": gateway_snapshot(),
        "hostname": os.uname().nodename,
    }
