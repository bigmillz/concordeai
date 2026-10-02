# Your own model server for ConcordeAI

ConcordeAI can use models running on a Linux machine of yours, from
anywhere, as if they were on the device in front of you. This guide sets
up that server by hand.

It is for people comfortable with a Linux shell. The pieces are in
[`ollama1/`](../ollama1) in this repository:

- a gateway in front of [Ollama](https://ollama.com);
- a pairing tool;
- systemd units and a model updater.

> **Status.** `ollama1/setup.sh` installs everything in one go, but for one
> machine layout (Ubuntu 26.04, an OS disk, a models disk and two mirror
> disks, which it wipes). Your server's name, user, LAN, domain and disks are
> arguments to it (see [ollama1/README.md](../ollama1/README.md)). A general
> installer is coming, as the app's "Add a server". Until then, the manual
> steps below are the way for any other machine.
>
> **"ollama1" is the kit's name** (its folders, commands and unit files),
> not your server's. Name your server whatever you like (for example `ai`,
> `workshop` or `gpu-2`, and a second one `gpu-3`): the name is `server_name`
> in `config.json`, and the app shows the server by the name you give it
> when you add it.

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
| Memory | enough RAM for the models you mark `ram`, plus 8 GiB (or 12%) for the system |
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
     on `127.0.0.1` only, and the SSH tunnel is the way in. It answers only
     requests addressed to `127.0.0.1` or `localhost` without an `Origin`
     header, so a web page in your browser can't use the tunnel. It refuses
     to start if a Cloudflare tunnel is also configured (`tunnel_id`).
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

## Using it from the app

Once a server is paired, the engine menu has one row with its name, under
Cloud Only. It opens a menu beside it with `<name> Only` first and then
each of the server's models. `<name> Only` answers with one model: the
strongest one that fits entirely on the server's graphics card, judged by
the size in its tag (`20b` beats `14b`) and checked against the card's
memory when the server reports it. A model marked `ram` (card plus memory,
slower) is never picked for you; pick it by hand from the list. If no
model fits on the card whole, the mode uses the server's smallest model,
and its hover note says so. When you pull a bigger model that fits, the
mode follows it. Nothing runs on the computer or in the cloud, and if the
server is off or fails partway, the app says so rather than answering
from anywhere else. The item is greyed while the server doesn't answer,
and if you remove the server the mode goes back to Fast.

Under Advanced, the server's models are listed under its name and can be
ticked into a hand-picked council. They draft one after another on the
server. They can't be the compositor, which is always on the computer or
in the cloud.

Fast, Thinking and Pro, the Code lane's agents and Funnels (after the cloud,
when its box is ticked) use the server's models before this computer's: its best models that fit its card whole fill
the seats first, and this computer's own picks fill what is left. Pro seats
at most four of the server's models, since one card runs them one after
another. If the server doesn't start answering within about half a minute
(Fast) or a minute, this computer's copy answers instead. The switch under
Settings › Your servers, "Use for Fast, Thinking, Pro and the Code lane",
turns all of that off for a server. The Workspace and Coding agents can send
the contents of a folder you give them to the server, as they would to any
model that answers.

The sidebar's meters card shows the server too. When a server is paired and
names its graphics card, the card grows two rows under this computer's
chip and memory pressure: the card's name with a bar for how busy it
is, and "<server name> memory" with a bar for how much of the server's memory
is in use. They are polled every few seconds while the window is showing, and
at most two servers are shown. The server answers with five numbers only (busy
percent, video memory in use and in total, memory in use and in total), read at
most once a second; it says nothing about which models are loaded or who is
using the card. A server that doesn't answer shows its rows dimmed with empty
bars. A server running an older kit has no usage reading (or no memory
reading), so its row shows the name and an empty bar until you update the kit.

## Sleep when idle

Under Settings, Your servers, each server has "Sleep when idle" and a box for
the minutes with no questions before it sleeps (5 to 1440, 30 to start). It is
off until you turn it on. The server suspends itself to memory after that long
if, and only if, all of these hold: nothing is being answered, nobody is logged
in (an open SSH or terminal session keeps it awake), the graphics card is under
10% busy (a card it can't read blocks sleep; a machine with no card is not held
up by it), no download, update, setup or stability test is running (named
anywhere in a command, so `python3 -u ram_model_test.py` counts), nothing holds
a sleep inhibitor lock, and the 1-minute load average is 1.5 or less (so a
build or test left running after you log out keeps it up). Only questions count
as use: the app's sidebar meters and Settings never keep it awake. Whatever the
setting, the time since the server last started, last woke or the gateway last
restarted counts as use too, and anything it can't find out (the gateway's
activity, the boot time, the load) keeps it awake rather than being guessed.

Waking is a magic packet. `setup.sh` switches it on for every real network card
that supports it (a `/etc/systemd/network/50-wol-<card>.link` file, which also
carries the system's own naming and MAC rules from `99-default.link` because the
first matching `.link` wins, and `ethtool -s <card> wol g`) and prints one line
if it did. A wake-on-LAN file you made by hand for the same card is brought to
that shape in place. Your BIOS must allow it too:
"Wake on PCI-E" or "Resume by PCI-E device" (MSI: Settings > Advanced > Wake Up
Event Setup). When you ask a question that would use a sleeping server, the app
sends the packet, says "Woke <name>." in the chat once it answers (up to a
minute), and carries on with that server; if it doesn't answer, nothing changes
from today (its models are skipped, and an explicit pick says it isn't
answering). Waking works on the network the server is on: away from home the
packet goes nowhere. The app never wakes a server for the sidebar meters,
Settings or a benchmark, and sends at most one wake-up a minute per server.

The pieces: `ollama1-idle.service` (root, checks every 30 seconds, logs only
why it did or didn't sleep: `journalctl -u ollama1-idle`), `sudo ollama1-idle
check` (what it would do right now), the gateway's activity file
`/run/ollama1/stats/activity.json` and settings file
`/var/lib/ollama1-gateway/sleep.json` (numbers only; the service doesn't trust
either), and `/run/ollama1/idle.json` (the cards that can wake it). Deep sleep
must be what suspend uses: `cat /sys/power/mem_sleep` shows `[deep]`.

## Graphics card tuning

Off unless you ask. With an AMD Navi 21 card (RX 6800, 6800 XT, 6900 XT,
6950 XT), `sudo ./setup.sh --gpu-tune` turns on a small tune (setup saves the
choice, so running it again without the flag keeps it): the card's highest power limit (the most the driver allows it)
and its memory clock +100 in the driver's units (GDDR6 runs at twice that),
never past the range the card reports. Core clocks and voltages are left
alone. The aim is about 5% faster token generation on models that fit the
card; only a measurement on your card proves it, and `sudo ollama1-gpu-tune
status` shows the stock and tuned speeds it measured. Expect more power drawn,
more heat and more fan noise under load. The memory clock needs the kernel's
overdrive switch (only that bit is added to `amdgpu.ppfeaturemask`), so it
starts after a reboot. A 60-second check under load follows; an amdgpu error
in the kernel log, the junction at 105 C, the memory at 100 C or slower answers
put the card back to stock, where it stays until `sudo ollama1-gpu-tune on`.
`sudo ollama1-gpu-tune off` puts it back to stock yourself; `setup.sh
--no-gpu-tune` is the explicit off. Any other card is left alone. The
details are in the kit's README, "Graphics card tuning".

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

- A `ram` model is counted at its full resident size (weights, context
  cache, 2.5 GiB of compute buffers) against the free RAM, less the larger
  of 8 GiB and 12% of RAM. VRAM counts too only if you turn llama.cpp's
  weight repacking off: `Environment=LLAMA_ARG_REPACK=false` in
  ollama.service and `"ollama_no_repack": true` in config.json. With
  repacking on, llama.cpp can copy the whole CPU part of the model into a
  new buffer. gpt-oss:120b does not fit a 64 GB machine that way; treat it
  as experimental.
- `ram` models are loaded with mmap off (`use_mmap: false`). Give Ollama a
  hard memory cap so a load that doesn't fit can't take the machine down.
  Add `/etc/systemd/system/ollama.service.d/10-memory.conf` with
  `[Service]`, then `MemoryMax=` your RAM less 8 GiB, `MemoryHigh=` 2 GiB
  below that, and `MemorySwapMax=0`. Put the same `MemoryMax` in bytes in
  `config.json` as `"ollama_memory_max_bytes"`.
- Try a big one once with
  `sudo bash ollama1/tools/ram-model-test.sh --model <name> --ctx 4096` before
  relying on it. Under a memory cap and a watchdog it loads the model with
  repacking off, then with the MoE experts placed by hand. With encrypted
  swap it also tries swap. It reports the numbers for each.
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

## Measuring it

Settings › Usage › Benchmark can run its fixed test on a server's models:
pick the server in the list, tick the models and press Run. It loads each
ticked model on the server's card and stores nothing there. Models that fit
the card whole start ticked; `gpu+ram` ones start unticked, because they load
the server's CPU hard. Writes, reads and load are Ollama's own figures, which
the gateway passes on as they are. First token is timed on the computer, so it
includes the network. The gateway doesn't let a client unload a model, so a
model that was already loaded has no load time to measure and the row says so.
The results are kept per profile and can be compared with this computer's and
with cloud models'.

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
     "server_name": "ai",
     "cf_zone": "example.com",
     "hostname_gateway": "ai.example.com",
     "hostname_admin": "ai-admin.example.com",
     "owner_label": "Sam",
     "admin_email": "you@users.noreply.github.com"
   }
   ```

   `server_name` is yours to choose: lowercase letters, digits and hyphens,
   1 to 32 characters, starting with a letter. The two hostnames default to
   `<server_name>.<cf_zone>` and `<server_name>-admin.<cf_zone>`, and the
   Cloudflare names follow it: the tunnel `<server_name>`, the service token
   `<server_name>-app`, the policy `<server_name> admin - <owner_label> only`
   (set `tunnel_name`, `token_name`, `policy_admin_name` to use other names).

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
| `ram_oom` | the load ran out of Ollama's memory cap and was stopped (the machine is fine); use a smaller context or model |
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
