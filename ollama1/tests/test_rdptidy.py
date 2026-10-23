"""Remote desktop cleanup (6b506): the parsers on fixture outputs of loginctl,
ss and /proc/<pid>/cgroup, which sessions may ever be considered, the
decision, and a whole pass on a fake clock with every probe faked."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import o1common
import o1rdptidy as R

NOW = 1_800_000_000.0

# `loginctl -j list-sessions` on the server (systemd 259), shaped like the stale state that was seen: the local
# greeter on seat0, two remote login screens, a remote desktop, an SSH login and the user's manager session.
LIST_JSON = json.dumps([
    {"session": "c1", "uid": 120, "user": "gdm", "seat": "seat0", "leader": 1201, "class": "greeter", "tty": "tty1",
     "idle": False, "since": None},
    {"session": "4", "uid": 60580, "user": "gdm-greeter-1", "seat": None, "leader": 3301, "class": "greeter",
     "tty": None, "idle": False, "since": None},
    {"session": "6", "uid": 60581, "user": "gdm-greeter-2", "seat": None, "leader": 3402, "class": "greeter",
     "tty": None, "idle": False, "since": None},
    {"session": "9", "uid": 1000, "user": "alice", "seat": None, "leader": 4410, "class": "user", "tty": None,
     "idle": False, "since": None},
    {"session": "11", "uid": 1000, "user": "alice", "seat": None, "leader": 5120, "class": "user", "tty": "pts/0",
     "idle": False, "since": None},
    {"session": "12", "uid": 1000, "user": "alice", "seat": None, "leader": 4398, "class": "manager", "tty": None,
     "idle": False, "since": None},
])
LIST_PLAIN = """c1  120 gdm           seat0 1201 greeter tty1  no -
4 60580 gdm-greeter-1 -     3301 greeter -     no -
9  1000 alice         -     4410 user    -     no -
"""


def show(**kw):
    """`loginctl show-session <id> -p ...` output."""
    return "".join("%s=%s\n" % (k, v) for k, v in kw.items())


SHOW = {
    "c1": show(Id="c1", User=120, Class="greeter", Type="wayland", Service="gdm-launch-environment", Remote="no",
               Seat="seat0", TTY="tty1", State="active"),
    "4": show(Id="4", User=60580, Class="greeter", Type="wayland", Service="gdm-launch-environment", Remote="yes",
              Seat="", TTY="", State="active"),
    "6": show(Id="6", User=60581, Class="greeter", Type="wayland", Service="gdm-launch-environment", Remote="yes",
              Seat="", TTY="", State="active"),
    "9": show(Id="9", User=1000, Class="user", Type="wayland", Service="gdm-password", Remote="yes", Seat="", TTY="",
              State="active"),
    "11": show(Id="11", User=1000, Class="user", Type="tty", Service="sshd", Remote="yes", Seat="", TTY="pts/0",
               State="active"),
    "12": show(Id="12", User=1000, Class="manager", Type="unspecified", Service="", Remote="no", Seat="", TTY="",
               State="active"),
}

# `ss -tnpH state established '( sport = :3389 )'` (no State column with one state filter)
SS_ONE = ("0      0      [::ffff:10.0.0.2]:3389   [::ffff:10.0.0.50]:51734 "
          'users:(("gnome-remote-de",pid=4471,fd=31))\n')
SS_TWO = SS_ONE + "0      0      10.0.0.2:3389   10.0.0.51:40022 users:((\"gnome-remote-de\",pid=3350,fd=29))\n"
SS_LISTEN = "LISTEN 0      10           *:3389            *:*\n"

CG_SESSION9 = "0::/user.slice/user-1000.slice/session-9.scope\n"
CG_HANDOVER = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/gnome-remote-desktop-handover.service\n"
CG_GREETER6 = "0::/user.slice/user-60581.slice/session-6.scope\n"
CG_SYSTEM = "0::/system.slice/gnome-remote-desktop.service\n"


class TestParsers(unittest.TestCase):
    def test_list_sessions_json(self):
        self.assertEqual(R.parse_list_sessions(LIST_JSON), ["c1", "4", "6", "9", "11", "12"])

    def test_list_sessions_plain_table(self):
        self.assertEqual(R.parse_list_sessions(LIST_PLAIN), ["c1", "4", "9"])

    def test_list_sessions_junk(self):
        self.assertEqual(R.parse_list_sessions(""), [])
        self.assertEqual(R.parse_list_sessions('[{"session": "../x"}, {"session": true}, 7, {"id": 5}]'), ["5"])
        self.assertEqual(R.parse_list_sessions("[not json"), [])
        self.assertEqual(R.parse_list_sessions('[{"session": "-h"}, {"session": "_1"}, {"session": "c2"}]'), ["c2"])

    def test_show_session_keeps_only_asked_props(self):
        p = R.parse_show_session(SHOW["9"] + "Name=alice\nRemoteHost=10.0.0.50\n")
        self.assertEqual(p["Class"], "user")
        self.assertEqual(p["Seat"], "")
        self.assertNotIn("Name", p)
        self.assertNotIn("RemoteHost", p)

    def test_ss_established(self):
        self.assertEqual(R.parse_ss(SS_ONE), [{"pids": [4471]}])
        self.assertEqual(len(R.parse_ss(SS_TWO)), 2)
        self.assertEqual(R.parse_ss(""), [])
        # a line with the State column, and one with no process (ss run without root)
        self.assertEqual(R.parse_ss("ESTAB 0 0 10.0.0.2:3389 10.0.0.9:5000\n"), [{"pids": []}])
        # another local port isn't counted (the filter should have kept it out; the parser checks again)
        self.assertEqual(R.parse_ss("0 0 10.0.0.2:22 10.0.0.9:3389\n"), [])

    def test_ss_listening(self):
        self.assertTrue(R.parse_listening(SS_LISTEN))
        self.assertTrue(R.parse_listening("0 10 0.0.0.0:3389 0.0.0.0:*\n"))
        self.assertFalse(R.parse_listening(""))
        self.assertFalse(R.parse_listening("LISTEN 0 10 *:3390 *:*\n"))

    def test_cgroup(self):
        self.assertEqual(R.parse_cgroup(CG_SESSION9), ("session", "9"))
        self.assertEqual(R.parse_cgroup(CG_GREETER6), ("session", "6"))
        self.assertEqual(R.parse_cgroup(CG_HANDOVER), ("uid", 1000))
        self.assertIsNone(R.parse_cgroup(CG_SYSTEM))
        self.assertIsNone(R.parse_cgroup(""))
        # cgroup v1 lines: the unified (0::) one wins
        self.assertEqual(R.parse_cgroup("12:pids:/user.slice\n0::/user.slice/user-60580.slice/session-4.scope\n"),
                         ("session", "4"))


class TestWhichSessions(unittest.TestCase):
    def cls(self, sid):
        return R.remote_display(R.parse_show_session(SHOW[sid]))

    def test_the_fixture(self):
        self.assertEqual({s: self.cls(s) for s in SHOW},
                         {"c1": None, "4": "greeter", "6": "greeter", "9": "user", "11": None, "12": None})

    def test_every_condition_must_hold(self):
        base = R.parse_show_session(SHOW["9"])
        self.assertEqual(R.remote_display(base), "user")
        for change in (dict(Remote="no"), dict(Remote=""), dict(Type="x11"), dict(Type="tty"), dict(Service="sshd"),
                       dict(Service="login"), dict(Seat="seat0"), dict(TTY="tty2"), dict(Class="manager"),
                       dict(Class="background"), dict(Class="lock-screen"), dict(Class="")):
            self.assertIsNone(R.remote_display(dict(base, **change)), change)
        for missing in ("Remote", "Type", "Service", "Seat", "TTY", "Class"):
            p = dict(base)
            del p[missing]
            self.assertIsNone(R.remote_display(p), missing)
        self.assertIsNone(R.remote_display(None))


class TestSettings(unittest.TestCase):
    def test_defaults_on_30(self):
        self.assertEqual(R.settings({}), (True, 30, ""))
        d = o1common.DEFAULTS
        self.assertIs(d["rdp_tidy"], True)
        self.assertEqual(d["rdp_tidy_minutes"], 30)

    def test_off_and_odd_values(self):
        self.assertEqual(R.settings({"rdp_tidy": False})[:2], (False, 30))
        on, _m, note = R.settings({"rdp_tidy": "false"})
        self.assertFalse(on)
        self.assertIn("isn't true or false", note)
        self.assertFalse(R.settings({"rdp_tidy": 1})[0])
        self.assertEqual(R.settings({"rdp_tidy_minutes": 1})[1], 5)
        self.assertEqual(R.settings({"rdp_tidy_minutes": 99999})[1], 1440)
        self.assertEqual(R.settings({"rdp_tidy_minutes": True})[1], 30)
        self.assertEqual(R.settings({"rdp_tidy_minutes": "45"})[1], 30)
        self.assertEqual(R.settings({"rdp_tidy_minutes": 45})[1], 45)

    def test_load_config_keeps_the_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"rdp_tidy": False, "rdp_tidy_minutes": 60}, f)
        try:
            cfg = o1common.load_config(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(R.settings(cfg)[:2], (False, 60))


SESS = {"4": {"class": "greeter", "uid": "60580"}, "9": {"class": "user", "uid": "1000"}}


def dos(acts):
    return {a["id"]: a["do"] for a in acts}


class TestPlan(unittest.TestCase):
    def test_any_connection_keeps_everything_and_clears_the_clocks(self):
        acts, seen = R.plan(SESS, 1, True, {"4": NOW - 999, "9": NOW - 9999}, NOW, 30)
        self.assertEqual(dos(acts), {"4": "keep", "9": "keep"})
        self.assertEqual(seen, {})
        self.assertIn("1 RDP connection open", acts[0]["reason"])

    def test_unknown_connections_end_nothing_and_keep_the_clocks(self):
        acts, seen = R.plan(SESS, None, None, {"9": NOW - 9999}, NOW, 30)
        self.assertEqual(dos(acts), {"4": "keep", "9": "keep"})
        self.assertEqual(seen, {"9": NOW - 9999})

    def test_nothing_listening_ends_nothing(self):
        for lis in (False, None):
            acts, _ = R.plan(SESS, 0, lis, {"4": NOW - 999, "9": NOW - 9999}, NOW, 30)
            self.assertEqual(dos(acts), {"4": "keep", "9": "keep"}, lis)

    def test_greeter_on_the_second_look(self):
        acts, seen = R.plan(SESS, 0, True, {}, NOW, 30)
        self.assertEqual(dos(acts), {"4": "wait", "9": "wait"})
        self.assertEqual(seen, {"4": NOW, "9": NOW})
        acts, seen = R.plan(SESS, 0, True, seen, NOW + 30, 30)
        self.assertEqual(dos(acts)["4"], "wait")
        acts, _ = R.plan(SESS, 0, True, seen, NOW + R.GREETER_GRACE_S, 30)
        self.assertEqual(dos(acts), {"4": "end", "9": "wait"})

    def test_user_after_the_minutes(self):
        seen = {"9": NOW}
        acts, _ = R.plan(SESS, 0, True, seen, NOW + 29 * 60 + 59, 30)
        self.assertEqual(dos(acts)["9"], "wait")
        self.assertIn("29 of 30 minutes", acts[1]["reason"])
        acts, _ = R.plan(SESS, 0, True, seen, NOW + 30 * 60, 30)
        self.assertEqual(dos(acts)["9"], "end")
        acts, _ = R.plan(SESS, 0, True, seen, NOW + 10 * 60, 10)
        self.assertEqual(dos(acts)["9"], "end")

    def test_a_future_or_odd_timestamp_starts_now(self):
        for bad in (NOW + 5000, True, "x", None):
            acts, seen = R.plan(SESS, 0, True, {"4": bad, "9": bad}, NOW, 30)
            self.assertEqual(dos(acts), {"4": "wait", "9": "wait"}, bad)
            self.assertEqual(seen, {"4": NOW, "9": NOW})

    def test_gone_sessions_drop_out(self):
        _, seen = R.plan({"9": SESS["9"]}, 0, True, {"4": NOW - 50, "9": NOW - 50}, NOW, 30)
        self.assertEqual(seen, {"9": NOW - 50})


class TestPerSessionView(unittest.TestCase):
    def test_views(self):
        conns = R.parse_ss(SS_ONE)
        owner = {4471: ("session", "9")}.get
        self.assertEqual(R.per_session_view(conns, owner, SESS), {"4": False, "9": True})
        owner = {4471: ("uid", 1000)}.get                 # the handover daemon under the user's systemd
        self.assertEqual(R.per_session_view(conns, owner, SESS), {"4": False, "9": True})
        self.assertEqual(R.per_session_view(conns, lambda p: None, SESS), {"4": None, "9": None})
        self.assertEqual(R.per_session_view([], lambda p: None, SESS), {"4": False, "9": False})


class Fake:
    """Probes for Tidy: the fixture outputs, changeable per pass."""

    def __init__(self):
        self.installed = True
        self.list = LIST_JSON
        self.show = dict(SHOW)
        self.ss = ""
        self.listen = SS_LISTEN
        self.cg = {4471: CG_SESSION9, 3350: CG_GREETER6}
        self.boot = "boot-a"
        self.ended = []
        self.term_ok = True
        self.asked = []

    def probes(self):
        def sessions():
            self.asked.append("sessions")
            return None if self.list is None else R.parse_list_sessions(self.list)

        def show_(sid):
            t = self.show.get(sid)
            return None if t is None else R.parse_show_session(t)

        def term(sid):
            self.ended.append(sid)
            return self.term_ok
        return {"installed": lambda: self.installed, "sessions": sessions, "show": show_,
                "conns": lambda: None if self.ss is None else R.parse_ss(self.ss),
                "listening": lambda: None if self.listen is None else R.parse_listening(self.listen),
                "owner": lambda pid: R.parse_cgroup(self.cg.get(pid, "")), "boot": lambda: self.boot,
                "terminate": term}


class TestPass(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=U.PREFIX)
        self.path = os.path.join(self.dir, "rdp-tidy.json")
        self.f = Fake()
        self.logs = []
        self.now = NOW
        self.cfg = {}

    def run_once(self, dry=False):
        t = R.Tidy(self.f.probes(), self.cfg, log=self.logs.append, clock=lambda: self.now, path=self.path)
        return t.run(dry_run=dry)

    def text(self):
        with open(self.path) as f:
            return f.read()

    def state(self):
        return json.loads(self.text())

    def test_the_owner_s_stale_server(self):
        # the state that was seen: two remote login screens and a remote desktop, no client connected
        self.run_once()
        self.assertEqual(self.f.ended, [])
        self.now += 60
        self.run_once()
        self.assertEqual(self.f.ended, ["4", "6"])              # login screens on the second look
        self.f.show.pop("4"), self.f.show.pop("6")              # logind dropped them
        self.now += 28 * 60
        self.run_once()
        self.assertEqual(self.f.ended, ["4", "6"])
        self.now += 60
        self.run_once()
        self.assertEqual(self.f.ended, ["4", "6", "9"])         # the desktop 30 minutes after it was first seen
        self.assertEqual(self.state()["last_ended"], {"at": int(self.now), "count": 1})

    def test_never_the_local_greeter_ssh_or_manager(self):
        for _ in range(40):
            self.run_once()
            self.now += 60
        self.assertEqual(sorted(set(self.f.ended)), ["4", "6", "9"])

    def test_a_connected_client_keeps_everything(self):
        self.f.ss = SS_ONE
        for _ in range(40):
            self.run_once()
            self.now += 60
        self.assertEqual(self.f.ended, [])
        self.assertEqual(self.state()["seen"], {})

    def test_a_reconnect_resets_the_desktop_clock(self):
        self.f.show.pop("4"), self.f.show.pop("6")
        self.run_once()
        self.now += 20 * 60
        self.f.ss = SS_ONE
        self.run_once()                                         # the client came back
        self.f.ss = ""
        self.now += 60
        self.run_once()
        self.now += 20 * 60
        self.run_once()
        self.assertEqual(self.f.ended, [])                      # 41 minutes since first seen, 20 since the reconnect
        self.now += 10 * 60
        self.run_once()
        self.assertEqual(self.f.ended, ["9"])

    def test_ss_failing_ends_nothing(self):
        self.f.ss = None
        for _ in range(40):
            self.run_once()
            self.now += 60
        self.assertEqual(self.f.ended, [])
        self.assertTrue(any("couldn't list the RDP connections" in x for x in self.logs))

    def test_nothing_listening_ends_nothing(self):
        self.f.listen = ""
        for _ in range(40):
            self.run_once()
            self.now += 60
        self.assertEqual(self.f.ended, [])
        self.assertTrue(any("nothing listens on :3389" in x for x in self.logs))

    def test_logind_failing_ends_nothing(self):
        self.f.list = None
        self.run_once()
        self.now += 60
        self.run_once()
        self.assertEqual(self.f.ended, [])
        self.assertIn("couldn't ask logind", self.logs[0])

    def test_a_session_that_vanished_mid_pass_is_skipped(self):
        del self.f.show["4"]
        self.run_once()
        self.now += 60
        self.run_once()
        self.assertEqual(self.f.ended, ["6"])

    def test_not_installed_or_off_does_nothing(self):
        self.f.installed = False
        self.run_once()
        self.now += 3600
        self.run_once()
        self.assertEqual(self.f.ended, [])
        self.assertEqual(self.f.asked, [])                      # logind not even asked
        self.assertIn("isn't installed", self.logs[0])
        self.f.installed = True
        self.cfg = {"rdp_tidy": False}
        self.run_once()
        self.now += 3600
        self.run_once()
        self.assertEqual(self.f.ended, [])
        self.assertEqual(self.f.asked, [])
        self.assertTrue(any("off" in x for x in self.logs))

    def test_a_reboot_forgets_the_clocks(self):
        self.f.show.pop("4"), self.f.show.pop("6")
        self.run_once()
        self.now += 29 * 60
        self.f.boot = "boot-b"                                  # session ids start again after a reboot
        self.run_once()
        self.now += 2 * 60
        self.run_once()
        self.assertEqual(self.f.ended, [])
        self.assertEqual(self.state()["seen"], {"9": NOW + 29 * 60})

    def test_dry_run_ends_nothing_and_saves_nothing(self):
        self.run_once()
        before = self.text()
        self.now += 31 * 60
        acts, _ = self.run_once(dry=True)
        self.assertEqual(self.f.ended, [])
        self.assertEqual(self.text(), before)
        self.assertEqual({a["id"]: a["do"] for a in acts}, {"4": "would end", "6": "would end", "9": "would end"})
        self.assertTrue(self.logs[-4].startswith("dry run: 3 remote sessions, 0 RDP connections"))
        self.assertIn("session 9 (user): would end: remote desktop, no RDP client for 31 minutes; "
                      "per-connection view: not connected", self.logs)

    def test_dry_run_without_state(self):
        acts, _ = self.run_once(dry=True)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual({a["id"]: a["do"] for a in acts}, {"4": "wait", "6": "wait", "9": "wait"})

    def test_failed_terminate_is_said_and_tried_again(self):
        self.f.term_ok = False
        self.run_once()
        self.now += 60
        self.run_once()
        self.now += 60
        self.run_once()
        self.assertEqual(self.f.ended, ["4", "6", "4", "6"])
        self.assertTrue(any(": end failed: " in x for x in self.logs))
        self.assertIn("4", self.state()["seen"])

    def test_logs_only_what_changed(self):
        self.f.show.pop("4"), self.f.show.pop("6")
        self.run_once()
        n = len(self.logs)
        self.assertGreater(n, 0)
        for _ in range(5):
            self.now += 60
            self.run_once()                                     # the same wait, the minutes ticking: quiet
        self.assertEqual(len(self.logs), n)
        self.f.ss = SS_ONE
        self.run_once()
        self.assertGreater(len(self.logs), n)

    def test_per_connection_view_is_logged_not_obeyed(self):
        # a client is connected to session 6 (by its cgroup): the v1 rule keeps everything, the log says what a
        # per-session rule would have concluded
        self.f.ss = SS_TWO.splitlines()[1] + "\n"
        self.run_once()
        self.now += 3600
        self.run_once()
        self.assertEqual(self.f.ended, [])
        text = "\n".join(self.logs)
        self.assertIn("session 6 (greeter): keep: 1 RDP connection open; per-connection view: connected", text)
        self.assertIn("session 4 (greeter): keep: 1 RDP connection open; per-connection view: not connected", text)

    def test_no_personal_data_in_logs_or_state(self):
        self.f.ss = ""
        for _ in range(35):
            self.run_once()
            self.now += 60
        self.f.ss = SS_TWO
        self.run_once()
        out = "\n".join(self.logs) + self.text()
        for bad in ("alice", "gdm-greeter", "10.0.0.", "pid", "uid"):
            self.assertNotIn(bad, out, bad)
        for uid in ("1000", "60580", "60581", "120"):
            self.assertNotRegex(out, r"(?<![0-9.])%s(?![0-9.])" % uid, uid)
        for x in self.logs:
            self.assertRegex(x, r"^(session [A-Za-z0-9_-]+ \((greeter|user)\): [a-z ]+: |\d+ remote session|"
                                r"couldn't|gnome-remote-desktop|off)")

    def test_state_file_is_private(self):
        self.run_once()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


class TestKit(unittest.TestCase):
    def test_unit_and_timer(self):
        with open(os.path.join(U.SYSTEMD, "ollama1-rdp-tidy.service")) as f:
            unit = f.read()
        for line in ("Type=oneshot", "ExecStart=/usr/local/lib/ollama1/bin/ollama1-rdp-tidy run", "NoNewPrivileges=yes",
                     "ProtectSystem=strict", "ReadWritePaths=/var/lib/ollama1\n", "ProtectHome=yes", "PrivateTmp=yes",
                     "PrivateDevices=yes", "IPAddressDeny=any", "MemoryMax=64M"):
            self.assertIn(line, unit)
        self.assertEqual(unit.count("ReadWritePaths="), 1)
        self.assertNotIn("[Install]", unit)                     # the timer starts it
        with open(os.path.join(U.SYSTEMD, "ollama1-rdp-tidy.timer")) as f:
            timer = f.read()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_setup_enables_the_timer(self):
        with open(os.path.join(U.KIT, "setup.sh")) as f:
            s = f.read()
        self.assertIn("run systemctl enable --now ollama1-rdp-tidy.timer", s)
        self.assertIn('ln -sfn "$LIBDIR/bin/ollama1-rdp-tidy" /usr/local/sbin/ollama1-rdp-tidy', s)

    def test_commands_run(self):
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX)
        cfg = os.path.join(U.PREFIX, "etc", "ollama1", "config.json")
        made = not os.path.exists(cfg)
        if made:
            with open(cfg, "w") as f:
                json.dump({"rdp_tidy": False}, f)
        try:
            for args, want in ((["--dry-run"], r"dry run: "), (["status"], r"remote desktop cleanup: ")):
                r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-rdp-tidy")] + args, env=env,
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertRegex(r.stdout, want)
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-rdp-tidy"), "nonsense"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
        finally:
            if made:
                os.unlink(cfg)

    def test_readme_and_doc(self):
        with open(os.path.join(U.KIT, "README.md")) as f:
            readme = f.read()
        self.assertIn("## Remote desktop cleanup", readme)
        for word in ("rdp_tidy", "rdp_tidy_minutes", "--dry-run", "terminate-session"):
            self.assertIn(word, readme)
        with open(os.path.join(U.BIN, "ollama1-rdp-tidy")) as f:
            self.assertTrue(re.search(r"--dry-run", f.read()))


if __name__ == "__main__":
    unittest.main()
