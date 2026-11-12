"""The millenai.py changes the C++ front needs for the real app (6b618), for
a tree that doesn't carry them yet (beta-4's, beta-5's). Each part applies
once; a tree that has one keeps it.

1. _other_millenai_running() counted a listener of this user's on 8889 as
   "another MillenAI" unless it was this process's own PORT; behind the C++
   front the server's PORT is the private port and 8889 is the front's, so
   the server took itself for a sibling and left the engines running at quit.
   In shell mode the front's port (CONCORDE_PUBLIC_PORT, set by the C++
   shell) is this app's own.
2. The updater's swap helper inherits the server's environment, and `open`
   hands it to the app it relaunches. Behind the front that environment says
   MILLENAI_PORT=<the private port>: a rollback to the Tauri shell served the
   page on that random port, a different origin, so the page's own browser
   settings (the Effects level, the model) were not there. The helper now
   starts with MILLENAI_PORT set to the public port, either shell's next start.

Usage: patch_py_public_port.py <millenai.py>"""
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
done = []

if "_shell_public_port()" not in s:
    a = '''    for p in (8889,):     # 9889, the retired web copy's port, went (6b320)
        if p != PORT and _port_in_use(p) and _listener_is_mine(p) is True:'''
    b = '''    for p in (8889,):     # 9889, the retired web copy's port, went (6b320)
        if p not in (PORT, _shell_public_port()) and _port_in_use(p) and _listener_is_mine(p) is True:'''
    assert s.count(a) == 1, "anchor 1 not found"
    s = s.replace(a, b)
    a2 = '''def _other_millenai_running() -> bool:'''
    b2 = '''def _shell_public_port() -> int:
    """The port the C++ shell's front serves this app on (6b618): the app's
    own public port, never a sibling's. 0 outside the native shell."""
    v = os.environ.get("CONCORDE_PUBLIC_PORT", "") if SHELL else ""
    return int(v) if v.isdigit() else 0


def _other_millenai_running() -> bool:'''
    assert s.count(a2) == 1, "anchor 2 not found"
    s = s.replace(a2, b2)
    done.append("sibling check")

if "def _swap_env()" not in s:
    a3 = '''        subprocess.Popen(["/bin/zsh", script], start_new_session=True)'''
    b3 = '''        subprocess.Popen(["/bin/zsh", script], start_new_session=True, env=_swap_env())'''
    assert s.count(a3) == 1, "anchor 3 not found"
    s = s.replace(a3, b3)
    # just above _app_bundle_path: inside the stretch beta-4's gauntlet execs
    # for its _do_update cases (6b620), and it needs nothing but os
    a4 = '''def _app_bundle_path():'''
    b4 = '''def _swap_env() -> dict:
    """The swap helper's environment, which `open` hands to the app it
    relaunches (6b618). Behind the C++ front this server's MILLENAI_PORT is
    the private port, and CONCORDE_PUBLIC_PORT (set only by the C++ shell)
    the app's own; the next app, either shell, starts on the public one, so
    the page keeps its origin and with it its own browser settings."""
    env = dict(os.environ)
    pub = env.pop("CONCORDE_PUBLIC_PORT", "")
    if pub.isdigit():
        env["MILLENAI_PORT"] = pub
    return env


def _app_bundle_path():'''
    assert s.count(a4) == 1, "anchor 4 not found"
    s = s.replace(a4, b4)
    done.append("swap helper's port")

if done:
    open(p, "w", encoding="utf-8").write(s)
    print("patched:", ", ".join(done))
else:
    print("already patched")
