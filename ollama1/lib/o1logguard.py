"""The log guard (6b517): no log may fill the root disk again.

On 2026-10-24 a failed suspend left the server's NVIDIA driver logging two
assertion lines hundreds of millions of times. In about five hours rsyslog
wrote 119 GB to /var/log/syslog and 118 GB to /var/log/kern.log, the 295 GB
root disk filled and the server hung until someone at the house pressed the
power button. Daily logrotate is far too slow for logs growing about 47 GB
an hour; the journal was fine (journald has its own cap).

ollama1-logguard.timer runs one pass a minute (ollama1-logguard run):

  * the size rule: each file on the list (syslog, kern.log, auth.log and
    ufw.log, always, plus any logguard_files) and any /var/log/*.log whose
    size is over logguard_max_bytes (2 GiB; 256 MiB to 20 GiB) keeps its
    last 64 MiB, cut at a line start;
  * the low-disk rule: while the root filesystem has under
    logguard_min_free_bytes free (8 GiB; 1 to 64 GiB), every log anywhere in
    /var/log over 256 MiB is trimmed the same way, biggest first, until the
    free space is back over the minimum. If that isn't enough, the journal
    is vacuumed to 500M (journalctl --vacuum-size=500M).

A trim is in place, on the same inode: rsyslog keeps its open handle and
appends (O_APPEND) to the same file, so nothing is ever renamed. The kept
tail is read into memory (a full disk needs no room for a copy), what was
appended meanwhile is read after it, then the file is truncated to 0 and the
tail is appended back. Lines rsyslog writes while that happens land in the
file; only those written in the instant between the last read and the
truncate can be lost. A file that got shorter meanwhile (logrotate's
copytruncate, say) is left alone.

Never: anything outside /var/log (every path is opened from a handle on
/var/log, one component at a time, never through a symlink; a file with
another hard link or on another filesystem is refused), a FIFO, socket or
device, the journal's own files, a compressed file, or a login record (wtmp,
btmp, lastlog, faillog). Nothing is ever deleted. One pass at a time: an
exclusive flock on the /var/log handle itself, so the lock needs no disk
space either. A pass with nothing to do stats a few files and returns.

Standalone on purpose (stdlib only, no o1common): tools/install-logguard.sh
puts this file and bin/ollama1-logguard on a running server without
replacing any library the rest of the kit is using. Logged (the journal):
one line per trim, the file and its sizes before and after, never a byte of
what is in it.
"""
import errno
import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import time

PREFIX = os.environ.get("OLLAMA1_PREFIX", "").rstrip("/")
LOG_ROOT = PREFIX + "/var/log"
ROOT_FS = PREFIX + "/"
CONFIG = PREFIX + "/etc/ollama1/config.json"
STATE = PREFIX + "/var/lib/ollama1/logguard.json"

MIB, GIB = 1 << 20, 1 << 30
KEEP_BYTES = 64 * MIB                  # what a trimmed log keeps: its last 64 MiB
DEFAULT_MAX, MIN_MAX, MAX_MAX = 2 * GIB, 256 * MIB, 20 * GIB
DEFAULT_MIN_FREE, MIN_MIN_FREE, MAX_MIN_FREE = 8 * GIB, 1 * GIB, 64 * GIB
LOWDISK_FLOOR = 256 * MIB              # the low-disk rule trims logs over this
JOURNAL_SIZE = "500M"
DEFAULT_FILES = ("syslog", "kern.log", "auth.log", "ufw.log")
MAX_DEPTH = 6                          # folders below /var/log the low-disk rule looks into
MAX_ENTRIES = 50000                    # and at most this many names in all
CHUNK = 4 * MIB
CATCHUP_ROUNDS = 16
LINE_LOOK = 1 * MIB                    # a trimmed log starts at the first line start in its first MiB
LOW_NOTE_EVERY_S = 3600                # "still low, nothing left to trim" at most once an hour
NOFOLLOW = os.O_NOFOLLOW
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
NOCTTY = getattr(os, "O_NOCTTY", 0)
SKIP_NAMES = frozenset(("wtmp", "btmp", "lastlog", "faillog"))
SKIP_SUFFIXES = (".gz", ".xz", ".zst", ".bz2", ".lz4", ".lzma", ".zip", ".journal", ".journal~")
SKIP_TOP_DIRS = frozenset(("journal",))           # journald's own files: vacuumed, never cut
NAME_RX = re.compile(r"^[A-Za-z0-9_@+-][A-Za-z0-9._@+-]{0,127}$")


def fmt(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def label(s, limit=160):
    """A file name made safe for a log line."""
    s = "".join(ch for ch in str(s) if ch.isprintable() and ch not in "\r\n\t\x1b")
    return s[:limit]


# ---- settings ----------------------------------------------------------------------

def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def clamp(v, lo, hi, default):
    """A whole number of bytes held to lo..hi; anything else is the default."""
    return max(lo, min(hi, v)) if _int(v) else default


def rel_parts(name):
    """A log named in logguard_files ("syslog", "apt/term.log" or "/var/log/syslog") as its path components
    below /var/log, or None for anything else: another absolute path, "." or "..", an empty component, a name
    with odd characters, or too deep."""
    if not isinstance(name, str):
        return None
    if name.startswith("/"):
        if not name.startswith("/var/log/"):
            return None
        name = name[len("/var/log/"):]
    parts = name.split("/")
    if not parts or len(parts) > MAX_DEPTH + 1:
        return None
    for c in parts:
        if c in ("", ".", "..") or not NAME_RX.match(c):
            return None
    return tuple(parts)


def settings(cfg):
    """{"max_bytes", "min_free", "files", "notes"} from config.json. The four default logs are always on the
    list; logguard_files only adds to them. A setting that isn't usable falls back and says so in notes."""
    cfg = cfg if isinstance(cfg, dict) else {}
    notes = []
    mx = cfg.get("logguard_max_bytes", DEFAULT_MAX)
    if not _int(mx):
        notes.append("logguard_max_bytes isn't a whole number of bytes, so it is %s" % fmt(DEFAULT_MAX))
    mf = cfg.get("logguard_min_free_bytes", DEFAULT_MIN_FREE)
    if not _int(mf):
        notes.append("logguard_min_free_bytes isn't a whole number of bytes, so it is %s" % fmt(DEFAULT_MIN_FREE))
    files = [(f,) for f in DEFAULT_FILES]
    extra = cfg.get("logguard_files", [])
    if not isinstance(extra, list):
        notes.append("logguard_files isn't a list of names, so only the default logs are guarded")
        extra = []
    for name in extra[:64]:
        parts = rel_parts(name)
        if parts is None:
            notes.append("logguard_files: %r is not a log in /var/log, so it is left out" % label(name, 80))
        elif parts not in files:
            files.append(parts)
    return {"max_bytes": clamp(mx, MIN_MAX, MAX_MAX, DEFAULT_MAX),
            "min_free": clamp(mf, MIN_MIN_FREE, MAX_MIN_FREE, DEFAULT_MIN_FREE),
            "files": files, "notes": notes}


def load_config(path=None):
    """config.json as a dict, read the way root reads a file someone could have placed (no symlink, no FIFO,
    at most 1 MiB). Missing or broken is {}: the guard runs on its defaults, never not at all."""
    try:
        fd = os.open(path or CONFIG, os.O_RDONLY | NOFOLLOW | os.O_NONBLOCK | CLOEXEC)
    except OSError:
        return {}
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > (1 << 20):
            return {}
        raw = os.read(fd, (1 << 20) + 1)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    finally:
        os.close(fd)


# ---- files -------------------------------------------------------------------------

def used(st):
    """The disk space a file really takes: its size, or less for a sparse one (blocks)."""
    blocks = getattr(st, "st_blocks", None)
    return st.st_size if blocks is None else min(st.st_size, blocks * 512)


def skipped_name(name):
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


def _lstat_at(name, dfd):
    return os.stat(name, dir_fd=dfd, follow_symlinks=False)


def free_bytes(path=None):
    """Free space on the root filesystem for an ordinary user (what rsyslog, as syslog, can still write)."""
    st = os.statvfs(path or ROOT_FS)
    return st.f_bavail * st.f_frsize


def vacuum_journal():
    """journalctl --vacuum-size=500M; True when it ran and said it was done. Never in a trial or test run
    (OLLAMA1_PREFIX): that machine's journal isn't the kit's to vacuum."""
    if PREFIX:
        return False
    try:
        r = subprocess.run(["journalctl", "--vacuum-size=" + JOURNAL_SIZE], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Refused(Exception):
    """A path the guard won't touch; the message says why."""


def _pread(fd, off, n):
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, min(CHUNK, n - len(out)), off + len(out))
        if not b:
            break
        out += b
    return out


def cut_in_place(fd, keep=KEEP_BYTES, on_read=None):
    """Keep the last `keep` bytes of the open file (O_RDWR|O_APPEND), on the same inode. Returns (old size, new
    size), or None when the file got shorter while it was being read (someone else cut it: left alone).
    on_read is a test hook, called once the tail is in memory."""
    size = os.fstat(fd).st_size
    old = size
    if size <= keep:
        return old, size
    buf = _pread(fd, size - keep - 1, keep + 1)      # and the byte before: was the cut at a line start?
    if on_read is not None:
        on_read()
    # what was appended while the tail was read comes after it, so it isn't cut away
    for _ in range(CATCHUP_ROUNDS):
        now = os.fstat(fd).st_size
        if now < size:
            return None
        if now == size:
            break
        if now - size > keep:            # it outran the read: only its own last bytes are kept
            buf = _pread(fd, now - keep - 1, keep + 1)
            size = now
            continue
        buf += _pread(fd, size, now - size)
        size = now
        if len(buf) > keep + 1:
            del buf[:len(buf) - keep - 1]
    nl = buf.find(b"\n", 0, min(len(buf), LINE_LOOK))
    del buf[:nl + 1 if 0 <= nl < len(buf) - 1 else 1]     # start at a whole line
    if os.fstat(fd).st_size < size:
        return None
    os.ftruncate(fd, 0)
    view = memoryview(buf)
    while view:
        n = os.write(fd, view[:CHUNK])   # O_APPEND: after anything written since the truncate, never over it
        view = view[n:]
    return old, os.fstat(fd).st_size


# ---- one pass ----------------------------------------------------------------------

class Guard:
    """One pass of both rules. Everything that touches the machine can be injected: the /var/log folder (root),
    the free space (free), the journal (vacuum), the log, the clock and the state file. max_bytes, min_free,
    keep and floor override the real sizes for the tests only."""

    def __init__(self, cfg=None, root=None, free=None, vacuum=None, log=print, clock=time.time, state_path=None,
                 keep=KEEP_BYTES, floor=LOWDISK_FLOOR, max_bytes=None, min_free=None, on_read=None):
        self.s = settings(cfg if cfg is not None else load_config())
        self.root = root or LOG_ROOT
        self.free = free or free_bytes
        self.vacuum = vacuum or vacuum_journal
        self.log = log
        self.clock = clock
        self.state_path = state_path or STATE
        self.keep = keep
        self.floor = floor
        self.max_bytes = self.s["max_bytes"] if max_bytes is None else max_bytes
        self.min_free = self.s["min_free"] if min_free is None else min_free
        self.on_read = on_read
        self.root_fd = None
        self.dev = None

    def show(self, parts):
        base = self.root.rstrip("/")
        if PREFIX and base.startswith(PREFIX + "/"):
            base = base[len(PREFIX):]
        return label(base + "/" + "/".join(parts))

    # -- looking up, never through a symlink --

    def lookup(self, parts):
        """(stat, parent folder fd) of a plain file at root/parts, or Refused. The caller closes the fd."""
        if any(c in ("", ".", "..") or "/" in c for c in parts):
            raise Refused("not a name in the folder")      # rel_parts never gives one; never "..", whatever asks
        dfd = os.dup(self.root_fd)
        try:
            for c in parts[:-1]:
                try:
                    nfd = os.open(c, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=dfd)
                except OSError as e:
                    raise Refused("missing" if e.errno == errno.ENOENT else "a folder on the way isn't a plain folder")
                os.close(dfd)
                dfd = nfd
                if os.fstat(dfd).st_dev != self.dev:
                    raise Refused("on another filesystem")
            try:
                st = _lstat_at(parts[-1], dfd)
            except OSError as e:
                raise Refused("missing" if e.errno == errno.ENOENT else "can't be looked at")
            self.check(parts[-1], st)
            return st, dfd
        except BaseException:
            os.close(dfd)
            raise

    def check(self, name, st):
        if stat.S_ISLNK(st.st_mode):
            raise Refused("a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise Refused("not a plain file")
        if st.st_nlink != 1:
            raise Refused("it has another hard link")
        if st.st_dev != self.dev:
            raise Refused("on another filesystem")
        if skipped_name(name):
            raise Refused("a compressed file, a journal file or a login record")

    def trim(self, parts, why, dry_run):
        """Trim one log; a dict of what happened, or None when it was refused or is gone."""
        try:
            st, dfd = self.lookup(parts)
        except Refused:
            return None
        try:
            if dry_run:
                self.log("would trim %s: %s to about %s (%s)" % (self.show(parts), fmt(st.st_size),
                                                                  fmt(min(st.st_size, self.keep)), why))
                return {"file": self.show(parts), "old": st.st_size, "new": min(st.st_size, self.keep), "dry": True}
            try:
                fd = os.open(parts[-1], os.O_RDWR | os.O_APPEND | NOFOLLOW | os.O_NONBLOCK | NOCTTY | CLOEXEC,
                             dir_fd=dfd)
            except OSError as e:
                return self.failed(parts, "couldn't open it (%s)" % (e.strerror or e))
            try:
                fst = os.fstat(fd)
                if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                    return None          # swapped since it was looked at: next pass
                self.check(parts[-1], fst)
                res = cut_in_place(fd, self.keep, self.on_read)
            except Refused:
                return None
            except OSError as e:
                return self.failed(parts, "couldn't trim it (%s)" % (e.strerror or e))
            finally:
                os.close(fd)
        finally:
            os.close(dfd)
        if res is None:
            self.log("left %s alone: it got shorter while it was read" % self.show(parts))
            return None
        old, new = res
        self.log("trimmed %s from %d to %d bytes (%s to %s): %s" % (self.show(parts), old, new, fmt(old), fmt(new), why))
        return {"file": self.show(parts), "old": old, "new": new, "at": int(self.clock()), "why": why}

    def failed(self, parts, msg):
        self.log("couldn't trim %s: %s" % (self.show(parts), msg))
        self.errors.append(self.show(parts))
        return None

    # -- the two rules --

    def size_rule_candidates(self):
        """The guarded list, then every /var/log/*.log, once each."""
        out = list(self.s["files"])
        try:
            with os.scandir(self.root_fd) as it:
                for e in it:
                    if e.name.endswith(".log") and (e.name,) not in out:
                        out.append((e.name,))
        except OSError:
            pass
        return out

    def scan(self):
        """[(space used, parts)] of every log under /var/log over the floor: plain files only, no symlink
        followed, not into another filesystem or the journal's folder, at most MAX_DEPTH folders down."""
        found, seen = [], [0]

        def walk(dfd, prefix):
            try:
                with os.scandir(dfd) as it:
                    entries = [e.name for e in it]
            except OSError:
                return
            for name in entries:
                seen[0] += 1
                if seen[0] > MAX_ENTRIES:
                    return
                try:
                    st = _lstat_at(name, dfd)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if len(prefix) >= MAX_DEPTH or st.st_dev != self.dev or (not prefix and name in SKIP_TOP_DIRS):
                        continue
                    try:
                        sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=dfd)
                    except OSError:
                        continue
                    try:
                        if os.fstat(sub).st_dev == self.dev:
                            walk(sub, prefix + (name,))
                    finally:
                        os.close(sub)
                    continue
                try:
                    self.check(name, st)
                except Refused:
                    continue
                if used(st) > self.floor:
                    found.append((used(st), prefix + (name,)))

        walk(self.root_fd, ())
        return found

    def run(self, dry_run=False):
        """One pass. {"trims", "errors", "low", "skipped"}."""
        self.errors = []
        out = {"trims": [], "errors": self.errors, "low": False, "skipped": ""}
        try:
            self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | CLOEXEC)
        except OSError:
            self.log("%s isn't a plain folder (a symlink, or missing): nothing is touched" % label(self.root))
            out["skipped"] = "no log folder"
            self.errors.append(label(self.root))
            return out
        try:
            self.dev = os.fstat(self.root_fd).st_dev
            if not dry_run:
                try:
                    fcntl.flock(self.root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self.log("another pass is running; this one does nothing")
                    out["skipped"] = "locked"
                    return out
            self._rules(out, dry_run)
        finally:
            os.close(self.root_fd)        # closing it lets the lock go
            self.root_fd = None
        if out["trims"] and not dry_run:
            self.save(out)
        return out

    def _rules(self, out, dry_run):
        for parts in self.size_rule_candidates():
            try:
                st, dfd = self.lookup(parts)
            except Refused:
                continue
            os.close(dfd)
            if used(st) > self.max_bytes:
                t = self.trim(parts, "over the %s limit" % fmt(self.max_bytes), dry_run)
                if t:
                    out["trims"].append(t)
        free = self.free()
        if free >= self.min_free:
            return
        out["low"] = True
        cands = self.scan()
        cands.sort(key=lambda c: -c[0])
        if cands:
            self.log("the root filesystem has %s free, under the %s minimum: trimming the biggest of %d log(s) over %s"
                     % (fmt(free), fmt(self.min_free), len(cands), fmt(self.floor)))
        for size, parts in cands:
            t = self.trim(parts, "the root filesystem had %s free" % fmt(free), dry_run)
            if t:
                out["trims"].append(t)
                if dry_run:
                    free += max(0, size - self.keep)
            free = free if dry_run else self.free()
            if free >= self.min_free:
                return
        if dry_run:
            self.log("would vacuum the journal to %s (still about %s free)" % (JOURNAL_SIZE, fmt(free)))
            return
        ok = self.vacuum()
        after = self.free()
        out["vacuumed"] = ok
        if after >= self.min_free:
            self.log("vacuumed the journal to %s: %s free now" % (JOURNAL_SIZE, fmt(after)))
            return
        st = self.load_state()
        if self.clock() - st.get("low_note_at", 0) >= LOW_NOTE_EVERY_S:
            self.log("the root filesystem still has only %s free (minimum %s), and no log in /var/log over %s is left "
                     "to trim%s" % (fmt(after), fmt(self.min_free), fmt(self.floor),
                                    "" if ok else "; the journal vacuum didn't run"))
            st["low_note_at"] = int(self.clock())
            self.write_state(st)

    # -- state: the last trims, for `ollama1-logguard status` --

    def load_state(self):
        try:
            fd = os.open(self.state_path, os.O_RDONLY | NOFOLLOW | os.O_NONBLOCK | CLOEXEC)
        except OSError:
            return {}
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {}
            d = json.loads(os.read(fd, 1 << 20).decode("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        finally:
            os.close(fd)

    def save(self, out):
        st = self.load_state()
        trims = [t for t in st.get("trims", []) if isinstance(t, dict)] + out["trims"]
        st["trims"] = trims[-20:]
        st["last_trim"] = out["trims"][-1]
        st["count"] = (st.get("count") if _int(st.get("count")) else 0) + len(out["trims"])
        self.write_state(st)

    def write_state(self, obj):
        """Best effort, atomic, 0600; never a reason for a pass to fail (a full disk can't take it)."""
        d = os.path.dirname(self.state_path)
        if not os.path.isdir(d):
            return
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".logguard-", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(obj, f, separators=(",", ":"), sort_keys=True)
            os.replace(tmp, self.state_path)
            tmp = None
        except OSError:
            pass
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)       # only ever its own temporary state file, never a log
                except OSError:
                    pass
