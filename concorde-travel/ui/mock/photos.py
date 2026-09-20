#!/usr/bin/env python3
"""Fetch a few high-resolution, properly licensed photos of a city for the
shortlist cards, and write them to photos.json for build.py to inline.

    python3 concorde-travel/ui/mock/photos.py Brooklyn
    python3 concorde-travel/ui/mock/photos.py "Brooklyn Bridge" --count 4 --width 2400
    python3 concorde-travel/ui/mock/build.py            # then rebuild the mocks

Run this on a machine that can reach the internet; the sandbox the mocks were
built in cannot reach any image host, which is why the page ships with
generated skyline artwork as a stand-in until this has been run.

Source: Wikimedia Commons, via its public API (no key). Only files under CC0,
public domain, CC BY or CC BY-SA are kept, and the author, licence and page
URL are written beside each image so the page can print the credit - CC BY
and CC BY-SA require it. Landscape files only; portraits do not work as a
strip across three cards.

In the product this would be a search against Unsplash or Pexels keyed by the
origin city (both free with attribution and a key); the shape written here -
[{src, credit, source}] - is what the page expects from either.
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "ConcordeGo-mockups/1.0 (https://github.com/bigmillz/concordeai; design mockup photo fetch)"
OK_LICENCES = ("cc0", "public domain", "cc by", "cc-by", "cc by-sa", "cc-by-sa")


def api(params):
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers.get("Content-Type", "image/jpeg")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) if "=" in a else (a[2:], "1") for a in sys.argv[1:] if a.startswith("--"))
    query = " ".join(args) or "Brooklyn skyline"
    count = int(opts.get("count", 4))
    width = int(opts.get("width", 2400))
    res = api({"action": "query", "format": "json", "generator": "search", "gsrsearch": query + " filetype:bitmap",
               "gsrnamespace": 6, "gsrlimit": 40, "prop": "imageinfo",
               "iiprop": "url|size|extmetadata|mime", "iiurlwidth": width})
    pages = list((res.get("query") or {}).get("pages", {}).values())
    picked = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if not lic or not any(k in lic.lower() for k in OK_LICENCES):
            continue
        if w < 1600 or h == 0 or w / h < 1.4:
            continue
        artist = (meta.get("Artist") or {}).get("value", "")
        artist = " ".join(t for t in artist.replace("<", " <").split() if not t.startswith("<"))  # strip markup
        picked.append({"url": ii.get("thumburl") or ii.get("url"), "title": p.get("title"),
                       "credit": "%s · %s · Wikimedia Commons" % (artist or "unknown", lic),
                       "source": ii.get("descriptionurl") or ""})
        if len(picked) >= count:
            break
    if not picked:
        sys.exit("nothing suitable for %r (need landscape, >=1600px, CC0/PD/CC BY/CC BY-SA)" % query)
    out = []
    for p in picked:
        data, ctype = fetch(p["url"])
        out.append({"src": "data:%s;base64,%s" % (ctype.split(";")[0], base64.b64encode(data).decode()),
                    "credit": p["credit"], "source": p["source"], "query": query})
        print("  %6d KB  %s" % (len(data) // 1024, p["title"]))
    dest = os.path.join(HERE, "photos.json")
    json.dump(out, open(dest, "w", encoding="utf-8"))
    print("%s: %d photos of %r, %.1f MB. Now: python3 concorde-travel/ui/mock/build.py" % (
        dest, len(out), query, os.path.getsize(dest) / 1e6))


if __name__ == "__main__":
    main()
