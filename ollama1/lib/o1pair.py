"""Paired devices, the pairing window, and the root-side pairing check.

Files (see setup.sh and ollama1.tmpfiles for owners and modes):
  /etc/ollama1/devices.json          root:o1view 0640  paired public keys (not secret)
  /run/ollama1/pair/window.json      root:o1pair 0640  the open window, its code,
                                                        wrong-code count, used nonces
  /run/ollama1/pair-public.json      root 0644         window id + expiry only (no code)
  /run/ollama1/pair-spool/<r>.req    o1gw -> root      a pairing request, as received
  /run/ollama1/pair-result/<r>.json  root:o1gw 0640    the answer for that request

The gateway never sees the code. It checks the request's shape, spaces
attempts, drops the request in the spool and relays the answer. The root
step (ollama1-pair --commit, started by ollama1-pair-commit.path) checks the
code, counts wrong codes (5 close the window), refuses reused nonces,
adds the key and closes the window after one success, and computes the
proof the app checks. Only root writes devices.json and the window.

There is no invite, share, join or pool anywhere. A device is added only
through a window opened at the server.
"""
import datetime
import json
import os
import secrets
import threading
import time

from o1auth import (AuthError, PAIR_MAX_FAILURES, SKEW, check_pair_mac, device_id_for,
                    generate_code, pair_proof, parse_pair_request)
from o1common import Paths, read_json, read_json_safe, write_json_atomic
from o1crypto import b64url_encode

WINDOW_SECONDS = 300
RESULT_TTL = 120


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


# ---- the window (root) ---------------------------------------------------

def read_window(path=None, now=None):
    """The open window with its code (root and the console dashboard)."""
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


def read_public(now=None):
    """Whether a window is open: id and expiry only (the gateway's view)."""
    w = read_json(Paths.pair_public)
    now = time.time() if now is None else now
    if isinstance(w, dict) and isinstance(w.get("expires_at"), (int, float)) and w["expires_at"] > now:
        return w
    return None


def _save_window(w):
    write_json_atomic(Paths.window, w, mode=0o640, group="o1pair")
    write_json_atomic(Paths.pair_public, {"id": w["id"], "expires_at": w["expires_at"]}, mode=0o644)


def open_window(seconds=WINDOW_SECONDS):
    """Root only. Replaces any open window with a new one and a new code."""
    now = time.time()
    w = {"id": secrets.token_hex(8), "code": generate_code(), "opened_at": int(now),
         "expires_at": int(now + seconds), "failures": 0, "nonces": []}
    _save_window(w)
    return w


def close_window(window_id=None):
    w = read_json(Paths.window)
    if window_id is not None and (not isinstance(w, dict) or w.get("id") != window_id):
        return False
    closed = False
    for path in (Paths.window, Paths.pair_public):
        try:
            os.unlink(path)
            closed = True
        except FileNotFoundError:
            pass
    return closed


def _result(req_id, status, body):
    write_json_atomic(os.path.join(Paths.pair_result, req_id + ".json"),
                      {"status": status, "body": body}, mode=0o640, group="o1gw")


def _check(req, w, now, devices_path):
    """One request against the open window. Returns (status, body)."""
    if not w or req.get("window") != w["id"]:
        return 403, {"error": "no pairing window is open at the server", "code": "pair_closed"}
    try:
        name, pub, pub_b64, ts, nonce, mac = parse_pair_request(req.get("body"))
    except AuthError as e:
        return e.status, {"error": str(e), "code": e.code}
    if abs(now - ts) > SKEW:
        return 401, {"error": "timestamp is more than %d s off" % SKEW, "code": "clock_skew"}
    if nonce in w.get("nonces", []):
        return 401, {"error": "nonce already used", "code": "replay"}
    w.setdefault("nonces", []).append(nonce)
    if not check_pair_mac(w["code"], name, pub_b64, ts, nonce, mac):
        w["failures"] = int(w.get("failures", 0)) + 1
        left = PAIR_MAX_FAILURES - w["failures"]
        if left <= 0:
            close_window(w["id"])
            w.clear()
            return 403, {"error": "too many wrong codes; the window is closed", "code": "pair_closed"}
        _save_window(w)
        return 401, {"error": "wrong code", "code": "wrong_code", "attempts_left": left}
    dev_id = device_id_for(pub)
    data = read_json(devices_path or Paths.devices, {}) or {}
    devs = [d for d in data.get("devices", []) if isinstance(d, dict) and d.get("id") != dev_id]
    devs.append({"id": dev_id, "name": name, "public_key": pub_b64,
                 "paired_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")})
    _write_devices(devs, devices_path)
    proof = b64url_encode(pair_proof(w["code"], dev_id, pub_b64, nonce))
    close_window(w["id"])  # one success closes the window
    w.clear()
    return 200, {"device_id": dev_id, "name": name, "proof": proof}


def commit_spool(spool=None, devices_path=None, now=None, log=print):
    """Root only. Answers every waiting pairing request. Returns the device
    id added, or None."""
    spool = spool or Paths.spool
    now = time.time() if now is None else now
    added = None
    try:
        names = sorted(os.listdir(spool))
    except OSError:
        return None
    for fn in names:
        fp = os.path.join(spool, fn)
        if not fn.endswith(".req"):
            if not fn.startswith("."):
                _unlink(fp)
            continue
        req_id = fn[:-4]
        req = read_json_safe(fp, max_bytes=8192)     # the gateway's folder: no symlinks, no FIFOs
        _unlink(fp)
        if not isinstance(req, dict) or not all(c in "0123456789abcdef" for c in req_id) or len(req_id) != 16:
            continue
        w = read_window(now=now)
        if w and "failures" not in w:
            w["failures"] = 0
        status, body = _check(req, w, now, devices_path)
        _result(req_id, status, body)
        if status == 200:
            added = body["device_id"]
            log("pair: paired device %s" % added)
        else:
            log("pair: refused (%s)" % body.get("code"))
    try:
        for fn in os.listdir(Paths.pair_result):
            fp = os.path.join(Paths.pair_result, fn)
            if now - os.stat(fp).st_mtime > RESULT_TTL:
                _unlink(fp)
    except OSError:
        pass
    return added


def _unlink(fp):
    try:
        os.unlink(fp)
    except OSError:
        pass


# ---- the gateway's side --------------------------------------------------

def spool_request(req_id, window_id, obj, spool=None):
    """Hand a pairing request to the root step, as received."""
    spool = spool or Paths.spool
    body = {k: obj.get(k) for k in ("name", "public_key", "timestamp", "nonce", "mac")}
    tmp = os.path.join(spool, ".%s.tmp" % req_id)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"window": window_id, "body": body}, f)
    os.replace(tmp, os.path.join(spool, req_id + ".req"))


def wait_result(req_id, timeout=10.0):
    path = os.path.join(Paths.pair_result, req_id + ".json")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = read_json(path)
        if isinstance(r, dict) and isinstance(r.get("status"), int):
            return r["status"], r.get("body") or {}
        time.sleep(0.1)
    return None
