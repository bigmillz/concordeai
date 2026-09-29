#!/usr/bin/env bash
# ollama1 host kit: turns this desktop into a private, locked-down model
# server for ConcordeAI. See README.md.
#
#   sudo ./setup.sh                  everything, in order (safe to run again)
#   sudo ./setup.sh --skip-cloudflare  everything except the tunnel and Access
#   sudo ./setup.sh --remove-setup-key also take claude-setup@concordeai out of
#                                    ~pmiller/.ssh/authorized_keys (your own key stays)
#   ./setup.sh --plan                show what it would do; changes nothing
#
# Every step checks what is already done and skips it, so after a stop (a
# reboot it asks for, a failed download) just run it again. Everything it
# prints also goes to /var/log/ollama1-setup.log (root-only).
#
# Nothing personal is stored in this script or the repo: the admin email,
# Cloudflare IDs and the tunnel credential are asked for or created here, on
# the desktop, and kept root-only.
# The single-quoted $... below are meant literally (checks run later, GRUB
# text matched as written):
# shellcheck disable=SC2016
set -euo pipefail
umask 022

# ---- the machine this was written for ------------------------------------
OS_SERIAL=S6B0NU0W805372Z        # nvme, the OS (LVM ubuntu-vg/ubuntu-lv)
MODELS_SERIAL=S6B0NG0R906564E    # nvme, 2 TB: wiped, becomes /srv/models
HDD1_SERIAL=ZR127RMQ             # 8 TB: holds /home today; wiped into the mirror
HDD2_SERIAL=ZR11ZRGJ             # 8 TB: wiped into the mirror
ADMIN_USER=pmiller
HOME_LAN=192.168.86.0/24
NEW_HOSTNAME=ollama1
OLD_HOSTNAME=concordeai
TIMEZONE=America/New_York
CLAUDE_KEY=claude-setup@concordeai
CF_KEY_URL=https://pkg.cloudflare.com/cloudflare-main.gpg
CF_KEY_FPR=CC94B39C77AE7342A68B89628A682D308D4E5E73   # "CloudFlare Software Packaging 2025"
GW_HOST=ollama1.flyconcordefly.com
ADMIN_HOST=ollama1-admin.flyconcordefly.com
LIBDIR=/usr/local/lib/ollama1
LOG=/var/log/ollama1-setup.log

PLAN_ONLY=0
SKIP_CF=0
REMOVE_SETUP_KEY=0
for a in "$@"; do
  case "$a" in
    --plan) PLAN_ONLY=1 ;;
    --skip-cloudflare) SKIP_CF=1 ;;
    --remove-setup-key) REMOVE_SETUP_KEY=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- output ---------------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B=; G=; Y=; R=; N=; fi
STEP=0
step() { STEP=$((STEP + 1)); printf '\n%s== %d. %s%s   (%s)\n' "$B" "$STEP" "$1" "$N" "$(date '+%H:%M:%S')"; }
ok()   { printf '   %sok%s  %s\n' "$G" "$N" "$*"; }
note() { printf '   %s--%s  %s\n' "$Y" "$N" "$*"; }
run()  { printf '   $ %s\n' "$*"; "$@"; }
die()  { printf '\n%sSTOPPED:%s %s\n' "$R" "$N" "$*"; printf 'Nothing after this point was changed. Fix it and run setup.sh again.\n'; exit 1; }
LATER=()
later() { LATER+=("$*"); }

ask_yes() { # prompt -> true only if the answer is exactly "yes"
  local ans
  printf '%s' "$1"
  read -r ans </dev/tty || return 1
  [ "$ans" = "yes" ]
}

# ---- disks ----------------------------------------------------------------
disk_by_serial() { # serial -> /dev/<disk> (whole disk), empty if absent
  local d n ser
  for d in /sys/block/*; do
    n=${d##*/}
    case $n in loop*|ram*|dm-*|md*|sr*|zram*) continue ;; esac
    ser=$(lsblk -dno SERIAL "/dev/$n" 2>/dev/null | tr -d '[:space:]')
    if [ "$ser" = "$1" ]; then echo "/dev/$n"; return 0; fi
  done
  return 0
}
disk_desc() { lsblk -dno MODEL,SIZE "$1" 2>/dev/null | sed 's/  */ /g;s/^ //'; }
disk_of() { # a block device -> its whole disk /dev/<name>
  local src=$1 pk
  pk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1 | tr -d '[:space:]')
  if [ -n "$pk" ]; then echo "/dev/$pk"; else echo "$src"; fi
}
first_part() { lsblk -lnpo NAME,TYPE "$1" | awk '$2=="part"{print $1; exit}'; }
dev_busy() { # true if the kernel holds the device (mounted, even lazily; md member; LVM)
  python3 -c 'import os,sys; os.close(os.open(sys.argv[1], os.O_RDONLY | os.O_EXCL))' "$1" 2>/dev/null && return 1
  return 0
}
root_disk() {
  local src pv
  src=$(findmnt -no SOURCE /)
  pv=$(pvs --noheadings -o pv_name 2>/dev/null | tr -d ' ' | head -n1 || true)
  if [ -n "$pv" ] && lsblk -no NAME "$src" >/dev/null 2>&1 && [[ "$src" == /dev/mapper/* || "$src" == /dev/dm-* ]]; then
    disk_of "$pv"
  else
    disk_of "$src"
  fi
}

OS_DISK=$(disk_by_serial "$OS_SERIAL")
MODELS_DISK=$(disk_by_serial "$MODELS_SERIAL")
HDD1=$(disk_by_serial "$HDD1_SERIAL")
HDD2=$(disk_by_serial "$HDD2_SERIAL")

home_on_own_disk() { findmnt -n --target /home -o TARGET 2>/dev/null | grep -qx /home; }
models_done() { mountpoint -q /srv/models && [ "$(disk_of "$(findmnt -no SOURCE /srv/models)")" = "$MODELS_DISK" ]; }
raid_array() { mdadm --detail --scan 2>/dev/null | awk '/name=ollama1:data/{print $2; exit}'; }
raid_done() { mountpoint -q /srv/data && [ -n "$(raid_array)" ]; }
vg_free() { vgs --noheadings --units g -o vg_free ubuntu-vg 2>/dev/null | tr -d ' g' | cut -d. -f1; }

# ---- the plan --------------------------------------------------------------
show_disks() {
  printf '\n   %-13s %-17s %-32s %s\n' "Device" "Serial" "Model / size" "Role"
  printf '   %-13s %-17s %-32s %s\n' "${OS_DISK:-MISSING}" "$OS_SERIAL" "$(disk_desc "${OS_DISK:-/dev/null}")" "OS: kept; root LV grows into free space"
  printf '   %-13s %-17s %-32s %s\n' "${MODELS_DISK:-MISSING}" "$MODELS_SERIAL" "$(disk_desc "${MODELS_DISK:-/dev/null}")" "$(models_done && echo 'models: already set up' || echo 'WIPED -> ext4 /srv/models')"
  printf '   %-13s %-17s %-32s %s\n' "${HDD1:-MISSING}" "$HDD1_SERIAL" "$(disk_desc "${HDD1:-/dev/null}")" "$(raid_done && echo 'mirror: already set up' || echo 'WIPED -> RAID1 /srv/data (after /home moves off it)')"
  printf '   %-13s %-17s %-32s %s\n' "${HDD2:-MISSING}" "$HDD2_SERIAL" "$(disk_desc "${HDD2:-/dev/null}")" "$(raid_done && echo 'mirror: already set up' || echo 'WIPED -> RAID1 /srv/data')"
}

state() { if eval "$1" >/dev/null 2>&1; then printf '%sdone%s ' "$G" "$N"; else printf 'to do'; fi; }

print_plan() {
  printf '%sollama1 setup: the plan%s\n' "$B" "$N"
  show_disks
  cat <<EOF

   $(state '[ "$(hostname)" = "$NEW_HOSTNAME" ]')  1. Host name $NEW_HOSTNAME, time zone $TIMEZONE, boot menu shown for 5 s
   $(state 'command -v cloudflared && command -v ttyd && python3 -c "import nacl"')  2. Packages: ttyd, python3-nacl, mdadm, nftables, zstd, cloudflared (Cloudflare's apt repo, key checked)
   $(state '[ "${VGFREE:-1}" = 0 ]')  3. Grow the root volume into the free space on the OS disk (online)
   $(state '! home_on_own_disk')  4. Copy /home onto the root filesystem, check it (SSH keys included), stop mounting the old disk
   $(state models_done)  5. Models disk: wipe, ext4, mount at /srv/models (noatime)
   $(state raid_done)  6. Mirror: wipe both 8 TB disks, RAID1, ext4, mount at /srv/data (resync runs in the background)
   $(state 'id o1gw && id o1admin && id o1dash && id ollama')  7. Service users (ollama, o1gw, o1admin, o1dash, cloudflared), no shells
   $(state '[ -x $LIBDIR/bin/ollama1-gateway ]')  8. Install the gateway, admin panel, dashboard, pairing tool, updater, units, polkit rule
   $(state '[ -L /opt/ollama/current ]')  9. Ollama (ROCm build) from GitHub, checksums verified; GPU only; local only
   $(state 'ufw status | grep -q "Status: active"') 10. Firewall: nothing in except SSH from $HOME_LAN; bridged LAN traffic (the Pi) untouched
   $(state '[ -f /etc/ssh/sshd_config.d/10-ollama1.conf ]') 11. SSH: passwords off, only $ADMIN_USER with a key, no root (only if a key of your own is there)
   $(state '[ -f /etc/apt/apt.conf.d/52ollama1-unattended-upgrades ]') 12. Automatic security updates (+ cloudflared), reboot at 04:00 when needed; weekly Ollama update
   $(state 'systemctl is-active ollama1-dash') 13. Services: gateway, admin panel, web terminal, dashboard on the screen, timers
   $(state 'systemctl is-active ollama1-tunnel') 14. Cloudflare Tunnel ($GW_HOST, $ADMIN_HOST) and Access
         15. Only if you say so: remove the setup key $CLAUDE_KEY from authorized_keys

   Not touched: the network settings (netplan, br0), anything in /home besides the move.
   No models are installed.
EOF
}

# ---- preflight --------------------------------------------------------------
VGFREE=$(vg_free || echo "?")
if [ "$PLAN_ONLY" = 1 ]; then
  print_plan
  exit 0
fi
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo:  sudo $0"; exit 1; }

# Run from the root filesystem, not /home: /home is about to move.
if [ "${OLLAMA1_RELOCATED:-}" != 1 ]; then
  case "$KIT" in
    /home/*|/root/*)
      dest=/var/tmp/ollama1-kit
      rm -rf "$dest"; mkdir -p "$dest"; cp -a "$KIT/." "$dest/"
      cd /
      OLLAMA1_RELOCATED=1 exec bash "$dest/setup.sh" "$@" ;;
  esac
fi
cd /

touch "$LOG"; chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
printf '\n===== ollama1 setup %s =====\n' "$(date -Is)"

# shellcheck source=/dev/null
. /etc/os-release
[ "${VERSION_ID:-}" = "26.04" ] || die "this kit is for Ubuntu 26.04; this is ${PRETTY_NAME:-unknown}"
for f in lib/o1common.py bin/ollama1-gateway systemd/ollama.service config/50-ollama1.rules; do
  [ -f "$KIT/$f" ] || die "the kit is incomplete: $f is missing"
done
[ -n "$OS_DISK" ] || die "no disk with serial $OS_SERIAL (the OS disk)"
[ -n "$MODELS_DISK" ] || die "no disk with serial $MODELS_SERIAL (the models disk)"
[ -n "$HDD1" ] || die "no disk with serial $HDD1_SERIAL"
[ -n "$HDD2" ] || die "no disk with serial $HDD2_SERIAL"
[ "$(root_disk)" = "$OS_DISK" ] || die "/ is not on the disk with serial $OS_SERIAL; refusing to touch any disk"
for d in "$MODELS_DISK" "$HDD1" "$HDD2"; do
  [ "$d" != "$OS_DISK" ] || die "$d is the OS disk"
done
[ "$HDD1" != "$HDD2" ] && [ "$MODELS_DISK" != "$HDD1" ] && [ "$MODELS_DISK" != "$HDD2" ] || die "two roles map to one disk"
id "$ADMIN_USER" >/dev/null 2>&1 || die "no user $ADMIN_USER"

print_plan
WIPES=()
models_done || WIPES+=("$MODELS_DISK ($MODELS_SERIAL, $(disk_desc "$MODELS_DISK"))")
raid_done || WIPES+=("$HDD1 ($HDD1_SERIAL, $(disk_desc "$HDD1"))" "$HDD2 ($HDD2_SERIAL, $(disk_desc "$HDD2"))")
echo
if [ "${#WIPES[@]}" -gt 0 ]; then
  printf '%sThese disks will be ERASED. Everything on them is lost:%s\n' "$R$B" "$N"
  for w in "${WIPES[@]}"; do printf '   %s\n' "$w"; done
  printf '(/home is copied off %s first, and the copy is checked before that disk is touched.)\n' "$HDD1"
fi
ask_yes "Type yes to go ahead: " || { echo "Nothing changed."; exit 1; }

# ---- 1. identity ----------------------------------------------------------------
step "Host name, time zone, boot menu"
if [ "$(hostnamectl --static)" != "$NEW_HOSTNAME" ]; then
  run hostnamectl set-hostname "$NEW_HOSTNAME"
fi
if grep -qE "^127\.0\.1\.1[[:space:]]" /etc/hosts; then
  sed -i -E "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1 $NEW_HOSTNAME/" /etc/hosts
else
  printf '127.0.1.1 %s\n' "$NEW_HOSTNAME" >>/etc/hosts
fi
if [ -d /etc/cloud/cloud.cfg.d ]; then  # or cloud-init puts the old name back at boot
  printf '# ollama1: keep the host name set by setup.sh\npreserve_hostname: true\n' >/etc/cloud/cloud.cfg.d/99-ollama1.cfg
fi
ok "host name $(hostnamectl --static) (was $OLD_HOSTNAME)"
[ "$(timedatectl show -p Timezone --value)" = "$TIMEZONE" ] || run timedatectl set-timezone "$TIMEZONE"
ok "time zone $(timedatectl show -p Timezone --value)"

grub_ok() {
  local cfg=/boot/grub/grub.cfg
  [ -f "$cfg" ] || return 1
  # every timeout GRUB can use is 5 s: the normal path and both recordfail paths
  grep -E 'set timeout=[0-9]+' "$cfg" | grep -qvE 'set timeout=5([^0-9]|$)' && return 1
  grep -A1 -F 'if [ "${recordfail}" = 1 ] ; then' "$cfg" | grep -q 'set timeout=5' || return 1
  grep -A3 -F 'if [ x$feature_timeout_style = xy ] ; then' "$cfg" | grep -q 'set timeout=5' || return 1
  if grep -q 'recordfail_broken=1' "$cfg"; then
    grep -A1 -F 'if [ $grub_platform = efi ]; then' "$cfg" | grep -q 'set timeout=5' || return 1
  fi
  return 0
}
mkdir -p /etc/default/grub.d
if ! cmp -s "$KIT/config/99-ollama1.cfg" /etc/default/grub.d/99-ollama1.cfg || ! grub_ok; then
  install -m 0644 "$KIT/config/99-ollama1.cfg" /etc/default/grub.d/99-ollama1.cfg
  run update-grub
fi
grub_ok || die "/boot/grub/grub.cfg still has a boot menu timeout other than 5 s (grep 'set timeout' /boot/grub/grub.cfg)"
ok "boot menu: 5 s, also after a failed boot ($(grep -cE 'set timeout=5([^0-9]|$)' /boot/grub/grub.cfg) timeout lines, all 5)"

# ---- 2. packages ----------------------------------------------------------------
step "Packages"
export DEBIAN_FRONTEND=noninteractive
if [ ! -f /usr/share/keyrings/cloudflare-main.gpg ] || ! gpg --show-keys --with-colons /usr/share/keyrings/cloudflare-main.gpg 2>/dev/null | grep -q "^fpr:::::::::$CF_KEY_FPR:"; then
  tmpk=$(mktemp)
  run curl -fsSL --proto '=https' "$CF_KEY_URL" -o "$tmpk"
  fpr=$(gpg --show-keys --with-colons "$tmpk" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
  [ "$fpr" = "$CF_KEY_FPR" ] || { rm -f "$tmpk"; die "Cloudflare's apt key has fingerprint '$fpr', expected $CF_KEY_FPR. If Cloudflare rotated it, check https://pkg.cloudflare.com and update CF_KEY_FPR."; }
  install -m 0644 "$tmpk" /usr/share/keyrings/cloudflare-main.gpg
  rm -f "$tmpk"
fi
ok "Cloudflare apt key $CF_KEY_FPR"
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" >/etc/apt/sources.list.d/cloudflared.list
run apt-get update -q
run apt-get install -y -q ttyd python3-nacl mdadm rsync zstd nftables ufw gdisk parted curl gnupg unattended-upgrades cloudflared
# ttyd must never listen on its own; only ollama1-ttyd (127.0.0.1, login) may run.
systemctl disable --now ttyd.service >/dev/null 2>&1 || true
python3 -c 'import nacl.signing' || die "python3-nacl did not install"
ok "ttyd $(ttyd --version 2>&1 | awk '{print $NF}'), cloudflared $(cloudflared --version 2>&1 | awk '{print $3}'), PyNaCl"

# ---- 3. root volume -----------------------------------------------------------
step "Root volume"
VGFREE=$(vg_free)
if [ "${VGFREE:-0}" -gt 0 ]; then
  run lvextend -r -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
fi
ok "/ is $(df -h --output=size / | tail -n1 | tr -d ' ') ($(df -h --output=avail / | tail -n1 | tr -d ' ') free)"

# ---- 4. /home onto the root filesystem -------------------------------------------
step "/home onto the root filesystem"
KEYS=/home/$ADMIN_USER/.ssh/authorized_keys
if home_on_own_disk; then
  src=$(findmnt -no SOURCE /home)
  hdisk=$(disk_of "$src")
  [ "$hdisk" = "$HDD1" ] || [ "$hdisk" = "$HDD2" ] || die "/home is on $hdisk, not one of the 8 TB disks; not moving it"
  need=$(du -sxm /home | cut -f1)
  avail=$(df -m --output=avail / | tail -n1 | tr -d ' ')
  [ "$need" -lt $((avail - 1024)) ] || die "/home needs ${need} MB and / has ${avail} MB free"
  keysum=""
  [ -f "$KEYS" ] && keysum=$(sha256sum "$KEYS" | cut -d' ' -f1)
  ROOTVIEW=/run/ollama1-rootfs
  mkdir -p "$ROOTVIEW"
  mountpoint -q "$ROOTVIEW" || mount --bind / "$ROOTVIEW"   # / alone: the real /home folder under the mount
  mkdir -p "$ROOTVIEW/home"
  run rsync -aHAX --numeric-ids --delete --exclude=/lost+found /home/ "$ROOTVIEW/home/"
  diffs=$(rsync -aHAXn --numeric-ids --delete --checksum --itemize-changes --exclude=/lost+found /home/ "$ROOTVIEW/home/")
  [ -z "$diffs" ] || { umount "$ROOTVIEW"; die "the copy of /home differs from the original: $diffs"; }
  if [ -n "$keysum" ]; then
    [ "$(sha256sum "$ROOTVIEW$KEYS" | cut -d' ' -f1)" = "$keysum" ] || { umount "$ROOTVIEW"; die "authorized_keys did not copy intact"; }
    ok "copy checked, file by file; $KEYS intact ($(grep -cvE '^[[:space:]]*(#|$)' "$KEYS") key(s))"
  fi
  umount "$ROOTVIEW"; rmdir "$ROOTVIEW"
  cp -a /etc/fstab "/etc/fstab.before-ollama1-$(date +%Y%m%d-%H%M%S)"
  awk -v d="$(date +%F)" '
    /^[[:space:]]*#/ {print; next}
    $2 == "/home" {print "# ollama1 " d ": /home now lives on the root filesystem"; print "# " $0; next}
    {print}' /etc/fstab >/etc/fstab.ollama1.new
  mv /etc/fstab.ollama1.new /etc/fstab
  systemctl daemon-reload
  if umount /home 2>/dev/null; then
    ok "/home now on the root filesystem"
  else
    # A login (probably yours) still has a folder open on the old disk. Detach
    # it: new logins and every /home path now use the copy; the old disk is
    # released when those sessions end.
    run umount -l /home
    note "/home detached; the old disk is released when every earlier login has ended"
  fi
  [ -f "$KEYS" ] || [ -z "$keysum" ] || die "authorized_keys is missing after the move; the old disk is untouched, see /etc/fstab.before-ollama1-*"
else
  grep -qE '^[^#][^[:space:]]*[[:space:]]+/home[[:space:]]' /etc/fstab && die "/etc/fstab still mounts /home but it isn't mounted; look at it before continuing"
  ok "/home is on the root filesystem"
fi

# ---- 5. users (the disks need the ollama user) ------------------------------------------
step "Service users"
ensure_group() { getent group "$1" >/dev/null || run groupadd --system "$1"; }
ensure_user() { # name home
  if ! id "$1" >/dev/null 2>&1; then
    run useradd --system --user-group --home-dir "$2" --no-create-home --shell /usr/sbin/nologin "$1"
  fi
}
ensure_group o1view
ensure_group o1pair
ensure_user ollama /var/lib/ollama
ensure_user o1gw /nonexistent
ensure_user o1admin /nonexistent
ensure_user o1dash /nonexistent
ensure_user cloudflared /nonexistent
usermod -aG render,video ollama
usermod -aG o1pair,o1view o1gw
usermod -aG o1view o1admin
usermod -aG o1view,o1pair o1dash
usermod -aG o1view "$ADMIN_USER"
install -d -m 0750 -o ollama -g ollama /var/lib/ollama
ok "ollama, o1gw, o1admin, o1dash, cloudflared (all /usr/sbin/nologin)"

# ---- 6. models disk ---------------------------------------------------------------
step "Models disk ($MODELS_SERIAL)"
set_fstab() { # mountpoint line
  awk -v m="$1" '!($2 == m && $0 !~ /^[[:space:]]*#/)' /etc/fstab >/etc/fstab.ollama1.new
  printf '%s\n' "$2" >>/etc/fstab.ollama1.new
  mv /etc/fstab.ollama1.new /etc/fstab
  systemctl daemon-reload
}
if models_done; then
  ok "/srv/models already on $MODELS_DISK"
else
  [ "$(lsblk -dno SERIAL "$MODELS_DISK" | tr -d '[:space:]')" = "$MODELS_SERIAL" ] || die "serial check failed for $MODELS_DISK"
  part=$(first_part "$MODELS_DISK")
  if [ -n "$part" ] && [ "$(blkid -s LABEL -o value "$part" 2>/dev/null)" = "o1models" ]; then
    note "found an earlier o1models filesystem; keeping it"
  else
    for p in $(lsblk -lnpo NAME "$MODELS_DISK"); do
      if findmnt -S "$p" >/dev/null; then die "$p is mounted; not wiping"; fi
    done
    dev_busy "$MODELS_DISK" && die "$MODELS_DISK is in use; not wiping"
    run wipefs -a "$MODELS_DISK"
    run sgdisk --zap-all "$MODELS_DISK"
    run sgdisk -n1:1MiB:0 -t1:8300 -c1:ollama1-models "$MODELS_DISK"
    partprobe "$MODELS_DISK" || true; udevadm settle
    part=$(first_part "$MODELS_DISK")
    [ -n "$part" ] || die "no partition appeared on $MODELS_DISK"
    run mkfs.ext4 -F -q -L o1models -m 1 "$part"
  fi
  uuid=$(blkid -s UUID -o value "$part")
  mkdir -p /srv/models
  set_fstab /srv/models "UUID=$uuid /srv/models ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2"
  run mount /srv/models
  ok "/srv/models: $(df -h --output=size /srv/models | tail -n1 | tr -d ' ') on $part"
fi
chown ollama:ollama /srv/models; chmod 0750 /srv/models

# ---- 7. the mirror ------------------------------------------------------------------
step "Mirror ($HDD1_SERIAL + $HDD2_SERIAL)"
RAID_PENDING=0
if raid_done; then
  ok "/srv/data already on $(raid_array)"
elif home_on_own_disk; then
  die "/home is still mounted from an 8 TB disk; not wiping"
elif grep -qE '^[^#][^[:space:]]*[[:space:]]+/home[[:space:]]' /etc/fstab; then
  die "/etc/fstab still mounts /home from an 8 TB disk; not wiping"
elif dev_busy "$HDD1" || dev_busy "$HDD2"; then
  RAID_PENDING=1
  note "an 8 TB disk is still held by an earlier login that had /home open (the old /home)."
  note "Log out of every SSH session (or reboot), log in again and run setup.sh again: it continues here."
  later "Build the mirror: log out of every session (or reboot), then run sudo ./setup.sh again"
else
  md=$(raid_array)
  if [ -z "$md" ]; then
    for d in "$HDD1" "$HDD2"; do
      [ -n "$(lsblk -dno SERIAL "$d")" ] || die "serial check failed for $d"
      for p in $(lsblk -lnpo NAME "$d"); do findmnt -S "$p" >/dev/null && die "$p is mounted; not wiping"; done
      run wipefs -a "$d"
      run sgdisk --zap-all "$d"
      # leave 100 MiB free at the end, so a replacement disk a little smaller still fits
      run sgdisk -n1:1MiB:-100MiB -t1:FD00 -c1:ollama1-data "$d"
    done
    partprobe "$HDD1" "$HDD2" || true; udevadm settle
    p1=$(first_part "$HDD1"); p2=$(first_part "$HDD2")
    [ -n "$p1" ] && [ -n "$p2" ] || die "partitions did not appear on the 8 TB disks"
    run mdadm --create /dev/md/o1data --run --level=1 --raid-devices=2 --metadata=1.2 \
      --bitmap=internal --homehost="$NEW_HOSTNAME" --name=data "$p1" "$p2"
    udevadm settle
    md=/dev/md/o1data
    run mkfs.ext4 -F -q -L o1data -m 0 -E lazy_itable_init=1,lazy_journal_init=1 "$md"
  fi
  touch /etc/mdadm/mdadm.conf
  sed -i '/name=ollama1:data/d' /etc/mdadm/mdadm.conf
  mdadm --detail --brief "$md" >>/etc/mdadm/mdadm.conf
  run update-initramfs -u
  uuid=$(blkid -s UUID -o value "$md")
  mkdir -p /srv/data
  set_fstab /srv/data "UUID=$uuid /srv/data ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2"
  run mount /srv/data
  install -d -m 0700 /srv/data/backups
  ok "/srv/data: $(df -h --output=size /srv/data | tail -n1 | tr -d ' '), RAID1; first sync runs in the background (cat /proc/mdstat)"
fi

# ---- 8. the kit -------------------------------------------------------------------------
step "Install the kit"
install -d -m 0755 "$LIBDIR" "$LIBDIR/bin" "$LIBDIR/lib"
install -m 0644 "$KIT"/lib/*.py "$LIBDIR/lib/"
install -m 0755 "$KIT"/bin/* "$LIBDIR/bin/"
ln -sfn "$LIBDIR/bin/ollama1-pair" /usr/local/sbin/ollama1-pair
ln -sfn "$LIBDIR/bin/ollama1-cf-access" /usr/local/sbin/ollama1-cf-access
ln -sfn "$LIBDIR/bin/ollama1-lan" /usr/local/sbin/ollama1-lan
ln -sfn "$LIBDIR/bin/ollama1-dash" /usr/local/bin/ollama1-top
ln -sfn /opt/ollama/current/bin/ollama /usr/local/bin/ollama
install -m 0644 "$KIT"/systemd/* /etc/systemd/system/
install -d -m 0750 -g polkitd /etc/polkit-1/rules.d 2>/dev/null || install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$KIT/config/50-ollama1.rules" /etc/polkit-1/rules.d/50-ollama1.rules
install -m 0644 "$KIT/config/ollama1.tmpfiles" /etc/tmpfiles.d/ollama1.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ollama1.conf
install -m 0644 "$KIT/config/60-ollama1-bridge.conf" /etc/sysctl.d/60-ollama1-bridge.conf
sysctl -q -p /etc/sysctl.d/60-ollama1-bridge.conf 2>/dev/null || true
install -d -m 0755 /etc/ollama1 /var/lib/ollama1 /opt/ollama
sed -e "s/@UID_OLLAMA@/$(id -u ollama)/g" -e "s/@UID_O1GW@/$(id -u o1gw)/g" \
    -e "s/@UID_O1ADMIN@/$(id -u o1admin)/g" -e "s/@UID_CLOUDFLARED@/$(id -u cloudflared)/g" \
    "$KIT/config/ollama1.nft.in" >/etc/ollama1/ollama1.nft
nft -c -f /etc/ollama1/ollama1.nft || die "the nftables rules don't load"
[ -f /etc/ollama1/models.allow ] || install -m 0644 "$KIT/config/models.allow" /etc/ollama1/models.allow
chown root:root /etc/ollama1/models.allow; chmod 0644 /etc/ollama1/models.allow
if [ ! -f /etc/ollama1/devices.json ]; then
  printf '{"version": 1, "devices": []}\n' >/etc/ollama1/devices.json
fi
chown root:o1view /etc/ollama1/devices.json; chmod 0640 /etc/ollama1/devices.json

LANIF=enp39s0
if [ -d /sys/class/net/br0 ]; then
  LANIF=br0
  [ "$(cat /sys/class/net/br0/operstate)" = up ] || note "br0 exists but is not up; not touching it (network settings are not this script's)"
fi
LANIP=$(ip -4 -o addr show dev "$LANIF" | awk '{print $4}' | cut -d/ -f1 | head -n1)
ok "LAN: $LANIF ${LANIP:-no address} (network settings left as they are)"

python3 - "$LANIP" <<'PY'
import json, os, sys
path = "/etc/ollama1/config.json"
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)
if sys.argv[1]:
    cfg["lan_bind"] = sys.argv[1]
cfg.setdefault("lan_mode", False)
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=1, sort_keys=True)
os.replace(tmp, path)
PY
chown root:root /etc/ollama1/config.json; chmod 0600 /etc/ollama1/config.json
if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open("/etc/ollama1/config.json")).get("admin_email") else 1)'; then
  printf '   The admin email: the only address allowed into %s.\n   It stays on this desktop (root-only), never in the repo.\n   Admin email: ' "$ADMIN_HOST"
  read -r email </dev/tty
  [[ "$email" == *@*.* ]] || die "that doesn't look like an email address"
  python3 - "$email" <<'PY'
import json, os, sys
p = "/etc/ollama1/config.json"
cfg = json.load(open(p))
cfg["admin_email"] = sys.argv[1].strip()
fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=1, sort_keys=True)
os.replace(p + ".tmp", p)
PY
fi
systemctl daemon-reload
ok "kit in $LIBDIR; settings in /etc/ollama1 (config.json root-only)"

# ---- 9. Ollama ------------------------------------------------------------------
step "Ollama (verified download, ROCm build)"
run systemctl enable --now ollama1-nft.service
if ! "$LIBDIR/bin/ollama1-update-ollama" --no-restart; then
  [ -L /opt/ollama/current ] || die "could not install Ollama (see above); nothing is running yet. Run setup.sh again."
  note "the update check failed; keeping the installed version"
fi
run systemctl enable ollama.service
run systemctl restart ollama.service
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:11434/api/version >/dev/null || die "Ollama did not start: journalctl -u ollama"
gpuline=$(journalctl -u ollama -b --no-pager -o cat 2>/dev/null | grep -i 'inference compute' | tail -n1 || true)
if echo "$gpuline" | grep -qi 'rocm'; then
  ok "Ollama $(curl -fsS http://127.0.0.1:11434/api/version | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])') sees the GPU: ${gpuline#*msg=}"
else
  note "Ollama did not report a ROCm GPU yet: ${gpuline:-no 'inference compute' line}. The gateway refuses anything not 100% on the GPU, so nothing runs on the CPU. Check: journalctl -u ollama | grep -i -E 'rocm|amdgpu|gfx'"
  later "Ollama didn't report the GPU at setup time; check journalctl -u ollama"
fi

# ---- 10. firewall ----------------------------------------------------------------------
step "Firewall"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw default deny routed >/dev/null
for r in OpenSSH ssh 22 22/tcp; do ufw delete allow "$r" >/dev/null 2>&1 || true; done
run ufw allow in on "$LANIF" from "$HOME_LAN" to any port 22 proto tcp comment 'ollama1: SSH from the home LAN'
if [ "$LANIF" = br0 ]; then
  # Bridged frames (the Pi on enp38s0) don't go through iptables while
  # bridge netfilter is off (60-ollama1-bridge.conf). If something loads
  # br_netfilter anyway, this rule keeps them flowing.
  run ufw route allow in on br0 out on br0 comment 'ollama1: bridged LAN traffic (the Pi)'
fi
if python3 -c 'import json,sys; sys.exit(0 if json.load(open("/etc/ollama1/config.json")).get("lan_mode") else 1)'; then
  # the same rule ollama1-lan adds and removes
  run ufw allow from "$HOME_LAN" to "$LANIP" port 8431 proto tcp
fi
run ufw --force enable
for k in net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables; do
  v=$(sysctl -n "$k" 2>/dev/null || echo "off")
  [ "$v" = 0 ] || [ "$v" = off ] || die "$k is $v; bridged traffic would hit the firewall"
done
ok "incoming: SSH from $HOME_LAN only; bridge netfilter off$([ "$LANIF" = br0 ] && echo '; br0 to br0 routed traffic allowed')"

# ---- 11. SSH --------------------------------------------------------------------------------
step "SSH"
# Keys of your own = key lines in authorized_keys other than the setup key.
own_keys() {
  [ -f "$KEYS" ] || { echo 0; return; }
  grep -E '^[[:space:]]*(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-(ssh-ed25519|ecdsa-sha2))' "$KEYS" | grep -vcF "$CLAUDE_KEY" || true
}
OWN_KEYS=$(own_keys)
DROPIN=/etc/ssh/sshd_config.d/10-ollama1.conf
CLOUDINIT=/etc/ssh/sshd_config.d/50-cloud-init.conf
if [ "$OWN_KEYS" -gt 0 ]; then
  pw=$'PasswordAuthentication no\nKbdInteractiveAuthentication no\nAuthenticationMethods publickey'
else
  pw=$'# Password login stays on until '"$KEYS"$'\n# holds a key of your own (besides '"$CLAUDE_KEY"$'). Run setup.sh again after adding it.'
fi
new=$(mktemp)
awk -v pw="$pw" '{ if ($0 == "@PASSWORD_LINES@") print pw; else print }' "$KIT/config/10-ollama1-sshd.conf.in" >"$new"
bak=$(mktemp -d)
[ -f "$DROPIN" ] && cp -a "$DROPIN" "$bak/dropin"
[ -f "$CLOUDINIT" ] && cp -a "$CLOUDINIT" "$bak/cloudinit"
restore_ssh() {
  if [ -f "$bak/dropin" ]; then cp -a "$bak/dropin" "$DROPIN"; else rm -f "$DROPIN"; fi
  if [ -f "$bak/cloudinit" ]; then cp -a "$bak/cloudinit" "$CLOUDINIT"; fi
}
install -m 0644 "$new" "$DROPIN"; rm -f "$new"
# 10-ollama1.conf is read before 50-cloud-init.conf and sshd keeps the first
# value it reads, so ours already wins. The cloud-init file's
# "PasswordAuthentication yes" is commented out too, so nobody reading it is
# misled and nothing depends on file order alone (a copy stays in /root).
if [ "$OWN_KEYS" -gt 0 ] && [ -f "$CLOUDINIT" ] && grep -qiE '^[[:space:]]*PasswordAuthentication[[:space:]]+yes' "$CLOUDINIT"; then
  [ -f /root/50-cloud-init.conf.before-ollama1 ] || cp -a "$CLOUDINIT" /root/50-cloud-init.conf.before-ollama1
  sed -i -E 's/^([[:space:]]*PasswordAuthentication[[:space:]]+yes.*)$/# ollama1: password login is off (10-ollama1.conf)\n# \1/I' "$CLOUDINIT"
fi
if ! sshd -t; then
  restore_ssh; rm -rf "$bak"
  die "sshd rejected the new settings; the previous ones are back"
fi
# What sshd will actually do for pmiller coming from the LAN:
eff=$(sshd -T -C "user=$ADMIN_USER,host=mac.lan,addr=192.168.86.20,laddr=${LANIP:-192.168.86.10},lport=22" 2>/dev/null || true)
want_pw=yes; [ "$OWN_KEYS" -gt 0 ] && want_pw=no
if ! echo "$eff" | grep -qx "permitrootlogin no" || ! echo "$eff" | grep -qx "passwordauthentication $want_pw" \
   || { [ "$want_pw" = no ] && ! echo "$eff" | grep -qx "kbdinteractiveauthentication no"; }; then
  restore_ssh; rm -rf "$bak"
  die "sshd's effective settings are not what they should be (sshd -T); the previous ones are back"
fi
rm -rf "$bak"
systemctl try-reload-or-restart ssh.service
if [ "$OWN_KEYS" -gt 0 ]; then
  ok "passwords off: only $ADMIN_USER, from $HOME_LAN, with a key ($OWN_KEYS key(s) of your own); no root login"
else
  note "no root login; only $ADMIN_USER from $HOME_LAN. Password login stays ON: $KEYS has no key of your own"
  note "(on your Mac: ssh-copy-id $ADMIN_USER@${LANIP:-192.168.86.10}, then run setup.sh again)"
  later "Add your Mac's SSH key (ssh-copy-id), then run sudo ./setup.sh again: it switches passwords off"
fi

# ---- 12. updates ------------------------------------------------------------------------------
step "Automatic updates"
install -m 0644 "$KIT/config/52ollama1-unattended-upgrades" /etc/apt/apt.conf.d/52ollama1-unattended-upgrades
install -m 0644 "$KIT/config/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
run systemctl enable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer
[ "$(apt-config dump | awk -F'"' '/^Unattended-Upgrade::Automatic-Reboot-Time /{print $2}')" = "04:00" ] || die "unattended-upgrades did not pick up the reboot time"
ok "security updates + cloudflared daily; reboot at 04:00 ($TIMEZONE) when one needs it"
run systemctl enable --now ollama1-update-ollama.timer
ok "Ollama: weekly, $(systemctl show -P NextElapseUSecRealtime ollama1-update-ollama.timer)"

# ---- 13. services ------------------------------------------------------------------------------
step "Services"
run systemctl enable --now ollama1-pair-commit.path ollama1-backup.timer
run systemctl restart ollama1-nft.service
run systemctl enable ollama1-gateway.service ollama1-admin.service ollama1-ttyd.service
run systemctl restart ollama1-gateway.service ollama1-admin.service ollama1-ttyd.service
systemctl mask getty@tty1.service >/dev/null 2>&1 || true
run systemctl enable ollama1-dash.service
systemctl stop getty@tty1.service >/dev/null 2>&1 || true
run systemctl restart ollama1-dash.service
sleep 2
for s in ollama ollama1-gateway ollama1-admin ollama1-ttyd ollama1-dash; do
  if systemctl is-active --quiet "$s"; then ok "$s running"; else note "$s is not running: journalctl -u $s"; later "$s did not start: journalctl -u $s"; fi
done

# ---- 14. Cloudflare -------------------------------------------------------------------------
step "Cloudflare Tunnel and Access"
cfg_has() { python3 -c 'import json,sys; c=json.load(open("/etc/ollama1/config.json")); sys.exit(0 if all(c.get(k) for k in sys.argv[1:]) else 1)' "$@"; }
if [ "$SKIP_CF" = 1 ]; then
  note "skipped (--skip-cloudflare)"
  later "Cloudflare: run sudo ./setup.sh again without --skip-cloudflare"
else
  export HOME=/root
  if [ ! -f /root/.cloudflared/cert.pem ]; then
    cat <<EOF

   Cloudflare needs your OK in a browser once:
     1. cloudflared prints a link below. Open it on your Mac.
     2. Log in to Cloudflare, pick flyconcordefly.com, click Authorize.
     3. Come back here; it continues by itself.

EOF
    run cloudflared tunnel login
  fi
  chmod 600 /root/.cloudflared/cert.pem
  tid=$(cloudflared tunnel list -o json 2>/dev/null | python3 -c 'import json,sys
for t in json.load(sys.stdin) or []:
    if t.get("name") == "ollama1" and not t.get("deleted_at", "").startswith("2"): print(t["id"]); break' || true)
  if [ -z "$tid" ]; then
    run cloudflared tunnel create ollama1
    tid=$(cloudflared tunnel list -o json | python3 -c 'import json,sys
for t in json.load(sys.stdin) or []:
    if t.get("name") == "ollama1": print(t["id"]); break')
  fi
  [ -n "$tid" ] || die "could not create or find the tunnel ollama1"
  install -d -m 0755 /etc/cloudflared
  if [ ! -f /etc/cloudflared/ollama1.json ]; then
    if [ -f "/root/.cloudflared/$tid.json" ]; then
      install -m 0600 "/root/.cloudflared/$tid.json" /etc/cloudflared/ollama1.json
      rm -f "/root/.cloudflared/$tid.json"
    else
      run cloudflared tunnel token --cred-file /etc/cloudflared/ollama1.json ollama1
    fi
  fi
  chown root:root /etc/cloudflared/ollama1.json; chmod 0600 /etc/cloudflared/ollama1.json
  ok "tunnel ollama1 ($tid); credential root-only in /etc/cloudflared/ollama1.json"
  for h in "$GW_HOST" "$ADMIN_HOST"; do
    if cloudflared tunnel route dns ollama1 "$h"; then ok "DNS $h -> tunnel"; else
      note "DNS for $h not set (a record may already exist). In the dashboard: DNS > $h > CNAME $tid.cfargotunnel.com, proxied"
      later "Check the DNS record for $h (CNAME to $tid.cfargotunnel.com)"
    fi
  done

  if ! cfg_has access_team_domain gateway_aud admin_aud admin_email; then
    cat <<EOF

   Cloudflare Access protects both names. Choose one:
     1  Use the API helper now (paste a Cloudflare API token; it is not saved)
     2  I did it in the dashboard (README: "Access in the dashboard"); ask me for the values
     3  Later (the tunnel stays off until Access is set up)
EOF
    printf '   1, 2 or 3: '
    read -r choice </dev/tty
    case "$choice" in
      1) "$LIBDIR/bin/ollama1-cf-access" || note "the API helper stopped; run sudo ollama1-cf-access later" ;;
      2)
        printf '   Team domain (like yourteam.cloudflareaccess.com): '; read -r team </dev/tty
        printf '   AUD tag of the %s application: ' "$ADMIN_HOST"; read -r aaud </dev/tty
        printf '   AUD tag of the %s application: ' "$GW_HOST"; read -r gaud </dev/tty
        printf '   Service token Client ID (ends in .access): '; read -r cid </dev/tty
        python3 - "$team" "$aaud" "$gaud" "$cid" <<'PY'
import json, os, re, sys
team, aaud, gaud, cid = [a.strip() for a in sys.argv[1:]]
team = re.sub(r"^https?://", "", team).rstrip("/")
if not re.match(r"^[a-z0-9-]+\.cloudflareaccess\.com$", team): sys.exit("team domain should look like yourteam.cloudflareaccess.com")
for v in (aaud, gaud):
    if not re.match(r"^[0-9a-f]{64}$", v): sys.exit("an AUD tag is 64 hex characters")
if cid and not cid.endswith(".access"): sys.exit("a service token Client ID ends in .access")
p = "/etc/ollama1/config.json"
c = json.load(open(p))
c.update(access_team_domain=team, admin_aud=aaud, gateway_aud=gaud, service_token_client_id=cid)
fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(c, f, indent=1, sort_keys=True)
os.replace(p + ".tmp", p)
PY
        ;;
      *) note "Access later" ;;
    esac
  fi
  if cfg_has access_team_domain gateway_aud admin_aud admin_email; then
    read -r TEAM GAUD AAUD < <(python3 -c 'import json, re
c = json.load(open("/etc/ollama1/config.json"))
vals = [c["access_team_domain"].split(".")[0], c["gateway_aud"], c["admin_aud"]]
assert all(re.match(r"^[A-Za-z0-9-]+$", v) for v in vals), "unexpected characters in the Access settings"
print(*vals)')
    sed -e "s/@TUNNEL_ID@/$tid/" -e "s/@TEAM_NAME@/$TEAM/g" -e "s/@GATEWAY_AUD@/$GAUD/" -e "s/@ADMIN_AUD@/$AAUD/" \
      "$KIT/config/cloudflared.yml.in" >/etc/ollama1/cloudflared.yml
    chown root:cloudflared /etc/ollama1/cloudflared.yml; chmod 0640 /etc/ollama1/cloudflared.yml
    run systemctl enable ollama1-tunnel.service
    run systemctl restart ollama1-tunnel.service ollama1-gateway.service ollama1-admin.service
    for _ in $(seq 1 20); do curl -fsS http://127.0.0.1:8439/ready >/dev/null 2>&1 && break; sleep 1; done
    if curl -fsS http://127.0.0.1:8439/ready >/dev/null 2>&1; then ok "tunnel connected"; else
      note "tunnel not connected yet: journalctl -u ollama1-tunnel"; later "Tunnel did not connect: journalctl -u ollama1-tunnel"
    fi
  else
    note "Access is not set up, so the tunnel stays off (nothing is reachable from outside)"
    later "Cloudflare Access: sudo ollama1-cf-access, or the dashboard steps in the README; then sudo ./setup.sh"
  fi
fi

# ---- 15. the setup key (only when asked) ----------------------------------------------------
remove_setup_key() {
  local n own tmp
  n=$(grep -cF "$CLAUDE_KEY" "$KEYS" 2>/dev/null || true)
  if [ "${n:-0}" -eq 0 ]; then ok "the setup key ($CLAUDE_KEY) is not in $KEYS"; return 0; fi
  own=$(own_keys)
  if [ "$own" -lt 1 ]; then
    note "not removing the setup key: $KEYS has no other key, and removing it would lock you out"
    return 0
  fi
  cp -a "$KEYS" "/root/authorized_keys.before-ollama1-$(date +%Y%m%d-%H%M%S)"
  tmp=$(mktemp "$KEYS.XXXXXX")
  # Drop only lines whose last field is exactly the setup key's comment.
  awk -v c="$CLAUDE_KEY" '$NF != c' "$KEYS" >"$tmp"
  [ "$(grep -cF "$CLAUDE_KEY" "$tmp" || true)" -eq 0 ] || { rm -f "$tmp"; die "could not take the setup key out of $KEYS"; }
  [ "$(grep -E '^[[:space:]]*(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-(ssh-ed25519|ecdsa-sha2))' "$tmp" | grep -vcF "$CLAUDE_KEY" || true)" -eq "$own" ] \
    || { rm -f "$tmp"; die "removing the setup key would also have changed your own keys; nothing changed"; }
  chown --reference="$KEYS" "$tmp"; chmod --reference="$KEYS" "$tmp"
  mv "$tmp" "$KEYS"
  ok "removed the setup key ($CLAUDE_KEY); your $own key(s) stay. A copy of the old file is in /root."
}
if [ "$(own_keys)" -gt 0 ] && grep -qF "$CLAUDE_KEY" "$KEYS" 2>/dev/null; then
  step "Remove the setup key ($CLAUDE_KEY)"
  if [ "$REMOVE_SETUP_KEY" = 1 ]; then
    remove_setup_key
  else
    printf '   %s is the key used to prepare this kit. With it gone, only your own key(s)\n   can log in over SSH. Remove it now? Type yes to remove it, anything else keeps it: ' "$CLAUDE_KEY"
    if ask_yes ""; then remove_setup_key; else
      note "kept; remove it later with: sudo ./setup.sh --remove-setup-key"
      later "Remove the setup key when you're done with it: sudo ./setup.sh --remove-setup-key"
    fi
  fi
fi

# ---- done -------------------------------------------------------------------------------------
printf '\n%sDone.%s  Log: %s\n' "$B" "$N" "$LOG"
cat <<EOF

  Screen:   the dashboard is on the desktop's monitor (tty1)
  SSH:      ollama1-top     (the same dashboard in a terminal)
  Pair:     sudo ollama1-pair
  Models:   none installed; add names to /etc/ollama1/models.allow, then Pull in the panel
  Panel:    https://$ADMIN_HOST
EOF
if [ "${#LATER[@]}" -gt 0 ]; then
  printf '\n%sStill to do:%s\n' "$Y$B" "$N"
  for l in "${LATER[@]}"; do printf '  - %s\n' "$l"; done
fi
if [ "$RAID_PENDING" = 1 ]; then exit 3; fi
exit 0
