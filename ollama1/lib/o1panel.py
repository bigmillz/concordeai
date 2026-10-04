"""The graphical information panel (6b380): the server's state drawn as bars,
rings and trend lines into a Pixmap (lib/o1gfx.py), for a screen that is
connected to the server. ollama1-dash writes the picture to the Linux
framebuffer (lib/o1fb.py); `ollama1-dash --png` writes it to a file.

It reads the very same state dict as the text dashboard (lib/o1dashui.py)
and reuses that module's readers and its list of problems, so the two never
disagree about what is wrong. It draws counts, sizes and timings only: there
is no field in it that could show a prompt or an answer.

Built so that nothing overlaps (as the text dashboard is):

  * layout(w, h) splits the screen into fixed boxes, none inside another;
  * each box is drawn inside Pixmap.clipped(box), so a widget cannot paint
    outside its box whatever it is given;
  * every string is cut to its column with an ellipsis by its measured pixel
    width (o1pixfont.fit), never by its length;
  * a panel that raises draws "no data" and notes the error in ERRORS; the
    rest of the screen is unaffected.

It is designed for a logical screen of about 640x360 (o1fb scales that up
to the real one by a whole number) and adapts to anything from 480x270 up.
"""
import datetime
import math

import o1dashui as D
import o1gfx
import o1pixfont as F

# ---- colours -------------------------------------------------------------------

T = {
    "bg": 0x080c12, "panel": 0x111821, "edge": 0x263446, "head": 0x0d1420, "plot": 0x0c121a,
    "grid": 0x1b2735, "track": 0x1f2c3c, "title": 0x9db3cc,
    "text": 0xe8eef6, "dim": 0x8497ad, "ok": 0x38d98a, "warn": 0xffb020, "bad": 0xff5a5a,
    "gpu": 0x4da3ff, "gpu2": 0xb18cff, "cpu": 0x38d9c8, "cpu2": 0xffb020, "net": 0x6fd3ff,
    "pow": 0xffb020, "tmp": 0xff7a59, "tps": 0x38d98a, "mem": 0xb18cff,
}
STYLE = {"ok": "ok", "warn": "warn", "bad": "bad", "value": "text", "text": "text", "dim": "dim"}


def col(style):
    return T[STYLE.get(style, "text")]


ERRORS = []       # panels that raised (the tests assert this stays empty)
MIN_W, MIN_H = 480, 270


# ---- layout -------------------------------------------------------------------

def layout(w, h):
    """The boxes for a w x h screen: {name: (x, y, width, height)}. Empty
    when the screen is too small. No two of them overlap."""
    if w < MIN_W or h < MIN_H:
        return {}
    m = 6 if w >= 560 else 4
    head_h = 24 if h >= 300 else 20
    foot_h = 16
    top = head_h + m
    bottom = h - foot_h - m
    body = bottom - top
    top_h = (body - m) * 53 // 100
    bot_h = body - m - top_h
    col_w = (w - 2 * m - m) // 2
    avail = w - 2 * m - 2 * m
    w1 = avail * 40 // 100
    w2 = avail * 32 // 100
    w3 = avail - w1 - w2
    y2 = top + top_h + m
    return {
        "header": (0, 0, w, head_h),
        "gpu": (m, top, col_w, top_h),
        "cpu": (m + col_w + m, top, w - 2 * m - col_w - m, top_h),
        "models": (m, y2, w1, bot_h),
        "storage": (m + w1 + m, y2, w2, bot_h),
        "status": (m + w1 + m + w2 + m, y2, w3, bot_h),
        "footer": (0, h - foot_h, w, foot_h),
    }


# ---- helpers on the state -----------------------------------------------------------

def _d(v):
    return v if isinstance(v, dict) else {}


def _l(v):
    return v if isinstance(v, list) else []


def series(st, name, range_s):
    s = _d(st.get("series")).get(name)
    return list(s)[-range_s:] if isinstance(s, (list, tuple)) else []


def last_value(vals):
    for v in reversed(vals):
        f = D._f(v)
        if f is not None:
            return f
    return None


def nice_max(v, floor=1.0):
    """The next 'round' number (1, 2, 5, 10 ... times a power of ten) at or above v."""
    v = max(v, floor)
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * p * 1.0001:
            return m * p
    return 10 * p


def gb(v):
    return "%.1f" % ((D._f(v) or 0) / 2**30)


def rate_short(bps):
    return D.rate(bps).replace("/s", "/s")


# ---- widgets: each draws inside the box it is given -------------------------------

def frame(pm, r, title, right="", accent=None):
    """A panel: border, fill, title. Returns the inside box, and narrows the
    clip to it (the caller's clipped() block restores the old one)."""
    x, y, w, h = r
    pm.rrect(x, y, w, h, 3, accent if accent is not None else T["edge"])
    pm.rrect(x + 1, y + 1, w - 2, h - 2, 2, T["panel"])
    tw = pm.text(x + 8, y + 5, title.upper(), T["title"], max_w=w - 16)
    if right:
        room = w - 16 - tw - 10
        if room > 12:
            pm.text_right(x + w - 8, y + 5, right, T["dim"], max_w=room)
    inner = (x + 8, y + 18, w - 16, h - 18 - 6)
    c = pm.clip                                   # from here on the widgets cannot reach the border or the title
    pm.clip = (max(c[0], inner[0]), max(c[1], inner[1]), min(c[2], inner[0] + inner[2]), min(c[3], inner[1] + inner[3]))
    return inner


def bar(pm, x, y, w, h, f, color):
    """A horizontal gauge: a dark track with a coloured fill up to f (0..1)."""
    if w < 4 or h < 1:
        return
    pm.rrect(x, y, w, h, min(2, h // 2), T["track"])
    f = D._f(f)
    if f is None:
        return
    f = max(0.0, min(1.0, f))
    n = int(round(f * w))
    if f > 0 and n < 2:
        n = 2
    if n:
        pm.rrect(x, y, n, h, min(2, h // 2, n // 2), color)


def widest(items, scale=1):
    return max([F.text_width(i, scale) for i in items] + [0])


def bar_rows(pm, box, rows, row_h=13):
    """rows: [(label, value text, fraction or None, colour)] one under the
    other, in a label column, a bar and a value column that are each as wide
    as their widest text (the bar takes what is left). Rows that do not fit
    in the box's height are left out."""
    x, y, w, h = box
    n = max(0, min(len(rows), (h + row_h - 7) // row_h))
    rows = rows[:n]
    if not rows:
        return 0
    lw = min(widest([r[0] for r in rows]), w * 38 // 100)
    vw = max(0, min(widest([r[1] for r in rows]), w - lw - 12 - 14))
    for i, (label, value, f, color) in enumerate(rows):
        ry = y + i * row_h
        pm.text(x, ry, label, T["dim"], max_w=lw)
        pm.text_right(x + w, ry, value, T["text"], max_w=vw)
        bx = x + lw + 6
        bw = x + w - vw - 6 - bx
        if bw >= 14 and value:
            bar(pm, bx, ry, bw, 7, f, color)
    return n * row_h


def ring(pm, cx, cy, r, th, f, color, label, sub=""):
    """A 270-degree dial, open at the bottom, with a figure inside."""
    start, sweep = 225.0, 270.0
    pm.arc(cx, cy, r, r - th, start, start + sweep, T["track"])
    f = D._f(f)
    if f is not None and f > 0:
        pm.arc(cx, cy, r, r - th, start, start + sweep * max(0.0, min(1.0, f)), color)
    inner = 2 * (r - th) - 6
    number, unit = (label[:-1], "%") if label.endswith("%") else (label, "")
    scale = 2 if F.text_width(number, 2) + (F.advance("%") if unit else 0) <= inner else 1
    nw = F.text_width(number, scale)
    uw = F.text_width(unit) + 1 if unit else 0
    ty = cy - (7 * scale) // 2 - (4 if sub else 0)
    x0 = cx - (nw + uw) // 2
    pm.text(x0, ty, number, T["text"], scale, max_w=inner)
    if unit:
        pm.text(x0 + nw + 1, ty + 7 * scale - 7, unit, T["dim"])
    if sub:
        pm.text_center(cx, cy + 8, sub, T["dim"], 1, max_w=inner)


def graph(pm, box, curves, range_label=""):
    """A rolling line graph with a legend line above it. curves: [{values,
    color, vmax (None = scale to the data), floor, label, now (text), fill}]
    drawn oldest to newest left to right; each in its own scale."""
    x, y, w, h = box
    if w < 24 or h < 22:
        return
    lx = x
    for c in curves:
        txt = "%s %s" % (c["label"], c["now"]) if c.get("now") else c["label"]
        tw = F.text_width(txt)
        if lx + tw > x + w:
            break
        pm.text(lx, y, txt, c["color"])
        lx += tw + 9
    if range_label and lx + F.text_width(range_label) + 4 <= x + w:
        pm.text_right(x + w, y, range_label, T["dim"])
    px, py, pw, ph = x, y + 11, w, h - 11
    pm.rrect(px, py, pw, ph, 2, T["edge"])
    pm.rrect(px + 1, py + 1, pw - 2, ph - 2, 1, T["plot"])
    ix, iy, iw, ih = px + 2, py + 2, pw - 4, ph - 4
    if iw < 8 or ih < 8:
        return
    with pm.clipped(ix, iy, iw, ih):
        for q in (1, 2, 3):
            pm.hline(ix, iy + ih * q // 4, iw, T["grid"])
        any_data = False
        for k, c in enumerate(curves):
            pts = D.resample(c["values"], iw)
            seen = [v for v in (D._f(p) for p in pts) if v is not None]
            if not seen:
                continue
            any_data = True
            top = c.get("vmax") or nice_max(max(seen) * 1.05, c.get("floor", 1.0))
            ys = []
            for p in pts:
                v = D._f(p)
                ys.append(None if v is None else iy + ih - 1 - int(round(max(0.0, min(1.0, v / top)) * (ih - 1))))
            if c.get("fill", k == 0):
                shade = o1gfx.mix(T["plot"], c["color"], 0.22)
                for i, yy in enumerate(ys):
                    if yy is not None:
                        pm.vline(ix + i, yy + 1, iy + ih - yy - 1, shade)
            thick = ih >= 28
            prev = None
            for i, yy in enumerate(ys):
                if yy is None:
                    prev = None
                    continue
                if prev is not None:
                    pm.line(ix + i - 1, prev, ix + i, yy, c["color"])
                    if thick:
                        pm.line(ix + i - 1, prev + 1, ix + i, yy + 1, c["color"])
                else:
                    pm.px(ix + i, yy, c["color"])
                prev = yy
        if not any_data:
            pm.text_center(ix + iw // 2, iy + ih // 2 - 3, "no data yet", T["dim"], max_w=iw - 4)


def tile(pm, x, y, w, big, label, color=None):
    """A big figure over a small label."""
    color = color if color is not None else T["text"]
    scale = 2 if F.text_width(big, 2) <= w else 1
    pm.text(x, y + (0 if scale == 2 else 4), big, color, scale, max_w=w)
    pm.text(x, y + 17, label, T["dim"], max_w=w)


def dot(pm, x, y, color):
    pm.disc(x + 3, y + 3, 3, color)


def wrap(text, max_w, max_lines, scale=1):
    """The text in at most max_lines lines, each at most max_w wide, broken
    at spaces; the last line ends with "..." if the text did not all fit."""
    words = F.clean(text).split()
    lines, cur = [], ""
    for i, wd in enumerate(words):
        trial = (cur + " " + wd).strip()
        if F.text_width(trial, scale) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = wd
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    rest = " ".join(words)
    done = " ".join(lines)
    if done != rest and lines:
        lines[-1] = F.fit(lines[-1] + " ...", max_w, scale) if F.text_width(lines[-1] + "...", scale) > max_w else lines[-1] + "..."
    return [F.fit(l, max_w, scale) for l in lines[:max_lines]]


# ---- the panels -------------------------------------------------------------------

def _graph_row(pm, inner, used_h, left, right, ctx):
    """Two graphs side by side under whatever is above them, if they fit."""
    x, y, w, h = inner
    gy = y + used_h
    gh = y + h - gy
    if gh < 30:
        return
    gw = (w - 6) // 2
    graph(pm, (x, gy, gw, gh), left, "")
    graph(pm, (x + gw + 6, gy, w - gw - 6, gh), right, "")


def temp_style(t, warn, bad):
    return D.pct_style(t, warn, bad)


def draw_gpu(pm, r, st, ctx):
    g = st.get("gpu")
    if not isinstance(g, dict):
        inner = frame(pm, r, "Graphics card", accent=T["bad"])
        pm.text(inner[0], inner[1] + 4, "No graphics card found", T["bad"], 2, max_w=inner[2])
        return
    sclk = D._f(g.get("sclk_mhz"))
    inner = frame(pm, r, "Graphics card", ("core %d MHz" % sclk) if sclk else "")
    x, y, w, h = inner
    busy = D._f(g.get("busy_pct"))
    vu, vt = D._f(g.get("vram_used")), D._f(g.get("vram_total"))
    pw, cap = D._f(g.get("power_w")), D._f(g.get("power_cap_w"))
    temps = [t for t in (D._f(v) for v in _d(g.get("temps")).values()) if t is not None]
    hot = D._f(D.dig(g, "temps", "junction"))
    hot = hot if hot is not None else (max(temps) if temps else None)
    rpm, fan = D._f(g.get("fan_rpm")), D._f(g.get("fan_pct"))
    rd = 28 if h >= 100 else 22
    ring(pm, x + rd + 1, y + rd, rd, 6 if rd == 28 else 5, (busy or 0) / 100.0 if busy is not None else None,
         T["gpu"], "%s%%" % D.num(busy, "%.0f") if busy is not None else "-", "busy" if rd == 28 else "")
    vf = D.frac(vu, vt)
    rows = [
        ("VRAM", "%s/%s GiB" % (gb(vu), gb(vt)) if vt else "-", vf,
         col(D.pct_style(100 * vf, 85, 97)) if vf is not None else T["gpu2"]),
        ("Power", ("%s/%s W" % (D.num(pw, "%.0f"), D.num(cap, "%.0f"))) if cap else ("%s W" % D.num(pw, "%.0f")),
         D.frac(pw, cap), T["pow"]),
        ("Temp", ("%s°C" % D.num(hot, "%.0f")) if hot is not None else "-", None if hot is None else hot / 110.0,
         col(D.pct_style(hot, 80, 95))),
        ("Fan", ("%d rpm" % rpm) if rpm is not None else ("%s%%" % D.num(fan, "%.0f") if fan is not None else "-"),
         None if fan is None else fan / 100.0, T["net"]),
    ]
    used = max(2 * rd + 1, bar_rows(pm, (x + 2 * rd + 10, y, w - 2 * rd - 10, 2 * rd + 2), rows, 14 if rd == 28 else 12))
    rs = ctx["range_s"]
    busy_s, vram_s = series(st, "gpu_busy", rs), series(st, "vram_used", rs)
    pow_s, tmp_s = series(st, "gpu_power", rs), series(st, "gpu_temp", rs)
    _graph_row(pm, inner, 2 * rd + 8, [
        {"values": busy_s, "color": T["gpu"], "vmax": 100, "label": "Use", "now": "%s%%" % D.num(last_value(busy_s), "%.0f")},
        {"values": [None if v is None else v / vt * 100 for v in vram_s] if vt else [], "color": T["gpu2"], "vmax": 100,
         "label": "VRAM", "now": "%s%%" % D.num(100 * vf, "%.0f") if vf is not None else "", "fill": False}],
        [{"values": pow_s, "color": T["pow"], "vmax": cap or None, "floor": 50, "label": "Power",
          "now": "%sW" % D.num(last_value(pow_s), "%.0f")},
         {"values": tmp_s, "color": T["tmp"], "vmax": 110, "label": "Temp",
          "now": ("%s°C" % D.num(hot, "%.0f")) if hot is not None else "", "fill": False}], ctx)


def draw_cpu(pm, r, st, ctx):
    cpu, m, cg = _d(st.get("cpu")), _d(st.get("mem")), _d(st.get("ollama_cg"))
    mhz, mhz_max = D._f(cpu.get("mhz")), D._f(cpu.get("max_mhz"))
    inner = frame(pm, r, "Processor and memory", ("%d cores" % len(_l(cpu.get("cores")))) if _l(cpu.get("cores")) else "")
    x, y, w, h = inner
    use = D._f(cpu.get("total"))
    temp = D._f(cpu.get("temp"))
    total = D._f(m.get("total")) or 0.0
    used = total - (D._f(m.get("available")) or 0.0) if total else None
    rd = 28 if h >= 100 else 22
    ring(pm, x + rd + 1, y + rd, rd, 6 if rd == 28 else 5, (use or 0) / 100.0 if use is not None else None,
         T["cpu"], "%s%%" % D.num(use, "%.0f") if use is not None else "-", "busy" if rd == 28 else "")
    uf = D.frac(used, total)
    cmax = D._f(cg.get("max"))
    rows = [
        ("Clock", ("%.2f GHz" % (mhz / 1000.0)) if mhz else "-", D.frac(mhz, mhz_max), T["cpu"]),
        ("Temp", ("%s°C" % D.num(temp, "%.0f")) if temp is not None else "-", None if temp is None else temp / 110.0,
         col(D.pct_style(temp, 80, 90))),
        ("RAM", "%s/%s GiB" % (gb(used), gb(total)) if total else "-", uf, col(D.pct_style(100 * uf, 85, 94)) if uf is not None else T["mem"]),
    ]
    if cmax:
        cf = D.frac(cg.get("current"), cmax)
        rows.append(("Ollama", "%s/%s GiB" % (gb(cg.get("current")), gb(cmax)), cf, col(D.pct_style(100 * (cf or 0), 85, 94))))
    else:
        st_t = D._f(m.get("swap_total")) or 0.0
        st_u = st_t - (D._f(m.get("swap_free")) or 0.0)
        rows.append(("Swap", "%s/%s GiB" % (gb(st_u), gb(st_t)), D.frac(st_u, st_t), T["warn"] if st_u > 2**28 else T["mem"]))
    bar_rows(pm, (x + 2 * rd + 10, y, w - 2 * rd - 10, 2 * rd + 2), rows, 14 if rd == 28 else 12)
    rs = ctx["range_s"]
    use_s, tmp_s, mhz_s, ram_s = series(st, "cpu_total", rs), series(st, "cpu_temp", rs), series(st, "cpu_mhz", rs), series(st, "ram_used", rs)
    _graph_row(pm, inner, 2 * rd + 8, [
        {"values": use_s, "color": T["cpu"], "vmax": 100, "label": "Use", "now": "%s%%" % D.num(last_value(use_s), "%.0f")},
        {"values": tmp_s, "color": T["tmp"], "vmax": 110, "label": "Temp",
         "now": ("%s°C" % D.num(temp, "%.0f")) if temp is not None else "", "fill": False}],
        [{"values": ram_s, "color": T["mem"], "vmax": total or None, "label": "RAM",
          "now": "%s GiB" % gb(last_value(ram_s)) if last_value(ram_s) is not None else ""},
         {"values": mhz_s, "color": T["cpu2"], "vmax": mhz_max or None, "label": "Clock",
          "now": ("%.1f GHz" % (mhz / 1000.0)) if mhz else "", "fill": False}], ctx)


def draw_models(pm, r, st, ctx):
    gw = _d(st.get("gw"))
    inner = frame(pm, r, "Requests and models", ("%d today" % (D._f(gw.get("requests_today")) or 0)) if gw else "")
    x, y, w, h = inner
    if not gw or gw.get("stale"):
        pm.text(x, y + 4, "Gateway is not running", T["bad"], 1, max_w=w)
        return
    active = int(D._f(gw.get("active")) or 0)
    queued = int(D._f(gw.get("queued")) or 0)
    tps = D._f(D.dig(gw, "tps", "current"))
    tw = (w - 8) // 3
    tile(pm, x, y, tw, "%d" % active, "in flight", T["text"] if not active else T["ok"])
    tile(pm, x + tw + 4, y, tw, "%d" % queued, "waiting", T["warn"] if queued else T["text"])
    tile(pm, x + 2 * (tw + 4), y, w - 2 * (tw + 4), D.num(tps, "%.1f") if tps is not None else "-", "tokens/s", T["tps"])
    yy = y + 30
    loaded = [m for m in _l(gw.get("loaded")) if isinstance(m, dict)]
    now = ctx["now"]
    if not loaded:
        pm.text(x, yy, "No model in memory", T["dim"], max_w=w)
        yy += 12
    room = max(0, (y + h - yy - 36) // 12 + 1)           # leave about 36 px under the list for the graph
    room = max(1, min(room, 4)) if y + h - yy >= 24 else 0
    shown = loaded if len(loaded) <= room else loaded[:max(0, room - 1)]
    for m in shown:
        size, vram = int(D._f(m.get("size")) or 0), int(D._f(m.get("size_vram")) or 0)
        share = int(100 * vram / size) if size else 0
        right = "%d%% GPU  %sG  %s" % (share, gb(vram), D._until(m.get("expires_at"), now))
        ok = share >= 100 or m.get("ram_allowed")
        rw = F.text_width(right)
        pm.text_right(x + w, yy, right, T["text"] if ok else T["warn"], max_w=w // 2 + 20)
        pm.text(x, yy, m.get("name") or "?", T["text"], max_w=max(10, w - rw - 8))
        yy += 12
    if len(loaded) > len(shown):
        pm.text(x, yy, "+%d more" % (len(loaded) - len(shown)), T["dim"])
        yy += 12
    gh = y + h - yy - 2
    if gh >= 32:
        sp = series(st, "tps", ctx["range_s"])
        graph(pm, (x, yy + 2, w, gh), [{"values": sp, "color": T["tps"], "vmax": None, "floor": 10, "label": "Speed",
                                         "now": "%s tok/s" % D.num(last_value(sp), "%.0f") if last_value(sp) is not None else ""}],
              "")


def draw_storage(pm, r, st, ctx):
    inner = frame(pm, r, "Storage and network")
    x, y, w, h = inner
    disks = [d for d in _l(st.get("disks")) if isinstance(d, dict)]
    raid = [a for a in _l(st.get("raid")) if isinstance(a, dict)]
    io, net = _d(st.get("io")), _d(st.get("net"))
    rows = []
    for d in disks:
        label = str(d.get("mount") or "?")
        total = D._f(d.get("total"))
        if d.get("mounted") and total:
            f = (D._f(d.get("used")) or 0) / total
            rows.append((label, "%s free" % D.human_bytes(d.get("free")), f, col(D.pct_style(100 * f, 80, 92))))
        else:
            rows.append((label, "MISSING", None, T["bad"]))
    # the lines under the disks, most important first; the last ones go when the box is short
    tail = []
    for a in raid[:1]:
        ok = bool(a.get("healthy"))
        txt = "RAID %s %s" % (a.get("name"), "healthy" if ok else "DEGRADED")
        if a.get("action"):
            txt += " %s %s%%" % (a["action"], D.num(a.get("progress"), "%.0f"))
        tail.append(("raid", txt, ok))
    tail.append(("Disk", "read %s  write %s" % (D.rate(io.get("read_bps")), D.rate(io.get("write_bps"))), True))
    tail.append(("Net", "in %s  out %s" % (D.rate(net.get("rx_bps")), D.rate(net.get("tx_bps"))), True))
    ports = [p for p in _l(net.get("ports")) if isinstance(p, dict)]
    if ports:
        up = [p for p in ports if p.get("carrier")]
        sp = {D._f(p.get("speed_mbps")) for p in up}
        if len(up) == len(ports) and len(sp) == 1 and None not in sp:
            tail.append(("Link", "%d port%s %d Mb/s" % (len(ports), "" if len(ports) == 1 else "s", sp.pop()), True))
        else:
            tail.append(("Link", "%d of %d ports up" % (len(up), len(ports)), False))
    while tail and 13 * len(rows) - 6 + 3 + 12 * len(tail) > h:        # every disk first, then the lines under them
        tail.pop()
    keep = max(1, (h - 3 - 12 * len(tail) + 6) // 13)
    if len(rows) > keep:
        rows.sort(key=lambda r: 0 if r[1] == "MISSING" else 1)           # a missing disk is never the one left out
        more = len(rows) - keep + 1
        rows = rows[:keep - 1] + [("+%d more" % more, "", None, T["dim"])]
    used = bar_rows(pm, (x, y, w, h), rows)
    for i, row in enumerate(rows):
        if row[1] == "MISSING":
            pm.text_right(x + w, y + i * 13, row[1], T["bad"], max_w=w // 2)
    yy = y + used + 3
    lw = widest([t[0] for t in tail if t[0] != "raid"])
    for label, text, ok in tail:
        if yy + 8 > y + h:
            break
        if label == "raid":
            dot(pm, x, yy, T["ok"] if ok else T["bad"])
            pm.text(x + 10, yy, text, T["text"] if ok else T["bad"], max_w=w - 10)
        else:
            pm.text(x, yy, label, T["dim"], max_w=lw)
            pm.text(x + lw + 6, yy, text, T["text"] if ok else T["warn"], max_w=w - lw - 6)
        yy += 12


def sleep_summary(st, now):
    """What the sleep line says: (text, style). Uses the activity file the
    gateway writes (the last real work and the requests in flight) and, when
    the idle service has published them, whether auto sleep is on and after
    how long."""
    act, idle = _d(st.get("activity")), _d(st.get("idle"))
    last, at = D._f(act.get("last")), D._f(act.get("at"))
    inflight = act.get("inflight") if isinstance(act.get("inflight"), int) else None
    fresh = at is not None and now - at <= 120
    if idle.get("supported") is False:
        return "No deep sleep on this machine", "dim"
    enabled = idle.get("enabled") if isinstance(idle.get("enabled"), bool) else None
    minutes = D._f(idle.get("minutes"))
    if enabled is False:
        return "Auto sleep is off", "dim"
    if fresh and inflight:
        return "Awake: request running", "ok"
    if last and fresh:
        idle_s = max(0, now - last)
        if enabled and minutes:
            left = minutes * 60 - idle_s
            if left > 0:
                return "Sleeps in %d:%02d (idle %s)" % (left // 60, left % 60, D.dur(idle_s)), "warn" if left < 300 else "text"
            return "Idle long enough: may sleep now", "warn"
        return "Idle for %s" % D.dur(idle_s), "text"
    return "Awake", "text"


def draw_status(pm, r, st, ctx):
    warns = ctx["warns"]
    bad = any(s == "bad" for _, s in warns)
    inner = frame(pm, r, "Status", accent=T["bad"] if bad else (T["warn"] if warns else None))
    x, y, w, h = inner
    now = ctx["now"]
    yy = y
    sleep_txt, sleep_style = sleep_summary(st, now)
    sleep_h = len(wrap(sleep_txt, w - 10, 2)) * 10 + 3
    if not warns:
        dot(pm, x, yy + 3, T["ok"])
        pm.text(x + 12, yy, "ALL CLEAR", T["ok"], 2, max_w=w - 12)
        yy += 19
    else:
        budget = max(1, (h - sleep_h - 12) // 11)
        shown = warns[:budget] if len(warns) <= budget else warns[:max(1, budget - 1)]
        for text, style in shown:
            lines = wrap(text, w - 11, 2)
            dot(pm, x, yy, col(style))
            for ln in lines:
                pm.text(x + 11, yy, ln, col(style), max_w=w - 11)
                yy += 10
            yy += 1
        if len(shown) < len(warns):
            pm.text(x, yy, "+%d more to check" % (len(warns) - len(shown)), T["warn"], max_w=w)
            yy += 11
    yy += 2
    if yy + 9 <= y + h:
        pm.hline(x, yy - 2, w, T["edge"])
    if yy + 9 <= y + h:
        pm.text(x, yy, "SLEEP", T["title"], max_w=w)
        yy += 11
        for ln in wrap(sleep_txt, w, max(1, (y + h - yy + 3) // 10)):
            if yy + 7 > y + h:
                break
            pm.text(x, yy, ln, col(sleep_style), max_w=w)
            yy += 10
    up = _d(st.get("updates"))
    t = _d(st.get("tunnel"))
    gw = _d(st.get("gw"))
    extra = [("Gateway", "running" if gw and not gw.get("stale") else "DOWN", "ok" if gw and not gw.get("stale") else "bad")]
    extra.append(("Tunnel", ("up (%d)" % (D._f(t.get("connections")) or 0)) if t.get("up") else "DOWN",
                  "ok" if t.get("up") else "bad"))
    if D._f(up.get("last_unattended")):
        extra.append(("Updated", D.ago(up.get("last_unattended"), now), "text"))
    pw = _d(st.get("power"))
    if D._f(pw.get("watts")) is not None:
        extra.append(("Power", "%s W" % D.num(pw.get("watts"), "%.0f"), "text"))
    lw = widest([e[0] for e in extra])
    yy += 2
    for label, text, style in extra:
        if yy + 8 > y + h:
            break
        pm.text(x, yy, label, T["dim"], max_w=lw)
        pm.text(x + lw + 6, yy, text, col(style), max_w=w - lw - 6)
        yy += 11


def draw_header(pm, r, st, ctx):
    x, y, w, h = r
    pm.fill_rect(x, y, w, h, T["head"])
    pm.hline(x, y + h - 1, w, T["edge"])
    warns, now = ctx["warns"], ctx["now"]
    if any(s == "bad" for _, s in warns):
        badge, bc = "%d PROBLEM%s" % (len(warns), "" if len(warns) == 1 else "S"), T["bad"]
    elif warns:
        badge, bc = "%d TO CHECK" % len(warns), T["warn"]
    else:
        badge, bc = "ALL CLEAR", T["ok"]
    sc = 1
    bw = F.text_width(badge, sc) + 14
    bh = 14
    bx, by = w - bw - 6, (h - 1 - bh) // 2
    pm.rrect(bx, by, bw, bh, 7, bc)
    pm.text_center(bx + bw // 2, by + 4, badge, 0x07101a, sc, max_w=bw - 4)
    name = str(st.get("host") or "server")
    nscale = 2 if h >= 22 else 1
    nm_room = bx - 24
    nw = pm.text(10, (h - 1 - 7 * nscale) // 2, name, T["text"], nscale, max_w=nm_room // 2 + 40)
    try:
        clock = datetime.datetime.fromtimestamp(now).astimezone().strftime("%a %d %b  %H:%M:%S") if now else ""
    except (ValueError, OverflowError, OSError):
        clock = ""
    room = bx - (10 + nw + 14) - 8
    ty = (h - 1 - 7) // 2
    cx = 10 + nw + 18
    if clock and F.text_width(clock) <= room:
        pm.text(cx, ty, clock, T["dim"])
        cx += F.text_width(clock) + 14
        room = bx - cx - 8
    upt = "up %s" % D.dur(st.get("uptime"))
    if F.text_width(upt) <= room:
        pm.text(cx, ty, upt, T["dim"])


def draw_footer(pm, r, st, ctx):
    x, y, w, h = r
    pm.fill_rect(x, y, w, h, T["head"])
    pm.hline(x, y, w, T["edge"])
    net = _d(st.get("net"))
    addr = str(net.get("address") or "").split("/")[0]
    parts = []
    if addr:
        parts.append(("Address", addr))
    ver = D.dig(st, "updates", "ollama", "version") or D.dig(st, "gw", "ollama_version")
    if ver:
        parts.append(("Ollama", str(ver)))
    pw = _d(st.get("power"))
    right = ""
    if D._f(pw.get("kwh_24h")) is not None:
        right = "%s kWh in 24 h" % D.num(pw.get("kwh_24h"), "%.2f")
        if D._f(pw.get("cost_24h")) is not None:
            right += "  %s%.2f" % (pw.get("symbol") or "$", pw["cost_24h"])
    rw = F.text_width(right)
    ty = y + (h - 7) // 2 + 1
    cx = 10
    room = w - 20 - (rw + 12 if right else 0)
    for label, val in parts:
        seg = F.text_width(label) + 5 + F.text_width(val)
        left = 10 + room - cx
        if seg > left:                                  # cut the last one that fits at all
            lw_ = F.text_width(label) + 5
            if left > lw_ + 20:
                pm.text(cx, ty, label, T["dim"])
                pm.text(cx + lw_, ty, val, T["text"], max_w=left - lw_)
            break
        pm.text(cx, ty, label, T["dim"])
        pm.text(cx + F.text_width(label) + 5, ty, val, T["text"])
        cx += seg + 16
    mid = "graphs: last %s" % ctx["rng"]
    mw = F.text_width(mid)
    mx = (w - mw) // 2                       # in the middle if there is room between the other two
    if cx + 8 <= mx and mx + mw + 12 <= w - 10 - (rw if right else 0):
        pm.text(mx, ty, mid, T["dim"])
    if right:
        pm.text_right(w - 10, ty, right, T["dim"], max_w=max(0, w - cx - 20))


# ---- the pairing screen ---------------------------------------------------------------

def draw_pairing(pm, st, now):
    """The pairing code as large as the screen allows, with the time left."""
    from o1auth import format_code
    w, h = pm.w, pm.h
    win = _d(st.get("pairing"))
    code = format_code(win.get("code", ""))
    left = max(0, int((D._f(win.get("expires_at")) or now) - now))
    pm.fill_rect(0, 0, w, h, 0x0b2a4a)
    pm.rrect(8, 8, w - 16, h - 16, 6, 0x1d6fd1)
    pm.rrect(10, 10, w - 20, h - 20, 5, 0x0d1b2e)
    pm.text_center(w // 2, 20, "PAIRING WINDOW OPEN", 0x6fd3ff, 2, max_w=w - 40)
    # the biggest whole scale that fits the code across the screen
    scale = 1
    for s in range(12, 0, -1):
        if F.text_width(code, s) <= w - 40 and 7 * s <= h - 120:
            scale = s
            break
    cy = max(56, (h - 7 * scale) // 2 - 6)
    pm.text_center(w // 2, cy, code, T["text"], scale, max_w=w - 24)
    ty = cy + 7 * scale + 14
    pm.text_center(w // 2, ty, "closes in %d:%02d" % (left // 60, left % 60), T["warn"] if left < 60 else T["ok"], 2, max_w=w - 24)
    total = max(1, int(D._f(win.get("ttl")) or 300))
    bx, bw = 40, w - 80
    bar(pm, bx, ty + 24, bw, 6, min(1.0, left / float(total)), T["warn"] if left < 60 else T["ok"])
    pm.text_center(w // 2, ty + 40, "Type this code in ConcordeAI on the device you are pairing.", T["dim"], 1, max_w=w - 24)
    name = str(st.get("host") or "")
    addr = str(_d(st.get("net")).get("address") or "").split("/")[0]
    foot = "  ".join(x for x in (name, addr) if x)
    if foot:
        pm.text_center(w // 2, h - 28, foot, T["title"], 1, max_w=w - 24)


# ---- the whole frame ----------------------------------------------------------------

def render(st, w, h, range_s=300, pm=None):
    """The whole panel as a Pixmap of w x h (drawn into `pm` if one is
    given: the caller keeps one buffer and the frame is repainted over it)."""
    st = st if isinstance(st, dict) else {}
    if pm is None or (pm.w, pm.h) != (w, h):
        pm = o1gfx.Pixmap(w, h, T["bg"])
    else:
        pm.fill_rect(0, 0, w, h, T["bg"])
    now = D._f(st.get("time")) or 0
    boxes = layout(w, h)
    if not boxes:
        pm.text(8, 8, "Screen too small for the panel", T["warn"], 1, max_w=w - 16)
        return pm
    warns = D.warnings(st, now)
    ctx = {"now": now, "range_s": range_s, "rng": "5 min" if range_s <= 300 else "1 h", "warns": warns}
    for name, fn in (("header", draw_header), ("gpu", draw_gpu), ("cpu", draw_cpu), ("models", draw_models),
                     ("storage", draw_storage), ("status", draw_status), ("footer", draw_footer)):
        r = boxes[name]
        with pm.clipped(*r):
            try:
                fn(pm, r, st, ctx)
            except Exception as e:                   # one bad reading must not blank the screen
                ERRORS.append("%s: %r" % (name, e))
                pm.fill_rect(*r, T["panel"])
                pm.text(r[0] + 8, r[1] + 8, "%s: no data" % name, T["dim"], max_w=r[2] - 16)
    if st.get("pairing"):
        try:
            draw_pairing(pm, st, now)
        except Exception as e:
            ERRORS.append("pairing: %r" % (e,))
    return pm
