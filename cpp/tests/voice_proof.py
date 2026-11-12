"""Dictation, native in the C++ shell, proven without a voice (NOTES 6b642).

A probe build of the shell runs as a dev copy of its own (MILLENAI_DEV, a
temp MILLENAI_HOME, ports 9991-9993, never the real data folder or a paired
server). Clips made with `say` are played through the SAME conversion and
recognizer path the microphone uses (the probe's //native:voice-file swaps
the input node for a file read in real time), and the page's bridge,
window.pywebview.api.voice(), is driven from inside the page:

  * the reply contract (warm, start, stop, cancel) and the events;
  * partials arrive while the clip plays and grow, each the WHOLE utterance;
  * the final is the words said (case and punctuation aside), every
    segment of it (SpeechTranscriber finalises "What's up?" and " Man."
    separately);
  * the engine is SpeechAnalyzer on macOS 26+; the locale is the asked one,
    and with the system's languages set to Spanish (Uruguay) for the shell
    process only (-AppleLanguages (es-UY) -AppleLocale es_UY) an explicit
    en-US still writes English; with no language asked it follows the
    system's (recorded, not asserted: that is the honest answer);
  * a cancel sends nothing more; a second start drops the first.
Timings, five runs each, medians: start -> capture, -> first level,
-> first partial, end of audio -> final.

    python3 cpp/tests/voice_proof.py <ConcordeAI.app> [--runs 5]
"""
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from window_e2e import Probe, check, RESULTS, port_open  # noqa: E402

VENV = os.path.expanduser("~/Library/Application Support/MillenAI/venv/bin/python3")
PORTS = (9991,)   # one copy at a time, on 9991 (9992-9993 may be another helper's)


def clips(d):
    """WAVs, 48 kHz mono float-free int16 (what a microphone gives, roughly)."""
    out = {}
    lines = {
        "wsm": "What's up, man?",
        "two": "Let's meet at the station tonight. Bring the blue umbrella, please.",
    }
    for k, text in lines.items():
        a, w = os.path.join(d, k + ".aiff"), os.path.join(d, k + ".wav")
        subprocess.run(["say", "-v", "Samantha", "-o", a, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@48000", "-c", "1", a, w], check=True)
        out[k] = (w, text)
    # the words, then 1.5 s of silence (what a pause before the auto-stop is)
    src, dst = out["wsm"][0], os.path.join(d, "tail.wav")
    with wave.open(src) as r:
        p, fr = r.getparams(), r.readframes(r.getnframes())
    with wave.open(dst, "wb") as o:
        o.setparams(p)
        o.writeframes(fr + b"\0\0" * int(p.framerate * 1.5))
    out["tail"] = (dst, out["wsm"][1])
    return out


def norm(s):
    return " ".join(re.sub(r"[^\w' ]+", " ", (s or "").lower().replace("’", "'")).split())


def launch(app, port, extra_args=()):
    base = tempfile.mkdtemp(prefix="cpp-voice-")
    home, probe = os.path.join(base, "home"), os.path.join(base, "probe")
    os.makedirs(home, mode=0o700)
    os.makedirs(probe)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MILLENAI_", "CONCORDE_"))}
    env.update(MILLENAI_DEV="1", MILLENAI_HOME=home, MILLENAI_PORT=str(port),
               MILLENAI_TEST_HOOKS="no-downloads", CONCORDE_PYTHON=VENV,
               CONCORDE_SHELL_TIMING="1", CONCORDE_SHELL_PROBE=probe)
    marks = []
    p = subprocess.Popen([os.path.join(app, "Contents", "MacOS", "ConcordeAI"), *extra_args], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [marks.append(l.rstrip()) for l in p.stderr], daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < 90 and not any("page loaded" in m for m in marks):
        if p.poll() is not None:
            break
        time.sleep(0.1)
    return p, Probe(probe), marks, base


def stop(p, base):
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        p.kill()
    shutil.rmtree(base, ignore_errors=True)


# one utterance, driven from inside the page: start, the clip plays, stop at
# its end (the eof event), the final; every event with its time from start
UTTER = r"""
const L = %s;
const ev = []; let res; const fin = new Promise(r => res = r);
const t0 = performance.now(); let u = 0, tStop = -1;
const prev = window.__concordeVoice;
window.__concordeVoice = e => {
  e.t = Math.round(performance.now() - t0); ev.push(e);
  if (e.ev === "eof" && e.u === u) { tStop = e.t; window.pywebview.api.voice({act: "stop", u}); }
  if (e.ev === "done" && e.u === u) res();
};
try {
  const r = await window.pywebview.api.voice({act: "start", lang: L});
  r.t = Math.round(performance.now() - t0);
  if (!r || !r.ok) return {r, ev};
  u = r.u;
  await Promise.race([fin, new Promise(x => setTimeout(x, 25000))]);
  return {r, ev, tStop};
} finally { window.__concordeVoice = prev; }
"""


def utter(pr, wav, lang):
    pr.run("//native:voice-file " + wav)
    return pr.run(UTTER % json.dumps(lang), timeout=40)


def first(ev, kind):
    for e in ev:
        if e.get("ev") == kind:
            return e
    return None


def summary(o):
    ev = o.get("ev", [])
    parts = [e["text"] for e in ev if e.get("ev") == "partial"]
    f = first(ev, "final")
    lv = first(ev, "level")
    li = first(ev, "listening")
    p1 = next((e for e in ev if e.get("ev") == "partial" and e.get("text")), None)
    return {
        "reply": o.get("r"),
        "parts": parts,
        "final": f and f.get("text"),
        "capture": li and li.get("ms"),
        "level": lv and lv.get("t"),
        "partial": p1 and p1.get("t"),
        "end_to_final": (f["t"] - o["tStop"]) if f and o.get("tStop", -1) >= 0 else None,
        "levels": [e["rms"] for e in ev if e.get("ev") == "level"],
        "kinds": sorted({e.get("ev") for e in ev}),
    }


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def main():
    app = os.path.abspath(sys.argv[1])
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 5
    for port in PORTS:
        if port_open(port):
            sys.exit("%d is taken" % port)
    work = tempfile.mkdtemp(prefix="cpp-voice-clips-")
    cl = clips(work)
    report = {}
    # ------------------------------------------------ the system as it is
    p, pr, marks, base = launch(app, PORTS[0])
    try:
        check("the page loaded in the shell", any("page loaded" in m for m in marks), marks[-5:])
        t = time.time()
        w = pr.run("return await window.pywebview.api.voice({act: 'warm', lang: ''});")
        report["warm_cold_ms"] = round((time.time() - t) * 1000)
        print("  warm:", w, "(%d ms, probe round trip included)" % report["warm_cold_ms"])
        check("warm: ok, the analyzer, a locale, ready",
              w.get("ok") is True and w.get("engine") == "analyzer" and w.get("ready") is True
              and w.get("locale", "").startswith("en"), w)
        t = time.time()
        w2 = pr.run("return await window.pywebview.api.voice({act: 'warm', lang: 'en-US'});")
        print("  warm again:", w2, "(%d ms)" % round((time.time() - t) * 1000))
        check("warm en-US resolves en-US", w2.get("locale") == "en-US", w2)
        check("the module is DictationTranscriber for en-US", w2.get("module") == "dictation", w2)
        bad = pr.run("return await window.pywebview.api.voice({act: 'nope'});")
        check("an unknown act is refused", bad == {"ok": False, "code": "bad-act"}, bad)

        rows = []
        for i in range(runs):
            o = utter(pr, cl["wsm"][0], "en-US")
            s = summary(o)
            rows.append(s)
            print("  run %d: reply %s, final %r, capture %s ms, first level %s ms, first partial %s ms, "
                  "end of audio->final %s ms, partials %s" % (i + 1, s["reply"], s["final"], s["capture"],
                                                             s["level"], s["partial"], s["end_to_final"],
                                                             s["parts"]))
        s0 = rows[0]
        r0 = s0["reply"] or {}
        check("start replies {ok, u, engine, locale}",
              r0.get("ok") is True and isinstance(r0.get("u"), int) and r0.get("engine") == "analyzer"
              and r0.get("locale") == "en-US", r0)
        check("events: listening, level, partial, final, done (and the probe's eof)",
              all(k in s0["kinds"] for k in ("listening", "level", "partial", "final", "done")), s0["kinds"])
        check("every run's final is the words said, all of them",
              all(norm(r["final"]) == norm(cl["wsm"][1]) for r in rows), [r["final"] for r in rows])
        check("partials arrive before the final and grow",
              all(len(r["parts"]) >= 2 and len(r["parts"][-1]) > len(r["parts"][0]) for r in rows),
              [r["parts"] for r in rows])
        check("the levels are real numbers 0..1, about 20 a second",
              all(r["levels"] and all(0 <= x <= 1 for x in r["levels"]) and max(r["levels"]) > 0.01
                  for r in rows), rows[0]["levels"][:10])
        report["wsm"] = {k: med([r[k] for r in rows]) for k in ("capture", "level", "partial", "end_to_final")}
        print("  medians (What's up, man?):", report["wsm"])

        rows2 = [summary(utter(pr, cl["two"][0], "en-US")) for _ in range(min(runs, 3))]
        for s in rows2:
            print("  two sentences: final %r, first partial %s ms, end->final %s ms, %d partials"
                  % (s["final"], s["partial"], s["end_to_final"], len(s["parts"])))
        check("two sentences: every segment is in the final",
              all(norm(s["final"]) == norm(cl["two"][1]) for s in rows2), [s["final"] for s in rows2])
        check("two sentences: a partial is the whole utterance so far (the first sentence stays)",
              any(norm(cl["two"][1].split(".")[0]) in norm(x) and len(norm(x)) > len(norm(cl["two"][1].split(".")[0]))
                  for s in rows2 for x in s["parts"]), rows2[0]["parts"])
        report["two"] = {k: med([r[k] for r in rows2]) for k in ("partial", "end_to_final")}

        st = summary(utter(pr, cl["tail"][0], "en-US"))
        print("  words + 1.5 s silence: final %r, partials %s, end->final %s ms" % (st["final"], st["parts"],
                                                                                  st["end_to_final"]))
        check("words then silence: the final is the words", norm(st["final"]) == norm(cl["tail"][1]), st)
        quiet = [x for x in st["levels"][-20:]]
        check("words then silence: the level falls to near zero in the pause (what the page's auto-stop reads)",
              quiet and max(quiet[-10:]) < 0.005, quiet)

        # a stop before any audio (the analyzer would hang finishing an
        # utterance given nothing): the final is "" and done comes
        o = pr.run(r"""
const ev = []; const prev = window.__concordeVoice;
let res; const fin = new Promise(r => res = r);
window.__concordeVoice = e => { ev.push(e); if (e.ev === "done") res(); };
const r = await window.pywebview.api.voice({act: "start", lang: "en-US"});
const s = await window.pywebview.api.voice({act: "stop", u: r.u});
const t0 = performance.now();
await Promise.race([fin, new Promise(x => setTimeout(x, 6000))]);
window.__concordeVoice = prev;
return {r, s, ms: Math.round(performance.now() - t0), ev: ev.filter(e => e.ev !== "level").map(e => e.ev + ":" + (e.text ?? ""))};
""", timeout=30)
        print("  stop before any audio:", o)
        check("a stop before any audio still ends: final \"\" then done, quickly",
              "final:" in (o.get("ev") or []) and "done:" in (o.get("ev") or []) and o.get("ms", 9999) < 3000, o)

        # cancel: nothing more for that utterance
        o = pr.run(r"""
const ev = []; const prev = window.__concordeVoice;
window.__concordeVoice = e => ev.push(e);
const r = await window.pywebview.api.voice({act: "start", lang: "en-US"});
await new Promise(x => setTimeout(x, 600));
const c = await window.pywebview.api.voice({act: "cancel", u: r.u});
const n = ev.length;
await new Promise(x => setTimeout(x, 2500));
window.__concordeVoice = prev;
return {r, c, after: ev.slice(n).map(e => e.ev), before: [...new Set(ev.slice(0, n).map(e => e.ev))]};
""".replace("\n", "\n"), timeout=30)
        check("cancel: ok, and no event for it afterwards", o.get("c") == {"ok": True} and o.get("after") == [], o)
        # a second start drops the first (one microphone)
        o = pr.run(r"""
const ev = []; const prev = window.__concordeVoice;
let res; const fin = new Promise(r => res = r);
window.__concordeVoice = e => { ev.push(e); if (e.ev === "eof") window.pywebview.api.voice({act: "stop", u: e.u}); if (e.ev === "done") res(); };
const a = await window.pywebview.api.voice({act: "start", lang: "en-US"});
await new Promise(x => setTimeout(x, 300));
const b = await window.pywebview.api.voice({act: "start", lang: "en-US"});
await Promise.race([fin, new Promise(x => setTimeout(x, 15000))]);
window.__concordeVoice = prev;
return {a, b, ofA: ev.filter(e => e.u === a.u && e.ev !== "level").map(e => e.ev), finals: ev.filter(e => e.ev === "final")};
""", timeout=40)
        check("a second start drops the first: the first gets nothing after it, the second finishes",
              o.get("b", {}).get("u") == o.get("a", {}).get("u", 0) + 1
              and not any(k in (o.get("ofA") or []) for k in ("final", "done"))
              and len(o.get("finals") or []) == 1 and o["finals"][0]["u"] == o["b"]["u"], o)
        tl = [m for m in marks if "voice:" in m]
        print("  native timing lines (last 3):")
        for m in tl[-3:]:
            print("   ", m)
        check("the shell prints one timing line per utterance", len(tl) >= runs, len(tl))
    finally:
        stop(p, base)

    # ------------------------------------------------ Spanish (Uruguay) system
    p, pr, marks, base = launch(app, PORTS[0], ("-AppleLanguages", "(es-UY)", "-AppleLocale", "es_UY"))
    try:
        t = time.time()
        # a language whose model isn't on this Mac yet: the OS installs it at the warm (minutes, once)
        w = pr.run("return await window.pywebview.api.voice({act: 'warm', lang: ''});", timeout=300)
        print("  es-UY system, warm with no language:", w, "(%d ms)" % round((time.time() - t) * 1000))
        o = summary(utter(pr, cl["wsm"][0], "en-US"))
        print("  es-UY system, lang en-US: reply %s final %r" % (o["reply"], o["final"]))
        check("a Spanish-region system with en-US asked still writes English",
              (o["reply"] or {}).get("locale") == "en-US" and norm(o["final"]) == norm(cl["wsm"][1]), o)
        o2 = summary(utter(pr, cl["wsm"][0], ""))
        print("  es-UY system, no language asked: reply %s final %r (recorded: the system's language)"
              % (o2["reply"], o2["final"]))
        report["es_uy_no_lang"] = {"warm": w, "reply": o2["reply"], "final": o2["final"]}
        check("with no language asked it follows the system's preferred one, never detection",
              (w.get("locale") or "").startswith("es"), w)
    finally:
        stop(p, base)
    shutil.rmtree(work, ignore_errors=True)
    print("REPORT", json.dumps(report))
    bad = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
