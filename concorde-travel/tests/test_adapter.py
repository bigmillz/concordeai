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
    # ------------------------------------------------------------- amadeus
    print("\namadeus: what the second feed adds")
    apath = os.path.join(HERE, "..", "adapter_samples", "amadeus-jfk-lhr.json")
    araw = json.load(open(apath, encoding="utf-8"))
    asc = adapter.from_amadeus(araw, checked_bags=1)
    check("the amadeus sample normalises", bool(asc.get("options")), asc.get("error", ""))
    check("its provenance block says it was SYNTHESISED, not captured",
          araw["_provenance"]["captured"] is False)
    check("and the adapter ignores that block rather than choking on it",
          len(asc["options"]) == 6, str(len(asc.get("options", []))))

    aopts = {o["option_id"]: o for o in asc["options"]}
    codeshare = next(o for o in asc["options"] if o["option_id"].startswith("aa6175"))
    seg = codeshare["segments"][0]
    check("an operating carrier that differs from the marketing one is carried through",
          seg["marketing"]["carrier"] == "AA" and seg["operating"]["carrier"] == "BA",
          "%s / %s" % (seg["marketing"]["carrier"], seg["operating"]["carrier"]))
    check("and the cabin claim is looked up against the OPERATOR's fleet, not the seller's",
          "777-300ER" in str(seg["claims"]["subfleet"].get("value", "")),
          str(seg["claims"]["subfleet"])[:90])

    # An absent `operating` block in this schema MEANS the marketing carrier
    # flies it. Treating that as a hole would throw away real information -
    # and treating Kiwi's silence as the same thing would invent it.
    ba = aopts["ba112-basic-1"]
    check("an absent operating block resolves to the marketing carrier",
          ba["segments"][0]["operating"]["carrier"] == "BA")
    check("and that is NOT reported as a codeshare gap",
          "codeshare" not in json.dumps(ba["segments"][0]["claims"]))

    print("\namadeus: an equipment code is still not a cabin")
    check("the equipment code survives into the segment",
          ba["segments"][0]["equipment_code"] == "789")
    sub = ba["segments"][0]["claims"]["subfleet"]
    check("a curated type produces a HEDGED claim, not a fact",
          0.0 < sub["observed_frequency"] < 1.0, str(sub.get("observed_frequency")))
    check("carrying the evidence the narrator is allowed to quote",
          all(k in sub for k in ("sample_size", "observation_window", "source", "as_of")))
    check("and flagged as needing a primary source, because the table is a draft",
          sub.get("needs_primary_source") is True)
    kl = aopts["kl642-light-5"]
    short = next(s for s in kl["segments"] if s["equipment_code"] == "73H")
    check("a type with NO curated row abstains rather than being guessed from the type",
          short["claims"]["subfleet"].get("coverage") == "none")
    check("and says which file would fix it",
          "curated" in short["claims"]["subfleet"]["reason"],
          short["claims"]["subfleet"]["reason"][:80])

    # Hard rule 2, mechanically: nothing anywhere in a normalised scenario may
    # assert an aircraft fact outright.
    blob = json.dumps(asc)
    for phrase in ("you will have", "guaranteed", "always has", "will have wifi"):
        check("no unhedged aircraft claim: %r appears nowhere" % phrase,
              phrase not in blob.lower())

    print("\namadeus: the bags arithmetic finally has inputs")
    basic = aopts["ba112-basic-1"]["tickets"][0]
    plus = aopts["ba112-plus-2"]["tickets"][0]
    check("the fare brand comes off the feed", basic["fare_brand_name"] == "BASIC",
          basic["fare_brand_name"])
    check("the included allowance comes off the feed too, and they differ",
          basic["entitlements"]["checked_included"] == 0
          and plus["entitlements"]["checked_included"] == 1)
    check("a curated brand gets its curated fee schedule",
          [t["amount_cents"] for t in basic["checked_bag_fee_tiers"]][:1] == [7500],
          str(basic["checked_bag_fee_tiers"][:1]))
    check("prices parsed from a decimal STRING land as integer cents",
          basic["price"]["base_cents"] == 31000
          and sum(x["amount_cents"] for x in basic["price"]["taxes"]) == 17000,
          str(basic["price"]["base_cents"]))
    check("base plus taxes reconciles to the feed's grand total",
          basic["price"]["base_cents"]
          + sum(x["amount_cents"] for x in basic["price"]["taxes"]) == 48000)

    # The traveller's bag load is a property of the TRAVELLER. Reading it off
    # the fare makes every basic-economy ticket score as hand-baggage-only,
    # which is the exact comparison the product exists to make.
    for n in (0, 2):
        s2 = adapter.from_amadeus(araw, checked_bags=n)
        got = sum(1 for b in s2["query"]["party"][0]["bags"] if b["kind"] == "checked")
        check("checked_bags=%d comes from the caller, not from the fare" % n, got == n,
              "got %d" % got)

    one = adapter.from_amadeus(araw, checked_bags=1)
    rows = {r.option_id: r for r in scorer.score_all(one, "reference")}
    bag_cost = lambda oid: sum(l.amount_cents for l in rows[oid].lines if l.code == "bags")
    check("a fare including no bag is charged for the traveller's bag",
          bag_cost("ba112-basic-1") == 7500, str(bag_cost("ba112-basic-1")))
    check("and the fare that includes one is not",
          bag_cost("ba112-plus-2") == 0, str(bag_cost("ba112-plus-2")))

    # Unknown must never be CHEAP, or the least-documented fare wins on silence.
    uncurated = json.loads(json.dumps(araw))
    for off in uncurated["data"]:
        for d in off["travelerPricings"][0]["fareDetailsBySegment"]:
            d["brandedFare"] = "NO-SUCH-BRAND"
    unc = adapter.from_amadeus(uncurated, checked_bags=1)
    ut = next(o for o in unc["options"] if o["option_id"].startswith("ba112"))["tickets"][0]
    check("a brand with no curated fees falls back to a default",
          bool(ut["checked_bag_fee_tiers"]))
    check("and the default is DEARER than the curated row, never cheaper",
          ut["checked_bag_fee_tiers"][0]["amount_cents"] > 7500,
          str(ut["checked_bag_fee_tiers"][0]["amount_cents"]))
    check("the fallback says it is a placeholder rather than a quote",
          "not a quote" in (ut["checked_bag_fee_tiers"][0].get("note") or ""),
          str(ut["checked_bag_fee_tiers"][0].get("note"))[:70])
    check("and coverage() counts how many options it happened to",
          adapter.coverage(unc)["counts"]["default_bag_fees"] == len(unc["options"]))

    print("\namadeus: what this feed cannot see")
    check("GDS content is one ticket per offer - no self-transfer exists here",
          all(len(o["tickets"]) == 1 for o in asc["options"]))
    check("so a connection's bags are checked through",
          all(l["bags_checked_through"] for o in asc["options"] for l in o["layovers"]))
    check("and nobody is forced landside between segments",
          not any(l["forced_landside"] for o in asc["options"] for l in o["layovers"]))
    check("reliability still abstains: shopping carries no on-time record",
          all(s["reliability"]["coverage"] == "none"
              for o in asc["options"] for s in o["segments"]))

    print("\namadeus: the joins that are load-bearing for BOTH feeds")
    check("every timestamp gains an explicit UTC offset",
          all(re.search(r"[+-]\d{2}:\d{2}$", s[k])
              for o in asc["options"] for s in o["segments"]
              for k in ("departure_local", "arrival_local")))
    check("November in New York resolves to standard time, not summer time",
          asc["options"][0]["segments"][0]["departure_local"].endswith("-05:00"),
          asc["options"][0]["segments"][0]["departure_local"])
    check("an itinerary through an uncurated airport is DROPPED, not guessed",
          any("DUB" in d["why"] for d in asc["_dropped"]), json.dumps(asc["_dropped"]))
    check("and no surviving option touches it", "DUB" not in json.dumps(asc["options"]))

    print("\namadeus: coverage tells the two feeds apart")
    acov = adapter.coverage(asc)
    check("it does not claim the amadeus feed lacks an equipment code",
          not any("carries no equipment code" in g for g in acov["gaps"]),
          json.dumps(acov["gaps"]))
    check("the kiwi feed still reports exactly that gap",
          any("carries no equipment code" in g for g in cov["gaps"]))
    check("draft claims are surfaced as a gap in their own right",
          any("DRAFT" in g for g in acov["gaps"]), json.dumps(acov["gaps"]))
    check("the verdict says the aircraft layer is not yet reviewed",
          "DRAFT" in acov["verdict"], acov["verdict"])
    check("a dropped itinerary is listed but does not degrade the verdict",
          any("dropped an itinerary" in g for g in acov["gaps"])
          and "indicative only" not in acov["verdict"])

    print("\namadeus: the reads that only a hostile payload exercises")
    # Money is integer cents END TO END. The feed hands over decimal STRINGS, so
    # this is the one place a float could get in, and a float that gets in here
    # makes ties sort by rounding noise for the rest of the pipeline.
    # 19.99, 0.29 and 8.87 are here because float(x)*100 truncates each of them
    # a cent LOW - they are the values that tell a textual parse from a float
    # one, and a table of round numbers would pass either way.
    for text, want in (("480.00", 48000), ("480.10", 48010), ("0.07", 7),
                       ("19.99", 1999), ("0.29", 29), ("8.87", 887),
                       ("1129.15", 112915), ("8.9", 890), ("", 0), ("12", 1200),
                       ("-3.50", -350), ("1234.99", 123499)):
        check("%r parses to %d cents exactly" % (text, want),
              adapter._decimal_cents(text) == want,
              "got %s" % adapter._decimal_cents(text))

    # A ticket has ONE allowance. Where the feed disagrees across segments, the
    # smaller number is the one the traveller can rely on at the first bag drop;
    # taking the larger invents an allowance for the leg that does not have it.
    mixed = json.loads(json.dumps(araw))
    kl_off = next(o for o in mixed["data"] if o["id"] == "5")
    kl_off["travelerPricings"][0]["fareDetailsBySegment"][0]["includedCheckedBags"] = {"quantity": 2}
    kl_off["travelerPricings"][0]["fareDetailsBySegment"][1]["includedCheckedBags"] = {"quantity": 0}
    msc = adapter.from_amadeus(mixed, checked_bags=1)
    mt = next(o for o in msc["options"] if o["option_id"].startswith("kl642"))["tickets"][0]
    check("segments disagreeing on the allowance resolve to the SMALLER one",
          mt["entitlements"]["checked_included"] == 0,
          str(mt["entitlements"]["checked_included"]))

    # Some carriers express the allowance as a weight with no piece count at
    # all. Reading the absent quantity as zero invents a bag fee on a fare that
    # includes one - the same class of error as treating unknown as free, in
    # the opposite direction.
    wt = json.loads(json.dumps(araw))
    ba_off = next(o for o in wt["data"] if o["id"] == "1")
    ba_off["travelerPricings"][0]["fareDetailsBySegment"][0]["includedCheckedBags"] = {
        "weight": 23, "weightUnit": "KG"}
    wsc = adapter.from_amadeus(wt, checked_bags=1)
    wtick = next(o for o in wsc["options"] if o["option_id"].startswith("ba112-basic"))["tickets"][0]
    check("a weight-only allowance counts as one included piece, not zero",
          wtick["entitlements"]["checked_included"] == 1,
          str(wtick["entitlements"]["checked_included"]))
    wrows = {r.option_id: r for r in scorer.score_all(wsc, "reference")}
    check("so no bag fee is invented on a fare that does include one",
          sum(l.amount_cents for l in wrows["ba112-basic-1"].lines if l.code == "bags") == 0)

    # coverage() is what the page prints to say how much to trust a grade. A
    # count that overstates what is known is worse than no count.
    counts = acov["counts"]
    known = sum(1 for o in asc["options"] for s in o["segments"]
                if s["equipment_code"] not in (None, "", "UNKNOWN"))
    check("coverage counts equipment as known only where the feed gave a code",
          counts["equipment_known"] == known == counts["segments"],
          "%d known / %d counted / %d segments"
          % (known, counts["equipment_known"], counts["segments"]))
    noeq = json.loads(json.dumps(araw))
    for off in noeq["data"]:
        for s in off["itineraries"][0]["segments"]:
            s.pop("aircraft", None)
    ncov = adapter.coverage(adapter.from_amadeus(noeq, checked_bags=1))
    check("strip the equipment codes and the count follows the payload down",
          ncov["counts"]["equipment_known"] == 0,
          str(ncov["counts"]["equipment_known"]))
    check("and the gap goes back to naming the missing code",
          any("carries no equipment code" in g for g in ncov["gaps"]),
          json.dumps(ncov["gaps"]))

    # -------------------------------------------------------------- duffel
    print("\nduffel: the third feed")
    dpath = os.path.join(HERE, "..", "adapter_samples", "duffel-jfk-lhr.json")
    draw = json.load(open(dpath, encoding="utf-8"))
    dsc = adapter.from_duffel(draw, checked_bags=1)
    check("the duffel sample normalises", bool(dsc.get("options")), dsc.get("error", ""))
    check("its provenance says SYNTHESISED, and names the SDK it was built from",
          draw["_provenance"]["captured"] is False
          and "duffel_api/models/offer.py" in draw["_provenance"]["how"])
    check("an itinerary through an uncurated airport is dropped",
          any("DUB" in d["why"] for d in dsc["_dropped"]), json.dumps(dsc["_dropped"]))

    # Duffel nests offers under data.offers on the search call and returns them
    # as a bare data[] on the offers endpoint. Both are the same offers.
    flat = {"data": draw["data"]["offers"]}
    fsc = adapter.from_duffel(flat, checked_bags=1)
    check("both duffel payload shapes produce the same options",
          [o["option_id"] for o in fsc["options"]] == [o["option_id"] for o in dsc["options"]])

    print("\nduffel: carrier, aircraft, and what it refuses to say")
    share = next(o for o in dsc["options"] if o["option_id"].startswith("aa6175"))
    sseg = share["segments"][0]
    check("a codeshare is detected from the two carrier objects",
          sseg["marketing"]["carrier"] == "AA" and sseg["operating"]["carrier"] == "BA")
    check("and the operating carrier's own flight number is kept, not the seller's",
          sseg["operating"]["number"] == 178 and sseg["marketing"]["number"] == 6175,
          "%s / %s" % (sseg["operating"]["number"], sseg["marketing"]["number"]))
    check("the cabin claim is looked up on the operator, not the seller",
          "777-300ER" in str(sseg["claims"]["subfleet"].get("value", "")))
    ba = next(o for o in dsc["options"] if "Basic" == o["tickets"][0]["fare_brand_name"])
    check("carrier rating follows the OPERATING carrier",
          share.get("carrier_rating", {}).get("rating")
          == ba.get("carrier_rating", {}).get("rating"),
          "AA-marketed BA metal should rate as BA")

    zz = next(o for o in dsc["options"] if o["segments"][0]["marketing"]["carrier"] == "ZZ")
    check("a segment with aircraft: null abstains rather than guessing",
          zz["segments"][0]["claims"]["subfleet"].get("coverage") == "none")
    check("an uncurated carrier keeps its option but loses its rating",
          "carrier_rating" not in zz)
    check("and test-mode fiction is called out in the scenario notes",
          any("Duffel Airways" in n and "invented" in n for n in dsc["notes"]),
          json.dumps(dsc["notes"])[:120])

    print("\nduffel: it is the only feed that quotes a bag price")
    bat = ba["tickets"][0]
    check("an airline-quoted bag price is used", bat["checked_bag_fee_tiers"][0]["amount_cents"] == 7500)
    check("and is labelled as quoted, not estimated",
          "not a curated estimate" in (bat["checked_bag_fee_tiers"][0].get("note") or ""),
          str(bat["checked_bag_fee_tiers"][0].get("note"))[:70])
    check("coverage reports that as a STRENGTH, not a gap",
          any("airline-quoted" in s for s in adapter.coverage(dsc).get("strengths", [])),
          json.dumps(adapter.coverage(dsc).get("strengths")))

    # The quote covers maximum_quantity pieces and nothing beyond it. The scorer
    # RAISES on an unpriced piece, so a heavy bag load must not fall off the end.
    heavy = adapter.from_duffel(draw, checked_bags=5)
    hb = next(o for o in heavy["options"]
              if o["tickets"][0]["fare_brand_name"] == "Basic")["tickets"][0]
    pieces = [t["piece"] for t in hb["checked_bag_fee_tiers"]]
    check("pieces beyond the airline's quoted cap are still priced",
          pieces == sorted(set(pieces)) and max(pieces) >= 3, str(pieces))
    check("and those are marked as estimates rather than quotes",
          any("estimate" in (t.get("note") or "") for t in hb["checked_bag_fee_tiers"]))
    # scorer._bag_cost RAISES on an unpriced piece. The traveller's bag load
    # comes from the request and is unbounded, so a ladder that stops at three
    # takes the whole search down on a four-bag party - which it did, on both
    # feeds, until _extend_tiers.
    for feed, raw_payload in (("amadeus", araw), ("duffel", draw)):
        ok_loads = True
        for load in range(0, 10):
            s = adapter.from_feed(raw_payload, checked_bags=load)
            try:
                scorer.score_all(s, "reference")
            except ValueError as exc:
                ok_loads = False
                detail = "%s at %d bags: %s" % (feed, load, exc)
                break
        check("%s prices every bag load from 0 to 9" % feed, ok_loads,
              "" if ok_loads else detail)
    check("padding repeats the DEAREST published tier, never a cheaper one",
          all(t["amount_cents"] >= max(x["amount_cents"]
                                       for x in hb["checked_bag_fee_tiers"]
                                       if x["piece"] < t["piece"])
              for t in hb["checked_bag_fee_tiers"] if t["piece"] > 1))

    # An empty available_services means Duffel has no quote for that airline. It
    # does NOT mean the bag is free, which is the reading that loses money.
    noserv = json.loads(json.dumps(draw))
    for o in noserv["data"]["offers"]:
        o["available_services"] = []
    nsc = adapter.from_duffel(noserv, checked_bags=1)
    nbt = next(o for o in nsc["options"]
               if o["tickets"][0]["fare_brand_name"] == "Basic")["tickets"][0]
    check("no quoted service falls back to the curated table, never to free",
          bool(nbt["checked_bag_fee_tiers"])
          and nbt["checked_bag_fee_tiers"][0]["amount_cents"] > 0,
          str(nbt["checked_bag_fee_tiers"][:1]))

    # A ticket has one allowance. Where the feed's segments disagree, the
    # smaller is what survives the first bag drop; the larger invents an
    # allowance on the leg that does not have it.
    mixd = json.loads(json.dumps(draw))
    klo = next(o for o in mixd["data"]["offers"] if o["id"] == "off_0000KlConnect")
    klo["slices"][0]["segments"][0]["passengers"][0]["baggages"] = [
        {"type": "checked", "quantity": 2}, {"type": "carry_on", "quantity": 1}]
    klo["slices"][0]["segments"][1]["passengers"][0]["baggages"] = [
        {"type": "checked", "quantity": 0}, {"type": "carry_on", "quantity": 1}]
    mdsc = adapter.from_duffel(mixd, checked_bags=1)
    mdt = next(o for o in mdsc["options"]
               if o["segments"][0]["marketing"]["carrier"] == "KL")["tickets"][0]
    check("segments disagreeing on the allowance resolve to the SMALLER one",
          mdt["entitlements"]["checked_included"] == 0,
          str(mdt["entitlements"]["checked_included"]))
    check("and the traveller is therefore charged for their bag",
          sum(l.amount_cents for l in
              {r.option_id: r for r in scorer.score_all(mdsc, "reference")}[
                  next(o["option_id"] for o in mdsc["options"]
                       if o["segments"][0]["marketing"]["carrier"] == "KL")].lines
              if l.code == "bags") > 0)

    print("\nduffel: brands are prose here, not codes")
    fares = adapter.load_enrichment()["fares"]
    check("an exact code still matches", adapter._brand_key(fares, "BA", "BASIC") == "BA:BASIC")
    check("'Basic Economy' resolves to the BASIC row",
          adapter._brand_key(fares, "AA", "Basic Economy") == "AA:BASIC")
    check("'Economy Light' resolves to LIGHT, not to some ECONOMY row",
          adapter._brand_key(fares, "KL", "Economy Light") == "KL:LIGHT")
    # Whole words, not substrings: SURPLUS contains PLUS.
    check("a token inside another word does NOT match",
          adapter._brand_key(fares, "BA", "Surplus Saver") is None,
          str(adapter._brand_key(fares, "BA", "Surplus Saver")))
    check("an unknown brand returns nothing so the default applies",
          adapter._brand_key(fares, "BA", "Wibble") is None)
    aat = next(o for o in dsc["options"]
               if o["option_id"].startswith("aa6175"))["tickets"][0]
    check("so a prose brand reaches its curated fees end to end",
          aat["checked_bag_fee_tiers"][0]["amount_cents"] == 7500,
          str(aat["checked_bag_fee_tiers"][:1]))

    print("\nduffel: time, conditions, and the ledger")
    check("departing_at arrives local-naive and gains an offset",
          all(re.search(r"[+-]\d{2}:\d{2}$", s[k])
              for o in dsc["options"] for s in o["segments"]
              for k in ("departure_local", "arrival_local")))
    check("November in New York is standard time",
          dsc["options"][0]["segments"][0]["departure_local"].endswith("-05:00"))
    # Duffel names each airport's IANA zone. It is never used to compute an
    # offset, but a disagreement means one of the two tables is wrong.
    badzone = json.loads(json.dumps(draw))
    badzone["data"]["offers"][0]["slices"][0]["segments"][0]["origin"]["time_zone"] = "Europe/Paris"
    bsc = adapter.from_duffel(badzone, checked_bags=1)
    check("a feed timezone contradicting the curated table is reported",
          any("one is wrong" in n for n in bsc["notes"]), json.dumps(bsc["notes"])[:140])
    check("and it does not silently change the computed offset",
          bsc["options"][0]["segments"][0]["departure_local"]
          == dsc["options"][0]["segments"][0]["departure_local"])

    plus = next(o for o in dsc["options"]
                if o["tickets"][0]["fare_brand_name"] == "Plus")["tickets"][0]
    check("a change penalty reads as a fee", plus["entitlements"]["changes"] == "fee")
    check("a disallowed change reads as not permitted",
          bat["entitlements"]["changes"] == "not_permitted")
    check("a null condition reads as unknown, not as a no",
          zz["tickets"][0]["entitlements"]["changes"] == "unknown")
    check("nothing non-refundable is sold as refundable",
          not any(o["tickets"][0]["entitlements"]["refundable"] for o in dsc["options"]))

    check("base plus tax reconciles to the offer total",
          all(t["price"]["base_cents"] + sum(x["amount_cents"] for x in t["price"]["taxes"])
              == o["booking"][0]["price_cents"]
              for o in dsc["options"] for t in o["tickets"]))
    # If the feed's own three numbers disagree, the total is what gets charged.
    skew = json.loads(json.dumps(draw))
    skew["data"]["offers"][0]["tax_amount"] = "5.00"
    ssc = adapter.from_duffel(skew, checked_bags=1)
    st = ssc["options"][0]["tickets"][0]
    check("a self-contradicting price still reconciles against the total",
          st["price"]["base_cents"] + sum(x["amount_cents"] for x in st["price"]["taxes"])
          == 48000,
          str(st["price"]))

    print("\nduffel: topology and scope")
    check("one offer is one ticket - no self-transfer in this feed",
          all(len(o["tickets"]) == 1 for o in dsc["options"]))
    check("so a connection's bags are checked through",
          all(l["bags_checked_through"] and not l["forced_landside"]
              for o in dsc["options"] for l in o["layovers"]))
    for n in (0, 3):
        s2 = adapter.from_duffel(draw, checked_bags=n)
        got = sum(1 for b in s2["query"]["party"][0]["bags"] if b["kind"] == "checked")
        check("checked_bags=%d comes from the caller" % n, got == n, "got %d" % got)
    ret = json.loads(json.dumps(draw))
    ret["data"]["offers"][0]["slices"].append(
        json.loads(json.dumps(ret["data"]["offers"][0]["slices"][0])))
    rsc = adapter.from_duffel(ret, checked_bags=1)
    check("a return trip says it only scored the outbound rather than pretending",
          any("return trips are not modelled" in n for n in rsc["notes"]),
          json.dumps(rsc["notes"])[:120])

    print("\ndispatch, three feeds")
    check("from_feed routes a duffel offer-request payload",
          (adapter.from_feed(draw).get("_feed") or {}).get("provider") == "duffel")
    check("and a bare duffel offers list",
          (adapter.from_feed(flat).get("_feed") or {}).get("provider") == "duffel")
    check("amadeus still routes to amadeus, not to duffel",
          (adapter.from_feed(araw).get("_feed") or {}).get("provider") == "amadeus")
    check("a duffel scenario scores with no special-casing",
          len(scorer.score_all(dsc, "reference")) == len(dsc["options"]))
    blob = json.dumps(dsc).lower()
    for phrase in ("you will have", "guaranteed", "always has"):
        check("no unhedged aircraft claim: %r appears nowhere" % phrase, phrase not in blob)

    print("\ndispatch")
    check("from_feed routes an amadeus payload to from_amadeus",
          (adapter.from_feed(araw).get("_feed") or {}).get("provider") == "amadeus")
    check("and a kiwi payload to from_kiwi",
          adapter.from_feed(raw).get("fixture_id", "").startswith("live-jfk"))
    check("garbage is refused rather than half-normalised",
          "unrecognised" in adapter.from_feed({"hello": "world"}).get("error", ""))
    check("a scenario built from either feed scores without special-casing",
          len(scorer.score_all(asc, "reference")) == len(asc["options"]))

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
