#!/usr/bin/env bash
# Put Cloudflare Access in front of go.flyconcordfly.com: an email allowlist,
# one-time-PIN sign-in (and Google, once that identity provider exists), and
# nothing reaches the server until someone has signed in. Idempotent.
#
#   export CF_API_TOKEN=...        # a token with:  Access: Apps and Policies : Edit
#                                  #               Access: Organizations, Identity Providers, and Groups : Edit
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
: "${CF_API_TOKEN:?set CF_API_TOKEN}"; : "${ALLOW:?set ALLOW to a comma-separated list of emails}"
HOST="${HOST:-go.flyconcordfly.com}"; SESSION="${SESSION:-24h}"; TEAM="${TEAM:-}"
TUNNEL="${TUNNEL:-concordego}"

# The account that owns the zone is the one the tunnel was made in: cloudflared
# logged into it, and wrote its AccountTag into the tunnel's credentials file.
# Access can only guard a hostname whose zone is in the same account, so that
# tag is the Account ID this script must use. It is read here so nobody has to
# find it in the dashboard, and a CF_ACCOUNT_ID that disagrees is refused before
# a team or an application is created in the wrong account.
TUN_ACCT=$(python3 - "$TUNNEL" <<'PY'
import glob, json, os, sys
want = sys.argv[1]; found = []
for f in sorted(glob.glob(os.path.expanduser("~/.cloudflared/*.json"))):
    try: d = json.load(open(f))
    except Exception: continue
    if d.get("AccountTag") and d.get("TunnelID"): found.append((d.get("TunnelName") == want, d["AccountTag"]))
found.sort(key=lambda x: not x[0])
print(found[0][1] if found and (found[0][0] or len(found) == 1) else "")
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
  curl -sS -X "$1" "$API$2" -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" ${3:+--data "$3"}
}
jq_(){ python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }
ROOT="https://api.cloudflare.com/client/v4"
cfroot(){ curl -sS "$ROOT$1" -H "Authorization: Bearer $CF_API_TOKEN"; }

# A request that fails is not the same as a team that does not exist, and the
# first version of this script conflated them: an unauthorised GET read as "no
# team yet" and sent people off to pick a name. So the token and the account
# are checked first, each with the reason spelled out.
echo "== token"
cfroot /user/tokens/verify | jq_ 'r=d.get("result") or {}
if not d.get("success") or r.get("status") != "active": sys.exit("  Cloudflare does not accept this token: " + json.dumps(d.get("errors")) + "\n  Make one at dash.cloudflare.com > My Profile > API Tokens > Create Token > Custom token.")
print("  active")'
echo "== account"
# Reading /accounts/{id} itself needs "Account Settings: Read", which this token
# is not asked to carry, so the account is checked through Access instead: the
# list of Access applications needs only the permission the script needs anyway.
cf GET /apps | jq_ 'r=d.get("result") or []
if not d.get("success"): sys.exit("  The token cannot reach Access on this account: " + json.dumps(d.get("errors")) + "\n  One of three things:\n   - CF_ACCOUNT_ID is wrong. The sure way to read it: open dash.cloudflare.com, click the account, and copy the 32 characters in the address bar right after dash.cloudflare.com/ (a Zone ID looks identical and is the usual mix-up).\n   - The token was made with Account Resources set to a different account, or to none.\n   - The token lacks \"Access: Apps and Policies: Edit\".\n  My Profile > API Tokens > the token shows its permissions and which account it covers.")
print("  reachable, " + str(len(r)) + " Access application(s) so far")'

# Best effort, before anything is created: if the token can list zones, make sure
# the zone that owns $HOST is on this account. Without Zone: Read the lookup is
# refused and skipped; the applications step reports the same thing later.
APEX=$(echo "$HOST" | awk -F. '{print $(NF-1)"."$NF}')
cfroot "/zones?name=$APEX" | jq_ 'r = d.get("result") or []
if d.get("success") and not r: sys.exit("== zone\n  The token can list zones and '$APEX' is not among this account'"'"'s. The zone lives in another account of yours: its Overview page shows that Account ID on the right. Use that, with a token covering that account.")
print("== zone\n  " + ("'$APEX' is on this account" if d.get("success") else "not checked (the token has no Zone: Read, which is fine)"))'

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
elif "does not belong to zone" in errs: sys.exit("  " + errs + "\n  Access can only guard a hostname whose zone is in THIS account, and the zone that owns '$DOM' is not.\n  The zone lives in another Cloudflare account of yours: open that zone in the dashboard, and its Overview page shows the Account ID on the right. Use that one,\n  make sure the token covers that account (My Profile > API Tokens > the token > Account Resources), and re-run. If that account has no team yet, pass TEAM= with a NEW name: the one just used is now taken by this account.")
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
ensure_app "ConcordeGo API" "$HOST/api" "$FRIENDS"
ensure_app "ConcordeGo sign-in" "$HOST/signin" "$FRIENDS"
ensure_app "ConcordeGo" "$HOST" "$EVERYONE"
echo
echo "Done. https://$HOST is open to browse; /api and /signin ask for a sign-in, and the server sees the email in Cf-Access-Authenticated-User-Email"
echo "and gives each person a daily allowance (CONCORDEGO_USER_SEARCHES, default 20; CONCORDEGO_USER_WISHES, default 200)."
