"""Ollama's own cache entries (6b448): Ollama 0.40 converts a model for its
llama.cpp runner and lists the copy in /api/tags as llamacpp:<sha>. The one
rule (o1ollama.is_internal) and the readers that don't have their own test
file's check: none of them shows, counts or picks it."""
import unittest

import o1test_util as U
import o1gputune as T
import o1ollama
from stub_ollama import CACHE, CACHE_SHA, Stub


class TestRule(unittest.TestCase):
    def test_the_entry_from_the_server(self):
        self.assertTrue(o1ollama.is_internal(CACHE))
        self.assertTrue(o1ollama.is_internal("llamacpp:" + CACHE_SHA))
        self.assertTrue(o1ollama.is_internal({"model": "llamacpp:" + CACHE_SHA}))

    def test_name_forms(self):
        for n in ("llamacpp:abc", "LlamaCpp:" + CACHE_SHA, "llamacpp/gpt-oss:20b",
                  "registry.ollama.ai/library/llamacpp:" + CACHE_SHA, " llamacpp:x "):
            self.assertTrue(o1ollama.is_internal(n), n)

    def test_models_people_pull_are_not_internal(self):
        for n in ("llama3.2:3b", "ministral-3:14b", "gpt-oss:20b", "llamacpp", "llamacppx:1b",
                  "my-llamacpp:1b", "hf.co/llamacpp-fan/model:q4", "", None, 5):
            self.assertFalse(o1ollama.is_internal(n), n)

    def test_runner_metadata_only_with_a_sha_tag(self):
        self.assertTrue(o1ollama.is_internal({"name": "gpt-oss:" + CACHE_SHA, "runner": "llamacpp"}))
        self.assertTrue(o1ollama.is_internal({"name": "x:" + CACHE_SHA, "details": {"runner": "LlamaCpp"}}))
        # a runner field alone never hides a model someone pulled
        self.assertFalse(o1ollama.is_internal({"name": "gpt-oss:20b", "runner": "llamacpp"}))
        self.assertFalse(o1ollama.is_internal({"name": "x:" + CACHE_SHA, "runner": "ollama"}))
        self.assertFalse(o1ollama.is_internal({"name": "x:" + CACHE_SHA.upper()[:63], "runner": "llamacpp"}))

    def test_user_models_and_cache_bytes(self):
        tags = [{"name": "a:1b", "size": 5}, CACHE, {"name": "gpt-oss:120b-cloud", "size": 384},
                {"name": "llamacpp/x:1", "size": 7}, {"name": "llamacpp:y", "size": True}, "junk", {"name": ""}]
        self.assertEqual([m["name"] for m in o1ollama.user_models(tags)], ["a:1b"])
        self.assertEqual(o1ollama.cache_bytes(tags), CACHE["size"] + 7)
        self.assertEqual((o1ollama.user_models(None), o1ollama.cache_bytes("x")), ([], 0))


class TestGpuTune(unittest.TestCase):
    def test_the_self_check_never_loads_the_cache(self):
        stub = Stub(U.free_port(), models={"small:8b": {"size": 5 << 30, "info": {}}})
        try:
            stub.extra_tags = [dict(CACHE, size=1 << 30)]          # smaller than the real model: would win
            ol = T.Ollama("http://127.0.0.1:%d" % stub.port)
            names = [m["name"] for m in ol.models()]
            self.assertIn("small:8b", names)
            self.assertNotIn(CACHE["name"], names)
            self.assertEqual(T.pick_model(ol, 16 << 30), "small:8b")
        finally:
            stub.close()


if __name__ == "__main__":
    unittest.main()
