#!/usr/bin/env python3
"""Prepare the native shell (shell/) for a bundle build (6b537).

Run from the repo root, by build_shell_dmg.sh on a Mac and by the
shell-build workflow on Windows and Linux. It

  * stamps the shell's `version` (shell/src-tauri/tauri.conf.json) from
    APP_VERSION in millenai.py, the one place the version is stored, so the
    bundle's CFBundleShortVersionString, the MSI's ProductVersion and the
    About box all say what the app says (CFBundleVersion is APP_BUILD and is
    set after the bundle is built; Tauri writes the version there);
  * writes the sidecar's first-run files into shell/src-tauri/sidecar/
    (ignored by git): launch.sh, extracted from build_macos_app.sh's LAUNCH
    heredoc so there is one copy of the venv bootstrap, and the hash-pinned
    crypto-requirements.txt that script installs (packaging/crypto_reqs.py).

Prints "<APP_VERSION> <APP_BUILD>" on one line for the caller.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "millenai.py"
CONF = ROOT / "shell" / "src-tauri" / "tauri.conf.json"
SIDECAR = ROOT / "shell" / "src-tauri" / "sidecar"


def main() -> int:
    src = SRC.read_text(encoding="utf-8")
    version = re.search(r'^APP_VERSION = "([^"]+)"', src, re.M).group(1)
    build = int(re.search(r"^APP_BUILD = (\d+)", src, re.M).group(1))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit("APP_VERSION must look like 1.2.3 for the bundlers; got %r" % version)

    conf = json.loads(CONF.read_text(encoding="utf-8"))
    if conf.get("version") != version:
        conf["version"] = version
        CONF.write_text(json.dumps(conf, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(CONF.read_text(encoding="utf-8"))["version"] == version

    SIDECAR.mkdir(parents=True, exist_ok=True)
    bm = (ROOT / "build_macos_app.sh").read_text(encoding="utf-8")
    m = re.search(r"<<'LAUNCH'\n(.*?)\nLAUNCH\n", bm, re.S)
    if not m:
        sys.exit("build_macos_app.sh: the LAUNCH heredoc was not found")
    launch = SIDECAR / "launch.sh"
    launch.write_text(m.group(1) + "\n", encoding="utf-8")
    os.chmod(launch, 0o755)
    subprocess.run([sys.executable, str(ROOT / "packaging" / "crypto_reqs.py"),
                    str(SRC), str(SIDECAR / "crypto-requirements.txt")], check=True)
    print(version, build)
    return 0


if __name__ == "__main__":
    sys.exit(main())
