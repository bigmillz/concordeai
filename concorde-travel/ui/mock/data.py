#!/usr/bin/env python3
"""Score a real capture and write slim.json, the data every mockup inlines.

    python3 concorde-travel/ui/mock/data.py                      # the trimmed 23-offer sample
    python3 concorde-travel/ui/mock/data.py path/to/capture.json # a fuller capture
    python3 concorde-travel/ui/mock/build.py                     # then inline it

Nothing in the mockups is a placeholder: every dollar figure, grade, ledger line
and leg length here came out of scorer.py over a real Duffel response, at the
modelled par. The old slim.json was built by hand from a script that never made
it into the repo, against the retired $1,050 guess - which is why its grades
were almost all A+ and why the second design round could not simply reuse it.

Two additions over what the page itself gets from /api/live:

- `grid`: the effective cost of every option at 66 weightings spanning the
  triangle between the three named targets. The Dial mockup re-ranks live as
  the handle moves, and rather than approximate the scorer in JavaScript (two
  models drift) each point is the real scorer at that exact profile. Hard rule
  1 in the browser: same weights, same order, every time.
- `results`: the pool sorted under each named target, so a mockup can flip
  between them without sorting anything itself.
"""
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "concorde-travel"))
import adapter    # noqa: E402
import scorer     # noqa: E402
import server     # noqa: E402

POOL = 36          # options carried; a real page shows about this many before "more"
STEPS = 10         # triangle resolution: (STEPS+1)(STEPS+2)/2 = 66 weightings
NAMED = {"cheapest": (1, 0, 0), "fastest": (0, 1, 0), "comfort": (0, 0, 1)}


def blend(a, b, c, profiles):
    """A profile on the triangle: a, b, c weight the three named targets."""
    p = [profiles["cheapest"], profiles["fastest"], profiles["comfort"]]
    return {
        "label": "dial",
        "hourly_value_cents": int(round(a * p[0]["hourly_value_cents"] + b * p[1]["hourly_value_cents"]
                                        + c * p[2]["hourly_value_cents"])),
        "comfort_weight": a * p[0]["comfort_weight"] + b * p[1]["comfort_weight"] + c * p[2]["comfort_weight"],
        "risk_weight": a * p[0]["risk_weight"] + b * p[1]["risk_weight"] + c * p[2]["risk_weight"],
    }


def grid_points():
    pts = []
    for i in range(STEPS + 1):
        for j in range(STEPS + 1 - i):
            pts.append((i / STEPS, j / STEPS, (STEPS - i - j) / STEPS))
    return pts


def carrier_name(display, code):
    head = display.split(" · ")[0].strip()
    parts = head.split()
    if parts and parts[-1].upper().startswith(code) and any(ch.isdigit() for ch in parts[-1]):
        parts = parts[:-1]
    return " ".join(parts) or code


def _cents(x):
    try:
        return int(round(float(x) * 100)) if x is not None else None
    except (TypeError, ValueError):
        return None


def _iso_minutes(d):
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?", d or "")
    return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) if m else None


def feed_details(offer):
    """Everything Duffel says about an offer that the scorer does not price:
    the aircraft by name, terminals, distance, cabin and fare basis, the
    published amenities, the fare's change and refund terms with their
    penalties, price-hold and payment deadlines, emissions, the loyalty
    programmes it credits. Absent fields stay absent; the page says so."""
    if not offer:
        return None
    c = offer.get("conditions") or {}
    ch, rf = c.get("change_before_departure") or {}, c.get("refund_before_departure") or {}
    pay = offer.get("payment_requirements") or {}
    segs = []
    for sl in offer.get("slices") or []:
        for sg in sl.get("segments") or []:
            px = (sg.get("passengers") or [{}])[0]
            am = ((px.get("cabin") or {}).get("amenities") or {})
            mk, op, ac = sg.get("marketing_carrier") or {}, sg.get("operating_carrier") or {}, sg.get("aircraft") or {}
            segs.append({
                "marketing": {"code": mk.get("iata_code"), "name": mk.get("name"), "number": sg.get("marketing_carrier_flight_number")},
                "operating": {"code": op.get("iata_code"), "name": op.get("name"), "number": sg.get("operating_carrier_flight_number")},
                "aircraft": {"code": ac.get("iata_code"), "name": ac.get("name")} if ac else None,
                "from": {"code": (sg.get("origin") or {}).get("iata_code"), "name": (sg.get("origin") or {}).get("name"), "terminal": sg.get("origin_terminal")},
                "to": {"code": (sg.get("destination") or {}).get("iata_code"), "name": (sg.get("destination") or {}).get("name"), "terminal": sg.get("destination_terminal")},
                "minutes": _iso_minutes(sg.get("duration")),
                "km": int(round(float(sg["distance"]))) if sg.get("distance") else None,
                "cabin": px.get("cabin_class_marketing_name") or px.get("cabin_class"),
                "fare_basis": px.get("fare_basis_code"),
                "bags": [{"type": b.get("type"), "quantity": b.get("quantity")} for b in (px.get("baggages") or [])],
                "wifi": am.get("wifi"), "seat": am.get("seat"), "power": am.get("power"),
                "fare_brand": sl.get("fare_brand_name"),
            })
    return {
        "base_cents": _cents(offer.get("base_amount")), "tax_cents": _cents(offer.get("tax_amount")),
        "emissions_kg": offer.get("total_emissions_kg"),
        "expires_at": offer.get("expires_at"), "price_guarantee_until": pay.get("price_guarantee_expires_at"),
        "pay_by": pay.get("payment_required_by"), "instant_payment": pay.get("requires_instant_payment"),
        "change": {"allowed": ch.get("allowed"), "penalty_cents": _cents(ch.get("penalty_amount"))},
        "refund": {"allowed": rf.get("allowed"), "penalty_cents": _cents(rf.get("penalty_amount"))},
        "owner": (offer.get("owner") or {}).get("name"),
        "carriage_url": (offer.get("owner") or {}).get("conditions_of_carriage_url"),
        "loyalty": offer.get("supported_loyalty_programmes") or [],
        "id_docs_required": offer.get("passenger_identity_documents_required"),
        "segments": segs,
    }


def build_slim(raw, origin_key="Bushwick, Brooklyn", checked_bags=1, origin_full=None, source_note=None, built_from="a live search",
               dest_point=None, destination_full=None):
    """A feed payload -> everything mock 10 reads. server.py calls this for a
    live search; main() below calls it for the checked-in sample."""
    sc = adapter.from_feed(raw, origin_key=origin_key, checked_bags=checked_bags, dest_point=dest_point)
    # The raw offers, keyed the way the adapter names its options (the last ten
    # characters of the offer id), so the page can show everything the feed
    # said about a flight, not only what the scorer priced.
    offers = raw.get("data") if isinstance(raw, dict) else raw
    if isinstance(offers, dict):
        offers = offers.get("offers") or []
    by_tail = {re.sub(r"[^a-z0-9]+", "-", (o.get("id") or "x")[-10:].lower()): o for o in (offers or [])}
    if "error" in sc:
        raise ValueError("adapter: %s" % sc["error"])
    cov = adapter.coverage(sc)
    profiles = sc["query"]["profiles"]

    # The fictional test carrier is filtered here, not in the adapter: a real
    # search never returns it, and a mockup should look like a real search.
    opts = [o for o in sc["options"]
            if not any(s["marketing"]["carrier"] == "ZZ" for s in o["segments"])]

    # Pool: the best under each target, then filled by the reference order.
    ref = {o["option_id"]: scorer.score(sc, o, "reference") for o in opts}
    feasible = [o for o in opts if not ref[o["option_id"]].infeasible_reason
                and not ref[o["option_id"]].filtered_reason]
    # Pool: the best ten under each target, then the rest of the ranking sampled
    # evenly so the page carries the connections and the C-to-F grades a real
    # search returns. A pool of only the best looks like a brochure.
    pool_ids = []
    for prof in NAMED:
        ranked = sorted(feasible, key=lambda o: (scorer.score(sc, o, prof).effective_cents, o["option_id"]))
        for o in ranked[:10]:
            if o["option_id"] not in pool_ids:
                pool_ids.append(o["option_id"])
    rest = [o for o in sorted(feasible, key=lambda o: (ref[o["option_id"]].effective_cents, o["option_id"]))
            if o["option_id"] not in pool_ids]
    need = max(0, POOL - len(pool_ids))
    if rest and need:
        step = len(rest) / float(need)
        for k in range(need):
            o = rest[min(len(rest) - 1, int(k * step))]
            if o["option_id"] not in pool_ids:
                pool_ids.append(o["option_id"])
    pool = [o for o in feasible if o["option_id"] in pool_ids]

    pts = grid_points()
    for k, (a, b, c) in enumerate(pts):
        profiles["dial-%d" % k] = blend(a, b, c, profiles)

    # Straight off the feed, for the page: the airline's own logo (Duffel
    # serves an SVG per carrier), and the published cabin amenities, which the
    # scorer deliberately does not price (hard rule 2) but a "wants" chip may
    # show. Keyed by the offer id suffix the adapter keeps in option_id.
    airlines, amenities = {}, {}
    for off in raw.get("data", {}).get("offers", []):
        for who in [off.get("owner")] + [s.get(k) for sl in off.get("slices", []) for s in sl.get("segments", [])
                                          for k in ("marketing_carrier", "operating_carrier")]:
            if who and who.get("iata_code"):
                airlines.setdefault(who["iata_code"], {"name": who.get("name"), "logo": who.get("logo_symbol_url"),
                                                        "lockup": who.get("logo_lockup_url")})
        wifi = power = False
        wifi_cost = None
        for sl in off.get("slices", []):
            for sg in sl.get("segments", []):
                am = ((sg.get("passengers") or [{}])[0].get("cabin") or {}).get("amenities") or {}
                w = am.get("wifi") or {}
                wifi = wifi or bool(w.get("available"))
                if w.get("available") and w.get("cost") in ("free", "paid"):
                    # the long leg decides; a paid pass anywhere on the trip is a paid trip
                    wifi_cost = "paid" if (wifi_cost == "paid" or w["cost"] == "paid") else "free"
                power = power or bool((am.get("power") or {}).get("available"))
        amenities[(off["id"] or "")[-10:].lower().replace("_", "-")] = {"wifi": wifi, "power": power, "wifi_cost": wifi_cost}

    entries = {}
    for o in pool:
        v = server._view(sc, o, "reference")
        code = v["carrier"]
        e = collections.OrderedDict()
        e["id"] = v["option_id"]
        e["name"] = v["display_name"]
        e["carrier"] = code
        e["carrier_name"] = carrier_name(v["display_name"], code)
        e["flight"] = v["segments"][0]["flight"].replace(" ", "")
        e["operator"] = o["segments"][0]["operating"]["carrier"]
        e["brand"] = v["fare_brand"]
        e["route"] = v["route"].split(" - ")
        e["depart"] = v["depart_iso"]
        e["arrive"] = v["arrive_iso"]
        e["day_offset"] = v["day_offset"]
        e["stops"] = v["stops"]
        e["grade"] = v["grade"]
        e["reference_cents"] = v["reference_cents"]
        e["ticket_cents"] = v["ticket_cents"]
        e["door_minutes"] = v["door_to_door_minutes"]
        e["bags_included"] = v["bag_included"]
        e["rating"] = (v["carrier_rating"] or {}).get("rating")
        e["equipment"] = [s["equipment"] for s in v["segments"]]
        e["pitch"] = [(s["claims"] or {}).get("seat_pitch_inches") for s in v["segments"]]
        e["segments"] = [{"flight": s["flight"], "from": s["from"], "to": s["to"],
                          "dep": s["dep"], "arr": s["arr"], "equipment": s["equipment"]}
                         for s in v["segments"]]
        e["booking"] = v["booking"]
        # Per-target: effective, the ledger and the legs (the ground choice can
        # differ by target, so the legs are per target too).
        e["by"] = {}
        for prof in NAMED:
            pv = server._view(sc, o, prof)
            e["by"][prof] = {
                "effective_cents": pv["effective_cents"],
                "lines": [{"code": l["code"], "label": l["label"], "cents": l["amount_cents"],
                           "evidence": l["evidence"], "kind": l["kind"]} for l in pv["lines"]],
                "legs": [{"kind": l["kind"], "label": l["label"], "minutes": l["minutes"],
                          "quality": l["quality"], "tip": l["tip"]} for l in pv["bar"]],
            }
        # The reference profile lives at the top level (the first round's
        # mockups read it there) and is the ledger the GRADE is made of.
        e["effective_cents"] = v["effective_cents"]
        e["lines"] = [{"code": l["code"], "label": l["label"], "cents": l["amount_cents"],
                       "evidence": l["evidence"], "kind": l["kind"]} for l in v["lines"]]
        e["legs"] = [{"kind": l["kind"], "label": l["label"], "minutes": l["minutes"],
                      "quality": l["quality"], "tip": l["tip"]} for l in v["bar"]]
        e["changes"] = o["tickets"][0]["entitlements"].get("changes", "unknown")
        e["refundable"] = bool(o["tickets"][0]["entitlements"].get("refundable"))
        am = next((v for k, v in amenities.items() if k in e["id"]), {})
        e["wifi_published"] = bool(am.get("wifi")); e["power_published"] = bool(am.get("power"))
        e["wifi_cost"] = am.get("wifi_cost")
        e["layover_minutes"] = [l["minutes"] for l in e["legs"] if l["kind"] == "layover"]
        e["details"] = feed_details(by_tail.get(e["id"].rsplit("-", 1)[-1]))
        # What the bill needs to be rebuilt as choices change: the fare's own bag
        # ladder and seat terms, and every ground mode at each end with its fare
        # and minutes, so a rideshare/transit choice is a swap, not a re-score.
        t0 = o["tickets"][0]
        e["bag_tiers"] = t0.get("checked_bag_fee_tiers") or []
        e["seat_selection"] = t0["entitlements"].get("seat_selection")
        e["ground"] = {end: [{"mode": m["mode"], "kind": m.get("mode_kind"), "cents": m.get("fare_cents"),
                              "minutes": (m.get("door_to_door_minutes") or {}).get("p50"),
                              "estimated": bool(m.get("estimated")), "feasible": m.get("feasible", True)}
                             for m in (o.get("ground") or {}).get(src) or []]
                       for end, src in (("out", "outbound"), ("in", "arrival"))}
        # The scorer's ledger at every weighting, not only its total: the bill on the
        # page is then the scorer's own lines at the dial, to the cent, rather than a
        # rescaling of the nearest named target (which was $150 out in the middle).
        # Labels and evidence do not change with the weights, so only the amounts are
        # stored, aligned to one code sequence when the sequence is the same at every
        # point (it is, unless a rounding-to-zero drops a line somewhere).
        modes = {end: [m["mode"] for m in ((o.get("ground") or {}).get(src) or [])]
                 for end, src in (("out", "outbound"), ("in", "arrival"))}
        def which(led, code, end):
            ln = next((l for l in led.lines if l.code == code), None)
            if not ln:
                return -1
            return next((i for i, m in enumerate(modes[end]) if ln.evidence.startswith(m)), -1)
        leds = [scorer.score(sc, o, "dial-%d" % k) for k in range(len(pts))]
        e["grid"] = [led.effective_cents for led in leds]
        seqs = [[l.code for l in led.lines] for led in leds]
        gl = {"out": [which(led, "ground_out", "out") for led in leds],
              "in": [which(led, "ground_in", "in") for led in leds],
              "d2d": [led.door_to_door_minutes for led in leds]}
        if all(sq == seqs[0] for sq in seqs):
            gl["codes"] = seqs[0]
            gl["cents"] = [[l.amount_cents for l in led.lines] for led in leds]
        else:
            gl["pairs"] = [[[l.code, l.amount_cents] for l in led.lines] for led in leds]
        e["gridlines"] = gl
        entries[e["id"]] = e

    results = collections.OrderedDict()
    for prof in ("cheapest", "fastest", "comfort", "reference"):
        eff = lambda i: (entries[i]["by"][prof]["effective_cents"] if prof in NAMED
                         else entries[i]["effective_cents"])
        results[prof] = [entries[i] for i in sorted(entries, key=lambda i: (eff(i), i))]

    out = collections.OrderedDict()
    out["_provenance"] = {
        "built_from": built_from,
        "note": "Real Duffel search, scored by scorer.py at the modelled par. A duffel_test_ "
                "token invents some prices and schedules; the fictional carrier ZZ is filtered.",
        "options_in_capture": len(sc["options"]), "pool": len(pool),
    }
    out["query"] = {
        "origin": sc["query"]["origin"]["label"],
        "origin_full": origin_full or "Wyckoff Ave & Myrtle Ave, Bushwick, Brooklyn 11237",
        "destination": (dest_point or {}).get("city") or sc["query"]["destination"]["label"],
        "destination_full": destination_full or None,
        "date": sc["query"]["depart_date"],
        "adults": 1, "bags": 1,
        "par_cents": sc["query"]["route_par_cents"],
        "total_found": len(sc["options"]), "dropped": len(sc.get("_dropped") or []),
    }
    out["par"] = {k: sc["_par"].get(k) for k in
                  ("source", "par_cents", "fare_cents", "miles", "market", "month",
                   "season_factor", "holiday_factor", "holiday", "reads_as", "reference", "ledger")}
    out["profiles"] = {k: profiles[k] for k in ("reference", "cheapest", "fastest", "comfort")}
    out["airlines"] = {k: airlines[k] for k in sorted(airlines) if k != "ZZ"}
    # The curated airport facts the page's hover cards need: which union a
    # border check is against, the minimum connection, and the hours the
    # terminal's shops and food keep (so a layover that lands after they close
    # can be called out). An airport the table does not know is listed as
    # uncurated, so the page says so rather than guessing.
    apts = json.load(open(os.path.join(HERE, "..", "..", "enrichment", "airports.json"), encoding="utf-8"))
    apts = apts.get("airports") or apts
    codes = sorted({c for e in entries.values() for c in e["route"]})
    out["airports"] = {}
    for c in codes:
        a = apts.get(c)
        if not a:
            out["airports"][c] = {"curated": False}
            continue
        out["airports"][c] = {"curated": True, "city": a.get("city"), "border": a.get("border"),
                              "mct": (a.get("mct_minutes") or {}).get("default"),
                              "services": [{"what": s.get("what"), "hours": s.get("hours_local"), "note": s.get("note")}
                                           for s in (a.get("services") or [])]}
    out["grid_points"] = [[round(a, 2), round(b, 2), round(c, 2)] for a, b, c in pts]
    out["verdict"] = cov.get("verdict")
    out["gaps"] = cov.get("gaps", [])
    out["strengths"] = cov.get("strengths", [])
    out["results"] = results

    out["query"]["adults"] = int(sc["query"].get("adults") or 1) if isinstance(sc.get("query"), dict) else 1
    out["query"]["bags"] = checked_bags
    if source_note:
        out["source_note"] = source_note
    return out


def main(path):
    raw = json.load(open(path, encoding="utf-8"))
    try:
        out = build_slim(raw, built_from=os.path.basename(path))
    except ValueError as exc:
        sys.exit(str(exc))
    dest = os.path.join(HERE, "slim.json")
    json.dump(out, open(dest, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    pool = out["results"]["reference"]
    grades = collections.Counter(e["grade"] for e in pool)
    print("%s: %d options in capture, %d in pool, %.0f KB" % (
        dest, out["_provenance"]["options_in_capture"], len(pool), os.path.getsize(dest) / 1024))
    print("grades in pool:", dict(sorted(grades.items())))
    for prof in NAMED:
        top = out["results"][prof][0]
        print("  %-8s %-34s $%d" % (prof, top["name"][:34], top["by"][prof]["effective_cents"] // 100))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else os.path.join(ROOT, "concorde-travel", "adapter_samples", "duffel-jfk-lhr.json"))
