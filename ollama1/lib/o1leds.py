"""Lights that follow the server's work (6b395): every RGB device OpenRGB lists
(on this server: the motherboard's Mystic Light and the AIO cooler's pump head)
is held at the same colour.

  idle      100% white  (255, 255, 255, brightness at its maximum)
  working   red         (255, 0, 0): faded to over FADE_RED_S (0.8 s)
  ended     red for HOLD_RED_S (3 s: nothing flickers between two requests),
            then faded back to white over FADE_WHITE_S (2 s). The 3 s count from the
            poll that first sees the work has ended, so they are never less than 3 s
            after it did (and at most POLL_S more).

A new request in the middle of a fade turns it round from the colour it has
reached (the position is one number from 0 = white to 1 = red, and the colour
is a function of it: nothing jumps). Frames go out at FRAME_S (20 Hz) only
while a fade runs; otherwise the service sleeps, with a POLL_S (2 s) look at
the probes that also re-sends the colour (a keepalive: a device that was reset
by a wake or a hot-plug gets it again).

"Working" is lib/o1work.py's, the same as the fan service: a request in flight,
a long job, the card at 15% (6 s average) or the processors at 40% (10 s average); no load average.

How it talks to the lights: the sibling unit ollama1-openrgb.service runs
`openrgb --server` (the OpenRGB package; it owns the USB access), bound to
127.0.0.1 only. This service speaks OpenRGB's SDK network protocol to it with
the standard library (little-endian packets "ORGB", device index, packet id,
size; the protocol version is negotiated, capped at PROTO_MAX), so a fade
costs no process per frame. When the server is gone or answers badly the
service says so in its status and tries again with a growing pause; it never
exits for that. It sets each device to Direct mode (else Static, else Custom)
and, where a mode has a brightness, to its maximum; the device list is
re-read when the server says it changed.
"""
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import o1common
import o1fan
import o1work
from o1common import read_json_safe, write_json_atomic

HOST = "127.0.0.1"                # the SDK server is bound here and nowhere else
PORT = 6742
CLIENT_NAME = "ollama1-leds"
WHITE = (255, 255, 255)
RED = (255, 0, 0)
FADE_RED_S = 0.8                  # white to red
FADE_WHITE_S = 2.0                # red to white
HOLD_RED_S = 3.0                  # red this long after the work ends, then back to white
FRAME_S = 0.05                    # 20 Hz while a fade runs
POLL_S = 2                        # the probes and the keepalive; nothing else runs while idle
STATUS_STALE_S = 15               # a status file older than this: the service isn't running
TOOLS_EVERY_S = 6
CONNECT_S = 1.0
IO_S = 2.0
OPEN_BUDGET_S = 10.0              # one whole connect + enumerate may take this long, no more
RETRY_FIRST_S, RETRY_MAX_S = 2, 30
NO_SERVER_WAIT_S = 60             # openrgb missing: wait this long before the unit tries again

# ---- the OpenRGB SDK network protocol ---------------------------------------------------
MAGIC = b"ORGB"
PROTO_MAX = 4                     # the highest version this client understands (segments came in 4)
PID_COUNT, PID_DATA, PID_VERSION, PID_NAME, PID_LIST_UPDATED = 0, 1, 40, 50, 51
PID_UPDATELEDS, PID_UPDATEZONELEDS, PID_UPDATEMODE = 1050, 1051, 1101
MODE_FLAG_BRIGHTNESS, MODE_FLAG_PER_LED, MODE_FLAG_MODE_COLOR = 1 << 4, 1 << 5, 1 << 6
MAX_BODY = 1 << 20
MAX_ITEMS = 4096                  # a count above this in a reply is a broken reply
MODE_PREFERENCE = ("direct", "custom", "static")


class ProtocolError(Exception):
    """The server's bytes make no sense."""


def packet(dev, pid, body=b""):
    return struct.pack("<4sIII", MAGIC, dev, pid, len(body)) + body


def rgb_bytes(c):
    return bytes((c[0] & 255, c[1] & 255, c[2] & 255, 0))


def encode_update_leds(colors):
    """Body of UPDATELEDS: u32 size (of the whole body), u16 count, one R,G,B,0 each."""
    n = len(colors)
    return struct.pack("<IH", 4 + 2 + 4 * n, n) + b"".join(rgb_bytes(c) for c in colors)


def encode_update_zone_leds(zone, colors):
    """Body of UPDATEZONELEDS: u32 size, u32 zone index, u16 count, colours."""
    n = len(colors)
    return struct.pack("<IIH", 4 + 4 + 2 + 4 * n, zone, n) + b"".join(rgb_bytes(c) for c in colors)


def _str(s):
    b = s.encode("utf-8") + b"\0"
    return struct.pack("<H", len(b)) + b


class Reader:
    def __init__(self, data):
        self.b, self.i = data, 0

    def take(self, n):
        if n < 0 or self.i + n > len(self.b):
            raise ProtocolError("short reply")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def count(self):
        n = self.u16()
        if n > MAX_ITEMS:
            raise ProtocolError("a count of %d" % n)
        return n

    def str(self):
        return self.take(self.u16()).rstrip(b"\0").decode("utf-8", "replace")

    def color(self):
        c = self.take(4)
        return (c[0], c[1], c[2])


class Mode:
    FIELDS = ("name", "value", "flags", "speed_min", "speed_max", "bri_min", "bri_max", "colors_min",
              "colors_max", "speed", "brightness", "direction", "color_mode", "colors")

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f, [] if f == "colors" else 0 if f != "name" else ""))

    @classmethod
    def read(cls, r, ver):
        m = cls(name=r.str(), value=r.i32(), flags=r.u32(), speed_min=r.u32(), speed_max=r.u32())
        if ver >= 3:
            m.bri_min, m.bri_max = r.u32(), r.u32()
        m.colors_min, m.colors_max, m.speed = r.u32(), r.u32(), r.u32()
        if ver >= 3:
            m.brightness = r.u32()
        m.direction, m.color_mode = r.u32(), r.u32()
        m.colors = [r.color() for _ in range(r.count())]
        return m

    def encode(self, ver):
        out = [_str(self.name), struct.pack("<iIII", self.value, self.flags, self.speed_min, self.speed_max)]
        if ver >= 3:
            out.append(struct.pack("<II", self.bri_min, self.bri_max))
        out.append(struct.pack("<III", self.colors_min, self.colors_max, self.speed))
        if ver >= 3:
            out.append(struct.pack("<I", self.brightness))
        out.append(struct.pack("<IIH", self.direction, self.color_mode, len(self.colors)))
        out.extend(rgb_bytes(c) for c in self.colors)
        return b"".join(out)

    def copy(self):
        m = Mode(**{f: getattr(self, f) for f in self.FIELDS})
        m.colors = list(self.colors)
        return m


class Device:
    def __init__(self, idx):
        self.idx, self.name, self.vendor, self.nleds = idx, "", "", 0
        self.active, self.modes, self.zones = 0, [], []
        self.mode, self.kind = None, None       # the mode index in use and "leds" / "mode" / None

    @classmethod
    def parse(cls, idx, body, ver):
        r = Reader(body)
        r.u32()                                  # the block's own size
        r.i32()                                  # device type
        d = cls(idx)
        d.name = r.str()
        if ver >= 1:
            d.vendor = r.str()
        r.str(), r.str(), r.str(), r.str()      # description, version, serial, location
        nmodes = r.count()
        d.active = r.i32()
        d.modes = [Mode.read(r, ver) for _ in range(nmodes)]
        for _ in range(r.count()):               # zones
            zname = r.str()
            r.i32()
            r.u32()
            r.u32()
            zleds = r.u32()
            if r.u16() > 0:                      # a matrix: height, width, then the map
                h, w = r.u32(), r.u32()
                r.take(4 * h * w)
            if ver >= 4:
                for _s in range(r.count()):      # segments
                    r.str()
                    r.i32()
                    r.u32()
                    r.u32()
            d.zones.append((zname, zleds))
        d.nleds = r.count()                      # leds: name, value
        for _ in range(d.nleds):
            r.str()
            r.u32()
        return d

    def choose_mode(self):
        """Direct, else Custom, else Static: the first the device has that takes a colour."""
        by = {m.name.lower(): i for i, m in enumerate(self.modes)}
        for name in MODE_PREFERENCE:
            i = by.get(name)
            if i is None:
                continue
            m = self.modes[i]
            if m.flags & MODE_FLAG_PER_LED:
                return i, "leds"
            if m.flags & MODE_FLAG_MODE_COLOR and m.colors_min <= 1 <= max(m.colors_max, 1):
                return i, "mode"
        if 0 <= self.active < len(self.modes) and self.modes[self.active].flags & MODE_FLAG_PER_LED:
            return self.active, "leds"           # none by name: whatever it is in, if it takes per-LED colours
        return None, None


class Client:
    """One connection to the SDK server. Everything raises OSError (the socket) or ProtocolError."""

    def __init__(self, host=HOST, port=PORT, connect=socket.create_connection):
        self.host, self.port, self.connect = host, port, connect
        self.sock, self.ver, self.devices = None, 0, []
        self.list_changed = False

    @property
    def connected(self):
        return self.sock is not None

    def close(self):
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def open(self):
        """Connect, say who we are, agree on a version, list the devices, put them in a mode."""
        self.close()
        deadline = time.monotonic() + OPEN_BUDGET_S
        try:
            self.sock = self.connect((self.host, self.port), timeout=CONNECT_S)
            self.sock.settimeout(IO_S)
            self.sock.sendall(packet(0, PID_NAME, CLIENT_NAME.encode() + b"\0"))
            self.ver = self.negotiate()
            self.enumerate(deadline)
            self.prepare()
        except BaseException:
            self.close()
            raise

    def negotiate(self):
        self.sock.sendall(packet(0, PID_VERSION, struct.pack("<I", PROTO_MAX)))
        try:
            body = self.wait_for(PID_VERSION)
        except socket.timeout:
            return 0                              # a server from before versions: it never answers
        if len(body) < 4:
            raise ProtocolError("short version reply")
        return min(PROTO_MAX, struct.unpack("<I", body[:4])[0])

    def read(self):
        hdr = self.recv_exact(16)
        magic, dev, pid, size = struct.unpack("<4sIII", hdr)
        if magic != MAGIC or size > MAX_BODY:
            raise ProtocolError("not an OpenRGB server")
        return dev, pid, self.recv_exact(size)

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("the server closed the connection")
            buf += chunk
        return buf

    def wait_for(self, pid, dev=None):
        """The body of the next packet with this id; a device-list notice on the way is noted."""
        for _ in range(64):
            d, p, body = self.read()
            if p == PID_LIST_UPDATED:
                self.list_changed = True
            elif p == pid and (dev is None or d == dev):
                return body
        raise ProtocolError("no answer to packet %d" % pid)

    def enumerate(self, deadline=None):
        self.list_changed = False
        self.sock.sendall(packet(0, PID_COUNT))
        body = self.wait_for(PID_COUNT)
        if len(body) < 4:
            raise ProtocolError("short count reply")
        n = struct.unpack("<I", body[:4])[0]
        if n > 256:
            raise ProtocolError("%d devices" % n)
        devs = []
        for i in range(n):
            if deadline is not None and time.monotonic() > deadline:
                raise socket.timeout("the server is too slow")
            self.sock.sendall(packet(i, PID_DATA, struct.pack("<I", self.ver)))
            devs.append(Device.parse(i, self.wait_for(PID_DATA, i), self.ver))
        self.devices = devs

    def prepare(self):
        """Each device into its mode, at the highest brightness it has."""
        for d in self.devices:
            d.mode, d.kind = d.choose_mode()
            if d.kind is None or d.nleds == 0:
                d.kind = None
                continue
            m = d.modes[d.mode]
            if d.active != d.mode or (m.flags & MODE_FLAG_BRIGHTNESS and m.brightness != m.bri_max):
                self.send_mode(d, WHITE)
                d.active = d.mode

    def send_mode(self, d, color):
        m = d.modes[d.mode].copy()
        if m.flags & MODE_FLAG_BRIGHTNESS:
            m.brightness = m.bri_max
        if d.kind == "mode":
            m.colors = [color]
        body = struct.pack("<Ii", 0, d.mode) + m.encode(self.ver)
        body = struct.pack("<I", len(body)) + body[4:]
        self.sock.sendall(packet(d.idx, PID_UPDATEMODE, body))

    def show(self, color):
        """Every device that takes a colour to `color`."""
        for d in self.devices:
            if d.kind == "leds":
                self.sock.sendall(packet(d.idx, PID_UPDATELEDS, encode_update_leds([color] * d.nleds)))
            elif d.kind == "mode":
                self.send_mode(d, color)

    def poll(self):
        """Read anything the server sent (without waiting): a closed connection raises; a changed
        device list is read again. True when the devices changed."""
        while self.sock is not None and select.select([self.sock], [], [], 0)[0]:
            _d, pid, _body = self.read()
            if pid == PID_LIST_UPDATED:
                self.list_changed = True
        if self.list_changed or not self.devices:
            was = [(d.name, d.nleds) for d in self.devices]
            self.enumerate(time.monotonic() + OPEN_BUDGET_S)
            self.prepare()
            return was != [(d.name, d.nleds) for d in self.devices]
        return False


# ---- the colour --------------------------------------------------------------------------

def smooth(x):
    return x * x * (3.0 - 2.0 * x)


class Fader:
    """pos: 0 = white, 1 = red. The colour is a function of pos alone, so a turn round is smooth."""

    def __init__(self):
        self.pos = 0.0

    def step(self, dt, to_red):
        if to_red:
            self.pos = min(1.0, self.pos + dt / FADE_RED_S)
        else:
            self.pos = max(0.0, self.pos - dt / FADE_WHITE_S)

    def rgb(self):
        e = smooth(self.pos)
        return tuple(int(round(w + (r - w) * e)) for w, r in zip(WHITE, RED))

    def state(self):
        return "white" if self.pos <= 0.0 else "red" if self.pos >= 1.0 else "fading"


def hex_of(c):
    return "%02X%02X%02X" % tuple(c)


def status_path():
    return o1common.p("/run/ollama1/leds.json")


class Leds:
    """One tick() per wake-up; it returns how long to sleep. `probes` as in lib/o1work.py;
    `clock` is monotonic; `wall` stamps the status file."""

    def __init__(self, probes, client=None, clock=time.monotonic, wall=time.time, log=print, poll_s=POLL_S,
                 hold_s=HOLD_RED_S, tools_every=TOOLS_EVERY_S, status=True):
        self.work = o1work.Work(probes, clock, tools_every)
        self.client = client or Client()
        self.clock, self.wall, self.log = clock, wall, log
        self.poll_s, self.hold_s, self.status = poll_s, hold_s, status
        self.fader = Fader()
        self.working, self.why, self.last_work = False, [], None
        self.last, self.moving = None, False
        self.next_poll, self.retry_at, self.backoff = 0.0, 0.0, RETRY_FIRST_S
        self.sent, self.err, self.seen_state = None, None, None
        self.resync = False
        self.target_red = False

    # -- the link -----------------------------------------------------------------------
    def lost(self, e, now):
        msg = "%s" % (getattr(e, "strerror", None) or str(e) or type(e).__name__)
        if self.err != msg:
            self.log("OpenRGB: %s (%s)" % (msg, type(e).__name__))
        self.err = msg
        self.client.close()
        self.sent = None
        self.retry_at, self.backoff = now + self.backoff, min(self.backoff * 2, RETRY_MAX_S)

    def link(self, now):
        c = self.client
        if self.resync:
            self.resync = False
            c.close()
            self.retry_at, self.backoff = 0.0, RETRY_FIRST_S
        if c.connected:
            try:
                if c.poll():
                    self.sent = None
                    self.log("devices: " + ", ".join("%s (%d LEDs)" % (d.name, d.nleds) for d in c.devices))
            except (OSError, ProtocolError) as e:
                self.lost(e, now)
        if not c.connected and now >= self.retry_at:
            try:
                c.open()
            except (OSError, ProtocolError) as e:
                self.lost(e, now)
            else:
                self.err, self.backoff, self.sent = None, RETRY_FIRST_S, None
                self.log("connected to OpenRGB (protocol %d): %s" % (
                    c.ver, ", ".join("%s (%d LEDs)" % (d.name, d.nleds) for d in c.devices) or "no devices yet"))

    # -- one tick -----------------------------------------------------------------------
    def tick(self):
        now = self.clock()
        polled = now >= self.next_poll
        if polled:
            self.next_poll = now + self.poll_s
            was = self.working
            self.working, self.why = self.work.working(now)
            if self.working or was:
                self.last_work = now                  # the end counts from the poll that first sees it: never under 3 s
            self.link(now)
        self.target_red = self.working or (self.last_work is not None and now - self.last_work < self.hold_s)
        dt = (now - self.last) if (self.moving and self.last is not None) else 0.0
        self.last = now
        self.fader.step(dt, self.target_red)
        self.moving = self.fader.pos != (1.0 if self.target_red else 0.0)
        rgb = self.fader.rgb()
        if self.client.connected and (rgb != self.sent or polled):
            try:
                self.client.show(rgb)
                self.sent = rgb
            except OSError as e:
                self.lost(e, now)
        label = self.fader.state()
        if label == "fading":
            label += " to red" if self.target_red else " to white"
        if polled or label != self.seen_state:
            if label != self.seen_state:
                self.log(label + (": " + "; ".join(self.why) if self.target_red and self.why else ""))
            self.seen_state = label
            self.write_status(now)
        if self.moving:
            return FRAME_S
        return max(0.0, min(self.poll_s, self.next_poll - now))

    def shutdown(self):
        """A stop must not leave the lights red: white, then the connection closed."""
        if self.client.connected:
            try:
                self.client.show(WHITE)
            except OSError:
                pass
        self.client.close()

    # -- status ---------------------------------------------------------------------------
    def write_status(self, now):
        if not self.status:
            return
        c = self.client
        why = "; ".join(self.why) if self.working else (
            "the work ended; white again in %d s" % max(0, int(self.hold_s - (now - self.last_work) + 0.999))
            if self.target_red and self.last_work is not None else "idle")
        st = {"at": int(self.wall()), "state": self.fader.state(), "target": "red" if self.target_red else "white",
              "pos": round(self.fader.pos, 3), "rgb": list(self.fader.rgb()), "why": why,
              "connected": c.connected, "error": self.err, "protocol": c.ver if c.connected else None,
              "devices": [{"name": d.name, "vendor": d.vendor, "leds": d.nleds,
                           "mode": d.modes[d.mode].name if d.kind else None, "usable": d.kind is not None}
                          for d in c.devices] if c.connected else []}
        st["line"] = status_line(st)
        try:
            os.makedirs(os.path.dirname(status_path()), exist_ok=True)
            write_json_atomic(status_path(), st, mode=0o644)
        except OSError:
            pass


# ---- status text ----------------------------------------------------------------------------

def status_line(st):
    """One line for the admin panel's CPU card and the top of `status`."""
    if not st.get("connected"):
        return "Lights: " + ("OpenRGB: %s" % st["error"] if st.get("error") else "waiting for OpenRGB")
    devs = st.get("devices") or []
    if not devs:
        return "Lights: OpenRGB lists no devices"
    state, target = st.get("state"), st.get("target")
    if state == "fading":
        head = "fading to red" if target == "red" else "fading back to white"
    elif state == "red":
        head = "red" if target == "red" else "red, fading to white soon"
    else:
        head = "white" if target == "white" else "white, going red"
    why = st.get("why")
    return "Lights: %s%s  -  %d device%s" % (head, " (%s)" % why if why and why != "idle" else
                                            " (idle)" if state == "white" else "", len(devs),
                                            "" if len(devs) == 1 else "s")


def read_status(path=None, now=None):
    """The service's status file, or None when it isn't fresh (the service is not running)."""
    st = read_json_safe(path or status_path(), None, max_bytes=16384)
    if not isinstance(st, dict) or not isinstance(st.get("at"), int):
        return None
    if abs((now if now is not None else time.time()) - st["at"]) > STATUS_STALE_S:
        return None
    return st


def panel_line(path=None, now=None):
    """The one status line, or None (no service, nothing to say) for the admin panel."""
    st = read_status(path, now)
    line = st.get("line") if st else None
    return line[:200] if isinstance(line, str) and line else None


def render_status(st):
    """The text of `ollama1-leds status`."""
    if st is None:
        return "lights: the service isn't running (the lights are as the board leaves them)"
    rgb = st.get("rgb") or [0, 0, 0]
    out = ["lights: %s  now %d,%d,%d (%s)" % (st.get("state", "?"), rgb[0], rgb[1], rgb[2], hex_of(rgb)),
           "target: %s  -  why: %s" % (st.get("target", "?"), st.get("why") or "?")]
    devs = st.get("devices") or []
    if st.get("connected"):
        out.append("openrgb: connected to %s:%d (protocol %s), %d device%s found" % (
            HOST, PORT, st.get("protocol"), len(devs), "" if len(devs) == 1 else "s"))
        for d in devs:
            out.append("  %-34s %s, %d LEDs%s" % (d.get("name"), "mode " + d["mode"] if d.get("mode") else "no usable mode",
                                                  d.get("leds", 0), "  (" + d["vendor"] + ")" if d.get("vendor") else ""))
    else:
        out.append("openrgb: not connected (%s:%d)" % (HOST, PORT))
    out.append("errors: " + (st.get("error") or "none"))
    return "\n".join(out)


# ---- the OpenRGB server's command line, and setup ------------------------------------------

def openrgb_argv(run=subprocess.run, which=shutil.which):
    """(argv, bound): `openrgb --server --server-host 127.0.0.1 --server-port 6742`, with the host flag
    only when this build lists it (the unit's address filter keeps it off the network either way)."""
    exe = which("openrgb")
    if not exe:
        return None, False
    try:
        r = run([exe, "--help"], capture_output=True, text=True, timeout=30)
        text = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        text = ""
    bound = "--server-host" in text
    argv = [exe, "--server"] + (["--server-host", HOST] if bound else []) + ["--server-port", str(PORT)]
    return argv, bound


def run_openrgb(log=print, execv=os.execv, sleep=time.sleep, **kw):
    argv, bound = openrgb_argv(**kw)
    if argv is None:
        log("openrgb is not installed (sudo ./setup.sh --leds on installs it); waiting %d s" % NO_SERVER_WAIT_S)
        sleep(NO_SERVER_WAIT_S)
        return 1
    if not bound:
        log("this openrgb has no --server-host: kept off the network by the unit's address filter instead")
    execv(argv[0], argv)
    return 1


def run_systemctl(*args):
    try:
        r = subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_apt_install(package):
    try:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        return subprocess.run(["apt-get", "-y", "-q", "install", package], env=env, timeout=1800).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def setup(choice, systemctl=run_systemctl, which=shutil.which, apt=run_apt_install, log=print):
    """setup.sh's step. on: the openrgb package (only if it isn't there), both units enabled and
    (re)started. off: both stopped (the lights go white as the service stops) and disabled; the
    package and everything else stay."""
    if choice == "on":
        if not which("openrgb") and not apt("openrgb"):
            log("the lights: apt could not install openrgb; left off")
            return 1
        ok = systemctl("enable", "ollama1-openrgb.service", "ollama1-leds.service")
        ok = systemctl("restart", "ollama1-openrgb.service") and ok
        ok = systemctl("restart", "ollama1-leds.service") and ok
        log("the lights: %s" % ("on" if ok else "the services did not start (journalctl -u ollama1-leds -u ollama1-openrgb)"))
        return 0 if ok else 1
    systemctl("disable", "--now", "ollama1-leds.service")
    systemctl("disable", "--now", "ollama1-openrgb.service")
    log("the lights: off (stopped; they stay as they were left, white)")
    return 0


# ---- the service ---------------------------------------------------------------------------

def service_step(leds, notify=o1fan.sd_notify, log=print, first=False):
    """One turn of the loop: a tick, then the watchdog's ping (also when the tick failed: a loop
    that runs is alive; one that hangs is what WatchdogSec is for). Returns the seconds to sleep."""
    wait = POLL_S
    try:
        wait = leds.tick()
    except Exception as e:                                        # never stop; say what kind, not what it held
        log("tick failed: %s" % type(e).__name__)
    notify(("READY=1\n" if first else "") + "WATCHDOG=1\nSTATUS=%s" % leds.fader.state())
    return wait


def run_service(log=print):
    leds = Leds(o1work.probes(), log=log)
    stop, poke = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), poke.set()))
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), poke.set()))

    def wake(*_):                                                 # the sleep hook: the colour again, on a fresh link
        leds.resync = True
        poke.set()
    signal.signal(signal.SIGUSR1, wake)
    first = True
    while not stop.is_set():
        wait = service_step(leds, log=log, first=first)
        first = False
        poke.wait(wait)
        poke.clear()
    leds.shutdown()
    return 0
