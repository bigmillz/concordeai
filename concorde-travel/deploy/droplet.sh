#!/usr/bin/env bash
# ConcordeGo on a small Ubuntu droplet, updating itself from GitHub every hour.
#
#   ssh root@<droplet>
#   curl -fsSL https://raw.githubusercontent.com/bigmillz/concordeai/BRANCH/concorde-travel/deploy/droplet.sh \
#     | bash -s -- BRANCH CLOUDFLARE_TUNNEL_TOKEN
#
# What it sets up, all idempotent (re-run any time):
#   - /opt/concordego          the repo, on BRANCH, owned by a system user
#   - /opt/concordego/venv     python3 + the anthropic SDK (the one non-stdlib package)
#   - concordego.service       server.py on 127.0.0.1:9897, serving mock-10 at /
#   - concordego-update.timer  hourly: fetch BRANCH, and if HEAD moved, reset to it and restart the service
#   - /api/admin on the served site the same by hand: which commit is live, what GitHub has, update now, the logs;
#                              for the emails in CONCORDEGO_OWNERS (put them behind Access too: access.sh does)
#   - cloudflared              a Cloudflare tunnel made in the Zero Trust dashboard, run from its token;
#                              the dashboard owns the public hostname and its DNS record, so nothing here
#                              touches DNS (the laptop route step that kept failing is gone)
#   - /etc/concordego.env      the keys, 0600, read by systemd, never in the repo
#
# Secrets go in /etc/concordego.env after the first run:
#   CONCORDEGO_FLIGHT_KEY=duffel_live_...      the Duffel key
#   ANTHROPIC_API_KEY=sk-ant-...               for the narrator and the wish box
#   CONCORDEGO_OWNERS=pat@millertechnology.net signed-in people who are not metered
# then: systemctl restart concordego
set -euo pipefail
BRANCH="${1:-${BRANCH:-main}}"
TUNNEL_TOKEN="${2:-${CLOUDFLARE_TUNNEL_TOKEN:-}}"
REPO="${REPO:-https://github.com/bigmillz/concordeai.git}"
APP=/opt/concordego
ENVF=/etc/concordego.env
SVC=concordego
PORT="${CONCORDEGO_PORT:-9897}"
ROOT_MOCK="${CONCORDEGO_ROOT:-mock-10}"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq git python3 python3-venv curl ufw >/dev/null

say "code: $REPO on $BRANCH -> $APP"
id -u concordego >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin concordego
usermod -aG systemd-journal concordego   # /admin shows the service journal
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" --depth 50 "$REPO" "$APP"
fi
# --system, not --global: the updater runs from systemd with no HOME, so root's
# ~/.gitconfig is never read there and every hourly pull died on "dubious ownership"
git config --system --add safe.directory "$APP" >/dev/null 2>&1 || true
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip anthropic >/dev/null
mkdir -p /var/lib/concordego && chown -R concordego:concordego "$APP" /var/lib/concordego
echo "  at $(git -C "$APP" rev-parse --short HEAD): $(git -C "$APP" log -1 --format=%s | cut -c1-70)"

say "environment file $ENVF"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<ENV
# ConcordeGo. Read by systemd; the keys never leave this file.
CONCORDEGO_PORT=$PORT
CONCORDEGO_ROOT=$ROOT_MOCK
HOME=/var/lib/concordego
# CONCORDEGO_FLIGHT_KEY=duffel_live_...
# ANTHROPIC_API_KEY=sk-ant-...
# CONCORDEGO_OWNERS=pat@millertechnology.net
# CONCORDEGO_PUBLIC=1                 always set on a served box: nothing is local here, so a request without the
#                                     proxy's headers is a stranger, never the owner (server.py _remote)
# CONCORDEGO_PEXELS_KEY=...          photos of the origin and destination on the shortlist tiles (pexels.com/api, free)
# CONCORDEGO_SERPAPI_KEY=...          the Delta supplement: Google Flights through SerpApi (serpapi.com), its own key and counters
# CONCORDEGO_ACCESS_TEAM=yourteam    the Zero Trust team name (the part before .cloudflareaccess.com): the server verifies the sign-in cookie against it
# CONCORDEGO_USER_SEARCHES=20
# CONCORDEGO_USER_WISHES=200
ENV
  chmod 600 "$ENVF"; echo "  written; add the keys, then: systemctl restart $SVC"
else echo "  exists, left alone"; fi

say "service $SVC"
cat > /etc/systemd/system/$SVC.service <<UNIT
[Unit]
Description=ConcordeGo flight re-ranker
After=network-online.target
Wants=network-online.target

[Service]
User=concordego
Group=concordego
WorkingDirectory=$APP/concorde-travel
EnvironmentFile=$ENVF
ExecStart=$APP/venv/bin/python3 $APP/concorde-travel/server.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/concordego $APP
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable -q "$SVC" && systemctl restart "$SVC"
for _ in $(seq 1 20); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.5; done
curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && echo "  up on 127.0.0.1:$PORT" || { journalctl -u "$SVC" -n 20 --no-pager; exit 1; }

say "hourly update from GitHub"
cat > /usr/local/bin/concordego-update <<'UPD'
#!/usr/bin/env bash
# Pull the branch the droplet tracks; restart the service only when HEAD moved.
set -euo pipefail
APP=/opt/concordego
cd "$APP"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
before=$(git rev-parse HEAD)
git fetch -q origin "$BRANCH"
after=$(git rev-parse "origin/$BRANCH")
if [ "$before" = "$after" ]; then echo "up to date at ${before:0:7} on $BRANCH"; exit 0; fi
git reset -q --hard "origin/$BRANCH"
chown -R concordego:concordego "$APP"
"$APP/venv/bin/pip" install -q --upgrade anthropic >/dev/null 2>&1 || true
systemctl restart concordego
echo "updated ${before:0:7} -> ${after:0:7} on $BRANCH: $(git log -1 --format=%s | cut -c1-70)"
UPD
chmod 755 /usr/local/bin/concordego-update
cat > /etc/systemd/system/concordego-update.service <<UNIT
[Unit]
Description=ConcordeGo: update from GitHub
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/concordego-update
UNIT
cat > /etc/systemd/system/concordego-update.timer <<UNIT
[Unit]
Description=ConcordeGo: check GitHub every hour

[Timer]
OnBootSec=3min
OnUnitActiveSec=1h
RandomizedDelaySec=5min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload && systemctl enable -q --now concordego-update.timer
echo "  every hour; by hand:  concordego-update"

say "firewall"
ufw allow OpenSSH >/dev/null; ufw --force enable >/dev/null; echo "  ssh only; the server listens on 127.0.0.1 and the tunnel dials out"
# ssh by key only, root included; and security updates on their own, with a reboot at a quiet hour
# (09:30 UTC, 05:30 New York) only when a kernel asks for one (per Patrick, 2026-09-21)
mkdir -p /etc/ssh/sshd_config.d
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/50-concordego.conf
sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true)
apt-get install -y -qq unattended-upgrades >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Unattended-Upgrade "1";
APT
cat > /etc/apt/apt.conf.d/52concordego-unattended <<'APT'
// ConcordeGo: security updates on their own, reboot at a quiet hour when a kernel asks for it
Unattended-Upgrade::Allowed-Origins { "${distro_id}:${distro_codename}-security"; "${distro_id}ESMApps:${distro_codename}-apps-security"; "${distro_id}ESM:${distro_codename}-infra-security"; };
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "09:30";
Unattended-Upgrade::SyslogEnable "true";
APT
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades >/dev/null 2>&1 || true
echo "  security updates unattended; reboots at 09:30 UTC only when required"

say "cloudflared"
if ! command -v cloudflared >/dev/null; then
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq && apt-get install -y -qq cloudflared >/dev/null
fi
if [ -n "$TUNNEL_TOKEN" ]; then
  # a dashboard-made tunnel: the token carries everything, the dashboard owns the hostname and its DNS
  if systemctl is-enabled cloudflared >/dev/null 2>&1; then cloudflared service uninstall >/dev/null 2>&1 || true; fi
  cloudflared service install "$TUNNEL_TOKEN" >/dev/null
  systemctl restart cloudflared; sleep 3
  systemctl is-active -q cloudflared && echo "  tunnel running from the dashboard token" || { journalctl -u cloudflared -n 20 --no-pager; exit 1; }
  say "LIVE once the dashboard's public hostname points at http://localhost:$PORT. Then the door: concorde-travel/access.sh (from any machine)."
else
  say "no tunnel token given. Make the tunnel in the dashboard (one.dash.cloudflare.com > Networks > Tunnels > Create a tunnel > Cloudflared),"
  echo "  name it, copy the token from the install command it shows, add a Public hostname (go . flyconcordefly.com -> HTTP localhost:$PORT),"
  echo "  then:  cloudflared service install <token>   or re-run this script with the token as its second argument."
fi
