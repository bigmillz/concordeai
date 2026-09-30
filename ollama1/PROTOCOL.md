# ollama1 protocol: request signing and pairing (v1)

This is what the ConcordeAI app implements to use the desktop model server
("Desktop · …" in the model picker). The desktop side is
`ollama1/bin/ollama1-gateway`; `ollama1/lib/o1auth.py` holds the exact
rules. The test vectors at the end are checked against the code by
`tests/test_vectors.py`, so this document and the gateway can't drift.

Everything here sits behind Cloudflare Access. The app adds two things:
a service-token header pair on every request (so Access lets it through)
and an Ed25519 signature from a key that was paired at the desktop (so
the gateway knows it is Patrick's device). Both are required. Neither is
enough on its own.

There is no invite, share, join or pool. A key is added only while a
pairing window is open at the desktop, and the window closes after one
success.

## 1. Transport

- Base URL: `https://ollama1.flyconcordefly.com`
- Every request carries the Access service token:
  - `CF-Access-Client-Id: <client id>`
  - `CF-Access-Client-Secret: <client secret>`

  Access then adds `Cf-Access-Jwt-Assertion` on its way to the desktop,
  and the gateway checks that JWT (signature against the team's certs,
  issuer, AUD tag, expiry, and the service token's client id).
- A server set up without Cloudflare (`"access": "none"`, reached through
  an SSH tunnel; see `docs/your-own-server.md`) takes the same requests
  at the tunnel's local end, e.g. `http://127.0.0.1:8431`, without the two
  Access headers. Everything else here is the same, signatures included.
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

LAN mode (off unless Patrick turns it on at the desktop) also accepts
`http://192.168.86.10:8431` (the desktop's address on br0) from 192.168.86.0/24 without Access headers.
Signatures are still required. Traffic on the LAN is then unencrypted.
Pairing (`/v1/pair`) is refused on the LAN listener: it works only through
the tunnel, so a pairing request is never sent in the clear.

## 2. Device key

- On first use the app generates an Ed25519 key pair (RFC 8032) and
  keeps the 32-byte seed in the Keychain.
- `public_key` = the 32-byte public key, base64url **without padding**
  (43 characters).
- `device_id` = the first 16 lowercase hex characters of
  SHA-256(public key bytes). The desktop derives it the same way.

## 3. Pairing (POST /v1/pair)

Patrick opens a window at the desktop (`sudo ollama1-pair`, or the admin
panel's button). For five minutes the desktop's screen and that terminal
show a code like `7K4M-2QXD-9FHT`: 12 characters (60 bits) of Crockford
base32 (`0-9 A-Z` without `I L O U`), in groups of four. The app asks Patrick
to type it.

Normalize the code: uppercase, drop `-` and spaces, then `O`→`0`,
`I`→`1`, `L`→`1`.

```
key     = SHA-256( "ollama1-pair-key-v1\n" || normalized_code )        (32 bytes)
message = "ollama1-pair-v1\n" NAME "\n" PUBLIC_KEY "\n" TIMESTAMP "\n" NONCE
mac     = HMAC-SHA256(key, message)                                     (32 bytes)
```

- `NAME`: how the device shows up at the desktop, 1–40 printable
  characters, no newline, no leading/trailing spaces (UTF-8).
- `PUBLIC_KEY`: the unpadded base64url public key.
- `TIMESTAMP`: unix seconds, decimal, within 60 s of the desktop's clock.
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
| 503 | `pair_not_saved` | The desktop didn't answer in 10 s. Open a new window |
| 403 | `pair_closed` | Also: the request came in on the LAN listener |

On 200 the app must check `proof` before trusting the pairing. It proves
the desktop knows the code too:

```
proof = HMAC-SHA256(key, "ollama1-pair-ok-v1\n" DEVICE_ID "\n" PUBLIC_KEY "\n" NONCE)
```

Compare in constant time, and check that `device_id` matches the one you
derived. If either check fails, discard the pairing.

The window allows 5 wrong codes, then closes. One success also closes it.
Wrong codes are also spaced: at most one attempt every 2 seconds. The
gateway never sees the code: it passes the request to a root-only step on
the desktop that checks the MAC, counts wrong codes, refuses a reused
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
| 401 | `clock_skew` | Timestamp more than 60 s from the desktop's clock, or dated before the gateway last started |
| 401 | `bad_signature` | The signature doesn't verify |
| 401 | `replay` | This nonce was already used |

Every error body is `{"error": "<text>", "code": "<code>", "server_time": <unix s>}`.
On `clock_skew` the app may compute its offset from `server_time`, re-sign
with a fresh nonce and retry once. After a gateway restart, requests signed
before the restart are refused with `clock_skew`; a retry fixes that.

## 5. What the gateway passes on

Ollama's own API shapes, minus anything that changes state on the desktop:

| Call | Notes |
|---|---|
| `GET /v1/whoami` | `{"device_id", "name", "server_time"}`: a cheap "am I paired" check |
| `GET /api/tags` | Installed local models. Ollama cloud models are never listed. Each has `placement` and `gpu_pct` (below) |
| `GET /api/ps` | Loaded models, with `size`, `size_vram`, `placement` and `gpu_pct` |
| `GET /api/version` | `{"version"}` |
| `POST /api/show` | `{"model"}` → Ollama's model description |
| `POST /api/chat` | Streams NDJSON by default, as Ollama does |
| `POST /api/generate` | The same |
| `POST /api/embed`, `POST /api/embeddings` | Whole JSON answers |

Anything else is a 404: `pull`, `delete`, `create`, `copy`, `push` and `blobs`
are never reachable from outside. Models are managed at the desktop only.

Request shaping (so every request stands alone and stays on the GPU):

- Only known top-level fields go through (chat: `model messages tools
  format options stream think logprobs top_logprobs`; generate: `model
  prompt suffix images format options system template stream raw context
  think logprobs top_logprobs`; embed: `model input truncate options
  dimensions`). `keep_alive` is dropped: the desktop decides how long
  weights stay loaded.
- Only sampling options go through (`temperature top_k top_p min_p
  typical_p repeat_last_n repeat_penalty presence_penalty
  frequency_penalty seed stop num_predict num_keep penalize_newline
  mirostat mirostat_tau mirostat_eta tfs_z`). Options that change how a
  model loads (`num_gpu`, `main_gpu`, `num_thread`, `use_mmap`,
  `num_batch`...) are dropped.
- `options.num_ctx` is honored between 512 and the model's maximum;
  the default is 8192.
- The desktop keeps no chat history. Send the whole conversation each time.
- Nothing is shared between devices, not even Ollama's prompt cache: when a
  request comes from a different paired device than the one before it, the
  desktop unloads every loaded model first. Expect one model reload (a few
  seconds) when you switch devices. `prompt_eval_count` and
  `prompt_eval_duration` stay in the answers.

GPU only, unless the desktop's owner allows a model system memory:

- Every model runs entirely in VRAM, except models the owner marked `ram`
  in the desktop's allow-list (mixture-of-experts models such as
  gpt-oss:120b). Those may load partly into system memory, and run slower.
- `placement` tells the app which kind a model is, so it can label the
  slow ones:
  - in `/api/tags`: `"gpu"` (GPU only) or `"gpu+ram"` (may use system
    memory); `gpu_pct` is the share in VRAM while it is loaded, else `null`;
  - in `/api/ps`: the actual placement, `"gpu"` when 100% is in VRAM, else
    `"gpu+ram"`, and `gpu_pct` from 0 to 100.
- Before loading, the gateway estimates weights + KV cache for the
  requested `num_ctx` + a margin. It checks that against the card's VRAM
  less a reserve (16 GB here); for a `gpu+ram` model, against VRAM plus the
  free system memory less 6 GB for the system. If it doesn't fit, the
  answer is **507** `gpu_fit`, with `need_bytes` and `budget_bytes`. Try
  a smaller `num_ctx` or a smaller model. For a `gpu+ram` model sent
  without `options.num_ctx`, the gateway first tries 4096, then 2048.
- After loading, a GPU-only model must show 100% in VRAM in `/api/ps`. If
  any of it landed on the CPU, the gateway unloads it and answers
  `gpu_spill` (507 for non-streamed calls).
- A `gpu+ram` model is unloaded and refused with `ram_pressure` (507) if
  loading it pushed the desktop into swap or left under 1 GiB free.
- A `gpu+ram` model loads alone: the desktop unloads other models first,
  and unloads it before a GPU-only model runs. Expect a reload when you
  switch between them.

One job runs on the GPU at a time; up to 8 more wait in a queue. When
the queue is full: **503** `busy`.

Streaming (`stream` true, the default for chat and generate): once the
request passes auth, validation and the fit estimate, the gateway sends
`200` with `Content-Type: application/x-ndjson` and then waits its turn and
loads the model. A failure after that point arrives as one last NDJSON
line: `{"error": "...", "code": "gpu_spill" | "ram_pressure" | "busy" | "ollama", "done": true}`.
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
    }
  ],
  "pairing": {
    "code_shown": "7K4M-2QXD-9FHT",
    "code_normalized": "7K4M2QXD9FHT",
    "key_hex": "0cb34d68726b5dfa6196ddb20f2f9c3ea864b821b235bb545692bd0138e3502e",
    "name": "Patrick's MacBook Pro",
    "public_key_b64url": "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg",
    "timestamp": 1790000100,
    "nonce": "EBESExQVFhcYGRobHB0eHw",
    "message_utf8": "ollama1-pair-v1\nPatrick's MacBook Pro\nA6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg\n1790000100\nEBESExQVFhcYGRobHB0eHw",
    "mac_b64url": "zytUgalW6zDLx2taZpTve4SqP5iaJKGNkwpOvPDoioc",
    "request_json": {
      "name": "Patrick's MacBook Pro",
      "public_key": "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg",
      "timestamp": 1790000100,
      "nonce": "EBESExQVFhcYGRobHB0eHw",
      "mac": "zytUgalW6zDLx2taZpTve4SqP5iaJKGNkwpOvPDoioc"
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
gateway's backend (libsodium via PyNaCl on the desktop, OpenSSL via
`cryptography` elsewhere).

A checklist for the app's implementation:

1. Derive `device_id` from the vector seed → `56475aa75463474c`.
2. Rebuild both canonical strings and signatures byte for byte.
3. Rebuild the pairing `key_hex`, `mac_b64url` and `proof_b64url`.
4. Accept `7k4m2qxd9fht` and `7K4M-2QXD-9FHT` as the same code.
5. Refuse a pairing response whose `proof` doesn't match.
