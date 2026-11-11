#!/usr/bin/env python3
"""The per-build scorecard (6b560): a fixed, small, fast set, scored the same way every build.

  scorecard.py run   [--label L] [--where auto|local|server] [--judge mlx|packets] [--gate] [--port 9916]
  scorecard.py judge <label>                         (re)grade a recorded run with the local judge
  scorecard.py ingest <label> <judgments.jsonl>     scores from a Claude judge (for --judge packets)
  scorecard.py show  [<label>]                       the history, or one entry against the one before it

`run` starts a windowless dev copy of THIS checkout (its own MILLENAI_HOME, no cloud keys), posts the
bench persona, adds a small private notes folder under Settings > Your stuff, asks the 20 questions of
scorecard.json through POST /api/chat exactly as the page does, stops the copy and deletes its folders.
Then a judge scores each answer 1-10 against its reference sheet, and the run is appended to
scorecard/history.json with quality (mean and standard error), time to first words, total time, errors and
the facts check (the `must`/`never` regexes, no model).

Where the answers come from (--where): `auto` (default) copies the real pairing in; with a paired server
reachable the run is `server` (the modes answer there first, as in the app, and the server-only case asks
that server alone); otherwise `local` (the pairing is removed again and the server-only case is skipped).

The judge (--judge): `mlx` (default) loads a fixed local model with mlx_lm on a port of its own, after the
copy has stopped, at temperature 0, and unloads it at the end: free, offline, the same judge every build.
`packets` writes blind packets for a Claude judge from a development session instead (never the owner's
paid API keys); `ingest` adds its scores.

The gate (--gate, the release step): the run is compared with the last entry of the same question-set
version, `where` and judge, over the questions both answered. It FAILS (exit 1) when quality fell by more
than one standard error of the paired per-question differences, when the median time to first words grew
by more than 15 %, or when there are more error answers. Exit 2: the run itself could not be made.

Never part of the plain gauntlet (it loads real models and may search the web); SCORECARD=1 ./release.sh
runs it. Ports: 9916 for the copy, 9917 for the judge; never 8889, 9901-9903 or an even 88xx engine port.
"""
import argparse
import json
import math
import os
import re
import shutil
import statistics as st
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_bench as rb  # noqa: E402  (DevCopy, chat, pair_server, the forbidden ports)

SET = os.path.join(HERE, "scorecard.json")
OUT = os.path.join(HERE, "scorecard")
HISTORY = os.path.join(OUT, "history.json")
JUDGE_MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit"
TTFW_SLACK = 0.15
JUDGE_SYSTEM = "You are a strict, consistent grader of an AI assistant's answers. You answer with one JSON object only."
JUDGE_PROMPT = """Grade the ANSWER to the QUESTION against the REFERENCE SHEET, as of {as_of}.

QUESTION: {q}

ASKER'S STANDING PREFERENCES: {persona}

REFERENCE SHEET
Key facts a great answer gets right:
{facts}
Traps (wrong or stale claims):
{traps}
Shape of a great answer: {ideal}

ANSWER:
<<<
{answer}
>>>

Score each from 1 to 10. Use the whole scale and be hard to impress: 10 is flawless and rare, 8 is a
good answer with one small flaw, 6 is acceptable with clear flaws, 4 is weak, 2 is wrong or useless.
Take one point off a score for each flaw that score covers; do not round up.
- correct: the key facts right, no trap, nothing invented (a wrong central fact caps this at 3; a figure
  or claim the sheet doesn't support and you can't confirm costs 2)
- complete: every part of the question answered, with the specifics it needs
- concise: every sentence earns its place; padding, restating the question, a recap, an offer to help,
  needless headings or tables, or anything off the question each cost a point
- fit: does exactly what was asked (format, length, number of items, one pick when one is asked) and
  follows the preferences
- overall: your overall grade for this person, weighted toward correct and complete; an answer that is an
  error message, a refusal or empty scores 1

Reply with exactly one JSON object and nothing else:
{{"correct": n, "complete": n, "concise": n, "fit": n, "overall": n, "why": "one short sentence"}}"""


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_set():
    with open(SET, encoding="utf-8") as fh:
        return json.load(fh)


def load_history():
    try:
        with open(HISTORY, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"entries": []}


def save_history(h):
    os.makedirs(OUT, exist_ok=True)
    tmp = HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(h, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY)


def app_build(repo):
    src = open(os.path.join(repo, "millenai.py"), encoding="utf-8").read()
    g = lambda rx: (re.search(rx, src, re.M) or [None, ""])[1]
    return {"version": g(r'^APP_VERSION = "([^"]+)"'), "build": g(r"^APP_BUILD = (\d+)"),
            "beta": g(r"^APP_BETA = (\d+)"), "rc": g(r"^APP_RC = (\d+)")}


def facts_check(q, answer):
    a = answer or ""
    must = [bool(re.search(rx, a, re.I | re.M)) for rx in q.get("must") or []]
    never = [bool(re.search(rx, a, re.I | re.M)) for rx in q.get("never") or []]
    ok = sum(must) + sum(1 for x in never if not x)
    return {"must": must, "never_hit": never, "ok": ok, "of": len(must) + len(never)}


# ---------------------------------------------------------------- asking
def seed_notes(copy, folder, notes):
    os.makedirs(folder, mode=0o700, exist_ok=True)
    with open(os.path.join(folder, notes["name"]), "w", encoding="utf-8") as fh:
        fh.write(notes["text"])
    st_, out = copy.api("/api/know", {"add": folder, "chats": False})
    end = time.time() + 120
    while time.time() < end:
        st_, k = copy.api("/api/know")
        if ((k or {}).get("counts") or {}).get("files", 0) >= 1 and k.get("state") != "indexing":
            return k
        time.sleep(2)
    return k


def _ask_one(copy, q, tier, args):
    body = {"model": "", "models": [], "tier": tier, "auto_web": True, "images": [], "docs": [],
            "agent": "", "mode_comp": "", "messages": [{"role": "user", "content": q["q"]}]}
    r = rb.chat(copy, body, args.max_wait, args.idle)
    rec = {"id": q["id"], "category": q["category"], "tier": tier, "q": q["q"],
           "answer": r.get("answer") or "", "first_text_s": r.get("first_text_s"),
           "elapsed_s": r.get("elapsed_s"), "app_first_s": r.get("app_first_s"),
           "x_models": r.get("x_models"), "searched": r.get("searched"),
           "resets": r.get("resets"),
           "steps": ["%s: %s" % (x.get("l"), x.get("d")) for x in r.get("steps") or [] if isinstance(x, dict)][:30],
           # an empty answer is an error too (6b560: the server's empty replies scored as errors 0)
           "error": r.get("error") or ("error answer" if r.get("is_error_answer") else "")
           or ("empty answer" if not (r.get("answer") or "").strip() else ""),
           "facts": facts_check(q, r.get("answer") or "")}
    log("%s: %ss (first words %ss), %d chars, %s%s" % (
        q["id"], rec["elapsed_s"], rec["first_text_s"], len(rec["answer"]), rec["x_models"],
        (" ERROR " + rec["error"][:80]) if rec["error"] else ""))
    time.sleep(1.0)
    return rec


def ask_all(args, s, run_dir):
    home = os.path.expanduser("~/Library/Application Support/MillenAI-scorecard-%d" % args.port)
    notes_dir = home + "-notes"
    copy = rb.DevCopy(args.repo, args.python, home, args.port, os.path.join(run_dir, "devcopy.log"))
    paired, where, srv = False, "local", None
    recs = []
    try:
        if args.where in ("auto", "server"):
            why = rb.pair_server(home)
            paired = not why
            if why and args.where == "server":
                sys.exit("--where server: %s" % why)
        copy.start()
        with open(os.path.join(HERE, "persona.json"), encoding="utf-8") as fh:
            copy.api("/api/prefs", json.load(fh))
        if paired:
            _st, view = copy.api("/api/servers?refresh=1", timeout=120)
            ok = [e for e in view.get("servers") or [] if e.get("paired")
                  and (e.get("status") or {}).get("reachable")]
            if ok:
                where, srv = "server", ok[0]
            elif args.where == "server":
                sys.exit("--where server: no paired server is reachable")
            else:
                # nothing to answer there: the run is this computer's, and the pairing goes again
                copy.stop()
                rb.unpair_server(home)
                paired = False
                copy = rb.DevCopy(args.repo, args.python, home, args.port, os.path.join(run_dir, "devcopy.log"))
                copy.start()
                with open(os.path.join(HERE, "persona.json"), encoding="utf-8") as fh:
                    copy.api("/api/prefs", json.load(fh))
        log("answers from: %s%s" % (where, (" (%s)" % srv.get("name")) if srv else ""))
        k = seed_notes(copy, notes_dir, s["notes_file"])
        log("notes: %s" % json.dumps(k)[:200])
        warm = {"model": "", "models": [], "tier": args.tier, "auto_web": False, "images": [], "docs": [],
                "agent": "", "mode_comp": "",
                "messages": [{"role": "user", "content": "Explain in two sentences what a heat sink does."}]}
        w = rb.chat(copy, warm, args.max_wait, args.idle)
        log("warm-up %ss, %s" % (w["elapsed_s"], w.get("x_models")))
        for q in s["questions"]:
            if q.get("server_only") and not srv:
                recs.append({"id": q["id"], "skipped": "no paired server reachable"})
                log("%s: skipped (no server)" % q["id"])
                continue
            recs.append(_ask_one(copy, q, ("srv:%s:fast" % srv["id"]) if q.get("server_only") else args.tier, args))
        if where == "local" and args.where == "local" and not args.no_server_case:
            # THE SERVER-ONLY CASE ON A LOCAL RUN: the modes answered on this computer (a server's first
            # words moved by a third between two runs of main, too much for the gate), and the questions that ask the
            # server alone get a second copy with the real pairing, when a paired server answers
            copy.stop()
            if not rb.pair_server(home):
                paired = True
                copy = rb.DevCopy(args.repo, args.python, home, args.port, os.path.join(run_dir, "devcopy.log"))
                copy.start()
                _st, view = copy.api("/api/servers?refresh=1", timeout=120)
                ok = [e for e in view.get("servers") or [] if e.get("paired")
                      and (e.get("status") or {}).get("reachable")]
                if ok:
                    srv = ok[0]
                    for i, q in enumerate(s["questions"]):
                        if q.get("server_only"):
                            recs = [x for x in recs if x["id"] != q["id"]]
                            recs.insert(i, _ask_one(copy, q, "srv:%s:fast" % srv["id"], args))
    finally:
        copy.stop()
        if paired:
            rb.unpair_server(home)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(notes_dir, ignore_errors=True)
    return recs, where, srv


# ---------------------------------------------------------------- judging
class MlxJudge:
    def __init__(self, python, model, port, logfile):
        self.python, self.model, self.port, self.logfile = python, model, port, logfile
        self.proc = None

    def __enter__(self):
        if rb.port_open(self.port):
            sys.exit("judge port %d is taken" % self.port)
        self._log = open(self.logfile, "a")
        self.proc = subprocess.Popen([self.python, "-m", "mlx_lm", "server", "--model", self.model,
                                      "--host", "127.0.0.1", "--port", str(self.port)],
                                     stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True,
                                     env=dict(os.environ, HF_HUB_OFFLINE="1"))
        end = time.time() + 600
        while time.time() < end:
            if self.proc.poll() is not None:
                sys.exit("the judge exited with %s (see %s)" % (self.proc.returncode, self.logfile))
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % self.port, timeout=5):
                    log("judge up: %s" % self.model)
                    return self
            except Exception:
                time.sleep(2)
        sys.exit("the judge never came up")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        log("judge stopped")

    def grade(self, prompt):
        body = {"model": self.model, "temperature": 0.0, "top_p": 1.0, "max_tokens": 2000, "seed": 560,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                             {"role": "user", "content": prompt}]}
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % self.port,
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        text = out["choices"][0]["message"].get("content") or ""
        return parse_grade(text)


def parse_grade(text):
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S)
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", t, re.S))):
        try:
            o = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(o.get("overall"), (int, float)):
            for k in ("correct", "complete", "concise", "fit", "overall"):
                v = o.get(k)
                o[k] = max(1, min(10, int(round(v)))) if isinstance(v, (int, float)) else None
            return o
    return None


def judge_text(s, q, rec, persona):
    ref = q["reference"]
    return JUDGE_PROMPT.format(
        as_of=s["as_of"], q=q["q"], persona=persona,
        facts="\n".join("- " + x for x in ref.get("key_facts") or []) or "- (none)",
        traps="\n".join("- " + x for x in ref.get("traps") or []) or "- (none)",
        ideal=ref.get("ideal") or "", answer=(rec.get("answer") or "(empty)")[:8000])


def judge_all(args, s, recs, run_dir):
    persona = json.dumps(json.load(open(os.path.join(HERE, "persona.json"), encoding="utf-8")), ensure_ascii=False)
    by_id = {q["id"]: q for q in s["questions"]}
    todo = [r for r in recs if not r.get("skipped")]
    if args.judge == "packets":
        pk = os.path.join(run_dir, "packets.md")
        with open(pk, "w", encoding="utf-8") as fh:
            fh.write("Grade each packet on its own. Output one JSON line per packet: "
                     '{"id": "<id>", "correct": n, "complete": n, "concise": n, "fit": n, "overall": n, "why": "..."}\n')
            for r in todo:
                fh.write("\n\n---\n\n## Packet %s\n\n" % r["id"] + judge_text(s, by_id[r["id"]], r, persona))
        log("packets for a Claude judge: %s; then scorecard.py ingest %s <jsonl>" % (pk, args.label))
        return
    with MlxJudge(args.python, args.judge_model, args.judge_port, os.path.join(run_dir, "judge.log")) as j:
        for r in todo:
            if r.get("error") and not r.get("answer"):
                r["grade"] = {"correct": 1, "complete": 1, "concise": 1, "fit": 1, "overall": 1, "why": "error"}
                continue
            g = None
            for _try in range(2):
                try:
                    g = j.grade(judge_text(s, by_id[r["id"]], r, persona))
                except Exception as exc:
                    log("%s: judge failed (%s)" % (r["id"], exc))
                if g:
                    break
            r["grade"] = g
            log("%s: %s" % (r["id"], json.dumps(g)[:160] if g else "no grade"))


# ---------------------------------------------------------------- the numbers
def entry_numbers(e):
    graded = [r for r in e["records"] if (r.get("grade") or {}).get("overall")]
    ov = [r["grade"]["overall"] for r in graded]
    ttfw = [r["first_text_s"] for r in e["records"] if r.get("first_text_s") is not None]
    tot = [r["elapsed_s"] for r in e["records"] if r.get("elapsed_s") is not None and not r.get("skipped")]
    dims = {}
    for k in ("correct", "complete", "concise", "fit"):
        v = [r["grade"][k] for r in graded if r["grade"].get(k)]
        dims[k] = round(st.mean(v), 2) if v else None
    fc = [r["facts"] for r in e["records"] if r.get("facts")]
    e["summary"] = {
        "n": len(ov),
        "quality": round(st.mean(ov), 2) if ov else None,
        "quality_se": round(st.stdev(ov) / math.sqrt(len(ov)), 2) if len(ov) > 1 else None,
        "dims": dims,
        "ttfw_median_s": round(st.median(ttfw), 2) if ttfw else None,
        "total_median_s": round(st.median(tot), 2) if tot else None,
        "total_sum_s": round(sum(tot), 1) if tot else None,
        "errors": sum(1 for r in e["records"] if r.get("error")),
        "facts_ok": "%d/%d" % (sum(f["ok"] for f in fc), sum(f["of"] for f in fc)),
        "skipped": [r["id"] for r in e["records"] if r.get("skipped")],
        "by_category": {c: round(st.mean(v), 2) for c, v in _by_cat(graded).items()},
    }
    return e["summary"]


def _by_cat(graded):
    out = {}
    for r in graded:
        out.setdefault(r.get("category", "?"), []).append(r["grade"]["overall"])
    return out


def previous(h, e):
    for p in reversed(h["entries"]):
        if p is e:
            continue
        if (p.get("set_version") == e.get("set_version") and p.get("where") == e.get("where")
                and p.get("judge") == e.get("judge") and (p.get("summary") or {}).get("quality") is not None):
            return p
    return None


def _gated(r):
    """The gate reads the questions the build answers in its modes; the one asked of the server alone
    (tier srv:...) is recorded and shown but not gated: its state is the server's, not the build's
    (6b560: the same question was a busy error, a wrong answer and an empty one in three runs of main)."""
    return not str(r.get("tier") or "").startswith("srv:")


def compare(new, old):
    """(verdict lines, failed)"""
    a = {r["id"]: r["grade"]["overall"] for r in old["records"] if (r.get("grade") or {}).get("overall") and _gated(r)}
    b = {r["id"]: r["grade"]["overall"] for r in new["records"] if (r.get("grade") or {}).get("overall") and _gated(r)}
    common = sorted(set(a) & set(b))
    lines, failed = [], False
    if len(common) >= 2:
        d = [b[i] - a[i] for i in common]
        md, se = st.mean(d), st.stdev(d) / math.sqrt(len(d))
        bad = md < -se and md < 0
        failed |= bad
        lines.append("quality %+.2f (se %.2f) over %d questions vs %s: %s" % (
            md, se, len(d), old["label"], "FAIL (fell by more than one standard error)" if bad else "ok"))
    else:
        lines.append("quality: fewer than two questions in common with %s; not compared" % old["label"])
    # first words, over the questions the SAME model answered in both runs: a mode with a server falls
    # back to this computer for a minute when the server misses its deadline, and a median over a
    # different mix of machines measures the mix, not the build
    fa = {r["id"]: r for r in old["records"] if r.get("first_text_s") is not None and _gated(r)}
    fb = {r["id"]: r for r in new["records"] if r.get("first_text_s") is not None and _gated(r)}
    same = [i for i in sorted(set(fa) & set(fb)) if fa[i].get("x_models") == fb[i].get("x_models")]
    if len(same) >= 5:
        t0 = st.median(fa[i]["first_text_s"] for i in same)
        t1 = st.median(fb[i]["first_text_s"] for i in same)
        over = "%d questions answered by the same model" % len(same)
    else:
        t0, t1 = (old["summary"] or {}).get("ttfw_median_s"), (new["summary"] or {}).get("ttfw_median_s")
        over = "all questions (fewer than 5 answered by the same model in both)"
    if t0 and t1:
        bad = t1 > t0 * (1 + TTFW_SLACK)
        failed |= bad
        lines.append("median first words %.1f s vs %.1f s (%+.0f%%) over %s: %s" % (
            t1, t0, (t1 / t0 - 1) * 100, over, "FAIL (more than 15% slower)" if bad else "ok"))
    e0 = sum(1 for r in old["records"] if r.get("error") and _gated(r))
    e1 = sum(1 for r in new["records"] if r.get("error") and _gated(r))
    bad = e1 > e0
    failed |= bad
    lines.append("error answers %d vs %d: %s" % (e1, e0, "FAIL" if bad else "ok"))
    sv = [r for r in new["records"] if not _gated(r) and not r.get("skipped")]
    if sv:
        lines.append("the server-only case (not gated): " + "; ".join(
            "%s %s%s" % (r["id"], (r.get("grade") or {}).get("overall"), " (" + r["error"] + ")" if r.get("error") else "")
            for r in sv))
    lines.append("total time median %.1f s vs %.1f s (not gated)" % (
        new["summary"].get("total_median_s") or 0, old["summary"].get("total_median_s") or 0))
    return lines, failed


def print_entry(e):
    s = e["summary"]
    print("%s  build %s (%s)  %s  where=%s judge=%s" % (e["label"], e["app"].get("build"), e["app"].get("version"),
                                                       e["head"][:10], e["where"], e["judge"]))
    print("  quality %s (se %s) over %d · %s" % (s["quality"], s["quality_se"], s["n"],
                                                " ".join("%s %s" % (k, v) for k, v in s["dims"].items())))
    print("  first words median %ss · total median %ss (sum %ss) · errors %d · facts %s · skipped %s" % (
        s["ttfw_median_s"], s["total_median_s"], s["total_sum_s"], s["errors"], s["facts_ok"], s["skipped"] or "none"))
    print("  by category: " + ", ".join("%s %.1f" % kv for kv in sorted(s["by_category"].items())))


# ---------------------------------------------------------------- commands
def cmd_run(args):
    if args.port in rb.FORBIDDEN_PORTS or args.judge_port in rb.FORBIDDEN_PORTS or any(
            8884 <= p <= 8999 and p % 2 == 0 for p in (args.port, args.judge_port)):
        sys.exit("a forbidden port (the owner's app, the gauntlet's or an engine's)")
    s = load_set()
    app = app_build(args.repo)
    label = args.label or "build-%s-%s" % (app["build"], time.strftime("%Y%m%d-%H%M"))
    args.label = label
    run_dir = os.path.join(OUT, "runs", re.sub(r"[^\w.-]+", "-", label))
    os.makedirs(run_dir, exist_ok=True)
    lock = rb.Lock(os.path.join(args.lock_dir, "engines.lock") if not args.no_lock else "",
                   "scorecard %s pid %d" % (label, os.getpid()))
    t0 = time.time()
    try:
        lock.acquire()
        t0 = time.time()            # the run's own time, not the wait for the lock
        recs, where, srv = ask_all(args, s, run_dir)
        judge_all(args, s, recs, run_dir)
    finally:
        lock.release()
    e = {"label": label, "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "app": app, "head": args.head or " ".join(x for x in rb.git_head(args.repo) if x),
         "set_version": s["version"], "as_of": s["as_of"], "where": where,
         "server": (srv or {}).get("name"), "tier": args.tier,
         "judge": "packets" if args.judge == "packets" else "mlx:" + args.judge_model,
         "wall_s": round(time.time() - t0, 1), "records": recs}
    with open(os.path.join(run_dir, "records.json"), "w", encoding="utf-8") as fh:
        json.dump(e, fh, ensure_ascii=False, indent=1)
    if args.judge == "packets":
        return 0
    return finish(e, args.gate)


def finish(e, gate):
    entry_numbers(e)
    h = load_history()
    h["entries"] = [x for x in h["entries"] if x["label"] != e["label"]] + [e]
    save_history(h)
    print_entry(e)
    p = previous(h, e)
    if not p:
        print("  no earlier run of set v%s, where=%s, judge=%s: this is the baseline" % (
            e["set_version"], e["where"], e["judge"]))
        return 0
    lines, failed = compare(e, p)
    for ln in lines:
        print("  " + ln)
    print("SCORECARD %s" % ("FAILED" if failed else "passed"))
    return 1 if (failed and gate) else 0


def cmd_ingest(args):
    run_dir = os.path.join(OUT, "runs", re.sub(r"[^\w.-]+", "-", args.label))
    e = json.load(open(os.path.join(run_dir, "records.json"), encoding="utf-8"))
    grades = {}
    for ln in open(args.jsonl, encoding="utf-8"):
        ln = ln.strip()
        if ln.startswith("{"):
            o = json.loads(ln)
            g = parse_grade(json.dumps(o))
            if g:
                grades[o["id"]] = g
    for r in e["records"]:
        if r.get("skipped"):
            continue
        r["grade"] = grades.get(r["id"]) or r.get("grade")
    e["judge"] = args.judge_name
    with open(os.path.join(run_dir, "records.json"), "w", encoding="utf-8") as fh:
        json.dump(e, fh, ensure_ascii=False, indent=1)
    return finish(e, False)


def cmd_judge(args):
    """(Re)grade a recorded run with the local judge, without asking again."""
    run_dir = os.path.join(OUT, "runs", re.sub(r"[^\w.-]+", "-", args.label))
    e = json.load(open(os.path.join(run_dir, "records.json"), encoding="utf-8"))
    args.judge = "mlx"
    lock = rb.Lock(os.path.join(args.lock_dir, "engines.lock") if not args.no_lock else "",
                   "scorecard judge %s pid %d" % (args.label, os.getpid()))
    try:
        lock.acquire()
        judge_all(args, load_set(), e["records"], run_dir)
    finally:
        lock.release()
    e["judge"] = "mlx:" + args.judge_model
    with open(os.path.join(run_dir, "records.json"), "w", encoding="utf-8") as fh:
        json.dump(e, fh, ensure_ascii=False, indent=1)
    return finish(e, args.gate)


def cmd_show(args):
    h = load_history()
    if not args.label:
        for e in h["entries"]:
            s = e.get("summary") or {}
            print("%-28s build %-4s %-6s %-26s quality %s (se %s)  first words %ss  total %ss  errors %s" % (
                e["label"], e["app"].get("build"), e["where"], e["judge"][-26:], s.get("quality"),
                s.get("quality_se"), s.get("ttfw_median_s"), s.get("total_median_s"), s.get("errors")))
        return 0
    e = next((x for x in h["entries"] if x["label"] == args.label), None)
    if not e:
        sys.exit("no entry %r" % args.label)
    print_entry(e)
    p = previous(h, e)
    if p:
        for ln in compare(e, p)[0]:
            print("  " + ln)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label")
    r.add_argument("--where", choices=("auto", "local", "server"), default="auto")
    r.add_argument("--tier", default="Fast")
    r.add_argument("--no-server-case", action="store_true",
                   help="a local run skips the server-only question even when a server is paired")
    r.add_argument("--judge", choices=("mlx", "packets"), default="mlx")
    r.add_argument("--judge-model", default=JUDGE_MODEL)
    r.add_argument("--judge-port", type=int, default=9917)
    r.add_argument("--gate", action="store_true", help="exit 1 on a regression (the release step)")
    r.add_argument("--repo", default=rb.REPO)
    r.add_argument("--python", default=rb.DEFAULT_PY if os.path.exists(rb.DEFAULT_PY) else sys.executable)
    r.add_argument("--port", type=int, default=9916)
    r.add_argument("--max-wait", type=int, default=900)
    r.add_argument("--idle", type=int, default=300)
    r.add_argument("--lock-dir", default=os.environ.get("EVAL_LOCK_DIR", rb.DEFAULT_LOCK_DIR))
    r.add_argument("--no-lock", action="store_true")
    r.add_argument("--head", help="the commit the checkout is (for an exported tree without .git)")
    j = sub.add_parser("judge")
    j.add_argument("label")
    j.add_argument("--judge-model", default=JUDGE_MODEL)
    j.add_argument("--judge-port", type=int, default=9917)
    j.add_argument("--python", default=rb.DEFAULT_PY if os.path.exists(rb.DEFAULT_PY) else sys.executable)
    j.add_argument("--gate", action="store_true")
    j.add_argument("--lock-dir", default=os.environ.get("EVAL_LOCK_DIR", rb.DEFAULT_LOCK_DIR))
    j.add_argument("--no-lock", action="store_true")
    i = sub.add_parser("ingest")
    i.add_argument("label")
    i.add_argument("jsonl")
    i.add_argument("--judge-name", default="claude")
    w = sub.add_parser("show")
    w.add_argument("label", nargs="?")
    args = ap.parse_args()
    try:
        rc = {"run": cmd_run, "judge": cmd_judge, "ingest": cmd_ingest, "show": cmd_show}[args.cmd](args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print("scorecard: " + exc.code, file=sys.stderr)
            sys.exit(2)
        raise
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
