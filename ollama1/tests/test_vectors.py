"""PROTOCOL.md's test vectors are real: the document matches the code, the
reference Ed25519 matches the gateway's backend, and the verifier accepts
them (and refuses them one byte off)."""
import json
import os
import re
import unittest

import o1test_util as U
import gen_vectors
import o1crypto
from o1auth import AuthError, RequestVerifier, check_pair_mac


class TestVectors(unittest.TestCase):
    def test_rfc8032_reference(self):
        # RFC 8032 section 7.1, TEST 1 and TEST 2
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(U.ed_public(seed).hex(),
                         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        self.assertEqual(U.ed_sign(seed, b"").hex(),
                         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        seed2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
        self.assertEqual(U.ed_sign(seed2, b"\x72").hex(),
                         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
        self.assertTrue(o1crypto.ed25519_verify(U.ed_public(seed2), b"\x72", U.ed_sign(seed2, b"\x72")))

    def test_document_matches_code(self):
        doc = open(os.path.join(U.KIT, "PROTOCOL.md"), encoding="utf-8").read()
        m = re.search(r"<!-- vectors:begin -->\s*```json\n(.*?)```\s*<!-- vectors:end -->", doc, re.S)
        self.assertIsNotNone(m, "PROTOCOL.md has no vectors block")
        self.assertEqual(json.loads(m.group(1)), gen_vectors.vectors(),
                         "regenerate with: python3 tests/gen_vectors.py")

    def test_verifier_accepts_vectors(self):
        v = gen_vectors.vectors()
        pub = U.ed_public(bytes.fromhex(v["device"]["seed_hex"]))
        devs = {v["device"]["device_id"]: {"public_key": pub, "name": "vec"}}
        for r in v["requests"]:
            ver = RequestVerifier(lambda: devs, clock=lambda: r["timestamp"] + 5, start_time=0)
            ver.verify(r["method"], r["path"], r["headers"], r["body_utf8"].encode())
            bad = dict(r["headers"])
            ver2 = RequestVerifier(lambda: devs, clock=lambda: r["timestamp"] + 5, start_time=0)
            with self.assertRaises(AuthError):
                ver2.verify(r["method"], r["path"], bad, r["body_utf8"].encode() + b" ")

    def test_pairing_vector(self):
        pv = gen_vectors.vectors()["pairing"]
        mac = o1crypto.b64url_decode(pv["mac_b64url"])
        self.assertTrue(check_pair_mac(pv["code_shown"], pv["name"], pv["public_key_b64url"],
                                       pv["timestamp"], pv["nonce"], mac))
        self.assertTrue(check_pair_mac(pv["code_normalized"].lower(), pv["name"],
                                       pv["public_key_b64url"], pv["timestamp"], pv["nonce"], mac))
        self.assertFalse(check_pair_mac("7K4M-2QXE", pv["name"], pv["public_key_b64url"],
                                        pv["timestamp"], pv["nonce"], mac))


if __name__ == "__main__":
    unittest.main()
