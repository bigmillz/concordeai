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
         "update-initramfs", "findmnt", "mountpoint", "mount", "vgs", "systemctl", "lvextend"]
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
        os.symlink(self.sda, os.path.join(self.byid, "ata-ST8000DM004-2CX188_SERIAL-HDD1"))
        os.symlink(self.sdb, os.path.join(self.byid, "ata-ST8000DM004-2U9188_SERIAL-HDD2"))
        os.symlink(self.nvme, os.path.join(self.byid, "nvme-Samsung_SSD_980_PRO_2TB_SERIAL-MODELS"))
        self.fstab = os.path.join(self.dir, "fstab")
        with open(self.fstab, "w") as f:
            f.write("/dev/disk/by-uuid/root / ext4 defaults 0 1\n"
                    "# ollama1: /home now lives on the root filesystem\n"
                    "# /dev/disk/by-uuid/%s /home ext4 defaults 0 1\n" % OLD_HOME_UUID)
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = {
            "serial": {self.sda: "SERIAL-HDD1", self.sdb: "SERIAL-HDD2", self.nvme: "SERIAL-MODELS"},
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
        self.call = 'raid_step "%s" "%s" SERIAL-HDD1 SERIAL-HDD2' % (self.sb.sda, self.sb.sdb)
        self.md = os.path.join(self.sb.dir, "md-o1data")

    def tearDown(self):
        self.sb.close()

    def data_lines(self):
        return [l for l in self.sb.fstab_text().splitlines() if "srv-data" in l]

    def test_fresh_build(self):
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        wiped = [l.split()[-1] for l in self.sb.log("wipefs")]
        self.assertEqual(wiped, [os.path.join(self.sb.byid, "ata-ST8000DM004-2CX188_SERIAL-HDD1"),
                                 os.path.join(self.sb.byid, "ata-ST8000DM004-2U9188_SERIAL-HDD2")])
        self.assertEqual(len(self.sb.log("mkfs.ext4")), 1)
        lines = self.data_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("UUID=00000000-"))

    def test_rerun_after_crash_between_create_and_mkfs(self):
        # the array exists and is running, but has no filesystem; the run
        # that created it left its marker
        self.sb.state["md_detail"] = ["ARRAY %s metadata=1.2 name=ollama1:data UUID=x" % self.md]
        self.sb.state["blkid"][self.md] = {}
        os.makedirs(os.path.join(self.sb.dir, "state"), exist_ok=True)
        open(os.path.join(self.sb.dir, "state", "raid.mkfs-pending"), "w").close()
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])
        self.assertEqual(len(self.sb.log("mkfs.ext4")), 1)
        self.assertTrue(self.data_lines()[0].startswith("UUID=00000000-"))

    def test_found_array_without_filesystem_is_never_formatted(self):
        """No marker: setup didn't make this array, so an unreadable
        filesystem may be damage. Stop with the e2fsck hint, never mkfs."""
        self.sb.state["md_detail"] = ["ARRAY %s metadata=1.2 name=ollama1:data UUID=x" % self.md]
        self.sb.state["blkid"][self.md] = {}
        before = self.sb.fstab_text()
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertEqual(self.sb.log("mkfs.ext4"), [])
        self.assertEqual(self.sb.log("wipefs"), [])
        self.assertIn("e2fsck -b", self.sb.out)
        self.assertIn("mke2fs -n", self.sb.out)
        self.assertEqual(self.sb.fstab_text(), before)

    def test_reassembled_array_without_filesystem_is_never_formatted(self):
        p1, p2 = self.sb.sda + "1", self.sb.sdb + "1"
        self.sb.state["devs"] = {self.sb.sda: [self.sb.sda, p1], self.sb.sdb: [self.sb.sdb, p2]}
        self.sb.state["blkid"] = {p1: {"TYPE": "linux_raid_member"}, p2: {"TYPE": "linux_raid_member"}}
        line = "ARRAY /dev/md/data metadata=1.2 UUID=aaaa1111:bbbb2222:cccc3333:dddd4444 name=ollama1:data"
        self.sb.state["md_examine"] = {p1: line, p2: line}
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertEqual(self.sb.log("mkfs.ext4"), [])
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_blkid_read_error_stops(self):
        self.sb.state["md_detail"] = ["ARRAY %s metadata=1.2 name=ollama1:data UUID=x" % self.md]
        self.sb.state["blkid_fail"] = [self.md]
        os.makedirs(os.path.join(self.sb.dir, "state"), exist_ok=True)
        open(os.path.join(self.sb.dir, "state", "raid.mkfs-pending"), "w").close()
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("blkid could not read", self.sb.out)
        self.assertEqual(self.sb.log("mkfs.ext4"), [])

    def test_blkid_read_error_on_a_disk_stops_before_wiping(self):
        self.sb.state["blkid_fail"] = [self.sb.sdb]
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_marker_cleared_after_mkfs(self):
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertFalse(os.path.exists(os.path.join(self.sb.dir, "state", "raid.mkfs-pending")))

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
        self.sb.state["serial"][self.sb.sdb] = "SERIAL-HDDX"
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
        self.call = 'models_step "%s" SERIAL-MODELS' % self.sb.nvme

    def tearDown(self):
        self.sb.close()

    def test_fresh(self):
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual([l.split()[-1] for l in self.sb.log("wipefs")],
                         [os.path.join(self.sb.byid, "nvme-Samsung_SSD_980_PRO_2TB_SERIAL-MODELS")])
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

    def test_partition_without_filesystem_not_made_here_stops(self):
        part = self.sb.nvme + "p1"
        self.sb.state["devs"][self.sb.nvme] = [self.sb.nvme, part]
        self.sb.state["blkid"] = {part: {"LABEL": "o1models"}}   # label but no readable fs
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertEqual(self.sb.log("mkfs.ext4"), [])
        self.assertIn("e2fsck -b", self.sb.out)

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

    def test_reserve_extents(self):
        # 4 MiB extents: 64 GiB is 16384 of them; a part-extent rounds up
        for gib, want in ((0, 0), (64, 16384), (1, 256)):
            self.assertEqual(self.sb.run('n=$(vg_keep_extents %d) || die "bad"; echo "K=$n"' % gib), 0)
            self.assertIn("K=%d" % want, self.sb.out)
        self.sb.state["vgs_extent"] = "  3000000 "
        self.assertEqual(self.sb.run('n=$(vg_keep_extents 1) || die "bad"; echo "K=$n"'), 0)
        self.assertIn("K=358", self.sb.out)                     # 1073741824 / 3000000 = 357.9
        for bad in ("", "0", "4m"):
            self.sb.state["vgs_extent"] = bad
            self.assertEqual(self.sb.run('n=$(vg_keep_extents 1) || die "bad"; echo "K=$n"'), 1, bad)

    def test_setup_calls_grow_root_with_the_reserve(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('grow_root "$VG_RESERVE_GIB"', setup)
        self.assertIn("VG_RESERVE_GIB=0\n", setup)                       # default: no reserve
        self.assertNotIn("lvextend", setup)                               # only through grow_root
        self.assertLess(setup.index('grow_root "$VG_RESERVE_GIB"'), setup.index('step "/home onto the root filesystem"'))


class TestGrowRoot(unittest.TestCase):
    """setup's step 3, which grows / online: exactly the free space less the
    reserve, once, and it stops on anything unexpected."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def grow(self, gib, free, extent="  4194304 ", **kw):
        self.sb.state.update({"vgs": "  %s " % free, "vgs_extent": extent, "log": []}, **kw)
        rc = self.sb.run('grow_root %d; echo "LEFT=$ROOT_VG_FREE_EXT"' % gib)
        return rc, self.sb.log("lvextend")

    def test_default_takes_everything_like_before(self):
        rc, lv = self.grow(0, 451190)
        self.assertEqual(rc, 0, self.sb.out)
        self.assertEqual(lv, ["lvextend -r -l +451190 /dev/ubuntu-vg/ubuntu-lv"])
        self.assertIn("LEFT=0", self.sb.out)
        # no reserve: the extent size isn't even read
        self.assertFalse([l for l in self.sb.state["log"] if "vg_extent_size" in l])

    def test_reserve_left_free(self):
        rc, lv = self.grow(64, 451190)
        self.assertEqual(rc, 0, self.sb.out)
        self.assertEqual(lv, ["lvextend -r -l +%d /dev/ubuntu-vg/ubuntu-lv" % (451190 - 16384)])
        self.assertIn("LEFT=16384", self.sb.out)

    def test_already_grown_does_nothing(self):
        for gib, free in ((0, 0), (64, 16384), (64, 100), (64, 0)):
            rc, lv = self.grow(gib, free)
            self.assertEqual(rc, 0, (gib, free, self.sb.out))
            self.assertEqual(lv, [], (gib, free))
            self.assertIn("LEFT=%d" % free, self.sb.out)

    def test_second_run_changes_nothing(self):
        self.grow(64, 451190)
        self.sb.state["log"] = []
        rc = self.sb.run('grow_root 64; echo "LEFT=$ROOT_VG_FREE_EXT"')
        self.assertEqual(rc, 0)
        self.assertEqual(self.sb.log("lvextend"), [])

    def test_stops_on_the_unexpected(self):
        rc, lv = self.grow(0, "<1762.47g")
        self.assertEqual((rc, lv), (1, []))
        self.assertIn("couldn't read the free space", self.sb.out)
        rc, lv = self.grow(64, 451190, extent="  4m ")
        self.assertEqual((rc, lv), (1, []))
        self.assertIn("extent size", self.sb.out)
        rc, lv = self.grow(64, 451190, lvextend_leaves=5)
        self.assertEqual(rc, 1)
        self.assertIn("5 free extents after lvextend, not 16384", self.sb.out)
        rc, lv = self.grow(0, 451190, lvextend_fails=True)
        self.assertNotEqual(rc, 0)                     # setup's run() stops on a failed command
        self.assertNotIn("DONE", self.sb.out)


if __name__ == "__main__":
    unittest.main()


class TestSetupArgs(unittest.TestCase):
    """setup.sh's option parse runs before anything else (a non-root run
    stops at the sudo check right after it), so it's safe to run here."""
    def run_setup(self, *args):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh")] + list(args),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def test_a_forgotten_size_is_refused_not_zero(self):
        for flag in ("--vg-reserve", "--encrypted-swap"):
            code, out = self.run_setup("--plan", flag)
            self.assertEqual(code, 2, out)
            self.assertIn("takes a size", out)

    def test_reserve_bounded_so_bash_cannot_wrap_it(self):
        for bad in ("18446744073709551616G", "1000000G", "", "G", "-5G", "1.5G", "5T"):
            code, out = self.run_setup("--vg-reserve", bad)
            self.assertEqual(code, 2, "%r: %s" % (bad, out))

    def test_good_sizes_get_past_the_parse(self):
        if os.geteuid() == 0:
            self.skipTest("as root this would really run setup")
        for good in ("64G", "08G", "999999G"):
            code, out = self.run_setup("--vg-reserve", good)
            self.assertIn("Run it with sudo", out, good)   # reached the sudo check

    def test_bad_settings_are_refused_before_anything_runs(self):
        for flag, bad in (("--name", "Bad_Name"), ("--name", "1abc"), ("--name", "a" * 33), ("--name", "x-"),
                          ("--user", "Bad User"), ("--lan", "10.0.0.5/24"), ("--lan", "10.0.0/24"), ("--lan", "lan"),
                          ("--zone", "nodots"), ("--owner", "a;b"), ("--timezone", "A B"), ("--os-serial", "x y z w"),
                          ("--hdd1-serial", "ab")):
            code, out = self.run_setup("--plan", flag, bad)
            self.assertEqual(code, 2, "%s %r: %s" % (flag, bad, out))
            self.assertIn(flag, out)

    def test_the_lan_must_be_private_and_not_wider_than_a_16_unless_it_is_meant(self):
        for lan in ("8.8.8.0/24", "10.0.0.0/8", "192.0.2.0/24", "0.0.0.0/0", "172.16.0.0/12"):
            code, out = self.run_setup("--plan", "--lan", lan)
            self.assertEqual(code, 2, "%s: %s" % (lan, out))
            self.assertIn("--lan-public-ok", out)
        for args in (("--lan", "8.8.8.0/24", "--lan-public-ok"), ("--lan-public-ok", "--lan", "10.0.0.0/8")):
            code, out = self.run_setup("--plan", *args)
            self.assertEqual(code, 0, "%s: %s" % (args, out))
        code, out = self.run_setup("--plan", "--lan", "10.0.0.0/16")
        self.assertEqual(code, 0, out)

    def test_the_plan_names_the_access_policy_and_what_another_owner_does(self):
        code, out = self.run_setup("--plan", "--name", "testsrv", "--owner", "Alice")
        self.assertEqual(code, 0, out)
        self.assertIn("'testsrv admin - Alice only'", out)
        self.assertIn("different --owner makes a second policy", out)
        self.assertIn("policy_admin_name", out)

    def test_ssh_from_outside_the_lan_needs_an_explicit_yes_and_sshd_must_say_allowusers(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        a = setup.index('CLIENT_IP=$(ssh_client_ip)')
        b = setup.index('ask_yes "Type yes to go ahead: "')
        self.assertLess(a, b)
        self.assertIn('if ssh_outside_lan "$CLIENT_IP" "$HOME_LAN"; then', setup[a:b])
        self.assertIn('ask_yes "Type yes to continue anyway: " || { echo "Nothing changed."; exit 1; }', setup[a:b])
        self.assertIn('grep -qix "allowusers $ADMIN_USER@$HOME_LAN"', setup)
        self.assertIn('valid_lan_shape "$v"', setup)                       # --lan: the shape now, the policy after every flag
        self.assertIn('if [ -n "$A_LAN" ] && ! valid_lan "$A_LAN"; then', setup)
        self.assertIn("--lan-public-ok) LAN_PUBLIC_OK=1 ;;", setup)
        self.assertIn('if [ -n "$HOME_LAN" ] && ! valid_lan "$HOME_LAN"; then', setup)   # a detected LAN is not used unasked

    def test_a_setting_without_its_value_is_refused(self):
        for flag in ("--name", "--user", "--lan", "--zone", "--owner", "--timezone", "--os-serial",
                     "--models-serial", "--hdd1-serial", "--hdd2-serial"):
            code, out = self.run_setup("--plan", flag)
            self.assertEqual(code, 2, flag)
            self.assertIn("takes a value", out)

    def test_the_plan_shows_the_settings_given(self):
        code, out = self.run_setup("--plan", "--name", "testsrv", "--user", "alice", "--lan", "10.0.0.0/24",
                                   "--zone", "Example.Test", "--os-serial", "SERIAL-OS")
        self.assertEqual(code, 0, out)
        self.assertIn("Server name testsrv, SSH user alice, LAN 10.0.0.0/24", out)
        self.assertIn("testsrv.example.test", out)
        self.assertIn("testsrv-admin.example.test", out)
        self.assertIn("only alice with a key", out)

    def test_the_plan_shows_placeholders_for_what_is_not_given(self):
        code, out = self.run_setup("--plan")
        self.assertEqual(code, 0, out)
        for ph in ("<server-name>", "<server-name>.<your-domain>", "<os-serial>"):
            self.assertIn(ph, out)

    def test_the_machine_is_a_setting_not_a_constant_in_setup_sh(self):
        # (test_repo's no-personal-values scan covers the values themselves)
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertNotRegex(setup, r"(?m)^(OS|MODELS|HDD1|HDD2)_SERIAL=\S")      # disks
        self.assertNotRegex(setup, r"(?m)^(ADMIN_USER|HOME_LAN|NEW_HOSTNAME|SERVER_NAME|GW_HOST|ADMIN_HOST)=\S")


class TestSettingsFromTheLibrary(unittest.TestCase):
    """lib/setuplib.sh's validators and the server name's resolution
    (arguments, then saved, then config.json; an old install is 'ollama1')."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="o1settings-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.saved = os.path.join(self.d, "setup.env")
        self.cfg = os.path.join(self.d, "config.json")

    def sh(self, call):
        r = subprocess.run(["bash", "-c", 'set -uo pipefail; source "$LIBSH"; %s' % call], capture_output=True,
                           text=True, env=dict(os.environ, LIBSH=LIBSH, SAVED=self.saved, CONFIG_JSON=self.cfg), timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr

    def write_cfg(self, data):
        with open(self.cfg, "w") as f:
            json.dump(data, f)

    def test_a_wide_or_public_lan_is_valid_only_when_it_is_meant(self):
        for v in ("10.0.0.0/8", "192.0.2.0/24", "8.8.8.0/24", "0.0.0.0/0", "172.16.0.0/12"):
            self.assertEqual(self.sh('LAN_PUBLIC_OK=1; valid_lan "%s"' % v)[0], 0, v)
            self.assertNotEqual(self.sh('valid_lan "%s"' % v)[0], 0, v)
            self.assertNotEqual(self.sh('LAN_PUBLIC_OK=0; valid_lan "%s"' % v)[0], 0, v)
        for v in ("10.0.0.5/24", "x", "10.0.0.0/33"):                       # the flag doesn't excuse a malformed one
            self.assertNotEqual(self.sh('LAN_PUBLIC_OK=1; valid_lan "%s"' % v)[0], 0, v)

    def test_the_ssh_session_s_address(self):
        r = lambda env: self.sh("%s; ssh_client_ip" % env)[1]
        self.assertEqual(r("SSH_CONNECTION='203.0.113.9 5555 10.0.0.2 22'; export SSH_CONNECTION"), "203.0.113.9")
        self.assertEqual(r("unset SSH_CONNECTION; SSH_CLIENT='198.51.100.7 5 22'; export SSH_CLIENT"), "198.51.100.7")
        self.assertEqual(r("SSH_CONNECTION='not-an-address 1 2 22'; export SSH_CONNECTION"), "")
        out = lambda ip, lan: self.sh('ssh_outside_lan "%s" "%s"' % (ip, lan))[0]
        self.assertEqual(out("203.0.113.9", "10.0.0.0/24"), 0)               # outside: warn
        self.assertEqual(out("10.0.0.77", "10.0.0.0/24"), 1)                 # inside: no warning
        self.assertEqual(out("", "10.0.0.0/24"), 1)                          # no SSH session: nothing to say
        self.assertEqual(out("10.0.1.1", "10.0.0.0/24"), 0)

    def test_validators(self):
        good = {"valid_name": ["a", "testsrv", "gpu-2", "a" * 32], "valid_user": ["alice", "_svc", "a-b_c"],
                "valid_lan": ["10.0.0.0/24", "172.16.0.0/16", "192.168.1.0/25", "10.0.0.0/16", "172.31.255.0/24"],
                "valid_zone": ["example.test", "sub.example.co"], "valid_owner": ["Alice", "Alice B. Smith"],
                "valid_tz": ["America/New_York", "UTC"], "valid_serial": ["SERIAL-1", "ab12.cd_3"]}
        bad = {"valid_name": ["", "1a", "A", "a-", "a" * 33, "a_b"], "valid_user": ["", "Alice", "a b", "1a"],
               "valid_lan": ["", "10.0.0.5/24", "10.0.0.0", "10.0.0.0/33", "a.b.c.d/8",
                       # a /8 or a network that isn't private opens SSH to far more than a home or office
                       "10.0.0.0/8", "172.16.0.0/12", "0.0.0.0/0", "192.0.2.0/24", "8.8.8.0/24", "172.15.0.0/16",
                       "172.32.0.0/16", "100.64.0.0/16", "169.254.0.0/16", "11.0.0.0/24"],
               "valid_zone": ["", "example", "Example.test", "-a.test"], "valid_owner": ["", "a;b", "$(x)", " a"],
               "valid_tz": ["", "a b"], "valid_serial": ["", "abc", "a b c d"]}
        for fn, vals in good.items():
            for v in vals:
                self.assertEqual(self.sh('%s "%s"' % (fn, v))[0], 0, (fn, v))
        for fn, vals in bad.items():
            for v in vals:
                self.assertNotEqual(self.sh('%s "%s"' % (fn, v))[0], 0, (fn, v))

    def test_an_old_install_without_a_name_is_ollama1(self):
        self.write_cfg({"admin_email": "alice@example.test", "lan_mode": False})     # what an earlier setup.sh wrote
        self.assertEqual(self.sh('resolve_server_name ""')[:2], (0, "ollama1"))
        self.assertEqual(self.sh('resolve_server_name "ollama1"')[:2], (0, "ollama1"))

    def test_an_old_install_is_never_renamed_by_a_new_argument(self):
        self.write_cfg({"admin_email": "alice@example.test"})
        rc, out, err = self.sh('resolve_server_name "other"')
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("already named 'ollama1'", err)

    def test_a_named_install_keeps_its_name(self):
        self.write_cfg({"server_name": "testsrv"})
        self.assertEqual(self.sh('resolve_server_name ""')[:2], (0, "testsrv"))
        self.assertEqual(self.sh('resolve_server_name "other"')[0], 2)

    def test_a_new_install_asks_for_a_name(self):
        self.assertEqual(self.sh('resolve_server_name ""')[:2], (1, ""))          # nothing yet: the caller asks
        self.assertEqual(self.sh('resolve_server_name "testsrv"')[:2], (0, "testsrv"))
        self.assertEqual(self.sh('resolve_server_name "Bad Name"')[0], 2)

    def test_the_saved_name_is_used_on_a_rerun(self):
        with open(self.saved, "w") as f:
            f.write("SERVER_NAME=srv2\nADMIN_USER=alice\n")
        self.assertEqual(self.sh('resolve_server_name ""')[:2], (0, "srv2"))
        self.assertEqual(self.sh('saved ADMIN_USER')[:2], (0, "alice"))
        self.assertEqual(self.sh('saved NO_SUCH_KEY')[:2], (0, ""))

    def test_no_serial_matches_no_disk(self):
        # a disk that reports no serial would otherwise match an empty one
        blk = os.path.join(self.d, "block")
        os.makedirs(os.path.join(blk, "sda"))
        stub = os.path.join(self.d, "bin")
        os.makedirs(stub)
        with open(os.path.join(stub, "lsblk"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(stub, "lsblk"), 0o755)
        env = 'PATH="%s:$PATH" SYS_BLOCK="%s"' % (stub, blk)
        self.assertEqual(self.sh('%s disk_by_serial ""' % env)[:2], (0, ""))
        self.assertEqual(self.sh('%s disk_by_serial "SERIAL-A"' % env)[:2], (0, ""))


class TestDashFont(unittest.TestCase):
    """dash_font_step (6b359): a big console font for the server's monitor,
    with an opt-out, and never fatal. apt-get and the dashboard are stubs."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="o1dashfont-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.bin = os.path.join(self.d, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.d, "calls.log")
        self.flag = os.path.join(self.d, "etc", "dash-font.off")
        self.stub("apt-get", 'echo "apt-get $*" >>"$LOG"\n[ "${APT_FAIL:-}" = "$4" ] && exit 100\nexit 0')
        self.stub("dash", 'echo "dash $*" >>"$LOG"\n[ -z "${DASH_FAIL:-}" ] || { echo "setfont: no such font"; exit 1; }\n'
                          'echo \'{"font": "/f/ter-v32n.psf.gz", "cols": 120}\'')

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def step(self, env=None, setup=""):
        script = r'''
set -euo pipefail
run() { "$@"; }
ok() { echo "OK: $*"; }
note() { echo "NOTE: $*"; }
die() { echo "DIE: $*"; exit 1; }
source "$LIBSH"
%s
dash_font_step "%s" /dev/tty1
echo DONE
''' % (setup, os.path.join(self.bin, "dash"))
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], LIBSH=LIBSH, LOG=self.log, DASH_FONT_OFF=self.flag)
        e.pop("OLLAMA1_DASH_FONT", None)
        e.update(env or {})
        r = subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True, timeout=30)
        calls = open(self.log).read().splitlines() if os.path.exists(self.log) else []
        if os.path.exists(self.log):
            os.unlink(self.log)
        return r.returncode, r.stdout + r.stderr, calls

    def test_installs_the_fonts_and_loads_one(self):
        rc, out, calls = self.step()
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, ["apt-get install -y -q console-setup-linux", "apt-get install -y -q console-terminus",
                                 "dash --set-font /dev/tty1"])
        self.assertIn("OK: console font:", out)
        self.assertIn("DONE", out)
        self.assertFalse(os.path.exists(self.flag))

    def test_opt_out_changes_nothing_on_the_screen(self):
        rc, out, calls = self.step({"OLLAMA1_DASH_FONT": "off"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, [])                      # no package, no font loaded
        self.assertTrue(os.path.exists(self.flag))       # and o1font will load none at the next start
        self.assertIn("left as it is", out)

    def test_running_setup_again_without_the_opt_out_brings_it_back(self):
        self.step({"OLLAMA1_DASH_FONT": "off"})
        rc, out, calls = self.step()
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.flag))
        self.assertIn("dash --set-font /dev/tty1", calls)

    def test_running_twice_is_the_same(self):
        a = self.step()
        b = self.step()
        self.assertEqual((a[0], a[2]), (b[0], b[2]))

    def test_a_missing_package_or_font_never_stops_setup(self):
        rc, out, calls = self.step({"APT_FAIL": "console-terminus"})
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: console-terminus isn't available", out)
        self.assertIn("dash --set-font /dev/tty1", calls)
        rc, out, _ = self.step({"APT_FAIL": "console-setup-linux"})
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: console-setup-linux did not install", out)
        rc, out, _ = self.step({"DASH_FAIL": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: couldn't load a console font", out)
        self.assertIn("DONE", out)

    def test_setup_calls_it_before_the_dashboard_restarts(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        a = setup.index('dash_font_step "$LIBDIR/bin/ollama1-dash" /dev/tty1')
        self.assertLess(a, setup.index("run systemctl restart ollama1-dash.service"))
        self.assertGreater(a, setup.index("run systemctl enable ollama1-dash.service"))
        self.assertIn("--no-console-font) export OLLAMA1_DASH_FONT=off", setup)
        self.assertIn("OLLAMA1_DASH_FONT=$(printf '%q' \"$OLLAMA1_DASH_FONT\")", setup)    # survives the tmux relaunch
        unit = open(os.path.join(U.KIT, "systemd", "ollama1-dash.service")).read()
        self.assertIn("ExecStartPre=-+/usr/local/lib/ollama1/bin/ollama1-dash --set-font /dev/tty1", unit)
        self.assertIn("User=o1dash", unit)
        self.assertIn("TTYPath=/dev/tty1", unit)
