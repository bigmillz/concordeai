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
ADMIN_HOST = "testsrv-admin.example.test"
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
    cfg.update(U.BASE_CFG)
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
        self.assertIn(b"<title>testsrv admin</title>", data)       # the server's own name, from config.json
        self.assertIn(b"<h1>testsrv</h1>", data)
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
        self.assertIn(b"The server will sleep. Press its power button to wake it. While it sleeps it can\\'t be "
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
        self.assertEqual(self.P.apply_request(os.getuid()), (True, []))   # what the unit runs, as root (o1admin's file)
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


class TestMoneyFormat(unittest.TestCase):
    """Dollar amounts on the power card: whole cents, always two digits (a
    panel once showed $0.0062 and $0.482); under half a cent says so."""
    def panel_money(self):
        with open(os.path.join(U.BIN, "ollama1-admin")) as f:
            src = f.read()
        m = re.search(r"^const money=.*?^(?=const TIERN)", src, re.S | re.M)
        self.assertTrue(m, "the panel's money() is gone")
        return m.group(0)

    @unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
    def test_panel_money_is_whole_cents(self):
        import subprocess
        js = self.panel_money() + "console.log(JSON.stringify([" + ",".join(
            "money(%s,'%s')" % (v, sym) for v, sym in [
                ("null", "$"), ("0", "$"), ("0.004", "$"), ("0.0049", "$"), ("0.005", "$"), ("0.0123", "$"),
                ("0.482", "$"), ("0.987", "$"), ("12.3456", "$"), ("1234.5", "$"), ("7", "$"), ("3.1", "\u20ac"),
                ("-2.5", "$")]) + "]));"
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), ["-", "$0.00", "<$0.01", "<$0.01", "$0.01", "$0.01",
                                                "$0.48", "$0.99", "$12.35", "$1,234.50", "$7.00", "\u20ac3.10", "-$2.50"])

    def test_no_money_is_formatted_any_other_way(self):
        with open(os.path.join(U.BIN, "ollama1-admin")) as f:
            src = f.read()
        self.assertNotIn("toFixed(4)", self.panel_money())
        # every cost goes through money(); the per-token figure is per million so cents mean something
        self.assertIn("Cost per 1 million tokens: '+money(m.cost_per_1k_tokens*1000,sym)", src)
        self.assertNotIn("Cost per 1,000 tokens", src)


class TestGraphicalPage(unittest.TestCase):
    """The graphical page (6b398): the same actions and protections as
    before, a strict CSP, nothing from anywhere else, and nothing from the
    server ever treated as markup."""

    def page(self):
        st, data, r = get("/")
        self.assertEqual(st, 200)
        return data.decode(), r.getheader("Content-Security-Policy")

    def script(self, page):
        return re.search(r'<script nonce="[^"]+">(.*?)</script>', page, re.S).group(1)

    def test_csp_is_strict_and_matches_the_page(self):
        page, csp = self.page()
        nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
        for part in ("default-src 'none'", "style-src 'nonce-%s'" % nonce, "connect-src 'self'",
                     "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'",
                     "require-trusted-types-for 'script'", "trusted-types 'none'"):
            self.assertIn(part, csp)
        for loose in ("unsafe-inline", "unsafe-eval", "unsafe-hashes", "*", "http:", "https:"):
            self.assertNotIn(loose, csp)
        # one script and one style block, each with this response's nonce; a second load gets a new one
        self.assertEqual(re.findall(r"<script\b[^>]*>", page), ['<script nonce="%s">' % nonce])
        self.assertEqual(re.findall(r"<style\b[^>]*>", page), ['<style nonce="%s">' % nonce])
        self.assertNotEqual(self.page()[1], csp)
        # the CSP would block inline handlers and style attributes, so there must be none
        markup = page.replace(self.script(page), "")
        self.assertIsNone(re.search(r"<[^>]+\son[a-z]+\s*=", markup, re.I))
        self.assertIsNone(re.search(r"<[^>]+\sstyle\s*=", markup, re.I))

    def test_nothing_comes_from_anywhere_else(self):
        page, _ = self.page()
        urls = set(re.findall(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s\"')]*", page))
        self.assertEqual(urls, {"http://www.w3.org/2000/svg"})        # the SVG namespace name, never fetched
        self.assertNotIn("@import", page)
        self.assertIsNone(re.search(r"\ssrc\s*=", page))
        self.assertIsNone(re.search(r"<link\b(?![^>]*rel=\"icon\" href=\"data:)", page))
        self.assertLess(len(page.encode()), 150 * 1024)

    def test_no_markup_is_built_from_strings(self):
        page, _ = self.page()
        js = self.script(page)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "setAttribute('style'", "srcdoc"):
            self.assertNotIn(sink, js, sink)
        self.assertIn("Every figure from the server goes in with textContent", js)

    def test_every_action_is_on_the_page_and_posts_with_the_token(self):
        page, _ = self.page()
        js = self.script(page)
        acts = set(re.findall(r'data-act="([a-z-]+)"', page))
        self.assertEqual(acts, {"sleep", "reboot", "restart", "update", "lan", "backup", "pair"})
        mod = A["mod"]
        reach = {"lan-on": "'lan-on'", "lan-off": "'lan-off'", "library-preview": "act('library-preview'",
                 "library-sync": "act('library-sync',p.id,p.id", "power-apply": "post('/api/power/schedule'"}
        for k in mod.FIXED_UNITS:
            self.assertTrue(k in acts or reach.get(k, "\0") in js, k)
        for k, how in (("remove-device", "act('remove-device',x.id"), ("pull", "act('pull',a.hash"),
                       ("remove-model", "if(c===x.name)act('remove-model',x.hash,c")):
            self.assertIn(k, mod.TEMPLATE_UNITS)
            self.assertIn(how, js)
        # one POST in the whole page: JSON, same origin, the CSRF header
        self.assertEqual(js.count("method:'POST'"), 1)
        self.assertIn("{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',"
                      "'X-O1-CSRF':CSRF},body:JSON.stringify(obj)}", js)
        self.assertEqual(js.count("fetch("), 2)                          # that one, and api() for the GETs
        # every destructive action asks first, in the styled dialog
        self.assertIn("act('reboot',null,'reboot',b)", js)
        self.assertIn("act('sleep',null,'sleep',b)", js)
        for title in ("Reboot the server now?", "Put the server to sleep?", "Apply updates now?",
                      "Restart the services?", "'Unpair '+x.name+'?'", "'Remove '+x.name+'?'",
                      "Apply the library changes?", "Turn LAN mode on?", "Replace the prices?"):
            self.assertIn(title, js)
        self.assertIn('<dialog id="dlg" aria-labelledby="dlgt">', page)
        self.assertEqual(re.findall(r"(?<![.\w])(?:confirm|prompt|alert)\(", js), [])  # window.* only as the fallback
        # answers are shown as the server gave them
        self.assertIn("toast((LABEL[action]||action)+(r.ok?': '+r.msg:' refused')", js)

    def test_polling_pauses_while_hidden(self):
        js = self.script(self.page()[0])
        self.assertIn("document.addEventListener('visibilitychange',()=>{if(document.hidden)stopPolling();else startPolling();});", js)
        self.assertIn("@media (prefers-reduced-motion:reduce)", self.page()[0])


class TestStateForThePage(unittest.TestCase):
    """The status files the page draws, cut down to known fields."""

    def setUp(self):
        import o1fan
        import o1gputune
        import o1leds
        self.files = {"fan": o1fan.status_path(), "leds": o1leds.status_path(),
                      "idle": os.path.join(Paths.run, "idle.json"), "tune": o1gputune.state_path(),
                      "stats": Paths.stats}
        for f in self.files.values():
            self.addCleanup(lambda f=f: os.path.exists(f) and os.unlink(f))
        A["panel"].state_cache = (0, None)
        self.addCleanup(lambda: setattr(A["panel"], "state_cache", (0, None)))

    def write(self, key, obj):
        os.makedirs(os.path.dirname(self.files[key]), exist_ok=True)
        with open(self.files[key], "w") as f:
            json.dump(obj, f)

    def state(self):
        A["panel"].state_cache = (0, None)
        st, raw, _ = get("/api/state")
        self.assertEqual(st, 200)
        return raw, json.loads(raw)

    def test_absent_files_are_none(self):
        _, s = self.state()
        for k in ("fan_status", "leds_status", "idle", "gpu_tune"):
            self.assertIn(k, s)
            self.assertIsNone(s[k], k)

    def test_shapes_and_hostile_text(self):
        now = int(time.time())
        evil = "<script>alert(1)</script>"
        self.write("fan", {"at": now, "phase": "hold100", "pct": 100, "hold_left": 42, "why": evil,
                           "outputs": [{"label": evil, "rpm": 1500, "pwm": 255, "secret": "x"},
                                       {"label": "Case fan 2", "rpm": "fast", "pwm": float("nan")}],
                           "temps": [{"label": "NVMe", "c": 58, "limit": 70}], "line": "Fans: 100%",
                           "aio": {"found": True, "name": "AIO", "pump_rpm": 2800, "coolant_c": 31.5}})
        self.write("leds", {"at": now, "state": "red", "target": "red", "rgb": [255, 0, 999], "connected": True,
                            "devices": [{"name": evil}], "line": "Lights: red"})
        self.write("idle", {"at": now - 20, "enabled": True, "minutes": 30, "supported": True, "sleep_ok": False,
                            "reason": "idle 12 of 30 minutes", "idle_s": 720, "wake": ["02:00:5e:10:00:01"]})
        self.write("tune", {"wanted": "on", "applied": {"power_uw": 293_000_000, "mclk": 1075, "at": now},
                            "stock": {"power_uw": 255_000_000, "mclk": 1000},
                            "check": {"result": "passed", "note": "passed: 12 answers", "at": now}})
        raw, s = self.state()
        self.assertNotIn(b"<script", raw)                       # < in the JSON; the page uses textContent
        f = s["fan_status"]
        self.assertEqual((f["phase"], f["pct"], f["hold_left"]), ("hold100", 100, 42))
        self.assertEqual(f["outputs"][0], {"label": evil, "rpm": 1500, "pwm": 255, "min_pct": None, "note": None})
        self.assertEqual((f["outputs"][1]["rpm"], f["outputs"][1]["pwm"]), (None, None))
        self.assertEqual(f["aio"]["pump_rpm"], 2800)
        self.assertEqual(s["fan"], "Fans: 100%")                 # the one-line text is still there
        led = s["leds_status"]
        self.assertEqual(led["rgb"], [255, 0, 255])
        self.assertEqual((led["devices"], led["names"]), (1, [evil]))
        i = s["idle"]
        self.assertEqual((i["enabled"], i["minutes"], i["sleep_ok"], i["idle_s"], i["wake_cards"]), (True, 30, False, 720, 1))
        self.assertGreaterEqual(i["age_s"], 19)                 # by the server's clock, not the browser's
        t = s["gpu_tune"]
        self.assertEqual((t["applied"]["power_w"], t["stock"]["power_w"], t["check"]["result"]), (293, 255, "passed"))

    def test_a_stale_fan_file_is_none(self):
        self.write("fan", {"at": int(time.time()) - 600, "phase": "working", "pct": 100, "line": "x"})
        _, s = self.state()
        self.assertIsNone(s["fan_status"])
        self.assertIsNone(s["fan"])

    def test_logs_add_events_and_errors_and_escape(self):
        from o1common import write_json_atomic
        evil = "<img src=x onerror=alert(1)>"
        write_json_atomic(Paths.stats, {"time": int(time.time()), "recent": [{"t": 1, "device": evil, "model": evil,
                                                                             "status": 200}],
                                        "events": [{"t": 1, "kind": "load", "model": evil}], "errors": {"busy": 2}},
                          mode=0o644)
        st, raw, _ = get("/api/logs")
        self.assertEqual(st, 200)
        self.assertNotIn(b"<img", raw)
        d = json.loads(raw)
        self.assertEqual(d["recent"][0]["device"], evil)        # the same text once decoded
        self.assertEqual(d["events"][0]["model"], evil)
        self.assertEqual(d["errors"], {"busy": 2})
        for k in ("recent", "per_minute", "totals", "auth_failures", "actions", "ollama_update"):
            self.assertIn(k, d)

    @unittest.skipUnless(__import__("shutil").which("node"), "node not installed")
    def test_sleep_text_matches_the_server_screen(self):
        """The page's sleep line says what the HDMI panel says (o1panel.sleep_summary), from the same file."""
        import subprocess
        import o1panel
        page = get("/")[1].decode()
        fn = re.search(r"^function sleepSummary.*?^(?=// ---- gauges)", page, re.S | re.M).group(0)
        now = int(time.time())
        base = {"at": now - 10, "enabled": True, "supported": True, "minutes": 30, "sleep_ok": False,
                "reason": "idle 12 of 30 minutes", "idle_s": 720}
        cases = [base, dict(base, sleep_ok=True), dict(base, enabled=False), dict(base, supported=False),
                 dict(base, reason="a request is running"), dict(base, at=now - 400), dict(base, idle_s=1795)]
        mod = A["mod"]
        ins = [mod.clean_idle(c, now) for c in cases] + [None]
        js = ("const isNum=v=>typeof v==='number'&&isFinite(v);const mmss=s=>Math.floor(s/60)+':'+"
              "String(Math.floor(s%60)).padStart(2,'0');let fetchedAt=Date.now()/1000+60;" + fn +
              "console.log(JSON.stringify(" + json.dumps(ins) + ".map(sleepSummary)));")
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        want = [o1panel.sleep_summary({"idle": c}, now)[0] for c in cases]
        self.assertEqual(want, ["Sleeps in 17:50", "Going to sleep now", "Auto sleep is off",
                                "No deep sleep on this machine", "A request is running", "Sleep status unknown",
                                "Sleeping very soon"])
        page_says = {"Sleeps in 17:50": ["in 17:50"], "Going to sleep now": ["Now", "going to sleep now"],
                     "Auto sleep is off": ["Off", "auto sleep is off"], "No deep sleep on this machine": ["No deep sleep"],
                     "A request is running": ["Awake", "A request is running"], "Sleep status unknown": ["Unknown"],
                     "Sleeping very soon": ["Very soon", "sleeping very soon"]}
        want.append("Sleep status unknown")            # no file at all
        for w, g in zip(want, got):
            for part in page_says[w]:
                self.assertIn(part, (g[0], g[2]), (w, g))


class TestDemoNeverInProduction(unittest.TestCase):
    """tests/admin_demo.py drives the real panel on made-up data for a
    browser on a development machine. It is never installed, refuses root and
    systemd, and the panel itself has no switch for it."""

    def test_refusals(self):
        import admin_demo
        self.assertIn("root", admin_demo.refuse_reason(euid=0, environ={}))
        self.assertIn("systemd", admin_demo.refuse_reason(euid=1000, environ={"INVOCATION_ID": "abc"}))
        self.assertIsNone(admin_demo.refuse_reason(euid=1000, environ={}))

    def test_refused_under_systemd_before_anything_starts(self):
        import subprocess
        import sys
        port = U.free_port()
        r = subprocess.run([sys.executable, os.path.join(U.HERE, "admin_demo.py"), str(port)], capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, INVOCATION_ID="x"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("never part of a server", r.stderr)

    def test_not_installed_and_no_switch_in_the_panel(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('install -m 0755 "$KIT"/bin/* "$LIBDIR/bin/"', setup)
        self.assertNotIn("tests/", setup)
        self.assertFalse(os.path.exists(os.path.join(U.BIN, "admin_demo.py")))
        src = open(os.path.join(U.BIN, "ollama1-admin")).read()
        for word in ("environ", "getenv", "demo", "DEMO"):
            self.assertNotIn(word, src, word)
        unit = open(os.path.join(U.SYSTEMD, "ollama1-admin.service")).read()
        self.assertEqual(re.findall(r"^Environment=(.*)$", unit, re.M), ["PYTHONDONTWRITEBYTECODE=1"])


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
