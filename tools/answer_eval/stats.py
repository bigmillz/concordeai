#!/usr/bin/env python3
"""Timing and pipeline statistics for one or more runs (no judgments needed).

  python3 tools/answer_eval/stats.py <run-name> [<run-name> ...]

Per run: answers recorded, error answers, mean/median wall time, time to the
first visible text, answer length, the app's tok/s meter (chars/4 over the
whole wait), how often a search ran, which councils and compositors answered,
and the share of questions where a draft was skipped ("no answer").
"""
import glob
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def median(xs):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return float("nan")
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else float("nan")


def compositors(rec):
    out = []
    for e in rec.get("events") or []:
        d = e.get("run")
        if e.get("tag") == "RUN" and isinstance(d, dict) and d.get("c") is not None:
            out.append(str(d["c"]))
    for t, d in rec.get("run_frames") or []:
        if isinstance(d, dict) and d.get("c") is not None:
            out.append(str(d["c"]))
    return out


def main():
    runs = sys.argv[1:]
    if not runs:
        sys.exit(__doc__)
    for run in runs:
        recs = []
        for f in sorted(glob.glob(os.path.join(HERE, "runs", run, "q*.json"))):
            with open(f, encoding="utf-8") as fh:
                r = json.load(fh)
            r["file"] = os.path.basename(f)
            recs.append(r)
        if not recs:
            print("no records for", run)
            continue
        ok = [r for r in recs if not r.get("is_error_answer") and r.get("answer")]
        err = [r for r in recs if r.get("is_error_answer") or not r.get("answer")]
        print("## %s" % run)
        print("- answers: %d recorded, %d usable, %d error/empty (%s)" % (
            len(recs), len(ok), len(err), ", ".join(r["file"] for r in err) or "-"))
        print("- wall time: mean %.0fs, median %.0fs, max %.0fs (usable answers)" % (
            mean([r["elapsed_s"] for r in ok]), median([r["elapsed_s"] for r in ok]),
            max([r["elapsed_s"] for r in ok] or [0])))
        print("- first visible text: mean %.0fs, median %.0fs" % (
            mean([r["first_text_s"] for r in ok]), median([r["first_text_s"] for r in ok])))
        print("- answer length: mean %.0f chars, median %.0f; app meter mean %.1f tok/s" % (
            mean([r["chars"] for r in ok]), median([r["chars"] for r in ok]),
            mean([r["rate_ui_tok_s"] for r in ok])))
        print("- searched: %d of %d; sources shown per searched answer: mean %.1f" % (
            sum(1 for r in recs if r.get("searched")), len(recs),
            mean([len(r.get("sources") or []) for r in recs if r.get("searched")])))
        councils = Counter(r.get("x_models") for r in recs)
        print("- councils (X-Models): " + "; ".join("%dx [%s]" % (n, c) for c, n in councils.most_common()))
        comps = Counter(c for r in recs for c in compositors(r))
        print("- compositor: " + ("; ".join("%dx %s" % (n, c) for c, n in comps.most_common()) or "(none recorded)"))
        skipped = Counter()
        for r in recs:
            for d in r.get("drafts") or []:
                t = d.get("text") or ""
                if t.startswith("(no answer"):
                    skipped[(d.get("m"), t.strip("()").split("—")[-1].strip())] += 1
        if skipped:
            print("- drafts skipped: " + "; ".join("%dx %s (%s)" % (n, m, why) for (m, why), n in skipped.most_common()))
        resets = sum(r.get("resets", 0) for r in recs)
        if resets:
            # a merge that collapsed mid-stream, or (6b515) the fact-check re-sending a corrected answer
            print("- RESET frames (a collapsed merge, or a fact-check's corrected re-send): %d" % resets)
        ver = [s.get("l", "") for r in recs for s in r.get("steps") or []
               if isinstance(s, dict) and s.get("id") == "verify" and s.get("s") == "done"]
        if ver:
            fixed = sum(1 for v in ver if "fixed" in v)
            late = sum(1 for v in ver if "in time" in v)
            print("- fact-check (6b515): ran %d, changed the answer %d, held %d, out of time %d"
                  % (len(ver), fixed, len(ver) - fixed - late, late))
        rates = [r["rate_writer"]["tps"] for r in recs if isinstance(r.get("rate_writer"), dict)
                 and r["rate_writer"].get("tps")]
        if rates:
            rates.sort()
            print("- the final writer's own pace (RATE, 6b515): median %.1f tok/s over %d answers"
                  % (rates[len(rates) // 2], len(rates)))
        # quality-2 (6b518): the critic's notes checked, the corrections, the earlier sources kept
        def _steps(r, sid):
            return [s for s in r.get("steps") or [] if isinstance(s, dict) and s.get("id") == sid
                    and s.get("s") == "done"]
        import re as _re
        dropped = corr = 0
        for r in recs:
            for s in _steps(r, "review")[-1:]:
                d = s.get("d", "")
                m = _re.search(r"(\d+) dropped", d)
                dropped += int(m.group(1)) if m else 0
                m = _re.search(r"(\d+) corrections?", d)
                corr += int(m.group(1)) if m else 0
        fixes = [s.get("d", "") for r in recs for s in _steps(r, "fix")[-1:]]
        if dropped or corr or fixes:
            applied = total = by_check = 0
            for d in fixes:
                m = _re.match(r"(\d+) of (\d+)", d)
                if m:
                    applied += int(m.group(1))
                    total += int(m.group(2))
                m = _re.search(r"(\d+) by the check", d)
                by_check += int(m.group(1)) if m else 0
            print("- critic (6b518): %d note lines dropped (a number or name from nowhere, or a false "
                  "'unsupported'); %d corrections in %d answers, %d applied (%d of them by the fact-check)"
                  % (dropped, total or corr, len(fixes), applied, by_check))
        prior = sum(1 for r in recs if _steps(r, "prior"))
        if prior:
            print("- follow-ups that kept the earlier sources (6b518): %d" % prior)
        log = os.path.join(HERE, "runs", run, "devcopy.log")
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as fh:
                lines = [ln for ln in fh if ln.startswith("ground:")]
            if lines:
                landed = sum(1 for ln in lines if "model=[]" not in ln)
                print("- the model's query plan landed: %d of %d grounded searches" % (landed, len(lines)))
        print()


if __name__ == "__main__":
    main()
