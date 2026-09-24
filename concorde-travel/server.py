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
import datetime
import urllib.parse
import urllib.request
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter                                           # noqa: E402
import places                                           # noqa: E402
import live                                              # noqa: E402
import narrator                                          # noqa: E402
import wish                                              # noqa: E402
import rescue                                            # noqa: E402
import scorer                                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "ui")
FIXTURES = os.path.join(HERE, "..", "fixtures")
PORT = int(os.environ.get("CONCORDEGO_PORT", "9897"))
# CONCORDEGO_ROOT=mock-10 serves that mockup at / (the public address does this
# while the mockup is the interface); unset, / is the draft page in ui/.
ROOT = (os.environ.get("CONCORDEGO_ROOT") or "").strip()
# Per-person allowances for anyone who reached us through Cloudflare Access.
# Access puts the signed-in email in a header; nobody without one may spend.
USER_SEARCHES = int(os.environ.get("CONCORDEGO_USER_SEARCHES", "20"))     # metered flight searches a day
# Signed-in people who are not metered and may open /admin: the machine's owner reaching it through the tunnel.
OWNERS = {e.strip().lower() for e in os.environ.get("CONCORDEGO_OWNERS", "").split(",") if e.strip()}
PUBLIC = (os.environ.get("CONCORDEGO_PUBLIC") or "").strip().lower() in ("1", "true", "yes")
# A ceiling on the whole site's day, whoever asks: the per-person allowance bounds one account, this bounds a
# swarm of them. Wishes are the only metered thing that costs money per call (Duffel bills per booking, not per
# search; live.py caps searches on its own), so a day of wishes is at most this many Opus calls.
WISHES_TOTAL = int(os.environ.get("CONCORDEGO_WISHES_TOTAL", "1500"))
# Anyone may run a few real searches a day before signing in (per Patrick, 2026-09-21: never made-up results;
# a few free ones, then the account pop-up or the wait). Counted per address; a round trip is two searches.
ANON_SEARCHES = int(os.environ.get("CONCORDEGO_ANON_SEARCHES", "4"))
# who sells this fare: one SerpApi call each, counted apart from searches (2026-09-24)
ANON_SELLERS = int(os.environ.get("CONCORDEGO_ANON_SELLERS", "5"))
USER_SELLERS = int(os.environ.get("CONCORDEGO_USER_SELLERS", "30"))
# The Zero Trust team, e.g. millerworldindustries: the issuer of the sign-in cookie the server verifies below.
ACCESS_TEAM = os.environ.get("CONCORDEGO_ACCESS_TEAM", "").strip().lower()
# Emails the admin page marks unlimited: signed in, they are not metered. Kept beside users.json.
UNLIMITED_FILE = os.path.join(live.HOME, "unlimited.json")

def _unlimited():
    try:
        with open(UNLIMITED_FILE, encoding="utf-8") as fh:
            return sorted({str(e).strip().lower() for e in json.load(fh) if str(e).strip()})
    except (OSError, ValueError):
        return []

def _unlimited_set(emails):
    live._atomic_write(UNLIMITED_FILE, sorted({e.strip().lower() for e in emails if e.strip()}))
# Photos of the origin and the destination for the shortlist tiles: Pexels (free, 200 an hour, a credit line
# asked). Cached per place for a month, so a place costs one call ever; and at most PHOTO_CALLS uncached
# lookups a day site-wide, so nobody can spend the hour walking the atlas. No key: the stand-in artwork stays.
PEXELS_KEY = os.environ.get("CONCORDEGO_PEXELS_KEY", "")
PHOTO_CALLS = int(os.environ.get("CONCORDEGO_PHOTO_CALLS", "150"))
PHOTO_DIR = os.path.join(live.HOME, "photos")
# Autocomplete for the origin and destination: the offline table first (instant), then Duffel's places (airports
# and cities the feed can actually sell, with the key), then addresses from Nominatim for the origin. External
# answers are cached per query for a week and capped per day site-wide.
SUGGEST_CALLS = int(os.environ.get("CONCORDEGO_SUGGEST_CALLS", "1500"))
SUGGEST_DIR = os.path.join(live.HOME, "suggest")
USER_WISHES = int(os.environ.get("CONCORDEGO_USER_WISHES", "200"))        # model calls a day (cents each)
USERS_FILE = os.path.join(live.HOME, "users.json")
_USERS_LOCK = threading.Lock()


def _users_take(email, kind, limit):
    """Count one call of `kind` against `email` for today. Returns (allowed, left).
    Counters roll over by day on their own, like the global quota."""
    import datetime
    today = datetime.date.today().isoformat()
    with _USERS_LOCK:
        try:
            with open(USERS_FILE, encoding="utf-8") as fh:
                users = json.load(fh)
        except (OSError, ValueError):
            users = {}
        u = users.get(email) or {}
        if u.get("day") != today:
            u = {"day": today, "searches": 0, "wishes": 0, "recent": u.get("recent") or [], "profile": u.get("profile")}
        if u.get(kind, 0) >= limit:
            users[email] = u
            return False, 0
        u[kind] = u.get(kind, 0) + 1
        users[email] = u
        live._atomic_write(USERS_FILE, users)
        return True, limit - u[kind]


def _users_give(email, kind):
    """Hand back one call of `kind` taken today: input refused before any call spends nothing."""
    today = datetime.date.today().isoformat()
    with _USERS_LOCK:
        try:
            with open(USERS_FILE, encoding="utf-8") as fh:
                users = json.load(fh)
        except (OSError, ValueError):
            return
        u = users.get(email) or {}
        if u.get("day") == today and u.get(kind, 0) > 0:
            u[kind] -= 1
            users[email] = u
            live._atomic_write(USERS_FILE, users)


# ----------------------------------------------------------- who is this
# Cloudflare Access only writes Cf-Access-Authenticated-User-Email on requests
# to an application that demanded a sign-in. The free searches, the photos
# and the identity check live on the open part of the site, which Access
# passes through unstamped, so a signed-in person looked anonymous there and
# was sent round to sign in again (2026-09-21). The browser sends Access's own
# cookie, CF_Authorization, on every request to the host: a JWT signed with
# the team's key. It is verified here in the stdlib - RS256 is one modular
# exponentiation and a fixed padding - against the team's published keys,
# cached an hour. Issuer, expiry and signature all have to hold.
_JWKS = {"at": 0.0, "keys": {}}


def _b64u(s):
    import base64
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _access_keys():
    if not ACCESS_TEAM:
        return {}
    if time.time() - _JWKS["at"] > 3600 or not _JWKS["keys"]:
        try:
            req = urllib.request.Request("https://%s.cloudflareaccess.com/cdn-cgi/access/certs" % ACCESS_TEAM,
                                         headers={"User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)"})
            with urllib.request.urlopen(req, timeout=6) as r:
                d = json.loads(r.read().decode("utf-8"))
            keys = {}
            for k in d.get("keys") or []:
                if k.get("kty") == "RSA" and k.get("kid"):
                    keys[k["kid"]] = (int.from_bytes(_b64u(k["n"]), "big"), int.from_bytes(_b64u(k["e"]), "big"))
            if keys:
                _JWKS["keys"] = keys
            _JWKS["at"] = time.time()
        except Exception:
            _JWKS["at"] = time.time() - 3300     # try again in five minutes, keep what we had
    return _JWKS["keys"]


_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def access_email(token):
    """The signed-in email in an Access JWT, or None. Verifies RS256 against
    the team's keys, the issuer and the expiry; nothing else is trusted."""
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(_b64u(h64)); payload = json.loads(_b64u(p64)); sig = int.from_bytes(_b64u(s64), "big")
        if header.get("alg") != "RS256":
            return None
        n, e = _access_keys().get(header.get("kid"), (None, None))
        if not n:
            return None
        klen = (n.bit_length() + 7) // 8
        m = pow(sig, e, n).to_bytes(klen, "big")
        digest = hashlib.sha256((h64 + "." + p64).encode("ascii")).digest()
        expect = b"\x00\x01" + b"\xff" * (klen - 3 - len(_SHA256_PREFIX) - len(digest)) + b"\x00" + _SHA256_PREFIX + digest
        if m != expect:
            return None
        if payload.get("iss") != "https://%s.cloudflareaccess.com" % ACCESS_TEAM:
            return None
        if float(payload.get("exp") or 0) < time.time():
            return None
        email = (payload.get("email") or "").strip().lower()
        return email or None
    except Exception:
        return None


def _users_recent_add(email, item):
    """Remember a signed-in person's search (the trip, not the results), newest
    first, five deep, one entry per distinct trip."""
    key = json.dumps(item, sort_keys=True)
    with _USERS_LOCK:
        try:
            with open(USERS_FILE, encoding="utf-8") as fh:
                users = json.load(fh)
        except (OSError, ValueError):
            users = {}
        u = users.get(email) or {}
        recent = [r for r in (u.get("recent") or []) if json.dumps(r, sort_keys=True) != key]
        u["recent"] = ([item] + recent)[:5]
        users[email] = u
        live._atomic_write(USERS_FILE, users)


def _users_profile(email):
    try:
        with open(USERS_FILE, encoding="utf-8") as fh:
            return ((json.load(fh)).get(email) or {}).get("profile") or None
    except (OSError, ValueError):
        return None


def _users_profile_set(email, profile):
    """The signed-in person's profile: today the flying personality (the quiz's
    answers and the persona they make), kept small and only ever theirs."""
    with _USERS_LOCK:
        try:
            with open(USERS_FILE, encoding="utf-8") as fh:
                users = json.load(fh)
        except (OSError, ValueError):
            users = {}
        u = users.get(email) or {}
        u["profile"] = profile
        users[email] = u
        live._atomic_write(USERS_FILE, users)


def _users_recent(email):
    try:
        with open(USERS_FILE, encoding="utf-8") as fh:
            return ((json.load(fh)).get(email) or {}).get("recent") or []
    except (OSError, ValueError):
        return []


def _hours_to_midnight():
    import datetime
    now = datetime.datetime.now()
    return max(1, round((datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time()) - now).total_seconds() / 3600))


def _in_hours():
    """'in 1 hour' or 'in 6 hours', for the limit messages."""
    h = _hours_to_midnight()
    return "in %d hour%s" % (h, "" if h == 1 else "s")


def _users_left(email):
    import datetime
    today = datetime.date.today().isoformat()
    try:
        with open(USERS_FILE, encoding="utf-8") as fh:
            u = (json.load(fh)).get(email) or {}
    except (OSError, ValueError):
        u = {}
    if u.get("day") != today:
        u = {}
    return {"searches": max(0, USER_SEARCHES - u.get("searches", 0)), "wishes": max(0, USER_WISHES - u.get("wishes", 0)),
            "searches_used": u.get("searches", 0)}
if ROOT and not re.match(r"^mock-[0-9]+$", ROOT):
    sys.exit("CONCORDEGO_ROOT must name a mockup, e.g. mock-10 (got %r)" % ROOT)

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


def _place_short(label):
    """"Shoreditch, London EC2A" -> "London". The city is the part a person
    would say out loud; the postcode is not."""
    parts = [p.strip() for p in label.split(",")]
    pick = parts[1] if len(parts) > 1 else parts[0]
    words = [w for w in pick.split() if not any(c.isdigit() for c in w)]
    return " ".join(words) or pick


def list_fixtures():
    out = []
    for f in _fixture_files():
        d = json.load(open(f, encoding="utf-8"))
        origins, dests = [], []
        for o in d["options"]:
            a = o["segments"][0]["origin"]["iata"]
            b = o["segments"][-1]["destination"]["iata"]
            if a not in origins:
                origins.append(a)
            if b not in dests:
                dests.append(b)
        out.append({"fixture_id": d["fixture_id"], "title": d["title"],
                    "pins_down": d["pins_down"],
                    "origin": d["query"]["origin"]["label"],
                    "destination": d["query"]["destination"]["label"],
                    "origin_short": _place_short(d["query"]["origin"]["label"]),
                    "dest_short": _place_short(d["query"]["destination"]["label"]),
                    "airports": "/".join(origins) + " \u2192 " + "/".join(dests),
                    "options": len(d["options"]),
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
    card = scorer.report_card(sc, opt)                 # the letter on the page (see scorer.report_card)
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
        # The full stamps, because the results list puts every option on ONE
        # clock - a later departure has to actually sit further right, which is
        # the whole point of the timeline and is impossible from "13:09".
        "depart_iso": first["departure_local"],
        "arrive_iso": last["arrival_local"],
        "arrive_local": last["arrival_local"][11:16],
        "day_offset": (last["arrival_local"][:10] != first["departure_local"][:10]),
        "stops": len(opt["segments"]) - 1,
        "bag_included": max(t["entitlements"]["checked_included"] for t in tickets),
        "separate_tickets": len(tickets) > 1,
        "ticket_cents": led.lines[0].amount_cents,
        "effective_cents": led.effective_cents,
        "reference_cents": ref_cents,
        "door_to_door_minutes": led.door_to_door_minutes,
        "grade": card["grade"],
        "card": card,
        "value_grade": letter,
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
        return {"error": "No such fixture: %s" % fid}
    sc = json.load(open(path, encoding="utf-8"))
    return _score_scenario(sc, req)


def _score_scenario(sc, req):
    """One path for fixtures and live inventory alike. If these ever diverge,
    a live search is being judged by different rules than the corpus."""
    profile = req.get("profile") or "reference"
    if profile not in sc["query"]["profiles"]:
        profile = "reference"
    _apply_overrides(sc, req)

    views = [_view(sc, o, profile) for o in sc["options"]]
    kept = [v for v in views if not v["filtered_reason"] and not v["infeasible_reason"]]
    hidden = [v for v in views if v["filtered_reason"] or v["infeasible_reason"]]
    kept.sort(key=lambda v: (v["effective_cents"], v["option_id"]))

    fixture_meta = {"fixture_id": sc["fixture_id"], "title": sc["title"],
                    "origin": sc["query"]["origin"]["label"],
                    "destination": sc["query"]["destination"]["label"],
                    "depart_date": sc["query"]["depart_date"],
                    "route_par_cents": sc["query"]["route_par_cents"],
                    # Hard rule 4 applies to the yardstick too: a letter against
                    # a number nobody can see is hidden ranking logic.
                    "par": sc.get("_par"),
                    "profiles": list(sc["query"]["profiles"])}

    # The deterministic narration rides along with the scores - it costs
    # nothing and means the page never paints an empty box. /api/narrate
    # upgrades it afterwards if a model is reachable.
    b = narrator.brief(fixture_meta, kept, profile)
    return {
        "fixture": fixture_meta,
        "profile": profile,
        "options": kept,
        "hidden": hidden,
        "narration": narrator.narrate(b, allow_model=False),
    }


# Live scenarios are held in memory only. They are never written to fixtures/ -
# a fixture is a hand-authored assertion about behaviour, and a search result is
# neither hand-authored nor an assertion.
_LIVE = {}


def _fetch_raw(req):
    """Resolve what was typed, then fetch: the recording, an inline payload,
    or the provider. Returns (raw, meta, where, src) or ({"error": ...},).
    Shared by /api/live (the draft page) and /api/search (mock 10)."""
    src = req.get("source") or "sample"
    where = {}
    if src == "api":
        # what was typed is checked first, whether or not a call will be spent:
        # a typo is a typo, and the recording standing in should not hide it
        for field, default in (("origin", "NYC"), ("destination", "")):
            typed = req.get(field) or default
            code, how, problem = places.resolve(typed)
            point = None
            # An address on either side ("3820 Planters Watch, Loganville GA", "The
            # Savoy, Strand, London") is geocoded: the point prices the ride at that
            # end, and when the city in it is one the table does not know, the
            # nearest airport or metro to the point is where the flights go.
            if problem or _looks_like_address(typed):
                point = geo_place(typed)
                if problem and point:
                    code, how = place_near(point)
                    problem = None if code else problem
            if problem:
                return ({"error": problem, "field": field, "live": True,
                         "suggest": places.suggest(typed)},)
            if point and code:
                point["city"] = places.label_for(code).split(" (")[0]   # the metro the page names: London, not City of Westminster
            if point and field == "origin":
                import ground as _g
                _g.GEOCODED[typed.strip().lower()] = point   # the ride is priced from this door, not a guess near the airport
            where[field] = {"code": code, "how": how, "typed": typed,
                            "label": places.label_for(code), "point": point}
        d = (req.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return ({"error": "Pick a valid date.",
                     "field": "date", "live": True},)
        if d < time.strftime("%Y-%m-%d", time.gmtime(time.time() - 12 * 3600)):
            # the provider refuses a past date with a 422; say it in a sentence and spend nothing
            return ({"error": "That date has passed. Pick today or later.",
                     "field": "date", "live": True},)
        try:
            datetime.date.fromisoformat(d)
        except ValueError:
            return ({"error": "Pick a valid date.", "field": "date", "live": True},)
        where["date"] = d
        if not live.load_config().get("key"):
            if req.get("_served"):
                return ({"error": "Live search is not set up yet.", "live": True},)
            # a developer's machine with no key: the recording stands in, and says so
            src = "sample"
            req = dict(req, _fell_back=True)
    meta = {}
    if src == "sample":
        want = str(req.get("provider") or live.load_config().get("provider") or "duffel")
        name = {"duffel": "duffel-jfk-lhr.json",
                "amadeus": "amadeus-jfk-lhr.json",
                "kiwi-tequila": "kiwi-jfk-lhr.json"}.get(want, "duffel-jfk-lhr.json")
        path = os.path.join(HERE, "adapter_samples", name)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError as exc:
            return ({"error": "Recorded results missing: %s" % exc},)
        if req.get("_fell_back"):
            meta["fell_back"] = "No flight API key on this machine"
    elif src == "inline":
        raw = req.get("payload") or {}
    elif src == "api":
        q = {"origin": where["origin"]["code"], "destination": where["destination"]["code"],
             "date": where["date"],
             "adults": max(1, min(9, int(req.get("adults", 1)))), "currency": "USD", "limit": 50,
             "cabin": _cabin(req.get("cabin"))}
        raw, meta = live.search(q)
        if raw is None:
            return ({"error": meta.get("error", "Search is unavailable. Try again later."),
                     "hint": meta.get("hint") or meta.get("how"),
                     "quota": meta.get("quota"), "live": True},)
        # the supplement: Google Flights through SerpApi, for the carriers the
        # feed cannot sell (Delta). Its absence or failure never fails the search.
        meta = dict(meta)
        meta["supplement_payload"], meta["supplement"] = _supplement(q)
    else:
        return ({"error": "Unknown source %r. Use sample, inline or api." % src},)
    return raw, meta, where, src


_CABINS = {"economy": "economy", "premium": "premium_economy", "premium_economy": "premium_economy",
           "business": "business", "first": "first"}


def _cabin(word):
    """The page's cabin word -> the provider's. Unknown words are economy, never an error."""
    return _CABINS.get(str(word or "").strip().lower(), "economy")


def _supplement(q):
    """(payload, meta) from live.serp_search for the route, or (None, why)."""
    try:
        scfg = live.serp_config()
        if not scfg.get("key"):
            return None, {"source": "none", "skipped": "no SerpApi key"}
        # Google has no metro codes: NYC comes back empty, JFK,EWR,LGA comes back full
        return live.serp_search(",".join(places.airports_for(q["origin"])),
                                ",".join(places.airports_for(q["destination"])), q["date"],
                                adults=q.get("adults", 1), cfg=scfg, cabin=q.get("cabin"))
    except Exception as exc:                          # a supplement must never take the search down
        return None, {"source": "none", "error": "supplement failed: %s" % exc}


def flight_request(q, remote, headers):
    """GET /flight?number=DL5048&date=YYYY-MM-DD[&origin=LGA&destination=CLT] -> the observed status."""
    number = (q.get("number") or [""])[0]
    date = (q.get("date") or [""])[0][:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "Pick the flight date."}
    if remote:
        ip = (headers.get("Cf-Connecting-Ip") or headers.get("X-Forwarded-For") or "?").split(",")[0].strip()
        ok, _left = _users_take("ip:" + ip, "flights", 12)
        if not ok:
            return {"error": "Daily limit reached: 12 flight lookups. Try again tomorrow."}
    legs, meta = live.flight_status(number, date)
    if legs is None:
        return {"error": meta.get("error") or "Flight lookup unavailable. Enter the times by hand.", "hint": meta.get("hint") or meta.get("how"), "configured": bool(live.adb_config().get("key"))}
    tr = rescue.from_tracking(legs, (q.get("origin") or [""])[0], (q.get("destination") or [""])[0])
    if not tr:
        return {"error": "No flight %s found on %s." % (number.upper(), date) + (" Check the number and date." if legs == [] else ""), "legs": len(legs)}
    return {"tracking": tr, "fetched": meta.get("source"), "age_seconds": meta.get("age_seconds")}


FLEX_PER_DAY = int(os.environ.get("CONCORDEGO_FLEX_PER_DAY") or 40)


def flex_request(req):
    """Nearby days (per Patrick, 2026-09-22: "nearby airports and dates, priced
    from your door"): the same search on the three days either side, each
    through the ordinary pipeline so every day is priced all in from the
    traveller's door, and each day's best under each target. Up to six extra
    provider calls, each cached half an hour; capped site-wide by
    CONCORDEGO_FLEX_PER_DAY sweeps a day."""
    date = str(req.get("date") or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "Pick a trip date."}
    try:
        centre = datetime.date.fromisoformat(date)
        span = max(1, min(3, int(req.get("days") or 3)))
    except (TypeError, ValueError):
        return {"error": "Pick a valid date."}
    ok, _ = _users_take("_flex", "searches", FLEX_PER_DAY)
    if not ok:
        return {"error": "Nearby days: daily limit reached. Try again %s." % _in_hours()}
    today = datetime.date.today()
    days = []
    for k in range(-span, span + 1):
        d = centre + datetime.timedelta(days=k)
        if d < today:
            continue
        r = search_request(dict(req, date=d.isoformat(), source="api", trip={"leg": 1}))
        row = {"date": d.isoformat(), "offset": k, "found": 0}
        if r.get("error") and r.get("field"):
            return {"error": r["error"], "field": r["field"]}   # a typo in the form is the same typo on every day
        if r.get("error"):
            row["error"] = r["error"]
        else:
            row["found"] = r["query"]["total_found"]
            for prof in ("cheapest", "fastest", "comfort", "reference"):
                lst = (r.get("results") or {}).get(prof) or []
                if lst:
                    e = lst[0]
                    row[prof] = {"effective_cents": e["by"][prof]["effective_cents"] if prof in e.get("by", {}) else e["effective_cents"], "total_cents": sum(l["cents"] for l in (e.get("by", {}).get(prof) or e)["lines"] if l["code"] in ("ticket", "bags", "ground_out", "ground_in")),
                                 "ticket_cents": e["ticket_cents"], "carrier": e["carrier"], "flight": e["flight"], "grade": e["grade"]}
        days.append(row)
    return {"days": days, "centre": date, "note": "Best flight each day, rides and bags included."}


def rescue_request(req):
    """The delayed-or-cancelled helper: the situation in, the call out. One
    live search today (and tomorrow when the evening is gone), the arithmetic
    in rescue.py, and the advice from the model behind the guard rails."""
    sit = dict(req.get("situation") or {})
    o, d = str(sit.get("origin") or "").strip(), str(sit.get("destination") or "").strip()
    if not (o and d):
        return {"error": "Enter where you are flying from and to."}
    date = str(sit.get("date") or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return {"error": "Pick the flight date."}
    now = None
    try:
        now = datetime.datetime.fromisoformat(str(req.get("now") or ""))
    except ValueError:
        pass
    dates = [date]
    if now is not None and now.hour >= 17:
        dates.append((now + datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
    found, errors, feed, search = [], [], None, None
    for dt in dates:
        r = search_request({"source": "api", "origin": o, "origin_address": o, "destination": d, "date": dt,
                            "adults": 1, "checked_bags": 1 if sit.get("bags_checked") else 0, "_served": req.get("_served"),
                            "cabin": {"premium": "premium", "business": "business", "first": "first"}.get(str(sit.get("fare") or ""), "economy")})
        if r.get("error"):
            errors.append(r["error"])
            continue
        feed = r.get("feed")
        if search is None:
            search = r                                # the day's full results, so the page can show them as a search
        found.extend(r["results"]["reference"])
    if not found:
        return {"error": errors[0] if errors else "No flights found for this date."}
    # the countries the rules turn on, from the curated airports where we know them
    enr = adapter.load_enrichment()
    aps = enr["airports"]["airports"]
    # the typed text is often a city ("New York"): resolve it, then take the first curated airport of the metro
    pick = lambda typed: next((aps[a] for a in places.airports_for(places.resolve(typed)[0] or typed) if a in aps), {})
    o_ap, d_ap = pick(o), pick(d)
    sit.setdefault("us", (o_ap.get("border") == "us") or (d_ap.get("border") == "us"))
    sit.setdefault("eu", o_ap.get("border") in ("schengen", "ie"))
    sit.setdefault("international", bool(o_ap.get("border") and d_ap.get("border") and o_ap.get("border") != d_ap.get("border")))
    # the clocks the traveller typed are read in the airport's own zone, taken from the results, never the browser's
    dep_off, arr_off = found[0]["depart"][-6:], found[0]["arrive"][-6:]
    if now is not None and now.tzinfo is not None:    # the hours on the page are the airport's, never the browser's
        now = now.astimezone(datetime.datetime.fromisoformat(found[0]["depart"]).tzinfo)
    def clock(txt, off):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(\+1)?\s*$", str(txt or ""))
        if not m:
            return None
        day = datetime.date.fromisoformat(date) + datetime.timedelta(days=1 if m.group(3) else 0)
        return "%sT%02d:%02d:00%s" % (day.isoformat(), int(m.group(1)), int(m.group(2)), off)
    sched, newdep = clock(sit.get("sched_clock"), dep_off), clock(sit.get("new_dep_clock"), dep_off)
    if sched and newdep and datetime.datetime.fromisoformat(newdep) < datetime.datetime.fromisoformat(sched) - datetime.timedelta(hours=6):
        newdep = (datetime.datetime.fromisoformat(newdep) + datetime.timedelta(days=1)).isoformat()   # 11:30 PM delayed to 1:15 AM leaves tomorrow, +1 or not
    if newdep:
        sit["new_depart"] = newdep
    if sched and newdep:
        sit["delay_minutes"] = max(0, int((datetime.datetime.fromisoformat(newdep) - datetime.datetime.fromisoformat(sched)).total_seconds() // 60))
    newarr, rbarr = clock(sit.get("new_arr_clock"), arr_off), clock(sit.get("rebook_arr_clock"), arr_off)
    if newarr:
        sit["new_arrive"] = newarr
    if rbarr:
        sit["rebook_arrive"] = rbarr
    if o_ap.get("lat") is not None and d_ap.get("lat") is not None:
        import ground as _g
        sit.setdefault("distance_km", int(_g.haversine_km(o_ap["lat"], o_ap["lon"], d_ap["lat"], d_ap["lon"])))
    sit["route"] = "%s to %s" % (o, d)
    sit["now_clock"] = now.strftime("%H:%M") if now else None
    tr = sit.get("tracking") if isinstance(sit.get("tracking"), dict) and sit["tracking"].get("observed") else None
    if tr is None and sit.get("flight") and live.adb_config().get("key"):
        legs, _m = live.flight_status(sit["flight"], date)
        tr = rescue.from_tracking(legs, o, d) if legs else None
    if tr:
        sit = rescue.apply_tracking(sit, tr)
    # the airports' coordinates for the night's rides: the curated table, then whatever the feed named
    airports = {k: {"iata": k, "lat": v.get("lat"), "lon": v.get("lon"), "country": v.get("country")} for k, v in aps.items() if v.get("lat") is not None}
    try:
        got = _fetch_raw({"source": "api", "origin": o, "destination": d, "date": date, "adults": 1, "_served": req.get("_served"),
                         "cabin": {"premium": "premium", "business": "business", "first": "first"}.get(str(sit.get("fare") or ""), "economy")})
        if len(got) == 4:
            for k, g in adapter.duffel_geo(got[0]).items():
                if g.get("lat") is not None:
                    airports.setdefault(k, {"iata": k, "lat": g["lat"], "lon": g["lon"], "country": g.get("country")})
    except Exception:
        pass
    a = rescue.assess(sit, found, now=now, airports=airports, hotels=rescue.load_hotels())
    b = rescue.brief(sit, a)
    prose = rescue.narrate(b, allow_model=bool(os.environ.get("ANTHROPIC_API_KEY")))
    return {"assessment": a, "advice": prose, "searched": dates, "feed": feed, "found": len(found), "notes": errors,
            "search": search}


def _round_trip_totals(req, where):
    """The return leg of a round trip, priced as round-trip tickets with the outbound already chosen
    (2026-09-24, per Patrick: two one-ways can cost more than the round trip on the same airline). Returns
    {"outbound_cents", "totals": {return itinerary key: round-trip total in cents}, "sources"} or None.

    Google prices it in two calls: the outbounds, then the returns for the chosen outbound by its token, a
    reply of a few dozen flights. The feed is NOT asked: a two-slice request returns every outbound-and-return
    pair at once, and on 2026-09-24 that reply grew past what the 453 MB droplet could hold and the kernel
    killed the service. `req["pair"]` names the outbound and comes from the browser, so every field is checked."""
    pair = req.get("pair") if isinstance(req.get("pair"), dict) else None
    if not pair:
        return None
    fns = [str(x).replace(" ", "").upper() for x in (pair.get("flights") or [])][:6]
    o_ap, d_ap = str(pair.get("origin") or "").upper(), str(pair.get("destination") or "").upper()
    out_date, dep = str(pair.get("date") or "")[:10], str(pair.get("depart") or "")[:16]
    try:
        p_out = int(pair.get("ticket_cents"))
    except (TypeError, ValueError):
        return None
    if not (fns and all(re.match(r"^[A-Z0-9]{2}\d{1,4}$", f) for f in fns) and re.match(r"^[A-Z]{3}$", o_ap)
            and re.match(r"^[A-Z]{3}$", d_ap) and re.match(r"^\d{4}-\d{2}-\d{2}$", out_date)
            and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", dep) and 0 < p_out < 10 ** 8):
        return None
    back_date, ret_o, ret_d = where["date"], where["origin"]["code"], where["destination"]["code"]
    adults = max(1, min(9, int(req.get("adults", 1) or 1)))
    payload, meta = live.serp_round_trip(",".join(places.airports_for(ret_d)), ",".join(places.airports_for(ret_o)),
                                         out_date, back_date, fns, adults=adults, cabin=_cabin(req.get("cabin")))
    totals = {}
    for grp in ("best_flights", "other_flights"):
        for it in (payload or {}).get(grp) or []:
            fl = it.get("flights") or []
            if not fl or it.get("price") is None:
                continue
            key = adapter.serp_itin_key(it)      # the key the adapter gives the same flights
            cents = int(round(float(it["price"]) * 100))
            totals[key] = min(totals.get(key, cents), cents)
    return {"outbound_cents": p_out, "totals": totals, "sources": ["Google Flights"]} if totals else None


def sellers_request(req):
    """Who sells this fare (2026-09-24, per Patrick): Google's booking options for one flight, the airline's own
    price first and every travel agency after it, flagged as Google flags them. One SerpApi call, cached; the
    page asks only when someone opens a flight and taps the button. The token and the query come from the page,
    so each is checked here before anything is spent."""
    tok = str(req.get("token") or "")
    q = req.get("query") if isinstance(req.get("query"), dict) else {}
    fns = [str(x).replace(" ", "").upper() for x in (req.get("flights") or [])][:6]
    dep, arr = str(q.get("departure_id") or "").upper(), str(q.get("arrival_id") or "").upper()
    date = str(q.get("outbound_date") or "")[:10]
    try:
        adults = max(1, min(9, int(q.get("adults") or 1)))
    except (TypeError, ValueError):
        adults = 1
    if not (re.match(r"^[A-Za-z0-9_\-=+/]{20,4000}$", tok) and re.match(r"^[A-Z]{3}(,[A-Z]{3}){0,7}$", dep)
            and re.match(r"^[A-Z]{3}(,[A-Z]{3}){0,7}$", arr) and re.match(r"^\d{4}-\d{2}-\d{2}$", date)
            and fns and all(re.match(r"^[A-Z0-9]{2}\d{1,4}$", f) for f in fns)):
        return {"error": "This flight cannot be checked.", "field": "sellers"}
    if not live.serp_config().get("key"):
        return {"error": "Seller checks are not set up on this machine.", "field": "sellers"}
    payload, meta = live.serp_booking_options({"departure_id": dep, "arrival_id": arr, "outbound_date": date,
                                               "adults": adults, "cabin": _cabin(q.get("cabin"))}, tok)
    if payload is None:
        return {"error": "Google did not answer. Try again in a minute.", "detail": meta.get("error")}
    sellers = []
    for o in payload.get("booking_options") or []:
        t = o.get("together") if isinstance(o.get("together"), dict) else None
        if not t or t.get("price") is None:
            continue                          # sold as separate tickets per direction: not this one-way fare
        sold = [str(x).replace(" ", "").upper() for x in t.get("marketed_as") or []]
        req_b = t.get("booking_request") or {}
        url = str(req_b.get("url") or "")
        sellers.append({"name": str(t.get("book_with") or "")[:60], "airline": bool(t.get("airline")),
                        # the fare type when Google names it ("Basic Economy"): one airline often sells three
                        "fare": str(t.get("option_title") or "")[:40],
                        "cents": int(round(float(t["price"]) * 100)),
                        # the same flights under the same numbers; a codeshare sold under other numbers is noted
                        "same_flights": not sold or sold == fns,
                        "go": {"url": url, "post": str(req_b.get("post_data") or "")[:4000]} if url.startswith("https://www.google.com/") else None})
    if not sellers:
        return {"error": "Google lists no seller for this flight right now."}
    same = [x for x in sellers if x["same_flights"]] or sellers
    air = [x for x in same if x["airline"]]
    agencies = sorted([x for x in same if not x["airline"]], key=lambda x: x["cents"])
    sellers.sort(key=lambda x: (not x["airline"], not x["same_flights"], x["cents"]))
    return {"sellers": sellers,
            "airline": min(air, key=lambda x: x["cents"]) if air else None,
            "cheapest_agency": agencies[0] if agencies else None,
            "fetched": meta.get("source")}


def search_request(req):
    """Mock 10's search: the same fetch as /api/live, then everything the page
    reads (data.build_slim: the pool, the ledgers per target, the 66-point
    grid, the feed's own facts). One-way today: the outbound date only."""
    got = _fetch_raw(req)
    if len(got) == 1:
        return got[0]
    raw, meta, where, src = got
    sys.path.insert(0, os.path.join(UI, "mock"))
    import data as mockdata                                   # noqa: E402  (imports server, so it is loaded here, not at the top)
    origin_text = (req.get("origin_address") or req.get("origin") or "bushwick-brooklyn")
    dest_point = (where.get("destination") or {}).get("point") if src == "api" else None
    rt = None
    if src == "api" and req.get("pair"):
        try:
            rt = _round_trip_totals(req, where)
        except Exception:
            rt = None                        # a round-trip price is a bonus; its failure never fails the search
    try:
        out = mockdata.build_slim(raw, origin_key=origin_text, checked_bags=max(0, min(9, int(req.get("checked_bags", 1)))),
                                  origin_full=req.get("origin_address") or None,
                                  dest_point=dest_point,
                                  destination_full=(req.get("destination") if dest_point else None),
                                  supplement=meta.get("supplement_payload"), round_trip=rt)
    except ValueError as exc:
        return {"error": "No flights found for this date.", "detail": str(exc)}
    if not out["results"]["reference"]:
        return {"error": "No flights found for this route and date. Try another date or airport."}
    out["feed"] = {"source": src, "fetched": meta.get("source"), "age_seconds": meta.get("age_seconds"),
                   "quota": meta.get("quota"), "fell_back": meta.get("fell_back"),
                   "supplement": {k: v for k, v in (meta.get("supplement") or {}).items()
                                  if k in ("source", "error", "skipped", "age_seconds", "hint")}}
    out["where"] = where or None
    if src == "api" and meta.get("supplement_payload"):
        # what the seller check must repeat to Google with a flight's booking token: the supplement's own query
        out["seller_query"] = {"departure_id": ",".join(places.airports_for(where["origin"]["code"])),
                               "arrival_id": ",".join(places.airports_for(where["destination"]["code"])),
                               "outbound_date": where["date"], "adults": max(1, min(9, int(req.get("adults", 1) or 1))),
                               "cabin": _cabin(req.get("cabin"))}
    if rt:
        out["round_trip"] = {"outbound_cents": rt["outbound_cents"], "sources": rt["sources"],
                             "applied": len({e["id"] for v in out["results"].values() if isinstance(v, list) for e in v if e.get("round_trip")})}
    try:
        cheapest = min((e["ticket_cents"] for e in out["results"]["reference"]), default=None)
        out["signal"] = adapter.price_signal(meta.get("supplement_payload"), cheapest) if src == "api" else None
    except Exception:
        out["signal"] = None
    if src == "sample":
        out["source_note"] = ("Recorded JFK–LHR results, not a live search" +
                              (". " + meta["fell_back"] if meta.get("fell_back") else "") + ".")
    if not out.get("_provenance"):
        out["_provenance"] = {}
    out["_provenance"]["live"] = src == "api"
    return out


def live_request(req):
    """Normalise a recorded or live payload into a scenario, then score it with
    exactly the same code path the fixtures use."""
    got = _fetch_raw(req)
    if len(got) == 1:
        return got[0]
    raw, _LIVE_META, where, src = got

    origin_text = (req.get("origin_address") or req.get("origin_key")
                   or req.get("origin") or "bushwick-brooklyn")
    sc = adapter.from_feed(raw, origin_key=origin_text,
                           checked_bags=int(req.get("checked_bags", 1)),
                           dest_point=(where.get("destination") or {}).get("point") if src == "api" else None)
    if sc.get("error"):
        return sc
    if _LIVE_META.get("supplement_payload"):
        sc = adapter.merge_scenarios(sc, adapter.from_serpapi(
            _LIVE_META["supplement_payload"], origin_key=origin_text,
            checked_bags=int(req.get("checked_bags", 1)), geo=adapter.duffel_geo(raw),
            dest_point=(where.get("destination") or {}).get("point"), carriers=live.serp_want()), "Google Flights")
    _LIVE[sc["fixture_id"]] = sc
    out = _score_scenario(sc, req)
    out["coverage"] = adapter.coverage(sc)
    out["live"] = True
    out["feed"] = {"source": src,
                   "provider": (sc.get("_feed") or {}).get("provider", "kiwi")}
    out["where"] = where or None
    out["ground"] = sc.get("_ground")

    # With no key the inventory is a RECORDING of one route, so someone asking
    # for Paris to Tokyo gets JFK to London and no hint that they did. Only the
    # ground leg actually responded to what they typed. Say so, rather than
    # letting a confident-looking page answer a question nobody asked.
    if src == "sample" and (req.get("destination") or req.get("origin")):
        first = (sc.get("options") or [{}])[0]
        segs = first.get("segments") or []
        if segs:
            actual = "%s-%s" % (segs[0]["origin"]["iata"], segs[-1]["destination"]["iata"])
            asked_o, _, _ = places.resolve(req.get("origin") or "")
            asked_d, _, _ = places.resolve(req.get("destination") or "")
            codes = {segs[0]["origin"]["iata"], segs[-1]["destination"]["iata"]}
            mismatch = bool(asked_d) and not any(places.covers(asked_d, c) for c in codes)
            out["sample_notice"] = (
                "Recorded %s results, not your route. Add a Duffel "
                "key to search it." % actual
                if mismatch else
                "Recorded %s results. Add a Duffel key for live results." % actual)
    if src == "api":
        out["feed"].update({"fetched": _LIVE_META.get("source"),
                            "age_seconds": _LIVE_META.get("age_seconds"),
                            "quota": _LIVE_META.get("quota")})
    return out


def wish_request(req):
    """One sentence from the wish box -> rules from the page's vocabulary,
    chosen by the model and re-validated here. The page keeps its own pattern
    parser for when this answers 'fallback'."""
    airlines = [{"code": str(a.get("code", ""))[:3].upper(), "name": str(a.get("name", ""))[:60]}
                for a in (req.get("airlines") or [])[:80] if isinstance(a, dict)]
    prior = [str(t)[:200] for t in (req.get("prior") or [])[:8]]
    return wish.parse(str(req.get("text") or "")[:500], airlines, prior)


def narrate_request(req):
    """Prose over an already-computed ledger. Never re-ranks - it scores the
    same request and narrates the order it was given."""
    scored = score_request(req)
    if "error" in scored:
        return scored
    b = narrator.brief(scored["fixture"], scored["options"], scored["profile"])
    return narrator.narrate(b, allow_model=True)



# ----------------------------------------------------------------- admin
# One page for the person who runs this: which commit is live, whether GitHub
# has moved, a button to update now, and the logs. Update means `git reset
# --hard origin/<branch>` in the checkout the server runs from, then exiting:
# the supervisor (systemd's Restart=always on the droplet, launchd's KeepAlive
# on a Mac) brings the new code up. Gated to the local owner and to the emails
# in CONCORDEGO_OWNERS; through the tunnel Access fronts /admin as well.
REPO_DIR = os.path.dirname(HERE)
STARTED = time.time()
LOG_FILE = os.environ.get("CONCORDEGO_LOG", "")
# System updates (per Patrick, 2026-09-24): a root timer checks nightly and writes SYS_STATUS; the admin page
# asks for an update by writing one word to SYS_REQUEST, which a root .path unit acts on (deploy/install-sysupdate.sh).
# This process never runs anything as root and never touches apt.
SYS_STATUS = os.environ.get("CONCORDEGO_SYS_STATUS", "/var/lib/concordego-sys/status.json")
SYS_REQUEST = os.environ.get("CONCORDEGO_SYS_REQUEST", "/var/lib/concordego/sysupdate-request")
SYS_LOGS = "/var/log/update-system"


def _git(*args, timeout=40):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", REPO_DIR] + list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:
        return 1, "", "%s: %s" % (type(exc).__name__, exc)


def _users_today():
    today = time.strftime("%Y-%m-%d")
    try:
        with open(USERS_FILE) as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for email, u in (data or {}).items():
        if isinstance(u, dict) and u.get("day") == today and "@" in email:
            out.append({"email": email, "searches": u.get("searches", 0), "wishes": u.get("wishes", 0)})
    return sorted(out, key=lambda x: -(x["searches"] + x["wishes"]))


_STAMP = {"at": 0.0, "build": "", "updated": ""}


def _stamp(body):
    """The footer's build and date, from git HEAD, so a served page says what is
    running. Cached a minute: the updater changes it, nothing else does."""
    if time.time() - _STAMP["at"] > 60:
        rc, head, _ = _git("log", "-1", "--format=%h%x09%cs")
        if rc == 0 and "\t" in head:
            _STAMP["build"], _STAMP["updated"] = head.split("\t", 1)
        _STAMP["at"] = time.time()
    if not _STAMP["build"]:
        return body
    b, u = _STAMP["build"].encode(), _STAMP["updated"].encode()
    body = re.sub(rb'data-build="[^"]*">[^<]*</code>', b'data-build="' + b + b'">' + b + b'</code>', body)
    body = re.sub(rb'data-updated="[^"]*">[^<]*</time>', b'data-updated="' + u + b'">' + u + b'</time>', body)
    return body


def sys_status():
    """What the nightly check found, for the admin page. The file is written by root; a box without the
    units installed (a Mac, a fresh droplet) reports itself unavailable rather than up to date."""
    # available only when the root side is installed: a request nobody acts on would lock the buttons
    out = {"available": os.path.isdir(os.path.dirname(SYS_REQUEST)) and os.path.exists(SYS_STATUS)
                        and os.path.exists("/etc/systemd/system/concordego-sysupdate.path"),
           "kernel": os.uname().release, "requested": os.path.exists(SYS_REQUEST)}
    # the .path unit picks a request up within a second; one still lying there after ten minutes was never
    # going to be, so it is cleared rather than holding the buttons as "already running"
    try:
        if out["requested"] and time.time() - os.path.getmtime(SYS_REQUEST) > 600:
            os.remove(SYS_REQUEST)
            out["requested"] = False
    except OSError:
        pass
    try:
        with open(SYS_STATUS) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return out
    out.update({k: st.get(k) for k in ("checked_at", "pending", "pending_count", "deferred", "restart_likely", "reboot_waiting", "state", "last_run")})
    # the file is only rewritten by the next check; after a reboot the flag it copied is gone, so trust the box
    if not os.path.exists("/var/run/reboot-required"):
        out["reboot_waiting"] = []
    # a run that died without finishing (the box went down mid-run) must not lock the buttons forever
    lr = out.get("last_run") or {}
    if out.get("state") == "running":
        try:
            started = datetime.datetime.strptime(lr.get("started_at") or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            if (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() > 3 * 3600:
                out["state"] = "stale"
        except ValueError:
            out["state"] = "stale"
    out["restart"] = bool(out.get("reboot_waiting") or out.get("restart_likely"))
    return out


def sys_update(mode):
    """The admin page's two buttons. Writes one word for the root unit; installs nothing itself."""
    if mode not in ("update", "reboot"):
        return {"error": "Unknown update type."}
    st = sys_status()
    if not st.get("available"):
        return {"error": "System updates are not set up on this machine."}
    if st.get("state") == "running" or st.get("requested"):
        return {"error": "An update is already running."}
    tmp = SYS_REQUEST + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(mode)
    os.replace(tmp, SYS_REQUEST)
    return {"ok": True, "mode": mode}


def admin_status(fetch=False):
    out = {"repo": REPO_DIR, "uptime_s": int(time.time() - STARTED), "pid": os.getpid(), "python": sys.version.split()[0],
           "owners": sorted(OWNERS), "log_source": None, "now": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    rc, branch, err = _git("rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = branch if rc == 0 else None
    if rc != 0:
        out["git_error"] = err
    rc, head, _ = _git("log", "-1", "--format=%h%x09%cI%x09%s")
    if rc == 0 and head:
        h, d, sub = (head.split("\t") + ["", ""])[:3]
        out["head"] = {"short": h, "date": d, "subject": sub}
    if fetch and out["branch"]:
        rc, _, err = _git("fetch", "-q", "origin", out["branch"], timeout=90)
        out["fetched"] = rc == 0
        out["fetch_error"] = None if rc == 0 else live.redact(err)
    if out["branch"]:
        rc, n, _ = _git("rev-list", "--count", "HEAD..origin/%s" % out["branch"])
        out["behind"] = int(n) if rc == 0 and n.isdigit() else None
        rc, log, _ = _git("log", "--format=%h%x09%cI%x09%s", "HEAD..origin/%s" % out["branch"])
        out["incoming"] = [dict(zip(("short", "date", "subject"), (l.split("\t") + ["", ""])[:3])) for l in log.splitlines() if l] if rc == 0 else []
    fh = os.path.join(REPO_DIR, ".git", "FETCH_HEAD")
    out["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(os.path.getmtime(fh))) if os.path.exists(fh) else None
    try:
        st = live.status()
        out["live"] = {"provider": st.get("provider"), "key_configured": st.get("key_configured"), "ready": st.get("ready"), "quota": st.get("quota")}
    except Exception as exc:
        out["live"] = {"error": str(exc)}
    out["narrator"] = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    out["users_today"] = _users_today()
    out["allowances"] = {"searches": USER_SEARCHES, "wishes": USER_WISHES}
    out["unlimited"] = _unlimited()
    import shutil
    out["log_source"] = "journal" if shutil.which("journalctl") else (LOG_FILE or None)
    out["system"] = sys_status()
    return out


def photos_request(q):
    """place -> a handful of landscape photos with credits, from Pexels, cached."""
    place = re.sub(r"[^A-Za-z0-9 ,'\-]", "", (q.get("place") or [""])[0]).strip()[:60]
    if not place:
        return {"photos": [], "reason": "no place"}
    if not PEXELS_KEY:
        return {"photos": [], "reason": "no key"}
    slug = re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")
    os.makedirs(PHOTO_DIR, exist_ok=True)
    path = os.path.join(PHOTO_DIR, slug + ".json")
    try:
        if time.time() - os.path.getmtime(path) < 30 * 86400:
            with open(path) as f:
                return json.load(f)
    except OSError:
        pass
    ok, _ = _users_take("_photos", "photos", PHOTO_CALLS)
    if not ok:
        return {"photos": [], "reason": "Daily photo limit reached."}
    # The place, not its people (per Patrick, 2026-09-21: a doll's face and somebody's baby came back for
    # Bushwick). Pexels has no subject filter, so the query names the subject and the photo's own alt text
    # is read: anything describing a person, a face, a costume or a pet is left out.
    from urllib.parse import quote
    PEOPLE = re.compile(r"\b(man|men|woman|women|girl|boy|child|children|kid|kids|baby|infant|toddler|people|person|couple|"
                        r"family|portrait|face|faces|selfie|model|bride|groom|wedding|doll|mask|costume|halloween|makeup|"
                        r"dog|cat|puppy|kitten|pet|smil\w*|posing|crowd|fashion|dress|hair|tattoo)\b", re.I)
    seen, photos = set(), []
    for subject in ("skyline", "street", "architecture", "landmark"):
        url = "https://api.pexels.com/v1/search?query=%s&orientation=landscape&per_page=8" % quote("%s %s" % (place, subject))
        req = urllib.request.Request(url, headers={"Authorization": PEXELS_KEY, "User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            if not photos:
                return {"photos": [], "reason": "Pexels did not respond: %s" % live.redact(str(exc), PEXELS_KEY)}
            break
        for p in d.get("photos") or []:
            if not p.get("src") or p.get("id") in seen or PEOPLE.search(p.get("alt") or ""):
                continue
            seen.add(p.get("id"))
            photos.append({"src": p["src"].get("large2x") or p["src"].get("large"), "credit": "%s / Pexels" % p.get("photographer", ""),
                           "url": p.get("url", ""), "avg_color": p.get("avg_color"), "alt": (p.get("alt") or "")[:120]})
        if len(photos) >= 16:
            break
    out = {"place": place, "photos": photos[:16]}
    try:
        with open(path, "w") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


def _looks_like_address(text):
    """A digit, or three parts, says street address rather than a city name:
    '842 Bushwick Ave', 'The Savoy, Strand, London'. 'Paris, France' is not one."""
    t = (text or "").strip()
    return bool(re.search(r"\d", t)) or t.count(",") >= 2


def _geo_tries(t):
    """Nominatim misses a house it does not hold ("3820 Planters Watch,
    Loganville, GA" -> nothing), so a search is repeated from the end of the
    address inward: the street without its number, then the town. A town-level
    point still prices the ride to within a few dollars. (parts, queries)."""
    parts = [x.strip() for x in t.split(",") if x.strip()] or [t]
    tries = [t]
    words = parts[0].split(" ")
    if len(parts) > 1 and len(words) > 1 and words[0].isdigit():
        tries.append(", ".join([" ".join(words[1:])] + parts[1:]))
    if len(parts) > 1:
        tries.append(", ".join(parts[1:]))
    if len(parts) > 2:
        tries.append(", ".join(parts[2:]))
    out, seen = [], set()
    for q in tries:
        if q.lower() not in seen:
            seen.add(q.lower()); out.append(q)
    return parts, out


def geo_place(text):
    """What was typed -> {lat, lon, label, city, cc} through Nominatim, cached
    a month, or None. The label is the short address the ledger prints
    ('3820 Planters Watch, Loganville'); the city is what the page names."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) < 3:
        return None

    def ask(q):
        from urllib.parse import quote
        req = urllib.request.Request("https://nominatim.openstreetmap.org/search?format=jsonv2&addressdetails=1&limit=1&q=" + quote(q),
                                     headers={"User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception:
            return []
        out = []
        for hit in d or []:
            a = hit.get("address") or {}
            try:
                lat, lon = float(hit["lat"]), float(hit["lon"])
            except (KeyError, ValueError, TypeError):
                continue
            city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county") or ""
            out.append({"lat": lat, "lon": lon, "label": _address_text(a) or q, "city": city,
                        "cc": (a.get("country_code") or "").upper(), "precision": "address"})
        return out

    def fetch():
        parts, tries = _geo_tries(t)
        for i, q in enumerate(tries):
            rows = ask(q)
            if rows:
                if i:
                    rows[0]["precision"] = "street" if i == 1 and len(tries) > 2 else "town"
                    rows[0]["label"] = parts[0] + ", " + rows[0]["label"] if rows[0]["label"] != q else t
                return rows
            time.sleep(1.05)                         # Nominatim's one-a-second rule
        return []
    rows = _lookup_cached("geo", t, 30 * 86400, fetch)
    return rows[0] if rows else None


def place_near(point):
    """(code, how) for the airport or metro a geocoded point flies through:
    the city named in the address when the table knows it, else what the
    provider lists within 150 km of the point (a city entry first, so every
    airport there is weighed), else the nearest curated airport. (None, 'unknown')
    when nothing is close enough, and the caller keeps its sentence."""
    if not point:
        return None, "unknown"
    code, how, problem = places.resolve(point.get("city") or "")
    if code and not problem:
        return code, "address"
    cfg = live.load_config()
    if cfg.get("key") and cfg.get("provider") == "duffel":
        key = "%.2f,%.2f" % (point["lat"], point["lon"])

        def fetch():
            req = urllib.request.Request("https://api.duffel.com/places/suggestions?lat=%.4f&lng=%.4f&rad=150000" % (point["lat"], point["lon"]),
                                         headers={"Authorization": "Bearer " + cfg["key"], "Duffel-Version": "v2", "Accept": "application/json",
                                                  "User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)"})
            try:
                with urllib.request.urlopen(req, timeout=6) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception:
                return []
            out = []
            for pl in d.get("data") or []:
                if pl.get("iata_code") and pl.get("latitude") is not None:
                    city = pl.get("city") if isinstance(pl.get("city"), dict) else {}
                    out.append({"code": pl["iata_code"], "kind": pl.get("type") or "airport",
                                "city_code": (city or {}).get("iata_code") or pl.get("iata_city_code") or "",
                                "lat": pl["latitude"], "lon": pl["longitude"]})
            return out
        import ground as _g
        near = _lookup_cached("near", key, 30 * 86400, fetch)
        near.sort(key=lambda p: _g.haversine_km(point["lat"], point["lon"], p["lat"], p["lon"]))
        cities = [p for p in near if p["kind"] == "city"]
        if cities:
            return cities[0]["code"], "near"
        if near:
            # the nearest airport's METRO code when it has one: an address near
            # City airport is a London search, every airport weighed, not an LCY one
            return (near[0].get("city_code") or near[0]["code"]), "near"
    import ground as _g
    best, dist = None, 150.0
    for iata, ap in (adapter.load_enrichment()["airports"]["airports"] or {}).items():
        if ap.get("lat") is None:
            continue
        km = _g.haversine_km(point["lat"], point["lon"], ap["lat"], ap["lon"])
        if km < dist:
            best, dist = iata, km
    return (best, "near") if best else (None, "unknown")


def _address_text(a):
    road = " ".join(x for x in (a.get("house_number"), a.get("road")) if x) or a.get("pedestrian") or a.get("footway") or ""
    hood = a.get("neighbourhood") or a.get("suburb") or a.get("quarter") or a.get("city_district") or a.get("borough") or ""
    city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county") or ""
    parts = []
    for x in (road, hood, city):
        if x and x not in parts:
            parts.append(x)
    return ", ".join(parts)


def _lookup_cached(kind, key, ttl, fetch):
    """A small JSON cache on disk for the lookups above; the day's cap counts only the misses."""
    os.makedirs(SUGGEST_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:60] + "-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    path = os.path.join(SUGGEST_DIR, "%s-%s.json" % (kind, slug))
    try:
        if time.time() - os.path.getmtime(path) < ttl:
            with open(path) as f:
                return json.load(f)
    except OSError:
        pass
    ok, _ = _users_take("_suggest", "suggest", SUGGEST_CALLS)
    if not ok:
        return []
    out = fetch()
    if not out:
        return out                                    # a miss (or an outage) is not worth remembering for a month
    try:
        with open(path, "w") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


def suggest_request(q):
    """q, kind=from|to -> rows of {value, label, sub, kind}. The value is what
    goes in the field, and always resolves: 'Honolulu (HNL)', or an address."""
    text = (q.get("q") or [""])[0].strip()[:80]
    kind = (q.get("kind") or ["to"])[0]
    cc = re.sub(r"[^a-z]", "", (q.get("cc") or [""])[0].lower())[:2]   # the visitor's country, from the browser's locale
    if len(text) < 2:
        return {"rows": []}
    rows = places.search(text, 5)
    seen = {r["code"] for r in rows}
    cfg = live.load_config()
    if cfg.get("key") and cfg.get("provider") == "duffel" and len(text) >= 2:
        def fetch():
            from urllib.parse import quote
            req = urllib.request.Request("https://api.duffel.com/places/suggestions?query=" + quote(text),
                                         headers={"Authorization": "Bearer " + cfg["key"], "Duffel-Version": "v2", "Accept": "application/json",
                                                  "User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)"})
            try:
                with urllib.request.urlopen(req, timeout=6) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception:
                return []
            out = []
            for pl in d.get("data") or []:
                code = pl.get("iata_code") or ""
                if not code:
                    continue
                name = pl.get("name") or code
                sub = pl.get("type") == "airport" and (pl.get("city_name") or "") or ""
                out.append({"value": "%s (%s)" % (name, code), "label": name, "sub": " · ".join(x for x in (sub, pl.get("iata_country_code") or "") if x),
                            "code": code, "kind": pl.get("type") or "airport"})
            return out[:8]
        for r in _lookup_cached("duffel", text, 7 * 86400, fetch):
            if r["code"] not in seen:
                seen.add(r["code"]); rows.append(r)
    # a digit means an address or a postal code (EC2A, 11237, 361 Harman), which no airport name carries;
    # those go to the address service, and come back ahead of the places
    has_digit = bool(re.search(r"\d", text))
    # both sides: the destination takes the hotel's address too, and the ride from the airport is priced to it
    looks_address = ((has_digit and len(text) >= 3) or (len(text) >= 5 and " " in text)
                     or (len(text) >= 4 and not rows))   # a neighbourhood nobody else knows
    if looks_address:
        def fetch_addr():
            from urllib.parse import quote
            digits0 = text.replace(" ", "")
            # a bare postal code is searched in the visitor's own country first: 11237 is Bushwick, and also a town in Lithuania
            extra = ("&countrycodes=" + cc) if (cc and digits0.isdigit()) else ""
            parts, tries = _geo_tries(text)
            d, used = [], 0
            for used, q0 in enumerate(tries):
                req = urllib.request.Request("https://nominatim.openstreetmap.org/search?format=jsonv2&addressdetails=1&limit=5" + extra + "&q=" + quote(q0),
                                             headers={"User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)", "Accept": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=6) as r:
                        d = json.loads(r.read().decode("utf-8"))
                except Exception:
                    d = []
                if d or used == len(tries) - 1:
                    break
                time.sleep(1.05)
            if not d:
                return []
            out, seen_txt = [], set()
            digits = text.replace(" ", "")
            for hit in d:
                # a bare postal code: only places that actually carry it, not every 11237 on Earth
                if digits.isdigit() and not str((hit.get("address") or {}).get("postcode") or "").replace(" ", "").startswith(digits):
                    continue
                t = _address_text(hit.get("address") or {})
                if used and t:
                    t = parts[0] + ", " + t                  # the house or the name as typed, then what the map knows
                if t and t not in seen_txt:
                    seen_txt.add(t)
                    out.append({"value": t, "label": t, "sub": (hit.get("address") or {}).get("country") or "", "kind": "address"})
            return out
        addr = _lookup_cached("addr", text + ("|" + cc if cc else ""), 7 * 86400, fetch_addr)
        rows = (addr + rows) if has_digit else (rows + addr)
    return {"rows": rows[:9]}


def locate_request(q):
    """lat, lon -> a street address in the shape places.py reads from the end.
    OpenStreetMap's Nominatim, one identified User-Agent, nothing stored."""
    try:
        lat, lon = float(q["lat"][0]), float(q["lon"][0])
        assert -90 <= lat <= 90 and -180 <= lon <= 180
    except Exception:
        return {"error": "Missing or invalid lat and lon."}
    url = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18&addressdetails=1&lat=%.6f&lon=%.6f" % (lat, lon)
    req = urllib.request.Request(url, headers={"User-Agent": "ConcordeGo/1.0 (go.flyconcordefly.com)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"error": "Map service did not respond: %s" % type(exc).__name__}
    a = d.get("address") or {}
    return {"address_text": _address_text(a), "address": a}


def admin_update():
    st = admin_status(fetch=True)
    if not st.get("branch"):
        return {"error": "Not a git checkout. Nothing to update from."}
    if st.get("fetched") is False:
        return {"error": "Could not fetch from GitHub: " + (st.get("fetch_error") or "unknown")}
    rc, _, err = _git("reset", "--hard", "origin/%s" % st["branch"])
    if rc:
        return {"error": "Update failed: " + live.redact(err)}
    rc, head, _ = _git("rev-parse", "--short", "HEAD")
    # exit after the reply has gone out; the supervisor restarts the service on the new code
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"ok": True, "head": head, "was": (st.get("head") or {}).get("short"), "restarting": True}


def admin_logs(unit, n):
    import subprocess, shutil
    n = max(20, min(2000, n))
    if unit == "system":
        path = ((sys_status().get("last_run") or {}).get("log")) or ""
        if not (path.startswith(SYS_LOGS + "/") and ".." not in path and os.path.isfile(path)):
            logs = sorted(f for f in os.listdir(SYS_LOGS) if f.endswith(".log")) if os.path.isdir(SYS_LOGS) else []
            path = os.path.join(SYS_LOGS, logs[-1]) if logs else ""
        if not path:
            return {"source": None, "text": "No system update has run on this machine."}
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 400000)); data = f.read().decode("utf-8", "replace")
        return {"source": path, "text": "\n".join(data.splitlines()[-n:])}
    if shutil.which("journalctl"):
        svc = {"server": "concordego", "update": "concordego-update"}.get(unit, "concordego")
        try:
            r = subprocess.run(["journalctl", "-u", svc, "-n", str(n), "--no-pager", "-o", "short-iso"], capture_output=True, text=True, timeout=20)
            text = r.stdout.strip()
            # "-- No entries --" means the unit is not here (a Mac, a dev shell): fall through to the file
            if r.returncode == 0 and text and not text.startswith("-- No entries"):
                return {"source": "journal: " + svc, "text": live.redact(r.stdout)}
        except Exception:
            pass
    if LOG_FILE and os.path.exists(LOG_FILE):
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 400000)); data = f.read().decode("utf-8", "replace")
        return {"source": LOG_FILE, "text": live.redact("\n".join(data.splitlines()[-n:]))}
    return {"source": None, "text": "No log source: the journal is not readable and CONCORDEGO_LOG is not set."}


ADMIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ConcordeGo admin</title>
<style>
:root{--bg:#101013;--panel:#0a0a0c;--panel2:#191a1e;--line:#26272c;--text:#ececec;--dim:#b4b4b4;--faint:#8e8e8e;--green:#35e08a;--speed:#4da3ff;--red:#e26d5a;--price:#ffb020}
html{color-scheme:dark}body{margin:0;background:var(--bg);color:var(--text);font:300 15px/1.5 "Space Grotesk","Helvetica Neue",Arial,sans-serif;padding:28px 16px 60px}
.wrap{max-width:960px;margin:0 auto}h1{font:400 14px "Michroma","Space Grotesk",sans-serif;letter-spacing:.15em;text-transform:uppercase;margin:0 0 22px}h1 b{font-weight:800}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.card .k{font:400 10.5px "IBM Plex Mono",monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
.card .v{font-size:15px}.card .v b{font-weight:500}.card .s{color:var(--dim);font-size:12.5px;margin-top:4px}.ok{color:var(--green)}.warn{color:var(--price)}.bad{color:var(--red)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:6px 0 18px}
button{background:var(--text);color:var(--bg);border:0;border-radius:999px;padding:10px 16px;font:600 13.5px "Space Grotesk",sans-serif;cursor:pointer}button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}button:disabled{opacity:.45;cursor:default}
.note{color:var(--dim);font-size:13px}ul.in{margin:8px 0 0;padding-left:18px;color:var(--dim);font-size:13px}ul.in code{color:var(--text);font-family:"IBM Plex Mono",monospace;font-size:12px}
pre{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;font:12px/1.55 "IBM Plex Mono",monospace;color:var(--dim);white-space:pre-wrap;word-break:break-word;max-height:60vh;overflow:auto;margin:0}
.tabs{display:flex;gap:6px;margin:14px 0 8px}.tabs button{padding:6px 12px;font-size:12.5px}.tabs button[aria-pressed=true]{background:var(--text);color:var(--bg)}
button.orange{background:var(--price);color:#1a1204}.sysbox{background:var(--panel2);border:1px solid rgba(255,176,32,.45);border-radius:12px;padding:12px 16px;margin:-6px 0 18px}.sysbox .note{margin:0 0 6px}.sysbox code{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text)}
table{border-collapse:collapse;font-size:13px;width:100%}td,th{text-align:left;padding:4px 8px 4px 0;color:var(--dim)}th{color:var(--faint);font-weight:400;font-size:11px;letter-spacing:.1em;text-transform:uppercase}
</style></head><body><div class="wrap">
<h1>Concorde<b>Go</b> · admin</h1>
<div class="grid" id="cards"></div>
<div class="row"><button id="check">Check GitHub</button><button id="update" disabled>Update now</button><button class="ghost" id="refresh">Refresh</button><button class="orange" id="syspend" hidden>Updates pending</button><span class="note" id="msg"></span></div>
<div class="sysbox" id="sysbox" hidden></div>
<div id="incoming"></div>
<div class="card" style="margin-bottom:18px"><div class="k">Unlimited accounts</div><div class="v">Signed-in emails with no daily limit.</div>
<div id="unl" style="margin:8px 0 6px"></div>
<div class="row" style="margin:0"><input id="unladd" placeholder="email" style="flex:1;min-width:220px;background:var(--panel);border:1px solid var(--line);border-radius:10px;color:var(--text);font:13px 'Space Grotesk',sans-serif;padding:9px 12px"><button id="unlgo">Add</button></div></div>
<div class="tabs"><button id="t-server" aria-pressed="true">Server log</button><button id="t-update" aria-pressed="false">Update log</button><button id="t-system" aria-pressed="false">System update log</button><button class="ghost" id="t-reload">Reload log</button></div>
<pre id="log">…</pre>
</div><script>
const $ = s => document.querySelector(s); let ST = null, UNIT = 'server';
const ago = s => s < 90 ? s + 's' : s < 5400 ? Math.round(s/60) + ' min' : s < 172800 ? (s/3600).toFixed(1) + ' h' : Math.round(s/86400) + ' d';
const when = iso => iso ? new Date(iso).toLocaleString() : '—';
const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function j(url, opts){ if (opts && opts.method === 'POST') opts = Object.assign({}, opts, {headers: Object.assign({'Content-Type':'application/json', 'X-ConcordeGo-Admin':'1'}, opts.headers || {})}); const r = await fetch(url, opts); if (!r.ok) throw new Error(r.status + ' ' + r.statusText); return r.json(); }
function paint(st){ ST = st; const h = st.head || {}, behind = st.behind; paintKey(st);
  const cards = [
    ['Running', `<b>${h.short || '?'}</b> on ${st.branch || '?'}`, (h.subject || '') + (h.date ? ' · ' + when(h.date) : '')],
    ['GitHub', behind == null ? '<span class="warn">not checked</span>' : behind ? `<span class="warn">${behind} new commit${behind > 1 ? 's' : ''}</span>` : '<span class="ok">up to date</span>', st.last_check ? 'last checked ' + when(st.last_check) : 'never checked'],
    ['Process', `up ${ago(st.uptime_s)}`, 'pid ' + st.pid + ' · python ' + st.python + ' · ' + (st.owners.length ? 'owners: ' + st.owners.join(', ') : '<span class="warn">no CONCORDEGO_OWNERS set</span>') + ' · you: ' + (st.you === 'owner' ? 'this machine' : (st.you || 'nobody')) + (st.public ? ' · public box' : ' · <span class="warn">not marked public</span>')],
    ['Flight API', st.live && st.live.key_configured ? `<span class="ok">${st.live.provider} key on</span>` : '<span class="warn">no key: live search off</span>', st.live && st.live.quota ? `today ${st.live.quota.day_calls}/${st.live.quota.day_limit} · month ${st.live.quota.month_calls}/${st.live.quota.month_limit}` : ''],
    ['Claude', st.narrator ? '<span class="ok">key on</span>' : '<span class="warn">no key: template only</span>', 'narrator, wish box, Flight Fixer advice'],
    sysCard(st.system || {}),
    ['People today', st.users_today.length ? st.users_today.length + ' signed in' : 'none', st.users_today.slice(0, 4).map(u => `${u.email} ${u.searches}/${st.allowances.searches} · ${u.wishes}/${st.allowances.wishes}`).join('<br>')],
  ];
  $('#cards').innerHTML = cards.map(c => `<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div><div class="s">${c[2]}</div></div>`).join('');
  paintSys(st.system || {});
  $('#update').disabled = !behind; $('#update').textContent = behind ? `Update now (${behind})` : 'Update now';
  $('#incoming').innerHTML = (st.incoming || []).length ? '<ul class="in">' + st.incoming.map(c => `<li><code>${c.short}</code> ${c.subject} <span style="color:var(--faint)">· ${when(c.date)}</span></li>`).join('') + '</ul>' : '';
  if (st.fetch_error) $('#msg').textContent = 'Fetch failed: ' + st.fetch_error;
}
async function load(){ try { paint(await j('/api/admin/status')); } catch (e){ $('#msg').textContent = e.message; } }
async function logs(){ $('#log').textContent = '…'; try { const r = await j('/api/admin/logs?unit=' + UNIT + '&n=300'); $('#log').textContent = (r.source ? '[' + r.source + ']\n' : '') + r.text; $('#log').scrollTop = 1e9; } catch (e){ $('#log').textContent = e.message; } }
$('#check').onclick = async () => { $('#msg').textContent = 'Checking GitHub…'; $('#check').disabled = true; try { paint(await j('/api/admin/check', {method:'POST'})); $('#msg').textContent = ST.behind ? '' : 'Nothing new.'; } catch (e){ $('#msg').textContent = e.message; } $('#check').disabled = false; };
$('#update').onclick = async () => { if (!confirm('Update to the newest commit and restart the service?')) return; $('#msg').textContent = 'Updating…'; $('#update').disabled = true;
  try { const r = await j('/api/admin/update', {method:'POST'}); if (r.error){ $('#msg').textContent = r.error; $('#update').disabled = false; return; }
    $('#msg').textContent = `Updated to ${r.head}. Restarting…`; const t0 = Date.now();
    const poll = async () => { try { const st = await j('/api/admin/status'); if (st.uptime_s < 30 || Date.now() - t0 > 20000){ paint(st); $('#msg').textContent = `Running ${st.head.short}.`; logs(); return; } } catch (e) {} if (Date.now() - t0 < 60000) setTimeout(poll, 1500); else $('#msg').textContent = 'Service not responding after 60 seconds. Check the log.'; };
    setTimeout(poll, 2500);
  } catch (e){ $('#msg').textContent = e.message; $('#update').disabled = false; } };
$('#refresh').onclick = () => { load(); logs(); };
const tab = u => { UNIT = u; ['server', 'update', 'system'].forEach(k => $('#t-' + k).setAttribute('aria-pressed', String(k === u))); logs(); };
$('#t-server').onclick = () => tab('server');
$('#t-update').onclick = () => tab('update');
$('#t-system').onclick = () => tab('system');
$('#t-reload').onclick = logs;
const paintUnl = list => { $('#unl').innerHTML = (list || []).length ? list.map(e => `<span style="display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;padding:5px 6px 5px 12px;margin:0 6px 6px 0;font-size:13px">${e}<button class="ghost" data-unl="${e}" style="padding:2px 8px;font-size:12px" title="Remove">×</button></span>`).join('') : '<span class="note">None. Owners are always unlimited.</span>'; };
document.addEventListener('click', async ev => { const b = ev.target.closest('[data-unl]'); if (!b) return; const r = await j('/api/admin/unlimited', {method:'POST', body: JSON.stringify({remove: b.dataset.unl})}); paintUnl(r.unlimited); });
$('#unlgo').onclick = async () => { const e = $('#unladd').value.trim(); if (!e.includes('@')) return; const r = await j('/api/admin/unlimited', {method:'POST', body: JSON.stringify({add: e})}); $('#unladd').value = ''; paintUnl(r.unlimited); };
const paintKey = st => { paintUnl(st.unlimited); };
// System updates (per Patrick, 2026-09-24): checked nightly, never installed automatically. An orange
// "Updates pending" button opens the two choices: Update (the site stays up) and Update and restart.
let SYSOPEN = false, SYSWAIT = null;
const sysBusy = sy => sy.state === 'running' || sy.requested;
function sysCard(sy){
  const n = sy.pending_count || 0, rb = (sy.reboot_waiting || []).length, lr = sy.last_run || {}, held = (sy.deferred || []).length;
  const v = !sy.available ? '<span class="warn">not set up on this machine</span>' : sysBusy(sy) ? '<span class="warn">updating</span>'
    : n ? `<span class="warn">${n} update${n > 1 ? 's' : ''} pending</span>` : rb ? '<span class="warn">restart pending</span>' : '<span class="ok">up to date</span>';
  const s = [sy.kernel ? 'kernel ' + esc(sy.kernel) : '', sy.checked_at ? 'checked ' + when(sy.checked_at) : 'not checked yet',
    held ? held + ' held back by Ubuntu for now' : '', lr.finished_at ? 'last update ' + when(lr.finished_at) + (lr.exit === 0 ? '' : ' <span class="bad">failed</span>') : ''].filter(Boolean).join(' · ');
  return ['System', v, s];
}
function paintSys(sy){
  const btn = $('#syspend'), box = $('#sysbox'), n = sy.pending_count || 0, rb = (sy.reboot_waiting || []).length, busy = sysBusy(sy);
  const show = !!sy.available && (busy || n > 0 || rb > 0);
  btn.hidden = !show; box.hidden = !show || !(SYSOPEN || busy);
  if (!show) return;
  btn.textContent = busy ? 'Updating…' : n ? `Updates pending (${n})` : 'Restart pending';
  if (busy){ box.innerHTML = '<p class="note">Updating now. The System update log below shows progress.</p>'; return; }
  const restart = [...new Set([...(sy.reboot_waiting || []), ...(sy.restart_likely || [])])];
  box.innerHTML = (n ? `<p class="note">${n} update${n > 1 ? 's' : ''}: ${(sy.pending || []).slice(0, 24).map(p => '<code>' + esc(p) + '</code>').join(', ')}${n > 24 ? ', and ' + (n - 24) + ' more' : ''}</p>` : '')
    + (restart.length ? `<p class="note">Needs a restart: ${restart.slice(0, 8).map(p => '<code>' + esc(p) + '</code>').join(', ')}</p>` : '')
    + `<div class="row" style="margin:8px 0 4px">${n ? '<button id="sysupd">Update</button>' : ''}${sy.restart ? '<button class="orange" id="sysrb">Update and restart</button>' : ''}</div>`
    + `<p class="note">${n ? 'Update keeps the site up. ' : ''}${sy.restart ? 'Update and restart takes it down for about a minute.' : ''}</p>`;
}
$('#syspend').onclick = () => { SYSOPEN = !SYSOPEN; paintSys((ST || {}).system || {}); };
document.addEventListener('click', async ev => {
  const b = ev.target.closest('#sysupd, #sysrb'); if (!b) return; const reboot = b.id === 'sysrb';
  if (!confirm(reboot ? 'Install updates and restart the server? The site is down for about a minute.' : 'Install updates now? The site stays up.')) return;
  b.disabled = true; $('#msg').textContent = 'Starting…';
  try { const r = await j('/api/admin/sysupdate', {method:'POST', body: JSON.stringify({mode: reboot ? 'reboot' : 'update'})});
    if (r.error){ $('#msg').textContent = r.error; b.disabled = false; return; }
    SYSWAIT = {reboot, t0: Date.now(), down: false}; SYSOPEN = true; tab('system'); load(); $('#msg').textContent = reboot ? 'Updating, then restarting…' : 'Updating…'; setTimeout(sysPoll, 3000);
  } catch (e){ $('#msg').textContent = e.message; b.disabled = false; } });
async function sysPoll(){
  const w = SYSWAIT; if (!w) return; let st = null;
  try { st = await j('/api/admin/status'); } catch (e){ w.down = true; }
  if (st){ paint(st); if (UNIT === 'system') logs();
    const sy = st.system || {}, lr = sy.last_run || {}, done = !sysBusy(sy) && Date.parse(lr.finished_at || 0) >= w.t0 - 5000;
    if (w.reboot && (w.down || st.uptime_s < 120) && Date.parse(lr.started_at || 0) >= w.t0 - 5000 && !sysBusy(sy)){ SYSWAIT = null; $('#msg').textContent = 'Restarted. Kernel ' + (sy.kernel || '?') + '.'; return; }
    if (done && !(w.reboot && (sy.reboot_waiting || []).length)){ SYSWAIT = null; $('#msg').textContent = lr.exit === 0 ? 'Update finished.' : 'Update failed. See the System update log.'; return; } }
  else $('#msg').textContent = 'Restarting… the page will reconnect.';
  if (Date.now() - w.t0 < 45 * 60000) setTimeout(sysPoll, 4000); else { SYSWAIT = null; $('#msg').textContent = 'Still not finished after 45 minutes. Check the System update log.'; } }
load(); logs();
</script></body></html>
"""



class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ConcordeGo"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("CONCORDEGO_QUIET"):
            return
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---------------------------------------------------------------- GET
    # A request that came through the tunnel carries Cloudflare's headers; a
    # local one never does (the same test millenai.py uses). The public
    # address may read anything but may not SPEND: no metered flight search,
    # no narrator call, no 250 MB sky download on its say-so.
    def _remote(self):
        # On the public box NOTHING is local: every request comes through the
        # tunnel, so a request that somehow arrives without the proxy's headers
        # is a stranger, never the owner (CONCORDEGO_PUBLIC=1, set by the
        # installer). Locality grants owner only on a developer's own machine.
        if PUBLIC:
            return True
        return bool(self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For"))

    # Who is asking. Local is the owner. Through the tunnel, Cloudflare Access
    # sets the signed-in email on every request it lets past; the origin only
    # listens on loopback, so that header can only arrive via Cloudflare and
    # nobody else can write it. (A JWT check against the team's public keys
    # would be belt and braces; the stdlib has no RSA, so the loopback bind
    # is the guarantee, and it is documented in go-live.sh.)
    def _who(self):
        if not self._remote():
            return "owner"
        email = (self.headers.get("Cf-Access-Authenticated-User-Email") or "").strip().lower()
        if email:
            return email
        # the open part of the site: read Access's own cookie, verified
        token = self.headers.get("Cf-Access-Jwt-Assertion") or ""
        if not token:
            from http.cookies import SimpleCookie
            try:
                c = SimpleCookie(); c.load(self.headers.get("Cookie") or "")
                token = c["CF_Authorization"].value if "CF_Authorization" in c else ""
            except Exception:
                token = ""
        return access_email(token) if token else None

    def _is_owner(self):
        who = self._who()
        return who == "owner" or (who is not None and who in OWNERS)

    def _unmetered(self, who):
        return who == "owner" or (who is not None and (who in OWNERS or who in _unlimited()))

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        # Outside /api on purpose: Access fronts /api with the sign-in, and these two are free, cached and
        # wanted before anyone signs in (the tiles' photos, the locate button while typing an origin).
        if path == "/whoami":
            # outside /api: the page asks this before anyone signs in
            who = self._who()
            owner = who == "owner" or (who is not None and who in OWNERS)
            ip = (self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For") or "?").split(",")[0].strip()
            free = None if who else max(0, ANON_SEARCHES - (_users_left("ip:" + ip) or {}).get("searches_used", 0))
            return self._json({"remote": self._remote(), "email": None if who in (None, "owner") else who,
                               "owner": owner, "unlimited": bool(who and "@" in who and who in _unlimited()),
                               "profile": _users_profile(who) if (who and "@" in who) else None,
                               "left": None if (owner or who is None or who in _unlimited()) else _users_left(who),
                               "recent": _users_recent(who)[:3] if (who and "@" in who) else [],
                               "allowances": {"searches": USER_SEARCHES, "wishes": USER_WISHES, "free": ANON_SEARCHES},
                               "free_left": free, "hours": _hours_to_midnight()})
        if path in ("/locate", "/photos", "/suggest"):
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            return self._json({"/locate": locate_request, "/photos": photos_request, "/suggest": suggest_request}[path](q))
        if path == "/api/admin" or path.startswith("/api/admin/"):
            if not self._is_owner():
                return self._html("<!doctype html><meta charset=utf-8><body style='background:#101013;color:#ececec;font:15px sans-serif;padding:40px'>"
                                  "<p>This page is for site admins. Sign in with an admin email.</p>", 403)
            if path == "/api/admin":
                return self._html(ADMIN_HTML)
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            if path == "/api/admin/status":
                st = admin_status(False)
                st["you"] = self._who()
                st["public"] = PUBLIC
                return self._json(st)
            if path == "/api/admin/logs":
                try:
                    n = int((q.get("n") or ["300"])[0])
                except ValueError:
                    n = 300
                return self._json(admin_logs((q.get("unit") or ["server"])[0], n))
            return self.send_error(404)
        if path == "/flight":
            # the Flight Fixer's tracking lookup: a flight number and a date -> the observed status
            # fully qualified: do_GET imports parse_qs locally further down, which makes the bare name a local here
            return self._json(flight_request(dict(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)), self._remote(), self.headers))
        if path == "/" or path == "/index.html":
            if ROOT:
                return self._send_file(os.path.join(UI, "mock", ROOT + ".html"), "text/html; charset=utf-8", stamp=True)
            return self._send_file(os.path.join(UI, "index.html"), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            # the Concorde website's own icon (flyconcordefly.com), for bookmarks and browsers that ask for it by name
            return self._send_file(os.path.join(UI, "mock", "assets", "favicon.ico"), "image/x-icon")
        if ROOT and path.startswith("/assets/"):
            # the mockup at / asks for its nameplates relative to itself
            name = os.path.basename(path)
            if re.match(r"^(mark-[a-z0-9]+|boom)-(pq\.mp4|hlg\.webm)$", name):
                return self._send_file(os.path.join(UI, "mock", "assets", name),
                                       "video/mp4" if name.endswith(".mp4") else "video/webm")
            return self.send_error(404)
        # The interface mockups. Standalone files by design, served here too so
        # they can be flipped between side by side with the real thing.
        if path == "/mock" or path == "/mock/":
            return self._send_file(os.path.join(UI, "mock", "index.html"),
                                   "text/html; charset=utf-8")
        if path.startswith("/mock/"):
            name = os.path.basename(path)
            if path.startswith("/mock/assets/") and re.match(r"^(mark-[a-z0-9]+|boom)-(pq\.mp4|hlg\.webm)$", name):
                # the HDR nameplates and the site's warp flare: tagged loops
                return self._send_file(os.path.join(UI, "mock", "assets", name),
                                       "video/mp4" if name.endswith(".mp4") else "video/webm")
            if not re.match(r"^(index|mock-[0-9]+)\.html$", name):
                return self.send_error(404)
            return self._send_file(os.path.join(UI, "mock", name),
                                   "text/html; charset=utf-8")
        if path in ("/signin", "/admin"):
            # the old paths: into the one protected application, /api, so the page and its calls share one sign-in
            q = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
            self.send_response(302); self.send_header("Location", "/api" + path + q); self.send_header("Content-Length", "0"); self.end_headers(); return
        if path == "/api/signin":
            # Cloudflare Access protects this path; by the time a request lands
            # here the person has signed in, and the cookie now covers /api/*
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            nxt = urllib.parse.unquote(dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("next", "/"))
            # a path on this site only: "//host" is another site, and anything outside printable ASCII breaks the header
            ok_next = nxt.startswith("/") and not nxt.startswith("//") and all(32 < ord(c) < 127 and c != chr(92) for c in nxt)
            self.send_response(302); self.send_header("Location", nxt if ok_next else "/"); self.send_header("Content-Length", "0"); self.end_headers(); return
        if path == "/api/whoami":
            who = self._who()
            owner = who == "owner" or (who is not None and who in OWNERS)
            return self._json({"remote": self._remote(), "email": None if who in (None, "owner") else who,
                               "owner": owner, "left": None if (owner or who is None) else _users_left(who),
                               "profile": _users_profile(who) if (who and "@" in who) else None,
                               "allowances": {"searches": USER_SEARCHES, "wishes": USER_WISHES}})
        if path == "/api/live/status":
            # Safe to serve: live.status() reports whether a key exists and
            # where it came from, never the key.
            return self._json(live.status())
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
            return self._json(sky_status(i, args.get("warm") == "1" and not self._remote()))
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
        path = self.path.split("?")[0]
        if path in ("/api/admin/check", "/api/admin/update", "/api/admin/unlimited", "/api/admin/sysupdate"):
            # read the body before any refusal: left unread on a kept-alive connection, it becomes the start
            # of the next request line (seen 2026-09-24 as a 501 for the method '{"mode":"update"}GET')
            try:
                raw = self.rfile.read(max(0, min(65536, int(self.headers.get("Content-Length") or 0))))
            except ValueError:
                raw = b""
            if not self._is_owner():
                return self._json({"error": "Owners only."})
            # the owner's browser carries the sign-in cookie to any site's form; only this page's own fetch sets this header
            if self.headers.get("X-ConcordeGo-Admin") != "1":
                return self._json({"error": "Refused: not sent from the admin page."})
            try:
                if path.endswith("/sysupdate"):
                    body = json.loads(raw or b"{}")
                    return self._json(sys_update(str((body if isinstance(body, dict) else {}).get("mode") or "")))
                if path.endswith("/unlimited"):
                    body = json.loads(raw or b"{}")
                    cur = set(_unlimited())
                    add = str(body.get("add") or "").strip().lower(); rm = str(body.get("remove") or "").strip().lower()
                    if add and "@" in add: cur.add(add)
                    cur.discard(rm)
                    _unlimited_set(cur)
                    return self._json({"unlimited": _unlimited()})
                return self._json(admin_status(True) if path.endswith("/check") else admin_update())
            except Exception as exc:
                return self._json({"error": "%s: %s" % (type(exc).__name__, exc)})
        if path == "/search":
            path = "/api/search"; anon_ok = True
        elif path == "/rescue":
            path = "/api/rescue"; anon_ok = True          # the delayed-flight helper: a search and a brief, on the free allowance
        elif path == "/flex":
            path = "/api/flex"; anon_ok = True            # nearby days: one allowance unit, the days each cached
        elif path == "/sellers":
            path = "/api/sellers"; anon_ok = True         # who sells this fare: one SerpApi call, its own allowance
        else:
            anon_ok = False
        if path == "/api/profile":
            # the flying personality: saved for a signed-in person, never metered, never for a stranger
            who = self._who()
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception as exc:
                return self._json({"error": "Bad request: %s" % exc})
            if not who or "@" not in who:
                return self._json({"ok": False, "local": who == "owner", "error": None if who == "owner" else "Sign in to save your profile."})
            persona = body.get("persona") if isinstance(body, dict) and isinstance(body.get("persona"), dict) else None
            profile = {"persona": persona} if persona else {}
            if len(json.dumps(profile)) > 4000:
                return self._json({"ok": False, "error": "Profile too large."})
            _users_profile_set(who, profile)
            return self._json({"ok": True, "profile": profile})
        if path not in ("/api/score", "/api/narrate", "/api/live", "/api/wish", "/api/search", "/api/rescue", "/api/flex"):
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("expected a JSON object")
        except Exception as exc:
            return self._json({"error": "Bad request: %s" % exc})
        if path == "/api/flex" and _users_left("_flex")["searches_used"] >= FLEX_PER_DAY:
            # the site-wide cap before anyone's allowance is charged, so a refused sweep costs nothing
            return self._json({"error": "Nearby days: daily limit reached. Try again %s." % _in_hours()})
        # Spending: a live search, the narrator and the wish model. The owner
        # spends freely; a signed-in visitor spends from a daily allowance;
        # nobody anonymous spends at all.
        # a live search only spends when this machine holds a key; without one the recording stands in
        spends = path == "/api/narrate" or path == "/api/wish" or path == "/api/sellers" or (path in ("/api/live", "/api/search", "/api/rescue", "/api/flex") and (path in ("/api/rescue", "/api/flex") or (req.get("source") or "sample") != "sample") and bool(live.load_config().get("key")))
        charged = None                                  # (who, kind) taken below, handed back if the input is refused
        if spends and self._remote():
            who = self._who()
            if not who and anon_ok:
                # the free searches: a few a day per address, then the account or the wait
                ip = (self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For") or "?").split(",")[0].strip()
                if path == "/api/sellers":
                    # a seller check has its own few a day, so it never spends one of the free searches
                    ok, left = _users_take("ip:" + ip, "sellers", ANON_SELLERS)
                    if not ok:
                        return self._json({"error": "Daily limit reached: %d seller checks. Sign in for %d a day, or try again %s."
                                                    % (ANON_SELLERS, USER_SELLERS, _in_hours()), "remote": True, "sign_in": True})
                    who = None
                    charged = ("ip:" + ip, "sellers")
                else:
                    ok, left = _users_take("ip:" + ip, "searches", ANON_SEARCHES)
                    if not ok:
                        return self._json({"error": "Daily limit reached: %d free searches. Sign in for %d a day, or try again %s."
                                                    % (ANON_SEARCHES, USER_SEARCHES, _in_hours()),
                                           "remote": True, "sign_in": True, "free": ANON_SEARCHES, "hours": _hours_to_midnight()})
                    who = None
                    charged = ("ip:" + ip, "searches")
            elif not who:
                return self._json({"error": "Sign in to use this. A free account adds more searches and the wish box.",
                                   "remote": True, "sign_in": True})
        if spends and self._remote() and who:
            kind, limit = (("searches", USER_SEARCHES) if path in ("/api/live", "/api/search", "/api/rescue", "/api/flex")
                           else ("sellers", USER_SELLERS) if path == "/api/sellers" else ("wishes", USER_WISHES))
            if kind == "wishes":
                okall, _ = _users_take("_everyone", "wishes", WISHES_TOTAL)
                if not okall:
                    return self._json({"error": "Wish box: daily limit reached. Try again %s." % _in_hours(),
                                       "remote": True, "allowance": True})
            ok, left = (True, None) if self._unmetered(who) else _users_take(who, kind, limit)
            charged = None if left is None else (who, kind)
            if not ok:
                return self._json({"error": "Daily limit reached: %d %s. Try again %s." % (limit, kind, _in_hours()),
                                   "remote": True, "allowance": True})
            if not self._unmetered(who):
                charged = (who, kind)
        try:
            if anon_ok and self._remote():
                req = dict(req, _served=True)
                who_r = self._who()
                trip = req.get("trip") or {}
                if who_r and "@" in who_r and path == "/api/search" and isinstance(trip, dict) and not trip.get("leg"):
                    _users_recent_add(who_r, {"from": str(req.get("origin") or "")[:120], "to": str(req.get("destination") or "")[:120], "date": str(req.get("date") or "")[:10],
                                              "kind": str(trip.get("kind") or "round")[:8], "back": str(trip.get("back") or "")[:40], "pax": str(trip.get("pax") or "")[:20], "bags": str(trip.get("bags") or "")[:20]})
            fn = {"/api/score": score_request, "/api/narrate": narrate_request, "/api/rescue": rescue_request, "/api/flex": flex_request,
                  "/api/live": live_request, "/api/wish": wish_request, "/api/search": search_request,
                  "/api/sellers": sellers_request}[path]
            out = fn(req)
            if charged and isinstance(out, dict) and out.get("field"):
                _users_give(*charged)                 # refused before any fetch: a typo or a past date costs no search
            return self._json(out)
        except Exception as exc:
            # a broken fixture should say so on the page, not 500 silently
            import traceback
            traceback.print_exc()
            return self._json({"error": "Something went wrong. Try again."})

    # ------------------------------------------------------------ helpers
    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, stamp=False):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(404)
        if stamp:
            body = _stamp(body)
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
    print("ConcordeGo  http://127.0.0.1:%d%s" % (PORT, ("  (/ is %s)" % ROOT) if ROOT else ""))
    print("  %d night clips available, %d already cached in %s"
          % (len(SKY_SOURCES), len(ready), _sky_dir()))
    if not ready:
        print("  first load warms one clip in the background (a few hundred MB);")
        print("  the canvas skyline carries the page until it lands.")
    Server(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
