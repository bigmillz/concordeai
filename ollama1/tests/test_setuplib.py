"""setup.sh's disk steps (lib/setuplib.sh) against fake disk tools: which
disks get wiped, when a filesystem gets made, and what reaches fstab.
No real disk is ever touched: every tool is tests/fakecmd.py."""
import json
import os
import re
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


class TestNoMirror(unittest.TestCase):
    """6b400: the kit builds no RAID. The two old mirror disks are accepted on the command line and in setup.env, and
    ignored: nothing wipes, assembles or mounts them, no step names them, nothing says mdadm."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.roles = ('OS_DISK="%s"; OS_SERIAL=SERIAL-OS; MODELS_DISK="%s"; MODELS_SERIAL=SERIAL-MODELS; '
                      % (self.sb.nvme, self.sb.nvme))

    def setup_text(self):
        return open(os.path.join(U.KIT, "setup.sh")).read()

    def lib_text(self):
        return open(LIBSH).read()

    def test_no_function_builds_assembles_or_looks_for_an_array(self):
        for fn in ("raid_step", "raid_find", "raid_done", "raid_members_on_disk", "md_saved_uuid", "md_line_is_ours"):
            self.assertEqual(self.sb.run("type %s" % fn), 1, fn)
        lib = re.sub(r"(?m)^\s*#.*$", "", self.lib_text())
        for word in ("mdadm", "mdadm.conf", "MD_DEV", "MD_NAME", "/srv/data", "DATA_MNT", "raid.mkfs-pending"):
            self.assertNotIn(word, lib, word)

    def test_setup_has_no_mirror_step_no_mdadm_package_and_no_srv_data(self):
        text = re.sub(r"(?m)^\s*#.*$", "", self.setup_text())
        for word in ("mdadm", "raid_step", "raid_done", "raid_find", "RAID_PENDING", "/srv/data", "home_on_own_disk"):
            self.assertNotIn(word, text, word)
        self.assertNotIn("Mirror (", text)

    def test_the_plan_says_no_mirror_not_used_and_never_wipes_the_old_disks(self):
        rc = self.sb.run('OS_DISK="%s"; OS_SERIAL=SERIAL-OS; MODELS_DISK="%s"; MODELS_SERIAL=SERIAL-MODELS; '
                         'HDD1="%s"; HDD1_SERIAL=SERIAL-HDD1; HDD2="%s"; HDD2_SERIAL=SERIAL-HDD2; show_disks; plan_wipes; '
                         'echo "WIPES=${#WIPES[@]}"; for w in "${WIPES[@]}"; do echo "W: $w"; done'
                         % (self.sb.dir + "/os", self.sb.nvme, self.sb.sda, self.sb.sdb))
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("WIPES=1", self.sb.out)
        self.assertIn("W: %s (SERIAL-MODELS" % self.sb.nvme, self.sb.out)
        self.assertIn("no mirror: not used", self.sb.out)
        self.assertIn("old mirror serials are ignored", self.sb.out)
        self.assertNotIn("RAID1", self.sb.out)
        self.assertNotIn("SERIAL-HDD", self.sb.out.replace("old mirror serials are ignored", ""))

    def test_old_hdd_serials_change_nothing_check_disks_passes_and_no_tool_touches_them(self):
        mnt = os.path.join(self.sb.dir, "srv-models")
        nv = self.sb.nvme
        self.sb.state["findmnt_source"] = {"/": nv + "p3", mnt: nv + "p4"}
        self.sb.state["devs"][nv] = [nv, nv + "p3", nv + "p4"]
        self.sb.state["up"] = {nv + "p3": "%sp3 part\n%s disk" % (nv, nv), nv + "p4": "%sp4 part\n%s disk" % (nv, nv)}
        self.sb.state["mounted"] = [mnt]
        self.sb.save()
        rc = self.sb.run(self.roles + 'HDD1="%s"; HDD1_SERIAL=SERIAL-HDD1; HDD2="%s"; HDD2_SERIAL=SERIAL-HDD2; check_disks'
                         % (self.sb.sda, self.sb.sdb))
        self.assertEqual(rc, 0, self.sb.out)
        for tool in ("wipefs", "sgdisk", "mkfs.ext4", "partprobe", "mdadm"):
            self.assertEqual(self.sb.log(tool), [], tool)
        self.assertNotIn(self.sb.sda, " ".join(self.sb.state["log"]))
        self.assertNotIn(self.sb.sdb, " ".join(self.sb.state["log"]))

    def test_the_hdd_options_are_accepted_with_a_note_and_still_checked_for_shape(self):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--plan", "--hdd1-serial", "SERIAL-HDD1",
                            "--hdd2-serial", "SERIAL-HDD2"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("--hdd1-serial and --hdd2-serial are ignored", out)
        self.assertIn("6. No mirror: not used", out)
        self.assertNotIn("RAID1", out)
        self.assertNotIn("Mirror:", out)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--plan"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotIn("ignored", r.stdout + r.stderr)                        # no note when they are not given
        self.assertIn("No mirror: not used", r.stdout)

    def test_a_saved_setup_env_with_hdd_serials_still_works_and_is_not_asked_for(self):
        s = self.setup_text()
        self.assertNotIn("ask_setting HDD", s)                                   # never prompts for them
        self.assertIn('[ -z "$HDD1_SERIAL$HDD2_SERIAL" ] || printf', s)          # kept in setup.env only if present
        self.assertIn('pick HDD1_SERIAL "$A_HDD1" HDD1_SERIAL "" valid_serial', s)

    def test_the_backup_folder_is_one_constant_that_the_python_side_agrees_with(self):
        import o1common
        out = subprocess.run(["bash", "-c", '. "%s"; echo "$BACKUP_DIR $STATE_DIR"' % LIBSH], capture_output=True,
                             text=True, env={k: v for k, v in os.environ.items() if k not in ("BACKUP_DIR", "STATE_DIR")}).stdout.split()
        self.assertEqual(out, [o1common.Paths.backups[len(o1common.PREFIX):], o1common.Paths.state[len(o1common.PREFIX):]])


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

    def test_unexpected_signature_crypto_luks_refused(self):
        self.sb.state["blkid"][self.sb.nvme] = {"TYPE": "crypto_LUKS", "UUID": fu("7")}
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("crypto_LUKS", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_blkid_read_error_stops_before_wiping(self):
        self.sb.state["blkid_fail"] = [self.sb.nvme]
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_a_disk_still_in_fstab_is_refused(self):
        with open(self.sb.fstab, "a") as f:
            f.write("/dev/disk/by-uuid/%s /mnt ext4 defaults 0 1\n" % fu("6"))
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("still used by fstab", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_a_disk_with_swap_on_it_is_refused(self):
        with open(os.path.join(self.sb.dir, "swaps"), "w") as f:
            f.write("Filename Type Size Used Priority\n%s partition 1 0 -2\n" % self.sb.nvme)
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("swap is on", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_a_serial_mismatch_is_refused(self):
        self.sb.state["serial"][self.sb.nvme] = "SERIAL-OTHER"
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("serial check failed", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])

    def test_no_uuid_no_fstab_line(self):
        self.sb.state["mkfs_uuid"] = ""
        before = self.sb.fstab_text()
        self.assertEqual(self.sb.run(self.call), 1)
        self.assertIn("no filesystem UUID", self.sb.out)
        self.assertEqual(self.sb.fstab_text(), before)

    def test_set_fstab_refuses_empty_uuid_and_replaces_its_own_line(self):
        before = self.sb.fstab_text()
        self.assertEqual(self.sb.run('set_fstab /srv/models "UUID= /srv/models ext4 defaults 0 2"'), 1)
        self.assertEqual(self.sb.fstab_text(), before)
        self.assertEqual(self.sb.run('set_fstab /srv/models "UUID=0f0f0f0f-1111 /srv/models ext4 defaults 0 2"'), 0)
        self.assertEqual(self.sb.run('set_fstab /srv/models "UUID=0f0f0f0f-2222 /srv/models ext4 defaults 0 2"'), 0)
        self.assertEqual([l for l in self.sb.fstab_text().splitlines() if "/srv/models" in l],
                         ["UUID=0f0f0f0f-2222 /srv/models ext4 defaults 0 2"])

    def test_the_marker_is_cleared_after_mkfs(self):
        self.assertEqual(self.sb.run(self.call), 0, self.sb.out)
        self.assertEqual(len(self.sb.log("mkfs.ext4")), 1)
        self.assertFalse(os.path.exists(os.path.join(self.sb.dir, "state", "models.mkfs-pending")))


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
        self.assertIn('grow_root_if_lvm "$VG_RESERVE_GIB"', setup)
        self.assertIn("VG_RESERVE_GIB=0\n", setup)                       # default: no reserve
        self.assertNotIn("lvextend", setup)                               # only through grow_root
        self.assertLess(setup.index('grow_root_if_lvm "$VG_RESERVE_GIB"'), setup.index('step "/home"'))


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
        b = setup.index('  ask_yes "Type yes to go ahead: "')
        self.assertLess(a, b)
        self.assertIn('if ssh_outside_lan "$CLIENT_IP" "$HOME_LAN"; then', setup[a:b])
        self.assertIn('ask_yes "Type yes to continue anyway: " || { echo "Nothing changed."; exit 1; }', setup[a:b])
        self.assertIn('grep -qix "allowusers $ADMIN_USER@$HOME_LAN"', setup)
        self.assertIn('valid_lan_shape "$v"', setup)                       # --lan: the shape now, the policy after every flag
        self.assertIn('if [ -n "$A_LAN" ] && ! valid_lan "$A_LAN"; then', setup)
        self.assertIn("--lan-public-ok) LAN_PUBLIC_OK=1 ;;", setup)
        self.assertIn('if [ -n "$HOME_LAN" ] && ! valid_lan "$HOME_LAN"; then', setup)   # a detected LAN is not used unasked

    def test_no_type_yes_unless_a_disk_is_erased(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        i = setup.index('if [ "${#WIPES[@]}" -gt 0 ]; then\n  ask_yes "Type yes to go ahead: "')
        seg = setup[i:i + 300]
        self.assertIn('echo "No disk is erased. Going ahead."', seg)
        self.assertEqual(setup.count('ask_yes "Type yes to go ahead: "'), 1)       # only on the erase branch
        self.assertIn('ask_yes "Type yes to continue anyway: "', setup)            # outside the LAN: still asked

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


class TestOsAndModelsOnOneDisk(unittest.TestCase):
    """6b374: a server moved with migrate-os has the system and the models on ONE disk (plain partitions, / on p3,
    /srv/models on p4). Setup must accept that, wipe nothing on that disk, and still
    refuse every other way of two roles landing on the OS disk."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        sb = self.sb
        self.new = sb.nvme                                           # the disk with the system and the models
        self.old = os.path.join(sb.dir, "nvme0n1")                   # the old drive, still in the machine
        open(self.old, "w").close()
        os.symlink(self.old, os.path.join(sb.byid, "nvme-Samsung_SSD_980_PRO_2TB_SERIAL-OLD"))
        self.models_mnt = os.path.join(sb.dir, "srv-models")
        st = sb.state
        st["serial"][self.old] = "SERIAL-OLD"
        st["up"] = {}
        for disk in (self.new, self.old, sb.sda):
            st["devs"][disk] = [disk] + [disk + "p%d" % n for n in (1, 2, 3, 4)]
            for n in (1, 3, 4):
                st["up"][disk + "p%d" % n] = "%sp%d part\n%s disk" % (disk, n, disk)
        st["findmnt_source"] = {"/": self.new + "p3", self.models_mnt: self.new + "p4"}
        st["mounted"] = [self.models_mnt]
        sb.save()

    def roles(self, os_disk, os_serial, models_disk, models_serial):
        return 'OS_DISK="%s"; OS_SERIAL=%s; MODELS_DISK="%s"; MODELS_SERIAL=%s; ' % (os_disk, os_serial, models_disk, models_serial)

    def check(self, os_disk, os_serial, models_disk, models_serial, extra=""):
        return self.sb.run(self.roles(os_disk, os_serial, models_disk, models_serial) +
                           'check_disks; plan_wipes; echo "WIPES=${#WIPES[@]}"; ' + extra)

    def no_disk_writes(self):
        for tool in ("wipefs", "sgdisk", "mkfs.ext4", "partprobe", "lvextend"):
            self.assertEqual(self.sb.log(tool), [], tool)
        self.assertEqual(self.sb.log("mdadm"), [])

    def test_the_moved_server_passes_the_guards_and_wipes_nothing(self):
        rc = self.check(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("WIPES=0", self.sb.out)
        self.assertNotIn("DIE", self.sb.out)
        self.no_disk_writes()

    def test_the_plan_says_models_are_on_the_os_disk(self):
        rc = self.sb.run(self.roles(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS") + "show_disks")
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("models: on the OS disk, already set up", self.sb.out)
        self.assertIn("OS + models: kept, never wiped", self.sb.out)
        self.assertIn("root is a plain partition, left as it is", self.sb.out)
        self.assertNotIn("WIPED -> ext4", self.sb.out)
        self.assertIn("no mirror: not used", self.sb.out)

    def test_the_old_setup_env_still_refuses_and_says_what_to_run(self):
        # OS_SERIAL is the old drive's: that refusal stays, and it names the one line that fixes it
        rc = self.check(self.old, "SERIAL-OLD", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 1)
        self.assertIn("refusing to touch any disk", self.sb.out)
        self.assertIn("--os-serial SERIAL-MODELS --models-serial SERIAL-MODELS", self.sb.out)
        self.assertNotIn("WIPES=", self.sb.out)
        self.no_disk_writes()

    def test_the_old_serial_alone_refuses_without_the_hint_when_models_are_elsewhere(self):
        self.sb.state["findmnt_source"][self.models_mnt] = self.sb.sda + "p1"
        self.sb.save()
        rc = self.check(self.old, "SERIAL-OLD", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 1)
        self.assertIn("/ is not on the disk with serial SERIAL-OLD; refusing to touch any disk", self.sb.out)
        self.assertNotIn("--os-serial", self.sb.out)

    def test_models_on_another_disk_are_still_wiped_and_only_that_disk(self):
        # the system on the old drive, /srv/models not set up: the models disk is the one that gets wiped
        self.sb.state["findmnt_source"] = {"/": self.old + "p3"}
        self.sb.state["mounted"] = []
        self.sb.save()
        rc = self.check(self.old, "SERIAL-OLD", self.new, "SERIAL-MODELS", 'for w in "${WIPES[@]}"; do echo "W: $w"; done')
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("WIPES=1", self.sb.out)
        self.assertIn("W: %s (SERIAL-MODELS" % self.new, self.sb.out)
        self.assertNotIn("SERIAL-OLD)", self.sb.out)
        self.assertEqual(self.sb.log("wipefs"), [])          # planning wipes nothing; models_step does, below
        rc = self.sb.run(self.roles(self.old, "SERIAL-OLD", self.new, "SERIAL-MODELS") + 'models_step "%s" SERIAL-MODELS' % self.new)
        self.assertEqual(rc, 0, self.sb.out)
        self.assertEqual([l.split()[-1] for l in self.sb.log("wipefs")],
                         [os.path.join(self.sb.byid, "nvme-Samsung_SSD_980_PRO_2TB_SERIAL-MODELS")])

    def test_everything_still_to_do_lists_only_the_models_disk(self):
        self.sb.state["findmnt_source"] = {"/": self.old + "p3"}
        self.sb.state["mounted"] = []
        self.sb.save()
        rc = self.check(self.old, "SERIAL-OLD", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("WIPES=1", self.sb.out)

    def test_the_same_disk_without_models_mounted_from_it_dies(self):
        self.sb.state["mounted"].remove(self.models_mnt)                          # not mounted
        self.sb.save()
        rc = self.check(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 1)
        self.assertIn("is both the OS disk and the models disk, but /srv/models is not mounted from it", self.sb.out)
        self.assertIn("never wipes the OS disk", self.sb.out)
        self.assertNotIn("WIPES=", self.sb.out)
        self.no_disk_writes()

    def test_the_same_disk_with_models_mounted_from_another_disk_dies(self):
        self.sb.state["findmnt_source"][self.models_mnt] = self.sb.sda + "p1"
        self.sb.save()
        rc = self.check(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 1)
        self.assertIn("never wipes the OS disk", self.sb.out)
        self.assertNotIn("WIPES=", self.sb.out)
        self.no_disk_writes()

    def test_models_that_are_the_root_filesystem_itself_refuse(self):
        self.sb.state["findmnt_source"][self.models_mnt] = self.new + "p3"       # the same partition as /
        self.sb.save()
        rc = self.check(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS")
        self.assertEqual(rc, 1)
        self.assertIn("root filesystem itself", self.sb.out)

    def test_plan_wipes_never_lists_the_os_disk(self):
        self.sb.state["mounted"].remove(self.models_mnt)
        self.sb.save()
        rc = self.sb.run(self.roles(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS") + 'plan_wipes; echo "WIPES=${#WIPES[@]}"')
        self.assertEqual(rc, 1)
        self.assertIn("refusing to wipe the OS disk", self.sb.out)
        self.assertNotIn("WIPES=", self.sb.out)

    def test_models_step_never_wipes_the_os_disk(self):
        self.sb.state["mounted"].remove(self.models_mnt)
        self.sb.save()
        rc = self.sb.run(self.roles(self.new, "SERIAL-MODELS", self.new, "SERIAL-MODELS") + 'models_step "%s" SERIAL-MODELS' % self.new)
        self.assertEqual(rc, 1)
        self.assertIn("is the OS disk; not wiping", self.sb.out)
        self.no_disk_writes()

    def test_a_serial_that_does_not_match_still_refuses(self):
        rc = self.sb.run(self.roles(self.new, "SERIAL-MODELS", self.new, "WRONG") + "check_disks")
        self.assertEqual(rc, 1)
        self.assertIn("serial check failed", self.sb.out)

    def test_setup_uses_the_checked_functions(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        for word in ("check_disks\n", "plan_wipes\n", "grow_root_if_lvm"):
            self.assertIn(word, setup)
        self.assertNotIn('[ "$d" != "$OS_DISK" ]', setup)          # the guards live in lib/setuplib.sh now, tested here
        self.assertNotIn("WIPES+=", setup)


class TestRootNotOnLvm(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.root = os.path.join(self.sb.dir, "nvme1n1p3")
        self.sb.state.update({"findmnt_source": {"/": self.root}, "vgs": "  451190 ", "log": []})

    def grow(self, up):
        self.sb.state["up"] = {self.root: up}
        return self.sb.run('grow_root_if_lvm 0; echo "LEFT=$ROOT_VG_FREE_EXT"; root_on_lvm && echo LVM || echo PLAIN')

    def test_a_plain_partition_root_is_left_alone(self):
        rc = self.grow("%s part\n%s disk" % (self.root, self.sb.nvme))
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("PLAIN", self.sb.out)
        self.assertIn("not on LVM", self.sb.out)
        self.assertEqual(self.sb.log("lvextend"), [])
        self.assertEqual(self.sb.log("vgs"), [])                  # not even asked about a volume group
        for tool in ("sgdisk", "wipefs", "partprobe", "mkfs.ext4"):
            self.assertEqual(self.sb.log(tool), [], tool)

    def test_an_lvm_root_still_grows(self):
        rc = self.grow("/dev/mapper/ubuntu--vg-ubuntu--lv lvm\n%s part\n%s disk" % (self.root, self.sb.nvme))
        self.assertEqual(rc, 0, self.sb.out)
        self.assertIn("LVM", self.sb.out)
        self.assertEqual(self.sb.log("lvextend"), ["lvextend -r -l +451190 /dev/ubuntu-vg/ubuntu-lv"])

    def test_no_answer_about_root_is_not_lvm(self):
        self.sb.state["findmnt_source"] = {}
        rc = self.sb.run('grow_root_if_lvm 0; root_on_lvm && echo LVM || echo PLAIN')
        self.assertEqual(rc, 0, self.sb.out)
        self.assertEqual(self.sb.log("lvextend"), [])
