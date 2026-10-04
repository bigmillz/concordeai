"""Text measurement for the panel's layout, in LOGICAL units (6b383).

The panel is laid out on a small grid (about 640x360) and drawn at the real
resolution; the text in it is the smooth stroke font (lib/o1vecfont.py). The
layout code asks "how wide is this in grid units" in many places; this module
answers for text whose capitals are CAP units tall times a whole `scale`
(1 = the small text, 2 = the big figures), rounding up so that a line that
fits here also fits when drawn. Same function names as the old bitmap font
(o1pixfont) so the layout reads the same.
"""
import math

import o1vecfont as V

CAP = 7.0
GAP = 1


def clean(text):
    return V.clean(text, True)


def _units(text):
    return V.text_units(text, True)


def text_width(text, scale=1):
    """The width of the text, in grid units (rounded up)."""
    u = _units(text)
    return int(math.ceil(u * CAP * scale / 100.0 - 1e-9)) if u else 0


def advance(ch, scale=1):
    """How far the pen moves after ch, in grid units (rounded up)."""
    return int(math.ceil(V.advance_units(ch) * CAP * scale / 100.0))


def fit(text, max_w, scale=1, ellipsis="..."):
    """The text cut with "..." so that text_width() <= max_w; "" when not even
    the dots fit."""
    text = clean(text)
    if text_width(text, scale) <= max_w:
        return text
    if text_width(ellipsis, scale) > max_w:
        return ""
    cut = text
    while cut and text_width(cut.rstrip() + ellipsis, scale) > max_w:
        cut = cut[:-1]
    return cut.rstrip() + ellipsis
