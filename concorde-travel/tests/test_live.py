#!/usr/bin/env python3
"""Credentials, quota and cache — tested without a key or a network.

    python3 concorde-travel/tests/test_live.py

Everything here runs against a temporary HOME, so it never reads or writes the
real ~/.concordego and can be run on a machine with a live key configured
without touching its counters.

The two properties worth having tests for are the ones with consequences: a
quota bug spends someone's money, and a redaction bug leaks their key into a log.
"""

import io
import json
import os
import sys
import tempfile
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import live                                                 # noqa: E402

FAILS, N = [], [0]
# Deliberately not key-shaped. Every assertion below only ever asks whether this
# string is present or absent, so its shape guards nothing - and a realistic-looking
# "tq_live_..." here trips GitHub secret scanning on a value that was never a key.
SECRET = "NOT-A-REAL-KEY-test-fixture-only"
SECRET2 = "NOT-A-REAL-SECRET-test-fixture-only"
TOKEN = "NOT-A-REAL-BEARER-TOKEN-test-fixture-only"


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + (("  — " + detail) if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


def sandbox(tmp):
    """Point the module at a throwaway home."""
    live.HOME = tmp
    live.CONFIG_FILE = os.path.join(tmp, "cloud.json")
    live.QUOTA_FILE = os.path.join(tmp, "quota.json")
    live.TOKEN_FILE = os.path.join(tmp, "token.json")
    live.CACHE_DIR = os.path.join(tmp, "cache")


def cfg_with(profile="kiwi-tequila", **over):
    """Quota, cache and redaction are provider-independent, so they are exercised
    on the simpler header-key profile. The OAuth2 path gets its own section."""
    c = json.loads(json.dumps(live.PROFILES[profile]))
    c["key"] = SECRET
    if live.auth_mode(c) == "oauth2_client_credentials":
        c["secret"] = SECRET2
    c["quota"].update(over.pop("quota", {}))
    c.update(over)
    return c


def main():
    tmp = tempfile.mkdtemp(prefix="cgo-live-")
    sandbox(tmp)
    Q = {"origin": "JFK", "destination": "LHR", "date": "2026-11-12",
         "adults": 1, "currency": "USD"}

    # ---- quota arithmetic -------------------------------------------
    print("\nquota")
    cfg = cfg_with(quota={"per_day": 2, "per_month": 5,
                          "min_seconds_between_calls": 0, "cache_ttl_seconds": 600})
    ok1, _ = live._reserve(cfg)
    ok2, _ = live._reserve(cfg)
    ok3, why3 = live._reserve(cfg)
    check("a daily allowance is spendable", ok1 and ok2)
    check("and then refused, by name", not ok3 and "daily limit" in why3, why3)
    check("the counter persists to disk", live.load_quota()["day_calls"] == 2)

    live._refund()
    check("a refund returns exactly one call", live.load_quota()["day_calls"] == 1)

    q = live.load_quota()
    q["day"] = "2000-01-01"
    live._atomic_write(live.QUOTA_FILE, q)
    check("the daily counter rolls over on its own without a cron job",
          live.load_quota()["day_calls"] == 0)

    cfg2 = cfg_with(quota={"per_day": 50, "per_month": 50,
                           "min_seconds_between_calls": 60, "cache_ttl_seconds": 600})
    live._reserve(cfg2)
    ok, why = live._reserve(cfg2)
    check("the minimum interval between calls is enforced",
          not ok and "rate limited" in why, why)

    # ---- the cache must not spend quota ------------------------------
    print("\ncache")
    for f in os.listdir(tmp):
        if f.startswith("quota"):
            os.remove(os.path.join(tmp, f))
    cfg3 = cfg_with(quota={"per_day": 1, "per_month": 1,
                           "min_seconds_between_calls": 0, "cache_ttl_seconds": 600})
    live.cache_put(Q, {"itineraries": [{"id": "x"}]})
    before = live.load_quota()["day_calls"]
    payload, meta = live.search(Q, cfg3)
    after = live.load_quota()["day_calls"]
    check("a cache hit is served", meta["source"] == "cache" and payload is not None)
    check("and costs no quota", before == after == 0,
          "a cache that spends a call is not a cache")

    stale = live.cache_get(Q, ttl=0)
    check("a stale entry is not served", stale is None)

    other = dict(Q, destination="CDG")
    check("a different query is a different cache entry",
          live.cache_key(Q) != live.cache_key(other))
    check("and key order does not change the key",
          live.cache_key(Q) == live.cache_key(dict(reversed(list(Q.items())))))

    # ---- the key must never escape -----------------------------------
    print("\nsecrecy")
    leaks = []

    def watch(obj, where):
        if SECRET in json.dumps(obj, default=str):
            leaks.append(where)

    # no key at all
    nokey = cfg_with()
    nokey.pop("key")
    _, meta = live.search(dict(Q, date="2026-11-13"), nokey)
    check("with no key, it says so rather than calling", meta["error"] == "no API key configured")
    check("and no key means no call, so nothing was spent", live.load_quota()["day_calls"] == 0)
    watch(meta, "no-key path")

    # the provider refuses
    real_open = live.urllib.request.urlopen

    def http_error(*a, **k):
        raise urllib.error.HTTPError("https://x/v2/search?apikey=" + SECRET,
                                     403, "Forbidden", {},
                                     io.BytesIO(b'{"error":"bad key ' + SECRET.encode() + b'"}'))
    live.urllib.request.urlopen = http_error
    try:
        cfg4 = cfg_with(quota={"per_day": 9, "per_month": 9,
                               "min_seconds_between_calls": 0, "cache_ttl_seconds": 600})
        _, meta = live.search(dict(Q, date="2026-11-14"), cfg4)
        watch(meta, "HTTP error path")
        check("a 403 is reported with a usable hint",
              "403" in meta["error"] and "key" in (meta.get("hint") or ""))
        check("and the key is redacted out of the provider's own echo",
              SECRET not in json.dumps(meta), meta.get("error", "")[:120])
        check("a call the provider answered is NOT refunded",
              live.load_quota()["day_calls"] == 1,
              "they served the request; it counted")
    finally:
        live.urllib.request.urlopen = real_open

    # the network is down
    def net_error(*a, **k):
        raise urllib.error.URLError("nodename nor servname provided")
    live.urllib.request.urlopen = net_error
    try:
        before = live.load_quota()["day_calls"]
        _, meta = live.search(dict(Q, date="2026-11-15"), cfg4)
        watch(meta, "network error path")
        check("a call that never reached the provider IS refunded",
              meta.get("refunded") and live.load_quota()["day_calls"] == before)
    finally:
        live.urllib.request.urlopen = real_open

    # ---------------------------------------------------------------- oauth2
    # Amadeus needs a client id AND a secret, exchanged for a short-lived bearer
    # token. That is a different failure surface from a static header key, and
    # every one of its ways to go wrong costs either money or a locked-out key.
    live.token_clear()
    minted = {"n": 0}
    searched = {"n": 0, "auth": None, "url": None}

    class FakeResp:
        def __init__(self, body):
            self._b = body.encode() if isinstance(body, str) else body
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_open(req, *a, **k):
        url = req.full_url
        if "/oauth2/token" in url:
            minted["n"] += 1
            body = req.data.decode()
            if SECRET not in body or SECRET2 not in body:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {},
                                             io.BytesIO(b'{"error":"invalid_client"}'))
            return FakeResp(json.dumps({"access_token": TOKEN, "expires_in": 1799,
                                        "token_type": "Bearer"}))
        searched["n"] += 1
        searched["auth"] = req.headers.get("Authorization")
        searched["url"] = url
        return FakeResp(json.dumps({"data": [{"type": "flight-offer", "id": "1"}]}))

    oa = cfg_with("amadeus", quota={"per_day": 9, "per_month": 9,
                                    "min_seconds_between_calls": 0,
                                    "cache_ttl_seconds": 600})

    half = json.loads(json.dumps(oa))
    half.pop("secret")
    ok, why = live.credentials_complete(half)
    check("an oauth2 provider with an id but no secret is not 'configured'", not ok)
    before = live.load_quota()["day_calls"]
    _, meta = live.search(dict(Q, date="2026-12-01"), half)
    check("a half-credential refuses before spending a call",
          "secret" in meta["error"] and live.load_quota()["day_calls"] == before,
          meta.get("error", ""))

    live.urllib.request.urlopen = fake_open
    try:
        payload, meta = live.search(dict(Q, date="2026-12-02"), oa)
        watch(meta, "oauth2 search")
        check("an oauth2 search mints a token and calls the search endpoint",
              payload is not None and minted["n"] == 1 and searched["n"] == 1)
        check("and it presents the token as a bearer header",
              searched["auth"] == "Bearer " + TOKEN, str(searched["auth"])[:40])
        check("the url carries amadeus's own parameter names, from the profile",
              "originLocationCode=JFK" in searched["url"]
              and "departureDate=2026-12-02" in searched["url"]
              and "max=" in searched["url"], searched["url"] or "")

        # A token is good for ~30 minutes. Re-minting per search would triple
        # the latency of every request for nothing.
        payload, meta = live.search(dict(Q, date="2026-12-03"), oa)
        check("a second search reuses the cached token rather than re-minting",
              minted["n"] == 1 and searched["n"] == 2, "mints=%d" % minted["n"])

        # A token minted with the old key must not be replayed after a rotation.
        rotated = json.loads(json.dumps(oa))
        rotated["key"] = SECRET + "-rotated"
        try:
            live.search(dict(Q, date="2026-12-04"), rotated)
        except Exception:
            pass
        check("rotating the credentials invalidates the cached token",
              minted["n"] == 2, "mints=%d" % minted["n"])

        # A bad credential is the likeliest day-one mistake. It must not cost a
        # search from the allowance, because the metered endpoint was never hit.
        bad = json.loads(json.dumps(oa))
        bad["secret"] = "wrong"
        live.token_clear()
        before = live.load_quota()["day_calls"]
        _, meta = live.search(dict(Q, date="2026-12-05"), bad)
        check("a failed token mint spends NO quota",
              live.load_quota()["day_calls"] == before,
              "the metered endpoint was never reached")
        check("and it says the credentials are the problem",
              "401" in (meta.get("error") or "") and "no search was spent" in (meta.get("hint") or ""),
              (meta.get("error") or "")[:80])
        check("the secret is redacted out of a token-endpoint failure",
              SECRET2 not in json.dumps(meta) and SECRET not in json.dumps(meta))

        # A 401 on the SEARCH can be a retired token rather than a bad key.
        live.token_clear()
        live.search(dict(Q, date="2026-12-06"), oa)          # re-mint and cache
        held_before = live._token_load(oa)
        def expired_search(req, *a, **k):
            if "/oauth2/token" in req.full_url:
                return fake_open(req)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"error":"expired token"}'))
        live.urllib.request.urlopen = expired_search
        live.search(dict(Q, date="2026-12-07"), oa)
        check("a 401 on the search drops the cached token so the next call re-mints",
              held_before and live._token_load(oa) is None)
    finally:
        live.urllib.request.urlopen = real_open

    check("the bearer token itself never appears in status()",
          TOKEN not in json.dumps(live.status()))
    check("status() reports the auth mode so a half-configured key reads as such",
          live.status()["auth_mode"] == "oauth2_client_credentials"
          or live.status()["provider"] != "amadeus")
    ok, msg = live.set_provider("kiwi-tequila")
    check("switching provider rewrites the profile and keeps nothing stale",
          ok and json.load(open(live.CONFIG_FILE))["provider"] == "kiwi-tequila", msg)
    check("switching provider drops a token minted for the old one",
          not os.path.exists(live.TOKEN_FILE))
    ok, _ = live.set_provider("nope")
    check("an unknown provider is refused rather than written", not ok)

    check("the key never appears in anything returned", not leaks, ", ".join(leaks))
    check("redact() shortens a secret rather than echoing it",
          SECRET not in live.redact("key is " + SECRET, SECRET))
    check("redact() also catches a key-shaped query string it was not told about",
          "hunter2" not in live.redact("GET /v2/search?apikey=hunter2&x=1"))

    # ---- status is safe to show --------------------------------------
    print("\nstatus")
    os.environ["CONCORDEGO_FLIGHT_KEY"] = SECRET
    try:
        st = live.status()
        check("status reports a key is present", st["key_configured"])
        check("status never contains the key itself", SECRET not in json.dumps(st))
        check("and names where it came from", "CONCORDEGO_FLIGHT_KEY" in st["key_source"])
    finally:
        os.environ.pop("CONCORDEGO_FLIGHT_KEY", None)

    # ---- the probe uses the runtime path ------------------------------
    print("\nprobe")
    src = open(os.path.join(HERE, "..", "live.py"), encoding="utf-8").read()
    body = src[src.index("def probe("):src.index("def status(")]
    check("probe() calls search() rather than its own cheaper request",
          "search(q, cfg)" in body,
          "a probe shaped differently from the runtime call validates keys that then fail")
    check("probe() does not invent a separate validation endpoint",
          "urlopen" not in body and "/v1/" not in body)

    # ---- the example config carries no secret --------------------------
    print("\nconfig")
    os.remove(live.CONFIG_FILE) if os.path.exists(live.CONFIG_FILE) else None
    path, made = live.write_example_config()
    written = json.load(open(path, encoding="utf-8"))
    check("an example config is written when none exists", made)
    check("with an empty key", written.get("key") == "")
    mode = os.stat(path).st_mode & 0o777
    check("and 0600 permissions", mode == 0o600, "got %o" % mode)
    check("the shipped default warns that the wire format is unverified",
          "UNVERIFIED" in live.DEFAULTS["_note"])

    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    if FAILS:
        print()
        for f in FAILS:
            print("  FAIL  " + f)
        return 1
    print("all live checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
