#!/usr/bin/env python3
"""Writes the PDF fixtures for the gauntlet's PDF checks (NOTES 6b546), by
hand, with the standard library only: nothing is downloaded and no PDF
library is needed. The words are invented and neutral (a town library's
notices). Deterministic: the same bytes on every run (no clock, no
randomness, a fixed file ID).

  tests/fixtures/pdf/text.pdf      3 pages of text, Latin accents (WinAnsi)
                                   page 1 says "PDF-MARK-ALPHA", page 3 "PDF-MARK-OMEGA"
  tests/fixtures/pdf/scanned.pdf   4 pages: text, a picture with no text
                                   (scanned), a blank page, text
  tests/fixtures/pdf/locked.pdf    2 pages behind a user password (RC4, the
                                   standard security handler, revision 2)
  tests/fixtures/pdf/owner.pdf     1 page encrypted with an owner password only
                                   (opens; says "OWNER-MARK")
  tests/fixtures/pdf/unicode.pdf   1 page: Hebrew and Arabic (right to left),
                                   Greek, Japanese, an "fi" ligature and Arabic
                                   presentation forms, through a ToUnicode map

big(n) builds an n-page PDF in memory (the reading-time and size checks
make theirs at run time; it isn't written to the repo).

Run from the repo root:  python3 tests/fixtures/make_pdf_samples.py
"""
import hashlib
import os
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf")
FILE_ID = hashlib.md5(b"concorde pdf fixture 6b546").digest()


def _esc(b: bytes) -> bytes:
    """A PDF literal string's body: ( ) \\ escaped, bytes over 126 in octal."""
    out = bytearray()
    for c in b:
        if c in b"()\\":
            out += b"\\" + bytes([c])
        elif c < 32 or c > 126:
            out += b"\\%03o" % c
        else:
            out.append(c)
    return bytes(out)


def _text_stream(lines, font=b"F1", enc="cp1252", size=12) -> bytes:
    body = [b"BT /%s %d Tf 72 720 Td %d TL" % (font, size, size + 4)]
    for ln in lines:
        raw = ln if isinstance(ln, bytes) else ln.encode(enc)
        body.append(b"(" + _esc(raw) + b") Tj T*")
    body.append(b"ET")
    return b"\n".join(body)


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for c in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(c ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


class _Enc:
    """The standard security handler, revision 2 (40-bit RC4): enough for a
    reader to see a user password, which is all the lock check needs."""

    def __init__(self, user: bytes, owner: bytes, perms=-44):
        pu = (user + _PAD)[:32]
        po = (owner + _PAD)[:32]
        self.O = _rc4(hashlib.md5(po).digest()[:5], pu)
        self.P = perms
        self.key = hashlib.md5(pu + self.O + (perms & 0xFFFFFFFF).to_bytes(4, "little")
                               + FILE_ID).digest()[:5]
        self.U = _rc4(self.key, _PAD)

    def obj(self, num: int, data: bytes) -> bytes:
        k = hashlib.md5(self.key + num.to_bytes(3, "little") + b"\0\0").digest()[:10]
        return _rc4(k, data)


def build(pages, fonts=b"", extra=(), enc=None) -> bytes:
    """pages: [(content bytes, resources bytes)]; extra: more objects as
    (bytes) appended after the pages, numbered on from them."""
    objs = {}
    n_pages = len(pages)
    first_page = 3
    kids = []
    num = first_page
    for content, res in pages:
        kids.append(num)
        objs[num] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     b"/Resources " + res + b" /Contents %d 0 R >>" % (num + 1))
        objs[num + 1] = ("stream", content)
        num += 2
    for e in extra:
        objs[num] = e
        num += 1
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = (b"<< /Type /Pages /Count %d /Kids [" % n_pages
               + b" ".join(b"%d 0 R" % k for k in kids) + b"] >>")
    enc_num = None
    if enc:
        enc_num = num
        objs[enc_num] = getattr(enc, "dict_bytes", None) or (
            b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P %d /O <%s> /U <%s> >>"
            % (enc.P, enc.O.hex().encode(), enc.U.hex().encode()))
        num += 1
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for k in sorted(objs):
        v = objs[k]
        offs[k] = len(out)
        out += b"%d 0 obj\n" % k
        if isinstance(v, tuple):
            kind = v[0]
            data = v[1]
            head = b""
            if kind == "image":
                head, data = v[2], v[1]
            if enc and k != enc_num:
                data = enc.obj(k, data)
            out += (b"<< " + head + b"/Length %d >>\nstream\n" % len(data) + data
                    + b"\nendstream")
        else:
            out += v
        out += b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % num
    for k in range(1, num):
        out += b"%010d 00000 n \n" % offs[k]
    out += b"trailer\n<< /Size %d /Root 1 0 R " % num
    if enc:
        out += b"/Encrypt %d 0 R " % enc_num
    out += b"/ID [<%s> <%s>] >>\nstartxref\n%d\n%%%%EOF\n" % (
        FILE_ID.hex().encode(), FILE_ID.hex().encode(), xref)
    return bytes(out)


F1 = b"<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >> >> >>"


def text_pdf() -> bytes:
    p1 = ["Riverside Library notices", "", "PDF-MARK-ALPHA",
          "The reading room opens at nine on weekdays.",
          "Café corner: tea and coffee, no food near the shelves.",
          "Late returns pay nothing in the first week."]
    p2 = ["Page two: the study rooms", "",
          "Rooms can be booked a week ahead at the front desk.",
          "Each booking lasts two hours; extend it if nobody is waiting.",
          "Naïve questions are welcome at the help desk."]
    p3 = ["Page three: the garden", "",
          "The courtyard garden is open from May to September.",
          "PDF-MARK-OMEGA",
          "Grüße from the volunteers who look after it."]
    return build([(_text_stream(p), F1) for p in (p1, p2, p3)])


def _picture() -> bytes:
    """A 32 x 32 grey pattern, Flate-compressed: a page that is only a
    picture, as a scanner makes."""
    w = h = 32
    px = bytes((x * 7 + y * 13) % 200 for y in range(h) for x in range(w))
    return zlib.compress(px, 9)


def scanned_pdf() -> bytes:
    # the picture is object 11 (after 4 pages x 2 objects from 3)
    img_res = b"<< /XObject << /Im1 11 0 R >> >>"
    pages = [
        (_text_stream(["A page with words on it.", "SCAN-MARK-TEXT"]), F1),
        (b"q 400 0 0 400 106 196 cm /Im1 Do Q", img_res),
        (b"", b"<< >>"),
        (_text_stream(["The last page has words again.", "SCAN-MARK-END"]), F1),
    ]
    img = ("image", _picture(),
           b"/Type /XObject /Subtype /Image /Width 32 /Height 32 /ColorSpace /DeviceGray "
           b"/BitsPerComponent 8 /Filter /FlateDecode ")
    return build(pages, extra=[img])


def owner_pdf() -> bytes:
    """Encrypted with an owner password only (the user password empty): it
    opens and reads, as every reader opens it."""
    pages = [(_text_stream(["Opening hours, owner-protected.", "OWNER-MARK"]), F1)]
    return build(pages, enc=_Enc(b"", b"owner-6b546"))


def locked_pdf() -> bytes:
    pages = [(_text_stream(["Members' list, behind a password.", "LOCK-MARK"]), F1),
             (_text_stream(["Second page."]), F1)]
    return build(pages, enc=_Enc(b"fixture", b"owner-6b546"))


# right to left and other scripts, each character given a one-byte code
# through a ToUnicode map (the glyphs drawn are Helvetica's; what a reader
# extracts is the map's)
UNI_LINES = [
    "שלום עולם",                # Hebrew
    "مرحبا",                                      # Arabic
    "ﺍﻟﺒﺎﺏ",                                      # Arabic presentation forms
    "Καλημέρα",                    # Greek
    "こんにちは",                                      # Japanese
    "ﬁle and ﬂoor",                                              # the fi and fl ligatures
]


def _rtl(ln) -> bool:
    return any("\u0590" <= ch <= "\u08ff" or "\ufb1d" <= ch <= "\ufdff"
               or "\ufe70" <= ch <= "\ufeff" for ch in ln)


def unicode_pdf() -> bytes:
    chars = []
    for ln in UNI_LINES:
        for ch in ln:
            if ch != " " and ch not in chars:
                chars.append(ch)
    codes = [c for c in range(0x41, 0x7f) if c not in b"()\\"]
    assert len(chars) <= len(codes)
    cmap = {ch: codes[i] for i, ch in enumerate(chars)}
    # a right-to-left line is drawn in visual order, as PDF makers write
    # it (the last letter first); a reader gives it back in logical order
    lines = [bytes(0x20 if ch == " " else cmap[ch] for ch in (ln[::-1] if _rtl(ln) else ln))
             for ln in UNI_LINES]
    entries = [b"<20> <0020>"] + [b"<%02X> <%s>" % (cmap[ch], ch.encode("utf-16-be").hex().upper().encode())
                                  for ch in chars]
    tu = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
          b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
          b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
          b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
          + b"%d beginbfchar\n" % len(entries) + b"\n".join(entries)
          + b"\nendbfchar\nendcmap\nCMapName currentdict /CIDInit defineresource pop\nend\nend")
    res = (b"<< /Font << /F2 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
           b"/ToUnicode 5 0 R >> >> >>")
    return build([(_text_stream(lines, font=b"F2"), res)], extra=[("stream", tu)])


class _AesDeclared:
    """An /Encrypt dictionary that declares AES-128 (V 4, R 4, AESV2) with
    an empty user password, its content left as plain text: enough for a
    reader to see AES and stop (the stdlib has no AES). For the pypdf
    path's lock check only; built at run time, not written to the repo."""
    P = -44

    def __init__(self):
        ok = hashlib.md5((b"owner-aes" + _PAD)[:32]).digest()
        for _ in range(50):
            ok = hashlib.md5(ok).digest()
        o = _rc4(ok, _PAD)
        for i in range(1, 20):
            o = _rc4(bytes(b ^ i for b in ok), o)
        k = hashlib.md5(_PAD + o + (self.P & 0xFFFFFFFF).to_bytes(4, "little") + FILE_ID).digest()
        for _ in range(50):
            k = hashlib.md5(k[:16]).digest()
        u = _rc4(k, hashlib.md5(_PAD + FILE_ID).digest())
        for i in range(1, 20):
            u = _rc4(bytes(b ^ i for b in k), u)
        self.dict_bytes = (b"<< /Filter /Standard /V 4 /R 4 /Length 128 /CF << /StdCF << /AuthEvent /DocOpen "
                           b"/CFM /AESV2 /Length 16 >> >> /StmF /StdCF /StrF /StdCF /P %d /O <%s> /U <%s> >>"
                           % (self.P, o.hex().encode(), (u + b"\0" * 16).hex().encode()))

    def obj(self, num, data):
        return data


def aes_declared() -> bytes:
    return build([(_text_stream(["Behind AES."]), F1)], enc=_AesDeclared())


def big(n: int, words_per_page: int = 60) -> bytes:
    """n pages, each with its number and some neutral words (built at run time)."""
    filler = ("The shelves are sorted by subject and then by author. ") * (words_per_page // 10)
    pages = [(_text_stream(["Page %d of the long catalogue." % i, filler[:90], filler[90:180]]), F1)
             for i in range(1, n + 1)]
    return build(pages)


FIXTURES = {"text.pdf": text_pdf, "scanned.pdf": scanned_pdf,
            "locked.pdf": locked_pdf, "unicode.pdf": unicode_pdf, "owner.pdf": owner_pdf}

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, fn in FIXTURES.items():
        data = fn()
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data)
        print("  %s: %d bytes" % (name, len(data)))
