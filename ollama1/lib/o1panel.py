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
import re

import o1dashui as D
import o1gfx
import o1hipix
import o1vecfont as V
import o1vtext as F
import o1tariff

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
MID_MIN = (40, 49, 59)     # the middle column's boxes: storage, fans, network (title, padding and their lines)


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
    top_h = (body - m) * 47 // 100
    needed = sum(MID_MIN) + 2 * m                       # what the three middle boxes need to show everything
    if body - m - top_h < needed:
        top_h = max(min(top_h, 96), body - m - needed)
    bot_h = body - m - top_h
    col_w = (w - 2 * m - m) // 2
    avail = w - 2 * m - 2 * m
    w1 = avail * 40 // 100
    w2 = avail * 32 // 100
    w3 = avail - w1 - w2
    y2 = top + top_h + m
    room = bot_h - 2 * m                                # the three boxes share this (the minimum each, then a third of the rest)
    mins = list(MID_MIN)
    if room < sum(mins):
        mins = [room * v // sum(MID_MIN) for v in MID_MIN]
    spare = max(0, room - sum(mins))
    hs = [mins[0] + spare // 3, mins[1] + spare // 3, 0]
    hs[2] = room - hs[0] - hs[1]
    ys = y2
    mid = {}
    for name, hh in zip(("storage", "fans", "network"), hs):
        mid[name] = (m + w1 + m, ys, w2, hh)
        ys += hh + m
    return {
        "header": (0, 0, w, head_h),
        "gpu": (m, top, col_w, top_h),
        "cpu": (m + col_w + m, top, w - 2 * m - col_w - m, top_h),
        "models": (m, y2, w1, bot_h),
        "storage": mid["storage"],
        "fans": mid["fans"],
        "network": mid["network"],
        "status": (m + w1 + m + w2 + m, y2, w3, bot_h),
        "footer": (0, h - foot_h, w, foot_h),
    }


# ---- helpers on the state -----------------------------------------------------------

def _d(v):
    return v if isinstance(v, dict) else {}


def _l(v):
    return v if isinstance(v, list) else []


GRAPH_STEP_S = 6          # a graph's picture moves this often (seconds, whatever the tick)


def series(st, name, range_s):
    """The samples of the last range_s seconds, plus one step more that a graph that moves in steps drops again.
    A sample is the sampler's tick (D.tick_of), not a second (6b444)."""
    s = _d(st.get("series")).get(name)
    return list(s[-D.samples(st, range_s + GRAPH_STEP_S):]) if isinstance(s, (list, tuple)) else []


def graph_clock(st, now, range_s):
    """{"window": samples a graph shows, "shift": samples since its picture last moved, "graph": which step this is}.
    The picture moves every GRAPH_STEP_S seconds: within a step each new sample only adds to "shift", so the
    samples shown (and the kept picture of the plot) stay the same."""
    tick = D.tick_of(st)
    per_step = max(1, int(round(GRAPH_STEP_S / tick)))
    n = int(now / tick)
    return {"window": D.samples(st, range_s), "shift": n % per_step, "graph": n // per_step}


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


# ---- widgets: each draws inside the box it is given -------------------------------

def frame(pm, r, title, right="", accent=None, right_colour=None, compact=False):
    """A panel: border, fill, title. Returns the inside box, and narrows the
    clip to it (the caller's clipped() block restores the old one)."""
    x, y, w, h = r
    pm.rrect(x, y, w, h, 3, accent if accent is not None else T["edge"])
    pm.rrect(x + 1, y + 1, w - 2, h - 2, 2, T["panel"])
    rw = F.text_width(right) if right else 0
    tw = pm.text(x + 8, y + 5, title.upper(), T["title"], max_w=max(16, w - 16 - (rw + 10 if right else 0)))
    if right:
        room = w - 16 - tw - 10
        if room > 12:
            pm.text_right(x + w - 8, y + 5, right, right_colour if right_colour is not None else T["dim"], max_w=room)
    top, bottom = (15, 4) if compact else (18, 6)       # the middle column's boxes keep their padding small
    inner = (x + 8, y + top, w - 16, max(0, h - top - bottom))
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
    n = f * w if getattr(pm, "hires", False) else int(round(f * w))
    if f > 0 and n < 2:
        n = 2
    if n:
        pm.rrect(x, y, n, h, min(2, h // 2, n / 2.0), color)


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
    f = D._f(f)
    fq = None if f is None else round(max(0.0, min(1.0, f)) * 270) / 270.0       # a degree at a time

    def dial():
        pm.arc(cx, cy, r, r - th, start, start + sweep, T["track"])
        if fq is not None and fq > 0:
            pm.arc(cx, cy, r, r - th, start, start + sweep * fq, color)
    pm.cached(("ring", r, th, color, fq), (cx - r - 1, cy - r - 1, 2 * r + 3, 2 * r + 3), dial)
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


def graph_samples(vals, shift=0, window=None):
    """The samples a graph plots: once there are enough, the newest `shift` are left out (the picture moves
    every GRAPH_STEP_S, not with every sample), then the last `window` (all of them without one)."""
    if shift and len(vals) > shift + 10:
        vals = vals[:len(vals) - shift]
    return vals[-window:] if window else vals


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
    ix, iy, iw, ih = px + 2, py + 2, pw - 4, ph - 4
    if iw < 8 or ih < 8:
        pm.rrect(px, py, pw, ph, 2, T["edge"])
        pm.rrect(px + 1, py + 1, pw - 2, ph - 2, 1, T["plot"])
        return
    traces = []                                    # what the plot shows: one list of (x, y) runs per curve
    for k, c in enumerate(curves):
        pts = D.resample(graph_samples(c["values"], c.get("shift") or 0, c.get("window")), iw)
        seen = [v for v in (D._f(p) for p in pts) if v is not None]
        if not seen:
            continue
        top = c.get("vmax") or nice_max(max(seen) * 1.05, c.get("floor", 1.0))
        runs, run = [], []
        for i, p in enumerate(pts):
            v = D._f(p)
            if v is None:
                if run:
                    runs.append(run)
                run = []
                continue
            run.append((round(ix + i + 0.5, 1), round(iy + ih - 0.5 - max(0.0, min(1.0, v / top)) * (ih - 1), 2)))
        if run:
            runs.append(run)
        traces.append((c["color"], c.get("fill", k == 0), tuple(tuple(r_) for r_ in runs)))

    def plot():
        pm.rrect(px, py, pw, ph, 2, T["edge"])
        pm.rrect(px + 1, py + 1, pw - 2, ph - 2, 1, T["plot"])
        with pm.clipped(ix, iy, iw, ih):
            for q in (1, 2, 3):
                pm.hairline(ix, iy + ih * q // 4, iw, T["grid"], 0.5)
            for color, fill, runs in traces:
                if fill:
                    shade = o1gfx.mix(T["plot"], color, 0.22)
                    for run in runs:
                        if len(run) > 1:
                            pm.fill_under(list(run), iy + ih, shade)
                width = 1.7 if ih >= 28 else 1.1
                for run in runs:
                    pm.polyline(list(run), width, color)
            if not traces:
                pm.text_center(ix + iw // 2, iy + ih // 2 - 3, "no data yet", T["dim"], max_w=iw - 4)
    pm.cached(("plot", tuple(traces), T["edge"]), (px, py, pw, ph), plot)


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
    for c in left + right:
        c["shift"] = ctx.get("shift", 0)
        c["window"] = ctx["window"]
    graph(pm, (x, gy, gw, gh), left, "")
    graph(pm, (x + gw + 6, gy, w - gw - 6, gh), right, "")


def gpu_title(g):
    """The card's name for the box title: "AMD RX 6900 XT" from "Navi 21 [Radeon RX 6900 XT]" or
    "Advanced Micro Devices, Inc. [AMD/ATI] Radeon RX 6900 XT"; the old title when it is unknown."""
    name = str(_d(g).get("name") or "")
    cand = [x for x in re.findall(r"\[([^\]]+)\]", name) if "AMD/ATI" not in x]
    name = cand[-1] if cand else name
    name = re.sub(r"\((R|TM|C)\)|\b(Advanced Micro Devices, Inc\.?|Corporation|Graphics|Series|Laptop GPU)\b|\[AMD/ATI\]", " ", name)
    name = re.sub(r"\bAMD\s+Radeon\b|\bRadeon\b", "AMD", " " + name + " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Graphics card"


def cpu_title(c):
    """The processor's name for the box title: "AMD Ryzen 9 5950X" from "AMD Ryzen 9 5950X 16-Core Processor"."""
    name = str(_d(c).get("model") or "")
    name = re.sub(r"\((R|TM|C)\)", " ", name)
    name = re.sub(r"\b\d+-Core\b|\bProcessor\b|\bCPU\b|\bwith Radeon Graphics\b|@\s*[\d.]+\s*GHz|\bGenuine\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Processor and memory"


MEM_TEMP_MAX = 95.0       # the memory temperature bar is full at this (the card's limit)


def draw_gpu(pm, r, st, ctx):
    g = st.get("gpu")
    if not isinstance(g, dict):
        inner = frame(pm, r, gpu_title(st.get("gpu")), accent=T["bad"])
        pm.text(inner[0], inner[1] + 4, "No graphics card found", T["bad"], 2, max_w=inner[2])
        return
    sclk = D._f(g.get("sclk_mhz"))
    inner = frame(pm, r, gpu_title(g), ("core %d MHz" % sclk) if sclk else "")
    x, y, w, h = inner
    busy = D._f(g.get("busy_pct"))
    vu, vt = D._f(g.get("vram_used")), D._f(g.get("vram_total"))
    pw, cap = D._f(g.get("power_w")), D._f(g.get("power_cap_w"))
    temps = [t for t in (D._f(v) for v in _d(g.get("temps")).values()) if t is not None]
    hot = D._f(D.dig(g, "temps", "junction"))
    hot = hot if hot is not None else (max(temps) if temps else None)
    memt = D._f(D.dig(g, "temps", "mem"))             # the card's memory sensor: already read with the others (6b416)
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
        ("Mem", ("%s\u00b0C" % D.num(memt, "%.0f")) if memt is not None else "-", None if memt is None else memt / MEM_TEMP_MAX,
         col(D.pct_style(memt, 85, 95)) if memt is not None else T["net"]),
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
    inner = frame(pm, r, cpu_title(cpu), ("%d cores" % len(_l(cpu.get("cores")))) if _l(cpu.get("cores")) else "")
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


def _minutes(text):
    """"54m 40s" -> "54m": a countdown that changes once a minute."""
    parts = str(text).split()
    return " ".join(parts[:1]) if len(parts) == 2 and parts[1].endswith("s") and parts[0].endswith("m") else str(text)


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
        right = "%d%% GPU  %sG  %s" % (share, gb(vram), _minutes(D._until(m.get("expires_at"), now)))
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
                                         "shift": ctx.get("shift", 0), "window": ctx["window"],
                                         "now": "%s tok/s" % D.num(last_value(sp), "%.0f") if last_value(sp) is not None else ""}],
              "")


def dec_bytes(b):
    """"188 GB", "1.8 TB": decimal units, the way a disk is sold."""
    b = D._f(b) or 0.0
    return ("%.1f TB" % (b / 1e12)) if b >= 1e12 else ("%d GB" % round(b / 1e9))


def disk_total(disks):
    """(used, total, [mounts that are missing]) over the local filesystems added together; two
    mounts of one filesystem (the same size and free space) count once."""
    seen, used, total, missing = set(), 0.0, 0.0, []
    for d in disks:
        t = D._f(d.get("total"))
        if not d.get("mounted") or not t:
            missing.append(str(d.get("mount") or "?"))
            continue
        key = (t, D._f(d.get("free")), D._f(d.get("used")))
        if key in seen:
            continue
        seen.add(key)
        used += D._f(d.get("used")) or 0.0
        total += t
    return used, total, missing


def draw_storage(pm, r, st, ctx):
    inner = frame(pm, r, "Storage", compact=True)
    x, y, w, h = inner
    disks = [d for d in _l(st.get("disks")) if isinstance(d, dict)]
    io = _d(st.get("io"))
    used, total, missing = disk_total(disks)
    yy = y
    if total:
        f = used / total
        bar_rows(pm, (x, y, w, h), [("Used", "%s of %s" % (dec_bytes(used), dec_bytes(total)), f, col(D.pct_style(100 * f, 80, 92)))], 10)
        yy += 10
    elif not missing:
        pm.text(x, y, "no disk data", T["dim"], max_w=w)
        return
    if missing and yy + 8 <= y + h:                        # a disk that should be there is not: more important than the rates
        parts_line(pm, x, yy, w, [("%s MISSING" % missing[0], T["bad"]), (" +%d more" % (len(missing) - 1) if len(missing) > 1 else "", T["bad"])])
    elif yy + 8 <= y + h:
        parts_line(pm, x, yy, w, [("Read", T["dim"]), (D.rate(io.get("read_bps")), T["text"]),
                                  ("  Write", T["dim"]), (D.rate(io.get("write_bps")), T["text"])])


def net_lines(st, now):
    """The NETWORK box's lines, most important first: [[(text, colour)...]]."""
    n, t = _d(st.get("net")), _d(st.get("tunnel"))
    ports = [p for p in _l(n.get("ports")) if isinstance(p, dict)]
    up = [p for p in ports if p.get("carrier")]
    addr = str(n.get("address") or "").split("/")[0]
    speeds = {D._f(p.get("speed_mbps")) for p in up}
    lines = [[("Down", T["dim"]), (D.rate(n.get("rx_bps")), T["text"]), ("  Up", T["dim"]), (D.rate(n.get("tx_bps")), T["text"])]]
    if n.get("wifi"):
        link = [("Link", T["dim"]), ("on Wi-Fi", T["warn"])]
    elif ports and not up:
        link = [("Link", T["dim"]), ("no cable", T["bad"])]
    else:
        sp = ("%d Mb/s" % list(speeds)[0]) if len(speeds) == 1 and None not in speeds else ""
        link = [("Link", T["dim"]), ("wired " + sp if sp else "wired", T["text"])]
        if ports and len(up) < len(ports):
            link.append(("(%d of %d up)" % (len(up), len(ports)), T["warn"]))
    if addr:
        link.append((addr, T["text"]))
    lines.append(link)
    tun = [("Tunnel", T["dim"]), (("up (%d)" % (D._f(t.get("connections")) or 0)) if t.get("up") else "DOWN", T["ok"] if t.get("up") else T["bad"])]
    bad, drops = int(D._f(n.get("errors")) or 0), int(D._f(n.get("drops")) or 0)
    if bad or drops:
        tun += [("Errors %d  drops %d" % (bad, drops), T["warn"])]
    lines.append(tun)
    if D._f(n.get("rx_total")) is not None:
        lines.append([("Since boot", T["dim"]), ("in " + D.human_bytes(n.get("rx_total")), T["text"]),
                      ("out " + D.human_bytes(n.get("tx_total")), T["text"])])
    return lines


def draw_network(pm, r, st, ctx):
    inner = frame(pm, r, "Network", compact=True)
    x, y, w, h = inner
    for i, parts in enumerate(net_lines(st, ctx["now"])):
        if 10 * i + 8 > h:
            break
        parts_line(pm, x, y + 10 * i, w, parts)


RAID_COLOURS = {"ok": T["ok"], "info": T["gpu"], "warn": T["warn"], "bad": T["bad"]}


def raid_status(a):
    """(text, kind) for the array's line: kind is "ok" (healthy, idle), "info"
    (a scheduled check of a healthy mirror: blue, nothing to do), "warn" (a
    resync, recovery or reshape is running) or "bad" (a member is missing)."""
    name = a.get("name")
    action = a.get("action")
    if not a.get("healthy"):
        txt = "RAID %s DEGRADED" % name
        if action:
            txt += ", " + D.raid_action(a, "%.1f", True)
        return txt, "bad"
    if not action:
        return "RAID %s healthy" % name, "ok"
    return "RAID %s: %s" % (name, D.raid_action(a, "%.1f", True)), "info" if action == "check" else "warn"


FAN_STALE_S = 30


def fan_fresh(st, now):
    fan = _d(st.get("fan"))
    at = D._f(fan.get("at"))
    return at is not None and abs(now - at) <= FAN_STALE_S


def fan_view(st, now):
    """What the FANS box shows, from the fan service's status file
    (/run/ollama1/fan.json): ("none", None) when there is none or it is stale,
    else ("rows", header text, header is a warning, [(label, value, fraction, kind)], coolant line).
    The bar is the output's setting (pwm of 255) and falls back to its rpm against the fastest fan."""
    fan = _d(st.get("fan"))
    if not fan or not fan_fresh(st, now):
        return ("none", None)
    phase, pct = fan.get("phase"), int(D._f(fan.get("pct")) or 0)
    word = {"working": "working", "hot": "working", "calibrating": "measuring", "idle20": "idle",
            "hold100": "cooling down", "hold50": "cooling down", "ramp": "cooling down",
            "deepen": "idle", "deep": "deep idle"}.get(phase, "")
    head = ("%s %d%%" % (word, pct)) if word else ("%d%%" % pct)
    warn = phase == "hot" or bool(fan.get("hot"))
    if warn:
        head = "HOT %d%%" % pct
    outs = [o for o in _l(fan.get("outputs")) if isinstance(o, dict)]
    top_rpm = max([D._f(o.get("rpm")) or 0 for o in outs] + [1])

    def bar_of(o):
        rpm, pwm = D._f(o.get("rpm")), D._f(o.get("pwm"))
        return pwm / 255.0 if pwm is not None and o.get("enable") == 1 else (rpm / top_rpm if rpm else None)
    rows, case = [], []                                # case: (bar fraction or None, rpm or None) of every case-type fan
    for o in outs:
        rpm = D._f(o.get("rpm"))
        if o.get("label") == "GPU fan":
            rows.insert(0, ("GPU fan", ("%d rpm" % rpm) if rpm is not None else "-", bar_of(o), "fan"))
        elif o.get("label") == "Pump":                  # the cooler's pump: shown on the cooler line, never averaged in
            continue
        elif rpm or o.get("enable") == 1:
            case.append((bar_of(o), rpm))
    a = _d(fan.get("aio"))
    cool = ""
    if a.get("found") and a.get("state") == "controlling":
        for f in _l(a.get("fans")):                    # a radiator fan on the cooler's own port that reads an rpm counts
            if isinstance(f, dict) and D._f(f.get("rpm")):      # with the case fans, at its own commanded level; a port
                pc = D._f(f.get("pct"))                         # with no rpm is never shown (6b435: no "Cooler fans 100%")
                case.append((pc / 100.0 if pc is not None else None, D._f(f.get("rpm"))))
        parts = []
        pr = D._f(a.get("pump_rpm"))
        if pr is not None:                             # the pump is not averaged in: a line of its own
            parts.append("Pump %d rpm%s" % (pr, (", %s" % a["pump_mode"]) if a.get("pump_mode") else ""))
        if D._f(a.get("coolant_c")) is not None:
            parts.append("coolant %s\u00b0C" % D.num(a.get("coolant_c"), "%.0f"))
        cool = ", ".join(parts)
    if case:
        bars = [b for b, _r in case if b is not None]
        rpms = [r for _b, r in case if r is not None]
        rows.append(("Case fans", ("%d rpm avg" % round(sum(rpms) / len(rpms))) if rpms else "-",
                     sum(bars) / len(bars) if bars else None, "fan"))
    return ("rows", head, warn, rows, cool)


def parts_line(pm, x, y, w, parts):
    """One line of (text, colour) pieces, one after another; when they are wider than w the
    last pieces are left out (never cut in the middle)."""
    parts = [(t, c) for t, c in parts if t]
    while parts and sum(F.text_width(t) for t, _c in parts) + 4 * len(parts) > w:
        parts.pop()
    for t, c in parts:
        pm.text(x, y, t, c)
        x += F.text_width(t) + 4
    return bool(parts)


def draw_fans(pm, r, st, ctx):
    view = fan_view(st, ctx["now"])
    if view[0] == "none":
        inner = frame(pm, r, "Fans", compact=True)
        pm.text(inner[0], inner[1] + 1, "no fan data", T["dim"], max_w=inner[2])
        return
    _k, head, warn, rows, cool = view
    inner = frame(pm, r, "Fans", head, accent=T["warn"] if warn else None, right_colour=T["warn"] if warn else None, compact=True)
    x, y, w, h = inner
    brows = [(label, value, f, T["warn"] if warn else (T["net"] if kind == "fan" else T["gpu2"])) for label, value, f, kind in rows]
    line = 1 if cool else 0
    keep = max(0, (h - 10 * line + 3) // 10)
    if len(brows) > keep:
        brows = brows[:keep]
    used = bar_rows(pm, (x, y, w, h), brows, 10) if brows else 0
    if cool and used + 8 <= h:
        pm.text(x, y + used + 1, F.fit(cool, w), T["dim"], max_w=w)


SLEEP_STALE_S = 180


def sleep_summary(st, now):
    """What the sleep line says: (text, style), from the idle service's own
    last decision in /run/ollama1/idle.json (6b396): sleep_ok, the exact reason
    decide() gave, the idle seconds it counted, and the setting. Nothing here
    is estimated from other files, so it cannot disagree with the service
    (after a wake the service counts from the resume; a request, a tool, a
    block lock or the card keeps it awake and says so)."""
    idle = _d(st.get("idle"))
    at = D._f(idle.get("at"))
    if at is None or now - at > SLEEP_STALE_S or "sleep_ok" not in idle:
        if idle.get("supported") is False and at is not None and now - at <= SLEEP_STALE_S:
            return "No deep sleep on this machine", "dim"
        if idle.get("enabled") is False and at is not None and now - at <= SLEEP_STALE_S:
            return "Auto sleep is off", "dim"
        return "Sleep status unknown", "warn"                 # the service has not said lately
    if idle.get("supported") is False:
        return "No deep sleep on this machine", "dim"
    if idle.get("enabled") is False:
        return "Auto sleep is off", "dim"
    if idle.get("sleep_ok") is True:
        return "Going to sleep now", "warn"
    reason = str(idle.get("reason") or "").strip()
    minutes, idle_s = D._f(idle.get("minutes")), D._f(idle.get("idle_s"))
    if reason.startswith("idle ") and minutes and idle_s is not None:      # only the timer is left
        left = minutes * 60 - idle_s - max(0.0, now - at)
        if left <= 0:
            return "Sleeping very soon", "warn"
        return "Sleeps in %d:%02d" % (left // 60, left % 60), "warn" if left < 300 else "text"
    return (reason[:1].upper() + reason[1:]) if reason else "Awake", "warn"


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


BURN_FRESH_S, BURN_RESULT_S = 10, 60
BURN_NAMES = {"cpu": "processor", "gpu": "graphics card"}


def burn_view(st, now):
    """The burn test as the banner shows it: None (nothing to show), ("run", text) while it runs, or
    ("result", text, kind) for a minute after, kind "ok", "bad" or "warn". From
    /run/ollama1/quickburn.json: a running file the script has not touched for 10 s is a dead script."""
    b = _d(st.get("burn"))
    at = D._f(b.get("at"))
    if not b or at is None or now - at < -5:
        return None
    phase, result = b.get("phase"), str(b.get("result") or "")
    reason = str(b.get("reason") or "").strip()
    if phase in BURN_NAMES and result == "running":
        if now - at > BURN_FRESH_S:
            return None
        parts = ["BURN TEST", "%s %d s left" % (BURN_NAMES[phase], int(D._f(b.get("seconds_left")) or 0))]
        t = D._f(b.get("cpu_c" if phase == "cpu" else "gpu_c"))
        busy = D._f(b.get("cpu_busy" if phase == "cpu" else "gpu_busy"))
        if t is not None:
            parts.append("%d\u00b0C" % t)
        if busy is not None:
            parts.append("%d%% busy" % busy)
        return ("run", "  ".join(parts))
    if phase == "done" and now - at <= BURN_RESULT_S:
        if result == "passed":
            return ("result", ("Burn test: " + reason) if reason else "Burn test passed", "warn" if reason else "ok")
        if result == "failed":
            return ("result", "Burn test failed: " + (reason or "see the log"), "bad")
        if result == "aborted":
            return ("result", "Burn test aborted" + ((": " + reason) if reason and reason != "stopped from the keyboard" else ""), "warn")
        if result == "refused":
            return ("result", "Not started: " + (reason or "the server is busy"), "warn")
    return None


def burn_running(st, now):
    v = burn_view(st, now)
    return bool(v and v[0] == "run")


BANNER_FILL = {"run": 0x12325c, "ok": 0x0f3d2a, "bad": 0x4a1417, "warn": 0x4a3410}
BANNER_TEXT = {"run": 0xcfe3ff, "ok": T["ok"], "bad": T["bad"], "warn": T["warn"]}


def draw_banner(pm, r, view):
    x, y, w, h = r
    kind = "run" if view[0] == "run" else view[2]
    pm.fill_rect(x, y, w, h, BANNER_FILL[kind])
    pm.hline(x, y + h - 1, w, T["edge"])
    text = view[1]
    scale = 1
    for sc in (2, 1.6, 1.3, 1):
        scale = sc
        if F.text_width(text, sc) <= w - 20:
            break
    pm.text_center(x + w // 2, y + (h - 1 - 7 * scale) / 2.0, text, BANNER_TEXT[kind], scale, max_w=w - 12)


def draw_header(pm, r, st, ctx):
    x, y, w, h = r
    banner = burn_view(st, ctx["now"])
    if banner:                                          # the burn test takes the top strip while it runs and for a minute after
        draw_banner(pm, r, banner)
        return
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
    hint = "Space: electricity cost"
    free_l, free_r = cx + 8, w - 10 - (rw + 12 if right else 0)
    hint = hint + "    Enter: burn test"
    for mid in ("graphs: last %s    %s" % (ctx["rng"], hint), hint, "Space: cost  Enter: burn", "Enter: burn test"):     # the longest that fits the gap
        mw = F.text_width(mid)
        if free_r - free_l >= mw:
            pm.text(free_l + (free_r - free_l - mw) // 2, ty, mid, T["dim"])
            break
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


# ---- the electricity cost screen (Space on the keyboard, 6b381) ---------------------------

COST_WINDOWS = (("1d", "24 HOURS", 86400), ("1w", "7 DAYS", 7 * 86400), ("1m", "30 DAYS", 30 * 86400))
MSG_NO_PRICE = "Set your electricity price in the admin panel"
MSG_NO_DATA = "No data yet"


def money(value, symbol, currency=None):
    """A cost as text: the tariff's symbol and two decimals (none for yen)."""
    v = D._f(value)
    if v is None:
        return "-"
    return "%s%.*f" % (symbol, 0 if currency == "JPY" else 2, v)


def cost_view(st):
    """What the cost screen shows, from the power summary the root sampler writes
    (the same window figures as the admin panel): ("message", text) when there is
    nothing true to show, else ("rows", [(label, money text, kWh text, estimated,
    note)], any_estimated)."""
    p = _d(st.get("power"))
    now = D._f(st.get("time")) or 0
    if not p:
        return ("message", MSG_NO_DATA)
    ws = _d(p.get("windows"))
    if not ws and D._f(p.get("cost_24h")) is not None:            # a power service from before the cost screen
        ws = {"1d": {"cost": p.get("cost_24h"), "kwh": p.get("kwh_24h"), "measured_h": 1, "est": True}}
    priced = p.get("priced")
    if priced is None:
        priced = any(D._f(w.get("cost")) is not None for w in ws.values() if isinstance(w, dict))
    if not priced:
        return ("message", MSG_NO_PRICE)
    since = D._f(p.get("since"))
    if not ws or not any(D._f(_d(w).get("measured_h")) for w in ws.values()):
        return ("message", MSG_NO_DATA)
    currency = p.get("currency")
    symbol = p.get("symbol") or o1tariff.CURRENCY_SYMBOL.get(currency, "%s " % currency if currency else "$")
    rows, est_any = [], False
    for key, label, secs in COST_WINDOWS:
        w = _d(ws.get(key))
        cost, kwh = D._f(w.get("cost")), D._f(w.get("kwh"))
        if cost is None or not D._f(w.get("measured_h")):
            rows.append((label, "NO DATA", "", False, ""))
            continue
        est = bool(w.get("est"))
        est_any = est_any or est
        note = ""
        if since and now and now - since < 0.9 * secs:
            note = "only %s of data" % D.dur(now - since)
        rows.append((label, money(cost, symbol, currency), "%s kWh" % D.num(kwh, "%.1f" if kwh and kwh >= 10 else "%.2f"), est, note))
    return ("rows", rows, est_any)


def wrap_vec(text, max_w, height):
    """The words in lines no wider than max_w at this height (a word wider
    than the line stands alone)."""
    lines, cur = [], ""
    for wd in text.split():
        trial = (cur + " " + wd).strip()
        if cur and V.text_width(trial, height) > max_w:
            lines.append(cur)
            cur = wd
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def cost_layout(w, h):
    """The three row boxes of the cost screen and the footer line's top:
    ([(x, y, width, height)] x 3, footer y)."""
    m = max(6, h // 45)
    foot_h = max(14, h // 22)
    gap = max(4, h // 60)
    rh = (h - foot_h - 2 * m - 2 * gap) // 3
    return [(m, m + i * (rh + gap), w - 2 * m, rh) for i in range(3)], h - foot_h


_COST_CACHE = {"key": None, "pm": None}


def render_cost(st, w, h):
    """The electricity cost screen as a Pixmap of w x h in the smooth stroke
    font: only the cost over the last 24 hours, 7 days and 30 days, using the
    whole screen. The picture is kept and handed back again until the figures
    (or the size) change, so showing it costs nothing after the first time."""
    view = cost_view(st if isinstance(st, dict) else {})
    key = (w, h, tuple((r[0], r[1], r[2], r[4]) for r in view[1]) if view[0] == "rows" else view)
    if _COST_CACHE["key"] == key:
        return _COST_CACHE["pm"]
    pm = o1gfx.Pixmap(w, h, T["bg"])
    try:
        draw_cost(pm, view)
    except Exception as e:
        ERRORS.append("cost: %r" % (e,))
    _COST_CACHE.update(key=key, pm=pm)
    return pm


def draw_cost(pm, view):
    w, h = pm.w, pm.h
    bg, panel = T["bg"], T["panel"]
    rows_boxes, foot_y = cost_layout(w, h)
    foot_h = h - foot_y
    hint_h = max(8, int(foot_h * 0.55))
    if view[0] == "message":
        text = view[1].upper()
        colour = T["warn"] if view[1] == MSG_NO_PRICE else T["dim"]
        area_w, area_h = w - 2 * rows_boxes[0][0] - w // 20, foot_y - 2 * rows_boxes[0][0]
        best = None
        for lines_n in (1, 2, 3, 4):
            hh = V.fit_height(text, area_w * lines_n, area_h // lines_n)
            while hh >= 4:
                lines = wrap_vec(text, area_w, hh)
                if len(lines) * int(hh * 1.25) <= area_h and all(V.text_width(l, hh) <= area_w for l in lines):
                    break
                hh -= max(1, hh // 20)
            else:
                continue
            if best is None or hh > best[0]:
                best = (hh, lines)
        if best is None:
            best = (4, wrap_vec(text, area_w, 4))
        hh, lines = best
        line_h = int(hh * 1.25)
        y = (foot_y - line_h * len(lines) + (line_h - hh)) // 2
        pm.fill_rect(0, 0, w, foot_y, bg)
        for ln in lines:
            V.draw(pm, (w - V.text_width(ln, hh)) // 2, y, ln, hh, colour, bg)
            y += line_h
    else:
        rows = view[1]
        rh0 = rows_boxes[0][3]
        pad = max(10, rh0 // 12)
        label_h = max(8, rh0 // 7)
        small_h = max(7, rh0 // 12)
        inner = [(x + pad, y + pad, bw - 2 * pad, bh - 2 * pad) for x, y, bw, bh in rows_boxes]
        # a left column for the label, the kWh and (if short of history) a note; the figure takes the rest
        col_w = max(max(V.text_width(r[0], label_h), V.text_width(r[2].upper(), small_h)) for r in rows)
        col_w = max(col_w, (inner[0][2]) // 8)
        figure_w = inner[0][2] - col_w - pad * 2
        vh = None
        for (ix, iy, iw, ih), r in zip(inner, rows):
            if r[1] in ("NO DATA", "-"):
                continue
            f = V.fit_height(r[1], figure_w, ih)
            vh = f if vh is None else min(vh, f)
        vh = vh or max(8, inner[0][3] // 2)
        for (bx, by, bw, bh), (ix, iy, iw, ih), (label, value, kwh, _est, note) in zip(rows_boxes, inner, rows):
            pm.rrect(bx, by, bw, bh, max(4, bh // 12), T["edge"])
            pm.rrect(bx + 2, by + 2, bw - 4, bh - 4, max(3, bh // 12 - 2), panel)
            V.draw(pm, ix, iy, label, label_h, T["title"], panel)
            ty = iy + label_h + max(6, label_h // 2)
            if kwh:
                V.draw(pm, ix, ty, kwh.upper(), small_h, T["dim"], panel)
                ty += small_h + max(6, small_h // 2)
            if note:
                for ln in wrap_vec(note.upper(), col_w, small_h)[:3]:
                    if ty + small_h > iy + ih:
                        break
                    V.draw(pm, ix, ty, ln, small_h, T["warn"], panel)
                    ty += small_h + max(4, small_h // 3)
            dim = value in ("NO DATA", "-")
            h_ = V.fit_height(value, figure_w, ih, top=vh) if dim else min(vh, V.fit_height(value, figure_w, ih))
            vw = V.text_width(value, h_)
            fx = ix + col_w + pad
            V.draw(pm, fx + (figure_w - vw) // 2, V.ink_top(value, h_, iy, ih), value, h_,
                   T["dim"] if dim else T["text"], panel)
    V.draw(pm, w - 2 * 8 - V.text_width("SPACE: BACK", hint_h), foot_y + (foot_h - hint_h) // 2, "SPACE: BACK", hint_h,
           T["dim"], bg)


# ---- the whole frame ----------------------------------------------------------------

class PanelRenderer:
    """The panel drawn at `scale` times its logical size (a 6 draws it on a
    3840x2160 screen at 3840x2160). It keeps its surface between frames and
    with incremental=True redraws only the boxes whose numbers changed (and
    the dials and graphs inside them come from pictures it kept), so a quiet
    machine costs almost nothing to show."""

    BOXES = (("header", "draw_header"), ("gpu", "draw_gpu"), ("cpu", "draw_cpu"), ("models", "draw_models"),
             ("storage", "draw_storage"), ("fans", "draw_fans"), ("network", "draw_network"),
             ("status", "draw_status"), ("footer", "draw_footer"))

    def __init__(self, w, h, scale=1, pm=None):
        self.w, self.h, self.scale = w, h, scale
        self.pm = pm if (pm is not None and getattr(pm, "hires", False) and (pm.w, pm.h, pm.S) == (w, h, scale)) \
            else o1hipix.HiPixmap(w, h, T["bg"], scale)
        self.keys = {}
        self.overlay = True

    def box_key(self, name, st, ctx):
        """What a box shows, as something comparable: its numbers (not the long
        series, only their tails), the minute for countdowns, the graph step."""
        def tail(*names):
            return tuple(last_value(series(st, n, 5)) for n in names)
        g = (ctx["graph"], ctx["shift"])
        if name == "header":
            return (st.get("host"), int(ctx["now"]), D.dur(st.get("uptime")), repr(ctx["warns"]), repr(st.get("burn")))
        if name == "footer":
            return (repr(_d(st.get("net")).get("address")), repr(D.dig(st, "updates", "ollama")),
                    repr(_d(st.get("power"))), ctx["rng"], repr(D.dig(st, "gw", "ollama_version")))
        if name == "gpu":
            return (repr(st.get("gpu")), tail("gpu_busy", "vram_used", "gpu_power", "gpu_temp"), g)
        if name == "cpu":
            return (repr(st.get("cpu")), repr(st.get("mem")), repr(st.get("ollama_cg")),
                    tail("cpu_total", "cpu_temp", "cpu_mhz", "ram_used"), g)
        if name == "models":
            return (repr(st.get("gw")), tail("tps"), g, int(ctx["now"] // 60))
        if name == "storage":
            return (repr(st.get("disks")), repr(st.get("io")))
        if name == "network":
            return (repr(st.get("net")), repr(st.get("tunnel")))
        if name == "fans":
            return (repr(st.get("fan")), fan_fresh(st, ctx["now"]))
        return (repr(ctx["warns"]), sleep_summary(st, ctx["now"]), repr(st.get("tunnel")), repr(st.get("updates")),
                repr(st.get("power")), repr(st.get("gw") and bool(_d(st.get("gw")).get("stale"))))

    def draw(self, st, range_s=300, incremental=False):
        pm, w, h = self.pm, self.w, self.h
        st = st if isinstance(st, dict) else {}
        now = D._f(st.get("time")) or 0
        boxes = layout(w, h)
        full = not incremental or self.overlay
        if full:
            pm.fill_rect(0, 0, w, h, T["bg"])
            self.keys.clear()
        if not boxes:
            pm.text(8, 8, "Screen too small for the panel", T["warn"], 1, max_w=w - 16)
            return pm
        warns = D.warnings(st, now)
        ctx = dict({"now": now, "range_s": range_s, "rng": "5 min" if range_s <= 300 else "1 h", "warns": warns},
                   **graph_clock(st, now, range_s))
        for name, fname in self.BOXES:
            fn = globals()[fname]
            r = boxes[name]
            key = self.box_key(name, st, ctx)
            if not full and self.keys.get(name) == key:
                continue
            self.keys[name] = key
            if not full:
                pm.fill_rect(r[0] - 1, r[1] - 1, r[2] + 2, r[3] + 2, T["bg"])
            with pm.clipped(*r):
                try:
                    fn(pm, r, st, ctx)
                except Exception as e:                   # one bad reading must not blank the screen
                    ERRORS.append("%s: %r" % (name, e))
                    pm.fill_rect(*r, T["panel"])
                    pm.text(r[0] + 8, r[1] + 8, "%s: no data" % name, T["dim"], max_w=r[2] - 16)
        self.overlay = False
        if st.get("pairing"):
            try:
                draw_pairing(pm, st, now)
            except Exception as e:
                ERRORS.append("pairing: %r" % (e,))
            self.overlay = True                          # what was under it is gone: redraw everything afterwards
        return pm


def render(st, w, h, range_s=300, pm=None, screen="panel", scale=1):
    """The whole panel as a smooth surface of w x h logical units (w*scale x
    h*scale pixels; drawn into `pm` if one of that size is given). screen="cost"
    shows only the electricity cost (6b381) at w x h pixels; an open pairing
    window takes the screen from either."""
    st = st if isinstance(st, dict) else {}
    if screen == "cost" and not st.get("pairing"):
        return render_cost(st, w, h)
    return PanelRenderer(w, h, scale, pm).draw(st, range_s)
