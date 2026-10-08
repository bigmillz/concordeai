# ollama1 protocol: request signing and pairing (v1)

This is what the ConcordeAI app implements to use your own model server
("<server-name> · …" in the model picker: the app shows the name you gave
it). The server side is
`ollama1/bin/ollama1-gateway`; `ollama1/lib/o1auth.py` holds the exact
rules. The test vectors at the end are checked against the code by
`tests/test_vectors.py`, so this document and the gateway can't drift.

Everything here sits behind Cloudflare Access. The app adds two things:
a service-token header pair on every request (so Access lets it through)
and an Ed25519 signature from a key that was paired at the server (so
the gateway knows it is one of your devices). Both are required. Neither is
enough on its own.

There is no invite, share, join or pool. A key is added only while a
pairing window is open at the server, and the window closes after one
success.

## 1. Transport

- Base URL: `https://<server-name>.<your-domain>`
- Every request carries the Access service token:
  - `CF-Access-Client-Id: <client id>`
  - `CF-Access-Client-Secret: <client secret>`

  Access then adds `Cf-Access-Jwt-Assertion` on its way to the server,
  and the gateway checks that JWT (signature against the team's certs,
  issuer, AUD tag, expiry, and the service token's client id).
- A server set up without Cloudflare (`"access": "none"`, reached through
  an SSH tunnel; see `docs/your-own-server.md`) takes the same requests
  at the tunnel's local end, e.g. `http://127.0.0.1:8431`, without the two
  Access headers. Everything else here is the same, signatures included.
  Because a web page in the user's browser can reach that local port too,
  such a server refuses (403 `access`) any request whose `Host` isn't
  `127.0.0.1` or `localhost` (with or without the port), any request with
  an `Origin` header, and any POST without `Content-Type:
  application/json`. It won't start with `"access": "none"` while a
  Cloudflare tunnel is configured.
- Bodies are JSON (`Content-Type: application/json`) with a
  `Content-Length`. Chunked request bodies are refused (411). Signed calls
  may send up to 32 MiB, `/v1/pair` up to 8 KiB (413 otherwise). The body
  is only read after the Access check and the signature headers (a paired
  device, a fresh timestamp) pass.
- Every error response carries `Connection: close` and ends the connection.
  Open a new one for the next request.
- At most 16 requests are handled at once; beyond that the answer is 503
  `busy`.
- Store the client secret and the device's private key in the macOS
  Keychain. Never in a file, a log or a sync payload.

LAN mode (off unless the owner turns it on at the server) also accepts
`http://<server-ip>:8431` (the server's address on its LAN port) from `<lan-cidr>` without Access headers.
Signatures are still required. Traffic on the LAN is then unencrypted.
Pairing (`/v1/pair`) is refused on the LAN listener: it works only through
the tunnel, so a pairing request is never sent in the clear.

## 2. Device key

- On first use the app generates an Ed25519 key pair (RFC 8032) and
  keeps the 32-byte seed in the Keychain.
- `public_key` = the 32-byte public key, base64url **without padding**
  (43 characters).
- `device_id` = the first 16 lowercase hex characters of
  SHA-256(public key bytes). The server derives it the same way.

## 3. Pairing (POST /v1/pair)

The owner opens a window at the server (`sudo ollama1-pair`, or the admin
panel's button). For five minutes the server's screen and that terminal
show a code like `7K4M-2QXD-9FHT`: 12 characters (60 bits) of Crockford
base32 (`0-9 A-Z` without `I L O U`), in groups of four. The app asks the owner
to type it.

Normalize the code: uppercase, drop `-` and spaces, then `O`→`0`,
`I`→`1`, `L`→`1`.

```
key     = SHA-256( "ollama1-pair-key-v1\n" || normalized_code )        (32 bytes)
message = "ollama1-pair-v1\n" NAME "\n" PUBLIC_KEY "\n" TIMESTAMP "\n" NONCE
mac     = HMAC-SHA256(key, message)                                     (32 bytes)
```

- `NAME`: how the device shows up at the server, 1–40 printable
  characters, no newline, no leading/trailing spaces (UTF-8).
- `PUBLIC_KEY`: the unpadded base64url public key.
- `TIMESTAMP`: unix seconds, decimal, within 60 s of the server's clock.
- `NONCE`: 16–64 characters of `[A-Za-z0-9_-]`, fresh for every attempt
  (16 random bytes as base64url is the intended shape).

Request body (no signature headers on this one call; Access headers yes):

```json
{"name": "...", "public_key": "...", "timestamp": 1790000100,
 "nonce": "...", "mac": "<base64url of mac>"}
```

Responses:

| Status | `code` | Meaning |
|---|---|---|
| 200 | — | Paired. Body: `{"device_id", "name", "server_time", "proof"}` |
| 401 | `wrong_code` | Wrong code. `attempts_left` says how many remain |
| 403 | `pair_closed` | No window open, it expired, it already paired a device, or 5 wrong codes closed it |
| 429 | `rate_limited` | Less than 2 s since the last attempt. Wait and retry |
| 401 | `clock_skew` / `replay` | Timestamp off by more than 60 s / nonce reused |
| 400 | `bad_request` | Malformed field |
| 503 | `pair_not_saved` | The server didn't answer in 10 s. Open a new window |
| 403 | `pair_closed` | Also: the request came in on the LAN listener |

On 200 the app must check `proof` before trusting the pairing. It proves
the server knows the code too:

```
proof = HMAC-SHA256(key, "ollama1-pair-ok-v1\n" DEVICE_ID "\n" PUBLIC_KEY "\n" NONCE)
```

Compare in constant time, and check that `device_id` matches the one you
derived. If either check fails, discard the pairing.

The window allows 5 wrong codes, then closes. One success also closes it.
Wrong codes are also spaced: at most one attempt every 2 seconds. The
gateway never sees the code: it passes the request to a root-only step on
the server that checks the MAC, counts wrong codes, refuses a reused
nonce, adds the key and computes `proof`.

## 4. Signed requests

Every other call carries four headers:

| Header | Value |
|---|---|
| `X-O1-Device` | `device_id` |
| `X-O1-Timestamp` | unix seconds, decimal |
| `X-O1-Nonce` | 16–64 characters of `[A-Za-z0-9_-]`, never reused |
| `X-O1-Signature` | base64url (padding optional) Ed25519 signature over the canonical string |

Canonical string, UTF-8, joined with `\n` (no trailing newline):

```
ollama1-req-v1
METHOD              uppercase, e.g. POST
PATH                exactly as sent, including any query string
BODY_SHA256         lowercase hex SHA-256 of the exact body bytes (empty body: e3b0c442…b855)
TIMESTAMP
NONCE
DEVICE_ID
```

Sign the exact bytes you send. Don't re-serialize the JSON after signing.

The gateway refuses a request when:

| Status | `code` | Why |
|---|---|---|
| 403 | `access` | Access JWT missing or invalid, wrong host, or another service token |
| 401 | `unsigned` | A signature header is missing |
| 403 | `unpaired` | The device id isn't paired |
| 401 | `clock_skew` | Timestamp more than 60 s from the server's clock, or dated before the gateway last started |
| 401 | `bad_signature` | The signature doesn't verify |
| 401 | `replay` | This nonce was already used |

Every error body is `{"error": "<text>", "code": "<code>", "server_time": <unix s>}`.
On `clock_skew` the app may compute its offset from `server_time`, re-sign
with a fresh nonce and retry once. After a gateway restart, requests signed
before the restart are refused with `clock_skew`; a retry fixes that.

## 5. What the gateway passes on

Ollama's own API shapes, minus anything that changes state on the server:

| Call | Notes |
|---|---|
| `GET /v1/whoami` | `{"device_id", "name", "server_time"}`: a cheap "am I paired" check |
| `GET /v1/info` | `{"gpu": {"vendor", "name", "vram_bytes"}}`, see below |
| `GET /v1/usage` | `{"gpu": {"busy_pct", "vram_used_bytes", "vram_total_bytes"}, "ram": {"used_bytes", "total_bytes"}}`, see below |
| `GET /v1/sleep-config` | `{"enabled", "minutes", "supported", "wake"}`: the auto sleep setting, see below |
| `POST /v1/sleep-config` | `{"enabled": bool, "minutes": int}` (either): saves it and answers as the GET does |
| `GET /v1/models/state` | The models on the server, its allow-list, and the model set being applied or last applied: see "Model sets" below |
| `POST /v1/models/apply` | `{"plan", "add", "remove", "seen"}`: apply a model set chosen in the app, see "Model sets" below |
| `GET /api/tags` | Installed local models. Ollama cloud models and Ollama's own cache copies (`llamacpp:<sha>`) are never listed. Each has `placement` and `gpu_pct` (below) |
| `GET /api/ps` | Loaded models, with `size`, `size_vram`, `placement` and `gpu_pct` |
| `GET /api/version` | `{"version"}` |
| `POST /api/show` | `{"model"}` → Ollama's model description |
| `POST /api/chat` | Streams NDJSON by default, as Ollama does |
| `POST /api/generate` | The same |
| `POST /api/embed`, `POST /api/embeddings` | Whole JSON answers |

`GET /v1/info` says which GPU the server has, and nothing else about its
hardware. It is detected once, when the gateway starts:

```json
{"gpu": {"vendor": "amd", "name": "Radeon RX 6900 XT", "vram_bytes": 17163091968}}
```

- `vendor`: `"amd"`, `"nvidia"`, `"intel"` or `null` (no GPU found).
- `name`: a plain card name, or `null` if the server can't tell.
- `vram_bytes`: an integer, or `null` if unknown (e.g. an integrated Intel
  GPU).

All three keys are always there. The signed request is the third entry in
the test vectors below.

`GET /v1/usage` says how busy that card is right now, and how much of the
server's memory is in use, for the app's meters:

```json
{"gpu": {"busy_pct": 37, "vram_used_bytes": 9126805504, "vram_total_bytes": 17163091968},
 "ram": {"used_bytes": 23622320128, "total_bytes": 66571993088}}
```

- Exactly these keys, always there. `ram` is the server's memory: `used_bytes`
  is what Linux calls memory in use (`MemTotal` minus `MemAvailable` in
  `/proc/meminfo`, so the file cache doesn't count), `total_bytes` is
  `MemTotal`. A kit from before `ram` was added leaves it out: treat that as
  "not reported". Each is an integer, or `null` when
  the server can't read it (the card has no such file, or there is no card):
  a missing reading is `null`, never an error and never a guess.
- `busy_pct` is 0 to 100. On AMD it is the card's `gpu_busy_percent`; on
  NVIDIA, `nvidia-smi`'s utilization.
- Nothing else is in the answer (five numbers): not which models are loaded, not which
  device is using the card, not what was asked, not how many requests there
  were. A paired device already sees the card's name and size.
- Signed and paired-only, like every route here. The server reads the card
  at most once a second however many devices ask, so poll every few seconds
  at most. A server that doesn't have this route (an older kit) answers 404:
  treat that as "usage not reported".

`GET /v1/sleep-config` and `POST /v1/sleep-config` are the auto sleep setting
(the server suspends itself after it has been idle for a while, and a magic
packet wakes it). Signed and paired-only like every route here. The answer is
always exactly these four keys:

```json
{"enabled": false, "minutes": 30, "supported": true, "wake": ["02:00:5e:10:00:01"]}
```

- `enabled`: a boolean, false until someone turns it on.
- `minutes`: how long with no real work before it sleeps, a whole number, 5 to
  1440 (default 30).
- `supported`: true if the machine lists deep sleep (`deep` in
  `/sys/power/mem_sleep`).
- `wake`: the MAC addresses (lower case, at most 8) of the network cards, real
  ones only, standalone or inside a bridge, that have wake on a magic packet
  switched on. Empty if none, or if the sleep service hasn't published a list.
- `POST` takes `enabled` (a boolean) and/or `minutes` (a whole number: a
  boolean, a string or a decimal is a 400; a number outside 5..1440 is
  brought to the nearest end). Any other key is a 400. An older kit answers
  404 `not_found`: treat that as "update the server kit".
- Only chat, generate and embeddings count as work, and so does a picture or
  video job (see "Images and video" below) from its start until two minutes
  after the last poll or fetch of it. A model set the server accepts counts
  once, when it is accepted; the server then doesn't sleep until the set is
  done. `/v1/info`, `/v1/usage`, `/v1/whoami`, the model list,
  `/v1/models/state`, `/v1/generate/capabilities` and this route never keep
  the server awake.

To wake it, send a magic packet: six `0xFF` bytes then the card's address
sixteen times, as a UDP broadcast to port 9, and ask `/v1/info` every couple of
seconds until it answers.

### Images and video (optional)

A server that also runs ComfyUI (`ollama1/tools/install-comfyui.sh`; nothing
installs it unasked) can make pictures and short clips. Four routes, signed and
paired-only like every other one. The app never sends a ComfyUI graph: it sends a
kind, a prompt and a few numbers, and the gateway puts them into a fixed
template (`lib/o1gen.py`: FLUX.1 schnell fp8 for pictures, Wan 2.1 1.3B for
video). Anything not listed below is a 400.

| Call | Notes |
|---|---|
| `GET /v1/generate/capabilities` | `{"image": bool, "video": bool}`, exactly those two keys. True only when ComfyUI answers on the server's own loopback address and the model files and nodes the template needs are there. A kit without the route answers 404 `not_found`: treat that as neither. Cached for 20 s on the server; never counts as work |
| `POST /v1/generate/jobs` | Starts a job: **202** `{"id"}`, an unguessable 32-character id. Body below. Errors: 400 `bad_request` (a number or key outside the caps), 409 `busy` (a job is running, or a chat holds the card), 503 `unavailable` (that kind isn't set up) |
| `GET /v1/generate/jobs/<id>` | `{"state": "queued" or "running" or "done" or "failed", "kind", "progress"}`. `progress` is 0 to 1 while running and `null` when the server can't say; a done job adds `"type"` (the result's content type) and `"bytes"`; a failed one adds `"error"`: always the same one message, `"generation failed"`, never the cause. Poll every 2 s |
| `GET /v1/generate/jobs/<id>/result` | The bytes, with `Content-Type`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. 409 `not_ready` until the job is done |
| `DELETE /v1/generate/jobs/<id>` | Stops a running job (the card is freed) or forgets a finished one: **200** `{"ok": true}` |

A job belongs to the device that started it: another paired device gets 404 for
its id. An id that isn't there (never made, expired, stopped, someone else's) is
404 `not_found`.

The job body (any other key is a 400):

| Key | Image | Video |
|---|---|---|
| `kind` | `"image"` | `"video"` |
| `prompt` | 1 to 1000 characters | the same |
| `width`, `height` | multiples of 64, 256 to 1536, at most 1,700,000 pixels; default 1024 each | multiples of 16, 256 to 832, at most 400,000 pixels; default 832 by 480 |
| `steps` | 1 to 12, default 4 | 10 to 30, default 20 |
| `seed` | optional whole number, 0 to 2^53; random if left out | the same |
| `seconds` / `frames` | n/a | `seconds` 1 to 5 (default 2) or `frames`, 17 to 81 and one more than a multiple of 4 (`frames` wins). 16 frames a second: 2 s is 33 frames |

The result is a PNG (or JPEG) for a picture. For a video it is an MP4 with H.264
(`video/mp4`, which WKWebView and Safari play) when the server's ComfyUI has the
`CreateVideo` and `SaveVideo` nodes; a ComfyUI without them is made to write an
animated WebP instead (`image/webp`, which a browser draws in an `<img>`). Read
`type`, don't assume. The gateway checks the first bytes of the file and relays
nothing that isn't a picture, an MP4 or a WebP of the right kind, at most 16 MiB
for a picture and 64 MiB for a video.

How a job shares the card with chats:

- One job at a time, and not while a chat runs (409 `busy`). While a job runs, a
  chat or embedding request is answered 503 `busy` straight away; lists, `/v1/info`
  and polls still work.
- Before a job the gateway asks Ollama to unload every model it has loaded, waits
  (up to 30 s) until the card reads free, then starts. If the unload can't be
  confirmed the job fails and ComfyUI is never asked. After the job, success or
  not, it tells ComfyUI to unload its models and free its memory, so the next chat
  can load.
- A running job nobody has polled for 120 s is stopped. A job runs for at most 10
  minutes (picture) or 30 (video), whatever the app does.
- The job's state, and a finished job's bytes, are kept in the gateway's memory
  only, for 10 minutes after it finished and for at most two finished jobs. Nothing
  is written to disk by the gateway and the prompt is in no log. ComfyUI writes its
  own result file (to `/run`, which is memory, swept every 10 minutes) and forgets
  the prompt when the job ends.
- Nothing is shared between devices: only the device that started a job reads it.

The tunnel drops a response that stays silent for about 100 s, so none of these calls
waits for a job: start it, then poll.

### Model sets

The person picks a set for the server in the app (Light, Recommended or
Everything). The app works out which models that means for the card in
`/v1/info`, shows the exact Download and Remove lists, and sends them. The
gateway can't change a model itself: it checks the request and hands it to a
root step on the server, which checks all of it again, edits the server's
allow-list and has Ollama delete and download. Both routes are signed and
paired-only. A kit without them answers 404 `not_found`: treat that as "update
the server kit".

`GET /v1/models/state` answers exactly these keys:

```json
{"models": [{"name": "gemma4:12b", "size": 7600000000, "loaded": false}],
 "allow": ["gemma4:12b"],
 "busy": false,
 "jobs": [{"name": "gpt-oss:20b", "action": "pull", "state": "running", "pct": 41, "error": ""}],
 "plan": {"name": "recommended", "at": 1791100000, "by": "Alice's Mac"},
 "disk_free_bytes": 812345678912,
 "vram_bytes": 17163091968}
```

- `models`: what Ollama has on the server's disk, sorted by name; `size` in
  bytes; `loaded` is true while the model is in memory. Ollama cloud entries
  and Ollama's own cache copies (`llamacpp:<sha>`, `llamacpp/...`) are never
  listed (kits before 6b448 listed the copies; the app hashes `seen` over every
  name it was sent, and never shows or removes one).
- `allow`: the names on the server's allow-list, in its order (a line it
  can't read is left out).
- `busy`: true while a set is being applied, while a model-library sync or
  `ollama1-models` runs at the server, or while the admin panel pulls or
  removes a model. Applying then answers 409 `busy`.
- `jobs`: the jobs of the set being applied, or of the last one, kept until
  the next one starts (a refused request is not a set). Each is exactly
  `{"name", "action": "remove" | "pull", "state": "queued" | "running" |
  "done" | "failed", "pct": 0 to 100, "error"}`, in the order they run;
  `error` is `""` unless the job failed. A set the server couldn't finish (it
  restarted, say) shows its unfinished jobs as failed.
- `plan`: that set's name, when the server accepted it (unix seconds) and the
  device's name from the server's own list of paired devices; `null` if no
  set was ever applied.
- `disk_free_bytes`: free space on the models disk (0 if it can't be read).
  `vram_bytes`: the card's VRAM as in `/v1/info`, else the server's configured
  figure, else 0.

`POST /v1/models/apply` takes exactly these four keys:

```json
{"plan": "recommended", "add": ["gpt-oss:20b"], "remove": ["llama3.2:3b"], "seen": "<hex sha256>"}
```

- `plan`: `"light"`, `"recommended"` or `"everything"`.
- `add`, `remove`: at most 40 tags each, from Ollama's own library only:
  `^[a-z0-9][a-z0-9._-]{0,79}(:[a-z0-9][a-z0-9._-]{0,63})?$` and no `..` (no
  host, no namespace, no `/`). No model twice (`x` is `x:latest`), none in both
  lists, not both empty, no Ollama cloud model to add, no Ollama cache copy
  (`llamacpp:...`) in either list.
- `seen`: the hex sha256 of the model names the app saw: `"\n".join(sorted(names))`
  in UTF-8, where `names` are the `name`s in `models` above (the hash of no
  models is the hash of the empty string). It proves the lists were made from
  what is on the server now; if anything changed, nothing is done.

| Status | `code` | Meaning |
|---|---|---|
| **202** | | `{"ok": true, "jobs": [...]}`: accepted; the jobs in the order they run (the removals first) |
| 400 | `bad_request` | Unknown or missing keys, wrong types, a tag outside the pattern, more than 40 in a list, a model in both lists, nothing to add or remove, a set name outside the three |
| 409 | `changed` | `seen` isn't the hash of the models on the server now. The body has `"models"`: their names now. Nothing changed: rebuild the lists from those |
| 409 | `busy` | Something else is changing the models (see `busy` above) |
| 409 | `in_use` | A model to remove is loaded right now. The body has `"name"`. Nothing changed |
| 403 | `unpaired` | The server's root step no longer finds the device paired |
| 502 | `ollama` | Ollama isn't answering |
| 503 | `not_started` | The server couldn't start its root step (an incomplete kit: run its setup again) |
| 504 | `no_answer` | Started, but the server didn't confirm within 15 s: read `GET /v1/models/state` before sending it again |

Error bodies are the usual `{"error", "code", "server_time"}`, plus `models` or
`name` as listed.

What the server does with an accepted set:

- Its allow-list gains a line for each tag to add (at the end, with no flag)
  and loses every line naming a tag to remove; nothing else on it changes. A
  later model-library sync at the server follows that list, so it doesn't
  undo the set (it would remove other models that aren't on the list, as it
  always has).
- The removals run first, so their space is there for the downloads, then the
  downloads in the order given. Each download is tried 3 times; one that
  still fails is marked failed and the next one runs. A removal is not done
  (failed) if the model is loaded at that moment or is back on the
  allow-list; a download is not done if it was taken off the list meanwhile
  or the models disk has less than 2 GiB free. The server doesn't check that
  everything fits before it starts: use `disk_free_bytes`.
- The POST returns as soon as the server has accepted the set (normally
  within a few seconds); the work goes on after it. Poll `GET
  /v1/models/state` every few seconds until `busy` is false; `pct` moves at
  most once a second.

Anything else is a 404: `pull`, `delete`, `create`, `copy`, `push` and `blobs`
are never reachable from outside. A paired device changes which models the
server has only through a model set, above; everything else about models is
managed at the server.

Request shaping (so every request stands alone and stays on the GPU):

- Only known top-level fields go through (chat: `model messages tools
  format options stream think logprobs top_logprobs`; generate: `model
  prompt suffix images format options system template stream raw context
  think logprobs top_logprobs`; embed: `model input truncate options
  dimensions`). `keep_alive` is dropped: the server decides how long
  weights stay loaded. The one exception is Ollama's own unload: a
  `chat` or `generate` with `keep_alive: 0` (or `"0"`, `"0s"`) and nothing
  to answer (no `prompt`, no `messages`) unloads the model, waits for
  `/api/ps` to stop listing it (up to 5 s) and answers Ollama's own
  `{"model", "done": true, "done_reason": "unload"}`; 503 `busy` if it
  would not go, 404 if it isn't installed. It takes the GPU's turn like a
  request, so it waits behind an answer on the card rather than pulling
  the weights from under it (6b454: the benchmark empties the card
  before each model so every load is measured clean).
- A GPU-only model that loads partly into system memory beside another
  loaded model (Ollama 0.40 fits a new model to the memory the card has
  free instead of evicting the one before it) is not refused at once:
  both are unloaded, the model is loaded again alone, and only a spill on
  an empty card is refused (507 `gpu_spill`, with `gpu_pct` and
  `loaded_with`, the models that were beside it). The journal says
  `make-room model=<the others>` then `gpu-spill-retry model=… gpu_pct=…`.
- An error after a stream's headers have gone out is its last line,
  `{"error", "code", "status", "done": true}`; `status` is the HTTP status
  it would have had, so a reader can tell the server's own hiccup (502,
  503) from a refusal of the model (404, 507).
- Only sampling options go through (`temperature top_k top_p min_p
  typical_p repeat_last_n repeat_penalty presence_penalty
  frequency_penalty seed stop num_predict num_keep penalize_newline
  mirostat mirostat_tau mirostat_eta tfs_z`). Options that change how a
  model loads (`num_gpu`, `main_gpu`, `num_thread`, `use_mmap`,
  `num_batch`...) are dropped.
- `options.num_ctx` is honored between 512 and the model's maximum;
  the default is 8192.
- The server keeps no chat history. Send the whole conversation each time.
- Nothing is shared between devices, not even Ollama's prompt cache: when a
  request comes from a different paired device than the one before it, the
  server unloads every loaded model first. Expect one model reload (a few
  seconds) when you switch devices. `prompt_eval_count` and
  `prompt_eval_duration` stay in the answers.

GPU only, unless the server's owner allows a model system memory:

- Every model runs entirely in VRAM, except models the owner marked `ram`
  in the server's allow-list (mixture-of-experts models such as
  gpt-oss:120b). Those may load partly into system memory, and run slower.
- `placement` tells the app which kind a model is, so it can label the
  slow ones:
  - in `/api/tags`: `"gpu"` (GPU only) or `"gpu+ram"` (may use system
    memory); `gpu_pct` is the share in VRAM while it is loaded, else `null`;
  - in `/api/ps`: the actual placement, `"gpu"` when 100% is in VRAM, else
    `"gpu+ram"`, and `gpu_pct` from 0 to 100.
- Before loading, the gateway estimates weights + KV cache for the
  requested `num_ctx` + a margin. It checks that against the card's VRAM
  less a reserve (16 GB here). A `gpu+ram` model is counted with 2.5 GiB of
  compute buffers, against the free system memory less the larger of 8 GiB
  and 12% of RAM, under Ollama's own memory cap. VRAM counts too only when
  the server has turned llama.cpp's weight repacking off, because the
  repacked CPU copy can be as big as the whole model. If it doesn't fit, the
  answer is **507** `gpu_fit`, with `need_bytes` and `budget_bytes`. Try
  a smaller `num_ctx` or a smaller model. For a `gpu+ram` model sent
  without `options.num_ctx`, the gateway first tries 4096, then 2048.
- After loading, a GPU-only model must show 100% in VRAM in `/api/ps`. If
  any of it landed on the CPU, the gateway unloads it and answers
  `gpu_spill` (507 for non-streamed calls).
- If the load itself runs out of Ollama's memory cap, the kernel stops it
  inside Ollama (the server stays up) and the answer is `ram_oom` (507).
- A `gpu+ram` model is unloaded and refused with `ram_pressure` (507) if
  loading it pushed the server into swap or left under 1 GiB free.
- A `gpu+ram` model loads alone: the server unloads other models first,
  and unloads it before a GPU-only model runs. Expect a reload when you
  switch between them.

One job runs on the GPU at a time; up to 8 more wait in a queue. When
the queue is full: **503** `busy`.

Streaming (`stream` true, the default for chat and generate): once the
request passes auth, validation and the fit estimate, the gateway sends
`200` with `Content-Type: application/x-ndjson` and then waits its turn and
loads the model. A failure after that point arrives as one last NDJSON
line: `{"error": "...", "code": "gpu_spill" | "ram_pressure" | "ram_oom" | "busy" | "ollama", "status": 507 | 503 | 502, "done": true}`.
The app must treat a line with `error` as the end of the answer.
Prefer streaming: Cloudflare ends a non-streamed request whose answer
takes longer than 100 s.

## 6. Test vectors

Ed25519 is deterministic, so these are exact. Inputs: the seed is bytes
0x00–0x1f; nonces are bytes 0x00–0x0f (request 1) and 0x10–0x1f
(pairing) as base64url. `canonical_utf8` and `message_utf8` show `\n`
as JSON escapes.

<!-- vectors:begin -->
```json
{
  "device": {
    "seed_hex": "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
    "public_key_b64url": "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg",
    "device_id": "56475aa75463474c"
  },
  "requests": [
    {
      "method": "POST",
      "path": "/api/chat",
      "body_utf8": "{\"model\":\"qwen3:8b\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"stream\":true}",
      "timestamp": 1790000000,
      "nonce": "AAECAwQFBgcICQoLDA0ODw",
      "body_sha256_hex": "971629aec4bcfb87a3ae1a44aec3a79f66345f75bbc253dbee39875e641bb305",
      "canonical_utf8": "ollama1-req-v1\nPOST\n/api/chat\n971629aec4bcfb87a3ae1a44aec3a79f66345f75bbc253dbee39875e641bb305\n1790000000\nAAECAwQFBgcICQoLDA0ODw\n56475aa75463474c",
      "headers": {
        "X-O1-Device": "56475aa75463474c",
        "X-O1-Timestamp": "1790000000",
        "X-O1-Nonce": "AAECAwQFBgcICQoLDA0ODw",
        "X-O1-Signature": "TeZDr7AmJfNXHJgLqdW4psu4pigqNgrIfYvVAPSVX-esbP5DvZyxh5HQE3L9TbylXr3JWbtEi7MsS9SHXKl1Ag"
      }
    },
    {
      "method": "GET",
      "path": "/api/tags",
      "body_utf8": "",
      "timestamp": 1790000030,
      "nonce": "n0nce-0000000000000001",
      "body_sha256_hex": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "canonical_utf8": "ollama1-req-v1\nGET\n/api/tags\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n1790000030\nn0nce-0000000000000001\n56475aa75463474c",
      "headers": {
        "X-O1-Device": "56475aa75463474c",
        "X-O1-Timestamp": "1790000030",
        "X-O1-Nonce": "n0nce-0000000000000001",
        "X-O1-Signature": "-8x2n5tYGxbmc6TAQmsPGASjru0Be7hR6AEJsuF70sXhkXT60PECqckwoIBQ9JCrHZpYYMeXJUDSb4kiVgwYDw"
      }
    },
    {
      "method": "GET",
      "path": "/v1/info",
      "body_utf8": "",
      "timestamp": 1790000060,
      "nonce": "n0nce-0000000000000002",
      "body_sha256_hex": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "canonical_utf8": "ollama1-req-v1\nGET\n/v1/info\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n1790000060\nn0nce-0000000000000002\n56475aa75463474c",
      "headers": {
        "X-O1-Device": "56475aa75463474c",
        "X-O1-Timestamp": "1790000060",
        "X-O1-Nonce": "n0nce-0000000000000002",
        "X-O1-Signature": "9FRnVU3bslBOhaVD_B20ugookQzoD_GXUBkbyZHtCEftTT2tEQxVCJldivSJwcyIdQ1XQoznGYChv9BuTZYyBQ"
      }
    }
  ],
  "pairing": {
    "code_shown": "7K4M-2QXD-9FHT",
    "code_normalized": "7K4M2QXD9FHT",
    "key_hex": "0cb34d68726b5dfa6196ddb20f2f9c3ea864b821b235bb545692bd0138e3502e",
    "name": "Alice's MacBook Pro",
    "public_key_b64url": "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg",
    "timestamp": 1790000100,
    "nonce": "EBESExQVFhcYGRobHB0eHw",
    "message_utf8": "ollama1-pair-v1\nAlice's MacBook Pro\nA6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg\n1790000100\nEBESExQVFhcYGRobHB0eHw",
    "mac_b64url": "xL_ruufA23aPmkbEgTPXlIefvH0I0N2uQ2AxpVCPZqE",
    "request_json": {
      "name": "Alice's MacBook Pro",
      "public_key": "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg",
      "timestamp": 1790000100,
      "nonce": "EBESExQVFhcYGRobHB0eHw",
      "mac": "xL_ruufA23aPmkbEgTPXlIefvH0I0N2uQ2AxpVCPZqE"
    },
    "response_device_id": "56475aa75463474c",
    "proof_b64url": "F_m7YSSE-XjggeJ1cJjieXKyDA-ZcpnHyV4vr20K6aQ"
  }
}
```
<!-- vectors:end -->

The JSON is generated by `python3 tests/gen_vectors.py`. The signatures
were made with the RFC 8032 reference code in `tests/o1test_util.py`
(itself checked against RFC 8032's TEST 1 and TEST 2) and verified by the
gateway's backend (libsodium via PyNaCl on the server, OpenSSL via
`cryptography` elsewhere).

A checklist for the app's implementation:

1. Derive `device_id` from the vector seed → `56475aa75463474c`.
2. Rebuild both canonical strings and signatures byte for byte.
3. Rebuild the pairing `key_hex`, `mac_b64url` and `proof_b64url`.
4. Accept `7k4m2qxd9fht` and `7K4M-2QXD-9FHT` as the same code.
5. Refuse a pairing response whose `proof` doesn't match.
