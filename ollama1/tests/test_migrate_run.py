"""migrate-os, the runs: a whole migration against the fake server, what it
writes where (and never to the old drive), the fstab and GRUB on the copy, the
firmware order, the tmux relaunch, the status file, the reboot (flag, prompt,
countdown, never after a failure) and --finish. No real disk is touched."""
import copy
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import unittest

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))   # so `python3 -m unittest tests.test_migrate` works from ollama1/

import o1test_util as U
from fakecmd import fu
from migrate_fixture import (FROM, FSTAB, GIB, LIBPY, MIB, NEW_CMDLINE, NVME_OPTS, OLD_CMDLINE, OVERDRIVE, SERVICES_ACTIVE, TO,
                             WRITES_TO_DRIVE, Machine, load_lib)

L = load_lib()


class Base(unittest.TestCase):
    def setUp(self):
        self.m = Machine()

    def tearDown(self):
        self.m.close()

    def full_run(self, m=None, *args):
        m = m or self.m
        rc = m.run("--run", *args)
        self.assertEqual(rc, 0, m.out)
        return rc


def read(path):
    with open(path) as fh:
        return fh.read()


class SharedRun(unittest.TestCase):
    """One whole run (typed serial, no --reboot), looked at from many sides; read-only tests."""

    @classmethod
    def setUpClass(cls):
        cls.m = Machine()
        cls.before_from = copy.deepcopy(cls.m.fs["disks"][cls.m.names["from"]])
        cls.rc = cls.m.run("--run")
        cls.swapped = Machine(swap=True)
        cls.rc_swapped = cls.swapped.run("--run")

    @classmethod
    def tearDownClass(cls):
        cls.m.close()
        cls.swapped.close()

    def test_a_full_run(self):
        m = self.m
        self.assertEqual(self.rc, 0, m.out)
        st = m.state()
        for s in L.STAGES:
            self.assertIn(s, st["done"])
        self.assertEqual(st["from_serial"], FROM)
        self.assertEqual(st["to_serial"], TO)
        self.assertIn("DONE. The copy is finished and checked", m.out)
        self.assertIn("--finish", m.out)
        self.assertEqual(m.fs["services_active"], [])       # the model services, the admin panel and the pulls were stopped
        # the models are parked, kept, and restored on the new models partition
        self.assertTrue(os.path.isfile(m.data_dir + "/models-parked/a.gguf"))
        self.assertTrue(os.path.isfile(m.dir + "/run/o1migrate/models/b/c.gguf"))
        to = m.fs["disks"][m.names["to"]]["parts"]
        self.assertEqual([(p["n"], p["type"], p["label"]) for p in to],
                         [(1, "vfat", "O1ESP"), (2, "ext4", "o1boot"), (3, "ext4", "o1root"), (4, "ext4", "o1models")])
        self.assertEqual(to[2]["size"], 300 * GIB)
        self.assertEqual(to[0]["size"], GIB)
        root = m.dir + "/run/o1migrate/root"
        self.assertTrue(os.path.isfile(root + "/usr/bin/tool"))
        self.assertTrue(os.path.isfile(root + "/boot/vmlinuz-1"))
        self.assertEqual(os.path.getsize(root + "/swap.img"), MIB)
        self.assertEqual(len(m.log("mkswap")), 1)
        self.assertEqual([x for x in m.fs["mounts"] if x["target"].startswith(m.dir + "/run/")], [])   # all unmounted again
        self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], [])              # no --reboot, no reboot

    def test_the_from_drive_is_never_written_to(self):
        m = self.m
        self.assertEqual(m.fs["disks"][m.names["from"]], self.before_from)
        from_things = [m.byid("from")[:-2], m.byid("from"), m.dir + "/dev/" + m.names["from"], "/dev/mapper"]
        for l in m.log():
            if l.split()[0] in WRITES_TO_DRIVE | {"mkswap", "fallocate"}:
                for f in from_things:
                    self.assertNotIn(f, l)
            if l.split()[0] == "efibootmgr" and ("-c" in l.split()):
                self.assertNotIn(m.byid("from")[:-2], l)
        self.assertEqual(read(m.dir + "/src/etc/fstab"), FSTAB % m.uu)
        self.assertEqual(read(m.dir + "/src/boot/grub/grub.cfg"), "old grub\n")
        self.assertEqual(m.fs["mounts"][0]["target"], "/")
        self.assertEqual([x["target"] for x in m.fs["mounts"] if m.names["from"] in x["source"]], ["/boot", "/boot/efi"])

    def test_drives_are_addressed_by_id_only_whichever_nvme_name_they_have(self):
        for m in (self.m, self.swapped):
            self.assertEqual(self.rc_swapped, 0, self.swapped.out)
            writes = 0
            for l in m.log():
                c = l.split()
                if c[0] in WRITES_TO_DRIVE:
                    writes += 1
                    self.assertTrue(c[-1].startswith(m.dir + "/byid/nvme-Samsung_SSD_980_PRO_2TB_" + TO), l)
                    self.assertNotRegex(l, r"/dev/nvme\d")
                if c[0] == "mount" and "--rbind" not in c and "--make-rslave" not in c and "-t" not in c:
                    self.assertIn(m.dir + "/byid/", l)
            self.assertGreaterEqual(writes, 6)
        self.assertNotEqual(self.m.names["to"], self.swapped.names["to"])

    def test_the_state_file_is_on_the_raid(self):
        self.assertTrue(self.m.cfg_state.startswith(self.m.data_dir + "/"))
        found = subprocess.run(["find", self.m.dir, "-name", "state.json"], capture_output=True, text=True).stdout.split()
        self.assertEqual(found, [self.m.cfg_state])

    def test_fstab_is_rewritten_on_the_copy_only(self):
        m = self.m
        new = read(m.dir + "/run/o1migrate/root/etc/fstab")
        to = {p["n"]: p["uuid"] for p in m.fs["disks"][m.names["to"]]["parts"]}
        self.assertEqual(m.state()["uuids"], {"esp": to[1], "boot": to[2], "root": to[3], "models": to[4]})
        self.assertIn("UUID=%s / ext4 defaults 0 1" % to[3], new)
        self.assertIn("UUID=%s /boot ext4" % to[2], new)
        self.assertIn("UUID=%s /boot/efi vfat" % to[1], new)
        self.assertIn("UUID=%s /srv/models ext4 defaults,noatime,nofail" % to[4], new)
        self.assertIn("UUID=%s /srv/data" % m.uu["data"], new)
        self.assertIn("/swap.img\tnone\tswap\tsw\t0\t0", new)
        self.assertEqual(read(m.dir + "/src/etc/fstab"), FSTAB % m.uu)            # the running system's fstab
        self.assertEqual(read(m.dir + "/run/o1migrate/root/etc/fstab.before-migrate"), FSTAB % m.uu)

    def test_chroot_and_grub(self):
        m = self.m
        root = m.dir + "/run/o1migrate/root"
        calls = [l for l in m.log() if l.split()[0] in ("chroot", "mount", "umount")]
        ch = [l for l in calls if l.startswith("chroot")]
        self.assertEqual(len(ch), 3)
        self.assertTrue(ch[0].endswith("update-initramfs -u -k all"))
        self.assertIn("grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=o1new --recheck --no-nvram", ch[1])
        self.assertTrue(ch[2].endswith("update-grub"))
        for c in ch:
            self.assertTrue(c.startswith("chroot " + root + " /usr/bin/env LANG=C"))
        before = calls[:calls.index(ch[0])]
        for name in ("dev", "sys", "run"):        # bound as slaves: an unmount in there never reaches the host
            self.assertIn("mount --rbind /%s %s/%s" % (name, root, name), before)
            self.assertIn("mount --make-rslave %s/%s" % (root, name), before)
            self.assertLess(before.index("mount --rbind /%s %s/%s" % (name, root, name)),
                            before.index("mount --make-rslave %s/%s" % (root, name)))
        self.assertIn("mount -t proc proc %s/proc" % root, before)
        after = calls[calls.index(ch[2]) + 1:]
        for name in ("run", "sys", "proc", "dev"):
            self.assertIn("umount -R %s/%s" % (root, name), after)
        for f in ("EFI/o1new/shimx64.efi", "EFI/BOOT/BOOTX64.EFI", "EFI/BOOT/grub.cfg"):
            self.assertTrue(os.path.isfile(root + "/boot/efi/" + f), f)

    def test_the_kernel_options_and_the_grub_dropins_reach_the_new_system(self):
        m = self.m
        root = m.dir + "/run/o1migrate/root"
        for f in ("etc/default/grub", "etc/default/grub.d/97-amdgpu-overdrive.cfg", "etc/default/grub.d/99-ollama1.cfg"):
            self.assertEqual(read(root + "/" + f), read(m.dir + "/src/" + f), f)
        grub = read(root + "/boot/grub/grub.cfg")
        for t in NVME_OPTS + [OVERDRIVE]:
            self.assertIn(t, grub.split(), t)
        self.assertEqual(m.state()["cmdline_required"], ["quiet", "splash"] + NVME_OPTS + [OVERDRIVE, "panic=10"])
        self.assertIn("panic=10", grub.split())                      # a kernel panic reboots, into the default boot
        self.assertEqual(read(root + "/etc/default/grub.d/98-ollama1-migrate.cfg"),
                         'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT panic=10"\n')
        self.assertFalse(os.path.exists(m.dir + "/src/etc/default/grub.d/98-ollama1-migrate.cfg"))   # the running system's not touched
        self.assertIn("GRUB on the copy carries", m.out)

    def test_firmware_the_old_drive_stays_the_default_and_the_new_is_bootnext(self):
        m = self.m
        e = m.fs["efi"]
        self.assertEqual(e["order"], ["0001", "0000", "0002"])        # old first; the new entry last
        self.assertEqual(e["next"], "0002")                          # only the next boot tries the new drive
        self.assertIn("efibootmgr -n 0002", m.log("efibootmgr"))
        self.assertNotIn("BootNext was used up", m.out)              # the firmware stage set it; nothing had to set it again
        self.assertIn("A power cycle after a bad first boot comes back to the old drive", m.out)
        self.assertEqual(e["entries"]["0002"]["label"], "ollama1-new")
        self.assertEqual(e["entries"]["0001"]["label"], "ubuntu")
        self.assertEqual(e["entries"]["0001"].get("active", True), True)
        c = [l for l in m.log("efibootmgr") if " -c " in l][0]
        self.assertIn("-d " + m.byid("to")[:-2] + " ", c)          # the by-id name, never a kernel name
        self.assertIn("-p 1", c)
        self.assertIn("-l \\EFI\\o1new\\shimx64.efi", c.replace("'", ""))
        self.assertIn("F11", m.out)
        st = m.state()["efi"]
        self.assertEqual((st["new"], st["old"]), ("0002", "0001"))
        self.assertNotIn("promoted", st)                             # --finish does that

    def test_the_stale_signature_where_the_new_esp_starts_is_wiped_before_formatting(self):
        """The old models partition's ext4 superblock sits where the new ESP starts: each new partition is
        wiped, then always formatted; without the wipe the ESP would still read as ext4."""
        m = self.m
        self.assertEqual(self.rc, 0, m.out)
        log = m.log()
        for n in (1, 2, 3, 4):
            w = [i for i, l in enumerate(log) if l.startswith("wipefs -a ") and l.endswith("-part%d" % n)]
            f = [i for i, l in enumerate(log) if l.split()[0] in ("mkfs.vfat", "mkfs.ext4") and l.endswith("-part%d" % n)]
            self.assertEqual((len(w), len(f)), (1, 1), n)
            self.assertLess(w[0], f[0])
        self.assertEqual(m.fs["disks"][m.names["to"]]["parts"][0]["type"], "vfat")

    def test_models_dir_stays_immutable_on_the_old_root_and_never_on_the_new(self):
        m = self.m
        log = m.log()
        um = [i for i, l in enumerate(log) if l == "umount " + m.models_dir]
        on = [i for i, l in enumerate(log) if l == "chattr +i " + m.models_dir]
        wipe = min(i for i, l in enumerate(log) if l.startswith("wipefs"))
        self.assertEqual((len(um), len(on)), (1, 1))
        self.assertLess(um[0], on[0])
        self.assertLess(on[0], wipe)
        self.assertEqual([l for l in log if l.startswith("chattr -i")], [])       # not removed at the end of the restore
        self.assertEqual(m.fs.get("immutable", []), [m.models_dir])                # a fallback boot of the old drive stays protected
        self.assertEqual([l for l in log if l.startswith("chattr") and "/run/o1migrate" in l], [])   # the new root's folder: never

    def test_the_page_cache_is_dropped_before_the_final_compare(self):
        m = self.m
        self.assertEqual(read(m.drop_caches_file), "3\n")
        log = m.log()
        rs = [i for i, l in enumerate(log) if l.startswith("rsync")]
        syncs = [i for i, l in enumerate(log) if l == "sync"]
        self.assertTrue(any(rs[1] < i < rs[2] for i in syncs), (rs, syncs))         # between the park check and the pre-wipe check

    def test_the_summary_says_the_new_drive_boots_next(self):
        self.assertIn("boots NEXT (BootNext), once", self.m.out)
        self.assertIn("first in the boot order until --finish", self.m.out)
        self.assertNotIn("second)", self.m.out.split("DONE. The copy")[1])

    def test_the_admin_panel_and_pulls_are_stopped_too(self):
        stops = [l for l in self.m.log("systemctl") if l.startswith("systemctl stop ")]
        for u in ("ollama.service", "ollama1-gateway.service", "ollama1-admin.service", "ollama1-pull@llama.service"):
            self.assertIn("systemctl stop " + u, stops)

    def test_the_swap_file_is_made_new_not_copied(self):
        rs = [l for l in self.m.log("rsync") if "/run/o1migrate/root/" in l and "--delete" in l]
        self.assertTrue(rs)
        for l in rs[:2]:
            self.assertIn("--exclude=/swap.img", l)
            self.assertIn("--exclude=/proc/*", l)
            self.assertIn(" -aHAXx ", l)
        self.assertTrue(any(l.startswith("fallocate -l %d " % MIB) for l in self.m.log("fallocate")))

    def test_the_server_is_held_awake_for_the_whole_run(self):
        """The kit's auto sleep suspends an idle server; a copy of hours must not be suspended."""
        log = self.m.log()
        inh = [i for i, l in enumerate(log) if l.startswith("systemd-inhibit --what=sleep:handle-power-key --mode=block --who=ollama1")]
        self.assertEqual(len(inh), 1)
        self.assertLess(inh[0], min(i for i, l in enumerate(log) if l.startswith("systemctl stop")))
        self.assertLess(inh[0], min(i for i, l in enumerate(log) if l.startswith("rsync")))
        self.assertIn("holding the server awake", self.m.out)

    def test_a_finished_run_leaves_a_readable_status_and_a_private_log(self):
        m = self.m
        self.assertEqual(os.stat(m.status_file).st_mode & 0o777, 0o644)        # the run's umask was 077
        self.assertEqual(os.stat(m.log_file).st_mode & 0o777, 0o600)
        text = read(m.status_file)
        self.assertIn("state:      DONE", text)
        self.assertIn("stage:      6 of 6 (firmware boot entry (new first, old second))", text)
        self.assertIn("--finish", text.split("next step:")[1])
        for must_not in (FROM, TO, "TESTFROM", "TESTTO"):
            self.assertNotIn(must_not, text)
        for field in ("started:", "updated:", "boot id:", "doing:", "progress:"):
            self.assertIn(field, text)
        self.assertIn("serial %s" % TO, read(m.log_file))                       # the log keeps everything
        self.assertTrue(m.status_file.startswith(m.data_dir + "/"))
        self.assertTrue(m.log_file.startswith(m.data_dir + "/"))


class TestRun(Base):
    def test_each_write_to_a_drive_follows_a_fresh_serial_check(self):
        """A by-id name that points at the wrong drive (a udev glitch) right before the wipe: nothing is wiped."""
        m = self.m
        other = m.dir + "/dev/" + m.names["from"]
        base = m.dir + "/byid/nvme-Samsung_SSD_980_PRO_2TB_" + TO
        m.set_state(hook=[{"cmd": "umount", "nth": 1, "repoint": [[base, other], [base + "_1", other]]}])
        self.assertEqual(m.run("--run"), 1, m.out)
        self.assertEqual(m.log("wipefs"), [])
        self.assertEqual(m.log("sgdisk"), [])
        self.assertEqual(m.log("mkfs.ext4"), [])

    def test_check_target_refuses_a_path_that_is_not_the_drive_with_the_serial(self):
        m = self.m
        mg = L.Migrator(L.Cfg(m.env()), L.parse_args(["--from-serial", FROM, "--to-serial", TO, "--run"]))
        d = mg.check_target(TO, m.byid("to"))
        self.assertEqual(d.block, m.names["to"])
        self.assertEqual(mg.check_target(TO, m.byid("to", 3)).serial, TO)         # a partition of it
        with self.assertRaises(L.Abort):
            mg.check_target(TO, m.byid("from"))                                    # the other drive's by-id name
        with self.assertRaises(L.Abort):
            mg.check_target(TO, m.dir + "/dev/" + m.names["to"])                   # a kernel name, even the right one
        with self.assertRaises(L.Abort):
            mg.check_target(FROM, m.byid("to"))
        os.remove(m.byid("to"))
        os.symlink(m.dir + "/dev/" + m.names["from"], m.byid("to"))               # by-id re-pointed
        with self.assertRaises(L.Abort):
            mg.check_target(TO, m.byid("to"))

    def test_the_serial_is_found_whichever_nvme_name_it_has(self):
        for swap in (False, True):
            m = Machine(swap=swap)
            try:
                mg = L.Migrator(L.Cfg(m.env()), L.parse_args(["--from-serial", FROM, "--to-serial", TO]))
                a, b = mg.resolve(FROM), mg.resolve(TO)
                self.assertEqual((a.block, b.block), (m.names["from"], m.names["to"]))
                self.assertNotEqual(a.dev, b.dev)
                self.assertIsNone(mg.resolve("NOSUCHSERIAL"))
            finally:
                m.close()

    def test_sgdisk_uses_the_root_size_asked_for(self):
        m = self.m
        self.assertEqual(m.run("--run", "--root-size", "400G"), 0, m.out)
        self.assertIn("-n3:0:+400GiB", m.log("sgdisk")[-1])
        self.assertEqual(m.state()["root_gib"], 400)

    def test_a_kernel_option_that_would_be_lost_stops_the_boot_stage(self):
        m = self.m
        m.put(m.cmdline_file, OLD_CMDLINE + " some_option=1\n")       # running with something the copy's GRUB won't have
        self.assertEqual(m.run("--run", "--reboot"), 1, m.out)
        self.assertIn("lacks kernel options the running system has: some_option=1", m.out)
        self.assertNotIn("boot", m.state()["done"])
        self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], [])
        self.assertEqual(m.log("efibootmgr"), [])                       # the firmware was not touched

    def test_a_grub_dropin_missing_on_the_copy_stops_the_boot_stage(self):
        m = self.m
        m.set_state(hook=[{"cmd": "rsync", "nth": 5, "dst_remove": "etc/default/grub.d/97-amdgpu-overdrive.cfg"}])
        self.assertEqual(m.run("--run"), 1, m.out)
        self.assertIn("97-amdgpu-overdrive.cfg is not on the copy", m.out)

    def test_the_old_entry_is_found_by_its_esp_not_by_its_position(self):
        m = self.m
        m.set_state(efi={"order": ["0000", "0003", "0001"], "entries": {
            "0000": {"label": "UEFI: Built-in EFI Shell", "path": "VenMedia(5023b95c)"},
            "0003": {"label": "network", "path": "PciRoot(0x0)/Pci(0x1,0x0)"},
            "0001": {"label": "ubuntu", "path": "HD(1,GPT,%s,0x800,0x219800)/File(\\EFI\\ubuntu\\shimx64.efi)" % fu("a")}}})
        self.full_run()
        self.assertEqual(m.fs["efi"]["order"], ["0001", "0000", "0003", "0004"])
        self.assertEqual(m.fs["efi"]["next"], "0004")

    def test_the_old_drive_stays_the_default_boot_until_the_firmware_stage(self):
        m = self.m
        m.set_state(kill=[{"cmd": "efibootmgr", "nth": 2, "when": "before"}])      # the call that makes the entry
        self.assertEqual(m.run("--run"), -signal.SIGKILL, m.out)
        self.assertEqual(m.fs["efi"]["order"], ["0001", "0000"])
        self.assertEqual(len(m.fs["efi"]["entries"]), 2)
        done = m.state()["done"]
        for s in ("park", "partition", "copy", "boot", "restore"):     # everything else was ready by then
            self.assertIn(s, done)
        self.assertNotIn("firmware", done)

    def test_the_run_is_stopped_by_a_copy_that_lands_on_the_wrong_filesystem(self):
        """A mount that "succeeds" and does nothing: the copy must not go into a folder on the old root."""
        m = self.m
        m.set_state(fail=[{"cmd": "mount", "nth": 1, "rc": 0}])
        self.assertEqual(m.run("--run"), 1, m.out)
        self.assertIn("is not mounted from", m.out)
        self.assertEqual([l for l in m.log("rsync") if l.endswith("/run/o1migrate/root/")], [])

    def test_a_copy_that_fails_leaves_the_resume_hint(self):
        m = self.m
        m.set_state(fail=[{"cmd": "rsync", "nth": 4, "rc": 23}])             # the first pass over /
        self.assertEqual(m.run("--run"), 1)
        self.assertIn("STOPPED", m.out)
        self.assertIn("sudo bash migrate-os.sh --resume", m.out)
        self.assertIn("The old drive (serial %s) has not been written to" % FROM, m.out)
        self.assertIn("park", m.state()["done"])
        self.assertNotIn("copy", m.state()["done"])

    def test_the_parked_copy_is_compared_by_content_just_before_the_wipe(self):
        """Same size, one bit different: the dry run can't see it, the content compare does, before anything is erased."""
        m = self.m
        m.set_state(hook=[{"cmd": "rsync", "nth": 1, "dst_corrupt": "a.gguf"}])
        self.assertEqual(m.run("--run"), 1, m.out)
        self.assertIn("a.gguf differs from the parked copy (content)", m.out)
        for c in ("wipefs", "sgdisk", "umount", "chattr"):
            self.assertEqual(m.log(c), [], c)
        self.assertIn("park", m.state()["done"])
        self.assertNotIn("partition", m.state()["done"])

    def test_the_chroot_binds_are_undone_when_a_chroot_command_fails(self):
        m = self.m
        m.set_state(fail=[{"cmd": "chroot", "nth": 2, "rc": 1}])
        self.assertEqual(m.run("--run"), 1, m.out)
        left = [x["target"] for x in m.fs["mounts"] if x["target"].startswith(m.dir + "/run/o1migrate/root/")
                and x["target"].rsplit("/", 1)[1] in ("dev", "sys", "proc", "run")]
        self.assertEqual(left, [])

    def test_a_format_that_did_nothing_is_noticed(self):
        m = self.m
        m.set_state(fail=[{"cmd": "mkfs.vfat", "nth": 1, "rc": 0}])        # "succeeds" and writes nothing
        self.assertEqual(m.run("--run"), 1, m.out)
        self.assertIn("partition 1 reads as 'nothing' after formatting it vfat", m.out)
        self.assertNotIn("partition", m.state()["done"])

    def test_a_missing_grub_cfg_stub_stops_the_boot_stage(self):
        m = self.m
        m.set_state(no_grub_cfg=True)
        self.assertEqual(m.run("--run"), 1, m.out)
        self.assertIn("left no grub.cfg stub next to the boot loader", m.out)
        self.assertNotIn("boot", m.state()["done"])

    def test_parked_copy_that_differs_stops_before_the_wipe(self):
        self.m.set_state(hook=[{"cmd": "rsync", "nth": 1, "dst_remove": "a.gguf"}])
        self.assertEqual(self.m.run("--run"), 1, self.m.out)
        self.assertIn("the parked copy differs", self.m.out)
        for c in ("wipefs", "sgdisk", "umount"):
            self.assertEqual(self.m.log(c), [])


class TestStatusFile(Base):
    def test_a_failed_run_says_failed_and_never_reboots(self):
        for kill_cmd, nth in (("rsync", 1), ("mkfs.ext4", 2), ("rsync", 3), ("chroot", 2), ("efibootmgr", 3), ("rsync", 6)):
            m = Machine()
            try:
                m.set_state(fail=[{"cmd": kill_cmd, "nth": nth, "rc": 5}])
                rc = m.run("--run", "--reboot", "--confirm-serial", TO, tty=False)
                self.assertEqual(rc, 1, "%s#%d\n%s" % (kill_cmd, nth, m.out))
                self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], [], kill_cmd)
                text = read(m.status_file)
                self.assertIn("state:      FAILED", text)
                self.assertIn("nothing was rebooted", text)
                self.assertIn("--resume --reboot", text)
                self.assertNotIn(TO, text)
                self.assertNotIn(FROM, text)
                self.assertNotIn(m.dir, text)                          # no path, no command's words
                self.assertRegex(text, r"state:      FAILED in stage \w+: (\S+ stopped with exit 5|a safety check stopped it)")
                self.assertIn("The old drive", m.out)
            finally:
                m.close()

    def test_a_refusal_after_the_lock_is_in_the_status_too(self):
        m = Machine(root_gib=400)
        try:
            self.assertEqual(m.run("--run"), 1)
            self.assertIn("state:      FAILED: refused to start", read(m.status_file))
        finally:
            m.close()

    def test_a_wrong_serial_is_in_the_status(self):
        self.assertEqual(self.m.run("--run", answers=["nope"]), 1)
        self.assertIn("state:      FAILED: stopped before the first stage", read(self.m.status_file))

    def test_the_stage_and_the_percentage_while_it_runs(self):
        """The copy's progress lines (rsync --info=progress2) reach the status file as they arrive."""
        m = self.m
        # (the stand-in rsync prints nothing; feed the same line through Status the way Runner does)
        cfg = L.Cfg(m.env())
        st = L.Status(cfg)
        st.enable()
        st.stage("copy")
        st.feed("  1,073,741,824  42%   95.12MB/s    0:05:22\r")
        t = read(m.status_file)
        self.assertIn("stage:      3 of 6", t)
        self.assertIn("progress:   42% of the current copy", t)


class TestReboot(Base):
    def rebooted(self, m=None):
        return [l for l in (m or self.m).log("systemctl") if l == "systemctl reboot"]

    def test_reboot_flag_reboots_after_everything_passed_and_asks_nothing(self):
        m = self.m
        self.assertEqual(m.run("--run", "--reboot", "--confirm-serial", TO, tty=False, O1M_COUNTDOWN_SECS="1"), 0, m.out)
        self.assertEqual(len(self.rebooted()), 1)
        self.assertNotIn("Reboot into the new drive now?", m.out)
        self.assertIn("Rebooting into the new drive in 1 seconds. Press Ctrl-C to cancel.", m.out)
        # the reboot is the very last thing: after the firmware order was set and checked
        cmds = m.fs["log"]
        self.assertLess(max(i for i, l in enumerate(cmds) if l.startswith("efibootmgr -o")), cmds.index("systemctl reboot"))
        self.assertEqual(cmds[-1], "systemctl reboot")
        self.assertIn("state:      DONE", read(m.status_file))
        self.assertIn("reboots into the new drive by itself", read(m.status_file))

    def test_the_prompt_default_is_no(self):
        for ans in ("", "n", "N", "no", "nope", "maybe", " "):
            m = Machine()
            try:
                self.assertEqual(m.run("--run", answers=[TO, ans]), 0, m.out)
                self.assertIn("Reboot into the new drive now? [y/N]", m.out)
                self.assertEqual(self.rebooted(m), [], repr(ans))
            finally:
                m.close()

    def test_the_prompt_with_the_end_of_input_is_no(self):
        self.assertEqual(self.m.run("--run", answers=[TO]), 0, self.m.out)
        self.assertEqual(self.rebooted(), [])

    def test_yes_reboots_after_the_countdown(self):
        for ans in ("y", "Y", "yes"):
            m = Machine()
            try:
                self.assertEqual(m.run("--run", answers=[TO, ans], O1M_COUNTDOWN_SECS="1"), 0, m.out)
                self.assertEqual(len(self.rebooted(m)), 1, ans)
                self.assertIn("Rebooting into the new drive in 1 seconds", m.out)
            finally:
                m.close()

    def test_confirm_serial_never_prompts_even_if_someone_could_answer(self):
        m = self.m
        self.assertEqual(m.run("--run", "--confirm-serial", TO, answers=["y", "y", "y"]), 0, m.out)
        self.assertNotIn("Reboot into the new drive now?", m.out)
        self.assertEqual(self.rebooted(), [])

    def test_ctrl_c_in_the_countdown_cancels_the_reboot(self):
        m = self.m
        p = m.popen("--run", "--reboot", "--confirm-serial", TO, O1M_COUNTDOWN_SECS="60")
        out = []
        try:
            end = time.time() + 120
            for line in p.stdout:
                out.append(line)
                if "Press Ctrl-C to cancel" in line:
                    break
                self.assertLess(time.time(), end)
            time.sleep(0.5)
            p.send_signal(signal.SIGINT)
            rest, _ = p.communicate(timeout=60)
            out.append(rest)
        finally:
            if p.poll() is None:
                p.kill()
        text = "".join(out)
        self.assertEqual(p.returncode, 0, text)
        self.assertIn("Reboot cancelled", text)
        self.assertEqual(self.rebooted(), [])
        m.reload()
        t = read(m.status_file)
        self.assertIn("state:      DONE", t)
        self.assertIn("the reboot was cancelled", t)

    def test_the_countdown_is_real_time(self):
        m = self.m
        t0 = time.time()
        self.assertEqual(m.run("--run", "--reboot", "--confirm-serial", TO, tty=False, O1M_COUNTDOWN_SECS="3"), 0, m.out)
        self.assertGreaterEqual(m.out.count("  3 ..."), 1)
        self.assertGreaterEqual(m.out.count("  1 ..."), 1)
        self.assertLess(m.out.index("  3 ..."), m.out.index("  1 ..."))

    def test_resume_with_reboot_after_the_prompt_was_declined(self):
        m = self.m
        self.assertEqual(m.run("--run", answers=[TO, "n"]), 0, m.out)
        self.assertEqual(self.rebooted(), [])
        self.assertEqual(m.run("--resume", "--reboot", "--confirm-serial", TO, tty=False, serials=False), 0, m.out)
        self.assertEqual(len(self.rebooted()), 1)

    def test_reboot_flag_is_only_for_run_and_resume(self):
        self.assertEqual(self.m.run("--plan", "--reboot"), 2)
        self.assertEqual(self.m.run("--finish", "--reboot"), 2)


class TestTmux(Base):
    """The run starts itself again in a detached tmux session, once, after the confirmation."""

    def test_the_relaunch_command(self):
        m = self.m
        self.assertEqual(m.run("--run", "--reboot", "--confirm-serial", TO, tty=False, tmux=True), 0, m.out)
        tm = m.log("tmux")
        self.assertEqual(tm[0], "tmux has-session -t migrate")
        new = [l for l in tm if l.startswith("tmux new-session")]
        self.assertEqual(len(new), 1)
        lead = "tmux new-session -d -s migrate -c / "                  # detached, named, no terminal needed
        self.assertTrue(new[0].startswith(lead), new[0])
        child = shlex.split(new[0][len(lead):])                         # the one command the session runs
        self.assertEqual(child[1:4], ["-I", "-u", os.path.realpath(LIBPY)])
        rest = child[4:]
        self.assertEqual(rest, ["--from-serial", FROM, "--to-serial", TO, "--root-size", "300G", "--bwlimit", "200000",
                                "--resume", "--confirm-serial", TO, "--in-session", "--reboot"])
        # the launcher itself changed nothing: no stage ran, no service stopped, no drive written
        calls = {l.split()[0] for l in m.log()}
        self.assertEqual(calls & {"sgdisk", "wipefs", "mkfs.ext4", "rsync", "mount", "umount", "chroot", "efibootmgr"}, set())
        self.assertEqual([l for l in m.log("systemctl") if " stop " in l], [])
        self.assertEqual(m.state()["done"], {})
        self.assertIn("STARTED in the background (tmux session migrate)", m.out)
        self.assertIn("sudo tmux attach -t migrate", m.out)
        self.assertIn("cat " + m.status_file, m.out)
        self.assertIn("state:      RUNNING", read(m.status_file))
        self.assertIn("starting", read(m.status_file))

    def test_without_reboot_the_child_has_no_reboot_flag(self):
        m = self.m
        self.assertEqual(m.run("--run", "--confirm-serial", TO, tty=False, tmux=True), 0, m.out)
        new = [l for l in m.log("tmux") if l.startswith("tmux new-session")][0]
        self.assertNotIn("--reboot", new)

    def test_the_typed_serial_is_asked_in_the_terminal_before_detaching(self):
        m = self.m
        self.assertEqual(m.run("--run", answers=["wrong"], tmux=True), 1)
        self.assertEqual(m.log("tmux"), [])                                # not detached; nothing started
        self.assertEqual(m.run("--run", answers=[TO], tmux=True), 0, m.out)
        self.assertIn("serial>", m.out)
        self.assertEqual(len([l for l in m.log("tmux") if "new-session" in l]), 1)

    def test_the_session_runs_everything_by_itself_without_a_terminal(self):
        m = self.m
        m.set_state(tmux_run=True)
        rc = m.run("--run", "--reboot", "--confirm-serial", TO, tty=False, tmux=True, O1M_COUNTDOWN_SECS="1")
        self.assertEqual(rc, 0, m.out)
        m.reload()
        self.assertEqual(m.fs["tmux_child"]["rc"], 0, m.fs["tmux_child"]["out"])
        for s in L.STAGES:
            self.assertIn(s, m.state()["done"])
        self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], ["systemctl reboot"])
        t = read(m.status_file)
        self.assertIn("state:      DONE", t)
        self.assertNotIn("serial>", m.fs["tmux_child"]["out"])
        self.assertNotIn("Reboot into the new drive now?", m.fs["tmux_child"]["out"])
        self.assertIn("in the background", m.out)

    def test_a_failure_in_the_session_is_in_the_status_and_does_not_reboot(self):
        m = self.m
        m.set_state(tmux_run=True, fail=[{"cmd": "mkfs.ext4", "nth": 2, "rc": 1}])
        rc = m.run("--run", "--reboot", "--confirm-serial", TO, tty=False, tmux=True, O1M_COUNTDOWN_SECS="1")
        self.assertEqual(rc, 0)                         # the launcher's own job was done
        m.reload()
        self.assertEqual(m.fs["tmux_child"]["rc"], 1)
        self.assertIn("state:      FAILED in stage partition: mkfs.ext4 stopped with exit 1", read(m.status_file))
        self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], [])
        self.assertNotIn("partition", m.state()["done"])

    def test_a_tmux_that_fails_to_start_leaves_failed_in_the_status(self):
        m = self.m
        m.set_state(fail=[{"cmd": "tmux", "nth": 2, "rc": 1}])          # has-session is the first call, new-session the second
        self.assertEqual(m.run("--run", "--confirm-serial", TO, tty=False, tmux=True), 1)
        self.assertIn("state:      FAILED: could not start the tmux session", read(m.status_file))
        self.assertEqual({l.split()[0] for l in m.log()} & {"sgdisk", "wipefs", "rsync"}, set())

    def test_an_existing_session_is_not_clobbered(self):
        m = self.m
        m.set_state(tmux_sessions=["migrate"])
        self.assertEqual(m.run("--run", "--confirm-serial", TO, tty=False, tmux=True), 1)
        self.assertIn("a tmux session named migrate exists already", m.out)
        self.assertEqual([l for l in m.log("tmux") if "new-session" in l], [])

    def test_inside_tmux_it_runs_in_place(self):
        m = self.m
        self.assertEqual(m.run("--run", "--confirm-serial", TO, tty=False, tmux=True, TMUX="/tmp/x,1,0"), 0, m.out)
        self.assertEqual(m.log("tmux"), [])
        self.assertIn("firmware", m.state()["done"])

    def test_without_tmux_installed_it_runs_in_place_and_says_so(self):
        m = self.m
        os.remove(m.dir + "/bin/tmux")
        self.assertEqual(m.run("--run", "--confirm-serial", TO, tty=False, tmux=True,
                               PATH=m.dir + "/bin:" + os.path.dirname(shutil.which("python3") or "/usr/bin")), 0, m.out)
        self.assertIn("tmux is not installed", m.out)
        self.assertIn("firmware", m.state()["done"])

    def test_two_runs_at_once_are_refused(self):
        m = self.m
        import fcntl
        os.makedirs(m.data_dir + "/o1migrate", exist_ok=True)
        with open(m.data_dir + "/o1migrate/lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(m.run("--run", "--confirm-serial", TO, tty=False), 1)
            self.assertIn("another migrate-os is running", m.out)
        self.assertFalse(os.path.exists(m.status_file))          # and it did not overwrite the running one's status


class TestFinish(Base):
    def test_finish_before_the_reboot_refuses(self):
        self.full_run()
        self.assertEqual(self.m.run("--finish", serials=False), 1)
        self.assertIn("not running from the new drive yet", self.m.out)

    def test_finish_needs_a_finished_migration(self):
        self.assertEqual(self.m.run("--finish"), 1)
        self.assertIn("did not reach its end", self.m.out)
        self.assertEqual(self.m.run("--finish", serials=False), 1)
        self.assertIn("there is no migration", self.m.out)

    def test_finish_needs_no_serials_and_verifies_root_is_on_the_to_drive(self):
        self.full_run()
        self.m.reboot_into_new()
        self.assertEqual(self.m.run("--finish", serials=False), 0, self.m.out)
        self.assertIn("all on the new drive", self.m.out)
        self.assertIn("FINISHED", self.m.out)
        self.assertIn("never written to", self.m.out)
        self.assertIn("kernel command line has the options", self.m.out)
        self.assertIn("finished", self.m.state())
        self.assertTrue(os.path.isdir(self.m.data_dir + "/models-parked"))
        self.assertEqual(self.m.log("rm"), [])
        self.assertEqual([l for l in self.m.log("efibootmgr") if " -A " in l], [])
        self.assertIn("sudo blkdiscard " + self.m.byid("from")[:-2], self.m.out)
        self.assertIn("--finish --delete-parked", self.m.out)

    def test_finish_makes_the_new_drive_the_default_boot(self):
        m = self.m
        self.full_run()
        self.assertEqual(m.fs["efi"]["order"], ["0001", "0000", "0002"])
        m.reboot_into_new()
        self.assertEqual(m.run("--finish", serials=False), 0, m.out)
        self.assertEqual(m.fs["efi"]["order"], ["0002", "0001", "0000"])              # new first, old second
        self.assertIn("efibootmgr -o 0002,0001,0000", m.log("efibootmgr"))
        self.assertIn("Firmware boot order is now: 0002,0001,0000", m.out)
        self.assertIn("promoted", m.state()["efi"])
        self.assertIn("panic=10", m.out)
        # and again: nothing more to do
        n = len(m.log("efibootmgr"))
        self.assertEqual(m.run("--finish", serials=False), 0, m.out)
        self.assertEqual([l for l in m.log("efibootmgr")[n:] if l.startswith("efibootmgr -o")], [])

    def test_finish_takes_the_immutable_mark_off_the_old_root_only_when_it_can_reach_it(self):
        m = self.m
        self.full_run()
        m.reboot_into_new()
        self.assertEqual(m.run("--finish", serials=False), 0, m.out)               # the old root is not mounted: the mark stays
        self.assertEqual(m.fs["immutable"], [m.models_dir])
        self.assertIn("stays immutable", m.out)
        self.assertEqual([l for l in m.log("chattr") if l.startswith("chattr -i")], [])
        old = m.dir + "/oldroot"                                                    # the old root, mounted for some reason
        os.makedirs(old + m.models_dir)
        m.reload()
        m.fs["mounts"].append({"target": old, "source": "/dev/mapper/ubuntu--vg-ubuntu--lv", "fstype": "ext4", "options": "rw"})
        m.fs["immutable"] = [old + m.models_dir]
        m.flush()
        self.assertEqual(m.run("--finish", serials=False), 0, m.out)
        self.assertEqual(m.fs["immutable"], [])
        self.assertIn("chattr -i " + old + m.models_dir, m.log("chattr"))

    def test_finish_stops_if_the_new_entry_is_gone(self):
        m = self.m
        self.full_run()
        m.reboot_into_new()
        m.reload()
        del m.fs["efi"]["entries"]["0002"]
        m.fs["efi"]["order"] = ["0001", "0000"]
        m.flush()
        self.assertEqual(m.run("--finish", serials=False), 1)
        self.assertIn("firmware entry 0002 is gone", m.out)
        self.assertNotIn("finished", m.state())

    def test_finish_with_the_serials_swapped_is_refused(self):
        self.full_run()
        self.m.reboot_into_new()
        self.assertEqual(self.m.run("--from-serial", TO, "--to-serial", FROM, "--finish", serials=False), 1)
        self.assertIn("don't match", self.m.out)

    def test_finish_checks_the_kernel_options_of_the_new_boot(self):
        self.full_run()
        self.m.reboot_into_new()
        self.m.put(self.m.cmdline_file, "BOOT_IMAGE=/vmlinuz-6 root=UUID=x ro quiet splash\n")      # booted without them
        self.assertEqual(self.m.run("--finish", serials=False), 1)
        self.assertIn("booted without these kernel options", self.m.out)
        for t in NVME_OPTS + [OVERDRIVE]:
            self.assertIn(t, self.m.out)
        self.assertNotIn("finished", self.m.state())
        self.m.put(self.m.cmdline_file, NEW_CMDLINE + "\n")
        self.assertEqual(self.m.run("--finish", serials=False), 0, self.m.out)

    def test_the_sudo_permission_is_reminded_and_can_be_removed(self):
        m = self.m
        self.full_run()
        m.reboot_into_new()
        m.put(m.sudoers, "alice ALL=(root) NOPASSWD: /usr/local/lib/ollama1-migrate/migrate-os.sh\n")
        self.assertEqual(m.run("--finish", serials=False, answers=["n"]), 0, m.out)
        self.assertIn("REMINDER: remove the temporary sudo permission now:  sudo rm " + m.sudoers, m.out)
        self.assertTrue(os.path.exists(m.sudoers))
        self.assertEqual(m.run("--finish", serials=False, answers=["y"]), 0, m.out)         # offered, and taken
        self.assertIn("REMOVED the temporary sudo permission", m.out)
        self.assertFalse(os.path.exists(m.sudoers))
        m.put(m.sudoers, "x\n")
        self.assertEqual(m.run("--finish", "--remove-sudoers", serials=False, tty=False), 0, m.out)
        self.assertFalse(os.path.exists(m.sudoers))
        m.put(m.sudoers, "x\n")
        self.assertEqual(m.run("--finish", serials=False, tty=False), 0, m.out)             # no terminal: a reminder only
        self.assertTrue(os.path.exists(m.sudoers))
        self.assertIn("REMINDER", m.out)

    def test_delete_parked_needs_the_typed_word_and_a_live_copy(self):
        m = self.m
        self.full_run()
        m.reboot_into_new()
        shutil.rmtree(m.models_dir)
        os.makedirs(m.models_dir)
        self.assertEqual(m.run("--finish", "--delete-parked", answers=["delete"]), 1)      # the live copy is empty
        self.assertIn("not in", m.out)
        self.assertTrue(os.path.isdir(m.data_dir + "/models-parked"))
        shutil.copytree(m.data_dir + "/models-parked", m.models_dir, dirs_exist_ok=True)
        self.assertEqual(m.run("--finish", "--delete-parked", answers=["yes", "n"]), 1)    # a whole copy, the wrong word
        self.assertIn("the parked copy was kept", m.out)
        self.assertTrue(os.path.isdir(m.data_dir + "/models-parked"))
        self.assertEqual(m.run("--finish", "--delete-parked", answers=["delete", "n"]), 0, m.out)
        self.assertEqual(m.log("rm"), ["rm -rf --one-file-system -- " + m.data_dir + "/models-parked"])
        self.assertFalse(os.path.exists(m.data_dir + "/models-parked"))

    def test_delete_parked_refuses_another_folder(self):
        self.full_run()
        self.m.reboot_into_new()
        other = self.m.dir + "/precious"
        os.makedirs(other)
        self.assertEqual(self.m.run("--finish", "--delete-parked", answers=["delete"], O1M_PARKED=other), 1)
        self.assertIn("not the parked-models folder", self.m.out)
        self.assertEqual(self.m.log("rm"), [])
        self.assertTrue(os.path.isdir(other))

    def test_disable_old_entry_is_inactive_not_deleted_and_asks(self):
        m = self.m
        self.full_run()
        m.reboot_into_new()
        self.assertEqual(m.run("--finish", "--disable-old-entry", answers=["no"]), 1)
        self.assertEqual([l for l in m.log("efibootmgr") if " -A " in l], [])
        self.assertEqual(m.run("--finish", "--disable-old-entry", answers=["yes", "n"]), 0, m.out)
        self.assertEqual([l for l in m.log("efibootmgr") if " -A " in l], ["efibootmgr -A -b 0001"])
        self.assertFalse(m.fs["efi"]["entries"]["0001"]["active"])
        self.assertIn("0001", m.fs["efi"]["entries"])
        self.assertEqual([l for l in m.log("efibootmgr") if " -B " in l], [])

    def test_finish_only_flags_need_finish(self):
        self.assertEqual(self.m.run("--run", "--delete-parked"), 2)
        self.assertEqual(self.m.run("--run", "--remove-sudoers"), 2)


if __name__ == "__main__":
    unittest.main()
