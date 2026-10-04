"""Auto sleep for ollama1 (6b346): suspend the server when nobody has used
it for a while, and say which network cards can wake it again.

Three parts:
  * what the gateway writes: Activity (real work only: chat, generate and
    embeddings, never the app's polls) and the sleep settings (a state file
    the root service reads and does not trust);
  * the decision, one pure function: decide(inputs) -> (sleep?, why);
  * the root service's tick (Idle), with every probe injected so the tests
    run it on a fake clock.

The settings, the activity and the card list are numbers, booleans and MAC
addresses. Nothing here records what anyone asked or who asked.
"""
import json
import os
import re
import subprocess
import time

from o1common import Paths, read_json_safe, write_json_atomic

MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 5, 1440, 30
GPU_IDLE_PCT = 10            # busy below this counts as idle
ACTIVITY_STALE_S = 120       # a gateway that hasn't written this recently isn't running
RESUME_JUMP_S = 15           # wall clock ahead of the monotonic one by this: we slept
TICK_S = 30
LOAD_BUSY = 1.5              # a 1-minute load average above this means something is running (a detached build or test)
WAKE_REFRESH_S = 300
MAX_WAKE = 8
MAC_RX = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
VIRTUAL_NICS = re.compile(r"^(lo|veth|docker|br-|virbr|vnet|tap|tun|dummy|ifb|bond|wg|tailscale|zt|cni|flannel|vmnet)")
TOOL_SCRIPTS = ("stability-test.sh", "ram_model_test.py", "ram-model-test.sh", "setup.sh")   # named anywhere in the command
TOOL_PROGRAMS = ("apt", "apt-get", "dpkg", "unattended-upgrade", "unattended-upgrades")      # the program itself


def config_file():
    return os.path.join(Paths.gw_state, "sleep.json")


def activity_file():
    return os.path.join(Paths.stats_dir, "activity.json")


def idle_file():
    return os.path.join(Paths.run, "idle.json")


# ---- settings --------------------------------------------------------------------

def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def clamp_minutes(v):
    return max(MIN_MINUTES, min(MAX_MINUTES, v))


def clean_config(obj):
    """Whatever a file held, as {"enabled": bool, "minutes": int}: anything
    that isn't exactly a bool / a whole number falls back to off / 30."""
    obj = obj if isinstance(obj, dict) else {}
    return {"enabled": obj.get("enabled") is True,
            "minutes": clamp_minutes(obj["minutes"]) if _int(obj.get("minutes")) else DEFAULT_MINUTES}


def read_config(path=None):
    return clean_config(read_json_safe(path or config_file(), {}, max_bytes=4096))


def parse_config_update(obj):
    """A POST body -> (changes, error). Only `enabled` (a bool) and
    `minutes` (a whole number, clamped to 5..1440); a bool is not a number,
    and any other key or type is refused."""
    if not isinstance(obj, dict) or not obj:
        return None, "send enabled and/or minutes"
    if set(obj) - {"enabled", "minutes"}:
        return None, "only enabled and minutes can be set"
    out = {}
    if "enabled" in obj:
        if not isinstance(obj["enabled"], bool):
            return None, "enabled must be true or false"
        out["enabled"] = obj["enabled"]
    if "minutes" in obj:
        if not _int(obj["minutes"]):
            return None, "minutes must be a whole number"
        out["minutes"] = clamp_minutes(obj["minutes"])
    return out, ""


def describe_config(cfg=None, path=None):
    """One line for `ollama1-idle status`: the saved setting, as the root
    service will read it."""
    cfg = cfg or read_config(path)
    if cfg["enabled"]:
        return "auto sleep: on, after %d minutes idle (saved in %s)" % (cfg["minutes"], path or config_file())
    return "auto sleep: off (saved: %d minutes when turned on; file %s)" % (cfg["minutes"], path or config_file())


def write_config(cfg, path=None):
    """Saved where it survives a restart of the gateway or this service, a
    reboot and a setup.sh run: a state folder (never /run), written whole
    through a temp file and a rename, the folder flushed, mode 0600."""
    cfg = clean_config(cfg)
    write_json_atomic(path or config_file(), cfg, mode=0o600, sync_dir=True)
    return cfg


# ---- the admin panel's way to change it (6b402) -----------------------------------

SLEEPCFG_RX = re.compile(r"^(on|off)-([0-9]{1,4})$")


def sleepcfg_result_file():
    """What the last change from the panel did, for the panel to read back
    (o1gw writes it in the gateway's stats folder, group o1view)."""
    return os.path.join(Paths.stats_dir, "sleepcfg.json")


def sleepcfg_arg(enabled, minutes):
    """The unit instance name the panel starts: "on-30" / "off-45"."""
    return "%s-%d" % ("on" if enabled else "off", minutes)


def parse_sleepcfg_arg(arg):
    """"on-30" -> {"enabled": True, "minutes": 30} (minutes clamped to
    5..1440), or None for anything else, however it was spelled."""
    m = SLEEPCFG_RX.fullmatch(arg) if isinstance(arg, str) else None       # fullmatch: no trailing newline
    if not m:
        return None
    return {"enabled": m.group(1) == "on", "minutes": clamp_minutes(int(m.group(2)))}


def apply_sleepcfg(arg, path=None, result_path=None, clock=time.time):
    """Runs as the gateway's own user (ollama1-sleepcfg@.service): the same
    write_config as the gateway's /v1/sleep-config, so the file keeps its
    owner, mode and atomic write, and the app and the idle service see it.
    The saved file is read back and the answer says what it really holds.
    Returns (ok, result)."""
    want = parse_sleepcfg_arg(arg)
    res = {"req": arg if isinstance(arg, str) and len(arg) <= 16 else "", "at": clock()}
    if want is None:
        res.update(ok=False, error="not a valid request (on or off, then 5 to 1440 minutes)")
    else:
        try:
            write_config(want, path)
            back = read_config(path)
            res.update(ok=back == want, enabled=back["enabled"], minutes=back["minutes"])
            if back != want:
                res["error"] = "the file holds something else now (another change landed)"
        except OSError as e:
            res.update(ok=False, error="could not write the setting (%s)" % (e.strerror or "error"))
    try:
        write_json_atomic(result_path or sleepcfg_result_file(), res, mode=0o640, group="o1view")
    except OSError:
        pass
    return res["ok"], res


# ---- what the gateway records ----------------------------------------------------

class Activity:
    """Real work in progress and when it last happened. Written to a file
    the root service reads; /v1/info, /v1/usage and the other polls never
    touch it, so the app's sidebar can't keep the server awake."""

    def __init__(self, clock=time.time, path=None):
        import threading
        self.clock = clock
        self.path = path
        self.lock = threading.Lock()
        self.inflight = 0
        self.last = clock()          # a gateway that has just (re)started has not been idle for ever

    def begin(self):
        with self.lock:
            self.inflight += 1
            self.last = self.clock()
        self.write()

    def end(self):
        # no write here: the file follows within a second (the gateway's stats loop writes it),
        # and the handler must not be held up between its answer and letting go of its slot
        with self.lock:
            self.inflight = max(0, self.inflight - 1)
            self.last = self.clock()

    def touch(self):
        """Something that counts as work just happened without a request of its own
        (the app polling a running or just-finished generation job, 6b356)."""
        with self.lock:
            self.last = self.clock()

    def snapshot(self):
        with self.lock:
            return {"last": self.last, "inflight": self.inflight, "at": self.clock()}

    def write(self):
        try:
            write_json_atomic(self.path or activity_file(), self.snapshot(), mode=0o644)
        except OSError:
            pass


def read_activity(now, path=None):
    """{"last": epoch, "inflight": int, "fresh": bool}: never trusts the file.
    A file the gateway hasn't refreshed lately says nothing: the requests in
    flight are then unknown (None), not none."""
    d = read_json_safe(path or activity_file(), {}, max_bytes=4096)
    d = d if isinstance(d, dict) else {}

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e11 else None
    last, at = num(d.get("last")), num(d.get("at"))
    inflight = d.get("inflight")
    inflight = inflight if _int(inflight) and 0 <= inflight < 100000 else None
    fresh = at is not None and -5 <= now - at <= ACTIVITY_STALE_S
    return {"last": last or 0.0, "inflight": inflight if fresh else None, "fresh": fresh}


# ---- the decision ----------------------------------------------------------------

def decide(i):
    """(sleep now?, the reason, in a few plain words). Every condition has to
    hold; a probe that couldn't answer (None) blocks rather than guesses.

    Inputs: enabled, supported, minutes, now, last_activity, boot_time,
    last_resume (epoch seconds, or None), inflight,
    gpu_busy (percent) and gpu_present (False only on a machine with no
    graphics card: a card with no reading blocks), loadavg (1-minute; above
    LOAD_BUSY something is running), busy (reasons from o1sleep.busy_reasons),
    tools (names running), inhibitors (blocking locks)."""
    try:
        if i.get("enabled") is not True:
            return False, "auto sleep is off"
        if i.get("supported") is not True:
            return False, "this machine has no deep sleep"
        minutes = i.get("minutes")
        if not _int(minutes):
            return False, "the idle time isn't a number"
        minutes = clamp_minutes(minutes)
        now = float(i["now"])
        if i.get("inflight") is None:
            return False, "couldn't tell whether a request is running"
        if i["inflight"] > 0:
            return False, "a request is running"
        if i.get("busy") is None:
            return False, "couldn't ask what is running"
        if i["busy"]:
            return False, "busy: " + "; ".join(str(x) for x in i["busy"])
        if i.get("tools") is None:
            return False, "couldn't look for running tools"
        if i["tools"]:
            return False, "running: " + ", ".join(sorted(str(x) for x in i["tools"]))
        if i.get("inhibitors") is None:
            return False, "couldn't ask what blocks sleep"
        if i["inhibitors"]:
            return False, "something blocks sleep: " + "; ".join(str(x) for x in i["inhibitors"])[:120]
        la = i.get("loadavg")
        if la is None:
            return False, "couldn't read the load average"
        if la > LOAD_BUSY:
            return False, "the machine is busy (load %.1f)" % la
        g = i.get("gpu_busy")
        if g is None and i.get("gpu_present") is not False:
            return False, "no reading from the graphics card"
        if g is not None and g >= GPU_IDLE_PCT:
            return False, "the graphics card is busy (%d%%)" % g
        if i.get("boot_time") is None:
            return False, "couldn't tell when it started"
        last = min(float(i.get("last_activity") or 0), now)
        since = max(last, float(i["boot_time"]), float(i.get("last_resume") or 0))
        idle = now - since
        if idle < minutes * 60:
            return False, "idle %d of %d minutes" % (idle // 60, minutes)
        return True, "idle %d minutes" % (idle // 60)
    except (KeyError, TypeError, ValueError):
        return False, "couldn't read the inputs"


def idle_seconds(i):
    """Seconds counted since the latest of the last real work, the boot and
    the last resume (what decide() compares with the idle time), or None when
    the inputs don't say."""
    try:
        now = float(i["now"])
        last = min(float(i.get("last_activity") or 0), now)
        since = max(last, float(i["boot_time"]), float(i.get("last_resume") or 0))
        return int(max(0, now - since))
    except (KeyError, TypeError, ValueError):
        return None


# ---- probes (parsers are pure; the callers run the commands) ----------------------

def parse_sessions(text):
    """Logged-in sessions from `loginctl list-sessions --no-legend`."""
    return sum(1 for line in text.splitlines() if line.strip())


def parse_inhibitors(text):
    """Block-mode locks that stop sleep, from `systemd-inhibit --list
    --no-legend`: the last column is the mode and the What column names
    sleep. A lock that merely delays sleep doesn't count."""
    out = []
    for line in text.splitlines():
        t = line.split()
        if len(t) < 6 or t[-1] != "block":
            continue
        if any(re.fullmatch(r"(?:[a-z-]+:)*sleep(?::[a-z-]+)*", w) for w in t):
            out.append(" ".join(t)[:80])
    return out


def scan_tools(argvs):
    """Names of the long jobs running, from the processes' argument lists: a
    script of ours named anywhere in the command (python3 -u ram_model_test.py),
    apt, dpkg and the like only as the program itself (not as a word in vim's
    arguments)."""
    found = set()
    for argv in argvs:
        for i, a in enumerate(argv):
            b = os.path.basename(a)
            if b in TOOL_SCRIPTS or (i < 2 and b in TOOL_PROGRAMS):
                found.add(b)
    return sorted(found)


def run_text(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def sessions_count():
    t = run_text(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
    return None if t is None else parse_sessions(t)


def inhibitors_blocking():
    t = run_text(["systemd-inhibit", "--list", "--no-legend", "--no-pager"])
    return None if t is None else parse_inhibitors(t)


def tools_running(proc="/proc"):
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    argvs = []
    for p in pids:
        try:
            with open("%s/%s/cmdline" % (proc, p), "rb") as f:
                argvs.append([a.decode("utf-8", "replace") for a in f.read(4096).split(b"\0") if a])
        except OSError:
            continue
    return scan_tools(argvs)


def boot_time(stat="/proc/stat"):
    """When the machine started, or None (unknown: never sleep on a guess)."""
    try:
        for line in open(stat):
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def loadavg1():
    try:
        return float(os.getloadavg()[0])
    except (OSError, AttributeError):
        return None


def deep_sleep_supported(path=None):
    """/sys/power/mem_sleep lists the kinds of suspend-to-RAM ("s2idle
    [deep]"): deep sleep is there, or it isn't."""
    path = path or os.path.join(os.environ.get("OLLAMA1_SYS", "/sys"), "power", "mem_sleep")
    try:
        words = re.findall(r"[a-z0-9_]+", open(path).read())
    except OSError:
        return False
    return "deep" in words


# ---- which cards can wake the server --------------------------------------------

def _sysnet():
    return os.path.join(os.environ.get("OLLAMA1_SYS", "/sys"), "class", "net")


def _first_line(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def physical_nics(sysnet=None):
    """[(name, mac)] of the real network cards: the ones with a device behind
    them (not loopback, veth, docker, tap, bridges, VLANs or wifi), whether
    standalone or a member of a bridge (read from the bridge's brif)."""
    root = sysnet or _sysnet()
    try:
        names = set(os.listdir(root))
    except OSError:
        return []
    for n in list(names):
        try:
            names.update(os.listdir(os.path.join(root, n, "brif")))
        except OSError:
            pass
    out = []
    for n in sorted(names):
        d = os.path.join(root, n)
        if VIRTUAL_NICS.match(n) or not os.path.exists(os.path.join(d, "device")):
            continue
        if os.path.exists(os.path.join(d, "wireless")) or os.path.exists(os.path.join(d, "phy80211")):
            continue
        if _first_line(os.path.join(d, "type")) not in ("", "1"):
            continue
        mac = _first_line(os.path.join(d, "address")).lower()
        if MAC_RX.match(mac) and mac != "00:00:00:00:00:00":
            out.append((n, mac))
    return out


def parse_wol(text):
    """(supports magic-packet wake, has it switched on) from `ethtool <nic>`."""
    sup = on = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Supports Wake-on:"):
            sup = "g" in s.split(":", 1)[1]
        elif s.startswith("Wake-on:"):
            on = "g" in s.split(":", 1)[1]
    return sup, on


def read_wol(nic):
    t = run_text(["ethtool", nic])
    return None if t is None else parse_wol(t)


def wake_list(nics=None, wol=read_wol):
    """MACs of the cards with magic-packet wake switched on (at most 8)."""
    out = []
    for name, mac in (physical_nics() if nics is None else nics):
        w = wol(name)
        if w and w[1] and mac not in out:
            out.append(mac)
    return out[:MAX_WAKE]


def link_text(mac):
    """The card's .link file. The first .link that matches a card wins, so this
    one has to say what 99-default.link says (the names and MAC policy), not
    just the wake setting, or the card could come back as eth0 after a reboot."""
    return ("# ollama1: wake this computer with a magic packet\n[Match]\nMACAddress=%s\n\n[Link]\n"
            "NamePolicy=keep kernel database onboard slot path\nMACAddressPolicy=persistent\nWakeOnLan=magic\n"
            % mac)


def existing_links(link_dir, mac):
    """The 50-wol*.link files already there for this card's address (by hand
    or from an earlier setup), whatever they're called."""
    out = []
    try:
        names = sorted(os.listdir(link_dir))
    except OSError:
        return out
    rx = re.compile(r"(?im)^\s*MACAddress\s*=\s*%s\s*$" % re.escape(mac))
    for n in names:
        if n.startswith("50-wol") and n.endswith(".link"):
            try:
                with open(os.path.join(link_dir, n)) as f:
                    if rx.search(f.read()):
                        out.append(os.path.join(link_dir, n))
            except OSError:
                pass
    return out


def wol_setup(nics=None, wol=read_wol, link_dir="/etc/systemd/network", ethtool=None, log=print):
    """For each real card that can wake on a magic packet: its .link file
    (so the setting survives a reboot) and `ethtool -s <nic> wol g` now.
    Safe to run again: a file that is already right isn't touched. Returns
    the list of MACs set up; none, and nothing printed."""
    done = []
    for name, mac in (physical_nics() if nics is None else nics):
        w = wol(name)
        if not w or not w[0]:
            continue
        want = link_text(mac)
        # a file for this card is brought to the current shape where it is (the old one had only
        # the wake line); none yet: ours
        for path in existing_links(link_dir, mac) or [
                os.path.join(link_dir, "50-wol-%s.link" % re.sub(r"[^a-z0-9_-]", "", name.lower()))]:
            try:
                with open(path) as f:
                    have = f.read()
            except OSError:
                have = None
            if have != want:
                os.makedirs(link_dir, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    f.write(want)
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
                log("wake on a magic packet: %s" % path)
        if ethtool:
            ethtool(name)
        done.append(mac)
    return done


def ethtool_set(nic):
    subprocess.run(["ethtool", "-s", nic, "wol", "g"], capture_output=True, timeout=10)


def published_wake(path=None):
    """The card list the root service last wrote, MAC addresses only."""
    d = read_json_safe(path or idle_file(), {}, max_bytes=8192)
    w = d.get("wake") if isinstance(d, dict) else None
    return [m for m in (w if isinstance(w, list) else []) if isinstance(m, str) and MAC_RX.match(m)][:MAX_WAKE]


# ---- the root service's tick ------------------------------------------------------

class Idle:
    """One tick every TICK_S seconds. `probes` supplies inhibitors,
    tools, gpu_busy, busy, supported, wake; `suspend` is called when the
    decision is yes and returns 0 when the machine slept (or woke again)."""

    def __init__(self, probes, suspend, log=print, clock=time.time, mono=time.monotonic):
        self.p, self.suspend, self.log, self.clock, self.mono = probes, suspend, log, clock, mono
        self.prev = None
        self.last_resume = None
        self.last_reason = None
        self.wake = []
        self.wake_at = -1e9

    def _probe(self, name):
        try:
            return self.p[name]()
        except Exception:
            return None

    def tick(self):
        now, mono = self.clock(), self.mono()
        if self.prev is not None and (now - self.prev[0]) - (mono - self.prev[1]) > RESUME_JUMP_S:
            self.last_resume = now            # slept (or the clock jumped): that counts as activity
        self.prev = (now, mono)
        supported = bool(self._probe("supported"))
        if mono - self.wake_at >= WAKE_REFRESH_S:
            w = self._probe("wake")
            self.wake = w if isinstance(w, list) else []
            self.wake_at = mono
        cfg = read_config()
        act = read_activity(now)
        inputs = {
            "enabled": cfg["enabled"], "supported": supported, "minutes": cfg["minutes"], "now": now,
            "last_activity": act["last"], "boot_time": self._probe("boot_time"),
            "loadavg": self._probe("loadavg"), "gpu_present": self._probe("gpu_present"),
            "last_resume": self.last_resume, "inflight": act["inflight"],
            "gpu_busy": self._probe("gpu_busy"),
            "busy": self._probe("busy"), "tools": self._probe("tools"),
            "inhibitors": self._probe("inhibitors")}
        sleep, why = decide(inputs)
        try:
            # the server panel reads the setting (enabled, minutes) and this tick's DECISION from here
            # (6b396): sleep_ok, the exact reason decide() gave, and the idle seconds it counted
            write_json_atomic(idle_file(), {"at": int(now), "supported": supported, "wake": self.wake[:MAX_WAKE],
                                            "enabled": cfg["enabled"], "minutes": cfg["minutes"],
                                            "sleep_ok": bool(sleep), "reason": why[:160],
                                            "idle_s": idle_seconds(inputs)}, mode=0o644)
        except OSError:
            pass
        if why != self.last_reason:
            self.log("%s: %s" % ("sleeping" if sleep else "not sleeping", why))
            self.last_reason = why
        if sleep:
            rc = self.suspend()
            # however it ended (slept and woke, even a few seconds later, or was refused) the idle
            # clock starts again: a refused sleep isn't asked for again before a whole idle period
            self.last_resume = self.clock()
            self.last_reason = None
        return sleep, why
