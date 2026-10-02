#!/usr/bin/env python3
"""A stand-in for ComfyUI's HTTP and WebSocket API, enough for the gateway's
generation jobs (lib/o1gen.py): /object_info, /prompt, /history, /view, /interrupt,
/queue, /free and /ws. It runs the "graph" it is given for a fixed time, reports
steps over the WebSocket the way ComfyUI does, and keeps every call so the tests
can check what was sent and in what order.

It checks nothing about a graph except what the tests ask it to (`validate`), so
a test that passes here proves the gateway's own logic and not that a real
ComfyUI accepts the templates: that is the part only a real run can show.

    python3 stub_comfyui.py PORT        # standalone
"""
import base64
import hashlib
import json
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1, 1) + b"\x08\x02\x00\x00\x00"
       + b"\x90wS\xde" + b"\x00" * 64)
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200
WEBP = b"RIFF" + struct.pack("<I", 100) + b"WEBPVP8X" + b"\x00" * 100
GUID = b"258EAFA5-E914-47DA-95CA-" b"C5AB0DC85B11"   # RFC 6455's handshake string

DEFAULT_NODES = ["CheckpointLoaderSimple", "CLIPTextEncode", "EmptySD3LatentImage", "KSampler", "VAEDecode",
                 "SaveImage", "UNETLoader", "CLIPLoader", "VAELoader", "EmptyHunyuanLatentVideo",
                 "ModelSamplingSD3", "CreateVideo", "SaveVideo", "SaveAnimatedWEBP"]
# the file names as the three model folders list them: written out here on purpose, not
# imported from the gateway, so a renamed model in the templates shows up as a failing test
DEFAULT_FILES = {"CheckpointLoaderSimple/ckpt_name": ["flux1-schnell-fp8.safetensors"],
                 "UNETLoader/unet_name": ["wan2.1_t2v_1.3B_fp16.safetensors"],
                 "CLIPLoader/clip_name": ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
                 "VAELoader/vae_name": ["wan_2.1_vae.safetensors"]}


class StubComfy:
    def __init__(self, port, nodes=None, files=None, combo="list"):
        self.nodes = list(DEFAULT_NODES if nodes is None else nodes)
        self.files = dict(DEFAULT_FILES if files is None else files)
        self.combo = combo              # "list" or "COMBO": the two spellings of a combo input
        self.calls = []                 # (method, path, body)
        self.lock = threading.Lock()
        self.run_s = 0.4                # how long a graph "runs"
        self.steps = 4
        self.fail = False               # the graph ends in an execution error
        self.refuse = False             # POST /prompt answers 400
        self.output = None              # bytes /view returns, else a fitting sample
        self.outputs_missing = False    # history says "completed" with no outputs
        self.no_ws = False
        self.hold = threading.Event()   # a test can hold a graph here (set = run, clear = wait)
        self.hold.set()
        self.on_prompt = None           # called with the graph when it arrives
        self.history = {}
        self.count = 0
        self.last_pid = None
        self.files_out = {}
        self.running = None
        self.interrupted = set()
        self.subs = {}                  # client id -> list of queued ws messages
        self.view_status = 200
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

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return None

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                with stub.lock:
                    stub.calls.append(("GET", self.path, None))
                if u.path == "/object_info":
                    return self.js(200, stub.object_info())
                if u.path == "/system_stats":
                    return self.js(200, {"system": {}})
                if u.path.startswith("/history/"):
                    pid = u.path[len("/history/"):]
                    e = stub.history.get(pid)
                    return self.js(200, {pid: e} if e else {})
                if u.path == "/view":
                    q = urllib.parse.parse_qs(u.query)
                    name = (q.get("filename") or [""])[0]
                    data = stub.files_out.get(name)
                    if stub.view_status != 200 or data is None:
                        return self.js(stub.view_status if stub.view_status != 200 else 404, {})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if u.path == "/ws":
                    return self.ws(urllib.parse.parse_qs(u.query).get("clientId", [""])[0])
                self.js(404, {})

            def do_POST(self):
                body = self.body()
                with stub.lock:
                    stub.calls.append(("POST", self.path, body))
                if self.path == "/prompt":
                    if stub.refuse or not isinstance(body, dict) or not isinstance(body.get("prompt"), dict):
                        return self.js(400, {"error": {"type": "prompt_outputs_failed_validation"},
                                             "node_errors": {}})
                    with stub.lock:
                        stub.count += 1
                        pid = stub.last_pid = "p-%d" % stub.count
                    if stub.on_prompt:
                        stub.on_prompt(body["prompt"])
                    threading.Thread(target=stub.run, args=(pid, body["prompt"], body.get("client_id", "")),
                                     daemon=True).start()
                    return self.js(200, {"prompt_id": pid, "number": 1, "node_errors": {}})
                if self.path == "/interrupt":
                    with stub.lock:
                        if stub.running:
                            stub.interrupted.add(stub.running)
                    return self.js(200, {})
                if self.path == "/history":
                    for pid in (body or {}).get("delete") or []:
                        stub.history.pop(pid, None)
                    return self.js(200, {})
                if self.path in ("/queue", "/free"):
                    return self.js(200, {})
                self.js(404, {})

            def ws(self, client):
                if stub.no_ws:
                    return self.js(404, {})
                key = self.headers.get("Sec-WebSocket-Key", "").encode()
                accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept.decode())
                self.end_headers()
                self.close_connection = True
                q = stub.subs.setdefault(client, [])
                try:
                    while True:
                        while q:
                            data = q.pop(0)
                            if data is None:
                                return
                            self.wfile.write(bytes([0x81, len(data)]) + data if len(data) < 126
                                             else bytes([0x81, 126]) + struct.pack(">H", len(data)) + data)
                            self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    return

        self.port = port
        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    # -- what /object_info says -----------------------------------------------
    def object_info(self):
        out = {}
        for n in self.nodes:
            info = {"input": {"required": {}}, "output": []}
            for key, names in self.files.items():
                node, field = key.split("/")
                if node == n:
                    info["input"]["required"][field] = ([names, {}] if self.combo == "list"
                                                        else ["COMBO", {"options": names}])
            out[n] = info
        return out

    # -- a graph running ------------------------------------------------------
    def run(self, pid, graph, client):
        save = next((k for k, v in graph.items() if v.get("class_type") in ("SaveImage", "SaveVideo",
                                                                           "SaveAnimatedWEBP")), None)
        with self.lock:
            self.running = pid
        for i in range(1, self.steps + 1):
            self.hold.wait(30)
            time.sleep(self.run_s / self.steps)
            if pid in self.interrupted:
                self.history[pid] = {"status": {"status_str": "error", "completed": False,
                                                "messages": [["execution_interrupted", {}]]}, "outputs": {}}
                break
            self.subs.setdefault(client, []).append(json.dumps(
                {"type": "progress", "data": {"value": i, "max": self.steps, "prompt_id": pid,
                                              "node": "5"}}).encode())
        else:
            cls = graph.get(save, {}).get("class_type")
            data = self.output if self.output is not None else (
                PNG if cls == "SaveImage" else WEBP if cls == "SaveAnimatedWEBP" else MP4)
            name = "o1gen_%s_.bin" % pid
            self.files_out[name] = data
            if self.fail:
                self.history[pid] = {"status": {"status_str": "error", "completed": False,
                                                "messages": [["execution_error", {"exception_message": "boom"}]]},
                                     "outputs": {}}
            else:
                outs = {} if self.outputs_missing else {save: {"images": [
                    {"filename": name, "subfolder": "", "type": "output"}]}}
                self.history[pid] = {"status": {"status_str": "success", "completed": True, "messages": []},
                                     "outputs": outs}
        with self.lock:
            self.running = None

    def paths(self):
        with self.lock:
            return [c[1] for c in self.calls]

    def posted(self, path):
        with self.lock:
            return [c[2] for c in self.calls if c[0] == "POST" and c[1] == path]

    def close(self):
        for q in self.subs.values():
            q.append(None)
        self.srv.shutdown()


if __name__ == "__main__":
    StubComfy(int(sys.argv[1]))
    print("stub comfyui on 127.0.0.1:%s" % sys.argv[1], flush=True)
    while True:
        time.sleep(3600)
