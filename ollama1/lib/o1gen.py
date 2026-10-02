"""Image and video generation for ollama1 (6b356): the gateway builds a
ComfyUI workflow itself, runs it as a job the app polls, and hands back the
bytes.

ComfyUI is optional and local: it listens only on 127.0.0.1:8188 and only the
gateway's user (and root) may connect (the port guard, config/ollama1.nft.in).
The app never sends workflow JSON. It sends a kind, a prompt and a few numbers;
the gateway checks every one of them against a whitelist and caps and
substitutes them into a FIXED TEMPLATE below. Nothing else of a request can
reach ComfyUI.

What this module keeps:
  * jobs, in memory only: id (unguessable), the device that made it, state,
    progress, and, once done, the result bytes for RESULT_KEEP_S seconds.
    Nothing is written to a file and the prompt is never logged;
  * one job at a time (the card is shared with Ollama: the gateway's GPU slot
    is held from the start of a job to the end of it).

The gateway supplies the parts that belong to it (the GPU slot, unloading
Ollama's models, the VRAM reading, the activity record for auto sleep) as
`hooks`; every probe is injected so the tests run all of this on a fake ComfyUI.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import struct
import threading
import time
import urllib.parse

# ---- the models the templates use (as ComfyUI lists them) -------------------------

IMAGE_CKPT = "flux1-schnell-fp8.safetensors"            # models/checkpoints
WAN_UNET = "wan2.1_t2v_1.3B_fp16.safetensors"           # models/diffusion_models
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"     # models/text_encoders
WAN_VAE = "wan_2.1_vae.safetensors"                     # models/vae

# ---- caps -------------------------------------------------------------------------

PROMPT_MAX = 1000
IMAGE_SIDE = (256, 1536)          # width and height, multiples of 64
IMAGE_ALIGN = 64
IMAGE_MAX_PIXELS = 1_700_000      # 1536x1024 fits, 1536x1536 doesn't
IMAGE_DEFAULT = (1024, 1024)
IMAGE_STEPS = (1, 12)
IMAGE_STEPS_DEFAULT = 4           # FLUX.1 schnell is a 4-step model
VIDEO_SIDE = (256, 832)           # multiples of 16
VIDEO_ALIGN = 16
VIDEO_MAX_PIXELS = 400_000        # 832x480 (399,360) fits
VIDEO_DEFAULT = (832, 480)
VIDEO_FRAMES = (17, 81)           # Wan wants 4n+1 frames
VIDEO_SECONDS = (1, 5)
VIDEO_SECONDS_DEFAULT = 2
VIDEO_FPS = 16
VIDEO_STEPS = (10, 30)
VIDEO_STEPS_DEFAULT = 20
SEED_MAX = 2 ** 53
RESULT_MAX = {"image": 16 << 20, "video": 64 << 20}
JOB_TIMEOUT_S = {"image": 600, "video": 1800}     # the gateway's own ceiling (the app stops sooner)
RESULT_KEEP_S = 600               # a finished job's bytes are kept this long
KEEP_FINISHED = 2                 # and at most this many finished jobs
IDLE_CANCEL_S = 120               # a running job nobody has polled for this long is cancelled
ACTIVITY_TAIL_S = 120             # a finished job counts as activity this long after its last poll or fetch
CAPS_CACHE_S = 20
VRAM_WAIT_S = 30
JOB_ERROR = "generation failed"   # the one message a failed job carries

NEGATIVE_VIDEO = ("blurry, low quality, worst quality, jpeg artifacts, static, "
                  "deformed, extra fingers, watermark, subtitles")

ID_RX = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SUB_RX = re.compile(r"^[A-Za-z0-9_-]{0,64}$")
PNG = b"\x89PNG\r\n\x1a\n"


class GenError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


# ---- request checking -------------------------------------------------------------

_KEYS = {"image": {"kind", "prompt", "width", "height", "steps", "seed"},
         "video": {"kind", "prompt", "width", "height", "steps", "seed", "seconds", "frames"}}


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _bad(msg):
    return GenError(400, "bad_request", msg)


def _ranged(obj, key, default, lo, hi, align=1):
    v = obj.get(key, default)
    if not _int(v) or v < lo or v > hi or v % align:
        raise _bad("%s must be a whole number from %d to %d%s"
                   % (key, lo, hi, (", a multiple of %d" % align) if align > 1 else ""))
    return v


def parse_request(obj):
    """(kind, params) from a job request, or GenError 400. Whitelisted keys only;
    every number inside its cap."""
    if not isinstance(obj, dict):
        raise _bad("body must be a JSON object")
    kind = obj.get("kind")
    if kind not in _KEYS:
        raise _bad("kind must be image or video")
    extra = set(obj) - _KEYS[kind]
    if extra:
        raise _bad("not accepted for %s: %s" % (kind, ", ".join(sorted(str(k)[:20] for k in extra)[:4])))
    prompt = obj.get("prompt")
    if not isinstance(prompt, str):
        raise _bad("prompt is missing")
    prompt = "".join(ch for ch in prompt if ch >= " " or ch in "\n\t").strip()
    if not prompt:
        raise _bad("prompt is empty")
    if len(prompt) > PROMPT_MAX:
        raise _bad("prompt is longer than %d characters" % PROMPT_MAX)
    seed = obj.get("seed")
    if seed is None:
        seed = secrets.randbelow(2 ** 32)
    elif not _int(seed) or seed < 0 or seed > SEED_MAX:
        raise _bad("seed must be a whole number from 0 to %d" % SEED_MAX)
    if kind == "image":
        lo, hi = IMAGE_SIDE
        w = _ranged(obj, "width", IMAGE_DEFAULT[0], lo, hi, IMAGE_ALIGN)
        h = _ranged(obj, "height", IMAGE_DEFAULT[1], lo, hi, IMAGE_ALIGN)
        if w * h > IMAGE_MAX_PIXELS:
            raise _bad("a picture can have at most %d pixels" % IMAGE_MAX_PIXELS)
        steps = _ranged(obj, "steps", IMAGE_STEPS_DEFAULT, *IMAGE_STEPS)
        return kind, {"prompt": prompt, "width": w, "height": h, "steps": steps, "seed": seed}
    lo, hi = VIDEO_SIDE
    w = _ranged(obj, "width", VIDEO_DEFAULT[0], lo, hi, VIDEO_ALIGN)
    h = _ranged(obj, "height", VIDEO_DEFAULT[1], lo, hi, VIDEO_ALIGN)
    if w * h > VIDEO_MAX_PIXELS:
        raise _bad("a video can have at most %d pixels per frame" % VIDEO_MAX_PIXELS)
    steps = _ranged(obj, "steps", VIDEO_STEPS_DEFAULT, *VIDEO_STEPS)
    if "frames" in obj:
        frames = obj["frames"]
        if not _int(frames) or frames < VIDEO_FRAMES[0] or frames > VIDEO_FRAMES[1] or (frames - 1) % 4:
            raise _bad("frames must be 17 to 81 and one more than a multiple of 4")
    else:
        sec = obj.get("seconds", VIDEO_SECONDS_DEFAULT)
        if isinstance(sec, bool) or not isinstance(sec, (int, float)) \
                or not VIDEO_SECONDS[0] <= sec <= VIDEO_SECONDS[1]:
            raise _bad("seconds must be a number from %d to %d" % VIDEO_SECONDS)
        frames = min(VIDEO_FRAMES[1], int(round(sec * VIDEO_FPS)) // 4 * 4 + 1)
    return kind, {"prompt": prompt, "width": w, "height": h, "steps": steps, "seed": seed, "frames": frames}


# ---- the fixed templates ----------------------------------------------------------
# API-format ComfyUI graphs. Only the whitelisted numbers and the prompt are put
# into them (as plain JSON values: the prompt is the "text" of a CLIPTextEncode
# node and nothing else). The node that saves the result is named in SAVE_NODE so
# the answer is read from that node only.

SAVE_NODE = {"image": "7", "video-mp4": "11", "video-webp": "10"}


def image_workflow(p):
    """FLUX.1 schnell, the single-file fp8 checkpoint: ComfyUI's own example
    (4 steps, cfg 1, euler/simple, an empty negative that cfg 1 ignores)."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": IMAGE_CKPT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": p["prompt"], "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": p["width"], "height": p["height"], "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "seed": p["seed"], "steps": p["steps"], "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0], "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "o1gen"}},
    }


def video_workflow(p, fmt):
    """Wan 2.1 text-to-video 1.3B (ComfyUI's example: shift 8, cfg 6, uni_pc/simple).
    fmt "mp4": CreateVideo + SaveVideo, H.264 in an MP4 (what WKWebView plays).
    fmt "webp": SaveAnimatedWEBP, for a ComfyUI without those two nodes."""
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": WAN_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_CLIP, "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_VAE}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": p["prompt"], "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE_VIDEO, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyHunyuanLatentVideo",
              "inputs": {"width": p["width"], "height": p["height"], "length": p["frames"],
                         "batch_size": 1}},
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["7", 0], "seed": p["seed"], "steps": p["steps"], "cfg": 6.0,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["6", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
    }
    if fmt == "mp4":
        wf["10"] = {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": float(VIDEO_FPS)}}
        wf["11"] = {"class_type": "SaveVideo",
                    "inputs": {"video": ["10", 0], "filename_prefix": "o1gen", "format": "mp4",
                               "codec": "h264"}}
    else:
        wf["10"] = {"class_type": "SaveAnimatedWEBP",
                    "inputs": {"images": ["9", 0], "filename_prefix": "o1gen", "fps": float(VIDEO_FPS),
                               "lossless": False, "quality": 80, "method": "default"}}
    return wf


def build_workflow(kind, params, fmt=None):
    return image_workflow(params) if kind == "image" else video_workflow(params, fmt or "mp4")


# ---- what the result may be -------------------------------------------------------

def sniff(kind, data, fmt=None):
    """The content type of result bytes, from their first bytes, or None when they
    are not what this kind may return (a PNG or JPEG for an image; an MP4 for a
    video, or an animated WebP from the fallback template). Nothing else is
    relayed, whatever ComfyUI said."""
    if kind == "image":
        if data[:8] == PNG:
            return "image/png"
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        return None
    if fmt != "webp" and data[4:8] == b"ftyp":
        return "video/mp4"
    if fmt == "webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---- talking to ComfyUI -----------------------------------------------------------

class Comfy:
    """A small client for ComfyUI's HTTP API on loopback. Every call has a deadline."""

    def __init__(self, base):
        u = urllib.parse.urlsplit(base)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 8188
        # ComfyUI is reached on this machine only: any other address in the settings
        # turns generation off rather than sending a prompt somewhere else
        self.local = self.host in ("127.0.0.1", "localhost", "::1")

    def call(self, method, path, obj=None, timeout=15, cap=32 << 20, raw=False):
        """(status, parsed JSON | bytes). Raises OSError when ComfyUI isn't there."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            body = None if obj is None else json.dumps(obj).encode("utf-8")
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json"} if body is not None else {})
            r = conn.getresponse()
            data = r.read(cap + 1)
            if len(data) > cap:
                raise OSError("answer too large")
            if raw:
                return r.status, data
            try:
                return r.status, json.loads(data.decode("utf-8")) if data else None
            except ValueError:
                return r.status, None
        except http.client.HTTPException as e:
            raise OSError(str(type(e).__name__))
        finally:
            conn.close()


# the fixed handshake string of RFC 6455, in two pieces: it is a protocol constant, not an identifier
_WS_GUID = b"258EAFA5-E914-47DA-95CA-" b"C5AB0DC85B11"


class ProgressWatcher(threading.Thread):
    """ComfyUI reports a sampler's steps over a WebSocket, nowhere else. This is
    the smallest client that can read it (RFC 6455, no extensions, text frames
    only). Best effort: if it can't connect or the stream breaks, the job simply
    reports no progress. It never decides whether a job succeeded."""

    def __init__(self, comfy, client_id, on_progress, stop):
        super().__init__(daemon=True)
        self.comfy, self.client_id, self.on, self.stop = comfy, client_id, on_progress, stop
        self.prompt_id = None

    def _connect(self):
        s = socket.create_connection((self.comfy.host, self.comfy.port), timeout=5)
        try:
            key = base64.b64encode(os.urandom(16))
            s.sendall(b"GET /ws?clientId=" + self.client_id.encode("ascii") + b" HTTP/1.1\r\nHost: "
                      + ("%s:%d" % (self.comfy.host, self.comfy.port)).encode("ascii")
                      + b"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key
                      + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = s.recv(4096)
                if not chunk or len(head) > 16384:
                    raise OSError("no handshake")
                head += chunk
            first, _, rest = head.partition(b"\r\n\r\n")
            want = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest())
            if b" 101 " not in first.split(b"\r\n")[0] + b" " or want not in first:
                raise OSError("handshake refused")
            s.settimeout(1.0)
            return s, rest
        except Exception:
            s.close()
            raise

    @staticmethod
    def _frame(opcode, payload=b""):
        mask = os.urandom(4)
        head = bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
        return head + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def run(self):
        try:
            sock, buf = self._connect()
        except OSError:
            return
        try:
            def need(n):
                nonlocal buf
                while len(buf) < n:
                    if self.stop.is_set():
                        raise OSError("stopped")
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise OSError("closed")
                    buf += chunk
                out, buf = buf[:n], buf[n:]
                return out
            while not self.stop.is_set():
                b0, b1 = need(2)
                op, ln = b0 & 0x0F, b1 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", need(2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", need(8))[0]
                if ln > (16 << 20):
                    return                          # nothing we want is this big
                mask = need(4) if b1 & 0x80 else None
                payload = need(ln) if ln else b""
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if op == 8:
                    return
                if op == 9:
                    sock.sendall(self._frame(10, payload[:100]))
                elif op == 1:
                    self._message(payload)
        except (OSError, struct.error):
            return
        finally:
            sock.close()

    def _message(self, payload):
        try:
            msg = json.loads(payload.decode("utf-8"))
            data = msg.get("data") or {}
            pid = data.get("prompt_id")
            if pid is not None and self.prompt_id is not None and pid != self.prompt_id:
                return
            if msg.get("type") == "progress":
                cur, top = data.get("value"), data.get("max")
            elif msg.get("type") == "progress_state":
                running = [n for n in (data.get("nodes") or {}).values()
                           if isinstance(n, dict) and n.get("state") == "running"]
                if not running:
                    return
                cur, top = running[0].get("value"), running[0].get("max")
            else:
                return
            if _int(cur) and _int(top) and top > 0 and 0 <= cur <= top:
                self.on(cur / top)
        except (ValueError, AttributeError, UnicodeDecodeError):
            return


# ---- jobs -------------------------------------------------------------------------

class Job:
    def __init__(self, jid, kind, dev_id, params, now):
        self.id, self.kind, self.dev_id, self.params = jid, kind, dev_id, params
        self.state = "queued"
        self.progress = 0.0
        self.created = self.last_touch = now
        self.finished = None            # when it stopped (done or failed)
        self.data = None
        self.ctype = None
        self.cancel = threading.Event()
        self.held = True                # counted as activity until released
        self.fmt = None
        self.client_id = secrets.token_hex(16)
        self.prompt_id = None
        self.dropped = False            # cancelled: forget it when the worker lets go


class Generator:
    """hooks (all supplied by the gateway):
      slot_acquire() -> bool   take the GPU slot without waiting
      slot_release()
      unload() -> None         unload every model Ollama holds (raises GenError when it can't be confirmed)
      vram() -> (used, total)  bytes, either may be None
    """

    def __init__(self, url, hooks, clock=time.time, activity=None, log=None, poll_s=1.0, vram_poll_s=0.5):
        self.comfy = Comfy(url)
        self.hooks = hooks
        self.clock = clock
        self.activity = activity
        self.log = log or (lambda *a, **k: None)
        self.poll_s = poll_s
        self.vram_poll_s = vram_poll_s
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None              # the job whose worker is running
        self.caps_lock = threading.Lock()
        self.caps_at = None
        self.caps = {"image": False, "video": False}
        self.video_fmt = None

    # -- capabilities ---------------------------------------------------------
    @staticmethod
    def _options(info, node, field):
        """The names a combo input lists, from /object_info; None when it isn't there.
        ComfyUI has two spellings: [[names...], {...}] and ["COMBO", {"options": [...]}]."""
        try:
            spec = info[node]["input"]["required"][field]
            first = spec[0]
            if isinstance(first, list):
                return [str(x) for x in first]
            if first == "COMBO" and isinstance(spec[1].get("options"), list):
                return [str(x) for x in spec[1]["options"]]
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        return None

    def probe(self):
        """What this ComfyUI can do right now: {"image": bool, "video": bool}, and the
        video format that will be used. True only when ComfyUI answers AND the
        model files the template needs are listed AND the nodes it uses exist."""
        caps, fmt = {"image": False, "video": False}, None
        if not self.comfy.local:
            return caps, fmt
        try:
            st, info = self.comfy.call("GET", "/object_info", timeout=10, cap=64 << 20)
        except OSError:
            st, info = 0, None
        if st == 200 and isinstance(info, dict):
            def has(*nodes):
                return all(isinstance(info.get(n), dict) for n in nodes)

            def lists(node, field, name):
                return name in (self._options(info, node, field) or [])
            caps["image"] = (has("CheckpointLoaderSimple", "CLIPTextEncode", "EmptySD3LatentImage",
                                 "KSampler", "VAEDecode", "SaveImage")
                             and lists("CheckpointLoaderSimple", "ckpt_name", IMAGE_CKPT))
            base = (has("UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode",
                        "EmptyHunyuanLatentVideo", "ModelSamplingSD3", "KSampler", "VAEDecode")
                    and lists("UNETLoader", "unet_name", WAN_UNET)
                    and lists("CLIPLoader", "clip_name", WAN_CLIP)
                    and lists("VAELoader", "vae_name", WAN_VAE))
            if base and has("CreateVideo", "SaveVideo"):
                caps["video"], fmt = True, "mp4"
            elif base and has("SaveAnimatedWEBP"):
                caps["video"], fmt = True, "webp"
        return caps, fmt

    def capabilities(self, fresh=False):
        with self.caps_lock:
            now = time.monotonic()
            if fresh or self.caps_at is None or now - self.caps_at >= CAPS_CACHE_S:
                self.caps, self.video_fmt = self.probe()
                self.caps_at = now
            return {"image": bool(self.caps["image"]), "video": bool(self.caps["video"])}

    # -- the public calls -----------------------------------------------------
    def running(self):
        with self.lock:
            return self.active is not None

    def create(self, obj, dev_id):
        self.sweep()
        kind, params = parse_request(obj)
        if self.capabilities()[kind] is not True:
            raise GenError(503, "unavailable", "%s generation is not set up on this server" % kind)
        with self.lock:
            if self.active is not None:
                raise GenError(409, "busy", "the server is already making one")
        if not self.hooks.slot_acquire():
            raise GenError(409, "busy", "the server is busy with a chat; try again shortly")
        job = None
        try:
            with self.lock:
                if self.active is not None:
                    raise GenError(409, "busy", "the server is already making one")
                job = Job(secrets.token_urlsafe(24), kind, dev_id, params, self.clock())
                job.fmt = self.video_fmt if kind == "video" else None
                self.active = job
                self._drop_old_locked()
                self.jobs[job.id] = job
        except Exception:
            self.hooks.slot_release()
            raise
        if self.activity:
            self.activity.begin()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job.id

    def _find(self, jid, dev_id):
        if not isinstance(jid, str) or not ID_RX.match(jid):
            raise GenError(404, "not_found", "no such job")
        with self.lock:
            j = self.jobs.get(jid)
        if j is None or j.dev_id != dev_id or j.dropped or j.cancel.is_set():
            raise GenError(404, "not_found", "no such job")
        return j

    def _touch(self, j):
        j.last_touch = self.clock()
        if self.activity:
            self.activity.touch()

    def status(self, jid, dev_id):
        self.sweep()
        j = self._find(jid, dev_id)
        self._touch(j)
        out = {"state": j.state, "kind": j.kind,
               "progress": round(j.progress, 3) if j.state in ("running", "done") else None}
        if j.state == "done":
            out["progress"] = 1.0
            out["type"], out["bytes"] = j.ctype, len(j.data)
        if j.state == "failed":
            out["error"] = JOB_ERROR
        return out

    def result(self, jid, dev_id):
        self.sweep()
        j = self._find(jid, dev_id)
        self._touch(j)
        if j.state != "done":
            raise GenError(409, "not_ready", "the result is not ready")
        return j.ctype, j.data

    def cancel(self, jid, dev_id):
        """Stop a running job, or forget a finished one."""
        self.sweep()
        j = self._find(jid, dev_id)
        j.cancel.set()
        with self.lock:
            if j.finished is not None:
                j.dropped = True
                self.jobs.pop(j.id, None)
                j.data = None
        self._release(j)

    def sweep(self):
        """Forget what has expired, and let go of activity nobody is polling for."""
        now = self.clock()
        with self.lock:
            for j in list(self.jobs.values()):
                if j.finished is not None and now - j.finished >= RESULT_KEEP_S:
                    self.jobs.pop(j.id, None)
                    j.data = None
                    j.dropped = True
            snap = list(self.jobs.values())
        for j in snap:
            if j.held and j.finished is not None and now - max(j.last_touch, j.finished) >= ACTIVITY_TAIL_S:
                self._release(j)
            if j.finished is None and now - j.last_touch >= IDLE_CANCEL_S:
                j.cancel.set()          # nobody is asking any more: stop wasting the card

    def _drop_old_locked(self):
        done = sorted((j for j in self.jobs.values() if j.finished is not None), key=lambda j: j.finished)
        for j in done[:max(0, len(done) - KEEP_FINISHED + 1)]:
            self.jobs.pop(j.id, None)
            j.data = None
            j.dropped = True
            if j.held:
                threading.Thread(target=self._release, args=(j,), daemon=True).start()

    def _release(self, j):
        with self.lock:
            if not j.held:
                return
            j.held = False
        if self.activity:
            self.activity.end()

    # -- the worker -----------------------------------------------------------
    def _fail(self, j, why):
        j.state = "failed"
        self.log("gen", kind=j.kind, status="failed", reason=why)

    def _wait_vram(self, j):
        """After Ollama let go: wait (up to VRAM_WAIT_S) for the card to look free.
        An unreadable card, or one still busy after the wait, doesn't stop the job:
        ComfyUI says plainly if it can't fit."""
        end = time.monotonic() + VRAM_WAIT_S
        while not j.cancel.is_set():
            try:
                used, total = self.hooks.vram()
            except Exception:
                return
            if used is None or total is None:
                return
            if used <= max(int(2.5 * (1 << 30)), total * 0.15):
                return
            if time.monotonic() >= end:
                return
            time.sleep(self.vram_poll_s)

    def _find_output(self, j, entry):
        """The one file the save node wrote: (filename, subfolder, type) or None."""
        node = SAVE_NODE["image"] if j.kind == "image" else SAVE_NODE["video-" + (j.fmt or "mp4")]
        out = ((entry.get("outputs") or {}).get(node)) if isinstance(entry, dict) else None
        if not isinstance(out, dict):
            return None
        for key in ("images", "videos", "gifs", "video"):
            for item in out.get(key) or []:
                if isinstance(item, dict) and isinstance(item.get("filename"), str):
                    name, sub, typ = item["filename"], str(item.get("subfolder") or ""), item.get("type", "output")
                    if NAME_RX.match(name) and SUB_RX.match(sub) and typ in ("output", "temp"):
                        return name, sub, typ
        return None

    def _fetch(self, j, where):
        name, sub, typ = where
        q = urllib.parse.urlencode({"filename": name, "subfolder": sub, "type": typ})
        cap = RESULT_MAX[j.kind]
        st, data = self.comfy.call("GET", "/view?" + q, timeout=120, cap=cap, raw=True)
        if st != 200:
            return None
        return data

    def _run(self, j):
        t0 = time.monotonic()
        stop = threading.Event()
        watcher = None
        pid = None
        try:
            self.hooks.unload()
            if j.cancel.is_set():
                return
            self._wait_vram(j)
            if j.cancel.is_set():
                return
            wf = build_workflow(j.kind, j.params, j.fmt)
            st, ans = self.comfy.call("POST", "/prompt", {"prompt": wf, "client_id": j.client_id}, timeout=30)
            if st != 200 or not isinstance(ans, dict) or not isinstance(ans.get("prompt_id"), str):
                return self._fail(j, "refused")
            pid = j.prompt_id = ans["prompt_id"]
            j.state, j.progress = "running", 0.05
            watcher = ProgressWatcher(self.comfy, j.client_id,
                                      lambda f: setattr(j, "progress", max(j.progress, min(0.95, 0.05 + 0.9 * f))),
                                      stop)
            watcher.prompt_id = pid
            watcher.start()
            deadline = time.monotonic() + JOB_TIMEOUT_S[j.kind]
            errors = 0
            while True:
                if j.cancel.is_set():
                    self._interrupt(pid)
                    return
                if time.monotonic() > deadline:
                    self._interrupt(pid)
                    return self._fail(j, "timeout")
                time.sleep(self.poll_s)
                try:
                    st, hist = self.comfy.call("GET", "/history/" + pid, timeout=15)
                    errors = 0
                except OSError:
                    errors += 1
                    if errors >= 10:
                        return self._fail(j, "comfyui_gone")
                    continue
                entry = hist.get(pid) if st == 200 and isinstance(hist, dict) else None
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                if status.get("status_str") == "error":
                    return self._fail(j, "comfyui_error")
                where = self._find_output(j, entry)
                if where is None:
                    if status.get("completed") is True:
                        return self._fail(j, "no_output")
                    continue
                data = self._fetch(j, where)
                ctype = sniff(j.kind, data, j.fmt) if data else None
                if ctype is None:
                    return self._fail(j, "bad_output")
                j.data, j.ctype, j.progress, j.state = data, ctype, 1.0, "done"
                self.log("gen", kind=j.kind, status="done", ms=int((time.monotonic() - t0) * 1000))
                return
        except GenError as e:
            self._fail(j, e.code)
        except OSError:
            self._fail(j, "comfyui_gone")
        except Exception as e:                      # never echo anything the job carried
            self.log("error", kind=type(e).__name__)
            self._fail(j, "internal")
        finally:
            stop.set()
            try:
                self._cleanup(pid)
            finally:
                now = self.clock()
                with self.lock:
                    j.finished = now
                    if j.state in ("queued", "running"):
                        j.state = "failed"
                    if j.cancel.is_set() and j.state != "done":
                        j.dropped = True
                        self.jobs.pop(j.id, None)
                    if self.active is j:
                        self.active = None
                self.hooks.slot_release()
                if j.dropped:
                    self._release(j)

    def _interrupt(self, pid):
        for path, body in (("/interrupt", {"prompt_id": pid}), ("/queue", {"delete": [pid]})):
            try:
                self.comfy.call("POST", path, body, timeout=10)
            except OSError:
                pass

    def _cleanup(self, pid):
        """Give the card back: ComfyUI forgets the prompt and unloads its models, so a
        chat can load again."""
        if pid:
            try:
                self.comfy.call("POST", "/history", {"delete": [pid]}, timeout=10)
            except OSError:
                pass
        try:
            self.comfy.call("POST", "/free", {"unload_models": True, "free_memory": True}, timeout=30)
        except OSError:
            pass
