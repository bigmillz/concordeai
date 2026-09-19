#!/usr/bin/env python3
"""Inline the real scored data into each mockup so every file opens standalone.

    python3 concorde-travel/ui/mock/build.py

Each mock-N.src.html carries a __DATA__ token; this writes mock-N.html with the
real JFK-LHR numbers baked in. Standalone on purpose - a mockup you have to
start a server to look at is a mockup nobody looks at. They also serve from
server.py at /mock/N for side-by-side flipping.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
data = open(os.path.join(HERE, "slim.json"), encoding="utf-8").read()

for n in (1, 2, 3, 4):
    src = os.path.join(HERE, "mock-%d.src.html" % n)
    if not os.path.exists(src):
        continue
    html = open(src, encoding="utf-8").read()
    assert "__DATA__" in html, "mock-%d has no __DATA__ token" % n
    out = os.path.join(HERE, "mock-%d.html" % n)
    open(out, "w", encoding="utf-8").write(html.replace("__DATA__", data))
    print("mock-%d.html  %6.0f KB" % (n, os.path.getsize(out) / 1024))
