"""Published add-on prices (enrichment/addons.json): what the page prices wifi, seats, lounges, boarding and insurance
with (2026-09-25, per Patrick). The file keeps every source; the page gets only the prices, through build.py's
__ADDONS__ token. An airline with no published price falls back to the typical price, which the page marks."""
import json
import os
from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "enrichment", "addons.json")
PRICE_KEYS = ("cents", "from", "upto", "est", "free", "not_sold")


def load() -> Dict[str, Any]:
    try:
        with open(PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def page_table(doc: Dict[str, Any] = None) -> Dict[str, Any]:
    doc = load() if doc is None else doc
    keep = lambda p: {k: v for k, v in (p or {}).items() if k in PRICE_KEYS} or None
    return {"prices": {kind: {c: {"code": c, "long": keep(r.get("long")), "short": keep(r.get("short"))}
                              for c, r in rows.items() if not c.startswith("_")}
                       for kind, rows in (doc.get("prices") or {}).items()},
            "insurance": {k: v for k, v in (doc.get("insurance") or {}).items() if k in ("low", "high", "typical")},
            "typical": {k: {"cents": v["cents"], "basis": v["basis"]} for k, v in (doc.get("typical") or {}).items()}}
