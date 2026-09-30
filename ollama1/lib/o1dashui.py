"""The dashboard's renderer: state in, a grid of characters and styles out.

Pure Python, no curses: ollama1-dash paints the grid with curses, and the
tests render it to text at any size. It only ever draws counts, sizes,
timings and names from the fields it asks for by name; there is no field
it could print a prompt or an answer from.

Glyph sets:
  braille  charts in U+2800 dots (2 x 4 per cell), block bars, box lines
  blocks   charts in lower eighth blocks (U+2581..2588), block bars, box lines
  ascii    charts in ' .:-=+*#', bars in '#', boxes in + - |
"""
import datetime
import math

# ---- glyphs ------------------------------------------------------------------

EIGHTHS_V = " ▁▂▃▄▅▆▇█"      # lower blocks
EIGHTHS_H = " ▏▎▍▌▋▊▉█"      # left blocks
ASCII_V = " .:-=+*#"
BOX = {"h": "─", "v": "│", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘"}
BOX_ASCII = {"h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+"}
# braille: dot bit for (column 0/1, row 0..3 from the top)
BRAILLE_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

ASCII_MAP = {"°": "", "·": ".", "•": "*", "█": "#"}
# every glyph each mode may draw beyond ASCII (the font picker checks these)
NEEDED = {
    "blocks": set(EIGHTHS_V + EIGHTHS_H + "".join(BOX.values()) + "·•°") - {" "},
    "braille": set(chr(0x2800 + i) for i in range(256)),
}


# ---- canvas ------------------------------------------------------------------

class Canvas:
    def __init__(self, width, height, glyphs="blocks"):
        self.w, self.h = max(1, width), max(1, height)
        self.glyphs = glyphs
        self.chars = [[" "] * self.w for _ in range(self.h)]
        self.styles = [[""] * self.w for _ in range(self.h)]

    def put(self, x, y, text, style=""):
        if y < 0 or y >= self.h:
            return
        if self.glyphs == "ascii":
            text = "".join(ASCII_MAP.get(c, c) if 32 <= ord(c) < 127 or c in ASCII_MAP else "?"
                           for c in str(text))
        for i, ch in enumerate(str(text)):
            xi = x + i
            if 0 <= xi < self.w:
                self.chars[y][xi] = ch
                self.styles[y][xi] = style

    def text(self):
        return "\n".join("".join(r) for r in self.chars)

    def box(self, x, y, w, h, title="", style="border"):
        if w < 2 or h < 2:
            return
        b = BOX_ASCII if self.glyphs == "ascii" else BOX
        self.put(x, y, b["tl"] + b["h"] * (w - 2) + b["tr"], style)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, b["v"], style)
            self.put(x + w - 1, yy, b["v"], style)
        self.put(x, y + h - 1, b["bl"] + b["h"] * (w - 2) + b["br"], style)
        if title and w > 6:
            self.put(x + 2, y, " %s " % title[: w - 6], "title")

    def hbar(self, x, y, w, frac, style=""):
        """A horizontal bar w cells wide, filled to frac (0..1)."""
        if w <= 0:
            return
        frac = 0.0 if frac is None or frac != frac else max(0.0, min(1.0, frac))
        if self.glyphs == "ascii":
            n = int(round(frac * w))
            self.put(x, y, "#" * n, style)
            self.put(x + n, y, "." * (w - n), "dim")
            return
        eighths = int(round(frac * w * 8))
        full, part = divmod(eighths, 8)
        s = "█" * full + (EIGHTHS_H[part] if part and full < w else "")
        self.put(x, y, s, style)
        rest = w - len(s)
        if rest > 0:
            self.put(x + len(s), y, "·" * rest, "dim")

    def chart(self, x, y, w, h, values, vmax=None, style="", floor=0.0):
        """An area chart of `values` (oldest first) in a w x h cell box."""
        if w <= 0 or h <= 0:
            return
        per_cell = 2 if self.glyphs == "braille" else 1
        vals = resample(values, w * per_cell)
        top = vmax if vmax else max([v for v in vals if v is not None] + [floor, 1e-9])
        if self.glyphs == "braille":
            levels = h * 4
            for cx in range(w):
                for row in range(h):
                    bits = 0
                    for side in (0, 1):
                        v = vals[cx * 2 + side]
                        if v is None:
                            continue
                        n = int(round(max(0.0, min(1.0, v / top)) * levels))
                        if v > 0 and n == 0:
                            n = 1
                        # dots filled in this row (rows counted from the bottom)
                        start = (h - 1 - row) * 4
                        fill = max(0, min(4, n - start))
                        for d in range(fill):
                            bits |= BRAILLE_BITS[side][3 - d]
                    if bits:
                        self.put(x + cx, y + row, chr(0x2800 + bits), style)
            return
        levels = h * 8
        glyph = EIGHTHS_V if self.glyphs == "blocks" else None
        for cx in range(w):
            v = vals[cx]
            if v is None:
                continue
            n = int(round(max(0.0, min(1.0, v / top)) * levels))
            if v > 0 and n == 0:
                n = 1
            for row in range(h):
                start = (h - 1 - row) * 8
                fill = max(0, min(8, n - start))
                if not fill:
                    continue
                if glyph:
                    ch = glyph[fill]
                else:
                    ch = "#" if fill == 8 else ASCII_V[min(7, fill)]
                self.put(x + cx, y + row, ch, style)


def resample(values, n):
    """values -> exactly n points (average of buckets; None if a bucket is
    empty). Short series are right-aligned, with None on the left."""
    vals = [v for v in values]
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

def gib(b):
    return "%.1f" % ((b or 0) / 2**30)


def human_bytes(b):
    b = float(b or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if b < 1024 or unit == "T":
            return ("%.0f%s" if unit == "B" else "%.1f%s") % (b, unit)
        b /= 1024
    return "%.1fT" % b


def rate(bps):
    return human_bytes(bps) + "/s"


def dur(s):
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
    return "never" if not t else dur(now - t) + " ago"


def pct_style(p, warn=75, bad=90):
    if p is None:
        return "dim"
    return "bad" if p >= bad else ("warn" if p >= warn else "ok")


def num(v, fmt="%.1f"):
    return "-" if v is None else fmt % v


# ---- panels ------------------------------------------------------------------
# Each panel draws inside (x, y, w, h), border included, and adapts: charts
# get the rows left after the text, and vanish when there are none.

def _inner(c, x, y, w, h, title):
    c.box(x, y, w, h, title)
    return x + 2, y + 1, w - 4, h - 2


def _series(st, name, range_s):
    s = (st.get("series") or {}).get(name) or []
    return s[-range_s:]


def panel_tokens(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Tokens per second")
    gw = st.get("gw") or {}
    tps = gw.get("tps") or {}
    series = _series(st, "tps", range_s)
    peak = max([v for v in series if v is not None] + [0])
    lines = [
        [("now ", "dim"), ("%-7s" % num(tps.get("current")), "value"), (" 1 h ", "dim"),
         ("%-7s" % num(tps.get("1h")), "value"), (" 24 h ", "dim"), ("%-7s" % num(tps.get("24h")), "value"),
         (" peak ", "dim"), (num(peak), "value")],
        [("prompt ", "dim"), ("%s tok/s" % num(gw.get("prompt_tps")), "value"),
         ("   first token ", "dim"), (("%.2f s" % (gw["ttft_ms"] / 1000.0)) if gw.get("ttft_ms") else "-", "value")],
    ]
    _lines(c, ix, iy, iw, lines[: max(0, ih)])
    ch = ih - len(lines)
    if ch >= 1:
        c.chart(ix, iy + len(lines), iw, ch, series, style="c_tps", floor=10)


def panel_requests(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Requests")
    gw = st.get("gw") or {}
    err = gw.get("errors") or {}
    kinds = [("busy", "busy"), ("gpu_fit", "fit"), ("gpu_spill", "spill"), ("ram_pressure", "ram"),
             ("ram_oom", "oom"), ("ollama", "ollama")]
    e_line = [("errors ", "dim")]
    for k, label in kinds:
        e_line += [("%s " % label, "dim"), ("%d  " % err.get(k, 0), "bad" if err.get(k) else "value")]
    e_line += [("auth ", "dim"), ("%d" % ((gw.get("totals") or {}).get("auth_failures", 0)), "value")]
    lines = [
        [("active ", "dim"), ("%d" % gw.get("active", 0), "value"), ("  queued ", "dim"),
         ("%d" % gw.get("queued", 0), "warn" if gw.get("queued") else "value"), ("  today ", "dim"),
         ("%d" % gw.get("requests_today", 0), "value")],
        e_line,
    ]
    _lines(c, ix, iy, iw, lines[: max(0, ih)])
    ch = ih - len(lines)
    if ch >= 1:
        minutes = 5 if range_s <= 300 else 60
        per_min = [m[3] for m in (gw.get("per_minute") or [])][-minutes:]
        c.put(ix, iy + len(lines), "per minute, last %d min" % minutes, "dim")
        if ch >= 2:
            c.chart(ix, iy + len(lines) + 1, iw, ch - 1, per_min, style="c_req", floor=1)


def panel_models(c, x, y, w, h, st, now):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Loaded models")
    gw = st.get("gw") or {}
    loaded = gw.get("loaded") or []
    if not loaded:
        c.put(ix, iy, "no model in memory", "dim")
        return
    row = 0
    for m in loaded:
        if row >= ih:
            break
        size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
        share = int(100 * vram / size) if size else 0
        left = _until(m.get("expires_at"), now)
        detail = "%3d%% GPU  %s GiB VRAM  %s GiB RAM  unload in %s" % (share, gib(vram), gib(size - vram), left)
        if len(detail) + 10 > iw:
            detail = "%3d%% GPU  %s/%s GiB  %s" % (share, gib(vram), gib(size - vram), left)
        name = str(m.get("name") or "?")[: max(6, iw - len(detail) - 1)]
        c.put(ix, iy + row, name, "value")
        c.put(ix + iw - len(detail), iy + row, detail[:iw],
              "text" if share >= 100 or m.get("ram_allowed") else "bad")
        row += 1
        if share < 100 and row < ih:
            c.put(ix + 2, iy + row, "rest in RAM" if m.get("ram_allowed") else "PARTLY ON CPU (not allowed)",
                  "warn" if m.get("ram_allowed") else "bad")
            row += 1


def panel_events(c, x, y, w, h, st, now):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Model events")
    events = ((st.get("gw") or {}).get("events") or [])[-ih:]
    if not events:
        c.put(ix, iy, "none yet", "dim")
    for i, e in enumerate(reversed(events)):
        t = datetime.datetime.fromtimestamp(e.get("t", 0)).strftime("%H:%M:%S")
        kind = str(e.get("kind", ""))
        style = "bad" if kind.startswith("refused") else ("warn" if "unload" in kind or "switch" in kind else "ok")
        c.put(ix, iy + i, t, "dim")
        c.put(ix + 9, iy + i, ("%-16s %s" % (kind, e.get("model", "")))[: iw - 9], style)


def panel_gpu(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "GPU")
    g = st.get("gpu")
    if not g:
        c.put(ix, iy, "no GPU found", "bad")
        return
    temps = g.get("temps") or {}
    vt, vu = g.get("vram_total") or 0, g.get("vram_used") or 0
    lines = [
        [("busy ", "dim"), ("%3s%%" % num(g.get("busy_pct"), "%d"), pct_style(g.get("busy_pct"), 101, 101)),
         ("  VRAM ", "dim"), ("%s/%s GiB" % (gib(vu), gib(vt)), pct_style(100 * vu / vt if vt else None)),
         ("  power ", "dim"), ("%s/%s W" % (num(g.get("power_w"), "%.0f"), num(g.get("power_cap_w"), "%.0f")), "value")],
        [("temp ", "dim")] + sum([[("%s " % k, "dim"), ("%d°C  " % v, pct_style(v, 80, 95))]
                                  for k, v in sorted(temps.items())], []) +
        [("fan ", "dim"), ("%s rpm" % num(g.get("fan_rpm"), "%d"), "value"),
         (" (%s%%)" % num(g.get("fan_pct"), "%d"), "dim")],
        [("clocks ", "dim"), ("sclk %s MHz  mclk %s MHz" % (num(g.get("sclk_mhz"), "%d"), num(g.get("mclk_mhz"), "%d")),
                              "value")],
    ]
    _lines(c, ix, iy, iw, lines[: max(0, ih)])
    rows = ih - len(lines)
    charts = [("busy %", "gpu_busy", 100, "c_gpu"), ("VRAM", "vram_used", vt or None, "c_mem"),
              ("power", "gpu_power", g.get("power_cap_w") or None, "c_pow")]
    _stacked_charts(c, ix, iy + len(lines), iw, rows, st, charts, range_s)


def _stacked_charts(c, x, y, w, rows, st, charts, range_s):
    """Several labelled charts sharing `rows`; fewer if it's tight."""
    if rows < 1:
        return
    n = min(len(charts), max(1, rows // 3)) if rows >= 3 else 1
    per = rows // n
    for i, (label, name, vmax, style) in enumerate(charts[:n]):
        top = y + i * per
        c.put(x, top, label, "dim")
        lw = len(label) + 1
        if per == 1:
            c.chart(x + lw, top, w - lw, 1, _series(st, name, range_s), vmax=vmax, style=style)
        else:
            c.chart(x, top + 1, w, per - 1, _series(st, name, range_s), vmax=vmax, style=style)


def panel_cpu(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "CPU")
    cpu = st.get("cpu") or {}
    cores = cpu.get("cores") or []
    load = cpu.get("load") or []
    head = [("total ", "dim"), ("%s%%" % num(cpu.get("total"), "%.0f"), pct_style(cpu.get("total"))),
            ("  temp ", "dim"), ("%s°C" % num(cpu.get("temp"), "%.0f"), pct_style(cpu.get("temp"), 80, 90)),
            ("  load ", "dim"), (" ".join("%.2f" % l for l in load), "value")]
    _lines(c, ix, iy, iw, [head])
    rows = ih - 1
    if rows < 1 or not cores:
        return
    # a grid of per-core bars: as many columns as fit, rows as needed
    cell = 14 if iw >= 60 else 10
    cols = max(1, iw // cell)
    need_rows = int(math.ceil(len(cores) / float(cols)))
    if need_rows > rows:
        cols = int(math.ceil(len(cores) / float(rows)))
        cell = max(6, iw // cols)
        need_rows = rows
    for i, p in enumerate(cores):
        r, col = i % need_rows, i // need_rows
        cx = ix + col * cell
        if r >= rows or cx + cell > ix + iw + 1:
            continue
        label = "%2d" % i
        c.put(cx, iy + 1 + r, label, "dim")
        c.hbar(cx + 3, iy + 1 + r, cell - 4, (p or 0) / 100.0, pct_style(p))
    left = rows - need_rows
    if left >= 3:
        c.put(ix, iy + 1 + need_rows, "total %", "dim")
        c.chart(ix, iy + 2 + need_rows, iw, left - 1, _series(st, "cpu_total", range_s), vmax=100, style="c_cpu")


def panel_memory(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Memory")
    m = st.get("mem") or {}
    cg = st.get("ollama_cg") or {}
    total = m.get("total") or 0
    used = total - (m.get("available") or 0)
    swap_used = (m.get("swap_total") or 0) - (m.get("swap_free") or 0)
    lines = [
        [("RAM ", "dim"), ("%s/%s GiB" % (gib(used), gib(total)), pct_style(100 * used / total if total else None)),
         ("  avail ", "dim"), ("%s GiB" % gib(m.get("available")), "value"),
         ("  swap ", "dim"), ("%s/%s GiB" % (gib(swap_used), gib(m.get("swap_total"))), "warn" if swap_used > 2**28 else "value")],
        [("Ollama ", "dim"), ("%s GiB" % gib(cg.get("current")), "value"),
         (" of cap %s GiB" % (gib(cg.get("max")) if cg.get("max") else "none"), "dim")],
    ]
    _lines(c, ix, iy, iw, lines[: max(0, ih)])
    if ih > 2 and cg.get("max"):
        c.hbar(ix, iy + 2, iw, (cg.get("current") or 0) / float(cg["max"]), pct_style(100 * (cg.get("current") or 0) / cg["max"], 80, 92))
        start = 3
    else:
        start = 2
    rows = ih - start
    if rows >= 1:
        c.chart(ix, iy + start, iw, rows, _series(st, "ram_used", range_s), vmax=total or None, style="c_mem")


def panel_disks(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Disks")
    row = 0
    for d in st.get("disks") or []:
        if row >= ih:
            return
        label = "%-12s" % d.get("mount", "?")[:12]
        c.put(ix, iy + row, label, "dim")
        if d.get("mounted") and d.get("total"):
            frac = d["used"] / float(d["total"])
            c.hbar(ix + 13, iy + row, max(4, iw - 36), frac, pct_style(100 * frac, 80, 92))
            c.put(ix + iw - 22, iy + row, "%s free of %s" % (human_bytes(d.get("free")), human_bytes(d["total"])), "value")
        else:
            c.put(ix + 13, iy + row, "not mounted", "bad")
        row += 1
    for a in st.get("raid") or []:
        if row >= ih:
            return
        s = "%s %s [%s] %s" % (a.get("name"), a.get("level"), a.get("members"), "healthy" if a.get("healthy") else "DEGRADED")
        if a.get("action"):
            s += "  %s %s%%  about %s left" % (a["action"], a.get("progress"), a.get("finish") or "?")
        c.put(ix, iy + row, s[:iw], "ok" if a.get("healthy") else "bad")
        row += 1
    io = st.get("io") or {}
    if row < ih:
        c.put(ix, iy + row, "I/O  read %s  write %s" % (rate(io.get("read_bps")), rate(io.get("write_bps"))), "value")
        row += 1
    rows = ih - row
    if rows >= 1:
        _pair_chart(c, ix, iy + row, iw, rows, _series(st, "io_read", range_s), _series(st, "io_write", range_s), "c_io")


def _pair_chart(c, x, y, w, rows, a, b, style):
    """Two series side by side (read|write, in|out) on one shared scale."""
    half = (w - 1) // 2
    top = max([v for v in a + b if v is not None] + [1.0])
    c.chart(x, y, half, rows, a, vmax=top, style=style)
    c.chart(x + half + 1, y, w - half - 1, rows, b, vmax=top, style="c_io2")


def panel_network(c, x, y, w, h, st, range_s):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Network")
    n = st.get("net") or {}
    t = st.get("tunnel") or {}
    lines = [
        [(n.get("name", "br0") + " ", "dim"), (n.get("address") or "no address", "value"),
         ("  in ", "dim"), (rate(n.get("rx_bps")), "value"), ("  out ", "dim"), (rate(n.get("tx_bps")), "value")],
        [("tunnel ", "dim"), ("connected (%d)" % t.get("connections", 0) if t.get("up") else "DOWN", "ok" if t.get("up") else "bad"),
         ("  rtt ", "dim"), (("%.0f ms" % t["rtt_ms"]) if t.get("rtt_ms") is not None else "-", "value")],
        [("ports ", "dim")] + sum([[("%s " % p.get("name"), "dim"),
                                    ("%s  " % (("link %s" % (("%d Mb/s" % p["speed_mbps"]) if p.get("speed_mbps") else ""))
                                               if p.get("carrier") else "no link"), "ok" if p.get("carrier") else "warn")]
                                   for p in n.get("ports") or []], []),
    ]
    _lines(c, ix, iy, iw, lines[: max(0, ih)])
    rows = ih - len(lines)
    if rows >= 1:
        _pair_chart(c, ix, iy + len(lines), iw, rows, _series(st, "net_rx", range_s), _series(st, "net_tx", range_s), "c_net")


def panel_health(c, x, y, w, h, st, now):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Health")
    up = st.get("updates") or {}
    oll = up.get("ollama") or {}
    lines = [
        [("uptime ", "dim"), (dur(st.get("uptime")), "value"), ("  reboot needed ", "dim"),
         ("yes" if up.get("reboot_required") else "no", "warn" if up.get("reboot_required") else "ok")],
        [("security updates ", "dim"), (ago(up.get("last_unattended"), now), "value")],
        [("Ollama ", "dim"), ("%s %s" % (oll.get("result", "not run"), oll.get("version", "")), "bad" if oll.get("result") == "failed" else "value"),
         ("  next check ", "dim"), (str(up.get("next_ollama_update") or "-"), "value")],
        [("last sleep ", "dim"), (ago((up.get("sleep") or {}).get("last_sleep"), now), "value"),
         ("  wake ", "dim"), (ago((up.get("sleep") or {}).get("last_wake"), now), "value")]
        + ([("  after waking ", "dim"),
            ("ok" if ((up.get("sleep") or {}).get("resume_check") or {}).get("ok") else "PROBLEM",
             "ok" if ((up.get("sleep") or {}).get("resume_check") or {}).get("ok") else "bad")]
           if (up.get("sleep") or {}).get("resume_check") else []),
    ]
    pw = st.get("power")
    if pw:
        badge = pw.get("badge") or ""
        line = [("power ", "dim"),
                ("%s W" % num(pw.get("watts"), "%.0f") if pw.get("watts") is not None else "no reading",
                 "value" if pw.get("watts") is not None else "warn"),
                (" (plug)" if pw.get("src") == "plug" else " (estimate)" if pw.get("src") else "", "dim"),
                ("  24 h ", "dim"), ("%s kWh" % num(pw.get("kwh_24h"), "%.2f"), "value")]
        if pw.get("cost_24h") is not None:
            line.append(("  %s%.2f" % (pw.get("symbol") or "$", pw["cost_24h"]), "value"))
        if badge:
            line.append(("  " + badge, "bad" if badge.startswith("on-peak") else
                         "warn" if badge.startswith("mid-peak") else "ok"))
        lines.insert(0, line)
    _lines(c, ix, iy, iw, lines[: max(0, ih)])


def panel_devices(c, x, y, w, h, st, now):
    ix, iy, iw, ih = _inner(c, x, y, w, h, "Paired devices")
    devs = sorted((st.get("gw") or {}).get("devices") or [], key=lambda d: -(d.get("last_seen") or 0))
    if not devs:
        c.put(ix, iy, "none seen since the gateway started", "dim")
    for i, d in enumerate(devs[:ih]):
        name = str(d.get("name", "?"))[: iw - 24]
        c.put(ix, iy + i, name, "value")
        state = "connected" if d.get("connected") else ago(d.get("last_seen"), now)
        if d.get("active"):
            state += ", %d active" % d["active"]
        c.put(ix + iw - 22, iy + i, state[:22], "ok" if d.get("connected") else "dim")


def _lines(c, x, y, w, lines):
    for i, parts in enumerate(lines):
        cx = x
        for text, style in parts:
            if cx >= x + w:
                break
            c.put(cx, y + i, text[: x + w - cx], style)
            cx += len(text)


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
    except ValueError:
        return "-"
    return dur(t - now) if t > now else "now"


# ---- the screen ----------------------------------------------------------------

def render(st, width, height, glyphs="blocks", range_s=300, page=0):
    """The whole dashboard as a Canvas. `page` matters only when the
    screen is too small to show everything at once."""
    c = Canvas(width, height, glyphs)
    now = st.get("time") or 0
    clock = datetime.datetime.fromtimestamp(now).astimezone().strftime("%a %d %b %H:%M:%S %Z") if now else ""
    gw = st.get("gw")
    status = "gateway running" if gw and not gw.get("stale") else "GATEWAY NOT RUNNING"
    head = " %s  %s  up %s  %s " % (st.get("host", "ollama1"), clock, dur(st.get("uptime")), status)
    c.put(0, 0, head.ljust(width)[:width], "header")
    rng = "5 min" if range_s <= 300 else "1 h"
    W, H = width, height - 2
    y0 = 1
    if W >= 180 and H >= 44:
        cols = [W // 3, W // 3, W - 2 * (W // 3)]
        xs = [0, cols[0], cols[0] + cols[1]]
        _column(c, xs[0], y0, cols[0], H, [(panel_tokens, 0.26, "r"), (panel_requests, 0.2, "r"),
                                           (panel_models, 0.16, "n"), (panel_events, 0.2, "n"),
                                           (panel_devices, 0.18, "n")], st, range_s, now)
        _column(c, xs[1], y0, cols[1], H, [(panel_gpu, 0.55, "r"), (panel_memory, 0.45, "r")], st, range_s, now)
        _column(c, xs[2], y0, cols[2], H, [(panel_cpu, 0.3, "r"), (panel_disks, 0.3, "r"),
                                           (panel_network, 0.24, "r"), (panel_health, 0.16, "n")], st, range_s, now)
        pages = 1
    elif W >= 100 and H >= 28:
        half = W // 2
        if page % 2 == 0:
            _column(c, 0, y0, half, H, [(panel_tokens, 0.4, "r"), (panel_requests, 0.3, "r"), (panel_devices, 0.3, "n")],
                    st, range_s, now)
            _column(c, half, y0, W - half, H, [(panel_gpu, 0.5, "r"), (panel_models, 0.25, "n"), (panel_events, 0.25, "n")],
                    st, range_s, now)
        else:
            _column(c, 0, y0, half, H, [(panel_cpu, 0.45, "r"), (panel_memory, 0.55, "r")], st, range_s, now)
            _column(c, half, y0, W - half, H, [(panel_disks, 0.4, "r"), (panel_network, 0.35, "r"), (panel_health, 0.25, "n")],
                    st, range_s, now)
        pages = 2
    else:
        pages = 3
        p = page % 3
        if p == 0:
            _column(c, 0, y0, W, H, [(panel_tokens, 0.34, "r"), (panel_requests, 0.3, "r"), (panel_models, 0.36, "n")],
                    st, range_s, now)
        elif p == 1:
            _column(c, 0, y0, W, H, [(panel_gpu, 0.55, "r"), (panel_memory, 0.45, "r")], st, range_s, now)
        else:
            _column(c, 0, y0, W, H, [(panel_cpu, 0.4, "r"), (panel_disks, 0.3, "r"), (panel_network, 0.3, "r")],
                    st, range_s, now)
    keys = " t range: %s" % rng + ("   p page %d/%d" % (page % pages + 1, pages) if pages > 1 else "")
    keys += "   counts, sizes and times only" if width >= 80 else ""
    c.put(0, height - 1, keys.ljust(width)[:width], "footer")
    if st.get("pairing"):
        overlay_pairing(c, st["pairing"], now)
    return c


def _column(c, x, y, w, h, panels, st, range_s, now):
    heights = [max(3, int(round(h * share))) for _, share, _ in panels]
    heights[-1] = max(3, h - sum(heights[:-1]))
    while sum(heights) > h and len(heights) > 1:
        # too little room: drop the last panel
        panels, heights = panels[:-1], heights[:-1]
        heights[-1] = max(3, h - sum(heights[:-1]))
    yy = y
    for (fn, _share, kind), ph in zip(panels, heights):
        ph = min(ph, y + h - yy)
        if ph < 3:
            break
        if kind == "r":
            fn(c, x, yy, w, ph, st, range_s)
        else:
            fn(c, x, yy, w, ph, st, now)
        yy += ph


def overlay_pairing(c, window, now):
    """The pairing code, as large as the screen allows, over everything."""
    from o1auth import format_code
    import o1big
    code = format_code(window.get("code", ""))
    on = "#" if c.glyphs == "ascii" else "█"
    rows = o1big.render_code(code, c.w - 4, on=on, height=max(5, c.h - 8))
    left = max(0, int(window.get("expires_at", now) - now))
    for yy in range(c.h):
        c.put(0, yy, " " * c.w, "overlay")
    lines = ["PAIRING WINDOW OPEN", ""] + rows + ["", "%s    closes in %d:%02d" % (code, left // 60, left % 60),
                                                  "Type this code in ConcordeAI on the device you are pairing."]
    top = max(0, (c.h - len(lines)) // 2)
    width = max(len(r) for r in rows)
    for i, text in enumerate(lines):
        if top + i >= c.h:
            break
        if 2 <= i < 2 + len(rows):
            c.put((c.w - width) // 2, top + i, text, "overlay_code")
        else:
            c.put(max(0, (c.w - len(text)) // 2), top + i, text[: c.w], "overlay")
