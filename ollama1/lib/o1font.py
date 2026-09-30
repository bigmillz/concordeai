"""Console font for the dashboard on tty1.

The Linux console shows only the glyphs its font has (at most 512). This
reads the fonts installed in /usr/share/consolefonts (PSF1/PSF2, gzipped
or not), checks which of the dashboard's glyphs each one has, and picks
one sized for the screen (about 200-240 columns: dense but readable on a
1080p or 1440p monitor). The dashboard then draws with braille dots, with
block characters, or in plain ASCII, whatever that font can show.

Run as root before the dashboard starts (ollama1-dash --set-font /dev/tty1,
from the unit's ExecStartPre=+). It writes /run/ollama1/dash-font.json.
"""
import glob
import gzip
import json
import os
import struct
import subprocess

from o1dashui import NEEDED

FONT_DIRS = ("/usr/share/consolefonts", "/usr/share/kbd/consolefonts")
STATE = "/run/ollama1/dash-font.json"
TARGET_COLS = 220


def _open(path):
    with open(path, "rb") as f:
        data = f.read(2 << 20)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse_psf(data):
    """(width, height, set of code points) for a PSF1 or PSF2 font; None if
    it isn't one or has no unicode table."""
    if data[:2] == b"\x36\x04":                       # PSF1
        mode, height = data[2], data[3]
        n = 512 if mode & 0x01 else 256
        if not mode & 0x06:
            return None
        pos = 4 + n * height
        cps = set()
        glyph = 0
        while pos + 1 < len(data) and glyph < n:
            (v,) = struct.unpack_from("<H", data, pos)
            pos += 2
            if v == 0xFFFF:
                glyph += 1
            elif v == 0xFFFE:
                while pos + 1 < len(data):     # a sequence: skip to the end of this glyph's entry
                    (w,) = struct.unpack_from("<H", data, pos)
                    if w == 0xFFFF:
                        break
                    pos += 2
            else:
                cps.add(v)
        return 8, height, cps
    if data[:4] == b"\x72\xb5\x4a\x86":               # PSF2
        _ver, hsize, flags, n, charsize, height, width = struct.unpack_from("<7I", data, 4)
        if not flags & 1:
            return None
        pos = hsize + n * charsize
        cps = set()
        rest = data[pos:]
        for entry in rest.split(b"\xff")[:n]:
            single = entry.split(b"\xfe", 1)[0]
            try:
                cps.update(ord(c) for c in single.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        return width, height, cps
    return None


def glyph_mode(cps):
    """The best mode a font with these code points can draw."""
    ascii_ok = all(c in cps for c in range(0x20, 0x7f))
    if not ascii_ok:
        return None
    blocks = all(ord(c) in cps for c in NEEDED["blocks"])
    if blocks and all(ord(c) in cps for c in NEEDED["braille"]):
        return "braille"
    return "blocks" if blocks else "ascii"


def screen_pixels(sys_root="/sys"):
    v = None
    try:
        with open(sys_root + "/class/graphics/fb0/virtual_size") as f:
            v = f.read().strip()
    except OSError:
        return None
    try:
        w, h = (int(x) for x in v.split(","))
        return w, h
    except ValueError:
        return None


def pick(fonts, screen):
    """fonts: [(path, width, height, mode)]; screen: (px w, px h) or None.
    Prefers braille over blocks over ascii, then the size closest to
    TARGET_COLS columns (and at least 160), then Terminus."""
    rank = {"braille": 2, "blocks": 1, "ascii": 0}
    best = None
    for path, w, h, mode in fonts:
        if mode is None:
            continue
        cols = screen[0] // w if screen else 80
        rows = screen[1] // h if screen else 25
        if screen and cols < 160 and screen[0] >= 1600:
            continue
        key = (rank[mode], -abs(cols - TARGET_COLS), "Terminus" in os.path.basename(path), -w)
        if best is None or key > best[0]:
            best = (key, {"font": path, "cell": [w, h], "cols": cols, "rows": rows, "glyphs": mode})
    return best[1] if best else None


def survey(dirs=FONT_DIRS):
    out = []
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.psf*"))):
            try:
                r = parse_psf(_open(path))
            except (OSError, ValueError, struct.error, EOFError):
                continue
            if r:
                out.append((path, r[0], r[1], glyph_mode(r[2])))
    return out


def set_font(tty, sys_root="/sys", state=STATE):
    """Pick and load a font on `tty`; record what the dashboard can draw."""
    choice = pick(survey(), screen_pixels(sys_root))
    result = {"glyphs": "ascii", "font": None}
    if choice:
        r = subprocess.run(["setfont", "-C", tty, choice["font"]], capture_output=True, text=True)
        if r.returncode == 0:
            result = choice
        else:
            result["error"] = (r.stderr or r.stdout).strip()[:200]
    os.makedirs(os.path.dirname(state), exist_ok=True)
    with open(state + ".tmp", "w") as f:
        json.dump(result, f)
    os.chmod(state + ".tmp", 0o644)
    os.replace(state + ".tmp", state)
    return result
