"""The C++ window end to end (CPP_PLAN.md step 1), on this Mac.

Launches the bundle's binary as a dev copy (MILLENAI_DEV=1, a fresh
MILLENAI_HOME, a dev port; no server pairing, the stand-in provider as the
only cloud key and as the HTTPS proxy, local model calls recorded instead of
run), then through the probe hook (CONCORDE_SHELL_PROBE, a dev build's):

  * the page is the app's home on the front's origin, the bridge is the
    page's pywebview shim, the token answers (only its length is read),
    html.tl is set (the traffic lights' zone);
  * the page's own api() streams /api/chat through the front: the frames
    arrive one at a time;
  * the composer: a question typed and sent the way a person does, the
    answer read back from the conversation;
  * the traffic lights' frames, a capture of the window;
  * memory 20 s after the page, then SIGTERM: the app and its server stop
    and the port is free.

    python3 cpp/tests/window_e2e.py <ConcordeAI.app> [--port 9893] [--out dir]

Stops only what it started.
"""
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
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
sys.path.insert(0, HERE)
import stub_provider  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  ok  " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)[:600]), flush=True)


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


class Probe:
    def __init__(self, d):
        self.d, self.n = d, 0

    def run(self, js, timeout=60):
        self.n += 1
        out = os.path.join(self.d, "out-%d.json" % self.n)
        tmp = os.path.join(self.d, ".cmd-%d.js" % self.n)
        with open(tmp, "w") as fh:
            fh.write(js)
        os.rename(tmp, os.path.join(self.d, "cmd-%d.js" % self.n))
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.exists(out):
                time.sleep(0.05)
                try:
                    return json.load(open(out))
                except ValueError:
                    pass
            time.sleep(0.05)
        return {"error": "probe timeout"}


def rss_mb(pid):
    o = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return round(int(o) / 1024, 1) if o else 0.0


def main():
    args = sys.argv[1:]
    app = os.path.abspath(args[0])
    exe = os.path.join(app, "Contents", "MacOS", "ConcordeAI")
    port = int(args[args.index("--port") + 1]) if "--port" in args else 9893
    outdir = args[args.index("--out") + 1] if "--out" in args else tempfile.mkdtemp(prefix="cpp-win-out-")
    os.makedirs(outdir, exist_ok=True)
    if port in (8889, 9901, 9902, 9903, 9897) or port_open(port):
        sys.exit("port %d is not free for a dev copy" % port)

    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]
    base = tempfile.mkdtemp(prefix="cpp-win-")
    home = os.path.join(base, "home")
    probe_dir = os.path.join(base, "probe")
    os.makedirs(home, mode=0o700)
    os.makedirs(probe_dir)
    with open(os.path.join(home, "cloud.json"), "w") as fh:
        json.dump({"active": "groq", "providers": {"groq": {
            "name": "Groq", "base": surl + "/api.groq.com/openai/v1", "key": "gsk_standin_for_the_cpp_window",
            "model": "qwen/qwen3.8-27b", "models": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
            "status": "ok"}}}, fh)
    with open(os.path.join(home, "prefs.json"), "w") as fh:
        json.dump({"turbo": True}, fh)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="provider-stub=%s,no-downloads,local-record" % surl,
               CONCORDE_SERVER=os.path.join(REPO, "millenai.py"), CONCORDE_PYTHON=VENV,
               CONCORDE_SHELL_TIMING="1", CONCORDE_SHELL_PROBE=probe_dir,
               HTTPS_PROXY=surl, HTTP_PROXY=surl, https_proxy=surl, http_proxy=surl,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", HF_HUB_OFFLINE="1")
    marks = []
    t0 = time.perf_counter()
    p = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def pump():
        for line in p.stderr:
            if "[shell-timing]" in line:
                marks.append(line.strip())

    threading.Thread(target=pump, daemon=True).start()
    pr = Probe(probe_dir)
    try:
        while time.perf_counter() - t0 < 60 and not any("page loaded" in m for m in marks):
            if p.poll() is not None:
                sys.exit("the app exited %s" % p.returncode)
            time.sleep(0.05)
        check("the page loaded in the window", any("page loaded" in m for m in marks), marks)
        time.sleep(1.5)
        st = pr.run("""
          const t = await window.pywebview.api.api_token();
          return {href: location.href, title: document.title, platform: window.pywebview.platform,
                  tokLen: (typeof t === 'string') ? t.length : -1, tl: document.documentElement.classList.contains('tl'),
                  composer: !!document.getElementById('input'), header: !!document.getElementById('hz-top'),
                  paint: performance.getEntriesByType('paint').map(e => [e.name, Math.round(e.startTime)]),
                  nav: (performance.getEntriesByType('navigation')[0] || {}).type || null,
                  cookieVisible: document.cookie.indexOf('millen_key_') >= 0};""")
        check("the home on the front's own origin", st.get("href") == "http://127.0.0.1:%d/" % port, st)
        check("the bridge is the page's pywebview shim, and the token answers",
              st.get("platform") == "concorde-cpp" and st.get("tokLen", 0) >= 32, st)
        check("the page drew its composer and header, html.tl set", st.get("composer") and st.get("header") and st.get("tl"), st)
        check("the launch cookie is HttpOnly (the page can't read it)", st.get("cookieVisible") is False, st)
        print("  page:", json.dumps({k: st.get(k) for k in ("title", "paint", "nav")}))

        chat = pr.run("""
          const body = JSON.stringify({model: "", models: [], tier: "Cloud Only", auto_web: false,
            messages: [{role: "user", content: "say something about the window"}]});
          const t0 = performance.now();
          const r = await api('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body});
          const rd = r.body.getReader(); const dec = new TextDecoder(); let txt = '', at = [];
          for (;;) { const {done, value} = await rd.read(); if (done) break;
            at.push(Math.round(performance.now() - t0)); txt += dec.decode(value, {stream: true}); }
          return {status: r.status, models: r.headers.get('X-Models'), reads: at.length, at: at.slice(0, 40),
                  prose: txt.replace(/\\u0000[A-Z0-9]+(:[\\s\\S]*?)?\\u0000/g, '').slice(0, 400)};""", timeout=180)
        check("the page's api() streams /api/chat through the front",
              chat.get("status") == 200 and stub_provider.ANSWER[:40] in (chat.get("prose") or ""), chat)
        at = chat.get("at") or [0]
        check("its frames arrive in the page one at a time", chat.get("reads", 0) >= 5 and at[-1] - at[0] > 500, chat)
        print("  chat via api(): %s reads, first %s ms, last %s ms" % (chat.get("reads"), at[0], at[-1]))

        ui = pr.run("""
          const inp = document.getElementById('input');
          inp.focus(); inp.value = 'Say hello from the C++ window.';
          inp.dispatchEvent(new Event('input', {bubbles: true}));
          const t0 = performance.now();
          const errs = [];
          addEventListener('error', e => errs.push(String(e.message)));
          addEventListener('unhandledrejection', e => errs.push('rejection: ' + String(e.reason)));
          const p = send();
          let settled = false; Promise.resolve(p).then(() => { settled = true; }, e => { settled = true; errs.push('send: ' + e); });
          await new Promise(r => setTimeout(r, 300));
          while (!settled && performance.now() - t0 < 60000) await new Promise(r => setTimeout(r, 100));
          const ais = [...document.querySelectorAll('.msg.ai')];
          const last = ais[ais.length - 1];
          return {ms: Math.round(performance.now() - t0), n: ais.length, settled, errs,
                  generating: generating, gen: document.body.classList.contains('gen'),
                  text: last ? last.innerText.slice(0, 500) : null,
                  user: [...document.querySelectorAll('.msg.user')].map(e => e.innerText.slice(0, 80))};""",
                     timeout=180)
        check("the composer: a question sent the way a person does, an answer in the conversation",
              ui.get("n", 0) >= 1 and (ui.get("text") or "").strip() and
              any("C++ window" in u for u in ui.get("user") or []), ui)
        print("  ui answer (%s ms, settled %s, generating %s, errors %s): %r" % (ui.get("ms"), ui.get("settled"), ui.get("generating"), ui.get("errs"), (ui.get("text") or "")[:120]))

        lights = pr.run("//native:lights")
        c = lights.get("close") or [0, 0, 0, 0]
        z = lights.get("zoom") or [0, 0, 0, 0]
        cy = c[1] + c[3] / 2
        check("the traffic lights on the header row: centre y 36, close at x 16 (Tauri's TL_X/TL_Y)",
              abs(cy - 36) <= 1.5 and abs(c[0] - 16) <= 0.5, lights)
        print("  lights: close %s, zoom %s, centre y %.1f, green right edge %.1f" % (c, z, cy, z[0] + z[2]))

        wid = None
        import Quartz  # noqa: E402  (the venv has pyobjc)
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowOwnerPID") == p.pid and w.get("kCGWindowLayer") == 0:
                wid = w["kCGWindowNumber"]
        if wid:
            shot = os.path.join(outdir, "cpp-window.png")
            subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), shot], capture_output=True)
            print("  capture:", shot)
        time.sleep(max(0, 20 - (time.perf_counter() - t0)))
        kids = [int(x) for x in subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True,
                                                text=True).stdout.split()]
        mem = {"shell_mb": rss_mb(p.pid), "sidecar_mb": round(sum(rss_mb(k) for k in kids), 1)}
        print("  memory:", json.dumps(mem))
        print("  marks:")
        for m in marks:
            print("   ", m)
        with open(os.path.join(outdir, "window_e2e.json"), "w") as fh:
            json.dump({"state": st, "chat": chat, "ui": ui, "lights": lights, "memory": mem, "marks": marks}, fh,
                      indent=1)
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        t_end = time.time()
        while port_open(port) and time.time() - t_end < 15:
            time.sleep(0.2)
        check("SIGTERM quits the app and its server; the port is free", p.returncode == 0 and not port_open(port),
              p.returncode)
        srv.shutdown()
        shutil.rmtree(base, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
