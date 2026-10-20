#!/usr/bin/env bash
# quick-burn.sh (6b418): a 30-second processor burn, then a 30-second graphics-card burn.
# Started from the server's screen (Enter on the keyboard) through ollama1-quickburn.service,
# or by hand:   sudo bash quick-burn.sh
#
#   1. processor: stress-ng on every thread, matrix maths, results verified (the same load as
#      the cpu phase of stability-test.sh), 30 s
#   2. graphics card: gpu-burn.sh (llama-bench, ~/llama-bench/<release>/llama-bench of any user,
#      on a model Ollama holds), 30 s. If llama-bench or the model is missing the card part says
#      "not run: <reason>" and the processor's result stands.
#
# Safety, as in the stability scripts: it stops on a new hardware-error record in the kernel log,
# at CPU 95 C, graphics junction 105 C (AMD) or chip 90 C (NVIDIA), and at once when /run/ollama1/quickburn/abort exists
# (the panel makes it on Esc; only its existence is looked at). It refuses to start while a
# request is in flight, a model download or update runs, or another stability or burn test runs,
# and says why. stress-ng must be installed (the unit has no network): sudo apt install stress-ng.
#
# Progress: /run/ollama1/quickburn.json every second (phase cpu|gpu|done, seconds_left, the
# temperatures, the card's and the processor's busy percent, result, reason). The last result,
# in one line: /var/lib/ollama1/quickburn-last.json. When the processor part fails, stress-ng's last 20
# output lines are kept in /var/lib/ollama1/quickburn-last-output.txt.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECS=${O1_QB_SECONDS:-30}
TICK=${O1_QB_TICK:-1}
RUN=${O1_RUN_DIR:-/run/ollama1}
STATE_DIR=${O1_STATE_DIR:-/var/lib/ollama1}
LIBDIR=${O1_LIB_DIR:-$HERE/../lib}
HWMON=${O1_HWMON:-/sys/class/hwmon}
DRM=${O1_DRM:-/sys/class/drm}
PROG=$RUN/quickburn.json
ABORT=$RUN/quickburn/abort
LAST=$STATE_DIR/quickburn-last.json
GPU_BURN=${O1_GPU_BURN:-$HERE/gpu-burn.sh}
CPU_ABORT_C=95
GPU_ABORT_C=105
NV_ABORT_C=90           # an NVIDIA card: its one chip temperature (6b484)
NVSMI=${O1_NVIDIA_SMI:-nvidia-smi}

[ "$(id -u)" -eq 0 ] || [ "${O1_QB_NOROOT:-}" = 1 ] || { echo "Run it with sudo."; exit 1; }
mkdir -p "$RUN" "$STATE_DIR" 2>/dev/null

jstr() { printf '%s' "$1" | tr -d '\000-\037"\\' | cut -c1-200; }
num() { [ -n "${1:-}" ] && printf '%s' "$1" || printf 'null'; }
hwtemp() { local d f; for d in "$HWMON"/hwmon*; do [ "$(cat "$d/name" 2>/dev/null)" = "$1" ] || continue
  f=$d/$2; [ -r "$f" ] && { echo $(( $(cat "$f") / 1000 )); return; }; done; }
cpu_temp() { hwtemp k10temp temp1_input; }
nv() { "$NVSMI" --query-gpu="$1" --format=csv,noheader,nounits 2>/dev/null | head -1 | awk -F. '/^ *[0-9]/{gsub(/ /,"",$1); print $1}'; }
gpu_temp() { local t; t=$(hwtemp amdgpu temp2_input); [ -n "$t" ] || t=$(hwtemp amdgpu temp1_input); [ -n "$t" ] || t=$(nv temperature.gpu); printf '%s' "$t"; }
gpu_limit() { if [ -n "$(hwtemp amdgpu temp1_input)" ]; then echo "$GPU_ABORT_C"; else echo "$NV_ABORT_C"; fi; }
gpu_busy() { local f; for f in "$DRM"/card*/device/gpu_busy_percent; do [ -r "$f" ] && { cat "$f"; return; }; done; nv utilization.gpu; }
cpu_stat() { awk '/^cpu /{print $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' "${O1_PROC_STAT:-/proc/stat}"; }
hw_errors() { journalctl -k -b 0 --no-pager -q 2>/dev/null | grep -ciE 'mce: \[Hardware Error\]|amdgpu.*(ring.*timeout|gpu reset)|NVRM: Xid|AER:' || true; }

PHASE=""; LEFT=0; RESULT=running; REASON=""
write_progress() { # the progress file, replaced whole
  local tmp="$PROG.tmp" cb=$1 gb=$2
  printf '{"at": %d, "phase": "%s", "seconds_left": %d, "seconds_each": %d, "cpu_c": %s, "gpu_c": %s, "gpu_busy": %s, "cpu_busy": %s, "result": "%s", "reason": "%s"}\n' \
    "$(date +%s)" "$PHASE" "$LEFT" "$SECS" "$(num "$(cpu_temp)")" "$(num "$(gpu_temp)")" "$(num "$gb")" "$(num "$cb")" "$RESULT" "$(jstr "$REASON")" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$PROG" 2>/dev/null; chmod 0644 "$PROG" 2>/dev/null; return 0
}
finish() { # finish RESULT REASON [cpu_result] [gpu_result]
  PHASE=done; LEFT=0; RESULT=$1; REASON=$2
  write_progress "" ""
  printf '{"at": %d, "result": "%s", "reason": "%s", "cpu": "%s", "gpu": "%s"}\n' "$(date +%s)" "$RESULT" "$(jstr "$REASON")" \
    "$(jstr "${3:-}")" "$(jstr "${4:-}")" > "$LAST.tmp" 2>/dev/null && mv -f "$LAST.tmp" "$LAST" 2>/dev/null; chmod 0644 "$LAST" 2>/dev/null
  rm -f "$ABORT" 2>/dev/null
  echo "$RESULT${REASON:+: $REASON}"
}

# ---- refuse while something else is going on --------------------------------------------------
refusal() { # prints the reason, or nothing
  python3 - "$LIBDIR" <<'PY'
import os, sys, time
sys.path.insert(0, sys.argv[1])
import o1idle, o1sleep
now = time.time()
act = o1idle.read_activity(now)
if act.get("fresh") and (act.get("inflight") or 0) > 0:
    print("a request is running"); sys.exit(0)
busy = o1sleep.busy_reasons()
if busy:
    print(busy[0]); sys.exit(0)
tools = [t for t in (o1idle.tools_running(os.environ.get("O1_PROC", "/proc")) or []) if t != "quick-burn.sh"]
if tools:
    print("%s is running" % tools[0])
PY
}
mkdir -p "$RUN/quickburn" 2>/dev/null
exec 9>"$RUN/quickburn.lock"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  finish refused "another burn test is already running"; exit 1
fi
rm -f "$ABORT" 2>/dev/null
why=$(refusal 2>/dev/null) || why=""
if [ -n "$why" ]; then finish refused "$why"; exit 1; fi
if ! command -v stress-ng >/dev/null 2>&1; then finish refused "stress-ng is not installed (sudo apt install stress-ng)"; exit 1; fi

# ---- one phase -----------------------------------------------------------------------------------
LOAD_PID=""
command -v setsid >/dev/null 2>&1 || setsid() { "$@"; }       # a new process group where there is one
stop_load() { [ -n "$LOAD_PID" ] && { kill -TERM -- "-$LOAD_PID" 2>/dev/null || kill -TERM "$LOAD_PID" 2>/dev/null; pkill -P "$LOAD_PID" 2>/dev/null; } ; return 0; }
trap 'stop_load; finish aborted "stopped by the system"; exit 1' TERM INT

run_phase() { # run_phase NAME COMMAND...  -> VERDICT (OK|FAIL|ABORTED), WHY
  local name=$1; shift
  PHASE=$name; V=OK; W=""; GB_SUM=0; GB_N=0; GB_SEEN=0
  local errs0 errs1 c g busy cb t0 t1 b0 b1 end now
  errs0=$(hw_errors)
  read -r b0 t0 < <(cpu_stat)
  end=$(( $(date +%s) + SECS ))
  setsid "$@" >>"$OUT" 2>&1 &
  LOAD_PID=$!
  while :; do
    now=$(date +%s); LEFT=$(( end - now )); [ "$LEFT" -ge 0 ] || LEFT=0
    read -r b1 t1 < <(cpu_stat); cb=0; [ "$t1" -gt "$t0" ] && cb=$(( 100 * (b1 - b0) / (t1 - t0) )); b0=$b1; t0=$t1
    gb=$(gpu_busy); write_progress "$cb" "$gb"
    # the card's busy percent counts from the first busy reading: loading the model and (NVIDIA) compiling
    # shaders take seconds before the load starts (6b484)
    if [ "$name" = gpu ] && [ -n "$gb" ]; then
      [ "$GB_N" -gt 0 ] || [ "$gb" -ge 50 ] && { GB_SUM=$((GB_SUM + gb)); GB_N=$((GB_N + 1)); }
      GB_SEEN=$((GB_SEEN + 1))
    fi
    if [ -e "$ABORT" ]; then V=ABORTED; W="stopped from the keyboard"; break; fi
    c=$(cpu_temp); g=$(gpu_temp)
    if [ -n "$c" ] && [ "$c" -ge "$CPU_ABORT_C" ]; then V=ABORTED; W="the processor reached $c C"; break; fi
    if [ -n "$g" ] && [ "$g" -ge "$(gpu_limit)" ]; then V=ABORTED; W="the graphics card reached $g C"; break; fi
    errs1=$(hw_errors)
    if [ "${errs1:-0}" -gt "${errs0:-0}" ]; then V=FAIL; W="$((errs1 - errs0)) new hardware-error record(s) in the kernel log"; break; fi
    kill -0 "$LOAD_PID" 2>/dev/null || break
    [ "$now" -lt "$end" ] || break
    sleep "$TICK"
  done
  if [ "$V" != OK ]; then stop_load; fi
  wait "$LOAD_PID" 2>/dev/null; RC=$?
  LOAD_PID=""
  errs1=$(hw_errors)
  if [ "$V" = OK ] && [ "${errs1:-0}" -gt "${errs0:-0}" ]; then V=FAIL; W="$((errs1 - errs0)) new hardware-error record(s) in the kernel log"; fi
}

OUT=$(mktemp 2>/dev/null || echo /dev/null)
# (6b476) newer stress-ng aborts unless it has a writable temp path, and the unit's sandbox leaves
# only /tmp (private) writable: give it a folder of its own there, removed again on every way out.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/o1qb.XXXXXX" 2>/dev/null || echo "")
[ -n "$WORK" ] || { WORK="$RUN/quickburn/work.$$"; mkdir -p "$WORK" 2>/dev/null; }
trap 'stop_load; rm -rf "$WORK"; rm -f "$OUT"; finish aborted "stopped by the system"; exit 1' TERM INT

# 1. the processor
T_START=$(date +%s)
run_phase cpu stress-ng --temp-path "$WORK" --cpu 0 --cpu-method matrixprod --verify -t "${SECS}s" --metrics-brief
T_TOOK=$(( $(date +%s) - T_START ))
rm -rf "$WORK"
if [ "$V" = OK ] && [ "$RC" -ne 0 ] && [ "$RC" -ne 143 ]; then
  V=FAIL
  NFAIL=$(grep -oE 'failed: [0-9]+' "$OUT" 2>/dev/null | tail -1 | grep -oE '[0-9]+')
  ERRLINE=$(grep -iE 'abort|error|must be|failed to init|cannot|unable' "$OUT" 2>/dev/null | grep -v 'failed: 0' | head -1 | cut -c1-150)
  [ -n "$ERRLINE" ] || ERRLINE=$(grep -v '^[[:space:]]*$' "$OUT" 2>/dev/null | head -1 | cut -c1-150)
  if grep -qiE 'aborting|must be readable|failed to initiali' "$OUT" 2>/dev/null || { [ "$RC" -eq 1 ] && [ "$T_TOOK" -le 2 ] && [ "${NFAIL:-0}" -eq 0 ]; }; then
    W="stress-ng couldn't start: ${ERRLINE:-it exited with status $RC and said nothing}"
  elif [ "${NFAIL:-0}" -gt 0 ]; then
    W="a check failed: ${NFAIL} stressor(s) failed verification (stress-ng exit status $RC)"
  else
    W="stress-ng exited with status $RC (a check failed)"
  fi
fi
if [ "$V" = FAIL ]; then tail -n 20 "$OUT" > "$STATE_DIR/quickburn-last-output.txt" 2>/dev/null; chmod 0644 "$STATE_DIR/quickburn-last-output.txt" 2>/dev/null; fi
CPU_RES="$V${W:+: $W}"
if [ "$V" != OK ]; then
  rm -f "$OUT"
  case "$V" in ABORTED) finish aborted "$W" "$CPU_RES" "not run" ;; *) finish failed "processor: $W" "$CPU_RES" "not run" ;; esac
  exit 1
fi

# 2. the graphics card (its own script: it knows where llama-bench and the model are)
: > "$OUT"
mkdir -p "$STATE_DIR/gpu-cache" 2>/dev/null
run_phase gpu env XDG_CACHE_HOME="$STATE_DIR/gpu-cache" __GL_SHADER_DISK_CACHE_PATH="$STATE_DIR/gpu-cache" O1_GPU_BURN_LOG="${O1_QB_GPU_LOG:-/run/ollama1/quickburn/gpu-burn.log}" O1_BURN_SECONDS="$SECS" O1_BURN_TICK="${O1_BURN_TICK:-2}" O1_BURN_NOROOT=1 bash "$GPU_BURN" 1 "${O1_QB_MODEL:-gemma4:12b}"
if ! grep -q 'ollama1 gpu burn' "$OUT" 2>/dev/null && [ "$V" = OK ]; then          # it stopped before the load began
  why=$(grep -v '^[[:space:]]*$' "$OUT" 2>/dev/null | head -1 | cut -c1-150)
  rm -f "$OUT"
  finish passed "processor passed; graphics card not run: ${why:-it did not start}" "$CPU_RES" "not run: ${why:-it did not start}"
  exit 0
fi
if [ "$V" = OK ] && [ "$GB_SEEN" -ge 3 ] && { [ "$GB_N" -eq 0 ] || [ $((GB_SUM / GB_N)) -lt 30 ]; }; then   # the load never reached the card (6b484)
  if [ "$GB_N" -eq 0 ]; then W="the card never got busy: the load did not run"; else W="the card was only $((GB_SUM / GB_N))% busy once loaded"; fi
  V=FAIL
  grep -v '^[[:space:]]*$' "${O1_QB_GPU_LOG:-/run/ollama1/quickburn/gpu-burn.log}.err" 2>/dev/null | grep -vE 'load_backend|ggml_vulkan' | tail -3 >> "$OUT"
fi
if [ "$V" = OK ] && [ "$RC" -ne 0 ] && [ "$RC" -ne 143 ]; then
  V=FAIL; W=$(grep -E '^(FAIL|ABORTED)' "$OUT" | tail -1 | cut -c1-150); W=${W:-the card test failed}
fi
rm -f "$OUT"
case "$V" in
  OK) finish passed "" "$CPU_RES" "OK"; exit 0 ;;
  ABORTED) finish aborted "$W" "$CPU_RES" "ABORTED: $W"; exit 1 ;;
  *) finish failed "graphics card: $W" "$CPU_RES" "FAIL: $W"; exit 1 ;;
esac
