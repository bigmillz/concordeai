"""Paired devices, the pairing window, and the root-side commit.

Files (see setup.sh for owners and modes):
  /etc/ollama1/devices.json       root:o1view 0640 paired public keys (not secret)
  /run/ollama1/pair/window.json   root:o1pair 0640 an open window + its code
  /run/ollama1/pair-spool/        o1gw 0700       the gateway's hand-off:
      <window>.req   a pairing request whose code the gateway checked
      <window>.burn  too many wrong codes: close the window

Only root writes devices.json and window.json. The gateway can only drop a
request into the spool; the root commit step checks the code again itself
before adding a key, so the gateway alone can never add a device.

There is no invite, share, join or pool anywhere. A device is added only
through a window opened at the desktop.
"""
import datetime
import json
import os
import secrets
import threading
import time

from o1auth import (AuthError, check_pair_mac, device_id_for, generate_code,
                    parse_pair_request, SKEW)
from o1common import Paths, read_json, write_json_atomic

WINDOW_SECONDS = 300


class DeviceStore:
    """Read-only view of devices.json, reloaded when the file changes."""

    def __init__(self, path=None):
        self.path = path or Paths.devices
        self.mtime = None
        self.devices = {}
        self.lock = threading.Lock()

    def __call__(self):
        try:
            st = os.stat(self.path)
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            key = None
        with self.lock:
            if key != self.mtime:
                self.devices = load_devices(self.path)
                self.mtime = key
            return self.devices


def load_devices(path=None):
    from o1crypto import b64url_decode
    data = read_json(path or Paths.devices, {}) or {}
    out = {}
    for d in data.get("devices", []) if isinstance(data, dict) else []:
        try:
            pub = b64url_decode(d["public_key"])
            if len(pub) != 32 or device_id_for(pub) != d["id"]:
                continue
            out[d["id"]] = {"public_key": pub, "name": str(d.get("name", ""))[:40],
                            "paired_at": d.get("paired_at", "")}
        except (KeyError, ValueError, TypeError):
            continue
    return out


def devices_list(path=None):
    data = read_json(path or Paths.devices, {}) or {}
    devs = data.get("devices", []) if isinstance(data, dict) else []
    return [{"id": d.get("id"), "name": d.get("name"), "paired_at": d.get("paired_at")}
            for d in devs if isinstance(d, dict)]


def _write_devices(devs, path=None):
    write_json_atomic(path or Paths.devices, {"version": 1, "devices": devs},
                      mode=0o640, group="o1view")


def remove_device(dev_id, path=None):
    data = read_json(path or Paths.devices, {}) or {}
    devs = [d for d in data.get("devices", []) if isinstance(d, dict)]
    keep = [d for d in devs if d.get("id") != dev_id]
    if len(keep) == len(devs):
        return False
    _write_devices(keep, path)
    return True


# ---- the window ---------------------------------------------------------

def read_window(path=None, now=None):
    w = read_json(path or Paths.window)
    if not isinstance(w, dict):
        return None
    now = time.time() if now is None else now
    try:
        if float(w["expires_at"]) <= now or not w["id"] or not w["code"]:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return w


def open_window(seconds=WINDOW_SECONDS, path=None):
    """Root only. Replaces any open window with a new one and a new code."""
    now = time.time()
    w = {"id": secrets.token_hex(8), "code": generate_code(),
         "opened_at": int(now), "expires_at": int(now + seconds)}
    write_json_atomic(path or Paths.window, w, mode=0o640, group="o1pair")
    return w


def close_window(window_id=None, path=None):
    path = path or Paths.window
    w = read_json(path)
    if window_id is not None and (not isinstance(w, dict) or w.get("id") != window_id):
        return False
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def commit_spool(spool=None, window_path=None, devices_path=None, now=None, log=print):
    """Root only (ollama1-pair --commit, run by ollama1-pair-commit.path).
    Moves at most one checked request into devices.json and closes the
    window. Returns the device id added, or None."""
    spool = spool or Paths.spool
    now = time.time() if now is None else now
    added = None
    try:
        names = sorted(os.listdir(spool))
    except OSError:
        return None
    w = read_window(window_path, now)
    for fn in names:
        fp = os.path.join(spool, fn)
        if not (fn.endswith(".req") or fn.endswith(".burn")):
            _unlink(fp)
            continue
        wid = fn.rsplit(".", 1)[0]
        if fn.endswith(".burn"):
            if w and w["id"] == wid:
                close_window(wid, window_path)
                log("pair: window closed after too many wrong codes")
                w = None
            _unlink(fp)
            continue
        req = read_json(fp, max_bytes=8192)
        _unlink(fp)
        if added or not w or w["id"] != wid:
            log("pair: request for a closed window ignored")
            continue
        try:
            name, pub, pub_b64, ts, nonce, mac = parse_pair_request(req)
        except AuthError:
            log("pair: malformed request ignored")
            continue
        if abs(now - ts) > SKEW + 30:
            log("pair: stale request ignored")
            continue
        if not check_pair_mac(w["code"], name, pub_b64, ts, nonce, mac):
            log("pair: request with a wrong code ignored")
            continue
        dev_id = device_id_for(pub)
        data = read_json(devices_path or Paths.devices, {}) or {}
        devs = [d for d in data.get("devices", []) if isinstance(d, dict) and d.get("id") != dev_id]
        devs.append({"id": dev_id, "name": name, "public_key": pub_b64,
                     "paired_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
        _write_devices(devs, devices_path)
        close_window(wid, window_path)
        w = None
        added = dev_id
        log("pair: paired device %s" % dev_id)
    return added


def _unlink(fp):
    try:
        os.unlink(fp)
    except OSError:
        pass


def spool_request(window_id, obj, spool=None):
    """Gateway side: hand a code-checked request to the root commit step."""
    spool = spool or Paths.spool
    tmp = os.path.join(spool, ".%s.tmp" % window_id)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: obj[k] for k in ("name", "public_key", "timestamp", "nonce", "mac")}, f)
    os.replace(tmp, os.path.join(spool, window_id + ".req"))


def spool_burn(window_id, spool=None):
    spool = spool or Paths.spool
    with open(os.path.join(spool, window_id + ".burn"), "w") as f:
        f.write("1")
