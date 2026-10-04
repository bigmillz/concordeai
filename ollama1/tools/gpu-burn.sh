#!/usr/bin/env bash
# The graphics card at 100%, the processor left alone, nothing else:
#   sudo bash gpu-burn.sh                 60 minutes
#   sudo bash gpu-burn.sh 15              15 minutes
#   sudo bash gpu-burn.sh 60 gemma4:26b   a different installed model
# It runs llama.cpp's own benchmark (llama-bench, ONE processor thread) over and over on the
# model file Ollama already holds, so the card does long-prompt work and long-answer work
# back to back with no pauses, and the processor stays near idle. Every 30 s it prints the
# card's busy percent, its temperatures, its power, and the processor's busy percent. It stops
# with the reason on a new hardware-error record, on heat, or when the card isn't busy.
#
# llama-bench: set O1_LLAMA_BENCH, or have it in ~/llama-bench/<release>/llama-bench
# (a Vulkan Ubuntu build from https://github.com/ggml-org/llama.cpp/releases, needs libvulkan1
# and mesa-vulkan-drivers). Output also goes to /var/log/ollama1-gpu-burn.log.
set -u
MIN=${1:-60}
MODEL=${2:-gemma4:12b}
LOG=${O1_GPU_BURN_LOG:-/var/log/ollama1-gpu-burn.log}
DRM=${O1_DRM:-/sys/class/drm}
HWMON=${O1_HWMON:-/sys/class/hwmon}
MODELS=${O1_MODELS:-/srv/models}
TICK=${O1_BURN_TICK:-5}
ABORT_JUNCTION_C=105
[[ "$MIN" =~ ^[0-9]{1,3}$ ]] && [ "$MIN" -ge 1 ] || { echo "minutes: a whole number, like 15"; exit 2; }
[[ "$MODEL" =~ ^[A-Za-z0-9._:/-]{1,80}$ ]] || { echo "model: a name like gemma4:12b"; exit 2; }
[ "$(id -u)" -eq 0 ] || [ "${O1_BURN_NOROOT:-}" = 1 ] || { echo "Run it with sudo."; exit 1; }
say() { printf '%s\n' "$*" | tee -a "$LOG"; }

BENCH=${O1_LLAMA_BENCH:-}
if [ -z "$BENCH" ]; then
  for b in /home/*/llama-bench/*/llama-bench /root/llama-bench/*/llama-bench; do [ -x "$b" ] && { BENCH=$b; break; }; done
fi
[ -x "${BENCH:-/nonexistent}" ] || { echo "llama-bench not found: set O1_LLAMA_BENCH or put a build in ~/llama-bench/<release>/llama-bench"; exit 1; }

name=${MODEL%%:*}; tag=${MODEL#*:}; [ "$tag" != "$MODEL" ] || tag=latest
man="$MODELS/manifests/registry.ollama.ai/library/$name/$tag"
[ -r "$man" ] || { echo "$MODEL isn't installed ($man)"; exit 1; }
digest=$(python3 -c 'import json,sys
m=json.load(open(sys.argv[1]))
print([l["digest"] for l in m["layers"] if l["mediaType"].endswith("model")][0].replace(":","-"))' "$man") || { echo "can't read $man"; exit 1; }
BLOB="$MODELS/blobs/$digest"
[ -r "$BLOB" ] || { echo "model file missing: $BLOB"; exit 1; }

hwtemp() { local d f; for d in "$HWMON"/hwmon*; do [ "$(cat "$d/name" 2>/dev/null)" = "$1" ] || continue
  f=$d/$2; [ -r "$f" ] && { echo $(( $(cat "$f") / 1000 )); return; }; done; }
gpu_busy() { local f; for f in "$DRM"/card*/device/gpu_busy_percent; do [ -r "$f" ] && { cat "$f"; return; }; done; }
gpu_watts() { local d f; for d in "$HWMON"/hwmon*; do [ "$(cat "$d/name" 2>/dev/null)" = amdgpu ] || continue
  for f in power1_average power1_input; do [ -r "$d/$f" ] && { echo $(( $(cat "$d/$f") / 1000000 )); return; }; done; done; }
cpu_stat() { awk '/^cpu /{print $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' "${O1_PROC_STAT:-/proc/stat}"; }
hw_errors() { journalctl -k -b --no-pager 2>/dev/null | grep -c -i -E 'amdgpu.*(ring.*timeout|gpu reset|hang)|nvme.*(timeout|reset|fail)|Machine check|Hardware Error|AER:' || true; }

say ""
say "ollama1 gpu burn $(date '+%F %T'): $MODEL, $MIN min, one processor thread, $BENCH"
errs0=$(hw_errors)
secs=${O1_BURN_SECONDS:-$((MIN * 60))}
REPORT=${O1_BURN_REPORT:-30}; WARM=${O1_BURN_WARMUP:-90}
timeout "$secs" bash -c 'while :; do "$0" -m "$1" -ngl 99 -t 1 -p 2048 -n 256 -r 2 >/dev/null 2>>"$2"; done' "$BENCH" "$BLOB" "$LOG.err" &
LOAD=$!
read -r b0 t0 < <(cpu_stat)
start=$(date +%s); last=$start; sum=0; cnt=0; verdict=OK; why=""
while kill -0 "$LOAD" 2>/dev/null; do
  sleep "$TICK"
  now=$(date +%s); b=$(gpu_busy); b=${b:-0}; j=$(hwtemp amdgpu temp2_input)
  sum=$((sum + b)); cnt=$((cnt + 1))
  if [ -n "${j:-}" ] && [ "$j" -ge "$ABORT_JUNCTION_C" ]; then verdict=ABORTED; why="the card reached $j C"; fi
  if [ $((now - last)) -ge "$REPORT" ]; then
    read -r b1 t1 < <(cpu_stat); cpu=0; [ "$t1" -gt "$t0" ] && cpu=$(( 100 * (b1 - b0) / (t1 - t0) )); b0=$b1; t0=$t1
    avg=$((sum / cnt)); sum=0; cnt=0; last=$now
    say "   $(date +%T)  card busy ${avg}%, edge $(hwtemp amdgpu temp1_input)C junction ${j:-?}C, $(gpu_watts) W, processor busy ${cpu}%"
    if [ $((now - start)) -ge "$WARM" ] && [ "$avg" -lt 40 ]; then verdict=FAIL; why="the card was only ${avg}% busy (see $LOG.err)"; fi
  fi
  [ "$verdict" = OK ] || { pkill -P "$LOAD" 2>/dev/null; kill "$LOAD" 2>/dev/null; pkill -f "$BENCH" 2>/dev/null; break; }
done
wait "$LOAD" 2>/dev/null
errs1=$(hw_errors)
if [ "$verdict" = OK ] && [ "$errs1" -gt "$errs0" ]; then verdict=FAIL; why="$((errs1 - errs0)) new hardware-error record(s) in the kernel log"; fi
if [ "$verdict" = OK ]; then say ""; say "PASSED: $MIN minutes, no hardware error."; exit 0; fi
say ""; say "$verdict: $why"; exit 1
