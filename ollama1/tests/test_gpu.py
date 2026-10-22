"""GET /v1/info's GPU detection, from fixture sysfs trees, a fake
nvidia-smi and a small pci.ids."""
import os
import time
import shutil
import stat
import tempfile
import unittest

import o1test_util as U  # noqa: F401
import o1gpu

PCI_IDS = """# fixture
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t73bf  Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]
\t\t1043 04f2  ROG STRIX RX 6900 XT
\t7480  Navi 33 [Radeon RX 7600/7600 XT/7650 GRE]
8086  Intel Corporation
\t56a0  DG2 [Arc A770]
"""


class GpuFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1gpu-")
        self.sys = os.path.join(self.dir, "sys")
        self.ids = os.path.join(self.dir, "pci.ids")
        with open(self.ids, "w") as f:
            f.write(PCI_IDS)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.env = {k: os.environ.get(k) for k in ("OLLAMA1_SYS", "OLLAMA1_PCI_IDS", "PATH")}
        os.environ["OLLAMA1_SYS"] = self.sys
        os.environ["OLLAMA1_PCI_IDS"] = self.ids
        os.environ["PATH"] = self.bin + ":/usr/bin:/bin"

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def card(self, n, files):
        d = os.path.join(self.sys, "class/drm/card%d/device" % n)
        os.makedirs(d, exist_ok=True)
        for k, v in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, k)), exist_ok=True)
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")

    def nvidia_smi(self, out):
        p = os.path.join(self.bin, "nvidia-smi")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho '%s'\n" % out)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


class TestGpuInfo(GpuFixture):
    def test_the_servers_card(self):
        self.card(1, {"vendor": "0x1002", "device": "0x73bf", "revision": "0xc0",
                      "subsystem_vendor": "0x1002", "subsystem_device": "0x0e3a",
                      "mem_info_vram_total": "17163091968"})
        self.assertEqual(o1gpu.detect(), {"vendor": "amd", "name": "Radeon RX 6900 XT", "vram_bytes": 17163091968})

    def test_amd_subsystem_name_from_pci_ids(self):
        self.card(0, {"vendor": "0x1002", "device": "0x73bf", "revision": "0xc9",
                      "subsystem_vendor": "0x1043", "subsystem_device": "0x04f2",
                      "mem_info_vram_total": "17163091968"})
        self.assertEqual(o1gpu.detect()["name"], "ROG STRIX RX 6900 XT")

    def test_amd_bracket_name(self):
        self.card(0, {"vendor": "0x1002", "device": "0x7480", "revision": "0xc0",
                      "mem_info_vram_total": "8573157376"})
        self.assertEqual(o1gpu.detect()["name"], "Radeon RX 7600/7600 XT/7650 GRE")

    def test_amd_product_name(self):
        self.card(0, {"vendor": "0x1002", "device": "0x74a1", "product_name": "AMD Instinct MI300X",
                      "mem_info_vram_total": "206158430208"})
        self.assertEqual(o1gpu.detect(), {"vendor": "amd", "name": "Instinct MI300X", "vram_bytes": 206158430208})

    def test_nvidia(self):
        self.nvidia_smi("NVIDIA GeForce RTX 4090, 24564")
        self.assertEqual(o1gpu.detect(), {"vendor": "nvidia", "name": "GeForce RTX 4090", "vram_bytes": 24564 << 20})

    def test_amd_before_an_integrated_intel(self):
        self.card(0, {"vendor": "0x8086", "device": "0x4680"})
        self.card(1, {"vendor": "0x1002", "device": "0x73bf", "revision": "0xc0", "mem_info_vram_total": "17163091968"})
        self.assertEqual(o1gpu.detect()["vendor"], "amd")

    def test_intel_arc(self):
        self.card(0, {"vendor": "0x8086", "device": "0x56a0", "lmem_total_bytes": "17179869184"})
        self.assertEqual(o1gpu.detect(), {"vendor": "intel", "name": "Arc A770", "vram_bytes": 17179869184})

    def test_integrated_intel_has_no_vram(self):
        self.card(0, {"vendor": "0x8086", "device": "0x4680"})
        self.assertEqual(o1gpu.detect(), {"vendor": "intel", "name": None, "vram_bytes": None})

    def test_none(self):
        self.assertEqual(o1gpu.detect(), {"vendor": None, "name": None, "vram_bytes": None})


class TestGpuUsage(GpuFixture):
    """o1gpu.usage(): the three numbers GET /v1/usage serves."""
    AMD = {"vendor": "0x1002", "device": "0x73bf", "revision": "0xc0",
           "mem_info_vram_total": "17163091968", "mem_info_vram_used": "9126805504",
           "gpu_busy_percent": "37"}

    def test_amd_reads_the_dashboards_files(self):
        self.card(1, self.AMD)
        self.assertEqual(o1gpu.usage("amd"), {"busy_pct": 37, "vram_used_bytes": 9126805504,
                                              "vram_total_bytes": 17163091968})

    def test_only_three_fields(self):
        self.card(1, dict(self.AMD, product_name="Secret Card", serial_number="SN123"))
        self.assertEqual(sorted(o1gpu.usage("amd")), ["busy_pct", "vram_total_bytes", "vram_used_bytes"])

    def test_a_missing_file_is_null_not_an_error(self):
        files = dict(self.AMD)
        del files["gpu_busy_percent"], files["mem_info_vram_used"]
        self.card(1, files)
        self.assertEqual(o1gpu.usage("amd"), {"busy_pct": None, "vram_used_bytes": None,
                                              "vram_total_bytes": 17163091968})

    def test_no_card_at_all_is_all_null(self):
        none = {"busy_pct": None, "vram_used_bytes": None, "vram_total_bytes": None}
        self.assertEqual(o1gpu.usage("amd"), none)
        self.assertEqual(o1gpu.usage(None), none)
        self.assertEqual(o1gpu.usage("intel"), none)
        self.assertEqual(o1gpu.usage("nvidia"), none)       # no nvidia-smi on the path

    def test_garbage_and_out_of_range_are_null(self):
        self.card(1, dict(self.AMD, gpu_busy_percent="lots", mem_info_vram_used="-4"))
        got = o1gpu.usage("amd")
        self.assertIsNone(got["busy_pct"])
        self.assertIsNone(got["vram_used_bytes"])
        self.card(1, dict(self.AMD, gpu_busy_percent="180"))
        self.assertIsNone(o1gpu.usage("amd")["busy_pct"])

    def test_amd_ignores_an_integrated_intel_card(self):
        self.card(0, {"vendor": "0x8086", "device": "0x4680", "gpu_busy_percent": "99"})
        self.card(1, self.AMD)
        self.assertEqual(o1gpu.usage("amd")["busy_pct"], 37)

    def test_nvidia(self):
        self.nvidia_smi("61, 8123, 24564")
        self.assertEqual(o1gpu.usage("nvidia"), {"busy_pct": 61, "vram_used_bytes": 8123 << 20,
                                                 "vram_total_bytes": 24564 << 20})

    def test_nvidia_odd_output_is_null(self):
        self.nvidia_smi("[N/A], [N/A], 24564")
        self.assertEqual(o1gpu.usage("nvidia"), {"busy_pct": None, "vram_used_bytes": None,
                                                 "vram_total_bytes": 24564 << 20})


class TestStatsNvidia(GpuFixture):
    """o1stats.gpu() with no amdgpu card falls back to nvidia-smi (6b483)."""
    def test_dashboard_figures_from_nvidia_smi(self):
        import o1stats
        self.nvidia_smi("37, 12686, 16303, 61, 212.40, 300.00, 45, 2610, 14001")
        g = o1stats.gpu()
        self.assertEqual(g["busy_pct"], 37)
        self.assertEqual(g["vram_used"], 12686 << 20)
        self.assertEqual(g["vram_total"], 16303 << 20)
        self.assertEqual(g["temps"], {"edge": 61.0})
        self.assertEqual((g["power_w"], g["power_cap_w"], g["fan_pct"], g["fan_rpm"]), (212.4, 300, 45, None))
        self.assertEqual((g["sclk_mhz"], g["mclk_mhz"], g["vendor"]), (2610, 14001, "nvidia"))

    def test_not_supported_fields_are_none_not_an_error(self):
        import o1stats
        self.nvidia_smi("0, 512, 16303, 30, [N/A], [N/A], [N/A], 210, 405")
        g = o1stats.gpu()
        self.assertEqual((g["power_w"], g["power_cap_w"], g["fan_pct"]), (None, None, None))
        self.assertEqual(g["busy_pct"], 0)

    def test_no_card_and_no_nvidia_smi_is_none(self):
        import o1stats
        self.assertIsNone(o1stats.gpu())

    def test_an_amd_card_still_wins(self):
        import o1stats
        self.card(0, {"vendor": "0x1002", "mem_info_vram_total": "17163091968", "mem_info_vram_used": "1",
                      "gpu_busy_percent": "5"})
        self.nvidia_smi("99, 1, 16303, 61, 1, 1, 1, 1, 1")
        old, o1stats.SYS = o1stats.SYS, self.sys            # SYS is read once, at import
        try:
            self.assertEqual(o1stats.gpu()["busy_pct"], 5)
        finally:
            o1stats.SYS = old


class TestRamUsage(unittest.TestCase):
    """o1stats.ram_usage(): the server's memory in use, for GET /v1/usage."""
    def test_in_use_is_total_minus_available_not_free(self):
        import o1stats
        self.assertEqual(o1stats.ram_usage({"MemTotal": 62 << 30, "MemAvailable": 40 << 30, "MemFree": 1 << 30}),
                         {"used_bytes": 22 << 30, "total_bytes": 62 << 30})

    def test_missing_or_nonsense_is_null(self):
        import o1stats
        null = {"used_bytes": None, "total_bytes": None}
        self.assertEqual(o1stats.ram_usage({}), null)
        self.assertEqual(o1stats.ram_usage({"MemTotal": 0, "MemAvailable": 0}), null)
        self.assertEqual(o1stats.ram_usage({"MemTotal": 1 << 60}), null)
        self.assertEqual(o1stats.ram_usage({"MemTotal": 8 << 30}), {"used_bytes": None, "total_bytes": 8 << 30})
        self.assertEqual(o1stats.ram_usage({"MemTotal": 8 << 30, "MemAvailable": 9 << 30}),
                         {"used_bytes": None, "total_bytes": 8 << 30})

    def test_reads_a_proc_fixture(self):
        import tempfile
        import o1stats
        d = tempfile.mkdtemp(prefix="o1ram-")
        os.makedirs(d + "/proc")
        with open(d + "/proc/meminfo", "w") as f:
            f.write("MemTotal: 1000 kB\nMemFree: 10 kB\nMemAvailable: 400 kB\n")
        saved, o1stats.PROC = o1stats.PROC, d + "/proc"
        try:
            self.assertEqual(o1stats.ram_usage(), {"used_bytes": 600 * 1024, "total_bytes": 1000 * 1024})
        finally:
            o1stats.PROC = saved
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class TestNvidiaFromSandbox(GpuFixture):
    """6b486: no nvidia-smi (the gateway's PrivateDevices sandbox), an NVIDIA card in sysfs."""
    def test_vendor_and_name_without_memory(self):
        self.card(0, {"vendor": "0x10de", "device": "0x2c05", "subsystem_vendor": "0x1458", "subsystem_device": "0x4181"})
        g = o1gpu.detect()
        self.assertEqual(g["vendor"], "nvidia")
        self.assertIsNone(g["vram_bytes"])


class TestSharedReading(GpuFixture):
    """6b488: the fan service leaves its NVIDIA reading for the gateway, which can't run nvidia-smi."""
    def test_usage_falls_back_to_a_fresh_shared_reading(self):
        path = os.path.join(self.dir, "gpu-usage.json")
        old = o1gpu.SHARED
        o1gpu.SHARED = path
        try:
            o1gpu.publish({"busy_pct": 88, "vram_used_bytes": 5 << 30, "vram_total_bytes": 16 << 30})
            self.assertEqual(o1gpu.usage("nvidia"), {"busy_pct": 88, "vram_used_bytes": 5 << 30,
                                                     "vram_total_bytes": 16 << 30})
            self.assertIsNone(o1gpu.shared(path, now=time.time() + 60))         # stale: not believed
        finally:
            o1gpu.SHARED = old

    def test_nvidia_smi_still_wins(self):
        self.nvidia_smi("61, 8123, 24564")
        self.assertEqual(o1gpu.usage("nvidia")["busy_pct"], 61)


class TestPanelSharedReading(GpuFixture):
    """6b500: the panel can't run nvidia-smi either (PrivateDevices=yes); the fan service leaves the whole reading."""
    def setUp(self):
        super().setUp()
        self.old, o1gpu.SHARED = o1gpu.SHARED, os.path.join(self.dir, "gpu-usage.json")

    def tearDown(self):
        o1gpu.SHARED = self.old
        super().tearDown()

    def test_the_fan_service_publishes_the_full_reading_and_the_panel_reads_it(self):
        import o1stats
        import o1work
        self.nvidia_smi("37, 12686, 16303, 61, 212.40, 300.00, 45, 2610, 14001")
        g = o1work.Gpu()
        g.vendor = "nvidia"
        real = os.geteuid
        os.geteuid = lambda: 0
        try:
            self.assertEqual(g.busy(), 37)
        finally:
            os.geteuid = real
        self.assertEqual(o1gpu.usage("nvidia")["vram_total_bytes"], 16303 << 20)
        os.remove(os.path.join(self.bin, "nvidia-smi"))     # now the panel's sandbox: no nvidia-smi
        s = o1stats.gpu()
        self.assertEqual((s["busy_pct"], s["power_w"], s["power_cap_w"], s["temps"]), (37, 212.4, 300, {"edge": 61.0}))
        self.assertEqual((s["vram_used"], s["sclk_mhz"], s["vendor"]), (12686 << 20, 2610, "nvidia"))
        self.assertEqual(o1gpu.usage("nvidia")["busy_pct"], 37)             # the gateway's reader still works

    def test_a_stale_or_three_number_reading_is_not_shown(self):
        import o1stats
        o1gpu.publish({"busy_pct": 5, "vram_used_bytes": 1, "vram_total_bytes": 2})   # an older fan service
        self.assertIsNone(o1stats.gpu())
        o1gpu.publish({"busy_pct": 5, "vendor": "nvidia", "temps": {"edge": 50}})
        self.assertEqual(o1stats.gpu()["busy_pct"], 5)
        self.assertIsNone(o1stats.gpu_nvidia_shared(now=time.time() + 60))


class TestNet(unittest.TestCase):
    """6b500: network traffic for the panel, physical ports only."""
    DEV = ("Inter-|   Receive                                                |  Transmit\n"
           " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
           "    lo: %d 0 0 0 0 0 0 0 %d 0 0 0 0 0 0 0\n"
           "eth0: %d 0 0 0 0 0 0 0 %d 0 0 0 0 0 0 0\n"
           "   br0: %d 0 0 0 0 0 0 0 %d 0 0 0 0 0 0 0\n")

    def setUp(self):
        import o1stats
        self.o1stats = o1stats
        self.dir = tempfile.mkdtemp(prefix="o1net-")
        os.makedirs(os.path.join(self.dir, "sys/class/net/eth0/device"))
        os.makedirs(os.path.join(self.dir, "sys/class/net/br0"))
        os.makedirs(os.path.join(self.dir, "proc/net"))
        self.old = (o1stats.SYS, o1stats.PROC)
        o1stats.SYS, o1stats.PROC = os.path.join(self.dir, "sys"), os.path.join(self.dir, "proc")

    def tearDown(self):
        self.o1stats.SYS, self.o1stats.PROC = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def counters(self, port_rx, port_tx):
        with open(os.path.join(self.dir, "proc/net/dev"), "w") as f:
            f.write(self.DEV % (10 ** 9, 10 ** 9, port_rx, port_tx, port_rx, port_tx))

    def test_rate_between_two_calls_counts_each_byte_once(self):
        t = [100.0]
        n = self.o1stats.Net(clock=lambda: t[0])
        self.counters(1000, 500)
        self.assertIsNone(n.sample())
        self.counters(1000 + 2_000_000, 500 + 250_000)
        t[0] += 2
        self.assertEqual(n.sample(), {"rx_bps": 1_000_000, "tx_bps": 125_000})

    def test_a_counter_reset_is_no_reading(self):
        t = [100.0]
        n = self.o1stats.Net(clock=lambda: t[0])
        self.counters(5000, 5000)
        n.sample()
        self.counters(10, 10)
        t[0] += 1
        self.assertIsNone(n.sample())
