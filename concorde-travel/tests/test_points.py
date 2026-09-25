#!/usr/bin/env python3
"""Points and miles: the published award charts (points.py over enrichment/points.json) and the page's transfer
planner, offline.

    python3 concorde-travel/tests/test_points.py

What matters here is mostly what the engine REFUSES to do (2026-09-24, per Patrick: no sample data): price a
programme that publishes no chart, price a connection a chart's rules do not cover, price an award cheaper than
the chart when the rule is unclear, or plan a transfer with a bonus that has ended. The planner half runs the
page's own JavaScript under node and skips, saying so, without node.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import points                                               # noqa: E402

FAILS, N = [], [0]


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + (("  — " + detail) if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


# airports with coordinates, so nothing here needs the network
F = {
    "JFK": {"lat": 40.6413, "lon": -73.7781, "country": "US", "city": "New York"},
    "EWR": {"lat": 40.6895, "lon": -74.1745, "country": "US", "city": "Newark"},
    "BOS": {"lat": 42.3656, "lon": -71.0096, "country": "US", "city": "Boston"},
    "LAX": {"lat": 33.9416, "lon": -118.4085, "country": "US", "city": "Los Angeles"},
    "ATL": {"lat": 33.6407, "lon": -84.4277, "country": "US", "city": "Atlanta"},
    "HNL": {"lat": 21.3187, "lon": -157.9225, "country": "US", "city": "Honolulu"},
    "LHR": {"lat": 51.4700, "lon": -0.4543, "country": "GB", "city": "London"},
    "CDG": {"lat": 49.0097, "lon": 2.5479, "country": "FR", "city": "Paris"},
    "FRA": {"lat": 50.0379, "lon": 8.5622, "country": "DE", "city": "Frankfurt"},
    "MAD": {"lat": 40.4983, "lon": -3.5676, "country": "ES", "city": "Madrid"},
    "IST": {"lat": 41.2753, "lon": 28.7519, "country": "TR", "city": "Istanbul"},
    "BOG": {"lat": 4.7016, "lon": -74.1469, "country": "CO", "city": "Bogota"},
    "NRT": {"lat": 35.7720, "lon": 140.3929, "country": "JP", "city": "Tokyo"},
    "SIN": {"lat": 1.3644, "lon": 103.9915, "country": "SG", "city": "Singapore"},
}


def seg(a, b, op, cabin="economy", date="2026-11-18", mk=None):
    return {"from": a, "to": b, "op": op, "mk": mk or op, "cabin": cabin, "date": date}


def priced(segs, prog=None, facts=F):
    r = points.awards(segs, facts)
    return [a for a in r["priced"] if prog is None or a["prog"] == prog], r["bookable"]


def main():
    P = points.load()
    # --- the data file itself
    check("every chart names its programmes, its source and as-of date, and says how it prices",
          all(ch.get("programmes") and ch.get("url") and ch.get("as_of") and ch.get("rule") in points.RULES for ch in P["charts"].values()))
    check("every transfer row has a ratio, a minimum and a block size; a bonus has an end date",
          all(r["from"] > 0 and r["to"] > 0 and r["min"] and r["step"] for b in P["banks"].values() for r in b["partners"].values())
          and all(re.match(r"^\d{4}-\d{2}-\d{2}$", x["ends"]) for b in P["banks"].values() for x in b["bonuses"]))
    check("a programme with no published value is unknown, never zero",
          all(p["cents"] is None or p["cents"] > 0 for p in P["programmes"].values()) and P["programmes"]["lh"]["cents"] is None)
    pt = points.page_table()
    check("the page gets banks, programmes, status bags and the valuation source, never the charts or the geography",
          set(pt) == {"as_of", "valuation", "banks", "programmes", "avios_family", "status_bags"} and "charts" not in pt)

    # --- Delta One to London with Virgin Points: the region table, the season from Virgin's calendar
    off, dyn = priced([seg("JFK", "LHR", "DL", "business", "2026-11-18")], "vs")
    pk, _ = priced([seg("JFK", "LHR", "DL", "business", "2026-12-20")], "vs")
    check("Delta One New York-London with Virgin Points: 47,500 off-peak, 57,500 on a peak date (Virgin's calendar)",
          [a["miles"] for a in off] == [47500] and [a["miles"] for a in pk] == [57500], str((off, pk)))
    check("Delta, Flying Blue and Aeromexico can book a Delta flight but publish no chart: named, never priced",
          {"dl", "af"} <= set(dyn) and not priced([seg("JFK", "LHR", "DL", "business")], "dl")[0])
    up, _ = priced([seg("LHR", "JFK", "DL", "business", "2026-11-25")], "vs")
    check("from the UK, Virgin's printed cash is carried as a known fee ($1,027.80 in Delta One)",
          up and up[0].get("fee_usd") == 1027.8 and up[0].get("fee_known"), str(up))
    conn, _ = priced([seg("ATL", "JFK", "DL", "business"), seg("JFK", "LHR", "DL", "business")], "vs")
    check("a Delta connection is not a transatlantic nonstop: no Virgin price", not conn, str(conn))
    reg, _ = priced([seg("BOS", "JFK", "9E", "economy", mk="DL")], "vs")
    check("a Delta Connection flight (Endeavor, 9E) books as Delta", reg and reg[0]["miles"] == 7500, str(reg))

    # --- British Airways: its own route table by tier, per flight; its carrier charges on any programme
    ba, _ = priced([seg("JFK", "LHR", "BA", "economy", "2026-11-18")])
    own = [a for a in ba if a["prog"] == "ba"]
    check("BA's own New York-London: 33,000 Avios at peak, 27,500 on an off-peak date (the calendar is BA's)",
          own and own[0]["miles"] == 33000 and own[0].get("low") == 27500, str(own))
    check("British Airways' carrier charges are flagged whichever programme books its flight",
          ba and all(a["surcharge"] == "high" for a in ba), str([(a["prog"], a["surcharge"]) for a in ba]))
    aa, _ = priced([seg("JFK", "LHR", "AA", "economy")], "ba")
    short, _ = priced([seg("BOS", "JFK", "AA", "economy")], "ba")
    check("BA Avios on American: 23,000 across the Atlantic; a hop within North America prices as the second band",
          aa and aa[0]["miles"] == 23000 and short and short[0]["miles"] == 18000, str((aa, short)))
    check("American's own flights are bookable with AAdvantage, never priced by it (no chart since 2023)",
          not priced([seg("JFK", "LHR", "AA", "economy")], "aa")[0] and "aa" in priced([seg("JFK", "LHR", "AA", "economy")])[1])

    # --- AAdvantage on partners: Europe off-peak only outbound, and never through a third region
    o1, _ = priced([seg("JFK", "LHR", "BA", "economy", "2026-11-18")], "aa")
    o2, _ = priced([seg("JFK", "LHR", "BA", "economy", "2026-10-01")], "aa")
    o3, _ = priced([seg("LHR", "JFK", "BA", "economy", "2026-11-18")], "aa")
    check("AAdvantage partner economy to Europe: 22,500 off-peak (Nov 1-Dec 14) outbound, 30,000 otherwise and inbound",
          [a["miles"] for a in o1 + o2 + o3] == [22500, 30000, 30000], str([a["miles"] for a in o1 + o2 + o3]))

    # --- Aeroplan: zones and the distance flown; United and Air Canada are dynamic
    lh, _ = priced([seg("EWR", "FRA", "LH", "business"), seg("FRA", "LHR", "CL", "business", mk="LH")], "ac")
    ua, dua = priced([seg("EWR", "LHR", "UA", "business")], "ac")
    check("Aeroplan on Lufthansa: North America-Atlantic by the miles flown (4,263 -> 75,000), CityLine as Lufthansa",
          lh and lh[0]["miles"] == 75000, str(lh))
    check("Aeroplan on United is a select partner, priced like Air Canada's own: bookable, never charted",
          not ua and "ac" in dua, str((ua, dua)))
    _, blh = priced([seg("EWR", "FRA", "LH", "business"), seg("FRA", "LHR", "LH", "business")])
    check("a programme whose chart does not cover a routing is not called chartless: Turkish's partner chart is "
          "nonstop only, so a Lufthansa connection lists no 'tk'", "tk" not in blh and "ua" in blh, str(blh))

    # --- routing: a connection through a third region is not a two-region award
    third, _ = priced([seg("JFK", "BOG", "AV", "economy"), seg("BOG", "LHR", "AV", "economy")])
    progs = {a["prog"] for a in third}
    check("New York-Bogota-London on Avianca: no ANA or Miles & More price (third region); Aeroplan by distance flown",
          "nh" not in progs and "lh" not in progs and "ac" in progs, str(progs))

    # --- Virgin on Air France: a connection takes the higher of journey and flight by flight
    af, _ = priced([seg("JFK", "CDG", "AF", "business"), seg("CDG", "LHR", "AF", "business")], "vs")
    check("Virgin on Air France via Paris: the higher of the zone price and flight by flight (48,500 + 8,000)",
          af and af[0]["miles"] == 56500, str(af))

    # --- nonstop-only rules, and floors
    at, _ = priced([seg("JFK", "LHR", "AA", "business")], "as")
    atc, _ = priced([seg("BOS", "JFK", "AA", "business"), seg("JFK", "LHR", "AA", "business")], "as")
    check("Atmos on American: 45,000 nonstop as a published floor with its $20 fee; no price on a connection",
          at and at[0]["miles"] == 45000 and at[0].get("floor") and at[0].get("fee_usd") == 25.6 and not atc, str((at, atc)))
    tk, _ = priced([seg("JFK", "IST", "TK", "economy"), seg("IST", "LHR", "TK", "economy")], "tk")
    check("Turkish's own flights add up through Istanbul: 55,000 + 20,000, from 55,000 at the Promotion level",
          tk and tk[0]["miles"] == 75000 and tk[0].get("low") == 55000, str(tk))

    # --- missing facts: nothing priced, still bookable
    nof, bk = priced([seg("JFK", "XXX", "DL", "economy")])
    check("an airport with no coordinates prices nothing and still names who can book it", not nof and "dl" in bk, str(bk))

    pool()
    planner()
    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    for f in FAILS:
        print("  FAIL " + f)
    sys.exit(1 if FAILS else 0)


def pool():
    """The flights where a chart buys the most ticket per mile reach the page whatever their cash rank: the pool is
    cut before anyone's points are known (a $10,494 Delta One that 47,500 Virgin points buy was left out)."""
    sys.path.insert(0, os.path.join(HERE, "..", "ui", "mock"))
    import data as mockdata
    raw = json.load(open(os.path.join(HERE, "..", "adapter_samples", "duffel-jfk-lhr.json"), encoding="utf-8"))
    keep = (mockdata.TOP_PER_TARGET, mockdata.CHEAPEST_TICKETS, mockdata.POOL)
    try:
        mockdata.TOP_PER_TARGET, mockdata.CHEAPEST_TICKETS, mockdata.POOL = 1, 1, 1
        out = mockdata.build_slim(raw)
    finally:
        mockdata.TOP_PER_TARGET, mockdata.CHEAPEST_TICKETS, mockdata.POOL = keep
    rows = {e["id"]: e for v in out["results"].values() if isinstance(v, list) for e in v}
    per = lambda e: max([e["ticket_cents"] / a["miles"] for a in e["awards"]] or [0])
    full = mockdata.build_slim(raw)
    allrows = {e["id"]: e for v in full["results"].values() if isinstance(v, list) for e in v}
    top = sorted((e for e in allrows.values() if per(e) > 0), key=lambda e: -per(e))[:3]
    check("with the pool cut to one flight per target, the flights where points go furthest are still on the page",
          top and all(e["id"] in rows for e in top), str([(e["flight"], round(per(e), 2)) for e in top]))
    check("every flight on the page carries its chart prices and the programmes with no chart",
          all("awards" in e and "award_bookable" in e for e in allrows.values()))


def planner():
    """The page's own transfer planner, run under node with the page's points code and the real transfer table."""
    node = shutil.which("node")
    if not node:
        print("  skip the planner checks: no node on this machine")
        return
    src = open(os.path.join(HERE, "..", "ui", "mock", "mock-10.src.html"), encoding="utf-8").read()
    a = src.index("// ---- points and miles")
    b = src.index("const PLANS = new Map();")
    code = src[a:b].replace("__POINTS__", json.dumps(points.page_table()))
    harness = """
const S = {pts:{}, status:{}, legPick:{}, legIdx:0, pax:'1 adult'};
function paxCount(){ return 1; }
const CURRENCIES = {USD:{rate:1}, CAD:{rate:1.4}};
%s
const todayFixed = '2026-09-24';
todayLocal = () => todayFixed;
const e = {id:'x', ticket_cents: 1000000};
const plan = (pts, award) => { for (const k of Object.keys(S.pts)) S.pts[k] = 0; Object.assign(S.pts, pts); return planAward(e, award, {}); };
const vs = {prog:'vs', miles:47500, surcharge:'may'};
const out = {
  amex: plan({amex:60000}, vs),
  marriott: plan({marriott:150000}, vs),
  combo: plan({chase:30000, bilt:50000}, vs),
  own: plan({vs:20000, amex:60000}, vs),
  short: plan({amex:30000}, vs),
  bonus: plan({amex:100000}, {prog:'ba', miles:68500, surcharge:'may'}),
  citi: plan({citi_basic:80000}, vs),
  order: plan({citi_basic:30000, capone:30000}, vs),
  high: plan({amex:100000}, {prog:'vs', miles:47500, surcharge:'high'}),
  avios: plan({qr:10000, ba:0, amex:60000}, {prog:'ba', miles:23000, surcharge:'may'}),
  aeroplan: plan({chase:100000}, {prog:'ac', miles:75000, surcharge:'may', fee_local:{amount:39, currency:'CAD'}}),
};
todayLocal = () => '2026-09-28';
out.after = plan({amex:100000}, {prog:'ba', miles:68500, surcharge:'may'});
const slim = p => ({ok:p.ok, short:p.short, own:p.own, moves:p.moves.map(m => [m.kind, m.from, m.send, m.arrive]), worth:p.worth, known:p.known, head:p.head, good:p.good});
console.log(JSON.stringify(Object.fromEntries(Object.entries(out).map(([k, v]) => [k, slim(v)]))));
""" % code
    # the block uses helpers defined elsewhere in the page: the few it needs are stubbed above or here
    harness = "const esc = s => String(s); const money = c => '$' + Math.round(c / 100); const moneyPt = c => c + 'c';\n" + harness
    harness = harness.replace("const todayLocal = ", "let todayLocal = ")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(harness)
    r = subprocess.run([node, fh.name], capture_output=True, text=True)
    os.unlink(fh.name)
    if r.returncode:
        check("the planner runs", False, r.stderr[-800:])
        return
    o = json.loads(r.stdout)
    check("Amex to Virgin for 47,500: 48,000 sent in blocks of 1,000, worth $960 at 2c", o["amex"]["moves"] == [["bank", "amex", 48000, 48000]] and o["amex"]["worth"] == 96000, str(o["amex"]))
    check("Marriott to Virgin: 120,000 Bonvoy make 40,000 plus 10,000 bonus miles (5,000 per 60,000 moved at once)",
          o["marriott"]["moves"] == [["bank", "marriott", 120000, 50000]], str(o["marriott"]))
    check("two banks combine, the cheaper per mile first: 30,000 Chase (2.05c) then 18,000 Bilt (2.2c)",
          o["combo"]["moves"] == [["bank", "chase", 30000, 30000], ["bank", "bilt", 18000, 18000]], str(o["combo"]))
    check("miles already in the programme are used first, the rest transferred", o["own"]["own"] == 20000 and o["own"]["moves"] == [["bank", "amex", 28000, 28000]], str(o["own"]))
    check("not enough points says how many short, and never recommends", not o["short"]["ok"] and o["short"]["short"] == 17500 and not o["short"]["good"], str(o["short"]))
    check("a running transfer bonus counts (Amex to BA +30% until Sept 27: 53,000 make 68,900)", o["bonus"]["moves"] == [["bank", "amex", 53000, 68900]], str(o["bonus"]))
    check("an ended bonus never counts (Sept 28: 69,000 Amex for 68,500 Avios)", o["after"]["moves"] == [["bank", "amex", 69000, 69000]], str(o["after"]))
    check("Citi's other cards move 1,000:700: 68,000 make 47,600", o["citi"]["moves"] == [["bank", "citi_basic", 68000, 47600]], str(o["citi"]))
    check("the cheaper points per arriving mile go first: Capital One at 1:1 before Citi's 1,000:700",
          o["order"]["moves"][0][1] == "capone" and o["order"]["moves"][1] == ["bank", "citi_basic", 25000, 17500], str(o["order"]))
    check("points are never recommended on a flight whose carrier charges can eat the saving",
          o["high"]["ok"] and o["high"]["head"] > 0 and not o["high"]["good"], str(o["high"]))
    check("Avios in another Avios account move 1:1 before any card points", o["avios"]["moves"][0] == ["combine", "qr", 10000, 10000], str(o["avios"]))
    check("a fee in another currency is counted at today's rate (CA$39 at 1.4 = $27.86)", o["aeroplan"]["known"] == 2786, str(o["aeroplan"]))


if __name__ == "__main__":
    main()
