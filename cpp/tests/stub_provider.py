"""A stand-in cloud provider for the C++ front's end-to-end checks
(CPP_PLAN.md step 1). OpenAI-shaped: /models, and /chat/completions that
streams its answer word by word, a short pause between words, so a test can
see the frames reach it one at a time through the front. Nothing real is
asked of any model; the dev copy reaches it through the server's own
`provider-stub=http://127.0.0.1:<port>` hook (dev copies only, 6b326), and
it doubles as the copy's HTTPS proxy so a call to a real provider host would
be recorded as a CONNECT instead of leaving this computer.

    python3 stub_provider.py [port] [--gap 0.12]

Prints its port on the first line. GET /_log returns every request it saw.
"""
import http.server
import json
import sys
import threading
import time

ANSWER = ("A stand-in provider wrote this reply for the C++ front's end-to-end check. "
          "It arrives a few words at a time, with a short pause between them, so the "
          "test can see each piece reach the page on its own.")
LOG = []
LOCK = threading.Lock()
GAP = 0.12


class Stub(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        with LOCK:
            LOG.append({"t": time.time(), "m": "CONNECT", "p": self.path})
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._any("GET")

    def do_POST(self):
        self._any("POST")

    def _any(self, m):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        p = self.path.split("?")[0]
        if p == "/_log":
            with LOCK:
                return self._out(200, list(LOG))
        with LOCK:
            LOG.append({"t": time.time(), "m": m, "p": p})
        try:
            d = json.loads(body) if body else {}
        except ValueError:
            d = {}
        if p.endswith("/models"):
            return self._out(200, {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.8-27b"}]})
        if p.endswith("/chat/completions"):
            if d.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                words = ANSWER.split(" ")
                try:
                    for i in range(0, len(words), 3):
                        piece = " ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "")
                        self.wfile.write(("data: %s\n\n" % json.dumps(
                            {"choices": [{"delta": {"content": piece}}]})).encode())
                        self.wfile.flush()
                        time.sleep(GAP)
                    self.wfile.write(("data: %s\n\ndata: [DONE]\n\n" % json.dumps(
                        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 40}})).encode())
                    self.wfile.flush()
                except OSError:
                    pass
                return
            return self._out(200, {"choices": [{"message": {"content": ANSWER}}],
                                   "usage": {"prompt_tokens": 9, "completion_tokens": 40}})
        return self._out(404, {"error": {"message": "not found"}})


def main():
    global GAP
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    if "--gap" in sys.argv:
        GAP = float(sys.argv[sys.argv.index("--gap") + 1])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Stub)
    srv.daemon_threads = True
    print(srv.server_address[1], flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
