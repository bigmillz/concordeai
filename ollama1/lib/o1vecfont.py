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
from array import array

import o1raster

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

# ---- lower case and the signs (6b383) ------------------------------------------------------------
# A lower-case letter has its body between y = 38 and y = 92 (skeleton; the ink reaches 30 and 100),
# ascenders reach the capital's top, descenders go down to about 125.

def _dot(x, y):
    return [(x, y), (x, y + 1)]


_X, _XT, _XB = 28, 38, 92                         # the lower-case bowl: centre x, top, bottom
_BOWL = _arc(28, 65, 18, 27, 0, 360, 28)

STROKES.update({
    "a": [_BOWL, [(46, 38), (46, 92)]],
    "b": [[(10, 8), (10, 92)], _BOWL],
    "c": [_arc(30, 65, 19, 27, 320, 40, 22)],
    "d": [[(46, 8), (46, 92)], _BOWL],
    "e": [[(10, 65), (46, 65)] + _arc(28, 65, 18, 27, 360, 40, 22)[1:]],
    "f": [[(22, 92), (22, 24)] + _arc(33, 24, 11, 16, 180, 300, 8)[1:], [(8, 40), (36, 40)]],
    "g": [_BOWL, [(46, 38), (46, 104)] + _arc(28, 104, 18, 21, 0, 150, 10)[1:]],
    "h": [[(10, 8), (10, 92)], _arc(28, 62, 18, 24, 180, 360, 14) + [(46, 92)]],
    "i": [[(14, 38), (14, 92)], _dot(14, 10)],
    "j": [[(24, 38), (24, 106)] + _arc(10, 106, 14, 18, 0, 150, 8)[1:], _dot(24, 10)],
    "k": [[(10, 8), (10, 92)], [(44, 38), (10, 70)], [(22, 60), (46, 92)]],
    "l": [[(14, 8), (14, 92)]],
    "m": [[(10, 38), (10, 92)], _arc(24, 58, 14, 20, 180, 360, 10) + [(38, 92)], _arc(52, 58, 14, 20, 180, 360, 10) + [(66, 92)]],
    "n": [[(10, 38), (10, 92)], _arc(28, 60, 18, 22, 180, 360, 14) + [(46, 92)]],
    "o": [_arc(28, 65, 19, 27, 0, 360, 28)],
    "p": [[(10, 38), (10, 124)], _BOWL],
    "q": [[(46, 38), (46, 124)], _BOWL],
    "r": [[(10, 38), (10, 92)], _arc(30, 60, 20, 22, 180, 300, 10)],
    "s": [_arc(28, 52, 16, 14, 330, 90, 14) + _arc(28, 79, 17, 13, 270, 510, 16)[1:]],
    "t": [[(20, 14), (20, 80)] + _arc(32, 80, 12, 12, 180, 90, 6)[1:] + [(40, 92)], [(8, 40), (36, 40)]],
    "u": [[(10, 38)] + _arc(28, 70, 18, 22, 180, 0, 14), [(46, 38), (46, 92)]],
    "v": [[(8, 38), (28, 92), (48, 38)]],
    "w": [[(6, 38), (18, 92), (34, 50), (50, 92), (62, 38)]],
    "x": [[(10, 38), (46, 92)], [(46, 38), (10, 92)]],
    "y": [[(8, 38), (28, 92)], [(48, 38), (28, 92), (16, 122)]],
    "z": [[(10, 38), (46, 38), (10, 92), (46, 92)]],
    "!": [[(10, 8), (10, 66)], _dot(10, 90)],
    '"': [[(8, 8), (8, 28)], [(26, 8), (26, 28)]],
    "#": [[(20, 8), (14, 92)], [(42, 8), (36, 92)], [(6, 34), (50, 34)], [(4, 66), (48, 66)]],
    "%": [_arc(16, 24, 11, 13, 0, 360, 16), _arc(44, 76, 11, 13, 0, 360, 16), [(48, 8), (12, 92)]],
    "&": [_arc(28, 27, 13, 19, 0, 360, 20), _arc(26, 70, 19, 22, 270, 90, 14), [(24, 46), (50, 92)]],
    "'": [[(8, 8), (8, 28)]],
    "(": [_arc(40, 50, 26, 50, 235, 125, 12)],
    ")": [_arc(8, 50, 26, 50, 305, 415, 12)],
    "*": [[(24, 20), (24, 56)], [(8, 29), (40, 47)], [(8, 47), (40, 29)]],
    "+": [[(8, 52), (46, 52)], [(27, 32), (27, 72)]],
    "<": [[(42, 28), (10, 52), (42, 76)]],
    "=": [[(8, 40), (46, 40)], [(8, 64), (46, 64)]],
    ">": [[(10, 28), (42, 52), (10, 76)]],
    "?": [_arc(28, 30, 19, 22, 200, 450, 14) + [(28, 66)], _dot(28, 90)],
    ";": [_dot(8, 52), [(9, 78), (9, 84), (5, 96)]],
    "@": [_arc(30, 50, 26, 42, 50, 400, 28), _arc(30, 52, 10, 14, 0, 360, 14), [(40, 38), (40, 68)]],
    "[": [[(30, 8), (12, 8), (12, 92), (30, 92)]],
    "\\": [[(8, 8), (40, 92)]],
    "]": [[(8, 8), (26, 8), (26, 92), (8, 92)]],
    "^": [[(8, 40), (26, 8), (44, 40)]],
    "_": [[(4, 108), (52, 108)]],
    "`": [[(8, 8), (20, 24)]],
    "{": [[(34, 8), (24, 14), (24, 40), (14, 50), (24, 60), (24, 86), (34, 92)]],
    "|": [[(10, 0), (10, 112)]],
    "}": [[(10, 8), (20, 14), (20, 40), (30, 50), (20, 60), (20, 86), (10, 92)]],
    "~": [_arc(18, 56, 12, 10, 180, 360, 8) + _arc(42, 56, 12, 10, 180, 0, 8)],
    "°": [_arc(14, 18, 10, 10, 0, 360, 16)],
    "·": [_dot(8, 58)],
})
STROKES["."] = [[(8, 86), (8, 92)]]


def supported(ch):
    return ch in STROKES or ch == " "


def clean(text, mixed=False):
    """The text as this font can draw it. The cost screen draws capitals only
    (mixed=False); the panel keeps its case (mixed=True). A character it has no
    glyph for becomes "?" (mixed) or a gap (capitals only); control characters go."""
    out = []
    for ch in str(text) if mixed else str(text).upper():
        for c in _FOLD.get(ch, ch):
            if supported(c):
                out.append(c)
            elif c.isprintable():
                out.append("?" if mixed else " ")
    return "".join(out)


_FOLD = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "•": "·",
         "…": "..."}


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


def text_units(text, mixed=False):
    """The advance of the whole text in glyph units, without the trailing gap."""
    t = clean(text, mixed)
    return sum(advance_units(c) for c in t) - GAP if t else 0


def text_width(text, height, mixed=False):
    """The ink width in pixels of the text at a capital height of `height` pixels."""
    u = text_units(text, mixed)
    return int(math.ceil(u * height / 100.0)) if u else 0


def vertical_extent(text, mixed=False):
    """(top, bottom) of the ink in glyph units, the capital's top being 0 and
    its baseline 100 (a $, a comma or a descender reaches beyond them)."""
    text = clean(text, mixed).replace(" ", "")
    if not text:
        return -PEN, 100 + PEN
    return min(_bbox(c)[1] for c in text), max(_bbox(c)[3] for c in text)


def fit_height(text, max_w, max_h, top=None, mixed=False):
    """The largest capital height (pixels) at which all the ink of the text is
    at most max_w wide and max_h tall; at least 4."""
    units = max(1, text_units(text, mixed))
    y0, y1 = vertical_extent(text, mixed)
    h = min(max_h * 100.0 / (y1 - y0), max_w * 100.0 / units)
    if top:
        h = min(h, top)
    return max(4, int(h))


def ink_top(text, height, box_top, box_h, mixed=False):
    """The y at which to draw the text (the capitals' top) so that its ink is
    centred in the box."""
    y0, y1 = vertical_extent(text, mixed)
    return int(round(box_top + (box_h - (y1 - y0) * height / 100.0) / 2.0 - y0 * height / 100.0))


def fit(text, max_w, height, mixed=True, ellipsis="..."):
    """The text cut with "..." so that it is at most max_w pixels wide at this
    capital height; "" when not even the dots fit."""
    t = clean(text, mixed)
    if text_width(t, height, mixed) <= max_w:
        return t
    if text_width(ellipsis, height, mixed) > max_w:
        return ""
    while t and text_width(t.rstrip() + ellipsis, height, mixed) > max_w:
        t = t[:-1]
    return t.rstrip() + ellipsis


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


def rasterize(caps, r, w, h, ss=SS):
    """Coverage bytes, h rows of w, for the union of capsules (each a thick
    line of radius r with round ends) drawn on a w x h pixel grid."""
    rows = [bytes(w) for _ in range(h)]
    for y, x0, cov in o1raster.stroke_rows(caps, r, ss=ss, y_range=(0, h)):
        lead = max(0, -x0)
        cov = cov[lead:]
        x0 += lead
        if x0 >= w:
            continue
        cov = cov[:w - x0]
        rows[y] = bytes(x0) + cov + bytes(w - x0 - len(cov))
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
    if len(_CACHE) > 1500:
        _CACHE.clear()
    scale = height / 100.0
    r = PEN * scale
    x0, y0, x1, y1 = _bbox(ch)
    cell_x0 = (x0 + x1) / 2.0 - DIGIT_W / 2.0 if ch.isdigit() else x0      # where the pen is, in glyph units
    ox, oy = -x0 * scale, -y0 * scale
    w, h = int(math.ceil((x1 - x0) * scale)) + 1, int(math.ceil((y1 - y0) * scale)) + 1
    rows = rasterize(_capsules(STROKES[ch], scale, ox, oy), r, w, h, ss=SS if height >= 30 else 8)
    got = (rows, w, h, int(round((x0 - cell_x0) * scale)), int(math.floor(y0 * scale)))
    _CACHE[key] = got
    return got


def layout(text, height, mixed=False):
    """[(char, x offset in pixels)] for the text, pen starting at 0."""
    out, pen = [], 0.0
    for ch in clean(text, mixed):
        if ch != " ":
            out.append((ch, int(round(pen))))
        pen += advance_units(ch) * height / 100.0
    return out


_PX = {}


def glyph_pixels(ch, height, fg, bg):
    """The glyph ready to paint on a flat background: ([(row, x lead, array of
    pixels)], x offset, y offset). Cached by (character, height, colours)."""
    key = (ch, int(height), fg, bg)
    got = _PX.get(key)
    if got is not None:
        return got
    if len(_PX) > 6000:
        _PX.clear()
    rows, w, h, ox, oy = glyph(ch, height)
    lut = _lut(bg, fg)
    out = []
    for j, row in enumerate(rows):
        lead = len(row) - len(row.lstrip(b"\0"))
        if lead == len(row):
            continue
        seg = row[lead:len(row.rstrip(b"\0"))]
        out.append((j, lead, array("I", map(lut.__getitem__, seg))))
    got = (out, ox, oy)
    _PX[key] = got
    return got


def draw(pm, x, y, text, height, colour, bg, mixed=False):
    """Draw the text on a flat background of colour `bg` (the pixel buffer's
    own pixels: pm.buf, pm.pw wide, clipped to pm.pclip): the top of the
    capitals at y, the pen starting at x. Returns the ink width."""
    buf = pm.buf
    W = pm.pw if hasattr(pm, "pw") else pm.w
    x0, y0, x1, y1 = pm.pclip if hasattr(pm, "pclip") else pm.clip
    for ch, dx in layout(text, height, mixed):
        rows, ox, oy = glyph_pixels(ch, height, colour, bg)
        gx, gy = x + dx + ox, y + oy
        for j, lead, arr in rows:
            yy = gy + j
            if yy < y0 or yy >= y1:
                continue
            xs = gx + lead
            seg = arr
            if xs < x0:
                seg, xs = seg[x0 - xs:], x0
            if xs + len(seg) > x1:
                seg = seg[:max(0, x1 - xs)]
            if len(seg):
                i = yy * W + xs
                buf[i:i + len(seg)] = seg
    return text_width(text, height, mixed)


_LUTS = {}


def _lut(bg, fg):
    key = (bg, fg)
    got = _LUTS.get(key)
    if got is None:
        import o1gfx
        if len(_LUTS) > 256:
            _LUTS.clear()
        got = _LUTS[key] = [o1gfx.mix(bg, fg, c / 255.0) for c in range(256)]
    return got
