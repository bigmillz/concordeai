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

import subprocess, shutil, sys, re

AD = "concorde-travel/adapter.py"
GR = "concorde-travel/ground.py"
PL = "concorde-travel/places.py"
FA = "concorde-travel/enrichment/fares.json"


def slice_fn(src, name):
    m = re.search(r"^def %s\(" % re.escape(name), src, re.M)
    if not m:
        return None
    start = m.start()
    nxt = re.search(r"^(def |# -{5,})", src[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(src)
    return start, end


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
 (FA, None, '{"piece": 1, "amount_cents": 10000},\n      {"piece": 2, "amount_cents": 12000},',
  '{"piece": 1, "amount_cents": 1000},\n      {"piece": 2, "amount_cents": 1200},',
  'unknown-is-never-zero: make the fallback bag fee cheap'),
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
    r = subprocess.run([sys.executable, "concorde-travel/tests/test_adapter.py"],
                       capture_output=True, text=True)
    shutil.move(path + ".bak", path)
    ok = r.returncode != 0
    caught += ok
    print("  %s  %s" % ("caught " if ok else "MISSED!", why))
ran = len(MUTANTS) - skipped
print("\n%d/%d seeded faults caught (%d skipped)" % (caught, ran, skipped))
# A skip is a mutant that never ran. It is not a pass.
sys.exit(0 if caught == ran and not skipped else 1)
