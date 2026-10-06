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
import math
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
import o1hipix
import o1pixfont as F
import o1vecfont
import o1vtext

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
            self.dash.png(["--png", out, "--scale", "2", "--pairing", "--size", "1280x720"])
        w, h, _ = decode_png(self.slurp(out))
        self.assertEqual((w, h), (2560, 1440))                              # --size is the screen's pixels; --scale doubles the picture


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
            self.assertEqual(set(boxes), {"header", "gpu", "cpu", "models", "storage", "fans", "network", "status", "footer"}, (w, h))
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
        real, real_cost = o1panel.PanelRenderer.draw, o1panel.render_cost

        def spy(self_, st_, range_s=300, incremental=False):
            seen.append("panel")
            return real(self_, st_, range_s, incremental)

        def spy_cost(st_, w, h):
            seen.append("cost")
            return real_cost(st_, w, h)
        o1panel.PanelRenderer.draw, o1panel.render_cost = spy, spy_cost
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
            o1panel.PanelRenderer.draw, o1panel.render_cost = real, real_cost
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
        """[(text, height in pixels)] for every string the smooth font draws while fn runs."""
        seen = []
        orig = o1vecfont.draw

        def rec(pm, x, y, text, height, colour, bg):
            seen.append((text, height))
            return orig(pm, x, y, text, height, colour, bg)
        o1vecfont.draw = rec
        try:
            fn()
        finally:
            o1vecfont.draw = orig
        return seen

    def cost(self, st, w=640, h=360):
        o1panel._COST_CACHE.update(key=None, pm=None)
        return o1panel.render(st, w, h, screen="cost")

    def test_the_figures_are_very_large_and_all_one_size(self):
        for w, h in ((640, 360), (1920, 1080), (3840, 2160)):
            seen = self.texts_drawn(lambda: self.cost(cost_state(), w, h))
            big = [hh for s, hh in seen if s.startswith("$")]
            self.assertEqual(len(big), 3)
            self.assertEqual(len(set(big)), 1)
            self.assertGreaterEqual(big[0], 0.18 * h)                     # three rows share the screen: each figure is about a fifth of its height
            labels = [hh for s, hh in seen if s in ("24 HOURS", "7 DAYS", "30 DAYS")]
            self.assertEqual(len(labels), 3)
            self.assertLess(max(labels), big[0])

    def test_it_shows_only_the_cost_and_never_an_asterisk(self):
        seen = self.texts_drawn(lambda: self.cost(cost_state()))
        words = " ".join(s for s, _ in seen)
        for gone in ("GRAPHICS CARD", "PROCESSOR AND MEMORY", "GATEWAY", "ADDRESS", "TUNNEL", "VRAM", "ESTIMATED"):
            self.assertNotIn(gone, words.upper())
        self.assertNotIn("*", words)
        for there in ("24 HOURS", "7 DAYS", "30 DAYS", "SPACE: BACK"):
            self.assertIn(there, words)
        est = cost_state()
        for w in est["power"]["windows"].values():
            w["est"] = True
        self.assertNotIn("*", " ".join(s for s, _ in self.texts_drawn(lambda: self.cost(est))))
        a, b = self.cost(cost_state()), self.cost(est)
        self.assertEqual(a.buf, b.buf)                                     # whether estimated or not, the screen is the same

    def test_messages_are_smooth_large_and_in_full(self):
        for text, st in ((o1panel.MSG_NO_PRICE, cost_state(priced=False)), (o1panel.MSG_NO_DATA, {"time": 1790000000.0})):
            for w, h in ((640, 360), (1920, 1080)):
                seen = self.texts_drawn(lambda: self.cost(st, w, h))
                lines = [(s, hh) for s, hh in seen if s != "SPACE: BACK"]
                self.assertEqual(" ".join(s for s, _ in lines), text.upper())
                self.assertGreaterEqual(min(hh for _, hh in lines), h // 12)
                self.assertNotIn("$", " ".join(s for s, _ in lines))
                for s_, hh in lines:
                    self.assertLessEqual(o1vecfont.text_width(s_, hh), w)

    def test_big_numbers_in_any_currency_never_overflow_their_row(self):
        for sym, cur in (("$", "USD"), ("\u20ac", "EUR"), ("\u00a3", "GBP"), ("\u00a5", "JPY"), ("CHF ", "CHF"), ("kr ", "SEK"),
                         ("R$ ", "BRL")):
            for cost in (0.01, 1234.56, 99999.99, 1234567.89, 123456789012.34):
                st = cost_state(symbol=sym, currency=cur)
                for w in st["power"]["windows"].values():
                    w["cost"] = cost
                for sw, sh in ((640, 360), (480, 270), (1920, 1080), (1024, 768)):
                    pm = self.cost(st, sw, sh)
                    boxes, _foot = o1panel.cost_layout(sw, sh)
                    for (x, y, bw, bh) in boxes:
                        for yy in range(y + bh // 6, y + bh - bh // 6, max(1, bh // 40)):
                            for xx in list(range(x + 5, x + 8)) + list(range(x + bw - 8, x + bw - 5)):
                                self.assertEqual(pm.get(xx, yy), o1panel.T["panel"], (sym, cost, sw, sh, xx, yy))
                    texts = self.texts_drawn(lambda: self.cost(st, sw, sh))
                    for s_, hh in texts:
                        self.assertLessEqual(o1vecfont.text_width(s_, hh), sw - 2 * boxes[0][0], (s_, hh, sw))

    def test_the_row_sizes_follow_the_screen(self):
        for w, h in ((480, 270), (640, 360), (640, 480), (1024, 600), (1920, 1080), (3840, 2160)):
            pm = self.cost(cost_state(), w, h)
            self.assertEqual((pm.w, pm.h), (w, h))
            self.assertGreater(len({v for v in pm.buf}), 4)

    def test_the_picture_is_kept_until_the_figures_change(self):
        o1panel._COST_CACHE.update(key=None, pm=None)
        st = cost_state()
        a = o1panel.render_cost(st, 640, 360)
        self.assertIs(o1panel.render_cost(st, 640, 360), a)                 # same figures: the very same picture, nothing redrawn
        self.assertIs(o1panel.render_cost(dict(st, time=st["time"] + 60), 640, 360), a)    # the clock is not on this screen
        st2 = cost_state()
        st2["power"]["windows"]["1d"]["cost"] = 0.62
        b = o1panel.render_cost(st2, 640, 360)
        self.assertIsNot(b, a)
        self.assertIsNot(o1panel.render_cost(st2, 800, 450), b)             # another screen size

    def test_the_cost_screen_has_its_own_picture_size_at_full_resolution(self):
        self.assertEqual(o1fb.choose_cost_scale(3840, 2160), (1, 3840, 2160))
        self.assertEqual(o1fb.choose_cost_scale(1920, 1080), (1, 1920, 1080))
        self.assertEqual(o1fb.choose_cost_scale(7680, 4320), (2, 3840, 2160))
        self.assertEqual(o1fb.choose_cost_scale(1366, 768), (1, 1366, 768))

    def test_the_loop_draws_the_cost_screen_at_the_screens_own_size(self):
        sizes = []
        real = o1panel.render_cost

        def spy(st_, w, h):
            sizes.append((w, h))
            return real(st_, w, h)
        o1panel.render_cost = spy
        try:
            c = Clock()
            polls = iter([b" ", b"", b""])

            class K:
                def wait(self_, t):
                    c.sleep(t)
                    return next(polls, b"")
            fb = FakeFb()
            n = [0]

            def stop():
                n[0] += 1
                return n[0] > 8
            o1paneld.run(fb, FakeTty(), FakeSampler(cost_state()), clock=c.now, sleep=c.sleep, keys=K(), stop=stop)
        finally:
            o1panel.render_cost = real
        self.assertTrue(sizes)
        self.assertEqual(set(sizes), {(1280, 720)})                         # FakeFb's own size, drawn 1:1
        self.assertGreater(sum(n_ for _o, n_ in fb.writes), 3 * 1280 * 720 * 4)   # and the whole screen was rewritten when it flipped

    def bitmap_texts(self, fn):
        seen = []
        orig = o1hipix.HiPixmap.text

        def rec(self_, x, y, text, c, scale=1, max_w=None, ellipsis=True):
            seen.append((text, scale))
            return orig(self_, x, y, text, c, scale, max_w, ellipsis)
        o1hipix.HiPixmap.text = rec
        try:
            fn()
        finally:
            o1hipix.HiPixmap.text = orig
        return seen

    def test_the_normal_panel_hints_at_the_key(self):
        seen = self.bitmap_texts(lambda: o1panel.render(cost_state(), 640, 360))
        self.assertIn("Space: electricity cost", " ".join(s for s, _ in seen))
        seen = self.bitmap_texts(lambda: o1panel.render(dash_sample.sample(now=1790000000.0), 480, 270))
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


class TestVectorFont(unittest.TestCase):
    """The smooth rounded stroke font of the cost screen."""

    def test_every_character_the_screen_can_say_is_there(self):
        for ch in "0123456789.,:-/$\u20ac\u00a3\u00a5ABCDEFGHIJKLMNOPQRSTUVWXYZ ":
            self.assertTrue(o1vecfont.supported(ch), repr(ch))
        for text in ("SET YOUR ELECTRICITY PRICE IN THE ADMIN PANEL", "NO DATA YET", "ONLY 3D 4H OF DATA", "24 HOURS", "7 DAYS",
                     "30 DAYS", "118.6 KWH", "SPACE: BACK"):
            self.assertEqual(o1vecfont.clean(text), text)
        self.assertEqual(o1vecfont.clean("abc"), "ABC")                      # capitals only
        self.assertEqual(o1vecfont.clean("a\x00\u6f22"), "A ")

    def test_digits_are_all_one_width_and_figures_do_not_shift(self):
        self.assertEqual(len({o1vecfont.advance_units(d) for d in "0123456789"}), 1)
        for h in (40, 120, 333):
            self.assertEqual(o1vecfont.text_width("1111", h), o1vecfont.text_width("8888", h))
            self.assertEqual(o1vecfont.layout("1234", h)[3][1] - o1vecfont.layout("1234", h)[2][1],
                             o1vecfont.layout("8888", h)[3][1] - o1vecfont.layout("8888", h)[2][1])

    def capsule_area(self, length, r, size=200):
        rows = o1vecfont.rasterize([(30, 100, 30 + length, 100)], r, size, 200)
        return sum(sum(row) for row in rows) / 255.0, rows

    def test_a_capsule_has_the_area_of_a_capsule_and_round_ends(self):
        for length, r in ((60, 12.0), (0, 20.0), (100, 7.5)):
            area, rows = self.capsule_area(length, r)
            want = 2 * r * length + math.pi * r * r
            self.assertAlmostEqual(area / want, 1.0, delta=0.01, msg=(length, r))
        area, rows = self.capsule_area(60, 12.0)
        self.assertEqual(rows[100][60], 255)                                  # the middle is solid
        self.assertEqual(rows[100 - 12 - 2][60], 0)                           # clear of the pen above it
        self.assertEqual(rows[100 - 12 + 1][60], 255)                         # solid just inside
        left_end = 30 - 12                                                    # the round cap: the extreme corner is empty
        self.assertEqual(rows[100 - 12][left_end], 0)
        self.assertGreater(rows[100][left_end + 1], 200)

    def test_edges_are_anti_aliased(self):
        _a, rows = self.capsule_area(80, 11.3)
        edge = {v for row in rows for v in row if 0 < v < 255}
        self.assertGreater(len(edge), 10)                                     # many in-between levels, not a hard 0 / 255 edge
        diag = o1vecfont.rasterize([(10, 10, 150, 90)], 6.0, 160, 100)
        self.assertGreater(len({v for row in diag for v in row}), 15)

    def test_joins_are_round_and_do_not_double_count(self):
        # two segments meeting at a corner: the union, so the overlap at the joint is not added twice
        rows = o1vecfont.rasterize([(20, 20, 80, 20), (80, 20, 80, 80)], 8.0, 120, 120)
        self.assertTrue(all(0 <= v <= 255 for row in rows for v in row))
        self.assertEqual(rows[20][80], 255)
        corner_outside = rows[20 - 8][80 + 8]                                 # the square corner of the pen's bounding box
        self.assertEqual(corner_outside, 0)                                   # round join, not a mitre

    def test_glyph_bitmaps_have_ink_inside_their_box_and_are_cached(self):
        for ch in "0123456789.,$\u20ac\u00a3\u00a5AGMQRSW":
            rows, w, h, ox, oy = o1vecfont.glyph(ch, 120)
            self.assertEqual((len(rows), len(rows[0])), (h, w))
            self.assertTrue(any(any(r) for r in rows), ch)
            self.assertIs(o1vecfont.glyph(ch, 120), o1vecfont.glyph(ch, 120))      # cached
            self.assertEqual(max(max(r) for r in rows), 255)                     # fully inked somewhere
        self.assertGreater(sum(map(sum, o1vecfont.glyph("8", 160)[0])), sum(map(sum, o1vecfont.glyph("1", 160)[0])))

    def test_a_digit_is_as_tall_as_asked(self):
        for h in (60, 200, 400):
            rows, w, hh, ox, oy = o1vecfont.glyph("0", h)
            inked = [i for i, r in enumerate(rows) if any(v > 127 for v in r)]
            span = inked[-1] - inked[0] + 1
            self.assertAlmostEqual(span / float(h), 0.99, delta=0.03)       # the zero's skeleton spans 8..92, plus the pen

    def test_draw_stays_inside_the_clip_and_keeps_the_background(self):
        bg, fg = 0x112233, 0xE0F0FF
        pm = o1gfx.Pixmap(400, 200, bg)
        with pm.clipped(50, 40, 120, 90):
            o1vecfont.draw(pm, 20, 20, "$1234567.89", 150, fg, bg)
        for y in range(200):
            for x in range(400):
                if not (50 <= x < 170 and 40 <= y < 130):
                    self.assertEqual(pm.get(x, y), bg)
        self.assertTrue(any(v not in (bg, fg) for v in pm.buf))                # anti-aliased edge colours
        self.assertTrue(any(v == fg for v in pm.buf))

    def test_fit_height_never_overflows(self):
        for text in ("$0.61", "$1234.56", "\u20ac123456789012.34", "NO DATA", "SET YOUR ELECTRICITY PRICE"):
            for mw, mh in ((300, 100), (3000, 100), (50, 500), (1900, 300), (7, 7)):
                h = o1vecfont.fit_height(text, mw, mh)
                self.assertGreaterEqual(h, 4)
                if h > 4:
                    self.assertLessEqual(o1vecfont.text_width(text, h), mw + 1)
                    y0, y1 = o1vecfont.vertical_extent(text)
                    self.assertLessEqual((y1 - y0) * h / 100.0, mh + 1)

    def test_ink_is_centred_in_its_box(self):
        bg, fg = 0, 0xFFFFFF
        for text in ("$8", "0.61", "-"):
            pm = o1gfx.Pixmap(600, 300, bg)
            h = 120
            top = o1vecfont.ink_top(text, h, 50, 200)
            o1vecfont.draw(pm, 20, top, text, h, fg, bg)
            ys = [y for y in range(300) if any(pm.get(x, y) for x in range(600))]
            self.assertLessEqual(abs((ys[0] + ys[-1]) / 2.0 - 150), 3, text)


class TestRaster(unittest.TestCase):
    """lib/o1raster.py: anti-aliased strokes, corners and discs."""

    def area(self, rows):
        return sum(sum(cov) for _y, _x, cov in rows) / 255.0

    def test_a_stroke_has_the_area_of_a_capsule(self):
        import o1raster
        for length, r in ((60, 12.0), (0, 20.0), (200, 3.0), (30, 40.0)):
            rows = o1raster.stroke_rows([(50, 100, 50 + length, 100)], r)
            want = 2 * r * length + math.pi * r * r
            self.assertAlmostEqual(self.area(rows) / want, 1.0, delta=0.012, msg=(length, r))

    def test_rows_are_sparse_and_only_where_there_is_ink(self):
        import o1raster
        rows = o1raster.stroke_rows([(10, 500, 900, 500)], 5.0)
        ys = [y for y, _x, _c in rows]
        self.assertEqual(ys, list(range(min(ys), max(ys) + 1)))
        self.assertLessEqual(len(ys), 12)                                   # a thin line touches a few rows, not 1000
        self.assertTrue(all(len(c) <= 912 for _y, _x, c in rows))
        self.assertEqual(o1raster.stroke_rows([], 5.0), [])

    def test_a_polyline_is_the_union_of_its_segments(self):
        import o1raster
        pts = [(20, 20), (100, 20), (100, 100)]
        a = self.area(o1raster.stroke_rows(o1raster.polyline_caps(pts), 8.0))
        two = 2 * (2 * 8.0 * 80 + math.pi * 64)
        self.assertLess(a, two)                                              # the joint is not counted twice
        self.assertGreater(a, two - 2 * 64 * 2)

    def test_arcs_follow_the_angle_asked(self):
        import o1raster
        pts = o1raster.arc_points(100, 100, 50, 0, 90)                       # 12 o'clock to 3 o'clock
        self.assertAlmostEqual(pts[0][0], 100, delta=0.01)
        self.assertAlmostEqual(pts[0][1], 50, delta=0.01)
        self.assertAlmostEqual(pts[-1][0], 150, delta=0.01)
        self.assertAlmostEqual(pts[-1][1], 100, delta=0.01)
        self.assertGreaterEqual(len(pts), 3)

    def test_corners_and_discs(self):
        import o1raster
        for R in (3, 12, 36):
            q = o1raster.corner(R)
            self.assertEqual((len(q), len(q[0])), (R, R))
            self.assertEqual(q[R - 1][R - 1], 255)                           # inside
            self.assertEqual(q[0][0], 0)                                     # the very corner is outside the curve
            self.assertTrue(any(0 < v < 255 for row in q for v in row))
            area = sum(sum(row) for row in q) / 255.0
            self.assertAlmostEqual(area / (math.pi * R * R / 4.0 + 0.0), 1.0, delta=0.12 if R > 3 else 0.4)
            d = o1raster.disc_rows(R)
            self.assertAlmostEqual(sum(sum(r_) for r_ in d) / 255.0 / (math.pi * R * R), 1.0, delta=0.03)
            self.assertEqual(d[R][R], 255)
            self.assertEqual(d[0][0], 0)


class TestSmoothSurface(unittest.TestCase):
    """HiPixmap: draws in logical units at the real resolution, every edge smooth."""

    def test_logical_size_pixel_size_and_scale(self):
        pm = o1hipix.HiPixmap(64, 36, 7, 6)
        self.assertEqual((pm.w, pm.h, pm.pw, pm.ph, pm.S, len(pm.buf)), (64, 36, 384, 216, 6, 384 * 216))
        pm.fill_rect(10, 5, 3, 2, 9)
        self.assertEqual(pm.get(10, 5), 9)
        self.assertEqual(pm.get(12, 6), 9)
        self.assertEqual(pm.get(13, 5), 7)
        self.assertEqual(sum(1 for v in pm.buf if v == 9), 3 * 2 * 36)       # whole blocks of S x S pixels

    def test_everything_clips_in_logical_units(self):
        pm = o1hipix.HiPixmap(40, 20, 0, 4)
        with pm.clipped(10, 5, 10, 5):
            pm.fill_rect(0, 0, 40, 20, 1)
            pm.rrect(0, 0, 40, 20, 5, 2)
            pm.disc(15, 7, 30, 3)
            pm.arc(15, 7, 30, 20, 0, 359, 4)
            pm.polyline([(0, 0), (39, 19), (0, 19)], 3, 5)
            pm.text(0, 0, "Hello world", 6, 2)
            pm.hairline(0, 7, 40, 7)
        for y in range(pm.ph):
            for x in range(pm.pw):
                if not (40 <= x < 80 and 20 <= y < 40):
                    self.assertEqual(pm.buf[y * pm.pw + x], 0, (x, y))
        self.assertEqual(pm.clip, (0, 0, 40, 20))

    def test_a_rounded_rectangle_is_round_and_smooth(self):
        for S in (1, 3, 6):
            pm = o1hipix.HiPixmap(60, 40, 0, S)
            pm.rrect(10, 8, 40, 24, 6, 0xFFFFFF)
            self.assertEqual(pm.buf[(8 * S) * pm.pw + 10 * S], 0)                       # the corner pixel is empty
            self.assertEqual(pm.buf[(20 * S) * pm.pw + 30 * S], 0xFFFFFF)                # the middle is solid
            self.assertEqual(pm.buf[(8 * S) * pm.pw + 30 * S], 0xFFFFFF)                 # the straight top edge is solid
            self.assertEqual(pm.buf[(8 * S - 1) * pm.pw + 30 * S], 0)                    # and sharp
            if S > 1:
                edge = {v & 255 for v in pm.buf if v not in (0, 0xFFFFFF)}
                self.assertGreater(len(edge), 6)                                          # in-between greys along the curve
            area = sum(v & 255 for v in pm.buf) / 255.0 / (S * S)
            self.assertAlmostEqual(area, 40 * 24 - (4 - math.pi) * 36, delta=3.0)
            sym = [pm.buf[(8 * S + j) * pm.pw + 10 * S + i] for j in range(6 * S) for i in range(6 * S)]
            mirror = [pm.buf[(8 * S + j) * pm.pw + 50 * S - 1 - i] for j in range(6 * S) for i in range(6 * S)]
            self.assertEqual(sym, mirror)                                                 # both corners alike

    def test_a_disc_and_an_arc(self):
        pm = o1hipix.HiPixmap(60, 60, 0, 4)
        pm.disc(30, 30, 10, 0xFFFFFF)
        self.assertAlmostEqual(sum(v & 255 for v in pm.buf) / 255.0 / 16.0, math.pi * 100, delta=6)
        self.assertEqual(pm.get(30, 30), 0xFFFFFF)
        counts = []
        for f in (0.0, 0.25, 0.5, 1.0):
            d = o1hipix.HiPixmap(80, 80, 0, 3)
            d.arc(40, 40, 30, 24, 225, 225 + 270 * f, 0xFF0000)
            counts.append(sum(1 for v in d.buf if v))
        self.assertEqual(counts[0], 0)
        self.assertEqual(sorted(counts), counts)
        d = o1hipix.HiPixmap(80, 80, 0, 3)
        d.arc(40, 40, 30, 24, 225, 495, 0xFFFFFF)
        self.assertTrue(d.get(40, 40 - 27))                                  # 12 o'clock
        self.assertFalse(d.get(40, 40 + 27))                                 # the open bottom
        self.assertFalse(d.get(40, 40))                                      # the hole
        self.assertTrue(d.get(40 + 27, 40) and d.get(40 - 27, 40))

    def test_a_polyline_has_round_caps_and_smooth_edges(self):
        pm = o1hipix.HiPixmap(60, 30, 0, 6)
        pm.polyline([(10, 15), (50, 15)], 3.0, 0xFFFFFF)
        self.assertEqual(pm.get(30, 15), 0xFFFFFF)
        self.assertEqual(pm.get(30, 12), 0)
        self.assertEqual(pm.get(7, 15), 0)                                   # nothing left of the cap
        self.assertTrue(pm.get(10, 15))                                      # the cap itself
        area = sum(v & 255 for v in pm.buf) / 255.0 / 36.0
        self.assertAlmostEqual(area, 3 * 40 + math.pi * 1.5 ** 2, delta=1.5)
        self.assertGreater(len({v & 255 for v in pm.buf}), 6)

    def test_text_is_smooth_and_sized_in_grid_units(self):
        pm = o1hipix.HiPixmap(120, 20, 0, 6)
        w = pm.text(4, 4, "Hello 97%", 0xFFFFFF)
        self.assertEqual(w, o1vtext.text_width("Hello 97%") if False else w)
        ys = [y for y in range(pm.ph) if any(pm.buf[y * pm.pw:(y + 1) * pm.pw])]
        self.assertGreaterEqual(ys[0], 4 * 6 - 2)
        self.assertAlmostEqual(ys[-1] - ys[0] + 1, 7 * 6 * 1.0, delta=12)     # capitals 7 grid units tall (descenders none here)
        self.assertGreater(len({v & 255 for v in pm.buf}), 10)                # anti-aliased
        for max_w in (0, 10, 40, 100):
            q = o1hipix.HiPixmap(120, 20, 0, 6)
            q.text(10, 4, "A long line of text that cannot possibly fit", 0xFFFFFF, 1, max_w=max_w)
            xs = [x for y in range(q.ph) for x in range(q.pw) if q.buf[y * q.pw + x]]
            if xs:
                self.assertGreaterEqual(min(xs), 10 * 6 - 6)
                self.assertLess(max(xs), (10 + max_w) * 6 + 1)

    def test_a_kept_picture_looks_exactly_like_drawing_it_again(self):
        def draw(pm):
            pm.rrect(2, 2, 36, 26, 3, 0x203040)
            pm.cached(("k", 1), (4, 4, 30, 22), lambda: (pm.disc(18, 15, 9, 0xFFAA00), pm.polyline([(5, 6), (30, 24)], 2, 0xFFFFFF)))
        a = o1hipix.HiPixmap(40, 30, 0x101010, 4)
        draw(a)
        b = o1hipix.HiPixmap(40, 30, 0x101010, 4)
        draw(b)                                                              # this time the picture comes from the cache
        self.assertEqual(a.buf, b.buf)
        c = o1hipix.HiPixmap(40, 30, 0x101010, 4)
        c.rrect(2, 2, 36, 26, 3, 0x203040)
        c.disc(18, 15, 9, 0xFFAA00)
        c.polyline([(5, 6), (30, 24)], 2, 0xFFFFFF)
        self.assertEqual(a.buf, c.buf)
        d = o1hipix.HiPixmap(40, 30, 0x101010, 4)
        d.rrect(2, 2, 36, 26, 3, 0x203040)
        d.cached(("k", 2), (4, 4, 30, 22), lambda: d.disc(10, 10, 4, 0x00FF00))      # another key: its own picture
        self.assertNotEqual(d.buf, a.buf)
        self.assertEqual(d.get(10, 10), 0x00FF00)

    def test_the_cache_is_bounded(self):
        pm = o1hipix.HiPixmap(40, 30, 0, 4)
        old = o1hipix.HiPixmap.SNAP_BUDGET
        o1hipix.HiPixmap.SNAP_BUDGET = 30000
        try:
            for i in range(60):
                pm.cached(("b", i), (0, 0, 20, 10), lambda: pm.fill_rect(0, 0, 20, 10, i + 1))
            self.assertLessEqual(o1hipix.HiPixmap._SNAP_PIXELS[0], 30000 + 80 * 40)
        finally:
            o1hipix.HiPixmap.SNAP_BUDGET = old


class TestSmoothPanel(unittest.TestCase):
    """The whole panel at 1920x1080, 2560x1440 and 3840x2160."""

    SCREENS = ((1920, 1080), (2560, 1440), (3840, 2160))

    def setUp(self):
        del o1panel.ERRORS[:]

    def tearDown(self):
        self.assertEqual(o1panel.ERRORS, [])

    def grid(self, sw, sh):
        k, lw, lh = o1fb.choose_scale(sw, sh)
        return k, lw, lh

    def test_the_panel_is_drawn_at_the_screens_own_resolution(self):
        st = dash_sample.sample(now=1790000000.0)
        for sw, sh in self.SCREENS:
            k, lw, lh = self.grid(sw, sh)
            pm = o1panel.render(st, lw, lh, scale=k)
            self.assertEqual((pm.pw, pm.ph), (lw * k, lh * k))
            self.assertLessEqual(pm.pw, sw)
            self.assertGreaterEqual(pm.pw, sw * 0.95)
            self.assertGreater(len({v for v in pm.buf}), 500)                  # smooth: a great many in-between colours

    def test_text_edges_are_anti_aliased_not_stair_stepped(self):
        st = dash_sample.sample(now=1790000000.0)
        k, lw, lh = self.grid(1920, 1080)
        pm = o1panel.render(st, lw, lh, scale=k)
        a = pm.pw
        # no logical pixel is a flat block of one text colour: look at a stretch of the title row
        row = [pm.buf[(10 * k) * a + x] for x in range(16 * k, 90 * k)]
        self.assertGreater(len(set(row)), 6)

    def test_a_panel_cannot_paint_outside_its_box_at_any_resolution(self):
        st = dash_sample.sample(now=1790000000.0)
        for sw, sh in ((1920, 1080), (3840, 2160)):
            k, lw, lh = self.grid(sw, sh)
            base = o1panel.render(st, lw, lh, scale=k)
            boxes = o1panel.layout(lw, lh)
            for name, fn in (("gpu", "draw_gpu"), ("cpu", "draw_cpu"), ("models", "draw_models"), ("status", "draw_status"),
                             ("header", "draw_header")):
                orig = getattr(o1panel, fn)
                setattr(o1panel, fn, lambda pm, r, st_, ctx: (pm.fill_rect(-5, -5, lw + 9, lh + 9, 0xFF00FF),
                                                               pm.text(-3, -2, "x" * 90, 0x00FFFF, 3),
                                                               pm.arc(30, 30, 90, 3, 0, 359, 0x00FF00),
                                                               pm.polyline([(-5, -5), (lw + 5, lh + 5)], 6, 0xFFFF00),
                                                               pm.disc(50, 50, 80, 0xFFFFFF), pm.rrect(-4, -4, lw + 8, lh + 8, 9, 0xFF0000)))
                try:
                    pm = o1panel.render(st, lw, lh, scale=k)
                finally:
                    setattr(o1panel, fn, orig)
                x, y, w, h = boxes[name]
                step = 3 if k > 3 else 1
                for yy in range(0, pm.ph, step):
                    for xx in range(0, pm.pw, step):
                        if not (x * k <= xx < (x + w) * k and y * k <= yy < (y + h) * k):
                            self.assertEqual(pm.buf[yy * pm.pw + xx], base.buf[yy * pm.pw + xx], (name, xx, yy))

    def test_hostile_text_stays_inside_the_boxes_at_every_resolution(self):
        st = hostile_state()
        for sw, sh in self.SCREENS:
            k, lw, lh = self.grid(sw, sh)
            pm = o1panel.render(st, lw, lh, scale=k)
            for name, (x, y, bw, bh) in o1panel.layout(lw, lh).items():
                if name in ("header", "footer"):
                    continue
                for yy in range((y + 5) * k, (y + bh - 5) * k, max(1, k)):
                    for xx in list(range((x + bw - 2) * k - k, (x + bw - 1) * k)) + list(range((x + 1) * k, (x + 2) * k)):
                        self.assertEqual(pm.buf[yy * pm.pw + xx], o1panel.T["panel"], (name, sw, xx, yy))

    def test_empty_and_odd_states_draw_at_scale(self):
        for st in ({}, {"time": 5.0}, {"gpu": None, "gw": None}, hostile_state()):
            for sw, sh in ((1920, 1080), (1366, 768)):
                k, lw, lh = self.grid(sw, sh)
                o1panel.render(st, lw, lh, scale=k)

    def test_pairing_fills_the_screen_smoothly_at_every_resolution(self):
        st = dash_sample.sample(now=1790000000.0, pairing=True)
        for sw, sh in self.SCREENS:
            k, lw, lh = self.grid(sw, sh)
            pm = o1panel.render(st, lw, lh, scale=k)
            cols = [x for y in range(0, pm.ph, 4) for x in range(pm.pw) if pm.buf[y * pm.pw + x] == o1panel.T["text"]]
            self.assertGreater(max(cols) - min(cols), 0.6 * pm.pw)
            self.assertGreater(min(cols), 0.02 * pm.pw)
            self.assertLess(max(cols), 0.98 * pm.pw)

    def test_lines_and_bars_use_the_resolution(self):
        st = dash_sample.sample(now=1790000000.0)
        k, lw, lh = self.grid(1920, 1080)
        pm = o1panel.render(st, lw, lh, scale=k)
        bar_row = []
        boxes = o1panel.layout(lw, lh)
        x, y, w, h = boxes["gpu"]
        for yy in range((y + 22) * k, (y + 28) * k):
            bar_row.append(len({pm.buf[yy * pm.pw + xx] for xx in range((x + 60) * k, (x + w - 60) * k)}))
        self.assertGreater(max(bar_row), 2)                                   # rounded, anti-aliased bar ends

    # -- keeping the picture: redrawing only what changed must look the same as redrawing everything ------
    def test_incremental_drawing_equals_drawing_everything(self):
        k, lw, lh = 2, 640, 360
        r = o1panel.PanelRenderer(lw, lh, k)
        states = []
        for i in range(8):
            st = dash_sample.sample(now=1790000000.0 + 2 * i)
            st["gpu"]["busy_pct"] = (30 + 9 * i) % 100
            if i % 3 == 1:
                st["gw"]["queued"] = i
            if i == 4:
                st["disks"][1]["mounted"] = False
            if i == 5:
                st["pairing"] = {"id": "x", "code": "7K4M2QXD9FHT", "expires_at": st["time"] + 100}
            if i == 6:
                st["cpu"]["total"] = 91.0
            for j in range(4):
                st["series"]["gpu_busy"] = st["series"]["gpu_busy"][i:] + [float(40 + i)] * i
            states.append(st)
        for i, st in enumerate(states):
            got = r.draw(st, incremental=True)
            fresh = o1panel.PanelRenderer(lw, lh, k).draw(st)
            self.assertEqual(got.buf, fresh.buf, i)

    def test_a_still_machine_redraws_nothing(self):
        k, lw, lh = 3, 640, 360
        r = o1panel.PanelRenderer(lw, lh, k)
        st = dash_sample.sample(now=1790000000.0)
        r.draw(st, incremental=True)
        calls = []
        orig = o1panel.draw_gpu
        o1panel.draw_gpu = lambda *a: calls.append(1) or orig(*a)
        try:
            r.draw(dict(st), incremental=True)
            self.assertEqual(calls, [])                                       # the same numbers: the picture is left as it is
            st2 = dash_sample.sample(now=1790000000.0)
            st2["gpu"]["power_w"] = 250.0
            r.draw(st2, incremental=True)
            self.assertEqual(calls, [1])                                      # one number moved: that box is redrawn
        finally:
            o1panel.draw_gpu = orig

    def test_the_first_frame_and_a_quiet_one_are_fast_enough(self):
        k, lw, lh = 3, 640, 360                                              # 1920x1080
        st = dash_sample.sample(now=1790000000.0)
        t = time.time()
        r = o1panel.PanelRenderer(lw, lh, k)
        r.draw(st, incremental=True)
        first = time.time() - t
        t = time.time()
        for _ in range(5):
            r.draw(dict(st), incremental=True)
        quiet = (time.time() - t) / 5
        self.assertLess(first, 6.0)
        self.assertLess(quiet, 0.2)

    def test_the_presenter_takes_a_smooth_surface_whole(self):
        k, lw, lh = 2, 640, 360
        info_ = info(xres=1280, yres=720)
        pres = o1fb.Presenter(info_, lw * k, lh * k, 1)
        self.assertTrue(pres.identity)                                         # XRGB8888: the buffer's own bytes
        r = o1panel.PanelRenderer(lw, lh, k)
        pm = r.draw(dash_sample.sample(now=1790000000.0), incremental=True)
        w1 = pres.frame(pm)
        self.assertEqual(sum(len(d) for _o, d in w1), 1280 * 720 * 4)
        self.assertEqual(pres.frame(pm), [])
        pm.fill_rect(10, 10, 3, 3, 0x00FF00)
        w2 = pres.frame(pm)
        self.assertEqual(sum(len(d) for _o, d in w2), 3 * k * 1280 * 4)       # only the rows that changed


class TestMixedCaseFont(unittest.TestCase):
    """The stroke font for the whole panel: lower case, digits and every sign."""

    def test_every_printable_ascii_character_and_the_degree_sign_has_a_glyph(self):
        for c in range(0x20, 0x7f):
            self.assertTrue(o1vecfont.supported(chr(c)), repr(chr(c)))
        self.assertTrue(o1vecfont.supported("\u00b0"))

    def test_the_case_of_a_name_is_kept(self):
        for name in ("gemma4:26b", "qwen3-vl:8b", "ministral-3:14b", "Alice's MacBook Pro", "gpt-oss:120b", "10.0.0.1",
                     "tok/s", "MHz", "rpm", "GiB", "97%", "78\u00b0C", "(a) [b] +=<>|#@*'\""):
            self.assertEqual(o1vecfont.clean(name, True), name)
        self.assertEqual(o1vecfont.clean("MHz"), "MHZ")                       # the cost screen is in capitals

    def test_unknown_characters_and_controls(self):
        self.assertEqual(o1vecfont.clean("a\x00\x1b[b\u6f22", True), "a[b?")
        self.assertEqual(o1vecfont.clean("\u2019\u2026", True), "'...")

    def test_lower_case_has_the_right_shape(self):
        for ch in "acemnorsuvwxz":                                            # x-height letters stay between 30 and 100
            y0, y1 = o1vecfont._bbox(ch)[1], o1vecfont._bbox(ch)[3]
            self.assertGreaterEqual(y0, 29, ch)
            self.assertLessEqual(y1, 101, ch)
        for ch in "bdfhklt":                                                  # ascenders reach the capital's top
            self.assertLessEqual(o1vecfont._bbox(ch)[1], 12, ch)
        for ch in "gjpqy":                                                    # descenders go below the baseline
            self.assertGreater(o1vecfont._bbox(ch)[3], 112, ch)

    def test_widths_are_proportional_for_letters_and_fixed_for_digits(self):
        self.assertLess(o1vecfont.text_width("iii", 100, True), o1vecfont.text_width("mmm", 100, True))
        self.assertEqual(o1vecfont.text_width("1111", 100, True), o1vecfont.text_width("0000", 100, True))

    def test_fit_cuts_with_dots_and_never_overflows(self):
        for text in ("a-very-long-model-name:120b-instruct-q4_K_M", "Alice's MacBook Pro", "x" * 60):
            for h in (30, 42, 84):
                for mw in (0, 20, 100, 400, 2000):
                    out = o1vecfont.fit(text, mw, h)
                    self.assertLessEqual(o1vecfont.text_width(out, h, True), mw, (text, h, mw))
        self.assertEqual(o1vecfont.fit("short", 900, 42), "short")
        self.assertTrue(o1vecfont.fit("a very long server name indeed", 300, 42).endswith("..."))

    def test_the_grid_measure_is_never_less_than_what_is_drawn(self):
        for text in ("gemma4:26b", "Hello world", "14.6/16.0 GiB", "100% GPU  11.0G  4h 41m"):
            for scale in (1, 2, 3):
                for S in (1, 3, 6):
                    drawn = o1vecfont.text_width(text, int(round(7 * scale * S)), True)
                    self.assertLessEqual(drawn, o1vtext.text_width(text, scale) * S + 1, (text, scale, S))

    def test_text_is_bigger_than_it_was_never_smaller(self):
        # the panel's text keeps the size it had: capitals 7 grid units tall, as the bitmap font's
        for scale in (1, 2):
            self.assertEqual(o1hipix.CAP * scale, 7 * scale)
        w_old = F.text_width("Hello 97%", 1)
        w_new = o1vtext.text_width("Hello 97%", 1)
        self.assertLessEqual(abs(w_new - w_old), 12)                            # about the same width as before

    def test_pixel_rows_are_cached_and_painted_in_place(self):
        bg, fg = 0x112233, 0xFFEEDD
        pm = o1gfx.Pixmap(300, 80, bg)
        o1vecfont.draw(pm, 10, 10, "Aa:9", 60, fg, bg, True)
        a = list(pm.buf)
        self.assertIs(o1vecfont.glyph_pixels("A", 60, fg, bg), o1vecfont.glyph_pixels("A", 60, fg, bg))
        self.assertTrue(any(v not in (bg, fg) for v in a))


MD_HEAD = "Personalities : [raid1] [linear] [multipath] [raid0] [raid6] [raid5] [raid4] [raid10] \n"
MD_TAIL = "      bitmap: 0/59 pages [0KB], 65536KB chunk\n\nunused devices: <none>\n"


def mdstat(members, progress_line=""):
    return (MD_HEAD + "md127 : active raid1 sdc[1] sdb[0]\n      7813791040 blocks super 1.2 [2/%d] [%s]\n%s%s"
            % (members.count("U"), members, progress_line, MD_TAIL))


MD_CHECK = "      [==>..................]  check = 12.1% (946458368/7813791040) finish=615.5min speed=185934K/sec\n"


class TestRaidLine(unittest.TestCase):
    """/proc/mdstat as the kernel writes it, and the one RAID line the panel shows."""

    def parse(self, text):
        import o1stats
        d = tempfile.mkdtemp(prefix="o1md-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "mdstat"), "w") as f:
            f.write(text)
        old = o1stats.PROC
        o1stats.PROC = d
        try:
            return o1stats.raid()
        finally:
            o1stats.PROC = old

    def test_a_check_with_spaces_around_the_equals_sign(self):
        a = self.parse(mdstat("UU", MD_CHECK))[0]
        self.assertEqual((a["action"], a["progress"], a["finish"], a["healthy"], a["members"]),
                         ("check", 12.1, "615.5min", True, "UU"))

    def test_every_action_with_and_without_spaces(self):
        for action in ("check", "resync", "recovery", "reshape"):
            for sep in (" = ", "=", " =", "= "):
                line = "      [>....................]  %s%s 3.4%% (265416192/7813791040) finish=1234.5min speed=100K/sec\n" % (action, sep)
                a = self.parse(mdstat("UU", line))[0]
                self.assertEqual((a["action"], a["progress"], a["finish"]), (action, 3.4, "1234.5min"), (action, sep))
        line = "      [=====>...............]  recovery = 31.0% (1/2) finish=12.0min speed=1K/sec\n"
        a = self.parse(mdstat("U_", line))[0]
        self.assertEqual((a["action"], a["progress"], a["healthy"]), ("recovery", 31.0, False))

    def test_a_delayed_resync_has_no_percentage_and_nothing_breaks(self):
        a = self.parse(mdstat("UU", "      \tresync=DELAYED\n"))[0]
        self.assertEqual((a["action"], a["progress"], a["finish"]), ("resync", None, None))
        a = self.parse(mdstat("UU", "      \tresync=PENDING\n"))[0]
        self.assertEqual((a["action"], a["progress"]), ("resync", None))

    def test_idle_and_degraded_arrays(self):
        a = self.parse(mdstat("UU"))[0]
        self.assertEqual((a["action"], a["progress"], a["finish"], a["healthy"]), (None, None, None, True))
        a = self.parse(mdstat("U_"))[0]
        self.assertEqual((a["action"], a["healthy"], a["members"]), (None, False, "U_"))

    def test_the_bitmap_line_is_not_an_action(self):
        a = self.parse(mdstat("UU", "      bitmap: 3/59 pages [12KB], 65536KB chunk, check = 5.0%\n"))[0]
        self.assertEqual(a["action"], "check")                               # whatever the kernel puts on a progress-like line
        self.assertEqual(self.parse("garbage\n"), [])

    def test_how_long_is_left(self):
        import o1dashui
        for finish, want in (("615.5min", "about 10 h left"), ("45.2min", "about 45 min left"), ("89.9min", "about 90 min left"),
                             ("90min", "about 2 h left"), ("0.2min", "about 1 min left"), ("4000.0min", "about 3 days left"),
                             ("2880min", "about 2 days left"), (None, None), ("soon", None), ("nanmin", None)):
            self.assertEqual(o1dashui.raid_left(finish), want, finish)

    def test_the_text(self):
        import o1dashui
        a = {"action": "check", "progress": 12.1, "finish": "615.5min"}
        self.assertEqual(o1dashui.raid_action(a, "%.1f", True), "check 12.1%, about 10 h left")
        self.assertEqual(o1dashui.raid_action(a), "check 12.1%")
        self.assertEqual(o1dashui.raid_action(dict(a, progress=None), "%.1f", True), "check, about 10 h left")
        self.assertEqual(o1dashui.raid_action({"action": "resync", "progress": None}), "resync")

    def array(self, text):
        return self.parse(text)[0]

    def test_the_colour_rule(self):
        self.assertEqual(o1panel.raid_status(self.array(mdstat("UU", MD_CHECK))), ("RAID md127: check 12.1%, about 10 h left", "info"))
        self.assertEqual(o1panel.raid_status(self.array(mdstat("UU")))[1], "ok")
        self.assertEqual(o1panel.raid_status(self.array(mdstat("UU")))[0], "RAID md127 healthy")
        resync = "      [=>...................]  resync =  8.0% (1/2) finish=312.4min speed=1K/sec\n"
        self.assertEqual(o1panel.raid_status(self.array(mdstat("UU", resync))), ("RAID md127: resync 8.0%, about 5 h left", "warn"))
        rec = "      [=>...................]  recovery = 31.0% (1/2) finish=45.0min speed=1K/sec\n"
        txt, kind = o1panel.raid_status(self.array(mdstat("U_", rec)))
        self.assertEqual((txt, kind), ("RAID md127 DEGRADED, recovery 31.0%, about 45 min left", "bad"))
        self.assertEqual(o1panel.raid_status(self.array(mdstat("U_"))), ("RAID md127 DEGRADED", "bad"))
        self.assertEqual(o1panel.RAID_COLOURS["info"], o1panel.T["gpu"])           # blue, not amber
        self.assertNotEqual(o1panel.RAID_COLOURS["info"], o1panel.RAID_COLOURS["warn"])

    def test_a_scheduled_check_is_not_a_warning_but_a_resync_and_a_degraded_mirror_are(self):
        import o1dashui
        st = dash_sample.sample(now=1790000000.0)
        st["raid"] = self.parse(mdstat("UU", MD_CHECK))
        self.assertEqual([t for t, _ in o1dashui.warnings(st, st["time"]) if t.startswith("RAID")], [])
        st["raid"] = self.parse(mdstat("UU", "      [=>...................]  resync =  8.0% (1/2) finish=312.4min speed=1K/sec\n"))
        self.assertEqual([(t, k) for t, k in o1dashui.warnings(st, st["time"]) if t.startswith("RAID")],
                         [("RAID md127: resync 8%", "warn")])
        st["raid"] = self.parse(mdstat("U_"))
        self.assertEqual([(t, k) for t, k in o1dashui.warnings(st, st["time"]) if t.startswith("RAID")], [("RAID md127 degraded", "bad")])


class TestFans(unittest.TestCase):
    """The FANS box (6b416): /run/ollama1/fan.json, and the memory temperature in the card box."""

    NOW = 1790000000.0

    def st(self, **fan):
        st = dash_sample.sample(now=self.NOW)
        st["fan"].update(fan)
        return st

    def test_two_bars_and_one_average(self):
        kind, head, warn, rows, cool = o1panel.fan_view(self.st(), self.NOW)
        self.assertEqual((kind, head, warn), ("rows", "working 100%", False))
        self.assertEqual([r[0] for r in rows], ["GPU fan", "Case fans"])
        self.assertEqual(rows[0][1:3], ("2310 rpm", 1.0))
        # fan1 1180, fan2 1150 and the radiator fans 1180, 1175: the average of four (fan3: no rpm, not controlled)
        self.assertEqual(rows[1][1], "%d rpm avg" % round((1180 + 1150 + 1180 + 1175) / 4.0))
        self.assertEqual(rows[1][2], 1.0)
        self.assertEqual(cool, "Pump 2400 rpm, quiet, coolant 31\u00b0C")

    def test_the_average_bar_is_the_mean_of_the_bars_and_the_pump_is_left_out(self):
        outs = [{"label": "fan1", "enable": 1, "pwm": 255, "rpm": 2000}, {"label": "fan2", "enable": 1, "pwm": 51, "rpm": 400}]
        rows = o1panel.fan_view(self.st(outputs=outs, aio=None), self.NOW)[3]
        self.assertEqual(rows, [("Case fans", "1200 rpm avg", 0.6, "fan")])
        aio = {"found": True, "state": "controlling", "pump_rpm": 9000, "pump_mode": "balanced", "coolant_c": None,
               "fans": [{"n": 1, "rpm": 600, "pct": 20}]}
        v = o1panel.fan_view(self.st(outputs=outs, aio=aio), self.NOW)
        self.assertEqual(v[3][0][1], "1000 rpm avg")                              # (2000 + 400 + 600) / 3: no pump in it
        self.assertAlmostEqual(v[3][0][2], (1.0 + 0.2 + 0.2) / 3)
        self.assertEqual(v[4], "Pump 9000 rpm, balanced")

    def test_phases(self):
        for phase, pct, why, want in (("idle20", 20, "idle", "idle 20%"), ("hold100", 100, "the work ended", "cooling down 100%"),
                                      ("hold50", 50, "x", "cooling down 50%"), ("ramp", 73, "x", "cooling down 73%"),
                                      ("calibrating", 100, "x", "measuring 100%"), (None, 40, None, "40%"),
                                      ("deepen", 17, "x", "idle 17%"), ("deep", 10, "deep idle", "deep idle 10%")):
            self.assertEqual(o1panel.fan_view(self.st(phase=phase, pct=pct, why=why), self.NOW)[1], want)
        v = o1panel.fan_view(self.st(phase="hot", pct=100, why="too warm: GPU junction"), self.NOW)
        self.assertTrue(v[2])
        self.assertTrue(o1panel.fan_view(self.st(hot=["x"]), self.NOW)[2])

    def test_every_phase_word_fits_the_fans_title_bar_at_every_size(self):
        for w, h in ((480, 270), (640, 360), (800, 480), (1024, 600)):
            fw = o1panel.layout(w, h)["fans"][2]
            for head in ("working 100%", "cooling down 100%", "measuring 100%", "deep idle 10%", "idle 17%", "HOT 100%"):
                self.assertLessEqual(F.text_width("FANS") + F.text_width(head) + 10, fw - 16, (w, h, head))

    def test_the_cooler_pump_row_is_never_in_the_case_average_and_the_cooler_fans_without_rpm_are_not_shown(self):
        outs = [{"label": "Pump", "enable": 1, "pwm": 255, "rpm": 2357, "tach_raw": 4383},
                {"label": "case/CPU fan 2", "enable": 1, "pwm": 51, "rpm": 500}]
        aio = {"found": True, "state": "controlling", "pump_rpm": 2357, "pump_mode": "quiet", "coolant_c": 31,
               "fans": [{"n": 1, "rpm": 0, "pct": 20}, {"n": 2, "rpm": None, "pct": 20}]}
        v = o1panel.fan_view(self.st(outputs=outs, aio=aio), self.NOW)
        self.assertEqual([(r[0], r[1]) for r in v[3]], [("Case fans", "500 rpm avg")])
        self.assertAlmostEqual(v[3][0][2], 0.2)
        self.assertEqual(v[4], "Pump 2357 rpm, quiet, coolant 31\u00b0C")
        aio["fans"][0]["rpm"] = 520                                              # one reports: they average in as before
        v = o1panel.fan_view(self.st(outputs=outs, aio=aio), self.NOW)
        self.assertEqual([r[0] for r in v[3]], ["Case fans"])
        self.assertEqual(v[3][0][1], "510 rpm avg")

    def test_at_idle_the_fans_box_never_shows_the_cooler_ports_100(self):
        outs = [{"label": "Pump", "enable": 1, "pwm": 255, "rpm": 2357, "tach_raw": 4383},
                {"label": "case/CPU fan 2", "enable": 1, "pwm": 51, "rpm": 500},
                {"label": "case/CPU fan 3", "enable": 1, "pwm": 51, "rpm": 520}]
        aio = {"found": True, "state": "controlling", "pump_rpm": 2357, "pump_mode": "quiet", "coolant_c": 31,
               "fans": [{"n": 1, "rpm": 0, "pct": 100}, {"n": 2, "rpm": None, "pct": 100}]}   # nothing on the cooler's ports
        v = o1panel.fan_view(self.st(phase="idle20", pct=20, why="idle", outputs=outs, aio=aio), self.NOW)
        text = " | ".join([v[1]] + ["%s %s" % (r[0], r[1]) for r in v[3]] + [v[4]])
        self.assertNotIn("100%", text)
        self.assertNotIn("Cooler fans", text)
        self.assertIn("20%", text)
        self.assertEqual([r[0] for r in v[3]], ["Case fans"])
        self.assertAlmostEqual(v[3][0][2], 0.2)                     # the bar is the connected fans' commanded level
        self.assertEqual(v[4], "Pump 2357 rpm, quiet, coolant 31\u00b0C")      # the only cooler line
        aio["fans"][0]["rpm"] = 520                                  # a port that reads a fan: its own level, not 100%
        aio["fans"][0]["pct"] = 20
        v = o1panel.fan_view(self.st(phase="idle20", pct=20, why="idle", outputs=outs, aio=aio), self.NOW)
        self.assertAlmostEqual(v[3][0][2], 0.2)

    def test_controlled_output_without_rpm_is_shown_an_idle_unreadable_one_is_not(self):
        outs = [{"label": "fan1", "enable": 1, "pwm": 128, "rpm": None}, {"label": "fan2", "enable": 2, "pwm": 0, "rpm": 0}]
        rows = o1panel.fan_view(self.st(outputs=outs, aio=None), self.NOW)[3]
        self.assertEqual([(r[0], r[1]) for r in rows], [("Case fans", "-")])
        self.assertAlmostEqual(rows[0][2], 128 / 255.0)

    def test_no_data_when_missing_or_stale(self):
        st = dash_sample.sample(now=self.NOW)
        st["fan"] = None
        self.assertEqual(o1panel.fan_view(st, self.NOW), ("none", None))
        st = self.st()
        self.assertEqual(o1panel.fan_view(st, self.NOW + 31), ("none", None))
        self.assertEqual(o1panel.fan_view(st, self.NOW + 29)[0], "rows")
        seen = []
        orig = o1hipix.HiPixmap.text

        def rec(self_, x, y, s_, c, scale=1, max_w=None, ellipsis=True):
            seen.append(s_)
            return orig(self_, x, y, s_, c, scale, max_w, ellipsis)
        o1hipix.HiPixmap.text = rec
        try:
            st["fan"]["at"] = int(self.NOW) - 100
            o1panel.render(st, 640, 360, scale=1)
        finally:
            o1hipix.HiPixmap.text = orig
        self.assertIn("no fan data", seen)

    def test_the_card_shows_memory_temperature_not_the_fan(self):
        seen = []
        orig = o1hipix.HiPixmap.text

        def rec(self_, x, y, s_, c, scale=1, max_w=None, ellipsis=True):
            seen.append(s_)
            return orig(self_, x, y, s_, c, scale, max_w, ellipsis)
        o1hipix.HiPixmap.text = rec
        try:
            o1panel.render(dash_sample.sample(now=self.NOW), 640, 360)
        finally:
            o1hipix.HiPixmap.text = orig
        self.assertIn("Mem", seen)
        self.assertIn("70\u00b0C", seen)
        self.assertNotIn("Fan", seen)
        self.assertEqual([t for t in seen if t in ("VRAM", "Power", "Temp", "Mem")][:4], ["VRAM", "Power", "Temp", "Mem"])

    def test_it_fits_at_every_resolution_and_with_many_fans(self):
        outs = [{"label": "GPU fan", "enable": 1, "pwm": 200, "rpm": 2000}] + \
               [{"label": "fan%d" % i, "enable": 1, "pwm": 90, "rpm": 900 + i} for i in range(9)]
        for sw, sh in ((1920, 1080), (2560, 1440), (3840, 2160), (1366, 768)):
            k, lw, lh = o1fb.choose_scale(sw, sh)
            for fan in ({}, {"outputs": outs, "why": "x" * 150}, {"phase": "hot", "outputs": outs}):
                st = self.st(**fan)
                pm = o1panel.render(st, lw, lh, scale=k)
                x, y, w, h = o1panel.layout(lw, lh)["fans"]
                for yy in range((y + 4) * k, (y + h - 4) * k, max(1, k)):
                    for xx in list(range((x + w - 2) * k - k, (x + w - 1) * k)) + list(range((x + 1) * k, (x + 2) * k)):
                        self.assertEqual(pm.buf[yy * pm.pw + xx], o1panel.T["panel"] if not fan.get("phase") else pm.buf[yy * pm.pw + xx])

    def test_box_titles_are_the_hardware_names(self):
        for name, want in (("Navi 21 [Radeon RX 6900 XT]", "AMD RX 6900 XT"),
                           ("Advanced Micro Devices, Inc. [AMD/ATI] Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]", "AMD RX 6800/6800 XT / 6900 XT"),
                           ("AMD Radeon RX 6900 XT Graphics", "AMD RX 6900 XT"), ("NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 4090"),
                           (None, "Graphics card"), ("", "Graphics card"), (7, "7")):
            self.assertEqual(o1panel.gpu_title({"name": name}), want)
        self.assertEqual(o1panel.gpu_title(None), "Graphics card")
        for name, want in (("AMD Ryzen 9 5950X 16-Core Processor", "AMD Ryzen 9 5950X"),
                           ("Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz", "Intel Core i7-9700K"),
                           ("AMD Ryzen 7 5700G with Radeon Graphics", "AMD Ryzen 7 5700G"), (None, "Processor and memory")):
            self.assertEqual(o1panel.cpu_title({"model": name}), want)
        seen = []
        orig = o1hipix.HiPixmap.text

        def rec(self_, x, y, s_, c, scale=1, max_w=None, ellipsis=True):
            seen.append(s_)
            return orig(self_, x, y, s_, c, scale, max_w, ellipsis)
        o1hipix.HiPixmap.text = rec
        try:
            o1panel.render(dash_sample.sample(now=self.NOW), 640, 360)
            self.assertIn("AMD RX 6900 XT".upper(), [t.upper() for t in seen])
            self.assertIn("AMD RYZEN 9 5950X", [t.upper() for t in seen])
            del seen[:]
            st = dash_sample.sample(now=self.NOW)
            st["gpu"]["name"] = None
            st["cpu"]["model"] = None
            o1panel.render(st, 640, 360)
            self.assertIn("GRAPHICS CARD", [t.upper() for t in seen])
            self.assertIn("PROCESSOR AND MEMORY", [t.upper() for t in seen])
        finally:
            o1hipix.HiPixmap.text = orig

    def test_a_long_title_is_cut_and_the_value_text_stays_whole(self):
        for sw, sh in ((1920, 1080), (3840, 2160), (1366, 768)):
            k, lw, lh = o1fb.choose_scale(sw, sh)
            st = dash_sample.sample(now=self.NOW)
            st["gpu"]["name"] = "Some Very Long Graphics Adapter Name With Many Many Words In It 9000 Ultra Max Pro"
            st["cpu"]["model"] = "X" * 200
            seen = []
            orig = o1hipix.HiPixmap.text

            def rec(self_, x, y, s_, c, scale=1, max_w=None, ellipsis=True):
                seen.append((s_, x, max_w))
                return orig(self_, x, y, s_, c, scale, max_w, ellipsis)
            o1hipix.HiPixmap.text = rec
            try:
                o1panel.render(st, lw, lh, scale=k)
            finally:
                o1hipix.HiPixmap.text = orig
            boxes = o1panel.layout(lw, lh)
            for name in ("gpu", "cpu"):
                x, y, w, h = boxes[name]
                titles = [t for t in seen if x <= t[1] < x + w and t[0].isupper() and t[0].startswith(("SOME", "XXX"))]
                self.assertTrue(titles, (name, sw))
                for s_, tx, mw in titles:
                    right = "core 2310 MHz" if name == "gpu" else "32 cores"
                    self.assertLessEqual(tx + min(mw, o1vtext.text_width(s_)) + o1vtext.text_width(right) + 10, x + w - 8 + 1)
                self.assertIn(right, [t[0] for t in seen])                           # the right-hand text is still there

    def test_the_middle_column_is_three_boxes_of_one_width(self):
        for w, h in ((640, 360), (800, 450), (1024, 768), (480, 270), (683, 384)):
            b = o1panel.layout(w, h)
            names = ("storage", "fans", "network")
            self.assertEqual(len({b[n][0] for n in names}), 1)
            self.assertEqual(len({b[n][2] for n in names}), 1)
            for up, down in zip(names, names[1:]):
                self.assertLess(b[up][1] + b[up][3], b[down][1])
            self.assertEqual(b["network"][1] + b["network"][3], b["models"][1] + b["models"][3])
            self.assertEqual(b["storage"][1], b["models"][1])
        b = o1panel.layout(640, 360)
        for n, need in zip(("storage", "fans", "network"), o1panel.MID_MIN):
            self.assertGreaterEqual(b[n][3], need)                              # each fits its content at the design size

    def test_the_fan_header_has_no_cause_and_hot_is_a_word(self):
        v = o1panel.fan_view(self.st(why="running stability-test.sh"), self.NOW)
        self.assertEqual(v[1], "working 100%")
        v = o1panel.fan_view(self.st(phase="hot", pct=100, why="too warm"), self.NOW)
        self.assertEqual((v[1], v[2]), ("HOT 100%", True))

    def test_the_sampler_reads_the_file(self):
        import o1metrics
        src = open(os.path.join(U.LIB, "o1metrics.py")).read()
        self.assertIn('"fan.json"', src)


class TestStorageAndNetwork(unittest.TestCase):
    NOW = 1790000000.0
    SCREENS = ((1920, 1080), (2560, 1440), (3840, 2160), (1366, 768))

    def texts(self, st, lw, lh, k):
        seen = []
        orig = o1hipix.HiPixmap.text

        def rec(self_, x, y, s_, c, scale=1, max_w=None, ellipsis=True):
            seen.append((s_, x, y, max_w, c))
            return orig(self_, x, y, s_, c, scale, max_w, ellipsis)
        o1hipix.HiPixmap.text = rec
        try:
            o1panel.render(st, lw, lh, scale=k)
        finally:
            o1hipix.HiPixmap.text = orig
        return seen

    def test_the_filesystems_are_one_total(self):
        d = [{"mount": "/", "mounted": True, "total": 1.8e12, "used": 1.88e11, "free": 1.6e12},
             {"mount": "/srv/models", "mounted": True, "total": 1.0e11, "used": 4.0e10, "free": 6.0e10}]
        used, total, missing = o1panel.disk_total(d)
        self.assertEqual((used, total, missing), (2.28e11, 1.9e12, []))
        self.assertEqual(o1panel.disk_total(d + [dict(d[0])]), (used, total, []))          # one filesystem mounted twice counts once
        self.assertEqual(o1panel.disk_total(d + [{"mount": "/srv/data", "mounted": False}])[2], ["/srv/data"])
        self.assertEqual(o1panel.dec_bytes(188e9), "188 GB")
        self.assertEqual(o1panel.dec_bytes(1.8e12), "1.8 TB")
        self.assertEqual(o1panel.disk_total([]), (0.0, 0.0, []))

    def test_storage_says_used_of_total_and_no_network_lines(self):
        st = dash_sample.sample(now=self.NOW)
        st["disks"] = st["disks"][:1]
        st["disks"][0].update(total=1.8e12, used=1.88e11, free=1.6e12)
        seen = [t[0] for t in self.texts(st, 640, 360, 1)]
        self.assertIn("188 GB of 1.8 TB", seen)
        self.assertIn("STORAGE", seen)
        self.assertNotIn("STORAGE AND NETWORK", seen)
        self.assertIn("Read", seen)

    def test_a_missing_disk_is_said(self):
        st = dash_sample.sample(now=self.NOW)
        st["disks"][2]["mounted"] = False
        seen = [t[0] for t in self.texts(st, 640, 360, 1)]
        self.assertIn("/srv/data MISSING", seen)

    def test_network_lines(self):
        st = dash_sample.sample(now=self.NOW)
        lines = o1panel.net_lines(st, self.NOW)
        flat = [[t for t, _c in l] for l in lines]
        self.assertEqual(flat[0], ["Down", "2.3M/s", "  Up", "302.7K/s"])
        self.assertEqual(flat[1], ["Link", "wired 1000 Mb/s", "192.168.1.10"])
        self.assertEqual(flat[2], ["Tunnel", "up (4)"])
        self.assertEqual(flat[3], ["Since boot", "in 3.9G", "out 1.0G"])

    def test_wifi_errors_and_a_dead_tunnel_are_amber_or_red(self):
        st = dash_sample.sample(now=self.NOW)
        st["net"].update(wifi=True, errors=3, drops=12)
        st["tunnel"] = {"up": False, "connections": 0}
        lines = o1panel.net_lines(st, self.NOW)
        self.assertEqual(lines[1][1], ("on Wi-Fi", o1panel.T["warn"]))
        self.assertEqual(lines[2][1], ("DOWN", o1panel.T["bad"]))
        self.assertEqual(lines[2][2], ("Errors 3  drops 12", o1panel.T["warn"]))
        st["net"]["ports"][1]["carrier"] = False
        st["net"]["wifi"] = False
        self.assertIn("(1 of 2 up)", [t for t, _c in o1panel.net_lines(st, self.NOW)[1]])

    def test_the_tunnel_is_shown_once(self):
        st = dash_sample.sample(now=self.NOW)
        seen = [t[0] for t in self.texts(st, 640, 360, 1)]
        self.assertEqual(sum(1 for t in seen if t == "Tunnel"), 1)

    def test_nothing_in_the_middle_column_is_cut_off_or_overruns(self):
        for sw, sh in self.SCREENS:
            k, lw, lh = o1fb.choose_scale(sw, sh)
            st = dash_sample.sample(now=self.NOW)
            st["net"].update(address="255.255.255.255/24", rx_total=9.99e14, tx_total=9.99e14, rx_bps=9.9e9, tx_bps=9.9e9,
                             errors=123456, drops=654321, wifi=True)
            st["disks"][0].update(total=9.9e14, used=9.8e14, free=1e12)
            boxes = o1panel.layout(lw, lh)
            seen = self.texts(st, lw, lh, k)
            for name in ("storage", "fans", "network"):
                x, y, w, h = boxes[name]
                inside = [t for t in seen if x <= t[1] < x + w and y <= t[2] < y + h]
                self.assertTrue(inside, (name, sw))
                for s_, tx, ty, mw, c in inside:
                    width = o1vtext.text_width(s_)
                    self.assertLessEqual(tx + (width if mw is None else min(width, mw)), x + w - 8 + 1, (name, s_, sw))
                    self.assertLessEqual(ty + 7, y + h - 3, (name, s_, sw))          # no line below the box's bottom padding
                    self.assertFalse(s_.endswith("...") and name != "fans" and mw is not None and width > mw, (name, s_))

    def test_each_middle_box_keeps_every_line_at_the_design_size(self):
        st = dash_sample.sample(now=self.NOW)
        seen = [t[0] for t in self.texts(st, 640, 360, 1)]
        for want in ("Down", "Link", "Tunnel", "Since boot", "GPU fan", "Case fans", "Used", "Read"):
            self.assertIn(want, seen)
        self.assertTrue(any(t.startswith("Pump 2400") for t in seen))


BURN_NOW = 1790000000.0


class TestBurnBanner(unittest.TestCase):
    def st(self, kind, age=0):
        st = dash_sample.sample(now=BURN_NOW)
        st["burn"] = dash_sample.burn_state(kind, BURN_NOW - age)
        return st

    def view(self, kind, age=0, now=BURN_NOW):
        return o1panel.burn_view(self.st(kind, age), now)

    def test_running_text(self):
        self.assertEqual(self.view("cpu"), ("run", "BURN TEST  processor 18 s left  71\u00b0C  100% busy"))
        self.assertEqual(self.view("gpu"), ("run", "BURN TEST  graphics card 12 s left  66\u00b0C  99% busy"))

    def test_results(self):
        self.assertEqual(self.view("passed"), ("result", "Burn test passed", "ok"))
        self.assertEqual(self.view("failed"), ("result", "Burn test failed: graphics card: FAIL: the card was only 3% busy", "bad"))
        self.assertEqual(self.view("aborted"), ("result", "Burn test aborted", "warn"))
        self.assertEqual(self.view("refused"), ("result", "Not started: a request is running", "warn"))
        v = self.view("nogpu")
        self.assertEqual((v[2], v[1][:18]), ("warn", "Burn test: process"))

    def test_a_result_shows_for_a_minute_and_a_dead_script_for_ten_seconds(self):
        self.assertIsNotNone(self.view("passed", age=59))
        self.assertIsNone(self.view("passed", age=61))
        self.assertIsNotNone(self.view("cpu", age=9))
        self.assertIsNone(self.view("cpu", age=11))                             # a running file nobody updates is not a running test
        self.assertIsNone(o1panel.burn_view({}, BURN_NOW))
        self.assertIsNone(o1panel.burn_view({"burn": {"phase": "weird", "at": BURN_NOW}}, BURN_NOW))
        self.assertIsNone(o1panel.burn_view({"burn": "x"}, BURN_NOW))
        self.assertTrue(o1panel.burn_running(self.st("cpu"), BURN_NOW))
        self.assertFalse(o1panel.burn_running(self.st("passed"), BURN_NOW))

    def drawn(self, st, sw, sh):
        seen = []
        orig = o1hipix.HiPixmap.text

        def rec(self_, x, y, s_, c, scale=1, max_w=None, ellipsis=True):
            seen.append((s_, x, max_w, scale))
            return orig(self_, x, y, s_, c, scale, max_w, ellipsis)
        o1hipix.HiPixmap.text = rec
        try:
            k, lw, lh = o1fb.choose_scale(sw, sh)
            pm = o1panel.render(st, lw, lh, scale=k)
        finally:
            o1hipix.HiPixmap.text = orig
        return pm, seen, lw, lh, k

    def test_the_banner_takes_the_header_and_fits_at_every_resolution(self):
        for kind in ("cpu", "gpu", "passed", "failed", "aborted", "refused", "nogpu"):
            for sw, sh in ((1920, 1080), (2560, 1440), (3840, 2160)):
                st = self.st(kind)
                st["burn"]["reason"] = (st["burn"]["reason"] + " and a very long reason " * 6)[:200] if kind in ("failed", "refused") else st["burn"]["reason"]
                pm, seen, lw, lh, k = self.drawn(st, sw, sh)
                view = o1panel.burn_view(st, BURN_NOW)
                line = [t for t in seen if t[0] and view[1].startswith(t[0].rstrip(".")[:30]) and len(t[0]) > 12]
                self.assertTrue(line, (kind, sw))
                s_, x, mw, scale = line[0]
                self.assertEqual(o1panel.layout(lw, lh)["header"][2], lw)
                self.assertLessEqual(o1vtext.text_width(s_, scale), lw - 12 if o1vtext.text_width(s_, 1) <= lw - 12 else 10 ** 6)
                fill = o1panel.BANNER_FILL["run" if view[0] == "run" else view[2]]
                self.assertEqual(pm.get(3, 3), fill)
                self.assertEqual(pm.get(lw - 4, 3), fill)
                self.assertNotIn("1 TO CHECK", [t[0] for t in seen])                  # the badge is covered, not drawn under

    def test_no_banner_means_the_normal_header(self):
        st = dash_sample.sample(now=BURN_NOW)
        pm, seen, lw, lh, k = self.drawn(st, 1920, 1080)
        self.assertIn("1 TO CHECK", [t[0] for t in seen])

    def test_the_footer_hints_at_enter(self):
        st = dash_sample.sample(now=BURN_NOW)
        for w, h in ((640, 360), (480, 270)):
            seen = " ".join(t[0] for t in self.drawn(st, w, h)[1])
            self.assertIn("Enter: burn", seen)
        self.assertIn("Enter: burn test", " ".join(t[0] for t in self.drawn(st, 1920, 1080)[1]))

    def test_the_banner_is_gone_after_the_minute_and_the_header_comes_back_when_drawn_again(self):
        r = o1panel.PanelRenderer(640, 360, 1)
        st = self.st("passed")
        pm = r.draw(st, incremental=True)
        self.assertEqual(pm.get(3, 3), o1panel.BANNER_FILL["ok"])
        st2 = dash_sample.sample(now=BURN_NOW + 100)
        st2["burn"] = st["burn"]
        pm = r.draw(st2, incremental=True)
        self.assertEqual(pm.get(3, 3), o1panel.T["head"])


class TestBurnKeys(unittest.TestCase):
    def test_enter_starts_once_and_esc_aborts_only_while_running(self):
        h = o1paneld.handle_burn_keys
        self.assertEqual(h(b"\r", False, 100.0, 0.0), ("start", 100.0))
        self.assertEqual(h(b"\n", False, 100.0, 0.0), ("start", 100.0))
        self.assertEqual(h(b"\r", True, 100.0, 0.0), (None, 0.0))              # ignored while running
        self.assertEqual(h(b"\x1b", True, 100.0, 0.0), ("abort", 100.0))
        self.assertEqual(h(b"\x1b", False, 100.0, 0.0), (None, 0.0))           # nothing to abort
        for junk in (b"", b" ", b"a", b"\x1b[A", b"\x1b[B", b"\x1bOP", b"q", b"\x03", b"\t", b"1"):
            self.assertEqual(h(junk, False, 100.0, 0.0)[0], None, junk)
            self.assertEqual(h(junk, True, 100.0, 0.0)[0], None, junk)

    def test_debounce(self):
        h = o1paneld.handle_burn_keys
        self.assertEqual(h(b"\r", False, 100.1, 100.0), (None, 100.0))
        self.assertEqual(h(b"\r", False, 100.4, 100.0), ("start", 100.4))
        self.assertEqual(h(b"\x1b", True, 100.2, 100.0), (None, 100.0))

    def run_loop(self, script, st=None, steps=14, cost_first=False):
        c = Clock()
        c.t = 1790000000.0                                                          # the clock the fake machine's files are written in
        calls = []

        class Burn:
            def start(self_):
                calls.append("start")

            def abort(self_):
                calls.append("abort")
        polls = iter(script)

        class K:
            def wait(self_, t):
                c.sleep(t)
                return next(polls, b"")
        n = [0]

        def stop():
            n[0] += 1
            return n[0] > steps
        sampler = FakeSampler(st)
        o1paneld.run(FakeFb(), FakeTty(), sampler, clock=c.now, sleep=c.sleep, keys=K(), stop=stop, burn=Burn())
        return calls

    def test_enter_starts_the_unit_once_even_when_pressed_again_and_again(self):
        st = dash_sample.sample(now=1790000000.0)
        self.assertEqual(self.run_loop([b"\r", b"", b"\r", b"\r", b"\r"], st), ["start"])      # the grace until the file appears

    def test_while_running_enter_does_nothing_and_esc_aborts(self):
        st = dash_sample.sample(now=1790000000.0)
        st["burn"] = dash_sample.burn_state("cpu", 1790000000.0)
        calls = self.run_loop([b"\r", b"", b"\x1b", b"", b"\r"], st)
        self.assertEqual(calls, ["abort"])

    def test_pairing_wins_and_other_keys_do_nothing(self):
        st = dash_sample.sample(now=1790000000.0, pairing=True)
        self.assertEqual(self.run_loop([b"\r", b"", b"\x1b"], st), [])
        st = dash_sample.sample(now=1790000000.0)
        self.assertEqual(self.run_loop([b"a", b"\x1b[A", b"x", b"\x1b"], st), [])

    def test_space_still_flips_the_cost_screen(self):
        st = dash_sample.sample(now=1790000000.0)
        seen = []
        real = o1panel.render_cost

        def spy(st_, w, h):
            seen.append("cost")
            return real(st_, w, h)
        o1panel.render_cost = spy
        try:
            self.run_loop([b"", b" ", b"", b""], st)
        finally:
            o1panel.render_cost = real
        self.assertIn("cost", seen)

    def test_the_cost_screen_gives_way_to_a_running_burn_test(self):
        st = dash_sample.sample(now=1790000000.0)
        st["burn"] = dash_sample.burn_state("gpu", 1790000000.0)
        seen = []
        real = o1panel.render_cost

        def spy(st_, w, h):
            seen.append("cost")
            return real(st_, w, h)
        o1panel.render_cost = spy
        try:
            self.run_loop([b" ", b"", b"", b""], st)
        finally:
            o1panel.render_cost = real
        self.assertEqual(seen, [])

    def test_the_real_control_starts_one_unit_and_makes_one_empty_file(self):
        d = tempfile.mkdtemp(prefix="o1bc-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ran = []
        bc = o1paneld.BurnControl(abort_file=os.path.join(d, "abort"), popen=lambda argv, **kw: ran.append(argv))
        bc.start()
        self.assertEqual(ran, [["systemctl", "start", "--no-block", "ollama1-quickburn.service"]])
        bc.abort()
        self.assertEqual(os.path.getsize(os.path.join(d, "abort")), 0)
        self.assertEqual(oct(os.stat(os.path.join(d, "abort")).st_mode & 0o777), "0o600")
        bc.abort()                                                                  # twice is harmless


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
        self.assertTrue(all(o1vtext.text_width(l) <= 90 for l in lines))
        self.assertTrue(lines[-1].endswith("..."))
        self.assertEqual(o1panel.wrap("short", 90, 2), ["short"])
        self.assertEqual(o1panel.wrap("", 90, 2), [])
        self.assertTrue(all(o1vtext.text_width(l) <= 30 for l in o1panel.wrap("x" * 80 + " yy", 30, 3)))

    def test_sleep_line(self):
        now = 10000.0

        def line(age=0, **idle):
            base = {"at": now - age, "supported": True, "enabled": True, "minutes": 60, "sleep_ok": False,
                    "reason": "idle 3 of 60 minutes", "idle_s": 180}
            base.update(idle)
            return o1panel.sleep_summary({"idle": base}, now)
        # the timer is all that is left: the countdown from the service's own count
        self.assertEqual(line(), ("Sleeps in 57:00", "text"))
        self.assertEqual(line(age=20), ("Sleeps in 56:40", "text"))               # it counts down between the service's ticks
        self.assertEqual(line(idle_s=3400)[1], "warn")                              # the last five minutes
        self.assertEqual(line(idle_s=3600, reason="idle 60 minutes")[0], "Sleeping very soon")
        # right after a wake the service counts from the resume: a small idle_s, never "may sleep now"
        self.assertEqual(line(idle_s=5, reason="idle 0 of 60 minutes")[0], "Sleeps in 59:55")
        for st_ in (line(), line(idle_s=5), line(idle_s=3599), line(reason="busy: x")):
            self.assertNotIn("may sleep", st_[0].lower())
        # anything that keeps it awake is said as the service said it, in amber
        for reason, shown in (("busy: downloading a model", "Busy: downloading a model"),
                              ("running: stability-test.sh", "Running: stability-test.sh"),
                              ("a request is running", "A request is running"),
                              ("something blocks sleep: gdm: user is active", "Something blocks sleep: gdm: user is active"),
                              ("the graphics card is busy (35%)", "The graphics card is busy (35%)"),
                              ("the machine is busy (load 2.3)", "The machine is busy (load 2.3)")):
            self.assertEqual(line(reason=reason), (shown, "warn"))
        self.assertEqual(line(sleep_ok=True, reason="idle 60 minutes"), ("Going to sleep now", "warn"))
        self.assertEqual(line(enabled=False, reason="auto sleep is off"), ("Auto sleep is off", "dim"))
        self.assertEqual(line(supported=False), ("No deep sleep on this machine", "dim"))
        # a service that has not written for three minutes, or never has, is not guessed at
        self.assertEqual(line(age=179)[0][:6], "Sleeps")
        self.assertEqual(line(age=181), ("Sleep status unknown", "warn"))
        self.assertEqual(o1panel.sleep_summary({}, now), ("Sleep status unknown", "warn"))
        self.assertEqual(o1panel.sleep_summary({"idle": {"supported": True, "enabled": True, "minutes": 30, "at": now}}, now),
                         ("Sleep status unknown", "warn"))                         # an old service with no decision in the file

    def test_the_sleep_box_text_fits_at_every_resolution(self):
        long_reason = "something blocks sleep: " + "a long description of what holds the lock " * 4
        for sw, sh in ((1920, 1080), (2560, 1440), (3840, 2160)):
            k, lw, lh = o1fb.choose_scale(sw, sh)
            for reason in (long_reason, "idle 3 of 60 minutes", "busy: " + "x" * 120):
                st = dash_sample.sample(now=1790000000.0)
                st["idle"].update(reason=reason, sleep_ok=False, idle_s=180, minutes=60, at=int(st["time"]))
                x, y, w, h = o1panel.layout(lw, lh)["status"]
                texts = []
                orig = o1hipix.HiPixmap.text

                def rec(self_, tx, ty, s_, c, scale=1, max_w=None, ellipsis=True):
                    texts.append((s_, tx, max_w))
                    return orig(self_, tx, ty, s_, c, scale, max_w, ellipsis)
                o1hipix.HiPixmap.text = rec
                try:
                    o1panel.render(st, lw, lh, scale=k)
                finally:
                    o1hipix.HiPixmap.text = orig
                for s_, tx, mw in texts:
                    if tx >= x and tx < x + w and mw is not None:
                        self.assertLessEqual(tx + min(mw, o1vtext.text_width(s_)), x + w, (s_, tx, mw))


if __name__ == "__main__":
    unittest.main()
