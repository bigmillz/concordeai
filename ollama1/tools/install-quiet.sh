#!/usr/bin/env bash
# install-quiet.sh: install quiet hours (6b601) on their own, without a full setup.sh run, and undo them.
#
#   sudo bash tools/install-quiet.sh              install (refuses while the server is serving)
#   sudo bash tools/install-quiet.sh --uninstall  put back exactly what the last install replaced
#
# setup.sh would also reapply the firewall and SSH and restart the tunnel. This touches only:
#   /usr/local/lib/ollama1/lib/*.py and bin/*       the kit's programs (the same files setup.sh installs)
#   /etc/systemd/system/ollama1-update-ollama.timer  Sunday 15:30 instead of 03:30
#   /etc/apt/apt.conf.d/52ollama1-unattended-upgrades  an update's reboot at 15:00 instead of 04:00
#   /etc/systemd/system/ollama1-cpu-perf.service     the hand-made unit, retired (a copy is kept)
# then restarts the gateway, the admin panel, auto sleep, the fans and the lights (OpenRGB). Ollama, the tunnel,
# SSH, the firewall, the network, the drivers and the sleep mechanism are not touched. Everything it replaces goes
# into /var/backups/ollama1/quiet-<time>.tar first; if a restarted service doesn't come back within 30 s, that copy
# is put back and the services restarted again, by itself.
set -euo pipefail

R=${O1_QUIET_TESTROOT:-}         # tests only: every destination under this folder, a fake systemctl on PATH
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIBDIR=$R/usr/local/lib/ollama1
UNITDIR=$R/etc/systemd/system
APTDIR=$R/etc/apt/apt.conf.d
BACKUPS=$R/var/backups/ollama1
OPENRGB_JSON=$R/var/lib/ollama1-openrgb/OpenRGB/OpenRGB.json
CPUPERF=$UNITDIR/ollama1-cpu-perf.service
SERVICES=(ollama1-gateway.service ollama1-admin.service ollama1-idle.service ollama1-fan.service
          ollama1-openrgb.service ollama1-leds.service)

die() { echo "STOPPED: $*"; echo "Nothing was changed."; exit 1; }
[ -n "$R" ] || [ "$(id -u)" -eq 0 ] || die "run it with sudo"

restart_all() {
  systemctl daemon-reload
  for s in "${SERVICES[@]}"; do
    systemctl is-enabled --quiet "$s" 2>/dev/null || continue      # e.g. the lights when they are off
    systemctl restart "$s" || true
  done
  systemctl restart ollama1-update-ollama.timer 2>/dev/null || true
}

all_up() {
  local s i
  for i in $(seq 1 30); do
    local down=""
    for s in "${SERVICES[@]}"; do
      systemctl is-enabled --quiet "$s" 2>/dev/null || continue
      systemctl is-active --quiet "$s" || down="$down $s"
    done
    [ -z "$down" ] && return 0
    sleep 1
  done
  echo "not running after 30 s:$down"
  return 1
}

restore() {     # restore <tar>: every file as it was, the retired unit back if it was there
  local tar=$1
  [ -f "$tar" ] || die "no backup at $tar"
  tar -C "${R:-/}" -xpf "$tar"
  if [ -f "$CPUPERF" ]; then
    systemctl daemon-reload
    systemctl enable ollama1-cpu-perf.service >/dev/null 2>&1 || true
    systemctl start ollama1-cpu-perf.service >/dev/null 2>&1 || true
  fi
  restart_all
}

if [ "${1:-}" = "--uninstall" ]; then
  last=$(ls -1t "$BACKUPS"/quiet-*.tar 2>/dev/null | head -1 || true)
  [ -n "$last" ] || die "no quiet-hours backup in $BACKUPS"
  echo "putting back $last"
  restore "$last"
  all_up && echo "back as it was before quiet hours" || echo "CHECK: a service did not come back (journalctl -u <name>)"
  exit 0
fi

# ---- checks before anything changes --------------------------------------------------------------------------
for f in lib/o1quiet.py lib/o1idle.py lib/o1fan.py lib/o1leds.py lib/o1aio.py bin/ollama1-gateway \
         systemd/ollama1-update-ollama.timer config/52ollama1-unattended-upgrades; do
  [ -f "$KIT/$f" ] || die "the kit is incomplete: $f is missing"
done
# a copy that doesn't even parse never replaces a working one
for f in "$KIT"/lib/*.py "$KIT"/bin/*; do
  case "$f" in *.py) ;; *) head -1 "$f" | grep -q python || continue ;; esac
  python3 -I -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$f" \
    || die "$(basename "$f") doesn't parse"
done
if [ -z "$R" ]; then
  n=$(journalctl -u ollama1-gateway --since "-90 s" --no-pager -q 2>/dev/null | grep -c " req " || true)
  [ "${n:-0}" -eq 0 ] || die "the server is serving ($n requests in the last 90 s): try again when it is quiet"
fi

# ---- the backup ----------------------------------------------------------------------------------------------
install -d -m 0750 "$BACKUPS"
stamp=$(date +%Y%m%d-%H%M%S)
tar="$BACKUPS/quiet-$stamp.tar"
list=()
for f in "$LIBDIR"/lib/*.py "$LIBDIR"/bin/* "$UNITDIR/ollama1-update-ollama.timer" \
         "$APTDIR/52ollama1-unattended-upgrades" "$CPUPERF" "$OPENRGB_JSON"; do
  [ -e "$f" ] && list+=("${f#"${R:-}"/}")
done
tar -C "${R:-/}" -cpf "$tar" "${list[@]}"
echo "backup: $tar (${#list[@]} files)"

# ---- install -------------------------------------------------------------------------------------------------
install -d -m 0755 "$LIBDIR" "$LIBDIR/bin" "$LIBDIR/lib" "$UNITDIR" "$APTDIR"
install -m 0644 "$KIT"/lib/*.py "$LIBDIR/lib/"
for f in "$KIT"/bin/*; do install -m 0755 "$f" "$LIBDIR/bin/"; done
install -m 0644 "$KIT/systemd/ollama1-update-ollama.timer" "$UNITDIR/ollama1-update-ollama.timer"
install -m 0644 "$KIT/config/52ollama1-unattended-upgrades" "$APTDIR/52ollama1-unattended-upgrades"
if [ -f "$CPUPERF" ]; then
  cp -p "$CPUPERF" "$BACKUPS/ollama1-cpu-perf.service.bak"
  systemctl disable ollama1-cpu-perf.service >/dev/null 2>&1 || true
  rm -f "$CPUPERF"
  case "$(cat "$R"/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null)" in
    amd-pstate-epp|intel_pstate)
      for p in "$R"/sys/devices/system/cpu/cpu*/cpufreq; do
        echo powersave > "$p/scaling_governor" 2>/dev/null || true
        echo balance_performance > "$p/energy_performance_preference" 2>/dev/null || true
      done ;;
  esac
  echo "ollama1-cpu-perf retired: the processor is on its normal setting"
fi
echo "installed; restarting the gateway, the admin panel, auto sleep, the fans and the lights"
restart_all
if ! all_up; then
  echo "ROLLING BACK: putting $tar back"
  restore "$tar"
  all_up && echo "rolled back: everything is as it was" || echo "CHECK: rolled back, but a service is still down"
  exit 1
fi
echo "quiet hours are in: $(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import o1quiet, json; print(json.dumps(o1quiet.view()))' "$LIBDIR/lib" 2>/dev/null || echo '(could not read the view)')"
echo "undo: sudo bash tools/install-quiet.sh --uninstall"
