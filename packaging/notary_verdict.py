#!/usr/bin/env python3
"""Read `xcrun notarytool submit --wait --output-format json` off stdin and
print "<submission id> <status>" on one line (6b548), for build_shell_dmg.sh.

The id is the one Apple's log is fetched by (`notarytool log <id>`) when the
status is anything but Accepted. A status with spaces ("In Progress") comes
back with underscores so the shell's `read` keeps it one word. Nothing
parseable prints "- none": no submission was made, and the build stops.
The last JSON object in the text wins, so a progress line before it, or an
error after a failed upload, doesn't confuse it.
"""
import json
import re
import sys

ID_RX = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def _objects(text: str):
    """Every top-level JSON object in the text, on one line or over many."""
    dec, i = json.JSONDecoder(), 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return
        try:
            d, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(d, dict):
            yield d
        i = end


def verdict(text: str):
    sid, status = "-", "none"
    for d in _objects(text):
        if isinstance(d.get("id"), str) and ID_RX.match(d["id"]):
            sid = d["id"]
        if isinstance(d.get("status"), str) and d["status"].strip():
            status = d["status"].strip()
    return sid, status.replace(" ", "_")


if __name__ == "__main__":
    print("%s %s" % verdict(sys.stdin.read()))
