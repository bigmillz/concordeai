#!/usr/bin/env bash
# install-comfyui.sh: optional image and video generation for an ollama1 server (6b356).
#
# setup.sh does NOT run this: ComfyUI is 20 GB or more of downloads and a Python
# environment, so it is something you ask for. It is the same steps as doing it by
# hand, in order, each one skipped when it is already done, so it is safe to run again:
#
#   1. uv (the Python tool) for the admin user, and Python 3.12 through it
#   2. ComfyUI itself, cloned into ~/comfyui
#   3. a virtual environment with PyTorch built for ROCm 6.4 (AMD graphics cards),
#      then ComfyUI's own requirements
#   4. the four model files the gateway's templates use, downloaded with curl -C -
#      so a cut-off download carries on where it stopped:
#        checkpoints/flux1-schnell-fp8.safetensors             images (FLUX.1 schnell, fp8)    about 17 GB*
#        diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors     video (Wan 2.1, 1.3B)           about 2.8 GB*
#        text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors  video's text encoder             about 6.7 GB*
#        vae/wan_2.1_vae.safetensors                           video's decoder                 about 0.25 GB*
#   5. comfyui.service, which runs ComfyUI as the admin user on 127.0.0.1:8188 only,
#      and comfyui-clean.timer, which removes finished pictures and clips from memory
#      (they are written to /run, which is RAM) a few minutes after they were made
#   * estimates, from the files' listings: check them against what the download says.
#
# Nothing here opens a port: ComfyUI listens on the loopback address and the port guard
# (setup.sh; config/ollama1.nft.in) lets only root and the gateway's user connect to it.
# The gateway is the only thing that talks to it, and builds every job itself.
#
#   sudo bash install-comfyui.sh                  as the user who ran sudo
#   sudo bash install-comfyui.sh --user NAME      as another user (the one that owns ~/comfyui)
#   sudo bash install-comfyui.sh --skip-models    everything but the downloads
#   sudo bash install-comfyui.sh --update         also pull ComfyUI's newest code
#   sudo bash install-comfyui.sh --no-start       install, don't start the service
#   bash install-comfyui.sh --print-unit          show the service files and stop (no root)
#
# Settings (environment): COMFYUI_REF (the git branch or tag to clone, default master),
# TORCH_SPEC (default torch==2.9.1, the version known to work with ROCm 6.4),
# TORCH_INDEX (default https://download.pytorch.org/whl/rocm6.4).
# Extra ComfyUI options, such as --lowvram, go in /etc/default/comfyui as
# COMFYUI_EXTRA_ARGS="..." (read by the service; restart it after a change).
set -euo pipefail

COMFYUI_REPO=https://github.com/comfyanonymous/ComfyUI.git
COMFYUI_REF=${COMFYUI_REF:-master}
TORCH_SPEC=${TORCH_SPEC:-torch==2.9.1}
TORCH_INDEX=${TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.4}
PORT=8188
HF=https://huggingface.co/Comfy-Org
# folder|file|url|smallest size (bytes) the finished file can have
MODELS=(
  "checkpoints|flux1-schnell-fp8.safetensors|$HF/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors|16000000000"
  "diffusion_models|wan2.1_t2v_1.3B_fp16.safetensors|$HF/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors|2500000000"
  "text_encoders|umt5_xxl_fp8_e4m3fn_scaled.safetensors|$HF/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors|6000000000"
  "vae|wan_2.1_vae.safetensors|$HF/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors|200000000"
)

USER_NAME=${COMFYUI_USER:-${SUDO_USER:-}}
SKIP_MODELS=0
UPDATE=0
START=1
PRINT_UNIT=0

usage() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; }
say() { printf '%s\n' "$*"; }
die() { printf 'STOPPED: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --user) [ "$#" -ge 2 ] || die "--user takes a login name"; USER_NAME=$2; shift ;;
    --skip-models) SKIP_MODELS=1 ;;
    --update) UPDATE=1 ;;
    --no-start) START=0 ;;
    --print-unit) PRINT_UNIT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# The login must be a plain one: it is put into service files and run through sudo-like tools.
if [ -n "$USER_NAME" ] && ! [[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  die "'$USER_NAME' doesn't look like a login name"
fi
if [ "$PRINT_UNIT" = 1 ] && [ -z "$USER_NAME" ]; then USER_NAME=admin; fi
[ -n "$USER_NAME" ] || die "run it with sudo from your own login, or name the user: --user NAME"
[ "$USER_NAME" != root ] || die "ComfyUI is not run as root: use the admin user's login (--user NAME)"

if [ "$PRINT_UNIT" = 1 ]; then
  HOME_DIR=/home/$USER_NAME
else
  HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
  [ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] || die "no home folder for $USER_NAME"
fi
DIR=$HOME_DIR/comfyui
VENV=$DIR/.venv

# ---- the service files ---------------------------------------------------------------
# Output and temp go to /run (RAM, gone at a reboot and with the service): a finished
# picture is never kept on a disk, and the timer sweeps what is left every 10 minutes.
unit_text() {
  cat <<EOF
# ---- /etc/systemd/system/comfyui.service
[Unit]
Description=ComfyUI (image and video generation for ollama1, this machine only)
After=network.target

[Service]
User=$USER_NAME
SupplementaryGroups=render video
WorkingDirectory=$DIR
RuntimeDirectory=comfyui
RuntimeDirectoryMode=0700
EnvironmentFile=-/etc/default/comfyui
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/mkdir -p /run/comfyui/out /run/comfyui/tmp
ExecStart=$VENV/bin/python main.py --listen 127.0.0.1 --port $PORT --disable-metadata --output-directory /run/comfyui/out --temp-directory /run/comfyui/tmp \$COMFYUI_EXTRA_ARGS
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target

# ---- /etc/systemd/system/comfyui-clean.service
[Unit]
Description=Remove old ComfyUI results

[Service]
Type=oneshot
User=$USER_NAME
ExecStart=-/usr/bin/find /run/comfyui/out /run/comfyui/tmp -type f -mmin +15 -delete

# ---- /etc/systemd/system/comfyui-clean.timer
[Unit]
Description=Remove old ComfyUI results every 10 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF
}

if [ "$PRINT_UNIT" = 1 ]; then unit_text; exit 0; fi

# ---- checks ----------------------------------------------------------------------------
[ "$(id -u)" = 0 ] || die "run it with sudo"
command -v systemctl >/dev/null || die "this needs systemd"
[ -e /dev/kfd ] || say "-- /dev/kfd isn't there: ROCm needs the AMD driver (amdgpu). Carrying on, but ComfyUI will run on the CPU until it is."

as_user() { runuser -u "$USER_NAME" -- env HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

step() { printf '\n== %s\n' "$*"; }

step "Packages (git, curl)"
need=()
for c in git curl; do command -v "$c" >/dev/null || need+=("$c"); done
if [ "${#need[@]}" -gt 0 ]; then
  apt-get install -y -q "${need[@]}" ca-certificates
else
  say "   already installed"
fi

step "uv (Python 3.12 comes through it)"
if as_user sh -c 'command -v uv' >/dev/null 2>&1; then
  say "   $(as_user uv --version)"
else
  as_user sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

step "ComfyUI ($DIR)"
if [ -d "$DIR/.git" ]; then
  if [ "$UPDATE" = 1 ]; then as_user git -C "$DIR" pull --ff-only; else say "   already cloned (--update pulls the newest)"; fi
else
  [ ! -e "$DIR" ] || die "$DIR exists and is not a ComfyUI checkout; move it away"
  as_user git clone --branch "$COMFYUI_REF" "$COMFYUI_REPO" "$DIR"
fi

step "Python environment ($VENV)"
if [ -x "$VENV/bin/python" ]; then
  say "   already made"
else
  as_user uv venv --python 3.12 "$VENV"
fi

step "PyTorch for ROCm ($TORCH_SPEC)"
if as_user "$VENV/bin/python" -c 'import sys, torch; sys.exit(0 if (torch.version.hip or torch.version.cuda) else 1)' 2>/dev/null; then
  say "   $(as_user "$VENV/bin/python" -c 'import torch; print("torch", torch.__version__)')"
else
  as_user uv pip install --python "$VENV/bin/python" "$TORCH_SPEC" torchvision torchaudio --index-url "$TORCH_INDEX" \
    || as_user uv pip install --python "$VENV/bin/python" "$TORCH_SPEC" torchvision torchaudio \
         --index-url "$TORCH_INDEX" --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match
fi

step "ComfyUI's requirements"
as_user uv pip install --python "$VENV/bin/python" -r "$DIR/requirements.txt"

step "Model files"
fetch_model() { # folder file url min_bytes
  local dest=$DIR/models/$1/$2 size
  as_user mkdir -p "$DIR/models/$1"
  size=$(stat -c %s "$dest" 2>/dev/null || echo 0)
  if [ "$size" -ge "$4" ]; then
    say "   $2: already there"
    return 0
  fi
  [ "$SKIP_MODELS" = 0 ] || { say "   $2: skipped (--skip-models)"; return 0; }
  say "   $2: downloading (a cut-off download carries on when you run this again)"
  as_user curl -fL -C - --retry 5 --retry-delay 5 -o "$dest.part" "$3"
  size=$(stat -c %s "$dest.part" 2>/dev/null || echo 0)
  [ "$size" -ge "$4" ] || die "$2 came to only $size bytes: the download is incomplete or the address changed"
  as_user mv "$dest.part" "$dest"
}
for m in "${MODELS[@]}"; do
  IFS='|' read -r folder file url min <<<"$m"
  fetch_model "$folder" "$file" "$url" "$min"
done

step "The service"
write_units() {
  local name
  unit_text | awk '/^# ---- /{if (f) close(f); f = $3 ".new"; next} f {print > f}'
  for name in comfyui.service comfyui-clean.service comfyui-clean.timer; do
    if [ -f "/etc/systemd/system/$name" ] && ! cmp -s "/etc/systemd/system/$name" "/etc/systemd/system/$name.new"; then
      cp -p "/etc/systemd/system/$name" "/etc/systemd/system/$name.bak"
      say "   $name changed; the old one is kept as $name.bak"
    fi
    chmod 0644 "/etc/systemd/system/$name.new"
    mv "/etc/systemd/system/$name.new" "/etc/systemd/system/$name"
  done
}
write_units
systemctl daemon-reload
if [ "$START" = 1 ]; then
  systemctl enable comfyui.service comfyui-clean.timer >/dev/null
  systemctl restart comfyui.service
  systemctl start comfyui-clean.timer
  say "   waiting for ComfyUI to answer on 127.0.0.1:$PORT (the first start can take a minute)"
  ok=0
  for _ in $(seq 1 90); do
    if curl -fsS -m 3 "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" = 1 ]; then
    say "   ComfyUI is running (loopback only)"
  else
    die "ComfyUI did not answer in three minutes: journalctl -u comfyui -e"
  fi
else
  say "   installed, not started (--no-start). Start it with: sudo systemctl enable --now comfyui.service comfyui-clean.timer"
fi

cat <<EOF

Done. What to do next:
  1. On the server, run  sudo ./setup.sh  from the kit once (it installs the gateway that
     builds generation jobs, and adds ComfyUI's port to the local port guard).
  2. Check what the gateway sees:  sudo -u o1gw curl -s http://127.0.0.1:$PORT/system_stats | head -c 80
  3. In the app: Settings > Your servers > Test. The card shows Images and Video when the
     server can make them.
EOF
