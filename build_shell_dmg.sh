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
#     crypto-requirements.txt (the sidecar's venv bootstrap, 6b537) and
#     concorde-dictate, the on-device speech helper (6b541, built here by
#     shell/dictate/build.sh and signed on its own);
#   * the bundle verifies (ad hoc until a Developer ID is in the keychain:
#     CONCORDE_SIGN_IDENTITY names one).
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:/opt/homebrew/opt/rustup/bin:$PATH"
command -v cargo >/dev/null || { echo "cargo not found (rustup: brew install rustup)"; exit 1; }
cargo tauri --version >/dev/null 2>&1 || { echo "tauri-cli not found: cargo install tauri-cli --version '^2' --locked"; exit 1; }

read -r VER BUILD <<<"$(python3 packaging/shell_stamp.py)"
[[ -n "$VER" && -n "$BUILD" ]] || { echo "could not stamp the shell from millenai.py"; exit 1; }
echo "→ shell $VER (build $BUILD)"

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
# the on-device speech helper (6b541) goes in beside millenai.py
./shell/dictate/build.sh
[[ -x shell/dictate/concorde-dictate ]] || { echo "shell/dictate/concorde-dictate wasn't built"; exit 1; }
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
# invalidates the seal. Ad hoc unless an identity is named.
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$PLIST"
IDENTITY="${CONCORDE_SIGN_IDENTITY:--}"
# the speech helper is a nested executable: copied in and signed on its own
# before the bundle's seal is written over it
ditto shell/dictate/concorde-dictate "$BUNDLE/Contents/Resources/concorde-dictate"
codesign --force --sign "$IDENTITY" "$BUNDLE/Contents/Resources/concorde-dictate"
codesign --force --sign "$IDENTITY" "$BUNDLE"
codesign --verify --strict "$BUNDLE"
[[ "$IDENTITY" == "-" ]] && echo "  ad-hoc signed (no Developer ID; set CONCORDE_SIGN_IDENTITY to sign for Gatekeeper)" \
                          || echo "  signed as $IDENTITY"

# read back what the updater and the Finder will see
GOT_BUILD="$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$PLIST")"
GOT_VER="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PLIST")"
GOT_ID="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$PLIST")"
GOT_EXE="$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$PLIST")"
[[ "$GOT_BUILD" == "$BUILD" ]] || { echo "CFBundleVersion is $GOT_BUILD, not $BUILD"; exit 1; }
[[ "$GOT_VER" == "$VER" ]] || { echo "CFBundleShortVersionString is $GOT_VER, not $VER"; exit 1; }
[[ "$GOT_ID" == "com.millen.millenai" ]] || { echo "bundle id is $GOT_ID"; exit 1; }
[[ "$GOT_EXE" == "ConcordeAI" ]] || { echo "executable is $GOT_EXE"; exit 1; }
for f in millenai.py launch.sh crypto-requirements.txt fonts vfx concorde-dictate; do
  [[ -e "$BUNDLE/Contents/Resources/$f" ]] || { echo "Resources/$f is missing from the bundle"; exit 1; }
done
# the bundled server is this source, build and all
cmp -s millenai.py "$BUNDLE/Contents/Resources/millenai.py" || { echo "Resources/millenai.py differs from the source"; exit 1; }
echo "  Info.plist: $GOT_ID $GOT_VER ($GOT_BUILD), executable $GOT_EXE"

# hand the bundle to the disk-image script in the place it expects
rm -rf ConcordeAI.app
ditto "$BUNDLE" ConcordeAI.app
DMG_APP_READY=1 ./build_dmg.sh
[[ -f "ConcordeAI-$VER.dmg" ]] || { echo "expected ConcordeAI-$VER.dmg"; exit 1; }
