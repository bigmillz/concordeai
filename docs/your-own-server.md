# Your own model server for ConcordeAI

ConcordeAI can use models running on a Linux machine of yours, from
anywhere, as if they were on the device in front of you. This guide sets
up that server by hand.

It is for people comfortable with a Linux shell. The pieces are in
[`ollama1/`](../ollama1) in this repository:

- a gateway in front of [Ollama](https://ollama.com);
- a pairing tool;
- systemd units and a model updater.

> **Status.** `ollama1/setup.sh` is written for one particular machine
> (fixed disks, one domain) and is not meant to be run elsewhere. A general
> installer is coming, as the app's "Add a server". Until then, the manual
> steps below are the way.

## What you get

- **Only your devices.** Every request is signed by a key that was paired
  at the server's own terminal. There are no accounts, invites or sharing:
  a device is either paired at the machine or it can't get in.
- **Nothing mixes, nothing is kept.**
  - Each request stands alone.
  - The gateway writes no prompt or answer to disk or to its logs; logs
    hold counts, timings, model names and device names.
  - When requests switch from one paired device to another, loaded models
    are unloaded first, so not even Ollama's prompt cache is shared.
- **GPU first.** A model runs entirely in VRAM, or it is refused. The
  exception is a model you mark `ram` (see [Models](#choosing-models)).
- **No open ports** in the default setup: the server is reached through SSH.

## Requirements

| | |
|---|---|
| OS | Ubuntu 24.04 or newer, or Debian 12 or newer. x86_64, systemd |
| Python | 3.11 or newer, with `python3-nacl` |
| GPU | NVIDIA with its driver installed (`nvidia-smi` works); or AMD with a card ROCm supports (for example RX 6800/6900, RX 7000, RX 9000 series); or none (see below) |
| Memory | enough RAM for the models you mark `ram`, plus 6 GB for the system |
| Disk | room for the models: 5–20 GB each for most, 60+ GB for the largest |
| Access | an account with sudo, and SSH to the machine |

Without a GPU, only models marked `ram` will run, on the CPU, and they
will be slow. A GPU with at least 8 GB of VRAM is the practical minimum.

## Three ways to reach it

| | How | Needs | Good for |
|---|---|---|---|
| **SSH tunnel** (default) | the app forwards a local port over SSH to the gateway on the server's `127.0.0.1:8431` | SSH access; no domain | most people |
| **Cloudflare Tunnel** | `https://ai.example.com` through Cloudflare, protected by Cloudflare Access | a domain on Cloudflare, one API token | reaching it without SSH, from anywhere |
| **Home network** | plain HTTP on the server's LAN address | nothing | a machine on the same network; unencrypted |

Every mode needs the same device signatures. Pairing is always done over
the SSH tunnel or Cloudflare, never over the plain home-network listener.

Until the app's "Add a server" opens the SSH tunnel for you, open it by
hand on your Mac and point the app at `http://127.0.0.1:8431`:

```bash
ssh -N -L 8431:127.0.0.1:8431 you@your-server
```

## Install (SSH tunnel mode)

All commands run on the server.

1. **Packages.**

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-nacl zstd curl nftables openssh-server
   ```

2. **The kit.**

   ```bash
   git clone https://github.com/bigmillz/concordeai.git ~/concordeai
   sudo install -d /usr/local/lib/ollama1/bin /usr/local/lib/ollama1/lib
   sudo install -m 0644 ~/concordeai/ollama1/lib/*.py /usr/local/lib/ollama1/lib/
   sudo install -m 0755 ~/concordeai/ollama1/bin/* /usr/local/lib/ollama1/bin/
   sudo ln -sfn /usr/local/lib/ollama1/bin/ollama1-pair /usr/local/sbin/ollama1-pair
   sudo ln -sfn /usr/local/lib/ollama1/bin/ollama1-dash /usr/local/bin/ollama1-top
   sudo ln -sfn /opt/ollama/current/bin/ollama /usr/local/bin/ollama
   ```

3. **Users and groups.** The services run as users with no shell.
   `o1view` may read the counts-only statistics; `o1pair` may read the
   pairing code (the gateway must never be in it).

   ```bash
   sudo groupadd --system o1view
   sudo groupadd --system o1pair
   sudo groupadd --system o1admin     # owns the admin panel's terminal folder, even if unused
   for u in ollama o1gw; do
     sudo useradd --system --user-group --home-dir /var/lib/$u --no-create-home --shell /usr/sbin/nologin $u
   done
   sudo usermod -aG render,video ollama
   sudo usermod -aG o1view o1gw
   sudo usermod -aG o1view "$USER"
   sudo install -d -m 0750 -o ollama -g ollama /var/lib/ollama
   ```

4. **Folders and settings.**

   ```bash
   sudo install -m 0644 ~/concordeai/ollama1/config/ollama1.tmpfiles /etc/tmpfiles.d/ollama1.conf
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/ollama1.conf
   sudo install -d -m 0755 /etc/ollama1 /var/lib/ollama1 /opt/ollama
   sudo install -d -m 0750 -o ollama -g ollama /srv/models       # or any disk with room
   echo '{"version": 1, "devices": []}' | sudo tee /etc/ollama1/devices.json >/dev/null
   sudo chown root:o1view /etc/ollama1/devices.json && sudo chmod 0640 /etc/ollama1/devices.json
   sudo install -m 0644 ~/concordeai/ollama1/config/models.allow /etc/ollama1/models.allow
   ```

   Create `/etc/ollama1/config.json` (root-only, `sudo chmod 0600`):

   ```json
   {
     "access": "none",
     "ollama_rocm": false,
     "num_ctx_default": 8192
   }
   ```

   - `"access": "none"`: no Cloudflare in front. The gateway still listens
     on `127.0.0.1` only, and the SSH tunnel is the way in.
   - `"ollama_rocm"`: `true` on an AMD GPU, `false` on NVIDIA or without a
     GPU.
   - If VRAM isn't detected automatically (the dashboard shows "GPU not
     found"), add `"vram_total_bytes"`, for example `25769803776` for 24 GB.

5. **Ollama.** It is downloaded from Ollama's GitHub release. Each file must
   match the release's published SHA-256 sums, or nothing is installed.

   ```bash
   sudo /usr/local/lib/ollama1/bin/ollama1-update-ollama --no-restart
   ```

   On AMD, the ROCm component is included because `ollama_rocm` is `true`.

6. **Units.**

   ```bash
   cd ~/concordeai/ollama1/systemd
   sudo install -m 0644 ollama.service ollama1-gateway.service ollama1-nft.service \
     ollama1-pair-commit.path ollama1-pair-commit.service ollama1-pair-window.service \
     ollama1-update-ollama.service ollama1-update-ollama.timer /etc/systemd/system/
   ```

   If your models live somewhere other than `/srv/models`, change
   `OLLAMA_MODELS`, `RequiresMountsFor` and `ReadWritePaths` in
   `/etc/systemd/system/ollama.service`.

7. **The local port guard.** It lets only the named users connect to Ollama
   and the gateway, even from the same machine. The SSH tunnel arrives as
   your own user, so your user id takes the gateway's slot:

   ```bash
   sed -e "s/@UID_OLLAMA@/$(id -u ollama)/g" -e "s/@UID_O1GW@/$(id -u o1gw)/g" \
       -e "s/@UID_O1ADMIN@/$(id -u o1gw)/g" -e "s/@UID_CLOUDFLARED@/$(id -u)/g" \
       ~/concordeai/ollama1/config/ollama1.nft.in | sudo tee /etc/ollama1/ollama1.nft >/dev/null
   sudo nft -c -f /etc/ollama1/ollama1.nft && echo rules ok
   ```

8. **Start it.**

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now ollama1-nft ollama ollama1-gateway ollama1-pair-commit.path ollama1-update-ollama.timer
   curl -s http://127.0.0.1:8431/v1/whoami     # {"error": "request is not signed", ...}: working
   journalctl -u ollama -b | grep -i "inference compute"   # should name your GPU
   ```

9. **Firewall.** SSH is the only thing that needs to be reachable:

   ```bash
   sudo ufw allow OpenSSH && sudo ufw default deny incoming && sudo ufw --force enable
   ```

## Pairing

1. At the server, run `sudo ollama1-pair`. It shows a 12-character code,
   like `7K4M-2QXD-9FHT`, for 5 minutes.
2. In ConcordeAI, add the server and type the code.

One device pairs per window, and 5 wrong codes close the window. To see or
remove paired devices, use `sudo ollama1-pair --list` and
`sudo ollama1-pair --remove ID`. The code is never sent anywhere: the app
proves it knows it, and the server proves it back. The details are in
[PROTOCOL.md](../ollama1/PROTOCOL.md).

## Choosing models

Nothing is installed at first. Put the models you want in
`/etc/ollama1/models.allow`, one per line, then `sudo ollama pull <name>`.

What fits, roughly, with 4-bit quantisation and an 8k context:

| VRAM | Dense models that run fully on the GPU |
|---|---|
| 8 GB | up to 7–8B |
| 12 GB | up to 12–14B |
| 16 GB | up to 14B comfortably, ~20B with a short context |
| 24 GB | up to 27–32B |
| 48 GB | up to 70B |

The gateway does the arithmetic before loading. Weights, the context cache
for the requested `num_ctx` and a margin must fit in VRAM, or the answer is
`gpu_fit` with the numbers. After loading, Ollama must report 100% in VRAM,
or the model is unloaded and refused (`gpu_spill`). A longer context costs
VRAM: halve `num_ctx` if a model just misses.

### Models that may use system memory: `ram`

Mixture-of-experts (MoE) models only work part of their weights for each
token, so they stay usable with part of them in system RAM. Mark such a
model `ram` and it may spill:

```
qwen3:14b
gpt-oss:120b    ram
```

- A `ram` model may use VRAM plus the RAM that is free, less 6 GB kept for
  the system. gpt-oss:120b, about 61 GB, wants a 16 GB GPU and 64 GB of RAM.
- It is refused if loading it would push the machine into swap. Ollama is
  not allowed swap at all.
- It runs alone: other models are unloaded first.
- Without a `num_ctx` from the app, its context steps down from 8192 to
  4096, then 2048, to fit.
- Expect it to be several times slower than a model that fits in VRAM. The
  app labels it `gpu+ram`.
- Dense models gain little from this; pick a smaller one instead.
- A line with any other word after the name is ignored: `ram` is the only
  flag.

## The other two ways in

### Cloudflare Tunnel

You need a domain on Cloudflare and, once, a Cloudflare Zero Trust
organisation: dash.cloudflare.com > Zero Trust > pick a team name > Free
plan.

1. `sudo apt install cloudflared` (from
   [Cloudflare's repository](https://pkg.cloudflare.com)), then
   `sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin cloudflared`.
2. In `/etc/ollama1/config.json`, set `"access": "cloudflare"` and your
   names:

   ```json
   {
     "access": "cloudflare",
     "cf_zone": "example.com",
     "hostname_gateway": "ai.example.com",
     "hostname_admin": "ai-admin.example.com",
     "admin_email": "you@users.noreply.github.com"
   }
   ```

   `admin_email` is the address allowed into the admin panel. Keep it on
   the server; it never needs to be anywhere else.
3. Make a Cloudflare API token (My Profile > API Tokens > Create Custom
   Token), with a TTL of one day:

   | Scope | Permission | Level |
   |---|---|---|
   | Account | Cloudflare Tunnel | Edit |
   | Account | Access: Apps and Policies | Edit |
   | Account | Access: Service Tokens | Edit |
   | Account | Access: Organizations, Identity Providers, and Groups | Read |
   | Zone (your zone only) | DNS | Edit |
   | Zone (your zone only) | Zone | Read |

4. Run `sudo /usr/local/lib/ollama1/bin/ollama1-cf-access` and paste the
   token when asked. It is read without echo, never stored, and should be
   deleted afterwards. The helper:
   - creates the tunnel, its DNS records, the Access applications and a
     non-expiring service token;
   - shows the token's Client ID and Secret once, for the app;
   - writes the rest to `config.json`.
5. Write `/etc/ollama1/cloudflared.yml` from
   `ollama1/config/cloudflared.yml.in`. The helper saved the values to fill
   in to `config.json`:
   - `@TUNNEL_ID@` is `tunnel_id`;
   - `@TEAM_NAME@` is the first part of `access_team_domain`;
   - `@GATEWAY_AUD@` and `@ADMIN_AUD@` are `gateway_aud` and `admin_aud`;
   - `@GW_HOST@` and `@ADMIN_HOST@` are your two hostnames.
6. Install and start `ollama1-tunnel.service`, and add cloudflared's user
   id to the port guard in place of your own.
7. Restart the gateway.

The admin panel (`ollama1-admin.service`) only works in this mode: it
relies on Cloudflare Access to know it's you.

### Home network

Set `"lan_mode": true`, `"lan_bind": "<the server's LAN address>"` and
`"lan_cidr": "<your LAN, e.g. 192.168.1.0/24>"` in `config.json`. Allow
port 8431 from the LAN in the firewall and restart the gateway. The
requests travel unencrypted on your network. Pair over the SSH tunnel
first: pairing is refused on this listener.

## Updating

- **Ollama.** `ollama1-update-ollama.timer` checks weekly. It verifies each
  download against the release's checksums, switches, checks the new
  version starts, and switches back if not. To run it now:
  `sudo systemctl start ollama1-update-ollama`.
- **The kit.** `cd ~/concordeai && git pull`, repeat steps 2 and 6, then
  `sudo systemctl restart ollama1-gateway`.
- **The system.** Turn on unattended-upgrades
  (`sudo apt install unattended-upgrades`) for security updates.

## Troubleshooting

| Symptom | Look at |
|---|---|
| The app says the server refused the request | `journalctl -u ollama1-gateway`: one line per request, with the reason (`unpaired`, `clock_skew`, `bad_signature`...) and never the content |
| `clock_skew` | the server's and the device's clocks: `timedatectl` should say "synchronized: yes" |
| `gpu_fit` | the model is too big for the VRAM at that context; the error carries the numbers |
| `gpu_spill` | Ollama put part of a GPU-only model on the CPU; use a smaller model or context, or mark an MoE model `ram` |
| `ram_pressure` | not enough free RAM for that `ram` model; close other programs or pick a smaller one |
| Every model refused | Ollama didn't find the GPU: `journalctl -u ollama \| grep -i -E "inference compute\|cuda\|rocm"` |
| `ollama list` says connection refused | expected without sudo: the port guard lets only root and the services reach Ollama |
| Pairing says no window | `sudo ollama1-pair` must be running at the server (5 minutes) |
| The whole picture | `ollama1-top` |

## Security, in short

- **Keys.** A device's private key stays on the device (in the macOS
  Keychain). The server keeps only public keys, in
  `/etc/ollama1/devices.json`, written by root and only after pairing at
  the server.
- **Requests.**
  - Every request is signed over its method, path, body, time and a
    one-time nonce.
  - A copy replayed later, or sent 60 seconds late, is refused.
  - Nothing is read before the signature headers check out.
  - Only chat, generate, embeddings and the model lists are reachable.
    Models can't be pulled, deleted or created from outside.
- **The services** run as users without a shell, with systemd sandboxing,
  no core dumps and no swap for the gateway and Ollama.
- **Model placement.** No model uses system memory unless you mark it
  `ram`.
- **Reporting.** If something here looks wrong, open an issue in this
  repository.
