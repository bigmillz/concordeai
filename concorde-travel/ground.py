#!/usr/bin/env python3
"""Ground access anywhere, estimated rather than curated.

    python3 concorde-travel/ground.py "Bushwick, Brooklyn" JFK 06:10
    python3 concorde-travel/ground.py 48.8566,2.3522 CDG 18:00

The curated table in enrichment/ground.json knows one neighbourhood properly.
This produces the same SHAPE for anywhere else, so an uncurated origin stops
dropping the itinerary and starts costing an honest guess.

WHY AN ESTIMATE IS THE RIGHT ANSWER HERE, not a placeholder for a better one.
A real fare quote would be wrong by the time anyone books - surge alone moves it
further than this model's error bars - and most people type a neighbourhood
rather than a street address, so there is no exact origin to quote from. The job
is not to price the ride. It is to stop the $100 surprise: a New Yorker who
assumes the airport is a $30 cab and finds out at 5am that it is not.

WHAT THIS DELIBERATELY DOES NOT DO. It does not pretend distance predicts cost.
Bushwick to JFK is $11.75 and Bushwick to EWR is about $92 across near-identical
straight-line distances, because the reason is NJ Transit's airport rail stop
closing at 01:00, not miles. No formula recovers that. So: the curated table
always wins where it exists, the estimate fills the rest, and every estimated
mode carries estimated=True so the ledger and coverage() can say which it was.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

# Rideshare pricing by country: (base, per mile, per minute, airport fee), all
# in cents. Five bands is all the resolution an estimate can honestly carry.
# Unlisted countries take the middle band.
#
# THESE LEAN HIGH ON PURPOSE. The whole job of this number is stopping the $100
# surprise - a New Yorker who assumed the airport was a $30 cab. An estimate
# that comes in UNDER the real fare manufactures exactly the surprise it exists
# to prevent, so the asymmetry is deliberate: being $15 pessimistic costs a user
# nothing, being $30 optimistic costs them their morning. Same principle as
# "unknown is never cheap" in the scorer.
#
# The airport fee is a real, separate thing: pickup and dropoff charges, and the
# tolls that usually sit on an airport approach. Rolling it into the per-mile
# rate makes short airport runs look far too cheap.
FARE_BANDS = {
    "high":   (350, 275, 45, 1200, "US CA CH NO DK IS AU NZ SE JP"),
    "upper":  (300, 205, 36,  800, "GB DE FR NL IE AT BE FI SG IL AE QA LU"),
    "mid":    (200, 130, 22,  400, "ES IT PT GR CZ PL HU HR KR TW CL UY EE LT LV SK SI"),
    "lower":  (120,  80, 14,  200, "MX BR AR TR TH MY ZA CN RU RO BG RS UA CO PE"),
    "low":    ( 80,  45,  9,  100, "IN ID VN PH EG PK BD NG KE MA LK NP KH"),
}
_BAND_BY_COUNTRY = {c: name for name, (_, _, _, _, cc) in FARE_BANDS.items()
                    for c in cc.split()}

# Countries where an airport rail or metro link is common enough that offering
# a transit option is honest. Elsewhere we say nothing rather than invent a bus.
TRANSIT_COUNTRIES = set(
    "GB DE FR NL IE AT BE CH DK SE NO FI ES IT PT GR CZ PL HU HR SK SI EE LT LV "
    "US CA JP KR TW SG HK CN TH MY AE QA IL TR MX BR CL AR AU NZ IN".split())

# Flat transit fare bands, airport link included, in cents.
TRANSIT_FARE = {"high": 1200, "upper": 900, "mid": 600, "lower": 300, "low": 150}

# Airport runs are mostly motorway, so the detour penalty is smaller than a
# general urban trip would suggest.
ROAD_FACTOR = 1.22

# HOW WELL DO WE ACTUALLY KNOW THIS PLACE. Three states, and the difference
# matters enough to show a user:
#
#   curated  - a human wrote the modes for this exact origin and airport. The
#              Bushwick-to-EWR number knows about NJ Transit's 01:00 cutoff. No
#              formula recovers that, which is why curated always wins.
#   modelled - the country is in FARE_BANDS, so the fare rates are at least
#              anchored to somewhere real. Expect to be within about a third.
#   assumed  - the country is in none of the bands and took the middle one by
#              default. That is a guess about an entire economy, and the user
#              should be told rather than shown a confident dollar figure.
#
# Rideshare fares are genuinely not purchasable at any price: Uber's estimate
# endpoints are partner-gated. Transit fares ARE gettable - GTFS-Fares v2 is a
# real standard and Navitia serves US and EU - so the transit side of this is
# the half worth replacing with real data first.
SUPPORT_NOTE = {
    "curated":  None,
    "modelled": "Ride prices here are estimates and could be off by up to a third",
    "assumed":  "No ride prices for this area yet. The car fare is a "
                "placeholder, so check it",
}

# Airport rail does not travel at a fraction of road speed - it has its own
# right of way and beats the car in traffic, which is the entire reason people
# take it. Deriving it from road speed said the Elizabeth line takes 148
# minutes to Heathrow, which is three times the truth and would have quietly
# made every transit option look unusable.
TRANSIT_KMH = 38.0
TRANSIT_FIXED_MIN = 22      # walk to the line, wait, transfer, walk at the far end
KM_PER_MILE = 1.60934

# Typed origin text (lower-cased) -> the point the server geocoded for it, so
# the ride starts at the traveler's door rather than a guess near the airport.
GEOCODED: Dict[str, Dict[str, Any]] = {}


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _band(country: Optional[str]) -> str:
    return _BAND_BY_COUNTRY.get((country or "").upper(), "mid")


def region_support(country: Optional[str]) -> str:
    """'modelled' if we have rates for this country, 'assumed' if it fell back."""
    return "modelled" if (country or "").upper() in _BAND_BY_COUNTRY else "assumed"


def _road_speed_kmh(km: float, clock_minutes: int) -> float:
    """Average door-to-door road speed. Longer runs get more motorway; rush
    hour costs more than distance does, which is the whole reason a 6am car and
    a 6pm car are different products."""
    base = 24.0 if km < 12 else 48.0 if km < 45 else 72.0
    h = (clock_minutes // 60) % 24
    if 7 <= h < 10 or 16 <= h < 19:
        base *= 0.70                      # rush
    elif h >= 22 or h < 5:
        base *= 1.25                      # empty roads
    return base


def estimate_modes(origin: Dict[str, Any], airport: Dict[str, Any],
                   clock_minutes: int) -> List[Dict[str, Any]]:
    """The same mode dicts the curated table produces, computed instead.

    `origin` needs lat/lon; `airport` needs lat/lon and iata_country_code."""
    km = haversine_km(origin["lat"], origin["lon"],
                      airport["lat"], airport["lon"]) * ROAD_FACTOR
    km = max(km, 3.0)                     # nobody is door-to-door in two minutes
    band = _band(airport.get("country"))
    support = region_support(airport.get("country"))
    base, per_mile, per_min, airport_fee, _ = FARE_BANDS[band]
    speed = _road_speed_kmh(km, clock_minutes)
    drive_min = max(12, int(round(km / speed * 60)))

    miles = km / KM_PER_MILE
    fare = int(base + per_mile * miles + per_min * drive_min + airport_fee)
    # Door to door is not drive time: there is finding the car, and the last
    # hundred metres at both ends. Like the fare, the time leans long - arriving
    # early costs a coffee, arriving late costs the flight.
    d2d = drive_min + 10

    modes = [{
        "mode_id": "est-rideshare",
        "mode": "Rideshare (estimated)",
        "mode_kind": "rideshare",
        "fare_cents": fare,
        "door_to_door_minutes": {"p50": d2d, "p90": int(d2d * 1.45)},
        "transfers": 0,
        "tolls_cents": {"outbound": 0, "inbound": 0},
        "hours": "any", "feasible": True, "infeasible_reason": None,
        "estimated": True,
        "support": support,
        "basis": ("roughly %d km by road in %s traffic - an estimate, and a "
                  "deliberately cautious one" % (round(km), _traffic_word(clock_minutes)))
                 if support == "modelled" else
                 ("roughly %d km by road, but we have no fare rates for this "
                  "country - this number is a placeholder" % round(km)),
    }]

    # 120 km, not 70: the airports FURTHEST out are the ones with a dedicated
    # rail link, because that is why the link was built. Capping at 70 removed
    # the Narita Express and the Arlanda Express - the two cases where telling
    # someone about the train is worth the most money.
    if (airport.get("country") or "").upper() in TRANSIT_COUNTRIES and km < 120:
        t_fare = TRANSIT_FARE[band]
        # Transit is slower and does not scale the same way: a fixed penalty for
        # getting to the line and waiting, then a slower run.
        t_min = int(round(km / TRANSIT_KMH * 60)) + TRANSIT_FIXED_MIN
        modes.append({
            "mode_id": "est-transit",
            "mode": "Public transport (estimated)",
            "mode_kind": "transit",
            "fare_cents": t_fare,
            "door_to_door_minutes": {"p50": t_min, "p90": int(t_min * 1.4)},
            "transfers": 1 if km < 25 else 2,
            "hours": "05:00-23:30",
            "feasible": True, "infeasible_reason": None,
            "estimated": True,
            "support": support,
            "basis": "an airport rail or metro link is usual here; fare and time "
                     "are a band, not a timetable",
        })
    return modes


def _traffic_word(clock_minutes: int) -> str:
    h = (clock_minutes // 60) % 24
    if 7 <= h < 10 or 16 <= h < 19:
        return "rush-hour"
    if h >= 22 or h < 5:
        return "overnight"
    return "off-peak"


# --------------------------------------------------------------- resolving

def resolve_origin(text: str, enr: Dict[str, Any],
                   fallback_airport: Optional[Dict[str, Any]] = None
                   ) -> Tuple[Dict[str, Any], str]:
    """Turn whatever the user typed into something with a lat and a lon.

    Four steps, best first, and the last one cannot fail - an origin we cannot
    place must still produce an answer, because dropping every flight is a worse
    outcome than a wide estimate that says it is wide."""
    origins = (enr.get("ground") or {}).get("origins") or {}
    q = (text or "").strip().lower()

    if q in origins:
        o = origins[q]
        return ({"key": q, "label": o.get("label", q), "lat": o["lat"], "lon": o["lon"],
                 "precision": "curated"}, "curated")

    # Match in BOTH directions. Someone types a full address - "Wyckoff Ave &
    # Myrtle Ave, Bushwick, Brooklyn 11237" - and the curated row is called
    # "Bushwick, Brooklyn, NY". Checking only whether the typed text sits inside
    # the label misses that completely, which meant the one neighbourhood we
    # have real numbers for silently fell through to an estimate.
    for key, o in origins.items():
        label = (o.get("label") or "").lower()
        hood = label.split(",")[0].strip()
        if not q:
            break
        if (q in label or label == q or hood == q
                or (hood and re.search(r"\b%s\b" % re.escape(hood), q))
                or re.search(r"\b%s\b" % re.escape(key.replace("-", " ")), q)):
            return ({"key": key, "label": o.get("label", key), "lat": o["lat"],
                     "lon": o["lon"], "precision": "curated"}, "curated")

    pt = GEOCODED.get(q)
    if pt and pt.get("lat") is not None:
        return ({"key": None, "label": text, "lat": pt["lat"], "lon": pt["lon"],
                 "precision": "address"}, "address")

    # "40.69,-73.91" straight through
    if "," in q:
        parts = q.split(",")
        try:
            lat, lon = float(parts[0]), float(parts[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return ({"key": None, "label": text, "lat": lat, "lon": lon,
                         "precision": "coordinates"}, "coordinates")
        except ValueError:
            pass

    if fallback_airport and fallback_airport.get("lat") is not None:
        # We know which airport they are leaving from, so we know roughly which
        # city they are in. A typical in-city origin is a more useful guess than
        # nothing, and it is labelled as the guess it is.
        return ({"key": None,
                 "label": text or ("near " + (fallback_airport.get("city") or
                                              fallback_airport.get("iata", "the airport"))),
                 "lat": fallback_airport["lat"] + 0.11,
                 "lon": fallback_airport["lon"] + 0.11,
                 "precision": "city-guess"}, "city-guess")

    return ({"key": None, "label": text or "unknown", "lat": None, "lon": None,
             "precision": "unknown"}, "unknown")


def modes_for(origin: Dict[str, Any], airport: Dict[str, Any], clock_minutes: int,
              enr: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """(modes, source). The curated table wins wherever it has an answer."""
    origins = (enr.get("ground") or {}).get("origins") or {}
    key = origin.get("key")
    if key and key in origins:
        curated = (origins[key].get("airports") or {}).get(airport["iata"])
        if curated:
            out = []
            for m in curated:
                m = dict(m)
                hours = m.pop("hours", "any")
                m["feasible"] = _within(hours, clock_minutes)
                if m["feasible"]:
                    m["infeasible_reason"] = None
                m["estimated"] = False
                m["support"] = "curated"
                out.append(m)
            return out, "curated"
    if origin.get("lat") is None or airport.get("lat") is None:
        return [], "unknown"
    return estimate_modes(origin, airport, clock_minutes), "estimated"


def support_message(modes: List[Dict[str, Any]]) -> Optional[str]:
    """The one sentence to put in front of a user about how much to trust this.

    Worst case wins: if the car number is a placeholder, saying nothing because
    the train number happens to be fine is how somebody ends up stranded."""
    if not modes:
        return "The ride to the airport could not be priced and is not in these totals"
    order = {"curated": 0, "modelled": 1, "assumed": 2}
    worst = max((m.get("support", "modelled") for m in modes), key=lambda k: order.get(k, 1))
    return SUPPORT_NOTE.get(worst)


def _hhmm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def _within(hours: str, clock: int) -> bool:
    if hours in ("any", "24h"):
        return True
    lo, hi = _hhmm(hours[:5]), _hhmm(hours[6:11])
    return lo <= clock <= hi if lo <= hi else (clock >= lo or clock <= hi)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE)
    import adapter
    enr = adapter.load_enrichment()
    text = sys.argv[1] if len(sys.argv) > 1 else "Bushwick, Brooklyn"
    iata = (sys.argv[2] if len(sys.argv) > 2 else "JFK").upper()
    when = sys.argv[3] if len(sys.argv) > 3 else "09:00"
    ap = enr["airports"]["airports"].get(iata, {})
    airport = {"iata": iata, "lat": ap.get("lat"), "lon": ap.get("lon"),
               "country": ap.get("country"), "city": ap.get("city")}
    if airport["lat"] is None:      # not curated with coordinates - use a known one
        airport.update({"lat": 40.6413, "lon": -73.7781, "country": "US", "city": "New York"})
    o, how = resolve_origin(text, enr, airport)
    print("origin  : %s  (%s)" % (o["label"], how))
    if o["lat"] is not None:
        print("distance: %.1f km straight line" % haversine_km(
            o["lat"], o["lon"], airport["lat"], airport["lon"]))
    modes, src = modes_for(o, airport, _hhmm(when), enr)
    print("source  : %s\n" % src)
    for m in modes:
        print("  %-34s $%-7.2f %3d min  %s" % (
            m["mode"], m["fare_cents"] / 100, m["door_to_door_minutes"]["p50"],
            m.get("basis", "curated")))
