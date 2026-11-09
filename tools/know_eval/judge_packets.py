#!/usr/bin/env python3
"""Judge packets for the "It knows your life" answer eval (6b530).

run_eval.py's blind packets hold the question, the reference and the two
answers. A judge also needs the person's records to tell a grounded claim
from an invented one, so this adds them: the expected document(s) and every
document the "with" answer was given, whole, the same for A and B (which
answer saw them is not said). The key stays behind: the packets land in a
folder of their own, without key.json.

Usage:
  judge_packets.py --run runs/<name> --corpus <make_corpus.py's folder> --dest <dir>

Stdlib only. Nothing here is anyone's real data.
"""
import argparse
import html
import json
import os
import re
import zipfile
import zlib


def pdf_text(p):
    raw = open(p, "rb").read()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        s = m.group(1)
        try:
            s = zlib.decompress(s)
        except Exception:
            pass
        for t in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", s):
            out.append(t.decode("latin-1").replace("\\(", "(").replace("\\)", ")"))
    return "\n".join(out) or "(no text layer: a scanned PDF)"


def file_text(p):
    ext = os.path.splitext(p)[1].lower()
    if ext == ".docx":
        with zipfile.ZipFile(p) as z:
            x = z.read("word/document.xml").decode("utf-8", "replace")
        return "\n".join(html.unescape(re.sub(r"<[^>]+>", "", para))
                         for para in re.findall(r"<w:p[ >].*?</w:p>", x, re.S))
    if ext == ".pdf":
        return pdf_text(p)
    t = open(p, encoding="utf-8", errors="replace").read()
    if ext in (".html", ".htm"):
        t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", t)
        t = re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))
    return t.strip()


def chat_text(c):
    lines = ["Chat: %s" % c.get("title", "")]
    for m in c.get("messages", []):
        who = "You" if m.get("role") == "user" else "Assistant"
        lines.append("%s: %s" % (who, re.sub("\0[^\0]*\0", "", str(m.get("content", "")))))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--dest", required=True)
    a = ap.parse_args()
    corp = os.path.realpath(a.corpus)
    chats = {c["id"]: c for c in json.load(open(os.path.join(corp, "chats.json")))}
    key = json.load(open(os.path.join(a.run, "packets", "key.json")))
    os.makedirs(a.dest, exist_ok=True)
    for n, k in sorted(key.items(), key=lambda kv: int(kv[0])):
        rec = json.load(open(os.path.join(a.run, "with", k["id"] + ".json")))
        docs = []
        for e in rec.get("expect", []):
            kind, ref = e.split(":", 1)
            docs.append((kind, os.path.join(corp, ref) if kind == "file" else ref))
        for s in rec.get("know_sources", []):
            if s.get("k") == "file":
                docs.append(("file", s["p"]))
            else:
                docs.append(("chat", s["u"].split("know:chat:", 1)[1]))
        seen, blocks = set(), []
        for kind, ref in docs:
            rk = (kind, os.path.realpath(ref) if kind == "file" else ref)
            if rk in seen:
                continue
            seen.add(rk)
            if kind == "file" and os.path.exists(ref):
                blocks.append("### File: %s\n\n%s" % (os.path.relpath(os.path.realpath(ref), corp), file_text(ref)))
            elif kind == "chat" and ref in chats:
                blocks.append("### " + chat_text(chats[ref]))
        pk = open(os.path.join(a.run, "packets", "%02d.md" % int(n))).read()
        head, rest = pk.split("## Answer A", 1)
        with open(os.path.join(a.dest, "%02d.md" % int(n)), "w") as fh:
            fh.write(head.rstrip() + "\n\n## The asker's records on this topic (the same for both answers)\n\n"
                     + "\n\n".join(blocks) + "\n\n## Answer A" + rest)
    print(len(key), "packets in", a.dest)


if __name__ == "__main__":
    main()
