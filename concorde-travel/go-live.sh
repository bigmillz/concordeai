#!/usr/bin/env bash
# ConcordeGo at https://go.flyconcordefly.com, served from this Mac.
#
# One idempotent script, the sibling of the repo root's go-live.sh (the AI
# app at ai.millertechnology.net). It installs:
#   * a LaunchAgent that keeps concorde-travel/server.py running on :9897,
#     with / serving the current interface mockup (CONCORDEGO_ROOT=mock-10)
#   * a Cloudflare named tunnel, "concordego", with its OWN config file and
#     its own LaunchAgent, so the AI app's tunnel and this one never touch
#     each other's config
#   * the DNS route go.flyconcordefly.com -> the tunnel
#
# The one human step: if cloudflared has never been authorised on this Mac,
# a browser opens; click the flyconcordefly.com row, then Authorize. Re-run
# the script afterwards and it finishes. Until a proper host exists, the
# address works while this laptop is awake and online.
#
# A visitor through the tunnel spends nothing until they sign in (Cloudflare
# Access, set up by access.sh); a signed-in friend gets the per-user allowance
# in server.py. The keys are never written into the LaunchAgent plist (it is
# world-readable): the agent sources ~/.concordego/env (created here, 0600)
# before it starts the server, so put ANTHROPIC_API_KEY there, and put the
# Duffel token there as CONCORDEGO_FLIGHT_KEY or in ~/.concordego/cloud.json.
# After editing that file:  launchctl kickstart -k gui/$(id -u)/com.flyconcordefly.go
set -euo pipefail

HOST="go.flyconcordefly.com"
TUNNEL="concordego"
LABEL="com.flyconcordefly.go"
SERVE_PORT=9897
ROOT_MOCK="mock-10"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGS="$HOME/Library/Logs/ConcordeGo"; mkdir -p "$LOGS"
AGENTS="$HOME/Library/LaunchAgents"; mkdir -p "$AGENTS"
UID_N="$(id -u)"
SYS_PY="$(command -v python3)"
KEYS="$HOME/.concordego/env"

say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }

load_agent(){
  launchctl bootout "gui/$UID_N/$1" 2>/dev/null || true
  # bootout returns before the old instance is gone, and cloudflared drains
  # its connections for a few seconds on the way out. A bootstrap while the
  # old one lingers fails with "Bootstrap failed: 5: Input/output error", so
  # wait until launchd has forgotten it, then load, retrying for a while.
  for _ in $(seq 1 60); do
    launchctl print "gui/$UID_N/$1" >/dev/null 2>&1 || break
    sleep 0.5
  done
  local n=0
  until launchctl bootstrap "gui/$UID_N" "$AGENTS/$1.plist" 2>/dev/null; do
    n=$((n+1))
    if [ "$n" -ge 30 ]; then
      echo "  launchctl would not load $1. Try by hand:"
      echo "    launchctl bootout gui/$UID_N/$1; sleep 10; launchctl bootstrap gui/$UID_N $AGENTS/$1.plist"
      return 1
    fi
    sleep 1
  done
  launchctl kickstart -k "gui/$UID_N/$1" 2>/dev/null || true
}

# ---------------------------------------------------------------- keys
if [ ! -f "$KEYS" ]; then
  mkdir -p "$(dirname "$KEYS")"; (umask 077; cat > "$KEYS" <<'ENV'
# Sourced by the ConcordeGo LaunchAgent before server.py starts. Mode 0600.
# Uncomment and fill in; then: launchctl kickstart -k gui/$(id -u)/com.flyconcordefly.go
#export ANTHROPIC_API_KEY=sk-ant-...        # the wish box and the narrator (Claude Opus 5)
#export CONCORDEGO_FLIGHT_KEY=duffel_live_... # or put it in ~/.concordego/cloud.json instead
ENV
  ); echo "  wrote $KEYS (empty template, 0600)"
fi
chmod 600 "$KEYS"

# ---------------------------------------------------------------- python
# Homebrew's python3 refuses pip installs (PEP 668, "externally managed"), so
# the server runs on its own venv holding the one non-stdlib package, the
# anthropic SDK. server.py is stdlib and runs there unchanged. With no network
# the install is skipped and the wish box and narrator fall back to the
# template until the script is run again.
VENV="$HOME/.concordego/venv"
if [ ! -x "$VENV/bin/python3" ]; then
  say "python: making a venv at $VENV"
  "$SYS_PY" -m venv "$VENV"
fi
PY="$VENV/bin/python3"
if ! "$PY" -c "import anthropic" 2>/dev/null; then
  say "python: installing the anthropic SDK into the venv"
  "$PY" -m pip install -q --upgrade pip anthropic \
    || echo "  could not install anthropic (offline?): the wish box and narrator use the template until you re-run this"
fi

# ---------------------------------------------------------------- server
say "server: $HERE/server.py on :$SERVE_PORT, / = $ROOT_MOCK"
cat > "$AGENTS/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/sh</string><string>-c</string>
  <string>[ -f "$KEYS" ] &amp;&amp; . "$KEYS"; exec "\$0" "\$1"</string>
  <string>$PY</string><string>$HERE/server.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>CONCORDEGO_PORT</key><string>$SERVE_PORT</string>
    <key>CONCORDEGO_ROOT</key><string>$ROOT_MOCK</string>
    <key>CONCORDEGO_LOG</key><string>$LOGS/server.log</string>
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
  echo "  Click the flyconcordefly.com row, then Authorize. Waiting up to 5 minutes..."
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

# The tunnel is looked up by exact name from cloudflared's own JSON, not by
# grepping its table: the table grep once missed an existing tunnel and the
# create that followed failed with "tunnel with name already exists".
tunnel_id(){
  cloudflared tunnel list --name "$TUNNEL" --output json 2>/dev/null | "$PY" -c '
import json, sys
try: ts = json.load(sys.stdin)
except Exception: ts = []
ts = [t for t in ts if t.get("name") == sys.argv[1] and not str(t.get("deleted_at") or "").startswith("2")]
print(ts[0]["id"] if ts else "")' "$TUNNEL"
}
TID=$(tunnel_id)
if [ -z "$TID" ]; then
  cloudflared tunnel create "$TUNNEL" || true      # "already exists" is fine: it is looked up again below
  TID=$(tunnel_id)
fi
if [ -z "$TID" ]; then
  echo "could not find or create the tunnel $TUNNEL. What cloudflared sees:"
  cloudflared tunnel list --name "$TUNNEL" || true
  echo "  An outdated cloudflared can be the cause:  brew upgrade cloudflared"
  exit 1
fi
CRED="$HOME/.cloudflared/$TID.json"
if [ ! -f "$CRED" ]; then
  say "the tunnel $TUNNEL exists but its credentials file is not on this Mac."
  echo "  It was created on another machine. Make a fresh one here, then re-run:"
  echo "    cloudflared tunnel delete -f $TUNNEL"
  exit 1
fi
CFG="$HOME/.cloudflared/$TUNNEL.yml"
cat > "$CFG" <<YML
tunnel: $TID
credentials-file: $CRED
ingress:
  - hostname: $HOST
    service: http://localhost:$SERVE_PORT
  - service: http_status:404
YML

# The DNS route is what makes the name exist. cloudflared's own words are shown
# (a silent failure here once hid a misspelt zone for a day), and the record is
# then checked at a public resolver, so "LIVE" below is never printed for a name
# that does not resolve.
say "dns: $HOST -> $TID.cfargotunnel.com"
ROUTE_OUT=$(cloudflared tunnel route dns "$TUNNEL" "$HOST" 2>&1) || true
echo "  cloudflared: $(echo "$ROUTE_OUT" | tail -1)"
DNS_OK=""
for _ in $(seq 1 12); do
  if dig +short "$HOST" @1.1.1.1 2>/dev/null | grep -q .; then DNS_OK=1; break; fi
  sleep 5
done
if [ -n "$DNS_OK" ]; then echo "  resolves: $(dig +short "$HOST" @1.1.1.1 | head -1)"
else
  echo "  $HOST does not resolve yet at 1.1.1.1. If cloudflared reported an error above, fix that; otherwise add the record by hand:"
  echo "    zone flyconcordefly.com, DNS, add record: type CNAME, name go, target $TID.cfargotunnel.com, proxied ON"
  echo "  Your Mac may also be holding an old 'no such name' answer:  sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder"
fi

say "tunnel: $TUNNEL -> https://$HOST"
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
echo "  Keys:     $KEYS  (then: launchctl kickstart -k gui/$UID_N/$LABEL)"
echo "  To stop:  launchctl bootout gui/$UID_N/$LABEL-tunnel; launchctl bootout gui/$UID_N/$LABEL"
