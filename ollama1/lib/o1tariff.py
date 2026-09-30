"""Electricity prices: a flat rate, or time-of-use tiers by season, day and
time, in the utility's local time (DST included).

The schedule (JSON; `sudo ollama1-power set-schedule FILE.json`, or the panel):

  {
    "currency": "USD",
    "mode": "tou",                      # "flat" or "tou"
    "flat_rate": null,                  # $/kWh in flat mode
    "timezone": "America/New_York",
    "tiers": {"on": 0.30, "off": 0.10, "discount": 0.07, "mid": null},   # $/kWh
    "weekends_off_peak": true,          # on weekends only off-peak/discount windows apply
    "holidays": {"enabled": true,       # holidays count as weekends
                 "names": ["new_year", "memorial", "independence", "labor",
                           "thanksgiving", "christmas"],
                 "observed": true,      # Saturday -> Friday, Sunday -> Monday
                 "extra": ["2026-12-24"]},
    "seasons": [
      {"name": "Summer", "from": "05-01", "to": "09-30",
       "windows": [{"tier": "on", "days": "weekdays", "start": "16:00", "end": "21:00"},
                   {"tier": "discount", "days": "every day", "start": "00:00", "end": "06:00"}]},
      {"name": "Winter", "from": "10-01", "to": "04-30",
       "windows": [{"tier": "on", "days": ["mon", "tue", "wed", "thu", "fri"],
                    "start": "07:00", "end": "10:00"}]}
    ]
  }

- days: "weekdays", "weekends", "every day", or a list of mon..sun.
- A window whose end is at or before its start runs past midnight; it
  belongs to the day (and season) it starts on. "24:00" ends at midnight.
- Where windows overlap, the most specific wins: fewer days, then the
  shorter window. Time no window covers is off-peak.
- Seasons may wrap the new year and must not overlap; a day in no season
  is off-peak all day.

Nothing here is a utility's real schedule: the presets are generic shapes
to start from ("verify against your bill").
"""
import datetime
import hashlib
import json
import re

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:            # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

TIERS = ("on", "mid", "off", "discount")
TIER_LABEL = {"on": "on-peak", "mid": "mid-peak", "off": "off-peak", "discount": "discount"}
TIER_RANK = {"on": 3, "mid": 2, "discount": 1, "off": 0}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SETS = {"weekdays": DAYS[:5], "weekends": DAYS[5:], "every day": DAYS}
HOLIDAY_NAMES = {
    "new_year": "New Year's Day", "mlk": "Martin Luther King Jr. Day", "presidents": "Washington's Birthday",
    "memorial": "Memorial Day", "juneteenth": "Juneteenth", "independence": "Independence Day",
    "labor": "Labor Day", "columbus": "Columbus Day", "veterans": "Veterans Day",
    "thanksgiving": "Thanksgiving Day", "day_after_thanksgiving": "Day after Thanksgiving",
    "christmas": "Christmas Day",
}
FEDERAL = ("new_year", "mlk", "presidents", "memorial", "juneteenth", "independence", "labor",
           "columbus", "veterans", "thanksgiving", "christmas")
CURRENCY_SYMBOL = {"USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
MAX_RATE = 10.0

DEFAULT = {
    "currency": "USD",
    "mode": "flat",
    "flat_rate": None,
    "timezone": "America/New_York",
    "tiers": {"on": None, "off": None, "discount": None, "mid": None},
    "weekends_off_peak": True,
    "holidays": {"enabled": True, "names": list(FEDERAL), "observed": True, "extra": []},
    # typical, check your bill: switching to time-of-use starts from this
    "seasons": [{"name": "All year", "from": "01-01", "to": "12-31",
                 "windows": [{"tier": "on", "days": "weekdays", "start": "14:00", "end": "19:00"}]}],
}

PRESETS = {
    # Generic shapes only, not any utility's schedule. Verify against your bill.
    "weekdays-4pm-9pm": ("On-peak 4-9 pm weekdays", "16:00", "21:00"),
    "weekdays-2pm-7pm": ("On-peak 2-7 pm weekdays", "14:00", "19:00"),
    "weekdays-8am-8pm": ("On-peak 8 am-8 pm weekdays", "08:00", "20:00"),
}


def preset(key, base=None):
    """A schedule with one of the generic on-peak windows (rates kept from base)."""
    label, start, end = PRESETS[key]
    s = json.loads(json.dumps(base or DEFAULT))
    s["mode"] = "tou"
    s["seasons"] = [{"name": "All year", "from": "01-01", "to": "12-31",
                     "windows": [{"tier": "on", "days": "weekdays", "start": start, "end": end}]}]
    return s


# ---- validation ----------------------------------------------------------------

HHMM = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _minutes(hhmm):
    m = HHMM.match(hhmm or "")
    if not m:
        return None
    v = int(m.group(1)) * 60 + int(m.group(2))
    return v if v <= 1440 else None


def _mmdd_doy(mmdd):
    """Day of the year in a leap year (so 02-29 has a place), or None."""
    if not isinstance(mmdd, str) or not MMDD.match(mmdd):
        return None
    try:
        return datetime.date(2024, int(mmdd[:2]), int(mmdd[3:])).timetuple().tm_yday
    except ValueError:
        return None


def _rate(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= MAX_RATE)


def day_set(days):
    if isinstance(days, str):
        return DAY_SETS.get(days)
    if isinstance(days, list) and days and all(d in DAYS for d in days) and len(set(days)) == len(days):
        return tuple(d for d in DAYS if d in days)
    return None


def validate(s):
    """(clean schedule, [errors]). Strict: unknown keys are errors, so a typo
    can't silently change what something costs. Never raises."""
    try:
        return _validate(s)
    except Exception:
        return None, ["the schedule couldn't be read (unexpected values)"]


def _validate(s):
    errs = []
    if not isinstance(s, dict):
        return None, ["the schedule must be a JSON object"]
    allowed = set(DEFAULT) | {"note"}
    for k in s:
        if k not in allowed:
            errs.append("unknown setting %r" % k)
    out = json.loads(json.dumps(DEFAULT))
    out.update({k: v for k, v in s.items() if k in allowed})
    if "note" in out and not (isinstance(out["note"], str) and len(out["note"]) <= 200):
        errs.append("note must be text (up to 200 characters)")
    cur = out.get("currency")
    if not (isinstance(cur, str) and re.match(r"^[A-Z]{3}$", cur)):
        errs.append("currency must be a 3-letter code like USD")
    if not isinstance(out.get("mode"), str) or out["mode"] not in ("flat", "tou"):
        errs.append('mode must be "flat" or "tou"')
    if not _rate(out.get("flat_rate")):
        errs.append("flat_rate must be a price per kWh between 0 and %g, or null" % MAX_RATE)
    tz = out.get("timezone")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError, KeyError, OSError):
        errs.append("unknown timezone %r" % (tz,))
    tiers = out.get("tiers")
    if not isinstance(tiers, dict) or any(k not in TIERS for k in tiers):
        errs.append("tiers may only be on, mid, off and discount")
        tiers = {}
    for k in TIERS:
        tiers.setdefault(k, None)
        if not _rate(tiers[k]):
            errs.append("the %s price must be between 0 and %g, or null" % (TIER_LABEL[k], MAX_RATE))
    out["tiers"] = {k: tiers[k] for k in TIERS}
    if not isinstance(out.get("weekends_off_peak"), bool):
        errs.append("weekends_off_peak must be true or false")
    h = out.get("holidays")
    if not isinstance(h, dict) or any(k not in ("enabled", "names", "observed", "extra") for k in h):
        errs.append("holidays takes enabled, names, observed and extra")
    else:
        h = dict(DEFAULT["holidays"], **h)
        if not isinstance(h["enabled"], bool) or not isinstance(h["observed"], bool):
            errs.append("holidays.enabled and holidays.observed must be true or false")
        if not isinstance(h["names"], list) or any(not isinstance(n, str) or n not in HOLIDAY_NAMES
                                                   for n in h["names"]):
            errs.append("holidays.names may only hold: " + ", ".join(HOLIDAY_NAMES))
        if not isinstance(h["extra"], list) or len(h["extra"]) > 100 or any(
                not (isinstance(d, str) and DATE.match(d)) for d in h["extra"]):
            errs.append("holidays.extra must be dates like 2026-12-24")
        else:
            for d in h["extra"]:
                try:
                    datetime.date.fromisoformat(d)
                except ValueError:
                    errs.append("holidays.extra: %s isn't a date" % d)
        out["holidays"] = h
    seasons = out.get("seasons")
    if not isinstance(seasons, list) or not 1 <= len(seasons) <= 8:
        errs.append("seasons must be a list of 1 to 8 seasons")
        seasons = []
    used = set()
    owner = {}
    for i, se in enumerate(seasons):
        where = "season %d" % (i + 1)
        if not isinstance(se, dict) or any(k not in ("name", "from", "to", "windows") for k in se):
            errs.append("%s: takes name, from, to and windows" % where)
            continue
        name = se.get("name")
        if not isinstance(name, str) or not 0 < len(name) <= 40:
            errs.append("%s: needs a name (up to 40 characters)" % where)
        else:
            where = "%s (%s)" % (where, name)
        a, b = _mmdd_doy(se.get("from")), _mmdd_doy(se.get("to"))
        if a is None or b is None:
            errs.append("%s: from and to must be dates like 05-01" % where)
        else:
            span = range(a, b + 1) if a <= b else list(range(a, 367)) + list(range(1, b + 1))
            for doy in span:
                if doy in owner:
                    errs.append("%s overlaps %s" % (where, owner[doy]))
                    break
                owner[doy] = where
        wins = se.get("windows")
        if not isinstance(wins, list) or len(wins) > 16:
            errs.append("%s: windows must be a list (up to 16)" % where)
            continue
        for j, w in enumerate(wins):
            ww = "%s, window %d" % (where, j + 1)
            if not isinstance(w, dict) or set(w) != {"tier", "days", "start", "end"}:
                errs.append("%s: needs exactly tier, days, start and end" % ww)
                continue
            if not isinstance(w["tier"], str) or w["tier"] not in TIERS:
                errs.append("%s: tier must be on, mid, off or discount" % ww)
            else:
                used.add(w["tier"])
            if day_set(w["days"]) is None:
                errs.append('%s: days must be "weekdays", "weekends", "every day" or a list of mon..sun' % ww)
            s0 = _minutes(w["start"]) if isinstance(w["start"], str) else None
            e0 = _minutes(w["end"]) if isinstance(w["end"], str) else None
            if s0 is None or s0 >= 1440:
                errs.append("%s: start must be HH:MM (00:00-23:59)" % ww)
            elif e0 is None:
                errs.append("%s: end must be HH:MM (00:00-24:00)" % ww)
            elif s0 == e0:
                errs.append("%s: starts and ends at the same time" % ww)
    if out.get("mode") == "tou":
        for t in sorted(used | {"off"}):
            if out["tiers"].get(t) is None:
                errs.append("time-of-use needs a %s price" % TIER_LABEL[t])
    return (None if errs else out), errs


def schedule_id(s):
    return hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()[:12]


# ---- holidays --------------------------------------------------------------------

def _nth(year, month, weekday, n):
    """The n-th weekday (0 = Monday) of a month; n = -1 for the last."""
    if n > 0:
        d = datetime.date(year, month, 1)
        d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    d = datetime.date(year + (month == 12), month % 12 + 1, 1) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def holiday_dates(year, names, observed=True):
    """{date: name} for the named holidays that fall in `year` (an observed
    New Year's Day can fall on 31 December of the year before)."""
    out = {}
    for y in (year, year + 1):
        fixed = {"new_year": (1, 1), "juneteenth": (6, 19), "independence": (7, 4),
                 "veterans": (11, 11), "christmas": (12, 25)}
        for n in names:
            if n in fixed:
                d = datetime.date(y, *fixed[n])
                d = _observed(d) if observed else d
            elif y != year:
                continue
            elif n == "mlk":
                d = _nth(y, 1, 0, 3)
            elif n == "presidents":
                d = _nth(y, 2, 0, 3)
            elif n == "memorial":
                d = _nth(y, 5, 0, -1)
            elif n == "labor":
                d = _nth(y, 9, 0, 1)
            elif n == "columbus":
                d = _nth(y, 10, 0, 2)
            elif n == "thanksgiving":
                d = _nth(y, 11, 3, 4)
            elif n == "day_after_thanksgiving":
                d = _nth(y, 11, 3, 4) + datetime.timedelta(days=1)
            else:
                continue
            if d.year == year:
                out[d] = HOLIDAY_NAMES[n]
    return out


# ---- the price of a minute -----------------------------------------------------------

class Tariff:
    """Answers: which tier and price applies at a moment. Day maps (1440
    tiers per local date) are cached, so pricing a month is cheap."""

    def __init__(self, schedule):
        clean, errs = validate(schedule)
        if errs:
            raise ValueError("; ".join(errs))
        self.s = clean
        self.tz = ZoneInfo(clean["timezone"])
        self.maps = {}
        self.hol = {}
        self.seasons = []
        for se in clean["seasons"]:
            self.seasons.append((_mmdd_doy(se["from"]), _mmdd_doy(se["to"]), se))

    # -- calendar
    def holiday(self, d):
        h = self.s["holidays"]
        if not h["enabled"]:
            return None
        if d.year not in self.hol:
            days = holiday_dates(d.year, h["names"], h["observed"])
            for x in h["extra"]:
                xd = datetime.date.fromisoformat(x)
                if xd.year == d.year:
                    days[xd] = "Holiday"
            self.hol[d.year] = days
        return self.hol[d.year].get(d)

    def season(self, d):
        doy = datetime.date(2024, d.month, d.day).timetuple().tm_yday
        for a, b, se in self.seasons:
            if (a <= doy <= b) if a <= b else (doy >= a or doy <= b):
                return se
        return None

    def weekend_like(self, d):
        return (self.s["weekends_off_peak"] and d.weekday() >= 5) or self.holiday(d) is not None

    def _applies(self, w, d):
        if DAYS[d.weekday()] not in day_set(w["days"]):
            return False
        if w["tier"] in ("on", "mid") and self.weekend_like(d):
            return False
        return True

    def day_map(self, d):
        """1440 tiers for the local date d."""
        if self.s["mode"] == "flat":
            return None
        m = self.maps.get(d)
        if m is not None:
            return m
        best = [None] * 1440     # (key, tier); lower key = more specific
        for day, today in ((d, True), (d - datetime.timedelta(days=1), False)):
            se = self.season(day)
            if not se:
                continue
            for w in se["windows"]:
                if not self._applies(w, day):
                    continue
                s0, e0 = _minutes(w["start"]), _minutes(w["end"])
                wraps = e0 <= s0
                length = (e0 - s0) % 1440 or 1440
                key = (len(day_set(w["days"])), length, -TIER_RANK[w["tier"]])
                if today:
                    span = range(s0, 1440 if wraps else e0)
                elif wraps:
                    span = range(0, e0)
                else:
                    continue
                for i in span:
                    if best[i] is None or key < best[i][0]:
                        best[i] = (key, w["tier"])
        m = [b[1] if b else "off" for b in best]
        if len(self.maps) > 800:
            self.maps.clear()
        self.maps[d] = m
        return m

    def local(self, t):
        return datetime.datetime.fromtimestamp(t, self.tz)

    def tier_at(self, t):
        """The tier at UTC epoch second t ("flat" in flat mode)."""
        if self.s["mode"] == "flat":
            return "flat"
        lt = self.local(t)
        return self.day_map(lt.date())[lt.hour * 60 + lt.minute]

    def price(self, tier):
        if tier == "flat":
            return self.s["flat_rate"]
        return self.s["tiers"].get(tier)

    def price_at(self, t):
        return self.price(self.tier_at(t))

    def has_prices(self):
        if self.s["mode"] == "flat":
            return self.s["flat_rate"] is not None
        return True       # validate() insists on every tier in use

    def tiers_for_minutes(self, minutes):
        """Tier for each UTC minute epoch in a list, localizing once per
        half hour when the UTC offset holds across it (checked, not assumed;
        otherwise minute by minute)."""
        if self.s["mode"] == "flat":
            return ["flat"] * len(minutes)
        out = []
        cache_h, base = None, None
        for t in minutes:
            h = t - t % 1800
            if h != cache_h:
                cache_h = h
                lt = self.local(h)
                end = self.local(h + 1799)
                base = lt if end.utcoffset() == lt.utcoffset() else None
            if base is not None:
                lt = base + datetime.timedelta(seconds=t - cache_h)
            else:
                lt = self.local(t)
            out.append(self.day_map(lt.date())[lt.hour * 60 + lt.minute])
        return out

    def next_change(self, t, horizon_days=9):
        """(tier now, UTC epoch of the next change or None)."""
        now = self.tier_at(t)
        if self.s["mode"] == "flat":
            return now, None
        t0 = int(t) - int(t) % 60 + 60
        for k in range(horizon_days * 1440):
            tt = t0 + 60 * k
            if self.tier_at(tt) != now:
                return now, tt
        return now, None

    def badge(self, t):
        tier, until = self.next_change(t)
        p = self.price(tier)
        out = {"tier": tier, "label": "flat rate" if tier == "flat" else TIER_LABEL[tier], "price": p,
               "currency": self.s["currency"], "symbol": CURRENCY_SYMBOL.get(self.s["currency"], self.s["currency"] + " ")}
        if until:
            lu, ln = self.local(until), self.local(t)
            out["until"] = until
            same = lu.date() == ln.date()
            out["until_text"] = lu.strftime("%H:%M") if same else lu.strftime("%a %H:%M")
            out["text"] = "%s until %s" % (out["label"], out["until_text"])
        else:
            out["text"] = out["label"]
        return out
