"""How fluid each shell feels, measured the same way in both (NOTES 6b617).

Each shell runs as a dev copy in a test home (the stand-in provider, local
model calls recorded, no pairing, the same millenai.py for both), its window
on screen and in front, driven through its probe hook (the C++ shell's
CONCORDE_SHELL_PROBE, and the Tauri shell's in a debug build: both run a
script in the page). Measured in the page:

  frames     requestAnimationFrame over 240 frames on the home: the rate, the
             95th-percentile frame time, frames over 1.5x the median
  stream     a streamed answer (the stand-in sends a piece every 20 ms):
             the gaps between pieces as the page reads them
  request    60 sequential api() calls to a cheap route: ms each, median, p95
  input      real key presses posted to the shell's process (CGEventPostToPid):
             event to the next frame, median and p95 (skipped if the OS won't
             let this process post events)

    python3 fluid_ab.py --app cpp=<app> [--app cpp60=<app>:RENDER60] --app tauri=<app> --server <millenai.py>
"""
import argparse
import json
import os
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
sys.path.insert(0, os.path.join(HERE, ".."))
import stub_provider  # noqa: E402

VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")


def port_open(p):
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Probe:
    def __init__(self, d, kind):
        self.d, self.n, self.kind = d, 0, kind

    def ev(self, expr, timeout=30):
        """The value of a JS expression (a string comes back as a string)."""
        js = ("return (%s);" % expr) if self.kind == "cpp" else "(%s)" % expr
        self.n += 1
        out = os.path.join(self.d, "out-%d.json" % self.n)
        tmp = os.path.join(self.d, ".c%d" % self.n)
        open(tmp, "w").write(js)
        os.rename(tmp, os.path.join(self.d, "cmd-%d.js" % self.n))
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.exists(out):
                time.sleep(0.03)
                try:
                    v = json.load(open(out))
                    return v
                except ValueError:
                    pass
            time.sleep(0.03)
        return None

    def run(self, setup, result="JSON.stringify(window.__m||null)", timeout=60):
        """Start something that leaves its result in window.__m, then poll for it."""
        self.ev("(window.__m=null, %s, 1)" % setup)
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = self.ev(result)
            if isinstance(v, str) and v != "null":
                return json.loads(v)
            time.sleep(0.25)
        return None


FRAMES = """(function(){var ts=[];function f(t){ts.push(t);if(ts.length<241)requestAnimationFrame(f);
 else window.__m={ts:ts};}requestAnimationFrame(f);})()"""

STREAM = """(function(){var body=JSON.stringify({model:'',models:[],tier:'Cloud Only',auto_web:false,
 messages:[{role:'user',content:'stream test'}]});var at=[];var t0=performance.now();
 api('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:body}).then(function(r){
 var rd=r.body.getReader();function go(){return rd.read().then(function(x){if(x.done){window.__m={at:at};return;}
 at.push(performance.now()-t0);return go();});}return go();});})()"""

REQ = """(function(){var ms=[];var i=0;function one(){var t=performance.now();
 return api('/api/remote/classify?cmd=ls').then(function(r){return r.text();}).then(function(){ms.push(performance.now()-t);
 if(++i<60)return one();window.__m={ms:ms};});}one();})()"""

GPU = """JSON.stringify((function(){var c=document.createElement('canvas');var g=c.getContext('webgl2');
 var g1=g||c.getContext('webgl');var d=g1&&g1.getExtension('WEBGL_debug_renderer_info');
 return {webgl2:!!g,webgl:!!g1,renderer:d?g1.getParameter(d.UNMASKED_RENDERER_WEBGL):(g1?g1.getParameter(g1.RENDERER):null),
 vendor:d?g1.getParameter(d.UNMASKED_VENDOR_WEBGL):null,webgpu:!!navigator.gpu,
 maxTex:g1?g1.getParameter(g1.MAX_TEXTURE_SIZE):0,dpr:devicePixelRatio,
 fx:(document.documentElement.className.match(/hz-(full|bal|calm)/)||[null])[0]};})())"""

KEYS = """(function(){var ms=[];var inp=document.getElementById('input');inp.focus();inp.value='';
 window.__k=ms;inp.addEventListener('keydown',function(e){var t=e.timeStamp;requestAnimationFrame(function(f){
 ms.push(f-t);});},true);})()"""


def frames_stats(ts):
    iv = [b - a for a, b in zip(ts, ts[1:])]
    med = statistics.median(iv)
    iv_s = sorted(iv)
    return {"fps": round(1000 / med, 1), "median_ms": round(med, 2), "p95_ms": round(iv_s[int(len(iv) * 0.95)], 2),
            "long_frames": sum(1 for x in iv if x > 1.5 * med), "frames": len(iv)}


def post_keys(pid, n=20):
    try:
        import Quartz
    except Exception:
        return False
    for i in range(n):
        for down in (True, False):
            e = Quartz.CGEventCreateKeyboardEvent(None, 0, down)  # 'a'
            Quartz.CGEventPostToPid(pid, e)
        time.sleep(0.12)
    return True


def measure(name, app, server, env_extra, surl, port):
    exe = os.path.join(app, "Contents", "MacOS", "ConcordeAI")
    base = tempfile.mkdtemp(prefix="cpp-fluid-")
    home, probe = os.path.join(base, "home"), os.path.join(base, "probe")
    os.makedirs(home, mode=0o700)
    os.makedirs(probe)
    with open(os.path.join(home, "cloud.json"), "w") as fh:
        json.dump({"active": "groq", "providers": {"groq": {
            "name": "Groq", "base": surl + "/api.groq.com/openai/v1", "key": "gsk_standin_fluid",
            "model": "qwen/qwen3.8-27b", "models": ["qwen/qwen3.8-27b"], "status": "ok"}}}, fh)
    with open(os.path.join(home, "prefs.json"), "w") as fh:
        json.dump({"turbo": True}, fh)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="provider-stub=%s,no-downloads,local-record" % surl,
               CONCORDE_SERVER=server, CONCORDE_PYTHON=VENV, CONCORDE_SHELL_PROBE=probe, CONCORDE_SHELL_TIMING="1",
               HTTPS_PROXY=surl, https_proxy=surl, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               HF_HUB_OFFLINE="1")
    env.update(env_extra)
    marks = []
    p = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [marks.append(l) for l in p.stderr], daemon=True).start()
    pr = Probe(probe, "tauri" if name.startswith("tauri") else "cpp")
    res = {"shell": name}
    try:
        t0 = time.time()
        while time.time() - t0 < 60 and not any("page loaded" in m for m in marks):
            time.sleep(0.1)
        time.sleep(2.5)
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of (first process whose unix id is %d) to true' % p.pid],
                       capture_output=True)
        time.sleep(0.8)
        vis = pr.ev("document.visibilityState")
        res["visible"] = vis
        g = pr.ev(GPU)
        res["gpu"] = json.loads(g) if isinstance(g, str) else g
        fr = pr.run(FRAMES)
        res["frames"] = frames_stats(fr["ts"]) if fr else None
        st = pr.run(STREAM, timeout=90)
        if st:
            gaps = [b - a for a, b in zip(st["at"], st["at"][1:])]
            res["stream"] = {"pieces": len(st["at"]), "gap_median_ms": round(statistics.median(gaps), 1) if gaps else None,
                             "gap_p95_ms": round(sorted(gaps)[int(len(gaps) * 0.95)], 1) if gaps else None,
                             "first_ms": round(st["at"][0], 1)}
        rq = pr.run(REQ)
        if rq:
            ms = sorted(rq["ms"][5:])
            res["request"] = {"median_ms": round(statistics.median(ms), 2), "p95_ms": round(ms[int(len(ms) * 0.95)], 2)}
        pr.ev("(%s, 1)" % KEYS)
        time.sleep(0.3)
        if post_keys(p.pid):
            time.sleep(0.6)
            k = pr.ev("JSON.stringify(window.__k||[])")
            ks = json.loads(k) if isinstance(k, str) else []
            if ks:
                ks = sorted(ks)
                res["input"] = {"n": len(ks), "median_ms": round(statistics.median(ks), 2),
                                "p95_ms": round(ks[int(len(ks) * 0.95)], 2)}
            else:
                res["input"] = "no key events reached the page (posting not allowed?)"
        res["rate_mark"] = any("full rate" in m for m in marks)
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
        t_end = time.time()
        while port_open(port) and time.time() - t_end < 15:
            time.sleep(0.2)
        shutil.rmtree(base, ignore_errors=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", action="append", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--port", type=int, default=9893)
    ap.add_argument("--out")
    a = ap.parse_args()
    stub_provider.GAP = 0.02
    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]
    apps = []
    for x in a.app:
        name, rest = x.split("=", 1)
        extra = {}
        if rest.endswith(":RENDER60"):
            rest, extra = rest[:-len(":RENDER60")], {"CONCORDE_RENDER_60": "1"}
        apps.append((name, rest, extra))
    out = []
    for r in range(a.rounds):
        for name, app, extra in apps:
            res = measure(name, app, a.server, extra, surl, a.port)
            res["round"] = r + 1
            print(json.dumps(res), flush=True)
            out.append(res)
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1)
    srv.shutdown()


if __name__ == "__main__":
    main()
