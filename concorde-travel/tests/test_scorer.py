#!/usr/bin/env python3
"""Run every fixture's `expect` block against the scorer.

    python3 concorde-travel/tests/test_scorer.py

This is what makes the corpus executable. A fixture states what the scorer must
do — which ledger lines appear, with what sign and what evidence, in what order
the options land, what must NOT appear — and this walks each one. Stdlib only;
no server, no network, no clock.

Beyond the fixtures' own assertions it checks the properties that hold for every
scenario and that no single fixture would catch:

  * determinism — the same inputs scored twice give identical integers
  * reconciliation — the ledger lines sum to the effective cost, exactly
  * grade stability — switching profile re-orders the list and never moves a letter
  * purity — scoring writes nothing and reads no file
"""

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from scorer import (score, score_all, grade, timeline, chosen_ground,  # noqa: E402
                    load_scenario, DEFAULT, Line)

FAILS = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(label + ("  — " + detail if detail else ""))
    print(("  ok   " if cond else "  FAIL ") + label + (("\n         " + detail) if detail and not cond else ""))


def money(c):
    return "$%s" % format(round(c / 100), ",d")


def run(path):
    sc = load_scenario(path)
    name = os.path.basename(path)
    print("\n%s" % name)
    print("  %s" % sc["title"])
    exp = sc.get("expect", {})
    opts = {o["option_id"]: o for o in sc["options"]}

    # ---- the fixture's own assertions --------------------------------
    for prof, want in (exp.get("order") or {}).items():
        got = [l.option_id for l in score_all(sc, prof)]
        check("[%s] order under %s" % (name, prof), got == want,
              "wanted %s\n         got    %s" % (want, got))

    ledgers = {oid: score(sc, o, "reference") for oid, o in opts.items()}

    for a in exp.get("ledger_must_contain", []):
        led = ledgers[a["option_id"]]
        ln = led.by_code(a["code"], a.get("ref", ""))
        label = "[%s] %s has a '%s'%s line" % (name, a["option_id"], a["code"],
                                                 " (%s)" % a["ref"] if a.get("ref") else "")
        if ln is None:
            check(label, False, "no such line; got %s" % sorted({x.code for x in led.lines}))
            continue
        ok, why = True, []
        if "sign" in a:
            want_credit = a["sign"] == "credit"
            is_credit = ln.amount_cents < 0
            if want_credit != is_credit:
                ok = False
                why.append("wanted a %s, got %s" % (a["sign"], money(ln.amount_cents)))
        if "min_cents" in a and abs(ln.amount_cents) < a["min_cents"]:
            ok = False
            why.append("wanted at least %s, got %s" % (money(a["min_cents"]), money(ln.amount_cents)))
        if "max_cents" in a and abs(ln.amount_cents) > a["max_cents"]:
            ok = False
            why.append("wanted at most %s, got %s" % (money(a["max_cents"]), money(ln.amount_cents)))
        for frag in a.get("evidence_mentions", []):
            blob = (ln.evidence + " " + ln.label).lower()
            if frag.lower() not in blob:
                ok = False
                why.append("evidence never mentions %r: %r" % (frag, ln.evidence[:110]))
        check(label + " = " + money(ln.amount_cents), ok, "; ".join(why))

    for a in exp.get("ledger_must_not_contain", []):
        led = ledgers[a["option_id"]]
        ln = led.by_code(a["code"], a.get("ref", ""))
        check("[%s] %s has NO '%s' line" % (name, a["option_id"], a["code"]),
              ln is None, "found %s — %s" % (money(ln.amount_cents), ln.label) if ln else "")

    for r in exp.get("line_relations", []):
        A, B = r["a"], r["b"]
        la = ledgers[A["option_id"]].by_code(A["code"], A.get("ref", ""))
        lb = ledgers[B["option_id"]].by_code(B["code"], B.get("ref", ""))
        label = "[%s] %s/%s %s %s/%s" % (name, A["option_id"], A["code"],
                                         r["op"], B["option_id"], B["code"])
        if la is None or lb is None:
            check(label, False, "one side of the comparison has no such line")
            continue
        if r["op"] == "at_least_x_times":
            ok = abs(la.amount_cents) >= r["x"] * abs(lb.amount_cents)
            detail = "%s is not %gx %s" % (money(la.amount_cents), r["x"], money(lb.amount_cents))
        elif r["op"] == "costs_more_than":
            ok = la.amount_cents > lb.amount_cents
            detail = "%s is not worse than %s" % (money(la.amount_cents), money(lb.amount_cents))
        else:
            ok, detail = False, "unknown op %r" % r["op"]
        check(label, ok, detail)

    for a in exp.get("infeasible", []):
        ref = a["ref"]
        if "/ground.outbound/" in ref:
            oid, mode_id = ref.split("/ground.outbound/")
            oid = oid.split("/")[0]
            mode = next(m for m in opts[oid]["ground"]["outbound"] if m["mode_id"] == mode_id)
            check("[%s] %s is rejected, not merely penalised" % (name, mode_id),
                  not mode["feasible"] and bool(mode.get("weakest_link")))
            # and the ledger has to name the link, not make a blanket claim
            ev = ledgers[oid].by_code("ground_out").evidence.lower()
            check("[%s] the ledger names the leg that breaks" % name,
                  "nj transit" in ev or mode["weakest_link"].split("-")[0] in ev,
                  "evidence was %r" % ev[:140])

    # ---- properties that hold for every scenario ---------------------
    if exp.get("reconciles"):
        for oid, led in ledgers.items():
            check("[%s] %s reconciles" % (name, oid), led.reconciles(),
                  "lines sum to %s but effective is %s"
                  % (money(sum(l.amount_cents for l in led.lines)), money(led.effective_cents)))

    for oid, o in opts.items():
        a, b = score(sc, o, "reference"), score(sc, o, "reference")
        check("[%s] %s scores identically twice" % (name, oid),
              a == b, "two runs disagree — something in the model is not pure")

    if exp.get("grades_stable_across_profiles"):
        # An INDEPENDENT oracle. Calling grade() on both sides of a comparison
        # proves nothing — it did not, for a while: a mutant that graded at the
        # caller's profile sailed through, because the check was asking the same
        # broken function the same question twice.
        par = sc["query"]["route_par_cents"]
        def expected_letter(option):
            eff = score(sc, option, "reference").effective_cents
            idx = eff / par
            for threshold, letter in DEFAULT.grade_bands:
                if idx <= threshold:
                    return letter
            return "F"
        wrong = []
        for oid, o in opts.items():
            got, _ = grade(sc, o)
            want = expected_letter(o)
            if got != want:
                wrong.append("%s graded %s, reference ledger says %s" % (oid, got, want))
        check("[%s] the grade is the REFERENCE ledger against par" % name,
              not wrong, "; ".join(wrong))

        # and it must not move while the order does
        letters = {p: {l.option_id: grade(sc, opts[l.option_id])[0]
                       for l in score_all(sc, p)} for p in sc["query"]["profiles"]}
        first = letters[list(letters)[0]]
        moved = [("%s under %s" % (oid, p))
                 for p, m in letters.items() for oid, g in m.items() if g != first[oid]]
        check("[%s] grades hold across every profile" % name, not moved, ", ".join(moved))

    if exp.get("target_reorders"):
        orders = {p: [l.option_id for l in score_all(sc, p)] for p in sc["query"]["profiles"]}
        distinct = {tuple(v) for v in orders.values()}
        check("[%s] the target actually re-orders the list" % name, len(distinct) > 1,
              "every profile produced the same order — a scorer ignoring the weights "
              "entirely would pass every other check in this file")

    for oid, led in ledgers.items():
        neg = [l for l in led.lines if l.kind == "base" and l.amount_cents <= 0]
        check("[%s] %s has a positive ticket line" % (name, oid), not neg)

    # The bar must be a RENDERING of the ledger, not a second opinion about the
    # same trip. If these two ever disagree the page is lying in one of them.
    for prof in sc["query"]["profiles"]:
        for oid, o in opts.items():
            led = score(sc, o, prof)
            if led.infeasible_reason:
                continue
            tl = timeline(sc, o, prof)
            check("[%s] %s's bar totals its door-to-door time (%s)" % (name, oid, prof),
                  sum(l.minutes for l in tl) == led.door_to_door_minutes,
                  "bar sums to %d, ledger says %d"
                  % (sum(l.minutes for l in tl), led.door_to_door_minutes))

    # "Rideshare, yes or no" narrows the choice; it never silently substitutes.
    # The interesting half is the leg where the preference CANNOT be met.
    import copy
    for want in ("rideshare", "transit"):
        biased = copy.deepcopy(sc)
        biased["query"]["preferences"] = {"ground_mode": want}
        for oid, o in {x["option_id"]: x for x in biased["options"]}.items():
            modes = o["ground"]["outbound"]
            kinds = {m["mode_kind"] for m in modes}
            if want not in kinds:
                continue
            got, _ = chosen_ground(o, biased["query"]["profiles"]["reference"],
                                   "outbound", want)
            avail = [m for m in modes if m["mode_kind"] == want and m["feasible"]]
            led = score(biased, o, "reference")
            ev = (led.by_code("ground_out") or Line("", "", 0)).evidence.lower()
            if avail:
                check("[%s] %s honours a %s preference" % (name, oid, want),
                      got["mode_kind"] == want,
                      "picked %s instead" % got.get("mode_kind"))
            else:
                # the preference exists in the data but is not flyable at this hour
                check("[%s] %s says so when %s is not possible" % (name, oid, want),
                      "you asked for" in ev,
                      "evidence was %r" % ev[:120])

    # A credit for going into the city, given at an hour when there is no city to
    # go into, is the single most embarrassing thing this model could do. The
    # guard is cheap to write and was untested until a mutant walked through it.
    for oid, led in ledgers.items():
        for ln in led.all_by_code("layover"):
            ev = ln.evidence.lower()
            if "nothing open" in ev:
                check("[%s] %s/%s is not credited for leaving a shut airport"
                      % (name, oid, ln.ref),
                      "leave the airport" not in ev and ln.amount_cents > 0,
                      "evidence says %r and the line is %s" % (ln.evidence[:100], money(ln.amount_cents)))

    return sc


def invariants():
    """What the model means, asserted directly. A fixture can show that a dead
    layover costs more than a lively one; only this can say it must."""
    print("\nmodel invariants")
    t = DEFAULT
    check("dead layover time costs more than merely-stuck time",
          t.layover_frac_dead > t.layover_frac_open)
    check("stuck-in-the-terminal time costs more than time you can spend elsewhere",
          t.layover_frac_open > t.layover_frac_leavable)
    check("an unusable hour is never cheaper than a usable one",
          t.layover_frac_dead > t.layover_frac_leavable)
    check("grade bands rise monotonically",
          all(t.grade_bands[i][0] < t.grade_bands[i + 1][0]
              for i in range(len(t.grade_bands) - 1)))
    check("a misconnect probability is a probability",
          0.0 <= t.misconnect_floor < t.misconnect_ceiling <= 1.0)
    check("an abstention is never free",
          t.abstain_reliability_cents > 0 and t.abstain_claim_cents > 0)
    check("accepting red-eyes makes them cheaper, not free",
          0 < t.redeye_accepted_cents < t.redeye_cents)


def main():
    root = os.path.join(HERE, "..", "..", "fixtures")
    paths = sorted(glob.glob(os.path.join(root, "0*.json")))
    if not paths:
        print("no fixtures found at %s" % root)
        return 1
    for p in paths:
        run(p)
    invariants()

    print("\n%d checks, %d failed" % (CHECKS[0], len(FAILS)))
    if FAILS:
        print()
        for f in FAILS:
            print("  FAIL  " + f)
        return 1
    print("all scorer checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
