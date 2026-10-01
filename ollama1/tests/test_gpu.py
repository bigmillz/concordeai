"""GET /v1/info's GPU detection, from fixture sysfs trees, a fake
nvidia-smi and a small pci.ids."""
import os
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
    def test_the_desktops_card(self):
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
