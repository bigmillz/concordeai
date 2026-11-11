"""A tiny client for the local Ollama API (127.0.0.1:11434) plus the
GPU-fit arithmetic the gateway uses before it lets a model load."""
import http.client
import json
import socket
import urllib.parse


def split_url(base):
    u = urllib.parse.urlsplit(base)
    return u.hostname or "127.0.0.1", u.port or 11434


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a UNIX socket: the llama.cpp bridge's door (6b531), a
    base written unix:/run/ollama1/llamacpp/bridge.sock. No TCP port, so
    no port guard to add: the socket's folder says who may connect."""

    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def connect(base, timeout):
    if str(base).startswith("unix:"):
        return UnixHTTPConnection(str(base)[5:], timeout=timeout)
    host, port = split_url(base)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def call(base, method, path, obj=None, timeout=30):
    """One JSON call. Returns (status, parsed body or None)."""
    conn = connect(base, timeout)
    try:
        body = None if obj is None else json.dumps(obj).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read(64 << 20)
        try:
            return r.status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return r.status, None
    finally:
        conn.close()


def is_remote(entry):
    """Ollama 'cloud' models run on ollama.com, not here. Never allowed."""
    if not isinstance(entry, dict):
        return True
    if entry.get("remote_model") or entry.get("remote_host"):
        return True
    name = str(entry.get("name") or entry.get("model") or "")
    tag = name.split(":", 1)[1] if ":" in name else ""
    return tag == "cloud" or tag.endswith("-cloud") or name.endswith("-cloud")


# Ollama's own converted copies (6b448). Ollama 0.40 converts a model for its
# llama.cpp runner and keeps the result as a manifest of its own, named
# llamacpp:<64-hex sha> (manifest "runner":"llamacpp", layers "from" the
# source model). Nobody pulled it, it can't be pulled, and deleting it only
# makes Ollama build it again: it is Ollama's cache, not a model. It is
# never listed, counted, served, synced or removed by the kit; its bytes are
# still on the disk, so storage figures show them as "Ollama cache".
INTERNAL_PREFIXES = ("llamacpp:", "llamacpp/")
_DEFAULT_REPO = ("registry.ollama.ai/library/", "registry.ollama.ai/", "library/")


def _hex64(s):
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s)


def is_internal(entry):
    """True for an Ollama-internal cache entry (a dict from /api/tags or
    /api/ps, or a bare name). Matched on the name (llamacpp:... or
    llamacpp/...), or on `runner` metadata saying llamacpp when the tag is a
    bare sha256: a runner field alone never hides a model someone pulled."""
    if isinstance(entry, dict):
        names = [entry.get("name"), entry.get("model")]
        meta = [entry.get("runner")]
        if isinstance(entry.get("details"), dict):
            meta.append(entry["details"].get("runner"))
    else:
        names, meta = [entry], []
    llamacpp_runner = any(str(r or "").strip().lower() == "llamacpp" for r in meta)
    for n in names:
        n = str(n or "").strip().lower()
        for p in _DEFAULT_REPO:
            if n.startswith(p):
                n = n[len(p):]
                break
        if n.startswith(INTERNAL_PREFIXES):
            return True
        if llamacpp_runner and ":" in n and _hex64(n.rsplit(":", 1)[1]):
            return True
    return False


def user_models(entries):
    """The models someone installed, from an /api/tags "models" list: named
    entries that are neither Ollama cloud stubs nor Ollama's own cache."""
    return [m for m in (entries if isinstance(entries, list) else [])
            if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]
            and not is_remote(m) and not is_internal(m)]


def cache_bytes(entries):
    """Bytes the Ollama-internal entries report (for storage totals)."""
    total = 0
    for m in entries if isinstance(entries, list) else []:
        if isinstance(m, dict) and is_internal(m):
            s = m.get("size")
            if isinstance(s, int) and not isinstance(s, bool) and s > 0:
                total += s
    return total


def same_model(a, b):
    def norm(x):
        x = str(x or "")
        return x if ":" in x.rsplit("/", 1)[-1] else x + ":latest"
    return norm(a) == norm(b)


def _per_layer(v, n_layer):
    if isinstance(v, list):
        vals = [int(x) for x in v[:n_layer]]
        return vals + [vals[-1] if vals else 0] * (n_layer - len(vals))
    return [int(v)] * n_layer


# THE CACHE AS LLAMA.CPP ALLOCATES IT (6b660, per Patrick): the estimate used
# to count every layer at the full context, so on his 16 GB card the gateway
# refused gemma4:12b past ~11k (16k "needs" 18.3 GiB; it really holds 9.6 GB)
# and gpt-oss:20b past ~31k (64k really fits in 14.3 GB). llama.cpp (Ollama
# 0.40 runs llama-server) keeps far less:
# - a sliding-window layer holds its window plus one micro-batch of tokens,
#   rounded up to 256 cells (gemma4:12b at 16k: 2,048 cells; gpt-oss: 768);
# - Gemma's global and windowed layers have their own head sizes
#   (key_length / key_length_swa) and K/V head counts (per layer);
# - the last shared_kv_layers layers (Gemma 4 E2B) reuse an earlier layer's
#   cache and hold none;
# - a recurrent layer (Qwen 3.5, MiMo) holds a small fixed state, no cache.
# Measured on the server's log ("llama_kv_cache: size = ..."), 2026-11-11, and
# matched to the byte by these rules. A window with no layer pattern the kit
# knows still counts every layer in full (the old, safe rule).
SWA_UBATCH = 1024        # Ollama 0.40 starts llama-server with -ub 1024; the windowed cache adds one micro-batch
SWA_PAD = 256            # llama.cpp rounds a windowed cache up to this many cells
# window layers by position for architectures whose GGUF carries no pattern (llama.cpp's set_swa_pattern):
# with n, layer i is windowed when i % n < n - 1
SWA_PATTERN_BY_ARCH = {"gpt-oss": 2, "gemma2": 2, "gemma3": 6, "cohere2": 4}


def _swa_layers(g, arch, n_layer):
    """[bool] per layer: True for a sliding-window layer; None when the model has a
    window but the kit can't tell which layers use it (then all count in full)."""
    pat = g("attention.sliding_window_pattern")
    if isinstance(pat, list) and pat:
        vals = [bool(x) for x in pat[:n_layer]]
        return vals + [vals[-1]] * (n_layer - len(vals))
    n = SWA_PATTERN_BY_ARCH.get(arch)
    if not n:
        return None
    return [i % n < n - 1 for i in range(n_layer)]


def _recurrent_layers(g, n_layer):
    """[bool] per layer: True for a recurrent (state-space) layer, which keeps no K/V cache."""
    rec = g("attention.recurrent_layers")
    if isinstance(rec, list) and rec:
        vals = [bool(x) for x in rec[:n_layer]]
        return vals + [False] * (n_layer - len(vals))
    every = g("full_attention_interval")
    if isinstance(every, int) and every > 1:
        return [(i + 1) % every != 0 for i in range(n_layer)]
    return [False] * n_layer


def kv_cache_bytes(model_info, n_ctx, bytes_per_elem=2):
    """K+V cache for n_ctx tokens (f16 by default), from GGUF metadata as
    /api/show returns it, counted as llama.cpp allocates it (see above):
    windowed layers at their window plus a micro-batch, shared and recurrent
    layers without a cache (a recurrent layer's fixed f32 state is added).
    None if the metadata isn't there (then only the /api/ps check runs)."""
    if not isinstance(model_info, dict):
        return None
    arch = model_info.get("general.architecture")
    if not arch:
        return None

    def g(k):
        return model_info.get("%s.%s" % (arch, k))

    try:
        n_ctx = int(n_ctx)
        n_layer = int(g("block_count") or 0)
        emb = int(g("embedding_length") or 0)
        n_head = g("attention.head_count")
        n_kv = g("attention.head_count_kv")
        if not n_layer or not emb or not n_head:
            return None
        heads = _per_layer(n_head, n_layer)
        kvs = _per_layer(n_kv if n_kv is not None else n_head, n_layer)
        hmax = max(heads) or 1
        kl = int(g("attention.key_length") or emb // hmax)
        vl = int(g("attention.value_length") or emb // hmax)
        kl_swa = int(g("attention.key_length_swa") or kl)
        vl_swa = int(g("attention.value_length_swa") or vl)
        window = int(g("attention.sliding_window") or 0)
        swa = _swa_layers(g, arch, n_layer) if window > 0 else [False] * n_layer
        if swa is None:
            swa, window = [False] * n_layer, 0          # unknown layout: every layer in full
        swa_cells = min(n_ctx, -(-(window + SWA_UBATCH) // SWA_PAD) * SWA_PAD) if window else n_ctx
        recurrent = _recurrent_layers(g, n_layer)
        shared = int(g("attention.shared_kv_layers") or 0)
        own = n_layer - shared if 0 < shared < n_layer else n_layer
        total = 0
        for i in range(own):
            if recurrent[i]:
                continue
            if swa[i]:
                total += kvs[i] * (kl_swa + vl_swa) * swa_cells
            else:
                total += kvs[i] * (kl + vl) * n_ctx
        total *= bytes_per_elem
        n_rec = sum(1 for r in recurrent[:own] if r)
        if n_rec:
            # one sequence's state per recurrent layer, f32: the SSM state plus the convolution window
            state, inner = int(g("ssm.state_size") or 0), int(g("ssm.inner_size") or 0)
            groups, conv = int(g("ssm.group_count") or 0), int(g("ssm.conv_kernel") or 0)
            per = state * inner + max(0, conv - 1) * (inner + 2 * groups * state)
            total += n_rec * per * 4
        return total
    except (TypeError, ValueError):
        return None


def context_max(model_info):
    if not isinstance(model_info, dict):
        return None
    arch = model_info.get("general.architecture")
    v = model_info.get("%s.context_length" % arch) if arch else None
    return int(v) if isinstance(v, int) and v > 0 else None


RAM_COMPUTE_BYTES = 5 << 29   # 2.5 GiB: compute buffers on both the GPU and the CPU side


def fit_estimate(weights_bytes, model_info, n_ctx, ram=False):
    """Bytes the model needs resident: weights, KV cache, and a margin for
    the compute graph (5% of weights + 256 MiB). A model split between GPU
    and system memory ('ram') has compute buffers on both sides, so it is
    counted with 2.5 GiB instead of 256 MiB."""
    kv = kv_cache_bytes(model_info, n_ctx)
    need = int(weights_bytes) + int(weights_bytes * 0.05) + (RAM_COMPUTE_BYTES if ram else (256 << 20))
    if kv is not None:
        need += kv
    return need, kv
