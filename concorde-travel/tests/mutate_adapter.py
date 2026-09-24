#!/usr/bin/env python3
"""Mutation test for the adapters: break the code on purpose, one fault at a
time, and fail if `test_adapter.py` does not notice.

    python3 concorde-travel/tests/mutate_adapter.py

CLAUDE.md has claimed a mutation-testing standard for these suites since the
scorer was written, but the runs were ad hoc and nothing in the repo let the
next person check it. This makes the claim executable. A guard that stops
catching its fault is a guard that has quietly stopped existing - which is
exactly what happened when a grade-stability test was found comparing grade()
against itself.

Each mutant is applied INSIDE one named function. Three adapters now share
near-identical lines - the same offset-drop, the same bag-load line - so a
whole-file anchor matches several places, and a mutation runner that silently
skips an ambiguous anchor reports a better score than it earned. Scoping to a
function keeps every mutant honest, and a SKIP is printed loudly rather than
being folded into the total.

Every mutant here is a REAL mistake, not a syntactic nudge: reading a bag load
off the fare instead of the traveller, extrapolating a fee ladder from the
cheapest tier, guessing a UTC offset for an uncurated airport, turning a hedged
cabin claim into a certainty. If you add a behaviour worth protecting, add the
fault that breaks it.
"""

import subprocess, shutil, sys, re, os, tempfile

# Every mutant is written, tested and restored in a SCRATCH COPY, never in the checkout (2026-09-24). The repo
# lives in Google Drive, and its sync raced the runner's write-and-restore cycle: after a run that reported every
# fault caught, par.py still held two mutants (a 10:00 reference departure and a disabled nonstop-range rule),
# which the next run then silently skipped. Nothing here may write to the real tree.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = tempfile.mkdtemp(prefix="concordego-mutate-")
for _d in ("concorde-travel", "fixtures"):
    shutil.copytree(os.path.join(ROOT, _d), os.path.join(WORK, _d),
                    ignore=shutil.ignore_patterns("__pycache__", "*.bak", ".DS_Store"))
os.chdir(WORK)

AD = "concorde-travel/adapter.py"
DATA = "concorde-travel/ui/mock/data.py"
SV = "concorde-travel/server.py"
GR = "concorde-travel/ground.py"
PL = "concorde-travel/places.py"
FA = "concorde-travel/enrichment/fares.json"
PA = "concorde-travel/par.py"
SC = "concorde-travel/scorer.py"


def slice_fn(src, name):
    m = re.search(r"^def %s\(" % re.escape(name), src, re.M)
    if not m:
        return None
    start = m.start()
    nxt = re.search(r"^(def |# -{5,})", src[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(src)
    return start, end



def _uncache():
    """Drop compiled bytecode after every write. A mutant the same size as the
    original ("all" -> "any"), restored inside the same second, otherwise leaves
    its .pyc looking fresh, and the NEXT import runs the mutant (2026-09-21)."""
    for d in ("concorde-travel", "concorde-travel/tests", "concorde-travel/ui/mock"):
        shutil.rmtree(os.path.join(d, "__pycache__"), ignore_errors=True)

MUTANTS = [
 (AD, "from_amadeus", 'op = ((s.get("operating") or {}).get("carrierCode") or mkt)',
  'op = mkt', 'amadeus: ignore the operating block'),
 (AD, "from_amadeus", '''            if e1 or e2:
                fail = e1 or e2
                break''',
  '''            if e1 or e2:
                dep = dep or (s.get("departure") or {}).get("at", "") + "+00:00"
                arr = arr or (s.get("arrival") or {}).get("at", "") + "+00:00"''',
  'amadeus: guess an offset instead of dropping an uncurated airport'),
 (AD, "from_amadeus",
  'bags = [{"kind": "checked", "weight_kg": 20} for _ in range(max(0, int(checked_bags)))]',
  'bags = [{"kind": "checked", "weight_kg": 20} for _ in range(first["tickets"][0]["entitlements"]["checked_included"])]',
  'amadeus: read the bag load off the fare, not the caller'),
 (AD, "from_amadeus", '''                "bags_checked_through": True,
                "forced_landside": False,''',
  '''                "bags_checked_through": False,
                "forced_landside": True,''',
  'amadeus: call a single ticket a self-transfer'),
 (AD, "from_duffel", '''            if e1 or e2:
                fail = e1 or e2
                break''',
  '''            if e1 or e2:
                dep = dep or s.get("departing_at", "") + "+00:00"
                arr = arr or s.get("arriving_at", "") + "+00:00"''',
  'duffel: guess an offset instead of dropping an uncurated airport'),
 (AD, "from_duffel",
  'bags = [{"kind": "checked", "weight_kg": 20} for _ in range(max(0, int(checked_bags)))]',
  'bags = [{"kind": "checked", "weight_kg": 20} for _ in range(included)]',
  'duffel: read the bag load off the fare, not the caller'),
 (AD, "_decimal_cents", 'out = int(whole or 0) * 100 + int(frac or 0)',
  'out = int(float(s or 0) * 100)', 'money: parse via float instead of textually'),
 (AD, "_extend_tiers", 'top = max(out, key=lambda t: t["piece"])',
  'top = min(out, key=lambda t: t["piece"])',
  'tier padding: extrapolate from the CHEAPEST tier'),
 (AD, "_brand_key", '''        if c == carrier and token in words:''',
  '''        if c == carrier and token in up:''',
  'brand join: substring instead of whole word'),
 (AD, "_fleet_claims", '    return {"subfleet": dict(row["subfleet"]),',
  '    row = json.loads(json.dumps(row));\n    row["subfleet"]["observed_frequency"] = 1.0\n    return {"subfleet": dict(row["subfleet"]),',
  'hard rule 2: turn a hedged subfleet claim into a certainty'),
 (AD, "_duffel_bag_tiers", '''    if not svcs:
        return fallback, ("curated" if not used_default else "default")''',
  '''    if not svcs:
        return [], "feed"''',
  'duffel: no quoted service means the bag is free'),
 (AD, "coverage", 'if s.get("equipment_code") not in (None, "", "UNKNOWN"):',
  'if True:', 'coverage: count equipment as known when it is not'),
 (AD, "from_duffel", """            if pitch.isdigit():
                claims["seat_pitch_inches"] = int(pitch)""",
  """            if False:
                claims["seat_pitch_inches"] = int(pitch)""",
  'amenities: drop the published seat pitch'),
 (AD, "from_duffel", """            wifi = amen.get("wifi") or {}""",
  """            wifi = amen.get("wifi") or {}
            if wifi.get("available"):
                claims["connectivity_oceanic"] = {
                    "value": "Wifi fitted", "observed_frequency": 1.0,
                    "outcome": True, "sample_size": 1,
                    "observation_window": "the airline says so",
                    "source": "duffel amenity", "as_of": "2026-09-19"}""",
  'hard rule 2: publish a wifi amenity as an observed certainty'),
 (AD, "from_duffel", """            pw = amen.get("power") or {}""",
  """            pw = {}""",
  'amenities: drop the published power attribute'),
 (AD, "offset_for", """    if zone.get("observes_dst") is False:
        return zone["standard"]""",
  """    if False:
        return zone["standard"]""",
  'no-DST zones: treat Iceland/Turkey as an uncurated year'),
 (AD, "offset_for", """    if zone.get("observes_dst") is False:
        return zone["standard"]
    year = date_str[:4]
    window = (zone.get("dst") or {}).get(year)
    if not window:
        return None""",
  """    if zone.get("observes_dst") is False:
        return zone["standard"]
    year = date_str[:4]
    window = (zone.get("dst") or {}).get(year)
    if not window:
        return zone["standard"]""",
  'an uncurated DST year silently scores at standard time'),
 (AD, "crosses_border", """    return ba != bb""", """    return False""",
  'immigration: never charge for a border crossing'),
 (AD, "crosses_border", """    ba, bb = border_of(from_iata, enr, geo), border_of(to_iata, enr, geo)""",
  """    ba, bb = None, None""",
  'global borders: fall back to the Schengen-only test everywhere'),
 (AD, "border_of", """    return UNIONS.get(cc, cc.lower())""",
  """    return UNIONS.get(cc)""",
  'global borders: an uncurated country has no border at all'),
 (AD, "stamp", """    if feed_zone:
        off = _offset_from_tzdb(feed_zone, local_naive)
        if off:
            return local_naive[:19] + off, None""",
  """    if False:
        pass""",
  'global time: refuse to stamp an airport the table never curated'),
 (GR, "resolve_origin", '''                or (hood and re.search(r"\\b%s\\b" % re.escape(hood), q))''',
  '''                or False''',
  'ground: stop finding a curated neighbourhood inside a full address'),
 (GR, "estimate_modes", '''    fare = int(base + per_mile * miles + per_min * drive_min + airport_fee)''',
  '''    fare = int(base + per_mile * miles * 0.4)''',
  'ground: make the car estimate cheap, recreating the surprise it prevents'),
 (GR, "region_support", '''    return "modelled" if (country or "").upper() in _BAND_BY_COUNTRY else "assumed"''',
  '''    return "modelled"''',
  'ground: call an unpriced region modelled'),
 (PL, "resolve", '''    near = suggest(raw)''',
  '''    return "NYC", "city", None
    near = suggest(raw)''',
  'places: guess New York for anything unrecognised'),
 (PL, "covers", '''    return code == iata or iata in METRO.get(code, set())''',
  '''    return True''',
  'places: call every airport a match for every metro'),
 (FA, None, '"piece": 1,\n        "amount_cents": 10000\n      },\n      {\n        "piece": 2,\n        "amount_cents": 12000',
  '"piece": 1,\n        "amount_cents": 1000\n      },\n      {\n        "piece": 2,\n        "amount_cents": 1200',
  'unknown-is-never-zero: make the fallback bag fee cheap'),
 (AD, "route_par", 'arrival_international=_arrival_international(enr, o_iata, d_iata, geo))',
  'arrival_international=_arrival_international(enr, o_iata, d_iata, geo))\n'
  '    import inspect as _i\n'
  '    _opts = _i.currentframe().f_back.f_locals.get("options") or []\n'
  '    if _opts:\n'
  '        _p = sorted(x["booking"][0]["price_cents"] for x in _opts)\n'
  '        par = int(_p[len(_p) // 2] * 1.6)',
  'par: derive it from the median of the results (the grade goes relative)'),
 (AD, "route_par", '    if curated.get("par_cents"):', '    if False and curated.get("par_cents"):',
  'par: ignore the curated override'),
 (AD, "route_par", '        return 105000, {"source": "default", "par_cents": 105000,',
  '        return 105000, {"source": "modelled", "par_cents": 105000,',
  'par: dress the default up as a modelled number'),
 (PA, "season_factor", '    return row[m]', '    return 1.0',
  'par: flatten the season'),
 (PA, "reference_fare_cents", '    b3 = max(miles - 2000.0, 0.0)', '    b3 = 0.0',
  'par: drop the long-haul band from the fare'),
 (PA, "market_class", '    if oc and oc == dc:', '    if False:',
  'par: price a domestic route as an intercontinental one'),
 (PA, "reference_departure_minutes", '    return 19 * 60 + 30 if long_haul_east else 10 * 60',
  '    return 10 * 60',
  'par: a daytime reference on an overnight market'),
 (PA, "par_for", '            "reliability": {"on_time_fraction": 0.78, "delay_minutes_p50": 10,',
  '            "reliability": {"coverage": "none", "policy": "route_median", "reason": "x", "_": {"on_time_fraction": 0.78, "delay_minutes_p50": 10,',
  'par: charge the reference for an unknown on-time record'),
 (PA, "par_for", '    if miles > NONSTOP_RANGE_MILES:', '    if False:',
  'par: measure a route with no nonstop against a nonstop'),
 (AD, "from_serpapi", '''            if e1 or e2:
                fail = e1 or e2
                break''',
  '''            if e1 or e2:
                dep = dep or dep_naive + "+00:00"
                arr = arr or arr_naive + "+00:00"''',
  'serpapi: guess an offset instead of dropping an uncurated airport'),
 (AD, "from_serpapi", 'total_cents = int(round(float(it.get("price")) * 100))',
  'total_cents = int(round(float(it.get("price"))))',
  'serpapi: take the whole-dollar price as cents'),
 (AD, "from_serpapi", 'if want and not all(c in want for c in codes):',
  'if want and not any(c in want for c in codes):',
  'serpapi: keep an itinerary with one Delta leg among other carriers'),
    # 2026-09-24, the Google Flights price check: duplicates, the duration arithmetic, the cheapest tickets
    (AD, "merge_scenarios", 'if have.get(_itin_key(o), 1 << 62) <= _ticket_cents(o):',
     'if have.get(_itin_key(o), 1 << 62) >= _ticket_cents(o):',
     'merge: keep the dearer duplicate and drop the cheaper one'),
    (AD, "_offset_by_duration", 'other_utc = (known - step if not forward else known + step)',
     'other_utc = (known + step if not forward else known - step)',
     'serpapi: run the duration the wrong way when working out an offset'),
    (DATA, "build_slim", '[:CHEAPEST_TICKETS]', '[:0]',
     'pool: leave the cheapest tickets out when the ranking demotes them'),
    (AD, "_serp_number", 'tail = " ".join(parts[1:]) if len(parts) > 1 else str(flight_number or "").strip()[2:]',
     'tail = str(flight_number or "")',
     'serpapi: take every digit of the flight number, the carrier code\'s included'),
    (AD, "_short_haul", 'return _ground.haversine_km(float(la1), float(lo1), float(la2), float(lo2)) <= SHORT_HAUL_KM',
     'return _ground.haversine_km(float(la1), float(lo1), float(la2), float(lo2)) >= SHORT_HAUL_KM',
     'bags: call a long trip short-haul and a short one long-haul'),
    (AD, "_fare_row", 'elif short and row.get("short_haul"):', 'elif not short and row.get("short_haul"):',
     'bags: charge the short-haul fees on the long-haul trip'),
    (DATA, "build_slim", 'if extra + 100 > ow:', 'if extra + 100 < ow:', 'round trip: apply the round-trip fare when it is dearer'),
    (AD, "serp_itin_key", 'str((f.get("departure_airport") or {}).get("time") or "").replace(" ", "T")[:16])',
     'str((f.get("departure_airport") or {}).get("time") or "")[:16])',
     'round trip: key Google\'s returns by a clock the adapter never writes, so no return ever matches'),
    (SV, "sellers_request", '"same_flights": not sold or sold == fns,', '"same_flights": True,',
     'sellers: price a codeshare under other flight numbers as if it were these flights'),
    (AD, "_airport_block", 'intl = any(crosses_border(enr, first, s["destination"]["iata"], geo) for s in segments)',
     'intl = crosses_border(enr, first, segments[0]["destination"]["iata"], geo)',
     'airport time: judge a trip by its first flight only, so New York to Chicago to London is timed as domestic'),
    (PA, "airport_minutes", 'p50 = max(floor, int(cur.get("p50") or 0))', 'p50 = int(cur.get("p50") or floor)',
     'airport time: let a curated median shorter than the floor win, so JFK to London gets 55 minutes'),
    (AD, "cabin_norm", 'if w.startswith("business") or w.startswith("upper"):', 'if w == "business":',
     'cabins: read only the bare word, so Google\'s "Business Class" rows grade as economy'),
    (SC, "_cabin_short", 'gap = worth[want] - worth.get(have, 0)', 'gap = abs(worth[want] - worth.get(have, 0))',
     'cabins: charge a first-class leg on a business search as if it were a downgrade'),
    (SV, "_merge_serp", 'if not (k == "price_insights" and not _serp_its(a))', 'if True',
     'cabins: keep a business-class price history under a first-class search that found none'),
    (SV, "_merge_duffel", 'extra = [o for o in ob if _duffel_key(o) not in seen]', 'extra = list(ob)',
     'cabins: list the same flights twice when both cabin searches return them'),
    (DATA, "build_slim", 'if need:\n            geo.update(server.airport_geo(need))', 'if False:\n            geo.update(server.airport_geo(need))',
     'zones: never ask Duffel where an unknown airport is, so a Google trip through Houston and Quito is dropped'),
    (AD, "_arrival_international", 'if o_iata in US_PRECLEARANCE and border_of(d_iata, enr, geo) == "us":',
     'if False:', 'arrival: ignore US preclearance, so Dublin to New York queues for passport control twice'),
    (AD, "_arrival_block", 'last = segments[-1]',
     'last = {"origin": segments[0]["origin"], "destination": segments[-1]["destination"]}',
     'arrival: judge the border on the whole trip, so Paris to Frankfurt after New York queues at Frankfurt again'),
    (AD, "_carrier_rating", 'use = dict(row, **row["short_haul"])', 'use = row',
     'ratings: ignore the short-haul rating, so a domestic flight is judged on the airline\'s long-haul product'),
]

caught = skipped = 0
for path, fn, old, new, why in MUTANTS:
    shutil.copy(path, path + ".bak")
    src = open(path, encoding="utf-8").read()
    if fn:
        span = slice_fn(src, fn)
        if not span:
            print("  SKIP  (no function %s) %s" % (fn, why)); shutil.move(path+".bak", path); skipped += 1; continue
        a, b = span
        body = src[a:b]
        if body.count(old) != 1:
            print("  SKIP  (%d in %s) %s" % (body.count(old), fn, why)); shutil.move(path+".bak", path); skipped += 1; continue
        src = src[:a] + body.replace(old, new) + src[b:]
    else:
        if src.count(old) != 1:
            print("  SKIP  (%d matches) %s" % (src.count(old), why)); shutil.move(path+".bak", path); skipped += 1; continue
        src = src.replace(old, new)
    open(path, "w", encoding="utf-8").write(src)
    _uncache()      # a same-size mutant restored within the second would otherwise leave its .pyc looking fresh
    # -B and PYTHONDONTWRITEBYTECODE: a mutant must never leave compiled bytecode behind; a same-size mutant
    # restored within the second looks fresh to the cache and the NEXT import runs the mutant (2026-09-21)
    r = subprocess.run([sys.executable, "-B", "concorde-travel/tests/test_adapter.py"],
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                       capture_output=True, text=True)
    shutil.move(path + ".bak", path)
    ok = r.returncode != 0
    caught += ok
    print("  %s  %s" % ("caught " if ok else "MISSED!", why))
ran = len(MUTANTS) - skipped
os.chdir(ROOT)
shutil.rmtree(WORK, ignore_errors=True)
print("\n%d/%d seeded faults caught (%d skipped)" % (caught, ran, skipped))
# A skip is a mutant that never ran. It is not a pass.
sys.exit(0 if caught == ran and not skipped else 1)
