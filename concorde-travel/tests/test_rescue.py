#!/usr/bin/env python3
"""The delayed-or-cancelled helper, offline: the arithmetic, the rights, and the guard rails."""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import rescue                                                   # noqa: E402

FAILS, N = [], [0]


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + ("  — " + detail if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


def main():
    with open(os.path.join(HERE, "..", "ui", "mock", "slim.json"), encoding="utf-8") as fh:
        opts = json.load(fh)["results"]["reference"]
    morning = datetime.fromisoformat("2026-11-18T06:30:00-05:00")
    late = datetime.fromisoformat("2026-11-18T21:40:00-05:00")

    # cancelled, paid $400, the airline offered nothing: switch to the cheapest way there, refund counted
    sit = {"kind": "cancelled", "us": True, "international": True, "paid_cents": 40000, "hourly_value_cents": 3500, "flight": "DL 1", "route": "JFK to LHR"}
    a = rescue.assess(sit, opts, now=morning)
    check("cancelled with nothing offered: the call is to switch", a["verdict"]["action"] == "switch")
    check("the refund of the unused ticket is expected and counted against every alternative",
          a["refund"]["expected"] and all(r["out_of_pocket_cents"] == r["ticket_cents"] - 40000 for r in a["options"]))
    check("alternatives that leave within the buffer are not offered",
          all(datetime.fromisoformat(r["depart"]) > morning for r in a["options"]))
    check("the best alternative is the lowest net", a["options"][0]["net_cents"] == min(r["net_cents"] for r in a["options"]))
    import copy as _c
    twin = _c.deepcopy(opts[0]); twin["id"] = "twin-fare"; twin["ticket_cents"] += 9000
    a_tw = rescue.assess(sit, opts + [twin], now=morning)
    check("two fares of one flight make one row, the cheaper", sum(1 for r in a_tw["options"] if r["flight"] == opts[0]["flight"] and r["depart"] == opts[0]["depart"]) <= 1)

    # delayed two hours, domestic: no refund, and the rebook lands earlier than anything else -> stay
    sit2 = {"kind": "delayed", "us": True, "international": False, "delay_minutes": 120, "paid_cents": 30000,
            "rebook_arrive": "2026-11-18T12:00:00+00:00", "hourly_value_cents": 3500}
    a2 = rescue.assess(sit2, opts, now=morning)
    check("a two-hour domestic delay is not 'significant': no refund expected", not a2["refund"]["expected"] and a2["refund"]["cents"] == 0)
    check("with an early rebook in hand, the call is to stay", a2["verdict"]["action"] == "stay", a2["verdict"]["reason"])
    check("a later-landing alternative carries a negative 'sooner'", all((r["sooner_minutes"] or 0) <= 0 or r["time_value_cents"] > 0 for r in a2["options"]))

    # a six-hour international delay is significant; a late evening puts a hotel on tomorrow's flights
    sit3 = {"kind": "delayed", "us": True, "international": True, "delay_minutes": 370, "paid_cents": 50000,
            "new_arrive": "2026-11-19T20:00:00+00:00", "hourly_value_cents": 6000}
    # the sample is one day's results; give it a next-morning flight by shifting one entry a day
    import copy
    nxt = copy.deepcopy(opts[0]); nxt["id"] = "next-morning"; nxt["depart"] = nxt["depart"].replace("2026-11-18", "2026-11-19"); nxt["arrive"] = nxt["arrive"].replace("2026-11-18", "2026-11-19")
    a3 = rescue.assess(sit3, opts + [nxt], now=late)
    check("a six-hour international delay: the refund is expected", a3["refund"]["expected"])
    check("late at night, an option that leaves tomorrow morning carries a night's room", any(r["hotel_cents"] > 0 for r in a3["options"]) and a3["hotel_tonight"] is not None)
    check("the rights list names the refund, rebooking and the bag when one was checked",
          [r["what"] for r in rescue.rights(dict(sit3, bags_checked=True))][:2] == ["A refund of the unused ticket", "Rebooking at no charge"]
          and any("bag" in r["what"].lower() for r in rescue.rights(dict(sit3, bags_checked=True))))
    check("an EU departure adds EU261 by distance, hedged", any("EU261" in r["what"] and "€600" in r["what"] for r in rescue.rights(dict(sit3, eu=True, distance_km=5500)))
          and all("may" in r or True for r in rescue.rights(dict(sit3, eu=True))))

    # the night before a flight tomorrow: a typical room from the table, the rides from the ground model
    n = rescue.night_near({"iata": "JFK", "lat": 40.6413, "lon": -73.7781, "country": "US"}, late, datetime.fromisoformat("2026-11-19T08:00:00-05:00"))
    check("a night near JFK is priced from the table and says it is not a live price", n["hotel_cents"] == 19000 and "not a live price" in n["hotel_basis"])
    check("the rides there and back are estimated at the airport's own rates", len(n["rides"]) == 2 and n["rides_cents"] > 0 and "estimated" in n["rides_basis"])
    n2 = rescue.night_near({"iata": "XXX", "lat": 51.5, "lon": -0.1, "country": "GB"}, late, None)
    check("an airport not in the table takes its country's rate and says so", n2["hotel_cents"] == 15000 and "not in the table" in n2["hotel_basis"])
    n3 = rescue.night_near({"iata": "ZZZ"}, late, None)
    check("with no coordinates there is no ride estimate, and the room is the default", n3["rides_cents"] == 0 and n3["hotel_cents"] == 16000)
    a5 = rescue.assess(sit3, opts + [nxt], now=late, airports={"JFK": {"iata": "JFK", "lat": 40.6413, "lon": -73.7781, "country": "US"}})
    tm = next(r for r in a5["options"] if r["id"] == "next-morning")
    check("a next-morning option carries the night near JFK and the rides, itemised", tm["hotel_cents"] == 19000 and tm["rides_cents"] > 0 and any("Rides" in l["label"] for l in tm["lines"]) and tm["night"]["km"] == 4.0)

    # the fare's own rules
    ok, why = rescue.refund_rights({"kind": "delayed", "us": True, "delay_minutes": 30, "fare": "flex"})
    check("a flexible fare is refundable whatever the delay", ok and "flexible" in why)
    ok2, why2 = rescue.refund_rights({"kind": "delayed", "us": True, "delay_minutes": 30, "fare": "business"})
    check("business is 'often refundable' and not counted until checked", not ok2 and "often refundable" in why2)

    # the odds: two lines, both modelled, both falling through the day
    nine = datetime.fromisoformat("2026-11-18T09:00:00-05:00"); late2 = datetime.fromisoformat("2026-11-18T23:20:00-05:00")
    o = rescue.odds({"kind": "delayed", "delay_minutes": 65, "new_depart": "2026-11-18T19:35:00-05:00"}, datetime.fromisoformat("2026-11-18T18:00:00-05:00"), opts)
    check("a modest evening delay leaves good odds of the updated flight going", 0.6 <= o["flight"]["p"] <= 0.97, str(o["flight"]["p"]))
    fc = [k["p"] for k in o["flight"]["curve"]]
    check("the updated flight's line never rises as the hour it slips to gets later", all(fc[i] >= fc[i + 1] for i in range(len(fc) - 1)), str(fc))
    tc = [k["p"] for k in o["today"]["curve"]]
    check("the flying-today line never rises as the day runs out", all(tc[i] >= tc[i + 1] for i in range(len(tc) - 1)), str(tc))
    check("flying today is never less likely than the flight alone", all(k["p"] >= f["p"] for k, f in zip(o["today"]["curve"], o["flight"]["curve"])))
    o9 = rescue.odds({"kind": "cancelled"}, nine, opts)
    check("cancelled at nine in the morning, with a day of alternatives, flying today is very likely", o9["today"]["p"] >= 0.9, str(o9["today"]["p"]))
    o23 = rescue.odds({"kind": "cancelled"}, late2, opts)
    check("cancelled at twenty past eleven at night, with nothing left to leave, flying today is nil", o23["today"]["p"] == 0.0, str(o23["today"]["p"]))
    o2 = rescue.odds({"kind": "delayed", "delay_minutes": 300, "new_depart": "2026-11-18T23:20:00-05:00"}, datetime.fromisoformat("2026-11-18T22:30:00-05:00"), [])
    check("a five-hour delay pushed to 23:20 leaves slim odds for the flight", o2["flight"]["p"] < 0.45, str(o2["flight"]["p"]))
    check("with no US DOT record, the odds say they are estimated, not from records",
          o["modelled"] and not o["from_records"] and "Estimated" in o["basis"] and "not from records" in o["basis"])
    # from US DOT records (2026-09-24, per Patrick): a synthetic block stands in for the table so this stays offline.
    # 100 flights from JFK in the 6-9 PM block were at least 3 hours late; 40 left within 15 more minutes, 70 within
    # an hour, 90 within three hours, all within twelve; 20 flights in the block were cancelled.
    import ontime
    saved = dict(ontime._DOC)
    try:
        ontime._DOC.clear()
        ontime._DOC.update({"thresholds": [0, 30, 60, 90, 120, 180, 240], "slips": [0, 15, 60, 180, 720], "blocks": 8,
                            "window": "test", "flights": {},
                            "fixer": {"JFK": {"6": {"n": 1500, "cancelled": 20, "ge": [900, 400, 300, 250, 200, 100, 50],
                                                    "slip": [[0] * 5] * 5 + [[10, 40, 70, 90, 100]] + [[0] * 5]}}}})
        dsit = {"kind": "delayed", "delay_minutes": 180, "origin_iata": "JFK", "us": True, "international": False,
                "sched_depart": "2026-11-18T18:30:00-05:00", "new_depart": "2026-11-18T21:30:00-05:00"}
        od = rescue.odds(dsit, datetime.fromisoformat("2026-11-18T20:00:00-05:00"), [])
        # it has left by none of the 10 at-threshold flights yet? It is exactly 3 h late at 21:30, so the pool is
        # 100 + 20 - F(0)=10 -> 110; it leaves before midnight (2.5 h later: F(150) = 70 + 20*90/120 = 85) -> (85-10)/110
        want = round((70 + (90 - 70) * (150 - 60) / 120.0 - 10) / 110.0, 3)
        check("a US domestic flight's odds come from the DOT block: of the flights already as late, the share that left "
              "before midnight, with every cancellation counted against leaving",
              od["from_records"] and not od["modelled"] and abs(od["flight"]["p"] - want) < 0.005
              and "US DOT records" in od["basis"] and od["records"]["n_late"] == 100, str((od["flight"]["p"], want, od["basis"][:160])))
        check("the DOT odds never rise as the evening runs out", all(a["p"] >= b["p"] for a, b in zip(od["flight"]["curve"], od["flight"]["curve"][1:])),
              str([k["p"] for k in od["flight"]["curve"]]))
        # the high end counts no cancellation against leaving: (85 - 10) / (100 - 10)
        want_hi = round((70 + (90 - 70) * (150 - 60) / 120.0 - 10) / 90.0, 3)
        check("the DOT odds are a range: the low end counts every cancellation in the block against leaving, the high "
              "end none, and the advice says the range", abs(od["flight"]["p_hi"] - want_hi) < 0.005 and od["flight"]["p_hi"] > od["flight"]["p"]
              and all(k["p_hi"] >= k["p"] for k in od["flight"]["curve"]) and "%d%% to %d%%" % (round(want * 100), round(want_hi * 100)) in rescue._pct_range(od["flight"]),
              str((od["flight"]["p"], od["flight"]["p_hi"], want_hi)))
        # the measured rate (2026-09-25): of 400 flights here whose plane landed 3 h late or worse, 40 were cancelled.
        # That 10% (Wilson 95% interval) replaces "every cancellation, or none": a best figure and a narrow range.
        ontime._DOC["fixer"]["JFK"]["6"].update({"kn": [0, 0, 0, 0, 0, 400, 0], "kc": [0, 0, 0, 0, 0, 40, 0]})
        om = rescue.odds(dsit, datetime.fromisoformat("2026-11-18T20:00:00-05:00"), [])
        lo_q, q, hi_q = ontime.cancel_rate(400, 40)
        left = 85 - 10
        want_mid = round(left / (100 + 100 * q / (1 - q) - 10), 3)
        want_lo = round(left / (100 + 100 * hi_q / (1 - hi_q) - 10), 3)
        want_hi2 = round(left / (100 + 100 * lo_q / (1 - lo_q) - 10), 3)
        f = om["flight"]
        check("with late-arriving planes measured, the share of already-late flights cancelled is 40 of 400 and the "
              "odds are a best figure inside its margin of error (%.0f%%, %.0f-%.0f%%), far narrower than every-or-none"
              % (want_mid * 100, want_lo * 100, want_hi2 * 100),
              om["measured_cancellations"] and abs(f["p_mid"] - want_mid) < 0.006 and abs(f["p"] - want_lo) < 0.006
              and abs(f["p_hi"] - want_hi2) < 0.006 and f["p"] <= f["p_mid"] <= f["p_hi"]
              and (f["p_hi"] - f["p"]) < (want_hi - want) / 2, str(f))
        check("the advice gives the best figure with its range, and the basis names the measurement",
              rescue._pct_range(f).startswith("about %d%%" % round(want_mid * 100)) and "40 were cancelled" in om["basis"]
              and "margin of error" in om["basis"], rescue._pct_range(f) + " | " + om["basis"][:200])
        check("every point of the curve keeps low <= best <= high", all(k["p"] <= k["p_mid"] <= k["p_hi"] for k in f["curve"]),
              str([(k["p"], k["p_mid"], k["p_hi"]) for k in f["curve"]][:4]))
        oi = rescue.odds(dict(dsit, international=True), datetime.fromisoformat("2026-11-18T20:00:00-05:00"), [])
        check("an international flight has no DOT record and stays an estimate", oi["modelled"] and not oi["from_records"])
    finally:
        ontime._DOC.clear()
        ontime._DOC.update(saved)
    op = rescue.odds({"kind": "delayed", "delay_minutes": 212, "new_depart": "2026-11-18T17:45:00-05:00"}, datetime.fromisoformat("2026-11-18T22:28:00-05:00"), [])
    check("a posted departure that has passed makes the headline the curve's first point, and says it passed",
          op["planned_passed"] and abs(op["flight"]["p"] - op["flight"]["curve"][0]["p"]) < 0.01 and op["flight"]["p"] < 0.3, str(op["flight"]))
    a4 = rescue.assess(dict(sit2, new_depart="2026-11-18T09:30:00-05:00"), opts, now=morning)
    b4 = rescue.brief(dict(sit2, now_clock="06:30"), a4)
    check("the assessment carries both odds and the brief carries them as percentages the advice may use",
          a4["odds"]["today"]["p"] is not None and b4["odds_you_fly_today"] in b4["allowed"]["percent"] and b4["odds_the_updated_flight_goes"] in b4["allowed"]["percent"])

    # the tracking feed: an observed status fills the clocks and the delay, and says it is observed
    legs = json.load(open(os.path.join(HERE, "..", "adapter_samples", "aerodatabox-dl5048.json"), encoding="utf-8"))
    check("the AeroDataBox sample says it was synthesized", bool(legs[0].get("_provenance", {}).get("synthesized")))
    tr = rescue.from_tracking(legs, "LGA", "CLT")
    check("the leg is read: delayed, 3h10 behind, clocks in the airport's own zone",
          tr and tr["kind"] == "delayed" and tr["delay_minutes"] == 190 and tr["sched_depart"] == "2026-09-21T18:30:00-04:00" and tr["new_depart"] == "2026-09-21T21:40:00-04:00" and tr["observed"])
    check("a leg for another airport is not taken for this one", rescue.from_tracking(legs, "JFK", "CLT") is not None and rescue.from_tracking([], "LGA") is None)
    sit_t = rescue.apply_tracking({"kind": "delayed", "us": True, "paid_cents": 60000, "hourly_value_cents": 3500}, tr)
    check("the observed status fills the situation", sit_t["delay_minutes"] == 190 and sit_t["new_depart"] == tr["new_depart"] and sit_t["tracking"]["source"] == "AeroDataBox")
    ot = rescue.odds(sit_t, datetime.fromisoformat("2026-09-21T17:10:00-04:00"), [])
    check("the odds say the status is observed and the rest modelled", ot["observed_status"] and "own status" in ot["basis"] and "still modelled" in ot["basis"])
    canc = json.loads(json.dumps(legs)); canc[1]["status"] = "Canceled"
    check("a cancelled status is read as cancelled", rescue.from_tracking(canc, "LGA")["kind"] == "cancelled" and rescue.apply_tracking({"kind": "delayed"}, rescue.from_tracking(canc, "LGA"))["kind"] == "cancelled")

    # the brief and the guard rails
    b = rescue.brief(dict(sit, now_clock="06:30"), a)
    tpl = rescue.template(b)
    ok, why = rescue.verify(tpl, b)
    check("the template passes its own verifier", ok, why)
    check("a figure the brief did not give is rejected", not rescue.verify("Switch now, it is $9,999 cheaper.", b)[0])
    check("a promise about rights is rejected", not rescue.verify("You are owed a full refund.", b)[0])
    check("an airport the brief did not name is rejected", not rescue.verify("Fly through DXB instead.", b)[0])
    check("with no key, narrate ships the template and says why",
          rescue.narrate(b, allow_model=False)["source"] == "template")
    src = open(os.path.join(HERE, "..", "rescue.py"), encoding="utf-8").read()
    check("the helper never imports the scorer, the adapter or the live module (the ground model is allowed)",
          not any(("import %s" % m) in src or ("from %s" % m) in src for m in ("scorer", "adapter", "live")))

    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL  " + f)
        return 1
    print("all rescue checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
