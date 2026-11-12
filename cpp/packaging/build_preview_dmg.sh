#!/bin/zsh
# The installable C++ preview (NOTES 6b614): "ConcordeAI C++ Preview.app",
# universal, its own bundle id (com.millen.millenai.cpp), its own data folder
# (~/Library/Application Support/MillenAI-cpp), its own port (9861), the updater
# off. Signed with the keychain's Developer ID and the hardened runtime,
# notarized and stapled through the concorde-notary profile, the way
# build_shell_dmg.sh does it for the shipping app (6b549).
#
#   cpp/packaging/build_preview_dmg.sh <build dir> <out dir>
#
# CONCORDE_SERVER_ROOT names the tree the bundled millenai.py, page and assets come
# from (default this repo; the demo build uses a snapshot of the newest beta).
# CONCORDE_SIGN_ID names an identity (default: the keychain's first
# "Developer ID Application"); CONCORDE_NOTARY_PROFILE a notarytool profile
# (default concorde-notary); CONCORDE_PREVIEW_NOTARIZE=0 signs without notarizing.
set -e
set -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
B="${1:?build dir}"; OUT="${2:?out dir}"
CMAKE="${CMAKE:-cmake}"
NAME="ConcordeAI C++ Preview"
ENT="$REPO/shell/src-tauri/entitlements.plist"

SIGN_ID="${CONCORDE_SIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
  | awk -F'"' '/"Developer ID Application: /{split($1, a, " "); print a[2]; exit}')}"
[[ -n "$SIGN_ID" ]] || { echo "✗ no Developer ID Application identity in the keychain" >&2; exit 1; }
NOTARY="${CONCORDE_NOTARY_PROFILE:-concorde-notary}"
[[ "${CONCORDE_PREVIEW_NOTARIZE:-1}" == "0" ]] && NOTARY=""
SIGN=(--force --options runtime --timestamp --sign "$SIGN_ID")

# 1. build: universal, Release, no probe, no web inspector
# the C++ engine rides along when CONCORDE_ENGINE_BIN (a built cpp/spikes/mlx
# llama_spike) and CONCORDE_MLX_LIB (the folder with libmlx.dylib and
# mlx.metallib) are set; Apple silicon only, the app stays universal
"$CMAKE" -S "$REPO/cpp" -B "$B" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCONCORDE_PREVIEW=ON \
  -DCONCORDE_PROBE=OFF -DCONCORDE_DEVTOOLS=OFF "-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64" \
  "-DCONCORDE_ENGINE_BIN=${CONCORDE_ENGINE_BIN:-}" "-DCONCORDE_MLX_LIB=${CONCORDE_MLX_LIB:-}" \
  "-DCONCORDE_SERVER_ROOT=${CONCORDE_SERVER_ROOT:-$REPO}" >/dev/null
"$CMAKE" --build "$B" --target ConcordeAI >/dev/null
SRC="$B/ConcordeAI.app"
[[ "$(lipo -archs "$SRC/Contents/MacOS/ConcordeAI")" == *x86_64*arm64* || \
   "$(lipo -archs "$SRC/Contents/MacOS/ConcordeAI")" == *arm64*x86_64* ]] || { echo "✗ not universal" >&2; exit 1; }
for f in millenai.py launch.sh crypto-requirements.txt concorde-dictate concorde-pdftext; do
  [[ -e "$SRC/Contents/Resources/$f" ]] || { echo "✗ Resources/$f is missing (build the helpers: shell/*/build.sh)" >&2; exit 1; }
done

# 2. the bundle under its preview name, signed inside out
mkdir -p "$OUT"
APP="$OUT/$NAME.app"
rm -rf "$APP"
ditto "$SRC" "$APP"
xattr -cr "$APP"
codesign "${SIGN[@]}" "$APP/Contents/Resources/concorde-dictate"
codesign "${SIGN[@]}" "$APP/Contents/Resources/concorde-pdftext"
if [[ -d "$APP/Contents/Resources/engine" ]]; then
  for f in "$APP/Contents/Resources/engine/"*.dylib "$APP/Contents/Resources/engine/concorde-llama"; do
    codesign "${SIGN[@]}" "$f"
  done
  echo "  the C++ engine is in: $(du -sh "$APP/Contents/Resources/engine" | cut -f1)"
fi
codesign "${SIGN[@]}" --entitlements "$ENT" "$APP"
codesign --verify --strict --deep "$APP"
echo "  signed: $(codesign -dvv "$APP" 2>&1 | awk -F= '/^Authority=Developer ID Application/{print $2}')"

# 3. notarized and stapled: the app, then the image that carries it
notarize() {
  local out id st
  out="$(xcrun notarytool submit "$1" --keychain-profile "$NOTARY" --wait --output-format json 2>&1)" || true
  read -r id st <<<"$(printf '%s' "$out" | python3 "$REPO/packaging/notary_verdict.py")"
  if [[ "$st" != "Accepted" ]]; then
    echo "✗ notarization of $2 failed: $st (submission $id)" >&2; echo "$out" >&2
    [[ "$id" != "-" ]] && xcrun notarytool log "$id" --keychain-profile "$NOTARY" >&2
    exit 1
  fi
  echo "  notarized $2 (submission $id)"
}
if [[ -n "$NOTARY" ]]; then
  NZ="$(mktemp -d)"
  ditto -c -k --keepParent "$APP" "$NZ/preview.zip"
  notarize "$NZ/preview.zip" "the app"
  rm -rf "$NZ"
  xcrun stapler staple -q "$APP"
  xcrun stapler validate -q "$APP"
fi

# 4. the disk image: the app and an Applications link
VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
BLD="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist")"
DMG="$OUT/ConcordeAI-Cpp-Preview-$VER-b$BLD.dmg"
ST="$(mktemp -d)"
ditto "$APP" "$ST/$NAME.app"
ln -s /Applications "$ST/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "$NAME" -srcfolder "$ST" -ov -format UDZO "$DMG"
rm -rf "$ST"
codesign --force --timestamp --sign "$SIGN_ID" "$DMG"
codesign --verify --strict "$DMG"
if [[ -n "$NOTARY" ]]; then
  notarize "$DMG" "the disk image"
  xcrun stapler staple -q "$DMG"
  xcrun stapler validate -q "$DMG"
  GK="$(spctl -a -t open --context context:primary-signature -v "$DMG" 2>&1)" || true
  [[ "$GK" == *": accepted"* && "$GK" == *"Notarized Developer ID"* ]] || { echo "✗ Gatekeeper: $GK" >&2; exit 1; }
  MP="$(mktemp -d)"
  hdiutil attach -nobrowse -readonly -noverify -quiet -mountpoint "$MP" "$DMG"
  GA="$(spctl -a -vv "$MP/$NAME.app" 2>&1)" || true
  hdiutil detach -quiet "$MP"; rmdir "$MP" 2>/dev/null || true
  [[ "$GA" == *": accepted"* && "$GA" == *"Notarized Developer ID"* ]] || { echo "✗ Gatekeeper (app): $GA" >&2; exit 1; }
  echo "  Gatekeeper accepts the image and the app in it: notarized, stapled"
fi
echo "  $DMG ($(du -h "$DMG" | cut -f1))"
