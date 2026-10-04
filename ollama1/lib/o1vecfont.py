"""A smooth, rounded stroke font for very large print (6b382): the electricity
cost screen. Each glyph is a skeleton of lines and arcs drawn with a round
pen (round caps, round joins), anti-aliased by exact coverage, at the size
asked for. Nothing is a scaled bitmap, so a 400-pixel digit has the same
clean curves as a 40-pixel one.

Glyphs live on a 100-unit-high grid (y down: 0 is the top of a capital, 100
the baseline); the pen is PEN units in radius. Digits are all the same width
(so a figure that changes does not shift), letters and signs are as wide as
their ink plus a gap. Capitals only: this screen's words are capitals.

How a glyph is drawn (rasterize): every segment of the skeleton is a
capsule (a thick line with round ends); for each of SS sub-rows of every pixel
row the capsules' x-extent is found exactly, merged, and added to that pixel
row as fractional coverage. That is a few operations per capsule per sub-row,
not a test of every pixel, so a screen of big digits takes well under a
second in plain Python. Glyph bitmaps (rows of 0..255 coverage bytes) are
cached by (character, height).
"""
import math
from itertools import accumulate

PEN = 7.5            # pen radius, in glyph units (a stroke is 15 units thick on a 100-unit capital)
SS = 8               # sub-rows per pixel row
GAP = 14             # space between glyphs
SPACE = 40           # a space
DIGIT_W = 68         # ink width every digit is centred in


def _arc(cx, cy, rx, ry, a0, a1, n=14):
    """Points along an ellipse arc from angle a0 to a1 (degrees; 0 = right,
    90 = down, so angles grow clockwise on screen; a1 < a0 goes the other way)."""
    pts = []
    for i in range(n + 1):
        a = math.radians(a0 + (a1 - a0) * i / float(n))
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
    return pts


def _flip(strokes):
    """The strokes turned half a turn about the middle of a 60 x 100 digit."""
    return [[(60 - x, 100 - y) for x, y in s] for s in strokes]


_SIX = [_arc(30, 68, 22, 24, 0, 360, 28), _arc(36, 62, 28, 54, 180, 300, 14)]
_S = [_arc(30, 29, 21, 21, 330, 90, 16) + _arc(30, 71, 21, 21, 270, 510, 20)[1:]]
_O = [_arc(32, 50, 26, 42, 0, 360, 36)]
_P = [[(10, 92), (10, 8), (30, 8)] + _arc(30, 28, 20, 20, 270, 450, 12)[1:] + [(10, 48)]]
_C = [_arc(32, 50, 24, 42, 320, 40, 22)]

STROKES = {
    "0": [_arc(30, 30, 22, 22, 180, 360, 12) + _arc(30, 70, 22, 22, 0, 180, 12) + [(8, 30)]],
    "1": [[(14, 28), (34, 10), (34, 92)]],
    "2": [_arc(30, 32, 22, 24, 200, 380, 16) + [(9, 90), (52, 92)]],
    "3": [_arc(28, 30, 21, 21, 200, 450, 16), _arc(28, 71, 20, 21, 270, 520, 16)],
    "4": [[(38, 8), (8, 66), (52, 66)], [(38, 8), (38, 92)]],
    "5": [[(50, 8), (14, 8), (11, 46)] + _arc(27, 67, 23, 23, 235, 485, 18)],
    "6": _SIX,
    "7": [[(8, 8), (52, 8), (22, 92)]],
    "8": [_arc(30, 30, 19, 21, 0, 360, 24), _arc(30, 69, 22, 23, 0, 360, 28)],
    "9": _flip(_SIX),
    ".": [[(8, 86), (8, 92)]],
    ",": [[(9, 86), (9, 92), (5, 104)]],
    ":": [[(8, 28), (8, 34)], [(8, 68), (8, 74)]],
    "-": [[(6, 52), (34, 52)]],
    "/": [[(40, 8), (8, 92)]],
    "$": [_S[0], [(30, -4), (30, 104)]],
    "€": [_arc(33, 50, 26, 42, 325, 35, 22), [(5, 40), (34, 40)], [(5, 60), (34, 60)]],
    "£": [_arc(33, 30, 20, 22, 330, 180, 12) + [(13, 92), (52, 92)], [(5, 58), (30, 58)]],
    "¥": [[(8, 8), (30, 50), (52, 8)], [(30, 50), (30, 92)], [(12, 62), (48, 62)], [(12, 78), (48, 78)]],
    "A": [[(8, 92), (30, 8), (52, 92)], [(15, 66), (45, 66)]],
    "B": [[(10, 8), (10, 92)], [(10, 8), (30, 8)] + _arc(30, 27, 19, 19, 270, 450, 12)[1:] + [(10, 46)],
          [(10, 46), (32, 46)] + _arc(32, 69, 20, 23, 270, 450, 12)[1:] + [(10, 92)]],
    "C": _C,
    "D": [[(10, 8), (10, 92)], [(10, 8), (26, 8)] + _arc(26, 50, 26, 42, 270, 450, 20)[1:] + [(10, 92)]],
    "E": [[(50, 8), (10, 8), (10, 92), (50, 92)], [(10, 50), (42, 50)]],
    "F": [[(50, 8), (10, 8), (10, 92)], [(10, 50), (42, 50)]],
    "G": [_arc(32, 50, 24, 42, 320, 0, 26) + [(32, 50)]],
    "H": [[(10, 8), (10, 92)], [(50, 8), (50, 92)], [(10, 50), (50, 50)]],
    "I": [[(10, 8), (10, 92)]],
    "J": [[(46, 8), (46, 68)] + _arc(28, 68, 18, 24, 0, 160, 12)[1:]],
    "K": [[(10, 8), (10, 92)], [(48, 8), (10, 58)], [(22, 44), (50, 92)]],
    "L": [[(10, 8), (10, 92), (50, 92)]],
    "M": [[(8, 92), (8, 8), (30, 58), (52, 8), (52, 92)]],
    "N": [[(10, 92), (10, 8), (50, 92), (50, 8)]],
    "O": _O,
    "P": _P,
    "Q": [_O[0], [(34, 66), (54, 98)]],
    "R": _P + [[(28, 48), (52, 92)]],
    "S": _S,
    "T": [[(6, 8), (54, 8)], [(30, 8), (30, 92)]],
    "U": [[(10, 8)] + _arc(30, 66, 20, 26, 180, 0, 16) + [(50, 8)]],
    "V": [[(6, 8), (30, 92), (54, 8)]],
    "W": [[(4, 8), (16, 92), (30, 30), (44, 92), (56, 8)]],
    "X": [[(8, 8), (52, 92)], [(52, 8), (8, 92)]],
    "Y": [[(8, 8), (30, 50), (52, 8)], [(30, 50), (30, 92)]],
    "Z": [[(8, 8), (52, 8), (8, 92), (52, 92)]],
}


def _widen(strokes, f):
    return [[(30 + (x - 30) * f, y) for x, y in s] for s in strokes]


for _d in "0123456789":                      # rounder, roomier digits (Nunito-like proportions)
    STROKES[_d] = _widen(STROKES[_d], 1.16)


def supported(ch):
    return ch in STROKES or ch == " "


def clean(text):
    """The text as this font can draw it: capitals, anything unknown a "?"
    drawn as a hyphen-less gap (dropped)."""
    out = []
    for ch in str(text).upper():
        if supported(ch):
            out.append(ch)
        elif ch.isprintable():
            out.append(" ")
    return "".join(out)


def _bbox(ch):
    pts = [p for s in STROKES[ch] for p in s]
    return (min(x for x, _ in pts) - PEN, min(y for _, y in pts) - PEN,
            max(x for x, _ in pts) + PEN, max(y for _, y in pts) + PEN)


def advance_units(ch):
    if ch == " ":
        return SPACE
    if ch.isdigit():
        return DIGIT_W + GAP
    x0, _y0, x1, _y1 = _bbox(ch)
    return (x1 - x0) + GAP


def text_width(text, height):
    """The ink width in pixels of the text at a capital height of `height` pixels."""
    text = clean(text)
    if not text:
        return 0
    units = sum(advance_units(c) for c in text) - GAP
    return int(math.ceil(units * height / 100.0))


def vertical_extent(text):
    """(top, bottom) of the ink in glyph units, the capital's top being 0 and
    its baseline 100 (a $ or a comma reaches beyond them)."""
    text = clean(text).replace(" ", "")
    if not text:
        return -PEN, 100 + PEN
    return min(_bbox(c)[1] for c in text), max(_bbox(c)[3] for c in text)


def fit_height(text, max_w, max_h, top=None):
    """The largest capital height (pixels) at which all the ink of the text is
    at most max_w wide and max_h tall; at least 4."""
    text = clean(text)
    units = max(1, sum(advance_units(c) for c in text) - GAP)
    y0, y1 = vertical_extent(text)
    h = min(max_h * 100.0 / (y1 - y0), max_w * 100.0 / units)
    if top:
        h = min(h, top)
    return max(4, int(h))


def ink_top(text, height, box_top, box_h):
    """The y at which to draw the text (the capitals' top) so that its ink is
    centred in the box."""
    y0, y1 = vertical_extent(text)
    return int(round(box_top + (box_h - (y1 - y0) * height / 100.0) / 2.0 - y0 * height / 100.0))


# ---- rasterizing ---------------------------------------------------------------------------------

def _capsules(strokes, scale, ox, oy):
    """[(x0, y0, x1, y1)] in pixels for every segment (a lone point is a zero-length one)."""
    caps = []
    for s in strokes:
        pts = [(ox + x * scale, oy + y * scale) for x, y in s]
        if len(pts) == 1:
            caps.append((pts[0][0], pts[0][1], pts[0][0], pts[0][1]))
        for a, b in zip(pts, pts[1:]):
            caps.append((a[0], a[1], b[0], b[1]))
    return caps


def _prep(cap, r):
    x0, y0, x1, y1 = cap
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    poly = None
    if length > 1e-9:
        nx, ny = -dy / length * r, dx / length * r
        c = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
        poly = [(c[i], c[(i + 1) % 4]) for i in range(4) if abs(c[i][1] - c[(i + 1) % 4][1]) > 1e-9]
    return (min(y0, y1) - r, max(y0, y1) + r, x0, y0, x1, y1, poly)


def _span(p, y, r2):
    """The x-extent of one capsule on the line at height y, or None."""
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
        lo, hi = min(lo, x1 - w), max(hi, x1 + w)
    if poly:
        for (ax, ay), (bx, by) in poly:
            if (ay <= y < by) or (by <= y < ay):
                x = ax + (y - ay) * (bx - ax) / (by - ay)
                if x < lo:
                    lo = x
                if x > hi:
                    hi = x
    return (lo, hi) if hi >= lo else None


def rasterize(caps, r, w, h, ss=SS):
    """Coverage bytes, h rows of w, for the union of capsules (each a thick
    line of radius r with round ends) drawn on a w x h pixel grid."""
    prepared = sorted((_prep(c, r) for c in caps), key=lambda p: p[0])
    r2 = r * r
    rows = []
    k = 255.0 / ss
    for py in range(h):
        part = [0.0] * (w + 2)
        dif = [0] * (w + 3)
        any_ink = False
        for sy in range(ss):
            y = py + (sy + 0.5) / ss
            spans = []
            for p in prepared:
                if p[0] > y:
                    break
                s = _span(p, y, r2)
                if s:
                    spans.append(s)
            if not spans:
                continue
            any_ink = True
            spans.sort()
            a, b = spans[0]
            merged = []
            for l, rr in spans[1:]:
                if l <= b:
                    if rr > b:
                        b = rr
                else:
                    merged.append((a, b))
                    a, b = l, rr
            merged.append((a, b))
            for a, b in merged:
                a, b = max(0.0, a), min(float(w), b)
                if b <= a:
                    continue
                ia, ib = int(a), int(b)
                if ia == ib:
                    part[ia] += b - a
                else:
                    part[ia] += ia + 1 - a
                    part[ib] += b - ib
                    if ib - ia > 1:
                        dif[ia + 1] += 1
                        dif[ib] -= 1
        if not any_ink:
            rows.append(bytes(w))
            continue
        rows.append(bytes([min(255, int((p + run) * k + 0.5)) for p, run in zip(part, accumulate(dif))][:w]))
    return rows


_CACHE = {}


def glyph(ch, height):
    """(rows, w, h, x_offset, y_offset) for one character at a capital height
    of `height` pixels: coverage rows and where their top-left corner sits
    relative to the pen position (x) and the top of a capital (y)."""
    key = (ch, int(height))
    got = _CACHE.get(key)
    if got is not None:
        return got
    if len(_CACHE) > 400:
        _CACHE.clear()
    scale = height / 100.0
    r = PEN * scale
    x0, y0, x1, y1 = _bbox(ch)
    cell_x0 = (x0 + x1) / 2.0 - DIGIT_W / 2.0 if ch.isdigit() else x0      # where the pen is, in glyph units
    ox, oy = -x0 * scale, -y0 * scale
    w, h = int(math.ceil((x1 - x0) * scale)) + 1, int(math.ceil((y1 - y0) * scale)) + 1
    rows = rasterize(_capsules(STROKES[ch], scale, ox, oy), r, w, h)
    got = (rows, w, h, int(round((x0 - cell_x0) * scale)), int(math.floor(y0 * scale)))
    _CACHE[key] = got
    return got


def layout(text, height):
    """[(char, x offset in pixels)] for the text, pen starting at 0."""
    out, pen = [], 0.0
    for ch in clean(text):
        if ch != " ":
            out.append((ch, int(round(pen))))
        pen += advance_units(ch) * height / 100.0
    return out


def draw(pm, x, y, text, height, colour, bg):
    """Draw the text on a flat background of colour `bg`: the top of the
    capitals at y, the pen starting at x. Returns the ink width."""
    lut = _lut(bg, colour)
    for ch, dx in layout(text, height):
        rows, w, h, ox, oy = glyph(ch, height)
        pm.blit_coverage(x + dx + ox, y + oy, rows, lut)
    return text_width(text, height)


_LUTS = {}


def _lut(bg, fg):
    key = (bg, fg)
    got = _LUTS.get(key)
    if got is None:
        import o1gfx
        if len(_LUTS) > 64:
            _LUTS.clear()
        got = _LUTS[key] = [o1gfx.mix(bg, fg, c / 255.0) for c in range(256)]
    return got
