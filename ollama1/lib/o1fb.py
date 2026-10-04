"""Getting the graphical panel onto the screen (6b380): the Linux framebuffer
and the console's graphics mode, with nothing but the standard library.

  * query_fb(fd) asks /dev/fb0 for its size, depth, colour layout and row
    stride (the FBIOGET_VSCREENINFO / FSCREENINFO ioctls); parse_* turn the
    kernel's bytes into an FbInfo so the tests can feed fake ones.
  * choose_scale() picks a small logical size (about 640 wide, the panel's
    design size) and the whole-number factor that scales it up to the real
    screen, centred; the rest of the screen stays black.
  * Presenter converts a Pixmap (0xRRGGBB integers) into the screen's own
    pixel format (32, 24 or 16 bits, any colour layout, any stride), scales
    it, and returns only the rows that changed since the last frame as
    (byte offset, bytes) writes. It never touches the device itself.
  * FbDevice is the real device: open, query, pwrite.
  * TtyGraphics puts the console into KD_GRAPHICS while the panel runs, so
    the text console stops drawing its cursor and text over the picture, and
    puts it back (KD_TEXT, cursor on, screen cleared) on exit or a signal.
  * display_connected() / panel_available() are the auto-detection: a
    framebuffer exists AND a monitor is attached (/sys/class/drm/*/status).
"""
import glob
import os
import struct
import sys
from array import array
from collections import namedtuple

try:
    import fcntl
except ImportError:                      # not a Unix (the tests import this on a Mac: fine)
    fcntl = None

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FBIOBLANK = 0x4611
KDSETMODE = 0x4B3A
KDGETMODE = 0x4B3B
KD_TEXT, KD_GRAPHICS = 0, 1
VT_GETSTATE = 0x5603

DESIGN_W = 640            # the logical width the panel is designed for
MIN_LOGICAL_W = 480       # below this the panel says the screen is too small

FbInfo = namedtuple("FbInfo", "xres yres xvirt yvirt xoff yoff bpp red green blue transp line_length")


class FbError(Exception):
    """The framebuffer cannot be used (absent, refused, a depth we cannot draw)."""


# ---- reading the device's description -------------------------------------------------

def parse_vscreeninfo(buf):
    """(xres, yres, xvirt, yvirt, xoff, yoff, bpp, red, green, blue, transp)
    from a struct fb_var_screeninfo; each colour is (offset, length)."""
    if len(buf) < 32 + 48:
        raise FbError("short fb_var_screeninfo")
    v = struct.unpack_from("<8I", buf, 0)
    ch = struct.unpack_from("<12I", buf, 32)         # red, green, blue, transp: (offset, length, msb_right)
    return v, [(ch[i], ch[i + 1]) for i in (0, 3, 6, 9)]


def parse_fscreeninfo(buf):
    """The row stride in bytes from a struct fb_fix_screeninfo (native
    layout: id[16], smem_start (unsigned long), smem_len, type, type_aux,
    visual, xpanstep, ypanstep, ywrapstep, line_length)."""
    fmt = "@16sLIIIIHHHI"
    size = struct.calcsize(fmt)
    if len(buf) < size:
        raise FbError("short fb_fix_screeninfo")
    return struct.unpack_from(fmt, buf, 0)[-1]


def make_info(var, fix_line_length):
    v, (red, green, blue, transp) = var
    xres, yres, xvirt, yvirt, xoff, yoff, bpp, _gray = v
    line = fix_line_length or (xvirt * ((bpp + 7) // 8))
    return FbInfo(xres, yres, xvirt, yvirt, xoff, yoff, bpp, red, green, blue, transp, line)


def query_fb(fd):
    """The FbInfo of an open framebuffer."""
    if fcntl is None:
        raise FbError("no ioctl on this system")
    try:
        var = bytearray(160)
        fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var)
        fix = bytearray(80)
        fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fix)
    except OSError as e:
        raise FbError("cannot query the framebuffer: %s" % e)
    info = make_info(parse_vscreeninfo(bytes(var)), parse_fscreeninfo(bytes(fix)))
    if info.xres < 1 or info.yres < 1:
        raise FbError("the framebuffer reports no size")
    return info


# ---- size ---------------------------------------------------------------------------

def choose_scale(xres, yres, design_w=DESIGN_W):
    """(k, logical width, logical height): the whole-number scale that brings
    the screen's width nearest to design_w, and the logical size that scale
    leaves (rounded down, so k x logical never exceeds the screen)."""
    k = max(1, int(round(xres / float(design_w))))
    while k > 1 and (xres // k < MIN_LOGICAL_W or yres // k < 270):
        k -= 1
    return k, max(1, xres // k), max(1, yres // k)


# ---- pixel format ---------------------------------------------------------------------

def make_packer(info):
    """f(0xRRGGBB) -> the integer that, written little-endian in
    bytes-per-pixel bytes, is that colour in this framebuffer's layout."""
    def part(v, off_len):
        off, ln = off_len
        if ln <= 0:
            return 0
        return (v >> (8 - min(8, ln))) << off
    alpha = ((1 << info.transp[1]) - 1) << info.transp[0] if info.transp[1] > 0 else 0

    def pack(c):
        return (part((c >> 16) & 255, info.red) | part((c >> 8) & 255, info.green) |
                part(c & 255, info.blue) | alpha)
    return pack


class Presenter:
    """Turns Pixmaps into writes for one framebuffer. `lw` x `lh` is the
    logical size, `k` the whole-number scale, and the picture sits in the
    middle of the screen."""

    def __init__(self, info, lw, lh, k):
        if info.bpp not in (16, 24, 32):
            raise FbError("cannot draw at %d bits per pixel" % info.bpp)
        self.info, self.lw, self.lh, self.k = info, lw, lh, k
        self.bpp = info.bpp // 8
        self.ox = max(0, (info.xres - lw * k) // 2)
        self.oy = max(0, (info.yres - lh * k) // 2)
        self.base = info.yoff * info.line_length + info.xoff * self.bpp
        self.pack = make_packer(info)
        self.tc = {32: "I", 16: "H"}.get(info.bpp)
        self.lut = {}
        self.prev = [None] * lh

    # -- one logical row -> one screen row's bytes (the k copies share it) ---------------
    def row_bytes(self, row):
        lut = self.lut
        miss = set(row) - lut.keys()
        if miss:
            if len(lut) > 50000:
                lut.clear()
                miss = set(row)
            for c in miss:
                lut[c] = self.pack(c)
        k = self.k
        if self.tc:
            vals = array(self.tc, [lut[c] for c in row])
            if k > 1:
                out = array(self.tc, bytes(len(vals) * k * self.bpp))
                for j in range(k):
                    out[j::k] = vals
                vals = out
            return vals.tobytes() if sys.byteorder == "little" else _swapped(vals)
        pix = [lut[c].to_bytes(3, "little") for c in row]            # 24 bits per pixel
        return b"".join(p * k for p in pix)

    def _offset(self, screen_row):
        return self.base + screen_row * self.info.line_length + self.ox * self.bpp

    def frame(self, pm, force=False):
        """The writes that make the screen show `pm`: [(offset, bytes)],
        only for rows that differ from the last frame (all of them when
        force). Adjacent screen rows are joined when the stride allows."""
        if (pm.w, pm.h) != (self.lw, self.lh):
            raise ValueError("pixmap %dx%d is not the logical size %dx%d" % (pm.w, pm.h, self.lw, self.lh))
        out = []
        W = pm.w
        for y in range(self.lh):
            raw = pm.buf[y * W:(y + 1) * W].tobytes()
            if not force and self.prev[y] == raw:
                continue
            self.prev[y] = raw
            data = self.row_bytes(pm.buf[y * W:(y + 1) * W])
            for j in range(self.k):
                out.append((self._offset(self.oy + y * self.k + j), data))
        return self.merge(out)

    def merge(self, writes):
        """Join writes that touch consecutive bytes (when there are no side margins the rows are contiguous)."""
        merged = []
        for off, data in writes:
            if merged and merged[-1][0] + len(merged[-1][1]) == off:
                merged[-1] = (merged[-1][0], merged[-1][1] + data)
            else:
                merged.append((off, data))
        return merged

    def clear(self):
        """The writes that paint the whole visible screen black, and forget
        what was drawn (so the next frame is drawn in full)."""
        self.prev = [None] * self.lh
        i = self.info
        row = bytes(i.xres * self.bpp)
        return self.merge([(self.base + y * i.line_length, row) for y in range(i.yres)])


def _swapped(vals):
    v = array(vals.typecode, vals)
    v.byteswap()
    return v.tobytes()


# ---- the real device -----------------------------------------------------------------

class FbDevice:
    def __init__(self, path="/dev/fb0"):
        try:
            self.fd = os.open(path, os.O_RDWR)
        except OSError as e:
            raise FbError("cannot open %s: %s" % (path, e))
        try:
            self.info = query_fb(self.fd)
        except Exception:
            os.close(self.fd)
            raise

    def write(self, offset, data):
        view = memoryview(data)
        while len(view):
            n = os.pwrite(self.fd, view, offset)
            if n <= 0:
                raise OSError("short write to the framebuffer")
            view, offset = view[n:], offset + n

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---- the console -----------------------------------------------------------------------

class TtyGraphics:
    """While active the kernel draws nothing on this tty (KD_GRAPHICS): no
    text, no cursor, no blanking. leave() always restores KD_TEXT. Safe to
    call twice; enter() on a tty that cannot do it raises FbError."""

    def __init__(self, fd, ioctl=None):
        self.fd = fd
        self.ioctl = ioctl or (fcntl.ioctl if fcntl else None)
        self.active = False
        self.old = KD_TEXT

    def enter(self):
        if self.ioctl is None:
            raise FbError("no ioctl on this system")
        try:
            buf = bytearray(4)
            self.ioctl(self.fd, KDGETMODE, buf)
            self.old = struct.unpack("<i", bytes(buf))[0]
            self.ioctl(self.fd, KDSETMODE, KD_GRAPHICS)
        except OSError as e:
            raise FbError("cannot switch the console to graphics: %s" % e)
        self.active = True

    def leave(self):
        if not self.active:
            return
        self.active = False
        try:
            self.ioctl(self.fd, KDSETMODE, KD_TEXT)
        except OSError:
            pass
        try:                                  # the text console paints itself again; start from a clean screen
            os.write(self.fd, b"\x1b[?25h\x1b[0m\x1b[2J\x1b[H")
        except OSError:
            pass

    def vt_active(self, ioctl=None):
        """The number of the virtual terminal that is on the screen, or None."""
        try:
            buf = bytearray(6)
            (ioctl or self.ioctl)(self.fd, VT_GETSTATE, buf)
            return struct.unpack_from("<H", bytes(buf), 0)[0]
        except (OSError, TypeError):
            return None


def vt_of(path):
    """3 for /dev/tty3, None for anything else."""
    base = os.path.basename(path or "")
    return int(base[3:]) if base.startswith("tty") and base[3:].isdigit() else None


# ---- is there a screen to draw on? ---------------------------------------------------------

def display_connected(sys_root="/sys"):
    """True when any display connector reports a monitor attached."""
    for p in glob.glob(os.path.join(sys_root, "class/drm/*/status")):
        try:
            with open(p) as f:
                if f.read(32).strip() == "connected":
                    return True
        except OSError:
            continue
    return False


def panel_available(fb_path="/dev/fb0", sys_root="/sys"):
    """(ok, why): the framebuffer exists and a monitor is connected."""
    if not os.path.exists(fb_path):
        return False, "no %s" % fb_path
    if not display_connected(sys_root):
        return False, "no display connected"
    return True, "display connected"


MODES = ("auto", "text", "graphic")


def resolve_mode(flag=None, env=None, file_text=None):
    """The dashboard mode: the flag, else the environment (OLLAMA1_DASH),
    else the saved setting, else "auto". An unknown word is ignored (a typo
    must not blank the screen)."""
    for v in (flag, env, file_text):
        v = (v or "").strip().lower()
        if v in MODES:
            return v
    return "auto"
