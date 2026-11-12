# ConcordeAI in C++: the plan

Patrick's call (2026-11-10): ConcordeAI moves to **C++, everything at once**: the window
(today the Tauri shell, `shell/src-tauri/`) and the engine (today `millenai.py`: the HTTP
server, the model orchestration, the page served as strings). It runs on a **separate
track**. The betas keep shipping from the code that ships today (Tauri shell + Python
server) until the C++ app catches up; nothing here changes what a person installs until
the C++ app passes the same tests the shipping app passes.

The approach is the **strangler** RUST_PLAN.md chose, now in C++: the product never stops
working. A C++ front owns the port and proxies everything to Python; routes move to C++
one at a time, each one proven identical; Python is retired when nothing is left behind
the proxy. Step 1 is built and proven on this branch (`cpp/`, NOTES 6b610). Estimates are
marked `*`.

---

## 0. What step 1 already shows (measured on this Mac, M4 Pro, 2026-11-10)

| | C++ (`cpp/`) | Tauri (7.0 beta 2, build 279) |
|---|---|---|
| Shell binary, universal | **0.6 MB** (`x86_64 arm64`) | 9.0 MB |
| `.app` | 4.1 MB (with `millenai.py`) | 12 MB |
| Window built (shell's own mark) | 168–187 ms | 164–187 ms |
| Server's notes read | 227–288 ms | 254–305 ms |
| Page loaded (`didFinishNavigation`) | 572–715 ms, median 616 | 605–719 ms, median 671 |
| Page's first contentful paint | 663–698 ms | (no mark in the Tauri shell) |
| Window on screen / page painted, by capture | 0.20 s / 0.93–1.05 s | 0.21–0.25 s / 0.98–1.00 s |
| White frames | none | none |
| Shell process RSS, 20 s after launch | **89–96 MB** | 107–113 MB |
| Shell + sidecar + its WebKit processes | 628–648 MB | 644–756 MB |
| Conformance, Python alone vs Python behind the C++ front | **83/83 identical**, heartbeat at 25.0 s both | — |

Four alternating rounds each after one uncounted launch, dev copies in fresh folders, the
same `millenai.py` and venv, the stand-in provider, local model calls recorded, no
pairing (`cpp/tests/bench/launch_ab.py`); the Tauri bundle is the installed 7.0 beta 2
copied under a test bundle id. Four of the eight window captures were unusable (the
window opened on another Space while the Mac was in use: missed, late, or a frame read
before the page could exist); the capture row is from the other four, the marks from all.

**What it says**: launch is a tie, because a launch is Python's boot plus WebKit's, and
both shells start them the same way; the C++ shell is ~15 MB lighter and 15× smaller on
disk. The front adds no measurable time: the page's first byte through it and straight
from Python are within each other's noise (Python renders the 740 KB page in 17–35 ms;
the splice is one more loopback hop). The wins this move is for (memory, launch, one
binary, in-process engines) come in steps 2 and 3, as RUST_PLAN.md already found for
the Rust shell; step 1 is a correctness step.

---

## 1. The contract (unchanged from RUST_PLAN.md §1)

Everything the page, the gauntlet and a paired server depend on stays byte-for-byte:
the HTTP surface (`http.server`, HTTP/1.0, one response per connection, `Cache-Control:
no-store`), the stream (`\0TAG:json\0` frames inline in `text/plain`, bare `\0RESET\0`,
the 20 s heartbeat that re-sends the last `STATUS`), `_gate` in its order (Host, no proxy
header, the `/?boot=` trade, the cookie `millen_key_<port>` compared on bytes, the
cookie-only allow-list, `X-Api-Token`, `X-Profile` → 409), `_csrf_ok`, the notes
(`run/instance.json` and `run/boot.json`, 0600), the dev-copy rules and exit codes
(1 port, 2 refused setting, 3 folder in use, 4 no window), the data folder and its file
formats, and the page's one bridge: `window.pywebview.api.api_token()`.
RUST_PLAN.md §1.1–1.5 is the reference text; it is not repeated here.

---

## 2. The shell

### 2.1 The choice: a thin hand-rolled layer per platform

| | webview/webview | saucer | hand-rolled (chosen) |
|---|---|---|---|
| What it is | one C/C++ header, WKWebView / WebView2 / WebKitGTK | modern C++23 framework, several backends, RPC "smartviews" | AppKit + WKWebView in Objective-C++; Win32 + WebView2 in C++ |
| Menus, traffic lights, close-to-hide, title bar | none: reach through `webview_get_window()` into native for all of it | partial; the Mac specifics still need Objective-C++ | all native, the exact calls tao made |
| Reply-style bridge (token), origin check per frame | async `bind()` over JSON, no frame info | yes | `WKScriptMessageHandlerWithReply` with `WKFrameInfo` (main frame, security origin); WebView2 `WebMessageReceived` with `Source` |
| Downloads, file picker, media permission, nav policy | not exposed | partly | all of it, ~60 lines each |
| Dependency risk | low, but we'd outgrow it on day one | young, small maintainer base, C++23 everywhere | none |
| Size | small | larger | **0.6 MB universal** today |

**Chosen: hand-rolled.** There is one window showing one page; every behaviour Patrick
asked for (the corner, the curated menu, close keeps the app, the fade-in, the
reconnect veil) is a native call, and the Mac half is already ~800 lines
(`cpp/src/mac/app.mm`). A library would sit between us and exactly the APIs we need.
webview/webview stays the reference if Linux (WebKitGTK) ever becomes a target.

### 2.2 What the Mac shell does (built, `cpp/src/mac/`)

The Tauri shell's behaviour, one for one (main.rs, 6b532–6b549), with the evidence:

- **Launch order**: the server starts first, the window is built while it boots; the
  webview has **no first document** and the first navigation is
  `http://127.0.0.1:<port>/?boot=<code>` (6b532's lesson: a first page of the shell's own
  made WebKit withhold the `SameSite=Strict` cookie). Proven: the page loads, the cookie is
  `HttpOnly` (the page can't read it), the home renders.
- **Hidden title bar, traffic lights**: `NSWindowStyleMaskFullSizeContentView`, transparent
  title bar, hidden title, dark appearance, window and webview the night `#03040a` before
  any paint (`drawsBackground = NO`, `underPageBackgroundColor`). The lights are seated with
  tao's own algorithm (`inset_traffic_lights`, tao 0.37) at `TL_X 16, TL_Y 38`, re-seated
  from the container's `layout` and on leaving full screen. Measured: close button at x 16,
  centre y **36.0** (6b545's target), green right edge 76.
- **The bridge**: `window.pywebview` defined at document start (main frame only), frozen,
  `platform: "concorde-cpp"`, over one `concorde` message handler with reply; the token is
  answered only when the asking frame is the main frame on `http://127.0.0.1:<the front's
  port>` **and** the webview's URL is that origin. The shim holds no secret.
- **The drag region**: the page's `data-tauri-drag-region` keeps working: a primary press
  on such an element posts `drag`, and the native side starts
  `performWindowDragWithEvent:` with the press itself, only while the button is still down;
  a double click follows the system's title-bar preference (zoom / minimise / none).
- **Menus**: ConcordeAI (About, Settings… ⌘,, Import Chats…, Check for Updates…, Hide,
  Hide Others, Quit), Edit (the native responders), View (Toggle Sidebar ⌘\, Enter Full
  Screen, Reload in dev builds), Window (Minimize, Zoom). Items call the page's own
  functions, as main.rs's `menu_action` does.
- **Close hides, Dock click and second launch reopen**: one window per data folder
  (`run/cpp-shell.lock` + a distributed notification named for the folder's hash, the same
  rule 6b548 gave the single-instance plugin).
- **Window place**: the Tauri shell's `shell-window.json`, same coordinates (top-left, in
  points, from the primary screen), same 160 × 120 pt on-screen rule, so a folder keeps
  its place across the two shells.
- **Crash restart**: any stop but 0/2/3/4 puts the Tauri shell's own "Reconnecting…" veil
  up and restarts after 1, 2, 4 s; after three in a row the plain error page with the log's
  tail; 60 s of health clears the tally. Exit 0 (the updater's hand-off) quits the app.
- **Quit**: Cmd+Q and SIGTERM both run `applicationShouldTerminate`: the place is written,
  the server gets SIGTERM, 8 s, SIGKILL. The server also follows its parent (6b532).
- **Page scripts**: `FADE_IN`, `RECONNECTING` and `BOOT_PY` are byte-identical to main.rs
  (checked by script); `CHROME` adds the drag rule and the paint marks.
- **Downloads** (`WKDownload`, to ~/Downloads, never over a file), the **file picker**
  (`runOpenPanelWithParameters`), `alert`/`confirm`, **microphone** granted to the app's
  own origin only (`requestMediaCapturePermissionForOrigin`; TCC still asks), links to
  other origins and `target=_blank` to the browser, a crashed WebContent process reloaded.
- **Timing and probe**: `CONCORDE_SHELL_TIMING=1` prints the Tauri shell's exact marks
  format, plus the page's first contentful paint; `CONCORDE_SHELL_PROBE` (dev builds) runs
  `cmd-<n>.js` as an async function in the page and answers `//native:lights`.

### 2.3 Still to do in the shell (step 1's remainder)

- **Dictation is native (6b642)**: the C++ shell captures (AVAudioEngine) and recognises
  (SpeechAnalyzer on macOS 26+, `voice_sa.swift` compiled into the same binary because the
  API is Swift-only; SFSpeechRecognizer on-device before 26) in its own process, and streams
  partials to the page over the window's `concorde` handler (`window.pywebview.api.voice`,
  events on `window.__concordeVoice`; contract in `cpp/src/mac/voice.mm`). No helper process
  and no Python on that path. The page uses it when the bridge offers `voice`; the Tauri
  shell keeps the getUserMedia → `/api/dictate` → `concorde-dictate` path. Proven with
  recorded clips (`cpp/tests/voice_proof.py`); a real microphone in the window is not.
- **Helpers**: `concorde-pdftext` (and `concorde-dictate`, for the Tauri shell and any page
  without the native bridge) are separate Swift binaries the *server* runs from beside
  `millenai.py`; the CMake bundle copies them into Resources when built
  (`shell/dictate/build.sh`, `shell/pdftext/build.sh`). Proof owed: `/api/doc/pdf` through
  the C++ bundle.
- **Haptics**: neither shell nor page has a bridge (6b553). If Patrick wants one, it is one
  more `op` on the same handler (`NSHapticFeedbackManager`), gated like the token.
- **First-run venv bootstrap** (main.rs `bootstrap_venv`, 6b537): one function, ported when
  the preview may run as the real app.
- **The updater**: **keep the in-app updater, not Sparkle/WinSparkle.** The C++ bundle keeps
  `Contents/MacOS/ConcordeAI` and `Contents/Resources/millenai.py`, so today's `_SWAP_SCRIPT`
  (waits for the executable, swaps the bundle, `open -n`) works unchanged; the C++ shell
  already quits on the server's exit 0. Sparkle would need an appcast, EdDSA keys and a
  second channel model beside `APP_BUILD`/prerelease betas, and switching updaters is how
  installs get stranded. In step 2 the updater moves into C++ with the same GitHub feed,
  channels and swap; Sparkle is reconsidered only if notarized delta updates matter.
  Proof owed before the C++ app ships: the stand-in-feed proof (6b537/6b549's `updproof`)
  run against the C++ bundle, Python install → C++ install and C++ → C++.
- **Signing**: the CMake build signs ad hoc with the shell's entitlements (audio-input
  only, 6b549). `build_shell_dmg.sh`'s Developer ID modes take a bundle as given, so the C++
  bundle drops into the same signing, notarization and stapling once it replaces the Tauri
  one; hardened runtime needs nothing new (no JIT in-process, nothing foreign loaded).
- **Bundle id**: the preview is `com.millen.millenai.cpp` so its WebKit storage and TCC
  grants never touch the shipping app's. At the switch it takes `com.millen.millenai`
  (the Local Network grant, the WebKit store and the updater's `pgrep` key on it).
- **Universal**: `CMAKE_OSX_ARCHITECTURES=arm64;x86_64` by default (proven: `lipo -archs`
  → `x86_64 arm64` on the app, the front and the tests). Once MLX is linked (step 3) the
  two slices are built separately and `lipo`'d, because MLX exists only for arm64.
- **Windows** (`cpp/src/win/app_win.cpp`, a stub with the design in its header): Win32 +
  WebView2 (the NuGet SDK, fetched by CMake on Windows), x64 and ARM64 with MSVC. The
  shared core (front, sidecar, notes) is written for Winsock/CreateProcess already, but
  **nothing Windows has been compiled**: the first CI run will find things. The sidecar on
  Windows needs a Job object (kill-on-close) because the server's parent watch is POSIX.

---

## 3. The engine

### 3.1 HTTP server: Boost.Beast (on Asio)

| | cpp-httplib | Drogon | Boost.Beast (chosen) |
|---|---|---|---|
| Model | blocking, thread pool | async framework (trantor), ORM, own JSON | async primitives on Asio, you write the server |
| Byte control | normalises headers (sorted map), its own error pages, chunked for streams | framework-shaped responses | exact: header order and case, HTTP/1.0 close-delimited bodies, http.server's error page bytes |
| Streaming | content providers (chunked) | yes | native, both directions, backpressure |
| Long streams (a council: minutes) | one pooled thread each | fine | a coroutine each; no thread pinned |
| Build (universal, Windows) | header-only, trivial | heavy (jsoncpp, trantor, optional DB drivers) | header-only Beast + Asio; Boost via CMake FetchContent or vcpkg |

**Chosen: Beast**, because conformance is the method: the recorder diffs status, headers
and bodies against Python's, and Python's quirks (HTTP/1.0, close-delimited streams, the
`send_error` template, `Cache-Control: no-store` from `end_headers`, the Range parse) must
be reproduced exactly. Asio's `co_await` gives the council a straight-line shape with
timers for the heartbeat and per-model timeouts on the same executor. cpp-httplib is
fine for tools and tests; Drogon is a framework we would fight.

**How Beast meets the step-1 front**: the front already reads each request head whole.
Step 2 adds a router there: a route C++ owns goes to a Beast session with the bytes
already read as its buffer prefix; every other route keeps the byte splice to Python (the
splice is what the 83/83 run proved; it stays until the last route moves).

### 3.2 The rest of the stack

| Need | Choice | Why |
|---|---|---|
| JSON | **nlohmann::json, `ordered_json`**, plus our own `pyjson::dumps` | Python dicts keep insertion order and `ordered_json` does too; `json.dumps`' bytes (`", "`/`": "`, `ensure_ascii`, float `repr`) need our 100-line emitter whichever library parses. glaze is faster, but JSON isn't where the time goes (the server's own overhead is ~3 ms a chat) and the code is dict-shaped |
| Outbound HTTPS (cloud providers, search, the update feed, server pairing) | **libcurl** (the macOS SDK's; vcpkg with Schannel on Windows) | streaming SSE through a write callback, honours `HTTPS_PROXY` (the gauntlet's stub proxy depends on it), our own User-Agent (the 403 trap), no OpenSSL to bundle on a Mac |
| Stores | the **file formats as they are** through step 2; **SQLite** (vendored amalgamation, FTS5, WAL) after | while Python and C++ share one folder the formats can't move; the know-you index (`index.db`) is already SQLite and reads as-is; chats, memory and the logs move to SQLite in step 3 with a one-way migration that keeps the JSON for one release (a rollback to a Python beta must still read the folder) |
| Gate secrets | **libsodium**: `randombytes_buf` + base64url = `token_urlsafe(32)`; `sodium_memcmp` = `compare_digest` | one audited library for all of it |
| Server pairing (the gateway, ollama1/PROTOCOL.md) | **libsodium** Ed25519 + HMAC, libcurl over the Cloudflare tunnel with the Access service token | PyNaCl *is* libsodium, so keys and signatures are byte-compatible; `servers.json` keeps its shape and its 0600 |
| Profiles/accounts crypto (`cai_crypto`) | libsodium `crypto_box`/`secretbox` | same reason |
| Processes | `posix_spawn` with `POSIX_SPAWN_CLOEXEC_DEFAULT` / `CreateProcessW` + Job objects | engines, `ssh`, `systemd-run`, Ollama |
| Tests | doctest (unit), libFuzzer (parsers), the Python gauntlet and recorder (over the wire) | |

### 3.3 The council, the compositor and the stream

One `Turn` per `/api/chat`, an Asio coroutine, the same lifecycle as CLAUDE.md's:
parse and alias tier names → resolve the council (compositor moved last) → agents /
images / docs hijack it → route resolution and engine pre-warm **before** search, on
their own task → headers out at once (the heartbeat starts after headers) → search branch
(weather / places / deep / plain), results on the turn, never a thread-local → stream →
`finally`: pins, the quality line, memory extraction on a detached task.

- `Stream` is the only writer: `emit(prose)` scrubs NUL, `frame(tag, json)` writes
  `\0TAG:json\0`, `reset()`; the heartbeat is a timer on the turn's strand that re-sends
  the last `STATUS` after 20 s of quiet, exactly as today (the recorder checks it fires at
  25.0 s).
- Drafts: local models **sequentially** (one MLX engine resident), every healthy cloud key
  **in parallel** (`co_spawn` + a parallel group), per-model and whole-loop deadlines as
  timers; a straggler is cancelled and simply absent. Reflection/peer review, then the
  compositor from `compositor_ladder()` (strength-first); the single-answer path uses
  `fast_cloud_ladder()` (speed-first). Providers resting on cooldown are healthy.
- Blocking model work (a llama.cpp decode) runs on a small dedicated pool and posts
  tokens to the turn's strand; nothing blocks the I/O threads.
- Approval (`APPROVE:` frame, Remote agent) is an awaitable completed by
  `/api/remote/approve`, replacing the `threading.Event`.

### 3.4 Local inference

- **llama.cpp, linked directly** (pinned git submodule, `add_subdirectory`, `GGML_METAL`
  on Apple silicon, CUDA or Vulkan on Windows), behind our own `Engine` interface. GGUF
  from the catalog. This replaces Ollama for local models on Windows and Intel Macs and is
  an option on Apple silicon.
- **MLX through its C++ API** on Apple silicon. Unlike Rust, C++ is MLX's native language
  (the Python package wraps it), so arrays, `mlx::core::fast` kernels, quantised matmuls
  and Metal streams are first-class. What is **not** in C++ is mlx-lm: model architectures,
  weight loading, KV cache, sampling and the tokenizer are ours.
  **The spike is done (NOTES 6b612, `cpp/spikes/mlx/`): GO, scoped.** Llama 3.2 1B 4-bit
  from C++ through MLX 0.32.4's C++ API: **256/256 greedy tokens identical to mlx-lm's**;
  time to first token 60.6 vs 77.5 ms, load 90 vs 603 ms, decode 272.9 vs 280.6 tok/s
  (2.7% behind; `mx::compile` on the step is the first fix to try), peak 861 vs 835 MB.
  The cost is coverage: the default tiers seat about six families (GPT-OSS, `gemma4` and
  its MoE and audio variants, the `qwen3_5` hybrid and its MoE, Ministral 3, Llama), most
  of them the hard kind, beside mlx-lm's shared layer (caches, samplers, streaming
  detokenizers, loader, prompt-cache reuse). Estimate*: the shared layer 3–4 weeks*, a dense
  family 2–5 days*, a hybrid/MoE/MLA family 1–2 weeks*, each gated on token identity against
  mlx-lm and decode within 5%. `mlx_lm server` stays the subprocess engine for every family
  not yet ported, behind the same `Engine` interface. Before writing families by hand, check
  ml-explore's Swift port (mlx-swift-lm): Swift links into the Objective-C++ app, and if it
  covers `gemma4` and `qwen3_5` the port is integration work. The tokenizer and templates are
  borrowed: `tokenizers-cpp` (HF tokenizers) and a C++ Jinja (minja). (*estimated)
- **Ollama stays** over HTTP: paired servers run it, people have it, Windows ARM needs it.
- Whisper (voice): whisper.cpp, when voice moves.

---

## 4. The strangler, step by step

Every step has the same three exit criteria, plus its own:
**(a)** the gauntlet green against the C++ server, **(b)** conformance diffs empty,
**(c)** the scorecard not worse (the answer bench, `--where local`, never the real server).

### Step 1: the C++ shell and a C++ front that proxies every route to Python

Built on this branch: `cpp/` (CMake), the Mac app, `concorde-front` (windowless, for the
gauntlet and the recorder), the unit tests and the fuzz target. The front owns the public
port; Python binds a private loopback port the front picks; the front edits exactly four
things in a head and passes every other byte (`cpp/src/core/rewrite.hpp`: Host, Origin,
the cookie's port in its name both ways, an absolute Location) and adds nothing (no
`X-Forwarded-*`: the server refuses proxy headers). The notes are written again with the
public port and the front's pid, so every native caller finds the front.

No change to `millenai.py`: the window's sidecar runs as `MILLENAI_SHELL=tauri` (the
server's only accepted value; renaming it to `native` is one line in `_dev_home` when the
Tauri shell retires).

**Exit criteria for step 1** (status):
- conformance 83/83 Python vs Python-behind-the-front — **done**, heartbeat 25.0 s both;
- the gauntlet green with every copy behind `concorde-front` — `SMOKE_FRONT` **added**
  (6b611); the full run's result is in NOTES 6b613;
- window parity — **done** (6b611): `window_e2e.py` 10/10, `shell_proofs.py` 10/10 (PDF
  through the bundled PDFKit helper, dictation through the bundled system recognizer,
  close/reopen/second launch, crash restart to the error page), `updater_proof.py` 7/7 (the
  relaunch, after a fix: `CONCORDE_PUBLIC_PORT`), `bootstrap_proof.py` 6/6 (the first-run
  venv); still owed: getUserMedia in the C++ window (one ⌘D hold by Patrick);
- launch and memory not worse than the Tauri shell (§0.1);
- the Windows WebView2 shell compiling and running in CI.

### Step 2: routes move to C++ one at a time

The router in the front decides per route; the recorder grows an entry for every route
before it moves (record Python, replay against C++, diff). Order, lowest risk and highest
value first (RUST_PLAN.md §3's order, kept):

1. **Gate + page + `/static/`** (rust-core 2.1 did this in Rust: reuse its findings:
   `send_error` escapes with `quote=False`, a chunked POST is passed, not gathered; the
   Range quirks; the ETag `"b<build>-<mtime>-<tag>"`; the page template still read from
   `millenai.py` at start, the token values fetched per render until their owners move).
   The C++ side mints the key, the token and the boot code from here on.
2. **Stores** (prefs, chats `.v2`, memory, usage/quality logs, the profile seam as an
   explicit `Ctx`), byte-compatible, `run/instance.lock` taken by C++.
3. **Read-mostly** `/api/stats`, `/api/tiers`, `/api/engines`, `/api/setup`, catalog and
   RAM fit.
4. **Cloud providers**: save/validate/discover, cooldowns, balance, both ladders. The
   probe payload is the runtime payload (the trap).
5. **Servers**: list, models, wake, sleep, usage, pairing (libsodium).
6. **`/api/chat` and the council** (§3.3), engines still subprocesses over HTTP. Behind a
   flag, A/B on recorded prompts.
7. **Search family** (plain, weather, OSM places, deep research, grounded): `ddgs` has no
   C++ twin; port its HTML-result path or keep it last in Python.
8. **Remote SSH agent** (the classifier first: pure, gauntlet-covered).
9. **Benchmarks, the updater, first-run setup and downloads.**
10. **Voice, exports, image tools, funnels**: port what has a good library; the rest is a
    small Python helper or dropped with Patrick's say-so.
11. **Profiles/accounts/crypto** when the accounts work resumes.

**Exit criteria per route**: its recorder entries identical; its gauntlet checks green
behind the front; its source pins moved (§5); the scorecard not worse.

### Step 3: Python retires

In-process engines (§3.4), the stores on SQLite, the page out of `millenai.py` into
`page/` (embedded at build time), the venv bootstrap and the pip pins deleted, the bundle
loses Resources/millenai.py. **Exit**: no route left behind the proxy for one full beta
cycle; the gauntlet, the recorder (now C++ vs the last Python build's recordings) and the
scorecard all green; an update from the last Python-backed build lands cleanly.

---

## 5. The gauntlet

`tests_smoketest.py` stays Python and keeps its three surfaces:

- **Live HTTP** and **the served HTML**: unchanged. They run against whatever owns the
  port. Add `SMOKE_FRONT=<concorde-front>` so every copy it starts runs behind the C++
  front (the shell-mode copies, which set `MILLENAI_SHELL` themselves, keep running Python
  directly, as with `SMOKE_CORE`).
- **Source text** (`_MILLENAI_SRC`): checks on code still in Python are untouched. When a
  subsystem moves, each of its source pins is either (1) replaced by a behavioural check
  over HTTP that kills the same mutant, (2) replaced by a C++ unit test, or (3) re-pinned
  on the C++ source the way the 6b532/6b548 checks pin `main.rs` (a `_CPP_SRC` reader over
  `cpp/src/**`). A pin is retired only when its mutant is killed by the replacement (the
  gauntlet's mutant lists are re-run against C++). The page's pins keep working while the
  page lives in `millenai.py`; at step 3 `_MILLENAI_SRC` reads `page/` as well.
- Ports and rules are unchanged: 9901–9903 for its copies, never the real server.

---

## 6. Risks

- **Memory safety.** C++ gives up what Python and Rust gave us for free. The countermeasures
  are standing CI jobs, not intentions: ASan+UBSan and TSan builds of every test
  (`-DCONCORDE_SANITIZE=ON`); libFuzzer on every parser that touches outside bytes (the
  head rewriter and note reader today, `cpp/tests/fuzz_rewrite.cpp`; next: the Range
  header, SSE from providers, the frame writer, the `.v2` chat loader, the PROTOCOL.md
  messages); clang-tidy (`bugprone-*`, `cert-*`, `cppcoreguidelines-pro-bounds-*`,
  `-Werror` in CI) over `compile_commands.json`; libc++ hardening
  (`_LIBCPP_HARDENING_MODE_EXTENSIVE`); no raw `new`/`delete`, spans and string_views at
  boundaries. Found already by the step-1 tests: an inherited listening socket let Python
  accept the front's connections itself (fixed with `POSIX_SPAWN_CLOEXEC_DEFAULT` and
  `FD_CLOEXEC`), the kind of bug only a running test shows.
- **The MLX language-model layer** is the largest unknown (§3.4). Spike before committing.
- **Python-only libraries**: `ddgs`, `huggingface_hub`, reportlab/docx/pptx exports, Pillow,
  `mlx_whisper`. Each needs a C++ equivalent or stays in a shrinking Python helper.
- **Two codebases at once.** Betas keep changing `millenai.py` while C++ ports it. The
  recorder and the gauntlet catch drift on moved routes; an agent owns re-recording when
  Python changes a moved route.
- **The conformance corpus is only as good as its entries**: every route gets entries
  before it moves, including its refusals.
- **Windows** has had no run of anything in this plan.
- **Reviewability**: agents can write C++ faster than it can be reviewed; every PR is one
  route or one subsystem, with its recorder diff and sanitizer run attached.

---

## 7. A team of agents

One subsystem per agent, one branch each, acceptance = the exit criteria above.

| Lane | Owns | First deliverable |
|---|---|---|
| Referee | the recorder, `SMOKE_FRONT`, the mutant re-runs, the scorecard | the full gauntlet behind `concorde-front` |
| Shell-Mac | `cpp/src/mac` | dictation, PDF, updater relaunch, crash/close proofs |
| Shell-Win | `cpp/src/win`, CI | WebView2 window, x64 + ARM64 builds green |
| Front/Gate | the router, Beast, gate, page, static | step 2.1 |
| Stores | files now, SQLite later | step 2.2 |
| Cloud | libcurl, providers, ladders | step 2.4 |
| Council | `Turn`, `Stream`, drafts, compositor | step 2.6 (after 2.4) |
| Servers | pairing, libsodium, wake/sleep | step 2.5 |
| Search, Remote | their families | 2.7, 2.8 |
| Engines | llama.cpp link, the MLX spike | in parallel from 2.3 |
| Packaging | signing, DMG, updater port | the C++ bundle through `build_shell_dmg.sh` |

The integrator merges in step order; nobody but the Referee edits the gauntlet; nobody
edits `millenai.py` except to keep a moved route's Python twin in step with a beta.

---

## 8. Timeline, in steps

1. **1a** (this branch): the Mac shell, the front, the notes, conformance 83/83, the
   window proven end to end. Done.
2. **1b**: `SMOKE_FRONT` and a full gauntlet behind the front; the shell's owed proofs
   (done, 6b611/6b613), the MLX spike (done, GO, 6b612).
3. **1c**: Windows WebView2 shell in CI. **1d**: the C++ bundle through signing and the
   updater proof; then it can replace the Tauri bundle in a beta, still Python inside.
4. **2.1–2.5** in order, engines spike alongside.
5. **2.6** (the council), the largest single piece; then 2.7–2.11.
6. **3**: in-process engines, SQLite stores, the page out of `millenai.py`, Python gone.

---

## 9. What the Rust work gives this plan

- **rust-core 2.1** (branch `rust-core`, 2985007): the conformance recorder, now in
  `cpp/tests/conformance/record.py` (four small changes, listed in its docstring); the
  gate/page/static quirks list (§4 step 2.1); the `/_core/*` handshake design, if step 2.1
  wants the template from Python again.
- **The Tauri shell** (`shell/src-tauri/src/main.rs`): every behaviour in §2.2 is a port of
  it; `FADE_IN`, `RECONNECTING` and `BOOT_PY` are copied byte for byte; the timing marks
  share its format so one bench measures both; the window-place file is shared.
- **RUST_PLAN.md**: the contract (§1), the subsystem inventory and the porting order.
- The Tauri shell keeps shipping betas until the C++ bundle passes 1d; `rust-core` stays
  parked as reference. No further Rust work is planned.

---

## 10. Build and run

```bash
# out of the Drive folder is better (build trees don't belong in Drive)
cmake -S cpp -B /tmp/cpp-build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/cpp-build && ctest --test-dir /tmp/cpp-build

# the window as a dev copy (never 8889, 9901-9903; never a copied servers.json)
MILLENAI_DEV=1 MILLENAI_HOME="$HOME/Library/Application Support/MillenAI-cpp" MILLENAI_PORT=9893 \
  CONCORDE_SERVER="$PWD/millenai.py" CONCORDE_SHELL_TIMING=1 \
  /tmp/cpp-build/ConcordeAI.app/Contents/MacOS/ConcordeAI

# windowless, for the gauntlet and the recorder
MILLENAI_DEV=1 MILLENAI_HOME=… MILLENAI_NOWINDOW=1 MILLENAI_PORT=9893 \
  CONCORDE_SERVER="$PWD/millenai.py" /tmp/cpp-build/concorde-front

# the proofs
python3 cpp/tests/e2e_front.py /tmp/cpp-build/concorde-front
python3 cpp/tests/window_e2e.py /tmp/cpp-build/ConcordeAI.app
python3 cpp/tests/conformance/record.py . --front /tmp/cpp-build/concorde-front
python3 cpp/tests/bench/launch_ab.py --app cpp=… --app tauri=<a copy with a test bundle id>

# the fuzzer (Homebrew LLVM ships libFuzzer; Apple clang does not)
cmake -S cpp -B /tmp/cpp-fuzz -DCONCORDE_FUZZ=ON -DCMAKE_CXX_COMPILER=$(brew --prefix llvm)/bin/clang++
cmake --build /tmp/cpp-fuzz --target fuzz_rewrite && /tmp/cpp-fuzz/fuzz_rewrite -max_total_time=60
```

The preview refuses to run anywhere but a dev copy (`preview_guard`: MILLENAI_DEV,
MILLENAI_HOME, a dev MILLENAI_PORT outside the app's, ConcordeGo's, the gauntlet's and the
engines' ports) unless built with `-DCONCORDE_ALLOW_REAL_APP=ON`. The `.py` tools run on
the app's venv (they use its Quartz and Pillow for the window checks).

---

## 11. Decisions

Approved by Patrick (2026-11-10): the hand-rolled shell, Boost.Beast, keeping and porting
the in-app updater, the MLX spike (done: GO, scoped, §3.4).

Still his:
1. **When the C++ bundle may replace the Tauri bundle in a beta** (after 1d, still with
   Python inside), and when it may take the shipping bundle id.
2. **MLX in C++, scoped as §3.4 says**: the shared layer and the default tiers' families
   first, `mlx_lm server` for the rest; and whether to check mlx-swift-lm before writing
   families by hand.
