"""Anti-aliased strokes for the smooth panel (6b383): the coverage of thick
lines with round caps and round joins, as sparse pixel rows.

A stroke is a list of capsules (a segment thickened by a pen of radius r, ends
round). stroke_rows() returns, for every pixel row that has ink, where the
ink starts and a byte of coverage (0..255) per pixel. For each of `ss` sub-rows
the x-extent of every capsule is found exactly, the extents are merged, and
their fractional ends are added to the row: a few operations per capsule per
sub-row, never a test of every pixel, so long thin strokes (a graph line, a
ring) cost in proportion to their area. Plain Python, standard library only.
"""
import math
from itertools import accumulate


def prep(cap, r):
    x0, y0, x1, y1 = cap
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    poly = None
    if length > 1e-9:
        nx, ny = -dy / length * r, dx / length * r
        c = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
        poly = [(c[i], c[(i + 1) % 4]) for i in range(4) if abs(c[i][1] - c[(i + 1) % 4][1]) > 1e-9]
    return (min(y0, y1) - r, max(y0, y1) + r, x0, y0, x1, y1, poly)


def span(p, y, r2):
    """The x-extent of one prepared capsule on the line at height y, or None."""
    ymin, ymax, x0, y0, x1, y1, poly = p
    if y < ymin or y > ymax:
        return None
    lo, hi = 1e18, -1e18
    d = y - y0
    if d * d <= r2:
        w = math.sqrt(r2 - d * d)
        lo, hi = x0 - w, x0 + w
    d = y - y1
    if d * d <= r2:
        w = math.sqrt(r2 - d * d)
        if x1 - w < lo:
            lo = x1 - w
        if x1 + w > hi:
            hi = x1 + w
    if poly:
        for (ax, ay), (bx, by) in poly:
            if (ay <= y < by) or (by <= y < ay):
                x = ax + (y - ay) * (bx - ax) / (by - ay)
                if x < lo:
                    lo = x
                if x > hi:
                    hi = x
    return (lo, hi) if hi >= lo else None


def stroke_rows(caps, r, ss=4, y_range=None):
    """[(y, x0, coverage bytes)] for the union of the capsules (pixel
    coordinates, floats), one entry per pixel row that has ink. y_range
    (lo, hi) skips rows outside it."""
    prepared = sorted((prep(c, r) for c in caps), key=lambda p: p[0])
    if not prepared:
        return []
    r2 = r * r
    k = 255.0 / ss
    y_lo = int(math.floor(prepared[0][0]))
    y_hi = int(math.ceil(max(p[1] for p in prepared)))
    if y_range:
        y_lo, y_hi = max(y_lo, y_range[0]), min(y_hi, y_range[1] - 1)
    out = []
    nxt, n, active = 0, len(prepared), []
    for py in range(y_lo, y_hi + 1):
        ivs = []
        for sy in range(ss):
            y = py + (sy + 0.5) / ss
            while nxt < n and prepared[nxt][0] <= y:
                active.append(prepared[nxt])
                nxt += 1
            if active:
                active = [p for p in active if p[1] >= y]
            spans = []
            for p in active:
                s = span(p, y, r2)
                if s:
                    spans.append(s)
            if not spans:
                continue
            spans.sort()
            a, b = spans[0]
            for lft, rgt in spans[1:]:
                if lft <= b:
                    if rgt > b:
                        b = rgt
                else:
                    ivs.append((a, b))
                    a, b = lft, rgt
            ivs.append((a, b))
        if not ivs:
            continue
        xlo = int(math.floor(min(a for a, _ in ivs)))
        xhi = int(math.floor(max(b for _, b in ivs))) + 1
        width = xhi - xlo + 1
        part = [0.0] * (width + 1)
        dif = [0] * (width + 2)
        for a, b in ivs:
            a -= xlo
            b -= xlo
            ia, ib = int(a), int(b)
            if ia == ib:
                part[ia] += b - a
            else:
                part[ia] += ia + 1 - a
                part[ib] += b - ib
                if ib - ia > 1:
                    dif[ia + 1] += 1
                    dif[ib] -= 1
        cov = bytes([min(255, int((p + run) * k + 0.5)) for p, run in zip(part, accumulate(dif))][:width])
        out.append((py, xlo, cov))
    return out


def polyline_caps(points):
    """Capsule end points for a polyline (a lone point is a zero-length one)."""
    if len(points) == 1:
        x, y = points[0]
        return [(x, y, x, y)]
    return [(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])]


def arc_points(cx, cy, radius, a0, a1, step=2.0):
    """Points along a circular arc from angle a0 to a1 degrees (0 = 12 o'clock,
    clockwise on screen), at most `step` degrees apart."""
    n = max(2, int(math.ceil(abs(a1 - a0) / step)))
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / float(n))
        pts.append((cx + radius * math.sin(a), cy - radius * math.cos(a)))
    return pts


_CORNERS = {}


def corner(R):
    """Coverage of the top-left quarter of a rounded corner of radius R
    pixels: R rows of R bytes (255 inside the disc's quarter, 0 outside,
    in-between along the curve), the disc's centre at (R, R)."""
    got = _CORNERS.get(R)
    if got is not None:
        return got
    ss = 4
    rows = []
    for j in range(R):
        row = bytearray()
        for i in range(R):
            inside = 0
            for sy in range(ss):
                dy = R - (j + (sy + 0.5) / ss)
                for sx in range(ss):
                    dx = R - (i + (sx + 0.5) / ss)
                    if dx * dx + dy * dy <= R * R:
                        inside += 1
            row.append(int(inside * 255.0 / (ss * ss) + 0.5))
        rows.append(bytes(row))
    if len(_CORNERS) > 200:
        _CORNERS.clear()
    _CORNERS[R] = rows
    return rows


_DISCS = {}


def disc_rows(R):
    """Dense coverage rows of a disc of radius R (pixels) centred on the middle
    of a (2R+1)-pixel square: [bytes] of length 2R+1."""
    got = _DISCS.get(R)
    if got is not None:
        return got
    size = 2 * R + 1
    caps = [(float(R) + 0.5, float(R) + 0.5, float(R) + 0.5, float(R) + 0.5)]
    rows = [bytes(size) for _ in range(size)]
    for y, x0, cov in stroke_rows(caps, R + 0.0001, ss=4):
        if 0 <= y < size:
            line = bytearray(size)
            for i, v in enumerate(cov):
                if 0 <= x0 + i < size:
                    line[x0 + i] = v
            rows[y] = bytes(line)
    if len(_DISCS) > 100:
        _DISCS.clear()
    _DISCS[R] = rows
    return rows
