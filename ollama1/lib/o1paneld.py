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
import os
import select
import time

import o1fb
import o1panel

try:
    import termios
except ImportError:                      # not a Unix (the tests import this on a Mac: fine)
    termios = None

TICK_S = 1.0
DRAW_S = 2.0
PAIRING_DRAW_S = 1.0
POLL_S = 0.5
FULL_REFRESH_S = 30.0     # every row again this often: a screen that lost its picture (suspend, a mode change) heals


FLIP_DEBOUNCE_S = 0.3     # a held-down key repeats; one flip per press, not a flicker


def handle_keys(data, screen, now, last_flip, debounce=FLIP_DEBOUNCE_S):
    """(screen, time of the last flip) after these keyboard bytes. Space flips
    between the panel and the electricity cost; every other key is ignored."""
    if b" " in data and now - last_flip >= debounce:
        return ("cost" if screen == "panel" else "panel"), now
    return screen, last_flip


class Keys:
    """The console keyboard, read without echo or line editing and without ever
    blocking the drawing loop: wait() sleeps in select() for up to `timeout`
    seconds and returns the bytes typed (b"" if none). start() puts the tty in
    raw mode and throws away what was typed before; stop() restores it. A fd
    that is not a terminal just sleeps."""

    def __init__(self, fd, tcmod=None, sel=select.select, read=os.read, sleep=time.sleep):
        self.fd, self.tc, self.sel, self.read, self.sleep = fd, tcmod or termios, sel, read, sleep
        self.old = None

    def start(self):
        if self.tc is None or self.fd is None:
            return
        try:
            old = self.tc.tcgetattr(self.fd)
            new = self.tc.tcgetattr(self.fd)
            new[3] &= ~(self.tc.ECHO | self.tc.ICANON | self.tc.ISIG | self.tc.IEXTEN)
            new[6][self.tc.VMIN] = 0
            new[6][self.tc.VTIME] = 0
            self.tc.tcsetattr(self.fd, self.tc.TCSANOW, new)
            self.old = old
            self.tc.tcflush(self.fd, self.tc.TCIFLUSH)
        except (OSError, ValueError, self.tc.error):
            self.old = None                  # not a terminal (or one that refuses): no keys, still a panel

    def stop(self):
        if self.old is not None:
            try:
                self.tc.tcsetattr(self.fd, self.tc.TCSANOW, self.old)
            except (OSError, ValueError, self.tc.error):
                pass
            self.old = None

    def wait(self, timeout):
        if self.old is None:
            self.sleep(timeout)
            return b""
        try:
            if self.sel([self.fd], [], [], timeout)[0]:
                return self.read(self.fd, 64)
        except (OSError, ValueError):
            self.sleep(timeout)
        return b""


class _Sleeper:
    def __init__(self, sleep):
        self.sleep = sleep

    def wait(self, timeout):
        self.sleep(timeout)
        return b""


def run(fb, tty, sampler, vt=None, range_s=300, clock=time.time, sleep=time.sleep,
        stop=lambda: False, max_frames=None, log=None, keys=None):
    """Draw until stop() is true (or max_frames pictures were drawn, for the
    tests). `keys` has wait(timeout) -> bytes (see Keys); Space flips to the
    electricity cost screen and back. `tty` is an o1fb.TtyGraphics; `vt` the number of the terminal
    this runs on, so the picture is only drawn while that one is showing.
    Returns the number of frames drawn."""
    info = fb.info
    k, lw, lh = o1fb.choose_scale(info.xres, info.yres)
    if lw < o1fb.MIN_LOGICAL_W:
        raise o1fb.FbError("the screen (%dx%d) is too small for the panel" % (info.xres, info.yres))
    pres = o1fb.Presenter(info, lw, lh, k)
    kc, lwc, lhc = o1fb.choose_cost_scale(info.xres, info.yres)       # the cost screen draws at (about) full resolution
    pres_by = {"panel": pres, "cost": o1fb.Presenter(info, lwc, lhc, kc)}
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
        on_screen = "panel"
        keys = keys or _Sleeper(sleep)
        screen, last_flip = "panel", -1e9
        while not stop():
            now = clock()
            if st is None or now >= next_tick:
                st = sampler.tick()
                next_tick = now + TICK_S
            active = tty.vt_active() if vt is not None else None
            mine = vt is None or active is None or active == vt
            if mine and (now >= next_draw or not shown):
                eff = "panel" if st.get("pairing") else screen          # an open pairing window takes the screen
                if not shown or eff != on_screen:    # another terminal was showing, or the picture's size changes: start from black
                    for off, data in pres_by[eff].clear():
                        fb.write(off, data)
                    on_screen = eff
                    shown = False
                full = not shown or now - last_full >= FULL_REFRESH_S
                if full:
                    last_full = now
                if eff == "cost":
                    pic = o1panel.render_cost(st, lwc, lhc)             # kept until the figures change
                else:
                    pm = pic = o1panel.render(st, lw, lh, range_s, pm, "panel")
                for off, data in pres_by[eff].frame(pic, force=full):
                    fb.write(off, data)
                next_draw = now + (PAIRING_DRAW_S if st.get("pairing") else DRAW_S)
                frames += 1
                if max_frames is not None and frames >= max_frames:
                    break
            shown = mine
            typed = keys.wait(POLL_S)
            if typed:
                new, last_flip = handle_keys(typed, screen, clock(), last_flip)
                if new != screen:
                    screen, next_draw = new, 0.0      # flip at once, not at the next two-second picture
    finally:
        tty.leave()
    return frames
