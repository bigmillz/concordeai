"""The Ollama updater installs only what matches the release's published
checksums; on any mismatch it stays at the current version."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import o1test_util as U

OPT = os.path.join(U.PREFIX, "opt/ollama")
STATUS = os.path.join(U.PREFIX, "var/lib/ollama1/ollama-update.json")


def tgz(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data, mode in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class Release:
    def __init__(self):
        self.files = {}
        self.release = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/release":
                    raw = json.dumps(outer.release).encode()
                elif self.path.startswith("/dl/") and self.path[4:] in outer.files:
                    raw = outer.files[self.path[4:]]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.port = U.free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def publish(self, tag, files, sums=None, digests=None, with_sums=True):
        self.files = dict(files)
        sums = sums or {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}
        if with_sums:
            self.files["sha256sum.txt"] = "".join("%s  ./%s\n" % (h, n) for n, h in sums.items()).encode()
        assets = []
        for n, b in self.files.items():
            a = {"name": n, "size": len(b), "browser_download_url": "http://127.0.0.1:%d/dl/%s" % (self.port, n)}
            if digests and n in digests:
                a["digest"] = digests[n]
            assets.append(a)
        self.release = {"tag_name": tag, "prerelease": False, "draft": False, "assets": assets}


def good_files(version):
    base = tgz([("bin/ollama", b"#!/bin/sh\necho ollama " + version.encode() + b"\n", 0o755),
                ("lib/ollama/libggml-base.so", b"base", 0o644)])
    rocm = tgz([("lib/ollama/rocm/libggml-hip.so", b"rocm", 0o644)])
    return {"ollama-linux-amd64.tgz": base, "ollama-linux-amd64-rocm.tgz": rocm}


class TestUpdater(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rel = Release()

    @classmethod
    def tearDownClass(cls):
        cls.rel.srv.shutdown()

    def setUp(self):
        shutil.rmtree(OPT, ignore_errors=True)
        os.makedirs(OPT)
        # "installed": v0.1.0
        self.rel.publish("v0.1.0", good_files("0.1.0"))
        self.assertEqual(self.run_updater(), 0)
        self.assertEqual(self.current(), "v0.1.0")

    def run_updater(self, *extra):
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-update-ollama"),
                            "--no-restart", "--api", "http://127.0.0.1:%d/release" % self.rel.port] + list(extra),
                           env=env, capture_output=True, text=True, timeout=60)
        self.out = r.stdout + r.stderr
        return r.returncode

    def current(self):
        return os.path.basename(os.readlink(os.path.join(OPT, "current")))

    def status(self):
        return json.load(open(STATUS))

    def test_good_update(self):
        self.rel.publish("v0.2.0", good_files("0.2.0"))
        self.assertEqual(self.run_updater(), 0, self.out)
        self.assertEqual(self.current(), "v0.2.0")
        root = os.path.join(OPT, "versions/v0.2.0")
        self.assertTrue(os.path.exists(os.path.join(root, "lib/ollama/rocm/libggml-hip.so")))
        self.assertEqual(self.status()["result"], "updated")
        self.assertTrue(os.path.isdir(os.path.join(OPT, "versions/v0.1.0")))  # kept for rollback

    def test_already_current(self):
        self.assertEqual(self.run_updater(), 0)
        self.assertEqual(self.status()["result"], "current")

    def test_checksum_mismatch_keeps_current(self):
        files = good_files("0.2.0")
        sums = {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}
        files["ollama-linux-amd64-rocm.tgz"] += b"tampered"
        self.rel.publish("v0.2.0", files, sums=sums)
        self.assertEqual(self.run_updater(), 1)
        self.assertIn("checksum mismatch", self.out)
        self.assertEqual(self.current(), "v0.1.0")
        self.assertFalse(os.path.exists(os.path.join(OPT, "versions/v0.2.0")))
        self.assertEqual(self.status()["result"], "failed")

    def test_missing_checksum_file(self):
        self.rel.publish("v0.2.0", good_files("0.2.0"), with_sums=False)
        self.assertEqual(self.run_updater(), 1)
        self.assertEqual(self.current(), "v0.1.0")

    def test_asset_not_in_sums(self):
        files = good_files("0.2.0")
        self.rel.publish("v0.2.0", files, sums={"ollama-linux-amd64.tgz": hashlib.sha256(files["ollama-linux-amd64.tgz"]).hexdigest()})
        self.assertEqual(self.run_updater(), 1)
        self.assertIn("is not in sha256sum.txt", self.out)
        self.assertEqual(self.current(), "v0.1.0")

    def test_github_digest_disagrees(self):
        files = good_files("0.2.0")
        self.rel.publish("v0.2.0", files, digests={"ollama-linux-amd64.tgz": "sha256:" + "0" * 64})
        self.assertEqual(self.run_updater(), 1)
        self.assertIn("disagree", self.out)
        self.assertEqual(self.current(), "v0.1.0")

    def test_path_traversal_refused(self):
        files = good_files("0.2.0")
        files["ollama-linux-amd64-rocm.tgz"] = tgz([("../../evil", b"x", 0o644)])
        self.rel.publish("v0.2.0", files)
        self.assertEqual(self.run_updater(), 1)
        self.assertEqual(self.current(), "v0.1.0")
        self.assertFalse(os.path.exists(os.path.join(OPT, "evil")))

    def test_rocm_component_required(self):
        files = good_files("0.2.0")
        del files["ollama-linux-amd64-rocm.tgz"]
        self.rel.publish("v0.2.0", files)
        self.assertEqual(self.run_updater(), 1)
        self.assertEqual(self.current(), "v0.1.0")

    def test_no_rocm_for_nvidia_or_cpu(self):
        files = good_files("0.2.0")
        del files["ollama-linux-amd64-rocm.tgz"]
        self.rel.publish("v0.2.0", files)
        self.assertEqual(self.run_updater("--no-rocm"), 0, self.out)
        self.assertEqual(self.current(), "v0.2.0")

    def test_odd_tag_refused(self):
        self.rel.publish("../v9", good_files("9"))
        self.assertEqual(self.run_updater(), 1)
        self.assertEqual(self.current(), "v0.1.0")

    @unittest.skipUnless(sys.version_info >= (3, 14), "needs compression.zstd")
    def test_zst_archives(self):
        from compression import zstd
        files = {n.replace(".tgz", ".tar.zst"): zstd.compress(tarfile_raw(b)) for n, b in good_files("0.3.0").items()}
        self.rel.publish("v0.3.0", files)
        self.assertEqual(self.run_updater(), 0, self.out)
        self.assertEqual(self.current(), "v0.3.0")


def tarfile_raw(tgz_bytes):
    import gzip
    return gzip.decompress(tgz_bytes)


if __name__ == "__main__":
    unittest.main()
