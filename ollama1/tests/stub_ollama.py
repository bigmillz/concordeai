#!/usr/bin/env python3
"""A stand-in for Ollama's HTTP API, enough for the gateway, the panel and
the dashboard. Models are fake; answers contain a marker built from the
prompt so tests can prove the marker never reaches a file or a log.

    python3 stub_ollama.py PORT        # standalone (user-mode trial on the server)
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GIB = 1 << 30


def llama_info(layers=32, emb=4096, heads=32, kv=8, ctx=131072):
    return {"general.architecture": "llama", "llama.block_count": layers,
            "llama.embedding_length": emb, "llama.attention.head_count": heads,
            "llama.attention.head_count_kv": kv, "llama.context_length": ctx}


DEFAULT_MODELS = {
    # fits: 5 GiB weights + small KV
    "small:8b": {"size": 5 * GIB, "info": llama_info()},
    # can't fit in 16 GiB: 20 GiB weights
    "huge:70b": {"size": 20 * GIB, "info": llama_info(layers=80, emb=8192, heads=64)},
    # the estimate says it fits, but Ollama puts part of it on the CPU
    "sneaky:14b": {"size": 9 * GIB, "info": llama_info(layers=40, emb=5120, heads=40), "spill": True},
    # a mixture-of-experts giant: fits only with system RAM (gpt-oss:120b-like)
    "moe:120b": {"size": 56 * GIB, "share": 0.25,
                 "info": {"general.architecture": "gptoss", "gptoss.block_count": 36,
                          "gptoss.embedding_length": 2880, "gptoss.attention.head_count": 64,
                          "gptoss.attention.head_count_kv": 8, "gptoss.attention.key_length": 64,
                          "gptoss.attention.value_length": 64, "gptoss.context_length": 131072}},
    "embed:small": {"size": GIB // 2, "info": {"general.architecture": "bert",
                                               "bert.block_count": 12, "bert.embedding_length": 768,
                                               "bert.attention.head_count": 12},
                    "embedding": True},
    "embed:spill": {"size": GIB // 2, "info": {"general.architecture": "bert"},
                    "embedding": True, "spill": True},
}
CLOUD = {"name": "gpt-oss:120b-cloud", "model": "gpt-oss:120b-cloud", "size": 384,
         "remote_model": "gpt-oss:120b", "remote_host": "https://ollama.com:443", "digest": "c1"}
# a remote model whose name doesn't say so: only the remote_* fields give it away
CLOUD2 = {"name": "plain:latest", "model": "plain:latest", "size": 384,
          "remote_model": "plain", "remote_host": "https://ollama.com:443", "digest": "c2"}

# Ollama 0.40's own converted copy of a model for its llama.cpp runner (6b448):
# a manifest named llamacpp:<sha> that nobody pulled. /api/tags lists it like
# a model; the kit must never show, serve, count or remove it.
CACHE_SHA = "9ba9cc2b" * 8      # the shape of the real one (a 64-hex literal trips test_repo)
CACHE = {"name": "llamacpp:" + CACHE_SHA, "model": "llamacpp:" + CACHE_SHA, "size": 13_800_000_000,
         "digest": "d-cache", "modified_at": "2026-10-07T12:23:00Z",
         "details": {"format": "gguf", "family": "gptoss", "parameter_size": "20.9B"}}


class Stub:
    def __init__(self, port, models=None):
        self.models = dict(DEFAULT_MODELS if models is None else models)
        self.loaded = {}
        self.calls = []
        self.lock = threading.Lock()
        self.tokens = 6
        self.delay = 0.0
        self.reply_text = ""        # when set, the whole answer (a test that wants JSON back)
        self.fail_marker = ""       # when set, a chat whose last message holds it gets a 500
        # pulls: name -> [(digest, size, already on disk)], the digest the
        # model gets once pulled, and how many pulls fail before one works
        self.pull_layers = {}
        self.pull_digest = {}
        self.pull_fail = 0
        self.extra_tags = [CACHE]   # listed by /api/tags beside the models and the cloud stubs
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def js(self, status, obj):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                with stub.lock:
                    stub.calls.append(("GET", self.path, None))
                if self.path == "/api/tags":
                    ms = [{"name": n, "model": n, "size": m["size"], "digest": m.get("digest", "d-" + n),
                           "details": {"family": "x"}, "modified_at": "2026-09-29T00:00:00Z"}
                          for n, m in stub.models.items()]
                    return self.js(200, {"models": ms + [CLOUD, CLOUD2] + list(stub.extra_tags)})
                if self.path == "/api/ps":
                    return self.js(200, {"models": list(stub.loaded.values())})
                if self.path == "/api/version":
                    return self.js(200, {"version": "0.34.4"})
                self.js(404, {"error": "not found"})

            def do_DELETE(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = {}
                with stub.lock:
                    stub.calls.append(("DELETE", self.path, body))
                    gone = stub.models.pop(body.get("model"), None) if self.path == "/api/delete" else None
                if self.path == "/api/delete" and gone is None:
                    return self.js(404, {"error": "model not found"})
                self.js(200, {})

            def pull(self, name, body):
                with stub.lock:
                    fail = stub.pull_fail > 0
                    if fail:
                        stub.pull_fail -= 1
                if body.get("stream", True) is False:
                    return self.js(500 if fail else 200, {"error": "boom"} if fail else {"status": "success"})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.chunk(b'{"status":"pulling manifest"}\n')
                if fail:
                    self.chunk(b'{"error":"connection reset"}\n')
                    self.wfile.write(b"0\r\n\r\n")
                    return
                layers = stub.pull_layers.get(name) or [("sha256:" + "0" * 64, 1000, False)]
                for digest, size, cached in layers:
                    steps = [size] if cached else [size // 4, size // 2, size]
                    for done in [0] + steps if not cached else steps:
                        self.chunk(json.dumps({"status": "pulling " + digest[7:19], "digest": digest,
                                               "total": size, "completed": done}).encode() + b"\n")
                self.chunk(b'{"status":"verifying sha256 digest"}\n')
                with stub.lock:     # installed before "success", as Ollama does (and no race with the next test)
                    stub.models[name] = {"size": sum(x[1] for x in layers), "info": llama_info(),
                                         "digest": stub.pull_digest.get(name, "d-" + name)}
                self.chunk(b'{"status":"success"}\n')
                self.wfile.write(b"0\r\n\r\n")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                with stub.lock:
                    stub.calls.append(("POST", self.path, body))
                name = body.get("model")
                m = stub.models.get(name)
                if self.path == "/api/pull":
                    return self.pull(name, body)
                if self.path in ("/api/delete", "/api/create", "/api/copy", "/api/push"):
                    return self.js(200, {"status": "success"})
                if m is None:
                    return self.js(404, {"error": "model '%s' not found" % name})
                if self.path == "/api/show":
                    caps = (["embedding"] if m.get("embedding")
                            else ["completion", "vision"] if m.get("vision") else ["completion"])
                    return self.js(200, {"model_info": m["info"], "capabilities": caps,
                                         "details": {"family": "x"}})
                if (stub.fail_marker and self.path == "/api/chat" and stub.fail_marker in str(
                        ((body.get("messages") or [{}])[-1] or {}).get("content", ""))):
                    return self.js(500, {"error": "refused by the test"})
                if body.get("keep_alive") == 0:
                    stub.loaded.pop(name, None)
                    return self.js(200, {"model": name, "done": True, "done_reason": "unload"})
                ctx = (body.get("options") or {}).get("num_ctx", 4096)
                share = m.get("share", 0.6 if m.get("spill") else 1.0)
                stub.loaded[name] = {"name": name, "model": name, "size": m["size"],
                                     "size_vram": int(m["size"] * share), "context_length": ctx,
                                     "expires_at": "2026-09-29T23:00:00Z"}
                if self.path == "/api/embed":
                    if not m.get("embedding"):
                        return self.js(400, {"error": "does not support embeddings"})
                    return self.js(200, {"model": name, "embeddings": [[0.1, 0.2, 0.3]]})
                if self.path == "/api/embeddings":
                    return self.js(200, {"embedding": [0.1, 0.2, 0.3]})
                if m.get("embedding"):
                    return self.js(400, {"error": "\"%s\" does not support generate" % name})
                if self.path == "/api/generate":
                    prompt = body.get("prompt", "")
                    if not prompt:
                        return self.js(200, {"model": name, "response": "", "done": True,
                                             "done_reason": "load"})
                    text = "ANSWER-" + prompt[-12:]
                elif self.path == "/api/chat":
                    msgs = body.get("messages") or []
                    text = "ANSWER-" + (msgs[-1].get("content", "")[-12:] if msgs else "")
                else:
                    return self.js(404, {"error": "not found"})
                pieces = [text] + ["t%d" % i for i in range(stub.tokens - 1)]
                if stub.reply_text:
                    pieces = [stub.reply_text]
                final = {"model": name, "done": True, "done_reason": "stop",
                         "eval_count": len(pieces), "eval_duration": len(pieces) * 20_000_000,
                         "prompt_eval_count": 7, "total_duration": 1}
                if body.get("stream", True) is False:
                    out = dict(final)
                    if self.path == "/api/chat":
                        out["message"] = {"role": "assistant", "content": "".join(pieces)}
                    else:
                        out["response"] = "".join(pieces)
                    return self.js(200, out)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for piece in pieces:
                    if self.path == "/api/chat":
                        line = {"model": name, "message": {"role": "assistant", "content": piece},
                                "done": False}
                    else:
                        line = {"model": name, "response": piece, "done": False}
                    self.chunk(json.dumps(line).encode() + b"\n")
                    if stub.delay:
                        time.sleep(stub.delay)
                if self.path == "/api/chat":
                    final["message"] = {"role": "assistant", "content": ""}
                else:
                    final["response"] = ""
                self.chunk(json.dumps(final).encode() + b"\n")
                self.wfile.write(b"0\r\n\r\n")

            def chunk(self, data):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

        self.port = port
        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def paths(self):
        with self.lock:
            return [c[1] for c in self.calls]

    def close(self):
        self.srv.shutdown()


if __name__ == "__main__":
    Stub(int(sys.argv[1]))
    print("stub ollama on 127.0.0.1:%s" % sys.argv[1], flush=True)
    while True:
        time.sleep(3600)
