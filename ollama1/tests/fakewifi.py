#!/usr/bin/env python3
"""Stand-ins for iw, netplan, ip, apt-get, udevadm and networkctl, used by test_wifi.py. Every call is logged
(its arguments, and its whole environment, so a test can prove a password never reached either); state lives in
$FAKE_STATE (JSON). Nothing here touches a real network."""
import json
import os
import sys

IW_INFO = ("Wiphy phy0\n\tSupported interface modes:\n\t\t * managed\n\tWoWLAN support:\n\t\t * wake up on disconnect\n"
           "%s\t\t * wake up on pattern match, up to 20 patterns of 16-128 bytes\n\tSupported commands:\n")


def main():
    cmd = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    path = os.environ["FAKE_STATE"]
    with open(path) as f:
        s = json.load(f)
    s.setdefault("log", []).append(" ".join([cmd] + args))
    s.setdefault("envs", []).append(json.dumps(dict(os.environ)))
    out, err, rc = "", "", 0
    if cmd == "iw":
        if s.get("iw_fail"):
            rc = 1
        elif args[:1] == ["phy"] and args[2:3] == ["info"]:
            out = IW_INFO % ("\t\t * wake up on magic packet\n" if s.get("magic", True) else "")
        elif args[:1] == ["phy"] and args[2:3] == ["wowlan"]:
            s["wowlan"] = args[3:]
        elif args[:1] == ["dev"] and args[2:3] == ["link"]:
            out = s.get("link", "Not connected.\n")
    elif cmd == "netplan":
        step = args[0] if args else ""
        if s.get("fail") == step:
            rc, err = 1, s.get("fail_text", "Error in network definition")
        elif step == "apply":
            s["applied"] = s.get("applied", 0) + 1
            for n in s.get("cut_wired", []):
                if s.get("cut_armed", True) and os.path.exists(os.path.join(s["cut_root"], n, "carrier")):
                    with open(os.path.join(s["cut_root"], n, "carrier"), "w") as f:
                        f.write("0\n")
    elif cmd == "ip":
        if "route" in args:
            out = s.get("routes", "")
        else:
            out = s.get("addr", "")
    elif cmd == "systemctl":
        rc = 0 if args[:1] == ["start"] else 1          # nothing is active or enabled
    elif cmd == "apt-get":
        if s.get("apt_fail"):
            rc = 100
        else:
            s.setdefault("installed", []).extend(a for a in args if not a.startswith("-") and a != "install")
    with open(path, "w") as f:
        json.dump(s, f)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(rc)


if __name__ == "__main__":
    main()
