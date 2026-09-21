#!/usr/bin/env python3
"""The ledger narrator. Build step five.

Hard rule 1 says the LLM narrates and does not rank. This module is the whole
of the "narrates" half, and it is built so that the rule holds by construction
rather than by good intentions.

THE MODEL DOES NOT DO ARITHMETIC. `brief()` precomputes every number the prose
is allowed to contain - the saving, the gap, the effective costs, the grades -
and hands over a closed list of them. The model picks words. It cannot divide
two figures and quote the result, because a result it computed would not be in
the allowed set and `verify()` would reject the sentence.

VERIFY, THEN FALL BACK. Every generated sentence is checked against the brief:
each dollar figure, percentage and duration must be one the arithmetic already
produced, each flight and airport must be one in the brief, and a short list of
certainty phrases is banned outright because hard rule 2 forbids unhedged
equipment claims. Anything that fails is discarded and the deterministic
template ships instead. A narrator that cannot be checked is a narrator that
will eventually invent a number.

THE TEMPLATE IS THE PRODUCT'S FLOOR, not a degraded mode. With no API key, no
network, or a failed check, the page still says something true. Hard rule 5's
spirit: nothing external is load-bearing.

This is the one surface in ConcordeGo that is not deterministic, and it is the
only one where that is safe - prose cannot reorder a list. `temperature` would
not help even if it were available: it is removed on Claude Opus 5 and returns
a 400.

The `anthropic` SDK is imported lazily and is entirely optional; the scorer and
its tests remain stdlib-only.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

MODEL = "claude-opus-5"

# Thousands separators are part of a figure; a sentence comma after one is
# not. "$455," must read as $455, or the verifier rejects its own template
# and every narration silently degrades.
MONEY_RE = r"\$\d{1,3}(?:,\d{3})*(?!\d)"

SYSTEM = """You write one short paragraph for a flight search result.

You are given a BRIEF containing figures that have already been calculated. Your
job is to turn them into two sentences a person would actually say.

Absolute rules:
- Use ONLY numbers that appear in the brief. Never calculate anything - not a
  percentage, not a difference, not a total. If a number is not in the brief, it
  cannot go in the sentence.
- Never state an aircraft, cabin or connectivity fact as certain. The brief
  gives observed frequencies; carry them across ("on 73% of recent departures"),
  or leave the claim out.
- Never recommend a different order, and never say an option is "best" beyond
  what the brief's ordering already says.
- No exclamation marks, no "amazing", no sales voice. Plain and specific.

Write:
- lead: one sentence on the trade-off a traveller should understand first.
- sub: one sentence on the top pick and what it costs all-in.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "lead": {"type": "string", "description": "One sentence on the headline trade-off."},
        "sub": {"type": "string", "description": "One sentence on the top pick."},
    },
    "required": ["lead", "sub"],
    "additionalProperties": False,
}

# Phrases that promise something the data cannot. Hard rule 2, enforced on the
# way out rather than only asked for on the way in.
BANNED = [
    "you will have", "you'll have", "guaranteed", "always has", "definitely",
    "is equipped with", "comes with wifi", "you will get", "you'll get",
    "certainly", "every flight has", "will be flown",
]


def _d(cents: int) -> str:
    return "$%s" % format(abs(int(round(cents / 100.0))), ",d")


def _hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return "%dh%02dm" % (h, m) if h else "%dm" % m


def brief(fixture: Dict[str, Any], options: List[Dict[str, Any]],
          profile: str) -> Dict[str, Any]:
    """Everything the narrator may say, and nothing else.

    Note what is precomputed here: `saving_pct`, `gap`, every effective cost.
    The model is never asked to work anything out, which is what makes the
    output checkable."""
    if not options:
        return {"profile": profile, "options": [], "allowed": {}}

    top = options[0]
    cheapest = min(options, key=lambda o: o["ticket_cents"])
    gap_cents = cheapest["effective_cents"] - top["effective_cents"]
    saving_pct = (int(round((1 - cheapest["ticket_cents"] / float(top["ticket_cents"])) * 100))
                  if top["ticket_cents"] else 0)

    def worst_line(o):
        rows = [l for l in o["lines"]
                if l["kind"] != "base" and l["code"] != "time" and l["amount_cents"] > 0]
        return max(rows, key=lambda l: l["amount_cents"]) if rows else None

    worst = worst_line(cheapest)

    def shape(o, rank):
        return {
            "rank": rank,
            "route": o["route"].replace(" - ", " to "),
            "carrier": o["carrier"],
            "fare": o["fare_brand"],
            "ticket": _d(o["ticket_cents"]),
            "effective": _d(o["effective_cents"]),
            "grade": o["grade"],
            "door_to_door": _hm(o["door_to_door_minutes"]),
            "stops": o["stops"],
            "notable": [
                {"what": l["label"], "amount": _d(l["amount_cents"]),
                 "direction": "costs you" if l["amount_cents"] > 0 else "in its favour",
                 "evidence": l["evidence"]}
                for l in o["lines"]
                if l["kind"] != "base" and abs(l["amount_cents"]) >= 4000
            ][:4],
        }

    shaped = [shape(o, i + 1) for i, o in enumerate(options[:3])]
    b = {
        "profile": profile,
        "route": fixture.get("destination", ""),
        "options": shaped,
        "cheapest_ticket": {
            "carrier": cheapest["carrier"], "ticket": _d(cheapest["ticket_cents"]),
            "effective": _d(cheapest["effective_cents"]),
            "saving_pct": "%d%%" % saving_pct,
            "worse_by": _d(gap_cents),
            "mostly_because": worst["label"] if worst else None,
            "is_also_top_pick": cheapest["option_id"] == top["option_id"],
        },
    }

    # the closed vocabulary of figures, harvested from what we just built
    blob = _walk(b)
    b["allowed"] = {
        "money": sorted({m for m in re.findall(MONEY_RE, blob)}),
        "percent": sorted({m for m in re.findall(r"\d+%", blob)}),
        "durations": sorted({m for m in re.findall(r"\d+h\d+m|\b\d+m\b", blob)}),
        "codes": sorted({m for m in re.findall(r"\b[A-Z]{2,3}\s?\d{1,4}\b|\b[A-Z]{3}\b", blob)}),
    }
    return b


def _walk(node: Any) -> str:
    if isinstance(node, dict):
        return " ".join(_walk(v) for v in node.values())
    if isinstance(node, list):
        return " ".join(_walk(v) for v in node)
    return str(node)


def template(b: Dict[str, Any]) -> Dict[str, str]:
    """The deterministic floor. Always available, always true."""
    if not b.get("options"):
        return {"lead": "Nothing matched.", "sub": "", "source": "template"}
    top = b["options"][0]
    c = b["cheapest_ticket"]
    if c["is_also_top_pick"]:
        lead = "The cheapest ticket here is also the one that costs you least, which is rarer than it sounds."
    else:
        lead = ("You can save %s on the ticket at %s - it lands %s worse once everything is counted%s."
                % (c["saving_pct"], c["ticket"], c["worse_by"],
                   ", mostly " + c["mostly_because"][0].lower() + c["mostly_because"][1:]
                   if c["mostly_because"] else ""))
    sub = ("Top pick %s at %s, %s effective, %s door to door. Grade %s."
           % (top["route"], top["ticket"], top["effective"], top["door_to_door"], top["grade"]))
    return {"lead": lead, "sub": sub, "source": "template"}


def verify(text: str, b: Dict[str, Any], max_len: int = 400) -> Tuple[bool, str]:
    """Is every checkable claim in this sentence one the arithmetic produced?
    `max_len` is the narrator's two sentences by default; the delayed-flight
    helper's advice is allowed more."""
    allowed = b.get("allowed", {})
    low = text.lower()

    for phrase in BANNED:
        if phrase in low:
            return False, "unhedged certainty: %r" % phrase

    for money in re.findall(MONEY_RE, text):
        if money not in allowed.get("money", []):
            return False, "invented figure %s" % money
    for pct in re.findall(r"\d+%", text):
        if pct not in allowed.get("percent", []):
            return False, "invented percentage %s" % pct
    for dur in re.findall(r"\d+h\d+m", text):
        if dur not in allowed.get("durations", []):
            return False, "invented duration %s" % dur
    for code in re.findall(r"\b[A-Z]{3}\b", text):
        if code not in allowed.get("codes", []):
            return False, "airport or carrier not in the brief: %s" % code

    if len(text) > max_len:
        return False, "too long (%d chars)" % len(text)
    return True, ""


def _client():
    """The SDK is optional. No key, no package, no network - the template ships."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")):
        return None, "no credentials in the environment"
    try:
        import anthropic
    except ImportError:
        return None, "the anthropic package is not installed"
    try:
        return anthropic.Anthropic(), ""
    except Exception as exc:
        return None, "client init failed: %s" % exc


def narrate(b: Dict[str, Any], allow_model: bool = True) -> Dict[str, str]:
    """Model prose when it can be had AND checked; the template otherwise."""
    fallback = template(b)
    if not allow_model or not b.get("options"):
        return fallback

    client, why = _client()
    if client is None:
        fallback["note"] = why
        return fallback

    import json
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            # Opus 5 thinks by default; low effort is right for a two-sentence
            # job. There is no temperature to set - it is removed on this model
            # and returns a 400, which is why verification carries determinism
            # rather than sampling settings.
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user",
                       "content": "BRIEF:\n" + json.dumps(b, indent=1, sort_keys=True)}],
        )
    except Exception as exc:
        fallback["note"] = "model call failed: %s" % type(exc).__name__
        return fallback

    if resp.stop_reason == "refusal":
        fallback["note"] = "model declined"
        return fallback

    text = "".join(blk.text for blk in resp.content if blk.type == "text")
    try:
        out = json.loads(text)
        lead, sub = str(out["lead"]).strip(), str(out["sub"]).strip()
    except Exception:
        fallback["note"] = "unparseable response"
        return fallback

    ok, reason = verify(lead + " " + sub, b)
    if not ok:
        fallback["note"] = "rejected: %s" % reason
        return fallback
    return {"lead": lead, "sub": sub, "source": "model"}
