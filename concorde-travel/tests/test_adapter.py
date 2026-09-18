#!/usr/bin/env python3
"""The live adapter, tested against a recorded payload.

    python3 concorde-travel/tests/test_adapter.py

No network. The sample under adapter_samples/ is a real Kiwi.com response
captured once; every run replays it. That is what lets a live adapter exist in
a product whose fifth hard rule is "no live flight API dependency in the scorer
or its tests" - the dependency is recorded, not live.

The checks that matter here are mostly about what the adapter REFUSES to do. A
feed that omits the operating carrier, the equipment code and the baggage
schedule can be turned into something that looks complete very easily, and the
result would grade flights on invented facts.
"""

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import adapter                                              # noqa: E402
import scorer                                               # noqa: E402

FAILS, N = [], [0]


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + (("  — " + detail) if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


def main():
    sample = os.path.join(HERE, "..", "adapter_samples", "kiwi-jfk-lhr.json")
    raw = json.load(open(sample, encoding="utf-8"))
    sc = adapter.from_kiwi(raw)
    cov = adapter.coverage(sc)

    print("\nnormalisation")
    check("the recorded payload produces options", bool(sc.get("options")),
          sc.get("error", ""))
    check("every option carries a route par", bool(sc["query"]["route_par_cents"]))

    # --- the offset join, which the feed cannot do for itself -----------
    print("\ntime")
    stamps = [s["departure_local"] for o in sc["options"] for s in o["segments"]]
    check("every timestamp gained an explicit UTC offset",
          all(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", t) for t in stamps),
          "first few: %s" % stamps[:2])
    jfk = [s for o in sc["options"] for s in o["segments"] if s["origin"]["iata"] == "JFK"]
    check("12 Nov 2026 at JFK resolves to EST, not EDT",
          all(s["departure_local"].endswith("-05:00") for s in jfk),
          "US daylight saving ended 1 Nov 2026; got %s"
          % {s["departure_local"][-6:] for s in jfk})
    lhr = [s for o in sc["options"] for s in o["segments"] if s["destination"]["iata"] == "LHR"]
    check("and the same date in London resolves to GMT",
          all(s["arrival_local"].endswith("+00:00") for s in lhr))
    # the scorer's own elapsed time must match what the feed said it was
    mism = []
    for it in raw["itineraries"]:
        segs = it["outbound"]["segments"]
        for o in sc["options"]:
            if len(o["segments"]) != len(segs):
                continue
            if o["segments"][0]["marketing"]["carrier"] != segs[0]["carrier"]:
                continue
            for a, b in zip(o["segments"], segs):
                got = scorer.minutes_between(a["departure_local"], a["arrival_local"])
                want = b["durationSeconds"] // 60
                if got != want:
                    mism.append("%s %d vs %d" % (b["flightNumber"], got, want))
            break
    check("elapsed time derived from the offsets matches the feed's own duration",
          not mism, "; ".join(mism[:4]))

    # --- what it refuses to claim ---------------------------------------
    print("\nrefusals")
    segs = [s for o in sc["options"] for s in o["segments"]]
    check("no equipment code is invented",
          all(s["equipment_code"] == "UNKNOWN" for s in segs))
    check("every aircraft and connectivity claim abstains",
          all(c.get("coverage") == "none"
              for s in segs for c in s["claims"].values() if isinstance(c, dict)))
    check("every reliability record abstains",
          all(s["reliability"].get("coverage") == "none" for s in segs))
    check("no baggage fee schedule is invented",
          all(not t["checked_bag_fee_tiers"] for o in sc["options"] for t in o["tickets"]),
          "the feed prices bags inline and never exposes a tier table")
    codeshare = [s for s in segs
                 if "codeshare" in s["claims"]["subfleet"].get("reason", "")]
    check("codeshares are detected and named", len(codeshare) >= 5,
          "found %d of %d segments" % (len(codeshare), len(segs)))

    # --- ticket topology, which drives bags and risk ---------------------
    print("\nticket topology")
    multi = [o for o in sc["options"] if len(o["tickets"]) > 1]
    check("a self-transfer is recognised as two tickets", bool(multi),
          "the IB/VY itinerary in the sample is sold as separate tickets")
    # A three-segment, two-ticket itinerary has BOTH kinds of layover: one
    # inside a ticket, which IS through-checked, and one crossing the boundary,
    # which is not. Demanding that every layover on a self-transfer be
    # unprotected was the test being wrong, not the adapter.
    crossing = inside = 0
    for o in multi:
        owner = {}
        for t in o["tickets"]:
            for sid in t["segment_ids"]:
                owner[sid] = t["ticket_id"]
        for l in o.get("layovers") or []:
            same = owner[l["arrive_segment_id"]] == owner[l["depart_segment_id"]]
            if same:
                inside += 1
                check("a layover INSIDE one ticket keeps its through-check",
                      l["bags_checked_through"] and l["recovery"]["protected"])
            else:
                crossing += 1
                check("a layover ACROSS two tickets loses the through-check",
                      not l["bags_checked_through"] and not l["recovery"]["protected"])
    check("the sample exercises both kinds of layover boundary",
          crossing >= 1 and inside >= 1,
          "crossing=%d inside=%d" % (crossing, inside))
    through = [o for o in sc["options"] if len(o["tickets"]) == 1 and len(o["segments"]) > 1]
    check("a through-ticketed connection is protected",
          all(l["recovery"]["protected"] for o in through for l in o["layovers"]),
          "%d through-ticketed multi-segment options" % len(through))

    # --- scope discipline ------------------------------------------------
    print("\nscope")
    # Assert the MECHANISM rather than relying on the sample containing an
    # airport we happen not to have curated - curating one later should not
    # quietly delete this guard.
    import copy
    thin = copy.deepcopy(adapter.load_enrichment())
    thin["airports"]["airports"].pop("AMS", None)
    thin_sc = adapter.from_kiwi(raw, enr=thin)
    check("an uncurated airport drops the itinerary rather than guessing",
          any("AMS" in d["why"] for d in thin_sc["_dropped"]),
          "dropped: %s" % [d["why"] for d in thin_sc["_dropped"]])
    check("and the drop is reported, not silent",
          any("dropped an itinerary" in g for g in adapter.coverage(thin_sc)["gaps"]))
    check("nothing that survived the drop lost its offsets",
          all(len(sg["departure_local"]) == 25
              for o in thin_sc["options"] for sg in o["segments"]))
    check("coverage says grades are only indicative here",
          "indicative" in cov["verdict"], cov["verdict"])
    check("coverage names the missing equipment data",
          any("equipment code" in g for g in cov["gaps"]))

    # --- it is a scenario like any other ---------------------------------
    print("\ndownstream")
    clean = {k: v for k, v in sc.items() if not k.startswith("_")}
    try:
        import jsonschema
        schema = json.load(open(os.path.join(HERE, "..", "..", "fixtures", "schema.json"),
                                encoding="utf-8"))
        errs = list(jsonschema.Draft202012Validator(schema).iter_errors(clean))
        check("a live scenario validates against the fixture schema", not errs,
              "; ".join(e.message[:90] for e in errs[:3]))
    except ImportError:
        print("  (jsonschema unavailable - structural check skipped)")

    for prof in sc["query"]["profiles"]:
        rows = scorer.score_all(sc, prof)
        check("[%s] live inventory scores and reconciles" % prof,
              rows and all(l.reconciles() for l in rows))
    check("abstentions cost something",
          any(l.code in ("reliability", "connectivity")
              for l in scorer.score(sc, sc["options"][0], "reference").lines),
          "unknown scoring as zero would make the least-documented option win")

    # --- hard rule 5, enforced rather than trusted ------------------------
    print("\nhard rule 5")
    src = open(os.path.join(HERE, "..", "scorer.py"), encoding="utf-8").read()
    check("the scorer does not import the adapter",
          "import adapter" not in src and "from adapter" not in src)
    for t in sorted(glob.glob(os.path.join(HERE, "test_scorer.py"))):
        body = open(t, encoding="utf-8").read()
        check("the scorer's tests do not import the adapter either",
              "import adapter" not in body)

    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    if FAILS:
        print()
        for f in FAILS:
            print("  FAIL  " + f)
        return 1
    print("all adapter checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
