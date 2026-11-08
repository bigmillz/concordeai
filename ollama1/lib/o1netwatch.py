"""Wi-Fi recovery (6b524): a server that is up but offline gets itself back online. stdlib only, like o1logguard
and o1bootguard, so tools/install-watchdogs.sh can put it on a running server without replacing any library the
rest of the kit is using.

ollama1-netwatch.timer runs `ollama1-netwatch run` (root) every minute. Each run:

  PROBES   the default gateway (from /proc/net/route; ping, else a TCP connect: a refusal is an answer too) and
           two public addresses, 1.1.1.1 and 8.8.8.8 (a TCP connect to 443, else ping). ONLINE = the gateway OR
           the internet answers. An ISP outage with the home network up is online: a reboot would not help.
  TUNNEL   ollama1-tunnel.service, tracked on its own: off (not enabled: LAN mode or no tunnel), up (active, and
           cloudflared's /ready on its metrics port says it holds an edge connection) or down.

  ESCALATION while offline (one step per run, in this order; a later step only after the earlier ones):
     3 min  reconnect: `nmcli connection up id <the Wi-Fi connection>` (NetworkManager), or, with networkd,
            `networkctl reconfigure <card>` and a restart of netplan's wpa_supplicant unit for the card
     6 min  radio: `nmcli radio wifi off`, 5 s, `nmcli radio wifi on` (networkd: the link down and up)
    10 min  driver: the card's driver let go of it and take it again (sysfs unbind and bind, the device found
            from /sys/class/net/<card>/device). OFF unless config.json says "netwatch_driver_reload": true:
            a driver that hangs in its remove path can wedge the kernel, and one that fails to bind again leaves
            the server with no Wi-Fi at all until a reboot, so it is the owner's choice.
    20 min  reboot: `systemctl start ollama1-reboot.service` (the kit's own reboot path). Never within 30 min of
            boot, never sooner than 2 h after the last reboot this program asked for (4 h, 8 h, then 12 h when the
            reboots before did not bring it back), never in a boot the boot-loop guard tripped, never during the
            install's trial, never while setup.sh, apt/dpkg or a gateway request is running.
  After the ladder the soft steps repeat: reconnect every 5 min, radio every 15 min, driver every 60 min.

  TUNNEL down while online for 10 min: `systemctl restart ollama1-tunnel.service`, then at most once every 30 min.

REPAIRS come first in every run, so a run killed half-way can never leave the server worse: a radio this program
switched off is switched on again (NetworkManager remembers the radio across reboots), and a card it unbound is
bound again. A NetworkManager radio found off for any other reason while the server is offline is switched on too
(unless paused): offline, with the radio off, nothing else can help.

NEVER a configuration change: no netplan, no NetworkManager profile (only `connection up` of an existing one, never
`device connect`, which can make one), no firewall, no sshd, no cloudflared file. It reconnects, restarts or reboots.

/etc/ollama1/netwatch.off pauses every action (the repairs of its own half-done steps still run); status only.
Each action is a line in the journal and an entry in /var/lib/ollama1/netwatch.json, which is also the status for
the admin panel ("status" inside it).
"""
import errno
import fcntl
import grp
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

PREFIX = os.environ.get("OLLAMA1_PREFIX", "").rstrip("/")


def p(path):
    return PREFIX + path


def proc():
    return os.environ.get("OLLAMA1_PROC", "/proc")


def sysfs():
    return os.environ.get("OLLAMA1_SYS", "/sys")


STATE = "/var/lib/ollama1/netwatch.json"
PAUSE = "/etc/ollama1/netwatch.off"
CONFIG = "/etc/ollama1/config.json"
CLOUDFLARED_YML = "/etc/ollama1/cloudflared.yml"
ACTIVITY = "/run/ollama1/stats/activity.json"     # the gateway's requests in flight (o1idle.Activity)
SETUP_LOCK = "/run/ollama1-setup.lock"            # setup.sh holds it (o1sleep.setup_running)
BOOTGUARD = "/var/lib/ollama1/bootguard.json"
TRIAL = "/var/lib/ollama1/watchdogs-trial.json"
TUNNEL_UNIT = "ollama1-tunnel.service"
REBOOT_UNIT = "ollama1-reboot.service"
PUBLIC = ("1.1.1.1", "8.8.8.8")
METRICS_DEFAULT = 8439                            # config/cloudflared.yml.in: metrics: 127.0.0.1:8439

# (step, offline this long before it, then again this often; None: see reboot_hold)
STEPS = (("reconnect", 180, 300), ("radio", 360, 900), ("driver", 600, 3600), ("reboot", 1200, None))
REBOOT_MIN_UPTIME = 1800
REBOOT_SPACING = 7200
REBOOT_SPACING_MAX = 43200
TUNNEL_AFTER = 600
TUNNEL_SPACING = 1800
RADIO_PAUSE_S = 5
HISTORY = 30
ACTIVITY_STALE_S = 120                            # o1idle.ACTIVITY_STALE_S
PKG_PROCS = ("dpkg", "apt", "apt-get", "unattended-upgr")

IFACE_RX = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
PCI_RX = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
DRIVER_RX = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
BOOT_ID_RX = re.compile(r"^[0-9a-f-]{32,36}$")


# ---- small helpers -----------------------------------------------------------------------------------------

def _read(path, limit=65536):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def read_json(path, default=None):
    t = _read(path, 1 << 20)
    if t is None:
        return default
    try:
        d = json.loads(t)
    except ValueError:
        return default
    return d if isinstance(d, dict) else default


def write_json(path, obj, mode=0o640, group="o1view"):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if group and os.geteuid() == 0:
            try:
                os.chown(tmp, 0, grp.getgrnam(group).gr_gid)
            except (KeyError, OSError):
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_cmd(argv, timeout=30):
    """(returncode, stdout). A missing program or a timeout is (127 / 124, '')."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout or ""
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""


def nm_split(line):
    """One line of `nmcli -t`: fields split at unescaped ':', with '\\:' and '\\\\' unescaped."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def parse_route(text):
    """The default route with the lowest metric from /proc/net/route: (iface, gateway ip) or (None, None)."""
    best = None
    for line in (text or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dest, gw, flags, metric = int(f[1], 16), int(f[2], 16), int(f[3], 16), int(f[6])
        except ValueError:
            continue
        if dest != 0 or not flags & 0x1 or not flags & 0x2 or gw == 0:
            continue
        if best is None or metric < best[0]:
            best = (metric, f[0], socket.inet_ntoa(struct.pack("<I", gw)))
    return (best[1], best[2]) if best else (None, None)


def metrics_port(text):
    for line in (text or "").splitlines():
        m = re.match(r"^\s*metrics:\s*(?:127\.0\.0\.1|localhost):(\d{1,5})\s*$", line)
        if m and 0 < int(m.group(1)) < 65536:
            return int(m.group(1))
    return METRICS_DEFAULT


def lock_held(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def gateway_inflight(now):
    """The gateway's requests in flight, read the way o1idle.read_activity reads them: a file the gateway hasn't
    written for 2 minutes says nothing (None)."""
    d = read_json(p(ACTIVITY), {}) or {}
    at, n = d.get("at"), d.get("inflight")
    if not isinstance(at, (int, float)) or isinstance(at, bool) or not -5 <= now - at <= ACTIVITY_STALE_S:
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 100000 else None


def busy_reasons(now=None):
    """Why a reboot must wait (and why the installer must wait): [] when nothing is running."""
    now = time.time() if now is None else now
    out = []
    n = gateway_inflight(now)
    if n:
        out.append("the gateway is serving %d request%s" % (n, "" if n == 1 else "s"))
    if lock_held(p(SETUP_LOCK)):
        out.append("setup.sh is running")
    seen = set()
    try:
        pids = [d for d in os.listdir(proc()) if d.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        comm = (_read(os.path.join(proc(), pid, "comm"), 64) or "").strip()
        if comm in PKG_PROCS and comm not in seen:
            seen.add(comm)
            out.append("packages are being installed (%s)" % comm)
    return out


# ---- what the machine looks like ------------------------------------------------------------------------------

class System:
    """Everything a run looks at. Every outside call goes through `run`, `connect` and `http`, so tests fake them."""

    def __init__(self, run=run_cmd, connect=None, http=None, clock=time.monotonic, wall=time.time):
        self.run = run
        self.connect = connect or self._connect
        self.http = http or self._http
        self.clock, self.wall = clock, wall

    @staticmethod
    def _connect(ip, port, timeout=3.0):
        """True when the address answered: a connection, or a refusal (a reset is an answer from the host)."""
        try:
            s = socket.create_connection((ip, port), timeout=timeout)
            s.close()
            return True
        except ConnectionRefusedError:
            return True
        except OSError:
            return False

    @staticmethod
    def _http(url, timeout=3.0):
        """The HTTP status, or None when nothing answered."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def boot_id(self):
        t = (_read(os.path.join(proc(), "sys", "kernel", "random", "boot_id")) or "").strip()
        return t if BOOT_ID_RX.match(t) else "unknown"

    def ping(self, ip):
        rc, _ = self.run(["ping", "-n", "-q", "-c", "1", "-W", "2", ip], 6)
        return None if rc == 127 else rc == 0

    def probe_gateway(self, ip):
        r = self.ping(ip)
        if r:
            return True
        return any(self.connect(ip, port, 2.0) for port in (53, 80, 443))

    def probe_public(self, ip):
        if self.connect(ip, 443, 3.0):
            return True
        return bool(self.ping(ip))

    def wifi_iface(self):
        base = os.path.join(sysfs(), "class", "net")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        for n in names:
            if IFACE_RX.match(n) and (os.path.isdir(os.path.join(base, n, "wireless"))
                                      or os.path.exists(os.path.join(base, n, "phy80211"))):
                return n
        return None

    def nm_devices(self):
        rc, out = self.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"], 15)
        if rc != 0:
            return None
        return [nm_split(line) for line in out.splitlines() if line.strip()]

    def backend(self, iface):
        """'nm' when NetworkManager manages the card, 'networkd' otherwise (the kit's own Wi-Fi backup), or None."""
        devs = self.nm_devices()
        if devs is not None:
            for f in devs:
                if len(f) >= 3 and (f[0] == iface or (iface is None and f[1] == "wifi")) and f[2] != "unmanaged":
                    return "nm"
        rc, _ = self.run(["networkctl", "--no-pager", "--no-legend", "list"], 10)
        return "networkd" if rc == 0 else ("nm" if devs is not None else None)

    def nm_active_wifi(self):
        rc, out = self.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], 15)
        if rc != 0:
            return None
        for line in out.splitlines():
            f = nm_split(line)
            if len(f) >= 2 and f[1] == "802-11-wireless" and f[0]:
                return f[0]
        return None

    def nm_wifi_profiles(self):
        rc, out = self.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], 15)
        if rc != 0:
            return []
        return [f[0] for f in (nm_split(l) for l in out.splitlines()) if len(f) >= 2 and f[1] == "802-11-wireless" and f[0]]

    def nm_radio(self):
        rc, out = self.run(["nmcli", "radio", "wifi"], 10)
        if rc != 0:
            return None
        w = out.strip().lower()
        return True if w == "enabled" else False if w == "disabled" else None

    def link_up(self, iface):
        t = _read(os.path.join(sysfs(), "class", "net", iface, "flags")) if iface else None
        try:
            return bool(int(t.strip(), 16) & 0x1) if t else None
        except ValueError:
            return None

    def driver(self, iface):
        """The card's PCI device and driver, found from sysfs and checked: {"ok", "pci", "driver", "module",
        "bound"} or {"ok": False, "why"}."""
        if not iface:
            return {"ok": False, "why": "no Wi-Fi card"}
        base = os.path.join(sysfs(), "class", "net", iface, "device")
        try:
            dev = os.path.realpath(base)
        except OSError:
            return {"ok": False, "why": "no device for %s" % iface}
        pci = os.path.basename(dev)
        if not os.path.isdir(dev) or not PCI_RX.match(pci):
            return {"ok": False, "why": "%s is not a PCI device" % iface}
        if os.path.basename(os.path.realpath(os.path.join(dev, "subsystem"))) != "pci":
            return {"ok": False, "why": "%s is not on the PCI bus" % iface}
        try:
            nets = sorted(os.listdir(os.path.join(dev, "net")))
        except OSError:
            nets = []
        if nets and nets != [iface]:
            return {"ok": False, "why": "the device also carries %s" % ", ".join(n for n in nets if n != iface)}
        drv_link = os.path.join(dev, "driver")
        bound = os.path.exists(drv_link)
        drv = os.path.basename(os.path.realpath(drv_link)) if bound else None
        if bound and not DRIVER_RX.match(drv or ""):
            return {"ok": False, "why": "an unexpected driver name"}
        mod = None
        if bound and os.path.exists(os.path.join(drv_link, "module")):
            mod = os.path.basename(os.path.realpath(os.path.join(drv_link, "module")))
        return {"ok": bound, "why": None if bound else "no driver is bound", "pci": pci, "driver": drv, "module": mod,
                "bound": bound}

    def bound(self, pci):
        return os.path.exists(os.path.join(sysfs(), "bus", "pci", "devices", pci, "driver"))

    def tunnel(self):
        rc, out = self.run(["systemctl", "is-enabled", TUNNEL_UNIT], 10)
        if out.strip() not in ("enabled", "enabled-runtime"):
            return "off"
        rc, out = self.run(["systemctl", "is-active", TUNNEL_UNIT], 10)
        if out.strip() != "active":
            return "down"
        code = self.http("http://127.0.0.1:%d/ready" % metrics_port(_read(p(CLOUDFLARED_YML))))
        if code is None:
            return "up"                     # active, and its metrics did not answer: nothing to say it is down
        return "up" if code == 200 else "down"

    def config(self):
        c = read_json(p(CONFIG), {}) or {}
        return {"driver_reload": c.get("netwatch_driver_reload") is True}

    def guard_tripped(self, bid):
        st = read_json(p(BOOTGUARD), {}) or {}
        return bool(bid) and st.get("tripped_boot") == bid

    def observe(self, st):
        """One look at everything decide() needs."""
        bid = self.boot_id()
        iface, gw = parse_route(_read(os.path.join(proc(), "net", "route")))
        probes = {}
        if gw:
            probes["gateway"] = {"ip": gw, "via": iface, "ok": self.probe_gateway(gw)}
        else:
            probes["gateway"] = {"ip": None, "ok": False, "why": "no default route"}
        for ip in PUBLIC:
            probes[ip] = {"ok": self.probe_public(ip)}
        online = any(v["ok"] for v in probes.values())
        wifi = self.wifi_iface()
        backend = self.backend(wifi)
        w = {"iface": wifi, "backend": backend, "connection": None, "radio": None}
        if backend == "nm":
            w["connection"] = self.nm_active_wifi()
            w["radio"] = self.nm_radio()
        elif backend == "networkd" and wifi:
            up = self.link_up(wifi)
            w["radio"] = None if up is None else up
        cfg = self.config()
        drv = self.driver(wifi)
        unb = (st or {}).get("unbound")
        if isinstance(unb, dict) and PCI_RX.match(str(unb.get("pci", ""))):
            drv["pending_bound"] = self.bound(unb["pci"])
        mono = self.clock()
        return {
            "boot_id": bid, "mono": mono, "wall": self.wall(), "online": online, "probes": probes,
            "tunnel": self.tunnel() if online else "unknown (offline)", "wifi": w, "cfg": cfg, "driver": drv,
            "paused": os.path.exists(p(PAUSE)), "guard_tripped": self.guard_tripped(bid),
            "trial": os.path.exists(p(TRIAL)), "busy": busy_reasons(self.wall()),
        }


# ---- the decision (pure: everything comes in, nothing goes out) ---------------------------------------------------

def new_boot(st, bid):
    """A state from another boot keeps only what is about earlier boots."""
    st = dict(st or {})
    if st.get("boot_id") == bid:
        return st
    keep = {k: st[k] for k in ("last_reboot_wall", "last_reboot_boot", "reboots_since_online", "wifi_connection",
                               "history", "last_online_wall", "radio_pending") if k in st}
    keep["boot_id"] = bid
    return keep


def spacing(n):
    """How long after the last reboot asked for another may come, when `n` reboots have not brought it back."""
    return min(REBOOT_SPACING_MAX, REBOOT_SPACING * 2 ** max(0, n - 1))


def reboot_hold(st, o):
    """None when a reboot may happen now, else why not."""
    if o["guard_tripped"]:
        return "the boot-loop guard tripped this boot"
    if o["trial"]:
        return "the install's trial is running (its rollback deadline decides)"
    if o["mono"] < REBOOT_MIN_UPTIME:
        return "less than %d min since boot" % (REBOOT_MIN_UPTIME // 60)
    gap = spacing(int(st.get("reboots_since_online") or 0))
    last = st.get("last_reboot_wall")
    mine = st.get("last_reboot_mono")
    if st.get("last_reboot_boot") == o["boot_id"] and isinstance(mine, (int, float)):
        # asked for in this very boot, and the server is still up: only this boot's own clock counts
        if o["mono"] - mine < gap:
            return "a reboot was asked for %d min ago and has not happened" % ((o["mono"] - mine) // 60)
        last = None
    # Every earlier reboot came before this boot began, so an uptime of `gap` proves it whatever the clock says.
    # Short of that the wall clock must show it; a clock that went back (or jumped years) proves nothing.
    if isinstance(last, (int, float)) and o["mono"] < gap and not gap <= o["wall"] - last < 10 * 365 * 86400:
        return "the last reboot this program asked for was less than %d h ago" % (gap // 3600)
    if o["busy"]:
        return "busy: " + "; ".join(o["busy"])
    return None


def decide(st, o):
    """(state, [actions], held). The state comes back with the bookkeeping for the actions already done in it
    (the run saves it before acting, so a run killed half-way never repeats a reboot)."""
    st = new_boot(st, o["boot_id"])
    st["done"] = dict(st.get("done") or {})
    acts, held = [], None
    now = o["mono"]
    w = o["wifi"]

    # repairs of this program's own half-done steps: always, even paused
    if st.get("radio_pending") or (w.get("backend") == "nm" and w.get("radio") is False and not o["online"]
                                   and not o["paused"]):
        acts.append({"do": "radio_on", "why": "the radio was left off" if st.get("radio_pending") else "the radio is off"})
    unb = st.get("unbound")
    if isinstance(unb, dict):
        if o["driver"].get("pending_bound") is False:
            acts.append({"do": "driver_bind", "pci": unb.get("pci"), "driver": unb.get("driver"),
                         "why": "the card was left without its driver"})
        elif o["driver"].get("pending_bound"):
            st["unbound"] = None

    if o["online"]:
        if st.get("offline_since") is not None:
            st["back_online_after_s"] = int(now - st["offline_since"])
        st["offline_since"] = None
        st["done"] = {}
        st["reboots_since_online"] = 0
        st["last_online_wall"] = o["wall"]
        if w.get("connection"):
            st["wifi_connection"] = w["connection"]
    elif st.get("offline_since") is None:
        st["offline_since"] = now

    if o["paused"]:
        st["tunnel_down_since"] = None
        return st, acts, "paused (%s): status only" % PAUSE

    # the tunnel, only while the network is up
    if o["online"] and o["tunnel"] == "down":
        if st.get("tunnel_down_since") is None:
            st["tunnel_down_since"] = now
        last = st.get("last_tunnel_restart")
        if now - st["tunnel_down_since"] >= TUNNEL_AFTER and (last is None or now - last >= TUNNEL_SPACING):
            acts.append({"do": "tunnel_restart", "why": "the tunnel has been down %d min with the network up"
                         % ((now - st["tunnel_down_since"]) // 60)})
            st["last_tunnel_restart"] = now
    else:
        st["tunnel_down_since"] = None

    if o["online"]:
        return st, acts, None

    off = now - st["offline_since"]
    done = st["done"]
    ladder = [s for s in STEPS if s[0] != "driver" or (o["cfg"].get("driver_reload") and o["driver"].get("ok"))]
    chosen = None
    for name, at, _rep in ladder:
        if name == "reboot":
            if off >= at:
                held = reboot_hold(st, o)
                if held is None:
                    chosen = name
                else:
                    held = "reboot held: " + held
            break
        if name not in done:
            if off >= at:
                chosen = name
            break
    if chosen is None:
        for name, at, rep in ladder:
            if rep and name in done and off >= at and now - done[name] >= rep:
                chosen = name
                break
    if chosen:
        a = {"do": chosen, "why": "offline for %d min" % (off // 60)}
        if chosen == "driver":
            a.update(pci=o["driver"]["pci"], driver=o["driver"]["driver"])
        if chosen == "reconnect":
            a["connection"] = w.get("connection") or st.get("wifi_connection")
        acts.append(a)
        done[chosen] = now
        if chosen == "reboot":
            st["last_reboot_wall"] = o["wall"]
            st["last_reboot_boot"] = o["boot_id"]
            st["last_reboot_mono"] = now
            st["reboots_since_online"] = int(st.get("reboots_since_online") or 0) + 1
    return st, acts, held


# ---- doing it ----------------------------------------------------------------------------------------------

class Actions:
    """The only commands this program runs that change anything. Each returns (ok, detail)."""

    def __init__(self, sysm, run=None, sleep=time.sleep):
        self.sys = sysm
        self.run = run or sysm.run
        self.sleep = sleep

    def _ok(self, argv, timeout=60):
        rc, _ = self.run(argv, timeout)
        return rc == 0, "%s: %s" % (" ".join(argv[:4]), "ok" if rc == 0 else "exit %d" % rc)

    def reconnect(self, a, w):
        iface, backend = w.get("iface"), w.get("backend")
        if backend == "nm":
            name = a.get("connection")
            if not name:
                profiles = self.sys.nm_wifi_profiles()
                name = profiles[0] if len(profiles) == 1 else None
            if not name:
                return False, "no Wi-Fi connection to bring up (none active, none remembered, not exactly one saved)"
            rc, _ = self.run(["nmcli", "-w", "45", "connection", "up", "id", name], 60)
            return rc == 0, "nmcli connection up: %s" % ("ok" if rc == 0 else "exit %d" % rc)
        if backend == "networkd" and iface:
            ok, d = self._ok(["networkctl", "reconfigure", iface], 30)
            unit = "netplan-wpa-%s.service" % iface
            if os.path.exists(p("/run/systemd/system/" + unit)):
                ok2, d2 = self._ok(["systemctl", "restart", unit], 60)
                return ok and ok2, d + "; " + d2
            return ok, d
        return False, "no Wi-Fi card or network manager to ask"

    def radio_off(self, w):
        if w.get("backend") == "nm":
            return self._ok(["nmcli", "radio", "wifi", "off"], 30)
        if w.get("iface"):
            return self._ok(["ip", "link", "set", "dev", w["iface"], "down"], 15)
        return False, "no Wi-Fi card"

    def radio_on(self, w):
        if w.get("backend") == "nm":
            return self._ok(["nmcli", "radio", "wifi", "on"], 30)
        if w.get("iface"):
            ok, d = self._ok(["ip", "link", "set", "dev", w["iface"], "up"], 15)
            if ok and w.get("backend") == "networkd":
                self.run(["networkctl", "reconfigure", w["iface"]], 30)
            return ok, d
        return False, "no Wi-Fi card"

    def _sysfs_write(self, path, value):
        try:
            with open(path, "w") as f:
                f.write(value)
            return True, None
        except OSError as e:
            return False, e.strerror or "write failed"

    def unbind(self, pci, driver):
        if not PCI_RX.match(pci or "") or not DRIVER_RX.match(driver or ""):
            return False, "refused: not a checked device or driver"
        ok, why = self._sysfs_write(os.path.join(sysfs(), "bus", "pci", "drivers", driver, "unbind"), pci)
        return ok, "unbind %s from %s: %s" % (pci, driver, "ok" if ok else why)

    def bind(self, pci, driver):
        if not PCI_RX.match(pci or "") or not DRIVER_RX.match(driver or ""):
            return False, "refused: not a checked device or driver"
        ok, why = self._sysfs_write(os.path.join(sysfs(), "bus", "pci", "drivers", driver, "bind"), pci)
        if not ok:   # the kernel's own matching, as at boot
            ok, why = self._sysfs_write(os.path.join(sysfs(), "bus", "pci", "drivers_probe"), pci)
        return ok, "bind %s to %s: %s" % (pci, driver, "ok" if ok else why)

    def tunnel_restart(self):
        return self._ok(["systemctl", "restart", TUNNEL_UNIT], 90)

    def reboot(self):
        ok, d = self._ok(["systemctl", "start", "--no-block", REBOOT_UNIT], 30)
        if ok:
            return ok, d
        ok2, d2 = self._ok(["systemctl", "reboot"], 30)
        return ok2, d + "; " + d2


def execute(a, st, o, act, save, sleep=time.sleep):
    """One action, with the marks a half-done one needs saved first. Returns (ok, detail)."""
    w = o["wifi"]
    do = a["do"]
    if do == "reconnect":
        return act.reconnect(a, w)
    if do == "radio":
        st["radio_pending"] = True
        save(st)
        ok, d = act.radio_off(w)
        sleep(RADIO_PAUSE_S)
        ok2, d2 = act.radio_on(w)
        if ok2:
            st["radio_pending"] = False
        return ok and ok2, d + "; " + d2
    if do == "radio_on":
        ok, d = act.radio_on(w)
        if ok:
            st["radio_pending"] = False
        return ok, d
    if do == "driver":
        st["unbound"] = {"pci": a["pci"], "driver": a["driver"]}
        save(st)
        ok, d = act.unbind(a["pci"], a["driver"])
        sleep(2)
        ok2, d2 = act.bind(a["pci"], a["driver"])
        if ok2:
            st["unbound"] = None
        return ok and ok2, d + "; " + d2
    if do == "driver_bind":
        ok, d = act.bind(a.get("pci"), a.get("driver"))
        if ok:
            st["unbound"] = None
        return ok, d
    if do == "tunnel_restart":
        return act.tunnel_restart()
    if do == "reboot":
        return act.reboot()
    return False, "unknown action"


def status_of(st, o, held):
    off = None if st.get("offline_since") is None else int(o["mono"] - st["offline_since"])
    return {
        "at": int(o["wall"]), "online": o["online"], "probes": o["probes"], "tunnel": o["tunnel"],
        "wifi": {k: o["wifi"].get(k) for k in ("iface", "backend", "radio")}, "offline_for_s": off,
        "paused": o["paused"], "held": held, "guard_tripped": o["guard_tripped"], "trial": o["trial"],
        "driver_step": ("on" if o["cfg"].get("driver_reload") and o["driver"].get("ok") else
                        "off (config)" if not o["cfg"].get("driver_reload") else "off (%s)" % o["driver"].get("why")),
        "reboots_since_online": int(st.get("reboots_since_online") or 0),
        "last_reboot_wall": st.get("last_reboot_wall"),
    }


def run_once(sysm, act=None, log=print, load=None, save=None, sleep=time.sleep):
    """One run of the timer. Returns the saved state."""
    load = load or (lambda: read_json(p(STATE), {}) or {})
    save = save or (lambda s: write_json(p(STATE), s))
    act = act or Actions(sysm, sleep=sleep)
    old = load()
    o = sysm.observe(old)
    was_offline = old.get("boot_id") == o["boot_id"] and old.get("offline_since") is not None
    st, acts, held = decide(old, o)
    if o["online"] and was_offline:
        log("netwatch: online again after %d min" % (st.get("back_online_after_s", 0) // 60))
    elif not o["online"] and not was_offline:
        log("netwatch: OFFLINE: the gateway (%s) and %s do not answer" % (o["probes"]["gateway"].get("ip") or "no route",
                                                                          " and ".join(PUBLIC)))
    if held != st.get("held_logged"):
        if held:
            log("netwatch: " + held)
        st["held_logged"] = held
    if acts:
        save(st)                                   # the bookkeeping first: a run killed now never repeats a reboot
    for a in acts:
        log("netwatch: %s (%s)" % (a["do"], a["why"]))
        ok, detail = execute(a, st, o, act, save, sleep=sleep)
        log("netwatch: %s %s: %s" % (a["do"], "done" if ok else "FAILED", detail))
        hist = list(st.get("history") or [])
        hist.append({"at": int(o["wall"]), "do": a["do"], "ok": ok, "why": a["why"], "detail": detail[:200]})
        st["history"] = hist[-HISTORY:]
    st["status"] = status_of(st, o, held)
    save(st)
    return st


def repair(sysm, act=None, log=print):
    """--uninstall: undo this program's own half-done steps (a radio it switched off, a card it unbound) and nothing
    else. Returns the number of repairs that failed."""
    st = read_json(p(STATE), {}) or {}
    act = act or Actions(sysm)
    bad = 0
    if st.get("radio_pending"):
        iface = sysm.wifi_iface()
        ok, d = act.radio_on({"iface": iface, "backend": sysm.backend(iface)})
        log("netwatch repair: radio on: " + d)
        bad += 0 if ok else 1
    unb = st.get("unbound")
    if isinstance(unb, dict) and PCI_RX.match(str(unb.get("pci", ""))) and not sysm.bound(unb["pci"]):
        ok, d = act.bind(unb["pci"], unb.get("driver"))
        log("netwatch repair: " + d)
        bad += 0 if ok else 1
    return bad


def ago(t, now):
    s = max(0, int(now - t))
    return "%d s" % s if s < 90 else "%d min" % (s // 60) if s < 5400 else "%.1f h" % (s / 3600)


def render_status(st, now=None):
    now = time.time() if now is None else now
    s = (st or {}).get("status")
    if not s:
        return "Network watch: has not run yet (systemctl list-timers ollama1-netwatch.timer)"
    head = "online" if s.get("online") else "OFFLINE for %s" % ago(0, s.get("offline_for_s") or 0)
    lines = ["Network watch: %s%s (checked %s ago)" % (head, ", PAUSED" if s.get("paused") else "", ago(s.get("at", now), now))]
    pr = s.get("probes") or {}
    g = pr.get("gateway") or {}
    lines.append("  gateway      %s %s" % (g.get("ip") or "(no default route)", "answers" if g.get("ok") else "no answer"))
    for ip in PUBLIC:
        lines.append("  %-12s %s" % (ip, "answers" if (pr.get(ip) or {}).get("ok") else "no answer"))
    w = s.get("wifi") or {}
    lines.append("  Wi-Fi        %s (%s), radio %s" % (w.get("iface") or "no card", w.get("backend") or "no manager",
                                                       {True: "on", False: "OFF", None: "?"}[w.get("radio")]))
    lines.append("  tunnel       %s" % s.get("tunnel"))
    lines.append("  driver step  %s" % s.get("driver_step"))
    if s.get("held"):
        lines.append("  held         " + s["held"])
    if s.get("guard_tripped"):
        lines.append("  note         the boot-loop guard tripped this boot: no reboot")
    if s.get("last_reboot_wall"):
        lines.append("  last reboot  %s ago (asked by netwatch; %d since it was last online)" % (
            ago(s["last_reboot_wall"], now), s.get("reboots_since_online") or 0))
    for h in (st.get("history") or [])[-5:]:
        lines.append("  %s  %-14s %s  %s" % (time.strftime("%m-%d %H:%M", time.localtime(h.get("at", 0))), h.get("do"),
                                            "ok" if h.get("ok") else "FAILED", h.get("detail", "")))
    return "\n".join(lines)
