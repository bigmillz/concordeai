"""The SSH door (6b507): SSH through the Cloudflare Tunnel, behind Access and
then sshd's keys, off unless asked for. With it off, every file setup writes
is exactly what it was before the door existed; with it on, the tunnel gets
one ssh:// route behind Access, sshd takes the admin user from 127.0.0.1 with
keys only, only cloudflared may connect there, and no port opens. Its Access
app is made by ollama1-cf-access against a fake API (never the real one)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import test_cloudflare as TC

sys.path.insert(0, U.LIB)
from o1common import access_names, resolve  # noqa: E402

LIBSH = os.path.join(U.LIB, "setuplib.sh")
YML = os.path.join(U.CONFIG, "cloudflared.yml.in")
NFT = os.path.join(U.CONFIG, "ollama1.nft.in")
SSHD = os.path.join(U.CONFIG, "10-ollama1-sshd.conf.in")
SSH_HOST = U.SERVER_NAME + "-ssh." + U.ZONE
SSH_AUD = "c" * 64
PW_OFF = "PasswordAuthentication no\nKbdInteractiveAuthentication no\nAuthenticationMethods publickey"


def sh(script, *args):
    """Run bash with setuplib.sh sourced; the script's own arguments are $1..."""
    r = subprocess.run(["bash", "-c", '. "$0"; ' + script, LIBSH] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("bash failed: %s" % r.stderr)
    return r.stdout


def without_door(text):
    """A template as it was before the door: its door lines taken out (done here in Python, not by the awk)."""
    out, skip = [], False
    for line in text.splitlines(True):
        if "@SSH_DOOR_BEGIN@" in line:
            skip = True
            continue
        if "@SSH_DOOR_END@" in line:
            skip = False
            continue
        if skip or "#ssh-door# " in line or line.strip() == "@SSH_DOOR_LINES@":
            continue
        out.append(line)
    return "".join(out)


def old_yml(tid, team, gaud, aaud, gw, admin):
    """What setup.sh's sed made of the template before the door."""
    t = without_door(open(YML).read())
    for k, v in (("@TUNNEL_ID@", tid), ("@GATEWAY_AUD@", gaud), ("@ADMIN_AUD@", aaud), ("@GW_HOST@", gw),
                 ("@ADMIN_HOST@", admin)):
        t = t.replace(k, v, 1)
    return t.replace("@TEAM_NAME@", team)


def old_sshd(pw, who):
    """What setup.sh's awk made of the drop-in before the door."""
    out = []
    for line in without_door(open(SSHD).read()).splitlines():
        out.append(pw if line == "@PASSWORD_LINES@" else ("AllowUsers " + who if line == "AllowUsers @ADMIN_USER_AT_LAN@"
                                                          else line))
    return "\n".join(out) + "\n"


YARGS = ["tunnel-id", "o1test", U.GW_AUD, U.ADMIN_AUD, U.GW_HOST, U.ADMIN_HOST]


class TestTunnelConfig(unittest.TestCase):
    def render(self, *door):
        return sh('render_tunnel_yml "$@"', YML, *(YARGS + list(door)))

    def test_door_off_is_what_it_was(self):
        want = old_yml(*YARGS)
        self.assertEqual(self.render(), want)
        self.assertEqual(self.render("", ""), want)
        self.assertEqual(self.render(SSH_HOST, ""), want)       # no AUD yet: no route
        self.assertEqual(self.render("", SSH_AUD), want)
        self.assertNotIn("ssh", want)
        self.assertNotIn("@", want)                                  # every placeholder filled

    def test_door_on_adds_one_route_behind_access_before_the_catch_all(self):
        out = self.render(SSH_HOST, SSH_AUD)
        self.assertNotIn("@", out)
        self.assertNotIn("SSH_DOOR", out)
        rules = re.split(r"\n  - ", out.split("ingress:\n", 1)[1])
        self.assertEqual(len(rules), 4)
        ssh = rules[2]
        self.assertIn("hostname: %s\n" % SSH_HOST, ssh)
        self.assertIn("service: ssh://127.0.0.1:22\n", ssh)
        self.assertIn("required: true", ssh)
        self.assertIn("teamName: o1test", ssh)
        self.assertIn('audTag: ["%s"]' % SSH_AUD, ssh)
        self.assertTrue(rules[3].startswith("service: http_status:404"))
        # the gateway's and the panel's rules are untouched
        self.assertEqual(out.replace("\n  - " + ssh, "", 1), old_yml(*YARGS))

    def test_setup_renders_through_the_function_and_checks_the_values(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('render_tunnel_yml "$KIT/config/cloudflared.yml.in"', s)
        self.assertNotIn('"$KIT/config/cloudflared.yml.in" >/etc/ollama1/cloudflared.yml', s)
        sec = s[s.index("# the SSH door's route (6b507)"):s.index("render_tunnel_yml \"$KIT")]
        self.assertIn('[ "$SSH_DOOR" = on ] && [ "${SSHD_DOOR:-off}" = on ]', sec)   # sshd must take it, keys only
        self.assertIn('! valid_zone "$door_host" || ! valid_aud "$door_aud"', sec)


class TestPortGuard(unittest.TestCase):
    def test_door_off_is_what_it_was(self):
        t = open(NFT).read()
        self.assertEqual(sh('ssh_door_lines off <"$1"', NFT), without_door(t))
        self.assertNotIn("dport 22", without_door(t))

    def test_door_on_lets_only_cloudflared_reach_sshd_from_loopback(self):
        out = sh('ssh_door_lines on <"$1"', NFT)
        self.assertNotIn("#ssh-door#", out)
        rules = [l.strip() for l in out.splitlines() if "dport 22 " in l]
        self.assertEqual(rules, ["tcp dport 22 fib daddr type local ip saddr 127.0.0.0/8 meta skuid != "
                                 "{ 0, @UID_CLOUDFLARED@ } counter reject with tcp reset"])
        body = out[out.index("chain output"):]
        self.assertLess(body.index("dport 22 "), body.index("    }"))             # inside the chain
        self.assertEqual(out.count("{"), out.count("}"))

    def test_setup_filters_the_guard(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('"$KIT/config/ollama1.nft.in" | ssh_door_lines "$SSH_DOOR" >/etc/ollama1/ollama1.nft', s)


class TestSshd(unittest.TestCase):
    def render(self, pw, who, door):
        return sh('render_sshd_dropin "$@"', SSHD, pw, who, door)

    def test_door_off_is_what_it_was(self):
        for pw in (PW_OFF, "# Password login stays on until ..."):
            self.assertEqual(self.render(pw, "alice@10.0.0.0/24", ""), old_sshd(pw, "alice@10.0.0.0/24"))

    def test_door_on_keys_only_from_loopback(self):
        door = sh("ssh_door_sshd_lines yes")
        out = self.render(PW_OFF, "alice@10.0.0.0/24 alice@127.0.0.1", door.rstrip("\n"))
        self.assertIn("\nAllowUsers alice@10.0.0.0/24 alice@127.0.0.1\n", out)
        for line in ("PasswordAuthentication no", "KbdInteractiveAuthentication no", "AuthenticationMethods publickey",
                     "PermitRootLogin no", "PerSourcePenaltyExemptList 127.0.0.1"):
            self.assertIn("\n" + line + "\n", out)
        self.assertNotIn("@", out.replace("alice@", ""))
        self.assertNotRegex(out, r"(?m)^\s*(MaxAuthTries|PasswordAuthentication yes|ListenAddress|Port )")

    def test_no_penalty_exemption_on_an_older_sshd(self):
        door = sh("ssh_door_sshd_lines no")
        self.assertNotIn("PerSourcePenalt", "\n".join(l for l in door.splitlines() if not l.startswith("#")))
        self.assertTrue(all(l.startswith("#") for l in door.splitlines()))

    def test_setup_opens_the_door_only_with_passwords_off(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        sec = s[s.index("# The SSH door (6b507): sshd also takes"):s.index('render_sshd_dropin "$KIT')]
        self.assertIn('if [ "$SSH_DOOR" = on ] && [ "$PW_OFF" = 1 ]; then', sec)
        self.assertIn('who="$who $ADMIN_USER@127.0.0.1"', sec)
        self.assertIn("pw=$'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\nAuthenticationMethods publickey'", s)
        # sshd -T is asked what it will really do for a door connection
        self.assertIn('addr=127.0.0.1,laddr=127.0.0.1,lport=22', s)
        for want in ('grep -qix "allowusers $ADMIN_USER@127.0.0.1"', 'grep -qx "passwordauthentication no"',
                     'grep -qx "kbdinteractiveauthentication no"', 'grep -qx "authenticationmethods publickey"'):
            self.assertIn(want, s)


class TestNothingElseOpens(unittest.TestCase):
    def test_firewall_unchanged(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        fw = s[s.index('step "Firewall"'):s.index('step "SSH"')]
        self.assertNotIn("SSH_DOOR", fw)
        self.assertNotIn("127.0.0.1", fw)
        allows = [l.strip() for l in fw.splitlines() if re.match(r"\s*run ufw allow", l)]
        self.assertEqual(allows[0], "run ufw allow in on \"$LANIF\" from \"$HOME_LAN\" to any port 22 proto tcp "
                                    "comment 'ollama1: SSH from the home LAN'")
        self.assertTrue(all("port 22" not in a for a in allows[1:]))

    def test_no_new_listener(self):
        # the door is a route inside the outbound tunnel to the sshd already there
        out = sh('render_tunnel_yml "$@"', YML, *(YARGS + [SSH_HOST, SSH_AUD]))
        services = re.findall(r"service: (\S+)", out)
        self.assertEqual(services, ["http://127.0.0.1:8431", "http://127.0.0.1:8432", "ssh://127.0.0.1:22",
                                    "http_status:404"])
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn("*:22) continue ;;", s)   # the listener check still allows sshd alone


class TestChoice(unittest.TestCase):
    def test_off_unless_asked(self):
        self.assertEqual(sh('ssh_door_choice "" ""'), "off\n")
        self.assertEqual(sh('ssh_door_choice "" true'), "on\n")      # config.json remembers it
        self.assertEqual(sh('ssh_door_choice off true'), "off\n")    # --no-ssh-door closes it
        self.assertEqual(sh('ssh_door_choice on ""'), "on\n")
        r = subprocess.run(["bash", "-c", '. "$0"; ssh_door_choice maybe ""', LIBSH], capture_output=True)
        self.assertNotEqual(r.returncode, 0)

    def test_aud_shape(self):
        self.assertEqual(sh('valid_aud "$1" && echo y', SSH_AUD), "y\n")
        for bad in ("C" * 64, "c" * 63, "c" * 65, "", "g" * 64):
            r = subprocess.run(["bash", "-c", '. "$0"; valid_aud "$1"', LIBSH, bad], capture_output=True)
            self.assertNotEqual(r.returncode, 0, bad)

    def test_flags_parse(self):
        setup = os.path.join(U.KIT, "setup.sh")
        for args, msg in ((["--ssh-door", "--no-ssh-door"], "give one"), (["--ssh-aud", "xyz"], "64 characters"),
                          (["--ssh-aud"], "AUD tag")):
            r = subprocess.run(["bash", setup] + args, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(msg, r.stdout)

    def test_config_derives_the_hostname_only_with_the_door_on(self):
        base = {"server_name": U.SERVER_NAME, "cf_zone": U.ZONE}
        self.assertFalse(resolve(dict(base)).get("hostname_ssh"))
        self.assertEqual(resolve(dict(base, ssh_door=True))["hostname_ssh"], SSH_HOST)
        self.assertEqual(resolve(dict(base, ssh_door=True, hostname_ssh="door.example.test"))["hostname_ssh"],
                         "door.example.test")
        self.assertEqual(access_names(base)["app_ssh"], "testsrv ssh")


class FakeCFNoSSHType(TC.FakeCF):
    """An API that refuses the "ssh" application type."""

    def route(self, m, p, qs, b):
        if b and b.get("type") == "ssh":
            return 400, "application type ssh is not allowed"
        return super().route(m, p, qs, b)


class FakeCFNoTypeChange(TC.FakeCF):
    """An API that won't change an application's type in place."""

    def route(self, m, p, qs, b):
        mm = re.match(r"/accounts/[^/]+/access/apps/([^/]+)$", p)
        if m == "PUT" and mm and b and self.apps.get(mm.group(1), {}).get("type") != b.get("type"):
            return 400, "the application type can't be changed"
        return super().route(m, p, qs, b)


class TestCfAccessDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cf = TC.FakeCF()

    @classmethod
    def tearDownClass(cls):
        cls.cf.srv.shutdown()

    def setUp(self):
        self.cf.reset()
        for f in (TC.CREDS, TC.CONFIG, TC.CF_STATE):
            if os.path.exists(f):
                os.unlink(f)
        os.makedirs(os.path.dirname(TC.CONFIG), exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="o1door-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_cfg(self, **kw):
        with open(TC.CONFIG, "w") as f:
            json.dump(dict(TC.BASE, **kw), f)

    def run_helper(self, cf=None, extra=()):
        cf = cf or self.cf
        tty = os.path.join(self.tmp, "tty")
        open(tty, "w").close()
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX, OLLAMA1_CF_API=cf.url, TMPDIR=self.tmp, OLLAMA1_TTY=tty)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-cf-access"), "--token-stdin", "--no-prompt"]
                           + list(extra), input=TC.TOKEN + "\n", env=env, capture_output=True, text=True, timeout=60)
        self.out = r.stdout + r.stderr
        return r.returncode

    def ssh_app(self, cf=None):
        apps = [a for a in (cf or self.cf).apps.values() if a["domain"] == SSH_HOST]
        self.assertEqual(len(apps), 1)
        return apps[0]

    def check_door(self, cf=None):
        cf = cf or self.cf
        cfg = json.load(open(TC.CONFIG))
        app = self.ssh_app(cf)
        self.assertEqual(app["name"], "testsrv ssh")
        self.assertEqual(cfg["ssh_aud"], app["aud"])
        self.assertIs(cfg["ssh_door"], True)
        self.assertEqual(cfg["hostname_ssh"], SSH_HOST)
        # the admin panel's policy: Allow, the admin email
        admin_app = next(a for a in cf.apps.values() if a["domain"] == TC.ADMIN)
        self.assertEqual(app["policies"], admin_app["policies"])
        pol = cf.policies[app["policies"][0]["id"]]
        self.assertEqual((pol["decision"], pol["include"]), ("allow", [{"email": {"email": U.ADMIN_EMAIL}}]))
        self.assertNotIn("service_auth_401_redirect", app)
        tid = next(iter(cf.tunnels))
        recs = [r for r in cf.dns.values() if r["name"] == SSH_HOST]
        self.assertEqual([(r["type"], r["content"], r["proxied"]) for r in recs],
                         [("CNAME", tid + ".cfargotunnel.com", True)])
        self.assertNotIn(TC.TOKEN, self.out)
        return app

    def test_door_on_in_config(self):
        self.write_cfg(ssh_door=True)
        self.assertEqual(self.run_helper(), 0, self.out)
        app = self.check_door()
        self.assertEqual(app["type"], "self_hosted")  # 6b510: not browser-rendered (its login page 404d)
        self.assertEqual(len(self.cf.apps), 3)
        self.assertEqual(len(self.cf.policies), 2)    # no new policy: the admin's is reused
        self.assertIn("Run setup.sh again", self.out)

    def test_door_flag_turns_it_on(self):
        self.write_cfg()
        self.assertEqual(self.run_helper(extra=["--ssh-door"]), 0, self.out)
        self.assertEqual(self.check_door()["type"], "self_hosted")

    def test_door_off_changes_nothing(self):
        self.write_cfg()
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual(sorted(a["domain"] for a in self.cf.apps.values()), sorted([TC.GW, TC.ADMIN]))
        self.assertEqual(len(self.cf.dns), 2)
        cfg = json.load(open(TC.CONFIG))
        for k in ("ssh_door", "hostname_ssh", "ssh_aud"):
            self.assertNotIn(k, cfg)
        self.assertTrue(all(a["type"] == "self_hosted" for a in self.cf.apps.values()))

    def test_closed_door_leaves_its_app_alone(self):
        self.write_cfg(ssh_door=True)
        self.assertEqual(self.run_helper(), 0, self.out)
        self.write_cfg(ssh_door=False, hostname_ssh=SSH_HOST, ssh_aud=json.load(open(TC.CONFIG))["ssh_aud"])
        n = len(self.cf.calls)
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertFalse([c for c in self.cf.calls[n:] if c[0] in ("POST", "DELETE", "PATCH")])
        self.assertIs(json.load(open(TC.CONFIG))["ssh_door"], False)

    def test_rerun_changes_nothing(self):
        self.write_cfg(ssh_door=True)
        self.assertEqual(self.run_helper(), 0, self.out)
        before = (json.load(open(TC.CONFIG)), json.dumps(self.cf.dns, sort_keys=True), len(self.cf.apps))
        n = len(self.cf.calls)
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual(before, (json.load(open(TC.CONFIG)), json.dumps(self.cf.dns, sort_keys=True),
                                  len(self.cf.apps)))
        self.assertFalse([c for c in self.cf.calls[n:] if c[0] in ("POST", "PATCH", "DELETE")])

    def test_ssh_type_refused_falls_back_to_self_hosted(self):
        cf = FakeCFNoSSHType()
        try:
            self.write_cfg(ssh_door=True)
            self.assertEqual(self.run_helper(cf), 0, self.out)
            app = self.check_door(cf)
            self.assertEqual(app["type"], "self_hosted")
        finally:
            cf.srv.shutdown()

    def test_door_host_outside_the_zone_stops_it(self):
        self.write_cfg(ssh_door=True, hostname_ssh="ssh.elsewhere.test")
        self.assertEqual(self.run_helper(), 1)
        self.assertIn("ssh.elsewhere.test is not in the zone", self.out)
        self.assertEqual(self.cf.calls, [])


    # ---- --ssh-door-only (6b509): an install whose Cloudflare side was made by hand under other names
    def writes(self, cf=None):
        return [(c[0], c[1]) for c in (cf or self.cf).calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")]

    def test_full_run_never_makes_a_second_tunnel(self):
        self.write_cfg(ssh_door=True, tunnel_id="hand-made-tunnel")
        self.assertEqual(self.run_helper(), 1)
        self.assertIn("--ssh-door-only", self.out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.cf.tunnels, {})

    def test_door_only_touches_only_the_door(self):
        self.write_cfg(tunnel_id="hand-made-tunnel", service_token_client_id="kept.access", admin_aud="kept-aud")
        self.assertEqual(self.run_helper(extra=["--ssh-door-only"]), 0, self.out)
        self.assertEqual((self.cf.tunnels, self.cf.stokens), ({}, {}))
        app = self.ssh_app()
        self.assertEqual((app["name"], app["type"]), ("testsrv ssh", "self_hosted"))
        self.assertEqual(len(self.cf.apps), 1)
        self.assertEqual([p["name"] for p in self.cf.policies.values()], ["testsrv ssh - admin only"])
        pol = next(iter(self.cf.policies.values()))
        self.assertEqual((pol["decision"], pol["include"]), ("allow", [{"email": {"email": U.ADMIN_EMAIL}}]))
        recs = [r for r in self.cf.dns.values() if r["name"] == SSH_HOST]
        self.assertEqual([(r["content"], r["proxied"]) for r in recs], [("hand-made-tunnel.cfargotunnel.com", True)])
        self.assertEqual(len(self.cf.dns), 1)
        cfg = json.load(open(TC.CONFIG))
        self.assertEqual((cfg["ssh_door"], cfg["hostname_ssh"], cfg["ssh_aud"]), (True, SSH_HOST, app["aud"]))
        self.assertEqual((cfg["tunnel_id"], cfg["service_token_client_id"], cfg["admin_aud"]),
                         ("hand-made-tunnel", "kept.access", "kept-aud"))
        self.assertNotIn(TC.TOKEN, self.out)
        n = len(self.writes())
        self.assertEqual(self.run_helper(extra=["--ssh-door-only"]), 0, self.out)   # again: brought in line only
        self.assertEqual((len(self.cf.apps), len(self.cf.policies), len(self.cf.dns)), (1, 1, 1))
        self.assertFalse([w for w in self.writes()[n:] if w[0] == "POST"])

    def test_door_only_leaves_a_foreign_app_alone(self):
        self.write_cfg(tunnel_id="hand-made-tunnel")
        self.cf.apps["x"] = {"id": "x", "name": "someone's app", "domain": SSH_HOST, "aud": "foreign"}
        self.assertEqual(self.run_helper(extra=["--ssh-door-only"]), 1)
        self.assertIn("that this didn't make", self.out)
        self.assertEqual(self.cf.apps["x"]["name"], "someone's app")
        self.assertFalse([w for w in self.writes() if "/apps" in w[1]])

    def test_door_only_needs_the_tunnel_id(self):
        self.write_cfg()
        self.assertEqual(self.run_helper(extra=["--ssh-door-only"]), 1)
        self.assertIn("no tunnel_id", self.out)
        self.assertEqual(self.writes(), [])

    def test_door_only_turns_a_browser_ssh_app_into_a_plain_one(self):
        # 6b510: the owner's first door app was type "ssh" and 404'd its login page
        self.write_cfg(tunnel_id="hand-made-tunnel")
        self.cf.apps["old"] = {"id": "old", "name": "testsrv ssh", "domain": SSH_HOST, "type": "ssh", "aud": "old-aud"}
        cf = FakeCFNoTypeChange()
        try:
            cf.apps["old"] = dict(self.cf.apps["old"])
            self.write_cfg(tunnel_id="hand-made-tunnel")
            self.assertEqual(self.run_helper(cf, extra=["--ssh-door-only"]), 0, self.out)
            app = self.ssh_app(cf)
            self.assertEqual(app["type"], "self_hosted")
            self.assertNotEqual(app["aud"], "old-aud")
            self.assertEqual(json.load(open(TC.CONFIG))["ssh_aud"], app["aud"])
            self.assertIn("making it again", self.out)
        finally:
            cf.srv.shutdown()

    def test_door_only_with_the_service_token(self):
        # 6b510: the app's service token (found by its Client ID, never made) opens the door too
        self.cf.stokens["t1"] = {"id": "t1", "name": "hand-made-app", "client_id": "kept.access", "client_secret": "s"}
        self.write_cfg(tunnel_id="hand-made-tunnel", service_token_client_id="kept.access")
        self.assertEqual(self.run_helper(extra=["--ssh-door-only", "--door-service-token"]), 0, self.out)
        app = self.ssh_app()
        pols = [self.cf.policies[r["id"]] for r in sorted(app["policies"], key=lambda r: r["precedence"])]
        self.assertEqual([(p["decision"], p["include"]) for p in pols],
                         [("allow", [{"email": {"email": U.ADMIN_EMAIL}}]),
                          ("non_identity", [{"service_token": {"token_id": "t1"}}])])
        self.assertEqual(list(self.cf.stokens), ["t1"])                    # none made, none rotated
        self.assertFalse([w for w in self.writes() if "service_tokens" in w[1]])
        self.assertIs(json.load(open(TC.CONFIG))["ssh_door_service_token"], True)
        n = len(self.cf.policies)
        self.assertEqual(self.run_helper(extra=["--ssh-door-only"]), 0, self.out)   # the setting is remembered
        self.assertEqual((len(self.cf.policies), len(self.ssh_app()["policies"])), (n, 2))

    def test_door_service_token_not_found_still_opens_the_door_for_the_admin(self):
        self.write_cfg(tunnel_id="hand-made-tunnel", service_token_client_id="missing.access")
        self.assertEqual(self.run_helper(extra=["--ssh-door-only", "--door-service-token"]), 0, self.out)
        self.assertIn("isn't added", self.out)
        self.assertEqual(len(self.ssh_app()["policies"]), 1)

    def test_door_only_falls_back_to_self_hosted(self):
        cf = FakeCFNoSSHType()
        try:
            self.write_cfg(tunnel_id="hand-made-tunnel")
            self.assertEqual(self.run_helper(cf, extra=["--ssh-door-only"]), 0, self.out)
            self.assertEqual(self.ssh_app(cf)["type"], "self_hosted")
        finally:
            cf.srv.shutdown()


if __name__ == "__main__":
    unittest.main()
