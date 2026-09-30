#!/usr/bin/env python3
"""Stand-ins for vgs, lvs, lvcreate, lvremove, systemctl, swapon, swapoff and
mkswap, for test_tools.py's encrypted-swap tests. State lives under
$O1_SWAP_TESTROOT (fake.json, proc-swaps, dev/mapper/); nothing real is
touched. $O1_SWAP_TESTROOT/fake.json "fail" names tools that fail."""
import json
import os
import sys

R = os.environ["O1_SWAP_TESTROOT"]
SJ = os.path.join(R, "fake.json")


def main():
    cmd, args = os.path.basename(sys.argv[0]), sys.argv[1:]
    with open(SJ) as f:
        s = json.load(f)
    s.setdefault("log", []).append(" ".join([cmd] + args))
    swaps = os.path.join(R, "proc-swaps")
    lines = open(swaps).read().splitlines() if os.path.exists(swaps) else []
    rc, out = 0, ""
    if cmd in s.get("fail", []):
        rc = 1
    elif cmd == "vgs":
        out = "  %d\n" % s["vg_free"]
    elif cmd == "lvs":
        rc = 0 if s.get("lv") else 5
        out = "  32.00g\n" if s.get("lv") else ""
    elif cmd == "lvcreate":
        s["lv"] = True
    elif cmd == "lvremove":
        s["lv"] = False
    elif cmd == "systemctl":
        mp = os.path.join(R, "dev/mapper/ollama1swap")
        if args[:1] == ["start"]:
            os.makedirs(os.path.dirname(mp), exist_ok=True)
            open(mp, "w").close()
        elif args[:1] == ["stop"] and os.path.exists(mp):
            os.unlink(mp)
    elif cmd == "swapon":
        lines.append("%s partition 1 0 -2" % args[-1])
    elif cmd == "swapoff":
        lines = [x for x in lines if not x.startswith(args[-1] + " ")]
    with open(swaps, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    with open(SJ, "w") as f:
        json.dump(s, f)
    sys.stdout.write(out)
    sys.exit(rc)


if __name__ == "__main__":
    main()
