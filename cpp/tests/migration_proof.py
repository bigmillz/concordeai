"""Beta 4 (Tauri) -> beta 5 (C++) -> back to Tauri, through the in-app updater
(NOTES 6b618). The move to the C++ window on a Mac must keep everything a
person has: chats, prefs, the page's own localStorage (the Effects level lives
there; WebKit keys its store on the bundle id, which both shells share), server
pairings, the window's place; and a rollback must keep them too.

Three bundles under ONE test bundle id (com.millen.millenai.migtest, so the
WebKit store is the test's, never the shipping app's), all dev copies of one
test folder on port 9906, a stand-in GitHub on 9908, the stand-in provider as
the HTTPS proxy (nothing leaves this Mac), no real server:
  280  beta-4's Tauri shell (a debug build: its probe), beta-4's server
  281  the C++ shell (a build with the probe), the same server
  282  beta-4's Tauri shell again (the rollback)
280 is offered 281, installs it, quits; the updater's helper swaps the bundle
and relaunches it; then 281 is offered 282 and does the same.

    python3 cpp/tests/migration_proof.py --tauri <debug Tauri binary> --cpp <C++ .app with probe>
        --server <beta-4 millenai.py> --work <dir>
"""
import argparse
import http.server
import json
import os
import plistlib
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "bench"))
import stub_provider  # noqa: E402
from fluid_ab import Probe  # noqa: E402

VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
BID = "com.millen.millenai.migtest"
PORT, FEED = 9906, 9908
# never plain "ConcordeAI": a test window must not pass for the real app (6b618)
TEST_NAME = "ConcordeAI Test"
R = []


def check(n, ok, d=""):
    R.append((n, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + n + ("" if ok else "  -- " + str(d)[:400]), flush=True)


def sh(*a):
    return subprocess.run(a, check=True, capture_output=True, text=True).stdout


def port_open(p):
    import socket
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def plist_set(pl, key, val):
    d = plistlib.load(open(pl, "rb"))
    d[key] = val
    plistlib.dump(d, open(pl, "wb"))


def stamp_server(path, build):
    s = open(path, encoding="utf-8").read()
    import re
    s2, n = re.subn(r"(?m)^APP_BUILD = \d+", "APP_BUILD = %d" % build, s, count=1)
    assert n == 1
    open(path, "w", encoding="utf-8").write(s2)


def tauri_bundle(dst, binary, server, build, lsenv):
    c = os.path.join(dst, "Contents")
    os.makedirs(os.path.join(c, "MacOS"))
    os.makedirs(os.path.join(c, "Resources"))
    shutil.copy(binary, os.path.join(c, "MacOS", "ConcordeAI"))
    root = os.path.dirname(server)
    shutil.copy(server, os.path.join(c, "Resources", "millenai.py"))
    stamp_server(os.path.join(c, "Resources", "millenai.py"), build)
    for d in ("fonts", "vfx"):
        if os.path.isdir(os.path.join(root, d)):
            shutil.copytree(os.path.join(root, d), os.path.join(c, "Resources", d))
    plistlib.dump({"CFBundleExecutable": "ConcordeAI", "CFBundleIdentifier": BID, "CFBundleName": TEST_NAME, "CFBundleDisplayName": TEST_NAME,
                   "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "7.0.0", "CFBundleVersion": str(build),
                   "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0", "LSEnvironment": lsenv},
                  open(os.path.join(c, "Info.plist"), "wb"))
    sh("codesign", "--force", "--sign", "-", dst)


def cpp_bundle(dst, src_app, build, lsenv):
    sh("ditto", src_app, dst)
    pl = os.path.join(dst, "Contents", "Info.plist")
    plist_set(pl, "CFBundleVersion", str(build))
    plist_set(pl, "LSEnvironment", lsenv)
    plist_set(pl, "CFBundleName", TEST_NAME)
    plist_set(pl, "CFBundleDisplayName", TEST_NAME)
    assert plistlib.load(open(pl, "rb"))["CFBundleIdentifier"] == BID, "build the C++ app with -DCONCORDE_BUNDLE_ID=" + BID
    stamp_server(os.path.join(dst, "Contents", "Resources", "millenai.py"), build)
    sh("codesign", "--force", "--sign", "-", dst)


def dmg(app_dir, out):
    st = out + ".src"
    shutil.rmtree(st, ignore_errors=True)
    os.makedirs(st)
    sh("ditto", app_dir, os.path.join(st, "ConcordeAI.app"))
    sh("hdiutil", "create", "-quiet", "-volname", "ConcordeAI", "-srcfolder", st, "-ov", "-format", "UDZO", out)
    shutil.rmtree(st)


RELEASE = {}


class Feed(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        rel = RELEASE["rel"]
        if self.path.startswith("/repos/bigmillz/concordeai/releases"):
            b = json.dumps(rel if self.path.endswith("/latest") else [rel]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/ConcordeAI-7.0.0.dmg.sha256":
            b = RELEASE["sha"]
            self.send_response(200)
        elif self.path == "/ConcordeAI-7.0.0.dmg":
            b = open(RELEASE["dmg"], "rb").read()
            self.send_response(200)
        else:
            b = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def offer(build, path):
    """The release as release.sh publishes it: the DMG and its .sha256 beside it
    (6b620: an update without a checksum is never installed)."""
    import hashlib
    RELEASE["dmg"] = path
    RELEASE["sha"] = ("%s  ConcordeAI-7.0.0.dmg\n" % hashlib.sha256(open(path, "rb").read()).hexdigest()).encode()
    RELEASE["rel"] = {"tag_name": "v%d" % build, "name": "7.0 beta", "prerelease": True, "draft": False,
                      "published_at": "2026-11-11T10:00:00Z", "body": "test", "html_url": "http://127.0.0.1:%d/r" % FEED,
                      "assets": [{"name": "ConcordeAI-7.0.0.dmg", "size": os.path.getsize(path),
                                  "browser_download_url": "http://127.0.0.1:%d/ConcordeAI-7.0.0.dmg" % FEED},
                                 {"name": "ConcordeAI-7.0.0.dmg.sha256", "size": len(RELEASE["sha"]),
                                  "browser_download_url": "http://127.0.0.1:%d/ConcordeAI-7.0.0.dmg.sha256" % FEED}]}


def note(home):
    try:
        return json.load(open(os.path.join(home, "run", "instance.json")))
    except Exception:
        return None


def q(home, path, data=None, method=None):
    d = note(home)
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), method=method or ("POST" if data is not None else "GET"),
                               data=None if data is None else json.dumps(data).encode(),
                               headers={"Cookie": "millen_key_%d=%s" % (PORT, d["key"]), "X-Api-Token": d["token"],
                                        "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read() or b"null")


def app_pid(app):
    out = subprocess.run(["pgrep", "-f", os.path.join(app, "Contents", "MacOS", "ConcordeAI")], capture_output=True,
                         text=True).stdout.split()
    return int(out[0]) if out else None


def win_place(pid):
    """Position and size of the process's front window (System Events, points, top-left)."""
    out = subprocess.run(["osascript", "-e",
                          'tell application "System Events" to tell (first process whose unix id is %d) to '
                          'get {position, size} of window 1' % pid], capture_output=True, text=True).stdout.strip()
    try:
        return [int(x) for x in out.replace(" ", "").split(",")]
    except ValueError:
        return out


def set_place(pid, x, y, w, h):
    subprocess.run(["osascript", "-e", 'tell application "System Events" to tell (first process whose unix id is %d) '
                    'to set {position, size} of window 1 to {{%d, %d}, {%d, %d}}' % (pid, x, y, w, h)],
                   capture_output=True)


def status_of(home):
    try:
        return q(home, "/api/update/status")
    except Exception as exc:
        return repr(exc)[:120]


def swap_log(home):
    try:
        return open(os.path.join(home, "update-swap.log")).read().strip()
    except OSError:
        return None


SEED = {"x": 180, "y": 140, "w": 1120, "h": 760, "zoomed": False}


def fresh_probe(d, kind):
    """Each shell counts its cmd-<n>.js from 1, so a new process gets an empty folder."""
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    return Probe(d, kind)


def native(pr, op, timeout=20):
    """The C++ shell's own probe ops (//native:state ...), written raw."""
    pr.n += 1
    out = os.path.join(pr.d, "out-%d.json" % pr.n)
    tmp = os.path.join(pr.d, ".c%d" % pr.n)
    open(tmp, "w").write(op)
    os.rename(tmp, os.path.join(pr.d, "cmd-%d.js" % pr.n))
    return wait_for(lambda: os.path.exists(out) and json.load(open(out)), t=timeout, step=0.1)


def shell_window(home):
    try:
        d = json.load(open(os.path.join(home, "shell-window.json")))
        return [d.get("x"), d.get("y"), d.get("w"), d.get("h")]
    except (OSError, ValueError):
        return None


def ready(home, pr):
    """The server answers and the page has run its script."""
    def up():
        try:
            return q(home, "/api/prefs") is not None
        except Exception:
            return False
    wait_for(up, t=120, step=0.5)
    return wait_for(lambda: pr.ev("typeof send === 'function' && document.readyState === 'complete'", timeout=5),
                    t=60, step=0.5)


def wait_for(fn, t=120, step=0.5):
    t0 = time.time()
    while time.time() - t0 < t:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def state_of(home, pr, kind):
    """Everything a person has, read back the same way in either shell."""
    out = {}
    v = pr.ev("JSON.stringify({fx: localStorage.getItem('millen.fxlevel'), mark: localStorage.getItem('millen.recentsDays'),"
              " origin: location.origin, cls: (document.documentElement.className.match(/hz-(full|bal|calm)/)||[null])[0]})")
    out["ls"] = json.loads(v) if isinstance(v, str) else v
    chats = q(home, "/api/chats")
    lst = chats.get("chats") if isinstance(chats, dict) else chats
    out["chats"] = sorted((c.get("id"), len(c.get("messages") or []) or c.get("n") or c.get("count")) for c in (lst or []))
    prefs = q(home, "/api/prefs")
    out["prefs"] = {k: prefs.get(k) for k in ("turbo", "tier", "fx_level") if isinstance(prefs, dict)}
    srv = q(home, "/api/servers")
    out["servers"] = sorted((s.get("id"), s.get("name")) for s in (srv.get("servers") or []))
    out["servers_json"] = os.path.exists(os.path.join(home, "servers.json"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tauri", required=True)
    ap.add_argument("--cpp", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--work", required=True)
    # THE WINDOWS ARE REAL (6b618): both shells open a visible window and take focus.
    # A run on a Mac someone is using put one in front of them; run this only where
    # nobody is working, and say so
    ap.add_argument("--visible-windows-ok", action="store_true")
    a = ap.parse_args()
    if not a.visible_windows_ok:
        sys.exit("this proof opens visible windows that take focus: run it only on a Mac nobody is using "
                 "(--visible-windows-ok)")
    for p in (PORT, FEED):
        if port_open(p):
            sys.exit("port %d is taken" % p)
    store = os.path.expanduser("~/Library/WebKit/" + BID)
    assert store.endswith(".migtest")
    shutil.rmtree(store, ignore_errors=True)
    W = os.path.abspath(a.work)
    shutil.rmtree(W, ignore_errors=True)
    for d in ("apps", "b2", "b3", "files", "home", "probe"):
        os.makedirs(os.path.join(W, d))
    home, probe = os.path.join(W, "home"), os.path.join(W, "probe")
    APP = os.path.join(W, "apps", "ConcordeAI.app")
    srv = stub_provider.http.server.ThreadingHTTPServer(("127.0.0.1", 0), stub_provider.Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    surl = "http://127.0.0.1:%d" % srv.server_address[1]
    env = {"MILLENAI_DEV": "1", "MILLENAI_HOME": home, "MILLENAI_PORT": str(PORT),
           "MILLENAI_UPDATE_API": "http://127.0.0.1:%d" % FEED, "MILLENAI_TEST_HOOKS": "no-downloads,local-record",
           "CONCORDE_PYTHON": VENV, "CONCORDE_SHELL_PROBE": probe, "CONCORDE_SHELL_TIMING": "1",
           "CONCORDE_SERVER": os.path.join(APP, "Contents", "Resources", "millenai.py"),
           "HTTPS_PROXY": surl, "https_proxy": surl, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    tauri_bundle(APP, a.tauri, a.server, 280, env)
    cpp_bundle(os.path.join(W, "b2", "ConcordeAI.app"), a.cpp, 281, env)
    tauri_bundle(os.path.join(W, "b3", "ConcordeAI.app"), a.tauri, a.server, 282, env)
    dmg281, dmg282 = os.path.join(W, "files", "c281.dmg"), os.path.join(W, "files", "t282.dmg")
    dmg(os.path.join(W, "b2", "ConcordeAI.app"), dmg281)
    dmg(os.path.join(W, "b3", "ConcordeAI.app"), dmg282)
    feed = socketserver.ThreadingTCPServer(("127.0.0.1", FEED), Feed)
    feed.daemon_threads = True
    threading.Thread(target=feed.serve_forever, daemon=True).start()
    offer(281, dmg281)
    penv = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    penv.update(env)
    json.dump(SEED, open(os.path.join(home, "shell-window.json"), "w"))
    p1 = subprocess.Popen([os.path.join(APP, "Contents", "MacOS", "ConcordeAI")], env=penv,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    last_pid = None
    try:
        d = wait_for(lambda: (note(home) or {}).get("port") == PORT and note(home))
        check("280, beta-4's Tauri shell, starts on the test folder", d is not None, d)
        pr = fresh_probe(probe, "tauri")
        check("280's page is up", ready(home, pr))
        # ---- what a person has, made in 280
        mark = str((1, 3, 7, 14, 30)[os.getpid() % 5])   # millen.recentsDays: on the page's KEEP_LS; the store starts empty
        pr.ev("(localStorage.setItem('millen.fxlevel','full'), localStorage.setItem('millen.recentsDays','%s'), 1)" % mark)
        pr.ev("(document.getElementById('input').value='A question asked in beta 4.', "
              "document.getElementById('input').dispatchEvent(new Event('input',{bubbles:true})), send(), 1)")
        wait_for(lambda: any(len(c.get("messages") or []) >= 2 for c in
                             ((q(home, "/api/chats") or {}).get("chats") or [])), t=90, step=1)
        q(home, "/api/prefs", {"turbo": True, "tier": "Thinking"})
        added = q(home, "/api/servers/add", {"url": "https://standin-box.example.test", "name": "Test box"})
        before = state_of(home, pr, "tauri")
        before["place"] = win_place(p1.pid)     # System Events' view (needs its Accessibility grant)
        print("  in 280:", json.dumps(before))
        check("280 holds a chat, prefs, a pairing and the page's own Effects level",
              before["chats"] and before["prefs"].get("tier") == "Thinking" and before["servers"]
              and before["ls"] and before["ls"].get("fx") == "full" and before["ls"].get("mark") == mark, [before, added])
        # ---- 280 -> 281 (C++)
        c = q(home, "/api/update/check?force=1")
        check("280 is offered 281 (the C++ shell) by the stand-in feed", c.get("available") is True, c)
        q(home, "/api/update/install", {})
        quit1 = wait_for(lambda: p1.poll() is not None, t=120)
        prc = fresh_probe(probe, "cpp")
        left280 = shell_window(home)
        check("280's server exits clean and the Tauri shell quits on it", quit1 and p1.returncode == 0,
              p1.returncode if quit1 else ("still running", status_of(home)))
        wait_for(lambda: plistlib.load(open(os.path.join(APP, "Contents", "Info.plist"), "rb"))["CFBundleVersion"] == "281", t=90)
        d2 = wait_for(lambda: (note(home) or {}).get("pid") not in (None, d["pid"]) and note(home), t=90)
        last_pid = app_pid(APP)
        exe = open(os.path.join(APP, "Contents", "MacOS", "ConcordeAI"), "rb").read()
        check("the updater swapped in 281 and relaunched it: the C++ shell (its bridge's platform), its front in the note",
              d2 and last_pid and b"concorde-cpp" in exe and d2.get("pid") == last_pid, [d2, last_pid])
        check("281's page is up", ready(home, prc))
        after = state_of(home, prc, "cpp")
        st = native(prc, "//native:state") or {}
        after["place"] = st.get("frame")
        after["visible"] = st.get("visible")
        print("  in 281:", json.dumps(after))
        for k, label in (("chats", "the chats"), ("prefs", "the prefs"), ("servers", "the server pairing"),
                         ("servers_json", "servers.json")):
            check("C++ 281 keeps %s" % label, after[k] == before[k], [before[k], after[k]])
        check("C++ 281 keeps the page's localStorage (one WebKit store per bundle id): the Effects level Full, applied",
              after["ls"] and after["ls"].get("fx") == "full" and after["ls"].get("mark") == mark
              and after["ls"].get("cls") == "hz-full" and after["ls"].get("origin") == before["ls"].get("origin"),
              [before["ls"], after["ls"]])
        check("C++ 281 opens where 280 left the window (shell-window.json, one format)",
              left280 == [SEED["x"], SEED["y"], SEED["w"], SEED["h"]]
              and [int(v) for v in (after["place"] or [])] == left280 and after["visible"],
              [left280, after["place"], after["visible"]])
        # ---- 281 -> 282 (back to Tauri)
        offer(282, dmg282)
        c = q(home, "/api/update/check?force=1")
        check("281 is offered 282 (Tauri again: the rollback)", c.get("available") is True, c)
        old_note = note(home)
        cpp_pid = last_pid
        q(home, "/api/update/install", {})
        time.sleep(4)
        st2 = status_of(home)
        wait_for(lambda: subprocess.run(["kill", "-0", str(cpp_pid)], capture_output=True).returncode != 0, t=120)
        prt = fresh_probe(probe, "tauri")
        left281 = shell_window(home)
        wait_for(lambda: plistlib.load(open(os.path.join(APP, "Contents", "Info.plist"), "rb"))["CFBundleVersion"] == "282", t=120)
        d3 = wait_for(lambda: (note(home) or {}).get("pid") not in (None, old_note["pid"]) and note(home), t=120)
        last_pid = app_pid(APP)
        check("the C++ shell quit on its server's clean exit; the helper swapped in 282 and relaunched it",
              d3 is not None and last_pid is not None, [d3, last_pid, st2, swap_log(home)])
        check("282's page is up", ready(home, prt))
        back = state_of(home, prt, "tauri")
        back["place"] = win_place(last_pid)
        print("  in 282:", json.dumps(back))
        check("the rollback keeps everything: chats, prefs, pairing, localStorage, the place",
              all(back[k] == before[k] for k in ("chats", "prefs", "servers", "servers_json"))
              and back["ls"] and back["ls"].get("fx") == "full" and back["ls"].get("mark") == mark
              and back["ls"].get("origin") == before["ls"].get("origin")
              and left281 == left280, [before, back, left281])
    except Exception as exc:
        check("the proof ran to its end", False, repr(exc))
    finally:
        # the swap helper watches the app it relaunched for 20 s and brings the old one back
        # if it is gone: killing it inside that window relaunched an app after the proof had
        # ended (6b618). Let the watch run out first, then sweep for anything relaunched
        time.sleep(25)
        for pid in {last_pid, app_pid(APP)} - {None}:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if p1.poll() is None:
            p1.send_signal(signal.SIGTERM)
        wait_for(lambda: not port_open(PORT), t=20)
        t_end = time.time() + 30
        while time.time() < t_end:
            for d in (APP, os.path.join(W, "b2", "ConcordeAI.app"), os.path.join(W, "b3", "ConcordeAI.app")):
                left = app_pid(d)
                if left:
                    print("  swept a relaunched test app, pid %d" % left)
                    try:
                        os.kill(left, signal.SIGTERM)
                    except OSError:
                        pass
            time.sleep(1)
        feed.shutdown()
        srv.shutdown()
    print("RESULT %d/%d" % (sum(1 for _, o in R if o), len(R)))
    sys.exit(0 if all(o for _, o in R) else 1)


if __name__ == "__main__":
    main()
