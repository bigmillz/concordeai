"""Award prices from PUBLISHED charts, for the flights a search returned (2026-09-24, per Patrick: "if a person
spends the time putting in all their miles and points and airline status, then that should work to their
advantage", and no sample data anywhere).

A chart says what a seat COSTS if the airline releases one; nothing here says a seat is available, and the page
says so. Programmes that publish no chart (Delta, United, Flying Blue, and most airlines on their own flights) are
listed as able to book the flight, never priced: a made-up price is the one thing this must not show.

Deterministic arithmetic over enrichment/points.json, same as the scorer: no network, no model. The transfer plan
(which card's points to move, how many) runs in the page, against the balances the person typed, from the same
file's transfer table; this module only prices the award in the programme's own currency.

    python3 concorde-travel/points.py JFK LHR DL business 2026-11-18    # what the charts say for one flight
"""
import datetime
import functools
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
CABINS = ("economy", "premium_economy", "business", "first")
RANK = {c: i for i, c in enumerate(CABINS)}


@functools.lru_cache(maxsize=1)
def load() -> Dict[str, Any]:
    with open(os.path.join(HERE, "enrichment", "points.json"), encoding="utf-8") as fh:
        return json.load(fh)


def miles_between(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[int]:
    """Great-circle miles between two airports (the distance every distance chart means), or None without both
    airports' coordinates."""
    try:
        la1, lo1, la2, lo2 = map(math.radians, (float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])))
    except (KeyError, TypeError, ValueError):
        return None
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return int(round(2 * 3958.8 * math.asin(math.sqrt(h))))


def _hawaii(f): return f.get("country") == "US" and f.get("lat") is not None and 18 <= float(f["lat"]) <= 23 and float(f["lon"]) < -154
def _alaska(f): return f.get("country") == "US" and f.get("lat") is not None and float(f["lat"]) > 51 and (float(f["lon"]) < -129 or float(f["lon"]) > 170)


def _in(spec: Dict[str, Any], code: str, f: Dict[str, Any]) -> bool:
    """Whether an airport falls inside one region definition from points.json's geo block."""
    if code in (spec.get("not_airports") or []):
        return False
    if code in (spec.get("airports") or []):
        return True
    if spec.get("us_hawaii"):
        return _hawaii(f)
    if spec.get("not_hawaii") and _hawaii(f):
        return False
    geo = load()["geo"]
    if f.get("country") == "US" and spec.get("us_states"):
        return geo["us_state"].get(code) in spec["us_states"]
    if f.get("country") == "CA" and spec.get("ca_provinces"):
        return geo["ca_province"].get(code) in spec["ca_provinces"]
    return f.get("country") in (spec.get("countries") or [])


def region(zones: Dict[str, Dict[str, Any]], code: str, f: Dict[str, Any]) -> Optional[str]:
    """The one zone an airport sits in, or None when it sits in none (or, which the data never allows, two)."""
    hits = [z for z, spec in zones.items() if _in(spec, code, f)]
    return hits[0] if len(hits) == 1 else None


def _band(bands: List[Dict[str, Any]], miles: int) -> Optional[Dict[str, Any]]:
    for b in bands:
        if miles >= (b.get("min") or 0) and (b.get("max") is None or miles <= b["max"]):
            return b
    return None


def _season(dates: Dict[str, List[List[str]]], day: str) -> Optional[str]:
    for s, spans in dates.items():
        if any(a <= day <= b for a, b in spans):
            return s
    return None


def _top(segs):
    return max((s["cabin"] for s in segs), key=lambda c: RANK.get(c, 0))


def _cont(f: Dict[str, Any]) -> Optional[str]:
    """A coarse continent for the rules that need one."""
    geo = load()["geo"]["country_sets"]
    c = f.get("country")
    if not c:
        return None
    if c in ("US", "CA", "MX", "GL", "BM", "PM"):
        return "north_america"
    for k, v in geo.items():
        if c in v:
            return k
    if c in ("TR", "RU"):
        return c
    return None


# ------------------------------------------------------------------------------------------------ chart rules
def _out(chart_id, ch, miles, cabin, basis, low=None, low_when=None, fee_usd=None, extra=None):
    a = {"chart": chart_id, "miles": int(miles), "cabin": cabin, "basis": basis}
    if low is not None and low < miles:
        a["low"] = int(low)
        a["low_when"] = low_when
    if fee_usd is not None:
        a["fee_usd"] = round(float(fee_usd), 2)
    if extra:
        a.update(extra)
    return a


def rule_distance(cid, ch, segs, F):
    if ch.get("nonstop") and len(segs) != 1:
        return None
    s = segs[0]
    if len(segs) != 1 or s["op"] not in ch["operators"]:
        return None
    cab = s["cabin"]
    if cab == "business" and ch.get("business_as"):
        cab = ch["business_as"]
    only = (ch.get("cabins_by_operator") or {}).get(cab)
    if only is not None and s["op"] not in only:
        return None
    m = miles_between(F[s["from"]], F[s["to"]])
    if m is None:
        return None
    b = _band(ch["bands"], m)
    if not b or b.get(cab) is None:
        return None
    return _out(cid, ch, b[cab], s["cabin"], "%s, %s miles" % (ch["name"], "{:,}".format(m)),
                fee_usd=ch.get("fee_usd"), extra={"floor": True} if ch.get("floor") else None)


def rule_distance_season(cid, ch, segs, F):
    if len(segs) != 1 or segs[0]["op"] not in ch["operators"]:
        return None
    s = segs[0]
    if [s["from"], s["to"]] in ch.get("exclude_routes", []) or [s["to"], s["from"]] in ch.get("exclude_routes", []):
        return None
    m = miles_between(F[s["from"]], F[s["to"]])
    if m is None:
        return None
    pk, op = _band(ch["bands"]["peak"], m), _band(ch["bands"]["off-peak"], m)
    if not pk or pk.get(s["cabin"]) is None:
        return None
    low = op.get(s["cabin"]) if op else None
    return _out(cid, ch, pk[s["cabin"]], s["cabin"], "%s, %s miles, peak" % (ch["name"], "{:,}".format(m)),
                low=low, low_when="on an off-peak date (%s)" % ch["season"])


def rule_ba_own(cid, ch, segs, F):
    geo = load()["geo"]
    if any(s["op"] != "BA" for s in segs):
        return None
    tot_pk = tot_op = 0
    for s in segs:
        a, b = s["from"], s["to"]
        other = b if a in geo["london"] else a if b in geo["london"] else None
        if other is None or (a in geo["london"] and b in geo["london"]):
            return None
        city = geo["ba_city"].get(other) or (F[other].get("city") or "")
        norm = lambda x: "".join(ch_ for ch_ in __import__("unicodedata").normalize("NFKD", x.lower()) if ch_.isalnum())
        tier = next((t for t in ch["tiers"] if norm(city) in {norm(c) for c in t["cities"]}), None)
        if not tier or tier["peak"].get(s["cabin"]) is None:
            return None
        tot_pk += tier["peak"][s["cabin"]]
        tot_op += tier["off-peak"].get(s["cabin"], tier["peak"][s["cabin"]])
    return _out(cid, ch, tot_pk, _top(segs), "%s, London route tier%s, peak" % (ch["name"], "s" if len(segs) > 1 else ""),
                low=tot_op, low_when="on an off-peak date (BA's calendar)")


def rule_ba_partner(cid, ch, segs, F):
    ops = {s["op"] for s in segs}
    if len(ops) != 1 or not ops <= set(ch["partners"]):
        return None
    op = ops.pop()
    grp = "AA/AS" if op in ("AA", "AS") else "CX/CZ" if op in ("CX", "CZ") else "JL" if op == "JL" else "other"
    total = 0
    for s in segs:
        if s["cabin"] not in ("economy", "business"):
            return None
        m = miles_between(F[s["from"]], F[s["to"]])
        if m is None:
            return None
        na = {F[s["from"]].get("country"), F[s["to"]].get("country")} <= {"US", "CA", "MX"}
        if na and m <= 650:
            m = 651          # within North America the first band prices as the second (The MileLion)
        b = _band(ch["groups"]["all"], m) if m > 3000 else _band(ch["groups"][grp], m)
        if not b or b.get(s["cabin"]) is None:
            return None
        total += b[s["cabin"]]
    ex = {"caveat": ch["caveat"]} if grp == "AA/AS" else None
    return _out(cid, ch, total, _top(segs), "%s, priced per flight by distance" % ch["name"], extra=ex)


def rule_atmos_partner(cid, ch, segs, F):
    if len(segs) != 1 or segs[0]["op"] not in ch["operators"]:
        return None
    s = segs[0]
    fa, fb = F[s["from"]], F[s["to"]]
    ca, cb = _cont(fa), _cont(fb)
    americas = {"north_america", "central_america", "caribbean", "south_america"}
    emea = {"europe", "mideast", "north_africa", "southern_africa", "africa_rest", "TR"}
    apac = {"east_asia", "southeast_asia", "oceania"}
    us = lambda f: f.get("country") == "US"
    if ca in americas and cb in americas:
        key = "Americas"
    elif (us(fa) and cb in emea) or (us(fb) and ca in emea) or (ca in emea and cb in emea):
        key = "Europe, Middle East, Africa"
    elif ((us(fa) or ca in emea) and cb in apac) or ((us(fb) or cb in emea) and ca in apac) or (ca in apac and cb in apac):
        key = "Asia Pacific"
    else:
        return None
    m = miles_between(fa, fb)
    if m is None:
        return None
    b = _band(ch["regions"][key], m)
    if not b or b.get(s["cabin"]) is None:
        return None
    return _out(cid, ch, b[s["cabin"]], s["cabin"], "%s, %s chart, %s miles" % (ch["name"], key, "{:,}".format(m)),
                fee_usd=ch.get("fee_usd"), extra={"floor": True})


def _aa_region(code, f):
    geo = load()["geo"]["country_sets"]
    c = f.get("country")
    if c == "US":
        return "Hawaii" if _hawaii(f) else "Alaska" if _alaska(f) else "Contiguous 48 US & Canada"
    if c == "CA":
        return "Contiguous 48 US & Canada"
    if c == "MX":
        return "Mexico"
    if c in geo["caribbean"]:
        return "Caribbean"
    if c in geo["central_america"]:
        return "Central America"
    if c in ("CO", "EC", "GY", "PE", "SR", "VE"):
        return "South America Region 1"
    if c in ("AR", "BO", "BR", "CL", "PY", "UY"):
        return "South America Region 2"
    if c in geo["europe"] or c in ("TR", "MA"):
        return "Europe"
    if c in geo["mideast"] or c == "EG":
        return "Middle East"
    if c in ("IN", "PK", "BD", "LK", "NP", "BT", "MV"):
        return "Indian Subcontinent"
    if c in ("JP", "KR", "MN"):
        return "Asia Region 1"
    if c in ("CN", "HK", "MO", "TW") or c in geo["southeast_asia"]:
        return "Asia Region 2"
    if c in ("AU", "NZ", "FJ", "PF", "NC", "WS", "TO", "VU", "PG"):
        return "South Pacific"
    if c in geo["africa_rest"] or c in geo["southern_africa"] or c in ("DZ", "TN", "LY", "SD"):
        return "Africa"
    return None


def rule_aa_partner(cid, ch, segs, F):
    if any(s["op"] not in ch["operators"] for s in segs):
        return None
    long_intl = [s for s in segs if F[s["from"]].get("country") != F[s["to"]].get("country")
                 and (miles_between(F[s["from"]], F[s["to"]]) or 0) > 3000]
    if len(long_intl) > 1:
        return None          # "two flight awards are needed if your one-way trip includes two long-haul international flights"
    o, d = segs[0]["from"], segs[-1]["to"]
    ro, rd = _aa_region(o, F[o]), _aa_region(d, F[d])
    if any(_aa_region(s["to"], F[s["to"]]) not in (ro, rd) for s in segs[:-1]):
        return None          # through a third region: American's routing rules, not this chart
    home = "Contiguous 48 US & Canada"
    if ro == home:
        far, outbound = rd, True
    elif rd == home:
        far, outbound = ro, False
    else:
        return None          # only the chart from the contiguous US and Canada was read
    row = ch["regions"].get(far)
    cab = _top(segs)
    if not row or (row.get("standard") or {}).get(cab) is None:
        return None
    miles = row["standard"][cab]
    extra = {"floor": True}
    if far == "Europe" and cab == "economy" and outbound and (row.get("off-peak") or {}).get("economy"):
        md = segs[0]["date"][5:]
        if any(a <= md <= b for a, b in ch["europe_offpeak"]):
            miles, extra["season"] = row["off-peak"]["economy"], "off-peak"
    if any(s["op"] in ch.get("surcharge_high", []) for s in segs):
        extra["surcharge"] = "high"
    return _out(cid, ch, miles, cab, "%s, %s to %s" % (ch["name"], ro, rd), extra=extra)


def _ac_zone(code, f):
    c, cont = f.get("country"), _cont(f)
    if cont in ("north_america", "central_america", "caribbean"):
        return "North America"
    if cont == "south_america":
        return "South America"
    if cont in ("europe", "mideast", "north_africa", "southern_africa", "africa_rest", "central_asia", "caucasus", "TR") or c in ("IN", "PK"):
        return "Atlantic"
    if c == "RU":
        return "Atlantic" if f.get("lon") is not None and float(f["lon"]) < 60 else "Pacific"
    if cont in ("east_asia", "southeast_asia", "oceania") and c != "MM":
        return "Pacific"
    return None


def rule_aeroplan(cid, ch, segs, F):
    if any(s["op"] not in ch["operators"] for s in segs):
        return None
    o, d = segs[0]["from"], segs[-1]["to"]
    za, zb = _ac_zone(o, F[o]), _ac_zone(d, F[d])
    if not za or not zb:
        return None
    flown = 0
    for s in segs:
        m = miles_between(F[s["from"]], F[s["to"]])
        if m is None:
            return None
        flown += m
    cab = _top(segs)
    rows = [r for r in ch["rows"] if {r["a"], r["b"]} == {za, zb}]
    b = _band(rows, flown)
    if not b or b.get(cab) is None:
        return None
    fee = ch["fee"]
    return _out(cid, ch, b[cab], cab, "%s, %s to %s, %s miles flown" % (ch["name"], za, zb, "{:,}".format(flown)),
                extra={"fee_local": fee})


def _zones_for(ch):
    geo = load()["geo"]
    return {"ana": geo["ana_zones"], "tk": geo["tk_regions"], "mm": geo["mm_regions"]}[ch["zoneset"]]


def rule_zones(cid, ch, segs, F):
    if any(s["op"] not in ch["operators"] for s in segs):
        return None
    if ch.get("nonstop") and len(segs) != 1:
        return None
    zones = _zones_for(ch)
    o, d = segs[0]["from"], segs[-1]["to"]
    cab = _top(segs)
    if ch.get("same_country") and F[o].get("country") == F[d].get("country") and len(segs) == 1:
        v = ch["same_country"].get(cab)
        return _out(cid, ch, v, cab, "%s, a flight within one country" % ch["name"]) if v else None
    za, zb = region(zones, o, F[o]), region(zones, d, F[d])
    if not za or not zb:
        return None
    # a connection through a third region is not a two-region award (Miles & More prices those higher, ANA's routing
    # rules limit them): priced only when every stop sits in the origin's or the destination's region
    if any(region(zones, s["to"], F[s["to"]]) not in (za, zb) for s in segs[:-1]):
        return None
    row = next((r for r in ch["rows"] if {r["a"], r["b"]} == {za, zb} and (za != zb or r["a"] == r["b"])), None)
    if not row or row.get(cab) is None:
        return None
    return _out(cid, ch, row[cab], cab, "%s, %s to %s" % (ch["name"], za, zb))


def _vs_region(code, f):
    geo = load()["geo"]["country_sets"]
    if f.get("country") == "GB":
        return "UK"
    if f.get("country") in geo["europe"]:
        return "Europe (ex UK)"
    return None


def rule_vs_delta(cid, ch, segs, F):
    ops = {s["op"] if s["op"] not in ch["connection_carriers"] or s["mk"] != "DL" else "DL" for s in segs}
    if ops != {"DL"} or len(segs) != 1:
        return None
    s = segs[0]
    us = {code: reg for reg, codes in ch["us_regions"].items() for code in codes}
    fa, fb = F[s["from"]], F[s["to"]]
    season = _season(ch["season_dates"], s["date"])
    eu_end = s["from"] if _vs_region(s["from"], fa) else s["to"] if _vs_region(s["to"], fb) else None
    us_end = s["to"] if eu_end == s["from"] else s["from"] if eu_end == s["to"] else None
    if eu_end and us_end:
        reg_eu, reg_us = _vs_region(eu_end, F[eu_end]), us.get(us_end)
        if not reg_us or not season:
            return None
        row = next((r for r in ch["transatlantic"] if r["a"] == reg_eu and r["b"] == reg_us and r["season"] == season), None)
        if not row or row.get(s["cabin"]) is None:
            return None
        ex = {"season": season}
        if eu_end == s["from"]:
            fee = (ch["fees_from"].get(reg_eu) or {}).get(s["cabin"])
            if fee is not None:
                ex["fee_usd"] = fee
                ex["fee_known"] = True
        if row.get("note"):
            ex["caveat"] = row["note"]
        return _out(cid, ch, row[s["cabin"]], s["cabin"], "%s, %s to %s, %s" % (ch["name"], reg_eu, reg_us.replace(" USA", ""), season), extra=ex)
    if _cont(fa) == "europe" or _cont(fb) == "europe":
        return None          # a transatlantic nonstop from a US city Virgin does not list: no published price
    m = miles_between(fa, fb)
    if m is None:
        return None
    b = _band(ch["other"], m)
    if not b or b.get(s["cabin"]) is None:
        return None
    return _out(cid, ch, b[s["cabin"]], s["cabin"], "%s, %s miles" % (ch["name"], "{:,}".format(m)))


def rule_vs_afkl(cid, ch, segs, F):
    if any(s["op"] not in ch["operators"] for s in segs):
        return None
    zones = load()["geo"]["vs_zones"]
    o, d = segs[0]["from"], segs[-1]["to"]
    season = _season(ch["season_dates"], segs[0]["date"])
    if not season:
        return None
    cab = _top(segs)
    za, zb = region(zones, o, F[o]), region(zones, d, F[d])
    if za in ("1", "2") and zb in ("1", "2") and len(segs) == 1:
        m = miles_between(F[o], F[d])
        if m is None:
            return None
        b = _band(ch["short"][season], m)
        if not b or b.get(cab) is None:
            return None
        return _out(cid, ch, b[cab], cab, "%s, short haul, %s miles, %s" % (ch["name"], "{:,}".format(m), season), extra={"season": season})
    if not za or not zb or za == zb:
        return None

    def zone_price(x, y, c):
        if x in ("1", "2") and y in ("1", "2"):
            return None
        i, j = sorted((int(x), int(y)))
        r = next((r for r in ch["zones"] if r["a"] == i and r["b"] == j and r["season"] == season), None)
        return r.get(c) if r else None
    whole = zone_price(za, zb, cab)
    if whole is None:
        return None
    if len(segs) == 1:
        return _out(cid, ch, whole, cab, "%s, zone %s to zone %s, %s" % (ch["name"], za, zb, season), extra={"season": season})
    # Virgin's page does not say whether a connection prices as one journey or flight by flight: the higher of the two
    each = 0
    for s in segs:
        x, y = region(zones, s["from"], F[s["from"]]), region(zones, s["to"], F[s["to"]])
        if not x or not y or x == y and x not in ("1", "2"):
            return None
        if x in ("1", "2") and y in ("1", "2"):
            m = miles_between(F[s["from"]], F[s["to"]])
            b = _band(ch["short"][season], m) if m is not None else None
            v = b.get(s["cabin"]) if b else None
        else:
            v = zone_price(x, y, s["cabin"])
        if v is None:
            return None
        each += v
    return _out(cid, ch, max(whole, each), cab, "%s, zone %s to zone %s, %s, %s" % (ch["name"], za, zb, season,
                "priced flight by flight" if each > whole else "priced as one journey"),
                extra={"season": season, "caveat": "Virgin does not say whether a connection prices as one journey or flight by flight; this is the higher"})


def _tk_region(code, f):
    geo = load()["geo"]
    r = region(geo["tk_regions"], code, f)
    return "Türkiye" if r == "Türkiye" else r


def rule_tk_own(cid, ch, segs, F):
    if any(s["op"] != "TK" for s in segs):
        return None
    tot_std = tot_pro = 0
    for s in segs:
        ra, rb = _tk_region(s["from"], F[s["from"]]), _tk_region(s["to"], F[s["to"]])
        if "Türkiye" not in (ra, rb):
            return None
        other = rb if ra == "Türkiye" else ra
        if other is None:
            return None
        key = "Türkiye" if other == "Türkiye" else other
        std, pro = (ch["standard"].get(key) or {}).get(s["cabin"]), (ch["promotion"].get(key) or {}).get(s["cabin"])
        if std is None:
            return None
        tot_std += std
        tot_pro += pro if pro is not None else std
    return _out(cid, ch, tot_std, _top(segs), "%s, Award Ticket level, priced per flight through Istanbul" % ch["name"],
                low=tot_pro, low_when="at the Promotion level, when Turkish releases those seats")


def rule_sq_own(cid, ch, segs, F):
    if len(segs) != 1 or segs[0]["op"] != "SQ":
        return None
    s = segs[0]
    zones = load()["geo"]["sq_zones"]
    if s["from"] == "SIN":
        other = s["to"]
    elif s["to"] == "SIN":
        other = s["from"]
    else:
        return None
    z = region(zones, other, F[other])
    if not z:
        return None
    adv, sav = (ch["advantage"].get(z) or {}).get(s["cabin"]), (ch["saver"].get(z) or {}).get(s["cabin"])
    if adv is None and sav is None:
        return None
    if adv is None:          # premium economy has no Advantage level: Saver is the only published price
        return _out(cid, ch, sav, s["cabin"], "%s, Singapore to zone %s, Saver" % (ch["name"], z))
    return _out(cid, ch, adv, s["cabin"], "%s, Singapore to zone %s, Advantage" % (ch["name"], z),
                low=sav, low_when="at the Saver level, when Singapore Airlines releases those seats")


HIGH_CHARGES = {"BA"}

RULES = {"distance": rule_distance, "distance_season": rule_distance_season, "ba_own": rule_ba_own, "ba_partner": rule_ba_partner,
         "atmos_partner": rule_atmos_partner, "aa_partner": rule_aa_partner, "aeroplan": rule_aeroplan, "zones": rule_zones,
         "vs_delta": rule_vs_delta, "vs_afkl": rule_vs_afkl, "tk_own": rule_tk_own, "sq_own": rule_sq_own}


def awards(segs: List[Dict[str, Any]], facts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """segs: [{from, to, op, mk, cabin, date}] for ONE direction of travel; facts: IATA -> {lat, lon, country, city}.
    -> {"priced": [award in the programme's own currency, per person, one way], "bookable": [programme keys that can
    book every flight here but publish no chart]}"""
    P = load()
    # a regional airline flying for its parent (Lufthansa CityLine, KLM Cityhopper, Delta Connection) books as the parent
    reg = P.get("regional") or {}
    segs = [dict(s, cabin=s.get("cabin") if s.get("cabin") in CABINS else "economy",
                 op=s["mk"] if s.get("op") != s.get("mk") and s.get("op") in reg.get(s.get("mk"), []) else s.get("op")) for s in segs]
    if not segs:
        return {"priced": [], "bookable": []}
    ops = {s["op"] for s in segs}
    bookable_all = lambda have: sorted(k for k, p in P["programmes"].items()
                                       if k not in have and ops <= set(p.get("dynamic") or []) and ops <= set(p.get("books") or []))
    if any(s["from"] not in facts or s["to"] not in facts for s in segs):
        return {"priced": [], "bookable": bookable_all(set())}      # no coordinates: nothing priced, still bookable
    priced = []
    for cid, ch in P["charts"].items():
        try:
            a = RULES[ch["rule"]](cid, ch, segs, facts)
        except (KeyError, TypeError, ValueError):
            a = None
        if not a:
            continue
        for prog in ch["programmes"]:
            priced.append(dict(a, prog=prog, surcharge=a.get("surcharge") or ch.get("surcharge"), fees=ch.get("fees"),
                               url=ch.get("url"), as_of=ch.get("as_of"), secondary=bool(ch.get("secondary")), chart_name=ch["name"]))
    # British Airways' own carrier charges ride on its flights whichever programme books them (AwardWallet: "several
    # hundred dollars" to and from Europe), so a BA flight is never shown as cheap in cash beside the points
    if any(s["op"] in HIGH_CHARGES for s in segs):
        for a in priced:
            if a["surcharge"] != "high":
                a["surcharge"] = "high"
                a["fees"] = (a.get("fees") or "") + "; British Airways adds its own carrier charges, several hundred dollars across the Atlantic"
    bookable = bookable_all({a["prog"] for a in priced})
    priced.sort(key=lambda a: (a["miles"], a["prog"]))
    return {"priced": priced, "bookable": bookable}


def facts_for(codes, curated: Dict[str, Any], geo: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """IATA -> {lat, lon, country, city}: the curated table first, then what the feed or Duffel's places said."""
    out = {}
    for c in codes:
        a, g = curated.get(c) or {}, geo.get(c) or {}
        lat = a.get("lat") if a.get("lat") is not None else g.get("lat")
        lon = a.get("lon") if a.get("lon") is not None else g.get("lon")
        country = a.get("country") or g.get("country")
        if lat is None or lon is None or not country:
            continue
        out[c] = {"lat": lat, "lon": lon, "country": country, "city": a.get("city") or g.get("city")}
    return out


def page_table() -> Dict[str, Any]:
    """What the page needs to plan transfers and name things: banks, programmes, the Avios family, status bags and
    the valuation source. Charts and geography stay on the server."""
    P = load()
    progs = {k: {x: v[x] for x in ("name", "currency", "site", "cents", "cents_note", "search", "home") if x in v} for k, v in P["programmes"].items()}
    return {"as_of": P["as_of"], "valuation": P["valuation"], "banks": P["banks"], "programmes": progs,
            "avios_family": P["avios_family"], "status_bags": P["status_bags"]}


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(1)
    o, d, op, cab = sys.argv[1:5]
    day = sys.argv[5] if len(sys.argv) > 5 else datetime.date.today().isoformat()
    sys.path.insert(0, HERE)
    import adapter
    import server
    cur = adapter.load_enrichment()["airports"]["airports"]
    F = facts_for([o, d], cur, server.airport_geo([c for c in (o, d) if c not in cur]))
    r = awards([{"from": o, "to": d, "op": op, "mk": op, "cabin": cab, "date": day}], F)
    for a in r["priced"]:
        print("%-3s %8s  %s%s" % (a["prog"], "{:,}".format(a["miles"]), a["basis"], "  (%s %s)" % ("{:,}".format(a["low"]), a["low_when"]) if a.get("low") else ""))
    print("bookable, no published price:", ", ".join(r["bookable"]) or "none")
