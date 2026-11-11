#!/bin/zsh
# Builds the native shell (shell/, Tauri 2) as ConcordeAI.app and packages it
# as ConcordeAI-<version>.dmg — THE macOS asset from 7.0 (6b537). Same bundle
# name, same bundle id (com.millen.millenai), same DMG name and the same
# .dmg-by-extension rule the updater picks by, so a Python-app install
# updates into the shell through the one tag rule (RUST_PLAN.md §7), and
# the styled disk image is build_dmg.sh's, fed this bundle instead of the
# Python one.
#
#   ./build_shell_dmg.sh            # from the repo root; release.sh calls it
#
# What it guarantees, and verifies before it hands the bundle on:
#   * the shell's version is stamped from APP_VERSION (packaging/shell_stamp.py)
#     and lands in CFBundleShortVersionString;
#   * CFBundleVersion is APP_BUILD — the tag the updater compares is
#     v<APP_BUILD>, so the bundle's own build must match it. Tauri writes
#     the version there; the bundle is patched after the build and signed
#     again, and both keys are read back;
#   * Resources/ carries millenai.py, fonts/, vfx/, the first-run launch.sh,
#     crypto-requirements.txt (the sidecar's venv bootstrap, 6b537),
#     engine-requirements.txt (the MLX engine at the app's pin, 6b630) and
#     concorde-dictate, the on-device speech helper (6b543, built here by
#     shell/dictate/build.sh and signed on its own) and concorde-pdftext,
#     the on-device PDF reader (6b546, shell/pdftext/build.sh, the same way);
#   * the bundle verifies, signed the strongest way this Mac can (6b549):
#     a Developer ID with the hardened runtime, notarized and stapled when
#     the notary profile is there too; ad hoc, as before, when it isn't.
#     docs/SIGNING.md is the owner's side of it.
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:/opt/homebrew/opt/rustup/bin:$PATH"
command -v cargo >/dev/null || { echo "cargo not found (rustup: brew install rustup)"; exit 1; }
cargo tauri --version >/dev/null 2>&1 || { echo "tauri-cli not found: cargo install tauri-cli --version '^2' --locked"; exit 1; }

read -r VER BUILD <<<"$(python3 packaging/shell_stamp.py)"
[[ -n "$VER" && -n "$BUILD" ]] || { echo "could not stamp the shell from millenai.py"; exit 1; }
echo "→ shell $VER (build $BUILD)"

# SIGNING (6b549, per Patrick: his mom was stopped by macOS's "can't check
# this app" on the ad-hoc 7.0 beta 1). Decided before anything is built:
#   developer-id  CONCORDE_SIGN_ID names an identity (CONCORDE_SIGN_IDENTITY,
#                 its old name, too), or the keychain's first valid
#                 "Developer ID Application" is taken (by its hash: two
#                 certificates can share a name). Inside out, the helpers
#                 first, then the bundle (its main binary with it), all
#                 with the hardened runtime and a secure timestamp, the
#                 bundle with shell/src-tauri/entitlements.plist; the DMG
#                 is signed too. With a notarytool profile (concorde-notary,
#                 or CONCORDE_NOTARY_PROFILE) the app and then the DMG are
#                 notarized and stapled and Gatekeeper is asked about both.
#   rehearsal     CONCORDE_SIGN_HARDENED=1 and no identity: ad hoc, but with
#                 the hardened runtime and the entitlements, so a missing
#                 entitlement shows before the certificate arrives. Never
#                 for a release.
#   adhoc         neither: exactly as before, and one line says so.
# CONCORDE_SIGN_ID=- is ad hoc whatever the keychain holds.
ENTITLEMENTS="shell/src-tauri/entitlements.plist"
[[ -f "$ENTITLEMENTS" ]] || { echo "$ENTITLEMENTS is missing"; exit 1; }
SIGN_ID="${CONCORDE_SIGN_ID:-${CONCORDE_SIGN_IDENTITY:-}}"
if [[ -z "$SIGN_ID" ]]; then
  SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' '/"Developer ID Application: /{split($1, a, " "); print a[2]; exit}')"
fi
NOTARY=""
if [[ -n "$SIGN_ID" && "$SIGN_ID" != "-" ]]; then
  SIGN_MODE="developer-id"
  SIGN=(--force --options runtime --timestamp --sign "$SIGN_ID")
  SIGN_NAME="$(security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' -v id="$SIGN_ID" 'index($1, id) || $2 == id {print $2; exit}')"
  [[ -n "$SIGN_NAME" ]] || { echo "✗ no valid signing identity matches $SIGN_ID in the keychain" >&2; exit 1; }
  NOTARY="${CONCORDE_NOTARY_PROFILE:-concorde-notary}"
  # history answers only with working credentials; a profile that was
  # never stored is the one quiet case, anything else stops the build
  if ! NOUT="$(xcrun notarytool history --keychain-profile "$NOTARY" 2>&1)"; then
    if [[ -z "${CONCORDE_NOTARY_PROFILE:-}" && "$NOUT" == *"No Keychain password item found"* ]]; then
      NOTARY=""
    else
      echo "✗ the notary profile \"$NOTARY\" is there but notarytool can't use it:" >&2
      echo "$NOUT" >&2; exit 1
    fi
  fi
  if [[ -n "$NOTARY" ]]; then
    echo "  signing as $SIGN_NAME, notarizing with the \"$NOTARY\" profile"
  else
    echo "  signing as $SIGN_NAME; no concorde-notary profile, so it won't be notarized (docs/SIGNING.md, step 3)"
  fi
elif [[ "${CONCORDE_SIGN_HARDENED:-}" == 1 ]]; then
  SIGN_MODE="rehearsal"; SIGN_ID="-"
  SIGN=(--force --options runtime --sign -)
  echo "  rehearsal: ad hoc with the hardened runtime and the entitlements (never for a release)"
else
  SIGN_MODE="adhoc"; SIGN_ID="-"
  SIGN=(--force --sign -)
  echo "  no Developer ID in the keychain: signing ad hoc, as before (docs/SIGNING.md sets one up)"
fi

# notarytool's verdict on one file, or a loud stop with Apple's own log
notarize() {
  local out id st
  out="$(xcrun notarytool submit "$1" --keychain-profile "$NOTARY" --wait --output-format json 2>&1)" || true
  read -r id st <<<"$(printf '%s' "$out" | python3 packaging/notary_verdict.py)"
  if [[ "$st" != "Accepted" ]]; then
    echo "✗ notarization of $2 failed: ${st//_/ } (submission $id)" >&2
    echo "$out" >&2
    [[ "$id" != "-" ]] && xcrun notarytool log "$id" --keychain-profile "$NOTARY" >&2
    exit 1
  fi
  echo "  notarized $2 (submission $id)"
}

# UNIVERSAL, LIKE THE PYTHON APP'S LAUNCHER (6b443): Intel Macs run the
# app on Ollama and update by the same .dmg, so an arm64-only shell would
# be an app they can't open. Both Apple targets installed (rustup target
# add x86_64-apple-darwin) build one universal binary; with one target
# the build is that machine's arch alone and says so — fine for a dev
# image, never for a release (CONCORDE_SHELL_ARCH=native asks for it).
TARGET="universal-apple-darwin"
if [[ "${CONCORDE_SHELL_ARCH:-}" == "native" ]]; then
  TARGET=""
elif ! rustup target list --installed 2>/dev/null | grep -q '^x86_64-apple-darwin$' \
  || ! rustup target list --installed 2>/dev/null | grep -q '^aarch64-apple-darwin$'; then
  echo "  ! only one Apple target is installed: building $(uname -m) only (rustup target add x86_64-apple-darwin aarch64-apple-darwin for universal)"
  TARGET=""
fi
# the bundle only; the disk image is build_dmg.sh's (Finder layout, the
# starfield, the label), which Tauri's plain dmg target doesn't do
TARGS=(); [[ -n "$TARGET" ]] && TARGS=(--target "$TARGET")
# the on-device speech helper (6b543) goes in beside millenai.py
./shell/dictate/build.sh
[[ -x shell/dictate/concorde-dictate ]] || { echo "shell/dictate/concorde-dictate wasn't built"; exit 1; }
# and the PDF reader (6b546)
./shell/pdftext/build.sh
[[ -x shell/pdftext/concorde-pdftext ]] || { echo "shell/pdftext/concorde-pdftext wasn't built"; exit 1; }
( cd shell/src-tauri && cargo tauri build --bundles app "${TARGS[@]}" )
BUNDLE="shell/src-tauri/target/${TARGET:+$TARGET/}release/bundle/macos/ConcordeAI.app"
[[ -d "$BUNDLE" ]] || { echo "expected $BUNDLE but it wasn't built"; exit 1; }
PLIST="$BUNDLE/Contents/Info.plist"
ARCHS="$(lipo -archs "$BUNDLE/Contents/MacOS/ConcordeAI")"
if [[ -n "$TARGET" && "$ARCHS" != *arm64* || -n "$TARGET" && "$ARCHS" != *x86_64* ]]; then
  echo "expected a universal binary, got: $ARCHS"; exit 1
fi
echo "  archs: $ARCHS"

# CFBundleVersion = APP_BUILD (Tauri wrote the version there), the short
# version re-asserted, then the bundle is signed again: a changed plist
# invalidates the seal. How it is signed is SIGN_MODE's (above).
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$PLIST"
# the helpers are nested executables: copied in and signed on their own,
# inside out, before the bundle's seal is written over them. They take no
# entitlements: neither opens the microphone (the page does) or loads code
ditto shell/dictate/concorde-dictate "$BUNDLE/Contents/Resources/concorde-dictate"
codesign "${SIGN[@]}" "$BUNDLE/Contents/Resources/concorde-dictate"
ditto shell/pdftext/concorde-pdftext "$BUNDLE/Contents/Resources/concorde-pdftext"
codesign "${SIGN[@]}" "$BUNDLE/Contents/Resources/concorde-pdftext"
# the map's scripts, when they are shipped (vendor/, 6b620): the page serves
# them from /static/vendor/ and reaches for unpkg only without them
[[ -d vendor ]] && ditto vendor "$BUNDLE/Contents/Resources/vendor"
# then the bundle, which signs its main binary (Contents/MacOS/ConcordeAI)
# as its own; under the hardened runtime that binary carries the entitlements
if [[ "$SIGN_MODE" == "adhoc" ]]; then
  codesign "${SIGN[@]}" "$BUNDLE"
else
  codesign "${SIGN[@]}" --entitlements "$ENTITLEMENTS" "$BUNDLE"
fi
codesign --verify --strict "$BUNDLE"
# read the signatures back: every Mach-O in the bundle under the hardened
# runtime (and, with a Developer ID, that identity and a secure timestamp,
# which notarization refuses to do without), the entitlements on the main
# binary and none on the helpers
if [[ "$SIGN_MODE" != "adhoc" ]]; then
  codesign --verify --strict --deep "$BUNDLE"
  NMACHO=0
  for f in "$BUNDLE"/Contents/**/*(.); do
    [[ "$(file -b "$f")" == Mach-O* ]] || continue
    NMACHO=$((NMACHO + 1))
    CS="$(codesign -dvv "$f" 2>&1)"
    [[ "$CS" == *"flags="*"runtime)"* ]] || { echo "✗ ${f#$BUNDLE/} isn't under the hardened runtime" >&2; exit 1; }
    if [[ "$SIGN_MODE" == "developer-id" ]]; then
      [[ "$CS" == *"Authority=Developer ID Application: "* ]] || { echo "✗ ${f#$BUNDLE/} isn't signed with the Developer ID" >&2; exit 1; }
      [[ "$CS" == *"Timestamp="* ]] || { echo "✗ ${f#$BUNDLE/} has no secure timestamp" >&2; exit 1; }
    fi
  done
  (( NMACHO >= 3 )) || { echo "✗ expected the main binary and two helpers, found $NMACHO Mach-O files" >&2; exit 1; }
  codesign -d --entitlements - --xml "$BUNDLE" 2>/dev/null | grep -q "com.apple.security.device.audio-input" \
    || { echo "✗ the main binary lacks the audio-input entitlement" >&2; exit 1; }
  for h in concorde-dictate concorde-pdftext; do
    [[ -z "$(codesign -d --entitlements - --xml "$BUNDLE/Contents/Resources/$h" 2>/dev/null)" ]] \
      || { echo "✗ $h carries entitlements it doesn't need" >&2; exit 1; }
  done
  echo "  hardened runtime on all $NMACHO Mach-O files; the bundle's entitlement: audio-input"
fi
case "$SIGN_MODE" in
  adhoc) echo "  signed ad hoc" ;;
  rehearsal) echo "  signed ad hoc with the hardened runtime (rehearsal)" ;;
  developer-id) echo "  signed as $SIGN_NAME" ;;
esac

# read back what the updater and the Finder will see
GOT_BUILD="$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$PLIST")"
GOT_VER="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PLIST")"
GOT_ID="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$PLIST")"
GOT_EXE="$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$PLIST")"
[[ "$GOT_BUILD" == "$BUILD" ]] || { echo "CFBundleVersion is $GOT_BUILD, not $BUILD"; exit 1; }
[[ "$GOT_VER" == "$VER" ]] || { echo "CFBundleShortVersionString is $GOT_VER, not $VER"; exit 1; }
[[ "$GOT_ID" == "com.millen.millenai" ]] || { echo "bundle id is $GOT_ID"; exit 1; }
[[ "$GOT_EXE" == "ConcordeAI" ]] || { echo "executable is $GOT_EXE"; exit 1; }
# the words macOS shows when it asks for the microphone and for speech
# recognition: a request without its string is a crash, not a prompt
for k in NSMicrophoneUsageDescription NSSpeechRecognitionUsageDescription; do
  /usr/libexec/PlistBuddy -c "Print :$k" "$PLIST" >/dev/null 2>&1 || { echo "Info.plist has no $k"; exit 1; }
done
for f in millenai.py launch.sh crypto-requirements.txt engine-requirements.txt fonts vfx concorde-dictate concorde-pdftext; do
  [[ -e "$BUNDLE/Contents/Resources/$f" ]] || { echo "Resources/$f is missing from the bundle"; exit 1; }
done
# the bundled server is this source, build and all
cmp -s millenai.py "$BUNDLE/Contents/Resources/millenai.py" || { echo "Resources/millenai.py differs from the source"; exit 1; }
echo "  Info.plist: $GOT_ID $GOT_VER ($GOT_BUILD), executable $GOT_EXE"

# hand the bundle to the disk-image script in the place it expects
rm -rf ConcordeAI.app
ditto "$BUNDLE" ConcordeAI.app
# NOTARIZED TWICE, STAPLED TWICE (6b549): the app first, so its own ticket
# travels with it out of the image (dragged to Applications, it opens with
# no network to ask); then the image that carries it
if [[ -n "$NOTARY" ]]; then
  NZ="$(mktemp -d)"
  ditto -c -k --keepParent ConcordeAI.app "$NZ/ConcordeAI.zip"
  notarize "$NZ/ConcordeAI.zip" "the app"
  rm -rf "$NZ"
  xcrun stapler staple -q ConcordeAI.app
  xcrun stapler validate -q ConcordeAI.app || { echo "✗ the app's ticket didn't staple" >&2; exit 1; }
fi
DMG_APP_READY=1 ./build_dmg.sh
DMG="ConcordeAI-$VER.dmg"
[[ -f "$DMG" ]] || { echo "expected $DMG"; exit 1; }
if [[ "$SIGN_MODE" == "developer-id" ]]; then
  codesign --force --timestamp --sign "$SIGN_ID" "$DMG"
  codesign --verify --strict "$DMG"
elif [[ "$SIGN_MODE" == "rehearsal" ]]; then
  codesign --force --sign - "$DMG"
  codesign --verify --strict "$DMG"
fi
if [[ -n "$NOTARY" ]]; then
  notarize "$DMG" "the disk image"
  xcrun stapler staple -q "$DMG"
  xcrun stapler validate -q "$DMG" || { echo "✗ the disk image's ticket didn't staple" >&2; exit 1; }
  # Gatekeeper's own verdict, on the image and on the app inside it
  GK="$(spctl -a -t open --context context:primary-signature -v "$DMG" 2>&1)" || true
  [[ "$GK" == *": accepted"* && "$GK" == *"source=Notarized Developer ID"* ]] \
    || { echo "✗ Gatekeeper refuses the disk image:" >&2; echo "$GK" >&2; exit 1; }
  MP="$(mktemp -d)"
  hdiutil attach -nobrowse -readonly -noverify -quiet -mountpoint "$MP" "$DMG"
  GA="$(spctl -a -vv "$MP/ConcordeAI.app" 2>&1)" || true
  SA=0; xcrun stapler validate -q "$MP/ConcordeAI.app" && SA=1
  hdiutil detach -quiet "$MP"; rmdir "$MP" 2>/dev/null || true
  [[ "$GA" == *": accepted"* && "$GA" == *"source=Notarized Developer ID"* && "$SA" == 1 ]] \
    || { echo "✗ Gatekeeper refuses the app in the image (stapled: $SA):" >&2; echo "$GA" >&2; exit 1; }
  echo "  Gatekeeper accepts the disk image and the app in it: notarized, stapled"
fi
echo "  signing: $SIGN_MODE${NOTARY:+, notarized and stapled}"
