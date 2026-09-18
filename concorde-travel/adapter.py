#!/usr/bin/env python3
"""Live inventory adapter. Build step six.

Turns a third-party flight search into a scenario the scorer already knows how
to read. Everything downstream - scorer, narrator, UI - is untouched; a live
search and a hand-written fixture arrive at `score()` in exactly the same shape.

Hard rule 5 survives intact: the scorer and its tests never call this, and this
module's own tests replay a recorded payload rather than the network.

WHAT A FEED ACTUALLY GIVES YOU, measured against a real Kiwi response rather
than guessed at - this is the finding that shapes the whole module:

  * Timestamps with NO UTC offset. "2026-11-12T20:00:00" and nothing more. The
    scorer's time model requires an explicit offset, so the adapter attaches one
    from the curated zone table. Without that join the feed cannot satisfy the
    schema at all.
  * Marketing carrier only. "KL6101" on JFK-LHR is a codeshare - KLM does not
    fly that route - and the feed never says who the metal belongs to. A
    four-digit number under a two-letter code is the signature.
  * No equipment code anywhere. Every aircraft, cabin and connectivity claim
    therefore ABSTAINS. Hard rule 2 is not a stylistic preference here; the data
    to violate it does not exist.
  * Baggage as a count already priced in, not a fee schedule, and no fare brand.
    The bags-flip arithmetic - the cheapest real win in the whole product - has
    nothing to work with.

So the feed supplies the skeleton and the price. Everything that makes this
product different is the join against `enrichment/`, and where that join misses,
the adapter abstains loudly. `coverage()` reports exactly what was known and
what was guessed, because a grade computed over mostly-abstained data is not
worth the same as one computed over a curated route, and the interface should
be able to say so.

TWO FEEDS, AND THEY ARE NOT INTERCHANGEABLE. `from_kiwi` and `from_amadeus`
produce the same scenario shape and are read by the same scorer, but they do not
know the same things, and pretending otherwise would be the whole failure this
module exists to prevent:

  * Amadeus names the OPERATING carrier, and omits the field when it is the same
    as the marketing one. An absent `operating` is therefore information, not a
    gap - the opposite of Kiwi, where its absence means nothing is known.
  * Amadeus carries an equipment code, so the aircraft layer stops abstaining.
    It is still only a TYPE: "789" does not say which of a carrier's 787-9 cabins
    you get, so the join against `enrichment/fleets.json` produces a HEDGED claim
    with an observed frequency and never a fact. Hard rule 2 is not relaxed
    because better data turned up; it is exactly what the better data is for.
  * Amadeus carries a fare brand and an included-bag count, which is what finally
    lets the bags-versus-fare arithmetic run - joined against
    `enrichment/fares.json` for the fee the feed still does not give.
  * Amadeus is GDS content on ONE ticket, so there is no self-transfer and no
    virtual interlining. Kiwi's two-ticket itineraries are a real part of the
    market that this feed simply cannot see. That is a coverage loss, not a
    quality gain, and it is why both adapters are kept.
  * Neither feed puts a UTC offset on a timestamp. The zone join is not a Kiwi
    workaround; it is load-bearing for both.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ENRICH = os.path.join(HERE, "enrichment")

# A two-letter code with a four-digit number is a codeshare in all but name.
CODESHARE = re.compile(r"^[A-Z0-9]{2}(\d{4})$")


def load_enrichment() -> Dict[str, Any]:
    out = {}
    for name in ("airports", "carriers", "ground", "fleets", "fares"):
        with open(os.path.join(ENRICH, name + ".json"), encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


# --------------------------------------------------------------------- time

def offset_for(zone: Dict[str, Any], date_str: str) -> Optional[str]:
    """The UTC offset for a local date, from curated transition dates.

    Deliberately not a timezone database: the scorer must run with no I/O and
    no versioned external state, and a tzdata update that silently moved a
    golden number would be very hard to notice."""
    year = date_str[:4]
    window = (zone.get("dst") or {}).get(year)
    if not window:
        return None                      # outside the curated years - say so
    start, end = window
    return zone["daylight"] if start <= date_str < end else zone["standard"]


def stamp(local_naive: str, iata: str, enr: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """'2026-11-12T20:00:00' + JFK -> '2026-11-12T20:00:00-05:00'."""
    ap = enr["airports"]["airports"].get(iata)
    if not ap:
        return None, "no curated timezone for %s" % iata
    zone = enr["airports"]["zones"].get(ap["zone"])
    if not zone:
        return None, "no zone rules for %s" % ap["zone"]
    off = offset_for(zone, local_naive[:10])
    if not off:
        return None, "no daylight-saving rules curated for %s" % local_naive[:4]
    return local_naive[:19] + off, None


def _hhmm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def _within(hours: str, clock: int) -> bool:
    if hours == "any":
        return True
    lo, hi = _hhmm(hours[:5]), _hhmm(hours[6:11])
    return lo <= clock <= hi if lo <= hi else (clock >= lo or clock <= hi)


# ------------------------------------------------------------------- ground

def ground_for(origin_key: str, iata: str, clock_minutes: int,
               enr: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    g = enr["ground"]["origins"].get(origin_key)
    if not g:
        return [], "no curated ground access from %r" % origin_key
    modes = (g["airports"] or {}).get(iata)
    if not modes:
        return [], "no curated ground access from %s to %s" % (origin_key, iata)
    out = []
    for m in modes:
        m = dict(m)
        hours = m.pop("hours", "any")
        m["feasible"] = _within(hours, clock_minutes)
        if m["feasible"]:
            m["infeasible_reason"] = None
        out.append(m)
    return out, None


def arrival_ground_for(iata: str, clock_minutes: int,
                       enr: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    modes = enr["ground"]["arrivals"].get(iata)
    if not modes:
        return [], "no curated ground access at %s" % iata
    out = []
    for m in modes:
        m = dict(m)
        hours = m.pop("hours", "any")
        m["feasible"] = _within(hours, clock_minutes)
        if m["feasible"]:
            m["infeasible_reason"] = None
        out.append(m)
    return out, None


# -------------------------------------------------------------------- kiwi

def _tickets_from_id(itin_id: str, n_segments: int) -> List[List[int]]:
    """Kiwi encodes ticket boundaries in its id: chunks sharing a prefix are one
    e-ticket, a new prefix is a new one. That is exactly the self-transfer
    signal the baggage and risk terms need, and it is otherwise invisible."""
    chunks = itin_id.split("|")
    if len(chunks) != n_segments:
        return [list(range(n_segments))]          # cannot tell - assume through
    groups, order = {}, []
    for i, c in enumerate(chunks):
        pre = c.rsplit("_", 1)[0]
        if pre not in groups:
            groups[pre] = []
            order.append(pre)
        groups[pre].append(i)
    return [groups[p] for p in order]


def from_kiwi(raw: Dict[str, Any], origin_key: str = "bushwick-brooklyn",
              enr: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A Kiwi search response -> a scenario dict the scorer can read."""
    enr = enr or load_enrichment()
    notes: List[str] = []
    options: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []

    for it in raw.get("itineraries", []):
        leg = it.get("outbound") or {}
        segs_in = leg.get("segments") or []
        if not segs_in:
            continue
        oid = re.sub(r"[^a-z0-9]+", "-", (it.get("id") or "x").lower())[:40]
        fail = None

        segments, seg_ids = [], []
        for i, s in enumerate(segs_in):
            dep, e1 = stamp(s["departureTime"], s["from"], enr)
            arr, e2 = stamp(s["arrivalTime"], s["to"], enr)
            if e1 or e2:
                fail = e1 or e2
                break
            ap_from = enr["airports"]["airports"].get(s["from"], {})
            ap_to = enr["airports"]["airports"].get(s["to"], {})
            num = re.sub(r"^[A-Z0-9]{2}", "", s.get("flightNumber", "") or "")
            sid = "s%d" % (i + 1)
            seg_ids.append(sid)
            codeshare = bool(CODESHARE.match(s.get("flightNumber", "") or ""))
            segments.append({
                "segment_id": sid,
                "marketing": {"carrier": s["carrier"], "number": int(num or 0)},
                # The feed does not name the operator. Repeating the marketing
                # carrier here would be an invention, so it is flagged instead.
                "operating": {"carrier": s["carrier"], "number": int(num or 0)},
                "origin": {"iata": s["from"], "iata_area": ap_from.get("iata_area", 1),
                           "schengen": ap_from.get("schengen")},
                "destination": {"iata": s["to"], "iata_area": ap_to.get("iata_area", 2),
                                "schengen": ap_to.get("schengen")},
                "departure_local": dep, "arrival_local": arr,
                "tz_hint": {"departure": ap_from.get("zone", ""), "arrival": ap_to.get("zone", "")},
                "equipment_code": "UNKNOWN",
                "cabin_marketed": (s.get("cabinClass") or "economy").lower(),
                "claims": {
                    "subfleet": {"coverage": "none", "policy": "route_median",
                                 "reason": "the feed carries no equipment code"
                                           + (", and %s is a codeshare so the operating carrier is unknown too"
                                              % s.get("flightNumber") if codeshare else "")},
                    "connectivity_oceanic": {"coverage": "none", "policy": "route_median",
                                             "reason": "no equipment code, so no connectivity fit can be looked up"},
                },
                "reliability": {"coverage": "none", "policy": "route_median",
                                "reason": "no on-time record joined for %s" % s.get("flightNumber", "?")},
            })
        if fail:
            dropped.append({"id": it.get("id", "?"), "why": fail})
            continue

        groups = _tickets_from_id(it.get("id", ""), len(segs_in))
        bags = it.get("baggage") or {}
        total_cents = int(round(float(it.get("price", 0)) * 100))
        per_ticket = total_cents // max(len(groups), 1)
        tickets = []
        for gi, idxs in enumerate(groups):
            tickets.append({
                "ticket_id": "t%d" % (gi + 1),
                "issuing_carrier": segs_in[idxs[0]]["carrier"],
                "fare_brand_name": (leg.get("cabinClass") or "Economy"),
                "price": {"currency": raw.get("currency", "USD"),
                          "base_cents": per_ticket + (total_cents - per_ticket * len(groups) if gi == 0 else 0),
                          "fx_rate_to_usd": 1.0, "taxes": [], "carrier_imposed": [], "agency_fees": []},
                "entitlements": {
                    "checked_included": int(bags.get("checkedBag", 0)),
                    "cabin_bag_included": bool(bags.get("cabinBag", 0)),
                    "personal_item_included": bool(bags.get("personalItem", 1)),
                    "seat_selection": "paid", "changes": "fee", "refundable": False,
                    "earns_redeemable_miles": False, "boarding_group": None},
                # No fee schedule in the feed. An empty tier list is honest; a
                # guessed one would quietly re-price every option in the corpus.
                "checked_bag_fee_tiers": [],
                "segment_ids": [seg_ids[i] for i in idxs],
            })

        layovers = []
        for i in range(len(segments) - 1):
            a, b = segs_in[i], segs_in[i + 1]
            ap = enr["airports"]["airports"].get(a["to"], {})
            through = any(i in g and (i + 1) in g for g in groups)
            mct = (ap.get("mct_minutes") or {}).get("default")
            layovers.append({
                "layover_id": "lay%d" % (i + 1),
                "airport": a["to"],
                "arrive_segment_id": seg_ids[i], "depart_segment_id": seg_ids[i + 1],
                "immigration_required": bool(ap.get("schengen")) and not bool(
                    enr["airports"]["airports"].get(a["from"], {}).get("schengen")),
                "security_reclear_required": not through,
                "ees_first_registration": False,
                "inter_terminal": ap.get("inter_terminal") or {"mode": "walk", "minutes": 20},
                "published_mct_minutes": mct,
                "mct_source": "curated airport table" if mct else "not curated",
                "bags_checked_through": through,
                "forced_landside": not through,
                "leave_airport_viable": bool(ap.get("leave_airport_viable")),
                "services_open": ap.get("services") or [],
                "recovery": {"protected": through,
                             "next_departure_minutes": 180 if through else None,
                             "overnight_implied": not through,
                             "walkup_fare_cents": 0 if through else per_ticket},
                "note": "Built from live inventory; layover facts joined from the curated airport table.",
            })

        dep_clock = _hhmm(segments[0]["departure_local"][11:16])
        arr_clock = _hhmm(segments[-1]["arrival_local"][11:16])
        out_modes, gerr = ground_for(origin_key, segments[0]["origin"]["iata"], dep_clock, enr)
        in_modes, aerr = arrival_ground_for(segments[-1]["destination"]["iata"], arr_clock, enr)
        if gerr:
            dropped.append({"id": it.get("id", "?"), "why": gerr})
            continue
        if aerr and aerr not in notes:
            notes.append(aerr)

        rating = (enr["carriers"]["ratings"] or {}).get(segs_in[0]["carrier"])
        opt = {
            "option_id": oid,
            "display_name": "%s %s" % (segs_in[0].get("carrierName", ""), leg.get("route", [""])[-1]),
            "tickets": tickets,
            "segments": segments,
            "layovers": layovers,
            "ground": {"outbound": out_modes, "arrival": in_modes},
            "airport_process_minutes": (enr["airports"]["airports"]
                                        .get(segments[0]["origin"]["iata"], {})
                                        .get("process_minutes") or {"p50": 60}),
            "booking": [{"who": "Kiwi.com", "price_cents": total_cents, "direct": False,
                         "note": "Self-transfer protection is Kiwi's own, not the airlines'"
                                 if len(groups) > 1 else "Resold inventory"}],
        }
        if rating:
            opt["carrier_rating"] = {"rating": rating["rating"], "note": rating["note"],
                                     "source": enr["carriers"]["_source"],
                                     "as_of": enr["carriers"]["_as_of"]}
        options.append(opt)

    if not options:
        return {"error": "nothing could be normalised", "dropped": dropped}

    first = options[0]
    o_iata = first["segments"][0]["origin"]["iata"]
    d_iata = first["segments"][-1]["destination"]["iata"]
    dest_city = d_iata
    for it in raw.get("itineraries", []):
        for sg in ((it.get("outbound") or {}).get("segments") or []):
            if sg.get("to") == d_iata and sg.get("toCity"):
                dest_city = sg["toCity"]
                break
    par = ((enr["ground"].get("routes") or {}).get("%s-%s" % (o_iata, d_iata)) or {}).get("par_cents")
    if not par:
        par = 105000
        notes.append("no curated par for %s-%s; grades on this route are indicative only"
                     % (o_iata, d_iata))

    org = enr["ground"]["origins"].get(origin_key, {})
    scenario = {
        "schema_version": "0.1.0",
        "fixture_id": "live-%s-%s" % (o_iata.lower(), d_iata.lower()),
        "title": "Live inventory: %s to %s" % (o_iata, d_iata),
        "pins_down": "Nothing. This is a live search, not a fixture - it pins no behaviour and must never be committed to fixtures/.",
        "as_of": first["segments"][0]["departure_local"][:10],
        "notes": notes,
        "query": {
            "origin": {"label": org.get("label", origin_key),
                       "lat": org.get("lat", 0.0), "lon": org.get("lon", 0.0),
                       "geocode_precision": "neighbourhood"},
            "destination": {"label": "%s (%s)" % (dest_city, d_iata),
                            "lat": 0.0, "lon": 0.0, "geocode_precision": "city"},
            "depart_date": first["segments"][0]["departure_local"][:10],
            "return_date": None,
            "route_par_cents": par,
            "party": [{"passenger_id": "p1", "type": "adult",
                       "bags": [{"kind": "checked", "weight_kg": 20}
                                for _ in range(int((raw.get("itineraries") or [{}])[0]
                                                   .get("baggage", {}).get("checkedBag", 0)))]
                               + [{"kind": "cabin"}, {"kind": "personal_item"}]}],
            "profiles": {
                "reference": {"label": "neutral reference", "hourly_value_cents": 3500, "comfort_weight": 1.0, "risk_weight": 1.0},
                "cheapest": {"label": "Cheapest", "hourly_value_cents": 1200, "comfort_weight": 0.45, "risk_weight": 1.0},
                "fastest": {"label": "Fastest", "hourly_value_cents": 9500, "comfort_weight": 0.85, "risk_weight": 1.0},
                "comfort": {"label": "Most comfortable", "hourly_value_cents": 5500, "comfort_weight": 2.0, "risk_weight": 1.0},
            },
        },
        "options": options,
        "expect": {"reconciles": True},
        "_dropped": dropped,
    }
    return scenario


# ----------------------------------------------------------------- amadeus

def _amadeus_cents(v) -> int:
    """Amadeus prices are decimal STRINGS ("480.00"). Parsing to float and
    multiplying is how you get 47999 out of 480.00 on some values, so the
    decimal is split textually and money stays integral from the first read."""
    s = str(v or "0").strip()
    neg = s.startswith("-")
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    out = int(whole or 0) * 100 + int(frac or 0)
    return -out if neg else out


def _fare_details(offer: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """segmentId -> the first traveller's fare detail for that segment."""
    tps = offer.get("travelerPricings") or []
    if not tps:
        return {}
    return {str(d.get("segmentId")): d
            for d in (tps[0].get("fareDetailsBySegment") or [])}


def _included_checked(detail: Dict[str, Any]) -> int:
    """`includedCheckedBags` is a quantity for most carriers and a WEIGHT for
    some ({"weight": 23, "weightUnit": "KG"}), with no quantity at all. A weight
    allowance with no piece count is one piece for our purposes; reading the
    absent quantity as zero would invent a bag fee on a fare that includes one."""
    inc = (detail or {}).get("includedCheckedBags") or {}
    if "quantity" in inc:
        return int(inc["quantity"] or 0)
    if inc.get("weight"):
        return 1
    return 0


def _bag_tiers(enr: Dict[str, Any], carrier: str, brand: str
               ) -> Tuple[List[Dict[str, Any]], bool]:
    """(tiers, used_the_pessimistic_default)."""
    fares = enr.get("fares") or {}
    row = (fares.get("brands") or {}).get("%s:%s" % (carrier, (brand or "").upper()))
    if row:
        return [dict(t) for t in row["tiers"]], False
    dft = fares.get("_default") or {"tiers": [], "note": ""}
    tiers = [dict(t) for t in dft["tiers"]]
    if tiers and dft.get("note"):
        tiers[0]["note"] = dft["note"]
    return tiers, True


def _fleet_claims(enr: Dict[str, Any], operating: str, equipment: str,
                  codeshare_note: str) -> Dict[str, Any]:
    """The equipment code buys a hedged claim, never a fact.

    A feed that says "789" has named a type, not a cabin: a carrier can fly two
    or three configurations of the same type with two or three connectivity
    systems, and the frame is swapped after booking regardless. So a curated row
    becomes a claim with an observed frequency - which is what the scorer's
    cabin-uncertainty term is built to price - and a type with NO row abstains
    rather than being talked up from the type alone."""
    row = ((enr.get("fleets") or {}).get("fleets") or {}).get(
        "%s:%s" % (operating, equipment))
    if not row:
        return {
            "subfleet": {"coverage": "none", "policy": "route_median",
                         "reason": "the feed names a %s but no cabin configuration is "
                                   "curated for %s on that type%s"
                                   % (equipment, operating, codeshare_note)},
            "connectivity_oceanic": {"coverage": "none", "policy": "route_median",
                                     "reason": "no connectivity fit curated for %s on the %s"
                                               % (operating, equipment)},
        }
    return {"subfleet": dict(row["subfleet"]),
            "connectivity_oceanic": dict(row["connectivity_oceanic"])}


def from_amadeus(raw: Dict[str, Any], origin_key: str = "bushwick-brooklyn",
                 enr: Optional[Dict[str, Any]] = None,
                 checked_bags: int = 1) -> Dict[str, Any]:
    """An Amadeus Flight Offers Search v2 response -> a scenario dict.

    `checked_bags` is what the TRAVELLER is carrying, and it deliberately comes
    from the request rather than from the fare. The traveller's bag load does not
    change because they clicked a cheaper fare; reading it off the fare is how
    every basic-economy ticket silently scores as if the passenger travelled with
    hand baggage only, which is the exact comparison this product exists to make."""
    enr = enr or load_enrichment()
    notes: List[str] = []
    options: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    dicts = raw.get("dictionaries") or {}
    ac_names = dicts.get("aircraft") or {}
    carrier_names = dicts.get("carriers") or {}
    default_used = 0
    unreviewed = 0

    for offer in raw.get("data", []):
        itins = offer.get("itineraries") or []
        if not itins:
            continue
        segs_in = itins[0].get("segments") or []
        if not segs_in:
            continue
        details = _fare_details(offer)
        fail = None
        segments, seg_ids = [], []

        for i, s in enumerate(segs_in):
            dep_ap = (s.get("departure") or {}).get("iataCode")
            arr_ap = (s.get("arrival") or {}).get("iataCode")
            dep, e1 = stamp((s.get("departure") or {}).get("at", ""), dep_ap, enr)
            arr, e2 = stamp((s.get("arrival") or {}).get("at", ""), arr_ap, enr)
            if e1 or e2:
                fail = e1 or e2
                break
            ap_from = enr["airports"]["airports"].get(dep_ap, {})
            ap_to = enr["airports"]["airports"].get(arr_ap, {})
            sid = "s%d" % (i + 1)
            seg_ids.append(sid)
            mkt = s.get("carrierCode", "")
            # An ABSENT `operating` block means the marketing carrier flies it.
            # That is a statement, not a hole - which is why this adapter can
            # name an operator and from_kiwi cannot.
            op = ((s.get("operating") or {}).get("carrierCode") or mkt)
            codeshare = op != mkt
            equip = ((s.get("aircraft") or {}).get("code") or "").strip()
            det = details.get(str(s.get("id")), {})
            note = (", and %s%s is sold by %s but flown by %s"
                    % (mkt, s.get("number", ""), mkt, op)) if codeshare else ""
            claims = _fleet_claims(enr, op, equip, note) if equip else {
                "subfleet": {"coverage": "none", "policy": "route_median",
                             "reason": "this offer carries no equipment code" + note},
                "connectivity_oceanic": {"coverage": "none", "policy": "route_median",
                                         "reason": "no equipment code, so no connectivity fit "
                                                   "can be looked up"},
            }
            for c in claims.values():
                if c.get("needs_primary_source"):
                    unreviewed += 1
            segments.append({
                "segment_id": sid,
                "marketing": {"carrier": mkt, "number": int(s.get("number") or 0)},
                "operating": {"carrier": op, "number": int(s.get("number") or 0)},
                "origin": {"iata": dep_ap, "iata_area": ap_from.get("iata_area", 1),
                           "schengen": ap_from.get("schengen")},
                "destination": {"iata": arr_ap, "iata_area": ap_to.get("iata_area", 2),
                                "schengen": ap_to.get("schengen")},
                "departure_local": dep, "arrival_local": arr,
                "tz_hint": {"departure": ap_from.get("zone", ""),
                            "arrival": ap_to.get("zone", "")},
                "equipment_code": equip or "UNKNOWN",
                "equipment_name": ac_names.get(equip, ""),
                "cabin_marketed": (det.get("cabin") or "economy").lower(),
                "claims": claims,
                # Amadeus shopping carries no on-time record. Reliability is a
                # separate feed (DOT/BTS by flight number) and is not pretended at.
                "reliability": {"coverage": "none", "policy": "route_median",
                                "reason": "no on-time record joined for %s%s"
                                          % (mkt, s.get("number", ""))},
            })
        if fail:
            dropped.append({"id": "offer %s" % offer.get("id", "?"), "why": fail})
            continue

        # GDS content is ONE ticket across every segment. There is no
        # self-transfer here and no virtual interlining - a real gap against
        # Kiwi, not a simplification.
        issuing = (offer.get("validatingAirlineCodes") or [segs_in[0].get("carrierCode", "")])[0]
        first_det = details.get(str(segs_in[0].get("id")), {})
        brand = (first_det.get("brandedFare") or first_det.get("brandedFareLabel") or "")
        brand_label = (first_det.get("brandedFareLabel") or brand or "Economy")
        # One allowance covers the ticket; where segments disagree the smaller
        # one is what the traveller can actually rely on.
        included = min([_included_checked(details.get(str(s.get("id")), {}))
                        for s in segs_in] or [0])
        tiers, used_default = _bag_tiers(enr, issuing, brand)
        if used_default:
            default_used += 1

        price = offer.get("price") or {}
        total_cents = _amadeus_cents(price.get("grandTotal") or price.get("total"))
        base_cents = _amadeus_cents(price.get("base"))
        if base_cents > total_cents:
            base_cents = total_cents
        amens = {a.get("description", ""): a for a in (first_det.get("amenities") or [])}
        seat_free = amens.get("PRE RESERVED SEAT ASSIGNMENT")
        ticket = {
            "ticket_id": "t1",
            "issuing_carrier": issuing,
            "fare_brand_name": brand_label,
            "price": {
                "currency": price.get("currency", "USD"),
                "base_cents": base_cents,
                "fx_rate_to_usd": 1.0,
                # Flight Offers Search gives base and total, not a tax breakdown.
                # One combined line labelled as unitemised is honest; inventing
                # plausible tax codes to fill the shape would not be.
                "taxes": ([{"code": "TOT", "amount_cents": total_cents - base_cents,
                            "label": "Taxes and carrier-imposed charges (the feed does "
                                     "not itemise them)",
                            "refundable_on_cancel": False}]
                          if total_cents > base_cents else []),
                "carrier_imposed": [], "agency_fees": []},
            "entitlements": {
                "checked_included": included,
                "cabin_bag_included": bool((first_det.get("includedCabinBags") or {}).get("quantity", 1)),
                "personal_item_included": True,
                "seat_selection": ("free" if seat_free and not seat_free.get("isChargeable")
                                   else "paid"),
                "changes": "fee", "refundable": False,
                "earns_redeemable_miles": brand.upper() not in ("BASIC", "LIGHT", "SAVER"),
                "boarding_group": None},
            "checked_bag_fee_tiers": tiers,
            "segment_ids": list(seg_ids),
        }
        if used_default:
            notes_for_ticket = "no curated bag fees for %s %s" % (issuing, brand_label)
            if notes_for_ticket not in notes:
                notes.append(notes_for_ticket + " - priced at the pessimistic default")

        layovers = []
        for i in range(len(segments) - 1):
            a_to = segments[i]["destination"]["iata"]
            ap = enr["airports"]["airports"].get(a_to, {})
            mct = (ap.get("mct_minutes") or {}).get("default")
            # One ticket: bags are through-checked, the carrier owns the
            # reconnection, and nobody is ejected landside.
            layovers.append({
                "layover_id": "lay%d" % (i + 1),
                "airport": a_to,
                "arrive_segment_id": seg_ids[i], "depart_segment_id": seg_ids[i + 1],
                "immigration_required": bool(ap.get("schengen")) and not bool(
                    segments[i]["origin"].get("schengen")),
                "security_reclear_required": False,
                "ees_first_registration": False,
                "inter_terminal": ap.get("inter_terminal") or {"mode": "walk", "minutes": 20},
                "published_mct_minutes": mct,
                "mct_source": "curated airport table" if mct else "not curated",
                "bags_checked_through": True,
                "forced_landside": False,
                "leave_airport_viable": bool(ap.get("leave_airport_viable")),
                "services_open": ap.get("services") or [],
                "recovery": {"protected": True, "next_departure_minutes": 180,
                             "overnight_implied": False, "walkup_fare_cents": 0},
                "note": "Single ticket: the carrier owes the reconnection and the bags "
                        "are checked through.",
            })

        dep_clock = _hhmm(segments[0]["departure_local"][11:16])
        arr_clock = _hhmm(segments[-1]["arrival_local"][11:16])
        out_modes, gerr = ground_for(origin_key, segments[0]["origin"]["iata"], dep_clock, enr)
        in_modes, aerr = arrival_ground_for(segments[-1]["destination"]["iata"], arr_clock, enr)
        if gerr:
            dropped.append({"id": "offer %s" % offer.get("id", "?"), "why": gerr})
            continue
        if aerr and aerr not in notes:
            notes.append(aerr)

        oid = re.sub(r"[^a-z0-9]+", "-",
                     ("%s%s-%s-%s" % (segs_in[0].get("carrierCode", ""),
                                      segs_in[0].get("number", ""),
                                      brand or "fare", offer.get("id", "x"))).lower())[:40]
        opt = {
            "option_id": oid,
            "display_name": "%s %s%s · %s" % (
                carrier_names.get(segs_in[0].get("carrierCode", ""),
                                  segs_in[0].get("carrierCode", "")).title(),
                segs_in[0].get("carrierCode", ""), segs_in[0].get("number", ""),
                brand_label),
            "tickets": [ticket],
            "segments": segments,
            "layovers": layovers,
            "ground": {"outbound": out_modes, "arrival": in_modes},
            "airport_process_minutes": (enr["airports"]["airports"]
                                        .get(segments[0]["origin"]["iata"], {})
                                        .get("process_minutes") or {"p50": 60}),
            "booking": [{"who": carrier_names.get(issuing, issuing).title(),
                         "price_cents": total_cents, "direct": True,
                         "note": "Airline inventory - bookable direct with %s" % issuing}],
        }
        rating = (enr["carriers"]["ratings"] or {}).get(segments[0]["operating"]["carrier"])
        if rating:
            opt["carrier_rating"] = {"rating": rating["rating"], "note": rating["note"],
                                     "source": enr["carriers"]["_source"],
                                     "as_of": enr["carriers"]["_as_of"]}
        options.append(opt)

    if not options:
        return {"error": "nothing could be normalised", "dropped": dropped}

    first = options[0]
    o_iata = first["segments"][0]["origin"]["iata"]
    d_iata = first["segments"][-1]["destination"]["iata"]
    par = ((enr["ground"].get("routes") or {}).get("%s-%s" % (o_iata, d_iata)) or {}).get("par_cents")
    if not par:
        par = 105000
        notes.append("no curated par for %s-%s; grades on this route are indicative only"
                     % (o_iata, d_iata))
    if unreviewed:
        notes.append("%d aircraft claims come from the DRAFT fleet table, which has not "
                     "been human-reviewed" % unreviewed)

    org = enr["ground"]["origins"].get(origin_key, {})
    bags = [{"kind": "checked", "weight_kg": 20} for _ in range(max(0, int(checked_bags)))]
    scenario = {
        "schema_version": "0.1.0",
        "fixture_id": "live-amadeus-%s-%s" % (o_iata.lower(), d_iata.lower()),
        "title": "Live inventory (Amadeus): %s to %s" % (o_iata, d_iata),
        "pins_down": "Nothing. This is a live search, not a fixture - it pins no behaviour "
                     "and must never be committed to fixtures/.",
        "as_of": first["segments"][0]["departure_local"][:10],
        "notes": notes,
        "query": {
            "origin": {"label": org.get("label", origin_key),
                       "lat": org.get("lat", 0.0), "lon": org.get("lon", 0.0),
                       "geocode_precision": "neighbourhood"},
            "destination": {"label": d_iata, "lat": 0.0, "lon": 0.0,
                            "geocode_precision": "city"},
            "depart_date": first["segments"][0]["departure_local"][:10],
            "return_date": None,
            "route_par_cents": par,
            "party": [{"passenger_id": "p1", "type": "adult",
                       "bags": bags + [{"kind": "cabin"}, {"kind": "personal_item"}]}],
            "profiles": {
                "reference": {"label": "neutral reference", "hourly_value_cents": 3500,
                              "comfort_weight": 1.0, "risk_weight": 1.0},
                "cheapest": {"label": "Cheapest", "hourly_value_cents": 1200,
                             "comfort_weight": 0.45, "risk_weight": 1.0},
                "fastest": {"label": "Fastest", "hourly_value_cents": 9500,
                            "comfort_weight": 0.85, "risk_weight": 1.0},
                "comfort": {"label": "Most comfortable", "hourly_value_cents": 5500,
                            "comfort_weight": 2.0, "risk_weight": 1.0},
            },
        },
        "options": options,
        "expect": {"reconciles": True},
        "_dropped": dropped,
        "_feed": {"provider": "amadeus", "default_bag_fees_used": default_used,
                  "unreviewed_claims": unreviewed},
    }
    return scenario


# ----------------------------------------------------------------- coverage

def coverage(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """What was known and what was guessed.

    A grade computed over mostly-abstained enrichment is not worth the same as
    one computed over a curated route, and the interface should be able to say
    which it is looking at rather than presenting both with equal confidence."""
    feed = scenario.get("_feed") or {}
    tot = {"segments": 0, "abstained_claims": 0, "abstained_reliability": 0,
           "codeshare_segments": 0, "options": 0, "rated_carriers": 0,
           "options_with_ground": 0, "options_with_bag_schedule": 0,
           "equipment_known": 0, "operator_known": 0, "unreviewed_claims": 0,
           "default_bag_fees": int(feed.get("default_bag_fees_used") or 0)}
    for o in scenario.get("options", []):
        tot["options"] += 1
        if o.get("carrier_rating"):
            tot["rated_carriers"] += 1
        if o.get("ground", {}).get("outbound"):
            tot["options_with_ground"] += 1
        if any(t.get("checked_bag_fee_tiers") for t in o["tickets"]):
            tot["options_with_bag_schedule"] += 1
        for s in o["segments"]:
            tot["segments"] += 1
            if s.get("equipment_code") not in (None, "", "UNKNOWN"):
                tot["equipment_known"] += 1
            for c in (s.get("claims") or {}).values():
                if isinstance(c, dict) and c.get("coverage") == "none":
                    tot["abstained_claims"] += 1
                elif isinstance(c, dict) and c.get("needs_primary_source"):
                    tot["unreviewed_claims"] += 1
            if (s.get("reliability") or {}).get("coverage") == "none":
                tot["abstained_reliability"] += 1
            if "codeshare" in str((s.get("claims") or {}).get("subfleet", {}).get("reason", "")):
                tot["codeshare_segments"] += 1
    # An operator is KNOWN when the feed distinguished it from the marketing
    # carrier at all. Kiwi copies the marketing carrier across and flags it in
    # the claim reason; Amadeus states it, including by omission.
    tot["operator_known"] = (tot["segments"] if feed.get("provider") == "amadeus"
                             else 0)

    gaps = []
    # A claim sourced from an unreviewed table is NOT the same as a known fact,
    # and it is a more dangerous gap than an abstain: an abstain announces
    # itself in the ledger, a draft row prints a confident dollar figure.
    if tot["unreviewed_claims"]:
        gaps.append("%d aircraft and connectivity claims come from the DRAFT fleet "
                    "table, which has not been human-reviewed"
                    % tot["unreviewed_claims"])
    if tot["default_bag_fees"]:
        gaps.append("%d of %d options have no curated bag fees for their fare brand "
                    "and were priced at the pessimistic default"
                    % (tot["default_bag_fees"], tot["options"]))
    if tot["abstained_claims"]:
        no_code = sum(1 for o in scenario.get("options", []) for s in o["segments"]
                      if s.get("equipment_code") in (None, "", "UNKNOWN"))
        if no_code:
            gaps.append("no aircraft, cabin or connectivity claim can be made: the feed "
                        "carries no equipment code")
        else:
            # A different gap with a different fix: the feed did its job and the
            # curated table has not caught up. Saying "no equipment code" here
            # would send someone to change the wrong file.
            n = sum(1 for o in scenario.get("options", []) for s in o["segments"]
                    if ((s.get("claims") or {}).get("subfleet") or {}).get("coverage") == "none")
            gaps.append("%d segment%s fl%s a type with no curated cabin configuration, so "
                        "the aircraft claims there abstain"
                        % (n, "" if n == 1 else "s", "ies" if n == 1 else "y"))
    if tot["codeshare_segments"]:
        gaps.append("%d of %d segments are codeshares, so even the operating carrier is unknown"
                    % (tot["codeshare_segments"], tot["segments"]))
    if tot["abstained_reliability"] == tot["segments"] and tot["segments"]:
        gaps.append("no on-time record joined for any segment")
    if not tot["options_with_bag_schedule"]:
        gaps.append("no baggage fee schedule: the feed prices bags inline, so the "
                    "fare-versus-bags comparison cannot run")
    if tot["rated_carriers"] < tot["options"]:
        gaps.append("%d of %d options fly a carrier with no curated rating"
                    % (tot["options"] - tot["rated_carriers"], tot["options"]))
    enrichment_gaps = len(gaps)
    for d in scenario.get("_dropped", []):
        gaps.append("dropped an itinerary: %s" % d["why"])

    enriched = tot["options_with_ground"] and tot["rated_carriers"]
    if not enriched or enrichment_gaps >= 4:
        verdict = "grades are indicative only - most enrichment is missing"
    elif tot["unreviewed_claims"]:
        verdict = ("ground access, carrier quality and fare brands are curated; the "
                   "aircraft claims are DRAFT and not yet reviewed")
    elif tot["equipment_known"] == tot["segments"] and tot["segments"]:
        verdict = "every layer this product prices is curated for these options"
    else:
        verdict = "ground access and carrier quality are curated; aircraft claims are not"
    return {"counts": tot, "gaps": gaps, "verdict": verdict}


def from_feed(raw: Dict[str, Any], **kw) -> Dict[str, Any]:
    """Dispatch on the payload's own shape rather than on a caller-supplied
    name, so a profile pointed at the wrong provider fails as a parse error
    here instead of as a plausible-looking scenario built from the wrong keys."""
    if isinstance(raw.get("data"), list) and any(
            r.get("type") == "flight-offer" for r in raw["data"][:3] if isinstance(r, dict)):
        return from_amadeus(raw, **kw)
    if isinstance(raw.get("itineraries"), list):
        kw.pop("checked_bags", None)      # from_kiwi takes the load from the feed
        return from_kiwi(raw, **kw)
    return {"error": "unrecognised payload: no Amadeus `data[].type=flight-offer` and "
                     "no Kiwi `itineraries`", "dropped": []}


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "adapter_samples", "kiwi-jfk-lhr.json")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    sc = from_feed(raw)
    if sc.get("error"):
        print("error: %s" % sc["error"])
        raise SystemExit(1)
    cov = coverage(sc)
    print("%s -> %d options via %s"
          % (os.path.basename(path), len(sc.get("options", [])),
             (sc.get("_feed") or {}).get("provider", "kiwi")))
    print("\ncoverage: %s" % cov["verdict"])
    for g in cov["gaps"]:
        print("  - " + g)
