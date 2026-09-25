#!/usr/bin/env python3
"""Inline the real scored data into each mockup so every file opens standalone.

    python3 concorde-travel/ui/mock/build.py

Each mock-N.src.html carries a __DATA__ token; this writes mock-N.html with the
real JFK-LHR numbers baked in (slim.json, made by data.py). Standalone on purpose - a mockup you have to
start a server to look at is a mockup nobody looks at. They also serve from
server.py at /mock/N for side-by-side flipping.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
data = open(os.path.join(HERE, "slim.json"), encoding="utf-8").read()

import glob
import json
import subprocess, datetime
import sys as _sys
_sys.path.insert(0, os.path.join(HERE, "..", ".."))
import points      # noqa: E402  the transfer table, programmes and status bags the page plans with (enrichment/points.json)
POINTS = json.dumps(points.page_table(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
import addons      # noqa: E402  published add-on prices (enrichment/addons.json); the sources stay in the file
ADDONS = json.dumps(addons.page_table(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
# The stamp in the page footers: the checkout's HEAD and its date. server.py rewrites both from git at serve time,
# so a served page always shows what is running; this is what a page opened as a file shows.
def _git(*args):
    try: return subprocess.run(["git"] + list(args), cwd=HERE, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception: return ""
BUILD = _git("rev-parse", "--short", "HEAD") or "dev"
UPDATED = (_git("log", "-1", "--format=%cs") or datetime.date.today().isoformat())
for src in sorted(glob.glob(os.path.join(HERE, "mock-*.src.html"))):
    n = int(os.path.basename(src)[5:-9])
    html = open(src, encoding="utf-8").read()
    assert "__DATA__" in html, "mock-%d has no __DATA__ token" % n
    # Photos are optional and only some mocks carry the token: photos.json is
    # written by photos.py on a machine that can reach an image host.
    photos = os.path.join(HERE, "photos.json")
    ph = open(photos, encoding="utf-8").read() if os.path.exists(photos) else "[]"
    out = os.path.join(HERE, "mock-%d.html" % n)
    open(out, "w", encoding="utf-8").write(html.replace("__DATA__", data).replace("__PHOTOS__", ph)
                                           .replace("__BUILD__", BUILD).replace("__UPDATED__", UPDATED)
                                           .replace("__POINTS__", POINTS).replace("__ADDONS__", ADDONS))
    print("mock-%d.html  %6.0f KB" % (n, os.path.getsize(out) / 1024))
    # One bad line kills the whole client silently (a duplicate const did, 2026-09-21), so every built page's
    # scripts are syntax-checked with node when it is on the machine. Skipped, and said so, without it.
    import re, shutil, tempfile, sys
    if shutil.which("node"):
        built = open(out, encoding="utf-8").read()
        for i, js in enumerate(re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application)[^>]*>(.*?)</script>", built, re.S)):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                fh.write(js)
            r = subprocess.run(["node", "--check", fh.name], capture_output=True, text=True)
            os.unlink(fh.name)
            if r.returncode:
                sys.exit("mock-%d.html script %d does not parse; the page would be dead:\n%s" % (n, i, r.stderr.strip()[-600:]))
    elif n == 10:
        print("  (no node on this machine: the scripts were not syntax-checked)")
    # A script that parses can still die while the page loads: a shared const read before its line has run throws in
    # the temporal dead zone and stops every line after it (2026-09-25: the lounge chip's rules were read by prepWants,
    # which runs at load, above where they were defined). So mock 10 is opened in headless Chrome, when this machine
    # has it, and any error while it loads stops the build. Skipped, and said so, without Chrome.
    if n == 10:
        chrome = next((c for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome") or "",
                                   shutil.which("chromium") or "", shutil.which("chromium-browser") or "") if c and os.path.exists(c)), None)
        if chrome:
            page = open(out, encoding="utf-8").read()
            probe = ('<script>window.__E=[];addEventListener("error",e=>__E.push(e.error&&e.error.stack||e.message));'
                     'addEventListener("unhandledrejection",e=>__E.push(String(e.reason&&e.reason.stack||e.reason)));</script>')
            done = ('<script>addEventListener("load",()=>setTimeout(()=>{document.body.innerHTML="<pre id=loaderr></pre>";'
                    'document.getElementById("loaderr").textContent=JSON.stringify(__E)},800));</script>')
            i = page.index("<head>") + 6 if "<head>" in page else 0
            page = page[:i] + probe + page[i:]
            j = page.rindex("</body>")
            page = page[:j] + done + page[j:]
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
                fh.write(page)
            try:
                r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", "file://" + fh.name],
                                   capture_output=True, text=True, timeout=120)
            finally:
                os.unlink(fh.name)
            m = re.search(r'<pre id="loaderr">(.*?)</pre>', r.stdout, re.S)
            import html as _html
            errs = json.loads(_html.unescape(m.group(1))) if m else None
            if errs is None:
                print("  (headless Chrome gave no answer: the page was not load-checked)")
            elif errs:
                sys.exit("mock-10.html throws while it loads; the page would be dead:\n" + "\n".join(str(e)[:400] for e in errs[:3]))
            else:
                print("  mock-10.html loads with no errors (headless Chrome)")
        else:
            print("  (no Chrome on this machine: the page was not load-checked)")
