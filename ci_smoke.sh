#!/bin/bash
# PRE-PUBLISH SMOKE (6b309, per Patrick: "do we have agents and harnesses
# in place"). The nightly used to publish with no check at all, not even
# a compile: a build that can't start can't update itself out of the
# problem. This compiles with warnings as errors (the THIN LIST crash
# shipped as a SyntaxWarning), then boots the app headless in an empty
# home, with no models and no keys, and checks the page and one API.
# Same script locally and in CI:  ./ci_smoke.sh
set -euo pipefail
PY="${PYTHON:-python3}"
# a FREE port per run (review): a fixed one could answer from some other
# server and pass while this build never started
PORT="${SMOKE_PORT:-$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')}"
if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "smoke: port $PORT is already in use"; exit 1
fi
KEY="ci-smoke-$$"
"$PY" -W error::SyntaxWarning -m py_compile millenai.py
echo "compile: ok"
HOME_DIR="$(mktemp -d)"
LOG="$HOME_DIR/boot.log"
HOME="$HOME_DIR" MILLENAI_HEADLESS=1 MILLENAI_PORT="$PORT" MILLENAI_KEY="$KEY" \
  "$PY" millenai.py >"$LOG" 2>&1 &
PID=$!
disown $PID 2>/dev/null || true
# the app's children (an engine it started) go first, while their parent
# can still be named, then the app, then its throwaway home
trap 'pkill -9 -P $PID 2>/dev/null || true; kill -9 $PID 2>/dev/null || true; rm -rf "$HOME_DIR"' EXIT
code=000
for _ in $(seq 1 90); do
  kill -0 $PID 2>/dev/null || { echo "boot: the app exited"; tail -40 "$LOG"; exit 1; }
  code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" -H "Cookie: millen_key=$KEY" \
         "http://127.0.0.1:$PORT/" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
if grep -q 'Traceback\|Address already in use' "$LOG"; then
  echo "boot: errors in the log"; tail -40 "$LOG"; exit 1
fi
[ "$code" = "200" ] || { echo "boot: no page after 90s (HTTP $code)"; tail -40 "$LOG"; exit 1; }
PAGE="$HOME_DIR/page.html"
curl -sf -m 10 -H "Cookie: millen_key=$KEY" "http://127.0.0.1:$PORT/" >"$PAGE"
LEFT=$(grep -oE '__[A-Z_]{3,}__' "$PAGE" | grep -v '^__MAIN__$' | sort -u || true)
[ -z "$LEFT" ] || { echo "page: unreplaced template tokens: $LEFT"; exit 1; }
curl -sf -m 10 -H "Cookie: millen_key=$KEY" "http://127.0.0.1:$PORT/api/stats" >/dev/null \
  || { echo "api: /api/stats failed"; tail -40 "$LOG"; exit 1; }
echo "boot: ok (page $(wc -c <"$PAGE" | tr -d ' ') bytes, /api/stats 200)"
