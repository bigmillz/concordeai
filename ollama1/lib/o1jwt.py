"""Cloudflare Access JWT checks (Cf-Access-Jwt-Assertion), stdlib only.

Access signs its tokens with RS256. The public keys come from the team
domain's certs endpoint, https://<team>.cloudflareaccess.com/cdn-cgi/access/certs.
RSA PKCS#1 v1.5 verification is done by rebuilding the expected encoded
message and comparing it whole (no parsing of the decrypted block), which
is the safe way to do it without a crypto library.

A token passes only if: alg is RS256, its kid is one of the team's keys,
the signature verifies, iss is the team domain, aud contains the configured
AUD tag, and it is inside exp/nbf/iat (30 s leeway).
"""
import hashlib
import hmac
import json
import threading
import time
import urllib.request

from o1crypto import b64url_decode

SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
LEEWAY = 30


class JWTError(Exception):
    pass


def rsa_pkcs1_sha256_verify(n, e, msg, sig):
    k = (n.bit_length() + 7) // 8
    if k < 256 or len(sig) != k:  # at least 2048-bit keys
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_DIGESTINFO + hashlib.sha256(msg).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "ollama1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(1 << 20).decode("utf-8"))


class AccessVerifier:
    """Validates Access JWTs for one application (one AUD tag)."""

    def __init__(self, team_domain, aud, certs_url="", allow_insecure=False,
                 fetch=_fetch_json, clock=time.time):
        team_domain = (team_domain or "").strip().lower()
        for pre in ("https://", "http://"):
            if team_domain.startswith(pre):
                team_domain = team_domain[len(pre):]
        self.team_domain = team_domain.rstrip("/")
        self.issuer = "https://" + self.team_domain
        self.aud = (aud or "").strip()
        url = certs_url or (self.issuer + "/cdn-cgi/access/certs")
        if not url.startswith("https://") and not allow_insecure:
            raise ValueError("certs URL must be https")
        self.certs_url = url
        self.fetch = fetch
        self.clock = clock
        self.keys = {}
        self.fetched_at = 0.0
        self.last_try = 0.0
        self.lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.team_domain and self.aud)

    def _refresh(self, force=False):
        now = self.clock()
        with self.lock:
            fresh = self.keys and now - self.fetched_at < 3600
            if fresh and not force:
                return
            # don't hammer the certs endpoint on unknown kids or while it is down
            if self.last_try and now - self.last_try < (30 if self.keys else 5):
                return
            self.last_try = now
            try:
                data = self.fetch(self.certs_url)
            except Exception:
                return
            keys = {}
            for k in (data or {}).get("keys", []):
                try:
                    if k.get("kty") != "RSA" or not k.get("kid"):
                        continue
                    if k.get("alg", "RS256") != "RS256":
                        continue
                    n = int.from_bytes(b64url_decode(k["n"]), "big")
                    e = int.from_bytes(b64url_decode(k["e"]), "big")
                    keys[k["kid"]] = (n, e)
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
            if keys:
                self.keys = keys
                self.fetched_at = now

    def verify(self, token):
        if not self.configured:
            raise JWTError("access not configured")
        if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16384:
            raise JWTError("malformed token")
        h64, p64, s64 = token.split(".")
        try:
            header = json.loads(b64url_decode(h64))
            payload = json.loads(b64url_decode(p64))
            sig = b64url_decode(s64)
        except (ValueError, UnicodeDecodeError):
            raise JWTError("malformed token")
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise JWTError("malformed token")
        if header.get("alg") != "RS256":
            raise JWTError("bad alg")
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise JWTError("no kid")
        self._refresh()
        if kid not in self.keys:
            self._refresh(force=True)
        key = self.keys.get(kid)
        if key is None:
            raise JWTError("unknown kid")
        if not rsa_pkcs1_sha256_verify(key[0], key[1], (h64 + "." + p64).encode("ascii"), sig):
            raise JWTError("bad signature")
        if payload.get("iss") != self.issuer:
            raise JWTError("bad issuer")
        aud = payload.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if not any(isinstance(a, str) and hmac.compare_digest(a, self.aud) for a in auds):
            raise JWTError("bad audience")
        now = self.clock()
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or now > exp + LEEWAY:
            raise JWTError("expired")
        nbf = payload.get("nbf")
        if isinstance(nbf, (int, float)) and now + LEEWAY < nbf:
            raise JWTError("not yet valid")
        iat = payload.get("iat")
        if isinstance(iat, (int, float)) and now + LEEWAY < iat:
            raise JWTError("issued in the future")
        return payload
