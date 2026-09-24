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


def req_method_is_post(seen):
    """urllib picks POST purely from `data` being set, so the body's presence
    IS the method - assert on what actually goes on the wire."""
    return seen["body"] is not None


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



class _Body:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serp_checks():
    """SerpApi, the Delta supplement: its own key and counters, the same discipline."""
    import io
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    print("\nserpapi supplement")
    tmp = tempfile.mkdtemp()
    sandbox(tmp)
    os.environ.pop("CONCORDEGO_SERPAPI_KEY", None)
    p, m = live.serp_search("JFK", "LHR", "2026-11-18")
    check("with no SerpApi key nothing is called, and the reason names the variable",
          p is None and "CONCORDEGO_SERPAPI_KEY" in (m.get("how") or ""))
    check("and no call was spent", m["quota"]["day_calls"] == 0)
    os.environ["CONCORDEGO_SERPAPI_KEY"] = SECRET
    real = live.urllib.request.urlopen
    scfg = live.serp_config()
    scfg["quota"]["min_seconds_between_calls"] = 0          # the floor is a real limit; the test is about the rest
    try:
        with open(os.path.join(here, "..", "adapter_samples", "serpapi-jfk-lhr.json"), encoding="utf-8") as fh:
            sample = json.load(fh)
        seen = {"n": 0, "url": ""}

        def fake_open(req, timeout=0):
            seen["n"] += 1
            seen["url"] = req.full_url
            return _Body(json.dumps(sample).encode())
        live.urllib.request.urlopen = fake_open
        p, m = live.serp_search("JFK", "LHR", "2026-11-18", cfg=scfg)
        check("a search with a key reaches SerpApi once and comes back as api",
              p is not None and m["source"] == "api" and seen["n"] == 1)
        check("by default the request asks Google Flights for EVERY airline (no include_airlines), one way, in dollars",
              all(x in seen["url"] for x in ("engine=google_flights", "type=2",
                                              "currency=USD", "departure_id=JFK", "arrival_id=LHR",
                                              "outbound_date=2026-11-18")) and "include_airlines" not in seen["url"]
              and scfg["carriers"] == ["*"] and live.serp_want() == (), seen["url"].replace(SECRET, "[key]"))
        check("one call was spent, on SerpApi's OWN counter, not the feed's",
              m["quota"]["day_calls"] == 1 and live.quota_state()["day_calls"] == 0)
        p2, m2 = live.serp_search("JFK", "LHR", "2026-11-18", cfg=scfg)
        check("the same search again is served from cache and spends nothing",
              m2["source"] == "cache" and seen["n"] == 1 and m2["quota"]["day_calls"] == 1)

        def http_error(req, timeout=0):
            raise live.urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                              io.BytesIO(("bad key " + SECRET).encode()))
        live.urllib.request.urlopen = http_error
        p3, m3 = live.serp_search("JFK", "CDG", "2026-11-18", cfg=scfg)
        check("a refused key is reported without the key in it",
              p3 is None and SECRET not in json.dumps(m3) and "401" in m3["error"])
        check("and the refused call still counted", m3["quota"]["day_calls"] == 2)

        def net_error(req, timeout=0):
            raise live.urllib.error.URLError("no route")
        live.urllib.request.urlopen = net_error
        p4, m4 = live.serp_search("JFK", "AMS", "2026-11-18", cfg=scfg)
        check("a call that never reached SerpApi is refunded",
              bool(m4.get("refunded")) and m4["quota"]["day_calls"] == 2)

        def soft_error(req, timeout=0):
            return _Body(json.dumps({"error": "Google hasn't returned any results for this query."}).encode())
        live.urllib.request.urlopen = soft_error
        p5, m5 = live.serp_search("JFK", "FRA", "2026-11-18", cfg=scfg)
        check("a 200 carrying an error sentence is a failure, not a payload",
              p5 is None and "Google" in m5["error"])
        p6, m6 = live.serp_search("JFK", "FRA", "2026-11-18", cfg=scfg)
        check("and it was not cached", m6["source"] != "cache")
        st = live.status()
        check("status reports the SerpApi key as configured, from the environment, and never prints it",
              st["serpapi"]["key_configured"] and "CONCORDEGO_SERPAPI_KEY" in st["serpapi"]["key_source"]
              and SECRET not in json.dumps(st))
        live.urllib.request.urlopen = fake_open
        live.serp_search("JFK", "LHR", "2026-11-19", cfg=dict(scfg, carriers=["DL"]))
        check("configured to a list, it asks for those airlines only (include_airlines=DL)",
              "include_airlines=DL" in seen["url"], seen["url"].replace(SECRET, "[key]"))
    finally:
        live.urllib.request.urlopen = real
        os.environ.pop("CONCORDEGO_SERPAPI_KEY", None)



def cabin_checks():
    """The cabin the page asked for reaches the provider's body; nothing asked for is economy."""
    print("\ncabin")
    d = json.loads(json.dumps(live.PROFILES["duffel"]))
    body = live._render_body(d, {"origin": "JFK", "destination": "LHR", "date": "2026-11-18", "adults": 1, "cabin": "business"})
    check("a business search asks Duffel for business", body["data"]["cabin_class"] == "business")
    body2 = live._render_body(d, {"origin": "JFK", "destination": "LHR", "date": "2026-11-18", "adults": 1})
    check("no cabin asked for is economy", body2["data"]["cabin_class"] == "economy")



def adb_checks():
    """AeroDataBox, the Flight Fixer's tracking feed: its own key and counters, the same discipline."""
    import io
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    print("\naerodatabox tracking")
    tmp = tempfile.mkdtemp()
    sandbox(tmp)
    os.environ.pop("CONCORDEGO_AERODATABOX_KEY", None)
    legs, m = live.flight_status("DL 5048", "2026-09-21")
    check("with no key nothing is called, and the reason names the variable", legs is None and "CONCORDEGO_AERODATABOX_KEY" in (m.get("how") or ""))
    check("a string that is not a flight number is refused before anything else", live.flight_status("hello there", "2026-09-21")[0] is None)
    os.environ["CONCORDEGO_AERODATABOX_KEY"] = SECRET
    real = live.urllib.request.urlopen
    cfg = live.adb_config(); cfg["quota"]["min_seconds_between_calls"] = 0
    try:
        with open(os.path.join(here, "..", "adapter_samples", "aerodatabox-dl5048.json"), encoding="utf-8") as fh:
            sample = json.load(fh)
        seen = {"n": 0, "url": "", "hdr": {}}

        def fake_open(req, timeout=0):
            seen["n"] += 1; seen["url"] = req.full_url; seen["hdr"] = dict(req.header_items())
            return _Body(json.dumps(sample).encode())
        live.urllib.request.urlopen = fake_open
        legs, m = live.flight_status("DL 5048", "2026-09-21", cfg=cfg)
        check("a lookup with a key reaches AeroDataBox once, by number and date, and comes back as api",
              legs is not None and m["source"] == "api" and seen["n"] == 1 and "/flights/number/DL5048/2026-09-21" in seen["url"])
        check("the key rides in the RapidAPI header, not the address",
              any(k.lower() == "x-rapidapi-key" and v == SECRET for k, v in seen["hdr"].items()) and SECRET not in seen["url"])
        check("one call was spent, on the feed's OWN counter", m["quota"]["day_calls"] == 1 and live.quota_state()["day_calls"] == 0)
        legs2, m2 = live.flight_status("DL5048", "2026-09-21", cfg=cfg)
        check("the same lookup again, spaces or not, is served from cache", m2["source"] == "cache" and seen["n"] == 1)

        def http_error(req, timeout=0):
            raise live.urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(("not subscribed " + SECRET).encode()))
        live.urllib.request.urlopen = http_error
        l3, m3 = live.flight_status("AA 100", "2026-09-21", cfg=cfg)
        check("a refused key is reported without the key in it", l3 is None and SECRET not in json.dumps(m3) and "403" in m3["error"])

        def nothing(req, timeout=0):
            raise live.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))
        live.urllib.request.urlopen = nothing
        l4, m4 = live.flight_status("ZZ 1", "2026-09-21", cfg=cfg)
        check("no flight known that day is an answer (an empty list), not an error", l4 == [] and m4["source"] == "api")
        st = live.status()
        check("status reports the tracking key as configured without printing it",
              st["aerodatabox"]["key_configured"] and SECRET not in json.dumps(st))
    finally:
        live.urllib.request.urlopen = real
        os.environ.pop("CONCORDEGO_AERODATABOX_KEY", None)


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

    # ---------------------------------------------------------------- duffel
    # A POST provider with a JSON body and a static bearer key: a third auth and
    # transport shape, and the only one you can currently sign up for.
    seen = {"n": 0, "body": None, "hdrs": None, "url": None}

    def duffel_open(req, *a, **k):
        seen["n"] += 1
        seen["url"] = req.full_url
        seen["hdrs"] = dict(req.headers)
        seen["body"] = json.loads(req.data.decode()) if req.data else None
        return FakeResp(json.dumps({"data": {"id": "orq_x", "offers": [
            {"id": "off_1", "slices": []}, {"id": "off_2", "slices": []}]}}))

    df = cfg_with("duffel", quota={"per_day": 9, "per_month": 9,
                                   "min_seconds_between_calls": 0,
                                   "cache_ttl_seconds": 600})
    live.urllib.request.urlopen = duffel_open
    try:
        payload, meta = live.search(dict(Q, date="2027-01-05", adults=2), df)
        watch(meta, "duffel search")
        check("a duffel search is issued as a POST with a JSON body",
              seen["body"] is not None and req_method_is_post(seen))
        check("the query travels in the body, not the query string",
              seen["body"]["data"]["slices"][0]["origin"] == "JFK"
              and seen["body"]["data"]["slices"][0]["departure_date"] == "2027-01-05",
              json.dumps(seen["body"])[:120])
        check("one passenger object per adult is built from the template",
              seen["body"]["data"]["passengers"] == [{"type": "adult"}, {"type": "adult"}],
              json.dumps(seen["body"]["data"]["passengers"]))
        check("the static switches still ride on the URL",
              "return_offers=true" in seen["url"], seen["url"])
        check("a static key is presented with its scheme",
              seen["hdrs"].get("Authorization") == "Bearer " + SECRET,
              str(seen["hdrs"].get("Authorization"))[:30])
        check("the provider's required version header is sent",
              seen["hdrs"].get("Duffel-version") == "v2"
              or seen["hdrs"].get("Duffel-Version") == "v2", json.dumps(seen["hdrs"]))
        check("and our own User-Agent, because a bare urllib one gets 403'd",
              "ConcordeGo" in str(seen["hdrs"].get("User-agent")
                                  or seen["hdrs"].get("User-Agent")))
        check("a nested duffel payload counts its offers, not its keys",
              live._count_results(payload) == 2, str(live._count_results(payload)))
        # No OAuth2 here, so nothing should have been minted or stored.
        check("a static-key provider mints no token", live._token_load(df) is None)
    finally:
        live.urllib.request.urlopen = real_open

    check("counting handles all three response shapes",
          live._count_results({"data": [1, 2, 3]}) == 3
          and live._count_results({"itineraries": [1]}) == 1
          and live._count_results({}) == 0 and live._count_results(None) == 0)
    check("the shipped default is the provider you can actually sign up for",
          live.DEFAULTS["provider"] == "duffel", live.DEFAULTS["provider"])
    check("and its note says a test token returns invented inventory",
          "invented" in live.PROFILES["duffel"]["_note"])

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
    check("the shipped default says its wire format was verified against the live service",
          "VERIFIED against the live service" in live.DEFAULTS["_note"] and "UNVERIFIED" not in live.DEFAULTS["_note"])

    cabin_checks()
    serp_checks()
    adb_checks()
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
