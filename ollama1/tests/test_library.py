"""The model library sync (ollama1-models): the preview is exactly what a
sync does, removal only ever touches installed models that are NOT on the
allow-list, updates are found from the registry's manifest digest, and
retries, progress and the allow-list edits behave. Uses the stub Ollama and
a fake registry."""
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import o1test_util as U
import o1library as L
import o1sleep
from o1common import Paths
from stub_ollama import Stub, llama_info

GIB = 1 << 30


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def manifest_for(name, version="v1", size=1000):
    """A registry manifest (bytes) for a model; its layer digests are derived
    from the name and version, so a new version has a new weights layer."""
    m = {"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
         "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                    "digest": "sha256:" + sha(name + "cfg"), "size": 100},
         "layers": [{"mediaType": "application/vnd.ollama.image.model",
                     "digest": "sha256:" + sha(name + version), "size": size},
                    {"mediaType": "application/vnd.ollama.image.license",
                     "digest": "sha256:" + sha("shared-license"), "size": 10}]}
    return json.dumps(m, indent=1).encode()


class FakeRegistry:
    def __init__(self):
        self.manifests = {}   # (repo, tag) -> bytes
        self.calls = []
        reg = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                reg.calls.append(("GET", self.path, self.headers.get("Accept")))
                parts = self.path.split("/manifests/")
                key = (parts[0][len("/v2/"):], parts[1]) if len(parts) == 2 else None
                raw = reg.manifests.get(key)
                if raw is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.docker.distribution.manifest.v2+json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_HEAD(self):
                reg.calls.append(("HEAD", self.path, None))
                self.send_response(405)
                self.end_headers()

            def do_POST(self):
                reg.calls.append(("POST", self.path, None))
                self.send_response(405)
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", U.free_port()), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def publish(self, name, version="v1", size=1000):
        host, repo, tag = L.split_name(name)
        raw = manifest_for(name, version, size)
        self.manifests[(repo, tag)] = raw
        return hashlib.sha256(raw).hexdigest()

    def close(self):
        self.srv.shutdown()


R = {}


def setUpModule():
    R["reg"] = FakeRegistry()
    os.environ["OLLAMA1_REGISTRY"] = R["reg"].url
    R["stub"] = Stub(U.free_port(), models={})
    R["base"] = "http://127.0.0.1:%d" % R["stub"].port


def tearDownModule():
    R["reg"].close()
    R["stub"].close()
    os.environ.pop("OLLAMA1_REGISTRY", None)


class Base(unittest.TestCase):
    def setUp(self):
        self.stub, self.reg, self.base = R["stub"], R["reg"], R["base"]
        self.stub.models.clear()
        self.stub.calls.clear()
        self.stub.pull_layers.clear()
        self.stub.pull_digest.clear()
        self.stub.pull_fail = 0
        self.reg.manifests.clear()
        self.reg.calls.clear()
        self.dir = tempfile.mkdtemp(prefix="o1lib-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.allow = os.path.join(self.dir, "models.allow")
        blobs = os.path.join(Paths.models, "blobs")
        shutil.rmtree(blobs, True)
        os.makedirs(blobs)

    def write_allow(self, text):
        with open(self.allow, "w") as f:
            f.write(text)

    def install(self, name, version="v1", size=1000, blobs=True):
        """An installed model whose digest matches that version's manifest."""
        raw = manifest_for(name, version, size)
        self.stub.models[name] = {"size": size + 110, "info": llama_info(), "digest": hashlib.sha256(raw).hexdigest()}
        if blobs:
            for d in L.layers(json.loads(raw)):
                open(os.path.join(Paths.models, "blobs", d.replace(":", "-")), "w").close()

    def plan(self):
        return L.plan(base=self.base, allow_path=self.allow)

    def run_plan(self, p, **kw):
        return L.execute(p, base=self.base, allow_path=self.allow, sleep=lambda s: None, **kw)

    def deleted(self):
        return [c[2]["model"] for c in self.stub.calls if c[0] == "DELETE"]

    def pulled(self):
        return [c[2]["model"] for c in self.stub.calls if c[0] == "POST" and c[1] == "/api/pull"]


class TestPlan(Base):
    def test_the_four_kinds(self):
        self.write_allow("# my models\nkeep:1b\nold:7b   ram\nnew:4b\nnowhere:1b\n")
        for n in ("keep:1b", "old:7b", "new:4b"):
            self.reg.publish(n, "v2" if n == "old:7b" else "v1", size=5000)
        self.install("keep:1b", size=5000)
        self.install("old:7b", "v1", size=5000)
        self.install("extra:3b")
        self.install("handmade:latest")
        p = self.plan()
        self.assertEqual(sorted(x["name"] for x in p["remove"]), ["extra:3b", "handmade:latest"])
        self.assertEqual([x["name"] for x in p["download"]], ["new:4b"])
        self.assertEqual([x["name"] for x in p["update"]], ["old:7b"])
        self.assertEqual([x["name"] for x in p["unknown"]], ["nowhere:1b"])
        # the new layer only: the license and config blobs are already here
        self.assertEqual([x["bytes"] for x in p["update"]], [5000])
        self.assertEqual([x["bytes"] for x in p["download"]], [5000 + 100])
        self.assertEqual(p["totals"]["freed_bytes"], 2 * 1110)
        self.assertEqual(p["totals"]["free_after"],
                         p["totals"]["free_now"] + 2220 - 5000 - 5100)
        self.assertTrue(p["enough_space"])

    def test_cloud_stubs_never_removed(self):
        stub = Stub(U.free_port())      # the default models include two cloud entries
        try:
            self.write_allow("small:8b\n")
            p = L.plan(base="http://127.0.0.1:%d" % stub.port, allow_path=self.allow,
                       manifest_fn=lambda n: (None, None))
            names = [x["name"] for x in p["remove"]]
            self.assertNotIn("gpt-oss:120b-cloud", names)
            self.assertNotIn("plain:latest", names)
            self.assertIn("huge:70b", names)
        finally:
            stub.close()

    def test_update_found_by_digest_without_downloading(self):
        self.write_allow("same:1b\n")
        self.install("same:1b")
        self.reg.publish("same:1b")
        self.assertEqual(self.plan()["update"], [])
        self.reg.publish("same:1b", "v2")
        self.assertEqual([x["name"] for x in self.plan()["update"]], ["same:1b"])
        # only manifests were fetched, with the manifest media type; no pull
        self.assertTrue(all("/manifests/" in c[1] for c in self.reg.calls))
        self.assertTrue(all(c[2] == L.MANIFEST_ACCEPT for c in self.reg.calls))
        self.assertEqual(self.pulled(), [])

    def test_latest_and_namespaces(self):
        self.assertEqual(L.split_name("qwen3"), ("registry.ollama.ai", "library/qwen3", "latest"))
        self.assertEqual(L.split_name("qwen3:14b"), ("registry.ollama.ai", "library/qwen3", "14b"))
        self.assertEqual(L.split_name("me/tool:q4"), ("registry.ollama.ai", "me/tool", "q4"))
        self.assertEqual(L.split_name("hf.co/org/m:Q4"), ("hf.co", "org/m", "Q4"))
        # 'x' on the list covers an installed 'x:latest'
        self.write_allow("plainname\n")
        self.install("plainname:latest")
        self.reg.publish("plainname:latest")      # the same manifest, under 'plainname'
        self.assertEqual(self.reg.manifests.keys(), {("library/plainname", "latest")})
        p = self.plan()
        self.assertEqual((p["remove"], p["download"], p["update"]), ([], [], []))

    def test_registry_down_is_unknown_not_removed(self):
        self.write_allow("a:1b\nb:1b\n")
        self.install("a:1b")
        p = L.plan(base=self.base, allow_path=self.allow, manifest_fn=lambda n: (None, None))
        self.assertEqual(p["remove"], [])
        self.assertEqual(p["download"], [])
        self.assertEqual(sorted(x["name"] for x in p["unknown"]), ["a:1b", "b:1b"])

    def test_bad_lines_ignored_and_reported(self):
        self.write_allow("ok:1b\nbad name here\nx:1b gpu\n")
        self.reg.publish("ok:1b")
        p = self.plan()
        self.assertEqual([x["name"] for x in p["download"]], ["ok:1b"])
        self.assertEqual(len(p["allow_errors"]), 2)

    def test_not_enough_space(self):
        self.write_allow("big:1b\n")
        self.reg.publish("big:1b", size=L.disk_free() + GIB)
        self.assertFalse(self.plan()["enough_space"])

    def test_plan_id_is_what_it_does(self):
        self.write_allow("a:1b\n")
        self.reg.publish("a:1b")
        self.install("z:1b")
        p1 = self.plan()
        self.assertEqual(p1["id"], self.plan()["id"])
        self.write_allow("a:1b\nz:1b\n")
        self.reg.publish("z:1b")
        self.assertNotEqual(p1["id"], self.plan()["id"])


class TestExecute(Base):
    def test_does_exactly_the_preview(self):
        self.write_allow("keep:1b\nold:7b\nnew:4b\n")
        self.reg.publish("keep:1b")
        self.reg.publish("old:7b", "v2")
        self.reg.publish("new:4b")
        self.install("keep:1b")
        self.install("old:7b")
        self.install("extra:3b")
        p = self.plan()
        s = self.run_plan(p)
        self.assertEqual(sorted(self.deleted()), sorted(x["name"] for x in p["remove"]))
        self.assertEqual(sorted(self.pulled()), sorted(x["name"] for x in p["download"] + p["update"]))
        self.assertEqual(s["removed"], ["extra:3b"])
        self.assertEqual(s["downloaded"], ["new:4b"])
        self.assertEqual(s["updated"], ["old:7b"])
        self.assertEqual(s["failed"], [])
        # removals go first, so their space is there for the downloads
        kinds = [c[1] for c in self.stub.calls if c[0] in ("DELETE", "POST")]
        self.assertEqual(kinds[0], "/api/delete")

    def test_nothing_unlisted_is_ever_removed(self):
        """Random libraries and allow-lists (with and without :latest, bad
        lines, comments): after a sync, every deleted model was installed
        and not on the list, every listed installed model is still there,
        and exactly the unlisted ones are gone."""
        rnd = random.Random(6333)
        pool = ["m%d:%s" % (i, t) for i in range(8) for t in ("1b", "latest")] + ["plain%d" % i for i in range(4)]
        for case in range(60):
            self.setUp()
            installed = rnd.sample(pool, rnd.randint(0, 8))
            listed = rnd.sample(pool, rnd.randint(0, 8))
            lines = ["# case %d" % case]
            for n in listed:
                lines.append(n + (" ram" if rnd.random() < 0.3 else ""))
                if rnd.random() < 0.2:
                    lines.append(n + " typo-flag")          # ignored line
            self.write_allow("\n".join(lines) + "\n")
            full = [n if ":" in n else n + ":latest" for n in installed]
            for n in pool:
                self.reg.publish(n if ":" in n else n + ":latest", rnd.choice(["v1", "v2"]))
            for n in full:
                self.install(n, rnd.choice(["v1", "v2"]))
            valid = L.allowed_names(self.allow)
            p = self.plan()
            self.run_plan(p)
            gone = self.deleted()
            for n in gone:
                self.assertIn(n, full, case)
                self.assertFalse(L.listed(n, valid), (case, n))
            self.assertEqual(sorted(gone), sorted(n for n in full if not L.listed(n, valid)), case)
            for n in full:
                if L.listed(n, valid):
                    self.assertIn(n, self.stub.models, (case, n))
            self.assertEqual(sorted(gone), sorted(x["name"] for x in p["remove"]), case)

    def test_put_back_on_the_list_after_the_preview_is_kept(self):
        self.write_allow("a:1b\n")
        self.reg.publish("a:1b")
        self.install("a:1b")
        self.install("z:1b")
        p = self.plan()
        self.assertEqual([x["name"] for x in p["remove"]], ["z:1b"])
        self.write_allow("a:1b\nz:1b\n")
        s = self.run_plan(p)
        self.assertEqual(self.deleted(), [])
        self.assertEqual(s["skipped"], ["z:1b"])
        self.assertIn("z:1b", self.stub.models)

    def test_taken_off_the_list_after_the_preview_is_not_fetched(self):
        self.write_allow("a:1b\n")
        self.reg.publish("a:1b")
        p = self.plan()
        self.write_allow("")
        s = self.run_plan(p)
        self.assertEqual(self.pulled(), [])
        self.assertEqual(s["skipped"], ["a:1b"])

    def test_three_tries(self):
        self.write_allow("a:1b\n")
        self.reg.publish("a:1b")
        self.stub.pull_fail = 2
        naps = []
        s = L.execute(self.plan(), base=self.base, allow_path=self.allow, sleep=naps.append)
        self.assertEqual(s["downloaded"], ["a:1b"])
        self.assertEqual(len(self.pulled()), 3)
        self.assertEqual(len(naps), 2)
        self.stub.calls.clear()
        self.stub.models.clear()
        self.stub.pull_fail = 3
        s = self.run_plan(self.plan())
        self.assertEqual(len(self.pulled()), 3)
        self.assertEqual([f["name"] for f in s["failed"]], ["a:1b"])
        self.assertIn("connection reset", s["failed"][0]["error"])

    def test_progress_ignores_layers_already_here(self):
        seen = []
        self.stub.pull_layers["a:1b"] = [("sha256:" + "1" * 64, 900, True), ("sha256:" + "2" * 64, 400, False)]
        L.pull_one("a:1b", lambda d, t: seen.append((d, t)), base=self.base)
        self.assertTrue(seen)
        self.assertTrue(all(t == 400 for _, t in seen), seen)
        self.assertEqual(seen[-1], (400, 400))
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_report_events(self):
        self.write_allow("a:1b\nb:1b\n")
        self.reg.publish("a:1b", size=3000)
        self.reg.publish("b:1b", size=1000)
        evs = []
        self.run_plan(self.plan(), report=evs.append)
        pulls = [e for e in evs if e["phase"] == "download"]
        self.assertEqual({e["count"] for e in pulls}, {2})
        self.assertEqual([e["index"] for e in pulls], sorted(e["index"] for e in pulls))
        overall = [e["overall"] for e in pulls]
        self.assertEqual(overall, sorted(overall))
        self.assertLessEqual(max(overall), 1.0)


class TestProgress(unittest.TestCase):
    def test_eta_text(self):
        self.assertEqual(L.eta_text(None), "")
        self.assertEqual(L.eta_text(30), "less than a minute left")
        self.assertEqual(L.eta_text(61), "about 2 min left")
        self.assertEqual(L.eta_text(600), "about 10 min left")

    def test_smoothed_rate(self):
        t = [0.0]
        p = L.Progress(1000, clock=lambda: t[0])
        for i in range(1, 11):
            t[0] = float(i)
            p.update(10 * i)             # 10 B/s
        self.assertAlmostEqual(p.rate, 10.0, places=6)
        self.assertAlmostEqual(p.eta(), 90.0, places=6)
        t[0] = 11.0
        p.update(10 * 10 + 100)          # one fast second moves it only a little
        self.assertLess(p.rate, 30)
        self.assertGreater(p.rate, 10)
        p.finish_model(200)
        self.assertAlmostEqual(p.fraction(), 0.2)

    def test_empty(self):
        p = L.Progress(0)
        self.assertEqual(p.fraction(), 1.0)
        self.assertIsNone(p.eta())


class TestAllowEdits(Base):
    def test_add_keeps_comments_and_flags(self):
        self.write_allow("# top\nqwen3:14b  # fast one\n")
        self.assertEqual(L.allow_add("gpt-oss:120b", ram=True, allow_path=self.allow), "added")
        text = open(self.allow).read()
        self.assertIn("# top", text)
        self.assertIn("# fast one", text)
        self.assertEqual(L.allowed_names(self.allow), ["qwen3:14b", "gpt-oss:120b"])
        self.assertEqual(L.allow_add("gpt-oss:120b", ram=True, allow_path=self.allow), "already on the list")
        self.assertEqual(L.allow_add("gpt-oss:120b", ram=False, allow_path=self.allow), "flag changed")
        from o1common import ram_models
        self.assertEqual(ram_models(self.allow), set())
        self.assertEqual(oct(os.stat(self.allow).st_mode & 0o777), "0o644")

    def test_add_refuses_bad_names(self):
        self.write_allow("")
        for bad in ("", "a b", "../x", "x:1b ram", "gpt-oss:120b-cloud", "-x", "x" * 200):
            with self.assertRaises(ValueError, msg=bad):
                L.allow_add(bad, allow_path=self.allow)
        self.assertEqual(open(self.allow).read(), "")

    def test_a_refused_line_is_never_left_behind(self):
        self.write_allow("a:1b\n")
        real = L.valid_model_name
        L.valid_model_name = lambda n: True      # pretend the first check let it through
        try:
            with self.assertRaises(ValueError):
                L.allow_add("b:1b turbo", allow_path=self.allow)
        finally:
            L.valid_model_name = real
        self.assertEqual(open(self.allow).read(), "a:1b\n")

    def test_remove_only_that_model(self):
        self.write_allow("# keep me\na:1b\nb  ram\nab:1b\n")
        self.assertTrue(L.allow_remove("b:latest", allow_path=self.allow))
        self.assertEqual(L.allowed_names(self.allow), ["a:1b", "ab:1b"])
        self.assertFalse(L.allow_remove("zzz", allow_path=self.allow))
        self.assertIn("# keep me", open(self.allow).read())


class TestCLI(unittest.TestCase):
    """bin/ollama1-models end to end, in its own prefix."""

    def setUp(self):
        self.stub = Stub(U.free_port(), models={})
        self.reg = FakeRegistry()
        self.pre = tempfile.mkdtemp(prefix="o1cli-")
        self.addCleanup(shutil.rmtree, self.pre, True)
        self.addCleanup(self.stub.close)
        self.addCleanup(self.reg.close)
        for d in ("etc/ollama1", "run/ollama1/library", "srv/models/blobs"):
            os.makedirs(os.path.join(self.pre, d))
        with open(os.path.join(self.pre, "etc/ollama1/config.json"), "w") as f:
            json.dump({"ollama_url": "http://127.0.0.1:%d" % self.stub.port}, f)
        self.allow = os.path.join(self.pre, "etc/ollama1/models.allow")
        with open(self.allow, "w") as f:
            f.write("keep:1b\nnew:4b\n")
        for n in ("keep:1b", "new:4b"):
            # once pulled, Ollama lists the sha256 of the registry's manifest as the digest
            self.stub.pull_digest[n] = self.reg.publish(n)
        raw = manifest_for("keep:1b")
        self.stub.models["keep:1b"] = {"size": 1110, "info": llama_info(), "digest": hashlib.sha256(raw).hexdigest()}
        self.stub.models["extra:3b"] = {"size": 1110, "info": llama_info()}

    def run_cli(self, *args, stdin=""):
        env = dict(os.environ, OLLAMA1_PREFIX=self.pre, OLLAMA1_REGISTRY=self.reg.url)
        return subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-models")] + list(args),
                              input=stdin, capture_output=True, text=True, env=env, timeout=60)

    def deleted(self):
        return [c[2]["model"] for c in self.stub.calls if c[0] == "DELETE"]

    def test_status_changes_nothing(self):
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Remove", r.stdout)
        self.assertIn("extra:3b", r.stdout)
        self.assertIn("Download", r.stdout)
        self.assertIn("new:4b", r.stdout)
        self.assertNotIn("keep:1b", r.stdout)
        self.assertIn("Disk free now", r.stdout)
        self.assertEqual(self.deleted(), [])
        self.assertFalse([c for c in self.stub.calls if c[1] == "/api/pull"])

    def test_sync_needs_yes(self):
        for answer in ("", "no", "y", "YES please"):
            r = self.run_cli("sync", stdin=answer + "\n")
            self.assertEqual(r.returncode, 1)
            self.assertIn("Nothing changed", r.stdout)
        self.assertEqual(self.deleted(), [])
        r = self.run_cli("sync", stdin="yes\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.deleted(), ["extra:3b"])
        self.assertIn("Removed 1, downloaded 1, updated 0, failed 0", r.stdout)
        self.assertIn("new:4b", self.stub.models)
        r = self.run_cli("status")
        self.assertIn("matches the allow-list", r.stdout)

    def test_panel_sync_runs_only_the_preview(self):
        r = self.run_cli("status", "--panel")
        self.assertEqual(r.returncode, 0, r.stderr)
        prev = json.load(open(os.path.join(self.pre, "run/ollama1/library/preview.json")))
        self.assertEqual([x["name"] for x in prev["remove"]], ["extra:3b"])
        # the allow-list changes after the preview: the sync refuses, changes nothing
        with open(self.allow, "a") as f:
            f.write("extra:3b\n")
        self.reg.publish("extra:3b")
        r = self.run_cli("sync", "--panel")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.deleted(), [])
        self.assertFalse([c for c in self.stub.calls if c[1] == "/api/pull"])
        st = json.load(open(os.path.join(self.pre, "run/ollama1/library/sync.json")))
        self.assertEqual(st["state"], "changed")
        # the refusal wrote the new preview; syncing that one works
        r = self.run_cli("sync", "--panel")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.deleted(), [])
        st = json.load(open(os.path.join(self.pre, "run/ollama1/library/sync.json")))
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["summary"]["downloaded"], ["new:4b"])

    def test_panel_sync_without_preview_refuses(self):
        r = self.run_cli("sync", "--panel")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.deleted(), [])

    def test_add_and_remove(self):
        r = self.run_cli("add", "fresh:2b", "ram", stdin="n\n")
        self.assertIn("added", r.stdout)
        self.assertIn("doesn't know", r.stdout)      # not in the registry
        self.assertIn("fresh:2b   ram", open(self.allow).read())
        self.reg.publish("fresh:2b")
        r = self.run_cli("add", "fresh:2b", "ram", stdin="y\n")
        self.assertIn("already on the list", r.stdout)
        self.assertIn("downloaded 1", r.stdout)
        self.assertIn("fresh:2b", self.stub.models)
        r = self.run_cli("add", "bad name")
        self.assertNotEqual(r.returncode, 0)
        r = self.run_cli("add", "x:1b", "gpu")
        self.assertNotEqual(r.returncode, 0)
        r = self.run_cli("remove", "fresh:2b", stdin="no\n")
        self.assertIn("fresh:2b", open(self.allow).read())
        self.assertEqual(self.deleted(), [])
        r = self.run_cli("remove", "fresh:2b", stdin="yes\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("fresh:2b", open(self.allow).read())
        self.assertEqual(self.deleted(), ["fresh:2b"])
        r = self.run_cli("remove", "never:1b", stdin="yes\n")
        self.assertNotEqual(r.returncode, 0)

    def test_one_sync_at_a_time_and_sleep_knows(self):
        import fcntl
        lock = os.path.join(self.pre, "run/ollama1/library/sync.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            r = self.run_cli("sync", stdin="yes\n")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("another model-library sync is running", r.stderr)
            self.assertEqual(self.deleted(), [])
            self.assertTrue(o1sleep.setup_running(lock))
        finally:
            os.close(fd)
        self.assertFalse(o1sleep.setup_running(lock))

    def test_needs_root_outside_tests(self):
        env = {k: v for k, v in os.environ.items() if k != "OLLAMA1_PREFIX"}
        if os.geteuid() == 0:
            self.skipTest("running as root")
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-models"), "status"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertIn("sudo", r.stderr)


class TestSleepSeesLibraryLock(unittest.TestCase):
    def test_busy(self):
        import fcntl
        os.makedirs(L.library_dir(), exist_ok=True)
        with L.Lock():
            why = o1sleep.busy_reasons(active_units=lambda pats: [], setup_lock=os.path.join(U.PREFIX, "nolock"))
            self.assertEqual(why, ["the model library is being synced"])
        self.assertEqual(o1sleep.busy_reasons(active_units=lambda pats: [],
                                              setup_lock=os.path.join(U.PREFIX, "nolock")), [])
        # the unit and the lock together say it once
        why = None
        with L.Lock():
            why = o1sleep.busy_reasons(active_units=lambda pats: ["ollama1-models-sync.service"],
                                       setup_lock=os.path.join(U.PREFIX, "nolock"))
        self.assertEqual(why, ["the model library is being synced"])
        del fcntl


if __name__ == "__main__":
    unittest.main()
