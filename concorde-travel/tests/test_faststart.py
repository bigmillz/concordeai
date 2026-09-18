#!/usr/bin/env python3
"""Guard for the one piece of byte-level code in ConcordeGo.

server.py rewrites a QuickTime file so `moov` comes before `mdat`, which
is what makes Apple's aerials playable in a browser at all. Getting it
subtly wrong produces a file that still *opens* and then plays garbage or
nothing — the failure is silent, which is exactly the kind millenai.py's
notes warn about. So: build a synthetic .mov by hand, remux it, and
assert the chunk offsets still land on the same payload bytes.

    python3 concorde-travel/tests/test_faststart.py

Stdlib only. No server, no network, no fixtures.
"""

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from server import _atoms, _faststart          # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (("  — " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def atom(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def build(chunks, moov_first=False):
    """A minimal but structurally real .mov: ftyp, mdat carrying `chunks`,
    and a moov whose stco points at each chunk's absolute offset."""
    ftyp = atom(b"ftyp", b"qt  " + struct.pack(">I", 512))
    mdat_payload = b"".join(chunks)
    mdat = atom(b"mdat", mdat_payload)

    def assemble(mdat_off):
        offs, cur = [], mdat_off + 8
        for c in chunks:
            offs.append(cur)
            cur += len(c)
        stco = atom(b"stco", b"\0\0\0\0" + struct.pack(">I", len(offs))
                    + b"".join(struct.pack(">I", o) for o in offs))
        return atom(b"moov", atom(b"trak", atom(b"mdia", atom(b"minf", atom(b"stbl", stco)))))

    if moov_first:
        # moov sits between ftyp and mdat, so mdat's offset depends on
        # moov's length — which depends on nothing, so one pass is enough
        probe = assemble(0)
        moov = assemble(len(ftyp) + len(probe))
        return ftyp + moov + mdat, mdat_payload
    moov = assemble(len(ftyp))
    return ftyp + mdat + moov, mdat_payload


def read_stco(path):
    """Walk the output and pull the chunk offsets back out."""
    data = open(path, "rb").read()
    i = data.find(b"stco")
    if i < 0:
        return None, data
    n = struct.unpack(">I", data[i + 8:i + 12])[0]
    base = i + 12
    return [struct.unpack(">I", data[base + 4 * k:base + 4 * k + 4])[0] for k in range(n)], data


def order(path):
    with open(path, "rb") as fh:
        return [t.decode("latin1") for t, _, _ in _atoms(fh, os.path.getsize(path))]


def main():
    chunks = [b"CHUNK-AAAA", b"CHUNK-BBBBBB", b"CHUNK-C"]

    # ---- 1. the real case: moov trails mdat and must be moved ----------
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.mov"), os.path.join(d, "out.mov")
        blob, _ = build(chunks)
        open(src, "wb").write(blob)
        before = order(src)
        check("input really is moov-last", before.index("mdat") < before.index("moov"),
              "got " + str(before))

        _faststart(src, dst)

        after = order(dst)
        check("output is ftyp, moov, mdat", after[:3] == ["ftyp", "moov", "mdat"], "got " + str(after))
        check("source removed after remux", not os.path.exists(src))
        check("no .part left behind", not os.path.exists(dst + ".part"))

        offs, data = read_stco(dst)
        check("stco survived the rewrite", offs is not None and len(offs) == len(chunks))
        if offs:
            for k, c in enumerate(chunks):
                landed = data[offs[k]:offs[k] + len(c)]
                check("chunk %d offset still lands on its payload" % k, landed == c,
                      "offset %d gave %r, wanted %r" % (offs[k], landed[:14], c))
        check("byte count preserved", len(data) == len(blob),
              "%d vs %d" % (len(data), len(blob)))

    # ---- 2. already fast-start: must pass through untouched ------------
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.mov"), os.path.join(d, "out.mov")
        blob, _ = build(chunks, moov_first=True)
        open(src, "wb").write(blob)
        _faststart(src, dst)
        check("already-faststart file is passed through byte-identical",
              open(dst, "rb").read() == blob)
        offs, data = read_stco(dst)
        if offs:
            ok = all(data[offs[k]:offs[k] + len(chunks[k])] == chunks[k] for k in range(len(chunks)))
            check("untouched file's offsets still resolve", ok)

    # ---- 3. a 64-bit co64 table shifts too -----------------------------
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.mov"), os.path.join(d, "out.mov")
        ftyp = atom(b"ftyp", b"qt  " + struct.pack(">I", 512))
        mdat = atom(b"mdat", b"".join(chunks))
        off0 = len(ftyp) + 8
        co64 = atom(b"co64", b"\0\0\0\0" + struct.pack(">I", 1) + struct.pack(">Q", off0))
        moov = atom(b"moov", atom(b"trak", atom(b"mdia", atom(b"minf", atom(b"stbl", co64)))))
        open(src, "wb").write(ftyp + mdat + moov)
        _faststart(src, dst)
        data = open(dst, "rb").read()
        i = data.find(b"co64")
        v = struct.unpack(">Q", data[i + 12:i + 20])[0]
        check("co64 offset shifted by len(moov)", v == off0 + len(moov),
              "got %d, wanted %d" % (v, off0 + len(moov)))
        check("co64 offset lands on its payload", data[v:v + len(chunks[0])] == chunks[0])

    print()
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        return 1
    print("all faststart checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
