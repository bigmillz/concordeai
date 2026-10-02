"""The CPU card's data (lib/o1cpu.py) against fixture /sys and /proc trees:
AMD (k10temp + amd-pstate), Intel (coretemp + intel_pstate), no cpufreq, a
virtual machine with no temperature, a renumbered hwmon, garbage values, a
huge core count, hostile text; then the admin panel's status route and page."""
import json
import os
import shutil
import tempfile
import time
import unittest

import o1test_util as U
import o1cpu
from o1test_util import KIT  # noqa: F401  (keeps the import order: the scratch prefix first)

import importlib.machinery
import importlib.util


def load_admin():
    loader = importlib.machinery.SourceFileLoader("ollama1-admin-cpu", os.path.join(U.BIN, "ollama1-admin"))
    spec = importlib.util.spec_from_loader("ollama1-admin-cpu", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CPUINFO_AMD = """processor\t: 0
model name\t: AMD Ryzen 9 5950X 16-Core Processor
cpu MHz\t\t: 3400.000
physical id\t: 0
core id\t\t: 0

processor\t: 1
model name\t: AMD Ryzen 9 5950X 16-Core Processor
cpu MHz\t\t: 3500.000
physical id\t: 0
core id\t\t: 0

processor\t: 2
model name\t: AMD Ryzen 9 5950X 16-Core Processor
cpu MHz\t\t: 3600.000
physical id\t: 0
core id\t\t: 1

processor\t: 3
model name\t: AMD Ryzen 9 5950X 16-Core Processor
cpu MHz\t\t: 3700.000
physical id\t: 0
core id\t\t: 1
"""


class Tree:
    """A scratch /sys and /proc."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="o1cpu-")
        self.sys = os.path.join(self.root, "sys")
        self.proc = os.path.join(self.root, "proc")
        os.makedirs(self.sys)
        os.makedirs(self.proc)

    def w(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def cpu(self, n, cur_khz=None, mn=400000, mx=4900000, rated=4900000, gov="powersave", drv="amd-pstate-epp"):
        base = "sys/devices/system/cpu/cpu%d" % n
        os.makedirs(os.path.join(self.root, base), exist_ok=True)
        if cur_khz is not None:
            self.w(base + "/cpufreq/scaling_cur_freq", "%s\n" % cur_khz)
            self.w(base + "/cpufreq/scaling_min_freq", "%s\n" % mn)
            self.w(base + "/cpufreq/scaling_max_freq", "%s\n" % mx)
            self.w(base + "/cpufreq/cpuinfo_max_freq", "%s\n" % rated)
            self.w(base + "/cpufreq/scaling_governor", gov + "\n")
            self.w(base + "/cpufreq/scaling_driver", drv + "\n")

    def hwmon(self, num, name, temps):
        """temps: {label: millidegrees}."""
        base = "sys/class/hwmon/hwmon%d" % num
        self.w(base + "/name", name + "\n")
        for i, (label, v) in enumerate(temps.items(), 1):
            self.w(base + "/temp%d_label" % i, label + "\n")
            self.w(base + "/temp%d_input" % i, "%s\n" % v)

    def stat(self, per_core):
        """per_core: [(busy, idle)] jiffies; the 'cpu' line is their sum."""
        lines = ["cpu  %d 0 0 %d 0 0 0 0" % (sum(b for b, _ in per_core), sum(i for _, i in per_core))]
        lines += ["cpu%d %d 0 0 %d 0 0 0 0" % (n, b, i) for n, (b, i) in enumerate(per_core)]
        self.w("proc/stat", "\n".join(lines) + "\nintr 5\nprocs_running 2\n")

    def probe(self, clock=None):
        kw = {"clock": clock} if clock else {}
        return o1cpu.CpuProbe(sys_root=self.sys, proc_root=self.proc, **kw)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.close)

    def amd(self):
        t = self.t
        t.w("proc/cpuinfo", CPUINFO_AMD)
        for n, khz in enumerate((3400000, 3500000, 3600000, 3700000)):
            t.cpu(n, khz)
        t.hwmon(0, "nvme", {"Composite": 41000})
        t.hwmon(1, "amdgpu", {"edge": 50000})
        t.hwmon(7, "k10temp", {"Tctl": 67500, "Tccd1": 62000})
        t.w("proc/loadavg", "1.20 0.90 0.80 3/1204 99999\n")
        t.stat([(100, 900)] * 4)


class TestAmd(Base):
    def test_everything_from_an_amd_machine(self):
        self.amd()
        p = self.t.probe()
        self.t.stat([(200, 1200)] * 4)               # 100 busy of 400 jiffies = 25 %
        s = p.snapshot()
        self.assertEqual(s["model"], "AMD Ryzen 9 5950X 16-Core Processor")
        self.assertEqual((s["cores"], s["threads"]), (2, 4))
        self.assertEqual((s["driver"], s["governor"]), ("amd-pstate-epp", "powersave"))
        f = s["freq"]
        self.assertEqual((f["min_mhz"], f["max_mhz"], f["rated_mhz"]), (400, 4900, 4900))
        self.assertEqual((f["avg_mhz"], f["hi_mhz"], f["lo_mhz"]), (3550, 3700, 3400))
        self.assertEqual(f["cores"], [3400, 3500, 3600, 3700])
        self.assertEqual(s["temp"]["c"], 67.5)
        self.assertEqual(s["temp"]["label"], "Tctl")
        self.assertEqual(s["temp"]["chip"], "k10temp")
        self.assertEqual(s["temp"]["sensors"], {"Tctl": 67.5, "Tccd1": 62.0})
        self.assertEqual(s["util_pct"], 25.0)
        self.assertEqual(s["util_cores"], [25.0] * 4)
        self.assertEqual(s["load"], [1.2, 0.9, 0.8])
        self.assertEqual((s["running"], s["tasks"]), (3, 1204))
        self.assertIsNone(s["power_w"])
        self.assertIsNone(s["throttle"])
        json.dumps(s, allow_nan=False)

    def test_die_temperature_preferred_over_tctl(self):
        self.amd()
        self.t.hwmon(7, "k10temp", {"Tctl": 95000, "Tdie": 68000, "Tccd1": 62000})
        self.assertEqual(self.t.probe().snapshot()["temp"]["c"], 68.0)
        self.assertEqual(self.t.probe().snapshot()["temp"]["label"], "Tdie")

    def test_hwmon_found_by_name_whatever_its_number(self):
        for num in (0, 3, 12, 57):
            t = Tree()
            try:
                t.hwmon(0 if num else 1, "nvme", {"Composite": 99000})
                t.hwmon(num, "k10temp", {"Tctl": 55000})
                self.assertEqual(t.probe().snapshot()["temp"]["c"], 55.0, num)
            finally:
                t.close()

    def test_peak_is_the_highest_seen(self):
        self.amd()
        p = self.t.probe()
        self.t.hwmon(7, "k10temp", {"Tctl": 80000})
        self.assertEqual(p.snapshot()["temp"]["peak_c"], 80.0)
        self.t.hwmon(7, "k10temp", {"Tctl": 60000})
        s = p.snapshot()
        self.assertEqual((s["temp"]["c"], s["temp"]["peak_c"]), (60.0, 80.0))


class TestIntel(Base):
    def setUp(self):
        super().setUp()
        t = self.t
        t.w("proc/cpuinfo", CPUINFO_AMD.replace("AMD Ryzen 9 5950X 16-Core Processor", "Intel(R) Core(TM) i7-12700K"))
        for n in range(4):
            t.cpu(n, 4000000, drv="intel_pstate", gov="performance")
            t.w("sys/devices/system/cpu/cpu%d/thermal_throttle/core_throttle_count" % n, "0\n")
        t.hwmon(2, "coretemp", {"Package id 0": 71000, "Core 0": 69000, "Core 1": 70000})
        t.stat([(100, 900)] * 4)

    def test_package_temperature(self):
        s = self.t.probe().snapshot()
        self.assertEqual((s["temp"]["c"], s["temp"]["label"], s["temp"]["chip"]), (71.0, "Package id 0", "coretemp"))
        self.assertEqual(s["driver"], "intel_pstate")
        self.assertIn("Intel", s["model"])

    def test_throttle_count_rising_is_said_plainly_then_forgotten(self):
        now = [1000.0]
        p = self.t.probe(clock=lambda: now[0])
        self.assertIsNone(p.snapshot()["throttle"])
        self.t.w("sys/devices/system/cpu/cpu1/thermal_throttle/core_throttle_count", "3\n")
        now[0] += 10
        self.assertIn("heat", p.snapshot()["throttle"])
        now[0] += o1cpu.THROTTLE_MEMORY_S + 1
        self.assertIsNone(p.snapshot()["throttle"])

    def test_busy_and_far_under_top_speed_is_noted(self):
        for n in range(4):
            self.t.cpu(n, 1500000, rated=4900000, drv="intel_pstate")
        p = self.t.probe()
        self.t.stat([(1000, 1000)] * 4)               # 100 % busy
        s = p.snapshot()
        self.assertIn("top speed", s["throttle"])
        for n in range(4):
            self.t.cpu(n, 4500000, rated=4900000, drv="intel_pstate")
        self.t.stat([(2000, 1000)] * 4)
        self.assertIsNone(p.snapshot()["throttle"])    # fast and busy: nothing to say

    def test_idle_and_slow_is_not_a_throttle(self):
        for n in range(4):
            self.t.cpu(n, 800000, rated=4900000, drv="intel_pstate")
        p = self.t.probe()
        self.t.stat([(100, 5000)] * 4)
        self.assertIsNone(p.snapshot()["throttle"])


class TestMissingSources(Base):
    def test_virtual_machine_no_temperature_no_cpufreq(self):
        self.t.w("proc/cpuinfo", "processor\t: 0\nmodel name\t: QEMU Virtual CPU version 2.5+\ncpu MHz\t\t: 2495.998\n\n"
                                  "processor\t: 1\nmodel name\t: QEMU Virtual CPU version 2.5+\ncpu MHz\t\t: 2495.998\n")
        self.t.w("proc/loadavg", "0.10 0.20 0.30 1/80 5\n")
        for n in (0, 1):
            self.t.cpu(n)
        self.t.stat([(1, 9), (1, 9)])
        s = self.t.probe().snapshot()
        self.assertIsNone(s["temp"])
        self.assertIsNone(s["driver"])
        self.assertIsNone(s["power_w"])
        self.assertEqual(s["model"], "QEMU Virtual CPU version 2.5+")
        self.assertEqual(s["threads"], 2)
        self.assertEqual(s["freq"]["cores"], [2496, 2496])        # /proc/cpuinfo steps in
        self.assertEqual(s["freq"]["avg_mhz"], 2496)
        self.assertIsNone(s["freq"]["rated_mhz"])

    def test_nothing_at_all(self):
        s = self.t.probe().snapshot()
        self.assertEqual(s["freq"]["cores"], [])
        self.assertEqual(s["util_cores"], [])
        for k in ("model", "cores", "threads", "driver", "governor", "temp", "util_pct", "load", "power_w", "throttle"):
            self.assertIsNone(s[k], k)
        json.dumps(s, allow_nan=False)

    def test_snapshot_without_cpufreq_but_with_temperature(self):
        self.t.hwmon(4, "k10temp", {"Tctl": 45000})
        s = self.t.probe().snapshot()
        self.assertEqual(s["temp"]["c"], 45.0)
        self.assertIsNone(s["freq"]["avg_mhz"])


class TestGarbage(Base):
    def test_garbage_values_become_blanks(self):
        t = self.t
        t.w("proc/cpuinfo", "processor\t: 0\nmodel name\t: x\ncpu MHz\t\t: nan\n")
        t.cpu(0, "inf")
        t.w("sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq", "-5\n")
        t.w("sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq", "99999999999999\n")
        t.w("sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "garbage\n")
        t.w("sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "<script>alert(1)</script>\n")
        t.w("sys/devices/system/cpu/cpu0/cpufreq/scaling_driver", "x" * 500 + "\n")
        t.hwmon(0, "k10temp", {"Tctl": 99999999999, "Tdie": "nan", "Tccd1": "-500000", "Tccd2": "abc", "Tccd3": ""})
        t.w("proc/loadavg", "nan 1 2 x/y\n")
        t.w("proc/stat", "cpu  a b c\ncpu0 -1 0 0 5\nprocs_running x\n")
        s = o1cpu.CpuProbe(sys_root=t.sys, proc_root=t.proc).snapshot()
        self.assertIsNone(s["temp"])
        self.assertIsNone(s["freq"]["avg_mhz"])
        self.assertIsNone(s["freq"]["min_mhz"])
        self.assertIsNone(s["freq"]["max_mhz"])
        self.assertIsNone(s["freq"]["rated_mhz"])
        self.assertIsNone(s["governor"])
        self.assertIsNone(s["driver"])
        self.assertIsNone(s["load"])
        self.assertIsNone(s["util_pct"])
        json.dumps(s, allow_nan=False)

    def test_a_temperature_out_of_range_is_dropped_not_shown(self):
        self.t.hwmon(0, "k10temp", {"Tctl": 250000})
        self.assertIsNone(self.t.probe().snapshot()["temp"])
        self.t.hwmon(0, "k10temp", {"Tctl": 250000, "Tccd1": 70000})
        s = self.t.probe().snapshot()
        self.assertEqual((s["temp"]["c"], s["temp"]["label"]), (70.0, "Tccd1"))

    def test_number_parser(self):
        self.assertEqual(o1cpu.number("65000\n", -30, 150, 1000.0), 65.0)
        for bad in ("", None, "nan", "inf", "-inf", "1e999", "x", "999999", "-31000"):
            self.assertIsNone(o1cpu.number(bad, -30, 150, 1000.0), bad)

    def test_hostile_model_text(self):
        evil = "<script>alert(1)</script>\x1b[31m\"'`&\\ Ryzen\n\tX " + "A" * 400
        self.t.w("proc/cpuinfo", "processor\t: 0\nmodel name\t: %s\n" % evil)
        m = self.t.probe().snapshot()["model"]
        for ch in "<>&\"'`\\\x1b\n\t":
            self.assertNotIn(ch, m)
        self.assertLessEqual(len(m), o1cpu.MODEL_LIMIT)
        self.assertIn("Ryzen", m)
        self.assertNotIn("<script", json.dumps(self.t.probe().snapshot()))

    def test_planted_symlink_and_fifo_are_not_followed(self):
        t = self.t
        secret = t.w("secret_temp", "55000\n")
        t.hwmon(0, "k10temp", {"Tctl": 40000})
        link = os.path.join(t.sys, "class/hwmon/hwmon0/temp1_input")
        os.unlink(link)
        os.symlink(secret, link)
        self.assertIsNone(t.probe().snapshot()["temp"])
        os.unlink(link)
        os.mkfifo(link)
        t0 = time.time()
        self.assertIsNone(o1cpu.read_text(link))
        self.assertLess(time.time() - t0, 2)
        self.assertIsNone(o1cpu.read_text(t.sys))              # a directory

    def test_reads_are_bounded(self):
        p = self.t.w("big", "1" * 100000)
        self.assertEqual(len(o1cpu.read_text(p)), o1cpu.READ_LIMIT)


class TestUtilisation(Base):
    def test_zero_elapsed_is_a_blank_not_a_crash(self):
        self.assertIsNone(o1cpu.utilisation((10, 100), (10, 100)))
        self.t.stat([(5, 5)])
        p = self.t.probe()
        s = p.snapshot()                                       # same /proc/stat again
        self.assertIsNone(s["util_pct"])
        self.assertEqual(s["util_cores"], [None])

    def test_backwards_counters_and_over_100(self):
        self.assertIsNone(o1cpu.utilisation((50, 100), (40, 150)))
        self.assertIsNone(o1cpu.utilisation((50, 100), (60, 90)))
        self.assertEqual(o1cpu.utilisation((0, 0), (300, 100)), 100.0)    # inconsistent counters: clamped
        self.assertEqual(o1cpu.utilisation((0, 0), (0, 100)), 0.0)
        self.assertIsNone(o1cpu.utilisation(None, (1, 2)))

    def test_core_order_is_numeric(self):
        lines = "cpu  1 0 0 1 0 0 0 0\n" + "".join("cpu%d 1 0 0 1 0 0 0 0\n" % i for i in (0, 1, 2, 10, 11, 3))
        self.t.w("proc/stat", lines)
        p = self.t.probe()
        self.t.w("proc/stat", lines.replace("cpu10 1 0 0 1", "cpu10 51 0 0 1"))
        s = p.snapshot()
        # cpu0 1 2 3 10 11: only cpu10 moved, and it is 5th in numeric order (a text sort would put it 3rd)
        self.assertEqual(s["util_cores"], [None, None, None, None, 100.0, None])

    def test_very_many_cores(self):
        n = 1500
        for i in range(n):
            self.t.cpu(i, 2000000 + i)
        self.t.stat([(1, 9)] * n)
        t0 = time.time()
        p = self.t.probe()
        self.t.stat([(2, 18)] * n)
        s = p.snapshot()
        self.assertLessEqual(len(s["util_cores"]), o1cpu.MAX_CORES)
        self.assertEqual(len(s["freq"]["cores"]), o1cpu.MAX_CORES)
        self.assertEqual(len(s["util_cores"]), o1cpu.MAX_CORES)
        self.assertEqual(s["threads"], n)                      # counted, not all listed
        self.assertLess(time.time() - t0, 10)
        json.dumps(s)


class TestPower(Base):
    def test_rapl_with_a_wrap(self):
        now = [0.0]
        t = self.t
        base = "sys/class/powercap/intel-rapl:0/"
        t.w(base + "max_energy_range_uj", "1000000000\n")
        t.w(base + "energy_uj", "999000000\n")
        p = t.probe(clock=lambda: now[0])
        self.assertIsNone(p.snapshot()["power_w"])             # first look: nothing to subtract from
        now[0] = 10.0
        t.w(base + "energy_uj", "99000000\n")                  # wrapped: 1,000,000 + 99,000,001 uJ in 10 s
        self.assertEqual(p.snapshot()["power_w"], 10.0)

    def test_rapl_absurd_or_unreadable_is_a_blank(self):
        now = [0.0]
        t = self.t
        base = "sys/class/powercap/intel-rapl:0/"
        t.w(base + "energy_uj", "0\n")
        p = t.probe(clock=lambda: now[0])
        now[0] = 1.0
        t.w(base + "energy_uj", "900000000000\n")              # 900 kW
        self.assertIsNone(p.snapshot()["power_w"])
        t.w(base + "energy_uj", "junk\n")
        now[0] = 2.0
        self.assertIsNone(p.snapshot()["power_w"])

    def test_amd_energy_hwmon(self):
        now = [0.0]
        t = self.t
        t.w("sys/class/hwmon/hwmon3/name", "amd_energy\n")
        t.w("sys/class/hwmon/hwmon3/energy1_label", "Ecore000\n")
        t.w("sys/class/hwmon/hwmon3/energy1_input", "5\n")
        t.w("sys/class/hwmon/hwmon3/energy2_label", "Esocket0\n")
        t.w("sys/class/hwmon/hwmon3/energy2_input", "1000000\n")
        p = t.probe(clock=lambda: now[0])
        now[0] = 2.0
        t.w("sys/class/hwmon/hwmon3/energy2_input", "121000000\n")
        self.assertEqual(p.snapshot()["power_w"], 60.0)


class TestAdminPanel(unittest.TestCase):
    """The panel's /api/state, /api/history and page."""

    @classmethod
    def setUpClass(cls):
        import test_admin
        cls.TA = test_admin
        test_admin.setUpModule()

    @classmethod
    def tearDownClass(cls):
        pass                                                   # test_admin's own tearDownModule closes the server

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.close)
        self.panel = self.TA.A["panel"]
        self.saved = (self.panel.cpuprobe, self.panel.state_cache, list(self.panel.history))
        self.addCleanup(self.restore)

    def restore(self):
        self.panel.cpuprobe, self.panel.state_cache = self.saved[0], self.saved[1]
        self.panel.history.clear()
        self.panel.history.extend(self.saved[2])

    def state(self):
        self.panel.state_cache = (0, None)
        st, data, _ = self.TA.get("/api/state")
        self.assertEqual(st, 200)
        return json.loads(data)

    def test_state_has_the_cpu_block_and_keeps_the_old_fields(self):
        t = self.t
        t.w("proc/cpuinfo", CPUINFO_AMD)
        for n in range(4):
            t.cpu(n, 3000000 + n * 100000)
        t.hwmon(5, "k10temp", {"Tctl": 70000})
        t.stat([(1, 9)] * 4)
        self.panel.cpuprobe = t.probe()
        s = self.state()
        for k in ("cpu_pct", "load", "memory", "disks", "gpu", "gateway", "tunnel", "uptime"):
            self.assertIn(k, s)
        self.assertEqual(s["cpu"]["temp"]["c"], 70.0)
        self.assertEqual(s["cpu"]["freq"]["hi_mhz"], 3300)
        self.assertEqual(s["cpu"]["model"], "AMD Ryzen 9 5950X 16-Core Processor")

    def test_no_sources_means_blanks_not_an_error(self):
        self.panel.cpuprobe = self.t.probe()
        s = self.state()
        self.assertIsNone(s["cpu"]["temp"])
        self.assertEqual(s["cpu"]["freq"]["cores"], [])

    def test_a_probe_that_raises_does_not_take_the_state_down(self):
        class Boom:
            def snapshot(self):
                raise RuntimeError("boom")
        self.panel.cpuprobe = Boom()
        s = self.state()
        self.assertIsNone(s["cpu"])
        self.assertIn("memory", s)

    def test_hostile_model_is_inert_in_the_json_and_the_page(self):
        self.t.w("proc/cpuinfo", "processor\t: 0\nmodel name\t: <img src=x onerror=alert(1)><script>x</script>\n")
        self.panel.cpuprobe = self.t.probe()
        st, raw, _ = self.TA.get("/api/state")
        self.panel.state_cache = (0, None)
        st, raw, _ = self.TA.get("/api/state")
        self.assertNotIn(b"<img", raw)
        self.assertNotIn(b"<script", raw)
        st, page, _ = self.TA.get("/")
        self.assertNotIn(b"onerror", page)
        self.assertNotIn(b"innerHTML", page)                   # nothing on the page builds markup from data
        self.assertIn(b"$('cpumodel').textContent=c.model", page)

    def test_sampler_row_has_cpu_columns(self):
        self.t.w("proc/cpuinfo", CPUINFO_AMD)
        for n in range(4):
            self.t.cpu(n, 4000000)
        self.t.hwmon(5, "k10temp", {"Tctl": 66000})
        self.panel.cpuprobe = self.t.probe()
        self.panel.state_cache = (0, None)
        self.panel.sample()
        row = self.panel.history[-1]
        self.assertEqual(len(row), 9)
        self.assertEqual((row[7], row[8]), (66.0, 4.0))
        st, raw, _ = self.TA.get("/api/history")
        d = json.loads(raw)
        self.assertEqual(d["fields"][:7], ["t", "tps", "gpu_busy", "vram_gib", "gpu_temp", "power_w", "active"])
        self.assertEqual(d["fields"][7:], ["cpu_temp", "cpu_ghz"])
        self.assertTrue(all(len(r) == 9 for r in d["rows"]))

    def test_sampler_row_without_cpu_data_has_blanks(self):
        self.panel.cpuprobe = self.t.probe()
        self.panel.state_cache = (0, None)
        self.panel.sample()
        self.assertEqual(self.panel.history[-1][7:], [None, None])

    def test_old_seven_column_history_is_still_read(self):
        from o1common import Paths, write_json_atomic
        now = int(time.time())
        write_json_atomic(Paths.history, {"rows": [[now - 5, 1, 2, 3, 4, 5, 6], [now - 4, 1, 2, 3, 4, 5, 6, 7, 8.5],
                                                   [now - 3, 1, 2], "junk", [now - 2] + [0] * 10]}, mode=0o600)
        try:
            p = self.TA.A["mod"].Panel(self.TA.A["cfg"])
            rows = list(p.history)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][7:], [None, None])
            self.assertEqual(rows[1][7:], [7, 8.5])
        finally:
            os.unlink(Paths.history)

    def test_page_card_and_charts(self):
        st, page, _ = self.TA.get("/")
        for needle in (b'id="cpucard"', b'id="cpumodel"', b'id="cputemp"', b'id="cpuspeed"', b'id="cbars"',
                       b'id="ctempspark"', b"CPU temperature", b"CPU speed (GHz)"):
            self.assertIn(needle, page, needle)
        self.assertIn(b"@media (max-width:600px){.strips,.sparks{grid-template-columns:1fr}}", page)


if __name__ == "__main__":
    unittest.main()
