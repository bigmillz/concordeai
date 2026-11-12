"""End to end through the C++ front (CPP_PLAN.md step 1), windowless.

Starts concorde-front as a dev copy in a fresh folder (MILLENAI_DEV=1, its
own MILLENAI_HOME, MILLENAI_NOWINDOW=1, a dev port; never 8889 or
9901-9903), with the stand-in provider (stub_provider.py) as its only cloud
key, its HTTPS proxy, and no server pairing at all. Then, over the wire:

  * the notes name the front's port and pid (key and token the server's);
  * the gate is the server's, unchanged: the page with the cookie, 403
    without it, the token on /api, a Host naming the private port, a proxy
    header, another port's cookie, the boot trade's cookie named for the
    PUBLIC port;
  * the same answers through the front as straight from the server (the
    page byte for byte, a 404, a 403, a JSON route), and the first byte's
    time both ways;
  * a streamed /api/chat (Cloud Only, the stand-in): the frames arrive
    one at a time, in order, with the prose and X-Models intact.

    python3 cpp/tests/e2e_front.py <concorde-front> [--port 9893] [--keep]

Exit 0 when every check passes. Stops only the process it started.
"""
import http.client
import json
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
sys.path.insert(0, HERE)
import stub_provider  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  ok  " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)[:400]), flush=True)


def port_open(p):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def req(port, method, path, headers=None, body=None, host=None, timeout=30):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", host or "127.0.0.1:%d" % port)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    t0 = time.perf_counter()
    r = c.getresponse()
    first = r.read(1)
    ttfb = time.perf_counter() - t0
    data = first + r.read()
    hdrs = r.getheaders()
    c.close()
    return r.status, hdrs, data, ttfb


def main():
    args = sys.argv[1:]
    front = os.path.abspath(args[0])
    port = int(args[args.index("--port") + 1]) if "--port" in args else 9893
    keep = "--keep" in args
    if port in (8889, 9901, 9902, 9903, 9897) or port_open(port):
        sys.exit("port %d is not free for a dev copy" % port)

    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]

    base = tempfile.mkdtemp(prefix="cpp-e2e-")
    home = os.path.join(base, "home")
    os.makedirs(home, mode=0o700)
    with open(os.path.join(home, "cloud.json"), "w") as fh:
        json.dump({"active": "groq", "providers": {"groq": {
            "name": "Groq", "base": surl + "/api.groq.com/openai/v1", "key": "gsk_standin_for_the_cpp_front",
            "model": "qwen/qwen3.8-27b", "models": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
            "status": "ok"}}}, fh)
    with open(os.path.join(home, "prefs.json"), "w") as fh:
        json.dump({"turbo": True}, fh)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_NOWINDOW="1", MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="provider-stub=%s,no-downloads,local-record,boot-code" % surl,
               CONCORDE_SERVER=os.path.join(REPO, "millenai.py"), CONCORDE_PYTHON=VENV,
               CONCORDE_SHELL_TIMING="1",
               HTTPS_PROXY=surl, HTTP_PROXY=surl, https_proxy=surl, http_proxy=surl,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", HF_HUB_OFFLINE="1")
    log = open(os.path.join(base, "front.log"), "w")
    t0 = time.perf_counter()
    p = subprocess.Popen([front], env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
    note = boot = None
    try:
        while time.perf_counter() - t0 < 120:
            if p.poll() is not None:
                sys.exit("concorde-front exited %s:\n%s" % (p.returncode, open(log.name).read()[-3000:]))
            try:
                d = json.load(open(os.path.join(home, "run", "instance.json")))
                b = json.load(open(os.path.join(home, "run", "boot.json")))
                if d.get("pid") == p.pid and b.get("pid") == p.pid:
                    note, boot = d, b
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        t_note = time.perf_counter() - t0
        check("the notes name the front's port and pid", note and note["port"] == port and boot,
              [note, boot])
        key, tok = note["key"], note["token"]
        kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        py = int(kids[0]) if kids else 0
        lsof = subprocess.run(["lsof", "-a", "-nP", "-p", str(py), "-iTCP", "-sTCP:LISTEN"],
                              capture_output=True, text=True).stdout
        m = re.search(r"127\.0\.0\.1:(\d+) \(LISTEN\)", lsof)
        priv = int(m.group(1)) if m else 0
        check("the server listens on a private port, never the public one", priv and priv != port, lsof)

        ck = "millen_key_%d=%s" % (port, key)
        ckp = "millen_key_%d=%s" % (priv, key)
        H = {"Cookie": ck}
        HT = {"Cookie": ck, "X-Api-Token": tok}

        # ---- the gate, unchanged
        s, h, b, _ = req(port, "GET", "/?boot=" + boot["code"])
        sc = dict((k.lower(), v) for k, v in h).get("set-cookie", "")
        check("the boot trade: 302 and the cookie named for the PUBLIC port",
              s == 302 and sc.startswith("millen_key_%d=%s;" % (port, key)) and "SameSite=Strict" in sc, [s, sc])
        s2 = req(port, "GET", "/?boot=" + boot["code"])[0]
        check("a replayed boot code is refused", s2 == 403, s2)
        s, h, page, _ = req(port, "GET", "/", H)
        check("the page with the cookie", s == 200 and b"<html" in page[:400].lower() and len(page) > 300000,
              [s, len(page)])
        check("no secret in the page", key.encode() not in page and tok.encode() not in page)
        check("the page without the cookie: 403", req(port, "GET", "/")[0] == 403)
        check("/api with the cookie alone: 403", req(port, "GET", "/api/stats", H)[0] == 403)
        check("/api with the token alone: 403", req(port, "GET", "/api/stats", {"X-Api-Token": tok})[0] == 403)
        s, _, b, _ = req(port, "GET", "/api/stats", HT)
        check("/api with both: 200", s == 200, [s, b[:200]])
        check("a foreign Host: 403", req(port, "GET", "/", H, host="evil.example:%d" % port)[0] == 403)
        check("a Host naming the private port: 403 (not a way in)",
              req(port, "GET", "/", {"Cookie": ckp}, host="127.0.0.1:%d" % priv)[0] == 403)
        check("another port's cookie (the private one's name): 403",
              req(port, "GET", "/", {"Cookie": ckp})[0] == 403)
        check("a proxy header: 403", req(port, "GET", "/", dict(H, **{"X-Forwarded-For": "10.0.0.1"}))[0] == 403)
        s, _, b, _ = req(port, "POST", "/api/prefs", dict(HT, **{"Content-Type": "application/json",
                                                                  "Origin": "https://evil.example"}), b"{}")
        check("a cross-site write: refused", s == 403, [s, b[:200]])
        s, _, b, _ = req(port, "POST", "/api/prefs", dict(HT, **{"Content-Type": "application/json",
                                                                  "Origin": "http://127.0.0.1:%d" % port}),
                         b'{"turbo": true}')
        check("a write from the app's own origin: 200", s == 200, [s, b[:200]])

        # ---- the same answers both ways
        DH = {"Cookie": ckp}
        DHT = {"Cookie": ckp, "X-Api-Token": tok}
        pairs = [("the page", "GET", "/", H, DH), ("a 404", "GET", "/api/nope", HT, DHT),
                 ("a 403", "GET", "/api/stats", H, DH), ("/api/tiers", "GET", "/api/tiers", HT, DHT),
                 ("a sky clip that isn't there", "GET", "/static/sky/999.mov", H, DH)]
        for name, mth, path, hf, hd in pairs:
            a = req(port, mth, path, hf)
            d = req(priv, mth, path, hd)
            ha = [(k, v) for k, v in a[1] if k.lower() not in ("date",)]
            hd_ = [(k, v) for k, v in d[1] if k.lower() not in ("date",)]
            check("through the front == straight from the server: " + name,
                  a[0] == d[0] and a[2] == d[2] and ha == hd_, [a[0], d[0], len(a[2]), len(d[2]), ha, hd_])
        tf, td = [], []
        for _ in range(7):
            tf.append(req(port, "GET", "/", H)[3])
            td.append(req(priv, "GET", "/", DH)[3])
        ttfb = {"front_ms": round(statistics.median(tf) * 1000, 2), "direct_ms": round(statistics.median(td) * 1000, 2)}
        print("  / first byte, median of 7: through the front %.2f ms, straight %.2f ms"
              % (ttfb["front_ms"], ttfb["direct_ms"]))

        # ---- a streamed answer
        body = json.dumps({"model": "", "models": [], "tier": "Cloud Only", "auto_web": False,
                           "messages": [{"role": "user", "content": "say something about the front"}]}).encode()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        c.putrequest("POST", "/api/chat", skip_host=True, skip_accept_encoding=True)
        for k, v in (("Host", "127.0.0.1:%d" % port), ("Cookie", ck), ("X-Api-Token", tok),
                     ("Content-Type", "application/json"), ("Origin", "http://127.0.0.1:%d" % port),
                     ("Content-Length", str(len(body)))):
            c.putheader(k, v)
        c.endheaders(body)
        tc = time.perf_counter()
        r = c.getresponse()
        hdr = dict((k.lower(), v) for k, v in r.getheaders())
        chunks = []
        while True:
            x = r.read1(65536)
            if not x:
                break
            chunks.append((time.perf_counter() - tc, x))
        c.close()
        data = b"".join(x for _, x in chunks).decode("utf-8", "replace")
        tags = re.findall("\x00([A-Z0-9]+)[:\x00]", data)
        prose = re.sub("\x00[A-Z0-9]+(:.*?)?\x00", "", data, flags=re.S)
        want = stub_provider.ANSWER[:40]
        arrivals = [round(t, 3) for t, x in chunks]
        check("the streamed answer comes through: 200, the stand-in's words, X-Models",
              r.status == 200 and want in prose and "x-models" in hdr, [r.status, prose[-300:], hdr])
        check("its frames arrive one at a time (not buffered to the end)",
              len(chunks) >= 5 and arrivals[-1] - arrivals[0] > 0.5, arrivals[:20])
        print("  chat: %d reads over %.2f s, tags %s" % (len(chunks), arrivals[-1] if arrivals else 0,
                                                          sorted(set(tags))))
        leaks = [e for e in json.loads(req(srv.server_address[1], "GET", "/_log")[2])
                 if e["m"] == "CONNECT"]
        check("nothing tried to reach a real provider (no CONNECT through the proxy)", not leaks, leaks)

        out = {"t_note_s": round(t_note, 3), "ttfb": ttfb, "chat_reads": len(chunks),
               "chat_span_s": arrivals[-1] if arrivals else None, "tags": sorted(set(tags)),
               "front_connections_log": open(log.name).read().count("[shell-timing]")}
        print(json.dumps(out))
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        check("SIGTERM stops the front and its server; the port is free",
              p.returncode == 0 and not port_open(port), [p.returncode])
        srv.shutdown()
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print("kept", base)
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
