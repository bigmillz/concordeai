"""The delayed-or-cancelled flight helper (per Patrick, 2026-09-21): a
traveller whose flight is delayed or cancelled tells us the situation, we
search the alternatives, and the arithmetic makes the call in dollars: stay
with what the airline offers, or switch, and to what.

Same rules as the rest of the product. The decision is deterministic: every
alternative is priced as what it costs out of pocket (its ticket, less the
refund the rules say you can expect), less the hours it saves at your rate,
plus a hotel night when the only way on is tomorrow morning. The model
(narrator.py's guard rails) writes the advice from those figures and may not
add one. Rights are stated as what the rules say you MAY be owed, never as a
promise: the airline decides, and 'extraordinary circumstances' is a real
exception in Europe."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import narrator                                                  # noqa: E402  (the guard rails; never the scorer, never the adapter)
import ground                                                    # noqa: E402  (the ride model, for the night's rides)
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
HOTEL_KM = 4.0               # an airport hotel sits a few kilometres out; the ride is priced at that

HOTEL_CENTS = 18000          # a night near the airport, the page's own figure
BUFFER_MINUTES = 75          # you cannot make a flight that leaves sooner than this
US_DOMESTIC_REFUND_MINUTES = 180
US_INTL_REFUND_MINUTES = 360

SYSTEM = """You help a traveller whose flight is delayed or cancelled decide what to do
right now. You are given a BRIEF with the situation, the figures already calculated for
each alternative, the decision the arithmetic reached, and the rights the rules suggest.

Absolute rules:
- Use ONLY numbers that appear in the brief. Never calculate anything.
- Rights are what the traveller MAY be entitled to; say "may" or "can ask for", never
  "you are owed" or "the airline must".
- Never promise a seat, a refund, or an outcome. Never name a hotel or a price the brief
  does not give.
- Direct and specific: lead with what to do. Calm, not chatty. No "we" or "our",
  no exclamation marks.

Write `advice`: three to five sentences. What to do first, why the arithmetic says so,
the one practical step (the desk, the app, the phone line, a hotel tonight), and what to
ask the airline for.
"""

SCHEMA = {"type": "object", "properties": {"advice": {"type": "string"}},
          "required": ["advice"], "additionalProperties": False}


def _parse(s: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


def _minutes(a: Optional[datetime], b: Optional[datetime]) -> Optional[int]:
    if a is None or b is None:
        return None
    return int(round((b - a).total_seconds() / 60))


def load_hotels() -> Dict[str, Any]:
    try:
        with open(os.path.join(HERE, "enrichment", "hotels.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"airports": {}, "country_default_cents": {}, "_default_cents": HOTEL_CENTS}


def night_near(airport: Optional[Dict[str, Any]], now: Optional[datetime], dep: Optional[datetime],
               hotels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What a night near the airport costs before a flight tomorrow: a typical
    room rate from the curated table (never a live price, and it says so),
    and the ground model's own rideshare estimate for a hotel a few
    kilometres out, there at this hour and back two hours before the flight."""
    hotels = hotels or load_hotels()
    iata = (airport or {}).get("iata") or ""
    country = (airport or {}).get("country") or ""
    rate = (hotels.get("airports") or {}).get(iata)
    if rate:
        hotel_basis = "typical rate near %s, not a live price" % iata
    elif (hotels.get("country_default_cents") or {}).get(country):
        rate = hotels["country_default_cents"][country]
        hotel_basis = "typical rate in %s, not a live price (%s is not in the table)" % (country, iata or "the airport")
    else:
        rate = int(hotels.get("_default_cents") or HOTEL_CENTS)
        hotel_basis = "rough estimate for %s, not a live price" % (iata or "the airport")
    out = {"hotel_cents": int(rate), "hotel_basis": hotel_basis, "rides_cents": 0, "rides": [], "rides_basis": "",
           "km": HOTEL_KM}
    if airport and airport.get("lat") is not None and airport.get("lon") is not None:
        hotel_pt = {"lat": float(airport["lat"]) + HOTEL_KM / 111.0, "lon": float(airport["lon"])}
        ap = {"lat": float(airport["lat"]), "lon": float(airport["lon"]), "country": country, "iata": iata}
        legs = []
        if now is not None:
            legs.append(("to a hotel tonight", now.hour * 60 + now.minute))
        if dep is not None:
            back = dep - timedelta(hours=2)
            legs.append(("back for the flight", back.hour * 60 + back.minute))
        for label, clock in legs:
            try:
                modes = ground.estimate_modes(hotel_pt, ap, clock)
            except Exception:
                modes = []
            ride = next((m for m in modes if "ride" in str(m.get("mode", "")).lower() or "taxi" in str(m.get("mode", "")).lower()), None)
            if ride:
                out["rides"].append({"label": label, "cents": int(ride["fare_cents"]), "mode": ride["mode"],
                                     "minutes": int(ride["door_to_door_minutes"]["p50"])})
        out["rides_cents"] = sum(r["cents"] for r in out["rides"])
        out["rides_basis"] = ("estimated rideshare to a hotel about %d km away and back, at %s prices" % (HOTEL_KM, iata)
                              if out["rides"] else "no ride estimate for %s" % iata)
    return out


def _adb_time(t: Any) -> Optional[str]:
    """AeroDataBox's 'yyyy-MM-dd HH:mm±hh:mm' (or 'Z') -> ISO with an offset."""
    if not t:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?\s*(Z|[+-]\d{2}:?\d{2})?$", str(t).strip())
    if not m:
        return None
    off = m.group(3) or "+00:00"
    if off == "Z":
        off = "+00:00"
    if re.match(r"^[+-]\d{4}$", off):
        off = off[:3] + ":" + off[3:]
    return "%sT%s:00%s" % (m.group(1), m.group(2), off)


CANCELLED_WORDS = ("canceled", "cancelled", "canceleduncertain")
FLOWN_WORDS = ("departed", "enroute", "approaching", "arrived", "diverted")


def from_tracking(legs: Any, origin: str = "", destination: str = "") -> Optional[Dict[str, Any]]:
    """The tracking feed's legs for the flight number and day -> what the
    Fixer's form asks: the scheduled and revised clocks, delayed or
    cancelled, the delay so far, and where the airline last put the
    aircraft. OBSERVED, not modelled, and the result says so. Picks the leg
    leaving `origin` (and reaching `destination`) when several legs share a
    number; None when nothing matches."""
    if not isinstance(legs, list):
        return None
    o, d = (origin or "").upper()[:3], (destination or "").upper()[:3]
    cands = [l for l in legs if isinstance(l, dict) and isinstance(l.get("departure"), dict)]
    if o:
        cands = [l for l in cands if str((l["departure"].get("airport") or {}).get("iata") or "").upper() == o] or cands
    if d:
        cands = [l for l in cands if str(((l.get("arrival") or {}).get("airport") or {}).get("iata") or "").upper() == d] or cands
    if not cands:
        return None
    leg = cands[0]
    dep, arr = leg.get("departure") or {}, leg.get("arrival") or {}
    sched_dep = _adb_time((dep.get("scheduledTime") or {}).get("local"))
    new_dep = _adb_time((dep.get("revisedTime") or {}).get("local"))
    sched_arr = _adb_time((arr.get("scheduledTime") or {}).get("local"))
    new_arr = _adb_time((arr.get("revisedTime") or {}).get("local"))
    word = str(leg.get("status") or "").strip()
    low = word.lower()
    if low in CANCELLED_WORDS:
        kind = "cancelled"
    elif low in FLOWN_WORDS:
        kind = "flown"
    else:
        kind = "delayed" if (new_dep and sched_dep and _parse(new_dep) and _parse(sched_dep) and _parse(new_dep) > _parse(sched_dep) + timedelta(minutes=14)) else "on_time"
    delay = None
    if new_dep and sched_dep and _parse(new_dep) and _parse(sched_dep):
        delay = max(0, int((_parse(new_dep) - _parse(sched_dep)).total_seconds() // 60))
    return {"observed": True, "source": "AeroDataBox", "status_word": word or "Unknown", "kind": kind,
            "number": leg.get("number"), "airline": (leg.get("airline") or {}).get("name"),
            "origin": (dep.get("airport") or {}).get("iata"), "destination": (arr.get("airport") or {}).get("iata"),
            "sched_depart": sched_dep, "new_depart": new_dep, "sched_arrive": sched_arr, "new_arrive": new_arr,
            "delay_minutes": delay, "terminal": dep.get("terminal"), "gate": dep.get("gate"),
            "aircraft": (leg.get("aircraft") or {}).get("model"), "registration": (leg.get("aircraft") or {}).get("reg"),
            "last_updated": _adb_time(leg.get("lastUpdatedUtc"))}


def apply_tracking(sit: Dict[str, Any], tr: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The observed status fills what the traveller did not type, and overrides
    the delay it can measure. What they typed about rebooking and money stays."""
    if not tr:
        return sit
    sit = dict(sit)
    if tr["kind"] == "cancelled":
        sit["kind"] = "cancelled"
    elif tr["kind"] in ("delayed", "on_time") and sit.get("kind") != "cancelled":
        sit["kind"] = "delayed"
    for k in ("sched_depart", "new_depart", "new_arrive"):
        if tr.get(k):
            sit[k] = tr[k]
    if tr.get("delay_minutes") is not None:
        sit["delay_minutes"] = tr["delay_minutes"]
    sit["tracking"] = tr
    return sit


def refund_rights(sit: Dict[str, Any]) -> Tuple[bool, str]:
    """Can the traveller expect the unused ticket back if they do not fly it?
    US DOT's 2024 rule: a cancelled flight, or a delay of three hours at home
    and six abroad, and you choose not to travel. Elsewhere: cancelled, yes;
    delayed, ask."""
    kind = sit.get("kind")
    fare = str(sit.get("fare") or "")
    if fare == "flex":
        return True, "a flexible fare can be refunded under its own rules, whatever the delay"
    if kind == "cancelled":
        return True, "your flight was canceled, so if you skip the airline's new flight you may get a full refund"
    delay = sit.get("delay_minutes")
    intl = bool(sit.get("international"))
    need = US_INTL_REFUND_MINUTES if intl else US_DOMESTIC_REFUND_MINUTES
    if sit.get("us") and delay is not None and delay >= need:
        return True, ("a %s delay on a US %s flight counts as significant under US rules, so you may get a refund if you don't fly"
                      % (narrator._hm(delay), "international" if intl else "domestic"))
    if fare in ("business", "first"):
        return False, ("a %s fare is often refundable; check its rules. It is not counted in the totals%s"
                       % (fare, (". Under US rules, a delay under %s is too short for a refund" % narrator._hm(need)) if sit.get("us") else ""))
    return False, ("a delay under %s is too short for a refund under US rules, so a new ticket means paying twice unless the airline refunds you"
                   % narrator._hm(need) if sit.get("us") else "outside the US, a refund for a delay is up to the airline; ask before you buy")


def rights(sit: Dict[str, Any]) -> List[Dict[str, str]]:
    """What the rules say the traveller may ask for. Stated as 'may', decided by the airline."""
    out = []
    ok, why = refund_rights(sit)
    out.append({"what": "A refund of the unused ticket", "may": ok, "detail": why})
    if sit.get("kind") == "cancelled" or (sit.get("delay_minutes") or 0) >= 120:
        out.append({"what": "Rebooking at no charge", "may": True,
                    "detail": "on the airline's next flight or a partner's; ask for the earliest"})
    if sit.get("eu"):
        km = int(sit.get("distance_km") or 0)
        comp = "€600" if km > 3500 else "€400" if km > 1500 else "€250" if km else "€250 to €600 by distance"
        out.append({"what": "EU261 compensation of %s" % comp, "may": True,
                    "detail": "if canceled within two weeks or 3+ hours late, unless caused by weather, air traffic control or outside strikes"})
        out.append({"what": "Meals, and a hotel if you wait overnight", "may": True,
                    "detail": "EU rules cover this whatever the cause; keep receipts"})
    elif sit.get("us"):
        out.append({"what": "A meal, and a hotel overnight, if the airline caused it", "may": True,
                    "detail": "most US airlines offer this when the delay is their fault, not for weather"})
    if sit.get("bags_checked"):
        out.append({"what": "Your checked bag", "may": True,
                    "detail": "it will not follow you to another airline; ask the desk to pull it"})
    return out


def assess(sit: Dict[str, Any], options: List[Dict[str, Any]], now: Optional[datetime] = None,
           airports: Optional[Dict[str, Dict[str, Any]]] = None, hotels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The decision. `options` are the page's result entries (ticket_cents,
    depart, arrive with offsets, stops, carrier, flight, route, grade);
    `airports` maps IATA to lat, lon and country for the night's rides."""
    nights: Dict[str, Dict[str, Any]] = {}
    hourly = int(sit.get("hourly_value_cents") or 3500)
    paid = int(sit.get("paid_cents") or 0)
    ok, why = refund_rights(sit)
    refund = paid if ok else 0
    base_arrive = _parse(sit.get("rebook_arrive")) or (_parse(sit.get("new_arrive")) if sit.get("kind") == "delayed" else None)
    if base_arrive is None and sit.get("kind") == "delayed" and _parse(sit.get("new_depart")):
        # no new arrival given: the delayed flight lands the fastest option's flying time after its new departure
        spans = [_parse(o.get("arrive")) - _parse(o.get("depart")) for o in options if _parse(o.get("arrive")) and _parse(o.get("depart"))]
        if spans:
            base_arrive = _parse(sit.get("new_depart")) + min(spans)
    base_label = ("the airline's new flight" if sit.get("rebook_arrive")
                  else "your delayed flight" if base_arrive else "nothing yet")
    rows = []
    for o in options:
        dep, arr = _parse(o.get("depart")), _parse(o.get("arrive"))
        if dep is None or arr is None:
            continue
        if now is not None and dep <= now + timedelta(minutes=BUFFER_MINUTES):
            continue
        oop = int(o.get("ticket_cents", 0)) - refund
        sooner = _minutes(arr, base_arrive) if base_arrive else None      # positive: this one lands earlier
        time_value = int(round(sooner / 60.0 * hourly)) if sooner is not None else 0
        # a flight tomorrow means a night somewhere tonight, whatever the clock says now: a typical
        # room near the airport it leaves from, and the rides there and back
        night = None
        hotel = rides = 0
        if now is not None and dep.date() > now.date():
            ap = (o.get("route") or [None])[0] or ""
            if ap not in nights:
                nights[ap] = night_near((airports or {}).get(ap) or ({"iata": ap} if ap else None), now, dep, hotels)
            night = nights[ap]
            hotel, rides = night["hotel_cents"], night["rides_cents"]
        net = oop - time_value + hotel + rides
        lines = [{"label": "Ticket", "cents": int(o.get("ticket_cents", 0))}]
        if refund:
            lines.append({"label": "Possible refund", "cents": -refund})
        if sooner is not None and sooner != 0:
            lines.append({"label": ("Time saved, lands %s sooner" if sooner > 0 else "Time lost, lands %s later") % narrator._hm(abs(sooner)), "cents": -time_value})
        if hotel:
            lines.append({"label": "Hotel near %s tonight" % ((o.get("route") or ["the airport"])[0]), "cents": hotel})
        if rides:
            lines.append({"label": "Rides to the hotel and back", "cents": rides})
        rows.append({"id": o.get("id"), "carrier": o.get("carrier"), "flight": o.get("flight"), "route": o.get("route"),
                     "depart": o.get("depart"), "arrive": o.get("arrive"), "stops": o.get("stops", 0), "grade": o.get("grade"),
                     "ticket_cents": int(o.get("ticket_cents", 0)), "out_of_pocket_cents": oop, "sooner_minutes": sooner,
                     "time_value_cents": time_value, "hotel_cents": hotel, "rides_cents": rides, "night": night,
                     "net_cents": net, "lines": lines})
    rows.sort(key=lambda r: (r["net_cents"], r["arrive"]))
    # one row per FLIGHT: the feed sells each fare family as its own offer; the best-priced fare stands for it
    seen, uniq = set(), []
    for r in rows:
        key = (r.get("flight"), r.get("depart"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    rows = uniq
    best = rows[0] if rows else None
    if best is None:
        action, reason = "wait", "no other flight leaves in time, so stay with the airline"
    elif base_arrive is None:
        action, reason = "switch", "the airline has not given you a new flight or time yet, and this is the cheapest way there"
    elif best["net_cents"] <= -2500:
        action, reason = "switch", "counting any refund and the time saved, switching puts you %s ahead" % narrator._d(-best["net_cents"])
    else:
        action, reason = "stay", "no other flight beats %s, counting cost, refund and time" % base_label
    return {"kind": sit.get("kind"), "baseline": {"label": base_label, "arrive": base_arrive.isoformat() if base_arrive else None},
            "odds": odds(sit, now, options),
            "refund": {"expected": ok, "cents": refund, "why": why, "paid_cents": paid},
            "rights": rights(sit), "options": rows[:5],
            "verdict": {"action": action, "reason": reason, "option": best if action == "switch" else None,
                        "ahead_cents": -best["net_cents"] if (best and action == "switch" and best["net_cents"] < 0) else 0},
            "hotel_tonight": bool(best and best["hotel_cents"]) }


# ------------------------------------------------------------------ the odds
#
# Two lines, both MODELLED and labelled so, both read against the hour of the
# day and both falling as it goes (per Patrick, 2026-09-21: at nine in the
# morning there is a whole day of chances; at ten to midnight there are not):
#
#   the updated flight: if it is still planned for hour h (it may slip there),
#   the chance it goes at all today. A delay grows more often than it shrinks,
#   and the later it gets the likelier the cancellation, because crews time
#   out and the day runs out.
#
#   flying today at all: at hour h, the flight's own chance or one of the
#   alternatives still leaving after it, from the DAY'S OWN RESULTS: every
#   alternative on sale that leaves at least 75 minutes after h is one more
#   way out, and each is given an even chance of taking you. Inventory
#   dwindles through the evening, so the line falls to what the flight alone
#   is worth, and to nothing after the last departure.
#
# A tracking feed for the flight number (a key the box can hold later) would
# replace the priors with the flight's own day.

_SLIP = ((0, 0.55), (60, 0.25), (120, 0.12), (180, 0.08))     # how much further a delayed flight slips, minutes -> share
_ALT_TAKES_YOU = 0.5                                            # each alternative on sale: an even chance it takes you


def _cancel_risk(delay_minutes: int, planned_hour: float) -> float:
    r = 0.04 + max(0.0, delay_minutes - 60) / 60.0 * 0.05
    if planned_hour >= 23:
        r += 0.25
    elif planned_hour >= 21:
        r += 0.12
    return min(0.70, r)


def _flight_goes_today(planned: datetime, delay: int) -> float:
    """The updated flight, planned for `planned` with `delay` behind it: the
    chance it leaves at all today, its slips counted and midnight the end."""
    midnight = planned.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    got = 0.0
    for slip, share in _SLIP:
        at = planned + timedelta(minutes=slip)
        if at < midnight:
            got += share * (1.0 - _cancel_risk(delay + slip, at.hour + at.minute / 60.0))
    return round(got, 3)


def odds(sit: Dict[str, Any], now: Optional[datetime], options: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{flight:{p, curve}, today:{p, curve}, modelled, planned, basis}. Each
    curve is [{hour, p}] for the hours left in the day."""
    planned = _parse(sit.get("rebook_depart")) or _parse(sit.get("new_depart"))
    cancelled = sit.get("kind") == "cancelled" and not planned
    if now is None:
        return {"flight": {"p": None, "curve": []}, "today": {"p": None, "curve": []}, "modelled": True, "planned": None,
                "basis": "no local time came with the request, so the hours cannot be drawn"}
    delay = int(sit.get("delay_minutes") or 0)
    start = now.hour + (1 if now.minute else 0)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = list(range(max(start, 0), 25))
    # the updated flight, by the hour it is planned for
    fcurve = []
    if planned is not None and not cancelled:
        ph = (planned - day0).total_seconds() / 3600.0      # hours since today's midnight: 1:15 AM tomorrow is 25.25, not 1.25
        for h in hours:
            at = planned if h <= ph else planned.replace(hour=min(h, 23), minute=planned.minute if h < 24 else 59)
            slip = 0 if h <= ph else int((h - ph) * 60)
            p = 0.0 if h >= 24 else _flight_goes_today(at, delay + slip)
            fcurve.append({"hour": h, "p": p})
        # a posted departure that has already passed with no departure posted is a flight still slipping:
        # the headline is the curve's first point, not the odds of a time that is gone
        passed = planned < now
        p_flight = fcurve[0]["p"] if (passed and fcurve) else _flight_goes_today(planned, delay)
    else:
        p_flight = 0.0
        passed = False
    # flying today at all, by the hour you are standing there
    deps = []
    for o in options or []:
        d = _parse(o.get("depart"))
        if d is not None and d.date() == now.date():
            deps.append(d)
    tcurve = []
    for h in hours:
        edge = day0 + timedelta(hours=h, minutes=BUFFER_MINUTES)
        n = sum(1 for d in deps if d >= edge)
        p_alt = 1.0 - (1.0 - _ALT_TAKES_YOU) ** n if n else 0.0
        pf = next((k["p"] for k in fcurve if k["hour"] == h), 0.0)
        # never certain: a seat on sale is not a seat in hand, so the line tops out at 97%
        tcurve.append({"hour": h, "p": round(min(0.97, 1.0 - (1.0 - pf) * (1.0 - p_alt)), 3), "alternatives_left": n})
    p_today = next((k["p"] for k in tcurve if k["hour"] >= now.hour + now.minute / 60.0), tcurve[0]["p"] if tcurve else 0.0)
    cancel = _cancel_risk(delay, planned.hour + planned.minute / 60.0) if (planned is not None and not cancelled) else None
    tr = sit.get("tracking") or {}
    hm12 = lambda t: t.strftime("%I:%M %p").lstrip("0")
    upd = _parse(tr.get("last_updated"))
    basis = (("The airline's own status%s: %s%s%s. The rest is still modelled. " % (
                  (" at " + hm12(upd.astimezone(now.tzinfo))) if upd and upd.tzinfo else "", tr.get("status_word"),
                  (", %s late" % narrator._hm(tr["delay_minutes"])) if tr.get("delay_minutes") else "",
                  (", aircraft %s" % tr["aircraft"]) if tr.get("aircraft") else "")
              if tr.get("observed") else "Modelled on the time of day and the delay so far. ")
             + ("Your flight: %s, leaving at %s. Cancellation risk: %d%%. Chance it leaves at the new time: %d%%. %s"
                % (("%s late" % narrator._hm(delay)) if delay else "no delay yet", hm12(planned), round(cancel * 100), round(_SLIP[0][1] * 100),
                   "After midnight it counts as lost. " if planned.date() == now.date() else "")
                if cancel is not None else "Your flight is canceled, so only other flights count. " if sit.get("kind") == "cancelled"
                else "No new departure time yet, so only other flights count. ")
             + "Flying today: each other flight leaving 75+ minutes from now counts as a 50%% chance of a seat. "
               "Flights that qualify: %d." % sum(1 for d in deps if d >= now + timedelta(minutes=BUFFER_MINUTES)))
    return {"flight": {"p": round(p_flight, 2), "curve": fcurve}, "today": {"p": round(p_today, 2), "curve": tcurve},
            "modelled": True, "observed_status": bool(tr.get("observed")), "planned": planned.isoformat() if planned else None,
            "planned_passed": passed,
            "cancel_risk": cancel, "basis": basis}


def _clock(iso: Optional[str]) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else ""


def brief(sit: Dict[str, Any], a: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the advice may say, precomputed, and the closed vocabulary of figures."""
    d = narrator._d
    b = {
        "situation": {"kind": a["kind"], "flight": sit.get("flight"), "route": sit.get("route"),
                      "delay": narrator._hm(sit["delay_minutes"]) if sit.get("delay_minutes") else None,
                      "airline_offered": a["baseline"]["label"], "offered_arrival": _clock(a["baseline"]["arrive"]),
                      "local_time_now": sit.get("now_clock"), "bags_checked": bool(sit.get("bags_checked")),
                      "airline_status": ((sit.get("tracking") or {}).get("status_word") if sit.get("tracking") else None)},
        "decision": {"action": a["verdict"]["action"], "reason": a["verdict"]["reason"],
                     "ahead_by": d(a["verdict"]["ahead_cents"]) if a["verdict"]["ahead_cents"] else None},
        "refund": {"expected": a["refund"]["expected"], "amount": d(a["refund"]["cents"]) if a["refund"]["cents"] else None, "why": a["refund"]["why"]},
        "alternatives": [{"flight": r["flight"], "route": " to ".join(r["route"] or []), "departs": _clock(r["depart"]), "arrives": _clock(r["arrive"]),
                          "ticket": d(r["ticket_cents"]), "out_of_pocket": d(r["out_of_pocket_cents"]) if r["out_of_pocket_cents"] > 0 else "nothing",
                          "sooner": (narrator._hm(r["sooner_minutes"]) if r["sooner_minutes"] and r["sooner_minutes"] > 0 else None),
                          "hotel": d(r["hotel_cents"]) if r["hotel_cents"] else None, "net": d(r["net_cents"]), "stops": r["stops"]}
                         for r in a["options"][:3]],
        "rights": [{"what": r["what"], "detail": r["detail"]} for r in a["rights"]],
        "hotel_tonight": a["hotel_tonight"],
        "odds_you_fly_today": ("%d%%" % round(a["odds"]["today"]["p"] * 100)) if (a.get("odds", {}).get("today") or {}).get("p") is not None else None,
        "odds_the_updated_flight_goes": ("%d%%" % round(a["odds"]["flight"]["p"] * 100)) if (a.get("odds", {}).get("flight") or {}).get("p") is not None and a["odds"]["planned"] else None,
        "odds_are_modelled_not_observed": True,
    }
    blob = narrator._walk(b)
    b["allowed"] = {
        "money": sorted({m for m in re.findall(narrator.MONEY_RE, blob)}),
        "percent": sorted({m for m in re.findall(r"\d+%", blob)}),
        "durations": sorted({m for m in re.findall(r"\d+h\d+m|\b\d+m\b", blob)}),
        "codes": sorted({m for m in re.findall(r"\b[A-Z]{2,3}\s?\d{1,4}\b|\b[A-Z]{3}\b", blob)}),
    }
    return b


def template(b: Dict[str, Any]) -> str:
    """The deterministic floor: true, plain, and always available."""
    dec, s = b["decision"], b["situation"]
    if dec["action"] == "switch" and b["alternatives"]:
        t = b["alternatives"][0]
        out = "Switch to %s, leaving %s and landing %s: %s out of pocket%s. %s." % (
            t["flight"], t["departs"], t["arrives"], t["out_of_pocket"],
            " after the refund" if b["refund"]["expected"] else "", dec["reason"][0].upper() + dec["reason"][1:])
    elif dec["action"] == "stay":
        out = "Stay with %s. %s." % (s["airline_offered"], dec["reason"][0].upper() + dec["reason"][1:])
    else:
        out = "Wait it out. %s." % (dec["reason"][0].upper() + dec["reason"][1:])
    if b.get("odds_you_fly_today") and dec["action"] != "switch":
        out += " Chance you fly today: %s (estimate)." % b["odds_you_fly_today"]
    if b.get("hotel_tonight"):
        out += " The best option leaves tomorrow. Book a hotel near the airport and ask if the airline will pay."
    if b["rights"]:
        out += " You may ask for: " + "; ".join(r["what"] if r["what"][1:2].isupper() else r["what"][0].lower() + r["what"][1:] for r in b["rights"][:3]) + "."
    return out


def narrate(b: Dict[str, Any], allow_model: bool = True) -> Dict[str, str]:
    """The model's advice when it can be had AND checked; the template otherwise."""
    fallback = {"advice": template(b), "source": "template"}
    if not allow_model:
        return fallback
    client, why = narrator._client()
    if client is None:
        fallback["note"] = why
        return fallback
    import json
    try:
        resp = client.messages.create(
            model=narrator.MODEL, max_tokens=700, system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": "BRIEF:\n" + json.dumps(b, indent=1, sort_keys=True)}])
        if resp.stop_reason == "refusal":
            fallback["note"] = "model declined"
            return fallback
        text = "".join(blk.text for blk in resp.content if blk.type == "text")
        advice = str(json.loads(text)["advice"]).strip()
    except Exception as exc:
        fallback["note"] = "model call failed: %s" % type(exc).__name__
        return fallback
    ok, reason = verify(advice, b)
    if not ok:
        fallback["note"] = "rejected: %s" % reason
        return fallback
    return {"advice": advice, "source": "model"}


PROMISES = ["you are owed", "must refund", "will refund", "guaranteed", "the airline must", "you are entitled", "will be refunded"]


def verify(text: str, b: Dict[str, Any]) -> Tuple[bool, str]:
    """The narrator's checks, plus: no promises about rights."""
    low = text.lower()
    for p in PROMISES:
        if p in low:
            return False, "a promise about rights: %r" % p
    return narrator.verify(text, b, max_len=900)
