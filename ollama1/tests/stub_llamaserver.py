#!/usr/bin/python3
"""A stand-in for llama.cpp's llama-server, enough for the bridge: /health
(503 while "loading", then ok), /v1/chat/completions and /v1/completions
(SSE or whole, with llama-server's `timings` and the OpenAI `usage` on the
last chunk), /props. Started by the bridge as the `bin` of its config, so it
takes llama-server's flags; it writes its argv to the file named by
STUB_LLAMA_ARGV so a test can prove which flags were used. The answer
carries a marker from the prompt so tests can prove it never reaches a log.

    python3 stub_llamaserver.py -m x.gguf --host 127.0.0.1 --port 8470 ...
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main(argv):
    port, load_s = 8080, float(os.environ.get("STUB_LLAMA_LOAD_S") or 0.3)
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
    if os.environ.get("STUB_LLAMA_ARGV"):
        with open(os.environ["STUB_LLAMA_ARGV"], "w") as f:
            json.dump(argv, f)
    if os.environ.get("STUB_LLAMA_DIE"):
        sys.exit(3)
    ready_at = time.monotonic() + load_s

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
            if self.path == "/health":
                if time.monotonic() < ready_at:
                    return self.js(503, {"error": {"code": 503, "message": "Loading model"}})
                return self.js(200, {"status": "ok"})
            if self.path == "/props":
                return self.js(200, {"default_generation_settings": {"n_ctx": 32768}, "total_slots": 1})
            self.js(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if os.environ.get("STUB_LLAMA_BODY"):
                with open(os.environ["STUB_LLAMA_BODY"], "w") as f:
                    json.dump({"path": self.path, "body": body}, f)
            if self.path == "/v1/chat/completions":
                msgs = body.get("messages") or []
                text = "ANSWER-" + (msgs[-1].get("content", "")[-12:] if msgs else "")
                key = "delta"
            elif self.path == "/v1/completions":
                text = "ANSWER-" + str(body.get("prompt", ""))[-12:]
                key = "text"
            else:
                return self.js(404, {"error": "not found"})
            pieces = [text, " more", " words"]
            timings = {"prompt_n": 9, "prompt_ms": 45.0, "predicted_n": len(pieces), "predicted_ms": 60.0}
            usage = {"prompt_tokens": 9, "completion_tokens": len(pieces), "total_tokens": 9 + len(pieces)}
            if not body.get("stream"):
                if key == "delta":
                    ch = {"index": 0, "message": {"role": "assistant", "content": "".join(pieces)}, "finish_reason": "stop"}
                else:
                    ch = {"index": 0, "text": "".join(pieces), "finish_reason": "stop"}
                return self.js(200, {"object": "chat.completion", "model": body.get("model"), "choices": [ch],
                                     "usage": usage, "timings": timings})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def ev(obj):
                raw = ("data: " + json.dumps(obj) + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(raw), raw))
                self.wfile.flush()
            for i, piece in enumerate(pieces):
                ch = {"index": 0, "finish_reason": None}
                if key == "delta":
                    ch["delta"] = {"content": piece} if i else {"role": "assistant", "content": piece}
                else:
                    ch["text"] = piece
                ev({"object": "chat.completion.chunk", "model": body.get("model"), "choices": [ch]})
            last = {"index": 0, "finish_reason": "stop"}
            last["delta" if key == "delta" else "text"] = {} if key == "delta" else ""
            ev({"object": "chat.completion.chunk", "model": body.get("model"), "choices": [last],
                "usage": usage, "timings": timings})
            end = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(end), end))
            self.wfile.flush()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
