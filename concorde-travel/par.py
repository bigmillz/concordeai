#!/usr/bin/env python3
"""Par for any route on Earth: what a clean, unremarkable trip costs, all in.

    python3 concorde-travel/par.py JFK LHR 2026-11-18
    python3 concorde-travel/par.py 40.64,-73.78,US 35.77,140.39,JP 2027-07-04

The grade is `effective_cost / par`. Par used to be a curated number for eight
routes and $1,050 for everywhere else - a figure with nothing behind it, which
put 57% of a real search at A+. This replaces the guess with a model, so that a
route nobody has curated still gets a par with a story.

WHAT PAR IS, PRECISELY. Not the cheapest fare on the route, not the average of
what came back, not a percentile of anything. It is the effective cost of a
DEFINED REFERENCE ITINERARY - nonstop, main cabin with one checked bag, a
carrier at the curated baseline rating, the 31-inch transatlantic pitch norm,
leaving from the origin city at the time of day this market actually flies -
scored by the same scorer at the same reference profile as every real option.
One function, called on a hypothetical flight instead of a real one.

THE ONE THING IT MUST NEVER DO is look at the search results. A par derived from
the offers makes every grade relative: the same flight earns a B on Tuesday and
a D on Wednesday because the competition changed. par_for() takes an origin, a
destination and a date and NOTHING ELSE, and test_scorer.py guards the property.

THE REFERENCE FARE. The only genuinely modelled input is what a normal main-cabin
ticket costs, and that is route economics: it scales with distance but
sub-linearly (yield per mile falls with stage length, as it does in every
airline's own accounts), it depends on which MARKET the route sits in
(intra-Europe is an LCC price war, intra-Africa is not), and it swings with the
season. The per-mile yields below are calibrated against what these markets
actually charge - US domestic against DOT DB1B averages, transatlantic and
transpacific against published economy fares - and each is checked in
test_scorer.py against a route with a known price. They are a specification, so
they are here in code with their reasoning, not in a fixture.

A CURATED ROUTE PAR STILL WINS. enrichment/ground.json routes are a human's
number for a route somebody has actually studied; this fills in everywhere else.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

# ----------------------------------------------------------------- regions

# Country -> region. Coarse on purpose: what matters for fares is which
# competitive market a route sits in, and seven regions capture that.
_REGION = {}
for _r, _cc in (
    ("NA", "US CA"),
    ("MX", "MX"),                                        # priced like NA-Latin, not NA
    ("EU", "GB IE FR DE NL BE LU AT CH ES PT IT GR DK SE NO FI IS PL CZ SK HU RO "
           "BG HR SI EE LV LT MT CY UA RS BA ME MK AL XK MD BY"),
    ("ME", "AE QA SA KW BH OM JO IL LB TR EG IQ IR"),
    ("AF", "MA DZ TN LY SD ET KE TZ UG RW NG GH SN CI CM ZA ZW ZM MZ AO NA BW MU "
           "SC ER DJ SO ML BF NE TD CD CG GA GN SL LR GM TG BJ MW MG"),
    ("SA", "BR AR CL PE CO EC UY PY BO VE GY SR PA CR GT HN SV NI BZ CU DO JM TT "
           "BS BB HT PR"),
    ("AS", "JP KR CN TW HK MO SG MY TH VN PH ID IN PK BD LK NP MM KH LA BN MN KZ "
           "UZ KG TJ TM AF MV BT"),
    ("OC", "AU NZ FJ PG WS TO VU NC PF"),
):
    for _c in _cc.split():
        _REGION[_c] = _r


def region_of(country: Optional[str]) -> str:
    return _REGION.get((country or "").upper(), "XX")


def market_class(o_country: str, d_country: str) -> str:
    """The competitive market a route sits in. Same country beats same region."""
    oc, dc = (o_country or "").upper(), (d_country or "").upper()
    ro, rd = region_of(oc), region_of(dc)
    if oc and oc == dc:
        return "domestic-" + ("na" if ro == "NA" else "eu" if ro == "EU" else "other")
    if ro == rd:
        return "intra-" + ro.lower()
    pair = "-".join(sorted((ro, rd)))
    return {
        "EU-NA": "transatlantic", "AS-NA": "transpacific", "NA-OC": "transpacific",
        "MX-NA": "na-mexico", "NA-SA": "na-latam", "MX-SA": "intra-sa",
        "EU-ME": "eu-mideast", "AS-EU": "eu-asia", "EU-OC": "eu-asia",
        "AF-EU": "eu-africa", "AS-ME": "mideast-asia", "AF-ME": "mideast-africa",
        "AS-OC": "asia-oceania", "EU-SA": "eu-latam", "AF-NA": "na-africa",
        "ME-NA": "na-mideast", "AF-AS": "asia-africa", "AS-SA": "long-thin",
        "OC-SA": "long-thin", "AF-SA": "long-thin", "AF-OC": "long-thin",
        "ME-OC": "mideast-asia", "ME-SA": "long-thin", "MX-EU": "transatlantic",
        "AS-MX": "transpacific", "MX-OC": "transpacific",
    }.get(pair, "other")


# ------------------------------------------------------------- the fare model

# (base cents, yield cents/mile over the first 600 mi, over the next 1400, beyond).
# Marginal bands, so the fare is continuous in distance and a ledger line can
# say exactly where each dollar came from. One-way, main cabin, a bag included,
# booked about six weeks out, in the shoulder season - the seasonality table
# moves it from there.
YIELDS = {
    "domestic-na":   (4500, 16, 10,  7),
    "domestic-eu":   (3500, 12,  9,  8),
    "domestic-other":(4000, 16, 11,  9),
    "intra-eu":      (3500, 12,  9,  8),
    "intra-as":      (3500, 13, 10,  8.5),
    "intra-na":      (4500, 16, 10,  7),
    "intra-sa":      (5000, 20, 15, 13),
    "intra-af":      (6000, 28, 20, 17),
    "intra-me":      (5000, 18, 13, 11),
    "intra-oc":      (5000, 20, 13, 10),
    "transatlantic": (9000, 14, 13, 11),
    "transpacific":  (11000, 14, 13, 11.5),
    "na-mexico":     (6000, 18, 12, 10),
    "na-latam":      (9000, 15, 14, 12),
    "eu-mideast":    (7000, 14, 13, 11),
    "eu-asia":       (10000, 14, 13, 10.5),
    "eu-africa":     (8000, 15, 15, 13),
    "eu-latam":      (10000, 14, 14, 12),
    "mideast-asia":  (7000, 14, 12, 10.5),
    "mideast-africa":(7000, 15, 14, 12),
    "asia-oceania":  (9000, 14, 13, 11),
    "asia-africa":   (9000, 15, 14, 12.5),
    "na-africa":     (11000, 15, 15, 13),
    "na-mideast":    (10000, 14, 13, 11.5),
    "long-thin":     (12000, 16, 15, 13.5),
    "other":         (7000, 18, 13, 11),
}

# Month multipliers. Northern-hemisphere summer peak, holiday bumps, a
# January-February trough. Markets whose demand is driven by the southern
# summer are flipped six months.
SEASON = {
    "transatlantic": (0.82, 0.85, 0.92, 1.00, 1.08, 1.35, 1.45, 1.40, 1.10, 0.98, 0.88, 1.15),
    "domestic-na":   (0.88, 0.90, 1.05, 1.00, 1.00, 1.12, 1.15, 1.05, 0.92, 0.95, 1.05, 1.15),
    "intra-eu":      (0.80, 0.82, 0.90, 1.05, 1.05, 1.20, 1.35, 1.35, 1.05, 0.95, 0.85, 1.05),
    "transpacific":  (0.90, 0.92, 1.00, 1.02, 1.00, 1.15, 1.30, 1.25, 1.00, 0.98, 0.92, 1.20),
    "default":       (0.85, 0.87, 0.95, 1.00, 1.02, 1.18, 1.28, 1.25, 1.02, 0.97, 0.90, 1.12),
}
_SOUTHERN = {"intra-oc", "intra-sa", "asia-oceania", "intra-af"}


def season_factor(market: str, month: int, southern: bool = False) -> float:
    row = SEASON.get(market) or SEASON.get(market.split("-")[0] + "-" + market.split("-")[-1]) \
        or SEASON["default"]
    m = (month - 1) % 12
    if southern:
        m = (m + 6) % 12
    return row[m]


# Holiday weeks. A month multiplier cannot know that the Wednesday before
# Thanksgiving is the dearest domestic day of the year, so a whole search on
# it came back F against a par built for an ordinary November (2026-09-21).
# Par is a specification of a fair fare ON THAT DATE, and a fair fare in a
# holiday week is higher; so the factor belongs in par, not in the grade.
# Windows are inclusive; the factor is the largest window that covers the day.
def _easter(year: int):
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
    import datetime
    return datetime.date(year, month, day)


def holiday_factor(market: str, date: str, countries=()) -> Tuple[float, Optional[str]]:
    """(factor, why). US holidays touch any market with a US end; Christmas
    and New Year touch every market; Easter the European ones."""
    import datetime
    try:
        d = datetime.date(int(date[0:4]), int(date[5:7]), int(date[8:10]))
    except (TypeError, ValueError):
        return 1.0, None
    cs = {(c or "").upper() for c in countries}
    us = "US" in cs or market in ("domestic-na", "intra-na")
    eu = market in ("intra-eu", "domestic-eu") or bool(cs & {"GB", "IE", "FR", "DE", "ES", "IT", "NL", "BE", "PT", "AT", "CH", "PL", "SE", "DK", "NO", "FI"})
    best = (1.0, None)
    def window(start, end, factor, why):
        nonlocal best
        if start <= d <= end and factor > best[0]:
            best = (factor, why)
    y = d.year
    # Christmas and New Year, everywhere
    window(datetime.date(y, 12, 18), datetime.date(y, 12, 31), 1.35, "Christmas week")
    window(datetime.date(y, 1, 1), datetime.date(y, 1, 4), 1.25, "New Year")
    if us:
        nov1 = datetime.date(y, 11, 1); thanks = nov1 + datetime.timedelta(days=(3 - nov1.weekday()) % 7 + 21)
        window(thanks - datetime.timedelta(days=2), thanks + datetime.timedelta(days=4), 1.55, "Thanksgiving week")
        window(thanks - datetime.timedelta(days=1), thanks - datetime.timedelta(days=1), 1.75, "Thanksgiving Eve")
        window(thanks + datetime.timedelta(days=3), thanks + datetime.timedelta(days=3), 1.75, "Thanksgiving Sunday")
        j4 = datetime.date(y, 7, 4); window(j4 - datetime.timedelta(days=3), j4 + datetime.timedelta(days=2), 1.2, "Fourth of July")
        may31 = datetime.date(y, 5, 31); mem = may31 - datetime.timedelta(days=may31.weekday()); window(mem - datetime.timedelta(days=3), mem, 1.2, "Memorial Day weekend")
        sep1 = datetime.date(y, 9, 1); lab = sep1 + datetime.timedelta(days=(0 - sep1.weekday()) % 7); window(lab - datetime.timedelta(days=3), lab, 1.2, "Labor Day weekend")
    if eu:
        e = _easter(y); window(e - datetime.timedelta(days=3), e + datetime.timedelta(days=1), 1.25, "Easter weekend")
    return best


def haversine_mi(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def reference_fare_cents(o: Dict[str, Any], d: Dict[str, Any], date: str
                         ) -> Tuple[int, Dict[str, Any]]:
    """(cents, basis). `o` and `d` need lat, lon, country. `date` is YYYY-MM-DD."""
    miles = haversine_mi(o["lat"], o["lon"], d["lat"], d["lon"])
    market = market_class(o.get("country"), d.get("country"))
    base, y1, y2, y3 = YIELDS.get(market, YIELDS["other"])
    b1 = min(miles, 600.0)
    b2 = min(max(miles - 600.0, 0.0), 1400.0)
    b3 = max(miles - 2000.0, 0.0)
    shoulder = base + b1 * y1 + b2 * y2 + b3 * y3
    try:
        month = int(date[5:7])
    except (TypeError, ValueError):
        month = 4
    southern = market in _SOUTHERN or (o["lat"] < 0 and d["lat"] < 0)
    sf = season_factor(market, month, southern)
    hf, holiday = holiday_factor(market, date, (o.get("country"), d.get("country")))
    fare = int(round(shoulder * sf * hf))
    return fare, {
        "miles": round(miles), "market": market, "month": month,
        "base_cents": base,
        "bands": [{"miles": round(b1), "cents_per_mile": y1},
                  {"miles": round(b2), "cents_per_mile": y2},
                  {"miles": round(b3), "cents_per_mile": y3}],
        "shoulder_cents": int(round(shoulder)),
        "season_factor": sf, "southern_hemisphere": southern,
        "holiday_factor": hf, "holiday": holiday,
        "fare_cents": fare,
        "reads_as": "%d mi, %s market, x%.2f for month %d%s -> $%d main cabin with a bag"
                    % (round(miles), market, sf, month, (", x%.2f for %s" % (hf, holiday)) if holiday else "", fare // 100),
    }


# ------------------------------------------------------- the reference trip

# The longest scheduled nonstop is about 9,500 mi (SIN-JFK). Past this a route
# has no nonstop to be measured against, and the reference allows a connection.
NONSTOP_RANGE_MILES = 8800
CONNECTION_MINUTES = 150


def _block_minutes(miles: float) -> int:
    """Nonstop block time: cruise plus taxi, climb and descent. 520 mph is the
    long-haul average once the ends are counted; the constant is the ends."""
    return int(round(miles / 520.0 * 60)) + 45


def reference_departure_minutes(o: Dict[str, Any], d: Dict[str, Any], miles: float) -> int:
    """When a clean trip on this route leaves, as minutes after local midnight.

    Chosen by the ROUTE'S STRUCTURE, never by what the search returned. An
    eastbound long-haul across an ocean is an overnight - that is how the
    market flies, and a reference that pretends otherwise would set a bar no
    real flight on the route can reach. Everything shorter or westbound leaves
    mid-morning, which is the cleanest a day can be."""
    east = (d["lon"] - o["lon"]) % 360
    long_haul_east = miles > 2500 and 0 < east < 180
    return 19 * 60 + 30 if long_haul_east else 10 * 60


def par_for(o: Dict[str, Any], d: Dict[str, Any], date: str,
            enr: Dict[str, Any], scorer_mod, ground_mod
            ) -> Tuple[int, Dict[str, Any]]:
    """The par for a route, in cents, with its full basis.

    Builds the reference itinerary and scores it through the real scorer at the
    real reference profile, from an estimated city-centre origin. Nothing here
    reads a search result. `scorer_mod` and `ground_mod` are passed in rather
    than imported so this module has no import-order dependency on either and
    the scorer's tests can call it without the adapter."""
    fare, fbasis = reference_fare_cents(o, d, date)
    miles = fbasis["miles"]
    dep_min = reference_departure_minutes(o, d, miles)
    block = _block_minutes(miles)

    o_iata, d_iata = o.get("iata", "ORG"), d.get("iata", "DST")
    aps = enr["airports"]["airports"]
    o_ap, d_ap = aps.get(o_iata, {}), aps.get(d_iata, {})

    # Local timestamps carrying an explicit offset, as the scorer requires. The
    # offsets come from the same join the adapters use.
    o_off = _offset(o, o_iata, date, enr)
    d_off = _offset(d, d_iata, date, enr)
    dep_local = "%sT%02d:%02d:00%s" % (date, dep_min // 60, dep_min % 60, o_off)
    # Arrival: departure instant plus block, re-expressed in the destination's offset.
    dep_utc = _to_utc_minutes(date, dep_min, o_off)
    arr_utc = dep_utc + block
    arr_local = _from_utc_minutes(arr_utc, d_off)

    city_o = {"lat": o["lat"] + 0.11, "lon": o["lon"] + 0.11}
    out_modes = ground_mod.estimate_modes(city_o, o, dep_min)
    city_d = {"lat": d["lat"] + 0.11, "lon": d["lon"] + 0.11}
    in_modes = ground_mod.estimate_modes(city_d, d, int(arr_local[11:13]) * 60 + int(arr_local[14:16]))

    option = {
        "option_id": "par-reference",
        "display_name": "reference itinerary",
        "tickets": [{
            "ticket_id": "t1", "issuing_carrier": "XX", "fare_brand_name": "Main",
            "price": {"currency": "USD", "base_cents": fare, "fx_rate_to_usd": 1.0,
                      "taxes": [], "carrier_imposed": [], "agency_fees": []},
            "entitlements": {"checked_included": 1, "cabin_bag_included": True,
                             "personal_item_included": True, "seat_selection": "paid",
                             "changes": "fee", "refundable": False,
                             "earns_redeemable_miles": True, "boarding_group": None},
            "checked_bag_fee_tiers": [{"piece": 1, "amount_cents": 10000},
                                      {"piece": 2, "amount_cents": 12000},
                                      {"piece": 3, "amount_cents": 25000}],
            "segment_ids": ["s1"]}],
        "segments": [{
            "segment_id": "s1",
            "marketing": {"carrier": "XX", "number": 1},
            "operating": {"carrier": "XX", "number": 1},
            "origin": {"iata": o_iata, "iata_area": o_ap.get("iata_area", 1),
                       "schengen": o_ap.get("schengen")},
            "destination": {"iata": d_iata, "iata_area": d_ap.get("iata_area", 2),
                            "schengen": d_ap.get("schengen")},
            "departure_local": dep_local, "arrival_local": arr_local,
            "tz_hint": {"departure": o_ap.get("zone", ""), "arrival": d_ap.get("zone", "")},
            "equipment_code": "UNKNOWN", "cabin_marketed": "economy",
            "claims": {"seat_pitch_inches": 31},
            # A SPECIFIED on-time record, not an abstain. The scorer charges an
            # unknown record (unknown is never free) and that is right for a
            # real flight nobody has data on - but the reference is a
            # definition, and a definition that says "and we do not know how
            # punctual it is" adds $30 to every par on Earth for no reason.
            # 78% is the long-run US DOT system average; the window and source
            # say what it is.
            "reliability": {"on_time_fraction": 0.78, "delay_minutes_p50": 10,
                            "delay_minutes_p90": 60, "sample_size": 60,
                            "observation_window": "specification",
                            "source": "reference itinerary: long-run system average"}}],
        "layovers": [],
        "ground": {"outbound": out_modes, "arrival": in_modes},
        "airport_process_minutes": o_ap.get("process_minutes") or {"p50": 60},
        "booking": [{"who": "reference", "price_cents": fare, "direct": True, "note": ""}],
    }
    scenario = {
        "query": {
            "origin": {"label": "City center", "lat": city_o["lat"], "lon": city_o["lon"],
                       "geocode_precision": "city"},
            "destination": {"label": d.get("city") or d_iata, "lat": 0.0, "lon": 0.0,
                            "geocode_precision": "city"},
            "depart_date": date, "return_date": None,
            "route_par_cents": 1,               # unused: we are computing it
            "party": [{"passenger_id": "p1", "type": "adult",
                       "bags": [{"kind": "checked", "weight_kg": 20},
                                {"kind": "cabin"}, {"kind": "personal_item"}]}],
            "profiles": {"reference": {"label": "neutral reference",
                                       "hourly_value_cents": 3500,
                                       "comfort_weight": 1.0, "risk_weight": 1.0}},
        },
        "options": [option],
    }
    led = scorer_mod.score(scenario, option, "reference")
    par = int(led.effective_cents)
    lines = [{"label": l.label, "cents": l.amount_cents} for l in led.lines]
    if miles > NONSTOP_RANGE_MILES:
        # Nothing flies this far without stopping, so a nonstop reference sets
        # a bar no real flight can reach and the whole route grades D. The
        # allowance is what a clean connection costs at the reference profile:
        # the layover time, and the nonstop credit given back.
        allowance = (CONNECTION_MINUTES * 3500) // 60 + scorer_mod.DEFAULT.nonstop_credit_cents
        par += allowance
        lines.append({"label": "One connection allowed: too far for a nonstop",
                      "cents": allowance})
    basis = dict(fbasis)
    basis.update({
        "reference": {
            "shape": "nonstop, main cabin with one checked bag, a carrier at the "
                     "baseline rating, 31-inch pitch, from the city centre",
            "departs_local": dep_local[11:16], "arrives_local": arr_local[11:16],
            "block_minutes": block,
            "door_to_door_minutes": led.door_to_door_minutes,
            "ground_out": out_modes[0]["mode"] if out_modes else None,
        },
        "ledger": lines,
        "par_cents": par,
        "source": "modelled",
    })
    return par, basis


# ---------------------------------------------------------------- time bits

def _offset(place: Dict[str, Any], iata: str, date: str, enr: Dict[str, Any]) -> str:
    ap = enr["airports"]["airports"].get(iata)
    if ap:
        zone = enr["airports"]["zones"].get(ap["zone"])
        if zone:
            from adapter import offset_for
            off = offset_for(zone, date)
            if off:
                return off
    tz = place.get("tz")
    if tz:
        from adapter import _offset_from_tzdb
        off = _offset_from_tzdb(tz, date + "T12:00:00")
        if off:
            return off
    # Last resort: solar time from longitude, rounded to the hour. Wrong by an
    # hour in places with odd zones; never wrong by a day.
    h = int(round(place["lon"] / 15.0))
    return "%s%02d:00" % ("+" if h >= 0 else "-", abs(h))


def _to_utc_minutes(date: str, local_min: int, off: str) -> int:
    y, m, dd = int(date[:4]), int(date[5:7]), int(date[8:10])
    days = _days_from_civil(y, m, dd)
    sign = -1 if off[0] == "-" else 1
    om = sign * (int(off[1:3]) * 60 + int(off[4:6]))
    return days * 1440 + local_min - om


def _from_utc_minutes(utc_min: int, off: str) -> str:
    sign = -1 if off[0] == "-" else 1
    om = sign * (int(off[1:3]) * 60 + int(off[4:6]))
    local = utc_min + om
    days, mins = divmod(local, 1440)
    y, m, d = _civil_from_days(days)
    return "%04d-%02d-%02dT%02d:%02d:00%s" % (y, m, d, mins // 60, mins % 60, off)


def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z: int) -> Tuple[int, int, int]:
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (m <= 2), m, d


# A few uncurated airports for the command line only, so a sanity check on a
# known route does not need a feed to supply coordinates. The adapters get
# these from the feed; nothing in the product reads this table.
_CLI_AIRPORTS = {
    "LAX": (33.9425, -118.4081, "US"), "SFO": (37.6213, -122.3790, "US"),
    "ORD": (41.9742, -87.9073, "US"), "MIA": (25.7959, -80.2870, "US"),
    "DEN": (39.8561, -104.6737, "US"), "NRT": (35.7720, 140.3929, "JP"),
    "HND": (35.5494, 139.7798, "JP"), "SIN": (1.3644, 103.9915, "SG"),
    "SYD": (-33.9399, 151.1753, "AU"), "MEL": (-37.6690, 144.8410, "AU"),
    "DXB": (25.2532, 55.3657, "AE"), "DOH": (25.2731, 51.6081, "QA"),
    "GRU": (-23.4356, -46.4731, "BR"), "EZE": (-34.8222, -58.5358, "AR"),
    "BOG": (4.7016, -74.1469, "CO"), "MEX": (19.4363, -99.0721, "MX"),
    "CUN": (21.0365, -86.8771, "MX"), "YYZ": (43.6777, -79.6248, "CA"),
    "JNB": (-26.1367, 28.2411, "ZA"), "CPT": (-33.9715, 18.6021, "ZA"),
    "NBO": (-1.3192, 36.9278, "KE"), "DEL": (28.5562, 77.1000, "IN"),
    "BOM": (19.0896, 72.8656, "IN"), "HKG": (22.3080, 113.9185, "HK"),
    "ICN": (37.4602, 126.4407, "KR"), "BKK": (13.6900, 100.7501, "TH"),
    "ATH": (37.9364, 23.9445, "GR"), "TLV": (32.0055, 34.8854, "IL"),
    "LGW": (51.1537, -0.1821, "GB"), "EDI": (55.9500, -3.3725, "GB"),
    "BOS": (42.3656, -71.0096, "US"), "IAD": (38.9531, -77.4565, "US"),
    "ATL": (33.6407, -84.4277, "US"), "AUH": (24.4330, 54.6511, "AE"),
    "CMN": (33.3675, -7.5899, "MA"),
}


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import adapter
    import ground
    import scorer
    enr = adapter.load_enrichment()

    def place(text):
        aps = enr["airports"]["airports"]
        if text.upper() in aps:
            a = aps[text.upper()]
            return {"iata": text.upper(), "lat": a["lat"], "lon": a["lon"],
                    "country": a["country"], "city": a.get("city")}
        if text.upper() in _CLI_AIRPORTS:
            lat, lon, cc = _CLI_AIRPORTS[text.upper()]
            return {"iata": text.upper(), "lat": lat, "lon": lon, "country": cc}
        lat, lon, cc = text.split(",")
        return {"iata": "XXX", "lat": float(lat), "lon": float(lon), "country": cc.upper()}

    o = place(sys.argv[1] if len(sys.argv) > 1 else "JFK")
    d = place(sys.argv[2] if len(sys.argv) > 2 else "LHR")
    date = sys.argv[3] if len(sys.argv) > 3 else "2026-11-18"
    par, basis = par_for(o, d, date, enr, scorer, ground)
    print("fare   : " + basis["reads_as"])
    print("leaves : %s local, %dh%02dm block, lands %s"
          % (basis["reference"]["departs_local"], basis["reference"]["block_minutes"] // 60,
             basis["reference"]["block_minutes"] % 60, basis["reference"]["arrives_local"]))
    print("ledger :")
    for l in basis["ledger"]:
        print("   %-52s %s$%d" % (l["label"][:52], "-" if l["cents"] < 0 else "", abs(l["cents"]) // 100))
    print("PAR    : $%d" % (par // 100))
