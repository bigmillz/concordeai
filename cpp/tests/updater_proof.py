"""The in-app updater end to end on the C++ bundle (CPP_PLAN.md §2.3, NOTES 6b611),
from the Tauri shell's proof (6b537/6b548's updproof2/proof.py).

An installed C++ bundle (this build, APP_BUILD N, a dev copy in a temp folder,
never /Applications or the real data) is offered build N+1 by a stand-in
GitHub, downloads it, its server exits 0, the C++ shell quits on that, the
updater's helper (millenai.py's _SWAP_SCRIPT, unchanged) swaps the bundle and
`open -n` relaunches it; the new bundle carries LSEnvironment for the same
dev folder, so the relaunch is a dev copy too. Ports 9906 (app), 9908 (feed).

    python3 cpp/tests/updater_proof.py <built ConcordeAI.app> <work dir>
"""
import http.server
import json
import os
import plistlib
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

SRC_APP, S = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
APPS, FILES, HOME, B2 = S + "/apps", S + "/files", S + "/home", S + "/b2"
APP = APPS + "/ConcordeAI.app"
ENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shell", "src-tauri", "entitlements.plist")
R = []


def check(n, ok, d=""):
    R.append((n, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + n + ("" if ok else "  -- " + str(d)[:300]), flush=True)


def sh(*a):
    return subprocess.run(a, check=True, capture_output=True, text=True).stdout


for port in (9906, 9908):
    if subprocess.run(["lsof", "-tnP", "-iTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True).stdout:
        sys.exit("port %d is taken" % port)
for d in (APPS, FILES, HOME, B2):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
sh("ditto", SRC_APP, APP)
src = open(APP + "/Contents/Resources/millenai.py").read()
N = int(re.search(r"^APP_BUILD = (\d+)", src, re.M).group(1))
NEW = N + 1
# the build-(N+1) bundle: plist and APP_BUILD stamped, LSEnvironment for the dev folder, re-signed, in a DMG
nb = B2 + "/ConcordeAI.app"
sh("ditto", SRC_APP, nb)
pl = nb + "/Contents/Info.plist"
sh("/usr/libexec/PlistBuddy", "-c", "Set :CFBundleVersion %d" % NEW, pl)
for k, v in (("MILLENAI_DEV", "1"), ("MILLENAI_HOME", HOME), ("MILLENAI_PORT", "9906"),
             ("MILLENAI_UPDATE_API", "http://127.0.0.1:9")):
    subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Add :LSEnvironment dict", pl], capture_output=True)
    sh("/usr/libexec/PlistBuddy", "-c", "Add :LSEnvironment:%s string %s" % (k, v), pl)
mp = nb + "/Contents/Resources/millenai.py"
s = open(mp).read()
assert s.count("\nAPP_BUILD = %d" % N) == 1
open(mp, "w").write(s.replace("\nAPP_BUILD = %d" % N, "\nAPP_BUILD = %d" % NEW, 1))
sh("codesign", "--force", "--sign", "-", "--entitlements", ENT, nb)
DMG = FILES + "/ConcordeAI-7.0.0.dmg"
sh("hdiutil", "create", "-quiet", "-volname", "ConcordeAI", "-srcfolder", B2, "-ov", "-format", "UDZO", DMG)
rel = {"tag_name": "v%d" % NEW, "name": "7.0 beta", "prerelease": True, "draft": False,
       "published_at": "2026-11-10T10:00:00Z", "body": "test", "html_url": "http://127.0.0.1:9908/r",
       "assets": [{"name": "ConcordeAI-7.0.0.dmg", "size": os.path.getsize(DMG),
                   "browser_download_url": "http://127.0.0.1:9908/ConcordeAI-7.0.0.dmg"}]}
hits = []


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        hits.append(self.path)
        if self.path.startswith("/repos/bigmillz/concordeai/releases"):
            b = json.dumps(rel if self.path.endswith("/latest") else [rel]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/ConcordeAI-7.0.0.dmg":
            b = open(DMG, "rb").read()
            self.send_response(200)
        else:
            b = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 9908), H)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
env.update(MILLENAI_DEV="1", MILLENAI_HOME=HOME, MILLENAI_PORT="9906", MILLENAI_UPDATE_API="http://127.0.0.1:9908")
log = open(S + "/shell1.log", "w")
p1 = subprocess.Popen([APP + "/Contents/MacOS/ConcordeAI"], env=env, stdout=log, stderr=subprocess.STDOUT,
                      start_new_session=True)
new_pid = None


def note():
    try:
        return json.load(open(HOME + "/run/instance.json"))
    except Exception:
        return None


def q(path, method="GET"):
    d = note()
    r = urllib.request.Request("http://127.0.0.1:9906" + path, method=method,
                               data=(b"{}" if method == "POST" else None),
                               headers={"Cookie": "millen_key_9906=" + d["key"], "X-Api-Token": d["token"],
                                        "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())


try:
    d = None
    for _ in range(120):
        d = note()
        if d and d.get("port") == 9906 and d.get("pid") == p1.pid:
            break
        time.sleep(1)
    front1 = d["pid"]
    check("the installed C++ app (build %d) starts as a dev copy; the note names its front" % N,
          d and d.get("port") == 9906 and front1 == p1.pid)
    time.sleep(3)
    c = q("/api/update/check?force=1")
    check("it is offered %d by the stand-in feed" % NEW, c.get("available") is True and str(NEW) in json.dumps(c), c)
    q("/api/update/install", "POST")
    t0 = time.time()
    rc = None
    for _ in range(240):
        rc = p1.poll()
        if rc is not None:
            break
        time.sleep(0.5)
    check("the server exited cleanly and the C++ app quit on its own", rc == 0, rc)
    for _ in range(120):
        try:
            if plistlib.load(open(APP + "/Contents/Info.plist", "rb"))["CFBundleVersion"] == str(NEW):
                break
        except Exception:
            pass
        time.sleep(0.5)
    v = plistlib.load(open(APP + "/Contents/Info.plist", "rb"))["CFBundleVersion"]
    check("the helper swapped the bundle in place (CFBundleVersion %d)" % NEW, v == str(NEW), v)
    check("no .new left, no quarantine, the signature verifies", not os.path.exists(APP + ".new") and
          "com.apple.quarantine" not in subprocess.run(["xattr", APP], capture_output=True, text=True).stdout
          and subprocess.run(["codesign", "--verify", "--strict", APP], capture_output=True).returncode == 0)
    d2 = None
    for _ in range(90):
        d2 = note()
        if d2 and d2.get("pid") not in (None, front1) and d2.get("port") == 9906:
            break
        time.sleep(1)
    out = subprocess.run(["pgrep", "-f", APP + "/Contents/MacOS/ConcordeAI"], capture_output=True, text=True).stdout.split()
    new_pid = int(out[0]) if out else None
    check("open -n relaunched the new bundle: a new C++ app from the same path, its front in the note",
          new_pid is not None and new_pid != p1.pid and d2 and d2.get("pid") == new_pid, [out, d2 and d2.get("pid")])
    src2 = {}
    for _ in range(20):          # the relaunched server may still be warming up
        try:
            src2 = q("/api/update/check?force=1")
            break
        except Exception as exc:
            src2 = {"error": repr(exc), "note": note(),
                    "listen": subprocess.run(["lsof", "-nP", "-iTCP:9906", "-sTCP:LISTEN"], capture_output=True,
                                             text=True).stdout[-300:],
                    "alive": subprocess.run(["ps", "-o", "pid,etime,command", "-p", str(new_pid)],
                                            capture_output=True, text=True).stdout[-300:]}
            time.sleep(1)
    check("the relaunched app is build %d (no offer of %d any more)" % (NEW, NEW), src2.get("available") is False, src2)
    print("  secs from install to the relaunched app's note: %.1f" % (time.time() - t0))
except Exception as exc:
    check("the proof ran to its end", False, repr(exc))
finally:
    if new_pid:
        os.kill(new_pid, signal.SIGTERM)
        for _ in range(80):
            if subprocess.run(["kill", "-0", str(new_pid)], capture_output=True).returncode:
                break
            time.sleep(0.25)
    if p1.poll() is None:
        os.kill(p1.pid, signal.SIGTERM)
    srv.shutdown()
print("RESULT %d/%d" % (sum(1 for _, o in R if o), len(R)))
sys.exit(0 if all(o for _, o in R) else 1)
