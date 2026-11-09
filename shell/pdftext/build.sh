#!/bin/zsh
# Builds concorde-pdftext, the on-device PDF text reader (6b546): one Swift
# file over Apple's PDFKit, macOS 11 and later (the shell's own minimum).
# Universal when both targets link; the machine's own arch otherwise, which
# is fine for a dev copy, never for a release.
# The binary is a build output (.gitignore): build_shell_dmg.sh runs this and
# copies it into the bundle's Resources, where millenai.py looks first.
set -e
cd "$(dirname "$0")"
command -v swiftc >/dev/null || { echo "swiftc not found (xcode-select --install)"; exit 1; }
OUT="concorde-pdftext"
rm -f "$OUT" "$OUT.arm64" "$OUT.x86_64"
if [[ "${CONCORDE_PDFTEXT_ARCH:-}" == "native" ]]; then
  swiftc -O -o "$OUT" pdftext.swift
else
  swiftc -O -target arm64-apple-macos11 -o "$OUT.arm64" pdftext.swift
  if swiftc -O -target x86_64-apple-macos11 -o "$OUT.x86_64" pdftext.swift 2>/dev/null; then
    lipo -create -output "$OUT" "$OUT.arm64" "$OUT.x86_64"; rm -f "$OUT.arm64" "$OUT.x86_64"
  else
    echo "  ! x86_64 didn't link: $(uname -m) only"; mv "$OUT.arm64" "$OUT"
  fi
fi
chmod +x "$OUT"
echo "  $OUT: $(lipo -archs "$OUT") $(stat -f%z "$OUT") bytes"
