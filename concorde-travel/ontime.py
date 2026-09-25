"""US DOT on-time records: every flight a US airline flew within the US for a year, boiled down (2026-09-24, per
Patrick: the Flight Fixer's odds and the grade's reliability part should rest on real records, not constants).

Source: the Bureau of Transportation Statistics' "Reporting Carrier On-Time Performance" files (transtats.bts.gov,
one zip a month). Only US airlines' domestic flights are in them: a flight abroad, or on a foreign airline, has no
record here, and says so rather than scoring as punctual.

Two tables, built once by `python3 concorde-travel/ontime.py build ZIP...` into enrichment/ontime.json.gz:

  flights  the on-time record of each flight number on each route (operating airline, number, origin, destination),
           kept when it flew at least MIN_FLIGHTS times in the year, and of each airline's route across all its numbers:
           how many were scheduled, how many arrived within 15 minutes (a cancelled or diverted flight is NOT on time,
           as DOT counts it), how many were cancelled, and the median and 90th-percentile arrival delay of those flown.
  fixer    for each origin airport and three-hour block of scheduled departure, and each delay already reached D:
           how many flights were at least D late leaving, how much longer they took to leave (counts at each extra
           slip), and how many flights in that block were cancelled. The Flight Fixer reads "of the flights here that
           were already D late, this many left within the next hour" off it. The files cannot date a cancellation, so
           the share of already-late flights that ended cancelled is MEASURED another way (2026-09-25, per Patrick:
           "22 to 52% is pretty wide"): every row carries the plane's tail number, so a flight whose plane landed from
           its previous leg too late for it to leave within D minutes was at least D late, whatever happened next.
           Among those flights (`kn`, by threshold) the ones cancelled (`kc`) give the cancellation rate of flights
           already D late, with its margin of error, instead of the old 0-to-every-cancellation range.

No network at run time; the table is read once, lazily.
"""
import csv
import gzip
import io
import json
import os
import sys
import zipfile
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "enrichment", "ontime.json.gz")
MIN_FLIGHTS = 20
THRESHOLDS = (0, 30, 60, 90, 120, 180, 240, 300, 360)          # minutes already late
TURN = 25            # the quickest a plane is turned round: a lateness read off its inbound is a floor, never a guess high
SLIPS = (0, 15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480, 720)   # further minutes before leaving
BLOCKS = 8                                                       # three-hour blocks of scheduled departure
MIN_KNOWN = 40       # late-arriving planes a block needs before its own cancellation rate is read, else the country's


def _pct(xs: List[int], p: float) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    return int(xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))])


def _hhmm(v: str) -> Optional[int]:
    v = (v or "").strip()
    if not v.isdigit():
        return None
    v = v.zfill(4)
    h, m = int(v[:2]), int(v[2:])
    return (h % 24) * 60 + m


def build(zips: List[str], out: str = PATH) -> Dict[str, Any]:
    flights: Dict[str, List[Any]] = {}          # key -> [n, on_time, cancelled, [arrival delays of those flown]]
    routes: Dict[str, List[Any]] = {}
    fixer: Dict[str, Dict[int, Dict[str, Any]]] = {}
    months = []
    new_cell = lambda: {"n": 0, "cancelled": 0, "ge": [0] * len(THRESHOLDS), "slip": [[0] * len(SLIPS) for _ in THRESHOLDS],
                        "kn": [0] * len(THRESHOLDS), "kc": [0] * len(THRESHOLDS)}
    for path in sorted(zips):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(name) as fh:
            r = csv.reader(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
            head = next(r)
            ix = {h: i for i, h in enumerate(head)}
            got = None
            legs: Dict[Any, List[Any]] = {}          # (date, tail) -> the plane's flights that day, for the inbound step
            for row in r:
                if len(row) < len(head) - 1:
                    continue
                got = got or row[ix["FlightDate"]][:7]
                car, num = row[ix["Reporting_Airline"]], row[ix["Flight_Number_Reporting_Airline"]].lstrip("0")
                o, d = row[ix["Origin"]], row[ix["Dest"]]
                canc = row[ix["Cancelled"]].startswith("1")
                div = row[ix["Diverted"]].startswith("1")
                arr = row[ix["ArrDelay"]].strip()
                dep = row[ix["DepDelay"]].strip()
                on_time = (not canc and not div and arr != "" and float(arr) <= 15.0)
                for table, key in ((flights, "%s|%s|%s|%s" % (car, num, o, d)), (routes, "%s|*|%s|%s" % (car, o, d))):
                    rec = table.get(key)
                    if rec is None:
                        rec = table[key] = [0, 0, 0, []]
                    rec[0] += 1
                    rec[1] += on_time
                    rec[2] += canc
                    if not canc and not div and arr != "":
                        rec[3].append(max(0, int(float(arr))))
                sched = _hhmm(row[ix["CRSDepTime"]])
                if sched is None:
                    continue
                block = min(BLOCKS - 1, sched // 180)
                cell = fixer.setdefault(o, {}).setdefault(block, new_cell())
                tail = row[ix["Tail_Number"]].strip()
                if tail:
                    legs.setdefault((row[ix["FlightDate"]], tail), []).append(
                        (sched, o, d, canc, div, _hhmm(row[ix["CRSArrTime"]]), float(arr) if arr != "" else None, block))
                cell["n"] += 1
                if canc:
                    cell["cancelled"] += 1
                    continue
                if dep == "":
                    continue
                late = float(dep)
                for ti, t in enumerate(THRESHOLDS):
                    if late < t:
                        break
                    cell["ge"][ti] += 1
                    extra = late - t
                    for si, s in enumerate(SLIPS):
                        if extra <= s:
                            cell["slip"][ti][si] += 1
        # the inbound step: a flight whose plane landed from its previous leg (same tail, same day, into this airport)
        # too late for it to leave within D minutes was at least D late before anything else happened to it
        for day_legs in legs.values():
            day_legs.sort(key=lambda x: x[0])
            for i, (sched, o, _d, canc, _div, _sa, _ad, block) in enumerate(day_legs):
                prev = next((p for p in reversed(day_legs[:i]) if p[2] == o), None)
                if prev is None or prev[3] or prev[4] or prev[6] is None or prev[5] is None:
                    continue                     # first flight of the day, or the plane never came: nothing to read
                landed = prev[5] + prev[6]
                if landed < prev[0] - 120:       # scheduled to land after midnight
                    landed += 1440
                late = landed + TURN - sched
                if late < 0:
                    continue
                cell = fixer[o][block]
                for ti, t in enumerate(THRESHOLDS):
                    if late < t:
                        break
                    cell["kn"][ti] += 1
                    cell["kc"][ti] += canc
        months.append(got)
        print("read", os.path.basename(path), got, file=sys.stderr)
    pack = lambda rec: [rec[0], rec[1], rec[2], _pct(rec[3], .5), _pct(rec[3], .9)]
    out_flights = {k: pack(v) for k, v in flights.items() if v[0] >= MIN_FLIGHTS}
    out_flights.update({k: pack(v) for k, v in routes.items() if v[0] >= MIN_FLIGHTS})
    # the national cell for each block, for an airport too small to stand on its own
    nat: Dict[int, Dict[str, Any]] = {}
    for cells in fixer.values():
        for b, c in cells.items():
            t = nat.setdefault(b, new_cell())
            t["n"] += c["n"]
            t["cancelled"] += c["cancelled"]
            t["ge"] = [a + b2 for a, b2 in zip(t["ge"], c["ge"])]
            t["kn"] = [a + b2 for a, b2 in zip(t["kn"], c["kn"])]
            t["kc"] = [a + b2 for a, b2 in zip(t["kc"], c["kc"])]
            t["slip"] = [[a + b2 for a, b2 in zip(x, y)] for x, y in zip(t["slip"], c["slip"])]
    fixer["*"] = nat
    months = sorted(m for m in months if m)
    doc = {"_about": "US DOT BTS Reporting Carrier On-Time Performance, boiled down by ontime.py. US airlines' domestic "
                     "flights only. flights: key AIRLINE|NUMBER|ORIGIN|DEST (or AIRLINE|*|ORIGIN|DEST for the route) -> "
                     "[scheduled, arrived within 15 min, cancelled, median arrival delay, 90th-percentile arrival delay]. "
                     "fixer: ORIGIN -> three-hour block of scheduled departure -> counts by delay already reached.",
           "source": "US DOT Bureau of Transportation Statistics, Reporting Carrier On-Time Performance (transtats.bts.gov)",
           "window": "%s to %s" % (months[0], months[-1]) if months else "",
           "thresholds": THRESHOLDS, "slips": SLIPS, "blocks": BLOCKS, "min_flights": MIN_FLIGHTS,
           "flights": out_flights,
           "fixer": {o: {str(b): c for b, c in cells.items()} for o, cells in fixer.items()}}
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    return {"flights": len(out_flights), "airports": len(fixer) - 1, "window": doc["window"], "bytes": os.path.getsize(out)}


_DOC: Dict[str, Any] = {}


def load() -> Dict[str, Any]:
    if not _DOC:
        try:
            with gzip.open(PATH, "rt", encoding="utf-8") as fh:
                _DOC.update(json.load(fh))
        except (OSError, ValueError):
            _DOC.update({"flights": {}, "fixer": {}, "window": "", "source": ""})
    return _DOC


def claim(operating: str, marketing: str, number: Any, origin: str, destination: str,
          regional: Optional[Dict[str, List[str]]] = None) -> Optional[Dict[str, Any]]:
    """The schema's reliability claim for one flight, or None when DOT has no record of it. The operating airline
    files the record under the MARKETING number (a Delta Connection flight flown by Endeavor is 9E with Delta's
    number), so the lookup tries the operator, then the marketer, then the marketer's regional airlines, and
    falls back to the operator's route across all its numbers."""
    D = load()
    F = D.get("flights") or {}
    num = str(number or "").lstrip("0")
    cands = [c for c in [operating, marketing] + list((regional or {}).get(marketing or "", [])) if c]
    rec, how = None, ""
    for c in cands:
        rec = F.get("%s|%s|%s|%s" % (c, num, origin, destination))
        if rec:
            how = "this flight"
            break
    if not rec:
        for c in cands[:2]:
            rec = F.get("%s|*|%s|%s" % (c, origin, destination))
            if rec:
                how = "this airline on this route"
                break
    if not rec:
        return None
    n, ok, canc, p50, p90 = rec
    return {"on_time_fraction": round(ok / float(n), 3), "delay_minutes_p50": int(p50), "delay_minutes_p90": int(p90),
            "sample_size": int(n), "observation_window": "%s, %s" % (how, D.get("window") or "the last year"),
            "source": "US DOT on-time records"}


def fixer_cell(origin: str, sched_minutes: int, delay_minutes: int) -> Optional[Dict[str, Any]]:
    """The counts for flights from `origin` scheduled in the same three-hour block that were already at least
    `delay_minutes` late: {n_late, cancelled, slips:[(extra_minutes, how many had left by then)], basis, threshold}.
    The airport's own block when it holds 30 or more such flights, else the whole country's; None with no table."""
    D = load()
    fx = D.get("fixer") or {}
    if not fx:
        return None
    th = list(D.get("thresholds") or THRESHOLDS)
    ti = max(i for i, t in enumerate(th) if t <= max(0, int(delay_minutes)))
    block = str(min(int(D.get("blocks") or BLOCKS) - 1, max(0, int(sched_minutes)) // 180))
    for where in (origin, "*"):
        c = (fx.get(where) or {}).get(block)
        if c and c["ge"][ti] >= 30:
            out = {"n_late": c["ge"][ti], "cancelled": c["cancelled"], "block_flights": c["n"],
                   "slips": list(zip(D.get("slips") or SLIPS, c["slip"][ti])), "threshold": th[ti],
                   "where": "US flights" if where == "*" else "flights from " + where,
                   "window": D.get("window") or ""}
            # the cancellation rate of flights already this late, read off late-arriving planes: this airport's block
            # when it holds MIN_KNOWN of them, else the country's same block; None with a table built before the step
            for kw in (where, "*"):
                k = (fx.get(kw) or {}).get(block) or {}
                if len(k.get("kn") or []) > ti and k["kn"][ti] >= MIN_KNOWN:
                    out.update(known_late=k["kn"][ti], known_cancelled=k["kc"][ti],
                               known_where="US flights" if kw == "*" else "flights from " + kw)
                    break
            return out
    return None


def cancel_rate(known_late: int, known_cancelled: int, z: float = 1.96):
    """The share of already-late flights that ended cancelled, with its 95% Wilson interval: (low, mid, high)."""
    n = float(known_late)
    if n <= 0:
        return None
    p = known_cancelled / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return max(0.0, mid - half), p, min(1.0, mid + half)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "build":
        print(json.dumps(build(sys.argv[2:]), indent=1))
    elif len(sys.argv) >= 5:
        print(json.dumps(claim(sys.argv[1], sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]), indent=1))
    else:
        print(__doc__)
