#!/usr/bin/env python3
"""Airline ratings from published scores: enrichment/carrier_scores.json -> enrichment/carriers.json.

    python3 concorde-travel/rate_airlines.py            # print what it would write, and any row that would change
    python3 concorde-travel/rate_airlines.py --write

A rating is ARITHMETIC over published scores, never an opinion (hard rule 1: a model may author the table, only the
arithmetic reads it; per Patrick, 2026-09-24, who asked whether AI could give "a general consensus for each airline"
and got this instead of a question asked at search time). Each score goes on 0..1 by a FIXED absolute scale, never by
where it sits among these airlines, so adding an airline never moves another's rating. The available scores are
averaged with fixed weights, and one linear map puts the result on the table's scale, fitted to the ratings a person
reviewed (rows without "reviewed": false), which are kept as they are. Skytrax rates low-cost and leisure airlines on
their own scales, so their stars move onto the full-service scale by an offset fitted in the same calibration.

Where Skytrax rates short-haul economy differently from long-haul, the row gets a `short_haul` block, which
adapter._carrier_rating uses on a short-haul trip.
"""
import itertools, json, os, re, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCORES = os.path.join(HERE, "enrichment", "carrier_scores.json")
CARRIERS = os.path.join(HERE, "enrichment", "carriers.json")
ALIAS = {"W6": ["W9", "W4", "5W"], "DY": ["D8"], "U2": ["EC", "DS"], "HV": ["TO"]}   # same airline group, same product

# fixed absolute scales: (value read as 0, value read as 1)
SCALES = {"stars": (1.0, 5.0), "airhelp": (5.5, 8.5), "acsi": (65.0, 85.0), "jdpower": (550.0, 750.0)}
# Skytrax economy stars weigh most; AirHelp less, because half of it is punctuality (the grade's reliability part
# already has that) and claim handling (mostly EU261); the two passenger surveys where they exist (US, Canada)
WEIGHTS = {"stars": 0.45, "airhelp": 0.15, "acsi": 0.20, "jdpower": 0.20}
CLAMP = (0.30, 0.95)


def unit(kind, v):
    lo, hi = SCALES[kind]
    return max(0.0, min(1.0, (float(v) - lo) / (hi - lo)))


def composite(stars, airhelp, acsi, jdp):
    parts = [(k, unit(k, v)) for k, v in (("stars", stars), ("airhelp", airhelp), ("acsi", acsi), ("jdpower", jdp))
             if v is not None]
    if not parts:
        return None
    w = sum(WEIGHTS[k] for k, _ in parts)
    return sum(WEIGHTS[k] * u for k, u in parts) / w


def stars_for(sk, haul, offs):
    """(stars on the full-service scale, stars as published) for this haul's economy; the other haul, then the
    overall certification, when Skytrax publishes only one."""
    if not sk:
        return None, None
    order = ("economy_long_haul", "economy_short_haul") if haul == "long" else ("economy_short_haul", "economy_long_haul")
    s = next((sk[k] for k in order + ("overall_stars",) if sk.get(k) is not None), None)
    if s is None:
        return None, None
    return max(1.0, s - offs.get(sk.get("scale"), 0.0)), s


def comp(a, haul, offs):
    st, _ = stars_for(a.get("skytrax"), haul, offs)
    if st is None and a.get("skytrax_stars") is not None:
        st = a["skytrax_stars"]
    return composite(st, a.get("airhelp_score"), a.get("acsi_score"), a.get("jdpower_score"))


def build(scores, table):
    reviewed = {k: v for k, v in table["ratings"].items() if v.get("reviewed") is not False}
    air = scores["airlines"]
    best = None
    for lc, le in itertools.product((0, 0.5, 1.0, 1.5), (0, 0.5, 1.0)):
        offs = {"low-cost": lc, "leisure": le}
        pts = [(comp(air[k], "long", offs), v["rating"], k) for k, v in sorted(reviewed.items())
               if k in air and comp(air[k], "long", offs) is not None]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        a0 = my - b * mx
        sse = sum((a0 + b * x - y) ** 2 for x, y in zip(xs, ys))
        if best is None or sse < best[0] - 1e-9:
            best = (sse, lc, le, a0, b, len(pts))
    sse, lc, le, a0, b, n = best
    offs = {"low-cost": lc, "leisure": le}
    R = lambda c: round(max(CLAMP[0], min(CLAMP[1], a0 + b * c)), 2)
    as_of = scores.get("_as_of")

    new = {}
    for code, a in sorted(air.items()):
        c_long, c_short = comp(a, "long", offs), comp(a, "short", offs)
        if c_long is None:
            continue
        sk = a.get("skytrax") or {}
        _, raw_l = stars_for(sk, "long", offs)
        _, raw_s = stars_for(sk, "short", offs)
        bits = []
        if raw_l is not None:
            bits.append("Skytrax economy %g stars%s%s" % (raw_l, "" if sk.get("scale") == "full" else " on its %s scale" % sk.get("scale"),
                                                         " (page %s)" % sk["page_year"] if sk.get("page_year") else ""))
        if a.get("airhelp_score") is not None:
            bits.append("AirHelp %g (%s)" % (a["airhelp_score"], int(a.get("airhelp_year") or 2025)))
        if a.get("acsi_score") is not None:
            bits.append("ACSI %g (%s)" % (a["acsi_score"], int(a.get("acsi_year") or 0)))
        if a.get("jdpower_score") is not None:
            bits.append("J.D. Power %g (%s)" % (a["jdpower_score"], int(a.get("jdpower_year") or 0)))
        if len(bits) == 1:
            bits.append("the only published score found")
        row = {"rating": R(c_long), "note": (a.get("note") or "").strip().rstrip("."), "basis": ", ".join(bits),
               "sources": [u for u in (sk.get("url"), a.get("airhelp_url"), a.get("acsi_url"), a.get("jdpower_url")) if u],
               "as_of": as_of, "reviewed": False,
               "source": "published scores (Skytrax, AirHelp, ACSI, J.D. Power), formula in _method"}
        if raw_s is not None and raw_l is not None and raw_s != raw_l:
            row["short_haul"] = {"rating": R(c_short),
                                 "note": "Short-haul economy rated %g stars by Skytrax against %g long-haul" % (raw_s, raw_l),
                                 "basis": row["basis"].replace("Skytrax economy %g stars" % raw_l,
                                                               "Skytrax short-haul economy %g stars" % raw_s, 1)}
        new[code] = row
    for code, al in ALIAS.items():
        for c in al:
            if code in new and c not in new:
                new[c] = dict(new[code], note=new[code]["note"] + " (same airline group as %s)" % code)

    merged = {}
    for code in sorted(set(reviewed) | set(new)):
        if code in reviewed:
            r = {k: v for k, v in reviewed[code].items() if k not in ("basis", "published_sources", "short_haul")}
            n_ = new.get(code)
            if n_:
                r["basis"], r["published_sources"] = n_["basis"], n_["sources"]
                if n_.get("short_haul"):
                    r["short_haul"] = {"rating": round(r["rating"] + n_["short_haul"]["rating"] - n_["rating"], 2),
                                       "note": n_["short_haul"]["note"], "basis": n_["short_haul"]["basis"]}
            merged[code] = r
        else:
            merged[code] = new[code]
    method = {
        "written": as_of,
        "what": "Rows marked reviewed:false are arithmetic over published scores (enrichment/carrier_scores.json), not "
                "opinion, made by concorde-travel/rate_airlines.py: each score on 0..1 by a fixed absolute scale, the "
                "available ones averaged with fixed weights, and a linear map fitted to the reviewed rows puts the "
                "result on this table's scale.",
        "scales": {k: {"reads_as_0": v[0], "reads_as_1": v[1]} for k, v in SCALES.items()},
        "weights": WEIGHTS,
        "skytrax": "Economy stars for the haul (long-haul for the main rating, short-haul for the short_haul block); "
                   "low-cost-scale stars minus %g and leisure-scale minus %g to sit on the full-service scale" % (lc, le),
        "calibration": {"rating": "%.4f + %.4f x composite" % (a0, b), "fitted_on": sorted(reviewed),
                        "typical_miss": round((sse / n) ** 0.5, 3), "clamp": list(CLAMP)},
        "short_haul": "A short_haul block exists where Skytrax rates short-haul economy differently from long-haul; the "
                      "adapter uses it when the trip is short-haul (adapter._short_haul).",
    }
    return merged, method


if __name__ == "__main__":
    scores = json.load(open(SCORES, encoding="utf-8"))
    table = json.load(open(CARRIERS, encoding="utf-8"))
    merged, method = build(scores, table)
    changed = [c for c in sorted(set(merged) | set(table["ratings"])) if merged.get(c) != table["ratings"].get(c)]
    print("%d rated; calibration %s (typical miss %s); %d rows would change%s"
          % (len(merged), method["calibration"]["rating"], method["calibration"]["typical_miss"], len(changed),
             (": " + ", ".join(changed)) if changed else ""))
    if "--write" in sys.argv:
        table["ratings"], table["_method"] = merged, method
        with open(CARRIERS, "w", encoding="utf-8") as fh:
            json.dump(table, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("written", CARRIERS)
    else:
        raise SystemExit(1 if changed else 0)
