#!/usr/bin/env python3
"""(6b560) Clean runs recorded before run_bench.py knew the USAGE, DONE and RUBIN frames.

  python3 tools/answer_eval/refix_frames.py <run-name> [...]

Those frames were read as answer text: the recorded answers carry them, and first_text_s fired on the first
USAGE frame (about 1 s, before any word). This strips every frame the page strips from `answer` and
`answer_raw`, and takes the app's own clock from the DONE frame (app_first_s: its first visible word,
app_total_s: the whole run), which is what the timing comparisons read. first_text_s is left as recorded
and marked unreliable. Idempotent.
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_bench as rb  # noqa: E402

DONE_RX = re.compile("\0DONE:(.*?)\0", re.S)

for run in sys.argv[1:]:
    n = 0
    for f in sorted(glob.glob(os.path.join(HERE, "runs", run, "q*.json"))):
        rec = json.load(open(f, encoding="utf-8"))
        raw = rec.get("answer_raw") or ""
        changed = False
        m = DONE_RX.search(raw)
        if m and rec.get("app_first_s") is None:
            try:
                d = json.loads(m.group(1))
                rec["app_first_s"], rec["app_total_s"] = d.get("f"), d.get("t")
                changed = True
            except ValueError:
                pass
        if "\0" in raw or "\0" in (rec.get("answer") or ""):
            clean = rb.stream_text(raw).strip()
            rec["answer_raw"] = clean
            rec["answer"] = rb.page_text(clean)
            rec["chars"] = len(clean)
            rec["first_text_s_unreliable"] = True
            changed = True
        if changed:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)
            n += 1
    if n:
        rb.write_answers_md(os.path.join(HERE, "runs", run),
                            rb.load_questions(os.path.join(HERE, "questions.json"), "", 0))
    print(run, "fixed", n)
