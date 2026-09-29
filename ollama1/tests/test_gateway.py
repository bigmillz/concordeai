"""Gateway: Access JWT, request signatures, replay, skew, pairing, GPU-only,
and the routes that must never be reachable. Runs against a stub Ollama
and a local fake Access certs endpoint; nothing leaves 127.0.0.1."""
import importlib.machinery
import importlib.util
import json
import os
import threading
import time
import unittest

import o1test_util as U  # sets OLLAMA1_PREFIX first

import o1pair  # noqa: E402
from o1common import DEFAULTS, Paths  # noqa: E402
from stub_ollama import Stub  # noqa: E402

GIB = 1 << 30


def load_gateway():
    path = os.path.join(U.BIN, "ollama1-gateway")
    loader = importlib.machinery.SourceFileLoader("o1gateway", path)
    spec = importlib.util.spec_from_loader("o1gateway", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


G = {}


def setUpModule():
    G["mod"] = load_gateway()
    G["mod"].PAIR_MIN_INTERVAL = 0.0
    G["key"] = U.RSAKey("kid-1")
    G["other"] = U.RSAKey("kid-1")        # same kid, different key: a forgery
    G["jwks"] = U.FakeJWKS([G["key"]])
    G["stub"] = Stub(U.free_port())
    cfg = dict(DEFAULTS)
    cfg.update({"access_team_domain": U.TEAM, "gateway_aud": U.GW_AUD, "admin_aud": U.ADMIN_AUD,
                "admin_email": U.ADMIN_EMAIL, "service_token_client_id": U.CLIENT_ID,
                "gateway_port": U.free_port(), "ollama_url": "http://127.0.0.1:%d" % G["stub"].port,
                "certs_url": G["jwks"].url, "allow_insecure_certs_url": True,
                "vram_total_bytes": 16 * GIB, "queue_wait_s": 20})
    G["cfg"] = cfg
    G["dev"] = U.Device(name="Pat's Mac")
    U.write_devices([G["dev"]])
    G["gw"], G["servers"], G["stop"] = G["mod"].serve(cfg)
    G["port"] = cfg["gateway_port"]
    time.sleep(1.1)  # signatures must be dated after the gateway started


def tearDownModule():
    G["stop"].set()
    for s in G["servers"]:
        s.shutdown()
    G["stub"].close()
    G["jwks"].close()


def jwt(**kw):
    return U.make_jwt(G["key"], U.claims(**kw))


def call(method, path, obj=None, dev=None, jwt_token="default", headers=None, raw=None, **sign):
    body = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
    h = {}
    if jwt_token == "default":
        h["Cf-Access-Jwt-Assertion"] = jwt()
    elif jwt_token:
        h["Cf-Access-Jwt-Assertion"] = jwt_token
    if dev is not False:
        h.update((dev or G["dev"]).headers(method, path, body, **sign))
    h.update(headers or {})
    return U.request(G["port"], method, path, body, h)


class TestAccess(unittest.TestCase):
    def test_valid_request_passes(self):
        st, data, _ = call("GET", "/api/tags")
        self.assertEqual(st, 200, data)
        names = [m["name"] for m in json.loads(data)["models"]]
        self.assertIn("small:8b", names)

    def test_cloud_models_are_hidden_and_refused(self):
        st, data, _ = call("GET", "/api/tags")
        names = [m["name"] for m in json.loads(data)["models"]]
        for cloud in ("gpt-oss:120b-cloud", "plain:latest", "plain"):
            self.assertNotIn(cloud, names)
            st, data, _ = call("POST", "/api/chat", {"model": cloud,
                                                     "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(st, 404, cloud)

    def test_missing_jwt(self):
        st, data, _ = call("GET", "/api/tags", jwt_token=None)
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(data)["code"], "access")

    def test_wrong_audience(self):
        st, _, _ = call("GET", "/api/tags", jwt_token=jwt(aud=U.ADMIN_AUD))
        self.assertEqual(st, 403)

    def test_expired(self):
        now = int(time.time())
        st, _, _ = call("GET", "/api/tags", jwt_token=jwt(iat=now - 900, nbf=now - 900, exp=now - 120))
        self.assertEqual(st, 403)

    def test_not_yet_valid(self):
        now = int(time.time())
        st, _, _ = call("GET", "/api/tags", jwt_token=jwt(nbf=now + 600))
        self.assertEqual(st, 403)

    def test_wrong_issuer(self):
        st, _, _ = call("GET", "/api/tags", jwt_token=jwt(iss="https://evil.cloudflareaccess.com"))
        self.assertEqual(st, 403)

    def test_forged_signature_same_kid(self):
        st, _, _ = call("GET", "/api/tags", jwt_token=U.make_jwt(G["other"], U.claims()))
        self.assertEqual(st, 403)

    def test_alg_none(self):
        tok = U.make_jwt(G["key"], U.claims(), alg="none")
        st, _, _ = call("GET", "/api/tags", jwt_token=tok)
        self.assertEqual(st, 403)

    def test_unknown_kid(self):
        st, _, _ = call("GET", "/api/tags", jwt_token=U.make_jwt(G["key"], U.claims(), kid="nope"))
        self.assertEqual(st, 403)

    def test_tampered_payload(self):
        h, p, s = jwt().split(".")
        p2 = U.b64u(json.dumps(U.claims(common_name="someone-else")).encode())
        st, _, _ = call("GET", "/api/tags", jwt_token=h + "." + p2 + "." + s)
        self.assertEqual(st, 403)

    def test_other_service_token(self):
        st, _, _ = call("GET", "/api/tags", jwt_token=jwt(common_name="another.access"))
        self.assertEqual(st, 403)

    def test_wrong_host(self):
        body = b""
        h = {"Cf-Access-Jwt-Assertion": jwt()}
        h.update(G["dev"].headers("GET", "/api/tags", body))
        st, _, _ = U.request(G["port"], "GET", "/api/tags", body, h, host="evil.example")
        self.assertEqual(st, 403)


class TestSignatures(unittest.TestCase):
    def test_unsigned(self):
        st, data, _ = call("GET", "/api/tags", dev=False)
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(data)["code"], "unsigned")

    def test_unpaired_device(self):
        st, data, _ = call("GET", "/api/tags", dev=U.Device())
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(data)["code"], "unpaired")

    def test_bad_signature(self):
        h = G["dev"].headers("GET", "/api/tags")
        h["X-O1-Signature"] = U.b64u(bytes(64))
        st, data, _ = call("GET", "/api/tags", dev=False, headers=h)
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(data)["code"], "bad_signature")

    def test_signature_from_other_key_with_paired_id(self):
        other = U.Device()
        h = other.headers("GET", "/api/tags")
        h["X-O1-Device"] = G["dev"].id
        st, data, _ = call("GET", "/api/tags", dev=False, headers=h)
        self.assertEqual(st, 401)

    def test_tampered_body(self):
        obj = {"model": "small:8b", "messages": [{"role": "user", "content": "one"}], "stream": False}
        body = json.dumps(obj).encode()
        h = G["dev"].headers("POST", "/api/chat", body)
        h["Cf-Access-Jwt-Assertion"] = jwt()
        tampered = body.replace(b"one", b"two")
        st, data, _ = U.request(G["port"], "POST", "/api/chat", tampered, h)
        self.assertEqual(st, 401)

    def test_tampered_path(self):
        h = G["dev"].headers("GET", "/api/ps")
        st, _, _ = call("GET", "/api/tags", dev=False, headers=h)
        self.assertEqual(st, 401)

    def test_tampered_method(self):
        h = G["dev"].headers("POST", "/api/tags")
        st, _, _ = call("GET", "/api/tags", dev=False, headers=h)
        self.assertEqual(st, 401)

    def test_replay(self):
        h = G["dev"].headers("GET", "/api/tags")
        h["Cf-Access-Jwt-Assertion"] = jwt()
        st1, _, _ = U.request(G["port"], "GET", "/api/tags", b"", h)
        st2, data, _ = U.request(G["port"], "GET", "/api/tags", b"", h)
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 401)
        self.assertEqual(json.loads(data)["code"], "replay")

    def test_skew_too_old(self):
        st, data, _ = call("GET", "/api/tags", ts=int(time.time()) - 61)
        self.assertEqual(st, 401)
        body = json.loads(data)
        self.assertEqual(body["code"], "clock_skew")
        self.assertIn("server_time", body)

    def test_skew_too_new(self):
        st, _, _ = call("GET", "/api/tags", ts=int(time.time()) + 61)
        self.assertEqual(st, 401)

    def test_skew_inside_window(self):
        st, _, _ = call("GET", "/api/tags", ts=int(time.time()) + 45)
        self.assertEqual(st, 200)

    def test_signed_before_start_refused(self):
        st, _, _ = call("GET", "/api/tags", ts=G["gw"].verifier.start_time - 1)
        self.assertEqual(st, 401)

    def test_whoami(self):
        st, data, _ = call("GET", "/v1/whoami")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["device_id"], G["dev"].id)


class TestRoutes(unittest.TestCase):
    def test_never_routes(self):
        before = len(G["stub"].calls)
        for path in ("/api/pull", "/api/delete", "/api/create", "/api/copy", "/api/push",
                     "/api/blobs/sha256:00"):
            for method in ("POST", "DELETE"):
                st, _, _ = call(method, path, {"model": "small:8b"} if method == "POST" else None)
                self.assertEqual(st, 404, (method, path))
        reached = [c[1] for c in G["stub"].calls[before:]]
        for bad in ("/api/pull", "/api/delete", "/api/create", "/api/copy", "/api/push"):
            self.assertNotIn(bad, reached)

    def test_unknown_route(self):
        st, _, _ = call("GET", "/")
        self.assertEqual(st, 404)


class TestModels(unittest.TestCase):
    def setUp(self):
        G["stub"].loaded.clear()

    def chat(self, model, text="hello", stream=True, **extra):
        obj = {"model": model, "messages": [{"role": "user", "content": text}], "stream": stream}
        obj.update(extra)
        return call("POST", "/api/chat", obj)

    def test_stream_chat(self):
        st, data, r = self.chat("small:8b", "ping-XYZ")
        self.assertEqual(st, 200)
        lines = U.ndjson(data)
        self.assertTrue(lines[-1]["done"])
        self.assertIn("ANSWER-", lines[0]["message"]["content"])
        self.assertIn("no-transform", r.getheader("Cache-Control"))

    def test_non_stream_generate(self):
        st, data, _ = call("POST", "/api/generate", {"model": "small:8b", "prompt": "hi", "stream": False})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(data)["response"].startswith("ANSWER-"))

    def test_too_big_refused_before_loading(self):
        before = len(G["stub"].calls)
        st, data, _ = self.chat("huge:70b")
        self.assertEqual(st, 507)
        self.assertEqual(json.loads(data)["code"], "gpu_fit")
        loads = [c for c in G["stub"].calls[before:] if c[1] in ("/api/generate", "/api/chat")]
        self.assertEqual(loads, [])

    def test_context_that_does_not_fit(self):
        st, data, _ = self.chat("small:8b", options={"num_ctx": 131072})
        self.assertEqual(st, 507)
        self.assertEqual(json.loads(data)["code"], "gpu_fit")

    def test_spill_is_unloaded_and_refused(self):
        st, data, _ = self.chat("sneaky:14b")
        self.assertEqual(st, 200)  # streaming: headers go out before the load
        lines = U.ndjson(data)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["code"], "gpu_spill")
        self.assertNotIn("sneaky:14b", G["stub"].loaded)
        chats = [c for c in G["stub"].calls if c[1] == "/api/chat" and c[2].get("model") == "sneaky:14b"]
        self.assertEqual(chats, [])

    def test_spill_non_stream(self):
        st, data, _ = self.chat("sneaky:14b", stream=False)
        self.assertEqual(st, 507)
        self.assertEqual(json.loads(data)["code"], "gpu_spill")

    def test_embed_spill_discards_result(self):
        st, data, _ = call("POST", "/api/embed", {"model": "embed:spill", "input": "x"})
        self.assertEqual(st, 507)
        self.assertNotIn("embeddings", json.loads(data))
        self.assertNotIn("embed:spill", G["stub"].loaded)

    def test_embed_ok(self):
        st, data, _ = call("POST", "/api/embed", {"model": "embed:small", "input": "x"})
        self.assertEqual(st, 200)
        self.assertIn("embeddings", json.loads(data))

    def test_load_options_are_stripped(self):
        before = len(G["stub"].calls)
        st, _, _ = self.chat("small:8b", options={"num_gpu": 1, "main_gpu": 1, "use_mmap": False,
                                                   "num_thread": 64, "temperature": 0.2},
                             keep_alive=-1)
        self.assertEqual(st, 200)
        sent = [c[2] for c in G["stub"].calls[before:] if c[1] == "/api/chat"][-1]
        self.assertEqual(sent["options"], {"temperature": 0.2, "num_ctx": 8192})
        self.assertNotIn("keep_alive", sent)

    def test_not_installed(self):
        st, _, _ = self.chat("nothere:1b")
        self.assertEqual(st, 404)


class TestPairing(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.th = threading.Thread(target=self.committer, daemon=True)
        self.th.start()
        G["mod"].PAIR_MIN_INTERVAL = 0.0

    def tearDown(self):
        self.stop.set()
        self.th.join()
        o1pair.close_window()
        U.write_devices([G["dev"]])
        G["mod"].PAIR_MIN_INTERVAL = 0.0

    def committer(self):  # stands in for ollama1-pair-commit.path (root)
        while not self.stop.is_set():
            o1pair.commit_spool(log=lambda *_: None)
            time.sleep(0.05)

    def pair(self, dev, code, ts=None, nonce=None, jwt_token="default"):
        from o1auth import pair_mac
        ts = int(time.time()) if ts is None else ts
        nonce = nonce or U.b64u(os.urandom(16))
        pub = U.b64u(dev.pub)
        obj = {"name": dev.name, "public_key": pub, "timestamp": ts, "nonce": nonce,
               "mac": U.b64u(pair_mac(code, dev.name, pub, ts, nonce))}
        return call("POST", "/v1/pair", obj, dev=False, jwt_token=jwt_token)

    def test_closed_window(self):
        st, data, _ = self.pair(U.Device(), "ABCD1234")
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(data)["code"], "pair_closed")

    def test_pair_then_use(self):
        from o1auth import pair_proof
        w = o1pair.open_window()
        dev = U.Device(name="New Mac")
        st, data, _ = self.pair(dev, w["code"])
        self.assertEqual(st, 200, data)
        out = json.loads(data)
        self.assertEqual(out["device_id"], dev.id)
        self.assertIsNone(o1pair.read_window())  # one success closes the window
        st, _, _ = call("GET", "/v1/whoami", dev=dev)
        self.assertEqual(st, 200)

    def test_code_is_case_and_dash_insensitive(self):
        w = o1pair.open_window()
        code = w["code"].lower()
        st, _, _ = self.pair(U.Device(), code[:4] + "-" + code[4:])
        self.assertEqual(st, 200)

    def test_one_success_only(self):
        w = o1pair.open_window()
        st1, _, _ = self.pair(U.Device(), w["code"])
        # the gateway itself refuses a second one even if the file came back
        with open(Paths.window, "w") as f:
            json.dump(w, f)
        st2, data, _ = self.pair(U.Device(), w["code"])
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 403)

    def test_wrong_codes_close_window(self):
        w = o1pair.open_window()
        codes = []
        for _ in range(5):
            st, data, _ = self.pair(U.Device(), "ZZZZZZZZ" if w["code"] != "ZZZZZZZZ" else "YYYYYYYY")
            codes.append((st, json.loads(data)["code"]))
        self.assertEqual(codes[:4], [(401, "wrong_code")] * 4)
        self.assertEqual(codes[4], (403, "pair_closed"))
        st, data, _ = self.pair(U.Device(), w["code"])  # even the right code now
        self.assertEqual(st, 403)
        time.sleep(0.3)
        self.assertIsNone(o1pair.read_window())  # root closed the window

    def test_rate_limit(self):
        G["mod"].PAIR_MIN_INTERVAL = 2.0
        G["gw"].pair["last_try"] = 0.0
        w = o1pair.open_window()
        st1, _, _ = self.pair(U.Device(), "ZZZZZZZZ")
        st2, data, _ = self.pair(U.Device(), w["code"])
        self.assertEqual(st1, 401)
        self.assertEqual(st2, 429)

    def test_expired_window(self):
        w = o1pair.open_window(seconds=1)
        time.sleep(1.2)
        st, _, _ = self.pair(U.Device(), w["code"])
        self.assertEqual(st, 403)

    def test_pair_needs_access_jwt(self):
        w = o1pair.open_window()
        st, _, _ = self.pair(U.Device(), w["code"], jwt_token=None)
        self.assertEqual(st, 403)

    def test_pair_skew(self):
        w = o1pair.open_window()
        st, data, _ = self.pair(U.Device(), w["code"], ts=int(time.time()) - 120)
        self.assertEqual(st, 401)

    def test_root_commit_rechecks_code(self):
        """A request dropped into the spool without the right code (as a
        compromised gateway might) is not added."""
        w = o1pair.open_window()
        self.stop.set()
        self.th.join()
        dev = U.Device()
        from o1auth import pair_mac
        ts, nonce, pub = int(time.time()), U.b64u(os.urandom(16)), U.b64u(dev.pub)
        o1pair.spool_request(w["id"], {"name": "x", "public_key": pub, "timestamp": ts,
                                       "nonce": nonce, "mac": U.b64u(pair_mac("WRONGCOD", "x", pub, ts, nonce))})
        self.assertIsNone(o1pair.commit_spool(log=lambda *_: None))
        self.assertNotIn(dev.id, o1pair.load_devices())


class TestLanMode(unittest.TestCase):
    """The LAN listener skips the Access JWT only when LAN mode is on and
    the caller is on the home network."""

    def test_lan_listener(self):
        gw = G["gw"]
        mod = G["mod"]
        with self.assertRaises(mod.GatewayError):
            gw.check_access({}, True, "192.168.86.20")        # LAN mode off (the default)
        gw.cfg["lan_mode"] = True
        try:
            gw.check_access({}, True, "192.168.86.20")        # on, from the LAN: no JWT needed
            for ip in ("10.0.0.5", "192.168.87.20", "not-an-ip"):
                with self.assertRaises(mod.GatewayError):
                    gw.check_access({}, True, ip)
            with self.assertRaises(mod.GatewayError):
                gw.check_access({"Host": G["cfg"]["hostname_gateway"]}, False, "127.0.0.1")  # tunnel side still needs it
        finally:
            gw.cfg["lan_mode"] = False

    def test_lan_off_by_default(self):
        self.assertFalse(DEFAULTS["lan_mode"])


class TestStatsFile(unittest.TestCase):
    def test_snapshot_has_counts(self):
        call("POST", "/api/chat", {"model": "small:8b", "messages": [{"role": "user", "content": "x"}]})
        time.sleep(1.5)
        with open(Paths.stats) as f:
            snap = json.load(f)
        self.assertGreaterEqual(snap["requests_today"], 1)
        self.assertIn("tps", snap)
        self.assertTrue(any(d["id"] == G["dev"].id for d in snap["devices"]))


if __name__ == "__main__":
    unittest.main()
