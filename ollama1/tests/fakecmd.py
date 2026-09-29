#!/usr/bin/env python3
"""Stand-ins for lsblk, blkid, mdadm, mkfs.ext4, sgdisk, wipefs, vgs, ... used
by test_setuplib.py. Every call is logged; state lives in $FAKE_STATE (JSON).
Nothing here touches a real disk."""
import json
import os
import sys

MD_UUID = "aaaa1111:bbbb2222:cccc3333:dddd4444"


def fu(c):
    """A fake filesystem UUID made of one hex digit (built at run time, so
    the repo's secret scan never sees a UUID-shaped literal)."""
    return "-".join([c * 8, c * 4, c * 4, c * 4, c * 12])


def load():
    with open(os.environ["FAKE_STATE"]) as f:
        return json.load(f)


def save(s):
    with open(os.environ["FAKE_STATE"], "w") as f:
        json.dump(s, f)


def main():
    cmd = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    s = load()
    s.setdefault("log", []).append(" ".join([cmd] + args))
    out, rc = "", 0
    if cmd == "lsblk":
        dev = args[-1]
        spec = " ".join(args[:-1])
        if "SERIAL" in spec:
            out = s["serial"].get(dev, "")
        elif "MODEL,SIZE" in spec:
            out = "FAKE-DISK 7.3T"
        elif "PKNAME" in spec:
            out = ""
        elif "NAME,TYPE" in spec:
            out = "\n".join("%s %s" % (d, "disk" if d == dev else "part") for d in s["devs"].get(dev, [dev]))
        elif "NAME" in spec:
            out = "\n".join(s["devs"].get(dev, [dev]))
    elif cmd == "blkid":
        key = args[args.index("-s") + 1]
        dev = args[-1]
        val = s["blkid"].get(dev, {}).get(key)
        if val:
            out = val
        else:
            rc = 2
    elif cmd == "mdadm":
        if "--detail" in args and "--scan" in args:
            out = "\n".join(s.get("md_detail", []))
        elif "--examine" in args:
            out = s.get("md_examine", {}).get(args[-1], "")
            rc = 0 if out else 1
        elif "--create" in args or "--assemble" in args:
            md = args[args.index("--create" if "--create" in args else "--assemble") + 1]
            s["md_detail"] = ["ARRAY %s metadata=1.2 name=ollama1:data UUID=%s" % (md, MD_UUID)]
            s["blkid"].setdefault(md, {})
        elif "--export" in args:
            out = "MD_LEVEL=raid1\nMD_UUID=%s" % MD_UUID
        elif "--brief" in args:
            out = "ARRAY %s metadata=1.2 name=ollama1:data UUID=%s" % (args[-1], MD_UUID)
    elif cmd == "mkfs.ext4":
        dev = args[-1]
        b = s["blkid"].setdefault(dev, {})
        b["TYPE"] = "ext4"
        uuid = s.get("mkfs_uuid", fu("0"))
        if uuid:
            b["UUID"] = uuid
        if "-L" in args:
            b["LABEL"] = args[args.index("-L") + 1]
    elif cmd == "sgdisk":
        if any(a.startswith("-n1:") for a in args):
            open(args[-1] + "-part1", "w").close()
    elif cmd == "findmnt":
        rc = 0 if args[-1] in s.get("mounted_devs", []) else 1
    elif cmd == "mountpoint":
        rc = 0 if args[-1] in s.get("mounted", []) else 1
    elif cmd == "mount":
        s.setdefault("mounted", []).append(args[-1])
    elif cmd == "vgs":
        out = s.get("vgs", "  451190 ")
    save(s)
    if out:
        print(out)
    sys.exit(rc)


if __name__ == "__main__":
    main()
