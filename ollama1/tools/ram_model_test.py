#!/usr/bin/python3
"""Measure how a big 'ram' model (gpt-oss:120b) loads on this desktop, in
several configurations, each under a hard memory cap on Ollama so the
worst case is an Ollama restart, never a frozen desktop.

Run through ram-model-test.sh (it checks root and passes the arguments):

    sudo bash ram-model-test.sh [--model gpt-oss:120b] [--ctx 4096] [--ncmoe 29]
                                [--configs norepack,norepack-moe]

What Ollama 0.34/0.35 offers (from its llm/llama_server.go and llama.cpp's
common/arg.cpp, which Ollama builds llama-server from):
  - Ollama passes its own environment to llama-server, so llama.cpp's
    LLAMA_ARG_* variables set on ollama.service reach it:
      LLAMA_ARG_REPACK=false     --no-repack: no CPU_REPACK copy of the weights
      LLAMA_ARG_N_GPU_LAYERS=all -ngl all (Ollama passes -ngl only if num_gpu is set)
      LLAMA_ARG_N_CPU_MOE=N      the MoE experts of the first N layers stay on the CPU
      LLAMA_ARG_FIT=off          no automatic placement (it ignores repack buffers)
  - request options: use_mmap=false becomes --load-mode none; num_gpu
    becomes -ngl. There is no request option or OLLAMA_* variable for
    repacking.

Configurations (all at the same num_ctx, each after an Ollama restart):
  norepack      repack off, mmap on: the CPU weights stay file-backed page
                cache (evictable under the cap), llama.cpp places the layers
  norepack-moe  repack off, mmap on, every layer on the GPU except the
                experts of the first N layers (--ncmoe), placement fixed
  swap          Ollama's defaults (repack on, mmap off), but Ollama may use
                the encrypted swap (setup.sh --encrypted-swap). Only with
                --configs swap; refused unless the encrypted swap is on
                (which needs free space in ubuntu-vg). Not in the default list
  gateway       what the gateway sends a 'ram' model today (repack on,
                mmap off), expected to hit the cap; not in the default list

For each: loaded or not (OOM / watchdog), the GPU/CPU split, the buffers
llama.cpp reported, peak Ollama memory and swap, the lowest free memory on
the machine, load time, time to first token and generation tokens/s.
Results: the terminal, /var/log/ollama1-ram-test.log (via the wrapper) and
/var/log/ollama1-ram-test.json. Only a fixed counting prompt is ever sent.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
for lib in ("/usr/local/lib/ollama1/lib", os.path.join(HERE, "..", "lib")):
    if os.path.exists(os.path.join(lib, "o1ollama.py")):
        sys.path.insert(0, lib)
        break

import o1ollama  # noqa: E402
import o1stats  # noqa: E402

API = os.environ.get("OLLAMA1_OLLAMA_URL", "http://127.0.0.1:11434")
UNIT = "ollama.service"
DROPIN_DIR = "/run/systemd/system/ollama.service.d"
DROPIN = DROPIN_DIR + "/50-ollama1-ramtest.conf"
JSON_OUT = "/var/log/ollama1-ram-test.json"
GIB = 1 << 30
PROMPT = "Write the numbers from 1 to 40, separated by commas."

CONFIGS = {
    "norepack": {"title": "repack off, mmap on, llama.cpp places the layers",
                 "env": {"LLAMA_ARG_REPACK": "false"}, "options": {}},
    "norepack-moe": {"title": "repack off, mmap on, all layers on the GPU but the experts of the first {ncmoe}",
                     "env": {"LLAMA_ARG_REPACK": "false", "LLAMA_ARG_N_GPU_LAYERS": "all",
                             "LLAMA_ARG_N_CPU_MOE": "{ncmoe}", "LLAMA_ARG_FIT": "off"},
                     "options": {}},
    "swap": {"title": "Ollama's defaults (repack on, mmap off), may use the encrypted swap",
             "env": {}, "options": {"use_mmap": False}, "swap": True},
    "gateway": {"title": "what the gateway sends today (repack on, mmap off)",
                "env": {}, "options": {"use_mmap": False}},
}
DEFAULT_CONFIGS = ["norepack", "norepack-moe"]


def say(msg=""):
    print(msg, flush=True)


# ---- pure helpers (tested) ----------------------------------------------------

def memory_cap(mem_total):
    """MemoryMax = RAM - 8 GiB, MemoryHigh 2 GiB below it."""
    mx = mem_total - 8 * GIB
    return mx, mx - 2 * GIB


def dropin_text(cfg, ncmoe, mem_max, mem_high, swap_bytes):
    lines = ["# ollama1 ram-model-test (runtime; removed when the test ends)", "[Service]",
             "MemoryMax=%d" % mem_max, "MemoryHigh=%d" % mem_high,
             "MemorySwapMax=%d" % (swap_bytes if cfg.get("swap") else 0)]
    for k, v in sorted(cfg.get("env", {}).items()):
        lines.append("Environment=%s=%s" % (k, str(v).format(ncmoe=ncmoe)))
    return "\n".join(lines) + "\n"


BUF_RE = re.compile(r"(?P<buf>[A-Za-z0-9_]+) model buffer size\s*=\s*(?P<mib>[0-9.]+) MiB")


def parse_buffers(journal_text):
    """llama.cpp's 'X model buffer size = N MiB' lines -> {X: MiB} (last load)."""
    out = {}
    for m in BUF_RE.finditer(journal_text or ""):
        out[m.group("buf")] = float(m.group("mib"))
    return out


def gateway_verdicts(size, model_info, ctx, meminfo, vram_total, mem_max, reserve_mib=768):
    """What the gateway would say, with repacking on (the model must fit
    in RAM alone) and off (VRAM counts too)."""
    need, _ = o1ollama.fit_estimate(size, model_info, ctx, ram=True)
    margin = max(8 * GIB, int(meminfo.get("MemTotal", 0) * 0.12))
    ram_part = min(meminfo.get("MemAvailable", 0) - margin, mem_max - GIB)
    ram_part = max(0, ram_part)
    vram = max(0, vram_total - (reserve_mib << 20)) if vram_total else 0
    return {"need": need, "host_budget": ram_part, "total_budget": vram + ram_part,
            "repack_on": "fits" if need <= ram_part else "refused",
            "repack_off": "fits" if need <= vram + ram_part else "refused"}


# ---- the machine -------------------------------------------------------------

def http(path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def run(*cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("%s: %s" % (" ".join(cmd), (r.stderr or r.stdout).strip()))
    return r.stdout.strip()


def cgroup_dir():
    cg = run("systemctl", "show", "-p", "ControlGroup", "--value", UNIT)
    return "/sys/fs/cgroup" + cg


def read_int(path, default=0):
    try:
        with open(path) as f:
            v = f.read().strip()
        return int(v) if v.isdigit() else default
    except OSError:
        return default


def oom_kills(cg):
    try:
        for line in open(cg + "/memory.events"):
            k, _, v = line.partition(" ")
            if k == "oom_kill":
                return int(v)
    except OSError:
        pass
    return 0


def encrypted_swap_bytes():
    try:
        for line in open("/proc/swaps").read().splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].endswith("/ollama1swap"):
                return int(parts[2]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def vg_free_bytes(vg="ubuntu-vg"):
    try:
        r = subprocess.run(["vgs", "--noheadings", "--units", "b", "--nosuffix", "-o", "vg_free", vg],
                           capture_output=True, text=True, timeout=15)
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def swap_refusal(names, swap_bytes, vg_free):
    """Why the swap configuration can't run, or None."""
    if "swap" not in names or swap_bytes:
        return None
    if vg_free == 0:
        return ("the swap configuration needs the encrypted swap, and ubuntu-vg has no free space for it "
                "(vgs ubuntu-vg: VFree 0; setup gave it all to /). Run without --configs swap.")
    return ("the swap configuration needs the encrypted swap, which is off. Turn it on first "
            "(sudo ./setup.sh --encrypted-swap 32G; it needs 32G free in ubuntu-vg, see vgs ubuntu-vg), "
            "or run without --configs swap.")


def wait_ollama(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            http("/api/version", timeout=3)
            return True
        except (OSError, ValueError):
            time.sleep(1)
    return False


def apply(cfg, ncmoe, mem_max, mem_high, swap_bytes):
    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open(DROPIN, "w") as f:
        f.write(dropin_text(cfg, ncmoe, mem_max, mem_high, swap_bytes))
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", UNIT)
    if run("systemctl", "show", "-p", "MemoryMax", "--value", UNIT) != str(mem_max):
        raise RuntimeError("the memory cap didn't take; not loading")
    if not wait_ollama():
        raise RuntimeError("Ollama didn't come back after the restart")


def restore():
    try:
        os.unlink(DROPIN)
    except FileNotFoundError:
        pass
    run("systemctl", "daemon-reload", check=False)
    run("systemctl", "restart", UNIT, check=False)
    wait_ollama()


class Monitor(threading.Thread):
    """Once a second: free memory, Ollama's memory and swap; the watchdog."""

    def __init__(self, cg):
        super().__init__(daemon=True)
        self.cg = cg
        self.stop = threading.Event()
        self.low = None
        self.peak_mem = 0
        self.peak_swap = 0
        self.watchdog = False
        self.t0 = time.time()

    def run(self):
        say("     s   free GiB   Ollama GiB   Ollama swap GiB")
        while not self.stop.is_set():
            mi = o1stats.meminfo()
            avail = mi.get("MemAvailable", 0)
            cur = read_int(self.cg + "/memory.current")
            swp = read_int(self.cg + "/memory.swap.current")
            self.low = avail if self.low is None else min(self.low, avail)
            self.peak_mem = max(self.peak_mem, cur)
            self.peak_swap = max(self.peak_swap, swp)
            say("  %4d   %8.1f   %10.1f   %15.1f" % (time.time() - self.t0, avail / GIB, cur / GIB, swp / GIB))
            if avail < 1.5 * GIB and not self.watchdog:
                say("  WATCHDOG: under 1.5 GiB free on the machine; killing Ollama")
                self.watchdog = True
                run("systemctl", "kill", "--signal=KILL", UNIT, check=False)
            self.stop.wait(1.0)


def measure(name, cfg, model, ctx, ncmoe, mem_max, mem_high, swap_bytes):
    res = {"config": name, "title": cfg["title"].format(ncmoe=ncmoe), "loaded": False}
    if cfg.get("swap") and not swap_bytes:
        res["skipped"] = "encrypted swap is off (sudo ./setup.sh --encrypted-swap 32G)"
        return res
    say("\n=== %s: %s" % (name, res["title"]))
    apply(cfg, ncmoe, mem_max, mem_high, swap_bytes)
    cg = cgroup_dir()
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    oom0 = oom_kills(cg)
    mon = Monitor(cg)
    mon.start()
    opts = dict(cfg.get("options", {}), num_ctx=ctx)
    try:
        t0 = time.time()
        try:
            http("/api/generate", {"model": model, "prompt": "", "stream": False, "options": opts}, timeout=1800)
            res["load_s"] = round(time.time() - t0, 1)
            ps = http("/api/ps").get("models", [])
            m = next((m for m in ps if o1ollama.same_model(m.get("name"), model)), {})
            size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            res.update(loaded=True, size=size, vram=vram, gpu_pct=int(100 * vram / size) if size else 0,
                       context=m.get("context_length"))
            res.update(timed_generate(model, opts))
        except (urllib.error.URLError, OSError, ValueError) as e:
            res["error"] = str(e)[:200]
    finally:
        mon.stop.set()
        mon.join()
    res["peak_ollama_bytes"] = max(mon.peak_mem, read_int(cg + "/memory.peak"))
    res["peak_swap_bytes"] = max(mon.peak_swap, read_int(cg + "/memory.swap.peak"))
    res["min_free_bytes"] = mon.low
    res["oom_kill"] = oom_kills(cg) - oom0
    res["watchdog"] = mon.watchdog
    journal = run("journalctl", "-u", UNIT, "--since", since, "--no-pager", "-o", "cat", check=False)
    res["buffers_mib"] = parse_buffers(journal)
    res["mmap_warning"] = "mmap enabled" in journal
    try:
        http("/api/generate", {"model": model, "keep_alive": 0}, timeout=120)
    except (OSError, ValueError):
        pass
    return res


def timed_generate(model, opts):
    """Time to first token and generation speed, streaming a fixed prompt."""
    body = {"model": model, "prompt": PROMPT, "stream": True, "options": dict(opts, num_predict=96)}
    req = urllib.request.Request(API + "/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    ttft = None
    last = {}
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            obj = json.loads(line)
            if ttft is None and obj.get("response"):
                ttft = time.time() - t0
            if obj.get("done"):
                last = obj
    ev, ed = last.get("eval_count", 0), last.get("eval_duration", 0)
    pc, pd = last.get("prompt_eval_count", 0), last.get("prompt_eval_duration", 0)
    return {"ttft_s": round(ttft, 2) if ttft is not None else None,
            "eval_tps": round(ev / (ed / 1e9), 1) if ed else None,
            "prompt_tps": round(pc / (pd / 1e9), 1) if pd else None, "eval_tokens": ev}


def table(results):
    head = "%-13s %-9s %-15s %-11s %-10s %-9s %-7s %-7s %-7s" % (
        "config", "loaded", "GPU / system", "peak mem", "peak swap", "min free", "load s", "TTFT s", "tok/s")
    rows = [head, "-" * len(head)]
    for r in results:
        if r.get("skipped"):
            rows.append("%-13s skipped: %s" % (r["config"], r["skipped"]))
            continue
        status = "yes" if r.get("loaded") else ("OOM" if r.get("oom_kill") else ("watchdog" if r.get("watchdog") else "no"))
        split = ("%.1f / %.1f GiB" % (r["vram"] / GIB, (r["size"] - r["vram"]) / GIB)) if r.get("loaded") else "-"
        rows.append("%-13s %-9s %-15s %-11s %-10s %-9s %-7s %-7s %-7s" % (
            r["config"], status, split, "%.1f GiB" % (r.get("peak_ollama_bytes", 0) / GIB),
            "%.1f GiB" % (r.get("peak_swap_bytes", 0) / GIB),
            "%.1f GiB" % ((r.get("min_free_bytes") or 0) / GIB), r.get("load_s", "-"),
            r.get("ttft_s", "-") if r.get("ttft_s") is not None else "-",
            r.get("eval_tps", "-") if r.get("eval_tps") is not None else "-"))
    return "\n".join(rows)


def exit_on_signal(signum, frame):
    """SIGHUP (the SSH connection dropped) or SIGTERM: leave through the
    normal way out, so the finally: below removes the runtime drop-in and
    restarts Ollama with its own settings."""
    raise SystemExit(128 + signum)


def main():
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, exit_on_signal)
    ap = argparse.ArgumentParser(description="Measure a big 'ram' model under a memory cap.")
    ap.add_argument("--model", default="gpt-oss:120b")
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--ncmoe", type=int, default=29,
                    help="for norepack-moe: experts of the first N layers stay on the CPU (gpt-oss:120b has 36)")
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                    help="comma list of: " + ", ".join(CONFIGS))
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run it with sudo")
    names = [n.strip() for n in a.configs.split(",") if n.strip()]
    bad = [n for n in names if n not in CONFIGS]
    if bad:
        sys.exit("unknown configuration(s): %s" % ", ".join(bad))
    mi = o1stats.meminfo()
    mem_max, mem_high = memory_cap(mi.get("MemTotal", 0))
    swap_bytes = encrypted_swap_bytes()
    why = swap_refusal(names, swap_bytes, vg_free_bytes() if "swap" in names and not swap_bytes else None)
    if why:
        sys.exit("Not started: " + why)
    try:
        tags = http("/api/tags").get("models", [])
    except (OSError, ValueError):
        sys.exit("Ollama isn't answering on %s (systemctl status ollama)" % API)
    entry = next((m for m in tags if o1ollama.same_model(m.get("name"), a.model)), None)
    if not entry:
        sys.exit("%s isn't installed (add it to /etc/ollama1/models.allow, then sudo ollama pull %s)" % (a.model, a.model))
    info = http("/api/show", {"model": a.model}).get("model_info", {})
    v = gateway_verdicts(int(entry["size"]), info, a.ctx, mi, o1stats.gpu_vram_total(), mem_max)
    say("\nControlled load test: %s at num_ctx %d" % (a.model, a.ctx))
    say("  model file          %.1f GiB" % (int(entry["size"]) / GIB))
    say("  gateway's estimate  %.1f GiB resident; system memory it may use %.1f GiB, with VRAM %.1f GiB"
        % (v["need"] / GIB, v["host_budget"] / GIB, v["total_budget"] / GIB))
    say("  gateway's verdict   repack on (today): %s   repack off: %s" % (v["repack_on"], v["repack_off"]))
    say("  Ollama's cap        MemoryMax %.1f GiB, MemoryHigh %.1f GiB (runtime, for this test)" % (mem_max / GIB, mem_high / GIB))
    say("  encrypted swap      %s" % (("%.0f GiB" % (swap_bytes / GIB)) if swap_bytes else "off"))
    say("  watchdog            kills Ollama if the machine gets under 1.5 GiB free")
    say("  configurations      " + ", ".join(names))
    for n in names:
        say("    %-13s %s" % (n, CONFIGS[n]["title"].format(ncmoe=a.ncmoe)))
    say("\nOllama restarts before each configuration and at the end (back to its normal settings).")
    say("Each load can take several minutes. Worst case, if the cap works: an Ollama restart.")
    with open("/dev/tty") as tty:
        sys.stdout.write("Type yes to start: ")
        sys.stdout.flush()
        if tty.readline().strip() != "yes":
            say("Nothing done.")
            return 1
    results = []
    try:
        for n in names:
            try:
                results.append(measure(n, CONFIGS[n], a.model, a.ctx, a.ncmoe, mem_max, mem_high, swap_bytes))
            except RuntimeError as e:
                say("  %s: %s" % (n, e))
                results.append({"config": n, "loaded": False, "error": str(e)})
    finally:
        say("\nputting Ollama back to its normal settings")
        restore()
    say("\n===== results: %s, num_ctx %d =====" % (a.model, a.ctx))
    say(table(results))
    for r in results:
        if r.get("buffers_mib"):
            say("  %-13s buffers: %s" % (r["config"], ", ".join("%s %.0f MiB" % kv for kv in sorted(r["buffers_mib"].items()))))
        if r.get("mmap_warning"):
            say("  %-13s llama.cpp still warned about mmap" % r["config"])
        if r.get("error") and not r.get("loaded"):
            say("  %-13s %s" % (r["config"], r["error"]))
    with open(JSON_OUT, "w") as f:
        json.dump({"model": a.model, "ctx": a.ctx, "verdicts": v, "results": results,
                   "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f, indent=1)
    os.chmod(JSON_OUT, 0o600)
    say("\nSaved: %s" % JSON_OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
