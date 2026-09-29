"""Print millenai.py's CRYPTO_REQS: the hash-pinned PyNaCl, cffi and
pycparser wheels, as a pip requirements file (6b327).

    python3 packaging/crypto_reqs.py [millenai.py [crypto-requirements.txt]]

With no output file it prints to stdout. (PowerShell 5.1's `>` would
write UTF-16, so the Windows build names the file instead.)

The pins live only in millenai.py; the build scripts write this file
from them. The app is read, never imported (importing it would start
it). Exits 3 when the file has no CRYPTO_REQS (a release from before
6b327), so a build can say so instead of installing nothing.
"""
import ast
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "millenai.py"
with open(src, encoding="utf-8") as fh:
    tree = ast.parse(fh.read())
for node in tree.body:
    if (isinstance(node, ast.Assign) and len(node.targets) == 1
            and getattr(node.targets[0], "id", "") == "CRYPTO_REQS"):
        text = ast.literal_eval(node.value)
        if len(sys.argv) > 2:
            with open(sys.argv[2], "w", encoding="ascii", newline="\n") as out:
                out.write(text)
        else:
            sys.stdout.write(text)
        sys.exit(0)
sys.stderr.write("no CRYPTO_REQS in %s\n" % src)
sys.exit(3)
