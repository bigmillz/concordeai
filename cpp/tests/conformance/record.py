"""The conformance recorder (RUST_PLAN.md §8, 6b536; CPP_PLAN.md step 2):
the same requests to the Python app and to a front (the Rust core, or the C++
concorde-front) with the Python server as its sidecar, and a diff of status,
headers and bodies.

Carried over from the rust-core branch (2985007) for the C++ track, with
four changes: --front names the binary (--core still works), --ports picks
the two dev ports, the Python mode defaults to nowindow (the C++ front passes
the environment to its sidecar untouched, so both sides run the same mode),
and the report goes to --out instead of beside this file.

Two dev copies in fresh folders of their own, seeded alike: A is the Python
app as the shell runs it today (MILLENAI_SHELL=tauri: no window, a boot note,
the shell's page), B is the core (concorde-core) fronting a sidecar. Both
talk to one recording provider stub (the dev-only provider-stub hook), so a
streamed /api/chat runs for real with no model anywhere: the stub stalls
mid-answer long enough for the server's 20 s heartbeat to fire, and the
recorder times every chunk.

What is compared, per entry: the status; every header but Date, Server and
the hop-by-hop ones (http.server speaks HTTP/1.0 and closes, hyper speaks
HTTP/1.1 and may chunk); the body, after each copy's own port, key, token,
boot code, profile tag and folder are replaced by placeholders. For the
stream: the frame tags in order, the prose, the custom headers, and the
heartbeat's timing.

    python3 cpp/tests/conformance/record.py <repo> [--front <bin>] [--ports 9881,9883]
            [--keep] [--python-mode nowindow|tauri] [--stall 32] [--out report.json]

Exit 0 when every entry is identical (after the declared normalisation).
Ports: 9881 (A), 9883 (B) by default; the stub and the sidecar take
ephemeral ports. Never 8889 or 9901-9903 (CLAUDE.md).
"""
import argparse
import ast
import hashlib
import http.client
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_A, PORT_B = 9881, 9883
VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
IGNORE_HEADERS = {"date", "server", "connection", "transfer-encoding", "keep-alive"}
ANSWER = ("The stand-in wrote this reply for the conformance recording. It has a first part, "
          "then a long pause, then a second part, and no numbers anywhere in it.")


# ---------------------------------------------------------------- the stub
class Stub(http.server.BaseHTTPRequestHandler):
    """An OpenAI-shaped provider: /models, and /chat/completions that
    streams one delta, stalls, then the rest. One for both copies."""
    stall = 24.0
    log = []

    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._any()

    def do_POST(self):
        self._any()

    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        p = self.path.split("?")[0]
        Stub.log.append((time.monotonic(), self.command, p))
        if p.endswith("/models"):
            return self._out(200, {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.8-27b"}]})
        try:
            d = json.loads(body) if body else {}
        except ValueError:
            d = {}
        if p.endswith("/chat/completions"):
            if d.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                half = len(ANSWER) // 2
                self.wfile.write(("data: %s\n\n" % json.dumps(
                    {"choices": [{"delta": {"content": ANSWER[:half]}}]})).encode())
                self.wfile.flush()
                time.sleep(Stub.stall)
                self.wfile.write(("data: %s\n\ndata: [DONE]\n\n" % json.dumps(
                    {"choices": [{"delta": {"content": ANSWER[half:]}}]})).encode())
                self.wfile.flush()
                return
            return self._out(200, {"choices": [{"message": {"content": ANSWER}}],
                                   "usage": {"prompt_tokens": 9, "completion_tokens": 40}})
        return self._out(404, {"error": {"message": "nf"}})


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- the copies
class Copy:
    def __init__(self, name, port, home, argv, env, cwd):
        self.name, self.port, self.home, self.argv, self.env, self.cwd = name, port, home, argv, env, cwd
        self.proc = self.key = self.token = self.boot = self.tag = None
        self.log = None

    def start(self, timeout=180):
        self.log = open(os.path.join(os.path.dirname(self.home), self.name + ".log"), "w")
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        note = os.path.join(self.home, "run", "instance.json")
        bootp = os.path.join(self.home, "run", "boot.json")
        while time.monotonic() - self.t0 < timeout:
            if self.proc.poll() is not None:
                sys.exit("%s exited %s:\n%s" % (self.name, self.proc.returncode, self.tail()))
            try:
                d = json.load(open(note))
                if d.get("pid") == self.proc.pid:
                    self.key, self.token = d["key"], d["token"]
                    b = json.load(open(bootp))
                    if b.get("pid") == self.proc.pid:
                        self.boot = b["code"]
                        self.t_note = time.monotonic() - self.t0
                        break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.02)
        else:
            sys.exit("%s never wrote its notes:\n%s" % (self.name, self.tail()))
        while time.monotonic() - self.t0 < timeout:
            try:
                s, h, b = self.req("GET", "/", self.h(cookie=True))
                if s == 200:
                    self.t_page = time.monotonic() - self.t0
                    m = re.match(r'"b\d+-\d+-(.+)"$', h.get("etag", ""))
                    self.tag = m.group(1) if m else ""
                    return self
            except OSError:
                pass
            time.sleep(0.02)
        sys.exit("%s never answered /:\n%s" % (self.name, self.tail()))

    def tail(self, n=30):
        try:
            self.log.flush()
            return "".join(open(self.log.name, errors="replace").readlines()[-n:])
        except OSError:
            return "(no log)"

    def h(self, cookie=True, token=False, host=None, **extra):
        out = {"Host": host or "127.0.0.1:%d" % self.port}
        if cookie:
            out["Cookie"] = "millen_key_%d=%s" % (self.port, self.key)
        if token:
            out["X-Api-Token"] = self.token
        out.update({k.replace("_", "-"): v for k, v in extra.items()})
        return out

    def req(self, method, path, headers, body=None, timeout=20, chunked=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        if body is not None and not chunked:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders()
        if body is not None:
            if chunked:
                c.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
            else:
                c.send(body)
        r = c.getresponse()
        data = r.read()
        hdrs = {}
        for k, v in r.getheaders():
            hdrs.setdefault(k.lower(), []).append(v)
        hdrs = {k: "; ".join(v) for k, v in hdrs.items()}
        c.close()
        return r.status, hdrs, data

    def stream(self, method, path, headers, body, timeout=180):
        """The chunks as they arrive, each with its time since the request."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        t0 = time.monotonic()
        r = c.getresponse()
        t_h = time.monotonic() - t0
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        chunks = []
        while True:
            piece = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
            if not piece:
                break
            chunks.append((round(time.monotonic() - t0, 3), piece))
        c.close()
        return r.status, hdrs, chunks, t_h

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)

    def norm(self, s):
        """This copy's own numbers and secrets as placeholders."""
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        for v, tag in ((self.key, "<KEY>"), (self.token, "<TOKEN>"), (self.boot or "\0", "<BOOT>"),
                       (self.tag or "\0", "<TAG>"), (self.home, "<HOME>"), (":%d" % self.port, ":<PORT>"),
                       ("millen_key_%d" % self.port, "millen_key_<PORT>")):
            s = s.replace(v, tag)
        # the one token that says whose window it is (6b532): the shell paints
        # its own night, pywebview's window wipes
        s = re.sub(r'if\("[01]"==="1"\)', 'if("<WIPE>"==="1")', s)
        return s


def seed(home, sky_name, blob):
    os.makedirs(os.path.join(home, "sky"), mode=0o700, exist_ok=True)
    with open(os.path.join(home, "prefs.json"), "w") as fh:
        json.dump({"turbo": False, "auto_web": False}, fh)
    with open(os.path.join(home, "sky", sky_name), "wb") as fh:
        fh.write(blob)


def sky_file_name(src):
    """_sky_path(0)'s file name: sha1 of SKY_SOURCES[0]."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SKY_SOURCES":
            urls = ast.literal_eval(node.value)
            return "sky-%s.mov" % hashlib.sha1(urls[0].encode()).hexdigest()[:10]
    raise SystemExit("no SKY_SOURCES in millenai.py")


# ---------------------------------------------------------------- the corpus
FRAME = re.compile(rb"\x00([A-Z0-9]+)(?::(.*?))?\x00", re.S)


def frames_and_prose(data):
    tags, prose, pos = [], bytearray(), 0
    for m in FRAME.finditer(data):
        prose += data[pos:m.start()]
        tags.append((m.group(1).decode(), m.group(2) or b""))
        pos = m.end()
    prose += data[pos:]
    return tags, bytes(prose)


def corpus(c, boot_code):
    """Each entry: name, method, path, headers, body, chunked. Built per
    copy, since the headers carry its cookie and token."""
    B = c.h(cookie=True, token=True)
    J = dict(B, **{"Content-Type": "application/json"})
    return [
        # ---- the gate, every refusal (GET and POST shapes)
        ("gate: no cookie, page", "GET", "/", c.h(cookie=False), None, False),
        ("gate: no cookie, POST", "POST", "/api/chats", dict(c.h(cookie=False), **{"Content-Type": "application/json"}), b"{}", False),
        ("gate: rebinding Host", "GET", "/", c.h(host="evil.test:%d" % c.port), None, False),
        ("gate: another port's Host", "GET", "/", c.h(host="127.0.0.1:%d" % (c.port + 100)), None, False),
        ("gate: no Host", "GET", "/", {"Cookie": c.h()["Cookie"]}, None, False),
        ("gate: localhost Host is fine", "GET", "/api/remote/classify?cmd=ls", c.h(token=True, host="localhost:%d" % c.port), None, False),
        ("gate: X-Forwarded-For", "GET", "/", c.h(X_Forwarded_For="10.0.0.9"), None, False),
        ("gate: Forwarded", "GET", "/", c.h(Forwarded="for=10.0.0.9"), None, False),
        ("gate: Cf-Connecting-Ip", "GET", "/", c.h(Cf_Connecting_Ip="10.0.0.9"), None, False),
        ("gate: empty proxy header passes", "GET", "/", c.h(X_Real_IP=""), None, False),
        ("gate: cookie alone on /api", "GET", "/api/prefs", c.h(), None, False),
        ("gate: cookie alone on a POST", "POST", "/api/prefs", dict(c.h(), **{"Content-Type": "application/json"}), b'{"turbo": false}', False),
        ("gate: token alone", "GET", "/api/prefs", c.h(cookie=False, token=True), None, False),
        ("gate: wrong token", "GET", "/api/prefs", c.h(X_Api_Token="x" * 43), None, False),
        ("gate: short token", "GET", "/api/prefs", c.h(X_Api_Token="x"), None, False),
        ("gate: another port's cookie name", "GET", "/", {"Host": "127.0.0.1:%d" % c.port, "Cookie": "millen_key_%d=%s" % (c.port + 100, c.key)}, None, False),
        ("gate: wrong key", "GET", "/", {"Host": "127.0.0.1:%d" % c.port, "Cookie": "millen_key_%d=%s" % (c.port, "k" * 43)}, None, False),
        ("gate: a stray same-name cookie can't shadow the key", "GET", "/", {"Host": "127.0.0.1:%d" % c.port, "Cookie": "millen_key_%d=zzz; millen_key_%d=%s" % (c.port, c.port, c.key)}, None, False),
        ("gate: /?key= is not the page", "GET", "/?key=" + c.key, c.h(), None, False),
        ("gate: /?anything needs the token", "GET", "/?x=1", c.h(), None, False),
        ("gate: //api needs the token", "GET", "//api/prefs", c.h(), None, False),
        ("gate: //api with the token", "GET", "//api/prefs", B, None, False),
        ("gate: PUT", "PUT", "/", c.h(), None, False),
        ("gate: HEAD", "HEAD", "/", c.h(), None, False),
        ("gate: DELETE with a body", "DELETE", "/api/chats", J, b'{"x": 1}', False),
        ("gate: POST to an unknown route", "POST", "/nope", J, b"{}", False),
        ("gate: GET an unknown route", "GET", "/nope", B, None, False),
        ("gate: a cross-site form POST", "POST", "/api/prefs", dict(B, **{"Content-Type": "application/x-www-form-urlencoded"}), b"turbo=1", False),
        ("gate: a cross-site Origin", "POST", "/api/prefs", dict(J, Origin="http://evil.test"), b'{"turbo": false}', False),
        ("gate: a same-site Origin", "POST", "/api/prefs", dict(J, Origin="http://127.0.0.1:%d" % c.port), b'{"turbo": false}', False),
        ("gate: a null Origin", "POST", "/api/prefs", dict(J, Origin="null"), b'{"turbo": false}', False),
        # ---- the page and the boot code
        ("page: cookie alone", "GET", "/", c.h(), None, False),
        ("page: with the token too", "GET", "/", B, None, False),
        ("page: If-None-Match hit", "GET", "/", c.h(If_None_Match="__ETAG__"), None, False),
        ("page: If-None-Match miss", "GET", "/", c.h(If_None_Match='"b1-2-x"'), None, False),
        ("boot: a wrong code", "GET", "/?boot=wrong", c.h(cookie=False), None, False),
        ("boot: an empty code", "GET", "/?boot=", c.h(cookie=False), None, False),
        ("boot: the code", "GET", "/?boot=" + boot_code, c.h(cookie=False), None, False),
        ("boot: a replay", "GET", "/?boot=" + boot_code, c.h(cookie=False), None, False),
        ("boot: keyed already", "GET", "/?boot=anything", c.h(), None, False),
        # ---- /static/, the beacon and a sky clip, Range by Range
        ("beacon: whole", "GET", "/static/vfx/hdr-beacon.mp4", c.h(), None, False),
        ("beacon: bytes=100-199", "GET", "/static/vfx/hdr-beacon.mp4", c.h(Range="bytes=100-199"), None, False),
        ("beacon: bytes=6000-", "GET", "/static/vfx/hdr-beacon.mp4", c.h(Range="bytes=6000-"), None, False),
        ("beacon: bytes=-100 (suffix is no range here)", "GET", "/static/vfx/hdr-beacon.mp4", c.h(Range="bytes=-100"), None, False),
        ("beacon: past the end", "GET", "/static/vfx/hdr-beacon.mp4", c.h(Range="bytes=9999-99999"), None, False),
        ("beacon: bytes=0-0", "GET", "/static/vfx/hdr-beacon.mp4", c.h(Range="bytes=0-0"), None, False),
        ("beacon: with the token too", "GET", "/static/vfx/hdr-beacon.mp4", dict(B, Range="bytes=0-9"), None, False),
        ("sky: whole", "GET", "/static/sky/0.mov", c.h(), None, False),
        ("sky: bytes=0-1023", "GET", "/static/sky/0.mov", c.h(Range="bytes=0-1023"), None, False),
        ("sky: bytes=-1024", "GET", "/static/sky/0.mov", c.h(Range="bytes=-1024"), None, False),
        ("sky: bytes=1000-", "GET", "/static/sky/0.mov", c.h(Range="bytes=1000-"), None, False),
        ("sky: bytes=0-9,20-29 (first range)", "GET", "/static/sky/0.mov", c.h(Range="bytes=0-9,20-29"), None, False),
        ("sky: bytes=- (bad)", "GET", "/static/sky/0.mov", c.h(Range="bytes=-"), None, False),
        ("sky: bytes=5 (bad)", "GET", "/static/sky/0.mov", c.h(Range="bytes=5"), None, False),
        ("sky: bytes=a-b (bad)", "GET", "/static/sky/0.mov", c.h(Range="bytes=a-b"), None, False),
        ("sky: bytes=0-999999999 (clamped)", "GET", "/static/sky/0.mov", c.h(Range="bytes=0-999999999"), None, False),
        ("sky: bytes=99999999- (416)", "GET", "/static/sky/0.mov", c.h(Range="bytes=99999999-"), None, False),
        ("sky: bytes=10-5 (416)", "GET", "/static/sky/0.mov", c.h(Range="bytes=10-5"), None, False),
        ("sky: items=0-1 (not bytes)", "GET", "/static/sky/0.mov", c.h(Range="items=0-1"), None, False),
        ("sky: a clip not cached", "GET", "/static/sky/1.mov", c.h(), None, False),
        ("sky: past the list", "GET", "/static/sky/999.mov", c.h(), None, False),
        ("sky: four digits need the token", "GET", "/static/sky/1000.mov", c.h(), None, False),
        ("sky: four digits with the token", "GET", "/static/sky/1000.mov", B, None, False),
        ("sky: not a number, cookie alone", "GET", "/static/sky/abc.mov", c.h(), None, False),
        ("sky: not a number, with the token", "GET", "/static/sky/abc.mov", B, None, False),
        ("sky: a query string", "GET", "/static/sky/0.mov?x=1", B, None, False),
        ("static: another file", "GET", "/static/other.mov", B, None, False),
        ("static: /static/", "GET", "/static/", B, None, False),
        # ---- proxied routes
        ("api: classify danger", "GET", "/api/remote/classify?cmd=rm%20-rf%20/", B, None, False),
        ("api: classify read", "GET", "/api/remote/classify?cmd=ls%20-la", B, None, False),
        ("api: prefs", "GET", "/api/prefs", B, None, False),
        ("api: tiers", "GET", "/api/tiers", B, None, False),
        ("api: me", "GET", "/api/me", B, None, False),
        ("api: chats", "GET", "/api/chats", B, None, False),
        ("api: remote config", "GET", "/api/remote/config", B, None, False),
        ("api: prefs POST", "POST", "/api/prefs", J, b'{"turbo": false}', False),
        ("api: prefs POST, chunked", "POST", "/api/prefs", dict(J, **{"Transfer-Encoding": "chunked"}), b'{"turbo": false}', True),
        ("api: a bad JSON body", "POST", "/api/prefs", J, b"{not json", False),
        ("api: workspace GET", "GET", "/api/workspace", B, None, False),
        ("api: workspace/set as GET is 405", "GET", "/api/workspace/set", B, None, False),
        ("api: _core/info is nobody's from outside", "GET", "/_core/info", B, None, False),
        ("api: _core/tokens is nobody's from outside", "GET", "/_core/tokens", B, None, False),
    ]


def run(args):
    global PORT_A, PORT_B
    repo = os.path.abspath(args.repo)
    if args.ports:
        PORT_A, PORT_B = (int(x) for x in args.ports.split(","))
    if {PORT_A, PORT_B} & {8889, 9897, 9901, 9902, 9903}:
        sys.exit("those ports are the app's, ConcordeGo's or the gauntlet's")
    core = os.path.abspath(args.front or args.core or os.path.join(repo, "cpp", "build", "concorde-front"))
    if not os.path.exists(core):
        sys.exit("no front at %s (cmake --build in cpp/)" % core)
    src = open(os.path.join(repo, "millenai.py"), encoding="utf-8").read()
    sky_name = sky_file_name(src)
    blob = hashlib.sha256(b"concorde-conformance").digest() * (3 * 1024 * 1024 // 32)

    for p in (PORT_A, PORT_B):
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", p)) == 0:
                sys.exit("port %d is taken; stop what holds it (lsof -tnP -iTCP:%d -sTCP:LISTEN)" % (p, p))

    Stub.stall = args.stall
    stub_port = free_port()
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % stub_port

    tmp = tempfile.mkdtemp(prefix="cai-conformance-")
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("MILLENAI_") and not k.startswith("CONCORDE_")}
    base_env.update(HF_HUB_OFFLINE="1", HTTPS_PROXY=surl, HTTP_PROXY=surl, https_proxy=surl, http_proxy=surl,
                    NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    hooks = "provider-stub=%s,no-downloads,boot-code" % surl

    home_a = os.path.join(tmp, "A")
    home_b = os.path.join(tmp, "B")
    seed(home_a, sky_name, blob)
    seed(home_b, sky_name, blob)
    env_a = dict(base_env, MILLENAI_DEV="1", MILLENAI_HOME=home_a, MILLENAI_PORT=str(PORT_A), MILLENAI_TEST_HOOKS=hooks)
    if args.python_mode == "tauri":
        env_a["MILLENAI_SHELL"] = "tauri"
    else:
        env_a["MILLENAI_NOWINDOW"] = "1"
    env_b = dict(base_env, MILLENAI_DEV="1", MILLENAI_HOME=home_b, MILLENAI_PORT=str(PORT_B), MILLENAI_TEST_HOOKS=hooks,
                 MILLENAI_NOWINDOW="1", CONCORDE_PYTHON=VENV, CONCORDE_SERVER=os.path.join(repo, "millenai.py"))
    A = Copy("A-python", PORT_A, home_a, [VENV, "millenai.py"], env_a, repo)
    B = Copy("B-front", PORT_B, home_b, [core], env_b, repo)
    copies = [A, B]

    def cleanup(*_):
        for c in copies:
            c.stop()
        stub.shutdown()
        if args.keep:
            print("folders kept in", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    for sn in ("SIGTERM", "SIGHUP", "SIGINT"):
        signal.signal(getattr(signal, sn), lambda *_: (cleanup(), sys.exit(1)))

    results = []
    try:
        A.start()
        B.start()
        print("A: %s pid %d, note at %.2fs, page at %.2fs, tag %s" % (A.name, A.proc.pid, A.t_note, A.t_page, A.tag))
        print("B: %s pid %d, note at %.2fs, page at %.2fs, tag %s" % (B.name, B.proc.pid, B.t_note, B.t_page, B.tag))
        # the same cloud key into each (the stub validates it)
        for c in copies:
            s, _, b = c.req("POST", "/api/cloud/set", dict(c.h(token=True), **{"Content-Type": "application/json"}),
                            json.dumps({"provider": "groq", "key": "gsk_" + "b" * 52}).encode(), timeout=60)
            if s != 200:
                print("  %s cloud/set: %s %r" % (c.name, s, b[:200]))

        etags = {}
        for c in copies:
            etags[c.name] = c.req("GET", "/", c.h())[1].get("etag", "")

        entries_a = corpus(A, A.boot)
        entries_b = corpus(B, B.boot)
        for ea, eb in zip(entries_a, entries_b):
            name = ea[0]
            got = []
            for c, e in ((A, ea), (B, eb)):
                _, method, path, headers, body, chunked = e
                headers = {k: (etags[c.name] if v == "__ETAG__" else v) for k, v in headers.items()}
                try:
                    s, h, data = c.req(method, path, headers, body, chunked=chunked)
                except Exception as exc:  # a reset, a hang-up
                    s, h, data = -1, {}, repr(exc).encode()
                got.append((s, {k: c.norm(v) for k, v in h.items() if k not in IGNORE_HEADERS}, c.norm(data)))
            (sa, ha, ba), (sb, hb, bb) = got
            diffs = []
            if sa != sb:
                diffs.append("status %s vs %s" % (sa, sb))
            for k in sorted(set(ha) | set(hb)):
                if ha.get(k) != hb.get(k):
                    diffs.append("header %s: %r vs %r" % (k, ha.get(k), hb.get(k)))
            if ba != bb:
                i = next((i for i, (x, y) in enumerate(zip(ba, bb)) if x != y), min(len(ba), len(bb)))
                diffs.append("body differs at %d: %r vs %r (len %d vs %d)" % (
                    i, ba[max(0, i - 30):i + 50], bb[max(0, i - 30):i + 50], len(ba), len(bb)))
            results.append((name, sa, sb, diffs))
            print("  %-4s %-58s %s %s%s" % ("SAME" if not diffs else "DIFF", name, sa, sb,
                                            ("  — " + "; ".join(diffs)) if diffs else ""))

        # ---- the stream: one /api/chat each, the stub stalling mid-answer
        print("stream: /api/chat, Cloud Only, the stub stalls %.0f s mid-answer" % Stub.stall)
        chat = json.dumps({"model": "", "models": [], "tier": "Cloud Only", "auto_web": False,
                           "messages": [{"role": "user", "content": "Say hello in one short sentence."}]}).encode()
        streams = {}
        for c in copies:
            s, h, chunks, t_h = c.stream("POST", "/api/chat", dict(c.h(token=True), **{"Content-Type": "application/json"}), chat)
            data = b"".join(p for _, p in chunks)
            tags, prose = frames_and_prose(data)
            # the heartbeat: a STATUS frame repeated after a quiet spell of 20 s or more
            gaps = [(round(b - a, 1), ) for (a, _), (b, _) in zip(chunks, chunks[1:])]
            quiet = max((g[0] for g in gaps), default=0)
            # the heartbeat: a STATUS frame, alone, after a quiet spell of
            # 19 s or more (the thread looks every 5 s for >20 s of quiet)
            beat_at = beat_piece = None
            prev = None
            for t, piece in chunks:
                if prev is not None and t - prev >= 19.0 and piece.startswith(b"\x00STATUS:"):
                    beat_at = round(t, 1)
                    beat_piece = piece[:120]
                    break
                prev = t
            streams[c.name] = {"status": s, "x_models": h.get("x-models"), "x_web": h.get("x-web-search"),
                               "content_type": h.get("content-type"), "headers_s": round(t_h, 3),
                               "first_chunk_s": chunks[0][0] if chunks else None,
                               "last_chunk_s": chunks[-1][0] if chunks else None, "chunks": len(chunks),
                               "tags": [t for t, _ in tags], "prose": c.norm(prose), "longest_quiet_s": quiet,
                               "beat_at_s": beat_at, "beat": (beat_piece.decode("utf-8", "replace") if beat_at else None),
                               "bytes": len(data)}
            print("  %s: %s %d chunks, headers %.3fs, first %.3fs, last %.1fs, quiet %.1fs, beat %s, X-Models %r, X-Web-Search %r"
                  % (c.name, s, len(chunks), t_h, chunks[0][0] if chunks else -1, chunks[-1][0] if chunks else -1,
                     quiet, beat_at, h.get("x-models"), h.get("x-web-search")))
        sa, sb = streams[A.name], streams[B.name]
        diffs = []
        for k in ("status", "x_models", "x_web", "content_type", "tags", "prose"):
            if sa[k] != sb[k]:
                diffs.append("%s: %r vs %r" % (k, sa[k], sb[k]))
        if (sa["beat_at_s"] is None) != (sb["beat_at_s"] is None):
            diffs.append("heartbeat: %r vs %r" % (sa["beat_at_s"], sb["beat_at_s"]))
        elif sa["beat_at_s"] and abs(sa["beat_at_s"] - sb["beat_at_s"]) > 2.0:
            diffs.append("heartbeat timing: %.1fs vs %.1fs" % (sa["beat_at_s"], sb["beat_at_s"]))
        if sa["beat_at_s"] is None:
            diffs.append("no heartbeat seen on A (stall %.0fs; quiet %.1fs)" % (Stub.stall, sa["longest_quiet_s"]))
        results.append(("stream: /api/chat frames, prose, headers, heartbeat", sa["status"], sb["status"], diffs))
        print("  %-4s %-58s %s %s%s" % ("SAME" if not diffs else "DIFF", "stream: /api/chat", sa["status"], sb["status"],
                                        ("  — " + "; ".join(diffs)) if diffs else ""))
        print("  tags A: %s" % " ".join(sa["tags"]))
        print("  tags B: %s" % " ".join(sb["tags"]))
        print("  heartbeat A at %s: %r" % (sa["beat_at_s"], sa["beat"]))
        print("  heartbeat B at %s: %r" % (sb["beat_at_s"], sb["beat"]))

        out = {"a": {"argv": A.argv, "mode": args.python_mode, "t_note": A.t_note, "t_page": A.t_page},
               "b": {"argv": B.argv, "t_note": B.t_note, "t_page": B.t_page},
               "results": [{"name": n, "a": sa_, "b": sb_, "diffs": d} for n, sa_, sb_, d in results],
               "stream": streams}
        with open(args.out or os.path.join(tmp, "last_run.json"), "w") as fh:
            json.dump(out, fh, indent=1)
        same = sum(1 for r in results if not r[3])
        print("CONFORMANCE: %d/%d identical" % (same, len(results)))
        for n, _, _, d in results:
            if d:
                print("  DIFF:", n, "—", "; ".join(d))
        return 0 if same == len(results) else 1
    finally:
        cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--core")
    ap.add_argument("--front")
    ap.add_argument("--ports")
    ap.add_argument("--out")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--python-mode", choices=("tauri", "nowindow"), default="nowindow")
    # the heartbeat thread looks every 5 s for a stream quiet >20 s, so it
    # fires 25 s after the last write: a 32 s stall shows it on both sides
    ap.add_argument("--stall", type=float, default=32.0)
    sys.exit(run(ap.parse_args()))
