#!/usr/bin/env python3
"""The wish model's fence, offline: what it returns is re-validated against
the vocabulary and the airlines in the search, labels are built here, and
without credentials it steps aside for the page's own parser."""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import wish  # noqa: E402

FAILED = 0


def check(name, ok, detail=""):
    global FAILED
    print(("  ok   " if ok else "  FAIL ") + name + (("  " + detail) if detail and not ok else ""))
    if not ok:
        FAILED += 1


AIR = [{"code": "IB", "name": "Iberia"}, {"code": "BA", "name": "British Airways"}]


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        blk = types.SimpleNamespace(type="text", text=json.dumps(self.payload))
        return types.SimpleNamespace(content=[blk])


def with_client(payload):
    fake = FakeClient(payload)
    wish._client = lambda: (fake, "")
    return fake


# 1. no credentials: fallback, nothing invented
os.environ.pop("ANTHROPIC_API_KEY", None); os.environ.pop("ANTHROPIC_AUTH_TOKEN", None); os.environ.pop("ANTHROPIC_PROFILE", None)
r = wish.parse("not iberia", AIR)
check("no credentials -> fallback with no items", r["source"] == "fallback" and r["items"] == [], json.dumps(r))

# 2. a good answer is kept and labelled here
fake = with_client({"items": [
    {"kind": "avoid", "code": "IB", "unless": "nonstop", "id": None, "p": None, "cents": None, "h": None, "note": None},
    {"kind": "want", "code": None, "unless": None, "id": "lieflat", "p": None, "cents": None, "h": None, "note": None},
    {"kind": "max", "code": None, "unless": None, "id": None, "p": None, "cents": 50000, "h": None, "note": None},
    {"kind": "after", "code": None, "unless": None, "id": None, "p": None, "cents": None, "h": 9, "note": None},
    {"kind": "tilt", "code": None, "unless": None, "id": None, "p": "asap", "cents": None, "h": None, "note": None},
], "ask": ""})
r = wish.parse("not iberia unless nonstop, lie-flat, nothing over $500, not before 9, asap", AIR)
labels = [i["label"] for i in r["items"]]
check("model items kept", r["source"] == "model" and len(r["items"]) == 5, json.dumps(r))
check("labels built by the server", labels == ["Avoid Iberia unless nonstop", "Lie-flat seat matters", "Nothing over $500 all in", "Nothing before 09:00", "Get there as soon as possible"], json.dumps(labels))
check("the call used the model, low effort and the schema", fake.calls[0]["model"] == "claude-opus-5" and fake.calls[0]["output_config"]["effort"] == "low" and fake.calls[0]["output_config"]["format"]["schema"] is wish.SCHEMA)
check("the vocabulary is cached", fake.calls[0]["system"][0].get("cache_control", {}).get("type") == "ephemeral")
check("no temperature is sent (removed on this model)", "temperature" not in fake.calls[0])

# 3. what the vocabulary does not allow is dropped: an airline not in the search, an unknown want, a bad tilt, a negative cap, an hour off the clock
with_client({"items": [
    {"kind": "avoid", "code": "ZZ", "unless": None, "id": None, "p": None, "cents": None, "h": None, "note": None},
    {"kind": "want", "code": None, "unless": None, "id": "jacuzzi", "p": None, "cents": None, "h": None, "note": None},
    {"kind": "tilt", "code": None, "unless": None, "id": None, "p": "0.9,0.1,0", "cents": None, "h": None, "note": None},
    {"kind": "max", "code": None, "unless": None, "id": None, "p": None, "cents": -5, "h": None, "note": None},
    {"kind": "before", "code": None, "unless": None, "id": None, "p": None, "cents": None, "h": 27, "note": None},
    {"kind": "note", "code": None, "unless": None, "id": None, "p": None, "cents": None, "h": None, "note": "seat 14A costs $30"},
    {"kind": "rank", "code": None, "unless": None, "id": None, "p": None, "cents": None, "h": None, "note": None},
    {"kind": "avoid", "code": "BA", "unless": "teleport", "id": None, "p": None, "cents": None, "h": None, "note": None},
], "ask": "Did you mean Iberia? It is about $40 more."})
r = wish.parse("anything", AIR)
check("everything outside the vocabulary is dropped", [i["kind"] for i in r["items"]] == ["avoid"] and r["items"][0]["code"] == "BA" and "unless" not in r["items"][0], json.dumps(r))
check("an ask carrying a figure is suppressed", r["ask"] == "", r["ask"])

# 4. duplicates collapse; a question comes back with no items
with_client({"items": [
    {"kind": "want", "code": None, "unless": None, "id": "wifi", "p": None, "cents": None, "h": None, "note": None},
    {"kind": "want", "code": None, "unless": None, "id": "wifi", "p": None, "cents": None, "h": None, "note": None},
], "ask": "  Which airline do you mean by  'the red one'?  "})
r = wish.parse("wifi wifi", AIR)
check("duplicates collapse and the ask is tidied", len(r["items"]) == 1 and r["ask"] == "Which airline do you mean by 'the red one'?", json.dumps(r))

# 5. a failed call steps aside rather than raising
class Boom(FakeClient):
    def _create(self, **kw):
        raise RuntimeError("network")
wish._client = lambda: (Boom({}), "")
r = wish.parse("not iberia", AIR)
check("a failed call is a fallback, not an error", r["source"] == "fallback" and "failed" in r["note"], json.dumps(r))

# 6. the schema is strict: every property required, nothing extra
item = wish.SCHEMA["properties"]["items"]["items"]
check("strict item schema", item["additionalProperties"] is False and set(item["required"]) == set(item["properties"]))

print("\n%s" % ("all wish checks passed" if not FAILED else "%d wish checks FAILED" % FAILED))
sys.exit(1 if FAILED else 0)
