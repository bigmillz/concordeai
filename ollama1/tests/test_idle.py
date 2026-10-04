"""Auto sleep (6b346): the pure decision, the settings, what counts as
activity, the card list for waking, the setup's link files and the root
service's tick on a fake clock."""
import json
import os
import shutil
import tempfile
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1idle

NOW = 1_800_000_000.0
BASE = dict(enabled=True, supported=True, minutes=30, now=NOW, last_activity=NOW - 3600, boot_time=NOW - 7200,
            last_resume=None, inflight=0, sessions=0, gpu_busy=2, gpu_present=True, loadavg=0.1, busy=[], tools=[],
            inhibitors=[])


def d(**kw):
    i = dict(BASE)
    i.update(kw)
    return o1idle.decide(i)


class TestDecide(unittest.TestCase):
    def test_all_clear_sleeps(self):
        self.assertEqual(d(), (True, "idle 60 minutes"))

    def test_a_login_does_not_block_sleep(self):
        # 6b364: an open SSH tab or console login is not activity; only a
        # request, a running tool, load, the card or a lock keeps the server awake
        self.assertEqual(d(sessions=3), (True, "idle 60 minutes"))

    def test_each_blocker(self):
        table = [
            (dict(enabled=False), "off"), (dict(enabled=None), "off"), (dict(enabled=1), "off"),
            (dict(supported=False), "no deep sleep"), (dict(supported=None), "no deep sleep"),
            (dict(inflight=1), "request is running"), (dict(inflight=None), "whether a request is running"),
            (dict(loadavg=None), "load average"), (dict(loadavg=1.6), "busy (load 1.6)"), (dict(loadavg=9), "busy (load"),
            (dict(boot_time=None), "when it started"),
            (dict(gpu_busy=None), "no reading from the graphics card"),
            (dict(gpu_busy=None, gpu_present=None), "no reading from the graphics card"),
            (dict(busy=["a model is being downloaded"]), "downloaded"), (dict(busy=None), "what is running"),
            (dict(tools=["stability-test.sh"]), "stability-test.sh"), (dict(tools=None), "tools"),
            (dict(inhibitors=["backup block"]), "blocks sleep"), (dict(inhibitors=None), "blocks sleep"),
            (dict(gpu_busy=10), "graphics card is busy"), (dict(gpu_busy=95), "graphics card is busy"),
            (dict(minutes="30"), "isn't a number"), (dict(minutes=True), "isn't a number"),
            (dict(minutes=30.0), "isn't a number"),
        ]
        for kw, word in table:
            sleep, why = d(**kw)
            self.assertFalse(sleep, kw)
            self.assertIn(word, why, kw)

    def test_gpu_reading_missing_blocks_only_where_there_is_a_card(self):
        self.assertFalse(d(gpu_busy=None)[0])                       # a card, no reading: don't guess
        self.assertTrue(d(gpu_busy=None, gpu_present=False)[0])     # no card at all
        self.assertTrue(d(gpu_busy=9)[0])
        self.assertTrue(d(gpu_busy=0)[0])
        self.assertFalse(d(gpu_busy=95, gpu_present=False)[0])      # a reading is a reading

    def test_load_gate_edge(self):
        self.assertTrue(d(loadavg=1.5)[0])
        self.assertFalse(d(loadavg=1.51)[0])

    def test_the_idle_clock(self):
        self.assertEqual(d(last_activity=NOW - 30 * 60)[0], True)          # exactly the minutes
        self.assertEqual(d(last_activity=NOW - 30 * 60 + 1)[0], False)
        self.assertIn("idle 29 of 30", d(last_activity=NOW - 29 * 60 - 5)[1])
        # boot and resume count as activity, whichever is latest
        self.assertFalse(d(boot_time=NOW - 600)[0])
        self.assertFalse(d(last_resume=NOW - 600)[0])
        self.assertTrue(d(last_resume=NOW - 3 * 3600, boot_time=NOW - 4 * 3600)[0])
        self.assertFalse(d(last_activity=NOW - 3 * 3600, boot_time=NOW - 4 * 3600, last_resume=NOW - 60)[0])
        # an activity time in the future (a clock that stepped back) is now
        self.assertFalse(d(last_activity=NOW + 99999)[0])
        # never any activity: the boot time is what counts
        self.assertTrue(d(last_activity=0, boot_time=NOW - 3600)[0])
        self.assertFalse(d(last_activity=0, boot_time=None)[0])           # and an unknown boot time blocks

    def test_minutes_are_clamped(self):
        self.assertFalse(d(minutes=1, last_activity=NOW - 3 * 60)[0])      # 1 acts as 5
        self.assertTrue(d(minutes=1, last_activity=NOW - 5 * 60)[0])
        self.assertFalse(d(minutes=10 ** 9, last_activity=NOW - 86399 * 60)[0] and False)
        self.assertTrue(d(minutes=10 ** 9, last_activity=NOW - 1441 * 60, boot_time=NOW - 2000 * 60)[0])

    def test_bad_inputs_never_sleep(self):
        self.assertEqual(o1idle.decide({}), (False, "auto sleep is off"))
        self.assertFalse(o1idle.decide(dict(BASE, now=None))[0])
        i = dict(BASE)
        del i["now"]
        self.assertFalse(o1idle.decide(i)[0])


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1idle-")
        self.path = os.path.join(self.dir, "sleep.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_defaults(self):
        self.assertEqual(o1idle.read_config(self.path), {"enabled": False, "minutes": 30})

    def test_never_trusts_the_file(self):
        cases = [("not json", {"enabled": False, "minutes": 30}), ("[1]", {"enabled": False, "minutes": 30}),
                 ('{"enabled": "true", "minutes": "40"}', {"enabled": False, "minutes": 30}),
                 ('{"enabled": 1, "minutes": true}', {"enabled": False, "minutes": 30}),
                 ('{"enabled": true, "minutes": 1}', {"enabled": True, "minutes": 5}),
                 ('{"enabled": true, "minutes": 99999}', {"enabled": True, "minutes": 1440}),
                 ('{"enabled": true, "minutes": 45.5}', {"enabled": True, "minutes": 30}),
                 ('{"enabled": true, "minutes": 45, "x": 1}', {"enabled": True, "minutes": 45})]
        for text, want in cases:
            with open(self.path, "w") as f:
                f.write(text)
            self.assertEqual(o1idle.read_config(self.path), want, text)

    def test_round_trip_and_mode(self):
        o1idle.write_config({"enabled": True, "minutes": 7, "extra": 1}, self.path)
        self.assertEqual(json.load(open(self.path)), {"enabled": True, "minutes": 7})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_update_validation(self):
        P = o1idle.parse_config_update
        self.assertEqual(P({"enabled": True}), ({"enabled": True}, ""))
        self.assertEqual(P({"minutes": 45}), ({"minutes": 45}, ""))
        self.assertEqual(P({"enabled": False, "minutes": 1}), ({"enabled": False, "minutes": 5}, ""))
        self.assertEqual(P({"minutes": 99999}), ({"minutes": 1440}, ""))
        for bad in (None, [], {}, {"minutes": True}, {"minutes": "30"}, {"minutes": 30.0}, {"minutes": None},
                    {"enabled": 1}, {"enabled": "yes"}, {"enabled": None}, {"enabled": True, "x": 1},
                    {"minutes": 30, "device": "a"}):
            self.assertIsNone(P(bad)[0], bad)
            self.assertTrue(P(bad)[1], bad)


class TestActivity(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1idle-")
        self.path = os.path.join(self.dir, "activity.json")
        self.t = [NOW]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_counts_and_file(self):
        a = o1idle.Activity(clock=lambda: self.t[0], path=self.path)
        a.begin()
        a.begin()
        self.assertEqual(json.load(open(self.path))["inflight"], 2)        # a start is in the file at once
        self.t[0] += 5
        a.end()
        a.write()                                                    # an end is written by the next heartbeat
        self.assertEqual(json.load(open(self.path)), {"at": NOW + 5, "inflight": 1, "last": NOW + 5})
        a.end()
        a.end()                                                      # never below zero
        a.write()
        self.assertEqual(json.load(open(self.path))["inflight"], 0)

    def test_reading_it(self):
        R = o1idle.read_activity
        w = lambda obj: open(self.path, "w").write(json.dumps(obj))
        w({"last": NOW - 10, "inflight": 2, "at": NOW - 3})
        self.assertEqual(R(NOW, self.path), {"last": NOW - 10, "inflight": 2, "fresh": True})
        w({"last": NOW - 10, "inflight": 2, "at": NOW - 500})        # the gateway isn't writing: unknown, not none
        self.assertEqual(R(NOW, self.path), {"last": NOW - 10, "inflight": None, "fresh": False})
        w({"last": NOW - 10, "inflight": 2, "at": NOW + 500})        # from the future
        self.assertFalse(R(NOW, self.path)["fresh"])
        w({"last": "x", "inflight": True, "at": NOW})
        self.assertEqual(R(NOW, self.path), {"last": 0.0, "inflight": None, "fresh": True})
        w({"last": -5, "inflight": -1, "at": NOW})
        self.assertIsNone(R(NOW, self.path)["inflight"])
        open(self.path, "w").write("nope")
        self.assertEqual(R(NOW, self.path), {"last": 0.0, "inflight": None, "fresh": False})
        os.unlink(self.path)
        self.assertIsNone(R(NOW, self.path)["inflight"])

    def test_a_new_gateway_starts_awake(self):
        a = o1idle.Activity(clock=lambda: NOW, path=self.path)
        self.assertEqual(a.last, NOW)
        a.write()
        self.assertEqual(o1idle.read_activity(NOW + 1, self.path)["last"], NOW)

    def test_the_files_are_read_safely(self):
        """The root service reads what the gateway's user can write: a symlink, a FIFO or a big file
        is nothing."""
        good = os.path.join(self.dir, "real.json")
        with open(good, "w") as f:
            json.dump({"last": NOW, "inflight": 0, "at": NOW}, f)
        link = os.path.join(self.dir, "link.json")
        os.symlink(good, link)
        self.assertFalse(o1idle.read_activity(NOW, link)["fresh"])
        fifo = os.path.join(self.dir, "fifo.json")
        os.mkfifo(fifo)
        self.assertFalse(o1idle.read_activity(NOW, fifo)["fresh"])               # and doesn't block
        big = os.path.join(self.dir, "big.json")
        with open(big, "w") as f:
            f.write('{"last": 1, "pad": "' + "x" * 10000 + '"}')
        self.assertFalse(o1idle.read_activity(NOW, big)["fresh"])
        cfg_link = os.path.join(self.dir, "cfg.json")
        with open(good + ".cfg", "w") as f:
            json.dump({"enabled": True, "minutes": 40}, f)
        os.symlink(good + ".cfg", cfg_link)
        self.assertEqual(o1idle.read_config(cfg_link), {"enabled": False, "minutes": 30})
        self.assertEqual(o1idle.read_config(good + ".cfg"), {"enabled": True, "minutes": 40})
        cfifo = os.path.join(self.dir, "cfifo.json")
        os.mkfifo(cfifo)
        self.assertEqual(o1idle.read_config(cfifo), {"enabled": False, "minutes": 30})


INHIBIT = """\
NetworkManager   0 root 1021 NetworkManager sleep                    NetworkManager needs to turn off networks delay
UPower           0 root 1190 upowerd        sleep                    Pause device polling                     delay
ollama1          0 root 5120 sh             sleep:handle-power-key   a model is being downloaded              block
Unattended Upgrades Shutdown 0 root 900 python3 shutdown             Stop ongoing upgrades or perform upgrades block
Some Tool        1000 pat 77 tool           idle                     keeping the screen on                    block
"""


class TestProbes(unittest.TestCase):
    def test_sessions(self):
        self.assertEqual(o1idle.parse_sessions(""), 0)
        self.assertEqual(o1idle.parse_sessions("  3 1000 pat seat0 tty1 \n 5 1000 pat - -\n"), 2)

    def test_inhibitors_only_block_mode_sleep(self):
        got = o1idle.parse_inhibitors(INHIBIT)
        self.assertEqual(len(got), 1)
        self.assertIn("ollama1", got[0])
        self.assertEqual(o1idle.parse_inhibitors(""), [])

    def test_tools(self):
        argvs = [["/usr/bin/python3", "/usr/local/lib/ollama1/tools/ram_model_test.py", "--x"],
                 ["bash", "/opt/stability-test.sh"], ["/usr/bin/apt-get", "upgrade"], ["grep", "setup.sh"],
                 ["sshd", "-D"], ["vim", "notes.txt", "apt"], []]
        # (grep setup.sh counts too: a false alarm only keeps the box awake; an argument past the
        # second never counts)
        self.assertEqual(o1idle.scan_tools(argvs), ["apt-get", "ram_model_test.py", "setup.sh", "stability-test.sh"])

    def test_tools_named_anywhere_programs_only_as_programs(self):
        S = o1idle.scan_tools
        self.assertEqual(S([["python3", "-u", "-W", "ignore", "/x/ram_model_test.py", "--cfg", "a"]]), ["ram_model_test.py"])
        self.assertEqual(S([["/bin/bash", "-x", "-e", "/opt/stability-test.sh"]]), ["stability-test.sh"])
        self.assertEqual(S([["sudo", "-n", "bash", "-c", "x", "setup.sh"]]), ["setup.sh"])
        self.assertEqual(S([["vim", "notes.txt", "apt"], ["tool", "--x", "/usr/bin/dpkg"], ["less", "-N", "apt-get"]]), [])
        self.assertEqual(S([["sudo", "apt", "upgrade"], ["/usr/bin/dpkg", "-i", "x"]]), ["apt", "dpkg"])

    def test_tools_from_a_proc_tree(self):
        d_ = tempfile.mkdtemp(prefix="o1proc-")
        try:
            for pid, argv in (("101", ["bash", "setup.sh"]), ("102", ["sleep", "5"])):
                os.makedirs(os.path.join(d_, pid))
                open(os.path.join(d_, pid, "cmdline"), "wb").write(b"\0".join(a.encode() for a in argv) + b"\0")
            os.makedirs(os.path.join(d_, "self"))
            self.assertEqual(o1idle.tools_running(d_), ["setup.sh"])
            self.assertIsNone(o1idle.tools_running(os.path.join(d_, "none")))
        finally:
            shutil.rmtree(d_, ignore_errors=True)

    def test_deep_sleep(self):
        d_ = tempfile.mkdtemp(prefix="o1sleep-")
        try:
            p = os.path.join(d_, "mem_sleep")
            for text, want in (("s2idle [deep]\n", True), ("[s2idle] deep\n", True), ("[s2idle]\n", False),
                               ("", False), ("s2idle shallow\n", False)):
                open(p, "w").write(text)
                self.assertEqual(o1idle.deep_sleep_supported(p), want, text)
            self.assertFalse(o1idle.deep_sleep_supported(os.path.join(d_, "missing")))
        finally:
            shutil.rmtree(d_, ignore_errors=True)

    def test_boot_time(self):
        d_ = tempfile.mkdtemp(prefix="o1stat-")
        try:
            p = os.path.join(d_, "stat")
            open(p, "w").write("cpu  1 2 3\nbtime 1790000000\nprocesses 5\n")
            self.assertEqual(o1idle.boot_time(p), 1790000000.0)
            self.assertIsNone(o1idle.boot_time(os.path.join(d_, "none")))
            open(p, "w").write("cpu 1\nbtime nope\n")
            self.assertIsNone(o1idle.boot_time(p))
            self.assertGreaterEqual(o1idle.loadavg1(), 0.0)
        finally:
            shutil.rmtree(d_, ignore_errors=True)


ETHTOOL_ON = "Settings for enp5s0:\n\tSupports Wake-on: pumbg\n\tWake-on: g\n\tLink detected: yes\n"
ETHTOOL_OFF = "Settings for x:\n\tSupports Wake-on: pumbg\n\tWake-on: d\n"
ETHTOOL_NONE = "Settings for y:\n\tSupports Wake-on: d\n\tWake-on: d\n"


class NetFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1net-")
        self.net = os.path.join(self.dir, "net")
        os.makedirs(self.net)
        self.nic("enp5s0", "02:00:5e:10:00:01")
        self.nic("enp6s0", "02:00:5e:10:00:02")
        os.makedirs(os.path.join(self.net, "lo"))
        open(os.path.join(self.net, "lo", "address"), "w").write("00:00:00:00:00:00\n")
        self.virt("br0", "02:00:5e:10:00:01")
        os.makedirs(os.path.join(self.net, "br0", "brif"))
        for m in ("enp5s0", "enp6s0", "vethabc"):
            os.makedirs(os.path.join(self.net, "br0", "brif", m))
        self.virt("vethabc", "aa:bb:cc:dd:ee:01")
        self.virt("docker0", "aa:bb:cc:dd:ee:02")
        self.nic("wlp3s0", "aa:bb:cc:dd:ee:03", wireless=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def nic(self, name, mac, wireless=False, device=True):
        d_ = os.path.join(self.net, name)
        os.makedirs(d_, exist_ok=True)
        if device:
            os.makedirs(os.path.join(d_, "device"), exist_ok=True)
        if wireless:
            os.makedirs(os.path.join(d_, "wireless"), exist_ok=True)
        with open(os.path.join(d_, "address"), "w") as f:
            f.write(mac + "\n")
        with open(os.path.join(d_, "type"), "w") as f:
            f.write("1\n")

    def virt(self, name, mac):
        self.nic(name, mac, device=False)


class TestWake(NetFixture):
    def test_only_real_cards_even_inside_a_bridge(self):
        self.assertEqual(o1idle.physical_nics(self.net),
                         [("enp5s0", "02:00:5e:10:00:01"), ("enp6s0", "02:00:5e:10:00:02")])

    def test_a_virtual_name_with_a_device_is_still_skipped(self):
        self.nic("veth9", "aa:bb:cc:dd:ee:09")
        self.nic("tap0", "aa:bb:cc:dd:ee:0a")
        self.nic("virbr0", "aa:bb:cc:dd:ee:0b")
        self.assertEqual([n for n, _ in o1idle.physical_nics(self.net)], ["enp5s0", "enp6s0"])

    def test_bad_addresses_are_skipped(self):
        self.nic("enp40s0", "not-a-mac")
        self.nic("enp41s0", "00:00:00:00:00:00")
        self.nic("enp42s0", "AA:BB:CC:DD:EE:FF")
        self.assertEqual([n for n, _ in o1idle.physical_nics(self.net)], ["enp42s0", "enp5s0", "enp6s0"])        # in name order
        self.assertIn("aa:bb:cc:dd:ee:ff", [m for _, m in o1idle.physical_nics(self.net)])      # lower-cased

    def test_parse_wol(self):
        self.assertEqual(o1idle.parse_wol(ETHTOOL_ON), (True, True))
        self.assertEqual(o1idle.parse_wol(ETHTOOL_OFF), (True, False))
        self.assertEqual(o1idle.parse_wol(ETHTOOL_NONE), (False, False))
        self.assertEqual(o1idle.parse_wol(""), (False, False))

    def test_wake_list_is_the_cards_with_it_on(self):
        nics = o1idle.physical_nics(self.net)
        wol = {"enp5s0": (True, True), "enp6s0": (True, False)}.get
        self.assertEqual(o1idle.wake_list(nics, wol), ["02:00:5e:10:00:01"])
        self.assertEqual(o1idle.wake_list(nics, lambda n: (True, True)), ["02:00:5e:10:00:01", "02:00:5e:10:00:02"])
        self.assertEqual(o1idle.wake_list(nics, lambda n: None), [])
        many = [("e%d" % i, "00:00:00:00:00:%02x" % (i + 1)) for i in range(12)]
        self.assertEqual(len(o1idle.wake_list(many, lambda n: (True, True))), o1idle.MAX_WAKE)

    def test_published_list_is_macs_only(self):
        p = os.path.join(self.dir, "idle.json")
        open(p, "w").write(json.dumps({"wake": ["02:00:5e:10:00:01", "<script>", 5, "AA:BB:CC:DD:EE:FF", None]}))
        self.assertEqual(o1idle.published_wake(p), ["02:00:5e:10:00:01"])
        open(p, "w").write("nope")
        self.assertEqual(o1idle.published_wake(p), [])
        self.assertEqual(o1idle.published_wake(os.path.join(self.dir, "none")), [])

    def test_setup_writes_link_files_once(self):
        links = os.path.join(self.dir, "network")
        sets, lines = [], []
        nics = o1idle.physical_nics(self.net)
        wol = lambda n: (True, False)
        got = o1idle.wol_setup(nics, wol, links, sets.append, lines.append)
        self.assertEqual(got, ["02:00:5e:10:00:01", "02:00:5e:10:00:02"])
        self.assertEqual(sets, ["enp5s0", "enp6s0"])
        t = open(os.path.join(links, "50-wol-enp5s0.link")).read()
        self.assertEqual(t, "# ollama1: wake this computer with a magic packet\n[Match]\nMACAddress=02:00:5e:10:00:01\n\n"
                            "[Link]\nNamePolicy=keep kernel database onboard slot path\nMACAddressPolicy=persistent\n"
                            "WakeOnLan=magic\n")
        # it says what 99-default.link says, so a card keeps its name and its MAC after a reboot
        self.assertIn("NamePolicy=keep kernel database onboard slot path", t)
        self.assertIn("MACAddressPolicy=persistent", t)
        self.assertEqual(len(lines), 2)
        m = os.stat(os.path.join(links, "50-wol-enp5s0.link")).st_mtime_ns
        lines.clear()
        o1idle.wol_setup(nics, wol, links, sets.append, lines.append)             # again: nothing rewritten
        self.assertEqual(lines, [])
        self.assertEqual(os.stat(os.path.join(links, "50-wol-enp5s0.link")).st_mtime_ns, m)
        self.assertEqual(len(sets), 4)                                            # the setting itself is re-applied

    def test_setup_rewrites_a_link_file_of_the_old_shape_where_it_is(self):
        links = os.path.join(self.dir, "network")
        os.makedirs(links)
        old = "[Match]\nMACAddress=%s\n\n[Link]\nWakeOnLan=magic\n"
        hand = os.path.join(links, "50-wol.link")                      # by hand: any name, the old shape
        ours = os.path.join(links, "50-wol-enp6s0.link")
        other = os.path.join(links, "50-wol-other.link")
        with open(hand, "w") as f:
            f.write(old % "02:00:5E:10:00:01")                         # (an upper-case address)
        with open(ours, "w") as f:
            f.write(old % "02:00:5e:10:00:02")
        with open(other, "w") as f:
            f.write(old % "aa:bb:cc:dd:ee:ff")                          # another card's: not ours to touch
        nics = o1idle.physical_nics(self.net)
        lines = []
        o1idle.wol_setup(nics, lambda n: (True, True), links, None, lines.append)
        self.assertEqual(open(hand).read(), o1idle.link_text("02:00:5e:10:00:01"))
        self.assertEqual(open(ours).read(), o1idle.link_text("02:00:5e:10:00:02"))
        self.assertEqual(open(other).read(), old % "aa:bb:cc:dd:ee:ff")
        self.assertFalse(os.path.exists(os.path.join(links, "50-wol-enp5s0.link")))   # the hand one is the card's file
        self.assertEqual(len(lines), 2)
        lines.clear()
        o1idle.wol_setup(nics, lambda n: (True, True), links, None, lines.append)       # again: nothing more
        self.assertEqual(lines, [])
        self.assertEqual(sorted(os.listdir(links)), ["50-wol-enp6s0.link", "50-wol-other.link", "50-wol.link"])

    def test_setup_skips_cards_that_cannot_and_says_nothing_when_none(self):
        links = os.path.join(self.dir, "network")
        lines = []
        self.assertEqual(o1idle.wol_setup(o1idle.physical_nics(self.net), lambda n: (False, False), links, None,
                                          lines.append), [])
        self.assertEqual(o1idle.wol_setup(o1idle.physical_nics(self.net), lambda n: None, links, None, lines.append), [])
        self.assertEqual(o1idle.wol_setup([], lambda n: (True, True), links, None, lines.append), [])
        self.assertEqual(lines, [])
        self.assertFalse(os.path.exists(links))

    def test_setup_script_runs_it_and_names_the_bios(self):
        s = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('"$LIBDIR/bin/ollama1-idle" wol-setup', s)
        self.assertIn("ollama1-idle.service", s)
        b = open(os.path.join(U.BIN, "ollama1-idle")).read()
        self.assertIn("Wake on PCI-E", b)
        self.assertIn("Wake Up Event Setup", b)


class FakeBox:
    """A fake clock, fake probes and a suspend that records."""

    def __init__(self):
        self.wall, self.mono = NOW, 1000.0
        self.slept, self.logs, self.rc = 0, [], 0
        self.p = dict(supported=lambda: True, wake=lambda: ["02:00:5e:10:00:01"], sessions=lambda: 0,
                      inhibitors=lambda: [], tools=lambda: [], gpu_busy=lambda: 1, gpu_present=lambda: True,
                      loadavg=lambda: 0.1, busy=lambda: [], boot_time=lambda: NOW - 7200)
        self.idle = o1idle.Idle(self.p, self.suspend, log=self.logs.append, clock=lambda: self.wall,
                                mono=lambda: self.mono)

    def suspend(self):
        self.slept += 1
        return self.rc

    def advance(self, s, slept=0):
        """s seconds pass; `slept` of them with the machine suspended (the monotonic clock stops)."""
        self.wall += s
        self.mono += s - slept


class TestTick(unittest.TestCase):
    def setUp(self):
        self.box = FakeBox()
        o1idle.write_config({"enabled": True, "minutes": 30})
        for p in (o1idle.activity_file(), o1idle.idle_file()):
            if os.path.exists(p):
                os.unlink(p)

    def tearDown(self):
        for p in (o1idle.config_file(), o1idle.activity_file(), o1idle.idle_file()):
            if os.path.exists(p):
                os.unlink(p)

    def act(self, last, inflight=0, at=None):
        with open(o1idle.activity_file(), "w") as f:
            json.dump({"last": last, "inflight": inflight, "at": self.box.wall if at is None else at}, f)

    def test_sleeps_when_idle(self):
        self.act(NOW - 3600)
        self.assertEqual(self.box.idle.tick(), (True, "idle 60 minutes"))
        self.assertEqual(self.box.slept, 1)

    def test_off_never_sleeps_and_logs_the_reason_once(self):
        o1idle.write_config({"enabled": False, "minutes": 30})
        for _ in range(3):
            self.assertFalse(self.box.idle.tick()[0])
            self.box.advance(30)
        self.assertEqual(self.box.slept, 0)
        self.assertEqual(self.box.logs, ["not sleeping: auto sleep is off"])

    def test_a_request_in_flight_or_recent_keeps_it_awake(self):
        self.act(NOW - 3600, inflight=1)
        self.assertFalse(self.box.idle.tick()[0])
        self.act(NOW - 60)
        self.box.advance(30)
        self.assertFalse(self.box.idle.tick()[0])
        self.assertEqual(self.box.slept, 0)

    def test_a_stale_or_missing_activity_file_is_unknown_and_blocks(self):
        self.act(NOW - 3600, inflight=3, at=NOW - 1000)           # the gateway stopped (or is restarting)
        sleep, why = self.box.idle.tick()
        self.assertFalse(sleep)
        self.assertIn("whether a request is running", why)
        self.assertEqual(self.box.slept, 0)
        self.box.advance(30)
        self.act(NOW - 3600)                                       # it writes again: asleep-able
        self.assertTrue(self.box.idle.tick()[0])

    def test_no_activity_file_at_all_blocks(self):
        self.assertFalse(self.box.idle.tick()[0])
        self.assertEqual(self.box.slept, 0)

    def test_a_wake_of_a_few_seconds_is_a_resume_too(self):
        self.act(NOW - 3600)
        self.assertTrue(self.box.idle.tick()[0])                   # it slept, and woke at once (under the jump threshold)
        self.assertEqual(self.box.slept, 1)
        self.box.advance(30, slept=25)
        self.act(NOW - 3600, at=self.box.wall)
        sleep, why = self.box.idle.tick()
        self.assertFalse(sleep)
        self.assertIn("idle 0 of 30", why)
        self.assertEqual(self.box.slept, 1)                        # no sleep loop every 30 s

    def test_a_refused_sleep_also_waits_out_the_minutes(self):
        self.box.rc = 1
        self.act(NOW - 3600)
        self.box.idle.tick()
        self.assertEqual(self.box.idle.last_resume, NOW)

    def test_a_resume_counts_as_activity(self):
        self.act(NOW - 7200)
        self.box.idle.tick()                                       # first tick: sleeps (idle long enough)
        self.box.slept = 0
        o1idle.write_config({"enabled": True, "minutes": 30})
        self.box.advance(8 * 3600, slept=8 * 3600 - 30)            # 8 hours with the machine suspended
        self.act(NOW - 7200, at=self.box.wall)
        sleep, why = self.box.idle.tick()
        self.assertFalse(sleep)
        self.assertIn("idle 0 of 30", why)
        self.assertEqual(self.box.slept, 0)
        self.box.advance(31 * 60)                                  # awake and untouched for 31 minutes
        self.act(NOW - 7200, at=self.box.wall)
        self.assertTrue(self.box.idle.tick()[0])

    def test_a_small_clock_difference_is_not_a_resume(self):
        self.act(NOW - 60)
        self.box.idle.tick()
        self.box.advance(30, slept=5)                              # under the jump threshold
        self.assertIsNone(self.box.idle.last_resume)

    def test_each_probe_failing_blocks(self):
        self.act(NOW - 3600)
        for name in ("inhibitors", "tools", "busy", "boot_time", "loadavg"):
            box = FakeBox()
            box.p[name] = lambda: (_ for _ in ()).throw(OSError("x"))
            self.assertFalse(box.idle.tick()[0], name)
            self.assertEqual(box.slept, 0, name)

    def test_a_card_whose_probe_fails_blocks_without_a_reading(self):
        self.act(NOW - 3600)
        box = FakeBox()
        box.p["gpu_busy"] = lambda: None
        box.p["gpu_present"] = lambda: (_ for _ in ()).throw(OSError("x"))
        self.assertFalse(box.idle.tick()[0])
        box = FakeBox()
        box.p["gpu_busy"] = lambda: None
        box.p["gpu_present"] = lambda: False
        self.act(NOW - 3600)
        self.assertTrue(box.idle.tick()[0])

    def test_a_refused_sleep_waits_a_whole_idle_period(self):
        self.act(NOW - 3600)
        self.box.rc = 1
        self.assertTrue(self.box.idle.tick()[0])
        self.assertEqual(self.box.slept, 1)
        for _ in range(5):
            self.box.advance(300)
            self.act(NOW - 3600, at=self.box.wall)
            self.assertFalse(self.box.idle.tick()[0] and self.box.wall - NOW < 1800)
        self.assertEqual(self.box.slept, 1)                        # not tried again inside the 30 minutes
        self.box.advance(301)
        self.act(NOW - 3600, at=self.box.wall)
        self.assertTrue(self.box.idle.tick()[0])
        self.assertEqual(self.box.slept, 2)

    def test_it_publishes_the_card_list_and_support(self):
        self.box.idle.tick()
        d_ = json.load(open(o1idle.idle_file()))
        self.assertEqual(d_, {"at": int(NOW), "supported": True, "wake": ["02:00:5e:10:00:01"],
                              "enabled": True, "minutes": 30})   # enabled/minutes: the server panel reads them
        self.assertEqual(o1idle.published_wake(), ["02:00:5e:10:00:01"])

    def test_the_log_has_reasons_only(self):
        self.act(NOW - 3600)
        self.box.idle.tick()
        self.assertEqual(self.box.logs, ["sleeping: idle 60 minutes"])


if __name__ == "__main__":
    unittest.main()
