"""Test helpers: a scratch OLLAMA1_PREFIX, reference Ed25519 (RFC 8032, pure
Python, used to sign and to cross-check vectors), a pure-Python RSA key and
JWT signer, a fake Cloudflare Access certs endpoint, and client helpers.

Import this module before any o1* module: it sets OLLAMA1_PREFIX first.
"""
import atexit
import base64
import hashlib
import http.client
import json
import os
import random
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
LIB = os.environ.get("OLLAMA1_TEST_LIB") or os.path.join(KIT, "lib")
BIN = os.environ.get("OLLAMA1_TEST_BIN") or os.path.join(KIT, "bin")
TOOLS = os.environ.get("OLLAMA1_TEST_TOOLS") or os.path.join(KIT, "tools")
CONFIG = os.environ.get("OLLAMA1_TEST_CONFIG") or os.path.join(KIT, "config")

if not os.environ.get("OLLAMA1_PREFIX"):
    _prefix = tempfile.mkdtemp(prefix="o1test-")
    os.environ["OLLAMA1_PREFIX"] = _prefix
    atexit.register(shutil.rmtree, _prefix, True)
PREFIX = os.environ["OLLAMA1_PREFIX"]
for d in ("etc/ollama1", "run/ollama1/pair", "run/ollama1/pair-spool", "run/ollama1/pair-result", "run/ollama1/stats",
          "var/lib/ollama1-gateway", "var/lib/ollama1", "var/lib/ollama1-admin", "srv/models",
          "opt/ollama/versions"):
    os.makedirs(os.path.join(PREFIX, d), exist_ok=True)
sys.path.insert(0, LIB)
sys.path.insert(0, HERE)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ---- Ed25519, RFC 8032 section 6 reference code ---------------------------------

_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _p - 2, _p)


_d = -121665 * _inv(121666) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _recover_x(y, sign):
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed_public(secret):
    return _compress(_mul(_expand(secret)[0], _G))


def ed_sign(secret, msg):
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _q
    R = _compress(_mul(r, _G))
    h = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % _q
    return R + int.to_bytes((r + h * a) % _q, 32, "little")


class Device:
    def __init__(self, seed=None, name="Test Mac"):
        self.seed = seed or secrets.token_bytes(32)
        self.pub = ed_public(self.seed)
        self.name = name
        self.id = hashlib.sha256(self.pub).hexdigest()[:16]

    def headers(self, method, path, body=b"", ts=None, nonce=None):
        from o1auth import body_hash, canonical_request
        ts = int(time.time()) if ts is None else ts
        nonce = nonce or b64u(secrets.token_bytes(16))
        msg = canonical_request(method, path, body_hash(body), ts, nonce, self.id)
        return {"X-O1-Device": self.id, "X-O1-Timestamp": str(ts), "X-O1-Nonce": nonce,
                "X-O1-Signature": b64u(ed_sign(self.seed, msg))}

    def record(self):
        return {"id": self.id, "name": self.name, "public_key": b64u(self.pub),
                "paired_at": "2026-09-29T12:00:00-04:00"}


def write_devices(devs):
    with open(os.path.join(PREFIX, "etc/ollama1/devices.json"), "w") as f:
        json.dump({"version": 1, "devices": [d.record() for d in devs]}, f)


# ---- RSA + JWT (pure Python) -------------------------------------------------

_SMALL = [x for x in range(3, 2000, 2) if all(x % y for y in range(3, int(x ** 0.5) + 1, 2))]


def _is_prime(n, rounds=24):
    for sp in _SMALL:
        if n % sp == 0:
            return n == sp
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = random.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = random.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_prime(c):
            return c


class RSAKey:
    def __init__(self, kid, bits=2048):
        self.kid = kid
        e = 65537
        while True:
            pr, qr = _prime(bits // 2), _prime(bits // 2)
            phi = (pr - 1) * (qr - 1)
            if pr != qr and phi % e:
                break
        self.n, self.e = pr * qr, e
        self.d = pow(e, -1, phi)

    def jwk(self):
        k = (self.n.bit_length() + 7) // 8
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                "n": b64u(self.n.to_bytes(k, "big")), "e": b64u(self.e.to_bytes(3, "big"))}

    def sign(self, msg):
        k = (self.n.bit_length() + 7) // 8
        t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(msg).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")


def make_jwt(key, claims, alg="RS256", kid=None):
    header = {"alg": alg, "kid": kid or key.kid, "typ": "JWT"}
    h = b64u(json.dumps(header).encode())
    pl = b64u(json.dumps(claims).encode())
    sig = key.sign((h + "." + pl).encode()) if alg == "RS256" else b""
    return h + "." + pl + "." + b64u(sig)


TEAM = "o1test.cloudflareaccess.com"
GW_AUD = "a" * 64
ADMIN_AUD = "b" * 64
# Made-up fixtures: nothing here is any real server, person, domain or LAN.
SERVER_NAME = "testsrv"
ZONE = "example.test"
GW_HOST = SERVER_NAME + "." + ZONE
ADMIN_HOST = SERVER_NAME + "-admin." + ZONE
LAN_CIDR = "10.0.0.0/24"
LAN_IP = "10.0.0.10"
ADMIN_EMAIL = "alice@example.test"
# What setup.sh writes to config.json for a server like that
BASE_CFG = {"server_name": SERVER_NAME, "cf_zone": ZONE, "hostname_gateway": GW_HOST,
            "hostname_admin": ADMIN_HOST, "lan_bind": LAN_IP, "lan_cidr": LAN_CIDR}
CLIENT_ID = "0123456789abcdef.access"


def claims(aud=GW_AUD, **kw):
    now = int(time.time())
    c = {"aud": [aud], "iss": "https://" + TEAM, "iat": now, "nbf": now, "exp": now + 600,
         "type": "app", "sub": "", "common_name": CLIENT_ID}
    c.update(kw)
    return c


class FakeJWKS:
    """The Access certs endpoint, locally. Counts fetches."""

    def __init__(self, keys):
        self.keys = keys
        self.fetches = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.fetches += 1
                raw = json.dumps({"keys": [k.jwk() for k in outer.keys]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.port = free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/cdn-cgi/access/certs" % self.port

    def close(self):
        self.srv.shutdown()


def request(port, method, path, body=b"", headers=None, host="testsrv.example.test",
            timeout=30, addr="127.0.0.1"):
    c = http.client.HTTPConnection(addr, port, timeout=timeout)
    h = {"Host": host}
    h.update(headers or {})
    if method in ("POST", "PUT") or body:
        h["Content-Length"] = str(len(body))
    c.request(method, path, body=body or None, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, r


def ndjson(data):
    return [json.loads(x) for x in data.decode().splitlines() if x.strip()]
