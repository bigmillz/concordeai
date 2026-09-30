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
                "allow_insecure_certs_url": True, "tunnel_metrics_port": U.free_port(),
                "ttyd_socket": os.path.join(U.PREFIX, "run/ollama1/ttyd.sock")})
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

    def test_sleep(self):
        panel = A["panel"]
        panel.busy = lambda: []
        try:
            self.assertEqual(post({"action": "sleep"})[0], 400)                     # needs the confirm
            self.assertEqual(post({"action": "sleep", "confirm": "sleep"})[0], 202)
            self.assertEqual(A["started"], ["ollama1-sleep.service"])
            A["started"].clear()
            panel.busy = lambda: ["a model is being downloaded"]
            st, data, _ = post({"action": "sleep", "confirm": "sleep"})
            self.assertEqual(st, 409)
            self.assertIn("a model is being downloaded", json.loads(data)["result"])
            self.assertEqual(A["started"], [])
            self.assertEqual(post({"action": "sleep", "arg": "now", "confirm": "sleep"})[0], 409)
        finally:
            import o1sleep
            panel.busy = o1sleep.busy_reasons

    def test_sleep_confirmation_text(self):
        st, page, _ = get("/")
        self.assertIn(b"The desktop will sleep. Press its power button to wake it. While it sleeps it can\\'t be "
                      b"reached, and anything plugged into its second network port loses its connection.", page)
        self.assertIn(b"sync pauses while it sleeps", page)

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

    def test_allow_list_errors_and_ram_flag_shown(self):
        with open(Paths.allow, "w") as f:
            f.write("small:8b ram\nhuge:70b turbo\n")
        try:
            st, data, _ = get("/api/models")
            m = json.loads(data)
            self.assertEqual([(a["name"], a["ram"]) for a in m["allow"]], [("small:8b", True)])
            self.assertEqual(m["allow_errors"][0]["line"], 2)
            self.assertIn("turbo", m["allow_errors"][0]["reason"])
            # a refused line can't be pulled either
            self.assertEqual(post({"action": "pull", "arg": name_hash("huge:70b")})[0], 403)
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


class TestLibraryActions(unittest.TestCase):
    """Update model library: the preview unit, then the sync unit only for
    the preview the panel showed (confirmed, fresh, with something to do)."""

    def setUp(self):
        A["started"].clear()
        import o1library
        self.L = o1library
        os.makedirs(o1library.library_dir(), exist_ok=True)
        self.addCleanup(lambda: os.path.exists(o1library.preview_file()) and os.unlink(o1library.preview_file()))

    def preview(self, **kw):
        p = {"download": [{"name": "a:1b", "bytes": 10}], "update": [], "remove": [{"name": "z:1b", "bytes": 5}],
             "unknown": [], "allow_errors": [], "installed": ["z:1b"], "enough_space": True,
             "totals": {"download_bytes": 10, "update_bytes": 0, "freed_bytes": 5, "free_now": 100, "free_after": 95},
             "made_at": int(time.time())}
        p.update(kw)
        p["id"] = self.L.plan_id(p)
        self.L.write_preview(p)
        return p

    def test_preview_unit(self):
        self.assertEqual(post({"action": "library-preview"})[0], 202)
        self.assertEqual(A["started"], ["ollama1-models-preview.service"])

    def test_sync_only_the_shown_preview(self):
        self.assertEqual(post({"action": "library-sync", "arg": "0" * 16, "confirm": "0" * 16})[0], 409)
        p = self.preview()
        self.assertEqual(post({"action": "library-sync", "arg": p["id"]})[0], 409)             # not confirmed
        self.assertEqual(post({"action": "library-sync", "arg": "f" * 16, "confirm": "f" * 16})[0], 409)
        self.assertEqual(post({"action": "library-sync", "arg": p["id"], "confirm": "yes"})[0], 409)
        self.assertEqual(A["started"], [])
        self.assertEqual(post({"action": "library-sync", "arg": p["id"], "confirm": p["id"]})[0], 202)
        self.assertEqual(A["started"], ["ollama1-models-sync.service"])

    def test_sync_refused_when_stale_full_or_empty(self):
        for kw in ({"made_at": int(time.time()) - 3600}, {"enough_space": False},
                   {"download": [], "remove": []}):
            A["started"].clear()
            p = self.preview(**kw)
            st, data, _ = post({"action": "library-sync", "arg": p["id"], "confirm": p["id"]})
            self.assertEqual(st, 409, kw)
            self.assertEqual(A["started"], [], kw)

    def test_library_endpoint(self):
        p = self.preview()
        st, data, _ = get("/api/library")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["preview"]["id"], p["id"])
        st, page, _ = get("/")
        self.assertIn(b"Update model library", page)
        self.assertIn(b"removes installed models that aren't on the allow-list (nothing else)", page)


def post_path(path, raw, tok="auto", origin=ORIGIN, ctype="application/json"):
    h = {"Content-Type": ctype, "Cf-Access-Jwt-Assertion": admin_jwt()}
    if origin:
        h["Origin"] = origin
    if tok == "auto":
        tok = csrf()
    if tok:
        h["X-O1-CSRF"] = tok
    return U.request(A["port"], "POST", path, raw, h, host=ADMIN_HOST)


class TestPowerPanel(unittest.TestCase):
    """Power and cost: the numbers, and saving prices (CSRF, strict checks)."""

    def setUp(self):
        import o1power
        self.P = o1power
        for f in (o1power.tariff_file(), o1power.request_file()):
            self.addCleanup(lambda f=f: os.path.exists(f) and os.unlink(f))
        A["panel"].power_cache = (0, None, None)
        A["started"].clear()

    def good(self):
        return {"currency": "USD", "mode": "tou", "flat_rate": None, "timezone": "America/New_York",
                "tiers": {"on": 0.3, "off": 0.1, "discount": 0.05, "mid": None}, "weekends_off_peak": True,
                "holidays": {"enabled": True, "names": ["thanksgiving"], "observed": True, "extra": []},
                "seasons": [{"name": "All year", "from": "01-01", "to": "12-31",
                             "windows": [{"tier": "on", "days": "weekdays", "start": "16:00", "end": "21:00"},
                                         {"tier": "discount", "days": "every day", "start": "01:00", "end": "05:00"}]}]}

    def test_power_summary(self):
        st, data, _ = get("/api/power")
        self.assertEqual(st, 200)
        s = json.loads(data)
        self.assertEqual(set(s["windows"]), {"1h", "1d", "1w", "1m"})
        self.assertEqual(s["schedule"]["mode"], "flat")
        self.assertIn("weekdays-4pm-9pm", s["options"]["presets"])
        self.assertIn("thanksgiving", s["options"]["holiday_names"])
        st, page, _ = get("/")
        for want in (b"Power and cost", b"typical, verify against your bill", b"Import JSON", b"Export JSON"):
            self.assertIn(want, page)

    def test_save_needs_csrf_and_same_origin(self):
        raw = json.dumps(self.good()).encode()
        self.assertEqual(post_path("/api/power/schedule", raw, tok=None)[0], 403)
        self.assertEqual(post_path("/api/power/schedule", raw, tok="0" * 64)[0], 403)
        self.assertEqual(post_path("/api/power/schedule", raw, origin="https://evil.example")[0], 403)
        self.assertEqual(post_path("/api/power/schedule", raw, ctype="text/plain")[0], 403)
        self.assertFalse(os.path.exists(self.P.request_file()))
        self.assertEqual(A["started"], [])
        st, data, _ = post_path("/api/power/schedule", raw)
        self.assertEqual(st, 202, data)
        # the panel only leaves a request in its own folder and starts the fixed apply unit
        self.assertEqual(A["started"], ["ollama1-power-apply.service"])
        self.assertFalse(os.path.exists(self.P.tariff_file()))
        self.assertEqual(oct(os.stat(self.P.request_file()).st_mode & 0o777), "0o600")
        self.assertEqual(self.P.apply_request(), (True, []))          # what the unit runs, as root
        self.assertEqual(oct(os.stat(self.P.tariff_file()).st_mode & 0o777), "0o640")
        s = json.loads(get("/api/power")[1])
        self.assertEqual(s["schedule"]["mode"], "tou")
        self.assertIn(s["badge"]["tier"], ("on", "off", "discount"))
        st, data, _ = get("/api/power/schedule.json")
        self.assertEqual(json.loads(data)["tiers"]["on"], 0.3)

    def test_bad_schedule_refused_with_reasons(self):
        bad = self.good()
        bad["seasons"][0]["windows"][0]["end"] = "16:00"
        bad["tiers"]["discount"] = None
        st, data, _ = post_path("/api/power/schedule", json.dumps(bad).encode())
        self.assertEqual(st, 400)
        errs = json.loads(data)["errors"]
        self.assertTrue(any("same time" in e for e in errs))
        self.assertTrue(any("discount price" in e for e in errs))
        self.assertFalse(os.path.exists(self.P.request_file()))
        self.assertEqual(A["started"], [])
        self.assertEqual(post_path("/api/power/schedule", b"[1,2]")[0], 400)
        self.assertEqual(post_path("/api/power/schedule", b"not json")[0], 400)

    def test_size_limits(self):
        big = json.dumps(dict(self.good(), note="x" * 70000)).encode()
        self.assertEqual(post_path("/api/power/schedule", big)[0], 413)
        self.assertEqual(post_path("/api/action", json.dumps({"action": "restart", "pad": "x" * 5000}).encode())[0], 413)
        self.assertEqual(post_path("/api/elsewhere", b"{}")[0], 404)


class FakeTtyd:
    """ttyd on a UNIX socket, sending its own (weaker) framing headers."""

    def __init__(self, path):
        import socketserver
        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"

            def do_GET(self):
                raw = b"<html>ttyd page</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        if os.path.exists(path):
            os.unlink(path)
        self.path = path
        self.srv = socketserver.ThreadingUnixStreamServer(path, H)
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.unlink(self.path)


class TestTerminalProxy(unittest.TestCase):
    def test_proxied_over_unix_socket_with_frame_headers(self):
        t = FakeTtyd(A["cfg"]["ttyd_socket"])
        try:
            st, data, r = get("/term/")
        finally:
            t.close()
        self.assertEqual(st, 200)
        self.assertIn(b"ttyd page", data)
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))

    def test_websocket_needs_same_origin(self):
        h = {"Cf-Access-Jwt-Assertion": admin_jwt(), "Upgrade": "websocket", "Connection": "Upgrade",
             "Origin": "https://evil.example"}
        st, _, _ = U.request(A["port"], "GET", "/term/ws", b"", h, host=ADMIN_HOST)
        self.assertEqual(st, 403)

    def test_no_ttyd_tcp_port(self):
        unit = open(os.path.join(U.KIT, "systemd", "ollama1-ttyd.service")).read()
        self.assertIn("--interface /run/ollama1/ttyd/ttyd.sock", unit)
        self.assertNotIn("--port", unit)
        tmp = open(os.path.join(U.KIT, "config", "ollama1.tmpfiles")).read()
        self.assertRegex(tmp, r"d /run/ollama1/ttyd\s+0750 root\s+o1admin")


class TestPanelConnections(unittest.TestCase):
    def test_errors_close_the_connection(self):
        import socket
        inner = ("GET /api/state HTTP/1.1\r\nHost: %s\r\nCf-Access-Jwt-Assertion: %s\r\n\r\n"
                 % (ADMIN_HOST, admin_jwt())).encode()
        for head in ("POST /api/action HTTP/1.1\r\nHost: %s\r\nContent-Length: %d\r\n\r\n" % (ADMIN_HOST, len(inner)),
                     "POST /api/action HTTP/1.1\r\nHost: %s\r\nCf-Access-Jwt-Assertion: %s\r\n"
                     "Content-Length: 999999\r\n\r\n" % (ADMIN_HOST, admin_jwt())):
            s = socket.create_connection(("127.0.0.1", A["port"]))
            s.sendall(head.encode() + inner)
            s.settimeout(3)
            data = b""
            try:
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    data += b
            except socket.timeout:
                pass
            s.close()
            self.assertEqual(data.count(b"HTTP/1.1 "), 1, head[:40])
            self.assertIn(b"Connection: close", data)


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
