"""migrate-os (6b373): reading `efibootmgr -v` (a tab or spaces after the label, entries with and without the
`*`, trailing data after the loader, the same entry listed twice), the firmware stage reusing the entry a
stopped run already made instead of failing, and the optional, off-by-default step that puts the old
drive's boot files aside. The GUIDs are built at run time (the repo scan sees no UUID-shaped literal)."""
import json
import os
import shutil
import unittest

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))   # so `python3 -m unittest tests.test_migrate_efi` works from ollama1/

import o1test_util as U  # noqa: F401
from fakecmd import fu
from migrate_fixture import FROM, TO, Machine, load_lib

L = load_lib()
OLD, NEW, SHELL = fu("a"), fu("d"), fu("5")


def sample(tab="\t"):
    """What efibootmgr -v prints on a server booting from the old drive (ubuntu, the fallback \\EFI\\BOOT
    entry on the same ESP, the shell) after the new drive's entry was made. The Boot0003 line has the data
    efibootmgr appends after the loader."""
    return "\n".join([
        "BootCurrent: 0000",
        "Timeout: 1 seconds",
        "BootOrder: 0000,0001,0002,0003",
        "Boot0000* ubuntu" + tab + "HD(1,GPT,%s,0x800,0x219800)/File(\\EFI\\ubuntu\\shimx64.efi)" % OLD,
        "Boot0001* UEFI OS" + tab + "HD(1,GPT,%s,0x800,0x219800)/File(\\EFI\\BOOT\\BOOTX64.EFI)0000424f" % OLD,
        "Boot0002* UEFI: Built-in EFI Shell" + tab + "VenMedia(%s)..BO" % SHELL,
        "Boot0003* ollama1-new" + tab + "HD(1,GPT,%s,0x800,0x200000)/File(\\EFI\\o1new\\shimx64.efi)" % NEW,
        ""])


class TestParse(unittest.TestCase):
    def test_a_tab_after_the_label_and_labels_with_spaces(self):
        efi = L.parse_efi(sample())
        self.assertEqual(efi["order"], ["0000", "0001", "0002", "0003"])
        self.assertEqual({n: e["label"] for n, e in efi["entries"].items()},
                         {"0000": "ubuntu", "0001": "UEFI OS", "0002": "UEFI: Built-in EFI Shell", "0003": "ollama1-new"})
        self.assertEqual(efi["entries"]["0003"]["partuuid"], NEW)
        self.assertEqual(efi["entries"]["0003"]["loader"], "\\EFI\\o1new\\shimx64.efi")
        self.assertEqual(efi["entries"]["0002"]["partuuid"], "")       # not a disk path

    def test_spaces_instead_of_the_tab(self):
        for tab in (" ", "  ", "   "):
            efi = L.parse_efi(sample(tab))
            self.assertEqual(efi["entries"]["0003"]["label"], "ollama1-new", repr(tab))
            self.assertEqual(efi["entries"]["0003"]["partuuid"], NEW, repr(tab))
            self.assertEqual(efi["entries"]["0001"]["label"], "UEFI OS", repr(tab))
            self.assertEqual(efi["entries"]["0002"]["label"], "UEFI: Built-in EFI Shell", repr(tab))
            self.assertEqual(L.new_entry_number(efi, NEW, "\\EFI\\o1new\\shimx64.efi"), "0003")

    def test_entries_with_and_without_the_star(self):
        text = sample().replace("Boot0003*", "Boot0003 ").replace("Boot0001*", "Boot0001")
        efi = L.parse_efi(text)
        self.assertFalse(efi["entries"]["0003"]["active"])
        self.assertFalse(efi["entries"]["0001"]["active"])
        self.assertTrue(efi["entries"]["0000"]["active"])
        self.assertEqual(efi["entries"]["0003"]["label"], "ollama1-new")
        self.assertEqual(efi["entries"]["0001"]["label"], "UEFI OS")

    def test_trailing_data_after_the_loader(self):
        efi = L.parse_efi(sample())
        self.assertEqual(efi["entries"]["0001"]["loader"], "\\EFI\\BOOT\\BOOTX64.EFI")        # File(...)0000424f
        bare = ("Boot0003* UEFI OS\tHD(1,GPT,%s,0x800,0x219800)/\\EFI\\BOOT\\BOOTX64.EFI0000424f\n" % OLD)
        self.assertEqual(L.parse_efi(bare)["entries"]["0003"]["loader"], "\\EFI\\BOOT\\BOOTX64.EFI")
        slashes = "Boot0004* x\tHD(1,GPT,%s,0x800,0x2)/File(/EFI/o1new/shimx64.efi)\n" % NEW
        self.assertEqual(L.parse_efi(slashes)["entries"]["0004"]["loader"], "\\EFI\\o1new\\shimx64.efi")
        self.assertEqual(L.norm_loader("\\EFI\\BOOT\\BOOTX64.EFI0000424f"), "\\EFI\\BOOT\\BOOTX64.EFI")
        self.assertEqual(L.norm_loader("\\EFI\\ubuntu\\shimx64.efi"), "\\EFI\\ubuntu\\shimx64.efi")
        self.assertTrue(L.same_loader("\\efi\\o1new\\SHIMX64.EFI", "\\EFI\\o1new\\shimx64.efi0000424f"))
        self.assertFalse(L.same_loader("\\EFI\\o1new\\shimx64.efi", "\\EFI\\ubuntu\\shimx64.efi"))
        self.assertFalse(L.same_loader("", ""))

    def test_the_label_is_not_cut_at_a_device_word(self):
        text = "Boot0007* PCI HD Sandisk\tHD(1,GPT,%s,0x800,0x2)/File(\\EFI\\x.efi)\nBoot0008* HD Rescue  HD(1,GPT,%s,0x800,0x2)/File(\\EFI\\y.efi)\n" % (NEW, NEW)
        efi = L.parse_efi(text)
        self.assertEqual(efi["entries"]["0007"]["label"], "PCI HD Sandisk")
        self.assertEqual(efi["entries"]["0008"]["label"], "HD Rescue")
        self.assertEqual(efi["entries"]["0008"]["partuuid"], NEW)

    def test_windows_line_endings_and_blank_lines(self):
        efi = L.parse_efi(sample().replace("\n", "\r\n") + "\r\n\r\n")
        self.assertEqual(efi["entries"]["0003"]["loader"], "\\EFI\\o1new\\shimx64.efi")
        self.assertEqual(efi["order"], ["0000", "0001", "0002", "0003"])

    def test_boot_next_and_nothing(self):
        self.assertEqual(L.parse_efi("BootNext: 0003\nBootOrder: 0000\n")["next"], "0003")
        e = L.parse_efi("")
        self.assertEqual((e["order"], e["entries"], e["next"]), ([], {}, None))
        self.assertEqual(L.parse_efi(None)["entries"], {})


class TestDuplicates(unittest.TestCase):
    def dup(self):
        """ubuntu and the new entry each listed twice, under different numbers."""
        return (sample() + "Boot0004* ubuntu\tHD(1,GPT,%s,0x800,0x219800)/File(\\EFI\\ubuntu\\shimx64.efi)\n"
                "Boot0005* ollama1-new\tHD(1,GPT,%s,0x800,0x200000)/File(\\EFI\\o1new\\shimx64.efi)\n" % (OLD, NEW))

    def test_the_old_drives_entry_with_several_on_its_esp(self):
        efi = L.parse_efi(self.dup())
        # 0000 ubuntu, 0001 the fallback loader, 0004 a second ubuntu: the distro's own, earliest in the order
        self.assertEqual(L.find_old_entry(efi, OLD), "0000")
        efi["order"] = ["0004", "0001", "0000"]
        self.assertEqual(L.find_old_entry(efi, OLD), "0004")
        only_fallback = L.parse_efi("BootOrder: 0001\nBoot0001* UEFI OS\tHD(1,GPT,%s,0x800,0x2)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n" % OLD)
        self.assertEqual(L.find_old_entry(only_fallback, OLD), "0001")
        self.assertIsNone(L.find_old_entry(efi, fu("9")))
        self.assertIsNone(L.find_old_entry(efi, ""))

    def test_the_new_entry_is_reused_not_made_again(self):
        efi = L.parse_efi(self.dup())
        self.assertEqual(L.new_entry_number(efi, NEW, "\\EFI\\o1new\\shimx64.efi"), "0003")      # the lowest number
        self.assertEqual(L.entries_for(efi, NEW, loader="\\EFI\\o1new\\shimx64.efi"), ["0003", "0005"])
        self.assertIsNone(L.new_entry_number(efi, fu("9"), "\\EFI\\o1new\\shimx64.efi"))
        self.assertIsNone(L.new_entry_number(efi, "", "\\EFI\\o1new\\shimx64.efi"))

    def test_same_esp_and_path_under_another_label_is_the_same_entry(self):
        text = "Boot0006* something else\tHD(1,GPT,%s,0x800,0x2)/File(\\EFI\\o1new\\shimx64.efi)\n" % NEW
        self.assertEqual(L.new_entry_number(L.parse_efi(text), NEW, "\\EFI\\o1new\\shimx64.efi"), "0006")

    def test_our_label_wins_over_another_entry_on_the_esp(self):
        text = ("Boot0001* other\tHD(1,GPT,%s,0x800,0x2)/File(\\EFI\\BOOT\\BOOTX64.EFI)\n"
                "Boot0002* ollama1-new\tHD(1,GPT,%s,0x800,0x2)/File(\\EFI\\o1new\\grubx64.efi)\n" % (NEW, NEW))
        efi = L.parse_efi(text)
        self.assertEqual(L.new_entry_number(efi, NEW, "\\EFI\\o1new\\shimx64.efi"), "0002")      # same ESP and label, another loader name
        self.assertIsNone(L.new_entry_number(L.parse_efi(text.split("\n")[0] + "\n"), NEW, "\\EFI\\o1new\\shimx64.efi"))

    def test_the_boot_order_has_each_entry_once(self):
        efi = L.parse_efi(self.dup())
        order = L.boot_order_old_first("0003", "0000", ["0005", "0000", "0001", "0003", "0002"], efi, NEW)
        self.assertEqual(order, ["0000", "0001", "0002", "0003"])         # the new entry's duplicate (0005) is left out
        self.assertEqual(len(order), len(set(order)))
        order = L.boot_order("0003", "0000", ["0005", "0000", "0001", "0003", "0002"], efi, NEW)
        self.assertEqual(order, ["0003", "0000", "0001", "0002"])
        # the old signatures still work
        self.assertEqual(L.boot_order("0007", "0002", ["0001", "0002", "0000"]), ["0007", "0002", "0001", "0000"])
        self.assertEqual(L.boot_order_old_first("0003", "0001", ["0001", "0000"]), ["0001", "0000", "0003"])


class FirmwareBase(unittest.TestCase):
    def setUp(self):
        self.m = Machine()

    def tearDown(self):
        self.m.close()

    def full_run(self, **efi_flags):
        m = self.m
        m.reload()
        m.fs["efi"].update(efi_flags)
        m.flush()
        rc = m.run("--run")
        self.assertEqual(rc, 0, m.out)

    def made(self):
        return [l for l in self.m.log("efibootmgr") if " -c " in l]


class TestFirmwareStage(FirmwareBase):
    def test_spaces_after_the_label_no_longer_stop_the_stage(self):
        # the report: "efibootmgr made no entry" because the printed list wasn't read the way the tool expected
        self.full_run(sep="   ")
        e = self.m.fs["efi"]
        self.assertEqual(self.m.state()["efi"]["new"], "0002")
        self.assertEqual(e["order"], ["0001", "0000", "0002"])
        self.assertEqual(len(self.made()), 1)

    def test_a_bare_loader_path_with_trailing_data_no_longer_stops_the_stage(self):
        self.full_run(bare=True, trail="0000424f")
        self.assertEqual(self.m.state()["efi"]["new"], "0002")
        self.assertEqual(len(self.made()), 1)

    def test_the_entry_of_a_stopped_run_is_reused(self):
        self.full_run()
        m = self.m
        st = m.state()
        st["done"].pop("firmware")
        st.pop("efi")
        with open(m.cfg_state, "w") as fh:
            json.dump(st, fh)
        self.assertEqual(m.run("--resume"), 0, m.out)
        self.assertEqual(len(self.made()), 1)                         # not made a second time
        self.assertIn("entry 0002 already boots the new drive's ESP", m.out)
        self.assertEqual(m.state()["efi"]["new"], "0002")
        self.assertEqual(m.fs["efi"]["order"], ["0001", "0000", "0002"])
        self.assertEqual(m.fs["efi"]["next"], "0002")

    def test_an_entry_listed_twice_is_used_once_and_kept_out_of_the_order_twice(self):
        self.full_run()
        m = self.m
        m.reload()
        m.fs["efi"]["entries"]["0003"] = dict(m.fs["efi"]["entries"]["0002"])       # the firmware lists it again
        m.fs["efi"]["order"] = ["0003", "0001", "0000", "0002"]
        m.flush()
        st = m.state()
        st["done"].pop("firmware")
        st.pop("efi")
        with open(m.cfg_state, "w") as fh:
            json.dump(st, fh)
        self.assertEqual(m.run("--resume"), 0, m.out)
        self.assertEqual(len(self.made()), 1)
        self.assertIn("lists it again as 0003", m.out)
        self.assertEqual(m.state()["efi"]["new"], "0002")
        self.assertEqual(m.fs["efi"]["order"], ["0001", "0000", "0002"])

    def test_an_inactive_entry_of_an_earlier_run_is_made_active(self):
        self.full_run()
        m = self.m
        m.reload()
        m.fs["efi"]["entries"]["0002"]["active"] = False
        m.flush()
        st = m.state()
        st["done"].pop("firmware")
        st.pop("efi")
        with open(m.cfg_state, "w") as fh:
            json.dump(st, fh)
        self.assertEqual(m.run("--resume"), 0, m.out)
        self.assertIn("efibootmgr -a -b 0002", m.log("efibootmgr"))
        self.assertTrue(m.fs["efi"]["entries"]["0002"].get("active", True))

    def test_no_entry_after_the_make_says_what_it_saw(self):
        self.full_run()
        m = self.m
        m.reload()
        del m.fs["efi"]["entries"]["0002"]
        m.fs["efi"]["order"] = ["0001", "0000"]
        m.fs["efi_make_wrong_esp"] = True            # the firmware "makes" an entry that is on some other ESP
        m.flush()
        st = m.state()
        st["done"].pop("firmware")
        st.pop("efi")
        with open(m.cfg_state, "w") as fh:
            json.dump(st, fh)
        self.assertEqual(m.run("--resume"), 1)
        self.assertIn("efibootmgr made no entry for the new drive's ESP", m.out)
        self.assertIn("ollama1-new", m.out)
        self.assertIn("\\EFI\\o1new\\", m.out)
        self.assertIn("run --resume again, it is reused", m.out)


class TestDisableOldBootFiles(FirmwareBase):
    def finish_ready(self):
        self.full_run()
        self.m.reboot_into_new()
        self.esp = self.m.dir + "/run/o1migrate/old-esp"
        for rel in ("EFI/ubuntu/shimx64.efi", "EFI/ubuntu/grub.cfg", "EFI/BOOT/BOOTX64.EFI", "EFI/Other/keep.efi"):
            self.m.put(os.path.join(self.esp, rel), rel)

    def undo(self):
        return self.m.data_dir + "/old-drive-boot-files-undo.txt"

    def test_off_by_default_the_old_drive_is_untouched(self):
        self.finish_ready()
        m = self.m
        self.assertEqual(m.run("--finish", answers=["n"]), 0, m.out)
        self.assertTrue(os.path.isdir(self.esp + "/EFI/ubuntu"))
        self.assertTrue(os.path.isdir(self.esp + "/EFI/BOOT"))
        self.assertFalse(os.path.exists(self.undo()))
        self.assertEqual([l for l in m.log("mount") if m.byid("from") in l], [])
        self.assertIn("--disable-old-boot-files", m.out)                 # said as an option, not done

    def test_the_flag_goes_with_finish(self):
        self.assertEqual(self.m.run("--run", "--disable-old-boot-files"), 2)

    def test_it_asks_and_no_changes_nothing(self):
        self.finish_ready()
        m = self.m
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["no"]), 1)
        self.assertIn("not confirmed", m.out)
        self.assertTrue(os.path.isdir(self.esp + "/EFI/ubuntu"))
        self.assertFalse(os.path.exists(self.undo()))
        self.assertEqual([l for l in m.log("mount") if "-part1" in l and m.byid("from") in l], [])

    def test_yes_renames_the_two_folders_and_writes_the_way_back_first(self):
        self.finish_ready()
        m = self.m
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"]), 0, m.out)
        self.assertFalse(os.path.exists(self.esp + "/EFI/ubuntu"))
        self.assertFalse(os.path.exists(self.esp + "/EFI/BOOT"))
        self.assertTrue(os.path.isfile(self.esp + "/EFI/ubuntu.off/shimx64.efi"))
        self.assertTrue(os.path.isfile(self.esp + "/EFI/BOOT.off/BOOTX64.EFI"))
        self.assertTrue(os.path.isfile(self.esp + "/EFI/Other/keep.efi"))          # nothing else touched
        note = m.read(self.undo())
        self.assertIn("ubuntu -> ubuntu.off", note)
        self.assertIn("BOOT -> BOOT.off", note)
        self.assertIn("mv /mnt/old-esp/EFI/ubuntu.off /mnt/old-esp/EFI/ubuntu", note)
        self.assertIn("mv /mnt/old-esp/EFI/BOOT.off /mnt/old-esp/EFI/BOOT", note)
        self.assertIn(m.byid("from", 1), note)                                  # the old drive's EFI partition, by id
        self.assertIn(FROM, note)
        # mounted only for that, from the old drive's own ESP, and unmounted again
        mounts = [l for l in m.log("mount") if m.byid("from", 1) in l]
        self.assertEqual(len(mounts), 1)
        self.assertIn(m.byid("from", 1), mounts[0])
        self.assertEqual([x for x in m.fs["mounts"] if x["target"].endswith("/old-esp")], [])
        self.assertIn("EFI/ubuntu -> EFI/ubuntu.off", m.out)
        self.assertIn("not a fallback until they are renamed back", m.out)
        # the old drive's entry and the new boot order are not the business of this step
        self.assertEqual([l for l in m.log("efibootmgr") if " -A " in l or " -B " in l], [])

    def test_it_is_repeatable(self):
        self.finish_ready()
        m = self.m
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"]), 0, m.out)
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"]), 0, m.out)
        self.assertIn("already put aside", m.out)
        self.assertTrue(os.path.isdir(self.esp + "/EFI/ubuntu.off"))

    def test_a_folder_that_is_in_the_way_stops_it_before_renaming_anything(self):
        self.finish_ready()
        m = self.m
        os.makedirs(self.esp + "/EFI/ubuntu.off")                         # an earlier, different one
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"]), 1)
        self.assertIn("ubuntu and ubuntu.off both exist", m.out)
        self.assertTrue(os.path.isdir(self.esp + "/EFI/BOOT"))             # not even the other one
        self.assertTrue(os.path.isfile(self.esp + "/EFI/ubuntu/shimx64.efi"))
        self.assertEqual([x for x in m.fs["mounts"] if x["target"].endswith("/old-esp")], [])   # and it is unmounted again

    def test_the_names_are_matched_the_way_fat_does(self):
        todo, done, blocked = L.plan_boot_file_renames(["UBUNTU", "boot", "Microsoft"])
        self.assertEqual(todo, [("UBUNTU", "UBUNTU.off"), ("boot", "boot.off")])
        self.assertEqual((done, blocked), ([], []))
        todo, done, blocked = L.plan_boot_file_renames(["ubuntu.off", "BOOT.OFF"])
        self.assertEqual((todo, sorted(done), blocked), ([], ["BOOT.OFF", "ubuntu.off"], []))
        self.assertEqual(L.plan_boot_file_renames([]), ([], [], []))

    def test_it_never_guesses_the_partition(self):
        self.finish_ready()
        m = self.m
        st = m.state()
        st["old_esp_partuuid"] = fu("9")
        with open(m.cfg_state, "w") as fh:
            json.dump(st, fh)
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"]), 1)
        self.assertIn("no partition of the old drive", m.out)
        self.assertTrue(os.path.isdir(self.esp + "/EFI/ubuntu"))
        self.assertEqual([l for l in m.log("mount") if "-part" in l and m.byid("from") in l], [])
        st["old_esp_partuuid"] = ""
        with open(m.cfg_state, "w") as fh:
            json.dump(st, fh)
        self.assertEqual(m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"]), 1)
        self.assertIn("never recorded which partition", m.out)

    def test_the_old_drive_must_be_there(self):
        self.finish_ready()
        m = self.m
        for name in os.listdir(m.dir + "/byid"):
            if FROM in name:
                os.unlink(os.path.join(m.dir, "byid", name))
        rc = m.run("--finish", "--disable-old-boot-files", answers=["yes", "n"])
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.isdir(self.esp + "/EFI/ubuntu"))

    def test_the_undo_text(self):
        text = L.undo_note_text("/dev/disk/by-id/x-part1", None, [("ubuntu", "ubuntu.off")], "SER", "2026-10-04")
        self.assertIn("mount /dev/disk/by-id/x-part1 /mnt/old-esp", text)
        self.assertIn("mv /mnt/old-esp/EFI/ubuntu.off /mnt/old-esp/EFI/ubuntu", text)
        self.assertIn("efibootmgr -a -b <number>", text)


if __name__ == "__main__":
    unittest.main()
