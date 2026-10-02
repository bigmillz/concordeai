"""Image and video generation (6b356): the gateway's /v1/generate routes against a
stub ComfyUI and a stub Ollama. Signed and paired-only like every route; one job at
a time; the card freed before and after; a running job counts as activity for auto
sleep; results kept ten minutes and then gone; nothing a request carried in a log
or a file.

What this cannot show is that a real ComfyUI accepts the templates in
lib/o1gen.py: only a real run on the server can."""
import ast
import contextlib
import io
import json
import os
import re
import threading
import time
import unittest
import uuid

import o1test_util as U  # sets OLLAMA1_PREFIX first

import o1gen  # noqa: E402
from o1common import DEFAULTS  # noqa: E402
from stub_comfyui import MP4, PNG, WEBP, StubComfy  # noqa: E402
from stub_ollama import Stub  # noqa: E402
import test_gateway as TG  # noqa: E402

GIB = 1 << 30
G = {}


def setUpModule():
    G["mod"] = TG.load_gateway()
    G["mod"].PAIR_MIN_INTERVAL = 0.0
    G["key"] = U.RSAKey("kid-1")
    G["jwks"] = U.FakeJWKS([G["key"]])
    G["ollama"] = Stub(U.free_port())
    G["comfy"] = StubComfy(U.free_port())
    cfg = dict(DEFAULTS)
    cfg.update(U.BASE_CFG)
    cfg.update({"access_team_domain": U.TEAM, "gateway_aud": U.GW_AUD, "admin_aud": U.ADMIN_AUD,
                "admin_email": U.ADMIN_EMAIL, "service_token_client_id": U.CLIENT_ID,
                "gateway_port": U.free_port(), "ollama_url": "http://127.0.0.1:%d" % G["ollama"].port,
                "comfyui_url": "http://127.0.0.1:%d" % G["comfy"].port,
                "certs_url": G["jwks"].url, "allow_insecure_certs_url": True,
                "vram_total_bytes": 16 * GIB, "queue_wait_s": 20})
    G["dev"] = U.Device(name="Alice's Mac")
    G["dev2"] = U.Device(name="Bob's Mac")
    U.write_devices([G["dev"], G["dev2"]])
    G["gw"], G["servers"], G["stop"] = G["mod"].serve(cfg)
    G["port"] = cfg["gateway_port"]
    G["gw"].gen.poll_s = 0.05
    time.sleep(1.1)  # signatures must be dated after the gateway started


def tearDownModule():
    G["stop"].set()
    for s in G["servers"]:
        s.shutdown()
    G["comfy"].close()
    G["ollama"].close()
    G["jwks"].close()


def call(method, path, obj=None, dev=None, raw=None, jwt=True, sign=True, headers=None):
    body = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
    h = {}
    if jwt:
        h["Cf-Access-Jwt-Assertion"] = U.make_jwt(G["key"], U.claims())
    if sign:
        h.update((dev or G["dev"]).headers(method, path, body))
    h.update(headers or {})
    return U.request(G["port"], method, path, body, h)


def js(resp):
    return json.loads(resp[1])


def start(kind="image", **kw):
    st, data, _ = call("POST", "/v1/generate/jobs", dict({"kind": kind, "prompt": "a red balloon"}, **kw))
    return st, json.loads(data)


def wait_state(jid, states, timeout=15, dev=None):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        st, data, _ = call("GET", "/v1/generate/jobs/" + jid, dev=dev)
        if st == 200:
            last = json.loads(data)
            if last["state"] in states:
                return last
        time.sleep(0.03)
    raise AssertionError("job never reached %s: %s" % (states, last))


def wait_idle(timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if not G["gw"].gen.running() and not G["gw"].slot.busy:
            return
        time.sleep(0.03)
    raise AssertionError("the generator never went idle")


class Base(unittest.TestCase):
    def setUp(self):
        wait_idle()
        c = G["comfy"]
        c.nodes[:] = list(__import__("stub_comfyui").DEFAULT_NODES)
        c.files.clear()
        c.files.update(__import__("stub_comfyui").DEFAULT_FILES)
        c.combo, c.run_s, c.steps = "list", 0.3, 4
        c.fail = c.refuse = c.outputs_missing = c.no_ws = False
        c.output, c.on_prompt, c.view_status = None, None, 200
        c.hold.set()
        c.calls.clear()
        c.history.clear()
        c.interrupted.clear()
        gw = G["gw"]
        gw.gen.clock = time.time
        gw.gen.caps_at = None
        gw.gen.jobs.clear()
        gw.activity.inflight = 0
        G["ollama"].loaded.clear()
        gw.last_device = None


class TestCapabilities(Base):
    def caps(self):
        G["gw"].gen.caps_at = None
        st, data, _ = call("GET", "/v1/generate/capabilities")
        self.assertEqual(st, 200)
        return json.loads(data)

    def test_both_when_everything_is_there(self):
        self.assertEqual(self.caps(), {"image": True, "video": True})

    def test_exactly_two_booleans_and_nothing_about_the_machine(self):
        st, data, _ = call("GET", "/v1/generate/capabilities")
        self.assertEqual(set(json.loads(data)), {"image", "video"})
        for bad in (b"/", b"safetensors", b"comfy", b"8188", b"models"):
            self.assertNotIn(bad, data.lower())

    def test_image_needs_the_flux_checkpoint(self):
        G["comfy"].files["CheckpointLoaderSimple/ckpt_name"] = ["something-else.safetensors"]
        self.assertEqual(self.caps(), {"image": False, "video": True})

    def test_video_needs_all_three_wan_files(self):
        for key in ("UNETLoader/unet_name", "CLIPLoader/clip_name", "VAELoader/vae_name"):
            c = G["comfy"]
            keep = c.files[key]
            c.files[key] = []
            self.assertEqual(self.caps(), {"image": True, "video": False}, key)
            c.files[key] = keep

    def test_the_newer_combo_spelling_is_read_too(self):
        G["comfy"].combo = "COMBO"
        self.assertEqual(self.caps(), {"image": True, "video": True})
        G["comfy"].files["CheckpointLoaderSimple/ckpt_name"] = []
        self.assertEqual(self.caps(), {"image": False, "video": True})

    def test_a_missing_node_turns_it_off(self):
        G["comfy"].nodes.remove("EmptyHunyuanLatentVideo")
        self.assertEqual(self.caps(), {"image": True, "video": False})
        G["comfy"].nodes.append("EmptyHunyuanLatentVideo")
        G["comfy"].nodes.remove("SaveImage")
        self.assertEqual(self.caps(), {"image": False, "video": True})

    def test_video_falls_back_to_animated_webp_without_the_mp4_nodes(self):
        G["comfy"].nodes.remove("SaveVideo")
        self.assertEqual(self.caps(), {"image": True, "video": True})
        self.assertEqual(G["gw"].gen.video_fmt, "webp")
        G["comfy"].nodes.remove("SaveAnimatedWEBP")
        self.assertEqual(self.caps(), {"image": True, "video": False})

    def test_comfyui_down_means_neither(self):
        gen = o1gen.Generator("http://127.0.0.1:%d" % U.free_port(), object())
        self.assertEqual(gen.capabilities(), {"image": False, "video": False})

    def test_only_this_machine_is_ever_asked(self):
        asked = []
        for url in ("http://203.0.113.9:8188", "http://example.test:8188", "http://[2001:db8::1]:8188"):
            gen = o1gen.Generator(url, object())
            gen.comfy.call = lambda *a, **k: asked.append(a) or (_ for _ in ()).throw(OSError("never"))
            self.assertEqual(gen.capabilities(), {"image": False, "video": False})
        self.assertEqual(asked, [])
        self.assertEqual(G["comfy"].calls, [])
        gen = o1gen.Generator("http://localhost:8188", object())
        gen.comfy.call = lambda *a, **k: asked.append(a) or (_ for _ in ()).throw(OSError("down"))
        gen.capabilities()
        self.assertEqual(len(asked), 1)                  # the loopback names are asked

    def test_the_answer_is_kept_briefly(self):
        self.caps()
        n = len(G["comfy"].calls)
        call("GET", "/v1/generate/capabilities")
        self.assertEqual(len(G["comfy"].calls), n)


class TestGate(Base):
    ROUTES = (("GET", "/v1/generate/capabilities", None),
              ("POST", "/v1/generate/jobs", {"kind": "image", "prompt": "x"}),
              ("GET", "/v1/generate/jobs/" + "a" * 32, None),
              ("GET", "/v1/generate/jobs/" + "a" * 32 + "/result", None),
              ("DELETE", "/v1/generate/jobs/" + "a" * 32, None))

    def test_unsigned_unpaired_and_no_access_are_refused_everywhere(self):
        for method, path, obj in self.ROUTES:
            self.assertEqual(call(method, path, obj, sign=False)[0], 401, path)
            self.assertEqual(call(method, path, obj, dev=U.Device())[0], 403, path)
            self.assertEqual(call(method, path, obj, jwt=False)[0], 403, path)
        self.assertFalse(G["comfy"].paths())

    def test_a_replayed_request_is_refused(self):
        h = G["dev"].headers("GET", "/v1/generate/capabilities")
        h["Cf-Access-Jwt-Assertion"] = U.make_jwt(G["key"], U.claims())
        a = U.request(G["port"], "GET", "/v1/generate/capabilities", b"", h)
        b = U.request(G["port"], "GET", "/v1/generate/capabilities", b"", h)
        self.assertEqual((a[0], b[0]), (200, 401))
        self.assertEqual(json.loads(b[1])["code"], "replay")

    def test_a_tampered_body_is_refused_and_nothing_starts(self):
        body = json.dumps({"kind": "image", "prompt": "one"}).encode()
        h = G["dev"].headers("POST", "/v1/generate/jobs", body)
        h["Cf-Access-Jwt-Assertion"] = U.make_jwt(G["key"], U.claims())
        st, _, _ = U.request(G["port"], "POST", "/v1/generate/jobs", body.replace(b"one", b"two"), h)
        self.assertEqual(st, 401)
        self.assertFalse(G["comfy"].paths())

    def test_wrong_methods_and_paths_are_404(self):
        for method, path in (("PUT", "/v1/generate/jobs"), ("GET", "/v1/generate/jobs"),
                             ("POST", "/v1/generate/capabilities"), ("GET", "/v1/generate/"),
                             ("DELETE", "/v1/generate/jobs/" + "a" * 32 + "/result"),
                             ("GET", "/v1/generate/jobs/../x")):
            st, _, _ = call(method, path, {} if method in ("PUT", "POST") else None)
            self.assertEqual(st, 404, (method, path))

    def test_another_paired_device_cannot_see_a_job(self):
        st, ans = start()
        self.assertEqual(st, 202)
        jid = ans["id"]
        for path in ("/v1/generate/jobs/" + jid, "/v1/generate/jobs/" + jid + "/result"):
            self.assertEqual(call("GET", path, dev=G["dev2"])[0], 404)
        self.assertEqual(call("DELETE", "/v1/generate/jobs/" + jid, dev=G["dev2"])[0], 404)
        wait_state(jid, ("done",))
        self.assertEqual(call("GET", "/v1/generate/jobs/" + jid + "/result")[0], 200)

    def test_job_ids_are_long_and_unguessable(self):
        ids = set()
        for _ in range(3):
            st, ans = start()
            self.assertEqual(st, 202)
            self.assertRegex(ans["id"], r"^[A-Za-z0-9_-]{32,}$")
            ids.add(ans["id"])
            wait_state(ans["id"], ("done",))
            wait_idle()
        self.assertEqual(len(ids), 3)
        for bad in ("short", "a" * 15, "a" * 65, "../etc/passwd", "a" * 31 + "!"):
            self.assertEqual(call("GET", "/v1/generate/jobs/" + bad)[0], 404, bad)


class TestRequests(Base):
    def rejected(self, kind="image", **kw):
        st, ans = start(kind, **kw)
        self.assertEqual(st, 400, (kind, kw, ans))
        self.assertEqual(ans["code"], "bad_request")
        self.assertNotIn("/prompt", G["comfy"].paths())

    def test_validation(self):
        self.rejected(width=1000)                      # not a multiple of 64
        self.rejected(width=128)                       # under the minimum
        self.rejected(width=2048)                      # over the maximum
        self.rejected(width=1536, height=1536)         # too many pixels
        self.rejected(width="1024")
        self.rejected(width=True)
        self.rejected(steps=0)
        self.rejected(steps=99)
        self.rejected(steps=4.5)
        self.rejected(seed=-1)
        self.rejected(seed="7")
        self.rejected(seed=2 ** 60)
        self.rejected(seconds=2)                       # video only
        self.rejected(frames=33)                       # video only
        self.rejected(workflow={"1": {}})              # the app never sends a graph
        self.rejected(model="x.safetensors")
        self.rejected(negative="x")
        self.rejected("video", width=790)              # not a multiple of 16
        self.rejected("video", width=1280, height=720)
        self.rejected("video", seconds=0)
        self.rejected("video", seconds=60)
        self.rejected("video", seconds="2")
        self.rejected("video", frames=20)              # not 4n+1
        self.rejected("video", frames=161)
        self.rejected("video", steps=2)
        self.rejected("audio")
        self.rejected(None)

    def test_prompts(self):
        for p in ("", "   ", None, 7, ["a"], "x" * 1001, "\x00\x01"):
            st, data, _ = call("POST", "/v1/generate/jobs", {"kind": "image", "prompt": p})
            self.assertEqual(st, 400, repr(p)[:20])
        st, data, _ = call("POST", "/v1/generate/jobs", {"kind": "image"})
        self.assertEqual(st, 400)
        self.assertFalse(G["comfy"].posted("/prompt"))
        st, ans = start(prompt="x" * 1000)
        self.assertEqual(st, 202)
        wait_state(ans["id"], ("done",))

    def test_not_json_and_not_an_object(self):
        for raw in (b"nope", b"[1]", b'"x"'):
            self.assertEqual(call("POST", "/v1/generate/jobs", raw=raw)[0], 400)

    def test_the_template_gets_only_the_whitelisted_numbers_and_the_prompt(self):
        seen = []
        G["comfy"].on_prompt = seen.append
        hostile = 'a cat"}, "9": {"class_type": "LoadImage", "inputs": {"image": "../../etc/passwd"}} <script>'
        st, ans = start(prompt=hostile, width=768, height=512, steps=6, seed=1234)
        self.assertEqual(st, 202)
        wait_state(ans["id"], ("done",))
        g = seen[0]
        self.assertEqual(sorted(v["class_type"] for v in g.values()),
                         sorted(["CheckpointLoaderSimple", "CLIPTextEncode", "CLIPTextEncode",
                                 "EmptySD3LatentImage", "KSampler", "VAEDecode", "SaveImage"]))
        texts = [v["inputs"]["text"] for v in g.values() if v["class_type"] == "CLIPTextEncode"]
        self.assertEqual(sorted(texts), sorted([hostile.strip(), ""]))
        lat = next(v for v in g.values() if v["class_type"] == "EmptySD3LatentImage")["inputs"]
        self.assertEqual((lat["width"], lat["height"], lat["batch_size"]), (768, 512, 1))
        ks = next(v for v in g.values() if v["class_type"] == "KSampler")["inputs"]
        self.assertEqual((ks["steps"], ks["seed"], ks["cfg"], ks["sampler_name"], ks["scheduler"]),
                         (6, 1234, 1.0, "euler", "simple"))
        self.assertEqual(next(v for v in g.values() if v["class_type"] == "CheckpointLoaderSimple")["inputs"],
                         {"ckpt_name": "flux1-schnell-fp8.safetensors"})
        # the prompt is in no other place of the graph
        self.assertEqual(json.dumps(g).count("etc/passwd"), 1)
        self.assertNotIn("LoadImage", [v["class_type"] for v in g.values()])

    def test_the_video_template(self):
        seen = []
        G["comfy"].on_prompt = seen.append
        st, ans = start("video", prompt="waves", seconds=2, width=640, height=368, steps=12, seed=5)
        self.assertEqual(st, 202)
        wait_state(ans["id"], ("done",))
        g = seen[0]
        by = {v["class_type"]: v["inputs"] for v in g.values()}
        self.assertEqual(by["UNETLoader"]["unet_name"], "wan2.1_t2v_1.3B_fp16.safetensors")
        self.assertEqual(by["CLIPLoader"]["clip_name"], "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
        self.assertEqual(by["CLIPLoader"]["type"], "wan")
        self.assertEqual(by["VAELoader"]["vae_name"], "wan_2.1_vae.safetensors")
        v = by["EmptyHunyuanLatentVideo"]
        self.assertEqual((v["width"], v["height"], v["length"]), (640, 368, 33))
        self.assertEqual(by["KSampler"]["steps"], 12)
        self.assertEqual(by["CreateVideo"]["fps"], 16.0)
        self.assertEqual((by["SaveVideo"]["format"], by["SaveVideo"]["codec"]), ("mp4", "h264"))
        self.assertEqual(sorted(c.get("text") for c in g.values() if c["class_type"] == "CLIPTextEncode"
                                for c in [c["inputs"]])[1], "waves")

    def test_seconds_become_frames_of_the_right_shape(self):
        for sec, frames in ((1, 17), (2, 33), (3, 49), (5, 81), (4.9, 77), (1.2, 17)):
            self.assertEqual(o1gen.parse_request({"kind": "video", "prompt": "x", "seconds": sec})[1]["frames"],
                             frames, sec)
        for f in (17, 33, 81):
            self.assertEqual(o1gen.parse_request({"kind": "video", "prompt": "x", "frames": f})[1]["frames"], f)
        self.assertEqual(o1gen.parse_request({"kind": "video", "prompt": "x"})[1]["frames"], 33)

    def test_defaults(self):
        p = o1gen.parse_request({"kind": "image", "prompt": "x"})[1]
        self.assertEqual((p["width"], p["height"], p["steps"]), (1024, 1024, 4))
        p = o1gen.parse_request({"kind": "video", "prompt": "x"})[1]
        self.assertEqual((p["width"], p["height"], p["steps"], p["frames"]), (832, 480, 20, 33))

    def test_a_server_without_the_models_says_so(self):
        G["comfy"].files["CheckpointLoaderSimple/ckpt_name"] = []
        G["gw"].gen.caps_at = None
        st, ans = start()
        self.assertEqual((st, ans["code"]), (503, "unavailable"))
        self.assertFalse(G["comfy"].posted("/prompt"))
        self.assertFalse(G["gw"].slot.busy)


class TestLifecycle(Base):
    def test_an_image_from_start_to_bytes(self):
        st, ans = start(width=512, height=512)
        self.assertEqual(st, 202)
        self.assertEqual(set(ans), {"id"})
        jid = ans["id"]
        seen = []
        end = time.time() + 15
        while time.time() < end:
            s = js(call("GET", "/v1/generate/jobs/" + jid))
            seen.append(s)
            if s["state"] == "done":
                break
            time.sleep(0.02)
        self.assertEqual(seen[-1]["state"], "done")
        self.assertEqual(seen[-1]["progress"], 1.0)
        self.assertEqual(seen[-1]["type"], "image/png")
        self.assertEqual(seen[-1]["bytes"], len(PNG))
        self.assertEqual({s["state"] for s in seen} - {"queued", "running", "done"}, set())
        running = [s["progress"] for s in seen if s["state"] == "running"]
        self.assertTrue(running)
        self.assertEqual(running, sorted(running))                 # never goes back
        self.assertGreater(max(running), 0.2)                      # the sampler's steps came through
        self.assertTrue(all(0 <= p <= 1 for p in running))
        st, data, resp = call("GET", "/v1/generate/jobs/" + jid + "/result")
        self.assertEqual(st, 200)
        self.assertEqual(data, PNG)
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(call("GET", "/v1/generate/jobs/" + jid + "/result")[1], PNG)     # fetch again

    def test_a_video_is_an_mp4(self):
        st, ans = start("video", seconds=1)
        self.assertEqual(st, 202)
        done = wait_state(ans["id"], ("done",))
        self.assertEqual(done["type"], "video/mp4")
        st, data, resp = call("GET", "/v1/generate/jobs/%s/result" % ans["id"])
        self.assertEqual((st, data, resp.getheader("Content-Type")), (200, MP4, "video/mp4"))

    def test_the_webp_fallback_for_a_comfyui_without_the_mp4_nodes(self):
        G["comfy"].nodes.remove("SaveVideo")
        G["gw"].gen.caps_at = None
        seen = []
        G["comfy"].on_prompt = seen.append
        st, ans = start("video")
        self.assertEqual(st, 202)
        done = wait_state(ans["id"], ("done",))
        self.assertEqual(done["type"], "image/webp")
        self.assertIn("SaveAnimatedWEBP", [v["class_type"] for v in seen[0].values()])
        self.assertNotIn("SaveVideo", [v["class_type"] for v in seen[0].values()])
        self.assertEqual(call("GET", "/v1/generate/jobs/%s/result" % ans["id"])[1], WEBP)

    def test_the_result_is_not_ready_while_it_runs(self):
        G["comfy"].hold.clear()
        st, ans = start()
        wait_state(ans["id"], ("running",))
        st, data, _ = call("GET", "/v1/generate/jobs/%s/result" % ans["id"])
        self.assertEqual((st, json.loads(data)["code"]), (409, "not_ready"))
        G["comfy"].hold.set()
        wait_state(ans["id"], ("done",))

    def test_progress_is_absent_not_invented_without_the_websocket(self):
        G["comfy"].no_ws = True
        G["comfy"].hold.clear()
        st, ans = start()
        s = wait_state(ans["id"], ("running",))
        time.sleep(0.4)
        s = js(call("GET", "/v1/generate/jobs/" + ans["id"]))
        self.assertEqual(s["progress"], 0.05)
        G["comfy"].hold.set()
        self.assertEqual(wait_state(ans["id"], ("done",))["progress"], 1.0)

    def test_one_job_at_a_time_and_a_chat_during_a_job_is_told_busy(self):
        G["comfy"].hold.clear()
        st, ans = start()
        wait_state(ans["id"], ("running",))
        self.assertTrue(G["gw"].slot.busy)
        st, second = start("video")
        self.assertEqual((st, second["code"]), (409, "busy"))
        t0 = time.time()
        st, data, _ = call("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                                 "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((st, json.loads(data)["code"]), (503, "busy"))
        self.assertIn("picture or video", json.loads(data)["error"])
        self.assertLess(time.time() - t0, 3)             # told at once, not after the queue's wait
        self.assertEqual(call("POST", "/api/embed", {"model": "embed:small", "input": "x"})[0], 503)
        # lists and polls still work
        self.assertEqual(call("GET", "/api/tags")[0], 200)
        G["comfy"].hold.set()
        wait_state(ans["id"], ("done",))
        wait_idle()
        st, data, _ = call("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                                 "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 200)       # the card is free for chats again

    def test_a_job_while_a_chat_holds_the_card_is_told_busy(self):
        gw = G["gw"]
        self.assertEqual(gw.slot.acquire(0), "ok")
        try:
            st, ans = start()
            self.assertEqual((st, ans["code"]), (409, "busy"))
        finally:
            gw.slot.release()
        self.assertFalse(G["comfy"].posted("/prompt"))

    def test_ollamas_models_are_unloaded_first_and_comfyui_is_freed_after(self):
        st, data, _ = call("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                                 "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 200)
        self.assertIn("small:8b", G["ollama"].loaded)
        at_prompt = []
        G["comfy"].on_prompt = lambda g: at_prompt.append(sorted(G["ollama"].loaded))
        n_ollama = len(G["ollama"].calls)
        st, ans = start()
        wait_state(ans["id"], ("done",))
        wait_idle()
        self.assertEqual(at_prompt, [[]])                       # nothing of Ollama's was on the card
        unloads = [c for c in G["ollama"].calls[n_ollama:] if c[0] == "POST" and (c[2] or {}).get("keep_alive") == 0]
        self.assertEqual([u[2]["model"] for u in unloads], ["small:8b"])
        paths = G["comfy"].paths()
        self.assertLess(paths.index("/prompt"), paths.index("/free"))
        self.assertEqual(G["comfy"].posted("/free"), [{"unload_models": True, "free_memory": True}])
        self.assertEqual(G["comfy"].posted("/history"), [{"delete": [G["comfy"].last_pid]}])

    def test_a_job_does_not_start_when_the_models_cannot_be_unloaded(self):
        call("POST", "/api/chat", {"model": "small:8b", "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]})
        gw = G["gw"]
        real = gw.unload
        gw.unload = lambda *a, **k: None              # Ollama keeps the model
        try:
            st, ans = start()
            self.assertEqual(st, 202)
            s = wait_state(ans["id"], ("failed",))
            self.assertEqual(s["error"], "generation failed")
        finally:
            gw.unload = real
        self.assertFalse(G["comfy"].posted("/prompt"))
        wait_idle()
        self.assertFalse(gw.slot.busy)

    def test_failures_say_one_fixed_thing(self):
        cases = (("execution error", lambda c: setattr(c, "fail", True)),
                 ("refused graph", lambda c: setattr(c, "refuse", True)),
                 ("no output", lambda c: setattr(c, "outputs_missing", True)),
                 ("not a picture", lambda c: setattr(c, "output", b"<html>not a png</html>" * 10)),
                 ("a video in place of a picture", lambda c: setattr(c, "output", MP4)),
                 ("too large", lambda c: setattr(c, "output", PNG + b"\x00" * (17 << 20))),
                 ("view fails", lambda c: setattr(c, "view_status", 500)))
        for name, setup in cases:
            Base.setUp(self)
            setup(G["comfy"])
            st, ans = start()
            self.assertEqual(st, 202, name)
            s = wait_state(ans["id"], ("failed",))
            self.assertEqual(s["error"], "generation failed", name)
            self.assertEqual(set(s) - {"state", "kind", "progress", "error"}, set(), name)
            self.assertNotIn(b"boom", json.dumps(s).encode())
            self.assertEqual(call("GET", "/v1/generate/jobs/%s/result" % ans["id"])[0], 409, name)
            wait_idle()
            self.assertFalse(G["gw"].slot.busy, name)
            self.assertIn("/free", G["comfy"].paths(), name)       # the card is given back after a failure too

    def test_a_video_result_is_checked_against_the_kind(self):
        G["comfy"].output = PNG
        st, ans = start("video", seconds=1)
        wait_state(ans["id"], ("failed",))

    def test_comfyui_dying_mid_job_fails_it(self):
        G["comfy"].hold.clear()
        st, ans = start()
        wait_state(ans["id"], ("running",))
        gen = G["gw"].gen
        real = gen.comfy.port
        gen.comfy.port = U.free_port()          # nothing answers there now
        try:
            wait_state(ans["id"], ("failed",), timeout=20)
        finally:
            gen.comfy.port = real
            G["comfy"].hold.set()
        wait_idle()

    def test_stop_cancels_the_job_and_frees_the_card(self):
        G["comfy"].hold.clear()
        st, ans = start()
        wait_state(ans["id"], ("running",))
        st, data, _ = call("DELETE", "/v1/generate/jobs/" + ans["id"])
        self.assertEqual((st, json.loads(data)), (200, {"ok": True}))
        self.assertEqual(call("GET", "/v1/generate/jobs/" + ans["id"])[0], 404)
        G["comfy"].hold.set()
        wait_idle()
        self.assertTrue(G["comfy"].posted("/interrupt"))
        self.assertEqual(G["comfy"].posted("/queue"), [{"delete": [G["comfy"].last_pid]}])
        self.assertIn("/free", G["comfy"].paths())
        self.assertEqual(G["gw"].activity.inflight, 0)
        st, again = start()
        self.assertEqual(st, 202)
        wait_state(again["id"], ("done",))

    def test_deleting_a_finished_job_removes_its_result(self):
        st, ans = start()
        wait_state(ans["id"], ("done",))
        self.assertEqual(call("DELETE", "/v1/generate/jobs/" + ans["id"])[0], 200)
        self.assertEqual(call("GET", "/v1/generate/jobs/%s/result" % ans["id"])[0], 404)
        self.assertEqual(G["gw"].gen.jobs, {})

    def test_at_most_a_couple_of_finished_jobs_are_kept(self):
        ids = []
        for _ in range(4):
            st, ans = start()
            ids.append(ans["id"])
            wait_state(ans["id"], ("done",))
            wait_idle()
        self.assertEqual(call("GET", "/v1/generate/jobs/" + ids[0])[0], 404)
        self.assertEqual(call("GET", "/v1/generate/jobs/" + ids[3])[0], 200)
        self.assertLessEqual(len(G["gw"].gen.jobs), o1gen.KEEP_FINISHED)


class TestTimeAndActivity(Base):
    """A fake clock for the generator only: results expire, activity is held."""

    def setUp(self):
        super().setUp()
        self.now = [1_800_000_000.0]
        G["gw"].gen.clock = lambda: self.now[0]
        G["gw"].activity.clock = lambda: self.now[0]

    def tearDown(self):
        G["gw"].gen.clock = time.time
        G["gw"].activity.clock = time.time

    def test_a_running_job_counts_as_a_request_in_flight(self):
        gw = G["gw"]
        G["comfy"].hold.clear()
        self.assertEqual(gw.activity.inflight, 0)
        st, ans = start()
        wait_state(ans["id"], ("running",))
        self.assertEqual(gw.activity.inflight, 1)
        snap = gw.activity.snapshot()
        self.assertEqual(snap["inflight"], 1)
        G["comfy"].hold.set()
        wait_state(ans["id"], ("done",))
        wait_idle()
        self.assertEqual(gw.activity.inflight, 1)             # still held: the app is about to fetch it

    def test_activity_lasts_120_seconds_after_the_last_poll_or_fetch(self):
        gw = G["gw"]
        st, ans = start()
        wait_state(ans["id"], ("done",))
        wait_idle()
        self.now[0] += 100
        self.assertEqual(call("GET", "/v1/generate/jobs/%s/result" % ans["id"])[0], 200)   # a fetch
        last = gw.activity.last
        self.assertEqual(last, self.now[0])
        self.now[0] += 119
        gw.gen.sweep()
        self.assertEqual(gw.activity.inflight, 1)
        self.now[0] += 2
        gw.gen.sweep()
        self.assertEqual(gw.activity.inflight, 0)
        self.assertEqual(gw.activity.last, self.now[0])       # it ended just now, as any request does

    def test_a_poll_of_a_job_is_activity_but_capabilities_and_usage_are_not(self):
        gw = G["gw"]
        st, ans = start()
        wait_state(ans["id"], ("done",))
        wait_idle()
        self.now[0] += 50
        before = gw.activity.last
        call("GET", "/v1/generate/capabilities")
        call("GET", "/v1/usage")
        call("GET", "/v1/info")
        self.assertEqual(gw.activity.last, before)
        call("GET", "/v1/generate/jobs/" + ans["id"])
        self.assertEqual(gw.activity.last, self.now[0])
        self.assertEqual(call("GET", "/v1/generate/jobs/" + "z" * 32)[0], 404)

    def test_a_poll_of_a_job_that_isnt_there_is_not_activity(self):
        gw = G["gw"]
        self.now[0] += 77
        before = gw.activity.last
        call("GET", "/v1/generate/jobs/" + "z" * 32)
        self.assertEqual(gw.activity.last, before)

    def test_results_are_kept_ten_minutes(self):
        st, ans = start()
        wait_state(ans["id"], ("done",))
        wait_idle()
        self.now[0] += o1gen.RESULT_KEEP_S - 1
        self.assertEqual(call("GET", "/v1/generate/jobs/%s/result" % ans["id"])[0], 200)
        self.now[0] += 2                              # polled just now, but the clock runs from when it finished
        G["gw"].gen.sweep()
        self.assertEqual(call("GET", "/v1/generate/jobs/%s/result" % ans["id"])[0], 404)
        self.assertEqual(G["gw"].gen.jobs, {})
        self.assertEqual(G["gw"].activity.inflight, 0)

    def test_a_job_nobody_polls_is_stopped(self):
        gw = G["gw"]
        G["comfy"].hold.clear()
        st, ans = start()
        wait_state(ans["id"], ("running",))
        self.now[0] += o1gen.IDLE_CANCEL_S - 1
        gw.gen.sweep()
        time.sleep(0.2)
        self.assertFalse(G["comfy"].posted("/interrupt"))
        self.now[0] += 2
        gw.gen.sweep()
        G["comfy"].hold.set()
        wait_idle()
        self.assertTrue(G["comfy"].posted("/interrupt"))
        self.assertEqual(gw.activity.inflight, 0)
        self.assertEqual(gw.gen.jobs, {})

    def test_polling_keeps_a_running_job_alive(self):
        gw = G["gw"]
        G["comfy"].hold.clear()
        st, ans = start()
        wait_state(ans["id"], ("running",))
        for _ in range(5):
            self.now[0] += 100
            call("GET", "/v1/generate/jobs/" + ans["id"])
            gw.gen.sweep()
        self.assertFalse(G["comfy"].posted("/interrupt"))
        G["comfy"].hold.set()
        wait_state(ans["id"], ("done",))


class TestVram(Base):
    def test_it_waits_for_the_card_to_look_free(self):
        gw = G["gw"]
        readings = [(14 * GIB, 16 * GIB), (9 * GIB, 16 * GIB), (1 * GIB, 16 * GIB)]
        asked = []

        def vram():
            asked.append(time.time())
            return readings.pop(0) if len(readings) > 1 else readings[0]
        real = gw.gen.hooks.vram
        gw.gen.hooks.vram = vram
        gw.gen.vram_poll_s = 0.02
        seen = []
        G["comfy"].on_prompt = lambda g: seen.append(len(asked))
        try:
            st, ans = start()
            wait_state(ans["id"], ("done",))
        finally:
            gw.gen.hooks.vram = real
        self.assertEqual(seen, [3])                 # the graph went in only after the third reading

    def test_an_unreadable_card_does_not_block(self):
        gw = G["gw"]
        real = gw.gen.hooks.vram
        gw.gen.hooks.vram = lambda: (None, None)
        try:
            st, ans = start()
            wait_state(ans["id"], ("done",), timeout=5)
        finally:
            gw.gen.hooks.vram = real

    def test_a_card_that_never_frees_is_not_waited_on_for_ever(self):
        gw = G["gw"]
        real, old = gw.gen.hooks.vram, o1gen.VRAM_WAIT_S
        gw.gen.hooks.vram = lambda: (15 * GIB, 16 * GIB)
        gw.gen.vram_poll_s = 0.02
        o1gen.VRAM_WAIT_S = 0.3
        try:
            st, ans = start()
            wait_state(ans["id"], ("done",), timeout=5)
        finally:
            gw.gen.hooks.vram = real
            o1gen.VRAM_WAIT_S = old


class TestNothingKept(Base):
    def test_the_prompt_reaches_no_log_and_no_file(self):
        marker = "MK" + uuid.uuid4().hex[:12].upper()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for kind in ("image", "video"):
                st, ans = start(kind, prompt="a painting of " + marker)
                wait_state(ans["id"], ("done",))
                call("GET", "/v1/generate/jobs/%s/result" % ans["id"])
                wait_idle()
            G["comfy"].fail = True
            st, ans = start(prompt="again " + marker)
            wait_state(ans["id"], ("failed",))
            wait_idle()
            st, bad = start(prompt=marker, width=7)
            self.assertEqual(st, 400)
            self.assertNotIn(marker, json.dumps(bad))
            time.sleep(1.2)
        printed = out.getvalue()
        self.assertIn("gen ", printed)
        self.assertNotIn(marker, printed)
        for root, _dirs, files in os.walk(U.PREFIX):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    blob = open(fp, "rb").read()
                except OSError:
                    continue
                self.assertNotIn(marker.encode(), blob, fp)
        snap = json.load(open(os.path.join(U.PREFIX, "run/ollama1/stats/gateway.json")))
        self.assertNotIn(marker, json.dumps(snap))

    def test_no_path_or_internal_address_in_any_answer(self):
        G["comfy"].fail = True
        st, ans = start()
        s = wait_state(ans["id"], ("failed",))
        blob = json.dumps(s) + json.dumps(js(call("GET", "/v1/generate/capabilities")))
        G["gw"].gen.caps_at = None
        G["comfy"].files["CheckpointLoaderSimple/ckpt_name"] = []
        st, refused = start()
        blob += json.dumps(refused)
        for bad in ("/home", "/srv", "/opt", "127.0.0.1", "8188", "comfy", "safetensors", "Traceback"):
            self.assertNotIn(bad.lower(), blob.lower(), bad)


class TestSource(unittest.TestCase):
    def test_o1gen_writes_no_file_and_prints_nothing(self):
        src = open(os.path.join(U.LIB, "o1gen.py")).read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                self.assertNotIn(name, ("print", "write_text", "write_bytes", "mkstemp", "NamedTemporaryFile",
                                        "makedirs", "mkdir", "system", "Popen", "run", "check_output"), name)
                if name in ("open", "fdopen"):
                    self.fail("o1gen opens a file at line %d" % node.lineno)
        self.assertNotIn("import logging", src)
        self.assertNotIn("subprocess", src)

    def test_log_calls_carry_only_counts_and_names(self):
        tree = ast.parse(open(os.path.join(U.LIB, "o1gen.py")).read())
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "log":
                n += 1
                self.assertIsInstance(node.args[0], ast.Constant)
                self.assertEqual(len(node.args), 1)
                for kw in node.keywords:
                    self.assertIn(kw.arg, {"kind", "status", "reason", "ms"})
                    for sub in ast.walk(kw.value):
                        if isinstance(sub, ast.Name):
                            self.assertNotIn(sub.id, ("params", "prompt", "wf", "data", "ans", "entry", "obj"))
                        if isinstance(sub, ast.Subscript):
                            self.fail("a log field is built from an index at line %d" % node.lineno)
        self.assertGreaterEqual(n, 3)

    def test_the_gateway_names_comfyui_only_on_loopback_by_default(self):
        from o1common import DEFAULTS as D
        self.assertEqual(D["comfyui_url"], "http://127.0.0.1:8188")


class TestInstallScript(unittest.TestCase):
    """tools/install-comfyui.sh: the part that can run on a Mac (it prints the service files) and
    static checks of the rest. The install itself needs a Linux server and 27 GB* of downloads."""
    SCRIPT = os.path.join(U.TOOLS, "install-comfyui.sh")

    def run_script(self, *args, env=None):
        import subprocess
        e = dict(os.environ)
        e.pop("SUDO_USER", None)
        e.pop("COMFYUI_USER", None)
        e.update(env or {})
        return subprocess.run(["bash", self.SCRIPT] + list(args), capture_output=True, text=True, env=e, timeout=30)

    def test_it_is_valid_bash(self):
        import subprocess
        self.assertEqual(subprocess.run(["bash", "-n", self.SCRIPT]).returncode, 0)

    def test_the_service_listens_on_loopback_only_as_the_named_user(self):
        r = self.run_script("--print-unit", "--user", "sam")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = r.stdout
        for want in ("User=sam", "SupplementaryGroups=render video", "WorkingDirectory=/home/sam/comfyui",
                     "--listen 127.0.0.1 --port 8188", "--disable-metadata", "--output-directory /run/comfyui/out",
                     "--temp-directory /run/comfyui/tmp", "RuntimeDirectory=comfyui", "RuntimeDirectoryMode=0700",
                     "NoNewPrivileges=yes", "-mmin +15 -delete", "OnUnitActiveSec=10min",
                     "$COMFYUI_EXTRA_ARGS"):
            self.assertIn(want, t)
        self.assertNotIn("0.0.0.0", t)
        self.assertEqual(len(re.findall(r"--listen (\S+)", t)), 1)
        self.assertNotIn("User=root", t)
        self.assertNotIn("sam", t.replace("User=sam", "").replace("/home/sam", ""))

    def test_it_will_not_run_as_root_or_as_a_strange_login(self):
        for bad in ("root", "a b", "$(id)", "x;y", "-n", "A"):
            r = self.run_script("--print-unit", "--user", bad)
            self.assertNotEqual(r.returncode, 0, repr(bad))
        self.assertNotEqual(self.run_script("--bogus").returncode, 0)

    def test_it_names_the_same_four_files_the_gateway_uses(self):
        src = open(self.SCRIPT).read()
        for name in (o1gen.IMAGE_CKPT, o1gen.WAN_UNET, o1gen.WAN_CLIP, o1gen.WAN_VAE):
            self.assertEqual(src.count("|" + name + "|"), 1, name)
        for folder, name in (("checkpoints", o1gen.IMAGE_CKPT), ("diffusion_models", o1gen.WAN_UNET),
                             ("text_encoders", o1gen.WAN_CLIP), ("vae", o1gen.WAN_VAE)):
            self.assertIn('"%s|%s|' % (folder, name), src)
        for url in re.findall(r"\|(https://[^|]+)\|", src):
            self.assertTrue(url.startswith("https://huggingface.co/") or "$HF/" in url, url)
        self.assertIn("curl -fL -C -", src)
        # a download is kept under another name until it is long enough, then renamed
        a = src.index('[ "$size" -ge "$4" ] || die "$2 came to only')
        self.assertLess(src.index('-o "$dest.part"'), a)
        self.assertLess(a, src.index('as_user mv "$dest.part" "$dest"'))
        self.assertIn('size=$(stat -c %s "$dest" 2>/dev/null || echo 0)\n  if [ "$size" -ge "$4" ]; then', src)

    def test_the_steps_that_were_done_by_hand(self):
        src = open(self.SCRIPT).read()
        for want in ("astral.sh/uv/install.sh", "uv venv --python 3.12", "download.pytorch.org/whl/rocm6.4",
                     "torch==2.9.1", "requirements.txt", "git clone", "comfyanonymous/ComfyUI",
                     "systemctl enable comfyui.service"):
            self.assertIn(want, src)
        self.assertNotIn("chmod 777", src)
        self.assertNotIn("--listen 0.0.0.0", src)

    def test_setup_only_mentions_it_and_never_runs_it(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        uses = [l for l in setup.splitlines() if "install-comfyui" in l]
        self.assertEqual(len(uses), 1)
        self.assertIn("note ", uses[0])

    def test_the_port_guard_lets_only_root_and_the_gateway_reach_comfyui(self):
        nft = open(os.path.join(U.CONFIG, "ollama1.nft.in")).read()
        rules = [l.strip() for l in nft.splitlines() if "dport 8188" in l]
        self.assertEqual(len(rules), 1)
        self.assertIn("meta skuid != { 0, @UID_O1GW@ }", rules[0])
        self.assertIn("fib daddr type local", rules[0])
        self.assertIn("reject with tcp reset", rules[0])
        self.assertNotIn("@UID_OLLAMA@", rules[0])
        self.assertNotIn("@UID_CLOUDFLARED@", rules[0])

    def test_the_documents_describe_the_routes(self):
        proto = open(os.path.join(U.KIT, "PROTOCOL.md")).read()
        guide = open(os.path.join(os.path.dirname(U.KIT), "docs", "your-own-server.md")).read()
        for route in ("GET /v1/generate/capabilities", "POST /v1/generate/jobs", "GET /v1/generate/jobs/<id>",
                      "GET /v1/generate/jobs/<id>/result", "DELETE /v1/generate/jobs/<id>"):
            self.assertIn(route, proto)
        self.assertIn("install-comfyui.sh", guide)
        self.assertIn("Estimates", guide)                      # the sizes carry their asterisk and its footnote


if __name__ == "__main__":
    unittest.main()
