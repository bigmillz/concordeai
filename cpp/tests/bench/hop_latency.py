"""The front's cost per request (NOTES 6b617): the same cheap route, 300 times
each way, through concorde-front and straight to the Python server behind it
(a new connection per request, as the page's are: the server speaks HTTP/1.0
and closes). Also a streamed answer's inter-chunk gaps both ways, the stand-in
sending a piece every 20 ms.

    python3 hop_latency.py <concorde-front> <millenai.py> [--port 9893]
"""
import http.client
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import stub_provider  # noqa: E402

VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")


def one(port, path, cookie, tok):
    t = time.perf_counter()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path, headers={"Host": "127.0.0.1:%d" % port, "Cookie": cookie, "X-Api-Token": tok})
    r = c.getresponse()
    r.read()
    c.close()
    return (time.perf_counter() - t) * 1000


def stream(port, cookie, tok):
    body = json.dumps({"model": "", "models": [], "tier": "Cloud Only", "auto_web": False,
                       "messages": [{"role": "user", "content": "gaps"}]}).encode()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    c.request("POST", "/api/chat", body, {"Host": "127.0.0.1:%d" % port, "Cookie": cookie, "X-Api-Token": tok,
                                          "Content-Type": "application/json"})
    r = c.getresponse()
    at = []
    t0 = time.perf_counter()
    while True:
        x = r.read1(65536)
        if not x:
            break
        at.append((time.perf_counter() - t0) * 1000)
    c.close()
    return [b - a for a, b in zip(at, at[1:])]


def main():
    front, server = sys.argv[1], sys.argv[2]
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9893
    stub_provider.GAP = 0.02
    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]
    home = tempfile.mkdtemp(prefix="cpp-hop-")
    json.dump({"active": "groq", "providers": {"groq": {"name": "Groq", "base": surl + "/api.groq.com/openai/v1",
              "key": "gsk_standin_hop", "model": "qwen/qwen3.8-27b", "models": ["qwen/qwen3.8-27b"], "status": "ok"}}},
              open(os.path.join(home, "cloud.json"), "w"))
    json.dump({"turbo": True}, open(os.path.join(home, "prefs.json"), "w"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_NOWINDOW="1", MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="provider-stub=%s,no-downloads,local-record" % surl,
               CONCORDE_SERVER=server, CONCORDE_PYTHON=VENV, HTTPS_PROXY=surl, https_proxy=surl,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    if os.environ.get("CONCORDE_FRONT_TRACE"):
        env["CONCORDE_FRONT_TRACE"] = "1"
    trace = open(os.environ.get("HOP_TRACE_OUT", os.devnull), "w")
    p = subprocess.Popen([front], env=env, stdout=subprocess.DEVNULL, stderr=trace)
    try:
        for _ in range(600):
            try:
                d = json.load(open(os.path.join(home, "run", "instance.json")))
                if d["pid"] == p.pid:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        kid = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()[0]
        lsof = subprocess.run(["lsof", "-a", "-nP", "-p", kid, "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
        priv = int(re.search(r"127\.0\.0\.1:(\d+) \(LISTEN\)", lsof).group(1))
        out = {}
        # alternating, one request each way in turn, after a settling spell
        # for the server's own start-up work, so both see the same server
        time.sleep(float(os.environ.get("HOP_SETTLE", "20")))
        ways = (("front", port), ("direct", priv))
        ms = {n: [] for n, _ in ways}
        for i in range(400):
            for name, pt in (ways if i % 2 else ways[::-1]):
                v = one(pt, "/api/remote/classify?cmd=ls", "millen_key_%d=%s" % (pt, d["key"]), d["token"])
                if i >= 20:
                    ms[name].append(v)
        for name, pt in ways:
            m = sorted(ms[name])
            gaps = stream(pt, "millen_key_%d=%s" % (pt, d["key"]), d["token"])
            g = sorted(gaps)
            out[name] = {"request_median_ms": round(statistics.median(m), 3), "request_p95_ms": round(m[int(len(m) * .95)], 3),
                         "request_p99_ms": round(m[int(len(m) * .99)], 3), "stream_reads": len(gaps) + 1,
                         "gap_median_ms": round(statistics.median(g), 2) if g else None,
                         "gap_p95_ms": round(g[int(len(g) * .95)], 2) if g else None}
        print(json.dumps(out, indent=1))
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(20)
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
