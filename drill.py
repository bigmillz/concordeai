#!/usr/bin/env python3
"""ConcordeAI answer-quality drill — the COLLECTOR half (6b260).

Fires realistic questions at a live instance across all three lanes
and records everything verbatim. It deliberately does NOT judge: the
judge is Claude, reading each run's transcript, scoring it against the
rubric in drill_rubric.md, editing the prompts in millenai.py, and
launching the next batch. This split keeps the graded thing honest —
the same code never both answers and marks.

  usage: python3 drill.py [--batch N] [--seed S] [--modes chat,web,funnel]
  needs: a live server on 127.0.0.1:9894 (see CLAUDE.md)
  output: ~/Library/Application Support/MillenAI/drill_runs/<stamp>/
          transcript.jsonl  (one record per drill)

Transcripts live OUTSIDE the Drive folder on purpose: a days-long grind
writes a lot of them, Drive would sync every one, and a file written
into a freshly-created Drive directory came back 0 bytes in testing.

Each record: {mode, question, answer, ms, sources_n, funnel_stages?,
              funnel_summary?, meta}. Full multi-stage funnels are
walked with a mix of CLICKED options and TYPED answers — typing is the
path that used to dead-end, so it stays under permanent test.
"""
import argparse
import json
import os
import random
import re
import time
import urllib.request

BASE = "http://127.0.0.1:9894"
K = "millen_key=smoketestkey123"
NUL = "\x00"

# ---------------------------------------------------------------- banks
# Real questions in each register the app actually gets. Grouped by the
# failure mode they probe, so a judge can see at a glance WHAT a bad
# answer regressed. Extend freely; keep them answerable-in-principle.
CHAT_PLAIN = [
    "whats a good first programming language for a 12 year old",
    "my sourdough starter smells like acetone, is it dead",
    "explain how a heat pump works like im smart but not an engineer",
    "best way to learn to whistle loudly with fingers",
    "how much protein do i actually need a day, im 180lbs and lift 3x week",
    "why does my wifi get slow at night",
    "whats the difference between a trust and a will, plain english",
    "is it worth replacing my 2015 macbook battery or just buy new",
    "give me a 20 minute bodyweight workout for a hotel room",
    "how do i get red wine out of a white couch cushion",
]
CHAT_WEB = [
    "is there a supermarket open now that might sell msg, in bushwick ny",
    "whats the weather in 11221 this weekend",
    "any good coffee shops open right now near williamsburg",
    "whens the next full moon",
    "is the L train running normally today",
    "pharmacy open late tonight in bushwick",
    "what movies are playing this week worth seeing",
    "current price of a dozen eggs roughly",
    "best pizza slice near myrtle-broadway, open now",
    "when does daylight savings end this year",
]
FUNNELS = [
    {"goal": "Should I get a pet?", "reqs": "studio apartment, active",
     "typed": {2: "maybe 2 hours a day"}},
    {"goal": "What should I cook for a date on Friday?",
     "reqs": "one of us is vegetarian, small kitchen", "typed": {1: "italian-ish"}},
    {"goal": "Pick a weekend trip from NYC", "reqs": "under $400 total, no car",
     "typed": {3: "somewhere quiet, not a party town"}},
    {"goal": "Which candy should I try next?", "reqs": "",
     "typed": {1: "strawberry", 2: "frozen", 3: "with sprinkles"}},
    {"goal": "What laptop should I buy?", "reqs": "video editing, under $1500",
     "typed": {}},
    {"goal": "How should I start investing $5k?", "reqs": "im 28, can leave it 10 years",
     "typed": {2: "hands off, boring is fine"}},
]

def req(path, data=None, timeout=300):
    h = {"Cookie": K}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read()

def strip_frames(raw):
    txt = raw.decode("utf-8", "replace")
    cut = txt.rfind(NUL + "RESET" + NUL)
    if cut >= 0:
        txt = txt[cut + 7:]
    srcs = len(re.findall(NUL + r"SOURCES:", txt))
    txt = re.sub(NUL + r"[A-Z0-9]+:.*?" + NUL, "", txt, flags=re.S)
    txt = txt.replace(NUL + "RESET" + NUL, "").replace(NUL, "")
    return txt.strip(), srcs

def drill_chat(q, web):
    t0 = time.time()
    raw = req("/api/chat", {"model": "", "models": [], "tier": "Fast",
                            "auto_web": web, "images": [], "docs": [],
                            "agent": "", "messages":
                            [{"role": "user", "content": q}]})
    ans, srcs = strip_frames(raw)
    return {"mode": "web" if web else "chat", "question": q,
            "answer": ans, "ms": int((time.time() - t0) * 1000),
            "sources_n": srcs}

def drill_funnel(spec, rng):
    t0 = time.time()
    picks, stages = [], []
    asked = []
    state = {"goal": spec["goal"], "reqs": spec["reqs"], "opts": 4,
             "stages": 4, "images": False, "picks": picks,
             "asked": asked}
    for hop in range(1, 10):
        d = json.loads(req("/api/funnel", state))
        if d.get("err"):
            return {"mode": "funnel", "question": spec["goal"],
                    "error": d["err"], "ms": int((time.time()-t0)*1000)}
        if d.get("done"):
            return {"mode": "funnel", "question": spec["goal"],
                    "funnel_stages": stages, "picks": list(picks),
                    "funnel_summary": d.get("summary", ""),
                    "ms": int((time.time() - t0) * 1000)}
        opts = [o.get("label", "") for o in d.get("options", [])]
        stages.append({"q": d.get("q", ""), "options": opts})
        asked.append(d.get("q", ""))     # mirror the client (6b260)
        typed = spec.get("typed", {}).get(d.get("stage"))
        picks.append(typed if typed else
                     (rng.choice(opts) if opts else "(no options)"))
    return {"mode": "funnel", "question": spec["goal"],
            "error": "never finished in 9 hops",
            "funnel_stages": stages, "ms": int((time.time()-t0)*1000)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=8,
                    help="questions per lane (funnels capped at bank size)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--modes", default="chat,web,funnel")
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else int(time.time()) % 100000
    rng = random.Random(seed)
    modes = set(a.modes.split(","))

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = os.environ.get("DRILL_HOME") or os.path.expanduser(
        "~/Library/Application Support/MillenAI/drill_runs")
    outdir = os.path.join(root, stamp)
    os.makedirs(outdir, exist_ok=True)
    out = open(os.path.join(outdir, "transcript.jsonl"), "w",
               encoding="utf-8")
    def emit(rec):
        rec["seed"] = seed
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())
        tag = rec.get("mode", "?")
        err = " ERR:" + rec["error"] if rec.get("error") else ""
        print(f"  [{tag:6}] {rec['question'][:52]:52} "
              f"{rec.get('ms',0)/1000:6.1f}s{err}", flush=True)

    print(f"drill batch={a.batch} seed={seed} -> {outdir}", flush=True)
    if "chat" in modes:
        for q in rng.sample(CHAT_PLAIN, min(a.batch, len(CHAT_PLAIN))):
            try: emit(drill_chat(q, web=False))
            except Exception as e: emit({"mode":"chat","question":q,"error":str(e)[:200]})
    if "web" in modes:
        for q in rng.sample(CHAT_WEB, min(a.batch, len(CHAT_WEB))):
            try: emit(drill_chat(q, web=True))
            except Exception as e: emit({"mode":"web","question":q,"error":str(e)[:200]})
    if "funnel" in modes:
        for spec in rng.sample(FUNNELS, min(a.batch, len(FUNNELS))):
            try: emit(drill_funnel(spec, rng))
            except Exception as e: emit({"mode":"funnel","question":spec["goal"],"error":str(e)[:200]})
    out.close()
    path = os.path.join(outdir, "transcript.jsonl")
    print("done -> %s (%d bytes)" % (path, os.path.getsize(path)),
          flush=True)

if __name__ == "__main__":
    main()
