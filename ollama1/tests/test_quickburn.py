"""tools/quick-burn.sh (6b418): the 30 s processor burn then the 30 s graphics-card burn, with fake
stress-ng, gpu-burn, journalctl, systemctl, sysfs and /proc. Plus the unit's text and the polkit rule."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

import o1test_util as U

SCRIPT = os.path.join(U.TOOLS, "quick-burn.sh")


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


class Box:
    """A scratch machine for the script."""

    def __init__(self, test):
        self.d = tempfile.mkdtemp(prefix="o1qb-")
        test.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        d = self.d
        self.bin, self.run, self.state = d + "/bin", d + "/run", d + "/state"
        self.log = d + "/calls.log"
        os.makedirs(self.run + "/quickburn")
        os.makedirs(self.state)
        write(self.bin + "/stress-ng", '#!/bin/sh\necho "stress-ng $*" >>"$FAKE_LOG"\nt=1\nwhile [ $# -gt 0 ]; do [ "$1" = -t ] && t=${2%s}; shift; done\n'
              'sleep "${FAKE_STRESS_SLEEP:-$t}"\nexit "${FAKE_STRESS_RC:-0}"\n', 0o755)
        write(self.bin + "/journalctl", '#!/bin/sh\ncat "$FAKE_JOURNAL" 2>/dev/null\nexit 0\n', 0o755)
        write(self.bin + "/systemctl", '#!/bin/sh\ncat "$FAKE_UNITS" 2>/dev/null\nexit 0\n', 0o755)
        write(d + "/gpu-burn.sh", '#!/bin/sh\necho "gpu-burn $* secs=$O1_BURN_SECONDS" >>"$FAKE_LOG"\n'
              'case "${FAKE_GPU:-ok}" in\n'
              ' missing) echo "llama-bench not found: set O1_LLAMA_BENCH"; exit 1 ;;\n'
              ' fail) echo "ollama1 gpu burn x"; sleep 1; echo "FAIL: the card was only 3% busy"; exit 1 ;;\n'
              ' *) echo "ollama1 gpu burn x"; sleep "$O1_BURN_SECONDS"; echo "PASSED: ok"; exit 0 ;;\nesac\n', 0o755)
        write(d + "/hw/hwmon0/name", "k10temp\n")
        write(d + "/hw/hwmon0/temp1_input", "61000\n")
        write(d + "/hw/hwmon1/name", "amdgpu\n")
        write(d + "/hw/hwmon1/temp2_input", "66000\n")
        write(d + "/hw/hwmon1/temp1_input", "55000\n")
        write(d + "/drm/card0/device/gpu_busy_percent", "99\n")
        write(d + "/stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
        os.makedirs(d + "/proc")
        write(d + "/journal", "")
        write(d + "/units", "")
        os.makedirs(d + "/prefix/run/ollama1/stats")
        self.env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], O1_QB_NOROOT="1", O1_QB_SECONDS="2", O1_QB_TICK="0.2",
                        O1_RUN_DIR=self.run, O1_STATE_DIR=self.state, O1_LIB_DIR=U.LIB, O1_HWMON=d + "/hw", O1_DRM=d + "/drm",
                        O1_PROC_STAT=d + "/stat", O1_GPU_BURN=d + "/gpu-burn.sh", O1_PROC=d + "/proc", FAKE_LOG=self.log,
                        FAKE_JOURNAL=d + "/journal", FAKE_UNITS=d + "/units", OLLAMA1_PREFIX=d + "/prefix",
                        O1_GPU_BURN_LOG=d + "/gb.log")

    def go(self, timeout=60, **env):
        e = dict(self.env, **env)
        t = time.time()
        r = subprocess.run(["bash", SCRIPT], env=e, capture_output=True, text=True, timeout=timeout)
        self.took = time.time() - t
        return r

    def calls(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []

    def progress(self):
        with open(self.run + "/quickburn.json") as f:
            return json.load(f)

    def last(self):
        with open(self.state + "/quickburn-last.json") as f:
            return json.load(f)


class TestQuickBurn(unittest.TestCase):
    def test_a_clean_run_does_the_processor_then_the_card_each_for_the_set_time(self):
        b = Box(self)
        r = b.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = b.calls()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("stress-ng --cpu 0 --cpu-method matrixprod --verify -t 2s"), calls[0])
        self.assertEqual(calls[1], "gpu-burn 1 gemma4:12b secs=2")
        self.assertGreaterEqual(b.took, 3.8)
        self.assertEqual(b.last()["result"], "passed")
        self.assertEqual((b.last()["cpu"], b.last()["gpu"]), ("OK", "OK"))
        p = b.progress()
        self.assertEqual((p["phase"], p["result"], p["seconds_left"]), ("done", "passed", 0))
        self.assertFalse(os.path.exists(b.run + "/quickburn/abort"))

    def test_the_default_is_thirty_seconds_each(self):
        src = open(SCRIPT).read()
        self.assertIn("SECS=${O1_QB_SECONDS:-30}", src)
        self.assertIn("--cpu 0 --cpu-method matrixprod --verify", src)

    def test_progress_is_written_every_second_with_the_shape_the_panel_reads(self):
        b = Box(self)
        seen = []
        import threading

        def watch():
            end = time.time() + 6
            while time.time() < end:
                try:
                    seen.append(b.progress())
                except (OSError, ValueError):
                    pass
                time.sleep(0.15)
        t = threading.Thread(target=watch)
        t.start()
        b.go()
        t.join()
        phases = [p["phase"] for p in seen]
        self.assertIn("cpu", phases)
        self.assertIn("gpu", phases)
        self.assertLess(phases.index("cpu"), phases.index("gpu"))
        running = [p for p in seen if p["phase"] in ("cpu", "gpu")]
        for p in running:
            self.assertEqual(set(p), {"at", "phase", "seconds_left", "seconds_each", "cpu_c", "gpu_c", "gpu_busy", "cpu_busy", "result", "reason"})
            self.assertEqual(p["result"], "running")
            self.assertEqual((p["cpu_c"], p["gpu_c"], p["gpu_busy"]), (61, 66, 99))
            self.assertTrue(0 <= p["seconds_left"] <= 2)
        self.assertEqual(seen[-1]["phase"], "done")

    def test_a_missing_llama_bench_is_said_and_the_processor_result_stands(self):
        b = Box(self)
        r = b.go(FAKE_GPU="missing")
        self.assertEqual(r.returncode, 0)
        last = b.last()
        self.assertEqual(last["result"], "passed")
        self.assertIn("graphics card not run: llama-bench not found", last["reason"])
        self.assertTrue(last["gpu"].startswith("not run: llama-bench not found"))
        self.assertEqual(last["cpu"], "OK")

    def test_a_card_failure_is_a_failure_with_its_reason(self):
        b = Box(self)
        r = b.go(FAKE_GPU="fail")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(b.last()["result"], "failed")
        self.assertIn("graphics card: FAIL: the card was only 3% busy", b.last()["reason"])

    def test_a_failed_processor_check_stops_before_the_card(self):
        b = Box(self)
        r = b.go(FAKE_STRESS_RC="2")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(b.last()["result"], "failed")
        self.assertIn("stress-ng exited with status 2", b.last()["reason"])
        self.assertEqual(len(b.calls()), 1)                                     # the card was not started
        self.assertEqual(b.last()["gpu"], "not run")

    def test_a_new_hardware_error_stops_the_run(self):
        b = Box(self)
        import threading

        def later():
            time.sleep(0.8)
            write(b.d + "/journal", "kernel: mce: [Hardware Error]: Machine check events logged\n")
        threading.Thread(target=later).start()
        r = b.go(FAKE_STRESS_SLEEP="5")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(b.last()["result"], "failed")
        self.assertIn("hardware-error record", b.last()["reason"])
        self.assertLess(b.took, 4.0)                                            # stopped at once, not after the 5 s

    def test_old_hardware_errors_do_not_count(self):
        b = Box(self)
        write(b.d + "/journal", "kernel: mce: [Hardware Error]: from an earlier crash\n")
        self.assertEqual(b.go().returncode, 0)

    def test_temperature_limits(self):
        for chip, file, c, who in (("k10temp", "temp1_input", 95000, "the processor reached 95 C"),
                                   ("amdgpu", "temp2_input", 105000, "the graphics card reached 105 C")):
            b = Box(self)
            write(b.d + "/hw/hwmon%d/%s" % (0 if chip == "k10temp" else 1, file), "%d\n" % c)
            r = b.go(FAKE_STRESS_SLEEP="5")
            self.assertEqual(r.returncode, 1, chip)
            self.assertEqual(b.last()["result"], "aborted")
            self.assertEqual(b.last()["reason"], who)
            self.assertLess(b.took, 3.0)
        b = Box(self)
        write(b.d + "/hw/hwmon0/temp1_input", "94000\n")
        write(b.d + "/hw/hwmon1/temp2_input", "104000\n")
        self.assertEqual(b.go().returncode, 0)                                  # just under the limits

    def test_the_abort_file_stops_it_at_once(self):
        b = Box(self)
        import threading

        def esc():
            time.sleep(0.7)
            write(b.run + "/quickburn/abort", "")
        threading.Thread(target=esc).start()
        r = b.go(FAKE_STRESS_SLEEP="9")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(b.last()["result"], "aborted")
        self.assertEqual(b.last()["reason"], "stopped from the keyboard")
        self.assertLess(b.took, 3.0)
        self.assertFalse(os.path.exists(b.run + "/quickburn/abort"))            # cleared for the next run
        self.assertEqual(len(b.calls()), 1)

    def test_an_old_abort_file_does_not_stop_a_new_run(self):
        b = Box(self)
        write(b.run + "/quickburn/abort", "")
        self.assertEqual(b.go().returncode, 0)

    def test_it_refuses_while_a_request_is_running(self):
        b = Box(self)
        write(b.d + "/prefix/run/ollama1/stats/activity.json", json.dumps({"last": time.time(), "inflight": 1, "at": time.time()}))
        r = b.go()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(b.last()["result"], "refused")
        self.assertEqual(b.last()["reason"], "a request is running")
        self.assertEqual(b.calls(), [])
        self.assertEqual(b.progress()["result"], "refused")

    def test_it_refuses_while_a_download_or_another_test_runs(self):
        b = Box(self)
        write(b.d + "/units", "ollama1-pull@abcdef012345.service loaded active running pull\n")
        r = b.go()
        self.assertEqual((r.returncode, b.last()["result"], b.last()["reason"]), (1, "refused", "a model is being downloaded"))
        b = Box(self)
        os.makedirs(b.d + "/proc/4242")
        with open(b.d + "/proc/4242/cmdline", "wb") as f:
            f.write(b"bash\0/home/pat/ollama1/tools/stability-test.sh\0--minutes\x0010\0")
        r = b.go()
        self.assertEqual((r.returncode, b.last()["result"], b.last()["reason"]), (1, "refused", "stability-test.sh is running"))
        self.assertEqual(b.calls(), [])

    def test_its_own_process_does_not_count_as_another_burn(self):
        b = Box(self)
        os.makedirs(b.d + "/proc/4243")
        with open(b.d + "/proc/4243/cmdline", "wb") as f:
            f.write(b"bash\0/usr/local/lib/ollama1/tools/quick-burn.sh\0")
        self.assertEqual(b.go().returncode, 0)

    def test_it_says_when_stress_ng_is_missing(self):
        b = Box(self)
        os.unlink(b.bin + "/stress-ng")
        r = b.go(PATH=b.bin + ":/usr/bin:/bin")
        if shutil.which("stress-ng", path="/usr/bin:/bin"):
            self.skipTest("a real stress-ng is installed here")
        self.assertEqual((r.returncode, b.last()["result"]), (1, "refused"))
        self.assertIn("stress-ng is not installed", b.last()["reason"])

    def test_the_json_is_one_line_and_safe(self):
        b = Box(self)
        write(b.d + "/units", 'ollama1-pull@abcdef012345.service loaded active running "x"\n')
        b.go()
        raw = open(b.state + "/quickburn-last.json").read()
        self.assertEqual(raw.count("\n"), 1)
        json.loads(raw)
        self.assertEqual(set(json.loads(raw)), {"at", "result", "reason", "cpu", "gpu"})


class TestQuickBurnUnit(unittest.TestCase):
    def unit(self):
        with open(os.path.join(U.KIT, "systemd", "ollama1-quickburn.service")) as f:
            return f.read()

    def test_the_unit_is_a_root_oneshot_with_a_narrow_sandbox(self):
        u = self.unit()
        self.assertIn("Type=oneshot", u)
        self.assertIn("ExecStart=/usr/local/lib/ollama1/tools/quick-burn.sh", u)
        for line in ("ProtectSystem=strict", "NoNewPrivileges=yes", "IPAddressDeny=any", "ProtectHome=read-only",
                     "ReadWritePaths=/run/ollama1 /var/lib/ollama1", "PrivateTmp=yes", "RestrictAddressFamilies=AF_UNIX",
                     "ProtectKernelModules=yes", "ProtectControlGroups=yes", "LockPersonality=yes", "RestrictSUIDSGID=yes"):
            self.assertIn(line, u)
        self.assertNotIn("User=", u)                                                # root: stress-ng, hwmon and the journal
        self.assertNotIn("PrivateDevices", u)                                       # the card's render node and /dev/kfd must be reachable
        self.assertRegex(u, r"TimeoutStartSec=\d+")
        self.assertNotRegex(u, r"(?m)^\[Install\]")                                            # never started at boot

    def rules(self):
        with open(os.path.join(U.CONFIG, "50-ollama1.rules")) as f:
            return f.read()

    def test_polkit_lets_only_o1dash_start_exactly_this_unit(self):
        r = self.rules()
        i = r.index('subject.user !== "o1dash"')
        block = r[i - 200:]
        block = block[block.index("polkit.addRule"):]
        block = block[:block.index("});") + 3]
        self.assertEqual(re.findall(r'"([a-z0-9@.-]+\.service)"', block), ["ollama1-quickburn.service"])
        self.assertIn('action.lookup("verb") === "start"', block)
        self.assertIn("org.freedesktop.systemd1.manage-units", block)
        self.assertTrue(block.rstrip().endswith("});"))
        self.assertIn("polkit.Result.NO", block)                                    # everything else for o1dash is refused
        self.assertEqual(r.count('"o1dash"'), 1)

    def test_setup_installs_the_script_unit_and_rule(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('install -m 0755 "$KIT/tools/quick-burn.sh" "$KIT/tools/gpu-burn.sh" "$LIBDIR/tools/"', setup)
        self.assertIn("systemd/ollama1-quickburn.service", setup)
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        dash = open(os.path.join(U.KIT, "systemd", "ollama1-dash.service")).read()
        self.assertIn("ReadWritePaths=-/run/ollama1/quickburn", dash)               # the only place the screen's user can write
        self.assertEqual(dash.count("ReadWritePaths="), 1)

    def test_the_abort_needs_no_privilege_beyond_one_empty_file(self):
        t = open(os.path.join(U.CONFIG, "ollama1.tmpfiles")).read()
        self.assertRegex(t, r"(?m)^d /run/ollama1/quickburn\s+0730 root\s+o1dash\s+-$")


if __name__ == "__main__":
    unittest.main()
