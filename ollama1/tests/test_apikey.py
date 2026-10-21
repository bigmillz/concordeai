"""o1apikey (6b490): keys for the gateway's OpenAI-style door; only hashes are kept."""
import json
import os
import shutil
import tempfile
import unittest

import o1test_util  # noqa: F401  (sets the lib path)
import o1apikey


class TestKeys(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "keys.json")

    def test_the_file_holds_a_hash_never_the_key(self):
        key = o1apikey.new("omp-mac", self.path)
        raw = open(self.path).read()
        self.assertTrue(key.startswith("o1k_"))
        self.assertNotIn(key, raw)
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o640")
        self.assertEqual(o1apikey.verify(key, self.path), "omp-mac")

    def test_wrong_keys_and_odd_input_are_none(self):
        o1apikey.new("a", self.path)
        for bad in ("", None, "o1k_x", "Bearer o1k_x", "x" * 500, 7):
            self.assertIsNone(o1apikey.verify(bad, self.path))

    def test_new_with_the_same_name_replaces_and_revoke_removes(self):
        k1 = o1apikey.new("tool", self.path)
        k2 = o1apikey.new("tool", self.path)
        self.assertIsNone(o1apikey.verify(k1, self.path))
        self.assertEqual(o1apikey.verify(k2, self.path), "tool")
        self.assertTrue(o1apikey.revoke("tool", self.path))
        self.assertFalse(o1apikey.revoke("tool", self.path))
        self.assertIsNone(o1apikey.verify(k2, self.path))
        self.assertEqual(json.load(open(self.path)), {"keys": []})

    def test_names_are_plain(self):
        for bad in ("", "Has Space", "UPPER", "a" * 33, "../x"):
            with self.assertRaises(ValueError):
                o1apikey.new(bad, self.path)

    def test_a_missing_or_broken_file_is_no_keys(self):
        self.assertEqual(o1apikey.load(self.path), [])
        open(self.path, "w").write("{broken")
        self.assertEqual(o1apikey.load(self.path), [])


if __name__ == "__main__":
    unittest.main()
