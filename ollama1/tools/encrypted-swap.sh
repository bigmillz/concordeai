#!/usr/bin/env bash
# encrypted-swap.sh: an opt-in, undoable encrypted swap for ollama1.
# setup.sh runs it for --encrypted-swap SIZE and --remove-encrypted-swap.
#
#   sudo bash encrypted-swap.sh on 32G     make it (replaces the plain /swap.img)
#   sudo bash encrypted-swap.sh off        undo it (the plain /swap.img comes back)
#   bash encrypted-swap.sh status
#
# What "on" does, each step checked first, so running it again finishes a
# run that stopped half way:
#   - an LVM volume, ubuntu-vg/ollama1swap, of that size. A logical volume,
#     not a file: swap on a file through dm-crypt goes through a loop
#     device, which can stall under memory pressure. It needs that much
#     free space in ubuntu-vg, and stops if there isn't;
#   - an /etc/crypttab entry that opens it at every boot as plain dm-crypt
#     with a new random key from /dev/urandom (aes-xts-plain64, a 512-bit
#     key: AES-256 in XTS), formats it as swap, and never holds up the boot
#     (nofail). The key lives only in kernel memory, so after a reboot or
#     power-off nothing written to that swap can be read again;
#   - /dev/mapper/ollama1swap in /etc/fstab as swap;
#   - the plain, unencrypted /swap.img switched off and its fstab line
#     commented out (recorded, so "off" puts back only what "on" changed).
#     The file stays, and still holds whatever was swapped to it before:
#     "on" offers to overwrite it with zeros.
# It does NOT let Ollama swap: ollama.service keeps MemorySwapMax=0. Only
# tools/ram-model-test.sh's "swap" configuration allows it, for the length
# of that test, so Patrick can see the numbers first.
set -euo pipefail
R=${O1_SWAP_TESTROOT:-}          # tests only: all files under this folder, fake tools on PATH
NAME=ollama1swap
VG=ubuntu-vg
LV=ollama1swap
LVDEV=/dev/$VG/$LV
PLAIN=/swap.img
CRYPTTAB=$R/etc/crypttab
FSTAB=$R/etc/fstab
STATE=$R/var/lib/ollama1/encrypted-swap.state
SWAPS=${R:+$R/proc-swaps}
SWAPS=${SWAPS:-/proc/swaps}
MAPPER=/dev/mapper/$NAME
TAG="# ollama1 encrypted swap"
PLAIN_TAG="# ollama1: plain swap off while the encrypted swap is on"

die() { echo "STOPPED: $*"; exit 1; }
need_root() { [ -n "$R" ] || [ "$(id -u)" -eq 0 ] || die "run it with sudo"; }
active() { grep -q "^$1 " "$SWAPS" 2>/dev/null; }
lv_exists() { lvs --noheadings -o lv_name "$VG/$LV" >/dev/null 2>&1; }
mapper_ready() { if [ -n "$R" ]; then [ -e "$R$MAPPER" ]; else [ -b "$MAPPER" ]; fi; }
state_has() { grep -qx "$1" "$STATE" 2>/dev/null; }
state_add() { mkdir -p "$(dirname "$STATE")"; state_has "$1" || echo "$1" >>"$STATE"; }

status() {
  if lv_exists; then echo "volume: $LVDEV ($(lvs --noheadings --units g -o lv_size "$VG/$LV" 2>/dev/null | tr -d ' '))"
  else echo "volume: none"; fi
  if grep -q "^$NAME " "$CRYPTTAB" 2>/dev/null; then echo "crypttab: configured"; else echo "crypttab: not configured"; fi
  if active "$MAPPER"; then echo "  in use now: $MAPPER"; else echo "  not in use now"; fi
  if active "$PLAIN"; then echo "plain $PLAIN: in use"; else echo "plain $PLAIN: off"; fi
}

wipe_plain() {
  # the old plain swap file may still hold pages of anything that was swapped
  local f=$R$PLAIN
  [ -f "$f" ] || return 0
  [ -t 0 ] || { echo "  $PLAIN still holds whatever was swapped to it before; run this in a terminal to wipe it"; return 0; }
  local mib
  mib=$(( $(stat -c %s "$f") / 1048576 ))
  printf '  %s still holds whatever was swapped to it before this. Overwrite it with zeros now (%s MiB)?\n  Type wipe to do it, anything else skips it: ' "$PLAIN" "$mib"
  local a=""; read -r a || true
  [ "$a" = wipe ] || { echo "  kept as it is"; return 0; }
  active "$PLAIN" && die "$PLAIN is still in use; not wiping it"
  dd if=/dev/zero of="$f" bs=1M count="$mib" conv=notrunc,fsync status=none
  mkswap "$f" >/dev/null   # still a valid swap file for "off"
  echo "  $PLAIN overwritten with zeros"
}

on() {
  need_root
  local size=${1:-}
  [[ "$size" =~ ^[0-9]+G$ ]] || die "give a size in GiB, like 32G"
  local gib=${size%G}
  [ "$gib" -ge 4 ] && [ "$gib" -le 256 ] || die "size must be between 4G and 256G"
  local again="Run it again to finish, or 'off' to undo what was done."

  # 1. the volume
  if lv_exists; then
    echo "volume $LVDEV: already there"
  else
    local free
    free=$(vgs --noheadings --units b --nosuffix -o vg_free "$VG" 2>/dev/null | tr -d '[:space:]')
    case "$free" in ''|*[!0-9]*) die "couldn't read the free space in $VG (vgs -o vg_free $VG)" ;; esac
    local need=$((gib * 1073741824))
    [ "$free" -ge "$need" ] || die "$VG has $((free / 1073741824)) GiB free and $size is needed. (setup gives the free space in $VG to /, and / can't shrink while it's in use.) Nothing was changed."
    lvcreate --yes --wipesignatures y -L "$size" -n "$LV" "$VG" >/dev/null || die "lvcreate failed. $again"
    state_add lv_created
    echo "volume $LVDEV: made ($size)"
  fi

  # 2. crypttab and fstab
  touch "$CRYPTTAB"
  if ! grep -q "^$NAME " "$CRYPTTAB"; then
    [ -f "$CRYPTTAB.before-ollama1-swap" ] || cp -a "$CRYPTTAB" "$CRYPTTAB.before-ollama1-swap"
    printf '%s\n%s %s /dev/urandom swap,cipher=aes-xts-plain64,size=512,nofail\n' "$TAG" "$NAME" "$LVDEV" >>"$CRYPTTAB"
  fi
  if ! grep -q "^$MAPPER " "$FSTAB"; then
    [ -f "$FSTAB.before-ollama1-swap" ] || cp -a "$FSTAB" "$FSTAB.before-ollama1-swap"
    printf '%s\n%s none swap sw,nofail,pri=10 0 0\n' "$TAG" "$MAPPER" >>"$FSTAB"
  fi

  # 3. the plain swap file: off, and out of fstab (recorded, kept on disk for "off")
  if grep -qE "^${PLAIN}[[:space:]]" "$FSTAB"; then
    awk -v p="$PLAIN" -v tag="$PLAIN_TAG" '$1 == p { print tag; print "# " $0; next } { print }' \
      "$FSTAB" >"$FSTAB.new" && mv "$FSTAB.new" "$FSTAB"
    state_add plain_commented
  fi
  if active "$PLAIN"; then
    swapoff "$PLAIN" || die "swapoff $PLAIN failed (not enough free memory to take its pages back?). $again"
  fi

  # 4. open it and use it
  systemctl daemon-reload
  if ! mapper_ready; then
    systemctl start "systemd-cryptsetup@$NAME.service" || die "systemd-cryptsetup@$NAME didn't start (journalctl -u systemd-cryptsetup@$NAME). $again"
    for _ in $(seq 1 20); do mapper_ready && break; sleep 0.5; done
  fi
  mapper_ready || die "$MAPPER did not appear (journalctl -u systemd-cryptsetup@$NAME). $again"
  if ! active "$MAPPER"; then
    swapon "$MAPPER" || die "swapon $MAPPER failed (swapon --show). $again"
  fi
  active "$MAPPER" || die "the encrypted swap is set up but not in use (swapon --show). $again"
  echo "Encrypted swap on. Undo with: sudo ./setup.sh --remove-encrypted-swap"
  wipe_plain
  status
}

off() {
  need_root
  # taking the swap back in must work before anything else changes
  if active "$MAPPER"; then
    swapoff "$MAPPER" || die "swapoff $MAPPER failed (not enough free memory to take its pages back?). Nothing was changed; free some memory and try again."
  fi
  if mapper_ready; then
    systemctl stop "systemd-cryptsetup@$NAME.service" || die "couldn't close $MAPPER (systemctl stop systemd-cryptsetup@$NAME). The swap is off; nothing else was changed."
  fi
  if [ -f "$CRYPTTAB" ]; then
    awk -v n="$NAME" -v tag="$TAG" '$0 != tag && $1 != n' "$CRYPTTAB" >"$CRYPTTAB.new" && mv "$CRYPTTAB.new" "$CRYPTTAB"
  fi
  awk -v m="$MAPPER" -v tag="$TAG" '$0 != tag && $1 != m' "$FSTAB" >"$FSTAB.new" && mv "$FSTAB.new" "$FSTAB"
  # the plain swap file back in fstab, only if "on" took it out
  local restored=0
  if state_has plain_commented; then
    # uncomment only the line right after our own marker
    awk -v tag="$PLAIN_TAG" '$0 == tag { un = 1; next } un { un = 0; if (substr($0, 1, 2) == "# ") { print substr($0, 3); next } } { print }' \
      "$FSTAB" >"$FSTAB.new" && mv "$FSTAB.new" "$FSTAB"
    restored=1
  fi
  systemctl daemon-reload
  if lv_exists; then
    lvremove --yes "$VG/$LV" >/dev/null || die "lvremove $VG/$LV failed; crypttab and fstab are already back"
  fi
  if [ "$restored" = 1 ] && grep -qE "^${PLAIN}[[:space:]]" "$FSTAB" && [ -f "$R$PLAIN" ]; then
    swapon "$PLAIN" || echo "  note: swapon $PLAIN failed; it's in fstab and will be used after a reboot"
  fi
  rm -f "$STATE"
  echo "Encrypted swap removed."
  status
}

case "${1:-status}" in
  on) on "${2:-}" ;;
  off) off ;;
  status) status ;;
  *) echo "usage: $0 on SIZE | off | status"; exit 2 ;;
esac
