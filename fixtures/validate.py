#!/usr/bin/env python3
"""Validate every fixture against fixtures/schema.json, then run the checks a
JSON Schema cannot express.

    python3 fixtures/validate.py

The structural pass needs `jsonschema` (pip install jsonschema). Without it
that pass is skipped and the semantic checks below still run — those are the
ones that catch the mistakes that actually matter:

  * a stored answer (any field the scorer is supposed to compute)
  * a naive local time with no UTC offset
  * money as a float instead of integer cents
  * a scheduled arrival inside an airport's night curfew
  * a reference profile missing, so the grade has no fixed yardstick
  * an assertion pointing at an option_id that does not exist
"""

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
WARNS = []


def fail(f, msg):
    FAILS.append("%s: %s" % (os.path.basename(f), msg))


def warn(f, msg):
    WARNS.append("%s: %s" % (os.path.basename(f), msg))


# Airports with a scheduled-movement curfew. A fixture may carry a LATE ACTUAL
# inside the window — that is a delay, and delays are legal — but a SCHEDULED
# arrival inside it cannot be booked, and a fixture that pretends otherwise is
# confidently wrong in a way no reviewer would catch by eye.
CURFEW = {
    "CDG": {"arrivals": ("00:30", "05:29"), "departures": ("00:00", "04:59"),
            "source": "arretes of 6 Nov 2003"},
}

# Field names that would make a fixture its own answer key.
FORBIDDEN = re.compile(
    r"^(effective_cost|effective_cents|total_cents|score|grade|rank|"
    r"elapsed_minutes|block_minutes|duration_minutes|layover_minutes|"
    r"door_to_door_hours|door_to_door_minutes_total|arrival_day_offset|"
    r"is_codeshare|departure_utc|arrival_utc)$")

OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def walk(node, path, f):
    if isinstance(node, dict):
        for k, v in node.items():
            if FORBIDDEN.match(k):
                fail(f, "stored answer at %s.%s — the scorer must compute this, "
                        "or the fixture tests nothing" % (path, k))
            if k.endswith("_cents") and isinstance(v, float):
                fail(f, "%s.%s is a float; money is integer minor units, or ties "
                        "sort by rounding noise" % (path, k))
            if k in ("departure_local", "arrival_local", "actual_arrival_local") and v:
                if not OFFSET.match(v):
                    fail(f, "%s.%s = %r has no explicit UTC offset; a zone name "
                            "cannot disambiguate a repeated local hour" % (path, k, v))
            walk(v, "%s.%s" % (path, k), f)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, "%s[%d]" % (path, i), f)


def check_curfew(fx, f):
    for opt in fx.get("options", []):
        for seg in opt.get("segments", []):
            iata = (seg.get("destination") or {}).get("iata")
            rule = CURFEW.get(iata)
            if not rule:
                continue
            arr = seg.get("arrival_local", "")
            if not OFFSET.match(arr):
                continue
            clock = arr[11:16]
            lo, hi = rule["arrivals"]
            if hhmm(lo) <= hhmm(clock) <= hhmm(hi):
                fail(f, "%s/%s has a SCHEDULED arrival at %s into %s, inside the "
                        "%s-%s night ban (%s). Model it as a delay: keep a legal "
                        "scheduled time and put the late time in "
                        "actual_arrival_local."
                     % (opt["option_id"], seg["segment_id"], clock, iata, lo, hi,
                        rule["source"]))
            act = seg.get("actual_arrival_local")
            if act and OFFSET.match(act):
                a = act[11:16]
                if hhmm(lo) <= hhmm(a) <= hhmm(hi):
                    warn(f, "%s/%s lands at %s into %s, inside the night ban — "
                            "legal as a delay, which is what this models"
                         % (opt["option_id"], seg["segment_id"], a, iata))


def check_semantics(fx, f):
    ids = {o["option_id"] for o in fx.get("options", [])}
    if len(ids) != len(fx.get("options", [])):
        fail(f, "duplicate option_id")

    profs = fx.get("query", {}).get("profiles", {})
    if "reference" not in profs:
        fail(f, "no 'reference' profile — the grade has no fixed yardstick, so it "
                "would move when the user switches target")

    exp = fx.get("expect", {})
    for group in ("ledger_must_contain", "ledger_must_not_contain"):
        for a in exp.get(group, []):
            if a["option_id"] not in ids:
                fail(f, "%s references unknown option_id %r" % (group, a["option_id"]))
    for prof, order in (exp.get("order") or {}).items():
        if prof not in profs:
            fail(f, "expect.order names profile %r which the query does not define" % prof)
        if set(order) != ids:
            fail(f, "expect.order[%s] does not cover every option" % prof)

    for opt in fx.get("options", []):
        seg_ids = {s["segment_id"] for s in opt.get("segments", [])}
        covered = set()
        for t in opt.get("tickets", []):
            covered |= set(t.get("segment_ids", []))
        if covered != seg_ids:
            fail(f, "%s: tickets cover %s but segments are %s — every segment must "
                    "sit on exactly one ticketing boundary"
                 % (opt["option_id"], sorted(covered), sorted(seg_ids)))

        if len(opt.get("tickets", [])) > 1:
            for lay in opt.get("layovers", []):
                if lay.get("bags_checked_through"):
                    fail(f, "%s/%s claims bags check through, but this is a "
                            "self-transfer across separate tickets"
                         % (opt["option_id"], lay["layover_id"]))

        for lay in opt.get("layovers", []):
            for k in ("arrive_segment_id", "depart_segment_id"):
                if lay[k] not in seg_ids:
                    fail(f, "%s/%s.%s points at unknown segment %r"
                         % (opt["option_id"], lay["layover_id"], k, lay[k]))

        for t in opt.get("tickets", []):
            tiers = [x["piece"] for x in t.get("checked_bag_fee_tiers", [])]
            if tiers and tiers != sorted(tiers):
                fail(f, "%s/%s bag tiers out of order" % (opt["option_id"], t["ticket_id"]))
            if tiers and tiers[0] != 1:
                fail(f, "%s/%s bag tiers must index from 1 BEYOND the included "
                        "allowance, not from the traveller's first bag"
                     % (opt["option_id"], t["ticket_id"]))
            if t["price"]["currency"] != "USD" and t["price"].get("fx_rate_to_usd", 1.0) == 1.0:
                fail(f, "%s/%s is filed in %s with fx_rate_to_usd 1.0 — pin the real "
                        "rate or the conversion path never executes"
                     % (opt["option_id"], t["ticket_id"], t["price"]["currency"]))

            need = sum(1 for b in fx["query"]["party"][0]["bags"] if b["kind"] == "checked")
            beyond = max(0, need - t["entitlements"]["checked_included"])
            if beyond > len(tiers):
                fail(f, "%s/%s: traveller checks %d bag(s), fare includes %d, so %d "
                        "tier(s) are needed but only %d are priced"
                     % (opt["option_id"], t["ticket_id"], need,
                        t["entitlements"]["checked_included"], beyond, len(tiers)))

        for g in opt.get("ground", {}).get("outbound", []):
            if not g["feasible"] and not g.get("weakest_link"):
                fail(f, "%s/%s is infeasible with no weakest_link — the ledger has to "
                        "name the leg that breaks, never claim 'no transit'"
                     % (opt["option_id"], g["mode_id"]))
        if not any(g["feasible"] for g in opt.get("ground", {}).get("outbound", [])):
            fail(f, "%s has no feasible way to reach the airport" % opt["option_id"])


def main():
    files = sorted(f for f in glob.glob(os.path.join(HERE, "*.json"))
                   if not f.endswith("schema.json"))
    if not files:
        print("no fixtures found")
        return 1

    schema = None
    validator = None
    try:
        import jsonschema
        schema = json.load(open(os.path.join(HERE, "schema.json")))
        validator = jsonschema.Draft202012Validator(schema)
    except ImportError:
        print("  (jsonschema not installed — structural pass skipped)\n")

    for f in files:
        fx = json.load(open(f))
        name = os.path.basename(f)
        if validator:
            errs = sorted(validator.iter_errors(fx), key=lambda e: list(e.path))
            for e in errs[:6]:
                fail(f, "schema: %s at %s" % (e.message[:160], "/".join(str(x) for x in e.path)))
        walk(fx, "$", f)
        check_curfew(fx, f)
        check_semantics(fx, f)
        print("  checked %-34s %d options, %d assertions"
              % (name, len(fx.get("options", [])),
                 len(fx.get("expect", {}).get("ledger_must_contain", []))
                 + len(fx.get("expect", {}).get("ledger_must_not_contain", []))))

    print()
    for w in WARNS:
        print("  note  " + w)
    if FAILS:
        print()
        for x in FAILS:
            print("  FAIL  " + x)
        print("\n%d problem(s)" % len(FAILS))
        return 1
    print("\nall fixtures valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
