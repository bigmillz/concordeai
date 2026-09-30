"""tools/: the ram-model measurement (its pure parts; the rest needs root
and a real Ollama) and the encrypted-swap switch (static checks)."""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import o1test_util as U

GIB = 1 << 30


def load_tool():
    path = os.path.join(U.TOOLS, "ram_model_test.py")
    loader = importlib.machinery.SourceFileLoader("ram_model_test", path)
    spec = importlib.util.spec_from_loader("ram_model_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


T = load_tool()


class TestRamModelTool(unittest.TestCase):
    def test_cap(self):
        mx, hi = T.memory_cap(64 * GIB)
        self.assertEqual((mx, hi), (56 * GIB, 54 * GIB))

    def test_dropins(self):
        nr = T.dropin_text(T.CONFIGS["norepack"], 29, 52 * GIB, 50 * GIB, 32 * GIB)
        self.assertIn("MemoryMax=%d" % (52 * GIB), nr)
        self.assertIn("MemorySwapMax=0", nr)                  # no swap outside the swap config
        self.assertIn("Environment=LLAMA_ARG_REPACK=false", nr)
        moe = T.dropin_text(T.CONFIGS["norepack-moe"], 29, 52 * GIB, 50 * GIB, 32 * GIB)
        for line in ("LLAMA_ARG_N_CPU_MOE=29", "LLAMA_ARG_N_GPU_LAYERS=all", "LLAMA_ARG_FIT=off"):
            self.assertIn("Environment=" + line, moe)
        sw = T.dropin_text(T.CONFIGS["swap"], 29, 52 * GIB, 50 * GIB, 32 * GIB)
        self.assertIn("MemorySwapMax=%d" % (32 * GIB), sw)
        self.assertNotIn("LLAMA_ARG_REPACK", sw)
        self.assertEqual(T.CONFIGS["swap"]["options"], {"use_mmap": False})
        self.assertEqual(T.DEFAULT_CONFIGS, ["norepack", "norepack-moe"])

    def test_buffer_lines(self):
        journal = ("load_tensors:        ROCm0 model buffer size =  3120.25 MiB\n"
                   "load_tensors:   CPU_REPACK model buffer size = 58092.00 MiB\n"
                   "load_tensors:          CPU model buffer size =  4772.10 MiB\n"
                   "load_tensors:   CPU_Mapped model buffer size = 61000.00 MiB\n")
        self.assertEqual(T.parse_buffers(journal), {"ROCm0": 3120.25, "CPU_REPACK": 58092.0, "CPU": 4772.1,
                                                   "CPU_Mapped": 61000.0})
        self.assertEqual(T.parse_buffers(""), {})

    def test_verdicts_follow_the_gateway(self):
        from stub_ollama import DEFAULT_MODELS
        m = DEFAULT_MODELS["moe:120b"]      # 56 GiB, gpt-oss-like
        mi = {"MemTotal": 62 * GIB, "MemAvailable": 58 * GIB}
        v = T.gateway_verdicts(m["size"], m["info"], 4096, mi, 16 * GIB, 54 * GIB)
        self.assertEqual(v["repack_on"], "refused")        # RAM alone: 58 - 8 = 50 GiB < ~61
        self.assertEqual(v["repack_off"], "fits")          # plus 15.25 GiB of VRAM
        self.assertEqual(v["host_budget"], 50 * GIB)

    def test_table(self):
        rows = T.table([
            {"config": "norepack", "loaded": True, "size": 60 * GIB, "vram": 14 * GIB, "peak_ollama_bytes": 50 * GIB,
             "peak_swap_bytes": 0, "min_free_bytes": 9 * GIB, "load_s": 80.2, "ttft_s": 1.5, "eval_tps": 9.3},
            {"config": "gateway", "loaded": False, "oom_kill": 1, "peak_ollama_bytes": 54 * GIB,
             "peak_swap_bytes": 0, "min_free_bytes": 8 * GIB},
            {"config": "swap", "skipped": "encrypted swap is off"},
        ])
        self.assertIn("14.0 / 46.0 GiB", rows)
        self.assertRegex(rows, r"gateway\s+OOM")
        self.assertIn("skipped: encrypted swap is off", rows)

    def test_wrapper(self):
        path = os.path.join(U.TOOLS, "ram-model-test.sh")
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_wrapper_passes_no_empty_argument_to_tmux(self):
        """Run with no arguments, the wrapper once handed the Python script an
        empty one ('unrecognized arguments:') because printf '%q ' with no
        arguments prints ''. Runs the real quoted_args() from the script."""
        src = open(os.path.join(U.TOOLS, "ram-model-test.sh")).read()
        m = re.search(r"^quoted_args\(\) \{\n.*?^\}\n", src, re.S | re.M)
        self.assertTrue(m, "quoted_args() not found")
        script = m.group(0) + (
            'out=$(quoted_args); [ -z "$out" ] || { echo "none: [$out]"; exit 1; }\n'
            'eval "set -- $(quoted_args --configs "a b" --ctx 4096)"\n'
            '[ "$#" -eq 4 ] && [ "$2" = "a b" ] && [ "$4" = 4096 ] || { echo "args: $#"; exit 1; }\n'
            'eval "set -- $(quoted_args)"; [ "$#" -eq 0 ] || { echo "empty: $#"; exit 1; }\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('$(quoted_args "$@")', src)
        self.assertNotIn("$(printf '%q ' \"$@\")", src)

    def test_safety_lines(self):
        src = open(os.path.join(U.TOOLS, "ram_model_test.py")).read()
        self.assertIn('raise RuntimeError("the memory cap didn\'t take; not loading")', src)
        self.assertIn('"systemctl", "kill", "--signal=KILL", UNIT', src)     # the watchdog
        self.assertIn("avail < 1.5 * GIB", src)
        self.assertIn("restore()", src.split("finally:")[-1])                # always back to normal
        self.assertIn('DROPIN_DIR = "/run/systemd/system/ollama.service.d"', src)   # runtime only
        self.assertLess(src.index('tty.readline().strip() != "yes"'), src.index("results.append(measure("))


class TestStabilityTest(unittest.TestCase):
    """tools/stability-test.sh against fake stress-ng, curl and journalctl
    (no load, no root): a pass, a failed check, a hardware error logged
    during a phase, an interrupted run, and the argument handling."""
    SCRIPT = os.path.join(U.TOOLS, "stability-test.sh")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1stab-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.state = os.path.join(self.dir, "state")
        os.mkdir(self.state)
        self.jcount = os.path.join(self.dir, "jcount")
        self.fake("stress-ng", 'sleep 1; exit "${FAKE_STRESS_RC:-0}"')
        # timeout(1) isn't on every machine the tests run on
        self.fake("timeout", 'secs=$1; shift; "$@" & pid=$!; (sleep "$secs"; kill $pid 2>/dev/null) & wait $pid; exit 124')
        self.fake("curl", 'case "$*" in *api/tags*) echo \'{"models":[{"name":"gemma4:12b"}]}\'; exit 0;; '
                  '*api/generate*) [ -z "${FAKE_NO_ANSWER:-}" ] || exit 22;; esac; exit 0')
        # journalctl: a hardware-error line appears from the Nth call on
        self.fake("journalctl", 'n=$(cat "%s" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "%s"; '
                  'if [ -n "${FAKE_MCE_AFTER:-}" ] && [ "$n" -ge "$FAKE_MCE_AFTER" ]; then '
                  'echo "kernel: mce: [Hardware Error]: CPU 24: Machine Check: 0 Bank 5: bea0"; fi; exit 0'
                  % (self.jcount, self.jcount))

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_script(self, *args, env=None):
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], O1_STATE_DIR=self.state,
                 O1_STABILITY_LOG=os.path.join(self.dir, "log"), O1_STABILITY_NOROOT="1",
                 O1_NO_TMUX="1", O1_STABILITY_TICK="1", O1_HWMON=os.path.join(self.dir, "hwmon"))
        e.update(env or {})
        r = subprocess.run(["bash", self.SCRIPT] + list(args), capture_output=True, text=True, timeout=120, env=e,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def record(self):
        with open(os.path.join(self.state, "stability.state")) as f:
            return f.read()

    def test_wrapper(self):
        self.assertEqual(subprocess.run(["bash", "-n", self.SCRIPT]).returncode, 0)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, self.SCRIPT], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_pass(self):
        code, out = self.run_script("--phases", "cpu,memory", "--seconds", "2")
        self.assertEqual(code, 0, out)
        self.assertIn("PASSED", out)
        rec = self.record()
        self.assertIn("status=done", rec)
        self.assertIn("result cpu OK", rec)
        self.assertIn("result memory OK", rec)

    def test_a_failed_check_fails_and_stops(self):
        code, out = self.run_script("--phases", "cpu,memory", "--seconds", "2", env={"FAKE_STRESS_RC": "2"})
        self.assertEqual(code, 1, out)
        rec = self.record()
        self.assertIn("status=failed", rec)
        self.assertIn("result cpu FAIL", rec)
        self.assertNotIn("result memory", rec)        # the first failure is the answer

    def test_a_new_hardware_error_fails_the_phase(self):
        # the count is read before and after each phase: calls 1 (start) and 2 (before) see none
        code, out = self.run_script("--phases", "cpu", "--seconds", "2", env={"FAKE_MCE_AFTER": "3"})
        self.assertEqual(code, 1, out)
        self.assertIn("hardware-error record", self.record())
        self.assertIn("result cpu FAIL", self.record())

    def test_gpu_phase_needs_answers_and_the_model(self):
        code, out = self.run_script("--phases", "gpu", "--seconds", "2", "--model", "nothere:1b")
        self.assertEqual(code, 1, out)
        self.assertIn("isn't installed", out)
        code, out = self.run_script("--phases", "gpu", "--seconds", "3", "--model", "gemma4:12b")
        self.assertEqual(code, 0, out)         # the fake curl answers, so the loop records answers
        self.assertIn("result gpu OK", self.record())

    def test_gpu_load_alternates_reading_and_writing(self):
        """A short question alone leaves the card part-idle (decoding leans on
        memory speed); reading a long prompt is the compute-heavy half."""
        with open(self.SCRIPT) as f:
            src = f.read()
        self.assertIn("head -n 320", src)                    # the long prompt, about 5800 tokens
        self.assertIn("n % 2", src)                          # alternated, not one or the other
        self.assertRegex(src, r'num_ctx\\":8192,\\"num_predict\\":8\}')    # read a lot, say little
        self.assertRegex(src, r'num_ctx\\":8192,\\"num_predict\\":500\}')  # say a lot

    def test_a_gpu_phase_with_no_answers_fails(self):
        code, out = self.run_script("--phases", "gpu", "--seconds", "3", env={"FAKE_NO_ANSWER": "1"})
        self.assertEqual(code, 1, out)
        self.assertIn("no answer", out)
        self.assertIn("result gpu FAIL", self.record())

    def test_too_hot_aborts_the_phase(self):
        chip = os.path.join(self.dir, "hwmon", "hwmon0")
        os.makedirs(chip)
        with open(os.path.join(chip, "name"), "w") as f:
            f.write("k10temp\n")
        with open(os.path.join(chip, "temp1_input"), "w") as f:
            f.write("96000\n")
        self.fake("stress-ng", "sleep 30")
        code, out = self.run_script("--phases", "cpu,memory", "--seconds", "20")
        self.assertEqual(code, 1, out)
        self.assertIn("ABORTED: CPU reached 96 C", out)
        rec = self.record()
        self.assertIn("result cpu ABORTED", rec)
        self.assertIn("max_cpu_c=96", rec)
        self.assertNotIn("result memory", rec)

    def test_a_hot_graphics_card_aborts_the_phase(self):
        chip = os.path.join(self.dir, "hwmon", "hwmon1")
        os.makedirs(chip)
        with open(os.path.join(chip, "name"), "w") as f:
            f.write("amdgpu\n")
        with open(os.path.join(chip, "temp1_input"), "w") as f:
            f.write("70000\n")
        with open(os.path.join(chip, "temp2_input"), "w") as f:      # the junction, what the limit is on
            f.write("106000\n")
        self.fake("stress-ng", "sleep 30")
        code, out = self.run_script("--phases", "memory", "--seconds", "20")
        self.assertEqual(code, 1, out)
        self.assertIn("ABORTED: graphics junction reached 106 C", out)

    def test_status_reports_an_interrupted_run(self):
        with open(os.path.join(self.state, "stability.state"), "w") as f:
            f.write("status=running\nphase=all\nstarted=2026-09-30T16:00:00-04:00\nresult cpu OK max_cpu_c=70 max_gpu_c=40\n")
        code, out = self.run_script("status", env={"FAKE_MCE_AFTER": "1"})
        self.assertEqual(code, 0, out)
        self.assertIn("did not finish", out)
        self.assertIn("phase all", out)
        self.assertIn("Hardware-error records in the kernel log since this boot: 1", out)
        self.assertIn("Machine Check", out)

    def test_status_with_nothing_recorded(self):
        code, out = self.run_script("status")
        self.assertEqual(code, 0, out)
        self.assertIn("No run recorded", out)
        self.assertIn("since this boot: 0", out)

    def test_bad_arguments(self):
        for args in (["--phases", "nope"], ["--minutes", "x"], ["--minutes", "0"], ["--model", "a b"], ["--bogus"]):
            code, out = self.run_script(*args)
            self.assertEqual(code, 2, (args, out))
        self.assertEqual(self.run_script("--help")[0], 0)

    def test_root_is_required(self):
        if os.geteuid() == 0:
            self.skipTest("as root this would really run")
        r = subprocess.run(["bash", self.SCRIPT, "--phases", "cpu"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, O1_STABILITY_NOROOT=""))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Run it with sudo", r.stdout + r.stderr)

    def test_no_empty_argument_for_tmux(self):
        with open(self.SCRIPT) as f:
            src = f.read()
        m = re.search(r"^quoted_args\(\) \{\n.*?^\}\n", src, re.S | re.M)
        self.assertTrue(m)
        script = m.group(0) + (
            'out=$(quoted_args); [ -z "$out" ] || { echo "none: [$out]"; exit 1; }\n'
            'eval "set -- $(quoted_args --minutes 15 --model "a b")"\n'
            '[ "$#" -eq 4 ] && [ "$4" = "a b" ] || { echo "args: $#"; exit 1; }\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestRamTestCleansUp(unittest.TestCase):
    def test_hangup_and_term_leave_through_the_cleanup(self):
        src = open(os.path.join(U.TOOLS, "ram_model_test.py")).read()
        self.assertIn("for sig in (signal.SIGHUP, signal.SIGTERM):\n        signal.signal(sig, exit_on_signal)", src)
        with self.assertRaises(SystemExit) as e:
            T.exit_on_signal(1, None)
        self.assertEqual(e.exception.code, 129)
        # the handlers are set before anything is changed
        main = src[src.index("def main():"):]
        self.assertLess(main.index("signal.signal(sig, exit_on_signal)"), main.index("results = []"))

    def test_cleanup_before_any_output(self):
        src = open(os.path.join(U.TOOLS, "ram_model_test.py")).read()
        fin = src[src.index("    finally:\n        for sig in (signal.SIGHUP, signal.SIGTERM)"):]
        # signals ignored first, so a second HUP/TERM can't cut the restore short
        self.assertLess(fin.index("signal.SIG_IGN"), fin.index("restore()"))
        self.assertLess(fin.index("restore()"), fin.index("say("))
        import io
        import sys as _sys

        class Gone(io.StringIO):
            def write(self, *a):
                raise BrokenPipeError(32, "Broken pipe")
        real, _sys.stdout = _sys.stdout, Gone()
        try:
            T.say("the terminal is gone")                  # must not raise
        finally:
            _sys.stdout = real

    def test_runs_in_tmux(self):
        sh = open(os.path.join(U.TOOLS, "ram-model-test.sh")).read()
        self.assertIn("exec tmux new-session -A -s ollama1-ramtest", sh)
        self.assertLess(sh.index("exec tmux new-session"), sh.index('python3 -u "$HERE/ram_model_test.py"'))

    def test_setup_removes_a_left_drop_in_before_restarting_ollama(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        rm = setup.index("rm -f /run/systemd/system/ollama.service.d/50-ollama1-ramtest.conf")
        self.assertLess(rm, setup.index("run systemctl restart ollama.service"))
        self.assertIn('systemctl kill -s HUP systemd-logind || note', setup)


class TestRamTestSwapConfig(unittest.TestCase):
    def test_default_is_no_repack_only(self):
        self.assertEqual(T.DEFAULT_CONFIGS, ["norepack", "norepack-moe"])

    def test_swap_refused_clearly(self):
        self.assertIsNone(T.swap_refusal(["norepack"], 0, 0))
        self.assertIsNone(T.swap_refusal(["swap"], 32 << 30, None))            # encrypted swap is on
        self.assertIn("no free space", T.swap_refusal(["swap"], 0, 0))
        self.assertIn("vgs ubuntu-vg", T.swap_refusal(["swap"], 0, 0))
        self.assertIn("is off", T.swap_refusal(["swap"], 0, 64 << 30))
        src = open(os.path.join(U.TOOLS, "ram_model_test.py")).read()
        main = src[src.index("def main():"):]
        self.assertLess(main.index("swap_refusal("), main.index('Type yes to start'))


class TestEncryptedSwap(unittest.TestCase):
    def src(self):
        return open(os.path.join(U.TOOLS, "encrypted-swap.sh")).read()

    def test_lint(self):
        path = os.path.join(U.TOOLS, "encrypted-swap.sh")
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_random_key_every_boot_on_a_volume(self):
        s = self.src()
        self.assertIn('"$NAME" "$LVDEV" >>"$CRYPTTAB"', s)
        self.assertRegex(s, r"/dev/urandom swap,cipher=aes-xts-plain64,size=512,nofail")
        self.assertIn("LVDEV=/dev/$VG/$LV", s)
        self.assertNotIn("fallocate", s)             # no swap file, so no loop device

    def test_ollama_may_not_swap_by_default(self):
        self.assertNotIn("MemorySwapMax", self.src().replace("MemorySwapMax=0", ""))
        unit = open(os.path.join(U.KIT, "systemd", "ollama.service")).read()
        self.assertIn("\nMemorySwapMax=0\n", unit)


class TestEncryptedSwapRuns(unittest.TestCase):
    """The script itself, against fake LVM/systemd/swap tools."""

    def setUp(self):
        self.r = tempfile.mkdtemp(prefix="o1swap-")
        self.addCleanup(shutil.rmtree, self.r, True)
        os.makedirs(os.path.join(self.r, "etc"))
        self.bin = os.path.join(self.r, "bin")
        os.makedirs(self.bin)
        for t in ("vgs", "lvs", "lvcreate", "lvremove", "systemctl", "swapon", "swapoff", "mkswap"):
            os.symlink(os.path.join(U.HERE, "fakeswap.py"), os.path.join(self.bin, t))
        self.fstab_before = "UUID=x / ext4 defaults 0 1\n/swap.img none swap sw 0 0\n"
        with open(os.path.join(self.r, "etc/fstab"), "w") as f:
            f.write(self.fstab_before)
        with open(os.path.join(self.r, "proc-swaps"), "w") as f:
            f.write("/swap.img file 8388604 0 -2\n")
        open(os.path.join(self.r, "swap.img"), "w").close()
        self.fake({"vg_free": 100 << 30})

    def fake(self, s=None, **kw):
        path = os.path.join(self.r, "fake.json")
        if s is None:
            s = json.load(open(path))
        s.update(kw)
        with open(path, "w") as f:
            json.dump(s, f)
        return s

    def run_it(self, *args):
        env = dict(os.environ, O1_SWAP_TESTROOT=self.r, PATH=self.bin + os.pathsep + os.environ["PATH"])
        return subprocess.run(["bash", os.path.join(U.TOOLS, "encrypted-swap.sh")] + list(args),
                              capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)

    def read(self, rel):
        p = os.path.join(self.r, rel)
        return open(p).read() if os.path.exists(p) else ""

    def test_on_then_off(self):
        r = self.run_it("on", "32G")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.fake()["log"]
        self.assertIn("lvcreate --yes --wipesignatures y -L 32G -n ollama1swap ubuntu-vg", log)
        self.assertIn("ollama1swap /dev/ubuntu-vg/ollama1swap /dev/urandom swap,cipher=aes-xts-plain64,size=512,nofail",
                      self.read("etc/crypttab"))
        fstab = self.read("etc/fstab")
        self.assertIn("/dev/mapper/ollama1swap none swap sw,nofail,pri=10 0 0", fstab)
        self.assertIn("# /swap.img none swap sw 0 0", fstab)
        self.assertEqual(self.read("proc-swaps"), "/dev/mapper/ollama1swap partition 1 0 -2\n")
        self.assertIn("still holds whatever was swapped", r.stdout)
        # again: nothing new
        r = self.run_it("on", "32G")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([x for x in self.read("etc/crypttab").splitlines() if x.startswith("ollama1swap ")].__len__(), 1)
        self.assertEqual(sum(1 for x in self.fake()["log"] if x.startswith("lvcreate")), 1)
        r = self.run_it("off")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read("etc/fstab"), self.fstab_before)
        self.assertNotIn("ollama1swap", self.read("etc/crypttab"))
        self.assertFalse(self.fake()["lv"])
        self.assertEqual(self.read("proc-swaps"), "/swap.img partition 1 0 -2\n")
        self.assertIn("Encrypted swap removed.", r.stdout)

    def test_no_room_changes_nothing(self):
        self.fake(vg_free=0)
        r = self.run_it("on", "32G")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ubuntu-vg has 0 GiB free", r.stdout)
        self.assertEqual(self.read("etc/fstab"), self.fstab_before)
        self.assertEqual(self.read("etc/crypttab"), "")
        self.assertFalse([x for x in self.fake()["log"] if x.startswith("lvcreate")])

    def test_a_half_done_run_is_finished(self):
        self.fake(fail=["swapon"])
        r = self.run_it("on", "32G")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Run it again to finish", r.stdout)
        self.fake(fail=[])
        r = self.run_it("on", "32G")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(sum(1 for x in self.fake()["log"] if x.startswith("lvcreate")), 1)
        self.assertIn("/dev/mapper/ollama1swap", self.read("proc-swaps"))
        self.assertEqual(self.read("etc/fstab").count("/dev/mapper/ollama1swap"), 1)

    def test_off_stops_if_swapoff_fails(self):
        self.assertEqual(self.run_it("on", "32G").returncode, 0)
        crypttab, fstab = self.read("etc/crypttab"), self.read("etc/fstab")
        self.fake(fail=["swapoff"])
        r = self.run_it("off")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Nothing was changed", r.stdout)
        self.assertNotIn("removed", r.stdout)
        self.assertEqual((self.read("etc/crypttab"), self.read("etc/fstab")), (crypttab, fstab))
        self.assertTrue(self.fake()["lv"])

    def test_off_leaves_a_volume_it_didnt_make(self):
        self.fake(lv=True)                                  # already there before "on"
        self.assertEqual(self.run_it("on", "8G").returncode, 0)
        self.assertFalse([x for x in self.fake()["log"] if x.startswith("lvcreate")])
        r = self.run_it("off")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(self.fake()["lv"])
        self.assertFalse([x for x in self.fake()["log"] if x.startswith("lvremove")])
        self.assertIn("was there before", r.stdout)

    def test_off_restores_only_what_on_changed(self):
        # the plain swap was already commented out by hand: off leaves it that way
        hand = "UUID=x / ext4 defaults 0 1\n#/swap.img none swap sw 0 0\n"
        with open(os.path.join(self.r, "etc/fstab"), "w") as f:
            f.write(hand)
        with open(os.path.join(self.r, "proc-swaps"), "w") as f:
            f.write("")
        self.assertEqual(self.run_it("on", "8G").returncode, 0)
        self.assertEqual(self.run_it("off").returncode, 0)
        self.assertEqual(self.read("etc/fstab"), hand)
        self.assertEqual(self.read("proc-swaps"), "")

    def test_setup_flags(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('exec bash "$KIT/tools/encrypted-swap.sh" "$SWAP_ACTION" "$SWAP_SIZE"', setup)
        self.assertIn("--encrypted-swap) SWAP_ACTION=on", setup)
        self.assertIn("--remove-encrypted-swap) SWAP_ACTION=off", setup)


if __name__ == "__main__":
    unittest.main()
