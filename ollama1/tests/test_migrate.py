"""tools/migrate-os.sh and lib/o1migrate.py, the checks: what is refused, that
--plan changes nothing, that only the right serial lets a run start (typed or
--confirm-serial), that every write to a drive goes through its by-id path
after a fresh serial check, the status file, and the root-owned install with
its one sudoers rule. (The runs themselves are in test_migrate_run.py, the
crash-and-resume sweep in test_migrate_resume.py.) No real disk is touched."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import o1test_util as U
from fakecmd import fu
from migrate_fixture import (DESTRUCTIVE, DISK, FROM, FSTAB, GIB, LIBPY, MIB, TO, WRAPPER, Machine, load_lib)

L = load_lib()


class Base(unittest.TestCase):
    def setUp(self):
        self.m = Machine()

    def tearDown(self):
        self.m.close()


def read(path):
    with open(path) as fh:
        return fh.read()


class TestPure(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(L.parse_size_gib("300G"), 300)
        self.assertEqual(L.parse_size_gib("1T"), 1024)
        for bad in ("300", "G", "0G", "3 G", "-4G", "1.5T", ""):
            with self.assertRaises(L.Abort):
                L.parse_size_gib(bad)

    def test_layout_and_sgdisk(self):
        lay = L.layout(DISK, 300)
        self.assertEqual(lay["models_bytes"], DISK - 2 * MIB - 303 * GIB)
        a = L.sgdisk_args("/dev/disk/by-id/X", 300)
        self.assertEqual(a[-1], "/dev/disk/by-id/X")
        self.assertEqual([x for x in a if x.startswith("-n")],
                         ["-n1:1MiB:+1GiB", "-n2:0:+2GiB", "-n3:0:+300GiB", "-n4:0:0"])
        self.assertEqual([x for x in a if x.startswith("-t")], ["-t1:EF00", "-t2:8300", "-t3:8300", "-t4:8300"])

    def test_fstab_rewrite_keeps_everything_else(self):
        uu = {"root": fu("5"), "boot": fu("6"), "esp": fu("7"), "models": fu("8")}
        old = FSTAB % {"oldboot": fu("1"), "oldesp": fu("2"), "models": fu("3"), "data": fu("4")}
        new, changes = L.rewrite_fstab(old, uu)
        ol, nl = old.splitlines(), new.splitlines()
        self.assertEqual(len(ol), len(nl))
        keep = [i for i, l in enumerate(ol) if "/srv/data" in l or "swap" in l or l.startswith("#")]
        self.assertGreaterEqual(len(keep), 5)
        for i in keep:
            self.assertEqual(ol[i], nl[i])
        self.assertIn("UUID=%s / ext4 defaults 0 1" % uu["root"], nl)
        self.assertIn("UUID=%s /boot ext4 defaults 0 1" % uu["boot"], nl)
        self.assertIn("UUID=%s /boot/efi vfat defaults 0 1" % uu["esp"], nl)
        self.assertIn("UUID=%s /srv/models ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2" % uu["models"], nl)
        self.assertEqual(len(changes), 4)
        for old_uuid in (fu("1"), fu("2"), fu("3")):
            self.assertNotIn(old_uuid, new)
        self.assertNotIn("dm-uuid-LVM", new)

    def test_fstab_rewrite_refuses_what_it_cannot_do_safely(self):
        uu = {"root": fu("5"), "boot": fu("6"), "esp": fu("7"), "models": fu("8")}
        with self.assertRaises(L.Abort):
            L.rewrite_fstab("UUID=x /srv/data ext4 defaults 0 2\n", uu)            # no / line
        with self.assertRaises(L.Abort):
            L.rewrite_fstab("a / ext4 d 0 1\nb / ext4 d 0 1\n", uu)                # two / lines
        new, ch = L.rewrite_fstab("a / ext4 d 0 1\n# UUID=zz /boot ext4 d 0 1\n", uu)   # commented lines stay
        self.assertIn("# UUID=zz /boot ext4 d 0 1", new)
        self.assertTrue(any("added" in c for c in ch))
        self.assertIn("/srv/models ext4 defaults,noatime,nofail", new)             # a models line is added with nofail

    def test_chroot_commands(self):
        cmds = L.chroot_commands("/run/x/root")
        self.assertEqual([c[:2] for c in cmds], [["chroot", "/run/x/root"]] * 3)
        self.assertEqual(cmds[0][-4:], ["update-initramfs", "-u", "-k", "all"])
        self.assertEqual(cmds[1][-6:], ["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi",
                                        "--bootloader-id=o1new", "--recheck", "--no-nvram"])
        self.assertEqual(cmds[2][-1], "update-grub")

    def test_boot_order(self):
        self.assertEqual(L.boot_order("0007", "0001", ["0001", "0000"]), ["0007", "0001", "0000"])
        self.assertEqual(L.boot_order("0007", "0002", ["0001", "0002", "0000"]), ["0007", "0002", "0001", "0000"])
        self.assertEqual(L.boot_order("0007", None, ["0001"]), ["0007", "0001"])
        self.assertEqual(L.boot_order("0007", "0007", ["0007", "0001"]), ["0007", "0001"])

    def test_efi_parse_and_old_entry(self):
        txt = ("BootCurrent: 0001\nBootOrder: 0001,0000\nBoot0000* UEFI: Shell\tVenMedia(1)\n"
               "Boot0001* ubuntu\tHD(1,GPT,%s,0x800,0x219800)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
               "Boot0003  old thing\tHD(1,GPT,%s,0x800,0x2)/File(\\EFI\\x.efi)\n" % (fu("A"), fu("e")))
        efi = L.parse_efi(txt)
        self.assertEqual(efi["order"], ["0001", "0000"])
        self.assertEqual(L.find_old_entry(efi, fu("a")), "0001")
        self.assertIsNone(L.find_old_entry(efi, fu("9")))
        self.assertIsNone(L.find_old_entry(efi, ""))
        self.assertFalse(efi["entries"]["0003"]["active"])

    def test_drive_dead(self):
        self.assertIn("not 'live'", L.drive_dead("nvme0", "dead", "rw", ""))
        self.assertIn("read-only", L.drive_dead("nvme0", "live", "rw,ro", ""))
        self.assertEqual(L.drive_dead("nvme0", "live", "rw,relatime", ""), "")
        down = "nvme nvme0: controller is down; will reset: CSTS=0xffffffff, PCI_STATUS=0xffff"
        self.assertIn("controller is down", L.drive_dead("nvme0", "live", "rw", down))
        self.assertEqual(L.drive_dead("nvme1", "live", "rw", down), "")        # another controller's trouble
        self.assertEqual(L.drive_dead("nvme0", "live", "rw", down + "\nnvme nvme0: 16/0/0 default/read/poll queues"), "")
        self.assertIn("controller is down", L.drive_dead("nvme0", "live", "rw", "nvme nvme0: 16/0/0 default/read/poll queues\n" + down))
        self.assertIn("is down", L.drive_dead("nvme0", "live", "rw", "I/O error, dev nvme0n1\n" + down))

    def test_critical_warning(self):
        self.assertEqual(L.critical_warning("critical_warning                    : 0\n"), 0)
        self.assertEqual(L.critical_warning("critical_warning                    : 0x4\n"), 4)
        self.assertIsNone(L.critical_warning("nothing"))

    def test_kernel_options(self):
        run = "BOOT_IMAGE=/vmlinuz-6 root=/dev/mapper/x ro quiet splash pcie_aspm=off nvme_core.default_ps_max_latency_us=0"
        self.assertEqual(L.cmdline_tokens(run), ["quiet", "splash", "pcie_aspm=off", "nvme_core.default_ps_max_latency_us=0"])
        cfg = "set timeout=5\nmenuentry 'a' {\n\tlinux\t/vmlinuz-6 root=UUID=x ro  quiet splash pcie_aspm=off\n}\n"
        self.assertEqual(L.grub_linux_tokens(cfg), ["root=UUID=x", "ro", "quiet", "splash", "pcie_aspm=off"])
        self.assertEqual(L.grub_linux_tokens("nothing"), [])
        self.assertEqual(L.overdrive_token('GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT amdgpu.ppfeaturemask=0xfffd7fff"'),
                         "amdgpu.ppfeaturemask=0xfffd7fff")
        self.assertEqual(L.overdrive_token("x"), "")

    def test_the_programs_it_can_run(self):
        """A static guard: the only programs the library launches. No LVM
        command (the old drive's volume group is never touched), nothing else."""
        src = read(LIBPY)
        progs = set(re.findall(r'(?:\.run|\.rc|subprocess\.run)\(\s*\["([a-z][a-z0-9.-]*)"', src))
        progs |= set(re.findall(r'(?:argv|base|return)\s*=?\s*\["([a-z][a-z0-9.-]*)"', src))
        progs |= set(re.findall(r'\["(mount|umount)"\]', src))
        allowed = {"lsblk", "blkid", "findmnt", "df", "du", "nvme", "journalctl", "systemctl", "fuser", "sgdisk",
                   "wipefs", "mkfs.ext4", "mkfs.vfat", "mkswap", "fallocate", "mount", "umount", "rsync", "chroot",
                   "efibootmgr", "partprobe", "udevadm", "sync", "rm"}
        stray = {p for p in progs if p not in allowed and not p.startswith(("update-", "grub-"))}
        self.assertEqual(stray, set())
        code = src.split('"""', 2)[2]                          # the docstring may explain why not pvmove
        for bad in ("pvmove", "vgreduce", "vgextend", "pvremove", "lvremove", "lvextend", "pvcreate"):
            self.assertNotIn('"%s"' % bad, code)
        self.assertEqual(src.count('"rm"'), 1)                 # one rm -rf: the parked folder, in --finish
        self.assertIn('"rm", "-rf", "--one-file-system"', src)
        self.assertEqual(src.count('"reboot"'), 1)             # one reboot, in reboot()
        self.assertEqual(src.count('["wipefs"'), 1)

    def test_partition_names_by_id_in_either_family(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="o1part-"))
        try:
            plain, suffixed = os.path.join(d, "nvme-X_SER"), os.path.join(d, "nvme-X_SER_1")
            for byid, make in ((plain, "nvme-X_SER_1-part2"), (suffixed, "nvme-X_SER-part2")):
                drv = L.Drive("SER", "nvme0", "nvme0n1", "/dev/nvme0n1", byid)
                self.assertEqual(drv.part(2), byid + "-part2")                      # nothing exists yet: the plain rule
                open(os.path.join(d, make), "w").close()
                self.assertEqual(drv.part(2), os.path.join(d, make))                # the other family's link is found
                os.remove(os.path.join(d, make))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_runner_in_plan_mode_refuses_anything_that_changes_things(self):
        rn = L.Runner(L.Cfg({}), plan_only=True)
        with self.assertRaises(L.Abort):
            rn.run(["true"])                                   # even a command that would succeed
        rn.run(["true"], ro=True)
        L.Runner(L.Cfg({}), plan_only=False).run(["true"])        # the same command is fine when it is not a plan

    def test_the_hints_name_the_installed_copy_by_its_full_path(self):
        """A sudoers rule names the script itself, so the hints for an installed copy don't say 'bash'."""
        args = L.parse_args(["--from-serial", FROM, "--to-serial", TO, "--resume"])
        for script, want in (("/usr/local/lib/ollama1-migrate/migrate-os.sh", "sudo /usr/local/lib/ollama1-migrate/migrate-os.sh --finish"),
                             ("migrate-os.sh", "sudo bash migrate-os.sh --finish")):
            with mock.patch.dict(os.environ, {"O1_MIGRATE_SCRIPT": script}):
                self.assertEqual(L.Migrator(L.Cfg({}), args).cmdline("finish"), want)
                self.assertTrue(L.Migrator(L.Cfg({}), args).cmdline("run").startswith(want.rsplit(" --", 1)[0] + " --from-serial " + FROM))

    def test_countdown_is_ten_seconds_by_default(self):
        self.assertEqual(L.Cfg({}).countdown, 10)
        self.assertEqual(L.Cfg({}).tmux, "tmux")


class TestRefusals(Base):
    def refused(self, why, *args, rc=1, **kw):
        got = self.m.run(*args, **kw)
        self.assertEqual(got, rc, self.m.out)
        self.assertIn(why, self.m.out)
        calls = {l.split()[0] for l in self.m.log()}
        self.assertEqual(calls & {"sgdisk", "wipefs", "mkfs.ext4", "mkfs.vfat", "rsync", "mount", "umount", "chroot",
                                  "efibootmgr", "fallocate", "tmux"}, set(), self.m.out)
        self.assertFalse(os.path.exists(self.m.cfg_state))

    def test_same_serial(self):
        got = self.m.run("--from-serial", FROM, "--to-serial", FROM, "--run", serials=False)
        self.assertEqual(got, 1)
        self.assertIn("the two serials are the same", self.m.out)
        self.assertFalse(os.path.exists(self.m.cfg_state))

    def test_unknown_serials(self):
        got = self.m.run("--from-serial", FROM, "--to-serial", "NOSUCHDRIVE1", "--run", serials=False)
        self.assertEqual(got, 1)
        self.assertIn("no NVMe drive with the serial NOSUCHDRIVE1", self.m.out)
        self.assertIn(FROM, self.m.out)                       # it lists what IS there
        got = self.m.run("--from-serial", "NOSUCHDRIVE1", "--to-serial", TO, "--plan", serials=False)
        self.assertEqual(got, 1)
        self.assertIn("NOSUCHDRIVE1 (FROM)", self.m.out)

    def test_bad_serial_text(self):
        got = self.m.run("--from-serial", "x y", "--to-serial", TO, "--plan", serials=False)
        self.assertEqual(got, 1)
        self.assertIn("is not a serial number", self.m.out)

    def test_target_with_an_unexpected_mount(self):
        self.m.reload()
        self.m.fs["mounts"].append({"target": "/mnt/other", "source": self.m.dir + "/dev/%sp1" % self.m.names["to"],
                                    "fstype": "ext4", "options": "rw"})
        self.m.flush()
        self.refused("mounted filesystems this tool doesn't know about: /mnt/other", "--run")

    def test_target_with_a_holder(self):
        self.m.reload()
        self.m.fs["disks"][self.m.names["to"]]["extra"] = [{"part": 1, "name": "md9", "path": "/dev/md9", "type": "raid1"}]
        self.m.flush()
        self.refused("something sits on the TO drive", "--run")

    def test_target_swap_is_unexpected(self):
        self.m.reload()
        self.m.fs["swaps"] = [self.m.dir + "/dev/%sp1" % self.m.names["to"]]
        self.m.flush()
        self.refused("[SWAP]", "--run")

    def test_root_not_on_the_from_drive(self):
        self.m.reload()
        for mt in self.m.fs["mounts"]:
            if mt["target"] == "/":
                mt["source"] = self.m.dir + "/dev/%sp1" % self.m.names["to"]
        self.m.flush()
        self.refused("/ is already on the TO drive", "--run")

    def test_too_little_space_to_park_the_models(self):
        m = Machine(models_gib=800, data_avail_tib=0)
        try:
            m.reload()
            m.fs["df"][m.data_dir]["avail"] = 700 * GIB
            m.flush()
            self.assertEqual(m.run("--run"), 1)
            self.assertIn("parking the models needs", m.out)
            self.assertFalse(os.path.exists(m.cfg_state))
            self.assertEqual([l for l in m.log() if l.split()[0] in ("rsync", "sgdisk", "wipefs")], [])
        finally:
            m.close()

    def test_models_would_not_fit_the_new_models_partition(self):
        m = Machine(models_gib=1700)
        try:
            self.assertEqual(m.run("--run"), 1)
            self.assertIn("would not fit the models partition", m.out)
            self.assertEqual(m.run("--run", "--root-size", "30G"), 1)    # 1700G > 1.8T * 0.97 / 1.03 even with a 30G root
        finally:
            m.close()

    def test_root_bigger_than_root_size(self):
        m = Machine(root_gib=400)
        try:
            self.assertEqual(m.run("--run"), 1)
            self.assertIn("--root-size 300G is too small: / uses 400.0 GiB", m.out)
            self.assertEqual(m.run("--plan", "--root-size", "500G"), 0, m.out)   # 400 * 1.2 = 480 <= 500
        finally:
            m.close()

    def test_root_size_between_used_and_used_plus_20_percent(self):
        m = Machine(root_gib=290)
        try:
            self.assertEqual(m.run("--plan"), 1)
            self.assertIn("is too small", m.out)           # 290 * 1.2 = 348 > 300
        finally:
            m.close()

    def test_root_size_floor(self):
        self.assertEqual(self.m.run("--plan", "--root-size", "20G"), 1)
        self.assertIn("the least this tool makes is 30G", self.m.out)

    def test_bad_root_size_text(self):
        self.assertEqual(self.m.run("--plan", "--root-size", "300"), 2)
        self.assertIn("whole number with G or T", self.m.out)

    def test_from_drive_dead_by_controller_state(self):
        self.m.put(os.path.join(self.m.dir, "sys/class/nvme", self.m.ctrl["from"], "state"), "dead\n")
        self.refused("the FROM drive is dead right now: the controller reports 'dead'", "--run")
        self.assertIn("--resume", self.m.out)

    def test_from_drive_dead_by_readonly_root(self):
        self.m.reload()
        for mt in self.m.fs["mounts"]:
            if mt["target"] == "/":
                mt["options"] = "ro,relatime"
        self.m.flush()
        self.refused("remounted read-only", "--run")

    def test_from_drive_dead_by_the_kernel_log(self):
        self.m.set_state(klog="[ 1.0] nvme nvme%s: controller is down; will reset: CSTS=0xffffffff, PCI_STATUS=0xffff\n"
                         % self.m.ctrl["from"][-1])
        self.refused("the kernel log's last word on it", "--run")

    def test_critical_warning(self):
        self.m.set_state(smart={self.m.ctrl["from"]: 0, self.m.ctrl["to"]: 4})
        self.refused("the TO drive reports a critical warning", "--run")

    def test_not_booted_in_uefi(self):
        self.refused("did not boot in UEFI mode", "--run", O1M_EFI_SYS=self.m.dir + "/no-efi")

    def test_apt_running(self):
        self.m.set_state(dpkg_busy=True)
        self.refused("apt/dpkg is running", "--run")

    def test_a_missing_program(self):
        os.remove(os.path.join(self.m.dir, "bin", "sgdisk"))
        got = self.m.run("--plan", PATH=self.m.dir + "/bin:" + os.path.dirname(sys.executable))
        self.assertEqual(got, 1)
        self.assertIn("missing programs: sgdisk", self.m.out)
        self.assertIn("gdisk", self.m.out)

    def test_state_file_on_an_nvme_or_the_root_filesystem(self):
        self.refused("must be on", "--run", O1M_STATE_DIR=self.m.dir + "/state-elsewhere")
        self.assertFalse(os.path.exists(self.m.status_file))       # and nothing else is written either

    def test_state_folder_on_the_models_drive_is_refused(self):
        self.refused("must be on", "--run", O1M_STATE_DIR=self.m.models_dir + "/o1migrate")

    def test_the_status_file_is_never_written_on_an_nvme(self):
        """/srv/data not mounted (or an NVMe): no status, no log, no state folder."""
        self.m.reload()
        self.m.fs["mounts"] = [x for x in self.m.fs["mounts"] if x["target"] != self.m.data_dir]
        self.m.flush()
        self.assertEqual(self.m.run("--run"), 1)
        for f in (self.m.status_file, self.m.log_file, self.m.cfg_state):
            self.assertFalse(os.path.exists(f), f)

    def test_wiping_needs_the_parked_models(self):
        os.makedirs(self.m.data_dir + "/models-parked")           # the folder is there, but the stage never finished
        with mock.patch.dict(os.environ, self.m.env()):           # so the stand-ins would answer if it went on
            mg = L.Migrator(L.Cfg(self.m.env()), L.parse_args(["--from-serial", FROM, "--to-serial", TO, "--run"]))
            mg.state = {"done": {}, "started": {}}
            with self.assertRaises(L.Abort):
                mg.stage_partition()
        self.assertEqual(self.m.log("wipefs"), [])
        self.assertEqual(self.m.log("sgdisk"), [])
        self.assertEqual(self.m.log("umount"), [])


class TestPlan(Base):
    def test_plan_changes_nothing(self):
        def tree():
            return subprocess.run(["find", self.m.dir, "-not", "-name", "fake-state.json*", "-not", "-path", "*/tty"],
                                  capture_output=True, text=True).stdout
        before = tree()
        self.assertEqual(self.m.run("--plan"), 0, self.m.out)
        self.assertEqual(self.m.run(), 0, self.m.out)                   # no mode at all is the plan
        self.assertEqual(before, tree())
        names = {l.split()[0] for l in self.m.log()}
        self.assertEqual(names - {"lsblk", "findmnt", "df", "nvme", "journalctl", "fuser"}, set(), self.m.out)
        self.assertFalse(os.path.exists(self.m.cfg_state))
        for f in (self.m.status_file, self.m.log_file):
            self.assertFalse(os.path.exists(f))
        self.assertEqual(self.m.fs["services_active"], ["ollama.service", "ollama1-gateway.service"])

    def test_plan_says_what_is_read_backed_up_wiped_and_written(self):
        self.assertEqual(self.m.run("--plan"), 0, self.m.out)
        for word in ("READ:", "BACKED UP FIRST", "ERASED", "WRITTEN", "NEVER TOUCHED", "serial " + FROM,
                     "serial " + TO, "1  ESP", "300 GiB", "o1models", "BIOS boot menu (F11)", self.m.cfg_state,
                     self.m.status_file, "firmware boot entry"):
            self.assertIn(word, self.m.out)
        self.assertIn("All checks pass", self.m.out)

    def test_plan_with_a_refusal_exits_1_and_says_so(self):
        m = Machine(root_gib=400)
        try:
            self.assertEqual(m.run("--plan"), 1)
            self.assertIn("WOULD REFUSE TO START", m.out)
        finally:
            m.close()

    def test_plan_works_with_the_nvme_names_swapped(self):
        for swap in (False, True):
            m = Machine(swap=swap)
            try:
                self.assertEqual(m.run("--plan"), 0, m.out)
                lsb = m.log("lsblk")
                self.assertTrue(lsb[0].endswith("/dev/" + m.names["from"]), lsb)
                self.assertTrue(lsb[1].endswith("/dev/" + m.names["to"]), lsb)
                nv = m.log("nvme")
                self.assertEqual([l.split()[-1] for l in nv], ["/dev/" + m.ctrl["from"], "/dev/" + m.ctrl["to"]])
            finally:
                m.close()

    def test_a_plan_that_tried_to_change_something_would_stop(self):
        mg = L.Migrator(L.Cfg(self.m.env()), L.parse_args(["--from-serial", FROM, "--to-serial", TO, "--plan"]))
        self.assertTrue(mg.plan_only)
        for argv in (["true"], ["echo", "x"], ["systemctl", "stop", "ollama.service"], ["sgdisk", "--zap-all", "/x"]):
            with self.assertRaises(L.Abort):
                mg.rn.run(argv)
        self.assertEqual(self.m.log(), [])


class TestConfirmation(Base):
    def calls(self):
        return {l.split()[0] for l in self.m.log()}

    def test_wrong_serial_stops_with_nothing_changed(self):
        for ans in ("", "yes", "wrong", FROM, TO.lower(), TO + " x"):
            got = self.m.run("--run", answers=[ans])
            self.assertEqual(got, 1, ans)
            self.assertIn("that is not the serial of the drive to be erased", self.m.out)
        self.assertEqual(self.calls() & DESTRUCTIVE, set())
        self.assertFalse(os.path.exists(self.m.cfg_state))
        self.assertEqual(self.m.fs["services_active"], ["ollama.service", "ollama1-gateway.service"])

    def test_no_terminal_stops(self):
        self.assertEqual(self.m.run("--run", tty=False), 1)
        self.assertIn("no terminal to ask on", self.m.out)
        self.assertEqual(self.calls() & DESTRUCTIVE, set())

    def test_the_right_serial_runs_it(self):
        self.assertEqual(self.m.run("--run", answers=[TO]), 0, self.m.out)
        self.assertIn("serial>", self.m.out)

    def test_confirm_serial_must_equal_to_serial(self):
        for bad in ("WRONG", FROM, TO.lower(), "", TO + " "):
            self.assertEqual(self.m.run("--run", "--confirm-serial", bad, tty=False), 1, bad)
            self.assertIn("--confirm-serial is not the serial of the drive to be erased", self.m.out)
        self.assertEqual(self.m.log(), [])                           # refused before anything was even read
        self.assertFalse(os.path.exists(self.m.cfg_state))
        self.assertEqual(self.m.run("--resume", "--confirm-serial", "WRONG", tty=False), 1)
        self.assertEqual(self.m.log(), [])

    def test_confirm_serial_needs_no_terminal_and_the_plan_is_printed_and_logged_first(self):
        self.assertEqual(self.m.run("--run", "--confirm-serial", TO, tty=False), 0, self.m.out)
        self.assertNotIn("serial>", self.m.out)
        i, j = self.m.out.index("NEVER TOUCHED"), self.m.out.index("Confirmed with --confirm-serial")
        self.assertLess(i, j)
        log = read(self.m.log_file)
        self.assertLess(log.index("NEVER TOUCHED"), log.index("Confirmed with --confirm-serial"))
        self.assertLess(log.index("Confirmed with --confirm-serial"), log.index("== stage park"))

    def test_resume_asks_again(self):
        self.m.set_state(fail=[{"cmd": "mkfs.ext4", "nth": 1, "rc": 1}])
        self.assertEqual(self.m.run("--run"), 1)
        self.m.set_state(fail=[])
        self.assertEqual(self.m.run("--resume", answers=["nope"]), 1)
        self.assertIn("that is not the serial", self.m.out)

    def test_nothing_is_asked_after_the_confirmation(self):
        """A run confirmed by --confirm-serial never reads a terminal, not even a reboot question."""
        self.assertEqual(self.m.run("--run", "--confirm-serial", TO, answers=["y", "y"]), 0, self.m.out)
        self.assertNotIn("Reboot into the new drive now?", self.m.out)
        self.assertEqual([l for l in self.m.log("systemctl") if l == "systemctl reboot"], [])


class TestRemote(unittest.TestCase):
    """The root-owned install and the one sudoers rule."""

    @staticmethod
    def check_sudoers(text):
        rules = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        assert len(rules) == 1, rules
        m = re.fullmatch(r"([a-z_][a-z0-9_-]*) ALL=\(root\) NOPASSWD: (/usr/local/lib/ollama1-migrate/migrate-os\.sh)", rules[0])
        assert m, rules[0]
        for bad in ("SETENV", "*", "!", ",", "Defaults", "include", "Cmnd_Alias", "NOEXEC", "ALL=(ALL", "(ALL)"):
            assert bad not in rules[0], bad
        assert rules[0].endswith("migrate-os.sh")                   # the command is a single path; no ALL, no arguments
        return m.group(1)

    def test_the_sudoers_text_is_one_narrow_rule(self):
        for user in ("alice", "carol", "svc_admin", "a-b"):
            text = L.sudoers_text(user)
            self.assertEqual(self.check_sudoers(text), user)
            self.assertTrue(text.endswith("\n"))
            self.assertIn("sudo rm /etc/sudoers.d/90-ollama1-migrate", text)
        for bad in ("", "root", "ALL", "a b", "a;b", "Al", "a,b", "x" * 40, "-a", "a\nb"):
            with self.assertRaises(L.Abort):
                L.sudoers_text(bad)

    def test_the_sudoers_text_through_the_wrapper(self):
        r = subprocess.run(["bash", WRAPPER, "--print-sudoers", "--sudoers-user", "alice"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, L.sudoers_text("alice"))
        self.check_sudoers(r.stdout)
        r = subprocess.run(["bash", WRAPPER, "--print-sudoers", "--sudoers-user", "root"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_visudo_accepts_it_when_there_is_a_visudo(self):
        visudo = shutil.which("visudo")
        if not visudo:
            self.skipTest("no visudo here")
        d = tempfile.mkdtemp(prefix="o1sudo-")
        try:
            good = os.path.join(d, "good")
            with open(good, "w") as fh:
                fh.write("alice ALL=(root) NOPASSWD: /bin/true\n")
            if subprocess.run([visudo, "-cf", good], capture_output=True).returncode != 0:
                self.skipTest("visudo -cf can't run for this user")
            mine = os.path.join(d, "mine")
            with open(mine, "w") as fh:
                fh.write(L.sudoers_text("alice"))
            r = subprocess.run([visudo, "-cf", mine], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_installer_and_the_library_agree_on_the_path(self):
        src = read(WRAPPER)
        self.assertIn("INSTALL_DIR=%s\n" % L.INSTALL_DIR, src)
        self.assertIn("SUDOERS=%s\n" % L.SUDOERS_FILE, src)
        self.assertEqual(L.INSTALL_DIR, "/usr/local/lib/ollama1-migrate")
        self.assertEqual(L.SUDOERS_FILE, "/etc/sudoers.d/90-ollama1-migrate")

    def test_install_remote_permissions_and_order(self):
        src = read(WRAPPER)
        self.assertIn('install -d -o root -g root -m 0755 "$INSTALL_DIR"', src)
        self.assertIn('install -o root -g root -m 0644 "$PY" "$INSTALL_DIR/o1migrate.py.new"', src)
        self.assertIn('install -o root -g root -m 0755 "$SELF" "$INSTALL_DIR/migrate-os.sh.new"', src)
        self.assertIn('install -o root -g root -m 0440 "$tmp/90-ollama1-migrate" "$SUDOERS"', src)
        self.assertLess(src.index('visudo -cf "$tmp/90-ollama1-migrate"'), src.index('-m 0440 "$tmp/90-ollama1-migrate"'))
        self.assertLess(src.index("-m 0440"), src.index("visudo -c >/dev/null"))      # the whole set is checked after, and undone
        self.assertIn('rm -f "$SUDOERS"', src)
        # the check that nothing on the way to the installed files is writable by anyone else
        self.assertIn('for x in "$INSTALL_DIR" "$INSTALL_DIR/migrate-os.sh" "$INSTALL_DIR/o1migrate.py"', src)
        self.assertIn('[ "$HERE" != "$INSTALL_DIR" ]', src)       # the installed copy can't be used to reinstall
        self.assertIn("exec env O1_MIGRATE_SCRIPT=", src)
        self.assertIn("python3 -I -u", src)                        # isolated: no PYTHONPATH, no user site
        self.assertNotIn("rm -rf", src)
        self.assertTrue(src.startswith("#!/bin/bash\n"))

    def test_install_remote_needs_root(self):
        r = subprocess.run(["bash", WRAPPER, "--install-remote", "--sudoers-user", "alice"], capture_output=True, text=True)
        if os.geteuid() == 0:
            self.skipTest("running as root")
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs sudo", r.stderr)

    def test_path_safety(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix="o1path-"))
        try:
            f = os.path.join(d, "sub", "migrate-os.sh")
            os.makedirs(os.path.dirname(f))
            open(f, "w").close()

            class St:
                def __init__(self, uid, mode):
                    self.st_uid, self.st_mode = uid, mode

            def lstat(table):
                return lambda p: table.get(p, St(0, 0o40755))
            self.assertEqual(L.unsafe_path_reason(f, lstat({})), "")
            self.assertEqual(L.unsafe_path_reason(f, lstat({f: St(0, 0o100755)})), "")
            self.assertIn("not owned by root", L.unsafe_path_reason(f, lstat({f: St(1000, 0o100755)})))
            self.assertIn("not owned by root", L.unsafe_path_reason(f, lstat({os.path.dirname(f): St(1000, 0o40755)})))
            self.assertIn("writable by group or others", L.unsafe_path_reason(f, lstat({f: St(0, 0o100775)})))
            self.assertIn("writable by group or others", L.unsafe_path_reason(f, lstat({f: St(0, 0o100757)})))
            self.assertIn("writable by group or others", L.unsafe_path_reason(f, lstat({d: St(0, 0o40775)})))
            self.assertIn("writable by group or others", L.unsafe_path_reason(f, lstat({os.path.dirname(d): St(0, 0o41777)})))
            self.assertIn("not owned by root", L.unsafe_path_reason(f, lstat({"/": St(501, 0o40755)})))
            # a symlink in the way is followed, and what it points to is what is checked
            link = os.path.join(d, "link.sh")
            os.symlink(f, link)
            self.assertIn("not owned by root", L.unsafe_path_reason(link, lstat({f: St(1000, 0o100755)})))
            # the real thing: this temp folder is the test user's, so as root it would be refused
            if os.geteuid() != 0:
                self.assertIn("not owned by root", L.unsafe_path_reason(f))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_library_refuses_to_run_as_root_from_a_user_writable_place(self):
        env = dict(os.environ, O1M_FORCE_PATH_CHECK="1")
        r = subprocess.run([sys.executable, LIBPY, "--plan", "--from-serial", FROM, "--to-serial", TO],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing to run as root from here", r.stderr)
        self.assertIn("--install-remote", r.stderr)

    def test_the_wrapper_refuses_to_run_as_root_from_a_user_writable_place(self):
        env = dict(os.environ, O1M_FORCE_PATH_CHECK="1")
        r = subprocess.run(["bash", WRAPPER, "--plan", "--from-serial", FROM, "--to-serial", TO],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to run as root from here", r.stderr)
        self.assertNotIn("PLAN", r.stdout)

    def test_print_sudoers_works_from_a_checkout_even_when_the_path_check_is_on(self):
        """--install-remote runs it as root from the checkout, before anything is installed."""
        env = dict(os.environ, O1M_FORCE_PATH_CHECK="1")
        r = subprocess.run([sys.executable, LIBPY, "--print-sudoers", "--sudoers-user", "alice"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, L.sudoers_text("alice"))

    def test_the_wrapper_path_check_function(self):
        src = read(WRAPPER)
        m = re.search(r"^path_chain_unsafe\(\) \{\n.*?^\}\n", src, re.S | re.M)
        self.assertTrue(m, "path_chain_unsafe() not found")
        body = m.group(0) + 'path_chain_unsafe "$1"; echo "rc=$?"\n'
        d = tempfile.mkdtemp(prefix="o1path-")
        try:
            f = os.path.join(d, "x.sh")
            open(f, "w").close()
            r = subprocess.run(["bash", "-c", body, "_", f], capture_output=True, text=True)
            self.assertIn("rc=0", r.stdout)                                   # unsafe: not root's, or writable
            self.assertIn("not owned by root", r.stdout)
            chain = ["/usr", "/usr/bin", "/usr/bin/true"]
            if all(os.path.exists(p) and os.stat(p).st_uid == 0 and not os.stat(p).st_mode & 0o022 for p in chain + ["/"]):
                r = subprocess.run(["bash", "-c", body, "_", "/usr/bin/true"], capture_output=True, text=True)
                self.assertIn("rc=1", r.stdout)                               # a root-owned, closed chain is fine
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_wrapper_script(self):
        self.assertEqual(subprocess.run(["bash", "-n", WRAPPER]).returncode, 0)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, WRAPPER], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)
        src = read(WRAPPER)
        self.assertIn("set -euo pipefail", src)

    def test_overrides_are_not_taken_from_the_environment_by_root(self):
        """O1M_* folders are honoured for a test user, never for root (a sudo-run copy) unless O1M_TEST=1."""
        e = {"O1M_DATA": "/tmp/elsewhere", "O1M_MNT": "/tmp/x"}
        if os.geteuid() != 0:
            self.assertEqual(L.Cfg(e).data, "/tmp/elsewhere")
        else:
            self.assertEqual(L.Cfg(e).data, "/srv/data")
        self.assertEqual(L.Cfg(dict(e, O1M_TEST="1")).data, "/tmp/elsewhere")


class TestStatusUnit(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix="o1stat-"))
        self.cfg = L.Cfg({"O1M_TEST": "1", "O1M_DATA": self.d, "O1M_HEARTBEAT_SECS": "0.3"})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_modes_and_redaction(self):
        old = os.umask(0o077)
        try:
            st = L.Status(self.cfg, redact=[("SERIAL-A", "<old-drive>"), ("SERIAL-B", "<new-drive>")])
            st.enable()
        finally:
            os.umask(old)
        st.feed("  serial SERIAL-A and SERIAL-B\n 1,234,567  42%  95.0MB/s  0:05:22\n")
        st.stage("copy")
        st.finish("FAILED: SERIAL-B went away", "carry on")
        self.assertEqual(os.stat(self.cfg.status).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(self.cfg.log).st_mode & 0o777, 0o600)
        text = read(self.cfg.status)
        self.assertNotIn("SERIAL-", text)
        self.assertIn("<new-drive>", text)
        self.assertIn("state:      FAILED: <new-drive> went away", text)
        self.assertIn("stage:      3 of 6 (copy / and /boot)", text)
        self.assertIn("SERIAL-A", read(self.cfg.log))             # the log (root only) has everything
        self.assertEqual(os.listdir(self.d).count("migrate-os.status.new"), 0)

    def test_progress_and_last_line(self):
        st = L.Status(self.cfg)
        st.enable()
        st.feed("sending incremental file list\n      1,073,741,824  42%   95.12MB/s    0:05:22 (xfr#1)\r")
        text = read(self.cfg.status)
        self.assertIn("progress:   42% of the current copy", text)
        self.assertIn("last line:  1,073,741,824  42%", text)
        st.stage("restore")                                         # a new stage forgets the old percentage
        self.assertIn("progress:   -", read(self.cfg.status))

    def test_the_heartbeat_moves_updated_without_any_output(self):
        st = L.Status(self.cfg)
        st.enable()
        st.start_heartbeat()
        seen = set()
        end = time.time() + 2.6
        while time.time() < end:
            seen.add(re.search(r"updated:\s+(\S+)", read(self.cfg.status)).group(1))
            time.sleep(0.2)
        st.stop.set()
        self.assertGreaterEqual(len(seen), 2, seen)

    def test_diagnosis_after_a_reboot_or_a_crash(self):
        now = time.time()
        stamp = lambda age: time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now - age))
        text = lambda state, age, boot: L.render_status({"state": state, "stage": "3 of 6 (copy)", "percent": "",
                                                          "last": "x", "started": stamp(age), "updated": stamp(age),
                                                          "boot_id": boot, "next": "n"})
        self.assertIn("INTERRUPTED", L.diagnose_status(text("RUNNING", 5, "old-boot"), "new-boot", now))
        self.assertIn("--resume", L.diagnose_status(text("RUNNING", 5, "old-boot"), "new-boot", now))
        self.assertIn("STALE", L.diagnose_status(text("RUNNING", 600, "b"), "b", now))
        self.assertEqual(L.diagnose_status(text("RUNNING", 5, "b"), "b", now), "")
        self.assertEqual(L.diagnose_status(text("DONE", 5000, "old-boot"), "new-boot", now), "")
        self.assertEqual(L.diagnose_status(text("FAILED: x", 5000, "old-boot"), "new-boot", now), "")

    def test_status_command_needs_neither_root_nor_serials(self):
        m = Machine()
        try:
            self.assertEqual(m.run("--status", serials=False), 1)
            self.assertIn("No migration status", m.out)
            m.put(m.status_file, L.render_status({"state": "RUNNING", "stage": "1 of 6 (park)", "percent": "", "last": "x",
                                                  "started": "2026-01-01T00:00:00+00:00", "updated": "2026-01-01T00:00:00+00:00",
                                                  "boot_id": "some-other-boot", "next": "n"}))
            self.assertEqual(m.run("--status", serials=False), 0)
            self.assertIn("INTERRUPTED", m.out)
            r = subprocess.run(["bash", WRAPPER, "--status"], capture_output=True, text=True,
                               env=m.env())
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("INTERRUPTED", r.stdout)
        finally:
            m.close()


if __name__ == "__main__":
    unittest.main()
