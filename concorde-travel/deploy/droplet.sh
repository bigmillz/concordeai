#!/usr/bin/env bash
# ConcordeGo on a small Ubuntu droplet: the server as a systemd service, the
# anthropic SDK in a venv (the one non-stdlib dependency, for the narrator and
# the wish box on Claude Opus 5), and a Cloudflare tunnel carrying
# go.flyconcordfly.com to it. Cloudflare Access (access.sh) is the door.
#
#   ssh root@<droplet>
#   curl -fsSL https://raw.githubusercontent.com/bigmillz/concordeai/main/concorde-travel/deploy/droplet.sh | bash
#   # or: git clone ... && bash concordeai/concorde-travel/deploy/droplet.sh
#
# Secrets go in /etc/concordego.env (0600), never in the repo:
#   CONCORDEGO_FLIGHT_KEY=duffel_live_...      the Duffel key
#   ANTHROPIC_API_KEY=sk-ant-...               for the narrator and the wish box
# The one human step is `cloudflared tunnel login` the first time (a URL is
# printed; open it, pick flyconcordfly.com, Authorize). Re-run after that.
set -euo pipefail
REPO="${REPO:-https://github.com/bigmillz/concordeai.git}"
BRANCH="${BRANCH:-main}"
HOST="${HOST:-go.flyconcordfly.com}"
TUNNEL="${TUNNEL:-concordego}"
APP=/opt/concordego
ENVF=/etc/concordego.env
SVC=concordego
PORT="${CONCORDEGO_PORT:-9897}"
ROOT_MOCK="${CONCORDEGO_ROOT:-mock-10}"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq git python3 python3-venv curl >/dev/null

say "user and code"
id -u concordego >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin concordego
if [ -d "$APP/.git" ]; then git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" checkout -q "$BRANCH" && git -C "$APP" pull -q --ff-only origin "$BRANCH"
else git clone -q --branch "$BRANCH" --depth 50 "$REPO" "$APP"; fi
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip anthropic >/dev/null
mkdir -p /var/lib/concordego && chown -R concordego:concordego "$APP" /var/lib/concordego

say "environment file $ENVF"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<ENV
# ConcordeGo. Read by systemd; keys never leave this file.
CONCORDEGO_PORT=$PORT
CONCORDEGO_ROOT=$ROOT_MOCK
HOME=/var/lib/concordego
# CONCORDEGO_FLIGHT_KEY=duffel_live_...
# ANTHROPIC_API_KEY=sk-ant-...
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
ReadWritePaths=/var/lib/concordego
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable -q "$SVC" && systemctl restart "$SVC"
sleep 1.5; curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && echo "  up on 127.0.0.1:$PORT" || { journalctl -u "$SVC" -n 20 --no-pager; exit 1; }

say "cloudflared"
if ! command -v cloudflared >/dev/null; then
  mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq && apt-get install -y -qq cloudflared >/dev/null
fi
if [ ! -f /root/.cloudflared/cert.pem ]; then
  say "tunnel not authorised: run  cloudflared tunnel login  , open the URL it prints, pick flyconcordfly.com, then re-run this script."
  exit 0
fi
cloudflared tunnel list 2>/dev/null | grep -q " $TUNNEL " || cloudflared tunnel create "$TUNNEL"
TID=$(cloudflared tunnel list 2>/dev/null | awk -v t="$TUNNEL" '$2==t{print $1}')
mkdir -p /etc/cloudflared
cat > /etc/cloudflared/config.yml <<YML
tunnel: $TID
credentials-file: /root/.cloudflared/$TID.json
ingress:
  - hostname: $HOST
    service: http://localhost:$PORT
  - service: http_status:404
YML
cloudflared tunnel route dns "$TUNNEL" "$HOST" 2>/dev/null || echo "  DNS route exists or the zone is elsewhere: CNAME go -> $TID.cfargotunnel.com"
systemctl is-enabled cloudflared >/dev/null 2>&1 || cloudflared service install
systemctl restart cloudflared
say "LIVE: https://$HOST   (put Access in front of it: concorde-travel/access.sh)"
