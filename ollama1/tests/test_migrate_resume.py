"""migrate-os, crash and resume: the tool is killed (SIGKILL: a power cut) at
the boundaries of every stage, then run again with --resume, and must end
where an uninterrupted run ends, without redoing a finished stage and without
ever writing to the old drive. No real disk is touched."""
import re
import signal
import unittest

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))   # so `python3 -m unittest tests.test_migrate` works from ollama1/

from migrate_fixture import FROM, FSTAB, MDSTAT_DEGRADED, TO, WRITES_TO_DRIVE, Machine, load_lib

L = load_lib()


def read(path):
    with open(path) as fh:
        return fh.read()


def outcome(m):
    return {"parts": [(p["n"], p["type"], p["label"], p["size"]) for p in m.fs["disks"][m.names["to"]]["parts"]],
            "fstab": re.sub(r"UUID=\S+", "UUID=x", read(m.dir + "/run/o1migrate/root/etc/fstab")),
            "order": m.fs["efi"]["order"], "next": m.fs["efi"]["next"], "immutable": [x.replace(m.dir, "") for x in m.fs.get("immutable", [])],
            "done": sorted(m.state()["done"]),
            "models": read(m.dir + "/run/o1migrate/models/a.gguf"),
            "required": m.state()["cmdline_required"]}


class TestResume(unittest.TestCase):
    def setUp(self):
        self.m = Machine()

    def tearDown(self):
        self.m.close()

    # (program, nth call, when): the first destructive thing of each stage, the last of some, and the points between
    POINTS = [("rsync", 1, "before"), ("rsync", 1, "after"), ("rsync", 2, "after"), ("umount", 1, "after"),
              ("wipefs", 1, "before"), ("sgdisk", 2, "after"), ("mkfs.vfat", 1, "after"), ("mkfs.ext4", 3, "after"),
              ("mount", 1, "before"), ("rsync", 3, "after"), ("rsync", 4, "after"), ("rsync", 6, "after"), ("fallocate", 1, "after"),
              ("chroot", 1, "after"), ("chroot", 3, "after"), ("rsync", 7, "after"), ("efibootmgr", 2, "after"),
              ("efibootmgr", 4, "after"), ("efibootmgr", 5, "after"), ("chattr", 1, "after"), ("wipefs", 3, "after")]

    def test_killed_at_every_stage_boundary_then_resumed(self):
        base = Machine()
        try:
            self.assertEqual(base.run("--run", "--confirm-serial", TO, tty=False), 0, base.out)
            want = outcome(base)
        finally:
            base.close()
        for cmd, nth, when in self.POINTS:
            m = Machine()
            try:
                m.set_state(kill=[{"cmd": cmd, "nth": nth, "when": when}])
                rc = m.run("--run")
                self.assertEqual(rc, -signal.SIGKILL, "%s#%d %s: the tool was not killed (rc %s)\n%s" % (cmd, nth, when, rc, m.out))
                self.assertIn("state:      RUNNING", read(m.status_file))                 # the status keeps the stage it was in
                m.set_state(kill=[])
                if nth % 2:                       # half the points: a power cycle took the mounts too
                    m.power_cycle()
                rc = m.run("--resume", serials=False)                                    # the serials are in the state file
                self.assertEqual(rc, 0, "%s#%d %s\n%s" % (cmd, nth, when, m.out))
                self.assertEqual(outcome(m), want, "%s#%d %s" % (cmd, nth, when))
                self.assertEqual(len([e for e in m.fs["efi"]["entries"].values() if e["label"] == "ollama1-new"]), 1)
                self.assertEqual(read(m.dir + "/src/etc/fstab"), FSTAB % m.uu)           # the old drive was never written to
                for l in m.log():
                    if l.split()[0] in WRITES_TO_DRIVE:
                        self.assertIn("_" + TO, l, l)
                        self.assertNotIn("_" + FROM, l)
                self.assertIn("state:      DONE", read(m.status_file))
            finally:
                m.close()

    def test_a_finished_stage_is_not_done_again(self):
        m = self.m
        m.set_state(kill=[{"cmd": "mount", "nth": 1, "when": "before"}])       # the copy stage's first mount
        self.assertEqual(m.run("--run"), -signal.SIGKILL, m.out)
        self.assertIn("partition", m.state()["done"])
        self.assertNotIn("copy", m.state()["done"])
        wipes, rsyncs = len(m.log("wipefs")), len(m.log("rsync"))
        m.set_state(kill=[])
        self.assertEqual(m.run("--resume"), 0, m.out)
        self.assertEqual(len(m.log("wipefs")), wipes)          # not wiped a second time
        self.assertEqual(len(m.log("sgdisk")), 2)              # zap + create, once
        self.assertEqual(len(m.log("mkfs.ext4")), 3)
        self.assertEqual(len(m.log("mkfs.vfat")), 1)
        self.assertIn("stage park: done earlier, skipped", m.out)
        self.assertIn("stage partition: done earlier, skipped", m.out)
        later = m.log("rsync")[rsyncs:]                         # parking is not repeated
        self.assertTrue(all("models-parked" not in l or l.endswith("/run/o1migrate/models/") for l in later))

    def test_a_crash_inside_the_partition_stage_redoes_it_whole(self):
        m = self.m
        m.set_state(kill=[{"cmd": "mkfs.ext4", "nth": 2, "when": "before"}])
        self.assertEqual(m.run("--run"), -signal.SIGKILL, m.out)
        self.assertNotIn("partition", m.state()["done"])
        m.set_state(kill=[])
        self.assertEqual(m.run("--resume"), 0, m.out)
        disk_wipes = [l for l in m.log("wipefs") if "-part" not in l]
        self.assertEqual(len(disk_wipes), 2)                   # wiped again; the parked copy was checked first
        self.assertEqual(m.run("--finish"), 1)                 # (not rebooted yet)

    def test_a_raid_that_degrades_before_the_wipe_stops_it(self):
        m = self.m
        m.set_state(kill=[{"cmd": "rsync", "nth": 2, "when": "after"}])        # parked and checked, not yet wiped
        self.assertEqual(m.run("--run"), -signal.SIGKILL, m.out)
        m.set_state(kill=[])
        m.put(m.mdstat_file, MDSTAT_DEGRADED)
        self.assertEqual(m.run("--resume"), 1, m.out)                          # the preflight refuses
        self.assertIn("is degraded [U_]", m.out)
        self.assertEqual(m.log("wipefs"), [])
        self.assertEqual(m.log("umount"), [])

    def test_the_raid_is_looked_at_again_inside_the_partition_stage(self):
        import os
        from unittest import mock
        m = self.m
        mg = L.Migrator(L.Cfg(m.env()), L.parse_args(["--from-serial", FROM, "--to-serial", TO, "--run"]))
        os.makedirs(m.data_dir + "/models-parked")
        mg.state = {"done": {"park": "x"}, "started": {}}
        m.put(m.mdstat_file, MDSTAT_DEGRADED)
        with mock.patch.dict(os.environ, m.env()):
            with self.assertRaises(L.Abort) as cm:
                mg.stage_partition()
        self.assertIn("degraded", str(cm.exception))
        self.assertEqual(m.log("wipefs"), [])

    def test_bootnext_used_up_by_a_boot_is_set_again_on_resume(self):
        """A power cut, or a first boot of the new drive that fell back to the old one, uses BootNext up."""
        m = self.m
        self.assertEqual(m.run("--run"), 0, m.out)
        m.reload()
        m.fs["efi"]["next"] = None
        m.flush()
        n = len([l for l in m.log("efibootmgr") if l == "efibootmgr -n 0002"])
        self.assertEqual(m.run("--resume", "--reboot", O1M_COUNTDOWN_SECS="0"), 0, m.out)
        self.assertEqual(m.fs["efi"]["next"], "0002")
        self.assertEqual(len([l for l in m.log("efibootmgr") if l == "efibootmgr -n 0002"]), n + 1)
        self.assertIn("BootNext was used up", m.out)
        self.assertEqual(m.fs["efi"]["order"], ["0001", "0000", "0002"])
        self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], ["systemctl reboot"])
        # still armed: nothing more to do
        n = len(m.log("efibootmgr"))
        self.assertEqual(m.run("--resume"), 0, m.out)
        self.assertEqual([l for l in m.log("efibootmgr")[n:] if l.startswith("efibootmgr -n")], [])

    def test_wrong_firmware_entries_are_still_refused_on_resume(self):
        for what in ("new gone", "old gone", "old not first", "new first"):
            m = Machine()
            try:
                self.assertEqual(m.run("--run"), 0, m.out)
                m.reload()
                e = m.fs["efi"]
                e["next"] = None
                if what == "new gone":
                    del e["entries"]["0002"]
                    e["order"] = ["0001", "0000"]
                elif what == "old gone":
                    del e["entries"]["0001"]
                    e["order"] = ["0000", "0002"]
                elif what == "old not first":
                    e["order"] = ["0000", "0001", "0002"]
                else:
                    e["order"] = ["0002", "0001", "0000"]
                m.flush()
                self.assertEqual(m.run("--resume", "--reboot"), 1, what + m.out)
                self.assertEqual([l for l in m.log("efibootmgr") if l.startswith("efibootmgr -n")][1:], [], what)
                self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], [], what)
            finally:
                m.close()

    def test_resume_errors(self):
        m = self.m
        self.assertEqual(m.run("--resume"), 1)
        self.assertIn("there is no migration to resume", m.out)
        self.assertEqual(m.run("--resume", serials=False), 1)
        self.assertIn("there is no migration", m.out)
        m.set_state(fail=[{"cmd": "mkfs.ext4", "nth": 1, "rc": 1}])
        self.assertEqual(m.run("--run"), 1)
        m.set_state(fail=[])
        got = m.run("--from-serial", TO, "--to-serial", FROM, "--resume", serials=False)
        self.assertEqual(got, 1)
        self.assertIn("don't match", m.out)
        got = m.run("--resume", "--root-size", "500G")
        self.assertEqual(got, 1)
        self.assertIn("differs from the 300G this migration began with", m.out)
        self.assertEqual(m.run("--resume", "--from-serial", FROM, serials=False), 1)
        self.assertIn("give both --from-serial and --to-serial, or neither", m.out)

    def test_run_refuses_when_a_migration_is_under_way(self):
        m = self.m
        self.assertEqual(m.run("--run"), 0, m.out)
        self.assertEqual(m.run("--run"), 1)
        self.assertIn("already under way", m.out)

    def test_resume_after_the_new_system_runs_says_finish(self):
        m = self.m
        self.assertEqual(m.run("--run"), 0, m.out)
        m.reboot_into_new()
        self.assertEqual(m.run("--resume"), 1)
        self.assertIn("Run --finish instead", m.out)

    def test_a_dead_from_drive_mid_run_says_power_cycle_then_resume(self):
        m = self.m
        m.set_state(kill=[{"cmd": "rsync", "nth": 4, "when": "before"}])      # the first pass over /
        self.assertEqual(m.run("--run"), -signal.SIGKILL)
        m.set_state(kill=[])
        m.put(m.dir + "/sys/class/nvme/" + m.ctrl["from"] + "/state", "dead\n")
        self.assertEqual(m.run("--resume"), 1)
        self.assertIn("the FROM drive is dead right now", m.out)
        self.assertIn("--resume", m.out)
        self.assertEqual(len(m.log("rsync")), 4)               # nothing was copied after it died
        self.assertIn("state:      FAILED", read(m.status_file))
        self.assertEqual([l for l in m.log("systemctl") if l == "systemctl reboot"], [])


if __name__ == "__main__":
    unittest.main()
