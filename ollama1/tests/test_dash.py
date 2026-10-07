"""The dashboard: its metrics parsing from /proc and /sys fixtures, the
renderer at every size (no panel can leave its box, nothing overlaps, widths are
true display widths), the terminal writer (absolute positions, a clear on a
resize, nothing stale), the console-font picker, and that no content is ever
drawn."""
import ast
import gzip
import os
import re
import shutil
import signal
import struct
import tempfile
import time
import unittest

import o1test_util as U
import dash_sample
import o1dashterm
import o1dashui
import o1font
import o1metrics
import o1stats

SIZES = [(80, 24), (100, 30), (120, 40), (160, 50), (240, 67)]
SMALL = [(79, 24), (76, 20), (60, 30), (40, 12), (36, 9), (30, 8), (10, 3), (1, 1), (300, 90)]
MODES = ["blocks", "ascii"]
ORDER = ["gpu", "cpumem", "requests", "models", "storage", "network", "health"]
TITLES = {"gpu": "GPU", "cpumem": "CPU and memory", "requests": "Requests", "models": "Loaded models",
          "storage": "Storage", "network": "Network", "health": "Health"}


def hostile_state():
    """Long names, wide characters, an escape sequence in a name, nonsense numbers."""
    st = dash_sample.sample()
    long_name = "model-" + "x" * 200
    st["gw"]["loaded"][0]["name"] = long_name
    st["gw"]["loaded"][1]["name"] = "\u6a21\u578b\u540d\u524d-\U0001F600-e\u0301-" + "\u6f22" * 40
    st["gw"]["events"][-1]["model"] = "\x1b[2J\x1b[31mred\x1b[0m" + "y" * 100
    st["gw"]["devices"][0]["name"] = "\x1b]0;title\x07dev\x00ice\t\n"
    st["disks"][0]["mount"] = "/very/long/mount/point/" + "d" * 80
    st["host"] = "\u670d\u52a1\u5668" * 30
    st["net"]["ports"][0]["name"] = "enp" + "9" * 60
    st["gpu"]["busy_pct"] = 100
    st["gpu"]["vram_used"] = 1e30
    st["gpu"]["power_w"] = float("inf")
    st["gpu"]["temps"] = {"junction": float("nan"), "edge": "hot"}
    st["cpu"]["total"] = 0
    st["cpu"]["temp"] = -5
    st["mem"]["total"] = 10 ** 40
    st["gw"]["requests_today"] = 10 ** 15
    st["gw"]["active"] = 12345678901234567890
    st["io"]["read_bps"] = 1e300
    st["uptime"] = 10 ** 12
    return st


def sparse_state():
    """Fields present but empty or None, as a half-started server gives."""
    return {"time": 1790000000.0, "host": None, "uptime": None, "gw": {"loaded": None, "events": [None, 5]},
            "gpu": {"busy_pct": None, "temps": None}, "cpu": {"cores": None, "load": None}, "mem": None,
            "disks": [None, {}, {"mount": None, "mounted": True, "total": 0}], "raid": [{}], "io": None,
            "net": {"ports": [None, {}]}, "tunnel": {}, "updates": {"sleep": {"resume_check": {}}},
            "series": {"tps": [None, None, "x", 5], "gpu_busy": None}, "power": {}}


def states():
    return {"sample": dash_sample.sample(), "empty": {}, "no-time": {"time": None}, "sparse": sparse_state(),
            "hostile": hostile_state()}


def boxes(w, h):
    return [(n, x, y, bw, bh) for n, x, y, bw, bh in o1dashui.plan(w, h)]


def inside_a_box(bx, x, y):
    return any(x0 <= x < x0 + bw and y0 <= y < y0 + bh for _, x0, y0, bw, bh in bx)


class VScreen:
    """A tiny terminal: it follows the cursor-position, clear and colour
    sequences the dashboard writes, with line wrap off, and nothing else."""

    def __init__(self, w, h, fill=" "):
        self.w, self.h = w, h
        self.cells = [[fill] * w for _ in range(h)]
        self.cleared = 0

    def resize(self, w, h):
        old = self.cells
        self.w, self.h = w, h
        self.cells = [[(old[y][x] if y < len(old) and x < len(old[y]) else " ") for x in range(w)] for y in range(h)]

    def write(self, text):
        import re
        y = x = 0
        pos = 0
        pat = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")
        while pos < len(text):
            m = pat.match(text, pos)
            if m:
                pos = m.end()
                arg, cmd = m.group(1), m.group(2)
                if cmd == "H":
                    parts = [p for p in arg.split(";") if p]
                    y, x = (int(parts[0]) - 1, int(parts[1]) - 1) if len(parts) == 2 else (0, 0)
                elif cmd == "J" and arg == "2":
                    self.cells = [[" "] * self.w for _ in range(self.h)]
                    self.cleared += 1
                elif cmd in ("m", "l", "h"):
                    pass
                else:
                    raise AssertionError("unexpected sequence %r" % m.group(0))
                continue
            ch = text[pos]
            pos += 1
            if ord(ch) < 32:
                raise AssertionError("control character %r in the output" % ch)
            if o1dashui.cw(ch) == 0:
                continue
            if 0 <= y < self.h and 0 <= x < self.w:
                self.cells[y][x] = ch
                if o1dashui.cw(ch) == 2 and x + 1 < self.w:
                    self.cells[y][x + 1] = ""
            x = min(x + o1dashui.cw(ch), self.w)       # wrap is off: the last column stays the last column

    def rows(self):
        return ["".join(r) for r in self.cells]


class TestWidth(unittest.TestCase):
    def test_wide_characters_are_two_cells(self):
        self.assertEqual(o1dashui.swidth("abc"), 3)
        self.assertEqual(o1dashui.swidth("\u6a21\u578b"), 4)
        self.assertEqual(o1dashui.swidth("\U0001F600"), 2)
        self.assertEqual(o1dashui.swidth("e\u0301"), 1)         # a combining mark takes no room

    def test_ansi_takes_no_room(self):
        self.assertEqual(o1dashui.swidth("\x1b[31mab\x1b[0m"), 2)
        self.assertEqual(o1dashui.swidth("\x1b]0;title\x07ab"), 2)
        self.assertEqual(o1dashui.clean("a\x1b[2Jb\x00c\x07d"), "abcd")

    def test_truncation(self):
        self.assertEqual(o1dashui.trunc("abcdefghij", 10), "abcdefghij")
        self.assertEqual(o1dashui.trunc("abcdefghij", 6), "abc...")
        self.assertEqual(o1dashui.trunc("abcdefghij", 3), "abc")
        self.assertEqual(o1dashui.trunc("abcdefghij", 0), "")
        for w in range(0, 12):
            for s in ("x" * 40, "\u6a21\u578b\u540d\u524d" * 5, "a\u6a21b\u578bc" * 4, "\x1b[31m" + "y" * 30):
                self.assertLessEqual(o1dashui.swidth(o1dashui.trunc(s, w)), w, (w, s))
        self.assertEqual(o1dashui.trunc("\u6a21\u578b\u540d\u524d", 5), "\u6a21...")

    def test_cells_clip_at_every_edge(self):
        c = o1dashui.Cells(10, 3, "blocks")
        c.put(8, 0, "abcdef")
        c.put(-2, 1, "xyz")
        c.put(0, 5, "gone")
        c.put(0, -1, "gone")
        c.hbar(7, 2, 9, 1.0)
        c.spark(8, 0, 6, [1, 2, 3, 4])
        rows = c.text().split("\n")
        self.assertEqual([len(r) for r in rows], [10, 10, 10])
        self.assertEqual(rows[0][:8], " " * 8)
        self.assertEqual(rows[1][:1], "z")

    def test_wide_character_is_never_split_at_an_edge(self):
        c = o1dashui.Cells(6, 2, "blocks")
        c.put(5, 0, "\u6a21")                                   # half outside: nothing of it shows
        c.put(-1, 1, "\u6a21b")
        self.assertEqual(c.chars[0][5], " ")
        self.assertEqual(c.chars[1][0], " ")                    # its second half alone would be a lie
        self.assertEqual(c.chars[1][1], "b")
        c.put(2, 0, "\u6a21\u578b")
        self.assertEqual(c.chars[0][2:6], ["\u6a21", "", "\u578b", ""])
        c.put(3, 0, "x")                                        # over the second half of the first one
        self.assertEqual(c.chars[0][2:4], [" ", "x"])

    def test_blit_clips(self):
        big = o1dashui.Cells(10, 4, "blocks")
        small = o1dashui.Cells(6, 3, "blocks")
        small.fill(0, 0, 6, 3, "#")
        big.blit(small, 7, 2)
        big.blit(small, -3, -1)
        rows = big.text().split("\n")
        self.assertEqual([len(r) for r in rows], [10] * 4)
        self.assertEqual(rows[3], "       ###")
        self.assertEqual(rows[1], "###       ")
        self.assertEqual(rows[0], "###       ")


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        del o1dashui.ERRORS[:]

    def tearDown(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()

    def test_every_size_every_state(self):
        """Exactly w x h cells; every row is exactly w display cells; no widget error."""
        for w, h in SIZES + SMALL:
            for name, st in states().items():
                for g in MODES:
                    for console in (False, True):
                        c = o1dashui.render(st, w, h, g, 300, 0, console=console)
                        self.assertEqual((c.w, c.h), (w, h))
                        self.assertEqual(len(c.chars), h)
                        for y, row in enumerate(c.chars):
                            self.assertEqual(len(row), w, (w, h, name, g, y))
                            self.assertEqual(o1dashui.swidth("".join(row)), w, (w, h, name, g, y, "".join(row)))
                            self.assertEqual(len(c.styles[y]), w)
                            for x, ch in enumerate(row):
                                if ch == "":                    # only ever the second half of a wide character
                                    self.assertEqual(o1dashui.cw(row[x - 1]), 2, (w, h, name, x, y))
                                else:
                                    self.assertEqual(o1dashui.swidth(ch), o1dashui.cw(ch))
        self.assertEqual(o1dashui.ERRORS, [])

    def test_nothing_outside_the_boxes(self):
        """Between the boxes there is only blank: no style, no character."""
        for w, h in SIZES + SMALL:
            bx = boxes(w, h)
            if not bx:                                        # (the "screen too small" message is there)
                continue
            for name, st in states().items():
                c = o1dashui.render(st, w, h, "blocks", 300, 0)
                for y in range(1, h - 1):
                    for x in range(w):
                        if not inside_a_box(bx, x, y):
                            self.assertEqual((c.chars[y][x], c.styles[y][x]), (" ", ""), (w, h, name, x, y))

    def test_no_panel_is_overwritten_by_another(self):
        """What a box holds in the frame is exactly that panel drawn alone."""
        for w, h in SIZES + SMALL:
            for name, st in states().items():
                c = o1dashui.render(st, w, h, "blocks", 300, 0)
                ctx = {"now": st.get("time") or 0, "range_s": 300, "rng": "5 min",
                       "warns": o1dashui.warnings(st, st.get("time") or 0)}
                for pn, x, y, pw, ph in boxes(w, h):
                    alone = o1dashui.draw_panel(pn, pw, ph, st, ctx, "blocks", False)
                    for yy in range(ph):
                        self.assertEqual(c.chars[y + yy][x:x + pw], alone.chars[yy], (w, h, name, pn, yy))
                        self.assertEqual(c.styles[y + yy][x:x + pw], alone.styles[yy], (w, h, name, pn, yy))

    def test_a_widget_that_writes_everywhere_stays_in_its_box(self):
        def hostile(c, st, ctx):
            for dy in (-50, -1, 0, 1, c.h - 1, c.h, 40):
                for dx in (-300, -1, 0, 1, c.w - 3, c.w, 500):
                    c.put(dx, dy, "#" * 300, "bad")
                    c.put(dx, dy, "\u6a21" * 300, "bad")
            c.hbar(-5, 0, 500, 1.0, "bad")
            c.spark(-5, 1, 500, [1, 2, 3], style="bad")
            c.fill(-10, -10, 900, 900, "@", "bad")

        saved = dict(o1dashui.PANELS)
        try:
            for n in saved:
                o1dashui.PANELS[n] = (saved[n][0], hostile, saved[n][2])
            for w, h in SIZES + SMALL:
                c = o1dashui.render(dash_sample.sample(), w, h, "blocks", 300, 0)
                bx = boxes(w, h)
                for y in range(1, h - 1) if bx else ():
                    for x in range(w):
                        if not inside_a_box(bx, x, y):
                            self.assertEqual((c.chars[y][x], c.styles[y][x]), (" ", ""), (w, h, x, y))
                for n, x0, y0, bw, bh in bx:                    # the frames are intact
                    top = "".join(c.chars[y0][x0:x0 + bw])
                    self.assertTrue(top.startswith("\u250c\u2500 " + TITLES[n]), (w, h, n, top))
                    self.assertEqual(c.chars[y0 + bh - 1][x0], "\u2514")
                    self.assertEqual(c.chars[y0 + bh - 1][x0 + bw - 1], "\u2518")
                    self.assertEqual(c.chars[y0 + bh // 2][x0], "\u2502")
                    self.assertEqual(c.chars[y0 + bh // 2][x0 + bw - 1], "\u2502")
        finally:
            o1dashui.PANELS.clear()
            o1dashui.PANELS.update(saved)

    def test_plan_never_overlaps_or_leaves_the_body(self):
        for w in list(range(1, 260, 7)) + [75, 76, 99, 100, 119, 120, 121, 240]:
            for h in list(range(1, 80, 3)) + [24, 30, 40, 50, 67]:
                bx = o1dashui.plan(w, h)
                names = [b[0] for b in bx]
                self.assertEqual(names, [n for n in ORDER if n in names])         # priority order...
                dropped = [n for n in ORDER if n not in names]
                self.assertEqual(names + dropped, ORDER, (w, h))                   # ...and only the tail goes
                for _, x, y, bw, bh in bx:
                    self.assertTrue(x >= 0 and x + bw <= w and y >= 1 and y + bh <= h - 1 and bw >= 4 and bh >= 3, (w, h))
                for i, a in enumerate(bx):
                    for b in bx[i + 1:]:
                        self.assertTrue(a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1] or
                                        a[2] + a[4] <= b[2] or b[2] + b[4] <= a[2], (w, h, a, b))

    def test_wide_screens_centre_a_120_column_layout(self):
        for w in (160, 240):
            bx = o1dashui.plan(w, 67)
            self.assertEqual(min(b[1] for b in bx), (w - 120) // 2)
            self.assertEqual(max(b[1] + b[3] for b in bx), (w - 120) // 2 + 120)
        self.assertEqual(max(b[1] + b[3] for b in o1dashui.plan(100, 30)), 100)

    def test_which_panels_at_which_size(self):
        st = dash_sample.sample()

        def present(w, h):
            text = o1dashui.render(st, w, h, "blocks").text()
            return [n for n in ORDER if "\u2500 %s " % TITLES[n] in text]
        for w, h in ((100, 30), (120, 40), (160, 50), (240, 67)):
            self.assertEqual(present(w, h), ORDER, (w, h))
        self.assertEqual(present(80, 24), ORDER[:6])             # Health goes first (it is in the header)
        self.assertEqual(present(80, 20), ORDER[:4])
        self.assertEqual(present(60, 30), ORDER[:4])             # one column below 76 wide
        self.assertEqual(present(40, 10), ORDER[:1])
        self.assertEqual(present(30, 8), [])
        self.assertIn("Screen too small", o1dashui.render(st, 30, 8, "blocks").text())

    def test_the_facts_are_on_screen(self):
        text = o1dashui.render(dash_sample.sample(), 100, 30, "blocks", 300, 0).text()
        for fact in ("97%", "14.6/16.0 GiB", "212/289 W", "78\u00b0C", "Fan 38%", "14%", "58\u00b0C", "1.20 0.90 0.80",
                     "22.5/60.7 GiB", "47.1/52.7 GiB", "Active 1", "Queued 2", "Today 184", "48.6 tok/s",
                     "First token 0.83 s", "Errors 4", "fit 2", "qwen3:14b", "gpt-oss:120b", "100% 11.0G",
                     "RAID md127 healthy", "resync 41%", "/srv/data", "6.8T free", "Down", "Up",
                     "2 ports, 1000 Mb/s", "Tunnel connected (4)", "rtt 23 ms", "testsrv", "EDT", "1 TO CHECK",
                     "Power  312 W (plug)", "$0.61", "on-peak until 19:00", "Uptime 3d 4h", "Ollama current v0.35.1"):
            self.assertIn(fact, text)

    def test_wider_panels_name_the_ports(self):
        text = o1dashui.render(dash_sample.sample(), 120, 40, "blocks").text()
        self.assertIn("enp5s0 1000 Mb/s  enp6s0 1000 Mb/s", text)
        self.assertIn("RAID md127 raid1 [UU] healthy", text)
        self.assertIn("100% GPU  11.0 GiB  4h 41m", text)

    def test_missing_data_shows_dashes_and_a_problem(self):
        text = o1dashui.render({}, 100, 30, "blocks").text()
        self.assertIn("no GPU found", text)
        self.assertIn("PROBLEM", text)
        self.assertIn("Gateway is not running", text)
        self.assertEqual(o1dashui.ERRORS, [])
        text = o1dashui.render(None, 80, 24, "ascii").text()
        self.assertIn("No model in memory", text)

    def test_extreme_values(self):
        c = o1dashui.render(hostile_state(), 100, 30, "blocks")
        text = c.text()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\x00", text)
        self.assertNotIn("\n\n\n\n", text)
        self.assertIn("model-xxxx", text)
        self.assertIn("...", text)                       # the long name was cut with an ellipsis
        self.assertNotIn("x" * 60, text)                 # and not carried on over the border
        self.assertIn("\u6a21\u578b", text)
        self.assertEqual(o1dashui.ERRORS, [])

    def test_zero_and_full(self):
        st = dash_sample.sample()
        st["gpu"].update(busy_pct=0, vram_used=0, power_w=0)
        st["cpu"]["total"] = 0
        zero = o1dashui.render(st, 100, 30, "ascii").text()
        self.assertIn("Busy   0%", zero)
        st["gpu"].update(busy_pct=100, vram_used=16 * 2**30, power_w=289)
        st["cpu"]["total"] = 100
        full = o1dashui.render(st, 100, 30, "ascii").text()
        self.assertIn("Busy   100%", full)
        self.assertIn("16.0/16.0 GiB", full)
        self.assertEqual(o1dashui.ERRORS, [])

    def test_a_widget_that_fails_blanks_only_its_own_box(self):
        def broken(c, st, ctx):
            c.put(0, 0, "half drawn")
            raise KeyError("boom")
        saved = dict(o1dashui.PANELS)
        try:
            o1dashui.PANELS["gpu"] = (saved["gpu"][0], broken, saved["gpu"][2])
            text = o1dashui.render(dash_sample.sample(), 100, 30, "blocks").text()
            self.assertIn("no data", text)
            self.assertNotIn("half drawn", text)
            self.assertIn("Loaded models", text)
            self.assertIn("Tunnel connected", text)
            self.assertEqual(len(o1dashui.ERRORS), 1)
        finally:
            o1dashui.PANELS.clear()
            o1dashui.PANELS.update(saved)
            del o1dashui.ERRORS[:]

    def test_a_second_render_is_identical(self):
        for w, h in SIZES + SMALL:
            for name, st in states().items():
                a = o1dashui.render(st, w, h, "blocks", 300, 0)
                b = o1dashui.render(st, w, h, "blocks", 300, 0)
                self.assertEqual((a.chars, a.styles), (b.chars, b.styles), (w, h, name))

    def test_ascii_is_ascii(self):
        for w, h in SIZES + SMALL:
            for name, st in states().items():
                text = o1dashui.render(st, w, h, "ascii", 300, 0).text()
                self.assertTrue(all(ord(ch) < 128 for ch in text), (w, h, name))
        self.assertNotIn("?C", o1dashui.render(dash_sample.sample(), 100, 30, "ascii").text())   # the degree sign maps cleanly

    def test_console_draws_only_what_the_font_has(self):
        ok = set(chr(c) for c in range(32, 127)) | o1dashui.NEEDED["blocks"] | {"\n"}
        for w, h in SIZES:
            for name, st in states().items():
                text = o1dashui.render(st, w, h, "blocks", 300, 0, console=True).text()
                self.assertEqual(set(text) - ok, set(), (w, h, name))
        self.assertIn("?", o1dashui.render(hostile_state(), 100, 30, "blocks", console=True).text())  # CJK became '?'
        # on a terminal that is not the console the wide characters are drawn as they are
        self.assertIn("\u6a21\u578b", o1dashui.render(hostile_state(), 100, 30, "blocks").text())

    def test_blocks_draw_blocks(self):
        text = o1dashui.render(dash_sample.sample(), 100, 30, "blocks").text()
        self.assertIn("\u2588", text)
        self.assertTrue(any(ch in text for ch in "\u2581\u2582\u2583\u2584\u2585\u2586\u2587"))
        self.assertFalse(any(0x2800 <= ord(ch) <= 0x28ff for ch in text))

    def test_the_hour_range(self):
        a = o1dashui.render(dash_sample.sample(), 100, 30, "blocks", 300)
        b = o1dashui.render(dash_sample.sample(), 100, 30, "blocks", 3600)
        self.assertIn("5 min", a.text())
        self.assertIn("1 h", b.text())

    def test_warnings(self):
        st = dash_sample.sample()
        self.assertEqual([t for t, _ in o1dashui.warnings(st, st["time"])], ["RAID md127: resync 41%"])
        st["raid"][0].update(healthy=False)
        st["tunnel"]["up"] = False
        st["disks"][2]["mounted"] = False
        st["gpu"]["temps"]["junction"] = 97
        st["updates"]["reboot_required"] = True
        got = o1dashui.warnings(st, st["time"])
        self.assertEqual([s for _, s in got], ["bad", "bad", "bad", "bad", "warn"])
        for t in ("RAID md127 degraded", "Tunnel is down", "/srv/data is not mounted", "GPU very hot: 97 C", "Reboot needed"):
            self.assertIn(t, [x for x, _ in got])
        st["gw"]["stale"] = True
        self.assertIn("Gateway is not running", [t for t, _ in o1dashui.warnings(st, st["time"])])
        text = o1dashui.render(st, 100, 30, "blocks").text()
        self.assertIn("6 PROBLEMS", text)
        self.assertIn("! Gateway is not running", text)
        self.assertIn("+5 more to check", text)

    def test_pairing_overlay(self):
        for w, h in SIZES + [(60, 20), (40, 12), (30, 8)]:
            for g in MODES:
                text = o1dashui.render(dash_sample.sample(pairing=True), w, h, g, 300, 0).text()
                self.assertIn("PAIRING WINDOW OPEN", text)
                self.assertIn("7K4M-2QXD-9FHT", text)
                if (w, h) in SIZES:
                    self.assertGreaterEqual(text.count("\u2588" if g == "blocks" else "#"), 100)
                self.assertNotIn("GPU", text)                    # full screen: nothing else shows
                self.assertEqual({len(r) for r in text.split("\n")}, {w})

    def test_no_content_ever(self):
        marker = "SECRET-PROMPT-MARKER"
        st = dash_sample.sample(marker=marker)
        for w, h in SIZES:
            for g in MODES:
                self.assertNotIn(marker, o1dashui.render(st, w, h, g, 300, 0).text())

    def test_renderer_reads_no_content_fields(self):
        for f in ("o1dashui.py", "o1dashterm.py"):
            with open(os.path.join(U.LIB, f)) as fh:
                tree = ast.parse(fh.read())
            keys = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            for bad in ("prompt", "messages", "content", "response", "recent", "system", "images"):
                self.assertNotIn(bad, keys)

    def test_fast_enough(self):
        st = dash_sample.sample()
        t0 = time.time()
        for _ in range(20):
            o1dashterm.Screen().frame(o1dashui.render(st, 240, 67, "blocks", 3600, 0), 0)
        per = (time.time() - t0) / 20
        self.assertLess(per, 0.05)   # well under 5% of a core at 1 Hz, even on a slow machine


class TestTerminal(unittest.TestCase):
    def render(self, w, h, st=None, **kw):
        return o1dashui.render(st or dash_sample.sample(), w, h, "blocks", 300, 0, **kw)

    def test_the_first_frame_draws_everything_after_a_clear(self):
        for w, h in SIZES:
            c = self.render(w, h)
            out = o1dashterm.Screen().frame(c, 0)
            self.assertTrue(out.startswith(o1dashterm.CLEAR))
            self.assertIn("\x1b[?7l", out)                    # no line wrap: the last cell can't scroll the screen
            vs = VScreen(w, h, fill="X")                         # whatever was there before
            vs.write(out)
            self.assertEqual(vs.rows(), [r for r in c.text().split("\n")])

    def test_rows_are_written_at_absolute_positions(self):
        c = self.render(100, 30)
        out = o1dashterm.Screen().frame(c, 0)
        for y in range(1, 31):
            self.assertEqual(out.count("\x1b[%d;1H" % y), 1, y)
        self.assertNotRegex(out, "\x1b\\[[0-9]*[ABCDEFGIKLMPST]")   # no relative moves, no scrolling, no insert or delete

    def test_nothing_drifts_on_a_second_identical_frame(self):
        c = self.render(100, 30)
        scr = o1dashterm.Screen()
        scr.frame(c, 0)
        self.assertEqual(scr.frame(self.render(100, 30), 1), "")

    def test_only_changed_rows_are_rewritten(self):
        scr = o1dashterm.Screen()
        a = self.render(100, 30)
        vs = VScreen(100, 30)
        vs.write(scr.frame(a, 0))
        st = dash_sample.sample()
        st["gw"]["queued"] = 0
        b = self.render(100, 30, st)
        out = scr.frame(b, 1)
        self.assertNotIn(o1dashterm.CLEAR, out)
        self.assertLess(out.count("\x1b[") , 60)
        self.assertLess(len(out), 800)
        vs.write(out)
        self.assertEqual(vs.rows(), b.text().split("\n"))

    def test_a_resize_clears_the_screen_and_leaves_nothing_stale(self):
        scr = o1dashterm.Screen()
        vs = VScreen(240, 67)
        vs.write(scr.frame(self.render(240, 67), 0))
        for w, h in ((120, 33), (80, 24), (100, 40), (240, 67)):
            vs.resize(w, h)
            vs.cells = [[("X" if (x + y) % 3 else "Y") for x in range(w)] for y in range(h)]    # stale fragments
            before = vs.cleared
            c = self.render(w, h)
            out = scr.frame(c, 5)
            self.assertTrue(out.startswith(o1dashterm.CLEAR), (w, h))
            vs.write(out)
            self.assertEqual(vs.cleared, before + 1)
            self.assertEqual(vs.rows(), c.text().split("\n"), (w, h))

    def test_everything_is_redrawn_now_and_then(self):
        scr = o1dashterm.Screen(refresh_s=30)
        c = self.render(100, 30)
        scr.frame(c, 100)
        self.assertEqual(scr.frame(c, 120), "")
        out = scr.frame(c, 131)
        self.assertNotIn(o1dashterm.CLEAR, out)
        self.assertEqual(len(re.findall("\x1b\\[[0-9]+;1H", out)), 30)                      # all 30 rows, no clear (no flicker)
        vs = VScreen(100, 30, fill="X")                           # a glitch on the glass heals
        vs.write(out)
        self.assertEqual(vs.rows(), c.text().split("\n"))

    def test_forget_clears_again(self):
        scr = o1dashterm.Screen()
        c = self.render(80, 24)
        scr.frame(c, 0)
        scr.forget()
        self.assertTrue(scr.frame(c, 1).startswith(o1dashterm.CLEAR))

    def test_wide_characters_land_in_the_right_cells(self):
        c = self.render(100, 30, hostile_state())
        vs = VScreen(100, 30, fill="X")
        vs.write(o1dashterm.Screen().frame(c, 0))
        self.assertEqual(vs.rows(), c.text().split("\n"))

    def test_the_size_is_read_every_time(self):
        saved = os.get_terminal_size
        sizes = [os.terminal_size((240, 67)), os.terminal_size((120, 33)), OSError(), os.terminal_size((0, 0))]

        def fake(fd):
            v = sizes[0]
            if isinstance(v, Exception):
                raise v
            return v
        os.get_terminal_size = fake
        try:
            self.assertEqual(o1dashterm.term_size(fds=(1,)), (240, 67))
            sizes.pop(0)
            self.assertEqual(o1dashterm.term_size(fds=(1,)), (120, 33))
            sizes.pop(0)
            env = dict(os.environ)
            os.environ.pop("COLUMNS", None)
            os.environ.pop("LINES", None)
            self.assertEqual(o1dashterm.term_size(fds=(1,)), (80, 24))
            os.environ["COLUMNS"], os.environ["LINES"] = "132", "43"
            self.assertEqual(o1dashterm.term_size(fds=(1,)), (132, 43))
            os.environ.clear()
            os.environ.update(env)
            sizes.pop(0)
            self.assertEqual(o1dashterm.term_size(fds=(1,), default=(90, 25)), (90, 25) if "COLUMNS" not in os.environ
                             else (int(os.environ["COLUMNS"]), int(os.environ["LINES"])))
        finally:
            os.get_terminal_size = saved

    @unittest.skipUnless(hasattr(os, "fork"), "needs a pty")
    def test_the_program_follows_a_window_resize(self):
        """ollama1-dash on a pty: a size change (SIGWINCH) makes it clear and draw at the new size."""
        import fcntl
        import pty
        import select
        import termios
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm"
            os.execv(os.path.join(U.BIN, "ollama1-dash"), ["ollama1-dash"])
        try:
            def size(cols, rows):
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

            def collect(secs):
                out, end = b"", time.time() + secs
                while time.time() < end:
                    if select.select([fd], [], [], 0.1)[0]:
                        try:
                            out += os.read(fd, 65536)
                        except OSError:
                            break
                return out.decode("utf-8", "replace")
            size(100, 30)
            first = collect(2.5)
            self.assertIn(o1dashterm.CLEAR, first)
            size(80, 24)
            os.kill(pid, signal.SIGWINCH)
            second = collect(2.0)
            self.assertTrue(second.startswith(o1dashterm.CLEAR) or o1dashterm.CLEAR in second[:200], second[:80])
            vs = VScreen(80, 24, fill="X")
            vs.write(second)
            rows = vs.rows()
            self.assertEqual(len(rows), 24)
            self.assertTrue(all("X" not in r for r in rows))     # nothing of the old frame or the garbage is left
            self.assertIn("GPU", "".join(rows))
            os.write(fd, b"q")
            end = time.time() + 5
            while time.time() < end:
                collect(0.2)
                if os.waitpid(pid, os.WNOHANG)[0]:
                    pid = None
                    break
            self.assertIsNone(pid, "q did not end ollama1-top")
        finally:
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except OSError:
                    pass
            os.close(fd)


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

    # -- 6b444: a reading every half second ----------------------------------------------------------------
    def test_an_hour_of_history_is_an_hour_whatever_the_tick(self):
        self.assertEqual(o1metrics.TICK_S, 0.5)                              # the owner's ask: new numbers twice a second
        self.assertEqual(o1metrics.HISTORY * o1metrics.TICK_S, o1metrics.HISTORY_S)
        self.assertEqual(o1metrics.HISTORY_S, 3600)
        self.assertEqual({d.maxlen for d in o1metrics.Sampler(tunnel_port=1).series.values()}, {7200})
        for tick in (1.0, 0.5, 0.25, 2.0):
            s = o1metrics.Sampler(tunnel_port=1, tick_s=tick)
            self.assertEqual({d.maxlen * tick for d in s.series.values()}, {3600}, tick)

    def rates_at(self, tick, n=4):
        """The sampler read every `tick` seconds of a machine that reads 1 MiB/s from disk, receives 10 kB/s and
        keeps its one core 40% busy (100 jiffies a second): the last state."""
        clock = [1000.0]
        s = o1metrics.Sampler(clock=lambda: clock[0], tunnel_port=1, tick_s=tick)
        s.slow["net"] = (1e12, {"name": "br0"})
        s.slow["tunnel"] = (1e12, {"up": False})
        s.slow["updates"] = (1e12, {})
        self.write("proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        self.write("sys/class/net/br0/statistics/tx_bytes", "500\n")
        for i in range(n):
            t = i * tick
            busy, idle = int(40 * t), int(60 * t)
            self.write("proc/stat", "cpu  %d 0 0 %d 0 0 0 0\ncpu0 %d 0 0 %d 0 0 0 0\n" % (busy, idle, busy, idle))
            self.write("proc/diskstats", "   8 0 sda 1 0 %d 0 1 0 0 0 0 0 0\n" % int(2048 * t))
            self.write("sys/class/net/br0/statistics/rx_bytes", "%d\n" % int(10000 * t))
            clock[0] = 1000.0 + t
            st = s.tick()
        return st

    def test_rates_are_per_second_whatever_the_tick(self):
        for tick in (1.0, 0.5, 0.25):
            st = self.rates_at(tick)
            self.assertEqual(st["tick_s"], tick)
            self.assertEqual(st["io"]["read_bps"], 2048 * 512, tick)           # 1 MiB/s, not per reading
            self.assertEqual(st["net"]["rx_bps"], 10000, tick)
            self.assertEqual(st["cpu"]["total"], 40.0, tick)
            self.assertEqual(st["series"]["io_read"][-1], 2048 * 512, tick)
            self.assertEqual(len(st["series"]["cpu_total"]), 4)

    def test_the_beat(self):
        due = o1metrics.next_due
        self.assertEqual(due(0.0, 1000.0, 0.5), 1000.75)                        # the first: half way between two half seconds
        self.assertEqual(due(1000.75, 1000.75, 0.5), 1001.25)
        self.assertEqual(due(1000.75, 1001.2, 0.5), 1001.25)                    # however long the picture took: on the beat
        self.assertEqual(due(1000.75, 1003.1, 0.5), 1003.75)                    # more than a beat late: again from now
        self.assertEqual(due(10.5, 11.0, 1.0), 11.5)
        self.assertEqual(due(0.0, 100.2), 100.75)                               # TICK_S by default


class TestTrendSpan(unittest.TestCase):
    """6b444: the text dashboard's trends cover 5 minutes, or an hour after `t`, whatever the tick."""

    def test_the_trends_cover_their_range_whatever_the_tick(self):
        for tick in (1.0, 0.5, 0.25):
            n = int(round(3600 / tick))
            st = {"tick_s": tick, "series": {"gpu_busy": [(n - 1 - i) * tick for i in range(n)]}}
            for rng in (300, 3600):
                got = o1dashui._series(st, "gpu_busy", rng)
                self.assertEqual(len(got) * tick, rng, (tick, rng))
                self.assertEqual((got[0], got[-1]), (rng - tick, 0), (tick, rng))      # the oldest is as old as the range

    def test_the_hour_view_draws_with_half_second_samples(self):
        st = dash_sample.sample()
        st["series"] = {k: v + v for k, v in st["series"].items()}            # an hour at two samples a second
        st["tick_s"] = 0.5
        del o1dashui.ERRORS[:]
        for rng in (300, 3600):
            text = o1dashui.render(st, 120, 40, "blocks", rng).text()
            self.assertIn("5 min" if rng == 300 else "1 h", text)
        self.assertEqual(o1dashui.ERRORS, [])


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

    def test_psf2_modes(self):
        self.assertEqual(o1font.glyph_mode(o1font.parse_psf(psf2(self.ascii))[2]), "ascii")
        self.assertEqual(o1font.glyph_mode(o1font.parse_psf(psf2(self.ascii + self.blocks))[2]), "blocks")
        self.assertEqual(o1font.glyph_mode(o1font.parse_psf(psf2(self.ascii + self.blocks[:-1]))[2]), "ascii")
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

    def test_pick_a_big_font_about_120_columns(self):
        fonts = [("/f/Uni3-Terminus16.psf.gz", 8, 16, "blocks"), ("/f/ter-v24n.psf.gz", 12, 24, "blocks"),
                 ("/f/ter-v28n.psf.gz", 14, 28, "blocks"), ("/f/ter-v32n.psf.gz", 16, 32, "blocks"),
                 ("/f/Lat15-Fixed16.psf.gz", 8, 16, "ascii")]
        got = o1font.pick(fonts, (1920, 1080))
        self.assertEqual((got["font"], got["cols"], got["rows"]), ("/f/ter-v32n.psf.gz", 120, 33))
        self.assertEqual(o1font.pick(fonts, (2560, 1440))["font"], "/f/ter-v32n.psf.gz")     # 160 columns, the nearest
        self.assertEqual(o1font.pick(fonts, (1366, 768))["font"], "/f/ter-v24n.psf.gz")      # 113 columns
        self.assertEqual(o1font.pick(fonts[:1], (1920, 1080))["font"], "/f/Uni3-Terminus16.psf.gz")  # nothing bigger: that
        self.assertIsNone(o1font.pick([("/f/x.psf", 8, 16, None)], (1920, 1080)))

    def test_same_size_prefers_blocks_over_ascii(self):
        fonts = [("/f/Lat15-Terminus32x16.psf.gz", 16, 32, "ascii"), ("/f/Uni3-Terminus32x16.psf.gz", 16, 32, "blocks")]
        self.assertEqual(o1font.pick(fonts, (1920, 1080))["glyphs"], "blocks")
        self.assertEqual(o1font.pick(fonts[:1], (1920, 1080))["glyphs"], "ascii")

    def test_off_flag_loads_no_font(self):
        d = tempfile.mkdtemp()
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "", ""
        saved = (o1font.survey, o1font.screen_pixels, o1font.subprocess.run)
        o1font.survey = lambda *a: [("/f/ter-v32n.psf.gz", 16, 32, "blocks")]
        o1font.screen_pixels = lambda *a: (1920, 1080)
        o1font.subprocess.run = lambda cmd, **kw: calls.append(cmd) or Done()
        try:
            flag, state = os.path.join(d, "off"), os.path.join(d, "state.json")
            got = o1font.set_font("/dev/tty1", state=state, off_flag=flag)            # no flag: the font is loaded
            self.assertEqual((calls, got["glyphs"], got["cols"]),
                             ([["setfont", "-C", "/dev/tty1", "/f/ter-v32n.psf.gz"]], "blocks", 120))
            del calls[:]
            open(flag, "w").close()
            got = o1font.set_font("/dev/tty1", state=state, off_flag=flag)            # the flag: nothing is
            self.assertEqual(got, {"glyphs": "ascii", "font": None, "disabled": True})
            self.assertEqual(calls, [])
        finally:
            o1font.survey, o1font.screen_pixels, o1font.subprocess.run = saved
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
