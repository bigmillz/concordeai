#!/usr/bin/env bash
# install-logguard.sh: install or update the log guard (6b517) on its own, without a full setup.sh run.
#
#   cd ~/concordeai/ollama1 && git pull && sudo bash tools/install-logguard.sh
#
# setup.sh installs the log guard too, but it also restarts the firewall, SSH and the tunnel. This touches
# only the log guard's own four files and its timer:
#   /usr/local/lib/ollama1/bin/ollama1-logguard      the program
#   /usr/local/lib/ollama1/lib/o1logguard.py          its only library (stdlib only: no other kit file is
#                                                     replaced, so nothing else that runs changes)
#   /etc/systemd/system/ollama1-logguard.service      the oneshot (root, sandboxed: writes only /var/log)
#   /etc/systemd/system/ollama1-logguard.timer        every minute
#   /usr/local/sbin/ollama1-logguard                  a symlink, for `sudo ollama1-logguard status`
# then `systemctl daemon-reload`, enables and starts the timer, runs one pass now, and prints the result and
# the timer's next run. No network, no firewall, no sshd, no cloudflared, no restart of anything else, no
# package. Safe to run again: it puts the same files back.
set -euo pipefail

R=${O1_LOGGUARD_TESTROOT:-}      # tests only: every destination under this folder, a fake systemctl on PATH
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIBDIR=$R/usr/local/lib/ollama1
UNITDIR=$R/etc/systemd/system
SBIN=$R/usr/local/sbin
FILES=(bin/ollama1-logguard lib/o1logguard.py systemd/ollama1-logguard.service systemd/ollama1-logguard.timer)

die() { echo "STOPPED: $*"; echo "Nothing was changed."; exit 1; }
[ -n "$R" ] || [ "$(id -u)" -eq 0 ] || die "run it with sudo"
for f in "${FILES[@]}"; do
  [ -f "$KIT/$f" ] || die "the kit is incomplete: $f is missing (run it from the kit: ollama1/tools/install-logguard.sh)"
done
command -v python3 >/dev/null 2>&1 || die "python3 isn't installed"
# a copy that doesn't even parse never replaces a working one
for f in bin/ollama1-logguard lib/o1logguard.py; do
  python3 -I -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$KIT/$f" \
    || die "$f doesn't parse"
done

install -d -m 0755 "$LIBDIR" "$LIBDIR/bin" "$LIBDIR/lib" "$UNITDIR" "$SBIN"
install -m 0755 "$KIT/bin/ollama1-logguard" "$LIBDIR/bin/ollama1-logguard"
install -m 0644 "$KIT/lib/o1logguard.py" "$LIBDIR/lib/o1logguard.py"
install -m 0644 "$KIT/systemd/ollama1-logguard.service" "$UNITDIR/ollama1-logguard.service"
install -m 0644 "$KIT/systemd/ollama1-logguard.timer" "$UNITDIR/ollama1-logguard.timer"
ln -sfn /usr/local/lib/ollama1/bin/ollama1-logguard "$SBIN/ollama1-logguard"
echo "installed the log guard in /usr/local/lib/ollama1 and /etc/systemd/system"

systemctl daemon-reload
systemctl enable --now ollama1-logguard.timer
echo "one pass now:"
if systemctl start ollama1-logguard.service; then
  echo "  it ran (what it did: journalctl -u ollama1-logguard -n 20)"
else
  echo "  it FAILED: journalctl -u ollama1-logguard -n 50 says why (the timer is on and tries again every minute)"
fi
echo "timer:"
systemctl list-timers --all --no-pager ollama1-logguard.timer
