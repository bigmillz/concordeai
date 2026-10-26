"""(6b520) Scores by category and paired comparisons over judgments/<run>/*.jsonl.
usage: paired.py <groupA runs comma> <groupB runs comma> [label]"""
import json, os, sys, glob, math, statistics as st
AE = os.path.dirname(os.path.abspath(__file__))
Q = {q["id"]: q for q in json.load(open(os.path.join(AE, "questions.json")))["questions"]}
KEYS = ["correct_current", "grounded", "depth", "fit", "structure", "concise"]


def load(run):
    out = {}
    for f in sorted(glob.glob(os.path.join(AE, "judgments", run, "*.jsonl"))):
        for ln in open(f):
            ln = ln.strip()
            if ln.startswith("{"):
                o = json.loads(ln)
                out[o["file"]] = o
    return out


def cat(f):
    return "followup" if ".t" in f else Q[f.split(".")[0]]["category"]


def hi(o):
    return sum(1 for h in o.get("hallucinations") or [] if h.get("severity") == "high")


def summary(runs):
    rows = {}
    for r in runs:
        j = load(r)
        rows[r] = j
    return rows


def table(runs):
    J = summary(runs)
    print("%-14s %6s %6s %6s %6s | %s | high" % ("run", "fresh", "travel", "follow", "all", " ".join(k[:5] for k in KEYS)))
    for r, j in J.items():
        by = {}
        for f, o in j.items():
            by.setdefault(cat(f), []).append(o["overall"])
        allv = [o["overall"] for o in j.values()]
        sc = [st.mean(o["scores"][k] for o in j.values()) for k in KEYS]
        print("%-14s %6.2f %6.2f %6.2f %6.2f | %s | %d  (n=%d)" % (
            r, st.mean(by.get("fresh", [0])), st.mean(by.get("travel", [0])), st.mean(by.get("followup", [0])),
            st.mean(allv), " ".join("%5.1f" % x for x in sc), sum(hi(o) for o in j.values()), len(j)))
    return J


def paired(A, B, J):
    """per turn: mean over B runs minus mean over A runs"""
    files = sorted(set.intersection(*[set(J[r]) for r in A + B]))
    d, dh = [], []
    for f in files:
        a = st.mean(J[r][f]["overall"] for r in A)
        b = st.mean(J[r][f]["overall"] for r in B)
        d.append(b - a)
        dh.append(st.mean(hi(J[r][f]) for r in B) - st.mean(hi(J[r][f]) for r in A))
    se = st.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else 0
    better = sum(1 for x in d if x > 0.01)
    worse = sum(1 for x in d if x < -0.01)
    print("  %s -> %s: mean %+.2f (se %.2f) over %d turns; better/worse/same %d/%d/%d; high flags/answer %+.2f" % (
        ",".join(A), ",".join(B), st.mean(d), se, len(d), better, worse, len(d) - better - worse, st.mean(dh)))
    for c in ("fresh", "travel", "followup"):
        dc = [x for f, x in zip(files, d) if cat(f) == c]
        if dc:
            print("      %-8s %+.2f over %d" % (c, st.mean(dc), len(dc)))
    return d


if __name__ == "__main__":
    A = sys.argv[1].split(",")
    B = sys.argv[2].split(",")
    J = table(A + B)
    paired(A, B, J)
