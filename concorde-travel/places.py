#!/usr/bin/env python3
"""Turning what somebody typed into something a flight API accepts.

    python3 concorde-travel/places.py london
    python3 concorde-travel/places.py "new york" paris

Duffel takes an IATA airport code (LHR) or a city code (LON, which covers
Heathrow, Gatwick, City, Stansted and Luton at once). A person types "London".

WHY A LOCAL TABLE AND NOT A LOOKUP API. Duffel has a Places endpoint that would
do this properly, and it should be wired in eventually. But resolving a
destination must not depend on the network: a search that fails because an
autocomplete call timed out is a worse failure than one that says "I don't know
that city, try an airport code". So this answers offline for the places people
actually fly to, passes a code straight through, and leaves the long tail to
the user rather than guessing.

CITY CODES BEAT AIRPORT CODES for the destination. Someone flying to London
wants all five airports considered, not just the one they happened to name, and
the scorer is the thing that should be deciding between them - that is the
entire product. So a bare city name resolves to the metro code where one exists.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# city text -> code. Metro codes where a city has several airports, otherwise
# the airport itself. Ordered roughly by how often people fly there.
CITIES: Dict[str, str] = {}


def _add(code: str, *names: str) -> None:
    for n in names:
        CITIES[n.lower()] = code


# North America
_add("NYC", "new york", "new york city", "nyc", "manhattan", "brooklyn", "queens", "the bronx", "bronx", "staten island",
     # neighbourhoods people actually type as the last part of an address; still a table, not a guess
     "bushwick", "williamsburg", "greenpoint", "park slope", "bed-stuy", "bedford-stuyvesant", "crown heights", "flatbush",
     "astoria", "long island city", "flushing", "jackson heights", "harlem", "upper west side", "upper east side",
     "lower east side", "east village", "west village", "chelsea", "tribeca", "soho", "midtown", "financial district")
_add("JFK", "jfk", "kennedy"); _add("EWR", "newark", "ewr"); _add("LGA", "laguardia", "lga")
_add("LAX", "los angeles", "la", "lax"); _add("SFO", "san francisco", "sfo")
_add("CHI", "chicago"); _add("ORD", "o'hare", "ohare", "ord")
_add("WAS", "washington", "washington dc", "dc"); _add("IAD", "dulles", "iad")
_add("BOS", "boston"); _add("MIA", "miami"); _add("ATL", "atlanta")
_add("SEA", "seattle"); _add("DEN", "denver"); _add("DFW", "dallas")
_add("IAH", "houston"); _add("PHX", "phoenix"); _add("LAS", "las vegas", "vegas")
_add("MCO", "orlando"); _add("PHL", "philadelphia"); _add("SAN", "san diego")
_add("YTO", "toronto"); _add("YVR", "vancouver"); _add("YMQ", "montreal")
_add("MEX", "mexico city"); _add("CUN", "cancun")

# Europe
_add("LON", "london"); _add("LHR", "heathrow", "lhr"); _add("LGW", "gatwick", "lgw")
_add("PAR", "paris"); _add("CDG", "charles de gaulle", "cdg"); _add("ORY", "orly")
_add("AMS", "amsterdam"); _add("FRA", "frankfurt"); _add("MUC", "munich", "muenchen")
_add("BER", "berlin"); _add("DUS", "dusseldorf", "duesseldorf"); _add("HAM", "hamburg")
_add("MAD", "madrid"); _add("BCN", "barcelona"); _add("AGP", "malaga")
_add("ROM", "rome", "roma"); _add("FCO", "fiumicino", "fco"); _add("MIL", "milan", "milano")
_add("VCE", "venice", "venezia"); _add("NAP", "naples"); _add("FLR", "florence", "firenze")
_add("LIS", "lisbon", "lisboa"); _add("OPO", "porto")
_add("DUB", "dublin"); _add("EDI", "edinburgh"); _add("MAN", "manchester")
_add("ZRH", "zurich", "zuerich"); _add("GVA", "geneva", "geneve"); _add("VIE", "vienna", "wien")
_add("BRU", "brussels", "bruxelles"); _add("CPH", "copenhagen", "kobenhavn")
_add("STO", "stockholm"); _add("ARN", "arlanda", "arn"); _add("OSL", "oslo")
_add("HEL", "helsinki"); _add("KEF", "reykjavik", "keflavik")
_add("PRG", "prague", "praha"); _add("WAW", "warsaw", "warszawa"); _add("KRK", "krakow")
_add("BUD", "budapest"); _add("ATH", "athens"); _add("IST", "istanbul")
_add("OTP", "bucharest"); _add("SOF", "sofia"); _add("ZAG", "zagreb")
_add("SVQ", "seville", "sevilla"); _add("VLC", "valencia"); _add("PMI", "palma", "mallorca")
_add("NCE", "nice"); _add("LYS", "lyon"); _add("MRS", "marseille"); _add("TLS", "toulouse")

# Middle East, Africa
_add("DXB", "dubai"); _add("AUH", "abu dhabi"); _add("DOH", "doha")
_add("TLV", "tel aviv"); _add("AMM", "amman"); _add("RUH", "riyadh"); _add("JED", "jeddah")
_add("CAI", "cairo"); _add("CMN", "casablanca"); _add("RAK", "marrakech", "marrakesh")
_add("JNB", "johannesburg"); _add("CPT", "cape town"); _add("NBO", "nairobi")
_add("LOS", "lagos"); _add("ACC", "accra"); _add("ADD", "addis ababa")

# Asia, Oceania
_add("TYO", "tokyo"); _add("NRT", "narita"); _add("HND", "haneda")
_add("OSA", "osaka"); _add("SEL", "seoul"); _add("ICN", "incheon")
_add("HKG", "hong kong"); _add("SIN", "singapore"); _add("BKK", "bangkok")
_add("KUL", "kuala lumpur"); _add("CGK", "jakarta"); _add("MNL", "manila")
_add("SGN", "ho chi minh city", "saigon"); _add("HAN", "hanoi")
_add("BJS", "beijing"); _add("SHA", "shanghai"); _add("CAN", "guangzhou")
_add("TPE", "taipei"); _add("DEL", "delhi", "new delhi"); _add("BOM", "mumbai", "bombay")
_add("BLR", "bangalore", "bengaluru"); _add("MAA", "chennai"); _add("HYD", "hyderabad")
_add("CMB", "colombo"); _add("KTM", "kathmandu"); _add("DAC", "dhaka")
_add("SYD", "sydney"); _add("MEL", "melbourne"); _add("BNE", "brisbane")
_add("PER", "perth"); _add("AKL", "auckland"); _add("CHC", "christchurch")

# South America
_add("SAO", "sao paulo"); _add("GRU", "guarulhos", "gru"); _add("RIO", "rio de janeiro", "rio")
_add("BUE", "buenos aires"); _add("SCL", "santiago"); _add("LIM", "lima")
_add("BOG", "bogota"); _add("UIO", "quito"); _add("MVD", "montevideo")

# Which airports a metro code actually covers. Needed to answer "did we search
# what they asked for?" - LON and LHR are the same request, and a prefix test
# saying otherwise ("does LHR start with LO") is the kind of near-miss heuristic
# that looks fine until it tells somebody their London search was not a London
# search. Only the metros this module emits need a row.
METRO: Dict[str, set] = {
    "NYC": {"JFK", "EWR", "LGA"},
    "LON": {"LHR", "LGW", "STN", "LTN", "LCY", "SEN"},
    "PAR": {"CDG", "ORY", "BVA"},
    "MIL": {"MXP", "LIN", "BGY"},
    "ROM": {"FCO", "CIA"},
    "TYO": {"NRT", "HND"},
    "OSA": {"KIX", "ITM"},
    "SEL": {"ICN", "GMP"},
    "CHI": {"ORD", "MDW"},
    "WAS": {"IAD", "DCA", "BWI"},
    "SAO": {"GRU", "CGH", "VCP"},
    "RIO": {"GIG", "SDU"},
    "BUE": {"EZE", "AEP"},
    "STO": {"ARN", "BMA", "NYO"},
    "BJS": {"PEK", "PKX"},
    "SHA": {"PVG", "SHA"},
    "YTO": {"YYZ", "YTZ"},
    "YMQ": {"YUL"},
    "BER": {"BER"},
    "MOW": {"SVO", "DME", "VKO"},
}


def airports_for(code: str) -> List[str]:
    """A metro code as the airports it stands for, sorted; an airport as itself.
    Google Flights (through SerpApi) has no metro codes: NYC gets no results,
    JFK,EWR,LGA does (2026-09-21)."""
    code = (code or "").upper()
    return sorted(METRO.get(code) or [code]) if code else []


def covers(code: str, iata: str) -> bool:
    """Is `iata` the place `code` asked for? A metro covers its airports."""
    code, iata = (code or "").upper(), (iata or "").upper()
    if not code or not iata:
        return False
    return code == iata or iata in METRO.get(code, set())


CODE_RE = re.compile(r"^[A-Z]{3}$")


def _strip_postcode(part: str) -> str:
    """'brooklyn 11237' -> 'brooklyn', 'london ec2a' -> 'london'."""
    kept = [w for w in part.split() if not any(ch.isdigit() for ch in w)]
    return " ".join(kept).strip()


def resolve(text: str) -> Tuple[Optional[str], str, Optional[str]]:
    """(code, how, problem). `how` is 'code' | 'city' | 'unknown'.

    Never guesses. An unrecognised place comes back as a problem the interface
    can put in front of the user, because searching the wrong continent is a
    worse outcome than being asked to type three letters."""
    raw = (text or "").strip()
    if not raw:
        return None, "unknown", "Where to? Type a city or an airport code."
    if CODE_RE.match(raw.upper()) and len(raw) == 3:
        return raw.upper(), "code", None
    # "Honolulu (HNL)": what the autocomplete puts in the field, the code carried in brackets
    m = re.search(r"\(([A-Za-z]{3})\)\s*$", raw)
    if m:
        return m.group(1).upper(), "code", None

    key = raw.lower()
    if key in CITIES:
        return CITIES[key], "city", None

    # Real input is an ADDRESS, not a city: "Wyckoff Ave & Myrtle Ave, Bushwick,
    # Brooklyn 11237" and "Shoreditch, London EC2A". Walk the comma-separated
    # parts from the END, because the city is the last thing in an address and
    # the street is the first, and strip postcodes - a token carrying a digit is
    # never a city name, and "london ec2a" matching nothing is how the most
    # normal input in the world failed.
    parts = [p.strip() for p in re.split(r"[,/(]", key) if p.strip()]
    for part in reversed(parts):
        for cand in (part, _strip_postcode(part)):
            if cand and cand in CITIES:
                return CITIES[cand], "city", None

    near = suggest(raw)
    hint = (" Did you mean %s?" % ", ".join(near)) if near else ""
    return None, "unknown", ("We couldn't find \"%s\". Try a city or a 3-letter "
                             "airport code, like LHR.%s" % (raw, hint))


def suggest(text: str, limit: int = 3) -> List[str]:
    """Cheap prefix/substring suggestions - no fuzzy library, no dependency."""
    q = (text or "").strip().lower()
    if len(q) < 2:
        return []
    starts = sorted({n.title() for n in CITIES if n.startswith(q)})
    if starts:
        return starts[:limit]
    return sorted({n.title() for n in CITIES if q in n})[:limit]


def search(text: str, limit: int = 6) -> List[dict]:
    """Autocomplete over the table: names starting with what was typed first,
    then names containing it, one row per code, the proper name first and the
    matched spelling shown when it differs. Offline, instant."""
    q = (text or "").strip().lower()
    if len(q) < 2:
        return []
    seen, out = set(), []
    proper_of = {}
    for n, c in CITIES.items():
        proper_of.setdefault(c, n if n != c.lower() else None)
        if proper_of[c] is None and n != c.lower():
            proper_of[c] = n
    # the proper name starting with it, then a spelling or neighbourhood starting with it, then anything containing it
    for pick in (lambda n, c: n == proper_of.get(c) and n.startswith(q), lambda n, c: n.startswith(q), lambda n, c: q in n):
        for n, c in CITIES.items():
            if c in seen or not pick(n, c):
                continue
            seen.add(c)
            proper = label_for(c).rsplit(" (", 1)[0]
            is_airport = any(a == c.lower() for a, cc in CITIES.items() if cc == c) and c not in METROS
            out.append({"value": "%s (%s)" % (proper, c), "label": proper, "code": c, "kind": "airport" if is_airport else "city",
                        "sub": " · ".join(x for x in ((n.title() if n.title() != proper and n != c.lower() else ""), c) if x)})
            if len(out) >= limit:
                return out
    return out


METROS = {"NYC", "LON", "PAR", "CHI", "WAS", "YTO", "YMQ", "MIL", "ROM", "TYO", "OSA", "SAO", "RIO", "BUE", "MOW", "STO", "BER"}


def label_for(code: str) -> str:
    """A human name for a code, for echoing a search back at someone."""
    code = (code or "").upper()
    # the first name added for a code is its proper name; the rest are spellings and neighbourhoods
    names = [n for n, c in CITIES.items() if c == code and n != code.lower()]
    if not names:
        return code
    return "%s (%s)" % (names[0].title(), code)


if __name__ == "__main__":
    import sys
    for q in (sys.argv[1:] or ["london", "LHR", "new york", "Paris, France", "Narnia"]):
        code, how, problem = resolve(q)
        print("%-18s -> %-5s %-8s %s" % (q, code or "-", how, problem or label_for(code)))
