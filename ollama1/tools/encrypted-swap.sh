#!/usr/bin/env bash
# encrypted-swap.sh: an opt-in, undoable encrypted swap for ollama1.
# setup.sh runs it for --encrypted-swap SIZE and --remove-encrypted-swap.
#
#   sudo bash encrypted-swap.sh on 32G     make it (replaces the plain /swap.img)
#   sudo bash encrypted-swap.sh off        undo it (the plain /swap.img comes back)
#   bash encrypted-swap.sh status
#
# What "on" does:
#   - a swap file, /swap-ollama1.img, on the root filesystem (the OS NVMe);
#   - an /etc/crypttab entry that opens it at every boot as plain dm-crypt
#     with a new random key from /dev/urandom (aes-xts-plain64, 256-bit),
#     and formats it as swap. The key lives only in kernel memory, so after
#     a reboot or power-off nothing written to that swap can be read again.
#   - /dev/mapper/ollama1swap in /etc/fstab as swap;
#   - the plain, unencrypted /swap.img switched off and its fstab line
#     commented out (the file stays, for "off").
# It does NOT let Ollama swap: ollama.service keeps MemorySwapMax=0. Only
# tools/ram-model-test.sh's "swap" configuration allows it, for the length
# of that test, so Patrick can see the numbers first.
set -euo pipefail
NAME=ollama1swap
FILE=/swap-ollama1.img
PLAIN=/swap.img
CRYPTTAB=/etc/crypttab
FSTAB=/etc/fstab
MAPPER=/dev/mapper/$NAME
TAG="# ollama1 encrypted swap"

die() { echo "STOPPED: $*"; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run it with sudo"; }

status() {
  if grep -q "^$NAME " "$CRYPTTAB" 2>/dev/null; then
    echo "encrypted swap: configured ($FILE, $(du -h --apparent-size "$FILE" 2>/dev/null | cut -f1))"
  else
    echo "encrypted swap: not configured"
  fi
  if grep -q "^$MAPPER " /proc/swaps; then echo "  in use now: $MAPPER"; else echo "  not in use now"; fi
  if grep -q "^$PLAIN " /proc/swaps; then echo "plain $PLAIN: in use"; else echo "plain $PLAIN: off"; fi
}

on() {
  need_root
  local size=${1:-}
  [[ "$size" =~ ^[0-9]+G$ ]] || die "give a size in GiB, like 32G"
  local gib=${size%G}
  [ "$gib" -ge 4 ] && [ "$gib" -le 256 ] || die "size must be between 4G and 256G"
  local free_gib
  free_gib=$(df -BG --output=avail / | tail -n1 | tr -dc '0-9')
  [ "$free_gib" -gt $((gib + 20)) ] || die "the root filesystem has ${free_gib}G free; $size plus 20G is needed"
  if grep -q "^$NAME " "$CRYPTTAB" 2>/dev/null; then
    echo "already configured:"; status; return 0
  fi
  echo "Making $FILE ($size), encrypted with a new random key at every boot."
  if [ ! -f "$FILE" ]; then
    fallocate -l "$size" "$FILE"
  fi
  chmod 600 "$FILE"
  touch "$CRYPTTAB"
  cp -a "$CRYPTTAB" "$CRYPTTAB.before-ollama1-swap"
  cp -a "$FSTAB" "$FSTAB.before-ollama1-swap"
  printf '%s\n%s %s /dev/urandom swap,cipher=aes-xts-plain64,size=256\n' "$TAG" "$NAME" "$FILE" >>"$CRYPTTAB"
  printf '%s\n%s none swap sw,nofail,pri=10 0 0\n' "$TAG" "$MAPPER" >>"$FSTAB"
  # the plain swap file: off and out of fstab (kept on disk for "off")
  if grep -qE "^${PLAIN}[[:space:]]" "$FSTAB"; then
    sed -i -E "s|^(${PLAIN}[[:space:]].*)$|# ollama1: plain swap off while the encrypted swap is on\n# \1|" "$FSTAB"
  fi
  swapoff "$PLAIN" 2>/dev/null || true
  systemctl daemon-reload
  systemctl start "systemd-cryptsetup@$NAME.service"
  for _ in $(seq 1 20); do [ -b "$MAPPER" ] && break; sleep 0.5; done
  [ -b "$MAPPER" ] || die "$MAPPER did not appear (journalctl -u systemd-cryptsetup@$NAME)"
  swapon "$MAPPER" 2>/dev/null || swapon -a
  grep -q "^$MAPPER " /proc/swaps || die "the encrypted swap is set up but not in use; see swapon --show"
  echo "Done. Undo with: sudo bash $0 off"
  status
}

off() {
  need_root
  swapoff "$MAPPER" 2>/dev/null || true
  systemctl stop "systemd-cryptsetup@$NAME.service" 2>/dev/null || true
  if [ -f "$CRYPTTAB" ]; then
    awk -v n="$NAME" -v tag="$TAG" '$0 != tag && $1 != n' "$CRYPTTAB" >"$CRYPTTAB.new" && mv "$CRYPTTAB.new" "$CRYPTTAB"
  fi
  awk -v m="$MAPPER" -v tag="$TAG" '$0 != tag && $1 != m' "$FSTAB" >"$FSTAB.new" && mv "$FSTAB.new" "$FSTAB"
  # the plain swap file back in fstab and in use
  sed -i -E "/^# ollama1: plain swap off while the encrypted swap is on$/d; s|^# (${PLAIN}[[:space:]].*)$|\1|" "$FSTAB"
  systemctl daemon-reload
  if grep -qE "^${PLAIN}[[:space:]]" "$FSTAB" && [ -f "$PLAIN" ]; then swapon "$PLAIN" 2>/dev/null || true; fi
  rm -f "$FILE"
  echo "Encrypted swap removed."
  status
}

case "${1:-status}" in
  on) on "${2:-}" ;;
  off) off ;;
  status) status ;;
  *) echo "usage: $0 on SIZE | off | status"; exit 2 ;;
esac
