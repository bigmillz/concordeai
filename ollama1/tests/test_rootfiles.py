"""Root and other users' files: root never follows a planted symlink (or
blocks on a FIFO) when it reads a file someone else could have placed,
never sets a mode or owner by path, and never writes into the panel's
folder. Plus: the price checker never raises, and the root units that
touch the network or the panel's files are sandboxed."""
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unittest

import o1test_util as U
import o1power as P
import o1tariff as T
from o1common import Paths, read_json_safe, write_json_atomic


def unit(name):
    with open(os.path.join(U.KIT, "systemd", name)) as f:
        return f.read()


class TestSafeReads(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="o1safe-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.victim = os.path.join(self.d, "victim.json")
        with open(self.victim, "w") as f:
            json.dump({"secret": 1}, f)

    def test_symlink_fifo_dir_and_size(self):
        link = os.path.join(self.d, "link.json")
        os.symlink(self.victim, link)
        self.assertIsNone(read_json_safe(link))
        fifo = os.path.join(self.d, "fifo.json")
        os.mkfifo(fifo)
        t0 = time.monotonic()
        self.assertIsNone(read_json_safe(fifo))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIsNone(read_json_safe(self.d))
        big = os.path.join(self.d, "big.json")
        with open(big, "w") as f:
            f.write(json.dumps({"x": "a" * 5000}))
        self.assertIsNone(read_json_safe(big, max_bytes=1000))
        self.assertEqual(read_json_safe(self.victim), {"secret": 1})

    def test_write_never_follows_or_chmods_by_path(self):
        target = os.path.join(self.d, "out.json")
        os.symlink(self.victim, target)
        before = os.stat(self.victim)
        real_chmod, real_chown = os.chmod, os.chown

        def refuse(*a, **kw):
            raise AssertionError("mode or owner set by path")
        os.chmod = os.chown = refuse
        try:
            write_json_atomic(target, {"new": True}, mode=0o640, group="nosuchgroup-o1")
        finally:
            os.chmod, os.chown = real_chmod, real_chown
        self.assertFalse(os.path.islink(target))
        self.assertEqual(json.load(open(target)), {"new": True})
        self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o640")
        after = os.stat(self.victim)
        self.assertEqual((before.st_mode, before.st_size), (after.st_mode, after.st_size))
        self.assertEqual(json.load(open(self.victim)), {"secret": 1})


class TestPanelScheduleHandOff(unittest.TestCase):
    """The panel leaves a schedule in its own folder; root only reads it
    (safely) and writes root's own file."""

    def setUp(self):
        os.makedirs(os.path.dirname(P.request_file()), exist_ok=True)
        os.makedirs(os.path.dirname(P.tariff_file()), exist_ok=True)
        for f in (P.request_file(), P.tariff_file()):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        self.d = tempfile.mkdtemp(prefix="o1victim-")
        self.addCleanup(shutil.rmtree, self.d, True)
        # a root-only file that happens to be a valid schedule: following a
        # link to it would visibly apply it
        self.victim = os.path.join(self.d, "root-only.json")
        v = json.loads(json.dumps(T.DEFAULT))
        v["flat_rate"] = 9.99
        self.victim_text = json.dumps(v)
        with open(self.victim, "w") as f:
            f.write(self.victim_text)
        os.chmod(self.victim, 0o440)

    def sched(self):
        s = json.loads(json.dumps(T.DEFAULT))
        s["flat_rate"] = 0.15
        return s

    def test_root_writes_only_its_own_folder(self):
        self.assertEqual(os.path.dirname(P.tariff_file()), Paths.state)
        self.assertNotEqual(os.path.dirname(P.tariff_file()), Paths.admin_state)
        self.assertEqual(os.path.dirname(P.request_file()), Paths.admin_state)

    def test_apply_a_good_request(self):
        write_json_atomic(P.request_file(), self.sched(), mode=0o600)
        self.assertEqual(P.apply_request(), (True, []))
        self.assertEqual(P.load_schedule()["flat_rate"], 0.15)
        self.assertEqual(oct(os.stat(P.tariff_file()).st_mode & 0o777), "0o640")
        self.assertFalse(os.path.exists(P.request_file()))
        self.assertTrue(json.load(open(P.apply_result_file()))["ok"])

    def test_planted_symlinks_are_refused(self):
        os.symlink(self.victim, P.request_file())
        ok, errs = P.apply_request()
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(P.tariff_file()))
        self.assertFalse(os.path.lexists(P.request_file()))        # the link itself is removed
        self.assertEqual(open(self.victim).read(), self.victim_text)
        self.assertEqual(oct(os.stat(self.victim).st_mode & 0o777), "0o440")
        # and a link planted where root reads the tariff is ignored, not followed
        os.symlink(self.victim, P.tariff_file())
        self.assertIsNone(P.load_schedule()["flat_rate"])
        os.unlink(P.tariff_file())

    def test_fifo_and_oversize_requests(self):
        os.mkfifo(P.request_file())
        out = []
        t = threading.Thread(target=lambda: out.append(P.apply_request()), daemon=True)
        t.start()
        t.join(2.0)
        if t.is_alive():                      # blocked on the FIFO: let it go, then fail
            fd = os.open(P.request_file(), os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            t.join(2.0)
            self.fail("root blocked on a FIFO the panel planted")
        self.assertFalse(out[0][0])
        with open(P.request_file(), "w") as f:
            f.write(json.dumps(dict(self.sched(), note="x")) + " " * (P.MAX_SCHEDULE_BYTES + 1))
        self.assertFalse(P.apply_request()[0])
        write_json_atomic(P.request_file(), dict(self.sched(), flat_rate=-5), mode=0o600)
        ok, errs = P.apply_request()
        self.assertFalse(ok)
        self.assertTrue(any("flat_rate" in e for e in errs))
        self.assertFalse(os.path.exists(P.tariff_file()))


class TestValidateNeverRaises(unittest.TestCase):
    def test_fuzz(self):
        rnd = random.Random(333)
        junk = [None, True, 0, -1, 1e308, float("nan"), "", "x" * 300, [], [[]], {}, {"a": []},
                ["mon", ["tue"]], [{"x": 1}], {"tier": []}, "../../etc/passwd", "UTC", "24:00", "00:00"]

        def mutate(v, depth=0):
            if depth > 3 or rnd.random() < 0.3:
                return rnd.choice(junk)
            if isinstance(v, dict):
                v = dict(v)
                k = rnd.choice(list(v) + ["extra"]) if v else "extra"
                v[k] = mutate(v.get(k), depth + 1)
                return v
            if isinstance(v, list):
                v = list(v)
                if v:
                    i = rnd.randrange(len(v))
                    v[i] = mutate(v[i], depth + 1)
                return v
            return rnd.choice(junk)
        base = {"currency": "USD", "mode": "tou", "flat_rate": None, "timezone": "America/New_York",
                "tiers": {"on": 0.3, "off": 0.1, "discount": 0.05, "mid": None}, "weekends_off_peak": True,
                "holidays": {"enabled": True, "names": ["christmas"], "observed": True, "extra": []},
                "seasons": [{"name": "A", "from": "01-01", "to": "12-31",
                             "windows": [{"tier": "on", "days": "weekdays", "start": "16:00", "end": "21:00"}]}]}
        for _ in range(3000):
            s = mutate(base)
            clean, errs = T.validate(s)             # must not raise
            self.assertTrue((clean is None) == bool(errs))
            if clean is not None:
                T.Tariff(clean).tier_at(1790000000)
        for s in (None, [], "x", 5, {"holidays": {"names": [[1]]}}, {"seasons": [{"windows": [{"days": [[]]}]}]}):
            self.assertIsNone(T.validate(s)[0])


class TestSandboxes(unittest.TestCase):
    def test_power_sampler(self):
        u = unit("ollama1-power.service")
        caps = re.search(r"^CapabilityBoundingSet=(.*)$", u, re.M).group(1).split()
        self.assertNotIn("CAP_DAC_OVERRIDE", caps)
        self.assertNotIn("CAP_FOWNER", caps)
        for want in ("SystemCallFilter=@system-service", "ProtectProc=invisible", "IPAddressDeny=any",
                     "ProtectSystem=strict", "NoNewPrivileges=yes"):
            self.assertIn(want, u)
        self.assertRegex(u, r"(?m)^MemoryMax=\d+[MG]$")
        allow = re.search(r"^IPAddressAllow=(.*)$", u, re.M).group(1).split()
        self.assertEqual(sorted(allow), sorted(["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]))

    def test_library_and_apply_units(self):
        for name, rw in (("ollama1-models-preview.service", "/run/ollama1/library"),
                         ("ollama1-models-sync.service", "/run/ollama1/library"),
                         ("ollama1-power-apply.service", "/var/lib/ollama1")):
            u = unit(name)
            for want in ("ProtectSystem=strict", "ProtectHome=yes", "NoNewPrivileges=yes", "PrivateTmp=yes",
                         "ReadWritePaths=" + rw, "SystemCallFilter=@system-service"):
                self.assertIn(want, u, name)
        self.assertIn("PrivateNetwork=yes", unit("ollama1-power-apply.service"))

    def test_lan_nets_match_the_unit(self):
        allow = re.search(r"^IPAddressAllow=(.*)$", unit("ollama1-power.service"), re.M).group(1).split()
        self.assertEqual(sorted(str(n) for n in P.LAN_NETS), sorted(allow))


class TestPairSpoolIsReadSafely(unittest.TestCase):
    def test_uses_the_safe_reader(self):
        with open(os.path.join(U.LIB, "o1pair.py")) as f:
            src = f.read()
        body = src[src.index("def commit_spool"):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("read_json_safe(", body)
        self.assertNotIn(" read_json(", body)


if __name__ == "__main__":
    unittest.main()
