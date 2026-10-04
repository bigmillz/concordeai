"""The loop behind the graphical panel (6b380): sample, draw, write the changed
rows to the framebuffer, every couple of seconds. Everything it touches is
handed in (the framebuffer, the tty, the sampler, the clock, the sleep), so
the tests run it on a fake screen and a fake clock.

It is meant to be cheap, because the machine is also an AI server: one
sample a second (the charts need a point a second), one picture every
DRAW_S seconds (every second while the pairing window is open, for its
countdown), only the rows that changed are written, and nothing is drawn
while another virtual terminal is on the screen.

Any exception leaves this module after the tty is put back in text mode;
ollama1-dash logs it and falls back to the text dashboard.
"""
import time

import o1fb
import o1panel

TICK_S = 1.0
DRAW_S = 2.0
PAIRING_DRAW_S = 1.0
POLL_S = 0.5
FULL_REFRESH_S = 30.0     # every row again this often: a screen that lost its picture (suspend, a mode change) heals


def run(fb, tty, sampler, vt=None, range_s=300, clock=time.time, sleep=time.sleep,
        stop=lambda: False, max_frames=None, log=None):
    """Draw until stop() is true (or max_frames pictures were drawn, for the
    tests). `tty` is an o1fb.TtyGraphics; `vt` the number of the terminal
    this runs on, so the picture is only drawn while that one is showing.
    Returns the number of frames drawn."""
    info = fb.info
    k, lw, lh = o1fb.choose_scale(info.xres, info.yres)
    if lw < o1fb.MIN_LOGICAL_W:
        raise o1fb.FbError("the screen (%dx%d) is too small for the panel" % (info.xres, info.yres))
    pres = o1fb.Presenter(info, lw, lh, k)
    if log:
        log("graphical panel: %dx%d at %d bits, drawn %dx%d scaled x%d" % (info.xres, info.yres, info.bpp, lw, lh, k))
    tty.enter()
    frames = 0
    try:
        for off, data in pres.clear():
            fb.write(off, data)
        st, pm = None, None
        next_tick = next_draw = 0.0
        last_full = clock()
        shown = True
        while not stop():
            now = clock()
            if st is None or now >= next_tick:
                st = sampler.tick()
                next_tick = now + TICK_S
            active = tty.vt_active() if vt is not None else None
            mine = vt is None or active is None or active == vt
            if mine and (now >= next_draw or not shown):
                if not shown:                    # another terminal was on the screen: start from black
                    for off, data in pres.clear():
                        fb.write(off, data)
                full = not shown or now - last_full >= FULL_REFRESH_S
                if full:
                    last_full = now
                pm = o1panel.render(st, lw, lh, range_s, pm)
                for off, data in pres.frame(pm, force=full):
                    fb.write(off, data)
                next_draw = now + (PAIRING_DRAW_S if st.get("pairing") else DRAW_S)
                frames += 1
                if max_frames is not None and frames >= max_frames:
                    break
            shown = mine
            sleep(POLL_S)
    finally:
        tty.leave()
    return frames
