"""The dashboard's renderer: state in, a grid of characters and styles out.

Pure Python, no curses and no terminal: ollama1-dash writes the grid with
o1dashterm, and the tests render it to text at any size. It only ever
draws counts, sizes, timings and names from the fields it asks for by
name; there is no field it could print a prompt or an answer from.

Built so that nothing can overlap (6b359):

  * The screen is split into a fixed grid of boxes by plan(). Each box is
    drawn into its own bounded Cells buffer, and Cells.put CLIPS to that
    buffer, so a widget cannot write a cell outside its box. The buffers are
    then blitted (again clipped) into the one frame.
  * Every string is cleaned (escape sequences and control characters out) and
    cut to its width with "..." by its true display width (wide characters
    are 2 cells, combining marks 0, ANSI codes none), never by len().
  * The size is whatever the caller says it is now; nothing here keeps a
    size. Too small to fit a panel: the lowest-priority panels are dropped.

Glyph modes:
  blocks   bars in full blocks, sparklines in lower eighth blocks, box lines
  ascii    bars in '#', sparklines in '_.:-=+*#', boxes in + - |
"""
import datetime
import re
import unicodedata

# ---- glyphs ------------------------------------------------------------------

EIGHTHS_V = " ▁▂▃▄▅▆▇█"      # lower blocks
ASCII_V = " _.:-=+*#"
BOX = {"h": "─", "v": "│", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘"}
BOX_ASCII = {"h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+"}
ELLIPSIS = "..."

ASCII_MAP = {"°": "", "·": ".", "•": "*", "█": "#", "─": "-", "│": "|"}
# every glyph the blocks mode may draw beyond ASCII (the font picker checks
# these, and the console never shows anything else)
NEEDED = {
    "blocks": set(EIGHTHS_V + "".join(BOX.values()) + "·°") - {" "},
}
CONSOLE_OK = NEEDED["blocks"]

# ---- widths ------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()%#][ -~]|[@-Z\\-_])")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def clean(s):
    """Text safe to put in a cell: no escape sequences, no control characters."""
    s = strip_ansi(str(s)).replace("\t", " ")
    return CTRL_RE.sub("", s)


def cw(ch):
    """Display width of one character: 0 (combining, zero-width), 1 or 2."""
    o = ord(ch)
    if o < 32 or 0x7f <= o < 0xa0:
        return 0
    if o < 0x300:
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def swidth(s):
    """Display width of a string; ANSI sequences take no room."""
    return sum(cw(c) for c in strip_ansi(str(s)))


def _fit(s, w):
    out, used = [], 0
    for ch in s:
        n = cw(ch)
        if used + n > w:
            break
        out.append(ch)
        used += n
    return "".join(out), used


def trunc(s, w):
    """s cut to at most w display cells, with '...' where it was cut."""
    s = clean(s)
    if w <= 0:
        return ""
    if swidth(s) <= w:
        return s
    if w <= len(ELLIPSIS):
        return _fit(s, w)[0]
    return _fit(s, w - len(ELLIPSIS))[0] + ELLIPSIS


def fold(text, glyphs, console):
    """Text as the glyph mode allows: ASCII only in ascii mode; on the Linux
    console (whose font has only what the picker checked) '?' for any other."""
    text = clean(text)
    if glyphs == "ascii":
        return "".join(ASCII_MAP.get(c, c) if 32 <= ord(c) < 127 or c in ASCII_MAP else "?" for c in text)
    if console:
        return "".join(c if 32 <= ord(c) < 127 or c in CONSOLE_OK else "?" for c in text)
    return text


# ---- cells -------------------------------------------------------------------

class Cells:
    """A bounded w x h buffer of cells. put() clips to it. A wide character
    takes its cell and a "" continuation cell."""

    def __init__(self, width, height, glyphs="blocks", console=False):
        self.w, self.h = max(1, int(width)), max(1, int(height))
        self.glyphs = "ascii" if glyphs == "ascii" else "blocks"
        self.console = bool(console)
        self.chars = [[" "] * self.w for _ in range(self.h)]
        self.styles = [[""] * self.w for _ in range(self.h)]

    def _set(self, x, y, ch, style):
        row = self.chars[y]
        if row[x] == "" and x > 0:                      # on a wide char's second cell: blank its first
            row[x - 1] = " "
        if x + 1 < self.w and row[x + 1] == "" and row[x] and cw(row[x]) == 2:
            row[x + 1] = " "                            # on a wide char's first cell: blank its second
        row[x] = ch
        self.styles[y][x] = style

    def put(self, x, y, text, style="", maxw=None):
        """Write text at (x, y), cut to maxw cells (with '...'), clipped to the buffer."""
        if y < 0 or y >= self.h:
            return
        text = fold(text, self.glyphs, self.console)
        if maxw is not None:
            text = trunc(text, maxw)
        for ch in text:
            n = cw(ch)
            if n == 0:
                continue
            if n == 2:
                if 0 <= x and x + 1 < self.w:
                    self._set(x, y, ch, style)
                    self._set(x + 1, y, "", style)
                else:                                   # half outside: nothing of it shows
                    for xi in (x, x + 1):
                        if 0 <= xi < self.w:
                            self._set(xi, y, " ", style)
            elif 0 <= x < self.w:
                self._set(x, y, ch, style)
            x += n

    def text(self):
        return "\n".join("".join(r) for r in self.chars)

    def fill(self, x, y, w, h, ch=" ", style=""):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= yy < self.h and 0 <= xx < self.w:
                    self._set(xx, yy, ch, style)

    def blit(self, other, x, y):
        """Copy another buffer in at (x, y), clipped to this one."""
        for yy in range(other.h):
            ty = y + yy
            if not 0 <= ty < self.h:
                continue
            for xx in range(other.w):
                tx = x + xx
                if not 0 <= tx < self.w:
                    continue
                ch, st = other.chars[yy][xx], other.styles[yy][xx]
                if ch == "":                            # a wide character's second cell: its first one placed it
                    if tx == 0:                         # ... unless that one was clipped away
                        self._set(tx, ty, " ", st)
                elif cw(ch) == 2:
                    if tx + 1 < self.w:
                        self._set(tx, ty, ch, st)
                        self._set(tx + 1, ty, "", st)
                    else:
                        self._set(tx, ty, " ", st)
                else:
                    self._set(tx, ty, ch, st)

    def box(self, title="", style="border"):
        """A frame around the whole buffer, with a title in the top edge."""
        w, h = self.w, self.h
        if w < 2 or h < 2:
            return
        b = BOX_ASCII if self.glyphs == "ascii" else BOX
        self.put(0, 0, b["tl"] + b["h"] * (w - 2) + b["tr"], style)
        for yy in range(1, h - 1):
            self.put(0, yy, b["v"], style)
            self.put(w - 1, yy, b["v"], style)
        self.put(0, h - 1, b["bl"] + b["h"] * (w - 2) + b["br"], style)
        if title and w > 8:
            self.put(2, 0, " %s " % trunc(title, w - 6), "title")

    def hbar(self, x, y, w, frac, style=""):
        """A bar w cells wide, filled to frac (0..1)."""
        if w <= 0:
            return
        f = _f(frac)
        frac = 0.0 if f is None else max(0.0, min(1.0, f))
        n = int(round(frac * w))
        full = "#" if self.glyphs == "ascii" else "█"
        rest = "." if self.glyphs == "ascii" else "·"
        self.put(x, y, full * n, style)
        self.put(x + n, y, rest * (w - n), "dim")

    def spark(self, x, y, w, values, vmax=None, style="", floor=0.0):
        """One row of a trend: `values` (oldest first) as eighth blocks."""
        if w <= 0:
            return
        vals = resample([_f(v) for v in values], w)
        seen = [v for v in vals if v is not None]
        top = vmax if _f(vmax) else max(seen + [floor, 1e-9])
        glyphs = ASCII_V if self.glyphs == "ascii" else EIGHTHS_V
        out = []
        for v in vals:
            if v is None:
                out.append(" ")
                continue
            n = int(round(max(0.0, min(1.0, v / top)) * 8))
            out.append(glyphs[max(1, n)])
        self.put(x, y, "".join(out), style)


class Canvas(Cells):
    """The whole frame."""


def resample(values, n):
    """values -> exactly n points (average of buckets; None if a bucket is
    empty). Short series are right-aligned, with None on the left."""
    vals = list(values)
    if n <= 0:
        return []
    if len(vals) <= n:
        return [None] * (n - len(vals)) + vals
    out = []
    step = len(vals) / float(n)
    for i in range(n):
        a, b = int(i * step), int((i + 1) * step)
        chunk = [v for v in vals[a:max(b, a + 1)] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


# ---- formatting ----------------------------------------------------------------

def _f(v):
    """v as a finite float, else None (None, text, NaN and infinity all mean 'no reading')."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def dig(d, *keys):
    """d[k1][k2]... or None if any step is missing or not a dict."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def gib(b):
    return "%.1f" % ((_f(b) or 0) / 2**30)


def human_bytes(b):
    b = _f(b) or 0.0
    for unit in ("B", "K", "M", "G", "T"):
        if abs(b) < 1024 or unit == "T":
            return ("%.0f%s" if unit == "B" else "%.1f%s") % (b, unit)
        b /= 1024
    return "%.1fT" % b


def rate(bps):
    return human_bytes(bps) + "/s"


def dur(s):
    s = _f(s)
    if s is None:
        return "-"
    s = int(max(0, s))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm %ds" % (m, s)
    return "%ds" % s


def ago(t, now):
    t = _f(t)
    return "never" if not t else dur((_f(now) or 0) - t) + " ago"


def pct_style(p, warn=75, bad=90):
    p = _f(p)
    if p is None:
        return "dim"
    return "bad" if p >= bad else ("warn" if p >= warn else "ok")


def num(v, fmt="%.1f"):
    v = _f(v)
    return "-" if v is None else fmt % v


def frac(a, b):
    a, b = _f(a), _f(b)
    return None if a is None or not b else a / b


def _series(st, name, range_s):
    s = (st.get("series") or {}).get(name) if isinstance(st.get("series"), dict) else None
    return list(s)[-range_s:] if isinstance(s, (list, tuple)) else []


# ---- widgets: they draw into a Cells of their own and nothing else -------------

LW = 7   # the label column of a meter row


def put_parts(c, y, parts, x=0):
    """Pieces of one line, one after another, each cut to what is left of the row."""
    for text, style in parts:
        left = c.w - x
        if left <= 0:
            return
        text = fold(text, c.glyphs, c.console)
        c.put(x, y, text, style, maxw=left)
        x += min(swidth(text), left)


def meter(c, y, label, value, f, style, lw=LW, vw=16):
    """LABEL  value  [bar]. The columns are fixed, each cut to its own width,
    and the bar takes what is left (and goes, below 6 cells)."""
    vw = max(0, min(vw, c.w - lw))
    c.put(0, y, label, "dim", maxw=lw - 1)
    c.put(lw, y, value, style, maxw=vw)
    bx = lw + vw + 1
    if c.w - bx >= 6:
        c.hbar(bx, y, c.w - bx, f, style)


def trend(c, y, label, series, range_s, vmax=None, style="", floor=0.0):
    c.put(0, y, label, "dim", maxw=LW)
    c.spark(LW, y, c.w - LW, series, vmax=vmax, style=style, floor=floor)


def w_gpu(c, st, ctx):
    g = st.get("gpu")
    if not isinstance(g, dict):
        c.put(0, 0, "no GPU found", "bad")
        return
    busy = _f(g.get("busy_pct"))
    vu, vt = _f(g.get("vram_used")), _f(g.get("vram_total"))
    pw, cap = _f(g.get("power_w")), _f(g.get("power_cap_w"))
    temps = [t for t in (_f(v) for v in (g.get("temps") or {}).values()) if t is not None] \
        if isinstance(g.get("temps"), dict) else []
    hot = (_f(dig(g, "temps", "junction")) or (max(temps) if temps else None))
    fan = _f(g.get("fan_pct"))
    meter(c, 0, "Busy", "%s%%" % num(busy, "%.0f"), frac(busy, 100), pct_style(busy, 101, 101))
    meter(c, 1, "VRAM", "%s/%s GiB" % (gib(vu), gib(vt)), frac(vu, vt), pct_style(100 * (frac(vu, vt) or 0) if vt else None))
    meter(c, 2, "Power", "%s/%s W" % (num(pw, "%.0f"), num(cap, "%.0f")), frac(pw, cap), "value")
    put_parts(c, 3, [("Temp   ", "dim"), ("%s°C" % num(hot, "%.0f"), pct_style(hot, 80, 95)),
                     ("   Fan ", "dim"), ("%s%%" % num(fan, "%.0f"), "value")])
    trend(c, 4, ctx["rng"], _series(st, "gpu_busy", ctx["range_s"]), ctx["range_s"], vmax=100, style="c_gpu")


def w_system(c, st, ctx):
    cpu = st.get("cpu") if isinstance(st.get("cpu"), dict) else {}
    m = st.get("mem") if isinstance(st.get("mem"), dict) else {}
    cg = st.get("ollama_cg") if isinstance(st.get("ollama_cg"), dict) else {}
    total_cpu = _f(cpu.get("total"))
    load = [x for x in (cpu.get("load") or []) if _f(x) is not None] if isinstance(cpu.get("load"), list) else []
    meter(c, 0, "CPU", "%s%%" % num(total_cpu, "%.0f"), frac(total_cpu, 100), pct_style(total_cpu))
    put_parts(c, 1, [("Temp   ", "dim"), ("%s°C" % num(cpu.get("temp"), "%.0f"), pct_style(cpu.get("temp"), 80, 90)),
                     ("   Load ", "dim"), (" ".join("%.2f" % x for x in load) or "-", "value")])
    total = _f(m.get("total")) or 0.0
    used = total - (_f(m.get("available")) or 0.0) if total else None
    meter(c, 2, "RAM", "%s/%s GiB" % (gib(used), gib(total)), frac(used, total), pct_style(100 * (frac(used, total) or 0) if total else None))
    cmax = _f(cg.get("max"))
    if cmax:
        meter(c, 3, "Ollama", "%s/%s GiB" % (gib(cg.get("current")), gib(cmax)), frac(cg.get("current"), cmax),
              pct_style(100 * (frac(cg.get("current"), cmax) or 0), 80, 92))
    else:
        swap_t = _f(m.get("swap_total")) or 0.0
        swap_u = swap_t - (_f(m.get("swap_free")) or 0.0)
        meter(c, 3, "Swap", "%s/%s GiB" % (gib(swap_u), gib(swap_t)), frac(swap_u, swap_t),
              "warn" if swap_u > 2**28 else "value")
    trend(c, 4, ctx["rng"], _series(st, "cpu_total", ctx["range_s"]), ctx["range_s"], vmax=100, style="c_cpu")


def w_requests(c, st, ctx):
    gw = st.get("gw") if isinstance(st.get("gw"), dict) else {}
    err = gw.get("errors") if isinstance(gw.get("errors"), dict) else {}
    queued = _f(gw.get("queued")) or 0
    put_parts(c, 0, [("Active ", "dim"), ("%d" % (_f(gw.get("active")) or 0), "value"),
                     ("   Queued ", "dim"), ("%d" % queued, "warn" if queued else "value"),
                     ("   Today ", "dim"), ("%d" % (_f(gw.get("requests_today")) or 0), "value")])
    ttft = _f(gw.get("ttft_ms"))
    put_parts(c, 1, [("Speed  ", "dim"), ("%s tok/s" % num(dig(gw, "tps", "current")), "value"),
                     ("   First token " if c.w >= 44 else "   First ", "dim"), (("%.2f s" % (ttft / 1000.0)) if ttft else "-", "value")])
    kinds = [("busy", "busy"), ("gpu_fit", "fit"), ("gpu_spill", "spill"), ("ram_pressure", "ram"),
             ("ram_oom", "oom"), ("ollama", "ollama")]
    bad = [(label, int(_f(err.get(k)) or 0)) for k, label in kinds if _f(err.get(k))]
    n = sum(v for _, v in bad)
    put_parts(c, 2, [("Errors ", "dim"), ("%d" % n, "bad" if n else "ok")] +
              ([("   " + "  ".join("%s %d" % kv for kv in bad), "dim")] if bad else []))
    trend(c, 3, ctx["rng"], _series(st, "tps", ctx["range_s"]), ctx["range_s"], style="c_tps", floor=10)


def _until(expires, now):
    if not expires:
        return "-"
    try:
        s = str(expires).replace("Z", "+00:00")
        if "." in s:  # Ollama sends nanoseconds; Python takes microseconds
            head, _, tail = s.partition(".")
            digits = "".join(ch for ch in tail if ch.isdigit())
            s = head + "." + digits[:6] + tail[len(digits):]
        t = datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, OverflowError, OSError):
        return "-"
    return dur(t - now) if t > now else "now"


def w_models(c, st, ctx):
    gw = st.get("gw") if isinstance(st.get("gw"), dict) else {}
    loaded = [m for m in (gw.get("loaded") or []) if isinstance(m, dict)] if isinstance(gw.get("loaded"), list) else []
    events = [e for e in (gw.get("events") or []) if isinstance(e, dict)] if isinstance(gw.get("events"), list) else []
    now = ctx["now"]
    last = events[-1] if events else None
    room = c.h - (1 if last else 0)
    if not loaded:
        c.put(0, 0, "No model in memory", "dim")
    shown = loaded if len(loaded) <= room else loaded[:max(0, room - 1)]
    for i, m in enumerate(shown):
        size, vram = int(_f(m.get("size")) or 0), int(_f(m.get("size_vram")) or 0)
        share = int(100 * vram / size) if size else 0
        left = _until(m.get("expires_at"), now)
        dw = 27 if c.w >= 48 else 19
        detail = ("%d%% GPU  %s GiB  %s" % (share, gib(vram), left)) if dw == 27 else ("%d%% %sG %s" % (share, gib(vram), left))
        ok = share >= 100 or m.get("ram_allowed")
        nw = max(1, c.w - dw - 1)
        c.put(0, i, m.get("name") or "?", "value", maxw=nw)
        c.put(c.w - dw, i, detail, "text" if ok else "bad", maxw=dw)
    if len(loaded) > len(shown):
        c.put(0, len(shown), "+%d more" % (len(loaded) - len(shown)), "dim")
    if last:
        kind = str(last.get("kind", ""))
        style = "bad" if kind.startswith("refused") else ("warn" if "unload" in kind else "ok")
        try:
            t = datetime.datetime.fromtimestamp(_f(last.get("t")) or 0).strftime("%H:%M")
        except (ValueError, OverflowError, OSError):
            t = "-"
        put_parts(c, c.h - 1, [("Last ", "dim"), (t + " ", "dim"), ("%s %s" % (kind, last.get("model", "")), style)])


def w_storage(c, st, ctx):
    disks = [d for d in (st.get("disks") or []) if isinstance(d, dict)] if isinstance(st.get("disks"), list) else []
    raid = [a for a in (st.get("raid") or []) if isinstance(a, dict)] if isinstance(st.get("raid"), list) else []
    io = st.get("io") if isinstance(st.get("io"), dict) else {}
    budget = c.h - (1 if raid else 0) - 1
    shown = disks if len(disks) <= budget else disks[:max(0, budget - 1)]
    y = 0
    for d in shown:
        lw = 13 if c.w >= 36 else 9
        label = str(d.get("mount") or "?")
        total, used = _f(d.get("total")), _f(d.get("used"))
        if d.get("mounted") and total:
            f = (used or 0) / total
            meter(c, y, label, "%s free" % human_bytes(d.get("free")), f, pct_style(100 * f, 80, 92), lw=lw, vw=12)
        else:
            c.put(0, y, label, "dim", maxw=lw - 1)
            c.put(lw, y, "not mounted", "bad")
        y += 1
    if len(disks) > len(shown):
        c.put(0, y, "+%d more disks" % (len(disks) - len(shown)), "dim")
        y += 1
    for a in raid[:1]:
        s = ("RAID %s %s [%s] %s" % (a.get("name"), a.get("level"), a.get("members"),
                                     "healthy" if a.get("healthy") else "DEGRADED")) if c.w >= 46 else \
            ("RAID %s %s" % (a.get("name"), "healthy" if a.get("healthy") else "DEGRADED"))
        if a.get("action"):
            s += "  " + raid_action(a, "%.0f")
        c.put(0, y, s, "ok" if a.get("healthy") else "bad")
        y += 1
    put_parts(c, y, [("Disk IO ", "dim"), ("read ", "dim"), (rate(io.get("read_bps")), "value"),
                                             ("  write ", "dim"), (rate(io.get("write_bps")), "value")])


def w_network(c, st, ctx):
    n = st.get("net") if isinstance(st.get("net"), dict) else {}
    t = st.get("tunnel") if isinstance(st.get("tunnel"), dict) else {}
    put_parts(c, 0, [("Down   ", "dim"), (rate(n.get("rx_bps")), "value"), ("   Up ", "dim"), (rate(n.get("tx_bps")), "value")])
    ports = [p for p in (n.get("ports") or []) if isinstance(p, dict)] if isinstance(n.get("ports"), list) else []
    parts = [("Link   ", "dim")]
    if not ports:
        parts.append(("no ports found", "warn"))
    speeds = {_f(p.get("speed_mbps")) for p in ports}
    if c.w < 48 and len(ports) > 1 and all(p.get("carrier") for p in ports) and len(speeds) == 1 and None not in speeds:
        parts.append(("%d ports, %d Mb/s" % (len(ports), speeds.pop()), "ok"))
        ports = []
    for p in ports:
        if p.get("carrier"):
            sp = _f(p.get("speed_mbps"))
            parts.append(("%s %s  " % (p.get("name"), ("%d Mb/s" % sp) if sp else "up"), "ok"))
        else:
            parts.append(("%s no link  " % p.get("name"), "warn"))
    put_parts(c, 1, parts)
    up = bool(t.get("up"))
    rtt = _f(t.get("rtt_ms"))
    put_parts(c, 2, [("Tunnel ", "dim"), ("connected (%d)" % (_f(t.get("connections")) or 0) if up else "DOWN", "ok" if up else "bad"),
                     ("   rtt ", "dim"), (("%.0f ms" % rtt) if rtt is not None else "-", "value")])
    trend(c, 3, ctx["rng"], _series(st, "net_rx", ctx["range_s"]), ctx["range_s"], style="c_net")


def raid_action(a, fmt="%.1f", with_left=False):
    """"check 12.1%" (and ", about 10 h left" when asked) for an array that is
    checking, resyncing, recovering or reshaping; just the action when the
    kernel gave no percentage yet (resync=DELAYED)."""
    s = str(a.get("action") or "")
    p = _f(a.get("progress"))
    if p is not None:
        s += " " + (fmt % p) + "%"
    if with_left:
        left = raid_left(a.get("finish"))
        if left:
            s += ", " + left
    return s


def raid_left(finish):
    """"615.5min" -> "about 10 h left"; None if it isn't a number of minutes."""
    try:
        m = float(str(finish).replace("min", "").strip())
    except ValueError:
        return None
    if m != m or m < 0:
        return None
    if m < 90:
        return "about %d min left" % max(1, round(m))
    h = m / 60.0
    if h < 48:
        return "about %d h left" % round(h)
    return "about %d days left" % round(h / 24.0)


def warnings(st, now=0):
    """What needs a look, worst first: [(text, 'bad'|'warn')]."""
    out = []
    gw = st.get("gw")
    if not isinstance(gw, dict) or gw.get("stale"):
        out.append(("Gateway is not running", "bad"))
    g = st.get("gpu")
    if not isinstance(g, dict):
        out.append(("No GPU found", "bad"))
    else:
        hot = _f(dig(g, "temps", "junction"))
        if hot is not None and hot >= 95:
            out.append(("GPU very hot: %.0f C" % hot, "bad"))
        elif hot is not None and hot >= 85:
            out.append(("GPU hot: %.0f C" % hot, "warn"))
    cpu_t = _f(dig(st, "cpu", "temp"))
    if cpu_t is not None and cpu_t >= 90:
        out.append(("CPU hot: %.0f C" % cpu_t, "warn"))
    m = st.get("mem") if isinstance(st.get("mem"), dict) else {}
    total, avail = _f(m.get("total")), _f(m.get("available"))
    if total and avail is not None and avail / total < 0.08:
        out.append(("Memory nearly full: %s GiB free" % gib(avail), "warn"))
    swap_u = (_f(m.get("swap_total")) or 0) - (_f(m.get("swap_free")) or 0)
    if swap_u > 2**28:
        out.append(("Swap in use: %s GiB" % gib(swap_u), "warn"))
    for d in st.get("disks") or [] if isinstance(st.get("disks"), list) else []:
        if not isinstance(d, dict):
            continue
        if not d.get("mounted"):
            out.append(("%s is not mounted" % (d.get("mount") or "?"), "bad"))
        elif _f(d.get("total")) and (_f(d.get("free")) or 0) / d["total"] < 0.05:
            out.append(("%s almost full" % d.get("mount"), "bad"))
        elif _f(d.get("total")) and (_f(d.get("free")) or 0) / d["total"] < 0.10:
            out.append(("%s low on space" % d.get("mount"), "warn"))
    for a in st.get("raid") or [] if isinstance(st.get("raid"), list) else []:
        if not isinstance(a, dict):
            continue
        if not a.get("healthy"):
            out.append(("RAID %s degraded" % a.get("name"), "bad"))
        elif a.get("action"):
            if a["action"] != "check":                      # a scheduled check of a healthy mirror is only information
                out.append(("RAID %s: %s" % (a.get("name"), raid_action(a, "%.0f")), "warn"))
    t = st.get("tunnel")
    if isinstance(t, dict) and not t.get("up"):
        out.append(("Tunnel is down", "bad"))
    up = st.get("updates") if isinstance(st.get("updates"), dict) else {}
    if dig(up, "ollama", "result") == "failed":
        out.append(("Ollama update failed", "bad"))
    if dig(up, "sleep", "resume_check") and not dig(up, "sleep", "resume_check", "ok"):
        out.append(("Problem after waking up", "bad"))
    if up.get("reboot_required"):
        out.append(("Reboot needed", "warn"))
    q = _f(dig(gw, "queued")) or 0
    if q >= 3:
        out.append(("%d requests waiting" % q, "warn"))
    out.sort(key=lambda w: 0 if w[1] == "bad" else 1)
    return out


def w_health(c, st, ctx):
    now = ctx["now"]
    warns = ctx["warns"]
    up = st.get("updates") if isinstance(st.get("updates"), dict) else {}
    info = [[("Uptime ", "dim"), (dur(st.get("uptime")), "value"), ("   Security updates ", "dim"),
             (ago(up.get("last_unattended"), now), "value"), ("   Ollama ", "dim"),
             ("%s %s" % (dig(up, "ollama", "result") or "-", dig(up, "ollama", "version") or ""), "value")]]
    pw = st.get("power") if isinstance(st.get("power"), dict) else None
    if pw:
        watts = _f(pw.get("watts"))
        line = [("Power  ", "dim"), ("%s W" % num(watts, "%.0f") if watts is not None else "no reading",
                                     "value" if watts is not None else "warn"),
                (" (plug)" if pw.get("src") == "plug" else " (estimate)" if pw.get("src") else "", "dim"),
                ("   24 h ", "dim"), ("%s kWh" % num(pw.get("kwh_24h"), "%.2f"), "value")]
        if _f(pw.get("cost_24h")) is not None:
            line.append(("  %s%.2f" % (pw.get("symbol") or "$", pw["cost_24h"]), "value"))
        if pw.get("badge"):
            line.append(("  " + str(pw["badge"]), "dim"))
        info.append(line)
    y = 0
    needed = min(len(warns), 2) or 1
    while info and c.h - len(info) < needed:        # the problems come before the figures
        info.pop()
    if not warns:
        c.put(0, y, "All clear", "ok")
        y += 1
    else:
        room = max(1, c.h - len(info))
        show = warns if len(warns) <= room else warns[:max(1, room - 1)]
        for text, style in show:
            c.put(0, y, ("! " if style == "bad" else "* ") + text, style)
            y += 1
        if len(show) < len(warns):
            c.put(0, y, "+%d more to check" % (len(warns) - len(show)), "warn")
            y += 1
    for line in info:
        if y >= c.h:
            break
        put_parts(c, y, line)
        y += 1


# ---- the layout ----------------------------------------------------------------

# name: (title, widget, content rows). The GRID is in priority order: the
# last rows go first when the screen is short.
PANELS = {
    "gpu": ("GPU", w_gpu, 5),
    "cpumem": ("CPU and memory", w_system, 5),
    "requests": ("Requests", w_requests, 4),
    "models": ("Loaded models", w_models, 4),
    "storage": ("Storage", w_storage, 5),
    "network": ("Network", w_network, 4),
    "health": ("Health", w_health, 3),
}
GRID = [("gpu", "cpumem"), ("requests", "models"), ("storage", "network"), ("health",)]
MAX_W = 120          # wider screens centre a layout this wide
TWO_COLS_AT = 76     # below this width the panels stack in one column
MIN_W = 36


def plan(w, h):
    """The boxes for a w x h screen: [(name, x, y, width, height)], each
    inside the body (rows 1..h-2) and none overlapping another."""
    body = h - 2
    if w < MIN_W or body < 7:
        return []
    cw_ = min(w, MAX_W)
    x0 = (w - cw_) // 2
    two = w >= TWO_COLS_AT
    rows = [list(r) for r in GRID] if two else [[n] for r in GRID for n in r]
    rows = [[(n, PANELS[n][2] + 2) for n in r] for r in rows]
    chosen, used = [], 0
    for r in rows:
        rh = max(p[1] for p in r)
        if used + rh > body:
            break
        chosen.append((r, rh))
        used += rh
    if not chosen:
        return []
    spare = body - used
    gap = 1 if spare >= len(chosen) - 1 else 0
    spare -= gap * (len(chosen) - 1)
    extra = min(3, spare // len(chosen))
    spare -= extra * len(chosen)
    y = 1 + spare // 2
    colgap = 1 if cw_ >= 100 else 0
    out = []
    for r, rh in chosen:
        rh += extra
        if len(r) == 2:
            lw_ = (cw_ - colgap) // 2
            out.append((r[0][0], x0, y, lw_, rh))
            out.append((r[1][0], x0 + lw_ + colgap, y, cw_ - colgap - lw_, rh))
        else:
            out.append((r[0][0], x0, y, cw_, rh))
        y += rh + gap
    return out


def draw_panel(name, pw, ph, st, ctx, glyphs, console):
    """One panel into its own buffer: the frame, then the widget in the
    inside. Whatever the widget does, it cannot leave this buffer."""
    title, fn, _rows = PANELS[name]
    box = Cells(pw, ph, glyphs, console)
    box.box(title)
    iw, ih = pw - 4, ph - 2
    if iw >= 1 and ih >= 1:
        inner = Cells(iw, ih, glyphs, console)
        try:
            fn(inner, st, ctx)
        except Exception as e:                       # one bad reading must not blank the screen
            ERRORS.append("%s: %r" % (name, e))
            inner = Cells(iw, ih, glyphs, console)
            inner.put(0, 0, "no data", "dim")
        box.blit(inner, 2, 1)
    return box


ERRORS = []   # widget exceptions (the tests assert this stays empty)


def render(st, width, height, glyphs="blocks", range_s=300, page=0, console=False):
    """The whole dashboard as a Canvas of exactly width x height cells.
    (`page` is accepted and ignored: nothing is paged any more.)"""
    c = Canvas(width, height, glyphs, console)
    width, height = c.w, c.h
    st = st if isinstance(st, dict) else {}
    now = _f(st.get("time")) or 0
    try:
        clock = datetime.datetime.fromtimestamp(now).astimezone().strftime("%a %d %b %H:%M:%S %Z") if now else ""
    except (ValueError, OverflowError, OSError):
        clock = ""
    warns = warnings(st, now)
    if any(s == "bad" for _, s in warns):
        badge, bstyle = " %d PROBLEM%s " % (len(warns), "" if len(warns) == 1 else "S"), "header_bad"
    elif warns:
        badge, bstyle = " %d TO CHECK " % len(warns), "header_warn"
    else:
        badge, bstyle = " ALL CLEAR ", "header_ok"
    c.fill(0, 0, width, 1, " ", "header")
    bw = min(swidth(badge), max(0, width - 4))
    c.put(0, 0, " %s   %s   up %s" % (st.get("host") or "server", clock, dur(st.get("uptime"))), "header",
          maxw=max(0, width - bw))
    c.put(width - bw, 0, badge, bstyle, maxw=bw)
    rng = "5 min" if range_s <= 300 else "1 h"
    ctx = {"now": now, "range_s": range_s, "rng": rng, "warns": warns}
    placed = plan(width, height)
    for name, x, y, pw, ph in placed:
        c.blit(draw_panel(name, pw, ph, st, ctx, glyphs, console), x, y)
    if not placed:
        c.put(0, 1, "Screen too small: needs 80x24", "warn", maxw=width)
    shown = {p[0] for p in placed}
    keys = " t: chart range %s" % rng
    if placed and "health" not in shown and warns:
        keys += "   %d to check" % len(warns)
    keys += "   counts, sizes and times only"
    c.fill(0, height - 1, width, 1, " ", "footer")
    c.put(0, height - 1, keys, "footer", maxw=width)
    if st.get("pairing"):
        overlay_pairing(c, st["pairing"], now)
    return c


def overlay_pairing(c, window, now):
    """The pairing code, as large as the screen allows, over everything."""
    from o1auth import format_code
    import o1big
    window = window if isinstance(window, dict) else {}
    code = format_code(window.get("code", ""))
    on = "#" if c.glyphs == "ascii" else "█"
    rows = o1big.render_code(code, c.w - 4, on=on, height=max(5, c.h - 8))
    if len(rows) + 6 > c.h:                          # too small for the big letters: the code as text
        rows = []
    left = max(0, int((_f(window.get("expires_at")) or now) - now))
    c.fill(0, 0, c.w, c.h, " ", "overlay")
    lines = ["PAIRING WINDOW OPEN", ""] + rows + ["", "%s    closes in %d:%02d" % (code, left // 60, left % 60),
                                                  "Type this code in ConcordeAI on the device you are pairing."]
    top = max(0, (c.h - len(lines)) // 2)
    width = max(swidth(r) for r in rows) if rows else 0
    for i, text in enumerate(lines):
        if top + i >= c.h:
            break
        if 2 <= i < 2 + len(rows):
            c.put(max(0, (c.w - width) // 2), top + i, text, "overlay_code", maxw=c.w)
        else:
            c.put(max(0, (c.w - swidth(text)) // 2), top + i, text, "overlay", maxw=c.w)
