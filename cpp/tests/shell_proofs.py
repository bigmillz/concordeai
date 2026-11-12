"""Step 1's owed proofs in the C++ window (CPP_PLAN.md §2.3, NOTES 6b611).

The bundle runs as a dev copy from its OWN Resources (no CONCORDE_SERVER), so
the server is the bundled millenai.py and the helpers are the bundled
concorde-dictate and concorde-pdftext, as in a shipped app. The stand-in
provider is the only key and the HTTPS proxy; local model calls are recorded;
no pairing. Through the probe hook (a dev build's):

  pdf        a PDF made here posted by the page's own api() to /api/doc/pdf:
             read by the bundle's PDFKit helper, the text back
  dictation  `say` into 16 kHz mono PCM, fed by the page to /api/dictate
             (start, audio, end) as the page's recorder does: the system
             recognizer (the bundle's helper) writes the words back
  close      the close button's own path hides the window; the server and
             the page live on (an API call still answers)
  reopen     LaunchServices' reopen (`open` of the running bundle: what a
             Dock click sends) brings it back
  second     a second launch on the same folder exits 0 and brings the first
             window forward; nothing else starts
  restart    SIGKILL of the server: the veil, a new server, the page back;
             three more in a row: restarts 2 and 3, then the error page, the
             app still alive
  quit       SIGTERM quits the app: no server left, the port free

    python3 cpp/tests/shell_proofs.py <ConcordeAI.app> [--port 9893]

Stops only what it started.
"""
import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
sys.path.insert(0, HERE)
import stub_provider  # noqa: E402
from window_e2e import Probe, check, RESULTS, port_open  # noqa: E402

PHRASE = "The quick brown fox jumps over the lazy dog."
PDF_LINE = "Concorde reads this page on the Mac, through the C++ window."


def make_pdf(path):
    from reportlab.pdfgen import canvas  # the venv has it (launch.sh installs it)
    c = canvas.Canvas(path)
    c.drawString(72, 720, PDF_LINE)
    c.drawString(72, 700, "A second line, for the page-by-page reader.")
    c.showPage()
    c.drawString(72, 720, "Page two says hello as well.")
    c.save()


def make_pcm(tmp):
    aiff, wav = os.path.join(tmp, "say.aiff"), os.path.join(tmp, "say.wav")
    subprocess.run(["say", "-o", aiff, PHRASE], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, wav], check=True)
    data = open(wav, "rb").read()
    i = 12
    while i + 8 <= len(data):
        cid, n = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"data":
            return data[i + 8:i + 8 + n]
        i += 8 + n + (n & 1)
    raise SystemExit("no data chunk in the WAV")


def children(pid):
    return [int(x) for x in subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()]


def main():
    args = sys.argv[1:]
    if "--visible-windows-ok" not in args:
        # THE WINDOW IS REAL (6b618): it shows and takes focus; never on a Mac someone is using
        sys.exit("this proof opens a visible window that takes focus: run it only on a Mac nobody is using "
                 "(--visible-windows-ok)")
    app = os.path.abspath(args[0])
    exe = os.path.join(app, "Contents", "MacOS", "ConcordeAI")
    port = int(args[args.index("--port") + 1]) if "--port" in args else 9893
    if port in (8889, 9901, 9902, 9903, 9897) or port_open(port):
        sys.exit("port %d is not free for a dev copy" % port)
    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]
    base = tempfile.mkdtemp(prefix="cpp-proofs-")
    home, probe_dir = os.path.join(base, "home"), os.path.join(base, "probe")
    os.makedirs(home, mode=0o700)
    os.makedirs(probe_dir)
    with open(os.path.join(home, "prefs.json"), "w") as fh:
        json.dump({"turbo": False}, fh)
    pdf = os.path.join(base, "proof.pdf")
    make_pdf(pdf)
    pcm = make_pcm(base)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="provider-stub=%s,no-downloads,local-record" % surl,
               CONCORDE_PYTHON=VENV, CONCORDE_SHELL_TIMING="1", CONCORDE_SHELL_PROBE=probe_dir,
               HTTPS_PROXY=surl, HTTP_PROXY=surl, https_proxy=surl, http_proxy=surl,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", HF_HUB_OFFLINE="1")
    marks = []
    p = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [marks.append(l.strip()) for l in p.stderr if "[shell-timing]" in l],
                     daemon=True).start()
    pr = Probe(probe_dir)

    def wait_mark(word, count, t=60):
        t0 = time.time()
        while time.time() - t0 < t:
            if sum(1 for m in marks if m.endswith(word)) >= count:
                return True
            time.sleep(0.05)
        return False

    def note():
        try:
            return json.load(open(os.path.join(home, "run", "instance.json")))
        except (OSError, ValueError):
            return {}

    def api_ok():
        import urllib.request
        d = note()
        r = urllib.request.Request("http://127.0.0.1:%d/api/stats" % port, headers={
            "Cookie": "millen_key_%d=%s" % (port, d.get("key")), "X-Api-Token": d.get("token", "")})
        try:
            return urllib.request.urlopen(r, timeout=10).status == 200
        except Exception:
            return False

    try:
        check("the bundle's own server starts and the page loads", wait_mark("page loaded", 1))
        time.sleep(1.0)
        srv_cmd = subprocess.run(["ps", "-o", "command=", "-p", str(children(p.pid)[0])],
                                 capture_output=True, text=True).stdout
        check("the server is the bundle's Resources/millenai.py", app + "/Contents/Resources/millenai.py" in srv_cmd,
              srv_cmd)

        # ---- PDF
        b64 = base64.b64encode(open(pdf, "rb").read()).decode()
        r = pr.run("""
          const b = Uint8Array.from(atob(%s), c => c.charCodeAt(0));
          const r = await api('/api/doc/pdf', {method: 'POST', headers: {'Content-Type': 'application/pdf'}, body: b});
          return Object.assign({status: r.status}, await r.json());""" % json.dumps(b64), timeout=60)
        txt = json.dumps(r)
        check("PDF: the bundle's PDFKit helper reads a PDF posted by the page", r.get("status") == 200 and
              r.get("ok") is not False and PDF_LINE[:30] in txt and "Page two" in txt, txt[:500])
        print("  pdf:", {k: (v if k != "text" else (v or "")[:80]) for k, v in r.items() if k != "pages"})

        # ---- dictation
        b64 = base64.b64encode(pcm).decode()
        r = pr.run("""
          const pcm = Uint8Array.from(atob(%s), c => c.charCodeAt(0));
          const j = async (u, body) => (await api(u, {method: 'POST', body: body || new Uint8Array(0),
                                        headers: {'Content-Type': 'application/octet-stream'}})).json();
          const w = await j('/api/dictate/warm');
          const s = await j('/api/dictate/start');
          if (!s.ok) return {warm: w, start: s};
          const step = 16000;  // half a second of 16 kHz int16 a call, as the page's recorder pumps it
          let partial = '', calls = 0;
          for (let i = 0; i + step < pcm.length; i += step) {
            const a = await j('/api/dictate/audio?sid=' + s.sid, pcm.slice(i, i + step)); calls++;
            if (a.partial) partial = a.partial;
          }
          const rest = pcm.slice(Math.floor(pcm.length / step) * step);
          const t0 = performance.now();
          const e = await j('/api/dictate/end?sid=' + s.sid, rest);
          return {warm: w, start: s, calls, partial, end: e, end_ms: Math.round(performance.now() - t0)};"""
                   % json.dumps(b64), timeout=90)
        said = ((r.get("end") or {}).get("text") or "").lower()
        check("dictation: the bundle's system recognizer writes the words back through the window",
              (r.get("start") or {}).get("engine") == "system" and "quick brown fox" in said, r)
        print("  dictation:", json.dumps({"engine": (r.get("start") or {}).get("engine"), "partial": r.get("partial"),
                                          "final": (r.get("end") or {}).get("text"), "end_ms": r.get("end_ms")}))

        # ---- close, reopen, second launch
        st = pr.run("//native:close")
        time.sleep(0.6)
        st = pr.run("//native:state")
        check("close hides the window; the server and the page live on",
              st.get("visible") is False and api_ok() and p.poll() is None, st)
        subprocess.run(["open", app])
        t0 = time.time()
        while time.time() - t0 < 10 and not pr.run("//native:state").get("visible"):
            time.sleep(0.3)
        st = pr.run("//native:state")
        check("LaunchServices' reopen (a Dock click) brings it back", st.get("visible") is True, st)
        pr.run("//native:close")
        time.sleep(0.6)
        kids_before = children(p.pid)
        t0 = time.time()
        second = subprocess.run([exe], env=env, capture_output=True, text=True, timeout=30)
        took = time.time() - t0
        time.sleep(0.8)
        st = pr.run("//native:state")
        check("a second launch on the same folder exits 0 and brings the first window forward",
              second.returncode == 0 and st.get("visible") is True and children(p.pid) == kids_before
              and p.poll() is None, [second.returncode, st, took])
        print("  second launch exited in %.2f s" % took)

        # ---- crash restart
        def kill_server_and_wait(n_ready):
            k = children(p.pid)
            os.kill(k[0], signal.SIGKILL)
            veil = None
            t0 = time.time()
            while time.time() - t0 < 3 and veil is None:
                v = pr.run("return !!document.getElementById('shell-reconnect');", timeout=5)
                if v is True:
                    veil = round(time.time() - t0, 2)
                time.sleep(0.05)
            back = wait_mark("sidecar ready (notes read)", n_ready, 30)
            return k[0], veil, back, time.time() - t0

        old, veil, back, secs = kill_server_and_wait(2)
        wait_mark("page loaded", 1)
        time.sleep(1.5)
        st = pr.run("const t = await window.pywebview.api.api_token(); return {href: location.href, "
                    "tok: (t || '').length, veil: !!document.getElementById('shell-reconnect')};")
        check("a killed server: the veil, a new server, the page back on the same origin, the token answers",
              veil is not None and back and st.get("href") == "http://127.0.0.1:%d/" % port and st.get("tok", 0) >= 32
              and not st.get("veil") and api_ok() and children(p.pid) and children(p.pid)[0] != old,
              [veil, back, st])
        print("  restart 1: veil after %s s, page back in %.1f s" % (veil, secs))
        for n in (3, 4):
            kill_server_and_wait(n)
            time.sleep(1.0)
        k = children(p.pid)
        os.kill(k[0], signal.SIGKILL)
        time.sleep(1.5)
        e = pr.run("return {h1: (document.querySelector('h1') || {}).textContent || null, "
                   "p: ((document.querySelector('p') || {}).textContent || '').slice(0, 200)};")
        check("three restarts in a row, then the plain error page; the app stays up, no server left",
              "stopped" in (e.get("h1") or "") and "3 restarts" in (e.get("p") or "") and p.poll() is None
              and not children(p.pid), [e, children(p.pid)])
        print("  after the fourth kill:", e)
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
        check("SIGTERM quits the app: exit 0, the port free", p.returncode == 0 and not port_open(port), p.returncode)
        srv.shutdown()
        shutil.rmtree(base, ignore_errors=True)
    print("  marks:", [m.split("ms", 1)[1].strip() for m in marks])
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
