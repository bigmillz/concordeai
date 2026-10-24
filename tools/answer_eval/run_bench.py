#!/usr/bin/env python3
"""Answer bench: drive a dev copy of ConcordeAI over HTTP and record every answer.

For each question in questions.json this sends the same POST /api/chat the page
sends (model "", models [], tier, messages, auto_web, images, docs, agent), reads
the streamed body with its control frames, and writes one JSON record per turn:
the answer text, the drafts and their models, the sources, the STATUS / STEP /
RUN frames with timestamps, the response headers (X-Models, X-Web-Search,
X-Eta) and the timings.

Configs (``--config``):
  local-fast    this Mac's models, tier Fast
  local-think   this Mac's models, tier Thinking
  server-fast   the owner's paired server alone, tier "srv:<id>:fast"
  server-think  the owner's paired server alone, tier "srv:<id>:think"

The copy is a dev copy (MILLENAI_DEV=1, its own MILLENAI_HOME, no window, port
9894 by default), started from the checkout this file lives in, so the bench
runs against whatever branch is checked out. A server config copies ONLY
servers.json from the real data folder into the copy's own folder (0600; its
secrets are inline, nothing else is referenced), never prints it, and deletes
it, and the whole folder, when the run ends (``--keep-home`` keeps the folder
minus servers.json).

Shared hardware: a local config holds the engines.lock mkdir lock for the whole
run; a server config holds server-gpu.lock (with an ``owner`` file), both under
``--lock-dir`` (default: $EVAL_LOCK_DIR or this session's scratchpad). Never run
on 8889 (the owner's app) or 9901-9903 (the gauntlet).

Usage:
  python3 tools/answer_eval/run_bench.py --config server-think --name baseline-server-think
  python3 tools/answer_eval/run_bench.py --config local-think --name baseline-local-think
  ... --only q01,q07   --limit 5   --redo   --attach (use a copy already on --port)
"""
import argparse
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
REAL_APP_DIR = os.path.expanduser("~/Library/Application Support/MillenAI")
DEFAULT_PY = os.path.join(REAL_APP_DIR, "venv", "bin", "python3")
DEFAULT_LOCK_DIR = ("/private/tmp/claude-501/-Users-patrickmiller-My-Drive-Projects-Concorde-ConcordeAI/"
                    "b2cba696-73a2-4ea8-b3e7-2d49db11356a/scratchpad")
FORBIDDEN_PORTS = {8889, 9901, 9902, 9903}

CONFIGS = {
    "local-fast": {"where": "local", "tier": "Fast", "lock": "engines.lock"},
    "local-think": {"where": "local", "tier": "Thinking", "lock": "engines.lock"},
    "server-fast": {"where": "server", "tier": "srv:{sid}:fast", "lock": "server-gpu.lock"},
    "server-think": {"where": "server", "tier": "srv:{sid}:think", "lock": "server-gpu.lock"},
}

# the wire protocol, as the page reads it (FRAME_TAGS in millenai.py)
FRAME_TAGS = "RUN|STATUS|DRAFT|SOURCES|PHOTOS|MAP|CTX|PLACEHINT|PLACES2|STEP|APPROVE|RATE"  # RATE: 6b515
FRAME_RX = re.compile("\0(" + FRAME_TAGS + "):(.*?)\0", re.S)
FRAME_PART = re.compile("\0(?:" + FRAME_TAGS + "):[^\0]*$")
RESET = "\0RESET\0"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def stream_text(raw):
    """The visible answer: frames stripped, only what follows the last RESET."""
    t = FRAME_RX.sub("", raw)
    t = FRAME_PART.sub("", t)
    cut = t.rfind(RESET)
    return t[cut + len(RESET):] if cut >= 0 else t


PLACES_TRAILER = re.compile(r"\n?\[\[PLACES\]\][\s\S]*$")
DL_TOKENS = re.compile(r"\n*\[\[(?:dl|vid):\{.*?\}\]\]")


def page_text(answer):
    """What the page shows of a finished answer: the [[PLACES]] trailer feeds
    the places module and is cut (send()'s regex), and download/video tokens
    render as boxes, not text (stripTokens). The raw text is kept beside it."""
    t = PLACES_TRAILER.sub("", answer)
    return DL_TOKENS.sub("", t).strip()


def port_open(port):
    with socket.socket() as so:
        so.settimeout(0.5)
        return so.connect_ex(("127.0.0.1", port)) == 0


class Lock:
    """The session's mkdir lock: wait until mkdir succeeds, own it, rm -rf at the end."""

    def __init__(self, path, owner):
        self.path, self.owner, self.held = path, owner, False

    def acquire(self):
        if not self.path:
            return
        waited = 0
        while True:
            try:
                os.mkdir(self.path)
                break
            except FileExistsError:
                if waited % 300 == 0:
                    who = ""
                    try:
                        with open(os.path.join(self.path, "owner")) as fh:
                            who = fh.read().strip()
                    except OSError:
                        pass
                    log("waiting for %s%s" % (self.path, (" (held by %s)" % who) if who else ""))
                time.sleep(30)
                waited += 30
            except FileNotFoundError:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.held = True
        try:
            with open(os.path.join(self.path, "owner"), "w") as fh:
                fh.write(self.owner + "\n")
        except OSError:
            pass
        log("holding %s" % self.path)

    def release(self):
        if self.held:
            shutil.rmtree(self.path, ignore_errors=True)
            self.held = False
            log("released %s" % self.path)


class DevCopy:
    """One windowless dev copy of the app in a folder of its own."""

    def __init__(self, repo, python, home, port, logfile):
        self.repo, self.python, self.home, self.port = repo, python, home, port
        self.logfile = logfile
        self.proc = self.key = self.token = None
        self.attached = False

    @property
    def cookie(self):
        return "millen_key_%d=%s" % (self.port, self.key)

    def headers(self, extra=None):
        h = {"Cookie": self.cookie, "X-Api-Token": self.token}
        h.update(extra or {})
        return h

    def _read_note(self, want_pid=None):
        note = os.path.join(self.home, "run", "instance.json")
        try:
            with open(note) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return False
        if want_pid is not None and d.get("pid") != want_pid:
            return False
        if not d.get("key") or not d.get("token"):
            return False
        self.key, self.token = d["key"], d["token"]
        return True

    def attach(self):
        if not port_open(self.port):
            sys.exit("nothing listens on %d; drop --attach" % self.port)
        if not self._read_note():
            sys.exit("no run/instance.json in %s" % self.home)
        self.attached = True
        self._wait_page(30)
        log("attached to the copy on %d" % self.port)

    def start(self, timeout=240):
        if port_open(self.port):
            sys.exit("port %d is taken; stop it (kill $(lsof -tnP -iTCP:%d -sTCP:LISTEN)) or pick --port"
                     % (self.port, self.port))
        env = {k: v for k, v in os.environ.items() if not k.startswith("MILLENAI_")}
        env.update(MILLENAI_DEV="1", MILLENAI_HOME=self.home, MILLENAI_NOWINDOW="1",
                   MILLENAI_PORT=str(self.port))
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        os.chmod(self.home, 0o700)
        self._log = open(self.logfile, "a")
        self.proc = subprocess.Popen([self.python, "millenai.py"], cwd=self.repo, env=env,
                                     stdout=self._log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                sys.exit("the copy exited with %s:\n%s" % (self.proc.returncode, self.tail()))
            if self._read_note(self.proc.pid):
                try:
                    self._wait_page(5)
                    log("copy up on %d (pid %d)" % (self.port, self.proc.pid))
                    return
                except Exception:
                    pass
            time.sleep(0.5)
        sys.exit("the copy never answered:\n%s" % self.tail())

    def _wait_page(self, secs):
        end = time.time() + secs
        last = None
        while time.time() < end:
            try:
                r = urllib.request.Request("http://127.0.0.1:%d/" % self.port,
                                           headers={"Cookie": self.cookie})
                with urllib.request.urlopen(r, timeout=5) as resp:
                    if resp.status == 200:
                        return
            except Exception as exc:
                last = exc
            time.sleep(0.5)
        raise RuntimeError("page not up: %s" % last)

    def tail(self, n=30):
        try:
            with open(self.logfile, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-n:])
        except OSError:
            return "(no log)"

    def api(self, path, body=None, method=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data,
                                   headers=self.headers({"Content-Type": "application/json"} if data else {}),
                                   method=method or ("POST" if data else "GET"))
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")

    def stop(self):
        if self.attached or not self.proc or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        log("copy stopped")


def pair_server(home):
    """Copy the real servers.json into the copy's folder, 0600. Its secrets are
    inline (access token, device seed), so nothing else is needed. Never read
    for printing."""
    src = os.path.join(REAL_APP_DIR, "servers.json")
    if not os.path.exists(src):
        return "the real data folder has no servers.json (no server paired)"
    os.makedirs(home, mode=0o700, exist_ok=True)
    dst = os.path.join(home, "servers.json")
    with open(src, "rb") as fh:
        raw = fh.read()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    os.chmod(dst, 0o600)
    return ""


def unpair_server(home):
    p = os.path.join(home, "servers.json")
    try:
        os.remove(p)
    except FileNotFoundError:
        pass


def public_server_status(entry):
    """What the run record keeps of a server: name, id, status, models. Never
    its address, device or tokens."""
    st = dict(entry.get("status") or {})
    return {"id": entry.get("id"), "name": entry.get("name"), "paired": entry.get("paired"),
            "reachable": st.get("reachable"), "auth": st.get("auth"), "err": st.get("err"),
            "kind": st.get("kind"), "latency_ms": st.get("latency_ms"),
            "gpu": entry.get("gpu"), "only": entry.get("only"),
            "models": [m.get("name") if isinstance(m, dict) else m for m in (entry.get("models") or [])]}


def chat(copy, body, max_wait, idle):
    """POST /api/chat and read the stream. Returns the record for this turn."""
    t0 = time.time()
    conn = http.client.HTTPConnection("127.0.0.1", copy.port, timeout=idle)
    data = json.dumps(body).encode("utf-8")
    conn.request("POST", "/api/chat", body=data, headers=copy.headers({"Content-Type": "application/json"}))
    resp = conn.getresponse()
    hdr = {k: v for k, v in resp.getheaders()}
    rec = {"status_code": resp.status, "headers": {k: v for k, v in hdr.items() if k.lower().startswith("x-")},
           "first_byte_s": None, "first_text_s": None, "elapsed_s": None,
           "events": [], "statuses": [], "steps": [], "drafts": [], "sources": None, "photos": None,
           "run_frames": [], "resets": 0, "truncated": False, "error": ""}
    rec["x_models"] = urllib.parse.unquote(hdr.get("X-Models", ""))
    rec["searched"] = hdr.get("X-Web-Search") == "1"
    if resp.status != 200:
        rec["error"] = resp.read().decode("utf-8", "replace")[:2000]
        rec["elapsed_s"] = round(time.time() - t0, 2)
        conn.close()
        return rec
    raw, seen, buf = "", 0, b""
    text_so_far = ""
    while True:
        if time.time() - t0 > max_wait:
            rec["truncated"] = True
            rec["error"] = "gave up after %ds" % max_wait
            break
        try:
            chunk = resp.read1(65536)
        except (socket.timeout, TimeoutError):
            rec["truncated"] = True
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
            ev = {"t": now, "tag": tag}
            if tag == "STATUS":
                ev["text"] = js
                rec["statuses"].append([now, js])
            else:
                try:
                    d = json.loads(js)
                except ValueError:
                    d = js
                if tag == "DRAFT" and isinstance(d, dict):
                    rec["drafts"].append({"t_s": now, "m": d.get("m"), "text": d.get("t", "")})
                    ev["m"] = d.get("m")
                    ev["chars"] = len(d.get("t", "") or "")
                elif tag == "STEP":
                    rec["steps"].append(dict(d, t_s=now) if isinstance(d, dict) else {"raw": d, "t_s": now})
                    ev["step"] = d
                elif tag == "RUN":
                    rec["run_frames"].append([now, d])
                    ev["run"] = d
                elif tag == "SOURCES":
                    rec["sources"] = d
                    ev["n"] = len(d) if isinstance(d, list) else None
                elif tag == "PHOTOS":
                    rec["photos"] = d
                elif tag == "RATE":
                    rec["rate_writer"] = d       # the final writer's own pace (6b515)
                    ev["data"] = d
                else:
                    ev["data"] = d
            rec["events"].append(ev)
        seen = len(frames)
        resets = raw.count(RESET)
        if resets > rec["resets"]:
            rec["resets"] = resets
            rec["events"].append({"t": now, "tag": "RESET"})
        text_so_far = stream_text(raw)
        if rec["first_text_s"] is None and text_so_far.strip():
            rec["first_text_s"] = now
            rec["events"].append({"t": now, "tag": "FIRST_TEXT"})
    conn.close()
    rec["elapsed_s"] = round(time.time() - t0, 2)
    answer = stream_text(raw).strip()
    rec["answer_raw"] = answer
    rec["answer"] = page_text(answer)
    rec["chars"] = len(answer)
    rec["raw_bytes"] = len(raw.encode("utf-8"))
    rec["is_error_answer"] = answer.startswith("\u26a0\ufe0f") or "\n\u26a0\ufe0f" in answer
    el = rec["elapsed_s"] or 0.0001
    rec["rate_ui_tok_s"] = round(len(answer) / 4.0 / el, 2)
    gen = el - (rec["first_text_s"] or 0)
    rec["rate_gen_tok_s"] = round(len(answer) / 4.0 / gen, 2) if gen > 0.5 else None
    return rec


def load_questions(path, only, limit):
    with open(path, encoding="utf-8") as fh:
        qs = json.load(fh)["questions"]
    if only:
        want = set(x.strip() for x in only.split(",") if x.strip())
        qs = [q for q in qs if q["id"] in want]
    if limit:
        qs = qs[:limit]
    return qs


def git_head(repo):
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        br = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=10)
        return (out.stdout.strip(), br.stdout.strip())
    except Exception:
        return ("", "")


def write_answers_md(run_dir, qs):
    lines = ["# Answers — %s\n" % os.path.basename(run_dir)]
    for q in qs:
        for k, turn in enumerate(q["turns"]):
            p = os.path.join(run_dir, "%s%s.json" % (q["id"], ".t%d" % (k + 1) if k else ""))
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as fh:
                r = json.load(fh)
            lines.append("## %s%s — %s\n" % (q["id"], " (turn %d)" % (k + 1) if k else "", q.get("topic", "")))
            lines.append("**Q:** %s\n" % turn["q"])
            lines.append("*%ss, %s chars, models: %s, searched: %s, drafts: %s*\n" % (
                r.get("elapsed_s"), r.get("chars"), r.get("x_models"), r.get("searched"),
                ", ".join(str(d.get("m")) for d in r.get("drafts", []))))
            if r.get("sources"):
                lines.append("Sources: " + "; ".join("%s <%s>" % (s.get("t"), s.get("u")) for s in r["sources"]) + "\n")
            lines.append((r.get("answer") or "(no answer)") + "\n")
    with open(os.path.join(run_dir, "answers.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS))
    ap.add_argument("--name", help="run name (folder under runs/); default <config>-<date>")
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.json"))
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true", help="re-ask questions already recorded")
    ap.add_argument("--repo", default=REPO, help="the checkout whose millenai.py runs")
    ap.add_argument("--python", default=DEFAULT_PY if os.path.exists(DEFAULT_PY) else sys.executable)
    ap.add_argument("--port", type=int, default=9894)
    ap.add_argument("--home", help="the copy's MILLENAI_HOME (default: ~/Library/Application Support/MillenAI-evalbench-<port>)")
    ap.add_argument("--attach", action="store_true", help="use a copy already running on --port (no start, no stop)")
    ap.add_argument("--keep-home", action="store_true", help="keep the copy's folder (servers.json is still deleted)")
    ap.add_argument("--persona", default=os.path.join(HERE, "persona.json"),
                    help="prefs JSON posted to the copy (user_name, persona, home_area, length); '' for none")
    ap.add_argument("--server-name", help="which paired server, by name (default: the first paired one)")
    ap.add_argument("--max-wait", type=int, default=1500, help="seconds per turn before giving up")
    ap.add_argument("--idle", type=int, default=400, help="seconds without a byte before giving up")
    ap.add_argument("--lock-dir", default=os.environ.get("EVAL_LOCK_DIR", DEFAULT_LOCK_DIR))
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between turns")
    args = ap.parse_args()

    if args.port in FORBIDDEN_PORTS or 8884 <= args.port <= 8999 and args.port % 2 == 0:
        sys.exit("port %d is the owner's app, the gauntlet's or an engine's" % args.port)
    cfg = CONFIGS[args.config]
    name = args.name or "%s-%s" % (args.config, time.strftime("%Y%m%d-%H%M"))
    run_dir = os.path.join(HERE, "runs", name)
    os.makedirs(run_dir, exist_ok=True)
    home = args.home or os.path.expanduser("~/Library/Application Support/MillenAI-evalbench-%d" % args.port)
    _h, _r = os.path.realpath(home).lower(), os.path.realpath(REAL_APP_DIR).lower()
    if _h == _r or _h.startswith(_r.rstrip(os.sep) + os.sep) or _r.startswith(_h.rstrip(os.sep) + os.sep):
        sys.exit("--home must not be the real data folder, inside it, or around it")
    qs = load_questions(args.questions, args.only, args.limit)
    log("run %s: %s, %d questions, repo %s" % (name, args.config, len(qs), args.repo))

    lock = Lock(os.path.join(args.lock_dir, cfg["lock"]) if not args.no_lock else "",
                "answer_eval %s pid %d" % (name, os.getpid()))
    copy = DevCopy(args.repo, args.python, home, args.port, os.path.join(run_dir, "devcopy.log"))
    summary = {"name": name, "config": args.config, "where": cfg["where"], "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "repo": args.repo, "head": git_head(args.repo), "questions": [], "server": None, "persona": None}
    paired = False
    try:
        lock.acquire()
        if cfg["where"] == "server" and not args.attach:
            why = pair_server(home)
            if why:
                sys.exit("can't reuse the pairing: %s" % why)
            paired = True
        if args.attach:
            copy.attach()
        else:
            copy.start()
        # the person's standing preferences, as the real app carries them
        if args.persona and os.path.exists(args.persona):
            with open(args.persona, encoding="utf-8") as fh:
                prefs = json.load(fh)
            st, out = copy.api("/api/prefs", prefs)
            summary["persona"] = {"file": os.path.basename(args.persona), "result": out}
            log("prefs: %s" % json.dumps(out))
        tier = cfg["tier"]
        if cfg["where"] == "server":
            st, view = copy.api("/api/servers?refresh=1", timeout=90)
            entries = [e for e in (view.get("servers") or []) if e.get("paired")]
            if args.server_name:
                entries = [e for e in entries if e.get("name") == args.server_name]
            if not entries:
                sys.exit("the copy sees no paired server (servers.json copied: %s)" % paired)
            srv = entries[0]
            summary["server"] = public_server_status(srv)
            log("server %s: reachable=%s auth=%s err=%r models=%d" % (
                srv.get("name"), summary["server"]["reachable"], summary["server"]["auth"],
                summary["server"]["err"], len(summary["server"]["models"])))
            tier = tier.format(sid=srv["id"])
        summary["tier"] = tier
        log("tier %s" % tier)

        for q in qs:
            msgs = []
            for k, turn in enumerate(q["turns"]):
                fname = "%s%s.json" % (q["id"], ".t%d" % (k + 1) if k else "")
                path = os.path.join(run_dir, fname)
                prior = None
                if os.path.exists(path) and not args.redo:
                    with open(path, encoding="utf-8") as fh:
                        prior = json.load(fh)
                    if prior.get("answer") and not prior.get("is_error_answer"):
                        log("%s: kept" % fname)
                        msgs += [{"role": "user", "content": turn["q"]},
                                 {"role": "assistant", "content": prior["answer"]}]
                        continue
                msgs.append({"role": "user", "content": turn["q"]})
                body = {"model": "", "models": [], "tier": tier, "messages": list(msgs),
                        "auto_web": True, "images": [], "docs": [], "agent": "", "mode_comp": ""}
                log("%s: asking (%s)" % (fname, turn["q"][:70].replace("\n", " ")))
                for attempt in range(30):
                    rec = chat(copy, body, args.max_wait, args.idle)
                    if rec["status_code"] == 409 and "bench" in (rec.get("error") or ""):
                        log("a benchmark holds the engines; waiting 30 s")
                        time.sleep(30)
                        continue
                    break
                rec.update({"id": q["id"], "turn": k + 1, "category": q.get("category"), "topic": q.get("topic"),
                            "question": turn["q"], "tier": tier, "config": args.config, "run": name,
                            "asked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": body})
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, ensure_ascii=False, indent=1)
                log("%s: %ss, %d chars, models=%s, drafts=%d, sources=%d%s" % (
                    fname, rec["elapsed_s"], rec.get("chars", 0), rec.get("x_models"), len(rec["drafts"]),
                    len(rec["sources"] or []), (" ERROR " + rec["error"][:80]) if rec.get("error") else ""))
                summary["questions"].append({"file": fname, "elapsed_s": rec["elapsed_s"], "chars": rec.get("chars"),
                                             "error": rec.get("error", "")})
                msgs.append({"role": "assistant", "content": rec.get("answer") or ""})
                time.sleep(args.pause)
        summary["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    finally:
        try:
            copy.stop()
        finally:
            if paired:
                unpair_server(home)
            if not args.attach and not args.keep_home:
                shutil.rmtree(home, ignore_errors=True)
            lock.release()
        with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=1)
        write_answers_md(run_dir, qs)
        log("wrote %s" % run_dir)


if __name__ == "__main__":
    main()
