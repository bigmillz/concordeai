"""Quiet hours for ollama1 (6b601, per the owner): the server sits in a home, and at night it should be
as quiet and dark as it can while staying reachable.

From `start` to `end` in the server's own local time (23:00 to 08:00 unless /etc/ollama1/quiet.json says
otherwise; a window may cross midnight; start == end means never):
  * auto sleep counts `sleep_minutes` (12) instead of the day's minutes, when that is shorter
    (o1idle; only the timing value, the sleep itself is unchanged);
  * a model is unloaded `keep_alive` (2 minutes) after its last request (the gateway sets it on what it
    forwards);
  * the fans' working level is capped at `fan_pct` (35%), with the day's level kept as the heavy level, so a
    temperature near its limit still raises them at once, and the overheat override is untouched (o1fan);
  * the lights are off (o1leds, and the liquid cooler's own lights through o1aio);
  * heavy jobs nobody is waiting for (model downloads and syncs from the app) are refused with
    "quiet_hours" and can be asked again from `end` (the gateway).
The tunnel, SSH, the network and the sleep mechanism are not touched by any of it.

The file, if present, holds only these keys: {"enabled": bool, "start": "HH:MM", "end": "HH:MM",
"sleep_minutes": 5..120, "fan_pct": 30..100, "keep_alive_s": 60..1800, "lights_off": bool}. Anything else, or
a value of the wrong type, falls back to the default for that key. It is read again when it changes.
"""
import json
import os
import re
import time

from o1common import p

QUIET_FILE = p("/etc/ollama1/quiet.json")
DEFAULTS = {"enabled": True, "start": "23:00", "end": "08:00", "sleep_minutes": 12, "fan_pct": 35,
            "keep_alive_s": 120, "lights_off": True}
RANGES = {"sleep_minutes": (5, 120), "fan_pct": (30, 100), "keep_alive_s": (60, 1800)}
HHMM_RX = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

_cache = {"sig": None, "path": None, "val": None}


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def clean(obj):
    """Whatever the file held, as the full settings: each key checked on its own, a bad one is the default."""
    obj = obj if isinstance(obj, dict) else {}
    out = dict(DEFAULTS)
    if isinstance(obj.get("enabled"), bool):
        out["enabled"] = obj["enabled"]
    if isinstance(obj.get("lights_off"), bool):
        out["lights_off"] = obj["lights_off"]
    for k in ("start", "end"):
        if isinstance(obj.get(k), str) and HHMM_RX.match(obj[k]):
            out[k] = obj[k]
    for k, (lo, hi) in RANGES.items():
        if _int(obj.get(k)) and lo <= obj[k] <= hi:
            out[k] = obj[k]
    return out


def read_config(path=None):
    """The settings, read again only when the file changed (a stat per call)."""
    path = path or QUIET_FILE
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        sig = None
    if _cache["path"] == path and _cache["sig"] == sig and _cache["val"] is not None:
        return _cache["val"]
    raw = {}
    if sig is not None:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.loads(f.read(4096))
        except (OSError, ValueError):
            raw = {}
    val = clean(raw)
    _cache.update(sig=sig, path=path, val=val)
    return val


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(minute_of_day, start, end):
    """True when minute_of_day (0..1439) is inside [start, end); a window crossing midnight wraps; start == end
    is never."""
    s, e = _mins(start), _mins(end)
    if s == e:
        return False
    if s < e:
        return s <= minute_of_day < e
    return minute_of_day >= s or minute_of_day < e


def is_quiet(now=None, cfg=None, localtime=time.localtime):
    cfg = cfg or read_config()
    if not cfg["enabled"]:
        return False
    t = localtime(time.time() if now is None else now)
    return in_window(t.tm_hour * 60 + t.tm_min, cfg["start"], cfg["end"])


def tz_name(root=None):
    """The server's IANA zone ("America/New_York"), from /etc/timezone or the /etc/localtime link; "" if neither
    says."""
    root = root or p("/etc")
    try:
        with open(os.path.join(root, "timezone"), encoding="utf-8") as f:
            name = f.read(100).strip()
        if re.match(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$", name):
            return name
    except OSError:
        pass
    try:
        target = os.readlink(os.path.join(root, "localtime"))
        m = re.search(r"zoneinfo/(.+)$", target)
        if m:
            return m.group(1)
    except OSError:
        pass
    return ""


def view(now=None, cfg=None, localtime=time.localtime):
    """What the gateway tells a paired device (the app decides from it not to wake the server at night):
    {"enabled", "start", "end", "tz", "now", "sleep_minutes"}."""
    cfg = cfg or read_config()
    return {"enabled": cfg["enabled"], "start": cfg["start"], "end": cfg["end"], "tz": tz_name(),
            "now": is_quiet(now, cfg, localtime), "sleep_minutes": cfg["sleep_minutes"]}


def sleep_minutes(day_minutes, now=None, cfg=None, localtime=time.localtime):
    """The idle minutes auto sleep counts now: the night's when they are shorter, else the day's."""
    cfg = cfg or read_config()
    if is_quiet(now, cfg, localtime):
        return min(day_minutes, cfg["sleep_minutes"])
    return day_minutes


def keep_alive(now=None, cfg=None, localtime=time.localtime):
    """The keep_alive the gateway sets on forwarded model requests: "120s" at night, None by day (Ollama's own
    OLLAMA_KEEP_ALIVE applies)."""
    cfg = cfg or read_config()
    return "%ds" % cfg["keep_alive_s"] if is_quiet(now, cfg, localtime) else None


def fan_cap(now=None, cfg=None, localtime=time.localtime):
    """The fans' working level at night (a percent), or None by day."""
    cfg = cfg or read_config()
    return cfg["fan_pct"] if is_quiet(now, cfg, localtime) else None


def lights_off(now=None, cfg=None, localtime=time.localtime):
    cfg = cfg or read_config()
    return bool(cfg["lights_off"]) and is_quiet(now, cfg, localtime)
