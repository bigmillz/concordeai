"""MillenAI full-surface smoke test — the Fable-worthiness gate.

Runs against a locally spawned instance with a key (so every gate is
exercised) and reports a scorecard. Engine tests run REAL models.
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter


def _uq(s):
    return urllib.parse.quote(s, safe="")

BASE = "http://127.0.0.1:9894"
KEY = "smoketestkey123"
K = "millen_key=" + KEY

RESULTS = []

# the engine lives in server-side Python, not the served page — a few
# checks assert against the source directly
_MILLENAI_SRC = open("millenai.py").read()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  — " + detail if detail and not ok else ""))


def req(path, method="GET", data=None, headers=None, cookie=None, timeout=30):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


print("== access control ==")
# the key door is retired (1.20): local goes straight to the app, remote
# strangers land on the account screen
s, h, b = req("/")
check("local bare URL -> app", s == 200 and b"id=\"skyline\"" in b)
s, h, b = req("/?key=oldlink")
check("legacy key links still land", s == 200 and b"id=\"skyline\"" in b)
s, h, b = req("/", headers={"X-Forwarded-For": "1.2.3.4"})
check("remote stranger -> account screen", b"continue as guest" in b.lower()
      and b"pinform" in b)

print("== identities ==")
s, h, b = req("/", cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
check("remote no-identity -> sign-in", b"continue as guest" in b.lower())
s, h, b = req("/api/guest", "POST", {}, cookie=K,
              headers={"X-Forwarded-For": "1.2.3.4"})
mg = re.search(r"millen_user=([0-9a-f]{20})", str(h))
check("guest tap mints an identity", s == 200 and mg)
s, h, b = req("/api/welcome", "POST", {"name": "smoke", "pin": "1234"},
              cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
check("short PIN rejected", b"8-12 digit" in b)
s, h, b = req("/api/welcome", "POST", {"name": "smoke", "pin": "88881111"},
              cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
m = re.search(r"millen_user=([0-9a-f]{20})", str(h))
check("8-digit PIN -> identity cookie", s == 200 and m)
smoke_uid = m.group(1) if m else ""
s, h, b = req("/api/chats", cookie=K + "; millen_user=" + smoke_uid,
              headers={"X-Forwarded-For": "1.2.3.4"})
check("fresh profile sees empty chats", b == b'{"chats": []}')
s, h, b = req("/api/chats", cookie=K)
check("local owner sees real chats", b"title" in b)
own_pin = open("/Users/patrickmiller/Library/Application Support/MillenAI/owner_pin").read().strip()
s, h, b = req("/api/welcome", "POST", {"name": "anyname", "pin": own_pin},
              cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
m2 = re.search(r"millen_user=([0-9a-f]{20})", str(h))
s, h, b = req("/api/chats", cookie=K + "; millen_user=" + (m2.group(1) if m2 else ""),
              headers={"X-Forwarded-For": "1.2.3.4"})
check("owner PIN opens real chats remotely", b"title" in b)

print("== admin lockdown ==")
for p in ("/api/speak", "/api/model/download", "/api/open-logs",
          "/api/update/install", "/api/voice/prepare"):
    s, h, b = req(p, "POST", {}, cookie=K, headers={"X-Forwarded-For": "1.2.3.4"})
    check("remote blocked: " + p, b"owner only" in b)
s, h, b = req("/api/speak", "POST", {"stop": True}, cookie=K)
check("local speak allowed", b'"ok": true' in b)

print("== backdrop system ==")
s, h, b = req("/api/sky/cached", cookie=K)
cached = json.loads(b).get("cached", [])
check("cached list non-empty", len(cached) >= 1, str(cached))
if cached:
    i = cached[0]
    s, h, b = req(f"/api/sky/status?i={i}", cookie=K)
    check("cached clip reports ready", b'"ready"' in b)
    s, h, _ = req(f"/sky/{i}.mov", cookie=K, headers={"Range": "bytes=0-1023"})
    check("range serving 206", s == 206)
    s, h, _ = req(f"/sky/{i}.mov", cookie=K, headers={"Range": "bytes=-1024"})
    check("suffix range 206", s == 206)
s, h, b = req("/", cookie=K)
page = b.decode("utf-8", "replace")
check("SKY_N injected", re.search(r'parseInt\("\d+",10\)', page))
check("dark list injected", "darkSet=new Set(JSON.parse('[0, 3, 4" in page)

# THE VERSION FACTS, read once: the checks below assert that every
# surface agrees with the constants, never that the line is some
# particular number — pinning one is why three checks broke on a beta cut
_vsrc = dict(re.findall(r'^APP_(VERSION|BETA|RC) = "?([^"\n]+?)"?\s*(?:#.*)?$',
                        _MILLENAI_SRC, re.M))
_vraw = _vsrc.get("VERSION", "")
_vwant = _vraw[:-2] if (_vraw.count(".") >= 2 and _vraw.endswith(".0")) else _vraw
_vshown = (re.search(r'id="up-version">([^<]*)<', page) or
           re.match("(x)", "x")).group(1)

print("== page integrity ==")
leftovers = [t for t in re.findall(r"__[A-Z_]{3,}__", page)
             if t not in ("__MAIN__",)]
check("no unreplaced template tokens", not leftovers, str(leftovers[:5]))
check("no raw NUL bytes", b"\x00" not in b)
# 6.0b2: no in-app hero branding — greeting IS the hero (Claude-style);
# the only wordmark is the frame-wide sidebar header
# NB: ".h1row" survives as a dead CSS selector + haloTick query —
# assert the MARKUP is gone, not the substring
check("hero is greeting-only", '<p class="greet"' in page
      and 'class="h1row"' not in page)
# 6b255: 150 NYC greeting lines, condition-gated so a weather or
# time line never lands absurdly. The three landmines a naive filter
# hits, all guarded here: a wrapping hour range (23->2) must not be
# unreachable, h:[0,0] must not vanish under a falsy check, and a
# weekday line must carry an explicit `d` rather than prose.
check("greetings are condition-gated",
      "function greetOK" in page and "function greetPool" in page
      and "GREETINGS.filter" in page
      and "a<=b?(hr>=a&&hr<=b):(hr>=a||hr<=b)" in page   # wraparound
      and "h:[0,0]" in page                              # midnight kept
      and 'd:[5],h:[15,18]' in page)                     # Friday is real
# 6b269, per Patrick ("less NYC, but still fun... use the user's
# nickname... the user's location"): the bank is anywhere-on-earth,
# tokened with {name}/{city} (a line needing a token the app lacks is
# never drawn), and NO line asserts weather — vibes only.
check("no ungated weather-claim greetings",
      "Ninety degrees" not in page and "Hawk's out" not in page
      and "Slush season" not in page and "First snow" not in page
      and "umbrella's toast" not in page
      and "Summer, {name}. What's the move?" in page   # month vibe
      and "{name}" in page and "{city}" in page
      and "function greetHas" in page and "function greetFill" in page
      and "How's {city} tonight, {name}?" in page)
# 6.0b4 made the wordmark small; 6b264 moved the version out of the
# lockup; 6b285 (per Patrick, the VPN scheme) took it out of the main
# window altogether — it lives at the top of Settings → About only
check("corner wordmark clean, version out of the main window",
      "font-size:12.5px" in page
      and 'class="vsub"' not in page.split("</aside>")[0]
      and 'id="ver-foot"' not in page
      and 'id="up-version"' in page)
# 6b286, per Patrick (the macOS way): betas are numbered per version
# line from 1 — "6.1 beta 2" — never by build number ("beta 268")
_REL_SH = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "release.sh"), encoding="utf-8").read()
check("betas numbered per line, not by build",
      '" beta %d" % APP_BETA' in _MILLENAI_SRC
      and "APP_BUILD if APP_BETA" not in _MILLENAI_SRC
      and re.search(r"^APP_BETA = \d+$", _MILLENAI_SRC, re.M) is not None
      and 'SHOW="$SHOW beta $BETA"' in _REL_SH
      and 'if arg == "beta":' in _REL_SH and 'elif arg == "rc":' in _REL_SH
      and "APP_BETA = True" not in _REL_SH)
# 6b288, per Patrick ("major bug"): a provider's budget/quota notice sent
# as content is a FAILURE that falls to the next rung, never the answer
_ns = {"re": re}
_seg = re.search(r"_PROVIDER_ERR_RX = re\.compile\(.*?\n\n\ndef _cloud_budget_hit",
                 _MILLENAI_SRC, re.S)
exec(_seg.group(0).rsplit("\n\n\ndef _cloud_budget_hit", 1)[0], _ns) if _seg else None
_pe = _ns.get("_is_provider_error", lambda t: None)
check("provider error notice is a failure, not an answer",
      _pe("The API key used for this request has reached its budget. "
          "Please raise the key budget, then try again.\n\nTopping up the "
          "wallet does not raise this limit. If this isn\u2019t your "
          "Pollinations account, contact whoever runs the app or service "
          "you\u2019re using.") is True
      and _pe('{"error": {"message": "Rate limit exceeded"}}') is True
      and _pe("Production electric cars top out around 200 mph today: the "
              "Rimac Nevera has been clocked at 258 mph, Tesla's Model S "
              "Plaid at 200 mph, and Lucid's Air Sapphire at 205 mph. Most "
              "everyday EVs are limited to 100 to 130 mph to protect range "
              "and the battery, since drag rises with the square of speed "
              "and cooling limits sustained output.") is False
      and _MILLENAI_SRC.count("_is_provider_error(") >= 5
      and "_cloud_budget_hit(c)" in _MILLENAI_SRC
      and "head.append(tok)" in _MILLENAI_SRC)
# the post-update dialog reads THIS build's notes, and a nightly with a
# new commit counts as an update
try:
    _wn = json.loads(req("/api/update/whatsnew", cookie=K)[2])
except Exception:
    _wn = {}
check("what's new endpoint answers for this build",
      _wn.get("title", "").startswith(_vwant) and "notes" in _wn
      and 'fetch("/api/update/whatsnew")' in page
      and 'prefs.get("last_ident")' in _MILLENAI_SRC
      and "ident = short_version()" in _MILLENAI_SRC)
# 6b295, per Patrick: export to ~20 formats, routed from plain language,
# delivered in a download box. These run the REAL router and the REAL
# engines rather than asserting that strings exist.
_XNS = {"re": re, "html": __import__("html")}
try:
    _xseg = _MILLENAI_SRC[_MILLENAI_SRC.index("EXPORT_KEEP_N ="):
                          _MILLENAI_SRC.index("def _x_size(")]
    exec(_xseg, _XNS)
except Exception as _e:
    _XNS = {}
_xi = _XNS.get("export_intent", lambda *a, **k: None)
_XPOS = [("export this as a PDF", "pdf"), ("can I export a mermaid file of this", "mmd"),
         ("give me that as a spreadsheet", "xlsx"), ("turn this into slides", "pptx"),
         ("save the code above as run.py", "py"), ("convert this to markdown", "md"),
         ("Generate an Excel file of a budget", "xlsx"), ("download this as an ics file", "ics")]
_XNEG = ["what is a PDF", "how do I export from Excel", "explain the CSV format",
         "write a python script that makes a PDF", "respond in markdown please",
         "does this app support pdf export", "we use Next.js on the frontend",
         "the error is at Main.java line 40", "summarize the pdf i attached"]
check("export router: routes every format, vetoes the lookalikes",
      all((_xi(q, True) or {}).get("ext") == e for q, e in _XPOS)
      and all(_xi(q, True) is None for q in _XNEG))

_XSAMPLE = ("# Trip\n\nPick one.\n\n| City | Cost |\n| --- | --- |\n"
            "| Lisbon | 540 |\n| Tokyo | 890 |\n\n## Flow\n\nA -> B -> C\n\n"
            "```python\nx = 1\n```\n\nQ: Which?\nA: Lisbon.\n")
_xmade = {}
if _XNS:
    import tempfile as _tf
    for _e, _fn in (("csv", "ex_table"), ("md", "ex_text"), ("mmd", "ex_text"),
                    ("py", "ex_text"), ("anki", "ex_cards"), ("zip", "ex_archive")):
        try:
            _pp = os.path.join(_tf.gettempdir(), "gaunt_x." + _e)
            if _fn == "ex_table":
                _XNS[_fn](_XSAMPLE, _e, _pp)
            elif _fn in ("ex_cards", "ex_archive"):
                _XNS[_fn](_XSAMPLE, _e, _pp)
            else:
                _XNS[_fn](_XSAMPLE, _e, _pp)
            _xmade[_e] = os.path.getsize(_pp)
        except Exception as _ex:
            _xmade[_e] = str(_ex)[:60]
check("export engines: stdlib formats all write real files",
      all(isinstance(_xmade.get(e), int) and _xmade[e] > 0
          for e in ("csv", "md", "mmd", "py", "anki", "zip")), str(_xmade))
# a Mermaid export must be valid Mermaid, not whatever fence came first
_xmmd = ""
try:
    _xmmd = open(os.path.join(__import__("tempfile").gettempdir(),
                              "gaunt_x.mmd"), encoding="utf-8").read()
except Exception:
    pass
check("export: mermaid is real mermaid, with quoted labels",
      _xmmd.startswith("graph TD") and '"' in _xmmd and "-->" in _xmmd
      and "x = 1" not in _xmmd, _xmmd[:80])

# the download route: never the format's own MIME, always an attachment
_xst, _xhd, _xbody = req("/api/export/../../prefs.json", cookie=K)
_xst2, _, _ = req("/api/export/nope.csv", cookie=K)
check("export route: traversal and unknown ids are 404",
      _xst == 404 and _xst2 == 404, "%s/%s" % (_xst, _xst2))
check("export delivery: octet-stream, attachment, nosniff, per-identity",
      "application/octet-stream" in _MILLENAI_SRC
      and "X-Content-Type-Options" in _MILLENAI_SRC
      and "_x_disposition(nm)" in _MILLENAI_SRC
      and "export_dir(base)" in _MILLENAI_SRC
      and "self._data_base()" in _MILLENAI_SRC
      and "filename*=UTF-8''" in _MILLENAI_SRC)
# the box, and the token that must never reach a clipboard
check("download box: placeholder-safe, stripped from copy and speak",
      "function dlBox(" in page and 'class="dlbox"' in page
      and "\\u0000DL" in page and "function stripTokens(" in page
      and "msgActions(aiDiv,\"assistant\",stripTokens(full))" in page
      and "stripTokens(full)})" in page
      and "ALLOW_DOWNLOADS" in _MILLENAI_SRC
      and "/api/export/reveal" in _MILLENAI_SRC)
# 6b294, per Patrick: image generation — local FLUX first, cloud after;
# the intent is caught before the web search; the extra sits under the
# presets and in the wizard; the update pill is an arrow the size of its
# neighbours
_ins = {"re": re}
_iseg = _MILLENAI_SRC[_MILLENAI_SRC.index("_IMG_VERBS = "):_MILLENAI_SRC.index("def image_supported")]
exec(_iseg, _ins)
_ii = _ins["image_intent"]
try:
    _ist = json.loads(req("/api/setup", cookie=K)[2]).get("image") or {}
except Exception:
    _ist = {}
# 6b304: the leak hunt. Each check is the regression test for a finding
# the verifiers confirmed, and runs the real code rather than grepping it.
_LH = {"re": re, "os": os, "sys": sys, "time": time, "json": json,
       "html": __import__("html"), "glob": __import__("glob"),
       "shutil": __import__("shutil"), "secrets": __import__("secrets"),
       "threading": __import__("threading"), "tempfile": __import__("tempfile"),
       "contextlib": __import__("contextlib"),
       "subprocess": __import__("subprocess"), "app_dir": lambda: "/tmp"}
check("compiles with SyntaxWarning as an error (the THIN LIST crash)",
      __import__("subprocess").run(
          [sys.executable, "-W", "error::SyntaxWarning", "-c",
           "compile(open(%r,encoding='utf-8').read(),'m','exec')"
           % os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "millenai.py")],
          capture_output=True).returncode == 0)
_t0 = time.time(); req("/api/setup", cookie=K); req("/api/setup", cookie=K)
_setup_s = (time.time() - _t0) / 2
_b = json.loads(req("/api/setup/busy", cookie=K)[2])
check("the hot status endpoint is cheap and the idle strip cheaper",
      _setup_s < 1.0 and _b == {"busy": False}
      and 'python", "-c", "import' not in _MILLENAI_SRC
      and "def _studio_bytes_uncached" in _MILLENAI_SRC
      and "if(setupInFlight)return;" in page and "dlStripBusy" in page,
      "%.2fs" % _setup_s)
# cloud.json: four threads resting four providers at once must never
# lose a key or leave the file unreadable (it did in 71 of 500 rounds)
_cd = _LH["tempfile"].mkdtemp(); _cf = os.path.join(_cd, "cloud.json")
_cns = dict(_LH, CLOUD_FILE=_cf, QUOTA_COOLDOWN=600.0)
import ast as _ast
_ctree = _ast.parse(_MILLENAI_SRC)
exec(_MILLENAI_SRC[_MILLENAI_SRC.index("try:\n    import fcntl as _fcntl"):
                   _MILLENAI_SRC.index("def _cloud_save_state(")], _cns)
for _n in _ctree.body:
    if isinstance(_n, _ast.FunctionDef) and _n.name in (
            "_cloud_all", "_cloud_save_state", "cloud_cool"):
        exec(_ast.get_source_segment(_MILLENAI_SRC, _n), _cns)
_bad = 0
for _r in range(60):
    json.dump({"providers": {p: {"key": "K" + p, "status": "ok"}
                             for p in "abcd"}, "active": "a"}, open(_cf, "w"))
    _bar = _cns["threading"].Barrier(4)
    def _w(p):
        _bar.wait(); _cns["cloud_cool"](p, "rest", 60)
    _ts = [_cns["threading"].Thread(target=_w, args=(p,)) for p in "abcd"]
    [x.start() for x in _ts]; [x.join() for x in _ts]
    try:
        if len([1 for v in json.load(open(_cf))["providers"].values()
                if v.get("key")]) < 4:
            _bad += 1
    except Exception:
        _bad += 1
check("cloud.json survives concurrent writers with every key intact",
      _bad == 0 and "def _cloud_write" in _MILLENAI_SRC
      and 'with open(CLOUD_FILE, "w")' not in _MILLENAI_SRC, "%d bad rounds" % _bad)
_xns2 = dict(_LH, _home_tz=lambda: ("America/New_York", "Boston"))
exec(_MILLENAI_SRC[_MILLENAI_SRC.index("_X_FENCE = re.compile("):
                   _MILLENAI_SRC.index("def ex_cards(")], _xns2)
_icp = os.path.join(_cd, "t.ics")
try:
    _xns2["ex_calendar"]("- 2026-09-22 10:00 Standup\n", "ics", _icp, "")
    _ics = open(_icp).read()
except Exception as _e:
    _ics = "ERR " + str(_e)
check("a timed calendar export works (the tz tuple crash)",
      "DTSTART:20260922T100000" in _ics and "TZID" not in _ics, _ics[:60])
check("renders are serialized and stop when the reader leaves",
      "_render_lock = threading.Lock()" in _MILLENAI_SRC
      and "def _run_render" in _MILLENAI_SRC and "os.killpg" in _MILLENAI_SRC
      and "start_new_session=True" in _MILLENAI_SRC
      and "sock=self.connection" in _MILLENAI_SRC
      and "def _sweep_hf_carcasses" in _MILLENAI_SRC
      and "_no_text_model" in _MILLENAI_SRC)
# 6b303, per Patrick: a gear per studio for defaults, and one-shot
# quality overrides in chat that never touch those defaults.
_GNS = {"re": re, "os": os, "sys": sys, "time": time, "json": json,
        "html": __import__("html"), "glob": __import__("glob"),
        "shutil": __import__("shutil"), "secrets": __import__("secrets"),
        "threading": __import__("threading"),
        "subprocess": __import__("subprocess")}
_GNS.update(
    studio_tier=lambda k, t="": {
        "image": {"w": 1024, "h": 1024, "steps": 4, "base": "schnell"},
        "video": {"w": 640, "h": 384, "steps": 20, "frames": 33}}[k],
    _snap_dir=lambda r: "/nonexistent",
    load_prefs=lambda b=None: {}, store_prefs=lambda d, b=None: None)
try:
    # ONE dict for globals and locals: split them and the module's own
    # helpers resolve against the wrong namespace
    exec(_MILLENAI_SRC[_MILLENAI_SRC.index("STUDIO_RANGE = {"):
                       _MILLENAI_SRC.index("def _studio_engine_ok")], _GNS)
except Exception:
    pass
_go = _GNS.get("gen_overrides")
_prevr = {"w": 1024, "h": 1024, "steps": 4, "frames": 33}
def _ovr(t, key="image", loose=True):
    return _go(t, key, loose, _prevr) if _go else ({}, t, [])
check("chat overrides: parsed, stripped, and relative to the last render",
      _ovr("regenerate this, but double the resolution")[0] == {"w": 2048, "h": 2048}
      and _ovr("make the piano white and double the resolution")[1]
          == "make the piano white"
      and _ovr("same but at 4K")[0] == {"w": 3840, "h": 2160}
      and _ovr("do that again at 1920x1080")[0] == {"w": 1920, "h": 1080}
      and _ovr("higher quality")[0] == {"_effort": 1}
      and _ovr("as a gif", "video")[0] == {"fmt": "gif"}
      # a fresh commission must keep its subject words
      and _ovr("draw a portrait of a woman", "image", False)[0] == {}
      and _ovr("a 4k webcam on a desk", "image", False)[0] == {}
      and _ovr("make a faster car", "image", False)[0] == {}
      and "def gen_overrides" in _MILLENAI_SRC)
_so = _GNS.get("studio_opts")
check("studio settings are clamped, snapped and never raise",
      _so and _so("image", {"w": 99999, "h": 99999})["w"] == 2048
      and _so("image", {"steps": "abc"})["steps"] == 4
      and _so("video", {"frames": 500})["frames"] == 97
      and (_so("video", {"frames": 30})["frames"] - 1) % 4 == 0
      and _so("video", {"w": 4096, "h": 4096})["w"] * 
          _so("video", {"w": 4096, "h": 4096})["h"] <= 901120
      and "def _snap_frames" in _MILLENAI_SRC
      and "def _fit_area" in _MILLENAI_SRC)
try:
    _gs = json.loads(req("/api/setup", cookie=K)[2])["studios"]["image"]
except Exception:
    _gs = {}
check("the gear dialog exists and offers only live controls",
      'class="stgear"' in page and 'id="gear-veil"' in page
      and "function openGear" in page and 'id="gear-reset"' in page
      and set(_gs.get("formats") or []) == {"png", "jpg", "webp"}
      and "opts" in _gs and "ranges" in _gs
      # both confirmed dead by running the engine: mflux warns that
      # --negative-prompt is ignored, and never reads --lora-style
      and "lora-style" not in page and "lora_style" not in _MILLENAI_SRC
      and '"--negative-prompt", o["neg"]' in _MILLENAI_SRC
      and _MILLENAI_SRC.count('"--negative-prompt"') == 1)
# 6b299/6b300/6b301, per Patrick: video alongside image, a colour-coded
# size ladder, both removable, and intent that understands "make the
# piano white" without being told the word "generate".
_SNS = {"re": re, "html": __import__("html"), "os": os, "sys": sys,
        "time": time, "secrets": __import__("secrets"), "json": json,
        "threading": __import__("threading"),
        "subprocess": __import__("subprocess"), "app_dir": lambda: "/tmp"}
try:
    exec(_MILLENAI_SRC[_MILLENAI_SRC.index("EXPORT_KEEP_N ="):
                       _MILLENAI_SRC.index("def _x_size(")], _SNS)
    exec(_MILLENAI_SRC[_MILLENAI_SRC.index("_IMG_VERBS = "):
                       _MILLENAI_SRC.index("def image_supported()")], _SNS)
    exec(_MILLENAI_SRC[_MILLENAI_SRC.index("_VID_VERBS = "):
                       _MILLENAI_SRC.index("def video_ready()")], _SNS)
except Exception:
    pass
_fu = _SNS.get("image_followup", lambda *a: None)
_wf = _SNS.get("image_wants_fetch", lambda *a: False)
_vi = _SNS.get("video_intent", lambda *a: None)
check("a picture that exists is fetched, one that doesn't is painted",
      _wf("I would like to see a picture of a cookie from the Internet")
      and _wf("find me a photo of a red panda")
      and _wf("show me a real photo of Saturn")
      and not _wf("create me a picture of a cookie")
      and not _wf("draw me a red panda"))
check("a follow-up refines the picture just made",
      _fu("make the piano white", "a piano") == "a white piano"
      and _fu("make it blue", "a piano") == "a blue piano"
      and _fu("redo it", "a piano") == "a piano"
      and _fu("what is a piano", "a piano") is None
      and _fu("how do pianos work", "a piano") is None
      and _fu("tell me about jazz history", "a piano") is None
      and _fu("generate an image of a cat", "a piano") is None
      and _fu("export this as a PDF", "a piano") is None
      and "def _refine_with_model" in _MILLENAI_SRC)
check("video generation: intent, engine, cloud fallback, inline player",
      _vi("make a video of a cat") == "a cat"
      and _vi("create an animation of a rocket launch") == "a rocket launch"
      and _vi("what is a video codec") is None
      and "def generate_video" in _MILLENAI_SRC
      and "mlx_video.models.wan_2.generate" in _MILLENAI_SRC
      and "predictLongRunning" in _MILLENAI_SRC
      and 'class="genvid"' in page and "/api/video/" in _MILLENAI_SRC)
try:
    _ss = json.loads(req("/api/setup", cookie=K)[2]).get("studios") or {}
except Exception:
    _ss = {}
check("both studios expose a colour-coded ladder",
      set(_ss) == {"image", "video"}
      and all(len(v.get("tiers") or []) >= 3 for v in _ss.values())
      and all(t["fit"] in ("green", "amber", "red")
              for v in _ss.values() for t in v["tiers"])
      and all({"gb", "mem_gb", "note", "have"} <= set(t)
              for v in _ss.values() for t in v["tiers"])
      and "def tier_fit" in _MILLENAI_SRC
      and "def studio_remove" in _MILLENAI_SRC
      and 'class="stnotch ' in page and "stlegend" in page
      and "green \\u2014 runs comfortably here" in page,
      str({k: [t["fit"] for t in v.get("tiers", [])] for k, v in _ss.items()}))
check("download estimate uses a rolling window, not two polls",
      "_dl_hist = []" in _MILLENAI_SRC
      and "now - _dl_hist[-1][0] >= 2.0" in _MILLENAI_SRC
      and "if dt < 4.0 or db <= 0:" in _MILLENAI_SRC
      and "bps > 2e5" in _MILLENAI_SRC)
check("no brand or model name above an answer",
      ".msg.ai .who{display:none}" in page
      and "FLUX" not in page and "schnell" not in page)
# 6b296, per Patrick ("image generation doesn't seem to be working"):
# a conversational lead-in must not change what the request IS. Both
# matchers were ^-anchored on the verb, so "try again, generate an image
# of a cat" fell through to the chat path and the model answered with a
# hallucinated tool call.
_IMPOS = ["try again, generate an image of a cat", "now generate an image of a cat",
          "actually, draw me a cat", "great. now paint a sunset",
          "ok try again, generate an image of a cat",
          "one more time - generate an image of a cat",
          "please try again and draw a red bicycle"]
_IMNEG = ["try again, but explain it simply", "now explain what a picture element is",
          "how do I draw a circle in CSS", "generate a list of image formats",
          "what is an image sensor"]
check("image intent survives a conversational lead-in",
      all(_ii(q) for q in _IMPOS) and all(_ii(q) is None for q in _IMNEG)
      and "def _img_strip_pre" in _MILLENAI_SRC)
check("models are told they have no tools",
      "You have NO tools and NO function calling" in _MILLENAI_SRC
      and "dalle" in _MILLENAI_SRC)
check("image generation: intent, engine ladder, settings box, wizard, arrow pill",
      _ii("Generate an image of a cat.") == "a cat"
      and _ii("draw me a red bicycle") == "a red bicycle"
      and _ii("make a logo for a coffee shop") == "a logo for a coffee shop"
      and _ii("what is an image sensor") is None
      and _ii("generate a list of image formats") is None
      and "def generate_image" in _MILLENAI_SRC
      and "mflux-generate" in _MILLENAI_SRC and "image.pollinations.ai" in _MILLENAI_SRC
      and "gemini-2.5-flash-image" in _MILLENAI_SRC
      and '"/api/image/install",' in _MILLENAI_SRC
      and set(_ist.keys()) >= {"supported", "ready", "gb", "status", "pct"}
      and 'id="studio-row"' in page and 'id="wiz-img"' in page
      and 'id="wiz-vid"' in page and "/api/studio/remove" in _MILLENAI_SRC
      and "def studio_remove" in _MILLENAI_SRC
      and "def _dir_bytes_real" in _MILLENAI_SRC
      and 'class="genimg"' in page and "function paintStudios" in page
      and ">UPDATE<" not in page
      and '<div id="update-flag" hidden title="Install the update"><svg' in page)
# 6b292, per Patrick: performance mode is now "Enable visual effects" in
# About and touches ONLY the backdrop; the cog sits left of the pen;
# automatic update checks are a switch (launch + daily) with the button
# as the manual path
check("visual effects switch: backdrop only, in About; cog left of pen",
      'id="fx-toggle"' in page and 'id="perf-toggle"' not in page
      and "body.perf" not in page and "body:not(.perf)" not in page
      and "body.novideo #skyline{display:none}" in page
      and page.count("body.novideo ") == 3
      and 0 < page.index('id="settings-btn"') < page.index('id="newchat"')
      and 'id="settings">' not in page
      and "Toggle visual effects" in page)
_auto = json.loads(req("/api/update/check", cookie=K)[2])
check("automatic update checks: switch, daily cadence, server gate",
      'id="autochk-toggle"' in page and "auto_update_check" in page
      and "checkUpdate();},86400000)" in page
      and '"auto_update_check") is False' in _MILLENAI_SRC
      and "Automatic checks are off" in page
      and isinstance(_auto, dict) and "configured" in _auto)
# 6b291, per Patrick ("here we go again"): ONE download indicator — the
# pill hides while the strip speaks; no gradient hack, no second poller
check("one download indicator: the strip alone while busy",
      "DOWNLOADING MODELS" not in page
      and "linear-gradient(90deg,#e26d5a" not in page
      and '"downloading models \\u00b7 "' in page
      # 6b304: the strip reads the cheap busy endpoint and hides the pill
      # itself; it no longer re-reads the full status to drive it
      and 'fetch("/api/setup/busy")' in page)
# 6b290, per Patrick ("sitting at 100% doing nothing… idiot proof it"):
# the bar counts the batch in play, MLX runs two at a time and is judged
# by bytes, the pane reports live, and the preset on disk is marked
try:
    _st = json.loads(req("/api/setup", cookie=K)[2])
except Exception:
    _st = {}
check("model downloads: batch-true progress, live pane, current preset",
      "def _batch_labels" in _MILLENAI_SRC
      and "for label in _batch_labels():" in _MILLENAI_SRC
      and "_MLX_GATE = threading.Semaphore(2)" in _MILLENAI_SRC
      and "with _MLX_GATE:" in _MILLENAI_SRC
      and "// 1_000_000" in _MILLENAI_SRC
      and set((_st.get("plan_state") or {}).keys()) == {"min", "rec", "full", "all"}
      and all(v in ("current", "installed", "partial", "none")
              for v in (_st.get("plan_state") or {}).values())
      and "now" in _st and "queued_n" in _st
      and "function manageTick" in page and "function nowLine" in page
      and 'class="cur"' in page and "already installed \\u2014 nothing to download" in page
      and "watch the strip in the sidebar" not in page.split("#roster\").addEventListener")[0])
# 6b289, per Patrick: the rail's VERSION cell never ellipsizes a nightly —
# the word "nightly" goes, the commit stays ("6.0.4 · e6fb576")
_ns2 = {"os": os, "re": re, "APP_VERSION": "6.0.4", "APP_NIGHTLY": "3 e6fb576", "APP_RC": 0,
        "APP_BETA": 0}
_sv = re.search(r"def short_version\(.*?def spec_version\(.*?"
                r"return short_version\(\)\.replace\(\" nightly \", \" \\u00b7 \"\)\n",
                _MILLENAI_SRC, re.S)
exec(_sv.group(0), _ns2) if _sv else None
check("rail version cell: a nightly reads '<ver> \u00b7 <sha>', no ellipsis",
      _ns2.get("spec_version", lambda: "")() == "6.0.4 \u00b7 e6fb576"
      and re.search(r'id="about-ver">' + re.escape(_vwant), page) is not None
      and " nightly " not in re.search(r'id="about-ver">([^<]*)<',
                                       page).group(1)
      and "__APP_VER_SPEC__" not in page)
# 6b287, per Patrick: the disk image's window title and the line under
# the mark carry the app's own label (nightly + commit, beta N, RC), and
# the wordmark is set in the bundled Michroma, not a Helvetica stand-in
_DMG_SH = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "build_dmg.sh"), encoding="utf-8").read()
check("dmg window: true label, wordmark in Michroma",
      'VOL="ConcordeAI $LABEL"' in _DMG_SH
      and "APP_NIGHTLY" in _DMG_SH and "APP_BETA" in _DMG_SH
      and 'MICHROMA = "fonts/Michroma-Regular.ttf"' in _DMG_SH
      and "Helvetica.ttc\", FS)" not in _DMG_SH
      and 'DMGFILE="ConcordeAI-$VER.dmg"' in _DMG_SH)
# 6b285: three update channels replace the beta checkbox
check("update channel picker: stable / beta / nightly",
      'id="upchan"' in page and 'value="nightly"' in page
      and "update_channel" in page and 'id="betaup"' not in page
      and "def update_channel" in _MILLENAI_SRC
      and 'APP_NIGHTLY = ""' in _MILLENAI_SRC
      and 'x.get("tag_name") == "nightly"' in _MILLENAI_SRC
      and os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      ".github", "workflows", "nightly.yml")))
# 6.0b7: engine dropdown at the chip, Hermes agent, 300px rail
check("engine dropdown js + meta", "openEngMenu" in page
      and '"Fast"' in page and "engrow" in page)
# 6b209: agents UI pulled until the logistics are sorted — two tabs
# only, no specialist list; the machinery stays dormant (AGENT_META
# still feeds the Code tab's popups, Hermes waits inside it)
check("agents tab pulled, machinery dormant",
      'data-m="agents"' not in page and 'id="agents-wrap"' not in page
      and "showAgentPop" in page and '"Hermes"' in page)
# b228: three tabs again (Chat | Code | Funnels) — thirds glide
check("three-tab glide in thirds", "width:calc(33.334% - 2px)" in page
      and "translateX(200%)" in page)
check("funnels tab present", 'data-m="funnel"' in page
      and 'id="fn-goal"' in page and 'id="fn-stages"' in page)
# 6b253: the funnel lane gets DECISIONS, not questions — 190 rotating
# across 10 themed groups plus 10 "stuck" prompts surfaced persistently
# (the escape hatch for a decision that's on no list)
check("funnel decision chips + persistent stuck chip",
      "const FUNNEL_SETS=[" in page and "const FUNNEL_STUCK=[" in page
      and "startFunnel" in page and 'id="fnl-stuck"' in page
      and ".sugg.stuck{" in page
      and page.count("FUNNEL_SETS") >= 2)
# tender decisions shift the funnel from narrowing to SUPPORTING, and
# it keys off the GOAL TEXT so a typed decision gets the same care as a
# clicked chip
check("funnel care mode for tender decisions",
      "_TENDER_RX" in _MILLENAI_SRC and "FUNNEL_CARE" in _MILLENAI_SRC
      and "def funnel_sys_for" in _MILLENAI_SRC
      # 6b260: the SUMMARY got its own voice — one stage-prompt call
      # site remains, and the verdict helper carries care mode too
      and _MILLENAI_SRC.count("funnel_sys_for(goal)") == 1
      and _MILLENAI_SRC.count("funnel_summary_sys_for(goal)") == 1
      and "FUNNEL_SUMMARY_SYS" in _MILLENAI_SRC
      and "FUNNEL_SYS}," not in _MILLENAI_SRC)
# 6b260, per Patrick: the funnel verdict must never parrot the picks
# back ("something strawberry, frozen, with sprinkles"), the web voice
# must never shrink to sources-only hedging, and supermarket queries
# reach OSM's shop= tag for real open-now hours
check("funnel verdict is a verdict, not an echo",
      '"\\n".join(picks)' not in _MILLENAI_SRC
      and "NAME the specific thing" in _MILLENAI_SRC
      and "couldn't reach a model to weigh" in _MILLENAI_SRC
      and "MATERIALLY narrows" in _MILLENAI_SRC)
# 6b265, cycle 10 of the drill: "coffee shops near williamsburg"
# answered from VIRGINIA (generic venue nouns poisoned the locality),
# the movies question punted with zero titles, and funnel verdicts
# invented a B&B and overrode a clicked pick
check("cycle-10 fixes: venue nouns, listings commit, grounded verdicts",
      "shops?|stores?|joints?|venues?" in _MILLENAI_SRC
      and "_LISTINGS_RX" in _MILLENAI_SRC
      and "This is a what's-on question" in _MILLENAI_SRC
      and "Picks are binding" in _MILLENAI_SRC)
check("web answers blend sources with real knowledge",
      "drawing on BOTH" in _MILLENAI_SRC
      # scoped to RESEARCH_WRITE's old wording — the live-data/weather
      # prompt is legitimately source-bound and keeps its own "ONLY"
      and "using ONLY the numbered sources" not in _MILLENAI_SRC
      and "ADD the best-known real ones" in _MILLENAI_SRC)
# 6b262: nwr, not node — chains are mapped as building WAYS, and the
# node-only query returned zero pharmacies for all of Bushwick. Also
# guard the prefix stubs: \bpharmac\b can never match "pharmacy" (the
# trailing boundary), so those categories never had OSM data at all.
check("supermarkets reach OSM shop tag",
      "supermarket|convenience" in _MILLENAI_SRC
      and 'nwr["shop"~' in _MILLENAI_SRC
      and 'nwr["amenity"~' in _MILLENAI_SRC
      and "out center body" in _MILLENAI_SRC
      and "pharmac\\w*" in _MILLENAI_SRC)
# 6b260, seen live: a sixty-word message about a friend, money and
# maybe-booking a hotel was shredded into a fake venue name and
# answered with the not-found script ("...Sitting Down in Som" — the
# 80-char cap cutting "somewhere" mid-word). Three layers now: the
# lookup classifier only fires on short lookup-shaped queries, a
# prose-length "entity" is never dictated as a venue name, and the
# terms cap cuts between words.
# (6b261 tightened the entity guard 6 -> 4 terms and added the
# existence gate after "is there a supermarket that sells msg" slipped
# through at exactly six)
check("venue lookup can't eat a conversation",
      "len(query.split()) <= 14" in _MILLENAI_SRC
      and "len(pt) > 4" in _MILLENAI_SRC
      and '(is|are)\\s+there' in _MILLENAI_SRC
      and 'out[:80].rsplit(" ", 1)[0]' in _MILLENAI_SRC)
# the picker used to scroll sideways: 1fr columns won't shrink below
# max-content, and .tn is nowrap. minmax(0,1fr) is the fix.
check("task picker: no horizontal scroll",
      "grid-template-columns:repeat(2,minmax(0,1fr))" in page
      and "#task-card{width:940px" in page)
check("sidebar defaults to 300px", "width:300px;min-width:300px" in page)
# 6.0b206: rich answers — flow diagrams, code cards, highlighter
check("flow diagram renderer", "flowDiagram" in page and "wireFlow" in page
      and "fwires" in page)
check("code cards + mini highlighter", "codecard" in page
      and "hilite" in page and "hkw" in page)
# 6b244: every code card carries a copy button that is greyed (.wait,
# disabled) while its fence is still open and live once it closes
check("code-card copy button, greyed until the fence closes",
      'class="ccopy wait" disabled' in page and ".ccopy.wait" in page
      and "ccopy" in page and "Still generating" in page)
# 6b243: the burger was DEAD on phones — a 760px block set the sidebar
# display:none while the 700px drawer block only animated transform, so
# the ☰ toggled a class on an element that was never rendered. ONE
# breakpoint now; this guards the second one from creeping back.
# 6b250: the Code tab's task library, the rail/pane picker, interactive
# [[FORM]] cards, and batched approvals in the remote loop
check("server task library + picker present",
      "const TASKS=[" in page and 'id="task-veil"' in page
      and 'id="task-cats"' in page and "openTaskPicker" in page
      and "Harden this system" in page and "startTask" in page)
check("interactive form cards wired",
      "function formCard" in page and "[[FORM]]" in page
      and ".qopt" in page and "TASK_GUIDE" not in page)  # server-side only
# 6b250: risky tasks carry a grey ⚠ and gate behind an explaining card
# with two ways out; the lockout safeguard is taught to the agent itself
check("risky tasks gated by a warning card",
      "function riskCard" in page and "riskcard" in page
      and "challenging to undo" in page
      and "Let\u2019s go for it" in page and "Not today" in page
      and 'class="twarn"' in page)
check("full 53-task library with the flagged set",
      page.count("{n:\"") == 53 and page.count("w:\"") == 22)
# 6b251: the prereq card is GONE on purpose — the execution engine needs
# nothing installed on the server (systemd-run is already there, reboot
# survival is Concorde-side polling), so asking the user to install
# anything would have been a lie. Guard it from creeping back.
check("no prereq card — the engine is zero-install",
      "prereqCard" not in page and "concorde-resume" not in page
      and 'req:["reboot"' not in page
      and "systemd-run" in _MILLENAI_SRC
      and "ssh_wait_back" in _MILLENAI_SRC)
# 6b249: the Remote SSH agent — autonomy throttle, connection bar, and
# the live approval card in the stream
check("remote agent UI present",
      'id="remote-bar"' in page and 'id="autonomy-seg"' in page
      and 'data-a="manual"' in page and 'data-a="full"' in page
      and "showApprove" in page and 'data-agent="Remote"' in page)
# 6b249: the command safety classifier — the real gate behind the
# autonomy levels. Verified over the wire against the running server so
# a regression in _DANGER_RX/classify_cmd can never ship silently.
s, h, b = req("/api/remote/classify?cmd=" + _uq("rm -rf /"), cookie=K)
if s == 200:
    def _cls(c):
        s2, h2, b2 = req("/api/remote/classify?cmd=" + _uq(c), cookie=K)
        return json.loads(b2).get("risk")
    check("classifier: catastrophic commands are 'danger'",
          _cls("rm -rf /") == "danger" and _cls("mkfs.ext4 /dev/sda1")
          == "danger" and _cls("reboot") == "danger"
          and _cls("apt update && reboot") == "danger")
    check("classifier: reads and writes separate correctly",
          _cls("ls -la /etc") == "read"
          and _cls("systemctl status nginx") == "read"
          and _cls("apt-get install -y nginx") == "write"
          and _cls("ufw allow 51820/udp") == "write")
    # 6b250: recon lines with 2>/dev/null must stay 'read' or Auto mode
    # pauses on pure inspection (caught in the first live droplet run)
    check("classifier: recon with 2>/dev/null stays read",
          _cls("lsb_release -a 2>/dev/null; ip a; cat /etc/os-release")
          == "read" and _cls("wg show wg0") == "read"
          and _cls("wg genkey | tee k") == "write")
    # 6b250: a BATCH is priced at its riskiest member, never averaged
    _RANK = ["read", "write", "danger"]
    check("batch risk aggregates to its riskiest step",
          max(_RANK.index(_cls(c))
              for c in ("ls -la", "apt install -y nginx")) == 1
          and max(_RANK.index(_cls(c))
                  for c in ("ls -la", "reboot")) == 2)

# 6b255: the long-job engine, hardened by a live agent-driven run.
# Four bugs the run exposed, each guarded here because each one made a
# SUCCESSFUL job look like a failure (or ran it twice):
check("long-job engine: no double-run, honest exit codes",
      "--no-block" in _MILLENAI_SRC          # systemd-run blocks on oneshot
      and "__LIVE__" in _MILLENAI_SRC        # don't re-run an already-started job
      and "( %s ) > %s 2>&1; echo $? > %s" in _MILLENAI_SRC  # subshell, not brace
      and "__DONE__" in _MILLENAI_SRC)       # exit code from a file, not systemd
check("thinking budget and rate-limit backoff",
      'stop_reason") == "max_tokens"' in _MILLENAI_SRC
      and "budget=8000 + 6000 * _try" in _MILLENAI_SRC
      and "(4, 12, 25, 40)[_try]" in _MILLENAI_SRC)
# 6b248: the Advanced council — menu row behind a divider, the picker
# veil, per-model use-lines, and the compositor dropdown with guidance
check("advanced council picker present",
      '"__adv__"' in page and 'class="engdiv"' in page
      and 'id="adv-veil"' in page and 'id="adv-comp"' in page
      and "who holds the pen" in page and "ADV_USE" in page)
# 6b247: the four-step first-run wizard — markup, all four steps, the
# once-only gate, and the plan/provider machinery it drives
check("first-run wizard present, gated on wizard_done",
      'id="wiz-veil"' in page and page.count('class="wstep"') == 4
      and "wizard_done" in page and "openWizard" in page
      and "platform.moonshot.ai" in page and "aistudio.google.com" in page)
check("mobile drawer present and openable",
      'id="mburger"' in page and "body.sbopen #sidebar" in page
      and page.count("max-width:760px") == 1
      and "max-width:700px" not in page
      and "#sidebar{display:none}" not in page)
# 6b242: ONE mode picker. The sidebar's copy of the tier list is gone —
# the composer's engine pill is the only place modes are chosen, so guard
# both halves: the picker is there, and the duplicate has not crept back.
check("composer engine picker is the only mode selector",
      "openEngMenu" in page and 'id="model-chip"' in page
      and 'id="tier-rows"' not in page and 'class="tier"' not in page)
# 6b242: voice chat parked. The button greys out, the click is inert, and
# a machine that had it ON must not keep talking after the update — so the
# stale localStorage flag has to be cleared at boot, not just ignored.
check("voice chat parked, and stale flag cleared",
      "VOICE_PARKED=true" in page and "parked" in page
      and 'localStorage.setItem("millen.voice","0")' in page)
check("arena removed", "arena" not in page.lower())
check("blend progress bar css", ".blendprog" in page)
# 6b253: ONE progress aesthetic everywhere — thin, SHARP-cornered, and
# a fill that breathes. The old sideways shimmer is retired; assert its
# keyframe is gone so a bar can't quietly go back to sweeping.
check("progress bars: sharp + breathing, no shimmer",
      "@keyframes barBreathe" in page
      and "skyshimmer" not in page
      and "animation:barBreathe" in page)
check("serene entrance css", "heroIn 2.6s" in page and "shockOut" not in page)
# 5.2 surface (agents tab pulled again in 6b209 — two tabs is correct)
check("tab selector with Code lane", 'data-m="code"' in page
      and 'data-m="ai"' in page)
check("code tab carries Coding + Workspace",
      'data-agent="Coding"' in page and 'data-agent="Workspace"' in page)
check("query pinwheel css", ".wtspin" in page and "wtspin 1.5s" in page)
# 6b214: the LFG moment is fully retired — no element, no wash, no
# splash line, and the boot (cube wave + reveal) runs without it
check("LFG removed entirely",
      "lfg" not in page.lower() and "fucking" not in page.lower())
check("backdrop pantry js", "millen.skynext" in page
      and "fillPantry" in page and "PANTRY=5" in page)
# 5.3.2 surface: lane-aware sidebar + iconed tabs, AI renamed Chat
check("lane-aware sidebar js", "laneOK" in page and ".cempty" in page
      and "switchLane" in page)
check("tabs are iconed and AI reads Chat",
      page.count("#mode-tabs .ltab svg") >= 1 and ">Chat</span>" in page
      and ">AI</span>" not in page)
# 5.3.3: reveal masks must be dropped once the flourish lands, or a
# stalled transition leaves a permanent seam ("weird edge thing")
check("post-flourish mask teardown css+js",
      "paintdone" in page and "mask-image:none!important" in page)
# 5.3.5: the halo is CANVAS pixels now — live CSS blur on it raster-
# clipped in Blink and misrendered in WebKit (the seam, three ways)
check("canvas halo replaces the filtered one",
      "haloTick" in page and "halo-cv" in page
      and ".halo{display:none}" in page)
check("pantry rotates a fresh clip per session",
      "THE SHELF ROTATES" in page)
# 5.3.6: a stocked pantry overrides the first-run dark-set preference —
# private-mode WKWebView wiped localStorage every launch until now
check("veteran pantry overrides first-run dark set",
      "stocked pantry is proof" in page)
# 6b257: the brand is ConcordeAI on every user-facing surface; the old
# names survive only in internals (paths, bundle id, cookies) which
# never reach the page. Bare "Concorde" is a stray now too — the only
# place it stands alone is a lockup, where a nested <b> splits the AI
# off ("Concorde<b>AI"); lowercase tokens (concorde-resume,
# concorde-job) are internals and don't trip the case-sensitive regex.
check("ConcordeAI brand, no stray MillenAI or bare Concorde",
      "ConcordeAI" in page and "MillenAI" not in page
      and not re.search(r"Concorde(?!(?:<b>)?AI)", page))
# 6b257: every lockup sets the AI in BOLD inside the quiet 400-weight
# mark — a nested <b> (a span would trip the ">AI</span>" tab guard
# above), bolded by one shared rule across all three lockups
check("wordmark splits ConcordeAI with a bold AI",
      "Concorde<b>AI</b></b>" in page
      and "#wiz-brand b b{" in page)
# 6b258, per Patrick: EXTRA extra bold, the same recipe ConcordeVPN
# uses for its own second word. Michroma ships ONE weight, so a
# synthetic 700 barely moves — 800 plus a hair of text-stroke actually
# fattens the outline. The doors clip a gradient to the text, so their
# fill is transparent and a currentColor stroke would draw nothing:
# there the AI takes a solid silver of its own.
# 6b268 (per Patrick, "the bottom one not so much"): the boldness is
# the STROKE alone — synthetic 800 was WebKit double-striking the AI
# sideways, fattening it differently than the titlebar's pure
# AppKit stroke. One recipe now: regular weight + .12em.
check("AI is extra-extra bold in every wordmark",
      "font-weight:400;-webkit-text-stroke:.12em currentColor" in page
      and "font-weight:800;-webkit-text-stroke" not in page
      # the superseded synthetic-700 wordmark rule must not linger
      and "#wiz-brand b b{font-weight:700}" not in page
      # 6b264, per Patrick ("the font height is different"): the
      # centered stroke grows the caps, so the AI is scale-compensated
      # to sit flush with CONCORDE's cap line and baseline
      and "font-size:.865em;vertical-align:.06em" in page)
s, h, b = req("/", headers={"X-Forwarded-For": "1.2.3.4"})
door = b.decode("utf-8", "replace")
check("the door's AI is fattened too, against its gradient",
      "-webkit-text-fill-color:#f5f6f8" in door
      and "-webkit-text-stroke:.12em #f5f6f8" in door
      and "Concorde<b>AI</b>" in door)
# 6b265, per Patrick ("checkbox or similar for auto cleanup"): the
# Manage panel grows an auto-clean toggle; the sweep removes a model
# only when a newer generation of its family is ALSO installed, shares
# the /api/model/remove guards verbatim, and stands down during any
# download or app update
check("auto-cleanup wired end to end",
      "def superseded_installed" in _MILLENAI_SRC
      and "def _remove_models" in _MILLENAI_SRC
      and "def _auto_cleanup_pass" in _MILLENAI_SRC
      and "_auto_cleanup_pass()" in
          _MILLENAI_SRC.split("def _mlx_janitor")[1][:1200]
      and '"/api/model/cleanup"' in _MILLENAI_SRC
      and _MILLENAI_SRC.count('"/api/model/cleanup"') >= 2  # + ADMIN_PATHS
      and 'id="autoclean"' in page)
s, h, b = req("/api/setup", cookie=K, timeout=180)
check("setup reports the reclaimable set", b'"cleanup"' in b)
# 6b268, per Patrick's RC4 trial: the page AI tucks to the titlebar's
# measured coupling, the memory %% is gone (the bar carries it), and
# Clean now opens a pop-up naming the superseded models + GB and runs
# the guarded sweep on demand (force skips only the pref gate)
# 6b269: the prune (six rows retired into RETIRED_MODELS so their
# weights stay deletable), the wizard's own auto-clean box, the hero
# that greets by name and town, and chat search under the lane tabs
check("prune: retired registry + ladders scrubbed",
      "RETIRED_MODELS = {" in _MILLENAI_SRC
      and '"Gemma 2 9B IT":     ("mlx-community/gemma-2-9b-it-4bit"' in _MILLENAI_SRC
      and _MILLENAI_SRC.count('("Gemma 2 9B IT",') == 0
      and _MILLENAI_SRC.count('"Llama 3.1 8B"') == 1     # registry only
      and '"Mistral Small 24B":' in _MILLENAI_SRC
      and "def _gb_of" in _MILLENAI_SRC
      and "elif label in RETIRED_MODELS:" in _MILLENAI_SRC)
check("wizard has the auto-clean box, no button",
      'id="wiz-ac"' in page and "wiz-clean-now" not in page)
check("hero greets by name and town",
      "const NICK=" in page and "const CITY=" in page
      and "__USER_NICK__" not in page and "__USER_CITY__" not in page)
check("chat search under the tabs",
      'id="chat-q"' in page and "/api/chats/search?q=" in page
      and '"/api/chats/search"' in _MILLENAI_SRC)
s, h, b = req("/api/chats/search?q=zzqxv", cookie=K)
check("chat search endpoint answers", s == 200 and b'"ids"' in b)
check("titlebar lockup centers on the window",
      "standardWindowButton_(0).superview()" in _MILLENAI_SRC
      and "setAutoresizingMask_(1 | 4)" in _MILLENAI_SRC
      # the vanishing bug: _lockw must never be computed before _ww
      and _MILLENAI_SRC.find("_ww = _wh * (15.0 / 12.6)")
          < _MILLENAI_SRC.find("_lockw = 6 + _ww"))
check("clean-now flow wired",
      'id="clean-now"' in page and 'id="clean-veil"' in page
      and 'id="clean-go"' in page
      and "font-weight:400;-webkit-text-stroke:.12em currentColor" in page
      and 'id="autoclean-bar"' in page
      and "#roster:not(.managing) .rrm{display:none}" in page
      and "def _auto_cleanup_pass(manual=False)" in _MILLENAI_SRC
      and '_b.get("force")' in _MILLENAI_SRC)
# 6b284, per Patrick ("keep the design consistent"): the clean-up dialog
# wears the house chrome, reports the real reason a removal failed, and
# a retired Ollama row deletes the tag that is actually pulled
check("clean-up dialog: house chrome, honest errors, exact tags",
      "#clean-card .ghost{" in page and "#clean-card .primary{" in page
      and '"errors": dict(_CLEANUP_LAST_ERRORS)' in _MILLENAI_SRC
      and "resolve the" in _MILLENAI_SRC and "t.split(\":\")[0] == _tag" in _MILLENAI_SRC)
# 6b283, per Patrick (twice): the Clean-up button and note were clipped
# below the settings card — the card is a grid whose row grew to its
# content; the row is bounded now and the pane scrolls
check("settings card grid is bounded and the clean row is one line",
      "grid-template-rows:minmax(0,1fr)" in page
      and "#set-rail{min-height:0}" in page
      and "flex-wrap:nowrap}" in page
      and 'display:inline-block;width:auto}' in page)
# 6b282, per Patrick: the full-screen version zoom after an update is
# retired for an in-app dialog with a scrolling release-notes box
check("post-update dialog replaces the zoom",
      'id="updated-veil"' in page and 'id="updated-notes"' in page
      and "__JUST_UPDATED__" not in page
      and "_JUST_UPDATED[0] = str(last)" in _MILLENAI_SRC)
# 6b281, cycle 19 of the drill: the host clock leaked on questions
# that named no place (home zone now frames every request), a two-
# venue list became "nothing's open" (thin lists are scoped), an
# answer claimed lived experience, chat asserted "every 2015 MacBook",
# a $1,999 laptop was sold against "under $1500"
check("cycle-20 fixes: home zone always, thin lists scoped, walls, no fake experience",
      "_tl_search.tz, _tl_search.tz_place = _home_tz()" in _MILLENAI_SRC
      and "_tl_search.thin_closed" in _MILLENAI_SRC
      and "THIN LIST: only" in _MILLENAI_SRC
      and "Never claim lived experience" in _MILLENAI_SRC
      and "A stated NUMBER is a wall" in _MILLENAI_SRC
      and "At most ONE remembered fact per" in _MILLENAI_SRC)
# 6b280, the 6.0.1 pre-release review: the nine-day weekend fetch was
# clipped by an unchanged [:7]; CLOSED matched any lowercase "closed";
# the OSM row cache returned before the venue zone was set; a failed
# home geocode was pinned; the closure executor's exit waited on
# stragglers; the chrome pass had no re-entry guard
check("6.0.1 review fixes",
      'enumerate(dl.get("time", []))' in _MILLENAI_SRC
      and '(?i:\\bpermanently closed\\b)' in _MILLENAI_SRC
      and "open-now is a function of NOW" in _MILLENAI_SRC
      and "only a SUCCESS is remembered" in _MILLENAI_SRC
      and "ex.shutdown(wait=False, cancel_futures=True)" in _MILLENAI_SRC
      and '_CHROME.get("acc") or _CHROME.get("lock") is not None' in _MILLENAI_SRC
      # and the geocoder caches only successes — a rate-limited miss
      # used to poison every later weather ask in the process
      and "    if out:\n        _geo_cache[q] = out" in _MILLENAI_SRC)
# 6b278, cycles 17-18 of the drill: a dead pizzeria kept getting
# recommended off its own website (closure notices are now fetched
# and injected), movie slates came from memory (listings sites are
# searched first), and chat invented "most people" statistics
check("cycle-19 fixes: closure notices, listings sites, no invented precision",
      "def closure_notices" in _MILLENAI_SRC
      and "def _venue_names" in _MILLENAI_SRC
      and "_CLOSED_TITLE_RX" in _MILLENAI_SRC
      and 'snippets = closure_notices(query) + (snippets or "")' in _MILLENAI_SRC
      and "'listed, unverified'" in _MILLENAI_SRC
      and "site:rottentomatoes.com OR" in _MILLENAI_SRC
      and "never invent 'most people" in _MILLENAI_SRC)
# 6b277: a bare % in the verdict-audit prompt killed EVERY funnel
# verdict for two drill cycles while the gauntlet stayed green — the
# funnel checks were source-level only. This walks a real funnel to
# its verdict, the way drill.py does, so the verdict path is exercised
# on every run.
_fs = {"goal": "Which candy should I try next?", "reqs": "", "opts": 4,
       "stages": 3, "images": False, "picks": [], "asked": []}
_fdone, _ferr = False, ""
for _hop in range(1, 8):
    _st, _h, _b = req("/api/funnel", "POST", _fs, cookie=K, timeout=240)
    if _st != 200:
        _ferr = "http %s at hop %d" % (_st, _hop); break
    try:
        _fd = json.loads(_b)
    except Exception:
        _ferr = "non-json at hop %d" % _hop; break
    if _fd.get("err"):
        _ferr = str(_fd["err"])[:80]; break
    if _fd.get("done"):
        _fdone = len(str(_fd.get("summary") or "")) > 40; break
    _fo = [o.get("label", "") for o in _fd.get("options", [])]
    if not _fo:
        _ferr = "empty options at hop %d" % _hop; break
    _fs["asked"].append(_fd.get("q", "")); _fs["picks"].append(_fo[0])
check("a real funnel reaches its verdict", _fdone, _ferr)
# 6b276, cycle 16's other two: committed plans re-add their numbers
# (a "20-minute" workout summed to 24), and listing/price asks rank
# the authoritative hosts first
check("cycle-18 side fixes: arithmetic self-check, authoritative hosts",
      "re-add them before you send" in _MILLENAI_SRC
      and '"fandango.", "rottentomatoes.", "boxofficemojo."' in _MILLENAI_SRC
      and '"bls.gov"' in _MILLENAI_SRC)
# 6b275, cycle 16 of the drill: the host clock leaked back in when a
# venue failed to geocode (home area's zone is now the default for
# local-intent asks), Sunday was missing from a weekend forecast (nine
# days fetched), the audit's correction leaked "that name was
# invented" and passed a mangled ticker, and prices/lineups shipped
# from memory as "right now"
check("cycle-17 fixes: home-zone default, nine-day weekend, exact audit, dated facts",
      "def _home_tz" in _MILLENAI_SRC and "_LOCAL_INTENT_RX" in _MILLENAI_SRC
      and "9 if weekend else 3" in _MILLENAI_SRC
      and "must be EXACT" in _MILLENAI_SRC
      and "the audit, a wrong name" in _MILLENAI_SRC
      and "A CURRENT PRICE, CURRENT LINEUP" in _MILLENAI_SRC)
# 6b274, cycle 15 of the drill: the verdict auditor verified by
# MENTION (now it must describe each pick's real attribute before it
# may pass), an empty-options stage shipped (now a plain fallback
# stage), and a permanently closed pizzeria was recommended off its
# own website (now: closed means closed, a thin list is not the town)
check("cycle-16 fixes: describe-first audit, fallback stage, closure rule",
      "FIRST, for each pick" in _MILLENAI_SRC
      and "VERDICT:" in _MILLENAI_SRC
      and "In the final call, what matters most?" in _MILLENAI_SRC
      and "CLOSED MEANS CLOSED" in _MILLENAI_SRC)
# 6b273, cycle 15 of the drill: the host Mac sat in Asia/Tokyo on a
# trip and every Brooklyn "open now" answer was computed on Tokyo's
# hour — the venue's own clock now rides the request (OSM hours, the
# closed-day check, the system and user-turn clock lines, weather)
check("open-now answers use the venue's clock, not the host's",
      "def _tz_of" in _MILLENAI_SRC and "def _venue_now" in _MILLENAI_SRC
      and "_oh_open_now(oh, _vnow)" in _MILLENAI_SRC
      and 'wd = time.strftime("%A", _venue_now())' in _MILLENAI_SRC
      and '_venue_stamp("%A %-I:%M%p")' in _MILLENAI_SRC
      and "_tl_search.tz = _tz_of(" in _MILLENAI_SRC)
# 6b272, cycle 14 of the drill (web): "this weekend" asked on a Monday
# answered the weekend just ended — weekend asks now take the 7-day
# rung and report today + the COMING Sat/Sun, labeled; feels-like is
# bounded near the air temperature below 80°F
check("weekend asks resolve to the coming Sat/Sun",
      "def _weekend_dates" in _MILLENAI_SRC
      and "this coming " in _MILLENAI_SRC
      and "forecast_days=%d" in _MILLENAI_SRC
      and "apparent_temperature\"] = cur[\"temperature_2m\"]" in _MILLENAI_SRC)
# 6b271, cycle 14 of the drill: tables/H2s in 5 of 6 simple chat
# answers (the default rung now carries the shape law), remembered
# facts hedged into guesses again (plainly or not at all), and funnel
# verdicts bending picks in meaning — now audited by a second call
check("cycle-15 fixes: shape law, plain memory, verdict audit",
      "No headings, no tables, no code-fenced" in _MILLENAI_SRC
      and "never hedge one into a guess" in _MILLENAI_SRC
      and "VERDICT UNDER AUDIT" in _MILLENAI_SRC
      and "state the fork instead" not in _MILLENAI_SRC)
# 6b270, cycle 13 of the drill: a stale daytime reading shipped as
# "sunny 87°F at 3:30 AM" (now: age + above-high sanity, night sky
# words, the feed logged as a source); movie DESCRIPTORS shipped as
# titles (now: names only); "Conservative" got an all-equity fund and
# a trip verdict invented rail to Cape May (now: picks bind in
# meaning, transit claims need a named real line)
check("cycle-14 fixes: weather sanity, names only, semantic picks",
      "stale or impossible reading" in _MILLENAI_SRC
      and "relative_humidity_2m,is_day" in _MILLENAI_SRC
      and "_tl_search.weather_src" in _MILLENAI_SRC
      and "NAMES ONLY: an item is" in _MILLENAI_SRC
      and "LITERALLY AND IN MEANING" in _MILLENAI_SRC
      and "NAMED real line or operator" in _MILLENAI_SRC)
# 6b267, cycle 12 of the drill: local-Gemma stages wrote malformed
# options and reworded re-asks (three cycles running), one verdict
# invented an NJ Transit route to Jim Thorpe, another served gnudi to
# a Handmade-pasta click, and softened capability meta slipped the ban
check("cycle-13 fixes: stage gate, literal picks, honest negatives",
      "def _stage_ok" in _MILLENAI_SRC
      and "_stage_ok(data, asked, opts)" in _MILLENAI_SRC
      and "_stage_ok(d2, asked, opts)" in _MILLENAI_SRC
      and "FEASIBILITY IS AN ATTRIBUTE" in _MILLENAI_SRC
      and "Picks are binding, LITERALLY" in _MILLENAI_SRC
      and "softened forms" in _MILLENAI_SRC
      and "still commit: name ONE real place" in _MILLENAI_SRC)
# 6b266, cycle 11 of the drill: wttr.in died silently one night and
# the answer invented "71°F and sunny" at 10:30 PM; the L-train
# answer opened with capability meta; a candy verdict gave Nerds
# sprinkles they don't have
check("cycle-12 fixes: weather ladder + honesty, meta ban, no fake fits",
      "api.open-meteo.com" in _MILLENAI_SRC
      and "No live weather data reached you" in _MILLENAI_SRC
      and "Never describe your own data access" in _MILLENAI_SRC
      and "No fake fits" in _MILLENAI_SRC)
# 6b264, seen live: APP_BUILD holds still between releases (Patrick's
# rule), so a build-only ETag answered 304 to a WKWebView holding a
# cached weeks-old page — the test window kept showing stale UI. The
# ETag now carries the source mtime; a foreign tag must fetch fresh.
s, h, b = req("/", headers={"If-None-Match": '"b260"'}, cookie=K)
check("a stale cached page can never 304 its way back", s == 200)
# 6b258: the titlebar wears the lockup as a real accessory (the
# ConcordeVPN look, minus its gear — settings live in the sidebar
# here). Michroma has to be BUNDLED: a native NSTextField cannot pull
# a webfont the way the page does.
# 6b258, per Patrick ("almost there"): this line is a RELEASE
# CANDIDATE, not a beta — the label changes on every display surface
# while the prerelease hold stays exactly as it was, so /releases/latest
# still never offers it to a stable install.
# every surface names the same build: the constants, the page and the
# release label cannot drift apart, whatever line we are on
check("release labelling: the page and the constants name one build",
      _vshown.startswith(_vwant)
      and ((" beta " in _vshown) == (_vsrc.get("BETA", "0") != "0"))
      and ((" RC" in _vshown) == (_vsrc.get("RC", "0") != "0")),
      "%s vs %s/%s/%s" % (_vshown, _vsrc.get("VERSION"), _vsrc.get("BETA"),
                          _vsrc.get("RC")))
check("titlebar lockup: accessory + bundled font",
      "NSTitlebarAccessoryViewController" in _MILLENAI_SRC
      and "def _brand_accessory" in _MILLENAI_SRC
      and '"NSStrokeWidth": -12.0' in _MILLENAI_SRC
      and "def _load_michroma" in _MILLENAI_SRC
      and "CTFontManagerRegisterFontsForURL" in _MILLENAI_SRC
      and len(open("fonts/Michroma-Regular.ttf", "rb").read(8)) == 8
      and "cp -R fonts" in open("build_macos_app.sh").read())
# 6b257: the app checks for updates BY ITSELF — hourly while open,
# owner only (a tunnel visitor can't run the install, so never tempt
# them), skipping hidden windows and settling up on wake; the server
# answers the hourly pollers from a 15-min cache and only a human
# click on the Settings button forces a real GitHub hit
# 6b292: daily, not hourly — and only while the About switch is on
check("auto update check: daily, owner-only, wake-aware, cached",
      "setInterval(()=>{if(!document.hidden)checkUpdate();},86400000)" in page
      and "visibilitychange" in page
      and "/api/update/check?force=1" in page
      and "def check_update(force=False)" in _MILLENAI_SRC
      and "_chk_cache" in _MILLENAI_SRC)
# 6b257: in the funnel lane a TYPED answer advances the funnel — the
# composer routes free text through the same fnAnswer path as a card
# click (or starts a funnel with it) instead of falling through to
# /api/chat as dead-end generic prose
check("funnel accepts typed answers",
      'uiMode==="funnel"&&text' in page
      and "if(o&&fnAnswer)fnAnswer(o.label);" in page
      and "startFunnel(text)" in page)
# 6b257, per Patrick: "once the query is done ... it's redundant" — a
# finished answer shows sources ONLY inside the disclosure. Live
# answers fold chips in with the steps (collapseSteps, 6b242);
# reloaded answers now fold them into the same box (srcBox) instead
# of the loose row addMsg used to prepend.
check("sources fold into the disclosure on every path",
      "function srcBox(" in page and "srcBox(srcs)+renderMD" in page
      and 'srcRow(srcs):"")+renderMD' not in page)
# 6b257: the about-name id is RETIRED. It existed THREE times (both
# veil titles + the rail lockup — the trap NOTES logged three separate
# times), and the pre-rail platform line wrote into whichever copy came
# first: the new-models veil title, invisible behind announceModels'
# own rewrite. Veil titles now carry distinct ids, the rail lockup is
# just #set-brand b, and no rule or query names the old id at all.
# 6b264, per Patrick ("get rid of that bubble/oval"): the HDR glint
# is PULLED — the radial mask read as an oval pill behind the wordmark
# on dark ground. The beacon file and route stay for a future
# glyph-masked attempt; the PAGE must be clean of the machinery.
check("no glow oval behind the wordmark",
      "aiglow" not in page and "hdrai" not in page
      and "hdrglow" not in page
      and req("/vfx/hdr-beacon.mp4", cookie=K)[0] in (200, 206))
check("about-name id retired, veil titles distinct",
      'id="about-name"' not in page
      and 'id="new-title"' in page and 'id="up-title"' in page
      and "#about-name" not in page)
# 6b257: the stream opens on a QUIET pinwheel, not the pulsing caret,
# and the machinery card holds back for the run's first 5 seconds —
# a quick answer never shows its workings (paintSteps lifts .warm)
check("spinner-first stream, caret retired",
      '<span class="caret">' not in page and ".caret{" not in page
      and ".worktree.warm{" in page
      and 'box.classList.add("warm")' in page)
# 6b257: the bar the user SEES is a tween chasing the honest math —
# the in-place fast path moves .wtbar i without an innerHTML rewrite
# (a rewrite recreates the <i> and kills the width transition), and
# a per-tier EMA (millen.speeds) feeds the italic time-left line
check("smooth bar + honest time-left",
      "millen.speeds" in page and 'class="wteta"' in page
      and "dispPct" in page and "bi.style.width=shown" in page)
# 6b257: Answer now — armed only once a REAL draft exists, delegated
# like the chevron (the card is re-cloned every drip frame), and the
# server end: an unguessable X-Hurry id, a non-admin endpoint, and
# run_council trading the rest of the council for the fastest pen
check("Answer now: button, endpoint, council hooks",
      'class="wtnow"' in page and "Hurrying it along" in page
      and "/api/chat/hurry" in page
      and "X-Hurry" in _MILLENAI_SRC
      and "_hurry_jobs" in _MILLENAI_SRC
      and "hurry=hurry_ev" in _MILLENAI_SRC
      and "fast_cloud_ladder()" in _MILLENAI_SRC)
# 6b257: the seamless dark title bar — transparent titlebar + hidden
# title over the page-dark window background, NO fullSizeContentView
# (the VPN app proved content under the bar kills window drag)
check("seamless dark title bar",
      "setTitlebarAppearsTransparent_" in _MILLENAI_SRC
      and "NSAppearanceNameDarkAqua" in _MILLENAI_SRC
      and "fullSizeContentView" not in _MILLENAI_SRC.replace(
          "NO fullSizeContentView", ""))
# 6b257 settings round 2 (per Patrick's concept picks): panes open
# with a one-breath description, /api/me sits behind the Account pane,
# and Forget Me is scoped + triple-locked. 6b259: About is the
# exception — the version right under the title says it better than a
# sentence would — so five descriptions across six panes.
check("settings: descriptions + Account pane + scoped forget",
      # scoped to the settings panel: counting the whole page broke the
      # moment another dialog grew a description (6b303)
      (page.split('id="about-veil"')[1].split('id="gear-veil"')[0]
       if 'id="gear-veil"' in page
       else page.split('id="about-veil"')[1]).count('class="tdesc"') == 5
      and 'data-pane="p-account"' in page
      and '"/api/me"' in _MILLENAI_SRC
      and '"/api/logout"' in _MILLENAI_SRC
      and '"/api/forget"' in _MILLENAI_SRC
      and "FORGET ME" in page)
# 6b259, per Patrick: About leads the rail (it is what people open the
# panel to see) and Account closes it (the exits belong at the foot).
# The pane ids and the nav must agree on that order, and the first pane
# is the one that opens.
_nav = re.findall(r'data-pane="(p-[a-z]+)"', page)
_panes = re.findall(r'class="spane[^"]*" id="(p-[a-z]+)"', page)
_want = ["p-about", "p-account", "p-persona", "p-cloud", "p-community",
         "p-models"]
check("About leads the rail, Account right under it",
      _nav == _want and _panes == _want
      and '<button class="snav on" data-pane="p-about">About</button>' in page
      and '<section class="spane on" id="p-about">' in page
      and "p-updates" not in page
      # the removed blurb must not creep back
      and "What version you're flying" not in page)
# 6b257: the Community pane tells the truth — a ledger this Mac
# measured (its own file: prefs.json rewrites would race the worker
# thread), a TIME share that rests between jobs (no honest GPU-percent
# knob exists, so none is offered), and gates that finally make the
# idle-only tooltip promise real (AC via psutil, HIDIdleTime via
# ioreg). The politely-lying user-count line must never return.
check("community: honest ledger + real gates",
      'id="contrib-stats"' in page and 'id="contrib-seg"' in page
      and "contrib_ledger.json" in _MILLENAI_SRC
      and "_on_ac_power" in _MILLENAI_SRC
      and "HIDIdleTime" in _MILLENAI_SRC
      and "Contributing to " not in page)
# 6b257: the Models roster — status/size/purpose per mind from data
# the resolvers already compute (ADV_USE is the one description dict,
# so the picker and the roster can never drift); Manage reuses the
# first-run plans, and the only new destructive surface is
# admin-gated and refuses mid-download removals
check("models roster + manage flow",
      'id="roster"' in page and "paintRoster" in page
      and '"/api/model/remove"' in _MILLENAI_SRC
      and '"still downloading"' in _MILLENAI_SRC
      and _MILLENAI_SRC.count("/api/model/remove") >= 2)
# 6b257: the Updates pane wears the version, the release date, and
# the release notes (the gh release body now rides /api/update/check)
check("updates: version, date, notes",
      'id="up-version"' in page and 'id="up-reldate"' in page
      and '"notes": (rel.get("body")' in _MILLENAI_SRC)
# 6b257: the name field — placeholder-first, saved with persona,
# injected once into the system-prompt assembly every model reads
check("your name reaches every model",
      'id="user-name"' in page
      and "Your name (or nickname)" in page
      and "The user's name is " in _MILLENAI_SRC)
# 6b258, per Patrick ("this pane is glitchy"): NO checkboxes — every
# roster row carries a text action, install on one side and remove on
# the other, so both read the same. And the LIST scrolls: 20+ models
# used to stretch the dialog past the screen, which is why Manage kept
# ending up unreachable below the fold.
check("roster: text actions, no checkboxes, and it scrolls",
      'class="rin"' in page and 'class="rrm"' in page
      and "rpick" not in page and "manual-install" not in page
      and "#roster{max-height:230px;overflow-y:auto" in page)
# 6b258: Manage leads with the inventory (models installed, space
# taken) and offers four honestly-labelled sizes. Only the last can
# hurt — it installs models bigger than this Mac's memory — so it
# wears a warning triangle and confirms in place before it runs.
check("manage: inventory + four sizes, the risky one warned",
      'id="mg-count"' in page and 'id="mg-space"' in page
      and '"rec","Recommended"' in page.replace(" ", "")
      and 'classList.contains("risky")' in page
      and "may crash it if memory runs out" in page
      and '"plan_n"' in _MILLENAI_SRC
      and 'if plan == "rec":' in _MILLENAI_SRC
      and "def _family_of" in _MILLENAI_SRC)
# 6b258: a release body is hard-wrapped at ~72 columns for git, and
# rendering it pre-wrap dropped those breaks mid-sentence in a narrow
# pane. The notes reflow now: paragraphs rejoin, list items survive.
check("release notes reflow instead of keeping git's wraps",
      "function notesHTML" in page
      and "#up-notes{" in page
      and "pre-wrap" not in page.split("#up-notes{")[1][:260])
# 6b257: THE OWNER HAS NO COOKIE — they are authenticated by the mere
# absence of proxy headers, so SameSite protects them from nothing and
# any web page could POST to 127.0.0.1 and erase their chats or delete
# multi-GB weights. Writes now demand a same-origin Origin (browsers
# attach one to every cross-site POST), refuse the three form content
# types, and refuse a rebinding Host. Native callers — curl, the fleet
# workers, this gauntlet — send no Origin and sail through.
s, h, b = req("/api/forget", "POST", {"scopes": []}, cookie=K,
              headers={"Content-Type": "text/plain"})
check("CSRF: form content type refused", s == 403)
s, h, b = req("/api/forget", "POST", {"scopes": []}, cookie=K,
              headers={"Origin": "http://evil.example"})
check("CSRF: foreign Origin refused", s == 403)
s, h, b = req("/api/forget", "POST", {"scopes": []}, cookie=K,
              headers={"Host": "evil.example"})
check("CSRF: rebinding Host refused", s == 403)
s, h, b = req("/api/forget", "POST", {"scopes": []}, cookie=K,
              headers={"Origin": BASE})
check("CSRF: same-origin write allowed", s == 200)
s, h, b = req("/api/logout", "POST", cookie=K)
check("logout clears the cookie", s == 200
      and "max-age=0" in (h.get("Set-Cookie", "").lower()))
# a valid-JSON non-object body used to reach .get() and 500 the handler
s, h, b = req("/api/forget", "POST", [1, 2, 3], cookie=K)
check("non-dict JSON body survives", s == 200)
# 6b257: the contribute loop carries a generation token — the stop
# Event alone could not retire a loop stuck mid-job (contrib_apply
# gives up after 3s and CLEARS the flag for the new thread, and the
# old one sails on), so flipping a Settings toggle during a job left
# two loops polling the hub
check("contribute loop retires by generation",
      "_contrib_gen" in _MILLENAI_SRC
      and "gen == _contrib_gen[0]" in _MILLENAI_SRC)
# 6b257: erase means erase — a walled profile's .ident marker holds
# the very PII the pane promises to forget (the Google email), so a
# full three-scope forget takes the directory with it
check("full forget removes the profile marker",
      'shutil.rmtree(base, ignore_errors=True)' in _MILLENAI_SRC)
# 6b257: removal must not lie — a non-zero `ollama rm` used to report
# success while the weights stayed, and the MLX path must take the
# same _engine_lock every other process-table mutation takes
check("model removal is honest and locked",
      '"ollama rm failed"' in _MILLENAI_SRC
      and _MILLENAI_SRC.count("with _engine_lock:") >= 4)

print("== resolvers ==")
s, h, b = req("/api/tiers", cookie=K)
tiers = json.loads(b)
# 5.3: no skip list — Pro (all-models) must resolve like everything else
# 6b245: Kimi K3 is the 4th provider — dropdown option and board row.
# (The backend spec was proven live: a probe key reached api.moonshot.ai
# and came back with Moonshot's own "Invalid Authentication".)
check("Kimi K3 wired as a provider",
      'value="kimi">Kimi K3 (paid)' in page and '"kimi","Kimi K3"' in page)
# 6b261: Cloud Only may legitimately resolve EMPTY while every
# provider is quota-resting — the drill's own batches caused exactly
# that, twice, and each time this check cried wolf. Empty Cloud Only
# with all configured providers cooling is the environment, not a
# regression; empty ANY OTHER tier, or empty Cloud Only with a
# healthy provider available, is still a hard fail.
_cl = json.loads(req("/api/cloud", cookie=K)[2])
# 6b274: a provider can be resting with cool==0 ("rate limited —
# resting" in its note) or simply "not responding" — both are the
# environment, not a regression, and both cried wolf after a batch
_resting = all((v.get("cool") or 0) > 0 or v.get("status") != "ok"
               or "resting" in str(v.get("note") or "")
               or "not responding" in str(v.get("note") or "")
               for v in (_cl.get("providers") or {}).values())     if (_cl.get("providers") or {}) else False
check("every tier resolves",
      all(t.get("models") for n, t in tiers.items()
          if not (n == "Cloud Only" and _resting)),
      str({n: t.get("models") for n, t in tiers.items()})
      + (" [all providers resting]" if _resting else ""))
check("Best and Power tiers are gone",
      "Best" not in tiers and "Power" not in tiers, str(list(tiers)))
s, h, b = req("/api/stats", cookie=K)
st = json.loads(b)
check("stats has users + memory", "users_total" in st and "mem_total_gb" in st)
# 6b254: the MODELS meter became a MEMORY reading — pressure on macOS
# (wired+compressed, what Activity Monitor gauges), used% elsewhere.
# psutil's used% would have read ~2x higher on a healthy Mac.
check("memory meter replaced the models meter",
      'id="mem-meter"' in page and 'id="mem-val"' not in page  # 6b268
      and "models-meter" not in page
      and ("MEMORY PRESSURE" in page or "MEMORY USED" in page)
      and "def mem_pressure" in _MILLENAI_SRC
      and "Pages occupied by compressor" in _MILLENAI_SRC)

print("== engines (live generations) ==")
# LOCAL SILICON ONLY (6b293, per Patrick): a gauntlet run used to fan
# every live question out to all four cloud providers and rest them for
# ten minutes — quota burned by a test. Turbo is parked for the whole
# section and restored at the very end, whatever happens.
_prefs_gauntlet = json.loads(req("/api/prefs", cookie=K)[2])
req("/api/prefs", "POST", {"turbo": False}, cookie=K)
import atexit as _atexit
_atexit.register(lambda: req("/api/prefs", "POST",
                             {"turbo": bool(_prefs_gauntlet.get("turbo"))},
                             cookie=K))
check("gauntlet never spends cloud quota: turbo parked, dev state private",
      'cloud-dev-%d.json' in _MILLENAI_SRC
      and "if PORT not in (8889, 9889):" in _MILLENAI_SRC)


def chat(payload, timeout=600):
    s, h, b = req("/api/chat", "POST", payload, cookie=K, timeout=timeout)
    text = b.decode("utf-8", "replace")
    text = re.sub("\x00STATUS:.*?\x00", "", text)
    text = re.sub("\x00DRAFT:.*?\x00", "", text)
    cut = text.rfind("\x00RESET\x00")
    if cut >= 0:
        text = text[cut + 7:]
    return text.strip()


def healthy(text):
    words = re.findall(r"[a-z']+", text.lower())
    grams = Counter(tuple(words[i:i + 3]) for i in range(max(0, len(words) - 2)))
    rep = max(grams.values()) if grams else 0
    return len(text) > 300 and rep <= 8 and "⚠️" not in text, \
        f"{len(text)} chars, 3gram x{rep}"


# Fast and Smart merged in 1.20: Fast now runs the strongest fitting
# model, so it earns the strict health bar
t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": False,
          "messages": [{"role": "user", "content": "tell me about central park"}]})
ok, d = healthy(t)
check("Fast tier answer healthy", ok, d)

t = chat({"model": "", "models": [], "tier": "Smart", "auto_web": False,
          "messages": [{"role": "user", "content": "give me a great one-day brooklyn itinerary"}]})
ok, d = healthy(t)
check("legacy Smart alias still answers", ok, d)

t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "whats the weather in 11221"}]})
check("weather answer carries real data", ("°F" in t or "degrees" in t or " mph" in t)
      and "⚠️" not in t and len(t) > 60, t[:120])

# FLEET LOOPBACK (6b244): a real worker speaking the real protocol —
# register (auto-approve + token), long-poll, take the job, submit a
# sentinel — and the chat answer must BE that sentinel, delivered with
# the "GPU is on it" status. Proves dispatch end to end with zero
# engine loads. turbo is parked for the window (cloud outranks fleet
# in the single-model path) and restored no matter what.
import threading as _th

_FSENT = ("FLEET-GAUNTLET-7391: the pooled GPU answered this, and this "
          "sentence is long enough to clear the degenerate-output floor "
          "standing in for a real model's reply.")


# The hub hands a worker its token ONCE (register marks the claim
# "claimed"); a known wid arriving with no token is an imposter and
# parks in pending — correct security, but it made a fixed test wid
# work exactly once. Persist the (wid, token) PAIR across runs; if the
# cache is gone, a fresh random wid gets auto-approved and re-cached.
import os as _os
import secrets as _sec
import tempfile as _tf

_FCACHE = _os.path.join(_tf.gettempdir(), "millenai-gauntlet-fleet.json")


def _fleet_worker(stop):
    try:
        c = json.load(open(_FCACHE))
        wid, tok = c["wid"], c["token"]
    except Exception:
        wid, tok = "gauntlet" + _sec.token_hex(6), ""
    while not stop.is_set():
        try:
            s2, h2, b2 = req("/api/fleet/register", "POST",
                             {"id": wid, "token": tok, "name": "gauntlet-rig",
                              "models": [json.loads(
                                  req("/api/tiers", cookie=K)[2])
                                  ["Fast"]["models"][0]]}, cookie=K)
            out = json.loads(b2)
            if out.get("pending"):
                # claimed wid, lost token — start over as a new worker
                wid, tok = "gauntlet" + _sec.token_hex(6), ""
                continue
            if out.get("token"):
                tok = out["token"]
                json.dump({"wid": wid, "token": tok}, open(_FCACHE, "w"))
            if not tok:
                time.sleep(1)
                continue
            s2, h2, b2 = req("/api/fleet/poll", "POST",
                             {"id": wid, "token": tok}, cookie=K, timeout=40)
            job = json.loads(b2)
            if job.get("job"):
                req("/api/fleet/submit", "POST",
                    {"id": wid, "token": tok, "job": job["job"],
                     "text": _FSENT}, cookie=K)
                return
        except Exception:
            time.sleep(1)


_prefs0 = json.loads(req("/api/prefs", cookie=K)[2])
req("/api/prefs", "POST", {"turbo": False}, cookie=K)
_fstop = _th.Event()
_fth = _th.Thread(target=_fleet_worker, args=(_fstop,), daemon=True)
_fth.start()
time.sleep(2)
try:
    t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": False,
              "messages": [{"role": "user",
                            "content": "Say hello in one sentence."}]},
             timeout=60)
    check("fleet: worker's answer comes back through chat",
          "FLEET-GAUNTLET-7391" in t, t[:120])
finally:
    _fstop.set()
    req("/api/prefs", "POST", {"turbo": bool(_prefs0.get("turbo"))}, cookie=K)

# a place no index knows must NOT get a bare "couldn't find any info"
# shrug (3.3) — the answer says so plainly AND asks a pin-down question
t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "is qzxvbn cafe in bushwick open tonight"}]})
check("unknown place gets helpful no-match answer",
      len(t) > 100 and "?" in t and "⚠️" not in t
      # the shape is taught by a Milano's/Ridgewood worked example —
      # its names leaking into the answer means the fence failed
      and "milano" not in t.lower() and "ridgewood" not in t.lower(),
      t[:160])


print("== attached files ==")
t = chat({"model": "", "models": [], "tier": "Fast", "auto_web": True,
          "messages": [{"role": "user", "content": "what is the project codename mentioned in this file?"}],
          "docs": [{"name": "notes.txt",
                    "text": "quarterly planning notes\nthe project codename is ZEBRA-42\nlunch is at noon"}]})
# models emit fancy hyphens (ZEBRA‑42 with U+2011) — normalize first
flat = re.sub(r"[^a-z0-9]+", "", t.lower())
affirms = "zebra42" in flat and not re.search(
    r"not seeing|don't see|do not see|there is no|isn't a|can't help", t.lower())
check("doc content reaches the model", affirms, t[:160])

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
       "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
t = chat({"model": "", "models": [], "tier": "", "auto_web": False,
          "messages": [{"role": "user", "content": "what color is this image?"}],
          "images": ["data:image/png;base64," + PNG]})
check("vision answers about the pixels", "red" in t.lower() and "⚠️" not in t,
      t[:100])

print()
passed = sum(1 for _n, o, _d in RESULTS if o)
print(f"SCORECARD: {passed}/{len(RESULTS)} passed")
for n, o, d in RESULTS:
    if not o:
        print("  FAILED:", n, "—", d)
sys.exit(0 if passed == len(RESULTS) else 1)
