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
    live.CACHE_DIR = os.path.join(tmp, "cache")


def cfg_with(**over):
    c = json.loads(json.dumps(live.DEFAULTS))
    c["key"] = SECRET
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
