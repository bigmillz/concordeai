"""The llama.cpp bridge (6b531): chosen models served by llama.cpp's server,
behind the gateway, in Ollama's own dialect.

Measured on the server (BEAT_FABLE, 2026-10-24, the same GGUF files Ollama
serves): llama.cpp's CUDA build generated 15-25% faster than Ollama on three
of four models (gpt-oss:20b 184 vs 160 tok/s, qwen3.5:9b 137 vs 117,
hermes3:8b 167 vs 134) and 59% SLOWER on gemma4:12b (96 vs 152: Ollama runs
the Gemma family on its own engine). Draft-free n-gram speculation was
neutral on new text and 1.3-1.9x when the text repeats. So: llama.cpp for
the models where it measured faster, Ollama for the rest, chosen per model
in /etc/ollama1/llamacpp.json.

How it fits: the gateway speaks Ollama's /api/* to ONE upstream. With
"llamacpp_bridge" set in config.json it speaks to this bridge over a UNIX
socket instead of Ollama's port; the bridge passes everything through to
Ollama untouched, except the models listed in its own config, which it
serves itself by running llama-server (one at a time, loopback only) and
translating Ollama's chat/generate/load/unload/ps into llama-server's
OpenAI-style API and back. The gateway's rules keep working unchanged: its
fit check reads /api/show from Ollama (same weights, same metadata), its
"100% in VRAM" check reads /api/ps (the bridge reports the child's VRAM from
nvidia-smi, 0 when it cannot be read, so the check fails closed), its
keep_alive 0 unloads stop the child. One runtime holds the card at a time:
starting the child unloads Ollama's models first, and a request for an
Ollama model stops the child first.

Rollback is one key: remove "llamacpp_bridge" from config.json and restart
the gateway; Ollama serves everything again, the bridge can stay or go.

Stateless, like the gateway: bodies live in memory for one request. The
journal gets event names, model names and counts, never a prompt or an
answer (llama-server is started without prompt logging).
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import o1ollama
from o1common import Paths, load_config, read_json_safe, safe_label

CONFIG = os.path.join(Paths.etc, "llamacpp.json")
RUN_DIR = os.path.join(Paths.run, "llamacpp")
SOCKET = os.path.join(RUN_DIR, "bridge.sock")
DEFAULT_BIN = "/opt/llamacpp/current/llama-server"
DEFAULT_PORT = 8470
LOAD_TIMEOUT_S = 900
STOP_TIMEOUT_S = 20
MAX_BODY = 64 << 20
MODEL_MEDIA = "application/vnd.ollama.image.model"
PROJECTOR_MEDIA = "application/vnd.ollama.image.projector"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# an extra llama-server flag: one token, no spaces, no shell characters
ARG_RE = re.compile(r"^[-A-Za-z0-9_.=/:,+]{1,200}$")
# flags the bridge sets itself: an extra that names one is refused
OWN_FLAGS = {"-m", "--model", "--host", "--port", "-c", "--ctx-size", "-md", "--model-draft", "--mmproj",
             "--log-file", "--verbose-prompt", "-v", "--verbose", "--spec-type", "--spec-draft-n-max",
             "-ctk", "-ctv", "--cache-type-k", "--cache-type-v"}
SPEC_TYPES = ("none", "ngram-simple", "ngram-mod", "ngram-map-k", "ngram-map-k4v")
KV_TYPES = ("f16", "q8_0", "q4_0")
# Ollama option -> llama-server's OpenAI-style field
SAMPLING = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "min_p": "min_p",
            "repeat_penalty": "repeat_penalty", "presence_penalty": "presence_penalty",
            "frequency_penalty": "frequency_penalty", "seed": "seed"}
HOP = {"connection", "keep-alive", "transfer-encoding", "host", "content-length", "te", "trailers", "upgrade"}


def log(event, **fields):
    """Journal line: an event name and counts/names only."""
    parts = [event] + ["%s=%s" % (k, safe_label(v, 64).replace(" ", "_")) for k, v in fields.items()]
    print(" ".join(parts), flush=True)


class BridgeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---- config ------------------------------------------------------------------

def load_bridge_config(path=None):
    """llamacpp.json, checked: {"enabled", "bin", "port", "models": {name: spec}}.
    Returns (config, problems). A model with a problem is dropped from the
    config, so a typo never takes the whole bridge down with it."""
    path = path or CONFIG
    data = read_json_safe(path, default=None, max_bytes=1 << 20)
    problems = []
    if data is None:
        return {"enabled": False, "bin": DEFAULT_BIN, "port": DEFAULT_PORT, "models": {}}, \
            ["%s: missing or not JSON" % path]
    if not isinstance(data, dict):
        return {"enabled": False, "bin": DEFAULT_BIN, "port": DEFAULT_PORT, "models": {}}, \
            ["%s: not a JSON object" % path]
    cfg = {"enabled": bool(data.get("enabled", False)),
           "bin": str(data.get("bin") or DEFAULT_BIN),
           "port": data.get("port", DEFAULT_PORT),
           "models": {}}
    if not (isinstance(cfg["port"], int) and not isinstance(cfg["port"], bool) and 1024 <= cfg["port"] <= 65535):
        problems.append("port must be a whole number 1024..65535")
        cfg["port"] = DEFAULT_PORT
    models = data.get("models")
    if not isinstance(models, dict):
        problems.append("models must be an object of name: settings")
        models = {}
    for name, spec in models.items():
        ok, why = check_model_spec(name, spec)
        if ok:
            cfg["models"][name] = ok
        else:
            problems.append("%s: %s" % (safe_label(name), why))
    return cfg, problems


def check_model_spec(name, spec):
    """One model's settings, filled in with the defaults. (settings, "") or
    (None, why)."""
    if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}(:[A-Za-z0-9._-]{1,64})?$", name):
        return None, "not a model name"
    if not isinstance(spec, dict):
        return None, "settings must be an object"
    out = {"ctx": spec.get("ctx", 32768), "kv": spec.get("kv", "f16"), "spec": spec.get("spec", "none"),
           "spec_n_max": spec.get("spec_n_max", 16), "draft": spec.get("draft", ""), "args": spec.get("args", [])}
    if not (isinstance(out["ctx"], int) and not isinstance(out["ctx"], bool) and 512 <= out["ctx"] <= 1 << 20):
        return None, "ctx must be a whole number 512..1048576"
    if out["kv"] not in KV_TYPES:
        return None, "kv must be one of %s" % ", ".join(KV_TYPES)
    if out["spec"] not in SPEC_TYPES:
        return None, "spec must be one of %s" % ", ".join(SPEC_TYPES)
    if not (isinstance(out["spec_n_max"], int) and not isinstance(out["spec_n_max"], bool)
            and 1 <= out["spec_n_max"] <= 256):
        return None, "spec_n_max must be a whole number 1..256"
    if out["draft"] and (not isinstance(out["draft"], str) or not out["draft"].startswith("/")
                         or ".." in out["draft"] or not out["draft"].endswith(".gguf")):
        return None, "draft must be an absolute path to a .gguf file"
    if not isinstance(out["args"], list):
        return None, "args must be a list of flags"
    for a in out["args"]:
        if not isinstance(a, str) or not ARG_RE.match(a):
            return None, "args: %r is not a plain flag" % safe_label(a, 40)
        if a.split("=", 1)[0] in OWN_FLAGS:
            return None, "args: %s is set by the bridge" % a.split("=", 1)[0]
    return out, ""


# ---- the model's files -------------------------------------------------------

def manifest_path(name, models_dir=None):
    """Ollama's installed manifest of a model (o1library.split_name's shape)."""
    base, _, tag = name.partition(":")
    tag = tag or "latest"
    parts = base.split("/")
    if len(parts) > 1 and "." in parts[0]:
        host, repo = parts[0], "/".join(parts[1:])
    else:
        host, repo = "registry.ollama.ai", (base if "/" in base else "library/" + base)
    if ".." in repo or ".." in tag or ".." in host:
        return None
    return os.path.join(models_dir or Paths.models, "manifests", host, repo, tag)


def model_files(name, models_dir=None):
    """(gguf path, projector path or "", size in bytes) of an installed Ollama
    model, from its manifest; BridgeError when it is not here or has no
    single weights layer. The GGUF is the very blob Ollama serves."""
    p = manifest_path(name, models_dir)
    m = read_json_safe(p, default=None, max_bytes=1 << 20) if p else None
    if not isinstance(m, dict):
        raise BridgeError(404, "model is not installed on this server")
    layers = m.get("layers") if isinstance(m.get("layers"), list) else []
    weights = [l for l in layers if isinstance(l, dict) and l.get("mediaType") == MODEL_MEDIA]
    proj = [l for l in layers if isinstance(l, dict) and l.get("mediaType") == PROJECTOR_MEDIA]
    if len(weights) != 1 or not DIGEST_RE.match(str(weights[0].get("digest") or "")):
        raise BridgeError(400, "model has no single weights file llama.cpp can load")
    blobs = os.path.join(models_dir or Paths.models, "blobs")
    gguf = os.path.join(blobs, weights[0]["digest"].replace(":", "-"))
    if not os.path.isfile(gguf):
        raise BridgeError(404, "the model's weights file is missing")
    mmproj = ""
    if proj and DIGEST_RE.match(str(proj[0].get("digest") or "")):
        cand = os.path.join(blobs, proj[0]["digest"].replace(":", "-"))
        if os.path.isfile(cand):
            mmproj = cand
    try:
        size = os.path.getsize(gguf)
    except OSError:
        size = int(weights[0].get("size") or 0)
    return gguf, mmproj, size


def server_command(binary, port, gguf, mmproj, spec):
    """The llama-server command line for one model. Flags as measured on
    b11849 (BEAT_FABLE): --spec-type / --spec-draft-n-max (the old --draft-max
    is gone), -ctk/-ctv for the KV cache, -fa on. No prompt logging."""
    cmd = [binary, "-m", gguf, "--host", "127.0.0.1", "--port", str(port), "-c", str(spec["ctx"]),
           "-ngl", "999", "-fa", "on", "--jinja", "--no-webui"]
    if mmproj:
        cmd += ["--mmproj", mmproj]
    if spec["kv"] != "f16":
        cmd += ["-ctk", spec["kv"], "-ctv", spec["kv"]]
    if spec["draft"]:
        cmd += ["-md", spec["draft"]]
    if spec["spec"] != "none":
        cmd += ["--spec-type", spec["spec"], "--spec-draft-n-max", str(spec["spec_n_max"])]
    cmd += list(spec["args"])
    return cmd


def vram_used_by(pid):
    """Bytes the process holds on the card, from nvidia-smi; None when that
    can't be read (then /api/ps says 0 in VRAM and the gateway refuses the
    model: fails closed)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) >= 2 and cells[0] == str(pid) and cells[1].isdigit():
            return int(cells[1]) << 20
    return None


# ---- the child ---------------------------------------------------------------

class Child:
    """The one llama-server this bridge runs, for one model at a time."""

    EXTRA_ENV = {}       # the tests' hooks for the stub server; empty in service

    def __init__(self, cfg, models_dir=None, vram_fn=vram_used_by):
        self.cfg = cfg
        self.models_dir = models_dir
        self.vram_fn = vram_fn
        self.lock = threading.Lock()
        self.proc = None
        self.name = None
        self.spec = None
        self.size = 0
        self.started = 0.0

    @property
    def port(self):
        return int(self.cfg["port"])

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def serves(self, name):
        return self.running() and o1ollama.same_model(self.name, name)

    def bridged(self, name):
        for k in self.cfg["models"]:
            if o1ollama.same_model(k, name):
                return k
        return None

    def http(self, method, path, body=None, timeout=30, stream=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        data = None if body is None else json.dumps(body).encode("utf-8")
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        r = conn.getresponse()
        if stream:
            return conn, r
        raw = r.read(MAX_BODY)
        conn.close()
        try:
            return r.status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return r.status, None

    def healthy(self):
        try:
            st, data = self.http("GET", "/health", timeout=5)
        except (OSError, http.client.HTTPException):
            return False
        return st == 200 and isinstance(data, dict) and data.get("status") == "ok"

    def ensure(self, name):
        """llama-server up and healthy for `name` (a bridged model). Starts
        it, after stopping another model's child; waits for the weights."""
        key = self.bridged(name)
        if key is None:
            raise BridgeError(404, "model is not served by llama.cpp here")
        with self.lock:
            if self.serves(key):
                return
            self._stop_locked("swap")
            gguf, mmproj, size = model_files(key, self.models_dir)
            binary = self.cfg["bin"]
            if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
                raise BridgeError(503, "llama-server is not installed where the bridge expects it")
            if _port_open(self.port):
                raise BridgeError(503, "the bridge's llama-server port is taken by something else")
            cmd = server_command(binary, self.port, gguf, mmproj, self.cfg["models"][key])
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/var/lib/ollama"),
                   "LD_LIBRARY_PATH": os.path.dirname(os.path.realpath(binary))}
            env.update(self.EXTRA_ENV)
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            except OSError as e:
                raise BridgeError(503, "llama-server could not start: %s" % type(e).__name__)
            self.name, self.spec, self.size, self.started = key, self.cfg["models"][key], size, time.monotonic()
            log("start", model=key, pid=self.proc.pid, ctx=self.spec["ctx"], spec=self.spec["spec"],
                kv=self.spec["kv"], draft="yes" if self.spec["draft"] else "no")
            end = time.monotonic() + LOAD_TIMEOUT_S
            while time.monotonic() < end:
                if self.proc.poll() is not None:
                    code = self.proc.returncode
                    self.proc, self.name = None, None
                    log("died", model=key, code=code)
                    raise BridgeError(502, "llama-server stopped while loading the model")
                if self.healthy():
                    log("ready", model=key, s=int(time.monotonic() - self.started))
                    return
                time.sleep(0.5)
            self._stop_locked("load_timeout")
            raise BridgeError(502, "llama-server did not finish loading the model in time")

    def stop(self, why="unload"):
        with self.lock:
            self._stop_locked(why)

    def _stop_locked(self, why):
        p, name = self.proc, self.name
        self.proc, self.name, self.spec, self.size = None, None, None, 0
        if p is None:
            return
        if p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=STOP_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                    p.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        log("stop", model=name, why=why)

    def ps_entry(self):
        """The running model as Ollama's /api/ps would list it."""
        if not self.running():
            return None
        vram = self.vram_fn(self.proc.pid)
        return {"name": self.name, "model": self.name, "size": self.size,
                "size_vram": int(vram) if vram is not None else 0,
                "digest": "", "details": {"runner": "llama.cpp", "format": "gguf"},
                "expires_at": "", "context_length": self.spec["ctx"], "runner": "llama.cpp"}

    def status(self):
        return {"running": self.running(), "model": self.name, "port": self.port,
                "pid": self.proc.pid if self.running() else None,
                "up_s": int(time.monotonic() - self.started) if self.running() else 0,
                "models": sorted(self.cfg["models"])}


def _port_open(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


# ---- Ollama's dialect <-> llama-server's --------------------------------------

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def oai_from_chat(obj, model):
    """Ollama /api/chat body -> llama-server /v1/chat/completions body."""
    out = {"model": model, "messages": obj.get("messages") or [], "stream": bool(obj.get("stream", True)),
           "stream_options": {"include_usage": True}}
    opts = obj.get("options") if isinstance(obj.get("options"), dict) else {}
    for k, v in opts.items():
        if k in SAMPLING and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[SAMPLING[k]] = v
    n = opts.get("num_predict")
    if isinstance(n, int) and not isinstance(n, bool) and n > 0:
        out["max_tokens"] = n
    stop = opts.get("stop")
    if isinstance(stop, list) and all(isinstance(s, str) for s in stop) and stop:
        out["stop"] = stop[:8]
    if obj.get("format") == "json":
        out["response_format"] = {"type": "json_object"}
    elif isinstance(obj.get("format"), dict):
        out["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": obj["format"]}}
    return out


def oai_from_generate(obj, model):
    """Ollama /api/generate body -> llama-server /v1/completions body."""
    out = oai_from_chat(obj, model)
    out.pop("messages", None)
    prompt = str(obj.get("prompt") or "")
    if obj.get("system"):
        prompt = str(obj["system"]) + "\n\n" + prompt
    out["prompt"] = prompt
    return out


def counts_from(final):
    """Ollama's count fields from llama-server's last chunk (its timings,
    else the OpenAI usage)."""
    t = final.get("timings") if isinstance(final.get("timings"), dict) else {}
    u = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    pn = int(t.get("prompt_n") or u.get("prompt_tokens") or 0)
    en = int(t.get("predicted_n") or u.get("completion_tokens") or 0)
    pms = float(t.get("prompt_ms") or 0)
    ems = float(t.get("predicted_ms") or 0)
    return {"prompt_eval_count": pn, "prompt_eval_duration": int(pms * 1e6),
            "eval_count": en, "eval_duration": int(ems * 1e6)}


def sse_events(r):
    """The JSON objects of an SSE stream, in order; stops at [DONE]."""
    while True:
        line = r.readline(16 << 20)
        if not line:
            return
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            return
        try:
            yield json.loads(data.decode("utf-8"))
        except ValueError:
            continue


# ---- the server --------------------------------------------------------------

class Bridge:
    def __init__(self, cfg, ollama_url, models_dir=None, vram_fn=vram_used_by):
        self.cfg = cfg
        self.ollama = ollama_url
        self.child = Child(cfg, models_dir, vram_fn)
        self.started = time.monotonic()

    def unload_ollama_models(self):
        """Every model Ollama holds, unloaded (one runtime on the card)."""
        try:
            st, data = o1ollama.call(self.ollama, "GET", "/api/ps", timeout=10)
        except OSError:
            return
        if st != 200 or not isinstance(data, dict):
            return
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            for path, body in (("/api/generate", {"model": name, "keep_alive": 0}),
                               ("/api/embed", {"model": name, "input": [], "keep_alive": 0})):
                try:
                    st, _ = o1ollama.call(self.ollama, "POST", path, body, timeout=60)
                    if st == 200:
                        log("unload-ollama", model=name)
                        break
                except OSError:
                    pass


def make_handler(bridge):
    cfg = bridge.cfg
    child = bridge.child

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ollama1-llamacpp"
        sys_version = ""

        def log_message(self, *a):
            pass            # nothing of a request reaches the journal

        # -- answers --------------------------------------------------------
        def send_json(self, status, obj):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def start_ndjson(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache, no-store, no-transform")
            self.end_headers()

        def chunk(self, data):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def end_chunks(self):
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise BridgeError(413, "body too large")
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise BridgeError(400, "body is not JSON")
            return obj if isinstance(obj, dict) else {}

        # -- routes ---------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/v1/bridge/status":
                return self.send_json(200, dict(child.status(), enabled=bool(cfg.get("enabled")),
                                                up_s=int(time.monotonic() - bridge.started)))
            if path == "/api/ps":
                return self.ps()
            return self.passthrough("GET", None)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                obj = self.body()
                name = obj.get("model") if isinstance(obj.get("model"), str) else ""
                key = child.bridged(name) if name else None
                if path in ("/api/chat", "/api/generate", "/api/embed", "/api/embeddings", "/v1/chat/completions",
                            "/v1/completions", "/v1/embeddings"):
                    if key is None:
                        # Ollama's model: the card is Ollama's, so the child goes first
                        if child.running():
                            child.stop("ollama_model_asked")
                        return self.passthrough("POST", obj)
                    if path in ("/api/embed", "/api/embeddings", "/v1/embeddings"):
                        raise BridgeError(400, "this model is served by llama.cpp here; it does not embed")
                    if obj.get("keep_alive") == 0 and path.startswith("/api/"):
                        if child.serves(key):
                            child.stop("unload: asked")
                        return self.send_json(200, {"model": key, "created_at": _now(), "response": "",
                                                    "done": True, "done_reason": "unload"})
                    if path == "/api/generate" and not obj.get("prompt"):
                        self.load(key)
                        return self.send_json(200, {"model": key, "created_at": _now(), "response": "",
                                                    "done": True, "done_reason": "load"})
                    self.load(key)
                    if path == "/api/chat":
                        return self.chat(key, obj)
                    if path == "/api/generate":
                        return self.generate(key, obj)
                    return self.openai(path, obj, key)
                return self.passthrough("POST", obj)
            except BridgeError as e:
                self.send_json(e.status, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_DELETE(self):
            self.passthrough("DELETE", None)

        def do_HEAD(self):
            self.passthrough("HEAD", None)

        # -- Ollama, untouched ----------------------------------------------
        def passthrough(self, method, obj):
            """The request as it came, to Ollama; the answer as it comes back,
            streamed. Only the model's name is logged."""
            try:
                conn = o1ollama.connect(bridge.ollama, timeout=3600)
            except OSError:
                return self.send_json(502, {"error": "Ollama is not answering"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = (json.dumps(obj).encode("utf-8") if obj is not None
                       else (self.rfile.read(n) if n and method not in ("GET", "HEAD") else None))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                try:
                    conn.request(method, self.path, body=raw, headers=headers)
                    r = conn.getresponse()
                except (OSError, http.client.HTTPException):
                    return self.send_json(502, {"error": "Ollama is not answering"})
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    piece = r.read1(65536)
                    if not piece:
                        break
                    self.wfile.write(piece)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            finally:
                conn.close()

        def ps(self):
            """Ollama's loaded models plus the bridge's own."""
            models = []
            try:
                st, data = o1ollama.call(bridge.ollama, "GET", "/api/ps", timeout=10)
                if st == 200 and isinstance(data, dict):
                    models = [m for m in data.get("models") or [] if isinstance(m, dict)]
                elif not child.running():
                    return self.send_json(502, {"error": "Ollama is not answering"})
            except OSError:
                if not child.running():
                    return self.send_json(502, {"error": "Ollama is not answering"})
            own = child.ps_entry()
            if own:
                models.append(own)
            self.send_json(200, {"models": models})

        # -- llama.cpp, in Ollama's dialect ---------------------------------
        def load(self, key):
            if child.serves(key):
                return
            bridge.unload_ollama_models()
            child.ensure(key)

        def chat(self, key, obj):
            fwd = oai_from_chat(obj, key)
            t0 = time.monotonic()
            if not fwd["stream"]:
                try:
                    st, data = child.http("POST", "/v1/chat/completions", fwd, timeout=3600)
                except (OSError, http.client.HTTPException):
                    raise BridgeError(502, "llama-server stopped answering")
                if st != 200 or not isinstance(data, dict):
                    raise BridgeError(502, "llama-server: %s" % st)
                ch = (data.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                out = {"model": key, "created_at": _now(),
                       "message": {"role": "assistant", "content": msg.get("content") or ""},
                       "done": True, "done_reason": "length" if ch.get("finish_reason") == "length" else "stop",
                       "total_duration": int((time.monotonic() - t0) * 1e9), "load_duration": 0}
                if msg.get("reasoning_content"):
                    out["message"]["thinking"] = msg["reasoning_content"]
                out.update(counts_from(data))
                log("req", model=key, kind="chat", ms=int((time.monotonic() - t0) * 1000), tokens=out["eval_count"])
                return self.send_json(200, out)
            try:
                conn, r = child.http("POST", "/v1/chat/completions", fwd, timeout=3600, stream=True)
            except (OSError, http.client.HTTPException):
                raise BridgeError(502, "llama-server stopped answering")
            try:
                if r.status != 200:
                    raise BridgeError(502, "llama-server: %s" % r.status)
                self.start_ndjson()
                final, reason, n = {}, "stop", 0
                for ev in sse_events(r):
                    if ev.get("usage") or ev.get("timings"):
                        final = ev
                    ch = (ev.get("choices") or [{}])[0] or {}
                    delta = ch.get("delta") or {}
                    if ch.get("finish_reason") == "length":
                        reason = "length"
                    piece = delta.get("content") or ""
                    think = delta.get("reasoning_content") or ""
                    if piece or think:
                        n += 1
                        msg = {"role": "assistant", "content": piece}
                        if think:
                            msg["thinking"] = think
                        self.chunk(json.dumps({"model": key, "created_at": _now(), "message": msg,
                                               "done": False}).encode("utf-8") + b"\n")
                last = {"model": key, "created_at": _now(), "message": {"role": "assistant", "content": ""},
                        "done": True, "done_reason": reason,
                        "total_duration": int((time.monotonic() - t0) * 1e9), "load_duration": 0}
                last.update(counts_from(final))
                if not last["eval_count"]:
                    last["eval_count"] = n
                self.chunk(json.dumps(last).encode("utf-8") + b"\n")
                self.end_chunks()
                log("req", model=key, kind="chat", ms=int((time.monotonic() - t0) * 1000), tokens=last["eval_count"])
            finally:
                conn.close()

        def generate(self, key, obj):
            fwd = oai_from_generate(obj, key)
            t0 = time.monotonic()
            if not fwd["stream"]:
                try:
                    st, data = child.http("POST", "/v1/completions", fwd, timeout=3600)
                except (OSError, http.client.HTTPException):
                    raise BridgeError(502, "llama-server stopped answering")
                if st != 200 or not isinstance(data, dict):
                    raise BridgeError(502, "llama-server: %s" % st)
                ch = (data.get("choices") or [{}])[0]
                out = {"model": key, "created_at": _now(), "response": ch.get("text") or "", "done": True,
                       "done_reason": "length" if ch.get("finish_reason") == "length" else "stop",
                       "total_duration": int((time.monotonic() - t0) * 1e9), "load_duration": 0}
                out.update(counts_from(data))
                log("req", model=key, kind="generate", ms=int((time.monotonic() - t0) * 1000), tokens=out["eval_count"])
                return self.send_json(200, out)
            try:
                conn, r = child.http("POST", "/v1/completions", fwd, timeout=3600, stream=True)
            except (OSError, http.client.HTTPException):
                raise BridgeError(502, "llama-server stopped answering")
            try:
                if r.status != 200:
                    raise BridgeError(502, "llama-server: %s" % r.status)
                self.start_ndjson()
                final, reason, n = {}, "stop", 0
                for ev in sse_events(r):
                    if ev.get("usage") or ev.get("timings"):
                        final = ev
                    ch = (ev.get("choices") or [{}])[0] or {}
                    if ch.get("finish_reason") == "length":
                        reason = "length"
                    piece = ch.get("text") or ""
                    if piece:
                        n += 1
                        self.chunk(json.dumps({"model": key, "created_at": _now(), "response": piece,
                                               "done": False}).encode("utf-8") + b"\n")
                last = {"model": key, "created_at": _now(), "response": "", "done": True, "done_reason": reason,
                        "total_duration": int((time.monotonic() - t0) * 1e9), "load_duration": 0}
                last.update(counts_from(final))
                if not last["eval_count"]:
                    last["eval_count"] = n
                self.chunk(json.dumps(last).encode("utf-8") + b"\n")
                self.end_chunks()
                log("req", model=key, kind="generate", ms=int((time.monotonic() - t0) * 1000), tokens=last["eval_count"])
            finally:
                conn.close()

        def openai(self, path, obj, key):
            """The OpenAI-style door (the gateway's /openai/v1/...): the body
            goes to llama-server as it is, the answer comes back as it is."""
            obj = dict(obj, model=key)
            t0 = time.monotonic()
            try:
                conn, r = child.http("POST", path, obj, timeout=3600, stream=True)
            except (OSError, http.client.HTTPException):
                raise BridgeError(502, "llama-server stopped answering")
            try:
                self.send_response(r.status)
                self.send_header("Content-Type", r.getheader("Content-Type") or "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    piece = r.read1(65536)
                    if not piece:
                        break
                    self.wfile.write(piece)
                    self.wfile.flush()
                log("req", model=key, kind="openai", ms=int((time.monotonic() - t0) * 1000))
            finally:
                conn.close()

    return Handler


class UnixServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def server_bind(self):
        try:
            os.unlink(self.server_address)
        except OSError:
            pass
        self.socket.bind(self.server_address)

    def get_request(self):
        sock, _ = self.socket.accept()
        return sock, ("unix", 0)


def serve(bridge, sock_path=SOCKET, stop=None):
    """The bridge on its UNIX socket (group-readable: the folder is setgid
    o1gw, so the gateway can connect and nobody else can). Returns the
    server; stop() shuts it down and stops the child."""
    old = os.umask(0o007)
    try:
        srv = UnixServer(sock_path, make_handler(bridge))
    finally:
        os.umask(old)
    try:
        os.chmod(sock_path, 0o660)
    except OSError:
        pass
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    t.start()
    log("listening", socket=sock_path, models=len(bridge.cfg["models"]), enabled="yes" if bridge.cfg["enabled"] else "no")
    if stop is not None:
        def _wait():
            stop.wait()
            srv.shutdown()
            bridge.child.stop("bridge_stopping")
        threading.Thread(target=_wait, daemon=True).start()
    return srv


def check(cfg_path=None, config_path=None):
    """What the bridge would do, before it runs: the config's problems, each
    model's command line, whether llama-server and nvidia-smi are there.
    Returns (lines, ok)."""
    bcfg, problems = load_bridge_config(cfg_path)
    lines, ok = [], not problems
    for p in problems:
        lines.append("problem: " + p)
    binary = bcfg["bin"]
    if os.path.isfile(binary) and os.access(binary, os.X_OK):
        lines.append("llama-server: %s" % binary)
    else:
        lines.append("problem: llama-server not found or not executable at %s" % binary)
        ok = False
    lines.append("nvidia-smi: %s" % (shutil.which("nvidia-smi") or "MISSING (every model would be refused as not on the GPU)"))
    if not shutil.which("nvidia-smi"):
        ok = False
    lines.append("enabled: %s; port: %d; socket: %s" % ("yes" if bcfg["enabled"] else "no", bcfg["port"], SOCKET))
    for name, spec in bcfg["models"].items():
        try:
            gguf, mmproj, size = model_files(name)
            lines.append("%s: %.1f GiB %s" % (name, size / (1 << 30), gguf))
            lines.append("  " + " ".join(server_command(binary, bcfg["port"], gguf, mmproj, spec)))
        except BridgeError as e:
            lines.append("problem: %s: %s" % (name, e))
            ok = False
    gcfg = load_config(config_path)
    br = gcfg.get("llamacpp_bridge") or ""
    lines.append("gateway upstream: %s" % (br or gcfg.get("ollama_url")))
    if br and br != "unix:" + SOCKET:
        lines.append("problem: config.json's llamacpp_bridge is not unix:%s" % SOCKET)
        ok = False
    return lines, ok


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="ollama1-llamacpp: chosen models through llama-server, in Ollama's dialect, "
                                             "for the gateway")
    ap.add_argument("command", nargs="?", default="serve", choices=("serve", "check", "status"))
    ap.add_argument("--config", help="llamacpp.json (default: the systemd credential, else %s)" % CONFIG)
    ap.add_argument("--socket", default=SOCKET)
    a = ap.parse_args(argv)
    cfg_path = a.config
    if cfg_path is None:
        cred = os.environ.get("CREDENTIALS_DIRECTORY")
        if cred and os.path.exists(os.path.join(cred, "llamacpp.json")):
            cfg_path = os.path.join(cred, "llamacpp.json")
    if a.command == "check":
        lines, ok = check(cfg_path)
        print("\n".join(lines))
        return 0 if ok else 1
    if a.command == "status":
        try:
            conn = o1ollama.connect("unix:" + a.socket, timeout=5)
            conn.request("GET", "/v1/bridge/status")
            r = conn.getresponse()
            print(r.read(1 << 16).decode("utf-8", "replace"))
            conn.close()
            return 0 if r.status == 200 else 1
        except OSError as e:
            print("bridge not answering on %s: %s" % (a.socket, type(e).__name__))
            return 1
    bcfg, problems = load_bridge_config(cfg_path)
    for p in problems:
        log("config", problem=p)
    gcfg = load_config()
    bridge = Bridge(bcfg, gcfg["ollama_url"])
    stop = threading.Event()
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_a: stop.set())
    os.makedirs(os.path.dirname(a.socket), exist_ok=True)
    serve(bridge, a.socket, stop)
    stop.wait()
    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
