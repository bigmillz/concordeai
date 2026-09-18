#!/usr/bin/env python3
"""ConcordeGo — the draft interface's server.

Why this exists at all, when the interface is one HTML file: the video
backdrop. Apple's ATV aerial clips cannot be played straight from a
browser — sylvan puts the `moov` atom AFTER 370 MB of `mdat`, so nothing
starts until the whole file has landed, and phobos is http-only. The AI
app solved this once already: download once, rewrite the file so `moov`
comes first, cache it, and stream it same-origin with Range support.

That machinery is lifted from millenai.py (`_atoms` / `_patch_moov` /
`_faststart` / `_sky_fetch` / `_send_sky`) with the comments that explain
the traps intact. It is pure stdlib — no ffmpeg, no pip install. Ports
8884-8930 belong to the model engines; this binds 9897 by default.

    python3 concorde-travel/server.py           # then open the printed URL
    CONCORDEGO_PORT=9898 python3 .../server.py

127.0.0.1 only. There is no auth here because there is nothing to
protect yet: the scorer is client-side fixture data and the only server
state is a cache of publicly downloadable video.
"""

import glob
import hashlib
import http.server
import json
import mimetypes
import os
import re
import socketserver
import struct
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scorer                                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "ui")
FIXTURES = os.path.join(HERE, "..", "fixtures")
PORT = int(os.environ.get("CONCORDEGO_PORT", "9897"))

# Apple's ATV aerials, the NIGHT subset — the ones millenai.py flags as
# dark, because this interface is dark and the city reads as light and
# colour through the glass rather than as detail.
SKY_SOURCES = [
    "https://sylvan.apple.com/Videos/SE_A016_C009_SDR_20190717_3m30s_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT026_363A_103NC_E1027_KOREA_JAPAN_NIGHT_v18_SDR_PS_20180907_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A001_C007_SDR_20190717_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_WH_D004_L014_SDR_20191031_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT312_162NC_139M_1041_AFRICA_NIGHT_v14_SDR_FINAL_20180706_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A004_C003_SDR_20190719_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/DL_B002_C011_SDR_20191122_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/KP_A010_C002_SDR_20190717_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/RS_A008_C010_SDR_20191218_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT314_139M_170NC_NORTH_AMERICA_AURORA__COMP_v22_SDR_20181206_v12CC_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A018_C029_SDR_20190812_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A002_C009_SDR_20190730_ALT01_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/MEX_A006_C008_SDR_20190923_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_CA_A016_C002_SDR_20191114_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/g201_AK_A003_C014_SDR_20191113_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/FK_U009_C004_SDR_20191220_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A012_C031_SDR_20190726_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/CR_A009_C007_SDR_20191113_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A014_C023_SDR_20190717_F240F3709_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/PA_A010_C007_SDR_20190717_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/comp_GMT307_136NC_134K_8277_NY_NIGHT_01_v25_SDR_PS_20180907_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/BO_A014_C008_SDR_20190719_SDR_2K_AVC.mov",
    "https://sylvan.apple.com/Videos/AK_A004_C012_SDR_20191217_SDR_2K_AVC.mov",
]

_sky_jobs = {}
_sky_lock = threading.Lock()


def _sky_dir():
    d = os.path.join(os.path.expanduser("~"), ".concordego", "sky")
    os.makedirs(d, exist_ok=True)
    return d


def _sky_path(i):
    # keyed by URL hash, not list index — editing SKY_SOURCES must never
    # make a cached file impersonate a different clip
    h = hashlib.sha1(SKY_SOURCES[i].encode()).hexdigest()[:10]
    return os.path.join(_sky_dir(), "sky-%s.mov" % h)


def _atoms(fh, end):
    """Top-level QuickTime atoms as (type, offset, size)."""
    off = fh.tell()
    while off + 8 <= end:
        fh.seek(off)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        if size == 1:
            size = struct.unpack(">Q", fh.read(8))[0]
        elif size == 0:
            size = end - off
        if size < 8:
            return
        yield typ, off, size
        off += size


def _patch_moov(buf, shift):
    """Shift every stco/co64 chunk offset inside a moov blob by `shift`.
    Recursive descent over the real container atoms — a naive byte scan
    for b'stco' can hit sample data and corrupt the file."""
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}

    def walk(start, end):
        off = start
        while off + 8 <= end:
            size, typ = struct.unpack(">I4s", buf[off:off + 8])
            hs = 8
            if size == 1:
                size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
                hs = 16
            if size < hs or off + size > end:
                return
            if typ in containers:
                walk(off + hs, off + size)
            elif typ in (b"stco", b"co64"):
                n = struct.unpack(">I", buf[off + hs + 4:off + hs + 8])[0]
                base = off + hs + 8
                w = 4 if typ == b"stco" else 8
                fmt = ">I" if typ == b"stco" else ">Q"
                for k in range(n):
                    p = base + w * k
                    v = struct.unpack(fmt, buf[p:p + w])[0] + shift
                    buf[p:p + w] = struct.pack(fmt, v)
            off += size

    walk(8, len(buf))


def _faststart(src, dst):
    """qt-faststart: rewrite `src` so moov precedes mdat, into `dst`."""
    total = os.path.getsize(src)
    with open(src, "rb") as fh:
        atoms = list(_atoms(fh, total))
        moov = next(((o, s) for t, o, s in atoms if t == b"moov"), None)
        mdat = next(((o, s) for t, o, s in atoms if t == b"mdat"), None)
        if not moov or not mdat:
            raise ValueError("no moov/mdat atom")
        if moov[0] < mdat[0]:                     # already fast-start
            os.replace(src, dst)
            return
        fh.seek(moov[0])
        blob = bytearray(fh.read(moov[1]))
        if b"cmov" in blob[:256]:
            raise ValueError("compressed moov unsupported")
        # every atom after ftyp moves back by exactly len(moov)
        _patch_moov(blob, moov[1])
        with open(dst + ".part", "wb") as out:
            for typ, off, size in atoms:          # ftyp keeps pole position
                if typ == b"ftyp":
                    fh.seek(off)
                    out.write(fh.read(size))
            out.write(blob)
            for typ, off, size in atoms:
                if typ in (b"ftyp", b"moov"):
                    continue
                fh.seek(off)
                left = size
                while left:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
    os.replace(dst + ".part", dst)
    os.remove(src)


def _sky_fetch(i):
    tmp = _sky_path(i) + ".dl"
    try:
        # a bare Python-urllib User-Agent gets 403'd by some provider
        # edges; "works in curl, fails in-app" is a UA fingerprint, not
        # logic (millenai.py learned this the hard way)
        req = urllib.request.Request(SKY_SOURCES[i],
                                     headers={"User-Agent": "ConcordeGo"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                with _sky_lock:
                    _sky_jobs[i] = {"status": "downloading",
                                    "pct": int(got * 92 / total) if total else 0}
        with _sky_lock:
            _sky_jobs[i] = {"status": "remuxing", "pct": 96}
        _faststart(tmp, _sky_path(i))
        with _sky_lock:
            _sky_jobs[i] = {"status": "ready", "pct": 100}
        # keep the last 6 clips; mtime is touched on serve, so the one
        # currently playing is never the evictee
        try:
            valid = {os.path.basename(_sky_path(n)) for n in range(len(SKY_SOURCES))}
            clips = sorted(glob.glob(os.path.join(_sky_dir(), "sky*.mov")),
                           key=os.path.getmtime)
            for p in clips:
                if os.path.basename(p) not in valid:
                    os.remove(p)
            clips = [p for p in clips if os.path.basename(p) in valid]
            for old in clips[:-6]:
                os.remove(old)
            for part in glob.glob(os.path.join(_sky_dir(), "*.dl")):
                if time.time() - os.path.getmtime(part) > 86400:
                    os.remove(part)
        except Exception:
            pass
    except Exception as exc:
        with _sky_lock:
            _sky_jobs[i] = {"status": "error", "pct": 0, "note": str(exc)[:120]}
        try:
            os.remove(tmp)
        except Exception:
            pass


def sky_status(i, warm=False):
    if not 0 <= i < len(SKY_SOURCES):
        return {"status": "error", "pct": 0, "note": "no such clip"}
    if os.path.exists(_sky_path(i)):
        return {"status": "ready", "pct": 100}
    with _sky_lock:
        job = _sky_jobs.get(i)
        if job and job.get("status") != "error":
            return dict(job)
        # ONE download at a time: several refreshes each kicking off a
        # 400 MB prewarm saturates the line and makes everything crawl
        busy = any(j.get("status") in ("downloading", "remuxing")
                   for j in _sky_jobs.values())
        if warm and not busy:
            _sky_jobs[i] = {"status": "downloading", "pct": 0}
            threading.Thread(target=_sky_fetch, args=(i,), daemon=True).start()
            return {"status": "downloading", "pct": 0}
    return {"status": "cold", "pct": 0}


def _cached():
    out = []
    for i in range(len(SKY_SOURCES)):
        if os.path.exists(_sky_path(i)):
            out.append(i)
    return out


# ------------------------------------------------------------------ scoring
# The page does not own a model. It asks for one, and renders what comes back.
# Everything below is presentation: it reshapes a Ledger and a timeline into
# what the interface draws, and computes nothing of its own. The moment this
# file starts deciding what a layover is worth, there are two scorers.


def _fixture_files():
    return sorted(f for f in glob.glob(os.path.join(FIXTURES, "*.json"))
                  if not f.endswith("schema.json"))


def _fixture_path(fid):
    for f in _fixture_files():
        if json.load(open(f, encoding="utf-8"))["fixture_id"] == fid:
            return f
    return None


def list_fixtures():
    out = []
    for f in _fixture_files():
        d = json.load(open(f, encoding="utf-8"))
        out.append({"fixture_id": d["fixture_id"], "title": d["title"],
                    "pins_down": d["pins_down"],
                    "origin": d["query"]["origin"]["label"],
                    "destination": d["query"]["destination"]["label"],
                    "depart_date": d["query"]["depart_date"],
                    "profiles": list(d["query"]["profiles"])})
    return out


def _apply_overrides(sc, req):
    """The scenario is the inventory; the query side belongs to the user. Bags,
    target, filters and the incidental ceiling all come off the form."""
    bags = req.get("bags")
    if bags is not None:
        for p in sc["query"]["party"]:
            keep = [b for b in p["bags"] if b["kind"] != "checked"]
            p["bags"] = [{"kind": "checked", "weight_kg": 20} for _ in range(int(bags))] + keep
    inc = req.get("incidental_cents")
    if inc is not None:
        sc["query"].setdefault("budget", {})["incidental_allowance_cents"] = int(inc)
    mx = req.get("max_cents")
    if mx is not None:
        sc["query"].setdefault("budget", {})["maximum_cents"] = int(mx) or None
    sc["query"]["preferences"] = req.get("prefs") or {}
    filters = req.get("filters") or []
    weights = req.get("weights") or {}
    for prof in sc["query"]["profiles"].values():
        prof["hard_filters"] = list(filters)
        if weights:
            prof["weights"] = dict(weights)
    return sc


def _view(sc, opt, profile):
    """One option, shaped for the page. Two renderings of one computed object:
    the ledger lines and the bar. Nothing here recalculates either."""
    led = scorer.score(sc, opt, profile)
    letter, ref_cents = scorer.grade(sc, opt)
    tl = scorer.timeline(sc, opt, profile)
    first, last = opt["segments"][0], opt["segments"][-1]
    tickets = opt["tickets"]
    award = next((t["award"] for t in tickets if t.get("award")), None)

    return {
        "option_id": opt["option_id"],
        "display_name": opt.get("display_name", opt["option_id"]),
        "carrier": first["marketing"]["carrier"],
        "carrier_name": opt.get("carrier_rating", {}).get("note", "") and
                        first["marketing"]["carrier"] or first["marketing"]["carrier"],
        "fare_brand": ", ".join(t["fare_brand_name"] for t in tickets),
        "route": " - ".join([first["origin"]["iata"]]
                            + [s["destination"]["iata"] for s in opt["segments"]]),
        "depart_local": first["departure_local"][11:16],
        "arrive_local": last["arrival_local"][11:16],
        "day_offset": (last["arrival_local"][:10] != first["departure_local"][:10]),
        "stops": len(opt["segments"]) - 1,
        "bag_included": max(t["entitlements"]["checked_included"] for t in tickets),
        "separate_tickets": len(tickets) > 1,
        "ticket_cents": led.lines[0].amount_cents,
        "effective_cents": led.effective_cents,
        "reference_cents": ref_cents,
        "door_to_door_minutes": led.door_to_door_minutes,
        "grade": letter,
        "filtered_reason": led.filtered_reason,
        "infeasible_reason": led.infeasible_reason,
        "award": award,
        "carrier_rating": opt.get("carrier_rating"),
        "lines": [{"code": l.code, "ref": l.ref, "label": l.label,
                   "amount_cents": l.amount_cents, "evidence": l.evidence,
                   "kind": l.kind, "overridable": l.overridable} for l in led.lines],
        "bar": [{"kind": l.kind, "label": l.label, "minutes": l.minutes,
                 "quality": l.quality, "detail": l.detail, "tip": l.tip} for l in tl],
        "segments": [{
            "flight": "%s %d" % (s["marketing"]["carrier"], s["marketing"]["number"]),
            "from": s["origin"]["iata"] + (("/" + s["origin"]["terminal"]) if s["origin"].get("terminal") else ""),
            "to": s["destination"]["iata"] + (("/" + s["destination"]["terminal"]) if s["destination"].get("terminal") else ""),
            "dep": s["departure_local"][11:16], "arr": s["arrival_local"][11:16],
            "actual_arr": (s.get("actual_arrival_local") or "")[11:16],
            "equipment": s.get("equipment_code", ""),
            "claims": s.get("claims", {}), "reliability": s.get("reliability", {}),
        } for s in opt["segments"]],
        "booking": sorted(opt.get("booking", []),
                          key=lambda b: (not b["direct"], b["price_cents"])),
    }


def score_request(req):
    fid = req.get("fixture") or "ground-access-swing"
    path = _fixture_path(fid)
    if not path:
        return {"error": "no such fixture: %s" % fid}
    sc = json.load(open(path, encoding="utf-8"))
    profile = req.get("profile") or "reference"
    if profile not in sc["query"]["profiles"]:
        profile = "reference"
    _apply_overrides(sc, req)

    views = [_view(sc, o, profile) for o in sc["options"]]
    kept = [v for v in views if not v["filtered_reason"] and not v["infeasible_reason"]]
    hidden = [v for v in views if v["filtered_reason"] or v["infeasible_reason"]]
    kept.sort(key=lambda v: (v["effective_cents"], v["option_id"]))

    return {
        "fixture": {"fixture_id": sc["fixture_id"], "title": sc["title"],
                    "origin": sc["query"]["origin"]["label"],
                    "destination": sc["query"]["destination"]["label"],
                    "depart_date": sc["query"]["depart_date"],
                    "route_par_cents": sc["query"]["route_par_cents"],
                    "profiles": list(sc["query"]["profiles"])},
        "profile": profile,
        "options": kept,
        "hidden": hidden,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ConcordeGo"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("CONCORDEGO_QUIET"):
            return
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            return self._send_file(os.path.join(UI, "index.html"), "text/html; charset=utf-8")
        if path == "/api/fixtures":
            return self._json(list_fixtures())
        if path == "/api/sky/cached":
            return self._json({"cached": _cached(), "total": len(SKY_SOURCES)})
        if path.startswith("/api/sky/status"):
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            args = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
            try:
                i = int(args.get("i", "0"))
            except ValueError:
                i = 0
            return self._json(sky_status(i, args.get("warm") == "1"))
        if re.match(r"^/sky/\d+\.mov$", path):
            return self._send_sky(path)
        # anything else under ui/ (future css, js, images)
        safe = os.path.normpath(path).lstrip("/")
        cand = os.path.join(UI, safe)
        if safe and os.path.isfile(cand) and cand.startswith(UI):
            typ = mimetypes.guess_type(cand)[0] or "application/octet-stream"
            return self._send_file(cand, typ)
        self.send_error(404)

    # --------------------------------------------------------------- POST
    def do_POST(self):
        if self.path.split("?")[0] != "/api/score":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:
            return self._json({"error": "bad request: %s" % exc})
        try:
            return self._json(score_request(req))
        except Exception as exc:
            # a broken fixture should say so on the page, not 500 silently
            import traceback
            traceback.print_exc()
            return self._json({"error": "%s: %s" % (type(exc).__name__, exc)})

    # ------------------------------------------------------------ helpers
    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # the page is edited constantly during the draft — never let a
        # browser hold a stale copy
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_sky(self, path):
        """Stream a cached clip with Range support — Safari asks for dozens
        of byte ranges while scrubbing a video into playback, and a plain
        200 makes it re-pull the whole file each time."""
        m = re.match(r"/sky/(\d+)\.mov$", path)
        i = int(m.group(1))
        p = _sky_path(i) if 0 <= i < len(SKY_SOURCES) else None
        if not (p and os.path.exists(p)):
            return self.send_error(404)
        try:
            os.utime(p, None)      # LRU keys on mtime = "recently played"
        except OSError:
            pass
        size = os.path.getsize(p)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        partial = rng.startswith("bytes=")
        if partial:
            try:
                a, b = (rng[6:].split(",")[0].split("-") + [""])[:2]
                start = int(a) if a else max(0, size - int(b))
                if a:
                    end = min(int(b), size - 1) if b else size - 1
            except ValueError:
                partial = False
                start, end = 0, size - 1
        if start > end or start >= size:
            return self.send_error(416)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/quicktime")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        try:
            with open(p, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass               # client hung up mid-stream — normal for video


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ready = _cached()
    print("ConcordeGo  http://127.0.0.1:%d" % PORT)
    print("  %d night clips available, %d already cached in %s"
          % (len(SKY_SOURCES), len(ready), _sky_dir()))
    if not ready:
        print("  first load warms one clip in the background (a few hundred MB);")
        print("  the canvas skyline carries the page until it lands.")
    Server(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
