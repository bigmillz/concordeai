"""The dashboard: its metrics parsing from /proc and /sys fixtures, the
renderer at 80x25, 120x40 and 240x67 in every glyph mode, the ASCII
fallback, the console-font picker, and that no content is ever drawn."""
import ast
import gzip
import os
import shutil
import struct
import tempfile
import time
import unittest

import o1test_util as U
import dash_sample
import o1dashui
import o1font
import o1metrics
import o1stats

SIZES = [(80, 25), (120, 40), (240, 67), (200, 56), (100, 30)]
MODES = ["braille", "blocks", "ascii"]


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()

    def test_every_size_mode_page(self):
        st = dash_sample.sample()
        for w, h in SIZES:
            for g in MODES:
                for page in range(3):
                    for rng in (300, 3600):
                        rows = o1dashui.render(st, w, h, g, rng, page).text().split("\n")
                        self.assertEqual(len(rows), h, (w, h, g, page))
                        for r in rows:
                            self.assertEqual(len(r), w, (w, h, g, page))

    def test_canvas_clips_at_every_edge(self):
        c = o1dashui.Canvas(10, 3, "blocks")
        c.put(8, 0, "abcdef")
        c.put(-2, 1, "xyz")
        c.put(0, 5, "gone")
        c.hbar(7, 2, 9, 1.0)
        c.chart(8, 0, 6, 3, [1, 2, 3, 4])
        rows = c.text().split("\n")
        self.assertEqual([len(r) for r in rows], [10, 10, 10])
        self.assertEqual(rows[0][8:], "ab")
        self.assertEqual(rows[1][:1], "z")

    def test_empty_state(self):
        for w, h in SIZES:
            for g in MODES:
                o1dashui.render({"time": time.time()}, w, h, g, 300, 0)

    def test_ascii_is_ascii(self):
        st = dash_sample.sample(pairing=False)
        for w, h in SIZES:
            for page in range(3):
                text = o1dashui.render(st, w, h, "ascii", 300, page).text()
                self.assertTrue(all(ord(ch) < 128 for ch in text), (w, h, page))
                self.assertNotIn("?C", text)                 # the degree sign maps cleanly
        text = o1dashui.render(dash_sample.sample(pairing=True), 80, 25, "ascii", 300, 0).text()
        self.assertTrue(all(ord(ch) < 128 for ch in text))

    def test_braille_draws_braille(self):
        text = o1dashui.render(dash_sample.sample(), 240, 67, "braille", 300, 0).text()
        self.assertTrue(any(0x2801 <= ord(ch) <= 0x28ff for ch in text))
        text = o1dashui.render(dash_sample.sample(), 240, 67, "blocks", 300, 0).text()
        self.assertFalse(any(0x2800 <= ord(ch) <= 0x28ff for ch in text))

    def test_panels_on_the_big_screen(self):
        text = o1dashui.render(dash_sample.sample(), 240, 67, "blocks", 300, 0).text()
        for title in ("Tokens per second", "Requests", "Loaded models", "Model events", "Paired devices", "GPU",
                      "Memory", "CPU", "Disks", "Network", "Health"):
            self.assertIn(title, text)
        for fact in ("48.6", "first token 0.83 s", "prompt 612.4", "fit 2", "gpt-oss:120b", "rest in RAM",
                     "refused: gpu_fit", "sclk 2310", "mclk 1000", "junction 78", "212/289 W",
                     "Ollama 47.1 GiB of cap 52.7 GiB", "resync 41.3%", "rtt 23 ms", "enp38s0 link 1000",
                     "Patrick's MacBook Pro", "connected", "last sleep", "EDT",
                     "power 312 W (plug)  24 h 3.54 kWh  $0.61  on-peak until 19:00"):
            self.assertIn(fact, text)

    def test_80x25_pages_cover_everything(self):
        st = dash_sample.sample()
        text = "".join(o1dashui.render(st, 80, 25, "blocks", 300, p).text() for p in range(3))
        for title in ("Tokens per second", "Requests", "Loaded models", "GPU", "Memory", "CPU", "Disks", "Network"):
            self.assertIn(title, text)

    def test_pairing_overlay(self):
        for w, h in SIZES:
            text = o1dashui.render(dash_sample.sample(pairing=True), w, h, "blocks", 300, 0).text()
            self.assertIn("PAIRING WINDOW OPEN", text)
            self.assertIn("7K4M-2QXD-9FHT", text)
            self.assertGreaterEqual(text.count("█"), 100)
            self.assertNotIn("Tokens per second", text)          # full screen: nothing else shows

    def test_no_content_ever(self):
        marker = "SECRET-PROMPT-MARKER"
        st = dash_sample.sample(marker=marker)
        for w, h in SIZES:
            for g in MODES:
                for page in range(3):
                    self.assertNotIn(marker, o1dashui.render(st, w, h, g, 300, page).text())

    def test_renderer_reads_no_content_fields(self):
        tree = ast.parse(open(os.path.join(U.LIB, "o1dashui.py")).read())
        keys = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for bad in ("prompt", "messages", "content", "response", "recent", "system", "images"):
            self.assertNotIn(bad, keys)

    def test_fast_enough(self):
        st = dash_sample.sample()
        t0 = time.time()
        for _ in range(20):
            o1dashui.render(st, 240, 67, "braille", 3600, 0)
        per = (time.time() - t0) / 20
        self.assertLess(per, 0.05)   # well under 5% of a core at 1 Hz, even on a slow machine


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1metrics-")
        self.sys, self.proc = os.path.join(self.dir, "sys"), os.path.join(self.dir, "proc")
        os.makedirs(self.proc)
        self.saved = (o1stats.SYS, o1stats.PROC, o1metrics.SYS, o1metrics.PROC)
        o1stats.SYS = o1metrics.SYS = self.sys
        o1stats.PROC = o1metrics.PROC = self.proc

    def tearDown(self):
        o1stats.SYS, o1stats.PROC, o1metrics.SYS, o1metrics.PROC = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def test_cpu_per_core(self):
        a = o1metrics.parse_proc_stat("cpu  100 0 100 800 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\ncpu1 50 0 50 400 0 0 0 0\nintr 5\n")
        b = o1metrics.parse_proc_stat("cpu  200 0 200 900 0 0 0 0\ncpu0 150 0 50 400 0 0 0 0\ncpu1 50 0 150 500 0 0 0 0\n")
        total, cores = o1metrics.cpu_percent(a, b)
        self.assertEqual(total, 66.7)
        self.assertEqual(cores, [100.0, 50.0])

    def test_cpu_core_order(self):
        text = "cpu 1 0 0 1 0 0 0 0\n" + "".join("cpu%d 1 0 0 1 0 0 0 0\n" % i for i in (0, 1, 2, 10, 11, 3))
        _, cores = o1metrics.cpu_percent(o1metrics.parse_proc_stat(text), o1metrics.parse_proc_stat(text))
        self.assertEqual(len(cores), 6)

    def test_diskstats_whole_disks_only(self):
        text = ("   8  0 sda 10 0 1000 0 5 0 200 0 0 0 0\n"
                "   8  1 sda1 10 0 1000 0 5 0 200 0 0 0 0\n"
                " 259  0 nvme0n1 1 0 64 0 1 0 8 0 0 0 0\n"
                " 259  1 nvme0n1p3 1 0 64 0 1 0 8 0 0 0 0\n"
                " 253  0 dm-0 1 0 64 0 1 0 8 0 0 0 0\n"
                "   9 127 md127 1 0 64 0 1 0 8 0 0 0 0\n")
        self.assertEqual(o1metrics.parse_diskstats(text), ((1000 + 64) * 512, (200 + 8) * 512))

    def test_rtt(self):
        m = ('# HELP x\nquic_client_smoothed_rtt{conn_index="0"} 20\nquic_client_smoothed_rtt{conn_index="1"} 30\n'
             'quic_client_latest_rtt{conn_index="0"} 99\n')
        self.assertEqual(o1metrics.parse_rtt(m), 25.0)
        self.assertEqual(o1metrics.parse_rtt("quic_client_latest_rtt 12\n"), 12.0)
        self.assertIsNone(o1metrics.parse_rtt("nothing 1\n"))

    def test_hwmon(self):
        self.write("sys/class/hwmon/hwmon2/name", "k10temp\n")
        self.write("sys/class/hwmon/hwmon2/temp1_label", "Tctl\n")
        self.write("sys/class/hwmon/hwmon2/temp1_input", "58500\n")
        self.assertEqual(o1metrics.cpu_temp(), 58.5)
        dev = os.path.join(self.sys, "class/drm/card1/device")
        self.write("sys/class/drm/card1/device/hwmon/hwmon9/freq1_label", "sclk\n")
        self.write("sys/class/drm/card1/device/hwmon/hwmon9/freq1_input", "2310000000\n")
        self.write("sys/class/drm/card1/device/hwmon/hwmon9/freq2_label", "mclk\n")
        self.write("sys/class/drm/card1/device/hwmon/hwmon9/freq2_input", "1000000000\n")
        self.assertEqual(o1metrics.gpu_clocks(dev), {"sclk_mhz": 2310, "mclk_mhz": 1000})

    def test_ollama_cgroup(self):
        self.write("sys/fs/cgroup/system.slice/ollama.service/memory.current", "5368709120\n")
        self.write("sys/fs/cgroup/system.slice/ollama.service/memory.max", "max\n")
        self.assertEqual(o1metrics.ollama_cgroup(), {"current": 5 << 30, "max": None})
        self.write("sys/fs/cgroup/system.slice/ollama.service/memory.max", "56908316672\n")
        self.assertEqual(o1metrics.ollama_cgroup()["max"], 56908316672)

    def test_sampler_rates(self):
        self.write("proc/stat", "cpu  100 0 100 800 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0\n")
        self.write("proc/diskstats", "   8 0 sda 1 0 0 0 1 0 0 0 0 0 0\n")
        self.write("proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        self.write("sys/class/net/br0/statistics/rx_bytes", "1000\n")
        self.write("sys/class/net/br0/statistics/tx_bytes", "500\n")
        clock = [1000.0]
        s = o1metrics.Sampler(clock=lambda: clock[0], tunnel_port=1)
        s.slow["net"] = (1e12, {"name": "br0"})
        s.slow["tunnel"] = (1e12, {"up": False})
        s.slow["updates"] = (1e12, {})
        s.tick()
        clock[0] = 1002.0
        self.write("proc/stat", "cpu  200 0 200 900 0 0 0 0\ncpu0 200 0 200 900 0 0 0 0\n")
        self.write("proc/diskstats", "   8 0 sda 1 0 4096 0 1 0 2048 0 0 0 0\n")
        self.write("sys/class/net/br0/statistics/rx_bytes", "5000\n")
        st = s.tick()
        self.assertEqual(st["io"], {"read_bps": 4096 * 512 / 2, "write_bps": 2048 * 512 / 2})
        self.assertEqual(st["net"]["rx_bps"], 2000.0)
        self.assertEqual(st["cpu"]["cores"], [66.7])
        self.assertEqual(st["series"]["ram_used"][-1], 600 * 1024)
        self.assertEqual(len(st["series"]["cpu_total"]), 2)


def psf2(glyphs):
    """A PSF2 font whose unicode table maps one code point per glyph."""
    n, h, w = len(glyphs), 16, 8
    head = struct.pack("<4s7I", b"\x72\xb5\x4a\x86", 0, 32, 1, n, h, h, w)
    table = b"".join(chr(cp).encode("utf-8") + b"\xff" for cp in glyphs)
    return head + b"\x00" * (n * h) + table


def psf1(glyphs):
    n = 512 if len(glyphs) > 256 else 256
    head = bytes([0x36, 0x04, 0x02 | (0x01 if n == 512 else 0), 16])
    table = b"".join(struct.pack("<HH", cp, 0xFFFF) for cp in glyphs)
    table += b"".join(struct.pack("<H", 0xFFFF) for _ in range(n - len(glyphs)))
    return head + b"\x00" * (n * 16) + table


class TestFont(unittest.TestCase):
    ascii = list(range(0x20, 0x7f))
    blocks = [ord(c) for c in o1dashui.NEEDED["blocks"]]
    braille = list(range(0x2800, 0x2900))

    def test_psf2_modes(self):
        self.assertEqual(o1font.glyph_mode(o1font.parse_psf(psf2(self.ascii))[2]), "ascii")
        self.assertEqual(o1font.glyph_mode(o1font.parse_psf(psf2(self.ascii + self.blocks))[2]), "blocks")
        self.assertEqual(o1font.glyph_mode(o1font.parse_psf(psf2(self.ascii + self.blocks + self.braille))[2]),
                         "braille")
        self.assertIsNone(o1font.glyph_mode(o1font.parse_psf(psf2(list(range(0x41, 0x5b))))[2]))

    def test_psf1(self):
        w, h, cps = o1font.parse_psf(psf1(self.ascii + self.blocks))
        self.assertEqual((w, h), (8, 16))
        self.assertEqual(o1font.glyph_mode(cps), "blocks")

    def test_gzip_and_survey(self):
        d = tempfile.mkdtemp()
        try:
            with gzip.open(os.path.join(d, "Uni3-Terminus16.psf.gz"), "wb") as f:
                f.write(psf2(self.ascii + self.blocks))
            with open(os.path.join(d, "Lat15-Fixed16.psf"), "wb") as f:
                f.write(psf2(self.ascii))
            got = {os.path.basename(p): m for p, w, h, m in o1font.survey([d])}
            self.assertEqual(got, {"Uni3-Terminus16.psf.gz": "blocks", "Lat15-Fixed16.psf": "ascii"})
        finally:
            shutil.rmtree(d)

    def test_pick_by_glyphs_then_size(self):
        fonts = [("/f/Uni3-Terminus16.psf.gz", 8, 16, "blocks"), ("/f/Uni3-Terminus20x10.psf.gz", 10, 20, "blocks"),
                 ("/f/Uni3-Terminus24x12.psf.gz", 12, 24, "blocks"), ("/f/Lat15-Fixed16.psf.gz", 8, 16, "ascii")]
        self.assertEqual(o1font.pick(fonts, (1920, 1080))["font"], "/f/Uni3-Terminus16.psf.gz")    # 240 cols
        self.assertEqual(o1font.pick(fonts, (2560, 1440))["font"], "/f/Uni3-Terminus24x12.psf.gz")  # 213 cols
        self.assertEqual(o1font.pick(fonts + [("/f/Braille8.psf", 8, 16, "braille")], (1920, 1080))["glyphs"],
                         "braille")
        self.assertIsNone(o1font.pick([("/f/x.psf", 8, 16, None)], (1920, 1080)))


if __name__ == "__main__":
    unittest.main()
