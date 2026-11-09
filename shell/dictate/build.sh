#!/bin/zsh
# Builds concorde-dictate, the on-device speech helper (6b542): one Swift
# file over Apple's SpeechAnalyzer, macOS 26 and later. Universal when both
# targets link (an Intel Mac on macOS 26 dictates too); the machine's own
# arch otherwise, which is fine for a dev copy, never for a release.
# The binary is a build output (.gitignore): build_shell_dmg.sh runs this and
# copies it into the bundle's Resources, where millenai.py looks first.
set -e
cd "$(dirname "$0")"
command -v swiftc >/dev/null || { echo "swiftc not found (xcode-select --install)"; exit 1; }
OUT="concorde-dictate"
# the usage descriptions ride in the binary's own __info_plist section
LINK=(-Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist)
rm -f "$OUT" "$OUT.arm64" "$OUT.x86_64"
if [[ "${CONCORDE_DICTATE_ARCH:-}" == "native" ]]; then
  swiftc -O -o "$OUT" dictate.swift "${LINK[@]}"
else
  swiftc -O -target arm64-apple-macos26 -o "$OUT.arm64" dictate.swift "${LINK[@]}"
  if swiftc -O -target x86_64-apple-macos26 -o "$OUT.x86_64" dictate.swift "${LINK[@]}" 2>/dev/null; then
    lipo -create -output "$OUT" "$OUT.arm64" "$OUT.x86_64"; rm -f "$OUT.arm64" "$OUT.x86_64"
  else
    echo "  ! x86_64 didn't link: $(uname -m) only"; mv "$OUT.arm64" "$OUT"
  fi
fi
chmod +x "$OUT"
echo "  $OUT: $(lipo -archs "$OUT") $(stat -f%z "$OUT") bytes"
