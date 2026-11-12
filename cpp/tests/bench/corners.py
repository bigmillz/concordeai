"""The window's corners, each shell against the other (NOTES 6b617): each runs as
a dev copy in a test home with the same server, its window is captured with
its shadow (screencapture -l), and the four corners are cut out and laid side
by side, 4x, in one image; the corner pixels are also checked for anything
square poking past the rounding (an opaque pixel where the rounded corner
should leave it see-through).

    python3 corners.py --app cpp=<app> --app tauri=<app> --server <millenai.py> --out <dir>
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image
import Quartz

VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
C = 64  # corner size in pixels (Retina)


def capture(name, app, server, port, out):
    exe = os.path.join(app, "Contents", "MacOS", "ConcordeAI")
    base = tempfile.mkdtemp(prefix="cpp-corner-")
    home = os.path.join(base, "home")
    os.makedirs(home, mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="no-downloads,local-record", CONCORDE_SERVER=server, CONCORDE_PYTHON=VENV,
               CONCORDE_SHELL_TIMING="1")
    marks = []
    p = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [marks.append(l) for l in p.stderr], daemon=True).start()
    shot = os.path.join(out, "%s-window.png" % name)
    try:
        t0 = time.time()
        while time.time() - t0 < 60 and not any("page loaded" in m for m in marks):
            time.sleep(0.1)
        time.sleep(3)
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of (first process whose unix id is %d) to true' % p.pid], capture_output=True)
        time.sleep(1)
        wid = None
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowOwnerPID") == p.pid and w.get("kCGWindowLayer") == 0 and w["kCGWindowBounds"]["Width"] > 400:
                wid = w["kCGWindowNumber"]
        if wid:
            subprocess.run(["screencapture", "-x", "-l", str(wid), shot], capture_output=True)  # with the shadow
            subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), shot.replace(".png", "-noshadow.png")],
                           capture_output=True)
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
        shutil.rmtree(base, ignore_errors=True)
    return shot.replace(".png", "-noshadow.png") if os.path.exists(shot) else None


def corners(img):
    w, h = img.size
    return [img.crop(b) for b in ((0, 0, C, C), (w - C, 0, w, C), (0, h - C, C, h), (w - C, h - C, w, h))]


def leak(img):
    """Opaque pixels in the outermost 3x3 of each corner: a square corner."""
    w, h = img.size
    px = img.load()
    out = []
    for (x0, y0) in ((0, 0), (w - 3, 0), (0, h - 3), (w - 3, h - 3)):
        out.append(sum(1 for x in range(x0, x0 + 3) for y in range(y0, y0 + 3) if px[x, y][3] > 200))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", action="append", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=9893)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rows = []
    for x in a.app:
        name, app = x.split("=", 1)
        shot = capture(name, app, a.server, a.port, a.out)
        if not shot:
            print(name, "no window captured")
            continue
        img = Image.open(shot).convert("RGBA")
        print(name, "window", img.size, "opaque pixels in each corner's outer 3x3 (TL, TR, BL, BR):", leak(img))
        rows.append((name, corners(img)))
    if rows:
        S = 4
        sheet = Image.new("RGBA", (4 * C * S + 5 * 8, len(rows) * (C * S + 8) + 8), (128, 128, 128, 255))
        for r, (name, cs) in enumerate(rows):
            for i, c in enumerate(cs):
                sheet.paste(c.resize((C * S, C * S), Image.NEAREST), (8 + i * (C * S + 8), 8 + r * (C * S + 8)), c.resize((C * S, C * S), Image.NEAREST))
        path = os.path.join(a.out, "corners.png")
        sheet.save(path)
        print("sheet:", path, "rows:", [n for n, _ in rows])


if __name__ == "__main__":
    main()
