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
                self.pending = {"m": dict(od["mclk"]), "s": dict(od["sclk"])}
            parts = value.split()
            if parts[0] in ("m", "s"):
                i, v = int(parts[1]), int(parts[2])
                lo, hi = od["range"]["MCLK" if parts[0] == "m" else "SCLK"]
                if i not in (0, 1) or not lo <= v <= hi:
                    raise OSError(errno.EINVAL, "Invalid argument")
                self.pending[parts[0]][i] = v
            elif parts[0] == "r":
                self.pending = {"m": {0: 97, 1: 1000}, "s": {0: 500, 1: 2660}}
            elif parts[0] == "c":
                text = open(path).read()
                if od["odd"]:                                 # an older card's table: not modelled here
                    self.pending = None
                    return None
                lines, sec = [], None
                for l in text.splitlines():
                    if l.endswith(":"):
                        sec = l[:-1]
                    elif sec in ("OD_MCLK", "OD_SCLK") and l[:1] in "01":
                        k = "m" if sec == "OD_MCLK" else "s"
                        l = "%s: %dMhz" % (l[0], self.pending[k][int(l[0])])
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
    """Speeds follow what is set on the fake card: stock_tps at stock, tuned_tps when the power limit is raised,
    mem_tps / core_tps (default tuned_tps) when that clock is raised too; prompt reading likewise (stock_ptps,
    core_ptps)."""

    def __init__(self, clock, tree, models=(("qwen3:14b", 9 << 30),), stock_tps=40.0, tuned_tps=42.0,
                 answer=True, on_generate=None, mem_tps=None, core_tps=None, stock_ptps=800.0, core_ptps=None):
        self.clock, self.tree = clock, tree
        self.list = [{"name": n, "size": s} for n, s in models]
        self.stock_tps, self.tuned_tps = stock_tps, tuned_tps
        self.mem_tps, self.core_tps = mem_tps, core_tps
        self.stock_ptps, self.core_ptps = stock_ptps, core_ptps
        self.answer = answer
        self.on_generate = on_generate
        self.calls = []
        self.pcalls = 0
        self.is_up = True

    def up(self):
        return self.is_up

    def models(self):
        return list(self.list)

    def loaded(self):
        return []

    def levels(self):
        """(power raised, memory raised, core raised) on the fake card now."""
        base = os.path.join(self.tree.root, "bus/pci/devices", NAVI_ADDR)
        try:
            od = T.parse_od(open(os.path.join(base, "pp_od_clk_voltage")).read())
        except OSError:
            od = T.parse_od("")
        try:
            cap = int(open(os.path.join(base, "hwmon/hwmon3/power1_cap")).read())
            dflt = int(open(os.path.join(base, "hwmon/hwmon3/power1_cap_default")).read())
        except (OSError, ValueError):
            cap = dflt = 0
        return cap > dflt, od["mclk"].get(1, 1000) > 1000, od["sclk"].get(1, 2660) > 2660

    def rate(self):
        power, mem, core = self.levels()
        tps = self.tuned_tps if power else self.stock_tps
        if mem and self.mem_tps is not None:
            tps = self.mem_tps
        if core and self.core_tps is not None:
            tps = self.core_tps
        return tps

    def generate(self, model):
        self.calls.append(model)
        self.clock.t += 4
        if self.on_generate:
            self.on_generate(len(self.calls))
        if not self.answer:
            return None
        return int(self.rate() * 4), 4.0

    def generate_prompt(self, model):
        self.pcalls += 1
        self.clock.t += 2
        if not self.answer:
            return None
        core = self.levels()[2]
        ptps = self.core_ptps if core and self.core_ptps is not None else self.stock_ptps
        return 2000, 2000.0 / ptps


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
                       out=self.out.append, check_seconds=60, stock_seconds=20, **dict({"pause": lambda s: None}, **kw))

    def dev(self, rel, addr=NAVI_ADDR):
        return open(os.path.join(self.tree.root, "bus/pci/devices", addr, rel)).read()

    def cap(self):
        return int(self.dev("hwmon/hwmon3/power1_cap"))

    def mclk(self):
        return T.parse_od(self.dev("pp_od_clk_voltage"))["mclk"][1]

    def sclk(self):
        return T.parse_od(self.dev("pp_od_clk_voltage"))["sclk"][1]

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
        self.assertEqual(T.mclk_target(1000, 1075, 75), 1075)     # the 6900 XT's range stops at +75
        self.assertEqual(T.mclk_target(1000, 1200, 75), 1075)     # +75 is the most that may be asked
        self.assertEqual(T.mclk_target(1000, 1200, 25), 1025)
        self.assertEqual(T.mclk_target(1000, 1200, 500), 1075)    # a state file asking for more is clamped
        self.assertEqual(T.mclk_target(1000, 1050, 75), 1050)     # the card's own range wins
        self.assertEqual(T.mclk_target(1000, 1200, 0), None)      # 0 = off: no target
        self.assertEqual(T.mclk_target(1000, 1200, -5), None)
        self.assertEqual(T.mclk_target(1000, 1000, 75), None)     # nothing above stock
        self.assertEqual(T.mclk_target(1100, 1075, 75), None)     # stock already past the range: never lowered
        self.assertEqual(T.mclk_target(None, 1075, 75), None)
        self.assertEqual(T.mclk_target(2660, 3150, 150, T.CORE_MAX_MHZ), 2810)    # the core clock: +150 at most
        self.assertEqual(T.mclk_target(2660, 3150, 900, T.CORE_MAX_MHZ), 2810)
        self.assertEqual(T.mclk_target(2660, 2700, 150, T.CORE_MAX_MHZ), 2700)

    def test_the_command_line_values(self):
        self.assertEqual(T.parse_memory("0"), 0)
        self.assertEqual(T.parse_memory("75"), 75)
        self.assertEqual(T.parse_core("150"), 150)
        for bad in ("76", "-1", "x", "", "1.5", None):
            with self.assertRaises(ValueError):
                T.parse_memory(bad)
        for bad in ("151", "-1", "x"):
            with self.assertRaises(ValueError):
                T.parse_core(bad)
        self.assertEqual(T.clamp_mhz(True, 75), 0)
        self.assertEqual(T.clamp_mhz("75", 75), 0)

    def test_the_core_table(self):
        od = T.parse_od(NAVI21_OD)
        self.assertEqual(od["sclk"], {0: 500, 1: 2660})
        self.assertTrue(T.od_core_usable(od))
        self.assertFalse(T.od_core_usable(T.parse_od(VEGA_OD)))
        self.assertFalse(T.od_core_usable(T.parse_od("OD_MCLK:\n0: 97Mhz\n1: 1000MHz\n")))
        self.assertFalse(T.od_core_usable(None))

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
    def test_on_sets_only_the_power_limit_and_the_memory_clock_stays_at_stock(self):
        self.tree.navi21()
        t = self.tuner()
        t.cmd_on()
        self.assertEqual(self.cap(), 293 * W)                 # power1_cap_max
        self.assertEqual(self.mclk(), 1000)                   # the memory clock is opt-in (6b420): untouched
        self.assertEqual(self.sclk(), 2660)
        self.assertEqual(self.dev(T.LEVEL_FILE).strip(), "auto")
        for name, v in self.driver.card_writes():
            if name == "power1_cap":
                self.assertLessEqual(int(v), 293 * W)          # never a value above the card's maximum
        self.assertEqual([w for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage" and w[1][0] in "ms"], [])
        st = self.state()
        self.assertEqual(st["wanted"], "on")
        self.assertEqual(st["version"], T.STATE_VERSION)
        self.assertEqual((st["memory"], st["core"]), (0, 0))
        self.assertEqual(st["stock"], {"mclk": 1000, "sclk": 2660, "power_uw": 255 * W})
        self.assertEqual(st["applied"]["mclk"], None)
        self.assertEqual(st["applied"]["power_uw"], 293 * W)
        self.assertEqual(st["check"]["result"], "passed")
        self.assertEqual(st["check"]["part"], "power")
        self.assertEqual(st["measure"]["stock_tps"], 40.0)
        self.assertEqual(st["check"]["tps"], 42.0)
        self.assertEqual(st["checked_parts"], {"power": 293 * W})
        self.assertGreaterEqual(len(self.ollama.calls), 15)    # 20 s of stock, then 60 s of load
        self.assertIn("+5.0%", st["check"]["note"])
        self.assertTrue(any("power limit 293 W (maximum)" in l for l in self.out), self.out)
        self.assertFalse(any("memory" in l.lower() and "clock" in l.lower() for l in self.out), self.out)   # not asked: not mentioned

    def test_memory_is_opt_in_and_clamped(self):
        self.tree.navi21()
        self.tuner().cmd_on(memory=75)
        self.assertEqual(self.mclk(), 1075)                   # stock 1000 + 75, the card's own range ends there too
        st = self.state()
        self.assertEqual(st["memory"], 75)
        self.assertEqual(st["applied"]["mclk"], 1075)
        self.assertEqual(st["checked_parts"], {"power": 293 * W, "memory": 75})
        self.assertEqual(self.cap(), 293 * W)

    def test_a_smaller_memory_ask_is_set_as_asked(self):
        self.tree.navi21(od=NAVI21_OD.replace("1075Mhz", "1300Mhz"))
        self.tuner().cmd_on(memory=25)
        self.assertEqual(self.mclk(), 1025)

    def test_a_state_file_asking_for_more_than_allowed_is_clamped(self):
        self.tree.navi21(od=NAVI21_OD.replace("1075Mhz", "1300Mhz"))
        self.tuner().cmd_on()
        st = self.state()
        st["memory"] = 900
        with open(T.state_path(), "w") as f:
            json.dump(st, f)
        self.tuner().cmd_apply()
        self.assertEqual(self.mclk(), 1075)                    # never past +75, however it got into the file

    def test_core_is_opt_in_and_set_with_the_overdrive_table(self):
        self.tree.navi21()
        self.tuner().cmd_on(core=50)
        self.assertEqual(self.sclk(), 2710)
        self.assertEqual(self.mclk(), 1000)
        st = self.state()
        self.assertEqual(st["core"], 50)
        self.assertEqual(st["applied"]["sclk"], 2710)
        self.assertEqual(st["stock"]["sclk"], 2660)
        self.assertEqual(st["checked_parts"], {"power": 293 * W, "core": 50})
        # the write sequence: set the top state, then commit
        seq = [w[1] for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage"]
        i = seq.index("s 1 2710")
        self.assertEqual(seq[i + 1], "c")
        self.assertTrue(any("core +50 MHz (top 2660 -> 2710)" in l for l in self.out), self.out)
        self.assertFalse(any("memory" in l.lower() and "clock" in l.lower() for l in self.out), self.out)

    def test_core_is_clamped_to_150_and_to_the_cards_range(self):
        self.tree.navi21(od=NAVI21_OD.replace("3150Mhz", "2700Mhz"))
        self.tuner().cmd_on(core=150)
        self.assertEqual(self.sclk(), 2700)                    # the OD range's maximum
        st = self.state()
        st["core"] = 9999
        with open(T.state_path(), "w") as f:
            json.dump(st, f)
        self.tree.navi21(od=NAVI21_OD)
        self.tuner().cmd_apply()
        self.assertEqual(self.sclk(), 2810)                    # +150 and no more

    def test_memory_and_core_together(self):
        self.tree.navi21()
        self.tuner().cmd_on(memory=50, core=100)
        self.assertEqual((self.mclk(), self.sclk()), (1050, 2760))
        self.assertEqual(self.state()["checked_parts"], {"power": 293 * W, "memory": 50, "core": 100})

    def test_a_wider_range_does_not_widen_the_ask(self):
        self.tree.navi21(od=NAVI21_OD.replace("1075Mhz", "1300Mhz"))
        self.tuner().cmd_on()
        self.assertEqual(self.mclk(), 1000)

    def test_a_range_that_allows_nothing_more(self):
        self.tree.navi21(od=NAVI21_OD.replace("1075Mhz", "1000Mhz"))
        t = self.tuner()
        t.cmd_on(memory=75)
        self.assertEqual(self.mclk(), 1000)
        self.assertEqual([w for w in self.driver.writes if w[0] == "pp_od_clk_voltage" and w[1].startswith("m 1")], [])
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
        self.tuner(driver=FakeDriver(need_manual=True)).cmd_on(memory=75)
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
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))
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
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))
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
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))

    def test_no_model_checks_later(self):
        self.tree.navi21()
        t = self.tuner(ollama=FakeOllama(self.clock, self.tree, models=()))
        t.cmd_on()
        self.assertEqual(self.mclk(), 1000)
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


NVME_TIMEOUT = "nvme nvme0: I/O 713 QID 4 timeout, aborting"


class ScriptedOllama(FakeOllama):
    """FakeOllama whose speed follows a script: tps_fn(tuned, n) with n the 1-based answer count. The
    first 5 answers are the stock measurement (20 s), 6..20 the first tuned load (60 s), then the
    repeats: stock 21..25, tuned 26..30, stock 31..35, tuned 36..40."""

    def __init__(self, clock, tree, tps_fn, loaded_fn=None, **kw):
        super().__init__(clock, tree, **kw)
        self.tps_fn = tps_fn
        self.loaded_fn = loaded_fn

    def loaded(self):
        return self.loaded_fn(len(self.calls)) if self.loaded_fn else []

    def generate(self, model):
        self.calls.append(model)
        n = len(self.calls)
        self.clock.t += 4
        if self.on_generate:
            self.on_generate(n)
        tps = self.tps_fn(any(self.levels()), n)
        if tps is None:
            return None
        return int(tps * 4), 4.0


def incident(tuned, n):
    """What was seen: stock 249.5, tuned 148.5."""
    return 148.5 if tuned else 249.5


class TestSlowdownIsNotReverted(Base):
    """6b372: "answers were slower than stock (148.5 against 249.5 tokens/s)" reverted a card that was
    fine, while a drive and an engine were failing. A slow figure now needs repeating, in clean conditions."""

    def run_on(self, tps_fn, klog=(), loaded_fn=None, on_generate=None, busy=None):
        self.tree.navi21()
        if busy is not None:
            self.tree.write("bus/pci/devices/%s/gpu_busy_percent" % NAVI_ADDR, "%d\n" % busy)
        self.klog.extend(klog)
        self.ollama = ScriptedOllama(self.clock, self.tree, tps_fn, loaded_fn, on_generate=on_generate)
        t = self.tuner(ollama=self.ollama)
        t.cmd_on()
        return t

    def tuned_values_kept(self):
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))

    def test_a_failing_drive_during_the_measurement_defers_instead_of_reverting(self):
        t = self.run_on(incident, klog=[NVME_TIMEOUT])
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertEqual(st["check"]["result"], "deferred")
        self.assertIn("drive error", st["check"]["note"])
        self.assertIn("nvme0", st["check"]["note"])
        self.assertIn("nothing was changed", st["check"]["note"])
        self.assertIn("148.5", st["check"]["note"])
        self.assertTrue(t.pending())                  # not marked as checked: it runs again next boot
        self.assertNotIn("checked", st)
        self.tuned_values_kept()

    def test_a_deferred_check_runs_again_and_a_clean_one_passes(self):
        self.run_on(incident, klog=[NVME_TIMEOUT])
        self.klog.clear()                              # the drive is fine at the next boot
        self.boot = "bootc"
        self.tree.navi21()
        self.tuner(ollama=ScriptedOllama(self.clock, self.tree, lambda tuned, n: 42.0 if tuned else 40.0)).cmd_restore()
        self.state()
        t = self.tuner(ollama=ScriptedOllama(self.clock, self.tree, lambda tuned, n: 42.0 if tuned else 40.0))
        t.cmd_check_pending()
        self.assertEqual(self.state()["check"]["result"], "passed")
        self.assertFalse(t.pending())

    def test_a_slow_reading_that_does_not_repeat_passes(self):
        # slow only in the first tuned load: n 6..20
        t = self.run_on(lambda tuned, n: (148.5 if 6 <= n <= 20 else 250.0) if tuned else 249.5)
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertEqual(st["check"]["result"], "passed")
        self.assertIn("didn't repeat", st["check"]["note"])
        self.assertIn("median tuned", st["check"]["note"])
        self.assertFalse(t.pending())
        self.tuned_values_kept()

    def test_a_slowdown_that_repeats_reverts_and_says_what_was_measured(self):
        self.run_on(incident)
        st = self.state()
        why = st["reverted"]
        self.assertIn("slower than stock in 2 of 2 repeats", why)
        self.assertIn("median tuned 148.5 against median stock 249.5", why)
        self.assertIn("tuned 148.5/148.5/148.5", why)
        self.assertEqual(st["check"]["result"], "reverted")
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))

    def test_the_repeats_alternate_stock_and_tuned(self):
        self.run_on(incident)
        raised = [w for w in self.driver.card_writes() if w[0] == "power1_cap" and int(w[1]) == 293 * W]
        self.assertGreaterEqual(len(raised), 3)        # tuned set: first, then once per alternation
        stock = [w for w in self.driver.card_writes() if w[0] == "power1_cap" and int(w[1]) == 255 * W]
        self.assertGreaterEqual(len(stock), 3)         # stock before each repeat (and the final revert)
        self.assertGreaterEqual(len(self.ollama.calls), 5 + 15 + 4 * 5)   # stock, first load, 2 x (stock + tuned)

    def test_one_slow_alternation_of_two_is_not_enough(self):
        # second alternation's tuned run (36..40) is fine
        self.run_on(lambda tuned, n: (148.5 if n <= 30 else 250.0) if tuned else 249.5)
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertEqual(st["check"]["result"], "passed")

    def test_another_model_loading_makes_the_reading_unreliable(self):
        t = self.run_on(incident, loaded_fn=lambda n: [{"name": "llama3:8b"}] if n >= 10 else [])
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertEqual(st["check"]["result"], "deferred")
        self.assertIn("another model was loaded: llama3:8b", st["check"]["note"])
        self.assertTrue(t.pending())
        self.tuned_values_kept()

    def test_a_model_that_was_already_resident_is_not_noise(self):
        self.run_on(incident, loaded_fn=lambda n: [{"name": "nomic-embed-text"}])
        self.assertIn("slower than stock in 2 of 2", self.state()["reverted"])

    def test_a_card_busy_with_something_else_defers(self):
        t = self.run_on(incident, busy=85)
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertEqual(st["check"]["result"], "deferred")
        self.assertIn("wasn't quiet (the card is 85% busy)", st["check"]["note"])
        self.assertTrue(t.pending())
        self.assertEqual(self.ollama.calls, [])                  # nothing was measured while it was busy
        self.tuned_values_kept()                                  # the power limit is harmless and stays

    def test_an_idle_card_is_no_noise(self):
        self.run_on(incident, busy=2)
        self.assertIn("slower than stock in 2 of 2", self.state()["reverted"])

    def test_an_answer_that_fails_partway_defers(self):
        # the engine falls over in the first tuned load, after a slow answer
        def fn(tuned, n):
            if n == 12:
                return None
            return incident(tuned, n)
        self.run_on(fn)
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertIn(st["check"]["result"], ("deferred", "passed"))
        if st["check"]["result"] == "deferred":
            self.assertIn("failed partway", st["check"]["note"])

    def test_a_real_amdgpu_error_in_a_repeat_still_reverts_at_once(self):
        def hang(n):
            if n == 27:                                # during the first tuned repeat
                self.klog.append(RING_TIMEOUT)
        self.run_on(incident, on_generate=hang)
        st = self.state()
        self.assertIn("ring gfx_0.0.0 timeout", st["reverted"])
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))
        self.assertEqual(len(self.ollama.calls), 27)   # stopped there: no further answer was asked for

    def test_a_real_amdgpu_error_in_a_stock_repeat_reverts_at_once_too(self):
        def hang(n):
            if n == 22:                                # during the first stock repeat
                self.klog.append(RING_TIMEOUT)
        self.run_on(incident, on_generate=hang)
        self.assertIn("ring gfx_0.0.0 timeout", self.state()["reverted"])
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))
        self.assertEqual(len(self.ollama.calls), 22)   # stopped there: no further answer was asked for

    def test_an_amdgpu_error_in_the_first_load_is_not_softened_by_nvme_noise(self):
        def hang(n):
            if n == 10:
                self.klog.append(RING_TIMEOUT)
        self.run_on(incident, klog=[NVME_TIMEOUT], on_generate=hang)
        self.assertIn("ring gfx_0.0.0 timeout", self.state()["reverted"])

    def test_ctrl_c_during_the_repeats_puts_the_card_back(self):
        def stop(n):
            if n == 27:
                raise KeyboardInterrupt
        self.tree.navi21()
        self.ollama = ScriptedOllama(self.clock, self.tree, incident, on_generate=stop)
        with self.assertRaises(KeyboardInterrupt):
            self.tuner(ollama=self.ollama).cmd_on()
        self.assertIn("stopped before it ended", self.state()["reverted"])
        self.assertEqual((self.cap(), self.mclk()), (255 * W, 1000))

    def test_a_fast_reading_never_starts_the_repeats(self):
        self.run_on(lambda tuned, n: 42.0 if tuned else 40.0)
        self.assertEqual(self.state()["check"]["result"], "passed")
        self.assertEqual(len(self.ollama.calls), 5 + 15)   # stock measurement and one load, nothing more

    def test_nvme_error_lines(self):
        bad = ["nvme nvme0: I/O 713 QID 4 timeout, aborting",
               "nvme nvme0: controller is down; will reset: CSTS=0xffffffff, PCI_STATUS=0xffff",
               "blk_update_request: I/O error, dev nvme0n1, sector 12345 op 0x0:(READ)",
               "Buffer I/O error on dev nvme0n1p2, logical block 9, async page read",
               "nvme nvme1: Device not ready; aborting reset, CSTS=0x1"]
        for line in bad:
            self.assertEqual(len(T.nvme_errors([line])), 1, line)
        good = ["nvme nvme0: pci function 0000:01:00.0", "nvme nvme0: 16/0/0 default/read/poll queues",
                "amdgpu 0000:0b:00.0: amdgpu: SMU is initialized successfully!", "EXT4-fs (nvme0n1p2): mounted filesystem"]
        for line in good:
            self.assertEqual(T.nvme_errors([line]), [], line)
        self.assertEqual(T.nvme_errors(None), [])


class TestSetupChoice(Base):
    def test_first_run_turns_it_on(self):
        self.tree.navi21()
        self.tuner().cmd_setup("on")
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))
        self.assertEqual(self.sclk(), 2660)

    def test_setup_passes_the_clock_asks_on(self):
        self.tree.navi21()
        self.tuner().cmd_setup("on", memory=50, core=100)
        self.assertEqual((self.mclk(), self.sclk()), (1050, 2760))
        t = self.tuner()
        t.cmd_setup("on", memory=25)                           # a re-run asks for less: the saved choice changes
        self.assertEqual(t.st["memory"], 25)
        self.assertEqual(self.state()["memory"], 25)
        self.assertEqual(self.mclk(), 1025)

    def test_an_admins_off_is_kept(self):
        self.tree.navi21()
        self.tuner().cmd_off()
        self.tuner().cmd_setup("on")
        self.assertEqual(self.driver.card_writes(), [])
        self.tuner().cmd_setup("force-on")
        self.assertEqual(self.cap(), 293 * W)

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
        self.assertIn("Memory clock: now 1000 (2000 MHz effective); stock 1000; the card allows up to 1075; raise off (on --memory N)", lines)
        self.assertIn("Core clock: now 2660; stock 2660; the card allows up to 3150; raise off (on --core N)", lines)
        self.assertIn("Now: power 293 W, memory stock, core stock", lines)
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


class TestParts(Base):
    """6b420: the power limit alone by default; the memory and core clocks opt-in, each checked and reverted on
    its own (the power limit kept); a real error or heat reverts everything; nothing experimental is left on
    unverified."""

    def go(self, memory=None, core=None, ollama=None, pre=None, **kw):
        self.tree.navi21(od=NAVI21_OD.replace("3150Mhz", "3150Mhz"))
        if pre:
            pre()
        self.ollama = ollama or FakeOllama(self.clock, self.tree, **kw)
        t = self.tuner(ollama=self.ollama)
        t.cmd_on(memory=memory, core=core)
        return t

    def values(self):
        return (self.cap(), self.mclk(), self.sclk())

    # -- the default and the memory part --
    def test_default_on_never_writes_the_memory_or_core_clock(self):
        self.go()
        self.assertEqual(self.values(), (293 * W, 1000, 2660))
        self.assertEqual([w for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage" and w[1][0] in "ms"], [])
        for cmd in ("cmd_restore", "cmd_apply"):
            getattr(self.tuner(), cmd)()
        self.assertEqual([w for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage" and w[1][0] in "ms"], [])

    def test_memory_slower_reverts_only_the_memory_and_keeps_the_power_limit(self):
        # the real card: power alone is as fast as stock, the memory bump makes it 2.4 times slower
        t = self.go(memory=25, stock_tps=248.0, tuned_tps=247.0, mem_tps=103.0)
        st = self.state()
        self.assertNotIn("reverted", st)                                   # not everything
        self.assertEqual(st["wanted"], "on")
        self.assertIn("slower than power-only in 2 of 2 repeats", st["memory_reverted"])
        self.assertIn("median tuned 103.0 against median power-only 247.0", st["memory_reverted"])
        self.assertEqual(self.values(), (293 * W, 1000, 2660))             # power kept, memory at stock
        self.assertTrue(any("memory +25 MHz was slower, back to stock; power limit kept" in l for l in self.out), self.out)
        self.assertEqual(st["checked_parts"], {"power": 293 * W})
        self.assertEqual(st["check"]["result"], "part-reverted")
        self.assertEqual(self.mclk(), 1000)
        self.assertEqual(self.state()["applied"]["mclk"], None)
        self.assertFalse(t.pending())

    def test_a_reverted_memory_stays_off_across_a_reboot_and_until_asked_again(self):
        self.go(memory=25, stock_tps=248.0, tuned_tps=247.0, mem_tps=103.0)
        self.tree.navi21()
        self.boot = "bootc"
        self.tuner().cmd_restore()
        self.assertEqual(self.values(), (293 * W, 1000, 2660))
        self.driver.writes.clear()
        t = self.tuner()
        t.cmd_check_pending()
        self.assertEqual(self.mclk(), 1000)
        t = self.tuner(ollama=FakeOllama(self.clock, self.tree, stock_tps=248.0, tuned_tps=247.0))   # a fast one now
        t.cmd_on(memory=25)                                               # asked again: tried again
        self.assertNotIn("memory_reverted", self.state())
        self.assertEqual(self.mclk(), 1025)

    def test_memory_that_is_fine_is_kept(self):
        self.go(memory=25, stock_tps=248.0, tuned_tps=247.0, mem_tps=250.0)
        self.assertEqual(self.values(), (293 * W, 1025, 2660))
        self.assertEqual(self.state()["checked_parts"], {"power": 293 * W, "memory": 25})

    def test_a_power_raise_that_is_slower_still_reverts_everything(self):
        self.go(memory=25, stock_tps=248.0, tuned_tps=150.0)
        st = self.state()
        self.assertIn("slower than stock in 2 of 2 repeats", st["reverted"])
        self.assertEqual(self.values(), (255 * W, 1000, 2660))
        self.assertNotIn("memory_reverted", st)                            # the memory was never tried on top of it
        self.assertEqual([w for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage" and w[1].startswith("m 1")], [])

    # -- the core part --
    def test_core_answers_slower_reverts_only_the_core(self):
        self.go(memory=25, core=100, stock_tps=248.0, tuned_tps=247.0, mem_tps=247.0, core_tps=120.0)
        st = self.state()
        self.assertNotIn("reverted", st)
        self.assertIn("answers were slower than without the core raise in 2 of 2", st["core_reverted"])
        self.assertEqual(self.values(), (293 * W, 1025, 2660))             # power and memory kept, core at stock
        self.assertEqual(st["checked_parts"], {"power": 293 * W, "memory": 25})
        self.assertTrue(any("core +100 MHz was slower, back to stock; power limit kept" in l for l in self.out), self.out)

    def test_core_prompt_reading_slower_reverts_only_the_core(self):
        self.go(core=50, stock_ptps=900.0, core_ptps=500.0, stock_tps=100.0, tuned_tps=100.0)
        st = self.state()
        self.assertIn("prompt reading were slower than without the core raise", st["core_reverted"])
        self.assertNotIn("answers were slower", st["core_reverted"])
        self.assertEqual(self.values(), (293 * W, 1000, 2660))

    def test_core_that_helps_prompt_reading_is_kept(self):
        self.go(core=50, stock_ptps=800.0, core_ptps=1000.0, stock_tps=100.0, tuned_tps=100.0)
        self.assertEqual(self.values(), (293 * W, 1000, 2710))
        self.assertGreaterEqual(self.ollama.pcalls, 4)                     # prompt reading was measured
        self.assertEqual(self.state()["checked_parts"], {"power": 293 * W, "core": 50})
        self.assertIn("prompt reading", self.state()["check"]["note"])

    def test_the_core_junction_limit_is_100_not_105(self):
        def hot(n):
            if n > 25:                                                    # in the core check (the power check ends at 20)
                self.tree.temp("junction", 100)
        self.go(core=50, ollama=FakeOllama(self.clock, self.tree, on_generate=hot))
        st = self.state()
        self.assertIn("junction reached 100 C", st["reverted"])          # heat reverts everything
        self.assertEqual(self.values(), (255 * W, 1000, 2660))
        self.tree.navi21()
        self.tuner().cmd_off()
        # the same 100 C without the core raised is below the 105 C limit
        self.setUp()
        self.go(ollama=FakeOllama(self.clock, self.tree, on_generate=lambda n: self.tree.temp("junction", 100) if n > 8 else None))
        self.assertNotIn("reverted", self.state())
        self.assertEqual(self.cap(), 293 * W)

    def test_a_kernel_error_during_the_core_check_reverts_everything(self):
        def hang(n):
            if n == 28:
                self.klog.append(RING_TIMEOUT)
        self.go(memory=25, core=50, ollama=FakeOllama(self.clock, self.tree, on_generate=hang, mem_tps=42.0))
        st = self.state()
        self.assertIn("ring gfx_0.0.0 timeout", st["reverted"])
        self.assertEqual(self.values(), (255 * W, 1000, 2660))

    def test_a_kernel_error_during_the_memory_check_reverts_everything(self):
        def hang(n):
            if n == 25:
                self.klog.append(RING_TIMEOUT)
        self.go(memory=25, ollama=FakeOllama(self.clock, self.tree, on_generate=hang))
        self.assertIn("ring gfx_0.0.0 timeout", self.state()["reverted"])
        self.assertEqual(self.values(), (255 * W, 1000, 2660))
        self.assertNotIn("memory_reverted", self.state())

    def test_the_overdrive_write_sequence_for_the_core(self):
        self.go(core=100)
        ods = [w[1] for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage"]
        i = ods.index("s 1 2760")
        self.assertEqual(ods[i + 1], "c")                                  # set the top state, then commit
        self.assertNotIn("m 1", " ".join(ods))
        # read back: the table now says so
        self.assertEqual(self.sclk(), 2760)

    # -- nothing experimental is left on unverified --
    def test_a_deferred_clock_check_leaves_the_clock_at_stock(self):
        # the report: a check that "wasn't clean" left the memory bump live
        self.go()                                                          # the power check passed, clean
        self.tree.write("bus/pci/devices/%s/gpu_busy_percent" % NAVI_ADDR, "99\n")
        t = self.tuner(ollama=FakeOllama(self.clock, self.tree))
        n = len(t.ollama.calls)
        t.cmd_setup("on", core=150)
        self.assertEqual(self.values(), (293 * W, 1000, 2660))             # core not applied; power kept
        self.assertEqual(len(t.ollama.calls), n)                           # nothing measured while it was busy
        self.assertTrue(any("core +150 not applied yet: the card wasn't quiet (the card is 99% busy); run it again "
                            "when the server is idle" in l for l in self.out), self.out)
        self.assertTrue(t.pending())
        self.assertEqual([w for w in self.driver.card_writes() if w[0] == "pp_od_clk_voltage" and w[1].startswith("s 1")], [])
        # at the next boot it is not applied either, only checked when quiet
        self.tree.navi21()
        self.boot = "bootc"
        self.tuner().cmd_restore()
        self.assertEqual(self.values(), (293 * W, 1000, 2660))
        self.tree.write("bus/pci/devices/%s/gpu_busy_percent" % NAVI_ADDR, "2\n")
        self.tuner(ollama=FakeOllama(self.clock, self.tree, stock_ptps=800.0, core_ptps=800.0)).cmd_check_pending()
        self.assertEqual(self.sclk(), 2810)                                # quiet now, and it checked out
        self.assertEqual(self.state()["checked_parts"]["core"], 150)

    def test_an_unclean_check_drops_the_clock_that_was_applied_for_it(self):
        def noisy(n):
            if n == 25:
                self.klog.append(NVME_TIMEOUT)                            # in the core check
        self.go(core=100, ollama=FakeOllama(self.clock, self.tree, on_generate=noisy))
        st = self.state()
        self.assertEqual(st["check"]["result"], "deferred")
        self.assertNotIn("reverted", st)
        self.assertEqual(self.values(), (293 * W, 1000, 2660))             # the core went back to stock
        self.assertEqual(st["applied"]["sclk"], None)
        self.assertTrue(any("core +100 not applied yet" in l and "run it again when the server is idle" in l for l in self.out), self.out)

    def test_no_answer_or_no_load_does_not_leave_a_clock_on(self):
        self.go(core=100, ollama=FakeOllama(self.clock, self.tree, models=()))
        self.assertEqual(self.values(), (293 * W, 1000, 2660))
        self.go_again = None

    def test_boot_applies_an_experimental_clock_only_after_its_check_passed(self):
        self.go(memory=25, stock_tps=248.0, tuned_tps=247.0, mem_tps=250.0)
        self.assertEqual(self.mclk(), 1025)
        self.tree.navi21()
        self.boot = "bootc"
        self.tuner().cmd_restore()
        self.assertEqual(self.values(), (293 * W, 1025, 2660))             # passed: set again at boot, like power
        st = self.state()
        st["checked_parts"].pop("memory")                                  # not proven (say, the state was edited)
        with open(T.state_path(), "w") as f:
            json.dump(st, f)
        self.tree.navi21()
        self.tuner().cmd_restore()
        self.assertEqual(self.values(), (293 * W, 1000, 2660))             # unproven: not applied before its check

    # -- waiting for a quiet card --
    def test_it_waits_for_the_card_to_be_quiet_and_says_so(self):
        self.tree.navi21()
        busy = os.path.join(self.tree.root, "bus/pci/devices", NAVI_ADDR, "gpu_busy_percent")
        self.tree.write("bus/pci/devices/%s/gpu_busy_percent" % NAVI_ADDR, "99\n")
        pauses = []

        def pause(sec):
            pauses.append(sec)
            if len(pauses) == 3:
                open(busy, "w").write("1\n")
        self.ollama = FakeOllama(self.clock, self.tree)
        t = self.tuner(ollama=self.ollama, pause=pause)
        t.cmd_on()
        self.assertEqual(len([x for x in pauses if x == 2]), 4)            # 3 busy looks + 1 settling before the stock speed, 1 before the check
        self.assertTrue(any("waiting up to 60 s for the card to be quiet: the card is 99% busy" in l for l in self.out), self.out)
        self.assertEqual(self.state()["check"]["result"], "passed")

    def test_a_card_that_never_gets_quiet_defers_and_applies_nothing_experimental(self):
        self.tree.navi21()
        self.tree.write("bus/pci/devices/%s/gpu_busy_percent" % NAVI_ADDR, "99\n")
        pauses = []
        t = self.tuner(pause=pauses.append)
        t.cmd_on(memory=75, core=150)
        self.assertEqual(len([x for x in pauses if x == 2]), 2 * T.QUIET_POLLS)   # stock speed's wait, then the check's: 60 s each
        self.assertEqual(self.values(), (293 * W, 1000, 2660))
        self.assertEqual(self.ollama.calls, [])
        st = self.state()
        self.assertEqual(st["check"]["result"], "deferred")
        self.assertIn("wasn't quiet", st["check"]["note"])
        self.assertNotIn("reverted", st)
        self.assertTrue(any("stock speed not measured" in l for l in self.out))

    def test_another_model_loading_is_not_quiet(self):
        self.tree.navi21()
        seq = iter([[], [{"name": "gemma4:12b"}]] + [[{"name": "gemma4:12b"}]] * 50)

        class Loading(FakeOllama):
            def loaded(inner):
                return next(seq)
        self.ollama = Loading(self.clock, self.tree)
        pauses = []
        t = self.tuner(ollama=self.ollama, pause=pauses.append)
        quiet, why = t.wait_quiet(type("C", (), {"dev": os.path.join(self.tree.root, "bus/pci/devices", NAVI_ADDR)})(), "qwen3:14b")
        self.assertTrue(quiet)                                             # it settled: loaded once, then stable
        self.assertGreaterEqual(len([x for x in pauses if x == 2]), 2)
        self.assertTrue(any("another model is loading (gemma4:12b)" in l for l in self.out), self.out)


class TestOldState(Base):
    """A server that already has the old state file (a revert recorded by the version that bumped the memory clock)."""
    OLD = {"wanted": "on", "reverted": "answers were slower than stock (tuned 108.5/103.8/102.2 against stock 248.3/247.8 tokens/s, 2 of 2 repeats)",
           "reverted_at": 1700000000, "stock": {"mclk": 1000, "power_uw": 255 * W},
           "applied": {"power_uw": None, "mclk": None, "at": 1700000000, "boot": "boota"},
           "check": {"result": "reverted", "note": "x", "at": 1700000000}, "checked": [293 * W, 1075],
           "measure": {"model": "qwen3:14b", "stock_tps": 248.0, "at": 1700000000}}

    def write_old(self, extra=None):
        d = dict(self.OLD)
        d.update(extra or {})
        os.makedirs(os.path.dirname(T.state_path()), exist_ok=True)
        with open(T.state_path(), "w") as f:
            json.dump(d, f)

    def test_the_old_revert_is_dropped_and_power_alone_is_applied_at_boot(self):
        self.tree.navi21()
        self.write_old()
        t = self.tuner()
        self.assertNotIn("reverted", t.st)
        self.assertEqual((t.st["version"], t.st["memory"], t.st["core"]), (T.STATE_VERSION, 0, 0))
        self.assertIn("old_revert", t.st["migrated"])
        self.boot = "bootb"
        t.cmd_restore()
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))       # the new default: power only
        st = self.state()                                                  # and the file is migrated on disk
        self.assertEqual(st["version"], T.STATE_VERSION)
        self.assertNotIn("reverted", st)
        self.assertEqual(st["wanted"], "on")
        self.assertEqual(st["stock"]["mclk"], 1000)                        # the stock figures survive
        self.assertEqual(st["measure"]["stock_tps"], 248.0)
        self.assertNotIn("checked", st)
        # and its check is due again (the power limit, on the new rules)
        t2 = self.tuner()
        self.assertTrue(t2.pending())
        t2.cmd_check_pending()
        self.assertEqual(self.state()["check"]["result"], "passed")

    def test_an_old_off_is_kept(self):
        self.tree.navi21()
        self.write_old({"wanted": "off", "reverted": None})
        t = self.tuner()
        t.cmd_restore()
        self.assertEqual(self.driver.card_writes(), [])
        self.assertEqual(self.state()["wanted"], "off")

    def test_an_old_state_that_had_the_memory_bump_applied_takes_it_off(self):
        self.tree.navi21()
        self.write_old({"reverted": None, "reverted_at": None, "applied": {"power_uw": 293 * W, "mclk": 1075,
                                                                           "at": 1700000000, "boot": "bootb"}})
        self.boot = "bootb"
        # the card still holds the old bump
        self.tree.write("bus/pci/devices/%s/pp_od_clk_voltage" % NAVI_ADDR, NAVI21_OD.replace("1: 1000MHz", "1: 1075MHz"))
        self.tuner().cmd_restore()
        self.assertEqual((self.cap(), self.mclk()), (293 * W, 1000))

    def test_status_says_where_the_old_revert_went(self):
        self.tree.navi21()
        self.write_old()
        lines = "\n".join(self.tuner().status_lines())
        self.assertIn("From an older version: its revert", lines)
        self.assertNotIn("kept at stock after", lines)

    def test_a_new_state_is_left_alone(self):
        st = {"version": T.STATE_VERSION, "wanted": "on", "reverted": "a real amdgpu error", "memory": 25}
        self.assertEqual(T.migrate_state(dict(st)), st)
        self.assertEqual(T.migrate_state({}), {})
        self.assertEqual(T.migrate_state(None), None)


class TestCommandClocks(Base):
    def run_bin(self, *args):
        env = dict(os.environ, OLLAMA1_SYS=self.tree.root, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-gpu-tune")] + list(args),
                              capture_output=True, text=True, env=env, timeout=30)

    def test_out_of_range_and_bad_values_are_refused(self):
        for args in (("on", "--memory", "76"), ("on", "--memory", "-1"), ("on", "--core", "151"), ("on", "--core", "x"),
                     ("on", "--memory"), ("on", "--bogus"), ("setup", "on", "--memory", "99"), ("setup", "on", "--core=999")):
            r = self.run_bin(*args)
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
        self.assertFalse(os.path.exists(T.state_path()) and "memory" in open(T.state_path()).read())

    def test_the_options_parse(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("o1tunebin", os.path.join(U.BIN, "ollama1-gpu-tune"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("o1tunebin", loader))
        loader.exec_module(mod)
        self.assertEqual(mod.clock_options(["--memory", "25", "--core=100"]), ({"memory": 25, "core": 100}, []))
        self.assertEqual(mod.clock_options([]), ({}, []))
        self.assertEqual(mod.clock_options(["--memory", "0"]), ({"memory": 0}, []))
        self.assertEqual(mod.clock_options(["x"]), ({}, ["x"]))

    def test_the_usage_mentions_the_options(self):
        r = self.run_bin("help")
        self.assertIn("--memory N", r.stdout)
        self.assertIn("--core N", r.stdout)


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

    def test_the_plan_says_power_only_by_default(self):
        code, out = self.plan("--gpu-tune")
        self.assertEqual(code, 0, out)
        self.assertIn("its highest power limit, memory clock left at stock, core clock left at stock", out)

    def test_the_clock_options_are_in_the_plan_and_saved(self):
        code, out = self.plan("--gpu-tune", "--gpu-tune-memory", "25", "--gpu-tune-core", "50")
        self.assertEqual(code, 0, out)
        self.assertIn("memory clock +25 MHz (opt-in), core clock +50 MHz (opt-in, experimental)", out)
        saved = self.saved_file("GPU_TUNE=on\nGPU_TUNE_MEMORY=75\nGPU_TUNE_CORE=150\n")        # kept by a re-run
        self.assertIn("memory clock +75 MHz (opt-in), core clock +150 MHz", self.plan(SAVED=saved)[1])
        self.assertIn("memory clock +10 MHz (opt-in), core clock +150 MHz", self.plan("--gpu-tune-memory", "10", SAVED=saved)[1])
        self.assertIn("memory clock left at stock", self.plan("--gpu-tune-memory", "0", SAVED=saved)[1])
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn("printf 'GPU_TUNE_MEMORY=%s\\nGPU_TUNE_CORE=%s\\n'", s)       # saved in setup.env
        self.assertIn('"$TUNE" setup "$tune_how" --memory "$GPU_TUNE_MEMORY" --core "$GPU_TUNE_CORE"', s)

    def test_the_clock_options_are_range_checked(self):
        for args in (("--gpu-tune-memory", "76"), ("--gpu-tune-memory", "-1"), ("--gpu-tune-memory", "x"),
                     ("--gpu-tune-core", "151"), ("--gpu-tune-core", "1.5"), ("--gpu-tune-core",), ("--gpu-tune-memory",)):
            code, out = self.plan("--gpu-tune", *args)
            self.assertEqual(code, 2, (args, out))
        for args in (("--gpu-tune-memory", "75"), ("--gpu-tune-core", "150"), ("--gpu-tune-memory", "0")):
            self.assertEqual(self.plan("--gpu-tune", *args)[0], 0)
        code, out = self.plan(SAVED=self.saved_file("GPU_TUNE=on\nGPU_TUNE_CORE=999\n"))
        self.assertNotEqual(code, 0)

    def test_the_help_lists_them(self):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--help"], capture_output=True, text=True, timeout=30)
        self.assertIn("--gpu-tune-memory", r.stdout)
        self.assertIn("--gpu-tune-core", r.stdout)
        self.assertIn("2.4x slower", r.stdout)

    def test_bad_values_are_refused(self):
        code, out = self.plan(OLLAMA1_GPU_TUNE="maybe")
        self.assertEqual(code, 2)
        code, out = self.plan("--gpu-tune", "--no-gpu-tune")
        self.assertEqual(code, 2)
        self.assertIn("give one", out)


if __name__ == "__main__":
    unittest.main()
