#!/usr/bin/env python3
"""Run one real search against the configured provider and record what came back.

    export CONCORDEGO_FLIGHT_KEY=duffel_test_...
    python3 concorde-travel/capture.py                     # JFK-LHR, 60 days out
    python3 concorde-travel/capture.py JFK LHR 2026-11-12

This exists because the machine that wrote the adapters cannot reach any flight
API - the sandbox's egress policy denies them - so the provider profiles are
written from published schemas and have never met the live service. This script
is how that gets fixed: it runs the REAL code path, writes the raw payload to
adapter_samples/, normalises it, and prints a diagnostic you can paste back.

THE KEY NEVER GOES IN THE OUTPUT. Everything written or printed goes through
live.redact(), and the saved payload is scrubbed for the key, the Authorization
header and anything key-shaped, before it touches disk. Read the file before you
send it anywhere - it is inventory data, not account data, but read it anyway.

A FAILURE HERE IS A USEFUL RESULT, not a wasted call. The most likely first
outcome is a 4xx because a parameter name in the profile is wrong, and the
report below is written to be pasteable in that case too.
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import adapter                                              # noqa: E402
import live                                                 # noqa: E402


def scrub(obj, *secrets):
    """Walk a payload and redact anything credential-shaped."""
    blob = json.dumps(obj)
    blob = live.redact(blob, *[s for s in secrets if s])
    out = json.loads(blob)

    def walk(node):
        if isinstance(node, dict):
            return {k: ("<redacted>" if k.lower() in
                        ("authorization", "api_key", "apikey", "access_token",
                         "client_secret", "secret", "token") else walk(v))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(out)


def describe(payload):
    """What the feed did and did not carry, in the terms the scorer needs."""
    sc = adapter.from_feed(payload, checked_bags=1)
    if sc.get("error"):
        return sc, None, ["NORMALISATION FAILED: %s" % sc["error"]] + [
            "  dropped %s: %s" % (d.get("id"), d.get("why"))
            for d in (sc.get("dropped") or sc.get("_dropped") or [])]
    cov = adapter.coverage(sc)
    lines = ["normalised %d options via %s"
             % (len(sc["options"]), (sc.get("_feed") or {}).get("provider", "kiwi")),
             "coverage: %s" % cov["verdict"]]
    for s in cov.get("strengths", []):
        lines.append("  + %s" % s)
    for g in cov["gaps"]:
        lines.append("  - %s" % g)
    for d in sc.get("_dropped", []):
        lines.append("  ! dropped %s: %s" % (d.get("id"), d.get("why")))
    for n in sc.get("notes", []):
        lines.append("  note: %s" % n)
    return sc, cov, lines


def main():
    args = sys.argv[1:]
    origin = args[0].upper() if len(args) > 0 else "JFK"
    dest = args[1].upper() if len(args) > 1 else "LHR"
    date = args[2] if len(args) > 2 else time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() + 60 * 86400))

    cfg = live.load_config()
    key = cfg.get("key") or ""
    secret = cfg.get("secret") or ""
    provider = cfg.get("provider", "?")
    ready, missing = live.credentials_complete(cfg)

    print("=" * 72)
    print("ConcordeGo capture · provider=%s · %s->%s on %s" % (provider, origin, dest, date))
    print("=" * 72)
    print("base          : %s" % cfg.get("base"))
    print("auth          : %s" % live.auth_mode(cfg))
    print("key           : %s (from %s)"
          % ("present" if key else "MISSING", cfg.get("_key_source", "none")))
    if not ready:
        print("\nNOT READY: %s" % missing)
        print("Set CONCORDEGO_FLIGHT_KEY (and _SECRET on an OAuth2 provider), then re-run.")
        return 2

    q = {"origin": origin, "destination": dest, "date": date,
         "adults": 1, "currency": "USD", "limit": 20}
    print("request       : %s" % live._url(cfg, q))
    if str(cfg.get("method", "GET")).upper() == "POST":
        print("body          : %s" % json.dumps(live._render_body(cfg, q)))

    t0 = time.time()
    payload, meta = live.search(q)
    took = time.time() - t0

    print("\n--- result ---------------------------------------------------------")
    print("source        : %s   (%.1fs)" % (meta.get("source"), took))
    print("quota left    : %s today, %s this month"
          % (meta["quota"]["day_left"], meta["quota"]["month_left"]))
    if payload is None:
        print("\nTHE CALL FAILED. This is the useful part - paste everything below.\n")
        print("error         : %s" % meta.get("error"))
        print("hint          : %s" % (meta.get("hint") or meta.get("how") or ""))
        print("\nThe wire format lives in %s, not in Python. If a parameter name is"
              "\nwrong, it is fixable there." % live.CONFIG_FILE)
        return 1

    n = live._count_results(payload)
    print("results       : %d" % n)
    if not n:
        print("\nZERO RESULTS but the credentials worked and the response parsed."
              "\nThat usually means a parameter name is wrong rather than the key.")

    stem = "%s-%s-%s-REAL" % (provider.split("-")[0], origin.lower(), dest.lower())
    path = os.path.join(HERE, "adapter_samples", stem + ".json")
    clean = scrub(payload, key, secret)
    clean["_provenance"] = {
        "what": "%s %s-%s on %s, one adult." % (provider, origin, dest, date),
        "captured": True,
        "how": "Captured off the wire by concorde-travel/capture.py. Scrubbed for "
               "credentials before writing. Prices and schedules were real at capture "
               "time and are now frozen; this is a SHAPE fixture, not live data.",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2, sort_keys=False)
    print("saved         : %s" % path)

    print("\n--- what the adapter made of it ------------------------------------")
    sc, cov, lines = describe(clean)
    for ln in lines:
        print(ln)

    if sc and not sc.get("error"):
        import scorer
        print("\n--- scored, one checked bag ----------------------------------------")
        by = {o["option_id"]: o for o in sc["options"]}
        for row in scorer.score_all(sc, "reference")[:8]:
            o = by[row.option_id]
            print("  %-42s %9s   %s" % (o["display_name"][:42],
                                        scorer._money(row.effective_cents),
                                        scorer.grade(sc, o)[0]))

    print("\n--- send me ---------------------------------------------------------")
    print("1. everything printed above")
    print("2. the file at %s" % path)
    print("   (read it first - it is inventory, not account data, but read it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
