"""The llama.cpp bridge (6b531): Ollama's dialect on a UNIX socket, the
chosen model on a (stub) llama-server with the measured flags, everything
else passed to a stub Ollama untouched; the gateway through the bridge end
to end. Nothing leaves 127.0.0.1; no prompt reaches a log."""
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout

import o1test_util as U  # sets OLLAMA1_PREFIX first

import o1llamacpp  # noqa: E402
import o1ollama  # noqa: E402
from o1common import DEFAULTS, Paths  # noqa: E402
from stub_ollama import Stub  # noqa: E402

GIB = 1 << 30
SHA = "ab" * 32
G = {}


def install_fake_model(name, size=4096):
    """An Ollama manifest and weights blob for `name` under the test prefix."""
    p = o1llamacpp.manifest_path(name, Paths.models)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump({"schemaVersion": 2, "layers": [
            {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + SHA, "size": size},
            {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:" + "cd" * 32, "size": 10}]}, f)
    os.makedirs(os.path.join(Paths.models, "blobs"), exist_ok=True)
    blob = os.path.join(Paths.models, "blobs", "sha256-" + SHA)
    with open(blob, "wb") as f:
        f.write(b"GGUF" + b"\0" * (size - 4))
    return blob


def bridge_cfg(**over):
    cfg = {"enabled": True, "bin": G["stub_bin"], "port": G["child_port"],
           "models": {"small:8b": {"ctx": 32768, "spec": "ngram-mod", "spec_n_max": 16, "kv": "q8_0"}}}
    cfg.update(over)
    return cfg


def write_bridge_cfg(cfg):
    with open(G["cfg_path"], "w") as f:
        json.dump(cfg, f)


def setUpModule():
    G["tmp"] = tempfile.mkdtemp(prefix="o1llama-")
    G["stub"] = Stub(U.free_port())
    G["child_port"] = U.free_port()
    G["argv_file"] = os.path.join(G["tmp"], "argv.json")
    G["body_file"] = os.path.join(G["tmp"], "body.json")
    o1llamacpp.Child.EXTRA_ENV = {"STUB_LLAMA_ARGV": G["argv_file"], "STUB_LLAMA_BODY": G["body_file"],
                                  "STUB_LLAMA_LOAD_S": "0.3"}
    stub_bin = os.path.join(G["tmp"], "llama-server")
    shutil.copy(os.path.join(U.HERE, "stub_llamaserver.py"), stub_bin)
    os.chmod(stub_bin, stat.S_IRWXU)
    G["stub_bin"] = stub_bin
    G["blob"] = install_fake_model("small:8b")
    G["cfg_path"] = os.path.join(G["tmp"], "llamacpp.json")
    write_bridge_cfg(bridge_cfg())
    cfg, problems = o1llamacpp.load_bridge_config(G["cfg_path"])
    assert not problems, problems
    G["vram"] = {"value": 4096}
    G["logs"] = []
    o1llamacpp.log = lambda event, **f: G["logs"].append((event, f))
    G["bridge"] = o1llamacpp.Bridge(cfg, "http://127.0.0.1:%d" % G["stub"].port, Paths.models,
                                    vram_fn=lambda pid: G["vram"]["value"])
    G["sock"] = os.path.join(G["tmp"], "bridge.sock")
    G["stop"] = threading.Event()
    G["srv"] = o1llamacpp.serve(G["bridge"], G["sock"], G["stop"])
    G["base"] = "unix:" + G["sock"]


def tearDownModule():
    G["stop"].set()
    time.sleep(0.3)
    G["stub"].close()
    shutil.rmtree(G["tmp"], True)


def call(method, path, obj=None, timeout=30):
    conn = o1ollama.connect(G["base"], timeout=timeout)
    body = None if obj is None else json.dumps(obj).encode()
    conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data, r


def child_argv():
    with open(G["argv_file"]) as f:
        return json.load(f)


def child_body():
    with open(G["body_file"]) as f:
        return json.load(f)


class TestConfig(unittest.TestCase):
    def test_defaults_and_refusals(self):
        ok, why = o1llamacpp.check_model_spec("gpt-oss:20b", {})
        self.assertEqual((ok["ctx"], ok["kv"], ok["spec"], ok["spec_n_max"], ok["draft"], ok["args"]),
                         (32768, "f16", "none", 16, "", []))
        for spec, needle in (({"ctx": 100}, "ctx"), ({"kv": "q2"}, "kv"), ({"spec": "eagle"}, "spec"),
                             ({"spec_n_max": 0}, "spec_n_max"), ({"draft": "rel.gguf"}, "draft"),
                             ({"draft": "/x/../y.gguf"}, "draft"), ({"args": "-fa"}, "args"),
                             ({"args": ["--port 1"]}, "plain flag"), ({"args": ["--host"]}, "set by the bridge"),
                             ({"args": ["-m=/x"]}, "set by the bridge"), ({"args": ["--verbose-prompt"]}, "set by the bridge")):
            ok, why = o1llamacpp.check_model_spec("m:1", spec)
            self.assertIsNone(ok, spec)
            self.assertIn(needle, why, spec)
        self.assertIsNone(o1llamacpp.check_model_spec("../x", {})[0])
        self.assertIsNone(o1llamacpp.check_model_spec("a b", {})[0])

    def test_bad_model_is_dropped_not_fatal(self):
        p = os.path.join(G["tmp"], "bad.json")
        with open(p, "w") as f:
            json.dump({"enabled": True, "port": 80, "models": {"good:1": {}, "bad:1": {"kv": "x"}, "a b": {}}}, f)
        cfg, problems = o1llamacpp.load_bridge_config(p)
        self.assertEqual(sorted(cfg["models"]), ["good:1"])
        self.assertEqual(cfg["port"], o1llamacpp.DEFAULT_PORT)
        self.assertEqual(len(problems), 3, problems)      # the port, the kv, the name
        cfg, problems = o1llamacpp.load_bridge_config(os.path.join(G["tmp"], "missing.json"))
        self.assertFalse(cfg["enabled"])
        self.assertTrue(problems)

    def test_command_line_carries_the_measured_flags(self):
        spec = o1llamacpp.check_model_spec("m:1", {"ctx": 16384, "kv": "q8_0", "spec": "ngram-mod", "spec_n_max": 24,
                                                   "draft": "/srv/d.gguf", "args": ["--threads", "8"]})[0]
        cmd = o1llamacpp.server_command("/opt/llamacpp/current/llama-server", 8470, "/srv/w.gguf", "/srv/p.gguf", spec)
        joined = " ".join(cmd)
        for part in ("-m /srv/w.gguf", "--host 127.0.0.1", "--port 8470", "-c 16384", "-ngl 999", "-fa on", "--jinja",
                     "--no-webui", "--mmproj /srv/p.gguf", "-ctk q8_0 -ctv q8_0", "-md /srv/d.gguf",
                     "--spec-type ngram-mod --spec-draft-n-max 24", "--threads 8"):
            self.assertIn(part, joined)
        self.assertNotIn("--draft-max", joined)          # removed upstream (BEAT_FABLE)
        self.assertNotIn("--verbose", joined)             # prompts never logged
        plain = o1llamacpp.server_command("/b", 1, "/w", "", o1llamacpp.check_model_spec("m:1", {})[0])
        self.assertNotIn("--spec-type", plain)
        self.assertNotIn("-ctk", plain)

    def test_model_files_from_the_manifest(self):
        gguf, mmproj, size = o1llamacpp.model_files("small:8b", Paths.models)
        self.assertEqual((gguf, mmproj, size), (G["blob"], "", 4096))
        with self.assertRaises(o1llamacpp.BridgeError) as cm:
            o1llamacpp.model_files("nothere:1", Paths.models)
        self.assertEqual(cm.exception.status, 404)
        self.assertIsNone(o1llamacpp.manifest_path("../../etc:x", Paths.models))

    def test_check_reports(self):
        with open(os.path.join(G["tmp"], "gw.json"), "w") as f:
            json.dump({"llamacpp_bridge": "unix:" + o1llamacpp.SOCKET}, f)
        lines, ok = o1llamacpp.check(G["cfg_path"], os.path.join(G["tmp"], "gw.json"))
        text = "\n".join(lines)
        self.assertIn("small:8b: 0.0 GiB " + G["blob"], text)
        self.assertIn("--spec-type ngram-mod --spec-draft-n-max 16", text)
        self.assertIn("gateway upstream: unix:" + o1llamacpp.SOCKET, text)
        # nvidia-smi is not on the test machine, or it is: either way the line says so
        self.assertIn("nvidia-smi:", text)
        bad = os.path.join(G["tmp"], "bad2.json")
        with open(bad, "w") as f:
            json.dump({"enabled": True, "bin": "/nonexistent/llama-server", "models": {"ghost:1": {}}}, f)
        lines, ok = o1llamacpp.check(bad, os.path.join(G["tmp"], "gw.json"))
        self.assertFalse(ok)
        self.assertTrue(any("ghost:1" in l and "not installed" in l for l in lines), lines)
        self.assertTrue(any("llama-server not found" in l for l in lines), lines)


class TestBridge(unittest.TestCase):
    def setUp(self):
        G["stub"].loaded.clear()
        G["stub"].calls.clear()
        G["vram"]["value"] = 4096
        G["logs"].clear()

    def tearDown(self):
        G["bridge"].child.stop("test")

    def test_unix_connect_and_passthrough(self):
        st, data, _ = call("GET", "/api/tags")
        self.assertEqual(st, 200)
        self.assertIn("huge:70b", [m["name"] for m in json.loads(data)["models"]])
        st, data, _ = call("POST", "/api/show", {"model": "huge:70b"})
        self.assertEqual(st, 200)
        self.assertIn("model_info", json.loads(data))
        st, data, _ = call("GET", "/api/version")
        self.assertEqual(json.loads(data)["version"], "0.34.4")
        st, data, _ = call("GET", "/api/nope")
        self.assertEqual(st, 404)

    def test_ollama_model_streams_through_untouched(self):
        st, data, r = call("POST", "/api/chat", {"model": "huge:70b", "messages": [{"role": "user", "content": "hi MARK1"}]})
        self.assertEqual(st, 200)
        self.assertIn("x-ndjson", r.getheader("Content-Type"))
        lines = U.ndjson(data)
        self.assertIn("ANSWER-", lines[0]["message"]["content"])
        self.assertTrue(lines[-1]["done"])
        self.assertEqual(G["stub"].calls[-1][1], "/api/chat")
        self.assertFalse(G["bridge"].child.running())

    def test_load_starts_llama_server_with_the_flags_and_unloads_ollama_first(self):
        # Ollama holds a model; the bridged model's load must clear the card first
        call("POST", "/api/generate", {"model": "huge:70b", "prompt": "", "stream": False})
        self.assertIn("huge:70b", G["stub"].loaded)
        st, data, _ = call("POST", "/api/generate", {"model": "small:8b", "prompt": ""})
        self.assertEqual(st, 200, data)
        self.assertEqual(json.loads(data)["done_reason"], "load")
        self.assertTrue(G["bridge"].child.running())
        argv = child_argv()
        joined = " ".join(argv)
        self.assertIn("-m " + G["blob"], joined)
        self.assertIn("--port %d" % G["child_port"], joined)
        self.assertIn("-c 32768", joined)
        self.assertIn("-ctk q8_0 -ctv q8_0", joined)
        self.assertIn("--spec-type ngram-mod --spec-draft-n-max 16", joined)
        self.assertNotIn("huge:70b", G["stub"].loaded)
        unloads = [c for c in G["stub"].calls if c[0] == "POST" and c[2].get("keep_alive") == 0]
        self.assertTrue(unloads)
        self.assertIn("unload-ollama", [e for e, _f in G["logs"]])
        # the second load is a no-op: same child
        pid = G["bridge"].child.proc.pid
        call("POST", "/api/generate", {"model": "small:8b", "prompt": ""})
        self.assertEqual(G["bridge"].child.proc.pid, pid)

    def test_ps_merges_the_child(self):
        call("POST", "/api/generate", {"model": "small:8b", "prompt": ""})
        st, data, _ = call("GET", "/api/ps")
        ms = {m["name"]: m for m in json.loads(data)["models"]}
        self.assertIn("small:8b", ms)
        self.assertEqual((ms["small:8b"]["size"], ms["small:8b"]["size_vram"], ms["small:8b"]["context_length"]),
                         (4096, 4096, 32768))
        # VRAM unknown: 0 in VRAM, so the gateway's 100% check refuses it (fails closed)
        G["vram"]["value"] = None
        st, data, _ = call("GET", "/api/ps")
        self.assertEqual({m["name"]: m["size_vram"] for m in json.loads(data)["models"]}["small:8b"], 0)

    def test_chat_is_translated_both_ways(self):
        st, data, r = call("POST", "/api/chat", {"model": "small:8b", "stream": True, "format": "json",
                                                 "messages": [{"role": "user", "content": "say MARK2"}],
                                                 "options": {"temperature": 0.3, "top_p": 0.9, "num_predict": 77,
                                                             "num_ctx": 8192, "stop": ["END"], "bogus": 1}})
        self.assertEqual(st, 200, data)
        self.assertIn("x-ndjson", r.getheader("Content-Type"))
        lines = U.ndjson(data)
        self.assertEqual(lines[0]["message"]["content"], "ANSWER-say MARK2")
        self.assertEqual("".join(l["message"]["content"] for l in lines), "ANSWER-say MARK2 more words")
        last = lines[-1]
        self.assertTrue(last["done"])
        self.assertEqual((last["done_reason"], last["eval_count"], last["prompt_eval_count"]), ("stop", 3, 9))
        self.assertEqual((last["eval_duration"], last["prompt_eval_duration"]), (60_000_000, 45_000_000))
        sent = child_body()
        self.assertEqual(sent["path"], "/v1/chat/completions")
        b = sent["body"]
        self.assertEqual((b["model"], b["temperature"], b["top_p"], b["max_tokens"], b["stop"], b["stream"]),
                         ("small:8b", 0.3, 0.9, 77, ["END"], True))
        self.assertEqual(b["response_format"], {"type": "json_object"})
        self.assertEqual(b["stream_options"], {"include_usage": True})
        self.assertNotIn("num_ctx", b)
        self.assertNotIn("bogus", b)
        # the OpenAI-style sampling knobs' names, as llama-server reads them
        self.assertEqual(o1llamacpp.oai_from_chat({"options": {"repeat_penalty": 1.1, "top_k": 40, "seed": 7}}, "m")
                         .items() >= {"repeat_penalty": 1.1, "top_k": 40, "seed": 7}.items(), True)

    def test_chat_whole_answer(self):
        st, data, _ = call("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                                 "messages": [{"role": "user", "content": "whole MARK3"}]})
        self.assertEqual(st, 200, data)
        obj = json.loads(data)
        self.assertEqual(obj["message"]["content"], "ANSWER-whole MARK3 more words")
        self.assertEqual((obj["done"], obj["eval_count"], obj["prompt_eval_count"]), (True, 3, 9))

    def test_generate_is_translated(self):
        st, data, _ = call("POST", "/api/generate", {"model": "small:8b", "prompt": "finish MARK4", "system": "be brief"})
        self.assertEqual(st, 200, data)
        lines = U.ndjson(data)
        self.assertEqual("".join(l["response"] for l in lines), "ANSWER-finish MARK4 more words")
        self.assertTrue(lines[-1]["done"])
        sent = child_body()
        self.assertEqual(sent["path"], "/v1/completions")
        self.assertEqual(sent["body"]["prompt"], "be brief\n\nfinish MARK4")
        st, data, _ = call("POST", "/api/generate", {"model": "small:8b", "prompt": "p", "stream": False})
        self.assertTrue(json.loads(data)["response"].startswith("ANSWER-"))

    def test_unload_stops_the_child(self):
        call("POST", "/api/generate", {"model": "small:8b", "prompt": ""})
        self.assertTrue(G["bridge"].child.running())
        st, data, _ = call("POST", "/api/generate", {"model": "small:8b", "keep_alive": 0})
        self.assertEqual(json.loads(data)["done_reason"], "unload")
        self.assertFalse(G["bridge"].child.running())
        st, data, _ = call("GET", "/api/ps")
        self.assertNotIn("small:8b", [m["name"] for m in json.loads(data)["models"]])
        # Ollama's own keep_alive 0 still goes to Ollama
        call("POST", "/api/generate", {"model": "huge:70b", "keep_alive": 0})
        self.assertEqual(G["stub"].calls[-1][2].get("keep_alive"), 0)

    def test_an_ollama_model_stops_the_child_first(self):
        call("POST", "/api/generate", {"model": "small:8b", "prompt": ""})
        self.assertTrue(G["bridge"].child.running())
        st, data, _ = call("POST", "/api/chat", {"model": "huge:70b", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 200)
        self.assertFalse(G["bridge"].child.running())
        self.assertIn(("stop", {"model": "small:8b", "why": "ollama_model_asked"}), G["logs"])

    def test_embeddings_of_a_bridged_model_are_refused(self):
        st, data, _ = call("POST", "/api/embed", {"model": "small:8b", "input": ["x"]})
        self.assertEqual(st, 400)
        self.assertFalse(G["bridge"].child.running())

    def test_openai_door_goes_to_llama_server(self):
        st, data, r = call("POST", "/v1/chat/completions", {"model": "small:8b", "stream": True,
                                                            "messages": [{"role": "user", "content": "oai MARK5"}]})
        self.assertEqual(st, 200, data)
        self.assertIn("text/event-stream", r.getheader("Content-Type"))
        self.assertIn(b"ANSWER-", data)
        self.assertIn(b"[DONE]", data)
        self.assertTrue(G["bridge"].child.running())
        # and Ollama's own OpenAI door for its models
        st, data, _ = call("POST", "/v1/chat/completions", {"model": "huge:70b", "stream": False,
                                                            "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(st, 200)
        self.assertEqual(G["stub"].calls[-1][1], "/v1/chat/completions")

    def test_status_and_unknown_model(self):
        st, data, _ = call("GET", "/v1/bridge/status")
        self.assertEqual(st, 200)
        s = json.loads(data)
        self.assertEqual((s["running"], s["models"], s["enabled"]), (False, ["small:8b"], True))
        st, data, _ = call("POST", "/api/chat", {"model": "ghost:1", "messages": []})
        self.assertEqual(st, 404)         # Ollama's answer, passed through

    def test_child_that_dies_is_reported_and_forgotten(self):
        o1llamacpp.Child.EXTRA_ENV["STUB_LLAMA_DIE"] = "1"
        try:
            st, data, _ = call("POST", "/api/generate", {"model": "small:8b", "prompt": ""})
        finally:
            o1llamacpp.Child.EXTRA_ENV.pop("STUB_LLAMA_DIE", None)
        self.assertEqual(st, 502)
        self.assertIn("stopped while loading", json.loads(data)["error"])
        self.assertFalse(G["bridge"].child.running())
        self.assertIn("died", [e for e, _f in G["logs"]])

    def test_nothing_of_a_prompt_in_the_journal(self):
        call("POST", "/api/chat", {"model": "small:8b", "messages": [{"role": "user", "content": "secret MARK6"}]})
        call("POST", "/api/chat", {"model": "huge:70b", "messages": [{"role": "user", "content": "secret MARK7"}]})
        text = json.dumps(G["logs"])
        self.assertNotIn("MARK6", text)
        self.assertNotIn("MARK7", text)
        self.assertNotIn("secret", text)


def load_gateway():
    path = os.path.join(U.BIN, "ollama1-gateway")
    loader = importlib.machinery.SourceFileLoader("o1gateway_llamacpp", path)
    spec = importlib.util.spec_from_loader("o1gateway_llamacpp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestGatewayThroughTheBridge(unittest.TestCase):
    """The real gateway with llamacpp_bridge set: a signed chat for the
    bridged model is answered by llama-server through the bridge, in the
    shape the app reads; the fit check still reads Ollama's /api/show; the
    VRAM check reads the merged /api/ps."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_gateway()
        cls.mod.PAIR_MIN_INTERVAL = 0.0
        cls.key = U.RSAKey("kid-1")
        cls.jwks = U.FakeJWKS([cls.key])
        cfg = dict(DEFAULTS)
        cfg.update(U.BASE_CFG)
        cfg.update({"access_team_domain": U.TEAM, "gateway_aud": U.GW_AUD, "admin_aud": U.ADMIN_AUD,
                    "admin_email": U.ADMIN_EMAIL, "service_token_client_id": U.CLIENT_ID,
                    "gateway_port": U.free_port(), "ollama_url": "http://127.0.0.1:%d" % G["stub"].port,
                    "llamacpp_bridge": G["base"],
                    "certs_url": cls.jwks.url, "allow_insecure_certs_url": True,
                    "vram_total_bytes": 16 * GIB, "queue_wait_s": 20})
        cls.cfg = cfg
        cls.dev = U.Device(name="Alice's Mac")
        U.write_devices([cls.dev])
        cls.gw, cls.servers, cls.stop = cls.mod.serve(cfg)
        cls.port = cfg["gateway_port"]
        time.sleep(1.1)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        for s in cls.servers:
            s.shutdown()
        cls.jwks.close() if hasattr(cls.jwks, "close") else None
        G["bridge"].child.stop("test")

    def gcall(self, method, path, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        h = {"Cf-Access-Jwt-Assertion": U.make_jwt(self.key, U.claims())}
        h.update(self.dev.headers(method, path, body))
        return U.request(self.port, method, path, body, h)

    def test_signed_chat_reaches_llama_server(self):
        G["stub"].loaded.clear()
        G["vram"]["value"] = 5 * GIB          # the stub's small:8b weighs 5 GiB in Ollama's tags
        st, data, r = self.gcall("POST", "/api/chat", {"model": "small:8b",
                                                       "messages": [{"role": "user", "content": "via gw MARK8"}]})
        self.assertEqual(st, 200, data)
        lines = U.ndjson(data)
        self.assertEqual(lines[0]["message"]["content"], "ANSWER-via gw MARK8")
        self.assertTrue(lines[-1]["done"])
        self.assertEqual(lines[-1]["eval_count"], 3)
        self.assertTrue(G["bridge"].child.running())
        self.assertEqual(self.gw.base, G["base"])
        # the gateway asked Ollama (through the bridge) to describe the model for its fit check
        self.assertIn("/api/show", [c[1] for c in G["stub"].calls])
        # and the OpenAI-style door
        st, data, r = self.gcall("GET", "/api/ps")
        self.assertEqual(st, 200)
        self.assertIn("small:8b", [m["name"] for m in json.loads(data)["models"]])

    def test_vram_unknown_is_refused_by_the_gateway(self):
        G["bridge"].child.stop("test")
        G["vram"]["value"] = None
        st, data, _ = self.gcall("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                                       "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(st, 507, data)
        self.assertEqual(json.loads(data).get("code"), "gpu_spill")
        self.assertFalse(G["bridge"].child.running())

    def test_ollama_model_through_the_bridge_still_works(self):
        st, data, _ = self.gcall("POST", "/api/chat", {"model": "huge:70b", "stream": False,
                                                       "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(st, 507)          # the gateway's own fit check, as before: 20 GiB on a 16 GiB card
        st, data, _ = self.gcall("GET", "/api/tags")
        self.assertEqual(st, 200)


class TestUnits(unittest.TestCase):
    def unit(self, name):
        with open(os.path.join(U.SYSTEMD, name)) as f:
            return f.read()

    def test_bridge_unit(self):
        t = self.unit("ollama1-llamacpp.service")
        for line in ("User=ollama", "SupplementaryGroups=render video o1gw", "LimitCORE=0", "MemorySwapMax=0",
                     "LoadCredential=config.json:/etc/ollama1/config.json",
                     "LoadCredential=llamacpp.json:/etc/ollama1/llamacpp.json",
                     "ExecStart=/usr/local/lib/ollama1/bin/ollama1-llamacpp serve",
                     "Requires=ollama1-nft.service ollama.service", "ReadWritePaths=/run/ollama1/llamacpp",
                     "NoNewPrivileges=yes", "ProtectSystem=strict"):
            self.assertIn("\n" + line + "\n", t, line)
        self.assertNotIn("WantedBy=multi-user.target\n\n", t)
        tmp = open(os.path.join(U.CONFIG, "ollama1.tmpfiles")).read()
        self.assertIn("d /run/ollama1/llamacpp     2770 ollama o1gw   -", tmp)

    def test_shipped_config_is_off_and_empty(self):
        with open(os.path.join(U.CONFIG, "llamacpp.json")) as f:
            cfg = json.load(f)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["models"], {})
        self.assertEqual(cfg["bin"], o1llamacpp.DEFAULT_BIN)
        self.assertIn("llamacpp_bridge", DEFAULTS)
        self.assertEqual(DEFAULTS["llamacpp_bridge"], "")

    def test_main_check_runs(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = o1llamacpp.main(["check", "--config", G["cfg_path"]])
        self.assertIn("small:8b", buf.getvalue())
        self.assertIn(code, (0, 1))        # 1 on this Mac: no nvidia-smi, no gateway key


if __name__ == "__main__":
    unittest.main()
