"""The server's name is the owner's to choose (6b347): which names are
allowed, what derives from one (hostnames, Cloudflare names), and that an
install from before the name was a setting keeps working unchanged.
Every value here is made up (testsrv, alice, example.test)."""
import json
import os
import tempfile
import unittest

import o1test_util as U  # noqa: F401  (puts lib/ on the path)
import o1common as C


class TestNameRules(unittest.TestCase):
    def test_allowed_names(self):
        for ok in ("a", "testsrv", "gpu-2", "srv01", "a" * 32, "my-big-server"):
            self.assertTrue(C.valid_server_name(ok), ok)

    def test_refused_names(self):
        for bad in ("", "1abc", "Abc", "a" * 33, "a_b", "a-", "-a", "a.b", "a b", "héllo", "a/b", None, 7):
            self.assertFalse(C.valid_server_name(bad), repr(bad))


class TestDerived(unittest.TestCase):
    def test_hostnames_from_name_and_zone(self):
        c = C.resolve(dict(C.DEFAULTS, server_name="testsrv", cf_zone="Example.Test"))
        self.assertEqual((c["hostname_gateway"], c["hostname_admin"]),
                         ("testsrv.example.test", "testsrv-admin.example.test"))

    def test_two_servers_do_not_collide(self):
        a = C.resolve(dict(C.DEFAULTS, server_name="srv1", cf_zone="example.test"))
        b = C.resolve(dict(C.DEFAULTS, server_name="srv2", cf_zone="example.test"))
        self.assertEqual(len({a["hostname_gateway"], a["hostname_admin"], b["hostname_gateway"], b["hostname_admin"]}), 4)
        na, nb = C.access_names(a), C.access_names(b)
        for k in ("tunnel", "token", "policy_admin", "policy_app", "app_admin", "app_gateway"):
            self.assertNotEqual(na[k], nb[k], k)

    def test_no_zone_no_hostnames(self):
        c = C.resolve(dict(C.DEFAULTS, server_name="testsrv"))
        self.assertEqual((c["hostname_gateway"], c["hostname_admin"]), ("", ""))

    def test_cloudflare_names(self):
        n = C.access_names(dict(C.DEFAULTS, server_name="testsrv", owner_label="Alice"))
        self.assertEqual(n, {"tunnel": "testsrv", "token": "testsrv-app", "policy_admin": "testsrv admin - Alice only",
                             "policy_app": "testsrv app - service token", "app_admin": "testsrv admin",
                             "app_gateway": "testsrv app"})
        self.assertEqual(C.access_names(dict(C.DEFAULTS, server_name="testsrv"))["policy_admin"], "testsrv admin only")


class TestInstallFromBeforeTheNameWasASetting(unittest.TestCase):
    """config.json written by an earlier setup.sh: no server_name."""

    def load(self, data):
        d = tempfile.mkdtemp(prefix="o1name-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, "config.json")
        with open(p, "w") as f:
            json.dump(data, f)
        return C.load_config(p)

    def test_absent_name_means_ollama1(self):
        c = self.load({"admin_email": "alice@example.test", "cf_zone": "example.test"})
        self.assertEqual(c["server_name"], "ollama1")
        self.assertEqual((c["hostname_gateway"], c["hostname_admin"]),
                         ("ollama1.example.test", "ollama1-admin.example.test"))
        n = C.access_names(c)
        self.assertEqual((n["tunnel"], n["token"], n["app_admin"], n["app_gateway"]),
                         ("ollama1", "ollama1-app", "ollama1 admin", "ollama1 app"))

    def test_an_invalid_name_does_not_rename_it(self):
        self.assertEqual(self.load({"server_name": "Not Valid"})["server_name"], "ollama1")

    def test_hostnames_already_written_win(self):
        c = self.load({"server_name": "testsrv", "cf_zone": "example.test",
                       "hostname_gateway": "old.example.test", "hostname_admin": "old-admin.example.test"})
        self.assertEqual((c["hostname_gateway"], c["hostname_admin"]), ("old.example.test", "old-admin.example.test"))

    def test_cloudflare_names_already_set_win(self):
        n = C.access_names({"server_name": "testsrv", "tunnel_name": "t0", "token_name": "k0",
                            "policy_admin_name": "p0", "policy_app_name": "p1"})
        self.assertEqual((n["tunnel"], n["token"], n["policy_admin"], n["policy_app"]), ("t0", "k0", "p0", "p1"))

    def test_a_missing_config_is_not_a_crash(self):
        c = C.load_config("/nonexistent/o1/config.json")
        self.assertEqual(c["server_name"], "ollama1")


if __name__ == "__main__":
    unittest.main()
