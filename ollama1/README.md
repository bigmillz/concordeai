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
- It has a text dashboard on the server's monitor, and a web admin panel
  with a terminal that still asks for your Linux password.

The request-signing and pairing rules the app follows are in
[PROTOCOL.md](PROTOCOL.md). For running a server like this on your own
Linux machine, see [docs/your-own-server.md](../docs/your-own-server.md).

## What goes where

| | |
|---|---|
| Host name | `<server-name>` (what you give with `--name`). The time zone stays as the machine has it, unless you give `--timezone` |
| Address | `<server-ip>` on the LAN port (`br0` if you have a bridge over both wired ports, which keeps a second device, like a Raspberry Pi, on the LAN). The kit never changes network settings |
| OS disk (`--os-serial`) | Kept. The root volume grows into the free space on it, and `/home` moves onto it |
| Models disk (`--models-serial`) | **Wiped**, ext4, `/srv/models`. This is Ollama's model folder |
| Two mirror disks (`--hdd1-serial`, `--hdd2-serial`) | **Wiped**, RAID1 mirror, ext4, `/srv/data`. Holds backups and bulk storage, including a nightly copy of the settings and the model list |
| Ollama | 127.0.0.1:11434 only, ROCm build, in `/opt/ollama` |
| Gateway | 127.0.0.1:8431, user `o1gw` |
| Admin panel | 127.0.0.1:8432, user `o1admin` |
| Web terminal (ttyd + `login`) | a UNIX socket in `/run/ollama1/ttyd` (root and the panel's user only), reached only through the panel |
| Dashboard | tty1 (the monitor), user `o1dash`. Also `ollama1-top` over SSH |
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
cd /                                          # not inside /home: /home is about to move
bash ~/concordeai/ollama1/setup.sh --plan     # optional: shows the plan, changes nothing
sudo bash ~/concordeai/ollama1/setup.sh --name <server-name> --zone <your-domain> \
     --os-serial <serial> --models-serial <serial> --hdd1-serial <serial> --hdd2-serial <serial>
```

### What you tell setup

Setup takes nothing about your machine from the kit: you give it these (and
it asks for any it can't find, at the terminal):

| Argument | What it is |
|---|---|
| `--name <server-name>` | Your server's name: lowercase letters, digits and hyphens, 1 to 32 characters, starting with a letter (`gpu-2`, `workshop`, `srv01`). It becomes the host name, the gateway `<server-name>.<your-domain>`, the panel `<server-name>-admin.<your-domain>`, and the names of the tunnel (`<server-name>`), the service token (`<server-name>-app`) and the Access policies and applications (`<server-name> admin - <your-name> only`, `<server-name> app - service token`, `<server-name> admin`, `<server-name> app`). Run several servers by giving each its own name |
| `--user <your-user>` | The one Linux user who may log in over SSH. Default: the user who ran `sudo` |
| `--lan <lan-cidr>` | The network SSH is allowed from, like `10.0.0.0/24`. Default: the network of the machine's LAN port (detected, and shown in the plan: check it before you type `yes`) |
| `--zone <your-domain>` | Your domain on Cloudflare. Not needed with `--skip-cloudflare` |
| `--owner <your-name>` | Your name, used only in the Access policy's name. Default: from your admin email |
| `--timezone <Area/City>` | Default: the time zone the machine already has |
| `--os-serial`, `--models-serial`, `--hdd1-serial`, `--hdd2-serial` | The four disks, by serial. `lsblk -d -o NAME,SIZE,MODEL,SERIAL` lists them. Setup checks each serial exactly before it wipes anything |

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
2. It installs packages: ttyd, python3-nacl, mdadm, nftables, zstd, tmux,
   and cloudflared from Cloudflare's apt repository. That repository's key
   must have the pinned fingerprint, and only that one key is kept.
3. It grows the root volume into the free space (online). It reads the free
   space as a count of extents and stops loudly if it can't.
4. It moves `/home` onto the root filesystem:
   - Anything already sitting in the root filesystem's own `/home` (hidden
     under the old mount) is moved aside to `/home.pre-ollama1-<date>`, not
     deleted.
   - It copies `/home`, checks the copy file by file (and that
     `authorized_keys` is intact), then copies once more right before the
     switch. Only writes in the few seconds after that last copy would be
     missed, which is why you run setup from `cd /`, not from your home
     folder.
   - Only then does it stop mounting the old disk. If a login still has the
     old `/home` open, it detaches it, and the mirror step waits.
5. It wipes the models disk and mounts it at `/srv/models` (by UUID,
   noatime). An earlier `o1models` filesystem is kept, never wiped.
6. It builds the RAID1 mirror of the two mirror disks and mounts it at
   `/srv/data`. The first sync takes many hours in the background; the mirror
   is usable meanwhile (`cat /proc/mdstat`). The safety rules for both disk
   steps are:
   - a disk is found by its serial, checked again exactly, and wiped only
     through its `/dev/disk/by-id/…<serial>` path;
   - it stops if fstab, crypttab or swap still uses the disk, or if it
     carries anything other than the ext4 filesystems this machine has now;
   - a mirror already on the disks (from an earlier run) is reassembled,
     never wiped;
   - a filesystem is made only on an array or partition that setup itself
     just created (it leaves a marker until the format is done), so a stop
     between building the array and formatting it is picked up next time;
   - a mirror or partition setup didn't create that has no readable
     filesystem is never formatted: setup stops and says how to check it
     (`mke2fs -n` lists the backup superblocks, `e2fsck -b <backup>`
     repairs). A read error from `blkid` also stops it;
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
14. It sets up Cloudflare with the API token you paste: the tunnel, the DNS
    records and Access (see below).
15. It checks what listens on the network: nothing but sshd may listen
    beyond loopback (plus the gateway on br0, in LAN mode).
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
- **Dashboard:** it fills the server's monitor (tty1), and `ollama1-top` shows
  it over SSH (`q` quits, `--ascii` for plain terminals, `--once` for one
  text frame). It updates every second, and its charts cover the last 5
  minutes, or an hour after pressing `t`.
  - **Tokens:** a live chart of tokens per second, with now, 1 h, 24 h and
    the peak; prompt tokens/s and the recent time to first token.
  - **Requests:** active, queued, today and per minute (chart); errors by
    kind (busy, fit, spill, ram, oom, Ollama, auth).
  - **Loaded models:** GPU share, VRAM and system memory, time until
    unload; "rest in RAM" for `ram` models.
  - **Model events:** the last loads, unloads (device switch, make room)
    and refusals, as model names and times only.
  - **GPU:** busy %, VRAM, power against its cap (three charts); edge,
    junction and memory temperatures, fan, sclk and mclk.
  - **CPU:** a bar per thread (32 on the 5950X), package temperature, load,
    and a total chart.
  - **Memory:** RAM (chart), swap, and Ollama's memory against its cap.
  - **Disks:** free space on /, /srv/models and /srv/data; read and write
    throughput (chart); RAID state with resync % and time left.
  - **Network:** br0's address and throughput in and out (chart), the
    tunnel and its round-trip time, and each port's link (the Pi's
    included).
  - **Health:** uptime, updates, reboot needed, the next Ollama check, the
    last sleep and wake. **Paired devices:** connected now, or last seen.
  - While a pairing window is open, the code fills the whole screen.

  How it looks and works:
  - It picks its layout by size: three columns on a monitor, two pages
    around 120 columns, three pages at 80x25 (`p` flips).
  - Before it starts on tty1, it chooses a console font for the screen from
    those installed: about 200-240 columns, and the most of its glyphs the
    font has. It draws charts with braille dots or block characters, or in
    ASCII if the font has neither.
  - Keys only change the view (`t`, `p`). On tty1 nothing quits it or opens
    a shell.
  - It never shows a prompt or an answer: only counts, sizes, times and
    names.
  - A frame takes a few milliseconds.
- **Admin panel:** `https://<server-name>-admin.<your-domain>`. Access asks
  for your email and a one-time code. The panel has:
  - the same figures, plus 1 h / 24 h graphs;
  - buttons to apply updates, restart the services, reboot, back up, and
    turn LAN mode on or off;
  - pairing and devices;
  - models from the allow-list;
  - a counts-only log;
  - **Open terminal**, which asks for your Linux user and password.
- **Sleep.** The panel's **Sleep** button (next to Reboot) suspends the
  server. Its power button does the same.
  - To wake it, press the power button again. Holding the button down
    still forces it off.
  - While it sleeps it can't be reached, and anything plugged into its
    second network port (a Raspberry Pi, say) loses its connection.
  - Sleep is refused while a model download or library sync, an update, or
    setup.sh is running. It is allowed during the mirror's first sync,
    which pauses and carries on after waking.
  - During setup.sh, a model download or library sync, and updates, the
    power button does nothing at all: each holds a logind inhibitor
    (`sleep` and `handle-power-key`, mode block) until it ends, so the
    button can't suspend the server mid-job. The inhibitor goes with its
    job however the job ends, even if it is killed. Holding the button down
    still forces it off. `systemd-inhibit --list` shows who holds one.
  - After every wake a check runs. Ollama must answer (`/api/version`,
    `/api/ps`) and the GPU must report through sysfs; if not, Ollama and
    the tunnel restart.
  - The dashboard and the panel show the last sleep and wake times, and the
    result of that check.
  - **Untested until someone tries it by hand.** AMD GPU compute (ROCm)
    after a suspend is a known weak spot.
  - Automatic idle sleep and Wake-on-LAN are on hold.
- **LAN mode** is off by default. With it on, the gateway also answers on
  `http://<server-ip>:8431` from the home LAN, without Access. Signatures
  are still required, and traffic on the LAN is unencrypted. To change it:
  `sudo ollama1-lan on|off`, or use the panel.

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
  - To run it by hand: `sudo systemctl start ollama1-update-ollama`.
- **Backups:** every night at 02:30, `/etc/ollama1`, the tunnel credential,
  the SSH/apt/GRUB drop-ins and the model list go to
  `/srv/data/backups/ollama1/<date>` (root-only, 30 days kept).

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
| Mirror | `cat /proc/mdstat`, `sudo mdadm --detail /dev/md/o1data` |
| Boot menu | `grep 'set timeout' /boot/grub/grub.cfg`, all 5 |
| SSH | `sudo sshd -T -C user=<your-user>,host=x,addr=<a-lan-address> \| grep -E 'password\|permitroot'` |

## Tests (on the Mac)

```bash
cd ollama1/tests
python3 -m unittest discover -s .      # ~20 s; stub Ollama, fake Access certs, all on 127.0.0.1
python3 mutate.py                      # ~20 min; breaks each of about 250 protections on purpose, expects a failing test
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
- setup's disk steps against fake disk tools: what gets wiped, the
  reassembly of an existing mirror, the filesystem after a crash, fstab
  lines without a UUID;
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
- that the repo holds no personal data and no utility's schedule.
