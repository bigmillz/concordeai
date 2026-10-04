#!/usr/bin/env bash
# remove-raid.sh: take the old RAID1 mirror (/srv/data) off a server that still has one (6b400).
#
# The kit no longer builds a mirror: it ties up the disks with checks and rebuilds and nothing needs it.
# This is the teardown for a server that was set up with one.
#
#   sudo bash remove-raid.sh                          a DRY RUN: prints what is on the array and exactly what it would do.
#                                                     Changes nothing (mdadm needs root to describe the array).
#   sudo bash remove-raid.sh --yes-erase-the-mirror   does it, after you type  yes  on the terminal (/dev/tty).
#                                                     It will not run without a terminal.
#   --hdd1-serial S --hdd2-serial S                   the two mirror disks, by serial (default: HDD1_SERIAL and
#                                                     HDD2_SERIAL in /etc/ollama1/setup.env). The array's members must
#                                                     be exactly these two disks, or nothing happens.
#   --keep-going                                      go on although /srv/data holds things this kit did not put there
#                                                     (or a system move that is not finished): the names are printed.
#
# What it does, in this order, and stops at the first thing that is wrong:
#   1. stops the services that use /srv/data (the nightly settings backup)
#   2. copies the settings backups (/srv/data/backups/ollama1) to /var/backups/ollama1 when they are small
#   3. unmounts /srv/data
#   4. mdadm --stop the array
#   5. mdadm --zero-superblock on each member partition (after reading the disk's serial again)
#   6. comments out the /srv/data line of /etc/fstab (a timestamped copy of fstab is kept)
#   7. removes the array's ARRAY line from /etc/mdadm/mdadm.conf (a timestamped copy is kept)
#   8. turns off the monthly array check (the mdcheck timers and the cron job), unless another array is left
#   9. update-initramfs -u
# The two disks keep their partition tables and their partitions: they are only no longer an array. What to do
# with them is yours to decide later (the last lines of the output say how to wipe them). It never touches an
# NVMe drive, and it refuses when the array's members are not exactly the two disks named.
# The log of a real run is /var/log/ollama1-remove-raid.log.
set -euo pipefail

# Tests only: every file under $R, fake programs on PATH, the terminal is the file in $O1_RAID_TTY.
R=${O1_RAID_TESTROOT:-}
MOUNT=/srv/data
DATA=$R$MOUNT
FSTAB=$R/etc/fstab
MDCONF=$R/etc/mdadm/mdadm.conf
CRON=$R/etc/cron.d/mdadm
SETUP_ENV=$R/etc/ollama1/setup.env
LOGFILE=$R/var/log/ollama1-remove-raid.log
BACKUPS_TO=$R/var/backups/ollama1
TTY=${O1_RAID_TTY:-/dev/tty}
[ -n "$R" ] || TTY=/dev/tty
BACKUP_MAX_KB=262144                                     # 256 MiB: the settings backups are small; a bigger folder is not copied
[ -z "$R" ] || BACKUP_MAX_KB=${O1_RAID_BACKUP_MAX_KB:-$BACKUP_MAX_KB}
ALLOWED=(backups lost+found models-parked migrate-os.status migrate-os.log o1migrate old-drive-boot-files-undo.txt)
SERVICES=(ollama1-backup.timer ollama1-backup.service)
CHECK_TIMERS=(mdcheck_start.timer mdcheck_continue.timer)

YES=0; KEEP=0; S1=""; S2=""
die() { echo "STOPPED: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { sed -n '2,/^set -euo/{/^#/s/^# \{0,1\}//p;}' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes-erase-the-mirror) YES=1 ;;
    --keep-going) KEEP=1 ;;
    --hdd1-serial) [ $# -ge 2 ] || die "--hdd1-serial takes a serial"; S1=$2; shift ;;
    --hdd2-serial) [ $# -ge 2 ] || die "--hdd2-serial takes a serial"; S2=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

valid_serial() { [[ "$1" =~ ^[A-Za-z0-9_.-]{4,40}$ ]]; }
env_get() { [ -r "$SETUP_ENV" ] && sed -n "s/^$1=//p" "$SETUP_ENV" | head -n1 || true; }

# ---- 0. who, and which disks -------------------------------------------------------------------
if [ "$YES" = 1 ] && [ -z "$R" ] && [ "$(id -u)" -ne 0 ]; then die "run it with sudo"; fi
[ -n "$S1" ] || S1=$(env_get HDD1_SERIAL)
[ -n "$S2" ] || S2=$(env_get HDD2_SERIAL)
[ -n "$S1" ] && [ -n "$S2" ] || die "the two mirror disks' serials are not known: give --hdd1-serial and --hdd2-serial (lsblk -d -o NAME,SIZE,MODEL,SERIAL), or put HDD1_SERIAL and HDD2_SERIAL in /etc/ollama1/setup.env"
valid_serial "$S1" && valid_serial "$S2" || die "a serial must be 4 to 40 letters, digits, . _ -"
[ "$S1" != "$S2" ] || die "the two serials are the same"
OS_SERIAL=$(env_get OS_SERIAL); MODELS_SERIAL=$(env_get MODELS_SERIAL)
for s in "$S1" "$S2"; do
  if [ "$s" = "$OS_SERIAL" ] || [ "$s" = "$MODELS_SERIAL" ]; then die "serial $s is the OS or models disk in setup.env, not a mirror disk; refusing"; fi
done

disk_by_serial() { # serial -> /dev/<name>, empty when absent (an exact match, one disk)
  lsblk -dno NAME,SERIAL 2>/dev/null | awk -v s="$1" '$2 == s { print "/dev/" $1 }'
}
disk_serial() { lsblk -dno SERIAL "$1" 2>/dev/null | tr -d '[:space:]'; }
disk_of() { # a partition or device -> the whole disk /dev/<name>
  lsblk -lsnpo NAME,TYPE "$1" 2>/dev/null | awk '$2 == "disk" { print $1; exit }'
}
is_nvme() { case "$1" in *nvme*) return 0 ;; esac; return 1; }

D1=$(disk_by_serial "$S1"); D2=$(disk_by_serial "$S2")
[ -n "$D1" ] || die "no disk has the serial $S1"
[ -n "$D2" ] || die "no disk has the serial $S2"
[ "$(printf '%s\n' "$D1" | wc -l | tr -d ' ')" = 1 ] && [ "$(printf '%s\n' "$D2" | wc -l | tr -d ' ')" = 1 ] || die "a serial matches more than one disk"
[ "$D1" != "$D2" ] || die "both serials are the same disk"
if is_nvme "$D1" || is_nvme "$D2"; then die "$D1 / $D2 is an NVMe drive; this tool never touches an NVMe"; fi

# ---- 1. the array --------------------------------------------------------------------------------
SRC=$(findmnt -no SOURCE "$MOUNT" 2>/dev/null | head -n1 || true)
MD=""
if [ -n "$SRC" ]; then
  base=$(basename "$(realpath "$SRC" 2>/dev/null || readlink -f "$SRC" 2>/dev/null || echo "$SRC")")
  case "$base" in md[0-9]*) MD=/dev/$base ;; *) die "$MOUNT is mounted from $SRC, which is not an md array; refusing" ;; esac
fi
if [ -z "$MD" ]; then
  MD=$(mdadm --detail --scan 2>/dev/null | awk '/name=[^ ]*:data( |$)/ { print $2; exit }' || true)
fi
FSTAB_LINES=$(awk '$0 !~ /^[[:space:]]*#/ && $2 == "'"$MOUNT"'"' "$FSTAB" 2>/dev/null || true)
CONF_LINES=$( { grep -E '^ARRAY' "$MDCONF" 2>/dev/null || true; } )
if [ -z "$MD" ]; then
  say "No RAID array is running here (nothing is mounted at $MOUNT from an md device, and none is found)."
  say "fstab lines for $MOUNT: ${FSTAB_LINES:-none}"
  say "ARRAY lines in $MDCONF: ${CONF_LINES:-none}"
  say "Nothing to remove."
  exit 0
fi
is_nvme "$MD" && die "$MD looks like an NVMe device; refusing"
EXPORT=$(mdadm --detail --export "$MD" 2>/dev/null) || die "mdadm cannot describe $MD"
MD_UUID=$(printf '%s\n' "$EXPORT" | sed -n 's/^MD_UUID=//p' | head -n1)
MD_NAME=$(printf '%s\n' "$EXPORT" | sed -n 's/^MD_NAME=//p' | head -n1)
MEMBERS=$(printf '%s\n' "$EXPORT" | sed -n 's/^MD_DEVICE_[^=]*_DEV=//p' | sort)
[ -n "$MEMBERS" ] || die "mdadm lists no member devices for $MD"

# the members must be exactly the two named disks, and nothing else
MEMBER_DISKS=""
for m in $MEMBERS; do
  is_nvme "$m" && die "member $m is an NVMe device; refusing"
  dk=$(disk_of "$m")
  [ -n "$dk" ] || die "cannot tell which disk $m is on"
  is_nvme "$dk" && die "member $m is on $dk, an NVMe drive; refusing"
  if [ "$dk" != "$D1" ] && [ "$dk" != "$D2" ]; then
    die "member $m is on $dk, which is not one of the two mirror disks ($D1 serial $S1, $D2 serial $S2); refusing"
  fi
  MEMBER_DISKS="$MEMBER_DISKS $dk"
done
for dk in $D1 $D2; do
  case " $MEMBER_DISKS " in *" $dk "*) ;; *) die "no member of $MD is on $dk (serial $(disk_serial "$dk")): the members are not exactly the two disks; refusing" ;; esac
done

# ---- 2. what is on it --------------------------------------------------------------------------------
UNKNOWN=()
NAMES=()
if [ -d "$DATA" ]; then
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    NAMES+=("$n")
    ok=0; for a in "${ALLOWED[@]}"; do [ "$n" = "$a" ] && ok=1; done
    [ "$ok" = 1 ] || UNKNOWN+=("$n")
  done < <(ls -A "$DATA" 2>/dev/null)
fi
UNFINISHED=""
if [ -f "$DATA/o1migrate/state.json" ] && ! grep -q '"finished"' "$DATA/o1migrate/state.json"; then
  UNFINISHED="a system move (migrate-os) is not finished: its state is on the array ($MOUNT/o1migrate)"
fi

say "The array: $MD  (name ${MD_NAME:-?}, uuid ${MD_UUID:-?})"
say "Members:"
for m in $MEMBERS; do say "   $m   on $(disk_of "$m"), serial $(disk_serial "$(disk_of "$m")")"; done
say "Mounted: $(findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS "$MOUNT" 2>/dev/null | head -n1 || echo 'no')"
say "State:   $(grep -A2 "^$(basename "$MD") " /proc/mdstat 2>/dev/null | tr '\n' ' ' | cut -c1-160 || true)"
say "On it ($MOUNT, top level):"
if [ "${#NAMES[@]}" = 0 ]; then say "   (empty)"; fi
for n in ${NAMES[@]+"${NAMES[@]}"}; do
  sz=$(du -sh -- "$DATA/$n" 2>/dev/null | awk '{print $1}')
  flag=""
  if [ "${#UNKNOWN[@]}" -gt 0 ]; then for u in "${UNKNOWN[@]}"; do [ "$u" = "$n" ] && flag="   <- NOT something this kit put here"; done; fi
  say "   ${sz:-?}	$n$flag"
done
say "fstab:        ${FSTAB_LINES:-no active line for $MOUNT}"
say "mdadm.conf:   $( [ -n "$CONF_LINES" ] && printf '%s' "$CONF_LINES" | tr '\n' ' ' || echo 'no ARRAY line')"

PROBLEMS=()
if [ "${#UNKNOWN[@]}" -gt 0 ]; then
  PROBLEMS+=("$MOUNT holds things this kit did not put there: ${UNKNOWN[*]}. Look at them; --keep-going goes on anyway (they are lost with the array's data)")
fi
[ -z "$UNFINISHED" ] || PROBLEMS+=("$UNFINISHED; finish it first (migrate-os --finish), or --keep-going")
OTHER_ARRAYS=$( { printf '%s\n' "$CONF_LINES" | grep -v -e "$(basename "$MD")" ${MD_UUID:+-e "$MD_UUID"} -e "name=${MD_NAME:-@@none@@}" -e "/dev/md/o1data" || true; } | grep -c '^ARRAY' || true)

# ---- 3. the plan -----------------------------------------------------------------------------------------------
STAMP=$(date +%Y%m%d-%H%M%S)
say ""
say "What it would do (in this order):"
say "   1. stop: ${SERVICES[*]}"
if [ -d "$DATA/backups/ollama1" ]; then say "   2. copy $MOUNT/backups/ollama1 to /var/backups/ollama1 (if under $((BACKUP_MAX_KB / 1024)) MiB)"; else say "   2. (no settings backups on the array to copy)"; fi
say "   3. umount $MOUNT"
say "   4. mdadm --stop $MD"
for m in $MEMBERS; do say "   5. mdadm --zero-superblock $m   (disk serial checked again first)"; done
say "   6. /etc/fstab: comment out the active line for $MOUNT   (copy: /etc/fstab.before-remove-raid-$STAMP)"
say "   7. /etc/mdadm/mdadm.conf: remove this array's ARRAY line   (copy: /etc/mdadm/mdadm.conf.before-remove-raid-$STAMP)"
if [ "${OTHER_ARRAYS:-0}" -gt 0 ]; then say "   8. (another array is in mdadm.conf: the array check stays on)"; else say "   8. turn off the array check: systemctl disable --now ${CHECK_TIMERS[*]}; comment out /etc/cron.d/mdadm"; fi
say "   9. update-initramfs -u"
say "Left alone: the partition tables and partitions of $D1 and $D2 (only the RAID superblocks go), every NVMe drive, /home, /srv/models."
say "Log of a real run: /var/log/ollama1-remove-raid.log"

if [ "${#PROBLEMS[@]}" -gt 0 ] && [ "$KEEP" != 1 ]; then
  say ""
  say "WOULD REFUSE:"
  for p in "${PROBLEMS[@]}"; do say "   - $p"; done
  exit 1
fi
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
  say ""; say "Going on anyway (--keep-going), although:"
  for p in "${PROBLEMS[@]}"; do say "   - $p"; done
fi

if [ "$YES" != 1 ]; then
  say ""
  say "This was a dry run: nothing was changed. To do it:  sudo bash $0 --yes-erase-the-mirror"
  exit 0
fi

# ---- 4. the typed confirmation: only from a terminal --------------------------------------------------------------
{ : <"$TTY"; } 2>/dev/null || die "no terminal to ask on: this will not run without one (run it in a terminal, not from a script or a pipe)"
printf '\nThis removes the RAID array %s. Everything on it ($MOUNT) is gone with it. Type yes to do it: ' "$MD"
ANS=""
read -r ANS <"$TTY" || die "no answer from the terminal; nothing was changed"
[ "$ANS" = "yes" ] || die "not confirmed (you typed '$ANS'); nothing was changed"

# ---- 5. do it ------------------------------------------------------------------------------------------------------------
mkdir -p "$(dirname "$LOGFILE")"
touch "$LOGFILE"; chmod 600 "$LOGFILE"
exec > >(tee -a "$LOGFILE") 2>&1
echo "===== ollama1 remove-raid $(date '+%Y-%m-%dT%H:%M:%S%z'): $MD, members: $(echo $MEMBERS) ====="

for u in "${SERVICES[@]}"; do systemctl stop "$u" 2>/dev/null || true; done
if command -v fuser >/dev/null 2>&1 && fuser -m "$MOUNT" >/dev/null 2>&1; then
  die "something still has files open on $MOUNT (fuser -vm $MOUNT shows what); nothing was changed yet"
fi

if [ -d "$DATA/backups/ollama1" ]; then
  kb=$(du -sk -- "$DATA/backups/ollama1" | awk '{print $1}')
  if [ "$kb" -le "$BACKUP_MAX_KB" ]; then
    mkdir -p "$BACKUPS_TO"
    cp -a "$DATA/backups/ollama1/." "$BACKUPS_TO/"
    chmod 700 "$BACKUPS_TO"
    echo "settings backups copied to /var/backups/ollama1 ($kb KiB)"
  else
    echo "the settings backups are $((kb / 1024)) MiB, over $((BACKUP_MAX_KB / 1024)) MiB: not copied"
  fi
fi

if findmnt -no TARGET "$MOUNT" >/dev/null 2>&1; then
  sync
  umount "$MOUNT" || die "could not unmount $MOUNT (fuser -vm $MOUNT shows what holds it); nothing else was changed"
fi
mdadm --stop "$MD" || die "mdadm --stop $MD failed; $MOUNT is unmounted, fstab and mdadm.conf are as they were"
for m in $MEMBERS; do
  dk=$(disk_of "$m")
  { [ "$dk" = "$D1" ] && [ "$(disk_serial "$dk")" = "$S1" ]; } || { [ "$dk" = "$D2" ] && [ "$(disk_serial "$dk")" = "$S2" ]; } \
    || die "serial check failed for $m; not zeroing it"
  is_nvme "$m" && die "$m is an NVMe device; not touching it"
  mdadm --zero-superblock "$m" || die "mdadm --zero-superblock $m failed"
  echo "superblock zeroed: $m"
done

# fstab: only the active /srv/data line(s) change, each into a comment that keeps its text
cp -a "$FSTAB" "$FSTAB.before-remove-raid-$STAMP"
awk -v m="$MOUNT" -v d="$(date +%F)" '
  $0 !~ /^[[:space:]]*#/ && $2 == m { print "# ollama1 " d ": the mirror was removed (tools/remove-raid.sh): " $0; next }
  { print }' "$FSTAB" >"$FSTAB.new" && mv "$FSTAB.new" "$FSTAB"
echo "fstab: the line for $MOUNT is commented out (copy: $FSTAB.before-remove-raid-$STAMP)"

# mdadm.conf: only this array's ARRAY line(s)
if [ -f "$MDCONF" ]; then
  cp -a "$MDCONF" "$MDCONF.before-remove-raid-$STAMP"
  awk -v md="$(basename "$MD")" -v uuid="$MD_UUID" -v name="$MD_NAME" '
    /^ARRAY/ && (index($0, "/" md " ") || index($0, "/dev/md/o1data ") || (uuid != "" && index($0, "UUID=" uuid)) || (name != "" && index($0, "name=" name " "))) { next }
    { print }' "$MDCONF" >"$MDCONF.new" && mv "$MDCONF.new" "$MDCONF"
  echo "mdadm.conf: the ARRAY line is removed (copy: $MDCONF.before-remove-raid-$STAMP)"
fi

if [ "${OTHER_ARRAYS:-0}" -eq 0 ]; then
  for t in "${CHECK_TIMERS[@]}"; do systemctl disable --now "$t" 2>/dev/null || true; done
  if [ -f "$CRON" ]; then
    cp -a "$CRON" "$CRON.before-remove-raid-$STAMP"
    awk '$0 !~ /^[[:space:]]*#/ && $0 !~ /^[[:space:]]*$/ && $0 !~ /^[A-Z_]+=/ { print "# ollama1: the array check is off (tools/remove-raid.sh): " $0; next } { print }' "$CRON" >"$CRON.new" && mv "$CRON.new" "$CRON"
  fi
  echo "the monthly array check is off (timers disabled, cron job commented out)"
fi
systemctl daemon-reload 2>/dev/null || true
update-initramfs -u || echo "NOTE: update-initramfs -u failed; run it by hand: sudo update-initramfs -u"

# ---- 6. what is true now ------------------------------------------------------------------------------------------------------
echo ""
echo "Checking:"
bad=0
if findmnt -no TARGET "$MOUNT" >/dev/null 2>&1; then echo "   NOT OK: $MOUNT is still mounted"; bad=1; else echo "   ok: $MOUNT is not mounted"; fi
if mdadm --detail "$MD" >/dev/null 2>&1; then echo "   NOT OK: $MD still exists"; bad=1; else echo "   ok: $MD is gone"; fi
for m in $MEMBERS; do
  if mdadm --examine "$m" >/dev/null 2>&1; then echo "   NOT OK: $m still has a RAID superblock"; bad=1; else echo "   ok: $m has no RAID superblock"; fi
done
if awk '$0 !~ /^[[:space:]]*#/ && $2 == "'"$MOUNT"'"' "$FSTAB" | grep -q .; then echo "   NOT OK: fstab still has an active $MOUNT line"; bad=1; else echo "   ok: fstab has no active $MOUNT line"; fi
echo ""
echo "Done. $D1 and $D2 keep their partition tables and partitions (they are just not an array any more)."
echo "When you decide what to do with them:  sudo wipefs -a /dev/disk/by-id/<the disk>   (erases the partition table too; check the serial first)"
[ "$bad" = 0 ] || { echo "Something above is NOT OK: look at it before you reboot."; exit 1; }
