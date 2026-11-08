#!/usr/bin/env python3
"""Synthetic chat exports for the import tests (6b529).

Every file here is FICTIONAL: made-up people, places and dates, no real
account data. The repo is public. The shapes mirror the vendors' real
exports as documented on 2026-11-08 (see NOTES 6b529): ChatGPT's
conversations.json mapping tree with branches, hidden and system nodes,
code and execution output, image asset pointers and DALL·E files; Claude's
conversations.json with content blocks, attachments with extracted text,
projects.json, users.json and memories.json; Google Takeout's Gemini Apps
MyActivity.json in its three attested shapes and as HTML; plus plain
transcripts, a generic role/content JSON, a zip of nothing useful, a
Takeout zip without Gemini and a damaged zip.

Deterministic: fixed timestamps everywhere, so the zips are byte-identical
on every run and the gauntlet can prove they haven't drifted.

    python3 tests/fixtures/imports/make_fixtures.py            # (re)write them
    python3 tests/fixtures/imports/make_fixtures.py --big /tmp/big.json 200
"""
import io
import json
import os
import struct
import sys
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ZIP_DATE = (2026, 1, 15, 12, 0, 0)


def png(w=48, h=32, seed=1):
    """A real, valid PNG (RGB, a two-tone gradient), so pictures render."""
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            r = (x * 255 // max(1, w - 1) + seed * 40) & 255
            g = (y * 255 // max(1, h - 1)) & 255
            b = (255 - r) & 255
            row += bytes((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# a 1x1 lossless WebP (the smallest valid one), for the DALL·E folder
WEBP_1X1 = bytes.fromhex("524946461a000000574542505650384c0d0000002f0000001007101111"
                         "88 88 fe 07 00".replace(" ", ""))
# the bytes of a GIF header: a picture format we don't serve
GIF_HEAD = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 16


def add(zf, name, data):
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data if isinstance(data, bytes) else data.encode("utf-8"))


def write_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            add(zf, name, data)


def j(x):
    return json.dumps(x, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------- ChatGPT
T0 = 1766000000.0        # 2025-12-17 (fictional dates)


def gpt_node(nid, parent, children, role=None, ct="text", parts=None, t=None,
             meta=None, extra=None):
    msg = None
    if role:
        content = {"content_type": ct}
        if parts is not None:
            content["parts"] = parts
        if extra:
            content.update(extra)
        msg = {"id": nid, "author": {"role": role, "name": None, "metadata": {}},
               "create_time": t, "update_time": None, "content": content,
               "status": "finished_successfully", "end_turn": True, "weight": 1.0,
               "metadata": meta or {}, "recipient": "all", "channel": None}
    return {"id": nid, "message": msg, "parent": parent, "children": children}


def chatgpt_conversations():
    # A: Lisbon. Root -> system (hidden) -> user -> assistant(gpt-4o) -> user
    #    -> [assistant v1 (abandoned), assistant v2 (current)] with a thoughts
    #    node, a code + execution_output pair and a picture the user sent;
    #    then a DALL·E picture from the tool.
    a = {
        "title": "Trip to Lisbon in March",
        "create_time": T0, "update_time": T0 + 5000,
        "conversation_id": "conv-a1a1a1a1-0000-4000-8000-000000000001",
        "id": "conv-a1a1a1a1-0000-4000-8000-000000000001",
        "current_node": "a-t2",
        "default_model_slug": "gpt-4o",
        "is_archived": False, "is_starred": None, "gizmo_id": None,
        "mapping": {
            "a-root": gpt_node("a-root", None, ["a-sys"]),
            # a system message WITH text (dropped as system), and below a
            # hidden assistant note with text (dropped as hidden): each
            # would show if its rule were lost
            "a-sys": gpt_node("a-sys", "a-root", ["a-u1"], "system",
                              parts=["You are a helpful assistant. The user is planning a trip."],
                              t=T0, meta={"is_visually_hidden_from_conversation": True}),
            "a-u1": gpt_node("a-u1", "a-sys", ["a-a1"], "user",
                             ct="multimodal_text",
                             parts=[{"content_type": "image_asset_pointer",
                                     "asset_pointer": "file-service://file-AbC123xyz",
                                     "size_bytes": 4000, "width": 48, "height": 32,
                                     "fovea": None, "metadata": None},
                                    "Here's where we're staying. Three days in Lisbon in "
                                    "March: what should we not miss?"],
                             t=T0 + 10,
                             meta={"attachments": [{"id": "file-AbC123xyz", "name": "flat.png",
                                                    "size": 4000, "width": 48, "height": 32}]}),
            "a-a1": gpt_node("a-a1", "a-u1", ["a-h1"], "assistant",
                             parts=["Three days is a good fit. Day one: Alfama on foot, "
                                    "the viewpoint at Santa Luzia, then tram 28 back.\n\n"
                                    "Day two: Belém in the morning (the monastery opens "
                                    "at 10), pastéis at the counter, the river walk.\n\n"
                                    "Day three: LX Factory and a late lunch in Cais do Sodré."],
                             t=T0 + 40, meta={"model_slug": "gpt-4o"}),
            "a-h1": gpt_node("a-h1", "a-a1", ["a-u2"], "assistant",
                             parts=["Model set context updated (hidden note)."], t=T0 + 41,
                             meta={"is_visually_hidden_from_conversation": True,
                                   "model_slug": "gpt-4o"}),
            "a-u2": gpt_node("a-u2", "a-h1", ["a-r1", "a-t0"], "user",
                             parts=["Can you work out the tram fares for two people, "
                                    "in Python?"], t=T0 + 900),
            # the regenerated branch, abandoned
            "a-r1": gpt_node("a-r1", "a-u2", [], "assistant",
                             parts=["Sure, roughly 3 euros each way."], t=T0 + 920,
                             meta={"model_slug": "gpt-4o"}),
            # the shown branch: thoughts (dropped), code, its output, prose
            "a-t0": gpt_node("a-t0", "a-u2", ["a-c1"], "assistant", ct="thoughts",
                             t=T0 + 930, meta={"model_slug": "o3"},
                             extra={"thoughts": [{"summary": "Working out fares",
                                                  "content": "Each ride is 3.20…"}]}),
            "a-c1": gpt_node("a-c1", "a-t0", ["a-o1"], "assistant", ct="code",
                             t=T0 + 940, meta={"model_slug": "o3"},
                             extra={"language": "python",
                                    "text": "fare = 3.20\nprint(fare * 2 * 2)"}),
            "a-o1": gpt_node("a-o1", "a-c1", ["a-t1"], "tool", ct="execution_output",
                             t=T0 + 950, extra={"text": "12.8"}),
            "a-t1": gpt_node("a-t1", "a-o1", ["a-u3"], "assistant",
                             parts=["Two return trips for two people come to 12.80 euros "
                                    "at the onboard fare. A day pass is cheaper if you ride "
                                    "more than twice."],
                             t=T0 + 960, meta={"model_slug": "o3"}),
            "a-u3": gpt_node("a-u3", "a-t1", ["a-d1"], "user",
                             parts=["Draw me a postcard of the tram at sunset."],
                             t=T0 + 4000),
            "a-d1": gpt_node("a-d1", "a-u3", ["a-t2"], "tool", ct="multimodal_text",
                             parts=[{"content_type": "image_asset_pointer",
                                     "asset_pointer": "file-service://file-DaLLe777",
                                     "size_bytes": 34, "width": 1, "height": 1,
                                     "metadata": {"dalle": {"gen_id": "g1",
                                                            "prompt": "A yellow tram at sunset",
                                                            "seed": 1}}}],
                             t=T0 + 4900),
            "a-t2": gpt_node("a-t2", "a-d1", [], "assistant",
                             parts=["Here's your postcard."], t=T0 + 5000,
                             meta={"model_slug": "gpt-4o"}),
        },
    }
    # B: sourdough. A custom-instructions system node (kept as instructions,
    #    not as a message), a quote from the web, a user file not in the zip.
    b = {
        "title": "Sourdough timing",
        "create_time": T0 + 86400, "update_time": T0 + 86400 + 300,
        "conversation_id": "conv-b2b2b2b2-0000-4000-8000-000000000002",
        "current_node": "b-a2",
        "default_model_slug": "gpt-4o-mini",
        "mapping": {
            "b-root": gpt_node("b-root", None, ["b-ctx"]),
            "b-ctx": gpt_node("b-ctx", "b-root", ["b-u1"], "system",
                              ct="user_editable_context", t=T0 + 86400,
                              meta={"is_user_system_message": True,
                                    "user_context_message_data": {
                                        "about_user_message": "I bake at home on weekends.",
                                        "about_model_message": "Keep answers short."}},
                              extra={"user_profile": "I bake at home on weekends.",
                                     "user_instructions": "Keep answers short."}),
            "b-u1": gpt_node("b-u1", "b-ctx", ["b-a1"], "user",
                             parts=["My starter doubles in 5 hours. Bulk ferment how long?"],
                             t=T0 + 86400 + 5,
                             meta={"attachments": [{"id": "file-NotInZip99", "name": "notes.txt",
                                                    "size": 300}]}),
            "b-a1": gpt_node("b-a1", "b-u1", ["b-q1"], "assistant",
                             parts=["About 4 to 5 hours at 24 °C, until it has grown by "
                                    "half and the top is domed."],
                             t=T0 + 86400 + 60, meta={"model_slug": "gpt-4o-mini"}),
            "b-q1": gpt_node("b-q1", "b-a1", ["b-a2"], "assistant", ct="tether_quote",
                             t=T0 + 86400 + 61,
                             extra={"title": "Bulk fermentation guide", "url": "https://example.org/bulk",
                                    "domain": "example.org",
                                    "text": "Judge bulk by volume and feel, not the clock."}),
            "b-a2": gpt_node("b-a2", "b-q1", [], "assistant",
                             parts=["Judge it by the dough, not the clock."],
                             t=T0 + 86400 + 300, meta={"model_slug": "gpt-4o-mini"}),
        },
    }
    # C: nothing shown (only a hidden system node): counted as empty
    c = {"title": "New chat", "create_time": T0 + 2 * 86400, "update_time": T0 + 2 * 86400,
         "conversation_id": "conv-c3c3c3c3-0000-4000-8000-000000000003", "current_node": "c-sys",
         "mapping": {"c-root": gpt_node("c-root", None, ["c-sys"]),
                     "c-sys": gpt_node("c-sys", "c-root", [], "system", parts=[""],
                                       t=T0 + 2 * 86400,
                                       meta={"is_visually_hidden_from_conversation": True})}}
    return [a, b, c]


def make_chatgpt():
    convs = chatgpt_conversations()
    members = [
        ("conversations.json", j(convs)),
        ("chat.html", "<!doctype html><title>ChatGPT Data Export</title><p>see conversations.json</p>"),
        ("user.json", j({"id": "user-fictional000000000000", "email": "someone@example.com",
                         "chatgpt_plus_user": False, "phone_number": None})),
        ("message_feedback.json", j([])),
        ("shared_conversations.json", j([])),
        ("model_comparisons.json", j([])),
        ("file-AbC123xyz-flat.png", png(48, 32, 1)),
        ("dalle-generations/file-DaLLe777-0f0f0f0f-0000-4000-8000-000000000001.webp", WEBP_1X1),
        ("file-Unused0001-doodle.gif", GIF_HEAD),
    ]
    write_zip(os.path.join(HERE, "chatgpt-export.zip"), members)
    # the newer split layout: personal/conversations/conversations-000.json …
    half = [convs[0]], [convs[1], convs[2]]
    members2 = [
        ("personal/conversations/conversations-000.json", j(half[0])),
        ("personal/conversations/conversations-001.json", j(half[1])),
        ("export_manifest.json", j({"export_files": [
            {"path": "personal/conversations/conversations-000.json", "size_bytes": 1},
            {"path": "personal/conversations/conversations-001.json", "size_bytes": 1}]})),
        ("user_settings.json", j({"theme": "dark"})),
        ("file-AbC123xyz-flat.png", png(48, 32, 1)),
        ("dalle-generations/file-DaLLe777-0f0f0f0f-0000-4000-8000-000000000001.webp", WEBP_1X1),
    ]
    write_zip(os.path.join(HERE, "chatgpt-export-split.zip"), members2)
    # a bare conversations.json, as a person might drop it unzipped
    with open(os.path.join(HERE, "chatgpt-conversations.json"), "w", encoding="utf-8") as f:
        f.write(j(convs))


# ------------------------------------------------------------------ Claude
def make_claude():
    convs = [
        {"uuid": "11111111-aaaa-4bbb-8ccc-000000000001",
         "name": "Rename the garden shed project",
         "summary": "", "created_at": "2026-02-03T09:12:00.000000Z",
         "updated_at": "2026-02-03T09:40:00.000000Z",
         "account": {"uuid": "acct-fictional"},
         "chat_messages": [
             {"uuid": "m1", "sender": "human", "text": "Three names for a tiny tool shed turned writing room?",
              "created_at": "2026-02-03T09:12:00.000000Z", "updated_at": "2026-02-03T09:12:00.000000Z",
              "content": [{"type": "text", "text": "Three names for a tiny tool shed turned writing room?",
                           "start_timestamp": "2026-02-03T09:12:00Z", "stop_timestamp": "2026-02-03T09:12:00Z",
                           "citations": []}],
              "attachments": [], "files": []},
             {"uuid": "m2", "sender": "assistant", "text": "",
              "created_at": "2026-02-03T09:12:20.000000Z", "updated_at": "2026-02-03T09:12:30.000000Z",
              "content": [{"type": "thinking", "thinking": "Short, warm, a little dry.", "summaries": [],
                           "cut_off": False},
                          {"type": "text", "text": "The Nib. Shedquarters. Eight by Ten.", "citations": []}],
              "attachments": [], "files": []},
             {"uuid": "m3", "sender": "human", "text": "Here's the floor plan. Where does the desk go?",
              "created_at": "2026-02-03T09:30:00.000000Z", "updated_at": "2026-02-03T09:30:00.000000Z",
              "content": [{"type": "text", "text": "Here's the floor plan. Where does the desk go?"}],
              "attachments": [{"file_name": "shed-plan.txt", "file_type": "text/plain", "file_size": 88,
                               "extracted_content": "Door on the south wall. Window east. 8 ft by 10 ft."}],
              "files": [{"file_name": "shed-photo.jpg"}]},
             {"uuid": "m4", "sender": "assistant", "text": "",
              "created_at": "2026-02-03T09:31:00.000000Z", "updated_at": "2026-02-03T09:40:00.000000Z",
              "content": [{"type": "tool_use", "name": "web_search", "input": {"query": "desk under east window"},
                           "id": "tu1"},
                          {"type": "tool_result", "tool_use_id": "tu1", "name": "web_search",
                           "content": [{"type": "knowledge", "title": "x", "url": "https://example.org"}],
                           "is_error": False},
                          {"type": "text", "text": "Under the east window: morning light on the page, "
                                                   "and the door stays clear."}],
              "attachments": [], "files": []},
         ]},
        {"uuid": "22222222-aaaa-4bbb-8ccc-000000000002",
         "name": "", "created_at": "2026-03-10T18:00:00Z", "updated_at": "2026-03-10T18:02:00Z",
         "account": {"uuid": "acct-fictional"},
         "chat_messages": [
             {"uuid": "n1", "sender": "human", "text": "What's a good Tuesday soup?",
              "created_at": "2026-03-10T18:00:00Z", "updated_at": "2026-03-10T18:00:00Z",
              "content": [], "attachments": [], "files": []},
             {"uuid": "n2", "sender": "assistant", "text": "Leek and potato, forty minutes, one pot.",
              "created_at": "2026-03-10T18:00:30Z", "updated_at": "2026-03-10T18:00:30Z",
              "content": [], "attachments": [], "files": []},
         ]},
    ]
    projects = [{"uuid": "p-0001", "name": "Shed room", "description": "Turning the shed into a study",
                 "prompt_template": "You help plan a small writing room.", "created_at": "2026-02-01T10:00:00Z",
                 "updated_at": "2026-02-01T10:00:00Z", "creator": {"uuid": "acct-fictional"},
                 "docs": [{"uuid": "d-1", "filename": "measurements.md",
                           "content": "# Measurements\n8 ft by 10 ft, 7 ft at the eaves.",
                           "created_at": "2026-02-01T10:00:00Z"}]}]
    users = [{"uuid": "acct-fictional", "full_name": "Sam Example", "email_address": "sam@example.com",
              "verified_phone_number": None}]
    memories = [{"conversations_memory": "Sam is converting a garden shed into a writing room.",
                 "project_memories": {"p-0001": "Desk goes under the east window."},
                 "account_uuid": "acct-fictional"}]
    write_zip(os.path.join(HERE, "claude-export.zip"), [
        ("conversations.json", j(convs)), ("projects.json", j(projects)),
        ("users.json", j(users)), ("memories.json", j(memories))])


# ------------------------------------------------------------------ Gemini
def make_gemini():
    base = "Takeout/My Activity/Gemini Apps/"
    ents = [
        # shape (a): "Prompted: …" with the answer as HTML
        {"header": "Gemini Apps", "title": "Prompted: What rhymes with orange?",
         "safeHtmlItem": [{"html": "<p>Nothing exactly. <b>Door hinge</b> comes close, and so does <i>Blorenge</i>, a hill in Wales.</p>"}],
         "time": "2026-04-02T08:00:00.000Z", "products": ["Gemini Apps"],
         "activityControls": ["Gemini Apps Activity"]},
        {"header": "Gemini Apps", "title": "Prompted: Use that in a limerick",
         "safeHtmlItem": [{"html": "<p>A painter who ran out of orange<br>Found solace up on the Blorenge…</p>"}],
         "time": "2026-04-02T08:04:00.000Z", "products": ["Gemini Apps"]},
        {"header": "Gemini Apps", "title": "Prompted: Shorter",
         "safeHtmlItem": [{"html": "<p>Orange, Blorenge: a hill, a hinge.</p>"}],
         "time": "2026-04-02T08:20:00.000Z", "products": ["Gemini Apps"]},
        # two hours later: a new chat by the time rule
        {"header": "Gemini Apps", "title": "Prompted: How long do I boil an egg for jammy?",
         "safeHtmlItem": [{"html": "<p>Six and a half minutes from boiling, then into cold water.</p>"}],
         "time": "2026-04-02T10:30:00.000Z", "products": ["Gemini Apps"]},
        # shape (b): details Request/Response with a conversation id
        {"header": "Gemini", "title": "Used Gemini Apps",
         "titleUrl": "https://gemini.google.com/app/c/9f8e7d6c5b4a",
         "details": [{"name": "Request", "value": "Plan a 20-minute stretch routine"},
                     {"name": "Response", "value": "Five minutes of neck and shoulders, ten of hips and hamstrings, five of back."}],
         "time": "2026-04-03T07:00:00.000Z", "products": ["Gemini Apps"]},
        {"header": "Gemini", "title": "Used Gemini Apps",
         "titleUrl": "https://gemini.google.com/app/c/9f8e7d6c5b4a",
         "userInteractions": [{"userInteraction": {"endpoint": "chat",
                                                  "request": "Make it ten minutes",
                                                  "response": "Neck, hips, back: three minutes each, one to breathe."}}],
         "time": "2026-04-03T07:03:00.000Z", "products": ["Gemini Apps"]},
        # another product in the same file: ignored
        {"header": "Search", "title": "Searched for weather", "time": "2026-04-03T07:10:00.000Z",
         "products": ["Search"]},
    ]
    write_zip(os.path.join(HERE, "gemini-takeout.zip"), [
        (base + "MyActivity.json", j(ents)),
        ("Takeout/archive_browser.html", "<!doctype html><title>Takeout</title>")])
    html = ("<!doctype html><html><body>"
            '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
            '<p class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Gemini Apps<br></p></p>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Prompted Name three quiet board games'
            '<br>Apr 5, 2026, 9:15:02 PM UTC<br>Azul, Patchwork and Hive. All under an hour.</div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
            '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>&emsp;Gemini Apps<br></div>'
            "</div></div>"
            '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp"><div class="mdl-grid">'
            '<p class="header-cell mdl-cell mdl-cell--12-col"><p class="mdl-typography--title">Gemini Apps<br></p></p>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">Prompted Which is best for two?'
            '<br>Apr 5, 2026, 9:17:40 PM UTC<br>Patchwork. It was made for two.</div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 mdl-typography--text-right"></div>'
            '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption"><b>Products:</b><br>&emsp;Gemini Apps<br></div>'
            "</div></div></body></html>")
    write_zip(os.path.join(HERE, "gemini-takeout-html.zip"), [(base + "MyActivity.html", html)])
    # a Takeout zip without Gemini in it
    write_zip(os.path.join(HERE, "takeout-no-gemini.zip"), [
        ("Takeout/archive_browser.html", "<!doctype html><title>Takeout</title>"),
        ("Takeout/My Activity/Search/MyActivity.json", j([{"header": "Search", "title": "Searched for x",
                                                            "time": "2026-04-03T07:10:00.000Z"}]))])


# ------------------------------------------------------------- transcripts
def make_files():
    md = ("# Planning the allotment\n\n"
          "**You**: Which beds get the brassicas this year?\n\n"
          "**Claude**: The two on the north side, so the beans shade nothing.\n\n"
          "**You**: And the onions?\n\n"
          "**Claude**: Front left. They want the sun and don't mind the wind.\n\n"
          "```\nbeds = ['north-1', 'north-2']\n```\n")
    with open(os.path.join(HERE, "transcript.md"), "w", encoding="utf-8") as f:
        f.write(md)
    notes = ("# Notes from the talk\n\nMostly about pacing. Three ideas worth keeping:\n\n"
             "- start with the question\n- one example per claim\n- stop early\n")
    with open(os.path.join(HERE, "notes.md"), "w", encoding="utf-8") as f:
        f.write(notes)
    generic = {"title": "Bike fit", "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Saddle height for a 32 inch inseam?"},
        {"role": "assistant", "content": "Start around 71 cm from the bottom bracket centre and adjust by feel."}]}
    with open(os.path.join(HERE, "generic-chat.json"), "w", encoding="utf-8") as f:
        f.write(j(generic))
    write_zip(os.path.join(HERE, "transcripts.zip"), [
        ("allotment/transcript.md", md), ("notes.md", notes), ("generic-chat.json", j(generic)),
        ("__MACOSX/._notes.md", b"\x00junk")])
    write_zip(os.path.join(HERE, "not-an-export.zip"), [
        ("photos/readme.pdf", b"%PDF-1.4 fake"), ("photos/a.bin", b"\x00\x01\x02")])
    with open(os.path.join(HERE, "corrupt.zip"), "wb") as f:
        f.write(b"PK\x03\x04" + b"this is not really a zip file" * 20)


# --------------------------------------------------------------- the big one
def make_big(path, mb):
    """A conversations.json of about `mb` MB: the Lisbon chat repeated with
    unique ids, for the streaming test. Not committed."""
    a = chatgpt_conversations()[0]
    one = json.dumps(a)
    target = mb * 1024 * 1024
    with open(path, "w", encoding="utf-8") as f:
        f.write("[")
        i, written = 0, 1
        while written < target:
            s = one.replace("conv-a1a1a1a1-0000-4000-8000-000000000001", "conv-big-%08d" % i)
            s = s.replace("Trip to Lisbon in March", "Trip %d" % i)
            if i:
                f.write(",")
                written += 1
            f.write(s)
            written += len(s)
            i += 1
        f.write("]")
    return i


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--big":
        n = make_big(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 200)
        print("wrote", sys.argv[2], "with", n, "chats")
        return
    make_chatgpt()
    make_claude()
    make_gemini()
    make_files()
    for n in sorted(os.listdir(HERE)):
        if n != os.path.basename(__file__):
            print("%9d  %s" % (os.path.getsize(os.path.join(HERE, n)), n))


if __name__ == "__main__":
    main()
