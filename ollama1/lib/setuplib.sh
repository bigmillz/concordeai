# shellcheck shell=bash
# ollama1 setup: the disk steps, as functions, so tests/test_setuplib.py can
# run them against fake mdadm/blkid/lsblk/... and check what would be wiped.
# Sourced by setup.sh, which defines run, ok, note, die and later. Kept to
# bash 3.2 so the tests also run on a Mac.
#
# Rules these functions keep:
#   - a disk is found by its serial and wiped only through its
#     /dev/disk/by-id/...<serial> path, after an exact serial check;
#   - nothing is wiped that fstab, crypttab or swap uses, or that carries a
#     signature other than the ones this machine is known to have;
#   - there is no mirror (6b400): no RAID is built, assembled or mounted, and
#     the old mirror disks are never looked at, let alone wiped;
#   - a filesystem is made only on a partition this script has just created
#     (a marker in $STATE_DIR says so, so a crash between the partitioning and
#     mkfs is picked up on the next run). A found one without a readable
#     filesystem stops setup instead: that may be damage, and mkfs would
#     destroy what's left;
#   - a disk blkid can't read (any exit status but 0 or 2) stops setup;
#   - fstab never gets a line without a real UUID.

: "${FSTAB:=/etc/fstab}"
: "${CRYPTTAB:=/etc/crypttab}"
: "${SWAPS:=/proc/swaps}"
: "${STATE_DIR:=/var/lib/ollama1}"            # = o1common.Paths.state (a test keeps the two equal)
: "${BACKUP_DIR:=/var/backups/ollama1}"       # = o1common.Paths.backups: the nightly settings backup (6b400)
: "${BYID:=/dev/disk/by-id}"

# ---- the owner's settings -----------------------------------------------------
# Nothing about a particular server is written in setup.sh or the repo: the
# server's name, the admin's user, the LAN, the domain and the disks are
# arguments, else what an earlier run saved, else (for some) detected.
: "${SAVED:=/etc/ollama1/setup.env}"
: "${CONFIG_JSON:=/etc/ollama1/config.json}"
# What the server was called before the name was a setting. An install whose
# config.json has no server_name keeps this name (6b347).
LEGACY_SERVER_NAME=ollama1

valid_name() { [[ "$1" =~ ^[a-z][a-z0-9-]{0,31}$ ]] && [[ "$1" != *- ]]; }
valid_user() { [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; }
valid_lan_shape() { # a network written as its first address and a length, like 10.0.0.0/24
  [[ "$1" =~ ^[0-9.]{7,15}/[0-9]{1,2}$ ]] || return 1
  python3 -c 'import ipaddress,sys; ipaddress.IPv4Network(sys.argv[1], strict=True)' "$1" 2>/dev/null
}
# The LAN is the only place SSH is let in from (the firewall, and AllowUsers): a /8, or a
# network that isn't a private one, would open it to far more than a home or office. Both
# are refused unless --lan-public-ok (LAN_PUBLIC_OK=1) says it is meant.
lan_private() { # a private (RFC 1918) network, /16 or narrower
  python3 -c 'import ipaddress,sys
n = ipaddress.IPv4Network(sys.argv[1], strict=True)
priv = [ipaddress.ip_network(p) for p in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
sys.exit(0 if n.prefixlen >= 16 and any(n.subnet_of(p) for p in priv) else 1)' "$1" 2>/dev/null
}
valid_lan() {
  valid_lan_shape "$1" || return 1
  [ "${LAN_PUBLIC_OK:-0}" = 1 ] && return 0
  lan_private "$1"
}
ssh_client_ip() { # the address this SSH session comes from, if it is one (sudo may hide the variables)
  local ip=""
  if [ -n "${SSH_CONNECTION:-}" ]; then ip=${SSH_CONNECTION%% *}
  elif [ -n "${SSH_CLIENT:-}" ]; then ip=${SSH_CLIENT%% *}
  elif command -v who >/dev/null 2>&1; then ip=$(who am i 2>/dev/null | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -n1); fi
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] && printf '%s' "$ip"
  return 0
}
ssh_outside_lan() { # CLIENT-IP LAN: true if the client is not inside the LAN
  [ -n "$1" ] || return 1
  python3 -c 'import ipaddress,sys
sys.exit(1 if ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network(sys.argv[2]) else 0)' "$1" "$2" 2>/dev/null
}
valid_zone() { [[ "$1" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$ ]]; }
valid_owner() { local rx='^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$'; [[ "$1" =~ $rx ]]; }
valid_tz() { [[ "$1" =~ ^[A-Za-z0-9_+/-]{1,40}$ ]]; }
valid_serial() { [[ "$1" =~ ^[A-Za-z0-9_.-]{4,40}$ ]]; }

gpu_tune_choice() { # FLAG ENV SAVED -> "on", "off" or "default" (6b361): the flag, else the environment
  # (OLLAMA1_GPU_TUNE), else what an earlier run saved, else "default" = nothing was asked: OFF, and
  # setup says the option exists. Fails on a value it doesn't know.
  local v
  for v in "$1" "$2" "$3"; do
    case "$v" in
      "") ;;
      on|1|yes|true) echo on; return 0 ;;
      off|0|no|false) echo off; return 0 ;;
      *) return 1 ;;
    esac
  done
  echo default
}

gpu_clock_choice() { # FLAG SAVED MAX -> whole MHz 0..MAX (6b420): the flag, else what an earlier run saved, else 0 (off).
  # The memory and core clock raises of --gpu-tune are opt-in; a value that is not a whole number in range fails.
  local v max=$3
  for v in "$1" "$2"; do
    [ -n "$v" ] || continue
    [[ "$v" =~ ^[0-9]{1,4}$ ]] || return 1
    [ "$((10#$v))" -le "$max" ] || return 1
    echo "$((10#$v))"; return 0
  done
  echo 0
}

fans_choice() { # FLAG ENV SAVED -> "on" or "off" (6b385): the flag, else the environment (OLLAMA1_FANS), else what an
  # earlier run saved, else "on": the fans-at-full-speed-while-working service is on unless turned off.
  # Fails on a value it doesn't know.
  local v
  for v in "$1" "$2" "$3"; do
    case "$v" in
      "") ;;
      on|1|yes|true) echo on; return 0 ;;
      off|0|no|false) echo off; return 0 ;;
      *) return 1 ;;
    esac
  done
  echo on
}

fans_plan() { # the plan's line for the fans (6b385; the triggers of 6b421)
  if [ "$1" = off ]; then
    printf 'Fans OFF (--fans off): the graphics card and case fans stay automatic (BIOS control). On: --fans on'
  else
    printf 'Fans ON: graphics card and case fans at 100%% while the graphics card is over 50%% busy or the CPU is at 60 C or more, then down to 20%% over 1 minute; a Corsair Hydro liquid cooler on USB is controlled too (installs liquidctl) (ollama1-fan). Off: --fans off'
  fi
}

leds_choice() { # FLAG ENV SAVED -> "on" or "off" (6b395): the flag, else the environment (OLLAMA1_LEDS), else what an
  # earlier run saved, else "off": the lights service installs a package and takes over the case lights, so it is
  # opt-in. Fails on a value it doesn't know.
  local v
  for v in "$1" "$2" "$3"; do
    case "$v" in
      "") ;;
      on|1|yes|true) echo on; return 0 ;;
      off|0|no|false) echo off; return 0 ;;
      *) return 1 ;;
    esac
  done
  echo off
}

leds_plan() { # the plan's line for the lights (6b395; the states of 6b421)
  if [ "$1" = on ]; then
    printf 'Lights ON: installs the openrgb package (apt-get install openrgb); every light white when the graphics card is idle, through yellow and orange to red in 5 s when it works (over 50%% busy), back to white over 1 minute when it stops, dimming to 40%% after 5 idle minutes (ollama1-leds, ollama1-openrgb on 127.0.0.1). Off: --leds off'
  else
    printf 'Lights OFF (default): nothing installed, the lights stay as the board leaves them. On: --leds on'
  fi
}

watchdog_choice() { # FLAG ENV SAVED -> "on" or "off" (6b399): the flag, else the environment (OLLAMA1_WATCHDOG), else what
  # an earlier run saved, else "on": the hardware watchdog resets a server whose system disk has stopped answering.
  # Fails on a value it doesn't know.
  local v
  for v in "$1" "$2" "$3"; do
    case "$v" in
      "") ;;
      on|1|yes|true) echo on; return 0 ;;
      off|0|no|false) echo off; return 0 ;;
      *) return 1 ;;
    esac
  done
  echo on
}

watchdog_plan() { # the plan's line for the hardware watchdog (6b399)
  if [ "$1" = off ]; then
    printf 'Watchdog OFF (--watchdog off): nothing resets the board when the system disk stops answering. On: --watchdog on'
  else
    printf 'Watchdog ON: the chipset timer resets the board (about 60 s) when the system disk stops answering; petted every 10 s only while a real disk probe passes (ollama1-watchdog). Off: --watchdog off'
  fi
}

saved() { # KEY -> its value from the file an earlier run wrote, if any
  [ -r "$SAVED" ] || return 0
  sed -n "s/^$1=//p" "$SAVED" | head -n1
}

cfg_str() { # key -> a string from config.json, if there is one
  [ -r "$CONFIG_JSON" ] || return 0
  python3 -c 'import json,sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print(v if isinstance(v, str) else "")' "$CONFIG_JSON" "$1" 2>/dev/null || true
}

# The server's name: the argument; else the saved one; else config.json's
# server_name; else, if config.json exists without one, the legacy name (an
# install from before the name was a setting). A different name from the
# one an install already has is refused: its tunnel, DNS names and Access
# policies are named after it.
resolve_server_name() { # ARG -> the name on stdout, return 1 if there is none yet
  local arg=$1 have="" n=""
  have=$(cfg_str server_name)
  if [ -z "$have" ] && [ -e "$CONFIG_JSON" ]; then have=$LEGACY_SERVER_NAME; fi
  n=$arg
  [ -n "$n" ] || n=$(saved SERVER_NAME)
  [ -n "$n" ] || n=$have
  [ -n "$n" ] || return 1
  valid_name "$n" || { echo "the server name '$n' is not valid: lowercase letters, digits and hyphens, 1-32 characters, starting with a letter" >&2; return 2; }
  if [ -n "$have" ] && [ "$n" != "$have" ]; then
    echo "this server is already named '$have' (the tunnel, DNS names and Access policies carry it); setup will not rename it" >&2
    return 2
  fi
  printf '%s\n' "$n"
}

disk_by_serial() { # serial -> /dev/<disk> (whole disk), empty if absent
  local d n ser
  [ -n "$1" ] || return 0   # no serial given: no disk (an empty one would match any)
  for d in "${SYS_BLOCK:-/sys/block}"/*; do
    n=${d##*/}
    case $n in loop*|ram*|dm-*|md*|sr*|zram*) continue ;; esac
    ser=$(lsblk -dno SERIAL "/dev/$n" 2>/dev/null | tr -d '[:space:]')
    if [ "$ser" = "$1" ]; then echo "/dev/$n"; return 0; fi
  done
  return 0
}

serial_is() { [ "$(lsblk -dno SERIAL "$1" 2>/dev/null | tr -d '[:space:]')" = "$2" ]; }

byid_for() { # disk serial -> the /dev/disk/by-id path of that whole disk
  local disk=$1 serial=$2 f
  for f in "$BYID"/*_"$serial"; do
    [ -e "$f" ] || continue
    case "$f" in *-part*) continue ;; esac
    if [ "$(readlink -f "$f")" = "$(readlink -f "$disk")" ]; then echo "$f"; return 0; fi
  done
  return 1
}

# ---- which disk holds what (6b374) -------------------------------------------------------------
root_source() { findmnt -no SOURCE /; }
root_disk() { disk_of "$(root_source)"; }   # lsblk -s follows / through LVM to its own disk
root_on_lvm() { # true when / is a logical volume (a plain partition root, as migrate-os leaves it, is not)
  local src; src=$(root_source 2>/dev/null) || return 1
  [ -n "$src" ] || return 1
  lsblk -lsnpo NAME,TYPE "$src" 2>/dev/null | awk '$2=="lvm"{f=1} END{exit !f}'
}
models_done() { # /srv/models is mounted from a partition of the models disk
  mountpoint -q "${MODELS_MNT:-/srv/models}" \
    && [ "$(disk_of "$(findmnt -no SOURCE "${MODELS_MNT:-/srv/models}")")" = "$MODELS_DISK" ]
}
models_on_os_disk() { [ -n "${OS_DISK:-}" ] && [ "$MODELS_DISK" = "$OS_DISK" ]; }

check_disks() { # every refusal before any disk is touched. Needs OS_DISK MODELS_DISK and their serials.
  [ -n "$OS_DISK" ] || die "no disk with serial $OS_SERIAL (the OS disk)"
  [ -n "$MODELS_DISK" ] || die "no disk with serial $MODELS_SERIAL (the models disk)"
  local pair rd
  for pair in "$MODELS_DISK:$MODELS_SERIAL"; do
    serial_is "${pair%%:*}" "${pair#*:}" || die "serial check failed for ${pair%%:*}; refusing to touch any disk"
  done
  rd=$(root_disk)
  if [ "$rd" != "$OS_DISK" ]; then
    if [ "$rd" = "$MODELS_DISK" ] && models_done; then   # a server moved with migrate-os, its setup.env still the old one
      die "/ and /srv/models are both on the disk with serial $MODELS_SERIAL, but the OS disk is set to $OS_SERIAL; refusing to touch any disk. If the system was moved onto that disk, run once: sudo ./setup.sh --os-serial $MODELS_SERIAL --models-serial $MODELS_SERIAL"
    fi
    die "/ is not on the disk with serial $OS_SERIAL; refusing to touch any disk"
  fi
  # The one case where a role disk may be the OS disk: the models share it, because the system was moved there
  # (migrate-os). Only when /srv/models is already mounted from it, as its own partition, and then nothing on
  # that disk is wiped, repartitioned or reformatted.
  if [ "$MODELS_DISK" = "$OS_DISK" ]; then
    models_done || die "$OS_DISK (serial $OS_SERIAL) is both the OS disk and the models disk, but /srv/models is not mounted from it. Setup never wipes the OS disk: mount the models partition at /srv/models, or give another --models-serial"
    [ "$(findmnt -no SOURCE "${MODELS_MNT:-/srv/models}")" != "$(root_source)" ] \
      || die "/srv/models is the root filesystem itself, not a partition of its own; refusing to touch any disk"
  fi
}

show_disks() {
  printf '\n   %-13s %-17s %-32s %s\n' "Device" "Serial" "Model / size" "Role"
  if models_on_os_disk; then   # moved with migrate-os: the system and the models share one disk, which is never wiped
    printf '   %-13s %-17s %-32s %s\n' "${OS_DISK:-MISSING}" "$OS_SERIAL" "$(disk_desc "${OS_DISK:-/dev/null}")" "OS + models: kept, never wiped; $(root_on_lvm && echo 'root LV grows into free space' || echo 'root is a plain partition, left as it is')"
    printf '   %-13s %-17s %-32s %s\n' "(same disk)" "$MODELS_SERIAL" "" "$(models_done && echo 'models: on the OS disk, already set up' || echo 'models: NOT mounted from the OS disk: setup will stop, nothing is wiped')"
  else
    printf '   %-13s %-17s %-32s %s\n' "${OS_DISK:-MISSING}" "$OS_SERIAL" "$(disk_desc "${OS_DISK:-/dev/null}")" "OS: kept; $(root_on_lvm && echo 'root LV grows into free space' || echo 'root is not on LVM, left as it is')"
    printf '   %-13s %-17s %-32s %s\n' "${MODELS_DISK:-MISSING}" "$MODELS_SERIAL" "$(disk_desc "${MODELS_DISK:-/dev/null}")" "$(models_done && echo 'models: already set up' || echo 'WIPED -> ext4 /srv/models')"
  fi
  printf '   no mirror: not used (every other disk is left alone%s)\n' "$( [ -z "${HDD1_SERIAL:-}${HDD2_SERIAL:-}" ] || echo '; the old mirror serials are ignored' )"
}

plan_wipes() { # sets WIPES: the disks that will be erased
  WIPES=()
  if ! models_done; then
    [ "$MODELS_DISK" != "$OS_DISK" ] || die "refusing to wipe the OS disk $OS_DISK"
    WIPES+=("$MODELS_DISK ($MODELS_SERIAL, $(disk_desc "$MODELS_DISK"))")
  fi
  return 0
}

disk_desc() { lsblk -dno MODEL,SIZE "$1" 2>/dev/null | sed 's/  */ /g;s/^ //'; }
disk_of() { # a block device (partition, LVM volume, md array, disk) -> its whole disk /dev/<name>
  # Walk UP the device tree (-s) and take the first "disk". "lsblk -no PKNAME <part>" also lists the
  # partition's children, so on an LVM partition its first line is the partition itself (26.04, util-linux 2.41).
  local d
  d=$(lsblk -lsnpo NAME,TYPE "$1" 2>/dev/null | awk '$2=="disk"{print $1; exit}')
  if [ -n "$d" ]; then echo "$d"; else echo "$1"; fi
}
devs_of() { lsblk -lnpo NAME "$1" 2>/dev/null; }        # the disk and its partitions
first_part() { lsblk -lnpo NAME,TYPE "$1" | awk '$2=="part"{print $1; exit}'; }
dev_busy() { # true if the kernel holds the device (mounted, even lazily; md member; LVM)
  python3 -c 'import os,sys; os.close(os.open(sys.argv[1], os.O_RDONLY | os.O_EXCL))' "$1" 2>/dev/null && return 1
  return 0
}
probe() { # device key -> value, bypassing blkid's cache. Exit 2 = no such value;
  # anything else (a read error, say) is reported and fails.
  local out rc=0
  out=$(blkid -p -o value -s "$2" "$1" 2>/dev/null) || rc=$?
  case $rc in
    0) printf '%s\n' "$out" ;;
    2) ;;
    *) echo "blkid could not read $1 (exit $rc)" >&2; return 3 ;;
  esac
}
fs_type() { probe "$1" TYPE; }
BLKID_FAIL="blkid could not read a disk (above); a read error may mean a failing disk. Nothing was changed"
fsck_hint() { # device -> why setup won't format it, and what to try
  die "$1 has no readable filesystem, and setup didn't create it, so it won't format it (that could destroy data). If it is damaged: 'mke2fs -n $1' lists the backup superblocks (it changes nothing), then 'e2fsck -b <backup> $1'. If it really is empty and should be formatted, run: sudo touch $2 and run setup again"
}

vg_free_extents() { # free extents in ubuntu-vg as a plain integer, or fail
  local n
  n=$(vgs --noheadings -o vg_free_count ubuntu-vg 2>/dev/null | tr -d '[:space:]')
  case "$n" in ''|*[!0-9]*) return 1 ;; esac
  echo "$n"
}

hold_inhibitor() { # why: hold off sleep and the power button until this shell is gone.
  # The holder watches this shell's pid and exits within 2 s of it ending,
  # however it ends (even SIGKILL), and the inhibitor goes with it. (A pipe
  # wouldn't do: every command setup runs would inherit its write end.) It
  # never inherits setup's lock (fd 9).
  command -v systemd-inhibit >/dev/null 2>&1 || return 0
  # shellcheck disable=SC2016  # $1 is the inner shell's, on purpose
  systemd-inhibit --what=sleep:handle-power-key --mode=block --who=ollama1 --why="$1" \
    sh -c 'while kill -0 "$1" 2>/dev/null; do sleep 2; done' sh "$$" </dev/null >/dev/null 2>&1 9>&- &
}

vg_extent_bytes() { # ubuntu-vg's extent size in bytes, or fail
  local n
  n=$(vgs --noheadings --units b --nosuffix -o vg_extent_size ubuntu-vg 2>/dev/null | tr -d '[:space:]')
  case "$n" in ''|*[!0-9]*|0) return 1 ;; esac
  echo "$n"
}

vg_keep_extents() { # GiB -> extents of ubuntu-vg to leave free (rounded up), or fail
  local e
  [ "${1:-0}" -gt 0 ] || { echo 0; return 0; }   # no reserve: no extent size needed
  e=$(vg_extent_bytes) || return 1
  echo $(( (${1:-0} * 1073741824 + e - 1) / e ))
}

grow_root_if_lvm() { # reserve GiB: step 3. A root that is a plain partition (a server moved with migrate-os) is left
  # exactly as it is: no volume group to grow, and no partition of an in-use root is ever touched.
  if root_on_lvm; then
    grow_root "$@"
  else
    ROOT_VG_FREE_EXT=0
    note "/ is not on LVM, so there is no root volume to grow; left as it is"
  fi
}

grow_root() { # reserve GiB: grow ubuntu-lv and its filesystem (online) into ubuntu-vg's
  # free space, leaving the reserve free. Nothing happens if the free space is
  # already at or under the reserve. Sets ROOT_VG_FREE_EXT.
  local free keep
  free=$(vg_free_extents) || die "couldn't read the free space in ubuntu-vg (vgs -o vg_free_count ubuntu-vg)"
  keep=$(vg_keep_extents "${1:-0}") || die "couldn't read ubuntu-vg's extent size (vgs -o vg_extent_size ubuntu-vg)"
  if [ "$free" -gt "$keep" ]; then
    run lvextend -r -l "+$((free - keep))" /dev/ubuntu-vg/ubuntu-lv
    free=$(vg_free_extents) || die "couldn't read the free space in ubuntu-vg after growing it"
    [ "$free" = "$keep" ] || die "ubuntu-vg has $free free extents after lvextend, not $keep"
  fi
  # shellcheck disable=SC2034  # read by setup.sh
  ROOT_VG_FREE_EXT=$free
}

set_fstab() { # mountpoint "UUID=<uuid> <mountpoint> ..." : replaces that mountpoint's active line
  local mnt=$1 line=$2 tmp
  [[ "$line" =~ ^UUID=[0-9A-Fa-f-]{8,}[[:space:]] ]] || die "refusing to write an fstab line without a UUID: '$line'"
  tmp="$FSTAB.ollama1.new"
  awk -v m="$mnt" '!($2 == m && $0 !~ /^[[:space:]]*#/)' "$FSTAB" >"$tmp"
  printf '%s\n' "$line" >>"$tmp"
  mv "$tmp" "$FSTAB"
  command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload 2>/dev/null || true
}

referenced() { # disk -> true (and prints what) if fstab, crypttab or swap uses it or a partition
  local disk=$1 dev key val found=1 ids=()
  for dev in $(devs_of "$disk"); do
    ids+=("$dev" "/dev/${dev##*/}")
    for key in UUID PARTUUID LABEL PARTLABEL; do
      val=$(probe "$dev" "$key") || die "$BLKID_FAIL"
      [ -n "$val" ] && ids+=("$key=$val" "/dev/disk/by-$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')/$val")
    done
  done
  for f in "$FSTAB" "$CRYPTTAB"; do
    [ -f "$f" ] || continue
    for val in "${ids[@]}"; do
      if grep -vE '^[[:space:]]*(#|$)' "$f" | awk '{print $1; print $2}' | grep -qxF -- "$val"; then
        echo "$f uses $val"; found=0
      fi
    done
  done
  if [ -f "$SWAPS" ]; then
    for dev in $(devs_of "$disk"); do
      if awk 'NR>1{print $1}' "$SWAPS" | grep -qxF -- "$dev"; then echo "swap is on $dev"; found=0; fi
    done
  fi
  return $found
}

signatures_ok() { # disk allowed-types... -> true if every signature on it is expected
  local disk=$1 dev t bad=0
  shift
  for dev in $(devs_of "$disk"); do
    t=$(fs_type "$dev") || die "$BLKID_FAIL"
    [ -z "$t" ] && continue
    case " $* " in *" $t "*) ;; *) echo "$dev carries '$t'"; bad=1 ;; esac
  done
  return $bad
}

# The dashboard's console font (6b359). The monitor is read from across a room,
# so the dashboard wants a big font (about 120 columns: 16x32 on a 1080p
# screen), and the console's own small one gives 240. $1 the dashboard
# program (its --set-font picks the font, o1font), $2 the tty. Nothing here
# can leave the screen without a display: a font that won't load is a note,
# and the screen keeps the one it has.
# Opt out: OLLAMA1_DASH_FONT=off (or setup's --no-console-font) leaves the
# font as it is, and drops the flag file that tells o1font to load nothing at
# each start; running setup again without it brings the big font back.
: "${DASH_FONT_OFF:=/etc/ollama1/dash-font.off}"
dash_font_step() {
  local dash=$1 tty=${2:-/dev/tty1} out
  if [ "${OLLAMA1_DASH_FONT:-on}" = off ]; then
    mkdir -p "$(dirname "$DASH_FONT_OFF")"
    : >"$DASH_FONT_OFF"
    note "console font: left as it is ($DASH_FONT_OFF). Delete that file and run setup again to get the big one"
    return 0
  fi
  rm -f "$DASH_FONT_OFF"
  # console-setup-linux: the Terminus fonts as /usr/share/consolefonts/*.psf.gz.
  # console-terminus: the ter-v fonts, where the distribution has the package.
  run apt-get install -y -q console-setup-linux || note "console-setup-linux did not install; the dashboard uses the console fonts that are there"
  apt-get install -y -q console-terminus >/dev/null 2>&1 || note "console-terminus isn't available here; using the fonts that are"
  if out=$("$dash" --set-font "$tty" 2>&1); then
    ok "console font: $out"
  else
    note "couldn't load a console font ($out); the screen keeps the one it has"
  fi
}

# What the server's monitor shows (6b380): text, graphic or auto (the default: the graphical
# panel when a monitor is connected). dash_mode_choice FLAG ENV SAVED -> the word, or fails on one
# it doesn't know; dash_mode_step writes it where the dashboard reads it.
: "${DASH_MODE_FILE:=/etc/ollama1/dash-mode}"
dash_mode_choice() {
  local v
  for v in "$1" "$2" "$3"; do
    case "$v" in
      "") ;;
      text|graphic|auto) echo "$v"; return 0 ;;
      *) return 1 ;;
    esac
  done
  echo auto
}
dash_mode_step() {
  mkdir -p "$(dirname "$DASH_MODE_FILE")"
  printf '%s\n' "${DASH:-auto}" >"$DASH_MODE_FILE"
  chmod 0644 "$DASH_MODE_FILE"
  ok "monitor: ${DASH:-auto} (text, graphic or auto: setup.sh --dash)"
}

wait_for() { local _; for _ in $(seq 1 50); do [ -e "$1" ] && return 0; sleep 0.2; done; return 1; }

# The models disk. $1 disk, $2 serial.
models_step() {
  local disk=$1 serial=$2 part byid uuid
  [ -z "${OS_DISK:-}" ] || [ "$disk" != "$OS_DISK" ] || die "$disk is the OS disk; not wiping"
  part=$(first_part "$disk")
  local label=""
  [ -n "$part" ] && { label=$(probe "$part" LABEL) || die "$BLKID_FAIL"; }
  if [ "$label" = "o1models" ]; then
    note "found an earlier o1models filesystem on $part; keeping it"
  else
    serial_is "$disk" "$serial" || die "serial check failed for $disk (expected $serial)"
    byid=$(byid_for "$disk" "$serial") || die "no /dev/disk/by-id path for $serial"
    for part in $(devs_of "$disk"); do
      findmnt -S "$part" >/dev/null 2>&1 && die "$part is mounted; not wiping"
    done
    dev_busy "$disk" && die "$disk is in use; not wiping"
    referenced "$disk" && die "$disk is still used by fstab, crypttab or swap; not wiping"
    signatures_ok "$disk" ext4 || die "$disk carries something setup doesn't expect (above); not wiping"
    mkdir -p "$STATE_DIR"; touch "$STATE_DIR/models.mkfs-pending"
    run wipefs -a "$byid"
    run sgdisk --zap-all "$byid"
    run sgdisk -n1:1MiB:0 -t1:8300 -c1:ollama1-models "$byid"
    partprobe "$byid" 2>/dev/null || true
    udevadm settle 2>/dev/null || true
    wait_for "$byid-part1" || die "no partition appeared on $byid"
    part="$byid-part1"
  fi
  local t
  t=$(fs_type "$part") || die "$BLKID_FAIL"
  if [ -z "$t" ]; then
    [ -f "$STATE_DIR/models.mkfs-pending" ] || fsck_hint "$part" "$STATE_DIR/models.mkfs-pending"
    run mkfs.ext4 -F -q -L o1models -m 1 "$part"
  elif [ "$t" != ext4 ]; then
    die "$part holds '$t', not ext4; not touching it"
  fi
  rm -f "$STATE_DIR/models.mkfs-pending"
  uuid=$(probe "$part" UUID) || die "$BLKID_FAIL"
  [ -n "$uuid" ] || die "no filesystem UUID on $part; fstab left as it was"
  mkdir -p "${MODELS_MNT:-/srv/models}"
  set_fstab "${MODELS_MNT:-/srv/models}" "UUID=$uuid ${MODELS_MNT:-/srv/models} ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2"
  mountpoint -q "${MODELS_MNT:-/srv/models}" || run mount "${MODELS_MNT:-/srv/models}"
}
