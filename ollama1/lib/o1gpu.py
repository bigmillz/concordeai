"""Which GPU this server has, for the app: vendor, a plain card name and
its VRAM. Nothing else about the hardware is reported.

  {"vendor": "amd" | "nvidia" | "intel" | None, "name": str | None, "vram_bytes": int | None}

Sources, in order: amdgpu sysfs (VRAM) with the PCI ids for the name,
nvidia-smi, Intel i915/xe sysfs. Detected once, when the gateway starts.
"""
import glob
import os
import re
import subprocess

VENDORS = {"0x1002": "amd", "0x10de": "nvidia", "0x8086": "intel"}

# Names the PCI ids can't tell apart (same device id, different revision).
AMD_BY_REVISION = {
    ("0x73bf", "0xc0"): "Radeon RX 6900 XT",
    ("0x73bf", "0xc1"): "Radeon RX 6800 XT",
    ("0x73bf", "0xc3"): "Radeon RX 6800",
    ("0x73af", "0xc0"): "Radeon RX 6900 XT",
    ("0x73a5", "0xc0"): "Radeon RX 6950 XT",
    ("0x744c", "0xc8"): "Radeon RX 7900 XTX",
    ("0x744c", "0xcc"): "Radeon RX 7900 XT",
    ("0x744c", "0xce"): "Radeon RX 7900 GRE",
}


def _sys():
    return os.environ.get("OLLAMA1_SYS", "/sys")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _pci_ids_path():
    for p in (os.environ.get("OLLAMA1_PCI_IDS"), "/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids"):
        if p and os.path.exists(p):
            return p
    return None


def pci_name(vendor, device, subvendor=None, subdevice=None, path=None):
    """The name from pci.ids: the subsystem's if listed, else the device's
    (the part in brackets, if any, which is the marketing name)."""
    path = path or _pci_ids_path()
    if not path:
        return None
    v, d = vendor.replace("0x", "").lower(), device.replace("0x", "").lower()
    sv = (subvendor or "").replace("0x", "").lower()
    sd = (subdevice or "").replace("0x", "").lower()
    in_vendor = in_device = False
    dev_name = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    if in_vendor:
                        break
                    in_vendor = line.split()[0].lower() == v
                    continue
                if not in_vendor:
                    continue
                if not line.startswith("\t\t"):
                    if in_device:
                        break
                    parts = line.strip().split(None, 1)
                    in_device = parts[0].lower() == d
                    if in_device:
                        dev_name = parts[1] if len(parts) > 1 else None
                    continue
                if in_device and sv and sd:
                    parts = line.strip().split(None, 2)
                    if len(parts) == 3 and parts[0].lower() == sv and parts[1].lower() == sd:
                        return tidy(parts[2])
    except OSError:
        return None
    if dev_name:
        m = re.search(r"\[([^\]]+)\]", dev_name)
        return tidy(m.group(1) if m else dev_name)
    return None


def tidy(name):
    name = re.sub(r"^(AMD|ATI|NVIDIA|Intel(\(R\))?)\s+", "", name.strip())
    name = re.sub(r"\s+", " ", name)
    return name[:60] or None


def _amd(dev):
    vram = _read(dev + "/mem_info_vram_total")
    name = _read(dev + "/product_name")
    if not name:
        key = ((_read(dev + "/device") or "").lower(), (_read(dev + "/revision") or "").lower())
        name = AMD_BY_REVISION.get(key)
    if not name:
        name = pci_name("0x1002", _read(dev + "/device") or "", _read(dev + "/subsystem_vendor"),
                        _read(dev + "/subsystem_device"))
    return {"vendor": "amd", "name": tidy(name) if name else None,
            "vram_bytes": int(vram) if vram and vram.isdigit() else None}


def _nvidia():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    first = r.stdout.strip().splitlines()[0]
    name, _, mib = first.rpartition(",")
    try:
        vram = int(float(mib.strip())) << 20
    except ValueError:
        vram = None
    return {"vendor": "nvidia", "name": tidy(name) if name.strip() else None, "vram_bytes": vram}


def _intel(dev):
    vram = None
    for p in glob.glob(dev + "/tile*/vram*/io_size") + glob.glob(dev + "/lmem_total_bytes"):
        v = _read(p)
        if v and v.isdigit():
            vram = int(v)
            break
    name = pci_name("0x8086", _read(dev + "/device") or "", _read(dev + "/subsystem_vendor"),
                    _read(dev + "/subsystem_device"))
    return {"vendor": "intel", "name": name, "vram_bytes": vram}


def detect():
    """The first discrete-looking GPU: AMD (amdgpu), NVIDIA, then Intel."""
    cards = sorted(glob.glob(_sys() + "/class/drm/card[0-9]*/device"))
    seen = {}
    for dev in cards:
        vendor = VENDORS.get((_read(dev + "/vendor") or "").lower())
        if vendor and vendor not in seen:
            seen[vendor] = dev
    if "amd" in seen and os.path.exists(seen["amd"] + "/mem_info_vram_total"):
        return _amd(seen["amd"])
    nv = _nvidia()
    if nv:
        return nv
    if "intel" in seen:
        return _intel(seen["intel"])
    if "amd" in seen:
        return _amd(seen["amd"])
    return {"vendor": None, "name": None, "vram_bytes": None}


def _pct(v):
    """A whole number 0-100, or None."""
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 100 else None


def _bytes(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v < 1 << 50 else None


def usage(vendor):
    """How busy the card is right now, for the app's meter. Exactly three
    numbers, each None when it can't be read (a card with no such file, an
    older driver, nvidia-smi missing): never an error, never a guess.

      {"busy_pct": int | None, "vram_used_bytes": int | None, "vram_total_bytes": int | None}

    amdgpu: gpu_busy_percent, mem_info_vram_used and mem_info_vram_total,
    the same files the server's own dashboard reads. NVIDIA: nvidia-smi.
    Intel and no card: all None."""
    out = {"busy_pct": None, "vram_used_bytes": None, "vram_total_bytes": None}
    try:
        if vendor == "amd":
            for dev in sorted(glob.glob(_sys() + "/class/drm/card[0-9]*/device")):
                if (_read(dev + "/vendor") or "").lower() == "0x1002" \
                        and os.path.exists(dev + "/mem_info_vram_total"):
                    out["busy_pct"] = _pct(_read(dev + "/gpu_busy_percent"))
                    out["vram_used_bytes"] = _bytes(_read(dev + "/mem_info_vram_used"))
                    out["vram_total_bytes"] = _bytes(_read(dev + "/mem_info_vram_total"))
                    break
        elif vendor == "nvidia":
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                parts = [p.strip() for p in r.stdout.strip().splitlines()[0].split(",")]
                if len(parts) == 3:
                    out["busy_pct"] = _pct(parts[0])
                    for key, p in (("vram_used_bytes", parts[1]), ("vram_total_bytes", parts[2])):
                        try:
                            out[key] = _bytes(int(float(p)) << 20)
                        except ValueError:
                            pass
    except (OSError, subprocess.SubprocessError):
        pass
    return out
