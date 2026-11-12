"""Launch A/B: the C++ shell against the Tauri shell (CPP_PLAN.md step 1),
from the shell-polish bench (6b548's sp/launch_bench.py), on this Mac.

Each bundle runs as a dev copy in a folder of its own (primed by one launch
that isn't counted), with the same server (CONCORDE_SERVER), the same venv,
the stand-in provider as its only key and its HTTPS proxy, local model
calls recorded instead of run, and no server pairing. Rounds alternate A, B,
A, B. Per round:
  * window on screen: the first on-screen window of the shell's pid
    (CGWindowList), from exec;
  * page painted: the first capture of that window that is not the night
    (screencapture -l, about every 100-150 ms, so +-0.1 s), and whether any
    capture was white;
  * the shell's own marks (CONCORDE_SHELL_TIMING=1; both shells print the
    same format), "page loaded" among them;
  * memory 20 s after launch: the shell, its sidecar, and the WebKit
    processes that appeared with it (WebContent, GPU, Networking).
Stops only the shell it started (SIGTERM) and waits for the port to close.
Test bundles must carry a bundle id of their own (WebKit's storage is keyed
on it): never the shipping com.millen.millenai.

    python3 launch_ab.py --app cpp=<ConcordeAI.app> --app tauri=<ConcordeAI.app> \
        [--rounds 4] [--port 9893] [--out results.jsonl]
"""
import argparse
import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
sys.path.insert(0, os.path.join(HERE, ".."))
import stub_provider  # noqa: E402
import Quartz  # noqa: E402  (pyobjc, in the venv)
from PIL import Image  # noqa: E402

WEBKIT = ("com.apple.WebKit.WebContent", "com.apple.WebKit.GPU", "com.apple.WebKit.Networking")


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


def win_of(pid):
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
            b = w.get("kCGWindowBounds")
            if b and b["Width"] > 400:
                return w["kCGWindowNumber"]
    return None


def classify(path):
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        return "none"
    raw = im.resize((96, 64)).tobytes()
    px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
    n = len(px)
    white = sum(1 for r, g, b in px if r > 235 and g > 235 and b > 235) / n
    night = sum(1 for r, g, b in px if r < 14 and g < 14 and b < 22) / n
    if white > 0.3:
        return "WHITE"
    if night > 0.97:
        return "night"
    return "page"


def rss_kb(pid):
    o = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(o) if o else 0


def webkit_pids():
    o = subprocess.run(["ps", "-ax", "-o", "pid=,comm="], capture_output=True, text=True).stdout
    out = {}
    for line in o.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and any(parts[1].endswith(w) or w in parts[1] for w in WEBKIT):
            out[int(parts[0])] = parts[1].rsplit("/", 1)[-1]
    return out


def env_for(home, port, surl):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="provider-stub=%s,no-downloads,local-record" % surl,
               CONCORDE_SERVER=os.path.join(REPO, "millenai.py"), CONCORDE_PYTHON=VENV,
               CONCORDE_SHELL_TIMING="1",
               HTTPS_PROXY=surl, HTTP_PROXY=surl, https_proxy=surl, http_proxy=surl,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", HF_HUB_OFFLINE="1")
    return env


def one(name, app, home, port, surl, cap, want_rss=True):
    exe = os.path.join(app, "Contents", "MacOS", "ConcordeAI")
    while port_open(port):
        time.sleep(0.2)
    before = set(webkit_pids())
    t0 = time.perf_counter()
    p = subprocess.Popen([exe], env=env_for(home, port, surl), stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True)
    marks = []
    threading.Thread(target=lambda: [marks.append(l.strip()) for l in p.stderr if "[shell-timing]" in l],
                     daemon=True).start()
    t_win = t_first = t_page = None
    kinds = []
    wid = None
    while time.perf_counter() - t0 < 30:
        if wid is None:
            wid = win_of(p.pid)
            if wid is not None:
                t_win = time.perf_counter() - t0
            else:
                time.sleep(0.005)
                continue
        subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), cap], capture_output=True)
        tc = time.perf_counter() - t0
        k = classify(cap)
        if k == "none":
            continue
        kinds.append(k)
        if t_first is None:
            t_first = tc
        if k == "page" and t_page is None:
            t_page = tc
        if t_page is not None and tc - t_page > 0.8:
            break
    rss = None
    if want_rss:
        time.sleep(max(0, 20 - (time.perf_counter() - t0)))
        kids = [int(x) for x in subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True,
                                                text=True).stdout.split()]
        wk = {pid: n for pid, n in webkit_pids().items() if pid not in before}
        rss = {"shell_mb": round(rss_kb(p.pid) / 1024, 1),
               "sidecar_mb": round(sum(rss_kb(k) for k in kids) / 1024, 1),
               "webkit_mb": round(sum(rss_kb(k) for k in wk) / 1024, 1),
               "webkit": sorted(set(wk.values()))}
        rss["total_mb"] = round(rss["shell_mb"] + rss["sidecar_mb"] + rss["webkit_mb"], 1)
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    t_end = time.time()
    while port_open(port) and time.time() - t_end < 15:
        time.sleep(0.2)
    loaded = next((float(m.split()[1]) / 1000 for m in marks if m.endswith("page loaded")), None)
    return {"shell": name, "window_on_screen_s": t_win and round(t_win, 3),
            "first_frame": kinds[0] if kinds else None, "first_frame_s": t_first and round(t_first, 3),
            "page_painted_s": t_page and round(t_page, 3), "white_frames": kinds.count("WHITE"),
            "frames": len(kinds), "page_loaded_mark_s": loaded, "rss": rss, "marks": marks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", action="append", required=True, help="name=path/to/App.app")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--port", type=int, default=9893)
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.port in (8889, 9897, 9901, 9902, 9903) or port_open(a.port):
        sys.exit("port %d is not free for a dev copy" % a.port)
    apps = [x.split("=", 1) for x in a.app]
    for _, path in apps:
        bid = plistlib.load(open(os.path.join(path, "Contents", "Info.plist"), "rb")).get("CFBundleIdentifier")
        if bid == "com.millen.millenai":
            sys.exit("%s carries the shipping bundle id; copy it and give it a test id first" % path)
    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]
    base = tempfile.mkdtemp(prefix="cpp-ab-")
    cap = os.path.join(base, "cap.png")
    homes = {}
    for name, path in apps:
        homes[name] = os.path.join(base, name)
        os.makedirs(homes[name], mode=0o700)
        # primed: the first launch of a fresh ad-hoc binary pays macOS's
        # one-time assessment and the folder's first-run work; not counted
        one(name, path, homes[name], a.port, surl, cap, want_rss=False)
    out = open(a.out, "w") if a.out else None
    try:
        for r in range(a.rounds):
            for name, path in apps:
                res = one(name, path, homes[name], a.port, surl, cap)
                res["round"] = r + 1
                print(json.dumps({k: v for k, v in res.items() if k != "marks"}), flush=True)
                if out:
                    out.write(json.dumps(res) + "\n")
                    out.flush()
                time.sleep(1.5)
    finally:
        srv.shutdown()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
