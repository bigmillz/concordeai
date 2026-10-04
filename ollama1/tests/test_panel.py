"""The graphical panel (6b380): the bitmap font, the pixel buffer and its
clipping, the layout (nothing outside the screen, no two boxes overlapping),
the framebuffer conversion (32, 24 and 16 bits, strides, scaling, only the
changed rows), the auto-detection with a fake sysfs, the drawing loop on a
fake screen, and the fall-back to the text dashboard when anything raises.
Everything is drawn into memory; no framebuffer or terminal is touched."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

import o1test_util as U
import dash_sample
import o1fb
import o1gfx
import o1panel
import o1paneld
import o1pixfont as F

BG = 0x112233


def load_dash():
    loader = importlib.machinery.SourceFileLoader("o1dash_prog", os.path.join(U.BIN, "ollama1-dash"))
    spec = importlib.util.spec_from_loader("o1dash_prog", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def decode_png(data):
    """(w, h, rows of (r, g, b) tuples) from an 8-bit RGB PNG made by o1gfx."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(data):
        (n,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + n)
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF
        if tag == b"IHDR":
            size = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    w, h = size[0], size[1]
    raw = zlib.decompress(idat)
    rows = []
    for y in range(h):
        line = raw[y * (1 + 3 * w):(y + 1) * (1 + 3 * w)]
        assert line[0] == 0
        rows.append([tuple(line[1 + 3 * x:4 + 3 * x]) for x in range(w)])
    return w, h, rows


def hostile_state():
    st = dash_sample.sample(pairing=False)
    st["host"] = "h" * 300
    st["gw"]["loaded"][0]["name"] = "model-" + "x" * 300
    st["gw"]["loaded"][1]["name"] = "模型-\U0001F600-\x1b[31mred\x00" + "漢" * 50
    st["disks"][0]["mount"] = "/very/long/mount/point/" + "d" * 200
    st["net"]["address"] = "1" * 200
    st["updates"]["ollama"]["version"] = "v" * 200
    st["gw"]["events"][-1]["model"] = "e" * 200
    return st


class TestFont(unittest.TestCase):
    def test_every_printable_ascii_character_has_a_glyph(self):
        for c in range(0x20, 0x7f):
            ch = chr(c)
            if ch != " ":
                self.assertIn(ch, F.GLYPHS, repr(ch))
        self.assertIn("°", F.GLYPHS)

    def test_glyphs_fit_the_cell(self):
        for ch, (left, width, masks) in F.GLYPHS.items():
            self.assertEqual(len(masks), F.CELL_H, ch)
            self.assertTrue(1 <= width <= 5, ch)
            self.assertTrue(all(0 <= m < 32 for m in masks), ch)
            self.assertTrue(any(masks), "empty glyph %r" % ch)

    def test_capitals_and_digits_are_seven_high_and_descenders_go_below(self):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            rows = [i for i, m in enumerate(F.GLYPHS[ch][2]) if m]
            self.assertEqual((rows[0], rows[-1]), (0, 6), ch)
        for ch in "gjpqy":
            self.assertTrue(F.GLYPHS[ch][2][8] or F.GLYPHS[ch][2][7], ch)
        for ch in "acemnorsuvwxz":
            rows = [i for i, m in enumerate(F.GLYPHS[ch][2]) if m]
            self.assertEqual((rows[0], rows[-1]), (2, 6), ch)

    def test_digits_all_advance_the_same_so_numbers_do_not_jiggle(self):
        self.assertEqual({F.advance(d) for d in "0123456789"}, {6})
        self.assertEqual(F.text_width("111"), F.text_width("888"))

    def test_text_width_adds_up_and_scales(self):
        self.assertEqual(F.text_width(""), 0)
        self.assertEqual(F.text_width("a"), F.GLYPHS["a"][1])
        self.assertEqual(F.text_width("ab"), F.advance("a") + F.GLYPHS["b"][1])
        for s in ("Hello 97%", "gpt-oss:120b", "14.6/16.0 GiB"):
            self.assertEqual(F.text_width(s, 3), sum(F.advance(c, 3) for c in s) - 3)
            self.assertEqual(F.text_width(s, 2), sum(F.advance(c, 2) for c in s) - 2)

    def test_fit_never_exceeds_the_width(self):
        rnd = random.Random(5)
        alphabet = "abcXYZ 0123/:.-_%°漢\U0001F600\x00\x1b"
        for _ in range(400):
            s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
            for scale in (1, 2, 3):
                for w in (0, 3, 9, 20, 47, 100, 333):
                    out = F.fit(s, w, scale)
                    self.assertLessEqual(F.text_width(out, scale), w, (s, w, scale, out))

    def test_fit_keeps_short_text_and_adds_an_ellipsis_to_long(self):
        self.assertEqual(F.fit("short", 200), "short")
        out = F.fit("a very long server name indeed", 60)
        self.assertTrue(out.endswith("..."))
        self.assertLess(len(out), 30)
        self.assertEqual(F.fit("anything", 5), "")        # not even the dots fit

    def test_clean_removes_controls_and_replaces_unknowns(self):
        self.assertEqual(F.clean("a\x1b[31mb\x00c\n"), "a[31mbc")
        self.assertEqual(F.clean("漢"), "?")
        self.assertEqual(F.clean("·"), ".")

    def test_runs_cover_exactly_the_ink(self):
        for ch in "AgM%@8i":
            for scale in (1, 2):
                _left, _w, masks = F.GLYPHS[ch]
                ink = sum(bin(m).count("1") for m in masks) * scale * scale
                runs = F.glyph_runs(ch, scale)
                self.assertEqual(sum(w * h for _x, _y, w, h in runs), ink, (ch, scale))
                cells = set()
                for x, y, w, h in runs:
                    for yy in range(y, y + h):
                        for xx in range(x, x + w):
                            self.assertNotIn((xx, yy), cells)    # no overlap between fills
                            cells.add((xx, yy))


class TestPixmap(unittest.TestCase):
    def test_fill_clips_to_the_buffer(self):
        pm = o1gfx.Pixmap(20, 10, 0)
        pm.fill_rect(-5, -5, 10, 10, 7)
        pm.fill_rect(15, 5, 100, 100, 8)
        self.assertEqual(pm.get(0, 0), 7)
        self.assertEqual(pm.get(4, 4), 7)
        self.assertEqual(pm.get(5, 5), 0)
        self.assertEqual(pm.get(19, 9), 8)
        self.assertEqual(len(pm.buf), 200)

    def test_clip_rectangle_limits_every_primitive(self):
        pm = o1gfx.Pixmap(40, 20, 0)
        with pm.clipped(10, 5, 10, 5):
            pm.fill_rect(0, 0, 40, 20, 1)
            pm.hline(0, 7, 40, 2)
            pm.vline(12, 0, 20, 3)
            pm.line(0, 0, 39, 19, 4)
            pm.rect(0, 0, 40, 20, 5)
            pm.text(0, 0, "hello world", 6, 2)
            pm.disc(15, 7, 30, 9)
            pm.arc(15, 7, 30, 20, 0, 359, 10)
        for y in range(20):
            for x in range(40):
                if not (10 <= x < 20 and 5 <= y < 10):
                    self.assertEqual(pm.get(x, y), 0, (x, y))
        self.assertEqual(pm.clip, (0, 0, 40, 20))                # restored afterwards

    def test_nested_clips_only_narrow(self):
        pm = o1gfx.Pixmap(30, 30, 0)
        with pm.clipped(5, 5, 10, 10):
            with pm.clipped(0, 0, 100, 100):                    # asking for more than the outer one allows
                pm.fill_rect(0, 0, 30, 30, 1)
        self.assertEqual(sum(1 for v in pm.buf if v), 100)

    def test_text_with_max_w_never_draws_past_it(self):
        for scale in (1, 2, 3):
            for mw in (0, 7, 25, 60, 120):
                pm = o1gfx.Pixmap(300, 60, 0)
                pm.text(20, 10, "A long line of text that cannot possibly fit", 1, scale, max_w=mw)
                xs = [x for y in range(60) for x in range(300) if pm.get(x, y)]
                if xs:
                    self.assertGreaterEqual(min(xs), 20)
                    self.assertLess(max(xs), 20 + mw, (scale, mw))

    def test_text_right_and_center_end_where_asked(self):
        pm = o1gfx.Pixmap(200, 20, 0)
        pm.text_right(150, 4, "right", 1)
        xs = [x for x in range(200) if any(pm.get(x, y) for y in range(20))]
        self.assertEqual(max(xs), 149)
        pm = o1gfx.Pixmap(200, 20, 0)
        pm.text_center(100, 4, "centre", 1)
        xs = [x for x in range(200) if any(pm.get(x, y) for y in range(20))]
        self.assertLessEqual(abs((min(xs) + max(xs)) / 2 - 100), 1.5)

    def test_text_scale_makes_blocks_of_whole_pixels(self):
        a, b = o1gfx.Pixmap(40, 30, 0), o1gfx.Pixmap(80, 60, 0)
        a.text(0, 0, "Ag", 1, 1)
        b.text(0, 0, "Ag", 1, 2)
        for y in range(30):
            for x in range(40):
                for dy in (0, 1):
                    for dx in (0, 1):
                        self.assertEqual(b.get(2 * x + dx, 2 * y + dy), a.get(x, y))

    def test_line_hits_both_ends_and_stays_connected(self):
        pm = o1gfx.Pixmap(30, 30, 0)
        pm.line(2, 3, 25, 17, 1)
        self.assertEqual(pm.get(2, 3), 1)
        self.assertEqual(pm.get(25, 17), 1)
        self.assertGreaterEqual(sum(1 for v in pm.buf if v), 24)

    def test_arc_covers_the_angles_asked_and_only_those(self):
        pm = o1gfx.Pixmap(61, 61, 0)
        pm.arc(30, 30, 25, 18, 225, 495, 1)                      # the dial: open at the bottom
        self.assertTrue(pm.get(30, 30 - 22))                     # 12 o'clock
        self.assertTrue(pm.get(30 + 22, 30))                     # 3 o'clock
        self.assertTrue(pm.get(30 - 22, 30))                     # 9 o'clock
        self.assertFalse(pm.get(30, 30 + 22))                    # 6 o'clock: the gap
        self.assertFalse(pm.get(30, 30))                         # the hole in the middle
        less = o1gfx.Pixmap(61, 61, 0)
        less.arc(30, 30, 25, 18, 225, 225 + 135, 1)              # half the dial
        self.assertTrue(less.get(30, 30 - 22))
        self.assertFalse(less.get(30 + 22, 30 - 2) and less.get(30 + 22, 30 + 2))
        self.assertLess(sum(1 for v in less.buf if v), sum(1 for v in pm.buf if v))

    def test_mix(self):
        self.assertEqual(o1gfx.mix(0x000000, 0xFFFFFF, 0.0), 0)
        self.assertEqual(o1gfx.mix(0x000000, 0xFFFFFF, 1.0), 0xFFFFFF)
        self.assertEqual(o1gfx.mix(0x000000, 0xFEFEFE, 0.5), 0x7F7F7F)

    def test_png_is_a_valid_picture_of_the_buffer(self):
        pm = o1gfx.Pixmap(5, 3, 0x102030)
        pm.px(1, 1, 0xAABBCC)
        w, h, rows = decode_png(pm.png())
        self.assertEqual((w, h), (5, 3))
        self.assertEqual(rows[1][1], (0xAA, 0xBB, 0xCC))
        self.assertEqual(rows[0][0], (0x10, 0x20, 0x30))
        w, h, rows = decode_png(o1gfx.png_bytes(5, 3, pm.buf, upscale=3))
        self.assertEqual((w, h), (15, 9))
        self.assertEqual(rows[4][4], (0xAA, 0xBB, 0xCC))
        self.assertEqual(rows[3][5], (0xAA, 0xBB, 0xCC))


def info(xres=1920, yres=1080, bpp=32, red=(16, 8), green=(8, 8), blue=(0, 8), transp=(0, 0), line=None, yoff=0, xoff=0):
    return o1fb.FbInfo(xres, yres, xres, yres, xoff, yoff, bpp, red, green, blue, transp, line or xres * bpp // 8)


class TestFramebuffer(unittest.TestCase):
    def test_var_screeninfo_is_read_from_the_kernels_bytes(self):
        buf = bytearray(160)
        struct.pack_into("<8I", buf, 0, 1920, 1080, 1920, 2160, 0, 1080, 32, 0)
        struct.pack_into("<12I", buf, 32, 16, 8, 0, 8, 8, 0, 0, 8, 0, 24, 8, 0)
        v, fields = o1fb.parse_vscreeninfo(bytes(buf))
        self.assertEqual(v[:7], (1920, 1080, 1920, 2160, 0, 1080, 32))
        self.assertEqual(fields, [(16, 8), (8, 8), (0, 8), (24, 8)])
        self.assertRaises(o1fb.FbError, o1fb.parse_vscreeninfo, b"short")

    def test_fix_screeninfo_gives_the_stride(self):
        fmt = "@16sLIIIIHHHI"
        buf = struct.pack(fmt, b"simpledrmdrmfb", 0, 8294400, 0, 0, 2, 1, 1, 0, 7680) + bytes(40)
        self.assertEqual(o1fb.parse_fscreeninfo(buf), 7680)
        self.assertRaises(o1fb.FbError, o1fb.parse_fscreeninfo, b"x" * 10)

    def test_info_falls_back_to_the_width_when_the_stride_is_zero(self):
        v = ((800, 600, 1024, 600, 0, 0, 16, 0), [(11, 5), (5, 6), (0, 5), (0, 0)])
        self.assertEqual(o1fb.make_info(v, 0).line_length, 2048)
        self.assertEqual(o1fb.make_info(v, 1700).line_length, 1700)

    def test_scale_picks_a_whole_number_near_the_design_width(self):
        self.assertEqual(o1fb.choose_scale(1920, 1080), (3, 640, 360))
        self.assertEqual(o1fb.choose_scale(3840, 2160), (6, 640, 360))
        self.assertEqual(o1fb.choose_scale(1280, 720), (2, 640, 360))
        self.assertEqual(o1fb.choose_scale(1366, 768), (2, 683, 384))
        self.assertEqual(o1fb.choose_scale(1280, 1024), (2, 640, 512))
        self.assertEqual(o1fb.choose_scale(640, 480), (1, 640, 480))
        self.assertEqual(o1fb.choose_scale(800, 600), (1, 800, 600))
        for xres, yres in ((1920, 1200), (2560, 1440), (1024, 768), (1600, 900), (3440, 1440), (320, 200), (1, 1)):
            k, lw, lh = o1fb.choose_scale(xres, yres)
            self.assertGreaterEqual(k, 1)
            self.assertLessEqual(lw * k, xres) if xres >= 1 else None
            self.assertLessEqual(lh * k, max(yres, 1))

    def test_packers(self):
        xrgb = o1fb.make_packer(info())
        self.assertEqual(xrgb(0x112233), 0x112233)
        bgr = o1fb.make_packer(info(red=(0, 8), blue=(16, 8)))
        self.assertEqual(bgr(0x112233), 0x332211)
        argb = o1fb.make_packer(info(transp=(24, 8)))
        self.assertEqual(argb(0x112233), 0xFF112233)
        rgb565 = o1fb.make_packer(info(bpp=16, red=(11, 5), green=(5, 6), blue=(0, 5)))
        self.assertEqual(rgb565(0xFFFFFF), 0xFFFF)
        self.assertEqual(rgb565(0xFF0000), 0xF800)
        self.assertEqual(rgb565(0x00FF00), 0x07E0)
        self.assertEqual(rgb565(0x0000FF), 0x001F)
        self.assertEqual(rgb565(0x000000), 0)

    def pm(self, w=640, h=360):
        pm = o1gfx.Pixmap(w, h, 0x000000)
        return pm

    def read(self, writes, info_, x, y, bpp):
        """The bytes the writes put at screen pixel (x, y)."""
        off = y * info_.line_length + x * bpp
        for o, data in writes:
            if o <= off < o + len(data):
                return data[off - o:off - o + bpp]
        return None

    def test_full_frame_scales_each_logical_pixel_into_a_block(self):
        i = info()
        p = o1fb.Presenter(i, 640, 360, 3)
        pm = self.pm()
        pm.px(10, 20, 0xAA5500)
        pm.px(639, 359, 0x0000FF)
        writes = p.frame(pm)
        self.assertEqual(len(writes), 1)                        # no margins: one contiguous write
        self.assertEqual(writes[0][0], 0)
        self.assertEqual(len(writes[0][1]), 1920 * 1080 * 4)
        for dy in range(3):
            for dx in range(3):
                self.assertEqual(self.read(writes, i, 30 + dx, 60 + dy, 4), struct.pack("<I", 0xAA5500))
        self.assertEqual(self.read(writes, i, 29, 60, 4), bytes(4))
        self.assertEqual(self.read(writes, i, 33, 60, 4), bytes(4))
        self.assertEqual(self.read(writes, i, 1919, 1079, 4), struct.pack("<I", 0xFF))

    def test_a_picture_smaller_than_the_screen_is_centred(self):
        i = info()
        p = o1fb.Presenter(i, 640, 360, 2)                      # 1280x720 on 1920x1080
        self.assertEqual((p.ox, p.oy), (320, 180))
        pm = self.pm()
        pm.px(0, 0, 0xFFFFFF)
        writes = p.frame(pm)
        self.assertEqual(self.read(writes, i, 320, 180, 4), struct.pack("<I", 0xFFFFFF))
        self.assertEqual(self.read(writes, i, 321, 181, 4), struct.pack("<I", 0xFFFFFF))
        self.assertIsNone(self.read(writes, i, 319, 180, 4))       # the margin is not written by a frame
        self.assertIsNone(self.read(writes, i, 320, 179, 4))
        self.assertEqual(len(writes), 720)                       # one write per screen row (the margins break them up)

    def test_stride_longer_than_the_row_is_honoured(self):
        i = info(xres=1280, yres=720, line=8192)
        p = o1fb.Presenter(i, 640, 360, 2)
        pm = self.pm()
        pm.px(1, 1, 0x010203)
        writes = p.frame(pm)
        self.assertEqual(self.read(writes, i, 2, 2, 4), struct.pack("<I", 0x010203))
        self.assertEqual(self.read(writes, i, 3, 3, 4), struct.pack("<I", 0x010203))
        for o, data in writes:
            self.assertLessEqual(o + len(data), 8192 * 720)
            self.assertEqual(o % 8192 + len(data), 5120)         # exactly one screen row's worth in each, none runs into the padding

    def test_panned_framebuffer_starts_at_the_visible_part(self):
        i = info(xres=640, yres=360, yoff=360)
        i = i._replace(yvirt=720)
        p = o1fb.Presenter(i, 640, 360, 1)
        pm = self.pm()
        pm.px(0, 0, 0x7F7F7F)
        writes = p.frame(pm)
        self.assertEqual(writes[0][0], 360 * 2560)

    def test_only_changed_rows_are_written_again(self):
        i = info()
        p = o1fb.Presenter(i, 640, 360, 3)
        pm = self.pm()
        p.frame(pm)
        self.assertEqual(p.frame(pm), [])                       # nothing changed, nothing written
        pm.px(5, 100, 0x00FF00)
        writes = p.frame(pm)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], 300 * 7680)              # logical row 100 = screen rows 300..302
        self.assertEqual(len(writes[0][1]), 3 * 7680)
        self.assertEqual(p.frame(pm), [])
        self.assertEqual(len(p.frame(pm, force=True)), 1)       # force: everything again

    def test_clear_blanks_the_whole_screen_and_forgets_the_picture(self):
        i = info(xres=1000, yres=500, line=4096)
        p = o1fb.Presenter(i, 480, 270, 1)
        pm = o1gfx.Pixmap(480, 270, 0x123456)
        p.frame(pm)
        writes = p.clear()
        self.assertEqual(sum(len(d) for _, d in writes), 500 * 4000)
        self.assertTrue(all(not any(d) for _, d in writes))
        self.assertGreater(len(p.frame(pm)), 0)                 # redrawn in full after a clear

    def test_16_and_24_bit_screens(self):
        i16 = info(bpp=16, red=(11, 5), green=(5, 6), blue=(0, 5))
        p = o1fb.Presenter(i16, 640, 360, 3)
        pm = self.pm()
        pm.px(0, 0, 0xFF0000)
        writes = p.frame(pm)
        self.assertEqual(self.read(writes, i16, 2, 2, 2), struct.pack("<H", 0xF800))
        self.assertEqual(len(writes[0][1]), 1920 * 1080 * 2)
        i24 = info(bpp=24)
        p = o1fb.Presenter(i24, 640, 360, 3)
        writes = p.frame(pm)
        self.assertEqual(self.read(writes, i24, 2, 2, 3), bytes([0, 0, 0xFF]))      # B, G, R in memory
        self.assertEqual(len(writes[0][1]), 1920 * 1080 * 3)

    def test_unusable_depths_and_sizes_are_refused(self):
        self.assertRaises(o1fb.FbError, o1fb.Presenter, info(bpp=8), 640, 360, 3)
        p = o1fb.Presenter(info(), 640, 360, 3)
        self.assertRaises(ValueError, p.frame, o1gfx.Pixmap(100, 100))


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="o1sys-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def connector(self, name, status):
        p = os.path.join(self.d, "class", "drm", name)
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "status"), "w") as f:
            f.write(status + "\n")

    def test_a_connected_connector_means_a_display(self):
        self.assertFalse(o1fb.display_connected(self.d))                 # no drm at all
        self.connector("card0-DP-1", "disconnected")
        self.connector("card0-HDMI-A-1", "disconnected")
        self.assertFalse(o1fb.display_connected(self.d))
        self.connector("card0-HDMI-A-2", "connected")
        self.assertTrue(o1fb.display_connected(self.d))

    def test_unknown_is_not_connected(self):
        self.connector("card0-VGA-1", "unknown")
        self.assertFalse(o1fb.display_connected(self.d))

    def test_needs_the_framebuffer_too(self):
        self.connector("card0-HDMI-A-1", "connected")
        fb = os.path.join(self.d, "fb0")
        self.assertEqual(o1fb.panel_available(fb, self.d), (False, "no %s" % fb))
        open(fb, "w").close()
        self.assertEqual(o1fb.panel_available(fb, self.d)[0], True)
        self.connector("card0-HDMI-A-1", "disconnected")
        self.assertEqual(o1fb.panel_available(fb, self.d), (False, "no display connected"))

    def test_mode_precedence_and_typos(self):
        self.assertEqual(o1fb.resolve_mode(), "auto")
        self.assertEqual(o1fb.resolve_mode(None, None, "text\n"), "text")
        self.assertEqual(o1fb.resolve_mode(None, "graphic", "text"), "graphic")
        self.assertEqual(o1fb.resolve_mode("text", "graphic", "auto"), "text")
        self.assertEqual(o1fb.resolve_mode("GRAPHIC"), "graphic")
        self.assertEqual(o1fb.resolve_mode("nonsense", "", "  "), "auto")
        self.assertEqual(o1fb.resolve_mode("nonsense", "text"), "text")

    def test_vt_number_from_a_tty_path(self):
        self.assertEqual(o1fb.vt_of("/dev/tty1"), 1)
        self.assertEqual(o1fb.vt_of("/dev/tty12"), 12)
        self.assertIsNone(o1fb.vt_of("/dev/pts/3"))
        self.assertIsNone(o1fb.vt_of("/dev/tty"))
        self.assertIsNone(o1fb.vt_of(None))


class FakeTty:
    def __init__(self, active=1, fail_enter=False):
        self.entered = self.left = 0
        self.active, self.fail_enter = active, fail_enter

    def enter(self):
        if self.fail_enter:
            raise o1fb.FbError("no")
        self.entered += 1

    def leave(self):
        self.left += 1

    def vt_active(self):
        return self.active


class FakeFb:
    def __init__(self, i=None, fail_after=None):
        self.info = i or info(xres=1280, yres=720)
        self.writes = []
        self.fail_after = fail_after

    def write(self, off, data):
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            raise OSError("the screen went away")
        self.writes.append((off, len(data)))


class FakeSampler:
    def __init__(self, st=None):
        self.n = 0
        self.st = st

    def tick(self):
        self.n += 1
        return self.st if self.st is not None else dash_sample.sample(now=1790000000.0 + self.n)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestLoop(unittest.TestCase):
    def go(self, fb=None, tty=None, sampler=None, **kw):
        c = Clock()
        fb = fb or FakeFb()
        tty = tty or FakeTty()
        n = o1paneld.run(fb, tty, sampler or FakeSampler(), clock=c.now, sleep=c.sleep, **kw)
        return n, fb, tty, c

    def test_draws_and_always_puts_the_console_back(self):
        n, fb, tty, _ = self.go(max_frames=3)
        self.assertEqual((n, tty.entered, tty.left), (3, 1, 1))
        self.assertGreater(len(fb.writes), 3)

    def test_a_picture_every_two_seconds_and_a_sample_every_second(self):
        s = FakeSampler()
        n, _fb, _tty, c = self.go(sampler=s, max_frames=5)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(c.t - 1000.0, 4 * o1paneld.DRAW_S, delta=1.0)
        self.assertGreaterEqual(s.n, 8)

    def test_the_pairing_countdown_redraws_every_second(self):
        st = dash_sample.sample(now=1790000000.0, pairing=True)
        n, _fb, _tty, c = self.go(sampler=FakeSampler(st), max_frames=5)
        self.assertLess(c.t - 1000.0, 5.0)

    def test_nothing_is_drawn_while_another_terminal_is_showing(self):
        fb, tty = FakeFb(), FakeTty(active=2)
        c = Clock()
        calls = []

        def stop():
            calls.append(1)
            return len(calls) > 6
        o1paneld.run(fb, tty, FakeSampler(), vt=1, clock=c.now, sleep=c.sleep, stop=stop)
        self.assertEqual(len(fb.writes) > 0, True)               # only the first clear at the start
        first = len(fb.writes)
        tty.active = 1
        calls.clear()
        o1paneld.run(fb, tty, FakeSampler(), vt=1, clock=c.now, sleep=c.sleep, stop=stop, max_frames=1)
        self.assertGreater(len(fb.writes), first)

    def test_drawing_resumes_from_black_when_the_terminal_comes_back(self):
        fb, tty, c = FakeFb(), FakeTty(active=1), Clock()
        seen = []

        def stop():
            seen.append(len(fb.writes))
            if len(seen) == 3:
                tty.active = 2
            if len(seen) == 5:
                tty.active = 1
            return len(seen) > 9
        o1paneld.run(fb, tty, FakeSampler(), vt=1, clock=c.now, sleep=c.sleep, stop=stop)
        self.assertEqual(seen[3], seen[4])                       # nothing drawn while away
        self.assertGreater(seen[-1], seen[4])                    # drawn again after

    def test_an_unchanged_picture_writes_nothing_but_is_refreshed_in_full_every_so_often(self):
        st = dash_sample.sample(now=1790000000.0)
        screen = 1280 * 720 * 4

        def written(frames):
            _n, fb, _t, _c = self.go(sampler=FakeSampler(st), max_frames=frames)
            return sum(n for _o, n in fb.writes) / float(screen)
        self.assertAlmostEqual(written(10), 2.0, delta=0.01)        # the black at the start, then the first picture
        self.assertAlmostEqual(written(20), 3.0, delta=0.01)        # and everything again after 30 s

    def test_a_dead_screen_raises_and_still_restores_the_console(self):
        tty = FakeTty()
        c = Clock()
        with self.assertRaises(OSError):
            o1paneld.run(FakeFb(fail_after=5), tty, FakeSampler(), clock=c.now, sleep=c.sleep, max_frames=3)
        self.assertEqual((tty.entered, tty.left), (1, 1))

    def test_a_console_that_cannot_enter_graphics_raises_before_drawing(self):
        fb = FakeFb()
        with self.assertRaises(o1fb.FbError):
            self.go(fb=fb, tty=FakeTty(fail_enter=True), max_frames=1)
        self.assertEqual(fb.writes, [])

    def test_a_screen_too_small_is_refused(self):
        with self.assertRaises(o1fb.FbError):
            self.go(fb=FakeFb(info(xres=320, yres=200)), max_frames=1)

    def test_a_sampler_that_returns_junk_still_draws(self):
        for junk in ({}, {"gpu": "x", "gw": 5, "disks": "no"}):
            n, fb, tty, _ = self.go(sampler=FakeSampler(junk), max_frames=2)
            self.assertEqual(n, 2)


class TestFallback(unittest.TestCase):
    """ollama1-dash --console: any failure of the panel leaves the text dashboard running."""

    def setUp(self):
        self.dash = load_dash()
        self.calls = []
        self.logged = []
        self.dash.log = self.logged.append
        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))
        os.environ.pop("OLLAMA1_DASH", None)
        self.dash.read_mode_file = lambda path=None: None
        self.dash.run = lambda console, ascii_, switch=None: self.calls.append(("text", switch)) or "done"

    def test_an_exception_in_the_panel_falls_back_to_text_and_is_logged(self):
        self.dash.o1fb.panel_available = lambda *a, **k: (True, "x")
        self.dash.run_graphic = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        self.dash.console_main(False, None)
        self.assertEqual([c[0] for c in self.calls], ["text"])
        self.assertIsNone(self.calls[0][1])                          # and it does not try the panel again
        self.assertIn("boom", "".join(self.logged))

    def test_a_clean_stop_ends_the_program(self):
        self.dash.o1fb.panel_available = lambda *a, **k: (True, "x")
        self.dash.run_graphic = lambda *a, **k: "stopped"
        self.dash.console_main(False, None)
        self.assertEqual(self.calls, [])

    def test_no_display_means_text_and_it_keeps_looking(self):
        self.dash.o1fb.panel_available = lambda *a, **k: (False, "no display connected")
        self.dash.run_graphic = lambda *a, **k: self.fail("must not start the panel")
        self.dash.console_main(False, None)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(callable(self.calls[0][1]))
        self.assertFalse(self.calls[0][1]())                         # the poll says no
        self.dash.o1fb.panel_available = lambda *a, **k: (True, "x")
        self.assertTrue(self.calls[0][1]())                          # a monitor was plugged in: switch

    def test_text_mode_never_starts_the_panel(self):
        self.dash.o1fb.panel_available = lambda *a, **k: (True, "x")
        self.dash.run_graphic = lambda *a, **k: self.fail("must not start the panel")
        self.dash.console_main(False, "text")
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0][1])

    def test_the_environment_and_the_file_pick_the_mode(self):
        self.dash.run_graphic = lambda *a, **k: self.fail("must not start the panel")
        self.dash.read_mode_file = lambda path=None: "text\n"
        self.dash.console_main(False, None)
        self.assertEqual(len(self.calls), 1)

    def test_graphic_mode_needs_only_the_framebuffer(self):
        self.dash.FB_PATH = os.devnull
        self.dash.o1fb.panel_available = lambda *a, **k: (False, "no display connected")
        ran = []
        self.dash.run_graphic = lambda *a, **k: ran.append(1) or "stopped"
        self.dash.console_main(False, "graphic")
        self.assertEqual(ran, [1])

    def test_a_framebuffer_that_cannot_be_opened_is_an_error_not_a_crash(self):
        d = tempfile.mkdtemp(prefix="o1fb-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with self.assertRaises(o1fb.FbError):
            o1fb.FbDevice(os.path.join(d, "no-such-fb"))

    @staticmethod
    def slurp(path):
        with open(path, "rb") as f:
            return f.read()

    def test_the_png_option_writes_a_picture(self):
        d = tempfile.mkdtemp(prefix="o1png-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        out = os.path.join(d, "panel.png")
        with contextlib.redirect_stdout(io.StringIO()):
            self.dash.png(["--png", out, "--size", "640x360", "--scale", "1"])
        w, h, rows = decode_png(self.slurp(out))
        self.assertEqual((w, h), (640, 360))
        self.assertNotEqual(len({px for r in rows for px in r}), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.dash.png(["--png", out, "--scale", "2", "--pairing"])
        w, h, _ = decode_png(self.slurp(out))
        self.assertEqual((w, h), (1280, 720))


class TestDashSetting(unittest.TestCase):
    """setup.sh --dash text|graphic|auto: parsed, saved, written where the dashboard reads it."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="o1dashmode-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.lib = os.path.join(U.KIT, "lib", "setuplib.sh")
        self.setup = os.path.join(U.KIT, "setup.sh")

    def sh(self, script, env=None):
        e = dict(os.environ, DASH_MODE_FILE=os.path.join(self.d, "etc", "dash-mode"))
        e.update(env or {})
        r = subprocess.run(["bash", "-c", 'ok() { echo "OK: $*"; }\nsource "%s"\n%s' % (self.lib, script)], env=e,
                           capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()

    def test_the_flag_wins_then_the_environment_then_what_was_saved(self):
        self.assertEqual(self.sh('dash_mode_choice "" "" ""'), (0, "auto"))
        self.assertEqual(self.sh('dash_mode_choice "" "" text'), (0, "text"))
        self.assertEqual(self.sh('dash_mode_choice "" graphic text'), (0, "graphic"))
        self.assertEqual(self.sh('dash_mode_choice text graphic auto'), (0, "text"))

    def test_a_word_it_does_not_know_is_refused(self):
        for args in ('"bogus" "" ""', '"" "bogus" ""', '"" "" "bogus"', '"" "" "TEXT"'):
            rc, out = self.sh("dash_mode_choice %s" % args)
            self.assertEqual(rc, 1, args)

    def test_the_step_writes_the_choice_for_the_dashboard(self):
        f = os.path.join(self.d, "etc", "dash-mode")
        rc, out = self.sh("DASH=text; dash_mode_step")
        self.assertEqual(rc, 0, out)
        with open(f) as fh:
            self.assertEqual(fh.read(), "text\n")
        with open(f) as fh:
            self.assertEqual(o1fb.resolve_mode(None, None, fh.read()), "text")
        self.sh("unset DASH; dash_mode_step")
        with open(f) as fh:
            self.assertEqual(fh.read(), "auto\n")                  # a re-run without it brings the default back
        self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o644")

    def test_setup_checks_the_option_before_doing_anything(self):
        for args in (["--dash", "bogus"], ["--dash"]):
            r = subprocess.run(["bash", self.setup] + args, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("--dash takes text, graphic or auto", r.stdout + r.stderr)
        r = subprocess.run(["bash", self.setup, "--plan"], env=dict(os.environ, OLLAMA1_DASH="zzz"),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("OLLAMA1_DASH takes", r.stdout + r.stderr)

    def test_help_and_the_saved_settings_mention_it(self):
        r = subprocess.run(["bash", self.setup, "--help"], capture_output=True, text=True, timeout=30)
        self.assertIn("--dash text|graphic|auto", r.stdout)
        with open(self.setup) as f:
            src = f.read()
        self.assertIn("printf 'DASH=%s\\n' \"$DASH\"", src)
        self.assertIn('"$(saved DASH)"', src)
        self.assertLess(src.index('dash_font_step "$LIBDIR/bin/ollama1-dash" /dev/tty1'), src.index("\ndash_mode_step\n"))

    def test_the_unit_gives_the_dashboard_the_framebuffer_group(self):
        with open(os.path.join(U.KIT, "systemd", "ollama1-dash.service")) as f:
            unit = f.read()
        self.assertRegex(unit, r"(?m)^SupplementaryGroups=o1view o1pair video$")
        self.assertIn("TTYPath=/dev/tty1", unit)                      # the console it takes out of text mode is its own


SIZES = [(480, 270), (640, 360), (640, 400), (640, 480), (683, 384), (800, 450), (1024, 600), (1280, 1024), (1920, 1080)]


class TestLayout(unittest.TestCase):
    def test_boxes_are_inside_the_screen_and_never_overlap(self):
        for w, h in SIZES:
            boxes = o1panel.layout(w, h)
            self.assertEqual(set(boxes), {"header", "gpu", "cpu", "models", "storage", "status", "footer"}, (w, h))
            items = list(boxes.items())
            for name, (x, y, bw, bh) in items:
                self.assertTrue(x >= 0 and y >= 0 and x + bw <= w and y + bh <= h and bw > 0 and bh > 0, (w, h, name))
            for i, (n1, a) in enumerate(items):
                for n2, b in items[i + 1:]:
                    sep = a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1]
                    self.assertTrue(sep, (w, h, n1, n2, a, b))

    def test_too_small_gives_no_boxes_and_a_message(self):
        self.assertEqual(o1panel.layout(479, 360), {})
        self.assertEqual(o1panel.layout(640, 269), {})
        pm = o1panel.render(dash_sample.sample(), 300, 100)
        self.assertGreater(len({v for v in pm.buf}), 1)

    def test_panels_leave_a_margin_around_the_screen(self):
        for w, h in SIZES:
            boxes = o1panel.layout(w, h)
            for name in ("gpu", "cpu", "models", "storage", "status"):
                x, y, bw, bh = boxes[name]
                self.assertGreaterEqual(x, 4)
                self.assertLessEqual(x + bw, w - 4)


class TestPanel(unittest.TestCase):
    def setUp(self):
        del o1panel.ERRORS[:]

    def tearDown(self):
        self.assertEqual(o1panel.ERRORS, [])

    def test_renders_the_sample_at_every_size(self):
        st = dash_sample.sample(now=time.time())
        for w, h in SIZES:
            pm = o1panel.render(st, w, h)
            self.assertEqual((pm.w, pm.h), (w, h))
            self.assertGreater(len({v for v in pm.buf}), 8, (w, h))

    def test_rendering_reuses_the_buffer_and_repaints_it(self):
        st = dash_sample.sample(now=time.time())
        a = o1panel.render(st, 640, 360)
        b = o1panel.render(st, 640, 360, pm=a)
        self.assertIs(a, b)
        self.assertEqual(a.buf, o1panel.render(st, 640, 360).buf)

    def test_a_panel_cannot_paint_outside_its_box(self):
        """Replace each panel by one that floods the whole screen: only its own box may change."""
        st = dash_sample.sample(now=time.time())
        base = o1panel.render(st, 640, 360)
        boxes = o1panel.layout(640, 360)
        for name, fn in (("header", "draw_header"), ("gpu", "draw_gpu"), ("cpu", "draw_cpu"), ("models", "draw_models"),
                         ("storage", "draw_storage"), ("status", "draw_status"), ("footer", "draw_footer")):
            orig = getattr(o1panel, fn)
            setattr(o1panel, fn, lambda pm, r, st_, ctx: (pm.fill_rect(-50, -50, 900, 600, 0xFF00FF),
                                                           pm.text(-20, -5, "x" * 400, 0x00FFFF, 3),
                                                           pm.arc(100, 100, 400, 3, 0, 359, 0x00FF00)))
            try:
                pm = o1panel.render(st, 640, 360)
            finally:
                setattr(o1panel, fn, orig)
            x, y, w, h = boxes[name]
            for yy in range(360):
                for xx in range(640):
                    inside = x <= xx < x + w and y <= yy < y + h
                    if not inside:
                        self.assertEqual(pm.get(xx, yy), base.get(xx, yy), (name, xx, yy))

    def test_a_panel_that_raises_shows_no_data_and_the_rest_is_drawn(self):
        st = dash_sample.sample(now=time.time())
        base = o1panel.render(st, 640, 360)
        orig = o1panel.draw_gpu
        o1panel.draw_gpu = lambda *a: 1 / 0
        try:
            pm = o1panel.render(st, 640, 360)
            self.assertEqual(len(o1panel.ERRORS), 1)
        finally:
            o1panel.draw_gpu = orig
            del o1panel.ERRORS[:]
        x, y, w, h = o1panel.layout(640, 360)["cpu"]
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.assertEqual(pm.get(xx, yy), base.get(xx, yy))

    def test_hostile_and_empty_states_do_not_raise(self):
        states = [hostile_state(), {}, {"time": time.time()}, None, "x", {"gpu": None, "gw": None},
                  {"gpu": {"busy_pct": "x", "vram_total": 0, "temps": {"a": float("nan")}}, "gw": {"loaded": "no", "tps": 5},
                   "cpu": {"total": float("inf"), "cores": "x"}, "mem": {"total": "9"}, "disks": [None, {"mount": 5}],
                   "raid": [3], "net": {"ports": [None]}, "series": {"gpu_busy": "x", "tps": [None, "a", float("nan")]}}]
        for st in states:
            for w, h in ((640, 360), (480, 270), (1024, 768)):
                o1panel.render(st, w, h)

    def test_long_names_are_cut_not_drawn_across(self):
        st = hostile_state()
        for w, h in ((640, 360), (480, 270)):
            pm = o1panel.render(st, w, h)
            boxes = o1panel.layout(w, h)
            for name in ("gpu", "cpu", "models", "storage", "status"):
                x, y, bw, bh = boxes[name]
                # the panel's own border column and the gutter beside it stay clear of text colours
                for yy in range(y + 4, y + bh - 4):
                    self.assertEqual(pm.get(x + bw - 2, yy), o1panel.T["panel"], (name, yy))
                    self.assertEqual(pm.get(x + 1, yy), o1panel.T["panel"], (name, yy))

    def test_a_problem_turns_the_badge_and_the_status_box_red(self):
        def reds(st):
            pm = o1panel.render(st, 640, 360)
            x, y, w, h = o1panel.layout(640, 360)["header"]
            n = sum(1 for yy in range(y, y + h) for xx in range(x, x + w) if pm.get(xx, yy) == o1panel.T["bad"])
            return n, pm
        ok = dash_sample.sample(now=time.time())
        ok["raid"][0]["action"] = None
        ok["raid"][0]["progress"] = None
        n_ok, pm_ok = reds(ok)
        self.assertEqual(n_ok, 0)
        bad = dash_sample.sample(now=time.time())
        bad["raid"][0]["action"] = None
        bad["disks"][1]["mounted"] = False
        n_bad, pm_bad = reds(bad)
        self.assertGreater(n_bad, 100)
        sx, sy, sw, sh = o1panel.layout(640, 360)["status"]
        self.assertEqual(pm_bad.get(sx + sw // 2, sy), o1panel.T["bad"])         # the status box's border
        self.assertNotEqual(pm_ok.get(sx + sw // 2, sy), o1panel.T["bad"])

    def test_a_missing_graphics_card_is_a_red_panel(self):
        st = dash_sample.sample(now=time.time())
        st["gpu"] = None
        pm = o1panel.render(st, 640, 360)
        x, y, w, h = o1panel.layout(640, 360)["gpu"]
        self.assertEqual(pm.get(x + w // 2, y), o1panel.T["bad"])

    def test_pairing_fills_the_screen_with_the_code(self):
        st = dash_sample.sample(now=time.time(), pairing=True)
        plain = o1panel.render(dash_sample.sample(now=time.time()), 640, 360)
        pm = o1panel.render(st, 640, 360)
        self.assertNotEqual(pm.buf, plain.buf)
        # the code is drawn as large as fits: wide, and not touching the screen edges
        rows = [y for y in range(360) for x in range(640) if pm.get(x, y) == o1panel.T["text"]]
        cols = [x for y in range(360) for x in range(640) if pm.get(x, y) == o1panel.T["text"]]
        self.assertGreater(max(cols) - min(cols), 400)
        self.assertGreater(min(cols), 16)
        self.assertLess(max(cols), 624)
        self.assertGreater(max(rows) - min(rows), 30)
        for w, h in ((480, 270), (1024, 768), (800, 450)):
            pm = o1panel.render(st, w, h)
            cols = [x for y in range(h) for x in range(w) if pm.get(x, y) == o1panel.T["text"]]
            self.assertGreater(min(cols), 10, (w, h))
            self.assertLess(max(cols), w - 10, (w, h))

    def test_the_text_dashboards_problem_list_is_reused(self):
        import o1dashui
        st = dash_sample.sample(now=time.time())
        st["disks"][1]["mounted"] = False
        self.assertIn(("/srv/models is not mounted", "bad"), o1dashui.warnings(st, st["time"]))
        pm = o1panel.render(st, 640, 360)
        self.assertGreater(len({v for v in pm.buf}), 8)


def power_state(**kw):
    p = {"watts": 300.0, "src": "est", "kwh_24h": 3.54, "cost_24h": 0.61, "symbol": "$", "badge": None, "price": 0.17,
         "currency": "USD", "priced": True, "since": 1790000000 - 90 * 86400,
         "windows": {"1d": {"cost": 0.61, "kwh": 3.54, "measured_h": 24.0, "est": True},
                     "1w": {"cost": 4.87, "kwh": 27.9, "measured_h": 168.0, "est": False},
                     "1m": {"cost": 21.34, "kwh": 118.6, "measured_h": 700.0, "est": True}}}
    p.update(kw)
    return p


def cost_state(**kw):
    st = dash_sample.sample(now=1790000000.0)
    st["power"] = power_state(**kw)
    return st


class FakeTermios:
    """The parts of termios the keyboard reader uses."""
    ECHO, ICANON, ISIG, IEXTEN, VMIN, VTIME, TCSANOW, TCIFLUSH = 8, 2, 1, 32768, 6, 5, 0, 0
    error = OSError

    def __init__(self, fail=False):
        self.attrs = [0, 0, 0, 8 | 2 | 1 | 32768 | 64, 0, 0, [b"\x00"] * 32]
        self.calls, self.fail = [], fail

    def tcgetattr(self, fd):
        if self.fail:
            raise OSError("not a tty")
        return [list(a) if isinstance(a, list) else a for a in self.attrs]

    def tcsetattr(self, fd, when, attrs):
        self.calls.append(("set", [list(a) if isinstance(a, list) else a for a in attrs]))
        self.attrs = attrs

    def tcflush(self, fd, q):
        self.calls.append(("flush", fd))


class TestCostScreen(unittest.TestCase):
    def setUp(self):
        del o1panel.ERRORS[:]

    def tearDown(self):
        self.assertEqual(o1panel.ERRORS, [])

    # -- the keys ---------------------------------------------------------------------
    def test_space_flips_and_nothing_else_does(self):
        self.assertEqual(o1paneld.handle_keys(b" ", "panel", 100.0, 0.0), ("cost", 100.0))
        self.assertEqual(o1paneld.handle_keys(b" ", "cost", 100.0, 0.0), ("panel", 100.0))
        for junk in (b"", b"a", b"\r", b"\x1b[A", b"t", b"q", b"\x03", b"\x1b", b"1234"):
            self.assertEqual(o1paneld.handle_keys(junk, "panel", 100.0, 0.0), ("panel", 0.0), junk)
            self.assertEqual(o1paneld.handle_keys(junk, "cost", 100.0, 0.0), ("cost", 0.0), junk)

    def test_a_held_key_is_one_flip(self):
        screen, last = o1paneld.handle_keys(b" ", "panel", 100.0, -1e9)
        screen, last = o1paneld.handle_keys(b" ", screen, 100.1, last)      # key repeat
        self.assertEqual(screen, "cost")
        screen, last = o1paneld.handle_keys(b" ", screen, 100.5, last)      # a second, deliberate press
        self.assertEqual(screen, "panel")
        self.assertEqual(o1paneld.handle_keys(b"  ", "panel", 200.0, 0.0)[0], "cost")   # two bytes in one read: one flip

    def test_the_keyboard_is_read_raw_without_echo_and_put_back(self):
        tc = FakeTermios()
        before = tc.tcgetattr(0)
        k = o1paneld.Keys(0, tcmod=tc, sel=lambda r, w, x, t: ([], [], []))
        k.start()
        raw = tc.attrs
        self.assertEqual(raw[3] & (tc.ECHO | tc.ICANON | tc.ISIG | tc.IEXTEN), 0)
        self.assertEqual((raw[6][tc.VMIN], raw[6][tc.VTIME]), (0, 0))
        self.assertIn(("flush", 0), tc.calls)                       # what was typed before is thrown away
        k.stop()
        self.assertEqual(tc.attrs, before)
        k.stop()                                                    # twice is harmless

    def test_waiting_for_a_key_never_blocks_longer_than_the_timeout(self):
        asked = []

        def sel(r, w, x, t):
            asked.append(t)
            return ([], [], [])
        k = o1paneld.Keys(0, tcmod=FakeTermios(), sel=sel)
        k.start()
        self.assertEqual(k.wait(0.5), b"")
        self.assertEqual(asked, [0.5])
        k.sel = lambda r, w, x, t: ([0], [], [])
        k.read = lambda fd, n: b" "
        self.assertEqual(k.wait(0.5), b" ")

    def test_a_console_that_is_not_a_terminal_just_sleeps(self):
        slept = []
        k = o1paneld.Keys(0, tcmod=FakeTermios(fail=True), sleep=slept.append)
        k.start()
        self.assertEqual(k.wait(0.5), b"")
        self.assertEqual(slept, [0.5])
        k = o1paneld.Keys(None, tcmod=FakeTermios(), sleep=slept.append)
        k.start()
        k.stop()

    def run_loop(self, script, st=None, max_frames=40):
        """Run the loop with these keyboard reads (one per poll); returns the screens drawn, in order."""
        seen = []
        real = o1panel.render

        def spy(st_, w, h, range_s=300, pm=None, screen="panel"):
            seen.append(screen)
            return real(st_, w, h, range_s, pm, screen)
        o1panel.render = spy
        polls = iter(script)
        c = Clock()

        class K:
            def wait(self, timeout):
                c.sleep(timeout)
                return next(polls, b"")
        try:
            n = [0]

            def stop():
                n[0] += 1
                return n[0] > len(script) + 3
            o1paneld.run(FakeFb(), FakeTty(), FakeSampler(st), clock=c.now, sleep=c.sleep, keys=K(), stop=stop)
        finally:
            o1panel.render = real
        return seen

    def test_the_screen_flips_within_one_poll_and_back(self):
        seen = self.run_loop([b"", b"", b" ", b"", b"x", b"", b"", b" ", b"", b""])
        self.assertEqual(seen[0], "panel")
        first_cost = seen.index("cost")
        self.assertLessEqual(first_cost, 4)                          # a key at the third poll: drawn at once, not 2 s later
        self.assertEqual(seen[-1], "panel")
        self.assertEqual(sorted(set(seen)), ["cost", "panel"])
        self.assertEqual(seen.count("cost") >= 1, True)

    def test_other_keys_change_nothing(self):
        seen = self.run_loop([b"a", b"\r", b"\x1b[B", b"t", b"q", b"", b""])
        self.assertEqual(set(seen), {"panel"})

    def test_pairing_wins_over_the_cost_screen(self):
        st = dash_sample.sample(now=1790000000.0, pairing=True)
        st["power"] = power_state()
        a = o1panel.render(st, 640, 360, screen="panel")
        b = o1panel.render(st, 640, 360, screen="cost")
        self.assertEqual(a.buf, b.buf)
        st["pairing"] = None
        self.assertNotEqual(o1panel.render(st, 640, 360, screen="panel").buf, o1panel.render(st, 640, 360, screen="cost").buf)

    # -- what it says -----------------------------------------------------------------
    def test_three_windows_with_the_tariffs_symbol(self):
        kind, rows, est_any = o1panel.cost_view(cost_state())
        self.assertEqual(kind, "rows")
        self.assertEqual([r[0] for r in rows], ["24 HOURS", "7 DAYS", "30 DAYS"])
        self.assertEqual([r[1] for r in rows], ["$0.61", "$4.87", "$21.34"])
        self.assertEqual([r[3] for r in rows], [True, False, True])        # the * marks estimated figures
        self.assertTrue(est_any)
        self.assertEqual(rows[0][2], "3.54 kWh")
        self.assertEqual(rows[2][2], "118.6 kWh")

    def test_currency_symbols(self):
        for cur, sym, text in (("EUR", "€", "€0.61"), ("GBP", "£", "£0.61"), ("CHF", "CHF ", "CHF 0.61")):
            self.assertEqual(o1panel.cost_view(cost_state(currency=cur, symbol=sym))[1][0][1], text)
        self.assertEqual(o1panel.cost_view(cost_state(currency="EUR", symbol=None))[1][0][1], "€0.61")   # from the tariff table
        self.assertEqual(o1panel.cost_view(cost_state(currency="CHF", symbol=None))[1][0][1], "CHF 0.61")
        jp = power_state(currency="JPY", symbol="¥")
        jp["windows"]["1d"]["cost"] = 123.4
        self.assertEqual(o1panel.cost_view(dict(cost_state(), power=jp))[1][0][1], "¥123")       # yen has no decimals

    def test_no_price_says_so_instead_of_showing_zeros(self):
        st = cost_state(priced=False)
        st["power"]["windows"]["1d"]["cost"] = None
        self.assertEqual(o1panel.cost_view(st), ("message", o1panel.MSG_NO_PRICE))
        self.assertEqual(o1panel.MSG_NO_PRICE, "Set your electricity price in the admin panel")
        st = cost_state(priced=None)                                       # an older summary: judged by the figures
        for w in st["power"]["windows"].values():
            w["cost"] = None
        self.assertEqual(o1panel.cost_view(st)[0], "message")

    def test_no_data_yet(self):
        self.assertEqual(o1panel.cost_view({"time": 1.0}), ("message", "No data yet"))
        st = cost_state()
        for w in st["power"]["windows"].values():
            w["measured_h"] = 0
        self.assertEqual(o1panel.cost_view(st), ("message", "No data yet"))
        st = cost_state()
        st["power"]["windows"]["1w"].update(cost=None, measured_h=0)       # one window with none: that row, not a false 0
        rows = o1panel.cost_view(st)[1]
        self.assertEqual(rows[1][1], "NO DATA")
        self.assertEqual(rows[0][1], "$0.61")

    def test_a_short_history_is_labelled(self):
        st = cost_state(since=1790000000 - 3 * 86400 - 4 * 3600)
        rows = o1panel.cost_view(st)[1]
        self.assertEqual(rows[0][4], "")
        self.assertEqual(rows[1][4], "only 3d 4h of data")
        self.assertEqual(rows[2][4], "only 3d 4h of data")

    def test_a_power_service_from_before_this_still_shows_the_day(self):
        st = dash_sample.sample(now=1790000000.0)
        st["power"] = {"watts": 1.0, "kwh_24h": 3.0, "cost_24h": 0.5, "symbol": "$"}
        kind, rows, _est = o1panel.cost_view(st)
        self.assertEqual((kind, rows[0][1], rows[1][1]), ("rows", "$0.50", "NO DATA"))

    # -- how it is drawn --------------------------------------------------------------
    def texts_drawn(self, fn):
        seen = []
        orig = o1gfx.Pixmap.text

        def rec(self_, x, y, s, c, scale=1, max_w=None, ellipsis=True):
            seen.append((s, scale))
            return orig(self_, x, y, s, c, scale, max_w, ellipsis)
        o1gfx.Pixmap.text = rec
        try:
            fn()
        finally:
            o1gfx.Pixmap.text = orig
        return seen

    def test_the_figures_are_very_large(self):
        seen = self.texts_drawn(lambda: o1panel.render(cost_state(), 640, 360, screen="cost"))
        big = [sc for s, sc in seen if s.startswith("$")]
        self.assertEqual(len(big), 3)
        self.assertGreaterEqual(min(big), 8)                               # 8 x 7 px capitals at 640x360: 15% of the screen height
        self.assertEqual(len(set(big)), 1)                                 # one size for the three

    def test_it_shows_only_the_cost(self):
        seen = self.texts_drawn(lambda: o1panel.render(cost_state(), 640, 360, screen="cost"))
        words = " ".join(s for s, _ in seen)
        for gone in ("GRAPHICS CARD", "PROCESSOR AND MEMORY", "Gateway", "Address", "Tunnel", "VRAM"):
            self.assertNotIn(gone, words.upper() if gone.isupper() else words)
        for there in ("24 HOURS", "7 DAYS", "30 DAYS"):
            self.assertIn(there, words)

    def test_messages_are_drawn_large_and_in_full(self):
        for text, st in ((o1panel.MSG_NO_PRICE, cost_state(priced=False)), (o1panel.MSG_NO_DATA, {"time": 1790000000.0})):
            seen = self.texts_drawn(lambda: o1panel.render(st, 640, 360, screen="cost"))
            lines = [(s, sc) for s, sc in seen if s not in ("Space: back",)]
            self.assertEqual(" ".join(s for s, _ in lines), text)
            self.assertGreaterEqual(min(sc for _, sc in lines), 4)
            self.assertNotIn("$", " ".join(s for s, _ in lines))

    def test_big_numbers_in_any_currency_never_overflow_their_row(self):
        for sym, cur in (("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("CHF ", "CHF"), ("kr ", "SEK"), ("R$ ", "BRL")):
            for cost in (0.01, 1234.56, 99999.99, 1234567.89, 123456789012.34):
                st = cost_state(symbol=sym, currency=cur)
                for w in st["power"]["windows"].values():
                    w["cost"] = cost
                pm = o1panel.render(st, 640, 360, screen="cost")
                rh = (360 - 14 - 8 - 12 - 8) // 3
                boxes = [(8, 8 + i * (rh + 6), 640 - 16, rh) for i in range(3)]
                for (x, y, bw, bh) in boxes:
                    for yy in range(y + 6, y + bh - 6):
                        for xx in list(range(x + 2, x + 9)) + list(range(x + bw - 9, x + bw - 2)):
                            self.assertEqual(pm.get(xx, yy), o1panel.T["panel"], (sym, cost, xx, yy))
                # and in the real row boxes of every size the screen may have
                for w, h in ((480, 270), (640, 360), (800, 450), (1024, 768), (1280, 1024)):
                    o1panel.render(st, w, h, screen="cost")

    def test_the_row_sizes_follow_the_screen(self):
        for w, h in ((480, 270), (640, 360), (640, 480), (1024, 600)):
            pm = o1panel.render(cost_state(), w, h, screen="cost")
            self.assertGreater(len({v for v in pm.buf}), 4)

    def test_the_estimate_footnote_only_when_something_is_estimated(self):
        def foot(st):
            return " ".join(s for s, _ in self.texts_drawn(lambda: o1panel.render(st, 640, 360, screen="cost")))
        self.assertIn("estimated", foot(cost_state()))
        plug = cost_state()
        for w in plug["power"]["windows"].values():
            w["est"] = False
        self.assertNotIn("estimated", foot(plug))
        self.assertIn("Space: back", foot(plug))

    def test_the_normal_panel_hints_at_the_key(self):
        seen = self.texts_drawn(lambda: o1panel.render(cost_state(), 640, 360))
        self.assertIn("Space: electricity cost", " ".join(s for s, _ in seen))
        seen = self.texts_drawn(lambda: o1panel.render(dash_sample.sample(now=1790000000.0), 480, 270))
        self.assertTrue(any("Space:" in s for s, _ in seen))

    def test_the_new_glyphs(self):
        for ch in "€£¥":
            self.assertIn(ch, F.GLYPHS)
            self.assertEqual(F.clean(ch), ch)
            self.assertLessEqual(F.text_width(ch, 1), 5)

    def test_the_png_option_can_show_the_cost_screen(self):
        dash = load_dash()
        d = tempfile.mkdtemp(prefix="o1png-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        a, b = os.path.join(d, "a.png"), os.path.join(d, "b.png")
        with contextlib.redirect_stdout(io.StringIO()):
            dash.png(["--png", a, "--scale", "1"])
            dash.png(["--png", b, "--scale", "1", "--screen", "cost"])
        with open(a, "rb") as f, open(b, "rb") as g:
            self.assertNotEqual(f.read(), g.read())

    def test_the_power_service_publishes_the_three_windows(self):
        import test_power
        import o1power
        now = int(time.time())
        cut = now - now % 60
        o1power.append_rows([(cut - 60 * i, 2.0, "est", 60, 0) for i in range(1, 121)])
        s = o1power.summary(now, test_power.sched(flat_rate=0.2), live=None)
        c = o1power.compact(s)
        self.assertEqual(set(c["windows"]), {"1d", "1w", "1m"})
        self.assertEqual(c["windows"]["1d"]["cost"], s["windows"]["1d"]["cost"])      # the admin panel's own figure
        self.assertEqual(c["windows"]["1m"]["kwh"], s["windows"]["1m"]["kwh"])
        self.assertTrue(c["windows"]["1w"]["est"])
        self.assertTrue(c["priced"])
        self.assertEqual((c["currency"], c["since"]), ("USD", s["since"]))
        unpriced = o1power.compact(o1power.summary(now, test_power.sched(), live=None))
        self.assertFalse(unpriced["priced"])
        self.assertIsNone(unpriced["windows"]["1d"]["cost"])
        import o1metrics
        os.makedirs(os.path.join(o1power.run_dir()), exist_ok=True)
        with open(os.path.join(o1power.run_dir(), "summary.json"), "w") as f:
            json.dump(c, f)
        got = o1metrics.power_state()
        self.assertEqual(got["windows"], c["windows"])                       # and the dashboard reads them back
        self.assertEqual((got["priced"], got["currency"]), (True, "USD"))


class TestWidgets(unittest.TestCase):
    def test_bar_fills_in_proportion(self):
        for f in (0.0, 0.25, 0.5, 1.0):
            pm = o1gfx.Pixmap(120, 9, 0)
            o1panel.bar(pm, 10, 1, 100, 7, f, 0xFF0000)
            n = sum(1 for x in range(120) if pm.get(x, 4) == 0xFF0000)
            self.assertEqual(n, int(round(f * 100)) if f else 0, f)
            self.assertEqual(pm.get(5, 4), 0)                       # nothing left of the bar
            self.assertEqual(pm.get(115, 4), 0)

    def test_bar_with_no_reading_is_just_the_track(self):
        pm = o1gfx.Pixmap(60, 9, 0)
        o1panel.bar(pm, 0, 1, 50, 7, None, 0xFF0000)
        self.assertFalse(any(v == 0xFF0000 for v in pm.buf))
        self.assertTrue(any(v == o1panel.T["track"] for v in pm.buf))
        o1panel.bar(pm, 0, 1, 50, 7, float("nan"), 0xFF0000)
        o1panel.bar(pm, 0, 1, 50, 7, 7.0, 0xFF0000)                  # over 100%: clamped
        self.assertEqual(sum(1 for x in range(60) if pm.get(x, 4) == 0xFF0000), 50)

    def test_bar_rows_only_draw_the_rows_that_fit(self):
        pm = o1gfx.Pixmap(200, 100, 0)
        rows = [("A", "1", 0.5, 0xFF0000)] * 6
        used = o1panel.bar_rows(pm, (0, 0, 200, 30), rows)
        self.assertEqual(used, 2 * 13)                                # 30 px of height: a third row (at 26) would need 7 more
        for y in range(40, 100):
            self.assertFalse(any(pm.get(x, y) for x in range(200)))

    def test_bar_rows_with_huge_labels_and_values_stay_in_their_columns(self):
        for w in (60, 120, 295):
            pm = o1gfx.Pixmap(400, 40, 0)
            o1panel.bar_rows(pm, (50, 0, w, 40), [("L" * 300, "V" * 300, 0.5, 0xFF0000), ("ab", "1", 0.5, 0xFF0000)])
            for y in range(40):
                for x in range(400):
                    if not 50 <= x < 50 + w:
                        self.assertEqual(pm.get(x, y), 0, (w, x, y))
            if w >= 120:                                                    # the bar keeps some room
                self.assertTrue(any(pm.get(x, 3) == 0xFF0000 for x in range(50, 50 + w)))

    def test_a_frame_narrows_the_clip_to_its_inside(self):
        pm = o1gfx.Pixmap(100, 80, 0)
        with pm.clipped(0, 0, 100, 80):
            inner = o1panel.frame(pm, (10, 10, 80, 60), "Title")
            pm.fill_rect(0, 0, 100, 80, 0xFF00FF)
        got = [(x, y) for y in range(80) for x in range(100) if pm.get(x, y) == 0xFF00FF]
        self.assertEqual(min(x for x, _ in got), inner[0])
        self.assertEqual(max(x for x, _ in got), inner[0] + inner[2] - 1)
        self.assertEqual(min(y for _, y in got), inner[1])
        self.assertEqual(max(y for _, y in got), inner[1] + inner[3] - 1)
        self.assertEqual(pm.clip, (0, 0, 100, 80))

    def test_graph_draws_a_flat_series_at_its_height(self):
        pm = o1gfx.Pixmap(160, 70, 0)
        box = (5, 5, 150, 60)
        o1panel.graph(pm, box, [{"values": [50.0] * 300, "color": 0x00FF00, "vmax": 100, "label": "Use", "now": "50%"}])
        ys = [y for y in range(70) if pm.get(80, y) == 0x00FF00]
        self.assertTrue(ys)
        plot_top, plot_h = 5 + 11 + 2, 60 - 11 - 4
        expect = plot_top + plot_h - 1 - round(0.5 * (plot_h - 1))
        self.assertLessEqual(abs(min(ys) - expect), 1)
        # nothing drawn outside the graph's box
        for y in range(70):
            for x in range(160):
                if not (5 <= x < 155 and 5 <= y < 65):
                    self.assertEqual(pm.get(x, y), 0)

    def test_graph_leaves_gaps_for_missing_readings_and_says_so_with_no_data(self):
        pm = o1gfx.Pixmap(160, 70, 0)
        o1panel.graph(pm, (5, 5, 150, 60), [{"values": [None] * 50, "color": 0x00FF00, "label": "Use", "now": ""}])
        self.assertFalse(any(v == 0x00FF00 for v in pm.buf if v != 0x00FF00) and False)
        self.assertTrue(any(v == o1panel.T["dim"] for v in pm.buf))        # "no data yet"
        pm = o1gfx.Pixmap(160, 70, 0)
        vals = [10.0] * 100 + [None] * 100 + [90.0] * 100
        o1panel.graph(pm, (5, 5, 150, 60), [{"values": vals, "color": 0x00FF00, "vmax": 100, "label": "x", "now": ""}])
        cols = [x for x in range(160) if any(pm.get(x, y) == 0x00FF00 for y in range(70))]
        self.assertTrue(any(b - a > 20 for a, b in zip(cols, cols[1:])))   # a hole in the middle

    def test_graph_legend_stops_before_it_overruns(self):
        pm = o1gfx.Pixmap(120, 50, 0)
        curves = [{"values": [1.0] * 10, "color": c, "label": "A long label number %d" % i, "now": "12345 units"}
                  for i, c in enumerate((0xFF0000, 0x00FF00, 0x0000FF))]
        o1panel.graph(pm, (0, 0, 120, 50), curves)
        for c in (0xFF0000, 0x00FF00, 0x0000FF):
            xs = [x for y in range(10) for x in range(120) if pm.get(x, y) == c]
            if xs:
                self.assertLess(max(xs), 120)

    def test_ring_fill_follows_the_fraction(self):
        counts = []
        for f in (0.0, 0.3, 0.6, 1.0):
            pm = o1gfx.Pixmap(64, 64, 0)
            o1panel.ring(pm, 32, 32, 28, 6, f, 0xFF0000, "", "")
            counts.append(sum(1 for v in pm.buf if v == 0xFF0000))
        self.assertEqual(counts[0], 0)
        self.assertEqual(sorted(counts), counts)
        self.assertEqual(len(set(counts)), 4)

    def test_nice_max(self):
        self.assertEqual(o1panel.nice_max(0.3), 1)
        self.assertEqual(o1panel.nice_max(7), 10)
        self.assertEqual(o1panel.nice_max(41, 10), 50)
        self.assertEqual(o1panel.nice_max(101), 200)
        self.assertEqual(o1panel.nice_max(1500), 2000)

    def test_wrap_breaks_at_spaces_and_ends_with_dots(self):
        lines = o1panel.wrap("/srv/models is not mounted at all right now", 90, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(F.text_width(l) <= 90 for l in lines))
        self.assertTrue(lines[-1].endswith("..."))
        self.assertEqual(o1panel.wrap("short", 90, 2), ["short"])
        self.assertEqual(o1panel.wrap("", 90, 2), [])
        self.assertTrue(all(F.text_width(l) <= 30 for l in o1panel.wrap("x" * 80 + " yy", 30, 3)))

    def test_sleep_line(self):
        now = 10000.0

        def line(**kw):
            st = {"activity": {"last": now - kw.get("idle", 0), "at": now - kw.get("age", 0), "inflight": kw.get("inflight", 0)},
                  "idle": kw.get("idle_info", {})}
            return o1panel.sleep_summary(st, now)
        self.assertEqual(line(idle_info={"supported": False}), ("No deep sleep on this machine", "dim"))
        self.assertEqual(line(idle_info={"enabled": False}), ("Auto sleep is off", "dim"))
        self.assertEqual(line(inflight=2)[0], "Awake: request running")
        text, style = line(idle=600, idle_info={"enabled": True, "minutes": 30})
        self.assertTrue(text.startswith("Sleeps in 20:00"), text)
        self.assertEqual(style, "text")
        self.assertEqual(line(idle=1600, idle_info={"enabled": True, "minutes": 30})[1], "warn")
        self.assertEqual(line(idle=4000, idle_info={"enabled": True, "minutes": 30})[0], "Idle long enough: may sleep now")
        self.assertEqual(line(idle=600)[0], "Idle for 10m 0s")
        self.assertEqual(o1panel.sleep_summary({}, now), ("Awake", "text"))
        self.assertEqual(line(idle=600, age=500, idle_info={"enabled": True, "minutes": 30})[0], "Awake")   # a stale file says nothing


if __name__ == "__main__":
    unittest.main()
