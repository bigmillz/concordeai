"""Lights that follow the graphics card's work (6b395, 6b417; the states of 6b421): the OpenRGB SDK
protocol against a fake server on a local socket (framing, version negotiation, device lists,
modes, colours), the gradient and the ease, the service on a fake clock (the card's 50% / 1.5 s
trigger, the 5 s eased rise to red and full brightness, red while working, the 120 s linear cool-down,
work again mid-cool, white for 300 s then 40%, frames only when the colour changes, a keepalive), a
server that dies and comes back, the status, setup and the wiring (units, setup.sh, the panel, the
sleep hook); zones that list no LEDs (6b422): their size limits parsed, the resize packet, the same frames
on the new LEDs, a reconnect and a wake resizing again, a failing zone, the saved length."""
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import time
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import fakeopenrgb as F
import o1fan
import o1leds
import o1work

WHITE, RED = (255, 255, 255), (255, 0, 0)


def two_devices():
    return [
        F.device("MSI MYSTIC LIGHT", 6, [F.mode_spec("Off", F.FLAG_MODE_COLOR, 0), F.mode_spec("Direct", F.FLAG_PER_LED)],
                 active=0, vendor="MSI", zones=[("JRGB1", 2), ("JRGB2", 4)], matrix=True),
        F.device("Corsair Hydro Platinum", 16,
                 [F.mode_spec("Static", F.FLAG_MODE_COLOR, 2, bri=(0, 100, 40), colors=[(1, 2, 3)]),
                  F.mode_spec("Direct", F.FLAG_PER_LED, 1, bri=(0, 100, 40))], active=0, vendor="Corsair", segments=True),
    ]


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Probes:
    """gpu_busy is the card's percent; the rest is offered and must not matter."""

    def __init__(self, clock=None):
        self.clock = clock
        self.v = {"inflight": 0, "gpu_busy": 0, "tools": [], "cpu": (0, 0), "cpu_c": 40}

    def dict(self):
        return {k: (lambda k=k: self.v[k]()) if callable(self.v[k]) else (lambda k=k: self.v[k]) for k in self.v}


class StubClient:
    """The Client's face for the state-machine tests: records what it was told to show."""

    def __init__(self, clock):
        self.clock, self.connected, self.devices, self.ver = clock, False, [], 4
        self.shown, self.opens, self.closes, self.fail_open, self.fail_show = [], 0, 0, False, False

    def open(self):
        self.opens += 1
        if self.fail_open:
            raise ConnectionRefusedError(111, "Connection refused")
        self.connected = True

    def close(self):
        self.closes += 1
        self.connected = False

    def poll(self):
        return False

    def show(self, color):
        if self.fail_show:
            raise BrokenPipeError(32, "Broken pipe")
        self.shown.append((self.clock.t, color))


class Rig(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.probes = Probes(self.clock)
        self.client = StubClient(self.clock)
        self.lines = []
        self.make()
        self.addCleanup(lambda: os.path.exists(o1leds.status_path()) and os.unlink(o1leds.status_path()))

    def make(self, **kw):
        kw.setdefault("poll_s", 2)
        self.leds = o1leds.Leds(self.probes.dict(), client=self.client, clock=self.clock, wall=lambda: 1_700_000_000,
                                log=self.lines.append, **kw)

    def run_for(self, seconds, step=0.05, gpu=None):
        """Tick every `step` s; the colour on show after each tick as (t - start, rgb). `gpu` sets the reading."""
        if gpu is not None:
            self.probes.v["gpu_busy"] = gpu
        t0, out = self.clock.t, []
        for _ in range(int(round(seconds / step))):
            self.leds.tick()
            self.clock.t += step
            out.append((round(self.clock.t - t0, 6), self.leds.rgb()))
        return out

    def last_shown(self):
        return self.client.shown[-1][1] if self.client.shown else None


# ---- the protocol -----------------------------------------------------------------------

class TestProtocol(unittest.TestCase):
    def test_a_packet_is_a_16_byte_little_endian_header_and_the_body(self):
        self.assertEqual(o1leds.packet(3, 1050, b"abc"), b"ORGB" + struct.pack("<III", 3, 1050, 3) + b"abc")
        self.assertEqual(o1leds.packet(0, 0), b"ORGB" + b"\0" * 12)

    def test_update_leds_has_its_own_size_a_count_and_one_rgb0_per_led(self):
        b = o1leds.encode_update_leds([(255, 255, 255), (255, 0, 0)])
        self.assertEqual(b, struct.pack("<IH", 4 + 2 + 8, 2) + b"\xff\xff\xff\0" + b"\xff\0\0\0")
        self.assertEqual(o1leds.encode_update_leds([]), struct.pack("<IH", 6, 0))

    def test_update_zone_leds_adds_the_zone_index(self):
        b = o1leds.encode_update_zone_leds(2, [(1, 2, 3)])
        self.assertEqual(b, struct.pack("<IIH", 4 + 4 + 2 + 4, 2, 1) + b"\x01\x02\x03\0")

    def test_the_constants_are_the_protocols(self):
        self.assertEqual((o1leds.PID_COUNT, o1leds.PID_DATA, o1leds.PID_VERSION, o1leds.PID_NAME), (0, 1, 40, 50))
        self.assertEqual((o1leds.PID_UPDATELEDS, o1leds.PID_UPDATEZONELEDS, o1leds.PID_UPDATEMODE), (1050, 1051, 1101))
        self.assertEqual(o1leds.PID_LIST_UPDATED, 51)
        self.assertEqual(o1leds.PORT, 6742)

    def test_a_device_parses_at_every_version(self):
        for ver in (0, 1, 2, 3, 4):
            for d in two_devices():
                dev = o1leds.Device.parse(7, F.controller_data(d, ver), ver)
                self.assertEqual((dev.name, dev.nleds, dev.active), (d["name"], d["leds"], d["active"]), ver)
                self.assertEqual([m.name for m in dev.modes], [m["name"] for m in d["modes"]])
                self.assertEqual(dev.vendor, d["vendor"] if ver >= 1 else "")
                self.assertEqual(len(dev.zones), len(d["zones"]))
        dev = o1leds.Device.parse(0, F.controller_data(two_devices()[1], 4), 4)
        self.assertEqual((dev.modes[0].bri_min, dev.modes[0].bri_max, dev.modes[0].brightness), (0, 100, 40))
        self.assertEqual(dev.modes[0].colors, [(1, 2, 3)])
        dev = o1leds.Device.parse(0, F.controller_data(two_devices()[1], 2), 2)
        self.assertEqual(dev.modes[0].brightness, 0)                   # brightness only exists from version 3

    def test_a_mode_encodes_back_to_what_it_was_read_from(self):
        for ver in (1, 2, 3, 4):
            m = F.mode_spec("Static", F.FLAG_MODE_COLOR, 2, bri=(0, 100, 40), colors=[(1, 2, 3), (4, 5, 6)])
            raw = F.mode_bytes(m, ver)
            r = o1leds.Reader(raw)
            self.assertEqual(o1leds.Mode.read(r, ver).encode(ver), raw, ver)
            self.assertEqual(r.i, len(raw))

    def test_broken_replies_raise_instead_of_hanging_or_guessing(self):
        good = F.controller_data(two_devices()[0], 4)
        for cut in (3, 10, len(good) // 2):
            with self.assertRaises(o1leds.ProtocolError):
                o1leds.Device.parse(0, good[:cut], 4)
        with self.assertRaises(o1leds.ProtocolError):
            o1leds.Device.parse(0, struct.pack("<II", 8, 0) + F.s16("x") + F.s16("v") + F.s16("d") * 3 + F.s16("l")
                                + struct.pack("<Hi", 60000, 0), 4)           # 60000 modes

    def test_mode_choice_prefers_direct_then_custom_then_static(self):
        mk = lambda *names: o1leds.Device.parse(0, F.controller_data(F.device("x", 3, [
            F.mode_spec(n, F.FLAG_PER_LED) for n in names]), 4), 4)
        self.assertEqual(mk("Static", "Direct", "Rainbow").choose_mode(), (1, "leds"))
        self.assertEqual(mk("Rainbow", "Custom", "Static").choose_mode(), (1, "leds"))
        self.assertEqual(mk("Rainbow", "Static").choose_mode(), (1, "leds"))
        odd = o1leds.Device.parse(0, F.controller_data(F.device("x", 3, [
            F.mode_spec(n, F.FLAG_MODE_COLOR, 2) for n in ("Rainbow", "Breathing")]), 4), 4)
        self.assertEqual(odd.choose_mode(), (None, None))
        st = o1leds.Device.parse(0, F.controller_data(F.device("x", 3, [
            F.mode_spec("Static", F.FLAG_MODE_COLOR, 2, colors=[(0, 0, 0)])]), 4), 4)
        self.assertEqual(st.choose_mode(), (0, "mode"))


# ---- the client against the fake server -------------------------------------------------

class ServerCase(unittest.TestCase):
    def setUp(self):
        self.srv = F.FakeServer(two_devices()).start()
        self.addCleanup(self.srv.stop)
        self.client = o1leds.Client(port=self.srv.port)
        self.addCleanup(self.client.close)

    def pids(self):
        return [p for _d, p, _b in self.srv.packets]

    def sync(self, client=None, srv=None):
        """A round trip: when the answer is back the server has handled everything sent before it."""
        c = client or self.client
        c.sock.sendall(o1leds.packet(0, o1leds.PID_COUNT))
        c.wait_for(o1leds.PID_COUNT)
        (srv or self.srv).clear()


class TestClient(ServerCase):
    def test_the_handshake_names_the_client_negotiates_then_lists_the_devices(self):
        self.client.open()
        self.assertEqual(self.pids()[:4], [50, 40, 0, 1])
        self.assertEqual(self.srv.packets[0][2], b"ollama1-leds\0")
        self.assertEqual(self.srv.packets[1][2], struct.pack("<I", 4))
        self.assertEqual(self.srv.packets[2][1:], (0, b""))
        self.assertEqual([(d, b) for d, p, b in self.srv.packets if p == 1],
                         [(0, struct.pack("<I", 4)), (1, struct.pack("<I", 4))])
        self.assertEqual([(d.name, d.nleds) for d in self.client.devices],
                         [("MSI MYSTIC LIGHT", 6), ("Corsair Hydro Platinum", 16)])
        self.assertEqual(self.client.ver, 4)

    def test_the_version_is_the_lower_of_the_two(self):
        for server_ver, want in ((5, 4), (4, 4), (3, 3), (2, 2)):
            srv = F.FakeServer(two_devices(), version=server_ver).start()
            self.addCleanup(srv.stop)
            c = o1leds.Client(port=srv.port)
            self.addCleanup(c.close)
            c.open()
            self.assertEqual(c.ver, want)
            self.assertEqual([b for _d, p, b in srv.packets if p == 1][0], struct.pack("<I", want))
            self.assertEqual(len(c.devices), 2)

    def test_a_server_that_never_answers_the_version_is_taken_as_version_0(self):
        srv = F.FakeServer(two_devices(), version=0, answer_version=False).start()
        self.addCleanup(srv.stop)
        old, o1leds.IO_S = o1leds.IO_S, 0.3
        self.addCleanup(setattr, o1leds, "IO_S", old)
        c = o1leds.Client(port=srv.port)
        self.addCleanup(c.close)
        c.open()
        self.assertEqual(c.ver, 0)
        self.assertEqual(len(c.devices), 2)

    def test_directs_modes_are_set_with_the_brightness_at_its_maximum(self):
        self.client.open()
        modes = self.srv.wait_for(1101, 2)
        # the board: was Off, goes to Direct (no brightness); the cooler: Static, goes to Direct at brightness 100
        self.assertEqual([d for d, _b in modes], [0, 1])
        i0, m0 = F.decode_mode(modes[0][1], 4)
        self.assertEqual((i0, m0["name"], m0["color_mode"]), (1, "Direct", 1))
        i1, m1 = F.decode_mode(modes[1][1], 4)
        self.assertEqual((i1, m1["name"], m1["brightness"], m1["bri_max"]), (1, "Direct", 100, 100))

    def test_a_device_already_in_direct_without_brightness_is_not_touched(self):
        devs = [F.device("Plain", 4, [F.mode_spec("Direct", F.FLAG_PER_LED)], active=0)]
        srv = F.FakeServer(devs).start()
        self.addCleanup(srv.stop)
        c = o1leds.Client(port=srv.port)
        self.addCleanup(c.close)
        c.open()
        self.assertEqual(srv.seen(1101), [])

    def test_every_led_of_every_device_gets_exactly_255_255_255_then_255_0_0(self):
        self.client.open()
        self.sync()
        self.client.show(WHITE)
        self.client.show(RED)
        got = self.srv.wait_for(1050, 4)
        by_dev = {0: [], 1: []}
        for d, b in got:
            colors, pad = F.decode_leds(b)
            self.assertEqual(set(pad), {0})
            by_dev[d].append(colors)
        self.assertEqual(by_dev[0], [[WHITE] * 6, [RED] * 6])
        self.assertEqual(by_dev[1], [[WHITE] * 16, [RED] * 16])

    def test_a_static_only_device_gets_its_colour_in_the_mode(self):
        devs = [F.device("Cooler", 8, [F.mode_spec("Static", F.FLAG_MODE_COLOR, 2, bri=(0, 100, 10), colors=[(9, 9, 9)])])]
        srv = F.FakeServer(devs).start()
        self.addCleanup(srv.stop)
        c = o1leds.Client(port=srv.port)
        self.addCleanup(c.close)
        c.open()
        self.sync(c, srv)
        c.show(RED)
        (_d, body), = srv.wait_for(1101)
        _i, m = F.decode_mode(body, 4)
        self.assertEqual((m["colors"], m["brightness"]), ([RED], 100))
        self.assertEqual(srv.seen(1050), [])

    def test_devices_with_no_leds_or_no_usable_mode_are_skipped(self):
        devs = [F.device("NoLeds", 0, [F.mode_spec("Direct")]),
                F.device("Odd", 3, [F.mode_spec("Rainbow", F.FLAG_MODE_COLOR, 2)]),
                F.device("Fine", 2, [F.mode_spec("Direct")])]
        srv = F.FakeServer(devs).start()
        self.addCleanup(srv.stop)
        c = o1leds.Client(port=srv.port)
        self.addCleanup(c.close)
        c.open()
        self.sync(c, srv)
        c.show(WHITE)
        c.sock.sendall(o1leds.packet(0, o1leds.PID_COUNT))
        c.wait_for(o1leds.PID_COUNT)
        self.assertEqual([d for d, _b in srv.seen(1050)], [2])

    def test_a_changed_device_list_is_read_again(self):
        self.client.open()
        self.srv.devices.append(F.device("New", 3, [F.mode_spec("Direct")]))
        self.srv.notify_list_updated()
        time.sleep(0.1)
        self.assertTrue(self.client.poll())
        self.assertEqual([d.name for d in self.client.devices][-1], "New")
        self.assertFalse(self.client.poll())

    def test_no_devices_yet_is_asked_again_on_every_poll(self):
        self.srv.devices[:] = []
        self.client.open()
        self.assertEqual(self.client.devices, [])
        self.srv.devices.extend(two_devices())
        self.assertTrue(self.client.poll())
        self.assertEqual(len(self.client.devices), 2)

    def test_a_dead_server_is_noticed_on_the_next_poll(self):
        self.client.open()
        self.srv.stop()
        time.sleep(0.1)
        with self.assertRaises(OSError):
            self.client.poll()

    def test_not_an_openrgb_server_is_a_protocol_error(self):
        srv = F.FakeServer([], garbage=True).start()
        self.addCleanup(srv.stop)
        c = o1leds.Client(port=srv.port)
        with self.assertRaises((o1leds.ProtocolError, OSError)):
            c.open()
        self.assertFalse(c.connected)

    def test_nothing_listening_is_an_oserror_and_leaves_it_closed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        c = o1leds.Client(port=port)
        with self.assertRaises(OSError):
            c.open()
        self.assertFalse(c.connected)

    def test_the_default_is_the_loopback_only(self):
        self.assertEqual(o1leds.HOST, "127.0.0.1")
        self.assertEqual(o1leds.Client().host, "127.0.0.1")
        seen = []
        c = o1leds.Client(connect=lambda addr, timeout=None: seen.append(addr) or (_ for _ in ()).throw(OSError("no")))
        with self.assertRaises(OSError):
            c.open()
        self.assertEqual(seen, [("127.0.0.1", 6742)])


# ---- the colour -----------------------------------------------------------------------------

class TestRamp(unittest.TestCase):
    def test_the_ramp_at_the_seven_points(self):
        for x, want in ((0, (255, 255, 255)), (.17, (255, 255, 125)), (.33, (255, 255, 3)), (.5, (255, 192, 0)),
                        (.67, (255, 127, 0)), (.83, (255, 65, 0)), (1, (255, 0, 0))):
            self.assertEqual(o1leds.ramp(x), want, x)

    def test_the_stops_are_exact_white_yellow_orange_red(self):
        self.assertEqual(o1leds.ramp(0.0), WHITE)
        self.assertEqual(o1leds.ramp(1 / 3), (255, 255, 0))
        self.assertEqual(o1leds.ramp(2 / 3), (255, 128, 0))
        self.assertEqual(o1leds.ramp(1.0), RED)
        self.assertEqual(o1leds.STOPS[0][1], WHITE)
        self.assertEqual(o1leds.STOPS[-1][1], RED)

    def test_it_is_clamped_and_always_a_valid_colour(self):
        self.assertEqual(o1leds.ramp(-3), WHITE)
        self.assertEqual(o1leds.ramp(7), RED)
        self.assertEqual(o1leds.ramp(float("nan")), WHITE)
        for i in range(0, 1001):
            c = o1leds.ramp(i / 1000)
            self.assertTrue(all(0 <= v <= 255 for v in c) and c[0] == 255, c)

    def test_it_goes_white_through_yellow_and_orange_to_red_without_going_back(self):
        prev = None
        for i in range(0, 1001):
            c = o1leds.ramp(i / 1000)
            if prev:
                self.assertLessEqual(c[1], prev[1])
                self.assertLessEqual(c[2], prev[2])
            prev = c
        self.assertEqual(o1leds.ramp(0.4)[2], 0)                  # past yellow: no blue left
        self.assertGreater(o1leds.ramp(0.2)[2], 0)                # before it: still some

    def test_the_names(self):
        self.assertEqual([o1leds.color_name(x) for x in (0, .1, .2, .49, .5, .8, .84, 1)],
                         ["white", "white", "yellow", "yellow", "orange", "orange", "red", "red"])

    def test_the_ease_is_smooth_at_both_ends_and_half_way_at_the_middle(self):
        e = o1leds.ease
        self.assertEqual((e(0), e(0.5), e(1)), (0.0, 0.5, 1.0))
        self.assertEqual((e(-1), e(2)), (0.0, 1.0))
        self.assertLess(e(0.1), 0.05)                             # slow to start ...
        self.assertGreater(e(0.9), 0.95)                          # ... and slow to arrive
        vals = [e(i / 100) for i in range(101)]
        self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))
        self.assertAlmostEqual(e(0.25) + e(0.75), 1.0)            # symmetric

    def test_brightness_multiplies_each_channel(self):
        self.assertEqual(o1leds.scale(WHITE, 0.4), (102, 102, 102))
        self.assertEqual(o1leds.scale(RED, 0.4), (102, 0, 0))
        self.assertEqual(o1leds.scale((255, 128, 0), 0.5), (128, 64, 0))
        self.assertEqual(o1leds.scale(RED, 1.0), RED)


class TestTheNumbers(unittest.TestCase):
    def test_the_lights_use_the_shared_numbers_so_they_agree_with_the_fans(self):
        self.assertEqual((o1leds.RISE_S, o1leds.COOL_S, o1leds.IDLE_DIM_S, o1leds.DIM_S, o1leds.DIM_MIN),
                         (5.0, 120, 300.0, 10.0, 0.4))
        self.assertEqual(o1leds.COOL_S, o1fan.RAMP_S)            # lights and fans finish together
        self.assertEqual((o1leds.SAMPLE_S, o1leds.FRAME_S), (0.25, 0.04))


# ---- the service on a fake clock ----------------------------------------------------------------

class Phases(Rig):
    def until(self, cond, limit=600.0, step=0.05):
        """Tick every `step` s until cond() holds; the time it did."""
        end = self.clock.t + limit
        while self.clock.t < end:
            self.leds.tick()
            if cond():
                return self.clock.t
            self.clock.t += step
        self.fail("never")

    def start_rise(self, gpu=100):
        self.probes.v["gpu_busy"] = gpu
        return self.until(lambda: self.leds.phase == "rising")

    def to_red(self):
        self.start_rise()
        return self.until(lambda: self.leds.phase == "working")

    def start_cool(self):
        self.probes.v["gpu_busy"] = 0
        return self.until(lambda: self.leds.phase == "cooling")

    def at(self, t, step=0.05):
        """Tick on to time `t` (the last tick exactly at t)."""
        while self.clock.t + step < t - 1e-9:
            self.clock.t += step
            self.leds.tick()
        self.clock.t = t
        self.leds.tick()


class TestBehaviour(Phases):
    def test_it_starts_white_at_full_brightness_and_idle(self):
        self.leds.tick()
        self.assertEqual(self.client.shown, [(self.clock.t, WHITE)])
        self.assertEqual((self.leds.phase, self.leds.bright, self.leds.idle_since), ("idle", 1.0, self.clock.t))

    def test_the_card_over_50_percent_for_1_5_s_starts_the_rise(self):
        t0 = self.clock.t
        t = self.start_rise()
        self.assertAlmostEqual(t - t0, 1.5, delta=0.11)
        self.assertEqual(self.last_shown(), WHITE)                 # the rise starts from where it is

    def test_a_blip_over_50_or_50_itself_does_nothing(self):
        seq = self.run_for(1.2, gpu=100)                           # 1.2 s: under the 1.5 s
        seq += self.run_for(10, gpu=0)
        seq += self.run_for(10, gpu=50)
        self.assertTrue(all(c == WHITE for _t, c in seq))
        self.assertEqual(self.leds.phase, "idle")

    def test_the_rise_takes_5_s_through_yellow_and_orange_eased_with_full_brightness(self):
        t0 = self.start_rise()
        names, xs = [], []
        while self.clock.t < t0 + 6:
            self.clock.t += 0.05
            self.leds.tick()
            xs.append((self.clock.t - t0, self.leds.x))
            names.append(o1leds.color_name(self.leds.x))
        x_at = lambda s: min(xs, key=lambda p: abs(p[0] - s))[1]
        self.assertLess(x_at(0.5), 0.05)                           # eased in
        self.assertAlmostEqual(x_at(2.5), 0.5, delta=0.03)         # half way at half time
        self.assertGreater(x_at(4.5), 0.95)                        # eased out
        self.assertNotEqual(o1leds.ramp(x_at(4.8)), RED)
        self.assertEqual(o1leds.ramp(x_at(5.0)), RED)
        self.assertEqual(self.leds.phase, "working")
        order = [n for i, n in enumerate(names) if i == 0 or names[i - 1] != n]
        self.assertEqual(order, ["white", "yellow", "orange", "red"])
        self.assertTrue(all(b >= a for (_s, a), (_t, b) in zip(xs, xs[1:])))     # never back
        self.assertEqual(self.last_shown(), RED)

    def test_from_dimmed_the_brightness_comes_back_over_the_same_5_s(self):
        self.run_for(312, step=0.25)
        self.assertEqual(self.last_shown(), (102, 102, 102))       # dimmed idle: white at 40%
        t0 = self.start_rise()
        self.at(t0 + 2.5)
        self.assertAlmostEqual(self.leds.bright, 0.7, delta=0.02)  # 0.4 + 0.6 * ease(0.5)
        self.at(t0 + 5.0)
        self.assertEqual((self.leds.bright, self.last_shown()), (1.0, RED))

    def test_working_holds_red_at_100_and_sends_only_the_keepalive(self):
        self.to_red()
        self.client.shown.clear()
        self.run_for(10)
        self.assertEqual(len(self.client.shown), 5)                # one per 2 s poll
        self.assertTrue(all(c == RED for _t, c in self.client.shown))

    def test_a_card_that_jitters_99_0_while_working_stays_red(self):
        self.to_red()
        for i in range(40):                                        # every 0.25 s sample: 99, 0, 99, 0 ...
            self.probes.v["gpu_busy"] = 99 if i % 2 == 0 else 0
            self.run_for(0.25)
        self.assertEqual((self.leds.phase, self.last_shown()), ("working", RED))

    def test_the_cool_down_is_120_s_linear_in_time_red_orange_yellow_white(self):
        self.to_red()
        t0 = self.start_cool()
        for s, want in ((0, RED), (30, o1leds.ramp(0.75)), (40, (255, 128, 0)), (60, o1leds.ramp(0.5)),
                        (80, (255, 255, 0)), (119, o1leds.ramp(1 / 120)), (120, WHITE)):
            self.at(t0 + s, step=0.25)
            self.assertEqual(self.leds.rgb(), want, s)
            self.assertAlmostEqual(self.leds.x, max(0.0, 1 - s / 120.0), delta=1e-6)
        self.assertEqual(self.leds.phase, "idle")
        self.assertEqual(self.last_shown(), WHITE)

    def test_the_cool_down_starts_1_5_s_after_the_card_drops(self):
        self.to_red()
        self.probes.v["gpu_busy"] = 0
        t0 = self.clock.t
        t = self.until(lambda: self.leds.phase == "cooling")
        self.assertAlmostEqual(t - t0, 1.5, delta=0.26)

    def test_work_again_mid_cool_goes_back_to_red_in_5_s_from_where_it_is(self):
        self.to_red()
        t0 = self.start_cool()
        self.at(t0 + 60, step=0.25)
        self.assertEqual(self.leds.rgb(), o1leds.ramp(0.5))
        t1 = self.start_rise()
        x0 = self.leds.x0
        self.assertAlmostEqual(x0, 0.5 - (t1 - t0 - 60) / 120.0, delta=0.002)
        self.at(t1 + 2.5)
        self.assertAlmostEqual(self.leds.x, x0 + (1 - x0) * 0.5, delta=0.002)
        self.at(t1 + 4.5)
        self.assertNotEqual(self.leds.rgb(), RED)
        self.at(t1 + 5.0)
        self.assertEqual((self.leds.phase, self.leds.rgb()), ("working", RED))
        t2 = self.start_cool()                                     # and the next cool-down is the whole 120 s again
        self.at(t2 + 119.5, step=0.25)
        self.assertEqual(self.leds.phase, "cooling")
        self.at(t2 + 120, step=0.25)
        self.assertEqual(self.leds.phase, "idle")

    def test_work_that_ends_mid_rise_cools_from_where_it_got_to(self):
        t0 = self.start_rise()
        self.at(t0 + 2.5)
        x = self.leds.x
        t1 = self.start_cool()
        self.assertLess(self.leds.x0, 1.0)
        self.assertGreater(self.leds.x0, x)
        self.at(t1 + 30, step=0.25)
        self.assertAlmostEqual(self.leds.x, self.leds.x0 - 0.25, delta=1e-6)
        self.at(t1 + self.leds.x0 * 120 + 0.25, step=0.25)
        self.assertEqual((self.leds.phase, self.leds.rgb()), ("idle", WHITE))

    def test_lights_follow_the_card_only_not_a_request_a_tool_or_the_processors(self):
        self.probes.v.update(inflight=3, tools=["stability-test.sh", "setup.sh"], cpu=(900, 1000), cpu_c=75)
        seq = self.run_for(20, gpu=0)
        self.assertTrue(all(c == WHITE for _t, c in seq))

    def test_no_card_reading_is_white_and_says_so(self):
        for bad in (None, "x", True, float("nan")):
            self.probes.v["gpu_busy"] = bad
            seq = self.run_for(3)
            self.assertTrue(all(c == WHITE for _t, c in seq), bad)
        self.assertIsNone(self.leds.gpu)
        self.leds.probes["gpu_busy"] = lambda: 1 / 0
        self.assertTrue(all(c == WHITE for _t, c in self.run_for(3)))

    def test_a_lost_reading_while_working_cools_down(self):
        self.to_red()
        self.probes.v["gpu_busy"] = None
        self.until(lambda: self.leds.phase == "cooling", limit=3)

    def test_frames_go_out_only_on_a_change_and_at_most_25_a_second_during_the_rise(self):
        self.leds.tick()
        t0 = self.start_rise()
        self.client.shown.clear()
        self.at(t0 + 5.0, step=0.01)                               # a fast clock: the loop would tick at 100 Hz
        frames = [c for t, c in self.client.shown]
        self.assertGreater(len(frames), 60)                        # continuous: a frame for each change
        dup = len([1 for a, b in zip(frames, frames[1:]) if a == b])
        self.assertLessEqual(dup, 3)                               # only the 2 s keepalive repeats one

    def test_the_loop_ticks_at_25_hz_while_moving_and_slowly_otherwise(self):
        self.leds.tick()
        self.assertGreater(self.leds.tick(), o1leds.FRAME_S)       # idle white: the sample's pace
        self.start_rise()
        self.assertEqual(self.leds.tick(), o1leds.FRAME_S)
        self.until(lambda: self.leds.phase == "working")
        self.clock.t += 0.01
        w = self.leds.tick()
        self.assertGreater(w, o1leds.FRAME_S)
        self.assertLessEqual(w, o1leds.SAMPLE_S)
        self.start_cool()
        self.assertEqual(self.leds.tick(), o1leds.FRAME_S)         # the cool-down moves the whole 120 s

    def test_the_cool_down_sends_each_change_without_repeats(self):
        self.to_red()
        self.start_cool()
        self.client.shown.clear()
        while self.leds.phase == "cooling":
            self.clock.t += self.leds.tick()
        cols = [c for _t, c in self.client.shown]
        self.assertGreater(len(cols), 300)                         # red -> white is about 510 RGB steps
        self.assertLessEqual(len([1 for a, b in zip(cols, cols[1:]) if a == b]), 61)  # the keepalives only
        self.assertEqual(cols[-1], WHITE)

    def test_the_card_is_sampled_every_quarter_second_not_every_tick(self):
        calls = []
        self.leds.probes["gpu_busy"] = lambda: calls.append(self.clock.t) or 0
        self.run_for(2)
        self.assertIn(len(calls), (7, 8))                           # 0, .25, ... (a float step may slip one)

    def test_the_sampling_keeps_its_beat_under_25_hz_frames(self):
        calls = []
        self.leds.probes["gpu_busy"] = lambda: calls.append(self.clock.t) or 0
        self.run_for(10, step=0.04)
        self.assertGreaterEqual(len(calls), 39)                     # 4 a second, not one per 0.28 s

    def test_a_stop_sets_white_first(self):
        self.to_red()
        self.leds.shutdown()
        self.assertEqual(self.last_shown(), WHITE)
        self.assertFalse(self.client.connected)

    def test_a_wake_connects_afresh_and_starts_white_at_full_brightness(self):
        self.run_for(312, step=0.25)
        self.assertEqual(self.leds.bright, 0.4)
        self.assertEqual(self.client.opens, 1)
        self.leds.resync = True
        self.leds.tick()
        self.assertEqual((self.client.opens, self.client.closes), (2, 1))
        self.assertEqual((self.leds.phase, self.leds.bright, self.leds.idle_since), ("idle", 1.0, self.clock.t))
        self.assertEqual(self.last_shown(), WHITE)
        self.clock.t += 0.25
        self.run_for(298, step=0.25)
        self.assertEqual(self.leds.bright, 1.0)
        self.run_for(20, step=0.25)
        self.assertEqual(self.leds.bright, 0.4)

    def test_a_wake_while_working_goes_red_again_through_the_rise(self):
        self.to_red()
        self.leds.resync = True
        self.leds.tick()
        self.assertEqual(self.last_shown(), WHITE)                 # afresh
        self.until(lambda: self.leds.phase == "working", limit=8)
        self.assertEqual(self.last_shown(), RED)

    def test_a_reconnect_sends_the_colour_it_has_now(self):
        t0 = self.start_rise()
        self.at(t0 + 2.5)
        mid = self.leds.rgb()
        self.client.connected = False                              # the link dropped
        self.leds.retry_at = 0
        self.run_for(2.1)
        self.assertGreaterEqual(self.client.opens, 2)
        self.assertNotEqual(mid, WHITE)
        self.assertEqual(self.last_shown(), self.leds.rgb())


class TestDimming(Phases):
    """White for 300 s, then 1.0 to 0.4 over 10 s; the 300 s count from the moment it is white."""

    def test_dimming_starts_at_300_s_idle_and_takes_10_s(self):
        self.run_for(300, step=0.25)                                  # the last tick is at 299.75 s
        self.assertEqual(self.leds.bright, 1.0)
        self.run_for(0.25, step=0.25)                                 # the tick at exactly 300 s
        self.assertLess(self.leds.bright, 1.0)
        self.run_for(5, step=0.25)
        self.assertAlmostEqual(self.leds.bright, 0.7, delta=0.02)
        self.run_for(5, step=0.25)
        self.assertEqual(self.leds.bright, 0.4)
        self.assertEqual(self.last_shown(), (102, 102, 102))
        self.run_for(60, step=0.25)
        self.assertEqual(self.leds.bright, 0.4)                       # and no further

    def test_the_300_s_count_from_the_end_of_the_cool_down_not_from_the_end_of_the_work(self):
        self.to_red()
        t0 = self.start_cool()
        self.at(t0 + 120, step=0.25)
        self.assertEqual(self.leds.phase, "idle")
        self.assertAlmostEqual(self.leds.idle_since, t0 + 120, delta=1e-6)
        self.at(t0 + 120 + 299.75, step=0.25)
        self.assertEqual(self.leds.bright, 1.0)
        self.at(t0 + 120 + 300.25, step=0.25)
        self.assertLess(self.leds.bright, 1.0)

    def test_no_dimming_while_working_or_cooling(self):
        self.to_red()
        self.run_for(400, step=0.25)
        self.assertEqual(self.leds.bright, 1.0)
        t0 = self.start_cool()
        while self.clock.t < t0 + 119:
            self.run_for(1, step=0.25)
            self.assertEqual(self.leds.bright, 1.0)

    def test_nothing_but_the_card_brightens_it_again(self):
        self.run_for(312, step=0.25)
        self.probes.v.update(inflight=2, tools=["stability-test.sh"], cpu=(990, 1000))
        self.run_for(20, step=0.25, gpu=40)
        self.assertEqual(self.leds.bright, 0.4)
        self.run_for(1, step=0.25, gpu=100)                          # a 1 s blip: not yet
        self.run_for(1, step=0.25, gpu=0)
        self.assertEqual(self.leds.bright, 0.4)

    def test_the_resent_colour_includes_the_brightness(self):
        self.run_for(312, step=0.25)
        self.client.shown.clear()
        self.run_for(6, step=0.25)
        self.assertGreaterEqual(len(self.client.shown), 2)            # the keepalive
        self.assertTrue(all(c == (102, 102, 102) for _t, c in self.client.shown))

    def test_a_reconnect_sends_the_dimmed_colour(self):
        self.run_for(312, step=0.25)
        self.client.connected = False
        self.leds.retry_at = 0
        self.client.shown.clear()
        self.run_for(3, step=0.25)
        self.assertEqual(self.last_shown(), (102, 102, 102))

    def test_the_status_says_what_it_is_doing(self):
        self.client.devices = [type("D", (), {"name": "MSI", "vendor": "MSI", "nleds": 6, "kind": "leds",
                                              "modes": [type("M", (), {"name": "Direct"})()], "mode": 0,
                                              "zones": []})()]

        def st():
            with open(o1leds.status_path()) as f:
                return json.load(f)
        self.leds.tick()
        self.assertEqual(st()["line"], "Lights: white, idle (card 0% busy)  -  1 device")
        self.probes.v["gpu_busy"] = 87
        self.until(lambda: self.leds.phase == "rising")
        self.assertEqual(st()["line"], "Lights: white, turning red (card 87% busy)  -  1 device")
        self.until(lambda: self.leds.phase == "working")
        self.assertEqual((st()["phase"], st()["working"]), ("working", True))
        self.assertEqual(st()["line"], "Lights: red, working (card 87% busy)  -  1 device")
        t0 = self.start_cool()
        self.at(t0 + 58, step=0.25)
        self.leds.write_status(self.clock.t)
        self.assertEqual(st()["line"], "Lights: orange, cooling down (white in 62 s)  -  1 device")
        self.assertEqual(st()["cool_left"], 62)
        self.at(t0 + 120 + 305, step=0.25)
        self.run_for(1, step=0.25)
        s = st()
        self.assertLess(s["brightness"], 1.0)
        self.assertEqual(s["line"], "Lights: white, dimming (idle 5 min)  -  1 device")
        self.run_for(60, step=0.25)
        s = st()
        self.assertEqual((s["brightness"], s["rgb"]), (0.4, [102, 102, 102]))
        self.assertEqual(s["line"], "Lights: white, dimmed 40% (idle 6 min)  -  1 device")


class TestTheCardReadingIsTheSharedOne(unittest.TestCase):
    def test_the_service_reads_the_card_through_the_probes_the_fans_use(self):
        self.assertEqual(set(o1work.probes()), {"gpu_busy"})
        self.assertIs(o1fan.Gpu, o1work.Gpu)
        with open(os.path.join(U.LIB, "o1leds.py")) as f:
            src = f.read()
        self.assertIn("Leds(o1work.probes(), log=log)", src)
        self.assertIn('self.probes["gpu_busy"]()', src)
        self.assertIn("self.trigger = o1work.GpuTrigger()", src)    # the fans' own trigger, not a copy

    def test_the_cpu_temperature_does_not_turn_the_lights_red(self):
        with open(os.path.join(U.LIB, "o1leds.py")) as f:
            src = f.read()
        self.assertNotIn("CpuTrigger", src)
        self.assertNotIn("read_temps", src)


# ---- the server dying and coming back -----------------------------------------------------

class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.srv = F.FakeServer(two_devices()).start()
        self.addCleanup(self.srv.stop)
        self.clock = FakeClock()
        self.probes, self.lines = Probes(self.clock), []
        self.client = o1leds.Client(port=self.srv.port)
        self.addCleanup(self.client.close)
        self.leds = o1leds.Leds(self.probes.dict(), client=self.client, clock=self.clock, wall=lambda: 1_700_000_000,
                                log=self.lines.append, poll_s=0.5)
        self.addCleanup(lambda: os.path.exists(o1leds.status_path()) and os.unlink(o1leds.status_path()))

    def run_for(self, seconds, step=0.05):
        for _ in range(int(round(seconds / step))):
            self.leds.tick()
            self.clock.t += step

    def status(self):
        with open(o1leds.status_path()) as f:
            return json.load(f)

    def test_it_lights_the_real_protocol_end_to_end_and_the_status_says_so(self):
        self.run_for(1)
        self.assertTrue(self.leds.client.connected)
        got = self.srv.wait_for(1050)
        self.assertEqual(F.decode_leds(got[-1][1])[0][0], WHITE)
        st = self.status()
        self.assertEqual((st["state"], st["rgb"], st["connected"], st["error"]), ("white", [255, 255, 255], True, None))
        self.assertEqual([d["name"] for d in st["devices"]], ["MSI MYSTIC LIGHT", "Corsair Hydro Platinum"])
        self.assertEqual([d["mode"] for d in st["devices"]], ["Direct", "Direct"])
        self.assertIn("2 devices", st["line"])
        self.probes.v["gpu_busy"] = 100
        self.run_for(7)                                                    # 1.5 s to start, 5 s to red
        self.srv.clear()
        self.run_for(1)
        self.assertEqual(set(F.decode_leds(self.srv.wait_for(1050)[-1][1])[0]), {RED})
        st = self.status()
        self.assertEqual((st["rgb"], st["state"], st["gpu_pct"], st["phase"]), ([255, 0, 0], "red", 100, "working"))
        self.assertEqual(st["line"], "Lights: red, working (card 100% busy)  -  2 devices")

    def test_when_the_server_dies_it_says_so_keeps_going_and_reconnects_with_the_current_colour(self):
        self.probes.v["gpu_busy"] = 100
        self.run_for(7)
        self.srv.stop()
        self.run_for(1.5)
        st = self.status()
        self.assertFalse(st["connected"])
        self.assertTrue(st["error"])
        self.assertTrue(st["line"].startswith("Lights: OpenRGB:"))
        self.assertTrue(any(l.startswith("OpenRGB:") for l in self.lines))
        self.assertEqual(self.leds.rgb(), RED)                             # the colour ran on, unharmed
        again = F.FakeServer(two_devices(), port=self.srv.port).start()
        self.addCleanup(again.stop)
        self.run_for(40)                                                   # past any pause
        self.assertTrue(self.client.connected)
        got = again.wait_for(1050)
        self.assertEqual(set(F.decode_leds(got[-1][1])[0]), {RED})         # red again, as it was
        self.assertIsNone(self.status()["error"])
        self.assertTrue(any(l.startswith("connected to OpenRGB") for l in self.lines))

    def test_an_error_is_logged_once_not_every_try(self):
        self.srv.stop()
        self.run_for(30)
        self.assertEqual(len([l for l in self.lines if l.startswith("OpenRGB:")]), 1)


class TestNeverCrashLoops(Rig):
    def test_a_server_that_stays_away_is_tried_with_a_growing_pause_up_to_30_s(self):
        self.client.fail_open = True
        self.run_for(300, step=0.5)
        self.assertLessEqual(self.client.opens, 14)                      # 2+4+8+16+30... not one per poll
        self.assertGreaterEqual(self.client.opens, 8)
        gaps = []
        self.client.opens = 0
        t = self.clock.t
        for _ in range(240):
            before = self.client.opens
            self.leds.tick()
            self.clock.t += 0.5
            if self.client.opens != before:
                gaps.append(self.clock.t - 0.5 - t)
                t = self.clock.t - 0.5
        self.assertTrue(all(abs(g - o1leds.RETRY_MAX_S) < 1.0 for g in gaps[1:]), gaps)

    def test_a_failed_send_drops_the_link_and_retries_later(self):
        self.leds.tick()
        self.client.fail_show = True
        self.probes.v["gpu_busy"] = 100
        for _ in range(100):                                     # the rise sends a frame: it fails
            self.clock.t += 0.05
            self.leds.tick()
            if not self.client.connected:
                break
        self.assertFalse(self.client.connected)
        self.assertIn("Broken pipe", self.leds.err)
        self.client.fail_show = False
        self.run_for(10)
        self.assertTrue(self.client.connected)
        self.assertIsNone(self.leds.err)

    def test_the_loop_survives_a_bug_in_a_tick_and_still_pings_the_watchdog(self):
        sent, lines = [], []

        class Bad:
            phase = "idle"

            def tick(self):
                raise ValueError("secret request text")
        wait = o1leds.service_step(Bad(), notify=sent.append, log=lines.append, first=True)
        self.assertEqual(wait, o1leds.POLL_S)
        self.assertEqual(lines, ["tick failed: ValueError"])                # the kind, never what it held
        self.assertTrue(sent[0].startswith("READY=1\nWATCHDOG=1"))


# ---- status text ----------------------------------------------------------------------------

class TestStatus(Rig):
    def read(self):
        with open(o1leds.status_path()) as f:
            return json.load(f)

    def test_the_colour_card_load_and_line_follow_the_card(self):
        self.client.devices = [type("D", (), {"name": "MSI", "vendor": "MSI", "nleds": 6, "kind": "leds",
                                              "modes": [type("M", (), {"name": "Direct"})()], "mode": 0,
                                              "zones": []})()]
        self.leds.tick()
        st = self.read()
        self.assertEqual((st["state"], st["phase"], st["rgb"], st["gpu_pct"], st["intensity"], st["working"]),
                         ("white", "idle", [255, 255, 255], 0, 0, False))
        self.assertEqual(st["line"], "Lights: white, idle (card 0% busy)  -  1 device")
        self.assertEqual(st["why"], "idle (card 0% busy)")
        self.probes.v["gpu_busy"] = None
        self.run_for(2.5)
        self.leds.tick()
        self.assertEqual(self.read()["line"], "Lights: white, idle (no card reading)  -  1 device")
        self.assertFalse(self.read()["gpu_reading"])

    def test_the_text_for_a_person(self):
        st = {"at": 1, "state": "orange", "phase": "cooling", "rgb": [255, 128, 0], "gpu_pct": 3.4, "intensity": 0.67,
              "brightness": 1.0, "cool_left": 80, "working": False, "idle_s": 0, "connected": True, "protocol": 4,
              "error": None,
              "devices": [{"name": "MSI MYSTIC LIGHT", "vendor": "MSI", "leds": 6, "mode": "Direct", "usable": True},
                          {"name": "Odd", "vendor": "", "leds": 3, "mode": None, "usable": False}]}
        out = o1leds.render_status(st)
        self.assertIn("lights: orange  now 255,128,0 (FF8000)  -  cooling down (white in 80 s)", out)
        self.assertIn("card: 3% busy  -  not working  -  position 0.67 of 1 (0 white, 1 red)", out)
        self.assertIn("brightness: 100%  -  idle 0 s (dims to 40% after 300 s)", out)
        self.assertIn("2 devices found", out)
        self.assertIn("MSI MYSTIC LIGHT", out)
        self.assertIn("mode Direct, 6 LEDs", out)
        self.assertIn("no usable mode", out)
        self.assertIn("errors: none", out)
        st.update(connected=False, error="Connection refused", devices=[], gpu_pct=None, phase="working", working=True)
        out = o1leds.render_status(st)
        self.assertIn("not connected", out)
        self.assertIn("errors: Connection refused", out)
        self.assertIn("no reading (taken as not working)", out)
        self.assertIn("working (over 50%)", out)
        self.assertIn("isn't running", o1leds.render_status(None))

    def test_the_panel_line_only_while_the_service_is_fresh(self):
        path = os.path.join(U.PREFIX, "leds-test.json")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        o1leds.write_json_atomic(path, {"at": 1000, "line": "Lights: white, 100% (card 0% busy)  -  2 devices"}, mode=0o644)
        self.assertEqual(o1leds.panel_line(path, now=1005), "Lights: white, 100% (card 0% busy)  -  2 devices")
        self.assertIsNone(o1leds.panel_line(path, now=1000 + o1leds.STATUS_STALE_S + 1))
        self.assertIsNone(o1leds.panel_line(path + ".nope", now=1005))
        for st, line in (({"connected": False, "error": None}, "Lights: waiting for OpenRGB"),
                         ({"connected": True, "devices": []}, "Lights: OpenRGB lists no devices")):
            self.assertEqual(o1leds.status_line(st), line)

    def test_the_status_command_needs_no_root(self):
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-leds"), "status"], capture_output=True,
                           text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("isn't running", r.stdout)

    def test_the_other_commands_refuse_without_root(self):
        if os.geteuid() == 0:
            self.skipTest("root")
        for cmd in ("run", "openrgb", "setup"):
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-leds"), cmd], capture_output=True,
                               text=True, env={k: v for k, v in os.environ.items() if k != "OLLAMA1_PREFIX"})
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("sudo", r.stderr)


# ---- the server's command line and setup -----------------------------------------------------

class TestOpenrgbAndSetup(unittest.TestCase):
    def help(self, text):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, text, "")

    def test_the_server_is_started_headless_on_the_loopback_port(self):
        argv, bound = o1leds.openrgb_argv(run=self.help("--server\n--server-host ADDR\n--server-port PORT"),
                                          which=lambda n: "/usr/bin/openrgb")
        self.assertEqual(argv, ["/usr/bin/openrgb", "--server", "--server-host", "127.0.0.1", "--server-port", "6742"])
        self.assertTrue(bound)

    def test_without_the_host_flag_it_is_left_out_and_the_unit_filters_the_address(self):
        argv, bound = o1leds.openrgb_argv(run=self.help("--server\n--server-port PORT"), which=lambda n: "/usr/bin/openrgb")
        self.assertEqual(argv, ["/usr/bin/openrgb", "--server", "--server-port", "6742"])
        self.assertFalse(bound)
        argv, bound = o1leds.openrgb_argv(run=lambda *a, **k: (_ for _ in ()).throw(OSError("x")),
                                          which=lambda n: "/usr/bin/openrgb")
        self.assertFalse(bound)
        self.assertNotIn("0.0.0.0", argv)

    def test_missing_openrgb_waits_instead_of_looping(self):
        slept, lines = [], []
        rc = o1leds.run_openrgb(log=lines.append, sleep=slept.append, which=lambda n: None,
                                execv=lambda *a: self.fail("exec"))
        self.assertEqual((rc, slept), (1, [o1leds.NO_SERVER_WAIT_S]))
        self.assertIn("not installed", lines[0])

    def test_it_execs_the_server(self):
        calls = []
        o1leds.run_openrgb(log=lambda l: None, execv=lambda p, a: calls.append((p, a)),
                           run=self.help("--server-host"), which=lambda n: "/usr/bin/openrgb")
        self.assertEqual(calls[0][0], "/usr/bin/openrgb")
        self.assertIn("127.0.0.1", calls[0][1])

    def test_setup_on_installs_the_package_only_when_missing_and_starts_both_units(self):
        calls, apt, lines = [], [], []
        sc = lambda *a: calls.append(a) or True
        self.assertEqual(o1leds.setup("on", systemctl=sc, which=lambda n: None, apt=lambda p: apt.append(p) or True,
                                      log=lines.append), 0)
        self.assertEqual(apt, ["openrgb"])
        self.assertEqual(calls, [("enable", "ollama1-openrgb.service", "ollama1-leds.service"),
                                 ("restart", "ollama1-openrgb.service"), ("restart", "ollama1-leds.service")])
        apt.clear()
        o1leds.setup("on", systemctl=sc, which=lambda n: "/usr/bin/openrgb", apt=lambda p: apt.append(p), log=lines.append)
        self.assertEqual(apt, [])

    def test_setup_on_with_a_failed_install_enables_nothing(self):
        calls = []
        rc = o1leds.setup("on", systemctl=lambda *a: calls.append(a) or True, which=lambda n: None, apt=lambda p: False,
                          log=lambda l: None)
        self.assertEqual((rc, calls), (1, []))

    def test_setup_on_reports_a_unit_that_would_not_start(self):
        self.assertEqual(o1leds.setup("on", systemctl=lambda *a: False, which=lambda n: "x", log=lambda l: None), 1)

    def test_setup_off_stops_and_disables_both_and_removes_nothing(self):
        calls, apt = [], []
        self.assertEqual(o1leds.setup("off", systemctl=lambda *a: calls.append(a) or True,
                                      apt=lambda p: apt.append(p), log=lambda l: None), 0)
        self.assertEqual(calls, [("disable", "--now", "ollama1-leds.service"),
                                 ("disable", "--now", "ollama1-openrgb.service")])
        self.assertEqual(apt, [])


# ---- zones with no LEDs (6b422) ----------------------------------------------------------------

def board(jrainbow_max=200, jcorsair_max=40):
    """The server's board as OpenRGB lists it: JRGB1 and PIPE1 have one LED each, the three addressable headers
    have none but may grow; the cooler is fixed at 16."""
    msi = F.device("MSI MEG X570 ACE", 2,
                   [F.mode_spec("Off", F.FLAG_MODE_COLOR, 0), F.mode_spec("Direct", F.FLAG_PER_LED)], active=0,
                   vendor="MSI",
                   zones=[("JRGB1", 1, 1, 1, 1), ("JRAINBOW1", 0, 0, jrainbow_max, 1),
                          ("JRAINBOW2", 0, 0, jrainbow_max, 1), ("JCORSAIR", 0, 0, jcorsair_max, 1),
                          ("PIPE1", 1, 1, 1, 1)])
    cooler = F.device("Corsair Hydro Platinum", 16, [F.mode_spec("Direct", F.FLAG_PER_LED, 1, bri=(0, 100, 100))],
                      active=0, vendor="Corsair")
    return [msi, cooler]


def resizes(srv):
    """[(device, zone index, size)] of every RESIZEZONE packet the server got."""
    return [(d, *struct.unpack("<ii", b)) for d, b in srv.seen(1000)]


class TestZoneParsing(unittest.TestCase):
    def test_a_zone_carries_its_limits_count_and_type(self):
        for ver in (0, 1, 2, 3, 4):
            dev = o1leds.Device.parse(0, F.controller_data(board()[0], ver), ver)
            self.assertEqual([(z.name, z.ztype, z.leds_min, z.leds_max, z.leds) for z in dev.zones],
                             [("JRGB1", 1, 1, 1, 1), ("JRAINBOW1", 1, 0, 200, 0), ("JRAINBOW2", 1, 0, 200, 0),
                              ("JCORSAIR", 1, 0, 40, 0), ("PIPE1", 1, 1, 1, 1)], ver)
            self.assertEqual([z.resizable for z in dev.zones], [False, True, True, True, False])
            self.assertEqual(dev.nleds, 2)

    def test_the_three_zone_types_are_the_sdks(self):
        self.assertEqual((o1leds.ZONE_SINGLE, o1leds.ZONE_LINEAR, o1leds.ZONE_MATRIX), (0, 1, 2))
        d = F.device("X", 0, [F.mode_spec("Direct")], zones=[("S", 0, 0, 1, 0), ("L", 0, 0, 9, 1), ("M", 4, 4, 4, 2)])
        dev = o1leds.Device.parse(0, F.controller_data(d, 4), 4)
        self.assertEqual([z.ztype for z in dev.zones], [0, 1, 2])

    def test_the_packet_id_and_body_are_the_sdks(self):
        self.assertEqual(o1leds.PID_RESIZEZONE, 1000)


class TestTargetLength(unittest.TestCase):
    def z(self, leds=0, lo=0, hi=200, ztype=1):
        return o1leds.Zone("Z", ztype, lo, hi, leds)

    def test_an_empty_resizable_zone_gets_the_wanted_length(self):
        self.assertEqual(o1leds.target_length(self.z(), 60), 60)

    def test_it_is_clamped_to_the_zones_maximum_and_minimum(self):
        self.assertEqual(o1leds.target_length(self.z(hi=40), 60), 40)
        self.assertEqual(o1leds.target_length(self.z(lo=10, hi=40), 5), 10)
        self.assertEqual(o1leds.target_length(self.z(lo=10, hi=40), 25), 25)

    def test_a_zone_that_has_leds_is_never_touched(self):
        self.assertIsNone(o1leds.target_length(self.z(leds=1, lo=1, hi=200), 60))      # never grown
        self.assertIsNone(o1leds.target_length(self.z(leds=100, lo=0, hi=200), 10))    # never shrunk

    def test_a_zone_that_cannot_grow_is_left(self):
        self.assertIsNone(o1leds.target_length(self.z(lo=0, hi=0), 60))               # no maximum
        self.assertIsNone(o1leds.target_length(self.z(lo=5, hi=5), 60))               # fixed
        self.assertIsNone(o1leds.target_length(self.z(lo=0, hi=200, ztype=o1leds.ZONE_MATRIX), 60))

    def test_the_default_is_60_and_the_option_ceiling_1024(self):
        self.assertEqual((o1leds.DEFAULT_LENGTH, o1leds.LENGTH_MAX), (60, 1024))


class TestLengthSetting(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(U.PREFIX, "leds-length-test.json")
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_no_file_is_the_default(self):
        self.assertEqual(o1leds.configured_length(self.path), 60)

    def test_a_saved_length_is_read(self):
        self.assertEqual(o1leds.save_length("90", self.path), 90)
        self.assertEqual(o1leds.configured_length(self.path), 90)
        self.assertEqual(o1leds.save_length(1, self.path), 1)
        self.assertEqual(o1leds.save_length(1024, self.path), 1024)
        self.assertEqual(o1leds.configured_length(self.path), 1024)

    def test_a_bad_length_is_refused_and_writes_nothing(self):
        for bad in ("0", "-3", "1025", "ten", "", "6.5", None):
            self.assertIsNone(o1leds.save_length(bad, self.path), bad)
        self.assertFalse(os.path.exists(self.path))

    def test_a_damaged_file_is_the_default(self):
        for text in ("{", "[]", '{"length": "90"}', '{"length": 0}', '{"length": 5000}', '{"length": true}', '{"length": 6.5}'):
            with open(self.path, "w") as f:
                f.write(text)
            self.assertEqual(o1leds.configured_length(self.path), 60, text)

    def test_the_client_reads_the_saved_length_at_each_open(self):
        o1leds.save_length(25, self.path)
        srv = F.FakeServer(board()).start()
        self.addCleanup(srv.stop)
        c = o1leds.Client(port=srv.port)
        self.addCleanup(c.close)
        from unittest import mock
        with mock.patch.object(o1leds, "config_path", lambda: self.path):
            c.open()
        self.assertEqual([n for _d, _z, n in resizes(srv)], [25, 25, 25])

    def test_setup_saves_a_length_and_refuses_a_bad_one(self):
        from unittest import mock
        lines = []
        with mock.patch.object(o1leds, "config_path", lambda: self.path):
            self.assertEqual(o1leds.setup("on", systemctl=lambda *a: True, which=lambda n: "x", log=lines.append, length="75"), 0)
            self.assertEqual(o1leds.configured_length(), 75)
            calls = []
            self.assertEqual(o1leds.setup("on", systemctl=lambda *a: calls.append(a) or True, which=lambda n: "x",
                                          log=lines.append, length="0"), 1)
            self.assertEqual(calls, [])
            self.assertEqual(o1leds.configured_length(), 75)                     # left as it was
            self.assertEqual(o1leds.setup("on", systemctl=lambda *a: True, which=lambda n: "x", log=lines.append), 0)
            self.assertEqual(o1leds.configured_length(), 75)                     # no length given: the saved one stays
        self.assertTrue(any("1 to 1024" in l for l in lines))


class ZoneCase(unittest.TestCase):
    def setUp(self):
        self.srv = F.FakeServer(board()).start()
        self.addCleanup(self.srv.stop)
        self.client = o1leds.Client(port=self.srv.port, length=60)
        self.addCleanup(self.client.close)


class TestResize(ZoneCase):
    def test_every_empty_resizable_zone_is_resized_clamped_and_nothing_else(self):
        self.client.open()
        self.assertEqual(resizes(self.srv), [(0, 1, 60), (0, 2, 60), (0, 3, 40)])           # JCORSAIR's own maximum is 40
        msi, cooler = self.client.devices
        self.assertEqual([(z.name, z.leds) for z in msi.zones],
                         [("JRGB1", 1), ("JRAINBOW1", 60), ("JRAINBOW2", 60), ("JCORSAIR", 40), ("PIPE1", 1)])
        self.assertEqual(msi.nleds, 2 + 60 + 60 + 40)
        self.assertEqual((cooler.nleds, [z.leds for z in cooler.zones]), (16, [16]))
        self.assertEqual(self.client.notes, ["lights: resized JRAINBOW1 to 60 LEDs", "lights: resized JRAINBOW2 to 60 LEDs",
                                             "lights: resized JCORSAIR to 40 LEDs"])

    def test_the_packet_is_a_header_for_the_device_and_two_little_endian_ints(self):
        self.client.open()
        got = [(d, b) for d, p, b in self.srv.packets if p == 1000]
        self.assertEqual(got[0], (0, struct.pack("<ii", 1, 60)))
        self.assertEqual(len(got[0][1]), 8)                                               # no size prefix
        self.assertEqual(o1leds.packet(0, 1000, struct.pack("<ii", 1, 60)),
                         b"ORGB" + struct.pack("<IIII", 0, 1000, 8, 1) + struct.pack("<i", 60))

    def test_the_controller_data_is_read_again_after_the_resize(self):
        self.client.open()
        order = [(d, p) for d, p, _b in self.srv.packets if p in (1, 1000)]
        self.assertEqual(order, [(0, 1), (0, 1000), (0, 1000), (0, 1000), (0, 1), (1, 1)])

    def test_a_zone_that_has_leds_is_not_touched_even_when_it_could_grow(self):
        srv = F.FakeServer([F.device("Board", 5, [F.mode_spec("Direct")], zones=[("Grown", 5, 1, 100, 1)])]).start()
        self.addCleanup(srv.stop)
        c = o1leds.Client(port=srv.port, length=60)
        self.addCleanup(c.close)
        c.open()
        self.assertEqual(srv.seen(1000), [])
        self.assertEqual(c.devices[0].nleds, 5)

    def test_the_wanted_length_is_what_is_asked_for(self):
        for want, got in ((1, 1), (200, 200), (500, 200)):
            srv = F.FakeServer(board()).start()
            self.addCleanup(srv.stop)
            c = o1leds.Client(port=srv.port, length=want)
            self.addCleanup(c.close)
            c.open()
            self.assertEqual([n for _d, z, n in resizes(srv) if z == 1], [got], want)

    def test_a_zone_the_server_ignores_is_noted_and_the_rest_still_grow(self):
        self.srv.resize_deaf = {(0, 2)}                                                    # JRAINBOW2
        self.client.open()
        msi = self.client.devices[0]
        self.assertEqual([(z.name, z.leds) for z in msi.zones],
                         [("JRGB1", 1), ("JRAINBOW1", 60), ("JRAINBOW2", 0), ("JCORSAIR", 40), ("PIPE1", 1)])
        self.assertEqual(self.client.notes[0], "lights: resized JRAINBOW1 to 60 LEDs")
        self.assertIn("lights: could not resize JRAINBOW2 on MSI MEG X570 ACE", self.client.notes[1])
        self.assertEqual(self.client.notes[2], "lights: resized JCORSAIR to 40 LEDs")
        self.assertTrue(self.client.connected)

    def test_a_send_that_fails_on_one_zone_does_not_stop_the_others(self):
        real = socket.create_connection

        class Flaky:
            def __init__(self, s):
                self.s = s

            def sendall(self, data):
                if data[8:12] == struct.pack("<I", 1000) and data[16:20] == struct.pack("<i", 1):
                    raise BrokenPipeError(32, "Broken pipe")
                return self.s.sendall(data)

            def __getattr__(self, name):
                return getattr(self.s, name)

        c = o1leds.Client(port=self.srv.port, length=60, connect=lambda *a, **k: Flaky(real(*a, **k)))
        self.addCleanup(c.close)
        c.open()
        self.assertEqual([(z.name, z.leds) for z in c.devices[0].zones],
                         [("JRGB1", 1), ("JRAINBOW1", 0), ("JRAINBOW2", 60), ("JCORSAIR", 40), ("PIPE1", 1)])
        self.assertTrue(any(n.startswith("lights: could not resize JRAINBOW1") for n in c.notes))

    def test_a_refusing_server_is_asked_once_not_on_every_device_list_notice(self):
        self.srv.resize_deaf = {(0, 1), (0, 2), (0, 3)}
        self.client.open()
        self.client.poll()
        self.srv.notify_list_updated()
        time.sleep(0.1)
        self.client.poll()
        self.assertEqual(len(resizes(self.srv)), 3)

    def test_after_a_reopen_it_asks_again(self):
        self.srv.resize_deaf = {(0, 1), (0, 2), (0, 3)}
        self.client.open()
        self.srv.resize_deaf = set()
        self.client.open()
        self.assertEqual(len(resizes(self.srv)), 6)
        self.assertEqual(self.client.devices[0].nleds, 2 + 60 + 60 + 40)

    def test_a_device_that_comes_back_empty_is_resized_again(self):
        self.client.open()
        self.srv.clear()
        for zi in (1, 2, 3):                                                               # the board was re-plugged
            dev = self.srv.devices[0]
            name, _n, lo, hi, zt = dev["zones"][zi]
            dev["zones"][zi] = (name, 0, lo, hi, zt)
        self.srv.devices[0]["colors"] = [(0, 0, 0)] * 2
        self.srv.devices[0]["leds"] = 2
        self.srv.notify_list_updated()
        end = time.time() + 3
        while time.time() < end and len(resizes(self.srv)) < 3:
            time.sleep(0.02)
            self.client.poll()
        self.assertEqual(len(resizes(self.srv)), 3)


class TestZonesGetTheSameFrames(unittest.TestCase):
    def setUp(self):
        self.srv = F.FakeServer(board()).start()
        self.addCleanup(self.srv.stop)
        self.clock, self.lines = FakeClock(), []
        self.probes = Probes(self.clock)
        self.client = o1leds.Client(port=self.srv.port, length=60)
        self.addCleanup(self.client.close)
        self.leds = o1leds.Leds(self.probes.dict(), client=self.client, clock=self.clock, wall=lambda: 1_700_000_000,
                                log=self.lines.append, poll_s=0.5)
        self.addCleanup(lambda: os.path.exists(o1leds.status_path()) and os.unlink(o1leds.status_path()))

    def run_for(self, seconds, step=0.05):
        for _ in range(int(round(seconds / step))):
            self.leds.tick()
            self.clock.t += step

    def everywhere(self, color):
        """Every LED of both devices, in every zone, shows `color`."""
        end = time.time() + 3
        while True:
            seen = {c for d in (0, 1) for c in self.srv.devices[d]["colors"]}
            if seen == {tuple(color)} or time.time() > end:
                break
            time.sleep(0.01)
        self.assertEqual(seen, {tuple(color)})
        for zi in range(5):
            self.assertEqual(set(self.srv.zone_colors(0, zi)), {tuple(color)} if self.srv.zone_colors(0, zi) else set())
        self.assertEqual(len(self.srv.zone_colors(0, 1)), 60)
        self.assertEqual(len(self.srv.zone_colors(0, 2)), 60)
        self.assertEqual(len(self.srv.zone_colors(0, 3)), 40)

    def test_white_red_cooling_and_dim_reach_every_header_led_like_every_other(self):
        self.run_for(1)
        self.assertEqual([n for n in self.lines if "resized" in n],
                         ["lights: resized JRAINBOW1 to 60 LEDs", "lights: resized JRAINBOW2 to 60 LEDs",
                          "lights: resized JCORSAIR to 40 LEDs"])
        self.everywhere(WHITE)                                                             # idle
        self.probes.v["gpu_busy"] = 100
        self.run_for(3.5)                                                                   # 1.5 s to start, then rising
        self.assertEqual(self.leds.phase, "rising")
        self.everywhere(self.leds.rgb())
        self.run_for(4)
        self.assertEqual(self.leds.phase, "working")
        self.everywhere(RED)                                                                # working
        self.probes.v["gpu_busy"] = 0
        self.run_for(1.5 + 30)                                                              # cooling, part way down
        self.assertEqual(self.leds.phase, "cooling")
        self.assertNotIn(self.leds.rgb(), (WHITE, RED))
        self.everywhere(self.leds.rgb())
        self.run_for(100)
        self.assertEqual(self.leds.phase, "idle")
        self.everywhere(WHITE)
        self.run_for(o1leds.IDLE_DIM_S + o1leds.DIM_S + 5, step=0.25)                      # dimmed idle
        self.assertEqual(self.leds.rgb(), (102, 102, 102))
        self.everywhere((102, 102, 102))

    def test_the_same_frames_go_to_the_header_leds_and_the_cooler(self):
        self.run_for(1)
        self.probes.v["gpu_busy"] = 100
        self.run_for(8)
        by = {}
        for dev, frame in list(self.srv.frames):
            by.setdefault(dev, []).append(sorted(set(frame)))
        self.assertEqual(by[0][-1], by[1][-1])
        self.assertEqual(len(by[0]), len(by[1]))                                           # a frame for one is a frame for the other
        self.assertTrue(all(len(c) == 1 for c in by[0]))                                   # one colour over all LEDs, every frame
        self.assertEqual(len(self.srv.frames[0][1]) + len(self.srv.frames[1][1]), 162 + 16)

    def test_the_status_lists_every_zone_with_its_final_size(self):
        self.run_for(1)
        with open(o1leds.status_path()) as f:
            st = json.load(f)
        self.assertEqual(st["devices"][0]["zones"],
                         [{"name": "JRGB1", "leds": 1}, {"name": "JRAINBOW1", "leds": 60}, {"name": "JRAINBOW2", "leds": 60},
                          {"name": "JCORSAIR", "leds": 40}, {"name": "PIPE1", "leds": 1}])
        self.assertEqual(st["devices"][0]["leds"], 162)
        self.assertEqual(st["devices"][1]["zones"], [{"name": "Zone", "leds": 16}])
        out = o1leds.render_status(st)
        self.assertIn("zones: JRGB1 1, JRAINBOW1 60, JRAINBOW2 60, JCORSAIR 40, PIPE1 1", out)
        self.assertIn("162 LEDs", out)

    def test_the_log_says_it_once_per_zone_and_the_devices_line_has_the_new_count(self):
        self.run_for(3)
        self.assertEqual(len([l for l in self.lines if l.startswith("lights: resized")]), 3)
        self.assertTrue(any("MSI MEG X570 ACE (162 LEDs)" in l for l in self.lines if l.startswith("connected")))

    def test_a_zone_that_fails_leaves_the_service_up_and_the_others_lit(self):
        self.srv.resize_deaf = {(0, 2)}
        self.run_for(1)
        self.assertTrue(self.client.connected)
        self.assertTrue(any(l.startswith("lights: could not resize JRAINBOW2") for l in self.lines))
        self.assertEqual({tuple(c) for c in self.srv.devices[0]["colors"]}, {WHITE})
        self.assertEqual(len(self.srv.zone_colors(0, 1)), 60)
        self.assertEqual(len(self.srv.zone_colors(0, 2)), 0)
        self.assertEqual(len(self.srv.zone_colors(0, 3)), 40)
        with open(o1leds.status_path()) as f:
            self.assertEqual([z["leds"] for z in json.load(f)["devices"][0]["zones"]], [1, 60, 0, 40, 1])

    def test_a_server_restart_resizes_again(self):
        self.probes.v["gpu_busy"] = 100
        self.run_for(8)
        port = self.srv.port
        self.srv.stop()
        again = F.FakeServer(board(), port=port).start()                                   # OpenRGB restarted: zones empty again
        self.addCleanup(again.stop)
        self.run_for(40)
        self.assertTrue(self.client.connected)
        self.assertEqual(len(resizes(again)), 3)
        self.assertEqual(len([l for l in self.lines if l.startswith("lights: resized JRAINBOW1")]), 2)
        end = time.time() + 3
        while time.time() < end and {tuple(c) for c in again.devices[0]["colors"]} != {RED}:
            time.sleep(0.01)
        self.assertEqual({tuple(c) for c in again.devices[0]["colors"]}, {RED})            # red on the new LEDs too

    def test_a_wake_resizes_again_when_the_server_forgot(self):
        self.run_for(1)
        self.srv.clear()
        for zi in (1, 2, 3):
            name, _n, lo, hi, zt = self.srv.devices[0]["zones"][zi]
            self.srv.devices[0]["zones"][zi] = (name, 0, lo, hi, zt)
        self.srv.devices[0]["colors"] = [(0, 0, 0)] * 2
        self.srv.devices[0]["leds"] = 2
        self.leds.resync = True
        self.run_for(1)
        self.assertEqual(len(resizes(self.srv)), 3)
        self.everywhere(WHITE)


class TestZoneWiring(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(U.KIT, *p)) as f:
            return f.read()

    def bash(self, script):
        r = subprocess.run(["bash", "-c", ". %s; %s" % (shlex.quote(os.path.join(U.LIB, "setuplib.sh")), script)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def test_the_length_choice_is_the_flag_else_the_saved_value_else_nothing(self):
        for args, want in (('"" ""', ""), ('90 ""', "90"), ('"" 75', "75"), ('90 75', "90"), ('007 ""', "7"),
                           ('1 ""', "1"), ('1024 ""', "1024")):
            self.assertEqual(self.bash("leds_length_choice " + args), (0, want), args)

    def test_a_bad_length_fails(self):
        for args in ('0 ""', '1025 ""', 'ten ""', '-1 ""', '6.5 ""', '"" 0', '"" 5000', '"" ten', '12345 ""'):
            self.assertEqual(self.bash("leds_length_choice " + args)[0], 1, args)

    def test_the_plan_line_says_the_length(self):
        self.assertIn("set to 60 LEDs", self.bash('leds_plan on ""')[1])
        self.assertIn("set to 90 LEDs", self.bash("leds_plan on 90")[1])
        self.assertIn("--leds-length", self.bash("leds_plan on")[1])
        self.assertNotIn("LEDs so", self.bash("leds_plan off")[1])

    def test_the_flag_is_in_the_help_validated_and_saved(self):
        s = self.read("setup.sh")
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--help"], capture_output=True, text=True)
        self.assertIn("--leds-length N", r.stdout)
        self.assertIn("default 60", r.stdout)
        for args in (["--leds-length", "0"], ["--leds-length", "1025"], ["--leds-length", "lots"], ["--leds-length"]):
            r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh")] + args + ["--plan"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("--leds-length takes whole LEDs, 1 to 1024", r.stdout)
        self.assertIn('LEDS_LENGTH=$(leds_length_choice "$A_LEDS_LENGTH" "$(saved LEDS_LENGTH)")', s)
        self.assertIn("printf 'LEDS_LENGTH=%s\\n' \"$LEDS_LENGTH\"", s)                    # saved, only when there is one
        self.assertIn('setup "$LEDS" ${LEDS_LENGTH:+--length "$LEDS_LENGTH"}', s)           # and handed to the service's setup
        self.assertIn("|--leds-length) ;;", s)

    def test_the_cli_takes_length_and_refuses_the_rest(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for args in (["setup", "on", "--length"], ["setup", "on", "--len", "5"], ["setup", "on", "5"]):
            r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-leds")] + args, capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("--length N", r.stderr)
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-leds"), "setup", "on", "--length", "0"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 to 1024", r.stdout)

    def test_the_lights_unit_still_reads_only_and_writes_only_run(self):
        u = self.read("systemd", "ollama1-leds.service")
        self.assertIn("\nReadWritePaths=/run/ollama1\n", u)                                # the length is read from /etc, never written


# ---- the wiring -------------------------------------------------------------------------------

class TestWiring(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(U.KIT, *p)) as f:
            return f.read()

    def unit(self, name):
        with open(os.path.join(U.SYSTEMD, name)) as f:
            return f.read()

    def common(self, u):
        for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes",
                     "ProtectKernelModules=yes", "ProtectKernelLogs=yes", "ProtectControlGroups=yes", "LimitCORE=0",
                     "RestrictNamespaces=yes", "SystemCallArchitectures=native", "RestrictSUIDSGID=yes",
                     "LockPersonality=yes", "Restart=always", "StartLimitIntervalSec=0",
                     "IPAddressDeny=any", "IPAddressAllow=localhost", "WantedBy=multi-user.target"):
            self.assertIn("\n" + line + "\n", u, line)
        self.assertNotIn("0.0.0.0", u)
        self.assertNotIn("PrivateNetwork=yes", u)                          # it must reach 127.0.0.1

    def test_the_server_unit(self):
        u = self.unit("ollama1-openrgb.service")
        self.common(u)
        self.assertIn("\nExecStart=/usr/local/lib/ollama1/bin/ollama1-leds openrgb\n", u)
        self.assertIn("\nDevicePolicy=closed\n", u)
        self.assertIn("\nDeviceAllow=char-hidraw rw\n", u)
        self.assertIn("\nDeviceAllow=char-usb_device rw\n", u)
        self.assertNotIn("i2c", u.replace("I2C/SMBus", "").replace("/dev/i2c-*", ""))         # the bus is never allowed
        self.assertIn("\nCapabilityBoundingSet=CAP_DAC_OVERRIDE\n", u)
        self.assertIn("\nStateDirectory=ollama1-openrgb\n", u)
        self.assertIn("\nRestrictAddressFamilies=AF_UNIX AF_INET AF_NETLINK\n", u)
        self.assertNotIn("\nUser=", u)

    def test_the_lights_unit(self):
        u = self.unit("ollama1-leds.service")
        self.common(u)
        self.assertIn("\nExecStart=/usr/local/lib/ollama1/bin/ollama1-leds run\n", u)
        self.assertIn("\nType=notify\n", u)
        self.assertIn("\nWatchdogSec=30\n", u)
        self.assertIn("\nWants=ollama1-openrgb.service\n", u)
        self.assertIn("After=ollama1-gateway.service ollama1-openrgb.service", u)
        self.assertIn("\nReadWritePaths=/run/ollama1\n", u)
        self.assertIn("\nRestrictAddressFamilies=AF_UNIX AF_INET\n", u)
        self.assertNotIn("DeviceAllow", u)                                  # no hardware access at all
        self.assertNotIn("/dev", u.replace("(", "").split("[Unit]")[1])

    def test_setup_wiring(self):
        s = self.read("setup.sh")
        self.assertIn('ln -sfn "$LIBDIR/bin/ollama1-leds" /usr/local/bin/ollama1-leds', s)
        self.assertIn("printf 'LEDS=%s\\n' \"$LEDS\"", s)
        self.assertIn('LEDS=$(leds_choice "$A_LEDS" "${OLLAMA1_LEDS:-}" "$(saved LEDS)")', s)
        self.assertIn('$(leds_plan "$LEDS" "$LEDS_LENGTH")', s)
        a = s.index('step "Lights"')
        self.assertLess(s.index('step "Fans"'), a)
        self.assertLess(a, s.index('step "Cloudflare Tunnel and Access"'))
        self.assertIn('"$LIBDIR/bin/ollama1-leds" setup "$LEDS"', s[a:s.index('step "Cloudflare Tunnel and Access"')])
        self.assertIn("--leds|", s)

    def bash(self, script):
        r = subprocess.run(["bash", "-c", ". %s; %s" % (shlex.quote(os.path.join(U.LIB, "setuplib.sh")), script)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def test_the_choice_is_off_unless_turned_on_and_a_saved_choice_is_kept(self):
        for args, want in ((['"" "" ""'], "off"), (['on "" ""'], "on"), (['off "" ""'], "off"), (['"" 1 ""'], "on"),
                           (['"" 0 on'], "off"), (['"" "" on'], "on"), (['"" "" off'], "off"), (['off 1 on'], "off"),
                           (['on 0 off'], "on")):
            self.assertEqual(self.bash("leds_choice " + " ".join(args)), (0, want), args)
        self.assertEqual(self.bash('leds_choice "" "" maybe')[0], 1)
        self.assertEqual(self.bash('leds_choice maybe "" ""')[0], 1)

    def test_the_plan_line_says_it_installs_a_package_only_when_on(self):
        on = self.bash("leds_plan on")[1]
        self.assertIn("Lights ON", on)
        self.assertIn("apt-get install openrgb", on)
        self.assertIn("white", on)
        self.assertIn("red", on)
        off = self.bash("leds_plan off")[1]
        self.assertIn("Lights OFF", off)
        self.assertIn("nothing installed", off)

    def test_the_flag_is_in_the_help_and_a_bad_value_is_refused(self):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--help"], capture_output=True, text=True)
        self.assertIn("--leds on|off", r.stdout)
        self.assertIn("Default OFF", r.stdout)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--leds", "maybe", "--plan"], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--leds takes on or off", r.stdout)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--leds"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--plan"], capture_output=True, text=True,
                           env=dict(os.environ, OLLAMA1_LEDS="sometimes"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("OLLAMA1_LEDS takes 1 or 0", r.stdout)

    def test_the_panel_shows_the_line(self):
        a = self.read("bin", "ollama1-admin")
        self.assertIn('st["leds"] = o1leds.panel_line()', a)
        self.assertIn('id="ledsline"', a)
        self.assertIn("ll.textContent=s.leds||''", a)
        self.assertIn('st["fan"] = o1fan.panel_line()', a)                  # the fan line is still there

    def test_the_wake_hook_restarts_the_server_and_pokes_the_service(self):
        h = self.read("config", "ollama1-sleep-hook")
        self.assertIn("systemctl is-active --quiet ollama1-leds.service", h)
        self.assertIn("systemctl restart --no-block ollama1-openrgb.service", h)
        self.assertIn("systemctl kill --kill-whom=main --signal=USR1 ollama1-leds.service", h)
        self.assertIn("systemctl kill --kill-whom=main --signal=USR1 ollama1-fan.service", h)

    def test_the_service_handles_the_wake_signal(self):
        with open(os.path.join(U.LIB, "o1leds.py")) as f:
            src = f.read()
        self.assertIn("signal.signal(signal.SIGUSR1, wake)", src)
        self.assertIn("leds.resync = True", src)


if __name__ == "__main__":
    unittest.main()
