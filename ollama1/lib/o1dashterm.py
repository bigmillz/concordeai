"""Getting a rendered frame onto a terminal, and nothing else (6b359).

No curses. curses keeps its own picture of the screen and updates it with
relative cursor moves, insert/delete-line and repeat sequences chosen from
terminfo; on the Linux console, after a font change or a resize, those land
in the wrong place and leave fragments behind. This module instead:

  * is handed a finished Canvas (every row exactly `width` cells);
  * writes each row at an absolute position (ESC[row;1H), so a row can never
    start anywhere but its own line;
  * clears the screen first whenever the size is not the one it last drew
    (or it has drawn nothing yet), so nothing stale can remain;
  * otherwise rewrites only the rows that changed, and every REFRESH_S
    seconds all of them, so a stray glitch heals itself;
  * turns line wrap off, so the last cell of the last row cannot scroll the
    screen, and hides the cursor.

term_size() reads the real size every time it is asked (the tty's own
window size, not a cached one).
"""
import os

CLEAR = "\x1b[2J\x1b[H"
MODES = "\x1b[?7l\x1b[?25l"          # no line wrap, no cursor
RESTORE = "\x1b[?7h\x1b[?25h\x1b[0m"
REFRESH_S = 30.0

# style name -> SGR parameters (the 8 basic colours and bold: all a Linux console has)
SGR = {
    "header": "30;46", "header_ok": "1;30;42", "header_warn": "1;30;43", "header_bad": "1;37;41",
    "footer": "36", "border": "1;34", "title": "1;37", "text": "39", "dim": "36", "value": "1;37",
    "ok": "1;32", "warn": "1;33", "bad": "1;31",
    "c_tps": "1;32", "c_req": "1;36", "c_gpu": "1;35", "c_mem": "1;34", "c_cpu": "1;33", "c_pow": "1;33",
    "c_io": "1;36", "c_io2": "1;35", "c_net": "1;32",
    "overlay": "30;47", "overlay_code": "1;30;47",
}


def encode_row(canvas, y):
    """One row as text with colour codes: runs of one style, cells of "" (a
    wide character's second half) skipped. Always ends by resetting."""
    row, styles = canvas.chars[y], canvas.styles[y]
    out, x, w = [], 0, canvas.w
    while x < w:
        st = styles[x]
        x2 = x + 1
        while x2 < w and styles[x2] == st:
            x2 += 1
        out.append("\x1b[0;%sm" % SGR.get(st, "0") if st in SGR else "\x1b[0m")
        out.append("".join(row[x:x2]))
        x = x2
    out.append("\x1b[0m")
    return "".join(out)


class Screen:
    """Remembers what it last drew; frame() returns the text that makes the
    terminal show a new canvas."""

    def __init__(self, refresh_s=REFRESH_S):
        self.refresh_s = refresh_s
        self.size = None
        self.rows = None
        self.last_full = 0.0

    def frame(self, canvas, now=0.0):
        rows = [encode_row(canvas, y) for y in range(canvas.h)]
        size = (canvas.w, canvas.h)
        full = size != self.size or self.rows is None
        if full:
            out = [CLEAR, MODES]
        elif now - self.last_full >= self.refresh_s:
            out = [MODES]
            full = True
        else:
            out = []
        for y, r in enumerate(rows):
            if full or r != self.rows[y]:
                out.append("\x1b[%d;1H%s" % (y + 1, r))
        if full:
            self.last_full = now
        self.size, self.rows = size, rows
        return "".join(out)

    def forget(self):
        """The next frame starts with a clear (e.g. after another program used the screen)."""
        self.size = self.rows = None


def term_size(fds=(1, 0, 2), default=(80, 24)):
    """The terminal's size right now: (columns, rows)."""
    for fd in fds:
        try:
            sz = os.get_terminal_size(fd)
        except (OSError, ValueError):
            continue
        if sz.columns > 0 and sz.lines > 0:
            return sz.columns, sz.lines
    try:
        c, r = int(os.environ.get("COLUMNS", "")), int(os.environ.get("LINES", ""))
        if c > 0 and r > 0:
            return c, r
    except ValueError:
        pass
    return default
