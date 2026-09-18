#!/usr/bin/env python3
"""The narrator's guard rails.

    python3 concorde-travel/tests/test_narrator.py

No network and no API key needed: every check here runs against the brief and
the verifier, which is the point. The verifier is the entire safety argument
for letting a model write copy in a product whose first hard rule is that the
model does not rank - so it is tested adversarially, with sentences that are
fluent, plausible and wrong.
"""

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import narrator                                             # noqa: E402
import scorer                                               # noqa: E402

FAILS, N = [], [0]


def check(label, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(label + (("  — " + detail) if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


def views_for(path, profile="reference"):
    """The same shape server.py hands the page, built without importing it."""
    sc = scorer.load_scenario(path)
    out = []
    for o in sc["options"]:
        led = scorer.score(sc, o, profile)
        if led.filtered_reason or led.infeasible_reason:
            continue
        letter, _ = scorer.grade(sc, o)
        out.append({
            "option_id": o["option_id"], "carrier": o["segments"][0]["marketing"]["carrier"],
            "fare_brand": ", ".join(t["fare_brand_name"] for t in o["tickets"]),
            "route": " - ".join([o["segments"][0]["origin"]["iata"]]
                                + [s["destination"]["iata"] for s in o["segments"]]),
            "ticket_cents": led.lines[0].amount_cents,
            "effective_cents": led.effective_cents,
            "door_to_door_minutes": led.door_to_door_minutes,
            "stops": len(o["segments"]) - 1, "grade": letter,
            "lines": [{"code": l.code, "label": l.label, "amount_cents": l.amount_cents,
                       "evidence": l.evidence, "kind": l.kind} for l in led.lines],
        })
    out.sort(key=lambda v: (v["effective_cents"], v["option_id"]))
    return sc, out


def main():
    root = os.path.join(HERE, "..", "..", "fixtures")
    paths = sorted(glob.glob(os.path.join(root, "0*.json")))

    for path in paths:
        name = os.path.basename(path)
        sc, views = views_for(path)
        fx = {"destination": sc["query"]["destination"]["label"]}
        b = narrator.brief(fx, views, "reference")
        t = narrator.template(b)
        print("\n%s" % name)
        print("  lead: %s" % t["lead"])
        print("  sub:  %s" % t["sub"])

        # --- the floor is always usable -------------------------------
        check("[%s] the template says something" % name, bool(t["lead"]) and bool(t["sub"]))
        ok, why = narrator.verify(t["lead"] + " " + t["sub"], b)
        check("[%s] the template passes its own verifier" % name, ok, why)

        # --- the brief is a closed vocabulary -------------------------
        check("[%s] the brief precomputes the figures" % name,
              bool(b["allowed"]["money"]) and bool(b["allowed"]["percent"]),
              "money=%s pct=%s" % (b["allowed"]["money"][:3], b["allowed"]["percent"][:3]))
        blob = json.dumps(b)
        check("[%s] the brief carries no option the page is not showing" % name,
              all(v["option_id"] not in blob for v in views[3:]) if len(views) > 3 else True)

        # --- adversarial: fluent, plausible, wrong --------------------
        top = b["options"][0]
        bad = [
            ("a dollar figure nobody computed",
             "Top pick %s at %s, about $9,412 effective." % (top["route"], top["ticket"])),
            ("a percentage nobody computed",
             "You can save 47% on the ticket by flying the other one."),
            ("an airport not in the brief",
             "Top pick routes through SIN and lands early."),
            ("an unhedged connectivity promise",
             "Top pick %s at %s - you will have wifi the whole way." % (top["route"], top["ticket"])),
            ("a guarantee about the aircraft",
             "This is guaranteed to be flown by the refreshed cabin."),
            ("a duration nobody computed",
             "It is only 3h11m door to door."),
            ("an essay instead of a sentence",
             "Top pick. " + ("This option is worth considering carefully. " * 20)),
        ]
        for label, sentence in bad:
            ok, why = narrator.verify(sentence, b)
            check("[%s] rejects %s" % (name, label), not ok,
                  "verifier accepted it: %r" % sentence[:90])

        # --- false positives are as damaging as misses ----------------
        # A verifier that rejects its own template downgrades every narration
        # to the fallback and nobody notices, because the fallback is fine.
        punctuated = ("Top pick at %s, %s effective, and %s door to door."
                      % (top["ticket"], top["effective"], top["door_to_door"]))
        ok, why = narrator.verify(punctuated, b)
        check("[%s] a comma after a figure is punctuation, not a digit" % name, ok, why)

        # --- and does not reject honest prose -------------------------
        good = ("Top pick %s at %s, %s effective, %s door to door. Grade %s."
                % (top["route"], top["ticket"], top["effective"],
                   top["door_to_door"], top["grade"]))
        ok, why = narrator.verify(good, b)
        check("[%s] accepts prose built from the brief" % name, ok, why)

    # --- the model path degrades rather than breaks --------------------
    print("\nfallback behaviour")
    sc, views = views_for(paths[0])
    b = narrator.brief({"destination": "x"}, views, "reference")
    out = narrator.narrate(b, allow_model=False)
    check("with the model switched off, the template ships",
          out["source"] == "template" and bool(out["lead"]))

    saved = {k: os.environ.pop(k, None) for k in
             ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")}
    try:
        out = narrator.narrate(b, allow_model=True)
        check("with no credentials, the template ships and says why",
              out["source"] == "template" and "note" in out, out.get("note", "no note"))
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    empty = narrator.brief({"destination": "x"}, [], "reference")
    out = narrator.narrate(empty)
    check("an empty result set does not explode", out["source"] == "template")

    print("\n%d checks, %d failed" % (N[0], len(FAILS)))
    if FAILS:
        print()
        for f in FAILS:
            print("  FAIL  " + f)
        return 1
    print("all narrator checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
