#!/usr/bin/env bash
# ram-model-test.sh: measure how a big 'ram' model loads on this server,
# in several configurations, each under a hard memory cap on Ollama.
#
#   sudo bash ram-model-test.sh                  gpt-oss:120b, num_ctx 4096,
#                                                configurations norepack, norepack-moe
#   sudo bash ram-model-test.sh --configs swap   only with the encrypted swap on
#   sudo bash ram-model-test.sh --ncmoe 30 --configs norepack-moe
#   sudo bash ram-model-test.sh --help
#
# Each configuration: a runtime drop-in for ollama.service (MemoryMax = RAM
# less 8 GiB, MemoryHigh 2 GiB below, the configuration's LLAMA_ARG_*
# settings), an Ollama restart, a load, a timed generation of a fixed
# counting prompt, a 1 s memory log with a watchdog (kills Ollama if the
# machine gets under 1.5 GiB free), then an unload. At the end the drop-in
# is removed and Ollama restarts with its normal settings. The details are
# in ram_model_test.py next to this script.
#
# Worst case, if the cap works: Ollama is restarted. Output also goes to
# /var/log/ollama1-ram-test.log; results to /var/log/ollama1-ram-test.json.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/var/log/ollama1-ram-test.log
# The arguments, quoted to be pasted into the command tmux runs. With none,
# printf '%q ' would still print '' (an empty argument), which the Python
# script then refuses ("unrecognized arguments:"), so it's only used when
# there are some.
quoted_args() {
  if [ "$#" -gt 0 ]; then printf ' %q' "$@"; fi
}
case "${1:-}" in -h|--help) exec python3 "$HERE/ram_model_test.py" --help ;; esac
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo."; exit 1; }
{ command -v python3 && command -v systemctl; } >/dev/null || { echo "needs python3 and systemd"; exit 1; }
# In tmux, so a dropped SSH connection can't stop it halfway (reattach:
# sudo tmux attach -t ollama1-ramtest). Without tmux it still cleans up on a
# hangup, but a terminal of its own is the way to run it.
if [ -z "${TMUX:-}" ] && [ "${O1_NO_TMUX:-}" != 1 ] && [ -t 0 ] && command -v tmux >/dev/null; then
  echo "Starting in tmux (session ollama1-ramtest). If the connection drops: sudo tmux attach -t ollama1-ramtest"
  exec tmux new-session -A -s ollama1-ramtest \
    "O1_NO_TMUX=1 bash '$HERE/ram-model-test.sh'$(quoted_args "$@"); echo; read -r -p 'Finished. Press Enter to close. ' _"
fi
[ -n "${TMUX:-}" ] || [ "${O1_NO_TMUX:-}" = 1 ] || echo "Note: tmux isn't available; if this SSH session drops, the test stops (and cleans up)."
touch "$LOG"; chmod 600 "$LOG"
python3 -u "$HERE/ram_model_test.py" "$@" 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
