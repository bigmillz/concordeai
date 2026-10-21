"""API keys for the gateway's OpenAI-style door (/openai/v1/..., 6b490).

A tool that can't sign requests the way the paired app does (OMP, editors, scripts) gets a key instead.
Only a hash is kept: /etc/ollama1/api-keys.json, root:o1gw 0640, {"keys": [{"name", "sha256", "created"}]}.
The key itself is printed once, by `sudo ollama1-apikey new NAME`. Cloudflare Access still guards the door
(the request must carry the paired service token); the key is the second lock, and names the tool in the log.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time

PATH = os.environ.get("OLLAMA1_API_KEYS", "/etc/ollama1/api-keys.json")
PREFIX = "o1k_"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_cache = {"key": None, "keys": []}


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def load(path=None):
    """[{name, sha256, created}] or [] (a missing or unreadable file means no keys, never an error)."""
    path = path or PATH
    try:
        st = os.stat(path)
    except OSError:
        return []
    stamp = (path, st.st_mtime_ns, st.st_size)
    if _cache["key"] == stamp:
        return _cache["keys"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        keys = [k for k in (data.get("keys") or []) if isinstance(k, dict)
                and isinstance(k.get("name"), str) and isinstance(k.get("sha256"), str)]
    except (OSError, ValueError, AttributeError):
        keys = []
    _cache.update(key=stamp, keys=keys)
    return keys


def verify(key, path=None):
    """The key's name if `key` is a current key, else None. Compares every entry in constant time."""
    if not isinstance(key, str) or not key.startswith(PREFIX) or len(key) > 200:
        return None
    h = _hash(key)
    found = None
    for k in load(path):
        if hmac.compare_digest(h, k["sha256"]):
            found = k["name"]
    return found


def _save(keys, path, gid=None):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"keys": keys}, f, indent=1)
    if gid is not None:
        os.chown(tmp, 0, gid)
    os.replace(tmp, path)


def new(name, path=None, gid=None, now=None):
    """Make a key named `name`, keep its hash, return the key (shown once)."""
    if not NAME_RE.match(name or ""):
        raise ValueError("name: lowercase letters, digits and hyphens, up to 32, like omp-macbook")
    path = path or PATH
    keys = [k for k in load(path) if k["name"] != name]
    key = PREFIX + secrets.token_urlsafe(32)
    keys.append({"name": name, "sha256": _hash(key), "created": int(now or time.time())})
    _save(keys, path, gid)
    return key


def revoke(name, path=None, gid=None):
    """True when a key of that name was there."""
    path = path or PATH
    keys = load(path)
    left = [k for k in keys if k["name"] != name]
    if len(left) == len(keys):
        return False
    _save(left, path, gid)
    return True
