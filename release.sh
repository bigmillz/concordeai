#!/bin/zsh
# Publish a new ConcordeAI release to GitHub so existing installs self-update.
#
#   ./release.sh beta 6.1.0   beta 1 of a NEW line (prerelease, "6.1 beta 1")
#   ./release.sh beta         the NEXT beta of the current line ("6.1 beta 2")
#   ./release.sh rc           the next release candidate ("6.1 RC1", "6.1 RC2")
#   ./release.sh 6.1.0        STABLE: clears the beta/RC hold, lands on /releases/latest
#   ./release.sh patch|minor|major   stable, computed from the current version
#   ./release.sh --dry-run [ARG]  build every asset locally from the constants
#                             as they are; commit, tag, push and publish
#                             nothing. With ARG, also print the cut it would make.
#
# THE APP IS THE NATIVE SHELL FROM 7.0 (6b537, per Patrick: the move to
# Rust, step 1). The macOS asset is the Tauri shell's bundle in the same
# ConcordeAI-<version>.dmg (build_shell_dmg.sh), so a Python-app install
# updates into the shell and a shell install into the next shell through
# the one tag rule. CFBundleVersion is APP_BUILD (the tag's number) and
# the shell's version is APP_VERSION; both are verified in the built
# bundle before anything is published. The Windows assets stay the
# Python app's (the zip here, the MSI from CI) until the shell carries a
# Python of its own on Windows.
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

# The website checkout, wherever it lives on this machine. This repo sits in
# Drive and the site does not sit beside it, so it is searched for rather
# than assumed; CONCORDE_SITE overrides. Its download blocks are generated
# from GitHub Releases, so a cut has to be synced there or the page waits on
# the site's scheduled Action — which GitHub runs every few HOURS, not the
# 15 minutes its cron asks for.
SITE="${CONCORDE_SITE:-}"
if [[ -z "$SITE" ]]; then
  for candidate in "../concorde-site" "$HOME/Code/concorde-site"; do
    [[ -d "$candidate/.git" ]] && { SITE="$candidate"; break; }
  done
fi

DRY_RUN=0
if [[ "$1" == "--dry-run" ]]; then DRY_RUN=1; shift; fi
ARG="$1"; ARG2="${2:-}"
if [[ -z "$ARG" && "$DRY_RUN" == 0 ]]; then
  echo "usage: ./release.sh beta [X.Y.Z]   next numbered beta (or beta 1 of a new line)"
  echo "       ./release.sh rc             next release candidate of the current line"
  echo "       ./release.sh X.Y.Z          stable — clears the beta/RC hold"
  echo "       ./release.sh patch|minor|major   stable, computed from the current version"
  exit 1
fi
if [[ "$DRY_RUN" == 0 ]] && ! command -v gh >/dev/null; then
  echo "gh not found — install with: brew install gh && gh auth login"; exit 1
fi

# --- work out the next version + build from millenai.py
# (a dry run without ARG builds the constants as they are: "--dry-run"
# alone passes "current" through, which is not a cut)
read -r VERSION BUILD BETA RC <<<"$(python3 - "${ARG:-current}" "$ARG2" <<'PY'
import re, sys, pathlib
arg, arg2 = sys.argv[1], sys.argv[2]
src = pathlib.Path("millenai.py").read_text()
cur = re.search(r'APP_VERSION = "([^"]+)"', src).group(1)
build = int(re.search(r"APP_BUILD = (\d+)", src).group(1)) + 1
beta = int(re.search(r"^APP_BETA = (\d+)", src, re.M).group(1))
rc = int(re.search(r"^APP_RC = (\d+)", src, re.M).group(1))

if arg == "current":
    version, build = cur, build - 1          # a dry run of what is there
elif arg == "beta":
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
    if not beta and not arg2:
        # the working version runs ahead of the last stable (nightlies
        # are 6.0.3 while 6.0.2 is out), so an explicit version opens
        # its line at beta 1; a bare "beta" on a stable line is ambiguous
        sys.exit(f"{cur} has no betas yet — name the line: ./release.sh beta {cur}")
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

if [[ "$DRY_RUN" == 1 ]]; then
  if [[ -n "$ARG" ]]; then
    echo "→ DRY RUN: a real '$ARG' cut would publish $SHOW (build $BUILD) as tag v$BUILD${PRE:+ (prerelease)}"
    echo "  building the constants as they ARE instead; millenai.py is not stamped"
    read -r VERSION BUILD BETA RC <<<"$(python3 - <<'PY'
import re, pathlib
s = pathlib.Path("millenai.py").read_text()
print(re.search(r'APP_VERSION = "([^"]+)"', s).group(1),
      re.search(r"^APP_BUILD = (\d+)", s, re.M).group(1),
      re.search(r"^APP_BETA = (\d+)", s, re.M).group(1),
      re.search(r"^APP_RC = (\d+)", s, re.M).group(1))
PY
)"
    PRE=(); (( RC > 0 || BETA > 0 )) && PRE=(--prerelease)
  else
    echo "→ DRY RUN: $SHOW (build $BUILD), the constants as they are"
  fi
else
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
fi

echo "→ building macOS (the native shell)"
./build_shell_dmg.sh >/dev/null
DMG="ConcordeAI-${VERSION}.dmg"   # build_dmg.sh derives this from millenai.py
[[ -f "$DMG" ]] || { echo "expected $DMG but it wasn't built"; exit 1; }
# THE BUNDLE SAYS WHAT THE TAG SAYS (6b537): CFBundleVersion is the build
# the updater compares (v$BUILD), the short version is the version. Read
# off the bundle that went into the image, not off the config that fed it.
PL="ConcordeAI.app/Contents/Info.plist"
GOT_BUILD="$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$PL")"
GOT_VER="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PL")"
[[ "$GOT_BUILD" == "$BUILD" ]] || { echo "the bundle's CFBundleVersion is $GOT_BUILD, not $BUILD"; exit 1; }
[[ "$GOT_VER" == "$VERSION" ]] || { echo "the bundle's CFBundleShortVersionString is $GOT_VER, not $VERSION"; exit 1; }
echo "  $DMG — CFBundleVersion $GOT_BUILD, CFBundleShortVersionString $GOT_VER"
# HOW THE MAC BUILD IS SIGNED, SAID OUT LOUD (6b549), read off what was
# built (the bundle's authority, the tickets stapled to the app and the
# image), not off the settings that fed it. docs/SIGNING.md sets it up.
SIGNED_BY="$(codesign -dvv ConcordeAI.app 2>&1 | sed -n 's/^Authority=\(Developer ID Application: .*\)$/\1/p' | head -1)"
if [[ -n "$SIGNED_BY" ]] && xcrun stapler validate -q ConcordeAI.app >/dev/null 2>&1 \
   && xcrun stapler validate -q "$DMG" >/dev/null 2>&1; then
  MAC_SIGNING="notarized and stapled ($SIGNED_BY)"
elif [[ -n "$SIGNED_BY" ]]; then
  MAC_SIGNING="Developer ID but NOT notarized ($SIGNED_BY): macOS still warns on first open"
else
  MAC_SIGNING="ad hoc (no Developer ID): macOS warns on first open"
fi
echo "  macOS signing: $MAC_SIGNING"

echo "→ building Windows"
./build_windows.sh >/dev/null
ZIP="ConcordeAI-${VERSION}-Windows.zip"
[[ -f "$ZIP" ]] || { echo "expected $ZIP but it wasn't built"; exit 1; }

if [[ "$DRY_RUN" == 1 ]]; then
  echo ""
  echo "✓ dry run: built, verified, published nothing"
  ls -la "$DMG" "$ZIP" | awk '{printf "  %s  %.1f MB\n", $NF, $5/1e6}'
  echo "  macOS signing: $MAC_SIGNING"
  echo "  a real run would: commit, push, gh release create v$BUILD $DMG $ZIP${PRE:+ --prerelease}"
  exit 0
fi

echo "→ publishing v$BUILD"
git add -A && git commit -m "Release $SHOW (build $BUILD)" || true
git push origin HEAD
# WHAT'S NEW, IN HUMAN (6b257): every cut deserves a short bulleted
# summary a person would actually read. Write it into RELEASE_NOTES.md
# before releasing; it is consumed here and then cleared, so a stale list
# can never ship with the next build. ITS OWN FIELD (6b630, per Patrick):
# it goes between the whats-new markers, which GitHub's page doesn't show,
# and the app shows only that (user_notes in millenai.py), never the
# install notes under it (the .msi, the .zip).
WHATS_NEW=""
if [[ -s RELEASE_NOTES.md ]]; then
  WHATS_NEW="$(cat RELEASE_NOTES.md)"
else
  echo "  (no RELEASE_NOTES.md — the app's What's new will be empty for this cut)"
fi
gh release create "v$BUILD" "$DMG" "$ZIP" \
  "${PRE[@]}" \
  --title "$SHOW" \
  --notes "<!-- whats-new -->
${WHATS_NEW}
<!-- /whats-new -->

**macOS** — download the .dmg. Existing installs update themselves.

**Windows** — the .msi installer (attached by CI a few minutes after
release) needs nothing else: no Python, no admin. On an NVIDIA machine CUDA
is used automatically. The .zip is the same app for people who prefer a
portable copy (needs Python 3.10+)."
# consumed — the next build writes its own
[[ -s RELEASE_NOTES.md ]] && : > RELEASE_NOTES.md

# THE WEBSITE (flyconcordefly.com). Land on its current tip and regenerate
# there — never rebase generated content: two syncs that each INSERT the
# same block at the same anchor replay as two insertions with no conflict,
# and the page ships a channel twice (it did, on 2026-09-18). Nightlies do
# not need this — they are built by Actions and the site reads a nightly's
# commit and date live — but a beta, RC or stable only reaches the page
# from here or from that slow scheduled sync.
if [[ -n "$SITE" ]]; then
  echo "→ syncing the website"
  # Resetting is what makes this immune to that race, so it must never run
  # over work that is not pushed — a curated release note written just
  # before a cut is exactly the thing that would be sitting there.
  if [[ -n "$(git -C "$SITE" status --porcelain)" ]]; then
    echo "  site checkout has uncommitted changes — leaving it alone:"
    git -C "$SITE" status --short | sed 's/^/    /'
  elif [[ "$(git -C "$SITE" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)" != "0" ]]; then
    echo "  site checkout has unpushed commits — leaving it alone"
  else
    for attempt in 1 2 3; do
      # Every git step is guarded: the release is already published, so a
      # blip here must not abort with an exit that reads like it failed.
      if ! { git -C "$SITE" fetch -q origin main \
             && git -C "$SITE" reset -q --hard origin/main; }; then
        echo "  could not reach the site repo — leaving it to the Action"; break
      fi
      if ! python3 "$SITE/tools/sync-releases.py"; then
        if [[ "$attempt" != 3 ]]; then
          echo "  site sync failed (attempt $attempt) — retrying"; sleep 20; continue
        fi
        echo "  site sync failed — leaving it to the Action"; break
      fi
      if git -C "$SITE" diff --quiet -- index.html; then
        echo "  site already current"; SITE_OK=1; break
      fi
      if ! { git -C "$SITE" add index.html \
             && git -C "$SITE" commit -q -m "ConcordeAI $SHOW"; }; then
        echo "  could not commit the site change — leaving it to the Action"; break
      fi
      if git -C "$SITE" push -q origin main 2>/dev/null; then
        echo "  site pushed"; SITE_OK=1; break
      fi
      echo "  site push rejected (attempt $attempt) — regenerating on the new tip"
      [[ "$attempt" == 3 ]] && echo "  gave up; the Action will catch it"
    done
  fi
else
  echo "  (no concorde-site checkout found — set CONCORDE_SITE to sync the website)"
fi

echo ""
echo "✓ published $SHOW — installs on the right channel will offer it within the hour."
echo "  shipped macOS: $MAC_SIGNING"
if [[ -n "$SITE_OK" ]]; then
  echo "  the website is up to date"
elif [[ -n "$SITE" ]]; then
  echo "  the website is NOT updated — the scheduled sync will catch it"
fi
