# ollama1: a private model server on the desktop

This kit turns the spare Linux desktop into **ollama1**, a model server that
only ConcordeAI on Patrick's own devices can use. It runs Ollama on the
Radeon RX 6900 XT and nothing else.

- It is reachable from anywhere through a Cloudflare Tunnel. No port is open
  at home, and the home IP stays hidden.
  - `ollama1.flyconcordefly.com` is the gateway the app talks to.
  - `ollama1-admin.flyconcordefly.com` is the admin panel.
- Every request needs **both** of these:
  - a Cloudflare Access service token;
  - a signature from a device that was paired **at the desktop**.

  There is no invite, share or join. Nobody else can use it, and requests
  never mix.
- It keeps nothing. No prompt or answer is written to disk or to a log, and
  there is no chat history. Logs hold counts, timings, model names and
  device names.
- It runs on the GPU only. A model that can't fit entirely in the 16 GB of
  VRAM is refused. If one lands partly on the CPU anyway, it is unloaded and
  the request gets an error.
- It is locked down:
  - The firewall only lets in SSH, and only from the home LAN.
  - SSH accepts keys only (your Mac's), with no root login.
  - Services run as users with no shell.
  - Security updates install automatically, with a reboot at 04:00 when an
    update needs one.
  - Ollama is updated weekly, from verified downloads.
- It has a text dashboard on the desktop's monitor, and a web admin panel
  with a terminal that still asks for your Linux password.

The request-signing and pairing rules the app follows are in
[PROTOCOL.md](PROTOCOL.md).

## What goes where

| | |
|---|---|
| Host name | `ollama1` (was `concordeai`), time zone America/New_York |
| Address | 192.168.86.10 on `br0`. The bridge over both wired ports keeps the Raspberry Pi on the LAN. The kit never changes network settings |
| OS disk (nvme, S6B0NU0W805372Z) | Kept. The root volume grows into the free 1.7 TB, and `/home` moves onto it |
| Models disk (nvme, S6B0NG0R906564E) | **Wiped**, ext4, `/srv/models`. This is Ollama's model folder |
| 8 TB disks (ZR127RMQ, ZR11ZRGJ) | **Wiped**, RAID1 mirror, ext4, `/srv/data`. Holds backups and bulk storage, including a nightly copy of the settings and the model list |
| Ollama | 127.0.0.1:11434 only, ROCm build, in `/opt/ollama` |
| Gateway | 127.0.0.1:8431, user `o1gw` |
| Admin panel | 127.0.0.1:8432, user `o1admin` |
| Web terminal (ttyd + `login`) | 127.0.0.1:8433, reached only through the panel |
| Dashboard | tty1 (the monitor), user `o1dash`. Also `ollama1-top` over SSH |
| Settings | `/etc/ollama1/`: `config.json` (root-only), `devices.json`, `models.allow` |
| Setup log | `/var/log/ollama1-setup.log` |

## Before you start

1. **Put your Mac's SSH key on the desktop.** Setup turns password login
   off, and after that only a key can log in. On the Mac:

   ```bash
   ssh-copy-id pmiller@192.168.86.10
   ssh pmiller@192.168.86.10        # should log in without asking for the password
   ```

   Setup refuses to turn passwords off until `~/.ssh/authorized_keys` holds
   a key other than `claude-setup@concordeai` (the key used to build this
   kit). Without one it keeps passwords on and tells you what to do.
2. **Cloudflare Zero Trust.** Open dash.cloudflare.com > **Zero Trust** once.
   If it asks, pick a team name (for example `concorde`) and the **Free**
   plan. Cloudflare may ask for a card for the $0 plan.
3. **Keep your Mac's browser handy.** Cloudflare asks you to approve the
   tunnel once.

## Run it

At the desktop or over SSH:

```bash
git clone https://github.com/bigmillz/concordeai.git ~/concordeai
cd /                                          # not inside /home: /home is about to move
bash ~/concordeai/ollama1/setup.sh --plan     # optional: shows the plan, changes nothing
sudo bash ~/concordeai/ollama1/setup.sh
```

Setup prints its plan and a table of the disks with their serials and
models. It stops unless you type `yes`. Here is what it does, in order.
Every step skips what is already done, so it is safe to run again.

1. It sets the host name `ollama1` and the time zone America/New_York. The
   boot menu shows for 5 seconds, via `/etc/default/grub.d/99-ollama1.cfg`:
   - `GRUB_TIMEOUT=5`
   - `GRUB_RECORDFAIL_TIMEOUT=5`
   - `GRUB_TIMEOUT_STYLE=menu`

   It then runs `update-grub` and checks that every `set timeout=` in
   `/boot/grub/grub.cfg` is 5. That includes the recordfail path, which is
   what made it wait 30 seconds on this LVM machine.
2. It installs packages: ttyd, python3-nacl, mdadm, nftables, zstd, and
   cloudflared from Cloudflare's apt repository. It checks that repository
   key's fingerprint first.
3. It grows the root volume into the free space. This happens online.
4. It moves `/home` onto the root filesystem:
   - It copies `/home` and checks the copy file by file. It also checks that
     `authorized_keys` is intact.
   - Only then does it stop mounting the old disk.
   - If a login still has the old `/home` open, it detaches it, and the
     mirror step waits.
5. It wipes the models disk and mounts it at `/srv/models` (by UUID, noatime).
6. It wipes both 8 TB disks, builds the RAID1 mirror and mounts it at
   `/srv/data`. The first sync takes many hours in the background; the mirror
   is usable meanwhile (`cat /proc/mdstat`).
7. It creates the service users.
8. It installs the kit to `/usr/local/lib/ollama1`, along with the systemd
   units, the polkit rule and the local port guard.
9. It asks for the admin email: the only address allowed into the admin
   panel. The email is stored root-only on the desktop.
10. It installs Ollama from its GitHub release (the linux-amd64 archive plus
    the ROCm component). Each file must match the release's `sha256sum.txt`.
11. It sets up the firewall: nothing comes in except SSH from 192.168.86.0/24.
12. It sets up SSH:
    - no root login;
    - only `pmiller` can log in, and only from the LAN;
    - passwords are off, provided a key of your own is there.
13. It turns on automatic security updates, cloudflared updates and the
    weekly Ollama update.
14. It starts the services: the gateway, the panel, the terminal and the
    dashboard on the monitor.
15. It sets up the Cloudflare Tunnel and Access (see below).
16. It asks whether to remove the setup key `claude-setup@concordeai`. It only
    asks if a key of your own is present, and it removes nothing unless you
    type `yes`. You can also pass `--remove-setup-key`. Your own key(s) always
    stay.

**If it asks you to log out:** the old `/home` disk is still held by an
earlier login. This happens if a shell was sitting in your home folder.
Log out of every session (or reboot), log in, and run
`sudo bash ~/concordeai/ollama1/setup.sh` again. It continues with the mirror.

When it finishes, it lists anything still to do.

## Cloudflare

### The tunnel (setup does this)

Setup runs `cloudflared tunnel login` and waits:

1. It prints a link. Open it in your Mac's browser.
2. Log in to Cloudflare, click **flyconcordefly.com**, then **Authorize**.
3. Go back to the terminal. It carries on by itself.

Setup then does the following:

- It creates the tunnel `ollama1` (`cloudflared tunnel create ollama1`).
- It moves the tunnel's credential to `/etc/cloudflared/ollama1.json` (root-only).
- It points both hostnames at the tunnel (`cloudflared tunnel route dns`).
- It writes `/etc/ollama1/cloudflared.yml`, which has these ingress rules:
  - the app hostname goes to the gateway;
  - the admin hostname goes to the panel;
  - everything else gets a 404.

  cloudflared also checks the Access token itself, before anything reaches
  the desktop's services.

The tunnel only starts once Access is set up, so nothing is reachable from
outside before then.

### Access: pick one way

Setup asks when it gets there:

- **1** runs the API helper.
- **2** takes the values you copy from the dashboard.
- **3** leaves Access for later. Run `sudo bash .../setup.sh` again when
  you're ready.

#### Access in the dashboard (click by click)

In dash.cloudflare.com, go to **Zero Trust**. The dashboard's wording moves
around a little from time to time; the pieces are the same.

1. **Login method.** Go to **Settings > Authentication > Login methods**.
   **One-time PIN** is there by default: Access emails you a code. Keep it.
2. **Service token.** Go to **Access > Service auth > Service Tokens >
   Create Service Token**.
   - Name: `ollama1-app`. Duration: 1 year.
   - Click **Generate token**.
   - Copy the **Client ID** and the **Client Secret** now. The secret is
     shown only once, and ConcordeAI will need both.
3. **Admin application.** Go to **Access > Applications > Add an application
   > Self-hosted**.
   - Application name: `ollama1 admin`. Session duration: 24 hours.
   - Public hostname: subdomain `ollama1-admin`, domain `flyconcordefly.com`.
   - Policies: **Create new policy**.
     - Name: `ollama1 admin - Patrick only`
     - Action: **Allow**
     - Include: **Emails**, your email
   - Login methods: One-time PIN.
   - Save.
4. **App application.** Go to **Add an application > Self-hosted** again.
   - Application name: `ollama1 app`. Hostname: `ollama1.flyconcordefly.com`.
   - Policy:
     - Name: `ollama1 app - service token`
     - Action: **Service Auth**
     - Include: **Service Token**, `ollama1-app`
   - Under the application's settings, turn on **Return 401 response for
     service auth policies**. This makes a bad or missing token get a plain
     401 instead of a login page.
   - Save.
5. **Copy four values:**
   - the **Application Audience (AUD) Tag** of each application (open the
     application; it's on its overview / basic information);
   - your **team domain**, like `concorde.cloudflareaccess.com`, from
     **Settings**;
   - the service token's **Client ID**.
6. Run setup again and choose **2**. Paste the values when it asks.

#### Access through the API helper

`sudo ollama1-cf-access`, or choose **1** in setup, does steps 2 to 5 for
you. It needs an API token, which it asks for, uses once and never stores:

1. Go to dash.cloudflare.com > **My Profile > API Tokens > Create Token >
   Create Custom Token**.
2. Give it these permissions:
   - Account: **Access: Apps and Policies**, Edit
   - Account: **Access: Service Tokens**, Edit
   - Account: **Access: Organizations, Identity Providers, and Groups**, Read
   - Zone: **Zone**, Read, for **Specific zone: flyconcordefly.com**
3. Create it and copy it. Paste it when the helper asks. The helper prints
   the service token's Client ID and Secret once, for the app.
4. Delete the API token in the dashboard afterwards.

Neither way puts anything in this repo. The values live in
`/etc/ollama1/config.json` (root-only, 0600), and the tunnel credential in
`/etc/cloudflared/ollama1.json` (0600).

## After setup

- **SSH:** only your Mac's key can log in, as `pmiller`, from the home LAN.
  Passwords and root login are off.
  - If you removed the setup key, it is only your key.
  - To add another Mac later, log in with the first one and append its key
    to `~/.ssh/authorized_keys`.
- **Pairing a device:** at the desktop, run `sudo ollama1-pair`, or click
  **Open pairing window** in the panel. The code shows, large, on the
  monitor and in that terminal for 5 minutes. Type it into ConcordeAI.
  - One device pairs per window.
  - 5 wrong codes close the window.
  - To list devices: `sudo ollama1-pair --list`.
  - To remove one: `sudo ollama1-pair --remove ID`, or use the panel.
- **Models:** none are installed. Choose them first. For each one:
  1. Add its name to the allow-list at the desktop:
     `sudo nano /etc/ollama1/models.allow` (one name per line, e.g. `qwen3:14b`).
  2. Click **Pull** in the panel.

  The panel can't pull anything that isn't on the list. On the desktop,
  `sudo ollama list` shows what's installed. Ollama only answers root and
  the kit's services.
- **Dashboard:** it's on the monitor, and `ollama1-top` shows it over SSH
  (`q` quits). It shows the following, and never a prompt or an answer:
  - tokens per second (now, 1 h, 24 h) and requests;
  - devices;
  - GPU busy %, VRAM, temperatures, power and fan;
  - loaded models;
  - CPU, RAM, disks and RAID;
  - the LAN (`br0` and the Pi's port);
  - the tunnel;
  - updates.
- **Admin panel:** `https://ollama1-admin.flyconcordefly.com`. Access asks
  for your email and a one-time code. The panel has:
  - the same figures, plus 1 h / 24 h graphs;
  - buttons to apply updates, restart the services, reboot, back up, and
    turn LAN mode on or off;
  - pairing and devices;
  - models from the allow-list;
  - a counts-only log;
  - **Open terminal**, which asks for your Linux user and password.
- **LAN mode** is off by default. With it on, the gateway also answers on
  `http://192.168.86.10:8431` from the home LAN, without Access. Signatures
  are still required, and traffic on the LAN is unencrypted. To change it:
  `sudo ollama1-lan on|off`, or use the panel.

## The rules, and where they're enforced

| Rule | Where |
|---|---|
| Only Patrick's devices | The Access service token (checked by cloudflared and again by the gateway, pinned to the one token's Client ID), plus an Ed25519 signature from a key in `devices.json`. Keys are added only by a root step that re-checks the pairing code shown at the desktop |
| Replay, old requests | 60 s timestamp window, nonces remembered for 2 minutes, anything signed before the gateway last started is refused |
| Nothing mixes, nothing kept | One request at a time on the GPU. No history; bodies are never written or logged (`tests/test_stateless.py` checks the source and does a live check with markers). Ollama keeps weights loaded, not conversations |
| GPU only | Fit estimate before loading, `/api/ps` must show 100% VRAM after. Options that change placement (`num_gpu`, etc.) are stripped. Ollama cloud models are refused |
| No model management from outside | The gateway passes on chat, generate, embed, tags, ps, show, version. Pull/delete/create/copy/push are 404. The panel can pull only allow-listed models |
| Admin only | Panel: Access JWT with your email on every request. Actions need a CSRF token, same-origin and JSON. It can only *start* fixed systemd units (polkit rule), never run a command |
| Local users | `ollama1-nft` lets only the kit's users (and root) connect to Ollama, the gateway, the panel and the terminal |
| Services | Dedicated no-shell users, `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, empty capability sets. Secrets are handed in with `LoadCredential=`, so they stay root-only on disk |

## Updates

- **Ubuntu security updates and cloudflared:** unattended-upgrades runs
  daily and reboots at 04:00 New York time when an update needs it.
- **Ollama:** `ollama1-update-ollama.timer` runs Sundays around 03:30.
  - It downloads the release's linux-amd64 archive and the ROCm component.
  - Each must match the release's `sha256sum.txt`, and GitHub's own digest
    for the file.
  - It switches to the new version, then checks that it starts. If it
    doesn't, it switches back. On any mismatch it stays where it is.
  - To run it by hand: `sudo systemctl start ollama1-update-ollama`.
- **Backups:** every night at 02:30, `/etc/ollama1`, the tunnel credential,
  the SSH/apt/GRUB drop-ins and the model list go to
  `/srv/data/backups/ollama1/<date>` (root-only, 30 days kept).

## The network

The desktop sits on `br0`, a bridge over `enp39s0` (to the router) and
`enp38s0` (the Raspberry Pi), at 192.168.86.10. That comes from Patrick's
bridge script (`/etc/netplan/60-ollama1-bridge.yaml`).

Setup never touches netplan or the bridge. It only reads `br0`'s address
for LAN mode and checks that the bridge is up.

The firewall is set so it can't cut the Pi off:

- **Bridge netfilter stays off.** `/etc/sysctl.d/60-ollama1-bridge.conf`
  sets `net.bridge.bridge-nf-call-iptables=0`, so bridged frames between
  the Pi and the router never go through iptables. ufw's FORWARD policy
  therefore never applies to them. Setup checks this.
- **`ufw route allow in on br0 out on br0`** is added as a second line of
  defence, in case something loads `br_netfilter` later.
- **Interface rules use `br0`.** SSH is allowed in on `br0`, from
  192.168.86.0/24.

## Troubleshooting

| | |
|---|---|
| Services | `systemctl status ollama ollama1-gateway ollama1-admin ollama1-tunnel ollama1-dash` |
| Logs (counts only) | `journalctl -u ollama1-gateway`, `journalctl -u ollama1-tunnel` |
| GPU seen by Ollama | `journalctl -u ollama \| grep -i "inference compute"` should say ROCm, gfx1030. The RX 6900 XT is supported as is, so `HSA_OVERRIDE_GFX_VERSION` is not set. If Ollama ever reports no GPU, the gateway refuses every model instead of running it on the CPU |
| Mirror | `cat /proc/mdstat`, `sudo mdadm --detail /dev/md/o1data` |
| Boot menu | `grep 'set timeout' /boot/grub/grub.cfg`, all 5 |
| SSH | `sudo sshd -T -C user=pmiller,host=x,addr=192.168.86.20 \| grep -E 'password\|permitroot'` |

## Tests (on the Mac)

```bash
cd ollama1/tests
python3 -m unittest discover -s .      # ~20 s; stub Ollama, fake Access certs, all on 127.0.0.1
python3 mutate.py                      # ~5 min; breaks each protection on purpose, expects a failing test
python3 gen_vectors.py                 # regenerates PROTOCOL.md's test vectors
```

The tests need Python 3.12 or later, plus `cryptography` or PyNaCl for
Ed25519 (the desktop uses PyNaCl). They cover:

- signatures, replay, clock skew, unpaired devices;
- the pairing window, its rate limit and the root re-check;
- Access JWTs (with a local fake certs endpoint);
- the GPU fit refusal and the unload on a CPU spill;
- that no request body is ever written anywhere;
- admin authentication and CSRF, and the match between the polkit rule and
  the panel;
- the updater's checksum refusals;
- that the repo holds no personal data.
