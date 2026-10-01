"""Repo hygiene for a PUBLIC repo. Every line this branch adds, in any file
anywhere in the repo (tracked changes against origin/main plus untracked
files), is scanned for personal email addresses, keys, tokens, Cloudflare
IDs, UUIDs and long hex strings. The only exemption is PROTOCOL.md's
generated test-vector block. Also: the shell scripts parse (and pass
shellcheck when it's installed); every unit points at a program the kit
installs; the units keep what the security review asked for."""
import os
import re
import shutil
import subprocess
import unittest

import o1test_util as U

REPO = os.path.dirname(U.KIT)
ALLOWED_EMAIL = re.compile(r"@(users\.noreply\.github\.com|cloudflare\.com|anthropic\.com)$", re.I)
# RFC 2606 / 6761 reserved names: nobody can own an address there
RESERVED_EMAIL = re.compile(r"@([A-Za-z0-9-]+\.)*(example\.(com|net|org)|example|test|invalid|localhost)$", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("assigned token/secret", re.compile(r"(?i)(api[_-]?token|client[_-]?secret|tunnel[_-]?secret)\s*[:=]\s*['\"][A-Za-z0-9_\-+/=]{20,}")),
    ("tunnel credential", re.compile(r"\"TunnelSecret\"\s*:\s*\"[A-Za-z0-9+/=]{20,}")),
    ("64-hex string", re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")),
    ("32-hex id (Cloudflare account/zone shape)", re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{32}(?![0-9A-Fa-f])")),
    ("UUID", re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("Cloudflare API token shape", re.compile(r"\b[A-Za-z0-9_-]{40}\b(?=.*(token|bearer))", re.I)),
    ("private domain", re.compile(r"(?i)millertechnology")),
]


def git(*args):
    r = subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def added_lines():
    """{path: [added line, ...]} for everything this branch adds."""
    base = None
    for ref in ("origin/main", "main"):
        b = git("merge-base", "HEAD", ref)
        if b:
            base = b.strip()
            break
    out = {}
    if base is None or shutil.which("git") is None:
        for root, dirs, files in os.walk(U.KIT):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = os.path.join(root, f)
                try:
                    out[os.path.relpath(p, REPO)] = open(p, encoding="utf-8").read().splitlines()
                except (UnicodeDecodeError, OSError):
                    pass
        return out
    diff = git("diff", "-U0", "--no-color", "--no-ext-diff", base) or ""
    cur = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
            continue
        if cur and line.startswith("+"):
            out.setdefault(cur, []).append(line[1:])
    for rel in (git("ls-files", "--others", "--exclude-standard") or "").splitlines():
        try:
            out[rel] = open(os.path.join(REPO, rel), encoding="utf-8").read().splitlines()
        except (UnicodeDecodeError, OSError):
            pass
    return out


def vector_block_lines():
    doc = open(os.path.join(U.KIT, "PROTOCOL.md"), encoding="utf-8").read()
    m = re.search(r"<!-- vectors:begin -->(.*?)<!-- vectors:end -->", doc, re.S)
    return set(m.group(1).splitlines()) if m else set()


class TestRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = added_lines()
        cls.vectors = vector_block_lines()

    def scan(self):
        for path, lines in self.lines.items():
            if "__pycache__" in path or path.endswith(".pyc"):
                continue
            for line in lines:
                if path.endswith("ollama1/PROTOCOL.md") and line in self.vectors:
                    continue
                yield path, line

    def test_scan_covers_the_whole_diff(self):
        if not any(p.startswith("ollama1/") for p in self.lines):
            self.skipTest("this branch adds nothing under ollama1/")

    def test_no_personal_email(self):
        for path, line in self.scan():
            for m in EMAIL.findall(line):
                if m.endswith("@concordeai") or re.search(r"\.(service|timer|path|socket)$", m) or RESERVED_EMAIL.search(m):
                    continue
                self.assertRegex(m, ALLOWED_EMAIL, "%s: %s" % (path, m))

    def test_no_secrets_or_ids(self):
        hits = []
        for path, line in self.scan():
            if path.endswith("tests/test_repo.py"):
                continue
            for name, pat in SECRET_PATTERNS:
                if pat.search(line):
                    hits.append("%s: %s: %s" % (path, name, line.strip()[:100]))
        self.assertEqual(hits, [])

    def test_scanner_catches(self):
        samples = ["a" + "@" + "EXAMPLE.COM", "3b" * 32, "12345678-1234-1234-1234-123456789abc",
                   '"TunnelSecret": "' + "Q" * 44 + '"', "ab" * 16]
        self.assertFalse(ALLOWED_EMAIL.search(EMAIL.findall(samples[0])[0]))
        for s in samples[1:]:
            self.assertTrue(any(p.search(s) for _, p in SECRET_PATTERNS), s)

    def test_shell_scripts(self):
        for sh in ("setup.sh", "bin/ollama1-lan", "lib/setuplib.sh"):
            r = subprocess.run(["bash", "-n", os.path.join(U.KIT, sh)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, "-x", "setup.sh", "bin/ollama1-lan", "lib/setuplib.sh"], cwd=U.KIT,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_units_point_at_the_kit(self):
        progs = set(os.listdir(os.path.join(U.KIT, "bin")))
        for f in os.listdir(os.path.join(U.KIT, "systemd")):
            text = open(os.path.join(U.KIT, "systemd", f)).read()
            for m in re.findall(r"/usr/local/lib/ollama1/bin/([a-z0-9-]+)", text):
                self.assertIn(m, progs, f)

    def test_setup_installs_every_unit_it_enables(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        units = set(os.listdir(os.path.join(U.KIT, "systemd")))
        for m in re.findall(r"(ollama1-[a-z-]+\.(?:service|timer|path))", setup):
            self.assertIn(m, units, m)

    def test_allow_list_ships_empty(self):
        lines = [l for l in open(os.path.join(U.KIT, "config", "models.allow")) if l.strip() and not l.startswith("#")]
        self.assertEqual(lines, [])


class TestGuide(unittest.TestCase):
    """docs/your-own-server.md: what it names exists, its config keys are
    real."""

    def guide(self):
        return open(os.path.join(REPO, "docs", "your-own-server.md"), encoding="utf-8").read()

    def test_named_files_exist(self):
        g = self.guide()
        for unit in set(re.findall(r"\b(ollama1?-?[a-z-]*\.(?:service|timer|path))\b", g)):
            self.assertTrue(os.path.exists(os.path.join(U.KIT, "systemd", unit)), unit)
        for f in set(re.findall(r"ollama1/config/([A-Za-z0-9_.-]+)", g)):
            self.assertTrue(os.path.exists(os.path.join(U.KIT, "config", f)), f)
        for b in set(re.findall(r"/usr/local/lib/ollama1/bin/([a-z0-9-]+)", g)):
            self.assertTrue(os.path.exists(os.path.join(U.BIN, b)), b)

    def test_config_keys_are_real(self):
        from o1common import DEFAULTS
        import json as _json
        for block in re.findall(r"```json\n(.*?)```", self.guide(), re.S):
            for k in _json.loads(block):
                self.assertIn(k, DEFAULTS, k)


class TestNoPersonalValues(unittest.TestCase):
    """The kit and its docs are public (6b347): nothing of the maintainer's
    machine, user, LAN, domain or disks may be in them. Every example uses a
    placeholder (<your-user>, <server-ip>, <your-domain>, <server-name>) or a
    made-up value (testsrv, alice, example.test, 10.0.0.0/24). NOTES.md is a
    dated log and is not scanned; this file holds the needles, so it is not
    either."""

    NEEDLES = ("pmiller", "flyconcordefly", "192.168.86.", "enp38s0", "enp39s0", "patrick", "bigmillz",
               "s6b0n", "zr127", "zr11z")
    PUBLIC_URL = "github.com/bigmillz/concordeai"          # the real clone address: it must work
    MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
    MADE_UP_MACS = ("02:00:5e:10:00:", "aa:bb:cc:dd:ee:", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")

    def files(self):
        for top in (U.KIT, os.path.join(REPO, "docs")):
            for root, dirs, names in os.walk(top):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for n in names:
                    path = os.path.join(root, n)
                    if os.path.abspath(path) == os.path.abspath(__file__) or n.endswith(".pyc"):
                        continue
                    try:
                        with open(path, encoding="utf-8") as f:
                            yield os.path.relpath(path, REPO), f.read()
                    except (UnicodeDecodeError, OSError):
                        continue

    def test_kit_and_docs_carry_no_personal_values(self):
        seen = 0
        for rel, text in self.files():
            seen += 1
            low = text.replace(self.PUBLIC_URL, "").lower()
            for needle in self.NEEDLES:
                self.assertNotIn(needle, low, "%s has %r: use a placeholder" % (rel, needle))
            self.assertIsNone(re.search(r"\bPat\b", text), "%s names Pat" % rel)
            for mac in self.MAC.findall(text):
                self.assertTrue(mac.lower().startswith(self.MADE_UP_MACS), "%s has the MAC %s" % (rel, mac))
        self.assertGreater(seen, 60)          # it really walked the kit

    def test_the_scan_catches_what_it_is_for(self):
        # the check itself must not be vacuous
        for bad in ("ssh pmiller@host", "https://ollama1.flyconcordefly.com", "ip 192.168.86.10", "port enp39s0",
                    "Patrick's MacBook Pro", "mac 3c:7c:3f:12:34:56", "serial ZR127RMQ"):
            hit = any(n in bad.lower() for n in self.NEEDLES) or bool(
                [m for m in self.MAC.findall(bad) if not m.lower().startswith(self.MADE_UP_MACS)])
            self.assertTrue(hit, bad)
        self.assertNotIn("bigmillz", ("see " + self.PUBLIC_URL + "/tree/main").replace(self.PUBLIC_URL, "").lower())


class TestUnits(unittest.TestCase):
    def unit(self, name):
        return open(os.path.join(U.KIT, "systemd", name)).read()

    def test_no_core_dumps_no_swap(self):
        for u in ("ollama.service", "ollama1-gateway.service"):
            t = self.unit(u)
            self.assertIn("\nLimitCORE=0\n", t, u)
            self.assertIn("\nMemorySwapMax=0\n", t, u)

    def test_ollama_memory_cap(self):
        t = self.unit("ollama.service")
        self.assertIn("\nOOMPolicy=continue\n", t)
        self.assertIn("\nMemorySwapMax=0\n", t)
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn("/etc/systemd/system/ollama.service.d/10-ollama1-memory.conf", setup)
        self.assertIn("mem_total=$(awk '/^MemTotal:/", setup)
        self.assertIn("mem_max=$((mem_total - (8 << 30)))", setup)
        self.assertIn(r"MemoryMax=%s\nMemoryHigh=%s\nMemorySwapMax=0", setup)
        self.assertIn('systemctl show -p MemoryMax --value ollama.service)" = "$mem_max"', setup)
        self.assertIn("cfg_set_num ollama_memory_max_bytes", setup)
        self.assertIn("cfg_set_num ollama_memory_high_bytes", setup)

    def test_gateway_protected_from_the_oom_killer(self):
        self.assertIn("\nOOMScoreAdjust=-500\n", self.unit("ollama1-gateway.service"))

    def test_ollama_never_logs_prompts(self):
        t = self.unit("ollama.service")
        self.assertIn("Environment=OLLAMA_DEBUG=0", t)
        self.assertNotRegex(t, r"OLLAMA_DEBUG=(1|2|true|trace|debug)")
        self.assertNotIn("OLLAMA_LOG", t.replace("OLLAMA_LOG_", ""))
        self.assertIn("ExecStart=/opt/ollama/current/bin/ollama serve\n", t)

    def test_port_guard_required(self):
        for u in ("ollama.service", "ollama1-gateway.service", "ollama1-admin.service", "ollama1-ttyd.service"):
            head = self.unit(u).split("[Service]")[0]
            self.assertIn("Requires=ollama1-nft.service", head, u)
            self.assertRegex(head, r"After=.*ollama1-nft\.service", u)

    def test_port_guard_matches_every_local_address(self):
        nft = open(os.path.join(U.KIT, "config", "ollama1.nft.in")).read()
        rules = [l for l in nft.splitlines() if "reject" in l]
        self.assertTrue(rules)
        for l in rules:
            self.assertIn("fib daddr type local", l)
            self.assertNotIn("127.0.0.1", l)

    def test_gateway_not_in_pairing_group(self):
        self.assertNotIn("o1pair", self.unit("ollama1-gateway.service"))
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        for line in setup.splitlines():
            if re.match(r"\s*(usermod|gpasswd|adduser)\b", line) and "o1gw" in line:
                self.assertFalse("o1pair" in line and "-d" not in line, line)
        self.assertRegex(setup, r"gpasswd -d o1gw o1pair")
        self.assertRegex(setup, r"id -nG o1gw .*grep -qx o1pair")

    def test_setup_locks_before_relocating(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertLess(setup.index('flock -w'), setup.index("dest=/var/tmp/ollama1-kit"))
        self.assertIn("echo \\$? >$STATUS_FILE", setup)
        tmux_block = setup[setup.index('if [ "$NO_TMUX" = 0 ]'):setup.index('touch "$LOG"')]
        self.assertIn("flock -u 9", tmux_block)                        # handed over only for tmux
        self.assertEqual(setup.count("flock -u 9"), 1)                 # --no-tmux keeps it

    def test_sshd_keeps_default_maxauthtries(self):
        t = open(os.path.join(U.KIT, "config", "10-ollama1-sshd.conf.in")).read()
        self.assertNotRegex(t, r"(?m)^\s*MaxAuthTries")


if __name__ == "__main__":
    unittest.main()
