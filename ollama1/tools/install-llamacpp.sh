#!/usr/bin/env bash
# install-llamacpp.sh: the llama.cpp bridge for an ollama1 server (6b531), in steps
# safe to run from far away: no reboot, no network or driver change, nothing while
# the server is answering a request.
#
# What it does (--install), each step skipped when already done, so it is safe to run again:
#   1. refuses while the gateway is busy (the install-when-quiet rule: activity.json's
#      requests in flight, and no `req` line in the gateway's journal for --quiet-min minutes)
#   2. copies llama.cpp's server build from --from DIR (a build already on this machine, the one
#      the measurements used; it is NOT downloaded here) into /opt/llamacpp/<name> and points
#      /opt/llamacpp/current at it; `llama-server --version` must run
#   3. installs the kit's new files the way setup.sh does: lib/o1llamacpp.py, lib/o1ollama.py,
#      lib/o1common.py, bin/ollama1-llamacpp, bin/ollama1-gateway, the unit, the tmpfiles line
#   4. /etc/ollama1/llamacpp.json from config/llamacpp.json if there is none yet (enabled: false,
#      no models: you choose them), root:root 0600 (the unit hands the bridge a copy)
#   5. the runtime folder /run/ollama1/llamacpp (ollama:o1gw 2770), the bridge enabled and started
#   6. `ollama1-llamacpp check` must pass before the gateway is pointed at the bridge
#   7. config.json gains "llamacpp_bridge": "unix:/run/ollama1/llamacpp/bridge.sock" and the
#      gateway restarts (one restart, a few seconds; a request in flight would be cut, hence step 1)
#
# --verify: the bridge answers on its socket, the gateway's upstream is the bridge, /api/tags and
#   /api/ps through the gateway still work, and the journal shows no error.
# --rollback: removes the key from config.json, restarts the gateway (Ollama serves everything
#   again), stops and disables the bridge. The files stay; nothing else changes.
#
#   sudo bash install-llamacpp.sh --check                      what would happen; nothing changes
#   sudo bash install-llamacpp.sh --install --from ~/llama-cuda/llama-b11849 --name b11849
#   sudo bash install-llamacpp.sh --verify
#   sudo bash install-llamacpp.sh --rollback
#   sudo bash install-llamacpp.sh --install ... --force        skip the quiet check (never while someone is asking)
set -euo pipefail

KIT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LIBDIR=/usr/local/lib/ollama1
OPT=/opt/llamacpp
CFG=/etc/ollama1/config.json
BCFG=/etc/ollama1/llamacpp.json
SOCK=/run/ollama1/llamacpp/bridge.sock
QUIET_MIN=5
MODE=""; FROM=""; NAME=""; FORCE=0

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
say() { printf '%s\n' "$*"; }
ok() { printf '  ok   %s\n' "$*"; }
skip() { printf '  --   %s\n' "$*"; }
die() { printf 'stop: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check|--install|--verify|--rollback) MODE="${1#--}" ;;
    --from) FROM="${2:-}"; shift ;;
    --name) NAME="${2:-}"; shift ;;
    --quiet-min) QUIET_MIN="${2:-5}"; shift ;;
    --force) FORCE=1 ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument $1" ;;
  esac
  shift
done
[ -n "$MODE" ] || usage 1
[ "$(id -u)" = 0 ] || die "run with sudo"
[ -f "$CFG" ] || die "$CFG is missing: this is not a set-up ollama1 server"

quiet_check() {
  # 1. nothing in flight and nothing answered lately: the server is not being used
  local inflight recent
  inflight=$(python3 - <<'EOF'
import json, os, time
try:
    d = json.load(open("/run/ollama1/stats/activity.json"))
except Exception:
    d = {}
at = d.get("written") or d.get("at") or 0
fresh = isinstance(at, (int, float)) and (time.time() - at) < 120
print(d.get("inflight", "?") if fresh else "?")
EOF
)
  recent=$(journalctl -u ollama1-gateway --since "-${QUIET_MIN} min" --no-pager -o cat 2>/dev/null | grep -c '^req ' || true)
  if [ "$inflight" != "0" ] && [ "$inflight" != "?" ]; then
    [ "$FORCE" = 1 ] || die "the gateway has $inflight request(s) in flight; try again when it is quiet (or --force)"
  fi
  if [ "${recent:-0}" -gt 0 ]; then
    [ "$FORCE" = 1 ] || die "the gateway answered $recent request(s) in the last $QUIET_MIN min; try again when it is quiet (or --force)"
  fi
  ok "quiet: in flight ${inflight}, requests in the last ${QUIET_MIN} min: ${recent:-0}"
}

install_build() {
  # 2. the llama.cpp build, from a folder already on this machine
  if [ -x "$OPT/current/llama-server" ] && [ -z "$FROM" ]; then
    skip "llama-server already at $OPT/current ($("$OPT/current/llama-server" --version 2>&1 | head -1))"
    return
  fi
  [ -n "$FROM" ] || die "--from DIR: the folder holding llama-server and its libraries (the measured CUDA build)"
  [ -x "$FROM/llama-server" ] || die "$FROM/llama-server is not there or not executable"
  [ -n "$NAME" ] || NAME=$(basename "$FROM")
  [[ "$NAME" =~ ^[A-Za-z0-9._-]{1,40}$ ]] || die "--name must be a plain word"
  install -d -m 0755 "$OPT"
  if [ ! -d "$OPT/$NAME" ]; then
    cp -a "$FROM" "$OPT/$NAME.tmp"
    chown -R root:root "$OPT/$NAME.tmp"
    chmod -R go-w "$OPT/$NAME.tmp"
    mv "$OPT/$NAME.tmp" "$OPT/$NAME"
    ok "copied $FROM to $OPT/$NAME"
  else
    skip "$OPT/$NAME is already there"
  fi
  LD_LIBRARY_PATH="$OPT/$NAME" "$OPT/$NAME/llama-server" --version >/dev/null 2>&1 || die "$OPT/$NAME/llama-server --version failed (missing libraries?)"
  ln -sfn "$OPT/$NAME" "$OPT/current"
  ok "$OPT/current -> $OPT/$NAME ($(LD_LIBRARY_PATH="$OPT/$NAME" "$OPT/$NAME/llama-server" --version 2>&1 | head -1))"
}

install_files() {
  # 3. the kit's files, as setup.sh installs them
  install -d -m 0755 "$LIBDIR" "$LIBDIR/bin" "$LIBDIR/lib"
  install -m 0644 "$KIT/lib/o1llamacpp.py" "$KIT/lib/o1ollama.py" "$KIT/lib/o1common.py" "$LIBDIR/lib/"
  install -m 0755 "$KIT/bin/ollama1-llamacpp" "$KIT/bin/ollama1-gateway" "$LIBDIR/bin/"
  install -m 0644 "$KIT/systemd/ollama1-llamacpp.service" /etc/systemd/system/
  if ! grep -q '^d /run/ollama1/llamacpp' /etc/tmpfiles.d/ollama1.conf 2>/dev/null; then
    install -m 0644 "$KIT/config/ollama1.tmpfiles" /etc/tmpfiles.d/ollama1.conf
  fi
  systemctl daemon-reload
  ok "kit files in $LIBDIR, unit installed"
  # 4. the bridge's own config, yours to fill in
  if [ ! -f "$BCFG" ]; then
    install -m 0600 -o root -g root "$KIT/config/llamacpp.json" "$BCFG"
    ok "$BCFG written (enabled: false, no models yet: edit it, then restart ollama1-llamacpp)"
  else
    skip "$BCFG kept as it is"
  fi
  # 5. the folder and the service
  install -d -m 2770 -o ollama -g o1gw /run/ollama1/llamacpp
  systemctl enable --now ollama1-llamacpp.service
  for _ in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.25; done
  [ -S "$SOCK" ] || { journalctl -u ollama1-llamacpp -n 20 --no-pager; die "the bridge did not open $SOCK"; }
  ok "ollama1-llamacpp running on $SOCK"
}

point_gateway() {
  # 6. the check must pass first
  if ! "$LIBDIR/bin/ollama1-llamacpp" check --config "$BCFG"; then
    die "ollama1-llamacpp check found problems: fix $BCFG (or install llama-server) before the gateway is pointed at the bridge"
  fi
  # 7. one key, one gateway restart
  if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("llamacpp_bridge")=="unix:"+sys.argv[2] else 1)' "$CFG" "$SOCK"; then
    skip "config.json already names the bridge"
  else
    cp -a "$CFG" "$CFG.before-llamacpp"
    python3 - "$CFG" "$SOCK" <<'EOF'
import json, os, sys
p, sock = sys.argv[1], sys.argv[2]
cfg = json.load(open(p))
cfg["llamacpp_bridge"] = "unix:" + sock
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2, sort_keys=True)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, p)
EOF
    chown root:root "$CFG"; chmod 0600 "$CFG"
    ok "config.json: llamacpp_bridge set (a copy is at $CFG.before-llamacpp)"
  fi
  systemctl restart ollama1-gateway.service
  sleep 2
  systemctl is-active --quiet ollama1-gateway.service || { journalctl -u ollama1-gateway -n 20 --no-pager; die "the gateway did not come back: run --rollback"; }
  ok "gateway restarted and active"
}

verify() {
  systemctl is-active --quiet ollama1-llamacpp.service && ok "ollama1-llamacpp active" || die "ollama1-llamacpp is not active"
  systemctl is-active --quiet ollama1-gateway.service && ok "ollama1-gateway active" || die "ollama1-gateway is not active"
  "$LIBDIR/bin/ollama1-llamacpp" status --socket "$SOCK" && ok "the bridge answers on its socket"
  "$LIBDIR/bin/ollama1-llamacpp" check --config "$BCFG" >/dev/null && ok "ollama1-llamacpp check passes" || die "ollama1-llamacpp check fails"
  python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("llamacpp_bridge")=="unix:"+sys.argv[2] else 1)' "$CFG" "$SOCK" \
    && ok "config.json points the gateway at the bridge" || die "config.json does not name the bridge"
  # through the bridge, as the gateway sees Ollama: the model list and the loaded list
  python3 - "$SOCK" <<'EOF' && ok "/api/tags and /api/ps answer through the bridge" || exit 1
import http.client, json, socket, sys
class U(http.client.HTTPConnection):
    def __init__(self, p): super().__init__("localhost", timeout=10); self.p = p
    def connect(self):
        s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect(self.p); self.sock = s
for path in ("/api/tags", "/api/ps"):
    c = U(sys.argv[1]); c.request("GET", path); r = c.getresponse(); d = json.loads(r.read())
    assert r.status == 200 and "models" in d, (path, r.status)
    print("   ", path, len(d["models"]), "model(s)")
EOF
  if journalctl -u ollama1-gateway -u ollama1-llamacpp --since "-10 min" --no-pager -o cat 2>/dev/null | grep -E '^(error|died|fatal) ' | head -3 | grep -q .; then
    say "  !!   errors in the last 10 minutes of the journal (above): read them before using the server"
  else
    ok "no error in the journal for the last 10 minutes"
  fi
  say "next: a real question from the app to a model listed in $BCFG, then: journalctl -u ollama1-llamacpp -n 20"
}

rollback() {
  if python3 -c 'import json,sys; sys.exit(0 if "llamacpp_bridge" in json.load(open(sys.argv[1])) else 1)' "$CFG"; then
    python3 - "$CFG" <<'EOF'
import json, os, sys
p = sys.argv[1]
cfg = json.load(open(p))
cfg.pop("llamacpp_bridge", None)
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2, sort_keys=True)
    f.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, p)
EOF
    chown root:root "$CFG"; chmod 0600 "$CFG"
    ok "config.json: llamacpp_bridge removed"
  else
    skip "config.json does not name the bridge"
  fi
  systemctl restart ollama1-gateway.service
  sleep 2
  systemctl is-active --quiet ollama1-gateway.service && ok "gateway restarted on Ollama directly" || die "the gateway did not come back: journalctl -u ollama1-gateway -n 30"
  systemctl disable --now ollama1-llamacpp.service 2>/dev/null && ok "ollama1-llamacpp stopped and disabled" || skip "ollama1-llamacpp was not running"
  say "Ollama serves every model again. $BCFG, $LIBDIR and $OPT are left in place."
}

case "$MODE" in
  check)
    say "would: check the server is quiet, install llama-server from --from into $OPT, install the kit's bridge files,"
    say "       write $BCFG if absent, start ollama1-llamacpp, run its check, set llamacpp_bridge in $CFG, restart the gateway."
    say "now:"
    [ -x "$OPT/current/llama-server" ] && ok "llama-server: $OPT/current" || skip "llama-server: not installed under $OPT (needs --from)"
    [ -f "$BCFG" ] && ok "$BCFG present" || skip "$BCFG absent (would be written, disabled, no models)"
    systemctl is-active --quiet ollama1-llamacpp.service 2>/dev/null && ok "bridge active" || skip "bridge not running"
    python3 -c 'import json,sys; print("  gateway upstream:", json.load(open(sys.argv[1])).get("llamacpp_bridge") or "Ollama directly")' "$CFG"
    command -v nvidia-smi >/dev/null && ok "nvidia-smi present (VRAM of the child is read from it)" || say "  !!   nvidia-smi missing: every bridged model would be refused as not on the GPU"
    quiet_check || true
    ;;
  install)
    quiet_check
    install_build
    install_files
    point_gateway
    verify
    ;;
  verify) verify ;;
  rollback) rollback ;;
esac
