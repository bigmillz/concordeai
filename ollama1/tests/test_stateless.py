"""Statelessness: the gateway never writes a prompt or an answer anywhere.

Static part: walks the gateway's source (and the library modules it uses)
and fails on any file write, logging call or print that isn't one of the
known counts-only ones. Dynamic part: sends requests carrying unique
markers, then searches every file under the scratch prefix and everything
the gateway printed for those markers.
"""
import ast
import contextlib
import io
import json
import os
import time
import unittest
import uuid

import o1test_util as U
import test_gateway as TG

GATEWAY_FILES = [os.path.join(U.BIN, "ollama1-gateway")] + [
    os.path.join(U.LIB, m) for m in ("o1auth.py", "o1pair.py", "o1ollama.py", "o1common.py",
                                     "o1jwt.py", "o1crypto.py", "o1stats.py", "o1gen.py")]

# Functions allowed to open a file for writing, and why.
WRITERS = {
    "write_json_atomic": "counts-only stats/counters/devices JSON",
    "spool_request": "pairing hand-off: name, public key, timestamp, nonce, mac",
    "spool_burn": "pairing hand-off: a flag",
}
LOG_FIELDS = {"reason", "device", "model", "kind", "status", "ms", "tokens", "prompt_tokens",
              "attempts_left", "id", "port", "lan", "crypto", "access", "gpu_pct"}


def funcs_with_writes(tree):
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                mode = None
                if name in ("open", "fdopen"):
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        mode = node.args[1].value
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode = kw.value.value
                    if mode and any(c in mode for c in "wax+"):
                        if fn.name not in WRITERS:
                            bad.append((fn.name, node.lineno))
                if name in ("write_text", "write_bytes", "mkstemp", "NamedTemporaryFile"):
                    if fn.name not in WRITERS:
                        bad.append((fn.name, node.lineno))
    return bad


class TestStaticScan(unittest.TestCase):
    def trees(self):
        for path in GATEWAY_FILES:
            with open(path) as f:
                yield path, ast.parse(f.read(), path)

    def test_no_unexpected_file_writes(self):
        for path, tree in self.trees():
            self.assertEqual(funcs_with_writes(tree), [], path)

    def test_no_logging_module_or_stderr(self):
        for path, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                    self.assertNotIn("logging", names, path)
                if isinstance(node, ast.Attribute) and node.attr == "stderr":
                    self.fail("%s writes to stderr at line %d" % (path, node.lineno))

    def test_gateway_prints_only_through_log(self):
        path = GATEWAY_FILES[0]
        tree = ast.parse(open(path).read())
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name != "log":
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
                        self.fail("print() outside log() in %s line %d" % (fn.name, node.lineno))

    def test_log_calls_carry_only_counts_and_names(self):
        tree = ast.parse(open(GATEWAY_FILES[0]).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "log":
                self.assertEqual(len(node.args), 1, "log() takes one event name, line %d" % node.lineno)
                self.assertIsInstance(node.args[0], ast.Constant)
                for kw in node.keywords:
                    self.assertIn(kw.arg, LOG_FIELDS, "log field %r line %d" % (kw.arg, node.lineno))
                    # no field may be built from the request or response body
                    for sub in ast.walk(kw.value):
                        if isinstance(sub, ast.Name):
                            self.assertNotIn(sub.id, ("body", "obj", "fwd", "data", "line", "piece",
                                                      "raw", "msg"),
                                             "log field %s from %s, line %d" % (kw.arg, sub.id, node.lineno))

    def test_default_request_log_is_silenced(self):
        src = open(GATEWAY_FILES[0]).read()
        self.assertIn("def log_message(self, fmt, *args):", src)
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == "log_message":
                body = [st for st in fn.body if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))]
                self.assertEqual(len(body), 1)
                self.assertIsInstance(body[0], ast.Pass)

    def test_stats_record_fields(self):
        """The per-request record kept in memory and in the stats file."""
        tree = ast.parse(open(GATEWAY_FILES[0]).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "rec" for t in node.targets):
                keys = {k.value for k in node.value.keys}
                self.assertEqual(keys, {"t", "device", "model", "kind", "status", "ms", "tokens",
                                        "prompt_tokens"})


class TestDynamic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TG.setUpModule()

    @classmethod
    def tearDownClass(cls):
        TG.tearDownModule()

    def test_markers_never_land_anywhere(self):
        markers = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for kind in ("chat", "generate", "chat-nostream", "embed"):
                m = "MK" + uuid.uuid4().hex[:10].upper()
                markers.append(m)
                if kind.startswith("chat"):
                    obj = {"model": "small:8b", "messages": [{"role": "user", "content": m}],
                           "stream": kind == "chat"}
                    st, data, _ = TG.call("POST", "/api/chat", obj)
                elif kind == "generate":
                    st, data, _ = TG.call("POST", "/api/generate", {"model": "small:8b", "prompt": m})
                else:
                    st, data, _ = TG.call("POST", "/api/embed", {"model": "embed:small", "input": m})
                self.assertEqual(st, 200)
                if kind != "embed":
                    self.assertIn(m[-12:].encode(), data)  # the answer did carry it
            # also a refused and a failed one
            m = "MK" + uuid.uuid4().hex[:10].upper()
            markers.append(m)
            TG.call("POST", "/api/chat", {"model": "huge:70b", "messages": [{"role": "user", "content": m}]})
            TG.call("POST", "/api/chat", {"model": "nothere:1b", "messages": [{"role": "user", "content": m}]})
            time.sleep(1.5)  # let the stats loop write
            TG.G["gw"].stats  # noqa
            from o1common import Paths, write_json_atomic
            write_json_atomic(Paths.counters, TG.G["gw"].stats.counters())
        printed = out.getvalue()
        self.assertIn("req ", printed)
        found = []
        for root, _dirs, files in os.walk(U.PREFIX):
            for fn in files:
                fp = os.path.join(root, fn)
                if not os.path.isfile(fp):
                    continue
                try:
                    blob = open(fp, "rb").read()
                except OSError:
                    continue
                for m in markers:
                    if m.encode() in blob or m[-12:].encode() in blob:
                        found.append((fp, m))
        for m in markers:
            self.assertNotIn(m, printed)
            self.assertNotIn(m[-12:], printed)
        self.assertEqual(found, [])
        snap = json.load(open(os.path.join(U.PREFIX, "run/ollama1/stats/gateway.json")))
        self.assertTrue(snap["recent"])


if __name__ == "__main__":
    unittest.main()
