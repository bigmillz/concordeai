"""A tiny client for the local Ollama API (127.0.0.1:11434) plus the
GPU-fit arithmetic the gateway uses before it lets a model load."""
import http.client
import json
import urllib.parse


def split_url(base):
    u = urllib.parse.urlsplit(base)
    return u.hostname or "127.0.0.1", u.port or 11434


def connect(base, timeout):
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


def kv_cache_bytes(model_info, n_ctx, bytes_per_elem=2):
    """f16 K+V cache for n_ctx tokens, from GGUF metadata as /api/show
    returns it. Conservative: sliding-window layers are counted in full.
    None if the metadata isn't there (then only the /api/ps check runs)."""
    if not isinstance(model_info, dict):
        return None
    arch = model_info.get("general.architecture")
    if not arch:
        return None

    def g(k):
        return model_info.get("%s.%s" % (arch, k))

    try:
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
        per_token = sum(k * (kl + vl) for k in kvs)
        return per_token * int(n_ctx) * bytes_per_elem
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
