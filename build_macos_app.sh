#!/bin/zsh
# Builds ConcordeAI.app — a double-clickable native macOS bundle.
# (Executable, bundle id, icns filename and data dirs stay "MillenAI":
#  the updater pgreps the binary and WebKit keys storage to the bundle id.)
# Version comes from APP_VERSION/APP_BUILD in millenai.py.
# Creates a private venv so it never fights Homebrew's managed Python (PEP 668).
# Run from the folder containing millenai.py and MillenAI.icns:
#   chmod +x build_macos_app.sh && ./build_macos_app.sh
set -e

BASE_PY="$(command -v python3)"
if [[ -z "$BASE_PY" ]]; then
  echo "python3 not found on PATH"; exit 1
fi

# --- private venv, safely outside Homebrew's jurisdiction
VENV="$HOME/Library/Application Support/MillenAI/venv"
if [[ ! -x "$VENV/bin/python3" ]]; then
  echo "creating venv at: $VENV"
  "$BASE_PY" -m venv "$VENV"
fi

echo "installing dependencies into the venv…"
"$VENV/bin/pip" install --quiet --upgrade pip
# pywebview is pinned (6b321): the app guards its js_api bridge, which
# was written and tested against this version
"$VENV/bin/pip" install --quiet pywebview==6.2.1 ddgs psutil mlx-lm mlx-whisper
# 6b295: the export engines. Their own line with || true — a wheel that
# fails to fetch must degrade to the in-app on-demand install, never to a
# build that cannot run.
"$VENV/bin/pip" install --quiet --only-binary=:all: \
  reportlab openpyxl python-docx python-pptx || true
# 6b327: PyNaCl for accounts, the hash-pinned wheels (cffi and pycparser
# with it) written out from CRYPTO_REQS in millenai.py, the one list. Its
# own call, wheels only, every hash checked. Not fatal: the app fetches
# the same pins itself at its next start.
python3 packaging/crypto_reqs.py millenai.py crypto-requirements.txt
# --force-reinstall: --require-hashes checks only what it downloads, so a
# copy already in this long-lived venv is fetched and checked again
"$VENV/bin/pip" install --quiet --only-binary=:all: --require-hashes --no-deps \
  --force-reinstall -r crypto-requirements.txt \
  || echo "  ! PyNaCl didn't install here; the app installs it at its next start"
PY="$VENV/bin/python3"
echo "app will run on: $PY"

# Bundle stays version-less on purpose: dropping a new build into
# /Applications then replaces the old one instead of piling up copies.
# The version lives in the UI and on the disk image, not in the filename.
# single source of truth for the version lives in millenai.py
APP_VERSION="$(python3 -c "import re;print(re.search(r'APP_VERSION = \"([^\"]+)\"', open('millenai.py').read()).group(1))")"
APP_BUILD="$(python3 -c "import re;print(re.search(r'APP_BUILD = (\d+)', open('millenai.py').read()).group(1))")"
echo "version: $APP_VERSION (build $APP_BUILD)"

APP="ConcordeAI.app"
rm -rf "$APP" Concorde.app MillenAI.app "MillenAI Beta 2.app"   # drop old-brand bundles too
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp millenai.py "$APP/Contents/Resources/"
# the launcher's first run installs these pins (6b327), and the engine at
# the app's own pin (6b630)
cp crypto-requirements.txt "$APP/Contents/Resources/"
python3 packaging/shell_stamp.py --engine-reqs "$APP/Contents/Resources/engine-requirements.txt"
[[ -f MillenAI.icns ]] && cp MillenAI.icns "$APP/Contents/Resources/"
# the titlebar lockup is a native NSTextField and cannot pull Michroma
# from Google the way the page does — it needs the real file (6b258)
[[ -d fonts ]] && cp -R fonts "$APP/Contents/Resources/"
[[ -d vendor ]] && cp -R vendor "$APP/Contents/Resources/"    # the map's scripts (6b620)
# the HDR light source (6b261): one tiny PQ clip, same as ConcordeVPN
[[ -d vfx ]] && cp -R vfx "$APP/Contents/Resources/"

# --- Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>ConcordeAI</string>
  <key>CFBundleDisplayName</key>     <string>ConcordeAI</string>
  <key>CFBundleIdentifier</key>      <string>com.millen.millenai</string>
  <key>CFBundleVersion</key>         <string>${APP_BUILD}</string>
  <key>CFBundleShortVersionString</key> <string>${APP_VERSION}</string>
  <key>CFBundleExecutable</key>      <string>MillenAI</string>
  <key>CFBundleIconFile</key>        <string>MillenAI</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>NSHighResolutionCapable</key> <true/>
  <key>LSMinimumSystemVersion</key>  <string>11.0</string>
  <key>NSLocalNetworkUsageDescription</key> <string>ConcordeAI wakes your own server over your home network and talks to it there.</string>
  <key>NSMicrophoneUsageDescription</key> <string>ConcordeAI uses the microphone for voice input — audio never leaves this Mac.</string>
</dict>
</plist>
PLIST

# --- launcher: self-bootstrapping, so the .app works on a fresh Mac.
# On first run it downloads the app's own Python (a pinned, hash-checked
# python-build-standalone 3.12, never the system's: 6b630), builds a
# private venv from it in ~/Library/Application Support and pip-installs
# the engine deps (needs internet, a few minutes). The native shell runs
# it too (--bootstrap --shell) and draws the progress itself.
# It lives in Resources (6b443): the main executable is the native launcher
# below. DIR is Resources now, so "$DIR/../.." is still the bundle and
# "$DIR/../Resources" still Resources. The launcher runs it with
# --bootstrap (6b455): the venv check and setup, then exit 0 instead of
# running python, which the launcher then does in its own process. Without
# the flag it still execs python: the launcher's fallback for a python
# with no framework dylib to load.
# NOTE: single-quoted heredoc — nothing here is expanded at build time.
cat > "$APP/Contents/Resources/launch.sh" <<'LAUNCH'
#!/bin/zsh
DIR="$(cd "$(dirname "$0")" && pwd)"

# We only get here if the user already cleared Gatekeeper once, so strip the
# download flag from our own bundle: later launches, and drag-and-drop
# updates onto this same bundle, then open without prompting again.
# (Nothing can do this *before* first launch — that is the whole point of
# quarantine — so the DMG still has to explain the one-time approval.)
xattr -dr com.apple.quarantine "$DIR/../.." 2>/dev/null || true

SUPPORT="$HOME/Library/Application Support/MillenAI"
LOGDIR="$HOME/Library/Logs/MillenAI"
VENV="$SUPPORT/venv"
PY="$VENV/bin/python3"
LOG="$LOGDIR/bootstrap.log"
mkdir -p "$SUPPORT" "$LOGDIR"
# --shell (6b630): the native shell runs this with --bootstrap --shell and
# draws its own setup screen from the "@@" lines below; it needs no pywebview
SHELLRUN=0; [[ "$2" == "--shell" ]] && SHELLRUN=1
ARM=0; [[ "$(uname -m)" == "arm64" ]] && ARM=1

# OUR OWN PYTHON (6b630, per Patrick). A fresh Mac's python3 is the Command
# Line Tools' 3.9 (or a dialog asking to install them), and pip on 3.9 stops
# at mlx-lm 0.29.1, which can't load Gemma 4 or Qwen 3.5. So the app never
# runs the system Python: a standalone CPython (python-build-standalone),
# pinned by version and SHA-256 for each architecture, is downloaded once
# into the data folder and the venv is made from it. curl, shasum and tar
# come with macOS itself.
PBS_TAG=20261026
PBS_VER=3.12.16
case "$(uname -m)" in
  arm64)  PBS_ARCH=aarch64 PBS_BYTES=25035343
          PBS_SHA=353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e ;;
  x86_64) PBS_ARCH=x86_64 PBS_BYTES=24757272
          PBS_SHA=37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734 ;;
  *)      PBS_ARCH="" PBS_BYTES=1 PBS_SHA="" ;;
esac
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_VER%2B$PBS_TAG-$PBS_ARCH-apple-darwin-install_only_stripped.tar.gz"
BASE_DIR="$SUPPORT/python/cpython-$PBS_VER+$PBS_TAG"
BASE="$BASE_DIR/bin/python3"

# what the shell's screen reads: "@@steps N", "@@step I NAME", "@@pct P",
# "@@item NAME"; everything else goes to the log
say() { print -r -- "$*"; }
step() { say "@@step $1 $2"; print -r -- "== step $1: $2 ($(date +%T))" >> "$LOG"; }
# a command's output into the log; the package pip is looking at, the file
# it is downloading (its name and size) and that file's "Progress n of m"
# (pip --progress-bar raw) become screen lines
to_log() {
  local l; local -a w
  while IFS= read -r l; do
    print -r -- "$l" >> "$LOG"
    case "$l" in
      "Progress "*" of "*) w=(${=l}); (( w[4] > 0 )) && say "@@pct $(( w[2] * 100 / w[4] ))" ;;
      "Collecting "*) w=(${=l}); say "@@item ${w[2]%%[<>=~!;\[]*}" ;;
      "Downloading "*" ("*")") w=(${=l}); say "@@item ${w[2]%%-*} · ${w[3]#\(} ${w[4]%\)}" ;;
    esac
  done
}
pip_do() { "$VENV/bin/pip" "$@" --progress-bar raw 2>&1 | to_log; return ${pipestatus[1]}; }

# the venv's Python from its pyvenv.cfg (never by running it): older than
# 3.11 is the system's from an earlier first run, on which the engine can't
# get past mlx-lm 0.29.1; such a venv is made again from our Python
too_old() {
  local v
  [[ -f "$VENV/pyvenv.cfg" ]] || return 1
  v="$(sed -n 's/^version[_info]* *= *//p' "$VENV/pyvenv.cfg" | head -1)"
  [[ "$v" == 3.* ]] || return 1
  v="${v#3.}"; v="${v%%.*}"
  (( v < 11 ))
}
# a setup stopped half way (Quit, a lost connection) leaves this behind, so
# the next launch finishes it instead of starting on half a venv (6b630)
UNFINISHED="$VENV/.setup-unfinished"
ready() {
  [[ -x "$PY" && ! -e "$UNFINISHED" ]] && ! too_old || return 1
  if (( SHELLRUN )); then
    "$PY" -c 'import importlib.util as u, sys
sys.exit(not all(u.find_spec(m) for m in sys.argv[1:]))' ddgs psutil ${${ARM:#0}:+mlx_lm} 2>/dev/null
  else
    "$PY" -c "import webview" 2>/dev/null
  fi
}
get_python() {
  local tmp got sum cp
  if [[ -x "$BASE" ]] && "$BASE" -c "import ssl, venv" 2>/dev/null; then return 0; fi
  [[ -n "$PBS_ARCH" ]] || { print -r -- "no Python for $(uname -m)" >> "$LOG"; return 1; }
  tmp="$SUPPORT/python/.get-$$"
  rm -rf "$SUPPORT/python/.get-"*(N); mkdir -p "$tmp" || return 1
  print -r -- "fetching $PBS_URL" >> "$LOG"
  /usr/bin/curl -fsSL --retry 3 --connect-timeout 20 -o "$tmp/py.tar.gz" "$PBS_URL" 2>> "$LOG" &
  cp=$!
  while kill -0 $cp 2>/dev/null; do
    got=$(/usr/bin/stat -f %z "$tmp/py.tar.gz" 2>/dev/null || echo 0)
    say "@@pct $(( got * 100 / PBS_BYTES ))"
    /bin/sleep 0.3
  done
  wait $cp || { rm -rf "$tmp"; return 1; }
  sum="$(/usr/bin/shasum -a 256 "$tmp/py.tar.gz" | /usr/bin/cut -d' ' -f1)"
  if [[ "$sum" != "$PBS_SHA" ]]; then
    print -r -- "checksum mismatch: got $sum, want $PBS_SHA" >> "$LOG"
    rm -rf "$tmp"; return 1
  fi
  /usr/bin/tar -xzf "$tmp/py.tar.gz" -C "$tmp" 2>> "$LOG" || { rm -rf "$tmp"; return 1; }
  rm -rf "$BASE_DIR" && mv "$tmp/python" "$BASE_DIR" && rm -rf "$tmp" || return 1
  "$BASE" -c "import ssl, venv" >> "$LOG" 2>&1
}
OLD=""
fail() {
  print -r -- "== setup failed ($(date +%T))" >> "$LOG"
  # an old venv that was moved aside comes back: the app still opens on it
  if [[ -n "$OLD" && -d "$OLD" ]]; then rm -rf "$VENV"; mv "$OLD" "$VENV"; fi
  if (( ! SHELLRUN )); then
    /usr/bin/osascript -e 'display dialog "ConcordeAI couldn’t finish setting up. Check your internet connection and open it again." buttons {"OK"} with title "ConcordeAI"' 2>/dev/null || true
  fi
  exit 1
}

if ! ready; then
  (( SHELLRUN )) || /usr/bin/osascript -e 'display notification "Setting up. This takes a few minutes the first time." with title "ConcordeAI"' 2>/dev/null || true
  print -r -- "--- setup $(date) $(uname -m) $(sw_vers -productVersion 2>/dev/null)" >> "$LOG"
  say "@@steps $(( 3 + ARM ))"
  step 1 python
  get_python || fail
  step 2 venv
  if too_old; then OLD="$SUPPORT/venv-old-$$"; mv "$VENV" "$OLD" || fail; fi
  "$BASE" -m venv "$VENV" >> "$LOG" 2>&1 || fail
  : > "$UNFINISHED"
  pip_do install --upgrade pip || fail
  step 3 base
  if (( SHELLRUN )); then pip_do install ddgs psutil || fail
  else pip_do install pywebview==6.2.1 ddgs psutil || fail; fi
  # PyNaCl for accounts (6b327): hash-pinned wheels only. Not fatal; the
  # app tries the same pins again when it starts
  "$VENV/bin/pip" install --only-binary=:all: --require-hashes --no-deps \
    -r "$DIR/../Resources/crypto-requirements.txt" >> "$LOG" 2>&1 || true
  # MLX exists only for Apple silicon; Intel Macs run the models on Ollama,
  # which the app downloads for itself on first use. The engine is the
  # app's pin (engine-requirements.txt, written from MLX_LM_PIN and
  # MLX_PIN at build time, 6b630); the shell can't start without it
  if (( ARM )); then
    step 4 engine
    ENG="$DIR/../Resources/engine-requirements.txt"
    if [[ -f "$ENG" ]]; then pip_do install -r "$ENG" mlx-whisper
    else pip_do install mlx-lm mlx-whisper; fi || (( ! SHELLRUN )) || fail
    "$VENV/bin/pip" install --only-binary=:all: reportlab openpyxl \
      python-docx python-pptx >> "$LOG" 2>&1 || true
  fi
  rm -f "$UNFINISHED"
  ready || { : > "$UNFINISHED"; fail; }
  [[ -n "$OLD" ]] && rm -rf "$OLD"
  print -r -- "== setup done ($(date +%T))" >> "$LOG"
fi

# --bootstrap (6b455): the venv is ready; the launcher runs python itself
[[ "$1" == "--bootstrap" ]] && exit 0
exec "$PY" "$DIR/../Resources/millenai.py"
LAUNCH
chmod +x "$APP/Contents/Resources/launch.sh"

# --- the main executable: a native Mach-O launcher (6b443, per Patrick:
# "Neither Concord AI nor Python is listed under local network and privacy
# and security settings"). macOS asks for Local Network on behalf of the
# bundle's main executable, by its signature and Mach-O UUID (TN3179). A
# zsh script has no UUID, and exec'ing python turned the process into
# Homebrew's Python.app, so LAN connections to the server failed with "No
# route to host" and no alert. The launcher IS python (6b455, per Patrick:
# the Dock showed the window as a Python rocket and a click on the app's
# icon started a second copy): it loads the venv's Python framework dylib
# into its own process, so the window's process is this bundle, with its
# icon, its name and one Dock entry, and this bundle stays the responsible
# code. Universal when the SDK allows; one arch is enough to run. No
# compiler, no app: a script launcher would bring the silent block back.
LSRC="packaging/macos_launcher.m"
LFLAGS=(-O2 -fobjc-arc -mmacosx-version-min=11.0 -framework Cocoa
        -DLAUNCHER_TAG='"com.millen.millenai"')
if ! xcrun clang "${LFLAGS[@]}" -arch arm64 -arch x86_64 \
       -o "$APP/Contents/MacOS/MillenAI" "$LSRC" 2>/dev/null; then
  echo "  universal launcher build failed; building for $(uname -m) only"
  xcrun clang "${LFLAGS[@]}" -arch "$(uname -m)" \
    -o "$APP/Contents/MacOS/MillenAI" "$LSRC"
fi
lipo -archs "$APP/Contents/MacOS/MillenAI" | sed 's/^/  launcher archs: /'

touch "$APP"

# --- ad-hoc signature (free, no Apple account).
# This does NOT satisfy Gatekeeper — only a paid Developer ID + notarization
# does — but it gives the bundle a stable identity, avoids the harsher
# "app is damaged" rejection, and keeps Apple silicon happy.
# The launcher must carry it (6b443): Local Network tracks the app by it,
# so a bundle that won't verify is a failed build, not a warning.
codesign --force --sign - "$APP"
codesign --verify --strict "$APP" && echo "  ad-hoc signed"

echo ""
echo "✓ built $APP"
echo "  drag it into /Applications."
echo "  first launch on another Mac: System Settings ▸ Privacy & Security"
echo "  ▸ scroll down ▸ 'Open Anyway'  (macOS 15+ removed right-click→Open)"
