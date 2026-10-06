"""Lights that follow the graphics card's work (6b417, 6b419; the states of 6b421, per the owner):
every RGB device OpenRGB lists (on this server: the motherboard's Mystic Light and the AIO
cooler's pump head) is held at the same colour.

  idle       white; the brightness is 100% always (6b434: no more dimming). After
             IDLE_DEEP_S (300 s) of idle:
  bluing     white to BLUE over BLUE_S (30 s), a straight line in RGB and in time
  blue       BLUE at 100% while idle (the fans go from 20% to 10% over the same 30 s)
  waking     work while blue or part way: back to white over WAKE_FADE_S (2 s; less from
             part way, the same speed), THEN the rise. Work while white: no such step.
  rising     the card starts working: over RISE_S (5 s) from the colour it has now
             (white after a waking) to red, along the gradient (so through yellow and
             orange) and eased in and out (smoothstep)
  working    red at 100% while the card works
  cooling    the work ended: from red back through orange and yellow to white over
             COOL_S (60 s), linear in time along the gradient: the fans' 60 s ramp
             (lib/o1fan.py), so the two finish together. Work again mid-cool: the 5 s
             rise again, from wherever it is (no white step).

"The card works" is lib/o1work.py's GpuTrigger, the very rule that sends the fans to
100%: busy over 50% (sysfs gpu_busy_percent) for 1.5 s in a row, ending after 1.5 s in
a row at or under 50%; it is sampled every SAMPLE_S (0.25 s), so a 1 s blip does
nothing. The fans' other trigger, the processor at 60 C, does NOT turn the lights red:
the lights follow the card's work only. A request in flight, a running tool or a busy
processor changes nothing; no card reading counts as not working.

  gradient   piecewise linear in RGB: 0 white (255,255,255), 1/3 yellow (255,255,0),
             2/3 orange (255,128,0), 1 red (255,0,0)
  blue       BLUE (0,0,255): white to it is a straight line per channel, rounded

Frames go out only when the rounded RGB changes, at most FRAME_S apart (25 a second)
while anything moves; when nothing moves the loop wakes only for the 0.25 s sample and
every POLL_S (2 s) the connection is looked at and the colour sent again (a keepalive:
a device that was reset by a wake or a hot-plug gets it back). At start, and after a
wake (the sleep hook's SIGUSR1), it is white and the 300 s start then.

Zones with no real length yet (6b428, per the owner: "just send the same lighting signals to all of those
headers"): an MSI Mystic Light board lists its addressable headers (JRAINBOW1, JRAINBOW2,
JCORSAIR) with ZERO LEDs, or (the MEG X570 ACE, 6b435) a placeholder of ONE, until a length is configured, so
nothing is sent to the strips behind them. At connect time (and after every reconnect or wake) each zone that
has 0 or 1 LEDs and can be resized (its minimum differs from its maximum; JRGB1 and PIPE1 cannot) is resized to DEFAULT_LENGTH (60; `setup.sh --leds-length N`), clamped to
what the zone allows, and the controller's data is read again so those LEDs are in the list
and get the very same frames as every other LED. A zone that already has more than one LED is never
touched. A length longer than the strip is harmless (what is written past its end goes
nowhere); one that is too short leaves the strip's tail dark, so the default errs long.
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
RISE_S = o1work.RISE_S            # white to red takes this long (5 s)
COOL_S = o1work.COOL_S            # red to white takes this long: the fans' ramp (60 s)
IDLE_DEEP_S = o1work.IDLE_DEEP_S  # idle this long, then white goes to blue (300 s)
BLUE_S = o1work.BLUE_S            # white to blue takes this long (30 s)
BLUE = o1work.BLUE                # the deep-idle colour
WAKE_FADE_S = o1work.WAKE_FADE_S  # blue back to white takes this long (2 s)
FRAME_S = 0.04                    # at most 25 frames a second while anything moves
POLL_S = 2                        # the connection and the keepalive
STATUS_STALE_S = 15               # a status file older than this: the service isn't running
CONNECT_S = 1.0
IO_S = 2.0
OPEN_BUDGET_S = 10.0              # one whole connect + enumerate may take this long, no more
RETRY_FIRST_S, RETRY_MAX_S = 2, 30
DEFAULT_LENGTH = 60               # LEDs given to a zone that has none: a splitter feeding several strips
LENGTH_MAX = 1024                 # the most `--leds-length N` may ask for (a zone's own maximum clamps it further)
NO_SERVER_WAIT_S = 60             # openrgb missing: wait this long before the unit tries again

# ---- the OpenRGB SDK network protocol ---------------------------------------------------
MAGIC = b"ORGB"
PROTO_MAX = 4                     # the highest version this client understands (segments came in 4)
PID_COUNT, PID_DATA, PID_VERSION, PID_NAME, PID_LIST_UPDATED = 0, 1, 40, 50, 51
PID_RESIZEZONE = 1000              # body: i32 zone index, i32 new size (no size prefix); the device is the header's
PID_UPDATELEDS, PID_UPDATEZONELEDS, PID_UPDATEMODE = 1050, 1051, 1101
ZONE_SINGLE, ZONE_LINEAR, ZONE_MATRIX = 0, 1, 2
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


class Zone:
    """One zone as the controller data lists it: its size limits and how many LEDs it has now."""

    def __init__(self, name, ztype, leds_min, leds_max, leds):
        self.name, self.ztype, self.leds_min, self.leds_max, self.leds = name, ztype, leds_min, leds_max, leds

    @property
    def resizable(self):
        return self.leds_min != self.leds_max

    def __repr__(self):
        return "Zone(%r, type %d, %d..%d, %d LEDs)" % (self.name, self.ztype, self.leds_min, self.leds_max, self.leds)


PLACEHOLDER_LEDS = 1        # a resizable zone with this many LEDs or fewer has no real length yet (6b435)


def target_length(z, want):
    """What to resize zone z to, or None to leave it: only a resizable zone with no real length yet (0 LEDs, or the
    1 LED OpenRGB lists as a placeholder, as the MSI board's JRAINBOW1/2 and JCORSAIR do), never a matrix or a
    fixed one (JRGB1, PIPE1), never one that already has more, and never to a size that is not larger than now."""
    if z.leds > PLACEHOLDER_LEDS or z.leds_max <= 0 or z.ztype == ZONE_MATRIX or not z.resizable:
        return None
    n = min(max(want, z.leds_min), z.leds_max)
    return n if n > max(z.leds, 0) else None


def config_path():
    return o1common.p("/etc/ollama1/leds.json")


def configured_length(path=None):
    """The length setup.sh saved (`--leds-length N`, LEDS_LENGTH), else DEFAULT_LENGTH; a bad file is the default."""
    v = read_json_safe(path or config_path(), {}, max_bytes=4096)
    n = v.get("length") if isinstance(v, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= LENGTH_MAX else DEFAULT_LENGTH


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
            ztype = r.i32()
            zmin = r.u32()
            zmax = r.u32()
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
            d.zones.append(Zone(zname, ztype, zmin, zmax, zleds))
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

    def __init__(self, host=HOST, port=PORT, connect=socket.create_connection, length=None):
        self.host, self.port, self.connect = host, port, connect
        self.sock, self.ver, self.devices = None, 0, []
        self.list_changed = False
        self.length = length                      # LEDs for a zone with none; None: read the saved setting at each open
        self.tried = set()                        # (device, zone) a resize was already asked for on this connection
        self.notes = []                           # one line per resize, for the service's log (Leds.link takes them)

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
        self.tried = set()
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
            d = Device.parse(i, self.wait_for(PID_DATA, i), self.ver)
            devs.append(self.grow_zones(d))
        self.devices = devs

    def grow_zones(self, d):
        """Give each zone of d that has no LEDs (and can grow) the configured length, then read the controller's
        data again so the LEDs are in its list. A zone that fails is noted and skipped; the others go on."""
        want = self.length if self.length is not None else configured_length()
        asked = []
        for zi, z in enumerate(d.zones):
            n = target_length(z, want)
            key = (d.name, zi, z.name)
            if z.leds > PLACEHOLDER_LEDS or not z.resizable:
                self.tried.discard(key)           # it has LEDs: if it is ever empty again (a re-plug), ask again
            if n is None or key in self.tried:
                continue
            self.tried.add(key)
            try:
                self.sock.sendall(packet(d.idx, PID_RESIZEZONE, struct.pack("<ii", zi, n)))
            except OSError as e:
                self.notes.append("lights: could not resize %s on %s (%s)" % (z.name, d.name, e.strerror or type(e).__name__))
                continue
            asked.append((zi, z, n, z.leds))
        if not asked:
            return d
        self.sock.sendall(packet(d.idx, PID_DATA, struct.pack("<I", self.ver)))
        fresh = Device.parse(d.idx, self.wait_for(PID_DATA, d.idx), self.ver)
        self.list_changed = False                 # our own resize's notices are in this read already
        for zi, z, n, before in asked:
            now = fresh.zones[zi].leds if zi < len(fresh.zones) and fresh.zones[zi].name == z.name else 0
            if now > before:
                self.tried.discard((d.name, zi, z.name))      # it worked: only a refusal is remembered
            self.notes.append("lights: resized %s to %d LEDs" % (z.name, now) if now > before else
                              "lights: could not resize %s on %s (still %d LEDs after asking for %d)" % (z.name, d.name, before, n))
        return fresh

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
    """The colour at position x along the gradient (0 white to 1 red): piecewise linear in RGB between STOPS."""
    x = 0.0 if x != x else min(1.0, max(0.0, x))
    for (x0, c0), (x1, c1) in zip(STOPS, STOPS[1:]):
        if x <= x1:
            f = (x - x0) / (x1 - x0)
            return tuple(int(round(a + (b - a) * f)) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def color_name(x):
    return "white" if x < 1 / 6 else "yellow" if x < 0.5 else "orange" if x < 5 / 6 else "red"


def blend(z):
    """White to BLUE, z 0..1: a straight line in RGB, each channel rounded."""
    z = 0.0 if z != z else min(1.0, max(0.0, z))
    return tuple(int(round(a + (b - a) * z)) for a, b in zip(WHITE, BLUE))


def ease(u):
    """Ease in and out (smoothstep): 0 at 0, 1 at 1, flat at both ends, 0.5 halfway."""
    u = min(1.0, max(0.0, u))
    return u * u * (3.0 - 2.0 * u)


def hex_of(c):
    return "%02X%02X%02X" % tuple(c)


def status_path():
    return o1common.p("/run/ollama1/leds.json")


class Leds:
    """One tick() per wake-up; it returns how long to sleep. `probes` as in lib/o1work.py (gpu_busy);
    `clock` is monotonic; `wall` stamps the status file.

    phase  idle      white; `idle_since` counts towards the blue (IDLE_DEEP_S)
           bluing    z (0 white .. 1 BLUE) rises 1/BLUE_S a second
           blue      z = 1
           waking    z falls 1/WAKE_FADE_S a second to 0 (work started while z > 0), then the rise
           rising    RISE_S from x0 to red at 1.0, eased; started by the card's work trigger
           working   red at 1.0 while the trigger holds
           cooling   x falls 1/COOL_S a second from where it was (red to white in COOL_S)
    The brightness is 100% always; there is no dimming."""

    def __init__(self, probes, client=None, clock=time.monotonic, wall=time.time, log=print, poll_s=POLL_S,
                 status=True):
        self.probes = probes
        self.client = client or Client()
        self.clock, self.wall, self.log = clock, wall, log
        self.poll_s, self.status = poll_s, status
        self.gpu = None                           # the last reading, or None: no reading
        self.last, self.next_sample = None, 0.0
        self.next_poll, self.retry_at, self.backoff = 0.0, 0.0, RETRY_FIRST_S
        self.sent, self.err, self.seen_name, self.seen_phase = None, None, None, None
        self.resync = False
        self.reset(self.clock())

    def reset(self, now):
        """Start (and a wake): white, idle, the 300 s from now; the trigger begins again."""
        self.trigger = o1work.GpuTrigger()
        self.phase, self.x, self.z = "idle", 0.0, 0.0
        self.idle_since = now
        self.t0, self.x0, self.z0 = now, 0.0, 0.0   # where the current rise, cool-down or wake fade started

    def idle_s(self, now):
        return max(0.0, now - self.idle_since) if self.phase in ("idle", "bluing", "blue") else 0.0

    def read_gpu(self):
        try:
            g = self.probes["gpu_busy"]()
        except Exception:
            return None
        return o1work.number(g)

    # -- the machine -----------------------------------------------------------------------
    def follow(self, now, working):
        """The phase from the trigger: work from white or mid-cool starts the rise from wherever it is; work while
        blue (or part way) first fades back to white (WAKE_FADE_S); the end of the work starts the cool-down."""
        if working and self.phase in ("bluing", "blue"):
            if self.z > 0.0:
                self.phase, self.t0, self.z0 = "waking", now, self.z
            else:
                self.phase, self.t0, self.x0 = "rising", now, 0.0
        elif working and self.phase in ("idle", "cooling"):
            self.phase, self.t0, self.x0 = "rising", now, self.x
        elif not working and self.phase in ("rising", "working"):
            self.phase, self.t0, self.x0 = "cooling", now, self.x

    def move(self, now):
        """x (the way to red) and z (the way to blue) for now."""
        if self.phase == "waking":
            end = self.t0 + self.z0 * WAKE_FADE_S
            if now >= end:
                self.z = 0.0
                if self.trigger.on:
                    self.phase, self.t0, self.x0 = "rising", end, 0.0
                else:
                    self.phase, self.idle_since = "idle", end            # the work ended meanwhile: white, idle again
            else:
                self.z = self.z0 - (now - self.t0) / WAKE_FADE_S
        if self.phase == "rising":
            e = ease((now - self.t0) / RISE_S)
            self.x = self.x0 + (1.0 - self.x0) * e
            if e >= 1.0:
                self.phase, self.x = "working", 1.0
        elif self.phase == "working":
            self.x = 1.0
        elif self.phase == "cooling":
            self.x = max(0.0, self.x0 - (now - self.t0) / COOL_S)
            if self.x <= 0.0:
                self.phase, self.idle_since = "idle", self.t0 + self.x0 * COOL_S       # white from that moment
        if self.phase == "idle":
            self.x, self.z = 0.0, 0.0
            if self.idle_s(now) >= IDLE_DEEP_S:
                self.phase, self.t0 = "bluing", self.idle_since + IDLE_DEEP_S
        if self.phase == "bluing":
            self.z = min(1.0, max(0.0, (now - self.t0) / BLUE_S))
            if self.z >= 1.0:
                self.phase = "blue"
        elif self.phase == "blue":
            self.z = 1.0

    def moving(self):
        """True while the colour is on its way somewhere: frames at FRAME_S."""
        return self.phase in ("rising", "cooling", "bluing", "waking")

    def rgb(self):
        return blend(self.z) if self.z > 0.0 else ramp(self.x)

    # -- the link -----------------------------------------------------------------------
    def lost(self, e, now):
        msg = "%s" % (getattr(e, "strerror", None) or str(e) or type(e).__name__)
        if self.err != msg:
            self.log("OpenRGB: %s (%s)" % (msg, type(e).__name__))
        self.err = msg
        self.client.close()
        self.sent = None
        self.retry_at, self.backoff = now + self.backoff, min(self.backoff * 2, RETRY_MAX_S)

    def drain_notes(self):
        notes = getattr(self.client, "notes", None)
        while notes:
            self.log(notes.pop(0))

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
            self.drain_notes()
        if not c.connected and now >= self.retry_at:
            try:
                c.open()
            except (OSError, ProtocolError) as e:
                self.lost(e, now)
                self.drain_notes()
            else:
                self.drain_notes()
                self.err, self.backoff, self.sent = None, RETRY_FIRST_S, None
                self.log("connected to OpenRGB (protocol %d): %s" % (
                    c.ver, ", ".join("%s (%d LEDs)" % (d.name, d.nleds) for d in c.devices) or "no devices yet"))

    # -- one tick -----------------------------------------------------------------------
    def tick(self):
        now = self.clock()
        if self.resync:
            self.reset(now)                       # a wake: white, the 5 minutes start again
        if now >= self.next_sample:
            self.next_sample += SAMPLE_S                        # on the 0.25 s beat, not drifting with the frames
            if self.next_sample <= now:
                self.next_sample = now + SAMPLE_S
            self.gpu = self.read_gpu()
            self.follow(now, self.trigger.update(now, self.gpu))
        polled = now >= self.next_poll or self.resync          # a wake does not wait for the poll
        if polled:
            self.next_poll = now + self.poll_s
            self.link(now)
        self.last = now
        self.move(now)
        rgb = self.rgb()
        if self.client.connected and (rgb != self.sent or polled):
            try:
                self.client.show(rgb)
                self.sent = rgb
            except OSError as e:
                self.lost(e, now)
        name = self.state_name()
        if polled or name != self.seen_name or self.phase != self.seen_phase:
            if self.phase != self.seen_phase:
                self.log("%s%s" % (self.phase, " (card %d%% busy)" % round(self.gpu) if self.gpu is not None else
                                   " (no card reading)"))
            self.seen_name, self.seen_phase = name, self.phase
            self.write_status(now)
        if self.moving():
            return FRAME_S                        # a rise, a cool-down, a blue fade or a wake fade: 25 frames a second
        return max(0.0, min(self.next_sample, self.next_poll) - now)

    def shutdown(self):
        """A stop must not leave the lights red: white, then the connection closed."""
        if self.client.connected:
            try:
                self.client.show(WHITE)
            except OSError:
                pass
        self.client.close()

    # -- status ---------------------------------------------------------------------------
    def state_name(self):
        """The colour in a word: white, yellow, orange, red, blue, or the fade between white and blue."""
        if self.phase == "blue":
            return "blue"
        if self.phase == "bluing":
            return "white to blue"
        if self.phase == "waking":
            return "blue to white"
        return color_name(self.x)

    def cool_left(self, now):
        return int(max(0.0, self.x0 * COOL_S - (now - self.t0)) + 0.999) if self.phase == "cooling" else 0

    def write_status(self, now):
        if not self.status:
            return
        c = self.client
        st = {"at": int(self.wall()), "state": self.state_name(), "phase": self.phase,
              "rgb": list(self.rgb()), "brightness": 1.0, "intensity": round(self.x, 3), "blue": round(self.z, 3),
              "idle": self.phase == "idle", "idle_s": int(self.idle_s(now)), "cool_left": self.cool_left(now),
              "working": self.trigger.on, "gpu_pct": None if self.gpu is None else round(self.gpu, 1),
              "gpu_reading": self.gpu is not None,
              "connected": c.connected, "error": self.err, "protocol": c.ver if c.connected else None,
              "devices": [{"name": d.name, "vendor": d.vendor, "leds": d.nleds,
                           "mode": d.modes[d.mode].name if d.kind else None, "usable": d.kind is not None,
                           "zones": [{"name": z.name, "leds": z.leds} for z in d.zones]}
                          for d in c.devices] if c.connected else []}
        st["why"] = what_text(st)
        st["line"] = status_line(st)
        try:
            os.makedirs(os.path.dirname(status_path()), exist_ok=True)
            write_json_atomic(status_path(), st, mode=0o644)
        except OSError:
            pass


# ---- status text ----------------------------------------------------------------------------

def card_text(st):
    pct = st.get("gpu_pct")
    return "card %d%% busy" % round(pct) if pct is not None else "no card reading"


def what_text(st):
    """What the lights are doing, in words: "working (card 87% busy)", "cooling down (white in 64 s)", ..."""
    ph = st.get("phase")
    idle_min = int(st.get("idle_s", 0) / 60.0 + 0.5)
    if ph == "rising":
        return "turning red (%s)" % card_text(st)
    if ph == "working":
        return "working (%s)" % card_text(st)
    if ph == "cooling":
        return "cooling down (white in %d s)" % st.get("cool_left", 0)
    if ph == "waking":
        return "back to white (%s)" % card_text(st)
    if ph == "bluing":
        return "going blue (idle %d min)" % idle_min
    if ph == "blue":
        return "blue (idle %d min)" % idle_min
    return "idle (%s)" % card_text(st)


def status_line(st):
    """One line for the admin panel's CPU card and the top of `status`."""
    if not st.get("connected"):
        return "Lights: " + ("OpenRGB: %s" % st["error"] if st.get("error") else "waiting for OpenRGB")
    devs = st.get("devices") or []
    if not devs:
        return "Lights: OpenRGB lists no devices"
    return "Lights: %s, %s  -  %d device%s" % (st.get("state"), what_text(st), len(devs), "" if len(devs) == 1 else "s")


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
    pct = st.get("gpu_pct")
    out = ["lights: %s  now %d,%d,%d (%s)  -  %s" % (st.get("state", "?"), rgb[0], rgb[1], rgb[2], hex_of(rgb),
                                                    what_text(st)),
           "card: %s  -  %s  -  position %s of 1 (0 white, 1 red)" % (
               "%d%% busy" % round(pct) if pct is not None else "no reading (taken as not working)",
               "working (over %d%%)" % o1work.GPU_BUSY_PCT if st.get("working") else "not working",
               st.get("intensity", "?")),
           "brightness: %d%%  -  idle %d s (white goes to blue over %d s after %d s)" % (
               round(st.get("brightness", 1.0) * 100), st.get("idle_s", 0), BLUE_S, IDLE_DEEP_S)]
    devs = st.get("devices") or []
    if st.get("connected"):
        out.append("openrgb: connected to %s:%d (protocol %s), %d device%s found" % (
            HOST, PORT, st.get("protocol"), len(devs), "" if len(devs) == 1 else "s"))
        for d in devs:
            out.append("  %-34s %s, %d LEDs%s" % (d.get("name"), "mode " + d["mode"] if d.get("mode") else "no usable mode",
                                                  d.get("leds", 0), "  (" + d["vendor"] + ")" if d.get("vendor") else ""))
            zs = d.get("zones") or []
            if zs:
                out.append("    zones: " + ", ".join("%s %d" % (z.get("name"), z.get("leds", 0)) for z in zs))
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


def save_length(length, path=None):
    """setup.sh's --leds-length N: kept for the service (/etc/ollama1/leds.json). Returns the length, or None when
    it is not a whole number from 1 to LENGTH_MAX (nothing is written then)."""
    try:
        n = int(str(length), 10)
    except ValueError:
        return None
    if not 1 <= n <= LENGTH_MAX:
        return None
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, {"length": n}, mode=0o644)
    return n


def setup(choice, systemctl=run_systemctl, which=shutil.which, apt=run_apt_install, log=print, length=None):
    """setup.sh's step. on: the openrgb package (only if it isn't there), both units enabled and
    (re)started. off: both stopped (the lights go white as the service stops) and disabled; the
    package and everything else stay."""
    if choice == "on":
        if length is not None and save_length(length) is None:
            log("the lights: --leds-length takes a whole number from 1 to %d; left as it was" % LENGTH_MAX)
            return 1
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
    notify(("READY=1\n" if first else "") + "WATCHDOG=1\nSTATUS=%s" % leds.phase)
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
