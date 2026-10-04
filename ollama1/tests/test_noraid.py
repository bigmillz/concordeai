"""6b400: the kit has no RAID. What replaced /srv/data: the nightly settings backup on the root filesystem (with a
free-space rule and a size limit), one constant for each folder, the storage rows that no longer show a mirror that
is not there, and nothing anywhere that says a RAID is missing."""
import datetime
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1common
import o1stats
import o1dashui
import dash_sample
from o1common import Paths

REPO = os.path.dirname(U.KIT)


def load_helper():
    loader = importlib.machinery.SourceFileLoader("o1helper_under_test", os.path.join(U.BIN, "ollama1-helper"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


Usage = namedtuple("Usage", "total used free")


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.h = load_helper()
        shutil.rmtree(Paths.backups, ignore_errors=True)
        self.addCleanup(shutil.rmtree, Paths.backups, True)
        self.rec = []
        self.h.record = lambda *a: self.rec.append(a)
        self.day = datetime.date.today().isoformat()

    def test_it_lands_on_the_root_filesystem_not_on_srv_data(self):
        self.assertEqual(self.h.backup(), 0)
        self.assertEqual(self.rec[-1][:2], ("backup", "ok"))
        dest = os.path.join(Paths.backups, self.day)
        self.assertTrue(os.path.isfile(dest + "/settings.tar.gz"))
        self.assertTrue(os.path.isfile(dest + "/models.json"))
        self.assertEqual(self.rec[-1][2], dest)
        self.assertEqual(Paths.backups[len(o1common.PREFIX):], "/var/backups/ollama1")
        self.assertEqual(os.stat(Paths.backups).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o700)

    def test_it_needs_no_mount(self):
        src = re.sub(r"(?m)^\s*#.*$", "", open(os.path.join(U.BIN, "ollama1-helper")).read())
        self.assertNotIn("/srv/data", src)
        self.assertNotIn("ismount", src)
        self.assertNotIn("Paths.data", src)

    def test_it_is_skipped_with_a_message_when_the_root_filesystem_is_nearly_full(self):
        with mock.patch.object(self.h.shutil, "disk_usage", return_value=Usage(100 << 30, 99 << 30, 1 << 30)):
            self.assertEqual(self.h.backup(), 0)
        what, result, detail = self.rec[-1]
        self.assertEqual((what, result), ("backup", "skipped"))
        self.assertIn("1.0 GiB free", detail)
        self.assertIn("needs 2.0 GiB", detail)
        self.assertFalse(os.path.exists(os.path.join(Paths.backups, self.day)))

    def test_it_runs_with_exactly_the_free_space_asked_for(self):
        with mock.patch.object(self.h.shutil, "disk_usage", return_value=Usage(100 << 30, 0, o1common.BACKUP_MIN_FREE_BYTES)):
            self.assertEqual(self.h.backup(), 0)
        self.assertEqual(self.rec[-1][1], "ok")

    def test_a_backup_over_the_limit_is_skipped_with_a_message_and_leaves_nothing(self):
        self.h.BACKUP_MAX_BYTES = 10
        self.assertEqual(self.h.backup(), 0)
        what, result, detail = self.rec[-1]
        self.assertEqual((what, result), ("backup", "skipped"))
        self.assertIn("over the", detail)
        self.assertNotIn(self.day, os.listdir(Paths.backups))                  # no folder, no half-made .tmp
        self.assertEqual(os.listdir(Paths.backups), [])

    def test_the_limit_is_what_o1common_says(self):
        self.assertEqual(o1common.BACKUP_MAX_BYTES, 256 << 20)
        self.assertEqual(o1common.BACKUP_MIN_FREE_BYTES, 2 << 30)
        self.assertEqual(self.h.BACKUP_MAX_BYTES, o1common.BACKUP_MAX_BYTES)

    def test_thirty_days_are_kept(self):
        os.makedirs(Paths.backups, mode=0o700)
        for n in range(1, 36):
            os.makedirs(os.path.join(Paths.backups, (datetime.date.today() - datetime.timedelta(days=n + 1)).isoformat()))
        self.assertEqual(self.h.backup(), 0)
        days = sorted(d for d in os.listdir(Paths.backups) if re.match(r"^\d{4}-\d{2}-\d{2}$", d))
        self.assertEqual(len(days), o1common.BACKUP_KEEP_DAYS)
        self.assertIn(self.day, days)

    def test_the_unit_does_not_wait_for_a_mount_that_is_not_there(self):
        t = open(os.path.join(U.SYSTEMD, "ollama1-backup.service")).read()
        self.assertNotIn("RequiresMountsFor", t)
        self.assertNotIn("/srv/data", t)
        self.assertIn("/var/backups/ollama1", t)


class TestOneConstantEach(unittest.TestCase):
    def test_no_program_of_the_kit_names_srv_data_except_to_say_it_is_gone_or_to_tear_it_down(self):
        allowed = {"tools/remove-raid.sh", "lib/o1stats.py", "lib/o1migrate.py"}
        for top in ("bin", "lib", "systemd", "config", "tools"):
            for name in sorted(os.listdir(os.path.join(U.KIT, top))):
                rel = top + "/" + name
                path = os.path.join(U.KIT, rel)
                if not os.path.isfile(path):
                    continue
                try:
                    text = open(path, encoding="utf-8").read()
                except UnicodeDecodeError:
                    continue
                code = re.sub(r"(?m)^\s*#.*$", "", text) if not name.endswith(".md") else text
                if "/srv/data" in code:
                    self.assertIn(rel, allowed, rel)

    def test_the_state_folder_is_the_same_everywhere(self):
        from migrate_fixture import load_lib
        self.assertEqual(load_lib().DATA_DIR, Paths.state[len(o1common.PREFIX):])

    def test_migrate_and_backup_folders_sit_on_the_root_filesystem(self):
        self.assertEqual(Paths.backups[len(o1common.PREFIX):], "/var/backups/ollama1")
        self.assertTrue(Paths.state[len(o1common.PREFIX):].startswith("/var/lib/"))


class TestStorageRows(unittest.TestCase):
    def setUp(self):
        self.root = o1common.PREFIX
        self.fstab = self.root + "/etc/fstab"
        os.makedirs(os.path.dirname(self.fstab), exist_ok=True)
        self.addCleanup(lambda: os.path.exists(self.fstab) and os.unlink(self.fstab))
        self.addCleanup(shutil.rmtree, self.root + "/srv/data", True)

    def rows(self):
        return [d["mount"] for d in o1stats.disks()]

    def test_a_server_without_srv_data_shows_two_rows_and_nothing_missing(self):
        shutil.rmtree(self.root + "/srv/data", ignore_errors=True)
        with open(self.fstab, "w") as f:
            f.write("/dev/x / ext4 defaults 0 1\n")
        self.assertEqual(self.rows(), ["/", "/srv/models"])
        self.assertTrue(all(d["mounted"] for d in o1stats.disks()))

    def test_a_commented_out_fstab_line_is_not_a_mirror(self):
        with open(self.fstab, "w") as f:
            f.write("# ollama1 2026-10-04: the mirror was removed (tools/remove-raid.sh): UUID=x /srv/data ext4 defaults 0 2\n")
        self.assertEqual(self.rows(), ["/", "/srv/models"])

    def test_a_server_that_still_has_one_shows_it_and_a_dropped_mount_shows_missing(self):
        with open(self.fstab, "w") as f:
            f.write("UUID=x /srv/data ext4 defaults,nofail 0 2\n")
        self.assertEqual(self.rows(), ["/", "/srv/models", "/srv/data"])
        os.makedirs(self.root + "/srv/data", exist_ok=True)
        self.assertEqual(self.rows(), ["/", "/srv/models", "/srv/data"])

    def test_in_fstab_reads_only_active_lines(self):
        p = self.fstab
        with open(p, "w") as f:
            f.write("# UUID=a /srv/data ext4 x 0 2\nUUID=b /srv/data2 ext4 x 0 2\n   \nUUID=c /srv/data ext4 x 0 2\n")
        self.assertTrue(o1stats.in_fstab("/srv/data", p))
        self.assertTrue(o1stats.in_fstab("/srv/data2", p))
        with open(p, "w") as f:
            f.write("# UUID=a /srv/data ext4 x 0 2\n")
        self.assertFalse(o1stats.in_fstab("/srv/data", p))
        self.assertFalse(o1stats.in_fstab("/srv/data", p + ".none"))


class TestNothingSaysRaidIsMissing(unittest.TestCase):
    def test_no_mdstat_no_arrays(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(o1stats, "PROC", d):
            self.assertEqual(o1stats.raid(), [])
            with open(d + "/mdstat", "w") as f:
                f.write("Personalities :\nunused devices: <none>\n")
            self.assertEqual(o1stats.raid(), [])

    def test_the_console_dashboard_without_an_array_or_a_data_row_has_no_raid_word_and_no_missing_mount(self):
        st = dash_sample.sample()
        st["raid"] = []
        st["disks"] = [d for d in st["disks"] if d.get("mount") != "/srv/data"]
        for w, h in ((100, 30), (120, 40), (80, 24)):
            text = o1dashui.render(st, w, h, "blocks", 300, 0).text()
            self.assertNotIn("RAID", text)
            self.assertNotIn("/srv/data", text)
            self.assertNotIn("not mounted", text)

    def test_the_admin_page_has_no_no_array_warning(self):
        admin = open(os.path.join(U.BIN, "ollama1-admin")).read()
        self.assertNotIn("no array", admin)
        self.assertNotIn("RAID missing", admin)
        self.assertIn("(s.raid||[]).forEach", admin)                    # an array that is there is still listed

    def test_no_source_says_a_raid_is_missing(self):
        for top in ("bin", "lib", "tools"):
            for name in sorted(os.listdir(os.path.join(U.KIT, top))):
                path = os.path.join(U.KIT, top, name)
                if not os.path.isfile(path):
                    continue
                try:
                    text = open(path, encoding="utf-8").read()
                except UnicodeDecodeError:
                    continue
                for bad in ("RAID missing", "RAID is missing", "no RAID array", "'RAID','no array'", "RAID not found"):
                    self.assertNotIn(bad, text, top + "/" + name)


class TestDocs(unittest.TestCase):
    def test_the_readme_and_the_guide_do_not_promise_a_mirror(self):
        readme = open(os.path.join(U.KIT, "README.md"), encoding="utf-8").read()
        guide = open(os.path.join(REPO, "docs", "your-own-server.md"), encoding="utf-8").read()
        for text, name in ((readme, "README"), (guide, "guide")):
            self.assertNotIn("two mirror disks", text, name)
            self.assertNotIn("RAID1 mirror, ext4", text, name)
        self.assertIn("tools/remove-raid.sh", readme)
        self.assertIn("--yes-erase-the-mirror", readme)
        self.assertIn("/var/backups/ollama1", readme)
        self.assertIn("--park-dir", readme)

    def test_the_removal_tool_is_in_the_tools_and_executable(self):
        p = os.path.join(U.TOOLS, "remove-raid.sh")
        self.assertTrue(os.path.isfile(p))
        self.assertTrue(os.access(p, os.X_OK))


if __name__ == "__main__":
    unittest.main()
