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
        self.assertTrue(any(p.startswith("ollama1/") for p in self.lines))

    def test_no_personal_email(self):
        for path, line in self.scan():
            for m in EMAIL.findall(line):
                if m.endswith("@concordeai") or re.search(r"\.(service|timer|path|socket)$", m):
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


class TestUnits(unittest.TestCase):
    def unit(self, name):
        return open(os.path.join(U.KIT, "systemd", name)).read()

    def test_no_core_dumps_no_swap(self):
        for u in ("ollama.service", "ollama1-gateway.service"):
            t = self.unit(u)
            self.assertIn("\nLimitCORE=0\n", t, u)
            self.assertIn("\nMemorySwapMax=0\n", t, u)

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

    def test_sshd_keeps_default_maxauthtries(self):
        t = open(os.path.join(U.KIT, "config", "10-ollama1-sshd.conf.in")).read()
        self.assertNotRegex(t, r"(?m)^\s*MaxAuthTries")


if __name__ == "__main__":
    unittest.main()
