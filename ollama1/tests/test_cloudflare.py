"""ollama1-cf-access against a local fake Cloudflare API: one API token does
the tunnel, DNS and Access; reruns change nothing and never duplicate DNS
records; the API token is never written anywhere."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import o1test_util as U

TOKEN = "cfT0ken-" + uuid.uuid4().hex + "-SECRET"
ACCT = "fake-account-id"
ZONE_ID = "fake-zone-id"
BASE = {"server_name": U.SERVER_NAME, "cf_zone": U.ZONE, "owner_label": "Alice", "admin_email": U.ADMIN_EMAIL}
GW = "testsrv.example.test"
ADMIN = "testsrv-admin.example.test"
CREDS = os.path.join(U.PREFIX, "etc/cloudflared/ollama1.json")
CONFIG = os.path.join(U.PREFIX, "etc/ollama1/config.json")
CF_STATE = os.path.join(U.PREFIX, "var/lib/ollama1/cf-state.json")


class FakeCF:
    def __init__(self):
        self.reset()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != "Bearer " + outer.token:
                    return self.reply(403, {"success": False, "errors": [{"code": 9109, "message": "Invalid access token"}]})
                u = urlsplit(self.path)
                path = u.path.replace("/client/v4", "", 1)
                qs = {k: v[0] for k, v in parse_qs(u.query).items()}
                with outer.lock:
                    outer.calls.append((method, path))
                    if (method, path.rsplit("/", 1)[-1]) == outer.fail_on:
                        st, res = 500, "simulated failure"
                    else:
                        st, res = outer.route(method, path, qs, body)
                self.reply(st, {"success": st < 400, "errors": [] if st < 400 else [{"message": str(res)}],
                                "result": res if st < 400 else None})

            def reply(self, st, obj):
                raw = json.dumps(obj).encode()
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PUT(self):
                self._go("PUT")

            def do_PATCH(self):
                self._go("PATCH")

            def do_DELETE(self):
                self._go("DELETE")

        self.lock = threading.Lock()
        self.port = U.free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/client/v4" % self.port

    def reset(self):
        self.token = TOKEN
        self.org = True
        self.tunnels = {}
        self.dns = {}
        self.stokens = {}
        self.policies = {}
        self.apps = {}
        self.calls = []
        self.fail_on = None
        self.redirect_to = None

    def nid(self):
        return uuid.uuid4().hex

    def route(self, m, p, qs, b):
        A = "/accounts/%s" % ACCT
        if m == "GET" and p == "/zones":
            return 200, [{"id": ZONE_ID, "name": "example.test", "account": {"id": ACCT, "name": "Alice"}}] \
                if qs.get("name") == "example.test" else []
        if m == "GET" and p == A + "/access/organizations":
            return (200, {"auth_domain": "concorde.cloudflareaccess.com"}) if self.org else (404, "no org")
        if p == A + "/cfd_tunnel":
            if m == "GET":
                return 200, [t for t in self.tunnels.values() if t["name"] == qs.get("name")]
            t = {"id": str(uuid.uuid4()), "name": b["name"], "secret": b["tunnel_secret"],
                 "config_src": b.get("config_src"), "deleted_at": None}
            self.tunnels[t["id"]] = t
            return 200, t
        m2 = re.match(A + r"/cfd_tunnel/([^/]+)/token$", p)
        if m2 and m == "GET":
            t = self.tunnels[m2.group(1)]
            tok = base64.b64encode(json.dumps({"a": ACCT, "t": t["id"], "s": t["secret"]}).encode()).decode()
            return 200, tok
        D = "/zones/%s/dns_records" % ZONE_ID
        if p == D and m == "GET":
            return 200, [r for r in self.dns.values() if r["name"] == qs.get("name")]
        if p == D and m == "POST":
            r = dict(b, id=self.nid())
            self.dns[r["id"]] = r
            return 200, r
        m2 = re.match(D + r"/([^/]+)$", p)
        if m2:
            if m == "DELETE":
                self.dns.pop(m2.group(1))
                return 200, {"id": m2.group(1)}
            self.dns[m2.group(1)].update(b)
            return 200, self.dns[m2.group(1)]
        S = A + "/access/service_tokens"
        if p == S:
            if m == "GET":
                return 200, [{k: v for k, v in t.items() if k != "client_secret"} for t in self.stokens.values()]
            self.token_duration = b.get("duration")
            t = {"id": self.nid(), "name": b["name"], "client_id": self.nid() + ".access",
                 "client_secret": "svc-" + self.nid()}
            self.stokens[t["id"]] = t
            return 200, t
        m2 = re.match(S + r"/([^/]+)/rotate$", p)
        if m2:
            t = self.stokens[m2.group(1)]
            t["client_secret"] = "svc-" + self.nid()
            return 200, t
        P = A + "/access/policies"
        if p == P:
            if m == "GET":
                return 200, list(self.policies.values())
            pol = dict(b, id=self.nid())
            self.policies[pol["id"]] = pol
            return 200, pol
        m2 = re.match(P + r"/([^/]+)$", p)
        if m2 and m == "PUT":
            self.policies[m2.group(1)].update(b)
            return 200, self.policies[m2.group(1)]
        AP = A + "/access/apps"
        if p == AP:
            if m == "GET":
                return 200, list(self.apps.values())
            app = dict(b, id=self.nid(), aud=uuid.uuid4().hex + uuid.uuid4().hex)
            self.apps[app["id"]] = app
            return 200, app
        m2 = re.match(AP + r"/([^/]+)$", p)
        if m2 and m == "PUT":
            self.apps[m2.group(1)].update(b)
            return 200, self.apps[m2.group(1)]
        return 404, "no route %s %s" % (m, p)


CF = None


def setUpModule():
    global CF
    CF = FakeCF()


def tearDownModule():
    CF.srv.shutdown()


class TestCloudflareHelper(unittest.TestCase):
    def setUp(self):
        CF.reset()
        for f in (CREDS, CONFIG, CF_STATE):
            if os.path.exists(f):
                os.unlink(f)
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(BASE, f)
        self.tmp = tempfile.mkdtemp(prefix="o1cf-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_helper(self, token=TOKEN, extra=(), tty_ok=True):
        self.tty = os.path.join(self.tmp, "tty")
        open(self.tty, "w").close()
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX, OLLAMA1_CF_API=CF.url, TMPDIR=self.tmp,
                   OLLAMA1_TTY=self.tty if tty_ok else os.path.join(self.tmp, "no-such-dir", "tty"))
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-cf-access"), "--token-stdin",
                            "--no-prompt"] + list(extra), input=token + "\n", env=env,
                           capture_output=True, text=True, timeout=60)
        self.out = r.stdout + r.stderr
        self.shown = open(self.tty).read()
        return r.returncode

    def counts(self):
        return (len(CF.tunnels), len(CF.dns), len(CF.stokens), len(CF.policies), len(CF.apps))

    def test_first_run_does_everything(self):
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual(self.counts(), (1, 2, 1, 2, 2))
        tid = next(iter(CF.tunnels))
        self.assertEqual(CF.tunnels[tid]["config_src"], "local")
        for host in (GW, ADMIN):
            recs = [r for r in CF.dns.values() if r["name"] == host]
            self.assertEqual(len(recs), 1)
            self.assertEqual((recs[0]["type"], recs[0]["content"], recs[0]["proxied"]),
                             ("CNAME", tid + ".cfargotunnel.com", True))
        creds = json.load(open(CREDS))
        self.assertEqual(creds, {"AccountTag": ACCT, "TunnelID": tid, "TunnelSecret": CF.tunnels[tid]["secret"]})
        self.assertEqual(os.stat(CREDS).st_mode & 0o777, 0o600)
        cfg = json.load(open(CONFIG))
        self.assertEqual(cfg["tunnel_id"], tid)
        self.assertEqual(cfg["access_team_domain"], "concorde.cloudflareaccess.com")
        tok = next(iter(CF.stokens.values()))
        self.assertEqual(cfg["service_token_client_id"], tok["client_id"])
        self.assertEqual({a["domain"]: a["aud"] for a in CF.apps.values()},
                         {ADMIN: cfg["admin_aud"], GW: cfg["gateway_aud"]})
        self.assertEqual(os.stat(CONFIG).st_mode & 0o777, 0o600)
        self.assertEqual(self.shown.count(tok["client_secret"]), 1)   # shown once, on the terminal
        self.assertNotIn(tok["client_secret"], self.out)              # never on stdout (the setup log)
        self.assertNotIn(tok["client_secret"], json.dumps(cfg))    # and saved nowhere
        gw_app = next(a for a in CF.apps.values() if a["domain"] == GW)
        self.assertTrue(gw_app["service_auth_401_redirect"])
        pols = {p["name"]: p for p in CF.policies.values()}
        self.assertEqual(pols["testsrv admin - Alice only"]["include"], [{"email": {"email": U.ADMIN_EMAIL}}])
        self.assertEqual(pols["testsrv app - service token"]["decision"], "non_identity")

    def test_names_come_from_the_server_name(self):
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual([t["name"] for t in CF.tunnels.values()], ["testsrv"])
        self.assertEqual([t["name"] for t in CF.stokens.values()], ["testsrv-app"])
        self.assertEqual(sorted(p["name"] for p in CF.policies.values()),
                         ["testsrv admin - Alice only", "testsrv app - service token"])
        self.assertEqual(sorted(a["name"] for a in CF.apps.values()), ["testsrv admin", "testsrv app"])

    def test_install_from_before_the_name_was_a_setting_keeps_its_names(self):
        # config.json with no server_name and no hostnames: the name is ollama1,
        # the hostnames are derived from the zone, nothing is renamed
        with open(CONFIG, "w") as f:
            json.dump({"cf_zone": U.ZONE, "owner_label": "Alice", "admin_email": U.ADMIN_EMAIL}, f)
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual([t["name"] for t in CF.tunnels.values()], ["ollama1"])
        self.assertEqual([t["name"] for t in CF.stokens.values()], ["ollama1-app"])
        self.assertEqual(sorted(p["name"] for p in CF.policies.values()),
                         ["ollama1 admin - Alice only", "ollama1 app - service token"])
        self.assertEqual(sorted(a["domain"] for a in CF.apps.values()),
                         ["ollama1-admin." + U.ZONE, "ollama1." + U.ZONE])

    def test_names_already_in_config_win_over_derived_ones(self):
        with open(CONFIG, "w") as f:
            json.dump(dict(BASE, tunnel_name="old-tunnel", token_name="old-app", policy_admin_name="old admin policy",
                           hostname_gateway="gw.example.test", hostname_admin="panel.example.test"), f)
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual([t["name"] for t in CF.tunnels.values()], ["old-tunnel"])
        self.assertEqual([t["name"] for t in CF.stokens.values()], ["old-app"])
        self.assertIn("old admin policy", [p["name"] for p in CF.policies.values()])
        self.assertEqual(sorted(a["domain"] for a in CF.apps.values()), ["gw.example.test", "panel.example.test"])

    def test_no_zone_stops_before_calling_cloudflare(self):
        with open(CONFIG, "w") as f:
            json.dump({"admin_email": U.ADMIN_EMAIL}, f)
        self.assertNotEqual(self.run_helper(), 0)
        self.assertEqual(CF.calls, [])
        self.assertIn("--zone", self.out)

    def test_rerun_changes_nothing(self):
        self.assertEqual(self.run_helper(), 0, self.out)
        before = (self.counts(), json.load(open(CREDS)), json.load(open(CONFIG)),
                  json.dumps(CF.dns, sort_keys=True))
        secret = next(iter(CF.stokens.values()))["client_secret"]
        n = len(CF.calls)
        self.assertEqual(self.run_helper(), 0, self.out)
        after = (self.counts(), json.load(open(CREDS)), json.load(open(CONFIG)),
                 json.dumps(CF.dns, sort_keys=True))
        self.assertEqual(before, after)
        self.assertNotIn(secret, self.out + self.shown)  # not shown again
        writes = [c for c in CF.calls[n:] if c[0] in ("POST", "PATCH", "DELETE")]
        self.assertEqual(writes, [])  # the apps are PUT (brought in line), nothing created

    def test_lost_credential_is_rebuilt_for_the_same_tunnel(self):
        self.assertEqual(self.run_helper(), 0)
        first = json.load(open(CREDS))
        os.unlink(CREDS)
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual(json.load(open(CREDS)), first)
        self.assertEqual(len(CF.tunnels), 1)

    def test_dns_fixed_and_deduplicated(self):
        CF.dns["x1"] = {"id": "x1", "type": "CNAME", "name": GW, "content": "old-tunnel.cfargotunnel.com",
                        "proxied": False}
        CF.dns["x2"] = {"id": "x2", "type": "CNAME", "name": GW, "content": "somewhere.example.com",
                        "proxied": True, "comment": "ollama1 tunnel (ollama1-cf-access)"}
        self.assertEqual(self.run_helper(), 0, self.out)
        tid = next(iter(CF.tunnels))
        recs = [r for r in CF.dns.values() if r["name"] == GW]
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["content"], recs[0]["proxied"]), (tid + ".cfargotunnel.com", True))
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(len(CF.dns), 2)

    def test_foreign_cname_is_not_touched(self):
        CF.dns["c1"] = {"id": "c1", "type": "CNAME", "name": ADMIN, "content": "shop.example.net", "proxied": True}
        self.assertEqual(self.run_helper(), 1)
        self.assertIn("shop.example.net", self.out)
        self.assertEqual(CF.dns["c1"]["content"], "shop.example.net")
        self.assertFalse([c for c in CF.calls if c[0] in ("PATCH", "DELETE")])

    def test_token_never_expires(self):
        self.assertEqual(self.run_helper(), 0, self.out)
        self.assertEqual(CF.token_duration, "forever")

    def test_secret_shown_even_if_a_later_step_fails(self):
        CF.fail_on = ("POST", "policies")
        self.assertEqual(self.run_helper(), 1)
        tok = next(iter(CF.stokens.values()))
        self.assertEqual(self.shown.count(tok["client_secret"]), 1)
        self.assertNotIn(tok["client_secret"], self.out)
        self.assertFalse(json.load(open(CF_STATE)).get("secret_unshown"))
        CF.fail_on = None
        self.assertEqual(self.run_helper(), 0, self.out)   # the rerun keeps that token and secret
        self.assertEqual(next(iter(CF.stokens.values()))["client_secret"], tok["client_secret"])

    def test_unshown_secret_is_rotated_next_run(self):
        # a run that made the token but had no terminal to show it on
        self.assertEqual(self.run_helper(tty_ok=False), 0, self.out)
        old = next(iter(CF.stokens.values()))["client_secret"]
        self.assertEqual(json.load(open(CF_STATE))["secret_unshown"], "testsrv-app")
        self.assertEqual(self.run_helper(), 0, self.out)   # --no-prompt, yet it rotates
        new = next(iter(CF.stokens.values()))["client_secret"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.shown.count(new), 1)
        self.assertFalse(json.load(open(CF_STATE)).get("secret_unshown"))

    def test_redirects_are_not_followed(self):
        seen = []

        class Grab(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        port = U.free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Grab)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        CF.redirect_to = "http://127.0.0.1:%d/steal" % port
        try:
            self.assertEqual(self.run_helper(), 1)
        finally:
            CF.redirect_to = None
            srv.shutdown()
        self.assertEqual(seen, [])
        self.assertIn("HTTP 302", self.out)

    def test_foreign_record_stops_it(self):
        CF.dns["a1"] = {"id": "a1", "type": "A", "name": ADMIN, "content": "203.0.113.9", "proxied": True}
        self.assertEqual(self.run_helper(), 1)
        self.assertIn("already has A record", self.out)
        self.assertIn("a1", CF.dns)  # never deleted
        self.assertEqual(json.load(open(CONFIG)), BASE)

    def test_no_zero_trust_org(self):
        CF.org = False
        self.assertEqual(self.run_helper(), 1)
        self.assertIn("Zero Trust (left sidebar)", self.out)
        self.assertIn("Free plan", self.out)
        self.assertEqual(len(CF.tunnels), 0)

    def test_bad_token(self):
        self.assertEqual(self.run_helper(token="wrong-token"), 1)
        self.assertIn("Invalid access token", self.out)
        self.assertFalse(os.path.exists(CREDS))

    def test_rotation_on_request(self):
        self.assertEqual(self.run_helper(), 0)
        old = next(iter(CF.stokens.values()))["client_secret"]
        self.assertEqual(self.run_helper(extra=["--rotate-service-token"]), 0)
        new = next(iter(CF.stokens.values()))["client_secret"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.shown.count(new), 1)
        self.assertNotIn(new, self.out)
        self.assertEqual(len(CF.stokens), 1)

    def test_token_never_written(self):
        self.assertEqual(self.run_helper(), 0)
        os.unlink(CREDS)
        self.run_helper()
        CF.dns["a1"] = {"id": "a1", "type": "A", "name": ADMIN, "content": "203.0.113.9"}
        self.run_helper()  # a failing run too
        self.assertNotIn(TOKEN, self.out)
        for root in (U.PREFIX, self.tmp):
            for d, _dirs, files in os.walk(root):
                for f in files:
                    if not os.path.isfile(os.path.join(d, f)):
                        continue
                    with open(os.path.join(d, f), "rb") as fh:
                        self.assertNotIn(TOKEN.encode(), fh.read(), os.path.join(d, f))


class TestSetupTokenHandling(unittest.TestCase):
    """setup.sh reads the token silently and hands it over a pipe; it never
    puts it on a command line, in a file, in the environment or in the log."""

    def section(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        return s[s.index("cf_by_token() {"):s.index("\n}\n", s.index("cf_by_token() {"))]

    def test_read_silently_and_piped(self):
        sec = self.section()
        self.assertIn('read -r -s -p', sec)
        self.assertIn('local CF_TOKEN=""', sec)
        self.assertIn("printf '%s\\n' \"$CF_TOKEN\" | \"$LIBDIR/bin/ollama1-cf-access\" --token-stdin", sec)
        uses = [l.strip() for l in sec.splitlines() if "CF_TOKEN" in l]
        allowed = ('local CF_TOKEN=""', 'read -r -s -p', 'if [ -z "$CF_TOKEN" ]', "if printf '%s\\n' \"$CF_TOKEN\"",
                   'CF_TOKEN=""')
        for line in uses:
            self.assertTrue(line.startswith(allowed), line)
        self.assertNotIn("export CF_TOKEN", open(os.path.join(U.KIT, "setup.sh")).read())

    def test_bash_snippet_end_to_end(self):
        """The same read-and-pipe pattern, run for real with a fake tty."""
        tmp = tempfile.mkdtemp(prefix="o1sh-")
        try:
            tty = os.path.join(tmp, "tty")
            with open(tty, "w") as f:
                f.write(TOKEN + "\n")
            got = os.path.join(tmp, "got")
            script = ('f() { local CF_TOKEN=""; read -r -s -p "token: " CF_TOKEN <"$1"; echo; '
                      'printf \'%s\\n\' "$CF_TOKEN" | cat >"$2"; CF_TOKEN=""; }; f "$1" "$2"; '
                      'echo "after=[${CF_TOKEN:-}]"')
            r = subprocess.run(["bash", "-c", script, "x", tty, got], capture_output=True, text=True)
            self.assertEqual(open(got).read().strip(), TOKEN)
            self.assertNotIn(TOKEN, r.stdout + r.stderr)
            self.assertIn("after=[]", r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
