#!/usr/bin/env python3
"""Answer eval for "It knows your life": the same questions with and without
the personal index, through a running dev copy's /api/chat, exactly as the
page sends them (tier Fast, no web search).

Usage:
  run_eval.py --home <the dev copy's MILLENAI_HOME> --port 9886 \
      --questions <corpus>/questions.json --out runs/<name> [--cond with|without|both]

The copy must already hold the index for "with" (make_corpus.py, the folder
added under Settings > Your stuff, chats.v2.json seeded). For "without" the
runner turns the sources off (POST /api/know/forget {"all": true}) and turns
them back on afterwards (chats on, the folder re-added), so run "with" first.

Writes <out>/<cond>/<qid>.json per answer, <out>/summary.json, and
<out>/packets/<n>.md: blind judging packets (the two answers as A and B in
a random order, with the reference), plus <out>/packets/key.json.
Stdlib only. Nothing here is anyone's real data.
"""
import argparse
import http.client
import json
import os
import random
import re
import socket
import time
import urllib.parse

FRAME_TAGS = "RUN|STATUS|DRAFT|SOURCES|PHOTOS|MAP|CTX|PLACEHINT|PLACES2|STEP|APPROVE|RATE"
FRAME_RX = re.compile("\0(" + FRAME_TAGS + "):(.*?)\0", re.S)
FRAME_PART = re.compile("\0(?:" + FRAME_TAGS + "):[^\0]*$")


def stream_text(raw):
    t = FRAME_PART.sub("", FRAME_RX.sub("", raw))
    cut = t.rfind("\0RESET\0")
    return t[cut + 7:] if cut >= 0 else t


class Copy:
    def __init__(self, home, port):
        with open(os.path.join(home, "run", "instance.json")) as fh:
            d = json.load(fh)
        self.port, self.key, self.token = port, d["key"], d["token"]

    def headers(self):
        return {"Cookie": "millen_key_%d=%s" % (self.port, self.key), "X-Api-Token": self.token,
                "Content-Type": "application/json"}

    def json(self, method, path, body=None, timeout=120):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, self.headers())
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, json.loads(raw or b"{}")

    def chat(self, body, max_wait=600, idle=240):
        t0 = time.time()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=idle)
        conn.request("POST", "/api/chat", json.dumps(body).encode(), self.headers())
        resp = conn.getresponse()
        rec = {"status_code": resp.status, "first_byte_s": None, "elapsed_s": None, "answer": "",
               "statuses": [], "steps": [], "sources": [], "error": "", "x_models": urllib.parse.unquote(
                   resp.getheader("X-Models", "") or "")}
        if resp.status != 200:
            rec["error"] = resp.read().decode("utf-8", "replace")[:500]
            rec["elapsed_s"] = round(time.time() - t0, 2)
            conn.close()
            return rec
        raw, buf, seen = "", b"", 0
        while True:
            if time.time() - t0 > max_wait:
                rec["error"] = "gave up after %ds" % max_wait
                break
            try:
                chunk = resp.read1(65536)
            except (socket.timeout, TimeoutError):
                rec["error"] = "no bytes for %ds" % idle
                break
            if not chunk:
                break
            now = round(time.time() - t0, 2)
            if rec["first_byte_s"] is None:
                rec["first_byte_s"] = now
            buf += chunk
            try:
                raw += buf.decode("utf-8")
                buf = b""
            except UnicodeDecodeError:
                continue
            frames = list(FRAME_RX.finditer(raw))
            for m in frames[seen:]:
                tag, js = m.group(1), m.group(2)
                if tag == "STATUS":
                    rec["statuses"].append([now, js])
                elif tag == "STEP":
                    try:
                        rec["steps"].append(dict(json.loads(js), t_s=now))
                    except ValueError:
                        pass
                elif tag == "SOURCES":
                    try:
                        rec["sources"] = json.loads(js)
                    except ValueError:
                        pass
            seen = len(frames)
        conn.close()
        rec["answer"] = stream_text(raw).strip()
        rec["elapsed_s"] = round(time.time() - t0, 2)
        rec["first_text_s"] = None
        return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--port", type=int, default=9886)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cond", default="both", choices=("with", "without", "both"))
    ap.add_argument("--tier", default="Fast")
    ap.add_argument("--folder", default="", help="the corpus notes folder, re-added after 'without'")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    copy = Copy(a.home, a.port)
    qs = json.load(open(a.questions))["questions"]
    if a.limit:
        qs = qs[:a.limit]
    os.makedirs(a.out, exist_ok=True)
    conds = ["with", "without"] if a.cond == "both" else [a.cond]
    summary = {"tier": a.tier, "conds": {}, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for cond in conds:
        if cond == "without":
            st, d = copy.json("POST", "/api/know/forget", {"all": True})
            print("sources off:", d.get("line"))
        d_out = os.path.join(a.out, cond)
        os.makedirs(d_out, exist_ok=True)
        rows = []
        for q in qs:
            path = os.path.join(d_out, q["id"] + ".json")
            if os.path.exists(path):
                rec = json.load(open(path))
                if rec.get("answer") and not rec.get("error"):
                    rows.append(rec)
                    print(cond, q["id"], "kept")
                    continue
            body = {"model": "", "models": [], "tier": a.tier, "messages": [{"role": "user", "content": q["q"]}],
                    "auto_web": False, "images": [], "docs": [], "agent": "", "mode_comp": ""}
            for _attempt in range(20):
                rec = copy.chat(body)
                if rec["status_code"] == 409:
                    print("busy; waiting 30 s")
                    time.sleep(30)
                    continue
                break
            rec.update({"id": q["id"], "cond": cond, "question": q["q"], "ref": q["ref"], "expect": q["expect"]})
            rec["used_know"] = any(s.get("id") == "know" and "match" in (s.get("d") or "")
                                   and "nothing" not in (s.get("d") or "") for s in rec["steps"])
            rec["know_sources"] = [s for s in (rec["sources"] or []) if str(s.get("u", "")).startswith("know:")]
            rec["cited"] = bool(re.search(r"\[\d+\]", rec["answer"] or ""))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)
            rows.append(rec)
            print("%s %s: %ss, %d chars, know=%s, cites=%s, models=%s%s" % (
                cond, q["id"], rec["elapsed_s"], len(rec["answer"]), rec["used_know"], rec["cited"],
                rec["x_models"][:40], (" ERROR " + rec["error"][:60]) if rec["error"] else ""))
        el = sorted(r["elapsed_s"] for r in rows if r.get("elapsed_s"))
        summary["conds"][cond] = {"n": len(rows), "median_s": el[len(el) // 2] if el else None,
                                  "mean_s": round(sum(el) / len(el), 1) if el else None,
                                  "used_know": sum(1 for r in rows if r.get("used_know")),
                                  "cited": sum(1 for r in rows if r.get("cited")),
                                  "errors": sum(1 for r in rows if r.get("error"))}
        if cond == "without":
            st, d = copy.json("POST", "/api/know", {"chats": True, "add": a.folder} if a.folder else {"chats": True})
            print("sources back on:", d.get("line"))
    # the blind packets
    if a.cond == "both":
        pk = os.path.join(a.out, "packets")
        os.makedirs(pk, exist_ok=True)
        random.seed(530)
        key = {}
        for n, q in enumerate(qs, 1):
            w = json.load(open(os.path.join(a.out, "with", q["id"] + ".json")))
            wo = json.load(open(os.path.join(a.out, "without", q["id"] + ".json")))
            pair = [("with", w), ("without", wo)]
            random.shuffle(pair)
            key[str(n)] = {"A": pair[0][0], "B": pair[1][0], "id": q["id"]}
            with open(os.path.join(pk, "%02d.md" % n), "w", encoding="utf-8") as fh:
                fh.write("# Packet %02d\n\nThe asker is a fictional person; everything below is invented test data.\n\n"
                         "## Question\n%s\n\n## Reference answer (what the asker's own notes say)\n%s\n\n"
                         "## Answer A\n%s\n\n## Answer B\n%s\n" % (
                             n, q["q"], q["ref"], pair[0][1]["answer"] or "(empty)", pair[1][1]["answer"] or "(empty)"))
        with open(os.path.join(pk, "key.json"), "w") as fh:
            json.dump(key, fh, indent=1)
    summary["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(os.path.join(a.out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
