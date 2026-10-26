"""(6b520) Blind batches by turn: every run's packet for one turn, run names replaced by codes.
usage: blind_batches.py make <run>...   -> blind/<turn>.md + blind/map.json
       blind_batches.py split           -> judgments/<run>/opus-blind.jsonl from blind/out-<turn>.jsonl"""
import json, os, random, re, subprocess, sys
AE = os.path.dirname(os.path.abspath(__file__))
BD = os.path.join(AE, "blind", os.environ.get("BLIND_DIR", "wave"))
os.makedirs(BD, exist_ok=True)
sys.path.insert(0, AE)
import judge_packet as jp  # noqa: E402

if sys.argv[1] == "make":
    runs = sys.argv[2:]
    d, by_id = jp.load_questions(os.path.join(AE, "questions.json"))
    persona = json.load(open(os.path.join(AE, "persona.json")))
    prompt = jp.judge_prompt(d["as_of"])
    turns = {}
    for r in runs:
        for f in sorted(os.listdir(os.path.join(AE, "runs", r))):
            if not re.match(r"q\d\d(\.t\d)?\.json$", f):
                continue
            rec = json.load(open(os.path.join(AE, "runs", r, f)))
            rec["file"] = f
            turns.setdefault(f, []).append((r, rec))
    mapping = {}
    rnd = random.Random(520)
    for f, lst in sorted(turns.items()):
        codes = ["R%02d" % (i + 1) for i in range(len(lst))]
        rnd.shuffle(codes)
        parts = [prompt, "\nAll packets below answer the SAME turn, each from a different run (code names hide which "
                         "build made it). Grade each one on its own against the reference sheet; be consistent "
                         "across them. Output one JSON line per packet, with \"run\" set to its code.\n"]
        for (r, rec), code in zip(lst, codes):
            rec2 = dict(rec)
            rec2["run"] = code
            rec2["config"] = "local"
            rec2["tier"] = "local"
            txt = jp.packet(rec2, by_id[rec["id"]], persona, d["as_of"])
            txt = txt.replace(r, code)
            parts.append("\n\n---\n\n" + txt)
            mapping.setdefault(f, {})[code] = r
        open(os.path.join(BD, f.replace(".json", ".md")), "w").write("".join(parts))
    json.dump(mapping, open(os.path.join(BD, "map.json"), "w"), indent=1)
    print("turns", len(turns), "runs", len(runs))
else:
    mapping = json.load(open(os.path.join(BD, "map.json")))
    outs = {}
    for f, codes in mapping.items():
        p = os.path.join(BD, "out-" + f.replace(".json", ".jsonl"))
        if not os.path.exists(p):
            print("missing", p)
            continue
        for ln in open(p):
            ln = ln.strip()
            if not ln.startswith("{"):
                continue
            o = json.loads(ln)
            run = codes.get(o.get("run"))
            if not run:
                print("unknown code", f, o.get("run"))
                continue
            o["run"] = run
            o["file"] = f
            outs.setdefault(run, []).append(o)
    for run, lst in outs.items():
        dd = os.path.join(AE, "judgments", run)
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, os.environ.get("BLIND_OUT", "opus-blind.jsonl")), "w") as fh:
            for o in lst:
                fh.write(json.dumps(o, ensure_ascii=False) + "\n")
        print(run, len(lst))
