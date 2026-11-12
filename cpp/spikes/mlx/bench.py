"""The MLX spike's A/B (NOTES 6b611): the same model, the same prompt ids,
greedy, the same token budget, C++ (llama_spike) against today's Python path
(mlx_lm.stream_generate), each in a fresh process at `nice -n 10`, rounds
alternating. Both are timed the same way from outside the model: time to the
first token from the start of generation (after load), and decode tokens/s
over the tokens after the first. Also: load time, peak memory, and whether the
two chose the same tokens.

    python3 bench.py <llama_spike> <model dir> [--rounds 3] [--max 256] [--out results.json]
    python3 bench.py --py <model dir> <ids file> <max>       (one Python run; internal)

Run on the app's venv (it has mlx and mlx_lm). One engine at a time: the
caller holds the scratchpad's engines.lock and checks no engine is resident.
"""
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

PROMPT = ("I keep a small sailboat on a lake that freezes every winter. Explain, in a few short paragraphs, "
          "how I should prepare the boat for the cold months: what to do about the hull, the rigging, the sails, "
          "the outboard motor and its fuel, the battery, and anything left aboard that could freeze or grow "
          "mould. Then give me a short checklist I can print and tape inside the cabin door, and finish with "
          "one sentence about what to check first when the ice goes out in spring.")


def py_run(model_dir, ids_file, max_tokens):
    import mlx.core as mx
    from mlx_lm import load, stream_generate
    ids = [int(x) for x in open(ids_file).read().split()]
    t0 = time.perf_counter()
    model, tok = load(model_dir)
    load_ms = (time.perf_counter() - t0) * 1000
    out, t_first = [], None
    t1 = time.perf_counter()
    for r in stream_generate(model, tok, prompt=ids, max_tokens=max_tokens):
        if t_first is None:
            t_first = time.perf_counter()
        out.append(r.token)
        last = time.perf_counter()
    ttft = (t_first - t1) * 1000
    gen = len(out)
    print(json.dumps({"impl": "python", "load_ms": round(load_ms, 1), "prompt_tokens": len(ids),
                      "ttft_ms": round(ttft, 1), "prefill_tps": round(len(ids) / (ttft / 1000), 1),
                      "gen_tokens": gen, "decode_tps": round((gen - 1) / (last - t_first), 1) if gen > 1 else 0,
                      "peak_mem_mb": round(mx.get_peak_memory() / 1048576, 1), "ids": out}))


def main():
    if sys.argv[1] == "--py":
        return py_run(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    spike, model_dir = os.path.abspath(sys.argv[1]), sys.argv[2]
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 3
    max_tokens = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 256
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    os.environ["HF_HUB_OFFLINE"] = "1"
    from transformers import AutoTokenizer  # mlx_lm's own tokenizer stack
    tok = AutoTokenizer.from_pretrained(model_dir)
    ids = tok.apply_chat_template([{"role": "user", "content": PROMPT}], add_generation_prompt=True)
    ids = list(ids["input_ids"]) if hasattr(ids, "keys") else list(ids)  # a BatchEncoding in newer transformers
    tmp = tempfile.mkdtemp(prefix="mlx-spike-")
    ids_file = os.path.join(tmp, "ids.txt")
    open(ids_file, "w").write(" ".join(str(i) for i in ids))
    runs = []
    for r in range(rounds):
        for impl in ("cpp", "python"):
            cmd = (["nice", "-n", "10", spike, model_dir, ids_file, str(max_tokens), "--quiet"] if impl == "cpp" else
                   ["nice", "-n", "10", sys.executable, os.path.abspath(__file__), "--py", model_dir, ids_file,
                    str(max_tokens)])
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            line = [l for l in p.stdout.splitlines() if l.startswith("{")]
            if not line:
                print(impl, "failed:", p.stderr[-800:])
                continue
            d = json.loads(line[-1])
            d["round"] = r + 1
            runs.append(d)
            print(json.dumps({k: v for k, v in d.items() if k != "ids"}), flush=True)
    cpp = [d for d in runs if d["impl"] == "cpp"]
    py = [d for d in runs if d["impl"] == "python"]
    summary = {}
    for name, rs in (("cpp", cpp), ("python", py)):
        if rs:
            summary[name] = {k: round(statistics.median(d[k] for d in rs), 1)
                             for k in ("load_ms", "ttft_ms", "prefill_tps", "decode_tps", "peak_mem_mb", "gen_tokens")}
    if cpp and py:
        a, b = cpp[0]["ids"], py[0]["ids"]
        same = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        summary["same_tokens"] = {"identical_prefix": same, "cpp_len": len(a), "python_len": len(b)}
        text = tok.decode(a)
        summary["cpp_text_head"] = text[:300]
    summary["prompt_tokens"] = len(ids)
    print("SUMMARY", json.dumps(summary, indent=1))
    if out_path:
        json.dump({"runs": runs, "summary": summary}, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
