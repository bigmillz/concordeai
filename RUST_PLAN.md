# ConcordeAI: the move from Python to Rust

Patrick: "I want to do the fastest language we can run that will be cross-platform."
Rust, chosen and approved (6b532). This is the plan, grounded in the code as it is
today (`millenai.py` at 55,280 lines, build 277) and in measurements taken on this Mac
(M4 Pro) on 2026-11-08. Every estimate is marked `*`.

The approach is a **strangler**, never a big-bang rewrite: the Python app keeps running
and keeps shipping while Rust takes over from the outside in. Step 1 is done on macOS
(the native shell, `shell/`); steps 2 and 3 are planned here.

---

## 0. What the baseline says, before anything else

Measured on a windowless dev copy (`MILLENAI_DEV=1`, own `MILLENAI_HOME`, port 9884, no
servers, no cloud keys; scripts in the session scratchpad, `rs_baseline_*.py`):

| What | Python today | Notes |
|---|---|---|
| Process start → `run/instance.json` written | **0.35 s** | import of the 55k-line file + bind |
| Process start → `/` answers 200 | **0.38–0.40 s** | windowless; the window adds its own time (below) |
| Idle RSS, server only, 30 s and 60 s | **208 MB** | no window, no engine |
| Served page | **742,353 bytes**, one request, no gzip | 523 KB inline JS, 173 KB inline CSS, 2 `<script>`s |
| Server's own render of the page (warm) | 17–35 ms | the `__TOKEN__` substitutions over a 740 KB string |
| Page parse, Blink (Browser pane) | responseEnd → domInteractive **19 ms**, DCL at 36 ms, load at 40 ms | WKWebView not measured yet (no probe in the shell) |
| `/api/chat` received → first model call | **2.3–3.1 ms** warm, 9.1 ms on the first call (it probes `/models` first) | Cloud Only tier against a recording stub, search off, one-line prompt |
| headers out | 1.6–2.2 ms | |

The one surprising number is the last: **the Python server adds ~3 ms between receiving a
chat and calling the model.** Request handling is not where the time goes; model load,
network and generation are. So the Rust move does not buy per-request latency. What it buys,
measured below and in step 1: launch time, memory, a real binary, an end to the pywebview
crash classes (the close crash, 6b508), and a path to in-process engines (no `mlx_lm server`
subprocess, no HTTP hop to the engine, no multi-second cold start of a Python engine process).

The windowed Python app (pywebview, WKWebView), same dev-copy setup, fresh folder:

| | pywebview app | Tauri shell (release .app) |
|---|---|---|
| Process start → window on screen | **0.94–1.12 s** | **0.22–0.23 s** warm (3.0 s once, first launch after copying) |
| Memory at 20 s (app process + WebKit helpers + server) | **1.16–1.30 GB** | **0.70–1.24 GB** (the swing is the clip fetch; see §9) |
| App bundle on disk | 3.0 MB `.app` (+ the venv it builds on first run, ~2–4 GB* with mlx) | 6.7 MB `.app` (+ the same venv, until step 3) |

(§9 holds the A/B table; the shell's numbers are from the release bundle in
`scratchpad/out/ConcordeAI.app`.)

---

## 1. The contract to preserve

Everything below is what the page, the gauntlet and the paired servers depend on. The Rust
core must answer identically; the gauntlet's live-HTTP checks are the conformance suite.

### 1.1 HTTP surface

`StudioHandler` (http.server, `HTTP/1.0`, one thread per request, streams then closes, no
chunking). `Cache-Control: no-store` on every response. Routes, from `_do_GET`/`_do_POST`:

**GET**: `/`, `/account`, `/account.css`, `/static/vfx/hdr-beacon.mp4`, `/static/sky/*`,
`/api/account/session`, `/api/workspace*`, `/api/geo*`, `/api/sky/cached`, `/api/sky/status*`,
`/api/remote/classify`, `/api/remote/config`, `/api/servers`, `/api/servers/sleep`,
`/api/servers/models`, `/api/servers/usage`, `/api/cloud`, `/api/stats`, `/api/usage`,
`/api/bench/running`, `/api/bench`, `/api/bench/targets`, `/api/engines`, `/api/setup`,
`/api/setup/busy`, `/api/models/offer`, `/api/model/update*`, `/api/update/check*`,
`/api/update/status`, `/api/update/whatsnew`, `/api/export/*`, `/api/video/*`, `/api/image/*`,
`/api/tiers`, `/api/prefs`, `/api/suggest`, `/api/chats`, `/api/chats/one?`,
`/api/chats/search*`, `/api/memory`, `/api/me`, `/api/voice/status`, plus dev-only
`/api/test/*` behind `MILLENAI_TEST_HOOKS`.

**POST**: `/api/account/*`, `/api/window/focus`, `/api/servers/*`, `/api/cloud/balance`,
`/api/cloud/set`, `/api/workspace/set|off`, `/api/remote/*` (test, forget, approve, …),
`/api/setup/install`, `/api/models/offer`, `/api/update/install`, `/api/update/download`,
`/api/funnel/image`, `/api/funnel`, `/api/prefs`, `/api/prefs/adopt`, `/api/webstore/clean`,
`/api/chats`, `/api/chats/ops`, `/api/sky/seen`, `/api/chat/hurry`, `/api/suggest/refresh`,
`/api/title`, `/api/open-logs`, `/api/memory/clear`, `/api/logout`, `/api/forget`,
`/api/voice/prepare`, `/api/transcribe`, `/api/speak`, `/api/annotate/save`,
`/api/export/reveal`, `/api/studio/opts`, `/api/studio/install|remove|…`, `/api/model/download`,
`/api/model/update`, `/api/model/cleanup`, `/api/model/remove`, `/api/bench/start|stop|skip|…`,
and **`/api/chat`** (everything else POSTed is a 404).

Headers the page reads: `X-Web-Search`, `X-Models`, `X-Eta` (6b459), `X-Profile` (409 +
`X-Profile: changed` on a profile switch), `ETag: "b<APP_BUILD>-…"` on `/`, `Content-Range`
on media (range serving is gauntlet-checked). Retired tier names are aliased on the way in
(`Smart`/`Best` → `Fast`, `Power` → `Pro`).

### 1.2 The stream protocol

`/api/chat` answers `text/plain` and streams prose with **out-of-band control frames**
inline: `\0TAG:json\0` (`NUL = chr(0)`). Tags: `STATUS`, `STEP`, `DRAFT`, `RUN`, `SOURCES`,
`PHOTOS`, `MAP`, `PLACES2`, `PLACEHINT`, `CTX`, `APPROVE`, and bare `\0RESET\0`. Only the
server writes frames (`Ctl(str)`): `emit()` strips NUL from every other string, so a model
answer or a quoted page can never open a frame. A heartbeat re-sends the last `STATUS` when
the stream is quiet for >20 s. The client strips frames with `\x00[A-Z0-9]+:.*?\x00`.
`APPROVE` blocks server-side on a `threading.Event` until `/api/remote/approve` arrives.

In Rust this is one type, `Frame { tag, json }`, serialised as above, written through the
same `emit` that scrubs NUL from prose. The conformance suite replays recorded streams.

### 1.3 Auth: `_gate`, in order

1. `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding fails first).
2. No proxy header (`X-Forwarded-For`, `X-Forwarded-Host`, `Forwarded`, `X-Real-IP`,
   `Cf-Connecting-Ip`, `True-Client-IP`) → 403.
3. `GET /?boot=<code>`: the one-time code (60 s TTL, minted just before the window opens;
   a wrong guess does not burn it, an expired one is burned) trades for the cookie
   `millen_key_<port>=<ACCESS_KEY>; Path=/; HttpOnly; SameSite=Strict` and a 302 to `/`.
   The cookie is named for the port because cookies are shared across ports of one host.
4. Otherwise the cookie must be present (`compare_digest` on bytes, every cookie of that
   name checked).
5. Cookie alone opens only `/` and the `/static/` clips (`_COOKIE_ONLY`, allow-listed, not
   prefix-checked). Everything else needs `X-Api-Token: <API_TOKEN>` (`compare_digest`).
6. `X-Profile`, when sent, must equal the active profile's tag, else 409.
7. POST writes also pass `_csrf_ok` (a form content type is the forgery).

Both secrets are minted per launch (`secrets.token_urlsafe(32)`), never in the HTML, a URL or
a cookie (the token). `run/instance.json` (0600) holds `{port, key, token, pid}` for native
callers (the gauntlet, a second launch's `/api/window/focus` hand-off). In shell mode
(6b532) `run/boot.json` (0600, `{code, pid}`) holds the one-time code for the shell, which
deletes it after reading.

The page gets the token through `window.pywebview.api.api_token()` (the one bridge method,
answered only while the window shows `http://127.0.0.1:<port>`); the Tauri shell provides the
same object as a shim over one IPC command with the same origin rule. **This stays the
contract in every step**: the Rust core mints the key and token, writes the same note, and
the shell (same process by step 2) hands the token to the page the same way.

### 1.4 Data folder and the profile seam

`~/Library/Application Support/MillenAI` (macOS, Linux) / `%LOCALAPPDATA%\MillenAI`
(Windows). Inside: `prefs.json`, `cloud.json` (keys, validated and model-discovered on
save), `profile.json`, `servers.json` (paired servers), `remote.json`, the chat store (the
`.v2` files, 6b324; a whole-list save wins, so one copy per folder: `run/instance.lock`),
`memory`, `usage.jsonl`, `quality.jsonl`, `run/` (instance note, boot note, lock, transient
prompt/speech/ssh files swept at start), `webkit/` (pywebview's store), `venv/`, `sky/`
(backdrop clips), `logs/` (dev copies), `accounts/` (dev builds). A dev copy
(`MILLENAI_DEV=1` + `MILLENAI_HOME`) has all of this in its own folder; the real folder, or
anything inside/around it, is refused (exit 2).

The profile seam: every request binds a `ctx` (`switch_ctx()` / `bind_ctx()`), and
`_pfile(name, ctx)` resolves a file for the profile the thread works for; `_data_base()`
returns None today (the app folder). In Rust: a `Ctx` passed explicitly to every store call
(no thread-locals), with the same file names and formats, so a Rust core and the Python
sidecar can read one folder during the transition.

### 1.5 Dev-copy rules and exit codes

`MILLENAI_DEV`, `MILLENAI_HOME`, `MILLENAI_NOWINDOW`, `MILLENAI_TEST_HOOKS`,
`MILLENAI_SYNC_URL`, `MILLENAI_PORT`, `MILLENAI_TESTBUILD`, `MILLENAI_NO_PACKAGE`, and now
`MILLENAI_SHELL` (only `tauri`). Any other `MILLENAI_*` name → exit 2. Exit 1 no port, 2
refused setting, 3 folder in use, 4 no window (never in shell mode). Ports: 8889 the app
(18890–18898 fallbacks), 9894/9895 dev, 9901–9903 the gauntlet, 9897 ConcordeGo, 8884+ by
twos the engines. The shell passes every `MILLENAI_*` through to the sidecar untouched and
mirrors `_real_app_dir`/`_dev_home` only to find `run/`; the server stays the judge.

---

## 2. Subsystem inventory

Sizes are line counts of `millenai.py` by region, rounded (`*` = estimated from markers and
`def` density). Risk is porting risk, not code quality.

| Subsystem | Size* | Depends on | Porting risk | Notes |
|---|---|---|---|---|
| Page (`HTML_CONTENT`, CSS, JS) | ~15,000 | nothing server-side but the `__TOKEN__`s | **none to port**: it is served as-is | The Horizon redesign happens here in parallel; the shell is agnostic |
| HTTP handler + `_gate` + CSRF + range serving | ~2,500 | stdlib | low | The first thing the Rust core takes |
| `/api/chat`, council (`run_council`, drafts, critic, compositor, ladders, ETA) | ~6,000* | engines, cloud, search, memory, quality log | **high** | The heart; most of the gauntlet's live checks |
| Cloud providers (4), key validation, cooldowns, balance | ~1,500 | HTTPS, UA fingerprint rules | medium | Probe payload must equal runtime payload (trap) |
| Engines: MLX (`mlx_lm server` per model), Ollama spawn/pull, catalog, routes, janitor, RAM fit | ~3,000* | subprocesses, HF cache, `psutil` | medium (step 2), **high** (step 3 in-process) | |
| Servers (`# ==== servers`), model sets, pictures, wake, sleep, gateway | ~4,400 | HTTP to the kit, LAN wake | medium | Ollama1 is down; send nothing during the port |
| Web search / weather / OSM places / deep research / grounded turn | ~3,000* | `ddgs`, HTTPS, redirects | medium | `ddgs` has no Rust twin: port the HTML-result path or keep a sidecar |
| Remote SSH agent (plan/run/read, classifier, approvals, systemd-run, reboots) | ~3,500* | `ssh` binary, `BatchMode=yes` | medium | Classifier is pure logic (easy); the loop is stateful |
| Profiles / accounts / crypto (`cai_crypto`, PyNaCl, sync) | ~3,500 | PyNaCl, sync server | medium | dev builds only today (`ACCOUNTS`) |
| Chat store (.v2), prefs, memory, usage/quality logs, migrations | ~2,000* | files | low | Formats fixed; shared by Python and Rust during transition |
| Benchmarks (local, servers, cloud; targets) | ~2,000 | engines | medium | |
| Updater (`check_update`, download, swap) | ~500 | GitHub API, DMG/MSI | low logic, **high consequence** | See §7 |
| First-run setup, model downloads, studio opts | ~1,500* | `huggingface_hub`, pip | medium | |
| Voice (transcribe `mlx_whisper`, speak), funnel images, photo edit, annotate, exports | ~2,500 | Python libs (reportlab, docx, pptx, Pillow-ish) | high per lib, low value | Last; or stay Python-sidecar longest |
| Sky backdrop clips, telemetry (`psutil`), window glue (pywebview, Cocoa patches) | ~1,500 | OS | low | Window glue is **replaced** by the shell (done) |

54 background threads are started across these (`ctx_thread`/`Thread`); each is a tokio
task or a blocking-pool job in Rust.

---

## 3. The three steps

### Step 1, native shell (done on macOS, this branch)

`shell/` is a Tauri 2 app (Rust) that replaces pywebview as the window:

- Starts `millenai.py` as a sidecar (`MILLENAI_SHELL=tauri`), waits for `run/instance.json`
  and `run/boot.json`, loads `/?boot=<code>`; the page gets its token through
  `window.pywebview.api.api_token()`, a shim over one IPC command that answers only
  `http://127.0.0.1:<the sidecar's port>` (capability `allow-api-token` on
  `http://127.0.0.1:*` + the command's own origin check).
- Lifecycle: quit (Cmd+Q, close, single-instance plugin) SIGTERMs the sidecar and waits
  8 s, then SIGKILLs; the sidecar also watches its parent pid and exits on its own, so a
  crashed or force-quit shell leaves no orphan (proven: SIGKILL of the shell → sidecar gone
  within 1–2 s, note dropped, port free).
- Native look: transparent title bar, hidden title, dark appearance, window and webview
  background `#03040a` before any paint (first frame captured: pure night, no flash).
- Menus: ConcordeAI (About, Services, Hide, Quit), Edit (undo/redo/cut/copy/paste/select
  all), View (Reload), Window (Minimize, Zoom, Fullscreen, Close).
- Downloads go to `~/Downloads`; links to other origins open in the default browser; the
  main frame only ever shows the app's own origin, `about:blank`, or the shell's own error
  page (a `data:` document with the server's exit code meaning and log tail).

Lesson recorded for step 2: the webview must start on `about:blank`. Starting on the
`tauri://localhost` page made WebKit treat the navigation to `/?boot=` as cross-site, so the
`SameSite=Strict` cookie set by the 302 was withheld on `/` and the window sat on the 403.

What is **not** done yet in the shell: microphone capture permission in WKWebView (pywebview
needed a `requestMediaCapturePermission` delegate and the private `mediaDevicesEnabled`
preference; wry grants the delegate call, the preference is unverified), `target=_blank`
handling needs a windowed check, the "web store" cleanup (`_webstore_native`) is a no-op in
shell mode (the shell's WKWebView store is keyed to the bundle id, a clean slate), Windows
and Linux builds (type-checked for Windows from the Mac; CI needed for real builds, §6),
signing/notarisation, the updater swap (§7), `release.sh` producing the shell bundle.

### Step 2, the core in Rust, one subsystem at a time

A Rust HTTP core (**axum 0.8 on hyper 1 / tokio 1**) inside the shell process (or a second
binary during development) owns the port and the auth gate. Every route it has not taken
yet is **proxied to the Python sidecar** on a private port (the sidecar's `_gate` still
runs; the core forwards the cookie and token it minted, and the sidecar in a new
`MILLENAI_SHELL=core` mode trusts only loopback from the core's pid-checked connection).
The gauntlet's live-HTTP checks run against the core's port and must stay green at every
cut; the Python-source checks move to Rust unit tests as each piece lands.

Porting order, lowest risk and highest value first:

1. **Gate + static + page**: `_gate`, `/`, `/static/`, the ETag, the `__TOKEN__`
   substitutions (the page becomes a `include_str!` template; the ~740 KB render drops from
   17–35 ms to <1 ms*). Note and boot note minted in Rust. ~1 week*.
2. **Stores**: prefs, chats (.v2), memory, usage/quality logs, profile seam (`Ctx`). Formats
   byte-compatible; Python keeps reading them. ~2 weeks*.
3. **Stats, tiers, engines inventory, catalog, RAM fit** (`/api/stats`, `/api/tiers`,
   `/api/engines`, `/api/setup`, `/api/models/offer`): read-mostly, well-covered. ~1 week*.
4. **Cloud providers**: key save/validate/discover, cooldowns, balance, the two ladders.
   One `Provider` trait, four impls, `reqwest` with the app's UA. ~2 weeks*.
5. **Servers** (paired kit): list, models, wake, sleep, usage, model sets. ~2 weeks*.
6. **`/api/chat` and the council**: stream type, heartbeat, search branch, drafts in
   parallel for cloud / sequential for local, critic, compositor, ETA header, quality log,
   memory extraction. The engines are still subprocesses (MLX server, Ollama) spoken to over
   HTTP, exactly as today. The largest piece; land it behind a flag with A/B against the
   sidecar on recorded prompts. ~6 weeks*.
7. **Search family** (plain, weather, OSM, deep research, grounded turn). `ddgs` is
   Python-only; the Rust path scrapes the same endpoints or we keep the sidecar for search
   until last. ~3 weeks*.
8. **Remote SSH agent** (classifier first; it is pure and gauntlet-covered). ~3 weeks*.
9. **Benchmarks, updater (§7), first-run setup/downloads.** ~3 weeks*.
10. **Voice, exports, image tools, funnels**: port only what has a good crate; the rest
    stays a small Python sidecar or is dropped with Patrick's say-so. ~3 weeks*.
11. **Profiles/accounts/crypto** when the accounts work resumes (`ACCOUNTS` is dev-only).

### Step 3, engines in-process

- **llama.cpp** via `llama-cpp-2` 0.1.161 (utilityai/llama-cpp-rs; `llama-cpp-sys-2`
  builds llama.cpp from source with the `metal` feature on Apple silicon, `cuda`/`vulkan`
  elsewhere). Maintained and released often (161 patch releases; tool-calling and
  JSON-schema grammar helpers in the latest docs), but the crate calls itself a thin, moving
  wrapper: pin exact versions, wrap it behind our own `Engine` trait. GGUF weights from the
  catalog; this replaces Ollama for *local* models on Windows/Linux/Intel Macs and is an
  option on Apple silicon.
- **MLX** via `mlx-rs` 0.34.0 (oxideai; `mlx-sys` 0.7.0 generated from `mlx-c`). Honest
  status: the wrapper tracks MLX's own version, but docs.rs fails to build it and there is no
  mature `mlx-lm`-equivalent generation stack in Rust (tokenisers, KV cache, sampling,
  quantised model loaders per architecture are ours to write or borrow). A second project,
  `mlxrs` (FinDIT), is single-thread only (`Array: !Send`). **Recommendation**: keep
  `mlx_lm server` as a subprocess engine through step 2, and start step 3's MLX work as a
  spike (one model, Gemma/Qwen class, greedy decode) before committing; llama.cpp with Metal
  is the fallback for Apple silicon if the spike stalls, at a known quality/speed cost to be
  measured against `mlx_lm`.
- **Ollama stays** supported over HTTP (paired servers, Windows ARM, people who already have
  it). Whisper via `whisper-rs` (whisper.cpp) when voice is ported.
- Python is retired when the last sidecar route is gone; the venv bootstrap, pip pins and
  `crypto-requirements.txt` go with it.

Sources checked today: crates.io (`cargo search`/API: tauri 2.12.5, tauri-build 2.7.4,
tauri-cli 2.12.4, tauri-plugin-single-instance 2.5.5, tauri-plugin-shell 2.6.0,
tauri-plugin-process 2.4.0, tauri-plugin-updater 2.14.1, axum 0.8.9, hyper 1.12.0, tokio
1.54.1, reqwest 0.13.5, llama-cpp-2 0.1.161, mlx-rs 0.34.0, mlx-sys 0.7.0; Tauri 3 is at
alpha 6 and not for us yet), the Tauri v2 sidecar and capabilities guides
(https://v2.tauri.app/develop/sidecar/, https://v2.tauri.app/security/capabilities/),
docs.rs for `WebviewWindowBuilder` (initialization_script is main-frame only;
`initialization_script_for_all_frames` exists and we do not use it; `on_download`,
`on_navigation`, `background_color`), `llama-cpp-2` (https://docs.rs/crate/llama-cpp-2/latest)
and `mlx-rs` (https://docs.rs/crate/mlx-rs/latest, README notes it is "in active development").

---

## 4. Crate choices

| Need | Crate | Why |
|---|---|---|
| Window, menus, IPC, bundler | `tauri` 2.12 (+ `single-instance`, `opener`) | WKWebView/WebView2/WebKitGTK, one codebase, bundler for .app/.dmg/.msi/.nsis/.deb/.AppImage, capabilities ACL for the one command |
| HTTP server | `axum` 0.8 on `hyper` 1 + `tokio` | Streaming bodies with `Body::from_stream`, tower middleware for `_gate`, mature; `hyper` alone if we want zero framework |
| HTTP client | `reqwest` 0.13 (rustls) | Streaming SSE from providers and engines, proxy env honoured, custom UA |
| JSON | `serde` + `serde_json` | Formats are JSON everywhere |
| Processes | `tokio::process` + `libc` (SIGTERM) | Engines, `ssh`, `systemd-run`, Ollama |
| Files | `std::fs` + `fs2`/`rustix` for `flock` | `run/instance.lock` semantics |
| Secrets | `rand` (OsRng) + `subtle::ConstantTimeEq` | `token_urlsafe(32)` and `compare_digest` twins |
| Crypto (accounts) | `crypto_box`/`sodiumoxide`-free: `crypto_box` + `blake2`/`argon2` to match PyNaCl | byte-compatible with `cai_crypto` |
| SSH | shell out to `ssh` (as today) | key-only by design; `russh` later if wanted |
| System telemetry | `sysinfo` | the `psutil` role |
| llama.cpp | `llama-cpp-2` | see step 3 |
| MLX | `mlx-rs` (spike) | see step 3 |
| Whisper | `whisper-rs` | voice, late |
| Logging | `tracing` + file appender | the launch log |

Not chosen: `tauri-plugin-shell`'s sidecar (`externalBin` wants a per-target-triple binary;
our sidecar is an interpreter plus a script, so `std::process::Command` is the right tool),
`tauri-plugin-updater` for now (§7), Tauri 3 alpha.

---

## 5. Dev run and build

```bash
export PATH="$HOME/.cargo/bin:/opt/homebrew/opt/rustup/bin:$PATH"
cd shell/src-tauri

# a dev copy, like any other (the shell passes MILLENAI_* through to the server)
MILLENAI_DEV=1 MILLENAI_HOME="$HOME/Library/Application Support/MillenAI-dev" MILLENAI_PORT=9895 \
  cargo tauri dev            # or: cargo run

# the real app on the real folder (what the .app does when double-clicked)
cargo run --release

# the release bundle (.app under target/release/bundle/macos/)
cargo tauri build --bundles app
```

Overrides for development: `CONCORDE_PYTHON` (the interpreter; default the app's venv
`…/MillenAI/venv/bin/python3`), `CONCORDE_SERVER` (a `millenai.py`; default the repo's copy
in a debug build, `Contents/Resources/millenai.py` in a bundle). A debug build also honours
`CONCORDE_SHELL_QUIT_AFTER=<secs>` (quits through the Cmd+Q path, for scripts) and logs
navigations to stderr. The bundle holds `millenai.py`, `fonts/`, `vfx/` in Resources; the
venv is still built by the Python app's first run (or `build_macos_app.sh`) until step 3.

---

## 6. Packaging and signing

- **macOS**: Tauri bundles `ConcordeAI.app` (identifier **`com.millen.millenai`, kept**: the
  Local Network permission, the WKWebView store and the updater's `pgrep` all key on it) and
  the `.dmg`. Signing: `bundle.macOS.signingIdentity` = the Developer ID Application cert;
  notarisation with `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID` (or `APPLE_API_KEY…`) in
  the env makes `cargo tauri build` staple the ticket. Hardened runtime needs the
  microphone entitlement (`com.apple.security.device.audio-input`) and, with the Python
  sidecar still in the bundle, `com.apple.security.cs.allow-unsigned-executable-memory` /
  `disable-library-validation` for the venv's dylibs — which is exactly why the sidecar
  should live outside the bundle (the venv already does) until Python is gone. Today's
  build is ad-hoc signed like the Python `.app`; the `Info.plist` in `shell/src-tauri/`
  carries the two usage strings.
- **Windows**: `cargo tauri build --bundles msi,nsis` (WiX 3 MSI matches the current
  `windows-installer.yml` contract: an `.msi`, with `arm64` in the name for ARM64). WebView2
  is a bundle option (`webviewInstallMode`: download bootstrapper by default). Code signing
  via `bundle.windows.certificateThumbprint` or a signtool hook.
- **Linux**: `.deb` (depends `python3` until step 3) and `.AppImage`; webkit2gtk 4.1.
- **CI**: `.github/workflows/shell-build.yml` (added, not pushed) builds all four targets on
  `workflow_dispatch`/pushes to `rust-shell` and uploads artifacts; it is not wired to
  releases. From this Mac: `cargo check --target x86_64-pc-windows-msvc` passes with Homebrew
  LLVM's `llvm-rc` on PATH (tauri-winres needs it for the icon resource); Linux cannot be
  checked here (gtk/webkit `.pc` files are needed by build scripts) — CI is the check.

---

## 7. The in-app updater

Today: `check_update()` reads `/releases/latest` (stable) or the release list (beta,
nightly) of `bigmillz/concordeai`, compares `_build_from_tag("v<N>") > APP_BUILD`, never
offers an older marketing version, picks the `.dmg` asset (or the right `.msi`/`-Windows.zip`
on a PC), downloads to `update.dmg`, swaps the bundle and relaunches. Prereleases carry
betas/RCs; stable users never see them.

**Keep the contract**: tags stay `v<APP_BUILD>`, `APP_BUILD` stays the one counter in
`millenai.py` (the shell's `tauri.conf.json` `version` is stamped from `APP_VERSION` by
`release.sh`, and `CFBundleVersion` must be set to `APP_BUILD` so the bundle's build matches
the tag — Tauri writes `version` there by default; verify the merged `Info.plist` wins or
patch it in `release.sh` after bundling), prereleases stay the beta channel.

Migration: (a) **step 1**: the Python updater keeps working inside the shell; the swap code
must learn the new bundle layout (`Contents/MacOS/ConcordeAI`, `Resources/millenai.py`) and
to relaunch the shell, and the DMG `release.sh` uploads becomes the Tauri `.dmg`. A user on
the Python app updates *into* the shell by the same path (same tag rule, same asset name).
(b) **step 2**: the check moves to Rust with the same GitHub calls, the same 15-minute cache
and the same channel rules; the gauntlet's updater checks become its conformance tests.
`tauri-plugin-updater` is not adopted: it wants its own `latest.json` and signature scheme,
which would break the one-tag contract and the Windows MSI path for existing installs.

---

## 8. Test strategy

- **The gauntlet stays the oracle.** Its live-HTTP checks (auth, owner lockdown, range
  serving, classifier, streams) point at whichever process owns the port; a Rust route is
  "done" when they pass against it. The served-HTML checks are untouched (the page is the
  same bytes). Python-source checks are rewritten as Rust `#[test]`s as each subsystem
  lands and deleted from the gauntlet in the same cut, with a NOTES entry each.
- **Recorded conformance**: before porting a route, record request/response pairs (and chat
  streams, frames included) from the Python sidecar with the provider stub; replay against
  Rust (`shell/tests/conformance/`). Frames compare as parsed `(tag, json)` sequences.
- **Shell tests** (step 1, in the gauntlet now, `== the native shell's sidecar mode ==`):
  the setting's rules and mutants, the boot note, the cookie/token/Host/proxy rules in shell
  mode, the orphan rule (parent killed → sidecar gone), and the Rust source's own rules
  (one command, origin check, about:blank, night colour, one permission, no secret in the
  shim).
- **Performance gates** in CI (§9): launch to window, idle RSS, request→first model call,
  page bytes; a regression fails the build.
- **Windowed checks** stay manual for now (voice, downloads, external links, Cmd+Q): a short
  checklist in NOTES per cut, as today.

---

## 9. Performance goals and the numbers

Goals (release builds, this M4 Pro; `*` = targets, to be re-measured each cut):

| Metric | Python today | Shell today (step 1) | Step 2 target* | Step 3 target* |
|---|---|---|---|---|
| Launch → window on screen | 0.94–1.12 s | **0.22–0.23 s** | ≤ 0.3 s | ≤ 0.3 s |
| Launch → page interactive | ~0.4 s server + page | server 0.4 s after window | ≤ 0.5 s | ≤ 0.5 s |
| Idle memory (everything, 20 s) | 1.16–1.30 GB | 0.70–1.24 GB | ≤ 250 MB | ≤ 200 MB + weights |
| `/api/chat` → first model call | 2–3 ms | 2–3 ms (same server) | ≤ 2 ms | ≤ 1 ms, no HTTP hop to a local engine |
| App on disk | 3.0 MB + venv (GBs) | 6.7 MB + venv | 15 MB* + venv | ≤ 60 MB*, no venv |
| Cold start of a local model | `mlx_lm server` spawn + load | same | same | load only |

A/B, pywebview window vs the release shell, fresh folders, two rounds each, memory at 20 s:

| Round | pywebview: window | pywebview: memory (Python / GPU / WebContent / Networking = total) | Shell: window | Shell: memory (ConcordeAI / Python sidecar / GPU / WebContent / Networking = total) |
|---|---|---|---|---|
| 1 | 1.03 s | 580 / 100 / 88 / 533 = **1,299 MB** | 3.00 s (first launch of a freshly copied binary: macOS's one-time assessment) | 102 / 483 / 32 / 67 / 20 = **704 MB** |
| 2 | 0.94 s | 577 / 78 / 85 / 417 = **1,156 MB** | **0.22 s** | 115 / 482 / 75 / 81 / 200 = **953 MB** |
| 3 | 1.12 s | 576 / 104 / 92 / 463 = **1,233 MB** | **0.23 s** | 109 / 486 / 78 / 90 / 476 = **1,239 MB** |

Reading it honestly: **launch is 4–5× faster** (0.22 s vs ~1.0 s to a window on screen; the
sidecar's note follows ~0.3 s later, the same server start as today). **Memory is a wash at
step 1**: the WebKit Networking process is the backdrop clips being fetched and swings
20–530 MB in both apps; without it, pywebview sits at 767–790 MB and the shell at 684–763 MB.
The app process itself is 102–115 MB (Rust + Tauri) against the ~95 MB pywebview adds to
the Python process (577 vs 482 MB with the same page in use). The Python server with the
page using it is ~480 MB either way; that is step 2's number to bring down (208 MB of it is
the bare server; the rest is the page's calls, clips and caches).

---

## 10. Timeline (estimates*, one engineer, Patrick reviewing)

| When* | What |
|---|---|
| Week 0 (done) | Step 1 on macOS: shell, sidecar mode, auth parity, lifecycle, gauntlet checks |
| Weeks 1–2 | Step 1 finish: mic permission, downloads/links verified windowed, Windows + Linux via CI, updater swap for the new layout, `release.sh` builds the shell bundle, notarisation. First shell release as a **beta** (prerelease) |
| Weeks 3–4 | Step 2.1–2.2: Rust gate + page + stores; proxy for the rest; conformance recorder |
| Weeks 5–6 | Step 2.3–2.4: stats/tiers/engines inventory; cloud providers |
| Weeks 7–8 | Step 2.5: servers |
| Weeks 9–14 | Step 2.6: `/api/chat` and the council (behind a flag, A/B against the sidecar) |
| Weeks 15–17 | Step 2.7: search family |
| Weeks 18–20 | Step 2.8: remote agent |
| Weeks 21–23 | Step 2.9: benchmarks, updater in Rust, setup |
| Weeks 24–26 | Step 2.10: voice/exports/images decided and ported or dropped |
| Weeks 27–30 | Step 3 spike: llama.cpp in-process on all platforms; MLX spike (one model) |
| Weeks 31–36 | Step 3: engines in-process, Python retired, venv gone |

Roughly nine months*. Every cut ships behind the same gauntlet; nothing waits for the end.

---

## 11. Risks

1. **MLX in Rust is immature.** `mlx-rs` exists but the LM stack above it does not; the
   spike may say "keep `mlx_lm server`" for a long time. Mitigation: llama.cpp+Metal as the
   Apple-silicon fallback; measure tok/s against `mlx_lm` before deciding.
2. **The council is the app.** Six weeks* of porting the most intertwined code; subtle
   behaviours (compositor moved to the end of the roster, thread-local search rows,
   heartbeat, provider cooldowns) are documented mostly in NOTES. Mitigation: recorded
   conformance streams, flag-gated rollout, NOTES entries read before each piece.
3. **Updater swap during the transition.** A bad swap strands installs. Mitigation: the
   first shell release goes to the beta channel; the swap is tested on a copy of the bundle
   in a dev folder; the tag/asset contract is unchanged.
4. **Two processes until step 2 lands**: the Python sidecar's memory (170–480 MB measured,
   varying with the backdrop clip fetch) is still paid; the shell adds its own ~110 MB plus
   WebKit. Step 1 is a window win, not yet a memory win; say so.
5. **WKWebView/WebView2/WebKitGTK differences**: cookies (the `about:blank` lesson),
   media permissions, downloads, `target=_blank`. Mitigation: a per-platform windowed
   checklist, CI builds early.
6. **Signing**: hardened runtime vs an unsigned venv inside the sandbox of a signed app.
   Mitigation: venv outside the bundle (already), notarise the shell alone.
7. **The page's 740 KB** is unchanged by any of this; the Horizon work owns it. The shell's
   only requirement on it is the `pywebview` bridge name, which the shim preserves.
8. **Two bundle identities during rollout**: the Python `.app` and the shell `.app` share
   `com.millen.millenai`; installing one over the other is intended, running both is
   prevented by `run/instance.lock` on the Python side and the single-instance plugin on the
   shell side.
9. **`ddgs`** has no Rust twin; search may be the last sidecar route or needs its own
   scraper and its own breakage risk.
10. **Google Drive rolling `.git` back** (CLAUDE.md): `git fetch` before any release.

---

## 12. Decisions for Patrick

1. **Ship the shell as the next beta** once mic/downloads are verified windowed, or hold it
   until step 2 shows memory gains? (The shell is a window-quality win now; memory comes
   with step 2.)
2. **Vibrancy**: the title bar is transparent over the night colour. A translucent
   (vibrant) sidebar/window is possible with `window-vibrancy`, but it would blend the
   desktop into the night sky; recommend against unless the Horizon designer wants it.
3. **MLX**: accept the plan to keep `mlx_lm server` as a subprocess until a Rust spike
   proves itself, with llama.cpp+Metal as the fallback?
4. **Voice, exports, image tools**: port, keep a small Python sidecar for them, or drop any
   of them when Python goes?
5. **Tauri 2 vs waiting for Tauri 3**: 2.12 is stable and what this is built on; 3 is alpha.
   Recommend 2 now and a migration cut later.
6. **Linux**: build and publish `.deb`/`.AppImage` from CI from the first shell release, or
   macOS + Windows only as today?
