"""Admin panel: Access JWT with the admin email on every request, CSRF on
actions, and actions that can only start fixed units (which the polkit rule
also allows, and nothing else)."""
import importlib.machinery
import importlib.util
import json
import os
import re
import time
import unittest

import o1test_util as U
import o1pair
from o1common import DEFAULTS, Paths, name_hash
from stub_ollama import Stub

A = {}
ADMIN_HOST = "ollama1-admin.flyconcordefly.com"
ORIGIN = "https://" + ADMIN_HOST


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(U.BIN, name))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def setUpModule():
    A["mod"] = load("ollama1-admin")
    A["key"] = U.RSAKey("kid-a")
    A["jwks"] = U.FakeJWKS([A["key"]])
    A["stub"] = Stub(U.free_port())
    A["started"] = []
    cfg = dict(DEFAULTS)
    cfg.update({"access_team_domain": U.TEAM, "gateway_aud": U.GW_AUD, "admin_aud": U.ADMIN_AUD,
                "admin_email": U.ADMIN_EMAIL, "admin_port": U.free_port(),
                "ollama_url": "http://127.0.0.1:%d" % A["stub"].port, "certs_url": A["jwks"].url,
                "allow_insecure_certs_url": True, "tunnel_metrics_port": U.free_port()})
    A["cfg"] = cfg
    A["panel"], A["srv"], A["stop"] = A["mod"].serve(cfg, runner=lambda u: (A["started"].append(u) or (True, "")))
    A["port"] = cfg["admin_port"]


def tearDownModule():
    A["stop"].set()
    A["srv"].shutdown()
    A["stub"].close()
    A["jwks"].close()


def admin_jwt(**kw):
    c = U.claims(aud=U.ADMIN_AUD, email=U.ADMIN_EMAIL, type="app", common_name=None)
    c.update(kw)
    return U.make_jwt(A["key"], c)


def get(path, token="default", host=ADMIN_HOST):
    h = {}
    if token == "default":
        token = admin_jwt()
    if token:
        h["Cf-Access-Jwt-Assertion"] = token
    return U.request(A["port"], "GET", path, b"", h, host=host)


def csrf():
    st, data, _ = get("/")
    return re.search(rb'name="csrf" content="([0-9a-f]+)"', data).group(1).decode()


def post(obj, token="default", origin=ORIGIN, ctype="application/json", tok="auto", extra=None):
    h = {"Content-Type": ctype}
    if token == "default":
        token = admin_jwt()
    if token:
        h["Cf-Access-Jwt-Assertion"] = token
    if origin:
        h["Origin"] = origin
    if tok == "auto":
        tok = csrf()
    if tok:
        h["X-O1-CSRF"] = tok
    h.update(extra or {})
    return U.request(A["port"], "POST", "/api/action", json.dumps(obj).encode(), h, host=ADMIN_HOST)


class TestPanelAuth(unittest.TestCase):
    def test_page_needs_admin(self):
        st, data, r = get("/")
        self.assertEqual(st, 200)
        self.assertIn(b"ollama1", data)
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))

    def test_no_jwt(self):
        self.assertEqual(get("/", token=None)[0], 403)
        self.assertEqual(get("/api/state", token=None)[0], 403)
        self.assertEqual(get("/term/", token=None)[0], 403)

    def test_other_email(self):
        self.assertEqual(get("/", token=admin_jwt(email="someone@users.noreply.github.com"))[0], 403)

    def test_email_case_insensitive(self):
        self.assertEqual(get("/", token=admin_jwt(email=U.ADMIN_EMAIL.upper()))[0], 200)

    def test_service_token_jwt_is_not_admin(self):
        c = U.claims(aud=U.ADMIN_AUD)  # a service token's JWT has no email
        self.assertEqual(get("/", token=U.make_jwt(A["key"], c))[0], 403)

    def test_gateway_audience_refused(self):
        self.assertEqual(get("/", token=admin_jwt(aud=[U.GW_AUD]))[0], 403)

    def test_expired(self):
        now = int(time.time())
        self.assertEqual(get("/", token=admin_jwt(exp=now - 100, iat=now - 900))[0], 403)

    def test_wrong_host(self):
        self.assertEqual(get("/", host="evil.example")[0], 403)


class TestActions(unittest.TestCase):
    def setUp(self):
        A["started"].clear()

    def test_restart(self):
        st, data, _ = post({"action": "restart"})
        self.assertEqual(st, 202, data)
        self.assertEqual(A["started"], ["ollama1-restart.service"])

    def test_missing_csrf(self):
        st, _, _ = post({"action": "restart"}, tok=None)
        self.assertEqual(st, 403)
        self.assertEqual(A["started"], [])

    def test_wrong_csrf(self):
        self.assertEqual(post({"action": "restart"}, tok="0" * 64)[0], 403)

    def test_cross_origin(self):
        self.assertEqual(post({"action": "restart"}, origin="https://evil.example")[0], 403)
        self.assertEqual(post({"action": "restart"}, origin=None)[0], 403)

    def test_form_post_refused(self):
        self.assertEqual(post({"action": "restart"}, ctype="application/x-www-form-urlencoded")[0], 403)

    def test_cross_site_fetch(self):
        self.assertEqual(post({"action": "restart"}, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(A["started"], [])

    def test_post_without_jwt(self):
        self.assertEqual(post({"action": "restart"}, token=None, tok="0")[0], 403)

    def test_reboot_needs_confirm(self):
        self.assertEqual(post({"action": "reboot"})[0], 400)
        self.assertEqual(post({"action": "reboot", "confirm": "reboot"})[0], 202)
        self.assertEqual(A["started"], ["ollama1-reboot.service"])

    def test_unknown_action(self):
        for bad in ({"action": "shell", "arg": "id"}, {"action": "restart; id"}, {"action": ["x"]}):
            self.assertEqual(post(bad)[0], 400)
        self.assertEqual(A["started"], [])

    def test_pull_only_allow_listed(self):
        with open(Paths.allow, "w") as f:
            f.write("# comment\nsmall:8b\n")
        try:
            self.assertEqual(post({"action": "pull", "arg": name_hash("huge:70b")})[0], 403)
            self.assertEqual(post({"action": "pull", "arg": "../../etc"})[0], 403)
            self.assertEqual(post({"action": "pull", "arg": name_hash("small:8b")})[0], 202)
            self.assertEqual(A["started"], ["ollama1-pull@%s.service" % name_hash("small:8b")])
        finally:
            os.unlink(Paths.allow)

    def test_empty_allow_list_pulls_nothing(self):
        self.assertEqual(post({"action": "pull", "arg": name_hash("small:8b")})[0], 403)

    def test_remove_model_needs_confirm(self):
        h = name_hash("small:8b")
        self.assertEqual(post({"action": "remove-model", "arg": h})[0], 400)
        self.assertEqual(post({"action": "remove-model", "arg": h, "confirm": "small:8b"})[0], 202)
        self.assertEqual(A["started"], ["ollama1-rmmodel@%s.service" % h])

    def test_remove_device(self):
        dev = U.Device()
        U.write_devices([dev])
        self.assertEqual(post({"action": "remove-device", "arg": "0" * 16})[0], 400)
        self.assertEqual(post({"action": "remove-device", "arg": dev.id + ".service"})[0], 400)
        self.assertEqual(post({"action": "remove-device", "arg": dev.id})[0], 202)
        self.assertEqual(A["started"], ["ollama1-rmdevice@%s.service" % dev.id])

    def test_pairing_code_never_in_panel(self):
        w = o1pair.open_window()
        try:
            for path in ("/", "/api/state", "/api/logs", "/api/devices", "/api/models", "/api/history"):
                st, data, _ = get(path)
                self.assertEqual(st, 200, path)
                self.assertNotIn(w["code"].encode(), data, path)
        finally:
            o1pair.close_window()
        self.assertEqual(post({"action": "pair"})[0], 202)
        self.assertEqual(A["started"], ["ollama1-pair-window.service"])


class TestPolkitMatchesPanel(unittest.TestCase):
    """Every unit the panel can start is allowed by the polkit rule, exists
    in systemd/, and the rule allows nothing else."""

    def rule(self):
        return open(os.path.join(U.KIT, "config", "50-ollama1.rules")).read()

    def test_fixed_units(self):
        fixed = set(re.findall(r'"(ollama1-[a-z-]+\.service)"', self.rule()))
        self.assertEqual(fixed, set(A["mod"].FIXED_UNITS.values()))
        for u in fixed:
            self.assertTrue(os.path.exists(os.path.join(U.KIT, "systemd", u)), u)

    def test_template_units(self):
        rule = self.rule()
        pats = [re.compile(p) for p in re.findall(r"/(\^ollama1-[^/]+\$)/", rule)]
        self.assertEqual(len(pats), 2)

        def allowed(unit):
            return any(p.match(unit) for p in pats)
        self.assertTrue(allowed("ollama1-pull@%s.service" % name_hash("x")))
        self.assertTrue(allowed("ollama1-rmmodel@%s.service" % name_hash("x")))
        self.assertTrue(allowed("ollama1-rmdevice@0123456789abcdef.service"))
        for bad in ("ollama1-pull@x.service", "ollama1-pull@%s.service;id" % name_hash("x"),
                    "ollama1-rmdevice@0123456789abcdef0.service", "ollama.service",
                    "ssh.service", "ollama1-pull@..service"):
            self.assertFalse(allowed(bad), bad)
        for t in ("ollama1-pull@.service", "ollama1-rmmodel@.service", "ollama1-rmdevice@.service"):
            self.assertTrue(os.path.exists(os.path.join(U.KIT, "systemd", t)), t)

    @unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
    def test_rule_logic_in_js(self):
        import subprocess
        r = subprocess.run(["node", os.path.join(U.HERE, "polkit_check.js"),
                            os.path.join(U.KIT, "config", "50-ollama1.rules")], capture_output=True, text=True)
        self.assertIn("polkit rule ok", r.stdout, r.stdout + r.stderr)

    def test_only_start_verb(self):
        self.assertIn('if (verb !== "start")', self.rule())
        self.assertIn('subject.user !== "o1admin"', self.rule())


if __name__ == "__main__":
    unittest.main()
