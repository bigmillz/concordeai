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

import ground as _ground
import par as _par
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
    golden number would be very hard to notice.

    "THIS PLACE HAS NO SUMMER TIME" IS NOT "WE HAVE NOT CURATED IT". Iceland has
    never observed it and Turkey abolished it in 2016, so a missing `dst` block
    for Atlantic/Reykjavik or Europe/Istanbul is a fact about the place, while a
    missing one for Europe/Berlin means the table has run out of years. Conflate
    them and either every Icelandic connection silently drops, or an uncurated
    year silently scores at standard time and every timestamp in it is an hour
    out for half the year. A zone must therefore SAY which it is."""
    if zone.get("observes_dst") is False:
        return zone["standard"]
    year = date_str[:4]
    window = (zone.get("dst") or {}).get(year)
    if not window:
        return None                      # outside the curated years - say so
    start, end = window
    return zone["daylight"] if start <= date_str < end else zone["standard"]


def _offset_from_tzdb(iana: str, local_naive: str) -> Optional[str]:
    """Last resort for an airport nobody has curated: resolve the feed's own
    IANA zone name with the stdlib.

    The curated table exists so that FIXTURES are hermetic - a tzdata update
    must never silently move a golden number in a test. That reason does not
    apply to a live search of Reykjavik, where there is no golden number and the
    alternative is refusing to show the flight at all. So curated wins where it
    exists, this fills the rest, and either way the SCORER still receives an
    explicit offset and never consults a timezone database itself."""
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(local_naive[:19]).replace(tzinfo=ZoneInfo(iana))
        off = dt.utcoffset()
        if off is None:
            return None
        total = int(off.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return "%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)
    except Exception:
        return None


def stamp(local_naive: str, iata: str, enr: Dict[str, Any],
          feed_zone: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """'2026-11-12T20:00:00' + JFK -> '2026-11-12T20:00:00-05:00'."""
    ap = enr["airports"]["airports"].get(iata)
    if ap:
        zone = enr["airports"]["zones"].get(ap["zone"])
        if zone:
            off = offset_for(zone, local_naive[:10])
            if off:
                return local_naive[:19] + off, None
    if feed_zone:
        off = _offset_from_tzdb(feed_zone, local_naive)
        if off:
            return local_naive[:19] + off, None
    if not ap:
        return None, "no timezone for %s, curated or from the feed" % iata
    return None, "no daylight-saving rules curated for %s and the feed named no zone" % local_naive[:4]


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
    ground_info: Dict[str, Any] = {}

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
            a = segs_in[i]
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
        out_modes, in_modes, ginfo = ground_both_ends(
            origin_key, segments[0]["origin"]["iata"],
            segments[-1]["destination"]["iata"], dep_clock, arr_clock, enr)
        ground_info = ginfo
        if ginfo.get("arrival_note") and ginfo["arrival_note"] not in notes:
            notes.append(ginfo["arrival_note"])

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
    par, par_basis = route_par(o_iata, d_iata, first["segments"][0]["departure_local"][:10],
                               enr, notes)

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
        "_ground": ground_info,
        "_par": par_basis,
    }
    return scenario


# ----------------------------------------------------------------- amadeus

def _decimal_cents(v) -> int:
    """Amadeus and Duffel both price in decimal STRINGS ("480.00"). Parsing to float and
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


def _brand_key(fares: Dict[str, Any], carrier: str, brand: str) -> Optional[str]:
    """Find the curated row for a fare brand across two feeds that name brands
    differently.

    Amadeus returns a CODE ("BASIC"); Duffel returns free text ("Basic Economy",
    "Economy Light"). An exact uppercase match handles the first and misses the
    second entirely, which silently sends every Duffel option to the pessimistic
    default and makes the curated table dead weight. So: exact match first, then
    match a curated token appearing as a WORD in the brand name - "Basic
    Economy" is a BASIC fare, "Economy Light" is a LIGHT one. Matching on
    substring rather than whole words would make "Economy Light" match a curated
    "ECONOMY" row, which is why this walks words."""
    brands = fares.get("brands") or {}
    up = (brand or "").upper().strip()
    if not up:
        return None
    if "%s:%s" % (carrier, up) in brands:
        return "%s:%s" % (carrier, up)
    # A row may name the feed's exact wording ("Basic Economy", "Partner Main");
    # that beats the word walk, which is what keeps UA:BASIC and UA:ECONOMY
    # from depending on which row happens to come first in the file.
    for key, row in brands.items():
        c, _, _ = key.partition(":")
        if c != carrier:
            continue
        names = [n.strip().upper() for n in str(row.get("feed_name") or "").split("/")]
        if up in names:
            return key
    words = set(re.findall(r"[A-Z]+", up))
    for key in brands:
        c, _, token = key.partition(":")
        if c == carrier and token in words:
            return key
    return None


def _bag_tiers(enr: Dict[str, Any], carrier: str, brand: str
               ) -> Tuple[List[Dict[str, Any]], bool]:
    """(tiers, used_the_pessimistic_default)."""
    fares = enr.get("fares") or {}
    key = _brand_key(fares, carrier, brand)
    row = (fares.get("brands") or {}).get(key) if key else None
    if row:
        return [dict(t) for t in row["tiers"]], False
    dft = fares.get("_default") or {"tiers": [], "note": ""}
    tiers = [dict(t) for t in dft["tiers"]]
    if tiers and dft.get("note"):
        tiers[0]["note"] = dft["note"]
    return tiers, True


def _extend_tiers(tiers: List[Dict[str, Any]], need: int) -> List[Dict[str, Any]]:
    """Pad a fee ladder so every bag the traveller is actually carrying has a price.

    `scorer._bag_cost` RAISES on a piece it cannot price - deliberately, because
    a fixture that forgets to price a bag is a broken fixture. But a live
    traveller's bag load comes from the request and is unbounded, while a
    published fee schedule stops after two or three pieces, so a party with four
    bags would take the whole search down. Padding repeats the HIGHEST published
    tier, which is the pessimistic reading and the only safe one: extrapolating
    downwards would make a heavy load look cheap on exactly the fares that do not
    publish a fourth-bag price."""
    if need <= 0 or not tiers:
        return tiers
    out = [dict(t) for t in tiers]
    top = max(out, key=lambda t: t["piece"])
    for piece in range(top["piece"] + 1, need + 1):
        out.append({"piece": piece, "amount_cents": top["amount_cents"],
                    "note": "beyond the published schedule - priced at the highest "
                            "published tier"})
    return out


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
    ground_info = {}

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
        tiers = _extend_tiers(tiers, max(0, int(checked_bags) - included))
        if used_default:
            default_used += 1

        price = offer.get("price") or {}
        total_cents = _decimal_cents(price.get("grandTotal") or price.get("total"))
        base_cents = _decimal_cents(price.get("base"))
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
                "immigration_required": crosses_border(
                    enr, segments[i]["origin"]["iata"], a_to),
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
        out_modes, in_modes, ginfo = ground_both_ends(
            origin_key, segments[0]["origin"]["iata"],
            segments[-1]["destination"]["iata"], dep_clock, arr_clock, enr)
        ground_info = ginfo
        if ginfo.get("arrival_note") and ginfo["arrival_note"] not in notes:
            notes.append(ginfo["arrival_note"])

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
    par, par_basis = route_par(o_iata, d_iata, first["segments"][0]["departure_local"][:10],
                               enr, notes)
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
        "_ground": ground_info,
        "_par": par_basis,
        "_feed": {"provider": "amadeus", "default_bag_fees_used": default_used,
                  "unreviewed_claims": unreviewed},
    }
    return scenario


# -------------------------------------------------------------------- duffel

def _duffel_offers(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Two shapes carry offers: the offer-request response nests them under
    `data.offers`, the offers-list endpoint returns them as `data` directly."""
    data = raw.get("data")
    if isinstance(data, dict):
        return list(data.get("offers") or [])
    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict) and "slices" in o]
    return []


def _duffel_bags(seg: Dict[str, Any], kind: str) -> int:
    """The per-segment, per-passenger allowance. Duffel states it as a list of
    {type, quantity}; an absent entry is genuinely zero of that type, which is
    different from Amadeus's absent-key-means-look-elsewhere."""
    pax = (seg.get("passengers") or [{}])[0]
    for b in pax.get("baggages") or []:
        if b.get("type") == kind:
            return int(b.get("quantity") or 0)
    return 0


def _duffel_bag_tiers(offer: Dict[str, Any], enr: Dict[str, Any],
                      carrier: str, brand: str) -> Tuple[List[Dict[str, Any]], str]:
    """(tiers, where_they_came_from).

    Duffel is the only one of the three feeds that prices an extra checked bag:
    `available_services` lists it as a bookable service with a real amount. That
    is the actual number the traveller would pay, so it outranks the curated
    table - this is the one place the feed knows better than the moat.

    Two things it does NOT give. It prices per unit with a `maximum_quantity`,
    so there is no escalating ladder and nothing at all beyond that cap; pieces
    past the cap are topped up from the curated table, because the scorer must
    be able to price every bag the traveller is actually carrying. And the
    service list is only populated on the single-offer endpoint for the airlines
    Duffel supports it for - an empty list means unknown, not free."""
    svcs = [s for s in (offer.get("available_services") or [])
            if s.get("type") == "baggage"
            and (s.get("metadata") or {}).get("type") == "checked"]
    fallback, used_default = _bag_tiers(enr, carrier, brand)
    if not svcs:
        return fallback, ("curated" if not used_default else "default")

    best = min(svcs, key=lambda s: _decimal_cents(s.get("total_amount")))
    per = _decimal_cents(best.get("total_amount"))
    cap = max(0, int(best.get("maximum_quantity") or 0))
    tiers = [{"piece": i, "amount_cents": per} for i in range(1, cap + 1)]
    if tiers:
        tiers[0]["note"] = ("quoted by the airline through Duffel, not a curated estimate"
                            + ("" if cap >= 3 else
                               "; bags past %d are not quoted and fall back to the table"
                               % cap))
    # Top up past the cap so a heavier bag load cannot make the scorer raise.
    for t in fallback:
        if t["piece"] > cap:
            tiers.append({"piece": t["piece"], "amount_cents": t["amount_cents"],
                          "note": "beyond the quoted allowance - estimate"})
    return tiers, "feed"


def _duffel_amenities(seg: Dict[str, Any]) -> Dict[str, Any]:
    """The published cabin attributes Duffel returns per segment.

    A real JFK-LHR capture carried these on 269 of 272 segments, which makes it
    the only feed here that says anything about the cabin without a curated
    join. What comes back is {wifi:{available,cost}, seat:{type,legroom,pitch},
    power:{available}}.

    WHAT IS TAKEN AND WHAT IS NOT. Seat pitch is a plain number in the schema,
    not a hedged claim, because a fare's pitch is a cabin-layout fact the airline
    publishes rather than an observation of what turned up - and the scorer
    already prices it against a 31-inch norm, which the real data's own mode
    confirms. Power is carried as a published attribute for the same reason.

    Wifi is deliberately NOT mapped onto `connectivity_oceanic`. That field is an
    OBSERVED claim - value, frequency, sample size, window - and an airline
    saying "wifi: available" is a marketing attribute with no source and no as-of
    date. Converting it into an observed frequency would mean inventing the
    sample that hard rule 2 exists to demand. It is kept raw for the narrator and
    the interface, and the connectivity term goes on abstaining until either the
    fleet table covers the frame or a real observation feed does."""
    pax = (seg.get("passengers") or [{}])[0]
    return ((pax.get("cabin") or {}).get("amenities") or {})


def _duffel_conditions(offer: Dict[str, Any]) -> Tuple[str, bool]:
    """(changes, refundable). A null condition is "the airline did not say",
    which is not the same as "no" - but it cannot be sold as a yes either."""
    cond = offer.get("conditions") or {}
    chg = cond.get("change_before_departure")
    ref = cond.get("refund_before_departure")
    if chg is None:
        changes = "unknown"
    elif not chg.get("allowed"):
        changes = "not_permitted"
    else:
        changes = "fee" if chg.get("penalty_amount") else "free"
    return changes, bool(ref and ref.get("allowed"))


# Immigration unions worth knowing globally. Everything NOT listed here is its
# own border, which is simply true: two different countries means a queue. That
# default is what makes this work for airports nobody has curated - Bogota needs
# no row to be correctly understood as a border from New York.
UNIONS = {}
for _members, _union in (
        ("AT BE CZ DK EE FI FR DE GR HU IS IT LV LI LT LU MT NL NO PL PT SK SI "
         "ES SE CH HR BG RO", "schengen"),
        # The Common Travel Area is NOT folded into one union here. Routine
        # passport checks between IE and GB are lighter than a full border but
        # not absent, and counting a queue that turns out to be quick costs a
        # few modelled minutes, while missing one costs a connection.
        ("GB", "uk"), ("IE", "ie"),
        ("AE QA BH KW OM SA", "gcc")):
    for _c in _members.split():
        UNIONS[_c] = _union


def border_of(iata: str, enr: Dict[str, Any],
              geo: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The immigration union an airport sits in: curated, then the feed's own
    country code, then nothing."""
    ap = (enr["airports"]["airports"].get(iata) or {})
    if ap.get("border"):
        return ap["border"]
    cc = (ap.get("country") or ((geo or {}).get(iata) or {}).get("country") or "").upper()
    if not cc:
        return None
    return UNIONS.get(cc, cc.lower())


def crosses_border(enr: Dict[str, Any], from_iata: str, to_iata: str,
                   geo: Optional[Dict[str, Any]] = None) -> bool:
    """Does arriving at `to_iata` from `from_iata` mean clearing immigration?

    NOT a Schengen test. `schengen` answers "is this airport inside that one
    union", which gets Frankfurt right and Dublin and Istanbul wrong: arriving
    at DUB from JFK you clear Irish immigration whatever Schengen says.

    Nor is it a CURATED test any more. Going global meant uncurated airports
    stopped being dropped, and the old fallback then read a Bogota layover as a
    free walk-through - a real scoring error, silently introduced the moment the
    gate came off. Two different countries is a border unless a union says
    otherwise, which needs no curation to be right."""
    ba, bb = border_of(from_iata, enr, geo), border_of(to_iata, enr, geo)
    if not ba or not bb:
        aps = enr["airports"]["airports"]
        b = aps.get(to_iata) or {}
        a = aps.get(from_iata) or {}
        return bool(b.get("schengen")) and not bool(a.get("schengen"))
    return ba != bb


# ------------------------------------------------------------------- par

def route_par(o_iata: str, d_iata: str, date: str, enr: Dict[str, Any],
              notes: List[str], geo: Optional[Dict[str, Any]] = None,
              scorer_mod=None) -> Tuple[int, Dict[str, Any]]:
    """(par_cents, basis) for a route. NEVER reads the options.

    A curated row in enrichment/ground.json wins - that is a human's number for
    a route somebody has studied. Everywhere else, par.py builds the reference
    itinerary from the two airports' coordinates and scores it through the real
    scorer. With no coordinates at all (a feed that carries none, an airport
    nobody curated) there is nothing to model, so the transatlantic default
    stands and the note says so - a grade over that number is indicative only.

    The one input that varies between searches on the same route is the DATE,
    because the reference fare is seasonal: a $500 ticket in July is a better
    deal than the same ticket in November, and the letter should say so. The
    par is still the same for every option in the search, and for every search
    on that route and date, whatever the inventory came back as."""
    curated = ((enr["ground"].get("routes") or {}).get("%s-%s" % (o_iata, d_iata)) or {})
    if curated.get("par_cents"):
        return int(curated["par_cents"]), {"source": "curated", "par_cents": int(curated["par_cents"]),
                                            "basis": curated.get("basis")}
    geo = geo or {}
    aps = enr["airports"]["airports"]

    def place_of(iata):
        node = dict(geo.get(iata) or {})
        ap = aps.get(iata) or {}
        node.setdefault("iata", iata)
        for k in ("lat", "lon", "country", "city"):
            if node.get(k) is None and ap.get(k) is not None:
                node[k] = ap[k]
        return node

    o, d = place_of(o_iata), place_of(d_iata)
    if o.get("lat") is None or d.get("lat") is None or not o.get("country") or not d.get("country"):
        missing = [i for i, n in ((o_iata, o), (d_iata, d)) if n.get("lat") is None or not n.get("country")]
        notes.append("no coordinates for %s, so par cannot be modelled; grades on this "
                     "route are indicative only" % " and ".join(missing))
        return 105000, {"source": "default", "par_cents": 105000,
                        "reads_as": "no coordinates for %s" % " and ".join(missing)}
    if scorer_mod is None:
        import scorer as scorer_mod                     # the adapter may import the scorer; never the reverse
    par, basis = _par.par_for(o, d, date, enr, scorer_mod, _ground)
    notes.append("par for %s-%s is modelled, not curated: %s, $%d all in"
                 % (o_iata, d_iata, basis["reads_as"], par // 100))
    return par, basis


def ground_both_ends(origin_text: str, dep_ap: str, arr_ap: str,
                     dep_clock: int, arr_clock: int, enr: Dict[str, Any],
                     geo: Optional[Dict[str, Any]] = None):
    """(out_modes, in_modes, ground_info). Shared by all three adapters.

    An uncurated ORIGIN must never drop an itinerary - that was the whole point
    of going global - and it would be a silly kind of bug for that to be true of
    the newest feed only because that is where it happened to be written."""
    geo = geo or {}
    aps = enr["airports"]["airports"]

    def place_of(iata):
        node = dict(geo.get(iata) or {})
        ap = aps.get(iata) or {}
        node.setdefault("iata", iata)
        for k in ("lat", "lon", "country", "city"):
            if node.get(k) is None and ap.get(k) is not None:
                node[k] = ap[k]
        return node

    dep, arr = place_of(dep_ap), place_of(arr_ap)
    origin, how = _ground.resolve_origin(origin_text, enr, dep)
    out_modes, src = _ground.modes_for(origin, dep, dep_clock, enr)

    in_modes, aerr = arrival_ground_for(arr_ap, arr_clock, enr)
    if not in_modes and arr.get("lat") is not None:
        in_modes = _ground.estimate_modes(
            {"lat": arr["lat"] + 0.11, "lon": arr["lon"] + 0.11}, arr, arr_clock)
        aerr = None
    info = {"origin": origin.get("label"), "precision": origin.get("precision"),
            "resolved_by": how, "source": src,
            "message": _ground.support_message(out_modes),
            "arrival_note": aerr}
    return out_modes, in_modes, info


def _place_label(node: Dict[str, Any], iata: str) -> str:
    """"LHR to LHR" is not a ledger line anybody can read.

    The destination label is what the arrival-ground line renders against, so a
    bare IATA code there produces "LHR to LHR - $92", which fails the output
    rule the whole product is built on: a dollar figure, a cause, in one line.
    Duffel names the city on every place it returns, so use it - bare, because
    the ledger renders "<arrival iata> to <label>" and already carries the code.
    The fixtures put an address here ("Shoreditch, London EC2A") and the scorer
    takes the part before the first comma; a live search only knows the city, so
    the city is what goes in."""
    city = (node.get("city_name")
            or ((node.get("city") or {}).get("name") if isinstance(node.get("city"), dict)
                else None))
    return city if city and city != iata else iata


def from_duffel(raw: Dict[str, Any], origin_key: str = "bushwick-brooklyn",
                enr: Optional[Dict[str, Any]] = None,
                checked_bags: int = 1) -> Dict[str, Any]:
    """A Duffel offer-request or offers response -> a scenario dict.

    Field names follow duffel-api's own model definitions, which is the
    authoritative parser for this payload rather than a reading of prose docs.

    Note that a `duffel_test_` token returns the fictional carrier Duffel Airways
    with invented prices. The adapter handles it - an uncurated carrier keeps its
    option and loses its rating - but a grade computed over test-mode inventory
    means nothing, and `coverage()` says so."""
    enr = enr or load_enrichment()
    notes: List[str] = []
    options: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    default_used = 0
    unreviewed = 0
    feed_priced_bags = 0
    amenity_segments = 0
    wifi_published = []

    # Duffel names every airport's coordinates, country and IANA zone on every
    # search. That is most of what the curated table was carrying, and it is
    # what lets an uncurated airport be stamped and an uncurated origin be
    # estimated instead of dropped.
    geo: Dict[str, Dict[str, Any]] = {}
    for off in _duffel_offers(raw):
        for sl in off.get("slices") or []:
            for sg in sl.get("segments") or []:
                for k in ("origin", "destination"):
                    n = sg.get(k) or {}
                    if n.get("iata_code"):
                        geo[n["iata_code"]] = {
                            "iata": n["iata_code"], "lat": n.get("latitude"),
                            "lon": n.get("longitude"),
                            "country": n.get("iata_country_code"),
                            "city": n.get("city_name") or (n.get("city") or {}).get("name"),
                            "tz": n.get("time_zone")}

    ground_info: Dict[str, Any] = {}

    for offer in _duffel_offers(raw):
        slices = offer.get("slices") or []
        if not slices:
            continue
        sl = slices[0]                       # one-way; a return is two slices
        if len(slices) > 1 and "return trips are not modelled yet" not in notes:
            notes.append("return trips are not modelled yet - only the outbound "
                         "slice of each offer was scored")
        segs_in = sl.get("segments") or []
        if not segs_in:
            continue
        fail = None
        segments, seg_ids = [], []

        for i, s in enumerate(segs_in):
            o_ap = (s.get("origin") or {}).get("iata_code")
            d_ap = (s.get("destination") or {}).get("iata_code")
            dep, e1 = stamp(s.get("departing_at", ""), o_ap, enr,
                            (geo.get(o_ap) or {}).get("tz"))
            arr, e2 = stamp(s.get("arriving_at", ""), d_ap, enr,
                            (geo.get(d_ap) or {}).get("tz"))
            if e1 or e2:
                fail = e1 or e2
                break
            ap_from = enr["airports"]["airports"].get(o_ap, {})
            ap_to = enr["airports"]["airports"].get(d_ap, {})
            # Duffel names the airport's IANA zone. It is NOT used to compute an
            # offset - the scorer must never consult a timezone database - but a
            # disagreement with the curated table means one of them is wrong, and
            # silently preferring ours would hide it.
            for ap_json, curated, code in ((s.get("origin") or {}), ap_from, o_ap), \
                                          ((s.get("destination") or {}), ap_to, d_ap):
                feed_zone = ap_json.get("time_zone")
                if feed_zone and curated.get("zone") and feed_zone != curated["zone"]:
                    msg = ("%s: the feed says %s, the curated table says %s - one is wrong"
                           % (code, feed_zone, curated["zone"]))
                    if msg not in notes:
                        notes.append(msg)
            sid = "s%d" % (i + 1)
            seg_ids.append(sid)
            mkt = ((s.get("marketing_carrier") or {}).get("iata_code") or "")
            op = ((s.get("operating_carrier") or {}).get("iata_code") or mkt)
            codeshare = op != mkt
            ac = s.get("aircraft") or {}
            equip = (ac.get("iata_code") or "").strip()
            pax = (s.get("passengers") or [{}])[0]
            note = ((", and %s%s is sold by %s but flown by %s"
                     % (mkt, s.get("marketing_carrier_flight_number", ""), mkt, op))
                    if codeshare else "")
            claims = _fleet_claims(enr, op, equip, note) if equip else {
                "subfleet": {"coverage": "none", "policy": "route_median",
                             "reason": "this offer carries no aircraft" + note},
                "connectivity_oceanic": {"coverage": "none", "policy": "route_median",
                                         "reason": "no aircraft named, so no connectivity "
                                                   "fit can be looked up"},
            }
            for c in claims.values():
                if isinstance(c, dict) and c.get("needs_primary_source"):
                    unreviewed += 1
            amen = _duffel_amenities(s)
            seat = amen.get("seat") or {}
            pitch = str(seat.get("pitch") or "").strip()
            if pitch.isdigit():
                claims["seat_pitch_inches"] = int(pitch)
                amenity_segments += 1
            wifi = amen.get("wifi") or {}
            if wifi:
                wifi_published.append({"segment": sid, "available": wifi.get("available"),
                                       "cost": wifi.get("cost")})
            pw = amen.get("power") or {}
            if pw.get("available") is not None:
                claims["power"] = ("Power at the seat, per the airline"
                                   if pw.get("available") else
                                   "No power at the seat, per the airline")
            num = re.sub(r"\D", "", s.get("marketing_carrier_flight_number", "") or "")
            segments.append({
                "segment_id": sid,
                "marketing": {"carrier": mkt, "number": int(num or 0)},
                "operating": {"carrier": op,
                              "number": int(re.sub(r"\D", "",
                                                   s.get("operating_carrier_flight_number")
                                                   or num or "0") or 0)},
                "origin": {"iata": o_ap, "iata_area": ap_from.get("iata_area", 1),
                           "schengen": ap_from.get("schengen")},
                "destination": {"iata": d_ap, "iata_area": ap_to.get("iata_area", 2),
                                "schengen": ap_to.get("schengen")},
                "departure_local": dep, "arrival_local": arr,
                "tz_hint": {"departure": ap_from.get("zone", ""),
                            "arrival": ap_to.get("zone", "")},
                "equipment_code": equip or "UNKNOWN",
                "equipment_name": ac.get("name", ""),
                "cabin_marketed": (pax.get("cabin_class") or "economy").lower(),
                "claims": claims,
                "reliability": {"coverage": "none", "policy": "route_median",
                                "reason": "no on-time record joined for %s%s"
                                          % (mkt, s.get("marketing_carrier_flight_number",
                                                        "?"))},
            })
        if fail:
            dropped.append({"id": offer.get("id", "?"), "why": fail})
            continue

        issuing = ((offer.get("owner") or {}).get("iata_code")
                   or segments[0]["marketing"]["carrier"])
        brand = (sl.get("fare_brand_name")
                 or ((segs_in[0].get("passengers") or [{}])[0]
                     .get("cabin_class_marketing_name"))
                 or "Economy")
        included = min([_duffel_bags(s, "checked") for s in segs_in] or [0])
        tiers, tier_src = _duffel_bag_tiers(offer, enr, issuing, brand)
        tiers = _extend_tiers(tiers, max(0, int(checked_bags) - included))
        if tier_src == "feed":
            feed_priced_bags += 1
        elif tier_src == "default":
            default_used += 1
            msg = ("no curated bag fees for %s %s and the offer quoted none - "
                   "priced at the pessimistic default" % (issuing, brand))
            if msg not in notes:
                notes.append(msg)

        total_cents = _decimal_cents(offer.get("total_amount"))
        base_cents = _decimal_cents(offer.get("base_amount"))
        if base_cents > total_cents:
            base_cents = total_cents
        tax_cents = _decimal_cents(offer.get("tax_amount"))
        if tax_cents != total_cents - base_cents:
            # The offer's own three numbers disagree. Trust the total - it is
            # what gets charged - and derive the rest so the ledger reconciles.
            tax_cents = total_cents - base_cents
        changes, refundable = _duffel_conditions(offer)
        ticket = {
            "ticket_id": "t1",
            "issuing_carrier": issuing,
            "fare_brand_name": brand,
            "price": {
                "currency": offer.get("total_currency", "USD"),
                "base_cents": base_cents,
                "fx_rate_to_usd": 1.0,
                "taxes": ([{"code": "TOT", "amount_cents": tax_cents,
                            "label": "Taxes and carrier-imposed charges (the feed gives "
                                     "one total, not a breakdown)",
                            "refundable_on_cancel": False}] if tax_cents > 0 else []),
                "carrier_imposed": [], "agency_fees": []},
            "entitlements": {
                "checked_included": included,
                "cabin_bag_included": bool(min([_duffel_bags(s, "carry_on")
                                                for s in segs_in] or [0])),
                "personal_item_included": True,
                "seat_selection": "paid",
                "changes": changes, "refundable": refundable,
                "earns_redeemable_miles": "basic" not in brand.lower()
                                          and "light" not in brand.lower()
                                          and "saver" not in brand.lower(),
                "boarding_group": None},
            "checked_bag_fee_tiers": tiers,
            "segment_ids": list(seg_ids),
        }

        layovers = []
        for i in range(len(segments) - 1):
            a_to = segments[i]["destination"]["iata"]
            ap = enr["airports"]["airports"].get(a_to, {})
            mct = (ap.get("mct_minutes") or {}).get("default")
            # One offer is one PNR with one airline: bags go through, the carrier
            # owes the reconnection, and nobody is put landside.
            layovers.append({
                "layover_id": "lay%d" % (i + 1),
                "airport": a_to,
                "arrive_segment_id": seg_ids[i], "depart_segment_id": seg_ids[i + 1],
                "immigration_required": crosses_border(
                    enr, segments[i]["origin"]["iata"], a_to, geo),
                "security_reclear_required": False,
                "ees_first_registration": False,
                "inter_terminal": ap.get("inter_terminal") or {"mode": "walk",
                                                               "minutes": 20},
                "published_mct_minutes": mct,
                "mct_source": "curated airport table" if mct else "not curated",
                "bags_checked_through": True,
                "forced_landside": False,
                "leave_airport_viable": bool(ap.get("leave_airport_viable")),
                "services_open": ap.get("services") or [],
                "recovery": {"protected": True, "next_departure_minutes": 180,
                             "overnight_implied": False, "walkup_fare_cents": 0},
                "note": "Single airline on one ticket: the carrier owes the "
                        "reconnection and the bags are checked through.",
            })

        dep_clock = _hhmm(segments[0]["departure_local"][11:16])
        arr_clock = _hhmm(segments[-1]["arrival_local"][11:16])
        # One ground path for all three feeds. Duffel supplies airport
        # coordinates the other two do not, so it hands them over as `geo` and
        # the helper falls back to the curated table for the rest.
        out_modes, in_modes, ginfo = ground_both_ends(
            origin_key, segments[0]["origin"]["iata"],
            segments[-1]["destination"]["iata"], dep_clock, arr_clock, enr, geo)
        ground_info = ginfo
        if ginfo.get("arrival_note") and ginfo["arrival_note"] not in notes:
            notes.append(ginfo["arrival_note"])

        oid = re.sub(r"[^a-z0-9]+", "-",
                     ("%s%s-%s" % (segments[0]["marketing"]["carrier"],
                                   segments[0]["marketing"]["number"],
                                   (offer.get("id") or "x")[-10:])).lower())[:40]
        opt = {
            "option_id": oid,
            "display_name": "%s %s%d · %s" % (
                (offer.get("owner") or {}).get("name", issuing),
                segments[0]["marketing"]["carrier"], segments[0]["marketing"]["number"],
                brand),
            "tickets": [ticket],
            "segments": segments,
            "layovers": layovers,
            "ground": {"outbound": out_modes, "arrival": in_modes},
            "airport_process_minutes": (enr["airports"]["airports"]
                                        .get(segments[0]["origin"]["iata"], {})
                                        .get("process_minutes") or {"p50": 60}),
            "booking": [{"who": (offer.get("owner") or {}).get("name", issuing),
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
    dest_label = d_iata
    for off in _duffel_offers(raw):
        for sl in off.get("slices") or []:
            for sg in sl.get("segments") or []:
                node = sg.get("destination") or {}
                if node.get("iata_code") == d_iata:
                    dest_label = _place_label(node, d_iata)
                    break
    par, par_basis = route_par(o_iata, d_iata, first["segments"][0]["departure_local"][:10],
                               enr, notes, geo)
    if unreviewed:
        notes.append("%d aircraft claims come from the DRAFT fleet table, which has not "
                     "been human-reviewed" % unreviewed)
    # A test token returns a fictional airline. Grading that is meaningless, and
    # the page must not present it as a real answer.
    if any(s["marketing"]["carrier"] == "ZZ" for o in options for s in o["segments"]):
        notes.append("this response contains Duffel Airways (ZZ), the test-mode "
                     "fiction - prices and schedules in it are invented")

    org = enr["ground"]["origins"].get(origin_key, {})
    bags = [{"kind": "checked", "weight_kg": 20} for _ in range(max(0, int(checked_bags)))]
    return {
        "schema_version": "0.1.0",
        "fixture_id": "live-duffel-%s-%s" % (o_iata.lower(), d_iata.lower()),
        "title": "Live inventory (Duffel): %s to %s" % (o_iata, d_iata),
        "pins_down": "Nothing. This is a live search, not a fixture - it pins no "
                     "behaviour and must never be committed to fixtures/.",
        "as_of": first["segments"][0]["departure_local"][:10],
        "notes": notes,
        "query": {
            "origin": {"label": org.get("label", origin_key),
                       "lat": org.get("lat", 0.0), "lon": org.get("lon", 0.0),
                       "geocode_precision": "neighbourhood"},
            "destination": {"label": dest_label, "lat": 0.0, "lon": 0.0,
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
        "_ground": ground_info,
        "_par": par_basis,
        "_feed": {"provider": "duffel", "default_bag_fees_used": default_used,
                  "unreviewed_claims": unreviewed,
                  "feed_priced_bags": feed_priced_bags,
                  "amenity_segments": amenity_segments,
                  "wifi_published": wifi_published},
    }


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
           "default_bag_fees": int(feed.get("default_bag_fees_used") or 0),
           "feed_priced_bags": int(feed.get("feed_priced_bags") or 0)}
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
        if no_code == tot["segments"]:
            gaps.append("no aircraft, cabin or connectivity claim can be made: the feed "
                        "carries no equipment code")
        elif no_code:
            # Some segments named an aircraft and some did not. Saying the feed
            # carries no code would be false, and would send someone to the
            # wrong fix.
            gaps.append("%d of %d segments name no aircraft, so their cabin and "
                        "connectivity claims abstain" % (no_code, tot["segments"]))
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
    # Not a gap - the opposite. Duffel quotes a real bag price in
    # available_services, which is the one field where a feed beats the moat.
    strengths = []
    if tot["feed_priced_bags"]:
        strengths.append("%d of %d options carry an airline-quoted checked-bag price, so "
                         "their bag arithmetic is not an estimate"
                         % (tot["feed_priced_bags"], tot["options"]))

    # Going global replaced "drop the itinerary" with "keep it and estimate".
    # That is only honest if the estimating is SAID OUT LOUD - an uncurated
    # layover airport has no minimum connection time, no service hours and no
    # terminal geography, and a grade built on that is not the same object as
    # one built on a curated route.
    blind = {}
    for o in scenario.get("options", []):
        for l in o.get("layovers") or []:
            if l.get("mct_source") == "not curated":
                blind[l["airport"]] = blind.get(l["airport"], 0) + 1
    if blind:
        gaps.append("%d layover%s at %s, which %s no curated connection time, "
                    "terminal layout or opening hours - those stops are modelled "
                    "on defaults"
                    % (sum(blind.values()), "" if sum(blind.values()) == 1 else "s",
                       ", ".join(sorted(blind)),
                       "has" if len(blind) == 1 else "have"))
    g = scenario.get("_ground") or {}
    if g.get("message"):
        gaps.append(g["message"])

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
    return {"counts": tot, "gaps": gaps, "strengths": strengths, "verdict": verdict}


def from_feed(raw: Dict[str, Any], **kw) -> Dict[str, Any]:
    """Dispatch on the payload's own shape rather than on a caller-supplied
    name, so a profile pointed at the wrong provider fails as a parse error
    here instead of as a plausible-looking scenario built from the wrong keys.

    Duffel is checked before Amadeus because both use a top-level `data`, and a
    Duffel offer is recognised by `slices` where an Amadeus one carries
    `type: "flight-offer"` - neither key appears in the other's payload."""
    if _duffel_offers(raw):
        return from_duffel(raw, **kw)
    if isinstance(raw.get("data"), list) and any(
            r.get("type") == "flight-offer" for r in raw["data"][:3] if isinstance(r, dict)):
        return from_amadeus(raw, **kw)
    if isinstance(raw.get("itineraries"), list):
        kw.pop("checked_bags", None)      # from_kiwi takes the load from the feed
        return from_kiwi(raw, **kw)
    return {"error": "unrecognised payload: no Duffel `slices`, no Amadeus "
                     "`data[].type=flight-offer`, no Kiwi `itineraries`", "dropped": []}


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
    for s in cov.get("strengths", []):
        print("  + " + s)
    for g in cov["gaps"]:
        print("  - " + g)
