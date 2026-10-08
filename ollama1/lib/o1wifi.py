"""Wi-Fi backup (6b439): the server's Wi-Fi card as a spare connection and a second way to wake it.

Opt-in (setup.sh --wifi on). Everything here runs as root, from `ollama1-wifi`, from the suspend hook or from the
idle service's wake list. The one secret is the Wi-Fi password:

  - `set` asks for it at the terminal with no echo (getpass). It is never read from an argument or an environment
    variable, never printed, never logged, and never part of any command line: the only place it is written is the
    netplan file (mode 0600, root), created with that mode from the start.
  - every message that could quote a tool's output goes through redact() first.

The wired link is never touched: the Wi-Fi netplan file is its own (70-ollama1-wifi.yaml), its default route has a
higher metric than the wired one (so Wi-Fi only carries traffic while the cable's link is down), it is `optional`
(boot never waits for it), the new file is checked with `netplan generate` before `netplan apply`, and a failed apply
or a lost wired link puts the previous file back.

Each network card answers ARP only for its own addresses (6b450): Linux's default let the Wi-Fi card answer for the
wired bridge's address too, and a client whose ARP cache took that reply sent the server's traffic to a card in power
save (SSH timed out until the cache flipped back). setup.sh installs /etc/sysctl.d/61-ollama1-arp.conf
(net.ipv4.conf.all.arp_ignore=1, arp_announce=2) on every server; `status` says whether the kernel is set that way.
With the cable out the server is reached at this card's own address, never at the wired one.
"""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time

from o1common import p
import o1idle

WIFI_METRIC = 600                  # the wired bridge's DHCP route is 100: a larger metric loses, so Wi-Fi is only the fallback
NETPLAN_NAME = "70-ollama1-wifi.yaml"
LINK_NAME = "50-ollama1-wifi.link"
UDEV_NAME = "70-ollama1-wifi.rules"
ARP_CONF = "61-ollama1-arp.conf"   # /etc/sysctl.d: each card answers ARP only for its own addresses (6b450)
PHY_RX = re.compile(r"^phy[0-9]{1,3}$")
IFACE_RX = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
PACKAGES = (("iw", "iw"), ("wpa_supplicant", "wpasupplicant"))


# ---- where things live ----------------------------------------------------------------

def netplan_path():
    return p("/etc/netplan/" + NETPLAN_NAME)


def state_path():
    return p("/etc/ollama1/wifi.json")


def link_path():
    return p("/etc/systemd/network/" + LINK_NAME)


def udev_path():
    return p("/etc/udev/rules.d/" + UDEV_NAME)


def tool_link():
    return p("/usr/local/sbin/ollama1-wifi")


def read_state():
    try:
        with open(state_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(d):
    write_file(state_path(), json.dumps(d, sort_keys=True) + "\n", 0o600)


def enabled(st=None):
    return (read_state() if st is None else st).get("enabled") is True


def configured():
    return os.path.exists(netplan_path())


# ---- small helpers --------------------------------------------------------------------

def write_file(path, text, mode):
    """Atomic, and created with `mode` from the first byte (no moment when a secret file is readable)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    try:
        os.unlink(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remove_file(path):
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def run(argv, timeout=30):
    """A command with no stdin and a bounded time: the CompletedProcess, or None when it could not run."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def ok(r):
    return r is not None and r.returncode == 0


def redact(text, secrets=()):
    """What a tool printed, with every secret and the SSID blanked out, and kept short."""
    t = text or ""
    for s in secrets:
        if s:
            t = t.replace(s, "***")
    return " ".join(t.split())[:300]


def sysnet():
    return o1idle._sysnet()


def proc_sys():
    return os.path.join(os.environ.get("OLLAMA1_PROC", "/proc"), "sys")


def arp_setting(key, iface=None, proc=None):
    """The kernel's effective net.ipv4.conf.<iface>.<key>: the larger of `all` and the card's own, as the kernel
    takes it for arp_ignore and arp_announce; 0 for what cannot be read."""
    root = os.path.join(proc or proc_sys(), "net", "ipv4", "conf")
    best = 0
    for scope in ("all",) + ((iface,) if iface else ()):
        try:
            best = max(best, int(o1idle._first_line(os.path.join(root, scope, key)) or 0))
        except ValueError:
            pass
    return best


def arp_own_only(iface=None, proc=None):
    """Whether the card answers ARP only for its own addresses and asks with its own (6b450): arp_ignore 1 (or 2,
    stricter) and arp_announce 2, as /etc/sysctl.d/61-ollama1-arp.conf sets for every card. 8 never answers at all."""
    return arp_setting("arp_ignore", iface, proc) in (1, 2) and arp_setting("arp_announce", iface, proc) == 2


# ---- the card ---------------------------------------------------------------------------

def wlan_devices(root=None):
    """The wireless cards: [{name, mac, phy, assign, wakeup}] (a device behind it, and a phy80211 or wireless dir)."""
    root = root or sysnet()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for n in names:
        d = os.path.join(root, n)
        if not IFACE_RX.match(n) or not os.path.exists(os.path.join(d, "device")):
            continue
        if not (os.path.exists(os.path.join(d, "wireless")) or os.path.exists(os.path.join(d, "phy80211"))):
            continue
        out.append({
            "name": n,
            "mac": o1idle._first_line(os.path.join(d, "address")).lower(),
            "phy": o1idle._first_line(os.path.join(d, "phy80211", "name")),
            "assign": o1idle._first_line(os.path.join(d, "addr_assign_type")),
            "wakeup": os.path.join(d, "device", "power", "wakeup"),
        })
    return out


def permanent_mac(dev):
    """The card's own address, never a random one: the kernel's addr_assign_type is 0 for a permanent address (where
    it says nothing, a locally administered address, bit 2 of the first byte, is taken for a random one)."""
    mac = dev.get("mac", "")
    if not o1idle.MAC_RX.match(mac) or mac == "00:00:00:00:00:00":
        return False
    if dev.get("assign", "") != "":
        return dev["assign"] == "0"
    return not int(mac[:2], 16) & 2


def supports_magic(text):
    return bool(re.search(r"wake up on magic packet", text or ""))


def set_wakeup(dev):
    """power/wakeup of the card's PCI device -> enabled (the kernel resets it on some resumes)."""
    try:
        with open(dev["wakeup"], "w") as f:
            f.write("enabled")
        return True
    except OSError:
        return False


def wake_macs(root=None, run_=run):
    """The Wi-Fi card's MAC for the wake list: only with the feature on, a network configured, a real
    (permanent) address and WoWLAN magic-packet support in `iw phy <phy> info`."""
    if not enabled() or not configured():
        return []
    out = []
    for dev in wlan_devices(root):
        if not permanent_mac(dev) or not PHY_RX.match(dev["phy"]):
            continue
        r = run_(["iw", "phy", dev["phy"], "info"], 10)
        if ok(r) and supports_magic(r.stdout) and dev["mac"] not in out:
            out.append(dev["mac"])
    return out


def wowlan(action, root=None, run_=run, log=print):
    """The sleep hook's call: enable (before suspend) or disable (after wake) wake on a magic packet or a
    disconnect. Does nothing, quietly, when the feature is off, nothing is configured or there is no card.
    Never fails: a suspend must not wait on, or be stopped by, this."""
    try:
        if not enabled() or not configured():
            return 0
        for dev in wlan_devices(root):
            if not PHY_RX.match(dev["phy"]):
                continue
            if action == "enable":
                set_wakeup(dev)
                r = run_(["iw", "phy", dev["phy"], "wowlan", "enable", "magic-packet", "disconnect"], 10)
                log("wifi wake: %s" % ("on" if ok(r) else "not turned on"))
            else:
                run_(["iw", "phy", dev["phy"], "wowlan", "disable"], 10)
                run_(["networkctl", "reconfigure", dev["name"]], 15)
                log("wifi wake: off")
    except Exception as e:                        # a hook never stops a suspend
        log("wifi wake: %s" % type(e).__name__)
    return 0


# ---- the files setup writes ------------------------------------------------------------

def link_text():
    """The .link file: the first .link that matches a card wins, so it says what 99-default.link says (names kept)
    and keeps the card's own MAC (persistent: nothing is changed when the card has a permanent address)."""
    return ("# ollama1: Wi-Fi backup. The card keeps its own MAC (the wake list publishes it).\n[Match]\nType=wlan\n\n[Link]\n"
            "NamePolicy=keep kernel database onboard slot path\nMACAddressPolicy=persistent\n")


def udev_text():
    return ('# ollama1: Wi-Fi backup. A network card (PCI class 0x0280) may wake the machine.\n'
            'ACTION=="add", SUBSYSTEM=="pci", ATTR{class}=="0x0280??", ATTR{power/wakeup}="enabled"\n')


def missing_packages(which=shutil.which):
    return [pkg for exe, pkg in PACKAGES if not which(exe)]


def run_apt_install(packages):
    try:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        return subprocess.run(["apt-get", "-y", "-q", "install"] + list(packages), env=env, timeout=1800,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def setup(choice, root=None, which=shutil.which, apt=run_apt_install, run_=run, log=print, tool=None):
    """setup.sh's step. on: the packages, the tool's link, the .link file, the udev rule, the state file, wakeup on
    now. It configures no network and touches no netplan file. off: removes what on installed and the netplan file
    `set` wrote (applying it), and leaves everything else."""
    if choice == "on":
        need = missing_packages(which)
        if need and not apt(need):
            log("Wi-Fi backup: apt could not install %s; left as it was" % " ".join(need))
            return 1
        tool = tool or os.path.realpath(sys.argv[0])
        os.makedirs(os.path.dirname(tool_link()), exist_ok=True)
        try:
            if os.path.lexists(tool_link()):
                os.unlink(tool_link())
            os.symlink(tool, tool_link())
        except OSError:
            log("Wi-Fi backup: could not link ollama1-wifi into /usr/local/sbin")
        st = read_state()
        st["enabled"] = True
        write_state(st)
        write_file(link_path(), link_text(), 0o644)
        write_file(udev_path(), udev_text(), 0o644)
        run_(["udevadm", "control", "--reload"], 20)
        for dev in wlan_devices(root):
            set_wakeup(dev)
        cards = wlan_devices(root)
        if not cards:
            log("Wi-Fi backup: on, but no Wi-Fi card was found")
        elif configured():
            log("Wi-Fi backup: on (a network is already set; sudo ollama1-wifi status)")
        else:
            log("Wi-Fi backup: on. Give it the network: sudo ollama1-wifi set")
        return 0
    if enabled() or configured() or os.path.exists(link_path()) or os.path.exists(udev_path()):
        wowlan("disable", root, run_, log=lambda *_: None)
        had = configured()
        remove_file(netplan_path())
        if had:
            apply_netplan(run_, root, None, lambda s: None)
        remove_file(link_path())
        if remove_file(udev_path()):
            run_(["udevadm", "control", "--reload"], 20)
        remove_file(state_path())
        if os.path.islink(tool_link()):
            remove_file(tool_link())
    log("Wi-Fi backup: off")
    return 0


# ---- the network ------------------------------------------------------------------------

def valid_ssid(s):
    b = s.encode("utf-8", "replace")
    return 1 <= len(b) <= 32 and not any(ord(c) < 32 or ord(c) == 127 for c in s)


def valid_passphrase(s):
    return 8 <= len(s) <= 63 and all(32 <= ord(c) <= 126 for c in s)


def country_for_timezone(tz, tab=None):
    """The two-letter country of an Area/City zone from zone1970.tab, only when exactly one country owns it."""
    tab = tab or p("/usr/share/zoneinfo/zone1970.tab")
    try:
        with open(tab) as f:
            for line in f:
                if line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 3 and cols[2] == tz:
                    codes = cols[0].split(",")
                    return codes[0] if len(codes) == 1 and re.fullmatch(r"[A-Z]{2}", codes[0]) else None
    except OSError:
        pass
    return None


def machine_timezone():
    try:
        with open(p("/etc/timezone")) as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    try:
        t = os.readlink(p("/etc/localtime"))
        return t.split("zoneinfo/", 1)[1] if "zoneinfo/" in t else None
    except (OSError, IndexError):
        return None


def q(s):
    """A YAML double-quoted scalar (JSON strings are valid ones)."""
    return json.dumps(s)


def netplan_text(iface, ssid, password, country=None):
    lines = ["# ollama1: Wi-Fi backup (written by ollama1-wifi set). Holds the Wi-Fi password: root only.",
             "network:", "  version: 2", "  renderer: networkd", "  wifis:", "    %s:" % iface,
             "      dhcp4: true", "      dhcp4-overrides:", "        route-metric: %d" % WIFI_METRIC,
             "      optional: true"]
    if country:
        lines.append("      regulatory-domain: %s" % q(country))
    lines += ["      access-points:", "        %s:" % q(ssid), "          password: %s" % q(password)]
    return "\n".join(lines) + "\n"


def wired_up(root=None):
    """The wired cards (a bridge's members too) whose cable is plugged in right now."""
    root = root or sysnet()
    return {n for n, _ in o1idle.physical_nics(root)
            if o1idle._first_line(os.path.join(root, n, "carrier")) == "1"}


def apply_netplan(run_, root, secrets, sleep, settle=None):
    """generate (the check), then apply, then make sure no wired cable went down. Returns an error text, or None."""
    secrets = secrets or ()
    g = run_(["netplan", "generate"], 60)
    if not ok(g):
        return "netplan refused it: " + redact((g.stderr or g.stdout) if g else "netplan did not run", secrets)
    before = wired_up(root)
    a = run_(["netplan", "apply"], 120)
    if not ok(a):
        return "netplan apply failed: " + redact((a.stderr or a.stdout) if a else "netplan did not run", secrets)
    sleep(float(os.environ.get("OLLAMA1_WIFI_SETTLE", "3")) if settle is None else settle)
    lost = sorted(before - wired_up(root))
    if lost:
        return "applying it took the wired link down (%s)" % ", ".join(lost)
    return None


def install_netplan(text, root=None, run_=run, sleep=time.sleep, secrets=(), settle=None):
    """Put the netplan file in place (None removes it) and apply it, or put the previous state back."""
    path = netplan_path()
    try:
        with open(path, "rb") as f:
            old = f.read()
        old_mode = os.stat(path).st_mode & 0o777
    except OSError:
        old = old_mode = None
    if text is None:
        remove_file(path)
    else:
        write_file(path, text, 0o600)
    err = apply_netplan(run_, root, secrets, sleep, settle)
    if err:
        if old is None:
            remove_file(path)
        else:
            write_file(path, old, old_mode)
        undo = apply_netplan(run_, root, secrets, sleep, 0)       # best effort: the network as it was
        err += "; the previous setting is back" + ("" if undo is None else " (re-applying it said: %s)" % undo)
    return err


def cmd_set(args, root=None, run_=run, ask=input, ask_secret=getpass.getpass, sleep=time.sleep, out=print):
    """Asks for the network name and the password, writes and applies the netplan file."""
    if args:
        out("ollama1-wifi set takes no arguments: it asks for the name and the password (a password given as an "
            "argument would sit in the process list and the shell history)")
        return 2
    if not enabled():
        out("Wi-Fi backup is off. Turn it on first: sudo ./setup.sh --wifi on")
        return 1
    cards = wlan_devices(root)
    if not cards:
        out("No Wi-Fi card found on this server.")
        return 1
    iface = cards[0]["name"]
    try:
        ssid = ask("Wi-Fi network name (SSID): ").strip("\r\n")
        pw = ask_secret("Wi-Fi password (not shown): ")
        again = ask_secret("Again: ")
    except (EOFError, KeyboardInterrupt):
        out("\nCancelled; nothing changed.")
        return 1
    if not valid_ssid(ssid):
        out("The network name must be 1 to 32 bytes, with no control characters. Nothing changed.")
        return 1
    if not valid_passphrase(pw):
        out("The password must be 8 to 63 characters (a WPA2/WPA3 passphrase; letters, digits and symbols, no accents). "
            "Nothing changed.")
        return 1
    if pw != again:
        out("The two passwords differ. Nothing changed.")
        return 1
    tz = machine_timezone()
    text = netplan_text(iface, ssid, pw, country_for_timezone(tz) if tz else None)
    err = install_netplan(text, root, run_, sleep, secrets=(pw, ssid))
    if err:
        out("Not set: " + err)
        return 1
    st = read_state()
    st["ssid"] = ssid
    st["iface"] = iface
    write_state(st)
    out("Saved. %s will connect to the network now; it carries traffic only when the cable's link is down. "
        "Check it with: sudo ollama1-wifi status" % iface)
    return 0


def cmd_remove(root=None, run_=run, sleep=time.sleep, out=print):
    if not configured():
        out("No Wi-Fi network is set.")
        return 0
    err = install_netplan(None, root, run_, sleep)
    if err:
        out("Not removed: " + err)
        return 1
    st = read_state()
    st.pop("ssid", None)
    st.pop("iface", None)
    if st:
        write_state(st)
    out("The Wi-Fi network is removed.")
    return 0


# ---- status -----------------------------------------------------------------------------

def default_route(text):
    """(device, metric) of the default route that wins (the lowest metric) in `ip -o route show default`."""
    best = None
    for line in (text or "").splitlines():
        m = re.search(r"\bdev (\S+)", line)
        if not m:
            continue
        mm = re.search(r"\bmetric (\d+)", line)
        metric = int(mm.group(1)) if mm else 0
        if best is None or metric < best[1]:
            best = (m.group(1), metric)
    return best


def parse_link(text):
    """(ssid, signal dBm) from `iw dev <if> link`, or None when it isn't connected."""
    if not text or "Not connected" in text or "Connected to" not in text:
        return None
    s = re.search(r"^\s*SSID: (.*)$", text, re.M)
    g = re.search(r"^\s*signal: (-?\d+) dBm", text, re.M)
    return (s.group(1) if s else "", int(g.group(1)) if g else None)


def status_lines(root=None, run_=run):
    root = root or sysnet()
    st = read_state()
    out = ["Wi-Fi backup: " + ("on" if enabled(st) else "off (sudo ./setup.sh --wifi on)")]
    cards = wlan_devices(root)
    if not cards:
        out.append("Wi-Fi card: none found")
    for dev in cards:
        out.append("Wi-Fi card: %s, MAC %s%s" % (dev["name"], dev["mac"], "" if permanent_mac(dev) else " (not the card's own address)"))
    if configured():
        out.append("Network: set%s" % (' ("%s")' % st["ssid"] if st.get("ssid") else ""))
    else:
        out.append("Network: not set (sudo ollama1-wifi set)")
    for dev in cards[:1]:
        r = run_(["iw", "dev", dev["name"], "link"], 10)
        link = parse_link(r.stdout) if ok(r) else None
        out.append("Link: " + ('connected to "%s", signal %s dBm' % (link[0], link[1] if link[1] is not None else "?")
                               if link else "not connected"))
        r = run_(["ip", "-4", "-o", "addr", "show", "dev", dev["name"]], 10)
        m = re.search(r"inet (\S+)", r.stdout) if ok(r) else None
        out.append("Address: " + (m.group(1) if m else "none"))
    wired = [(n, o1idle._first_line(os.path.join(root, n, "carrier")) == "1") for n, _ in o1idle.physical_nics(root)]
    out.append("Wired: " + (", ".join("%s %s" % (n, "up" if up else "down") for n, up in wired) or "no wired card found"))
    r = run_(["ip", "-o", "route", "show", "default"], 10)
    route = default_route(r.stdout) if ok(r) else None
    out.append("Default route: " + ("%s (metric %d)" % route if route else "none"))
    for dev in cards[:1]:
        # 6b450: with Linux's default the Wi-Fi card answered for the wired address too (SSH to it timed out)
        out.append("Answers ARP only for its own address: " + (
            "yes" if arp_own_only(dev["name"]) else "no (sudo ./setup.sh installs /etc/sysctl.d/%s)" % ARP_CONF))
    if cards:
        dev = cards[0]
        r = run_(["iw", "phy", dev["phy"], "info"], 10) if PHY_RX.match(dev["phy"]) else None
        magic = ok(r) and supports_magic(r.stdout)
        out.append("Card can wake on a magic packet (WoWLAN): " + ("yes" if magic else "no"))
        out.append("PCI wake enabled: " + ("yes" if o1idle._first_line(dev["wakeup"]) == "enabled" else "no"))
    out.append("Published to the app as a wake card: " + (", ".join(wake_macs(root, run_)) or "no"))
    return out


# ---- the command line -------------------------------------------------------------------

def main(argv, out=print):
    cmd = argv[1] if len(argv) > 1 else "status"
    rest = argv[2:]
    if cmd == "set":
        return cmd_set(rest)
    if cmd == "remove":
        return cmd_remove()
    if cmd == "status":
        for line in status_lines():
            out(line)
        return 0
    if cmd == "wowlan" and rest in (["enable"], ["disable"]):
        return wowlan(rest[0], log=lambda s: print(s, flush=True))
    if cmd == "setup" and rest in (["on"], ["off"]):
        return setup(rest[0])
    sys.stderr.write("usage: ollama1-wifi set | status | remove\n")
    return 2
