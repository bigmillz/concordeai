#!/usr/bin/env python3
"""Real records in place of guesses (2026-09-24, per Patrick): hotel prices from Google Hotels and on-time records
from US DOT, offline.

    python3 concorde-travel/tests/test_records.py

Synthetic answers stand in for the network and the built table, so nothing here spends a call or needs the DOT
files. What matters is what each refuses: a hotel average from two hotels, a holiday rental or a hotel across town;
an on-time share that forgets cancellations; a regional flight that misses its parent's record.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import adapter                                              # noqa: E402
import ontime                                               # noqa: E402
import server                                               # noqa: E402

FAILS, N = [], [0]


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + (("  — " + detail) if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


def prop(rate, lat=51.47, lon=-0.45, kind="hotel"):
    return {"type": kind, "gps_coordinates": {"latitude": lat, "longitude": lon}, "rate_per_night": {"extracted_lowest": rate}}


def main():
    # ---- hotels: the median of real hotels near the place
    near = [prop(r) for r in (60, 75, 90, 110, 400)]
    far = [prop(20, lat=51.75, lon=-0.10)]          # about 35 km away
    rentals = [prop(15, kind="vacation rental")]
    h = server.hotel_summary({"properties": near + far + rentals}, 51.4700, -0.4543)
    check("hotel price: the MEDIAN of hotels within 10 km ($90 of 60, 75, 90, 110, 400), never a mean, never a rental "
          "or a hotel across town", h and h["cents"] == 9000 and h["n"] == 5, str(h))
    check("hotel price: fewer than three priced hotels near the place is no answer",
          server.hotel_summary({"properties": [prop(80), prop(90), far[0]]}, 51.47, -0.4543) is None)
    check("hotel price: with no point to measure from (a typed place), every priced hotel counts",
          (server.hotel_summary({"properties": near + far}) or {}).get("n") == 6)
    check("hotel lookup: a bad date or no place is refused before any call",
          "error" in server.hotels_request({"near": ["LHR"], "date": ["18/11/2026"]})
          and "error" in server.hotels_request({"date": ["2026-11-18"]}))

    # ---- DOT records: a synthetic table stands in for enrichment/ontime.json.gz
    saved = dict(ontime._DOC)
    try:
        ontime._DOC.clear()
        ontime._DOC.update({"window": "2025-08 to 2026-07", "thresholds": [0, 30], "slips": [0, 60], "blocks": 8,
                            "flights": {"9E|5048|LGA|CLT": [300, 240, 12, 3, 58], "AA|*|JFK|MIA": [5000, 3900, 100, 5, 70],
                                        "AA|100|JFK|MIA": [10, 10, 0, 0, 0]}, "fixer": {}})
        c = ontime.claim("9E", "DL", "5048", "LGA", "CLT")
        check("DOT record: on time is arrivals within 15 minutes over everything SCHEDULED, cancellations included "
              "(240 of 300 -> 0.8)", c and c["on_time_fraction"] == 0.8 and c["sample_size"] == 300 and c["delay_minutes_p90"] == 58, str(c))
        r = ontime.claim(None, "DL", "5048", "LGA", "CLT", {"DL": ["9E", "OO"]})
        check("DOT record: a Delta Connection flight with no operator named is found under Delta's regional airlines",
              r and r["sample_size"] == 300, str(r))
        rt = ontime.claim("AA", "AA", "2099", "JFK", "MIA")
        check("DOT record: a flight number with no record of its own falls back to the airline's route, and says so",
              rt and rt["sample_size"] == 5000 and "route" in rt["observation_window"], str(rt))
        check("DOT record: a flight abroad has none", ontime.claim("BA", "BA", "117", "JFK", "LHR") is None)
        sc = {"options": [{"segments": [{"marketing": {"carrier": "AA", "number": 2099}, "operating": {"carrier": "AA"},
                                         "origin": {"iata": "JFK"}, "destination": {"iata": "MIA"},
                                         "reliability": {"coverage": "none", "policy": "route_median", "reason": "x"}},
                                        {"marketing": {"carrier": "BA", "number": 117}, "operating": {"carrier": "BA"},
                                         "origin": {"iata": "JFK"}, "destination": {"iata": "LHR"},
                                         "reliability": {"coverage": "none", "policy": "route_median", "reason": "y"}}]}]}
        adapter.join_ontime(sc)
        segs = sc["options"][0]["segments"]
        check("a search's flights take their DOT record where there is one and keep their priced abstain where not",
              segs[0]["reliability"].get("on_time_fraction") == 0.78 and segs[1]["reliability"].get("coverage") == "none",
              str([s["reliability"] for s in segs]))
    finally:
        ontime._DOC.clear()
        ontime._DOC.update(saved)

    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    for f in FAILS:
        print("  FAIL " + f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
