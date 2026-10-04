"""The graphical panel's own bitmap font (6b380): crisp pixels, no
anti-aliasing, drawn at a whole-number scale.

A glyph is 5 pixels wide. Capitals and digits are 7 high (rows 0..6), the
lowercase letters have a 5-high body (rows 2..6) with ascenders up to row 0
and descenders down to row 8, so a line of text is 9 rows tall at scale 1.
Digits and the space have a fixed width (so a changing number does not make
its neighbours jump); every other glyph is as wide as its ink, plus one
pixel between glyphs.

The art is below as text, one glyph per line: "TOP:" is the first row it
occupies (default 0), the rows are separated by "/", "#" is ink. Only ASCII
is drawn; anything else becomes "?" (a degree sign is kept, and a few look-
alikes are folded). Pure Python, no I/O: the tests read the table and
measure text.
"""

CAP_H = 7          # capitals and digits
CELL_H = 9         # cap height plus the 2 rows of descenders
SPACE_W = 3
GAP = 1

_ART = r"""
0=.###./#...#/#..##/#.#.#/##..#/#...#/.###.
1=..#../.##../..#../..#../..#../..#../.###.
2=.###./#...#/....#/...#./..#../.#.../#####
3=.###./#...#/....#/..##./....#/#...#/.###.
4=...#./..##./.#.#./#..#./#####/...#./...#.
5=#####/#..../####./....#/....#/#...#/.###.
6=..##./.#.../#..../####./#...#/#...#/.###.
7=#####/....#/...#./..#../.#.../.#.../.#...
8=.###./#...#/#...#/.###./#...#/#...#/.###.
9=.###./#...#/#...#/.####/....#/...#./.##..
A=.###./#...#/#...#/#####/#...#/#...#/#...#
B=####./#...#/#...#/####./#...#/#...#/####.
C=.###./#...#/#..../#..../#..../#...#/.###.
D=####./#...#/#...#/#...#/#...#/#...#/####.
E=#####/#..../#..../####./#..../#..../#####
F=#####/#..../#..../####./#..../#..../#....
G=.###./#...#/#..../#.###/#...#/#...#/.###.
H=#...#/#...#/#...#/#####/#...#/#...#/#...#
I=.###./..#../..#../..#../..#../..#../.###.
J=..###/...#./...#./...#./...#./#..#./.##..
K=#...#/#..#./#.#../##.../#.#../#..#./#...#
L=#..../#..../#..../#..../#..../#..../#####
M=#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#
N=#...#/##..#/#.#.#/#..##/#...#/#...#/#...#
O=.###./#...#/#...#/#...#/#...#/#...#/.###.
P=####./#...#/#...#/####./#..../#..../#....
Q=.###./#...#/#...#/#...#/#.#.#/#..#./.##.#
R=####./#...#/#...#/####./#.#../#..#./#...#
S=.####/#..../#..../.###./....#/....#/####.
T=#####/..#../..#../..#../..#../..#../..#..
U=#...#/#...#/#...#/#...#/#...#/#...#/.###.
V=#...#/#...#/#...#/#...#/#...#/.#.#./..#..
W=#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#
X=#...#/#...#/.#.#./..#../.#.#./#...#/#...#
Y=#...#/#...#/.#.#./..#../..#../..#../..#..
Z=#####/....#/...#./..#../.#.../#..../#####
a=2:.###./....#/.####/#...#/.####
b=#..../#..../####./#...#/#...#/#...#/####.
c=2:.###./#..../#..../#..../.###.
d=....#/....#/.####/#...#/#...#/#...#/.####
e=2:.###./#...#/#####/#..../.###.
f=..##./.#..#/.#.../###../.#.../.#.../.#...
g=2:.####/#...#/#...#/#...#/.####/....#/.###.
h=#..../#..../#.##./##..#/#...#/#...#/#...#
i=..#../...../.##../..#../..#../..#../.###.
j=...#./...../..##./...#./...#./...#./#..#./.##..
k=#..../#..../#..#./#.#../##.../#.#../#..#.
l=.##../..#../..#../..#../..#../..#../.###.
m=2:##.#./#.#.#/#.#.#/#.#.#/#.#.#
n=2:#.##./##..#/#...#/#...#/#...#
o=2:.###./#...#/#...#/#...#/.###.
p=2:####./#...#/#...#/#...#/####./#..../#....
q=2:.####/#...#/#...#/#...#/.####/....#/....#
r=2:#.##./##..#/#..../#..../#....
s=2:.####/#..../.###./....#/####.
t=1:.#.../####./.#.../.#.../.#..#/..##.
u=2:#...#/#...#/#...#/#..##/.##.#
v=2:#...#/#...#/#...#/.#.#./..#..
w=2:#...#/#...#/#.#.#/#.#.#/.#.#.
x=2:#...#/.#.#./..#../.#.#./#...#
y=2:#...#/#...#/#...#/.####/....#/....#/.###.
z=2:#####/...#./..#../.#.../#####
!=#/#/#/#/#/./#
"=#.#/#.#/#.#
#=.#.#./.#.#./#####/.#.#./#####/.#.#./.#.#.
$=..#../.####/#.#../.###./..#.#/####./..#..
%=##..#/##..#/...#./..#../.#.../#..##/#..##
&=.##../#..#./#.#../.#.../#.#.#/#..#./.##.#
'=#/#/#
(=..#/.#./#../#../#../.#./..#
)=#../.#./..#/..#/..#/.#./#..
*=2:#.#.#/.###./#####/.###./#.#.#
+=2:..#../..#../#####/..#../..#..
,=6:.#./.#./#..
-=4:####
.=5:##/##
/=....#/....#/...#./..#../.#.../#..../#....
:=2:##/##/../##/##
;=2:##/##/../##/.#/#.
==3:#####/...../#####
<=...#/..#./.#../#.../.#../..#./...#
>=#.../.#../..#./...#/..#./.#../#...
?=.###./#...#/....#/...#./..#../...../..#..
@=.###./#...#/#.###/#.#.#/#.###/#..../.###.
[=###/#../#../#../#../#../###
\=#..../#..../.#.../..#../...#./....#/....#
]=###/..#/..#/..#/..#/..#/###
^=..#../.#.#./#...#
_=8:#####
`=#./.#
{=..##/.#../.#../#.../.#../.#../..##
|=#/#/#/#/#/#/#/#/#
}=##../..#./..#./...#/..#./..#./##..
~=3:.##.#/#.##.
°=.#./#.#/.#.
"""


def _parse():
    glyphs = {}
    for line in _ART.strip().splitlines():
        ch, spec = line[0], line[2:]
        top = 0
        if ":" in spec[:3]:
            t, _, spec = spec.partition(":")
            top = int(t)
        rows = spec.split("/")
        masks = [0] * CELL_H
        for i, r in enumerate(rows):
            bits = 0
            for x in range(5):
                bits = (bits << 1) | (1 if x < len(r) and r[x] == "#" else 0)
            masks[top + i] = bits          # bit 4 is the leftmost column
        cols = [x for x in range(5) if any(m >> (4 - x) & 1 for m in masks)]
        left, right = (cols[0], cols[-1]) if cols else (0, 0)
        if ch.isdigit():
            left, right = 0, 4            # digits keep the full cell
        glyphs[ch] = (left, right - left + 1, tuple(masks))
    return glyphs


GLYPHS = _parse()
ALIASES = {"·": ".", "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-",
           "—": "-", "•": "*", "…": "..."}


def clean(text):
    """Text as the font can draw it: control characters out, look-alikes
    folded, anything else unknown a "?"."""
    out = []
    for ch in str(text):
        for c in ALIASES.get(ch, ch):
            if c in GLYPHS or c == " ":
                out.append(c)
            elif c.isprintable():
                out.append("?")
    return "".join(out)


def advance(ch, scale=1):
    """How far the pen moves after ch."""
    if ch == " ":
        return SPACE_W * scale
    g = GLYPHS.get(ch) or GLYPHS["?"]
    return (g[1] + GAP) * scale


def text_width(text, scale=1):
    """The ink width of the text (no trailing gap)."""
    text = clean(text)
    if not text:
        return 0
    return sum(advance(c, scale) for c in text) - GAP * scale


def fit(text, max_w, scale=1, ellipsis="..."):
    """The text cut with an ellipsis so that text_width() <= max_w; "" when
    even the ellipsis does not fit."""
    text = clean(text)
    if text_width(text, scale) <= max_w:
        return text
    if text_width(ellipsis, scale) > max_w:
        return ""
    cut = text
    while cut and text_width(cut.rstrip() + ellipsis, scale) > max_w:
        cut = cut[:-1]
    return cut.rstrip() + ellipsis


_RUNS = {}


def glyph_runs(ch, scale=1):
    """[(x, y, w, h)] solid rectangles that draw one glyph at a whole
    scale (relative to the pen, the glyph's own left edge at 0): horizontal
    runs of ink in each row, merged downwards when two rows are alike, so
    the fewest fills draw it."""
    key = (ch, scale)
    got = _RUNS.get(key)
    if got is not None:
        return got
    left, _width, masks = GLYPHS.get(ch) or GLYPHS["?"]
    runs = []
    for y, m in enumerate(masks):
        x = 0
        while x < 5:
            if m >> (4 - x) & 1:
                x2 = x
                while x2 + 1 < 5 and m >> (4 - x2 - 1) & 1:
                    x2 += 1
                runs.append([x - left, y, x2 - x + 1, 1])
                x = x2 + 1
            else:
                x += 1
    merged = []
    for r in runs:
        for m_ in merged:
            if m_[0] == r[0] and m_[2] == r[2] and m_[1] + m_[3] == r[1]:
                m_[3] += 1
                break
        else:
            merged.append(r)
    got = tuple((x * scale, y * scale, w * scale, h * scale) for x, y, w, h in merged)
    _RUNS[key] = got
    return got
