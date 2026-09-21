#!/usr/bin/env python3
"""Bake the airlines' own logos into slim.json, so the mockups show them even
where the page cannot reach assets.duffel.com (a claude.ai artifact, a sandbox).

    python3 concorde-travel/ui/mock/logos.py          # fetch every logo slim.json names
    python3 concorde-travel/ui/mock/build.py          # then rebuild the mocks

Run it on a machine with internet. The feed's airline objects carry an SVG per
carrier (logo_symbol_url); data.py keeps the URL under airlines[code].logo, the
page draws a brand-coloured monogram and lays the logo over it when it loads.
This script replaces each URL with the SVG itself as a data URI, so nothing
has to load. The URL is kept beside it under logo_url, and data.py's next run
starts from URLs again, so re-run this after rebuilding slim.json.

The live page should not do this: it takes the URLs straight from the offer.
"""
import base64
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "ConcordeGo-mockups/1.0 (https://github.com/bigmillz/concordeai; design mockup logo fetch)"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), (r.headers.get("Content-Type") or "image/svg+xml").split(";")[0]


def bake(slim, get=fetch, log=print):
    """Replace every airlines[code].logo URL with a data URI. Returns (done, failed)."""
    done, failed = [], []
    for code, a in sorted(slim.get("airlines", {}).items()):
        url = a.get("logo_url") or a.get("logo") or ""
        if not url.startswith("http"):
            continue
        try:
            data, ctype = get(url)
        except Exception as exc:  # a missing logo is a monogram, not a failure of the page
            failed.append((code, str(exc)))
            continue
        a["logo_url"] = url
        a["logo"] = "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode())
        done.append(code)
        log("  %-3s %6d bytes  %s" % (code, len(data), a.get("name", "")))
    return done, failed


def main():
    path = os.path.join(HERE, "slim.json")
    slim = json.load(open(path, encoding="utf-8"))
    done, failed = bake(slim)
    if not done and failed:
        sys.exit("no logo could be fetched (%s) - is this machine online?" % failed[0][1])
    json.dump(slim, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("%s: %d logos baked in%s, %.1f MB. Now: python3 concorde-travel/ui/mock/build.py" % (
        path, len(done), ", %d failed (%s)" % (len(failed), ", ".join(c for c, _ in failed)) if failed else "",
        os.path.getsize(path) / 1e6))


if __name__ == "__main__":
    main()
