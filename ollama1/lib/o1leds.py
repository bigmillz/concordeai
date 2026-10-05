"""Lights that follow the graphics card's load (6b417), cool down slowly and dim after 5 idle
minutes (6b419): every RGB device OpenRGB lists (on this server: the motherboard's Mystic
Light and the AIO cooler's pump head) is held at the same colour.

  intensity x = the card's busy percent / 100 (sysfs gpu_busy_percent, the reading the fan
                service and lib/o1work.py use), sampled every SAMPLE_S (0.25 s)
  displayed   x slew-limited: it rises at most 1.0 per RISE_S (2.5 s) and FALLS linearly
              over FALL_S (180 s: the fans' 60 s + 120 s cool-down), so red to white takes
              the whole cool-down, a new burst raises it again from where it is, and a
              reading that jitters 99, 0, 99, 0 stays near red
  colour      piecewise linear in RGB: 0 white (255,255,255), 1/3 yellow (255,255,0),
              2/3 orange (255,128,0), 1 red (255,0,0); at 0 exactly full white
  brightness  b in 0.5..1.0, multiplied into each channel (white at 0.5 is 128,128,128),
              independent of the colour. The machine is IDLE when the card (6 s average)
              is under 10% and the processors (10 s average of user+nice+system+irq+softirq,
              no iowait; lib/o1work.py's reading) are under 15%; it stops being idle at
              15% (card) or 20% (processors): the gap is the hysteresis. After IDLE_DIM_S
              (300 s) of idle, counted from the moment it became idle (so the 3-minute
              colour fade is part of it), b falls linearly to 0.5 over DIM_S (10 s).
              When it is not idle b goes back up to 1.0 over UNDIM_S (1.5 s), and the 300 s
              start again at the next idle moment. At start, and after a wake, b is 1.0 and
              the 300 s start then.

No card reading (no card, or the read failed): the intensity is 0 (white) and the card counts
as idle. A request in flight or a running tool does not colour or brighten the lights.

Frames go out only when the rounded RGB changes, at most FRAME_S (20 Hz) apart while it
moves; every POLL_S (2 s) the connection is looked at and the colour is sent again (a
keepalive: a device that was reset by a wake or a hot-plug gets it back).

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
STOPS = ((0.0, WHITE), (1 / 3, (255, 255, 0)), (2 / 3, (255, 128, 0)), (1.0, RED))
SAMPLE_S = 0.25                   # the card's busy percent is read this often
RISE_S = 2.5                      # 0 to 100% takes this long
FALL_S = 180.0                    # red to white takes this long: the whole cool-down (60 s + 120 s)
IDLE_DIM_S = 300.0                # this long idle, then dim
DIM_S = 10.0                      # 1.0 down to DIM_MIN takes this long
UNDIM_S = 1.5                     # DIM_MIN up to 1.0 takes this long
DIM_MIN = 0.5
GPU_WAKE_PCT, GPU_IDLE_PCT = o1work.GPU_BUSY_PCT, 10     # card (6 s average): not idle at 15%, idle again under 10%
CPU_WAKE_PCT, CPU_IDLE_PCT = 20, 15                      # processors (10 s average): the same, 20% and 15%
FRAME_S = 0.05                    # at most 20 frames a second while the colour moves
POLL_S = 2                        # the connection and the keepalive
STATUS_STALE_S = 15               # a status file older than this: the service isn't running
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

def ramp(x):
    """The colour for intensity x (0 to 1): piecewise linear in RGB between STOPS."""
    x = 0.0 if x != x else min(1.0, max(0.0, x))
    for (x0, c0), (x1, c1) in zip(STOPS, STOPS[1:]):
        if x <= x1:
            f = (x - x0) / (x1 - x0)
            return tuple(int(round(a + (b - a) * f)) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def color_name(x):
    return "white" if x < 1 / 6 else "yellow" if x < 0.5 else "orange" if x < 5 / 6 else "red"


def scale(c, b):
    """The colour with its brightness: each channel times b, rounded."""
    return tuple(int(round(v * b)) for v in c)


class Slew:
    """The displayed intensity: it follows the target no faster than RISE_S / FALL_S allow."""

    def __init__(self):
        self.x = 0.0

    def step(self, dt, target):
        if target > self.x:
            self.x = min(target, self.x + dt / RISE_S)
        else:
            self.x = max(target, self.x - dt / FALL_S)
        return self.x


def hex_of(c):
    return "%02X%02X%02X" % tuple(c)


def status_path():
    return o1common.p("/run/ollama1/leds.json")


class Leds:
    """One tick() per wake-up; it returns how long to sleep. `probes` as in lib/o1work.py (only
    gpu_busy is read); `clock` is monotonic; `wall` stamps the status file."""

    def __init__(self, probes, client=None, clock=time.monotonic, wall=time.time, log=print, poll_s=POLL_S,
                 status=True):
        self.probes = probes
        self.client = client or Client()
        self.clock, self.wall, self.log = clock, wall, log
        self.poll_s, self.status = poll_s, status
        self.slew = Slew()
        self.gpu = None                           # the last reading, or None: no reading
        self.target = 0.0
        self.last, self.next_sample = None, 0.0
        self.next_poll, self.retry_at, self.backoff = 0.0, 0.0, RETRY_FIRST_S
        self.sent, self.err, self.seen_name = None, None, None
        self.resync = False
        self.bright = 1.0
        self.reset_idle(self.clock())

    def reset_idle(self, now):
        """Start (and a wake): full brightness, idle, the 300 s from now; the averages begin again."""
        self.bright = 1.0
        self.idle, self.idle_since = True, now
        self.work = o1work.Work({"gpu_busy": lambda: self.gpu, "cpu": self.probes.get("cpu", lambda: None)},
                                self.clock)
        self.gpu_avg = self.cpu_avg = None

    def judge_idle(self, now):
        """Hysteresis on the two averages: busy at 15% card or 20% processors, idle again under 10% and 15%."""
        w = self.work
        self.gpu_avg, self.cpu_avg = w._gpu(now), w._cpu(now)
        g = self.gpu_avg if self.gpu_avg is not None else (0.0 if self.gpu is None else None)   # no card: idle
        c = self.cpu_avg
        if (g is not None and g >= GPU_WAKE_PCT) or (c is not None and c >= CPU_WAKE_PCT):
            if self.idle:
                self.idle = False
        elif g is not None and c is not None and g < GPU_IDLE_PCT and c < CPU_IDLE_PCT:
            if not self.idle:
                self.idle, self.idle_since = True, now

    def idle_s(self, now):
        return now - self.idle_since if self.idle else 0.0

    def read_gpu(self):
        try:
            g = self.probes["gpu_busy"]()
        except Exception:
            return None
        return float(g) if isinstance(g, (int, float)) and not isinstance(g, bool) and g == g else None

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
        if self.resync:
            self.reset_idle(now)                  # a wake: full brightness, the 5 minutes start again
        if now >= self.next_sample:
            self.next_sample = now + SAMPLE_S
            self.gpu = self.read_gpu()
            self.target = 0.0 if self.gpu is None else min(1.0, max(0.0, self.gpu / 100.0))
            self.judge_idle(now)
        polled = now >= self.next_poll or self.resync          # a wake does not wait for the poll
        if polled:
            self.next_poll = now + self.poll_s
            self.link(now)
        dt = 0.0 if self.last is None else min(max(now - self.last, 0.0), 2 * SAMPLE_S)
        self.last = now
        self.slew.step(dt, self.target)
        dimming = self.idle and self.idle_s(now) >= IDLE_DIM_S
        if dimming:
            self.bright = max(DIM_MIN, self.bright - dt * (1.0 - DIM_MIN) / DIM_S)
        else:
            self.bright = min(1.0, self.bright + dt * (1.0 - DIM_MIN) / UNDIM_S)
        rgb = scale(ramp(self.slew.x), self.bright)
        if self.client.connected and (rgb != self.sent or polled):
            try:
                self.client.show(rgb)
                self.sent = rgb
            except OSError as e:
                self.lost(e, now)
        name = color_name(self.slew.x)
        if polled or name != self.seen_name:
            if name != self.seen_name:
                self.log("%s%s" % (name, " (card %d%% busy)" % round(self.gpu) if self.gpu is not None else
                                   " (no card reading)"))
            self.seen_name = name
            self.write_status(now)
        fast = self.slew.x < self.target or self.bright != (DIM_MIN if dimming else 1.0)
        if fast:
            return FRAME_S                        # rising, dimming or brightening: 20 Hz
        return max(0.0, min(self.next_sample, self.next_poll) - now)     # a slow fall changes a level a tick

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
        st = {"at": int(self.wall()), "state": color_name(self.slew.x),
              "rgb": list(scale(ramp(self.slew.x), self.bright)), "brightness": round(self.bright, 3),
              "idle": self.idle, "idle_s": int(self.idle_s(now)),
              "gpu_avg": None if self.gpu_avg is None else round(self.gpu_avg, 1),
              "cpu_avg": None if self.cpu_avg is None else round(self.cpu_avg, 1),
              "target_rgb": list(ramp(self.target)), "gpu_pct": None if self.gpu is None else round(self.gpu, 1),
              "gpu_reading": self.gpu is not None, "intensity": round(self.slew.x, 3),
              "target_intensity": round(self.target, 3),
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
    pct = st.get("gpu_pct")
    b = st.get("brightness", 1.0)
    idle_min = int(st.get("idle_s", 0) / 60.0 + 0.5)
    if b <= DIM_MIN:
        how = "dimmed %d%% (idle %d min)" % (round(b * 100), idle_min)
    elif b < 1.0 and st.get("idle"):
        how = "dimming (idle %d min)" % idle_min
    else:
        how = "%d%% (%s)" % (round(b * 100), "card %d%% busy" % round(pct) if pct is not None else "no card reading")
    return "Lights: %s, %s  -  %d device%s" % (st.get("state"), how, len(devs), "" if len(devs) == 1 else "s")


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
    tg = st.get("target_rgb") or [0, 0, 0]
    pct = st.get("gpu_pct")
    out = ["lights: %s  now %d,%d,%d (%s)" % (st.get("state", "?"), rgb[0], rgb[1], rgb[2], hex_of(rgb)),
           "target: %d,%d,%d (%s)  -  card: %s  -  shown intensity %s of 1" % (
               tg[0], tg[1], tg[2], hex_of(tg), "%d%% busy" % round(pct) if pct is not None else
               "no reading (taken as 0%)", st.get("intensity", "?")),
           "brightness: %d%%  -  %s (%d s)  -  card avg %s, processors avg %s" % (
               round(st.get("brightness", 1.0) * 100), "idle" if st.get("idle") else "not idle",
               st.get("idle_s", 0), "?" if st.get("gpu_avg") is None else "%d%%" % round(st["gpu_avg"]),
               "?" if st.get("cpu_avg") is None else "%d%%" % round(st["cpu_avg"]))]
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
    notify(("READY=1\n" if first else "") + "WATCHDOG=1\nSTATUS=%s" % color_name(leds.slew.x))
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
