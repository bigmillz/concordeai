#!/usr/bin/env bash
# ram-model-test.sh: a controlled first load of a big model that needs
# system memory (a 'ram' model), before anyone relies on it.
#
#   sudo bash ram-model-test.sh [model] [num_ctx]     (default: gpt-oss:120b 4096)
#
# Loading gpt-oss:120b straight into Ollama (mmap, no memory cap) once froze
# the whole desktop. This script tries it again with the safeguards the kit
# now uses, and watches:
#   1. it caps Ollama's memory for this boot: MemoryMax = RAM - 8 GiB,
#      MemoryHigh 2 GiB below, no swap (systemctl set-property --runtime);
#   2. it unloads whatever is loaded, then loads the model through the local
#      Ollama API with use_mmap=false (Ollama runs llama-server with
#      --load-mode none) and the given num_ctx, and asks for a few tokens;
#   3. once a second it logs free memory, Ollama's own memory, swap and VRAM,
#      and, as a last resort, kills Ollama if the machine gets under 1.5 GiB
#      free (so even a cap that failed can't freeze the desktop);
#   4. it reports: loaded (GPU/RAM split, tokens per second, peak memory), or
#      stopped by Ollama's memory limit (the desktop stayed up), or stopped by
#      the watchdog; then it unloads the model.
# Worst case, if the cap works: Ollama's runner is killed and Ollama carries on.
# Everything is logged to /var/log/ollama1-ram-test.log. No prompt text other
# than the fixed test prompt below is sent.
set -euo pipefail

MODEL=${1:-gpt-oss:120b}
CTX=${2:-4096}
API=${OLLAMA1_OLLAMA_URL:-http://127.0.0.1:11434}
LOG=/var/log/ollama1-ram-test.log
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB=/usr/local/lib/ollama1/lib
[ -f "$LIB/o1ollama.py" ] || LIB="$HERE/../lib"

die() { echo "STOPPED: $*"; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run it with sudo"
case "$CTX" in ''|*[!0-9]*) die "num_ctx must be a number" ;; esac
for c in curl python3 systemctl awk; do command -v "$c" >/dev/null || die "$c is missing"; done
curl -fsS --max-time 5 "$API/api/version" >/dev/null || die "Ollama isn't answering on $API (systemctl status ollama)"

mem_total=$(awk '/^MemTotal:/{print $2 * 1024; exit}' /proc/meminfo)
mem_max=$((mem_total - (8 << 30)))
mem_high=$((mem_max - (2 << 30)))
CG=$(systemctl show -p ControlGroup --value ollama.service)
CGDIR=/sys/fs/cgroup$CG
[ -n "$CG" ] && [ -f "$CGDIR/memory.events" ] || die "can't find ollama.service's cgroup ($CGDIR)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

gib() { awk -v b="$1" 'BEGIN{printf "%.1f", b / 1073741824}'; }

# The gateway's own arithmetic for this model (lib/o1ollama.py, lib/o1stats.py).
verdict=$(OLLAMA1_OLLAMA_URL="$API" PYTHONPATH="$LIB" python3 - "$MODEL" "$CTX" "$mem_max" <<'PY'
import json, os, sys, urllib.request
import o1ollama, o1stats
model, ctx, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
def get(p, body=None):
    req = urllib.request.Request(os.environ["OLLAMA1_OLLAMA_URL"] + p, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))
tags = {m["name"]: m for m in get("/api/tags")["models"]}
entry = next((m for n, m in tags.items() if o1ollama.same_model(n, model)), None)
if entry is None:
    print("MISSING"); sys.exit(0)
info = get("/api/show", {"model": model}).get("model_info", {})
need, kv = o1ollama.fit_estimate(entry["size"], info, ctx, ram=True)
mi = o1stats.meminfo()
vram = max(0, o1stats.gpu_vram_total() - (768 << 20))
margin = max(8 << 30, int(mi.get("MemTotal", 0) * 0.12))
ram_part = min(mi.get("MemAvailable", 0) - margin, cap - (1 << 30))
budget = vram + max(0, ram_part)
print("%s %d %d %d %d" % ("FITS" if need <= budget else "REFUSED", entry["size"], need, budget, kv or 0))
PY
)
read -r fit size need budget kv <<<"$verdict"
[ "$fit" != MISSING ] || die "$MODEL isn't installed (sudo ollama pull $MODEL, after adding it to /etc/ollama1/models.allow)"

cat <<EOF

Controlled load test: $MODEL, num_ctx $CTX
  model file            $(gib "$size") GiB
  gateway's estimate    $(gib "$need") GiB resident (weights, KV cache $(gib "$kv") GiB, compute buffers)
  gateway's budget      $(gib "$budget") GiB (VRAM less 768 MiB, plus free RAM less max(8 GiB, 12%), under the cap)
  gateway's verdict     $fit
  Ollama's memory cap   MemoryMax $(gib "$mem_max") GiB, MemoryHigh $(gib "$mem_high") GiB, no swap (this boot only)
  watchdog              kills Ollama if the machine gets under 1.5 GiB free

Anything loaded now is unloaded first. The load can take several minutes.
If the cap works, the worst case is Ollama's runner being killed.
EOF
[ "$fit" = FITS ] || echo "Note: the gateway would refuse this model at num_ctx $CTX. The test still shows what really happens."
printf 'Type yes to start: '
read -r ans </dev/tty
[ "$ans" = yes ] || { echo "Nothing done."; exit 1; }

touch "$LOG"; chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
printf '\n===== ram model test %s: %s num_ctx %s =====\n' "$(date -Is)" "$MODEL" "$CTX"

systemctl set-property --runtime ollama.service MemoryMax="$mem_max" MemoryHigh="$mem_high" MemorySwapMax=0
got_max=$(systemctl show -p MemoryMax --value ollama.service)
[ "$got_max" = "$mem_max" ] || die "the memory cap didn't take (MemoryMax is $got_max); not loading"
echo "cap in place: MemoryMax $(gib "$mem_max") GiB"

for m in $(curl -fsS "$API/api/ps" | python3 -c 'import json,sys; print(" ".join(m["name"] for m in json.load(sys.stdin).get("models", [])))'); do
  echo "unloading $m"
  curl -fsS -o /dev/null "$API/api/generate" -d "{\"model\": \"$m\", \"keep_alive\": 0}" || true
done
sleep 2

oom_before=$(awk '/^oom_kill /{print $2}' "$CGDIR/memory.events")
since=$(date '+%Y-%m-%d %H:%M:%S')
vram_file=""
for f in /sys/class/drm/card*/device/mem_info_vram_used; do [ -r "$f" ] && { vram_file=$f; break; }; done

monitor() {
  local low=999999999999 t=0
  echo "  s  free GiB  Ollama GiB  swap free GiB  VRAM GiB  procs"
  while [ ! -f "$TMP/done" ]; do
    avail=$(awk '/^MemAvailable:/{print $2 * 1024}' /proc/meminfo)
    swapf=$(awk '/^SwapFree:/{print $2 * 1024}' /proc/meminfo)
    cur=$(cat "$CGDIR/memory.current" 2>/dev/null || echo 0)
    vram=$([ -n "$vram_file" ] && cat "$vram_file" || echo 0)
    procs=$(wc -l <"$CGDIR/cgroup.procs" 2>/dev/null || echo 0)
    [ "$avail" -lt "$low" ] && low=$avail && echo "$low" >"$TMP/low"
    if [ "$procs" -gt 1 ] && [ ! -f "$TMP/procs" ]; then
      ps -o pid=,args= -p "$(paste -sd, "$CGDIR/cgroup.procs")" 2>/dev/null | cut -c1-100 >"$TMP/procs" || true
    fi
    printf '%4d  %8s  %10s  %13s  %8s  %5s\n' "$t" "$(gib "$avail")" "$(gib "$cur")" "$(gib "$swapf")" "$(gib "$vram")" "$procs"
    if [ "$avail" -lt $((3 << 29)) ]; then
      echo "WATCHDOG: under 1.5 GiB free; killing Ollama"
      systemctl kill --signal=KILL ollama.service || true
      touch "$TMP/watchdog"
    fi
    sleep 1
    t=$((t + 1))
  done
}
monitor &
mon=$!

body=$(python3 -c 'import json,sys; print(json.dumps({"model": sys.argv[1], "prompt": "Reply with one word: ready.", "stream": False,
  "options": {"num_ctx": int(sys.argv[2]), "use_mmap": False, "num_predict": 32}}))' "$MODEL" "$CTX")
code=$(curl -sS --max-time 1500 -o "$TMP/resp.json" -w '%{http_code}' "$API/api/generate" -d "$body" || echo 000)
ps_json=$(curl -fsS --max-time 10 "$API/api/ps" || echo '{}')
touch "$TMP/done"
wait "$mon" || true

oom_after=$(awk '/^oom_kill /{print $2}' "$CGDIR/memory.events")
peak=$(cat "$CGDIR/memory.peak" 2>/dev/null || echo 0)
low=$(cat "$TMP/low" 2>/dev/null || echo 0)
mmap_warn=$(journalctl -u ollama --since "$since" --no-pager -o cat 2>/dev/null | grep -c "mmap enabled" || true)
load_none=$(journalctl -u ollama --since "$since" --no-pager -o cat 2>/dev/null | grep -c -- "--load-mode none" || true)

echo
echo "===== result ====="
if [ -f "$TMP/procs" ]; then echo "processes in Ollama's cgroup during the load:"; sed 's/^/  /' "$TMP/procs"; fi
echo "lowest free memory on the machine: $(gib "$low") GiB; Ollama's peak: $(gib "$peak") GiB (cap $(gib "$mem_max"))"
echo "mmap: $([ "$mmap_warn" = 0 ] && echo "no 'mmap enabled' warning" || echo "WARNING: Ollama still logged 'mmap enabled'")$([ "$load_none" -gt 0 ] && echo ", --load-mode none seen")"
if [ -f "$TMP/watchdog" ]; then
  echo "VERDICT: STOPPED BY THE WATCHDOG. Free memory fell under 1.5 GiB although Ollama was capped;"
  echo "         the cap did not hold. Do not use this model. Send $LOG."
elif [ "$oom_after" -gt "$oom_before" ]; then
  echo "VERDICT: STOPPED BY OLLAMA'S MEMORY LIMIT (oom_kill $oom_before -> $oom_after). The desktop stayed up."
  echo "         $MODEL does not fit at num_ctx $CTX with this cap. Try a smaller num_ctx, or leave it out."
elif [ "$code" = 200 ]; then
  python3 - "$MODEL" "$ps_json" "$TMP/resp.json" <<'PY'
import json, sys
model, ps, resp = sys.argv[1], json.loads(sys.argv[2] or "{}"), json.load(open(sys.argv[3]))
m = next((m for m in ps.get("models", []) if m.get("name", "").split(":")[0] == model.split(":")[0]), {})
size, vram = m.get("size", 0), m.get("size_vram", 0)
ev, ed = resp.get("eval_count", 0), resp.get("eval_duration", 0)
print("VERDICT: LOADED.")
print("  in VRAM   %.1f GiB (%d%%), in system memory %.1f GiB" % (vram / 2**30, 100 * vram / size if size else 0,
                                                              (size - vram) / 2**30))
print("  context   %s" % m.get("context_length"))
print("  load      %.0f s" % (resp.get("load_duration", 0) / 1e9))
print("  speed     %.1f tokens/s (%d tokens)" % (ev / (ed / 1e9) if ed else 0, ev))
PY
else
  echo "VERDICT: THE LOAD FAILED (HTTP $code), without an OOM kill:"
  head -c 400 "$TMP/resp.json" 2>/dev/null; echo
fi

echo
echo "unloading $MODEL"
curl -fsS -o /dev/null --max-time 60 "$API/api/generate" -d "{\"model\": \"$MODEL\", \"keep_alive\": 0}" || true
echo "The memory cap set here lasts until the next reboot; setup.sh makes it permanent."
echo "Log: $LOG"
