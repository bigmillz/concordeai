"""Dictation end to end through the PAGE's own UI in the C++ shell (NOTES 6b642).

voice_proof.py drives the bridge; this drives what a person does. A probe
build of the preview, its server and page from a tree whose page uses the
native path (beta-4-voice's, 6b641: dictStart -> dictStartNat when
window.pywebview.api.voice exists), runs as a dev copy on 9991 (a temp
MILLENAI_HOME, never the real data folder or a paired server). The clip
plays through the same conversion and recognizer path the microphone uses
(//native:voice-file; past the clip's end, silence, as a quiet room gives),
the composer is focused and a Cmd+D keydown is dispatched to it:

  * hold: the Meta keyup comes when the clip ends -> the stop at the release;
  * tap: the Meta keyup after ~120 ms and nothing else -> the toggle stays on
    and ends itself ~1 s after the words stop.

Asserted for each: no /api/dictate/ request reached the server (the page's
resource timing, which every fetch fills, nFetch's included), #input.value
grows while the clip plays, the final is the words said, #composer has
dict-on while listening and not after. Recorded: press -> first level,
speech -> first partial (the clip's own speech onset, measured from its
samples), end of audio -> final (hold), end of words -> auto-stop and
-> final (tap). Five runs each, medians.

    python3 cpp/tests/page_voice_proof.py "<ConcordeAI C++ Preview.app>" [--runs 5]
"""
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from window_e2e import check, RESULTS, port_open  # noqa: E402
from voice_proof import launch, stop, norm  # noqa: E402

PORT = 9991
WORDS = "What's up, man?"


def clip(d):
    a, w = os.path.join(d, "wsm.aiff"), os.path.join(d, "wsm.wav")
    subprocess.run(["say", "-v", "Samantha", "-o", a, WORDS], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", a, w], check=True)
    with wave.open(w) as r:
        sr, n = r.getframerate(), r.getnframes()
        import array
        x = array.array("h", r.readframes(n))
    loud = [i for i, v in enumerate(x) if abs(v) > 500]
    return w, 1000.0 * loud[0] / sr, 1000.0 * loud[-1] / sr, 1000.0 * n / sr


PRESS = r"""
const MODE = %s;   // "hold" | "tap"
const t0 = performance.now();
const T = () => Math.round(performance.now() - t0);
const ev = [], samples = [], calls = [];
const pageEv = window.__concordeVoice;
let eofRes, doneRes;
const eofP = new Promise(r => eofRes = r), doneP = new Promise(r => doneRes = r);
window.__concordeVoice = e => {
  const r = {ev: e.ev, t: T()};
  if (e.text != null) r.text = e.text;
  if (e.ms != null) r.ms = e.ms;
  if (e.ev !== "level" || !ev.some(x => x.ev === "level")) ev.push(r);
  try { pageEv && pageEv(e); } finally {
    if (e.ev === "eof") eofRes(r.t);
    if (e.ev === "done") doneRes(r.t);
  }
};
// the page's own calls into the shell, by name (a global function)
const natCall = window.dictNatCall;
if (typeof natCall === "function")
  window.dictNatCall = cmd => { calls.push({act: cmd && cmd.act, t: T()}); return natCall(cmd); };
try { performance.setResourceTimingBufferSize(10000); } catch (e) {}
performance.clearResourceTimings();
const cmp = document.getElementById("composer"), inp = document.getElementById("input");
inp.value = ""; inp.dispatchEvent(new Event("input"));
inp.focus();
// event-driven, not a timer: a window the system has occluded (a locked
// screen at night) throttles timers to a second, and a burst of partials
// lands within one task. The page's dictShow fires "input"; dict-on is a class.
let last = null;
const snap = () => {
  const s = [T(), inp.value, cmp.classList.contains("dict-on")];
  if (!last || s[1] !== last[1] || s[2] !== last[2]) samples.push(s);
  last = s;
};
inp.addEventListener("input", snap);
const mo = new MutationObserver(snap);
mo.observe(cmp, {attributes: true, attributeFilter: ["class"]});
const iv = setInterval(snap, 10);
const key = (type, init) => inp.dispatchEvent(new KeyboardEvent(type, Object.assign({bubbles: true, cancelable: true, composed: true}, init)));
const down = key("keydown", {key: "d", code: "KeyD", metaKey: true});
const pressDefaultPrevented = !down;
let tUp = -1;
if (MODE === "hold") {
  await Promise.race([eofP, new Promise(r => setTimeout(r, 15000))]);
  tUp = T(); key("keyup", {key: "Meta", code: "MetaLeft", metaKey: false});
} else {
  await new Promise(r => setTimeout(r, 120));
  tUp = T(); key("keyup", {key: "Meta", code: "MetaLeft", metaKey: false});
}
await Promise.race([doneP, new Promise(r => setTimeout(r, 20000))]);
await new Promise(r => setTimeout(r, 400));   // the final lands and dict-on clears
clearInterval(iv); mo.disconnect(); inp.removeEventListener("input", snap);
samples.push([T(), inp.value, cmp.classList.contains("dict-on")]);
window.__concordeVoice = pageEv;
if (typeof natCall === "function") window.dictNatCall = natCall;
const hidden = document.hidden;
const reqs = performance.getEntriesByType("resource").map(e => e.name).filter(n => n.includes("/api/"));
return {ev, samples, calls, tUp, pressDefaultPrevented, reqs, value: inp.value,
        dictOn: cmp.classList.contains("dict-on"), hidden, placeholder: inp.placeholder, recording: !!window.recording};
"""


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs)) if xs else None


def analyse(o, onset, offset, mode):
    ev = o.get("ev", [])
    get = lambda k: next((e for e in ev if e["ev"] == k), None)  # noqa: E731
    li, lv, fin, eof = get("listening"), get("level"), get("final"), get("eof")
    p1 = next((e for e in ev if e["ev"] == "partial" and e.get("text")), None)
    start = (li["t"] - li.get("ms", 0)) if li else None        # the shell's start, on the page's clock
    speech = start + onset if start is not None else None
    words_end = start + offset if start is not None else None
    on = [s for s in o.get("samples", []) if s[2]]
    off_after = [s for s in o.get("samples", []) if not s[2] and on and s[0] > on[0][0]]
    vals = [s[1] for s in o.get("samples", []) if s[2] and s[1]]
    stop_call = next((c["t"] for c in o.get("calls", []) if c["act"] == "stop"), None)
    landed = off_after[0][0] if off_after else None
    r = {
        "final_event": fin and fin.get("text"),
        "value": o.get("value"),
        "press_level": lv and lv["t"],
        "speech_partial": (p1["t"] - speech) if p1 and speech is not None else None,
        "dict_on_at": on[0][0] if on else None,
        "landed": landed,
        "grows": len(vals) >= 2 and len(vals[-1]) > len(vals[0]),
        "values_while_on": vals,
        "dict_reqs": [u for u in o.get("reqs", []) if "/api/dictate/" in u],
        "dict_on_after": o.get("dictOn"),
        "calls": o.get("calls"),
        "placeholder": o.get("placeholder"),
        "hidden": o.get("hidden"),
    }
    if mode == "hold":
        r["eof_final"] = (fin["t"] - eof["t"]) if fin and eof else None
        r["eof_landed"] = (landed - eof["t"]) if landed and eof else None
    else:
        r["words_autostop"] = (stop_call - words_end) if stop_call is not None and words_end is not None else None
        r["words_final"] = (fin["t"] - words_end) if fin and words_end is not None else None
        r["words_landed"] = (landed - words_end) if landed and words_end is not None else None
    return r


def main():
    app = os.path.abspath(sys.argv[1])
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 5
    if port_open(PORT):
        sys.exit("%d is taken" % PORT)
    work = tempfile.mkdtemp(prefix="cpp-pagevoice-")
    wav, onset, offset, dur = clip(work)
    print("  clip: speech %.0f-%.0f ms of %.0f ms" % (onset, offset, dur))
    p, pr, marks, base = launch(app, PORT)
    report = {}
    try:
        check("the page loaded in the shell", any("page loaded" in m for m in marks), marks[-5:])
        st = pr.run("""
for (let i = 0; i < 100 && !(document.getElementById('input') && typeof dictNative === 'function'); i++)
  await new Promise(r => setTimeout(r, 100));
const veils = [...document.querySelectorAll('[id$="-veil"]')].filter(v => !v.hidden).map(v => v.id);
return {native: typeof dictNative === 'function' && dictNative(), modal: typeof dictModal === 'function' && dictModal(),
        veils, hz: document.documentElement.classList.contains('hz') || document.body.classList.contains('hz'),
        natCallGlobal: typeof window.dictNatCall === 'function'};""")
        print("  page:", st)
        check("the page sees the shell's voice bridge (dictNative)", st.get("native") is True, st)
        if st.get("modal"):
            # a first-run sheet would refuse the hotkey (no start from a dialog, 6b338); close it as a person would
            pr.run("for (const v of document.querySelectorAll('[id$=\"-veil\"]')) v.hidden = true; return true;")
        pr.run("//native:voice-file " + wav)
        pr.run("document.getElementById('input').focus(); await new Promise(r => setTimeout(r, 800)); return true;")
        for mode in ("hold", "tap"):
            rows = []
            for i in range(runs):
                o = pr.run(PRESS % json.dumps(mode), timeout=60)
                if "error" in o:
                    print("  %s run %d: probe error %s" % (mode, i + 1, o))
                    rows.append(None)
                    continue
                a = analyse(o, onset, offset, mode)
                rows.append(a)
                keys = ("press_level", "speech_partial", "eof_final", "eof_landed") if mode == "hold" else \
                    ("press_level", "speech_partial", "words_autostop", "words_final", "words_landed")
                print("  %s run %d: value %r, %s, dictate reqs %d, partials %s" % (
                    mode, i + 1, a["value"], ", ".join("%s %s ms" % (k, a[k]) for k in keys), len(a["dict_reqs"]),
                    a["values_while_on"]))
                time.sleep(0.5)
            good = [r for r in rows if r]
            check("%s: every run finished" % mode, len(good) == runs, rows)
            check("%s: no /api/dictate/ request reached the server" % mode,
                  good and all(not r["dict_reqs"] for r in good), [r["dict_reqs"] for r in good])
            check("%s: #input grows while the clip plays" % mode, good and all(r["grows"] for r in good),
                  [r["values_while_on"] for r in good])
            check("%s: the final is the words said (%s)" % (mode, WORDS),
                  good and all(norm(r["value"]) == norm(WORDS) and norm(r["final_event"]) == norm(WORDS) for r in good),
                  [(r["value"], r["final_event"]) for r in good])
            check("%s: #composer has dict-on while listening and not after" % mode,
                  good and all(r["dict_on_at"] is not None and r["dict_on_after"] is False and r["landed"] for r in good),
                  [(r["dict_on_at"], r["dict_on_after"], r["landed"]) for r in good])
            if mode == "tap":
                check("tap: the toggle ended itself (the page's stop, no key) about a second after the words",
                      good and all(r["words_autostop"] is not None and 700 <= r["words_autostop"] <= 2500 for r in good),
                      [r["words_autostop"] for r in good])
            ks = [k for k in (good[0] if good else {}) if isinstance((good[0] or {}).get(k), (int, float))
                  and not isinstance(good[0][k], bool)]
            report[mode] = {k: med([r[k] for r in good]) for k in ks}
            print("  medians (%s):" % mode, report[mode])
    finally:
        stop(p, base)
        shutil.rmtree(work, ignore_errors=True)
    print("REPORT", json.dumps(report))
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
