#!/usr/bin/env python3
"""The admin panel on made-up data, for looking at it in a browser on a
development machine (6b398). Never part of an install: setup.sh copies bin/
and lib/ only, and this file lives in tests/.

    python3 tests/admin_demo.py [PORT]        # then open http://127.0.0.1:PORT/

What it does, all inside one scratch folder it makes (OLLAMA1_PREFIX,
OLLAMA1_SYS, OLLAMA1_PROC point there before any kit module loads):
  * writes a fake /sys and /proc (a graphics card, 16 threads, a RAID check
    under way) and the status files the services would write (gateway, fans,
    lights, sleep decision, power, GPU tuning), and moves them every second;
  * starts the real panel (bin/ollama1-admin's serve()) with a runner that
    starts nothing: it prints the unit and fakes what the unit would do;
  * puts a small proxy in front that adds what Cloudflare Access would (a
    signed JWT for the made-up admin email, the admin host name and the
    https Origin), so a plain browser can drive the page. Every POST the page
    sends is printed with the CSRF header it carried and the panel's answer.

It refuses to run as root, and it only ever binds 127.0.0.1.
"""
import datetime
import json
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client

GIB = 1 << 30


def refuse_reason(euid=None, environ=None):
    """Why this must not run here, or None. Root could reach real system
    paths through a mistake; a systemd service (INVOCATION_ID) is an install."""
    euid = os.geteuid() if euid is None else euid
    environ = os.environ if environ is None else environ
    if euid == 0:
        return "admin_demo refuses to run as root: it is a development tool, never part of a server"
    if environ.get("INVOCATION_ID"):
        return "admin_demo refuses to run under systemd: it is a development tool, never part of a server"
    return None


def setup_env():
    root = tempfile.mkdtemp(prefix="o1admin-demo-")
    os.environ["OLLAMA1_PREFIX"] = root
    os.environ["OLLAMA1_SYS"] = os.path.join(root, "sys")
    os.environ["OLLAMA1_PROC"] = os.path.join(root, "proc")
    return root


class Fake:
    """The made-up machine. tick() rewrites every file from a clock."""

    def __init__(self, root):
        self.root = root
        self.t0 = time.time()
        self.jiffies = [[0, 0] for _ in range(16)]
        self.models = {"qwen3:14b": 9.3, "gpt-oss:20b": 13.1, "llama3.1:8b": 4.9, "nomic-embed-text:latest": 0.27,
                       "<img src=x onerror=alert(1)>:latest": 0.1}
        self.devices = []
        self.pull = None
        self.actions = {}
        self.lock = threading.Lock()

    def w(self, rel, text):
        path = os.path.join(self.root, rel.lstrip("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)

    def wj(self, rel, obj):
        self.w(rel, json.dumps(obj))

    def wave(self, base, amp, period, phase=0.0):
        return base + amp * math.sin((time.time() - self.t0) / period + phase)

    def busy(self):
        return int(time.time() - self.t0) % 90 < 55

    def static(self):
        cpuinfo = "".join("processor\t: %d\nmodel name\t: AMD Ryzen 9 5950X 16-Core Processor\nphysical id\t: 0\n"
                          "core id\t\t: %d\ncpu MHz\t\t: 3400.000\n\n" % (n, n // 2) for n in range(16))
        self.w("proc/cpuinfo", cpuinfo)
        for n in range(16):
            b = "sys/devices/system/cpu/cpu%d/cpufreq/" % n
            for k, v in (("scaling_min_freq", 2200000), ("scaling_max_freq", 4900000), ("cpuinfo_max_freq", 4900000)):
                self.w(b + k, "%d\n" % v)
            self.w(b + "scaling_governor", "schedutil\n")
            self.w(b + "scaling_driver", "amd-pstate-epp\n")
        self.w("sys/class/hwmon/hwmon5/name", "k10temp\n")
        self.w("sys/class/hwmon/hwmon5/temp1_label", "Tctl\n")
        self.w("sys/class/hwmon/hwmon5/temp3_label", "Tccd1\n")
        dev = "sys/class/drm/card1/device/"
        self.w(dev + "mem_info_vram_total", "%d\n" % (16 * GIB))
        for i, lab in enumerate(("edge", "junction", "mem"), 1):
            self.w(dev + "hwmon/hwmon2/temp%d_label" % i, lab + "\n")
        self.w(dev + "hwmon/hwmon2/power1_cap", "%d\n" % 293_000_000)
        self.w(dev + "hwmon/hwmon2/pwm1_max", "255\n")
        net = "sys/class/net/"
        for n, sp in (("br0", None), ("enp5s0", 2500), ("enp6s0", 1000)):
            self.w(net + n + "/operstate", "up\n")
            self.w(net + n + "/carrier", "1\n")
            if sp:
                self.w(net + n + "/speed", "%d\n" % sp)
                self.w(net + "br0/brif/" + n, "")
        os.makedirs(os.path.join(self.root, "srv/data"), exist_ok=True)
        self.w("etc/ollama1/models.allow", "qwen3:14b\ngpt-oss:20b ram\nllama3.1:8b\nmistral-small:24b\nnomic-embed-text\n")
        self.devices = [{"id": "3f9a1c0d22b74e61", "name": "Sam's MacBook Pro", "paired_at": "2026-09-02T18:21:04Z"},
                        {"id": "a07c55e1b9d3f210", "name": "Studio iMac", "paired_at": "2026-09-14T09:02:51Z"},
                        {"id": "c3d2e1f0a9b8c7d6", "name": "<script>alert('x')</script> phone", "paired_at": "2026-10-01T07:44:10Z"}]
        self.write_devices()
        self.wj("var/lib/ollama1/ollama-update.json", {"last_run": int(time.time()) - 5 * 3600, "result": "updated",
                                                        "version": "0.12.3", "detail": "0.12.2 -> 0.12.3, checksum ok"})
        self.actions = {"backup": {"at": int(time.time()) - 26 * 3600, "result": "ok", "detail": "1.2 GiB to /srv/data/backup"},
                        "update": {"at": int(time.time()) - 3 * 86400, "result": "ok", "detail": "14 packages"}}
        self.wj("var/lib/ollama1/actions.json", self.actions)
        self.w("var/lib/apt/periodic/unattended-upgrades-stamp", "")
        os.utime(os.path.join(self.root, "var/lib/apt/periodic/unattended-upgrades-stamp"),
                 (time.time() - 7 * 3600,) * 2)
        self.wj("var/lib/ollama1/sleep.json", {"last_sleep": int(time.time()) - 30 * 3600,
                                              "last_wake": int(time.time()) - 22 * 3600,
                                              "resume_check": {"ok": True, "detail": "all services back"}})
        self.wj("var/lib/ollama1/gpu-tune.json", {
            "wanted": "on", "stock": {"power_uw": 255_000_000, "mclk": 1000},
            "applied": {"power_uw": 293_000_000, "mclk": 1075, "at": int(time.time()) - 22 * 3600},
            "check": {"result": "passed", "at": int(time.time()) - 22 * 3600, "tps": 61.8,
                      "note": "passed: 12 answers, no amdgpu errors, 61.8 tokens/s (stock 57.2, +8.0%); "
                              "hottest: junction 88 C, memory 84 C"}})
        self.energy()
        self.history()
        import o1power
        import o1tariff
        s = o1tariff.preset("weekdays-4pm-9pm", o1power.load_schedule())
        s.update(currency="USD", timezone="America/New_York")
        s["tiers"].update(on=0.38, mid=None, off=0.16, discount=None)
        o1power.save_schedule(s)

    def write_devices(self):
        self.wj("etc/ollama1/devices.json", {"version": 1, "devices": self.devices})

    def energy(self):
        import o1power
        now = int(time.time())
        rows = []
        start = now - now % 60 - 30 * 86400
        for t in range(start, now - now % 60, 60):
            h = (t // 3600) % 24
            w = 95 + (180 if 9 <= h < 23 and random.random() < 0.45 else 0) + random.random() * 20
            known = 0 if (t // 3600) % 97 == 0 else 60
            rows.append((t, round(w / 60.0 if known else 0, 5), "est" if t < now - 12 * 86400 else "plug", known,
                         int(random.random() * 900) if w > 200 else 0))
        o1power.append_rows(rows)

    def history(self):
        from o1common import Paths
        now = int(time.time())
        rows = []
        for t in range(now - 86400, now, 10):
            k = (t % 5400) / 5400.0
            busy = 1 if (t // 600) % 3 else 0
            rows.append([t, round(busy * (38 + 18 * math.sin(t / 97.0)), 1), round(busy * (70 + 25 * math.sin(t / 50.0))),
                         round(6 + busy * (6 + 2 * math.sin(t / 900.0)), 2), round(52 + busy * 24 + 4 * math.sin(t / 80.0), 1),
                         round(28 + busy * (170 + 40 * math.sin(t / 70.0)), 1), busy, round(48 + 14 * busy + 5 * math.sin(t / 90.0), 1),
                         round(3.6 + 0.9 * busy + 0.3 * math.sin(t / 60.0), 2), round(6 + 22 * busy + 6 * math.sin(t / 40.0), 1),
                         round(21 + 4 * k + busy * 3, 2)])
        os.makedirs(os.path.dirname(Paths.history), exist_ok=True)
        with open(Paths.history, "w") as f:
            json.dump({"rows": rows}, f)

    def tick(self):
        now = time.time()
        busy = self.busy()
        dev = "sys/class/drm/card1/device/"
        gb = max(0, min(100, int(self.wave(76, 14, 25)))) if busy else int(max(0, self.wave(2, 2, 5)))
        self.w(dev + "gpu_busy_percent", "%d\n" % gb)
        loaded = 9.3 + (13.1 if busy else 0)
        self.w(dev + "mem_info_vram_used", "%d\n" % int(min(15.6, loaded * (0.62 if busy else 1) + 0.7) * GIB))
        hw = dev + "hwmon/hwmon2/"
        for i, v in enumerate((self.wave(58 if busy else 41, 4, 45), self.wave(84 if busy else 45, 4, 40),
                               self.wave(80 if busy else 50, 3, 55)), 1):
            self.w(hw + "temp%d_input" % i, "%d\n" % int(v * 1000))
        self.w(hw + "power1_average", "%d\n" % int(self.wave(232 if busy else 31, 30 if busy else 3, 30) * 1e6))
        self.w(hw + "fan1_input", "%d\n" % int(self.wave(2150 if busy else 0, 120, 6) if busy else 0))
        self.w(hw + "pwm1", "%d\n" % (178 if busy else 0))
        self.w(hw + "freq1_input", "%d\n" % int((2400 if busy else 500) * 1e6))
        lines = []
        for n in range(16):
            frac = min(1.0, max(0.02, self.wave(0.45 if busy else 0.06, 0.25 if busy else 0.04, 12.0 + n, n)))
            self.jiffies[n][0] += int(100 * frac)
            self.jiffies[n][1] += int(100 * (1 - frac))
            self.w("sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % n,
                   "%d\n" % int(self.wave(4300000 if busy else 2900000, 500000, 15.0 + n, n)))
        tb, ti = sum(j[0] for j in self.jiffies), sum(j[1] for j in self.jiffies)
        lines.append("cpu  %d 0 0 %d 0 0 0 0" % (tb, ti))
        lines += ["cpu%d %d 0 0 %d 0 0 0 0" % (n, b, i) for n, (b, i) in enumerate(self.jiffies)]
        self.w("proc/stat", "\n".join(lines) + "\nintr 5\nprocs_running 3\nprocs_blocked 0\n")
        self.w("sys/class/hwmon/hwmon5/temp1_input", "%d\n" % int(self.wave(71 if busy else 49, 3, 50) * 1000))
        self.w("sys/class/hwmon/hwmon5/temp3_input", "%d\n" % int(self.wave(66 if busy else 45, 3, 50) * 1000))
        used = (24 + (3 if busy else 0) + self.wave(0, 0.8, 30)) * GIB
        self.w("proc/meminfo", "MemTotal: %d kB\nMemFree: %d kB\nMemAvailable: %d kB\nSwapTotal: 8388604 kB\nSwapFree: 8388604 kB\n"
               % (64 * GIB // 1024, (64 * GIB - used) // 2048, (64 * GIB - used) // 1024))
        self.w("proc/loadavg", "%.2f %.2f %.2f 3/812 44120\n" % (self.wave(2.4 if busy else 0.3, 0.4, 20), 1.71, 1.20))
        self.w("proc/uptime", "%.2f 1000.0\n" % (22 * 3600 + now - self.t0))
        pct = 37.4 + (now - self.t0) / 400.0
        self.w("proc/mdstat", "Personalities : [raid1]\nmd127 : active raid1 nvme1n1p1[1] nvme0n1p1[0]\n"
               "      1953382400 blocks super 1.2 [2/2] [UU]\n"
               "      [=======>.............]  check = %.1f%% (730558208/1953382400) finish=%.1fmin speed=1350000K/sec\n"
               "      bitmap: 0/15 pages [0KB], 65536KB chunk\n\nunused devices: <none>\n" % (pct, (100 - pct) * 1.6))
        self.gateway(now, busy)
        self.services(now, busy)

    def gateway(self, now, busy):
        recent = []
        names = ["Sam's MacBook Pro", "Studio iMac", "<script>alert('x')</script> phone"]
        for i in range(40):
            t = int(now) - (40 - i) * 47
            st = 200 if i % 13 else 503
            recent.append({"t": t, "device": names[i % 3], "model": ["qwen3:14b", "gpt-oss:20b", "llama3.1:8b"][i % 3],
                           "kind": ["chat", "chat", "generate", "embed"][i % 4], "status": st, "ms": 900 + (i * 377) % 9000,
                           "prompt_tokens": 120 + (i * 53) % 2000, "tokens": 0 if st >= 400 else 80 + (i * 97) % 900,
                           **({"code": "busy"} if st >= 400 else {})})
        exp = datetime.datetime.fromtimestamp(now + 240 - (now - self.t0) % 300, datetime.timezone.utc).isoformat()
        loaded = [{"name": "qwen3:14b", "size": int(9.3 * GIB), "size_vram": int(9.3 * GIB), "context_length": 16384,
                   "expires_at": exp}]
        if busy:
            loaded.append({"name": "gpt-oss:20b", "size": int(13.1 * GIB), "size_vram": int(5.2 * GIB),
                           "context_length": 8192, "expires_at": exp})
        snap = {"version": "demo", "time": int(now), "started": int(self.t0) - 3600, "active": 2 if busy else 0,
                "queued": 1 if busy and int(now) % 7 < 3 else 0, "requests_today": 184 + int((now - self.t0) / 9),
                "totals": {"requests": 5120, "tokens": 2_811_402, "errors": 31, "auth_failures": 4, "refused_gpu": 2},
                "auth_failures": {"bad_signature": 3, "unknown_device": 1},
                "tps": {"current": round(self.wave(46, 7, 20), 1) if busy else 0.0, "1h": 41.2, "24h": 38.9},
                "per_minute": [], "recent": recent,
                "devices": [{"id": "3f9a1c0d22b74e61", "name": names[0], "last_seen": int(now) - 3, "active": 1 if busy else 0,
                             "connected": True},
                            {"id": "a07c55e1b9d3f210", "name": names[1], "last_seen": int(now) - 4000, "active": 0,
                             "connected": False}],
                "loaded": loaded, "ollama_version": "0.12.3", "errors": {"busy": 27, "gpu_fit": 2, "upstream": 2},
                "events": [{"t": int(now) - 3000 + i * 300, "kind": ["load", "unload"][i % 2],
                            "model": ["qwen3:14b", "gpt-oss:20b"][i % 2]} for i in range(10)],
                "ttft_ms": 830, "prompt_tps": 612.4, "pairing_open": False}
        self.wj("run/ollama1/stats/gateway.json", snap)

    def services(self, now, busy):
        cyc = int(now - self.t0) % 90
        if busy:
            phase, pct, left = "working", 100, 0
        elif cyc < 70:
            phase, pct, left = "hold100", 100, 70 - cyc
        elif cyc < 85:
            phase, pct, left = "ramp", 50, 85 - cyc
        else:
            phase, pct, left = "idle20", 20, 0
        rpm = lambda full: int(full * pct / 100.0 + random.random() * 20)  # noqa: E731
        outs = [{"label": "CPU fan", "chip": "nct6798", "enable": 1, "pwm": int(2.55 * pct), "rpm": rpm(1650)},
                {"label": "Case fan 1", "chip": "nct6798", "enable": 1, "pwm": int(2.55 * pct), "rpm": rpm(1400)},
                {"label": "Case fan 2", "chip": "nct6798", "enable": 1, "pwm": int(2.55 * pct), "rpm": rpm(1380)},
                {"label": "Case fan 3", "chip": "nct6798", "enable": 1, "pwm": int(2.55 * pct), "rpm": rpm(1100)},
                {"label": "GPU fan", "chip": "amdgpu", "enable": 1, "pwm": int(2.55 * pct), "rpm": rpm(2400)}]
        temps = [{"label": "GPU junction", "c": 84.0 if busy else 47.0, "limit": 105},
                 {"label": "NVMe", "c": 58.0, "limit": 70}, {"label": "CPU", "c": 71.0 if busy else 49.0, "limit": 95}]
        st = {"at": int(now), "phase": phase, "pct": pct, "why": "a model is answering" if busy else "",
              "hold_left": left, "controlling": "5 outputs", "outputs": outs, "temps": temps,
              "aio": {"found": True, "name": "Corsair H115i Platinum", "state": "ok", "coolant_c": 31.4,
                      "coolant_hot": False, "pump_rpm": 2900 if busy else 1900, "pump_mode": "extreme", "fans": []},
              "hottest": temps[0], "closest": temps[1], "hot": []}
        import o1fan
        st["line"] = o1fan.status_line(st)
        self.wj("run/ollama1/fan.json", st)
        import o1leds
        fade = min(1.0, (cyc % 10) / 10.0)
        if busy:
            rgb, state, target = [255, int(60 * (1 - fade)), int(60 * (1 - fade))], ("fading" if fade < 1 else "red"), "red"
        else:
            rgb, state, target = [255, 255, 255], "white", "white"
        ls = {"at": int(now), "state": state, "target": target, "pos": fade, "rgb": rgb,
              "why": "a request is running" if busy else "idle", "connected": True, "error": None, "protocol": 4,
              "devices": [{"name": "ASUS ROG STRIX X570-E", "vendor": "ASUS", "leds": 8, "mode": "Direct", "usable": True},
                          {"name": "Corsair H115i Platinum", "vendor": "Corsair", "leds": 16, "mode": "Direct", "usable": True}]}
        ls["line"] = o1leds.status_line(ls)
        self.wj("run/ollama1/leds.json", ls)
        import o1idle
        cfg = o1idle.read_config()            # the setting the page (or a test) saved: the idle service re-reads it every tick
        idle_s = 0 if busy else 90 + int(now - self.t0) % max(60, cfg["minutes"] * 60 - 120)
        reason = ("auto sleep is off" if not cfg["enabled"] else "a request is running" if busy
                  else "idle %d of %d minutes" % (idle_s // 60, cfg["minutes"]))
        self.wj("run/ollama1/idle.json", {"at": int(now), "supported": True, "wake": ["02:00:5e:10:00:01"],
                                          "enabled": cfg["enabled"], "minutes": cfg["minutes"], "sleep_ok": False,
                                          "reason": reason, "idle_s": idle_s})
        w = self.wave(305 if busy else 96, 20 if busy else 4, 30)
        series = []
        for i in range(360):
            t = int(now) - (359 - i) * 10
            b = (t // 600) % 3
            series.append([t, round((300 if b else 95) + 25 * math.sin(t / 70.0), 1)])
        series[-1] = [int(now), round(w, 1)]
        self.wj("run/ollama1/power/now.json", {"t": int(now), "watts": round(w, 1), "src": "plug",
                                               "gpu_w": 232 if busy else 31, "cpu_w": 88 if busy else 34,
                                               "estimate_w": round(w, 1), "baseline_w": 40.0, "psu_efficiency": 0.9,
                                               "plug": {"configured": True, "ok": True, "type": "shelly2"},
                                               "series": series})
        if self.pull:
            p = self.pull
            p["completed"] = min(p["total"], p["completed"] + p["total"] // 12)
            p["status"] = "success" if p["completed"] >= p["total"] else "pulling"
            self.wj("run/ollama1/stats/pull.json", p)
            if p["status"] == "success":
                self.models[p["model"]] = p["total"] / GIB
                self.pull = None

    # -- what the units would have done ---------------------------------------
    def run_unit(self, unit):
        import o1library
        from o1common import name_hash
        verb = unit.split("@")[0].replace("ollama1-", "").replace(".service", "")
        print("UNIT %s" % unit, flush=True)
        with self.lock:
            if unit == "ollama1-models-preview.service":
                p = {"download": [{"name": "mistral-small:24b", "bytes": int(14.3 * GIB)}],
                     "update": [{"name": "qwen3:14b", "bytes": int(0.4 * GIB)}],
                     "remove": [{"name": "<img src=x onerror=alert(1)>:latest", "bytes": int(0.1 * GIB)}],
                     "unknown": [], "allow_errors": [], "installed": sorted(self.models), "enough_space": True,
                     "totals": {"download_bytes": int(14.3 * GIB), "update_bytes": int(0.4 * GIB), "freed_bytes": int(0.1 * GIB),
                                "free_now": int(1.2 * 2**40), "free_after": int(1.18 * 2**40)},
                     "made_at": int(time.time())}
                p["id"] = o1library.plan_id(p)
                o1library.write_preview(p)
            elif unit == "ollama1-models-sync.service":
                o1library.write_status({"state": "done", "message": "Done: removed 1, downloaded 1, updated 1 (demo)",
                                        "summary": {"removed": ["x"], "downloaded": ["mistral-small:24b"],
                                                    "updated": ["qwen3:14b"], "failed": []},
                                        "log": ["removed <img src=x onerror=alert(1)>:latest", "downloaded mistral-small:24b",
                                                "updated qwen3:14b"]})
                try:
                    os.unlink(o1library.preview_file())
                except OSError:
                    pass
            elif verb == "pull":
                h = unit.split("@")[1].split(".")[0]
                for line in open(os.path.join(self.root, "etc/ollama1/models.allow")):
                    n = line.split()[0] if line.split() else ""
                    if n and name_hash(n) == h:
                        self.pull = {"model": n, "status": "pulling", "completed": 0, "total": int(14.3 * GIB)}
            elif verb == "sleepcfg":
                import o1idle                 # the real program's own function, as the gateway's user would run it
                o1idle.apply_sleepcfg(unit.split("@", 1)[1][:-len(".service")])
            elif verb == "rmmodel":
                h = unit.split("@")[1].split(".")[0]
                self.models = {n: s for n, s in self.models.items() if name_hash(n) != h}
            elif verb == "rmdevice":
                d = unit.split("@")[1].split(".")[0]
                self.devices = [x for x in self.devices if x["id"] != d]
                self.write_devices()
            self.actions[verb] = {"at": int(time.time()), "result": "ok", "detail": "demo: nothing was started"}
            self.wj("var/lib/ollama1/actions.json", self.actions)
        return True, ""


def tunnel_metrics():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = b'{"status":200,"readyConnections":4}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def proxy(listen_port, admin_port, jwt, admin_host):
    """What cloudflared and Access would add, nothing else."""

    class P(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def fwd(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            h = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "origin", "connection")}
            h["Host"] = admin_host
            h["Cf-Access-Jwt-Assertion"] = jwt()
            if self.headers.get("Origin"):
                h["Origin"] = "https://" + admin_host
            c = http.client.HTTPConnection("127.0.0.1", admin_port, timeout=30)
            c.request(self.command, self.path, body=body or None, headers=h)
            r = c.getresponse()
            data = r.read()
            if self.command == "POST":
                print("POST %s csrf=%s body=%s -> %d %s" % (self.path, "yes" if self.headers.get("X-O1-CSRF") else "NO",
                                                            body.decode(errors="replace")[:300], r.status,
                                                            data.decode(errors="replace")[:200]), flush=True)
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in ("connection", "transfer-encoding"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
            c.close()

        do_GET = do_POST = fwd

    srv = ThreadingHTTPServer(("127.0.0.1", listen_port), P)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    bad = refuse_reason()
    if bad:
        print(bad, file=sys.stderr)
        return 2
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9910
    root = setup_env()
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import o1test_util as U              # noqa: E402  (after the environment above)
    from o1common import DEFAULTS
    from stub_ollama import Stub
    fake = Fake(root)
    fake.static()
    fake.tick()
    stub = Stub(U.free_port(), models={})
    stub.models = {n: {"size": int(s * GIB), "info": {}} for n, s in fake.models.items()}
    key = U.RSAKey("demo")
    jwks = U.FakeJWKS([key])
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("o1admin_demo", os.path.join(U.BIN, "ollama1-admin"))
    spec = importlib.util.spec_from_loader("o1admin_demo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    cfg = dict(DEFAULTS)
    cfg.update(U.BASE_CFG)
    cfg.update({"server_name": "atlas", "hostname_gateway": "atlas.example.test", "hostname_admin": "atlas-admin.example.test",
                "access_team_domain": U.TEAM, "admin_aud": U.ADMIN_AUD, "admin_email": U.ADMIN_EMAIL,
                "admin_port": U.free_port(), "ollama_url": "http://127.0.0.1:%d" % stub.port, "certs_url": jwks.url,
                "allow_insecure_certs_url": True, "tunnel_metrics_port": tunnel_metrics(),
                "ttyd_socket": os.path.join(root, "run/ollama1/ttyd.sock")})
    panel, srv, stop = mod.serve(cfg, runner=fake.run_unit)

    def jwt():
        return U.make_jwt(key, U.claims(aud=U.ADMIN_AUD, email=U.ADMIN_EMAIL, type="app", common_name=None))
    proxy(port, cfg["admin_port"], jwt, cfg["hostname_admin"])
    print("admin demo: http://127.0.0.1:%d/  (scratch folder %s)" % (port, root), flush=True)

    def on_term(*_):
        raise KeyboardInterrupt
    import signal
    signal.signal(signal.SIGTERM, on_term)              # a kill also removes the scratch folder
    try:
        while True:
            time.sleep(1)
            fake.tick()
            stub.models = {n: {"size": int(s * GIB), "info": {}} for n, s in fake.models.items()}
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
