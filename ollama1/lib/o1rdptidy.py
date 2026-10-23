"""Remote desktop cleanup (6b506): end GNOME remote desktop sessions nobody is
connected to any more.

GNOME's remote login (gnome-remote-desktop, RDP on :3389, through GDM) leaves
two kinds of session behind when a client goes away: the remote login screen
(a GDM greeter) and a logged-in remote desktop. Each keeps a
gnome-remote-desktop-daemon holding about 230 MiB of the graphics card's
memory, and GDM blocks suspend while any exists ("Remote sessions would die on
suspend"), so auto sleep never happens. Per the owner: a remote login screen
nobody is connected to is ended at once, a remote desktop after it has been
disconnected for rdp_tidy_minutes (30).

Conservative, because telling a connected session from a disconnected one is
not verified with a live client yet (logind's State, Active and IdleHint stay
"active"/"no" either way):
  * only a remote display session is ever considered: Remote=yes,
    Type=wayland, a gdm- service, class greeter or user, no seat, no TTY;
  * a session counts as disconnected only while NO TCP connection is
    established on local :3389 at all, machine-wide, and something listens
    on :3389 (else the port isn't the one RDP uses, and nothing is ended);
  * any probe that can't answer ends nothing;
  * a login screen is ended on the second look that finds no client (about a
    minute), which rides out the reconnect RDP does when a login hands the
    client over to the new session;
  * which session each connection belongs to (the connection's process ->
    its cgroup -> session-<id>.scope, or the user slice) is worked out and
    logged only, as what a per-session rule would have decided.

Pure functions parse the command outputs and decide; Tidy runs one pass with
every probe injected, so the tests use fixture outputs and a fake clock.
Logged and stored: session ids, the class, counts and reasons. Never a user
name, a uid or a remote address.
"""
import json
import os
import re
import subprocess
import time

from o1common import Paths, read_json_safe, write_json_atomic

RDP_PORT = 3389
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 5, 1440, 30
GREETER_GRACE_S = 45          # a login screen is ended when no client was seen on two looks this far apart
SID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")    # never "-x": loginctl would read an option
CLASSES = ("greeter", "user")
PROPS = ("Id", "Class", "Type", "Service", "Remote", "Seat", "TTY", "State", "User")   # never Name/RemoteHost
GRD_PATHS = ("/usr/libexec/gnome-remote-desktop-daemon", "/usr/lib/gnome-remote-desktop/gnome-remote-desktop-daemon",
             "/usr/bin/gnome-remote-desktop-daemon", "/usr/lib/systemd/system/gnome-remote-desktop.service",
             "/lib/systemd/system/gnome-remote-desktop.service")


def state_file():
    return os.path.join(Paths.state, "rdp-tidy.json")


# ---- settings ---------------------------------------------------------------------

def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def settings(cfg):
    """(enabled, minutes, note) from config.json's rdp_tidy and rdp_tidy_minutes. rdp_tidy is on unless it is
    false; anything that isn't true or false turns it off (ending nothing is the safe side) and says so."""
    cfg = cfg if isinstance(cfg, dict) else {}
    on = cfg.get("rdp_tidy", True)
    note = ""
    if not isinstance(on, bool):
        on, note = False, "rdp_tidy in config.json isn't true or false, so it is off"
    m = cfg.get("rdp_tidy_minutes", DEFAULT_MINUTES)
    m = max(MIN_MINUTES, min(MAX_MINUTES, m)) if _int(m) else DEFAULT_MINUTES
    return on, m, note


def grd_installed(paths=GRD_PATHS, exists=os.path.exists):
    return any(exists(p) for p in paths)


# ---- parsers (pure) ---------------------------------------------------------------

def parse_list_sessions(text):
    """Session ids from `loginctl -j list-sessions` (a JSON list of objects with "session"), or from the plain
    `--no-legend` table (the first column). Anything that doesn't look like an id is dropped."""
    ids = []
    s = (text or "").strip()
    rows = None
    if s.startswith("["):
        try:
            rows = json.loads(s)
        except ValueError:
            rows = None
    if isinstance(rows, list):
        for r in rows:
            v = r.get("session", r.get("id")) if isinstance(r, dict) else None
            v = str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
            if SID_RX.match(v) and v not in ids:
                ids.append(v)
        return ids
    for line in s.splitlines():
        t = line.split()
        if t and SID_RX.match(t[0]) and t[0] not in ids:
            ids.append(t[0])
    return ids


def parse_show_session(text):
    """`loginctl show-session <id> -p ...` (Key=value lines) -> dict."""
    out = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in PROPS:
            out[k.strip()] = v.strip()
    return out


def remote_display(props):
    """The class ("greeter" or "user") when this is a GNOME remote desktop session the tidy may consider, else
    None. Every condition has to hold; a missing property is not a yes."""
    p = props if isinstance(props, dict) else {}
    if p.get("Remote") != "yes":
        return None
    if p.get("Type") != "wayland":
        return None
    if not p.get("Service", "").startswith("gdm-"):
        return None
    if "Seat" not in p or p["Seat"] or "TTY" not in p or p["TTY"]:
        return None                                  # a seat or a TTY: a local or console session
    c = p.get("Class")
    return c if c in CLASSES else None               # never manager, background, lock-screen, ...


def _port(addr):
    m = re.search(r":(\d+)$", addr or "")
    return int(m.group(1)) if m else None


def parse_ss(text, port=RDP_PORT):
    """Established connections from `ss -tnpH state established '( sport = :3389 )'`: [{"pids": [...]}], one per
    line whose local address ends in :port. With a single state filter ss leaves the State column out (Recv-Q
    Send-Q Local Peer Process); a line that still has it is read too. Addresses are not kept."""
    out = []
    for line in (text or "").splitlines():
        t = line.split()
        if t and not t[0].isdigit():
            t = t[1:]                                # a State column
        if len(t) < 4 or _port(t[2]) != port:
            continue
        out.append({"pids": sorted({int(x) for x in re.findall(r"pid=(\d+)", line)})})
    return out


def parse_listening(text, port=RDP_PORT):
    """True when `ss -tlnH '( sport = :3389 )'` shows something listening on the port."""
    for line in (text or "").splitlines():
        t = line.split()
        if t and not t[0].isdigit():
            t = t[1:]
        if len(t) >= 4 and _port(t[2]) == port:
            return True
    return False


def parse_cgroup(text):
    """Who a process belongs to, from /proc/<pid>/cgroup: ("session", id) for a session-<id>.scope, ("uid", n)
    for anything under user-<n>.slice (a user's systemd, say), else None."""
    path = ""
    for line in (text or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[0] == "0" or not path):
            path = parts[2]
    m = re.search(r"/session-([A-Za-z0-9_-]{1,32})\.scope(?:/|$)", path)
    if m:
        return ("session", m.group(1))
    m = re.search(r"/user-(\d+)\.slice(?:/|$)", path)
    if m:
        return ("uid", int(m.group(1)))
    return None


def per_session_view(conns, owner_of, sessions):
    """What a per-session rule would conclude, for the log only: {sid: True (a connection is that session's),
    False (none is), None (a connection's owner couldn't be told)}. `owner_of(pid)` -> parse_cgroup's answer."""
    owners = []
    unknown = False
    for c in conns:
        got = [o for o in (owner_of(p) for p in c.get("pids") or []) if o]
        if not got:
            unknown = True
        owners.extend(got)
    out = {}
    for sid, s in sessions.items():
        mine = any((k == "session" and v == sid) or (k == "uid" and str(v) == s.get("uid", "")) for k, v in owners)
        out[sid] = True if mine else (None if unknown else False)
    return out


def view_word(v):
    return {True: "connected", False: "not connected", None: "unknown"}[v]


# ---- the decision (pure) ----------------------------------------------------------

def plan(sessions, conns, listening, seen, now, minutes):
    """One pass. `sessions`: {sid: {"class": "greeter"|"user", "uid": str}} (remote display sessions only);
    `conns`: established connection count on :3389, or None when ss couldn't answer; `listening`: True/False/None;
    `seen`: {sid: epoch first seen with no client}. Returns (actions, new_seen): actions is
    [{"id", "class", "do": "end"|"wait"|"keep", "reason"}], sorted by id."""
    acts = []
    new_seen = {}
    if conns is None or listening is not True:
        why = ("couldn't list the RDP connections" if conns is None else
               "couldn't tell what listens on :%d" % RDP_PORT if listening is None else
               "nothing listens on :%d, so RDP may be on another port" % RDP_PORT)
        for sid in sorted(sessions):
            acts.append({"id": sid, "class": sessions[sid]["class"], "do": "keep", "reason": why})
            if sid in seen:
                new_seen[sid] = seen[sid]            # an unknown isn't a reconnect: the clock keeps running
        return acts, new_seen
    if conns > 0:
        why = "%d RDP connection%s open" % (conns, "" if conns == 1 else "s")
        for sid in sorted(sessions):                 # a client is connected: nothing ends, every clock is cleared
            acts.append({"id": sid, "class": sessions[sid]["class"], "do": "keep", "reason": why})
        return acts, {}
    for sid in sorted(sessions):
        cls = sessions[sid]["class"]
        first = seen.get(sid)
        first = first if isinstance(first, (int, float)) and not isinstance(first, bool) and first <= now else now
        new_seen[sid] = first
        gone = now - first
        if cls == "greeter":
            if gone >= GREETER_GRACE_S:
                acts.append({"id": sid, "class": cls, "do": "end", "reason": "remote login screen, no RDP client"})
            else:
                acts.append({"id": sid, "class": cls, "do": "wait",
                             "reason": "remote login screen, no RDP client: ended on the next look"})
        elif gone >= minutes * 60:
            acts.append({"id": sid, "class": cls, "do": "end",
                         "reason": "remote desktop, no RDP client for %d minutes" % (gone // 60)})
        else:
            acts.append({"id": sid, "class": cls, "do": "wait",
                         "reason": "remote desktop, no RDP client for %d of %d minutes" % (gone // 60, minutes)})
    return acts, new_seen


# ---- the probes (the callers run the commands) ------------------------------------

def run_text(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def list_sessions():
    t = run_text(["loginctl", "-j", "list-sessions", "--no-pager"])
    ids = parse_list_sessions(t) if t is not None else None
    if not ids:
        t2 = run_text(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
        if t2 is not None:
            return parse_list_sessions(t2)
    return ids


def show_session(sid):
    cmd = ["loginctl", "show-session", sid, "--no-pager"]
    for p in PROPS:
        cmd += ["-p", p]
    t = run_text(cmd)
    return None if t is None else parse_show_session(t)


def connections():
    t = run_text(["ss", "-tnpH", "state", "established", "( sport = :%d )" % RDP_PORT])
    return None if t is None else parse_ss(t)


def listening():
    t = run_text(["ss", "-tlnH", "( sport = :%d )" % RDP_PORT])
    return None if t is None else parse_listening(t)


def cgroup_of(pid, proc="/proc"):
    try:
        with open("%s/%d/cgroup" % (proc, int(pid))) as f:
            return parse_cgroup(f.read(8192))
    except (OSError, ValueError):
        return None


def boot_id(path="/proc/sys/kernel/random/boot_id"):
    try:
        with open(path) as f:
            return f.read(128).strip()[:64]
    except OSError:
        return ""


def terminate(sid):
    """True when logind took `loginctl terminate-session <id>`."""
    try:
        return subprocess.run(["loginctl", "terminate-session", sid], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def real_probes():
    return {"installed": grd_installed, "sessions": list_sessions, "show": show_session, "conns": connections,
            "listening": listening, "owner": cgroup_of, "boot": boot_id, "terminate": terminate}


# ---- one pass ---------------------------------------------------------------------

def _clean_seen(d):
    d = d if isinstance(d, dict) else {}
    return {k: float(v) for k, v in d.items()
            if isinstance(k, str) and SID_RX.match(k) and isinstance(v, (int, float)) and not isinstance(v, bool)}


def line(a):
    """One log line: the session id, its class, what was done and why, and what the per-connection view says
    (no names, no uids, no addresses)."""
    s = "session %s (%s): %s: %s" % (a["id"], a["class"], a["do"], a["reason"])
    return s + ("; per-connection view: %s" % view_word(a["view"]) if "view" in a else "")


class Tidy:
    """One pass, as the timer's service runs it (or a dry run, which ends nothing and writes nothing).
    `probes` as real_probes(); `log` gets the lines worth keeping: what changed, and every session ended."""

    def __init__(self, probes, cfg, log=print, clock=time.time, path=None):
        self.p, self.cfg, self.log, self.clock = probes, cfg, log, clock
        self.path = path or state_file()

    def _probe(self, name, *a):
        try:
            return self.p[name](*a)
        except Exception:
            return None

    def gather(self):
        """{sid: {"class", "uid"}} of the remote display sessions, or None when logind couldn't be asked."""
        ids = self._probe("sessions")
        if ids is None:
            return None
        out = {}
        for sid in ids:
            props = self._probe("show", sid)
            if props is None:
                continue                             # gone since the list, or unreadable: never touched
            cls = remote_display(props)
            if cls and props.get("Id", sid) == sid:
                out[sid] = {"class": cls, "uid": props.get("User", "")}
        return out

    def run(self, dry_run=False):
        """Returns (actions, summary). Dry run: says what it would do, ends nothing, saves nothing."""
        now = self.clock()
        st = read_json_safe(self.path, {}, max_bytes=65536)
        st = st if isinstance(st, dict) else {}
        boot = self._probe("boot") or ""
        seen = _clean_seen(st.get("seen")) if st.get("boot") == boot else {}    # ids start again after a reboot
        on, minutes, note = settings(self.cfg)
        acts, conns, view = [], None, {}
        if not on:
            summary = note or "off (rdp_tidy is false in config.json)"
            seen = {}
        elif not self._probe("installed"):
            summary, seen = "gnome-remote-desktop isn't installed: nothing to do", {}
        else:
            sessions = self.gather()
            if sessions is None:
                summary = "couldn't ask logind for the sessions: nothing ended"
            else:
                cl = self._probe("conns")
                conns = None if cl is None else len(cl)
                lis = self._probe("listening") if cl is not None else None
                acts, seen = plan(sessions, conns, lis, seen, now, minutes)
                if cl is not None and sessions:
                    view = per_session_view(cl, lambda pid: self._probe("owner", pid), sessions)
                summary = ("%d remote session%s, %s" % (len(sessions), "" if len(sessions) == 1 else "s",
                           "RDP connections unknown" if conns is None else "%d RDP connection%s" % (
                               conns, "" if conns == 1 else "s")))
        final = []
        for a in acts:
            if a["id"] in view:
                a = dict(a, view=view[a["id"]])
            if a["do"] == "end" and not dry_run:
                ok = bool(self._probe("terminate", a["id"]))
                a = dict(a, do="ended" if ok else "end failed")
                if ok:
                    seen.pop(a["id"], None)
            elif a["do"] == "end":
                a = dict(a, do="would end")
            final.append(a)
        acts = final
        lines = [line(a) for a in acts]
        # what was said last time, without the minutes that tick up while a session waits
        said = "\n".join([summary] + [re.sub(r"\d+ (of \d+ )?minutes", "", x) for x in lines
                                       if ": ended: " not in x and ": end failed: " not in x])
        if dry_run:
            self.log("dry run: " + summary)
            for x in lines:
                self.log(x)
            return acts, summary
        if said != st.get("said"):                   # a oneshot every minute: only what changed reaches the journal
            self.log(summary)
            for x in lines:
                if ": ended: " not in x and ": end failed: " not in x:
                    self.log(x)
        for x in lines:
            if ": ended: " in x or ": end failed: " in x:
                self.log(x)
        ended = sum(1 for x in lines if ": ended: " in x)
        out = {"boot": boot, "seen": seen, "said": said, "at": int(now), "summary": summary[:160],
               "connections": conns, "sessions": len(acts), "lines": [x[:200] for x in lines][:20]}
        if ended:
            out["last_ended"] = {"at": int(now), "count": ended}
        elif isinstance(st.get("last_ended"), dict):
            out["last_ended"] = st["last_ended"]
        try:
            write_json_atomic(self.path, out, mode=0o600)
        except OSError:
            self.log("couldn't save %s" % self.path)
        return acts, summary
