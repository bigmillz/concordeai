#!/usr/bin/env bash
# install-watchdogs.sh: install, check or remove the server's self-recovery pieces (6b524) on their own, without a
# full setup.sh run (which restarts the firewall, SSH and the tunnel).
#
#   cd ~/concordeai/ollama1 && git pull
#   sudo bash tools/install-watchdogs.sh --dry-run          exactly what would change; changes nothing
#   sudo bash tools/install-watchdogs.sh --arm-rollback     install, with a 20-minute dead man's switch
#   sudo bash tools/install-watchdogs.sh --confirm          (from a NEW ssh session) keep it: cancel the switch
#   sudo bash tools/install-watchdogs.sh                    install, no switch
#   sudo bash tools/install-watchdogs.sh --uninstall        remove every piece, the sysctl values from before back
#
# The pieces:
#   ollama1-watchdog-doctor   read-only report on the hardware watchdog (6b399) and how the last boot ended
#   ollama1-bootguard         panic-reboot (kernel.panic=10, soft/hard lockup, oops and hung-task panics) applied
#                             early in each boot from /etc/ollama1/panic-sysctl.conf, unless 3 boots came within
#                             60 minutes (then the values from before, so a loop stops and the box stays up)
#   ollama1-netwatch          every minute: offline for 3/6/10/20 min -> reconnect / radio off-on / (optional)
#                             driver rebind / reboot; the tunnel restarted after 10 min down with the network up
# Files: three programs and three libraries in /usr/local/lib/ollama1 (stdlib only: no library the rest of the kit
# uses is replaced), four units in /etc/systemd/system, /etc/ollama1/panic-sysctl.conf (an edited copy is kept),
# three links in /usr/local/sbin, and this script in /usr/local/lib/ollama1/tools (the rollback runs that copy).
#
# It never restarts Ollama, the gateway or the tunnel, never reboots, and touches no network, firewall, SSH or
# cloudflared setting. It refuses while the gateway is serving a request (and while setup.sh, a model download
# or an update runs): the same test the auto sleep uses (o1idle's activity file, o1sleep.busy_reasons).
# The sysctl values take effect at once (no reboot); --uninstall puts back the ones saved before the install.
set -euo pipefail

R=${O1_WATCHDOGS_TESTROOT:-}     # tests only: every destination under this folder, fake systemctl/systemd-run on PATH
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIBDIR=$R/usr/local/lib/ollama1
UNITDIR=$R/etc/systemd/system
SBIN=$R/usr/local/sbin
ETC=$R/etc/ollama1
STATE=$R/var/lib/ollama1
PY=(python3 -I -B)
DEADLINE_UNIT=ollama1-watchdogs-deadline
ROLLBACK_UNIT=ollama1-watchdogs-rollback.service
TRIAL_MIN=20
BINS=(ollama1-netwatch ollama1-bootguard ollama1-watchdog-doctor)
LIBS=(o1netwatch.py o1bootguard.py o1wddoctor.py)
UNITS=(ollama1-netwatch.service ollama1-netwatch.timer ollama1-bootguard.service ollama1-watchdogs-rollback.service)
STATES=(netwatch.json bootguard.json watchdogs-trial.json)
if [ -n "$R" ]; then
  export OLLAMA1_PREFIX="$R" OLLAMA1_PROC="${OLLAMA1_PROC:-$R/proc}" OLLAMA1_SYS="${OLLAMA1_SYS:-$R/sys}"
fi

die() { echo "STOPPED: $*"; echo "Nothing was changed."; exit 1; }
DRY=0
say() { if [ "$DRY" = 1 ]; then echo "  would: $*"; else echo "  $*"; fi; }
act() { if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }

# what a file copy would do: new, changed or the same
compare() { if [ ! -e "$2" ]; then echo new; elif cmp -s "$1" "$2"; then echo unchanged; else echo changed; fi; }

put() {   # put SRC DEST MODE
  if [ "$DRY" = 1 ]; then
    echo "  would install $2 ($(compare "$1" "$2"), mode $3)"
  elif ! [ "$1" -ef "$2" ]; then          # never over itself (the installed copy running --confirm, say)
    install -m "$3" "$1" "$2"
  fi
}

busy_check() {   # the kit's own test, from the checkout (nothing installed is used or replaced)
  "${PY[@]}" - "$KIT/lib" <<'PYEOF'
import sys, time
sys.path.insert(0, sys.argv[1])
import o1idle, o1sleep
why = []
a = o1idle.read_activity(time.time())
if a.get("inflight"):
    why.append("the gateway is serving %d request(s)" % a["inflight"])
why += o1sleep.busy_reasons()
for w in why:
    print("  busy: " + w)
if not a.get("fresh"):
    print("  note: the gateway hasn't written its activity file in 2 minutes (it may not be running)")
sys.exit(1 if why else 0)
PYEOF
}

boot_id() { cat "${OLLAMA1_PROC:-/proc}/sys/kernel/random/boot_id" 2>/dev/null || echo unknown; }

install_all() {
  echo "== installing the self-recovery pieces (6b524)"
  for d in "$LIBDIR" "$LIBDIR/bin" "$LIBDIR/lib" "$LIBDIR/tools" "$UNITDIR" "$SBIN" "$ETC" "$STATE"; do
    [ -d "$d" ] || act install -d -m 0755 "$d"
  done
  for b in "${BINS[@]}"; do put "$KIT/bin/$b" "$LIBDIR/bin/$b" 0755; done
  for l in "${LIBS[@]}"; do put "$KIT/lib/$l" "$LIBDIR/lib/$l" 0644; done
  for u in "${UNITS[@]}"; do put "$KIT/systemd/$u" "$UNITDIR/$u" 0644; done
  put "$KIT/tools/install-watchdogs.sh" "$LIBDIR/tools/install-watchdogs.sh" 0755
  if [ -e "$ETC/panic-sysctl.conf" ] && ! cmp -s "$KIT/config/panic-sysctl.conf" "$ETC/panic-sysctl.conf"; then
    echo "  kept your edited $ETC/panic-sysctl.conf (the kit's is $KIT/config/panic-sysctl.conf)"
  else
    put "$KIT/config/panic-sysctl.conf" "$ETC/panic-sysctl.conf" 0644
  fi
  for b in "${BINS[@]}"; do
    if [ "$DRY" = 1 ]; then echo "  would link $SBIN/$b -> /usr/local/lib/ollama1/bin/$b"
    else ln -sfn "/usr/local/lib/ollama1/bin/$b" "$SBIN/$b"; fi
  done
  act systemctl daemon-reload

  if [ "$ARM" = 1 ]; then
    echo "== the dead man's switch: everything is rolled back in $TRIAL_MIN minutes unless you confirm"
    local now deadline
    now=$(date +%s); deadline=$((now + TRIAL_MIN * 60))
    if [ "$DRY" = 1 ]; then
      echo "  would write $STATE/watchdogs-trial.json (this boot, the deadline)"
    else
      printf '{"armed_at": %s, "boot_id": "%s", "deadline": %s}\n' "$now" "$(boot_id)" "$deadline" >"$STATE/watchdogs-trial.json"
      systemctl stop "$DEADLINE_UNIT.timer" >/dev/null 2>&1 || true            # an earlier arm's
      systemctl reset-failed "$DEADLINE_UNIT.timer" "$DEADLINE_UNIT.service" >/dev/null 2>&1 || true
    fi
    if ! act systemd-run --unit="$DEADLINE_UNIT" --description="ollama1: roll back the watchdogs install unless confirmed" \
      --on-active="${TRIAL_MIN}min" --timer-property=AccuracySec=10s /bin/systemctl start --no-block "$ROLLBACK_UNIT"; then
      rm -f "$STATE/watchdogs-trial.json"
      echo "STOPPED: the rollback timer could not be armed. The files are copied; nothing is enabled or applied."
      exit 1
    fi
  fi

  echo "== panic-reboot settings (they take effect now, no reboot)"
  if [ "$DRY" = 1 ]; then
    echo "  would save the values from before in $STATE/panic-before.json (once), then set:"
    if [ -e "$ETC/panic-sysctl.conf" ]; then "${PY[@]}" "$KIT/bin/ollama1-bootguard" plan
    else "${PY[@]}" "$KIT/bin/ollama1-bootguard" plan "$KIT/config/panic-sysctl.conf"; fi
  else
    "${PY[@]}" "$LIBDIR/bin/ollama1-bootguard" snapshot
  fi
  act systemctl enable ollama1-bootguard.service
  act systemctl restart ollama1-bootguard.service
  echo "== the network watch"
  act systemctl enable --now ollama1-netwatch.timer
  [ "$DRY" = 1 ] && return 0

  echo "== checks"
  "${PY[@]}" "$LIBDIR/bin/ollama1-bootguard" status || true
  if [ -z "$R" ]; then
    if "${PY[@]}" "$LIBDIR/bin/ollama1-netwatch" check; then
      echo "  the server reaches its gateway or the internet from here"
    else
      echo "  WARNING: the server does not reach its gateway or the internet right now"
    fi
  fi
  systemctl list-timers --all --no-pager ollama1-netwatch.timer || true
  echo "== the watchdog doctor (read-only)"
  "${PY[@]}" "$LIBDIR/bin/ollama1-watchdog-doctor" || true
  echo
  if [ "$ARM" = 1 ]; then
    echo "NEXT: from a NEW ssh session (that proves the server is still reachable), within $TRIAL_MIN minutes:"
    echo "  sudo bash /usr/local/lib/ollama1/tools/install-watchdogs.sh --confirm"
    echo "If nobody confirms, or the server restarts before that, everything above is removed again."
  fi
  echo "Undo everything: sudo bash /usr/local/lib/ollama1/tools/install-watchdogs.sh --uninstall"
}

uninstall_all() {
  echo "== removing the self-recovery pieces (6b524)${ROLLBACK:+ (the trial was not confirmed: rollback)}"
  if [ -x "$LIBDIR/bin/ollama1-netwatch" ]; then
    act "${PY[@]}" "$LIBDIR/bin/ollama1-netwatch" repair || echo "  a repair failed (see above); going on"
  fi
  act systemctl disable --now ollama1-netwatch.timer || true
  act systemctl stop ollama1-netwatch.service || true
  if [ -x "$LIBDIR/bin/ollama1-bootguard" ]; then
    if [ "$DRY" = 1 ]; then
      echo "  would put back the sysctl values saved before the install ($STATE/panic-before.json; the kernel's"
      echo "  defaults for any not saved)"
    else
      "${PY[@]}" "$LIBDIR/bin/ollama1-bootguard" restore || echo "  WARNING: a value did not go back (see above)"
    fi
  fi
  act systemctl disable ollama1-bootguard.service || true
  act systemctl stop ollama1-bootguard.service || true
  if [ -z "$ROLLBACK" ]; then act systemctl stop "$DEADLINE_UNIT.timer" 2>/dev/null || true; fi
  for b in "${BINS[@]}"; do act rm -f "$SBIN/$b" "$LIBDIR/bin/$b"; done
  for l in "${LIBS[@]}"; do act rm -f "$LIBDIR/lib/$l"; done
  for u in "${UNITS[@]}"; do act rm -f "$UNITDIR/$u"; done
  for s in "${STATES[@]}"; do act rm -f "$STATE/$s"; done
  act rm -f "$ETC/panic-sysctl.conf" "$ETC/netwatch.off" "$LIBDIR/tools/install-watchdogs.sh"
  act systemctl daemon-reload
  [ "$DRY" = 1 ] || echo "removed. (The hardware watchdog, 6b399, is left as it was.)"
}

confirm() {
  echo "== confirming the install: the rollback is cancelled"
  act systemctl stop "$DEADLINE_UNIT.timer" 2>/dev/null || echo "  (no rollback timer was running)"
  if [ -e "$STATE/watchdogs-trial.json" ]; then act rm -f "$STATE/watchdogs-trial.json"; fi
  if [ ! -e "$LIBDIR/bin/ollama1-bootguard" ]; then
    echo "  note: the pieces are not installed (the rollback may have run already): install again"
    return 1
  fi
  echo "  kept. Undo later: sudo bash /usr/local/lib/ollama1/tools/install-watchdogs.sh --uninstall"
}

main() {
  local mode=install
  ARM=0; ROLLBACK=""
  for a in "$@"; do
    case "$a" in
      --dry-run) DRY=1 ;;
      --arm-rollback) ARM=1 ;;
      --uninstall) mode=uninstall ;;
      --rollback) ROLLBACK=1 ;;
      --confirm) mode=confirm ;;
      -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
      *) die "unknown option $a (see the top of this file)" ;;
    esac
  done
  [ -n "$R" ] || [ "$(id -u)" -eq 0 ] || die "run it with sudo"
  command -v python3 >/dev/null 2>&1 || die "python3 isn't installed"
  case "$mode" in
    uninstall) uninstall_all; return 0 ;;
    confirm) confirm; return $? ;;
  esac
  local f
  for f in "${BINS[@]/#/bin/}" "${LIBS[@]/#/lib/}" "${UNITS[@]/#/systemd/}" config/panic-sysctl.conf tools/install-watchdogs.sh; do
    [ -f "$KIT/$f" ] || die "the kit is incomplete: $f is missing (run it from the kit: ollama1/tools/install-watchdogs.sh)"
  done
  for f in "${BINS[@]/#/bin/}" "${LIBS[@]/#/lib/}"; do   # a copy that doesn't even parse never replaces a working one
    "${PY[@]}" -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$KIT/$f" || die "$f doesn't parse"
  done
  if ! busy_check; then
    die "the server is busy (above); try again when it is idle"
  fi
  [ "$DRY" = 1 ] && echo "DRY RUN: nothing below is done"
  install_all
}

main "$@"
exit $?
