#!/usr/bin/env python3
"""Stand-ins for lsblk, findmnt, mdadm, umount, systemctl, fuser and update-initramfs, used by
test_removeraid.py. Every call is logged; the machine lives in $FAKE_STATE (JSON). Nothing here
touches a real disk or array."""
import json
import os
import sys


def load():
    with open(os.environ["FAKE_STATE"]) as f:
        return json.load(f)


def save(s):
    with open(os.environ["FAKE_STATE"], "w") as f:
        json.dump(s, f)


def disk_of(s, dev):
    if dev in s["disks"]:
        return dev
    return s["parts"].get(dev)


def main():
    cmd = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    s = load()
    s.setdefault("log", []).append(" ".join([cmd] + args))
    out, rc = "", 0
    md = s.get("md", {})
    if cmd == "lsblk":
        flags = args[0] if args else ""
        if flags == "-dno" and args[1] == "NAME,SERIAL":
            out = "\n".join("%s %s" % (d[5:], v["serial"]) for d, v in sorted(s["disks"].items()))
        elif flags == "-dno" and args[1] == "SERIAL":
            out = s["disks"].get(args[2], {}).get("serial", "")
            if s.get("serial_changes_after_stop") and not any(v.get("running") for v in md.values()):
                out = "SERIAL-CHANGED"                      # a different disk answers under that name now
            rc = 0 if out else 1
        elif flags == "-lsnpo":
            dev = args[-1]
            d = disk_of(s, dev)
            if d is None:
                rc = 1
            else:
                out = ("%s part\n" % dev if dev != d else "") + "%s disk" % d
    elif cmd == "findmnt":
        mp = s.get("mount", {}).get(args[-1])
        if mp is None:
            rc = 1
        elif args[1] == "SOURCE":
            out = mp["source"]
        elif args[1] == "TARGET":
            out = args[-1]
        else:
            out = "%s %s %s %s" % (args[-1], mp["source"], mp["fstype"], mp["options"])
    elif cmd == "mdadm":
        if args[:2] == ["--detail", "--scan"]:
            out = "\n".join("ARRAY %s metadata=1.2 name=%s UUID=%s" % (k, v["name"], v["uuid"])
                            for k, v in sorted(md.items()) if v.get("running"))
        elif args[0] == "--detail":
            dev = args[-1]
            if dev not in md or not md[dev].get("running"):
                rc = 1
            elif "--export" in args:
                lines = ["MD_LEVEL=raid1", "MD_UUID=" + md[dev]["uuid"], "MD_NAME=" + md[dev]["name"]]
                for m in md[dev]["members"]:
                    lines.append("MD_DEVICE_%s_DEV=%s" % (os.path.basename(m), m))
                out = "\n".join(lines)
        elif args[0] == "--stop":
            dev = args[-1]
            if any(m["source"] == dev for m in s.get("mount", {}).values()):
                rc = 1
                sys.stderr.write("mdadm: Cannot get exclusive access to %s\n" % dev)
            elif s.get("stop_fails"):
                rc = 1
            else:
                md[dev]["running"] = False
        elif args[0] == "--zero-superblock":
            dev = args[-1]
            if any(v.get("running") and dev in v["members"] for v in md.values()):
                rc = 1
            else:
                s["superblock"][dev] = False
        elif args[0] == "--examine":
            rc = 0 if s["superblock"].get(args[-1]) else 1
    elif cmd == "umount":
        if s.get("umount_fail"):
            rc = 32
        else:
            s["mount"].pop(args[-1], None)
    elif cmd == "fuser":
        rc = 0 if s.get("fuser_busy") else 1
    save(s)
    if out:
        print(out)
    sys.exit(rc)


if __name__ == "__main__":
    main()
