"""tools/: the ram-model measurement (its pure parts; the rest needs root
and a real Ollama) and the encrypted-swap switch (static checks)."""
import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import unittest

import o1test_util as U

GIB = 1 << 30


def load_tool():
    path = os.path.join(U.KIT, "tools", "ram_model_test.py")
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
        self.assertEqual(T.DEFAULT_CONFIGS, ["norepack", "norepack-moe", "swap"])

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
        path = os.path.join(U.KIT, "tools", "ram-model-test.sh")
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_safety_lines(self):
        src = open(os.path.join(U.KIT, "tools", "ram_model_test.py")).read()
        self.assertIn('raise RuntimeError("the memory cap didn\'t take; not loading")', src)
        self.assertIn('"systemctl", "kill", "--signal=KILL", UNIT', src)     # the watchdog
        self.assertIn("avail < 1.5 * GIB", src)
        self.assertIn("restore()", src.split("finally:")[-1])                # always back to normal
        self.assertIn('DROPIN_DIR = "/run/systemd/system/ollama.service.d"', src)   # runtime only
        self.assertLess(src.index('tty.readline().strip() != "yes"'), src.index("results.append(measure("))


class TestEncryptedSwap(unittest.TestCase):
    def src(self):
        return open(os.path.join(U.KIT, "tools", "encrypted-swap.sh")).read()

    def test_lint(self):
        path = os.path.join(U.KIT, "tools", "encrypted-swap.sh")
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        sc = shutil.which("shellcheck")
        if sc:
            r = subprocess.run([sc, path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_random_key_every_boot(self):
        s = self.src()
        self.assertIn('"$NAME" "$FILE" >>"$CRYPTTAB"', s)
        self.assertRegex(s, r"/dev/urandom swap,cipher=aes-xts-plain64,size=256")

    def test_ollama_may_not_swap_by_default(self):
        self.assertNotIn("MemorySwapMax", self.src().replace("MemorySwapMax=0", ""))
        unit = open(os.path.join(U.KIT, "systemd", "ollama.service")).read()
        self.assertIn("\nMemorySwapMax=0\n", unit)

    def test_undo(self):
        s = self.src()
        off = s[s.index("off() {"):s.index("case \"${1:-status}\"")]
        for step in ("swapoff \"$MAPPER\"", "systemd-cryptsetup@$NAME.service", '"$CRYPTTAB.new"', '"$FSTAB.new"',
                     'rm -f "$FILE"', 'swapon "$PLAIN"'):
            self.assertIn(step, off)

    def test_setup_flags(self):
        setup = open(os.path.join(U.KIT, "setup.sh")).read()
        self.assertIn('exec bash "$KIT/tools/encrypted-swap.sh" "$SWAP_ACTION" "$SWAP_SIZE"', setup)
        self.assertIn("--encrypted-swap) SWAP_ACTION=on", setup)
        self.assertIn("--remove-encrypted-swap) SWAP_ACTION=off", setup)


if __name__ == "__main__":
    unittest.main()
