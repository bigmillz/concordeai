"""tools/ram-model-test.sh: its verdict uses the gateway's own arithmetic.
(The rest of it needs root and a real Ollama; test_repo checks its safety
lines.)"""
import os
import subprocess
import sys
import unittest

import o1test_util as U
from stub_ollama import Stub


class TestRamModelVerdict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = Stub(U.free_port())
        s = open(os.path.join(U.KIT, "tools", "ram-model-test.sh")).read()
        cls.code = s[s.index("<<'PY'\n") + 7:s.index("\nPY\n)")]

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()

    def verdict(self, model, ctx, cap):
        env = dict(os.environ, PYTHONPATH=U.LIB,
                   OLLAMA1_OLLAMA_URL="http://127.0.0.1:%d" % self.stub.port)
        r = subprocess.run([sys.executable, "-", model, str(ctx), str(cap)], input=self.code,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split()

    def test_missing_model(self):
        self.assertEqual(self.verdict("nothere:1b", 4096, 40 << 30), ["MISSING"])

    def test_numbers_match_the_gateway(self):
        import o1ollama
        from stub_ollama import DEFAULT_MODELS
        m = DEFAULT_MODELS["moe:120b"]
        fit, size, need, budget, kv = self.verdict("moe:120b", 4096, 40 << 30)
        self.assertIn(fit, ("FITS", "REFUSED"))
        self.assertEqual(int(size), m["size"])
        self.assertEqual(int(need), o1ollama.fit_estimate(m["size"], m["info"], 4096, ram=True)[0])

    def test_a_small_cap_refuses(self):
        self.assertEqual(self.verdict("moe:120b", 4096, 2 << 30)[0], "REFUSED")


if __name__ == "__main__":
    unittest.main()
