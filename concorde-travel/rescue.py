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
- Plain, calm, specific. No exclamation marks.

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


def refund_rights(sit: Dict[str, Any]) -> Tuple[bool, str]:
    """Can the traveller expect the unused ticket back if they do not fly it?
    US DOT's 2024 rule: a cancelled flight, or a delay of three hours at home
    and six abroad, and you choose not to travel. Elsewhere: cancelled, yes;
    delayed, ask."""
    kind = sit.get("kind")
    if kind == "cancelled":
        return True, "the flight was cancelled and you choose not to take the airline's alternative, so the unused ticket may be refunded in full"
    delay = sit.get("delay_minutes")
    intl = bool(sit.get("international"))
    need = US_INTL_REFUND_MINUTES if intl else US_DOMESTIC_REFUND_MINUTES
    if sit.get("us") and delay is not None and delay >= need:
        return True, ("a delay of %s on a US %s flight is 'significant' under the DOT rule, so the unused ticket may be refunded if you choose not to travel"
                      % (narrator._hm(delay), "international" if intl else "domestic"))
    return False, ("a delay under %s is not 'significant' under the US rule; buying another ticket means paying twice unless the airline agrees to refund"
                   % narrator._hm(need) if sit.get("us") else "outside the US rules, a refund for a delay is the airline's call; ask before you buy")


def rights(sit: Dict[str, Any]) -> List[Dict[str, str]]:
    """What the rules say the traveller may ask for. Stated as 'may', decided by the airline."""
    out = []
    ok, why = refund_rights(sit)
    out.append({"what": "A refund of the unused ticket", "may": ok, "detail": why})
    if sit.get("kind") == "cancelled" or (sit.get("delay_minutes") or 0) >= 120:
        out.append({"what": "Rebooking at no charge", "may": True,
                    "detail": "the airline's own next flight, and on a partner if it has an agreement; ask for the earliest, not the first offered"})
    if sit.get("eu"):
        km = int(sit.get("distance_km") or 0)
        comp = "€600" if km > 3500 else "€400" if km > 1500 else "€250" if km else "€250 to €600 by distance"
        out.append({"what": "EU261 compensation of %s" % comp, "may": True,
                    "detail": "for a cancellation with under two weeks' notice or arriving three hours late or more, unless the cause was extraordinary (weather, air traffic control, strikes outside the airline)"})
        out.append({"what": "Meals, and a hotel if you wait overnight", "may": True,
                    "detail": "the EU duty of care applies whatever the cause; keep receipts"})
    elif sit.get("us"):
        out.append({"what": "A meal, and a hotel overnight, if the cause was the airline's", "may": True,
                    "detail": "most US carriers commit to it in their customer service plan for controllable delays and cancellations; weather is not covered"})
    if sit.get("bags_checked"):
        out.append({"what": "Your checked bag", "may": True,
                    "detail": "if you switch airlines the bag does not follow; ask the desk to pull it, or expect it delivered later"})
    return out


def assess(sit: Dict[str, Any], options: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """The decision. `options` are the page's result entries (ticket_cents,
    depart, arrive with offsets, stops, carrier, flight, route, grade)."""
    hourly = int(sit.get("hourly_value_cents") or 3500)
    paid = int(sit.get("paid_cents") or 0)
    ok, why = refund_rights(sit)
    refund = paid if ok else 0
    base_arrive = _parse(sit.get("rebook_arrive")) or (_parse(sit.get("new_arrive")) if sit.get("kind") == "delayed" else None)
    base_label = ("the rebooking the airline offered" if sit.get("rebook_arrive")
                  else "the delayed flight's new arrival" if base_arrive else "nothing: the airline has offered no way on yet")
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
        hotel = HOTEL_CENTS if (now is not None and dep.date() > now.date() and (dep - now) >= timedelta(hours=7) and now.hour >= 19) else 0
        net = oop - time_value + hotel
        lines = [{"label": "Ticket", "cents": int(o.get("ticket_cents", 0))}]
        if refund:
            lines.append({"label": "Less the refund you may expect", "cents": -refund})
        if sooner is not None and sooner != 0:
            lines.append({"label": ("Lands %s sooner" if sooner > 0 else "Lands %s later") % narrator._hm(abs(sooner)) + " at your rate", "cents": -time_value})
        if hotel:
            lines.append({"label": "A hotel tonight, it leaves tomorrow", "cents": hotel})
        rows.append({"id": o.get("id"), "carrier": o.get("carrier"), "flight": o.get("flight"), "route": o.get("route"),
                     "depart": o.get("depart"), "arrive": o.get("arrive"), "stops": o.get("stops", 0), "grade": o.get("grade"),
                     "ticket_cents": int(o.get("ticket_cents", 0)), "out_of_pocket_cents": oop, "sooner_minutes": sooner,
                     "time_value_cents": time_value, "hotel_cents": hotel, "net_cents": net, "lines": lines})
    rows.sort(key=lambda r: (r["net_cents"], r["arrive"]))
    best = rows[0] if rows else None
    if best is None:
        action, reason = "wait", "nothing else we can find leaves in time; stay with the airline and hold it to its plan"
    elif base_arrive is None:
        action, reason = "switch", "the airline has offered no way on; this is the cheapest way there once the refund you may expect is counted"
    elif best["net_cents"] <= -2500:
        action, reason = "switch", "counting the refund you may expect and the hours it saves at your rate, switching comes out %s ahead" % narrator._d(-best["net_cents"])
    else:
        action, reason = "stay", "no alternative beats %s once its ticket, the refund you may expect and the hours are counted" % base_label
    return {"kind": sit.get("kind"), "baseline": {"label": base_label, "arrive": base_arrive.isoformat() if base_arrive else None},
            "refund": {"expected": ok, "cents": refund, "why": why, "paid_cents": paid},
            "rights": rights(sit), "options": rows[:5],
            "verdict": {"action": action, "reason": reason, "option": best if action == "switch" else None,
                        "ahead_cents": -best["net_cents"] if (best and action == "switch" and best["net_cents"] < 0) else 0},
            "hotel_tonight": bool(best and best["hotel_cents"]) }


def _clock(iso: Optional[str]) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else ""


def brief(sit: Dict[str, Any], a: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the advice may say, precomputed, and the closed vocabulary of figures."""
    d = narrator._d
    b = {
        "situation": {"kind": a["kind"], "flight": sit.get("flight"), "route": sit.get("route"),
                      "delay": narrator._hm(sit["delay_minutes"]) if sit.get("delay_minutes") else None,
                      "airline_offered": a["baseline"]["label"], "offered_arrival": _clock(a["baseline"]["arrive"]),
                      "local_time_now": sit.get("now_clock"), "bags_checked": bool(sit.get("bags_checked"))},
        "decision": {"action": a["verdict"]["action"], "reason": a["verdict"]["reason"],
                     "ahead_by": d(a["verdict"]["ahead_cents"]) if a["verdict"]["ahead_cents"] else None},
        "refund": {"expected": a["refund"]["expected"], "amount": d(a["refund"]["cents"]) if a["refund"]["cents"] else None, "why": a["refund"]["why"]},
        "alternatives": [{"flight": r["flight"], "route": " to ".join(r["route"] or []), "departs": _clock(r["depart"]), "arrives": _clock(r["arrive"]),
                          "ticket": d(r["ticket_cents"]), "out_of_pocket": d(r["out_of_pocket_cents"]),
                          "sooner": (narrator._hm(r["sooner_minutes"]) if r["sooner_minutes"] and r["sooner_minutes"] > 0 else None),
                          "hotel": d(r["hotel_cents"]) if r["hotel_cents"] else None, "net": d(r["net_cents"]), "stops": r["stops"]}
                         for r in a["options"][:3]],
        "rights": [{"what": r["what"], "detail": r["detail"]} for r in a["rights"]],
        "hotel_tonight": a["hotel_tonight"],
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
            " with the refund counted" if b["refund"]["expected"] else "", dec["reason"][0].upper() + dec["reason"][1:])
    elif dec["action"] == "stay":
        out = "Stay with %s. %s." % (s["airline_offered"], dec["reason"][0].upper() + dec["reason"][1:])
    else:
        out = "Wait it out. %s." % (dec["reason"][0].upper() + dec["reason"][1:])
    if b.get("hotel_tonight"):
        out += " The way on leaves tomorrow morning, so book a hotel near the airport tonight and ask the desk whether the airline covers it."
    if b["rights"]:
        out += " Ask for: " + "; ".join(r["what"].lower() for r in b["rights"][:3]) + "."
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
