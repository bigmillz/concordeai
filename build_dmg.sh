#!/bin/zsh
# Packages ConcordeAI into a styled, shareable disk image: $VOL.dmg
# Finder shows a custom starfield background with a drag-to-install arrow.
set -e
cd "$(dirname "$0")"

./build_macos_app.sh

VER="$(python3 -c "import re;print(re.search(r'APP_VERSION = \"([^\"]+)\"', open('millenai.py').read()).group(1))")"
# THE LABEL (6b287, per Patrick: "keep the version numbers in the DMG
# window accurate… this should be one of the nightlies and reflect as
# such"). The window title and the line under the mark carry the app's
# own short_version() — "6.0.3 nightly 7a23650", "6.1 beta 2", "6.1 RC1",
# "6.1" — mirrored here from the same constants CI stamps before building.
LABEL="$(python3 - <<'PY'
import re
s = open("millenai.py", encoding="utf-8").read()
v = re.search(r'APP_VERSION = "([^"]+)"', s).group(1)
nightly = re.search(r'^APP_NIGHTLY = "([^"]*)"', s, re.M).group(1)
beta = int(re.search(r"^APP_BETA = (\d+)", s, re.M).group(1))
rc = int(re.search(r"^APP_RC = (\d+)", s, re.M).group(1))
show = v[:-2] if v.count(".") >= 2 and v.endswith(".0") else v
if nightly: show += " nightly " + nightly.split()[-1]
elif rc:    show += " RC%d" % rc
elif beta:  show += " beta %d" % beta
print(show)
PY
)"
VOL="ConcordeAI $LABEL"        # VOLUME label = window title, keeps the spaces
# FILENAME - hyphenated on purpose, RAW version only: the site and the
# updater read the version off the asset name, and GitHub rewrites spaces
# to dots in release-asset names ("ConcordeAI.6.1.0.dmg").
DMGFILE="ConcordeAI-$VER.dmg"
SHOW_VER="$LABEL"
echo "→ label: $LABEL"

VENV="$HOME/Library/Application Support/MillenAI/venv"
"$VENV/bin/pip" install --quiet pillow

STAGE="$(mktemp -d)/MillenAI"
mkdir -p "$STAGE/.background"
cp -R ConcordeAI.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"

# --- background art (1320x800 @144dpi -> 660x400 pts, retina-sharp)
"$VENV/bin/python3" - "$STAGE/.background/bg.png" "$SHOW_VER" <<'PYEOF'
import random
import sys

from PIL import Image, ImageDraw, ImageFont

# 6b242, per Patrick: BLACK ground, grey/white stars. The window is the
# same greyscale world as the app — no periwinkle, no teal, nothing that
# isn't in the identity.
W, H = 1320, 800
img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(overlay)

random.seed(7)
palette = [(255, 255, 255), (222, 226, 234), (176, 182, 194), (124, 130, 142)]
for _ in range(190):
    x, y = random.uniform(0, W), random.uniform(0, H)
    r = random.uniform(0.7, 2.5)
    c = random.choice(palette)
    od.ellipse([x - r, y - r, x + r, y + r],
               fill=c + (int(random.uniform(55, 235)),))
# a few warp streaks, kept short, faint and off the edges — long ones read
# as scratches on the black rather than as motion
for _ in range(16):
    x, y = random.uniform(90, W - 90), random.uniform(70, H - 70)
    dx, dy = (x - W / 2), (y - H / 2)
    n = max((dx * dx + dy * dy) ** 0.5, 1)
    ln = random.uniform(18, 40)
    c = random.choice(palette)
    od.line([x, y, x + dx / n * ln, y + dy / n * ln],
            fill=c + (int(random.uniform(22, 52)),), width=2)

def font(path, size, index=0):
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        return ImageFont.load_default()

def centered(y, text, f, fill):
    w = od.textlength(text, font=f)
    od.text(((W - w) / 2, y), text, font=f, fill=fill)

# ---------------------------------------------------------------- the mark
# THE SETTINGS LOCKUP, SCALED UP (6b242, per Patrick). Not redrawn by eye —
# every number below was MEASURED off #set-brand in the running app and is
# expressed against the wing's height WH, so the two windows carry the one
# mark at the one set of proportions:
#     wing   19.6 x 16.4 px  ->  width 1.1951 WH
#     gap    7 px            ->        0.4268 WH
#     cap    11.5px Michroma, cap 0.7656em  ->  0.5368 WH
#     wordmark ink width     ->        6.7982 WH
# The wing is SMALL against a long, wide-tracked wordmark — that ratio is
# the whole character of the lockup, and eyeballing it always drifts.
WH = 84.0
WW, GAP, CAP, WORD_W = 1.1951 * WH, 0.4268 * WH, 0.5368 * WH, 6.7982 * WH
TOP = 40.0

# the five bars in the SVG's own viewBox units (origin 2, 2.3), round caps
LINES = [(3.2, 17.5, 20.4, 3.5), (7.5, 17.5, 20.4, 7.0),
         (11.8, 17.5, 20.4, 10.5), (16.1, 17.5, 20.4, 14.0),
         (19.3, 17.5, 20.4, 16.6)]
SW, S = 2.4, WH / 16.4
wx, wy = (W - WW) / 2, TOP

SS = 4                          # supersample: the round caps need the help
mask = Image.new("L", (int(WW * SS) + 8, int(WH * SS) + 8), 0)
md = ImageDraw.Draw(mask)
rad = SW * S * SS / 2.0
for x1, y1, x2, y2 in LINES:
    p = [((x1 - 2.0) * S * SS + 4, (y1 - 2.3) * S * SS + 4),
         ((x2 - 2.0) * S * SS + 4, (y2 - 2.3) * S * SS + 4)]
    md.line([p[0], p[1]], fill=255, width=int(round(rad * 2)))
    for cx, cy in p:                                  # stroke-linecap:round
        md.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=255)
mask = mask.resize((int(WW) + 2, int(WH) + 2), Image.LANCZOS)

# the SVG's own gradient: objectBoundingBox, bottom-left -> top-right over
# the GEOMETRY box (x 3.2..20.4, y 3.5..17.5), steel -> silver
STOPS = [(0.0, (0x78, 0x7e, 0x89)), (0.55, (0xb7, 0xbc, 0xc6)),
         (1.0, (0xf4, 0xf5, 0xf8))]

def ramp(t):
    t = min(max(t, 0.0), 1.0)
    for i in range(len(STOPS) - 1):
        a, ca = STOPS[i]
        b, cb = STOPS[i + 1]
        if t <= b:
            f = 0.0 if b == a else (t - a) / (b - a)
            return tuple(int(u + (v - u) * f) for u, v in zip(ca, cb))
    return STOPS[-1][1]

gw, gh = mask.size
grad = Image.new("RGBA", (gw, gh))
gp = grad.load()
gx0, gy0 = (3.2 - 2.0) * S, (3.5 - 2.3) * S
gw_, gh_ = 17.2 * S, 14.0 * S
for py in range(gh):
    v = (py - gy0) / gh_
    for px in range(gw):
        u = (px - gx0) / gw_
        gp[px, py] = ramp((u - v + 1) / 2) + (255,)
overlay.paste(grad, (int(wx), int(wy)), mask)

# The wordmark, in the wordmark's own face (6b287, per Patrick: "fix the
# font… to our wordmark"). The bundled Michroma the titlebar registers
# (fonts/) is right here for PIL, so the disk image and the app carry ONE
# lockup: size from the measured cap height, tracking from the measured
# ink width (6.7982 WH off "CONCORDE" — the app's .2em rhythm), and the
# AI exactly as the app sets it: regular weight at .865em with a .12em
# same-colour stroke, raised .06em. No synthetic bold, no stand-in face.
import os
MICHROMA = "fonts/Michroma-Regular.ttf"
assert os.path.exists(MICHROMA), "fonts/Michroma-Regular.ttf is missing"
WHITE = (255, 255, 255, 255)
probe = font(MICHROMA, 100)
bb = od.textbbox((0, 0), "CONCORDE", font=probe)
FS = int(round(100 * CAP / (bb[3] - bb[1])))
wm = font(MICHROMA, FS)
track = (WORD_W - sum(od.textlength(ch, font=wm)
                      for ch in "CONCORDE")) / 7.0
AI_FS = int(round(FS * 0.865))
ai = font(MICHROMA, AI_FS)
# a CSS stroke straddles the outline (half out); PIL's grows outward only
AI_SW = max(1, int(round(AI_FS * 0.12 / 2)))
AI_UP = AI_FS * 0.06
INK = (sum(od.textlength(ch, font=wm) for ch in "CONCORDE")
       + sum(od.textlength(ch, font=ai) + AI_SW * 2 for ch in "AI")
       + track * 9.0)
cx, base = (W - INK) / 2, wy + WH + GAP      # base = the cap TOP line
hb, hab = od.textbbox((0, 0), "H", font=wm), od.textbbox((0, 0), "H", font=ai)
topy = base - hb[1]
# the smaller AI shares the baseline (cap bottom), then rises .06em
ai_topy = base + (hb[3] - hb[1]) - (hab[3] - hab[1]) - AI_UP - hab[1]
for ch in "CONCORDE":
    od.text((cx, topy), ch, font=wm, fill=WHITE)
    cx += od.textlength(ch, font=wm) + track
for ch in "AI":
    od.text((cx + AI_SW, ai_topy), ch, font=ai, fill=WHITE,
            stroke_width=AI_SW, stroke_fill=WHITE)
    cx += od.textlength(ch, font=ai) + AI_SW * 2 + track

# the version sits quietly under the mark, not welded into it
sub = font("/System/Library/Fonts/Helvetica.ttc", 30)
centered(base + CAP + 30, sys.argv[2], sub, (150, 156, 168, 235))

# install help. macOS 15+ removed the right-click→Open bypass, so an
# unnotarized app MUST be allowed from System Settings the first time.
small = font("/System/Library/Fonts/Menlo.ttc", 21)
centered(668, "1. drag ConcordeAI into Applications, then open it once",
         small, (124, 130, 142, 235))
centered(704, "2. macOS will block it — that is expected for a free app",
         small, (124, 130, 142, 235))
centered(740, "3. System Settings ▸ Privacy & Security ▸ Open Anyway",
         small, (226, 230, 238, 245))

# drag arrow between the two icon slots (icons at 165pt / 495pt), carrying
# the wing's own steel -> silver ramp so the window stays monochrome
ay, x1, x2 = 396, 500, 790
c1, c2 = (0x78, 0x7e, 0x89), (0xf4, 0xf5, 0xf8)
steps = 60
for i in range(steps):
    t0, t1 = i / steps, (i + 1) / steps
    c = tuple(int(a + (b - a) * t0) for a, b in zip(c1, c2))
    od.line([x1 + (x2 - x1) * t0, ay, x1 + (x2 - x1) * t1 + 2, ay],
            fill=c + (255,), width=14)
od.polygon([(x2 + 52, ay), (x2 - 6, ay - 34), (x2 - 6, ay + 34)],
           fill=c2 + (255,))

# no bloom pass: the old navy ground could carry a blurred halo, but on
# true black it just lifts the whole field to charcoal
img = Image.alpha_composite(img, overlay)
img.convert("RGB").save(sys.argv[1], dpi=(144, 144))
print("background written")
PYEOF

# --- writable image first, style it via Finder, then compress
rm -f "$DMGFILE" "$VOL.dmg" MillenAI.dmg ConcordeAI-rw.dmg MillenAI-rw.dmg
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDRW \
  -quiet ConcordeAI-rw.dmg
# detach any leftover mount of this volume first — a stale one makes the
# Finder styling step fail with "Can't get disk ..."
hdiutil detach -quiet "/Volumes/$VOL" 2>/dev/null || true
hdiutil attach -readwrite -noverify -noautoopen -quiet ConcordeAI-rw.dmg

# wait for Finder to actually see the volume before scripting it
for i in $(seq 1 40); do
  [[ -d "/Volumes/$VOL" ]] && break
  sleep 0.25
done
sleep 1

osascript <<OSA || echo "  (Finder styling skipped — automation not authorized; plain DMG)"
tell application "Finder"
  tell disk "$VOL"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set pathbar visible of container window to false
    set vo to the icon view options of container window
    set arrangement of vo to not arranged
    set icon size of vo to 128
    set text size of vo to 13
    set background picture of vo to file ".background:bg.png"
    set position of item "ConcordeAI.app" of container window to {165, 210}
    set position of item "Applications" of container window to {495, 210}
    update without registering applications
    delay 1
    -- set bounds twice with a nudge: Finder only persists the size if it
    -- registers a change while the window is frontmost
    set the bounds of container window to {200, 120, 859, 547}
    delay 1
    set the bounds of container window to {200, 120, 860, 548}
    update without registering applications
    delay 2
    close
    delay 1
  end tell
end tell
OSA

# volume icon LAST — the Finder styling session above deletes
# .VolumeIcon.icns and clears the custom-icon flag if they exist earlier
if [[ -f MillenAI.icns ]]; then
  cp MillenAI.icns "/Volumes/$VOL/.VolumeIcon.icns"
  SetFile -a C "/Volumes/$VOL" 2>/dev/null \
    || xcrun SetFile -a C "/Volumes/$VOL" 2>/dev/null \
    || echo "  (SetFile unavailable — volume icon flag skipped)"
fi

sync
hdiutil detach -quiet "/Volumes/$VOL"
hdiutil convert -quiet ConcordeAI-rw.dmg -format UDZO -o "$DMGFILE"
rm -f ConcordeAI-rw.dmg
rm -rf "$(dirname "$STAGE")"

echo ""
echo "✓ built $DMGFILE ($(du -h "$DMGFILE" | cut -f1 | tr -d ' '))"
