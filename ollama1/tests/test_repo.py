"""Repo hygiene for a PUBLIC repo: no personal email, no keys or tokens, no
Cloudflare IDs; the shell scripts parse (and pass shellcheck when it's
installed); every unit points at a program the kit installs."""
import os
import re
import shutil
import subprocess
import unittest

import o1test_util as U

ALLOWED_EMAIL = re.compile(r"@(users\.noreply\.github\.com|cloudflare\.com|anthropic\.com)$")
TEXT = (".py", ".sh", ".md", ".service", ".timer", ".path", ".rules", ".conf", ".in", ".cfg", ".allow",
        ".tmpfiles", "", ".txt")


def kit_files():
    for root, dirs, files in os.walk(U.KIT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for f in files:
            if os.path.splitext(f)[1] in TEXT:
                yield os.path.join(root, f)


class TestRepo(unittest.TestCase):
    def test_no_personal_email(self):
        for path in kit_files():
            text = open(path, encoding="utf-8", errors="replace").read()
            for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", text):
                if m.endswith("@concordeai") or re.search(r"\.(service|timer|path|socket)$", m):
                    continue
                self.assertRegex(m, ALLOWED_EMAIL, "%s: %s" % (path, m))

    def test_no_secrets(self):
        pats = [r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bAKIA[0-9A-Z]{16}\b",
                r"(?i)(api[_-]?token|client[_-]?secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}",
                r"\b[0-9a-f]{32}\b(?!.*(sha|digest|hash))",   # a Cloudflare account/zone id shape
                r"TunnelSecret", r"millertechnology"]
        for path in kit_files():
            if path.endswith("test_repo.py"):
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
            for pat in pats:
                for line in text.splitlines():
                    if re.search(pat, line) and "PROTOCOL" not in path:
                        self.fail("%s: %s" % (path, line.strip()[:120]))

    def test_shell_scripts(self):
        for sh in ("setup.sh", "bin/ollama1-lan"):
            r = subprocess.run(["bash", "-n", os.path.join(U.KIT, sh)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, "-x", "setup.sh", "bin/ollama1-lan"], cwd=U.KIT, capture_output=True, text=True)
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
            if m.startswith("ollama1-setup"):
                continue
            self.assertIn(m, units, m)

    def test_allow_list_ships_empty(self):
        lines = [l for l in open(os.path.join(U.KIT, "config", "models.allow")) if l.strip() and not l.startswith("#")]
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
