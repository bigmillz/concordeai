#!/usr/bin/env bash
# ConcordeGo at https://go.flyconcordfly.com, served from this Mac.
#
# One idempotent script, the sibling of the repo root's go-live.sh (the AI
# app at ai.millertechnology.net). It installs:
#   * a LaunchAgent that keeps concorde-travel/server.py running on :9897,
#     with / serving the current interface mockup (CONCORDEGO_ROOT=mock-10)
#   * a Cloudflare named tunnel, "concordego", with its OWN config file and
#     its own LaunchAgent, so the AI app's tunnel and this one never touch
#     each other's config
#   * the DNS route go.flyconcordfly.com -> the tunnel
#
# The one human step: if cloudflared has never been authorised on this Mac,
# a browser opens; click the flyconcordfly.com row, then Authorize. Re-run
# the script afterwards and it finishes. Until a proper host exists, the
# address works while this laptop is awake and online.
#
# Nothing metered is reachable from the public address: server.py refuses a
# live flight search or a narrator call that arrives through the tunnel, so
# a visitor cannot spend the Duffel quota or the Anthropic key. Set
# CONCORDEGO_FLIGHT_KEY in your own shell for local live searches; it is
# NOT written into the LaunchAgent.
set -euo pipefail

HOST="go.flyconcordfly.com"
TUNNEL="concordego"
LABEL="com.flyconcordfly.go"
SERVE_PORT=9897
ROOT_MOCK="mock-10"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGS="$HOME/Library/Logs/ConcordeGo"; mkdir -p "$LOGS"
AGENTS="$HOME/Library/LaunchAgents"; mkdir -p "$AGENTS"
UID_N="$(id -u)"
PY="$(command -v python3)"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

load_agent(){
  launchctl bootout "gui/$UID_N/$1" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$AGENTS/$1.plist"
  launchctl kickstart -k "gui/$UID_N/$1" 2>/dev/null || true
}

# ---------------------------------------------------------------- server
say "server: $HERE/server.py on :$SERVE_PORT, / = $ROOT_MOCK"
cat > "$AGENTS/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$HERE/server.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>CONCORDEGO_PORT</key><string>$SERVE_PORT</string>
    <key>CONCORDEGO_ROOT</key><string>$ROOT_MOCK</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/server.log</string>
  <key>StandardErrorPath</key><string>$LOGS/server.log</string>
</dict></plist>
PLIST
load_agent "$LABEL"
for _ in $(seq 1 20); do
  curl -sf -o /dev/null "http://127.0.0.1:$SERVE_PORT/" && break
  sleep 0.5
done
curl -sf -o /dev/null "http://127.0.0.1:$SERVE_PORT/" && echo "  up: http://127.0.0.1:$SERVE_PORT/" \
  || { echo "  server did not answer; see $LOGS/server.log"; exit 1; }

# ---------------------------------------------------------------- tunnel
if ! command -v cloudflared >/dev/null; then
  say "cloudflared missing - installing via homebrew"
  brew install cloudflared
fi

if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  say "tunnel not authorised yet - your browser is opening Cloudflare now."
  echo "  Click the flyconcordfly.com row, then Authorize. Waiting up to 5 minutes..."
  cloudflared tunnel login &
  LOGIN_PID=$!
  for _ in $(seq 1 60); do
    [ -f "$HOME/.cloudflared/cert.pem" ] && break
    sleep 5
  done
  kill "$LOGIN_PID" 2>/dev/null || true
fi

if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  say "tunnel still not authorised - the server IS installed."
  echo "  Local: http://127.0.0.1:$SERVE_PORT/   Re-run this script after clicking Authorize."
  exit 0
fi

if ! cloudflared tunnel list 2>/dev/null | grep -q " $TUNNEL "; then
  cloudflared tunnel create "$TUNNEL"
fi
TID=$(cloudflared tunnel list 2>/dev/null | awk -v t="$TUNNEL" '$2==t{print $1}')
[ -n "$TID" ] || { echo "could not find the tunnel id for $TUNNEL"; exit 1; }
CFG="$HOME/.cloudflared/$TUNNEL.yml"
cat > "$CFG" <<YML
tunnel: $TID
credentials-file: $HOME/.cloudflared/$TID.json
ingress:
  - hostname: $HOST
    service: http://localhost:$SERVE_PORT
  - service: http_status:404
YML

if ! cloudflared tunnel route dns "$TUNNEL" "$HOST" 2>/dev/null; then
  echo "  DNS route not added by cloudflared (already there, or the zone is not on this account)."
  echo "  If $HOST does not resolve, add a proxied CNAME in the flyconcordfly.com zone:"
  echo "    go  ->  $TID.cfargotunnel.com"
fi

cat > "$AGENTS/$LABEL-tunnel.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL-tunnel</string>
  <key>ProgramArguments</key>
  <array><string>$(command -v cloudflared)</string>
  <string>tunnel</string><string>--config</string><string>$CFG</string>
  <string>run</string><string>$TUNNEL</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/tunnel.log</string>
  <key>StandardErrorPath</key><string>$LOGS/tunnel.log</string>
</dict></plist>
PLIST
load_agent "$LABEL-tunnel"

say "LIVE: https://$HOST"
echo "  while this Mac is awake and online. Logs: $LOGS"
echo "  To stop:  launchctl bootout gui/$UID_N/$LABEL-tunnel; launchctl bootout gui/$UID_N/$LABEL"
