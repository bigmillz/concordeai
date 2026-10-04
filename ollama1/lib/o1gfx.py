"""A tiny software renderer for the graphical panel (6b380): a pixel buffer,
rectangles, lines, arcs, bitmap text and a PNG writer. Standard library only.

Colours are 0xRRGGBB integers. The buffer is one array('I') of w*h pixels, so
a filled rectangle is one slice assignment per row and the whole frame is a
few hundred thousand integers: cheap enough to redraw every two seconds on a
machine that is busy running models.

Everything clips. Pixmap.clip is a rectangle (x0, y0, x1, y1) that no
primitive draws outside; clipped() narrows it for a block of drawing and
restores it after, so a widget handed a box cannot touch anything beyond it.
"""
import math
import struct
import zlib
from array import array
from contextlib import contextmanager

import o1pixfont

_FILLS = {}


def _fill_row(c, n):
    """An array of n pixels of colour c (cached for short runs)."""
    if n <= 64:
        key = (c, n)
        a = _FILLS.get(key)
        if a is None:
            if len(_FILLS) > 4000:
                _FILLS.clear()
            a = _FILLS[key] = array("I", [c]) * n
        return a
    return array("I", [c]) * n


def rgb(c):
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def mix(a, b, t):
    """Colour a at t=0 to colour b at t=1."""
    t = max(0.0, min(1.0, t))
    ra, ga, ba = rgb(a)
    rb, gb, bb = rgb(b)
    return (int(ra + (rb - ra) * t + 0.5) << 16) | (int(ga + (gb - ga) * t + 0.5) << 8) | int(ba + (bb - ba) * t + 0.5)


class Pixmap:
    def __init__(self, w, h, bg=0):
        self.w, self.h = max(1, int(w)), max(1, int(h))
        self.buf = array("I", [bg]) * (self.w * self.h)
        self.clip = (0, 0, self.w, self.h)

    # ---- clipping ----------------------------------------------------------
    @contextmanager
    def clipped(self, x, y, w, h):
        old = self.clip
        self.clip = (max(old[0], x), max(old[1], y), min(old[2], x + w), min(old[3], y + h))
        try:
            yield
        finally:
            self.clip = old

    # ---- pixels and rectangles --------------------------------------------
    def px(self, x, y, c):
        x0, y0, x1, y1 = self.clip
        if x0 <= x < x1 and y0 <= y < y1:
            self.buf[y * self.w + x] = c

    def get(self, x, y):
        return self.buf[y * self.w + x] if 0 <= x < self.w and 0 <= y < self.h else 0

    def blend(self, x, y, c, a):
        """Colour c over what is there, at opacity a (0..1)."""
        x0, y0, x1, y1 = self.clip
        if a <= 0 or not (x0 <= x < x1 and y0 <= y < y1):
            return
        i = y * self.w + x
        self.buf[i] = c if a >= 1 else mix(self.buf[i], c, a)

    def fill_rect(self, x, y, w, h, c):
        x0, y0, x1, y1 = self.clip
        xa, xb = max(x, x0), min(x + w, x1)
        if xb <= xa:
            return
        row = _fill_row(c, xb - xa)
        W = self.w
        for yy in range(max(y, y0), min(y + h, y1)):
            i = yy * W + xa
            self.buf[i:i + (xb - xa)] = row

    def hline(self, x, y, w, c):
        self.fill_rect(x, y, w, 1, c)

    def vline(self, x, y, h, c):
        x0, y0, x1, y1 = self.clip
        if not x0 <= x < x1:
            return
        ya, yb = max(y, y0), min(y + h, y1)
        if yb <= ya:
            return
        n = yb - ya
        self.buf[ya * self.w + x:(yb - 1) * self.w + x + 1:self.w] = array("I", [c]) * n

    def rect(self, x, y, w, h, c):
        """A one-pixel outline."""
        if w < 1 or h < 1:
            return
        self.hline(x, y, w, c)
        self.hline(x, y + h - 1, w, c)
        self.vline(x, y, h, c)
        self.vline(x + w - 1, y, h, c)

    def rrect(self, x, y, w, h, r, c):
        """A filled rectangle with rounded corners of radius r."""
        r = max(0, min(r, w // 2, h // 2))
        for i in range(h):
            d = max(r - i, i - (h - 1 - r), 0)
            inset = r - int(math.sqrt(max(0, r * r - d * d))) if r and d else 0
            self.fill_rect(x + inset, y + i, w - 2 * inset, 1, c)

    def rrect_outline(self, x, y, w, h, r, c, fill=None):
        """A one-pixel rounded outline (optionally with a fill inside)."""
        if fill is not None:
            self.rrect(x, y, w, h, r, c)
            self.rrect(x + 1, y + 1, w - 2, h - 2, max(0, r - 1), fill)
            return
        self.rrect(x, y, w, h, r, c)

    # ---- lines -------------------------------------------------------------
    def line(self, x0, y0, x1, y1, c):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        n = dx - dy + 2
        while n > 0:
            self.px(x0, y0, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
            n -= 1

    # ---- arcs --------------------------------------------------------------
    def arc(self, cx, cy, r_out, r_in, a0, a1, c):
        """A ring segment from angle a0 to a1 (degrees, 0 = 12 o'clock,
        clockwise), its edges smoothed by blending with what is there."""
        span = a1 - a0
        x0, y0, x1, y1 = self.clip
        for y in range(int(cy - r_out - 1), int(cy + r_out + 2)):
            if not y0 <= y < y1:
                continue
            for x in range(int(cx - r_out - 1), int(cx + r_out + 2)):
                if not x0 <= x < x1:
                    continue
                dx, dy = x - cx, y - cy
                d = math.sqrt(dx * dx + dy * dy)
                cover = min(1.0, r_out + 0.5 - d, d - (r_in - 0.5))
                if cover <= 0:
                    continue
                ang = math.degrees(math.atan2(dx, -dy)) % 360.0
                rel = (ang - a0) % 360.0
                if rel > span:
                    continue
                self.blend(x, y, c, min(1.0, cover))

    def disc(self, cx, cy, r, c):
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                d = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
                cover = min(1.0, r + 0.5 - d)
                if cover > 0:
                    self.blend(x, y, c, cover)

    # ---- text --------------------------------------------------------------
    def text(self, x, y, s, c, scale=1, max_w=None, ellipsis=True):
        """Draw s with its top (the capitals' top edge) at y. Cut with "..."
        so that it is never wider than max_w. Returns the ink width drawn."""
        s = o1pixfont.clean(s)
        if max_w is not None:
            s = o1pixfont.fit(s, max_w, scale) if ellipsis else s
        pen = x
        for ch in s:
            if ch != " ":
                for gx, gy, gw, gh in o1pixfont.glyph_runs(ch, scale):
                    self.fill_rect(pen + gx, y + gy, gw, gh, c)
            pen += o1pixfont.advance(ch, scale)
        return max(0, pen - x - o1pixfont.GAP * scale) if s else 0

    def text_right(self, xr, y, s, c, scale=1, max_w=None):
        s = o1pixfont.clean(s)
        if max_w is not None:
            s = o1pixfont.fit(s, max_w, scale)
        w = o1pixfont.text_width(s, scale)
        return self.text(xr - w, y, s, c, scale)

    def text_center(self, xc, y, s, c, scale=1, max_w=None):
        s = o1pixfont.clean(s)
        if max_w is not None:
            s = o1pixfont.fit(s, max_w, scale)
        w = o1pixfont.text_width(s, scale)
        return self.text(xc - w // 2, y, s, c, scale)

    def blit_coverage(self, x, y, rows, lut):
        """Paint a glyph given as rows of coverage bytes (0..255) with its
        top-left at (x, y): each pixel becomes lut[coverage] (the ink colour
        already mixed over the flat background, so the background under it
        must be that colour). Clipped; blank margins are skipped."""
        x0, y0, x1, y1 = self.clip
        W = self.w
        for j, row in enumerate(rows):
            yy = y + j
            if yy < y0 or yy >= y1:
                continue
            lead = len(row) - len(row.lstrip(b"\0"))
            if lead == len(row):
                continue
            seg = row[lead:len(row.rstrip(b"\0"))]
            xs = x + lead
            if xs < x0:
                seg, xs = seg[x0 - xs:], x0
            if xs + len(seg) > x1:
                seg = seg[:max(0, x1 - xs)]
            if seg:
                i = yy * W + xs
                self.buf[i:i + len(seg)] = array("I", map(lut.__getitem__, seg))

    # ---- output ------------------------------------------------------------
    def rows(self):
        W = self.w
        for y in range(self.h):
            yield self.buf[y * W:(y + 1) * W]

    def png(self):
        return png_bytes(self.w, self.h, self.buf, upscale=1)


def png_bytes(w, h, buf, upscale=1):
    """An RGB PNG of the buffer, each pixel `upscale` times larger."""
    k = max(1, int(upscale))
    raw = bytearray()
    cache = {}
    for y in range(h):
        row = buf[y * w:(y + 1) * w]
        line = bytearray()
        for c in row:
            b = cache.get(c)
            if b is None:
                b = cache[c] = bytes(rgb(c)) * k
            line += b
        line = b"\x00" + bytes(line)
        raw += line * k

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w * k, h * k, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
