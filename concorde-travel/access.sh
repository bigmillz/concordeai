#!/usr/bin/env bash
# Put Cloudflare Access in front of go.flyconcordfly.com: an email allowlist,
# one-time-PIN sign-in (and Google, once that identity provider exists), and
# nothing reaches the server until someone has signed in. Idempotent.
#
#   export CF_API_TOKEN=...        # a token with:  Access: Apps and Policies : Edit
#   export CF_ACCOUNT_ID=...       #                Access: Organizations, Identity Providers, and Groups : Edit
#   ALLOW="pat@millertechnology.net,friend@example.com" ./concorde-travel/access.sh
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
: "${CF_API_TOKEN:?set CF_API_TOKEN}"; : "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID}"; : "${ALLOW:?set ALLOW to a comma-separated list of emails}"
HOST="${HOST:-go.flyconcordfly.com}"; SESSION="${SESSION:-24h}"; TEAM="${TEAM:-}"
API="https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/access"

cf(){ # method path [json]
  curl -sS -X "$1" "$API$2" -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" ${3:+--data "$3"}
}
jq_(){ python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }

echo "== organisation"
if ! cf GET /organizations | jq_ 'sys.exit(0 if d.get("success") and d.get("result") and d["result"].get("auth_domain") else 1)'; then
  [ -n "$TEAM" ] || { echo "This account has no Zero Trust team yet. Re-run with TEAM=<name> (it becomes <name>.cloudflareaccess.com)."; exit 1; }
  cf POST /organizations "{\"name\":\"$TEAM\",\"auth_domain\":\"$TEAM.cloudflareaccess.com\"}" | jq_ 'print("  created", d["result"]["auth_domain"]) if d.get("success") else sys.exit("  " + json.dumps(d.get("errors")))'
else
  cf GET /organizations | jq_ 'print("  ", d["result"]["auth_domain"])'
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
  if [ -z "$APP" ]; then APP=$(cf POST /apps "$BODY" | jq_ 'print(d["result"]["id"]) if d.get("success") else sys.exit("  " + json.dumps(d.get("errors")))'); echo "  created $NAME ($DOM)"
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
