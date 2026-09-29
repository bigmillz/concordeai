"""Request signing and pairing: the rules PROTOCOL.md describes.

Signed request (every gateway call except POST /v1/pair):

    X-O1-Device:    device id, 16 lowercase hex = sha256(public key)[:8 bytes]
    X-O1-Timestamp: unix seconds, decimal
    X-O1-Nonce:     16-64 chars of [A-Za-z0-9_-], never reused
    X-O1-Signature: base64url Ed25519 signature over the canonical string

    canonical = "ollama1-req-v1\n" METHOD "\n" PATH "\n" hex(sha256(body))
                "\n" TIMESTAMP "\n" NONCE "\n" DEVICE_ID

Pairing (POST /v1/pair, only while a window is open at the desktop):

    key       = sha256("ollama1-pair-key-v1\n" + normalized code)
    mac       = HMAC-SHA256(key, "ollama1-pair-v1\n" NAME "\n" PUBKEY_B64URL
                            "\n" TIMESTAMP "\n" NONCE)
    proof     = HMAC-SHA256(key, "ollama1-pair-ok-v1\n" DEVICE_ID "\n"
                            PUBKEY_B64URL "\n" NONCE)   (sent back by the desktop)
"""
import hashlib
import hmac
import re
import secrets
import threading
import time

from o1crypto import b64url_decode, b64url_encode, ed25519_verify

SKEW = 60
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
DEVICE_RE = re.compile(r"^[0-9a-f]{16}$")
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
CODE_LEN = 8
PAIR_MAX_FAILURES = 5
PAIR_MIN_INTERVAL = 2.0


class AuthError(Exception):
    def __init__(self, code, message, status=401):
        super().__init__(message)
        self.code = code
        self.status = status


def device_id_for(pub):
    return hashlib.sha256(pub).hexdigest()[:16]


def body_hash(body):
    return hashlib.sha256(body or b"").hexdigest()


def canonical_request(method, path, body_sha_hex, ts, nonce, device_id):
    return "\n".join([
        "ollama1-req-v1", method.upper(), path, body_sha_hex, str(ts), nonce, device_id,
    ]).encode("utf-8")


class NonceCache:
    """Nonces seen in the last (2 x SKEW) seconds. Checked only after the
    signature is valid, so a stranger can't fill it."""

    def __init__(self, ttl=2 * SKEW + 10, cap=200000):
        self.ttl = ttl
        self.cap = cap
        self.seen = {}
        self.lock = threading.Lock()

    def check_and_add(self, key, now):
        with self.lock:
            if len(self.seen) > self.cap // 2:
                cutoff = now - self.ttl
                self.seen = {k: t for k, t in self.seen.items() if t > cutoff}
            if key in self.seen:
                return False
            if len(self.seen) >= self.cap:
                return False  # fail closed rather than forget
            self.seen[key] = now
            return True


class RequestVerifier:
    def __init__(self, devices, clock=time.time, skew=SKEW, start_time=None):
        """devices: a callable returning {device_id: {"public_key": bytes, "name": str}}."""
        self.devices = devices
        self.clock = clock
        self.skew = skew
        # Nonces are kept in memory only. After a restart, anything signed
        # before the restart is refused, so an old request can't be replayed.
        self.start_time = int(clock()) if start_time is None else start_time
        self.nonces = NonceCache()

    def verify(self, method, path, headers, body):
        dev_id = headers.get("X-O1-Device", "")
        ts_s = headers.get("X-O1-Timestamp", "")
        nonce = headers.get("X-O1-Nonce", "")
        sig_s = headers.get("X-O1-Signature", "")
        if not (dev_id and ts_s and nonce and sig_s):
            raise AuthError("unsigned", "request is not signed")
        if not DEVICE_RE.match(dev_id):
            raise AuthError("bad_device", "malformed device id")
        if not NONCE_RE.match(nonce):
            raise AuthError("bad_nonce", "malformed nonce")
        if not re.match(r"^[0-9]{1,12}$", ts_s):
            raise AuthError("bad_timestamp", "malformed timestamp")
        ts = int(ts_s)
        dev = self.devices().get(dev_id)
        if dev is None:
            raise AuthError("unpaired", "device is not paired with this desktop", 403)
        now = self.clock()
        if abs(now - ts) > self.skew:
            raise AuthError("clock_skew", "timestamp is more than %d s off" % self.skew)
        if ts < self.start_time:
            raise AuthError("clock_skew", "signed before the gateway started; sign again")
        try:
            sig = b64url_decode(sig_s)
        except ValueError:
            raise AuthError("bad_signature", "signature does not verify")
        msg = canonical_request(method, path, body_hash(body), ts, nonce, dev_id)
        if not ed25519_verify(dev["public_key"], msg, sig):
            raise AuthError("bad_signature", "signature does not verify")
        if not self.nonces.check_and_add(dev_id + ":" + nonce, now):
            raise AuthError("replay", "nonce already used")
        return dev_id, dev


# ---- pairing -----------------------------------------------------------

def generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def normalize_code(code):
    c = (code or "").upper().replace("-", "").replace(" ", "")
    c = c.replace("O", "0").replace("I", "1").replace("L", "1")
    return c


def format_code(code):
    c = normalize_code(code)
    return c[:4] + "-" + c[4:]


def pair_key(code):
    return hashlib.sha256(b"ollama1-pair-key-v1\n" + normalize_code(code).encode("ascii")).digest()


def pair_message(name, pub_b64, ts, nonce):
    return "\n".join(["ollama1-pair-v1", name, pub_b64, str(ts), nonce]).encode("utf-8")


def pair_mac(code, name, pub_b64, ts, nonce):
    return hmac.new(pair_key(code), pair_message(name, pub_b64, ts, nonce), hashlib.sha256).digest()


def pair_proof(code, device_id, pub_b64, nonce):
    msg = "\n".join(["ollama1-pair-ok-v1", device_id, pub_b64, nonce]).encode("utf-8")
    return hmac.new(pair_key(code), msg, hashlib.sha256).digest()


def valid_device_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= 40
            and all(ch.isprintable() for ch in name) and name == name.strip()
            and "\n" not in name)


def parse_pair_request(obj):
    """Shape checks shared by the gateway and the root commit step.
    Returns (name, pub_bytes, pub_b64, ts, nonce, mac_bytes)."""
    if not isinstance(obj, dict):
        raise AuthError("bad_request", "body must be a JSON object", 400)
    name = obj.get("name")
    pub_b64 = obj.get("public_key")
    ts = obj.get("timestamp")
    nonce = obj.get("nonce")
    mac_s = obj.get("mac")
    if not valid_device_name(name):
        raise AuthError("bad_request", "name must be 1-40 printable characters", 400)
    if not isinstance(ts, int) or isinstance(ts, bool):
        raise AuthError("bad_request", "timestamp must be an integer", 400)
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        raise AuthError("bad_request", "malformed nonce", 400)
    try:
        pub = b64url_decode(pub_b64)
        mac = b64url_decode(mac_s)
    except ValueError:
        raise AuthError("bad_request", "public_key and mac must be base64url", 400)
    if len(pub) != 32 or len(mac) != 32:
        raise AuthError("bad_request", "public_key must be 32 bytes, mac 32 bytes", 400)
    if b64url_encode(pub) != pub_b64:
        raise AuthError("bad_request", "public_key must be unpadded base64url", 400)
    return name, pub, pub_b64, ts, nonce, mac


def check_pair_mac(code, name, pub_b64, ts, nonce, mac):
    return hmac.compare_digest(pair_mac(code, name, pub_b64, ts, nonce), mac)
