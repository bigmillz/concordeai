"""Lights that follow the server's work (6b395): the OpenRGB SDK protocol against a fake
server on a local socket (framing, version negotiation, device lists, modes, colours), the
fade curve, the state machine on a fake clock (white, red in 0.8 s, 3 s of red after the
work, white in 2 s, a turn round in mid-fade, 20 Hz only while fading, a keepalive), a
server that dies and comes back, the same definition of "working" as the fan service, the
status, setup and the wiring (units, setup.sh, the panel, the sleep hook)."""
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
    def __init__(self):
        self.v = {"inflight": 0, "gpu_busy": 0, "tools": [], "loadavg": 0.1}

    def dict(self):
        return {k: (lambda k=k: self.v[k]) for k in self.v}


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
        self.probes = Probes()
        self.client = StubClient(self.clock)
        self.lines = []
        self.make()
        self.addCleanup(lambda: os.path.exists(o1leds.status_path()) and os.unlink(o1leds.status_path()))

    def make(self, **kw):
        kw.setdefault("poll_s", 0.25)
        kw.setdefault("tools_every", 0)
        self.leds = o1leds.Leds(self.probes.dict(), client=self.client, clock=self.clock, wall=lambda: 1_700_000_000,
                                log=self.lines.append, **kw)

    def run_for(self, seconds, step=0.25, until=None):
        """Tick every `step` s; the colour after each tick as (t - start, rgb)."""
        t0, out = self.clock.t, []
        n = int(round(seconds / step))
        for _ in range(n):
            self.leds.tick()
            out.append((round(self.clock.t - t0, 6), self.leds.fader.rgb()))
            if until and until(out[-1][1]):
                break
            self.clock.t += step
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


# ---- the fade -----------------------------------------------------------------------------

class TestFader(unittest.TestCase):
    def test_the_ends_are_exactly_white_and_exactly_red(self):
        f = o1leds.Fader()
        self.assertEqual((f.rgb(), f.state()), (WHITE, "white"))
        f.pos = 1.0
        self.assertEqual((f.rgb(), f.state()), (RED, "red"))
        f.pos = 0.5
        self.assertEqual(f.state(), "fading")

    def test_to_red_takes_0_8_s_and_every_step_only_lowers_green_and_blue(self):
        f, prev, t, frames = o1leds.Fader(), WHITE, 0.0, 0
        while f.state() != "red" and frames < 200:
            f.step(o1leds.FRAME_S, True)
            t += o1leds.FRAME_S
            frames += 1
            c = f.rgb()
            self.assertEqual((c[0], c[1], c[2] <= prev[2]), (255, c[2], True))      # R stays 255, G == B
            self.assertLessEqual(c[1], prev[1])
            prev = c
        self.assertEqual(frames, 16)
        self.assertAlmostEqual(t, 0.8, places=6)

    def test_to_white_takes_2_s_and_every_step_only_raises_them(self):
        f = o1leds.Fader()
        f.pos = 1.0
        prev, frames = RED, 0
        while f.state() != "white" and frames < 400:
            f.step(o1leds.FRAME_S, False)
            frames += 1
            c = f.rgb()
            self.assertGreaterEqual(c[1], prev[1])
            prev = c
        self.assertEqual(frames, 40)
        self.assertEqual(prev, WHITE)

    def test_the_colour_depends_on_the_position_alone_so_a_turn_round_is_smooth(self):
        f = o1leds.Fader()
        seq = []
        for _ in range(8):
            f.step(0.05, True)
            seq.append(f.rgb()[1])
        for _ in range(8):
            f.step(0.05, False)
            seq.append(f.rgb()[1])
        mid = f.rgb()[1]
        self.assertLessEqual(max(abs(a - b) for a, b in zip(seq, seq[1:])), 30)     # no jump at the turn
        self.assertGreater(mid, 150)                                                # most of the way back
        self.assertEqual(o1leds.Fader().rgb(), WHITE)

    def test_it_never_leaves_its_range(self):
        f = o1leds.Fader()
        f.step(100, True)
        self.assertEqual(f.rgb(), RED)
        f.step(100, False)
        self.assertEqual(f.rgb(), WHITE)


# ---- the state machine --------------------------------------------------------------------

class TestBehaviour(Rig):
    def test_it_starts_white_at_full_value_without_a_fade(self):
        self.leds.tick()
        self.assertEqual(self.client.shown, [(self.clock.t, WHITE)])
        self.assertEqual(self.leds.fader.state(), "white")

    def test_working_fades_to_red_in_0_8_s_and_stops_there(self):
        self.make(poll_s=0.05)
        self.leds.tick()
        self.probes.v["inflight"] = 1
        seq = self.run_for(1.5, step=0.05)
        times = [t for t, c in seq if c == RED]
        self.assertTrue(0.75 <= times[0] <= 0.9, times[0])
        greens = [c[1] for _t, c in seq]
        self.assertEqual(greens, sorted(greens, reverse=True))                      # only ever down
        self.assertEqual(self.last_shown(), RED)
        self.assertTrue(all(c == RED for _t, c in seq if _t >= times[0]))

    def test_the_work_ending_waits_3_s_then_fades_to_white_in_2_s(self):
        self.make(poll_s=0.25)
        self.probes.v["inflight"] = 1
        self.run_for(3)                                                  # red by now
        self.assertEqual(self.leds.fader.rgb(), RED)
        self.probes.v["inflight"] = 0
        end = self.clock.t                                               # the first tick that sees the work has ended
        seq = []
        for _ in range(60):
            self.leds.tick()
            seq.append((round(self.clock.t - end, 6), self.leds.fader.rgb()))
            self.clock.t += 0.25
        by_t = dict(seq)
        self.assertEqual(by_t[2.75], RED)
        self.assertEqual(by_t[3.0], RED)                                 # exactly 3 s: still red (it starts after)
        self.assertNotEqual(by_t[3.25], RED)
        first_white = min(t for t, c in seq if c == WHITE)
        self.assertTrue(4.75 <= first_white <= 5.5, first_white)         # 3 s hold + 2 s fade
        self.assertLessEqual(max(t for t, c in seq if c != WHITE), first_white)

    def test_work_again_inside_the_3_s_never_leaves_red(self):
        self.make(poll_s=0.25)
        self.probes.v["inflight"] = 1
        self.run_for(3)
        self.probes.v["inflight"] = 0
        seq = self.run_for(2.5)
        self.probes.v["inflight"] = 1
        seq += self.run_for(5)
        self.assertTrue(all(c == RED for _t, c in seq))                  # not one frame of anything else

    def test_a_new_request_mid_fade_turns_round_from_the_colour_it_has_reached(self):
        self.make(poll_s=0.05)
        self.probes.v["inflight"] = 1
        self.run_for(1.0, step=0.05)
        self.probes.v["inflight"] = 0
        self.run_for(3.0, step=0.05)                                     # the hold
        seq = self.run_for(1.0, step=0.05)                               # half way to white
        turned_at = seq[-1][1]
        self.assertTrue(0 < turned_at[1] < 255, turned_at)
        self.probes.v["inflight"] = 1
        back = self.run_for(1.5, step=0.05)
        greens = [turned_at[1]] + [c[1] for _t, c in back]
        self.assertLessEqual(max(abs(a - b) for a, b in zip(greens, greens[1:])), 30)       # no jump at the turn
        self.assertEqual(back[-1][1], RED)
        tail = [g for g in greens[1:greens.index(0) + 1]]
        self.assertEqual(tail, sorted(tail, reverse=True))               # and from there only down

    def test_while_idle_it_wakes_only_at_the_poll_not_at_20_hz(self):
        self.make(poll_s=2)
        waits = []
        for _ in range(10):
            waits.append(self.leds.tick())
            self.clock.t += waits[-1]
        self.assertTrue(all(w == 2 for w in waits[1:]), waits)
        # a keepalive: the colour goes out again at every poll, and only then
        self.assertEqual(len(self.client.shown), 10)
        self.assertTrue(all(c == WHITE for _t, c in self.client.shown))

    def test_while_fading_it_asks_for_20_hz_and_then_goes_back_to_the_poll(self):
        self.make(poll_s=2)
        self.leds.tick()
        self.probes.v["inflight"] = 1
        self.clock.t += 2
        self.assertEqual(self.leds.tick(), o1leds.FRAME_S)
        self.assertEqual(o1leds.FRAME_S, 0.05)
        for _ in range(40):
            self.clock.t += 0.05
            self.leds.tick()
        self.assertEqual(self.leds.fader.rgb(), RED)
        self.clock.t += 0.05
        w = self.leds.tick()
        self.assertNotEqual(w, o1leds.FRAME_S)
        self.assertGreater(w, 1.0)                                       # the next poll, about 2 s on

    def test_frames_go_out_only_when_the_colour_changes_or_at_a_poll(self):
        self.make(poll_s=2)
        self.leds.tick()
        self.probes.v["inflight"] = 1
        self.clock.t += 2
        self.leds.tick()
        self.client.shown.clear()
        for _ in range(50):                                              # 2.5 s of 20 Hz ticks
            self.clock.t += 0.05
            self.leds.tick()
        self.assertEqual([c for _t, c in self.client.shown].count(RED) <= 4, True)
        self.assertLessEqual(len(self.client.shown), 16 + 2)             # 16 fade frames + the one poll

    def test_with_the_default_poll_the_hold_is_never_under_3_s_after_the_work(self):
        self.make(poll_s=o1leds.POLL_S)
        self.probes.v["inflight"] = 1
        seq = self.run_for(6, step=0.05)
        self.assertLessEqual(min(t for t, c in seq if c == RED), 0.8 + 0.1)   # the first tick polls: no wait to start
        self.probes.v["inflight"] = 0
        seq = self.run_for(12, step=0.05)
        left_red = min(t for t, c in seq if c != RED)
        white_at = min(t for t, c in seq if c == WHITE)
        self.assertGreaterEqual(left_red, 3 - 0.05)
        self.assertLessEqual(left_red, 3 + o1leds.POLL_S + 0.1)
        self.assertGreaterEqual(white_at, left_red + 1.8)            # the first frames still round to red
        self.assertLessEqual(white_at, left_red + 2 + 0.2)

    def test_a_probe_that_raises_is_not_work_and_not_a_crash(self):
        def boom():
            raise OSError("x")
        self.leds.work.p = {k: boom for k in ("inflight", "gpu_busy", "tools", "loadavg")}
        self.leds.tick()
        self.assertEqual(self.leds.fader.rgb(), WHITE)

    def test_a_stop_sets_white_first(self):
        self.probes.v["inflight"] = 1
        self.run_for(3)
        self.assertEqual(self.last_shown(), RED)
        self.leds.shutdown()
        self.assertEqual(self.last_shown(), WHITE)
        self.assertFalse(self.client.connected)

    def test_a_wake_connects_afresh_and_sets_the_colour_at_once(self):
        self.leds.tick()
        self.assertEqual(self.client.opens, 1)
        self.client.shown.clear()
        self.leds.resync = True
        self.clock.t += 0.25
        self.leds.tick()
        self.assertEqual((self.client.opens, self.client.closes), (2, 1))
        self.assertEqual(self.last_shown(), WHITE)


class TestSameDefinitionAsTheFans(Rig):
    def red_after(self, **probe):
        self.setUp()
        self.probes.v.update(probe)
        self.run_for(1.5, step=0.05)
        return self.leds.fader.rgb() == RED

    def test_the_four_probes_and_their_limits_are_the_fans(self):
        self.assertTrue(self.red_after(inflight=1))
        self.assertTrue(self.red_after(gpu_busy=10))
        self.assertFalse(self.red_after(gpu_busy=9))
        self.assertTrue(self.red_after(tools=["stability-test.sh"]))
        self.assertTrue(self.red_after(loadavg=1.6))
        self.assertFalse(self.red_after(loadavg=1.5))
        self.assertFalse(self.red_after())
        self.assertFalse(self.red_after(inflight=True * 0, gpu_busy=None, loadavg=None))

    def test_it_is_the_same_code_as_the_fan_service_not_a_copy(self):
        self.assertIsInstance(self.leds.work, o1work.Work)
        fan = o1fan.Fan(self.probes.dict(), clock=self.clock, tools_every=0, sysroot="/nonexistent")
        self.assertIsInstance(fan.work, o1work.Work)
        self.assertIs(o1fan.probes, o1work.probes)
        self.assertIs(o1fan.Gpu, o1work.Gpu)
        for v in ({"inflight": 1}, {"gpu_busy": 50}, {"tools": ["x"]}, {"loadavg": 3}, {}):
            self.probes.v.update({"inflight": 0, "gpu_busy": 0, "tools": [], "loadavg": 0.1})
            self.probes.v.update(v)
            self.assertEqual(self.leds.work.working(self.clock.t)[0], fan.working(self.clock.t)[0], v)

    def test_the_service_builds_the_shared_probes(self):
        self.assertEqual(set(o1work.probes()), {"inflight", "gpu_busy", "tools", "loadavg"})


# ---- the server dying and coming back -----------------------------------------------------

class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.srv = F.FakeServer(two_devices()).start()
        self.addCleanup(self.srv.stop)
        self.clock, self.probes, self.lines = FakeClock(), Probes(), []
        self.client = o1leds.Client(port=self.srv.port)
        self.addCleanup(self.client.close)
        self.leds = o1leds.Leds(self.probes.dict(), client=self.client, clock=self.clock, wall=lambda: 1_700_000_000,
                                log=self.lines.append, poll_s=0.5, tools_every=0)
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
        self.probes.v["inflight"] = 1
        self.run_for(1.5)
        self.srv.clear()
        self.run_for(1)
        self.assertEqual(set(F.decode_leds(self.srv.wait_for(1050)[-1][1])[0]), {RED})
        self.assertEqual(self.status()["rgb"], [255, 0, 0])
        self.assertIn("a request is running", self.status()["why"])

    def test_when_the_server_dies_it_says_so_keeps_going_and_reconnects_with_the_current_colour(self):
        self.probes.v["inflight"] = 1
        self.run_for(2)
        self.srv.stop()
        self.run_for(1.5)
        st = self.status()
        self.assertFalse(st["connected"])
        self.assertTrue(st["error"])
        self.assertTrue(st["line"].startswith("Lights: OpenRGB:"))
        self.assertTrue(any(l.startswith("OpenRGB:") for l in self.lines))
        self.assertEqual(self.leds.fader.rgb(), RED)                       # the state machine ran on, unharmed
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
        self.clock.t += 0.25
        self.probes.v["inflight"] = 1
        self.leds.tick()
        self.assertFalse(self.client.connected)
        self.assertIn("Broken pipe", self.leds.err)
        self.client.fail_show = False
        self.run_for(10)
        self.assertTrue(self.client.connected)
        self.assertIsNone(self.leds.err)

    def test_the_loop_survives_a_bug_in_a_tick_and_still_pings_the_watchdog(self):
        sent, lines = [], []

        class Bad:
            fader = o1leds.Fader()

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

    def test_white_fading_and_red_are_named_and_say_why(self):
        self.client.devices = [type("D", (), {"name": "MSI", "vendor": "MSI", "nleds": 6, "kind": "leds",
                                              "modes": [type("M", (), {"name": "Direct"})()], "mode": 0})()]
        self.leds.tick()
        st = self.read()
        self.assertEqual((st["state"], st["target"], st["why"]), ("white", "white", "idle"))
        self.assertEqual(st["line"], "Lights: white (idle)  -  1 device")
        self.probes.v["inflight"] = 1
        self.run_for(0.5, step=0.05)
        st = self.read()
        self.assertEqual((st["state"], st["target"]), ("fading", "red"))
        self.assertEqual(self.leds.fader.state(), "fading")
        self.leds.tick()
        self.assertEqual(o1leds.status_line(self.read()), "Lights: fading to red (a request is running)  -  1 device")
        self.run_for(1.5, step=0.05)
        self.leds.tick()
        self.assertEqual(self.read()["line"], "Lights: red (a request is running)  -  1 device")
        self.probes.v["inflight"] = 0
        self.run_for(0.5)
        self.assertIn("the work ended; white again in", self.read()["why"])

    def test_the_text_for_a_person(self):
        st = {"at": 1, "state": "red", "target": "red", "rgb": [255, 0, 0], "why": "a request is running",
              "connected": True, "protocol": 4, "error": None,
              "devices": [{"name": "MSI MYSTIC LIGHT", "vendor": "MSI", "leds": 6, "mode": "Direct", "usable": True},
                          {"name": "Odd", "vendor": "", "leds": 3, "mode": None, "usable": False}]}
        out = o1leds.render_status(st)
        self.assertIn("lights: red  now 255,0,0 (FF0000)", out)
        self.assertIn("why: a request is running", out)
        self.assertIn("2 devices found", out)
        self.assertIn("MSI MYSTIC LIGHT", out)
        self.assertIn("mode Direct, 6 LEDs", out)
        self.assertIn("no usable mode", out)
        self.assertIn("errors: none", out)
        st.update(connected=False, error="Connection refused", devices=[])
        out = o1leds.render_status(st)
        self.assertIn("not connected", out)
        self.assertIn("errors: Connection refused", out)
        self.assertIn("isn't running", o1leds.render_status(None))

    def test_the_panel_line_only_while_the_service_is_fresh(self):
        path = os.path.join(U.PREFIX, "leds-test.json")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        o1leds.write_json_atomic(path, {"at": 1000, "line": "Lights: white (idle)  -  2 devices"}, mode=0o644)
        self.assertEqual(o1leds.panel_line(path, now=1005), "Lights: white (idle)  -  2 devices")
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
        self.assertIn('$(leds_plan "$LEDS")', s)
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
