#!/usr/bin/env python3
"""ConcordeGo's scorer. One number per itinerary, in dollars.

    from scorer import score, score_all, grade
    ledger = score(scenario, option, "reference")

Build step two. Deliberately boring: stdlib only, no imports from the rest of
this project, no reading of files, no network, no clock. `score()` is a pure
function of (scenario, option, profile) and returns a ledger. Hand it the same
dict twice and you get the same integers twice, on any machine, forever. That
is hard rule 1, and it is the only reason the LLM can be let anywhere near the
product: the model shapes the weights going in and writes the prose coming out,
and neither can move a row.

Four things that look like fussiness and are not:

MONEY IS INTEGER CENTS, END TO END. Never a float. "Same data = same order
every time" does not survive floating-point accumulation - two itineraries that
should tie exactly would differ in the last bits and sort by rounding noise
instead of by the documented tie-break. The one division that must happen,
currency conversion, goes through Decimal with an explicit rounding mode.

TIME COMES FROM THE OFFSET IN THE STRING. Every timestamp is RFC3339 with an
explicit numeric offset, so an instant is recoverable with no timezone database
- no I/O, and no tzdata update silently moving a golden number a year from now.
The local wall clock, which layover quality and ground access both need, is read
straight off the same string.

UNKNOWN IS NOT ZERO. Every enrichment term can abstain, and an abstention costs
something. If missing data scored as zero the least-documented itinerary would
accumulate the fewest penalties and win, and the ranking would reward ignorance.

EVERY CONSTANT LIVES HERE, NOT IN A FIXTURE. A fixture carrying the curve it is
meant to be testing is a fixture that tests nothing. Tuning is one frozen
object; pass a different one to see the whole corpus move.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["Line", "Ledger", "Leg", "Tuning", "DEFAULT", "score", "score_all",
           "grade", "report_card", "card_letter", "CARD_WEIGHTS", "CARD_POINTS",
           "timeline", "chosen_ground", "load_scenario"]


# ---------------------------------------------------------------- primitives

def cents(amount_cents: int, fx_rate_to_usd: float) -> int:
    """Convert a minor-unit amount to USD cents. Decimal with an explicit
    rounding mode, because bankers' rounding and float drift both produce
    orders that change between runs."""
    if fx_rate_to_usd == 1.0:
        return int(amount_cents)
    q = Decimal(int(amount_cents)) * Decimal(str(fx_rate_to_usd))
    return int(q.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def instant(rfc3339: str) -> datetime:
    """An aware datetime from a string that carries its own offset. No tz
    database is consulted, which is the entire point."""
    return datetime.fromisoformat(rfc3339)


def minutes_between(a: str, b: str) -> int:
    return int((instant(b) - instant(a)).total_seconds() // 60)


def local_minutes(rfc3339: str) -> int:
    """Minutes past local midnight, read off the wall clock in the string."""
    return int(rfc3339[11:13]) * 60 + int(rfc3339[14:16])


def _hhmm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:5])


def window_covers(hours_local: str, clock_minutes: int) -> bool:
    """Does a strict OPEN window contain this local time? Windows that cross
    midnight are handled; prose is not, by design - the schema forbids it."""
    if hours_local == "24h":
        return True
    try:
        lo, hi = _hhmm(hours_local[:5]), _hhmm(hours_local[6:11])
    except (ValueError, IndexError):
        # A typo in the curated table used to surface as int('al') from four
        # frames down, which tells you nothing about which airport to go and fix.
        raise ValueError(
            "opening hours %r are not HH:MM-HH:MM or '24h'. Service windows are "
            "parsed, not read: prose belongs in a note field, not here." % hours_local)
    if lo <= hi:
        return lo <= clock_minutes <= hi
    return clock_minutes >= lo or clock_minutes <= hi      # wraps midnight


def is_abstain(node: Any) -> bool:
    return isinstance(node, dict) and node.get("coverage") == "none"


def claim_probability(claim: Dict[str, Any]) -> Optional[float]:
    """P(the good case) for a hedged claim, or None if the claim is
    descriptive only. `outcome` is what gives observed_frequency a polarity -
    "no oceanic connectivity, 73% of departures" and "connectivity fitted, 73%
    of departures" are opposite facts behind identical numbers."""
    if is_abstain(claim) or "outcome" not in claim:
        return None
    f = float(claim["observed_frequency"])
    return f if claim["outcome"] else 1.0 - f


# ------------------------------------------------------------------- tuning

@dataclass(frozen=True)
class Tuning:
    """Every curve and constant in the model. All money in cents.

    These are the product's opinions, and they belong in one auditable object
    rather than scattered through the arithmetic or - much worse - inside the
    fixtures they are supposed to be measured against."""

    # --- comfort ---
    # What an hour in each cabin is worth over economy, for pricing a leg flown BELOW the cabin the traveller asked
    # for (2026-09-24, per Patrick: a first-class search that has to use business on a leg is flagged and ranked
    # down, never hidden). A leg above the cabin asked for costs nothing.
    cabin_hour_cents: Tuple[Tuple[str, int], ...] = (("economy", 0), ("premium_economy", 2000),
                                                     ("business", 6500), ("first", 9000))
    connectivity_cents: int = 6000          # a transatlantic crossing with no usable wifi
    cabin_uncertainty_cents: int = 4000     # full penalty when the subfleet is a coin flip
    cabin_certain_at: float = 0.85          # at or above this frequency, no uncertainty cost
    pitch_norm_inches: float = 31.0
    pitch_cents_per_inch: int = 4500
    redeye_cents: int = 9500                # the arrival day is mostly gone
    redeye_accepted_cents: int = 2000       # ... unless the traveller said they are fine
    redeye_depart_after: int = 20 * 60
    redeye_arrive_before: int = 11 * 60
    boarding_last_group_cents: int = 2200   # bins are full by the time you board

    # --- layover ---
    layover_comfortable_minutes: int = 90
    # What an hour of layover costs depends on whether it can be USED. Charging
    # dead time and usable time at one rate is what made a Schiphol afternoon
    # score like a night on the floor at CDG.
    layover_frac_dead: float = 0.70         # nothing open, or ejected landside
    layover_frac_shut: float = 0.55         # no service data either way
    layover_frac_open: float = 0.35         # terminal alive, but you are stuck in it
    layover_frac_leavable: float = 0.10     # you can be somewhere else entirely
    layover_dead_cents: int = 8500          # nothing open at that hour
    layover_forced_landside_cents: int = 6500
    layover_immigration_cents: int = 2500
    layover_ees_first_cents: int = 2000
    layover_security_cents: int = 2000
    layover_inter_terminal: Tuple[Tuple[str, int], ...] = (
        ("walk", 0), ("none", 0), ("airside_shuttle", 1000),
        ("airside_bus", 1500), ("landside_train", 2500), ("landside_bus", 3000))
    layover_leave_airport_credit_cents: int = 9000
    layover_leave_airport_min_usable: int = 180

    # --- connection risk ---
    mct_floor_minutes: int = 45             # used when no MCT is published
    bag_reclaim_minutes: int = 45           # when bags are not checked through
    mct_tight_cents: int = 6000             # scheduled below the published minimum
    delay_p50_default: int = 10             # when reliability abstains
    delay_p90_default: int = 95
    misconnect_floor: float = 0.02
    misconnect_ceiling: float = 0.60
    hotel_night_cents: int = 28000
    rebook_wait_fraction: float = 1.0       # waiting for the next flight is worth the hourly rate

    # --- carrier ---
    # A curated, reviewed rating, never a live opinion. Scored against the
    # route's baseline so it cuts both ways: a good carrier earns a credit.
    carrier_baseline: float = 0.72
    carrier_cents_per_point: int = 26000

    # --- credits ---
    nonstop_credit_cents: int = 8000

    # --- abstention ---
    abstain_reliability_cents: int = 3000   # unknown is not free
    abstain_claim_cents: int = 2500

    # --- lounge ---
    lounge_day_pass_cents: int = 6500
    lounge_worth_after_minutes: int = 150

    # --- the bar ---
    # Thresholds in minutes for green / amber / orange / red, per segment KIND.
    # Deliberately per kind: two hours of flying is not the same news as two
    # hours of layover, and one global scale would say it was.
    band_ground: Tuple[int, int, int] = (45, 70, 100)
    band_process: Tuple[int, int, int] = (50, 75, 110)
    band_flight: Tuple[int, int, int] = (480, 660, 900)
    band_layover: Tuple[int, int, int] = (120, 240, 420)

    # --- grade bands, as a multiple of the route's par ---
    grade_bands: Tuple[Tuple[float, str], ...] = (
        (0.94, "A+"), (1.02, "A"), (1.08, "A-"), (1.15, "B+"), (1.22, "B"),
        (1.30, "B-"), (1.42, "C+"), (1.55, "C"), (1.70, "C-"), (1.90, "D"))


DEFAULT = Tuning()


# ------------------------------------------------------------------- ledger

@dataclass(frozen=True)
class Line:
    """One row of the ledger, and one line of the eventual prose.

    `amount_cents` is signed the way the arithmetic works: positive adds to
    what the trip costs you. The interface renders that as a minus, because
    from the traveller's side a cost is money off. Do not flip it here."""
    code: str
    label: str
    amount_cents: int
    evidence: str = ""
    kind: str = "cost"          # base | cost | credit
    overridable: bool = False
    ref: str = ""               # which layover/segment this line is about, when
                                # there is more than one and "the layover line"
                                # would otherwise be ambiguous


@dataclass(frozen=True)
class Ledger:
    option_id: str
    lines: Tuple[Line, ...]
    effective_cents: int
    door_to_door_minutes: int
    profile: str
    filtered_reason: Optional[str] = None
    infeasible_reason: Optional[str] = None

    def by_code(self, code: str, ref: str = "") -> Optional[Line]:
        for ln in self.lines:
            if ln.code == code and (not ref or ln.ref == ref):
                return ln
        return None

    def all_by_code(self, code: str) -> Tuple[Line, ...]:
        return tuple(ln for ln in self.lines if ln.code == code)

    def reconciles(self) -> bool:
        return sum(ln.amount_cents for ln in self.lines) == self.effective_cents


# --------------------------------------------------------------- the terms

def _ground_pref(scenario: Dict[str, Any]) -> Optional[str]:
    """"Rideshare, yes or no" off the form. None means let the arithmetic
    decide, which is the default and usually the better answer."""
    p = (scenario["query"].get("preferences") or {}).get("ground_mode")
    return p if p in ("rideshare", "transit") else None


def _party_checked_bags(scenario: Dict[str, Any]) -> List[int]:
    return [sum(1 for b in p["bags"] if b["kind"] == "checked")
            for p in scenario["query"]["party"]]


def _ticket_total_cents(ticket: Dict[str, Any]) -> int:
    p = ticket["price"]
    fx = p.get("fx_rate_to_usd", 1.0)
    total = cents(p["base_cents"], fx)
    for bucket in ("taxes", "carrier_imposed", "agency_fees"):
        for ch in p.get(bucket, []):
            total += cents(ch["amount_cents"], fx)
    return total


def _bag_cost(option, scenario, tuning) -> Tuple[int, List[str]]:
    """Bags are a reduction over TICKET TOPOLOGY, not a lookup on a fare name.

    Fees index from the first piece BEYOND the included allowance, tiers reset
    per passenger, and the whole thing is assessed once per ticketing boundary
    - which is why the same two bags on a self-transfer are paid for twice, at
    two carriers' schedules, and why the short leg can cost more than the ocean
    crossing."""
    loads = _party_checked_bags(scenario)
    total, notes = 0, []
    for t in option["tickets"]:
        fx = t["price"].get("fx_rate_to_usd", 1.0)
        included = t["entitlements"]["checked_included"]
        tiers = {x["piece"]: x["amount_cents"] for x in t.get("checked_bag_fee_tiers", [])}
        sub = 0
        for load in loads:
            for piece in range(1, max(0, load - included) + 1):
                if piece not in tiers:
                    raise ValueError(
                        "%s/%s: bag piece %d beyond the allowance is not priced"
                        % (option["option_id"], t["ticket_id"], piece))
                sub += cents(tiers[piece], fx)
        if sub:
            note = "%s %s includes %d checked" % (t["issuing_carrier"],
                                                  t["fare_brand_name"], included)
            tier_note = next((x.get("note") for x in t.get("checked_bag_fee_tiers", [])
                              if x.get("note")), None)
            if tier_note:
                note += " (%s)" % tier_note
            notes.append(note)
        total += sub
    return total, notes


def _pick_ground(modes: Sequence[Dict[str, Any]], hourly: int,
                 prefer: Optional[str] = None):
    """Cheapest by the traveller's own arithmetic, not by fare. A $3.00
    three-transfer route and an $11.75 one-transfer route are different
    products at the same duration, and which one wins is the judgment this
    whole product exists to make.

    `prefer` is a mode CLASS the traveller asked for. It narrows the field
    rather than overriding it: if nothing of that class can be flown at this
    hour the honest answer is the feasible mode plus a note saying the
    preference could not be met, never a silent substitution."""
    feasible = [m for m in modes if m["feasible"]]
    if not feasible:
        why = next((m.get("infeasible_reason") for m in modes
                    if m.get("infeasible_reason")), "no feasible mode")
        return None, why

    def cost(m):
        fare = m["fare_cents"] + (m.get("tolls_cents") or {}).get("outbound", 0)
        return fare + m["door_to_door_minutes"]["p50"] * hourly // 60

    unmet = None
    if prefer:
        wanted = [m for m in feasible if m.get("mode_kind") == prefer]
        if wanted:
            feasible = wanted
        else:
            blocked = [m for m in modes
                       if m.get("mode_kind") == prefer and not m["feasible"]]
            unmet = (blocked[0].get("infeasible_reason") if blocked
                     else "no %s option on this leg" % prefer)
    return min(feasible, key=lambda m: (cost(m), m["mode_id"])), unmet


def chosen_ground(option: Dict[str, Any], prof: Dict[str, Any], which: str = "outbound",
                  prefer: Optional[str] = None):
    """The mode the ledger charged for. The bar reads this too - if the two
    picked independently they would eventually disagree, and the page would be
    lying in one of two places."""
    modes = option["ground"].get(which) or []
    if not modes:
        return None, None
    return _pick_ground(modes, prof["hourly_value_cents"], prefer)


@dataclass(frozen=True)
class Leg:
    """One block of the door-to-door bar. Derived from the same numbers the
    ledger used, never measured separately."""
    kind: str            # ground | process | flight | layover
    label: str
    minutes: int
    quality: str         # good | fair | poor | bad | flight
    detail: str = ""
    tip: str = ""        # the one line the bar shows on hover. Short on purpose.


def _band(minutes: int, band: Tuple[int, int, int]) -> str:
    lo, mid, hi = band
    return ("good" if minutes <= lo else "fair" if minutes <= mid
            else "poor" if minutes <= hi else "bad")


def timeline(scenario: Dict[str, Any], option: Dict[str, Any],
             profile_name: str, tuning: Tuning = DEFAULT) -> Tuple[Leg, ...]:
    """The trip as blocks of time. Its total is the ledger's door-to-door
    figure by construction, which is the property that stops the bar becoming
    a second model with its own opinions."""
    prof = scenario["query"]["profiles"][profile_name]
    segs = {s["segment_id"]: s for s in option["segments"]}
    legs: List[Leg] = []

    out, _ = chosen_ground(option, prof, "outbound", _ground_pref(scenario))
    if out:
        m = out["door_to_door_minutes"]["p50"]
        ap = option["segments"][0]["origin"]["iata"]
        legs.append(Leg("ground", "to " + ap, m, _band(m, tuning.band_ground),
                        "%s - %s" % (out["mode"], _hm(m)),
                        "To %s \u00b7 %s \u00b7 %s" % (ap, _hm(m), out["mode"])))

    proc = (option.get("airport_process_minutes") or {}).get("p50", 0)
    if proc:
        legs.append(Leg("process", "airport", proc, _band(proc, tuning.band_process),
                        "Check-in, security and the walk to the gate at %s"
                        % option["segments"][0]["origin"]["iata"],
                        "At the airport \u00b7 %s" % _hm(proc)))

    for i, seg in enumerate(option["segments"]):
        m = minutes_between(seg["departure_local"], seg["arrival_local"])
        fl = "%s %d" % (seg["marketing"]["carrier"], seg["marketing"]["number"])
        legs.append(Leg("flight", fl, m, "flight",
                        "%s %s to %s - %s on a %s"
                        % (fl, seg["origin"]["iata"], seg["destination"]["iata"],
                           _hm(m), seg.get("equipment_code", "?")),
                        "Flight \u00b7 %s \u00b7 %s" % (fl, _hm(m))))
        lay = next((l for l in option.get("layovers", [])
                    if l["arrive_segment_id"] == seg["segment_id"]), None)
        if lay:
            d = segs[lay["depart_segment_id"]]
            m = minutes_between(seg["arrival_local"], d["departure_local"])
            landed = seg.get("actual_arrival_local") or seg["arrival_local"]
            clock = local_minutes(landed)
            q = _band(m, tuning.band_layover)
            services = lay.get("services_open") or []
            if services and not [x for x in services
                                 if x.get("eligible", True)
                                 and window_covers(x["hours_local"], clock)]:
                q = "bad"                      # nothing open outranks the clock
            legs.append(Leg("layover", "%s %s" % (lay["airport"], _hm(m)), m, q,
                            "%s - %s, landing %02d:%02d local. %s"
                            % (lay["airport"], _hm(m), clock // 60, clock % 60,
                               lay.get("note", "")),
                            "Layover \u00b7 %s \u00b7 %s \u00b7 lands %02d:%02d"
                            % (lay["airport"], _hm(m), clock // 60, clock % 60)))

    arr = option.get("arrival_process_minutes") or {}
    if arr.get("p50"):
        last = option["segments"][-1]["destination"]["iata"]
        names = {"walk": "off the plane and out", "bags": "baggage claim", "border": "passport control and customs"}
        parts = ", ".join("%s %s" % (names.get(k, k), _hm(v)) for k, v in (arr.get("parts") or {}).items())
        legs.append(Leg("arrival", "arrival", arr["p50"], _band(arr["p50"], tuning.band_process),
                        "%s at %s" % (parts or "Out of the airport", last),
                        "Arrival \u00b7 %s" % _hm(arr["p50"])))

    inb, _ = chosen_ground(option, prof, "arrival", _ground_pref(scenario))
    if inb:
        m = inb["door_to_door_minutes"]["p50"]
        dest = scenario["query"]["destination"]["label"].split(",")[0]
        legs.append(Leg("ground", "to " + dest, m, _band(m, tuning.band_ground),
                        "%s - %s" % (inb["mode"], _hm(m)),
                        "To %s \u00b7 %s \u00b7 %s" % (dest, _hm(m), inb["mode"])))
    return tuple(legs)


def _layover_lines(option, scenario, prof, tuning) -> List[Line]:
    lines: List[Line] = []
    segs = {s["segment_id"]: s for s in option["segments"]}
    comfort = float(prof.get("comfort_weight", 1.0))
    w = prof.get("weights") or {}
    w_layover = float(w.get("layover", 1.0))      # "short layovers"
    w_excursion = float(w.get("excursion", 1.0))  # "a day in the city"
    w_lounge = float(w.get("lounge", 1.0))

    for lay in option.get("layovers", []):
        arr = segs[lay["arrive_segment_id"]]
        dep = segs[lay["depart_segment_id"]]
        landed = arr.get("actual_arrival_local") or arr["arrival_local"]
        mins = minutes_between(arr["arrival_local"], dep["departure_local"])
        actual_mins = minutes_between(landed, dep["departure_local"])
        clock = local_minutes(landed)

        it = lay.get("inter_terminal") or {}
        mode = it.get("mode", "none")
        work = it.get("minutes", 0) + (it.get("headway_minutes", 0) or 0)
        if lay.get("immigration_required"):
            work += 20
        if lay.get("security_reclear_required"):
            work += 20
        if lay.get("ees_first_registration"):
            work += 15

        # nothing open is a FACT ABOUT THE DATA, not a flag somebody set
        services = lay.get("services_open") or []
        open_now = [s for s in services
                    if s.get("eligible", True) and window_covers(s["hours_local"], clock)]
        dead = bool(services) and not open_now

        usable = actual_mins - work
        leavable = (lay.get("leave_airport_viable") and not dead
                    and usable >= tuning.layover_leave_airport_min_usable)

        halls = "%s%s -> %s%s" % (arr["destination"].get("terminal") or "",
                                  ("/" + arr["destination"]["hall"]) if arr["destination"].get("hall") else "",
                                  dep["origin"].get("terminal") or "",
                                  ("/" + dep["origin"]["hall"]) if dep["origin"].get("hall") else "")
        pen, why = 0, []
        if halls.strip(" ->/"):
            why.append(halls)
        excess = max(0, mins - tuning.layover_comfortable_minutes)
        if excess:
            frac = (tuning.layover_frac_dead if (dead or lay.get("forced_landside"))
                    else tuning.layover_frac_leavable if leavable
                    else tuning.layover_frac_open if open_now
                    else tuning.layover_frac_shut)
            pen += int(excess * prof["hourly_value_cents"] * frac * w_layover / 60)
            why.append("%s on the ground at %s" % (_hm(mins), lay["airport"]))
        if dead:
            pen += tuning.layover_dead_cents
            why.append("lands %02d:%02d local with nothing open" % (clock // 60, clock % 60))
        if lay.get("forced_landside"):
            pen += tuning.layover_forced_landside_cents
            why.append("pushed landside through immigration with no way back in")
        if lay.get("immigration_required"):
            pen += tuning.layover_immigration_cents
            why.append("re-clear immigration")
        if lay.get("ees_first_registration"):
            pen += tuning.layover_ees_first_cents
            why.append("first EES registration")
        if lay.get("security_reclear_required"):
            pen += tuning.layover_security_cents
            why.append("re-clear security")
        pen += dict(tuning.layover_inter_terminal).get(mode, 0)
        if mode not in ("walk", "none"):
            why.append("terminal change by %s" % mode.replace("_", " "))

        if leavable:
            pen -= int(tuning.layover_leave_airport_credit_cents * w_excursion)

        if leavable:
            out = next((s["what"] for s in open_now if "train" in s["what"].lower()
                        or "metro" in s["what"].lower()), None)
            why.append("long enough and civil enough to leave the airport"
                       + (" - %s" % out if out else ""))

        pen = int(pen * comfort)
        lines.append(Line(
            code="layover",
            ref=lay["airport"],
            label="Layover at %s - %s, landing %02d:%02d local"
                  % (lay["airport"], _hm(actual_mins), clock // 60, clock % 60),
            amount_cents=pen,
            evidence="; ".join(why) or "uneventful",
            kind="credit" if pen < 0 else "cost",
            overridable=True))

        # A lounge is only a credit while it is open AND this passenger may
        # enter it. Crediting one that is shut is how a ledger starts lying.
        if actual_mins >= tuning.lounge_worth_after_minutes:
            lounges = [s for s in services if "lounge" in s["what"].lower()]
            if lounges:
                usable_lounge = [s for s in lounges
                                 if s.get("eligible", True)
                                 and window_covers(s["hours_local"], clock)]
                credit = -int(tuning.lounge_day_pass_cents * w_lounge) if usable_lounge else 0
                allowance = (scenario["query"].get("budget") or {}).get("incidental_allowance_cents") or 0
                if credit and allowance < tuning.lounge_day_pass_cents:
                    credit = 0
                lines.append(Line(
                    code="lounge",
                    ref=lay["airport"],
                    label="Lounge at %s" % lay["airport"],
                    amount_cents=credit,
                    evidence=("day pass within your incidentals" if credit else
                              "nothing you can use is open at %02d:%02d" % (clock // 60, clock % 60)),
                    kind="credit" if credit else "cost",
                    overridable=True))

        # Scheduled below the published minimum. Sold every day; still a defect.
        mct = lay.get("published_mct_minutes")
        if mct and mins < mct:
            lines.append(Line(
                code="mct_margin",
                ref=lay["airport"],
                label="Connection scheduled below the published minimum",
                amount_cents=int(tuning.mct_tight_cents * comfort),
                evidence="%d minutes against a published %d (%s)"
                         % (mins, mct, lay.get("mct_source", "source not stated")),
                overridable=False))
    return lines


def _risk_lines(option, scenario, prof, tuning) -> List[Line]:
    """Probability and severity are separate quantities, and severity is the
    one that matters. Missing a protected connection is a wait; missing a
    self-transfer is a new ticket at the walk-up fare and a hotel."""
    segs = {s["segment_id"]: s for s in option["segments"]}
    lines: List[Line] = []
    risk_w = float(prof.get("risk_weight", 1.0))

    for lay in option.get("layovers", []):
        arr, dep = segs[lay["arrive_segment_id"]], segs[lay["depart_segment_id"]]
        mins = minutes_between(arr["arrival_local"], dep["departure_local"])
        it = lay.get("inter_terminal") or {}
        need = lay.get("published_mct_minutes") or max(tuning.mct_floor_minutes,
                                                       it.get("minutes", 0) + 30)
        if not lay.get("bags_checked_through", True):
            # you collect them, you queue again, you check them in again
            need += tuning.bag_reclaim_minutes
        slack = mins - need

        rel = arr.get("reliability")
        if is_abstain(rel):
            p50, p90 = tuning.delay_p50_default, tuning.delay_p90_default
            src = "no on-time coverage for this segment (%s)" % rel["reason"]
        else:
            p50 = rel.get("delay_minutes_p50", tuning.delay_p50_default)
            p90 = rel.get("delay_minutes_p90", tuning.delay_p90_default)
            src = "on time %d%% over %d departures (%s)" % (
                round(rel["on_time_fraction"] * 100), rel["sample_size"], rel["source"])

        # Two points of a delay distribution, linearly interpolated between
        # them and held flat outside. Crude, documented, and deterministic -
        # which beats an elegant curve nobody can reproduce by hand.
        if slack <= p50:
            p = 0.50 + 0.10 * min(1.0, max(0.0, (p50 - slack) / max(p50, 1)))
        elif slack >= p90:
            p = tuning.misconnect_floor
        else:
            p = 0.50 - 0.40 * (slack - p50) / max(p90 - p50, 1)
        p = min(tuning.misconnect_ceiling, max(tuning.misconnect_floor, p))

        rec = lay.get("recovery") or {"protected": True, "next_departure_minutes": 180,
                                      "overnight_implied": False}
        if rec["protected"]:
            wait = rec.get("next_departure_minutes") or 240
            severity = int(wait * prof["hourly_value_cents"] * tuning.rebook_wait_fraction / 60)
            sev_why = "protected - the carrier rebooks you, about %s lost" % _hm(wait)
        else:
            severity = rec.get("walkup_fare_cents", 0)
            sev_why = ("separate tickets, so there is no rebooking duty at all - "
                       "nobody owes you anything and you buy the leg again")
        if rec.get("overnight_implied"):
            severity += tuning.hotel_night_cents
            sev_why += ", and it implies a hotel night"

        amount = int(p * severity * risk_w)
        lines.append(Line(
            code="risk",
            ref=lay["airport"],
            label="Misconnect risk at %s: %d%% of a %s downside"
                  % (lay["airport"], round(p * 100), _money(severity)),
            amount_cents=amount,
            evidence="%s; %s" % (sev_why, src),
            overridable=False))
    return lines


CABIN_NAMES = {"economy": "economy", "premium_economy": "premium economy", "business": "business", "first": "first class"}


def _cabin_short(option, scenario, prof, tuning) -> Optional[Line]:
    """One line for every leg flown below the cabin the traveller asked for, priced by the hour at the gap between
    the two cabins' hourly worth. None when no cabin was asked for (the fixtures) or every leg meets it."""
    want = str((scenario.get("query") or {}).get("cabin") or "")
    worth = dict(tuning.cabin_hour_cents)
    if want not in worth:
        return None
    comfort = float(prof.get("comfort_weight", 1.0))
    short, cents = [], 0
    for s in option["segments"]:
        have = str(s.get("cabin_marketed") or "economy")
        gap = worth[want] - worth.get(have, 0)
        if gap > 0:
            m = minutes_between(s["departure_local"], s["arrival_local"])
            cents += int(gap * m / 60 * comfort)
            short.append("%s-%s in %s" % (s["origin"]["iata"], s["destination"]["iata"], CABIN_NAMES.get(have, have)))
    if not short:
        return None
    n = len(option["segments"])
    have1 = CABIN_NAMES.get(str(option["segments"][0].get("cabin_marketed") or "economy"), "economy")
    label = ("%s, not %s" % (have1.capitalize(), CABIN_NAMES[want]) if n == 1 else
             "No %s on any flight" % CABIN_NAMES[want] if len(short) == n else
             "Below %s on %d of %d flights" % (CABIN_NAMES[want], len(short), n))
    return Line(code="cabin_short",
                label=label,
                amount_cents=cents,
                evidence="%s; you asked for %s" % (", ".join(short), CABIN_NAMES[want]),
                overridable=True)


def _comfort_lines(option, scenario, prof, tuning) -> List[Line]:
    lines: List[Line] = []
    comfort = float(prof.get("comfort_weight", 1.0))
    weights = prof.get("weights") or {}
    long_haul = max(option["segments"],
                    key=lambda s: minutes_between(s["departure_local"], s["arrival_local"]))
    claims = long_haul.get("claims") or {}
    below = _cabin_short(option, scenario, prof, tuning)
    if below:
        lines.append(below)

    conn = claims.get("connectivity_oceanic")
    if conn is not None:
        if is_abstain(conn):
            lines.append(Line(
                code="connectivity",
                label="Connectivity unknown on the long leg",
                amount_cents=int(tuning.abstain_claim_cents * comfort),
                evidence=conn["reason"] + " - priced as unknown, which is not the same as fine",
                overridable=True))
        else:
            p_good = claim_probability(conn)
            if p_good is not None and p_good < 1.0:
                base = tuning.connectivity_cents * float(weights.get("wifi", 1.0))
                amount = int(base * (1.0 - p_good) * comfort)
                if amount:
                    lines.append(Line(
                        code="connectivity",
                        label="Connectivity unusable over the ocean",
                        amount_cents=amount,
                        evidence="%s on %d%% of %s"
                                 % (conn["value"], round(conn["observed_frequency"] * 100),
                                    conn["observation_window"]),
                        overridable=True))

    sub = claims.get("subfleet")
    if isinstance(sub, dict) and not is_abstain(sub):
        f = float(sub["observed_frequency"])
        if f < tuning.cabin_certain_at:
            short = (tuning.cabin_certain_at - f) / tuning.cabin_certain_at
            lines.append(Line(
                code="cabin_uncertain",
                label="The cabin is close to a coin flip",
                amount_cents=int(tuning.cabin_uncertainty_cents * short * comfort
                                 * float(weights.get("cabin", 1.0))),
                evidence="%s on only %d%% of %s, and equipment is swapped after booking"
                         % (sub["value"], round(f * 100), sub["observation_window"]),
                overridable=True))

    pitch = claims.get("seat_pitch_inches")
    if pitch and pitch < tuning.pitch_norm_inches:
        lines.append(Line(
            code="pitch",
            label="%g inches of pitch on the long leg" % pitch,
            amount_cents=int((tuning.pitch_norm_inches - pitch)
                             * tuning.pitch_cents_per_inch * comfort
                             * float(weights.get("pitch", 1.0))),
            evidence="Below the %g inch transatlantic norm" % tuning.pitch_norm_inches,
            overridable=True))

    first, last = option["segments"][0], option["segments"][-1]
    dep_c, arr_c = local_minutes(first["departure_local"]), local_minutes(last["arrival_local"])
    if dep_c >= tuning.redeye_depart_after and arr_c <= tuning.redeye_arrive_before:
        accepted = bool((scenario["query"].get("preferences") or {}).get("redeye_ok"))
        lines.append(Line(
            code="redeye",
            label="Overnight flight",
            amount_cents=int((tuning.redeye_accepted_cents if accepted
                              else tuning.redeye_cents) * comfort),
            evidence=("you said overnights are fine, so this is priced light"
                      if accepted else "lands %02d:%02d and the arrival day is mostly gone"
                      % (arr_c // 60, arr_c % 60)),
            overridable=True))

    # Boarding position is NOT a baggage rule. Transatlantic Basic Economy
    # carries a full-size bag; what it does is board last, by which point the
    # bins are full. Charging a carry-on fee here would invent money.
    for t in option["tickets"]:
        grp = t["entitlements"].get("boarding_group")
        if grp and any(ch.isdigit() for ch in grp) and int("".join(
                ch for ch in grp if ch.isdigit())) >= 6:
            lines.append(Line(
                code="boarding",
                label="Boards in %s" % grp,
                amount_cents=int(tuning.boarding_last_group_cents * comfort),
                evidence="Bins are usually full by then - a carry-on is allowed, "
                         "finding space for it is the problem",
                overridable=True))
            break

    cr = option.get("carrier_rating")
    if cr:
        delta = tuning.carrier_baseline - float(cr["rating"])
        amount = int(delta * tuning.carrier_cents_per_point * comfort)
        if amount:
            lines.append(Line(
                code="carrier",
                label="%s the route average to fly"
                      % ("Below" if delta > 0 else "Above"),
                amount_cents=amount,
                evidence="curated rating %.2f against a %.2f baseline - %s (%s, as of %s)"
                         % (cr["rating"], tuning.carrier_baseline, cr.get("note", ""),
                            cr["source"], cr["as_of"]),
                kind="credit" if amount < 0 else "cost",
                overridable=True))

    if len(option["segments"]) == 1:
        lines.append(Line(
            code="nonstop",
            label="Nonstop, no misconnect exposure",
            amount_cents=-tuning.nonstop_credit_cents,
            evidence="Nothing to miss",
            kind="credit",
            overridable=False))

    covered = [s for s in option["segments"] if not is_abstain(s.get("reliability"))]
    if not covered:
        lines.append(Line(
            code="reliability",
            label="No on-time record for any segment",
            amount_cents=int(tuning.abstain_reliability_cents),
            evidence="BTS covers US reporting carriers; nothing here is one. Unknown is "
                     "priced, because scoring it as zero would reward missing data",
            overridable=True))
    return lines


# ------------------------------------------------------------------- filters

def filter_reason(option, scenario, prof) -> Optional[str]:
    """Why an option was pushed off the list - never why it vanished. Hard
    rule 4: a filter is the most aggressive demotion there is, so it owes the
    most disclosure, not the least."""
    for f in prof.get("hard_filters") or []:
        if f["kind"] == "carrier":
            names = {t["issuing_carrier"] for t in option["tickets"]}
            names |= {s["marketing"]["carrier"] for s in option["segments"]}
            if f["value"] in names:
                return "you excluded %s" % f["value"]
        elif f["kind"] == "max_price":
            if sum(_ticket_total_cents(t) for t in option["tickets"]) > f["value"]:
                return "over your %s maximum" % _money(f["value"])
        elif f["kind"] == "requires_cabin_bag":
            if any(not t["entitlements"]["cabin_bag_included"] for t in option["tickets"]):
                return "no cabin bag on this fare"
        elif f["kind"] == "no_self_transfer":
            if len(option["tickets"]) > 1:
                return "sold as separate tickets"
        elif f["kind"] == "min_cabin":
            want = str(f["value"]).lower()
            if all(str(sg.get("cabin_marketed", "economy")).lower() != want
                   for sg in option["segments"]):
                return "no %s cabin on this itinerary" % want
        elif f["kind"] == "nonstop_only":
            if len(option["segments"]) > 1:
                return "%d stop%s, and you asked for nonstop"  % (
                    len(option["segments"]) - 1, "" if len(option["segments"]) == 2 else "s")
    budget = (scenario["query"].get("budget") or {}).get("maximum_cents")
    if budget and sum(_ticket_total_cents(t) for t in option["tickets"]) > budget:
        return "over your %s maximum" % _money(budget)
    return None


# --------------------------------------------------------------------- score

def score(scenario: Dict[str, Any], option: Dict[str, Any],
          profile_name: str, tuning: Tuning = DEFAULT) -> Ledger:
    """The whole model. Pure: no clock, no network, no file reads."""
    prof = scenario["query"]["profiles"][profile_name]
    hourly = prof["hourly_value_cents"]
    lines: List[Line] = []

    # --- ticket ---------------------------------------------------------
    ticket_total = sum(_ticket_total_cents(t) for t in option["tickets"])
    brands = ", ".join("%s %s" % (t["issuing_carrier"], t["fare_brand_name"])
                       for t in option["tickets"])
    lines.append(Line(code="ticket", label="Ticket", amount_cents=ticket_total,
                      evidence=brands + (" (two tickets)" if len(option["tickets"]) > 1 else ""),
                      kind="base"))

    # --- bags -----------------------------------------------------------
    bag_total, bag_notes = _bag_cost(option, scenario, tuning)
    bag_total = int(bag_total * float((prof.get("weights") or {}).get("bags", 1.0)))
    if bag_total:
        n = sum(_party_checked_bags(scenario))
        lines.append(Line(
            code="bags",
            label="%d checked bag%s not included" % (n, "" if n == 1 else "s"),
            amount_cents=bag_total,
            evidence="; ".join(bag_notes) + (
                " - two tickets, so the same bags are paid for twice"
                if len(option["tickets"]) > 1 else ""),
            overridable=True))

    # --- ground, both ends ----------------------------------------------
    ground_minutes = 0
    pref = _ground_pref(scenario)
    out_mode, unmet = _pick_ground(option["ground"]["outbound"], hourly, pref)
    if out_mode is None:
        return Ledger(option["option_id"], tuple(lines), 0, 0, profile_name,
                      infeasible_reason=unmet)
    dep_clock = local_minutes(option["segments"][0]["departure_local"])
    rejected = [m for m in option["ground"]["outbound"] if not m["feasible"]]
    ev = "%s - %s" % (out_mode["mode"], _hm(out_mode["door_to_door_minutes"]["p50"]))
    if unmet:
        ev += ". You asked for %s: %s" % (pref, unmet)
    if rejected:
        r = rejected[0]
        ev += ". %s" % (r.get("infeasible_reason") or "another mode was not feasible")
    lines.append(Line(
        code="ground_out",
        label="%s to %s at %02d:%02d" % (
            scenario["query"]["origin"]["label"].split(",")[0],
            option["segments"][0]["origin"]["iata"], dep_clock // 60, dep_clock % 60),
        amount_cents=out_mode["fare_cents"] + (out_mode.get("tolls_cents") or {}).get("outbound", 0),
        evidence=ev, overridable=True))
    ground_minutes += out_mode["door_to_door_minutes"]["p50"]

    in_modes = option["ground"].get("arrival") or []
    if in_modes:
        in_mode, _ = _pick_ground(in_modes, hourly, pref)
        if in_mode:
            lines.append(Line(
                code="ground_in",
                label="%s to %s" % (option["segments"][-1]["destination"]["iata"],
                                    scenario["query"]["destination"]["label"].split(",")[0]),
                amount_cents=in_mode["fare_cents"] + (in_mode.get("tolls_cents") or {}).get("inbound", 0),
                evidence="%s - %s" % (in_mode["mode"], _hm(in_mode["door_to_door_minutes"]["p50"])),
                overridable=True))
            ground_minutes += in_mode["door_to_door_minutes"]["p50"]

    # --- door to door ----------------------------------------------------
    # Ground FARE is the line above; ground TIME is here. Splitting them is
    # what stops the same minutes being charged twice.
    air = sum(minutes_between(s["departure_local"], s["arrival_local"])
              for s in option["segments"])
    segs = {s["segment_id"]: s for s in option["segments"]}
    lay_minutes = 0
    for lay in option.get("layovers", []):
        a, d = segs[lay["arrive_segment_id"]], segs[lay["depart_segment_id"]]
        lay_minutes += minutes_between(a["arrival_local"], d["departure_local"])
    process = (option.get("airport_process_minutes") or {}).get("p50", 0)
    arrival = (option.get("arrival_process_minutes") or {}).get("p50", 0)     # 0 when none is modelled (the fixtures)
    d2d = ground_minutes + process + air + lay_minutes + arrival

    lines.append(Line(
        code="time",
        label="%s door to door at %s an hour" % (_hm(d2d), _money(hourly)),
        amount_cents=d2d * hourly // 60,
        evidence="ground %s + airport %s + air %s%s%s"
                 % (_hm(ground_minutes), _hm(process), _hm(air),
                    " + layover %s" % _hm(lay_minutes) if lay_minutes else "",
                    " + arrival %s" % _hm(arrival) if arrival else ""),
        overridable=False))

    lines.extend(_layover_lines(option, scenario, prof, tuning))
    lines.extend(_risk_lines(option, scenario, prof, tuning))
    lines.extend(_comfort_lines(option, scenario, prof, tuning))

    effective = sum(ln.amount_cents for ln in lines)
    return Ledger(option_id=option["option_id"], lines=tuple(lines),
                  effective_cents=effective, door_to_door_minutes=d2d,
                  profile=profile_name,
                  filtered_reason=filter_reason(option, scenario, prof))


def grade(scenario: Dict[str, Any], option: Dict[str, Any],
          tuning: Tuning = DEFAULT) -> Tuple[str, int]:
    """The grade is this option at the REFERENCE profile against the route's
    par. Same yardstick for everybody, which is the whole reason a bargain can
    sit at the top of a Cheapest list and still show a D."""
    led = score(scenario, option, "reference", tuning)
    par = scenario["query"]["route_par_cents"]
    idx = led.effective_cents / par
    for threshold, letter in tuning.grade_bands:
        if idx <= threshold:
            return letter, led.effective_cents
    return "F", led.effective_cents


# --------------------------------------------------------------- report card
#
# The letter on the page (per Patrick, 2026-09-21: "every plate shows up as an F
# when they're definitely not ... grades that each flight gets objectively in
# each category and not just price"). Six categories, each graded on its OWN
# absolute rubric - price against the route's fair fare, speed against the
# nonstop reference, the ground legs on what they cost and take, comfort on what
# the cabin is, routing on what the connection is, reliability on the record -
# and the whole is their weighted grade-point average. Price is the heaviest
# weight and still only three tenths of the letter, so a flight that is fine in
# every other way is not an F for one dear ticket, and a cheap seat with 28
# inches through a dead layover is not an A.
#
# It is still ABSOLUTE: nothing here looks at the other results, so the same
# flight earns the same card in every search, and test_scorer holds it to that.
# It is deterministic, so the same flight earns the same card twice. And it is
# not the ORDER: the list is ranked by effective cost at the traveller's dial,
# the same as ever; the card says what the flight is, the dial says what it
# costs you. grade() above stays as the model's own value grade (effective cost
# against par), which the ledger tests and the fixtures are written against.

CARD_WEIGHTS: Tuple[Tuple[str, str, float], ...] = (
    ("price", "Price", 0.30), ("speed", "Speed", 0.15), ("ground", "Getting there", 0.10),
    ("comfort", "Comfort", 0.20), ("routing", "Routing", 0.15), ("reliability", "Reliability", 0.10))
CARD_POINTS: Tuple[Tuple[str, float], ...] = (
    ("A+", 4.3), ("A", 4.0), ("A-", 3.7), ("B+", 3.3), ("B", 3.0), ("B-", 2.7),
    ("C+", 2.3), ("C", 2.0), ("C-", 1.7), ("D", 1.0), ("F", 0.0))
# points -> letter, each letter from midway to the next
_LETTER_AT: Tuple[Tuple[float, str], ...] = (
    (4.15, "A+"), (3.85, "A"), (3.5, "A-"), (3.15, "B+"), (2.85, "B"), (2.5, "B-"),
    (2.15, "C+"), (1.85, "C"), (1.5, "C-"), (0.7, "D"))
# a ratio to the reference -> points: the ticket to the fair fare, ...
_PRICE_BANDS: Tuple[Tuple[float, float], ...] = (
    (0.75, 4.3), (0.90, 4.0), (1.00, 3.7), (1.10, 3.3), (1.20, 3.0), (1.30, 2.7),
    (1.45, 2.3), (1.60, 2.0), (1.75, 1.7), (2.00, 1.0))
# ... and the door-to-door time to the nonstop reference's
_SPEED_BANDS: Tuple[Tuple[float, float], ...] = (
    (1.00, 4.3), (1.05, 4.0), (1.10, 3.7), (1.20, 3.3), (1.30, 3.0), (1.45, 2.7),
    (1.60, 2.3), (1.80, 2.0), (2.00, 1.7), (2.40, 1.0))
# a way to or from the airport, on its fare (cents) and, under $15, its time
_GROUND_BANDS: Tuple[Tuple[int, float], ...] = (
    (1500, 4.0), (4000, 3.0), (7000, 2.5), (10000, 2.0), (15000, 1.0))
# the seat: pitch on the long leg, inches -> points; unknown is never the best seat
_PITCH_POINTS: Tuple[Tuple[int, float], ...] = ((34, 4.3), (32, 4.0), (31, 3.5), (30, 3.0), (29, 2.3))
_PITCH_UNKNOWN, _PITCH_FLOOR = 2.7, 1.3
# comfort points taken off at a full shortfall: every flight a whole cabin-worth below the one asked for
_CABIN_SHORT_POINTS = 2.0
# the on-time record of the worst leg -> points; no record reads as a B: not a
# demerit (BTS covers US carriers only, so most of the world has none) and not
# a merit, and a card can still reach A+ past it only with everything else top
_ONTIME_BANDS: Tuple[Tuple[float, float], ...] = (
    (0.85, 4.3), (0.80, 4.0), (0.75, 3.3), (0.70, 2.7), (0.60, 2.0))
_ONTIME_UNKNOWN, _ONTIME_FLOOR = 3.0, 1.0
# with no modelled par basis (a hand-written fixture), the fair fare is the
# share of par the reference fare takes on the modelled routes
_FARE_SHARE_OF_PAR = 0.6


def card_letter(points: float) -> str:
    for at, letter in _LETTER_AT:
        if points >= at:
            return letter
    return "F"


def _card_band(value: float, bands: Sequence[Tuple[float, float]], below: float = 0.0) -> float:
    for top, pts in bands:
        if value <= top:
            return pts
    return below


def _usd(cents: int) -> str:
    return "$%s" % format(int(round(cents / 100.0)), ",d")


def report_card(scenario: Dict[str, Any], option: Dict[str, Any],
                tuning: Tuning = DEFAULT) -> Dict[str, Any]:
    """The six letters and the whole, with a sentence for each. Absolute and
    deterministic: only this option, the route's reference and the rubrics."""
    led = score(scenario, option, "reference", tuning)
    q = scenario["query"]
    prof = q["profiles"]["reference"]
    basis = scenario.get("_par") or {}
    par = int(q["route_par_cents"])
    segments = option["segments"]
    parts: List[Dict[str, Any]] = []

    def part(pid: str, pts: float, why: str) -> None:
        name, weight = next((n, w) for i, n, w in CARD_WEIGHTS if i == pid)
        pts = max(0.0, min(4.3, pts))
        parts.append({"id": pid, "name": name, "letter": card_letter(pts),
                      "points": round(pts, 2), "weight": weight, "why": why})

    # price: the ticket and the party's bags against the fair fare - the modelled
    # reference fare, which already carries the season and the holiday week
    ticket = led.lines[0].amount_cents
    bags = sum(l.amount_cents for l in led.all_by_code("bags"))
    fair = int(basis.get("fare_cents") or par * _FARE_SHARE_OF_PAR)
    ratio = (ticket + max(bags, 0)) / max(fair, 1)
    if ratio < 0.95:
        vs = "%d%% under the fair fare of %s" % (round((1 - ratio) * 100), _usd(fair))
    elif ratio <= 1.05:
        vs = "at the fair fare of %s" % _usd(fair)
    else:
        vs = "%d%% over the fair fare of %s" % (round((ratio - 1) * 100), _usd(fair))
    part("price", _card_band(ratio, _PRICE_BANDS),
         "%s ticket%s, %s" % (_usd(ticket), (" + %s bags" % _usd(bags)) if bags > 0 else "", vs))

    # speed: door to door against the reference itinerary's (nonstop, from the
    # centre); with no basis, the flying time plus the airport hour and a ride each end
    ref_d2d = int((basis.get("reference") or {}).get("door_to_door_minutes") or 0)
    if not ref_d2d:
        ref_d2d = sum(minutes_between(s["departure_local"], s["arrival_local"]) for s in segments) + 150
    d2d = led.door_to_door_minutes
    part("speed", _card_band(d2d / max(ref_d2d, 1), _SPEED_BANDS),
         "%s door to door, the nonstop reference %s" % (_hm(d2d), _hm(ref_d2d)))

    # getting there: each end's chosen way in, on what it costs and how long it takes
    def ground_points(mode: Optional[Dict[str, Any]], key: str) -> Tuple[float, str]:
        if not mode:
            return 1.0, "no way in we could price"
        fare = int(mode.get("fare_cents", 0)) + int((mode.get("tolls_cents") or {}).get(key, 0) or 0)
        mins = int(mode["door_to_door_minutes"]["p50"])
        pts = _card_band(fare, _GROUND_BANDS)
        if fare <= 1500 and mins <= 75:
            pts = 4.3
        if mode.get("support") == "assumed":          # a number we could not model, said so
            pts = min(pts, 3.0)
        return pts, "%s %s, %s" % (_usd(fare), str(mode.get("mode", "")).lower(), _hm(mins))
    pref = _ground_pref(scenario)
    ends: List[Tuple[float, str]] = []
    out_mode, _ = chosen_ground(option, prof, "outbound", pref)
    ends.append(ground_points(out_mode, "outbound"))
    if option["ground"].get("arrival"):
        in_mode, _ = chosen_ground(option, prof, "arrival", pref)
        ends.append(ground_points(in_mode, "inbound"))
    part("ground", sum(p for p, _ in ends) / len(ends), " · ".join(w for _, w in ends))

    # comfort: the seat on the long leg, the cabin, wifi and power as the airline
    # publishes them, the carrier's reviewed rating, and a red-eye
    long_leg = max(segments, key=lambda s: minutes_between(s["departure_local"], s["arrival_local"]))
    claims = long_leg.get("claims") or {}
    pitch = claims.get("seat_pitch_inches")
    why: List[str] = []

    def seat_points(sg) -> float:
        p = (sg.get("claims") or {}).get("seat_pitch_inches")
        v = _PITCH_UNKNOWN
        if p:
            v = _PITCH_FLOOR
            for at, pp in _PITCH_POINTS:
                if p >= at:
                    v = pp
                    break
        c = str(sg.get("cabin_marketed") or "economy").lower()
        return max(v, 4.3) if c in ("business", "first") else max(v, 3.7) if c.startswith("premium") else v

    # the seat on EVERY flight, weighted by its time in the air (2026-09-24, per Patrick): a business long-haul
    # with two economy hops is not a business trip, and grading only the long leg said it was
    mins = [max(1, minutes_between(sg["departure_local"], sg["arrival_local"])) for sg in segments]
    pts = sum(seat_points(sg) * m for sg, m in zip(segments, mins)) / sum(mins)
    why.append("%g-inch pitch" % pitch if pitch else "pitch unknown")
    cabins = [str(sg.get("cabin_marketed") or "economy").lower() for sg in segments]
    if len(set(cabins)) == 1:
        if cabins[0] != "economy":
            why.append(CABIN_NAMES.get(cabins[0], cabins[0]))
    else:
        why.append(" and ".join(CABIN_NAMES.get(c, c) for c in sorted(set(cabins), key=lambda c: -dict(tuning.cabin_hour_cents).get(c, 0))))
    # below the cabin asked for: points off by how far below and for how much of the trip, flights and time
    # blended, so a business search that is economy on two of three flights reads a letter lower (per Patrick)
    want = str(q.get("cabin") or "")
    worth = dict(tuning.cabin_hour_cents)
    if worth.get(want):
        gaps = [max(0, worth[want] - worth.get(c, 0)) / worth[want] for c in cabins]
        share = 0.5 * sum(g * m for g, m in zip(gaps, mins)) / sum(mins) + 0.5 * sum(gaps) / len(gaps)
        if share > 0:
            pts -= _CABIN_SHORT_POINTS * share
            why.append("%d of %d flights below %s" % (sum(1 for g in gaps if g > 0), len(gaps), CABIN_NAMES.get(want, want)))
    sids = {s.get("segment_id") for s in segments}
    wifi = [w for w in ((scenario.get("_feed") or {}).get("wifi_published") or []) if w.get("segment") in sids]
    if any(w.get("available") and w.get("cost") == "free" for w in wifi):
        pts += 0.3; why.append("free wifi")
    elif any(w.get("available") for w in wifi):
        pts += 0.1; why.append("paid wifi" if any(w.get("cost") == "paid" for w in wifi) else "wifi")
    elif wifi:
        pts -= 0.2; why.append("no wifi")
    power = str(claims.get("power") or "")
    if power.startswith("Power"):
        pts += 0.2; why.append("power at the seat")
    elif power.startswith("No power"):
        pts -= 0.2; why.append("no power")
    cr = option.get("carrier_rating") or {}
    rating = cr.get("rating")
    if rating is None:
        pts -= 0.2; why.append("unrated carrier")
    else:
        rating = float(rating)
        pts += 0.4 if rating >= 0.85 else 0.1 if rating >= tuning.carrier_baseline else -0.2 if rating >= 0.6 else -0.5
        why.append("carrier rated %.2f" % rating)
    if led.by_code("redeye"):
        pts -= 0.6; why.append("red-eye")
    if led.by_code("cabin_uncertain"):
        pts -= 0.2; why.append("cabin uncertain")
    part("comfort", pts, ", ".join(why))

    # routing: nonstop, or what the connection is - its hours, its margin, what
    # a miss costs, whether it is one ticket
    stops = len(segments) - 1
    if stops == 0:
        part("routing", 4.3, "nonstop")
    else:
        pts = 3.3 if stops == 1 else 2.3
        why = []
        segs = {s["segment_id"]: s for s in segments}
        for lay in option.get("layovers", []):
            arr, dep = segs[lay["arrive_segment_id"]], segs[lay["depart_segment_id"]]
            why.append("%s at %s" % (_hm(minutes_between(arr["arrival_local"], dep["departure_local"])),
                                     arr["destination"]["iata"]))
        lay_pen = sum(l.amount_cents for l in led.all_by_code("layover"))
        risk = sum(l.amount_cents for l in led.all_by_code("risk"))
        if lay_pen >= 8000:
            pts -= 1.2; why.append("dead hours")
        elif lay_pen >= 4000:
            pts -= 0.6; why.append("a rough layover")
        elif lay_pen >= 1500:
            pts -= 0.3
        if risk >= 4000:
            pts -= 1.0; why.append("a real misconnect exposure")
        elif risk >= 2000:
            pts -= 0.5; why.append("some misconnect exposure")
        elif risk >= 800:
            pts -= 0.2
        if led.by_code("mct_margin"):
            pts -= 0.5; why.append("tight against the minimum connection")
        if len(option.get("tickets", [])) > 1:
            pts -= 1.0; why.append("separate tickets")
        part("routing", pts, ", ".join(why))

    # reliability: the on-time record of the worst leg; a chain is as good as its weakest
    records = [s["reliability"] for s in segments
               if isinstance(s.get("reliability"), dict) and not is_abstain(s["reliability"])
               and s["reliability"].get("on_time_fraction") is not None]
    if records:
        worst = min(records, key=lambda r: float(r["on_time_fraction"]))
        f = float(worst["on_time_fraction"])
        pts = _ONTIME_FLOOR
        for at, p in _ONTIME_BANDS:
            if f >= at:
                pts = p
                break
        part("reliability", pts, "on time %d%% over %s departures%s" % (
            round(f * 100), format(int(worst.get("sample_size") or 0), ",d"),
            " on the weakest leg" if len(records) > 1 else ""))
    else:
        part("reliability", _ONTIME_UNKNOWN, "no on-time record for this flight")

    points = sum(p["points"] * p["weight"] for p in parts)
    return {"grade": card_letter(points), "points": round(points, 2), "parts": parts}


def score_all(scenario: Dict[str, Any], profile_name: str,
              tuning: Tuning = DEFAULT) -> List[Ledger]:
    """Every option, ordered. Filtered options are RETURNED, flagged - the
    caller discloses them. Ties break on option_id so the order is total."""
    out = [score(scenario, o, profile_name, tuning) for o in scenario["options"]]
    out.sort(key=lambda l: (l.infeasible_reason is not None,
                            l.effective_cents, l.option_id))
    return out


# ------------------------------------------------------------------ helpers

def _hm(minutes: int) -> str:
    h, m = minutes // 60, minutes % 60
    return "%dh%02dm" % (h, m) if h else "%dm" % m


def _money(c: int) -> str:
    return "$%s" % format(abs(round(c / 100)), ",d")


def load_scenario(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def render(led: Ledger, scenario: Dict[str, Any]) -> str:
    """The ledger as the brief writes it. A dollar figure, a cause, and the
    evidence where the claim is probabilistic."""
    opt = next(o for o in scenario["options"] if o["option_id"] == led.option_id)
    letter, _ = grade(scenario, opt)
    head = "%s  %s  %s ticket  %s effective  [%s]" % (
        led.option_id,
        " -> ".join([opt["segments"][0]["origin"]["iata"]]
                    + [s["destination"]["iata"] for s in opt["segments"]]),
        _money(led.lines[0].amount_cents), _money(led.effective_cents), letter)
    rows = []
    for ln in led.lines[1:]:
        if ln.amount_cents == 0:
            continue
        sign = "+" if ln.amount_cents < 0 else "-"
        rows.append("  %s %7s  %s" % (sign, _money(ln.amount_cents), ln.label))
    if led.filtered_reason:
        rows.append("    (hidden: %s)" % led.filtered_reason)
    return head + "\n" + "\n".join(rows)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    import glob
    for path in sorted(glob.glob(os.path.join(here, "..", "fixtures", "0*.json"))):
        sc = load_scenario(path)
        print("\n=== %s" % sc["title"])
        for prof in sc["query"]["profiles"]:
            print("\n-- %s" % prof)
            for led in score_all(sc, prof):
                print(render(led, sc))
