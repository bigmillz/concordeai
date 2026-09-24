#!/usr/bin/env bash
# A full system update for an Ubuntu or Debian server (per Patrick, 2026-09-24).
#
#   update-system.sh                 update the package lists, upgrade everything, remove what is no
#                                    longer needed, clean the download cache, refresh snaps
#   update-system.sh --reboot        the same, then reboot if the upgrade asks for one
#   update-system.sh --dry-run       show what would change and change nothing
#   update-system.sh --check         only refresh the lists and report what is pending (the nightly check)
#   ... --status FILE                also write a JSON summary to FILE (the ConcordeGo admin page reads it)
#
# Run it on a server from a laptop DETACHED, so a dropped connection cannot kill it halfway (on
# 2026-09-24 the session dropped while a 453 MB droplet built a kernel's boot image, and the rest of
# the run died with it). Copy it over, start it under systemd, then follow the log:
#   scp concorde-travel/deploy/update-system.sh root@SERVER:/root/update-system.sh
#   ssh root@SERVER 'systemd-run --collect --unit=update-system bash /root/update-system.sh --reboot'
#   ssh root@SERVER 'tail -f $(ls -t /var/log/update-system/*.log | head -1)'
#
# What a run does, in order:
#   1. on a server with under 1 GB of memory and no swap, adds a temporary 1 GB swap file (building
#      a kernel's boot image otherwise starves everything else on the box), removed at the end
#   2. waits for any running apt or dpkg (the nightly security updates) instead of failing on the lock
#   3. finishes any install a previous run left half done (dpkg --configure -a)
#   4. apt-get update, then full-upgrade (kernels and new dependencies included), keeping every config
#      file that was edited on the box (sshd, the firewall, unattended-upgrades)
#   5. autoremove --purge (old kernels and orphaned packages), autoclean (stale downloads)
#   6. snap refresh, when snapd is installed
#   7. says which services were restarted and whether a reboot is needed; reboots only with --reboot
#
# Everything is logged to /var/log/update-system/<date>.log on the server.

set -euo pipefail
umask 022

DRY=0
REBOOT=0
CHECK=0
STATUS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --reboot) REBOOT=1 ;;
        --check) CHECK=1 ;;
        --status) shift; STATUS="${1:-}" ;;
        -h|--help) sed -n '2,31p' "$0" 2>/dev/null || true; exit 0 ;;
        *) echo "unknown option: $1 (use --dry-run, --reboot, --check or --status FILE)" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root (or with sudo)" >&2
    exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
    echo "this server has no apt-get; it is not Debian or Ubuntu" >&2
    exit 1
fi

LOGDIR=/var/log/update-system
mkdir -p "$LOGDIR"
chmod 755 "$LOGDIR"
# (an if, not "[ ] && echo" inside $(...): under set -e a false test there ends the script, silently)
SUFFIX=""
if [ "$DRY" = 1 ]; then SUFFIX="-dry-run"; fi
if [ "$CHECK" = 1 ]; then SUFFIX="-check"; fi
LOG="$LOGDIR/$(date -u +%Y-%m-%dT%H%M%SZ)$SUFFIX.log"
exec > >(tee -a "$LOG") 2>&1

# never stop to ask a question: keep edited config files, and let needrestart restart services itself
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
APT=(apt-get -y -o DPkg::Lock::Timeout=900
     -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
if [ "$DRY" = 1 ]; then APT+=(--simulate); fi

step() { printf '\n== %s ==\n' "$*"; }

# The JSON summary: what is pending, whether a reboot is waiting or likely, and how the last run went.
# Written to a temp file and moved into place, so a reader never sees half a file.
write_status() {   # $1 = state (checked | running | finished | failed), $2 = exit code of the run
    [ -n "$STATUS" ] || return 0
    command -v python3 >/dev/null 2>&1 || { echo "no python3; status not written"; return 0; }
    mkdir -p "$(dirname "$STATUS")"
    # "pending" is what an upgrade would install now (a simulation's Inst lines); Ubuntu's phased updates,
    # which it rolls out to machines a slice at a time, are listed apart as "deferred": Update cannot take them yet
    apt-get -s -o Debug::NoLocking=1 full-upgrade 2>/dev/null | awk '/^Inst /{print $2}' > "$STATUS.pkgs" || true
    apt list --upgradable 2>/dev/null | sed 1d | cut -d/ -f1 > "$STATUS.all" || true
    STATE="$1" RC="${2:-}" LOGFILE="$LOG" MODE="$([ "$REBOOT" = 1 ] && echo reboot || echo update)" OUT="$STATUS" \
    python3 - <<'PY' || echo "status not written"
import json, os, re, datetime
out = os.environ["OUT"]
pkgs = sorted(set(p.strip() for p in open(out + ".pkgs") if p.strip()))
every = set(p.strip() for p in open(out + ".all") if p.strip())
deferred = sorted(every - set(pkgs))
os.remove(out + ".pkgs"); os.remove(out + ".all")
prev = {}
try:
    prev = json.load(open(out))
except Exception:
    pass
# packages whose upgrade usually leaves a reboot waiting (the kernel, the C library, the message bus, init)
likely = [p for p in pkgs if re.match(r"^(linux-(image|modules|base|firmware|generic|virtual)|libc6|libc-bin|dbus|systemd|libssl)", p)]
waiting = []
if os.path.exists("/var/run/reboot-required"):
    try:
        waiting = sorted(set(l.strip() for l in open("/var/run/reboot-required.pkgs") if l.strip()))
    except OSError:
        waiting = ["(unknown)"]
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
st = dict(prev)
st.update({"host": os.uname().nodename, "checked_at": now, "pending": pkgs, "pending_count": len(pkgs), "deferred": deferred,
           "restart_likely": likely, "reboot_waiting": waiting, "state": os.environ["STATE"]})
if os.environ["STATE"] in ("running", "finished", "failed"):
    st["last_run"] = dict(st.get("last_run") or {}, mode=os.environ["MODE"], log=os.environ["LOGFILE"],
                          **({"started_at": now} if os.environ["STATE"] == "running" else
                             {"finished_at": now, "exit": int(os.environ["RC"] or 0)}))
tmp = out + ".tmp"
with open(tmp, "w") as fh:
    json.dump(st, fh, indent=1)
os.chmod(tmp, 0o644)
os.replace(tmp, out)
PY
}

SWAP=""
cleanup() {
    rc=$?
    if [ -n "$SWAP" ]; then swapoff "$SWAP" 2>/dev/null || true; rm -f "$SWAP"; SWAP=""; fi
    if [ "$CHECK" = 0 ] && [ "$DRY" = 0 ]; then write_status "$([ $rc = 0 ] && echo finished || echo failed)" "$rc"; fi
}
trap cleanup EXIT

. /etc/os-release 2>/dev/null || true
echo "$(hostname) · ${PRETTY_NAME:-unknown system} · $(uname -r) · $(date -u '+%Y-%m-%d %H:%M UTC')"
if [ "$DRY" = 1 ]; then echo "DRY RUN: nothing will be installed, removed or restarted."; fi
if [ "$CHECK" = 1 ]; then echo "CHECK: refreshing the package lists and reporting only."; fi
df -h / | awk 'NR==2 {print "Disk before: " $3 " used, " $4 " free"}'

if [ "$CHECK" = 1 ]; then
    step "Updating the package lists"
    apt-get -o DPkg::Lock::Timeout=900 update
    step "Upgradable packages"
    apt list --upgradable 2>/dev/null | sed 1d || true
    write_status checked ""
    echo "Log: $LOG"
    exit 0
fi

if [ "$DRY" = 0 ]; then
    write_status running ""
    mem_kb=$(awk '/^MemTotal/ {print $2}' /proc/meminfo)
    swap_kb=$(awk '/^SwapTotal/ {print $2}' /proc/meminfo)
    free_kb=$(df -Pk / | awk 'NR==2 {print $4}')
    if [ "${mem_kb:-0}" -lt 1048576 ] && [ "${swap_kb:-0}" -eq 0 ] && [ "${free_kb:-0}" -gt 3145728 ]; then
        step "Adding a temporary 1 GB swap file (this server has $((mem_kb / 1024)) MB of memory and no swap)"
        SWAP=/swapfile-update-system
        if fallocate -l 1G "$SWAP" 2>/dev/null || dd if=/dev/zero of="$SWAP" bs=1M count=1024 status=none; then
            chmod 600 "$SWAP"
            if mkswap "$SWAP" >/dev/null && swapon "$SWAP"; then echo "on"; else echo "could not enable swap; carrying on without"; rm -f "$SWAP"; SWAP=""; fi
        else
            echo "could not make a swap file; carrying on without"; SWAP=""
        fi
    fi
    step "Finishing any interrupted install"
    dpkg --configure -a
fi

step "Updating the package lists"
apt-get -o DPkg::Lock::Timeout=900 update

step "Upgradable packages"
apt list --upgradable 2>/dev/null | sed 1d || true

step "Upgrading everything"
"${APT[@]}" full-upgrade

step "Removing packages no longer needed"
"${APT[@]}" autoremove --purge

step "Cleaning the download cache"
if [ "$DRY" = 0 ]; then
    apt-get -o DPkg::Lock::Timeout=900 autoclean
else
    echo "(skipped in a dry run)"
fi

if command -v snap >/dev/null 2>&1; then
    step "Refreshing snaps"
    if [ "$DRY" = 0 ]; then snap refresh || echo "snap refresh failed; the apt upgrade above still stands"
    else snap refresh --list 2>&1 || true
    fi
fi

step "Result"
df -h / | awk 'NR==2 {print "Disk after: " $3 " used, " $4 " free"}'
if command -v needrestart >/dev/null 2>&1 && [ "$DRY" = 0 ]; then
    needrestart -b 2>/dev/null | grep -E '^NEEDRESTART-(KSTA|SVC)' || true
fi
if [ -f /var/run/reboot-required ]; then
    echo "A reboot is needed for:"
    sed 's/^/  /' /var/run/reboot-required.pkgs 2>/dev/null | sort -u || true
    if [ "$REBOOT" = 1 ] && [ "$DRY" = 0 ]; then
        cleanup            # swap off and the status written before the box goes down
        trap - EXIT
        echo "Rebooting in 10 seconds (--reboot). Log: $LOG"
        sleep 10
        systemctl reboot
        exit 0
    fi
    echo "Not rebooting. Run again with --reboot, or reboot when convenient."
else
    echo "No reboot needed."
fi
echo "Log: $LOG"
