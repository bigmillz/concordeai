"""Model sets from the app (6b410): GET /v1/models/state and POST
/v1/models/apply through a real gateway (signed and paired-only, against the
stub Ollama); the root program that reads the gateway's request without
trusting it, checks all of it again and does the work (the allow-list edit,
removals first, a failed pull that doesn't stop the rest, the status files);
and the unit, polkit, tmpfiles and setup text that hold the privilege line."""
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import o1test_util as U  # sets OLLAMA1_PREFIX first

import o1library as L  # noqa: E402
import o1modelplan as M  # noqa: E402
import o1sleep  # noqa: E402
from o1common import DEFAULTS, Paths, parse_allow_list, write_json_atomic  # noqa: E402
from stub_ollama import Stub, llama_info  # noqa: E402

GIB = 1 << 30
G = {}
UNIT_TEXT = lambda: open(os.path.join(U.SYSTEMD, "ollama1-modelplan.service")).read()  # noqa: E731
RULES_TEXT = lambda: open(os.path.join(U.CONFIG, "50-ollama1.rules")).read()  # noqa: E731


def load(name, modname):
    loader = importlib.machinery.SourceFileLoader(modname, os.path.join(U.BIN, name))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def setUpModule():
    G["gwmod"] = load("ollama1-gateway", "o1gateway_modelplan")
    G["root"] = load("ollama1-modelplan", "o1modelplan_root")
    G["key"] = U.RSAKey("kid-mp")
    G["jwks"] = U.FakeJWKS([G["key"]])
    G["stub"] = Stub(U.free_port(), models={})
    G["base"] = "http://127.0.0.1:%d" % G["stub"].port
    cfg = dict(DEFAULTS)
    cfg.update(U.BASE_CFG)
    cfg.update({"access_team_domain": U.TEAM, "gateway_aud": U.GW_AUD, "admin_aud": U.ADMIN_AUD,
                "service_token_client_id": U.CLIENT_ID, "gateway_port": U.free_port(), "ollama_url": G["base"],
                "certs_url": G["jwks"].url, "allow_insecure_certs_url": True, "vram_total_bytes": 16 * GIB})
    G["dev"] = U.Device(name="Alice's Mac")
    U.write_devices([G["dev"]])
    G["gw"], G["servers"], G["stop"] = G["gwmod"].serve(cfg)
    G["port"] = cfg["gateway_port"]
    time.sleep(1.1)  # signatures must be dated after the gateway started


def tearDownModule():
    G["stop"].set()
    for s in G["servers"]:
        s.shutdown()
    G["stub"].close()
    G["jwks"].close()


def call(method, path, obj=None, dev=None, jwt_token="default", raw=None):
    body = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
    h = {}
    if jwt_token == "default":
        h["Cf-Access-Jwt-Assertion"] = U.make_jwt(G["key"], U.claims())
    elif jwt_token:
        h["Cf-Access-Jwt-Assertion"] = jwt_token
    if dev is not False:
        h.update((dev or G["dev"]).headers(method, path, body))
    st, data, _ = U.request(G["port"], method, path, body, h)
    try:
        return st, json.loads(data)
    except ValueError:
        return st, data


def held(path):
    """Hold an exclusive lock on `path` the way root's program does."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class Root:
    """Stands in for systemd starting ollama1-modelplan.service: the root
    program, in a thread, against the stub Ollama."""

    def __init__(self):
        self.logs, self.threads, self.started, self.units = [], [], [], []
        self.before = None          # runs first: something changes between the gateway's look and root's
        self.mode = "run"           # "run", "nothing" (started, never answers), "fail" (systemctl refused)
        self.rc = None

    def __call__(self):
        self.started.append(M.UNIT)
        if self.mode == "fail":
            return False, "Failed to start ollama1-modelplan.service: Interactive authentication required."
        if self.mode == "nothing":
            return True, ""

        def go():
            if self.before:
                self.before()
            self.rc = G["root"].run(owner_uid=os.getuid(), base=G["base"], active_units=lambda p: list(self.units),
                                    sleep=lambda s: None, tries=1, log=self.logs.append)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        self.threads.append(t)
        return True, ""

    def join(self):
        for t in self.threads:
            t.join(60)


ALLOW = "# my models\nkeep:1b\nold:7b   ram   # the big one\n\nqwen3:14b\n"


class Base(unittest.TestCase):
    allow_text = ALLOW

    def models(self):
        return {"keep:1b": {"size": 1 * GIB, "info": llama_info()}, "old:7b": {"size": 4 * GIB, "info": llama_info()},
                "byhand:2b": {"size": 2 * GIB, "info": llama_info()}}

    def setUp(self):
        self.stub = G["stub"]
        with self.stub.lock:
            self.stub.models.clear()
            self.stub.models.update(self.models())
            self.stub.loaded.clear()
            self.stub.calls.clear()
            self.stub.pull_fail = 0
            self.stub.pull_layers.clear()
        for d in (Paths.modelplan_run, Paths.modelplan_state, os.path.join(Paths.run, "library"), Paths.gw_state):
            os.makedirs(d, exist_ok=True)
        self.wipe()
        with open(Paths.allow, "w") as f:
            f.write(self.allow_text)
        self.root = Root()
        gw = G["gw"]
        self.saved = (gw.start_plan_unit, gw.list_units, gw.plan_wait_s, gw.gpu_info)
        gw.start_plan_unit, gw.list_units, gw.units_at, gw.plan_wait_s = self.root, (lambda p: []), None, 10.0
        self.logs = []
        self.addCleanup(self.cleanup)

    def wipe(self):
        for f in (M.answer_file(), M.status_file(), M.record_file(), M.request_file(), M.lock_file(), Paths.allow):
            if os.path.lexists(f):
                os.unlink(f)

    def cleanup(self):
        self.root.join()
        gw = G["gw"]
        gw.start_plan_unit, gw.list_units, gw.plan_wait_s, gw.gpu_info = self.saved
        gw.units_at = None
        self.wipe()

    def names(self):
        with self.stub.lock:
            return sorted(self.stub.models)

    def seen(self):
        """The app's side of `seen`, computed here from the definition."""
        return hashlib.sha256("\n".join(self.names()).encode()).hexdigest()

    def body(self, **over):
        b = {"plan": "recommended", "add": ["new:4b"], "remove": ["old:7b"], "seen": self.seen()}
        b.update(over)
        return b

    def calls(self, method=None, path=None):
        with self.stub.lock:
            return [c for c in self.stub.calls if (method is None or c[0] == method) and (path is None or c[1] == path)]

    def changes(self):
        """Every call that would change a model."""
        return [(c[0], c[1], (c[2] or {}).get("model")) for c in self.calls()
                if c[0] == "DELETE" or c[1] in ("/api/pull", "/api/delete")]

    def allow(self):
        with open(Paths.allow) as f:
            return f.read()

    def wait_done(self):
        self.root.join()
        end = time.time() + 30
        while time.time() < end:
            st = M.read_status()
            if st and st["state"] != "running":
                return st
            time.sleep(0.05)
        self.fail("the plan never finished")


# ---- the request's shape (both sides use this one check) ---------------------------

class TestRequestShape(unittest.TestCase):
    def good(self, **over):
        b = {"plan": "light", "add": ["gemma4:12b", "qwen3"], "remove": ["llama3.2:3b"], "seen": "ab" * 32}
        b.update(over)
        return b

    def test_a_good_request(self):
        clean, err = M.parse_request(self.good())
        self.assertEqual(err, "")
        self.assertEqual(clean, {"plan": "light", "add": ["gemma4:12b", "qwen3"], "remove": ["llama3.2:3b"],
                                 "seen": "ab" * 32})
        for plan in ("light", "recommended", "everything"):
            self.assertEqual(M.parse_request(self.good(plan=plan))[1], "")
        self.assertEqual(M.parse_request(self.good(add=[]))[1], "")                 # only removals
        self.assertEqual(M.parse_request(self.good(remove=[]))[1], "")              # only pulls
        self.assertEqual(M.parse_request(self.good(seen="AB" * 32))[0]["seen"], "ab" * 32)

    def test_bad_requests(self):
        bad = [None, [], "x", 5, {},
               dict(self.good(), extra=1), {k: v for k, v in self.good().items() if k != "seen"},
               {k: v for k, v in self.good().items() if k != "add"},
               self.good(plan="huge"), self.good(plan="Light"), self.good(plan=None), self.good(plan=["light"]),
               self.good(add="gemma4:12b"), self.good(add=None), self.good(remove={"x": 1}),
               self.good(add=[5]), self.good(add=[None]), self.good(add=[["a"]]),
               self.good(add=[], remove=[]),
               self.good(seen=""), self.good(seen="ab" * 31), self.good(seen="ab" * 33), self.good(seen="zz" * 32),
               self.good(seen=None), self.good(seen=64), self.good(seen="ab" * 32 + "\n")]
        for b in bad:
            clean, err = M.parse_request(b)
            self.assertIsNone(clean, repr(b)[:80])
            self.assertTrue(err, repr(b)[:80])

    def test_the_tag_pattern(self):
        good = ["gemma4:12b", "qwen3", "llama3.2:3b", "a", "a:b", "0:0", "gpt-oss:20b", "nomic-embed-text:v1.5",
                "a_b-c.d:e_f-g.h", "x" * 80, "x" * 80 + ":" + "y" * 64, "qwen3:latest"]
        for t in good:
            self.assertEqual(M.parse_request(self.good(add=[t], remove=[]))[1], "", t)
        bad = ["", "Gemma4:12b", "gemma4:12B", "user/model:1b", "registry.ollama.ai/library/x:1b", "hf.co/x/y:q4",
               "a/b", "a:b/c", "a\\b", "-a:1b", ".a", "_a", "a:-b", "a:.b", "a:", ":b", "a:b:c", "a b", "a\tb",
               "a:1b\n", "a\n", "\na", "x" * 81, "a:" + "y" * 65, "x" * 81 + ":1b", "a..b:1", "a:1..2", "a@b",
               "a:b#c", "a;id", "ａ", "a\x00", 5, None, ["a"]]
        for t in bad:
            for key in ("add", "remove"):
                clean, err = M.parse_request(self.good(**{key: [t], ("remove" if key == "add" else "add"): []}))
                self.assertIsNone(clean, (key, t))
                self.assertIn("%s[0]" % key, err)
            self.assertFalse(M.valid_tag(t), repr(t))

    def test_limits(self):
        tags = ["m%d:1b" % i for i in range(41)]
        self.assertEqual(M.parse_request(self.good(add=tags[:40], remove=[]))[1], "")
        self.assertEqual(M.parse_request(self.good(add=[], remove=tags[:40]))[1], "")
        self.assertIn("more than 40", M.parse_request(self.good(add=tags, remove=[]))[1])
        self.assertIn("more than 40", M.parse_request(self.good(add=[], remove=tags))[1])

    def test_overlap_and_twice(self):
        self.assertIn("both", M.parse_request(self.good(add=["a"], remove=["a:latest"]))[1])
        self.assertIn("both", M.parse_request(self.good(add=["b:1b", "a:1b"], remove=["a:1b"]))[1])
        self.assertIn("twice", M.parse_request(self.good(add=["a", "a:latest"], remove=[]))[1])
        self.assertIn("twice", M.parse_request(self.good(add=[], remove=["x:1", "x:1"]))[1])

    def test_cloud_models_are_never_added(self):
        for t in ("gpt-oss:120b-cloud", "x:cloud", "deepseek-cloud"):
            self.assertIn("cloud", M.parse_request(self.good(add=[t], remove=[]))[1])
            self.assertEqual(M.parse_request(self.good(add=[], remove=[t]))[1], "")      # off the list: fine

    def test_the_reason_never_repeats_what_was_sent(self):
        mark = "EVIL<script>"
        for b in (dict(self.good(), **{mark: 1}), self.good(plan=mark), self.good(add=[mark]),
                  self.good(seen=mark), self.good(remove=[mark + ":1b"])):
            err = M.parse_request(b)[1]
            self.assertTrue(err)
            self.assertNotIn("EVIL", err)

    def test_seen_is_the_hash_of_the_sorted_names(self):
        self.assertEqual(M.seen_hash([]), hashlib.sha256(b"").hexdigest())
        self.assertEqual(M.seen_hash(["b:1b", "a:1b", "c"]), hashlib.sha256(b"a:1b\nb:1b\nc").hexdigest())

    def test_cloud_entries_are_not_models_on_the_disk(self):
        tags = {"models": [{"name": "a:1b"}, {"name": "gpt-oss:120b-cloud"}, {"name": "x", "remote_host": "h"},
                           {"name": ""}, {"name": 5}, "junk", {"model": "no-name"}]}
        self.assertEqual([m["name"] for m in M.local_models(tags)], ["a:1b"])
        self.assertEqual(M.local_models(None), [])
        self.assertEqual(M.local_models({"models": "x"}), [])


# ---- GET /v1/models/state -----------------------------------------------------------

class TestStateRoute(Base):
    def test_signed_paired_and_through_access_only(self):
        for method, path, obj in (("GET", "/v1/models/state", None), ("POST", "/v1/models/apply", self.body())):
            self.assertEqual(call(method, path, obj, dev=False)[0], 401)
            self.assertEqual(call(method, path, obj, dev=U.Device())[0], 403)
            self.assertEqual(call(method, path, obj, jwt_token=None)[0], 403)
        self.assertEqual(self.root.started, [])
        self.assertFalse(os.path.exists(M.request_file()))
        self.assertEqual(self.allow(), ALLOW)
        self.assertEqual(call("GET", "/v1/models/apply")[0], 404)          # only POST
        self.assertEqual(call("POST", "/v1/models/state", {})[0], 404)     # only GET

    def test_the_answer_has_exactly_the_contract_keys(self):
        with self.stub.lock:
            self.stub.loaded["old:7b"] = {"name": "old:7b", "model": "old:7b", "size": 4 * GIB, "size_vram": 4 * GIB}
        G["gw"].gpu_info = {"vendor": "amd", "name": "Radeon RX 6900 XT", "vram_bytes": 17163091968}
        st, d = call("GET", "/v1/models/state")
        self.assertEqual(st, 200, d)
        self.assertEqual(sorted(d), ["allow", "busy", "disk_free_bytes", "jobs", "models", "plan", "vram_bytes"])
        self.assertEqual(d["models"], [{"name": "byhand:2b", "size": 2 * GIB, "loaded": False},
                                       {"name": "keep:1b", "size": 1 * GIB, "loaded": False},
                                       {"name": "old:7b", "size": 4 * GIB, "loaded": True}])     # no cloud entries
        self.assertEqual(d["allow"], ["keep:1b", "old:7b", "qwen3:14b"])
        self.assertIs(d["busy"], False)
        self.assertEqual((d["jobs"], d["plan"]), ([], None))
        self.assertIsInstance(d["disk_free_bytes"], int)
        self.assertGreater(d["disk_free_bytes"], 0)
        self.assertEqual(d["vram_bytes"], 17163091968)
        G["gw"].gpu_info = {"vendor": None, "name": None, "vram_bytes": None}       # no card read: config.json's figure
        self.assertEqual(call("GET", "/v1/models/state")[1]["vram_bytes"], 16 * GIB)

    def test_seen_from_the_answer_is_what_apply_wants(self):
        st, d = call("GET", "/v1/models/state")
        seen = M.seen_hash([m["name"] for m in d["models"]])
        self.assertEqual(seen, self.seen())
        st, out = call("POST", "/v1/models/apply", self.body(seen=seen))
        self.assertEqual(st, 202, out)
        self.wait_done()

    def test_busy_says_what_the_locks_and_units_say(self):
        gw = G["gw"]
        self.assertIs(call("GET", "/v1/models/state")[1]["busy"], False)
        fd = held(M.lock_file())
        try:
            self.assertIs(call("GET", "/v1/models/state")[1]["busy"], True)
        finally:
            os.close(fd)
        fd = held(o1sleep.library_lock())
        try:
            self.assertIs(call("GET", "/v1/models/state")[1]["busy"], True)
        finally:
            os.close(fd)
        for units in (["ollama1-pull@0123456789ab.service"], ["ollama1-rmmodel@0123456789ab.service"],
                      ["ollama1-models-sync.service"], ["ollama1-modelplan.service"]):
            gw.list_units, gw.units_at = (lambda p, u=units: list(u)), None
            self.assertIs(call("GET", "/v1/models/state")[1]["busy"], True, units)
        gw.list_units, gw.units_at = (lambda p: ["ollama1-gateway.service", "ollama.service"]), None
        self.assertIs(call("GET", "/v1/models/state")[1]["busy"], False)
        gw.list_units, gw.units_at = (lambda p: None), None            # systemd unreachable: the locks still count
        self.assertIs(call("GET", "/v1/models/state")[1]["busy"], False)

    def test_systemd_is_asked_at_most_every_two_seconds(self):
        gw, asked = G["gw"], []
        gw.list_units, gw.units_at = (lambda p: asked.append(1) or []), None
        for _ in range(4):
            call("GET", "/v1/models/state")
        self.assertEqual(len(asked), 1)

    def test_ollama_down(self):
        gw = G["gw"]
        saved = gw.base
        gw.base = "http://127.0.0.1:%d" % U.free_port()
        try:
            st, d = call("GET", "/v1/models/state")
            self.assertEqual((st, d["code"]), (502, "ollama"))
            st, d = call("POST", "/v1/models/apply", self.body())
            self.assertEqual((st, d["code"]), (502, "ollama"))
        finally:
            gw.base = saved
        self.assertEqual(self.root.started, [])


# ---- POST /v1/models/apply ----------------------------------------------------------

class TestApplyRoute(Base):
    def assert_nothing_happened(self):
        self.assertEqual(self.root.started, [])
        self.assertFalse(os.path.exists(M.request_file()))
        self.assertFalse(os.path.exists(M.status_file()))
        self.assertEqual(self.allow(), ALLOW)
        self.assertEqual(self.changes(), [])

    def test_bad_requests_are_400_and_change_nothing(self):
        tags = ["m%d:1b" % i for i in range(41)]
        for b in ({}, [], "x", self.body(extra=True), {"plan": "light", "add": [], "remove": ["old:7b"]},
                  self.body(plan="huge"), self.body(add=["user/model:1b"]), self.body(add=["Gemma4"]),
                  self.body(add=["a:1b\n"]), self.body(add=tags), self.body(remove=tags), self.body(add=[], remove=[]),
                  self.body(add=["old:7b"]), self.body(add=["x", "x:latest"]), self.body(seen="nope"),
                  self.body(add=["gpt-oss:120b-cloud"])):
            st, d = call("POST", "/v1/models/apply", b)
            self.assertEqual((st, d["code"]), (400, "bad_request"), b)
            self.assertTrue(d["error"])
        for raw in (b"not json", b"", b"\xff\xfe"):
            st, d = call("POST", "/v1/models/apply", raw=raw)
            self.assertEqual(st, 400, raw)
        self.assert_nothing_happened()

    def test_changed(self):
        st, d = call("POST", "/v1/models/apply", self.body(seen=M.seen_hash(["keep:1b", "old:7b"])))
        self.assertEqual((st, d["code"]), (409, "changed"))
        self.assertEqual(d["models"], ["byhand:2b", "keep:1b", "old:7b"])
        self.assert_nothing_happened()

    def test_busy_every_way(self):
        gw = G["gw"]
        gw.plan_lock.acquire()
        try:
            st, d = call("POST", "/v1/models/apply", self.body())
            self.assertEqual((st, d["code"]), (409, "busy"))
        finally:
            gw.plan_lock.release()
        for path in (M.lock_file(), o1sleep.library_lock()):
            fd = held(path)
            try:
                st, d = call("POST", "/v1/models/apply", self.body())
                self.assertEqual((st, d["code"]), (409, "busy"), path)
            finally:
                os.close(fd)
        for units in (["ollama1-pull@0123456789ab.service"], ["ollama1-rmmodel@0123456789ab.service"],
                      ["ollama1-models-sync.service"], ["ollama1-modelplan.service"]):
            gw.list_units = lambda p, u=units: list(u)
            st, d = call("POST", "/v1/models/apply", self.body())
            self.assertEqual((st, d["code"]), (409, "busy"), units)
        self.assert_nothing_happened()

    def test_busy_is_asked_fresh_not_from_the_cache(self):
        gw = G["gw"]
        call("GET", "/v1/models/state")                                  # cached: nothing running
        gw.list_units = lambda p: ["ollama1-models-sync.service"]
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"]), (409, "busy"))

    def test_in_use(self):
        with self.stub.lock:
            self.stub.loaded["old:7b"] = {"name": "old:7b", "model": "old:7b", "size": 4 * GIB, "size_vram": 4 * GIB}
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"], d["name"]), (409, "in_use", "old:7b"))
        self.assert_nothing_happened()
        # a loaded model that isn't being removed is no reason
        st, d = call("POST", "/v1/models/apply", self.body(remove=["byhand:2b"]))
        self.assertEqual(st, 202, d)
        self.wait_done()

    def test_accepted_end_to_end(self):
        G["gw"].activity.last = 0.0
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            st, d = call("POST", "/v1/models/apply", self.body(add=["new:4b", "fresh:2b"], remove=["old:7b"]))
        self.assertEqual(st, 202, d)
        self.assertEqual(sorted(d), ["jobs", "ok"])
        self.assertIs(d["ok"], True)
        self.assertEqual([(j["name"], j["action"]) for j in d["jobs"]],
                         [("old:7b", "remove"), ("new:4b", "pull"), ("fresh:2b", "pull")])     # removals first
        for j in d["jobs"]:
            self.assertEqual(sorted(j), ["action", "error", "name", "pct", "state"])
            self.assertIn(j["state"], ("queued", "running", "done"))
        self.assertEqual(self.root.started, ["ollama1-modelplan.service"])
        rec = self.wait_done()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(self.root.rc, 0)
        st, s = call("GET", "/v1/models/state")
        self.assertIs(s["busy"], False)
        self.assertEqual(sorted(s["plan"]), ["at", "by", "name"])
        self.assertEqual((s["plan"]["name"], s["plan"]["by"]), ("recommended", "Alice's Mac"))
        self.assertLessEqual(abs(s["plan"]["at"] - time.time()), 30)
        self.assertEqual([(j["name"], j["action"], j["state"], j["pct"], j["error"]) for j in s["jobs"]],
                         [("old:7b", "remove", "done", 100, ""), ("new:4b", "pull", "done", 100, ""),
                          ("fresh:2b", "pull", "done", 100, "")])
        self.assertEqual([m["name"] for m in s["models"]], ["byhand:2b", "fresh:2b", "keep:1b", "new:4b"])
        self.assertEqual(s["allow"], ["keep:1b", "qwen3:14b", "new:4b", "fresh:2b"])
        self.assertEqual(self.allow(), "# my models\nkeep:1b\n\nqwen3:14b\nnew:4b\nfresh:2b\n")
        self.assertEqual(self.changes(), [("DELETE", "/api/delete", "old:7b"), ("POST", "/api/pull", "new:4b"),
                                          ("POST", "/api/pull", "fresh:2b")])
        self.assertFalse(os.path.exists(M.request_file()))        # the gateway tidies its own folder
        self.assertGreater(G["gw"].activity.last, 0)              # applying a set counts as use once
        printed = buf.getvalue()
        self.assertRegex(printed, r"model-plan device=Alice's_Mac status=202 kind=recommended added=2 removed=1")
        text = "\n".join(self.root.logs)
        self.assertIn("applying the recommended set for Alice's Mac", text)
        self.assertIn("add new:4b,fresh:2b; remove old:7b", text)
        self.assertIn("finished the recommended set for Alice's Mac: removed old:7b; pulled new:4b,fresh:2b; failed -",
                      text)

    def test_a_second_set_while_one_runs_is_busy(self):
        self.root.mode = "nothing"                    # root is "running": it holds the plan lock
        fd = held(M.lock_file())
        try:
            st, d = call("POST", "/v1/models/apply", self.body())
            self.assertEqual((st, d["code"]), (409, "busy"))
        finally:
            os.close(fd)

    def test_root_refuses_what_changed_after_the_gateway_looked(self):
        def change():
            with self.stub.lock:
                self.stub.models.pop("byhand:2b")
        self.root.before = change
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"]), (409, "changed"))
        self.assertEqual(d["models"], ["keep:1b", "old:7b"])          # root's list, now
        self.root.join()
        self.assertEqual(self.allow(), ALLOW)
        self.assertEqual(self.changes(), [])
        self.assertIsNone(M.read_status())                          # a refusal is not a plan
        self.assertFalse(os.path.exists(M.request_file()))

    def test_root_refuses_a_model_loaded_after_the_gateway_looked(self):
        def load_it():
            with self.stub.lock:
                self.stub.loaded["old:7b"] = {"name": "old:7b", "model": "old:7b", "size": 1, "size_vram": 1}
        self.root.before = load_it
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"], d["name"]), (409, "in_use", "old:7b"))
        self.root.join()
        self.assertEqual((self.allow(), self.changes()), (ALLOW, []))

    def test_root_refuses_when_it_sees_a_unit_running(self):
        self.root.units = ["ollama1-pull@0123456789ab.service"]       # the gateway's systemd answer said nothing
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"]), (409, "busy"))
        self.assertIn("downloaded", d["error"])
        self.root.join()
        self.assertEqual((self.allow(), self.changes()), (ALLOW, []))

    def test_the_unit_would_not_start(self):
        self.root.mode = "fail"
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"]), (503, "not_started"))
        self.assertIn("Interactive authentication required", d["error"])
        self.assertFalse(os.path.exists(M.request_file()))
        self.assertEqual((self.allow(), self.changes()), (ALLOW, []))

    def test_no_answer_in_time(self):
        self.root.mode = "nothing"
        G["gw"].plan_wait_s = 0.3
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual((st, d["code"]), (504, "no_answer"))
        self.assertIn("/v1/models/state", d["error"])
        self.assertTrue(os.path.exists(M.request_file()))      # left for root, which may still take it
        self.assertEqual(oct(os.stat(M.request_file()).st_mode & 0o777), "0o600")

    def test_an_old_answer_is_not_taken_for_this_one(self):
        G["root"].answer("0123456789abcdef", True)
        self.root.mode = "nothing"
        G["gw"].plan_wait_s = 0.3
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual(st, 504)

    def test_what_the_gateway_hands_root(self):
        self.root.mode = "nothing"
        G["gw"].plan_wait_s = 0.2
        call("POST", "/v1/models/apply", self.body(add=["new:4b", "a:1b"], remove=["old:7b"]))
        req = json.load(open(M.request_file()))
        self.assertEqual(sorted(req), sorted(M.REQUEST_KEYS))
        self.assertEqual((req["v"], req["device"], req["plan"], req["add"], req["remove"], req["seen"]),
                         (1, G["dev"].id, "recommended", ["new:4b", "a:1b"], ["old:7b"], self.seen()))
        self.assertRegex(req["id"], r"^[0-9a-f]{16}$")
        self.assertLessEqual(abs(req["t"] - time.time()), 30)
        self.assertEqual(os.path.dirname(M.request_file()), Paths.gw_state)
        self.assertNotIn("name", req)                   # the device's name is root's to look up

    def test_the_one_thing_the_gateway_starts(self):
        mod = G["gwmod"]
        seen = []

        class R:
            returncode, stdout = 0, ""

        class No:
            returncode, stdout = 1, "Failed to start ollama1-modelplan.service: Access denied\n"
        real = mod.subprocess.run
        mod.subprocess.run = lambda argv, **kw: seen.append(argv) or R()
        try:
            self.assertEqual(mod.start_plan_unit(), (True, ""))
            mod.subprocess.run = lambda argv, **kw: No()
            self.assertEqual(mod.start_plan_unit(), (False, "Failed to start ollama1-modelplan.service: Access denied"))
            mod.subprocess.run = lambda argv, **kw: (_ for _ in ()).throw(FileNotFoundError(2, "no systemctl"))
            self.assertEqual(mod.start_plan_unit(), (False, "FileNotFoundError"))
        finally:
            mod.subprocess.run = real
        self.assertEqual(seen, [["systemctl", "start", "--no-block", "--no-ask-password", "ollama1-modelplan.service"]])
        self.assertEqual(M.UNIT, "ollama1-modelplan.service")
        # the gateway runs nothing else: its one subprocess call is that one
        src = open(os.path.join(U.BIN, "ollama1-gateway")).read()
        self.assertEqual(src.count("subprocess.run("), 1)
        self.assertNotIn("os.system", src)
        self.assertNotIn("Popen", src)
        body = src[src.index("def start_plan_unit"):src.index("\nclass ", src.index("def start_plan_unit"))]
        self.assertIn('subprocess.run(["systemctl", "start", "--no-block", "--no-ask-password", o1modelplan.UNIT]', body)

    def test_a_failed_pull_marks_its_job_and_the_next_one_runs(self):
        with self.stub.lock:
            self.stub.pull_fail = 1
        st, d = call("POST", "/v1/models/apply", self.body(add=["bad:1b", "good:1b"], remove=[]))
        self.assertEqual(st, 202, d)
        self.wait_done()
        jobs = call("GET", "/v1/models/state")[1]["jobs"]
        self.assertEqual([(j["name"], j["state"]) for j in jobs], [("bad:1b", "failed"), ("good:1b", "done")])
        self.assertEqual(jobs[0]["error"], "connection reset")
        self.assertEqual(self.root.rc, 1)
        self.assertIn("pull bad:1b: failed: connection reset", "\n".join(self.root.logs))

    def test_the_last_plan_stays_until_a_new_one_starts(self):
        stale = self.seen()
        st, d = call("POST", "/v1/models/apply", self.body())
        self.assertEqual(st, 202, d)
        self.wait_done()
        first = call("GET", "/v1/models/state")[1]
        st, d = call("POST", "/v1/models/apply", self.body(add=["x:1b"], remove=[], seen=stale))   # from before
        self.assertEqual((st, d["code"]), (409, "changed"))
        self.assertEqual(call("GET", "/v1/models/state")[1]["jobs"], first["jobs"])          # a refusal changes nothing
        st, d = call("POST", "/v1/models/apply", self.body(plan="light", add=[], remove=["zzz:1b"]))
        self.assertEqual(st, 202, d)          # off the list, and not installed: nothing to delete, done
        self.wait_done()
        again = call("GET", "/v1/models/state")[1]
        self.assertEqual(first["plan"]["name"], "recommended")
        self.assertEqual(again["plan"]["name"], "light")
        self.assertEqual([(j["name"], j["state"]) for j in again["jobs"]], [("zzz:1b", "done")])


# ---- the root program ----------------------------------------------------------------

class TestRoot(Base):
    def request(self, **over):
        obj = {"v": 1, "id": secrets.token_hex(8), "t": int(time.time()), "device": G["dev"].id, "plan": "light",
               "add": ["new:4b"], "remove": ["old:7b"], "seen": self.seen()}
        obj.update(over)
        write_json_atomic(M.request_file(), obj, mode=0o600)
        return obj

    def run_root(self, **kw):
        args = dict(owner_uid=os.getuid(), base=G["base"], active_units=lambda p: [], sleep=lambda s: None, tries=1,
                    log=self.logs.append)
        args.update(kw)
        return G["root"].run(**args)

    def answer(self):
        with open(M.answer_file()) as f:
            return json.load(f)

    def assert_unchanged(self):
        self.assertEqual(self.allow(), ALLOW)
        self.assertEqual(self.changes(), [])
        self.assertIsNone(M.read_status())

    def test_reads_only_a_file_it_can_trust(self):
        good = self.request()
        self.assertEqual(self.run_root(owner_uid=os.getuid() + 1), 2)             # not the gateway's file
        os.unlink(M.request_file())
        self.assertEqual(self.run_root(), 2)                                       # no request at all
        d = tempfile.mkdtemp(prefix="o1mp-")
        self.addCleanup(shutil.rmtree, d, True)
        other = os.path.join(d, "req.json")
        write_json_atomic(other, good, mode=0o600)
        os.symlink(other, M.request_file())                                        # a planted link
        self.assertEqual(self.run_root(), 2)
        os.unlink(M.request_file())
        os.link(other, M.request_file())                                           # a second name for it
        self.assertEqual(self.run_root(), 2)
        os.unlink(M.request_file())
        os.mkfifo(M.request_file())
        t0 = time.monotonic()
        self.assertEqual(self.run_root(), 2)                                       # never blocks on a FIFO
        self.assertLess(time.monotonic() - t0, 5)
        os.unlink(M.request_file())
        with open(M.request_file(), "w") as f:
            f.write(json.dumps(dict(good, pad="x" * M.REQUEST_MAX_BYTES)))
        self.assertEqual(self.run_root(), 2)                                       # over 16 KiB
        for text in ("not json", "[1]", json.dumps(dict(good, id="nope")), json.dumps({k: v for k, v in good.items()
                                                                                       if k != "id"})):
            with open(M.request_file(), "w") as f:
                f.write(text)
            self.assertEqual(self.run_root(), 2, text[:30])
        self.assertFalse(os.path.exists(M.answer_file()))
        self.assert_unchanged()

    def test_checks_the_shape_again(self):
        tags = ["m%d:1b" % i for i in range(41)]
        for over in ({"add": ["user/model:1b"]}, {"add": ["a:1b\n"]}, {"add": tags}, {"remove": tags},
                     {"add": ["old:7b"]}, {"plan": "huge"}, {"seen": "x"}, {"add": [], "remove": []},
                     {"extra": 1}, {"v": 2}, {"t": True}, {"t": "now"}, {"device": None}, {"device": "../x"},
                     {"add": ["gpt-oss:120b-cloud"]}):
            req = self.request(**over)
            self.assertEqual(self.run_root(), 1, over)
            a = self.answer()
            self.assertEqual((a["request"], a["ok"], a["code"]), (req["id"], False, "bad_request"), over)
        self.assert_unchanged()

    def test_a_stale_or_future_request(self):
        for t in (int(time.time()) - M.REQUEST_MAX_AGE - 5, int(time.time()) + M.REQUEST_MAX_AGE + 5):
            self.request(t=t)
            self.assertEqual(self.run_root(), 1)
            self.assertEqual(self.answer()["code"], "bad_request")
            self.assertIn("seconds old", self.answer()["error"])
        self.assert_unchanged()

    def test_a_request_is_carried_out_once(self):
        self.request()
        self.assertEqual(self.run_root(), 0)
        n = len(self.changes())
        self.assertEqual(n, 2)
        before = open(M.status_file()).read()
        self.assertEqual(self.run_root(), 0)                    # started again with the same file: nothing
        self.assertEqual(len(self.changes()), n)
        self.assertIn("answered already", self.logs[-1])
        self.assertEqual(json.loads(open(M.status_file()).read())["request"], json.loads(before)["request"])

    def test_an_unpaired_device(self):
        self.request(device="0123456789abcdef")
        self.assertEqual(self.run_root(), 1)
        self.assertEqual(self.answer()["code"], "unpaired")
        self.assert_unchanged()

    def test_the_name_comes_from_devices_json(self):
        self.request()
        self.assertEqual(self.run_root(), 0)
        self.assertEqual(M.read_status()["plan"]["by"], "Alice's Mac")

    def test_changed(self):
        self.request(seen=M.seen_hash(["keep:1b"]))
        self.assertEqual(self.run_root(), 1)
        a = self.answer()
        self.assertEqual((a["code"], a["models"]), ("changed", ["byhand:2b", "keep:1b", "old:7b"]))
        self.assert_unchanged()

    def test_in_use(self):
        with self.stub.lock:
            self.stub.loaded["old:7b"] = {"name": "old:7b", "model": "old:7b", "size": 1, "size_vram": 1}
        self.request()
        self.assertEqual(self.run_root(), 1)
        self.assertEqual((self.answer()["code"], self.answer()["name"]), ("in_use", "old:7b"))
        self.assert_unchanged()

    def test_busy(self):
        self.request()
        with L.Lock():                                          # a sync, or ollama1-models at the server
            self.assertEqual(self.run_root(), 1)
        self.assertEqual(self.answer()["code"], "busy")
        fd = held(M.lock_file())                                # another plan (a second copy run by hand)
        try:
            self.request()
            self.assertEqual(self.run_root(), 1)
            self.assertEqual(self.answer()["code"], "busy")
        finally:
            os.close(fd)
        for units in (["ollama1-pull@0123456789ab.service"], ["ollama1-rmmodel@0123456789ab.service"],
                      ["ollama1-models-sync.service"], None):
            self.request()
            self.assertEqual(self.run_root(active_units=lambda p, u=units: u), 1, units)
            self.assertEqual(self.answer()["code"], "busy", units)
        self.assert_unchanged()
        self.request()                                         # its own unit running (it is) is no reason
        self.assertEqual(self.run_root(active_units=lambda p: ["ollama1-modelplan.service"]), 0)

    def test_a_lock_held_for_a_moment_is_waited_out(self):
        fd = held(L.lock_file())
        threading.Timer(0.3, lambda: os.close(fd)).start()
        self.request()
        self.assertEqual(self.run_root(sleep=time.sleep), 0)

    def test_ollama_down(self):
        self.request()
        self.assertEqual(self.run_root(base="http://127.0.0.1:%d" % U.free_port()), 1)
        self.assertEqual(self.answer()["code"], "ollama")
        self.assertEqual(self.allow(), ALLOW)

    def test_the_allow_list_edit_is_exact(self):
        text = ("# ollama1 model allow-list\r\n# keep this comment\nkeep:1b\nOld:7b ram\n\n"
                "old:7b   ram   # the big one\n  # an indented comment\nqwen3:14b # trailing\nfast:1b turbo\n"
                "keep2:1b")          # no newline at the end
        with open(Paths.allow, "w", newline="") as f:
            f.write(text)
        ino = os.stat(Paths.allow).st_ino
        self.request(add=["keep:1b", "new:4b", "qwen3:latest"], remove=["old:7b"])
        self.assertEqual(self.run_root(), 0)
        with open(Paths.allow, newline="") as f:
            got = f.read()
        self.assertEqual(got, "# ollama1 model allow-list\r\n# keep this comment\nkeep:1b\n\n"
                              "  # an indented comment\nqwen3:14b # trailing\nfast:1b turbo\nkeep2:1b\n"
                              "new:4b\nqwen3:latest\n")
        st = os.stat(Paths.allow)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o644)
        self.assertNotEqual(st.st_ino, ino)                                  # replaced by a rename, not rewritten
        self.assertEqual([n for n in os.listdir(os.path.dirname(Paths.allow)) if n.startswith(".models.allow-")], [])
        entries, errors = parse_allow_list()
        self.assertEqual([e["name"] for e in entries], ["keep:1b", "qwen3:14b", "keep2:1b", "new:4b", "qwen3:latest"])
        self.assertEqual([e["line"] for e in errors], [7])                    # the bad line is still there, still bad

    def test_nothing_to_write_writes_nothing(self):
        ino = os.stat(Paths.allow).st_ino
        self.request(add=["keep:1b"], remove=["zzz:1b"])
        self.assertEqual(self.run_root(), 0)
        self.assertEqual((self.allow(), os.stat(Paths.allow).st_ino), (ALLOW, ino))

    def test_a_list_it_cannot_read_is_never_written(self):
        os.unlink(Paths.allow)                           # missing: a new list would let a sync delete the rest
        self.request()
        self.assertEqual(self.run_root(), 1)
        self.assertEqual(self.answer()["code"], "internal")
        self.assertFalse(os.path.exists(Paths.allow))
        with open(Paths.allow, "wb") as f:
            f.write(b"keep:1b\n\xff\xfe old:7b\n")       # not UTF-8
        self.request()
        self.assertEqual(self.run_root(), 1)
        self.assertEqual(self.answer()["code"], "internal")
        self.assertEqual(open(Paths.allow, "rb").read(), b"keep:1b\n\xff\xfe old:7b\n")
        self.assertEqual(self.changes(), [])

    def test_removals_first_then_pulls_in_the_order_given(self):
        self.request(add=["z:1b", "a:1b"], remove=["old:7b", "byhand:2b"])
        self.assertEqual(self.run_root(), 0)
        self.assertEqual(self.changes(), [("DELETE", "/api/delete", "old:7b"), ("DELETE", "/api/delete", "byhand:2b"),
                                          ("POST", "/api/pull", "z:1b"), ("POST", "/api/pull", "a:1b")])
        self.assertEqual([(j["action"], j["name"]) for j in M.read_status()["jobs"]],
                         [("remove", "old:7b"), ("remove", "byhand:2b"), ("pull", "z:1b"), ("pull", "a:1b")])

    def test_a_failed_pull_is_marked_and_the_rest_carry_on(self):
        with self.stub.lock:
            self.stub.pull_fail = 1
        self.request(add=["a:1b", "b:1b"], remove=["old:7b"])
        self.assertEqual(self.run_root(), 1)
        jobs = M.read_status()["jobs"]
        self.assertEqual([(j["name"], j["state"], j["error"]) for j in jobs],
                         [("old:7b", "done", ""), ("a:1b", "failed", "connection reset"), ("b:1b", "done", "")])
        self.assertIn("b:1b", self.stub.models)
        self.assertIn("failed pull a:1b (connection reset)", self.logs[-1])

    def test_a_broken_answer_is_tried_again_and_never_stops_the_set(self):
        import http.client
        calls, real = [], L.pull_one

        def flaky(name, progress, base=None):
            calls.append(name)
            if name == "a:1b" and calls.count("a:1b") == 1 or name == "b:1b":
                raise http.client.IncompleteRead(b"")
            return real(name, progress, base)
        L.pull_one = flaky
        try:
            self.request(add=["a:1b", "b:1b", "c:1b"], remove=[])
            self.assertEqual(self.run_root(tries=2), 1)
        finally:
            L.pull_one = real
        self.assertEqual(calls, ["a:1b", "a:1b", "b:1b", "b:1b", "c:1b"])
        self.assertEqual([(j["name"], j["state"], j["error"]) for j in M.read_status()["jobs"]],
                         [("a:1b", "done", ""), ("b:1b", "failed", "Ollama: IncompleteRead"), ("c:1b", "done", "")])

    def test_three_tries(self):
        with self.stub.lock:
            self.stub.pull_fail = 2
        waits = []
        self.request(add=["a:1b"], remove=[])
        self.assertEqual(self.run_root(tries=3, sleep=waits.append), 0)
        self.assertEqual(len(self.calls("POST", "/api/pull")), 3)
        self.assertEqual(waits, [5, 10])

    def test_the_status_files(self):
        self.request(add=["new:4b"], remove=["old:7b"])
        self.assertEqual(self.run_root(), 0)
        with open(M.status_file()) as f:
            rec = json.load(f)
        self.assertEqual(sorted(rec), ["add", "at", "device", "finished", "jobs", "plan", "remove", "request", "started",
                                       "state", "v"])
        self.assertEqual(sorted(rec["plan"]), ["at", "by", "name"])
        for j in rec["jobs"]:
            self.assertEqual(sorted(j), ["action", "error", "name", "pct", "state"])
        self.assertEqual((rec["state"], rec["device"], rec["add"], rec["remove"]), ("done", G["dev"].id, ["new:4b"],
                                                                                  ["old:7b"]))
        self.assertIsInstance(rec["finished"], int)
        with open(M.record_file()) as f:
            self.assertEqual(json.load(f), rec)                    # the copy that outlives a reboot
        for p in (M.status_file(), M.record_file(), M.answer_file()):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640, p)
        a = self.answer()
        self.assertEqual((a["request"], a["ok"]), (rec["request"], True))
        clean = M.read_status()
        self.assertEqual(sorted(clean), ["add", "finished", "jobs", "plan", "remove", "request", "started", "state"])

    def test_after_a_reboot_the_record_is_read_and_a_cut_short_plan_says_so(self):
        self.request()
        self.assertEqual(self.run_root(), 0)
        os.unlink(M.status_file())                                 # /run is emptied at boot
        self.assertEqual(M.read_status()["plan"]["name"], "light")
        self.assertEqual(M.read_status()["state"], "done")
        rec = json.load(open(M.record_file()))
        rec["state"] = "running"
        rec["jobs"][1]["state"] = "running"
        write_json_atomic(M.record_file(), rec)
        st = M.read_status()
        self.assertEqual(st["state"], "interrupted")
        self.assertEqual([(j["state"], j["error"] == M.INTERRUPTED) for j in st["jobs"]],
                         [("done", False), ("failed", True)])
        self.assertIs(call("GET", "/v1/models/state")[1]["busy"], False)
        fd = held(M.lock_file())                                   # still running: the lock says so
        try:
            write_json_atomic(M.status_file(), rec)
            self.assertEqual(M.read_status()["state"], "running")
        finally:
            os.close(fd)

    def test_a_job_checks_again_right_before(self):
        R = G["root"]
        job = {"name": "keep:1b", "action": "remove"}
        self.assertIn("names it again", R.remove_job(job, G["base"], None))       # on the list (put back meanwhile)
        with open(Paths.allow, "a") as f:
            f.write("BYHAND:2b # in another case\n")
        self.assertIn("names it again", R.remove_job({"name": "byhand:2b", "action": "remove"}, G["base"], None))
        with self.stub.lock:
            self.stub.loaded["old:7b"] = {"name": "old:7b", "model": "old:7b", "size": 1, "size_vram": 1}
        with open(Paths.allow, "w") as f:
            f.write("keep:1b\n")
        self.assertIn("loaded right now", R.remove_job({"name": "old:7b", "action": "remove"}, G["base"], None))
        self.assertEqual(R.remove_job({"name": "never:1b", "action": "remove"}, G["base"], None), "")  # nothing there
        self.assertEqual(self.changes(), [])
        self.assertIn("no longer on the allow-list",
                      R.pull_job({"name": "new:4b", "action": "pull"}, G["base"], None, lambda s: None, 1, None))
        with open(Paths.allow, "w") as f:
            f.write("new:4b\n")
        real = M.disk_free
        try:
            M.disk_free = lambda path=None: 1 << 30
            self.assertIn("less than 2.0 GiB free",
                          R.pull_job({"name": "new:4b", "action": "pull"}, G["base"], None, lambda s: None, 1, None))
            M.disk_free = lambda path=None: None
            self.assertIn("can't read the free space",
                          R.pull_job({"name": "new:4b", "action": "pull"}, G["base"], None, lambda s: None, 1, None))
        finally:
            M.disk_free = real
        self.assertEqual(self.changes(), [])

    def test_progress_is_written_while_a_pull_runs(self):
        seen = []
        R = G["root"]
        orig = R.save

        def save(rec, keep=False):
            seen.append([(j["state"], j["pct"]) for j in rec["jobs"]])
            return orig(rec, keep)
        R.save = save
        try:
            with self.stub.lock:
                self.stub.pull_layers["big:1b"] = [("sha256:" + "1" * 64, 4000, False)]
            self.request(add=["big:1b"], remove=[])
            self.assertEqual(self.run_root(), 0)
        finally:
            R.save = orig
        self.assertEqual(seen[0], [("queued", 0)])
        self.assertIn([("running", 0)], seen)
        self.assertIn([("running", 25)], seen)                 # the first quarter of the layer, at most once a second
        self.assertEqual(seen[-1], [("done", 100)])

    def test_the_journal(self):
        self.request(add=["new:4b"], remove=["old:7b"])
        self.assertEqual(self.run_root(), 0)
        text = "\n".join(self.logs)
        self.assertRegex(text, r"applying the light set for Alice's Mac \(device %s, request [0-9a-f]{16}\): "
                               r"add new:4b; remove old:7b; allow-list: 1 line\(s\) added, 1 taken out" % G["dev"].id)
        self.assertIn("remove old:7b: done", text)
        self.assertIn("pull new:4b: done", text)
        self.assertIn("finished the light set for Alice's Mac: removed old:7b; pulled new:4b; failed -", text)
        self.request(seen="ab" * 32)
        self.run_root()
        self.assertRegex(self.logs[-1], r"refused request [0-9a-f]{16} \(changed\)")

    def test_the_program(self):
        """bin/ollama1-modelplan end to end, in its own prefix, with a stand-in systemctl."""
        pre = tempfile.mkdtemp(prefix="o1mp-cli-")
        self.addCleanup(shutil.rmtree, pre, True)
        for d in ("etc/ollama1", "run/ollama1/modelplan", "run/ollama1/library", "var/lib/ollama1-gateway",
                  "var/lib/ollama1-modelplan", "srv/models", "fakebin"):
            os.makedirs(os.path.join(pre, d))
        with open(os.path.join(pre, "etc/ollama1/config.json"), "w") as f:
            json.dump({"ollama_url": G["base"]}, f)
        with open(os.path.join(pre, "etc/ollama1/devices.json"), "w") as f:
            json.dump({"version": 1, "devices": [G["dev"].record()]}, f)
        with open(os.path.join(pre, "etc/ollama1/models.allow"), "w") as f:
            f.write(ALLOW)
        fake = os.path.join(pre, "fakebin", "systemctl")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")                  # list-units: nothing running
        os.chmod(fake, 0o755)
        req = {"v": 1, "id": secrets.token_hex(8), "t": int(time.time()), "device": G["dev"].id, "plan": "everything",
               "add": ["new:4b"], "remove": ["old:7b"], "seen": self.seen()}
        with open(os.path.join(pre, "var/lib/ollama1-gateway/modelplan-request.json"), "w") as f:
            json.dump(req, f)
        env = dict(os.environ, OLLAMA1_PREFIX=pre, PATH=os.path.join(pre, "fakebin") + os.pathsep + os.environ["PATH"])
        prog = os.path.join(U.BIN, "ollama1-modelplan")
        run = lambda *a: subprocess.run([sys.executable, prog, *a], capture_output=True, text=True, env=env,  # noqa: E731
                                        timeout=60)
        r = run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("finished the everything set for Alice's Mac", r.stdout)
        with open(os.path.join(pre, "etc/ollama1/models.allow")) as f:
            self.assertEqual(f.read(), "# my models\nkeep:1b\n\nqwen3:14b\nnew:4b\n")
        with open(os.path.join(pre, "run/ollama1/modelplan/answer.json")) as f:
            self.assertEqual(json.load(f)["ok"], True)
        r = run()
        self.assertEqual(r.returncode, 0)
        self.assertIn("answered already", r.stdout)
        self.assertEqual(run("extra").returncode, 2)
        if os.geteuid() != 0:
            env2 = {k: v for k, v in env.items() if k != "OLLAMA1_PREFIX"}
            r = subprocess.run([sys.executable, prog], capture_output=True, text=True, env=env2, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("runs as root", r.stderr)


# ---- the privilege line: unit, polkit, tmpfiles, setup ------------------------------

class TestBoundary(unittest.TestCase):
    def test_the_unit_is_root_sandboxed_and_loopback_only(self):
        u = UNIT_TEXT()
        for want in ("Type=oneshot\n", "ExecStart=/usr/local/lib/ollama1/bin/ollama1-modelplan\n", "NoNewPrivileges=yes\n",
                     "ProtectSystem=strict\n", "ProtectHome=yes\n", "PrivateTmp=yes\n", "PrivateDevices=yes\n",
                     "IPAddressDeny=any\n", "IPAddressAllow=localhost\n", "AmbientCapabilities=\n",
                     "CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_CHOWN\n", "SystemCallFilter=@system-service\n",
                     "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n", "ProtectProc=invisible\n",
                     "MemoryDenyWriteExecute=yes\n", "RestrictSUIDSGID=yes\n", "LockPersonality=yes\n", "UMask=0027\n",
                     "StateDirectory=ollama1-modelplan\n", "LimitCORE=0\n", "ProtectKernelTunables=yes\n",
                     "ProtectControlGroups=yes\n", "RestrictNamespaces=yes\n"):
            self.assertIn(want, u)
        rw = re.findall(r"^ReadWritePaths=(.*)$", u, re.M)
        self.assertEqual(rw, ["/etc/ollama1 /run/ollama1/modelplan /run/ollama1/library"])
        self.assertEqual(re.findall(r"^ReadOnlyPaths=(.*)$", u, re.M),
                         ["-/etc/ollama1/config.json -/etc/ollama1/devices.json"])
        settings = re.sub(r"(?m)^#.*$", "", u)
        self.assertNotIn("ollama1-gateway", settings)                         # never the gateway's folder
        self.assertNotIn("CAP_DAC_OVERRIDE", settings)                        # reads it (DAC_READ_SEARCH), can't write it
        self.assertNotIn("CAP_FOWNER", settings)
        self.assertNotRegex(u, r"(?m)^User=")                                 # root
        self.assertNotIn("[Install]", u)                                       # started by the gateway, never at boot
        self.assertNotIn("%i", u)                                              # no argument at all
        self.assertNotRegex(u, r"(?m)^(PrivateNetwork|IPAddressAllow=any|DynamicUser)")

    def test_the_gateway_gains_nothing(self):
        g = open(os.path.join(U.SYSTEMD, "ollama1-gateway.service")).read()
        self.assertIn("CapabilityBoundingSet=\n", g)
        self.assertIn("NoNewPrivileges=yes\n", g)
        self.assertEqual(re.findall(r"^ReadWritePaths=(.*)$", g, re.M), ["/run/ollama1/stats /run/ollama1/pair-spool"])
        self.assertIn("StateDirectory=ollama1-gateway\n", g)          # where it leaves the request
        self.assertEqual(os.path.dirname(M.request_file()), Paths.gw_state)

    def gw_rule(self):
        rule = RULES_TEXT()
        first = rule.index("polkit.addRule(")
        second = rule.index("polkit.addRule(", first + 1)
        self.assertEqual(rule.count("polkit.addRule("), 3)                    # the panel's, the gateway's, the screen's (6b418)
        return rule[second:rule.index("\n});", second) + 4]

    def test_polkit_lets_the_gateway_start_that_one_unit(self):
        r = self.gw_rule()
        self.assertIn('if (subject.user !== "o1gw") {\n        return polkit.Result.NOT_HANDLED;', r)
        self.assertIn('action.id === "org.freedesktop.systemd1.manage-units" &&', r)
        self.assertIn('action.lookup("unit") === "ollama1-modelplan.service" &&', r)
        self.assertIn('action.lookup("verb") === "start")', r)
        self.assertEqual(r.count("polkit.Result.YES"), 1)
        self.assertTrue(r.rstrip().endswith("return polkit.Result.NO;\n});".rstrip()))
        self.assertEqual(re.findall(r'"(ollama1-[^"]*)"', r), ["ollama1-modelplan.service"])
        rule = RULES_TEXT()
        self.assertEqual(rule.count("ollama1-modelplan"), 2)               # this rule and its comment only
        first = rule.index("polkit.addRule(")
        admin = rule[first:rule.index("\n});", first)]
        self.assertNotIn("o1gw", admin)                                    # the panel's rule never grants it
        self.assertNotIn("modelplan", admin)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_polkit_rule_logic(self):
        r = subprocess.run(["node", os.path.join(U.HERE, "polkit_check.js"), os.path.join(U.CONFIG, "50-ollama1.rules")],
                           capture_output=True, text=True, timeout=30)
        self.assertIn("polkit rule ok", r.stdout, r.stdout + r.stderr)
        self.assertIn("'o1gw','org.freedesktop.systemd1.manage-units','ollama1-modelplan.service','start','yes'",
                      open(os.path.join(U.HERE, "polkit_check.js")).read())

    def test_tmpfiles_setup_and_the_files(self):
        t = open(os.path.join(U.CONFIG, "ollama1.tmpfiles")).read()
        self.assertRegex(t, r"(?m)^d /run/ollama1/modelplan\s+0750 root\s+o1view\s+-$")
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        check = setup[setup.index("for f in lib/o1common.py"):setup.index("done", setup.index("for f in lib/o1common.py"))]
        for f in ("bin/ollama1-modelplan", "systemd/ollama1-modelplan.service", "config/50-ollama1.rules"):
            self.assertIn(f, check)
        self.assertIn('install -m 0755 "$KIT"/bin/* "$LIBDIR/bin/"', setup)
        self.assertIn('install -m 0644 "$KIT"/systemd/* /etc/systemd/system/', setup)
        self.assertTrue(os.access(os.path.join(U.BIN, "ollama1-modelplan"), os.X_OK))
        self.assertEqual(M.request_file(), os.path.join(Paths.gw_state, "modelplan-request.json"))
        self.assertTrue(M.status_file().startswith(Paths.run + os.sep))
        self.assertTrue(M.record_file().startswith(Paths.modelplan_state + os.sep))

    def test_the_gateway_writes_the_request_private(self):
        clean, _ = M.parse_request({"plan": "light", "add": ["a:1b"], "remove": [], "seen": "ab" * 32})
        M.write_request("0123456789abcdef", "fedcba9876543210", clean, time.time())
        try:
            self.assertEqual(stat.S_IMODE(os.stat(M.request_file()).st_mode), 0o600)
        finally:
            M.drop_request()


class TestLocksAndSleep(unittest.TestCase):
    def test_the_library_lock_can_be_seen_by_the_gateway_and_panel(self):
        os.makedirs(L.library_dir(), exist_ok=True)
        with L.Lock():
            self.assertEqual(stat.S_IMODE(os.stat(L.lock_file()).st_mode), 0o640)
            self.assertTrue(o1sleep.setup_running(L.lock_file()))
        self.assertFalse(o1sleep.setup_running(L.lock_file()))

    def test_sleep_waits_for_a_set_and_says_so_once(self):
        os.makedirs(L.library_dir(), exist_ok=True)
        nolock = os.path.join(U.PREFIX, "nolock")
        r = o1sleep.busy_reasons(active_units=lambda p: ["ollama1-modelplan.service"], setup_lock=nolock)
        self.assertEqual(r, ["a model set from the app is being applied"])
        with L.Lock():                                    # the plan holds the library lock too
            r = o1sleep.busy_reasons(active_units=lambda p: ["ollama1-modelplan.service"], setup_lock=nolock)
        self.assertEqual(r, ["a model set from the app is being applied"])
        self.assertIn("ollama1-modelplan.service", [p for p, _ in o1sleep.BUSY_UNITS])
        self.assertEqual(G["root"].WHY, "a model set from the app is being applied")


if __name__ == "__main__":
    unittest.main()
