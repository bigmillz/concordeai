#!/usr/bin/env python3
"""The results timeline, measured in a real browser.

    python3 concorde-travel/server.py &          # it does not start one
    python3 concorde-travel/tests/test_timeline.py

Every option sits on ONE clock, so a flight departing at 19:25 must draw its
flight block under the 19:25 mark. If that slips, the picture is not merely
imprecise - it is actively lying, and a timeline you cannot trust is worse than
a list of numbers, because people believe pictures.

WHY THIS IS MEASURED AND NOT EYEBALLED. It looked right in a screenshot while
the lead-time calculation was dropping the airport hour, putting every ribbon
up to sixty minutes adrift of its own tick. CLAUDE.md has said for a long time
that screenshots lie; this is the check that does not.

SKIPS RATHER THAN FAILS when playwright or a server is absent. The product is
stdlib-only and this is the single test that is not, so it must never be the
reason a clean checkout cannot run its suite.
"""

import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:9897/"
FAILS, N = [], [0]


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + (("  — " + detail) if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label
          + (("\n         " + detail) if detail and not cond else ""))


def skip(why):
    print("SKIPPED: %s" % why)
    print("(not a failure - this is the one test that needs a browser)")
    return 0


MEASURE = """() => {
  const ticks = [...document.querySelectorAll('.axis span')].map(s => {
    const b = s.getBoundingClientRect();
    return {label: s.textContent, x: b.left + b.width / 2}; });
  const axis = document.querySelector('.axis');
  const rows = [...document.querySelectorAll('.opt')].map(o => {
    const rib = o.querySelector('.rib'), tr = o.querySelector('.track');
    const fly = o.querySelector('.seg-b.q-fly');
    const end = o.querySelector('.barend');
    const l2 = o.querySelector('.optline2');
    if (!rib || !tr) return null;
    return {when: l2 ? l2.innerText.split('\\n')[0].trim().slice(0, 5) : '',
            trackL: tr.getBoundingClientRect().left,
            trackR: tr.getBoundingClientRect().right,
            ribL: rib.getBoundingClientRect().left,
            ribR: rib.getBoundingClientRect().right,
            flyL: fly ? fly.getBoundingClientRect().left : null,
            endL: end ? end.getBoundingClientRect().left : null,
            segs: [...o.querySelectorAll('.seg-b')].length}; }).filter(Boolean);
  const a = axis ? axis.getBoundingClientRect() : null;
  return {axisL: a && a.left, axisR: a && a.right, ticks, rows};
}"""


def main():
    try:
        urllib.request.urlopen(URL, timeout=4).read(64)
    except (urllib.error.URLError, OSError) as exc:
        return skip("no server on 9897 (%s). Start concorde-travel/server.py" % exc)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return skip("playwright not installed")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
        except Exception:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                return skip("no chromium (%s)" % exc)
        pg = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(URL)
        pg.wait_for_timeout(1200)
        try:
            pg.click("#go", timeout=4000)
        except Exception:
            pass
        pg.wait_for_timeout(3500)
        out = pg.evaluate(MEASURE)
        browser.close()

    print("\nthe page itself")
    check("it renders without throwing", not errors, "; ".join(errors[:2]))
    check("a search produced options", len(out["rows"]) > 0)
    check("and an axis to read them against", len(out["ticks"]) >= 2,
          "%d ticks" % len(out["ticks"]))
    if not out["rows"] or len(out["ticks"]) < 2:
        return report()

    print("\nthe axis and the tracks share one set of gutters")
    r0 = out["rows"][0]
    check("left edges line up", abs(out["axisL"] - r0["trackL"]) < 2,
          "axis %.1f vs track %.1f" % (out["axisL"], r0["trackL"]))
    check("right edges line up", abs(out["axisR"] - r0["trackR"]) < 2,
          "axis %.1f vs track %.1f" % (out["axisR"], r0["trackR"]))

    def mins(lbl):
        h, m = lbl.split(":")
        return int(h) * 60 + int(m)

    t = out["ticks"]
    span = (mins(t[1]["label"]) - mins(t[0]["label"])) % 1440
    ppm = (t[1]["x"] - t[0]["x"]) / span
    check("the ticks are evenly spaced", ppm > 0, "%.4f px/min" % ppm)

    print("\nevery flight block sits under its own departure time")
    for r in out["rows"]:
        if r["flyL"] is None or not r["when"]:
            continue
        # The window can open the evening before and run past midnight, so a
        # departure may sit LEFT of the first tick; modulo alone forces it a
        # day forward. Take whichever reading is on the axis.
        d = (mins(r["when"]) - mins(t[0]["label"])) % 1440
        want = min((t[0]["x"] + c * ppm for c in (d, d - 1440)),
                   key=lambda x: abs(x - r["flyL"]))
        check("departing %s draws at its mark" % r["when"], abs(r["flyL"] - want) < 8,
              "block at %.0f, the clock says %.0f" % (r["flyL"], want))

    print("\nnothing is drawn on top of anything else")
    for r in out["rows"]:
        if r["endL"] is None:
            continue
        check("the figures sit beside the track, not on it (%s)" % r["when"],
              r["ribR"] <= r["endL"] + 1,
              "ribbon ends %.0f, figures start %.0f" % (r["ribR"], r["endL"]))
    check("every ribbon has its segments", all(r["segs"] >= 2 for r in out["rows"]),
          str([r["segs"] for r in out["rows"]]))
    return report()


def report():
    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    if FAILS:
        print()
        for f in FAILS:
            print("  FAIL  " + f)
        return 1
    print("all timeline checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
