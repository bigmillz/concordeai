#!/usr/bin/env bash
# ollama1 host kit: turns this machine into a private, locked-down model
# server for ConcordeAI. See README.md. ("ollama1" in this kit's file and
# command names is the kit's own name; your server's name is yours: --name.)
#
#   sudo ./setup.sh --name <server-name> --zone <your-domain> ...
#                                    everything, in order (safe to run again).
#                                    What you give is saved in /etc/ollama1/setup.env
#                                    (root-only), so a re-run needs none of it again.
#   sudo ./setup.sh --skip-cloudflare  everything except the tunnel and Access
#   sudo ./setup.sh --remove-setup-key also take claude-setup@concordeai out of
#                                    ~<your-user>/.ssh/authorized_keys (your own key stays)
#   sudo ./setup.sh --no-tmux        don't wrap the run in a tmux session
#   sudo ./setup.sh --no-console-font  leave the server's monitor font as it is (by default
#                                    the dashboard gets a big console font, about 120 columns;
#                                    OLLAMA1_DASH_FONT=off does the same)
#   sudo ./setup.sh --encrypted-swap 32G   opt in: encrypted swap (random key each boot),
#                                    replacing the plain /swap.img; nothing else runs
#   sudo ./setup.sh --remove-encrypted-swap  undo that
#   sudo ./setup.sh --vg-reserve 64G  when growing / the first time, leave 64G free
#                                    in ubuntu-vg (e.g. for --encrypted-swap); default 0
#   sudo ./setup.sh --gpu-tune       opt in (also: OLLAMA1_GPU_TUNE=1): tune an AMD Navi 21
#                                    card (ollama1-gpu-tune): its highest power limit and a
#                                    small memory-clock bump. OFF unless asked. Saved in
#                                    setup.env, so a re-run keeps it; --gpu-tune also tries
#                                    again after a safety revert
#   sudo ./setup.sh --no-gpu-tune    the explicit off (also: OLLAMA1_GPU_TUNE=0): the card back
#                                    to stock, and the setting saved as off
#   sudo ./setup.sh --fans on|off    the graphics card's and the case fans at 100% while the server
#                                    works and for one minute after, else automatic (ollama1-fan).
#                                    Default on; also OLLAMA1_FANS=1|0; saved in setup.env
#   ./setup.sh --plan                show what it would do; changes nothing
#
# Settings (each is asked for if it is not given and cannot be found):
#   --name <server-name>       your server's name: lowercase letters, digits, hyphens,
#                              1-32, starting with a letter. It becomes the host name,
#                              <server-name>.<your-domain> (the gateway),
#                              <server-name>-admin.<your-domain> (the panel), the tunnel
#                              and the Access names. Name several servers as you like.
#   --user <your-user>         the one Linux user who may log in over SSH
#                              (default: the user who ran sudo)
#   --lan <lan-cidr>           the network SSH is allowed from, like 10.0.0.0/24
#                              (default: the network of this machine's LAN port, if it is a private one)
#                              Only a private network of /16 or narrower is accepted;
#                              --lan-public-ok allows another one (SSH is let in from it)
#   --zone <your-domain>       your domain on Cloudflare (not needed with --skip-cloudflare)
#   --owner <your-name>        your name, only in the Access policy's name
#   --timezone <Area/City>     default: the time zone the machine already has
#   --os-serial / --models-serial / --hdd1-serial / --hdd2-serial <serial>
#                              the disks, by serial (lsblk -d -o NAME,SIZE,MODEL,SERIAL)
#
# It runs itself inside tmux (session "ollama1-setup"), so a dropped SSH
# connection can't stop it halfway: log in again and `sudo tmux attach -t
# ollama1-setup`. Every step checks what is already done and skips it, so
# after a stop just run it again. Everything it prints also goes to
# /var/log/ollama1-setup.log (root-only); secrets are never printed there.
#
# Nothing personal is stored in this script or the repo: the server's name,
# your user, LAN, domain and disks, the admin email, Cloudflare IDs and the
# tunnel credential are given or created here, on the server, and kept
# root-only.
# The single-quoted $... below are meant literally (checks run later, GRUB
# text matched as written):
# shellcheck disable=SC2016
set -euo pipefail
umask 022

# ---- fixed ---------------------------------------------------------------------
# (What is yours - name, user, LAN, domain, disks - is a setting: see below.)
OLD_HOSTNAME=$(hostnamectl --static 2>/dev/null || hostname 2>/dev/null || echo unknown)
CLAUDE_KEY=claude-setup@concordeai
CF_KEY_URL=https://pkg.cloudflare.com/cloudflare-main.gpg
CF_KEY_FPR=CC94B39C77AE7342A68B89628A682D308D4E5E73   # "CloudFlare Software Packaging 2025"
LIBDIR=/usr/local/lib/ollama1
LOG=/var/log/ollama1-setup.log
TMUX_SESSION=ollama1-setup

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/setuplib.sh
. "$KIT/lib/setuplib.sh"

bad_option() { echo "$1: $2"; exit 2; }
set_option() { # --flag value: checked here, so a typo stops before anything runs
  local v=$2
  case "$1" in
    --name) valid_name "$v" || bad_option --name "lowercase letters, digits and hyphens, 1-32 characters, starting with a letter"; A_NAME=$v ;;
    --user) valid_user "$v" || bad_option --user "a Linux user name, like alice"; A_USER=$v ;;
    --lan) valid_lan_shape "$v" || bad_option --lan "a network like 10.0.0.0/24 (the address part must be the network's first address)"; A_LAN=$v ;;
    --zone) v=$(printf '%s' "$v" | tr 'A-Z' 'a-z'); valid_zone "$v" || bad_option --zone "a domain like example.com"; A_ZONE=$v ;;
    --owner) valid_owner "$v" || bad_option --owner "your name: letters, digits, spaces, . _ - (1-40)"; A_OWNER=$v ;;
    --timezone) valid_tz "$v" || bad_option --timezone "like America/New_York"; A_TZ=$v ;;
    --os-serial) valid_serial "$v" || bad_option --os-serial "a disk serial (lsblk -d -o NAME,SERIAL)"; A_OS=$v ;;
    --models-serial) valid_serial "$v" || bad_option --models-serial "a disk serial (lsblk -d -o NAME,SERIAL)"; A_MODELS=$v ;;
    --hdd1-serial) valid_serial "$v" || bad_option --hdd1-serial "a disk serial (lsblk -d -o NAME,SERIAL)"; A_HDD1=$v ;;
    --hdd2-serial) valid_serial "$v" || bad_option --hdd2-serial "a disk serial (lsblk -d -o NAME,SERIAL)"; A_HDD2=$v ;;
  esac
}

PLAN_ONLY=0
LAN_PUBLIC_OK=0
SKIP_CF=0
REMOVE_SETUP_KEY=0
NO_TMUX=0
SWAP_ACTION=""
SWAP_SIZE=""
VG_RESERVE_GIB=0
A_GPU_TUNE=""
A_FANS=""
A_NAME=""; A_USER=""; A_LAN=""; A_ZONE=""; A_OWNER=""; A_TZ=""
A_OS=""; A_MODELS=""; A_HDD1=""; A_HDD2=""
prev=""
for a in "$@"; do
  if [ "$prev" = --encrypted-swap ]; then SWAP_SIZE=$a; prev=""; continue; fi
  if [ "$prev" = --fans ]; then A_FANS=$a; fans_choice "$A_FANS" "" "" >/dev/null || { echo "--fans takes on or off"; exit 2; }; prev=""; continue; fi
  case "$prev" in
    --name|--user|--lan|--zone|--owner|--timezone|--os-serial|--models-serial|--hdd1-serial|--hdd2-serial)
      set_option "$prev" "$a"; prev=""; continue ;;
  esac
  if [ "$prev" = --vg-reserve ]; then
    [[ "$a" =~ ^[0-9]{1,6}G$ ]] || { echo "--vg-reserve takes a size in GiB, like 64G (at most 999999G)"; exit 2; }
    VG_RESERVE_GIB=$((10#${a%G})); prev=""; continue   # 10#: "08G" is decimal, not octal
  fi
  prev=$a
  case "$a" in
    --encrypted-swap) SWAP_ACTION=on ;;
    --remove-encrypted-swap) SWAP_ACTION=off ;;
    --vg-reserve|--fans|--name|--user|--lan|--zone|--owner|--timezone|--os-serial|--models-serial|--hdd1-serial|--hdd2-serial) ;;
    --plan) PLAN_ONLY=1 ;;
    --skip-cloudflare) SKIP_CF=1 ;;
    --remove-setup-key) REMOVE_SETUP_KEY=1 ;;
    --no-tmux) NO_TMUX=1 ;;
    --no-console-font) export OLLAMA1_DASH_FONT=off ;;
    --lan-public-ok) LAN_PUBLIC_OK=1 ;;
    --gpu-tune|--no-gpu-tune)
      want=on; [ "$a" = --gpu-tune ] || want=off
      [ -z "$A_GPU_TUNE" ] || [ "$A_GPU_TUNE" = "$want" ] || { echo "--gpu-tune and --no-gpu-tune: give one"; exit 2; }
      A_GPU_TUNE=$want ;;
    -h|--help) sed -n '2,64p' "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done
# the LAN is checked once every flag is read (so --lan-public-ok may come after it)
if [ -n "$A_LAN" ] && ! valid_lan "$A_LAN"; then
  bad_option --lan "a private network of /16 or narrower, like 10.0.0.0/24 (SSH is let in from it, and from nowhere else); add --lan-public-ok only if a larger or public network is really meant"
fi
gpu_tune_choice "" "${OLLAMA1_GPU_TUNE:-}" "" >/dev/null || { echo "OLLAMA1_GPU_TUNE takes 1 or 0 (on or off)"; exit 2; }
# a flag left waiting for its value (the size forgotten) must not mean "no reserve"
case "$prev" in --vg-reserve|--encrypted-swap) echo "$prev takes a size, like 64G"; exit 2 ;; esac
[ "$prev" != --fans ] || { echo "--fans takes on or off"; exit 2; }
fans_choice "" "${OLLAMA1_FANS:-}" "" >/dev/null || { echo "OLLAMA1_FANS takes 1 or 0 (on or off)"; exit 2; }
case "$prev" in --name|--user|--lan|--zone|--owner|--timezone|--os-serial|--models-serial|--hdd1-serial|--hdd2-serial)
  echo "$prev takes a value"; exit 2 ;; esac


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

# ---- settings ---------------------------------------------------------------
# Given as arguments, else saved by an earlier run (/etc/ollama1/setup.env),
# else read from config.json or detected, else asked (prompt_settings).
default_route_dev() {
  command -v ip >/dev/null 2>&1 || return 0
  ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}'
}
lan_dev() { if [ -d /sys/class/net/br0 ]; then echo br0; else default_route_dev; fi; }
detect_lan() { # the network of the LAN port's address, like 10.0.0.0/24
  local dev cidr
  dev=$(lan_dev); [ -n "$dev" ] || return 0
  cidr=$(ip -4 -o addr show dev "$dev" 2>/dev/null | awk '{print $4}' | head -n1)
  [ -n "$cidr" ] || return 0
  python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$cidr" 2>/dev/null || true
}

pick() { # VAR ARG SAVEDKEY CFGKEY VALIDATOR: the argument, else the saved value, else config.json's
  local var=$1 v=$2
  if [ -z "$v" ]; then v=$(saved "$3"); fi
  if [ -z "$v" ] && [ -n "$4" ]; then v=$(cfg_str "$4"); fi
  if [ -n "$v" ] && ! "$5" "$v"; then die "the saved $3 '$v' is not valid; give it again as an argument"; fi
  printf -v "$var" '%s' "$v"
}

LAN_SOURCE=""
resolve_settings() {
  local v rc=0
  SERVER_NAME=""
  if v=$(resolve_server_name "$A_NAME"); then SERVER_NAME=$v; else rc=$?; [ "$rc" = 1 ] || die "the server name (see above)"; fi
  pick ADMIN_USER "$A_USER" ADMIN_USER "" valid_user
  if [ -z "$ADMIN_USER" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] && valid_user "$SUDO_USER"; then ADMIN_USER=$SUDO_USER; fi
  pick HOME_LAN "$A_LAN" HOME_LAN lan_cidr valid_lan
  LAN_SOURCE="given"
  if [ -z "$HOME_LAN" ]; then
    HOME_LAN=$(detect_lan)
    LAN_SOURCE="detected from this machine's LAN port"
    # a network that isn't a private one (or is wider than /16) is never used unasked
    if [ -n "$HOME_LAN" ] && ! valid_lan "$HOME_LAN"; then
      note "this machine's LAN port is on $HOME_LAN, which isn't a private network of /16 or narrower, so it is not used for SSH: give --lan (and --lan-public-ok if it is meant)"
      HOME_LAN=""
    fi
  fi
  pick CF_ZONE "$A_ZONE" CF_ZONE cf_zone valid_zone
  if [ -z "$CF_ZONE" ]; then
    v=$(cfg_str hostname_gateway)       # an install that has its hostnames written down
    if [ -n "$v" ] && [ "${v#*.}" != "$v" ] && valid_zone "${v#*.}"; then CF_ZONE=${v#*.}; fi
  fi
  pick OWNER "$A_OWNER" OWNER owner_label valid_owner
  pick TIMEZONE "$A_TZ" TIMEZONE "" valid_tz
  if [ -z "$TIMEZONE" ]; then TIMEZONE=$(timedatectl show -p Timezone --value 2>/dev/null || true); fi
  pick OS_SERIAL "$A_OS" OS_SERIAL "" valid_serial
  pick MODELS_SERIAL "$A_MODELS" MODELS_SERIAL "" valid_serial
  pick HDD1_SERIAL "$A_HDD1" HDD1_SERIAL "" valid_serial
  pick HDD2_SERIAL "$A_HDD2" HDD2_SERIAL "" valid_serial
  GPU_TUNE=$(gpu_tune_choice "$A_GPU_TUNE" "${OLLAMA1_GPU_TUNE:-}" "$(saved GPU_TUNE)") \
    || die "the saved GPU_TUNE in $SAVED is not on or off; give --gpu-tune or --no-gpu-tune"   # "default": nothing asked
  FANS=$(fans_choice "$A_FANS" "${OLLAMA1_FANS:-}" "$(saved FANS)") || die "the saved FANS in $SAVED is not on or off; give --fans on or --fans off"
}

# After the relaunch in tmux (a prompt needs the terminal): ask for what is
# still missing, and stop with the argument to give if there is no terminal.
ask_setting() { # VAR "what" "--flag" validator "example"
  local v=""
  [ -n "${!1}" ] && return 0
  [ -t 0 ] || [ -r /dev/tty ] || die "$2 is not set: give $3 <$5>"
  printf '   %s (like %s): ' "$2" "$5"
  read -r v </dev/tty || die "$2 is not set: give $3"
  "$4" "$v" || die "that is not a valid $2: give $3 <$5>"
  printf -v "$1" '%s' "$v"
}
prompt_settings() {
  ask_setting SERVER_NAME "Your server's name" --name valid_name "my-server"
  ask_setting ADMIN_USER "The Linux user who may log in over SSH" --user valid_user "alice"
  ask_setting HOME_LAN "Your LAN, the network SSH is allowed from" --lan valid_lan "10.0.0.0/24"
  [ "$SKIP_CF" = 1 ] || ask_setting CF_ZONE "Your domain on Cloudflare" --zone valid_zone "example.com"
  if [ -z "$OS_SERIAL$MODELS_SERIAL$HDD1_SERIAL$HDD2_SERIAL" ] && [ -r /dev/tty ]; then
    printf '\n   Your disks, by serial:\n'; lsblk -d -o NAME,SIZE,MODEL,SERIAL 2>/dev/null | sed 's/^/     /' || true
  fi
  ask_setting OS_SERIAL "Serial of the OS disk (kept)" --os-serial valid_serial "from the list above"
  ask_setting MODELS_SERIAL "Serial of the models disk (WIPED)" --models-serial valid_serial "from the list above"
  ask_setting HDD1_SERIAL "Serial of the first mirror disk (WIPED)" --hdd1-serial valid_serial "from the list above"
  ask_setting HDD2_SERIAL "Serial of the second mirror disk (WIPED)" --hdd2-serial valid_serial "from the list above"
}
save_settings() { # after "yes": so a re-run needs no arguments
  install -d -m 0755 /etc/ollama1
  local t=$SAVED.tmp
  ( umask 077; {
      printf 'SERVER_NAME=%s\nADMIN_USER=%s\nHOME_LAN=%s\nCF_ZONE=%s\nOWNER=%s\nTIMEZONE=%s\n' \
        "$SERVER_NAME" "$ADMIN_USER" "$HOME_LAN" "$CF_ZONE" "$OWNER" "$TIMEZONE"
      printf 'OS_SERIAL=%s\nMODELS_SERIAL=%s\nHDD1_SERIAL=%s\nHDD2_SERIAL=%s\n' \
        "$OS_SERIAL" "$MODELS_SERIAL" "$HDD1_SERIAL" "$HDD2_SERIAL"
      if [ "$GPU_TUNE" != default ]; then printf 'GPU_TUNE=%s\n' "$GPU_TUNE"; fi   # asked for, on or off
      printf 'FANS=%s\n' "$FANS"
    } >"$t" )
  chown root:root "$t"; chmod 0600 "$t"; mv "$t" "$SAVED"
}
derive_hosts() { # the hostnames: config.json's own win, else <name>.<zone>
  GW_HOST=$(cfg_str hostname_gateway); ADMIN_HOST=$(cfg_str hostname_admin)
  if [ -z "$GW_HOST" ] && [ -n "$CF_ZONE" ]; then GW_HOST=$SERVER_NAME.$CF_ZONE; fi
  if [ -z "$ADMIN_HOST" ] && [ -n "$CF_ZONE" ]; then ADMIN_HOST=$SERVER_NAME-admin.$CF_ZONE; fi
  if [ -z "$GW_HOST" ]; then GW_HOST="<server-name>.<your-domain>"; ADMIN_HOST="<server-name>-admin.<your-domain>"; fi
}

# ---- disks ----------------------------------------------------------------
root_disk() { disk_of "$(findmnt -no SOURCE /)"; }   # lsblk -s follows / through LVM to its own disk

find_disks() {
  OS_DISK=$(disk_by_serial "$OS_SERIAL")
  MODELS_DISK=$(disk_by_serial "$MODELS_SERIAL")
  HDD1=$(disk_by_serial "$HDD1_SERIAL")
  HDD2=$(disk_by_serial "$HDD2_SERIAL")
}

home_on_own_disk() { findmnt -n --target /home -o TARGET 2>/dev/null | grep -qx /home; }
home_in_fstab() { awk '$0 !~ /^[[:space:]]*#/ && $2 == "/home"' /etc/fstab | grep -q .; }
models_done() { mountpoint -q /srv/models && [ "$(disk_of "$(findmnt -no SOURCE /srv/models)")" = "$MODELS_DISK" ]; }
raid_done() { mountpoint -q /srv/data && [ -n "$(raid_find)" ]; }

# ---- the plan --------------------------------------------------------------
show_disks() {
  printf '\n   %-13s %-17s %-32s %s\n' "Device" "Serial" "Model / size" "Role"
  printf '   %-13s %-17s %-32s %s\n' "${OS_DISK:-MISSING}" "$OS_SERIAL" "$(disk_desc "${OS_DISK:-/dev/null}")" "OS: kept; root LV grows into free space"
  printf '   %-13s %-17s %-32s %s\n' "${MODELS_DISK:-MISSING}" "$MODELS_SERIAL" "$(disk_desc "${MODELS_DISK:-/dev/null}")" "$(models_done && echo 'models: already set up' || echo 'WIPED -> ext4 /srv/models')"
  printf '   %-13s %-17s %-32s %s\n' "${HDD1:-MISSING}" "$HDD1_SERIAL" "$(disk_desc "${HDD1:-/dev/null}")" "$(raid_done && echo 'mirror: already set up' || echo 'WIPED -> RAID1 /srv/data (after /home moves off it)')"
  printf '   %-13s %-17s %-32s %s\n' "${HDD2:-MISSING}" "$HDD2_SERIAL" "$(disk_desc "${HDD2:-/dev/null}")" "$(raid_done && echo 'mirror: already set up' || echo 'WIPED -> RAID1 /srv/data')"
}

state() { if eval "$1" >/dev/null 2>&1; then printf '%sdone%s ' "$G" "$N"; else printf 'to do'; fi; }

policy_line() { # which Access policy name the admin panel's access uses, and what another --owner does
  local p; p=$(cfg_str policy_admin_name)
  if [ -z "$p" ]; then
    if [ -n "$OWNER" ]; then p="$SERVER_NAME admin - $OWNER only"; else p="$SERVER_NAME admin only"; fi
  fi
  printf "Cloudflare Access policy for the admin panel: '%s'. Running setup again with a different --owner makes a second policy (the first stays); the app and panel use the one named policy_admin_name in config.json, else this name." "$p"
}

gpu_tune_plan() { # the plan's line for the graphics card (6b361)
  if [ "$GPU_TUNE" = on ]; then
    printf 'Graphics card tuning ON (AMD Navi 21 only): its highest power limit, memory clock +100 (within its range), a 60 s check; kernel overdrive switch, so a reboot. Off: --no-gpu-tune'
  elif [ "$GPU_TUNE" = off ]; then
    printf 'Graphics card tuning OFF (--no-gpu-tune): the card stays at stock. On: --gpu-tune'
  else
    printf 'Graphics card tuning: off unless you ask (--gpu-tune; AMD Navi 21 only; see the README). Nothing changes'
  fi
}

print_plan() {
  printf '%sollama1 setup: the plan%s\n' "$B" "$N"
  printf '\n   Server name %s, SSH user %s, LAN %s (%s), domain %s, time zone %s\n' \
    "$SERVER_NAME" "$ADMIN_USER" "$HOME_LAN" "$LAN_SOURCE" "${CF_ZONE:-none}" "$TIMEZONE"
  show_disks
  cat <<EOF

   $(state '[ "$(hostname)" = "$SERVER_NAME" ]')  1. Host name $SERVER_NAME, time zone $TIMEZONE, boot menu shown for 5 s
   $(state 'command -v cloudflared && command -v ttyd && python3 -c "import nacl"')  2. Packages: ttyd, python3-nacl, mdadm, nftables, zstd, cloudflared (Cloudflare's apt repo, key checked)
   $(state '[ "$(vg_free_extents)" -le "$(vg_keep_extents "$VG_RESERVE_GIB")" ]')  3. Grow the root volume into the free space on the OS disk (online)
   $(state '! home_on_own_disk && ! home_in_fstab')  4. Copy /home onto the root filesystem, check it (SSH keys included), stop mounting the old disk
   $(state models_done)  5. Models disk: wipe, ext4, mount at /srv/models (noatime)
   $(state raid_done)  6. Mirror: wipe both mirror disks, RAID1, ext4, mount at /srv/data (resync runs in the background)
   $(state 'id o1gw && id o1admin && id o1dash && id ollama')  7. Service users (ollama, o1gw, o1admin, o1dash, cloudflared), no shells
   $(state '[ -x $LIBDIR/bin/ollama1-gateway ]')  8. Install the gateway, admin panel, dashboard, pairing tool, updater, units, polkit rule
   $(state 'ufw status | grep -q "Status: active"')  9. Firewall: nothing in except SSH from $HOME_LAN; bridged LAN traffic untouched
   $(state '[ -f /etc/ssh/sshd_config.d/10-ollama1.conf ]') 10. SSH: passwords off, only $ADMIN_USER with a key, no root (after you confirm your key)
   $(state '[ -L /opt/ollama/current ]') 11. Ollama (ROCm build) from GitHub, checksums verified; GPU only; local only
   $(state '[ -f /etc/apt/apt.conf.d/52ollama1-unattended-upgrades ]') 12. Automatic security updates (+ cloudflared), reboot at 04:00 when needed; weekly Ollama update
   $(state 'systemctl is-active ollama1-dash') 13. Services: gateway, admin panel, web terminal, dashboard on the screen (big console font), timers
   $(state 'systemctl is-enabled ollama1-gpu-tune') 14. $(gpu_tune_plan)
   $(state 'systemctl is-active ollama1-fan')     $(fans_plan "$FANS")
   $(state 'systemctl is-active ollama1-tunnel') 15. Cloudflare with one API token: tunnel, DNS for $GW_HOST and $ADMIN_HOST, Access
         16. Only if you say so: remove the setup key $CLAUDE_KEY from authorized_keys

   Not touched: the network settings (netplan, br0), anything in /home besides the move.
   No models are installed.
   $(policy_line)
EOF
}

# ---- preflight --------------------------------------------------------------
if [ "$PLAN_ONLY" = 1 ]; then
  resolve_settings
  # what is not known yet shows as a placeholder (the real run asks for it)
  [ -n "$SERVER_NAME" ] || SERVER_NAME="<server-name>"
  [ -n "$ADMIN_USER" ] || ADMIN_USER="<your-user>"
  if [ -z "$HOME_LAN" ]; then HOME_LAN="<lan-cidr>"; LAN_SOURCE="not found: give --lan"; fi
  [ -n "$OS_SERIAL" ] || OS_SERIAL="<os-serial>"
  [ -n "$MODELS_SERIAL" ] || MODELS_SERIAL="<models-serial>"
  [ -n "$HDD1_SERIAL" ] || HDD1_SERIAL="<hdd1-serial>"
  [ -n "$HDD2_SERIAL" ] || HDD2_SERIAL="<hdd2-serial>"
  [ -n "$TIMEZONE" ] || TIMEZONE="<your-timezone>"
  find_disks
  derive_hosts
  print_plan
  exit 0
fi
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo:  sudo $0"; exit 1; }
if [ -n "$SWAP_ACTION" ]; then   # a separate, opt-in, undoable step
  exec bash "$KIT/tools/encrypted-swap.sh" "$SWAP_ACTION" "$SWAP_SIZE"
fi

# One setup at a time. The lock is taken before anything else, including
# the copy of the kit to /var/tmp; fd 9 (and so the lock) survives the
# exec below. --no-tmux runs keep it to the end.
LOCK=/run/ollama1-setup.lock
STATUS_FILE=/run/ollama1-setup.status
if [ "${OLLAMA1_LOCKED:-}" != 1 ]; then
  exec 9>"$LOCK"
  if ! flock -w "${OLLAMA1_LOCK_WAIT:-0}" 9; then
    echo "Setup is already running."
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      echo "Reattach with:  sudo tmux attach -t $TMUX_SESSION"
    fi
    exit 1
  fi
  export OLLAMA1_LOCKED=1
fi

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

# Run inside tmux, so a dropped SSH connection can't kill it mid-step. The
# run inside tmux takes the lock over; its exit status comes back here.
if [ "$NO_TMUX" = 0 ] && [ -z "${TMUX:-}" ] && [ -z "${STY:-}" ]; then
  if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      echo "Setup is already running in tmux. Reattach with:  sudo tmux attach -t $TMUX_SESSION"
      exit 1
    fi
    echo "Starting setup inside tmux (session $TMUX_SESSION), so a dropped connection can't stop it."
    echo "If you get disconnected, log in again and run:  sudo tmux attach -t $TMUX_SESSION"
    sleep 2
    rm -f "$STATUS_FILE"
    inner="OLLAMA1_LOCKED=0 OLLAMA1_LOCK_WAIT=15 OLLAMA1_RELOCATED=1 ${OLLAMA1_DASH_FONT:+OLLAMA1_DASH_FONT=$(printf '%q' "$OLLAMA1_DASH_FONT") }bash $(printf '%q' "$KIT/setup.sh")"
    for a in "$@"; do inner+=" $(printf '%q' "$a")"; done
    inner+="; echo \$? >$STATUS_FILE; read -r -p 'Setup has finished. Press Enter to close this tmux session. ' _"
    flock -u 9; exec 9>&-
    tmux new-session -s "$TMUX_SESSION" -c / "$inner" || true
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
      echo "Setup is still running in tmux. Reattach with:  sudo tmux attach -t $TMUX_SESSION"
      exit 0
    fi
    if [ -s "$STATUS_FILE" ]; then
      rc=$(cat "$STATUS_FILE")
      echo "Setup finished with exit status $rc. Log: $LOG"
      exit "$rc"
    fi
    echo "Setup's tmux session ended without reporting a status. Log: $LOG"
    exit 1
  fi
  echo "tmux is not installed; running without it (a dropped connection would stop setup)."
fi

touch "$LOG"; chmod 600 "$LOG"

# While setup runs, neither a sleep request nor the power button may suspend
# the server: a logind inhibitor that ends with setup, however it ends.
hold_inhibitor "setup.sh is running"
exec > >(tee -a "$LOG") 2>&1
printf '\n===== ollama1 setup %s =====\n' "$(date -Is)"

# shellcheck source=/dev/null
. /etc/os-release
[ "${VERSION_ID:-}" = "26.04" ] || die "this kit is for Ubuntu 26.04; this is ${PRETTY_NAME:-unknown}"
resolve_settings
prompt_settings
find_disks
derive_hosts
for f in lib/o1common.py lib/setuplib.sh bin/ollama1-gateway systemd/ollama.service config/50-ollama1.rules; do
  [ -f "$KIT/$f" ] || die "the kit is incomplete: $f is missing"
done
[ -n "$OS_DISK" ] || die "no disk with serial $OS_SERIAL (the OS disk)"
[ -n "$MODELS_DISK" ] || die "no disk with serial $MODELS_SERIAL (the models disk)"
[ -n "$HDD1" ] || die "no disk with serial $HDD1_SERIAL"
[ -n "$HDD2" ] || die "no disk with serial $HDD2_SERIAL"
for pair in "$HDD1:$HDD1_SERIAL" "$HDD2:$HDD2_SERIAL" "$MODELS_DISK:$MODELS_SERIAL"; do
  serial_is "${pair%%:*}" "${pair#*:}" || die "serial check failed for ${pair%%:*}; refusing to touch any disk"
done
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
  printf '(/home is copied off %s first, and the copy is checked before that disk is touched.\n' "$HDD1"
  printf ' A mirror already on the mirror disks is reassembled, never wiped.)\n'
fi
# Setup locks SSH to the LAN. A session that comes from outside it is cut off, and so is
# every other connection from there: say so, and go on only on an explicit yes
CLIENT_IP=$(ssh_client_ip)
if ssh_outside_lan "$CLIENT_IP" "$HOME_LAN"; then
  printf '\n%sThis SSH session comes from %s, which is OUTSIDE %s.%s\n' "$R$B" "$CLIENT_IP" "$HOME_LAN" "$N"
  printf 'Setup lets SSH in from %s only (the firewall and sshd). When it reaches that step this\n' "$HOME_LAN"
  printf 'session, and any login from where you are now, stops working. Use the console or a machine on %s afterwards.\n' "$HOME_LAN"
  ask_yes "Type yes to continue anyway: " || { echo "Nothing changed."; exit 1; }
fi
ask_yes "Type yes to go ahead: " || { echo "Nothing changed."; exit 1; }
save_settings

# ---- 1. identity ----------------------------------------------------------------
step "Host name, time zone, boot menu"
if [ "$(hostnamectl --static)" != "$SERVER_NAME" ]; then
  run hostnamectl set-hostname "$SERVER_NAME"
fi
if grep -qE "^127\.0\.1\.1[[:space:]]" /etc/hosts; then
  sed -i -E "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1 $SERVER_NAME/" /etc/hosts
else
  printf '127.0.1.1 %s\n' "$SERVER_NAME" >>/etc/hosts
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
KEYRING=/usr/share/keyrings/cloudflare-main.gpg
key_ok() { # exactly one key, the pinned one
  local f=$1
  [ "$(gpg --show-keys --with-colons "$f" 2>/dev/null | grep -c '^pub:')" = 1 ] || return 1
  [ "$(gpg --show-keys --with-colons "$f" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')" = "$CF_KEY_FPR" ]
}
if [ ! -f "$KEYRING" ] || ! key_ok "$KEYRING"; then
  tmpd=$(mktemp -d)
  run curl -fsSL --proto '=https' "$CF_KEY_URL" -o "$tmpd/key"
  fpr=$(gpg --show-keys --with-colons "$tmpd/key" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
  [ "$fpr" = "$CF_KEY_FPR" ] || { rm -rf "$tmpd"; die "Cloudflare's apt key has fingerprint '$fpr', expected $CF_KEY_FPR. If Cloudflare rotated it, check https://pkg.cloudflare.com and update CF_KEY_FPR."; }
  # Keep only the pinned key, whatever else the download carried.
  gpg --homedir "$tmpd" --batch --quiet --import "$tmpd/key" 2>/dev/null
  gpg --homedir "$tmpd" --batch --export --export-options export-minimal "$CF_KEY_FPR" >"$tmpd/only"
  key_ok "$tmpd/only" || { rm -rf "$tmpd"; die "could not isolate Cloudflare's apt key $CF_KEY_FPR"; }
  install -m 0644 "$tmpd/only" "$KEYRING"
  rm -rf "$tmpd"
fi
ok "Cloudflare apt key $CF_KEY_FPR (only that key)"
echo "deb [signed-by=$KEYRING] https://pkg.cloudflare.com/cloudflared any main" >/etc/apt/sources.list.d/cloudflared.list
run apt-get update -q
run apt-get install -y -q ttyd python3-nacl mdadm rsync zstd nftables ufw gdisk parted curl gnupg unattended-upgrades tmux cloudflared ethtool
# ttyd must never listen on its own; only ollama1-ttyd (a UNIX socket, login) may run.
systemctl disable --now ttyd.service >/dev/null 2>&1 || true
python3 -c 'import nacl.signing' || die "python3-nacl did not install"
ok "ttyd $(ttyd --version 2>&1 | awk '{print $NF}'), cloudflared $(cloudflared --version 2>&1 | awk '{print $3}'), PyNaCl"

# ---- 3. root volume -----------------------------------------------------------
step "Root volume"
grow_root "$VG_RESERVE_GIB"
ok "/ is $(df -h --output=size / | tail -n1 | tr -d ' ') ($(df -h --output=avail / | tail -n1 | tr -d ' ') free); $ROOT_VG_FREE_EXT extents left free in ubuntu-vg"

# ---- 4. /home onto the root filesystem -------------------------------------------
step "/home onto the root filesystem"
KEYS=/home/$ADMIN_USER/.ssh/authorized_keys
if home_on_own_disk; then
  src=$(findmnt -no SOURCE /home)
  hdisk=$(disk_of "$src")
  [ "$hdisk" = "$HDD1" ] || [ "$hdisk" = "$HDD2" ] || die "/home is on $hdisk, not one of the mirror disks; not moving it"
  # du warns (and exits non-zero) on anything unreadable; the total still counts
  need=$( { du -sxm /home 2>/dev/null || true; } | awk '{print $1}' | tail -n1)
  avail=$(df -m --output=avail / | tail -n1 | tr -d ' ')
  [ "${need:-0}" -lt $((avail - 1024)) ] || die "/home needs ${need} MB and / has ${avail} MB free"
  keysum=""
  [ -f "$KEYS" ] && keysum=$(sha256sum "$KEYS" | cut -d' ' -f1)
  ROOTVIEW=/run/ollama1-rootfs
  mkdir -p "$ROOTVIEW"
  mountpoint -q "$ROOTVIEW" || mount --bind / "$ROOTVIEW"   # / alone: the real /home folder under the mount
  mkdir -p "$ROOTVIEW/home"
  # Whatever already sits in the root filesystem's own /home (hidden under
  # the mount) is set aside, not deleted. A copy from an earlier run is
  # recognised by its marker and simply brought up to date.
  if [ ! -f /var/lib/ollama1/home-copy-started ] && [ -n "$(ls -A "$ROOTVIEW/home" 2>/dev/null)" ]; then
    aside="/home.pre-ollama1-$(date +%Y%m%d-%H%M%S)"
    run mv "$ROOTVIEW/home" "$ROOTVIEW$aside"
    mkdir -p "$ROOTVIEW/home"
    note "what was already under the root's own /home is now in $aside"
  fi
  mkdir -p /var/lib/ollama1; touch /var/lib/ollama1/home-copy-started
  home_sync() {
    rsync -aHAX --numeric-ids --delete --exclude=/lost+found /home/ "$ROOTVIEW/home/"
    local diffs
    diffs=$(rsync -aHAXn --numeric-ids --delete --checksum --itemize-changes --exclude=/lost+found /home/ "$ROOTVIEW/home/")
    [ -z "$diffs" ] || { umount "$ROOTVIEW"; die "the copy of /home differs from the original: $diffs"; }
  }
  printf '   $ rsync /home -> root filesystem, then compare\n'
  home_sync
  if [ -n "$keysum" ]; then
    [ "$(sha256sum "$ROOTVIEW$KEYS" | cut -d' ' -f1)" = "$keysum" ] || { umount "$ROOTVIEW"; die "authorized_keys did not copy intact"; }
    ok "copy checked, file by file; $KEYS intact ($(grep -cvE '^[[:space:]]*(#|$)' "$KEYS") key(s))"
  fi
  # Once more, right before the switch, so anything written meanwhile is in.
  # Writes to the old /home after this point (seconds) are not copied.
  printf '   $ rsync /home again, then compare\n'
  home_sync
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
  home_in_fstab && die "/etc/fstab still mounts /home but it isn't mounted; look at it before continuing"
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
usermod -aG o1view o1gw
# The gateway must never read the pairing window (it holds the code).
# SupplementaryGroups= in its unit only adds groups, so an /etc/group
# membership from an older kit is taken away here.
gpasswd -d o1gw o1pair >/dev/null 2>&1 || true
usermod -aG o1view o1admin
usermod -aG o1view,o1pair o1dash
usermod -aG o1view "$ADMIN_USER"
install -d -m 0750 -o ollama -g ollama /var/lib/ollama
ok "ollama, o1gw, o1admin, o1dash, cloudflared (all /usr/sbin/nologin)"

# ---- 6. models disk ---------------------------------------------------------------
step "Models disk ($MODELS_SERIAL)"
if models_done; then
  ok "/srv/models already on $MODELS_DISK"
else
  models_step "$MODELS_DISK" "$MODELS_SERIAL"
  ok "/srv/models: $(df -h --output=size /srv/models | tail -n1 | tr -d ' ')"
fi
chown ollama:ollama /srv/models; chmod 0750 /srv/models

# ---- 7. the mirror ------------------------------------------------------------------
step "Mirror ($HDD1_SERIAL + $HDD2_SERIAL)"
RAID_PENDING=0
if raid_done; then
  ok "/srv/data already on $(raid_find)"
elif home_on_own_disk; then
  die "/home is still mounted from a mirror disk; not wiping"
elif home_in_fstab; then
  die "/etc/fstab still mounts /home; not wiping"
elif [ -z "$(raid_find)" ] && [ -z "$(raid_members_on_disk "$HDD1" "$HDD2")" ] && { dev_busy "$HDD1" || dev_busy "$HDD2"; }; then
  RAID_PENDING=1
  note "a mirror disk is still held by an earlier login that had /home open (the old /home)."
  note "Log out of every SSH session (or reboot), log in again and run setup.sh again: it continues here."
  later "Build the mirror: log out of every session (or reboot), then run sudo ./setup.sh again"
else
  raid_step "$HDD1" "$HDD2" "$HDD1_SERIAL" "$HDD2_SERIAL"
  install -d -m 0700 /srv/data/backups
  ok "/srv/data: $(df -h --output=size /srv/data | tail -n1 | tr -d ' '), RAID1; the first sync runs in the background (cat /proc/mdstat)"
fi

# ---- 8. the kit -------------------------------------------------------------------------
step "Install the kit"
install -d -m 0755 "$LIBDIR" "$LIBDIR/bin" "$LIBDIR/lib"
install -m 0644 "$KIT"/lib/*.py "$LIBDIR/lib/"
install -m 0755 "$KIT"/bin/* "$LIBDIR/bin/"
ln -sfn "$LIBDIR/bin/ollama1-pair" /usr/local/sbin/ollama1-pair
ln -sfn "$LIBDIR/bin/ollama1-cf-access" /usr/local/sbin/ollama1-cf-access
ln -sfn "$LIBDIR/bin/ollama1-lan" /usr/local/sbin/ollama1-lan
ln -sfn "$LIBDIR/bin/ollama1-models" /usr/local/sbin/ollama1-models
ln -sfn "$LIBDIR/bin/ollama1-power" /usr/local/sbin/ollama1-power
ln -sfn "$LIBDIR/bin/ollama1-gpu-tune" /usr/local/sbin/ollama1-gpu-tune
ln -sfn "$LIBDIR/bin/ollama1-dash" /usr/local/bin/ollama1-top
ln -sfn "$LIBDIR/bin/ollama1-fan" /usr/local/bin/ollama1-fan
ln -sfn /opt/ollama/current/bin/ollama /usr/local/bin/ollama
install -m 0644 "$KIT"/systemd/* /etc/systemd/system/
install -d -m 0750 -g polkitd /etc/polkit-1/rules.d 2>/dev/null || install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$KIT/config/50-ollama1.rules" /etc/polkit-1/rules.d/50-ollama1.rules
# The power button sleeps instead of shutting down (pressing it again wakes
# it). HUP makes logind re-read its config; a restart would end sessions.
install -d -m 0755 /etc/systemd/logind.conf.d
if ! cmp -s "$KIT/config/logind-ollama1.conf" /etc/systemd/logind.conf.d/ollama1.conf; then
  install -m 0644 "$KIT/config/logind-ollama1.conf" /etc/systemd/logind.conf.d/ollama1.conf
  systemctl kill -s HUP systemd-logind || note "couldn't ask logind to re-read its settings; the power button change takes effect after a reboot"
fi
# After waking: record the time and check Ollama and the GPU.
install -d -m 0755 /usr/lib/systemd/system-sleep
install -m 0755 "$KIT/config/ollama1-sleep-hook" /usr/lib/systemd/system-sleep/ollama1
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
# once: the panel's prices from before they moved to root's folder, through
# the same checks as a panel request (never a symlink, owned by o1admin)
if [ -e /var/lib/ollama1-admin/tariff.json ] && [ ! -e /var/lib/ollama1/tariff.json ]; then
  "$LIBDIR/bin/ollama1-power" migrate-tariff || note "the panel's old prices weren't moved; set them again in the panel"
fi

LANIF=$(lan_dev)
[ -n "$LANIF" ] || die "no LAN port found (no br0 and no default route)"
if [ "$LANIF" = br0 ]; then
  [ "$(cat /sys/class/net/br0/operstate)" = up ] || note "br0 exists but is not up; not touching it (network settings are not this script's)"
fi
LANIP=$(ip -4 -o addr show dev "$LANIF" | awk '{print $4}' | cut -d/ -f1 | head -n1)
ok "LAN: $LANIF ${LANIP:-no address} (network settings left as they are)"

python3 - "$LANIP" "$SERVER_NAME" "$HOME_LAN" "$CF_ZONE" "$OWNER" "$GW_HOST" "$ADMIN_HOST" <<'PY'
import json, os, sys
path = "/etc/ollama1/config.json"
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)
lan_ip, name, lan, zone, owner, gw, admin = sys.argv[1:8]
if lan_ip:
    cfg["lan_bind"] = lan_ip
cfg.setdefault("lan_mode", False)
cfg["server_name"] = name            # an install from before it was a setting: "ollama1", unchanged
cfg["lan_cidr"] = lan
if zone:
    cfg["cf_zone"] = zone
    cfg.setdefault("hostname_gateway", gw)     # names already in config.json win
    cfg.setdefault("hostname_admin", admin)
if owner:
    cfg["owner_label"] = owner
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=1, sort_keys=True)
os.replace(tmp, path)
PY
chown root:root /etc/ollama1/config.json; chmod 0600 /etc/ollama1/config.json
if ! python3 -c 'import json,sys; sys.exit(0 if json.load(open("/etc/ollama1/config.json")).get("admin_email") else 1)'; then
  printf '   The admin email: the only address allowed into %s.\n   It stays on this server (root-only), never in the repo.\n   Admin email: ' "$ADMIN_HOST"
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

# ---- 9. firewall ----------------------------------------------------------------------
step "Firewall"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw default deny routed >/dev/null
for r in OpenSSH ssh 22 22/tcp; do ufw delete allow "$r" >/dev/null 2>&1 || true; done
run ufw allow in on "$LANIF" from "$HOME_LAN" to any port 22 proto tcp comment 'ollama1: SSH from the home LAN'
if [ "$LANIF" = br0 ]; then
  # Bridged frames (a device on the bridge's other port) don't go through iptables while
  # bridge netfilter is off (60-ollama1-bridge.conf). If something loads
  # br_netfilter anyway, this rule keeps them flowing.
  run ufw route allow in on br0 out on br0 comment 'ollama1: bridged LAN traffic'
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

# ---- 10. SSH --------------------------------------------------------------------------------
step "SSH"
KEY_RE='^[[:space:]]*(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-(ssh-ed25519|ecdsa-sha2))'
# Keys of your own = key lines in authorized_keys other than the setup key.
own_keys() {
  [ -f "$KEYS" ] || { echo 0; return; }
  grep -E "$KEY_RE" "$KEYS" | grep -vcF "$CLAUDE_KEY" || true
}
show_own_keys() { # comment and fingerprint of every key counted as yours
  local line tmp
  tmp=$(mktemp)
  grep -E "$KEY_RE" "$KEYS" | grep -vF "$CLAUDE_KEY" | while IFS= read -r line; do
    printf '%s\n' "$line" >"$tmp"
    printf '      %s\n' "$(ssh-keygen -lf "$tmp" 2>/dev/null || echo "(unreadable key line)")"
  done
  rm -f "$tmp"
}
OWN_KEYS=$(own_keys)
PW_OFF=0
if [ "$OWN_KEYS" -gt 0 ]; then
  if [ -f /etc/ssh/sshd_config.d/10-ollama1.conf ] && grep -qx 'PasswordAuthentication no' /etc/ssh/sshd_config.d/10-ollama1.conf; then
    PW_OFF=1   # confirmed on an earlier run
  else
    printf '   These keys in %s count as yours:\n' "$KEYS"
    show_own_keys
    printf '   After this step only these keys (and the setup key, until you remove it) can log in.\n'
    printf '   Keep this session open until a NEW login with your key has worked.\n'
    if ask_yes "   Are these your keys? Type yes to turn password login off: "; then PW_OFF=1; fi
  fi
fi
DROPIN=/etc/ssh/sshd_config.d/10-ollama1.conf
CLOUDINIT=/etc/ssh/sshd_config.d/50-cloud-init.conf
if [ "$PW_OFF" = 1 ]; then
  pw=$'PasswordAuthentication no\nKbdInteractiveAuthentication no\nAuthenticationMethods publickey'
else
  pw=$'# Password login stays on until '"$KEYS"$'\n# holds a key of your own (besides '"$CLAUDE_KEY"$') and you confirm it. Run setup.sh again.'
fi
new=$(mktemp)
awk -v pw="$pw" -v who="$ADMIN_USER@$HOME_LAN" '{ if ($0 == "@PASSWORD_LINES@") print pw; else if ($0 == "AllowUsers @ADMIN_USER_AT_LAN@") print "AllowUsers " who; else print }' "$KIT/config/10-ollama1-sshd.conf.in" >"$new"
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
if [ "$PW_OFF" = 1 ] && [ -f "$CLOUDINIT" ] && grep -qiE '^[[:space:]]*PasswordAuthentication[[:space:]]+yes' "$CLOUDINIT"; then
  [ -f /root/50-cloud-init.conf.before-ollama1 ] || cp -a "$CLOUDINIT" /root/50-cloud-init.conf.before-ollama1
  sed -i -E 's/^([[:space:]]*PasswordAuthentication[[:space:]]+yes.*)$/# ollama1: password login is off (10-ollama1.conf)\n# \1/I' "$CLOUDINIT"
fi
if ! sshd -t; then
  restore_ssh; rm -rf "$bak"
  die "sshd rejected the new settings; the previous ones are back"
fi
# What sshd will actually do for the admin user coming from the LAN (a made-up
# address inside it stands for the client):
LAN_PROBE=$(python3 -c 'import ipaddress,sys
n = ipaddress.ip_network(sys.argv[1]); print(n[min(20, n.num_addresses - 1)])' "$HOME_LAN")
eff=$(sshd -T -C "user=$ADMIN_USER,host=client.lan,addr=$LAN_PROBE,laddr=${LANIP:-$LAN_PROBE},lport=22" 2>/dev/null || true)
want_pw=yes; [ "$PW_OFF" = 1 ] && want_pw=no
if ! echo "$eff" | grep -qx "permitrootlogin no" || ! echo "$eff" | grep -qx "passwordauthentication $want_pw" \
   || ! echo "$eff" | grep -qix "allowusers $ADMIN_USER@$HOME_LAN" \
   || { [ "$want_pw" = no ] && ! echo "$eff" | grep -qx "kbdinteractiveauthentication no"; }; then
  restore_ssh; rm -rf "$bak"
  die "sshd's effective settings are not what they should be (sshd -T); the previous ones are back"
fi
rm -rf "$bak"
systemctl try-reload-or-restart ssh.service
if [ "$PW_OFF" = 1 ]; then
  ok "passwords off: only $ADMIN_USER, from $HOME_LAN, with a key; no root login"
  note "Keep this session open. From your Mac, open a NEW terminal and check: ssh $ADMIN_USER@${LANIP:-<server-ip>}"
  note "If your agent offers several keys, name yours: ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 $ADMIN_USER@${LANIP:-<server-ip>}"
  later "Before closing this session: check a NEW SSH login with your key works"
elif [ "$OWN_KEYS" -gt 0 ]; then
  note "no root login; only $ADMIN_USER from $HOME_LAN. Password login stays ON (you didn't confirm the keys)"
  later "Confirm your SSH key: run sudo ./setup.sh again and answer yes at the SSH step"
else
  note "no root login; only $ADMIN_USER from $HOME_LAN. Password login stays ON: $KEYS has no key of your own"
  note "(on your Mac: ssh-copy-id $ADMIN_USER@${LANIP:-<server-ip>}, then run setup.sh again)"
  later "Add your Mac's SSH key (ssh-copy-id), then run sudo ./setup.sh again: it switches passwords off"
fi

# ---- 11. Ollama ------------------------------------------------------------------
step "Ollama (verified download, ROCm build)"
run systemctl enable --now ollama1-nft.service
# Ollama's memory cap: all RAM but 8 GiB (MemoryHigh 2 GiB below that), from
# /proc/meminfo. A load too big for the machine is stopped inside Ollama's
# cgroup instead of freezing the server (it happened once, with mmap).
mem_total=$(awk '/^MemTotal:/{print $2 * 1024; exit}' /proc/meminfo)
[ "${mem_total:-0}" -gt $((16 << 30)) ] || die "couldn't read MemTotal from /proc/meminfo (or it is under 16 GiB)"
mem_max=$((mem_total - (8 << 30)))
mem_high=$((mem_max - (2 << 30)))
install -d -m 0755 /etc/systemd/system/ollama.service.d
printf '# ollama1 setup.sh: RAM %s bytes, less 8 GiB\n[Service]\nMemoryMax=%s\nMemoryHigh=%s\nMemorySwapMax=0\n' \
  "$mem_total" "$mem_max" "$mem_high" >/etc/systemd/system/ollama.service.d/10-ollama1-memory.conf
cfg_set_num() { # key integer
  python3 - "$1" "$2" <<'PY'
import json, os, sys
p = "/etc/ollama1/config.json"
c = json.load(open(p))
c[sys.argv[1]] = int(sys.argv[2])
fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(c, f, indent=1, sort_keys=True)
os.replace(p + ".tmp", p)
PY
}
cfg_set_num ollama_memory_max_bytes "$mem_max"
cfg_set_num ollama_memory_high_bytes "$mem_high"
# a controlled RAM test that was cut off may have left its runtime drop-in
rm -f /run/systemd/system/ollama.service.d/50-ollama1-ramtest.conf
systemctl daemon-reload
ok "Ollama's memory cap: MemoryMax $((mem_max >> 30)) GiB, MemoryHigh $((mem_high >> 30)) GiB, no swap"
if ! "$LIBDIR/bin/ollama1-update-ollama" --no-restart; then
  [ -L /opt/ollama/current ] || die "could not install Ollama (see above); nothing is running yet. Run setup.sh again."
  note "the update check failed; keeping the installed version"
fi
run systemctl enable ollama.service
run systemctl restart ollama.service
[ "$(systemctl show -p MemoryMax --value ollama.service)" = "$mem_max" ] \
  || die "ollama.service did not take its memory cap (systemctl show -p MemoryMax ollama.service)"
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:11434/api/version >/dev/null || die "Ollama did not start: journalctl -u ollama"
gpuline=$(journalctl -u ollama -b --no-pager -o cat 2>/dev/null | grep -i 'inference compute' | tail -n1 || true)
if echo "$gpuline" | grep -qi 'rocm'; then
  ok "Ollama $(curl -fsS http://127.0.0.1:11434/api/version | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])') sees the GPU: ${gpuline#*msg=}"
else
  note "Ollama did not report a ROCm GPU yet: ${gpuline:-no 'inference compute' line}. The gateway refuses anything not 100% on the GPU, so nothing runs on the CPU. Check: journalctl -u ollama | grep -i -E 'rocm|amdgpu|gfx'"
  later "Ollama didn't report the GPU at setup time; check journalctl -u ollama"
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
run systemctl enable ollama1-power.service
run systemctl restart ollama1-power.service
run systemctl restart ollama1-nft.service
run systemctl enable ollama1-gateway.service ollama1-admin.service ollama1-ttyd.service
run systemctl restart ollama1-gateway.service ollama1-admin.service ollama1-ttyd.service
systemctl mask getty@tty1.service >/dev/null 2>&1 || true
run systemctl enable ollama1-dash.service
# auto sleep (6b346): the service waits for the owner to turn it on in the app; the cards that
# can wake the server on a magic packet are set up now (a .link file each, and ethtool), quietly
# when there are none
run systemctl enable ollama1-idle.service
run systemctl restart ollama1-idle.service
"$LIBDIR/bin/ollama1-idle" wol-setup || note "couldn't set up wake on a magic packet; auto sleep still works, waking it from the app won't"
systemctl stop getty@tty1.service >/dev/null 2>&1 || true
dash_font_step "$LIBDIR/bin/ollama1-dash" /dev/tty1
run systemctl restart ollama1-dash.service
sleep 2
for s in ollama ollama1-gateway ollama1-admin ollama1-ttyd ollama1-dash ollama1-power ollama1-idle; do
  if systemctl is-active --quiet "$s"; then ok "$s running"; else note "$s is not running: journalctl -u $s"; later "$s did not start: journalctl -u $s"; fi
done
# image and video generation is optional and never installed here (6b356): one line says
# whether the gateway has it, and how to add it
if curl -fsS -m 3 http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
  ok "ComfyUI answers on this machine: the gateway offers image and video generation"
else
  note "image and video generation isn't installed (optional): sudo bash $KIT/tools/install-comfyui.sh"
fi
if id -nG o1gw | tr ' ' '\n' | grep -qx o1pair; then
  die "the gateway's user o1gw is in the o1pair group, so it could read the pairing code; remove it (gpasswd -d o1gw o1pair) and run setup again"
fi
ok "the gateway's user can't read the pairing window (o1gw: $(id -nG o1gw))"

# ---- 14. graphics card tuning (6b361) ---------------------------------------------------------
# OFF unless asked (--gpu-tune or OLLAMA1_GPU_TUNE=1; the choice is saved in setup.env, so a re-run
# keeps it). For an AMD Navi 21 card (RX 6800/6900 series): its highest power limit and the
# memory clock +100 in the driver's units, clamped to what the card reports, checked under load
# (lib/o1gputune.py). The memory clock needs the kernel's overdrive switch: only the overdrive
# bit (0x4000) is added to the amdgpu feature mask the driver runs with now, in a GRUB drop-in,
# so it takes a reboot. --no-gpu-tune (or OLLAMA1_GPU_TUNE=0) puts the card back to stock and
# takes the drop-in out; with neither asked, nothing here changes anything. A safety revert or an admin's "ollama1-gpu-tune off" is kept on a
# re-run; --gpu-tune tries again.
step "Graphics card tuning"
GPU_DROPIN=/etc/default/grub.d/97-amdgpu-overdrive.cfg
TUNE=$LIBDIR/bin/ollama1-gpu-tune
if [ "$GPU_TUNE" = on ]; then
  if od_cfg=$("$TUNE" grub-cfg 2>/dev/null); then
    od_mask=$(printf '%s\n' "$od_cfg" | sed -n 's/.*amdgpu\.ppfeaturemask=\(0x[0-9a-f]*\).*/\1/p')
    if [ "$(cat "$GPU_DROPIN" 2>/dev/null)" != "$od_cfg" ]; then
      printf '%s\n' "$od_cfg" >"$GPU_DROPIN"
      chmod 0644 "$GPU_DROPIN"
      run update-grub
    fi
    grep -q "amdgpu.ppfeaturemask=$od_mask" /boot/grub/grub.cfg \
      || die "/boot/grub/grub.cfg doesn't carry amdgpu.ppfeaturemask=$od_mask after update-grub (see $GPU_DROPIN)"
    ok "kernel overdrive switch: amdgpu.ppfeaturemask=$od_mask (the running mask plus the overdrive bit only)"
    run systemctl enable ollama1-gpu-tune.service ollama1-gpu-tune-check.service
    if [ "$A_GPU_TUNE" = on ]; then tune_how=force-on; else tune_how=on; fi
    "$TUNE" setup "$tune_how" || note "ollama1-gpu-tune stopped (see above); the card is left as it was"
    if ! grep -q "amdgpu.ppfeaturemask=$od_mask" /proc/cmdline; then
      note "the memory clock (and, on most Navi 21 cards, the higher power limit) start after a reboot: the kernel's overdrive switch. The check under load then runs by itself"
      later "Reboot for the graphics card tuning (sudo reboot), then: sudo ollama1-gpu-tune status"
    fi
    ok "the aim is about 5% faster token generation on models that fit the card; only a measurement on your card proves it (sudo ollama1-gpu-tune status). Back to stock: sudo ollama1-gpu-tune off"
  else
    note "$("$TUNE" grub-cfg 2>&1 >/dev/null || true)"
  fi
elif [ "$GPU_TUNE" = default ]; then
  ok "graphics card tuning is off (it is opt-in): for an AMD Navi 21 card, run setup again with --gpu-tune (what it does: the README's \"Graphics card tuning\")"
else
  if systemctl is-enabled --quiet ollama1-gpu-tune.service 2>/dev/null; then
    "$TUNE" setup off || note "ollama1-gpu-tune couldn't put the card back to stock (see above)"
  fi
  systemctl disable ollama1-gpu-tune.service ollama1-gpu-tune-check.service >/dev/null 2>&1 || true
  if [ -f "$GPU_DROPIN" ]; then
    rm -f "$GPU_DROPIN"
    run update-grub
    later "Reboot to switch the kernel's graphics-card overdrive off again (sudo reboot)"
  fi
  ok "off (--no-gpu-tune): the graphics card runs at stock"
fi

# ---- 14b. fans (6b385) ------------------------------------------------------------------------
# ON unless --fans off (OLLAMA1_FANS=0; saved in setup.env): the service puts the graphics card's and the
# case fans at 100% while the server works and for a minute after (lib/o1fan.py). The step also removes
# the old hand-made full-speed-always fan unit (see the README) when it is there.
step "Fans"
"$LIBDIR/bin/ollama1-fan" setup "$FANS" || note "ollama1-fan setup stopped (see above); the fans are left as they were"

# ---- 15. Cloudflare -------------------------------------------------------------------------
step "Cloudflare Tunnel and Access"
cfg_has() { python3 -c 'import json,sys; c=json.load(open("/etc/ollama1/config.json")); sys.exit(0 if all(c.get(k) for k in sys.argv[1:]) else 1)' "$@"; }
cfg_set() { # key value
  python3 - "$1" "$2" <<'PY'
import json, os, sys
p = "/etc/ollama1/config.json"
c = json.load(open(p))
c[sys.argv[1]] = sys.argv[2]
fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(c, f, indent=1, sort_keys=True)
os.replace(p + ".tmp", p)
PY
}
cf_ready() { cfg_has access_team_domain gateway_aud admin_aud admin_email tunnel_id && [ -s /etc/cloudflared/ollama1.json ]; }

# The fallback, without an API token: cloudflared logs in through a browser
# for the tunnel and DNS; Access is clicked in the dashboard (README) and
# its values typed in here.
cf_by_browser() {
  [ -n "$CF_ZONE" ] || die "the Cloudflare step needs your domain: give --zone <your-domain>"
  TUNNEL_NAME=$(cfg_str tunnel_name); TUNNEL_NAME=${TUNNEL_NAME:-$SERVER_NAME}   # config.json's own name wins
  export HOME=/root
  if [ ! -f /root/.cloudflared/cert.pem ]; then
    cat <<EOF

   Cloudflare needs your OK in a browser once:
     1. cloudflared prints a link below. Open it on your Mac.
     2. Log in to Cloudflare, pick $CF_ZONE, click Authorize.
     3. Come back here; it continues by itself.

EOF
    run cloudflared tunnel login
  fi
  chmod 600 /root/.cloudflared/cert.pem
  local tid h
  tid=$(cloudflared tunnel list -o json 2>/dev/null | python3 -c 'import json,sys
for t in json.load(sys.stdin) or []:
    if t.get("name") == sys.argv[1] and not t.get("deleted_at", "").startswith("2"): print(t["id"]); break' "$TUNNEL_NAME" || true)
  if [ -z "$tid" ]; then
    run cloudflared tunnel create "$TUNNEL_NAME"
    tid=$(cloudflared tunnel list -o json | python3 -c 'import json,sys
for t in json.load(sys.stdin) or []:
    if t.get("name") == sys.argv[1]: print(t["id"]); break' "$TUNNEL_NAME")
  fi
  [ -n "$tid" ] || die "could not create or find the tunnel $TUNNEL_NAME"
  install -d -m 0755 /etc/cloudflared
  if [ ! -s /etc/cloudflared/ollama1.json ]; then
    if [ -f "/root/.cloudflared/$tid.json" ]; then
      install -m 0600 "/root/.cloudflared/$tid.json" /etc/cloudflared/ollama1.json
      rm -f "/root/.cloudflared/$tid.json"
    else
      run cloudflared tunnel token --cred-file /etc/cloudflared/ollama1.json "$TUNNEL_NAME"
    fi
  fi
  cfg_set tunnel_id "$tid"
  for h in "$GW_HOST" "$ADMIN_HOST"; do
    if cloudflared tunnel route dns "$TUNNEL_NAME" "$h"; then ok "DNS $h -> tunnel"; else
      note "DNS for $h not set (a record may already exist). In the dashboard: DNS > $h > CNAME $tid.cfargotunnel.com, proxied"
      later "Check the DNS record for $h (CNAME to $tid.cfargotunnel.com)"
    fi
  done
  if ! cfg_has access_team_domain gateway_aud admin_aud; then
    printf '\n   Now set up Access in the dashboard (README: "Without an API token"), then type the values.\n'
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
if not cid.endswith(".access"): sys.exit("the service token Client ID is required; it ends in .access")
p = "/etc/ollama1/config.json"
c = json.load(open(p))
c.update(access_team_domain=team, admin_aud=aaud, gateway_aud=gaud, service_token_client_id=cid)
fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f: json.dump(c, f, indent=1, sort_keys=True)
os.replace(p + ".tmp", p)
PY
  fi
}

# The default: one API token does the tunnel, DNS and Access. It is read
# with read -s (not echoed, not in any history, not in the setup log), goes
# to the helper on a pipe (printf is a shell builtin, so it never shows in a
# process list or a command line), and is cleared here straight after.
cf_by_token() {
  local CF_TOKEN=""
  read -r -s -p "   Cloudflare API token (not shown): " CF_TOKEN </dev/tty
  echo
  if [ -z "$CF_TOKEN" ]; then note "no token given"; return 0; fi
  if printf '%s\n' "$CF_TOKEN" | "$LIBDIR/bin/ollama1-cf-access" --token-stdin; then
    ok "Cloudflare set up through the API"
  else
    note "the API setup stopped (see above); fix it and run setup.sh again, or choose 2"
  fi
  CF_TOKEN=""
  later "Delete the Cloudflare API token now: dash.cloudflare.com > My Profile > API Tokens"
}

if [ "$SKIP_CF" = 1 ]; then
  note "skipped (--skip-cloudflare)"
  later "Cloudflare: run sudo ./setup.sh again without --skip-cloudflare"
else
  if cf_ready; then
    ok "already set up; to check or redo it through the API: sudo ollama1-cf-access"
  else
    cat <<EOF

   Cloudflare: the tunnel, the two DNS names and Access. Choose one:
     1  Paste one Cloudflare API token and setup does all of it (recommended).
        Make the token first: README, "The API token" (6 permissions, 1-day TTL).
     2  No API token: log in with cloudflared in a browser, and click Access
        together in the dashboard (README, "Without an API token")
     3  Later (nothing is reachable from outside until this is done)
EOF
    printf '   1, 2 or 3 [1]: '
    read -r choice </dev/tty
    case "${choice:-1}" in
      1) cf_by_token ;;
      2) cf_by_browser ;;
      *) note "Cloudflare later" ;;
    esac
  fi
  if cf_ready; then
    chown root:root /etc/cloudflared/ollama1.json; chmod 0600 /etc/cloudflared/ollama1.json
    read -r TID TEAM GAUD AAUD < <(python3 -c 'import json, re
c = json.load(open("/etc/ollama1/config.json"))
vals = [c["tunnel_id"], c["access_team_domain"].split(".")[0], c["gateway_aud"], c["admin_aud"]]
assert all(re.match(r"^[A-Za-z0-9-]+$", v) for v in vals), "unexpected characters in the Cloudflare settings"
print(*vals)')
    sed -e "s/@TUNNEL_ID@/$TID/" -e "s/@TEAM_NAME@/$TEAM/g" -e "s/@GATEWAY_AUD@/$GAUD/" -e "s/@ADMIN_AUD@/$AAUD/" \
        -e "s/@GW_HOST@/$GW_HOST/" -e "s/@ADMIN_HOST@/$ADMIN_HOST/" \
      "$KIT/config/cloudflared.yml.in" >/etc/ollama1/cloudflared.yml
    chown root:cloudflared /etc/ollama1/cloudflared.yml; chmod 0640 /etc/ollama1/cloudflared.yml
    ok "tunnel $TID; credential root-only in /etc/cloudflared/ollama1.json"
    run systemctl enable ollama1-tunnel.service
    run systemctl restart ollama1-tunnel.service ollama1-gateway.service ollama1-admin.service
    for _ in $(seq 1 20); do curl -fsS http://127.0.0.1:8439/ready >/dev/null 2>&1 && break; sleep 1; done
    if curl -fsS http://127.0.0.1:8439/ready >/dev/null 2>&1; then ok "tunnel connected"; else
      note "tunnel not connected yet: journalctl -u ollama1-tunnel"; later "Tunnel did not connect: journalctl -u ollama1-tunnel"
    fi
  else
    note "Cloudflare is not fully set up, so the tunnel stays off (nothing is reachable from outside)"
    later "Cloudflare: run sudo ./setup.sh again and paste an API token (or run: sudo ollama1-cf-access)"
  fi
fi

# ---- listeners -----------------------------------------------------------------------------
step "What listens on the network"
# Only sshd may listen beyond loopback (plus the gateway on br0 in LAN mode).
lan_on=0
python3 -c 'import json,sys; sys.exit(0 if json.load(open("/etc/ollama1/config.json")).get("lan_mode") else 1)' && lan_on=1
unit_of_pid() { # pid -> the systemd unit it runs in (from its cgroup), or "-"
  local u
  u=$(sed -n 's#^0::.*/\([^/]*\.service\)$#\1#p' "/proc/$1/cgroup" 2>/dev/null | head -n1)
  echo "${u:--}"
}
exposed_listeners() { # "address unit" for every TCP listener beyond loopback, except sshd's
  local addr proc pid
  ss -Hltnp 2>/dev/null | awk '{print $4 "  " $6}' | while read -r addr proc; do
    case "$addr" in
      127.*|"[::1]":*|"[::ffff:127."*) continue ;;
      *:22) continue ;;
    esac
    if [ "$lan_on" = 1 ] && [ "$addr" = "${LANIP:-x}:8431" ]; then continue; fi
    pid=$(printf '%s' "$proc" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -n1)
    printf '%s %s\n' "$addr" "$( [ -n "$pid" ] && unit_of_pid "$pid" || echo - )"
  done
}
exposed=$(exposed_listeners)
if [ -z "$exposed" ]; then
  ok "nothing listens beyond loopback except sshd$([ "$lan_on" = 1 ] && echo ' and the LAN-mode gateway')"
else
  printf '%s\n' "$exposed" | sed 's/^/      /'
  # Only ollama1's own services are an error here (judged by their systemd
  # unit, not by program name); anything else is reported.
  if printf '%s\n' "$exposed" | awk '{print $2}' | grep -qE '^(ollama|ollama1-[a-z0-9@-]+)\.service$'; then
    die "one of ollama1's services listens beyond loopback (above); the firewall blocks it, but it must not"
  fi
  note "other programs listen beyond loopback (above). The firewall blocks them from outside."
  later "Look at the listeners setup listed under 'What listens on the network'"
fi

# ---- 16. the setup key (only when asked) ----------------------------------------------------
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
  chown "$(stat -c %u:%g "$KEYS")" "$tmp"; chmod "$(stat -c %a "$KEYS")" "$tmp"
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

  Screen:   the dashboard is on the server's monitor (tty1)
  SSH:      ollama1-top     (the same dashboard in a terminal)
  Pair:     sudo ollama1-pair
  Models:   sudo ollama1-models add qwen3:14b   (then: sudo ollama1-models sync, or the panel's
            Update model library button)
  Panel:    https://$ADMIN_HOST
EOF
if [ "${PW_OFF:-0}" = 1 ]; then
  printf '\n  %sBefore you close this session:%s open a NEW terminal on your Mac and log in with
' "$Y$B" "$N"
  printf '  your key (ssh %s@%s). If your agent offers several keys first, name yours:\n' "$ADMIN_USER" "${LANIP:-<server-ip>}"
  printf '    ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 %s@%s\n' "$ADMIN_USER" "${LANIP:-<server-ip>}"
fi
if [ "${#LATER[@]}" -gt 0 ]; then
  printf '\n%sStill to do:%s\n' "$Y$B" "$N"
  for l in "${LATER[@]}"; do printf '  - %s\n' "$l"; done
fi
if [ "$RAID_PENDING" = 1 ]; then exit 3; fi
exit 0
