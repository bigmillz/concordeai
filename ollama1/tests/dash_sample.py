"""A full, busy sample state for the dashboard renderer (tests and the
text capture in the report). Numbers and names only."""
import math
import time

GIB = 1 << 30


def sample(now=None, pairing=False, marker=None):
    now = now or 1790000000.0
    n = 3600
    wave = lambda base, amp, period, i: max(0.0, base + amp * math.sin(i / period))
    series = {
        "tps": [wave(40, 35, 60, i) if (i // 300) % 3 else 0.0 for i in range(n)],
        "gpu_busy": [wave(55, 45, 50, i) for i in range(n)],
        "vram_used": [wave(9, 5, 400, i) * GIB for i in range(n)],
        "gpu_power": [wave(160, 120, 70, i) for i in range(n)],
        "ram_used": [wave(20, 6, 500, i) * GIB for i in range(n)],
        "io_read": [wave(40e6, 40e6, 30, i) for i in range(n)],
        "io_write": [wave(8e6, 8e6, 45, i) for i in range(n)],
        "net_rx": [wave(2e6, 2e6, 20, i) for i in range(n)],
        "net_tx": [wave(5e5, 5e5, 25, i) for i in range(n)],
        "cpu_total": [wave(12, 10, 40, i) for i in range(n)],
        "active": [1 if i % 90 < 40 else 0 for i in range(n)],
        "gpu_temp": [wave(66, 14, 80, i) for i in range(n)],
        "cpu_temp": [wave(58, 8, 90, i) for i in range(n)],
        "cpu_mhz": [wave(3200, 900, 60, i) for i in range(n)],
        "fan_rpm": [wave(1400, 300, 80, i) for i in range(n)],
    }
    gw = {
        "time": int(now), "stale": False, "active": 1, "queued": 2, "requests_today": 184,
        "totals": {"auth_failures": 3, "refused_gpu": 2},
        "tps": {"current": 48.6, "1h": 41.2, "24h": 38.9},
        "prompt_tps": 612.4, "ttft_ms": 830,
        "errors": {"busy": 1, "gpu_fit": 2, "ram_pressure": 1},
        "per_minute": [[int(now // 60) - 59 + i, 300, 7e9, (i * 7) % 11, 0] for i in range(60)],
        "loaded": [
            {"name": "qwen3:14b", "size": 11 * GIB, "size_vram": 11 * GIB, "context_length": 8192,
             "expires_at": "2026-09-21T14:55:00.123456789-04:00", "ram_allowed": False},
            {"name": "gpt-oss:120b", "size": 60 * GIB, "size_vram": 14 * GIB, "context_length": 4096,
             "expires_at": "2026-09-21T15:10:00Z", "ram_allowed": True},
        ],
        "events": [{"t": int(now) - 600 + 45 * i, "kind": k, "model": m} for i, (k, m) in enumerate([
            ("load", "qwen3:14b"), ("unload: device switch", "qwen3:14b"), ("load", "qwen3:14b"),
            ("refused: gpu_fit", "llama3.3:70b"), ("unload: make room", "qwen3:14b"), ("load", "gpt-oss:120b"),
            ("refused: ram_pressure", "gpt-oss:120b"), ("load", "embeddinggemma"), ("unload", "embeddinggemma"),
            ("load", "qwen3:14b")])],
        "devices": [{"id": "56475aa75463474c", "name": "Alice's MacBook Pro", "last_seen": int(now) - 5,
                     "active": 1, "connected": True},
                    {"id": "a1b2c3d4e5f60718", "name": "iPad", "last_seen": int(now) - 7200, "active": 0,
                     "connected": False}],
        "ollama_version": "0.35.1",
    }
    if marker:
        gw["recent"] = [{"t": int(now), "device": "x", "model": "y", "prompt": marker, "messages": marker}]
        gw["messages"] = marker
        gw["content"] = marker
    st = {
        "time": now, "host": "testsrv", "uptime": 3 * 86400 + 4 * 3600 + 17 * 60,
        "gw": gw,
        "gpu": {"busy_pct": 97, "vram_used": 14.6 * GIB, "vram_total": 16 * GIB,
                "temps": {"edge": 64.0, "junction": 78.0, "mem": 70.0}, "power_w": 212.0, "power_cap_w": 289,
                "fan_rpm": 1450, "fan_pct": 38, "sclk_mhz": 2310, "mclk_mhz": 1000},
        "cpu": {"mhz": 3612, "max_mhz": 4700, "total": 14.2, "cores": [(i * 13) % 100 for i in range(32)], "temp": 58.5, "load": [1.2, 0.9, 0.8]},
        "mem": {"total": 60.7 * GIB, "available": 38.2 * GIB, "swap_total": 8 * GIB, "swap_free": 8 * GIB},
        "ollama_cg": {"current": 47.1 * GIB, "max": 52.7 * GIB},
        "disks": [{"label": "/", "mount": "/", "mounted": True, "total": 1.8e12, "used": 3.1e11, "free": 1.4e12},
                  {"label": "models", "mount": "/srv/models", "mounted": True, "total": 1.9e12, "used": 1.9e11,
                   "free": 1.6e12},
                  {"label": "data", "mount": "/srv/data", "mounted": True, "total": 7.9e12, "used": 1.2e10,
                   "free": 7.5e12}],
        "raid": [{"name": "md127", "level": "raid1", "members": "UU", "healthy": True, "action": "resync",
                  "progress": 41.3, "finish": "312.4min"}],
        "io": {"read_bps": 52e6, "write_bps": 3.1e6},
        "net": {"name": "br0", "address": "192.168.1.10/24", "state": "up", "rx_bps": 2.4e6, "tx_bps": 3.1e5,
                "ports": [{"name": "enp5s0", "carrier": True, "speed_mbps": 1000},
                          {"name": "enp6s0", "carrier": True, "speed_mbps": 1000}]},
        "tunnel": {"up": True, "connections": 4, "rtt_ms": 23.4},
        "updates": {"reboot_required": False, "last_unattended": int(now) - 5 * 3600,
                    "ollama": {"result": "current", "version": "v0.35.1"},
                    "next_ollama_update": "Sun 2026-10-04 03:30:00 EDT",
                    "sleep": {"last_sleep": int(now) - 86400, "last_wake": int(now) - 80000,
                              "resume_check": {"ok": True, "detail": "Ollama and the GPU answered"}}},
        "pairing": {"id": "x", "code": "7K4M2QXD9FHT", "expires_at": now + 200} if pairing else None,
        "power": {"watts": 312.4, "src": "plug", "kwh_24h": 3.54, "cost_24h": 0.61, "symbol": "$",
                  "badge": "on-peak until 19:00", "price": 0.3,
                  "currency": "USD", "priced": True, "since": int(now) - 90 * 86400,
                  "windows": {"1d": {"cost": 0.61, "kwh": 3.54, "measured_h": 24.0, "est": True},
                              "1w": {"cost": 4.87, "kwh": 27.9, "measured_h": 168.0, "est": True},
                              "1m": {"cost": 21.34, "kwh": 118.6, "measured_h": 700.0, "est": True}}},
        "activity": {"last": now - 420, "at": now, "inflight": 1},
        "idle": {"supported": True, "enabled": True, "minutes": 30},
        "series": series,
    }
    return st
