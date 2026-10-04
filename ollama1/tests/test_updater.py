"""The Ollama updater installs only what matches the release's published
checksums; on any mismatch it stays at the current version."""
import errno
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import unittest
import urllib.error
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


def load_updater():
    import importlib.machinery
    import importlib.util
    path = os.path.join(U.BIN, "ollama1-update-ollama")
    spec = importlib.util.spec_from_loader("o1_update_ollama", importlib.machinery.SourceFileLoader("o1_update_ollama", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeNet:
    """A fake clock whose sleep advances it, and a resolver that works from `up_at` on."""

    def __init__(self, up_at=0):
        self.t, self.up_at, self.resolves, self.sleeps = 0.0, up_at, 0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def resolver(self, host, port):
        self.resolves += 1
        if self.t < self.up_at:
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        return [("x",)]


class TestNetworkWait(unittest.TestCase):
    def setUp(self):
        self.m = load_updater()
        self.net = FakeNet()
        self.statuses, self.lines = [], []

    def run_it(self, attempt, wait=True, cur="v0.35.1"):
        return self.m.run_with_retries(attempt, "api.github.com", cur, wait=wait, resolver=self.net.resolver,
                                       sleep=self.net.sleep, clock=self.net.clock,
                                       set_status=lambda *a: self.statuses.append(a), log=self.lines.append)

    def test_what_counts_as_no_network(self):
        m = self.m
        for e in (socket.gaierror(-3, "Temporary failure in name resolution"),
                  urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")),
                  urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable")),
                  urllib.error.URLError(ConnectionRefusedError(111, "refused")), socket.timeout("timed out"),
                  TimeoutError(), ConnectionResetError(104, "reset")):
            self.assertTrue(m.is_no_network(e), repr(e))
        for e in (urllib.error.HTTPError("http://x", 403, "rate limited", {}, None),
                  urllib.error.HTTPError("http://x", 500, "oops", {}, None), m.Refused("checksum"),
                  FileNotFoundError(2, "no such file"), PermissionError(13, "denied"), ValueError("x"),
                  urllib.error.URLError("some text reason")):
            self.assertFalse(m.is_no_network(e), repr(e))

    def test_dns_that_comes_up_in_time(self):
        self.net.up_at = 31
        self.assertTrue(self.m.wait_for_dns("api.github.com", 120, self.net.resolver, self.net.sleep, self.net.clock))
        self.assertEqual(self.net.sleeps, [5] * 7)

    def test_dns_that_never_comes_gives_up_after_2_minutes(self):
        self.net.up_at = 1e9
        self.assertFalse(self.m.wait_for_dns("api.github.com", 120, self.net.resolver, self.net.sleep, self.net.clock))
        self.assertGreaterEqual(self.net.t, 120)
        self.assertLess(self.net.t, 126)

    def test_a_zero_wait_is_one_try(self):
        self.net.up_at = 1e9
        self.assertFalse(self.m.wait_for_dns("h", 0, self.net.resolver, self.net.sleep, self.net.clock))
        self.assertEqual((self.net.resolves, self.net.sleeps), (1, []))

    def test_the_boot_case_the_network_comes_up_after_a_minute_and_the_update_goes_through(self):
        self.net.up_at = 65                                    # name resolution arrives a minute after the run began
        calls = []
        rc = self.run_it(lambda: calls.append(self.net.t) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], 65)
        self.assertEqual(self.statuses, [])                     # nothing was written: no "failed", no noise

    def test_an_attempt_that_hits_no_network_is_retried_and_says_waiting_not_failed(self):
        results = [self.m.NoNetwork("Temporary failure in name resolution")] * 2 + [0]

        def attempt():
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        rc = self.run_it(attempt)
        self.assertEqual(rc, 0)
        self.assertEqual([s[0] for s in self.statuses], ["waiting for the network"] * 2)
        self.assertEqual(self.statuses[0][1], "v0.35.1")
        self.assertIn("Temporary failure in name resolution", self.statuses[0][2])
        self.assertEqual(self.net.sleeps, [30, 30])

    def test_ten_minutes_without_a_network_is_failed_and_only_then(self):
        self.net.up_at = 1e9

        def attempt():
            raise self.m.NoNetwork("never")
        rc = self.run_it(attempt)
        self.assertEqual(rc, 1)
        self.assertEqual({s[0] for s in self.statuses[:-1]}, {"waiting for the network"})
        self.assertEqual(self.statuses[-1][0], "failed")
        self.assertIn("no network for 10 minutes", self.statuses[-1][2])
        self.assertGreaterEqual(self.net.t, 600)
        self.assertLess(self.net.t, 660)

    def test_a_dns_that_resolves_but_an_attempt_that_keeps_failing_also_ends_at_10_minutes(self):
        def attempt():
            raise self.m.NoNetwork("timed out")
        self.assertEqual(self.run_it(attempt), 1)
        self.assertEqual(self.statuses[-1][0], "failed")
        self.assertGreaterEqual(self.net.t, 570)
        self.assertLessEqual(self.net.t, 600)

    def test_another_failure_is_not_retried(self):
        calls = []
        self.assertEqual(self.run_it(lambda: calls.append(1) or 1), 1)
        self.assertEqual((calls, self.statuses, self.net.sleeps), ([1], [], []))

    def test_no_wait_is_one_attempt_and_failed_at_once(self):
        self.net.up_at = 1e9
        calls = []
        rc = self.run_it(lambda: calls.append(1) or 0, wait=False)
        self.assertEqual((rc, calls), (0, [1]))                  # no waiting for DNS either: it just tries

        def attempt():
            raise self.m.NoNetwork("refused")
        self.assertEqual(self.run_it(attempt, wait=False), 1)
        self.assertEqual(self.statuses, [("failed", "v0.35.1", "refused")])
        self.assertEqual(self.net.sleeps, [])

    def test_the_budget_is_never_overshot_by_a_sleep(self):
        self.net.t = 590

        def attempt():
            raise self.m.NoNetwork("x")
        self.net.up_at = 0
        self.m.run_with_retries(attempt, "h", None, resolver=self.net.resolver, sleep=self.net.sleep, clock=self.net.clock,
                                set_status=lambda *a: None, log=lambda s: None)
        self.assertLessEqual(max(self.net.sleeps), 30)


class TestNetworkWaitEndToEnd(unittest.TestCase):
    """The real program: an unreachable API with --no-wait is "failed" with the reason; a later good run replaces it."""

    @classmethod
    def setUpClass(cls):
        cls.rel = Release()

    def run_updater(self, api, *extra):
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-update-ollama"), "--no-restart", "--api", api] +
                           list(extra), env=env, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_a_refused_connection_is_no_network_and_a_later_success_clears_it(self):
        shutil.rmtree(OPT, ignore_errors=True)
        os.makedirs(OPT)
        dead = "http://127.0.0.1:%d/release" % U.free_port()
        rc, out = self.run_updater(dead, "--no-wait")
        self.assertEqual(rc, 1)
        st = json.load(open(STATUS))
        self.assertEqual(st["result"], "failed")
        self.assertIn("refused", st["detail"].lower())
        self.rel.publish("v0.1.0", good_files("0.1.0"))
        rc, out = self.run_updater("http://127.0.0.1:%d/release" % self.rel.port)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.load(open(STATUS))["result"], "updated")

    def test_waiting_in_the_status_file_is_replaced_by_the_later_success(self):
        m = load_updater()
        net = FakeNet(up_at=0)
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        if os.path.exists(STATUS):
            os.unlink(STATUS)
        seen = []

        def spy(result, version="", detail=""):
            m.status(result, version, detail)
            seen.append(json.load(open(STATUS))["result"])
        calls = []

        def attempt():
            calls.append(1)
            if len(calls) < 3:
                raise m.NoNetwork("Temporary failure in name resolution")
            m.status("updated", "v0.36.0", "from v0.35.1")
            return 0
        self.assertEqual(m.run_with_retries(attempt, "api.github.com", "v0.35.1", resolver=net.resolver, sleep=net.sleep,
                                            clock=net.clock, set_status=spy, log=lambda s: None), 0)
        self.assertEqual(seen, ["waiting for the network", "waiting for the network"])
        self.assertEqual(json.load(open(STATUS))["result"], "updated")                 # the later success wins

    def test_an_unreachable_host_raises_no_network_out_of_the_real_attempt(self):
        import argparse
        m = load_updater()
        shutil.rmtree(OPT, ignore_errors=True)
        os.makedirs(os.path.join(OPT, "versions"))
        dead = "http://127.0.0.1:%d/release" % U.free_port()
        a = argparse.Namespace(api=dead, check=False, force=False, no_rocm=True, no_restart=True, no_wait=True)
        with self.assertRaises(m.NoNetwork):
            m.attempt(a, OPT, os.path.join(OPT, "versions"), None)
        bad = argparse.Namespace(api="http://127.0.0.1:%d/nothing" % self.rel.port, check=False, force=False, no_rocm=True,
                                 no_restart=True, no_wait=True)
        self.assertEqual(m.attempt(bad, OPT, os.path.join(OPT, "versions"), None), 1)         # a 404 is a real failure
        self.assertEqual(json.load(open(STATUS))["result"], "failed")

    def test_the_unit_and_the_panel(self):
        u = open(os.path.join(U.KIT, "systemd", "ollama1-update-ollama.service")).read()
        self.assertIn("After=network-online.target nss-lookup.target\n", u)
        self.assertIn("\nWants=network-online.target\n", u)
        self.assertNotIn("Restart=", u)                         # the program retries itself: no restart loop on a real failure
        a = open(os.path.join(U.BIN, "ollama1-admin")).read()
        self.assertIn("ou.result==='waiting for the network'", a)
        self.assertIn("'Waiting for the network'", a)
        self.assertEqual(a.count("ou.result==='failed'?'Last update failed'"), 1)       # only a real failure is "failed"


if __name__ == "__main__":
    unittest.main()
