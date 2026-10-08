"""Model sets from the app (6b410).

The person picks Light, Recommended or Everything for a paired server in
ConcordeAI. The app works out which models that means for this server's
graphics card, shows the exact Download and Remove lists, and sends them:

    GET  /v1/models/state   the models on the disk, the allow-list, whether
                            anything is changing them, the plan running or
                            last run and its jobs, free disk and VRAM
    POST /v1/models/apply   {"plan", "add", "remove", "seen"}

Two sides, one module (like o1pair):
  * the gateway (o1gw) checks the request, refuses what it can see is wrong
    (busy, changed, in use), leaves the request in its own folder, starts
    ollama1-modelplan.service (the one unit polkit lets it start) and waits
    for root's answer;
  * root (bin/ollama1-modelplan) reads that file without trusting it, checks
    everything again, edits /etc/ollama1/models.allow (those lines, nothing
    else), then has Ollama delete the models to remove, first, and pull the
    ones to add, in the order given.

Files:
  /var/lib/ollama1-gateway/modelplan-request.json  o1gw 0600         the request (the gateway's folder)
  /run/ollama1/modelplan/answer.json               root:o1view 0640  root's answer to the last request
  /run/ollama1/modelplan/status.json               root:o1view 0640  the plan running or last run
  /run/ollama1/modelplan/plan.lock                 root:o1view 0640  held while a plan runs
  /var/lib/ollama1-modelplan/last.json             root:o1view 0640  the same record, across reboots

Only model names, a set's name, a device's id and name, and times are
written: nothing anyone asked a model.
"""
import fnmatch
import hashlib
import os
import re

import o1ollama
import o1sleep
from o1common import Paths, read_json_safe, write_json_atomic

UNIT = "ollama1-modelplan.service"
PLANS = ("light", "recommended", "everything")
KEYS = ("plan", "add", "remove", "seen")
# A model from Ollama's own library: no host, no namespace, no "/".
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}(:[a-z0-9][a-z0-9._-]{0,63})?$")
MAX_PER_LIST = 40
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
REQUEST_KEYS = ("v", "id", "t", "device") + KEYS
REQUEST_MAX_BYTES = 16384
REQUEST_MAX_AGE = 120             # seconds: root refuses an older request (or one from the future)
FILE_MAX_BYTES = 1 << 20
JOB_STATES = ("queued", "running", "done", "failed")
ACTIONS = ("remove", "pull")
# root's refusals, and what the gateway answers for each
REFUSALS = {"bad_request": 400, "unpaired": 403, "changed": 409, "busy": 409, "in_use": 409,
            "internal": 500, "ollama": 502}
# what may be changing the models, and how that is said
BUSY_UNITS = (
    (UNIT, "a model set is being applied"),
    ("ollama1-models-sync.service", "the model library is being synced"),
    ("ollama1-pull@*.service", "a model is being downloaded"),
    ("ollama1-rmmodel@*.service", "a model is being removed"),
)
LIBRARY_BUSY = "the model library is being synced"


def request_file():
    return os.path.join(Paths.gw_state, "modelplan-request.json")


def answer_file():
    return os.path.join(Paths.modelplan_run, "answer.json")


def status_file():
    return os.path.join(Paths.modelplan_run, "status.json")


def lock_file():
    return os.path.join(Paths.modelplan_run, "plan.lock")


def record_file():
    return os.path.join(Paths.modelplan_state, "last.json")


# ---- the request: one check for both sides ----------------------------------------

def norm(tag):
    """'gemma4' and 'gemma4:latest' are one model."""
    return tag if ":" in tag else tag + ":latest"


def valid_tag(t):
    """A library tag: the pattern, matched whole (no trailing newline), and
    never "..", which the allow-list would read as a bad line."""
    return isinstance(t, str) and TAG_RE.fullmatch(t) is not None and ".." not in t


def parse_request(obj):
    """A POST /v1/models/apply body -> ({"plan", "add", "remove", "seen"}, "")
    or (None, why). Exactly those four keys; `plan` one of the three; `add`
    and `remove` lists of at most 40 library tags (no host, no namespace, no
    "/"), no model twice, none in both, not both empty, no Ollama cloud model
    to add; `seen` the hex sha256 of the model names the app saw. The reason
    never repeats what was sent."""
    if not isinstance(obj, dict) or not obj:
        return None, "send plan, add, remove and seen"
    if any(k not in KEYS for k in obj):
        return None, "unknown key: send exactly plan, add, remove and seen"
    for k in KEYS:
        if k not in obj:
            return None, "%s is missing: send exactly plan, add, remove and seen" % k
    if not isinstance(obj["plan"], str) or obj["plan"] not in PLANS:
        return None, "plan must be light, recommended or everything"
    lists = {}
    for key in ("add", "remove"):
        v = obj[key]
        if not isinstance(v, list):
            return None, "%s must be a list of model names" % key
        if len(v) > MAX_PER_LIST:
            return None, "%s has more than %d models" % (key, MAX_PER_LIST)
        for i, t in enumerate(v):
            if not valid_tag(t):
                return None, ("%s[%d] is not a model from Ollama's library (lowercase letters, digits, '.', '_' "
                              "and '-', one ':' before the tag; no host, no namespace, no '/')" % (key, i))
            if key == "add" and o1ollama.is_remote({"name": t}):
                return None, "add[%d] is an Ollama cloud model: those never run on this server" % i
            if o1ollama.is_internal(t):
                return None, "%s[%d] is Ollama's own cache, not a model" % (key, i)
        if len({norm(t) for t in v}) != len(v):
            return None, "%s names a model twice" % key
        lists[key] = list(v)
    if {norm(t) for t in lists["add"]} & {norm(t) for t in lists["remove"]}:
        return None, "a model is in both add and remove"
    if not lists["add"] and not lists["remove"]:
        return None, "nothing to add or remove"
    seen = obj["seen"]
    if not isinstance(seen, str) or not HEX64_RE.fullmatch(seen):
        return None, "seen must be the hex sha256 of the model names on the server"
    return {"plan": obj["plan"], "add": lists["add"], "remove": lists["remove"], "seen": seen.lower()}, ""


def seen_hash(names):
    """What the app sends as `seen`: the hex sha256 of the names of the models
    on the server (GET /v1/models/state's "models"), sorted, joined by "\\n"."""
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()


def local_models(tags):
    """The models on this server's disk from an /api/tags answer. Ollama's
    cloud entries aren't (the gateway never serves them, a sync never touches
    them), nor are Ollama's own cache copies (llamacpp:<sha>, 6b448): never
    shown, never in `seen`, never removed."""
    models = tags.get("models") if isinstance(tags, dict) else None
    return o1ollama.user_models(models)


def loaded_names(ps):
    """Names of the models loaded now, from an /api/ps answer."""
    models = ps.get("models") if isinstance(ps, dict) else None
    out = []
    for m in models if isinstance(models, list) else []:
        n = (m.get("name") or m.get("model")) if isinstance(m, dict) else None
        if isinstance(n, str) and n:
            out.append(n)
    return out


def disk_free(path=None):
    """Bytes free on the models filesystem, or None."""
    try:
        st = os.statvfs(path or Paths.models)
        return int(st.f_bavail * st.f_frsize)
    except OSError:
        return None


# ---- the gateway's hand-off ----------------------------------------------------------

def write_request(rid, device, clean, now):
    """The request for root, in the gateway's own folder (0600, o1gw): the id
    root answers to, when, which device, and the checked request."""
    obj = {"v": 1, "id": rid, "t": int(now), "device": device}
    obj.update((k, clean[k]) for k in KEYS)
    write_json_atomic(request_file(), obj, mode=0o600)


def drop_request():
    try:
        os.unlink(request_file())
    except OSError:
        pass


def plan_running():
    """True while root's program holds the plan lock."""
    return o1sleep.setup_running(lock_file())


def busy_reasons(active_units):
    """Why the models can't change now ([] = they can): a plan running (its
    lock), a sync or `ollama1-models` at the server (the library lock), or a
    unit that changes models. active_units(patterns) lists the active ones,
    or None when systemd can't be asked; then only the locks count (root asks
    again and refuses when it can't tell)."""
    reasons = []
    if plan_running():
        reasons.append(BUSY_UNITS[0][1])
    elif o1sleep.setup_running(o1sleep.library_lock()):
        reasons.append(LIBRARY_BUSY)
    units = active_units([p for p, _ in BUSY_UNITS]) or []
    for pattern, why in BUSY_UNITS:
        if why not in reasons and any(fnmatch.fnmatchcase(u, pattern) for u in units):
            reasons.append(why)
    return reasons


# ---- what root writes, as the gateway and the panel read it -------------------------

def _int(v, lo=0, hi=1 << 62):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _text(v, n):
    return "".join(ch for ch in v if ch.isprintable())[:n] if isinstance(v, str) else ""


def clean_job(j):
    """{"name", "action", "state", "pct", "error"}, exactly, or None."""
    if not isinstance(j, dict) or not valid_tag(j.get("name")) or j.get("action") not in ACTIONS \
            or j.get("state") not in JOB_STATES:
        return None
    return {"name": j["name"], "action": j["action"], "state": j["state"],
            "pct": _int(j.get("pct"), 0, 100) or 0, "error": _text(j.get("error"), 200)}


def clean_plan(p):
    """{"name", "at", "by"}, exactly, or None."""
    if not isinstance(p, dict) or p.get("name") not in PLANS or _int(p.get("at")) is None:
        return None
    return {"name": p["name"], "at": p["at"], "by": _text(p.get("by"), 40)}


def clean_status(st):
    """A status record cut down to known fields of the right type, or None."""
    if not isinstance(st, dict) or st.get("state") not in ("running", "done"):
        return None
    plan = clean_plan(st.get("plan"))
    if plan is None:
        return None
    jobs = st.get("jobs") if isinstance(st.get("jobs"), list) else []
    lists = {}
    for k in ("add", "remove"):
        v = st.get(k) if isinstance(st.get(k), list) else []
        lists[k] = [t for t in v[:MAX_PER_LIST] if valid_tag(t)]
    rid = st.get("request")
    return {"request": rid if isinstance(rid, str) and ID_RE.fullmatch(rid) else "", "state": st["state"],
            "plan": plan, "jobs": [j for j in (clean_job(x) for x in jobs[:2 * MAX_PER_LIST]) if j],
            "add": lists["add"], "remove": lists["remove"],
            "started": _int(st.get("started")), "finished": _int(st.get("finished"))}


INTERRUPTED = "stopped before it finished (the server restarted, or the program was stopped)"


def read_status():
    """The plan running now or the last one: from /run while the server has
    been up, else the copy kept in /var/lib. A record that says running while
    nothing holds the plan lock was cut short (a crash, a power cut, a
    reboot): it reads as "interrupted" and its unfinished jobs as failed.
    The file is read again before saying so, in case the plan had just
    finished."""
    st = None
    for _attempt in (1, 2):
        st = clean_status(read_json_safe(status_file(), None, max_bytes=FILE_MAX_BYTES))
        if st is None:
            st = clean_status(read_json_safe(record_file(), None, max_bytes=FILE_MAX_BYTES))
        if st is None or st["state"] != "running" or plan_running():
            return st
    st["state"] = "interrupted"
    for j in st["jobs"]:
        if j["state"] in ("queued", "running"):
            j["state"], j["error"] = "failed", INTERRUPTED
    return st


def read_answer():
    """Root's answer to the last request, cut down to known fields, or None."""
    a = read_json_safe(answer_file(), None, max_bytes=FILE_MAX_BYTES)
    if not isinstance(a, dict) or not isinstance(a.get("request"), str) or not ID_RE.fullmatch(a["request"]):
        return None
    models = a.get("models") if isinstance(a.get("models"), list) else []
    return {"request": a["request"], "ok": a.get("ok") is True,
            "code": a.get("code") if a.get("code") in REFUSALS else "internal",
            "error": _text(a.get("error"), 300),
            "models": [n for n in models[:5000] if isinstance(n, str) and 0 < len(n) <= 200 and n.isprintable()],
            "name": a.get("name") if valid_tag(a.get("name")) else ""}
