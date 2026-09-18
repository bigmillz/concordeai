#!/usr/bin/env python3
"""Credentials, quota and cache for live flight search.

    python3 concorde-travel/live.py status     # what is configured, what is left
    python3 concorde-travel/live.py probe      # validate the key with a REAL search
    python3 concorde-travel/live.py search JFK LHR 2026-11-12

Nothing here decides anything about a flight. It answers one question - may we
call the provider right now, and if so, what came back - and hands the payload
to `adapter.from_kiwi`. The scorer never sees this module; hard rule 5 is
unaffected.

FOUR THINGS THIS GETS RIGHT ON PURPOSE, each of them a trap the repo has
already been bitten by or a way a metered API turns into a surprise bill:

THE PROBE USES THE RUNTIME PAYLOAD. A validation call whose shape differs from
the real one will happily bless a key that then fails on every actual search.
`probe()` runs `search()` with a real query and a one-call allowance - there is
no separate "is this key ok" endpoint shape to drift from.

THE CACHE IS CHECKED BEFORE THE QUOTA. A cached answer costs nothing, so it must
not spend a call. Getting this backwards is how a cache stops being a cache.

QUOTA IS RESERVED BEFORE THE CALL, NOT COUNTED AFTER. A crash or a timeout mid
request has already consumed the provider's quota; counting afterwards
undercounts exactly when it matters. A connection that never reached them is
refunded, because that one genuinely cost nothing.

THE KEY NEVER LEAVES THIS MODULE. Not into a log line, not into an error
message, not into the page, not into git. `redact()` runs over everything that
escapes, and the config file is written 0600.

The provider's wire format lives in CONFIG, not in code. The Tequila profile
below is written from documentation and is UNVERIFIED against the live service
from here - every host was unreachable from the machine this was built on. If
their parameter names differ, fix the profile in ~/.concordego/cloud.json; you
should not have to edit Python to correct someone else's query string.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.join(os.path.expanduser("~"), ".concordego")
CONFIG_FILE = os.path.join(HOME, "cloud.json")
QUOTA_FILE = os.path.join(HOME, "quota.json")
TOKEN_FILE = os.path.join(HOME, "token.json")
CACHE_DIR = os.path.join(HOME, "cache")

# A bare Python-urllib User-Agent gets 403'd by provider edges - "works in
# curl, fails in-app" is usually a UA fingerprint, not logic.
UA = "ConcordeGo/0.1 (+flight re-ranker; contact: operator)"

_QUOTA_DEFAULT = {
    "per_day": 100,
    "per_month": 1000,
    "min_seconds_between_calls": 2.0,
    "cache_ttl_seconds": 1800,
}

# Two provider profiles. Both are UNVERIFIED against their live service - no
# credential for either was available on the machine this was written on - so
# both carry a _note saying so rather than being presented as tested.
PROFILES = {
    "amadeus": {
        "provider": "amadeus",
        # test.api.amadeus.com is the free sandbox and returns a limited, cached
        # slice of real content; api.amadeus.com is production and is metered.
        # They take the same credentials shape but NOT the same credentials.
        "base": "https://test.api.amadeus.com",
        "path": "/v2/shopping/flight-offers",
        "auth": {
            "mode": "oauth2_client_credentials",
            "token_path": "/v1/security/oauth2/token",
            "header": "Authorization",
            "scheme": "Bearer",
            "grant_type": "client_credentials",
            "client_id_field": "client_id",
            "client_secret_field": "client_secret",
            "expiry_margin_seconds": 60,
        },
        "params": {
            "origin": "originLocationCode", "destination": "destinationLocationCode",
            "date": "departureDate", "adults": "adults",
            "currency": "currencyCode", "limit": "max",
        },
        "date_format": "%Y-%m-%d",
        "static_params": {},
        "quota": dict(_QUOTA_DEFAULT),
        "_note": ("SELF-SERVICE IS GONE. Amadeus decommissioned the Self-Service "
                  "developer portal on 2026-07-17 and disabled its keys; what remains "
                  "at developers.amadeus.com is the Enterprise portal, which is a "
                  "sales process, not a signup. This profile is kept because Amadeus "
                  "Enterprise speaks the same Flight Offers Search v2 schema, so the "
                  "adapter still applies to anyone who has that access. Wire format is "
                  "from the published docs and UNVERIFIED against the live service. "
                  "For a self-serve key today, see docs/design.md."),
    },
    "kiwi-tequila": {
        "provider": "kiwi-tequila",
        "base": "https://tequila-api.kiwi.com",
        "path": "/v2/search",
        "auth": {"mode": "header_key", "header": "apikey", "scheme": ""},
        "auth_header": "apikey",                    # honoured for older configs
        "params": {
            "origin": "fly_from", "destination": "fly_to",
            "date_from": "date_from", "date_to": "date_to",
            "adults": "adults", "currency": "curr", "limit": "limit",
        },
        "date_format": "%d/%m/%Y",
        "static_params": {"vehicle_type": "aircraft", "sort": "quality"},
        "quota": dict(_QUOTA_DEFAULT),
        "_note": ("Wire format from Tequila's published docs and UNVERIFIED against the "
                  "live service - every Kiwi host was unreachable from the machine this "
                  "was written on. Correct the params here rather than in Python. NOTE "
                  "that Kiwi closed self-serve Tequila registration in 2024; new access "
                  "is an application, not a signup."),
    },
}

# Amadeus is the default for its SCHEMA, not its availability: it carries the
# operating carrier, the equipment code and the fare brand - the three fields the
# scorer has to abstain on with the Kiwi feed - and Amadeus Enterprise still
# speaks it. Neither shipped profile is self-serve any more: Amadeus retired
# Self-Service on 2026-07-17 and Kiwi closed Tequila signups in 2024. The
# self-serve option today is Duffel, which has no adapter yet.
# Switch with `live.py provider kiwi-tequila`.
DEFAULTS = json.loads(json.dumps(PROFILES["amadeus"]))


# --------------------------------------------------------------- redaction

def redact(text, *secrets):
    """Nothing carrying a key may escape this module. Applied to every error
    string, not only the ones that look risky."""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, s[:3] + "…" + s[-2:])
    # and anything key-shaped we were not told about
    out = re.sub(r"\b(apikey|api_key|key|token)=([^&\s\"']+)", r"\1=<redacted>", out, flags=re.I)
    return out


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ config

def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            user = json.load(fh)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        cfg["_config_error"] = "could not read %s: %s" % (CONFIG_FILE, exc)
    # env wins, so a key need never be written to disk at all
    env = os.environ.get("CONCORDEGO_FLIGHT_KEY")
    if env:
        cfg["key"] = env
        cfg["_key_source"] = "CONCORDEGO_FLIGHT_KEY"
    elif cfg.get("key"):
        cfg["_key_source"] = CONFIG_FILE
    # OAuth2 providers need a second half. It is a credential in every sense the
    # key is, so it gets the same treatment: env first, redacted everywhere.
    env_secret = os.environ.get("CONCORDEGO_FLIGHT_SECRET")
    if env_secret:
        cfg["secret"] = env_secret
        cfg["_secret_source"] = "CONCORDEGO_FLIGHT_SECRET"
    elif cfg.get("secret"):
        cfg["_secret_source"] = CONFIG_FILE
    return cfg


def auth_mode(cfg):
    return ((cfg.get("auth") or {}).get("mode")
            or ("header_key" if cfg.get("auth_header") else "header_key"))


def credentials_complete(cfg):
    """(ok, what_is_missing). An OAuth2 provider with only half its credentials
    is not 'configured' - it is a call that will 401 after spending quota."""
    if not cfg.get("key"):
        return False, "no API key configured"
    if auth_mode(cfg) == "oauth2_client_credentials" and not cfg.get("secret"):
        return False, ("this provider uses OAuth2 and needs a client secret as well "
                       "as a client id")
    return True, ""


def write_example_config():
    """Write a config with no key in it, so there is something to edit."""
    if os.path.exists(CONFIG_FILE):
        return CONFIG_FILE, False
    sample = json.loads(json.dumps(DEFAULTS))
    sample["key"] = ""
    if auth_mode(sample) == "oauth2_client_credentials":
        sample["secret"] = ""
        sample["_how"] = ("key = your API Key (client id), secret = your API Secret. "
                          "Both come from the same Amadeus self-service app. Or export "
                          "CONCORDEGO_FLIGHT_KEY and CONCORDEGO_FLIGHT_SECRET and leave "
                          "these empty.")
    _atomic_write(CONFIG_FILE, sample)
    return CONFIG_FILE, True


# ------------------------------------------------------------------- quota

def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _month():
    return time.strftime("%Y-%m", time.gmtime())


def load_quota():
    try:
        with open(QUOTA_FILE, encoding="utf-8") as fh:
            q = json.load(fh)
    except (OSError, ValueError):
        q = {}
    # counters roll over on their own; no cron, no cleanup job
    if q.get("day") != _today():
        q["day"], q["day_calls"] = _today(), 0
    if q.get("month") != _month():
        q["month"], q["month_calls"] = _month(), 0
    q.setdefault("day_calls", 0)
    q.setdefault("month_calls", 0)
    q.setdefault("last_call_at", 0.0)
    q.setdefault("total_calls", 0)
    return q


def quota_state(cfg=None):
    cfg = cfg or load_config()
    lim = cfg["quota"]
    q = load_quota()
    return {
        "day": q["day"], "month": q["month"],
        "day_calls": q["day_calls"], "day_limit": lim["per_day"],
        "day_left": max(0, lim["per_day"] - q["day_calls"]),
        "month_calls": q["month_calls"], "month_limit": lim["per_month"],
        "month_left": max(0, lim["per_month"] - q["month_calls"]),
        "total_calls": q["total_calls"],
        "seconds_until_next": max(0.0, lim["min_seconds_between_calls"]
                                  - (time.time() - q["last_call_at"])),
    }


def _reserve(cfg):
    """Spend a call up front. Counting afterwards undercounts precisely when a
    request dies mid-flight, which is when the provider has charged you anyway."""
    lim = cfg["quota"]
    q = load_quota()
    if q["day_calls"] >= lim["per_day"]:
        return False, ("daily limit reached: %d of %d calls used today"
                       % (q["day_calls"], lim["per_day"]))
    if q["month_calls"] >= lim["per_month"]:
        return False, ("monthly limit reached: %d of %d calls used in %s"
                       % (q["month_calls"], lim["per_month"], q["month"]))
    wait = lim["min_seconds_between_calls"] - (time.time() - q["last_call_at"])
    if wait > 0:
        return False, "rate limited: %.1fs until the next call is allowed" % wait
    q["day_calls"] += 1
    q["month_calls"] += 1
    q["total_calls"] += 1
    q["last_call_at"] = time.time()
    _atomic_write(QUOTA_FILE, q)
    return True, ""


def _refund():
    """Only for a request that never reached the provider."""
    q = load_quota()
    q["day_calls"] = max(0, q["day_calls"] - 1)
    q["month_calls"] = max(0, q["month_calls"] - 1)
    q["total_calls"] = max(0, q["total_calls"] - 1)
    _atomic_write(QUOTA_FILE, q)


# ------------------------------------------------------------------- token

def _cred_fingerprint(cfg):
    """Identifies a credential pair WITHOUT storing it. A cached bearer token
    minted with an old key must not be replayed after the key is rotated; the
    fingerprint is what notices, and it is a one-way hash so the token file
    never becomes a second place a secret lives."""
    blob = (str(cfg.get("key", "")) + "\x00" + str(cfg.get("secret", ""))).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _token_load(cfg):
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            tok = json.load(fh)
    except (OSError, ValueError):
        return None
    margin = float((cfg.get("auth") or {}).get("expiry_margin_seconds", 60))
    if tok.get("provider") != cfg.get("provider"):
        return None
    if tok.get("fingerprint") != _cred_fingerprint(cfg):
        return None                       # credentials changed under us
    if time.time() >= float(tok.get("expires_at", 0)) - margin:
        return None                       # expired, or close enough to be a race
    return tok.get("access_token")


def _token_store(cfg, access_token, expires_in):
    _atomic_write(TOKEN_FILE, {
        "provider": cfg.get("provider"),
        "fingerprint": _cred_fingerprint(cfg),
        "access_token": access_token,
        "expires_at": time.time() + float(expires_in or 0),
    })


def token_clear():
    try:
        os.remove(TOKEN_FILE)
        return True
    except OSError:
        return False


def get_token(cfg):
    """(token, error). Minted BEFORE any quota is reserved, deliberately.

    A token mint is not the metered call - the provider does not charge it
    against the search allowance - so a bad credential should cost nothing.
    Reserving first would burn a search on every attempt with a mistyped
    secret, which is the most likely thing to be wrong on day one."""
    cached = _token_load(cfg)
    if cached:
        return cached, None
    a = cfg.get("auth") or {}
    url = cfg["base"].rstrip("/") + a.get("token_path", "/v1/security/oauth2/token")
    body = urllib.parse.urlencode({
        "grant_type": a.get("grant_type", "client_credentials"),
        a.get("client_id_field", "client_id"): cfg.get("key", ""),
        a.get("client_secret_field", "client_secret"): cfg.get("secret", ""),
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": UA,
    })
    secrets = (cfg.get("key", ""), cfg.get("secret", ""))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return None, redact("HTTP %s minting an access token: %s" % (exc.code, detail),
                            *secrets)
    except (urllib.error.URLError, OSError) as exc:
        return None, redact("could not reach the token endpoint: %s" % exc, *secrets)
    except ValueError:
        return None, "the token endpoint did not return JSON"
    tok = payload.get("access_token")
    if not tok:
        return None, redact("the token endpoint returned no access_token: %s"
                            % json.dumps(payload)[:200], *secrets)
    _token_store(cfg, tok, payload.get("expires_in", 1799))
    return tok, None


# ------------------------------------------------------------------- cache

def cache_key(query):
    blob = json.dumps(query, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:24]


def cache_get(query, ttl):
    path = os.path.join(CACHE_DIR, cache_key(query) + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    age = time.time() - entry.get("at", 0)
    if age > ttl:
        return None
    entry["_age_seconds"] = int(age)
    return entry


def cache_put(query, payload):
    os.makedirs(CACHE_DIR, exist_ok=True)
    _atomic_write(os.path.join(CACHE_DIR, cache_key(query) + ".json"),
                  {"at": time.time(), "query": query, "payload": payload})


def cache_stats():
    try:
        files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]
    except OSError:
        return {"entries": 0, "bytes": 0}
    total = 0
    for f in files:
        try:
            total += os.path.getsize(os.path.join(CACHE_DIR, f))
        except OSError:
            pass
    return {"entries": len(files), "bytes": total}


# ------------------------------------------------------------------ search

def _url(cfg, query):
    p = cfg["params"]
    args = dict(cfg.get("static_params") or {})
    args[p["origin"]] = query["origin"]
    args[p["destination"]] = query["destination"]
    d = time.strftime(cfg["date_format"], time.strptime(query["date"], "%Y-%m-%d"))
    # One provider wants a single departure date, another a range covering the
    # same day. Both are just names in the profile, which is the point of
    # keeping the wire format out of the code.
    if p.get("date"):
        args[p["date"]] = d
    else:
        args[p["date_from"]] = d
        args[p["date_to"]] = d
    for logical, qkey, default in (("adults", "adults", 1),
                                   ("currency", "currency", "USD"),
                                   ("limit", "limit", 20)):
        name = p.get(logical)
        if name:
            args[name] = query.get(qkey, default)
    return cfg["base"].rstrip("/") + cfg["path"] + "?" + urllib.parse.urlencode(args)


def _auth_headers(cfg):
    """(headers, error). The only place an auth scheme is interpreted."""
    a = cfg.get("auth") or {}
    mode = auth_mode(cfg)
    if mode == "oauth2_client_credentials":
        tok, err = get_token(cfg)
        if err:
            return None, err
        return {a.get("header", "Authorization"):
                ("%s %s" % (a.get("scheme", "Bearer"), tok)).strip()}, None
    header = a.get("header") or cfg.get("auth_header") or "apikey"
    scheme = a.get("scheme") or ""
    value = ("%s %s" % (scheme, cfg["key"])).strip() if scheme else cfg["key"]
    return {header: value}, None


def search(query, cfg=None, allow_call=True):
    """Cache, then quota, then the wire. Returns (payload, meta)."""
    cfg = cfg or load_config()
    ttl = cfg["quota"]["cache_ttl_seconds"]
    key = cfg.get("key") or ""

    # A cached answer costs nothing, so it must not spend a call.
    hit = cache_get(query, ttl)
    if hit:
        return hit["payload"], {"source": "cache", "age_seconds": hit["_age_seconds"],
                                "quota": quota_state(cfg)}

    have, missing = credentials_complete(cfg)
    if not have:
        return None, {"source": "none", "error": missing,
                      "how": ("set CONCORDEGO_FLIGHT_KEY and CONCORDEGO_FLIGHT_SECRET, "
                              "or put them in " + CONFIG_FILE)
                             if auth_mode(cfg) == "oauth2_client_credentials"
                             else "set CONCORDEGO_FLIGHT_KEY, or put one in " + CONFIG_FILE,
                      "quota": quota_state(cfg)}
    if not allow_call:
        return None, {"source": "none", "error": "live calls are disabled for this request",
                      "quota": quota_state(cfg)}

    # Authenticate BEFORE reserving. A token mint is not the metered call, so a
    # wrong secret must not cost a search from the day's allowance.
    headers, auth_err = _auth_headers(cfg)
    if auth_err:
        return None, {"source": "none", "error": auth_err, "quota": quota_state(cfg),
                      "hint": "check the key and secret; no search was spent"}

    ok, why = _reserve(cfg)
    if not ok:
        return None, {"source": "none", "error": why, "quota": quota_state(cfg)}

    secrets = (key, cfg.get("secret", "")) + tuple(headers.values())
    url = _url(cfg, query)
    req = urllib.request.Request(url, headers=dict(
        headers, **{"Accept": "application/json", "User-Agent": UA}))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code in (401, 403):
            token_clear()   # may be a retired token rather than a bad credential
        # The provider answered, so the call counted. No refund.
        return None, {"source": "none", "quota": quota_state(cfg),
                      "error": redact("HTTP %s from the provider: %s" % (exc.code, detail), *secrets),
                      "hint": ("check the key" if exc.code in (401, 403) else
                               "the provider is rate limiting or out of budget" if exc.code == 429 else
                               "the query shape in %s may not match this provider" % CONFIG_FILE)}
    except (urllib.error.URLError, OSError) as exc:
        _refund()   # never reached them; that one genuinely cost nothing
        return None, {"source": "none", "quota": quota_state(cfg),
                      "error": redact("could not reach the provider: %s" % exc, *secrets),
                      "refunded": True}

    try:
        payload = json.loads(body)
    except ValueError:
        return None, {"source": "none", "quota": quota_state(cfg),
                      "error": "the provider did not return JSON",
                      "first_bytes": redact(body[:200], *secrets)}

    cache_put(query, payload)
    return payload, {"source": "api", "quota": quota_state(cfg)}


def probe(cfg=None):
    """Validate a key with a REAL search.

    Deliberately not a cheaper or simpler call: a probe whose payload differs
    from the runtime one will bless a key that fails on every actual search,
    which is a failure mode this repo has already paid for once."""
    cfg = cfg or load_config()
    q = {"origin": "JFK", "destination": "LHR",
         "date": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 60 * 86400)),
         "adults": 1, "currency": "USD", "limit": 5}
    payload, meta = search(q, cfg)
    if payload is None:
        return {"ok": False, "why": meta.get("error"), "hint": meta.get("hint"),
                "quota": meta.get("quota")}
    n = len(payload.get("data") or payload.get("itineraries") or [])
    return {"ok": True, "source": meta["source"], "itineraries": n,
            "quota": meta.get("quota"),
            "provider": cfg.get("provider"),
            "note": ("the credentials work and the response parsed, but zero itineraries "
                     "came back - check the parameter names in %s against %s's docs"
                     % (CONFIG_FILE, cfg.get("provider"))) if not n else ""}


def status():
    cfg = load_config()
    mode = auth_mode(cfg)
    ready, missing = credentials_complete(cfg)
    tok = None
    if mode == "oauth2_client_credentials":
        try:
            with open(TOKEN_FILE, encoding="utf-8") as fh:
                t = json.load(fh)
            # The token itself is never reported - only whether one is held and
            # how long it has left.
            tok = {"held": bool(t.get("access_token")),
                   "for_current_credentials": t.get("fingerprint") == _cred_fingerprint(cfg),
                   "seconds_left": max(0, int(float(t.get("expires_at", 0)) - time.time()))}
        except (OSError, ValueError):
            tok = {"held": False, "for_current_credentials": False, "seconds_left": 0}
    return {
        "provider": cfg.get("provider"),
        "base": cfg.get("base"),
        "auth_mode": mode,
        "key_configured": bool(cfg.get("key")),
        "key_source": cfg.get("_key_source", "none"),
        "secret_configured": bool(cfg.get("secret")),
        "secret_source": cfg.get("_secret_source", "none"),
        "ready": ready,
        "not_ready_because": "" if ready else missing,
        "token": tok,
        "available_providers": sorted(PROFILES),
        "config_file": CONFIG_FILE,
        "config_error": cfg.get("_config_error"),
        "quota": quota_state(cfg),
        "cache": cache_stats(),
        "unverified": cfg.get("_note", ""),
    }


def set_provider(name):
    """Switch profiles, keeping any key/secret and quota settings already set."""
    if name not in PROFILES:
        return False, "unknown provider %r - have %s" % (name, ", ".join(sorted(PROFILES)))
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        existing = {}
    fresh = json.loads(json.dumps(PROFILES[name]))
    for keep in ("key", "secret", "quota"):
        if existing.get(keep):
            fresh[keep] = existing[keep]
    _atomic_write(CONFIG_FILE, fresh)
    # A bearer token minted for the previous provider is meaningless now.
    token_clear()
    return True, "provider is now %s (%s)" % (name, fresh["base"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        st = status()
        print(json.dumps(st, indent=2))
        if not st["key_configured"]:
            path, made = write_example_config()
            print("\nNo key configured. %s %s - add one, or export "
                  "CONCORDEGO_FLIGHT_KEY." % (path, "written" if made else "exists"))
    elif cmd == "probe":
        print(json.dumps(probe(), indent=2))
    elif cmd == "provider":
        if len(sys.argv) < 3:
            print("providers: %s\ncurrent:   %s"
                  % (", ".join(sorted(PROFILES)), load_config().get("provider")))
        else:
            ok, msg = set_provider(sys.argv[2])
            print(msg)
            raise SystemExit(0 if ok else 1)
    elif cmd == "search" and len(sys.argv) >= 5:
        payload, meta = search({"origin": sys.argv[2], "destination": sys.argv[3],
                                "date": sys.argv[4], "adults": 1, "currency": "USD"})
        print(json.dumps(meta, indent=2))
        if payload:
            print("payload keys: %s" % sorted(payload)[:10])
    else:
        print(__doc__)
