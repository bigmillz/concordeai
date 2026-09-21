#!/usr/bin/env bash
# Put Cloudflare Access in front of go.flyconcordfly.com: an email allowlist,
# one-time-PIN sign-in (and Google, once that identity provider exists), and
# nothing reaches the server until someone has signed in. Idempotent.
#
#   export CF_API_TOKEN=...        # a token with:  Access: Apps and Policies : Edit
#                                  #               Access: Organizations, Identity Providers, and Groups : Edit
#                                  #               Zone : Zone : Read, Zone Resources: All zones
#   (or CF_EMAIL=... CF_GLOBAL_KEY=...  the Global API Key, for one run, never stored)
#   ALLOW="pat@millertechnology.net,friend@example.com" ./concorde-travel/access.sh
#
# The Account ID is read from the tunnel's credentials in ~/.cloudflared (the
# account go-live.sh made the tunnel in owns the zone, and Access needs that
# one); CF_ACCOUNT_ID=... overrides it and is refused if it disagrees.
#
# Optional: HOST (default go.flyconcordfly.com), TEAM (the Zero Trust team name,
# needed only the first time an account uses Access; it becomes
# <TEAM>.cloudflareaccess.com), SESSION (default 24h).
#
# The token is read from the environment and never written anywhere. To add
# Google sign-in: Zero Trust -> Settings -> Authentication -> Add new -> Google,
# paste an OAuth client id and secret from Google Cloud, then re-run this script
# and the app picks it up beside the one-time PIN.
set -euo pipefail
: "${ALLOW:?set ALLOW to a comma-separated list of emails}"
# Two ways in. A scoped API token is the right one; Access resolves a hostname
# through the zones the token can see, so it needs Zone: Read on the zone as
# well as the two Access rows. The Global API Key (My Profile > API Tokens >
# Global API Key > View) sees everything and cannot be mis-scoped; it is read
# from the environment for this one run and written nowhere.
if [ -n "${CF_GLOBAL_KEY:-}" ]; then
  : "${CF_EMAIL:?set CF_EMAIL (the Cloudflare login email) beside CF_GLOBAL_KEY}"
  AUTH=(-H "X-Auth-Email: $CF_EMAIL" -H "X-Auth-Key: $CF_GLOBAL_KEY"); AUTH_KIND=globalkey
else
  : "${CF_API_TOKEN:?set CF_API_TOKEN, or CF_EMAIL and CF_GLOBAL_KEY}"
  AUTH=(-H "Authorization: Bearer $CF_API_TOKEN"); AUTH_KIND=token
fi
HOST="${HOST:-go.flyconcordfly.com}"; SESSION="${SESSION:-24h}"; TEAM="${TEAM:-}"; HOST_OK=""
TUNNEL="${TUNNEL:-concordego}"

# The account that owns the zone is the one the tunnel was made in: cloudflared
# logged into it, and wrote its AccountTag into the tunnel's credentials file.
# Access can only guard a hostname whose zone is in the same account, so that
# tag is the Account ID this script must use. It is read here so nobody has to
# find it in the dashboard, and a CF_ACCOUNT_ID that disagrees is refused before
# a team or an application is created in the wrong account.
TUN_ACCT=$(python3 - "$TUNNEL" <<'PY'
import glob, json, os, re, sys
want = sys.argv[1]; home = os.path.expanduser("~/.cloudflared")
def tag(path):
    try: d = json.load(open(path)); return d.get("AccountTag") or ""
    except Exception: return ""
# 1. the config go-live.sh wrote names the credentials file outright
cfg = os.path.join(home, want + ".yml")
if os.path.exists(cfg):
    for line in open(cfg):
        m = re.match(r"\s*credentials-file:\s*(.+?)\s*$", line)
        if m and tag(m.group(1)): print(tag(m.group(1))); sys.exit()
# 2. the tunnel id from cloudflared itself, then its credentials file
try:
    import subprocess
    out = subprocess.run(["cloudflared", "tunnel", "list", "--name", want, "--output", "json"], capture_output=True, text=True, timeout=20).stdout
    for t in json.loads(out or "[]"):
        if t.get("name") == want and tag(os.path.join(home, t["id"] + ".json")): print(tag(os.path.join(home, t["id"] + ".json"))); sys.exit()
except Exception: pass
# 3. a credentials file that names the tunnel, or the only one there is
files = [f for f in sorted(glob.glob(os.path.join(home, "*.json"))) if tag(f)]
named = [f for f in files if (json.load(open(f)).get("TunnelName") == want)]
if named: print(tag(named[0]))
elif len(files) == 1: print(tag(files[0]))
else: print("")
PY
)
if [ -z "${CF_ACCOUNT_ID:-}" ]; then
  [ -n "$TUN_ACCT" ] || { echo "set CF_ACCOUNT_ID (no tunnel credentials in ~/.cloudflared to read it from; run go-live.sh first, or pass it)"; exit 1; }
  CF_ACCOUNT_ID="$TUN_ACCT"; echo "== account id\n  $CF_ACCOUNT_ID, from the tunnel's credentials in ~/.cloudflared" | sed 's/\\n/\n/'
elif [ -n "$TUN_ACCT" ] && [ "$TUN_ACCT" != "$CF_ACCOUNT_ID" ]; then
  echo "== account id"
  echo "  CF_ACCOUNT_ID is $CF_ACCOUNT_ID, but the tunnel that serves $HOST was made in account $TUN_ACCT"
  echo "  (its credentials file in ~/.cloudflared says so), and that is the account that owns the zone."
  echo "  Access can only guard the hostname from there. Re-run with CF_ACCOUNT_ID=$TUN_ACCT and a token"
  echo "  whose Account Resources cover that account; or leave CF_ACCOUNT_ID out and it is used automatically."
  exit 1
fi
API="https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/access"

cf(){ # method path [json]
  curl -sS -X "$1" "$API$2" "${AUTH[@]}" -H "Content-Type: application/json" ${3:+--data "$3"}
}
jq_(){ python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }
ROOT="https://api.cloudflare.com/client/v4"
cfroot(){ curl -sS "$ROOT$1" "${AUTH[@]}"; }

# A request that fails is not the same as a team that does not exist, and the
# first version of this script conflated them: an unauthorised GET read as "no
# team yet" and sent people off to pick a name. So the token and the account
# are checked first, each with the reason spelled out.
echo "== credentials"
if [ "$AUTH_KIND" = token ]; then
  cfroot /user/tokens/verify | jq_ 'r=d.get("result") or {}
if not d.get("success") or r.get("status") != "active": sys.exit("  Cloudflare does not accept this token: " + json.dumps(d.get("errors")) + "\n  Make one at dash.cloudflare.com > My Profile > API Tokens > Create Token > Custom token.")
print("  token active")'
else
  cfroot /user | jq_ 'r=d.get("result") or {}
if not d.get("success"): sys.exit("  Cloudflare does not accept this Global API Key and email: " + json.dumps(d.get("errors")))
print("  global key for " + str(r.get("email") or ""))'
fi
echo "== account"
# Reading /accounts/{id} itself needs "Account Settings: Read", which this token
# is not asked to carry, so the account is checked through Access instead: the
# list of Access applications needs only the permission the script needs anyway.
cf GET /apps | jq_ 'r=d.get("result") or []
if not d.get("success"): sys.exit("  The token cannot reach Access on this account: " + json.dumps(d.get("errors")) + "\n  One of three things:\n   - CF_ACCOUNT_ID is wrong. The sure way to read it: open dash.cloudflare.com, click the account, and copy the 32 characters in the address bar right after dash.cloudflare.com/ (a Zone ID looks identical and is the usual mix-up).\n   - The token was made with Account Resources set to a different account, or to none.\n   - The token lacks \"Access: Apps and Policies: Edit\".\n  My Profile > API Tokens > the token shows its permissions and which account it covers.")
print("  reachable, " + str(len(r)) + " Access application(s) so far" + ((": " + ", ".join(str(a.get("domain") or "?") for a in r[:8])) if r else ""))'

# Best effort, before anything is created: with Zone: Read on the token this
# confirms the zone that owns $HOST is on this account. Without it the list is
# simply EMPTY (not refused), which proves nothing, so it never stops the run.
APEX=$(echo "$HOST" | awk -F. '{print $(NF-1)"."$NF}')
ZJ=$(cfroot "/zones?name=$APEX")
ZONE_SEEN=$(echo "$ZJ" | jq_ 'r = d.get("result") or []
print("1" if (d.get("success") and r) else "")')
echo "== zone"
if [ -n "$ZONE_SEEN" ]; then echo "  $APEX is visible to these credentials"
else
  echo "  $APEX is NOT visible to these credentials. What Cloudflare said, and who these credentials are:"
  echo "$ZJ" | jq_ 'print("   zones?name=: success=" + str(d.get("success")) + " result=" + json.dumps(d.get("result")) + " errors=" + json.dumps(d.get("errors")))'
  cfroot "/zones?per_page=50" | jq_ 'r = d.get("result") or []
print("   zones they can see: " + (", ".join(z.get("name","?") + " (" + (z.get("account") or {}).get("id","?")[:8] + ")" for z in r) if r else "none") + ("" if d.get("success") else "  errors=" + json.dumps(d.get("errors"))))'
  cfroot "/memberships" | jq_ 'r = d.get("result") or []
print("   account memberships: " + ("; ".join((m.get("account") or {}).get("name","?") + " " + (m.get("account") or {}).get("id","?")[:8] + " as " + ", ".join(m.get("roles") or []) for m in r) if r else "none") + ("" if d.get("success") else "  errors=" + json.dumps(d.get("errors"))))'
  echo "  Access resolves a hostname through the zones the caller can see, so the applications would be refused. Nothing is created."
  echo "  Read the lines above:"
  echo "   - If the zone is missing even with the Global API Key, the login these credentials belong to does not own the zone: it is a MEMBER"
  echo "     of the account with a role that covers Access but not DNS. The owner login (the one that shows the zone in its dashboard) must"
  echo "     either run this script with ITS Global API Key, or make this login a Super Administrator of the account."
  echo "   - If this is a scoped token and the Global API Key does see the zone, edit the token: add  Zone : Zone : Read  with Zone Resources"
  echo "     Include > All zones, save, and re-run."
  exit 1
fi

echo "== organisation"
ORG=$(cf GET /organizations)
if echo "$ORG" | jq_ 'sys.exit(0 if d.get("success") else 1)'; then
  if echo "$ORG" | jq_ 'sys.exit(0 if (d.get("result") or {}).get("auth_domain") else 1)'; then
    echo "$ORG" | jq_ 'print("  ", d["result"]["auth_domain"])'
  else
    [ -n "$TEAM" ] || { echo "This account has no Zero Trust team yet. Re-run with TEAM=<name> (it becomes <name>.cloudflareaccess.com)."; exit 1; }
    cf POST /organizations "{\"name\":\"$TEAM\",\"auth_domain\":\"$TEAM.cloudflareaccess.com\"}" | jq_ 'print("  created", d["result"]["auth_domain"]) if d.get("success") else sys.exit("  " + json.dumps(d.get("errors")) + "\n  Creating the team needs the permission \"Access: Organizations, Identity Providers, and Groups: Edit\" on the token.\n  Or create it once by hand at one.dash.cloudflare.com (pick the team name, the Free plan) and re-run without TEAM.")'
  fi
else
  echo "$ORG" | jq_ 'sys.exit("  Access refused the request: " + json.dumps(d.get("errors")) + "\n  The token needs BOTH permissions, on this account: \"Access: Apps and Policies: Edit\" and \"Access: Organizations, Identity Providers, and Groups: Edit\".\n  If the account has never opened Zero Trust, open one.dash.cloudflare.com once, pick a team name and the Free plan, then re-run.")'
fi

echo "== identity providers"
IDPS=$(cf GET /identity_providers)
OTP=$(echo "$IDPS" | jq_ 'print(next((i["id"] for i in d.get("result") or [] if i.get("type")=="onetimepin"), ""))')
if [ -z "$OTP" ]; then
  OTP=$(cf POST /identity_providers '{"name":"One-time PIN","type":"onetimepin","config":{}}' | jq_ 'print(d["result"]["id"]) if d.get("success") else sys.exit("  " + json.dumps(d.get("errors")))')
  echo "  created one-time PIN"
fi
GOOGLE=$(echo "$IDPS" | jq_ 'print(next((i["id"] for i in d.get("result") or [] if i.get("type")=="google"), ""))')
echo "  one-time PIN: $OTP${GOOGLE:+   google: $GOOGLE}"
IDP_LIST="\"$OTP\"${GOOGLE:+,\"$GOOGLE\"}"

ensure_app(){ # name domain policy_json
  local NAME="$1" DOM="$2" POLICY="$3" APP
  APP=$(cf GET "/apps" | jq_ "print(next((a['id'] for a in d.get('result') or [] if a.get('domain')=='$DOM'), ''))")
  local BODY="{\"name\":\"$NAME\",\"type\":\"self_hosted\",\"domain\":\"$DOM\",\"session_duration\":\"$SESSION\",\"allowed_idps\":[$IDP_LIST],\"auto_redirect_to_identity\":false,\"app_launcher_visible\":false}"
  if [ -z "$APP" ]; then APP=$(cf POST /apps "$BODY" | jq_ 'errs = json.dumps(d.get("errors"))
if d.get("success"): print(d["result"]["id"])
elif "does not belong to zone" in errs and "'${HOST_OK:-}'": sys.exit("  " + errs + "\n  The bare hostname was accepted a moment ago, so the zone is fine and Access is refusing the PATH form ('$DOM').")
elif "does not belong to zone" in errs and not "'${ZONE_SEEN:-}'": sys.exit("  " + errs + "\n  The token cannot see the zone that owns '$DOM' (its zone list is empty), and Access resolves the hostname through the zones the token can see.\n  Fix the token, not the zone: dash.cloudflare.com > My Profile > API Tokens > this token > Edit:\n    - Permissions: add  Zone : Zone : Read  (keep the two Access rows)\n    - Zone Resources: Include > All zones from an account > the account that owns the zone\n  Save, then re-run. The zone step above will then say the zone is visible, and the applications go through.")
elif "does not belong to zone" in errs: sys.exit("  " + errs + "\n  The token can see the zone, yet Access refuses the hostname. Check the zone in the dashboard: it must be Active with DNS fully on Cloudflare (not pending, not a partial CNAME setup), and the Account ID on its Overview page must be '$CF_ACCOUNT_ID'.")
else: sys.exit("  " + errs)'); echo "  created $NAME ($DOM)"
  else cf PUT "/apps/$APP" "$BODY" >/dev/null; echo "  updated $NAME ($DOM)"; fi
  local POL; POL=$(cf GET "/apps/$APP/policies" | jq_ 'print(next((p["id"] for p in d.get("result") or [] if p.get("name") in ("Friends","Everyone")), ""))')
  if [ -z "$POL" ]; then cf POST "/apps/$APP/policies" "$POLICY" | jq_ 'print("    policy", d["result"]["id"]) if d.get("success") else sys.exit("  " + json.dumps(d.get("errors")))'
  else cf PUT "/apps/$APP/policies/$POL" "$POLICY" | jq_ 'print("    policy", d["result"]["id"]) if d.get("success") else sys.exit("  " + json.dumps(d.get("errors")))'; fi
}
INCLUDE=$(python3 -c "import json,sys; print(json.dumps([{'email':{'email':e.strip().lower()}} for e in '$ALLOW'.split(',') if e.strip()]))")
FRIENDS="{\"name\":\"Friends\",\"decision\":\"allow\",\"include\":$INCLUDE,\"precedence\":1}"
EVERYONE='{"name":"Everyone","decision":"bypass","include":[{"everyone":{}}],"precedence":1}'

echo "== applications"
# Two doors need a sign-in: everything under /api (a live search, the wish box,
# the price check) and /signin, the page the site sends people to. The page
# itself and its assets stay open to everyone, so anyone can browse and read
# the recorded results; the server spends nothing for a visitor Access did
# not sign in, and Access never lets an anonymous request reach /api at all.
ensure_app "ConcordeGo" "$HOST" "$EVERYONE"
HOST_OK=1
ensure_app "ConcordeGo sign-in" "$HOST/signin" "$FRIENDS"
ensure_app "ConcordeGo API" "$HOST/api" "$FRIENDS"
echo
echo "Done. https://$HOST is open to browse; /api and /signin ask for a sign-in, and the server sees the email in Cf-Access-Authenticated-User-Email"
echo "and gives each person a daily allowance (CONCORDEGO_USER_SEARCHES, default 20; CONCORDEGO_USER_WISHES, default 200)."
