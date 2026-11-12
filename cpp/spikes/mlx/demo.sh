#!/bin/zsh
# Ask Llama 3.2 1B a question straight from C++ (MLX's C++ API; the template,
# the tokenizer, the model and the decode all C++, no Python), with the engine
# the C++ preview carries. One command (NOTES 6b614):
#
#   zsh cpp/spikes/mlx/demo.sh "Name three uses of a lighthouse."
#
# CONCORDE_PREVIEW_APP names the app (default /Applications/ConcordeAI C++ Preview.app).
APP="${CONCORDE_PREVIEW_APP:-/Applications/ConcordeAI C++ Preview.app}"
ENGINE="$APP/Contents/Resources/engine/concorde-llama"
[[ -x "$ENGINE" ]] || { echo "No C++ engine at $ENGINE (install the preview, or set CONCORDE_PREVIEW_APP)."; exit 1; }
HUB="${HF_HUB_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}/hub}"
[[ -n "${HF_HUB_CACHE:-}" ]] && HUB="$HF_HUB_CACHE"
MODEL=""
for d in "$HUB"/models--mlx-community--Llama-3.2-1B-Instruct-4bit/snapshots/*/; do
  [[ -f "$d/model.safetensors" ]] && MODEL="$d" && break
done
[[ -n "$MODEL" ]] || { echo "Llama 3.2 1B isn't on this Mac (download it in ConcordeAI's Models)."; exit 1; }
Q="${1:-Name three uses of a lighthouse, in one sentence each.}"
STATS="$(mktemp)"
echo "\n  Q: $Q\n"
"$ENGINE" "$MODEL" --text "$Q" 400 --json-stderr 2>"$STATS"
J="$(grep '{"impl"' "$STATS" | tail -1)"
rm -f "$STATS"
num() { print -r -- "$J" | sed -n "s/.*\"$1\": \([0-9.]*\).*/\1/p"; }
echo "\n  C++ · MLX · first token $(num ttft_ms) ms · $(num decode_tps) tokens/s · $(num gen_tokens) tokens · loaded in $(num load_ms) ms · peak $(num peak_mem_mb) MB\n"
