# ollama1: a private model server kit

This kit turns a spare Linux machine into a model server that only your own
devices, running ConcordeAI, can use. It runs Ollama on the GPU and nothing
else. (It was built on a machine with a Radeon RX 6900 XT, 16 GB of VRAM and
64 GB of RAM; the numbers below are that machine's.)

**"ollama1" is the kit's name, not your server's name.** It is in the kit's
file and command names (`ollama1-pair`, `/etc/ollama1`, `ollama1-gateway.service`)
and stays there. Your server's name is yours to choose when you run setup
(`--name`), and you can run as many servers as you like, named and numbered
however you like. This page writes it as `<server-name>`, your user as
`<your-user>`, your domain as `<your-domain>`, the server's LAN address as
`<server-ip>` and your LAN as `<lan-cidr>`.

- It is reachable from anywhere through a Cloudflare Tunnel. No port is open
  at home, and the home IP stays hidden.
  - `<server-name>.<your-domain>` is the gateway the app talks to.
  - `<server-name>-admin.<your-domain>` is the admin panel.
- Every request needs **both** of these:
  - a Cloudflare Access service token;
  - a signature from a device that was paired **at the server**.

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
  - SSH accepts keys only (your Mac's), for the one user you name, with no
    root login.
  - Services run as users with no shell.
  - Security updates install automatically, with a reboot at 04:00 when an
    update needs one.
  - Ollama is updated weekly, from verified downloads.
- It has a graphical information panel on the server's monitor (a text
  dashboard where none can be drawn), and a web admin panel
  with a terminal that still asks for your Linux password.

The request-signing and pairing rules the app follows are in
[PROTOCOL.md](PROTOCOL.md). For running a server like this on your own
Linux machine, see [docs/your-own-server.md](../docs/your-own-server.md).

## What goes where

| | |
|---|---|
| Host name | `<server-name>` (what you give with `--name`). The time zone stays as the machine has it, unless you give `--timezone` |
| Address | `<server-ip>` on the LAN port (`br0` if you have a bridge over both wired ports, which keeps a second device, like a Raspberry Pi, on the LAN). The kit never changes network settings |
| OS disk (`--os-serial`) | Kept. The root volume grows into the free space on it. `/home` stays where it is |
| Models disk (`--models-serial`) | **Wiped**, ext4, `/srv/models`. This is Ollama's model folder |
| Any other disk (including the two old mirror disks, `--hdd1-serial`, `--hdd2-serial`) | **Never touched.** There is no mirror: the kit builds no RAID and never wipes, assembles or mounts these disks (6b400). The nightly copy of the settings and the model list goes to `/var/backups/ollama1` on the root filesystem. A server that still has the old RAID can remove it with `tools/remove-raid.sh` (below) |
| Ollama | 127.0.0.1:11434 only, ROCm build, in `/opt/ollama` |
| Gateway | 127.0.0.1:8431, user `o1gw` |
| Admin panel | 127.0.0.1:8432, user `o1admin` |
| Web terminal (ttyd + `login`) | a UNIX socket in `/run/ollama1/ttyd` (root and the panel's user only), reached only through the panel |
| Dashboard | tty1 (the monitor), user `o1dash`: the graphical panel (`/dev/fb0`) or the text dashboard. Also `ollama1-top` over SSH |
| ComfyUI (optional) | 127.0.0.1:8188, only root and the gateway may connect. Not installed by `setup.sh`: `sudo bash tools/install-comfyui.sh` (images and video; see [docs/your-own-server.md](../docs/your-own-server.md)) |
| Settings | `/etc/ollama1/`: `config.json` (root-only), `devices.json`, `models.allow` |
| Setup log | `/var/log/ollama1-setup.log` |

## Before you start

1. **Put your Mac's SSH key on the server.** Setup turns password login
   off, and after that only a key can log in. On the Mac:

   ```bash
   ssh-copy-id <your-user>@<server-ip>
   ssh <your-user>@<server-ip>        # should log in without asking for the password
   ```

   Setup refuses to turn passwords off until `~/.ssh/authorized_keys` holds
   a key other than `claude-setup@concordeai` (the key used to build this
   kit). Without one it keeps passwords on and tells you what to do.
2. **Make one Cloudflare API token** (see [The API token](#the-api-token)).
   It is the only thing you do in the Cloudflare dashboard. Setup does the
   tunnel, the DNS records and Access with it.
3. **Zero Trust must exist once.** If this Cloudflare account has never
   opened Zero Trust, setup stops and says so. The fix is one visit:
   dash.cloudflare.com > **Zero Trust** (left sidebar) > pick a team name
   (for example `concorde`) > choose the **Free** plan > **Proceed**.
   Cloudflare may ask for a card for the $0 plan.

## Run it

At the server or over SSH:

```bash
git clone https://github.com/bigmillz/concordeai.git ~/concordeai
cd /                                          # not inside /home
bash ~/concordeai/ollama1/setup.sh --plan     # optional: shows the plan, changes nothing
sudo bash ~/concordeai/ollama1/setup.sh --name <server-name> --zone <your-domain> \
     --os-serial <serial> --models-serial <serial>
```

### What you tell setup

Setup takes nothing about your machine from the kit: you give it these (and
it asks for any it can't find, at the terminal):

| Argument | What it is |
|---|---|
| `--name <server-name>` | Your server's name: lowercase letters, digits and hyphens, 1 to 32 characters, starting with a letter (`gpu-2`, `workshop`, `srv01`). It becomes the host name, the gateway `<server-name>.<your-domain>`, the panel `<server-name>-admin.<your-domain>`, and the names of the tunnel (`<server-name>`), the service token (`<server-name>-app`) and the Access policies and applications (`<server-name> admin - <your-name> only`, `<server-name> app - service token`, `<server-name> admin`, `<server-name> app`). Run several servers by giving each its own name |
| `--user <your-user>` | The one Linux user who may log in over SSH. Default: the user who ran `sudo` |
| `--lan <lan-cidr>` | The network SSH is allowed from, like `10.0.0.0/24`. Default: the network of the machine's LAN port (detected, and shown in the plan: check it before you type `yes`). Must be a private network of /16 or narrower; `--lan-public-ok` allows another one. SSH from outside it is cut off, and setup says so (and asks for `yes`) if your session is outside it |
| `--zone <your-domain>` | Your domain on Cloudflare. Not needed with `--skip-cloudflare` |
| `--owner <your-name>` | Your name, used only in the Access policy's name. Default: from your admin email |
| `--timezone <Area/City>` | Default: the time zone the machine already has |
| `--os-serial`, `--models-serial` | The two disks, by serial. `lsblk -d -o NAME,SIZE,MODEL,SERIAL` lists them. Setup checks each serial exactly before it wipes anything (only the models disk is ever wiped) |
| `--hdd1-serial`, `--hdd2-serial` | Accepted and **ignored**, with a one-line note, so an old command line or `setup.env` keeps working. They are kept in `setup.env` only so `tools/remove-raid.sh` can find the old mirror's disks |
| `--fans on\|off` | The graphics card's fan and the motherboard's fans at 100% while the server works and for 60 s after, 50% for the next 60 s, then 20%: see "Fans" below. **On unless you say `--fans off`** (or `OLLAMA1_FANS=0`). Saved in `setup.env` (`FANS=`), so a re-run without the flag keeps it |
| `--leds on\|off` | The lights: every RGB device OpenRGB lists follows the graphics card's load, white at 0% through yellow and orange to red at 100% (see "Lights" below). **Off unless you say `--leds on`** (or `OLLAMA1_LEDS=1`); `on` installs the `openrgb` package. Saved in `setup.env` (`LEDS=`), so a re-run without the flag keeps it |
| `--gpu-tune` | Opt in (or `OLLAMA1_GPU_TUNE=1`): tune an AMD Navi 21 graphics card, see "Graphics card tuning" below. **Off unless you ask**: without it setup changes nothing about the card and prints one line saying the option exists. Saved in `setup.env`, so a re-run without the flag keeps it. `--no-gpu-tune` (or `OLLAMA1_GPU_TUNE=0`) is the explicit off: the card goes back to stock and the choice is saved as off |

What you give is saved in `/etc/ollama1/setup.env` (root-only) and the
server's name and domain in `/etc/ollama1/config.json`, so a re-run needs
none of it again. A name already in `config.json` is never changed by
setup: the tunnel, DNS and Access carry it.

**A server set up before the name was a setting** has no `server_name` in
`config.json`. Setup treats that as the name `ollama1`, so it keeps its
hostnames, tunnel, token and policies. Run setup again with the values it
has always had, so they are written down: `--user`, `--lan`, `--zone`, the
four serials, and `--owner` with the name that is in its Access policy's name
(for the policy `ollama1 admin - Sam only`, that is `--owner Sam`). Anything
already in `config.json` (hostnames, `tunnel_name`, `token_name`,
`policy_admin_name`, ...) wins over what is derived from the name.

Setup starts itself inside **tmux** (session `ollama1-setup`), so a dropped
SSH connection can't stop it halfway. If you get disconnected, log in again
and run `sudo tmux attach -t ollama1-setup`. (`--no-tmux` turns this off.)
Only one setup runs at a time (a lock), and when the tmux session ends the
command you typed reports setup's exit status.

Setup prints its plan and a table of the disks with their serials and
models. It stops unless you type `yes`. Here is what it does, in order.
Every step skips what is already done, so it is safe to run again.

1. It sets the host name `<server-name>` (and the time zone, if you gave
   one). The boot menu shows for 5 seconds, via `/etc/default/grub.d/99-ollama1.cfg`:
   - `GRUB_TIMEOUT=5`
   - `GRUB_RECORDFAIL_TIMEOUT=5`
   - `GRUB_TIMEOUT_STYLE=menu`

   It then runs `update-grub` and checks that every `set timeout=` in
   `/boot/grub/grub.cfg` is 5. That includes the recordfail path, which is
   what made it wait 30 seconds on the LVM machine this was built on.
2. It installs packages: ttyd, python3-nacl, nftables, zstd, tmux,
   and cloudflared from Cloudflare's apt repository. That repository's key
   must have the pinned fingerprint, and only that one key is kept.
3. It grows the root volume into the free space (online). It reads the free
   space as a count of extents and stops loudly if it can't.
4. It leaves `/home` where it is (it used to copy it onto the root
   filesystem so a mirror disk could be wiped; there is no mirror now).
5. It wipes the models disk and mounts it at `/srv/models` (by UUID,
   noatime). An earlier `o1models` filesystem is kept, never wiped.
6. (There is no mirror step any more: the plan says "No mirror: not used".)
   The safety rules for the models disk are:
   - a disk is found by its serial, checked again exactly, and wiped only
     through its `/dev/disk/by-id/…<serial>` path;
   - it stops if fstab, crypttab or swap still uses the disk, or if it
     carries anything other than the ext4 filesystems this machine has now;
   - a filesystem is made only on a partition that setup itself just created
     (it leaves a marker until the format is done), so a stop between
     partitioning and formatting is picked up next time;
   - a partition setup didn't create that has no readable filesystem is never
     formatted: setup stops and says how to check it (`mke2fs -n` lists the
     backup superblocks, `e2fsck -b <backup>` repairs). A read error from
     `blkid` also stops it;
   - fstab never gets a line without a UUID.
7. It creates the service users.
8. It installs the kit to `/usr/local/lib/ollama1`, along with the systemd
   units, the polkit rule and the local port guard. It asks for the admin
   email: the only address allowed into the admin panel. The email is stored
   root-only on the server.
9. It sets up the firewall: nothing comes in except SSH from `<lan-cidr>`.
10. It sets up SSH:
    - no root login;
    - only `<your-user>` can log in, and only from the LAN;
    - passwords go off once it has shown you the comment and fingerprint of
      each key it counts as yours and you type `yes`.

    **Keep that session open** and check a NEW login from another terminal
    on the Mac before closing it. If your SSH agent offers several keys
    first, name yours:
    `ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 <your-user>@<server-ip>`.
11. It installs Ollama from its GitHub release (the linux-amd64 archive plus
    the ROCm component). Each file must match the release's `sha256sum.txt`.
    The firewall and SSH are done before this long download.
12. It turns on automatic security updates, cloudflared updates and the
    weekly Ollama update.
13. It starts the services: the gateway, the panel, the terminal and the
    dashboard on the monitor.
14. Only if you give `--gpu-tune` (or saved it on an earlier run): it tunes
    the graphics card, if it is an AMD Navi 21 (RX 6800, 6800 XT,
    6900 XT, 6950 XT): the card's highest
    power limit and a small memory-clock bump, checked under load (see
    "Graphics card tuning" below). The memory clock needs the kernel's
    overdrive switch, `/etc/default/grub.d/97-amdgpu-overdrive.cfg`, so it
    starts after a reboot; setup lists that under "Still to do". Without
    `--gpu-tune` it only prints that the option exists.
    Then the fans (see "Fans" below), unless you give `--fans off`: it
    removes the hand-made `ollama1-gpu-fan.service` if there is one, loads
    the motherboard's fan chip driver when it isn't loaded, and starts
    `ollama1-fan.service`.
    Then the lights (see "Lights" below), only with `--leds on`: it installs
    the `openrgb` package, and starts `ollama1-openrgb.service` and
    `ollama1-leds.service`. With `--leds off` (the default) it stops and
    disables both and removes nothing else.
15. It sets up Cloudflare with the API token you paste: the tunnel, the DNS
    records and Access (see below).
16. It checks what listens on the network: nothing but sshd may listen
    beyond loopback (plus the gateway on br0, in LAN mode).
17. It asks whether to remove the setup key `claude-setup@concordeai`. It only
    asks if a key of your own is present, and it removes nothing unless you
    type `yes`. You can also pass `--remove-setup-key`. Your own key(s) always
    stay.

When it finishes, it lists anything still to do.

## Cloudflare

Setup offers three choices when it gets there:

- **1 (the default): paste one API token.** Setup does everything through
  Cloudflare's API.
- **2: no API token.** `cloudflared tunnel login` in a browser for the
  tunnel and DNS, and Access clicked in the dashboard. This is the
  fallback, described at the end of this section.
- **3: later.** Run `sudo bash .../setup.sh` again when you're ready.

Nothing reaches the server from outside until this step is done: the
tunnel only starts once Access is in place.

### The API token

This is the only thing to click in Cloudflare:

1. Go to dash.cloudflare.com > **My Profile > API Tokens > Create Token >
   Create Custom Token**. Name it whatever you like, like `<server-name> setup`.
2. Add exactly these permissions:

   | Scope | Permission | Level |
   |---|---|---|
   | Account | Cloudflare Tunnel | Edit |
   | Account | Access: Apps and Policies | Edit |
   | Account | Access: Service Tokens | Edit |
   | Account | Access: Organizations, Identity Providers, and Groups | Read |
   | Zone | DNS | Edit |
   | Zone | Zone | Read |

3. Set **Zone Resources** to **Include > Specific zone > `<your-domain>`**.
4. Set **TTL** so the token ends tomorrow (a 1-day expiry). It is needed
   for a few minutes only.
5. Click **Continue to summary > Create Token**, and copy the token.
6. In setup, choose **1** and paste it. It isn't shown as you paste.
7. When setup is done, **delete the token**: My Profile > API Tokens > the
   token's **...** menu > **Delete**.

How setup treats the token:

- It is read with `read -s`: never echoed, never in the shell history or
  the setup log.
- It goes to `ollama1-cf-access` over a pipe, never as a command-line
  argument or an environment variable, and setup clears it right after.
- The helper keeps it in memory only and drops it when done.
- `tests/test_cloudflare.py` checks that it never lands in a file, a log
  or the output.

With the token, `ollama1-cf-access` does the following. Each step reuses
what already exists, so running setup again changes nothing that's
already right.

1. It finds the zone `<your-domain>`, the account that owns it, and
   the Zero Trust team domain.
2. **Tunnel `<server-name>`.** This is a *locally-managed* tunnel (`config_src:
   local`). Its routes, and cloudflared's own Access check, are in
   `/etc/ollama1/cloudflared.yml` on the server, not in the dashboard.
   - The tunnel's credential is written to `/etc/cloudflared/ollama1.json`
     (root, 0600).
   - If that file is ever lost, a rerun rebuilds it for the same tunnel.
3. **DNS.** One proxied CNAME for each of `<server-name>` and `<server-name>-admin`,
   pointing to `<tunnel id>.cfargotunnel.com`.
   - A wrong target is corrected and duplicate CNAMEs are removed, but only
     for CNAMEs that point at a tunnel or that it made itself.
   - Any other record on those names (an A record, or a CNAME somewhere
     else) makes it stop and name the record. It never changes or deletes
     records it didn't make.
4. **Access.**
   - The service token `<server-name>-app`, which never expires.
   - The policies `<server-name> admin - <your-name> only` (your email) and
     `<server-name> app - service token`.
   - One self-hosted application per hostname. The app hostname answers a
     bad token with a plain 401, not a login page.

   Access logs you in with a one-time code sent to your email; that login
   method exists in every Zero Trust account.
5. It writes the team domain, both AUD tags, your email, the service token's
   Client ID and the tunnel id to `/etc/ollama1/config.json` (root, 0600).
6. It shows the service token's **Client ID and Client Secret once**, for
   ConcordeAI, straight on the terminal (not through the setup log), the
   moment the token is made, so a later failure can't lose it. Copy them
   then: the secret is saved nowhere, in the repo or on the server. If a
   run stops before it could show the secret, the next run makes a new
   secret by itself.
   - Running it again doesn't show the secret again.
   - To make a new secret, run
     `sudo ollama1-cf-access --rotate-service-token`. After that the app
     needs the new secret.

You can run the helper on its own at any time: `sudo ollama1-cf-access`.
It asks for a token.

### Without an API token (fallback)

Choose **2** in setup.

**The tunnel.** `cloudflared tunnel login` prints a link:

1. Open the link on your Mac.
2. Pick **`<your-domain>`** and click **Authorize**.

Setup then creates the tunnel, puts its credential in
`/etc/cloudflared/ollama1.json` (0600), and runs `cloudflared tunnel route
dns` for both names. The login leaves `/root/.cloudflared/cert.pem`
(root-only).

**Access, clicked in dash.cloudflare.com > Zero Trust.** The wording moves
around a little from time to time.

1. **Login method.** Go to **Settings > Authentication > Login methods**.
   **One-time PIN** is there by default. Keep it.
2. **Service token.** Go to **Access > Service auth > Service Tokens >
   Create Service Token**.
   - Name: `<server-name>-app`. Duration: **Non-expiring**.
   - Click **Generate token**.
   - Copy the **Client ID** and the **Client Secret** now. The secret is
     shown once.
3. **Admin application.** Go to **Access > Applications > Add an application
   > Self-hosted**.
   - Name: `<server-name> admin`. Session duration: 24 hours.
   - Public hostname: `<server-name>-admin`.`<your-domain>`.
   - **Create new policy**:
     - Name: `<server-name> admin - <your-name> only`
     - Action: **Allow**
     - Include: **Emails**, your email
   - Save.
4. **App application.** Go to **Add an application > Self-hosted** again.
   - Name: `<server-name> app`. Hostname: `<server-name>.<your-domain>`.
   - Policy:
     - Name: `<server-name> app - service token`
     - Action: **Service Auth**
     - Include: **Service Token**, `<server-name>-app`
   - In the application's settings, turn on **Return 401 response for
     service auth policies**.
   - Save.
5. **Copy four values:**
   - each application's **Application Audience (AUD) Tag**;
   - the **team domain** (like `concorde.cloudflareaccess.com`, under
     **Settings**);
   - the service token's **Client ID**.

   Type them when setup asks.

## After setup

- **SSH:** only your Mac's key can log in, as `<your-user>`, from the home LAN.
  Passwords and root login are off.
  - If you removed the setup key, it is only your key.
  - To add another Mac later, log in with the first one and append its key
    to `~/.ssh/authorized_keys`.
- **Pairing a device:** at the server, run `sudo ollama1-pair`, or click
  **Open pairing window** in the panel. The code shows, large, on the
  monitor and in that terminal for 5 minutes: 12 characters, like
  `7K4M-2QXD-9FHT`. Type it into ConcordeAI.
  - One device pairs per window.
  - 5 wrong codes close the window.
  - Pairing works only through the tunnel, never on the LAN listener.
  - The gateway never sees the code: a root step checks it and adds the key.
  - To list devices: `sudo ollama1-pair --list`.
  - To remove one: `sudo ollama1-pair --remove ID`, or use the panel.
- **Models:** none are installed. Choose them first. The allow-list,
  `/etc/ollama1/models.allow`, decides what may be installed (one name per
  line, e.g. `qwen3:14b`), and `ollama1-models` makes the installed models
  match it:

  ```bash
  sudo ollama1-models add qwen3:14b          # on the list, then offers to download it
  sudo ollama1-models add gpt-oss:120b ram   # the same, allowed to use system memory
  sudo ollama1-models status                 # what a sync would do; changes nothing
  sudo ollama1-models sync                   # that preview, then (typed yes) exactly that
  sudo ollama1-models remove qwen3:14b       # off the list, and deleted
  ```

  - **A sync** lists what it will do, with sizes: **Remove** (installed,
    not on the list), **Download** (on the list, not installed) and
    **Update** (the registry has a newer version). Then the total to
    download, the space freed, and the disk space now and after.
    - Nothing happens until you type `yes`, and then it does exactly what
      it listed: removals first (so their space is there), then downloads
      and updates, with a bar for the model and one for the whole sync
      ("about 12 min left", from a smoothed rate). Each download is tried 3
      times. A summary follows.
    - **Removal only ever touches installed models that are not on the
      allow-list.** Each one is checked against the list again right before
      it's deleted: a model put back on the list meanwhile is kept, and one
      taken off the list isn't downloaded.
    - **A missing or unreadable allow-list is an error**, never an empty
      list: nothing is allowed, and nothing is removed.
    - **A line the allow-list can't read never costs a model.** A model
      named on any line (say `gpt-oss:120b  RAM`, where the flag is
      misspelt) is never removed, in any letter case. While any line is
      bad, a sync removes nothing at all; the preview and the panel's
      confirmation show the bad lines.
    - If the free disk space can't be read, the sync is refused. "Freed"
      counts only the files no kept model shares.
    - An update is found without downloading anything: the registry's
      manifest for the tag is compared with the installed one (Ollama keeps
      the manifest byte for byte, and its digest is what `ollama list`
      shows). Sizes count only layers that aren't on the disk already.
    - A model the registry doesn't know (a typo) or can't be checked
      (offline) is listed with a `?` and left alone: never removed.
    - A sync that would leave under 2 GiB free is refused.
    - Ollama's `cloud` entries aren't models on this disk: a sync leaves them
      alone, and the gateway never serves them.
    - One sync at a time. The server won't sleep during one.
  - **In the panel**, **Update model library** shows the same preview.
    **Apply these N changes** then starts a root unit that recomputes the
    plan and carries it out only if it is still the one you saw; if the
    list or the registry changed, it refuses and shows the new preview
    instead. A preview is good for 30 minutes. Progress shows in the panel.
  - The panel's **Pull** and **Remove** buttons still work for one model.
    The panel can't add to the list: that stays at the server.
  - On the server, `sudo ollama list` shows what's installed. Ollama only
    answers root and the kit's services.

  **Models that may use system memory.** Every model runs entirely on the
  GPU unless its line in the allow-list ends in `ram`:

  ```
  qwen3:14b
  gemma4:26b      ram
  qwen3.6:35b     ram
  gpt-oss:120b    ram
  ```

  - A `ram` model loads whatever doesn't fit in the 16 GB of VRAM into the
    server's 64 GB of RAM. Mixture-of-experts models are the ones worth
    doing this for: only a few experts work on each token, so they stay
    usable.
  - Expect a few to low tens of tokens per second, against 50 to 100+ for a
    model that fits in VRAM. The app shows these models as `gpu+ram`.
  - The gateway lets a `ram` model load only if its whole resident size
    fits. That size is its weights, the KV cache for the context, and 2.5 GiB
    of compute buffers. The room it has is the free RAM, less the larger of
    8 GiB and 12% of RAM, and it must stay under Ollama's memory cap. If the
    app didn't ask for a context size, it tries 8192, then 4096, then 2048.
    Otherwise it refuses with the numbers (`gpu_fit`) instead of trying.
  - **Repacking decides whether VRAM counts.** By default llama.cpp repacks
    the weights it keeps on the CPU into a new anonymous buffer
    (`CPU_REPACK`) whose size Ollama doesn't predict. On the server,
    gpt-oss:120b got a 58 GiB `CPU_REPACK` buffer and almost nothing on the
    GPU, and the kernel OOM-killed it at about 62 GB. So while repacking is
    on (the default), the gateway counts the whole model against system
    memory alone and ignores VRAM.
    - VRAM counts too only when repacking is off. That takes both
      `LLAMA_ARG_REPACK=false` in ollama.service and `"ollama_no_repack":
      true` in config.json, and it is not the default until the test below
      has shown the numbers.
    - With repacking on, `ram` models are read without mmap
      (`use_mmap: false`, llama-server `--load-mode none`). With it off,
      mmap stays on: the CPU weights are then file-backed page cache, which
      the kernel can evict under the cap.
  - Ollama has a hard memory cap: all RAM but 8 GiB (`MemoryMax`, with
    `MemoryHigh` 2 GiB below). setup.sh works it out from `/proc/meminfo`
    and writes it to `ollama.service.d/10-ollama1-memory.conf`.
    - A load that doesn't fit ends in Ollama's own cgroup: the kernel kills
      the runner, Ollama carries on (`OOMPolicy=continue`), and the app gets
      `ram_oom`. The server stays up; that is what happened on the second
      try.
    - Ollama can't use swap (`MemorySwapMax=0`). A load that makes anything
      else swap is unloaded and refused (`ram_pressure`).
  - **gpt-oss:120b is experimental on a 64 GB machine.** With repacking on,
    the gateway refuses it: about 61 GiB can't fit in the roughly 50 GiB of
    system memory it may use. Whether it can run with repacking off, or with
    encrypted swap, is what the controlled test measures. The smaller
    mixture-of-experts models (gemma4:26b, qwen3.6:35b) are well inside the
    limits.
  - A `ram` model runs alone: other models are unloaded before it loads,
    and it is unloaded before a GPU-only model runs.
  - The only flag is `ram`. A line with any other word after the name is
    ignored entirely, and the panel shows it with the reason.
  - **The controlled test**, run at the server:

    ```bash
    sudo bash ~/concordeai/ollama1/tools/ram-model-test.sh
    ```

    It measures gpt-oss:120b at num_ctx 4096 in the `norepack` and
    `norepack-moe` configurations (the default). `swap` runs only when asked
    for (`--configs swap`), and only with the encrypted swap on; otherwise
    it refuses before starting and says why. Swap is to be revisited only if
    both no-repack configurations fail. Each
    runs under a runtime memory cap (RAM less 8 GiB), with a watchdog that
    kills Ollama if the machine drops under 1.5 GiB free. Worst case: an
    Ollama restart. At the end Ollama goes back to its normal settings.

    | Configuration | What changes | Question it answers |
    |---|---|---|
    | `norepack` | `LLAMA_ARG_REPACK=false`, mmap on, llama.cpp places the layers | do file-backed weights fit under the cap, and how fast is it? |
    | `norepack-moe` | the same, plus every layer on the GPU except the experts of the first N (`--ncmoe`, default 29 of 36), automatic placement off | how much faster with the GPU full? |
    | `swap` (only with `--configs swap`) | Ollama's defaults, but Ollama may use the encrypted swap (needs `--encrypted-swap`; refused otherwise) | does swap carry the repack buffer, and at what speed? |

    For each configuration it records:
    - whether the model loaded, was OOM-killed, or was stopped by the
      watchdog;
    - the GPU / system-memory split, and the buffers llama.cpp reported;
    - peak Ollama memory and swap, and the lowest free memory on the
      machine;
    - load time, time to first token, and tokens/s.

    It starts itself in tmux (session `ollama1-ramtest`), so a dropped SSH
    connection can't stop it halfway; if the connection drops, `sudo tmux
    attach -t ollama1-ramtest`. If it is stopped anyway (hangup, `kill`),
    it still removes its runtime settings and restarts Ollama normally, and
    setup.sh removes them too.

    Options: `--model`, `--ctx`, `--ncmoe`, and `--configs norepack,gateway`
    (`gateway` = what the gateway sends today; it is expected to hit the
    cap). Results: the terminal, `/var/log/ollama1-ram-test.log` and
    `/var/log/ollama1-ram-test.json`.
  - **Encrypted swap (opt-in, undoable):**
    `sudo ./setup.sh --encrypted-swap 32G`.
    - It makes an LVM volume, `ubuntu-vg/ollama1swap`, opened at every boot
      as plain dm-crypt with a new random key (`/etc/crypttab`:
      `/dev/urandom swap,cipher=aes-xts-plain64,size=512,nofail`, i.e.
      AES-256 in XTS; `nofail` so it never holds up a boot). A volume, not
      a swap file: dm-crypt over a file goes through a loop device, which
      can stall under memory pressure.
    - **It needs that much free space in `ubuntu-vg`.** Check with
      `sudo vgs ubuntu-vg`: the `VFree` column is what's left. By default
      setup.sh gives all of the volume group's free space to `/`, and `/`
      can't shrink while it's mounted, so on a server set up that way
      (VFree `0`) it stops with the numbers and changes nothing.
    - For a new install that should have room for it, run setup the first
      time with `sudo ./setup.sh --vg-reserve 64G`: when it grows `/` it
      leaves that much free in `ubuntu-vg` (default `0`, so nothing
      changes unless asked). It has no effect once `/` has been grown.
    - The key lives only in kernel memory, so after a reboot nothing written
      there can be read.
    - It replaces the plain, unencrypted `/swap.img`, which is switched off
      and kept on disk. **That file still holds whatever was swapped to it
      before**: `on` offers to overwrite it with zeros (type `wipe`).
    - If a run stops half way, running it again finishes it.
    - It does not let Ollama swap: only the test's `swap` configuration
      does, for the length of the test.
    - Undo: `sudo ./setup.sh --remove-encrypted-swap`. It stops, changing
      nothing, if the swap can't be taken back in (not enough free memory),
      and it puts `/swap.img` back in fstab only if `on` took it out, and
      removes the volume only if `on` made it.
- **Graphical panel (the monitor's default when one is connected):** instead
  of characters and hash marks, the monitor shows a picture: ring and bar
  gauges, and rolling line graphs of the last 5 minutes (graphics card use,
  VRAM, power and temperature; CPU use, temperature, clock and RAM; tokens
  per second), the loaded models, requests in flight and waiting, disks and
  the network, the sleep state (idle time and the countdown when auto sleep
  is on), and the server's name and address. Green, amber and red are the
  same as the text dashboard's (a drive missing, a hot card, a failed
  update turn the badge and the Status box red). When a pairing window is
  open the whole screen shows the code, as large as fits, with the time left.
  - It draws straight to the screen's framebuffer (`/dev/fb0`): no desktop,
    no browser, nothing added to the server. **Everything is smooth**: it is
    laid out on a small grid (about 640x360) so sizes stay big and readable
    from across a room, but drawn at the screen's real resolution (a 3840x2160
    screen is drawn at 3840x2160; the grid is multiplied by 6, never a small
    picture stretched). All text is a rounded stroke font (capitals, lower
    case, digits and every sign the panel prints, names kept as they are);
    rings, bars, graph lines (round joins) and box corners have anti-aliased
    edges. It costs little: a picture every 2 seconds (every second while
    pairing), only the boxes whose numbers changed are redrawn, dials and graph
    plots come from kept pictures, only the rows that changed are written (about
    3% of one core at 4K with every number moving), and nothing is drawn while
    another console (Alt+F2) is on the screen. It puts the console in graphics mode
    while it runs, so the text console never paints over it, and always puts
    it back when it stops.
  - **When it is used.** `--dash auto` (the default): when `/dev/fb0` exists
    and a monitor is connected (`/sys/class/drm/*/status` says `connected`);
    a monitor plugged in later is picked up within 10 seconds. Otherwise
    the text dashboard below. `sudo ./setup.sh --dash text` keeps the text
    dashboard always, `--dash graphic` insists on the panel whenever there is
    a framebuffer, `--dash auto` goes back; the choice is saved in
    `setup.env` and written to `/etc/ollama1/dash-mode`. A one-off:
    `OLLAMA1_DASH=text` in the unit's environment.
  - **Space: electricity cost.** Press Space on the server's keyboard and the
    screen shows only the cost of electricity over the last 24 hours, 7 days
    and 30 days, in very large smooth rounded print (the same figures as the
    admin panel's Power card, with the tariff's currency symbol); Space again
    goes back. Other keys do nothing. There are no asterisks on this screen,
    estimated or not. With no price set it says "Set your electricity price in
    the admin panel", with no readings yet "No data yet", and a history shorter
    than a window is labelled ("only 3d 4h of data"). An open
    pairing window takes the screen from either view. The keyboard is read
    without echo, so keys never reach a login prompt. The seven-day and
    30-day figures come from the power service, so it needs restarting after
    an update of this kit (`sudo systemctl restart ollama1-power`).
  - **If anything goes wrong** (the framebuffer can't be opened, an odd
    pixel format, any error while drawing) the reason goes to the journal
    (`journalctl -u ollama1-dash`) and the text dashboard takes over; it
    does not try the panel again until the service restarts.
  - To see the design without a screen: `ollama1-dash --png panel.png`
    (`--size 3840x2160` is the screen's size in pixels, `--screen cost`, `--pairing`, `--live` for this
    machine's own numbers instead of sample ones). It reads the same numbers
    as the text dashboard and shows counts, sizes and times only.
- **Text dashboard:** it fills the server's monitor (tty1) when the panel is
  off, and `ollama1-top` shows it over SSH (`q` quits, `--ascii` for plain
  terminals, `--once` for one
  text frame). It updates every second. It is made to be read from a few
  feet away: a few boxes, each a label, a value and one bar or trend line
  (the trend covers the last 5 minutes, or an hour after pressing `t`).
  - **GPU:** busy %, VRAM and power against their limits (bars), temperature
    and fan, and a trend of busy %.
  - **CPU and memory:** CPU %, temperature and load; RAM, and Ollama's
    memory against its cap (or swap when there is no cap).
  - **Requests:** active, queued, today; tokens per second and time to
    first token; errors by kind (busy, fit, spill, ram, oom, Ollama).
  - **Loaded models:** each model's GPU share, VRAM and time until unload,
    and the last load, unload or refusal.
  - **Storage:** free space on / and /srv/models (and on /srv/data and the RAID
    state with resync %, only on a server that still has the old mirror);
    read and write throughput.
  - **Network:** throughput in and out, each port's link, the tunnel and its
    round-trip time.
  - **Health:** the problems, worst first, or "All clear"; uptime, updates,
    power and cost. The header's badge (ALL CLEAR, N TO CHECK, N PROBLEMS)
    shows the same count on any size of screen.
  - While a pairing window is open, the code fills the whole screen.

  How it looks and works:
  - The screen is a fixed grid of boxes (two columns from 76 wide, one
    below; about 100x30 is what it is made for). A box can never be written
    outside of: each is drawn on its own small buffer, cut to the box, and
    every text is cut to its width (counting wide characters as two cells
    and ANSI codes as none) with "...". A short or narrow screen drops the
    lowest-priority boxes (Health first, then Network and Storage, ...), and
    never overlaps. Wider than 120 columns, the layout is centred, not
    stretched; 80x24 shows six of the seven boxes, and the problem count is
    in the header.
  - The whole frame is built in memory and written with absolute cursor
    positions: changed lines only, everything every 30 seconds, and a full
    clear whenever the screen size changes (the size is read every time,
    and when the window changes). There is no curses, so no stale
    fragments.
  - **A big font on the monitor.** The console's own font is 8x16, which
    makes a 1080p screen 240x67 characters, too small to read from a chair.
    Setup installs the console fonts (`console-setup-linux`, and
    `console-terminus` where the distribution has it) and, each time the
    dashboard starts on tty1, it loads the one nearest 120 columns (a 16x32
    font on a 1080p monitor: about 120x33). It draws with block characters
    if that font has them (it checks), else in plain ASCII. To get it on a
    server that is already set up, pull the kit and run setup again
    (`sudo ./setup.sh`; every step skips what is done). To keep the console
    font as it is: `sudo ./setup.sh --no-console-font` (or
    `OLLAMA1_DASH_FONT=off`); running setup again without it brings the big
    font back. The font is only ever loaded by the dashboard service;
    nothing about the video mode or the kernel command line changes, so the
    display can't be lost.
  - Keys only change the view (`t`). On tty1 nothing quits it or opens a
    shell.
  - It never shows a prompt or an answer: only counts, sizes, times and
    names.
  - A frame takes a few milliseconds.
- **Admin panel:** `https://<server-name>-admin.<your-domain>`. Access asks
  for your email and a one-time code. It is one dark, graphical page that
  updates itself every few seconds (and stops asking while its tab is
  hidden). From the top:
  - a header with the server's name, a status ring (Healthy, Working,
    Attention or Problem, with what needs attention beside it: tunnel or
    gateway down, a RAID member missing, a disk over 90%, a failed update,
    a hot CPU or GPU, a reboot needed, the pairing window open), and tokens
    per second, requests in flight, watts, the sleep countdown and uptime;
  - live gauges: GPU busy, VRAM, GPU power and temperature, CPU busy, clock
    and temperature, memory;
  - 1 h / 24 h area graphs of tokens per second, GPU busy, VRAM, GPU
    temperature and power, CPU busy, temperature and speed, and memory,
    with a crosshair that reads out any moment;
  - **Hardware**: the CPU card below, the fans (the phase: working, hold100,
    hold50 or idle20, the level, each fan's rpm and the pump), the lights
    (their colour now and why), storage (each disk's use, the RAID mirror
    and its check's progress), the services, the network, and GPU tuning
    (what is applied and the last check; it is changed at the server only);
  - **Models**: what is loaded, how much of the graphics memory each uses
    and when it unloads; the library with Remove (type the name to confirm),
    Pull, and Update model library;
  - **Power and cost**: watts now and over the last hour, cost and energy
    for the last hour, 24 h, 7 and 30 days with the hours measured, the
    projection, and the prices editor;
  - **Controls**: Sleep now, Reboot, Restart services, the auto sleep
    switch and its idle minutes (chips 15 / 30 / 60 / 120 or any number
    from 5 to 1440; the app sets the same setting and the last change wins)
    with the server's own decision ("Sleeps in 12:34", or why not; the page
    says "Saved" until the idle service's next look, within 30 seconds), and
    a change shows the toast only after the server has read the file back;
    Apply updates now with the updater's last
    result, LAN mode, Back up now and Open terminal;
  - **Devices**, with Remove and Open pairing window;
  - **Logs**: requests, model loads, actions, updates, the model library,
    errors and the services' status lines, each with a filter, Follow new
    lines and Copy. Counts, names and times only, never a prompt or an
    answer.

  Every button that changes something asks first in a dialog, and the
  answer shown afterwards is the server's own (a refusal says why). The
  page loads nothing from anywhere else.
  - the CPU card in detail:
    the model, cores and threads, the frequency driver and
    governor, the speed now (average, highest and lowest across the
    threads, and a strip for each thread), the temperature (the CPU's own
    sensor, found by name: `k10temp` on AMD, `coretemp` on Intel; amber at
    80 °C, red at 90 °C, with the highest since the panel started), how busy
    it is (overall and per thread), the load averages, the package power
    where the kernel lets the panel read it, and a plain note when the
    CPU runs well under its top speed while busy. Two small graphs show the
    last 5 minutes; the 1 h / 24 h graphs gain CPU temperature and speed. A
    reading the machine doesn't give (a virtual machine has no temperature)
    is simply left out.
  - **Open terminal** asks for your Linux user and password.
  - To look at the page without a server (development only):
    `python3 ollama1/tests/admin_demo.py 9910`, then open
    `http://127.0.0.1:9910/`. It runs the real panel on made-up data in a
    scratch folder, starts nothing (it prints each unit a button would
    start), refuses to run as root or under systemd, and is never
    installed (setup.sh copies `bin/` and `lib/` only).
- **Sleep.** The panel's **Sleep now** button (next to Reboot) suspends the
  server. Its power button does the same.
  - To wake it, press the power button again. Holding the button down
    still forces it off.
  - While it sleeps it can't be reached, and anything plugged into its
    second network port (a Raspberry Pi, say) loses its connection.
  - Sleep is refused while a model download or library sync, an update, or
    setup.sh is running. (A RAID resync, on a server that still has the old
    mirror, is allowed: the md driver pauses it and carries on after waking.)
  - During setup.sh, a model download or library sync, and updates, the
    power button does nothing at all: each holds a logind inhibitor
    (`sleep` and `handle-power-key`, mode block) until it ends, so the
    button can't suspend the server mid-job. The inhibitor goes with its
    job however the job ends, even if it is killed. Holding the button down
    still forces it off. `systemd-inhibit --list` shows who holds one.
  - After every wake a check runs. Ollama must answer (`/api/version`,
    `/api/ps`) and the GPU must report through sysfs. If not, it first waits
    (at most 90 s) for the card's driver and the models drive (`/srv/models`)
    to be back, then restarts Ollama and the tunnel, and once more (after
    another bounded wait for whatever was missing) if Ollama still doesn't
    answer. The record says why: how long it waited, what was still missing,
    how many restarts (`resume_check` in `/var/lib/ollama1/sleep.json`, shown
    on the dashboard and the panel). A server with no AMD card isn't held up
    waiting for one.
  - The dashboard and the panel show the last sleep and wake times, and the
    result of that check.
  - **Untested until someone tries it by hand.** AMD GPU compute (ROCm)
    after a suspend is a known weak spot.
  - **Auto sleep** (the app's Settings > Your servers) suspends the server
    after the minutes you pick with no real work (a request, a download or
    tool, load, a busy card). The setting is one small file,
    `/var/lib/ollama1-gateway/sleep.json` (mode 0600, written whole through a
    rename and flushed, never under `/run`), so it stays across a restart of
    the gateway or `ollama1-idle`, a reboot, and another run of `setup.sh`
    (setup never touches it, whatever its flags). The nightly backup copies it
    to `/var/backups/ollama1`. `sudo ollama1-idle status` prints the saved setting.
    Wake-on-LAN is set up by setup.sh for each card that can do it.
- **LAN mode** is off by default. With it on, the gateway also answers on
  `http://<server-ip>:8431` from the home LAN, without Access. Signatures
  are still required, and traffic on the LAN is unencrypted. To change it:
  `sudo ollama1-lan on|off`, or use the panel.

## Graphics card tuning

**Off unless you ask**: `sudo ./setup.sh --gpu-tune` (or `OLLAMA1_GPU_TUNE=1`)
turns it on, and the choice is saved in `/etc/ollama1/setup.env`, so a later
run of setup without the flag keeps it. It is for an AMD Navi 21 card
(RX 6800, 6800 XT, 6900 XT, 6950 XT); any other card is left alone, with a
one-line note. It runs a card past its stock limits, which is the owner's call
to make: read this section first. Writing an answer reads
the whole model from the card's memory for every word, so the memory clock
sets the pace; the power limit keeps the clocks up under load. **The aim is
about 5% faster token generation on models that fit the card. Only a
measurement on your card proves it**: `sudo ollama1-gpu-tune status` shows
the stock and tuned speeds it measured. Back to stock at any time:
`sudo ollama1-gpu-tune off`.

What it sets (`ollama1-gpu-tune`, `lib/o1gputune.py`), always within what the
driver reports for the card, which it finds by its PCI ids (not by `cardN`):

- **Power limit:** `power1_cap` = `power1_cap_max`, the most the driver
  allows this card (never more). On an RX 6900 XT that is roughly 15% above
  stock, up to about 330 W depending on the board. That means more power
  drawn, more heat and more fan noise under load; check that your power
  supply has the headroom.
- **Memory clock:** the top state in `pp_od_clk_voltage` raised by 100 in the
  driver's units (GDDR6 runs at twice that: a 6900 XT's stock 1000 is 2000 MHz
  effective), and never past the `OD_RANGE` limit the card reports (on many
  6900 XTs 1075, so 2150 MHz effective: +7.5%).
- Core clocks and voltages: untouched.

The memory clock needs the kernel's overdrive switch: setup adds only the
overdrive bit (`0x4000`) to the `amdgpu.ppfeaturemask` the driver runs with
now, in `/etc/default/grub.d/97-amdgpu-overdrive.cfg` (the recovery entry in
the boot menu boots without it). It takes a reboot, and on most Navi 21 cards
the higher power limit waits for it too (until then the driver allows no more
than stock). With overdrive on, the kernel says so in its log and marks
itself tainted; that is expected.

**Safety.** After the values are set, a check runs 60 seconds of answers on
the card with a model already installed, while it watches the kernel log for
amdgpu errors (a ring timeout, a GPU reset, a page fault) and the card's
temperatures. Any error, the junction at 105 C or the memory at 100 C puts
the card back to stock at once. Slow answers (more than 5% under the stock
speed measured when it was turned on, with the same model) are not enough on
their own: the check measures stock and tuned again, alternating twice, and
only a slowdown that repeats (each alternation and the medians) puts the card
back, with the figures in the reason. If anything made a reading unreliable,
the check changes nothing and runs again next boot: a drive logging NVMe
errors, another model loaded, the card already busy for someone else, an
answer that failed partway (`status` shows "deferred" and why). A revert
stays at stock, across reboots, until
you run `sudo ollama1-gpu-tune on`; `status` says why. Every boot also reads
the kernel log of the boot the tuning last ran in, and an amdgpu error there
does the same. With no model installed yet the check runs once one is
(`ollama1-gpu-tune-check.service`, after Ollama starts).

- `sudo ollama1-gpu-tune status`: stock and current values, temperatures,
  the last check.
- `sudo ollama1-gpu-tune on`: turn it on (or on again after a revert):
  measures stock, sets the values, runs the check.
- `sudo ollama1-gpu-tune off`: back to stock, and kept there. A re-run of
  setup keeps it off; `setup.sh --gpu-tune` turns it on.
- `sudo setup.sh --no-gpu-tune`: off, saved as off, and the GRUB drop-in
  removed (a reboot turns overdrive off).
- At boot `ollama1-gpu-tune.service` sets it before Ollama starts, and after
  a wake the sleep hook runs it again (amdgpu should keep the values across
  a suspend; it writes only what differs).

## Fans

**On unless you say `--fans off`**: `sudo ./setup.sh --fans off` (or
`OLLAMA1_FANS=0`) turns it off, and the choice is saved in
`/etc/ollama1/setup.env`. The aim is maximum cooling while the server works
without running the fans flat out for ever, which wears the bearings:
`ollama1-fan.service` (`bin/ollama1-fan`, `lib/o1fan.py`) holds

- the graphics card's fan (amdgpu `pwm1`), and
- every fan header the motherboard's Super-IO chip lets it control (on an MSI
  MEG X570 ACE, the NCT6797D, hwmon `nct6797`, `pwm1` to `pwm7`)

at a level that follows what the server is doing:

| Phase | Level | When |
|---|---|---|
| `working` | 100% | a request, a long job, the card or the processors say it is working |
| `hold100` | 100% | for 60 s after the work ends |
| `hold50` | 50% | for the next 60 s |
| `idle20` | 20% | from 120 s after the work ended, and from the start |

A new request at any time goes back to 100% and starts the sequence over.
A level is `pwm = round(percent * 255 / 100)`: 20% is 51, 50% is 128, 100%
is 255. While the service runs it always holds the outputs; whenever it
stops, for any reason, they go back to their own control (the BIOS's
automatic), and that is also what is in force at boot before it starts.

**Working** (`lib/o1work.py`, shared with the lights) is a request in flight (at once), a long job running (`stability-test.sh` and
the like, at once), the graphics card at 15% or more averaged over 6 s, or the
processors at 40% or more averaged over 10 s (`/proc/stat` user, nice, system,
irq and softirq over every field, so time waiting on a disk does not count).
Once the card or the processors made it work it stays working until the value
has been under its threshold for 6 s. The 1-minute **load average is not used**:
it counts tasks blocked on a disk (a RAID check, boot work, apt) and lags by a
minute, which kept the fans at 100% with nothing running. Status, the admin
line and the lights' status say which signal made it work ("a request is
running", "the card is 62% busy", "processors 71% busy", "running
stability-test.sh"). A
download, an update or a backup is not heat and does not count. Auto sleep keeps
its own idle rules, load average included.

**Temperature override.** Any of these at its limit forces 100% whatever the
load says, until it is 10 C under it; then the normal rule follows:

| Sensor | Limit |
|---|---|
| CPU (k10temp, Tctl) | 80 C |
| Graphics card: junction / memory / edge | 90 / 95 / 85 C |
| NVMe drives (composite) | 70 C |
| DIMMs (jc42) | 70 C |
| Motherboard chip (nct6797) inputs, by `temp*_label`: board, system, AUXTIN and any label it does not know | 70 C |
| CPUTIN, PECI, TSI | 85 C |
| a label with VRM or MOS | 90 C |
| a label with CHIPSET or PCH | 80 C |

An unknown label is watched with the 70 C default, not ignored. A sensor that
reads 0 C or less (an unplugged input reads -128), or 120 C or more, is
disconnected or stuck: it is ignored, and let go if it was hot, so it can
never hold the fans at 100%.

**Never stall a fan or starve a pump.** What is plugged into each header is
not known, so a low level is checked, never trusted:

- The first start measures each output's rpm at 100% (about 8 s with every
  fan at full speed) and remembers it in `/var/lib/ollama1/fan.json`.
- Six seconds after an output is set to a low level its rpm is read. 0, or
  under the output's own `fanN_min`, means it stalled: it is raised in steps
  of 10% (30, 40, 50 ...) to the lowest level that spins. That floor is
  remembered per output and logged once per step.
- An output still at 60% or more of its 100% rpm when asked for 20% is a
  probable pump or a fixed header: it is kept at 100% for good, and logged.
- An output that reads no rpm at 100% (nothing connected, or no reading) is
  set to 100% only while working, and left to its own control otherwise.

To forget what was learned (a fan was changed), stop the service, delete
`/var/lib/ollama1/fan.json` and start it again.

**Fail-safe.**
- Before the first change, each output's `pwmN_enable` (and its `pwmN`, if it
  was manual) is saved in `/var/lib/ollama1/fan.json` with the boot id.
  After a reboot the saved originals are dropped: the hardware is already as
  it was.
- A clean stop, **and any other exit** (a crash, a kill, the watchdog), puts
  every output back as it was: `ExecStopPost` always restores, and
  `Restart=always` (no start limit) starts the service again. A dead service
  never leaves a fan at 20%.
- Watchdog: the service pings systemd on every poll (`sd_notify` over
  `NOTIFY_SOCKET`, stdlib only; `Type=notify`, `WatchdogSec=10`). A loop that
  has not run for 10 s is restarted, and restored.
- An output the kit cannot write is skipped; setup and the service log what
  it controls once, like "controlling GPU fan + 7 case/CPU fan outputs".
- After a wake, amdgpu resets its fan to automatic: the sleep hook pokes the
  service (`SIGUSR1`), and every tick writes again any output it holds that
  has changed, at the current level.

**A Corsair liquid cooler (optional).** A Hydro Platinum, Pro XT or Elite
cooler (an H115i Platinum is USB `1b1c:0c17`) has its fans and its pump on
its own USB controller, so the motherboard's headers never reach them.
`liquidctl` does. Setup installs the `liquidctl` apt package only when the
fan step is on and a Corsair Hydro cooler is on USB (the plan line says so);
without liquidctl or without a cooler the service logs one line and carries on
with the case fans. The cooler follows the same phases:

| Phase | Cooler fans | Pump |
|---|---|---|
| `working`, temperature override, `hold100` | 100% | extreme |
| first start (measuring) | 100% | balanced |
| `hold50` | 50% | balanced |
| `idle20` | 20% (or higher for a fan that stalls) | quiet |

The pump is at extreme only while working and for the 60 s after, and when the
coolant is hot: a coolant at 40 C or more forces the pump to extreme and the
fans to 100% until it is under 35 C. A cooler fan is measured at 100% on the
first start and raised in steps if it stalls at a low level, like the case
fans; one that reads no rpm stays at 100%. The cooler's status is read at
most every 5 s (a USB transaction), a command is sent only when its target
changes and no more often than every 5 s, and every `liquidctl` call has a
4 s timeout and runs in a thread of its own so the watchdog is never starved.
Five failed calls in a row leave the cooler on its safe curve, log it, and
the case fans carry on.

**The cooler is safe without the service.** When the service stops for any
reason, `ExecStopPost` (using a marker in `/var/lib/ollama1/fan-aio.json`)
sets the pump to balanced and each fan to a coolant-temperature curve the
cooler follows by itself: 30% at 25 C, 60% at 35 C, 100% at 45 C. The
unit allows what `liquidctl` needs (USB, `AF_NETLINK`, a runtime folder,
`MemoryMax=192M` for a second Python); `ollama1-fan status` and the admin
line show the coolant, the pump mode and rpm, and the fans' rpm.

**The motherboard's chip.** The service loads `nct6775` (it covers the
NCT6797D) when no chip with fan outputs shows, before it starts, outside its
sandbox (`ExecStartPre=-+`, so a failure is never fatal). Setup adds
`/etc/modules-load.d/ollama1-fan.conf` only when the chip does load. If it
doesn't, the card's fan is still controlled, and `journalctl -u ollama1-fan`
says why. If the kernel log says `ACPI: resource ... conflicts with ACPI
region`, the board's firmware holds the chip: the kit does not change kernel
settings for it (the usual fix is `acpi_enforce_resources=lax` on the kernel
command line; that is your call). Do not run `fancontrol` or another fan
daemon beside this one.

**Status.** `ollama1-fan status` (no root) shows the level and the phase
(`working`, `hold100 42s`, `hold50 30s`, `idle20`), which outputs it
controls, each fan's setting, rpm and lowest level, the highest temperature
and the sensor closest to its limit (and every sensor with its limit). The
admin panel's CPU card shows the same in one line. Logs:
`journalctl -u ollama1-fan` (phases and counts only).

**The hand-pasted `ollama1-gpu-fan.service`** (full speed always) is replaced
by this. Setup removes it when it finds one, and gives the card's fan back
to the driver. To remove it by hand:
`sudo systemctl disable --now ollama1-gpu-fan.service && sudo rm /etc/systemd/system/ollama1-gpu-fan.service && sudo systemctl daemon-reload`
and then `echo 2 | sudo tee /sys/class/drm/card*/device/hwmon/hwmon*/pwm1_enable`
(2 is automatic).

## Lights

**Off unless you say `--leds on`**: `sudo ./setup.sh --leds on` (or
`OLLAMA1_LEDS=1`); `--leds off` stops and disables it again. The choice is
saved in `/etc/ollama1/setup.env`. `on` runs `apt-get -y -q install openrgb`
(the plan says so before you confirm). Every RGB device OpenRGB lists (on an
MSI MEG X570 ACE with a Corsair H115i Platinum: the board's Mystic Light and
the cooler's pump head) gets the same colour, on all its LEDs:

The colour follows **the graphics card's busy percent** (`gpu_busy_percent`, the
same reading the fans use, read every 0.25 s), as a ramp, piecewise linear in RGB:

| Card busy | Colour |
|---|---|
| 0% | white, 255,255,255 (full brightness) |
| 33% | yellow, 255,255,0 |
| 67% | orange, 255,128,0 |
| 100% | red, 255,0,0 |

(50% is a golden yellow-orange, 255,192,0.) The colour on show is
**slew-limited**: it rises at most 100% per 2.5 s and falls at most 100% per
3.5 s, so 0% to 100% takes about 2.5 s (plus up to one 0.25 s sample to notice),
100% to 0% takes 3.5 s, and the short 0% gaps between batches of work only dip
it a little. There is no hold and no state: only the card's load colours the
lights, not a request in flight or a running tool (a long prompt-reading phase
with the card only partly busy shows as a weak colour; that is as intended). With
no card reading (no card, or the read failed) the intensity is 0, so white.
A frame goes out only when the rounded colour changes (at most 20 a second),
and every 2 s the connection is looked at and the colour sent again (a
keepalive: a device that was reset gets it back). The 6b395 rule (white idle,
red while "working", a 3 s hold, 0.8 s and 2 s fades) is gone.

**How.** Two units. `ollama1-openrgb.service` runs `openrgb --server` with no
window, bound to `127.0.0.1` (`--server-host` when this build has it, and
always an address filter: `IPAddressDeny=any`, `IPAddressAllow=localhost`).
`ollama1-leds.service` speaks OpenRGB's SDK network protocol to it with the
standard library (`lib/o1leds.py`; the protocol version is negotiated, capped
at 4), so a fade costs no process per frame. Each device is put in Direct mode
(else Custom, else Static) and, where its mode has a brightness, at its
maximum. If the server is gone the service says so in its status and tries
again with a pause that grows to 30 s; it does not exit.

**Why root, and what it can reach.** The OpenRGB server opens the lights'
USB `hidraw` nodes (root-only), so it runs as root, but its device policy
allows only `hidraw` and USB nodes: the I2C/SMBus scans OpenRGB can do for
memory modules and graphics cards (which can write to the wrong chip) find no
`/dev/i2c-*` to open, so DIMM sensors and the card are not touched. It has no
network but the loopback. The lights service has no device access and no
network but the loopback; it reads what the fan service reads and writes only
`/run/ollama1/leds.json`. If you want OpenRGB to drive more than USB lights,
that is your call, not the kit's.

**A stop** (including `--leds off`) sets the lights white first, so they are
never left red; after that they are as the board leaves them. After a wake the
sleep hook restarts the OpenRGB server (the USB devices may have come back new)
and pokes the service (`SIGUSR1`), which connects afresh and sets the colour at
once. The lights are not turned off for sleep: the board decides.

**Status.** `ollama1-leds status` (no root): the colour name, the colour now and its target, the card's
load and the intensity shown, the devices found and their mode, and any error. The admin panel's CPU card shows one line, "Lights: ...". Logs:
`journalctl -u ollama1-leds -u ollama1-openrgb` (states and counts only).

## Hardware watchdog

**On unless you say `--watchdog off`**: `sudo ./setup.sh --watchdog on|off` (or
`OLLAMA1_WATCHDOG=1|0`; saved in `/etc/ollama1/setup.env`). It answers one
failure: the system NVMe drops off the bus, the machine stays up from memory (it
pings, nothing on disk can be read, every program gives "Input/output error",
SSH resets) and only a power cycle brings it back. The chipset's own timer does
that power cycle by itself: `ollama1-watchdog.service` arms it with a timeout of
60 s (30 to 90 s if the chip limits it) and pets it every 10 s, but **only while
a real health probe passes**.

**Hardware only.** The driver is `sp5100_tco` (the AMD chipset's timer; on an
MSI MEG X570 ACE it is the processor's own), else `wdat_wdt` (a timer described
by ACPI). `softdog` is never used: it is software, and a frozen kernel cannot
run it. A watchdog that calls itself "Software Watchdog" is skipped. The service
loads the driver best effort from `ExecStartPre` (a `+` line, outside its
sandbox); setup also writes `/etc/modules-load.d/ollama1-watchdog.conf`, and
only when `/dev/watchdog0` really appeared.

**The probe** (every 10 s, in a thread that is given 8 s, so a read stuck on a
dead drive cannot stop the loop):

| Check | What it does |
|---|---|
| `token` | writes a small token to `/var/lib/ollama1/watchdog.token`, fsyncs it, drops it from the page cache and reads it back |
| `models` | when something is mounted at `/srv/models`: stats one of its entries and reads 4 KB of it (nothing mounted: skipped) |
| `rootread` | reads 4 KB of a real file on the root filesystem (the Python program), at a different place each time, after `posix_fadvise(DONTNEED)`, so it is not from the cache |

**Two probes in a row** that fail or time out (about 20 s) and the service
**stops petting and does not close the device**, for good until it is restarted
(a machine whose disk answers again is still reset: it has been dead). The board
resets within the timeout, 60 s after the last pet. What failed goes to the
journal (best effort: the journal may be on the dead drive) and to
`/run/ollama1/watchdog.json` (a tmpfs: it is there until the reset). A machine
that is only slow is not tripped: a probe has 8 s, two must fail one after the
other, and a load of 20 or more does not do it (`test_watchdog.py` runs 24 busy
processes). During a heavy copy (`migrate-os`) a probe can in principle take
longer than 8 s twice; if that ever happens, `sudo systemctl stop
ollama1-watchdog` for the duration (a clean stop disarms it) and start it again.

**How it is closed.** Writing the magic character `V` and closing disarms the
timer; closing any other way leaves it running. `V` is written in exactly two
places: a deliberate `systemctl stop` of a healthy service, and the pause before
sleep. A crash, a kill, a stop while a probe is failing, the unit's own
`WatchdogSec=30`: the timer stays armed and `Restart=always` starts the service
again within 2 s. The unit has no `ExecStopPost`, no memory or CPU limit,
`OOMScoreAdjust=-900`, `ProtectSystem=strict` (it writes only `/run/ollama1` and
`/var/lib/ollama1`), no network, and no `PrivateDevices`/`ProtectClock`/
`DeviceAllow` (any of them would hide `/dev/watchdog0`).

**Sleep.** The sleep hook runs `ollama1-watchdog pause` before suspend (the
service closes the device with `V` and the hook waits up to 5 s until the status
says so) and `ollama1-watchdog resume` first thing after the wake: the service
reopens the device when the first probe passes (the drive may take a moment to
come back), or 90 s after the wake at the latest, so a drive that never comes
back still ends in a reset. A machine that slept without the hook (the clock
that counts sleep runs ahead of the one that does not) has its failure count
cleared. A pause while the watchdog has already tripped is ignored.

**Status.** `ollama1-watchdog status` (no root; exit 1 when it is not healthy):
the device and its driver, the timeout, whether it is petting and when it last
did, the last probe with each check and its time, the failure count, and why it
stopped if it did. `sudo ollama1-watchdog probe` runs one probe now and prints it.

**In the BIOS.** The board may have a watchdog of its own, or leave the
chipset's timer off, and both matter:

- *MSI:* Settings, Advanced, **Integrated Peripherals** (some versions have a
  **Watchdog** or **Watch Dog Timer** entry under Advanced). Leave the BIOS's own
  watchdog **Disabled**: if the BIOS arms the timer first and keeps it running,
  the kit's `ollama1-watchdog` still works (it sets the timeout when it opens the
  device), but a BIOS timer with a short fixed timeout can reset the machine
  before Linux boots. If `sudo modprobe sp5100_tco` finds no device, look for an
  option such as "TCO timer" or "AMD fTPM/PSP watchdog" and enable it, and read
  `journalctl -k | grep -i tco`.
- *MSI:* Settings, Advanced, Power Management Setup: set
  **Restore after AC power loss** to `Power On`, so a remote power cycle (a smart
  plug off and on) brings the machine back by itself instead of waiting for the
  power button.
- systemd can use the same timer (`RuntimeWatchdogSec` in `/etc/systemd/system.conf`):
  leave that off, only one program can hold `/dev/watchdog0` (the service says
  "held by another program" when it cannot).

**What the kit cannot know from here.** That your board's `sp5100_tco` really
starts the timer and that the board resets (rather than only logging) on this
firmware. Test it once, on purpose, with nothing running: stop the service
(a clean stop, it disarms), open the device from a shell and never pet it:
`sudo sh -c 'systemctl stop ollama1-watchdog && exec 3>/dev/watchdog0 && sleep 300'`.
The board must reset about 60 s later (the timeout in `ollama1-watchdog status`);
the service starts again at boot. Killing the service does not test it: it
restarts in 2 s and pets again.

## Removing the old RAID mirror

Since 6b400 the kit builds no RAID: a mirror of two big disks only ties up the
machine with checks and rebuilds, and nothing needs it. A server that was set up
with one (the array at `/srv/data`, `/dev/md/o1data`) keeps working until you
take it off, and `setup.sh` no longer looks at it. `tools/remove-raid.sh` is the
teardown, and it is careful:

```bash
sudo bash ollama1/tools/remove-raid.sh                        # a DRY RUN: what is on the array, what it would do. Changes nothing
sudo bash ollama1/tools/remove-raid.sh --yes-erase-the-mirror # does it, after you type  yes  on the terminal
```

- **Which disks.** The two old RAID disks, by serial: `HDD1_SERIAL`/`HDD2_SERIAL`
  from `/etc/ollama1/setup.env`, or `--hdd1-serial`/`--hdd2-serial`. The array's
  members must be **exactly** those two disks, or it refuses. It refuses any
  NVMe drive, and the OS and models serials from `setup.env`.
- **The dry run prints** the array and its members (with serials), what is on it
  (the top level of `/srv/data`, with sizes), the mount, the fstab line, the
  `mdadm.conf` line, and the nine steps it would take, in order.
- **It refuses** when `/srv/data` holds anything but what the kit put there
  (`backups`, `models-parked`, `o1migrate`, the migrate-os status and log, the undo
  note, `lost+found`) and when a system move (`migrate-os`) is not finished (its
  state is on the array). The unknown names are printed; `--keep-going` goes on
  anyway. It will not run without a terminal: `--yes-erase-the-mirror` still asks
  you to type `yes` on `/dev/tty`.
- **What it does:** stops the backup units; copies the settings backups to
  `/var/backups/ollama1` (when under 256 MiB); unmounts `/srv/data`; `mdadm --stop`;
  `mdadm --zero-superblock` on each member (after reading the disk's serial
  again); comments out the `/srv/data` line of `/etc/fstab` (the line stays, as a
  comment; a timestamped copy of fstab is kept); removes the array's `ARRAY` line
  from `/etc/mdadm/mdadm.conf` (copy kept); turns off the monthly array check
  (`mdcheck_start.timer`, `mdcheck_continue.timer`, the `/etc/cron.d/mdadm` job)
  unless another array is in `mdadm.conf`; `update-initramfs -u`. Then it checks
  each result and says what is true. The log is `/var/log/ollama1-remove-raid.log`.
- **What it leaves:** the two disks keep their partition tables and partitions
  (only the RAID superblocks go). It prints the `wipefs` line for later and does
  not run it. The empty `/srv/data` folder stays; `rmdir` it if you like.
- **What is lost:** everything on the array. The parked models copy
  (`models-parked`) goes with it. Copy anything you want first.

Where the things that lived on `/srv/data` are now (one place for each):

| What | Now | Where it is defined |
|---|---|---|
| nightly settings backup | `/var/backups/ollama1/<date>` (30 days; skipped under 2 GiB free or over 256 MiB) | `o1common.Paths.backups`, `BACKUP_*`; `setuplib.sh` `BACKUP_DIR` |
| migrate-os state, status and log | `/var/lib/ollama1/o1migrate/`, `/var/lib/ollama1/migrate-os.{status,log}` on the root filesystem; copied onto the new root at the end | `o1migrate.DATA_DIR` (= `Paths.state`) |
| migrate-os parked models | `/var/lib/ollama1/models-parked` by default, or `<folder>/models-parked` with `--park-dir <folder on another disk>` | `o1migrate.Cfg.parked` |

## Power and electricity cost

The panel's **Power and cost** card shows what the server draws now (with
an hour's chart), the energy and cost over the last hour, 24 hours, 7 days
and 30 days, a 30-day projection, and the electricity cost per million tokens.
The console dashboard's Health panel has a one-line version. A root
sampler, `ollama1-power.service`, takes a reading every 10 seconds.

**Where the watts come from.** A smart plug is best: it measures the
whole server at the wall. Without one, the kit estimates.

- **Smart plug**, on the home LAN only: its address must be an IP in
  10/8, 172.16/12, 192.168/16 or fc00::/7 (not link-local), and the
  sampler's unit can't reach anything else. Shelly Gen1 logs in with HTTP
  Basic only, Gen2+ with Digest only. Each reading has 3 seconds in all;
  a plug that doesn't answer properly means the estimate is used. The login, if the
  plug has one, is kept in `/etc/ollama1/power-plug.json`, readable by root
  only.

  ```bash
  sudo ollama1-power set-plug shelly2 <plug-ip>               # Shelly Plus / Pro / Gen3
  sudo ollama1-power set-plug shelly1 <plug-ip> --user admin  # Shelly Gen1 (Plug S), asks the password
  sudo ollama1-power set-plug kasa <plug-ip>                  # TP-Link Kasa HS110 / KP115
  sudo ollama1-power set-plug tasmota <plug-ip>               # Tasmota
  sudo ollama1-power set-plug none                            # back to the estimate
  ```

  It reads the plug once before saving. Newer Kasa firmware encrypts its
  local protocol differently and isn't supported. If the plug stops
  answering, the estimate takes over and the panel says why.
- **The estimate**: GPU power (amdgpu's own reading) plus CPU package power
  (the RAPL energy counter; its wraparound is handled) plus 40 W for
  everything else, divided by 90% power-supply efficiency. It is always
  labelled "estimate". `power_baseline_w` and `power_psu_efficiency` in
  config.json change the 40 W and the 90%.
- **Asleep** counts as 3 W, for the time between the sleep and wake stamps.
- **Any other gap is unknown**, never zero: the table shows the unknown
  hours, and the totals leave them out.

**What's kept**: energy per minute in `/var/lib/ollama1/energy/`
(`m-YYYY-MM-DD.csv`, by UTC day: the minute, Wh, the source, the seconds
measured, and the tokens generated in it), for 400 days. After that, each
day's totals go to `days.csv`. Nothing about a request is kept but its
token count.

**Prices.** The default is a flat rate with no price set, so there are no
costs until you set one. They are kept in `/var/lib/ollama1/tariff.json`
(root's, readable by the panel). Set them in the panel (**Prices**: the
panel checks them, leaves them in its own folder and starts
`ollama1-power-apply.service`, which reads that file only if it is a
regular file with one link, owned by the panel's user, without following a
link, checks it again and writes root's; it never writes or deletes
anything in the panel's folder), or from a file:

```bash
sudo ollama1-power set-schedule prices.json                     # checked first; nothing changes on an error
sudo ollama1-power export-schedule prices.json                  # the prices in use
sudo ollama1-power export-schedule --preset weekdays-4pm-9pm prices.json
sudo ollama1-power status
```

The panel's **Export JSON** and **Import JSON** use the same file. A
time-of-use schedule looks like this (made-up prices and times; take
yours from your bill):

```json
{
  "currency": "USD",
  "mode": "tou",
  "flat_rate": null,
  "timezone": "America/New_York",
  "tiers": {"on": 0.30, "mid": null, "off": 0.11, "discount": 0.07},
  "weekends_off_peak": true,
  "holidays": {"enabled": true, "observed": true,
               "names": ["new_year", "memorial", "independence", "labor", "thanksgiving", "christmas"],
               "extra": ["2026-12-24"]},
  "seasons": [
    {"name": "Summer", "from": "06-01", "to": "09-30",
     "windows": [{"tier": "on", "days": "weekdays", "start": "16:00", "end": "21:00"},
                 {"tier": "discount", "days": "every day", "start": "00:00", "end": "05:00"}]},
    {"name": "Winter", "from": "10-01", "to": "05-31",
     "windows": [{"tier": "on", "days": "weekdays", "start": "07:00", "end": "10:00"},
                 {"tier": "on", "days": "weekdays", "start": "17:00", "end": "20:00"},
                 {"tier": "discount", "days": "every day", "start": "00:00", "end": "05:00"}]}
  ]
}
```

- `mode` is `"flat"` (then `flat_rate` is the price per kWh) or `"tou"`.
- Tiers: `on`, `mid`, `off` and `discount`, each a price per kWh, or
  `null` when unused. Time-of-use needs a price for off-peak and for every
  tier a window uses.
- `days`: `"weekdays"`, `"weekends"`, `"every day"`, or a list such as
  `["mon", "wed", "fri"]`. `start` and `end` are `HH:MM` in the schedule's
  time zone, daylight saving included. `"24:00"` means midnight.
- A window that ends at or before its start runs past midnight. It belongs
  to the day, and the season, it starts on.
- **Where windows overlap, the most specific wins**: the one on fewer days,
  then the shorter one. Time no window covers is off-peak.
- `weekends_off_peak`: on Saturdays and Sundays only off-peak and discount
  windows apply, so a weekend discount still counts.
- Holidays count as weekends. `names` picks from the US federal holidays
  (`new_year`, `mlk`, `presidents`, `memorial`, `juneteenth`,
  `independence`, `labor`, `columbus`, `veterans`, `thanksgiving`,
  `christmas`), plus `day_after_thanksgiving`. `observed` moves one that
  falls on a Saturday to Friday, and one on a Sunday to Monday. `extra`
  adds dates.
- Seasons use `MM-DD` dates, may wrap the new year, and must not overlap.
  A day in no season is off-peak all day.
- The panel's presets ("4-9 pm weekdays", "2-7 pm weekdays", "8 am-8 pm
  weekdays") are generic shapes, not any utility's schedule. They are
  typical: verify against your bill.

**How the money is worked out.** Each minute's energy is priced at that
minute's tier, so a window that starts at 19:00 splits the costs at 19:00
exactly. The table splits kWh and cost by tier, and the badge says what
applies now ("on-peak until 21:00"). The 30-day projection takes the
average power in each hour of the week over the last 28 days, and prices
each future minute at its tier. Cost per million tokens (shown in whole cents) is all the
electricity over the last 30 days divided by the tokens generated then, so
idle time is included.

## Moving the system to the other drive

For a server whose OS drive keeps dropping off the PCIe bus under load (the
kernel logs `nvme controller is down; CSTS=0xffffffff`, then the root
filesystem goes read-only) while the models drive, on the other slot, is
healthy. `tools/migrate-os.sh` moves the operating system onto the models
drive, in software, with no drive swapping, and keeps the old drive exactly as
it was, bootable, until the new system has booted and been checked.

**What it does, and why it copies.** The OS drive holds LVM (`ubuntu-vg`). The
obvious way, `vgextend` and `pvmove`, was rejected: it rewrites the volume
group's metadata *on the old drive* while it runs, moves every extent of `/`
off it (the old drive would boot only while the new one is also there, and no
longer holds the system as it was), and a drive that drops off the bus half way
leaves a volume group with a missing disk. So the old drive's partitions, LVM, boot loader and `fstab` are never written to
(its root filesystem does get this tool's state and log, and the parked models, below; since 6b400 there is no RAID mirror to put them on).
`/` and `/boot` are copied file by file (`rsync -aHAXx`) onto plain ext4
partitions of the other drive, with new UUIDs; the copy gets its own
`/etc/fstab` (the old one is not touched), initramfs and GRUB, built in a
chroot; and one firmware boot entry is added for the NEW drive, used for the
next boot only (UEFI `BootNext`), while the old drive's entry stays first in the
boot order. The new root is a plain partition, not LVM. Only `--finish`, once
the new system has booted and been checked, makes the new drive the default;
until then a power cycle, or a kernel panic (the new system boots with
`panic=10`), brings the machine back to the old drive.

The other drive is **erased and re-partitioned**. Its models are parked first, on
the old drive's root filesystem (`/var/lib/ollama1/models-parked`; or `<folder>/models-parked` with `--park-dir <folder>`, on another disk, when `/` has no room: never on the
drive being erased), checked by an `rsync -n` dry run and by size, and copied back
at the end:

| | | |
|---|---|---|
| 1 | ESP | 1 GiB vfat (a new boot loader; the old ESP is not copied, its stub points at the old `/boot`) |
| 2 | `/boot` | 2 GiB ext4 |
| 3 | `/` | `--root-size` (default 300G) ext4. It must be at least 20% more than `/` uses now, and at least 30G |
| 4 | models | the rest, ext4, label `o1models`, mounted at `/srv/models` |

The models partition ends up smaller than the old one by the size of `/`; the
tool refuses, before touching anything, if the models would not fit.

**Drives are found by serial**, never by `nvme0`/`nvme1` (those swap between
boots): `lsblk -d -o NAME,SIZE,MODEL,SERIAL`. Everything is addressed through
`/dev/disk/by-id`, and the serial is read again from sysfs immediately before
each command that writes to a drive; any mismatch stops the run. `FROM` is the
drive the system runs from now (read, never written); `TO` is the drive that is
erased.

### Install it (once, with your password)

The tool refuses to run as root from a folder anyone but root can write to, so
first it is installed root-owned:

```bash
cd ~/concordeai && git pull
sudo bash ollama1/tools/migrate-os.sh --install-remote
```

This copies `migrate-os.sh` (0755) and `o1migrate.py` (0644) to
`/usr/local/lib/ollama1-migrate/` (root:root) and checks that no folder or file
on the way is writable by anyone else. Everything below runs that copy:

```bash
M=/usr/local/lib/ollama1-migrate/migrate-os.sh
```

### Run it

```bash
sudo $M --from-serial <os-serial> --to-serial <models-serial>         # 1. the plan; changes NOTHING
sudo $M --from-serial <os-serial> --to-serial <models-serial> --run --reboot   # 2. do it
```

The plan says what is read, backed up, erased and written, and lists the
stages. `--run` then asks you to **type the serial of the drive to be erased**
(or take `--confirm-serial <models-serial>`, which must equal `--to-serial`;
then no terminal is needed). After that nothing more is asked: the tool starts
itself again in a detached **tmux** session named `migrate`, so a dropped SSH
connection or a closed laptop can't stop it, and prints how to watch it.
`--reboot` reboots into the new drive at the end, after a 10-second countdown
that Ctrl-C cancels, and only if every stage and check passed. Without it the
tool stops and says so, and (when run in a terminal, not detached) asks
`Reboot into the new drive now? [y/N]` (the default is no). Default mode is
`--plan`; `--bwlimit KiB/s` (default 200000) keeps the copy gentle on a drive
that drops out under load.

The services that use the models stop for the duration (`ollama`,
`ollama1-gateway`, `ollama1-admin`, any `ollama1-pull@*` instance,
`ollama1-restart` and `ollama1-reboot` with their timers, `comfyui` if present,
the model sync and update timers, and apt's timers), so the server is out of
service until the reboot: plan for hours if the models are large. They start
again by themselves at the next boot. While `/srv/models` is unmounted, its
bare folder on the old root is marked immutable (`chattr +i`), so nothing can
write models into it. The mark stays on the old root (so a fallback boot of the
old drive is protected too) and is never put on the new root's folder; `--finish`
takes it off only if the old root happens to be mounted where it can reach it,
otherwise `sudo chattr -i /srv/models` on the old drive does, if you ever boot or
reuse it (mounting something on the folder works either way).
While it runs it holds a logind block on sleep and the power button, so the
kit's auto sleep can't suspend the server in the middle of a copy.

### Watch it

```bash
cat /var/lib/ollama1/migrate-os.status      # anyone can read it: state, stage N of 6, percent, what it is doing, times, next step
bash $M --status                     # the same, plus a diagnosis if it was interrupted (no sudo needed)
sudo tmux attach -t migrate          # the live screen; Ctrl-b then d leaves it running
sudo less /var/lib/ollama1/migrate-os.log   # the whole log (root only)
```

The state, status and log are in `/var/lib/ollama1` on the old drive's root filesystem (never on the drive being erased); at the end they are copied onto the new root, so `--finish` and `--status` find them after the reboot. The status file is rewritten at least every
15 seconds while it runs, and ends in `DONE` or `FAILED in stage <name>` with the
next step. It holds only fixed phrases, stage names and numbers: no serials, no
paths, none of a command's output (that is in the root-only log, with the
reason). A stage that is part-way through is picked up where it stopped.

### What each stage does and prints

| Stage | What it does |
|---|---|
| 0 checks | Both serials found (each by exactly one controller, native-multipath names understood) and different; `/` is on FROM and FROM has no mounted filesystem but `/`, `/boot`, `/boot/efi` and swap (`rsync -x` would silently skip another); `/srv/models` is mounted from a partition of TO, TO has exactly that one partition, no LVM/RAID, nothing else mounted; both drives report no critical warning (`nvme smart-log`); FROM is not dead right now (controller state, read-only `/`, the kernel log); UEFI boot; the data folder (`/var/lib/ollama1`) is on the root filesystem and, with the state folder, belongs to root and nobody else can write them; the parked folder is on a filesystem mounted read-write that is not the TO drive, with room for the models (a margin of 10 GiB and 2%); the root and models fit; `apt` is idle. Anything wrong is listed and nothing is changed. Then the model services stop |
| 1 park | `/srv/models` to `/var/lib/ollama1/models-parked` (or `--park-dir`); the `rsync -n --itemize-changes` dry run must find nothing left and the sizes must agree, or nothing is erased |
| 2 partition | Refused unless stage 1 is verified. Immediately before the wipe it looks again: the parked folder on a read-write filesystem; the page cache is flushed and dropped (`sync`, `drop_caches`) so the compare reads the disks; the dry run and sizes again; and a content compare of every parked file up to 64 MiB and of 18 MiB spread over each larger one (not a full checksum: reading a terabyte or more twice over disks of 150 MB/s takes hours, and sizes and times of everything are already exact). Then `/srv/models` is unmounted and marked immutable, the TO drive is wiped (`wipefs`, `sgdisk --zap-all`), partitioned, **each new partition is wiped again** (an old filesystem's signature can sit exactly where a new partition starts: the old models partition and the new ESP both begin at 1 MiB), then always formatted and read back |
| 3 copy | `/` (two passes: the second catches what changed) and `/boot`. Each destination is checked to be the new partition before anything is copied into it. `/swap.img` is made new, not copied |
| 4 boot | `/etc/fstab` of the **copy** rewritten (`/`, `/boot`, `/boot/efi`, `/srv/models` by their new UUIDs; swap and every other line, a `/srv/data` line of an old mirror included, kept); `update-initramfs`, `grub-install --no-nvram`, `update-grub` in a chroot (`/dev`, `/sys`, `/run` bound as slaves so unmounting never reaches the host, and unmounted again even if a command fails). The copy gets `panic=10` (a drop-in, `98-ollama1-migrate.cfg`), so a kernel panic reboots into the default boot, the old drive. The copy's `grub.cfg` must carry every kernel option the running system has (the NVMe settings, and the GPU overdrive switch if the tuning is on) and `panic=10`, the kit's GRUB drop-ins must be on the copy, and `EFI/o1new/grub.cfg` must exist next to the loader |
| 5 restore | The models are copied back to the new models partition and checked; the parked copy stays. The parked folder is excluded from the copy of `/`. Afterwards the state, a final status and the log are copied onto the new root (`/var/lib/ollama1`) |
| 6 firmware | The boot entry is made **last**. The old drive's entry (found by its ESP) stays first in the boot order, the new entry goes last, and `efibootmgr -n <new>` (BootNext) sends the next boot, once, to the new drive. Until `--finish` the old drive is the default boot |

When all of it has passed the tool prints a one-screen summary and, with
`--reboot`, reboots.

### After the reboot

```bash
findmnt /                        # the source must be a partition of the NEW drive, not ubuntu--vg
sudo $M --finish            # no serials needed; they are in the state file
```

`--finish` verifies that `/`, `/boot`, `/boot/efi` and `/srv/models` are on the
TO drive, and that the running kernel command line has the options the old
system ran with (the NVMe power settings, `amdgpu.ppfeaturemask` when the GPU
tuning is on) and `panic=10`. Only then does it make the new drive the default
boot (`efibootmgr -o`: new first, the old drive's entry second). It tells you that the old drive's partitions, LVM, `fstab` and boot loader were never written to and how to
reuse it (that is yours to do, later; the tool never wipes a drive). Options:
`--delete-parked` (frees the parked models after checking the live copy has
every file; you type `delete`. After the reboot the parked copy is on the OLD drive's
root filesystem, not mounted now, so there is usually nothing to delete here: it goes
when that drive is reused. With a `--park-dir` disk that is mounted, it works), `--disable-old-entry` (makes the old drive's
firmware entry inactive, not deleted; you type `yes`), `--remove-sudoers`, and
`--disable-old-boot-files` (**off by default**, below).

**setup.sh after the move.** Afterwards the system (plain partitions, `/` on
the third, no LVM) and `/srv/models` are on the one new drive, and `--finish`
rewrites `/etc/ollama1/setup.env` so `OS_SERIAL` and `MODELS_SERIAL` both name
it (atomically, every other line kept). `setup.sh` then accepts the layout
"models on the OS disk": only when `/srv/models` is mounted from a partition of
that disk (its own partition, not the root filesystem), and then nothing on the
disk is wiped, repartitioned or reformatted; the plan says "models: on the OS
disk, already set up". If `/srv/models` is not mounted from it, setup stops
rather than touch the OS disk. The root-volume growth step is skipped when `/`
is not on LVM. For a server moved before `--finish` did this, or by hand, one
line does it, setup.env keeping the rest:
`sudo ./setup.sh --os-serial <new-serial> --models-serial <new-serial>`.
The refusal "/ is not on the disk with serial ..." for a stale `OS_SERIAL`
stays, and now names that line when `/` and `/srv/models` are both on the
models disk.

**The firmware keeps booting the old drive.** Some boards re-order `BootOrder`
after the move and pick the old drive again. `--finish --disable-old-boot-files`
renames `EFI/ubuntu` and `EFI/BOOT` on the old drive's EFI partition to
`ubuntu.off` and `BOOT.off`, so there is nothing to boot there and the firmware
falls through to the new drive. It is the one step that writes to the old drive
(only its EFI partition, found by the PARTUUID recorded at the start, never
guessed, mounted only for this), so the old drive stops being a fallback until
you undo it. You type `yes`; the way back is written to
`/var/lib/ollama1/old-drive-boot-files-undo.txt` before the first rename (mount the
partition, `mv` the two folders back). It refuses if a `.off` folder is already
in the way, and repeating it changes nothing. It does not change the firmware
entries (`--disable-old-entry` does that).

The firmware stage reads `efibootmgr -v` in its several shapes (a tab or only
spaces after the label, entries with and without the `*`, a `File(...)` loader
or a bare path with trailing data such as `...BOOTX64.EFI0000424f`, the same
entry listed twice under two numbers). An entry for the new drive's EFI
partition and loader that is already there (an earlier run stopped after
making it) is reused, and made active if it was not, instead of making another
or stopping with "efibootmgr made no entry"; if no entry for the new drive can
be found after making one, the message says how many entries the list has and
what is on that partition.

### If something goes wrong

- **It stops with FAILED**: nothing was rebooted and the old drive is
  untouched. Fix what it says (if the drive dropped off the bus, power the
  server off at the switch for a minute first), then
  `sudo $M --resume --reboot`. Finished stages are not repeated.
- **The machine restarted or crashed mid-run**: the status file still says
  `RUNNING` with a stale time; `bash $M --status` says `INTERRUPTED` and what to
  run. The old drive is the default boot unless stage 6 had finished, so the
  server comes back as it was. (A boot-time unit would have to be installed on
  the old drive, which this tool never writes.) `--resume` asks for the serial
  again, or takes `--confirm-serial`.
- **The new drive does not boot**: BootNext was used up by that one try, so a
  power cycle (or a kernel panic, `panic=10`) comes back to the old drive, which
  is still first in the boot order. Or press the boot-menu key (F11 on an MSI
  board) and pick a drive. Run `--finish` only after a boot you have checked.
  BootNext is single use: any boot between stage 6 and the planned reboot (a
  power cut, a failed first boot that fell back) consumes it. `--resume --reboot`
  checks the entries and the order (new entry present, old first) and, if they
  are right, sets BootNext again (`efibootmgr -n`) and goes on; it stops only if
  the entries themselves are wrong.
- **Rollback, after booting the old drive**: nothing on it was changed. Its
  `fstab` still has the line for `/srv/models` with the UUID of the *old*
  models partition, which was wiped and re-made, so that mount fails (it has
  `nofail`, so the boot goes on) and `/srv/models` is an empty folder, so Ollama
  sees no models. Either put them back by hand: `sudo mount LABEL=o1models
  /srv/models` (the new models partition, once stage 5 finished; before that, `sudo mount
  --bind /var/lib/ollama1/models-parked /srv/models`), and, to make it stick, change that fstab
  line to `LABEL=o1models` (the one change you may want on the old drive; the
  bare folder is immutable, which does not stop a mount, only writes into it); or
  just re-download the models, or point Ollama at `/var/lib/ollama1/models-parked`. To
  drop the new entry from the firmware: `sudo efibootmgr` to see its number (label `ollama1-new`), then
  `sudo efibootmgr -B -b <number>`.
- **Not rebooted into the new drive?** `--finish` says which of `/`, `/boot`,
  `/boot/efi` and `/srv/models` are still on the old one.

### Letting someone without root run it (temporary)

An account without root can run exactly this tool, as root, without a password,
through one sudoers rule, installed from a root session:

```bash
sudo bash ollama1/tools/migrate-os.sh --install-remote --sudoers-user <admin-user>
```

That installs the root-owned copy (as above), then writes the drop-in to a
temporary file, checks it with `visudo -cf`, installs it `0440 root:root` as
`/etc/sudoers.d/90-ollama1-migrate` and checks the whole sudoers set (undoing it
if that fails). Its entire content:

```
# ollama1 migrate-os: lets <admin-user> run the system-migration tool as root, with no password, and nothing else.
# It is temporary: remove it when the migration is done:  sudo rm /etc/sudoers.d/90-ollama1-migrate
<admin-user> ALL=(root) NOPASSWD: /usr/local/lib/ollama1-migrate/migrate-os.sh
```

The same by hand: write those three lines to a file in a private folder
(`mktemp -d`), `visudo -cf <file>`, `install -o root -g root -m 0440 <file>
/etc/sudoers.d/90-ollama1-migrate`. The user then runs
`sudo /usr/local/lib/ollama1-migrate/migrate-os.sh <arguments>` (always the
full path: no wildcard, no `SETENV`, `secure_path` unchanged). The script and
its library are root-owned, and as root it refuses to run from anywhere that is
not (every folder and file on the way owned by root, none writable by group or
others). `--print-sudoers --sudoers-user <name>` prints the text.

**Remove the rule when the migration is done:**

```bash
sudo rm /etc/sudoers.d/90-ollama1-migrate
```

`--finish` reminds you of it and, run in a terminal as root, offers to remove it
(`--remove-sudoers` does it without asking). While it exists, that account can
run the tool with any arguments (which is as far as the tool's own checks let
it go: a drive of a serial it names, found by sysfs, passing the checks above,
confirmed with `--confirm-serial`).

### After the move: re-running `setup.sh`

`setup.sh` (not changed by this) still assumes `/` is an LVM volume
(`ubuntu-vg`) on the OS-serial disk and the models are on a separate disk. On a
migrated system a re-run stops at its first checks (`/ is not on the disk with
serial ...`, or, with the new serial, `... is the OS disk`) before changing
anything. Until `setup.sh` learns the one-NVMe layout, don't re-run it, and
note `--encrypted-swap` (an LVM volume in `ubuntu-vg`) and `--vg-reserve` do
not apply. The services, timers, tunnel and config keep working: they live in
`/etc` and `/var/lib`, which were copied.


## The rules, and where they're enforced

| Rule | Where |
|---|---|
| Only your devices | The Access service token (checked by cloudflared and again by the gateway, pinned to the one token's Client ID), plus an Ed25519 signature from a key in `devices.json`. Keys are added only by a root step that checks the pairing code shown at the server; the gateway never sees the code |
| Before the body | Access and the signature headers (a paired device, a fresh timestamp) are checked before any body is read; at most 16 requests are handled at once; bodies are capped at 32 MiB (8 KiB for pairing). Any error closes the connection, so a leftover body can't pass as the next request on a connection cloudflared reuses |
| Replay, old requests | 60 s timestamp window, nonces remembered for 2 minutes, anything signed before the gateway last started is refused |
| Nothing mixes, nothing kept | One request at a time on the GPU. When the next request comes from a different paired device, every loaded model is unloaded first, so not even Ollama's prompt cache is shared (the cost: one reload when the device changes). No history; bodies are never written or logged (`tests/test_stateless.py` checks the source and does a live check with markers); Ollama and the gateway run with no core dumps and no swap, and Ollama at its normal log level (its debug levels could print prompts) |
| GPU only | Fit estimate before loading, `/api/ps` must show 100% VRAM after. Options that change placement (`num_gpu`, etc.) are stripped. Ollama cloud models are refused |
| No model management from outside | The gateway passes on chat, generate, embed, tags, ps, show, version. Pull/delete/create/copy/push are 404. The panel can pull only allow-listed models |
| Admin only | Panel: Access JWT with your email on every request. Actions need a CSRF token, same-origin and JSON. It can only *start* fixed systemd units (polkit rule), never run a command |
| Local users | `ollama1-nft` lets only the kit's users (and root) connect to Ollama, the gateway and the panel, on any of the machine's own addresses; the services won't start without it. The terminal has no TCP port at all |
| Services | Dedicated no-shell users, `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, empty capability sets. Secrets are handed in with `LoadCredential=`, so they stay root-only on disk |

## Updates

- **Ubuntu security updates and cloudflared:** unattended-upgrades runs
  daily and reboots at 04:00 (the server's time zone) when an update needs it.
- **Ollama:** `ollama1-update-ollama.timer` runs Sundays around 03:30.
  - It downloads the release's linux-amd64 archive and the ROCm component.
  - Each must match the release's `sha256sum.txt`, and GitHub's own digest
    for the file.
  - It switches to the new version, then checks that it starts. If it
    doesn't, it switches back. On any mismatch it stays where it is.
  - The catch-up run after a boot waits for the network: up to 2 minutes at
    a time for `api.github.com` to resolve, and a run that fails only because
    the network is missing is retried every 30 s for 10 minutes. Until then
    the panel says "Waiting for the network", not "failed"; a later success
    replaces it. Any other failure (a checksum, an HTTP error) is "failed" at
    once. `--no-wait` makes one attempt.
  - To run it by hand: `sudo systemctl start ollama1-update-ollama`.
- **Backups:** every night at 02:30, `/etc/ollama1`, the tunnel credential,
  the SSH/apt/GRUB drop-ins and the model list go to
  `/var/backups/ollama1/<date>` on the root filesystem (root-only, 30 days kept; 6b400:
  it was on the mirror). The backup is small: it is skipped, with a message in the
  panel's action list, when the root filesystem has less than 2 GiB free or the
  backup would be over 256 MiB.

## The network

The kit never touches your network settings. It works with a plain wired
port, and also with a bridge. If your server has a bridge `br0` over its
two wired ports (one to the router, one to another device, like a
Raspberry Pi), at `<server-ip>`, that comes from your own bridge script
(for example `/etc/netplan/60-ollama1-bridge.yaml`). Setup then only reads
`br0`'s address for LAN mode and checks that the bridge is up.

With a bridge, the firewall is set so it can't cut the other device off:

- **Bridge netfilter stays off.** `/etc/sysctl.d/60-ollama1-bridge.conf`
  sets `net.bridge.bridge-nf-call-iptables=0`, so bridged frames between
  the other device and the router never go through iptables. ufw's FORWARD policy
  therefore never applies to them. Setup checks this.
- **`ufw route allow in on br0 out on br0`** is added as a second line of
  defence, in case something loads `br_netfilter` later.
- **Interface rules use `br0`.** SSH is allowed in on `br0`, from
  `<lan-cidr>`.

## Troubleshooting

| | |
|---|---|
| Services | `systemctl status ollama ollama1-gateway ollama1-admin ollama1-tunnel ollama1-dash` |
| Logs (counts only) | `journalctl -u ollama1-gateway`, `journalctl -u ollama1-tunnel` |
| GPU seen by Ollama | `journalctl -u ollama \| grep -i "inference compute"` should say ROCm, gfx1030. The RX 6900 XT is supported as is, so `HSA_OVERRIDE_GFX_VERSION` is not set. If Ollama ever reports no GPU, the gateway refuses every model instead of running it on the CPU |
| Old mirror (a server set up before 6b400) | `cat /proc/mdstat`; to remove it: `sudo bash ollama1/tools/remove-raid.sh` (a dry run), see "Removing the old RAID mirror" |
| Boot menu | `grep 'set timeout' /boot/grub/grub.cfg`, all 5 |
| Watchdog | `ollama1-watchdog status`; `journalctl -u ollama1-watchdog`; off: `sudo ./setup.sh --watchdog off`; see "Hardware watchdog" |
| Fans | `ollama1-fan status`; `journalctl -u ollama1-fan`; off: `sudo ./setup.sh --fans off` |
| Lights | `ollama1-leds status`; `journalctl -u ollama1-leds -u ollama1-openrgb`; off: `sudo ./setup.sh --leds off` |
| System move to the other NVMe | `cat /var/lib/ollama1/migrate-os.status`, `bash /usr/local/lib/ollama1-migrate/migrate-os.sh --status`; see "Moving the system to the other drive" |
| Graphics card tuning | `sudo ollama1-gpu-tune status`; `journalctl -u ollama1-gpu-tune -u ollama1-gpu-tune-check`; back to stock: `sudo ollama1-gpu-tune off` |
| SSH | `sudo sshd -T -C user=<your-user>,host=x,addr=<a-lan-address> \| grep -E 'password\|permitroot'` |

## Tests (on the Mac)

```bash
cd ollama1/tests
python3 -m unittest discover -s .      # ~20 s, plus ~3.5 min for the OS-move tests (test_migrate*.py); stub Ollama, fake Access certs, all on 127.0.0.1
python3 mutate.py                      # ~25 min; breaks each of about 320 protections on purpose, expects a failing test
python3 mutate.py migrate              # only the OS-move ones (about 75, each run against the test that must catch it)
python3 gen_vectors.py                 # regenerates PROTOCOL.md's test vectors
```

The tests need Python 3.12 or later, plus `cryptography` or PyNaCl for
Ed25519 (the server uses PyNaCl). They cover:

- signatures, replay, clock skew, unpaired devices;
- the pairing window, its rate limit and the root re-check;
- Access JWTs (with a local fake certs endpoint);
- the GPU fit refusal and the unload on a CPU spill;
- that no request body is ever written anywhere;
- admin authentication and CSRF, and the match between the polkit rule and
  the panel;
- the updater's checksum refusals;
- setup's disk steps against fake disk tools: what gets wiped (only the
  models disk), that no mirror is built or looked at, the filesystem after a
  crash, fstab lines without a UUID;
- the old mirror's removal (`tools/remove-raid.sh`, `test_removeraid.py`) against
  a fake machine: the dry run changes nothing, the typed `yes` on a terminal,
  the members checked by serial, unknown content refused, the exact fstab and
  mdadm.conf edits, no NVMe ever touched (`test_noraid.py` has the backup's
  free-space and size rules and the storage rows);
- request smuggling after errors, nothing read before authentication, the
  device-switch unload;
- the Cloudflare helper against a fake Cloudflare API: reruns change
  nothing, no duplicate DNS records, and the API token is never written
  anywhere;
- the model library sync: nothing on the allow-list is ever deleted, and a
  sync does exactly its preview;
- power and prices: DST, holidays, weekends, windows past midnight, season
  changes, minute-boundary splits, gaps as unknown, the RAPL wrap, the four
  plug types (fake plugs), LAN-only plugs;
- image and video jobs against a stub ComfyUI: the caps, the fixed templates,
  one job at a time, Ollama's models unloaded before and ComfyUI's freed after,
  a running job counted as use for auto sleep, results kept ten minutes, the
  signed and paired-only gate, and that no prompt reaches a log or a file;

- graphics card tuning against fixture sysfs trees and a stand-in driver:
  the card found by its ids, values clamped to what the card reports, only
  the overdrive bit added to the feature mask, back to stock on a kernel
  error or heat, a slower answer only when it repeats in stock/tuned
  alternations (and not while a drive logs errors, another model loads or the
  card is busy), and a revert never re-applied;
- the auto sleep setting across a gateway restart, the idle service's, a
  reboot and a setup.sh run (`test_sleepcfg.py`), and the check after a wake
  waiting, bounded, for the card and the models drive (`test_sleep.py`,
  fake clock);
- the move of the OS to the other NVMe (`tools/migrate-os.sh`) against a fake
  server (fake sysfs and /dev, stand-ins for lsblk, sgdisk, rsync, mount,
  chroot, efibootmgr, tmux; `test_migrate*.py`): drives found by serial with
  swapped nvme names, every refusal, that `--plan` changes nothing, the typed
  or `--confirm-serial` confirmation, every write through a by-id path after a
  fresh serial check, the fstab rewrite (and the old one untouched), the
  chroot and GRUB commands and the kernel options carried over, the firmware
  order (new first, old second, made last), the tmux relaunch, the status
  file (mode, no serials, kept moving), no reboot after a failure, the
  countdown and its Ctrl-C, a run killed at each stage boundary and resumed,
  the root-owned install and the one-rule sudoers text; `efibootmgr -v` read
  in its several shapes, an entry of a stopped run reused, and the optional
  renaming of the old drive's boot files (`test_migrate_efi.py`);
- that the repo holds no personal data and no utility's schedule.
