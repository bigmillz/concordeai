"""The first-run venv bootstrap on a fresh Mac, through the C++ window
(CPP_PLAN.md §2.3, NOTES 6b611; main.rs bootstrap_venv, 6b537).

A fresh test HOME (no ~/Library/Application Support/MillenAI/venv), a dev
copy inside it, no CONCORDE_PYTHON: the app finds no engine, shows "Setting
up ConcordeAI", runs the bundle's own launch.sh --bootstrap (the Python app's
first run: a venv, pywebview, ddgs, psutil, PyNaCl, mlx-lm, mlx-whisper, the
export libraries), then starts the server from that venv and loads the page.
Everything lands under the test HOME; the owner's venv is never touched.
pip's downloads go through the user's pip cache (PIP_CACHE_DIR) so a run
takes minutes, not a re-download. launch.sh posts one macOS notification
("First run: setting up the AI engine"), as a real first run does.

    python3 cpp/tests/bootstrap_proof.py <ConcordeAI.app> <work dir> [--port 9893]
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from window_e2e import Probe, check, RESULTS, port_open  # noqa: E402


def main():
    app, work = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 9893
    if port in (8889, 9901, 9902, 9903, 9897) or port_open(port):
        sys.exit("port %d is not free for a dev copy" % port)
    real_home = os.path.expanduser("~")
    fake = os.path.join(work, "home")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(fake)
    dev, probe_dir = os.path.join(fake, "devhome"), os.path.join(work, "probe")
    os.makedirs(dev, mode=0o700)
    os.makedirs(probe_dir)
    venv_py = os.path.join(fake, "Library", "Application Support", "MillenAI", "venv", "bin", "python3")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(HOME=fake, MILLENAI_DEV="1", MILLENAI_HOME=dev, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="no-downloads,local-record", CONCORDE_SHELL_TIMING="1",
               CONCORDE_SHELL_PROBE=probe_dir, PIP_CACHE_DIR=os.path.join(real_home, "Library", "Caches", "pip"),
               HF_HOME=os.path.join(fake, "hf"))
    marks = []
    t0 = time.time()
    p = subprocess.Popen([os.path.join(app, "Contents", "MacOS", "ConcordeAI")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [marks.append(l.strip()) for l in p.stderr if "[shell-timing]" in l],
                     daemon=True).start()
    pr = Probe(probe_dir)

    def has(word):
        return any(m.endswith(word) or word in m for m in marks)

    try:
        while time.time() - t0 < 30 and not has("bootstrap started"):
            time.sleep(0.2)
        time.sleep(1.0)
        page = pr.run("return (document.querySelector('h1') || {}).textContent || null;", timeout=15)
        check("no engine: the window says it is setting up, and the bundle's launch.sh runs",
              has("bootstrap started") and page == "Setting up ConcordeAI", [page, marks])
        while time.time() - t0 < 1500 and not has("bootstrap finished"):
            if p.poll() is not None:
                break
            time.sleep(2)
        took = time.time() - t0
        check("launch.sh --bootstrap exits 0 and leaves a venv in the (test) home",
              has("bootstrap finished (exit 0)") and os.path.exists(venv_py), [marks[-3:], took])
        print("  bootstrap took %.0f s" % took)
        while time.time() - t0 < 1600 and not has("page loaded"):
            time.sleep(0.5)
        check("then the server starts from that venv and the page loads", has("page loaded"), marks[-4:])
        kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        # the venv's python3 is a symlink that execs the base interpreter, so
        # ps shows that; what proves the venv is what the process has loaded
        files = subprocess.run(["lsof", "-p", kids[0]], capture_output=True, text=True).stdout if kids else ""
        venv_dir = os.path.dirname(os.path.dirname(venv_py))
        check("the server runs on the new venv (its site-packages loaded)",
              (venv_dir + "/lib/") in files, files[-400:])
        imp = subprocess.run([venv_py, "-c", "import webview, ddgs, psutil, mlx_lm, nacl; print('ok')"],
                             capture_output=True, text=True)
        check("the venv has the engine (webview, ddgs, psutil, mlx_lm, PyNaCl)", imp.stdout.strip() == "ok",
              imp.stderr[-300:])
        check("the owner's own venv was not touched (its python older than this run)",
              os.path.getmtime(os.path.join(real_home, "Library", "Application Support", "MillenAI", "venv",
                                            "bin", "python3")) < t0)
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        print("  marks:", [m.split("ms", 1)[1].strip() for m in marks])
        if "--keep" not in sys.argv:
            shutil.rmtree(work, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
