#!/usr/bin/env bash
# stability-test.sh: does this server stay up under sustained load?
#
# The server once reset itself with a fatal CPU hardware error ("Machine
# Check") while loading a big model, so this runs the loads that found it,
# all at once by default (or one phase at a time), and keeps a record that
# survives a crash:
#
#   cpu      every core, floating-point maths, results checked
#   memory   about 60% of the free memory, written and read back, checked
#   gpu      the graphics card, reading long prompts and writing long answers
#            nonstop (Ollama), which loads its compute and its memory both
#   all      all three at once (the combination that crashed it)
#
#   sudo bash stability-test.sh                 one load at a time: cpu, then gpu, then memory, 10 minutes each
#   sudo bash stability-test.sh --minutes 30    a longer soak
#   sudo bash stability-test.sh --phases cpu,gpu,memory,all   each alone first, then all at once
#   sudo bash stability-test.sh --model gemma4:12b   the model for the gpu load
#   bash stability-test.sh status               the last run, and any hardware
#                                               errors logged since this boot
#
# It watches the temperatures and stops a phase that gets too hot (CPU 95 C,
# graphics junction 105 C). It counts hardware-error records in the kernel
# log before and after each phase: any new one fails the phase. If the
# machine resets in the middle, `status` after the restart says which phase
# was running and shows the hardware error the crash left behind.
# Installs stress-ng (apt) if it isn't there. Output also goes to
# /var/log/ollama1-stability.log; the record is /var/lib/ollama1/stability.state.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR=${O1_STATE_DIR:-/var/lib/ollama1}
STATE=$STATE_DIR/stability.state
LOG=${O1_STABILITY_LOG:-/var/log/ollama1-stability.log}
HWMON=${O1_HWMON:-/sys/class/hwmon}
OLLAMA_URL=${O1_OLLAMA_URL:-http://127.0.0.1:11434}
TICK=${O1_STABILITY_TICK:-5}
CPU_ABORT_C=95
GPU_ABORT_C=105
MODEL=gemma4:12b
MINUTES=10
SECS_OVERRIDE=""
PHASES=cpu,gpu,memory

usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; }
say() { printf '%s\n' "$*" | tee -a "$LOG"; }

# The arguments, quoted to be pasted into the command tmux runs. With none,
# printf '%q ' would still print '' (an empty argument), so it's only used
# when there are some.
quoted_args() {
  if [ "$#" -gt 0 ]; then printf ' %q' "$@"; fi
}

# whole degrees C from a hwmon chip (name, file), or nothing
hwtemp() {
  local d f
  for d in "$HWMON"/hwmon*; do
    [ "$(cat "$d/name" 2>/dev/null)" = "$1" ] || continue
    f=$d/$2
    [ -r "$f" ] || continue
    awk '{printf "%d", $1/1000}' "$f"
    return
  done
}
cpu_temp() { hwtemp k10temp temp1_input; }
gpu_temp() { local t; t=$(hwtemp amdgpu temp2_input); [ -n "$t" ] || t=$(hwtemp amdgpu temp1_input); printf '%s' "$t"; }

# hardware-error records the kernel has logged this boot (after a crash, the
# one it left behind is logged in the first seconds of the next boot)
hw_error_lines() {
  journalctl -k -b 0 --no-pager -q 2>/dev/null | grep -iE 'mce: \[Hardware Error\]' || true
}
hw_errors() { hw_error_lines | wc -l | tr -d ' '; }

state() { # state KEY=VALUE ... : rewrite the record's header lines, keep the results
  local tmp="$STATE.tmp" k
  { for k in "$@"; do printf '%s\n' "$k"; done; grep '^result ' "$STATE" 2>/dev/null || true; } > "$tmp" && mv "$tmp" "$STATE"
}
add_result() { printf '%s\n' "$1" >> "$STATE"; }

cmd_status() {
  if [ ! -r "$STATE" ]; then echo "No run recorded yet."; else cat "$STATE"; fi
  if grep -q '^status=running' "$STATE" 2>/dev/null; then
    echo
    echo "That run did not finish: the machine restarted (or the script was killed) during"
    echo "phase $(sed -n 's/^phase=//p' "$STATE" | head -1)."
  fi
  local n
  n=$(hw_errors)
  echo
  echo "Hardware-error records in the kernel log since this boot: $n"
  [ "$n" -eq 0 ] || hw_error_lines | head -6 | cut -c1-200
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  status) cmd_status; exit 0 ;;
esac

while [ "$#" -gt 0 ]; do
  case "$1" in
    --minutes) MINUTES=${2:-}; shift 2 ;;
    --phases) PHASES=${2:-}; shift 2 ;;
    --model) MODEL=${2:-}; shift 2 ;;
    --seconds) SECS_OVERRIDE=${2:-}; shift 2 ;;     # for the tests
    *) echo "unknown option: $1"; usage; exit 2 ;;
  esac
done
[[ "$MINUTES" =~ ^[0-9]{1,3}$ ]] && [ "$MINUTES" -ge 1 ] || { echo "--minutes takes a whole number, like 5"; exit 2; }
[[ "$MODEL" =~ ^[A-Za-z0-9._:/-]{1,80}$ ]] || { echo "--model takes a model name, like gemma4:12b"; exit 2; }
[ -z "$SECS_OVERRIDE" ] || [[ "$SECS_OVERRIDE" =~ ^[0-9]{1,4}$ ]] || { echo "--seconds takes a whole number"; exit 2; }
IFS=, read -r -a PHASE_LIST <<< "$PHASES"
for p in "${PHASE_LIST[@]}"; do
  case "$p" in cpu|memory|gpu|all) ;; *) echo "unknown phase: $p (cpu, memory, gpu, all)"; exit 2 ;; esac
done

[ "$(id -u)" -eq 0 ] || [ "${O1_STABILITY_NOROOT:-}" = 1 ] || { echo "Run it with sudo."; exit 1; }

# In tmux, so a dropped SSH connection can't stop it halfway (reattach:
# sudo tmux attach -t ollama1-stress).
if [ -z "${TMUX:-}" ] && [ "${O1_NO_TMUX:-}" != 1 ] && [ -t 0 ] && command -v tmux >/dev/null; then
  echo "Starting in tmux (session ollama1-stress). If the connection drops: sudo tmux attach -t ollama1-stress"
  exec tmux new-session -A -s ollama1-stress \
    "O1_NO_TMUX=1 bash '$HERE/stability-test.sh'$(quoted_args "$@"); echo; read -r -p 'Finished. Press Enter to close. ' _"
fi

mkdir -p "$STATE_DIR"
touch "$LOG"; chmod 600 "$LOG" 2>/dev/null || true
if ! command -v stress-ng >/dev/null; then
  say "Installing stress-ng (apt)..."
  apt-get install -y -q stress-ng >>"$LOG" 2>&1 || { say "Couldn't install stress-ng."; exit 1; }
fi

WANT_GPU=0
for p in "${PHASE_LIST[@]}"; do case "$p" in gpu|all) WANT_GPU=1 ;; esac; done
if [ "$WANT_GPU" -eq 1 ]; then
  # the answer is read whole, then searched: `curl | grep -q` under pipefail
  # fails when grep stops at the first match and curl gets SIGPIPE on a long list
  tags=$(curl -fsS -m 10 "$OLLAMA_URL/api/tags" 2>/dev/null) || tags=""
  if ! grep -q "\"name\":\"$MODEL\"" <<<"$tags"; then
    say "Ollama isn't answering, or $MODEL isn't installed. Pick an installed one with --model, or run --phases cpu,memory."
    exit 1
  fi
fi

mem_kb() { # 60% of the memory that's free now, as kB per worker for $1 workers
  awk -v w="$1" '/^MemAvailable:/ {printf "%d", $2 * 0.6 / w}' /proc/meminfo
}

LOADS=()
GPU_OK=""
start_loads() { # start_loads PHASE SECONDS : sets LOADS (pids)
  local name=$1 secs=$2
  LOADS=()
  case "$name" in
    cpu)    stress-ng --cpu 0 --cpu-method matrixprod --verify -t "${secs}s" --metrics-brief >>"$LOG" 2>&1 & LOADS+=($!) ;;
    memory) stress-ng --vm 16 --vm-bytes "$(mem_kb 16)k" --vm-method all --verify -t "${secs}s" --metrics-brief >>"$LOG" 2>&1 & LOADS+=($!) ;;
    gpu)    gpu_load "$secs" & LOADS+=($!) ;;
    all)    stress-ng --cpu 0 --cpu-method all --vm 8 --vm-bytes "$(mem_kb 8)k" --vm-method all --verify -t "${secs}s" --metrics-brief >>"$LOG" 2>&1 & LOADS+=($!)
            gpu_load "$secs" & LOADS+=($!) ;;
  esac
}
gpu_load() { # answers questions nonstop for $1 seconds; one line in $GPU_OK per answer
  # Alternates two kinds of work: a long prompt to read (about 4000 tokens, few
  # words back) which is compute-heavy, and a short prompt with a long answer,
  # which leans on memory speed. Either alone leaves the card part-idle.
  timeout "$1" bash -c '
    long=$(yes "The quick brown fox jumps over the lazy dog near the old stone bridge." | head -n 320 | tr "\n" " ")
    n=0
    while :; do
      n=$((n + 1))
      if [ $((n % 2)) -eq 1 ]; then
        body="{\"model\":\"$1\",\"prompt\":\"Say ok after reading: $long\",\"stream\":false,\"keep_alive\":\"10m\",\"options\":{\"num_ctx\":8192,\"num_predict\":8}}"
      else
        body="{\"model\":\"$1\",\"prompt\":\"Count from 1 to 300 in words.\",\"stream\":false,\"keep_alive\":\"10m\",\"options\":{\"num_ctx\":8192,\"num_predict\":500}}"
      fi
      if curl -fsS -m 300 "$0/api/generate" -d "$body" >/dev/null 2>&1; then
        echo ok >> "$2"
        curl -fsS -m 10 "$0/api/ps" > "$2.ps" 2>/dev/null    # where the model is loaded: see the check below
      fi
    done' "$OLLAMA_URL" "$MODEL" "$GPU_OK" >/dev/null 2>&1
}
any_alive() { local p; for p in "$@"; do kill -0 "$p" 2>/dev/null && return 0; done; return 1; }

run_phase() {
  local name=$1 secs=$2 errs0 errs1 maxc='' maxg='' c g verdict="OK" why="" p rc n=0
  say ""
  say "NEXT: $name for $((secs / 60)) min ($secs s). If the server crashes now, $name is the one."
  say "== $name, $secs s   ($(date '+%H:%M:%S'))"
  state "status=running" "phase=$name" "started=$(date '+%Y-%m-%dT%H:%M:%S%z')" "minutes=$MINUTES"
  GPU_OK=$(mktemp)
  errs0=$(hw_errors)
  start_loads "$name" "$secs"
  while any_alive "${LOADS[@]}"; do
    sleep "$TICK"
    c=$(cpu_temp); g=$(gpu_temp)
    if [ -n "$c" ] && { [ -z "$maxc" ] || [ "$c" -gt "$maxc" ]; }; then maxc=$c; fi
    if [ -n "$g" ] && { [ -z "$maxg" ] || [ "$g" -gt "$maxg" ]; }; then maxg=$g; fi
    n=$((n + 1))
    [ $((n * TICK % 30)) -ne 0 ] || say "   cpu ${c:-?} C, gpu ${g:-?} C"
    if [ -n "$c" ] && [ "$c" -ge "$CPU_ABORT_C" ]; then verdict="ABORTED"; why="CPU reached $c C"; fi
    if [ -n "$g" ] && [ "$g" -ge "$GPU_ABORT_C" ]; then verdict="ABORTED"; why="graphics junction reached $g C"; fi
    if [ "$verdict" = ABORTED ]; then
      for p in "${LOADS[@]}"; do pkill -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done
      pkill stress-ng 2>/dev/null
      break
    fi
  done
  for p in "${LOADS[@]}"; do
    wait "$p"; rc=$?
    # stress-ng exits 0 when its checks pass; the gpu loop ends by timeout (124)
    # and is judged by its answers below
    if [ "$verdict" = OK ] && [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ]; then
      verdict="FAIL"; why="a load exited with status $rc (a stress-ng check failed)"
    fi
  done
  errs1=$(hw_errors)
  if [ "$errs1" -gt "$errs0" ]; then verdict="FAIL"; why="$((errs1 - errs0)) new hardware-error record(s) in the kernel log"; fi
  case "$name" in gpu|all)
    if [ "$verdict" = OK ] && [ ! -s "$GPU_OK" ]; then verdict="FAIL"; why="Ollama gave no answer during the phase"; fi ;;
  esac
  case "$name" in gpu|all)
    # The load only tests the card if Ollama really put the model on it. After a reboot
    # Ollama once started before the driver was ready and ran everything on the CPU.
    on_card=$(grep -o '"size_vram":[0-9]*' "$GPU_OK.ps" 2>/dev/null | grep -vc ':0$' || true)
    if [ "$verdict" = OK ] && [ "${on_card:-0}" -eq 0 ]; then
      verdict="FAIL"; why="Ollama ran the model on the CPU, so the graphics card wasn't tested (restart Ollama and check the driver)"
    fi ;;
  esac
  rm -f "$GPU_OK" "$GPU_OK.ps"
  say "   $verdict${why:+: $why}   (max cpu ${maxc:-?} C, gpu ${maxg:-?} C)"
  add_result "result $name $verdict max_cpu_c=${maxc:-?} max_gpu_c=${maxg:-?}${why:+ note=\"$why\"}"
  [ "$verdict" = OK ]
}

say ""
say "ollama1 stability test $(date '+%F %T'): phases ${PHASES}, $MINUTES min each, gpu model $MODEL"
say "Hardware-error records already in this boot's log: $(hw_errors)"
: > "$STATE"
failed=0
for p in "${PHASE_LIST[@]}"; do
  secs=$((MINUTES * 60)); [ -z "$SECS_OVERRIDE" ] || secs=$SECS_OVERRIDE
  run_phase "$p" "$secs" || failed=1
  [ "$failed" -eq 0 ] || break      # the first failure is the answer; no point pushing on
done
if [ "$failed" -eq 0 ]; then
  state "status=done" "finished=$(date '+%Y-%m-%dT%H:%M:%S%z')"
  say ""; say "PASSED: every phase finished with no error and no hardware-error record."
else
  state "status=failed" "finished=$(date '+%Y-%m-%dT%H:%M:%S%z')"
  say ""; say "FAILED: see the phase above. Results: $STATE"
fi
exit "$failed"
