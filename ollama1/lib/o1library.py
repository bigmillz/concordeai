"""The model library: make the installed models match the allow-list.

  plan()     what a sync would do, without doing it:
               remove    installed and NOT on the list (the space it frees)
               download  on the list, not installed
               update    installed and on the list, and the registry has a
                         newer manifest (found by comparing the manifest's
                         digest with the one installed: nothing is downloaded)
             Sizes count only the layers not already on disk.
  execute()  does exactly a plan, in that order (removals first, so their
             space is there for the downloads); each pull is tried 3 times.

Removal only ever touches models that are installed and not on the
allow-list: plan() computes it that way, and execute() checks each name
against the allow-list again right before deleting it (a download or update
whose model has left the list since the preview is skipped the same way).

Runs as root: the local Ollama API, the model folder (read only), and the
registry (manifests only). Nothing here is reachable through the gateway.
"""
import fcntl
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.request

import o1ollama
from o1common import Paths, load_config, parse_allow_list, read_json, valid_model_name, write_json_atomic

OLLAMA = "http://127.0.0.1:11434"
REGISTRY = "https://registry.ollama.ai"
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
TRIES = 3
MIN_FREE = 2 << 30          # a sync that would leave less than this on the disk is refused
PREVIEW_MAX_AGE = 30 * 60   # the panel's preview is good for this long


def library_dir():
    return os.path.join(Paths.run, "library")


def status_file():
    return os.path.join(library_dir(), "sync.json")


def preview_file():
    return os.path.join(library_dir(), "preview.json")


def lock_file():
    return os.path.join(library_dir(), "sync.lock")


def ollama_url(base=None):
    return base or load_config().get("ollama_url") or OLLAMA


def registry_base():
    if os.environ.get("OLLAMA1_PREFIX") and os.environ.get("OLLAMA1_REGISTRY"):
        return os.environ["OLLAMA1_REGISTRY"]   # tests only
    return REGISTRY


def split_name(name):
    """'qwen3:14b' -> ('registry.ollama.ai', 'library/qwen3', '14b')."""
    base, _, tag = name.partition(":")
    tag = tag or "latest"
    parts = base.split("/")
    if len(parts) > 1 and "." in parts[0]:
        return parts[0], "/".join(parts[1:]), tag
    return "registry.ollama.ai", base if "/" in base else "library/" + base, tag


def remote_manifest(name, timeout=20):
    """(manifest, sha256 hex of its bytes) from the registry, or (None, None).
    Ollama stores a pulled manifest byte for byte and lists its sha256 as the
    model's digest, so the two compare directly."""
    host, repo, tag = split_name(name)
    base = registry_base() if host == "registry.ollama.ai" else "https://" + host
    req = urllib.request.Request("%s/v2/%s/manifests/%s" % (base, repo, tag),
                                 headers={"Accept": MANIFEST_ACCEPT, "User-Agent": "ollama1-models"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4 << 20)
    except urllib.error.HTTPError as e:
        e.close()
        return None, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    try:
        m = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(m, dict):
        return None, None
    return m, hashlib.sha256(raw).hexdigest()


def layers(manifest):
    """{digest: size} of a manifest's layers and config."""
    out = {}
    for layer in list((manifest or {}).get("layers") or []) + [(manifest or {}).get("config") or {}]:
        if isinstance(layer, dict) and isinstance(layer.get("digest"), str):
            try:
                out[layer["digest"]] = max(0, int(layer.get("size") or 0))
            except (TypeError, ValueError):
                out[layer["digest"]] = 0
    return out


def have_blob(digest):
    return os.path.exists(os.path.join(Paths.models, "blobs", digest.replace(":", "-")))


def to_fetch(manifest):
    """Bytes of the layers not on disk yet (a shared layer is counted once)."""
    return sum(s for d, s in layers(manifest).items() if not have_blob(d))


def installed_models(base=None):
    """Local models (Ollama's 'cloud' stubs aren't models on this disk: they
    are left alone, and the gateway never serves them)."""
    st, data = o1ollama.call(ollama_url(base), "GET", "/api/tags", timeout=20)
    if st != 200 or not isinstance(data, dict):
        raise RuntimeError("Ollama isn't answering (systemctl status ollama)")
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("name")
            and not o1ollama.is_remote(m)]


def listed(name, allowed):
    return any(o1ollama.same_model(name, a) for a in allowed)


def allowed_names(allow_path=None):
    return [e["name"] for e in parse_allow_list(allow_path)[0]]


def disk_free():
    try:
        st = os.statvfs(Paths.models)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def plan(base=None, allow_path=None, manifest_fn=None):
    manifest_fn = manifest_fn or remote_manifest
    entries, errors = parse_allow_list(allow_path)
    allowed = [e["name"] for e in entries]
    installed = installed_models(base)
    download, update, remove, unknown = [], [], [], []
    for m in installed:
        if not listed(m["name"], allowed):
            try:
                size = max(0, int(m.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            remove.append({"name": m["name"], "bytes": size})
    for name in allowed:
        local = next((m for m in installed if o1ollama.same_model(m["name"], name)), None)
        manifest, digest = manifest_fn(name)
        if manifest is None:
            if local is None:
                unknown.append({"name": name, "why": "not installed, and the registry didn't answer "
                                                     "(misspelt, or offline?)"})
            else:
                unknown.append({"name": name, "why": "couldn't check for an update (the registry didn't answer)"})
            continue
        if local is None:
            download.append({"name": name, "bytes": to_fetch(manifest)})
        elif str(local.get("digest") or "").replace("sha256:", "") != digest:
            update.append({"name": local["name"], "bytes": to_fetch(manifest)})
    free = disk_free()
    down_b = sum(x["bytes"] for x in download)
    up_b = sum(x["bytes"] for x in update)
    freed = sum(x["bytes"] for x in remove)
    after = (free + freed - down_b - up_b) if free is not None else None
    p = {"download": download, "update": update, "remove": remove, "unknown": unknown,
         "allow_errors": errors, "installed": sorted(m["name"] for m in installed),
         "totals": {"download_bytes": down_b, "update_bytes": up_b, "freed_bytes": freed,
                    "free_now": free, "free_after": after},
         "enough_space": after is None or after >= MIN_FREE,
         "made_at": int(time.time())}
    p["id"] = plan_id(p)
    return p


def plan_id(p):
    """Stands for what a plan would do: which models are removed, downloaded
    and updated (not the byte counts, which can shift by a layer)."""
    key = json.dumps({k: sorted(x["name"] for x in p[k]) for k in ("download", "update", "remove")},
                     sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def nothing_to_do(p):
    return not (p["download"] or p["update"] or p["remove"])


# ---- progress --------------------------------------------------------------------

def eta_text(seconds):
    if seconds is None:
        return ""
    if seconds < 60:
        return "less than a minute left"
    return "about %d min left" % int(math.ceil(seconds / 60.0))


class Progress:
    """Bytes done over the whole sync, with a smoothed rate for the time left."""

    def __init__(self, total, clock=time.monotonic):
        self.total = max(0, int(total or 0))
        self.done_before = 0      # bytes of the models already finished
        self.current = 0          # bytes of the model now pulling
        self.rate = None
        self.clock = clock
        self.last = (clock(), 0)

    def update(self, current):
        now = self.clock()
        self.current = max(0, current)
        done = self.done_before + self.current
        t0, d0 = self.last
        if now - t0 >= 1.0:
            inst = max(0.0, (done - d0) / (now - t0))
            self.rate = inst if self.rate is None else 0.85 * self.rate + 0.15 * inst
            self.last = (now, done)

    def finish_model(self, size):
        self.done_before += max(0, size)
        self.current = 0

    def fraction(self):
        if not self.total:
            return 1.0
        return min(1.0, (self.done_before + self.current) / float(self.total))

    def eta(self):
        if not self.rate or not self.total:
            return None
        return max(0.0, (self.total - self.done_before - self.current) / self.rate)


def write_status(obj):
    try:
        os.makedirs(library_dir(), exist_ok=True)
        write_json_atomic(status_file(), dict(obj, at=int(time.time())), mode=0o640, group="o1view")
    except OSError:
        pass


def write_preview(p):
    os.makedirs(library_dir(), exist_ok=True)
    write_json_atomic(preview_file(), p, mode=0o640, group="o1view")


def read_preview(max_age=PREVIEW_MAX_AGE, now=None):
    p = read_json(preview_file())
    if not isinstance(p, dict) or not isinstance(p.get("id"), str):
        return None
    if (now or time.time()) - int(p.get("made_at") or 0) > max_age:
        return None
    return p


class Lock:
    """One sync at a time (the CLI's and the panel's). Held = busy for sleep."""

    def __init__(self):
        self.fd = None

    def __enter__(self):
        os.makedirs(library_dir(), exist_ok=True)
        self.fd = os.open(lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError("another model-library sync is running")
        return self

    def __exit__(self, *a):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


# ---- doing it ----------------------------------------------------------------

def pull_one(name, on_progress, base=None):
    """One streamed pull. Layers already on disk (reported complete at once)
    don't count toward the progress. Raises RuntimeError on failure."""
    conn = o1ollama.connect(ollama_url(base), timeout=3600)
    try:
        conn.request("POST", "/api/pull", body=json.dumps({"model": name, "stream": True}).encode(),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError("Ollama answered %d" % r.status)
        seen = {}
        while True:
            line = r.readline(1 << 16)
            if not line:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                raise RuntimeError(str(obj["error"])[:200])
            d, tot = obj.get("digest"), obj.get("total")
            if isinstance(d, str) and isinstance(tot, int) and tot > 0:
                done = int(obj.get("completed") or 0)
                if d not in seen:
                    seen[d] = {"cached": done >= tot, "done": 0, "total": tot}
                seen[d]["done"] = min(done, tot)
                live = [v for v in seen.values() if not v["cached"]]
                if live:
                    on_progress(sum(v["done"] for v in live), sum(v["total"] for v in live))
            if obj.get("status") == "success":
                return True
        raise RuntimeError("the download stopped before it finished")
    except OSError as e:
        raise RuntimeError("Ollama: %s" % e)
    finally:
        conn.close()


def delete_one(name, base=None):
    conn = o1ollama.connect(ollama_url(base), timeout=120)
    try:
        conn.request("DELETE", "/api/delete", body=json.dumps({"model": name}).encode(),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        return r.status == 200
    except OSError:
        return False
    finally:
        conn.close()


def execute(p, base=None, allow_path=None, report=None, tries=TRIES, sleep=time.sleep):
    """Carry out a plan (removals, then downloads, then updates). report(event)
    is called for progress. Returns the summary."""
    report = report or (lambda e: None)
    summary = {"removed": [], "downloaded": [], "updated": [], "failed": [], "skipped": []}
    allowed = allowed_names(allow_path)
    for item in p["remove"]:
        name = item["name"]
        if listed(name, allowed):   # back on the list since the preview: keep it
            summary["skipped"].append(name)
            continue
        if delete_one(name, base):
            summary["removed"].append(name)
            report({"phase": "remove", "name": name})
        else:
            summary["failed"].append({"name": name, "error": "delete failed"})
            report({"phase": "remove", "name": name, "error": "delete failed"})
    jobs = []
    for kind, item in [("download", x) for x in p["download"]] + [("update", x) for x in p["update"]]:
        if listed(item["name"], allowed):
            jobs.append((kind, item))
        else:                       # off the list since the preview: don't fetch it
            summary["skipped"].append(item["name"])
    prog = Progress(sum(x["bytes"] for _, x in jobs))
    for i, (kind, item) in enumerate(jobs):
        name = item["name"]
        for attempt in range(1, tries + 1):
            def on_progress(done, tot, kind=kind, name=name, attempt=attempt, i=i, item=item):
                # this model's share of the whole, scaled to the plan's size for it
                share = item["bytes"] * (done / float(tot)) if tot else 0
                prog.update(share)
                report({"phase": kind, "name": name, "index": i + 1, "count": len(jobs),
                        "model_done": done, "model_total": tot, "overall": prog.fraction(),
                        "eta": prog.eta(), "attempt": attempt})
            try:
                pull_one(name, on_progress, base)
                summary["downloaded" if kind == "download" else "updated"].append(name)
                break
            except RuntimeError as e:
                report({"phase": kind, "name": name, "error": str(e), "attempt": attempt, "tries": tries})
                if attempt == tries:
                    summary["failed"].append({"name": name, "error": str(e)})
                else:
                    sleep(min(30, 5 * attempt))
        prog.finish_model(item["bytes"])
    return summary


# ---- the allow-list -----------------------------------------------------------

def _write_allow(lines, allow_path):
    path = allow_path or Paths.allow
    tmp = path + ".o1new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _read_lines(allow_path):
    try:
        with open(allow_path or Paths.allow, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _line_name(raw):
    text = raw.split("#", 1)[0].strip()
    return text.split()[0] if text else None


def allow_add(name, ram=False, allow_path=None):
    """Put a model on the list (or change its ram flag). Returns what changed."""
    if not valid_model_name(name) or o1ollama.is_remote({"name": name}):
        raise ValueError("%r isn't a model name this server can run" % name)
    lines = _read_lines(allow_path)
    want = name + ("   ram" if ram else "")
    for i, raw in enumerate(lines):
        if _line_name(raw) and o1ollama.same_model(_line_name(raw), name):
            entry = next((e for e in parse_allow_list(allow_path)[0] if e["name"] == _line_name(raw)), None)
            if entry and entry["ram"] == bool(ram):
                return "already on the list"
            lines[i] = want
            _write_allow(lines, allow_path)
            return "flag changed"
    lines.append(want)
    _write_allow(lines, allow_path)
    if not listed(name, allowed_names(allow_path)):   # never leave a bad line behind
        _write_allow(lines[:-1], allow_path)
        raise ValueError("the allow-list refused %r" % name)
    return "added"


def allow_remove(name, allow_path=None):
    """Take a model off the list. Returns True if a line was removed."""
    lines = _read_lines(allow_path)
    keep = [raw for raw in lines if not (_line_name(raw) and o1ollama.same_model(_line_name(raw), name))]
    if len(keep) == len(lines):
        return False
    _write_allow(keep, allow_path)
    return True
