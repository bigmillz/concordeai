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
#   - a mirror already on the disks is assembled, never wiped;
#   - a filesystem is made only on a partition or array this script has
#     just created (a marker in $STATE_DIR says so, so a crash between
#     mdadm --create and mkfs is picked up on the next run). A found or
#     reassembled one without a readable filesystem stops setup instead:
#     that may be damage, and mkfs would destroy what's left;
#   - a disk blkid can't read (any exit status but 0 or 2) stops setup;
#   - fstab never gets a line without a real UUID.

: "${FSTAB:=/etc/fstab}"
: "${CRYPTTAB:=/etc/crypttab}"
: "${SWAPS:=/proc/swaps}"
: "${MDADM_CONF:=/etc/mdadm/mdadm.conf}"
: "${STATE_DIR:=/var/lib/ollama1}"
: "${BYID:=/dev/disk/by-id}"
: "${MD_DEV:=/dev/md/o1data}"
: "${MD_NAME:=ollama1:data}"

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

md_saved_uuid() { cat "$STATE_DIR/raid.uuid" 2>/dev/null || true; }

md_line_is_ours() { # an "ARRAY ..." line from mdadm -> true if it is the ollama1 mirror
  local line=$1 saved
  saved=$(md_saved_uuid)
  [[ " $line " == *" name=$MD_NAME "* ]] && return 0
  [ -n "$saved" ] && [[ " $line " == *" UUID=$saved "* ]] && return 0
  return 1
}

raid_find() { # the running mirror's device, if assembled
  local line
  while IFS= read -r line; do
    case "$line" in ARRAY*) md_line_is_ours "$line" && { echo "$line" | awk '{print $2}'; return 0; } ;; esac
  done < <(mdadm --detail --scan 2>/dev/null || true)
  return 0
}

raid_members_on_disk() { # the partitions of our mirror found on these disks (not assembled)
  local disk dev line
  for disk in "$@"; do
    for dev in $(devs_of "$disk"); do
      line=$(mdadm --examine --brief "$dev" 2>/dev/null | grep '^ARRAY' | head -n1 || true)
      [ -n "$line" ] && md_line_is_ours "$line" && echo "$dev"
    done
  done
  return 0
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

# The mirror. $1 $2 disks, $3 $4 their serials. The caller has made sure
# /home no longer lives on either.
raid_step() {
  local d1=$1 d2=$2 s1=$3 s2=$4 md members b1 b2 b d p t uuid
  md=$(raid_find)
  if [ -z "$md" ]; then
    members=()
    while IFS= read -r p; do [ -n "$p" ] && members+=("$p"); done < <(raid_members_on_disk "$d1" "$d2")
    if [ "${#members[@]}" -gt 0 ]; then
      note "the mirror is already on the disks (${members[*]}); assembling it, nothing is wiped"
      run mdadm --assemble --run "$MD_DEV" "${members[@]}"
      md=$MD_DEV
    fi
  fi
  if [ -z "$md" ]; then
    serial_is "$d1" "$s1" || die "serial check failed for $d1 (expected $s1)"
    serial_is "$d2" "$s2" || die "serial check failed for $d2 (expected $s2)"
    b1=$(byid_for "$d1" "$s1") || die "no /dev/disk/by-id path for $s1"
    b2=$(byid_for "$d2" "$s2") || die "no /dev/disk/by-id path for $s2"
    for d in "$d1" "$d2"; do
      for p in $(devs_of "$d"); do findmnt -S "$p" >/dev/null 2>&1 && die "$p is mounted; not wiping"; done
      referenced "$d" && die "$d is still used by fstab, crypttab or swap; not wiping"
      signatures_ok "$d" ext4 || die "$d carries something setup doesn't expect (above); not wiping"
    done
    for b in "$b1" "$b2"; do
      run wipefs -a "$b"
      run sgdisk --zap-all "$b"
      # 100 MiB left free at the end, so a slightly smaller replacement disk fits
      run sgdisk -n1:1MiB:-100MiB -t1:FD00 -c1:ollama1-data "$b"
    done
    partprobe "$b1" "$b2" 2>/dev/null || true
    udevadm settle 2>/dev/null || true
    wait_for "$b1-part1" || die "no partition appeared on $b1"
    wait_for "$b2-part1" || die "no partition appeared on $b2"
    mkdir -p "$STATE_DIR"; touch "$STATE_DIR/raid.mkfs-pending"   # this run makes the array, so it may format it
    run mdadm --create "$MD_DEV" --run --level=1 --raid-devices=2 --metadata=1.2 \
      --bitmap=internal --homehost="${MD_NAME%%:*}" --name="${MD_NAME#*:}" "$b1-part1" "$b2-part1"
    udevadm settle 2>/dev/null || true
    md=$MD_DEV
  fi
  mkdir -p "$STATE_DIR"
  mdadm --detail --export "$md" 2>/dev/null | sed -n 's/^MD_UUID=//p' >"$STATE_DIR/raid.uuid" || true
  t=$(fs_type "$md") || die "$BLKID_FAIL"
  if [ -z "$t" ]; then
    [ -f "$STATE_DIR/raid.mkfs-pending" ] || fsck_hint "$md" "$STATE_DIR/raid.mkfs-pending"
    run mkfs.ext4 -F -q -L o1data -m 0 -E lazy_itable_init=1,lazy_journal_init=1 "$md"
  elif [ "$t" != ext4 ]; then
    die "$md holds '$t', not ext4; not touching it"
  fi
  rm -f "$STATE_DIR/raid.mkfs-pending"
  touch "$MDADM_CONF"
  local saved conf_line pats
  saved=$(md_saved_uuid)
  conf_line=$(mdadm --detail --brief "$md")
  pats=(-e "name=$MD_NAME")
  [ -n "$saved" ] && pats+=(-e "UUID=$saved")
  grep -v "${pats[@]}" "$MDADM_CONF" >"$MDADM_CONF.new" || true
  printf '%s\n' "$conf_line" >>"$MDADM_CONF.new"
  mv "$MDADM_CONF.new" "$MDADM_CONF"
  run update-initramfs -u
  uuid=$(probe "$md" UUID) || die "$BLKID_FAIL"
  [ -n "$uuid" ] || die "no filesystem UUID on $md; fstab left as it was"
  mkdir -p "${DATA_MNT:-/srv/data}"
  set_fstab "${DATA_MNT:-/srv/data}" "UUID=$uuid ${DATA_MNT:-/srv/data} ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2"
  mountpoint -q "${DATA_MNT:-/srv/data}" || run mount "${DATA_MNT:-/srv/data}"
}
