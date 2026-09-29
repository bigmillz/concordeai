"""Access JWT checks against a local fake certs endpoint: key rotation,
refetch limits, and the RSA check itself."""
import time
import unittest

import o1test_util as U
from o1jwt import AccessVerifier, JWTError, rsa_pkcs1_sha256_verify

K1 = U.RSAKey("k1")
K2 = U.RSAKey("k2")


class TestJWT(unittest.TestCase):
    def setUp(self):
        self.jwks = U.FakeJWKS([K1])
        self.v = AccessVerifier(U.TEAM, U.GW_AUD, self.jwks.url, allow_insecure=True)

    def tearDown(self):
        self.jwks.close()

    def test_valid(self):
        self.assertEqual(self.v.verify(U.make_jwt(K1, U.claims()))["common_name"], U.CLIENT_ID)

    def test_https_required_outside_tests(self):
        with self.assertRaises(ValueError):
            AccessVerifier(U.TEAM, U.GW_AUD, "http://127.0.0.1:1/certs")
        self.assertEqual(AccessVerifier(U.TEAM, U.GW_AUD).certs_url,
                         "https://%s/cdn-cgi/access/certs" % U.TEAM)

    def test_unconfigured_refuses(self):
        with self.assertRaises(JWTError):
            AccessVerifier("", "", self.jwks.url, allow_insecure=True).verify(U.make_jwt(K1, U.claims()))

    def test_key_rotation_refetches(self):
        self.v.verify(U.make_jwt(K1, U.claims()))
        self.jwks.keys = [K1, K2]
        self.v.last_try = 0  # the 30 s refetch spacing has passed
        self.assertTrue(self.v.verify(U.make_jwt(K2, U.claims())))

    def test_unknown_kid_refetch_is_spaced(self):
        self.v.verify(U.make_jwt(K1, U.claims()))
        n = self.jwks.fetches
        for _ in range(5):
            with self.assertRaises(JWTError):
                self.v.verify(U.make_jwt(K2, U.claims()))
        self.assertEqual(self.jwks.fetches, n)

    def test_audience_list_and_string(self):
        self.v.verify(U.make_jwt(K1, U.claims(aud=U.GW_AUD)))
        self.v.verify(U.make_jwt(K1, dict(U.claims(), aud=["other", U.GW_AUD])))
        with self.assertRaises(JWTError):
            self.v.verify(U.make_jwt(K1, dict(U.claims(), aud=["other"])))

    def test_missing_exp(self):
        c = U.claims()
        del c["exp"]
        with self.assertRaises(JWTError):
            self.v.verify(U.make_jwt(K1, c))

    def test_leeway(self):
        now = int(time.time())
        self.v.verify(U.make_jwt(K1, U.claims(exp=now - 10)))
        with self.assertRaises(JWTError):
            self.v.verify(U.make_jwt(K1, U.claims(exp=now - 40)))

    def test_rsa_edges(self):
        msg = b"x"
        sig = K1.sign(msg)
        self.assertTrue(rsa_pkcs1_sha256_verify(K1.n, K1.e, msg, sig))
        self.assertFalse(rsa_pkcs1_sha256_verify(K1.n, K1.e, msg + b"y", sig))
        self.assertFalse(rsa_pkcs1_sha256_verify(K1.n, K1.e, msg, sig[1:]))
        self.assertFalse(rsa_pkcs1_sha256_verify(K1.n, K1.e, msg, K1.n.to_bytes(256, "big")))
        self.assertFalse(rsa_pkcs1_sha256_verify(K1.n, K1.e, msg, b"\x00" * 256))

    def test_garbage(self):
        for tok in ("", "a.b", "a.b.c", "....", "x" * 20000, None):
            with self.assertRaises(JWTError):
                self.v.verify(tok)


if __name__ == "__main__":
    unittest.main()
