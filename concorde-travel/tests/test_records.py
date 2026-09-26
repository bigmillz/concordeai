#!/usr/bin/env python3
"""Real records in place of guesses (2026-09-24, per Patrick): hotel prices from Google Hotels and on-time records
from US DOT, offline.

    python3 concorde-travel/tests/test_records.py

Synthetic answers stand in for the network and the built table, so nothing here spends a call or needs the DOT
files. What matters is what each refuses: a hotel average from two hotels, a holiday rental or a hotel across town;
an on-time share that forgets cancellations; a regional flight that misses its parent's record.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import adapter                                              # noqa: E402
import addons                                               # noqa: E402
import ground                                               # noqa: E402
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

    # ---- the inbound step (2026-09-25): a tiny synthetic month. Plane N1 lands at JFK at 18:40, 3 h 10 m late, so its
    # 16:00 flight could not leave before 19:05 (a 25-minute turn): at least 3 h late, and it was cancelled. Plane N2
    # lands on time at 16:30 and its 16:40 flight leaves 15 minutes late. Plane N3's 18:30 flight is its first of the
    # day and is cancelled: it tells nothing about lateness and is not counted.
    import zipfile
    cols = ["FlightDate", "Reporting_Airline", "Flight_Number_Reporting_Airline", "Origin", "Dest", "Cancelled", "Diverted",
            "ArrDelay", "DepDelay", "CRSDepTime", "CRSArrTime", "Tail_Number"]
    rows = [["2025-08-01", "AA", "10", "BOS", "JFK", "0.00", "0.00", "190", "185", "1400", "1530", "N1"],
            ["2025-08-01", "AA", "11", "JFK", "MIA", "1.00", "0.00", "", "", "1600", "1900", "N1"],
            ["2025-08-01", "AA", "20", "BOS", "JFK", "0.00", "0.00", "0", "0", "1500", "1630", "N2"],
            ["2025-08-01", "AA", "21", "JFK", "MIA", "0.00", "0.00", "20", "15", "1640", "1940", "N2"],
            ["2025-08-01", "AA", "31", "JFK", "ORD", "1.00", "0.00", "", "", "1830", "2030", "N3"]]
    tmp = tempfile.mkdtemp()
    try:
        zp = os.path.join(tmp, "m.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("m.csv", ",".join(cols) + "\n" + "\n".join(",".join(r) for r in rows) + "\n")
        saved_path = ontime.PATH
        ontime.build([zp], out=os.path.join(tmp, "t.json.gz"))
        import gzip
        doc = json.load(gzip.open(os.path.join(tmp, "t.json.gz"), "rt"))
        cell, night = doc["fixer"]["JFK"]["5"], doc["fixer"]["JFK"]["6"]
        ti = doc["thresholds"].index(180)
        check("the inbound step: a plane landing 3 h 10 m late makes its next flight known 3 h late (cancelled here); an "
              "on-time plane's flight counts only at 0; a first flight of the day tells nothing",
              cell["kn"][ti] == 1 and cell["kc"][ti] == 1 and cell["kn"][0] == 2 and cell["kc"][0] == 1
              and night["cancelled"] == 1 and not any(night["kn"]),
              str({k: cell[k] for k in ("kn", "kc", "cancelled")}) + str(night.get("kn")))
        lo_q, q, hi_q = ontime.cancel_rate(400, 40)
        check("the cancellation rate carries its 95% margin of error (40 of 400: 10%, about 7.4% to 13.3%)",
              abs(q - 0.10) < 1e-9 and 0.07 < lo_q < 0.08 and 0.13 < hi_q < 0.14, str((lo_q, q, hi_q)))
        check("memory readout: numbers where the box reports them, never a guess",
              set(server.memory_status()) >= {"total_mb", "available_mb", "rss_mb", "peak_mb", "warn_below_mb"}
              and all(v is None or isinstance(v, int) for k, v in server.memory_status().items()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- a flight's own extras through Duffel (2026-09-25): only what the airline priced counts
    samples = os.path.join(HERE, "..", "adapter_samples")
    syn = json.load(open(os.path.join(samples, "duffel-extras-synthesized.json")))
    x = adapter.duffel_extras_summary(syn)
    check("a flight's own quote: two checked bags at $45 and $65 (the insurance service is not a bag), cheapest first",
          [(b["kind"], b["amount"]) for b in x["bags"]] == [("checked", 45.0), ("checked", 65.0)], str(x["bags"]))
    check("a seat on every flight is the price of choosing one ($32 + $0), and extra legroom is the long flight's ($89 on "
          "the 7h10m flight; the 1h20m connection sells none)", x["seat"] and x["seat"]["any"] == 32.0 and x["seat"]["legroom"] == 89.0
          and not x["seat"]["free"], str(x["seat"]))
    real = json.load(open(os.path.join(samples, "duffel-extras-aa-unpriced.json")))
    y = adapter.duffel_extras_summary(real)
    check("a REAL seat map with no seat on sale (American, 2026-09-25) and no bag services prices nothing: unknown, never free",
          y["seat"] is None and y["bags"] == [], str(y))
    check("an unsupported airline is refused before any call", server.extras_request({"offer": ["off_0000BAmB8MyZMTvVJXPEVg"],
          "owner": ["IB"]}).get("unsupported") is True and "error" in server.extras_request({"offer": ["x"], "owner": ["BA"]}))

    # ---- meals (2026-09-25): the operating airline's published catering, by cabin, haul and flight length
    T = {"airlines": {
        "AA": [{"cabin": "economy", "haul": "short", "served": "snack", "rule": "snacks"},
               {"cabin": "economy", "haul": "short", "min_minutes": 300, "served": "meal", "rule": "meal on long domestic"},
               {"cabin": "economy", "haul": "long", "served": "meal", "rule": "meals abroad"}],
        "FI": [{"cabin": "economy", "haul": "long", "served": "buy", "rule": "food for sale"},
               {"cabin": "premium_economy", "haul": "all", "served": "meal", "rule": "Saga meal"}],
        "LL": [{"cabin": "economy", "haul": "long", "served": "meal", "rule": "meal"},
               {"cabin": "economy", "haul": "long", "brands": ["basic"], "served": "buy", "rule": "Basic: food for sale"}]}}
    mf = lambda *a, **k: adapter.meal_for(*a, table=T, **k)[0]
    check("meals: a short US domestic flight is a snack, a long domestic one a meal, one abroad a meal",
          mf("AA", "economy", 150, True) == "snack" and mf("AA", "economy", 330, True) == "meal" and mf("AA", "economy", 420, False) == "meal")
    check("meals: food for sale is not a meal (Icelandair economy across the Atlantic), and premium has its own rule",
          mf("FI", "economy", 400, False) == "buy" and mf("FI", "premium_economy", 400, False) == "meal")
    check("meals: a Basic fare with no meal wins over the cabin's rule; an airline with no published policy is unknown",
          mf("LL", "economy", 480, False, "LEVEL Basic") == "buy" and mf("LL", "economy", 480, False, "Optima") == "meal"
          and mf("ZZ", "economy", 480, False) is None)
    T["airlines"]["XB"] = [{"cabin": "economy", "haul": "long", "served": "buy", "rule": "food for sale"}]
    check("meals: business or first with no rule of its own takes a meal from a cabin below it (American first "
          "abroad, Icelandair business over its Saga premium), never a snack or food for sale (unknown then)",
          mf("AA", "first", 480, False) == "meal" and mf("AA", "business", 420, False) == "meal"
          and mf("AA", "business", 150, True) is None and mf("FI", "business", 400, False) == "meal"
          and mf("XB", "business", 480, False) is None and mf("XB", "first", 480, False) is None
          and mf("ZZ", "first", 480, False) is None)

    # meals' long haul is an intercontinental flight, or one over 3,500 km of 8 hours or more
    enr = adapter.load_enrichment()
    geo = {"SEA": {"country": "US", "lat": 47.45, "lon": -122.31}, "LIR": {"country": "CR", "lat": 10.59, "lon": -85.54},
           "GRU": {"country": "BR", "lat": -23.43, "lon": -46.47}, "YYC": {"country": "CA", "lat": 51.13, "lon": -114.01},
           "HNL": {"country": "US", "lat": 21.32, "lon": -157.92}, "SID": {"country": "CV", "lat": 16.74, "lon": -22.95}}
    lh = lambda o, d, m: adapter.meal_long_haul(o, d, m, enr, geo)
    check("meals' long haul: New York to Dublin crosses an IATA area (long at 6h25); New York to Sao Paulo is long at "
          "9h40; Seattle to Costa Rica, Calgary to Honolulu and Frankfurt to Cape Verde get the short-haul product",
          lh("JFK", "DUB", 385) and lh("JFK", "GRU", 580) and not lh("SEA", "LIR", 360) and not lh("YYC", "HNL", 390)
          and not lh("FRA", "SID", 330))
    meals = adapter.load_meals().get("airlines") or {}
    badm = [c for c, rs in meals.items() for r in rs
            if r.get("served") not in ("meal", "snack", "buy", "none") or not str(r.get("url", "")).startswith("http")
            or not r.get("date") or (r.get("haul") == "long" and r.get("min_minutes") is not None)]
    check("the meals table: every rule served meal, snack, buy or none, with its source and date, and no long-haul floor",
          len(meals) > 50 and not badm, str(badm[:5]))

    # ---- Google's bag sentences with a seller's fare (2026-09-25): the seller check already fetches them
    gb = adapter.google_bag_prices
    check("Google's bag sentences: pieces in order, a range at its top and flagged, free pieces 0, carry-on left out, "
          "nothing parsed is nothing (never a free bag)",
          gb(["1 free carry-on", "1st checked bag: $35", "2nd checked bag: $45"]) == [{"piece": 1, "cents": 3500, "range": False}, {"piece": 2, "cents": 4500, "range": False}]
          and gb(["1st checked bag: 99-187"]) == [{"piece": 1, "cents": 18700, "range": True}]
          and gb(["1 free checked bag", "2nd checked bag: 100"]) == [{"piece": 1, "cents": 0, "range": False}, {"piece": 2, "cents": 10000, "range": False}]
          and gb(["Carry-on bag: 25"]) == [] and gb(None) == []
          # Google's own wording, from real replies (2026-09-25): American and BA Basic across the Atlantic, BA Standard
          and gb(["1 free carry-on", "1st checked bag: 85"]) == [{"piece": 1, "cents": 8500, "range": False}]
          and gb(["1 free carry-on", "1st checked bag: 70-85"]) == [{"piece": 1, "cents": 8500, "range": True}]
          and gb(["1 free carry-on", "1st checked bag free"]) == [{"piece": 1, "cents": 0, "range": False}])

    # ---- published add-on prices (enrichment/addons.json, 2026-09-25): every price carries a source; the page gets
    # the prices only; a "from" price and a price in doubt keep their asterisk; the fallback is always marked
    doc = addons.load()
    bad = []
    for kind, rows in (doc.get("prices") or {}).items():
        for code, row in rows.items():
            if not (row.get("sources") and all(x.get("url", "").startswith("https://") and x.get("date") for x in row["sources"])):
                bad.append("%s %s: no dated source" % (kind, code))
            hauls = [row.get(h) for h in ("long", "short") if row.get(h)]
            if not hauls or not all(isinstance(q.get("cents"), int) and q["cents"] >= 0 or q.get("free") or q.get("not_sold")
                                    for q in hauls):
                bad.append("%s %s: no price" % (kind, code))
    ins = doc.get("insurance") or {}
    check("every published add-on price has a dated source and a price", not bad, "; ".join(bad[:4]))
    check("insurance is a share of the ticket inside its published range (4-10%)",
          0 < ins.get("low", 0) <= ins.get("typical", 0) <= ins.get("high", 0) <= 0.15 and ins.get("sources"))
    check("the page's table carries prices only, never a source", "http" not in json.dumps(addons.page_table()))
    node = shutil.which("node")
    if not node:
        print("  skip the add-on pricing checks: no node on this machine")
    else:
        src = open(os.path.join(HERE, "..", "ui", "mock", "mock-10.src.html"), encoding="utf-8").read()
        a, b = src.index("// Published add-on prices"), src.index("const ADDONS = {")
        js = ("const D = {airports:{JFK:{border:'us'}, LAX:{border:'us'}, LHR:{border:'uk'}}}; "
              "const airName = c => ({DL:'Delta', TP:'TAP', F9:'Frontier', AC:'Air Canada', UA:'United', LH:'Lufthansa'})[c] || c; "
              "const money = c => '$' + Math.round(c / 100);\n"
              + src[a:b].replace("__ADDONS__", json.dumps(addons.page_table())) + """
const e = (c, route, mins) => ({carrier:c, operator:c, route, ticket_cents:100000, by:{cheapest:{legs:[{kind:'flight', minutes:mins}]}}});
const lhr = c => e(c, ['JFK', 'LHR'], 420);
console.log(JSON.stringify({dl:addonPrice('lounge', lhr('DL'), 6500), tp:addonPrice('priority', lhr('TP'), 2500),
  f9:addonPrice('priority', e('F9', ['JFK', 'LAX'], 360), 2500), zz:addonPrice('priority', lhr('ZZ'), 2500),
  ac:addonPrice('lounge', lhr('AC'), 6500), ins:insurancePrice(lhr('BA')), short:shortHaul(e('F9', ['JFK', 'LAX'], 360)),
  acwifi:addonPrice('wifi', lhr('AC'), 1900), acwifi_short:addonPrice('wifi', e('AC', ['JFK', 'LAX'], 360), 1900),
  dlwifi:addonPrice('wifi', lhr('DL'), 1900), b6wifi:addonPrice('wifi', lhr('B6'), 1900), bags:bagsSeq(),
  ualounge:addonPrice('lounge', lhr('UA'), 6500), ualeg:addonPrice('legroom', lhr('UA'), 7900), lhseat:addonPrice('seat', lhr('LH'), 3000),
  dlprio:notSold('priority', lhr('DL')), uaprio:notSold('priority', lhr('UA'))}));
function bagsSeq(){
  const eb = Object.assign(lhr('AA'), {id:'x1', bag_tiers:[{piece:1, amount_cents:7500}, {piece:2, amount_cents:10000}]}), o = {};
  o.table = bagLadder(eb);
  SELLERS.x1 = {airline:{bags:[{piece:1, cents:0, range:false}, {piece:2, cents:9000, range:false}]}}; o.google = bagLadder(eb);
  SELLERS.x1 = {airline:{bags:[{piece:1, cents:18700, range:true}]}}; o.range = bagLadder(eb);
  SELLERS.x1 = {airline:{bags:[]}}; o.empty = bagLadder(eb);
  eb.offer = 'off1'; EXTRAS.off1 = {bags:[{kind:'checked', cents:7000, max:1}]}; SELLERS.x1 = {airline:{bags:[{piece:1, cents:9900, range:false}]}}; o.duffel = bagLadder(eb);
  return o;
}""")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, fh.name], capture_output=True, text=True)
        os.unlink(fh.name)
        if r.returncode:
            check("the page's add-on pricing runs", False, r.stderr[-600:])
        else:
            o = json.loads(r.stdout)
            check("an add-on the airline does not sell (Delta's day pass) says so and prices the walk-in lounge, marked",
                  "no longer sells" in o["dl"]["basis"] and o["dl"]["est"] and o["dl"]["cents"] == doc["typical"]["lounge"]["cents"], str(o["dl"]))
            check("a 'from' price is the airline's own and keeps its asterisk (TAP boarding from $10)",
                  o["tp"]["cents"] == 1000 and o["tp"]["est"] and o["tp"]["basis"].startswith("from TAP"), str(o["tp"]))
            check("a published top price needs no asterisk (Frontier up to $9.99), and a trip inside one border is short-haul",
                  o["f9"]["cents"] == 999 and not o["f9"]["est"] and o["short"], str(o["f9"]))
            check("Air Canada's lounge is the top of its published range in Canadian dollars (CAD 79, $56), no asterisk",
                  o["ac"]["cents"] == 5586 and not o["ac"]["est"] and o["ac"]["basis"].startswith("up to Air Canada"), str(o["ac"]))
            check("a price only a secondary source states keeps its asterisk and says so (United Club pass $59, as reported)",
                  o["ualounge"]["cents"] == 5900 and o["ualounge"]["est"] and "as reported" in o["ualounge"]["basis"], str(o["ualounge"]))
            check("a range a source states shows both ends beside its middle figure, marked (United Economy Plus across "
                  "the Atlantic, $120-200)", o["ualeg"]["cents"] == 16000 and o["ualeg"]["est"] and "$120–$200" in o["ualeg"]["basis"], str(o["ualeg"]))
            check("the airline's own price read off its seat map on sample flights says so and keeps the asterisk "
                  "(Lufthansa $43.90)", o["lhseat"]["cents"] == 4390 and o["lhseat"]["est"] and "sample flights" in o["lhseat"]["basis"], str(o["lhseat"]))
            check("an airline that does not sell priority boarding on its own reads as not sold (Delta), one that does "
                  "does not (United)", o["dlprio"] is True and o["uaprio"] is False, str((o["dlprio"], o["uaprio"])))
            check("an airline with no published price takes the typical one, marked", o["zz"]["est"] and o["zz"]["cents"] == 2500, str(o["zz"]))
            check("insurance is the typical share of the ticket (6.5% of $1,000), marked", o["ins"]["cents"] == 6500 and o["ins"]["est"], str(o["ins"]))
            check("a price published for one haul never stands in for the other (Air Canada's free wifi is within North "
                  "America, so across the Atlantic the typical pass stands, marked)",
                  o["acwifi"]["cents"] == 1900 and o["acwifi"]["est"] and o["acwifi_short"]["cents"] == 0, str(o["acwifi"]))
            bg = o["bags"]
            check("a flight's bag fees: the table until checked; then Google's listed fees for the airline's own fare (a "
                  "free first bag left out, a range priced at its top and marked); nothing listed keeps the table; this "
                  "flight's Duffel quote beats Google's",
                  bg["table"]["tiers"] == [7500, 10000] and not bg["table"]["own"]
                  and bg["google"]["tiers"] == [9000] and bg["google"]["google"] and not bg["google"].get("est")
                  and bg["range"]["tiers"] == [18700] and bg["range"]["est"]
                  and bg["empty"]["tiers"] == [7500, 10000] and not bg["empty"]["own"]
                  and bg["duffel"]["tiers"] == [7000] and not bg["duffel"].get("google"), str(bg))
            check("free wifi for members is $0 and says so; free on only part of the fleet keeps its asterisk (Delta), free "
                  "everywhere does not (JetBlue)", o["dlwifi"]["cents"] == 0 and o["dlwifi"]["est"] and "SkyMiles" in o["dlwifi"]["basis"]
                  and o["b6wifi"]["cents"] == 0 and not o["b6wifi"]["est"], str(o["dlwifi"]) + str(o["b6wifi"]))

    # ---- lounge access (2026-09-25): the published rules, run from the page itself
    if node:
        src = open(os.path.join(HERE, "..", "ui", "mock", "mock-10.src.html"), encoding="utf-8").read()
        al = src[src.index("const ALLIANCE = {"):]
        al = al[:al.index("\n")]
        blk = src[src.index("// Lounge access (2026-09-25"):src.index("const WANTS = [")]
        js = (al + "\nconst S = {status:{}}; const STATUS = {};\n"
              "const D = {airports:{JFK:{border:'us'}, LAX:{border:'us'}, LHR:{border:'uk'}, CDG:{border:'schengen'}}};\n" + blk + """
const mk = (c, route, cabin, brand) => ({carrier:c, operator:c, route, brand:brand || 'Main', lie_flat:false,
  segments:[{cabin}], by:{cheapest:{legs:[{kind:'flight', minutes:400}]}}});
const run = (st, e) => { S.status = st; return loungeAccess(e); };
console.log(JSON.stringify({
  sapphire:run({oneworld:'Sapphire'}, mk('AA', ['JFK','LHR'], 'economy')), aaPlatDom:run({AA:'Platinum'}, mk('AA', ['JFK','LAX'], 'economy')),
  aaPlatIntl:run({AA:'Platinum'}, mk('AA', ['JFK','LHR'], 'economy')), dlGoldDL:run({DL:'Gold'}, mk('DL', ['JFK','CDG'], 'economy')),
  dlGoldAF:run({DL:'Gold'}, mk('AF', ['JFK','CDG'], 'economy')), dlGoldLight:run({DL:'Gold'}, mk('AF', ['JFK','CDG'], 'economy', 'Economy Light')),
  uaGoldDom:run({UA:'Gold'}, mk('UA', ['JFK','LAX'], 'economy')), starGoldDom:run({star:'Gold'}, mk('UA', ['JFK','LAX'], 'economy')),
  mintLAX:run({}, mk('B6', ['LAX','JFK'], 'business')), bizIntl:run({}, mk('BA', ['JFK','LHR'], 'business')), none:run({}, mk('BA', ['JFK','LHR'], 'economy'))}));""")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([node, fh.name], capture_output=True, text=True)
        os.unlink(fh.name)
        if r.returncode:
            check("the page's lounge rules run", False, r.stderr[-600:])
        else:
            o = json.loads(r.stdout)
            check("lounge: oneworld Sapphire gets in on American across the Atlantic; AAdvantage Platinum only on a trip "
                  "that leaves North America", bool(o["sapphire"]) and not o["aaPlatDom"] and bool(o["aaPlatIntl"]), str(o))
            check("lounge: a Delta Medallion gets no Sky Club in Delta economy, gets a SkyTeam lounge flying Air France "
                  "abroad, and nothing on a Light fare", not o["dlGoldDL"] and bool(o["dlGoldAF"]) and not o["dlGoldLight"], str(o))
            check("lounge: United Premier Gold only abroad, Star Gold from another programme at home too; Mint from Los "
                  "Angeles has no BlueHouse; business abroad includes it; plain economy does not",
                  not o["uaGoldDom"] and bool(o["starGoldDom"]) and not o["mintLAX"] and bool(o["bizIntl"]) and not o["none"], str(o))

    # ---- ride estimates lean high: at or above the official taxi fares the fare bands were checked against (2026-09-25)
    enr = adapter.load_enrichment()
    official = [("Puerta del Sol, Madrid", "ES", 40.4169, -3.7035, "MAD", 40.4936, -3.5668, 3750),    # flat EUR 33
                ("Placa Catalunya, Barcelona", "ES", 41.3870, 2.1700, "BCN", 41.2974, 2.0833, 3300),  # T-1 meter, EUR 29
                ("Taksim, Istanbul", "TR", 41.0370, 28.9850, "IST", 41.2753, 28.7519, 4070)]      # meter, ~1,990 TL
    low = []
    for label, cc, la, lo, iata, ala, alo, cents in official:
        ap = {"iata": iata, "lat": ala, "lon": alo, "country": cc, "city": label.split(", ")[1]}
        o, _how = ground.resolve_origin(label, enr, ap)
        modes, _src = ground.modes_for(dict(o, lat=la, lon=lo, country=cc), ap, 14 * 60, enr)
        ride = next((m for m in modes if m.get("mode_kind") == "rideshare" or "ide" in m["mode"]), None)
        if not ride or ride["fare_cents"] < cents:
            low.append("%s %s under %d" % (iata, ride and ride["fare_cents"], cents))
    check("a ride estimate is never under the official taxi fare it was checked against (Madrid, Barcelona, Istanbul)",
          not low, "; ".join(low))

    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    for f in FAILS:
        print("  FAIL " + f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
