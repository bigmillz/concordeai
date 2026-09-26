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

import datetime
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
              enr: Optional[Dict[str, Any]] = None,
              dest_point: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                "cabin_marketed": cabin_norm(s.get("cabinClass")),
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
            segments[-1]["destination"]["iata"], dep_clock, arr_clock, enr,
            dest_point=dest_point)
        ground_info = ginfo
        if ginfo.get("arrival_note") and ginfo["arrival_note"] not in notes:
            notes.append(ginfo["arrival_note"])

        rating = _carrier_rating(enr, segs_in[0]["carrier"], segments)
        opt = {
            "option_id": oid,
            "display_name": "%s %s" % (segs_in[0].get("carrierName", ""), leg.get("route", [""])[-1]),
            "tickets": tickets,
            "segments": segments,
            "layovers": layovers,
            "ground": {"outbound": out_modes, "arrival": in_modes},
            "airport_process_minutes": _airport_block(enr, segments),
            "arrival_process_minutes": _arrival_block(enr, segments, int(bags.get("checkedBag", 0))),
            "booking": [{"who": "Kiwi.com", "price_cents": total_cents, "direct": False,
                         "note": "Self-transfer protection is Kiwi's own, not the airlines'"
                                 if len(groups) > 1 else "Sold by Kiwi.com, not the airline"}],
        }
        if rating:
            opt["carrier_rating"] = rating
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


SHORT_HAUL_KM = 3500


def _short_haul(o_iata: str, d_iata: str, enr: Dict[str, Any], geo: Optional[Dict[str, Any]] = None) -> bool:
    """A trip that starts and ends in one country, or covers at most SHORT_HAUL_KM: the fares where airlines
    charge their domestic or regional bag fees, not their long-haul ones (2026-09-24: every row was a
    transatlantic fee, and JetBlue's domestic bag was priced at $75). Unknown reads as long-haul, whose
    fees are the higher ones: unknown is never cheap."""
    def place(iata):
        ap = (enr.get("airports") or {}).get("airports", {}).get(iata) or {}
        g = (geo or {}).get(iata) or {}
        return (ap.get("lat", g.get("lat")), ap.get("lon", g.get("lon")), ap.get("country") or g.get("country"))
    la1, lo1, c1 = place(o_iata)
    la2, lo2, c2 = place(d_iata)
    if c1 and c2 and str(c1).upper() == str(c2).upper():
        return True
    if None in (la1, lo1, la2, lo2):
        return False
    return _ground.haversine_km(float(la1), float(lo1), float(la2), float(lo2)) <= SHORT_HAUL_KM


# IATA traffic conference areas by country (Area 1 the Americas, Area 3 Asia and the Pacific, everything else Area 2:
# Europe, Africa, the Middle East). Airlines' long-haul catering follows intercontinental flying, which is what an area
# crossing is (2026-09-25): New York to Dublin crosses, New York to Costa Rica and Canada to Hawaii do not.
_AREA1 = set(("US CA MX GT BZ SV HN NI CR PA CO VE EC PE BO CL AR UY PY BR GY SR GF CU JM HT DO PR BS BB TT AG DM LC VC GD "
              "KN AI VG VI KY TC BM AW CW SX BQ MQ GP MS BL MF PM GL FK").split())
_AREA3 = set(("CN JP KR KP TW HK MO MN IN PK BD LK NP BT MV AF MM TH LA KH VN MY SG ID BN PH TL AU NZ PG FJ SB VU NC PF WS "
              "TO KI TV NR FM MH PW GU MP CK NU WF KZ UZ TM TJ KG").split())


def iata_area(iata: str, enr: Dict[str, Any], geo: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """The airport's IATA area: the curated table's, else from its country; None when neither is known."""
    ap = ((enr.get("airports") or {}).get("airports") or {}).get(iata) or {}
    if ap.get("iata_area"):
        return int(ap["iata_area"])
    cc = str(ap.get("country") or ((geo or {}).get(iata) or {}).get("country") or "").upper()
    if not cc:
        return None
    return 1 if cc in _AREA1 else 3 if cc in _AREA3 else 2


def meal_long_haul(o_iata: str, d_iata: str, minutes: int, enr: Dict[str, Any], geo: Optional[Dict[str, Any]] = None) -> bool:
    """Whether a flight gets an airline's LONG-haul catering: it crosses an IATA area, or it covers more than
    SHORT_HAUL_KM and runs 8 hours or more (New York to Sao Paulo stays in the Americas and is a long-haul flight).
    Seattle to Costa Rica, Canada to Hawaii and Frankfurt to Cape Verde get the short-haul product, and read so."""
    a, b = iata_area(o_iata, enr, geo), iata_area(d_iata, enr, geo)
    if a and b and a != b:
        return True
    return (not _short_haul(o_iata, d_iata, enr, geo)) and int(minutes or 0) >= 480


_MEALS: Dict[str, Any] = {}


def load_meals() -> Dict[str, Any]:
    """enrichment/meals.json: each airline's published catering by cabin and flight length (2026-09-25, per Patrick:
    "so people don't end up with a bag of pretzels on a long flight instead of an actual meal")."""
    if not _MEALS:
        try:
            with open(os.path.join(ENRICH, "meals.json"), encoding="utf-8") as fh:
                _MEALS.update(json.load(fh))
        except (OSError, ValueError):
            _MEALS.update({"airlines": {}})
    return _MEALS


def meal_for(carrier: Any, cabin: Any, minutes: Any, short_haul: bool, brand: Any = "",
             table: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], str]:
    """What the airline flying the longest flight publishes it serves in that cabin on a flight that long:
    ("meal" | "snack" | "buy" | "none", the rule's sentence), or (None, "") when it publishes nothing that fits.
    A rule for the fare brand (a Basic or Light fare with no meal) wins over the cabin's; premium economy with no
    rule of its own reads economy's (never better than what is published); unknown is never a meal."""
    rows = ((table or load_meals()).get("airlines") or {}).get(str(carrier or "").upper()) or []
    cab = cabin_norm(cabin) if cabin else "economy"
    mins = int(minutes or 0)
    words = set(re.findall(r"[a-z]+", str(brand or "").lower()))

    def fits(r, want_cabin):
        if r.get("cabin") != want_cabin:
            return False
        if r.get("haul", "all") != "all" and (r["haul"] == "short") != bool(short_haul):
            return False
        if r.get("min_minutes") is not None and mins < int(r["min_minutes"]):
            return False
        if r.get("max_minutes") is not None and mins > int(r["max_minutes"]):
            return False
        return True
    ladder = ["first", "business", "premium_economy", "economy"]
    for want in ladder[ladder.index(cab):]:
        cands = [r for r in rows if fits(r, want)]
        branded = [r for r in cands if r.get("brands") and words & {w.lower() for w in r["brands"]}]
        plain = [r for r in cands if not r.get("brands")]
        pick = branded or plain
        if pick:
            # the most specific rule: one bounded by flight length beats an open one
            pick.sort(key=lambda r: -((r.get("min_minutes") is not None) + (r.get("max_minutes") is not None)))
            served, rule = pick[0].get("served"), pick[0].get("rule") or ""
            if want == cab or (cab == "premium_economy" and want == "economy"):
                return served, rule
            # a business or first cabin with no rule of its own (United publishes only economy's; American's first
            # was not researched): a cabin below it that serves a meal on this flight means this one does too, since
            # no airline feeds its front cabin less than its back one (2026-09-25, per Patrick: a first-class search
            # read "no meal"). A snack or food for sale below says nothing about the front, so that stays unknown.
            if served == "meal":
                return "meal", "%s gets a meal on this flight, so %s does too. %s" % (
                    want.replace("_", " ").capitalize(), cab.replace("_", " "), rule)
            break
    return None, ""


def cabin_norm(word: Any) -> str:
    """One cabin vocabulary whatever the feed says: Duffel's 'business', Google's 'Business Class', Kiwi's
    'BUSINESS' and Amadeus's 'PREMIUM_ECONOMY' all become the scorer's economy / premium_economy / business / first
    (2026-09-24: Google's 'business_class' matched nothing, so its premium rows graded as economy)."""
    w = str(word or "").strip().lower().replace("-", " ").replace("_", " ")
    if w.startswith("first"):
        return "first"
    if w.startswith("business") or w.startswith("upper"):
        return "business"
    if w.startswith("premium"):
        return "premium_economy"
    return "economy"


def _carrier_rating(enr: Dict[str, Any], carrier: str, segments: List[Dict[str, Any]],
                    geo: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The airline's curated rating for this trip, or None. A row's `short_haul` block replaces its rating and
    note on a short-haul trip (the same _short_haul test the bag fees use): several airlines fly a better product
    on long-haul than at home, and one number for both misstates one of them (2026-09-24, per Patrick). The
    row's own source and date win over the file's."""
    cars = enr["carriers"]
    row = (cars.get("ratings") or {}).get(carrier)
    if not row:
        return None
    # the cabin flown on the trip's longest flight picks the rating: Lufthansa's first class is not its economy
    # (2026-09-24, per Patrick); economy, or a cabin nobody rates, uses the airline's own row
    use = row
    if segments:
        def _mins(sg):
            try:
                return (datetime.datetime.fromisoformat(sg["arrival_local"]) - datetime.datetime.fromisoformat(sg["departure_local"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                return 0
        cab = cabin_norm(max(segments, key=_mins).get("cabin_marketed"))
        block = (row.get("cabins") or {}).get(cab) if cab != "economy" else None
        if block:
            use = dict(block, note=row.get("note", ""))
    if use.get("short_haul") and segments and _short_haul(segments[0]["origin"]["iata"],
                                                         segments[-1]["destination"]["iata"], enr, geo):
        use = dict(use, **use["short_haul"])
    out = {"rating": use["rating"], "note": use.get("note", ""),
           "source": row.get("source") or cars["_source"], "as_of": row.get("as_of") or cars["_as_of"]}
    if use.get("basis"):
        out["basis"] = use["basis"]             # the published scores behind it, for the page's flight card
    return out


def _fare_row(enr: Dict[str, Any], carrier: str, brand: str, short: bool = False
              ) -> Tuple[Dict[str, Any], bool]:
    """(the brand's row, used_the_pessimistic_default). On a short-haul trip a row's `short_haul` block,
    where it has one, replaces its tiers, included bags and carry-on; the default has its own short-haul
    version for the same reason."""
    fares = enr.get("fares") or {}
    key = _brand_key(fares, carrier, brand)
    row = (fares.get("brands") or {}).get(key) if key else None
    used_default = not row
    if not row:
        row = (fares.get("_default_short_haul") if short else None) or fares.get("_default") or {"tiers": [], "note": ""}
    elif short and row.get("short_haul"):
        row = dict(row, **row["short_haul"])
    return row, used_default


def _bag_tiers(enr: Dict[str, Any], carrier: str, brand: str, short: bool = False
               ) -> Tuple[List[Dict[str, Any]], bool]:
    """(tiers, used_the_pessimistic_default)."""
    row, used_default = _fare_row(enr, carrier, brand, short)
    tiers = [dict(t) for t in row.get("tiers") or []]
    if used_default and tiers and row.get("note"):
        tiers[0]["note"] = row["note"]
    return tiers, used_default


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


LEGROOM_SEAT = re.compile(r"extra|legroom|leg room|plus|comfort|space|exit|main cabin extra|preferred plus", re.I)


def google_bag_prices(lines: Any) -> List[Dict[str, Any]]:
    """Google's bag sentences for one booking option ("1st checked bag: 35", "2nd checked bag: 45-60", "1 free
    checked bag", "1st checked bag free", "1 free carry-on"), as [{piece, cents, range}] for the checked pieces, in dollars (the booking
    options are asked in USD). A range is priced at its top, as every bag fee here leans high, and says so; a free
    piece is 0; carry-on sentences are left to the fare's own carry-on field. Nothing parsed is an empty list, never
    a free bag (2026-09-25: the seller check already fetches these, so the flight's own bag price costs no call)."""
    out: Dict[int, Dict[str, Any]] = {}
    for line in lines or []:
        t = str(line).lower().replace("\u2013", "-").replace(",", "")
        m = re.search(r"(\d+)(?:st|nd|rd|th) checked bag[^:]*:\s*\$?(\d+(?:\.\d+)?)(?:\s*-\s*\$?(\d+(?:\.\d+)?))?", t)
        if m:
            top = float(m.group(3) or m.group(2))
            out[int(m.group(1))] = {"piece": int(m.group(1)), "cents": int(round(top * 100)), "range": bool(m.group(3))}
            continue
        m = re.search(r"(\d+)(?:st|nd|rd|th) checked bag free", t)
        if m:
            out[int(m.group(1))] = {"piece": int(m.group(1)), "cents": 0, "range": False}
            continue
        m = re.search(r"(\d+) free checked bags?", t)
        if m:
            for i in range(1, int(m.group(1)) + 1):
                out.setdefault(i, {"piece": i, "cents": 0, "range": False})
    return [out[k] for k in sorted(out)]


def duffel_extras_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One offer's own bag and seat prices, from Duffel's offer-with-services and seat-map replies (live.duffel_extras,
    2026-09-25). Only what the airline PRICED counts: a seat whose available_services is empty is unavailable, never
    free, and an empty services list means the airline quotes none here, never that bags are free (unknown is never
    cheap). Amounts stay in the offer's own currency; the server converts them.

    -> {"bags": [{"kind", "amount", "currency", "max", "kg"}], "seat": {"any", "legroom", "free", "currency",
        "segments", "priced_segments"} or None, "expired": bool}"""
    out: Dict[str, Any] = {"bags": [], "seat": None, "expired": bool((payload or {}).get("expired"))}
    off = (payload or {}).get("offer") or {}
    for svc in off.get("available_services") or []:
        if svc.get("type") != "baggage":
            continue
        md = svc.get("metadata") or {}
        try:
            amount = float(svc.get("total_amount"))
        except (TypeError, ValueError):
            continue
        out["bags"].append({"kind": md.get("type") or "checked", "amount": amount, "currency": svc.get("total_currency"),
                            "max": int(svc.get("maximum_quantity") or 1), "kg": md.get("maximum_weight_kg")})
    out["bags"].sort(key=lambda b: (b["kind"] != "checked", b["amount"]))
    maps = (payload or {}).get("seat_maps") or []
    # extra legroom is priced on the trip's LONGEST flight, the one the Extra legroom want judges (2026-09-25: a
    # SWISS trip had an $113 extra-legroom seat across the Atlantic and none on its short connection, and asking for
    # one on every flight left the price unknown). A trip whose flight lengths the offer does not give reads its
    # first map as the long one only when there is a single flight.
    mins = {}
    for sl in off.get("slices") or []:
        for sg in sl.get("segments") or []:
            m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", str(sg.get("duration") or ""))
            if sg.get("id") and m and any(m.groups()):
                mins[sg["id"]] = int(m.group(1) or 0) * 1440 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    longest = max(maps, key=lambda mp: mins.get(mp.get("segment_id"), -1)) if maps and (mins or len(maps) == 1) else None
    any_total, leg_price, priced, free, cur = 0.0, None, 0, False, None
    for m in maps:
        cheapest, cheapest_leg = None, None
        for cab in m.get("cabins") or []:
            for row in cab.get("rows") or []:
                for sec in row.get("sections") or []:
                    for el in sec.get("elements") or []:
                        if el.get("type") != "seat":
                            continue
                        for sv in el.get("available_services") or []:
                            try:
                                amt = float(sv.get("total_amount"))
                            except (TypeError, ValueError):
                                continue
                            cur = cur or sv.get("total_currency")
                            cheapest = amt if cheapest is None else min(cheapest, amt)
                            if LEGROOM_SEAT.search(" ".join([el.get("name") or ""] + list(el.get("disclosures") or []))):
                                cheapest_leg = amt if cheapest_leg is None else min(cheapest_leg, amt)
        if cheapest is None:
            continue
        priced += 1
        any_total += cheapest
        free = free or cheapest == 0
        if m is longest and cheapest_leg is not None:
            leg_price = cheapest_leg
    if priced and priced == len(maps):
        # a seat on every flight of the trip is the price of choosing one; a map with nothing on sale leaves it unknown
        out["seat"] = {"any": round(any_total, 2), "legroom": round(leg_price, 2) if leg_price is not None else None,
                       "free": free and any_total == 0, "currency": cur, "segments": len(maps), "priced_segments": priced}
    return out


def from_amadeus(raw: Dict[str, Any], origin_key: str = "bushwick-brooklyn",
                 enr: Optional[Dict[str, Any]] = None,
                 checked_bags: int = 1,
                 dest_point: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                "cabin_marketed": cabin_norm(det.get("cabin")),
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
            segments[-1]["destination"]["iata"], dep_clock, arr_clock, enr,
            dest_point=dest_point)
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
            "airport_process_minutes": _airport_block(enr, segments),
            "arrival_process_minutes": _arrival_block(enr, segments, checked_bags),
            "booking": [{"who": carrier_names.get(issuing, issuing).title(),
                         "price_cents": total_cents, "direct": True,
                         "note": "Book direct with %s." % issuing}],
        }
        rating = _carrier_rating(enr, segments[0]["operating"]["carrier"], segments)
        if rating:
            opt["carrier_rating"] = rating
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
    sl = offer.get("slices") or [{}]
    ends = [(sl[0].get("origin") or {}), (sl[-1].get("destination") or {})]
    geo = {e.get("iata_code"): {"lat": e.get("latitude"), "lon": e.get("longitude"), "country": e.get("iata_country_code")}
           for e in ends if e.get("iata_code")}
    short = _short_haul(ends[0].get("iata_code", ""), ends[1].get("iata_code", ""), enr, geo)
    fallback, used_default = _bag_tiers(enr, carrier, brand, short)
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


def _airport_block(enr: Dict[str, Any], segments: List[Dict[str, Any]],
                   geo: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """How long before departure the traveller is at the first airport (2026-09-24, per Patrick): an hour when
    the whole trip stays inside one border, an hour and a half when any flight crosses one, or the curated
    airport's own median when that is longer. The rule itself lives in par.airport_minutes, so the reference
    itinerary and every real option are timed the same way and the speed grade compares like with like."""
    first = segments[0]["origin"]["iata"]
    intl = any(crosses_border(enr, first, s["destination"]["iata"], geo) for s in segments)
    return _par.airport_minutes(enr["airports"]["airports"], first, intl)


# US Customs and Border Protection preclearance: a traveller clears US immigration and customs BEFORE boarding at
# these airports and lands in the US as a domestic arrival (CBP's published list, 2026).
US_PRECLEARANCE = frozenset("YYZ YVR YUL YOW YYC YEG YWG YHZ DUB SNN NAS BDA AUA AUH".split())


def _arrival_international(enr: Dict[str, Any], o_iata: str, d_iata: str, geo: Optional[Dict[str, Any]] = None) -> bool:
    """Does landing at d_iata from o_iata mean passport control and customs on arrival?"""
    if o_iata in US_PRECLEARANCE and border_of(d_iata, enr, geo) == "us":
        return False
    return crosses_border(enr, o_iata, d_iata, geo)


def _arrival_block(enr: Dict[str, Any], segments: List[Dict[str, Any]], checked_bags: int,
                   geo: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Off the plane and out of the last airport (2026-09-24, per Patrick): par.arrival_minutes, judged on the LAST
    flight, since a border crossed earlier was cleared during the layover."""
    last = segments[-1]
    return _par.arrival_minutes(_arrival_international(enr, last["origin"]["iata"], last["destination"]["iata"], geo),
                                checked_bags)


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
    par, basis = _par.par_for(o, d, date, enr, scorer_mod, _ground,
                              international=crosses_border(enr, o_iata, d_iata, geo),
                              arrival_international=_arrival_international(enr, o_iata, d_iata, geo))
    notes.append("par for %s-%s is modelled, not curated: %s, $%d all in"
                 % (o_iata, d_iata, basis["reads_as"], par // 100))
    return par, basis


def ground_both_ends(origin_text: str, dep_ap: str, arr_ap: str,
                     dep_clock: int, arr_clock: int, enr: Dict[str, Any],
                     geo: Optional[Dict[str, Any]] = None,
                     dest_point: Optional[Dict[str, Any]] = None):
    """(out_modes, in_modes, ground_info). Shared by all three adapters.

    `dest_point` is where the trip ENDS when the traveller typed an address on
    the destination side (a hotel, a friend's flat): a dict with lat, lon and a
    label, geocoded by the server. With it, the arrival ride is priced from the
    airport to that door through the same model as the outbound ride, instead
    of to a point near the airport standing in for "the city".

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

    if dest_point and dest_point.get("lat") is not None and arr.get("lat") is not None:
        in_modes, _isrc = _ground.modes_for(dict(dest_point, key=dest_point.get("key")), arr, arr_clock, enr)
        aerr = None
    else:
        in_modes, aerr = arrival_ground_for(arr_ap, arr_clock, enr)
    if not in_modes and arr.get("lat") is not None:
        in_modes = _ground.estimate_modes(
            {"lat": arr["lat"] + 0.11, "lon": arr["lon"] + 0.11}, arr, arr_clock)
        aerr = None
    info = {"origin": origin.get("label"), "precision": origin.get("precision"),
            "resolved_by": how, "source": src,
            "message": _ground.support_message(out_modes),
            "destination": (dest_point or {}).get("label"),
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
                checked_bags: int = 1,
                dest_point: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                "cabin_marketed": cabin_norm(pax.get("cabin_class")),
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
            segments[-1]["destination"]["iata"], dep_clock, arr_clock, enr, geo,
            dest_point=dest_point)
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
            "airport_process_minutes": _airport_block(enr, segments, geo),
            "arrival_process_minutes": _arrival_block(enr, segments, checked_bags, geo),
            "booking": [{"who": (offer.get("owner") or {}).get("name", issuing),
                         "price_cents": total_cents, "direct": True,
                         "note": "Book direct with the airline."}],
        }
        rating = _carrier_rating(enr, segments[0]["operating"]["carrier"], segments, geo)
        if rating:
            opt["carrier_rating"] = rating
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
            "destination": ({"label": dest_point["label"], "lat": dest_point["lat"], "lon": dest_point["lon"],
                             "geocode_precision": "address"} if dest_point and dest_point.get("lat") is not None
                            else {"label": dest_label, "lat": 0.0, "lon": 0.0, "geocode_precision": "city"}),
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
        gaps.append("Aircraft and wifi details are a DRAFT, not yet verified")
    if tot["default_bag_fees"]:
        gaps.append("%d of %d fares list no bag fees, so bags are priced at a high estimate"
                    % (tot["default_bag_fees"], tot["options"]))
    if tot["abstained_claims"]:
        no_code = sum(1 for o in scenario.get("options", []) for s in o["segments"]
                      if s.get("equipment_code") in (None, "", "UNKNOWN"))
        if no_code == tot["segments"]:
            gaps.append("No aircraft or wifi details: this search carries no equipment code")
        elif no_code:
            # Some segments named an aircraft and some did not. Saying the feed
            # carries no code would be false, and would send someone to the
            # wrong fix.
            gaps.append("%d of %d flights name no aircraft, so they have no cabin or wifi details"
                        % (no_code, tot["segments"]))
        else:
            # A different gap with a different fix: the feed did its job and the
            # curated table has not caught up. Saying "no equipment code" here
            # would send someone to change the wrong file.
            n = sum(1 for o in scenario.get("options", []) for s in o["segments"]
                    if ((s.get("claims") or {}).get("subfleet") or {}).get("coverage") == "none")
            gaps.append("No cabin details for the aircraft on %d flight%s"
                        % (n, "" if n == 1 else "s"))
    if tot["codeshare_segments"]:
        gaps.append("%d of %d flights are codeshares, so the airline flying them is unknown"
                    % (tot["codeshare_segments"], tot["segments"]))
    if tot["abstained_reliability"] == tot["segments"] and tot["segments"]:
        gaps.append("No on-time records for these flights yet")
    if not tot["options_with_bag_schedule"]:
        gaps.append("No bag fees listed, so fares with and without bags can't be compared")
    if tot["rated_carriers"] < tot["options"]:
        gaps.append("%d of %d flights are on an unrated airline"
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
        gaps.append("%d layover%s at %s %s estimated: no connection times or opening hours on file"
                    % (sum(blind.values()), "" if sum(blind.values()) == 1 else "s",
                       ", ".join(sorted(blind)),
                       "is" if sum(blind.values()) == 1 else "are"))
    g = scenario.get("_ground") or {}
    if g.get("message"):
        # a placeholder or missing ride is the gap a traveler must see, and the page shows the first
        gaps.insert(len(gaps) if g["message"] == _ground.SUPPORT_NOTE["modelled"] else 0, g["message"])

    enrichment_gaps = len(gaps)
    for d in scenario.get("_dropped", []):
        gaps.append("dropped an itinerary: %s" % d["why"])

    enriched = tot["options_with_ground"] and tot["rated_carriers"]
    if not enriched or enrichment_gaps >= 4:
        verdict = "Grades are indicative: many details are estimated"
    elif tot["unreviewed_claims"]:
        verdict = "Rides, airline ratings and bag fees are researched; aircraft details are a DRAFT"
    elif tot["equipment_known"] == tot["segments"] and tot["segments"]:
        verdict = "Every detail here is researched"
    else:
        verdict = "Rides and airline ratings are researched; aircraft details are not"
    return {"counts": tot, "gaps": gaps, "strengths": strengths, "verdict": verdict}


# ---------------------------------------------------------------- serpapi
#
# Google Flights' results, scraped by proxy through SerpApi, for the carriers
# the main feed cannot sell - Delta today, which does not distribute through
# Duffel (docs/design.md; live.serp_search holds the key). What Google gives:
# the segments with local clock times and NO offset, the airline and flight
# number, an aircraft NAME rather than a code, the cabin, the legroom in
# inches, a few published amenities as sentences ("Wi-Fi for a fee", "In-seat
# power & USB outlets"), the layovers, one price in whole dollars for the
# party, a CO2 estimate, and a booking token only Google can redeem. What it
# does not give: a fare brand, a bag allowance, an operating carrier, base and
# tax. So the offset comes from the curated airports or the zones the main
# feed named (an airport with neither DROPS, as everywhere else), the fare is
# priced as the carrier's no-bag brand because unknown is never cheap and the
# note says so, the aircraft claim abstains, and the booking is the airline's
# own site. An itinerary with a leg on another carrier is not that carrier's.

_SERP_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})$")


def _serp_code(flight_number: Any) -> str:
    """'DL 1' -> 'DL'. Google prints the marketing carrier's number."""
    return str(flight_number or "").strip().split(" ")[0].upper()[:2]


def _serp_number(flight_number: Any) -> str:
    """'B6 1024' -> '1024'. The digits AFTER the carrier code: stripping every non-digit from the whole
    string kept the 6 of B6 (and the 2 of U2, the 9 of W9) and made JetBlue's 1024 into 61024, so the
    same flight never matched the feed's copy and the page printed a number that does not exist (2026-09-24;
    DL has no digit, which is why the Delta-only supplement never showed it)."""
    parts = str(flight_number or "").strip().split()
    tail = " ".join(parts[1:]) if len(parts) > 1 else str(flight_number or "").strip()[2:]
    return re.sub(r"\D", "", tail)


def serp_itin_key(it: Dict[str, Any]) -> tuple:
    """A Google itinerary keyed as _itin_key keys an option: carrier, number and local departure minute per
    segment ('BA 117' leaving '2026-11-11 09:35' -> ('BA', 117, '2026-11-11T09:35'))."""
    return tuple((_serp_code(f.get("flight_number")), int(_serp_number(f.get("flight_number")) or 0),
                  str((f.get("departure_airport") or {}).get("time") or "").replace(" ", "T")[:16])
                 for f in (it or {}).get("flights") or [])


def _serp_naive(t: Any) -> Optional[str]:
    m = _SERP_TIME.match(str(t or "").strip())
    return "%sT%s:%s:00" % (m.group(1), m.group(2), m.group(3)) if m else None


def _serp_brand(enr: Dict[str, Any], carrier: str) -> str:
    """The carrier's no-bag brand by name: the fare row with the most tiers,
    which is how fares.json says 'no checked bag included'."""
    rows = {k: v for k, v in ((enr.get("fares") or {}).get("brands") or {}).items()
            if k.startswith(carrier + ":")}
    if not rows:
        return "Basic Economy"
    k, row = max(rows.items(), key=lambda kv: len(kv[1].get("tiers") or []))
    return (row.get("feed_name") or k.split(":", 1)[1].title()).split("/")[0].strip()


def duffel_geo(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The places a Duffel payload names, keyed by IATA, with the zone, the
    coordinates and the city: what the supplement borrows for airports the
    curated table does not know."""
    geo: Dict[str, Dict[str, Any]] = {}
    for off in _duffel_offers(raw):
        for sl in off.get("slices") or []:
            for sg in sl.get("segments") or []:
                for k in ("origin", "destination"):
                    n = sg.get(k) or {}
                    if n.get("iata_code"):
                        geo[n["iata_code"]] = {
                            "iata": n["iata_code"], "lat": n.get("latitude"), "lon": n.get("longitude"),
                            "country": n.get("iata_country_code"),
                            "city": n.get("city_name") or (n.get("city") or {}).get("name"),
                            "tz": n.get("time_zone")}
    return geo


def _offset_by_duration(known_iso: Optional[str], other_naive: str, minutes: float, forward: bool) -> Optional[str]:
    """One end of a flight stamped with its offset, the other end's local clock, and the flight's duration
    -> the other end's UTC offset ('+09:00'), rounded to the quarter hour every real zone keeps; None when
    the arithmetic lands outside the -12:00..+14:00 that zones span."""
    try:
        known = datetime.datetime.fromisoformat(str(known_iso))
        other = datetime.datetime.fromisoformat(str(other_naive)[:19])
    except (TypeError, ValueError):
        return None
    if known.tzinfo is None:
        return None
    step = datetime.timedelta(minutes=float(minutes))
    other_utc = (known - step if not forward else known + step).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    off = round((other - other_utc).total_seconds() / 900.0) * 15
    if not -12 * 60 <= off <= 14 * 60:
        return None
    sign = "+" if off >= 0 else "-"
    return "%s%02d:%02d" % (sign, abs(off) // 60, abs(off) % 60)


def from_serpapi(raw: Dict[str, Any], origin_key: str = "bushwick-brooklyn",
                 enr: Optional[Dict[str, Any]] = None, checked_bags: int = 1,
                 carriers: Sequence[str] = ("DL",), geo: Optional[Dict[str, Any]] = None,
                 dest_point: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A SerpApi Google Flights response -> a scenario dict the scorer reads,
    holding only the carriers asked for."""
    enr = enr or load_enrichment()
    geo = geo or {}
    want = {str(c).upper() for c in carriers if c}
    notes: List[str] = []
    options: List[Dict[str, Any]] = []
    dropped: List[Dict[str, str]] = []
    wifi_published: List[Dict[str, Any]] = []
    seen_ids: set = set()
    default_used = amenity_segments = 0
    ground_info: Dict[str, Any] = {}
    bag_brands: Dict[str, str] = {}

    itins = list(raw.get("best_flights") or []) + list(raw.get("other_flights") or [])
    derived: Dict[str, str] = {}      # airport -> UTC offset worked out from a flight's duration
    derived_n = 0
    for n, it in enumerate(itins):
        flights = it.get("flights") or []
        if not flights:
            continue
        codes = [_serp_code(f.get("flight_number")) for f in flights]
        if want and not all(c in want for c in codes):
            dropped.append({"id": "google-%d" % n,
                            "why": "not one of the carriers asked for (%s)" % "/".join(codes)})
            continue
        segments: List[Dict[str, Any]] = []
        seg_ids: List[str] = []
        fail = None
        for i, f in enumerate(flights):
            sid = "s%d" % (i + 1)
            o_ap = str((f.get("departure_airport") or {}).get("id") or "").upper()
            d_ap = str((f.get("arrival_airport") or {}).get("id") or "").upper()
            dep_naive = _serp_naive((f.get("departure_airport") or {}).get("time"))
            arr_naive = _serp_naive((f.get("arrival_airport") or {}).get("time"))
            if not (o_ap and d_ap and dep_naive and arr_naive):
                fail = "a segment without airports or times"
                break
            dep, e1 = stamp(dep_naive, o_ap, enr, (geo.get(o_ap) or {}).get("tz"))
            if e1 and o_ap in derived:
                dep, e1 = dep_naive[:19] + derived[o_ap], None
            arr, e2 = stamp(arr_naive, d_ap, enr, (geo.get(d_ap) or {}).get("tz"))
            if e2 and derived.get(d_ap):
                arr, e2 = arr_naive[:19] + derived[d_ap], None
            # Google gives local clocks and the flight's own duration: with one end's offset known, the
            # other's is arithmetic, not a guess (a connection through an airport no table knows used to
            # drop the itinerary; 2026-09-24). Neither end known still drops it.
            mins = f.get("duration")
            if isinstance(mins, (int, float)) and mins > 0:
                if e2 and not e1:
                    off = _offset_by_duration(dep, arr_naive, mins, forward=True)
                    if off:
                        arr, e2 = arr_naive[:19] + off, None
                        derived[d_ap] = off; derived_n += 1
                elif e1 and not e2:
                    off = _offset_by_duration(arr, dep_naive, mins, forward=False)
                    if off:
                        dep, e1 = dep_naive[:19] + off, None
                        derived[o_ap] = off; derived_n += 1
            if e1 or e2:
                fail = e1 or e2
                break
            ap_from = enr["airports"]["airports"].get(o_ap, {})
            ap_to = enr["airports"]["airports"].get(d_ap, {})
            mkt = codes[i]
            num = _serp_number(f.get("flight_number"))
            claims: Dict[str, Any] = {}
            legroom = re.search(r"(\d+)\s*in", str(f.get("legroom") or ""))
            if legroom:
                claims["seat_pitch_inches"] = int(legroom.group(1))
                amenity_segments += 1
            ext = [str(x) for x in (f.get("extensions") or [])]
            if any(("power" in x.lower() or "usb" in x.lower()) for x in ext):
                claims["power"] = "Power at the seat, per Google Flights"
            wifi = next((x for x in ext if "wi-fi" in x.lower() or "wifi" in x.lower()), None)
            if wifi:
                wifi_published.append({"segment": sid, "available": "no wi-fi" not in wifi.lower(),
                                       "cost": ("free" if "free" in wifi.lower()
                                                else "paid" if "fee" in wifi.lower() else None)})
            segments.append({
                "segment_id": sid,
                "marketing": {"carrier": mkt, "number": int(num or 0)},
                # Google names one carrier per leg; it is taken as the operator
                # too, which is what it means when the number is the carrier's own
                "operating": {"carrier": mkt, "number": int(num or 0)},
                "origin": {"iata": o_ap, "iata_area": ap_from.get("iata_area", 1),
                           "schengen": ap_from.get("schengen")},
                "destination": {"iata": d_ap, "iata_area": ap_to.get("iata_area", 2),
                                "schengen": ap_to.get("schengen")},
                "departure_local": dep, "arrival_local": arr,
                "tz_hint": {"departure": ap_from.get("zone", ""), "arrival": ap_to.get("zone", "")},
                "equipment_code": "UNKNOWN",     # a NAME ("Boeing 767"), not a code: the cabin claim abstains
                "equipment_name": str(f.get("airplane") or ""),
                "cabin_marketed": cabin_norm(f.get("travel_class")),
                "claims": claims,
                "reliability": {"coverage": "none", "policy": "route_median",
                                "reason": "no on-time record joined for %s%s" % (mkt, num)},
                "often_delayed": bool(f.get("often_delayed_by_over_30_min")),
            })
            seg_ids.append(sid)
        if fail:
            dropped.append({"id": "google-%d" % n, "why": fail})
            continue

        issuing = segments[0]["marketing"]["carrier"]
        # Google names no fare, only the cabin. An economy trip is priced as the carrier's no-bag brand (unknown is
        # never cheap); a premium one is named by its cabin, never as "Basic Economy" (2026-09-24: a $10,494 Delta
        # One read Basic Economy), and with no curated row for it, its bags go on the default price
        top = max((sg["cabin_marketed"] for sg in segments), key=lambda c: ["economy", "premium_economy", "business", "first"].index(c)
                  if c in ("economy", "premium_economy", "business", "first") else 0)
        brand = _serp_brand(enr, issuing) if top == "economy" else {"premium_economy": "Premium Economy", "business": "Business", "first": "First"}[top]
        short = _short_haul(segments[0]["origin"]["iata"], segments[-1]["destination"]["iata"], enr, geo)
        frow, dft = _fare_row(enr, issuing, brand, short)
        tiers, _ = _bag_tiers(enr, issuing, brand, short)
        included = 0 if dft else int(frow.get("includes_checked", max(0, 3 - len(tiers))))
        # a full-size carry-on: the row says, for the budget airlines whose lowest fare excludes one
        carry_on = True if dft else bool(frow.get("includes_carry_on", True))
        tiers = _extend_tiers(tiers, max(0, int(checked_bags) - included))
        if dft:
            default_used += 1
        bag_brands[issuing] = brand
        try:
            total_cents = int(round(float(it.get("price")) * 100))
        except (TypeError, ValueError):
            dropped.append({"id": "google-%d" % n, "why": "no price"})
            continue
        airline_name = str(flights[0].get("airline") or issuing)
        ticket = {
            "ticket_id": "t1",
            "issuing_carrier": issuing,
            "fare_brand_name": brand,
            "price": {"currency": "USD", "base_cents": total_cents, "fx_rate_to_usd": 1.0,
                      "taxes": [], "carrier_imposed": [], "agency_fees": []},
            "entitlements": {
                "checked_included": included, "cabin_bag_included": carry_on,
                "personal_item_included": True, "seat_selection": "paid",
                "changes": "unknown", "refundable": False,
                "earns_redeemable_miles": False, "boarding_group": None},
            "checked_bag_fee_tiers": tiers,
            "segment_ids": list(seg_ids),
        }
        layovers = []
        for i in range(len(segments) - 1):
            a_to = segments[i]["destination"]["iata"]
            ap = enr["airports"]["airports"].get(a_to, {})
            mct = (ap.get("mct_minutes") or {}).get("default")
            layovers.append({
                "layover_id": "lay%d" % (i + 1), "airport": a_to,
                "arrive_segment_id": seg_ids[i], "depart_segment_id": seg_ids[i + 1],
                "immigration_required": crosses_border(enr, segments[i]["origin"]["iata"], a_to, geo),
                "security_reclear_required": False, "ees_first_registration": False,
                "inter_terminal": ap.get("inter_terminal") or {"mode": "walk", "minutes": 20},
                "published_mct_minutes": mct,
                "mct_source": "curated airport table" if mct else "not curated",
                "bags_checked_through": True, "forced_landside": False,
                "leave_airport_viable": bool(ap.get("leave_airport_viable")),
                "services_open": ap.get("services") or [],
                "recovery": {"protected": True, "next_departure_minutes": 180,
                             "overnight_implied": False, "walkup_fare_cents": 0},
                "note": "One airline on one ticket, as Google Flights lists it: the carrier "
                        "owes the reconnection and the bags are checked through.",
            })
        dep_clock = _hhmm(segments[0]["departure_local"][11:16])
        arr_clock = _hhmm(segments[-1]["arrival_local"][11:16])
        out_modes, in_modes, ginfo = ground_both_ends(
            origin_key, segments[0]["origin"]["iata"], segments[-1]["destination"]["iata"],
            dep_clock, arr_clock, enr, geo, dest_point=dest_point)
        ground_info = ginfo
        if ginfo.get("arrival_note") and ginfo["arrival_note"] not in notes:
            notes.append(ginfo["arrival_note"])
        oid = "google-%s%s-%s" % (issuing.lower(), segments[0]["marketing"]["number"],
                                  segments[0]["departure_local"][11:16].replace(":", ""))
        k = 2
        while oid in seen_ids:
            oid = "%s-%d" % (oid.rsplit("-", 1)[0] if k > 2 else oid, k)
            k += 1
        seen_ids.add(oid)
        ce = it.get("carbon_emissions") or {}
        opt = {
            "option_id": oid,
            # "Ryanair FR8214", the feed's own shape: data.carrier_name strips the flight off it, which a
            # spaced "FR 8214" slipped past and the page printed "Ryanair FR 8214 FR8214" (2026-09-24)
            "display_name": "%s %s%s" % (airline_name, issuing, _serp_number(flights[0].get("flight_number"))),
            "segments": segments,
            "layovers": layovers,
            "tickets": [ticket],
            "ground": {"outbound": out_modes, "arrival": in_modes},
            "airport_process_minutes": _airport_block(enr, segments, geo),
            "arrival_process_minutes": _arrival_block(enr, segments, checked_bags, geo),
            "booking": [{"who": airline_name, "price_cents": total_cents, "direct": True,
                         "note": "Price from Google Flights. Book on %s's own site."
                                 % airline_name}],
            "_google": {"source": "Google Flights", "airline_logo": it.get("airline_logo"),
                        "total_duration": it.get("total_duration"),
                        "emissions_kg": (int(ce["this_flight"]) // 1000) if ce.get("this_flight") else None,
                        "typical_kg": (int(ce["typical_for_this_route"]) // 1000)
                                      if ce.get("typical_for_this_route") else None,
                        "booking_token": it.get("booking_token"),
                        "legs": [{"flight": f.get("flight_number"), "airline": f.get("airline"),
                                  "airplane": f.get("airplane"), "travel_class": f.get("travel_class"),
                                  "legroom": f.get("legroom"), "extensions": list(f.get("extensions") or []),
                                  "minutes": f.get("duration"),
                                  "from": {"code": (f.get("departure_airport") or {}).get("id"),
                                           "name": (f.get("departure_airport") or {}).get("name")},
                                  "to": {"code": (f.get("arrival_airport") or {}).get("id"),
                                         "name": (f.get("arrival_airport") or {}).get("name")},
                                  "often_delayed": bool(f.get("often_delayed_by_over_30_min"))}
                                 for f in flights]},
        }
        rating = _carrier_rating(enr, issuing, segments, geo)
        if rating:
            opt["carrier_rating"] = rating
        options.append(opt)

    if not options:
        return {"error": "nothing could be normalised from Google Flights", "dropped": dropped,
                "notes": notes}

    if len(bag_brands) <= 3:
        for c, b in sorted(bag_brands.items()):
            notes.append("%s comes from Google Flights through SerpApi, not from the airline's own "
                         "inventory: the lowest fare is shown with no brand, so it is priced as %s "
                         "with no bag (unknown is never cheap); no operating carrier is named; the "
                         "aircraft is a name, not a code, so cabin claims abstain; booking is on the "
                         "airline's site" % (c, b))
    else:
        # every airline Google shows: one sentence, not one per airline
        notes.append("%d airlines' fares come from Google Flights through SerpApi (%s), not from the airlines' "
                     "own inventory: each is priced as that airline's no-bag fare (unknown is never cheap), "
                     "no operating carrier or aircraft code is named, and booking is on the airline's site"
                     % (len(bag_brands), ", ".join(sorted(bag_brands))))
    if derived_n:
        notes.append("%d airport time zone%s worked out from the flight's own duration" % (derived_n, "" if derived_n == 1 else "s"))
    first = options[0]
    o_iata = first["segments"][0]["origin"]["iata"]
    d_iata = first["segments"][-1]["destination"]["iata"]
    dest_label = (geo.get(d_iata) or {}).get("city") or d_iata
    par, par_basis = route_par(o_iata, d_iata, first["segments"][0]["departure_local"][:10],
                               enr, notes, geo)
    org = enr["ground"]["origins"].get(origin_key, {})
    bags = [{"kind": "checked", "weight_kg": 20} for _ in range(max(0, int(checked_bags)))]
    return {
        "schema_version": "0.1.0",
        "fixture_id": "live-google-%s-%s" % (o_iata.lower(), d_iata.lower()),
        "title": "Google Flights via SerpApi (%s): %s to %s" % ("/".join(sorted(want)) or "all", o_iata, d_iata),
        "pins_down": "Nothing. This is a live search, not a fixture - it pins no "
                     "behaviour and must never be committed to fixtures/.",
        "as_of": first["segments"][0]["departure_local"][:10],
        "notes": notes,
        "query": {
            "origin": {"label": org.get("label", origin_key), "lat": org.get("lat", 0.0),
                       "lon": org.get("lon", 0.0), "geocode_precision": "neighbourhood"},
            "destination": ({"label": dest_point["label"], "lat": dest_point["lat"], "lon": dest_point["lon"],
                             "geocode_precision": "address"} if dest_point and dest_point.get("lat") is not None
                            else {"label": dest_label, "lat": 0.0, "lon": 0.0, "geocode_precision": "city"}),
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
        "_feed": {"provider": "serpapi", "carriers": sorted(want), "default_bag_fees_used": default_used,
                  "unreviewed_claims": 0, "feed_priced_bags": 0,
                  "amenity_segments": amenity_segments, "wifi_published": wifi_published},
    }


def price_signal(payload: Optional[Dict[str, Any]], lowest_now_cents: Optional[int]) -> Optional[Dict[str, Any]]:
    """Google's price context for the route, as SerpApi hands it back beside
    the flights: the typical range when it has one, and the recent history of
    the lowest fare. Against today's cheapest ticket from OUR results it makes
    one deterministic call, book / typical / wait, with the reason in a line.
    A signal, not a forecast: it says where today sits against the recent past."""
    ins = (payload or {}).get("price_insights") if isinstance(payload, dict) else None
    if not isinstance(ins, dict):
        return None
    hist = []
    for row in ins.get("price_history") or []:
        try:
            ts, price = int(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0:
            hist.append([ts, int(round(price * 100))])
    hist.sort()
    rng = ins.get("typical_price_range")
    low = high = None
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            low, high = int(round(float(rng[0]) * 100)), int(round(float(rng[1]) * 100))
        except (TypeError, ValueError):
            low = high = None
    vals = sorted(v for _, v in hist)
    median = vals[len(vals) // 2] if vals else None
    recent_low = min(v for _, v in hist[-30:]) if hist else None
    now = int(lowest_now_cents) if lowest_now_cents else None
    verdict, reason = None, ""                   # no range and no history: no verdict, and the page shows none
    if now is not None and (low is not None or median is not None):
        if low is not None and high is not None:
            if now <= low:
                verdict, reason = "book", "low for this route, which usually runs $%d to $%d" % (low // 100, high // 100)
            elif now > high:
                verdict, reason = "wait", "high for this route, which usually runs $%d to $%d" % (low // 100, high // 100)
            else:
                verdict, reason = "typical", "normal for this route, which usually runs $%d to $%d" % (low // 100, high // 100)
        elif median is not None:
            if now <= median * 0.9:
                verdict, reason = "book", "today's cheapest is %d%% under the recent median of $%d" % (round((1 - now / median) * 100), median // 100)
            elif now >= median * 1.2:
                verdict, reason = "wait", "today's cheapest is %d%% over the recent median of $%d" % (round((now / median - 1) * 100), median // 100)
            else:
                verdict, reason = "typical", "today's cheapest is near the recent median of $%d" % (median // 100)
    return {"level": ins.get("price_level"), "low_cents": low, "high_cents": high, "median_cents": median,
            "recent_low_cents": recent_low, "lowest_now_cents": now, "history": hist[-60:],
            "verdict": verdict, "reason": reason, "source": "Google Flights"}


def _itin_key(o: Dict[str, Any]) -> tuple:
    """The same flights, whichever source sold them: carrier, number and local departure minute per segment."""
    return tuple((s["marketing"]["carrier"], int(s["marketing"].get("number") or 0), str(s.get("departure_local") or "")[:16])
                 for s in o.get("segments") or [])


def _ticket_cents(o: Dict[str, Any]) -> int:
    total = 0
    for t in o.get("tickets") or []:
        p = t["price"]
        total += int(p.get("base_cents") or 0) + sum(int(c.get("amount_cents") or 0)
                                                     for b in ("taxes", "carrier_imposed", "agency_fees") for c in p.get(b) or [])
    return total


def merge_scenarios(main: Dict[str, Any], extra: Dict[str, Any], label: str) -> Dict[str, Any]:
    """Add a supplement's options to the main scenario: the main one's query,
    par and profiles stand (one par per route, whatever the source), ids that
    collide are suffixed, and the supplement's notes, drops and published
    amenities ride along under its own name.

    A supplement option for flights the main feed already sells at the same
    price or less is left out (the feed's is the richer record: brand, bags,
    aircraft). One that is cheaper stays, and the page shows the flight once,
    at its best-ranked fare (2026-09-24: Google carried cheaper fare brands of
    flights the feed returned only at a dearer one)."""
    if not extra or extra.get("error") or not extra.get("options"):
        return main
    ids = {o["option_id"] for o in main["options"]}
    have: Dict[tuple, int] = {}
    for o in main["options"]:
        k = _itin_key(o)
        have[k] = min(have.get(k, 1 << 62), _ticket_cents(o))
    kept = []
    for o in extra["options"]:
        if have.get(_itin_key(o), 1 << 62) <= _ticket_cents(o):
            continue
        kept.append(o)
    extra = dict(extra, options=kept)
    if not kept:
        return main
    for o in extra["options"]:
        oid = o["option_id"]
        while oid in ids:
            oid += "-x"
        o["option_id"] = oid
        ids.add(oid)
        main["options"].append(o)
    main["notes"] = list(main.get("notes") or []) + [n for n in (extra.get("notes") or [])
                                                     if n not in (main.get("notes") or [])]
    main["_dropped"] = list(main.get("_dropped") or []) + list(extra.get("_dropped") or [])
    feed = main.setdefault("_feed", {})
    feed["wifi_published"] = list(feed.get("wifi_published") or []) + list(
        (extra.get("_feed") or {}).get("wifi_published") or [])
    feed.setdefault("supplements", []).append({
        "label": label, "provider": (extra.get("_feed") or {}).get("provider"),
        "carriers": (extra.get("_feed") or {}).get("carriers"), "options": len(extra["options"]),
        "dropped": len(extra.get("_dropped") or [])})
    return main


def join_ontime(sc: Dict[str, Any]) -> Dict[str, Any]:
    """Each flight's US DOT on-time record where DOT has one (ontime.py; US airlines within the US only), in place of
    the abstain every feed leaves (2026-09-24, per Patrick): the grade's reliability part and the missed-connection
    risk then read a real record. A flight DOT does not cover keeps its abstain, which is priced, never free."""
    if not isinstance(sc, dict) or "error" in sc:
        return sc
    import ontime                                     # noqa: E402  (a local table, no network)
    try:
        import points                                 # noqa: E402  (which regional airlines fly for whom)
        reg = points.load().get("regional") or {}
    except Exception:
        reg = {}
    for o in sc.get("options") or []:
        for sg in o.get("segments") or []:
            if isinstance(sg.get("reliability"), dict) and sg["reliability"].get("coverage") != "none":
                continue
            mk = (sg.get("marketing") or {}).get("carrier")
            op = (sg.get("operating") or {}).get("carrier") or mk
            num = (sg.get("marketing") or {}).get("number")
            rec = ontime.claim(op, mk, num, (sg.get("origin") or {}).get("iata"), (sg.get("destination") or {}).get("iata"), reg)
            if rec:
                sg["reliability"] = rec
    return sc


def from_feed(raw: Dict[str, Any], **kw) -> Dict[str, Any]:
    return join_ontime(_from_feed(raw, **kw))


def _from_feed(raw: Dict[str, Any], **kw) -> Dict[str, Any]:
    """Dispatch on the payload's own shape rather than on a caller-supplied
    name, so a profile pointed at the wrong provider fails as a parse error
    here instead of as a plausible-looking scenario built from the wrong keys.

    Duffel is checked before Amadeus because both use a top-level `data`, and a
    Duffel offer is recognised by `slices` where an Amadeus one carries
    `type: "flight-offer"` - neither key appears in the other's payload."""
    if isinstance(raw.get("best_flights"), list) or isinstance(raw.get("other_flights"), list):
        return from_serpapi(raw, **kw)               # Google Flights through SerpApi: the supplement
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
