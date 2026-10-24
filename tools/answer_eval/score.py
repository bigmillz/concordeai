#!/usr/bin/env python3
"""Aggregate judgments into scores per category and compare runs.

  python3 tools/answer_eval/score.py <run-name> [<run-name> ...] [--md]

Reads judgments/<run-name>/*.json (one object per answer) and
judgments/<run-name>/*.jsonl (one object per line), joins them with
questions.json for the categories, and prints: mean of each of the six scores
and of `overall`, per category and overall; the count of hallucination flags by
severity; the number of error answers (all ones). With several runs the table
puts them side by side. Writes runs/<run-name>/scores.md for each run.
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = ["correct_current", "grounded", "depth", "fit", "structure", "concise"]
SHORT = {"correct_current": "correct", "grounded": "grounded", "depth": "depth",
         "fit": "fit", "structure": "structure", "concise": "concise"}


def load_judgments(run):
    d = os.path.join(HERE, "judgments", run)
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                j = json.load(fh)
        except ValueError as exc:
            print("skip %s: %s" % (f, exc), file=sys.stderr)
            continue
        objs = j if isinstance(j, list) else [j]
        for o in objs:
            if isinstance(o, dict) and o.get("file"):
                out[o["file"]] = o
    for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    print("skip a line of %s" % f, file=sys.stderr)
                    continue
                if isinstance(o, dict) and o.get("file"):
                    out[o["file"]] = o
    return out


def category_of(file, by_id):
    qid = file.split(".")[0]
    q = by_id.get(qid)
    if not q:
        return "?"
    return "followup" if ".t" in file else q.get("category", "?")


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else float("nan")


def fmt(x):
    return "-" if x != x else "%.1f" % x


def summarize(run, judg, by_id):
    rows = defaultdict(list)
    for file, j in judg.items():
        rows[category_of(file, by_id)].append(j)
        rows["ALL"].append(j)
    table = {}
    for cat, js in rows.items():
        t = {k: mean([(j.get("scores") or {}).get(k) for j in js]) for k in KEYS}
        t["overall"] = mean([j.get("overall") for j in js])
        t["n"] = len(js)
        hall = [h for j in js for h in (j.get("hallucinations") or [])]
        t["hall_high"] = sum(1 for h in hall if h.get("severity") == "high")
        t["hall_med"] = sum(1 for h in hall if h.get("severity") == "medium")
        t["hall_low"] = sum(1 for h in hall if h.get("severity") == "low")
        t["errors"] = sum(1 for j in js if j.get("overall") == 1 and all(
            (j.get("scores") or {}).get(k) == 1 for k in KEYS))
        table[cat] = t
    return table


def render(run, table, order):
    lines = ["## %s" % run, "",
             "| category | n | " + " | ".join(SHORT[k] for k in KEYS) + " | overall | halluc. high/med/low | error answers |",
             "|---|---|" + "---|" * len(KEYS) + "---|---|---|"]
    for cat in order:
        t = table.get(cat)
        if not t:
            continue
        lines.append("| %s | %d | %s | **%s** | %d/%d/%d | %d |" % (
            cat, t["n"], " | ".join(fmt(t[k]) for k in KEYS), fmt(t["overall"]),
            t["hall_high"], t["hall_med"], t["hall_low"], t["errors"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.json"))
    ap.add_argument("--worst", type=int, default=5, help="list the N lowest overall answers per run")
    args = ap.parse_args()
    with open(args.questions, encoding="utf-8") as fh:
        qdoc = json.load(fh)
    by_id = {q["id"]: q for q in qdoc["questions"]}
    cats = list(qdoc.get("categories", {}).keys()) + ["followup", "ALL"]
    tables = {}
    for run in args.runs:
        judg = load_judgments(run)
        if not judg:
            print("no judgments for %s (judgments/%s/)" % (run, run), file=sys.stderr)
            continue
        table = summarize(run, judg, by_id)
        tables[run] = table
        text = render(run, table, cats)
        worst = sorted(judg.values(), key=lambda j: (j.get("overall") or 0))[:args.worst]
        text += "\n\nLowest overall:\n" + "\n".join(
            "- %s: overall %s — %s" % (j["file"], j.get("overall"), (j.get("what_went_wrong") or "")[:200])
            for j in worst)
        print(text)
        print()
        run_dir = os.path.join(HERE, "runs", run)
        if os.path.isdir(run_dir):
            with open(os.path.join(run_dir, "scores.md"), "w", encoding="utf-8") as fh:
                fh.write("# Scores — %s\n\n%s\n" % (run, text))
    if len(tables) > 1:
        print("## Side by side (ALL): overall / correct / grounded / depth / fit / structure / concise / high-severity hallucinations")
        for run, table in tables.items():
            t = table.get("ALL", {})
            print("- %s: %s / %s / %s / %s / %s / %s / %s / %d" % (
                run, fmt(t.get("overall", float("nan"))), *[fmt(t.get(k, float("nan"))) for k in KEYS],
                t.get("hall_high", 0)))


if __name__ == "__main__":
    main()
