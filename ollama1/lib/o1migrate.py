#!/usr/bin/python3
"""Move the server's operating system from one NVMe drive to the other, in
software, keeping the old drive untouched (and bootable) as the fallback.
Run through tools/migrate-os.sh (root check, path check, installer):

    sudo bash migrate-os.sh --from-serial <S> --to-serial <S> [--root-size 300G]
                            [--plan | --run | --resume | --finish] [--reboot]
                            [--confirm-serial <S>]

Why a copy and not "pvmove" (decided, 6b362). The OS drive holds LVM (ubuntu-vg).
Moving it with vgextend + pvmove would put every extent of / on the new drive
and rewrite the volume group's metadata ON THE OLD DRIVE while it runs. The old
drive would then boot only while the new one is also present, it would no longer
hold a system as it was, and a drive that drops off the bus mid-move leaves a
volume group with a missing disk. So the old drive is never written: / is
copied file by file (rsync) onto a plain ext4 partition of the new drive, with
its own new UUIDs, an fstab rewritten ON THE COPY, and its own initramfs and
GRUB, built in a chroot. The old drive's partitions, LVM, fstab and boot loader
are exactly what they were; the firmware entry for the new drive is made LAST,
first in the order with the old entry second, so a boot that fails falls back
to the old drive, and until that last stage the old drive is the default boot.

Layout made on the TO drive (GPT, one drive, nothing LVM):
    1  ESP      1 GiB   vfat  (the new boot loader; the old ESP is NOT copied:
                              its stub points at the old /boot)
    2  /boot    2 GiB   ext4
    3  /        --root-size (default 300G)   ext4
    4  models   the rest      ext4 label o1models  (mounted at /srv/models)

Stages (each records its completion in STATE_DIR on /srv/data, never on an NVMe,
so a crash, hang or power cycle is picked up with --resume):
    0 preflight   read-only checks, then the kit's services that use the models stop
    1 park        /srv/models -> /srv/data/models-parked, checked by a dry run
    2 partition   the TO drive is wiped, partitioned and formatted
    3 copy        / and /boot copied, a fresh /swap.img made
    4 boot        fstab rewritten on the COPY, initramfs, GRUB (its kernel options checked)
    5 restore     the models copied back onto the new models partition
    6 firmware    the boot entry: new first, old second
    then          reboot (--reboot does it after a 10 s countdown), and --finish
--run and --resume ask for the TO drive's serial once (or take --confirm-serial,
which must equal it), then start themselves again in a detached tmux session
("migrate"), so a dropped SSH connection can't stop them. Progress is in
/srv/data/migrate-os.status (readable by everyone) and /srv/data/migrate-os.log.
Every drive is found by SERIAL (nvme0/nvme1 swap between boots) and addressed
only through /dev/disk/by-id; the serial is read again immediately before every
command that writes to a drive.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

OLD_BOOT_DIRS = ("ubuntu", "BOOT")      # the folders of the old ESP's EFI folder that --disable-old-boot-files puts aside
OFF_SUFFIX = ".off"
STAGES = ["preflight", "park", "partition", "copy", "boot", "restore", "firmware"]
STAGE_TEXT = {"preflight": "checks", "park": "park the models on the RAID", "partition": "partition and format the TO drive",
              "copy": "copy / and /boot", "boot": "fstab, initramfs and GRUB on the copy",
              "restore": "copy the models back", "firmware": "firmware boot entry (new first, old second)"}
SESSION = "migrate"
INSTALL_DIR = "/usr/local/lib/ollama1-migrate"
SUDOERS_FILE = "/etc/sudoers.d/90-ollama1-migrate"
SBIN = ("/usr/local/sbin", "/usr/sbin", "/sbin")
GIB = 1 << 30
MIB = 1 << 20
ESP_GIB, BOOT_GIB = 1, 2
MIN_ROOT_GIB = 30
BOOT_ID = "o1new"                       # the folder in the ESP and the label base
BOOT_LABEL = "ollama1-new"
SERVICES = ["ollama1-update-ollama.timer", "ollama1-update-ollama.service", "ollama1-models-sync.service",
            "ollama1-gateway.service", "ollama1-admin.service", "ollama1-restart.timer", "ollama1-restart.service",
            "ollama1-reboot.timer", "ollama1-reboot.service", "comfyui.service", "ollama.service",
            "apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service"]
PULL_UNITS = "ollama1-pull@*.service"
PANIC_DROPIN = "etc/default/grub.d/98-ollama1-migrate.cfg"
PANIC_OPTION = "panic=10"
TOOLS = {"lsblk": "util-linux", "blkid": "util-linux", "findmnt": "util-linux", "mount": "util-linux",
         "umount": "util-linux", "partprobe": "parted", "sgdisk": "gdisk", "wipefs": "util-linux",
         "mkfs.ext4": "e2fsprogs", "mkfs.vfat": "dosfstools", "mkswap": "util-linux", "fallocate": "util-linux",
         "rsync": "rsync", "nvme": "nvme-cli", "efibootmgr": "efibootmgr", "chroot": "coreutils",
         "df": "coreutils", "journalctl": "systemd", "systemctl": "systemd", "fuser": "psmisc",
         "udevadm": "udev", "sync": "coreutils", "chattr": "e2fsprogs"}
READ_ONLY = {"lsblk", "blkid", "findmnt", "df", "nvme", "journalctl", "fuser", "du"}
RSYNC_OK = (0, 24)                      # 24: a file vanished while it was read (a log); fine
DEAD_RX = re.compile(r"controller is down|CSTS=0xffffffff|Disabling device after reset failure|"
                     r"Removing after probe failure|device offline|controller is down; will reset")
ALIVE_RX = re.compile(r"default/read/poll queues|reset controller.*(done|success)|nvme_reset_work.*(done|success)")


class Abort(Exception):
    """A refusal or a failure: printed, exit 1."""


# ---- settings (environment overrides are for the tests) --------------------------------

class Cfg:
    def __init__(self, env=None, euid=None):
        env = os.environ if env is None else env
        euid = os.geteuid() if euid is None else euid
        # The test overrides below are honoured only for a user who is not root: a run as root (a copy run
        # through sudo) never takes a folder or a program from the environment.
        honour = euid != 0
        self.honour = honour

        def g(k, d):
            return env.get("O1M_" + k, d) if honour else d
        self.sys = g("SYS", "/sys")
        self.dev = g("DEV", "/dev")
        self.byid = g("BYID", "/dev/disk/by-id")
        self.data = g("DATA", "/srv/data")
        self.models = g("MODELS", "/srv/models")
        self.state_dir = g("STATE_DIR", self.data + "/o1migrate")
        self.parked = g("PARKED", self.data + "/models-parked")
        self.mnt = g("MNT", "/run/o1migrate")
        self.src_root = g("SRC_ROOT", "/")
        self.tty = g("TTY", "/dev/tty")
        self.efi_sys = g("EFI_SYS", "/sys/firmware/efi")
        self.bios_key = g("BIOS_KEY", "F11")
        self.cmdline = g("CMDLINE", "/proc/cmdline")
        self.mdstat = g("MDSTAT", "/proc/mdstat")
        self.drop_caches = g("DROP_CACHES", "/proc/sys/vm/drop_caches")
        self.countdown = int(g("COUNTDOWN_SECS", "10"))
        self.status = g("STATUS", self.data + "/migrate-os.status")
        self.log = g("LOG", self.data + "/migrate-os.log")
        self.heartbeat = float(g("HEARTBEAT_SECS", "15"))
        self.tmux = g("TMUX_BIN", "tmux")
        self.sudoers = g("SUDOERS", SUDOERS_FILE)
        self.new_root = self.mnt + "/root"
        self.new_models = self.mnt + "/models"


# ---- pure helpers (unit-tested) ------------------------------------------------------------

def parse_size_gib(text):
    """'300G' -> 300, '1T' -> 1024. A suffix is required (G or T)."""
    m = re.fullmatch(r"(\d{1,5})([GgTt])", text.strip())
    if not m:
        raise Abort("a size is a whole number with G or T, like 300G (got '%s')" % text)
    n = int(m.group(1)) * (1024 if m.group(2) in "Tt" else 1)
    if n < 1:
        raise Abort("a size must be above zero")
    return n


def human(n):
    for unit, size in (("TiB", 1 << 40), ("GiB", GIB), ("MiB", MIB)):
        if n >= size:
            return "%.1f %s" % (n / size, unit)
    return "%d B" % n


def valid_serial(s):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{4,40}", s or ""))


def layout(disk_bytes, root_gib):
    """The partitions to make on a disk of this size. Pure."""
    fixed = (ESP_GIB + BOOT_GIB + root_gib) * GIB
    models = disk_bytes - 2 * MIB - fixed          # 1 MiB at the front and the end
    return {"esp_gib": ESP_GIB, "boot_gib": BOOT_GIB, "root_gib": root_gib, "models_bytes": models}


def sgdisk_args(byid, root_gib):
    return ["sgdisk",
            "-n1:1MiB:+%dGiB" % ESP_GIB, "-t1:EF00", "-c1:o1-esp",
            "-n2:0:+%dGiB" % BOOT_GIB, "-t2:8300", "-c2:o1-boot",
            "-n3:0:+%dGiB" % root_gib, "-t3:8300", "-c3:o1-root",
            "-n4:0:0", "-t4:8300", "-c4:o1-models", byid]


def tree_nodes(node):
    yield node
    for c in node.get("children") or []:
        yield from tree_nodes(c)


def tree_mounts(node):
    out = set()
    for n in tree_nodes(node):
        for m in n.get("mountpoints") or []:
            if m:
                out.add(m)
    return out


def tree_holders(node):
    """Anything under a disk that is not a partition: LVM, md, crypt."""
    return [n for n in tree_nodes(node) if n.get("type") not in ("disk", "part")]


def rewrite_fstab(text, uuids):
    """The new root's fstab: / /boot /boot/efi and /srv/models point at the new
    partitions by UUID; every other line (/srv/data, swap, comments) is kept as
    it is. Returns (new text, [what changed]). Raises Abort on a fstab it can't
    rewrite safely."""
    for k in ("root", "boot", "esp", "models"):
        if not valid_uuid(uuids.get(k)):
            raise Abort("the %s filesystem UUID is not a UUID; not writing an fstab line from it" % k)
    want = {"/": uuids["root"], "/boot": uuids["boot"], "/boot/efi": uuids["esp"], "/srv/models": uuids["models"]}
    out, seen, changes = [], set(), []
    for line in text.splitlines():
        f = line.split()
        if not f or f[0].startswith("#") or len(f) < 2 or f[1] not in want:
            out.append(line)
            continue
        mp = f[1]
        if mp in seen:
            raise Abort("fstab has two active lines for %s; not guessing which one to rewrite" % mp)
        seen.add(mp)
        new0 = "UUID=" + want[mp]
        changes.append("%s: %s -> %s" % (mp, f[0], new0))
        out.append(re.sub(r"^(\s*)\S+", lambda m: m.group(1) + new0, line, count=1))
    if "/" not in seen:
        raise Abort("fstab has no line for /; refusing to write a new one blind")
    extra = {"/boot": "ext4 defaults 0 1", "/boot/efi": "vfat umask=0077 0 1",
             "/srv/models": "ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2"}
    for mp, rest in extra.items():
        if mp not in seen:
            out.append("UUID=%s %s %s" % (want[mp], mp, rest))
            changes.append("%s: added (it had no line)" % mp)
    return "\n".join(out) + "\n", changes


def chroot_commands(root):
    """The three commands that make the copy bootable, as argv lists. The
    firmware entry is made by efibootmgr afterwards (--no-nvram), so the order
    of the entries stays in this tool's hands."""
    base = ["chroot", root, "/usr/bin/env", "LANG=C", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
    return [base + ["update-initramfs", "-u", "-k", "all"],
            base + ["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi",
                    "--bootloader-id=" + BOOT_ID, "--recheck", "--no-nvram"],
            base + ["update-grub"]]


_DEVPATH_START = r"(?:PciRoot|ACPI|HD|VenHw|VenMedia|BBS|MAC|FvVol|Fv|USB|UsbClass|SATA|Sata|NVMe|Nvme|Pci|Scsi|Uri|IPv4|IPv6)\("
_BOOT_LINE = re.compile(r"^Boot([0-9A-Fa-f]{4})(\*?)\s*(.*)$")


def split_label(rest):
    """The text after "Boot0003* " -> (label, device path). efibootmgr -v puts a tab between them; a
    label can have spaces in it, and some versions print spaces instead of the tab, so without a tab the
    path starts at the first device-path node (HD(, PciRoot(, ...)."""
    if "\t" in rest:
        label, _, path = rest.partition("\t")
    else:
        m = re.search(r"\s(?=%s)" % _DEVPATH_START, rest)
        label, path = (rest[:m.start()], rest[m.end():]) if m else (rest, "")
    return label.strip(), path.strip()


def norm_loader(path):
    """A loader path as the firmware printed it -> \\EFI\\dir\\file.efi, without the data efibootmgr -v
    appends after it (Boot0003 style: `\\EFI\\BOOT\\BOOTX64.EFI0000424f`), slashes as backslashes."""
    p = (path or "").strip().replace("/", "\\")
    m = re.match(r"(?is)^(.*?\.efi)", p)
    return (m.group(1) if m else p).rstrip()


def same_loader(a, b):
    """FAT file names are not case sensitive."""
    return bool(a) and bool(b) and norm_loader(a).lower() == norm_loader(b).lower()


def _loader_of(path):
    m = re.search(r"File\(([^)]*)\)", path)
    if m:
        return norm_loader(m.group(1))
    m = re.search(r"\)/?(\\[^\s]*)\s*$", path) or re.search(r"(\\[^\s]*)\s*$", path)   # no File(): a bare path
    return norm_loader(m.group(1)) if m else ""


def parse_efi(text):
    """efibootmgr -v -> {'order': [...], 'next': num|None,
    'entries': {num: {'label','partuuid','active','loader'}}}. Entries with and without the `*`,
    a label followed by a tab (or only by spaces), a File(...) path or a bare one with trailing data,
    and the same entry listed more than once under different numbers are all read."""
    order, entries, nxt = [], {}, None
    for line in (text or "").splitlines():
        m = re.match(r"BootNext:\s*([0-9A-Fa-f]{4})\s*$", line)
        if m:
            nxt = m.group(1).upper()
            continue
        m = re.match(r"BootOrder:\s*(\S+)", line)
        if m:
            order = [x for x in m.group(1).split(",") if x]
            continue
        m = _BOOT_LINE.match(line.rstrip("\r"))
        if m:
            label, path = split_label(m.group(3))
            pu = re.search(r"HD\(\d+,GPT,([0-9A-Fa-f-]{36}),", path)
            entries[m.group(1).upper()] = {"label": label, "active": m.group(2) == "*",
                                           "partuuid": pu.group(1).lower() if pu else "",
                                           "loader": _loader_of(path)}
    return {"order": [o.upper() for o in order], "entries": entries, "next": nxt}


def entries_for(efi, partuuid, loader=None, label=None):
    """Entry numbers on that ESP (and, when given, with that loader / label), in boot-order order then
    number order: the same entry listed twice gives two numbers."""
    pu = (partuuid or "").lower()
    if not pu:
        return []
    hits = [n for n, e in efi["entries"].items() if e["partuuid"] == pu
            and (loader is None or same_loader(e["loader"], loader))
            and (label is None or e["label"] == label)]
    pos = {n: i for i, n in enumerate(efi["order"])}
    return sorted(hits, key=lambda n: (pos.get(n, len(pos)), n))


def find_old_entry(efi, from_esp_partuuid):
    """The firmware entry that boots the old drive: one whose ESP is the old drive's. Several can share
    the ESP (ubuntu's own, the fallback \\EFI\\BOOT one, duplicates): the distro's own loader first,
    then the earliest in the boot order. None if there is none."""
    hits = entries_for(efi, from_esp_partuuid)
    if not hits:
        return None
    own = [n for n in hits if "\\efi\\boot\\" not in efi["entries"][n]["loader"].lower()]
    return (own or hits)[0]


def boot_order(new, old, previous, efi=None, pu=None):
    """New entry first, the old drive's entry second, everything else after, no duplicates (and none of
    the new entry's own duplicates, when efi and the new ESP's partuuid are given)."""
    drop = set(entries_for(efi, pu, loader=efi["entries"][new]["loader"])) - {new} if efi and pu and new in efi["entries"] else set()
    order = [new]
    if old and old != new:
        order.append(old)
    for n in previous:
        if n not in order and n not in drop:
            order.append(n)
    return order


def boot_order_old_first(new, old, previous, efi=None, pu=None):
    """The order that keeps the old drive the default: the old entry first, the new one last, so only a
    BootNext (one boot) tries the new drive."""
    drop = set(entries_for(efi, pu, loader=efi["entries"][new]["loader"])) - {new} if efi and pu and new in efi["entries"] else set()
    rest = [n for n in previous if n not in (new, old) and n not in drop]
    return ([old] if old else []) + rest + [new]


def new_entry_number(efi, partuuid, loader=None):
    """The entry this tool made earlier (or that an earlier, half-finished run made), if any: the same
    ESP and the same loader path (any label, active or not). The one labelled ollama1-new is preferred
    when there are several; with the same entry listed twice, the lowest number is the one used."""
    hits = entries_for(efi, partuuid, loader=loader)
    if not hits and loader is not None:
        hits = entries_for(efi, partuuid, label=BOOT_LABEL)       # the label and the ESP alone: the loader name differs
    if not hits:
        return None
    mine = [n for n in hits if efi["entries"][n]["label"] == BOOT_LABEL]
    return sorted(mine or hits)[0]


def drive_dead(ctrl, state, root_opts, klog):
    """Is the drive dead right now? -> reason or ''. The controller's own state
    ('live') and a read-only root are the facts; the kernel log's last word
    about this controller (a drop-out with no recovery after it) is the same
    news from the other side."""
    if state != "live":
        return "the controller reports '%s', not 'live'" % (state or "nothing")
    if "ro" in (root_opts or "").split(","):
        return "/ has been remounted read-only (the drive dropped off the bus)"
    rx = re.compile(r"\b%s(n\d+(p\d+)?)?\b" % re.escape(ctrl))
    last = ""
    for line in (klog or "").splitlines():
        if not rx.search(line):
            continue
        if DEAD_RX.search(line):
            last = line.strip()
        elif ALIVE_RX.search(line):
            last = ""
    return "the kernel log's last word on it is: %s" % last[:160] if last else ""


IDENTITY_RX = re.compile(r"^(BOOT_IMAGE=|initrd=|root=|rootflags=|ro$|rw$)")


def cmdline_tokens(text):
    """The kernel options of a running system that must survive the move: every
    one except those that name the old kernel image, initrd and root."""
    return [t for t in (text or "").split() if not IDENTITY_RX.match(t)]


def grub_linux_tokens(grub_cfg_text):
    """The options on the first `linux` line of a grub.cfg (the default entry)."""
    for line in (grub_cfg_text or "").splitlines():
        m = re.match(r"^\s*linux\s+\S+\s*(.*)$", line)
        if m:
            return m.group(1).split()
    return []


def overdrive_token(dropin_text):
    m = re.search(r"amdgpu\.ppfeaturemask=0x[0-9a-fA-F]+", dropin_text or "")
    return m.group(0) if m else ""


UUID_RX = re.compile(r"[0-9A-Fa-f]{4,8}(-[0-9A-Fa-f]{4,12})+")
LOADER_RX = re.compile(r"[A-Za-z0-9._-]{1,40}\.efi")
ENTRY_RX = re.compile(r"[0-9A-Fa-f]{4}")


def valid_uuid(v):
    return isinstance(v, str) and bool(UUID_RX.fullmatch(v))


def validate_state(st):
    """Every value of the state file that later reaches fstab, efibootmgr or a command line, checked
    against what it can be. The state file is root's, but a damaged or edited one must not become a command."""
    def need(ok, what):
        if not ok:
            raise Abort("the state file has a bad %s; not using it" % what)
    need(valid_serial(st.get("from_serial")) and valid_serial(st.get("to_serial")), "serial")
    need(isinstance(st.get("root_gib"), int) and 1 <= st["root_gib"] <= 100000, "root size")
    for k, v in (st.get("uuids") or {}).items():
        need(k in ("esp", "boot", "root", "models") and valid_uuid(v), "filesystem UUID")
    for k in ("esp_partuuid", "old_esp_partuuid"):
        need(st.get(k, "") == "" or valid_uuid(st.get(k)), k)
    need(st.get("loader") is None or bool(LOADER_RX.fullmatch(str(st.get("loader")))), "boot loader name")
    e = st.get("efi") or {}
    for k in ("new", "old"):
        need(e.get(k) is None or bool(ENTRY_RX.fullmatch(str(e.get(k)))), "firmware entry number")
    for k in ("order", "previous_order"):
        need(all(isinstance(n, str) and ENTRY_RX.fullmatch(n) for n in e.get(k) or []), "firmware boot order")
    need(all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_.:=,/+@-]+", t) for t in st.get("cmdline_required") or []),
         "kernel option")
    return st


def raid_ok(mdstat, name):
    """'' if the md array is in mdstat with every member up ([UU]), else why not."""
    block, found = [], False
    for line in (mdstat or "").splitlines():
        if re.match(r"^%s\s*:" % re.escape(name), line):
            found, block = True, [line]
        elif found and line.strip() and not re.match(r"^md\d+\s*:", line):
            block.append(line)
        elif found:
            break
    if not found:
        return "%s is not in /proc/mdstat" % name
    m = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", "\n".join(block))
    if not m:
        return "cannot read the state of %s in /proc/mdstat" % name
    if m.group(3) != "U" * len(m.group(3)) or m.group(1) != m.group(2):
        return "the RAID %s is degraded [%s]: the parked models would be on one disk only" % (name, m.group(3))
    return ""


def dir_perm_problem(path, uid):
    """Why this folder can't hold files a root program trusts, or ''."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return "cannot look at %s (%s)" % (path, e.strerror)
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        return "%s is not a plain folder" % path
    if st.st_uid != uid:
        return "%s is not owned by root" % path
    if st.st_mode & 0o022:
        return "%s is writable by group or others" % path
    return ""


def open_nofollow(path, flags, mode=0o600):
    """os.open that never follows a symlink at the last component."""
    try:
        return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    except OSError as e:
        raise Abort("cannot open %s safely (%s)" % (path, e.strerror))


def sample_compare(src, dst, small=64 * MIB, chunk=MIB, samples=16):
    """Compare two trees by content without reading them twice in full: every file up to 64 MiB whole, a
    larger one by its first and last MiB and 16 more MiB at places picked from its name. Sizes are compared
    for all. -> (files, bytes compared); raises Abort on the first difference."""
    import random
    files = compared = 0
    for root, _, names in os.walk(src):
        for n in sorted(names):
            a = os.path.join(root, n)
            b = os.path.join(dst, os.path.relpath(a, src))
            if os.path.islink(a):
                continue
            try:
                sa, sb = os.path.getsize(a), os.path.getsize(b)
            except OSError:
                raise Abort("%s is missing from the parked copy" % os.path.relpath(a, src))
            if sa != sb:
                raise Abort("%s differs in size from the parked copy" % os.path.relpath(a, src))
            if sa <= small:
                spots = [(0, sa)]
            else:
                rnd = random.Random(os.path.relpath(a, src))
                spots = [(0, chunk), (sa - chunk, chunk)] + [(rnd.randrange(0, sa - chunk), chunk) for _ in range(samples)]
            with open(a, "rb") as fa, open(b, "rb") as fb:
                for off, ln in spots:
                    fa.seek(off)
                    fb.seek(off)
                    x, y = fa.read(ln), fb.read(ln)
                    compared += len(x)
                    if x != y:
                        raise Abort("%s differs from the parked copy (content)" % os.path.relpath(a, src))
            files += 1
    return files, compared


def critical_warning(text):
    m = re.search(r"^critical_warning\s*:\s*(\S+)", text or "", re.M)
    if not m:
        return None
    try:
        return int(m.group(1), 0)
    except ValueError:
        return None


def check_facts(f):
    """Every reason not to start, as sentences. Pure: f is a dict of facts the
    caller read from the machine."""
    p = []
    fs, ts = f["from_serial"], f["to_serial"]
    for what, s in (("--from-serial", fs), ("--to-serial", ts)):
        if not valid_serial(s):
            p.append("%s '%s' is not a serial number" % (what, s))
    if fs == ts:
        p.append("the two serials are the same; the drive to copy FROM and the drive to erase must differ")
    for role, s, d in (("FROM", fs, f["from_drive"]), ("TO", ts, f["to_drive"])):
        if d is None and valid_serial(s):
            p.append("no NVMe drive with the serial %s (%s) was found. The drives on this machine: %s. If the "
                     "FROM drive has dropped off the bus, power the server off at the switch for a minute, "
                     "start it and run --resume" % (s, role, ", ".join(f["serials_present"]) or "none"))
    if f["missing_tools"]:
        p.append("missing programs: %s. Install them: sudo apt install %s" % (
            ", ".join(sorted(f["missing_tools"])), " ".join(sorted(set(f["missing_tools"].values())))))
    if p:
        return p
    ft, tt = f["from_tree"], f["to_tree"]
    if "/" not in tree_mounts(ft):
        if "/" in tree_mounts(tt):
            p.append("/ is already on the TO drive: the new system is running. Run --finish instead")
        else:
            p.append("/ is not on the FROM drive (serial %s); nothing here would be copied from it" % fs)
    why = f["from_dead"]
    if why:
        p.append("the FROM drive is dead right now: %s. Power-cycle the server (off at the switch for a minute), "
                 "then run --resume (or --run)" % why)
    extra_from = sorted(m for m in tree_mounts(ft) if m not in ("/", "/boot", "/boot/efi", "[SWAP]"))
    if extra_from:
        p.append("the FROM drive has other mounted filesystems (%s): the copy keeps to one filesystem per mount "
                 "(rsync -x) and would silently leave them out. Unmount them or copy them by hand first" % ", ".join(extra_from))
    if f["fresh"]:
        if not f["models_from_to"]:
            p.append("%s is not mounted from a partition of the TO drive (serial %s). The models are what gets parked "
                     "and the drive is erased: this tool will not guess" % (f["models_path"], f["to_serial"]))
        if f["to_parts"] != 1:
            p.append("the TO drive has %d partitions; it must have exactly one (the models one) before it is erased" % f["to_parts"])
    allowed = f["to_allowed_mounts"]
    extra = sorted(m for m in tree_mounts(tt) if m not in allowed and not any(
        m.startswith(a.rstrip("/") + "/") for a in f["to_allowed_prefixes"]))
    if extra:
        p.append("the TO drive has mounted filesystems this tool doesn't know about: %s. Refusing to erase it"
                 % ", ".join(extra))
    holders = tree_holders(tt)
    if holders:
        p.append("something sits on the TO drive besides partitions (%s: LVM, RAID or encryption); refusing"
                 % ", ".join(sorted(h.get("name", "?") for h in holders)))
    for role, w in (("FROM", f["smart_from"]), ("TO", f["smart_to"])):
        if w is None:
            p.append("could not read the %s drive's health (nvme smart-log)" % role)
        elif w != 0:
            p.append("the %s drive reports a critical warning (0x%x); not touching a drive that says it is failing "
                     "in a way this tool wasn't written for" % (role, w))
    if not f["efi_boot"]:
        p.append("this machine did not boot in UEFI mode (%s is missing); this tool writes a UEFI boot entry"
                 % f["efi_path"])
    if f["state_mount_bad"]:
        p.append(f["state_mount_bad"])
    if f["perm_bad"]:
        p.append(f["perm_bad"])
    if f["raid_bad"]:
        p.append(f["raid_bad"])
    if f["dpkg_busy"]:
        p.append("apt/dpkg is running; wait for it, because the copy would catch it half way")
    root_gib = f["root_gib"]
    if root_gib < MIN_ROOT_GIB:
        p.append("--root-size %dG is too small; the least this tool makes is %dG" % (root_gib, MIN_ROOT_GIB))
    if f["root_used"] is not None and root_gib * GIB < f["root_used"] * 1.2:
        p.append("--root-size %dG is too small: / uses %s now and the copy needs 20%% more than that (%s). "
                 "Give a larger --root-size" % (root_gib, human(f["root_used"]), human(f["root_used"] * 1.2)))
    lay = layout(f["to_size"], root_gib) if f["to_size"] else None
    if lay and lay["models_bytes"] <= 0:
        p.append("--root-size %dG leaves nothing of the TO drive for the models" % root_gib)
    elif lay and f["models_used"] is not None and lay["models_bytes"] * 0.97 < f["models_used"] * 1.03:
        p.append("the models (%s) would not fit the models partition (%s after a %dG root). Lower --root-size, "
                 "or remove models you don't need first" % (human(f["models_used"]), human(lay["models_bytes"]), root_gib))
    if f["need_space"] and f["models_used"] is not None and f["data_avail"] is not None:
        margin = 10 * GIB + int(f["models_used"] * 0.02)
        if f["data_avail"] < f["models_used"] + margin:
            p.append("%s has %s free and parking the models needs %s (%s of models plus a margin)" % (
                f["data_path"], human(f["data_avail"]), human(f["models_used"] + margin), human(f["models_used"])))
    return p


def unsafe_path_reason(path, lstat=None):
    """Why a program running as root must not trust this path, or ''. Every
    component of its real path must be owned by root and writable by neither
    group nor others (a user who can change any of them changes what runs as
    root)."""
    lstat = lstat or os.lstat
    real = os.path.realpath(path)
    parts, cur = [], real
    while True:
        parts.append(cur)
        if cur == "/":
            break
        cur = os.path.dirname(cur) or "/"
    for part in reversed(parts):
        try:
            st = lstat(part)
        except OSError as e:
            return "cannot look at %s (%s)" % (part, e.strerror)
        if st.st_uid != 0:
            return "%s is not owned by root" % part
        if st.st_mode & 0o022:
            return "%s is writable by group or others" % part
    return ""


def sudoers_text(user):
    """The whole sudoers drop-in that lets one user run the installed tool as
    root without a password: one rule, one program, no wildcard, no SETENV, no
    other command."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user or "") or user in ("root", "ALL"):
        raise Abort("'%s' is not a user name this can write a sudoers rule for" % user)
    return ("# ollama1 migrate-os: lets %s run the system-migration tool as root, with no password, and nothing else.\n"
            "# It is temporary: remove it when the migration is done:  sudo rm %s\n"
            "%s ALL=(root) NOPASSWD: %s/migrate-os.sh\n" % (user, SUDOERS_FILE, user, INSTALL_DIR))


def render_status(f):
    """The status file's text. No serials, no secrets: it is world-readable."""
    lines = ["migrate-os status",
             "state:      %s" % f["state"],
             "stage:      %s" % f["stage"],
             "progress:   %s" % (f["percent"] if f["percent"] else "-"),
             "doing:      %s" % f["doing"],
             "started:    %s" % f["started"],
             "updated:    %s" % f["updated"],
             "boot id:    %s" % f["boot_id"],
             "next step:  %s" % f["next"],
             "",
             "If 'updated' stops moving for a few minutes while the state says RUNNING, the run was stopped",
             "(a reboot, a crash). Check with:  bash migrate-os.sh --status   Carry on with:  sudo bash migrate-os.sh --resume"]
    return "\n".join(lines) + "\n"


class Status:
    """The world-readable status file and the log, kept up to date while the
    run goes on (a thread rewrites it every few seconds, so the 'updated'
    time moves even through a step that prints nothing)."""

    PROGRESS_RX = re.compile(r"(\d{1,3})%\s+\S+/s")

    def __init__(self, cfg, redact=()):
        self.cfg = cfg
        self.redact = [(r, w) for r, w in redact if r]
        self.enabled = False
        self.lock = threading.RLock()
        self.buf = ""
        self.f = {"state": "RUNNING", "stage": "starting", "percent": "", "doing": "starting", "started": self.stamp(),
                  "updated": self.stamp(), "boot_id": self.boot_id(),
                  "next": "nothing to do; it runs by itself. Watch this file."}
        self.stop = threading.Event()
        self.thread = None
        self.logfh = None

    @staticmethod
    def stamp():
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def boot_id():
        try:
            with open("/proc/sys/kernel/random/boot_id") as fh:
                return fh.read().strip()
        except OSError:
            return "unknown"

    def clean(self, text):
        for r, w in self.redact:
            text = text.replace(r, w)
        return text

    def enable(self):
        fd = open_nofollow(self.cfg.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        self.logfh = os.fdopen(fd, "a")
        self.enabled = True
        self.write()

    def write(self):
        if not self.enabled:
            return
        with self.lock:
            self.f["updated"] = self.stamp()
            text = self.clean(render_status(self.f))
            tmp = self.cfg.status + ".new"
            fd = open_nofollow(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, self.cfg.status)

    def set(self, **kw):
        with self.lock:
            self.f.update(kw)
        self.write()

    def stage(self, name):
        n = STAGES.index(name)
        self.set(stage="%d of %d (%s)" % (n, len(STAGES) - 1, STAGE_TEXT[name]), percent="", doing=STAGE_TEXT[name])

    def feed(self, text):
        """Output of the run: the log gets all of it; the status file only the copy's percentage (a number)."""
        if self.logfh:
            try:
                self.logfh.write(text)
                self.logfh.flush()
            except OSError:
                pass
        self.buf += text
        pieces = re.split(r"[\r\n]", self.buf)
        self.buf = pieces.pop()
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            m = self.PROGRESS_RX.search(piece)
            if m:                    # only the number is taken from a command's output, never its words
                with self.lock:
                    self.f["percent"] = "%s%% of the current copy" % int(m.group(1))
        if pieces:
            self.write()

    def beat(self):
        while not self.stop.wait(self.cfg.heartbeat):
            try:
                self.write()
            except OSError:
                pass

    def start_heartbeat(self):
        if self.thread is None and self.enabled:
            self.thread = threading.Thread(target=self.beat, daemon=True)
            self.thread.start()

    def freeze(self):
        """Stop writing the status file (the log still gets the output)."""
        with self.lock:
            self.enabled = False
        self.stop.set()

    def finish(self, state, nxt):
        self.set(state=state, next=nxt, percent="")
        self.stop.set()


class Tee:
    """stdout/stderr that also feed the log and the status file."""

    def __init__(self, real, status):
        self.real, self.status = real, status

    def write(self, s):
        try:
            self.real.write(s)
        except (OSError, ValueError):
            pass
        self.status.feed(s)
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except (OSError, ValueError):
            pass

    def isatty(self):
        return self.real.isatty()

    def fileno(self):
        return self.real.fileno()


# ---- running commands ------------------------------------------------------------------------

class Drive:
    def __init__(self, serial, ctrl, block, dev, byid):
        self.serial, self.ctrl, self.block, self.dev, self.byid = serial, ctrl, block, dev, byid

    def part(self, n):
        """The by-id name of partition n. udev makes the -partN links for the plain name and for the
        one with a namespace suffix (_1); whichever exists is used."""
        names = ["%s-part%d" % (self.byid, n)]
        base = re.sub(r"_\d+$", "", self.byid)
        names.append("%s-part%d" % (base if base != self.byid else self.byid + "_1", n))
        for x in names:
            if os.path.exists(x):
                return x
        return names[0]


class Runner:
    def __init__(self, cfg, plan_only):
        self.cfg, self.plan_only = cfg, plan_only

    def run(self, argv, ro=False, ok=(0,), stream=False, input=None):
        if not ro:
            if self.plan_only:
                raise Abort("internal error: --plan was about to run a command that changes things (%s); stopped"
                            % shlex.join(argv))
            print("  + " + shlex.join(argv), flush=True)
        try:
            if stream:
                # the output goes through our own stdout (so into the log and the status file), as it arrives
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                tail = b""
                while True:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    tail = (tail + chunk)[-400:]
                    sys.stdout.write(chunk.decode("utf-8", "replace"))
                    sys.stdout.flush()
                rc = proc.wait()
                out, err = "", tail.decode("utf-8", "replace").strip()
            else:
                r = subprocess.run(argv, input=input, text=True, capture_output=True)
                rc, out, err = r.returncode, r.stdout or "", (r.stderr or "").strip()[-400:]
        except FileNotFoundError:
            raise Abort("the program %s is not installed" % argv[0])
        if rc not in ok:
            raise CommandFailed(argv, rc, err)
        return out


    def rc(self, argv):
        """Exit status of a read-only query."""
        try:
            return subprocess.run(argv, capture_output=True).returncode
        except FileNotFoundError:
            return 127


class CommandFailed(Abort):
    def __init__(self, argv, rc, err):
        self.argv, self.rc, self.err = argv, rc, err
        Abort.__init__(self, "%s failed (exit %d)%s" % (shlex.join(argv), rc, ": " + err if err else ""))


# ---- the migration -----------------------------------------------------------------------------

class Migrator:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.plan_only = args.mode == "plan"
        self.rn = Runner(cfg, self.plan_only)
        self.state = None
        self.lock_fd = None
        self.st = None
        self.tty_fh = None
        self.inhibitor = None
        self.cur_stage = None

    # -- finding drives (by serial, every time) ------------------------------------------------

    def serials_present(self):
        out = []
        for d in sorted(self.glob(os.path.join(self.cfg.sys, "class/nvme/nvme*"))):
            out.append(self.read(os.path.join(d, "serial")) or "?")
        return out

    @staticmethod
    def glob(pat):
        import glob
        return glob.glob(pat)

    @staticmethod
    def read(path):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def resolve(self, serial):
        """Drive for this serial, read fresh from sysfs and by-id. None if not there."""
        hits = [d for d in sorted(self.glob(os.path.join(self.cfg.sys, "class/nvme/nvme*")))
                if self.read(os.path.join(d, "serial")) == serial]
        if len(hits) > 1:
            raise Abort("%d NVMe controllers report the serial %s; refusing to guess which one is meant" % (len(hits), serial))
        for d in hits:
            ctrl = os.path.basename(d)
            blocks = []
            for n in sorted(os.listdir(d)):
                if re.fullmatch(r"nvme\d+n\d+", n):
                    blocks.append(n)
                else:                                     # native multipath: nvme<subsystem>c<ctrl>n<ns> is a path to nvme<subsystem>n<ns>
                    m = re.fullmatch(r"nvme(\d+)c\d+n(\d+)", n)
                    if m:
                        blocks.append("nvme%sn%s" % (m.group(1), m.group(2)))
            if not blocks:
                return None
            block = sorted(set(blocks))[0]
            dev = os.path.join(self.cfg.dev, block)
            byid = None
            for n in sorted(os.listdir(self.cfg.byid)) if os.path.isdir(self.cfg.byid) else []:
                if "-part" in n or not n.startswith("nvme-"):
                    continue
                if n.endswith("_" + serial) or re.search(r"_%s_\d+$" % re.escape(serial), n):
                    p = os.path.join(self.cfg.byid, n)
                    if os.path.realpath(p) == os.path.realpath(dev):
                        byid = p
                        break
            if not byid:
                raise Abort("drive %s has no /dev/disk/by-id path (udev not finished?); not touching it" % serial)
            return Drive(serial, ctrl, block, dev, byid)
        return None

    def check_target(self, serial, path):
        """Called right before a command that writes to a drive: the by-id path
        must belong to the drive with this serial AT THIS MOMENT."""
        base = re.sub(r"-part\d+$", "", path)
        if not base.startswith(self.cfg.byid + "/"):
            raise Abort("refusing to write to %s: drives are addressed only by /dev/disk/by-id" % path)
        d = self.resolve(serial)
        if d is None:
            raise Abort("the drive with serial %s is not there any more; stopped before writing" % serial)
        if os.path.realpath(base) != os.path.realpath(d.dev):
            raise Abort("serial check failed: %s is not the drive with serial %s; stopped before writing"
                        % (path, serial))
        return d

    def w(self, role, argv, path, **kw):
        """A command that writes to a drive: serial re-read first."""
        serial = self.args.to_serial if role == "to" else self.args.from_serial
        self.check_target(serial, path)
        return self.rn.run(argv, **kw)

    # -- reading the machine --------------------------------------------------------------------

    def lsblk(self, dev):
        out = self.rn.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,SERIAL,MODEL", dev], ro=True)
        try:
            return json.loads(out)["blockdevices"][0]
        except (ValueError, KeyError, IndexError):
            raise Abort("lsblk gave nothing usable for %s" % dev)

    def mounts(self):
        out = self.rn.run(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], ro=True)
        res = []
        for line in out.splitlines():
            f = line.split(None, 3)
            if len(f) == 4:
                res.append({"target": f[0].replace("\\x20", " "), "source": f[1], "fstype": f[2], "options": f[3]})
        return res

    @staticmethod
    def mount_of(mounts, path):
        best = None
        for m in mounts:
            t = m["target"].rstrip("/") or "/"
            if (path == t or path.startswith(t.rstrip("/") + "/") or t == "/") and (
                    best is None or len(t) > len(best["target"].rstrip("/") or "/")):
                best = m
        return best

    def df(self, path, col):
        out = self.rn.run(["df", "-B1", "--output=" + col, path], ro=True).split()
        try:
            return int(out[-1])
        except (ValueError, IndexError):
            return None

    def progress(self):
        s = self.state or {}
        return set(s.get("done", {})), set(s.get("started", {}))

    def gather(self):
        a = self.args
        done, started = self.progress()
        fd, td = self.resolve(a.from_serial), self.resolve(a.to_serial)
        mounts = self.mounts()
        f = {"from_serial": a.from_serial, "to_serial": a.to_serial, "from_drive": fd, "to_drive": td,
             "serials_present": self.serials_present(), "root_gib": a.root_gib, "from_tree": {}, "to_tree": {},
             "from_dead": "", "smart_from": 0, "smart_to": 0, "to_size": 0, "models_used": None,
             "root_used": None, "data_avail": None, "data_path": self.cfg.data, "need_space": "park" not in done,
             "efi_path": self.cfg.efi_sys, "efi_boot": os.path.isdir(self.cfg.efi_sys), "state_mount_bad": "",
             "dpkg_busy": False, "missing_tools": {t: pkg for t, pkg in TOOLS.items() if not shutil.which(t)},
             "mounts": mounts, "data_ok": False, "fresh": "partition" not in started, "models_from_to": False,
             "models_path": self.cfg.models, "to_parts": 0, "raid_bad": "", "perm_bad": ""}
        dm = [m for m in mounts if m["target"] == self.cfg.data]
        f["data_ok"] = bool(dm) and not re.search(r"nvme|ubuntu--vg", dm[-1]["source"])
        f["raid_bad"] = self.check_data_raid(mounts)
        uid = os.geteuid()
        f["perm_bad"] = dir_perm_problem(self.cfg.data, uid) if dm else ""
        if not f["perm_bad"] and os.path.lexists(self.cfg.state_dir):
            f["perm_bad"] = dir_perm_problem(self.cfg.state_dir, uid)
        # what the TO drive may have mounted: /srv/models (until it is wiped) and our own mount points
        f["to_allowed_mounts"] = {self.cfg.models}
        f["to_allowed_prefixes"] = [self.cfg.mnt]
        if not f["missing_tools"] and fd and td:
            f["from_tree"], f["to_tree"] = self.lsblk(fd.dev), self.lsblk(td.dev)
            f["to_size"] = int(f["to_tree"].get("size") or 0)
            f["models_from_to"] = self.cfg.models in tree_mounts(f["to_tree"])
            f["to_parts"] = len([n for n in f["to_tree"].get("children") or [] if n.get("type") == "part"])
            root = self.mount_of(mounts, "/")
            klog = self.rn.run(["journalctl", "-k", "-b", "0", "--no-pager", "-q"], ro=True, ok=(0, 1))
            f["from_dead"] = drive_dead(fd.ctrl, self.read(os.path.join(self.cfg.sys, "class/nvme", fd.ctrl, "state")),
                                        root["options"] if root else "", klog)
            f["smart_from"] = critical_warning(self.rn.run(["nvme", "smart-log", "/dev/" + fd.ctrl], ro=True, ok=(0, 1)))
            f["smart_to"] = critical_warning(self.rn.run(["nvme", "smart-log", "/dev/" + td.ctrl], ro=True, ok=(0, 1)))
            f["root_used"] = self.df("/", "used")
            if any(m["target"] == self.cfg.models for m in mounts):
                f["models_used"] = self.df(self.cfg.models, "used")
            elif "park" in done:
                f["models_used"] = (self.state or {}).get("parked_bytes")
            f["data_avail"] = self.df(self.cfg.data, "avail")
            sm = self.mount_of(mounts, self.cfg.state_dir)
            bad = tree_mounts(f["from_tree"]) | tree_mounts(f["to_tree"])
            if sm is None or sm["target"] == "/" or sm["target"] in bad:
                f["state_mount_bad"] = ("the state file's folder (%s) is on a NVMe drive or the root filesystem; it must be "
                                        "on %s (the RAID), which survives whatever happens to either NVMe"
                                        % (self.cfg.state_dir, self.cfg.data))
            elif sm["target"] != self.cfg.data:
                f["state_mount_bad"] = "%s is not on %s as expected (it is on %s)" % (self.cfg.state_dir, self.cfg.data, sm["target"])
            f["dpkg_busy"] = self.rn.rc(["fuser", "-s", "/var/lib/dpkg/lock-frontend"]) == 0
        f["data_ok"] = f["data_ok"] and not f["state_mount_bad"] and not f["perm_bad"]
        return f

    def check_data_raid(self, mounts):
        """'' if /srv/data is a mount point of a read-write md array with every member up, else why not.
        The parked models are on it, and a degraded mirror would leave them on one disk."""
        dm = [m for m in mounts if m["target"] == self.cfg.data]
        if not dm:
            return "%s is not a mount point; the models would be parked on the root filesystem" % self.cfg.data
        base = os.path.basename(os.path.realpath(dm[-1]["source"]))
        if not re.fullmatch(r"md\d+", base):
            return "%s is mounted from %s, not from the md mirror" % (self.cfg.data, dm[-1]["source"])
        if "rw" not in dm[-1]["options"].split(","):
            return "%s is not mounted read-write" % self.cfg.data
        try:
            with open(self.cfg.mdstat) as fh:
                text = fh.read()
        except OSError:
            return "cannot read /proc/mdstat"
        return raid_ok(text, base)

    # -- state file ---------------------------------------------------------------------------

    @property
    def state_path(self):
        return os.path.join(self.cfg.state_dir, "state.json")

    def load_state(self):
        if not os.path.lexists(self.state_path):
            return None
        fd = open_nofollow(self.state_path, os.O_RDONLY)
        try:
            with os.fdopen(fd) as fh:
                st = json.load(fh)
        except ValueError:
            raise Abort("%s is not readable JSON; not guessing. Look at it before doing anything else" % self.state_path)
        return validate_state(st)

    def ensure_state_dir(self):
        """The state folder, made only if /srv/data is a mount point (never silently on the root filesystem),
        and only if it and /srv/data belong to root and nobody else can write them."""
        c, uid = self.cfg, os.geteuid()
        if not os.path.lexists(c.state_dir):
            if not any(m["target"] == c.data for m in self.mounts()):
                raise Abort("refusing to create %s: %s is not a mount point, so it would land on the root filesystem"
                            % (c.state_dir, c.data))
            why = dir_perm_problem(c.data, uid)
            if why:
                raise Abort("refusing to create %s: %s" % (c.state_dir, why))
            os.mkdir(c.state_dir, 0o700)
        why = dir_perm_problem(c.state_dir, uid)
        if why:
            raise Abort("refusing to use the state folder: " + why)

    def save_state(self):
        self.ensure_state_dir()
        tmp = self.state_path + ".new"
        fd = open_nofollow(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.state, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def now(self):
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    def mark(self, kind, stage):
        self.state.setdefault(kind, {})[stage] = self.now()
        self.save_state()

    def take_lock(self):
        self.ensure_state_dir()
        self.lock_fd = os.fdopen(open_nofollow(os.path.join(self.cfg.state_dir, "lock"), os.O_WRONLY | os.O_CREAT, 0o600), "w")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Abort("another migrate-os is running (tmux session ollama1-migrate?)")

    # -- output ---------------------------------------------------------------------------------

    def cmdline(self, mode):
        a = self.args
        script = os.environ.get("O1_MIGRATE_SCRIPT") or self.sibling_script()
        # the installed copy is run by its full path (that is what a sudoers rule names); a checkout's by bash
        sudo = "sudo " + script if script.startswith("/") else "sudo bash " + script
        if mode in ("resume", "finish"):          # the serials are in the state file
            return "%s --%s" % (sudo, mode)
        return "%s --from-serial %s --to-serial %s --root-size %dG --%s" % (
            sudo, a.from_serial, a.to_serial, a.root_gib, mode)

    @staticmethod
    def sibling_script():
        """The migrate-os.sh next to this library (the installed copy), else just its name."""
        p = os.path.join(os.path.dirname(os.path.realpath(__file__)), "migrate-os.sh")
        return p if os.path.isfile(p) else "migrate-os.sh"

    def box(self, title, lines):
        print("\n" + "=" * 72)
        print(title)
        print("-" * 72)
        for l in lines:
            print(l)
        print("=" * 72, flush=True)

    def plan_text(self, f):
        a = self.args
        done, _ = self.progress()
        lay = layout(f["to_size"], a.root_gib) if f["to_size"] else None
        model = lambda t: (t.get("model") or "").strip() if t else ""
        L = []
        L.append("FROM (read only, never written): serial %s %s, %s" % (
            a.from_serial, model(f["from_tree"]), human(int(f["from_tree"].get("size") or 0)) if f["from_tree"] else "?"))
        L.append("TO   (ERASED and rewritten):    serial %s %s, %s" % (
            a.to_serial, model(f["to_tree"]), human(f["to_size"]) if f["to_size"] else "?"))
        L.append("")
        L.append("READ: / (%s used) and /boot of the FROM drive, /srv/models (%s) of the TO drive." % (
            human(f["root_used"]) if f["root_used"] is not None else "?",
            human(f["models_used"]) if f["models_used"] is not None else "?"))
        L.append("BACKED UP FIRST: /srv/models -> %s on the RAID (checked by a dry run and by size);" % self.cfg.parked)
        L.append("   it stays there until --finish --delete-parked.")
        L.append("ERASED: the whole TO drive. Its partitions are rebuilt:")
        if lay:
            L.append("   1  ESP    %d GiB vfat    2  /boot  %d GiB ext4    3  /  %d GiB ext4" % (lay["esp_gib"], lay["boot_gib"], lay["root_gib"]))
            L.append("   4  models %s ext4 (label o1models)" % human(lay["models_bytes"]))
        L.append("WRITTEN: a copy of / and /boot on the new partitions (a fresh /swap.img of the same size, not a copy);")
        L.append("   the copy's /etc/fstab (new UUIDs; the old fstab is not touched); its initramfs and GRUB;")
        L.append("   one firmware boot entry (%s), made last, used only for the NEXT boot (BootNext); the old drive stays the" % BOOT_LABEL)
        L.append("   default until --finish, so a power cycle after a bad first boot comes back to it; the new system has panic=10;")
        L.append("   the models copied back to the new models partition.")
        L.append("NEVER TOUCHED: the FROM drive: its partitions, LVM, fstab, boot loader and boot entry. If the new")
        L.append("   system doesn't boot, the firmware falls back to it, or pick it in the BIOS boot menu (%s)." % self.cfg.bios_key)
        L.append("STOPPED meanwhile: ollama, the gateway, the admin panel, model pulls, syncs and updates, the restart and reboot")
        L.append("   units, comfyui, apt's timers (they start again at the next boot). /srv/models is marked immutable while it is unmounted.")
        L.append("State file (on the RAID, not on either NVMe): %s" % self.state_path)
        L.append("Status (readable by everyone) and log: %s, %s" % (self.cfg.status, self.cfg.log))
        L.append("")
        L.append("Stages:")
        for i, s in enumerate(STAGES):
            L.append("   [%s] %d %s" % ("x" if s in done else " ", i, STAGE_TEXT[s]))
        L.append("   then: %s, and --finish on the new system" % (
            "the machine reboots by itself after a %d s countdown (--reboot)" % self.cfg.countdown if a.reboot
            else "you reboot (or run with --reboot to have it done)"))
        return L

    # -- preflight -------------------------------------------------------------------------------

    def preflight(self):
        f = self.gather()
        problems = check_facts(f)
        return f, problems

    def stop_services(self):
        stopped = self.state.setdefault("stopped", [])
        pulls = [l.split()[0] for l in self.rn.run(["systemctl", "list-units", "--plain", "--no-legend", "--state=active,activating",
                                                    PULL_UNITS], ro=True, ok=(0, 1)).splitlines() if l.split()]
        for u in SERVICES + pulls:
            if self.rn.rc(["systemctl", "is-active", "-q", u]) == 0:
                self.rn.run(["systemctl", "stop", u])
                if u not in stopped:
                    stopped.append(u)
        self.save_state()

    # -- asking (the terminal is opened once and read line by line) ---------------------------

    def ask(self, prompt):
        if getattr(self, "tty_fh", None) is None:
            try:
                self.tty_fh = open(self.cfg.tty)
            except OSError:
                raise Abort("no terminal to ask on; run it in a terminal (tmux)")
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return self.tty_fh.readline().strip()

    # -- the typed confirmation --------------------------------------------------------------

    def confirm(self, f):
        a = self.args
        print("\nEverything on the drive with serial %s (%s, %s) will be ERASED."
              % (a.to_serial, (f["to_tree"].get("model") or "").strip(), human(f["to_size"])))
        print("The models on it are copied to %s first. The drive with serial %s is not written to." % (self.cfg.parked, a.from_serial))
        if a.confirm_serial is not None:
            if a.confirm_serial != a.to_serial:
                raise Abort("--confirm-serial is not the serial of the drive to be erased; nothing was changed")
            print("Confirmed with --confirm-serial (no terminal needed).")
            return
        print("Type the serial of the drive to be erased to continue; anything else stops, changing nothing.")
        ans = self.ask("serial> ")
        if ans != a.to_serial:
            raise Abort("that is not the serial of the drive to be erased; nothing was changed")

    # -- mounts of the new system ---------------------------------------------------------------

    def ensure_mounted(self, part, target, extra=()):
        """Mount the partition at the target unless it already is, from that very
        partition. Anything else mounted there is a stop."""
        os.makedirs(target, exist_ok=True)
        cur = [m for m in self.mounts() if m["target"] == target]
        if cur:
            if os.path.realpath(cur[-1]["source"]) != os.path.realpath(part):
                raise Abort("%s is mounted from %s, not from %s; stopped" % (target, cur[-1]["source"], part))
            return
        self.rn.run(["mount"] + list(extra) + [part, target])

    def verify_mounted(self, part, target):
        cur = [m for m in self.mounts() if m["target"] == target]
        if not cur or os.path.realpath(cur[-1]["source"]) != os.path.realpath(part):
            raise Abort("%s is not mounted from %s; refusing to copy into it" % (target, part))

    def drive_to(self):
        d = self.resolve(self.args.to_serial)
        if d is None:
            raise Abort("the TO drive (serial %s) is not there" % self.args.to_serial)
        return d

    def unmount_new(self):
        mnt = self.cfg.mnt
        if mnt.rstrip("/") in ("", self.cfg.src_root.rstrip("/"), self.cfg.models, self.cfg.data):
            raise Abort("refusing to unmount under %s" % mnt)

        def live():
            return [m["target"] for m in self.mounts() if m["target"] == mnt or m["target"].startswith(mnt + "/")]
        if live():
            self.rn.run(["sync"])
            self.rn.run(["umount", "-R", self.cfg.new_root], ok=(0, 32))
            self.rn.run(["umount", self.cfg.new_models], ok=(0, 32))
            if live():
                raise Abort("could not unmount %s (still mounted: %s)" % (mnt, ", ".join(live())))

    # -- stage 1: park the models ---------------------------------------------------------------

    def rsync_models(self, src, dst, dry):
        flags = "-aHAXn" if dry else "-aHAX"
        argv = ["rsync", flags, "--numeric-ids"] + (["--itemize-changes"] if dry else ["--info=progress2,stats1"])
        if not dry and self.args.bwlimit:
            argv.append("--bwlimit=%d" % self.args.bwlimit)
        return argv + [src.rstrip("/") + "/", dst.rstrip("/") + "/"]

    def stage_park(self):
        c = self.cfg
        m = [x for x in self.mounts() if x["target"] == c.models]
        if not m:
            raise Abort("%s is not mounted, so there is nothing to park. If the models were parked earlier, the state "
                        "file would say so: it doesn't" % c.models)
        os.makedirs(c.parked, exist_ok=True)
        self.rn.run(self.rsync_models(c.models, c.parked, dry=False), ok=RSYNC_OK, stream=True)
        self.verify_parked()
        used = self.df(c.models, "used")
        self.state["parked_bytes"] = used
        self.state["parked_du"] = int(self.rn.run(["du", "-sb", "--apparent-size", c.parked], ro=True).split()[0])
        print("  parked: %s of models, the dry run finds nothing left to copy" % human(used or 0))

    def verify_parked(self):
        c = self.cfg
        out = self.rn.run(self.rsync_models(c.models, c.parked, dry=True), ok=RSYNC_OK)
        diff = [l for l in out.splitlines() if l.strip() and not l.startswith(("sending", "sent ", "total size"))]
        if diff:
            raise Abort("the parked copy differs from %s (%d differences, first: %s); not wiping anything" % (
                c.models, len(diff), diff[0]))
        a = self.rn.run(["du", "-sb", "--apparent-size", c.models], ro=True).split()
        b = self.rn.run(["du", "-sb", "--apparent-size", c.parked], ro=True).split()
        try:
            sa, sb = int(a[0]), int(b[0])
        except (ValueError, IndexError):
            raise Abort("could not measure the parked copy")
        if abs(sa - sb) > max(MIB, sa // 1000):
            raise Abort("the parked copy is %s and the original %s; not wiping anything" % (human(sb), human(sa)))

    # -- stage 2: partition and format ------------------------------------------------------------

    def stage_partition(self):
        c, a = self.cfg, self.args
        done, _ = self.progress()
        if "park" not in done:
            raise Abort("the models are not parked and verified; not erasing the drive")
        self.verify_parked_state()
        self.verify_before_wipe()
        self.mark("started", "partition")
        d = self.drive_to()
        # the models filesystem goes away: nothing may hold it
        for m in self.mounts():
            if m["target"] == c.models:
                self.rn.run(["umount", c.models])
        self.freeze_models_dir()
        tt = self.lsblk(d.dev)
        busy = sorted(tree_mounts(tt))
        if busy:
            raise Abort("the TO drive still has mounted filesystems (%s); not erasing it" % ", ".join(busy))
        if tree_holders(tt):
            raise Abort("something holds the TO drive; not erasing it")
        d = self.drive_to()
        self.w("to", ["wipefs", "-a", d.byid], d.byid)
        self.w("to", ["sgdisk", "--zap-all", d.byid], d.byid)
        self.w("to", sgdisk_args(d.byid, a.root_gib), d.byid)
        self.rn.run(["partprobe", d.byid])
        self.rn.run(["udevadm", "settle"])
        for n in (1, 2, 3, 4):
            self.wait_for_part(d, n)
        for n in (1, 2, 3, 4):
            # an old filesystem's signature can sit exactly where a new partition starts (the old models
            # partition and the new ESP both begin at 1 MiB): clear each new partition before formatting it
            self.w("to", ["wipefs", "-a", d.part(n)], d.part(n))
        self.rn.run(["udevadm", "settle"])
        fmt = {1: ["mkfs.vfat", "-F", "32", "-n", "O1ESP"], 2: ["mkfs.ext4", "-F", "-q", "-L", "o1boot"],
               3: ["mkfs.ext4", "-F", "-q", "-L", "o1root", "-m", "1"],
               4: ["mkfs.ext4", "-F", "-q", "-L", "o1models", "-m", "1"]}
        for n in (1, 2, 3, 4):
            self.w("to", fmt[n] + [d.part(n)], d.part(n))
        self.rn.run(["udevadm", "settle"])
        for n in (1, 2, 3, 4):
            want = "vfat" if n == 1 else "ext4"
            cur = self.probe(d.part(n), "TYPE")
            if cur != want:
                raise Abort("partition %d reads as '%s' after formatting it %s; stopped" % (n, cur or "nothing", want))
        uu = {}
        for n, k in ((1, "esp"), (2, "boot"), (3, "root"), (4, "models")):
            uu[k] = self.probe(d.part(n), "UUID")
            if not uu[k]:
                raise Abort("no filesystem UUID on partition %d after formatting" % n)
        self.state["uuids"] = uu
        self.state["esp_partuuid"] = self.probe(d.part(1), "PARTUUID")
        print("  partitions made and formatted: " + ", ".join("%s %s" % (k, v) for k, v in uu.items()))

    def verify_before_wipe(self):
        """The last look before the TO drive is erased: the RAID is whole and mounted read-write, and the parked
        copy is the models. With /srv/models still mounted, a rsync dry run and sizes again, then a content
        compare of every file up to 64 MiB and of 18 MiB spread over each larger one (a full checksum of a
        models store of a terabyte or more would read it twice over disks of 150 MB/s, hours; the sizes and
        times of everything are already exact). Resumed after the unmount, the parked folder is compared with
        the size recorded when it was verified."""
        c = self.cfg
        why = self.check_data_raid(self.mounts())
        if why:
            raise Abort(why + "; not erasing the drive")
        if any(m["target"] == c.models for m in self.mounts()):
            self.drop_page_cache()
            self.verify_parked()
            files, nbytes = sample_compare(c.models, c.parked)
            print("  checked again before the wipe: %d files, %s compared by content" % (files, human(nbytes)))
        else:
            want = self.state.get("parked_du")
            got = int(self.rn.run(["du", "-sb", "--apparent-size", c.parked], ro=True).split()[0])
            if not want or abs(got - want) > max(MIB, want // 1000):
                raise Abort("the parked copy is %s, not the size it had when it was verified; not going on" % human(got))

    def drop_page_cache(self):
        """So the final compare reads the disks, not the page cache the copy just filled."""
        self.rn.run(["sync"])
        try:
            with open(self.cfg.drop_caches, "w") as fh:
                fh.write("3\n")
        except OSError as e:
            raise Abort("cannot drop the page cache (%s); the compare would read cached data" % e.strerror)

    def freeze_models_dir(self):
        """chattr +i on the bare /srv/models while nothing is mounted there, so nothing can write models into the
        old root's folder. Refused while a filesystem is mounted on it (that would mark the models drive's own root)."""
        if any(m["target"] == self.cfg.models for m in self.mounts()):
            raise Abort("%s is mounted; not marking it immutable" % self.cfg.models)
        self.rn.run(["chattr", "+i", self.cfg.models])

    def thaw_old_models_dir(self, from_tree):
        """--finish, with the new system confirmed as the running root: take the immutable mark off the bare
        /srv/models folder of the OLD root, if that root is mounted somewhere to reach it. Normally it is not
        (the old drive isn't used any more), and the mark stays: harmless, and it protects a fallback boot."""
        mounts = {m["target"] for m in self.mounts()}
        for t in sorted(tree_mounts(from_tree) - {"[SWAP]", "/"}):
            p = t.rstrip("/") + self.cfg.models
            if os.path.isdir(p) and p not in mounts:
                self.rn.run(["chattr", "-i", p], ok=(0, 1))
                print("The old root's bare %s (at %s) is writable again." % (self.cfg.models, p))
                return
        print("The old drive's bare /srv/models folder stays immutable (it protects a fallback boot); when the old drive is "
              "reused, or booted, `sudo chattr -i /srv/models` there makes it writable.")

    def verify_parked_state(self):
        if not os.path.isdir(self.cfg.parked):
            raise Abort("%s is missing; not erasing the drive" % self.cfg.parked)

    def probe(self, dev, key):
        out = self.rn.run(["blkid", "-p", "-o", "value", "-s", key, dev], ro=True, ok=(0, 2))
        return out.strip()

    def wait_for_part(self, d, n):
        for _ in range(50):
            if os.path.exists(d.part(n)):
                return
            time.sleep(0.2)
        raise Abort("%s did not appear after partitioning" % d.part(n))

    # -- stage 3: copy / and /boot -----------------------------------------------------------------

    ROOT_EXCLUDES = ["/proc/*", "/sys/*", "/dev/*", "/run/*", "/tmp/*", "/swap.img", "/lost+found",
                     "/boot/*", "/srv/models/*", "/srv/data/*"]

    def stage_copy(self):
        c, d = self.cfg, self.drive_to()
        self.ensure_mounted(d.part(3), c.new_root)
        self.verify_mounted(d.part(3), c.new_root)
        src = c.src_root.rstrip("/") + "/"
        base = ["rsync", "-aHAXx", "--numeric-ids", "--delete"]
        if self.args.bwlimit:
            base.append("--bwlimit=%d" % self.args.bwlimit)
        base += ["--exclude=" + e for e in self.ROOT_EXCLUDES]
        for n in (1, 2):
            print("  copying / (pass %d of 2; the second only catches what changed)" % n)
            self.check_target(self.args.to_serial, d.byid)
            self.verify_mounted(d.part(3), c.new_root)
            self.rn.run(base + ["--info=progress2,stats1", src, c.new_root + "/"], ok=RSYNC_OK, stream=True)
        # the swap file: made new, not copied (8 GB of reads off a failing drive buys nothing)
        sw_src = os.path.join(c.src_root, "swap.img")
        sw_dst = c.new_root + "/swap.img"
        if os.path.isfile(sw_src) and not os.path.islink(sw_src):
            size = os.stat(sw_src).st_size
            if not os.path.isfile(sw_dst) or os.stat(sw_dst).st_size != size:
                self.rn.run(["fallocate", "-l", str(size), sw_dst])
                os.chmod(sw_dst, 0o600)
                self.rn.run(["mkswap", sw_dst])
        # /boot
        self.ensure_mounted(d.part(2), c.new_root + "/boot")
        self.verify_mounted(d.part(2), c.new_root + "/boot")
        self.verify_mounted(d.part(3), c.new_root)
        self.rn.run(["rsync", "-aHAXx", "--numeric-ids", "--delete", os.path.join(c.src_root, "boot").rstrip("/") + "/",
                     c.new_root + "/boot/"], ok=RSYNC_OK, stream=True)
        os.makedirs(c.new_root + "/boot/efi", exist_ok=True)
        self.ensure_mounted(d.part(1), c.new_root + "/boot/efi")
        self.verify_mounted(d.part(1), c.new_root + "/boot/efi")
        for need in ("etc/fstab", "usr", "var"):
            if not os.path.exists(os.path.join(c.new_root, need)):
                raise Abort("the copy has no /%s; the copy did not work" % need)

    # -- stage 4: fstab, initramfs, grub, firmware entry --------------------------------------------

    BINDS = [("dev", ["--rbind"]), ("sys", ["--rbind"]), ("run", ["--rbind"])]

    def bind_mounts(self):
        root = self.cfg.new_root
        have = {m["target"] for m in self.mounts()}
        for name, flags in self.BINDS:
            t = root + "/" + name
            if t in have:
                continue
            os.makedirs(t, exist_ok=True)
            self.rn.run(["mount"] + flags + ["/" + name, t])
            self.rn.run(["mount", "--make-rslave", t])         # an unmount in here must never reach the host's
        t = root + "/proc"
        if t not in have:
            os.makedirs(t, exist_ok=True)
            self.rn.run(["mount", "-t", "proc", "proc", t])

    def unbind_mounts(self):
        root = self.cfg.new_root
        have = {m["target"] for m in self.mounts()}
        for name in ("run", "sys", "proc", "dev"):
            t = root + "/" + name
            if t in have:
                self.rn.run(["umount", "-R", t])

    def stage_boot(self):
        c, d = self.cfg, self.drive_to()
        uu = self.state["uuids"]
        for part, t in ((3, c.new_root), (2, c.new_root + "/boot"), (1, c.new_root + "/boot/efi")):
            self.ensure_mounted(d.part(part), t)
            self.verify_mounted(d.part(part), t)
        fstab = c.new_root + "/etc/fstab"
        with open(fstab) as fh:
            old = fh.read()
        new, changes = rewrite_fstab(old, uu)
        if new != old:
            if not os.path.exists(fstab + ".before-migrate"):
                shutil.copy2(fstab, fstab + ".before-migrate")
            tmp = fstab + ".new"
            with open(tmp, "w") as fh:
                fh.write(new)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, fstab)
        for ch in changes:
            print("  fstab on the copy: " + ch)
        for f in ("etc/fstab", "etc/crypttab"):
            p = os.path.join(c.new_root, f)
            if os.path.exists(p) and "ubuntu-vg" in open(p).read().replace("ubuntu--vg-ubuntu--lv", ""):
                print("  NOTE: /%s mentions ubuntu-vg (an LVM volume on the old drive, such as the encrypted swap). "
                      "It has nofail, so the boot goes on; re-make that on the new system with setup.sh --encrypted-swap" % f)
        required = self.required_cmdline()
        if PANIC_OPTION in required:                      # a kernel panic reboots (into the default boot: the old drive)
            drop = os.path.join(c.new_root, PANIC_DROPIN)
            os.makedirs(os.path.dirname(drop), exist_ok=True)
            with open(drop, "w") as fh:
                fh.write('GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT %s"\n' % PANIC_OPTION)
        self.bind_mounts()
        try:
            for argv in chroot_commands(c.new_root):
                self.verify_mounted(d.part(3), c.new_root)
                self.rn.run(argv, stream=True)
        finally:
            self.unbind_mounts()
        self.verify_grub(required)
        self.state["cmdline_required"] = required
        esp = c.new_root + "/boot/efi/EFI"
        loader = None
        for cand in ("shimx64.efi", "grubx64.efi"):
            if os.path.isfile("%s/%s/%s" % (esp, BOOT_ID, cand)):
                loader = cand
                break
        if not loader:
            raise Abort("grub-install left no boot loader in the new ESP (EFI/%s)" % BOOT_ID)
        if not os.path.isfile("%s/%s/grub.cfg" % (esp, BOOT_ID)):
            raise Abort("grub-install left no grub.cfg stub next to the boot loader (EFI/%s/grub.cfg)" % BOOT_ID)
        # the removable path, for a firmware that forgets its entries
        fb = esp + "/BOOT"
        os.makedirs(fb, exist_ok=True)
        for n in os.listdir("%s/%s" % (esp, BOOT_ID)):
            dst = "BOOTX64.EFI" if n == loader else n
            shutil.copy2("%s/%s/%s" % (esp, BOOT_ID, n), "%s/%s" % (fb, dst))
        self.state["loader"] = loader
        self.save_state()

    def required_cmdline(self):
        """The kernel options the new system must boot with: those of the running
        system (the NVMe power settings, say) and the GPU overdrive switch the
        kit's drop-in adds."""
        c = self.cfg
        try:
            with open(c.cmdline) as fh:
                running = cmdline_tokens(fh.read())
        except OSError:
            raise Abort("cannot read %s; the kernel options to carry over are unknown" % c.cmdline)
        drop = os.path.join(c.src_root, "etc/default/grub.d/97-amdgpu-overdrive.cfg")
        if os.path.isfile(drop):
            with open(drop) as fh:
                tok = overdrive_token(fh.read())
            if tok and tok not in running:
                running.append(tok)
        if not any(t.startswith("panic=") for t in running):
            running.append(PANIC_OPTION)
        return running

    def verify_grub(self, required):
        """update-grub ran in the chroot with the kit's drop-ins: they are on the copy, and the
        copy's grub.cfg carries every kernel option the running system has."""
        c = self.cfg
        pairs = [("etc/default/grub", True)]
        gd = os.path.join(c.src_root, "etc/default/grub.d")
        for n in sorted(os.listdir(gd)) if os.path.isdir(gd) else []:
            pairs.append(("etc/default/grub.d/" + n, True))
        for rel, _ in pairs:
            src, dst = os.path.join(c.src_root, rel), os.path.join(c.new_root, rel)
            if os.path.isfile(src):
                if not os.path.isfile(dst):
                    raise Abort("/%s is not on the copy" % rel)
                with open(src, "rb") as a, open(dst, "rb") as b:
                    if a.read() != b.read():
                        raise Abort("/%s differs on the copy" % rel)
        cfg = os.path.join(c.new_root, "boot/grub/grub.cfg")
        try:
            with open(cfg) as fh:
                have = grub_linux_tokens(fh.read())
        except OSError:
            raise Abort("update-grub left no /boot/grub/grub.cfg on the copy")
        missing = [t for t in required if t not in have]
        if missing:
            raise Abort("the copy's GRUB config lacks kernel options the running system has: %s. Fix /etc/default/grub "
                        "(or a drop-in in grub.d) on the copy under %s, run update-grub in the chroot, then --resume"
                        % (" ".join(missing), c.new_root))
        print("  GRUB on the copy carries: %s" % " ".join(required))

    # -- stage 6: the firmware entry (last, so the old drive is the default boot until everything else is done)

    def stage_firmware(self):
        d = self.drive_to()
        loader = self.state.get("loader")
        if not loader:
            raise Abort("no boot loader was recorded by the boot stage; run --resume to redo it")
        text = self.rn.run(["efibootmgr", "-v"], ro=True)
        efi = parse_efi(text)
        previous = self.state.get("efi", {}).get("previous_order") or efi["order"]
        if "old_esp_partuuid" not in self.state:
            from_esp = self.mount_of(self.mounts(), "/boot/efi")
            self.state["old_esp_partuuid"] = (self.probe(from_esp["source"], "PARTUUID").lower()
                                              if from_esp and from_esp["target"] == "/boot/efi" else "")
        old = find_old_entry(efi, self.state["old_esp_partuuid"])
        pu = self.state["esp_partuuid"]
        path = "\\EFI\\%s\\%s" % (BOOT_ID, loader)
        new = new_entry_number(efi, pu, path)
        if new:
            # made by an earlier run (this stage was stopped, or --resume): the same ESP and loader path, so it is
            # used, not made again; the firmware may also list it twice under two numbers
            dup = [n for n in entries_for(efi, pu, loader=path) if n != new]
            print("  firmware: entry %s already boots the new drive's ESP (%s); using it%s" % (
                new, path, ("; the firmware lists it again as %s, left as it is" % ",".join(dup)) if dup else ""))
            if not efi["entries"][new]["active"]:
                self.rn.run(["efibootmgr", "-a", "-b", new])
        else:
            self.w("to", ["efibootmgr", "-c", "-d", d.byid, "-p", "1", "-L", BOOT_LABEL, "-l", path], d.byid)
            text = self.rn.run(["efibootmgr", "-v"], ro=True)
            efi = parse_efi(text)
            new = new_entry_number(efi, pu, path)
            if not new:
                mine = [l.strip()[:160] for l in text.splitlines() if pu and pu in l.lower()]
                raise Abort("efibootmgr made no entry for the new drive's ESP (%s, %s). Its list has %d entries, %d on that "
                            "ESP%s. Look at `efibootmgr -v`; if the entry is there, run --resume again, it is reused" % (
                                BOOT_LABEL, path, len(efi["entries"]), len(mine), (": " + mine[0]) if mine else ""))
        previous = [n for n in previous if n != new]
        order = boot_order_old_first(new, old, previous, efi, pu)
        self.rn.run(["efibootmgr", "-o", ",".join(order)])
        self.rn.run(["efibootmgr", "-n", new])            # BootNext: only the next boot tries the new drive
        self.state["efi"] = {"new": new, "old": old, "previous_order": previous, "order": order}
        self.save_state()
        print("  firmware: the old entry %s stays first in the boot order (%s); BootNext sends the next boot, once, to the "
              "new drive (entry %s). A power cycle after a bad first boot comes back to the old drive; --finish makes the new "
              "drive the default once it has booted and been checked" % (old or "NOT FOUND", ",".join(order), new))
        if not old:
            print("  NOTE: this tool could not tell which entry boots the old drive; the order it had is kept, with the new entry last")

    # -- stage 5: models back ------------------------------------------------------------------

    def stage_restore(self):
        c, d = self.cfg, self.drive_to()
        self.ensure_mounted(d.part(4), c.new_models)
        self.verify_mounted(d.part(4), c.new_models)
        self.rn.run(["rsync", "-aHAX", "--numeric-ids", "--info=progress2,stats1"] + (
            ["--bwlimit=%d" % self.args.bwlimit] if self.args.bwlimit else []) +
            [c.parked.rstrip("/") + "/", c.new_models + "/"], ok=RSYNC_OK, stream=True)
        out = self.rn.run(["rsync", "-aHAXn", "--numeric-ids", "--itemize-changes",
                           c.parked.rstrip("/") + "/", c.new_models + "/"], ok=RSYNC_OK)
        diff = [l for l in out.splitlines() if l.strip() and not l.startswith(("sending", "sent ", "total size"))]
        if diff:
            raise Abort("the models on the new partition differ from the parked copy (%d, first: %s)" % (len(diff), diff[0]))
        self.unmount_new()
        print("  models restored and checked; the new system is unmounted")

    # -- driving the stages ----------------------------------------------------------------------

    def run_stages(self):
        fns = {"park": self.stage_park, "partition": self.stage_partition, "copy": self.stage_copy,
               "boot": self.stage_boot, "restore": self.stage_restore, "firmware": self.stage_firmware}
        for s in STAGES[1:]:
            done, _ = self.progress()
            if self.st:
                self.st.stage(s)
            if s in done:
                print("stage %s: done earlier, skipped" % s)
                continue
            self.cur_stage = s
            print("\n== stage %s (%d of %d: %s) ==" % (s, STAGES.index(s), len(STAGES) - 1, STAGE_TEXT[s]))
            self.check_alive()
            fns[s]()
            self.mark("done", s)
            print("stage %s: done" % s)

    def check_alive(self):
        fd = self.resolve(self.args.from_serial)
        if fd is None:
            raise Abort("the FROM drive (serial %s) has dropped off the bus. Power-cycle the server, then --resume" % self.args.from_serial)
        why = drive_dead(fd.ctrl, self.read(os.path.join(self.cfg.sys, "class/nvme", fd.ctrl, "state")), "", "")
        if why:
            raise Abort("the FROM drive is dead right now: %s. Power-cycle the server, then --resume" % why)

    def final_checks(self):
        """Everything that must be true before anyone reboots into the new drive."""
        done, _ = self.progress()
        missing = [s for s in STAGES[1:] if s not in done]
        if missing:
            raise Abort("stages not finished: %s" % ", ".join(missing))
        self.rearm_bootnext()
        live = [m["target"] for m in self.mounts() if m["target"] == self.cfg.mnt or m["target"].startswith(self.cfg.mnt + "/")]
        if live:
            raise Abort("the new system is still mounted (%s)" % ", ".join(live))

    def rearm_bootnext(self):
        """BootNext is used up by any boot. If one happened since stage 6 (a power cut, or a first boot of the new
        drive that failed and fell back to the old one) and the entries and the order are still right, set it
        again; stop only if the entries themselves are wrong."""
        e = self.state.get("efi", {})
        new, old = e.get("new"), e.get("old")
        if not new:
            raise Abort("no firmware entry for the new drive was recorded")
        efi = parse_efi(self.rn.run(["efibootmgr", "-v"], ro=True))
        if not efi["entries"].get(new, {}).get("active"):
            raise Abort("the new drive's firmware entry %s is missing or inactive; run --resume after checking efibootmgr" % new)
        if old and old not in efi["entries"]:
            raise Abort("the old drive's firmware entry %s is gone" % old)
        if old and efi["order"][:1] != [old]:
            raise Abort("the old drive's firmware entry is not first in the boot order (it must stay the default)")
        if efi["order"][:1] == [new]:
            raise Abort("the new drive's entry is first in the boot order; it must only be BootNext until --finish")
        if efi["next"] != new:
            print("  BootNext was used up by a boot since the firmware stage; setting it again (entry %s)" % new)
            self.rn.run(["efibootmgr", "-n", new])
            if parse_efi(self.rn.run(["efibootmgr", "-v"], ro=True))["next"] != new:
                raise Abort("could not set BootNext to %s" % new)

    def final_summary(self, rebooting):
        efi = self.state.get("efi", {})
        self.box("DONE. The copy is finished and checked; nothing has booted from it yet.", [
            "Done: models parked, TO drive partitioned, / and /boot copied, fstab, initramfs and GRUB made on the copy,",
            "models restored. Firmware: the new drive (entry %s) boots NEXT (BootNext), once; the old drive (entry %s) stays" % (
                efi.get("new", "?"), efi.get("old") or "?"),
            "first in the boot order until --finish, so a power cycle after a bad first boot returns to it.",
            "The old drive (serial %s) was not written to (only its bare /srv/models folder is marked immutable)." % self.args.from_serial,
            "",
            ("Rebooting into the new drive in %d seconds (Ctrl-C cancels)." % self.cfg.countdown) if rebooting
            else "Reboot into the new drive:   sudo reboot",
            "After the reboot:  findmnt /     (the source must be a partition of the NEW drive, not ubuntu--vg)",
            "Then:              %s" % self.cmdline("finish"),
            "If it does not boot: choose the old drive in the BIOS boot menu (%s). Nothing on it was changed." % self.cfg.bios_key])

    def failure_summary(self, why):
        a = self.args
        done, _ = self.progress()
        self.box("STOPPED: " + why.splitlines()[0][:200], [
            "Stages finished: %s" % (", ".join(s for s in STAGES if s in done) or "none"),
            "The old drive (serial %s) has not been written to. Nothing was rebooted." % a.from_serial,
            "Anything an earlier stage finished is kept; running again carries on:",
            "   %s" % self.cmdline("resume"),
            "Services stopped for this: %s (they start by themselves at the next boot)" % (
                ", ".join((self.state or {}).get("stopped", [])) or "none")])

    def next_after_failure(self):
        return ("the old drive is untouched and nothing was rebooted. If the drive dropped off the bus, "
                "power-cycle the server first. Then: %s --reboot" % self.cmdline("resume"))

    def failure_phrase(self, exc=None):
        """What the world-readable status says about a failure: fixed words, a stage name, a program name from
        a fixed list and an exit number. The reason itself (paths, output) is in the terminal and the root-only log."""
        stage = self.cur_stage or "before the first stage"
        if isinstance(exc, CommandFailed):
            prog = os.path.basename(exc.argv[0])
            prog = prog if prog in TOOLS or prog in ("tmux", "systemd-inhibit", "update-initramfs", "grub-install", "update-grub") else "a program"
            return "FAILED in stage %s: %s stopped with exit %d (details in the log, root only)" % (stage, prog, exc.rc)
        return "FAILED in stage %s: a safety check stopped it (details in the log, root only)" % stage

    def fail(self, why, exc=None):
        self.failure_summary(why)
        if self.st:
            self.st.finish(self.failure_phrase(exc), self.next_after_failure())

    # -- reboot -------------------------------------------------------------------------------------

    def offer_reboot(self):
        """Interactive runs only (a --confirm-serial or detached run never asks). The default is no."""
        a = self.args
        if a.confirm_serial is not None or a.in_session:
            return False
        try:
            ans = self.ask("Reboot into the new drive now? [y/N] ")
        except Abort:
            print("(no terminal to ask on; reboot yourself: sudo reboot)")
            return False
        return ans.lower() in ("y", "yes")

    def reboot(self):
        n = self.cfg.countdown
        signal.signal(signal.SIGINT, signal.default_int_handler)      # Ctrl-C cancels, however this was started
        print("\nRebooting into the new drive in %d seconds. Press Ctrl-C to cancel." % n, flush=True)
        try:
            for i in range(n, 0, -1):
                print("  %d ..." % i, flush=True)
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nReboot cancelled. When you are ready: sudo reboot", flush=True)
            if self.st:
                self.st.finish("DONE", "the copy is finished; the reboot was cancelled. Reboot when ready (sudo reboot), then: " +
                               self.cmdline("finish"))
            return False
        self.rn.run(["systemctl", "reboot"])
        return True

    # -- the modes ---------------------------------------------------------------------------------

    def need_args(self):
        a = self.args
        if a.mode in ("resume", "finish") and not (a.from_serial and a.to_serial):
            if a.from_serial or a.to_serial:
                raise Abort("give both --from-serial and --to-serial, or neither (they are in the state file)")
            st = self.load_state()
            if not st:
                raise Abort("there is no migration (no %s). Start with --run" % self.state_path)
            a.from_serial, a.to_serial = st["from_serial"], st["to_serial"]
        if not a.from_serial or not a.to_serial:
            raise Abort("give both --from-serial and --to-serial (lsblk -d -o NAME,SIZE,MODEL,SERIAL lists them)")
        if a.confirm_serial is not None and a.confirm_serial != a.to_serial:
            raise Abort("--confirm-serial is not the serial of the drive to be erased (--to-serial); nothing was changed")
        if os.geteuid() != 0 and not (self.cfg.honour and os.environ.get("O1M_ALLOW_NONROOT")):
            raise Abort("run it with sudo")

    def mode_plan(self):
        self.need_args()
        self.state = self.load_state()
        f, problems = self.preflight()
        print("PLAN (nothing is changed by --plan)\n")
        for l in self.plan_text(f):
            print(l)
        print()
        if problems:
            print("WOULD REFUSE TO START:")
            for p in problems:
                print("  - " + p)
            return 1
        print("All checks pass. To do it, unattended, ending in a reboot into the new drive:\n  %s --reboot" % self.cmdline("run"))
        return 0

    def adopt_state(self, mode):
        a = self.args
        st = self.load_state()
        if mode == "run":
            if st and st.get("done") and any(s in st["done"] for s in STAGES[1:]):
                raise Abort("a migration is already under way (%s). Use --resume to carry on" % self.state_path)
            return {"version": 1, "from_serial": a.from_serial, "to_serial": a.to_serial, "root_gib": a.root_gib,
                    "created": self.now(), "done": {}, "started": {}}
        if not st:
            raise Abort("there is no migration to resume (no %s). Start with --run" % self.state_path)
        if st["from_serial"] != a.from_serial or st["to_serial"] != a.to_serial:
            raise Abort("the serials don't match the migration in progress (it is from %s to %s); stopped"
                        % (st["from_serial"], st["to_serial"]))
        if a.root_size_given and a.root_gib != st["root_gib"]:
            raise Abort("--root-size %dG differs from the %dG this migration began with" % (a.root_gib, st["root_gib"]))
        a.root_gib = st["root_gib"]
        return st

    def begin_outputs(self):
        """Status file and log, once the lock is ours and the folder is known to be on the RAID."""
        a = self.args
        self.st = Status(self.cfg, redact=[(a.from_serial, "<old-drive>"), (a.to_serial, "<new-drive>")])
        self.st.enable()
        sys.stdout = Tee(sys.__stdout__, self.st)
        sys.stderr = Tee(sys.__stderr__, self.st)
        self.st.start_heartbeat()
        if os.stat(self.cfg.data).st_mode & 0o005 != 0o005:
            print("NOTE: %s can't be entered by everyone, so the status file can't be read without sudo "
                  "(chmod o+rx %s to change that)" % (self.cfg.data, self.cfg.data))

    def release_lock(self):
        if self.lock_fd:
            self.lock_fd.close()
            self.lock_fd = None

    def mode_run(self, mode):
        a = self.args
        self.need_args()
        self.state = self.adopt_state(mode)
        f, problems = self.preflight()
        if f["data_ok"]:
            self.take_lock()
            self.begin_outputs()
        try:
            if problems:
                print("REFUSING TO START:")
                for p in problems:
                    print("  - " + p)
                print("\nNothing was changed.")
                if self.st:
                    self.st.finish("FAILED: refused to start (a preflight check failed; the terminal says which)", self.next_after_failure())
                return 1
            if not self.st:
                raise Abort("cannot place the status file safely; stopped")
            for l in self.plan_text(f):
                print(l)
            self.confirm(f)
        except Abort as e:
            if self.st:
                self.st.finish("FAILED: stopped before the first stage (not confirmed, or a check failed)", self.next_after_failure())
            raise
        if mode == "run":
            self.save_state()
        if not a.in_session and self.should_launch():
            self.launch()
            return 0
        self.execute()
        return 0

    def should_launch(self):
        if os.environ.get("TMUX") or os.environ.get("O1_NO_TMUX") == "1":
            return False
        if not shutil.which(self.cfg.tmux):
            print("tmux is not installed; running here (a dropped connection would stop it; --resume carries on).")
            return False
        return True

    def launch(self):
        """Start the run again inside a detached tmux session. It was confirmed here (the typed serial
        or --confirm-serial), so the session is told so and asks nothing."""
        a, c = self.args, self.cfg
        if self.rn.rc([c.tmux, "has-session", "-t", SESSION]) == 0:
            raise Abort("a tmux session named %s exists already. Attach: sudo tmux attach -t %s (or "
                        "sudo tmux kill-session -t %s if it is a leftover)" % (SESSION, SESSION, SESSION))
        child = [sys.executable, "-I", "-u", os.path.realpath(__file__), "--from-serial", a.from_serial,
                 "--to-serial", a.to_serial, "--root-size", "%dG" % a.root_gib, "--bwlimit", str(a.bwlimit),
                 "--resume", "--confirm-serial", a.to_serial, "--in-session"] + (["--reboot"] if a.reboot else [])
        self.st.set(stage="starting", next="nothing to do; it runs by itself in the tmux session. Watch this file.")
        self.st.freeze()               # from here the session owns the status file; this process must not rewrite it
        self.release_lock()
        try:
            self.rn.run([c.tmux, "new-session", "-d", "-s", SESSION, "-c", "/", shlex.join(child)])
        except Abort as e:
            self.st.enabled = True
            self.st.finish("FAILED: could not start the tmux session", self.next_after_failure())
            raise
        self.box("STARTED in the background (tmux session %s)" % SESSION, [
            "It runs by itself from here, whatever happens to this connection; nothing more is asked.",
            "Status:    cat %s" % c.status,
            "Log:       %s   (root only)" % c.log,
            "Watch it:  sudo tmux attach -t %s     (Ctrl-b then d leaves it running)" % SESSION,
            "If it stops: the status says FAILED or goes stale; carry on with  %s --reboot" % self.cmdline("resume"),
            "It %s" % ("reboots into the new drive by itself at the end." if a.reboot
                       else "stops before the reboot; reboot, then run --finish.")])

    def hold_awake(self):
        """No sleep and no power-button suspend while the run goes on (the kit's auto sleep honours a logind
        block). It ends with this process, however it ends (the holder reads a pipe only we write)."""
        exe = shutil.which("systemd-inhibit")
        if not exe or not (os.geteuid() == 0 or self.cfg.honour):
            return
        try:
            self.inhibitor = subprocess.Popen(
                [exe, "--what=sleep:handle-power-key", "--mode=block", "--who=ollama1",
                 "--why=migrate-os is running", "sh", "-c", "cat >/dev/null"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                start_new_session=True)
            print("  holding the server awake (no sleep, no power-button suspend) while this runs")
        except OSError:
            self.inhibitor = None

    def execute(self):
        a = self.args
        try:
            self.mark("done", "preflight")
            self.hold_awake()
            self.stop_services()
            if "firmware" in self.progress()[0]:
                self.rearm_bootnext()
            self.run_stages()
            self.final_checks()
        except Abort as e:
            self.fail(str(e), e)
            raise
        except KeyboardInterrupt:
            self.fail("interrupted")
            raise Abort("interrupted")
        except Exception as e:      # a bug or a full disk: still say how to carry on
            self.fail("unexpected: %r" % e)
            raise Abort("unexpected: %r" % e)
        go = a.reboot or self.offer_reboot()
        self.final_summary(go)
        if self.st:
            self.st.finish("DONE", ("the machine reboots into the new drive by itself in %d s; when it is back: %s" % (
                self.cfg.countdown, self.cmdline("finish"))) if go else
                ("reboot when you are ready (sudo reboot), then: " + self.cmdline("finish")))
        if go:
            self.reboot()

    def mode_status(self):
        c = self.cfg
        try:
            with open(c.status) as fh:
                text = fh.read()
        except OSError:
            print("No migration status at %s (nothing has run, or /srv/data is not mounted)." % c.status)
            return 1
        print(text, end="")
        verdict = diagnose_status(text, Status.boot_id(), time.time())
        if verdict:
            print("\n" + verdict)
        return 0

    def mode_finish(self):
        self.need_args()
        a, c = self.args, self.cfg
        self.state = self.load_state()
        if not self.state or not all(s in self.state.get("done", {}) for s in STAGES[1:]):
            raise Abort("the migration did not reach its end (stages done: %s); finish that with --resume first" % (
                ", ".join(s for s in STAGES[1:] if s in (self.state or {}).get("done", {})) or "none"))
        if self.state["from_serial"] != a.from_serial or self.state["to_serial"] != a.to_serial:
            raise Abort("the serials don't match the migration in the state file")
        td = self.resolve(a.to_serial)
        if td is None:
            raise Abort("the TO drive (serial %s) is not there" % a.to_serial)
        tt = self.lsblk(td.dev)
        have = tree_mounts(tt)
        want = {"/", "/boot", "/boot/efi", c.models}
        miss = sorted(want - have)
        if miss:
            raise Abort("not running from the new drive yet: %s are not on it (serial %s). Reboot, and if the old drive "
                        "came up, check the boot order in the BIOS (%s)" % (", ".join(miss), a.to_serial, c.bios_key))
        print("Verified: / , /boot, /boot/efi and %s are all on the new drive (serial %s)." % (c.models, a.to_serial))
        try:
            with open(c.cmdline) as fh:
                running = cmdline_tokens(fh.read())
        except OSError:
            raise Abort("cannot read %s" % c.cmdline)
        lacking = [t for t in self.state.get("cmdline_required", []) if t not in running]
        if lacking:
            raise Abort("the new system booted without these kernel options: %s (the NVMe power settings or the GPU "
                        "overdrive switch are not in force). Fix /etc/default/grub or its grub.d drop-ins, run "
                        "sudo update-grub, reboot, and run --finish again" % " ".join(lacking))
        print("Verified: the kernel command line has the options the old system ran with: %s" % " ".join(
            self.state.get("cmdline_required", [])))
        fd = self.resolve(a.from_serial)
        self.promote_new_entry()
        self.thaw_old_models_dir(self.lsblk(fd.dev) if fd else {})
        if a.delete_parked:
            self.delete_parked()
        if a.disable_old_entry:
            self.disable_old()
        if a.disable_old_boot_files:
            self.disable_old_boot_files()
        self.state["finished"] = self.now()
        self.save_state()
        old = self.state.get("efi", {}).get("old")
        lines = [
            "The old drive (serial %s)%s was never written to. It is still a complete, bootable system:" % (
                a.from_serial, "" if fd else " (not visible now)"),
            "keep it as the fallback for a while. Reusing it is yours to do, when the new system has run well for a",
            "while (this tool never wipes a drive)%s." % (
                ":  sudo blkdiscard %s" % fd.byid if fd else ""),
            "Its firmware entry (%s) is still there, second in the order%s." % (
                old or "not found", " (inactive now)" if a.disable_old_entry else "; --finish --disable-old-entry makes it inactive"),
            ("Its boot files (EFI/ubuntu, EFI/BOOT) are put aside as *.off: it is not a fallback until they are renamed back (%s)." % self.undo_path()
             if a.disable_old_boot_files else
             "Its boot files are untouched. If the firmware keeps picking it anyway: --finish --disable-old-boot-files (off by default; undoable)."),
            "The parked models copy %s%s" % (
                "was deleted." if a.delete_parked else "is still at %s;" % c.parked,
                "" if a.delete_parked else " --finish --delete-parked frees the space (it checks the live copy first)."),
            "The new drive is now the default boot; a kernel panic reboots (panic=10) into it, and the old drive stays second.",
            "The kit's setup.sh still thinks / is an LVM volume on the old drive; see ollama1/README.md before re-running it."]
        if os.path.exists(c.sudoers):
            if a.remove_sudoers or self.offer_remove_sudoers():
                os.remove(c.sudoers)
                lines.append("REMOVED the temporary sudo permission (%s)." % c.sudoers)
            else:
                lines.append("REMINDER: remove the temporary sudo permission now:  sudo rm %s" % c.sudoers)
        self.box("FINISHED. The system runs from the new drive.", lines)
        return 0

    def promote_new_entry(self):
        """The new system has booted and been checked: only now does it become the default boot (new first, the
        old drive's entry second)."""
        e = self.state.get("efi", {})
        new, old = e.get("new"), e.get("old")
        if not new:
            raise Abort("no firmware entry for the new drive was recorded; set the boot order by hand with efibootmgr")
        efi = parse_efi(self.rn.run(["efibootmgr", "-v"], ro=True))
        if new not in efi["entries"]:
            raise Abort("the new drive's firmware entry %s is gone; set the boot order by hand with efibootmgr" % new)
        order = boot_order(new, old, efi["order"], efi, self.state.get("esp_partuuid"))
        if efi["order"] != order:
            self.rn.run(["efibootmgr", "-o", ",".join(order)])
        e["order"] = order
        e["promoted"] = self.now()
        self.state["efi"] = e
        print("Firmware boot order is now: %s (new drive first, old drive second)" % ",".join(order))

    def offer_remove_sudoers(self):
        if self.args.confirm_serial is not None:
            return False
        try:
            return self.ask("Remove the temporary sudo permission %s now? [y/N] " % self.cfg.sudoers).lower() in ("y", "yes")
        except Abort:
            return False

    def delete_parked(self):
        p = self.cfg.parked
        if os.path.islink(p) or not os.path.isdir(p) or os.path.realpath(p) != os.path.realpath(self.cfg.data + "/models-parked"):
            raise Abort("%s is not the parked-models folder this tool made; not deleting anything" % p)
        out = self.rn.run(["rsync", "-rn", "--size-only", "--itemize-changes", p.rstrip("/") + "/",
                           self.cfg.models.rstrip("/") + "/"], ok=RSYNC_OK)
        missing = [l for l in out.splitlines() if l.startswith(">f+")]
        if missing:
            raise Abort("%d files in the parked copy are not in %s (first: %s); not deleting the parked copy"
                        % (len(missing), self.cfg.models, missing[0]))
        size = self.rn.run(["du", "-sb", "--apparent-size", p], ro=True).split()[0]
        print("About to delete %s (%s bytes). Type delete to do it." % (p, size))
        if self.ask("delete> ") != "delete":
            raise Abort("not confirmed; the parked copy was kept")
        self.rn.run(["rm", "-rf", "--one-file-system", "--", p])

    def old_esp_part(self, fd):
        """The old drive's EFI partition (by-id name): the partition of that drive whose PARTUUID is the one recorded
        for the ESP the old system booted from. Never guessed."""
        pu = self.state.get("old_esp_partuuid")
        if not pu:
            raise Abort("this tool never recorded which partition is the old drive's EFI partition; rename EFI/ubuntu "
                        "and EFI/BOOT there by hand")
        for n in range(1, 5):
            part = fd.part(n)
            if os.path.exists(part) and self.probe(part, "PARTUUID").lower() == pu.lower():
                if self.probe(part, "TYPE").lower() != "vfat":
                    raise Abort("the old drive's EFI partition (%s) is not vfat; not touching it" % part)
                return part
        raise Abort("no partition of the old drive (serial %s) has the EFI partition's id %s; not touching anything"
                    % (self.args.from_serial, pu))

    def disable_old_boot_files(self):
        """Optional (--finish --disable-old-boot-files), off by default. The firmware of this server kept
        re-ordering BootOrder and booting the old drive. Renaming EFI/ubuntu and EFI/BOOT on the old drive's EFI
        partition to *.off leaves it nothing to boot there, so it falls through to the new drive. It does make the
        old drive no longer a fallback until undone; the undo is written to /srv/data first and said out loud."""
        a, c = self.args, self.cfg
        fd = self.resolve(a.from_serial)
        if fd is None:
            raise Abort("the old drive (serial %s) is not there, so its boot files cannot be put aside" % a.from_serial)
        part = self.old_esp_part(fd)
        if [m for m in self.mounts() if os.path.realpath(m["source"]) == os.path.realpath(part)
                and m["target"] != c.mnt + "/old-esp"]:
            raise Abort("the old drive's EFI partition is mounted already (%s); not touching it" % part)
        target = c.mnt + "/old-esp"
        os.makedirs(target, exist_ok=True)
        print("About to rename EFI/ubuntu and EFI/BOOT on the OLD drive's EFI partition (%s) to *.off." % part)
        print("The old drive stops being a fallback until they are renamed back. How to undo it is written to %s." % self.undo_path())
        if self.ask("type yes> ") != "yes":
            raise Abort("not confirmed; the old drive's boot files were left as they are")
        self.w("from", ["mount", "-t", "vfat", part, target], part)
        try:
            efi_dir = target + "/EFI"
            names = os.listdir(efi_dir) if os.path.isdir(efi_dir) else []
            todo, done, blocked = plan_boot_file_renames(names)
            if blocked:
                raise Abort("not renaming: " + "; ".join(blocked) + ". Sort that out by hand")
            if not todo:
                print("  old drive: nothing to rename (%s)" % (("already put aside: " + ", ".join(done)) if done else
                                                              "there is no EFI/ubuntu or EFI/BOOT"))
            else:
                self.write_undo(part, todo)                  # the way back is on disk before the first rename
                for old_name, new_name in todo:
                    os.rename(os.path.join(efi_dir, old_name), os.path.join(efi_dir, new_name))
                    print("  old drive: EFI/%s -> EFI/%s" % (old_name, new_name))
                os.sync()
        finally:
            self.rn.run(["umount", target], ok=(0, 32))

    def undo_path(self):
        return self.cfg.data + "/old-drive-boot-files-undo.txt"

    def write_undo(self, part, renamed):
        text = undo_note_text(part, None, renamed, self.args.from_serial, self.now())
        tmp = self.undo_path() + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.undo_path())
        print("  the way back is in %s" % self.undo_path())

    def disable_old(self):
        old = self.state.get("efi", {}).get("old")
        if not old:
            raise Abort("this tool never found the old drive's boot entry; disable it by hand with efibootmgr")
        print("About to make the firmware entry %s (the old drive) inactive. It is not deleted; efibootmgr -a -b %s undoes it." % (old, old))
        if self.ask("type yes> ") != "yes":
            raise Abort("not confirmed; the entry was left as it is")
        self.rn.run(["efibootmgr", "-A", "-b", old])


def plan_boot_file_renames(names):
    """The entries of the old ESP's EFI folder to put aside -> [(name, name + ".off")], and what is already put
    aside or in the way. Only `ubuntu` and `BOOT` (FAT is not case sensitive; the names are matched that way)."""
    lower = {n.lower(): n for n in names}
    todo, done, blocked = [], [], []
    for base in OLD_BOOT_DIRS:
        have, off = lower.get(base.lower()), lower.get(base.lower() + OFF_SUFFIX)
        if have and off:
            blocked.append("%s and %s both exist" % (have, off))
        elif have:
            todo.append((have, have + OFF_SUFFIX))
        elif off:
            done.append(off)
    return todo, done, blocked


def undo_note_text(part, esp_mount_hint, renamed, serial, when):
    """What to run to put the old drive's boot files back."""
    lines = ["Old drive (serial %s): boot files put aside by migrate-os.sh --finish --disable-old-boot-files on %s." % (serial, when),
             "",
             "The old drive's EFI partition is %s." % part,
             "Renamed, inside its EFI folder: %s." % ", ".join("%s -> %s" % (a, b) for a, b in renamed),
             "",
             "To make the old drive bootable again (as root):",
             "  mkdir -p /mnt/old-esp && mount %s /mnt/old-esp" % part]
    lines += ["  mv /mnt/old-esp/EFI/%s /mnt/old-esp/EFI/%s" % (b, a) for a, b in renamed]
    lines += ["  umount /mnt/old-esp",
              "",
              "Then, if its firmware entry was made inactive too (--disable-old-entry), make it active again:  efibootmgr -a -b <number>",
              "(efibootmgr -v shows the numbers).", ""]
    return "\n".join(lines)


def parse_status(text):
    f = {}
    for line in text.splitlines():
        m = re.match(r"^([a-z ]+):\s+(.*)$", line)
        if m:
            f[m.group(1).strip()] = m.group(2).strip()
    return f


def diagnose_status(text, boot_id, now):
    """What a status file left behind means, for whoever reads it after a reboot or a crash."""
    f = parse_status(text)
    if not f.get("state", "").startswith("RUNNING"):
        return ""
    if f.get("boot id") and f["boot id"] != boot_id:
        return ("INTERRUPTED: the machine restarted after this was written (it was in stage %s). Nothing is running. "
                "The old drive is the default boot unless the last stage (firmware) had finished. "
                "To carry on: sudo bash migrate-os.sh --resume --reboot" % f.get("stage", "?"))
    try:
        age = now - datetime.datetime.fromisoformat(f["updated"]).timestamp()
    except (KeyError, ValueError):
        return ""
    if age > 180:
        return ("STALE: nothing has been written for %d seconds, so the run is stuck or was killed. "
                "Check: sudo tmux ls ; if there is no session 'migrate', carry on with: sudo bash migrate-os.sh --resume --reboot" % age)
    return ""


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="migrate-os.sh", description="Move the server's OS to the other NVMe, keeping the old one as the fallback.")
    ap.add_argument("--from-serial", help="serial of the OS drive (read from, never written)")
    ap.add_argument("--to-serial", help="serial of the drive that will be ERASED and become the OS drive")
    ap.add_argument("--root-size", default=None, help="size of / on the new drive (default 300G)")
    ap.add_argument("--bwlimit", type=int, default=200000, help="copy speed limit, KiB/s (0 = none); gentler on a drive that drops out under load")
    ap.add_argument("--confirm-serial", default=None, help="instead of typing the serial: it must equal --to-serial. With it, nothing is asked")
    ap.add_argument("--reboot", action="store_true", help="with --run/--resume: reboot into the new drive when everything has passed (10 s countdown, Ctrl-C cancels)")
    ap.add_argument("--in-session", action="store_true", help=argparse.SUPPRESS)
    g = ap.add_mutually_exclusive_group()
    for m in ("plan", "run", "resume", "finish", "status"):
        g.add_argument("--" + m, dest="mode", action="store_const", const=m)
    g.add_argument("--print-sudoers", dest="mode", action="store_const", const="print-sudoers",
                   help="print the sudoers drop-in for --sudoers-user and exit")
    g.add_argument("--install-remote", dest="mode", action="store_const", const="install-remote", help=argparse.SUPPRESS)
    ap.add_argument("--sudoers-user", default=None)
    ap.add_argument("--delete-parked", action="store_true", help="with --finish: delete the parked models copy")
    ap.add_argument("--disable-old-entry", action="store_true", help="with --finish: make the old drive's firmware entry inactive")
    ap.add_argument("--disable-old-boot-files", action="store_true",
                    help="with --finish: rename EFI/ubuntu and EFI/BOOT on the old drive's ESP to *.off, so a firmware that "
                         "keeps picking the old drive finds nothing to boot there (off by default; the undo is written to /srv/data)")
    ap.add_argument("--remove-sudoers", action="store_true", help="with --finish: remove the temporary sudo permission")
    a = ap.parse_args(argv)
    a.mode = a.mode or "plan"
    a.root_size_given = a.root_size is not None
    a.root_gib = parse_size_gib(a.root_size or "300G")
    if (a.delete_parked or a.disable_old_entry or a.disable_old_boot_files or a.remove_sudoers) and a.mode != "finish":
        ap.error("--delete-parked, --disable-old-entry, --disable-old-boot-files and --remove-sudoers go with --finish")
    if a.reboot and a.mode not in ("run", "resume"):
        ap.error("--reboot goes with --run or --resume")
    if a.in_session and a.mode != "resume":
        ap.error("--in-session is only for the session this tool starts itself")
    if a.mode == "install-remote":
        ap.error("--install-remote is done by migrate-os.sh, not by o1migrate.py")
    return a


def main(argv=None):
    for d in SBIN:                      # a tmux server started by someone else may have a PATH without them
        if d not in os.environ.get("PATH", "").split(":"):
            os.environ["PATH"] = os.environ.get("PATH", "") + ":" + d
    os.environ.setdefault("TERM", "xterm")
    try:
        a = parse_args(sys.argv[1:] if argv is None else argv)
        if a.mode == "print-sudoers":
            print(sudoers_text(a.sudoers_user), end="")
            return 0
        if a.mode not in ("status", "print-sudoers") and (os.geteuid() == 0 or os.environ.get("O1M_FORCE_PATH_CHECK")):
            why = unsafe_path_reason(__file__)
            if why:
                raise Abort("refusing to run as root from here: %s. Install it with: sudo bash <checkout>/ollama1/tools/"
                            "migrate-os.sh --install-remote, and run the installed copy" % why)
    except Abort as e:
        print("STOPPED: %s" % e, file=sys.stderr)
        return 2
    m = Migrator(Cfg(), a)
    m.st = None
    try:
        if a.mode == "plan":
            return m.mode_plan()
        if a.mode == "status":
            return m.mode_status()
        if a.mode in ("run", "resume"):
            return m.mode_run(a.mode)
        return m.mode_finish()
    except Abort as e:
        print("\nSTOPPED: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
