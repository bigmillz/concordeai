"""Power and electricity cost: the price schedule (flat and time-of-use,
seasons, weekends, holidays, DST, windows past midnight, precedence), the
per-minute energy store (gaps stay unknown, sleep counts 3 W, minute
boundaries split), the RAPL counter wrap, the four smart plugs (fake ones on
127.0.0.1), the LAN-only rule, the money math and the CLI.

None of the schedules here is a real utility's: they are made-up shapes
that exercise the same rules."""
import base64
import datetime
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import o1test_util as U
import o1power as P
import o1tariff as T
from o1common import Paths

NY = T.ZoneInfo("America/New_York")


def at(y, mo, d, h=0, mi=0):
    """UTC epoch of a New York wall-clock time."""
    return int(datetime.datetime(y, mo, d, h, mi, tzinfo=NY).timestamp())


def sched(**kw):
    s = json.loads(json.dumps(T.DEFAULT))
    s.update(kw)
    return s


def tou(seasons, tiers=None, **kw):
    return sched(mode="tou", tiers=dict({"on": 0.30, "mid": 0.20, "off": 0.10, "discount": 0.05}, **(tiers or {})),
                 seasons=seasons, **kw)


ALL_YEAR = lambda *wins: [{"name": "All year", "from": "01-01", "to": "12-31", "windows": list(wins)}]  # noqa: E731


def win(tier, days, start, end):
    return {"tier": tier, "days": days, "start": start, "end": end}


# a made-up two-season schedule with the season changes at midnight on 1 May and 1 October
TWO_SEASONS = [
    {"name": "Warm", "from": "05-01", "to": "09-30",
     "windows": [win("on", "weekdays", "17:00", "20:00"), win("discount", "every day", "00:00", "04:00")]},
    {"name": "Cool", "from": "10-01", "to": "04-30",
     "windows": [win("on", "weekdays", "06:00", "09:00"), win("discount", "every day", "00:00", "04:00"),
                 win("discount", "every day", "12:00", "15:00")]},
]


class TestValidation(unittest.TestCase):
    def test_default_is_flat_with_no_price(self):
        clean, errs = T.validate(T.DEFAULT)
        self.assertEqual(errs, [])
        self.assertEqual(clean["mode"], "flat")
        self.assertIsNone(clean["flat_rate"])
        self.assertEqual(clean["timezone"], "America/New_York")
        w = clean["seasons"][0]["windows"]
        self.assertEqual(w, [win("on", "weekdays", "14:00", "19:00")])      # typical, check your bill
        self.assertFalse(T.Tariff(clean).has_prices())

    def test_good_schedules(self):
        for s in (sched(flat_rate=0.15), tou(TWO_SEASONS), tou(ALL_YEAR(win("on", ["sat", "sun"], "22:00", "06:00")))):
            self.assertEqual(T.validate(s)[1], [], s)
        for k in T.PRESETS:
            p = T.preset(k)
            p["tiers"].update(on=0.3, off=0.1)
            self.assertEqual(T.validate(p)[1], [], k)

    def test_errors(self):
        bad = [
            (sched(flat_rate=-1), "flat_rate"),
            (sched(flat_rate=True), "flat_rate"),
            (sched(currency="dollars"), "currency"),
            (sched(mode="cheap"), "mode"),
            (sched(timezone="Mars/Olympus"), "timezone"),
            (sched(surprise=1), "unknown setting"),
            (tou(ALL_YEAR(win("on", "weekdays", "16:00", "21:00")), tiers={"on": None}), "on-peak price"),
            (tou(ALL_YEAR(win("discount", "weekdays", "01:00", "03:00")), tiers={"discount": None}), "discount price"),
            (tou(ALL_YEAR(win("peak", "weekdays", "16:00", "21:00"))), "tier must be"),
            (tou(ALL_YEAR(win("on", "workdays", "16:00", "21:00"))), "days must be"),
            (tou(ALL_YEAR(win("on", ["mon", "mon"], "16:00", "21:00"))), "days must be"),
            (tou(ALL_YEAR(win("on", "weekdays", "25:00", "21:00"))), "start must be"),
            (tou(ALL_YEAR(win("on", "weekdays", "16:00", "21:60"))), "end must be"),
            (tou(ALL_YEAR(win("on", "weekdays", "16:00", "16:00"))), "same time"),
            (tou(ALL_YEAR({"tier": "on", "days": "weekdays", "start": "16:00"})), "exactly tier"),
            (tou([{"name": "A", "from": "01-01", "to": "06-30", "windows": []},
                  {"name": "B", "from": "06-01", "to": "12-31", "windows": []}]), "overlaps"),
            (tou([{"name": "A", "from": "02-30", "to": "06-30", "windows": []}]), "dates like"),
            (tou([]), "1 to 8 seasons"),
            (sched(holidays={"enabled": True, "names": ["easter"]}), "holidays.names"),
            (sched(holidays={"enabled": True, "extra": ["2026-02-30"]}), "isn't a date"),
            (sched(tiers={"super": 1}), "tiers may only"),
        ]
        for s, want in bad:
            clean, errs = T.validate(s)
            self.assertIsNone(clean, want)
            self.assertTrue(any(want in e for e in errs), (want, errs))

    def test_full_day_and_midnight_ends(self):
        for a, b in (("00:00", "24:00"), ("22:00", "24:00"), ("22:00", "00:00"), ("22:00", "06:00")):
            self.assertEqual(T.validate(tou(ALL_YEAR(win("on", "every day", a, b))))[1], [], (a, b))


class TestTimeOfUse(unittest.TestCase):
    def tier(self, s, *when):
        return T.Tariff(s).tier_at(at(*when))

    def test_weekday_weekend_and_weekend_discount(self):
        s = tou(TWO_SEASONS)
        self.assertEqual(self.tier(s, 2026, 7, 15, 17, 30), "on")          # Wednesday
        self.assertEqual(self.tier(s, 2026, 7, 18, 17, 30), "off")         # Saturday: weekdays window
        self.assertEqual(self.tier(s, 2026, 7, 18, 2, 0), "discount")      # Saturday: the every-day discount
        # with weekends off-peak, even an every-day on-peak window skips Sunday
        s2 = tou(ALL_YEAR(win("on", "every day", "16:00", "21:00"), win("discount", "every day", "01:00", "05:00")))
        self.assertEqual(self.tier(s2, 2026, 7, 19, 17, 0), "off")
        self.assertEqual(self.tier(s2, 2026, 7, 19, 2, 0), "discount")
        s2["weekends_off_peak"] = False
        self.assertEqual(self.tier(s2, 2026, 7, 19, 17, 0), "on")

    def test_three_tiers_and_several_windows_a_season(self):
        s = tou(TWO_SEASONS)
        self.assertEqual(self.tier(s, 2026, 1, 14, 7, 0), "on")            # Wednesday, cool season
        self.assertEqual(self.tier(s, 2026, 1, 14, 3, 59), "discount")
        self.assertEqual(self.tier(s, 2026, 1, 14, 4, 0), "off")
        self.assertEqual(self.tier(s, 2026, 1, 14, 13, 0), "discount")
        self.assertEqual(self.tier(s, 2026, 1, 14, 15, 0), "off")
        t = T.Tariff(s)
        self.assertEqual([t.price(x) for x in ("on", "off", "discount")], [0.30, 0.10, 0.05])

    def test_season_changes_at_midnight(self):
        s = tou(TWO_SEASONS)
        # Thu 30 Apr is cool, Fri 1 May is warm
        self.assertEqual(self.tier(s, 2026, 4, 30, 6, 30), "on")
        self.assertEqual(self.tier(s, 2026, 4, 30, 23, 59), "off")
        self.assertEqual(self.tier(s, 2026, 5, 1, 0, 0), "discount")
        self.assertEqual(self.tier(s, 2026, 5, 1, 6, 30), "off")
        self.assertEqual(self.tier(s, 2026, 5, 1, 17, 30), "on")
        # Wed 30 Sep is warm, Thu 1 Oct is cool
        self.assertEqual(self.tier(s, 2026, 9, 30, 17, 30), "on")
        self.assertEqual(self.tier(s, 2026, 9, 30, 23, 59), "off")
        self.assertEqual(self.tier(s, 2026, 10, 1, 6, 30), "on")
        self.assertEqual(self.tier(s, 2026, 10, 1, 13, 0), "discount")
        self.assertEqual(self.tier(s, 2026, 10, 1, 17, 30), "off")

    def test_a_window_past_midnight_belongs_to_its_start_day(self):
        s = tou(ALL_YEAR(win("on", "weekdays", "22:00", "06:00")))
        self.assertEqual(self.tier(s, 2026, 7, 17, 23, 0), "on")          # Friday night
        self.assertEqual(self.tier(s, 2026, 7, 18, 3, 0), "on")           # Saturday 3 am: Friday's window
        self.assertEqual(self.tier(s, 2026, 7, 18, 6, 0), "off")
        self.assertEqual(self.tier(s, 2026, 7, 18, 23, 0), "off")         # Saturday's own: not a weekday
        self.assertEqual(self.tier(s, 2026, 7, 20, 2, 0), "off")          # Monday 2 am: Sunday's window
        self.assertEqual(self.tier(s, 2026, 7, 21, 2, 0), "on")           # Tuesday 2 am: Monday's
        # and across a season change: it follows the season it started in
        s = tou([{"name": "Cool", "from": "10-01", "to": "04-30", "windows": []},
                 {"name": "Warm", "from": "05-01", "to": "09-30",
                  "windows": [win("discount", "every day", "23:00", "02:00")]}])
        self.assertEqual(self.tier(s, 2026, 5, 1, 1, 0), "off")           # 30 Apr's (cool) night
        self.assertEqual(self.tier(s, 2026, 5, 2, 1, 0), "discount")
        self.assertEqual(self.tier(s, 2026, 10, 1, 1, 0), "discount")     # 30 Sep's (warm) night

    def test_most_specific_wins(self):
        s = tou(ALL_YEAR(win("discount", "every day", "00:00", "24:00"), win("on", "weekdays", "16:00", "21:00"),
                         win("mid", "weekdays", "18:00", "19:00")))
        self.assertEqual(self.tier(s, 2026, 7, 15, 10, 0), "discount")
        self.assertEqual(self.tier(s, 2026, 7, 15, 17, 0), "on")          # fewer days beats every day
        self.assertEqual(self.tier(s, 2026, 7, 15, 18, 30), "mid")        # same days: the shorter one
        self.assertEqual(self.tier(s, 2026, 7, 18, 17, 0), "discount")
        # the order they're listed in doesn't matter
        s = tou(ALL_YEAR(win("mid", "weekdays", "18:00", "19:00"), win("on", "weekdays", "16:00", "21:00"),
                         win("discount", "every day", "00:00", "24:00")))
        self.assertEqual(self.tier(s, 2026, 7, 15, 17, 0), "on")
        self.assertEqual(self.tier(s, 2026, 7, 15, 18, 30), "mid")
        self.assertEqual(self.tier(s, 2026, 7, 15, 10, 0), "discount")
        # custom days: two days beat five
        s = tou(ALL_YEAR(win("on", "weekdays", "16:00", "21:00"), win("off", ["mon", "fri"], "16:00", "21:00")))
        self.assertEqual(self.tier(s, 2026, 7, 17, 17, 0), "off")
        self.assertEqual(self.tier(s, 2026, 7, 16, 17, 0), "on")

    def test_uncovered_is_off_peak(self):
        s = tou([{"name": "Summer only", "from": "06-01", "to": "08-31",
                  "windows": [win("on", "weekdays", "16:00", "21:00")]}])
        self.assertEqual(self.tier(s, 2026, 7, 15, 17, 0), "on")
        self.assertEqual(self.tier(s, 2026, 1, 14, 17, 0), "off")          # no season

    def test_holidays(self):
        s = tou(ALL_YEAR(win("on", "weekdays", "16:00", "21:00"), win("discount", "every day", "01:00", "05:00")))
        self.assertEqual(self.tier(s, 2026, 11, 25, 17, 0), "on")          # Wednesday
        self.assertEqual(self.tier(s, 2026, 11, 26, 17, 0), "off")         # Thanksgiving
        self.assertEqual(self.tier(s, 2026, 11, 26, 2, 0), "discount")     # like a weekend: discount applies
        self.assertEqual(self.tier(s, 2026, 7, 3, 17, 0), "off")           # 4 July 2026 is a Saturday
        self.assertEqual(self.tier(s, 2027, 7, 5, 17, 0), "off")           # 4 July 2027 is a Sunday
        self.assertEqual(self.tier(s, 2027, 12, 31, 17, 0), "off")         # New Year 2028 is a Saturday
        s["holidays"]["observed"] = False
        self.assertEqual(self.tier(s, 2026, 7, 3, 17, 0), "on")
        s["holidays"] = {"enabled": True, "names": ["thanksgiving", "day_after_thanksgiving"], "observed": True,
                         "extra": ["2026-12-24"]}
        self.assertEqual(self.tier(s, 2026, 11, 27, 17, 0), "off")
        self.assertEqual(self.tier(s, 2026, 12, 24, 17, 0), "off")
        self.assertEqual(self.tier(s, 2026, 7, 3, 17, 0), "on")            # not on this list
        s["holidays"]["enabled"] = False
        self.assertEqual(self.tier(s, 2026, 11, 26, 17, 0), "on")

    def test_federal_dates(self):
        d = T.holiday_dates(2026, T.FEDERAL)
        want = {"2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
                "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25"}
        self.assertEqual({x.isoformat() for x in d}, want)
        self.assertIn(datetime.date(2027, 12, 31), T.holiday_dates(2027, ["new_year"]))
        self.assertEqual(T.holiday_dates(2028, ["new_year"]), {})

    def test_dst_days(self):
        s = tou(ALL_YEAR(win("discount", "every day", "01:00", "06:00")))
        t = T.Tariff(s)

        def local_day(y, mo, d):
            t0 = at(y, mo, d)
            t1 = int((datetime.datetime(y, mo, d, tzinfo=NY) + datetime.timedelta(days=1)).timestamp())
            return list(range(t0, t1, 60))
        spring, fall = local_day(2026, 3, 8), local_day(2026, 11, 1)
        self.assertEqual(len(spring), 23 * 60)
        self.assertEqual(len(fall), 25 * 60)
        # 01:00-06:00 on the clock: 4 real hours in spring, 6 in autumn
        self.assertEqual(t.tiers_for_minutes(spring).count("discount"), 4 * 60)
        self.assertEqual(t.tiers_for_minutes(fall).count("discount"), 6 * 60)
        # the fast path agrees with minute-by-minute across both changes
        for day in (spring, fall):
            self.assertEqual(t.tiers_for_minutes(day), [t.tier_at(x) for x in day])

    def test_a_change_off_the_half_hour(self):
        # Newfoundland changed its clocks at 00:01 local until 2011: 03:31 UTC on 5 April 1987
        s = tou(ALL_YEAR(win("discount", "every day", "00:00", "01:00")), timezone="America/St_Johns")
        t = T.Tariff(s)
        x = int(datetime.datetime(1987, 4, 5, 3, 0, tzinfo=datetime.timezone.utc).timestamp())
        mins = list(range(x, x + 2 * 3600, 60))
        self.assertEqual(t.tiers_for_minutes(mins), [t.tier_at(m) for m in mins])
        self.assertIn("discount", t.tiers_for_minutes(mins))

    def test_other_zone(self):
        s = tou(ALL_YEAR(win("on", "weekdays", "16:00", "21:00")), timezone="Asia/Kolkata")
        t = T.Tariff(s)
        ist = T.ZoneInfo("Asia/Kolkata")
        x = int(datetime.datetime(2026, 7, 15, 16, 0, tzinfo=ist).timestamp())
        self.assertEqual(t.tier_at(x), "on")
        self.assertEqual(t.tier_at(x - 60), "off")
        mins = list(range(x - 3 * 3600, x + 3 * 3600, 60))
        self.assertEqual(t.tiers_for_minutes(mins), [t.tier_at(m) for m in mins])

    def test_badge(self):
        t = T.Tariff(tou(ALL_YEAR(win("on", "weekdays", "16:00", "21:00"))))
        self.assertEqual(t.badge(at(2026, 7, 15, 15, 0))["text"], "off-peak until 16:00")
        b = t.badge(at(2026, 7, 15, 17, 0))
        self.assertEqual((b["text"], b["price"], b["symbol"]), ("on-peak until 21:00", 0.30, "$"))
        self.assertEqual(t.badge(at(2026, 7, 17, 22, 0))["text"], "off-peak until Mon 16:00")
        f = T.Tariff(sched(flat_rate=0.15))
        self.assertEqual((f.badge(at(2026, 7, 15, 17, 0))["text"], f.price_at(at(2026, 7, 15, 17, 0))),
                         ("flat rate", 0.15))


class TestEnergyAndMoney(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(P.energy_dir(), True)

    def test_minute_boundary_split(self):
        m = P.Meter()
        t = at(2026, 7, 15, 18, 59) + 50
        m.add(t, t + 20, 360.0, "plug")        # 10 s each side of 19:00
        rows = m.take_complete(t + 200)
        self.assertEqual([(r[0], r[1], r[2], r[3]) for r in rows],
                         [(t - 50, 1.0, "plug", 10), (t + 10, 1.0, "plug", 10)])
        # and each side is priced at its own minute
        tf = T.Tariff(tou(ALL_YEAR(win("on", "weekdays", "16:00", "19:00"))))
        w = P.window(tf, rows, t - 3600, t + 3600)
        self.assertAlmostEqual(w["by_tier"]["on"]["cost"], 0.001 * 0.30)
        self.assertAlmostEqual(w["by_tier"]["off"]["cost"], 0.001 * 0.10)
        self.assertAlmostEqual(w["cost"], 0.0004)

    def test_flat_cost_and_tokens(self):
        t0 = at(2026, 7, 15, 12, 0)
        rows = [(t0 + 60 * i, 1000 / 60.0, "plug", 60, 500) for i in range(60)]   # 1 kW for an hour
        w = P.window(T.Tariff(sched(flat_rate=0.15)), rows, t0, t0 + 3600)
        self.assertAlmostEqual(w["kwh"], 1.0)
        self.assertAlmostEqual(w["cost"], 0.15)
        self.assertEqual(w["tokens"], 30000)
        self.assertAlmostEqual(w["cost_per_1k_tokens"], 0.005)
        self.assertEqual(w["unknown_h"], 0)
        w = P.window(T.Tariff(sched()), rows, t0, t0 + 3600)
        self.assertIsNone(w["cost"])
        self.assertIsNone(w["cost_per_1k_tokens"])

    def test_gaps_are_unknown_not_zero(self):
        t0 = at(2026, 7, 15, 12, 0)
        rows = [(t0 + 60 * i, 100 / 60.0, "est", 60, 0) for i in range(60) if not 20 <= i < 50]
        rows.append((t0 + 60 * 20, 100 / 120.0, "est", 30, 0))     # half a minute measured
        w = P.window(T.Tariff(sched(flat_rate=0.2)), rows, t0, t0 + 3600)
        self.assertAlmostEqual(w["measured_h"], (30 * 60 + 30) / 3600.0, places=2)
        self.assertAlmostEqual(w["unknown_h"], (29 * 60 + 30) / 3600.0, places=2)
        self.assertAlmostEqual(w["kwh"], 0.1 * 30.5 / 60, places=4)
        # before the first reading isn't "unknown" either: it's before measuring began
        w = P.window(T.Tariff(sched(flat_rate=0.2)), rows, t0 - 86400, t0 + 3600, first=t0)
        self.assertAlmostEqual(w["unknown_h"], (29 * 60 + 30) / 3600.0, places=2)

    def test_tou_cost_by_tier(self):
        tf = T.Tariff(tou(TWO_SEASONS))
        t0 = at(2026, 7, 15)                    # a Wednesday, warm season
        rows = [(t0 + 60 * i, 1.0, "plug", 60, 0) for i in range(1440)]   # 60 W all day
        w = P.window(tf, rows, t0, t0 + 86400)
        self.assertAlmostEqual(w["by_tier"]["on"]["kwh"], 0.18)          # 3 h
        self.assertAlmostEqual(w["by_tier"]["discount"]["kwh"], 0.24)    # 4 h
        self.assertAlmostEqual(w["by_tier"]["off"]["kwh"], 1.02)         # 17 h
        self.assertAlmostEqual(w["cost"], 0.18 * 0.30 + 0.24 * 0.05 + 1.02 * 0.10)

    def test_projection(self):
        now = at(2026, 7, 15, 12, 0)
        rows = [(now - 60 * i, 100 / 60.0, "plug", 60, 0) for i in range(1, 28 * 1440 + 1)]   # 100 W
        p = P.projection(T.Tariff(sched(flat_rate=0.2)), rows, now)
        self.assertAlmostEqual(p["kwh"], 72.0, places=1)
        self.assertAlmostEqual(p["cost"], 14.4, places=1)
        self.assertAlmostEqual(p["basis_days"], 28.0)
        # per hour of the week: 300 W 17:00-20:00 on weekdays, 100 W otherwise
        rows = []
        for i in range(1, 28 * 1440 + 1):
            t = now - 60 * i
            lt = datetime.datetime.fromtimestamp(t, NY)
            w = 300 if lt.weekday() < 5 and 17 <= lt.hour < 20 else 100
            rows.append((t, w / 60.0, "plug", 60, 0))
        tf = T.Tariff(tou(TWO_SEASONS))
        p = P.projection(tf, rows, now)
        self.assertAlmostEqual(p["by_tier"]["on"]["kwh"], 0.9 * 22, delta=0.9 * 2)   # ~22 weekdays in 30 days
        self.assertIsNone(P.projection(tf, [], now))

    def test_store_round_trip_and_prune(self):
        t0 = at(2026, 7, 15, 23, 58)
        rows = [(t0 + 60 * i, 1.5, "plug", 60, 3) for i in range(6)]    # crosses the UTC day? (NY 23:58 = 03:58 UTC)
        P.append_rows(rows)
        P.append_rows([rows[2][:3] + (30, 1)])       # the same minute again, emptier
        got = P.read_rows(t0, t0 + 600)
        self.assertEqual(got, rows)
        self.assertEqual(oct(os.stat(P._day_file(t0)).st_mode & 0o777), "0o640")
        old = int(datetime.datetime(2025, 1, 2, tzinfo=datetime.timezone.utc).timestamp())
        P.append_rows([(old, 2.0, "est", 60, 7), (old + 60, 3.0, "est", 60, 0)])
        P.prune(at(2026, 7, 16))
        self.assertEqual(P.read_rows(old, old + 3600), [])
        with open(os.path.join(P.energy_dir(), "days.csv")) as f:
            self.assertEqual(f.read(), "2025-01-02,5.000,120,7\n")
        self.assertEqual(P.read_rows(t0, t0 + 600), rows)

    def test_summary(self):
        now = int(__import__("time").time())
        cut = now - now % 60
        P.append_rows([(cut - 60 * i, 2.0, "plug", 60, 10) for i in range(1, 121)])
        s = P.summary(now, sched(flat_rate=0.2), live={"t": now, "watts": 120.0, "src": "plug"})
        self.assertAlmostEqual(s["windows"]["1h"]["kwh"], 0.12)
        self.assertAlmostEqual(s["windows"]["1d"]["kwh"], 0.24)
        self.assertEqual(s["windows"]["1h"]["unknown_h"], 0)
        self.assertEqual(s["badge"]["text"], "flat rate")
        c = P.compact(s)
        self.assertEqual((c["watts"], c["src"], c["kwh_24h"], c["badge"]), (120.0, "plug", 0.24, None))
        self.assertAlmostEqual(c["cost_24h"], 0.048)
        stale = P.summary(now, sched(flat_rate=0.2), live={"t": now - 300, "watts": 1.0})
        self.assertIsNone(stale["live"])


class FakeRapl:
    def __init__(self, d, rng):
        self.d = d
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "max_energy_range_uj"), "w") as f:
            f.write(str(rng))

    def set(self, v):
        with open(os.path.join(self.d, "energy_uj"), "w") as f:
            f.write(str(v))


class TestSampler(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="o1rapl-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_rapl_wraparound(self):
        self.assertEqual(P.delta_uj(100, 600, 1000), 500)
        self.assertEqual(P.delta_uj(900, 50, 1000), 151)
        self.assertEqual(P.delta_uj(900, 50, None), 0)
        clock = [0.0]
        f = FakeRapl(self.dir, 262143328850)
        r = P.Rapl(self.dir, clock=lambda: clock[0])
        f.set(262143328850 - 30_000_000)
        self.assertIsNone(r.watts())                     # first reading: nothing to compare
        clock[0] = 1.0
        f.set(35_000_000 - 1)                            # wrapped: 30 J + 35 J in one second
        self.assertAlmostEqual(r.watts(), 65.0, places=1)
        clock[0] = 2.0
        f.set(35_000_000 + 45_000_000 - 1)
        self.assertAlmostEqual(r.watts(), 45.0, places=1)

    def test_estimate(self):
        self.assertEqual(P.estimate(200, 60, 40, 0.9), round(300 / 0.9, 1))
        self.assertEqual(P.estimate(None, None, 40, 0.9), round(40 / 0.9, 1))

    def sampler(self, plug=None, sleep_rec=None, tokens=None, gpu=100.0):
        clock = [1_800_000_000.0]
        rapl = type("R", (), {"watts": lambda self: 50.0})()
        s = P.Sampler({"power_baseline_w": 40, "power_psu_efficiency": 0.9, "power_sleep_w": 3},
                      clock=lambda: clock[0], plug=plug, rapl=rapl, gpu=lambda: gpu,
                      sleep_rec=sleep_rec, tokens=tokens or (lambda: None))
        return s, clock

    def test_plug_preferred_estimate_as_fallback(self):
        calls = []
        real = P.read_plug

        def fake(cfg, **kw):
            calls.append(cfg)
            if len(calls) > 1:
                raise P.PlugError("the plug didn't answer: timed out")
            return 250.0
        P.read_plug = fake
        try:
            s, clock = self.sampler(plug={"type": "shelly2", "host": "192.168.86.40"})
            live = s.tick()
            self.assertEqual((live["watts"], live["src"]), (250.0, "plug"))
            self.assertEqual(live["estimate_w"], round(190 / 0.9, 1))
            clock[0] += 10
            live = s.tick()
            self.assertEqual((live["watts"], live["src"]), (round(190 / 0.9, 1), "est"))
            self.assertFalse(live["plug"]["ok"])
            self.assertIn("timed out", live["plug"]["error"])
        finally:
            P.read_plug = real

    def test_any_plug_failure_falls_back_quietly(self):
        real = P.read_plug

        def boom(cfg, **kw):
            raise ValueError("\x00EVIL-MARKER raw plug bytes")
        P.read_plug = boom
        try:
            s, clock = self.sampler(plug={"type": "kasa", "host": "192.168.86.40"})
            live = s.tick()
        finally:
            P.read_plug = real
        self.assertEqual(live["src"], "est")
        self.assertEqual(live["plug"]["error"], "the plug's reply couldn't be read")
        self.assertNotIn("EVIL", json.dumps(live))

    def test_gap_unknown_and_sleep_counted(self):
        rec = {}
        s, clock = self.sampler(sleep_rec=lambda: rec)
        t0 = clock[0] = float(1_800_000_000 - 1_800_000_000 % 60)
        s.tick()
        clock[0] += 10
        s.tick()
        clock[0] += 600                                   # 10 minutes with no reading
        s.tick()
        rows = s.meter.take_complete(clock[0] + 120)
        self.assertAlmostEqual(sum(r[3] for r in rows), 10)
        self.assertAlmostEqual(sum(r[1] for r in rows), 190 / 0.9 * 10 / 3600, places=4)
        # now a sleep: the gap it covers counts at 3 W, marked as sleep
        rec.update(last_sleep=clock[0] + 5, last_wake=clock[0] + 3605)
        clock[0] += 3610
        s.tick()
        rows = s.meter.take_complete(clock[0] + 120)
        sleep = [r for r in rows if r[2] == "sleep"]
        self.assertAlmostEqual(sum(r[3] for r in sleep), 3600, delta=2)
        self.assertAlmostEqual(sum(r[1] for r in sleep), 3.0, places=3)
        del t0

    def test_a_stale_sleep_record_fills_nothing(self):
        rec = {"last_sleep": 1_700_000_000, "last_wake": 1_700_003_600}
        s, clock = self.sampler(sleep_rec=lambda: rec)
        s.tick()
        clock[0] += 600
        s.tick()
        self.assertEqual(s.meter.take_complete(clock[0] + 120), [])

    def test_tokens_by_minute(self):
        n = [1000]
        s, clock = self.sampler(tokens=lambda: n[0])
        s.tick()
        n[0] = 1250
        clock[0] += 10
        s.tick()
        n[0] = 40                                          # the counter was reset: no negative
        clock[0] += 10
        s.tick()
        n[0] = 100
        clock[0] += 10
        s.tick()
        rows = s.meter.take_complete(clock[0] + 120)
        self.assertEqual(sum(r[4] for r in rows), 250 + 60)


# ---- smart plugs ----------------------------------------------------------------

class FakePlugHTTP:
    """Shelly Gen1 (basic auth), Shelly Gen2 (digest auth, SHA-256), Tasmota."""

    def __init__(self):
        self.user, self.password = "admin", "plug-pass"
        self.auth = None                 # None / "basic" / "digest"
        self.redirect = False
        self.override = None
        self.calls = []
        plug = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj, extra=None):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                h = self.headers.get("Authorization") or ""
                if plug.auth == "basic":
                    want = "Basic " + base64.b64encode(("%s:%s" % (plug.user, plug.password)).encode()).decode()
                    return h == want
                if plug.auth == "digest":
                    if not h.startswith("Digest "):
                        return False
                    f = dict(p.strip().split("=", 1) for p in h[7:].split(","))
                    f = {k: v.strip('"') for k, v in f.items()}
                    sha = lambda x: hashlib.sha256(x.encode()).hexdigest()  # noqa: E731
                    ha1 = sha("%s:%s:%s" % (plug.user, "shellyplus", plug.password))
                    ha2 = sha("GET:%s" % f["uri"])
                    return f["response"] == sha(":".join([ha1, f["nonce"], f["nc"], f["cnonce"], f["qop"], ha2]))
                return True

            def do_GET(self):
                plug.calls.append((self.path, self.headers.get("Authorization")))
                if plug.override is not None:
                    raw = plug.override if isinstance(plug.override, str) else plug.override
                    return self.reply(200, raw)
                if plug.redirect and not self.path.startswith("/rpc/"):
                    loc = "http://127.0.0.1:%d/rpc/Switch.GetStatus?id=0" % plug.port
                    return self.reply(302, {}, {"Location": loc})
                if not self.authorized():
                    chal = ('Basic realm="shelly"' if plug.auth == "basic" else
                            'Digest qop="auth", realm="shellyplus", nonce="1700000000", algorithm=SHA-256')
                    return self.reply(401, {}, {"WWW-Authenticate": chal})
                if self.path == "/status":
                    return self.reply(200, {"relays": [{"ison": True}], "meters": [{"power": 212.5, "is_valid": True}]})
                if self.path == "/rpc/Switch.GetStatus?id=0":
                    return self.reply(200, {"id": 0, "output": True, "apower": 187.3, "voltage": 121.2})
                if self.path.startswith("/cm?"):
                    return self.reply(200, {"StatusSNS": {"Time": "2026-09-29T12:00:00",
                                                          "ENERGY": {"Power": 199, "Voltage": 120}}})
                self.reply(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", U.free_port()), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class FakeKasa:
    def __init__(self, reply):
        self.reply = reply
        self.got = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                n = struct.unpack(">I", c.recv(4))[0]
                data = b""
                while len(data) < n:
                    data += c.recv(n - len(data))
                self.got.append(json.loads(P.kasa_decrypt(data)))
                out = P.kasa_encrypt(json.dumps(self.reply).encode())
                c.sendall(struct.pack(">I", len(out)) + out)

    def close(self):
        self.sock.close()


def same(h):
    return h


class TestPlugs(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(P.parse_shelly1({"meters": [{"power": 10.5}, {"power": 2}]}), 12.5)
        self.assertEqual(P.parse_shelly1({"emeters": [{"power": 300}]}), 300.0)
        self.assertEqual(P.parse_shelly2({"apower": 187.3}), 187.3)
        self.assertEqual(P.parse_tasmota({"StatusSNS": {"ENERGY": {"Power": 199}}}), 199.0)
        self.assertEqual(P.parse_tasmota({"StatusSNS": {"ENERGY": {"Power": [100, 50]}}}), 150.0)
        self.assertEqual(P.parse_kasa({"emeter": {"get_realtime": {"power_mw": 123456, "err_code": 0}}}), 123.456)
        self.assertEqual(P.parse_kasa({"emeter": {"get_realtime": {"power": 77.7, "err_code": 0}}}), 77.7)
        for fn, bad in ((P.parse_shelly1, {}), (P.parse_shelly1, {"meters": [{"power": "x"}]}),
                        (P.parse_shelly2, {"apower": None}), (P.parse_shelly2, {"apower": True}),
                        (P.parse_shelly2, {"apower": 99999}), (P.parse_shelly2, {"apower": -5}),
                        (P.parse_tasmota, {"StatusSNS": {}}), (P.parse_kasa, {"emeter": {"get_realtime": {"err_code": -1}}}),
                        (P.parse_kasa, {"system": {}}), (P.parse_kasa, {"emeter": {"get_realtime": {"power_mw": "1"}}})):
            with self.assertRaises(P.PlugError, msg=repr(bad)):
                fn(bad)

    def test_kasa_cipher(self):
        # the documented autokey XOR: key starts at 171, each output byte is the next key
        msg = b'{"system":{"get_sysinfo":{}}}'
        key, want = 171, bytearray()
        for b in msg:
            key = key ^ b
            want.append(key)
        self.assertEqual(P.kasa_encrypt(msg), bytes(want))
        self.assertEqual(P.kasa_encrypt(msg)[:2], b"\xd0\xf2")
        self.assertEqual(P.kasa_decrypt(P.kasa_encrypt(msg)), msg)

    def test_lan_only(self):
        for ok in ("192.168.86.40", "10.0.0.7", "172.16.5.4", "172.31.255.1", "fd00::5"):
            self.assertTrue(P.lan_ip(ok), ok)
        self.assertEqual(P.lan_ip("::ffff:192.168.86.40"), "192.168.86.40")
        for bad in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "224.0.0.1", "plug.local", "192.168.1.5.nip.io",
                    "http://192.168.1.5", "", None, "::1", "2001:4860::8888",
                    "169.254.3.3", "169.254.169.254", "fe80::1", "::ffff:8.8.8.8", "::ffff:127.0.0.1",
                    "100.64.0.1", "172.32.0.1", "192.0.0.8", "198.18.0.1", "::ffff:169.254.169.254"):
            with self.assertRaises(P.PlugError, msg=bad):
                P.lan_ip(bad)
        with self.assertRaises(P.PlugError):
            P.read_plug({"type": "shelly2", "host": "127.0.0.1"})
        with self.assertRaises(P.PlugError):
            P.validate_plug({"type": "zigbee", "host": "192.168.1.5"})
        with self.assertRaises(P.PlugError):
            P.validate_plug({"type": "kasa", "host": "192.168.1.5", "user": "x"})
        with self.assertRaises(P.PlugError):
            P.validate_plug({"type": "shelly2", "host": "192.168.1.5", "url": "http://x"})
        self.assertEqual(P.validate_plug({"type": "shelly2", "host": "192.168.1.5", "user": "admin", "password": "p"}),
                         {"type": "shelly2", "host": "192.168.1.5", "user": "admin", "password": "p"})

    def test_http_plugs(self):
        f = FakePlugHTTP()
        self.addCleanup(f.close)
        rd = lambda cfg: P.read_plug(dict(cfg, host="127.0.0.1"), http_port=f.port, check=same)  # noqa: E731
        self.assertEqual(rd({"type": "shelly1"}), 212.5)
        self.assertEqual(rd({"type": "shelly2"}), 187.3)
        self.assertEqual(rd({"type": "tasmota"}), 199.0)
        self.assertEqual(f.calls[-1][0], "/cm?cmnd=Status%208")
        f.auth = "basic"
        with self.assertRaises(P.PlugError) as e:
            rd({"type": "shelly1"})
        self.assertIn("login", str(e.exception))
        self.assertEqual(rd({"type": "shelly1", "user": "admin", "password": "plug-pass"}), 212.5)
        f.auth = "digest"
        self.assertEqual(rd({"type": "shelly2", "user": "admin", "password": "plug-pass"}), 187.3)
        with self.assertRaises(P.PlugError):
            rd({"type": "shelly2", "user": "admin", "password": "wrong"})
        # each generation gets only its own scheme: Gen1 never answers a Digest
        # challenge, and Gen2 never sends a password in Basic
        with self.assertRaises(P.PlugError):
            rd({"type": "shelly1", "user": "admin", "password": "plug-pass"})
        f.auth = "basic"
        n = len(f.calls)
        with self.assertRaises(P.PlugError):
            rd({"type": "shelly2", "user": "admin", "password": "plug-pass"})
        self.assertTrue(all(not (a or "").startswith("Basic") for _, a in f.calls[n:]))
        f.auth = None
        f.redirect = True
        n = len(f.calls)
        with self.assertRaises(P.PlugError):
            rd({"type": "shelly1"})
        self.assertEqual(len(f.calls), n + 1)          # the redirect wasn't followed

    def test_hostile_replies(self):
        f = FakePlugHTTP()
        self.addCleanup(f.close)
        rd = lambda cfg: P.read_plug(dict(cfg, host="127.0.0.1"), http_port=f.port, check=same)  # noqa: E731
        for body in ([1, 2], "EVIL-MARKER", {"apower": "EVIL-MARKER"}, {"apower": float("inf")},
                     {"meters": "EVIL-MARKER"}, {"StatusSNS": ["EVIL-MARKER"]}):
            f.override = body
            for t in ("shelly1", "shelly2", "tasmota"):
                with self.assertRaises(P.PlugError) as e:
                    rd({"type": t})
                self.assertNotIn("EVIL", str(e.exception))
        f.override = None
        for bad in ([1], "x", {"emeter": []}, {"emeter": {"get_realtime": "EVIL"}},
                    {"emeter": {"get_realtime": {"err_code": "EVIL-MARKER"}}}):
            with self.assertRaises(P.PlugError) as e:
                P.parse_kasa(bad)
            self.assertNotIn("EVIL", str(e.exception))

    def test_a_trickling_plug_is_cut_off(self):
        import time as _t
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)
        stop = threading.Event()
        self.addCleanup(stop.set)

        start = _t.monotonic()

        def drip():
            while not stop.is_set():
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                def one(c=c):
                    with c:
                        try:
                            c.recv(4096)
                            for b in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 10000:
                                if stop.is_set() or _t.monotonic() - start > 12:
                                    return
                                c.send(bytes([b]))
                                _t.sleep(0.2)
                        except OSError:
                            pass
                threading.Thread(target=one, daemon=True).start()
        threading.Thread(target=drip, daemon=True).start()
        port = srv.getsockname()[1]
        for t in ("shelly2", "kasa"):
            t0 = _t.monotonic()
            with self.assertRaises(P.PlugError):
                P.read_plug({"type": t, "host": "127.0.0.1"}, timeout=1.5, http_port=port, kasa_port=port, check=same)
            self.assertLess(_t.monotonic() - t0, 3.0, t)

    def test_kasa_plug(self):
        k = FakeKasa({"emeter": {"get_realtime": {"voltage_mv": 121000, "power_mw": 95500, "err_code": 0}}})
        self.addCleanup(k.close)
        self.assertEqual(P.read_plug({"type": "kasa", "host": "127.0.0.1"}, kasa_port=k.port, check=same), 95.5)
        self.assertEqual(k.got, [{"emeter": {"get_realtime": {}}}])

    def test_nothing_answers(self):
        port = U.free_port()
        for t in ("shelly1", "kasa"):
            with self.assertRaises(P.PlugError):
                P.read_plug({"type": t, "host": "127.0.0.1"}, http_port=port, kasa_port=port, check=same, timeout=0.5)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.pre = tempfile.mkdtemp(prefix="o1pwcli-")
        self.addCleanup(shutil.rmtree, self.pre, True)
        for d in ("etc/ollama1", "var/lib/ollama1-admin", "var/lib/ollama1/energy", "run/ollama1/power"):
            os.makedirs(os.path.join(self.pre, d))

    def cli(self, *args):
        env = dict(os.environ, OLLAMA1_PREFIX=self.pre)
        return subprocess.run([sys.executable, os.path.join(U.BIN, "ollama1-power")] + list(args),
                              capture_output=True, text=True, env=env, timeout=60)

    def test_set_and_export_schedule(self):
        path = os.path.join(self.pre, "prices.json")
        good = tou(TWO_SEASONS)
        with open(path, "w") as f:
            json.dump(good, f)
        r = self.cli("set-schedule", path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("time-of-use, 2 season(s)", r.stdout)
        saved = os.path.join(self.pre, "var/lib/ollama1/tariff.json")
        self.assertEqual(oct(os.stat(saved).st_mode & 0o777), "0o640")
        r = self.cli("export-schedule")
        self.assertEqual(json.loads(r.stdout)["seasons"], good["seasons"])
        # a bad file changes nothing
        with open(path, "w") as f:
            json.dump(tou(ALL_YEAR(win("on", "weekdays", "16:00", "16:00"))), f)
        r = self.cli("set-schedule", path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("same time", r.stdout)
        self.assertEqual(json.load(open(saved))["seasons"], good["seasons"])
        r = self.cli("export-schedule", "--preset", "weekdays-4pm-9pm")
        s = json.loads(r.stdout)
        self.assertEqual(s["seasons"][0]["windows"], [win("on", "weekdays", "16:00", "21:00")])
        self.assertEqual(s["tiers"]["on"], 0.30)       # the prices in use carry over

    def test_status_and_plug_refusals(self):
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no price set", r.stdout)
        self.assertIn("Last 30 days", r.stdout)
        r = self.cli("set-plug", "shelly2", "8.8.8.8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't a private LAN address", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.pre, "etc/ollama1/power-plug.json")))
        r = self.cli("set-plug", "shelly2", "127.0.0.1")
        self.assertNotEqual(r.returncode, 0)


class TestReadmeExample(unittest.TestCase):
    def test_the_documented_shape_is_valid(self):
        import re
        with open(os.path.join(U.KIT, "README.md"), encoding="utf-8") as f:
            blocks = re.findall(r"```json\n(.*?)```", f.read(), re.S)
        self.assertTrue(blocks)
        for b in blocks:
            self.assertEqual(T.validate(json.loads(b))[1], [])


class TestNoUtilityInRepo(unittest.TestCase):
    """The kit ships generic shapes only: no utility's name or schedule."""

    def test_no_utility_names(self):
        import re
        # split, so this list doesn't match itself; whole words only
        names = ["du" + "ke", "con " + "ed", "con" + "ed", "con " + "edison", "ps" + "eg", "ps" + "e&g",
                 "national " + "grid", "ever" + "source", "ny" + "seg", "central " + "hudson", "fp" + "l",
                 "domin" + "ion", "pg" + "&e", "sdg" + "&e", "x" + "cel", "georgia " + "power",
                 "enter" + "gy", "amer" + "en", "southern california " + "edison", "so" + "cal edison",
                 "com" + "ed", "pep" + "co", "bg" + "&e", "ap" + "pco", "appalachian " + "power",
                 "tampa " + "electric", "orange and " + "rockland", "o&" + "r", "rg" + "&e", "jcp" + "&l",
                 "pp" + "l electric", "pec" + "o", "ever" + "gy", "we " + "energies", "consumers " + "energy",
                 "dte " + "energy", "l" + "ipa", "pse" + "g long island", "ai" + "g&e", "salt river " + "project"]
        pat = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![a-z0-9])")
        hits = []
        repo = os.path.dirname(U.KIT)
        paths = [os.path.join(repo, "NOTES.md")]
        for root in (U.KIT, os.path.join(repo, "docs")):
            for dp, _, files in os.walk(root):
                if "__pycache__" not in dp:
                    paths += [os.path.join(dp, fn) for fn in files]
        for path in paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().lower()
            except (OSError, UnicodeDecodeError):
                continue
            hits += ["%s: %s" % (os.path.basename(path), m.group(1)) for m in pat.finditer(text)]
        self.assertEqual(hits, [])

    def test_the_scan_would_notice(self):
        import re
        pat = re.compile(r"(?<![a-z0-9])(" + "du" + "ke|national " + "grid)(?![a-z0-9])")
        self.assertTrue(pat.search(("Our " + "Du" + "ke Energy bill").lower()))
        self.assertFalse(pat.search("the " + "du" + "kes"))            # whole words only


if __name__ == "__main__":
    unittest.main()
