#!/usr/bin/env bash
# System updates from the ConcordeGo admin page (per Patrick, 2026-09-24): a nightly CHECK, never an
# automatic upgrade ("it'll be system-wide and could break something"), and two buttons, Update and
# Update and restart. Run as root, from the checkout (droplet.sh calls it); idempotent.
#
# How the page, which runs as the unprivileged `concordego` user with NoNewPrivileges, gets root to
# upgrade: it cannot, and never runs anything as root. It writes ONE word, "update" or "reboot", to
# /var/lib/concordego/sysupdate-request (its own directory). A root .path unit watches that file and
# starts concordego-sysupdate.service, which reads at most 16 letters from it, deletes it, and runs the
# root-owned copy of update-system.sh. Anything else in the file is ignored. Root never writes into a
# directory the web user can write: the status the page reads lives in /var/lib/concordego-sys (root,
# 0755), and the logs in /var/log/update-system (root, 0755, files 0644).
#
#   concordego-updates-check.timer   daily at 07:00 UTC (03:00 New York) and 3 minutes after boot:
#                                    refresh the package lists and write what is pending. Installs nothing.
#   concordego-sysupdate.path        starts concordego-sysupdate.service when the page asks
#
# update-system.sh is COPIED to /usr/local/sbin, root-owned: the checkout belongs to the web user, and a
# root service must never run a file that user could edit. Re-run this script to install a new version.

set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS=/var/lib/concordego-sys/status.json
REQUEST=/var/lib/concordego/sysupdate-request

install -o root -g root -m 0755 "$HERE/update-system.sh" /usr/local/sbin/update-system.sh
install -d -o root -g root -m 0755 /var/lib/concordego-sys /var/log/update-system
[ -d /var/lib/concordego ] || { echo "no /var/lib/concordego: run droplet.sh first"; exit 1; }

cat > /usr/local/sbin/concordego-sysupdate-run <<RUN
#!/bin/bash
# Started by concordego-sysupdate.path when the admin page asks. The request file is data, not a
# command: a regular file (never a symlink or a pipe), at most 16 bytes, letters only, one of two words.
set -euo pipefail
REQ=$REQUEST
mode=""
if [ -f "\$REQ" ] && [ ! -L "\$REQ" ]; then mode=\$(head -c 16 "\$REQ" | tr -cd 'a-z'); fi
rm -f "\$REQ"
case "\$mode" in
  update) exec /usr/local/sbin/update-system.sh --status $STATUS ;;
  reboot) exec /usr/local/sbin/update-system.sh --reboot --status $STATUS ;;
  *) echo "ignored a request that was not 'update' or 'reboot'"; exit 0 ;;
esac
RUN
chmod 0755 /usr/local/sbin/concordego-sysupdate-run

cat > /etc/systemd/system/concordego-sysupdate.path <<UNIT
[Unit]
Description=ConcordeGo admin: system update requested

[Path]
PathExists=$REQUEST
Unit=concordego-sysupdate.service

[Install]
WantedBy=paths.target
UNIT

cat > /etc/systemd/system/concordego-sysupdate.service <<'UNIT'
[Unit]
Description=ConcordeGo admin: run the requested system update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/concordego-sysupdate-run
TimeoutStartSec=3h
UNIT

cat > /etc/systemd/system/concordego-updates-check.service <<UNIT
[Unit]
Description=ConcordeGo admin: check for system updates (installs nothing)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/update-system.sh --check --status $STATUS
UNIT

cat > /etc/systemd/system/concordego-updates-check.timer <<'UNIT'
[Unit]
Description=ConcordeGo admin: nightly check for system updates

[Timer]
OnCalendar=*-*-* 07:00:00 UTC
OnBootSec=3min
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable -q --now concordego-sysupdate.path concordego-updates-check.timer
[ -f "$STATUS" ] || systemctl start --no-block concordego-updates-check.service
echo "  system updates: nightly check at 07:00 UTC, installs only from the admin page"
