"""Wi-Fi backup (6b439): the ollama1-wifi tool, the suspend hook's wake-over-Wi-Fi step, the wake list, and
setup.sh's --wifi option; and (6b450) the sysctl file that makes each card answer ARP only for its own addresses,
with status reading the kernel's effective value from a fake /proc. Everything runs against fake sysfs and fake
iw / netplan / ip / apt-get (tests/fakewifi.py); nothing touches a real network, and nothing reads the machine's
own /proc."""
import json
import os
import pty
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1idle
import o1wifi

PW = "Zq9!pw-SENTINEL-7"
SSID = "Home Net 5G"
OLD_YAML = "# the user's own earlier Wi-Fi file\nnetwork:\n  version: 2\n"
BRIDGE = "network:\n  version: 2\n  bridges:\n    br0:\n      interfaces: [enp5s0, enp6s0]\n"
UNIVERSAL = ":".join(["a8", "bb", "cc", "dd", "ee", "01"])          # built, so the personal-values scan sees no real-looking MAC
LOCAL = ":".join(["a2", "bb", "cc", "dd", "ee", "01"])
FAKE = os.path.join(U.HERE, "fakewifi.py")
TOOLS = ["iw", "netplan", "ip", "apt-get", "udevadm", "networkctl", "systemctl"]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1wifi-")
        self.bin = os.path.join(self.dir, "bin")
        self.sys = os.path.join(self.dir, "sys")
        self.net = os.path.join(self.sys, "class", "net")
        os.makedirs(self.bin)
        os.makedirs(self.net)
        for t in TOOLS:
            os.symlink(FAKE, os.path.join(self.bin, t))
        self.state_path = os.path.join(self.dir, "state.json")
        self.fs = {"log": [], "envs": [], "cut_root": self.net}
        self.save()
        self.proc = os.path.join(self.dir, "proc")              # the kernel's settings; absent until proc_sys()
        self.env0 = dict(os.environ)
        os.environ.update(PATH=self.bin + ":" + os.environ["PATH"], FAKE_STATE=self.state_path,
                          OLLAMA1_SYS=self.sys, OLLAMA1_PROC=self.proc, OLLAMA1_WIFI_SETTLE="0")
        os.environ.pop("OLLAMA1_WIFI_PASSWORD", None)
        for sub in ("etc/netplan", "etc/ollama1", "etc/systemd/network", "etc/udev/rules.d", "usr/local/sbin"):
            shutil.rmtree(os.path.join(U.PREFIX, sub), ignore_errors=True)
        os.makedirs(os.path.join(U.PREFIX, "etc", "netplan"))
        self.bridge = os.path.join(U.PREFIX, "etc", "netplan", "60-ollama1-bridge.yaml")
        with open(self.bridge, "w") as f:
            f.write(BRIDGE)
        os.makedirs(os.path.join(U.PREFIX, "etc", "ollama1"), exist_ok=True)
        self.wired("enp5s0", "02:00:5e:10:00:01")
        self.wired("enp6s0", "02:00:5e:10:00:02")
        self.card()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env0)
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- fake machine
    def save(self):
        with open(self.state_path, "w") as f:
            json.dump(self.fs, f)

    def st(self):
        with open(self.state_path) as f:
            return json.load(f)

    def put(self, rel, text):
        path = os.path.join(self.net, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def wired(self, name, mac):
        self.put(name + "/device/.keep", "")
        self.put(name + "/address", mac + "\n")
        self.put(name + "/type", "1\n")
        self.put(name + "/carrier", "1\n")

    def card(self, name="wlo1", mac="02:00:5e:10:00:77", assign="0", phy="phy0"):
        self.put(name + "/device/power/wakeup", "disabled\n")
        self.put(name + "/wireless/.keep", "")
        self.put(name + "/phy80211/name", phy + "\n")
        self.put(name + "/address", mac + "\n")
        self.put(name + "/type", "1\n")
        if assign is not None:
            self.put(name + "/addr_assign_type", assign + "\n")
        elif os.path.exists(os.path.join(self.net, name, "addr_assign_type")):
            os.unlink(os.path.join(self.net, name, "addr_assign_type"))
        self.wakeup = os.path.join(self.net, name, "device", "power", "wakeup")

    def proc_sys(self, **scopes):
        """The kernel's ARP settings as a fake /proc/sys tree: all=(arp_ignore, arp_announce), wlo1=(...)."""
        shutil.rmtree(self.proc, ignore_errors=True)
        for scope, (ignore, announce) in scopes.items():
            d = os.path.join(self.proc, "sys", "net", "ipv4", "conf", scope)
            os.makedirs(d)
            for key, val in (("arp_ignore", ignore), ("arp_announce", announce)):
                with open(os.path.join(d, key), "w") as f:
                    f.write("%s\n" % val)

    def turn_on(self, ssid=None):
        o1wifi.write_state({"enabled": True, **({"ssid": ssid} if ssid else {})})

    def put_netplan(self, text=OLD_YAML):
        with open(o1wifi.netplan_path(), "w") as f:
            f.write(text)
        os.chmod(o1wifi.netplan_path(), 0o600)

    def log(self):
        return self.st()["log"]

    def run_set(self, ssid=SSID, pw=PW, again=None, **kw):
        out = []
        secrets = iter([pw, pw if again is None else again])
        rc = o1wifi.cmd_set([], ask=lambda _: ssid, ask_secret=lambda _: next(secrets), sleep=lambda s: None,
                            out=out.append, **kw)
        return rc, "\n".join(out)

    def everything_text(self):
        """Every place a password could have gone, as one text: the fake tools' argument and environment logs, the
        state file, the files under /etc other than the netplan file."""
        parts = [json.dumps(self.st()["log"]), json.dumps(self.st()["envs"])]
        for sub in ("etc/ollama1", "etc/systemd/network", "etc/udev/rules.d"):
            d = os.path.join(U.PREFIX, sub)
            for n in os.listdir(d) if os.path.isdir(d) else []:
                with open(os.path.join(d, n)) as f:
                    parts.append(f.read())
        return "\n".join(parts)


class TestValidation(unittest.TestCase):
    def test_ssid(self):
        for s in ("a", "Home Net 5G", "x" * 32, "café"):
            self.assertTrue(o1wifi.valid_ssid(s), s)
        for s in ("", "x" * 33, "bad\nname", "tab\tname", "nul\x00", "é" * 17):
            self.assertFalse(o1wifi.valid_ssid(s), repr(s))

    def test_passphrase(self):
        for s in ("12345678", "x" * 63, "with space ok", PW):
            self.assertTrue(o1wifi.valid_passphrase(s), s)
        for s in ("", "1234567", "x" * 64, "cafécafé", "line\nbreak1", "tab\there1"):
            self.assertFalse(o1wifi.valid_passphrase(s), repr(s))

    def test_country_only_when_one_country_owns_the_zone(self):
        d = tempfile.mkdtemp()
        try:
            tab = os.path.join(d, "zone1970.tab")
            with open(tab, "w") as f:
                f.write("# comment\nUS\t+404251-0740023\tAmerica/New_York\tEastern\n"
                        "CH,DE,LI\t+4723+00832\tEurope/Zurich\tGermany, Switzerland\n")
            self.assertEqual(o1wifi.country_for_timezone("America/New_York", tab), "US")
            self.assertIsNone(o1wifi.country_for_timezone("Europe/Zurich", tab))
            self.assertIsNone(o1wifi.country_for_timezone("Nowhere/Land", tab))
            self.assertIsNone(o1wifi.country_for_timezone("America/New_York", os.path.join(d, "none")))
        finally:
            shutil.rmtree(d)


class TestNetplanText(unittest.TestCase):
    def test_the_file(self):
        t = o1wifi.netplan_text("wlo1", SSID, PW, "US")
        lines = t.splitlines()
        for want in ("network:", "  version: 2", "  renderer: networkd", "  wifis:", "    wlo1:", "      dhcp4: true",
                     "      dhcp4-overrides:", "        route-metric: 600", "      optional: true",
                     '      regulatory-domain: "US"', "      access-points:", '        "Home Net 5G":',
                     '          password: "%s"' % PW):
            self.assertIn(want, lines)
        self.assertNotIn("regulatory-domain", o1wifi.netplan_text("wlo1", SSID, PW))

    def test_the_metric_loses_to_the_wired_one(self):
        self.assertGreater(o1wifi.WIFI_METRIC, 100)          # the wired bridge's route is 100
        self.assertIn("route-metric: %d" % o1wifi.WIFI_METRIC, o1wifi.netplan_text("wlo1", SSID, PW))

    def test_quotes_and_backslashes_stay_inside_the_strings(self):
        t = o1wifi.netplan_text("wlo1", 'a"b\\c', 'p"w\\1234', None)
        self.assertIn(r'"a\"b\\c":', t)
        self.assertIn(r'password: "p\"w\\1234"', t)


class TestSet(Fixture):
    def test_set_writes_the_file_and_applies_after_generating(self):
        self.turn_on()
        rc, out = self.run_set()
        self.assertEqual(rc, 0, out)
        path = o1wifi.netplan_path()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        text = open(path).read()
        self.assertIn("wlo1:", text)                                    # the card was found, not assumed
        self.assertIn("route-metric: %d" % o1wifi.WIFI_METRIC, text)
        self.assertIn("renderer: networkd", text)
        self.assertIn("optional: true", text)
        self.assertIn(PW, text)
        self.assertIn(SSID, text)
        log = self.log()
        self.assertEqual([c for c in log if c.startswith("netplan")], ["netplan generate", "netplan apply"])
        self.assertEqual(open(self.bridge).read(), BRIDGE)               # the bridge file is untouched
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["60-ollama1-bridge.yaml", "70-ollama1-wifi.yaml"])
        self.assertEqual(o1wifi.read_state()["ssid"], SSID)

    def test_the_password_goes_nowhere_but_the_file(self):
        self.turn_on()
        os.environ["OLLAMA1_WIFI_PASSWORD"] = "from-the-environment-123"
        rc, out = self.run_set()
        self.assertEqual(rc, 0)
        self.assertNotIn(PW, out)                                         # not printed
        self.assertNotIn(PW, self.everything_text())                      # not in an argument, an environment, a state file
        self.assertNotIn("from-the-environment", open(o1wifi.netplan_path()).read())   # the environment is never read
        r = o1wifi.status_lines()
        self.assertNotIn(PW, "\n".join(r))

    def test_arguments_are_refused_and_never_echoed(self):
        self.turn_on()
        out = []
        def eof(_):
            raise EOFError
        rc = o1wifi.cmd_set(["MyPassword123"], ask=eof, ask_secret=eof, out=out.append)
        self.assertEqual(rc, 2)
        self.assertNotIn("MyPassword123", "\n".join(out))
        self.assertFalse(o1wifi.configured())
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-wifi"), "set", "MyPassword123"],
                           capture_output=True, text=True, env=dict(os.environ, OLLAMA1_PREFIX=U.PREFIX), timeout=30,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("MyPassword123", r.stdout + r.stderr)

    def test_bad_input_changes_nothing(self):
        self.turn_on()
        for kw, word in ((dict(ssid=""), "1 to 32"), (dict(ssid="x" * 33), "1 to 32"), (dict(pw="short"), "8 to 63"),
                         (dict(pw="x" * 64), "8 to 63"), (dict(again="different-one-9"), "differ")):
            rc, out = self.run_set(**kw)
            self.assertEqual(rc, 1, kw)
            self.assertIn(word, out)
            self.assertNotIn(PW, out)
            self.assertFalse(o1wifi.configured())
        self.assertEqual([c for c in self.log() if c.startswith("netplan")], [])

    def test_off_or_no_card_is_refused(self):
        rc, out = self.run_set()
        self.assertEqual(rc, 1)
        self.assertIn("--wifi on", out)
        self.turn_on()
        shutil.rmtree(os.path.join(self.net, "wlo1"))
        rc, out = self.run_set()
        self.assertEqual(rc, 1)
        self.assertIn("No Wi-Fi card", out)
        self.assertFalse(o1wifi.configured())

    def test_a_netplan_that_refuses_it_puts_the_old_file_back_and_never_applies(self):
        self.turn_on()
        self.put_netplan()
        self.fs["fail"], self.fs["fail_text"] = "generate", "password: %s is wrong" % PW
        self.save()
        rc, out = self.run_set()
        self.assertEqual(rc, 1)
        self.assertEqual(open(o1wifi.netplan_path()).read(), OLD_YAML)
        self.assertEqual(stat.S_IMODE(os.stat(o1wifi.netplan_path()).st_mode), 0o600)
        self.assertNotIn("netplan apply", self.log())                    # generate first: refused, so no apply at all
        self.assertNotIn(PW, out)                                         # netplan's own text is redacted
        self.assertNotIn(SSID, out)

    def test_a_failed_apply_restores_the_old_file_or_removes_the_new_one(self):
        self.turn_on()
        for had_old in (True, False):
            if had_old:
                self.put_netplan()
            self.fs["fail"], self.fs["fail_text"] = "apply", "boom"
            self.save()
            rc, out = self.run_set()
            self.assertEqual(rc, 1, out)
            self.assertIn("previous setting is back", out)
            if had_old:
                self.assertEqual(open(o1wifi.netplan_path()).read(), OLD_YAML)
            else:
                self.assertFalse(o1wifi.configured())
            self.assertEqual(open(self.bridge).read(), BRIDGE)
            self.assertNotIn(PW, out)
            self.assertIsNone(o1wifi.read_state().get("ssid"))
            os.unlink(o1wifi.netplan_path()) if os.path.exists(o1wifi.netplan_path()) else None

    def test_an_apply_that_drops_the_cable_is_rolled_back(self):
        self.turn_on()
        self.put_netplan()
        self.fs["cut_wired"] = ["enp5s0"]
        self.save()
        rc, out = self.run_set()
        self.assertEqual(rc, 1)
        self.assertIn("wired link down", out)
        self.assertEqual(open(o1wifi.netplan_path()).read(), OLD_YAML)

    def test_a_running_pty_session_hides_the_password(self):
        # the real command, on a terminal: the password is typed, never echoed, never in the process list
        self.turn_on()
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX)
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, os.path.join(U.BIN, "ollama1-wifi"), "set"], env)
        seen = b""

        def read_until(word):
            nonlocal seen
            while word not in seen.lower():
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                seen += chunk

        read_until(b"ssid): ")
        os.write(fd, (SSID + "\n").encode())
        read_until(b"password (not shown): ")
        os.write(fd, (PW + "\n").encode())
        read_until(b"again: ")
        os.write(fd, (PW + "\n").encode())
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                seen += chunk
        except OSError:
            pass
        _, code = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(code), 0, seen)
        self.assertNotIn(PW.encode(), seen)                               # no echo
        self.assertIn(PW, open(o1wifi.netplan_path()).read())
        self.assertEqual(stat.S_IMODE(os.stat(o1wifi.netplan_path()).st_mode), 0o600)
        self.assertNotIn(PW, self.everything_text())

    def test_remove_deletes_only_its_file(self):
        self.turn_on(SSID)
        self.assertEqual(self.run_set()[0], 0)
        out = []
        self.assertEqual(o1wifi.cmd_remove(sleep=lambda s: None, out=out.append), 0)
        self.assertFalse(o1wifi.configured())
        self.assertEqual(open(self.bridge).read(), BRIDGE)
        self.assertNotIn("ssid", o1wifi.read_state())
        self.assertEqual([c for c in self.log() if c.startswith("netplan")][-2:], ["netplan generate", "netplan apply"])


class TestStatus(Fixture):
    def test_status_lines(self):
        self.turn_on(SSID)
        self.put_netplan()
        self.fs.update(link='Connected to aa:bb:cc:dd:ee:ff (on wlo1)\n\tSSID: Home Net 5G\n\tsignal: -52 dBm\n',
                       addr="3: wlo1    inet 192.0.2.9/24 brd 192.0.2.255 scope global wlo1\n",
                       routes="default via 192.0.2.1 dev br0 proto dhcp metric 100\n"
                              "default via 192.0.2.1 dev wlo1 proto dhcp metric 600\n")
        self.save()
        self.proc_sys(all=(1, 2))
        t = "\n".join(o1wifi.status_lines())
        for want in ("Wi-Fi backup: on", "wlo1, MAC 02:00:5e:10:00:77", 'Network: set ("Home Net 5G")',
                     'connected to "Home Net 5G", signal -52 dBm', "Address: 192.0.2.9/24",
                     "enp5s0 up, enp6s0 up", "Default route: br0 (metric 100)",
                     "Answers ARP only for its own address: yes", "(WoWLAN): yes",
                     "Published to the app as a wake card: 02:00:5e:10:00:77"):
            self.assertIn(want, t)

    def test_status_when_off_and_unset(self):
        t = "\n".join(o1wifi.status_lines())
        self.assertIn("Wi-Fi backup: off", t)
        self.assertIn("Network: not set (sudo ollama1-wifi set)", t)
        self.assertIn("Link: not connected", t)
        self.assertIn("Answers ARP only for its own address: no (sudo ./setup.sh", t)      # no /proc tree: not set
        self.assertIn("wake card: no", t)


class TestEachCardAnswersForItself(Fixture):
    """6b450: with Linux's default the Wi-Fi card answered ARP for the wired bridge's address too, and a client whose
    cache took that reply sent the server's traffic to a card in power save (SSH to the server timed out). The kit's
    sysctl file makes every card answer only for its own addresses, and status says whether the kernel is set so."""

    LINE = "Answers ARP only for its own address: "

    def arp_lines(self):
        return [l for l in o1wifi.status_lines() if l.startswith(self.LINE)]

    def test_the_sysctl_file_sets_exactly_the_two_keys_for_every_card(self):
        with open(os.path.join(U.CONFIG, o1wifi.ARP_CONF)) as f:
            text = f.read()
        keys = {}
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                k, _, v = s.partition("=")
                keys[k.strip()] = v.strip()
        self.assertEqual(keys, {"net.ipv4.conf.all.arp_ignore": "1", "net.ipv4.conf.all.arp_announce": "2"})
        # "all" (the kernel takes the larger of it and a card's own): no card name, nothing to time with the card's
        # creation; and the keys always exist, so there is no leading "-" to hide a typo behind
        self.assertNotIn("conf.wl", text)
        self.assertNotIn("conf.default", text)
        self.assertIn("6b450", text)

    def test_setup_installs_it_on_every_server_and_applies_it_now(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        inst = 'install -m 0644 "$KIT/config/%s" /etc/sysctl.d/%s' % (o1wifi.ARP_CONF, o1wifi.ARP_CONF)
        self.assertEqual(setup.count(inst), 1)
        i = setup.index(inst)
        self.assertLess(setup.index('install -m 0644 "$KIT/config/60-ollama1-bridge.conf"'), i)   # beside the bridge file
        self.assertLess(i, setup.index('step "Firewall"'))                     # in the kit-files step, every server
        self.assertNotIn('step "Wi-Fi backup"', setup[:i])                    # not only with --wifi on
        applied = [l for l in setup.splitlines() if l.startswith("sysctl -q -p /etc/sysctl.d/" + o1wifi.ARP_CONF)]
        self.assertEqual(len(applied), 1)
        self.assertNotIn("|| true", applied[0])                                # a failure is said, not swallowed
        self.assertNotIn("2>/dev/null", applied[0])
        self.assertIn("|| note", applied[0])

    def test_status_reads_the_kernels_effective_value(self):
        self.turn_on(SSID)
        self.put_netplan()
        self.assertEqual(self.arp_lines(), [self.LINE + "no (sudo ./setup.sh installs /etc/sysctl.d/%s)" % o1wifi.ARP_CONF])
        for scopes, want in (
                (dict(all=(1, 2)), "yes"),
                (dict(all=(2, 2)), "yes"),                          # stricter still counts
                (dict(all=(0, 0), wlo1=(1, 2)), "yes"),             # the card's own value: the kernel takes the larger
                (dict(all=(1, 2), wlo1=(0, 0)), "yes"),
                (dict(all=(0, 0)), "no"),                           # Linux's default: the symptom
                (dict(all=(1, 0)), "no"),                           # answers right, but asks with any address
                (dict(all=(0, 2)), "no"),
                (dict(all=(1, 1)), "no"),                           # arp_announce 1 does not help on one subnet
                (dict(all=(8, 2)), "no"),                           # 8: never answers at all
                (dict(all=("x", 2)), "no"),                         # unreadable: no, and no error
        ):
            self.proc_sys(**scopes)
            lines = self.arp_lines()
            self.assertEqual(len(lines), 1, scopes)
            self.assertTrue(lines[0].startswith(self.LINE + want), (scopes, lines))

    def test_the_effective_value_is_the_larger_of_all_and_the_cards_own(self):
        self.proc_sys(all=(1, 0), wlo1=(0, 2))
        self.assertEqual(o1wifi.arp_setting("arp_ignore", "wlo1"), 1)
        self.assertEqual(o1wifi.arp_setting("arp_announce", "wlo1"), 2)
        self.assertEqual(o1wifi.arp_setting("arp_announce"), 0)                # "all" alone
        self.assertEqual(o1wifi.arp_setting("arp_ignore", "enp5s0"), 1)        # a card with no file of its own
        self.assertTrue(o1wifi.arp_own_only("wlo1"))
        self.assertFalse(o1wifi.arp_own_only())
        self.assertFalse(o1wifi.arp_own_only("wlo1", proc=os.path.join(self.dir, "none")))

    def test_no_card_no_line(self):
        self.proc_sys(all=(1, 2))
        shutil.rmtree(os.path.join(self.net, "wlo1"))
        self.assertEqual(self.arp_lines(), [])


class TestWakeList(Fixture):
    def test_published_only_when_on_set_up_real_and_supported(self):
        self.assertEqual(o1wifi.wake_macs(), [])                          # off
        self.put_netplan()
        self.assertEqual(o1wifi.wake_macs(), [])                          # a network set, but the feature off
        os.unlink(o1wifi.netplan_path())
        self.turn_on()
        self.assertEqual(o1wifi.wake_macs(), [])                          # on, but no network set
        self.put_netplan()
        self.assertEqual(o1wifi.wake_macs(), ["02:00:5e:10:00:77"])
        self.fs["magic"] = False
        self.save()
        self.assertEqual(o1wifi.wake_macs(), [])                          # the card can't wake on a magic packet
        self.fs["magic"] = True
        self.save()
        self.card(assign="1")                                             # a random address
        self.assertEqual(o1wifi.wake_macs(), [])
        self.card(mac=UNIVERSAL, assign=None)                   # kind unknown, universally administered
        self.assertEqual(o1wifi.wake_macs(), [UNIVERSAL])
        self.card(mac=LOCAL, assign=None)             # kind unknown, locally administered (random)
        self.assertEqual(o1wifi.wake_macs(), [])
        self.fs["iw_fail"] = True
        self.save()
        self.card()
        self.assertEqual(o1wifi.wake_macs(), [])                          # iw failing: nothing, no error

    def test_wake_list_adds_it_after_the_wired_cards(self):
        self.turn_on()
        self.put_netplan()
        got = o1idle.wake_list(None, lambda n: (True, True))
        self.assertEqual(got, ["02:00:5e:10:00:01", "02:00:5e:10:00:02", "02:00:5e:10:00:77"])
        self.assertEqual(o1idle.wake_list(None, lambda n: (True, False)), ["02:00:5e:10:00:77"])
        self.assertEqual(o1idle.wake_list([("a", "02:00:5e:10:00:01")], lambda n: (True, True)), ["02:00:5e:10:00:01"])
        self.assertEqual(o1idle.wake_list([], lambda n: None, wifi=lambda: ["02:00:5e:10:00:77", "<bad>"]),
                         ["02:00:5e:10:00:77"])
        self.assertEqual(o1idle.wake_list([("a", "02:00:5e:10:00:77")], lambda n: (True, True),
                                          wifi=lambda: ["02:00:5e:10:00:77"]), ["02:00:5e:10:00:77"])      # no duplicates

    def test_not_published_when_off_or_unset(self):
        self.assertEqual(o1idle.wake_list(None, lambda n: (True, True)), ["02:00:5e:10:00:01", "02:00:5e:10:00:02"])
        self.turn_on()
        self.assertNotIn("02:00:5e:10:00:77", o1idle.wake_list(None, lambda n: (True, True)))

    def test_the_cap_holds(self):
        macs = ["02:00:5e:10:01:%02x" % i for i in range(20)]
        self.assertEqual(len(o1idle.wake_list([], lambda n: None, wifi=lambda: macs)), o1idle.MAX_WAKE)

    def test_a_broken_probe_costs_nothing(self):
        def boom():
            raise RuntimeError("x")
        self.assertEqual(o1idle.wake_list([("a", "02:00:5e:10:00:01")], lambda n: (True, True), wifi=lambda: []),
                         ["02:00:5e:10:00:01"])
        self.assertEqual(o1idle.wifi_wake(), [])


class TestHook(Fixture):
    def setUp(self):
        super().setUp()
        self.hook = os.path.join(U.CONFIG, "ollama1-sleep-hook")
        self.helper = os.path.join(self.dir, "helper")
        with open(self.helper, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        self.tool = os.path.join(self.dir, "wifitool")
        self.write_tool('exec "%s" "%s" "$@"' % (sys.executable, os.path.join(U.BIN, "ollama1-wifi")))
        os.chmod(self.helper, 0o755)

    def write_tool(self, body):
        with open(self.tool, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(self.tool, 0o755)

    def hook_run(self, when, tool=None):
        env = dict(os.environ, OLLAMA1_PREFIX=U.PREFIX, OLLAMA1_HELPER=self.helper,
                   OLLAMA1_WIFI_TOOL=tool or self.tool, OLLAMA1_WATCHDOG=os.path.join(self.dir, "none"))
        return subprocess.run(["sh", self.hook, when, "suspend"], env=env, capture_output=True, text=True, timeout=60)

    def wow(self):
        return [c for c in self.log() if c.startswith("iw phy phy0 wowlan")]

    def test_pre_turns_wake_on_with_the_right_phy_and_post_turns_it_off(self):
        self.turn_on()
        self.put_netplan()
        self.card(phy="phy3")
        r = self.hook_run("pre")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("iw phy phy3 wowlan enable magic-packet", self.log())
        self.assertNotIn("disconnect", self.log())   # 6b457: a dropped association must not wake it
        self.assertEqual(open(self.wakeup).read(), "enabled")           # PCI wake is on for the card
        r = self.hook_run("post")
        self.assertEqual(r.returncode, 0)
        self.assertIn("iw phy phy3 wowlan disable", self.log())
        self.assertIn("networkctl reconfigure wlo1", self.log())

    def test_nothing_happens_when_off_unset_or_no_card(self):
        self.put_netplan()
        self.assertEqual(self.hook_run("pre").returncode, 0)             # off
        self.turn_on()
        os.unlink(o1wifi.netplan_path())
        self.assertEqual(self.hook_run("pre").returncode, 0)             # no network set
        self.put_netplan()
        shutil.rmtree(os.path.join(self.net, "wlo1"))
        self.assertEqual(self.hook_run("pre").returncode, 0)             # no card
        self.assertEqual([c for c in self.log() if c.startswith(("iw", "networkctl"))], [])

    def test_a_missing_tool_is_fine(self):
        self.assertEqual(self.hook_run("pre", tool=os.path.join(self.dir, "absent")).returncode, 0)

    def test_a_failing_or_noisy_tool_never_fails_the_suspend(self):
        self.write_tool("echo noise; echo more >&2; exit 9")
        for when in ("pre", "post"):
            r = self.hook_run(when)
            self.assertEqual(r.returncode, 0, when)
            self.assertNotIn("noise", r.stdout + r.stderr)
        self.turn_on()
        self.put_netplan()
        self.write_tool('exec "%s" "%s" "$@"' % (sys.executable, os.path.join(U.BIN, "ollama1-wifi")))
        self.fs["iw_fail"] = True
        self.save()
        self.assertEqual(self.hook_run("pre").returncode, 0)             # iw failing: the suspend goes on

    def test_the_hook_text(self):
        h = open(self.hook).read()
        self.assertIn('[ ! -x "$WIFI" ] || "$WIFI" wowlan enable >/dev/null 2>&1 || true', h)
        self.assertIn('[ ! -x "$WIFI" ] || "$WIFI" wowlan disable >/dev/null 2>&1 || true', h)
        self.assertTrue(h.rstrip().endswith("exit 0"))


class TestSetupStep(Fixture):
    def go(self, choice, **kw):
        out = []
        kw.setdefault("which", lambda exe: "/usr/bin/" + exe)
        kw.setdefault("apt", lambda pk: True)
        rc = o1wifi.setup(choice, log=out.append, tool="/opt/kit/bin/ollama1-wifi", **kw)
        return rc, "\n".join(out)

    def test_on_installs_the_pieces_and_configures_no_network(self):
        before = sorted(os.listdir(os.path.join(U.PREFIX, "etc", "netplan")))
        asked = []
        rc, out = self.go("on", which=lambda exe: None, apt=lambda pk: asked.append(list(pk)) or True)
        self.assertEqual(rc, 0)
        self.assertEqual(asked, [["iw", "wpasupplicant"]])
        self.assertTrue(o1wifi.enabled())
        self.assertIn("sudo ollama1-wifi set", out)
        self.assertEqual(sorted(os.listdir(os.path.join(U.PREFIX, "etc", "netplan"))), before)       # no netplan file
        self.assertEqual(open(self.bridge).read(), BRIDGE)
        link = open(o1wifi.link_path()).read()
        self.assertIn("Type=wlan", link)
        self.assertIn("NamePolicy=keep kernel database onboard slot path", link)
        self.assertIn("MACAddressPolicy=persistent", link)
        self.assertNotIn("MACAddressPolicy=random", link)
        self.assertIn('ATTR{power/wakeup}="enabled"', open(o1wifi.udev_path()).read())
        self.assertEqual(open(self.wakeup).read(), "enabled")
        self.assertEqual(os.readlink(o1wifi.tool_link()), "/opt/kit/bin/ollama1-wifi")
        self.assertEqual([c for c in self.log() if c.startswith("netplan")], [])

    def test_only_what_is_missing_is_installed(self):
        asked = []
        self.go("on", which=lambda exe: "/x" if exe == "iw" else None, apt=lambda pk: asked.append(list(pk)) or True)
        self.assertEqual(asked, [["wpasupplicant"]])
        asked.clear()
        self.go("on", apt=lambda pk: asked.append(list(pk)) or True)
        self.assertEqual(asked, [])

    def test_a_failed_install_leaves_it_off(self):
        rc, out = self.go("on", which=lambda exe: None, apt=lambda pk: False)
        self.assertEqual(rc, 1)
        self.assertFalse(o1wifi.enabled())
        self.assertFalse(os.path.exists(o1wifi.udev_path()))

    def test_again_changes_nothing_and_keeps_the_network(self):
        self.go("on")
        self.put_netplan()
        o1wifi.write_state({"enabled": True, "ssid": SSID})
        self.go("on")
        self.assertEqual(open(o1wifi.netplan_path()).read(), OLD_YAML)
        self.assertEqual(o1wifi.read_state()["ssid"], SSID)
        self.assertEqual([c for c in self.log() if c.startswith("netplan")], [])

    def test_off_removes_what_it_installed_and_leaves_the_users_files(self):
        self.go("on")
        self.assertEqual(self.run_set()[0], 0)
        mine = os.path.join(U.PREFIX, "etc", "netplan", "50-users-own.yaml")
        with open(mine, "w") as f:
            f.write("network:\n  version: 2\n")
        rc, out = self.go("off")
        self.assertEqual(rc, 0)
        for path in (o1wifi.netplan_path(), o1wifi.link_path(), o1wifi.udev_path(), o1wifi.state_path(),
                     o1wifi.tool_link()):
            self.assertFalse(os.path.lexists(path), path)
        self.assertEqual(open(self.bridge).read(), BRIDGE)
        self.assertTrue(os.path.exists(mine))
        self.assertIn("iw phy phy0 wowlan disable", self.log())
        self.assertEqual([c for c in self.log() if c.startswith("netplan")][-2:], ["netplan generate", "netplan apply"])
        self.assertEqual(o1wifi.wake_macs(), [])
        rc, out = self.go("off")                                          # again: nothing to do, no error
        self.assertEqual(rc, 0)

    def test_the_off_default_touches_nothing(self):
        rc, out = self.go("off")
        self.assertEqual(rc, 0)
        self.assertEqual(self.log(), [])
        self.assertEqual(open(self.bridge).read(), BRIDGE)


class TestSetupSh(unittest.TestCase):
    def bash(self, script):
        r = subprocess.run(["bash", "-c", ". %s; %s" % (shlex.quote(os.path.join(U.LIB, "setuplib.sh")), script)],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def read(self, *p):
        with open(os.path.join(U.KIT, *p)) as f:
            return f.read()

    def test_the_choice_is_off_unless_turned_on_and_a_saved_choice_is_kept(self):
        for args, want in (('"" "" ""', "off"), ('on "" ""', "on"), ('off "" ""', "off"), ('"" 1 ""', "on"),
                           ('"" 0 on', "off"), ('"" "" on', "on"), ('"" "" off', "off"), ('off 1 on', "off"),
                           ('on 0 off', "on")):
            self.assertEqual(self.bash("wifi_choice " + args), (0, want), args)
        self.assertEqual(self.bash('wifi_choice "" "" maybe')[0], 1)
        self.assertEqual(self.bash('wifi_choice maybe "" ""')[0], 1)

    def test_the_plan_lines(self):
        on = self.bash("wifi_plan on")[1]
        self.assertIn("Wi-Fi backup ON", on)
        self.assertIn("apt-get", on)
        self.assertIn("does not configure any network", on)
        self.assertIn("sudo ollama1-wifi set", on)
        off = self.bash("wifi_plan off")[1]
        self.assertIn("Wi-Fi backup OFF", off)
        self.assertIn("nothing installed", off)

    def test_the_flag_is_in_the_help_and_a_bad_value_is_refused(self):
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--help"], capture_output=True, text=True)
        self.assertIn("--wifi on|off", r.stdout)
        self.assertIn("OLLAMA1_WIFI=1|0", r.stdout)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--wifi", "maybe", "--plan"], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--wifi takes on or off", r.stdout)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--wifi"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run(["bash", os.path.join(U.KIT, "setup.sh"), "--plan"], capture_output=True, text=True,
                           env=dict(os.environ, OLLAMA1_WIFI="sometimes"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("OLLAMA1_WIFI takes 1 or 0", r.stdout)

    def test_setup_wiring(self):
        s = self.read("setup.sh")
        self.assertIn('WIFI=$(wifi_choice "$A_WIFI" "${OLLAMA1_WIFI:-}" "$(saved WIFI)")', s)
        self.assertIn("printf 'WIFI=%s\\n' \"$WIFI\"", s)
        self.assertIn('$(wifi_plan "$WIFI")', s)
        self.assertIn("--leds|--wifi|", s)
        a = s.index('step "Wi-Fi backup"')
        self.assertLess(s.index('step "Hardware watchdog"'), a)
        self.assertLess(a, s.index('step "Cloudflare Tunnel and Access"'))
        self.assertIn('"$LIBDIR/bin/ollama1-wifi" setup "$WIFI"', s[a:s.index('step "Cloudflare Tunnel and Access"')])
        self.assertNotIn("/etc/netplan", s.split('step "Wi-Fi backup"')[1].split('step "Cloudflare')[0].replace(
            "touches no netplan file", "").replace("netplan file", ""))

    def test_the_readme_has_the_section(self):
        r = self.read("README.md")
        self.assertIn("## Wi-Fi backup", r)
        self.assertIn("| `--wifi on\\|off` |", r)
        sec = r.split("## Wi-Fi backup")[1].split("\n## ")[0]
        for word in ("sudo ollama1-wifi set", "never", "metric", "S3", "access point", "70-ollama1-wifi.yaml",
                     "61-ollama1-arp.conf", "arp_ignore", "Answers ARP only for its own address"):
            self.assertIn(word, sec)
        self.assertIn("61-ollama1-arp.conf", r.split("## The network")[1].split("## Wi-Fi backup")[0])


if __name__ == "__main__":
    unittest.main()
