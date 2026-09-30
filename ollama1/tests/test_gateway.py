"""Gateway: Access JWT, request signatures, replay, skew, pairing, GPU-only,
and the routes that must never be reachable. Runs against a stub Ollama
and a local fake Access certs endpoint; nothing leaves 127.0.0.1."""
import importlib.machinery
import importlib.util
import json
import os
import socket
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
        st, data, _ = call("GET", "/api/tags", ts=int(time.time()) - 62)  # 61 can round to 60 s
        self.assertEqual(st, 401)
        body = json.loads(data)
        self.assertEqual(body["code"], "clock_skew")
        self.assertIn("server_time", body)

    def test_skew_too_new(self):
        st, _, _ = call("GET", "/api/tags", ts=int(time.time()) + 62)  # 61 can round to 60 s
        self.assertEqual(st, 401)

    def test_skew_inside_window(self):
        st, _, _ = call("GET", "/api/tags", ts=int(time.time()) + 45)
        self.assertEqual(st, 200)

    def test_signed_before_start_refused(self):
        st, _, _ = call("GET", "/api/tags", ts=G["gw"].verifier.start_time - 1)
        self.assertEqual(st, 401)

    def test_info_gpu_only(self):
        gw = G["gw"]
        saved = gw.gpu_info
        gw.gpu_info = {"vendor": "amd", "name": "Radeon RX 6900 XT", "vram_bytes": 17163091968,
                       "serial": "NOT-REPORTED", "pci": "0000:2d:00.0"}
        try:
            st, data, _ = call("GET", "/v1/info")
        finally:
            gw.gpu_info = saved
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data), {"gpu": {"vendor": "amd", "name": "Radeon RX 6900 XT",
                                                    "vram_bytes": 17163091968}})
        self.assertEqual(call("GET", "/v1/info", dev=False)[0], 401)          # signed only
        self.assertEqual(call("GET", "/v1/info", dev=U.Device())[0], 403)     # paired only

    def test_info_detected_once(self):
        mod = G["mod"]
        calls = []
        real = mod.o1gpu.detect
        mod.o1gpu.detect = lambda: calls.append(1) or {"vendor": None, "name": None, "vram_bytes": None}
        try:
            for _ in range(3):
                call("GET", "/v1/info")
        finally:
            mod.o1gpu.detect = real
        self.assertEqual(calls, [])       # the value from start-up is served

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

    def body(self, dev, code, ts=None, nonce=None):
        from o1auth import pair_mac
        ts = int(time.time()) if ts is None else ts
        nonce = nonce or U.b64u(os.urandom(16))
        pub = U.b64u(dev.pub)
        return {"name": dev.name, "public_key": pub, "timestamp": ts, "nonce": nonce,
                "mac": U.b64u(pair_mac(code, dev.name, pub, ts, nonce))}

    def pair(self, dev, code, ts=None, nonce=None, jwt_token="default"):
        return call("POST", "/v1/pair", self.body(dev, code, ts, nonce), dev=False, jwt_token=jwt_token)

    def test_closed_window(self):
        st, data, _ = self.pair(U.Device(), "ABCD1234EFGH")
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(data)["code"], "pair_closed")

    def test_code_is_12_characters(self):
        w = o1pair.open_window()
        self.assertEqual(len(w["code"]), 12)
        from o1auth import format_code
        self.assertRegex(format_code(w["code"]), r"^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$")

    def test_pair_then_use(self):
        from o1auth import pair_proof
        w = o1pair.open_window()
        dev = U.Device(name="New Mac")
        b = self.body(dev, w["code"])
        st, data, _ = call("POST", "/v1/pair", b, dev=False)
        self.assertEqual(st, 200, data)
        out = json.loads(data)
        self.assertEqual(out["device_id"], dev.id)
        self.assertEqual(out["proof"], U.b64u(pair_proof(w["code"], dev.id, b["public_key"], b["nonce"])))
        self.assertIsNone(o1pair.read_window())  # one success closes the window
        st, _, _ = call("GET", "/v1/whoami", dev=dev)
        self.assertEqual(st, 200)

    def test_gateway_never_sees_the_code(self):
        """The gateway's process can't read the window file, and its
        source never touches the code."""
        src = open(os.path.join(U.BIN, "ollama1-gateway")).read()
        for needle in ('w["code"]', "window.json", "read_window", "check_pair_mac", "pair_proof", "Paths.window"):
            self.assertNotIn(needle, src)
        unit = open(os.path.join(U.KIT, "systemd", "ollama1-gateway.service")).read()
        self.assertNotIn("o1pair", unit)
        pub = json.load(open(os.path.join(U.PREFIX, "run/ollama1/pair-public.json"))) if \
            o1pair.open_window() else None
        self.assertEqual(set(pub), {"id", "expires_at"})

    def test_code_is_case_and_dash_insensitive(self):
        w = o1pair.open_window()
        code = w["code"].lower()
        st, _, _ = self.pair(U.Device(), code[:4] + "-" + code[4:8] + " " + code[8:])
        self.assertEqual(st, 200)

    def test_one_success_only(self):
        w = o1pair.open_window()
        st1, _, _ = self.pair(U.Device(), w["code"])
        st2, data, _ = self.pair(U.Device(), w["code"])
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 403)
        self.assertEqual(len(o1pair.load_devices()), 2)  # G["dev"] and the one new device

    def test_wrong_codes_close_window(self):
        w = o1pair.open_window()
        wrong = "ZZZZZZZZZZZZ" if w["code"] != "ZZZZZZZZZZZZ" else "YYYYYYYYYYYY"
        codes = []
        for _ in range(5):
            st, data, _ = self.pair(U.Device(), wrong)
            codes.append((st, json.loads(data)["code"]))
        self.assertEqual(codes[:4], [(401, "wrong_code")] * 4)
        self.assertEqual(codes[4], (403, "pair_closed"))
        st, data, _ = self.pair(U.Device(), w["code"])  # even the right code now
        self.assertEqual(st, 403)
        self.assertIsNone(o1pair.read_window())

    def test_nonce_reuse_refused(self):
        w = o1pair.open_window()
        wrong = "ZZZZZZZZZZZZ" if w["code"] != "ZZZZZZZZZZZZ" else "YYYYYYYYYYYY"
        dev = U.Device()
        nonce = U.b64u(os.urandom(16))
        st1, _, _ = self.pair(dev, wrong, nonce=nonce)
        st2, data, _ = self.pair(dev, w["code"], nonce=nonce)
        self.assertEqual(st1, 401)
        self.assertEqual((st2, json.loads(data)["code"]), (401, "replay"))

    def test_rate_limit(self):
        G["mod"].PAIR_MIN_INTERVAL = 2.0
        G["gw"].pair["last_try"] = 0.0
        w = o1pair.open_window()
        st1, _, _ = self.pair(U.Device(), "ZZZZZZZZZZZZ")
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

    def test_never_on_the_lan_listener(self):
        """LAN mode serves plain HTTP without Access: a sniffed MAC could be
        brute-forced offline, so pairing only works through the tunnel."""
        from http.server import ThreadingHTTPServer
        import ipaddress
        gw = G["gw"]
        gw.cfg["lan_mode"] = True
        old_net = gw.lan_net
        gw.lan_net = ipaddress.ip_network("127.0.0.0/8")
        port = U.free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), G["mod"].make_handler(gw, lan=True))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            w = o1pair.open_window()
            b = json.dumps(self.body(U.Device(), w["code"])).encode()
            st, data, _ = U.request(port, "POST", "/v1/pair", b, {}, host="192.168.86.10")
            self.assertEqual(st, 403)
            self.assertIn(b"only through the tunnel", data)
            self.assertIsNotNone(o1pair.read_window())  # nothing reached the root step
            # signed requests still work on the LAN listener in LAN mode
            h = G["dev"].headers("GET", "/v1/whoami")
            st, _, _ = U.request(port, "GET", "/v1/whoami", b"", h, host="192.168.86.10")
            self.assertEqual(st, 200)
        finally:
            srv.shutdown()
            gw.cfg["lan_mode"] = False
            gw.lan_net = old_net

    def test_root_step_checks_the_code(self):
        """A request dropped into the spool with a wrong code (as a
        compromised gateway might) is not added."""
        w = o1pair.open_window()
        self.stop.set()
        self.th.join()
        dev = U.Device()
        from o1auth import pair_mac
        ts, nonce, pub = int(time.time()), U.b64u(os.urandom(16)), U.b64u(dev.pub)
        o1pair.spool_request("00112233aabbccdd", w["id"], {
            "name": "x", "public_key": pub, "timestamp": ts, "nonce": nonce,
            "mac": U.b64u(pair_mac("WRONGCODE123", "x", pub, ts, nonce))})
        self.assertIsNone(o1pair.commit_spool(log=lambda *_: None))
        self.assertNotIn(dev.id, o1pair.load_devices())
        res = json.load(open(os.path.join(U.PREFIX, "run/ollama1/pair-result/00112233aabbccdd.json")))
        self.assertEqual(res["status"], 401)


class TestConnectionSafety(unittest.TestCase):
    """F1/F3/F5: errors close the connection (no request smuggling through
    cloudflared's pooled connections), nothing is read before the headers
    authenticate, and a failure mid-stream ends the stream cleanly."""

    def raw(self, head, body=b"", wait=3):
        s = socket.create_connection(("127.0.0.1", G["port"]))
        s.sendall(head + body)
        s.settimeout(wait)
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
        return data

    def inner(self):
        h = {"Host": "ollama1.flyconcordefly.com", "Cf-Access-Jwt-Assertion": jwt()}
        h.update(G["dev"].headers("GET", "/v1/whoami", b""))
        return ("GET /v1/whoami HTTP/1.1\r\n" + "".join("%s: %s\r\n" % kv for kv in h.items()) + "\r\n").encode()

    def test_no_smuggling_after_errors(self):
        inner = self.inner()
        heads = [
            "POST /api/chat HTTP/1.1\r\nHost: ollama1.flyconcordefly.com\r\nTransfer-Encoding: chunked\r\n"
            "Content-Length: %d\r\n\r\n" % len(inner),
            "POST /api/chat HTTP/1.1\r\nHost: ollama1.flyconcordefly.com\r\nContent-Length: %d\r\n\r\n" % (65 << 20),
            "POST /api/chat?%s HTTP/1.1\r\nHost: ollama1.flyconcordefly.com\r\nContent-Length: %d\r\n\r\n"
            % ("a" * 2100, len(inner)),
            "POST /api/chat HTTP/1.1\r\nHost: ollama1.flyconcordefly.com\r\nContent-Length: %d\r\n\r\n" % len(inner),
        ]
        for head in heads:
            data = self.raw(head.encode(), inner)
            self.assertEqual(data.count(b"HTTP/1.1 "), 1, head[:60])
            self.assertNotIn(b"device_id", data, head[:60])
            self.assertIn(b"Connection: close", data)

    def test_body_not_read_before_auth(self):
        """An unauthenticated request with a big declared body gets its
        answer without the gateway waiting for (or reading) the body."""
        head = ("POST /api/chat HTTP/1.1\r\nHost: ollama1.flyconcordefly.com\r\n"
                "Content-Length: %d\r\n\r\n" % (20 << 20)).encode()
        t0 = time.time()
        data = self.raw(head, b"", wait=5)
        self.assertLess(time.time() - t0, 4.5)   # didn't sit waiting for 20 MiB
        self.assertIn(b"403", data.split(b"\r\n", 1)[0])

    def test_unpaired_signed_request_not_read(self):
        dev = U.Device()
        h = {"Host": "ollama1.flyconcordefly.com", "Cf-Access-Jwt-Assertion": jwt(),
             "Content-Length": str(20 << 20)}
        h.update(dev.headers("POST", "/api/chat", b"x"))
        head = ("POST /api/chat HTTP/1.1\r\n" + "".join("%s: %s\r\n" % kv for kv in h.items()) + "\r\n").encode()
        t0 = time.time()
        data = self.raw(head, b"", wait=5)
        self.assertLess(time.time() - t0, 4.5)
        self.assertIn(b"403", data.split(b"\r\n", 1)[0])

    def test_inflight_cap(self):
        gw = G["gw"]
        held = []
        while gw.inflight.acquire(blocking=False):
            held.append(1)
        try:
            st, data, _ = call("GET", "/v1/whoami")
            self.assertEqual(st, 503)
        finally:
            for _ in held:
                gw.inflight.release()
        self.assertEqual(call("GET", "/v1/whoami")[0], 200)

    def test_exception_mid_stream_ends_stream(self):
        gw = G["gw"]
        orig = gw.warm_load

        def boom(name, n_ctx):
            raise ConnectionRefusedError(111, "Connection refused")  # Ollama restarting mid-queue
        gw.warm_load = boom
        try:
            st, data, r = call("POST", "/api/chat", {"model": "small:8b",
                                                     "messages": [{"role": "user", "content": "hi"}]})
        finally:
            gw.warm_load = orig
        self.assertEqual(st, 200)
        self.assertNotIn(b"HTTP/1.1 500", data)
        lines = U.ndjson(data)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["code"], "ollama")
        self.assertEqual(G["gw"].slot.busy, False)


class TestDeviceSwitch(unittest.TestCase):
    """No prompt cache is ever shared between devices: when the next job
    comes from another paired device, loaded models are unloaded first."""

    def setUp(self):
        self.other = U.Device(name="Pat's iPad")
        U.write_devices([G["dev"], self.other])
        G["stub"].loaded.clear()

    def tearDown(self):
        U.write_devices([G["dev"]])

    def chat(self, dev):
        return call("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                          "messages": [{"role": "user", "content": "x"}]}, dev=dev)

    def unloads(self, since):
        return [c for c in G["stub"].calls[since:] if c[0] == "POST" and (c[2] or {}).get("keep_alive") == 0]

    def test_same_device_keeps_the_model(self):
        self.chat(G["dev"])
        n = len(G["stub"].calls)
        st, data, _ = self.chat(G["dev"])
        self.assertEqual(st, 200)
        self.assertEqual(self.unloads(n), [])
        self.assertIn("prompt_eval_count", json.loads(data))  # kept for the benchmark

    def test_other_device_unloads_first(self):
        self.chat(G["dev"])
        self.assertIn("small:8b", G["stub"].loaded)
        n = len(G["stub"].calls)
        st, _, _ = self.chat(self.other)
        self.assertEqual(st, 200)
        unl = self.unloads(n)
        self.assertEqual([c[2]["model"] for c in unl], ["small:8b"])
        first_chat = next(i for i, c in enumerate(G["stub"].calls[n:]) if c[1] == "/api/chat")
        first_unload = G["stub"].calls[n:].index(unl[0])
        self.assertLess(first_unload, first_chat)

    def patched(self, fn):
        mod = G["mod"]
        real = mod.o1ollama.call

        def wrapper(base, method, path, obj=None, timeout=30):
            return fn(real, base, method, path, obj, timeout)
        mod.o1ollama.call = wrapper
        return real

    def test_switch_fails_closed_when_ps_fails(self):
        self.chat(G["dev"])
        gw, mod = G["gw"], G["mod"]

        def flaky(real, base, method, path, obj, timeout):
            if path == "/api/ps":
                raise ConnectionResetError("Ollama busy/restarting")
            return real(base, method, path, obj, timeout)
        real = self.patched(flaky)
        try:
            n = len(G["stub"].calls)
            st, data, _ = self.chat(self.other)
        finally:
            mod.o1ollama.call = real
        self.assertEqual(st, 503)
        self.assertEqual(json.loads(data)["code"], "busy")
        self.assertEqual(gw.last_device, G["dev"].id)        # unchanged
        self.assertEqual([c for c in G["stub"].calls[n:] if c[1] == "/api/chat"], [])
        st, _, _ = self.chat(self.other)                      # Ollama back: works
        self.assertEqual(st, 200)
        self.assertEqual(gw.last_device, self.other.id)

    def test_switch_fails_closed_when_unload_does_not_take(self):
        self.chat(G["dev"])
        gw, mod = G["gw"], G["mod"]

        def stuck(real, base, method, path, obj, timeout):
            if isinstance(obj, dict) and obj.get("keep_alive") == 0:
                return 500, {"error": "no"}                   # the unload fails
            return real(base, method, path, obj, timeout)
        real = self.patched(stuck)
        try:
            st, data, _ = self.chat(self.other)
        finally:
            mod.o1ollama.call = real
        self.assertEqual(st, 503)
        self.assertIn("small:8b", G["stub"].loaded)
        self.assertEqual(gw.last_device, G["dev"].id)

    def test_switch_streaming_refusal_is_in_band(self):
        self.chat(G["dev"])
        mod = G["mod"]

        def flaky(real, base, method, path, obj, timeout):
            if path == "/api/ps":
                raise ConnectionResetError("x")
            return real(base, method, path, obj, timeout)
        real = self.patched(flaky)
        try:
            st, data, _ = call("POST", "/api/chat", {"model": "small:8b",
                                                     "messages": [{"role": "user", "content": "x"}]},
                               dev=self.other)
        finally:
            mod.o1ollama.call = real
        self.assertEqual(st, 200)
        self.assertEqual(U.ndjson(data)[-1]["code"], "busy")


class TestAllowListParsing(unittest.TestCase):
    def parse(self, text):
        from o1common import parse_allow_list
        path = os.path.join(U.PREFIX, "etc/ollama1/parse-test.allow")
        with open(path, "w") as f:
            f.write(text)
        try:
            return parse_allow_list(path)
        finally:
            os.unlink(path)

    def test_plain_and_ram(self):
        entries, errors = self.parse("# models\nqwen3:14b\ngpt-oss:120b   ram   # the big one\n\n")
        self.assertEqual(errors, [])
        self.assertEqual(entries, [{"name": "qwen3:14b", "ram": False}, {"name": "gpt-oss:120b", "ram": True}])

    def test_strict(self):
        entries, errors = self.parse("a:1b fast\nb:1b RAM\nc:1b ram ram\n../x ram\nd:1b\nd:1b ram\ne:1b\n")
        self.assertEqual([e["name"] for e in entries], ["d:1b", "e:1b"])
        self.assertEqual([e["line"] for e in errors], [1, 2, 3, 4, 6])
        self.assertIn("unknown flag 'fast'", errors[0]["reason"])
        self.assertEqual(errors[5 - 1]["reason"], "listed twice")

    def test_names_still_read(self):
        from o1common import read_allow_list
        path = os.path.join(U.PREFIX, "etc/ollama1/parse-test.allow")
        with open(path, "w") as f:
            f.write("x:1b\ny:2b ram\nz:3b turbo\n")
        try:
            self.assertEqual(read_allow_list(path), ["x:1b", "y:2b"])
        finally:
            os.unlink(path)


class TestRamModels(unittest.TestCase):
    """Models marked 'ram' may use system memory; every other model stays
    GPU only."""
    GIB = 1 << 30

    def setUp(self):
        from o1common import Paths as P
        self.allow = P.allow
        with open(self.allow, "w") as f:
            f.write("moe:120b   ram\nsmall:8b\nsneaky:14b\n")
        self.gw = G["gw"]
        self.mem = {"MemTotal": 62 * self.GIB, "MemAvailable": 58 * self.GIB,
                    "SwapTotal": 8 * self.GIB, "SwapFree": 8 * self.GIB}
        self.swap_after = None
        self.calls = 0

        def fake_meminfo():
            self.calls += 1
            m = dict(self.mem)
            if self.swap_after is not None and "moe:120b" in G["stub"].loaded:
                m["SwapFree"] = self.swap_after   # swap used once the model is in memory
            return m
        self.gw.meminfo = fake_meminfo
        self.gw.stats.loaded = []
        # most of these tests run with repacking off, where VRAM counts too;
        # the repack-on rules have their own tests below
        self.gw.cfg["ollama_no_repack"] = True
        G["stub"].loaded.clear()

    def tearDown(self):
        import o1stats
        self.gw.meminfo = o1stats.meminfo
        self.gw.cfg["ollama_no_repack"] = False
        os.unlink(self.allow)
        G["stub"].loaded.clear()

    def chat(self, model, **extra):
        obj = {"model": model, "messages": [{"role": "user", "content": "x"}], "stream": False}
        obj.update(extra)
        return call("POST", "/api/chat", obj)

    def test_ram_model_may_use_system_memory(self):
        st, data, _ = self.chat("moe:120b")
        self.assertEqual(st, 200, data)
        st, data, _ = call("GET", "/api/ps")
        ps = {m["name"]: m for m in json.loads(data)["models"]}
        self.assertEqual((ps["moe:120b"]["placement"], ps["moe:120b"]["gpu_pct"]), ("gpu+ram", 25))
        st, data, _ = call("GET", "/api/tags")
        tags = {m["name"]: m for m in json.loads(data)["models"]}
        self.assertEqual(tags["moe:120b"]["placement"], "gpu+ram")
        self.assertEqual(tags["moe:120b"]["gpu_pct"], 25)
        self.assertEqual(tags["small:8b"]["placement"], "gpu")
        self.assertIsNone(tags["small:8b"]["gpu_pct"])

    def test_plain_model_still_refused_on_spill(self):
        st, data, _ = self.chat("sneaky:14b")   # on the allow-list, but without 'ram'
        self.assertEqual(st, 507)
        self.assertEqual(json.loads(data)["code"], "gpu_spill")

    def test_ram_model_that_does_not_fit_even_with_ram(self):
        self.mem["MemAvailable"] = 30 * self.GIB
        st, data, _ = self.chat("moe:120b")
        self.assertEqual(st, 507)
        self.assertEqual(json.loads(data)["code"], "gpu_fit")
        self.assertNotIn("moe:120b", G["stub"].loaded)

    def test_margin_is_8_gib_or_12_percent(self):
        vram = 16 * self.GIB - (768 << 20)
        old = self.gw.cfg.get("ram_margin_gib")
        self.gw.cfg["ram_margin_gib"] = 0
        try:
            self.assertEqual(self.gw.budget(True), vram + 58 * self.GIB - 8 * self.GIB)   # 12% of 62 < 8
            self.mem["MemTotal"] = 128 * self.GIB
            self.assertEqual(self.gw.budget(True), vram + 58 * self.GIB - int(128 * self.GIB * 0.12))
        finally:
            self.gw.cfg["ram_margin_gib"] = old
        self.assertEqual(self.gw.budget(False), vram)

    def test_ollama_memory_max_caps_the_budget(self):
        vram = 16 * self.GIB - (768 << 20)
        self.gw.cfg["ollama_memory_max_bytes"] = 40 * self.GIB
        try:
            self.assertEqual(self.gw.budget(True), vram + 39 * self.GIB)
        finally:
            self.gw.cfg["ollama_memory_max_bytes"] = 0

    def test_resident_size_counts_compute_buffers(self):
        import o1ollama
        from stub_ollama import DEFAULT_MODELS
        m = DEFAULT_MODELS["moe:120b"]
        gpu, _ = o1ollama.fit_estimate(m["size"], m["info"], 4096)
        ram, _ = o1ollama.fit_estimate(m["size"], m["info"], 4096, ram=True)
        self.assertEqual(ram - gpu, (5 << 29) - (256 << 20))

    def test_repack_on_counts_the_whole_model_in_ram(self):
        """Repacking on (the default): the model must fit in system memory
        alone. moe:120b (56 GiB) doesn't, although VRAM plus RAM would."""
        self.gw.cfg["ollama_no_repack"] = False
        st, data, _ = self.chat("moe:120b")
        self.assertEqual(st, 507)
        body = json.loads(data)
        self.assertEqual(body["code"], "gpu_fit")
        self.assertIn("whole model", body["error"])
        self.assertNotIn("moe:120b", G["stub"].loaded)
        vram = 16 * self.GIB - (768 << 20)
        self.assertEqual(self.gw.budget(True, host_only=True) + vram, self.gw.budget(True))

    def test_repack_on_reads_ram_models_without_mmap(self):
        self.gw.cfg["ollama_no_repack"] = False
        with open(self.allow, "w") as f:
            f.write("small:8b ram\n")
        n = len(G["stub"].calls)
        self.assertEqual(self.chat("small:8b")[0], 200)
        sent = [c[2] for c in G["stub"].calls[n:] if c[1] in ("/api/generate", "/api/chat")]
        self.assertTrue(sent)
        for body in sent:
            self.assertIs(body["options"]["use_mmap"], False, body)     # the warm load and the chat

    def test_repack_off_keeps_mmap(self):
        n = len(G["stub"].calls)
        self.assertEqual(self.chat("moe:120b")[0], 200)
        sent = [c[2] for c in G["stub"].calls[n:] if c[1] in ("/api/generate", "/api/chat")]
        self.assertTrue(sent)
        for body in sent:
            self.assertNotIn("use_mmap", body["options"], body)

    def test_mmap_off_for_ram_models_only(self):
        self.gw.cfg["ollama_no_repack"] = False
        with open(self.allow, "w") as f:
            f.write("small:8b ram\n")
        with open(self.allow, "w") as f:
            f.write("moe:120b ram\nsmall:8b\n")
        n = len(G["stub"].calls)
        self.assertEqual(self.chat("small:8b", options={"use_mmap": False})[0], 200)
        for c in G["stub"].calls[n:]:
            if c[1] in ("/api/generate", "/api/chat") and c[2].get("model") == "small:8b":
                self.assertNotIn("use_mmap", c[2].get("options", {}))

    def test_oom_during_load_is_a_clean_error(self):
        kills = {"n": 3}
        self.gw.oom_kills = lambda: kills["n"]
        mod = G["mod"]
        real = mod.o1ollama.call

        def dying(base, method, path, obj=None, timeout=30):
            if path == "/api/generate" and (obj or {}).get("prompt") == "":
                kills["n"] += 1                                   # the memory limit struck
                raise ConnectionResetError("runner killed")
            return real(base, method, path, obj, timeout)
        mod.o1ollama.call = dying
        try:
            st, data, _ = self.chat("moe:120b")
            self.assertEqual(st, 507)
            self.assertEqual(json.loads(data)["code"], "ram_oom")
            st, data, _ = call("POST", "/api/chat", {"model": "moe:120b", "stream": True,
                                                     "messages": [{"role": "user", "content": "x"}]})
            self.assertEqual(st, 200)
            self.assertEqual(U.ndjson(data)[-1]["code"], "ram_oom")
        finally:
            mod.o1ollama.call = real
            import o1stats
            self.gw.oom_kills = o1stats.ollama_oom_kills

    def test_ollama_dying_without_oom_is_still_clean(self):
        self.gw.oom_kills = lambda: 0
        mod = G["mod"]
        real = mod.o1ollama.call

        def dying(base, method, path, obj=None, timeout=30):
            if path == "/api/generate" and (obj or {}).get("prompt") == "":
                raise ConnectionResetError("gone")
            return real(base, method, path, obj, timeout)
        mod.o1ollama.call = dying
        try:
            st, data, _ = self.chat("moe:120b")
        finally:
            mod.o1ollama.call = real
            import o1stats
            self.gw.oom_kills = o1stats.ollama_oom_kills
        self.assertEqual(st, 502)
        self.assertEqual(json.loads(data)["code"], "ollama")

    def test_swap_refusal(self):
        self.swap_after = 7 * self.GIB          # loading pushed 1 GiB into swap
        st, data, _ = self.chat("moe:120b")
        self.assertEqual(st, 507)
        self.assertEqual(json.loads(data)["code"], "ram_pressure")
        self.assertNotIn("moe:120b", G["stub"].loaded)

    def test_context_steps_down_to_fit(self):
        import o1ollama
        from stub_ollama import DEFAULT_MODELS
        m = DEFAULT_MODELS["moe:120b"]
        need8, _ = o1ollama.fit_estimate(m["size"], m["info"], 8192, ram=True)
        need4, _ = o1ollama.fit_estimate(m["size"], m["info"], 4096, ram=True)
        vram = 16 * self.GIB - (768 << 20)
        self.mem["MemAvailable"] = (need8 + need4) // 2 - vram + 8 * self.GIB   # 8192 doesn't fit, 4096 does
        n = len(G["stub"].calls)
        st, data, _ = self.chat("moe:120b")
        self.assertEqual(st, 200, data)
        sent = [c[2] for c in G["stub"].calls[n:] if c[1] == "/api/chat"][-1]
        self.assertEqual(sent["options"]["num_ctx"], 4096)
        st, data, _ = self.chat("moe:120b", options={"num_ctx": 8192})   # asked for: no step-down
        self.assertEqual(st, 507)

    def test_a_ram_model_loads_alone(self):
        self.chat("small:8b")
        self.assertIn("small:8b", G["stub"].loaded)
        n = len(G["stub"].calls)
        st, _, _ = self.chat("moe:120b")
        self.assertEqual(st, 200)
        unl = [c[2]["model"] for c in G["stub"].calls[n:] if (c[2] or {}).get("keep_alive") == 0]
        self.assertEqual(unl, ["small:8b"])
        n = len(G["stub"].calls)
        st, _, _ = self.chat("small:8b")        # and the reverse
        self.assertEqual(st, 200)
        unl = [c[2]["model"] for c in G["stub"].calls[n:] if (c[2] or {}).get("keep_alive") == 0]
        self.assertEqual(unl, ["moe:120b"])

    def test_gpu_only_models_may_share(self):
        self.chat("small:8b")
        n = len(G["stub"].calls)
        st, _, _ = call("POST", "/api/embed", {"model": "embed:small", "input": "x"})
        self.assertEqual(st, 200)
        self.assertEqual([c for c in G["stub"].calls[n:] if (c[2] or {}).get("keep_alive") == 0], [])


class TestAccessNone(unittest.TestCase):
    def test_ssh_tunnel_mode(self):
        gw, mod = G["gw"], G["mod"]
        gw.cfg["access"] = "none"
        try:
            gw.check_access({"Host": "127.0.0.1:8431"}, False, "127.0.0.1")   # no JWT needed
            st, _, _ = call("GET", "/v1/whoami", jwt_token=None, headers={"Host": "127.0.0.1:8431"})
            self.assertEqual(st, 200)
            st, _, _ = call("GET", "/v1/whoami", jwt_token=None, dev=False,
                            headers={"Host": "127.0.0.1:8431"})                  # signatures still needed
            self.assertEqual(st, 401)
            with self.assertRaises(mod.GatewayError):
                gw.check_access({}, True, "192.168.86.20")                       # LAN mode still off
        finally:
            gw.cfg["access"] = "cloudflare"
        with self.assertRaises(mod.GatewayError):
            gw.check_access({"Host": "127.0.0.1:8431"}, False, "127.0.0.1")


class TestAccessNoneBrowserGuards(unittest.TestCase):
    """In SSH-tunnel mode a web page in the user's browser can reach the
    tunnel's local end. It must not be able to burn pairing attempts or
    rebind DNS onto the port (from probe_none_pair.py)."""

    def setUp(self):
        G["gw"].cfg["access"] = "none"
        G["mod"].PAIR_MIN_INTERVAL = 0.0
        G["gw"].pair["last_try"] = 0.0
        self.w = o1pair.open_window()
        self.stop = threading.Event()

        def committer():  # the root step, answering quickly
            while not self.stop.is_set():
                o1pair.commit_spool(log=lambda *_: None)
                time.sleep(0.05)
        self.th = threading.Thread(target=committer, daemon=True)
        self.th.start()
        self.body = json.dumps({"name": "x", "public_key": U.b64u(os.urandom(32)), "timestamp": int(time.time()),
                                "nonce": "A" * 22, "mac": U.b64u(os.urandom(32))}).encode()

    def tearDown(self):
        self.stop.set()
        self.th.join()
        G["gw"].cfg["access"] = "cloudflare"
        o1pair.close_window()

    def results(self):
        return len(os.listdir(os.path.join(U.PREFIX, "run/ollama1/pair-result")))

    def pair(self, headers, host):
        n = self.results()
        st, data, _ = U.request(G["port"], "POST", "/v1/pair", self.body, headers, host=host)
        return st, self.results() - n   # 1 if the request reached the root pairing step

    def test_the_probe_is_refused(self):
        st, spooled = self.pair({"Content-Type": "text/plain", "Origin": "https://evil.example"},
                                "attacker.example:8431")
        self.assertEqual((st, spooled), (403, 0))

    def test_each_guard_on_its_own(self):
        ok = {"Content-Type": "application/json"}
        self.assertEqual(self.pair(ok, "attacker.example:8431"), (403, 0))              # DNS rebinding
        self.assertEqual(self.pair(dict(ok, Origin="null"), "127.0.0.1:8431"), (403, 0))  # any Origin
        self.assertEqual(self.pair({"Content-Type": "text/plain"}, "localhost:8431"), (403, 0))
        self.assertEqual(self.pair({}, "localhost"), (403, 0))
        for host in ("127.0.0.1", "127.0.0.1:8431", "localhost", "LOCALHOST:18431"):
            G["gw"].pair["last_try"] = 0.0
            o1pair.open_window()                     # a fresh window each time (wrong codes add up)
            self.body = json.dumps(dict(json.loads(self.body), nonce=U.b64u(os.urandom(16)))).encode()
            st, reached = self.pair(ok, host)
            self.assertEqual((st, reached), (401, 1), host)   # reached the pairing step: wrong code

    def test_typos_fail_closed(self):
        gw, mod = G["gw"], G["mod"]
        for v in ("None", "NONE", "off", "", "none ", None, False, 0):
            gw.cfg["access"] = v
            with self.assertRaises(mod.GatewayError, msg=repr(v)):
                gw.check_access({"Host": "127.0.0.1:8431"}, False, "127.0.0.1", "GET")

    def test_refuses_to_start_next_to_a_tunnel(self):
        cfg = dict(G["cfg"], access="none", tunnel_id="t-1", gateway_port=U.free_port())
        with self.assertRaises(SystemExit):
            G["mod"].serve(cfg)


class TestMakeRoomSpilled(unittest.TestCase):
    """A model partly in system memory is unloaded before a GPU-only job,
    even after its 'ram' flag was removed (from probe_ram.py)."""

    def setUp(self):
        from o1common import Paths as P
        self.allow = P.allow
        mem = {"MemTotal": 62 << 30, "MemAvailable": 58 << 30, "SwapTotal": 8 << 30, "SwapFree": 8 << 30}
        G["gw"].meminfo = lambda: dict(mem)
        G["gw"].stats.loaded = []
        G["gw"].cfg["ollama_no_repack"] = True
        G["stub"].loaded.clear()

    def tearDown(self):
        import o1stats
        G["gw"].meminfo = o1stats.meminfo
        G["gw"].cfg["ollama_no_repack"] = False
        if os.path.exists(self.allow):
            os.unlink(self.allow)
        G["stub"].loaded.clear()

    def chat(self, m):
        return call("POST", "/api/chat", {"model": m, "messages": [{"role": "user", "content": "x"}],
                                          "stream": False})

    def test_flag_removed_while_loaded(self):
        with open(self.allow, "w") as f:
            f.write("moe:120b ram\nsmall:8b\n")
        self.assertEqual(self.chat("moe:120b")[0], 200)
        with open(self.allow, "w") as f:
            f.write("moe:120b\nsmall:8b\n")            # the flag is dropped
        t = time.time() + 5
        os.utime(self.allow, (t, t))
        n = len(G["stub"].calls)
        self.assertEqual(self.chat("small:8b")[0], 200)
        unl = [c[2]["model"] for c in G["stub"].calls[n:] if (c[2] or {}).get("keep_alive") == 0]
        self.assertEqual(unl, ["moe:120b"])
        st, data, _ = call("GET", "/api/ps")
        self.assertEqual([m["name"] for m in json.loads(data)["models"]], ["small:8b"])


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
