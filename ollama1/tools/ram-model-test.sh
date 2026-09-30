#!/usr/bin/env bash
# ram-model-test.sh: measure how a big 'ram' model loads on this desktop,
# in several configurations, each under a hard memory cap on Ollama.
#
#   sudo bash ram-model-test.sh                  gpt-oss:120b, num_ctx 4096,
#                                                configurations norepack, norepack-moe, swap
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
case "${1:-}" in -h|--help) exec python3 "$HERE/ram_model_test.py" --help ;; esac
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo."; exit 1; }
{ command -v python3 && command -v systemctl; } >/dev/null || { echo "needs python3 and systemd"; exit 1; }
touch "$LOG"; chmod 600 "$LOG"
python3 -u "$HERE/ram_model_test.py" "$@" 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
