#!/bin/zsh
# Publish a new ConcordeAI release to GitHub so existing installs self-update.
#
#   ./release.sh beta 6.1.0   beta 1 of a NEW line (prerelease, "6.1 beta 1")
#   ./release.sh beta         the NEXT beta of the current line ("6.1 beta 2")
#   ./release.sh rc           the next release candidate ("6.1 RC1", "6.1 RC2")
#   ./release.sh 6.1.0        STABLE: clears the beta/RC hold, lands on /releases/latest
#   ./release.sh patch|minor|major   stable, computed from the current version
#
# THE LINE (6b286, per Patrick, the macOS way): nightlies roll on every
# push; when one looks clean he says "commit this to beta N"; betas are
# numbered 1, 2, 3… per version line (never the build number); when a
# beta is clean it goes stable. Nightlies never touch this script.
#
# The build number is a separate monotonic counter that always increments —
# it is what the in-app updater compares, so the marketing version can move
# however you like without breaking updates.
#
# Needs the GitHub CLI:  brew install gh && gh auth login
set -e
cd "$(dirname "$0")"

ARG="$1"; ARG2="${2:-}"
if [[ -z "$ARG" ]]; then
  echo "usage: ./release.sh beta [X.Y.Z]   next numbered beta (or beta 1 of a new line)"
  echo "       ./release.sh rc             next release candidate of the current line"
  echo "       ./release.sh X.Y.Z          stable — clears the beta/RC hold"
  echo "       ./release.sh patch|minor|major   stable, computed from the current version"
  exit 1
fi
if ! command -v gh >/dev/null; then
  echo "gh not found — install with: brew install gh && gh auth login"; exit 1
fi

# --- work out the next version + build from millenai.py
read -r VERSION BUILD BETA RC <<<"$(python3 - "$ARG" "$ARG2" <<'PY'
import re, sys, pathlib
arg, arg2 = sys.argv[1], sys.argv[2]
src = pathlib.Path("millenai.py").read_text()
cur = re.search(r'APP_VERSION = "([^"]+)"', src).group(1)
build = int(re.search(r"APP_BUILD = (\d+)", src).group(1)) + 1
beta = int(re.search(r"^APP_BETA = (\d+)", src, re.M).group(1))
rc = int(re.search(r"^APP_RC = (\d+)", src, re.M).group(1))

if arg == "beta":
    # numbered per line: a new version starts at beta 1, the same
    # version counts up; a line that already reached RC does not go back
    new_line = False
    if arg2:
        if not re.fullmatch(r"\d+\.\d+\.\d+", arg2):
            sys.exit("version must look like 1.2.3")
        if arg2 != cur:
            cur, beta, rc, new_line = arg2, 0, 0, True
    if rc:
        sys.exit(f"{cur} is already at RC{rc} — ./release.sh rc, or ./release.sh {cur} for stable")
    if not beta and not new_line:
        sys.exit(f"{cur} is a stable line — start a new one: ./release.sh beta X.Y.Z")
    if beta >= 9:
        # single-digit, Apple-style: nine betas is the cap for a line
        sys.exit(f"{cur} already has {beta} betas — ./release.sh rc, or ./release.sh {cur} for stable")
    version, beta, rc = cur, beta + 1, 0
elif arg == "rc":
    version, beta, rc = cur, 0, rc + 1
elif arg in ("patch", "minor", "major"):
    beta = rc = 0
    parts = [int(x) for x in re.findall(r"\d+", cur)]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if arg == "patch":
        patch += 1
    elif arg == "minor":
        minor, patch = minor + 1, 0
    else:
        major, minor, patch = major + 1, 0, 0
    version = f"{major}.{minor}.{patch}"
else:
    if not re.fullmatch(r"\d+\.\d+\.\d+", arg):
        sys.exit("version must look like 1.2.3")
    version, beta, rc = arg, 0, 0        # stable: the hold comes off
print(version, build, beta, rc)
PY
)"
[[ -n "$VERSION" ]] || { echo "could not work out the version"; exit 1; }

# the display name every surface agrees on: ONE trailing .0 falls away
SHOW="$VERSION"; [[ "$VERSION" == *.*.0 ]] && SHOW="${VERSION%.0}"
# BETA HOLD: while APP_BETA > 0, publish as a PRERELEASE — the desktop
# updater reads /releases/latest (prereleases excluded), so stable
# installs stay on the last stable until the line graduates. APP_RC > 0
# relabels the held cut as a RELEASE CANDIDATE (6b258): still a
# prerelease, just further along than "beta".
PRE=()
if (( RC > 0 )); then
  PRE=(--prerelease); SHOW="$SHOW RC$RC"
elif (( BETA > 0 )); then
  PRE=(--prerelease); SHOW="$SHOW beta $BETA"
fi

echo "→ $SHOW (build $BUILD)"
python3 - "$VERSION" "$BUILD" "$BETA" "$RC" <<'PY'
import pathlib, re, sys
version, build, beta, rc = sys.argv[1:5]
p = pathlib.Path("millenai.py"); s = p.read_text()
s = re.sub(r'APP_VERSION = "[^"]*"', 'APP_VERSION = "%s"' % version, s, count=1)
s = re.sub(r"APP_BUILD = \d+", "APP_BUILD = %s" % build, s, count=1)
s = re.sub(r"^APP_BETA = \d+", "APP_BETA = %s" % beta, s, count=1, flags=re.M)
s = re.sub(r"^APP_RC = \d+", "APP_RC = %s" % rc, s, count=1, flags=re.M)
p.write_text(s)
PY

echo "→ building macOS"
./build_dmg.sh >/dev/null
DMG="ConcordeAI-${VERSION}.dmg"   # build_dmg.sh derives this from millenai.py
[[ -f "$DMG" ]] || { echo "expected $DMG but it wasn't built"; exit 1; }

echo "→ building Windows"
./build_windows.sh >/dev/null
ZIP="ConcordeAI-${VERSION}-Windows.zip"
[[ -f "$ZIP" ]] || { echo "expected $ZIP but it wasn't built"; exit 1; }

echo "→ publishing v$BUILD"
git add -A && git commit -m "Release $SHOW (build $BUILD)" || true
git push origin HEAD
# WHAT'S NEW, IN HUMAN (6b257): the Updates pane renders the release
# BODY now — /api/update/check carries it — so every cut deserves a
# short bulleted summary a person would actually read. Write it into
# RELEASE_NOTES.md before releasing; it is consumed here and then
# cleared, so a stale list can never ship with the next build.
WHATS_NEW=""
if [[ -s RELEASE_NOTES.md ]]; then
  WHATS_NEW="$(cat RELEASE_NOTES.md)

"
else
  echo "  (no RELEASE_NOTES.md — the Updates pane will show the boilerplate only)"
fi
gh release create "v$BUILD" "$DMG" "$ZIP" \
  "${PRE[@]}" \
  --title "$SHOW" \
  --notes "${WHATS_NEW}**macOS** — download the .dmg. Existing installs update themselves.

**Windows** — the .msi installer (attached by CI a few minutes after
release) needs nothing else: no Python, no admin. On an NVIDIA machine CUDA
is used automatically. The .zip is the same app for people who prefer a
portable copy (needs Python 3.10+)."
# consumed — the next build writes its own
[[ -s RELEASE_NOTES.md ]] && : > RELEASE_NOTES.md

# THE HOSTED WEB UI UPDATES WITH THE CUT (6b248, per Patrick: "going
# forth always update the web ui too"). The live :9889 self-updates
# hourly on its own; running its updater here closes the gap so
# ai.millertechnology.net serves the new build the moment it exists.
if [[ -x "$HOME/Library/MillenAI-live/update.sh" ]]; then
  echo "→ updating the hosted web ui"
  bash "$HOME/Library/MillenAI-live/update.sh" \
    && echo "  live instance now on the new build" \
    || echo "  (live update failed — it will catch up on its hourly tick)"
fi

echo ""
echo "✓ published $SHOW — installs on the right channel will offer it within the hour."
