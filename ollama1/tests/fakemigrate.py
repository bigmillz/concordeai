#!/usr/bin/env python3
"""Stand-ins for the programs tools/migrate-os.sh (lib/o1migrate.py) runs:
lsblk, blkid, findmnt, df, du, nvme, journalctl, systemctl, fuser, sgdisk,
wipefs, mkfs.*, mount, umount, rsync, chroot, efibootmgr, fallocate, ...
Every call is logged; the machine (disks, partitions, mounts, firmware
entries) lives in $FAKE_STATE as JSON and is changed the way the real
program would change it. Nothing here touches a real disk.

Failure injection, for the crash tests: state["kill"] is a list of
{"cmd", "nth", "when": "before"|"after"}; the nth call of that program kills
its PARENT (the migration tool) with SIGKILL, before or after doing its job:
a power cut. state["fail"] is a list of {"cmd", "nth", "rc"}: that call exits
with rc and does nothing."""
import fcntl
import json
import os
import re
import sys

GIB = 1 << 30
MIB = 1 << 20


def fu(c):
    return "-".join([c * 8, c * 4, c * 4, c * 4, c * 12])


def load():
    with open(os.environ["FAKE_STATE"]) as f:
        return json.load(f)


def save(s):
    tmp = os.environ["FAKE_STATE"] + ".%d.tmp" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, os.environ["FAKE_STATE"])


def new_uuid(s):
    s["uuid_n"] = s.get("uuid_n", 0) + 1
    n = s["uuid_n"]
    return "%08x-%04x-4000-8000-%012x" % (n, n, n)


def block_of(s, path):
    """('nvme1n1', None) for a disk, ('nvme1n1', 3) for its third partition, via the real path."""
    real = os.path.realpath(path)
    m = re.fullmatch(r"(nvme\d+n\d+)(?:p(\d+))?", os.path.basename(real))
    if not m or m.group(1) not in s["disks"]:
        return None, None
    return m.group(1), int(m.group(2)) if m.group(2) else None


def part_of(s, path):
    b, n = block_of(s, path)
    if b is None or n is None:
        return None
    for p in s["disks"][b]["parts"]:
        if p["n"] == n:
            return p
    return None


def drop_parts(s, block, dev_root):
    for p in s["disks"][block]["parts"]:
        f = os.path.join(dev_root, "%sp%d" % (block, p["n"]))
        if os.path.exists(f):
            os.remove(f)
    for n in os.listdir(s["byid"]):
        link = os.path.join(s["byid"], n)
        if "-part" in n and os.path.islink(link) and not os.path.exists(link):
            os.remove(link)
    s["disks"][block]["parts"] = []


def size_arg(v, rest):
    m = re.fullmatch(r"\+(\d+)GiB", v)
    if m:
        return int(m.group(1)) * GIB
    if v == "0":
        return rest
    raise SystemExit("fake sgdisk: size %s" % v)


def tree_size(path):
    t = 0
    for r, _, fs in os.walk(path):
        for f in fs:
            t += os.path.getsize(os.path.join(r, f))
    return t


def files_of(path):
    out = {}
    for r, _, fs in os.walk(path):
        for f in fs:
            p = os.path.join(r, f)
            out[os.path.relpath(p, path)] = os.path.getsize(p)
    return out


def lsblk(s, args):
    dev = args[-1]
    block, _ = block_of(s, dev)
    if block is None:
        return "", 1
    d = s["disks"][block]
    dr = s["devroot"]

    def mps(path):
        res = [m["target"] for m in s["mounts"] if os.path.realpath(m["source"]) == os.path.realpath(path)]
        if path in s.get("swaps", []):
            res.append("[SWAP]")
        return res or [None]
    kids = []
    for p in d["parts"]:
        path = os.path.join(dr, "%sp%d" % (block, p["n"]))
        node = {"name": "%sp%d" % (block, p["n"]), "path": path, "type": "part", "size": p["size"],
                "fstype": p.get("type"), "mountpoints": mps(path), "serial": None, "model": None}
        for ex in d.get("extra", []):
            if ex["part"] == p["n"]:
                node["children"] = [{"name": ex["name"], "path": ex["path"], "type": ex["type"], "size": p["size"],
                                     "fstype": ex.get("fstype", "ext4"), "mountpoints": mps(ex["path"]),
                                     "serial": None, "model": None}]
        kids.append(node)
    top = {"name": block, "path": os.path.join(dr, block), "type": "disk", "size": d["size"], "fstype": None,
           "mountpoints": [None], "serial": d["serial"], "model": d["model"]}
    if kids:
        top["children"] = kids
    return json.dumps({"blockdevices": [top]}), 0


def rsync(s, args):
    pos = [a for a in args if not a.startswith("-")]
    src, dst = pos[-2], pos[-1]
    dry = any(a.startswith("-") and not a.startswith("--") and "n" in a for a in args) or "-n" in args
    excl = [a[len("--exclude="):] for a in args if a.startswith("--exclude=")]
    skip = [e.strip("/").replace("/*", "") for e in excl]
    sfiles = files_of(src)
    sfiles = {k: v for k, v in sfiles.items() if not any(k == e or k.startswith(e + os.sep) for e in skip)}
    if dry:
        dfiles = files_of(dst) if os.path.isdir(dst) else {}
        out = []
        for k in sorted(sfiles):
            if dfiles.get(k) != sfiles[k]:
                out.append(">f+++++++++ %s" % k)
        return "\n".join(out), 0
    import shutil
    os.makedirs(dst, exist_ok=True)
    for k in sfiles:
        t = os.path.join(dst, k)
        os.makedirs(os.path.dirname(t), exist_ok=True)
        shutil.copy2(os.path.join(src, k), t)
    return "", 0


def efi_text(s):
    e = s["efi"]
    lines = ["BootCurrent: 0001", "Timeout: 1 seconds"] + (["BootNext: " + e["next"]] if e.get("next") else []) + [
        "BootOrder: " + ",".join(e["order"])]
    for n in sorted(e["entries"]):
        x = e["entries"][n]
        path = x["path"]
        if e.get("bare"):                              # some firmware/efibootmgr versions: no File(...), data after the path
            path = re.sub(r"File\(([^)]*)\)", lambda m: m.group(1) + e.get("trail", "0000424f"), path)
        lines.append("Boot%s%s %s%s%s" % (n, "*" if x.get("active", True) else "", x["label"], e.get("sep", "\t"), path))
    return "\n".join(lines)


def main():
    cmd = os.path.basename(sys.argv[0])
    args = sys.argv[1:]
    lock = open(os.environ["FAKE_STATE"] + ".lock", "w")      # the inhibitor runs beside the tool's own calls
    fcntl.flock(lock, fcntl.LOCK_EX)
    s = load()
    s.setdefault("log", []).append(" ".join([cmd] + args))
    nth = sum(1 for l in s["log"] if l.split()[0] == cmd)
    for f in s.get("fail", []):
        if f["cmd"] == cmd and f["nth"] == nth:
            save(s)
            sys.stderr.write("fake %s: injected failure\n" % cmd)
            sys.exit(f["rc"])
    kills = [k for k in s.get("kill", []) if k["cmd"] == cmd and k["nth"] == nth]
    if any(k["when"] == "before" for k in kills):
        save(s)
        import signal
        os.kill(os.getppid(), signal.SIGKILL)
        sys.exit(137)
    out, rc = "", 0
    dr = s["devroot"]
    if cmd == "lsblk":
        out, rc = lsblk(s, args)
    elif cmd == "findmnt":
        out = "\n".join("%s %s %s %s" % (m["target"].replace(" ", "\\x20"), m["source"], m["fstype"], m["options"])
                        for m in s["mounts"])
    elif cmd == "df":
        col = [a for a in args if a.startswith("--output=")][0].split("=")[1]
        v = s["df"].get(args[-1], {}).get(col)
        if v is None:
            rc = 1
        else:
            out = "%s\n%d" % (col.capitalize(), v)
    elif cmd == "du":
        out = "%d\t%s" % (tree_size(args[-1]), args[-1])
    elif cmd == "blkid":
        key, dev = args[args.index("-s") + 1], args[-1]
        p = part_of(s, dev)
        v = (p or {}).get({"TYPE": "type", "UUID": "uuid", "PARTUUID": "partuuid", "LABEL": "label"}[key])
        if v:
            out = v
        else:
            rc = 2
    elif cmd == "nvme":
        ctrl = os.path.basename(args[-1])
        out = "Smart Log for NVME device:%s\ncritical_warning                    : %d\ntemperature : 40 C" % (
            ctrl, s["smart"].get(ctrl, 0))
    elif cmd == "journalctl":
        out = s.get("klog", "")
    elif cmd == "chattr":
        im = s.setdefault("immutable", [])
        if args[0] == "+i" and args[-1] not in im:
            im.append(args[-1])
        elif args[0] == "-i" and args[-1] in im:
            im.remove(args[-1])
    elif cmd == "systemctl":
        if args[0] == "list-units":
            out = "\n".join("%s loaded active running x" % u for u in s["services_active"] if u.startswith("ollama1-pull@"))
        elif args[0] == "is-active":
            rc = 0 if args[-1] in s["services_active"] else 3
        elif args[0] == "stop" and args[1] in s["services_active"]:
            s["services_active"].remove(args[1])
    elif cmd == "fuser":
        rc = 0 if s.get("dpkg_busy") else 1
    elif cmd == "sgdisk":
        dev = args[-1]
        block, _ = block_of(s, dev)
        if block is None:
            rc = 3
        elif "--zap-all" in args:
            drop_parts(s, block, dr)
        else:
            d = s["disks"][block]
            used = 2 * MIB
            specs = {}
            for a in args[:-1]:
                m = re.fullmatch(r"-([ntc])(\d+)(?::(.*))?", a)
                if m:
                    specs.setdefault(int(m.group(2)), {})[m.group(1)] = m.group(3)
            for n in sorted(specs):
                sp = specs[n]
                start, end = sp["n"].split(":")
                size = size_arg(end, d["size"] - used)
                stale = d.get("stale", {}).get(str(n))      # an old filesystem's superblock where this partition starts
                part = {"n": n, "size": size, "type": stale, "uuid": None, "partuuid": new_uuid(s), "label": None,
                        "gpt_name": sp.get("c"), "gpt_type": sp.get("t"), "stale": bool(stale)}
                used += size
                d["parts"].append(part)
                f = os.path.join(dr, "%sp%d" % (block, n))
                open(f, "w").close()
                link = "%s-part%d" % (dev, n)
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(f, link)
    elif cmd == "wipefs":
        block, n = block_of(s, args[-1])
        if block is not None and n is None:
            drop_parts(s, block, dr)
        elif block is not None:
            pt = part_of(s, args[-1])
            if pt is not None:
                pt["type"], pt["uuid"], pt["stale"] = None, None, False
    elif cmd in ("mkfs.ext4", "mkfs.vfat"):
        p = part_of(s, args[-1])
        if p is None:
            rc = 1
        else:
            if not p.get("stale") or cmd == "mkfs.ext4":
                p["type"] = "ext4" if cmd == "mkfs.ext4" else "vfat"
            p["uuid"] = new_uuid(s)                 # (an unwiped old signature stays: the probe then reads the old type)
            for flag in ("-L", "-n"):
                if flag in args:
                    p["label"] = args[args.index(flag) + 1]
    elif cmd == "mount":
        if "--make-rslave" in args:
            pass
        elif "-t" in args:
            s["mounts"].append({"target": args[-1], "source": args[-2], "fstype": args[args.index("-t") + 1], "options": "rw"})
        elif not os.path.isdir(args[-1]):
            rc = 32
        else:
            src = args[-2]
            p = part_of(s, src)
            s["mounts"].append({"target": args[-1], "source": os.path.realpath(src) if os.path.exists(src) else src,
                                "fstype": (p or {}).get("type") or "none", "options": "rw"})
    elif cmd == "umount":
        t = args[-1]
        rec = "-R" in args
        hit = [m for m in s["mounts"] if m["target"] == t or (rec and m["target"].startswith(t + "/"))]
        if not hit or t in s.get("busy", []):
            rc = 32
        else:
            s["mounts"] = [m for m in s["mounts"] if m not in hit]
    elif cmd == "rsync":
        out, rc = rsync(s, args)
    elif cmd == "chroot":
        root = args[0]
        inner = " ".join(args[1:])
        if "update-grub" in inner:
            cur = ""
            import glob
            files = [root + "/etc/default/grub"] + sorted(glob.glob(root + "/etc/default/grub.d/*.cfg"))
            for fn in files:
                if os.path.isfile(fn):
                    for line in open(fn):
                        m = re.match(r'GRUB_CMDLINE_LINUX_DEFAULT="(.*)"', line.strip())
                        if m:
                            cur = m.group(1).replace("$GRUB_CMDLINE_LINUX_DEFAULT", cur).strip()
            os.makedirs(root + "/boot/grub", exist_ok=True)
            with open(root + "/boot/grub/grub.cfg", "w") as fh:
                fh.write("set timeout=5\nmenuentry 'Ubuntu' {\n\tlinux\t/vmlinuz-6 root=UUID=x ro  %s\n}\n" % cur)
        if "grub-install" in inner:
            eid = re.search(r"--bootloader-id=(\S+)", inner).group(1)
            if not any(m["target"] == root + "/boot/efi" for m in s["mounts"]):
                sys.stderr.write("fake grub-install: the ESP is not mounted\n")
                rc = 1
            else:
                d = os.path.join(root, "boot/efi/EFI", eid)
                os.makedirs(d, exist_ok=True)
                for n in ("shimx64.efi", "grubx64.efi", "mmx64.efi", "grub.cfg"):
                    if n == "grub.cfg" and s.get("no_grub_cfg"):
                        continue
                    with open(os.path.join(d, n), "w") as fh:
                        fh.write(n)
    elif cmd == "efibootmgr":
        e = s["efi"]
        if not args or args == ["-v"]:
            out = efi_text(s)
        elif args[0] == "-c":
            dev = args[args.index("-d") + 1]
            block, _ = block_of(s, dev)
            pu = [p for p in s["disks"][block]["parts"] if p["n"] == int(args[args.index("-p") + 1])][0]["partuuid"]
            if s.get("efi_make_wrong_esp"):
                pu = "-".join(["9" * 8, "9" * 4, "9" * 4, "9" * 4, "9" * 12])
            n = "%04X" % (max([int(x, 16) for x in e["entries"]] + [0]) + 1)
            e["entries"][n] = {"label": args[args.index("-L") + 1], "active": True,
                               "path": "HD(1,GPT,%s,0x800,0x200000)/File(%s)" % (pu, args[args.index("-l") + 1])}
            e["order"].insert(0, n)
            out = efi_text(s)
        elif args[0] == "-o":
            e["order"] = args[1].split(",")
        elif args[0] == "-n":
            e["next"] = args[1]
        elif args[0] == "-N":
            e["next"] = None
        elif args[0] in ("-A", "-a"):
            e["entries"][args[-1]]["active"] = args[0] == "-a"
    elif cmd == "rm":
        if os.path.isdir(args[-1]):
            import shutil
            shutil.rmtree(args[-1])
    elif cmd == "tmux":
        if args[0] == "has-session":
            rc = 0 if args[-1] in s.get("tmux_sessions", []) else 1
        elif args[0] == "new-session":
            s.setdefault("tmux_sessions", []).append(args[args.index("-s") + 1])
            if s.get("tmux_run"):
                save(s)
                import subprocess
                fcntl.flock(lock, fcntl.LOCK_UN)             # the session's own commands take the lock themselves
                r = subprocess.run(args[-1], shell=True, capture_output=True, text=True)
                fcntl.flock(lock, fcntl.LOCK_EX)
                s = load()
                s["tmux_child"] = {"rc": r.returncode, "out": r.stdout + r.stderr}
                s["tmux_sessions"] = [x for x in s["tmux_sessions"] if x != args[args.index("-s") + 1]]
    elif cmd == "fallocate":
        with open(args[-1], "wb") as fh:
            fh.truncate(int(args[args.index("-l") + 1]))
    # partprobe, udevadm, sync, mkswap: nothing to do but be logged
    for h in s.get("hook", []):
        if h["cmd"] == cmd and h["nth"] == nth:
            for link, target in h.get("repoint", []):      # udev glitch: a by-id name now points elsewhere
                os.remove(link)
                os.symlink(target, link)
            if h.get("dst_remove") and cmd == "rsync" and not any(a.startswith("-") and "n" in a and not a.startswith("--")
                                                                  for a in args):
                os.remove(os.path.join(args[-1], h["dst_remove"]))   # a copy that lost a file
            if h.get("dst_corrupt") and cmd == "rsync" and not any(a.startswith("-") and "n" in a and not a.startswith("--")
                                                                   for a in args):
                fn = os.path.join(args[-1], h["dst_corrupt"])
                with open(fn, "rb") as fh:
                    data = bytearray(fh.read())
                data[0] ^= 0x01                                    # the same size, one bit different
                with open(fn, "wb") as fh:
                    fh.write(bytes(data))
    save(s)
    if any(k["when"] == "after" for k in kills):
        import signal
        os.kill(os.getppid(), signal.SIGKILL)
        sys.exit(137)
    if out:
        print(out)
    if rc:
        sys.stderr.write("fake %s: exit %d\n" % (cmd, rc))
    sys.exit(rc)


if __name__ == "__main__":
    main()
