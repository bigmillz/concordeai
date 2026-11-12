"""The C++ engine's panel, end to end in the window (NOTES 6b614): a dev build
with the engine bundled (CONCORDE_ENGINE_BIN) runs as a dev copy; the probe
opens "Ask the C++ Engine..." and asks the way a person would; the panel's
streamed answer and its status line (first token, tokens/s) are read back,
and the panel is captured. Needs Llama 3.2 1B in the HF cache. One engine at
a time: run it only when no other engine work is going on.

    python3 cpp/tests/engine_panel_check.py <ConcordeAI.app> [--out dir]
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from window_e2e import Probe, check, RESULTS, port_open  # noqa: E402

VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")


def main():
    app = os.path.abspath(sys.argv[1])
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else tempfile.mkdtemp()
    os.makedirs(out, exist_ok=True)
    port = 9893
    if port_open(port):
        sys.exit("9893 is taken")
    base = tempfile.mkdtemp(prefix="cpp-eng-")
    home, probe = os.path.join(base, "home"), os.path.join(base, "probe")
    os.makedirs(home, mode=0o700)
    os.makedirs(probe)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="no-downloads,local-record", CONCORDE_PYTHON=VENV,
               CONCORDE_SHELL_TIMING="1", CONCORDE_SHELL_PROBE=probe)
    marks = []
    p = subprocess.Popen([os.path.join(app, "Contents", "MacOS", "ConcordeAI")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [marks.append(l) for l in p.stderr], daemon=True).start()
    pr = Probe(probe)
    try:
        t0 = time.time()
        while time.time() - t0 < 60 and not any("page loaded" in m for m in marks):
            time.sleep(0.1)
        q = "Name three uses of a lighthouse, in one sentence each."
        st = pr.run("//native:engine-ask " + q)
        check("the panel opens and the question goes to the C++ engine", st.get("visible") is True, st)
        t1 = time.time()
        while time.time() - t1 < 90:
            st = pr.run("//native:engine-state")
            if not st.get("running") and st.get("status", "").startswith("C++"):
                break
            time.sleep(0.3)
        print("  status:", st.get("status"))
        print("  answer:", (st.get("text") or "")[:300].replace("\n", " / "))
        check("the answer streams in from C++/MLX and the status line reports it",
              "lighthouse" in (st.get("text") or "").lower() and "tokens/s" in st.get("status", ""), st)
        import Quartz
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowOwnerPID") == p.pid and w.get("kCGWindowName") == "C++ Engine":
                shot = os.path.join(out, "engine-panel.png")
                subprocess.run(["screencapture", "-x", "-o", "-l", str(w["kCGWindowNumber"]), shot])
                print("  capture:", shot)
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(20)
        shutil.rmtree(base, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
