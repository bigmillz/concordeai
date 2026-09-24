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


def dofs_by_id(offers, opt):
    """Find the raw offer an option came from, by the id suffix the adapter keeps."""
    for o in offers:
        if (o["id"] or "")[-10:].lower().replace("_", "-") in opt["option_id"]:
            return o
    return {}


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
          len(asc["options"]) == len(araw["data"]),
          "%d options from %d offers" % (len(asc.get("options", [])), len(araw["data"])))

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
    ba_basic_first = adapter.load_enrichment()["fares"]["brands"]["BA:BASIC"]["tiers"][0]["amount_cents"]
    check("a curated brand gets its curated fee schedule",
          [t["amount_cents"] for t in basic["checked_bag_fee_tiers"]][:1] == [ba_basic_first]
          and ba_basic_first != 10000,
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
          bag_cost("ba112-basic-1") == ba_basic_first, str(bag_cost("ba112-basic-1")))
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
    # Tests the MECHANISM against a stripped table, not the accident of some
    # airport being absent. This assertion previously leaned on DUB happening to
    # be uncurated, so curating DUB silently deleted the guard - which is the
    # second time that has happened here (BCN was the first).
    stripped = json.loads(json.dumps(adapter.load_enrichment()))
    # Strip an airport SOME options touch, not all of them: removing the origin
    # drops every itinerary, and a scenario with nothing left in it cannot
    # demonstrate that a drop is reported alongside the survivors.
    touched = [s[k]["iata"] for o in asc["options"] for s in o["segments"]
               for k in ("origin", "destination")]
    victim = next(a for a in dict.fromkeys(touched)
                  if 0 < sum(1 for o in asc["options"]
                             if any(s[k]["iata"] == a for s in o["segments"]
                                    for k in ("origin", "destination")))
                  < len(asc["options"]))
    stripped["airports"]["airports"].pop(victim, None)
    ssc = adapter.from_amadeus(araw, enr=stripped, checked_bags=1)
    check("an itinerary through an uncurated airport is DROPPED, not guessed",
          bool(ssc.get("error") or ssc.get("_dropped")),
          "stripping %s should drop everything that touches it" % victim)
    check("and no surviving option touches it",
          victim not in json.dumps(ssc.get("options", [])))

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
    # Asserted against the stripped table for the same reason as above: the
    # sample's own drop case (DUB) became curated, and an assertion that reads
    # "a drop is listed" silently passes forever once nothing drops.
    scov = adapter.coverage(ssc)
    check("a dropped itinerary is listed but does not degrade the verdict",
          any("dropped an itinerary" in g for g in scov["gaps"]),
          json.dumps(scov["gaps"]))

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
    # This sample is a REAL capture, trimmed. Everything below is therefore an
    # assertion about what the live service actually returns, not about a shape
    # I guessed at - which is the whole reason it replaced the synthesised one.
    print("\nduffel: a real capture")
    dpath = os.path.join(HERE, "..", "adapter_samples", "duffel-jfk-lhr.json")
    draw = json.load(open(dpath, encoding="utf-8"))
    dsc = adapter.from_duffel(draw, checked_bags=1)
    check("the captured payload normalises", bool(dsc.get("options")), dsc.get("error", ""))
    check("and its provenance says CAPTURED, not synthesised",
          draw["_provenance"]["captured"] is True)
    check("the capture is trimmed, and says so",
          "TRIMMED" in draw["_provenance"]["how"])

    dofs = draw["data"]["offers"]
    dsegs = [sg for o in dofs for sl in o["slices"] for sg in sl["segments"]]

    # Duffel nests offers under data.offers on the search call and returns them
    # as a bare data[] on the offers endpoint. Both are the same offers.
    flat = {"data": dofs}
    fsc = adapter.from_duffel(flat, checked_bags=1)
    check("both duffel payload shapes produce the same options",
          [o["option_id"] for o in fsc["options"]] == [o["option_id"] for o in dsc["options"]])

    print("\nduffel: carrier and aircraft, as the live service gives them")
    share = next((o for o in dsc["options"] for s in o["segments"]
                  if s["operating"]["carrier"] != s["marketing"]["carrier"]), None)
    check("a real codeshare is present and its operator is kept", share is not None)
    if share:
        sseg = next(s for s in share["segments"]
                    if s["operating"]["carrier"] != s["marketing"]["carrier"])
        check("the operating carrier differs from the seller, and both survive",
              sseg["operating"]["carrier"] and sseg["marketing"]["carrier"]
              and sseg["operating"]["carrier"] != sseg["marketing"]["carrier"],
              "%s / %s" % (sseg["marketing"]["carrier"], sseg["operating"]["carrier"]))
        check("the carrier rating follows the OPERATING carrier",
              (share.get("carrier_rating") or {}).get("rating")
              == ((adapter.load_enrichment()["carriers"]["ratings"]
                   .get(sseg["operating"]["carrier"]) or {}).get("rating")))
    noac = [s for o in dsc["options"] for s in o["segments"]
            if s["equipment_code"] == "UNKNOWN"]
    check("segments the feed gave no aircraft for are present in a real capture",
          bool(noac), "the live feed does omit it sometimes")
    check("and every one of them abstains rather than guessing",
          all(s["claims"]["subfleet"].get("coverage") == "none" for s in noac))

    print("\nduffel: the cabin amenities, which no other feed here carries")
    # 269 of 272 segments in the untrimmed capture carried this block. It is the
    # aircraft/cabin/connectivity layer arriving without a curated join.
    amen = [((sg.get("passengers") or [{}])[0].get("cabin") or {}).get("amenities")
            for sg in dsegs]
    check("the capture really does carry per-segment cabin amenities",
          sum(1 for a in amen if a) >= len(dsegs) - 4,
          "%d of %d" % (sum(1 for a in amen if a), len(dsegs)))
    pitched = [s for o in dsc["options"] for s in o["segments"]
               if "seat_pitch_inches" in (s.get("claims") or {})]
    check("seat pitch is carried through as a number the scorer can price",
          bool(pitched) and all(20 < s["claims"]["seat_pitch_inches"] < 60
                                for s in pitched),
          str(sorted({s["claims"]["seat_pitch_inches"] for s in pitched})))
    rows = {r.option_id: r for r in scorer.score_all(dsc, "reference")}
    tight = [o for o in dsc["options"]
             if any((s.get("claims") or {}).get("seat_pitch_inches", 99) < 31
                    for s in o["segments"])]
    check("a sub-31-inch pitch produces a priced ledger line", bool(tight))
    if tight:
        line = next((l for l in rows[tight[0]["option_id"]].lines if l.code == "pitch"), None)
        check("and that line names the inches and costs real money",
              line is not None and line.amount_cents > 0 and "inches" in line.label,
              str(line.label if line else None))

    # Wifi is NOT mapped onto connectivity_oceanic. That field is an OBSERVED
    # claim; an airline saying "wifi: available" is a marketing attribute with
    # no source and no as-of. Inventing a frequency for it is the exact thing
    # hard rule 2 exists to stop.
    wifi = dsc["_feed"]["wifi_published"]
    check("published wifi is recorded for the narrator and the page", bool(wifi))
    check("and it keeps the airline's own words rather than a score",
          all(set(w) == {"segment", "available", "cost"} for w in wifi),
          json.dumps(wifi[:1]))
    conn = [(s.get("claims") or {}).get("connectivity_oceanic") for o in dsc["options"]
            for s in o["segments"]]
    check("no connectivity claim was manufactured from a published amenity",
          all(c is None or c.get("coverage") == "none" or "needs_primary_source" in c
              for c in conn))
    powered = [s for o in dsc["options"] for s in o["segments"]
               if (s.get("claims") or {}).get("power")]
    check("published power is carried through", bool(powered),
          "%d of %d segments" % (len(powered),
                                 sum(len(o["segments"]) for o in dsc["options"])))
    check("as a plain published attribute, not a hedged claim",
          all(isinstance(s["claims"]["power"], str) and "airline" in s["claims"]["power"]
              for s in powered))

    print("\nduffel: bags, and what the SEARCH response does not carry")
    # Measured: available_services was empty on all 172 offers of the real
    # capture. It is populated only by the single-offer endpoint, so on this
    # path the bag price is always a fallback - the opposite of what the first
    # Duffel commit claimed.
    check("the real search response quotes no bag prices at all",
          all(not o.get("available_services") for o in dofs))
    check("so coverage claims no airline-quoted bags on a search payload",
          not adapter.coverage(dsc).get("strengths"),
          json.dumps(adapter.coverage(dsc).get("strengths")))
    check("and every ticket still has a priced ladder rather than a free bag",
          all(t["checked_bag_fee_tiers"] and t["checked_bag_fee_tiers"][0]["amount_cents"] > 0
              for o in dsc["options"] for t in o["tickets"]))

    # The capability is real, just on GET /air/offers/{id}. Constructed here
    # rather than shipped as a sample, because a sample carrying it would be a
    # sample of a response this code path never sees.
    quoted = json.loads(json.dumps(draw))
    target = quoted["data"]["offers"][0]
    target["available_services"] = [{
        "id": "ase_x", "type": "baggage", "maximum_quantity": 2,
        "total_amount": "65.00", "total_currency": "USD",
        "passenger_ids": ["p"], "segment_ids": ["s"],
        "metadata": {"type": "checked", "maximum_weight_kg": 23}}]
    qsc = adapter.from_duffel(quoted, checked_bags=1)
    qt = qsc["options"][0]["tickets"][0]
    check("when the single-offer endpoint DOES quote a bag, the quote wins",
          qt["checked_bag_fee_tiers"][0]["amount_cents"] == 6500,
          str(qt["checked_bag_fee_tiers"][:1]))
    check("and it is labelled a quote rather than an estimate",
          "not a curated estimate" in (qt["checked_bag_fee_tiers"][0].get("note") or ""))
    check("coverage then reports it as a strength",
          any("airline-quoted" in s for s in adapter.coverage(qsc).get("strengths", [])))

    # The invariant is not "the ladder reaches N" - a fare including a bag needs
    # one fewer priced piece. It is that the scorer never raises, at any load.
    for load in range(0, 10):
        h = adapter.from_duffel(draw, checked_bags=load)
        for o in h["options"]:
            t = o["tickets"][0]
            need = max(0, load - t["entitlements"]["checked_included"])
            pieces = sorted(x["piece"] for x in t["checked_bag_fee_tiers"])
            if pieces != list(range(1, len(pieces) + 1)) or len(pieces) < need:
                check("every ticket prices the pieces it needs", False,
                      "%s at %d bags: %s for need %d" % (o["option_id"], load, pieces, need))
                break
        else:
            scorer.score_all(h, "reference")
            continue
        break
    else:
        check("every ticket prices the pieces it needs, loads 0 to 9", True)

    print("\nduffel: brands as the live service writes them")
    fares = adapter.load_enrichment()["fares"]
    live_brands = sorted({sl.get("fare_brand_name") for o in dofs for sl in o["slices"]},
                         key=lambda x: (x is None, x))
    check("the real feed writes brands as prose, and some are null",
          any(b is None for b in live_brands)
          and any(b and " " in b for b in live_brands if b), str(live_brands[:6]))
    check("a null brand does not crash and falls back to a cabin name",
          all(o["tickets"][0]["fare_brand_name"] for o in dsc["options"]))
    check("'Basic Economy' resolves to the BASIC row",
          adapter._brand_key(fares, "AA", "Basic Economy") == "AA:BASIC")
    check("'Economy Light' resolves to LIGHT, not to some ECONOMY row",
          adapter._brand_key(fares, "KL", "Economy Light") == "KL:LIGHT")
    check("a token inside another word does NOT match",
          adapter._brand_key(fares, "BA", "Surplus Saver") is None)
    check("an unknown brand returns nothing so the default applies",
          adapter._brand_key(fares, "BA", "Wibble") is None)

    # zoneinfo made an uncurated airport survivable, but ONLY because the feed
    # names its zone. Strip both and there is nothing left to resolve - and
    # assuming UTC there would silently shift a whole itinerary by hours in
    # exactly the places we know least about.
    nozone = json.loads(json.dumps(draw))
    hit = 0
    for off in nozone["data"]["offers"]:
        for sl in off.get("slices") or []:
            for sg in sl.get("segments") or []:
                for k in ("origin", "destination"):
                    if sg[k]["iata_code"] not in adapter.load_enrichment()["airports"]["airports"]:
                        sg[k].pop("time_zone", None); hit += 1
    check("the capture has uncurated airports to strip", hit > 0, str(hit))
    nz = adapter.from_duffel(nozone, checked_bags=1)
    check("an airport with no curated zone AND no feed zone is dropped, not assumed UTC",
          any("timezone" in d["why"] for d in nz.get("_dropped", [])),
          json.dumps(nz.get("_dropped", []))[:140])
    check("and nothing that survived got a fabricated offset",
          all(s[k].endswith(("-05:00", "+00:00", "+01:00", "+02:00", "-04:00"))
              for o in nz.get("options", []) for s in o["segments"]
              for k in ("departure_local", "arrival_local")))

    print("\nduffel: time, conditions, topology")
    check("departing_at arrives local-naive and gains an offset",
          all(re.search(r"[+-]\d{2}:\d{2}$", s[k])
              for o in dsc["options"] for s in o["segments"]
              for k in ("departure_local", "arrival_local")))
    check("November in New York is standard time",
          all(s["departure_local"].endswith("-05:00")
              for o in dsc["options"] for s in o["segments"]
              if s["origin"]["iata"] == "JFK" and s["departure_local"][5:7] == "11"))
    badzone = json.loads(json.dumps(draw))
    badzone["data"]["offers"][0]["slices"][0]["segments"][0]["origin"]["time_zone"] = "Europe/Paris"
    bsc = adapter.from_duffel(badzone, checked_bags=1)
    check("a feed timezone contradicting the curated table is reported",
          any("one is wrong" in n for n in bsc["notes"]), json.dumps(bsc["notes"])[:140])
    check("and it does not silently change the computed offset",
          bsc["options"][0]["segments"][0]["departure_local"]
          == dsc["options"][0]["segments"][0]["departure_local"])

    changes = {o["tickets"][0]["entitlements"]["changes"] for o in dsc["options"]}
    check("real conditions map to the three states and no others",
          changes <= {"fee", "free", "not_permitted", "unknown"}, str(changes))
    check("nothing is sold as refundable that the feed did not allow",
          all(o["tickets"][0]["entitlements"]["refundable"] is (
              (dofs_by_id(dofs, o).get("conditions", {}).get("refund_before_departure") or {})
              .get("allowed", False) is True)
              for o in dsc["options"] if dofs_by_id(dofs, o)))
    check("base plus tax reconciles to the offer total on every option",
          all(t["price"]["base_cents"] + sum(x["amount_cents"] for x in t["price"]["taxes"])
              == o["booking"][0]["price_cents"]
              for o in dsc["options"] for t in o["tickets"]))
    check("one offer is one ticket - no self-transfer in this feed",
          all(len(o["tickets"]) == 1 for o in dsc["options"]))
    check("so a connection's bags are checked through",
          all(l["bags_checked_through"] and not l["forced_landside"]
              for o in dsc["options"] for l in o["layovers"]))

    print("\nduffel: scope discipline against real inventory")
    # A JFK-LHR search really does return connections through Boston, Reykjavik,
    # Frankfurt and Bogota. Everything outside the curated set must drop.
    # THE CONTRACT CHANGED. Uncurated airports used to drop the itinerary. Going
    # global replaced that with keep-and-estimate, which is only honest if the
    # estimating is announced - so these now assert the announcement rather than
    # the drop.
    cur = set(adapter.load_enrichment()["airports"]["airports"])
    uncur = {sg[k]["iata_code"] for sg in dsegs for k in ("origin", "destination")
             if sg[k]["iata_code"] not in cur}
    check("the capture routes through airports outside the curated set", bool(uncur),
          str(sorted(uncur)))
    kept = {s[k]["iata"] for o in dsc["options"] for s in o["segments"]
            for k in ("origin", "destination")}
    check("an uncurated airport is KEPT, not dropped", bool(uncur & kept),
          "global coverage means showing the flight and saying what we don't know")
    check("its timestamps still carry an explicit offset",
          all(re.search(r"[+-]\d{2}:\d{2}$", s[k])
              for o in dsc["options"] for s in o["segments"]
              for k in ("departure_local", "arrival_local")))
    blindlay = [l for o in dsc["options"] for l in o["layovers"]
                if l["airport"] not in cur]
    check("a layover there is flagged as having no curated connection time",
          all(l["mct_source"] == "not curated" and l["published_mct_minutes"] is None
              for l in blindlay), "%d such layovers" % len(blindlay))
    check("and coverage names those airports rather than staying quiet",
          all(a in " ".join(adapter.coverage(dsc)["gaps"]) for a in
              {l["airport"] for l in blindlay}),
          json.dumps(adapter.coverage(dsc)["gaps"]))
    # The one thing that must NOT be silently lost with the gate: a border.
    # border_of needs the FEED's country codes for an airport nobody curated -
    # without them it returns None and every uncurated stop looks foreign, which
    # is how this assertion was wrong before the adapter was.
    feedgeo = {}
    for sg in dsegs:
        for k in ("origin", "destination"):
            n = sg[k]
            feedgeo[n["iata_code"]] = {"country": n.get("iata_country_code")}
    e = adapter.load_enrichment()
    home = adapter.border_of("JFK", e, feedgeo)
    cross = {l["airport"] for l in blindlay if l["immigration_required"]}
    foreign = {l["airport"] for l in blindlay
               if adapter.border_of(l["airport"], e, feedgeo) != home}
    check("an uncurated layover abroad still charges for immigration",
          cross == foreign, "flagged %s, actually abroad %s"
          % (sorted(cross), sorted(foreign)))
    check("and a domestic one does not",
          not (cross - foreign), "flagged as a border but in the same country")
    for n in (0, 3):
        s2 = adapter.from_duffel(draw, checked_bags=n)
        got = sum(1 for b in s2["query"]["party"][0]["bags"] if b["kind"] == "checked")
        check("checked_bags=%d comes from the caller" % n, got == n, "got %d" % got)
    ret = json.loads(json.dumps(draw))
    ret["data"]["offers"][0]["slices"].append(
        json.loads(json.dumps(ret["data"]["offers"][0]["slices"][0])))
    rsc = adapter.from_duffel(ret, checked_bags=1)
    check("a return trip says it only scored the outbound rather than pretending",
          any("return trips are not modelled" in n for n in rsc["notes"]))

    print("\ncurated airports: summer time, and the lack of it")
    enr = adapter.load_enrichment()
    zones = enr["airports"]["zones"]
    # "This place has no summer time" and "we ran out of curated years" produce
    # the same missing `dst` block and must NOT produce the same answer: one is
    # a fact, the other means drop the itinerary.
    for z in ("Atlantic/Reykjavik", "Europe/Istanbul"):
        check("%s declares that it does not observe summer time" % z,
              zones[z].get("observes_dst") is False)
        jan = adapter.offset_for(zones[z], "2027-01-15")
        jul = adapter.offset_for(zones[z], "2027-07-15")
        check("%s holds one offset all year" % z, jan == jul == zones[z]["standard"],
              "jan %s jul %s" % (jan, jul))
        far = adapter.offset_for(zones[z], "2044-07-15")
        check("%s still answers outside the curated DST years" % z, far == zones[z]["standard"])
    for z in ("Europe/Berlin", "Europe/Dublin", "Europe/London"):
        check("%s does observe it, and shifts" % z,
              adapter.offset_for(zones[z], "2026-01-15")
              != adapter.offset_for(zones[z], "2026-07-15"))
        check("%s still reports an uncurated year rather than guessing" % z,
              adapter.offset_for(zones[z], "2044-07-15") is None)
    # The distinction has to survive the whole join, not just the helper.
    ok, err = adapter.stamp("2027-07-15T10:00:00", "KEF", enr)
    check("a July timestamp at KEF stamps +00:00 rather than dropping",
          ok == "2027-07-15T10:00:00+00:00", "%s / %s" % (ok, err))
    ok, err = adapter.stamp("2027-07-15T10:00:00", "IST", enr)
    check("and Istanbul stamps +03:00", ok == "2027-07-15T10:00:00+03:00", str(err))

    print("\ncurated airports: immigration is a border, not a membership")
    # The old rule asked "is this airport Schengen", which reads DUB and IST as
    # free walk-throughs. Arriving at either from the US you clear immigration.
    check("arriving in Ireland from the US crosses a border",
          adapter.crosses_border(enr, "JFK", "DUB"))
    check("arriving in Turkey from the US crosses a border",
          adapter.crosses_border(enr, "JFK", "IST"))
    check("neither of those is Schengen, which is the point",
          not enr["airports"]["airports"]["DUB"]["schengen"]
          and not enr["airports"]["airports"]["IST"]["schengen"])
    check("Schengen to Schengen does not",
          not adapter.crosses_border(enr, "AMS", "CDG"))
    check("Schengen to the UK does",
          adapter.crosses_border(enr, "AMS", "LHR"))
    check("Ireland to the UK does too - the Common Travel Area is not one border here",
          adapter.crosses_border(enr, "DUB", "LHR"))
    check("an uncurated airport falls back rather than answering 'no'",
          adapter.crosses_border(enr, "XXX", "CDG"))

    print("\ncurated airports: what was added, and what deliberately was not")
    aps = enr["airports"]["airports"]
    for code in ("FRA", "MUC", "ZRH", "GVA", "FCO", "WAW", "CPH", "DUS",
                 "KEF", "DUB", "SNN", "IST"):
        check("%s is curated" % code, code in aps)
    for code in ("BOS", "IAD", "ATL", "BOG", "CMN", "TLV", "AUH"):
        check("%s stays OUT of scope and goes on dropping" % code, code not in aps)
    newly = [c for c, a in aps.items() if a.get("needs_primary_source")]
    check("every newly curated row is flagged for a primary-source pass",
          len(newly) == 12, str(sorted(newly)))
    check("and the older rows are not retro-flagged",
          not any(aps[c].get("needs_primary_source")
                  for c in ("JFK", "LHR", "AMS", "CDG")))
    check("every curated airport names its immigration union",
          all(a.get("border") for a in aps.values()),
          str([c for c, a in aps.items() if not a.get("border")]))
    check("every curated airport's zone exists",
          all(a["zone"] in zones for a in aps.values()))
    # Service windows are PARSED. A typo used to surface as int('al') four
    # frames down, naming neither the airport nor the value.
    for code, a in aps.items():
        for svc in a.get("services") or []:
            h = svc["hours_local"]
            check("%s service window %r is machine-readable" % (code, h),
                  h == "24h" or bool(re.match(r"^\d{2}:\d{2}-\d{2}:\d{2}$", h)))
    try:
        scorer.window_covers("whenever it feels like it", 600)
        check("unparseable opening hours raise rather than crashing obscurely", False)
    except ValueError as exc:
        check("unparseable opening hours raise and name the value",
              "whenever" in str(exc), str(exc)[:80])

    print("\nground access, anywhere")
    import ground as _g, places as _p
    e = adapter.load_enrichment()
    jfk = {"iata": "JFK", "lat": 40.6413, "lon": -73.7781, "country": "US"}
    # The curated row must survive being buried in a real address, which is what
    # people actually type. Matching only exact labels meant the one
    # neighbourhood with real numbers quietly fell through to an estimate.
    for typed in ("bushwick-brooklyn", "Bushwick", "BUSHWICK, BROOKLYN",
                  "Wyckoff Ave & Myrtle Ave, Bushwick, Brooklyn 11237"):
        o, how = _g.resolve_origin(typed, e, jfk)
        check("a curated origin is found in %r" % typed[:28], how == "curated", how)
    o, how = _g.resolve_origin("1 Bushwicky Lane, Nowhere", e, jfk)
    check("but a word that merely CONTAINS it does not match", how != "curated", how)
    check("an unplaceable origin still resolves to something",
          _g.resolve_origin("qqq", e, jfk)[0]["lat"] is not None)

    for feed, fn in (("kiwi", adapter.from_kiwi), ("amadeus", adapter.from_amadeus),
                     ("duffel", adapter.from_duffel)):
        payload = {"kiwi": raw, "amadeus": araw, "duffel": draw}[feed]
        base = len(fn(payload, origin_key="bushwick-brooklyn").get("options", []))
        for where in ("Upper East Side", "Accra", "qqq"):
            sc2 = fn(payload, origin_key=where)
            check("%s keeps every option from an uncurated origin (%s)" % (feed, where),
                  len(sc2.get("options", [])) == base and base > 0,
                  "%d vs %d" % (len(sc2.get("options", [])), base))
        cur = fn(payload, origin_key="bushwick-brooklyn")
        check("%s uses the curated table when it can" % feed,
              (cur.get("_ground") or {}).get("source") == "curated",
              str((cur.get("_ground") or {}).get("source")))
        check("%s says so when it could not" % feed,
              bool((fn(payload, origin_key="Accra").get("_ground") or {}).get("message")))

    check("an estimated mode is marked estimated",
          all(m.get("estimated") for m in _g.estimate_modes(
              {"lat": 40.77, "lon": -73.96}, jfk, 540)))
    check("and a curated one is not",
          not any(m.get("estimated") for m in
                  _g.modes_for({"key": "bushwick-brooklyn"}, jfk, 540, e)[0]))
    check("an unpriced region is called assumed, not modelled",
          _g.region_support("GH") == "assumed" and _g.region_support("US") == "modelled")
    check("and the assumed message warns rather than quoting",
          "placeholder" in (_g.SUPPORT_NOTE["assumed"] or ""))
    # Leaning high is the entire point - an estimate that comes in under the
    # real fare manufactures the surprise it exists to prevent.
    est = _g.estimate_modes({"lat": 40.7736, "lon": -73.9566}, jfk, 540)[0]
    check("a Manhattan-to-JFK car estimate is not cheap", est["fare_cents"] >= 6500,
          "$%.0f - under about $65 and it stops being a warning" % (est["fare_cents"] / 100))

    print("\nplaces")
    for typed, want in (("london", "LON"), ("LHR", "LHR"), ("Paris, France", "PAR"),
                        ("Shoreditch, London EC2A", "LON"),
                        ("Wyckoff Ave, Bushwick, Brooklyn 11237", "NYC"),
                        ("10 Downing St, London SW1A 2AA", "LON")):
        got, how, problem = _p.resolve(typed)
        check("%r resolves to %s" % (typed[:34], want), got == want, "%s (%s)" % (got, problem))
    # A metro code and its airports are the same request. A prefix test saying
    # otherwise ("does LHR start with LO") is the kind of near-miss that tells
    # somebody their London search was not a London search.
    for c, i, want in (("LON", "LHR", True), ("LON", "LGW", True), ("LON", "JFK", False),
                       ("NYC", "EWR", True), ("TYO", "NRT", True), ("LHR", "LHR", True),
                       ("PAR", "LHR", False), ("", "LHR", False), ("LON", "", False)):
        check("%s covers %s is %s" % (c or "''", i or "''", want),
              _p.covers(c, i) is want)

    code, how, problem = _p.resolve("Narnia")
    check("an unknown place is refused, not guessed", code is None and bool(problem))
    check("and the refusal tells the user what to type instead",
          "airport code" in problem)

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

    # ------------------------------------------------------------ par
    # Par is a SPECIFICATION scored through the real scorer, never a reading
    # of the results. Every property below is one a plausible shortcut breaks.
    import copy
    import par as parmod
    enr = adapter.load_enrichment()
    full = adapter.from_duffel(draw, checked_bags=1)
    offers = adapter._duffel_offers(draw)
    few = copy.deepcopy(draw); few["data"]["offers"] = offers[:3]
    dear = copy.deepcopy(draw)
    dear["data"]["offers"] = sorted(offers, key=lambda o: -float(o["total_amount"]))[:6]
    p_full, p_few, p_dear = (adapter.from_duffel(r, checked_bags=1)["query"]["route_par_cents"]
                             for r in (draw, few, dear))
    check("par is the same whatever the search returned",
          p_full == p_few == p_dear, "%s / %s / %s" % (p_full, p_few, p_dear))
    check("par is modelled and says so",
          full["_par"].get("source") == "modelled"
          and any("modelled, not curated" in n for n in full["notes"]))
    check("a modelled par carries its whole basis",
          all(k in full["_par"] for k in ("miles", "market", "season_factor", "ledger", "reference")))
    check("par ledger reconciles to the par",
          sum(l["cents"] for l in full["_par"]["ledger"]) == full["_par"]["par_cents"])
    check("the reference itinerary carries a specified on-time record, not an abstain",
          not any("on-time" in l["label"].lower() and "no " in l["label"].lower()
                  for l in full["_par"]["ledger"]))
    # A curated row wins, and says it is curated.
    cur = copy.deepcopy(enr)
    cur["ground"]["routes"] = {"JFK-LHR": {"par_cents": 99999, "basis": "test"}}
    csc = adapter.from_duffel(draw, checked_bags=1, enr=cur)
    check("a curated route par overrides the model",
          csc["query"]["route_par_cents"] == 99999 and csc["_par"]["source"] == "curated")

    # The fare model against routes with a known price.
    jfk = dict(enr["airports"]["airports"]["JFK"], iata="JFK")
    lhr = dict(enr["airports"]["airports"]["LHR"], iata="LHR")
    bcn = dict(enr["airports"]["airports"]["BCN"], iata="BCN")
    lax = {"iata": "LAX", "lat": 33.9425, "lon": -118.4081, "country": "US"}
    nrt = {"iata": "NRT", "lat": 35.772, "lon": 140.3929, "country": "JP"}
    def fare(o, d, date): return parmod.reference_fare_cents(o, d, date)[0]
    check("JFK-LHR shoulder fare is a real main-cabin-with-bag price",
          40000 <= fare(jfk, lhr, "2026-11-18") <= 56000, str(fare(jfk, lhr, "2026-11-18")))
    check("JFK-LAX is priced as a domestic route, not a transatlantic one",
          25000 <= fare(jfk, lax, "2026-11-18") <= 40000, str(fare(jfk, lax, "2026-11-18")))
    check("LHR-BCN is priced as an LCC market",
          7000 <= fare(lhr, bcn, "2026-11-18") <= 14000, str(fare(lhr, bcn, "2026-11-18")))
    check("LAX-NRT is priced as transpacific",
          55000 <= fare(lax, nrt, "2026-11-18") <= 90000, str(fare(lax, nrt, "2026-11-18")))
    check("July costs more than November across the Atlantic",
          fare(jfk, lhr, "2026-07-12") > fare(jfk, lhr, "2026-11-18") * 1.3)
    check("the fare rises with distance",
          fare(lhr, bcn, "2026-05-01") < fare(jfk, lhr, "2026-05-01") < fare(lax, nrt, "2026-05-01"))
    check("market class is a real partition",
          parmod.market_class("US", "GB") == "transatlantic"
          and parmod.market_class("GB", "ES") == "intra-eu"
          and parmod.market_class("US", "US") == "domestic-na"
          and parmod.market_class("US", "JP") == "transpacific")
    mi = parmod.haversine_mi(jfk["lat"], jfk["lon"], lhr["lat"], lhr["lon"])
    check("an eastbound long-haul reference is an overnight",
          parmod.reference_departure_minutes(jfk, lhr, mi) == 19 * 60 + 30)
    check("a westbound long-haul reference leaves in the morning",
          parmod.reference_departure_minutes(lhr, jfk, mi) == 10 * 60)
    import ground as groundmod
    p1, b1 = parmod.par_for(jfk, lhr, "2026-11-18", enr, scorer, groundmod)
    p2, _ = parmod.par_for(jfk, lhr, "2026-11-18", enr, scorer, groundmod)
    check("par_for is pure", p1 == p2)
    # a holiday week is dearer by specification: the day before Thanksgiving on a US route, not on a European one
    hf_us, why_us = parmod.holiday_factor("domestic-na", "2026-11-25", ("US", "US"))
    hf_eu, _ = parmod.holiday_factor("intra-eu", "2026-11-25", ("GB", "FR"))
    check("par knows the day before Thanksgiving on a US route", hf_us > 1.5 and "Thanksgiving" in (why_us or ""), (hf_us, why_us))
    check("and not on a European one", hf_eu == 1.0, hf_eu)
    check("an ordinary November day carries no holiday factor", parmod.holiday_factor("domestic-na", "2026-11-18", ("US", "US"))[0] == 1.0)
    p3, b3 = parmod.par_for(jfk, lhr, "2026-12-24", enr, scorer, groundmod)
    check("Christmas week raises par and the basis says so", p3 > p1 and b3.get("holiday") == "Christmas week", (p3, p1, b3.get("holiday")))
    check("par is the reference ledger, all in - more than the fare, less than double it",
          b1["fare_cents"] < p1 < 2 * b1["fare_cents"], str((b1["fare_cents"], p1)))
    syd = {"iata": "SYD", "lat": -33.9399, "lon": 151.1753, "country": "AU"}
    _, bs = parmod.par_for(jfk, syd, "2026-11-18", enr, scorer, groundmod)
    check("beyond nonstop range the reference allows a connection",
          any("connection" in l["label"] for l in bs["ledger"]))
    nn = []
    dp, db = adapter.route_par("JFK", "QQQ", "2026-11-18", enr, nn)
    check("an airport with no coordinates gets the default par AND a note",
          dp == 105000 and any("cannot be modelled" in x for x in nn))
    check("and that default is labelled a default, never a modelled number",
          db.get("source") == "default")

    # ---- the Delta supplement: Google Flights through SerpApi, replayed offline
    print("\nserpapi supplement (synthesized sample)")
    with open(os.path.join(HERE, "..", "adapter_samples", "serpapi-jfk-lhr.json"), encoding="utf-8") as fh:
        serp = json.load(fh)
    check("the SerpApi sample says it was synthesized, not captured",
          bool((serp.get("_provenance") or {}).get("synthesized")))
    ss = adapter.from_serpapi(serp, carriers=("DL",))
    check("from_feed dispatches a Google Flights payload to the supplement adapter",
          adapter.from_feed(serp)["fixture_id"] == ss["fixture_id"])
    ids = [o["option_id"] for o in ss["options"]]
    check("asked for Delta only, only Delta survives: the Virgin nonstop and the Delta+Air France pair are dropped, and say why",
          len(ids) == 2 and all(i.startswith("google-dl") for i in ids)
          and sum("not one of the carriers" in d["why"] for d in ss["_dropped"]) == 2, str(ids))
    bos = next((o for o in ss["options"] if o["layovers"] and o["layovers"][0]["airport"] == "BOS"), None)
    check("the connection through BOS, which no table here knows, is KEPT: BOS's offset is worked out from the flight's own "
          "duration, New York's -05:00 in November, and a note says so",
          bos is not None and bos["segments"][0]["arrival_local"].endswith("-05:00")
          and bos["segments"][1]["departure_local"].endswith("-05:00")
          and any("worked out from the flight's own duration" in n for n in ss["notes"]), str(bos and [s["arrival_local"] for s in bos["segments"]]))
    check("a flight number is the digits after the carrier code, even when the code has a digit: "
          "B6 1024 is 1024, U2 2323 is 2323, W9 5378 is 5378, DL 1 is 1",
          [adapter._serp_number(x) for x in ("B6 1024", "U2 2323", "W9 5378", "DL 1", "9W 12")] == ["1024", "2323", "5378", "1", "12"])
    check("the arithmetic itself: 21:15 at -05:00 plus 7h05 lands 09:20 local, which is +00:00; a flight that "
          "would need +16:00 is refused",
          adapter._offset_by_duration("2026-11-18T21:15:00-05:00", "2026-11-19T09:20:00", 425, forward=True) == "+00:00"
          and adapter._offset_by_duration("2026-11-19T09:20:00+00:00", "2026-11-18T21:15:00", 425, forward=False) == "-05:00"
          and adapter._offset_by_duration("2026-11-18T21:15:00-05:00", "2026-11-20T09:20:00", 425, forward=True) is None)
    blind = json.loads(json.dumps(serp))
    for it in (blind.get("best_flights") or []) + (blind.get("other_flights") or []):
        for f in it.get("flights") or []:
            f["departure_airport"]["id"] = "QQA"; f["arrival_airport"]["id"] = "QQB"
    sb = adapter.from_serpapi(blind, carriers=())
    check("a flight with NEITHER end's zone known still drops, naming the airport: nothing to work from",
          not sb.get("options") and any("QQ" in d["why"] for d in (sb.get("_dropped") or sb.get("dropped") or [])))
    every = adapter.from_serpapi(serp, carriers=())
    check("asked for every airline (the default since 2026-09-24), the Virgin nonstop and the Delta+Air France pair stay",
          len(every["options"]) == 4 and not any("not one of the carriers" in d["why"] for d in every["_dropped"]),
          str([o["option_id"] for o in every["options"]]))
    main = adapter.from_serpapi(serp, carriers=("DL",))
    dup = json.loads(json.dumps(adapter.from_serpapi(serp, carriers=("DL",))))
    cheaper = json.loads(json.dumps(dup["options"][0])); cheaper["tickets"][0]["price"]["base_cents"] -= 5000
    dearer = json.loads(json.dumps(dup["options"][1])); dearer["tickets"][0]["price"]["base_cents"] += 5000
    merged = adapter.merge_scenarios(json.loads(json.dumps(main)), dict(dup, options=[cheaper, dearer]), "test")
    # bag fees by route (2026-09-24): a row's short_haul block on a domestic or regional trip, its long-haul tiers otherwise
    enr0 = adapter.load_enrichment()
    lax = {"LAX": {"lat": 33.94, "lon": -118.41, "country": "US"}}
    check("short-haul: JFK-LAX is one country; LHR-FCO is 1,400 km; JFK-LHR is neither; an unknown end reads as long-haul",
          adapter._short_haul("JFK", "LAX", enr0, lax) and adapter._short_haul("LHR", "FCO", enr0)
          and not adapter._short_haul("JFK", "LHR", enr0) and not adapter._short_haul("QQA", "QQB", enr0))
    fake = {"brands": {"XX:BASIC": {"feed_name": "Basic", "includes_checked": 0, "includes_carry_on": True,
                                     "tiers": [{"piece": 1, "amount_cents": 9000}],
                                     "short_haul": {"includes_checked": 0, "includes_carry_on": False,
                                                    "tiers": [{"piece": 1, "amount_cents": 4000}]}}},
            "_default": {"tiers": [{"piece": 1, "amount_cents": 10000}], "note": "long"},
            "_default_short_haul": {"tiers": [{"piece": 1, "amount_cents": 7000}], "note": "short"}}
    fe = dict(enr0, fares=fake)
    r_long, d_long = adapter._fare_row(fe, "XX", "Basic", False)
    r_short, d_short = adapter._fare_row(fe, "XX", "Basic", True)
    check("a row's short-haul block replaces its tiers and carry-on only on a short-haul trip",
          r_long["tiers"][0]["amount_cents"] == 9000 and r_long["includes_carry_on"] is True
          and r_short["tiers"][0]["amount_cents"] == 4000 and r_short["includes_carry_on"] is False and not d_long and not d_short)
    check("an unknown fare takes the short-haul default short-haul and the long-haul default otherwise, flagged either way",
          adapter._bag_tiers(fe, "ZZ", "Basic", True)[0][0]["amount_cents"] == 7000
          and adapter._bag_tiers(fe, "ZZ", "Basic", False)[0][0]["amount_cents"] == 10000
          and adapter._bag_tiers(fe, "ZZ", "Basic", True)[1] is True)
    # the cheapest tickets always reach the page, whatever their rank (2026-09-24, the Google Flights price check)
    sys.path.insert(0, os.path.join(HERE, "..", "ui", "mock"))
    import data as mockdata
    with open(os.path.join(HERE, "..", "adapter_samples", "duffel-jfk-lhr.json"), encoding="utf-8") as fh:
        dfl = json.load(fh)
    keep = (mockdata.POOL, mockdata.TOP_PER_TARGET)
    try:
        mockdata.POOL, mockdata.TOP_PER_TARGET = 1, 1
        slim = mockdata.build_slim(dfl)
    finally:
        mockdata.POOL, mockdata.TOP_PER_TARGET = keep
    shown = {e["ticket_cents"] for v in slim["results"].values() if isinstance(v, list) for e in v}
    scd = adapter.from_feed(dfl)
    feas = [o for o in scd["options"] if not any(sg["marketing"]["carrier"] == "ZZ" for sg in o["segments"])
            and not scorer.score(scd, o, "reference").infeasible_reason and not scorer.score(scd, o, "reference").filtered_reason]
    five = sorted(adapter._ticket_cents(o) for o in feas)[:mockdata.CHEAPEST_TICKETS]
    check("with the pool cut to the single best per target, the five cheapest tickets still reach the page",
          all(t in shown for t in five) and len(five) == 5, str((five, sorted(shown)[:8])))

    # round trips (2026-09-24): a return the airline sells with the chosen outbound for less than two one-ways
    opts1 = [o for o in scd["options"] if len(o.get("tickets") or []) == 1]
    o0 = opts1[0]; k0 = adapter._itin_key(o0); ow0 = adapter._ticket_cents(o0)
    rt_cheap = mockdata.build_slim(dfl, round_trip={"outbound_cents": 30000, "totals": {k0: 30000 + ow0 - 5000}})
    rts = [e for v in rt_cheap["results"].values() if isinstance(v, list) for e in v if e.get("round_trip")]
    check("a round-trip fare $50 under the two one-ways makes the return's ticket what the round trip adds, and says so",
          rts and all(e["ticket_cents"] == ow0 - 5000 and e["round_trip"]["one_way_cents"] == ow0
                      and e["round_trip"]["total_cents"] == 30000 + ow0 - 5000 for e in rts), str([(e["ticket_cents"], ow0) for e in rts[:2]]))
    rt_dear = mockdata.build_slim(dfl, round_trip={"outbound_cents": 30000, "totals": {k0: 30000 + ow0 + 1000}})
    check("a round-trip fare dearer than the two one-ways changes nothing",
          not any(e.get("round_trip") for v in rt_dear["results"].values() if isinstance(v, list) for e in v))
    # the server matches the two-flight offers to the EXACT outbound chosen, number and departure minute
    offs = (dfl.get("data") or {}).get("offers") or dfl.get("offers") or []
    oa, ob = offs[0], offs[1]
    sa = oa["slices"][0]["segments"]
    pair = {"flights": [g["marketing_carrier"]["iata_code"] + str(int(g["marketing_carrier_flight_number"])) for g in sa],
            "origin": oa["slices"][0]["origin"]["iata_code"], "destination": oa["slices"][0]["destination"]["iata_code"],
            "date": sa[0]["departing_at"][:10], "depart": sa[0]["departing_at"][:16], "ticket_cents": 40000, "source": "feed"}
    good = {"slices": [oa["slices"][0], ob["slices"][0]], "total_amount": "500.00", "total_currency": "USD"}
    moved = json.loads(json.dumps(oa["slices"][0])); moved["segments"][0]["departing_at"] = "2026-12-01T06:00:00"
    decoy = {"slices": [moved, ob["slices"][0]], "total_amount": "100.00", "total_currency": "USD"}
    import server as srv
    calls = {"serp": 0}
    real_search, real_rt = srv.live.search, srv.live.serp_round_trip
    try:
        srv.live.search = lambda q: ({"data": {"offers": [decoy, good]}}, {"source": "api"})
        def fake_rt(*a, **k):
            calls["serp"] += 1
            return None, {"source": "none"}
        srv.live.serp_round_trip = fake_rt
        where = {"date": "2026-11-25", "origin": {"code": "LON"}, "destination": {"code": "NYC"}}
        got = srv._round_trip_totals({"pair": pair, "adults": 1}, where)
        kb = tuple((g["marketing_carrier"]["iata_code"], int(g["marketing_carrier_flight_number"]), g["departing_at"][:16])
                   for g in ob["slices"][0]["segments"])
        check("the server prices the return from the offer with the chosen outbound, not a cheaper one with the same "
              "flight numbers at another time, and asks Google nothing when the feed sold the outbound",
              got and got["totals"].get(kb) == 50000 and got["outbound_cents"] == 40000 and calls["serp"] == 0, str(got and list(got["totals"].values())))
        srv._round_trip_totals({"pair": dict(pair, source="google"), "adults": 1}, where)
        check("an outbound Google sold asks Google too", calls["serp"] == 1)
        check("a pair the browser sent with a malformed field is ignored",
              srv._round_trip_totals({"pair": dict(pair, flights=["<script>"]), "adults": 1}, where) is None)
    finally:
        srv.live.search, srv.live.serp_round_trip = real_search, real_rt
    check("merging: a supplement fare for flights the feed already sells at that price or less is left out; a cheaper one stays",
          len(merged["options"]) == len(main["options"]) + 1
          and min(adapter._ticket_cents(o) for o in merged["options"] if adapter._itin_key(o) == adapter._itin_key(cheaper)) == adapter._ticket_cents(cheaper))
    with_geo = adapter.from_serpapi(serp, carriers=("DL",), geo={"BOS": {"tz": "America/New_York", "lat": 42.36, "lon": -71.0, "country": "US", "city": "Boston"}})
    check("with the zone borrowed from the feed, the BOS connection is kept, with its layover at BOS",
          len(with_geo["options"]) == 2 and any(o["layovers"] and o["layovers"][0]["airport"] == "BOS" for o in with_geo["options"]))
    dl1 = ss["options"][0]
    check("every timestamp carries an explicit offset: JFK in November is -05:00, Heathrow +00:00",
          dl1["segments"][0]["departure_local"] == "2026-11-18T21:15:00-05:00"
          and dl1["segments"][0]["arrival_local"] == "2026-11-19T09:20:00+00:00"
          and all(re.search(r"[+-]\d\d:\d\d$", s[k]) for o in with_geo["options"] for s in o["segments"] for k in ("departure_local", "arrival_local")))
    check("the whole-dollar price becomes cents", dl1["tickets"][0]["price"]["base_cents"] == 51200)
    check("legroom becomes pitch, the power sentence a claim, the wifi sentence a published amenity (paid)",
          dl1["segments"][0]["claims"].get("seat_pitch_inches") == 31
          and str(dl1["segments"][0]["claims"].get("power", "")).startswith("Power")
          and any(w["segment"] == "s1" and w["available"] and w["cost"] == "paid" for w in ss["_feed"]["wifi_published"]))
    check("the aircraft is a name, not a code, so the equipment code abstains",
          dl1["segments"][0]["equipment_code"] == "UNKNOWN" and "A330" in dl1["segments"][0]["equipment_name"])
    check("the fare is priced as Delta's no-bag brand and the note says so",
          dl1["tickets"][0]["fare_brand_name"] == "Basic Economy" and dl1["tickets"][0]["entitlements"]["checked_included"] == 0
          and len(dl1["tickets"][0]["checked_bag_fee_tiers"]) >= 1 and any("unknown is never cheap" in n for n in ss["notes"]))
    led = scorer.score(ss, dl1, "reference")
    check("the scorer reads a Google itinerary like any other: it reconciles, and the ticket line is the price",
          led.reconciles() and led.lines[0].amount_cents == 51200)
    card = scorer.report_card(ss, dl1)
    check("and the report card grades it, nonstop routing A+", card["grade"] in {l for l, _ in scorer.CARD_POINTS}
          and next(p["letter"] for p in card["parts"] if p["id"] == "routing") == "A+")
    check("the booking goes to the airline, not to Google",
          "delta" in dl1["booking"][0]["who"].lower() and "own site" in dl1["booking"][0]["note"])
    check("the CO2 estimate rides along in kilograms for the page", dl1["_google"]["emissions_kg"] == 512)
    # merging into the main feed's scenario
    with open(os.path.join(HERE, "..", "adapter_samples", "duffel-jfk-lhr.json"), encoding="utf-8") as fh:
        duff = json.load(fh)
    main_sc = adapter.from_feed(duff)
    n0 = len(main_sc["options"])
    extra = adapter.from_serpapi(serp, carriers=("DL",), geo=adapter.duffel_geo(duff))
    n1 = len(extra["options"])
    check("with the feed's own zones, the BOS connection is kept too", n1 == 2, str([o["option_id"] for o in extra["options"]]))
    merged = adapter.merge_scenarios(main_sc, extra, "Delta via Google Flights")
    check("the supplement's options join the feed's, ids unique, par and profiles the feed's",
          len(merged["options"]) == n0 + n1 and len({o["option_id"] for o in merged["options"]}) == n0 + n1
          and merged["_feed"]["supplements"][0]["options"] == n1
          and all(o["segments"][0]["marketing"]["carrier"] == "DL" for o in merged["options"][n0:]))
    check("the merged scenario still scores end to end",
          all(scorer.score(merged, o, "reference").reconciles() for o in merged["options"]))
    # the price signal: Google's context, our cheapest, one deterministic call
    sig = adapter.price_signal(serp, 29500)
    check("a fare under the typical range says book, with the range in the reason",
          sig and sig["verdict"] == "book" and "$380 to $620" in sig["reason"] and sig["low_cents"] == 38000)
    check("a fare above the range says wait", adapter.price_signal(serp, 70000)["verdict"] == "wait")
    check("a fare inside the range is typical", adapter.price_signal(serp, 45000)["verdict"] == "typical")
    check("the history is carried in cents, oldest first, at most sixty points",
          len(sig["history"]) == 60 and sig["history"][0][1] == 28500 and sig["history"][0][0] < sig["history"][-1][0])
    no_range = json.loads(json.dumps(serp)); no_range["price_insights"].pop("typical_price_range")
    check("with no range, the recent median decides", adapter.price_signal(no_range, 20000)["verdict"] == "book" and "median" in adapter.price_signal(no_range, 20000)["reason"])
    check("no insights at all is no signal, never a made-up one", adapter.price_signal({}, 29500) is None and adapter.price_signal(None, 29500) is None)
    ad_src = open(os.path.join(HERE, "..", "adapter.py"), encoding="utf-8").read()
    check("the adapter never imports the live module (the key stays where it is)",
          not re.search(r"^\s*(import live\b|from live\b)", ad_src, re.M))

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
