"""setup.sh's disk steps (lib/setuplib.sh) against fake disk tools: which
disks get wiped, when a filesystem gets made, and what reaches fstab.
No real disk is ever touched: every tool is tests/fakecmd.py."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import o1test_util as U
from fakecmd import fu

LIBSH = os.path.join(os.path.dirname(U.LIB) if os.environ.get("OLLAMA1_TEST_LIB") else U.KIT, "lib",
                     "setuplib.sh")
TOOLS = ["lsblk", "blkid", "mdadm", "mkfs.ext4", "sgdisk", "wipefs", "partprobe", "udevadm",
         "update-initramfs", "findmnt", "mountpoint", "mount", "vgs", "systemctl"]
OLD_HOME_UUID = fu("9")


class Sandbox:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="o1setup-")
        self.bin = os.path.join(self.dir, "bin")
        self.byid = os.path.join(self.dir, "by-id")
        os.makedirs(self.bin)
        os.makedirs(self.byid)
        fake = os.path.join(U.HERE, "fakecmd.py")
        for t in TOOLS:
            os.symlink(fake, os.path.join(self.bin, t))
        self.sda = os.path.join(self.dir, "sda")
        self.sdb = os.path.join(self.dir, "sdb")
        self.nvme = os.path.join(self.dir, "nvme1n1")
        for d in (self.sda, self.sdb, self.nvme):
            open(d, "w").close()
        os.symlink(self.sda, os.path.join(self.byid, "ata-ST8000DM004-2CX188_ZR127RMQ"))
        os.symlink(self.sdb, os.path.join(self.byid, "ata-ST8000DM004-2U9188_ZR11ZRGJ"))
        os.symlink(self.nvme, os.path.join(self.byid, "nvme-Samsung_SSD_980_PRO_2TB_S6B0NG0R906564E"))
        self.fstab = os.path.join(self.dir, "fstab")
        with open(self.fstab, "w") as f:
            f.write("/dev/disk/by-uuid/root / ext4 defaults 0 1\n"
                    "# ollama1: /home now lives on the root filesystem\n"
                    "# /dev/disk/by-uuid/%s /home ext4 defaults 0 1\n" % OLD_HOME_UUID)
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = {
            "serial": {self.sda: "ZR127RMQ", self.sdb: "ZR11ZRGJ", self.nvme: "S6B0NG0R906564E"},
            "devs": {self.sda: [self.sda], self.sdb: [self.sdb], self.nvme: [self.nvme]},
            "blkid": {self.sda: {"TYPE": "ext4", "UUID": OLD_HOME_UUID},
                      self.sdb: {"TYPE": "ext4", "UUID": fu("5")},
                      self.nvme: {"TYPE": "ext4", "UUID": fu("6")}},
            "md_detail": [], "md_examine": {}, "mounted": [], "log": [],
        }
        self.save()

    def save(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state, f)

    def load(self):
        with open(self.state_path) as f:
            self.state = json.load(f)
        return self.state

    def run(self, call):
        self.save()
        script = r'''
set -euo pipefail
run() { "$@"; }
ok() { :; }
note() { echo "NOTE: $*"; }
later() { :; }
die() { echo "DIE: $*"; exit 1; }
source "$LIBSH"
dev_busy() { return 1; }
%s
echo DONE
''' % call
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], FAKE_STATE=self.state_path,
                   LIBSH=LIBSH, FSTAB=self.fstab, CRYPTTAB=os.path.join(self.dir, "crypttab"),
                   SWAPS=os.path.join(self.dir, "swaps"), MDADM_CONF=os.path.join(self.dir, "mdadm.conf"),
                   STATE_DIR=os.path.join(self.dir, "state"), BYID=self.byid,
                   MD_DEV=os.path.join(self.dir, "md-o1data"),
                   MODELS_MNT=os.path.join(self.dir, "srv-models"), DATA_MNT=os.path.join(self.dir, "srv-data"))
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.out = r.stdout + r.stderr
        self.load()
        return r.returncode

    def log(self, tool):
        return [l for l in self.state["log"] if l.split()[0] == tool]

    def fstab_text(self):
        return open(self.fstab).read()

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestRaid(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.call = 'raid_step "%s" "%s" ZR127RMQ ZR11ZRGJ' % (self.sb.sda, self.sb.sdb)
        self.md = os.path.join(self.sb.dir, "md-o1data")

    def tearDown(self):
        self.sb.close()

    def data_lines(self):
        return [l for l in self.sb.fstab_text().splitlines() if "srv-data" in l]

    def test_fresh_build(self):
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        wiped = [l.split()[-1] for l in self.sb.log("wipefs")]
        self.assertEqual(wiped, [os.path.join(self.sb.byid, "ata-ST8000DM004-2CX188_ZR127RMQ"),
                                 os.path.join(self.sb.byid, "ata-ST8000DM004-2U9188_ZR11ZRGJ")])
        self.assertEqual(len(self.sb.log("mkfs.ext4")), 1)
        lines = self.data_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("UUID=00000000-"))

    def test_rerun_after_crash_between_create_and_mkfs(self):
        # the array exists and is running, but has no filesystem
        self.sb.state["md_detail"] = ["ARRAY %s metadata=1.2 name=ollama1:data UUID=x" % self.md]
        self.sb.state["blkid"][self.md] = {}
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])
        self.assertEqual(len(self.sb.log("mkfs.ext4")), 1)
        self.assertTrue(self.data_lines()[0].startswith("UUID=00000000-"))

    def test_existing_filesystem_is_kept(self):
        self.sb.state["md_detail"] = ["ARRAY %s metadata=1.2 name=ollama1:data UUID=x" % self.md]
        self.sb.state["blkid"][self.md] = {"TYPE": "ext4", "UUID": fu("a")}
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual(self.sb.log("mkfs.ext4"), [])
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_array_on_disks_but_not_assembled_is_never_wiped(self):
        p1, p2 = self.sb.sda + "1", self.sb.sdb + "1"
        self.sb.state["devs"] = {self.sb.sda: [self.sb.sda, p1], self.sb.sdb: [self.sb.sdb, p2]}
        self.sb.state["blkid"] = {p1: {"TYPE": "linux_raid_member"}, p2: {"TYPE": "linux_raid_member"},
                                  self.md: {"TYPE": "ext4", "UUID": fu("a")}}
        line = "ARRAY /dev/md/data metadata=1.2 UUID=aaaa1111:bbbb2222:cccc3333:dddd4444 name=ollama1:data"
        self.sb.state["md_examine"] = {p1: line, p2: line}
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])
        self.assertEqual(self.sb.log("sgdisk"), [])
        self.assertEqual(self.sb.log("mkfs.ext4"), [])
        self.assertTrue(any("--assemble" in l for l in self.sb.log("mdadm")))

    def test_other_array_on_disks_is_refused(self):
        p1 = self.sb.sda + "1"
        self.sb.state["devs"][self.sb.sda] = [self.sb.sda, p1]
        self.sb.state["blkid"][p1] = {"TYPE": "linux_raid_member"}
        self.sb.state["md_examine"] = {p1: "ARRAY /dev/md/x metadata=1.2 UUID=1:2:3:4 name=nas:stuff"}
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("linux_raid_member", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_unexpected_signature_refused(self):
        self.sb.state["blkid"][self.sb.sdb] = {"TYPE": "crypto_LUKS", "UUID": fu("7")}
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("crypto_LUKS", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_disk_still_in_fstab_refused(self):
        with open(self.sb.fstab, "a") as f:
            f.write("/dev/disk/by-uuid/%s /home ext4 defaults 0 1\n" % OLD_HOME_UUID)
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("still used by fstab", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_disk_in_crypttab_or_swap_refused(self):
        with open(os.path.join(self.sb.dir, "swaps"), "w") as f:
            f.write("Filename Type Size Used Priority\n%s partition 1 0 -2\n" % self.sb.sdb)
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("swap is on", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_serial_mismatch_refused(self):
        self.sb.state["serial"][self.sb.sdb] = "ZR11ZRGX"
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("serial check failed", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_no_uuid_no_fstab_line(self):
        self.sb.state["mkfs_uuid"] = ""
        before = self.sb.fstab_text()
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("no filesystem UUID", self.sb.out)
        self.assertEqual(self.sb.fstab_text(), before)

    def test_set_fstab_refuses_empty_uuid(self):
        before = self.sb.fstab_text()
        self.assertEqual(self.sb.run('set_fstab /srv/data "UUID= /srv/data ext4 defaults 0 2"'), 1)
        self.assertEqual(self.sb.fstab_text(), before)
        self.assertEqual(self.sb.run('set_fstab /srv/data "UUID=0f0f0f0f-1111 /srv/data ext4 defaults 0 2"'), 0)
        self.assertEqual(self.sb.run('set_fstab /srv/data "UUID=0f0f0f0f-2222 /srv/data ext4 defaults 0 2"'), 0)
        self.assertEqual([l for l in self.sb.fstab_text().splitlines() if "/srv/data" in l],
                         ["UUID=0f0f0f0f-2222 /srv/data ext4 defaults 0 2"])


class TestModels(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.call = 'models_step "%s" S6B0NG0R906564E' % self.sb.nvme

    def tearDown(self):
        self.sb.close()

    def test_fresh(self):
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual([l.split()[-1] for l in self.sb.log("wipefs")],
                         [os.path.join(self.sb.byid, "nvme-Samsung_SSD_980_PRO_2TB_S6B0NG0R906564E")])
        self.assertIn("srv-models ext4 defaults,noatime", self.sb.fstab_text())

    def test_label_guard_keeps_models(self):
        part = self.sb.nvme + "p1"
        self.sb.state["devs"][self.sb.nvme] = [self.sb.nvme, part]
        self.sb.state["blkid"] = {part: {"TYPE": "ext4", "LABEL": "o1models",
                                         "UUID": fu("1")}}
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])
        self.assertEqual(self.sb.log("mkfs.ext4"), [])
        self.assertIn("UUID=" + fu("1"), self.sb.fstab_text())

    def test_unexpected_signature(self):
        self.sb.state["blkid"][self.sb.nvme] = {"TYPE": "LVM2_member"}
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertEqual(self.sb.log("wipefs"), [])


class TestVgFree(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.close()

    def test_extents_parse(self):
        self.sb.state["vgs"] = "   451190  "
        self.assertEqual(self.sb.run('n=$(vg_free_extents) || die "unreadable"; echo "N=$n"'), 0)
        self.assertIn("N=451190", self.sb.out)

    def test_unparseable_fails_loudly(self):
        for bad in ("  <1762.47g", "", "1.5", "abc"):
            self.sb.state["vgs"] = bad
            self.assertEqual(self.sb.run('n=$(vg_free_extents) || die "unreadable"; echo "N=$n"'), 1, bad)
            self.assertIn("DIE: unreadable", self.sb.out)


if __name__ == "__main__":
    unittest.main()
