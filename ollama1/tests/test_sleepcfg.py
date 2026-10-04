"""The auto sleep setting stays saved (6b370): across a gateway restart, the
idle service's restart, a reboot and a setup.sh run. The file is the only
copy; the app reads it back through /v1/sleep-config."""
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from unittest import mock

import o1test_util as U
import o1idle
from o1common import DEFAULTS, Paths

SETUP = open(os.path.join(U.KIT, "setup.sh")).read()
NOW = 1_800_000_000.0


def new_gateway():
    """A gateway process starting from scratch (what a restart is)."""
    loader = importlib.machinery.SourceFileLoader("o1gateway_cfg", os.path.join(U.BIN, "ollama1-gateway"))
    spec = importlib.util.spec_from_loader("o1gateway_cfg", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    cfg = dict(DEFAULTS)
    cfg.update(U.BASE_CFG)
    cfg.update({"access_team_domain": U.TEAM, "gateway_aud": U.GW_AUD, "ollama_url": "http://127.0.0.1:9"})
    return mod.Gateway(cfg)


def new_idle_service(logs):
    """The root service starting from scratch."""
    p = dict(supported=lambda: True, wake=lambda: [], inhibitors=lambda: [], tools=lambda: [], gpu_busy=lambda: 1,
             gpu_present=lambda: True, loadavg=lambda: 0.1, busy=lambda: [], boot_time=lambda: NOW - 7200)
    return o1idle.Idle(p, lambda: 0, log=logs.append, clock=lambda: NOW, mono=lambda: 1000.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self.clean)
        self.clean()

    def clean(self):
        for f in (o1idle.config_file(), o1idle.idle_file(), o1idle.activity_file()):
            if os.path.exists(f):
                os.unlink(f)


class TestSurvives(Base):
    def test_a_gateway_restart_keeps_it(self):
        gw1 = new_gateway()
        self.assertEqual((gw1.sleep_view()["enabled"], gw1.sleep_view()["minutes"]), (False, 30))
        gw1.sleep_set({"enabled": True, "minutes": 45})
        gw2 = new_gateway()                       # the gateway restarted: new object, nothing in memory
        v = gw2.sleep_view()
        self.assertEqual((v["enabled"], v["minutes"]), (True, 45))
        gw2.sleep_set({"minutes": 12})            # a change keeps the other half
        v = new_gateway().sleep_view()
        self.assertEqual((v["enabled"], v["minutes"]), (True, 12))

    def test_the_idle_service_restart_reads_it_back(self):
        new_gateway().sleep_set({"enabled": True, "minutes": 20})
        with open(o1idle.activity_file(), "w") as f:
            json.dump({"last": NOW - 3600, "inflight": 0, "at": NOW}, f)
        for _ in range(2):                         # the service, then a restarted service
            logs = []
            self.assertEqual(new_idle_service(logs).tick(), (True, "idle 60 minutes"))
            self.assertNotIn("not sleeping: auto sleep is off", logs)
        self.assertEqual(o1idle.read_config(), {"enabled": True, "minutes": 20})

    def test_it_lives_in_a_state_folder_not_in_run(self):
        # /run (and /tmp) are emptied at every boot; /var/lib is not
        f = o1idle.config_file()
        self.assertTrue(f.startswith(Paths.gw_state + os.sep))
        self.assertFalse(f.startswith(Paths.run + os.sep))
        # the service that writes it has exactly that folder as its state directory
        unit = open(os.path.join(U.KIT, "systemd", "ollama1-gateway.service")).read()
        self.assertIn("StateDirectory=ollama1-gateway\n", unit)
        self.assertRegex(unit, r"(?m)^User=o1gw$")
        self.assertNotRegex(unit, r"(?m)^DynamicUser")

    def test_a_reboot_finds_the_file_whole_and_private(self):
        new_gateway().sleep_set({"enabled": True, "minutes": 33})
        f = o1idle.config_file()
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        self.assertEqual(json.load(open(f)), {"enabled": True, "minutes": 33})
        self.assertEqual([n for n in os.listdir(os.path.dirname(f)) if n.startswith(".tmp-")], [])
        # what a reboot wipes (/run) is the activity and the card list, not the setting
        for p in (o1idle.idle_file(), o1idle.activity_file()):
            self.assertTrue(p.startswith(Paths.run + os.sep))
        self.assertEqual(o1idle.read_config(), {"enabled": True, "minutes": 33})

    def test_the_write_is_flushed_to_disk_file_and_folder(self):
        synced = []
        real = os.fsync

        def spy(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)
        with mock.patch("os.fsync", side_effect=spy):
            o1idle.write_config({"enabled": True, "minutes": 9})
        self.assertEqual(sorted(synced), [False, True])      # the file, then the folder entry of the rename

    def test_a_failed_write_keeps_the_old_setting(self):
        o1idle.write_config({"enabled": True, "minutes": 9})
        with mock.patch("os.replace", side_effect=OSError(28, "No space left")):
            with self.assertRaises(OSError):
                o1idle.write_config({"enabled": False, "minutes": 60})
        self.assertEqual(o1idle.read_config(), {"enabled": True, "minutes": 9})
        self.assertEqual([n for n in os.listdir(os.path.dirname(o1idle.config_file())) if n.startswith(".tmp-")], [])

    def test_the_setting_is_in_the_nightly_backup(self):
        src = open(os.path.join(U.BIN, "ollama1-helper")).read()
        self.assertIn('os.path.join(Paths.gw_state, "sleep.json")', src)


class TestIdleFilePublishesTheSetting(Base):
    """The server panel reads enabled and minutes from /run/ollama1/idle.json."""

    def tick_and_read(self):
        new_idle_service([]).tick()
        return json.load(open(o1idle.idle_file()))

    def test_enabled_and_minutes_are_published_with_the_old_fields(self):
        d = self.tick_and_read()                       # nothing saved: off, 30
        self.assertEqual((d["enabled"], d["minutes"]), (False, 30))
        self.assertIn("supported", d)
        self.assertIn("wake", d)
        o1idle.write_config({"enabled": True, "minutes": 45})
        d = self.tick_and_read()
        self.assertIs(d["enabled"], True)
        self.assertEqual(d["minutes"], 45)
        self.assertEqual(sorted(d), ["at", "enabled", "minutes", "supported", "wake"])

    def test_the_gateway_still_reads_the_wake_list_from_it(self):
        o1idle.write_config({"enabled": True, "minutes": 10})
        self.tick_and_read()
        self.assertEqual(o1idle.published_wake(), [])


class TestSetupLeavesItAlone(unittest.TestCase):
    """setup.sh is run again for every update and with --no-... flags; none of that may reset the file."""

    def test_setup_never_writes_or_removes_the_setting(self):
        for word in ("sleep.json", "sleep-config", "write_config", "o1idle", "gw_state"):
            self.assertNotIn(word, SETUP, word)
        for line in SETUP.splitlines():
            if "/var/lib/ollama1-gateway" in line:
                self.fail("setup.sh touches the gateway's state folder: " + line.strip())

    def test_the_idle_service_is_only_enabled_and_restarted(self):
        # the only things setup does with the idle service; the service itself only reads the file
        for line in SETUP.splitlines():
            if "ollama1-idle" in line and not line.lstrip().startswith("#"):
                self.assertRegex(line, r"systemctl (enable|restart) ollama1-idle|bin/ollama1-idle\" wol-setup|for s in |ok |note ")
        for flag in ("--no-gpu-tune", "--no-tmux", "--no-console-font", "--remove-encrypted-swap",
                     "--remove-setup-key", "--skip-cloudflare"):
            self.assertIn(flag, SETUP)

    def test_tmpfiles_does_not_clean_it(self):
        t = open(os.path.join(U.CONFIG, "ollama1.tmpfiles")).read()
        self.assertNotIn("ollama1-gateway", t)
        self.assertNotRegex(t, r"(?m)^[rRxX]\s")

    def test_the_idle_service_only_reads_it(self):
        unit = open(os.path.join(U.KIT, "systemd", "ollama1-idle.service")).read()
        self.assertNotIn("ExecStartPre", unit)
        self.assertNotIn("/var/lib/ollama1-gateway", unit)
        self.assertNotIn("write_config", open(os.path.join(U.BIN, "ollama1-idle")).read())


class TestStatusCommand(Base):
    def run_status(self):
        r = subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-idle"), "status"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_status_prints_the_saved_setting(self):
        self.assertIn("auto sleep: off", self.run_status())
        o1idle.write_config({"enabled": True, "minutes": 45})
        out = self.run_status()
        self.assertIn("auto sleep: on, after 45 minutes idle", out)
        self.assertIn(o1idle.config_file(), out)
        o1idle.write_config({"enabled": False, "minutes": 15})
        out = self.run_status()
        self.assertIn("auto sleep: off", out)
        self.assertIn("15 minutes", out)

    def test_status_is_in_the_usage_text(self):
        self.assertIn("ollama1-idle status", open(os.path.join(U.BIN, "ollama1-idle")).read())


if __name__ == "__main__":
    unittest.main()
