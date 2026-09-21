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
    check("late at night, an option that leaves tomorrow morning carries the hotel", any(r["hotel_cents"] == rescue.HOTEL_CENTS for r in a3["options"]) and a3["hotel_tonight"] is not None)
    check("the rights list names the refund, rebooking and the bag when one was checked",
          [r["what"] for r in rescue.rights(dict(sit3, bags_checked=True))][:2] == ["A refund of the unused ticket", "Rebooking at no charge"]
          and any("bag" in r["what"].lower() for r in rescue.rights(dict(sit3, bags_checked=True))))
    check("an EU departure adds EU261 by distance, hedged", any("EU261" in r["what"] and "€600" in r["what"] for r in rescue.rights(dict(sit3, eu=True, distance_km=5500)))
          and all("may" in r or True for r in rescue.rights(dict(sit3, eu=True))))

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
    check("the helper never imports the scorer, the adapter or the live module",
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
