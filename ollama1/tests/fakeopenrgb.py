"""A fake OpenRGB SDK server for the lights' tests (6b395): a real TCP listener on
127.0.0.1 that speaks the protocol the way OpenRGB's NetworkServer does (version
negotiation, controller count and data at the client's version, a device-list-
updated notice), records every packet it receives, and can be stopped and started
again on the same port. It builds its replies with its own struct code, not the
kit's, so a mistake in lib/o1leds.py's encoder and parser cannot cancel out.

Zones (6b422) carry a size range: a zone is (name, count) as before, or (name, count, min, max) or
(name, count, min, max, type) with type 0 single, 1 linear, 2 matrix; a zone with min != max can be resized
by the SDK's RESIZEZONE packet (id 1000: i32 zone index, i32 new size, the device in the header, no size
prefix, as OpenRGB's NetworkClient/NetworkServer write and read it). The server follows the real one: a
size outside min..max, or a zone with min == max, is ignored without an answer; a good one changes the
zone and the device's LED list. It also records the colour of every LED (device["colors"]), by UPDATELEDS
and UPDATEZONELEDS, and every UPDATELEDS frame it saw (self.frames).
"""
import socket
import struct
import threading
import time

FLAG_BRIGHTNESS, FLAG_PER_LED, FLAG_MODE_COLOR = 1 << 4, 1 << 5, 1 << 6
PID_RESIZEZONE, PID_UPDATELEDS, PID_UPDATEZONELEDS = 1000, 1050, 1051


def zone_spec(z):
    """(name, count, min, max, type) from a 2-, 4- or 5-tuple: a plain (name, count) is fixed at its size."""
    name, n = z[0], z[1]
    lo, hi = (z[2], z[3]) if len(z) >= 4 else (n, n)
    return name, n, lo, hi, z[4] if len(z) >= 5 else 0


def s16(text):
    b = text.encode() + b"\0"
    return struct.pack("<H", len(b)) + b


def mode_spec(name, flags=FLAG_PER_LED, color_mode=1, bri=None, colors=(), value=0):
    """bri: None, or (min, max, current)."""
    return {"name": name, "flags": flags | (FLAG_BRIGHTNESS if bri else 0), "color_mode": color_mode, "bri": bri,
            "colors": list(colors), "value": value}


def mode_bytes(m, ver):
    b = s16(m["name"]) + struct.pack("<iIII", m["value"], m["flags"], 10, 200)
    if ver >= 3:
        b += struct.pack("<II", *(m["bri"][:2] if m["bri"] else (0, 0)))
    b += struct.pack("<III", 0, 1 if m["colors"] else 0, 50)
    if ver >= 3:
        b += struct.pack("<I", m["bri"][2] if m["bri"] else 0)
    b += struct.pack("<IIH", 0, m["color_mode"], len(m["colors"]))
    for c in m["colors"]:
        b += bytes((c[0], c[1], c[2], 0))
    return b


def device(name, leds, modes, active=0, vendor="Fake Vendor", zones=None, matrix=False, segments=False):
    return {"name": name, "leds": leds, "modes": modes, "active": active, "vendor": vendor,
            "zones": zones or [("Zone", leds)], "matrix": matrix, "segments": segments,
            "colors": [(0, 0, 0)] * leds}


def controller_data(d, ver):
    b = struct.pack("<i", 1) + s16(d["name"])
    if ver >= 1:
        b += s16(d["vendor"])
    b += s16("desc") + s16("1.0") + s16("serial") + s16("HID: /dev/hidraw9")
    b += struct.pack("<Hi", len(d["modes"]), d["active"])
    for m in d["modes"]:
        b += mode_bytes(m, ver)
    b += struct.pack("<H", len(d["zones"]))
    for z in d["zones"]:
        zname, n, lo, hi, ztype = zone_spec(z)
        b += s16(zname) + struct.pack("<iIII", ztype or (1 if d["matrix"] else 0), lo, hi, n)
        if d["matrix"]:
            b += struct.pack("<HII", 8 + 4 * 2 * 2, 2, 2) + struct.pack("<4I", 0, 1, 2, 3)
        else:
            b += struct.pack("<H", 0)
        if ver >= 4:
            if d["segments"]:
                b += struct.pack("<H", 1) + s16("Seg") + struct.pack("<iII", 0, 0, n)
            else:
                b += struct.pack("<H", 0)
    b += struct.pack("<H", d["leds"])
    for i in range(d["leds"]):
        b += s16("LED %d" % i) + struct.pack("<I", 0)
    b += struct.pack("<H", d["leds"]) + b"".join(bytes(c) + b"\0" for c in d["colors"])
    return struct.pack("<I", 4 + len(b)) + b


def decode_leds(body):
    size, n = struct.unpack("<IH", body[:6])
    assert size == len(body), (size, len(body))
    assert len(body) == 6 + 4 * n
    return [tuple(body[6 + 4 * i:9 + 4 * i]) for i in range(n)], [body[9 + 4 * i] for i in range(n)]


def decode_mode(body, ver):
    """(mode index, dict of the mode) from an UPDATEMODE body."""
    size, idx = struct.unpack("<Ii", body[:8])
    assert size == len(body), (size, len(body))
    i = 8
    n = struct.unpack("<H", body[i:i + 2])[0]
    name = body[i + 2:i + 2 + n].rstrip(b"\0").decode()
    i += 2 + n
    value, flags, smin, smax = struct.unpack("<iIII", body[i:i + 16])
    i += 16
    bmin = bmax = bri = None
    if ver >= 3:
        bmin, bmax = struct.unpack("<II", body[i:i + 8])
        i += 8
    cmin, cmax, speed = struct.unpack("<III", body[i:i + 12])
    i += 12
    if ver >= 3:
        bri = struct.unpack("<I", body[i:i + 4])[0]
        i += 4
    direction, color_mode, nc = struct.unpack("<IIH", body[i:i + 10])
    i += 10
    colors = [tuple(body[i + 4 * k:i + 4 * k + 3]) for k in range(nc)]
    assert i + 4 * nc == len(body), "trailing bytes"
    return idx, {"name": name, "flags": flags, "bri_min": bmin, "bri_max": bmax, "brightness": bri,
                 "color_mode": color_mode, "colors": colors}


def zone_slices(d):
    """(start, end) of each zone's LEDs in the device's LED list, zones in order."""
    out, at = [], 0
    for z in d["zones"]:
        out.append((at, at + zone_spec(z)[1]))
        at += zone_spec(z)[1]
    return out


def resize_zone(d, zi, new):
    """What the real server does: the size is taken only inside the zone's range and for a resizable zone; the LED
    list grows or shrinks at that zone and the colours of the others stay. True when it changed."""
    if not 0 <= zi < len(d["zones"]):
        return False
    name, n, lo, hi, ztype = zone_spec(d["zones"][zi])
    if lo == hi or not lo <= new <= hi:
        return False
    start, end = zone_slices(d)[zi]
    d["colors"][start:end] = [(0, 0, 0)] * new
    d["zones"][zi] = (name, new, lo, hi, ztype)
    d["leds"] = len(d["colors"])
    return True


class FakeServer:
    def __init__(self, devices, version=4, port=0, answer_version=True, host="127.0.0.1", garbage=False):
        self.devices, self.version, self.port, self.host = devices, version, port, host
        self.answer_version, self.garbage = answer_version, garbage
        self.packets = []
        self.frames = []                     # (device, [colours]) of every UPDATELEDS, in order
        self.resize_deaf = set()             # (device, zone index) the server ignores resizes for, though in range
        self.lock = threading.Lock()
        self.listener, self.conns, self.client_ver = None, [], None
        self.connections = 0

    def start(self):
        ls = socket.socket()
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self.host, self.port))
        ls.listen(8)
        self.port = ls.getsockname()[1]
        self.listener = ls
        threading.Thread(target=self.accept, args=(ls,), daemon=True).start()
        return self

    def stop(self):
        ls, self.listener = self.listener, None
        if ls:
            ls.close()
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        self.conns = []

    def accept(self, ls):
        while True:
            try:
                c, _a = ls.accept()
            except OSError:
                return
            self.conns.append(c)
            self.connections += 1
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()

    def recv(self, c, n):
        b = b""
        while len(b) < n:
            chunk = c.recv(n - len(b))
            if not chunk:
                raise ConnectionError
            b += chunk
        return b

    def send(self, c, dev, pid, body=b""):
        c.sendall(struct.pack("<4sIII", b"ORGB", dev, pid, len(body)) + body)

    def serve(self, c):
        try:
            if self.garbage:
                c.sendall(b"HTTP/1.1 400 nope\r\n\r\n")
                return
            while True:
                magic, dev, pid, size = struct.unpack("<4sIII", self.recv(c, 16))
                assert magic == b"ORGB"
                body = self.recv(c, size) if size else b""
                with self.lock:
                    self.packets.append((dev, pid, body))
                if pid == 40 and self.answer_version:
                    self.send(c, 0, 40, struct.pack("<I", self.version))
                elif pid == 0:
                    self.send(c, 0, 0, struct.pack("<I", len(self.devices)))
                elif pid == 1:
                    ver = min(self.version, struct.unpack("<I", body[:4])[0]) if body else 0
                    self.client_ver = ver
                    with self.lock:
                        data = controller_data(self.devices[dev], ver)
                    self.send(c, dev, 1, data)
                elif pid == PID_RESIZEZONE:
                    zi, new = struct.unpack("<ii", body)           # exactly 8 bytes: a size prefix would not unpack
                    if (dev, zi) not in self.resize_deaf:
                        with self.lock:
                            resize_zone(self.devices[dev], zi, new)
                elif pid == PID_UPDATELEDS:
                    colors, _bri = decode_leds(body)
                    with self.lock:
                        self.frames.append((dev, list(colors)))
                        self.devices[dev]["colors"] = list(colors)
                elif pid == PID_UPDATEZONELEDS:
                    size, zi, n = struct.unpack("<IIH", body[:10])
                    assert size == len(body) and len(body) == 10 + 4 * n
                    d = self.devices[dev]
                    start, end = zone_slices(d)[zi]
                    with self.lock:
                        d["colors"][start:end] = [tuple(body[10 + 4 * i:13 + 4 * i]) for i in range(n)]
        except (OSError, ConnectionError, AssertionError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def notify_list_updated(self):
        for c in list(self.conns):
            try:
                self.send(c, 0, 51)
            except OSError:
                pass

    def seen(self, pid, dev=None):
        with self.lock:
            return [(d, b) for d, p, b in self.packets if p == pid and (dev is None or d == dev)]

    def wait_for(self, pid, count=1, timeout=3.0, dev=None):
        end = time.time() + timeout
        while time.time() < end:
            got = self.seen(pid, dev)
            if len(got) >= count:
                return got
            time.sleep(0.01)
        raise AssertionError("waited for %d packet(s) of id %d; saw %d" % (count, pid, len(self.seen(pid, dev))))

    def clear(self):
        with self.lock:
            self.packets.clear()
            self.frames.clear()

    def zone_colors(self, dev, zi):
        """The colours of one zone's LEDs now."""
        with self.lock:
            a, b = zone_slices(self.devices[dev])[zi]
            return list(self.devices[dev]["colors"][a:b])
