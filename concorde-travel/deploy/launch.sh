#!/usr/bin/env bash
# ConcordeGo, end to end, from a laptop: a droplet, a Cloudflare tunnel with its DNS
# record, the installer over ssh, the keys, the Access door, and a check that it
# answers. One command; every secret is read from the environment and written
# only to the droplet's /etc/concordego.env (0600). Re-runnable: an existing
# droplet, tunnel, record or application is reused, never duplicated.
#
#   brew install doctl jq            # doctl once; jq is optional (python3 stands in)
#   export DIGITALOCEAN_ACCESS_TOKEN=dop_v1_...     # DigitalOcean > API > Generate (read + write)
#   export CF_EMAIL=you@example.com CF_GLOBAL_KEY=...   # or CF_API_TOKEN with Tunnel, DNS, Zone and Access edit
#   export CONCORDEGO_FLIGHT_KEY=duffel_live_...  ANTHROPIC_API_KEY=sk-ant-...  CONCORDEGO_PEXELS_KEY=...  (Pexels optional: tile photos)
#   ALLOW="you@example.com,friend@example.com" ./concorde-travel/deploy/launch.sh
#
# Optional: BRANCH (default: the branch this checkout is on), REGION (nyc3),
# SIZE (s-1vcpu-1gb), NAME (concordego), HOST (go.flyconcordefly.com), TEAM
# (the Zero Trust team name, only the first time an account uses Access),
# SSH_KEY (a DigitalOcean ssh-key id or name; default: the first one listed).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; TRAVEL="$(dirname "$HERE")"
# doctl may already hold its token (doctl auth init); the env var is only needed when it does not
command -v doctl >/dev/null || { echo "doctl is missing:  brew install doctl"; exit 1; }
doctl account get >/dev/null 2>&1 || : "${DIGITALOCEAN_ACCESS_TOKEN:?doctl is not authorised: run  doctl auth init  , or set DIGITALOCEAN_ACCESS_TOKEN}"
: "${ALLOW:?set ALLOW to a comma-separated list of emails}"
[ -n "${CF_GLOBAL_KEY:-}" ] && : "${CF_EMAIL:?set CF_EMAIL beside CF_GLOBAL_KEY}"
[ -n "${CF_GLOBAL_KEY:-}" ] || : "${CF_API_TOKEN:?set CF_API_TOKEN, or CF_EMAIL and CF_GLOBAL_KEY}"
HOST="${HOST:-go.flyconcordefly.com}"; NAME="${NAME:-concordego}"; REGION="${REGION:-nyc3}"; SIZE="${SIZE:-s-1vcpu-1gb}"
BRANCH="${BRANCH:-$(git -C "$TRAVEL" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
PORT=9897; APEX=$(echo "$HOST" | awk -F. '{print $(NF-1)"."$NF}'); SUB="${HOST%.$APEX}"
OWNERS="${OWNERS:-${ALLOW%%,*}}"
say(){ printf '\n\033[1m%s\033[0m\n' "$*"; }
jq_(){ python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }
ROOT="https://api.cloudflare.com/client/v4"
if [ -n "${CF_GLOBAL_KEY:-}" ]; then AUTH=(-H "X-Auth-Email: $CF_EMAIL" -H "X-Auth-Key: $CF_GLOBAL_KEY"); else AUTH=(-H "Authorization: Bearer $CF_API_TOKEN"); fi
cf(){ curl -sS -X "$1" "$ROOT$2" "${AUTH[@]}" -H "Content-Type: application/json" ${3:+--data "$3"}; }

# ------------------------------------------------------------------ zone
say "zone $APEX"
ZONE=$(cf GET "/zones?name=$APEX" | jq_ 'r=d.get("result") or []
print(r[0]["id"] + " " + r[0]["account"]["id"] if r else "")')
[ -n "$ZONE" ] || { echo "  $APEX is not a zone these Cloudflare credentials can see. The Global API Key of the login that owns it sees it."; exit 1; }
ZONE_ID="${ZONE%% *}"; export CF_ACCOUNT_ID="${ZONE##* }"
echo "  zone $ZONE_ID in account $CF_ACCOUNT_ID"

# --------------------------------------------------------------- droplet
say "droplet $NAME ($REGION, $SIZE)"
IP=$(doctl compute droplet list --format Name,PublicIPv4 --no-header | awk -v n="$NAME" '$1==n{print $2}')
if [ -z "$IP" ]; then
  KEY="${SSH_KEY:-$(doctl compute ssh-key list --format ID --no-header | head -1)}"
  [ -n "$KEY" ] || { echo "  no ssh key on the DigitalOcean account. Add one:  doctl compute ssh-key import laptop --public-key-file ~/.ssh/id_ed25519.pub"; exit 1; }
  IP=$(doctl compute droplet create "$NAME" --region "$REGION" --size "$SIZE" --image ubuntu-24-04-x64 --ssh-keys "$KEY" --wait --format PublicIPv4 --no-header)
  echo "  created, $IP"
else echo "  exists, $IP"; fi
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o BatchMode=yes "root@$IP")
for _ in $(seq 1 40); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
"${SSH[@]}" true 2>/dev/null || { echo "  cannot ssh to root@$IP: is your ssh key the one on the droplet?"; exit 1; }
echo "  ssh ok"

# ---------------------------------------------------------------- tunnel
# A remotely-managed tunnel: Cloudflare holds its config, the droplet runs it
# from a token, and the DNS record points the hostname at it.
say "tunnel $NAME-droplet"
TUN=$(cf GET "/accounts/$CF_ACCOUNT_ID/cfd_tunnel?name=$NAME-droplet&is_deleted=false" | jq_ 'r=[t for t in (d.get("result") or []) if not t.get("deleted_at")]
print(r[0]["id"] if r else "")')
if [ -z "$TUN" ]; then
  TUN=$(cf POST "/accounts/$CF_ACCOUNT_ID/cfd_tunnel" "{\"name\":\"$NAME-droplet\",\"config_src\":\"cloudflare\"}" | jq_ 'print(d["result"]["id"]) if d.get("success") else sys.exit("  could not create the tunnel: " + json.dumps(d.get("errors")))')
  echo "  created $TUN"
else echo "  exists $TUN"; fi
cf PUT "/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUN/configurations" "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST\",\"service\":\"http://localhost:$PORT\"},{\"service\":\"http_status:404\"}]}}" \
  | jq_ 'print("  route: '"$HOST"' -> localhost:'"$PORT"'") if d.get("success") else sys.exit("  could not set the tunnel route: " + json.dumps(d.get("errors")))'
TOKEN=$(cf GET "/accounts/$CF_ACCOUNT_ID/cfd_tunnel/$TUN/token" | jq_ 'print(d["result"]) if d.get("success") else sys.exit("  could not read the tunnel token: " + json.dumps(d.get("errors")))')

say "dns $HOST"
REC=$(cf GET "/zones/$ZONE_ID/dns_records?name=$HOST" | jq_ 'r=d.get("result") or []
print(r[0]["id"] if r else "")')
BODY="{\"type\":\"CNAME\",\"name\":\"$SUB\",\"content\":\"$TUN.cfargotunnel.com\",\"proxied\":true,\"ttl\":1}"
if [ -z "$REC" ]; then cf POST "/zones/$ZONE_ID/dns_records" "$BODY" | jq_ 'print("  created CNAME -> " + d["result"]["content"]) if d.get("success") else sys.exit("  could not add the record: " + json.dumps(d.get("errors")))'
else cf PUT "/zones/$ZONE_ID/dns_records/$REC" "$BODY" | jq_ 'print("  updated CNAME -> " + d["result"]["content"]) if d.get("success") else sys.exit("  could not update the record: " + json.dumps(d.get("errors")))'; fi

# the Zero Trust team: the server verifies Access's sign-in cookie against its keys, so it must know the name
ACCESS_TEAM=$(cf GET "/accounts/$CF_ACCOUNT_ID/access/organizations" | jq_ 'r = d.get("result") or {}
print((r.get("auth_domain") or "").split(".")[0])')
[ -n "$ACCESS_TEAM" ] && echo "  Zero Trust team: $ACCESS_TEAM" || echo "  no Zero Trust team yet: access.sh (below) makes one; re-run this afterwards so the server learns its name"

# --------------------------------------------------------------- install
say "install on the droplet ($BRANCH)"
"${SSH[@]}" "curl -fsSL https://raw.githubusercontent.com/bigmillz/concordeai/$BRANCH/concorde-travel/deploy/droplet.sh | bash -s -- '$BRANCH' '$TOKEN'" 2>&1 | sed 's/^/  /'

say "keys -> /etc/concordego.env on the droplet"
# sent over the ssh channel, never on a command line
{ [ -n "${CONCORDEGO_FLIGHT_KEY:-}" ] && printf 'CONCORDEGO_FLIGHT_KEY=%s\n' "$CONCORDEGO_FLIGHT_KEY"
  [ -n "${ANTHROPIC_API_KEY:-}" ] && printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_API_KEY"
  [ -n "${CONCORDEGO_PEXELS_KEY:-}" ] && printf 'CONCORDEGO_PEXELS_KEY=%s\n' "$CONCORDEGO_PEXELS_KEY"
  [ -n "${CONCORDEGO_SERPAPI_KEY:-}" ] && printf 'CONCORDEGO_SERPAPI_KEY=%s\n' "$CONCORDEGO_SERPAPI_KEY"
  [ -n "$ACCESS_TEAM" ] && printf 'CONCORDEGO_ACCESS_TEAM=%s\n' "$ACCESS_TEAM"
  printf 'CONCORDEGO_OWNERS=%s\n' "$OWNERS"; printf 'CONCORDEGO_PUBLIC=1\n'; } | "${SSH[@]}" 'umask 077; cat > /root/.concordego-keys.tmp; python3 - <<"PY"
import sys, re, os
# the lines arrive on stdin; they are read into a file first because the heredoc below takes stdin over
new = dict(l.rstrip("\n").split("=", 1) for l in open("/root/.concordego-keys.tmp") if "=" in l)
os.remove("/root/.concordego-keys.tmp")
path = "/etc/concordego.env"; lines = open(path).read().splitlines()
out = [l for l in lines if not any(re.match(r"#?\s*" + k + "=", l) for k in new)] + [k + "=" + v for k, v in new.items()]
open(path, "w").write("\n".join(out) + "\n")
print("  " + ", ".join(sorted(new)) + " written")
PY
systemctl restart concordego && sleep 1 && systemctl is-active concordego'

# ------------------------------------------------------------------ door
say "access"
ALLOW="$ALLOW" HOST="$HOST" TEAM="${TEAM:-}" bash "$TRAVEL/access.sh" 2>&1 | sed 's/^/  /'

# ---------------------------------------------------------------- verify
say "verify"
for _ in $(seq 1 12); do dig +short "$HOST" @1.1.1.1 2>/dev/null | grep -q . && break; sleep 5; done
echo "  dns: $(dig +short "$HOST" @1.1.1.1 | head -1)"
for _ in $(seq 1 12); do CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$HOST/" || true); [ "$CODE" = 200 ] && break; sleep 5; done
echo "  https://$HOST/ -> $CODE"
echo "  https://$HOST/api/admin (sign in as $OWNERS)"
echo "  droplet: ssh root@$IP   logs: journalctl -u concordego -f   update now: concordego-update"
