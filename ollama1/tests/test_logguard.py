"""The log guard (6b517): a fixture /var/log tree in a temp folder, with the sizes scaled down (a 16 KiB tail, a
256 KiB limit, a 64 KiB low-disk floor) and the free space faked from what the tree holds. In place (same inode, a
concurrent appender still lands in the file), what is left alone, the low-disk order, symlinks, hard links and
paths outside the folder, the lock, the settings' clamps, that nothing is ever deleted, the units, setup.sh and
tools/install-logguard.sh against a fake root."""
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import o1test_util as U
import o1common
import o1logguard as G

KEEP = 16 << 10
FLOOR = 64 << 10
MAX = 256 << 10
SECRET = b"SECRET-CONTENT"          # in every fixture log: it must never reach a log line


def lines(n, tag="line", start=0):
    return b"".join(b"%s %08d %s padding padding padding\n" % (tag.encode(), i, SECRET) for i in range(start, start + n))


class Tree:
    """A temp folder with var/log (the guarded folder) and outside/ (which must never change)."""

    def __init__(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1lg-"))
        self.root = os.path.join(self.dir, "var", "log")
        self.out = os.path.join(self.dir, "outside")
        os.makedirs(self.root)
        os.makedirs(self.out)
        self.state = os.path.join(self.dir + "-state.json")
        self.cap = None
        self.vacuums = 0
        self.logs = []

    def put(self, rel, data, base=None):
        path = os.path.join(base or self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def held(self):
        """Bytes the tree's plain files hold, symlinks not followed."""
        total = 0
        for top, _dirs, files in os.walk(self.root):
            for n in files:
                st = os.lstat(os.path.join(top, n))
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
        return total

    def free(self):
        return self.cap - self.held()

    def vacuum(self):
        self.vacuums += 1
        return True

    def guard(self, cfg=None, **kw):
        args = dict(root=self.root, free=self.free if self.cap is not None else (lambda: 1 << 50),
                    vacuum=self.vacuum, log=self.logs.append, state_path=self.state, keep=KEEP, floor=FLOOR,
                    max_bytes=MAX, min_free=kw.pop("min_free", 1 << 20))
        args.update(kw)
        return G.Guard(cfg or {}, **args)

    def snapshot(self, top=None):
        """{relative path: (kind, size, content hash)} of everything, symlinks as links."""
        import hashlib
        snap = {}
        top = top or self.dir
        for d, dirs, files in os.walk(top):
            for n in dirs + files:
                p = os.path.join(d, n)
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    snap[os.path.relpath(p, top)] = ("link", os.readlink(p))
                elif stat.S_ISREG(st.st_mode):
                    with open(p, "rb") as f:
                        snap[os.path.relpath(p, top)] = ("file", st.st_size, hashlib.sha256(f.read()).hexdigest())
                else:
                    snap[os.path.relpath(p, top)] = ("other", stat.S_IFMT(st.st_mode))
        return snap

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        if os.path.exists(self.state):
            os.unlink(self.state)


class Base(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.close)


class TestInPlace(Base):
    def test_a_log_over_the_limit_keeps_its_tail_on_the_same_inode(self):
        data = lines(20000)
        p = self.t.put("syslog", data)
        ino = os.stat(p).st_ino
        out = self.t.guard().run()
        self.assertEqual(len(out["trims"]), 1)
        self.assertEqual(out["errors"], [])
        st = os.stat(p)
        self.assertEqual(st.st_ino, ino)
        self.assertLessEqual(st.st_size, KEEP)
        self.assertGreater(st.st_size, KEEP - 200)
        with open(p, "rb") as f:
            now = f.read()
        self.assertTrue(data.endswith(now))                       # its tail, nothing else
        self.assertTrue(data[:len(data) - len(now)].endswith(b"\n"))   # cut at a line start
        self.assertTrue(now.startswith(b"line "))
        self.assertNotIn(b"\0", now)

    def test_a_cut_on_a_line_start_keeps_that_line(self):
        one = b"x" * 127 + b"\n"                         # KEEP is 128 whole lines
        p = self.t.put("syslog", one * ((MAX // 128) + 50))
        self.t.guard().run()
        self.assertEqual(os.path.getsize(p), KEEP)

    def test_the_log_line_names_the_file_and_sizes_never_content(self):
        p = self.t.put("kern.log", lines(20000))
        old = os.path.getsize(p)
        self.t.guard().run()
        self.assertEqual(len(self.t.logs), 1)
        line = self.t.logs[0]
        self.assertRegex(line, r"^trimmed %s from %d to %d bytes \(\S+ \S+ to \S+ \S+\): over the 256\.0 KiB limit$"
                         % (re.escape(p), old, os.path.getsize(p)))
        for x in self.t.logs:
            self.assertNotIn(SECRET.decode(), x)
            self.assertNotIn("padding", x)

    def test_a_concurrent_appender_still_lands_in_the_file(self):
        p = self.t.put("syslog", lines(20000))
        ino = os.stat(p).st_ino
        fd = os.open(p, os.O_WRONLY | os.O_APPEND)               # rsyslog's handle
        stop = threading.Event()
        written = [0]

        def appender():
            i = 0
            while not stop.is_set():
                os.write(fd, b"live %08d %s\n" % (i, SECRET))
                i += 1
                written[0] = i
                if i % 50 == 0:
                    time.sleep(0.001)

        th = threading.Thread(target=appender)
        th.start()
        try:
            trims = 0
            deadline = time.time() + 20
            while trims < 5 and time.time() < deadline:
                if os.path.getsize(p) > MAX:
                    trims += len(self.t.guard().run()["trims"])
                time.sleep(0.005)
        finally:
            stop.set()
            th.join()
        self.assertGreaterEqual(trims, 5)
        os.write(fd, b"AFTER THE GUARD\n")
        os.close(fd)
        st = os.stat(p)
        self.assertEqual(st.st_ino, ino)
        with open(p, "rb") as f:
            now = f.read()
        self.assertNotIn(b"\0", now)                              # no hole: the handle appends to the new end
        self.assertTrue(now.endswith(b"AFTER THE GUARD\n"))
        self.assertLess(st.st_size, MAX + (4 << 20))
        last = b"live %08d " % (written[0] - 1)
        self.assertIn(last, now)                                  # the last line written before it stopped is there
        for ln in now.splitlines():
            self.assertRegex(ln, rb"^(line|live) \d{8} SECRET-CONTENT( padding padding padding)?$|^AFTER THE GUARD$")

    def test_what_is_appended_while_the_tail_is_read_is_kept(self):
        p = self.t.put("syslog", lines(20000))

        def late():
            with open(p, "ab") as f:
                f.write(b"LATE LINE\n")

        self.t.guard(on_read=late).run()
        with open(p, "rb") as f:
            self.assertTrue(f.read().endswith(b"LATE LINE\n"))

    def test_a_writer_that_outruns_the_read_keeps_its_own_last_lines(self):
        p = self.t.put("syslog", lines(20000))
        burst = lines(2000, "late")                               # far more than the tail

        def late():
            with open(p, "ab") as f:
                f.write(burst)

        self.t.guard(on_read=late).run()
        with open(p, "rb") as f:
            now = f.read()
        self.assertLessEqual(len(now), KEEP)
        self.assertTrue(burst.endswith(now))
        self.assertTrue(now.startswith(b"late "))

    def test_a_log_cut_by_someone_else_meanwhile_is_left_alone(self):
        p = self.t.put("syslog", lines(20000))
        out = self.t.guard(on_read=lambda: os.truncate(p, 10)).run()
        self.assertEqual(out["trims"], [])
        self.assertEqual(os.path.getsize(p), 10)
        self.assertTrue(any(x.startswith("left ") and "got shorter" in x for x in self.t.logs))

    def test_the_state_file_records_the_trim_privately(self):
        self.t.put("syslog", lines(20000))
        self.t.guard().run()
        self.assertEqual(os.stat(self.t.state).st_mode & 0o777, 0o600)
        with open(self.t.state) as f:
            st = json.load(f)
        self.assertEqual(st["count"], 1)
        self.assertEqual(st["last_trim"]["file"], os.path.join(self.t.root, "syslog"))
        self.assertNotIn("SECRET", json.dumps(st))


class TestLeftAlone(Base):
    def test_logs_under_the_limit_are_untouched(self):
        a = self.t.put("syslog", lines(1000))           # over the tail, under the limit
        b = self.t.put("auth.log", lines(10))
        c = self.t.put("other.log", lines(1000))
        old = time.time() - 3600
        for p in (a, b, c):
            os.utime(p, (old, old))
        before = self.t.snapshot()
        out = self.t.guard().run()
        self.assertEqual(out["trims"], [])
        self.assertEqual(self.t.snapshot(), before)
        for p in (a, b, c):
            self.assertEqual(int(os.stat(p).st_mtime), int(old))
        self.assertEqual(self.t.logs, [])                 # a pass with nothing to do says nothing
        self.assertFalse(os.path.exists(self.t.state))    # and writes nothing

    def test_only_the_list_and_top_level_logs_follow_the_size_rule(self):
        self.t.put("syslog.1", lines(20000))              # not on the list, not *.log
        self.t.put("apt/term.log", lines(20000))           # *.log, but not at the top
        before = self.t.snapshot()
        self.assertEqual(self.t.guard().run()["trims"], [])
        self.assertEqual(self.t.snapshot(), before)

    def test_listed_and_top_level_logs_follow_it(self):
        for n in ("syslog", "kern.log", "auth.log", "ufw.log", "foo.log"):
            self.t.put(n, lines(20000))
        self.t.put("apt/term.log", lines(20000))
        out = self.t.guard({"logguard_files": ["apt/term.log"]}).run()
        self.assertEqual(sorted(os.path.relpath(x["file"], self.t.root) for x in out["trims"]),
                         ["apt/term.log", "auth.log", "foo.log", "kern.log", "syslog", "ufw.log"])

    def test_a_dry_run_changes_nothing(self):
        self.t.put("syslog", lines(20000))
        self.t.put("sub/big", lines(5000))
        self.t.cap = self.t.held() + 1000
        before = self.t.snapshot()
        out = self.t.guard(min_free=1 << 20).run(dry_run=True)
        self.assertEqual(self.t.snapshot(), before)
        self.assertTrue(out["trims"])
        self.assertTrue(all(x.get("dry") for x in out["trims"]))
        self.assertEqual(self.t.vacuums, 0)
        self.assertTrue(all(x.startswith("would ") or x.startswith("the root") for x in self.t.logs), self.t.logs)

    def test_special_files_compressed_files_and_the_journal_are_never_cut(self):
        os.mkfifo(os.path.join(self.t.root, "auth.log"))  # opening it would block
        self.t.put("syslog.2.gz", lines(5000))
        self.t.put("journal/abc/system.journal", lines(5000))
        self.t.put("journal/abc/user-1000-x.journal", lines(5000))
        self.t.put("journal/abc/plain", lines(5000))      # the journal's folder is never entered at all
        self.t.put("wtmp", lines(5000))
        self.t.put("installer/plain", lines(5000))
        self.t.cap = self.t.held() + 1000
        before = self.t.snapshot()
        out = self.t.guard(min_free=1 << 30).run()
        self.assertEqual([os.path.relpath(x["file"], self.t.root) for x in out["trims"]], ["installer/plain"])
        after = self.t.snapshot()
        for k in before:
            if k != "var/log/installer/plain":
                self.assertEqual(after[k], before[k], k)


    def test_only_plain_files_pass_the_check(self):
        fifo = os.path.join(self.t.root, "fifo")
        os.mkfifo(fifo)
        sock = os.path.join(self.t.root, "s")
        import socket
        sk = socket.socket(socket.AF_UNIX)
        try:
            sk.bind(sock)
        except OSError:
            sock = None
        os.symlink("fifo", os.path.join(self.t.root, "link"))
        g = self.t.guard()
        g.dev = os.stat(self.t.root).st_dev
        for p in [fifo, os.path.join(self.t.root, "link"), self.t.root] + ([sock] if sock else []):
            with self.assertRaises(G.Refused, msg=p):
                g.check(os.path.basename(p), os.lstat(p))
        sk.close()
        g.check("syslog", os.lstat(self.t.put("syslog", b"x\n")))
        self.t.put("secret", b"x\n", base=self.t.out)
        g.root_fd = os.open(self.t.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for parts in ((("..", "..", "outside", "secret")), ("sub", "..", "..", "..", "outside", "secret")):
                with self.assertRaises(G.Refused, msg=parts):
                    g.lookup(parts)
        finally:
            os.close(g.root_fd)


class TestLowDisk(Base):
    def setUp(self):
        super().setUp()
        self.t.put("a", lines(4000))           # ~260 KiB
        self.t.put("b/b", lines(9000))         # ~590 KiB
        self.t.put("c.1", lines(6500))         # ~430 KiB
        self.t.put("small", lines(500))        # under the floor
        self.sizes = {n: os.path.getsize(os.path.join(self.t.root, n)) for n in ("a", "b/b", "c.1", "small")}

    def test_the_biggest_log_goes_first_and_it_stops_once_there_is_room(self):
        need = 100 << 10
        self.t.cap = self.t.held() + need - 1
        out = self.t.guard(min_free=need).run()
        self.assertTrue(out["low"])
        self.assertEqual([os.path.relpath(x["file"], self.t.root) for x in out["trims"]], ["b/b"])
        self.assertEqual(os.path.getsize(os.path.join(self.t.root, "a")), self.sizes["a"])
        self.assertEqual(os.path.getsize(os.path.join(self.t.root, "c.1")), self.sizes["c.1"])
        self.assertEqual(self.t.vacuums, 0)                     # the logs were enough
        self.assertTrue(self.t.logs[0].startswith("the root filesystem has "))

    def test_then_the_next_biggest(self):
        need = self.sizes["b/b"] + (50 << 10)
        self.t.cap = self.t.held() + (10 << 10)
        out = self.t.guard(min_free=need).run()
        self.assertEqual([os.path.relpath(x["file"], self.t.root) for x in out["trims"]], ["b/b", "c.1"])

    def test_with_nothing_left_the_journal_is_vacuumed(self):
        self.t.cap = self.t.held() + 10
        out = self.t.guard(min_free=1 << 40).run()
        self.assertEqual([os.path.relpath(x["file"], self.t.root) for x in out["trims"]], ["b/b", "c.1", "a"])
        self.assertEqual(self.t.vacuums, 1)
        self.assertEqual(os.path.getsize(os.path.join(self.t.root, "small")), self.sizes["small"])
        self.assertTrue(any("is left to trim" in x for x in self.t.logs))
        n = len(self.t.logs)
        self.t.guard(min_free=1 << 40).run()                     # the same note again only after an hour
        self.assertFalse(any("is left to trim" in x for x in self.t.logs[n:]))

    def test_with_enough_room_nothing_is_scanned(self):
        self.t.cap = self.t.held() + (1 << 30)
        out = self.t.guard(min_free=1 << 20).run()
        self.assertFalse(out["low"])
        self.assertEqual(out["trims"], [])


class TestNeverOutside(Base):
    def test_a_symlinked_log_is_never_followed(self):
        secret = self.t.put("secret", lines(20000), base=self.t.out)
        os.symlink(secret, os.path.join(self.t.root, "syslog"))
        os.symlink("../../outside/secret", os.path.join(self.t.root, "evil.log"))
        before = self.t.snapshot()
        self.t.cap = self.t.held() + 10
        out = self.t.guard(min_free=1 << 40).run()
        self.assertEqual(out["trims"], [])
        self.assertEqual(self.t.snapshot(), before)

    def test_a_symlinked_folder_is_never_entered(self):
        self.t.put("deep/big.log", lines(20000), base=self.t.out)
        os.symlink(os.path.join(self.t.out, "deep"), os.path.join(self.t.root, "linked"))
        os.makedirs(os.path.join(self.t.root, "real"))
        os.symlink(os.path.join(self.t.out, "deep"), os.path.join(self.t.root, "real", "inner"))
        self.t.cap = self.t.held() + 10
        before = self.t.snapshot()
        out = self.t.guard({"logguard_files": ["linked/big.log", "real/inner/big.log"]}, min_free=1 << 40).run()
        self.assertEqual(out["trims"], [])
        self.assertEqual(self.t.snapshot(), before)

    def test_a_hard_link_to_an_outside_file_is_refused(self):
        secret = self.t.put("shadow", lines(20000), base=self.t.out)
        os.link(secret, os.path.join(self.t.root, "syslog"))
        os.link(secret, os.path.join(self.t.root, "hard.log"))
        self.t.cap = self.t.held() + 10
        before = self.t.snapshot()
        out = self.t.guard(min_free=1 << 40).run()
        self.assertEqual(out["trims"], [])
        self.assertEqual(self.t.snapshot(), before)

    def test_names_outside_the_folder_are_refused(self):
        self.t.put("secret", lines(20000), base=self.t.out)
        bad = ["/etc/passwd", "../../outside/secret", "/var/log/../../outside/secret", "sub/../../outside/secret",
               "./syslog", "", "a//b", 7, None, "/var/log", "a\nb", ".hidden"]
        s = G.settings({"logguard_files": bad})
        self.assertEqual(s["files"], [(f,) for f in G.DEFAULT_FILES])
        self.assertEqual(len(s["notes"]), len(bad))
        for name in bad:
            self.assertIsNone(G.rel_parts(name), name)
        self.assertEqual(G.rel_parts("/var/log/apt/term.log"), ("apt", "term.log"))
        self.assertEqual(G.rel_parts("syslog"), ("syslog",))
        before = self.t.snapshot()
        self.t.guard({"logguard_files": bad}).run()
        self.assertEqual(self.t.snapshot(), before)

    def test_a_symlinked_log_folder_is_refused_whole(self):
        real = os.path.join(self.t.out, "logs")
        self.t.put("syslog", lines(20000), base=real)
        link = os.path.join(self.t.dir, "linklog")
        os.symlink(real, link)
        before = self.t.snapshot()
        out = self.t.guard(root=link).run()
        self.assertEqual(out["trims"], [])
        self.assertEqual(out["skipped"], "no log folder")
        self.assertEqual(self.t.snapshot(), before)

    def test_nothing_is_ever_deleted(self):
        for i in range(6):
            self.t.put("d%d/x%d.log" % (i % 3, i), lines(3000 + 1000 * i))
            self.t.put("top%d.log" % i, lines(500 * i + 10))
        for n in G.DEFAULT_FILES:
            self.t.put(n, lines(20000))
        os.symlink("syslog", os.path.join(self.t.root, "syslog.link"))
        self.t.put("keep", lines(100), base=self.t.out)
        names = set(self.t.snapshot())
        outside = self.t.snapshot(self.t.out)
        self.t.cap = self.t.held() + 10
        for _ in range(3):
            self.t.guard(min_free=1 << 40).run()
        self.assertEqual(set(self.t.snapshot()), names)
        self.assertEqual(self.t.snapshot(self.t.out), outside)
        src = open(os.path.join(U.LIB, "o1logguard.py")).read()
        for word in ("os.remove(", "rmtree", "rmdir(", "os.rename(", "shutil"):
            self.assertNotIn(word, src)
        self.assertEqual(src.count("unlink("), 1)              # its own temporary state file only
        self.assertIn("os.unlink(tmp)", src)


class TestLock(Base):
    def test_a_second_pass_waits_its_turn(self):
        p = self.t.put("syslog", lines(20000))
        size = os.path.getsize(p)
        fd = os.open(self.t.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.t.guard().run()
            self.assertEqual(out["skipped"], "locked")
            self.assertEqual(out["trims"], [])
            self.assertEqual(os.path.getsize(p), size)
        finally:
            os.close(fd)
        self.assertEqual(len(self.t.guard().run()["trims"]), 1)

    def test_the_lock_is_let_go(self):
        self.t.put("syslog", lines(20000))
        self.t.guard().run()
        fd = os.open(self.t.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # would raise if a pass still held it
        finally:
            os.close(fd)


class TestSettings(unittest.TestCase):
    def test_defaults(self):
        s = G.settings({})
        self.assertEqual((s["max_bytes"], s["min_free"]), (2 << 30, 8 << 30))
        self.assertEqual(s["files"], [("syslog",), ("kern.log",), ("auth.log",), ("ufw.log",)])
        self.assertEqual(s["notes"], [])
        self.assertEqual(G.KEEP_BYTES, 64 << 20)
        self.assertEqual(G.LOWDISK_FLOOR, 256 << 20)
        self.assertEqual(G.settings(None)["max_bytes"], 2 << 30)

    def test_clamps(self):
        self.assertEqual(G.settings({"logguard_max_bytes": 1})["max_bytes"], 256 << 20)
        self.assertEqual(G.settings({"logguard_max_bytes": 10 ** 15})["max_bytes"], 20 << 30)
        self.assertEqual(G.settings({"logguard_max_bytes": 5 << 30})["max_bytes"], 5 << 30)
        self.assertEqual(G.settings({"logguard_max_bytes": -5})["max_bytes"], 256 << 20)
        for bad in (True, "2G", 2.5, None, [1]):
            s = G.settings({"logguard_max_bytes": bad})
            self.assertEqual(s["max_bytes"], 2 << 30, bad)
            self.assertTrue(s["notes"], bad)
        self.assertEqual(G.settings({"logguard_min_free_bytes": 1})["min_free"], 1 << 30)
        self.assertEqual(G.settings({"logguard_min_free_bytes": 10 ** 15})["min_free"], 64 << 30)
        self.assertEqual(G.settings({"logguard_min_free_bytes": 16 << 30})["min_free"], 16 << 30)
        self.assertEqual(G.settings({"logguard_min_free_bytes": False})["min_free"], 8 << 30)

    def test_the_four_logs_stay_on_the_list(self):
        s = G.settings({"logguard_files": ["apt/term.log", "syslog", "/var/log/cups/error_log"]})
        self.assertEqual(s["files"], [("syslog",), ("kern.log",), ("auth.log",), ("ufw.log",), ("apt", "term.log"),
                                      ("cups", "error_log")])
        s = G.settings({"logguard_files": "syslog"})
        self.assertEqual(len(s["files"]), 4)
        self.assertTrue(s["notes"])
        self.assertEqual(len(G.settings({"logguard_files": ["x%d.log" % i for i in range(500)]})["files"]), 4 + 64)

    def test_the_kit_defaults_match(self):
        d = o1common.DEFAULTS
        self.assertEqual(d["logguard_max_bytes"], G.DEFAULT_MAX)
        self.assertEqual(d["logguard_min_free_bytes"], G.DEFAULT_MIN_FREE)
        self.assertEqual(d["logguard_files"], [])

    def test_a_broken_config_still_guards(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            for text in ("{nope", "[1, 2]", ""):
                with open(p, "w") as f:
                    f.write(text)
                self.assertEqual(G.load_config(p), {})
            self.assertEqual(G.load_config(os.path.join(d, "missing")), {})
            with open(p, "w") as f:
                json.dump({"logguard_max_bytes": 3 << 30}, f)
            os.symlink(p, os.path.join(d, "link.json"))
            self.assertEqual(G.load_config(os.path.join(d, "link.json")), {})
            self.assertEqual(G.load_config(p)["logguard_max_bytes"], 3 << 30)

    def test_stdlib_only(self):
        src = open(os.path.join(U.LIB, "o1logguard.py")).read()
        mods = set(re.findall(r"(?m)^(?:import|from) ([A-Za-z0-9_.]+)", src))
        self.assertFalse({m for m in mods if m.startswith("o1")}, mods)
        bin_src = open(os.path.join(U.BIN, "ollama1-logguard")).read()
        self.assertEqual({m for m in re.findall(r"(?m)^(?:import|from) ([A-Za-z0-9_.]+)", bin_src) if m.startswith("o1")},
                         {"o1logguard"})


class TestKit(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(*p)) as f:
            return f.read()

    def test_unit_and_timer(self):
        unit = self.read(U.SYSTEMD, "ollama1-logguard.service")
        for line in ("Type=oneshot", "ExecStart=/usr/local/lib/ollama1/bin/ollama1-logguard run", "NoNewPrivileges=yes",
                     "ProtectSystem=strict", "ReadWritePaths=/var/log -/var/lib/ollama1 -/run/log/journal\n",
                     "ProtectHome=yes", "PrivateDevices=yes", "PrivateNetwork=yes", "IPAddressDeny=any",
                     "RestrictAddressFamilies=AF_UNIX\n", "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH\n",
                     "SystemCallFilter=@system-service", "MemoryMax=256M", "LimitCORE=0"):
            self.assertIn(line, unit)
        self.assertEqual(unit.count("ReadWritePaths="), 1)
        self.assertNotIn("\nPrivateTmp=yes", unit)         # it would need disk space to start
        self.assertNotIn("[Install]", unit)                 # the timer starts it
        self.assertNotRegex(unit, r"(?m)^(Requires|After|BindsTo|PartOf)=")   # it runs with the rest of the kit down
        timer = self.read(U.SYSTEMD, "ollama1-logguard.timer")
        for line in ("OnBootSec=1min\n", "OnUnitActiveSec=1min\n", "AccuracySec=10s\n", "WantedBy=timers.target"):
            self.assertIn(line, timer)

    def test_setup_enables_the_timer_early(self):
        s = self.read(U.KIT, "setup.sh")
        self.assertIn("run systemctl enable --now ollama1-logguard.timer", s)
        self.assertIn('ln -sfn "$LIBDIR/bin/ollama1-logguard" /usr/local/sbin/ollama1-logguard', s)
        at = s.index("run systemctl enable --now ollama1-logguard.timer")
        self.assertLess(s.index('install -m 0644 "$KIT"/systemd/* /etc/systemd/system/'), at)
        self.assertLess(at, s.index('step "Firewall"'))     # before anything that can stop the run

    def test_commands_run(self):
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX)
        os.makedirs(os.path.join(U.PREFIX, "var", "log"), exist_ok=True)
        for args, want in ((["--dry-run"], r"dry run: \d+ log"), (["status"], r"log guard: a log over 2\.0 GiB"),
                           (["run"], r"(?s).*")):
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-logguard")] + args, env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertRegex(r.stdout, want)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-logguard"), "nonsense"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)

    def test_readme_and_notes(self):
        readme = self.read(U.KIT, "README.md")
        self.assertIn("## Log guard", readme)
        for word in ("logguard_max_bytes", "logguard_min_free_bytes", "logguard_files", "tools/install-logguard.sh",
                     "sudo ollama1-logguard status", "--dry-run"):
            self.assertIn(word, readme)
        notes = self.read(os.path.dirname(U.KIT), "NOTES.md")
        who = "P" + "atrick"                     # NOTES.md names the maintainer; the kit never does (test_repo)
        self.assertRegex(notes, r"(?m)^## 6b517 .*\(per %s\)$" % who)


class TestInstaller(unittest.TestCase):
    """tools/install-logguard.sh against a fake root: it puts exactly the guard's files in place, asks systemctl
    for exactly the reload, the timer, one pass and the timer's next run, and nothing else."""

    SCRIPT = os.path.join(U.TOOLS, "install-logguard.sh")

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1lgi-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "root")
        os.makedirs(self.root)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.dir, "calls")
        with open(os.path.join(self.bin, "systemctl"), "w") as f:
            f.write('#!/bin/sh\necho "$*" >>"$FAKE_CALLS"\n[ "$1" = list-timers ] && echo "NEXT 1min ollama1-logguard.timer"\n'
                    'exit 0\n')
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)

    def run_it(self, script=None):
        env = dict(os.environ, O1_LOGGUARD_TESTROOT=self.root, FAKE_CALLS=self.calls,
                   PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run(["bash", script or self.SCRIPT], env=env, capture_output=True, text=True, timeout=60)

    def tree(self):
        out = {}
        for d, dirs, files in os.walk(self.root):
            for n in files + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
                p = os.path.join(d, n)
                out[os.path.relpath(p, self.root)] = os.readlink(p) if os.path.islink(p) else oct(os.stat(p).st_mode & 0o777)
        return out

    def test_it_installs_only_the_guard(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.tree(), {
            "usr/local/lib/ollama1/bin/ollama1-logguard": "0o755",
            "usr/local/lib/ollama1/lib/o1logguard.py": "0o644",
            "etc/systemd/system/ollama1-logguard.service": "0o644",
            "etc/systemd/system/ollama1-logguard.timer": "0o644",
            "usr/local/sbin/ollama1-logguard": "/usr/local/lib/ollama1/bin/ollama1-logguard",
        })
        with open(self.calls) as f:
            self.assertEqual(f.read().splitlines(), ["daemon-reload", "enable --now ollama1-logguard.timer",
                                                     "start ollama1-logguard.service",
                                                     "list-timers --all --no-pager ollama1-logguard.timer"])
        self.assertIn("NEXT 1min ollama1-logguard.timer", r.stdout)
        for rel, src in (("usr/local/lib/ollama1/bin/ollama1-logguard", os.path.join(U.BIN, "ollama1-logguard")),
                         ("usr/local/lib/ollama1/lib/o1logguard.py", os.path.join(U.LIB, "o1logguard.py"))):
            with open(os.path.join(self.root, rel), "rb") as a, open(src, "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertEqual(self.run_it().returncode, 0)          # again: the same files

    def test_the_installed_copy_runs_on_its_own(self):
        self.assertEqual(self.run_it().returncode, 0)
        prefix = os.path.join(self.dir, "machine")
        os.makedirs(os.path.join(prefix, "var", "log"))
        env = dict(os.environ, OLLAMA1_PREFIX=prefix)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-I", os.path.join(self.root, "usr/local/lib/ollama1/bin/ollama1-logguard"),
                            "--dry-run"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run: 0 log(s)", r.stdout)

    def test_a_partial_kit_changes_nothing(self):
        kit = os.path.join(self.dir, "kit")
        for d in ("bin", "lib", "tools", "systemd"):
            os.makedirs(os.path.join(kit, d))
        shutil.copy(self.SCRIPT, os.path.join(kit, "tools"))
        shutil.copy(os.path.join(U.BIN, "ollama1-logguard"), os.path.join(kit, "bin"))
        shutil.copy(os.path.join(U.LIB, "o1logguard.py"), os.path.join(kit, "lib"))
        shutil.copy(os.path.join(U.SYSTEMD, "ollama1-logguard.service"), os.path.join(kit, "systemd"))
        r = self.run_it(os.path.join(kit, "tools", "install-logguard.sh"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ollama1-logguard.timer is missing", r.stdout)
        self.assertEqual(self.tree(), {})
        self.assertFalse(os.path.exists(self.calls))

    def test_it_touches_nothing_else(self):
        src = open(self.SCRIPT).read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for word in ("ufw", "sshd", "ssh ", "cloudflared", "nft", "netplan", "restart", "apt", "curl", "wget",
                     "setup.sh", "o1common", "rm ", "tunnel", "/etc/ollama1"):
            self.assertNotIn(word, code, word)
        self.assertEqual(re.findall(r"(?m)^\s*(?:if )?systemctl ([a-z-]+)", code),
                         ["daemon-reload", "enable", "start", "list-timers"])
        r = subprocess.run(["bash", "-n", self.SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, self.SCRIPT], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
