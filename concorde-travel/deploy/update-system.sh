#!/usr/bin/env bash
# A full system update for an Ubuntu or Debian server (per Patrick, 2026-09-24).
#
#   update-system.sh              update the package lists, upgrade everything, remove what is
#                                 no longer needed, clean the download cache, refresh snaps
#   update-system.sh --dry-run    show what would change and change nothing
#   update-system.sh --reboot     the same, then reboot if the upgrade asks for one
#
# Run it on a server from a laptop without copying it there first:
#   ssh root@SERVER 'bash -s' < concorde-travel/deploy/update-system.sh
#   ssh root@SERVER 'bash -s -- --reboot' < concorde-travel/deploy/update-system.sh
#
# What it does, in order:
#   1. waits for any running apt or dpkg (the nightly security updates) instead of failing on the lock
#   2. finishes any install a previous run left half done (dpkg --configure -a)
#   3. apt-get update, then full-upgrade (kernels and new dependencies included), keeping every
#      config file that was edited on the box (sshd, the firewall, unattended-upgrades) and
#      taking the package's default only where nothing was edited
#   4. autoremove --purge (old kernels and orphaned packages), autoclean (stale downloads)
#   5. snap refresh, when snapd is installed
#   6. says which services were restarted and whether a reboot is needed; it reboots only with --reboot
#
# Everything is logged to /var/log/update-system/<date>.log on the server.

set -euo pipefail

DRY=0
REBOOT=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --reboot) REBOOT=1 ;;
        -h|--help) sed -n '2,24p' "$0" 2>/dev/null || true; exit 0 ;;
        *) echo "unknown option: $arg (use --dry-run or --reboot)" >&2; exit 2 ;;
    esac
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
LOG="$LOGDIR/$(date -u +%Y-%m-%dT%H%M%SZ)$([ "$DRY" = 1 ] && echo -dry-run).log"
exec > >(tee -a "$LOG") 2>&1

# never stop to ask a question: keep edited config files, and let needrestart restart services itself
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
APT=(apt-get -y -o DPkg::Lock::Timeout=900
     -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
[ "$DRY" = 1 ] && APT+=(--simulate)

step() { printf '\n== %s ==\n' "$*"; }

. /etc/os-release 2>/dev/null || true
echo "$(hostname) · ${PRETTY_NAME:-unknown system} · $(uname -r) · $(date -u '+%Y-%m-%d %H:%M UTC')"
[ "$DRY" = 1 ] && echo "DRY RUN: nothing will be installed, removed or restarted."
df -h / | awk 'NR==2 {print "Disk before: " $3 " used, " $4 " free"}'

if [ "$DRY" = 0 ]; then
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
        echo "Rebooting in 10 seconds (--reboot)."
        sleep 10
        systemctl reboot
    else
        echo "Not rebooting. Run again with --reboot, or reboot when convenient."
    fi
else
    echo "No reboot needed."
fi
echo "Log: $LOG"
