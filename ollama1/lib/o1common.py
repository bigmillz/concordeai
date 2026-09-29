"""Shared paths, config and small file helpers for the ollama1 host kit.

Everything the kit reads or writes lives under a fixed set of paths. For
tests and for a user-mode trial run, OLLAMA1_PREFIX moves all of them under
one folder (e.g. ~/ollama1-test), so nothing outside it is touched.

Nothing in this module (or anywhere in the kit) writes a prompt or an answer.
"""
import json
import os
import re
import tempfile

PREFIX = os.environ.get("OLLAMA1_PREFIX", "").rstrip("/")


def p(path):
    """A system path, moved under OLLAMA1_PREFIX when that is set."""
    return PREFIX + path


class Paths:
    etc = p("/etc/ollama1")
    config = p("/etc/ollama1/config.json")
    devices = p("/etc/ollama1/devices.json")
    allow = p("/etc/ollama1/models.allow")
    run = p("/run/ollama1")
    pair_dir = p("/run/ollama1/pair")
    window = p("/run/ollama1/pair/window.json")
    spool = p("/run/ollama1/pair-spool")
    stats_dir = p("/run/ollama1/stats")
    stats = p("/run/ollama1/stats/gateway.json")
    pull_status = p("/run/ollama1/stats/pull.json")
    gw_state = p("/var/lib/ollama1-gateway")
    counters = p("/var/lib/ollama1-gateway/counters.json")
    state = p("/var/lib/ollama1")
    update_status = p("/var/lib/ollama1/ollama-update.json")
    helper_status = p("/var/lib/ollama1/actions.json")
    admin_state = p("/var/lib/ollama1-admin")
    history = p("/var/lib/ollama1-admin/history.json")
    models = p("/srv/models")
    data = p("/srv/data")
    opt = p("/opt/ollama")
    u_stamp = p("/var/lib/apt/periodic/unattended-upgrades-stamp")
    reboot_required = p("/run/reboot-required")
    reboot_pkgs = p("/run/reboot-required.pkgs")


DEFAULTS = {
    "hostname_gateway": "ollama1.flyconcordefly.com",
    "hostname_admin": "ollama1-admin.flyconcordefly.com",
    # Filled in at install time on the desktop. Never committed.
    "access_team_domain": "",        # e.g. yourteam.cloudflareaccess.com
    "gateway_aud": "",               # Access application AUD tag (app host)
    "admin_aud": "",                 # Access application AUD tag (admin host)
    "admin_email": "",
    "service_token_client_id": "",   # optional: pin the one service token
    "lan_mode": False,
    "lan_bind": "192.168.86.10",     # br0 (setup.sh writes the real one)
    "lan_cidr": "192.168.86.0/24",
    "gateway_port": 8431,
    "admin_port": 8432,
    "ttyd_port": 8433,
    "tunnel_metrics_port": 8439,
    "ollama_url": "http://127.0.0.1:11434",
    "num_ctx_default": 8192,
    "num_ctx_max": 131072,
    "vram_reserve_mib": 768,
    "vram_total_bytes": 0,           # 0 = read from amdgpu sysfs
    "queue_max": 8,
    "queue_wait_s": 300,
    "max_body_mib": 64,
    # Tests only: a local fake JWKS. Refused unless allow_insecure_certs_url.
    "certs_url": "",
    "allow_insecure_certs_url": False,
}


def load_config(path=None):
    """Config from systemd's credential dir (root-only file handed in by
    LoadCredential=), else the given path, else /etc/ollama1/config.json."""
    if path is None:
        cred = os.environ.get("CREDENTIALS_DIRECTORY")
        if cred and os.path.exists(os.path.join(cred, "config.json")):
            path = os.path.join(cred, "config.json")
        else:
            path = Paths.config
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULTS:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg


def read_json(path, default=None, max_bytes=4 << 20):
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return default
        return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path, obj, mode=0o640, group=None):
    """Write counts/state JSON atomically. Only ever called with numbers,
    names and ids, never with request or response content."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"), sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if group is not None:
            try:
                import grp
                os.chown(tmp, -1, grp.getgrnam(group).gr_gid)
            except (KeyError, PermissionError, ImportError):
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,127}(:[A-Za-z0-9._\-]{1,64})?$")


def valid_model_name(name):
    return isinstance(name, str) and bool(MODEL_RE.match(name)) and ".." not in name


def read_allow_list(path=None):
    """The root-owned allow-list: one model name per line, # comments.
    Starts empty; only Patrick adds lines."""
    out = []
    try:
        with open(path or Paths.allow, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line and valid_model_name(line) and line not in out:
                    out.append(line)
    except OSError:
        pass
    return out


def name_hash(name):
    """12 hex chars that stand for a model name in a systemd instance name
    (ollama1-pull@<hash>.service), so no escaping is ever needed."""
    import hashlib
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def safe_label(s, limit=40):
    """A device or model name made safe for a log line or a terminal."""
    s = "".join(ch for ch in str(s) if ch.isprintable() and ch not in "\r\n\t\x1b")
    return s[:limit]
