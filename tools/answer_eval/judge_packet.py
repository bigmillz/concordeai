#!/usr/bin/env python3
"""Assemble one judging packet per recorded answer of a run.

  python3 tools/answer_eval/judge_packet.py <run-name> [--batch 8] [--only q01,q07]

Reads runs/<run-name>/*.json (written by run_bench.py) and questions.json, and
writes packets/<run-name>/<file>.md: the question with its earlier turns, the
asker's standing preferences, the reference sheet (key facts, traps, sources),
the answer, and the run metadata a judge needs to say WHERE it went wrong
(models, drafts, sources, timings). batches/<run-name>/batch-N.md bundles
packets for one judging session, with the judge prompt from RUBRIC.md on top.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_questions(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    by_id = {q["id"]: q for q in d["questions"]}
    return d, by_id


def judge_prompt(as_of):
    with open(os.path.join(HERE, "RUBRIC.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"## Judge prompt.*?```\n(.*?)```", text, re.S)
    prompt = m.group(1) if m else ""
    return prompt.replace("{as_of}", as_of)


def fmt_list(items, bullet="- "):
    return "\n".join(bullet + str(x) for x in items) if items else "- (none)"


def packet(rec, q, persona, as_of):
    turn = rec.get("turn", 1)
    turns = q["turns"]
    ref = turns[turn - 1].get("reference") or {}
    out = ["# Packet %s — %s (%s)" % (rec["file"], q.get("topic", ""), q.get("category", "")),
           "", "Run: `%s` · config: `%s` · tier: `%s` · bench date: %s" % (
               rec.get("run"), rec.get("config"), rec.get("tier"), as_of), ""]
    if turn > 1:
        out.append("## Earlier turns (context the model had)")
        for k in range(turn - 1):
            out.append("**User (turn %d):** %s" % (k + 1, turns[k]["q"]))
            prev = rec.get("request", {}).get("messages", [])
            # the assistant's earlier answer rides in the request the runner sent
            ans = [m for m in prev if m.get("role") == "assistant"]
            if k < len(ans):
                out.append("\n**Assistant (turn %d):**\n\n%s\n" % (k + 1, ans[k]["content"][:4000]))
        out.append("")
    out.append("## Question (turn %d)" % turn)
    out.append(rec.get("question", turns[turn - 1]["q"]))
    out.append("")
    if persona:
        out.append("## The asker's standing preferences (set in the app)")
        out.append("Name: %s · home area: %s · length dial: %s/5" % (
            persona.get("user_name"), persona.get("home_area"), persona.get("length")))
        out.append("> " + str(persona.get("persona", "")))
        out.append("")
    out.append("## Reference sheet (as of %s)" % as_of)
    out.append("**Key facts a great answer gets right:**")
    out.append(fmt_list(ref.get("key_facts")))
    out.append("")
    out.append("**Traps (wrong or stale as of the bench date):**")
    out.append(fmt_list(ref.get("traps")))
    out.append("")
    if ref.get("ideal"):
        out.append("**Shape of a great answer:** " + ref["ideal"])
        out.append("")
    out.append("**Sources checked:**")
    out.append(fmt_list(ref.get("sources")))
    out.append("")
    out.append("## The answer the person saw")
    out.append("")
    out.append(rec.get("answer") or "(empty)")
    out.append("")
    out.append("## Run metadata (for `what_went_wrong`)")
    out.append("- elapsed %ss; first byte %ss; first visible text %ss; %s chars; searched=%s; resets=%s%s" % (
        rec.get("elapsed_s"), rec.get("first_byte_s"), rec.get("first_text_s"), rec.get("chars"),
        rec.get("searched"), rec.get("resets"), ("; ERROR " + rec["error"]) if rec.get("error") else ""))
    out.append("- X-Models (the council): %s" % rec.get("x_models"))
    srcs = rec.get("sources") or []
    out.append("- search sources shown (%d): %s" % (len(srcs), "; ".join(
        "%s <%s>" % (s.get("t"), s.get("u")) for s in srcs) or "(none)"))
    out.append("- status lines: %s" % " | ".join(dict.fromkeys(s[1] for s in rec.get("statuses", []))))
    steps = rec.get("steps") or []
    out.append("- steps: %s" % " | ".join("%s:%s(%s)" % (s.get("id"), s.get("s"), s.get("d")) for s in steps))
    drafts = rec.get("drafts") or []
    out.append("- drafts (%d):" % len(drafts))
    for d in drafts:
        t = (d.get("text") or "").replace("\n", " ")
        out.append("  - [%ss] %s — %d chars: %s%s" % (d.get("t_s"), d.get("m"), len(d.get("text") or ""),
                                                      t[:400], "…" if len(t) > 400 else ""))
    out.append("")
    out.append("## Output")
    out.append("One JSON line as RUBRIC.md's judge prompt specifies, with \"file\": \"%s\" and \"run\": \"%s\"."
               % (rec["file"], rec.get("run")))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.json"))
    ap.add_argument("--persona", default=os.path.join(HERE, "persona.json"))
    ap.add_argument("--batch", type=int, default=8, help="packets per judging batch")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--label", default="batch", help="batch file prefix (batches/<run>/<label>-N.md)")
    args = ap.parse_args()
    qdoc, by_id = load_questions(args.questions)
    as_of = qdoc.get("as_of", "")
    persona = None
    if args.persona and os.path.exists(args.persona):
        with open(args.persona, encoding="utf-8") as fh:
            persona = json.load(fh)
    run_dir = os.path.join(HERE, "runs", args.run)
    files = sorted(f for f in glob.glob(os.path.join(run_dir, "q*.json")))
    if args.only:
        want = set(x.strip() for x in args.only.split(","))
        files = [f for f in files if os.path.basename(f).split(".")[0] in want]
    if not files:
        sys.exit("no records in %s" % run_dir)
    pdir = os.path.join(HERE, "packets", args.run)
    bdir = os.path.join(HERE, "batches", args.run)
    os.makedirs(pdir, exist_ok=True)
    os.makedirs(bdir, exist_ok=True)
    packets = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["file"] = os.path.basename(f)
        q = by_id.get(rec.get("id"))
        if not q:
            continue
        text = packet(rec, q, persona, as_of)
        with open(os.path.join(pdir, rec["file"].replace(".json", ".md")), "w", encoding="utf-8") as fh:
            fh.write(text)
        packets.append((rec["file"], text))
    prompt = judge_prompt(as_of)
    n = 0
    for i in range(0, len(packets), args.batch):
        n += 1
        chunk = packets[i:i + args.batch]
        with open(os.path.join(bdir, "%s-%d.md" % (args.label, n)), "w", encoding="utf-8") as fh:
            fh.write("# Judging batch %s-%d of run %s\n\n" % (args.label, n, args.run))
            fh.write("Judge prompt (from RUBRIC.md):\n\n```\n%s```\n\n" % prompt)
            fh.write("Files in this batch: %s\n\n---\n\n" % ", ".join(f for f, _t in chunk))
            fh.write("\n\n---\n\n".join(t for _f, t in chunk))
    print("wrote %d packets to %s and %d batches to %s" % (len(packets), pdir, n, bdir))


if __name__ == "__main__":
    main()
