"""A fake server for the migrate-os tests: fake sysfs and /dev trees, a root
filesystem fixture, and tests/fakemigrate.py standing in for lsblk, sgdisk,
rsync, mount, chroot, efibootmgr, tmux and the rest. Nothing here touches a
real disk. The serials are made up."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))   # so `python3 -m unittest tests.test_migrate` works from ollama1/

import o1test_util as U
from fakecmd import fu

GIB = 1 << 30
MIB = 1 << 20
DISK = 2000398934016
FROM = "TESTFROM0001"
TO = "TESTTO00002"
LIBPY = os.path.join(os.environ.get("OLLAMA1_TEST_LIB") or os.path.join(U.KIT, "lib"), "o1migrate.py")
WRAPPER = os.path.join(U.TOOLS, "migrate-os.sh")
STUBS = ["lsblk", "blkid", "findmnt", "df", "du", "nvme", "journalctl", "systemctl", "fuser", "sgdisk", "wipefs",
         "mkfs.ext4", "mkfs.vfat", "mkswap", "fallocate", "mount", "umount", "rsync", "chroot", "efibootmgr",
         "partprobe", "udevadm", "sync", "rm", "tmux", "systemd-inhibit", "chattr"]
FSTAB = """# /etc/fstab: static file system information.
# / was on /dev/ubuntu-vg/ubuntu-lv during curtin installation
/dev/disk/by-id/dm-uuid-LVM-abcdef / ext4 defaults 0 1
# /boot was on /dev/nvme0n1p2 during curtin installation
/dev/disk/by-uuid/%(oldboot)s /boot ext4 defaults 0 1
# /boot/efi was on /dev/nvme0n1p1 during curtin installation
/dev/disk/by-uuid/%(oldesp)s /boot/efi vfat defaults 0 1
/swap.img	none	swap	sw	0	0
UUID=%(models)s /srv/models ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2
UUID=%(data)s /srv/data ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2
"""
NVME_OPTS = ["nvme_core.default_ps_max_latency_us=0", "pcie_aspm=off", "pcie_port_pm=off"]
OVERDRIVE = "amdgpu.ppfeaturemask=0xfffd7fff"
OLD_CMDLINE = "BOOT_IMAGE=/vmlinuz-6 root=/dev/mapper/ubuntu--vg-ubuntu--lv ro quiet splash " + " ".join(NVME_OPTS)
NEW_CMDLINE = "BOOT_IMAGE=/vmlinuz-6 root=UUID=x ro quiet splash " + " ".join(NVME_OPTS) + " " + OVERDRIVE + " panic=10"
SERVICES_ACTIVE = ["ollama.service", "ollama1-gateway.service", "ollama1-admin.service", "ollama1-pull@llama.service"]
MDSTAT_OK = ("Personalities : [raid1]\nmd127 : active raid1 sdb1[1] sda1[0]\n      7813894144 blocks super 1.2 [2/2] [UU]\n"
             "      bitmap: 0/59 pages [0KB], 65536KB chunk\n\nunused devices: <none>\n")
MDSTAT_DEGRADED = MDSTAT_OK.replace("[2/2] [UU]", "[2/1] [U_]")
DESTRUCTIVE = {"sgdisk", "wipefs", "mkfs.ext4", "mkfs.vfat", "mkswap", "fallocate", "mount", "umount", "chroot",
               "efibootmgr", "partprobe", "udevadm", "sync", "rsync", "rm", "tmux"}
WRITES_TO_DRIVE = {"sgdisk", "wipefs", "mkfs.ext4", "mkfs.vfat"}


def load_lib():
    spec = importlib.util.spec_from_file_location("o1migrate", LIBPY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Machine:
    """A fake server in a temp folder. swap=True gives the drives the other
    nvme names (the names swap between boots)."""

    def __init__(self, swap=False, models_gib=800, root_gib=40, data_avail_tib=6, multipath=False):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1mig-"))
        d = self.dir
        self.names = {"from": "nvme1n1" if swap else "nvme0n1", "to": "nvme0n1" if swap else "nvme1n1"}
        self.ctrl = {"from": self.names["from"][:5], "to": self.names["to"][:5]}
        for sub in ("sys/class/nvme", "dev", "byid", "bin", "src/etc/default/grub.d", "src/usr/bin", "src/var/lib",
                    "src/home/u", "src/boot/grub", "srv/models", "srv/data", "run", "efi", "state-elsewhere"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        for role, serial in (("from", FROM), ("to", TO)):
            c = os.path.join(d, "sys/class/nvme", self.ctrl[role])
            # native multipath: the controller's own block entry is nvme<subsystem>c<ctrl>n<ns>
            sysname = self.names[role].replace("n1", "c%sn1" % self.ctrl[role][-1]) if multipath else self.names[role]
            os.makedirs(os.path.join(c, sysname))
            self.put(os.path.join(c, "serial"), serial + "\n")
            self.put(os.path.join(c, "state"), "live\n")
            open(os.path.join(d, "dev", self.names[role]), "w").close()
            for suf in ("", "_1"):
                os.symlink(os.path.join(d, "dev", self.names[role]),
                           os.path.join(d, "byid", "nvme-Samsung_SSD_980_PRO_2TB_%s%s" % (serial, suf)))
        self.uu = {"oldboot": fu("1"), "oldesp": fu("2"), "models": fu("3"), "data": fu("4")}
        self.put(d + "/src/etc/fstab", FSTAB % self.uu)
        self.put(d + "/src/etc/crypttab", "# nothing\n")
        self.put(d + "/src/etc/default/grub", 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash %s"\n' % " ".join(NVME_OPTS))
        self.put(d + "/src/etc/default/grub.d/99-ollama1.cfg", "GRUB_TIMEOUT=5\n")
        self.put(d + "/src/etc/default/grub.d/97-amdgpu-overdrive.cfg",
                 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT %s"\n' % OVERDRIVE)
        self.put(d + "/src/usr/bin/tool", "tool\n")
        self.put(d + "/src/var/lib/x", "x\n")
        self.put(d + "/src/home/u/f", "f\n")
        self.put(d + "/src/boot/vmlinuz-1", "kernel\n")
        self.put(d + "/src/boot/grub/grub.cfg", "old grub\n")
        with open(d + "/src/swap.img", "wb") as fh:
            fh.truncate(MIB)
        for n, body in (("a.gguf", "aaaa" * 10), ("b/c.gguf", "cc" * 7)):
            self.put(d + "/srv/models/" + n, body)
        self.drop_caches_file = d + "/drop_caches"
        self.mdstat_file = d + "/mdstat"
        self.put(self.mdstat_file, MDSTAT_OK)
        self.cmdline_file = d + "/cmdline"
        self.put(self.cmdline_file, OLD_CMDLINE + "\n")
        fp = lambda r, n: os.path.join(d, "dev", "%sp%d" % (self.names[r], n))
        for n in (1, 2, 3):
            open(fp("from", n), "w").close()
        open(fp("to", 1), "w").close()
        for n in (1, 2, 3):
            os.symlink(fp("from", n), os.path.join(d, "byid", "nvme-Samsung_SSD_980_PRO_2TB_%s_1-part%d" % (FROM, n)))
        os.symlink(fp("to", 1), os.path.join(d, "byid", "nvme-Samsung_SSD_980_PRO_2TB_%s_1-part1" % TO))
        self.data_dir, self.models_dir = d + "/srv/data", d + "/srv/models"
        lvm = "/dev/mapper/ubuntu--vg-ubuntu--lv"
        self.fs = {
            "devroot": d + "/dev", "byid": d + "/byid", "log": [],
            "disks": {
                self.names["from"]: {"serial": FROM, "model": "FAKE SSD 2TB", "size": DISK, "parts": [
                    {"n": 1, "size": GIB, "type": "vfat", "uuid": self.uu["oldesp"], "partuuid": fu("a"), "label": None},
                    {"n": 2, "size": 2 * GIB, "type": "ext4", "uuid": self.uu["oldboot"], "partuuid": fu("b"), "label": None},
                    {"n": 3, "size": DISK - 3 * GIB - 2 * MIB, "type": "LVM2_member", "uuid": "x", "partuuid": fu("c"), "label": None}],
                    "extra": [{"part": 3, "name": "ubuntu--vg-ubuntu--lv", "path": lvm, "type": "lvm"}]},
                self.names["to"]: {"serial": TO, "model": "FAKE SSD 2TB", "size": DISK,
                                   "stale": {"1": "ext4"},     # the old models partition's superblock is where the new ESP starts
                                   "parts": [
                    {"n": 1, "size": DISK - 2 * MIB, "type": "ext4", "uuid": self.uu["models"], "partuuid": fu("d"), "label": "o1models"}]},
            },
            "mounts": [
                {"target": "/", "source": lvm, "fstype": "ext4", "options": "rw,relatime"},
                {"target": "/boot", "source": fp("from", 2), "fstype": "ext4", "options": "rw,relatime"},
                {"target": "/boot/efi", "source": fp("from", 1), "fstype": "vfat", "options": "rw,relatime"},
                {"target": self.models_dir, "source": fp("to", 1), "fstype": "ext4", "options": "rw,noatime"},
                {"target": self.data_dir, "source": "/dev/md127", "fstype": "ext4", "options": "rw,noatime"},
            ],
            "df": {"/": {"used": root_gib * GIB}, self.models_dir: {"used": models_gib * GIB},
                   self.data_dir: {"avail": data_avail_tib * (1 << 40)}},
            "smart": {self.ctrl["from"]: 0, self.ctrl["to"]: 0}, "klog": "",
            "services_active": list(SERVICES_ACTIVE),
            "efi": {"order": ["0001", "0000"], "entries": {
                "0000": {"label": "UEFI: Built-in EFI Shell", "path": "VenMedia(5023b95c)"},
                "0001": {"label": "ubuntu", "path": "HD(1,GPT,%s,0x800,0x219800)/File(\\EFI\\ubuntu\\shimx64.efi)" % fu("a")}}},
        }
        self.state_path = d + "/fake-state.json"
        self.flush()
        fake = os.path.join(U.HERE, "fakemigrate.py")
        for t in STUBS:
            os.symlink(fake, os.path.join(d, "bin", t))
        self.tty = d + "/tty"
        self.cfg_state = d + "/srv/data/o1migrate/state.json"
        self.status_file = d + "/srv/data/migrate-os.status"
        self.log_file = d + "/srv/data/migrate-os.log"
        self.sudoers = d + "/sudoers-90-ollama1-migrate"
        self.out = ""

    @staticmethod
    def put(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def flush(self):
        with open(self.state_path, "w") as fh:
            json.dump(self.fs, fh)

    def reload(self):
        with open(self.state_path) as fh:
            self.fs = json.load(fh)
        return self.fs

    def env(self, tmux=False, **extra):
        d = self.dir
        e = dict(os.environ, PATH=d + "/bin:" + os.environ["PATH"], FAKE_STATE=self.state_path,
                 O1M_TEST="1", O1M_SYS=d + "/sys", O1M_DEV=d + "/dev", O1M_BYID=d + "/byid", O1M_DATA=self.data_dir,
                 O1M_MODELS=self.models_dir, O1M_MNT=d + "/run/o1migrate", O1M_SRC_ROOT=d + "/src",
                 O1M_TTY=self.tty, O1M_EFI_SYS=d + "/efi", O1M_ALLOW_NONROOT="1", O1M_CMDLINE=self.cmdline_file,
                 O1M_COUNTDOWN_SECS="0", O1M_SUDOERS=self.sudoers, O1M_MDSTAT=self.mdstat_file,
                 O1M_DROP_CACHES=self.drop_caches_file)
        e.pop("TMUX", None)
        if not tmux:
            e["O1_NO_TMUX"] = "1"
        e.update(extra)
        return e

    def run(self, *args, answers=None, serials=True, tty=True, tmux=False, **env):
        if tty:
            self.put(self.tty, "\n".join(answers if answers is not None else [TO]) + "\n")
        else:
            env["O1M_TTY"] = self.dir + "/no-such-tty"
        argv = [sys.executable, LIBPY]
        if serials:
            argv += ["--from-serial", FROM, "--to-serial", TO]
        argv += list(args)
        self.flush()
        old = os.umask(0o077)       # so only an explicit chmod can make the status file readable by all
        try:
            r = subprocess.run(argv, env=self.env(tmux=tmux, **env), capture_output=True, text=True, timeout=300)
        finally:
            os.umask(old)
        self.reload()
        self.out = r.stdout + r.stderr
        return r.returncode

    def popen(self, *args, tmux=False, **env):
        self.put(self.tty, TO + "\n")
        argv = [sys.executable, "-u", LIBPY, "--from-serial", FROM, "--to-serial", TO] + list(args)
        self.flush()
        old = os.umask(0o077)
        try:
            return subprocess.Popen(argv, env=self.env(tmux=tmux, **env), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        finally:
            os.umask(old)

    def log(self, cmd=None):
        self.reload()
        ls = self.fs["log"]
        return [l for l in ls if cmd is None or l.split()[0] == cmd]

    def byid(self, role, part=None):
        s = FROM if role == "from" else TO
        p = os.path.join(self.dir, "byid", "nvme-Samsung_SSD_980_PRO_2TB_%s_1" % s)
        return p + ("-part%d" % part if part else "")

    def set_state(self, **kw):
        self.reload()
        self.fs.update(kw)
        self.flush()

    def power_cycle(self):
        """What a reboot does to the fake: the mounts of the migration are gone."""
        self.reload()
        self.fs["mounts"] = [m for m in self.fs["mounts"] if not m["target"].startswith(self.dir + "/run/")]
        self.flush()

    def reboot_into_new(self):
        """The fake machine after a reboot: / /boot /boot/efi /srv/models from the new drive."""
        self.reload()
        to = self.names["to"]
        src = lambda n: self.dir + "/dev/%sp%d" % (to, n)
        keep = [x for x in self.fs["mounts"] if x["target"] == self.data_dir]
        for t, n in (("/", 3), ("/boot", 2), ("/boot/efi", 1), (self.models_dir, 4)):
            keep.append({"target": t, "source": src(n), "fstype": "ext4", "options": "rw,relatime"})
        self.fs["mounts"] = keep
        self.flush()
        self.put(self.cmdline_file, NEW_CMDLINE + "\n")

    def state(self):
        with open(self.cfg_state) as fh:
            return json.load(fh)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)
