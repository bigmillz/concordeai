"""Graphics-card tuning (6b361): lib/o1gputune.py and bin/ollama1-gpu-tune
against fixture sysfs trees with a stand-in amdgpu driver (it refuses what
the real one refuses: a power limit above power1_cap_max, a memory clock
outside OD_RANGE), a stand-in Ollama and kernel log, and a fake clock.
Plus setup.sh's flag, the units and the sleep hook."""
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U
import o1gputune as T

NAVI21_OD = """OD_SCLK:
0: 500Mhz
1: 2660Mhz
OD_MCLK:
0: 97Mhz
1: 1000MHz
OD_VDDGFX_OFFSET:
0mV
OD_RANGE:
SCLK:     500Mhz       3150Mhz
MCLK:     674Mhz       1075Mhz
"""
VEGA_OD = """OD_SCLK:
0:        852Mhz        800mV
1:       1991Mhz       1200mV
OD_MCLK:
0:        167Mhz        800mV
1:       1000Mhz        900mV
OD_RANGE:
SCLK:     852MHz       2400MHz
MCLK:     167MHz       1500MHz
"""
W = 10 ** 6
NAVI_ADDR = "0000:0b:00.0"
IGPU_ADDR = "0000:00:02.0"
RING_TIMEOUT = "amdgpu 0000:0b:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1, emitted seq=3"


class Tree:
    """A fixture /sys: PCI devices, /sys/class/drm/cardN links, amdgpu's mask."""

    def __init__(self, root):
        self.root = root

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def pci(self, addr, vendor, device, cls="0x030000"):
        d = "bus/pci/devices/" + addr
        self.write(d + "/vendor", vendor + "\n")
        self.write(d + "/device", device + "\n")
        self.write(d + "/class", cls + "\n")
        return os.path.join(self.root, d)

    def drm(self, n, addr):
        os.makedirs(os.path.join(self.root, "class/drm"), exist_ok=True)
        link = os.path.join(self.root, "class/drm/card%d" % n)
        os.makedirs(link, exist_ok=True)
        os.symlink(os.path.join(self.root, "bus/pci/devices", addr), os.path.join(link, "device"))

    def navi21(self, addr=NAVI_ADDR, od=NAVI21_OD, cap=255, default=255, mx=293, mn=0, device="0x73bf"):
        d = self.pci(addr, "0x1002", device)
        rel = "bus/pci/devices/%s/" % addr
        if od is not None:
            self.write(rel + "pp_od_clk_voltage", od)
        self.write(rel + "power_dpm_force_performance_level", "auto\n")
        self.write(rel + "mem_info_vram_total", str(16 << 30) + "\n")
        hw = rel + "hwmon/hwmon3/"
        for f, v in (("power1_cap", cap), ("power1_cap_default", default), ("power1_cap_max", mx),
                     ("power1_cap_min", mn)):
            self.write(hw + f, "%d\n" % (v * W))
        for i, (lab, c) in enumerate((("edge", 50), ("junction", 60), ("mem", 58)), 1):
            self.write(hw + "temp%d_label" % i, lab + "\n")
            self.write(hw + "temp%d_input" % i, "%d\n" % (c * 1000))
        return d

    def temp(self, label, c, addr=NAVI_ADDR):
        i = {"edge": 1, "junction": 2, "mem": 3}[label]
        self.write("bus/pci/devices/%s/hwmon/hwmon3/temp%d_input" % (addr, i), "%d\n" % (c * 1000))

    def mask(self, text):
        self.write("module/amdgpu/parameters/ppfeaturemask", text + "\n")


class FakeDriver(T.SysfsIO):
    """amdgpu's side of the sysfs files: power1_cap within [min, max] (whole
    watts, as the driver keeps it), pp_od_clk_voltage commands ("m", "r", "c")
    within OD_RANGE, optionally only in "manual"."""

    def __init__(self, need_manual=False):
        self.writes = []
        self.need_manual = need_manual
        self.pending = None

    def _hw(self, path, name):
        return int(open(os.path.join(os.path.dirname(path), name)).read())

    def write(self, path, value):
        name = os.path.basename(path)
        self.writes.append((name, value))
        if name == "power1_cap":
            v = int(value)
            if v > self._hw(path, "power1_cap_max") or v < self._hw(path, "power1_cap_min"):
                raise OSError(errno.EINVAL, "Invalid argument")
            return super().write(path, "%d\n" % (v // W * W))
        if name == "pp_od_clk_voltage":
            dev = os.path.dirname(path)
            if self.need_manual and open(os.path.join(dev, T.LEVEL_FILE)).read().strip() != "manual":
                raise OSError(errno.EINVAL, "Invalid argument")
            od = T.parse_od(open(path).read())
            if self.pending is None:
                self.pending = dict(od["mclk"])
            parts = value.split()
            if parts[0] == "m":
                i, v = int(parts[1]), int(parts[2])
                lo, hi = od["range"]["MCLK"]
                if i not in (0, 1) or not lo <= v <= hi:
                    raise OSError(errno.EINVAL, "Invalid argument")
                self.pending[i] = v
            elif parts[0] == "r":
                self.pending = {0: 97, 1: 1000}
            elif parts[0] == "c":
                text = open(path).read()
                if od["odd"]:                                 # an older card's table: not modelled here
                    self.pending = None
                    return None
                lines, sec = [], None
                for l in text.splitlines():
                    if l.endswith(":"):
                        sec = l[:-1]
                    elif sec == "OD_MCLK" and l[:1] in "01":
                        l = "%s: %dMhz" % (l[0], self.pending[int(l[0])])
                    lines.append(l)
                super().write(path, "\n".join(lines) + "\n")
                self.pending = None
            else:
                raise OSError(errno.EINVAL, "Invalid argument")
            return None
        return super().write(path, value)

    def card_writes(self):
        return [w for w in self.writes if w[0] in ("power1_cap", "pp_od_clk_voltage")]


class Crash(BaseException):
    pass


class Clock:
    def __init__(self):
        self.t = 0.0


class FakeOllama:
    def __init__(self, clock, tree, models=(("qwen3:14b", 9 << 30),), stock_tps=40.0, tuned_tps=42.0,
                 answer=True, on_generate=None):
        self.clock, self.tree = clock, tree
        self.list = [{"name": n, "size": s} for n, s in models]
        self.stock_tps, self.tuned_tps = stock_tps, tuned_tps
        self.answer = answer
        self.on_generate = on_generate
        self.calls = []
        self.is_up = True

    def up(self):
        return self.is_up

    def models(self):
        return list(self.list)

    def loaded(self):
        return []

    def generate(self, model):
        self.calls.append(model)
        self.clock.t += 4
        if self.on_generate:
            self.on_generate(len(self.calls))
        if not self.answer:
            return None
        try:
            od = open(os.path.join(self.tree.root, "bus/pci/devices", NAVI_ADDR, "pp_od_clk_voltage")).read()
        except OSError:
            od = ""
        tps = self.tuned_tps if T.parse_od(od)["mclk"].get(1, 1000) > 1000 else self.stock_tps
        return int(tps * 4), 4.0


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1tune-")
        self.tree = Tree(os.path.join(self.dir, "sys"))
        os.makedirs(self.tree.root)
        self.old_sys = os.environ.get("OLLAMA1_SYS")
        os.environ["OLLAMA1_SYS"] = self.tree.root
        try:
            os.unlink(T.state_path())
        except OSError:
            pass
        self.clock = Clock()
        self.klog = []                 # this boot's kernel log
        self.boot_logs = {}            # earlier boots' kernel logs
        self.journal_calls = []
        self.boot = "bootb"
        self.out = []

    def tearDown(self):
        if self.old_sys is None:
            os.environ.pop("OLLAMA1_SYS", None)
        else:
            os.environ["OLLAMA1_SYS"] = self.old_sys
        shutil.rmtree(self.dir, ignore_errors=True)

    def journal(self, args):
        self.journal_calls.append(list(args))
        if args[0] == "-b":
            return self.boot_logs.get(args[1])
        return list(self.klog)

    def tuner(self, driver=None, ollama=None, **kw):
        self.driver = driver or FakeDriver()
        self.ollama = ollama or FakeOllama(self.clock, self.tree)
        return T.Tuner(io=self.driver, ollama=self.ollama, journal=self.journal,
                       clock=lambda: 1700000000 + self.clock.t, mono=lambda: self.clock.t, boot=lambda: self.boot,
                       out=self.out.append, check_seconds=60, stock_seconds=20, **kw)

    def dev(self, rel, addr=NAVI_ADDR):
        return open(os.path.join(self.tree.root, "bus/pci/devices", addr, rel)).read()

    def cap(self):
        return int(self.dev("hwmon/hwmon3/power1_cap"))

    def mclk(self):
        return T.parse_od(self.dev("pp_od_clk_voltage"))["mclk"][1]

    def state(self):
        return json.load(open(T.state_path()))


class TestParsing(unittest.TestCase):
    def test_navi21_table(self):
        od = T.parse_od(NAVI21_OD)
        self.assertEqual(od["mclk"], {0: 97, 1: 1000})
        self.assertEqual(od["range"], {"SCLK": (500, 3150), "MCLK": (674, 1075)})
        self.assertTrue(T.od_usable(od))

    def test_an_older_cards_table_is_left_alone(self):
        od = T.parse_od(VEGA_OD)
        self.assertTrue(od["odd"])
        self.assertFalse(T.od_usable(od))

    def test_no_range_or_no_table(self):
        self.assertFalse(T.od_usable(T.parse_od("OD_MCLK:\n0: 97Mhz\n1: 1000Mhz\n")))
        self.assertFalse(T.od_usable(T.parse_od("")))
        self.assertFalse(T.od_usable(None))

    def test_memory_clock_target_is_clamped_to_the_range(self):
        self.assertEqual(T.mclk_target(1000, 1075), 1075)     # the 6900 XT's range stops short of +100
        self.assertEqual(T.mclk_target(1000, 1200), 1100)     # +100 and no more
        self.assertEqual(T.mclk_target(1000, 1000), None)     # nothing above stock
        self.assertEqual(T.mclk_target(1100, 1075), None)     # stock already past the range: never lowered
        self.assertEqual(T.mclk_target(None, 1075), None)

    def test_power_target_is_the_cards_maximum(self):
        self.assertEqual(T.power_target({"cap": 255 * W, "default": 255 * W, "max": 293 * W, "min": 0}), 293 * W)
        self.assertIsNone(T.power_target({"default": 255 * W, "max": 255 * W}))     # overdrive off
        self.assertIsNone(T.power_target({}))

    def test_feature_mask_adds_only_the_overdrive_bit(self):
        self.assertEqual(T.feature_mask("0xfff7bfff"), 0xfff7ffff)        # the driver's default
        self.assertEqual(T.feature_mask(str(0xfff7bfff)), 0xfff7ffff)        # the same, printed in decimal
        self.assertEqual(T.feature_mask("0x00000001"), 0x4001)
        self.assertEqual(T.feature_mask("0xfff7ffff"), 0xfff7ffff)        # already on: unchanged
        for m in (0, 0x1, 0x80000, 0xfff7bfff, 0x12345678):
            self.assertEqual(T.feature_mask(hex(m)), m | 0x4000)          # every other bit kept as it was
        self.assertIsNone(T.feature_mask("junk"))
        self.assertIsNone(T.feature_mask(None))

    def test_grub_dropin(self):
        t = T.grub_dropin(0xfff7ffff)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT amdgpu.ppfeaturemask=0xfff7ffff"', t)
        self.assertNotIn("0xffffffff", t)

    def test_kernel_errors(self):
        lines = ["amdgpu: Overdrive is enabled, please disable it before reporting any bugs unrelated to overdrive.",
                 "amdgpu 0000:0b:00.0: amdgpu: SMU is initialized successfully!", "amdgpu: GPU mode1 reset",
                 RING_TIMEOUT, "amdgpu 0000:0b:00.0: amdgpu: GPU reset begin!",
                 "amdgpu 0000:0b:00.0: amdgpu: [gfxhub] page fault (src_id:0 ring:24 vmid:3 pasid:32769)",
                 "[drm:amdgpu_job_timedout [amdgpu]] *ERROR* ring sdma0 timeout, signaled seq=5, emitted seq=6"]
        self.assertEqual(len(T.amdgpu_errors(lines)), 4)
        self.assertEqual(T.amdgpu_errors(lines[:3]), [])
        self.assertEqual(T.amdgpu_errors(None), [])


class TestFindCard(Base):
    def test_found_by_ids_not_by_card_number(self):
        self.tree.pci(IGPU_ADDR, "0x8086", "0x4680")
        self.tree.navi21()
        self.tree.drm(0, IGPU_ADDR)
        self.tree.drm(1, NAVI_ADDR)
        card, why = T.find_card()
        self.assertIsNone(why)
        self.assertEqual(card.pci, NAVI_ADDR)
        self.assertEqual(card.device, "0x73bf")

    def test_renumbered(self):
        self.tree.pci(IGPU_ADDR, "0x8086", "0x4680")
        self.tree.navi21()
        self.tree.drm(0, NAVI_ADDR)
        self.tree.drm(1, IGPU_ADDR)
        self.assertEqual(T.find_card()[0].pci, NAVI_ADDR)

    def test_not_amd(self):
        self.tree.pci("0000:01:00.0", "0x10de", "0x2684")
        card, why = T.find_card()
        self.assertIsNone(card)
        self.assertIn("no AMD Navi 21", why)

    def test_another_amd_card(self):
        self.tree.navi21(device="0x744c")                  # a 7900 XTX: not what this is tuned for
        card, why = T.find_card()
        self.assertIsNone(card)
        self.assertIn("isn't a Navi 21", why)

    def test_audio_function_is_not_the_card(self):
        self.tree.pci("0000:0b:00.1", "0x1002", "0xab28", cls="0x040300")
        self.assertIsNone(T.find_card()[0])

    def test_no_amd_card_is_a_quiet_no_op(self):
        self.tree.pci("0000:01:00.0", "0x10de", "0x2684")
        t = self.tuner()
        self.assertEqual(t.cmd_on(), 0)
        self.assertEqual(t.cmd_restore(), 0)
        self.assertEqual(self.driver.writes, [])
        self.assertEqual(len([l for l in self.out if "nothing is tuned" in l]), 2)


class TestApply(Base):
    def test_on_sets_the_maximum_power_and_the_memory_clock_then_checks(self):
        self.tree.navi21()
        t = self.tuner()
        t.cmd_on()
        self.assertEqual(self.cap(), 293 * W)                 # power1_cap_max
        self.assertEqual(self.mclk(), 1075)                   # stock 1000 + 100, clamped to OD_RANGE's 1075
        self.assertEqual(self.dev(T.LEVEL_FILE).strip(), "auto")
        for name, v in self.driver.card_writes():
            if name == "power1_cap":
                self.assertLessEqual(int(v), 293 * W)          # never a value above the card's maximum
        st = self.state()
        self.assertEqual(st["wanted"], "on")
        self.assertEqual(st["stock"], {"mclk": 1000, "power_uw": 255 * W})
        self.assertEqual(st["applied"]["mclk"], 1075)
        self.assertEqual(st["applied"]["power_uw"], 293 * W)
        self.assertEqual(st["check"]["result"], "passed")
        self.assertEqual(st["measure"]["stock_tps"], 40.0)
        self.assertEqual(st["check"]["tps"], 42.0)
        self.assertEqual(st["checked"], [293 * W, 1075])
        self.assertGreaterEqual(len(self.ollama.calls), 15)    # 20 s of stock, then 60 s of load
        self.assertIn("+5.0%", st["check"]["note"])

    def test_a_wider_range_still_only_adds_100(self):
        self.tree.navi21(od=NAVI21_OD.replace("1075Mhz", "1300Mhz"))
        self.tuner().cmd_on()
        self.assertEqual(self.mclk(), 1100)

    def test_a_range_that_allows_nothing_more(self):
        self.tree.navi21(od=NAVI21_OD.replace("1075Mhz", "1000Mhz"))
        t = self.tuner()
        t.cmd_on()
        self.assertEqual(self.mclk(), 1000)
        self.assertNotIn(("pp_od_clk_voltage", "m 1 1100"), self.driver.writes)
        self.assertEqual(self.cap(), 293 * W)

    def test_without_overdrive_only_what_the_driver_allows(self):
        self.tree.navi21(od=None, mx=255)                    # no mask yet: no table, max = default
        t = self.tuner()
        t.cmd_on()
        self.assertEqual(self.driver.card_writes(), [])
        self.assertEqual(self.cap(), 255 * W)
        self.assertTrue(any("reboot" in l for l in self.out))
        self.assertEqual(self.ollama.calls[:1], ["qwen3:14b"])  # stock was measured, for the check later
        self.assertNotIn("check", self.state())
        self.assertFalse(t.pending())

    def test_power_without_the_memory_table(self):
        self.tree.navi21(od=None)
        self.tuner().cmd_on()
        self.assertEqual(self.cap(), 293 * W)
        self.assertEqual(self.state()["applied"]["mclk"], None)

    def test_an_older_tables_shape_is_not_written(self):
        self.tree.navi21(od=VEGA_OD)
        t = self.tuner()
        t.cmd_on()
        self.assertFalse([w for w in self.driver.writes if w[0] == "pp_od_clk_voltage" and w[1].startswith("m")])

    def test_a_card_that_wants_manual_ends_in_auto(self):
        self.tree.navi21()
        self.tuner(driver=FakeDriver(need_manual=True)).cmd_on()
        self.assertEqual(self.mclk(), 1075)
        self.assertEqual(self.dev(T.LEVEL_FILE).strip(), "auto")

    def test_apply_twice_writes_nothing_new(self):
        self.tree.navi21()
        self.tuner().cmd_on()
        t = self.tuner()
        t.cmd_apply()
        self.assertEqual(self.driver.card_writes(), [])
        self.assertEqual(self.ollama.calls, [])               # passed for these values: no second load

    def test_off_puts_it_back_and_a_boot_keeps_it_off(self):
        self.tree.navi21()
        self.tuner().cmd_on()
        self.tuner().cmd_off()
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))
        self.tree.navi21()                                    # a reboot: the card starts at stock
        t = self.tuner()
        t.cmd_restore()
        self.assertEqual(self.driver.card_writes(), [])
        self.assertEqual(self.state()["wanted"], "off")


class TestSafety(Base):
    def test_a_kernel_error_during_the_check_reverts_at_once(self):
        self.tree.navi21()

        def hang(n):
            if len(self.ollama.calls) > 6:                    # into the load, after the stock measurement
                self.klog.append(RING_TIMEOUT)
        self.ollama = FakeOllama(self.clock, self.tree, on_generate=hang)
        t = self.tuner(ollama=self.ollama)
        t.cmd_on()
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))
        st = self.state()
        self.assertIn("ring gfx_0.0.0 timeout", st["reverted"])
        self.assertEqual(st["check"]["result"], "reverted")
        self.assertLess(len(self.ollama.calls), 15)           # stopped early, not after the full minute

    def test_reverted_is_never_reapplied_until_on(self):
        self.tree.navi21()
        self.klog.append(RING_TIMEOUT)
        self.tuner().cmd_on()
        self.assertTrue(self.state()["reverted"])
        self.klog.clear()
        self.tree.navi21()                                    # a reboot
        self.boot = "bootc"
        for cmd in ("cmd_restore", "cmd_apply", "cmd_check_pending"):
            t = self.tuner()
            getattr(t, cmd)()
            self.assertEqual(self.driver.card_writes(), [], cmd)
        t = self.tuner()
        t.cmd_setup("on")                                      # a re-run of setup keeps the revert
        self.assertEqual(self.driver.card_writes(), [])
        t = self.tuner()
        t.cmd_on()                                             # the admin, explicitly
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1075))
        self.assertNotIn("reverted", self.state())

    def test_hot_junction_reverts(self):
        self.tree.navi21()
        self.ollama = FakeOllama(self.clock, self.tree,
                                 on_generate=lambda n: self.tree.temp("junction", 106 if n > 8 else 80))
        self.tuner(ollama=self.ollama).cmd_on()
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))
        self.assertIn("junction reached 106 C", self.state()["reverted"])

    def test_hot_memory_reverts(self):
        self.tree.navi21()
        self.ollama = FakeOllama(self.clock, self.tree,
                                 on_generate=lambda n: self.tree.temp("mem", 100 if n > 8 else 80))
        self.tuner(ollama=self.ollama).cmd_on()
        self.assertIn("memory reached 100 C", self.state()["reverted"])
        self.assertEqual(self.cap(), 255 * W)

    def test_slower_than_stock_reverts(self):
        self.tree.navi21()
        self.tuner(ollama=FakeOllama(self.clock, self.tree, stock_tps=40.0, tuned_tps=36.0)).cmd_on()
        self.assertIn("slower than stock", self.state()["reverted"])
        self.assertEqual(self.mclk(), 1000)

    def test_previous_boot_with_an_amdgpu_error_reverts_and_stays(self):
        self.tree.navi21()
        self.boot = "boota"
        self.tuner().cmd_on()
        self.tree.navi21()                                    # rebooted: stock again
        self.boot = "bootb"
        self.boot_logs["boota"] = ["amdgpu 0000:0b:00.0: amdgpu: GPU reset begin!"]
        t = self.tuner()
        t.cmd_restore()
        self.assertIn(["-b", "boota"], self.journal_calls)
        self.assertIn("GPU reset", self.state()["reverted"])
        self.assertFalse([w for w in self.driver.card_writes() if w[1].startswith("m")])
        self.assertEqual(self.cap(), 255 * W)

    def test_a_check_the_machine_never_finished_reverts(self):
        self.tree.navi21()
        self.boot = "boota"

        def crash(n):
            if len(self.ollama.calls) > 8:
                raise Crash                                   # the machine stops mid-load
        self.ollama = FakeOllama(self.clock, self.tree, on_generate=crash)
        with self.assertRaises(Crash):
            self.tuner(ollama=self.ollama).cmd_on()
        self.assertEqual(self.state()["check"]["result"], "running")
        self.tree.navi21()                                    # the reboot, with a clean log
        self.boot = "bootb"
        self.boot_logs["boota"] = []
        self.tuner().cmd_restore()
        self.assertIn("never ended", self.state()["reverted"])
        self.assertFalse([w for w in self.driver.card_writes() if w[1].startswith("m") or w[1] == str(293 * W)])
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))

    def test_ctrl_c_during_the_check_puts_it_back(self):
        self.tree.navi21()

        def stop(n):
            if len(self.ollama.calls) > 8:
                raise KeyboardInterrupt
        self.ollama = FakeOllama(self.clock, self.tree, on_generate=stop)
        with self.assertRaises(KeyboardInterrupt):
            self.tuner(ollama=self.ollama).cmd_on()
        self.assertIn("stopped before it ended", self.state()["reverted"])
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))

    def test_previous_boot_clean_applies_and_is_read_once(self):
        self.tree.navi21()
        self.boot = "boota"
        self.tuner().cmd_on()
        self.tree.navi21()
        self.boot = "bootb"
        self.boot_logs["boota"] = ["amdgpu: SMU is initialized successfully!"]
        self.tuner().cmd_restore()
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1075))
        self.assertEqual(self.state()["applied"]["boot"], "bootb")
        self.journal_calls.clear()
        self.tuner().cmd_restore()                            # a wake in the same boot
        self.assertNotIn(["-b", "boota"], self.journal_calls)

    def test_a_wake_after_an_error_this_boot_reverts(self):
        self.tree.navi21()
        self.tuner().cmd_on()
        self.klog.append(RING_TIMEOUT)
        self.tuner().cmd_restore()
        self.assertIn("ring gfx_0.0.0 timeout", self.state()["reverted"])
        self.assertEqual(self.mclk(), 1000)

    def test_a_wake_that_lost_the_values_sets_them_again(self):
        self.tree.navi21()
        self.tuner().cmd_on()
        self.tree.navi21()                                    # as if the driver hadn't kept them
        self.tuner().cmd_restore()
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1075))

    def test_no_model_checks_later(self):
        self.tree.navi21()
        t = self.tuner(ollama=FakeOllama(self.clock, self.tree, models=()))
        t.cmd_on()
        self.assertEqual(self.mclk(), 1075)
        self.assertEqual(self.state()["check"]["result"], "no load")
        self.assertTrue(t.pending())
        t = self.tuner()                                       # a model is there now (the check unit, next boot)
        t.cmd_check_pending()
        self.assertEqual(self.state()["check"]["result"], "passed")
        self.assertFalse(t.pending())

    def test_ollama_silent_is_not_a_pass(self):
        self.tree.navi21()
        t = self.tuner(ollama=FakeOllama(self.clock, self.tree, answer=False))
        t.cmd_on()
        self.assertEqual(self.state()["check"]["result"], "no answer")
        self.assertTrue(t.pending())


class TestSetupChoice(Base):
    def test_first_run_turns_it_on(self):
        self.tree.navi21()
        self.tuner().cmd_setup("on")
        self.assertEqual(self.mclk(), 1075)

    def test_an_admins_off_is_kept(self):
        self.tree.navi21()
        self.tuner().cmd_off()
        self.tuner().cmd_setup("on")
        self.assertEqual(self.driver.card_writes(), [])
        self.tuner().cmd_setup("force-on")
        self.assertEqual(self.mclk(), 1075)

    def test_opt_out_puts_it_back(self):
        self.tree.navi21()
        self.tuner().cmd_setup("on")
        self.tuner().cmd_setup("off")
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))


class TestStatus(Base):
    def test_status_shows_stock_and_now(self):
        self.tree.navi21()
        self.tree.mask("0xfff7ffff")
        self.tuner().cmd_on()
        t = self.tuner()
        lines = "\n".join(t.status_lines())
        self.assertIn("Graphics card tuning: on", lines)
        self.assertIn("Overdrive: on in the kernel (amdgpu.ppfeaturemask 0xfff7ffff)", lines)
        self.assertIn("Power limit: now 293 W; stock 255 W, the card's maximum 293 W", lines)
        self.assertIn("Memory clock: now 1075 (2150 MHz effective); stock 1000; the card allows up to 1075", lines)
        self.assertIn("passed", lines)
        self.assertIn("sudo ollama1-gpu-tune off", lines)


class TestCommand(Base):
    def run_bin(self, *args):
        env = dict(os.environ, OLLAMA1_SYS=self.tree.root, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-gpu-tune")] + list(args),
                              capture_output=True, text=True, env=env, timeout=30)

    def test_status_without_a_card(self):
        r = self.run_bin("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no AMD Navi 21", r.stdout)

    def test_grub_cfg(self):
        r = self.run_bin("grub-cfg")
        self.assertEqual(r.returncode, 1)                    # no card: setup leaves GRUB alone
        self.tree.navi21()
        self.tree.mask("0xfff7bfff")
        r = self.run_bin("grub-cfg")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("amdgpu.ppfeaturemask=0xfff7ffff", r.stdout)

    def test_usage(self):
        self.assertEqual(self.run_bin("bogus").returncode, 2)
        self.assertEqual(self.run_bin("setup", "maybe").returncode, 2)


class TestSystemPieces(unittest.TestCase):
    def read(self, *rel):
        return open(os.path.join(U.KIT, *rel)).read()

    def test_boot_unit(self):
        u = self.read("systemd", "ollama1-gpu-tune.service")
        self.assertIn("\nBefore=ollama.service\n", u)
        self.assertIn("\nExecStartPre=/usr/local/lib/ollama1/bin/ollama1-wait-gpu\n", u)
        self.assertIn("ollama1-gpu-tune restore\n", u)
        self.assertNotIn("ProtectKernelTunables", u)          # it writes the card's sysfs files
        self.assertIn("\nPrivateNetwork=yes\n", u)
        c = self.read("systemd", "ollama1-gpu-tune-check.service")
        self.assertIn("After=ollama.service", c)
        self.assertIn("ollama1-gpu-tune check-pending\n", c)
        self.assertIn("\nIPAddressDeny=any\n", c)

    def test_sleep_hook_sets_it_again_after_a_wake(self):
        d = tempfile.mkdtemp()
        try:
            log = os.path.join(d, "calls")
            for name, body in (("systemctl", 'echo "$*" >> %s\ncase "$1" in is-enabled) exit "${ENABLED:-0}" ;; esac\n'
                                % log), ("helper", "exit 0\n")):
                with open(os.path.join(d, name), "w") as f:
                    f.write("#!/bin/sh\n" + body)
                os.chmod(os.path.join(d, name), 0o755)
            hook = os.path.join(os.path.dirname(U.LIB), "config", "ollama1-sleep-hook")   # the kit under test
            env = dict(os.environ, PATH=d + ":/usr/bin:/bin", OLLAMA1_HELPER=os.path.join(d, "helper"))
            subprocess.run(["sh", hook, "post", "suspend"], env=env, check=True)
            self.assertIn("start --no-block ollama1-gpu-tune.service", open(log).read())
            os.unlink(log)
            subprocess.run(["sh", hook, "post", "suspend"], env=dict(env, ENABLED="1"), check=True)
            self.assertNotIn("start --no-block ollama1-gpu-tune.service", open(log).read())
            os.unlink(log)
            subprocess.run(["sh", hook, "pre", "suspend"], env=env, check=True)
            self.assertFalse(os.path.exists(log))
        finally:
            shutil.rmtree(d)

    def test_setup_step(self):
        s = self.read("setup.sh")
        a = s.index('step "Graphics card tuning"')
        self.assertLess(s.index('step "Services"'), a)                 # after Ollama is up: the check can load
        self.assertLess(a, s.index('step "Cloudflare Tunnel and Access"'))
        step = s[a:s.index('step "Cloudflare Tunnel and Access"')]
        self.assertIn("GPU_DROPIN=/etc/default/grub.d/97-amdgpu-overdrive.cfg", step)
        self.assertIn('od_cfg=$("$TUNE" grub-cfg 2>/dev/null)', step)
        self.assertIn('rm -f "$GPU_DROPIN"', step)
        self.assertEqual(step.count("run update-grub"), 2)
        self.assertIn("printf 'GPU_TUNE=%s\\n' \"$GPU_TUNE\"", s)
        self.assertIn('ln -sfn "$LIBDIR/bin/ollama1-gpu-tune" /usr/local/sbin/ollama1-gpu-tune', s)

    def bash(self, script, **env):
        r = subprocess.run(["bash", "-c", ". %s; %s" % (os.path.join(U.LIB, "setuplib.sh"), script)],
                           capture_output=True, text=True, env=dict(os.environ, **env))
        return r.returncode, r.stdout.strip()

    def test_choice(self):
        for args, want in ((('""', '""', '""'), "default"), (("off", '""', '""'), "off"), (('""', "0", '""'), "off"),
                           (('""', '""', "off"), "off"), (("on", '""', "off"), "on"), (('""', "1", "off"), "on"),
                           (('""', '""', "on"), "on"), (('""', "0", "on"), "off"), (("on", "0", '""'), "on"),
                           (('""', "1", '""'), "on")):
            self.assertEqual(self.bash("gpu_tune_choice " + " ".join(args)), (0, want), args)
        self.assertEqual(self.bash('gpu_tune_choice "" "" maybe')[0], 1)


class TestSetupFlags(unittest.TestCase):
    def plan(self, *args, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("OLLAMA1_GPU_TUNE", "SAVED")}
        env = dict(base, **env)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--plan"] + list(args), capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=30, env=env)
        return r.returncode, r.stdout + r.stderr

    def saved_file(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "setup.env")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_off_unless_asked_and_says_so(self):
        code, out = self.plan()
        self.assertEqual(code, 0, out)
        self.assertIn("Graphics card tuning: off unless you ask (--gpu-tune", out)
        self.assertNotIn("tuning ON", out)

    def test_opt_in_by_flag_or_environment(self):
        for args, env in ((("--gpu-tune",), {}), ((), {"OLLAMA1_GPU_TUNE": "1"})):
            code, out = self.plan(*args, **env)
            self.assertEqual(code, 0, out)
            self.assertIn("Graphics card tuning ON", out)

    def test_explicit_off(self):
        for args, env in ((("--no-gpu-tune",), {}), ((), {"OLLAMA1_GPU_TUNE": "0"})):
            code, out = self.plan(*args, **env)
            self.assertEqual(code, 0, out)
            self.assertIn("Graphics card tuning OFF", out)

    def test_a_saved_on_is_kept_by_a_rerun_without_the_flag(self):
        saved = self.saved_file("GPU_TUNE=on\n")
        self.assertIn("tuning ON", self.plan(SAVED=saved)[1])
        self.assertIn("tuning OFF", self.plan("--no-gpu-tune", SAVED=saved)[1])
        self.assertIn("tuning OFF", self.plan(SAVED=saved, OLLAMA1_GPU_TUNE="0")[1])
        off = self.saved_file("GPU_TUNE=off\n")
        self.assertIn("tuning OFF", self.plan(SAVED=off)[1])
        self.assertIn("tuning ON", self.plan("--gpu-tune", SAVED=off)[1])
        self.assertIn("tuning ON", self.plan("--gpu-tune", OLLAMA1_GPU_TUNE="0")[1])
        self.assertIn("off unless you ask", self.plan(SAVED=self.saved_file("OWNER=x\n"))[1])

    def test_the_choice_is_saved_only_when_asked(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('if [ "$GPU_TUNE" != default ]; then printf \'GPU_TUNE=%s\\n\' "$GPU_TUNE"; fi', s)
        self.assertIn('elif [ "$GPU_TUNE" = default ]; then', s)       # the step: a plain line, no change

    def test_bad_values_are_refused(self):
        code, out = self.plan(OLLAMA1_GPU_TUNE="maybe")
        self.assertEqual(code, 2)
        code, out = self.plan("--gpu-tune", "--no-gpu-tune")
        self.assertEqual(code, 2)
        self.assertIn("give one", out)


if __name__ == "__main__":
    unittest.main()
