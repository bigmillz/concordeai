#!/usr/bin/env python3
"""Everyday answers (answers-1): does a plain, real-life ask get a good, human answer?

  everyday.py run    --label L [--config fast|g12-ultra|...] [--only f01,s02] [--no-judge] [--port 9991]
  everyday.py judge  <label>           (re)grade a recorded run with the local judge
  everyday.py show   <label> [<label>] the scores by category (and the change from the first)
  everyday.py sides  <before> <after> [ids]   side-by-side answers, as Markdown

`run` starts a windowless dev copy of THIS checkout (its own MILLENAI_HOME, no cloud keys, no
servers.json: never a real server), posts everyday.json's profile, asks every case's turns in
order in one conversation each (the copy's memory cleared before each case, a case's own prefs
such as the Short length set for it and put back after), stops the copy and deletes its folder.
Each scored turn gets the rule checks (search or not, the length band, must / never, GLOBAL_NEVER,
citations on an unsearched answer) and, after the copy has stopped, the fixed local judge of
scorecard.py (Qwen 3.6 35B A3B on MLX, temperature 0, thinking off) with this file's rubric.

Configs: fast (the Fast mode at the page's default effort, medium: this Mac's strongest model that
fits), fast-ultra, g12 (Gemma 4 12B picked by hand, medium), g12-ultra (Gemma 4 12B at Ultra, the
owner's setting in the screenshots). Ports 9991 (copy) and 9993 (judge); it waits while a gauntlet
copy (9901-9903) is up, and holds the bench's engines.lock for the whole run.
"""
import argparse
import json
import os
import re
import shutil
import statistics as st
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_bench as rb  # noqa: E402
import scorecard as sc  # noqa: E402

SET = os.path.join(HERE, "everyday.json")
FIXTURE = os.path.join(HERE, "everyday_search.json")    # the stand-in web: no real search engine, ever (6b651)
REFS = os.path.join(HERE, "everyday_refs.json")         # reference answers written by Claude Fable 5.1 (the bar)
OUT = os.path.join(HERE, "everyday")
CONFIGS = {
    "fast": {"tier": "Fast", "model": "", "effort": "medium"},
    "fast-ultra": {"tier": "Fast", "model": "", "effort": "ultra"},
    "g12": {"tier": "", "model": "Gemma 4 12B", "effort": "medium"},
    "g12-ultra": {"tier": "", "model": "Gemma 4 12B", "effort": "ultra"},
}
# never in any everyday answer: the walkthrough's boilerplate (answers-1)
GLOBAL_NEVER = [
    ("hedge", r"\bI (?:didn'?t|did not|couldn'?t|could not|wasn'?t able to) (?:find|locate|turn up)\b"),
    ("limit", r"\b(?:length|word|response|technical|output) (?:limit|cap)s?\b|\bsingle response\b"),
    ("machinery", r"\b(?:my )?training data\b|\bknowledge cut-?off\b|\bas an AI\b|\bsearch results?\b|\bsnippets?\b"),
    ("filler", r"\bgreat question\b|\bI hope (?:this|that) helps\b|\blet me know if you (?:need|have) any(?:thing| other)\b"),
    ("heading-first", r"^\s*#"),
]
JUDGE_PROMPT = """You are judging one reply from a personal AI assistant. The ideal assistant answers like a
smart, warm friend who is good at things: short when the question is small, specific and useful, plain
words, matches the person's energy. Never filler, never a news dump, never hedging boilerplate ("I didn't
find..."), never jargon or a lecture for a small personal ask, never citations for things that need none.

CONVERSATION SO FAR (earlier turns, may be empty):
{history}

THE PERSON'S MESSAGE: {q}

WHAT A GREAT REPLY LOOKS LIKE: {tone}
Expected length: about {lo}-{hi} words.
{ref}

THE REPLY:
<<<
{answer}
>>>

Score each from 1 to 10; use the whole scale and be hard to impress (10 flawless and rare, 8 good with one
small flaw, 6 acceptable with clear flaws, 4 weak, 2 wrong or useless):
- useful: answers what was actually asked, correct, concrete and doable (a wrong fact caps this at 3)
- voice: warm, direct, human, plain words, matches the person's energy; no lecture, jargon, therapy-speak,
  corporate tone or "As an AI"
- size: the right length and shape for the ask (headings/lists only when they help); every sentence earns
  its place; no padding, recap, news, hedging or needless citations
- overall: would this person be glad they asked? An error, a refusal or an empty reply scores 1.

Reply with exactly one JSON object and nothing else:
{{"useful": n, "voice": n, "size": n, "overall": n, "why": "one short sentence"}}"""


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_set():
    with open(SET, encoding="utf-8") as fh:
        return json.load(fh)


def words(t):
    t = re.sub(r"```.*?```", lambda m: " ".join(["w"] * max(1, len(m.group(0).split()) // 3)), t or "", flags=re.S)
    return len(re.findall(r"[A-Za-z0-9$£€%'’.,-]+", t))


def rules(turn, rec):
    """The rule checks of one scored turn: {name: ok}."""
    r = turn["rubric"]
    a = rec.get("answer") or ""
    out = {}
    if r.get("search") is not None:
        out["search=%s" % ("yes" if r["search"] else "no")] = bool(rec.get("searched")) == bool(r["search"])
    lo, hi = r.get("words") or [1, 10000]
    n = words(a)
    out["words %d-%d (%d)" % (lo, hi, n)] = lo <= n <= hi
    for rx in r.get("must") or []:
        out["must /%s/" % rx[:40]] = bool(re.search(rx, a, re.I | re.M))
    for rx in r.get("never") or []:
        out["never /%s/" % rx[:40]] = not re.search(rx, a, re.I | re.M)
    for name, rx in GLOBAL_NEVER:
        out["global %s" % name] = not re.search(rx, a, re.I | re.M)
    if not rec.get("searched"):
        out["no citations unsearched"] = not re.search(r"\[\d+\]", a)
    out["no error"] = not rec.get("error")
    return out


def ask_case(copy, case, cfg, args):
    """Every turn of one case, in one conversation. The records of the scored turns."""
    copy.api("/api/memory/clear", {})
    prefs = case.get("prefs") or {}
    if prefs:
        copy.api("/api/prefs", prefs)
    msgs, recs = [], []
    try:
        for k, turn in enumerate(case["turns"]):
            msgs.append({"role": "user", "content": turn["q"]})
            body = {"model": cfg["model"], "models": [cfg["model"]] if cfg["model"] else [], "tier": cfg["tier"],
                    "auto_web": True, "images": [], "docs": [], "agent": case.get("agent", ""),
                    "mode_comp": "", "effort": cfg["effort"], "messages": [dict(m) for m in msgs]}
            if case.get("agent"):
                body["codeagent"] = case["agent"]
            r = rb.chat(copy, body, args.max_wait, args.idle)
            ans = r.get("answer") or ""
            msgs.append({"role": "assistant", "content": ans})
            rec = {"id": case["id"], "turn": k + 1, "cat": case["cat"], "q": turn["q"],
                   "history": [dict(m) for m in msgs[:-2]], "answer": ans,
                   "searched": r.get("searched"), "x_models": r.get("x_models"),
                   "sources": len(r.get("sources") or []) if isinstance(r.get("sources"), list) else 0,
                   "first_text_s": r.get("first_text_s"), "elapsed_s": r.get("elapsed_s"),
                   "app_first_s": r.get("app_first_s"), "resets": r.get("resets"),
                   "drafts": [d.get("m") for d in r.get("drafts") or []],
                   "steps": ["%s: %s" % (x.get("l"), x.get("d")) for x in r.get("steps") or [] if isinstance(x, dict)][:20],
                   "error": r.get("error") or ("error answer" if r.get("is_error_answer") else "")
                   or ("empty answer" if not ans.strip() else ""),
                   "scored": bool(turn.get("rubric"))}
            if turn.get("rubric"):
                rec["rules"] = rules(turn, rec)
            log("%s.t%d: %ss, searched %s, %d words, %s%s" % (
                case["id"], k + 1, rec["elapsed_s"], rec["searched"], words(ans), rec["x_models"],
                (" ERROR " + rec["error"][:80]) if rec["error"] else ""))
            recs.append(rec)
            time.sleep(0.5)
    finally:
        if prefs:
            copy.api("/api/prefs", {k_: load_set()["profile"].get(k_, 3) for k_ in prefs})
    return recs


def wait_gauntlet():
    if not os.environ.get("EVERYDAY_WAIT_GAUNTLET"):
        return          # the engines.lock is the bench's convention; a gauntlet runs a 1B model beside it
    waited = 0
    while any(rb.port_open(p) for p in (9901, 9902, 9903)):
        if waited % 300 == 0:
            log("a gauntlet copy is up on 9901-9903: waiting")
        time.sleep(30)
        waited += 30


def cmd_run(args):
    s = load_set()
    cfg = CONFIGS[args.config]
    run_dir = os.path.join(OUT, "runs", args.label)
    os.makedirs(run_dir, exist_ok=True)
    cases = [c for c in s["cases"] if not args.only or c["id"] in args.only.split(",")]
    wait_gauntlet()
    lock = rb.Lock("" if args.no_lock else os.path.join(args.lock_dir, "engines.lock"), "everyday %s" % args.label)
    lock.acquire()
    home = os.path.expanduser("~/Library/Application Support/MillenAI-everyday-%d" % args.port)
    copy = rb.DevCopy(args.repo, args.python, home, args.port, os.path.join(run_dir, "devcopy.log"))
    # NEVER A REAL SEARCH ENGINE (6b651, per Patrick via the lead: the test traffic got this Mac's connection
    # bot-flagged): the copy must know the search-fixture hook, and gets it
    with open(os.path.join(args.repo, "millenai.py"), encoding="utf-8") as fh:
        if "# ==== 6b651 search fixture: begin ====" not in fh.read():
            sys.exit("refusing: %s/millenai.py has no search-fixture hook (it would search the real web)" % args.repo)
    rb.SEARCH.update(real_web=False, fixture=FIXTURE)     # run_bench's DevCopy adds the hook (and refuses without it)
    if os.path.exists(os.path.join(home, "servers.json")):
        sys.exit("refusing: %s holds a servers.json" % home)
    recs = []
    t0 = time.time()
    try:
        copy.start()
        copy.api("/api/prefs", s["profile"])
        warm = {"model": cfg["model"], "models": [cfg["model"]] if cfg["model"] else [], "tier": cfg["tier"],
                "auto_web": False, "images": [], "docs": [], "agent": "", "mode_comp": "", "effort": "low",
                "messages": [{"role": "user", "content": "Say hello in five words."}]}
        w = rb.chat(copy, warm, args.max_wait, args.idle)
        log("warm-up %ss, %s" % (w["elapsed_s"], w.get("x_models")))
        for c in cases:
            recs.extend(ask_case(copy, c, cfg, args))
            with open(os.path.join(run_dir, "records.json"), "w", encoding="utf-8") as fh:
                json.dump({"label": args.label, "config": args.config, "cfg": cfg, "git": rb.git_head(args.repo),
                           "recs": recs}, fh, ensure_ascii=False, indent=1)
    finally:
        copy.stop()
        shutil.rmtree(home, ignore_errors=True)
        lock.release()
    log("asked %d turns in %.0f s" % (len(recs), time.time() - t0))
    if not args.no_judge:
        judge(args, args.label)
    show([args.label])


def _load(label):
    """A recorded run, its rule checks re-read against the CURRENT everyday.json (one rubric for every run)."""
    with open(os.path.join(OUT, "runs", label, "records.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    by = {c["id"]: c for c in load_set()["cases"]}
    for r in d["recs"]:
        c = by.get(r["id"])
        turn = c["turns"][r["turn"] - 1] if c and r["turn"] <= len(c["turns"]) else None
        r["scored"] = bool(turn and turn.get("rubric"))
        if r["scored"]:
            r["rules"] = rules(turn, r)
    return d


def judge(args, label):
    d = _load(label)
    s = load_set()
    by = {c["id"]: c for c in s["cases"]}
    todo = [r for r in d["recs"] if r.get("scored")]
    try:
        with open(REFS, encoding="utf-8") as fh:
            refs = json.load(fh).get("refs") or {}
    except (OSError, ValueError):
        refs = {}
    d["judged_with_refs"] = bool(refs)
    wait_gauntlet()
    lock = rb.Lock("" if getattr(args, "no_lock", False) else os.path.join(args.lock_dir, "engines.lock"),
                   "everyday judge %s" % label)
    lock.acquire()
    try:
        with sc.MlxJudge(args.python, sc.JUDGE_MODEL, args.judge_port,
                         os.path.join(OUT, "runs", label, "judge.log")) as j:
            for r in todo:
                turn = by[r["id"]]["turns"][r["turn"] - 1]
                if r.get("error") and not (r.get("answer") or "").strip():
                    r["grade"] = {"useful": 1, "voice": 1, "size": 1, "overall": 1, "why": "error"}
                    continue
                hist = "\n".join("%s: %s" % (m["role"].upper(), m["content"][:1200]) for m in r.get("history") or [])
                lo, hi = turn["rubric"].get("words") or [1, 400]
                ref = refs.get("%s.t%d" % (r["id"], r["turn"])) or ""
                ref = ("THE BAR: Claude Fable 5.1 (the best assistant there is) answered the same message like this; "
                       "grade the reply against this standard (it need not match the words, and the bar's own facts "
                       "may be dated):\n<<<\n%s\n>>>" % ref[:3000]) if ref else ""
                prompt = JUDGE_PROMPT.format(history=hist or "(none)", q=r["q"], tone=turn["rubric"].get("tone", ""),
                                             lo=lo, hi=hi, ref=ref, answer=(r.get("answer") or "(empty)")[:6000])
                g = None
                for _ in range(2):
                    try:
                        g = j.grade(prompt)
                    except Exception as exc:
                        log("%s: judge failed (%s)" % (r["id"], exc))
                    if g:
                        break
                r["grade"] = g
                log("%s.t%d: %s" % (r["id"], r["turn"], json.dumps(g)[:150] if g else "no grade"))
    finally:
        lock.release()
    with open(os.path.join(OUT, "runs", label, "records.json"), "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)


# the judge's parse: scorecard's wants "overall" plus its own keys; ours are useful/voice/size
_orig_parse = sc.parse_grade


def _parse(text):
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S)
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", t, re.S))):
        try:
            o = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(o.get("overall"), (int, float)):
            for k in ("useful", "voice", "size", "overall"):
                v = o.get(k)
                o[k] = max(1, min(10, int(round(v)))) if isinstance(v, (int, float)) else None
            return o
    return None


sc.parse_grade = _parse


def numbers(recs):
    sc_ = [r for r in recs if r.get("scored")]
    rows = {}
    for r in sc_:
        for cat in (r["cat"], "ALL"):
            x = rows.setdefault(cat, {"n": 0, "rules_ok": 0, "rules_n": 0, "clean": 0, "overall": [], "useful": [],
                                      "voice": [], "size": [], "words": [], "secs": []})
            x["n"] += 1
            ok = sum(1 for v in (r.get("rules") or {}).values() if v)
            x["rules_ok"] += ok
            x["rules_n"] += len(r.get("rules") or {})
            x["clean"] += int(ok == len(r.get("rules") or {}))
            g = r.get("grade") or {}
            for k in ("overall", "useful", "voice", "size"):
                if isinstance(g.get(k), (int, float)):
                    x[k].append(g[k])
            x["words"].append(words(r.get("answer")))
            if r.get("elapsed_s") is not None:
                x["secs"].append(r["elapsed_s"])
    return rows


def _m(v):
    return round(st.mean(v), 2) if v else None


def show(labels):
    cats = ["feelings", "smalltalk", "practical", "known", "fresh", "arith", "followup", "chat", "length", "code",
            "decision", "ALL"]
    tabs = [numbers(_load(l)["recs"]) for l in labels]
    print("\n| category | n | " + " | ".join("%s overall / voice / size / rules clean / median words / median s" % l
                                           for l in labels) + " |")
    print("|---|---|" + "---|" * len(labels))
    for c in cats:
        if not any(c in t for t in tabs):
            continue
        cells = []
        for t in tabs:
            x = t.get(c)
            if not x:
                cells.append("")
                continue
            cells.append("%s / %s / %s / %d of %d / %s / %s" % (
                _m(x["overall"]), _m(x["voice"]), _m(x["size"]), x["clean"], x["n"],
                int(st.median(x["words"])) if x["words"] else "", round(st.median(x["secs"]), 1) if x["secs"] else ""))
        print("| %s | %d | %s |" % (c, next(t[c]["n"] for t in tabs if c in t), " | ".join(cells)))
    for l in labels:
        recs = _load(l)["recs"]
        fails = [(r["id"], r["turn"], [k for k, v in (r.get("rules") or {}).items() if not v])
                 for r in recs if r.get("scored") and not all((r.get("rules") or {}).values())]
        print("\n%s rule failures (%d):" % (l, len(fails)))
        for i, t, f in fails:
            print("  %s.t%d: %s" % (i, t, "; ".join(f)))


def sides(a, b, ids):
    A = {(r["id"], r["turn"]): r for r in _load(a)["recs"] if r.get("scored")}
    B = {(r["id"], r["turn"]): r for r in _load(b)["recs"] if r.get("scored")}
    for key in sorted(A):
        if ids and key[0] not in ids:
            continue
        ra, rb_ = A[key], B.get(key) or {}
        print("\n### %s: %s\n" % (key[0], ra["q"]))
        for lab, r in ((a, ra), (b, rb_)):
            g = r.get("grade") or {}
            print("**%s** (overall %s, %s words, searched %s, %ss)\n" % (lab, g.get("overall"), words(r.get("answer")),
                                                                          r.get("searched"), r.get("elapsed_s")))
            print("> " + (r.get("answer") or "(none)").strip().replace("\n", "\n> ") + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--config", default="fast", choices=sorted(CONFIGS))
    r.add_argument("--only", default="")
    r.add_argument("--no-judge", action="store_true")
    for p in (r, sub.add_parser("judge")):
        p.add_argument("--python", default=rb.DEFAULT_PY if os.path.exists(rb.DEFAULT_PY) else sys.executable)
        p.add_argument("--judge-port", type=int, default=9993)
        p.add_argument("--lock-dir", default=os.environ.get("EVAL_LOCK_DIR", rb.DEFAULT_LOCK_DIR))
        p.add_argument("--no-lock", action="store_true", help="don't wait for the bench's engines.lock")
        if p is not r:
            p.add_argument("label")
    r.add_argument("--repo", default=rb.REPO)
    r.add_argument("--port", type=int, default=9991)
    r.add_argument("--max-wait", type=int, default=420)
    r.add_argument("--idle", type=int, default=240)
    sh = sub.add_parser("show")
    sh.add_argument("labels", nargs="+")
    sd = sub.add_parser("sides")
    sd.add_argument("a")
    sd.add_argument("b")
    sd.add_argument("ids", nargs="*")
    args = ap.parse_args()
    if args.cmd == "run":
        if args.port in rb.FORBIDDEN_PORTS:
            sys.exit("port %d is not for this" % args.port)
        cmd_run(args)
    elif args.cmd == "judge":
        judge(args, args.label)
        show([args.label])
    elif args.cmd == "show":
        show(args.labels)
    else:
        sides(args.a, args.b, set(args.ids))


if __name__ == "__main__":
    main()
