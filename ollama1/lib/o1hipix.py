"""The panel's drawing surface at the screen's real resolution (6b383).

HiPixmap is a Pixmap you draw on in LOGICAL units (the 640x360-ish grid the
panel is laid out on) that is stored and anti-aliased at `scale` times that
size: with a scale of 6 a 3840x2160 screen is drawn at 3840x2160, with nothing
scaled up afterwards. Every shape has smooth edges: rounded rectangles and
discs by coverage, arcs and polylines as thick strokes with round caps and
joins (lib/o1raster.py), text in the rounded stroke font (lib/o1vecfont.py).
Rectangles and lines that are straight and pixel-aligned in logical units stay
exact. At scale 1 it is an ordinary (if smoother) Pixmap, which is what the
tests use.

Cheap by construction: the work is in the edges. A filled rectangle is one
slice assignment per row; glyphs are cached as ready-made pixel rows; round
corners, discs and ring arcs are cached by size; only a stroke whose shape
changes (a graph line) is rasterized each time.
"""
import math
from array import array

import o1gfx
import o1raster
import o1vecfont as V

CAP = 7.0             # the panel's capital height, in logical units, at text scale 1


class HiPixmap(o1gfx.Pixmap):
    hires = True

    def __init__(self, w, h, bg=0, scale=1):
        S = max(1, int(scale))
        self.S = S
        self.pw, self.ph = int(w) * S, int(h) * S              # pixels
        super().__init__(self.pw, self.ph, bg)
        self.w, self.h = int(w), int(h)                        # logical: what the panel's layout sees
        self.clip = (0, 0, self.w, self.h)

    # ---- the clip, in logical units (pclip is the same in pixels) -----------------------
    @property
    def clip(self):
        return self._lclip

    @clip.setter
    def clip(self, v):
        self._lclip = tuple(v)
        S = self.S
        self.pclip = (max(0, int(round(v[0] * S))), max(0, int(round(v[1] * S))),
                      min(self.pw, int(round(v[2] * S))), min(self.ph, int(round(v[3] * S))))

    # ---- pixels ----------------------------------------------------------------------------
    def get(self, x, y):
        S = self.S
        xp, yp = int(x * S + S // 2), int(y * S + S // 2)
        return self.buf[yp * self.pw + xp] if 0 <= xp < self.pw and 0 <= yp < self.ph else 0

    def _bg(self, xp, yp):
        xp = min(max(xp, 0), self.pw - 1)
        yp = min(max(yp, 0), self.ph - 1)
        return self.buf[yp * self.pw + xp]

    def _fill_px(self, xa, ya, xb, yb, c):
        x0, y0, x1, y1 = self.pclip
        xa, xb = max(xa, x0), min(xb, x1)
        if xb <= xa:
            return
        row = array("I", [c]) * (xb - xa)
        W = self.pw
        buf = self.buf
        for yy in range(max(ya, y0), min(yb, y1)):
            i = yy * W + xa
            buf[i:i + (xb - xa)] = row

    def fill_rect(self, x, y, w, h, c):
        S = self.S
        self._fill_px(int(round(x * S)), int(round(y * S)), int(round((x + w) * S)), int(round((y + h) * S)), c)

    def px(self, x, y, c):
        self.fill_rect(x, y, 1, 1, c)

    def hline(self, x, y, w, c):
        self.fill_rect(x, y, w, 1, c)

    def vline(self, x, y, h, c):
        self.fill_rect(x, y, 1, h, c)

    def hairline(self, x, y, w, c, thick=0.5):
        """A horizontal line `thick` logical units thick (at least one pixel)."""
        S = self.S
        t = max(1, int(round(thick * S)))
        yp = int(round(y * S)) + (S - t) // 2
        self._fill_px(int(round(x * S)), yp, int(round((x + w) * S)), yp + t, c)

    def rect(self, x, y, w, h, c):
        if w < 1 or h < 1:
            return
        self.hline(x, y, w, c)
        self.hline(x, y + h - 1, w, c)
        self.vline(x, y, h, c)
        self.vline(x + w - 1, y, h, c)

    # ---- painting coverage over what is there ---------------------------------------------------
    def _paint_row(self, y, x0, cov, c):
        """Mix colour c into row y from pixel x0 by the coverage bytes, clipped."""
        cx0, cy0, cx1, cy1 = self.pclip
        if y < cy0 or y >= cy1:
            return
        if x0 < cx0:
            cov, x0 = cov[cx0 - x0:], cx0
        if x0 + len(cov) > cx1:
            cov = cov[:max(0, cx1 - x0)]
        n = len(cov)
        if n <= 0:
            return
        i = y * self.pw + x0
        buf = self.buf
        if cov.count(255) == n:
            buf[i:i + n] = array("I", [c]) * n
            return
        cr, cg, cb = (c >> 16) & 255, (c >> 8) & 255, c & 255
        out = buf[i:i + n]
        for j, a in enumerate(cov):
            if a == 0:
                continue
            if a == 255:
                out[j] = c
                continue
            e = out[j]
            inv = 255 - a
            out[j] = ((((e >> 16) & 255) * inv + cr * a + 127) // 255 << 16) | \
                     ((((e >> 8) & 255) * inv + cg * a + 127) // 255 << 8) | ((e & 255) * inv + cb * a + 127) // 255
        buf[i:i + n] = out

    def _paint_rows(self, rows, ox, oy, c):
        for y, x0, cov in rows:
            self._paint_row(y + oy, x0 + ox, cov, c)

    # ---- rounded shapes -----------------------------------------------------------------------
    def rrect(self, x, y, w, h, r, c):
        """A filled rectangle with rounded corners of radius r (logical units), anti-aliased."""
        S = self.S
        X, Y = int(round(x * S)), int(round(y * S))
        W, H = int(round((x + w) * S)) - X, int(round((y + h) * S)) - Y
        R = int(round(r * S))
        R = max(0, min(R, W // 2, H // 2))
        if W <= 0 or H <= 0:
            return
        if R < 1:
            self._fill_px(X, Y, X + W, Y + H, c)
            return
        self._fill_px(X, Y + R, X + W, Y + H - R, c)                 # the straight middle
        quarter = o1raster.corner(R)
        for j in range(R):
            row = quarter[j]
            solid = row.count(255) == R
            for yy in (Y + j, Y + H - 1 - j):
                if solid:                                              # a row clear of the curve
                    self._fill_px(X, yy, X + W, yy + 1, c)
                else:
                    self._paint_row(yy, X, row, c)
                    self._paint_row(yy, X + W - R, row[::-1], c)
                    self._fill_px(X + R, yy, X + W - R, yy + 1, c)

    def disc(self, cx, cy, r, c):
        S = self.S
        R = max(1, int(round(r * S)))
        rows = o1raster.disc_rows(R)
        X, Y = int(round(cx * S)) - R, int(round(cy * S)) - R
        for j, cov in enumerate(rows):
            if any(cov):
                self._paint_row(Y + j, X, cov, c)

    def _stroke(self, pts, radius_px, c, ss=4):
        """Paint a polyline (pixel coordinates) with a round pen of this radius."""
        if not pts:
            return
        x0, y0, x1, y1 = self.pclip
        rows = o1raster.stroke_rows(o1raster.polyline_caps(pts), radius_px, ss=ss, y_range=(y0, y1))
        self._paint_rows(rows, 0, 0, c)

    def polyline(self, points, width, c):
        """A line through the points (logical units; floats are fine), `width`
        logical units thick, round caps and joins."""
        S = self.S
        self._stroke([(px * S, py * S) for px, py in points], max(0.5, width * S / 2.0), c)

    def line(self, x0, y0, x1, y1, c):
        self.polyline([(x0 + 0.5, y0 + 0.5), (x1 + 0.5, y1 + 0.5)], 1.0, c)

    _ARCS = {}

    def arc(self, cx, cy, r_out, r_in, a0, a1, c):
        """A ring segment from angle a0 to a1 (degrees, 0 = 12 o'clock,
        clockwise): a thick round-capped stroke along the middle of the ring.
        The shape is cached by size and angle, so a dial that is redrawn
        with the same value costs only the painting."""
        S = self.S
        mid = (r_out + r_in) / 2.0 * S
        pen = max(0.5, (r_out - r_in) / 2.0 * S)
        if a1 - a0 < 0.01:
            return
        key = (round(mid, 1), round(pen, 1), round(a0 * 2) / 2.0, round(a1 * 2) / 2.0)
        rows = self._ARCS.get(key)
        if rows is None:
            if len(self._ARCS) > 120:
                self._ARCS.clear()
            pts = o1raster.arc_points(0.0, 0.0, mid, key[2], key[3], step=max(0.5, min(3.0, 3.0 * 90.0 / max(mid, 1.0))))
            lo = int(math.floor(-mid - pen)) - 2
            shift = -lo
            pts = [(px + shift, py + shift) for px, py in pts]
            rows = (shift, o1raster.stroke_rows(o1raster.polyline_caps(pts), pen, ss=4))
            self._ARCS[key] = rows
        shift, rr = rows
        self._paint_rows(rr, int(round(cx * S)) - shift, int(round(cy * S)) - shift, c)

    def fill_under(self, points, base_y, c):
        """Fill from a polyline (logical points, left to right) down to base_y
        (logical) with colour c, one pixel column at a time."""
        S = self.S
        x0, y0, x1, y1 = self.pclip
        pts = [(px * S, py * S) for px, py in points]
        yb = int(round(base_y * S))
        W = self.pw
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            for xp in range(max(int(math.ceil(ax)), x0), min(int(math.ceil(bx)), x1)):
                t = (xp - ax) / (bx - ax) if bx > ax else 0.0
                yt = int(round(ay + (by - ay) * t)) + 1
                ya, yz = max(yt, y0), min(yb, y1)
                if yz > ya:
                    self.buf[ya * W + xp:(yz - 1) * W + xp + 1:W] = array("I", [c]) * (yz - ya)

    # ---- pictures worth keeping --------------------------------------------------------------------
    _SNAPS = {}
    _SNAP_PIXELS = [0]
    SNAP_BUDGET = 6000000                           # pixels kept (24 MB): a few dozen dials and graphs

    def cached(self, key, box, fn):
        """Draw `fn` into the logical box, or, if the same `key` was drawn into a box of the same
        size before, paste the picture that came out then: its pixels, background included, so the
        box must hold nothing but what fn draws. A dial that shows the same value, a graph whose
        data has not moved: no drawing at all, just copying rows. A box that reaches past the clip
        keeps only the part inside it (fn cannot paint the rest), the cut being part of the key; the
        dials' boxes reach a unit above their panel's inside, and were drawn again every time (6b444)."""
        S = self.S
        xa, ya = int(round(box[0] * S)), int(round(box[1] * S))
        xb, yb = xa + int(round(box[2] * S)), ya + int(round(box[3] * S))
        c = self.pclip
        cut = (max(0, c[0] - xa), max(0, c[1] - ya), max(0, xb - c[2]), max(0, yb - c[3]))
        xa, ya, xb, yb = xa + cut[0], ya + cut[1], xb - cut[2], yb - cut[3]
        if xb <= xa or yb <= ya:
            fn()
            return False
        k = (key, xb - xa, yb - ya, self.pw)
        if any(cut):
            k += cut
        snap = self._SNAPS.get(k)
        W = self.pw
        if snap is not None:
            self._SNAPS[k] = self._SNAPS.pop(k)                      # most recently used last
            for j, row in enumerate(snap):
                i = (ya + j) * W + xa
                self.buf[i:i + (xb - xa)] = row
            return True
        fn()
        snap = [self.buf[(ya + j) * W + xa:(ya + j) * W + xb] for j in range(yb - ya)]
        self._SNAPS[k] = snap
        HiPixmap._SNAP_PIXELS[0] += (xb - xa) * (yb - ya)
        while HiPixmap._SNAP_PIXELS[0] > self.SNAP_BUDGET and len(self._SNAPS) > 1:
            old = next(iter(self._SNAPS))
            gone = self._SNAPS.pop(old)
            HiPixmap._SNAP_PIXELS[0] -= len(gone) * len(gone[0])
        return False

    # ---- text ----------------------------------------------------------------------------------
    def _cap_px(self, scale):
        return max(4, int(round(CAP * scale * self.S)))

    def text_px_width(self, s, scale=1):
        return V.text_width(s, self._cap_px(scale), True)

    def text(self, x, y, s, c, scale=1, max_w=None, ellipsis=True):
        """Draw s with its capitals' top at y (logical units); with max_w the
        text is cut with "..." to fit. Returns the logical width drawn."""
        S = self.S
        h = self._cap_px(scale)
        if max_w is not None and ellipsis:
            s = V.fit(s, int(max_w * S), h)
        else:
            s = V.clean(s, True)
        if not s:
            return 0
        xp, yp = int(round(x * S)), int(round(y * S))
        bg = self._bg(xp + 1, yp + h // 2)
        w = V.draw(self, xp, yp, s, h, c, bg, True)
        return int(math.ceil(w / float(S)))

    def text_right(self, xr, y, s, c, scale=1, max_w=None):
        S = self.S
        h = self._cap_px(scale)
        s = V.fit(s, int(max_w * S), h) if max_w is not None else V.clean(s, True)
        w = V.text_width(s, h, True)
        return self.text((xr * S - w) / float(S), y, s, c, scale)

    def text_center(self, xc, y, s, c, scale=1, max_w=None):
        S = self.S
        h = self._cap_px(scale)
        s = V.fit(s, int(max_w * S), h) if max_w is not None else V.clean(s, True)
        w = V.text_width(s, h, True)
        return self.text((xc * S - w / 2.0) / float(S), y, s, c, scale)

    # ---- output ---------------------------------------------------------------------------------
    def png(self):
        return o1gfx.png_bytes(self.pw, self.ph, self.buf)
