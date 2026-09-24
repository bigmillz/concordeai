"""Price parity: ConcordeGo's live search against Google Flights (through SerpApi), ticket price only.

Not part of the offline suites: it spends real searches (a Duffel search and a SerpApi call per leg, plus
one SerpApi round-trip call per trip unless PARITY_NO_RT=1) and needs the keys. Written for the price check
of 2026-09-24 (per Patrick: "make sure that we're at least on par with what Google Flights can come up
with"). On the droplet, as the service user with the service's own environment file:

  systemd-run --uid=concordego --gid=concordego -p EnvironmentFile=/etc/concordego.env \
      --wait --pipe --collect python3 /opt/concordego/concorde-travel/tests/parity_google.py [trip numbers...]

It keeps its own HOME (PARITY_HOME, counters and cache), so the site's daily allowances are untouched, writes
a JSON report to PARITY_OUT, and never prints a key. For each leg: the cheapest ticket the page carries and
where it came from (the feed or the Google supplement), the feed's own cheapest, Google's cheapest, and
Google's cheapest dozen matched flight by flight against the feed.
"""
import json, os, sys, time, datetime, urllib.parse, urllib.request

HOME = os.environ.get("PARITY_HOME", "/var/lib/concordego/parity/home")
os.makedirs(HOME, exist_ok=True)
os.environ["HOME"] = HOME
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [APP, os.path.join(APP, "ui", "mock")]
import server, places  # noqa: E402

OUT = os.environ.get("PARITY_OUT", "/var/lib/concordego/parity")
KEY = os.environ.get("CONCORDEGO_SERPAPI_KEY", "")

TRIPS = [
    ("New York", "London", "2026-11-04", "2026-11-11"),
    ("New York", "Los Angeles", "2026-10-22", "2026-10-26"),
    ("Chicago", "Miami", "2026-11-06", "2026-11-09"),
    ("New York", "Tokyo", "2026-11-10", "2026-11-24"),
    ("London", "Barcelona", "2026-10-30", "2026-11-02"),
]


def serp(params):
    q = dict(engine="google_flights", hl="en", gl="us", currency="USD", adults=1, travel_class=1, api_key=KEY)
    q.update(params)
    req = urllib.request.Request("https://serpapi.com/search.json?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "ConcordeGo-parity/1.0"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as exc:
        return {"error": type(exc).__name__ + ": " + str(exc).replace(KEY, "***")}
    d.pop("search_metadata", None)
    (d.get("search_parameters") or {}).pop("api_key", None)
    return d


def g_itins(d):
    out = []
    for grp in ("best_flights", "other_flights"):
        for it in d.get(grp) or []:
            segs = it.get("flights") or []
            if not segs or it.get("price") is None:
                continue
            out.append({"price": int(it["price"]),
                        "fn": tuple(str(s.get("flight_number", "")).replace(" ", "").upper() for s in segs),
                        "dep": tuple((s.get("departure_airport") or {}).get("id", "") + " " + str((s.get("departure_airport") or {}).get("time", ""))[-5:] for s in segs),
                        "airlines": sorted({s.get("airline", "") for s in segs}),
                        "stops": len(segs) - 1,
                        "route": "-".join([(segs[0].get("departure_airport") or {}).get("id", "")] + [(s.get("arrival_airport") or {}).get("id", "") for s in segs])})
    return out


def duffel_offers(raw):
    offers = ((raw or {}).get("data") or {}).get("offers") if isinstance((raw or {}).get("data"), dict) else (raw or {}).get("offers")
    out = []
    for o in offers or []:
        segs = [s for sl in o.get("slices", []) for s in sl.get("segments", [])]
        if not segs:
            continue
        try:
            cents = int(round(float(o["total_amount"]) * 100))
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"cents": cents, "cur": o.get("total_currency"), "owner": (o.get("owner") or {}).get("iata_code"),
                    "fn": tuple(((s.get("marketing_carrier") or {}).get("iata_code", "") + str(s.get("marketing_carrier_flight_number", ""))).upper() for s in segs),
                    "dep": tuple((s.get("origin") or {}).get("iata_code", "") + " " + str(s.get("departing_at", ""))[11:16] for s in segs),
                    "stops": len(segs) - 1, "brand": (o.get("slices") or [{}])[0].get("fare_brand_name"),
                    "route": "-".join([(segs[0].get("origin") or {}).get("iata_code", "")] + [(s.get("destination") or {}).get("iata_code", "") for s in segs])})
    return out


def ours(typed_from, typed_to, date):
    req = {"source": "api", "origin": typed_from, "origin_address": typed_from, "destination": typed_to,
           "date": date, "adults": 1, "checked_bags": 0, "cabin": "economy", "trip": {"leg": 1}}
    t0 = time.time()
    res = server.search_request(req)
    took = round(time.time() - t0, 1)
    if res.get("error"):
        return {"error": res["error"], "took_s": took}
    shown = {}
    for prof, lst in (res.get("results") or {}).items():
        if isinstance(lst, list):
            for e in lst:
                shown[e["id"]] = e
    top = (res["results"].get("cheapest") or [None])[0]
    got = server._fetch_raw(req)          # the same query again: served from the cache, costs nothing
    raw, meta = (got[0], got[1]) if len(got) == 4 else (None, {})
    feed = duffel_offers(raw)
    sup = meta.get("supplement_payload")
    return {
        "took_s": took, "total_found": res["query"].get("total_found"), "dropped": res["query"].get("dropped"),
        "fetched": (res.get("feed") or {}).get("fetched"), "shown_count": len(shown),
        "shown_min_cents": min((e["ticket_cents"] for e in shown.values()), default=None),
        "shown_min": min(({"cents": e["ticket_cents"], "carrier": e["carrier"], "flight": e["flight"], "route": "-".join(e["route"]), "stops": e["stops"], "brand": e.get("brand")} for e in shown.values()), key=lambda x: x["cents"], default=None),
        "top_pick": top and {"cents": top["ticket_cents"], "carrier": top["carrier"], "flight": top["flight"], "route": "-".join(top["route"]), "stops": top["stops"], "brand": top.get("brand"), "grade": top.get("grade")},
        "feed_count": len(feed), "feed_min": min(feed, key=lambda x: x["cents"]) if feed else None,
        "feed_currencies": sorted({o["cur"] for o in feed}),
        "feed_carriers": sorted({o["owner"] for o in feed if o["owner"]}),
        "supplement": (meta.get("supplement") or {}), "supplement_count": len(g_itins(sup or {})),
        "shown_min_source": min(((e["ticket_cents"], "google" if str(e["id"]).startswith("google-") else "feed") for e in shown.values()), default=(None, None))[1],
        "_feed": feed, "_sup": sup,
    }


def match(g, feed):
    """Google's itineraries against the feed's: the same flights by number, else by departure times."""
    by_fn, by_dep = {}, {}
    for o in feed:
        by_fn.setdefault(o["fn"], []).append(o); by_dep.setdefault(o["dep"], []).append(o)
    rows = []
    for it in sorted(g, key=lambda x: x["price"])[:12]:
        m = by_fn.get(it["fn"]) or by_dep.get(it["dep"]) or []
        best = min(m, key=lambda x: x["cents"]) if m else None
        rows.append({"google": it["price"], "fn": "/".join(it["fn"]), "route": it["route"], "airlines": it["airlines"],
                     "ours": best and round(best["cents"] / 100), "our_brand": best and best["brand"]})
    return rows


def leg(typed_from, typed_to, date):
    o = ours(typed_from, typed_to, date)
    fc, _, _ = places.resolve(typed_from); tc, _, _ = places.resolve(typed_to)
    # the supplement IS Google's one-way answer for this query (every airline since 2026-09-24): reuse it
    g = o.get("_sup") or serp({"type": 2, "departure_id": ",".join(places.airports_for(fc)), "arrival_id": ",".join(places.airports_for(tc)), "outbound_date": date})
    gi = g_itins(g) if not g.get("error") else []
    return {"from": typed_from, "to": typed_to, "date": date, "ours": {k: v for k, v in o.items() if k not in ("_feed", "_sup")},
            "google_error": g.get("error"), "google_count": len(gi),
            "google_min": min(gi, key=lambda x: x["price"]) if gi else None,
            "google_insights": g.get("price_insights"),
            "matches": match(gi, o.get("_feed") or [])}


def main(which):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"run_at": stamp, "trips": []}
    for n in which:
        a, b, d1, d2 = TRIPS[n - 1]
        print("trip %d: %s <-> %s, %s / %s" % (n, a, b, d1, d2), flush=True)
        t = {"n": n, "from": a, "to": b, "out": d1, "back": d2}
        t["legs"] = [leg(a, b, d1), leg(b, a, d2)]
        fc, _, _ = places.resolve(a); tc, _, _ = places.resolve(b)
        rt = {"error": "skipped (PARITY_NO_RT)"} if os.environ.get("PARITY_NO_RT") else serp({"type": 1, "departure_id": ",".join(places.airports_for(fc)), "arrival_id": ",".join(places.airports_for(tc)),
                   "outbound_date": d1, "return_date": d2})
        ri = g_itins(rt) if not rt.get("error") else []
        t["google_rt_error"] = rt.get("error")
        t["google_rt_min"] = min(ri, key=lambda x: x["price"]) if ri else None
        report["trips"].append(t)
        for L in t["legs"]:
            o = L["ours"]
            print("  %s -> %s %s: ours shown min %s [%s] (top pick %s), feed min %s, %s offers; google min %s (%s)" % (
                L["from"], L["to"], L["date"],
                o.get("shown_min_cents") and "$%d" % round(o["shown_min_cents"] / 100), o.get("shown_min_source"),
                o.get("top_pick") and "$%d %s" % (round(o["top_pick"]["cents"] / 100), o["top_pick"]["flight"]),
                o.get("feed_min") and "$%d %s" % (round(o["feed_min"]["cents"] / 100), "/".join(o["feed_min"]["fn"])),
                o.get("feed_count"), L["google_min"] and "$%d" % L["google_min"]["price"],
                L["google_min"] and "/".join(L["google_min"]["fn"])), flush=True)
        print("  google round trip min %s" % (t["google_rt_min"] and "$%d" % t["google_rt_min"]["price"]), flush=True)
    path = os.path.join(OUT, "parity-%s.json" % stamp)
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1, default=list)
    print("saved", path)


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1:]] or [1, 2, 3, 4, 5])
