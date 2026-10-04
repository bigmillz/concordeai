"""tools/remove-raid.sh (6b400): the teardown of the old RAID1 mirror, against a fake machine (tests/fakeraid.py
stands in for lsblk, findmnt, mdadm, umount, systemctl, fuser, update-initramfs; the files are in a temp folder
passed as O1_RAID_TESTROOT). Nothing here touches a real disk."""
import glob
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import o1test_util as U
from fakecmd import fu

SCRIPT = os.path.join(os.environ.get("OLLAMA1_TEST_TOOLS") or os.path.join(U.KIT, "tools"), "remove-raid.sh")
STUBS = ["lsblk", "findmnt", "mdadm", "umount", "systemctl", "fuser", "update-initramfs"]
UUID = "aaaa1111:bbbb2222:cccc3333:dddd4444"
U1, U2, U3, U9 = fu("1"), fu("2"), fu("3"), fu("9")
FSTAB = """# /etc/fstab: static file system information.
# the mirror lives at /srv/data (this comment must stay)
/dev/disk/by-id/dm-uuid-LVM-abcdef / ext4 defaults 0 1
UUID=%(U1)s /srv/models ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2
UUID=%(U2)s /srv/data ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2
UUID=%(U3)s /srv/data2 ext4 defaults 0 2
/swap.img\tnone\tswap\tsw\t0\t0
""" % {"U1": U1, "U2": U2, "U3": U3}
MDCONF = """# mdadm.conf
HOMEHOST <system>
MAILADDR root
ARRAY /dev/md/o1data metadata=1.2 name=ollama1:data UUID=%s
""" % UUID
CRON = """# cron job for the mdadm package
PATH=/usr/sbin:/usr/bin:/sbin:/bin
57 0 * * 0 root if [ -x /usr/share/mdadm/checkarray ] && [ $(date +\\%d) -le 7 ]; then /usr/share/mdadm/checkarray --cron --all --idle --quiet; fi
"""


class Machine:
    def __init__(self, members=("/dev/sda1", "/dev/sdb1"), extra_disks=True):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="o1raid-"))
        d = self.dir
        self.bin = d + "/bin"
        os.makedirs(self.bin)
        fake = os.path.join(U.HERE, "fakeraid.py")
        for t in STUBS:
            os.symlink(fake, os.path.join(self.bin, t))
        for sub in ("etc/mdadm", "etc/ollama1", "etc/cron.d", "srv/data/backups/ollama1/2026-01-01", "srv/data/models-parked/m",
                    "srv/data/o1migrate", "var/log", "var/backups"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        self.put("etc/fstab", FSTAB)
        self.put("etc/mdadm/mdadm.conf", MDCONF)
        self.put("etc/cron.d/mdadm", CRON)
        self.put("etc/ollama1/setup.env", "OS_SERIAL=SERIAL-OS\nMODELS_SERIAL=SERIAL-MODELS\nHDD1_SERIAL=SERIAL-HDD1\nHDD2_SERIAL=SERIAL-HDD2\n")
        self.put("srv/data/backups/ollama1/2026-01-01/settings.tar.gz", "x" * 2048)
        self.put("srv/data/models-parked/m/blob", "m" * 4096)
        self.put("srv/data/o1migrate/state.json", json.dumps({"finished": "2026-01-02"}))
        self.put("srv/data/migrate-os.status", "DONE\n")
        self.tty = d + "/tty"
        self.put("tty", "yes\n")
        self.state_path = d + "/fake.json"
        self.fs = {
            "disks": {"/dev/sda": {"serial": "SERIAL-HDD1"}, "/dev/sdb": {"serial": "SERIAL-HDD2"},
                      "/dev/sdc": {"serial": "SERIAL-OTHER"},
                      "/dev/nvme0n1": {"serial": "SERIAL-OS"}, "/dev/nvme1n1": {"serial": "SERIAL-MODELS"}},
            "parts": {"/dev/sda1": "/dev/sda", "/dev/sdb1": "/dev/sdb", "/dev/sdc1": "/dev/sdc",
                      "/dev/nvme0n1p3": "/dev/nvme0n1", "/dev/nvme1n1p1": "/dev/nvme1n1"},
            "mount": {"/srv/data": {"source": "/dev/md127", "fstype": "ext4", "options": "rw,noatime"}},
            "md": {"/dev/md127": {"uuid": UUID, "name": "ollama1:data", "members": list(members), "running": True}},
            "superblock": {m: True for m in members},
            "log": [],
        }
        self.flush()

    def put(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.dir, rel)) as f:
            return f.read()

    def flush(self):
        with open(self.state_path, "w") as f:
            json.dump(self.fs, f)

    def reload(self):
        with open(self.state_path) as f:
            self.fs = json.load(f)
        return self.fs

    def log(self, tool=None):
        L = self.reload()["log"]
        return [l for l in L if tool is None or l.split()[0] == tool]

    def mutations(self):
        return [l for l in self.log() if l.startswith(("umount", "mdadm --stop", "mdadm --zero", "update-initramfs"))
                or l.startswith("systemctl stop") or l.startswith("systemctl disable")]

    def run(self, *args, tty="yes\n", with_tty=True):
        if with_tty:
            self.put("tty", tty)
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], FAKE_STATE=self.state_path,
                   O1_RAID_TESTROOT=self.dir, O1_RAID_TTY=self.tty if with_tty else self.dir + "/no-tty")
        self.flush()
        r = subprocess.run(["bash", SCRIPT] + list(args), env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.out = r.stdout + r.stderr
        self.reload()
        return r.returncode

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Case(unittest.TestCase):
    def machine(self, **kw):
        self.m = Machine(**kw)
        self.addCleanup(self.m.close)
        return self.m


class TestDryRun(Case):
    def test_it_prints_what_is_on_the_array_and_every_step_and_changes_nothing(self):
        m = self.machine()
        before = {p: m.read(p) for p in ("etc/fstab", "etc/mdadm/mdadm.conf", "etc/cron.d/mdadm")}
        self.assertEqual(m.run(), 0, m.out)
        out = m.out
        for want in ("/dev/md127", "SERIAL-HDD1", "SERIAL-HDD2", "/dev/sda1", "/dev/sdb1",
                     "backups", "models-parked", "o1migrate", "migrate-os.status",           # the top level of the array, with sizes
                     "/srv/data ext4", "UUID=" + U2 + " /srv/data ext4",   # the mount and the fstab line
                     "ARRAY /dev/md/o1data", "name=ollama1:data",                            # the mdadm.conf line
                     "1. stop:", "2. copy", "3. umount /srv/data", "4. mdadm --stop /dev/md127",
                     "5. mdadm --zero-superblock /dev/sda1", "5. mdadm --zero-superblock /dev/sdb1",
                     "6. /etc/fstab: comment out", "7. /etc/mdadm/mdadm.conf: remove", "8. turn off the array check",
                     "9. update-initramfs -u", "This was a dry run: nothing was changed", "--yes-erase-the-mirror",
                     "ollama1-remove-raid.log"):
            self.assertIn(want, out)
        self.assertEqual(m.mutations(), [])
        self.assertEqual({p: m.read(p) for p in before}, before)
        self.assertFalse(os.path.exists(m.dir + "/var/log/ollama1-remove-raid.log"))
        self.assertEqual([c for c in m.log() if c.split()[0] in ("umount", "update-initramfs", "systemctl")], [])
        self.assertTrue(m.reload()["md"]["/dev/md127"]["running"])

    def test_a_dry_run_needs_no_terminal(self):
        m = self.machine()
        self.assertEqual(m.run(with_tty=False), 0, m.out)
        self.assertIn("dry run", m.out)

    def test_the_serials_come_from_setup_env_or_from_the_options(self):
        m = self.machine()
        os.unlink(m.dir + "/etc/ollama1/setup.env")
        self.assertEqual(m.run(), 1)
        self.assertIn("serials are not known", m.out)
        self.assertEqual(m.run("--hdd1-serial", "SERIAL-HDD1", "--hdd2-serial", "SERIAL-HDD2"), 0, m.out)
        self.assertEqual(m.run("--hdd1-serial", "ab", "--hdd2-serial", "SERIAL-HDD2"), 1)

    def test_no_array_means_nothing_to_remove(self):
        m = self.machine()
        m.fs["mount"] = {}
        m.fs["md"] = {}
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        self.assertIn("Nothing to remove", m.out)
        self.assertEqual(m.mutations(), [])

    def test_the_help_and_unknown_options(self):
        m = self.machine()
        self.assertEqual(m.run("--help"), 0)
        self.assertIn("--yes-erase-the-mirror", m.out)
        self.assertEqual(m.run("--bogus"), 2)

    def test_the_script_parses(self):
        self.assertEqual(subprocess.run(["bash", "-n", SCRIPT]).returncode, 0)


class TestRefusals(Case):
    def test_without_the_flag_nothing_is_ever_changed_whatever_else_is_given(self):
        m = self.machine()
        self.assertEqual(m.run("--keep-going", "--hdd1-serial", "SERIAL-HDD1", "--hdd2-serial", "SERIAL-HDD2"), 0, m.out)
        self.assertEqual(m.mutations(), [])

    def test_it_will_not_run_without_a_terminal(self):
        m = self.machine()
        self.assertEqual(m.run("--yes-erase-the-mirror", with_tty=False), 1)
        self.assertIn("no terminal to ask on", m.out)
        self.assertEqual(m.mutations(), [])
        self.assertFalse(os.path.exists(m.dir + "/var/log/ollama1-remove-raid.log"))

    def test_anything_but_a_typed_yes_stops_it(self):
        for answer in ("no\n", "\n", "y\n", "YES\n", "yes please\n", ""):
            m = self.machine()
            self.assertEqual(m.run("--yes-erase-the-mirror", tty=answer), 1, repr(answer))
            self.assertEqual(m.mutations(), [], repr(answer))
            self.assertTrue(m.reload()["md"]["/dev/md127"]["running"])
            self.assertIn("/srv/data", m.read("etc/fstab"))

    def test_the_questions_are_read_from_the_terminal_not_from_stdin(self):
        m = self.machine()
        m.put("tty", "no\n")
        env = dict(os.environ, PATH=m.bin + ":" + os.environ["PATH"], FAKE_STATE=m.state_path, O1_RAID_TESTROOT=m.dir,
                   O1_RAID_TTY=m.tty)
        r = subprocess.run(["bash", SCRIPT, "--yes-erase-the-mirror"], env=env, input="yes\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(m.mutations(), [])

    def test_the_two_serials_must_be_known_different_and_not_the_system_disks(self):
        m = self.machine()
        self.assertEqual(m.run("--hdd1-serial", "SERIAL-NONE", "--hdd2-serial", "SERIAL-HDD2"), 1)
        self.assertIn("no disk has the serial SERIAL-NONE", m.out)
        self.assertEqual(m.run("--hdd1-serial", "SERIAL-HDD1", "--hdd2-serial", "SERIAL-HDD1"), 1)
        self.assertIn("the same", m.out)
        for ser in ("SERIAL-OS", "SERIAL-MODELS"):
            self.assertEqual(m.run("--yes-erase-the-mirror", "--hdd1-serial", ser, "--hdd2-serial", "SERIAL-HDD2"), 1)
            self.assertIn("not a mirror disk", m.out)
        self.assertEqual(m.mutations(), [])

    def test_members_that_are_not_exactly_the_two_disks_are_refused(self):
        for members in (("/dev/sda1", "/dev/sdc1"),               # one member on a third disk
                        ("/dev/sda1",),                           # only one of the two disks is a member
                        ("/dev/sda1", "/dev/sdb1", "/dev/sdc1")):
            m = self.machine(members=members)
            self.assertEqual(m.run("--yes-erase-the-mirror"), 1, members)
            self.assertIn("refusing", m.out)
            self.assertEqual(m.mutations(), [], members)
            self.assertEqual(m.log("mdadm"), [l for l in m.log("mdadm") if "--zero" not in l and "--stop" not in l])

    def test_a_member_on_an_nvme_drive_is_refused_and_never_touched(self):
        m = self.machine(members=("/dev/sda1", "/dev/nvme0n1p3"))
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("NVMe", m.out)
        self.assertEqual(m.mutations(), [])
        # a member NAMED like an NVMe partition although it sits on a mirror disk: the name alone refuses it
        m = self.machine(members=("/dev/nvme0n1p3", "/dev/sdb1"))
        m.fs["parts"]["/dev/nvme0n1p3"] = "/dev/sda"
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("is an NVMe device", m.out)
        self.assertEqual(m.mutations(), [])
        # and the serial of an NVMe given as a mirror disk
        m = self.machine()
        m.fs["disks"]["/dev/nvme0n1"]["serial"] = "SERIAL-HDD1"      # the "mirror disk" is an NVMe drive
        del m.fs["disks"]["/dev/sda"]
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("NVMe", m.out)
        self.assertEqual(m.mutations(), [])

    def test_an_array_whose_mount_is_not_an_md_device_is_refused(self):
        m = self.machine()
        m.fs["mount"]["/srv/data"]["source"] = "/dev/sdc1"
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("not an md array", m.out)
        self.assertEqual(m.mutations(), [])

    def test_unknown_content_is_refused_and_named_unless_keep_going(self):
        m = self.machine()
        os.makedirs(m.dir + "/srv/data/photos")
        m.put("srv/data/notes.txt", "keep me\n")
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("WOULD REFUSE", m.out)
        for name in ("photos", "notes.txt"):
            self.assertIn(name, m.out)
        self.assertIn("NOT something this kit put here", m.out)
        self.assertEqual(m.mutations(), [])
        self.assertEqual(m.run(), 1)                              # the dry run says so too
        self.assertEqual(m.run("--keep-going"), 0, m.out)         # and a dry run with --keep-going still changes nothing
        self.assertEqual(m.mutations(), [])
        self.assertEqual(m.run("--yes-erase-the-mirror", "--keep-going"), 0, m.out)
        self.assertIn("Going on anyway", m.out)
        self.assertIn("photos", m.out)
        self.assertEqual(len([c for c in m.log("mdadm") if "--zero" in c]), 2)

    def test_the_allow_list_is_what_the_kit_wrote_there(self):
        m = self.machine()
        for name in ("lost+found", "old-drive-boot-files-undo.txt", "migrate-os.log"):
            os.makedirs(m.dir + "/srv/data/" + name, exist_ok=True) if name == "lost+found" else m.put("srv/data/" + name, "x")
        self.assertEqual(m.run(), 0, m.out)
        self.assertNotIn("NOT something", m.out)

    def test_a_system_move_that_is_not_finished_is_refused(self):
        m = self.machine()
        m.put("srv/data/o1migrate/state.json", json.dumps({"done": {"park": "x"}}))
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("migrate-os", m.out)
        self.assertEqual(m.mutations(), [])
        self.assertEqual(m.run("--yes-erase-the-mirror", "--keep-going"), 0, m.out)

    def test_something_holding_the_mount_stops_it_before_anything_changes(self):
        m = self.machine()
        m.fs["fuser_busy"] = True
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("still has files open", m.out)
        self.assertEqual([c for c in m.mutations() if not c.startswith("systemctl stop")], [])
        self.assertTrue(m.reload()["md"]["/dev/md127"]["running"])

    def test_a_failed_unmount_stops_everything_after_it(self):
        m = self.machine()
        m.fs["umount_fail"] = True
        before = m.read("etc/fstab")
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("could not unmount", m.out)
        self.assertEqual([c for c in m.log() if c.startswith(("mdadm --stop", "mdadm --zero"))], [])
        self.assertEqual(m.read("etc/fstab"), before)

    def test_a_failed_stop_leaves_fstab_and_mdadm_conf_alone(self):
        m = self.machine()
        m.fs["stop_fails"] = True
        before = (m.read("etc/fstab"), m.read("etc/mdadm/mdadm.conf"))
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("mdadm --stop", m.out)
        self.assertEqual((m.read("etc/fstab"), m.read("etc/mdadm/mdadm.conf")), before)
        self.assertEqual([c for c in m.log() if c.startswith("mdadm --zero")], [])


class TestTheTeardown(Case):
    def go(self, **kw):
        m = self.machine(**kw)
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        return m

    def test_the_commands_run_in_this_order_and_only_on_the_two_disks(self):
        m = self.go()
        seq = [c for c in m.log() if c.startswith(("systemctl", "umount", "mdadm --stop", "mdadm --zero", "update-initramfs", "fuser"))]
        self.assertEqual(seq, ["systemctl stop ollama1-backup.timer", "systemctl stop ollama1-backup.service",
                               "fuser -m /srv/data", "umount /srv/data", "mdadm --stop /dev/md127",
                               "mdadm --zero-superblock /dev/sda1", "mdadm --zero-superblock /dev/sdb1",
                               "systemctl disable --now mdcheck_start.timer", "systemctl disable --now mdcheck_continue.timer",
                               "systemctl daemon-reload", "update-initramfs -u"])
        self.assertFalse([c for c in m.log() if "nvme" in c], [c for c in m.log() if "nvme" in c])
        self.assertEqual([c for c in m.log() if c.split()[0] in ("wipefs", "sgdisk", "mkfs.ext4", "dd")], [])
        st = m.reload()
        self.assertFalse(st["md"]["/dev/md127"]["running"])
        self.assertEqual(st["superblock"], {"/dev/sda1": False, "/dev/sdb1": False})
        self.assertEqual(st["mount"], {})
        self.assertEqual(set(st["parts"]), {"/dev/sda1", "/dev/sdb1", "/dev/sdc1", "/dev/nvme0n1p3", "/dev/nvme1n1p1"})   # no partition removed

    def test_the_serial_is_read_again_before_each_superblock_is_zeroed(self):
        m = self.go()
        log = m.log()
        zeros = [i for i, c in enumerate(log) if c.startswith("mdadm --zero")]
        for i in zeros:
            prior = [c for c in log[:i] if c.startswith("lsblk -dno SERIAL")]
            self.assertTrue(prior)
        self.assertGreater(len([c for c in log[log.index("mdadm --stop /dev/md127"):] if c.startswith("lsblk -dno SERIAL")]), 1)

    def test_fstab_changes_in_exactly_one_line_and_a_copy_is_kept(self):
        m = self.go()
        old, new = FSTAB.split("\n"), m.read("etc/fstab").split("\n")
        self.assertEqual(len(new), len(old))
        changed = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(len(changed), 1)
        a, b = changed[0]
        self.assertTrue(a.startswith("UUID=") and " /srv/data " in a)
        self.assertTrue(b.startswith("# ollama1 "))
        self.assertTrue(b.endswith(": " + a), b)
        self.assertIn("the mirror was removed", b)
        self.assertEqual([l for l in new if not l.startswith("#") and "/srv/data" in l.split()[1:2]], [])
        for keep in ("# the mirror lives at /srv/data (this comment must stay)", "/srv/data2", "/srv/models", "/swap.img\tnone\tswap\tsw\t0\t0"):
            self.assertTrue(any(keep in l for l in new), keep)
        copies = glob.glob(m.dir + "/etc/fstab.before-remove-raid-*")
        self.assertEqual(len(copies), 1)
        self.assertEqual(open(copies[0]).read(), FSTAB)
        self.assertTrue(m.read("etc/fstab").endswith("\n"))

    def test_fstab_without_a_trailing_newline_or_with_two_lines_for_the_mount(self):
        m = self.machine()
        extra = "UUID=" + U9 + " /srv/data ext4 defaults 0 2"
        m.put("etc/fstab", FSTAB + extra + "\n")
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        new = m.read("etc/fstab")
        self.assertEqual([l for l in new.splitlines() if not l.startswith("#") and len(l.split()) > 1 and l.split()[1] == "/srv/data"], [])
        self.assertEqual(new.count("the mirror was removed"), 2)

    def test_mdadm_conf_loses_only_this_arrays_line_and_a_copy_is_kept(self):
        m = self.machine()
        m.put("etc/mdadm/mdadm.conf", MDCONF + "ARRAY /dev/md0 metadata=1.2 name=other:scratch UUID=11111111:22222222:33333333:44444444\n")
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        new = m.read("etc/mdadm/mdadm.conf")
        self.assertEqual(new, MDCONF.replace("ARRAY /dev/md/o1data metadata=1.2 name=ollama1:data UUID=%s\n" % UUID, "")
                         + "ARRAY /dev/md0 metadata=1.2 name=other:scratch UUID=11111111:22222222:33333333:44444444\n")
        copies = glob.glob(m.dir + "/etc/mdadm/mdadm.conf.before-remove-raid-*")
        self.assertEqual(len(copies), 1)
        # another array is left: the check stays on
        self.assertEqual([c for c in m.log() if c.startswith("systemctl disable")], [])
        self.assertEqual(m.read("etc/cron.d/mdadm"), CRON)

    def test_the_monthly_check_is_turned_off_timers_and_cron(self):
        m = self.go()
        self.assertEqual([c for c in m.log() if c.startswith("systemctl disable")],
                         ["systemctl disable --now mdcheck_start.timer", "systemctl disable --now mdcheck_continue.timer"])
        cron = m.read("etc/cron.d/mdadm").splitlines()
        self.assertEqual(cron[0], "# cron job for the mdadm package")
        self.assertEqual(cron[1], "PATH=/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertTrue(cron[2].startswith("# ollama1: the array check is off"))
        self.assertIn("checkarray", cron[2])
        self.assertEqual(len(glob.glob(m.dir + "/etc/cron.d/mdadm.before-remove-raid-*")), 1)
        self.assertNotIn("ARRAY", m.read("etc/mdadm/mdadm.conf"))

    def test_the_settings_backups_are_copied_to_the_new_place_before_the_unmount(self):
        m = self.go()
        self.assertEqual(m.read("var/backups/ollama1/2026-01-01/settings.tar.gz"), "x" * 2048)
        self.assertEqual(oct(os.stat(m.dir + "/var/backups/ollama1").st_mode & 0o777), "0o700")
        self.assertIn("settings backups copied", m.out)

    def test_a_big_backup_folder_is_not_copied(self):
        m = self.machine()
        os.environ["O1_RAID_BACKUP_MAX_KB"] = "1"
        self.addCleanup(os.environ.pop, "O1_RAID_BACKUP_MAX_KB", None)
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        self.assertFalse(os.path.exists(m.dir + "/var/backups/ollama1/2026-01-01"))
        self.assertIn("not copied", m.out)

    def test_a_serial_that_changes_before_the_zeroing_stops_it(self):
        m = self.machine()
        m.fs["serial_changes_after_stop"] = True
        self.assertEqual(m.run("--yes-erase-the-mirror"), 1)
        self.assertIn("serial check failed for /dev/sda1", m.out)
        self.assertEqual([c for c in m.log() if c.startswith("mdadm --zero")], [])

    def test_the_log_is_written_and_says_what_is_true_afterwards(self):
        m = self.go()
        log = m.read("var/log/ollama1-remove-raid.log")
        for want in ("remove-raid", "/dev/md127", "superblock zeroed: /dev/sda1", "superblock zeroed: /dev/sdb1",
                     "ok: /srv/data is not mounted", "ok: /dev/md127 is gone", "ok: /dev/sda1 has no RAID superblock",
                     "fstab has no active /srv/data line", "keep their partition tables"):
            self.assertIn(want, log)
        self.assertNotIn("NOT OK", log)
        self.assertEqual(oct(os.stat(m.dir + "/var/log/ollama1-remove-raid.log").st_mode & 0o777), "0o600")
        self.assertIn("wipefs -a", m.out)                          # the hint is printed, never run
        self.assertEqual([c for c in m.log() if c.startswith("wipefs")], [])

    def test_running_it_again_finds_nothing_to_remove(self):
        m = self.go()
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        self.assertIn("Nothing to remove", m.out)

    def test_the_array_is_found_even_when_it_is_not_mounted(self):
        m = self.machine()
        m.fs["mount"] = {}
        self.assertEqual(m.run("--yes-erase-the-mirror"), 0, m.out)
        self.assertEqual(len([c for c in m.log() if c.startswith("mdadm --zero")]), 2)
        self.assertEqual([c for c in m.log() if c.startswith("umount")], [])


if __name__ == "__main__":
    unittest.main()
