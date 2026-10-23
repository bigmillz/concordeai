"""The fan calibration (6b505): the search on a simulated machine (sensors that settle toward a level-dependent
temperature, as a first-order system), the projection, the ceilings, the run with fake loads on a fake clock
(the hold file the fan service follows, the result file the levels come from, every safety stop and the abort),
the refusals, the real query load against the Ollama stub, the burn load's tree stop, and the unit files. No real
hardware, no real Ollama, no real burn."""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import o1test_util as U  # noqa: F401  (sets OLLAMA1_PREFIX first)
import o1fan
import o1fancal as C
import o1sleep
import o1work
from stub_ollama import Stub

WALL0 = 1_800_000_000


class Plant:
    """A made-up server: each sensor heads for base + rise[load] * (1 + k * (100 - level) / 70) with its own time
    constant (2 s steps). load is "query" or "burn" (None: idle)."""

    SENSORS = {  # label: (limit, base, query rise, burn rise, k, tau s)
        "CPU": (80, 35.0, 15.0, 18.0, 0.30, 60.0),
        "GPU": (85, 30.0, 30.0, 40.0, 0.25, 120.0),
        "NVMe": (70, 33.0, 8.0, 12.0, 0.40, 300.0),
    }

    def __init__(self, sensors=None):
        self.spec = dict(self.SENSORS)
        self.spec.update(sensors or {})
        self.t = {k: v[1] for k, v in self.spec.items()}

    def target(self, label, level, load):
        limit, base, rq, rb, k, tau = self.spec[label]
        rise = {"query": rq, "burn": rb}.get(load, 0.0)
        return base + rise * (1 + k * (100 - level) / 70.0)

    def step(self, level, load, dt=2.0):
        for label, (limit, base, rq, rb, k, tau) in self.spec.items():
            self.t[label] += (self.target(label, level, load) - self.t[label]) * (1 - math.exp(-dt / tau))

    def rows(self):
        return [{"label": k, "c": round(v, 1), "limit": self.spec[k][0]} for k, v in self.t.items()]


def simulate(plant, busy=90, until=4000):
    """The search on the plant: (search, [(t, stage, level)])."""
    s = C.Search()
    trail = []
    t = 0.0
    while not s.done and t < until:
        load = "burn" if s.stage == "burn" else "query"
        plant.step(s.level, load)
        s.step(t, plant.rows(), busy)
        trail.append((t, s.stage, s.level))
        t += C.TICK_S
    return s, trail


class TestProjection(unittest.TestCase):
    def test_a_settled_series_is_where_it_is(self):
        self.assertAlmostEqual(C.project([60.0] * 30), 60.0)
        self.assertAlmostEqual(C.project([61, 60, 61, 60, 61, 60, 61, 60, 61]), 60.5, delta=0.5)

    def test_a_falling_series_is_its_latest_window(self):
        vals = [70 - i * 0.2 for i in range(60)]
        self.assertLess(C.project(vals), vals[0])
        self.assertAlmostEqual(C.project(vals), sum(vals[40:]) / 20, delta=0.01)

    def test_a_first_order_rise_is_projected_toward_where_it_settles(self):
        vals = [80 - 20 * math.exp(-t / 100.0) for t in range(0, 120, 2)]
        p = C.project(vals)
        self.assertGreater(p, vals[-1] + 2)                           # well past the last reading ...
        self.assertLessEqual(p, 80 + 0.5)                             # ... and not past where it settles

    def test_jitter_is_not_a_trend(self):
        import random
        rnd = random.Random(5)
        for _ in range(50):
            vals = [60 + rnd.choice((-1, 0, 1)) for _ in range(60)]
            self.assertLess(C.project(vals), 61.6)

    def test_a_rise_that_doesnt_slow_is_assumed_the_worst(self):
        vals = [50 + 0.01 * t * t for t in range(60)]
        self.assertAlmostEqual(C.project(vals) - (sum(vals[40:]) / 20), C.PROJECT_CAP_C)

    def test_short_series(self):
        self.assertIsNone(C.project([]))
        self.assertEqual(C.project([50, 52, 51]), 52)

    def test_smoothing_takes_the_spikes_off(self):
        self.assertEqual(max(C.smooth([50, 50, 62, 50, 50])), 54)


class TestCeilingsAndLadder(unittest.TestCase):
    def test_ceilings(self):
        self.assertEqual(C.ceiling_for(80, 50), 70)                   # 10 C under the limit
        self.assertEqual(C.ceiling_for(80, None), 70)
        self.assertEqual(C.ceiling_for(80, 67), 70)                   # 13 under: still the plain ceiling
        self.assertEqual(C.ceiling_for(80, 72), 75)                   # near its limit at 100%: 3 C over that reading
        self.assertIsNone(C.ceiling_for(80, 77))                      # within 3 C of its limit even at 100%

    def test_the_ladder(self):
        self.assertEqual(C.LADDER, (100, 80, 65, 50, 40, 30))
        self.assertEqual([C.down(x) for x in C.LADDER], [80, 65, 50, 40, 30, None])
        self.assertEqual([C.up(x) for x in C.LADDER], [100, 100, 80, 65, 50, 40])
        self.assertEqual(C.LADDER[-1], o1work.WORK_MIN_PCT)
        self.assertEqual([C.margin(x) for x in (30, 50, 95, 100)], [40, 60, 100, 100])


class TestSearch(unittest.TestCase):
    def test_a_machine_with_room_finds_the_query_level_low_and_the_burn_level_where_the_card_needs_it(self):
        s, trail = simulate(Plant())
        self.assertIsNone(s.error)
        self.assertTrue(s.done)
        # the card under the burn: 30 + 40 * (1 + .25 * (100 - L) / 70) under its 75 C ceiling first at 80%
        self.assertEqual(s.found, {"query": 30, "burn": 80})
        self.assertEqual(s.result, {"quiet_pct": 40, "heavy_pct": 90, "found": {"query": 30, "burn": 80}})
        self.assertEqual(s.ceil, {"CPU": 70, "GPU": 75, "NVMe": 60})
        stages = [st for _t, st, _l in trail]
        self.assertEqual(sorted(set(stages), key=stages.index), ["short", "long", "burn"])
        t_long = next(t for t, st, _l in trail if st == "long")
        t_burn = next(t for t, st, _l in trail if st == "burn")
        self.assertAlmostEqual(t_long, C.SHORT_S, delta=C.TICK_S)
        self.assertAlmostEqual(t_burn - t_long, C.LONG_S, delta=C.TICK_S)
        self.assertGreaterEqual(trail[-1][0] - t_burn, C.BURN_S)
        self.assertLessEqual(trail[-1][0] - t_burn, C.BURN_MAX_S)

    def test_the_levels_never_hunt(self):
        s, trail = simulate(Plant())
        levels = [l for _t, _st, l in trail]
        changes = [(a, b) for a, b in zip(levels, levels[1:]) if a != b]
        downs = [i for i, (a, b) in enumerate(changes) if b < a]
        ups = [i for i, (a, b) in enumerate(changes) if b > a]
        self.assertTrue(not ups or not downs or max(downs) < min(ups), changes)   # down first, then only up
        burn = [l for _t, st, l in trail if st == "burn"]
        self.assertEqual(burn, sorted(burn))                           # the burn only ever goes up
        at = [0.0] + [b[0] for a, b in zip(trail, trail[1:]) if a[2] != b[2]]
        held = [y - x for x, y in zip(at, at[1:])]
        self.assertTrue(all(h >= C.BREACH_GRACE_S for h in held), held)  # every level held a minute at least

    def test_a_card_that_runs_warm_under_queries_stops_the_descent_and_steps_back(self):
        # the card under queries: 30 + 36 * (1 + .4 * (100 - L) / 70): 65% is the lowest under 75 C
        s, trail = simulate(Plant({"GPU": (85, 30.0, 36.0, 40.0, 0.40, 60.0)}))
        self.assertIsNone(s.error)
        self.assertEqual(s.found["query"], 65)
        long_levels = [l for _t, st, l in trail if st == "long"]
        self.assertIn(50, long_levels)                                 # tried, too warm, back up for good
        self.assertEqual(long_levels[-1], 65)
        self.assertGreaterEqual(s.found["burn"], s.found["query"])
        self.assertTrue(any(not b["ok"] and b["worst"] == "GPU" for b in s.blocks))

    def test_a_sensor_near_its_limit_at_full_speed_ends_it_and_says_which(self):
        s, _ = simulate(Plant({"NVMe": (70, 68.0, 1.0, 1.0, 0.1, 30.0)}))
        self.assertTrue(s.done)
        self.assertIsNone(s.result)
        self.assertIn("NVMe", s.error)
        self.assertIn("nothing was changed", s.error)

    def test_a_card_that_never_gets_busy_ends_it(self):
        s, _ = simulate(Plant(), busy=0)
        self.assertIn("never got busy", s.error)
        s, _ = simulate(Plant(), busy=None)
        self.assertIn("never got busy", s.error)

    def test_a_card_only_a_little_busy_ends_it(self):
        s = C.Search()
        t = 0.0
        while not s.done and t < 400:
            s.step(t, Plant().rows(), 60 if t < 4 else 10)
            t += 2
        self.assertIn("only", s.error)

    def test_a_breach_steps_up_before_the_block_ends(self):
        s = C.Search()
        rows = lambda c: [{"label": "GPU", "c": c, "limit": 85}]   # noqa: E731
        t = 0.0
        while s.stage == "short":
            s.step(t, rows(60), 90)
            t += 2
        self.assertEqual((s.stage, s.level), ("long", 80))
        start = t
        while s.level == 80 and t - start < C.LONG_BLOCK_S:
            s.step(t, rows(60 + (t - start) * 0.3), 90)             # rising past 75 C, not falling
            t += 2
        self.assertEqual(s.level, 100)
        self.assertLess(t - start, C.LONG_BLOCK_S)
        self.assertGreaterEqual(t - start, C.BREACH_GRACE_S)

    def test_the_time_left(self):
        s = C.Search()
        self.assertEqual(s.left(0), C.SHORT_S + C.LONG_S + C.BURN_S)
        self.assertEqual(s.left(50), C.SHORT_S + C.LONG_S + C.BURN_S - 50)


class FakeLoad:
    def __init__(self, kind, log):
        self.kind, self.log, self.why, self.stopped = kind, log, None, False
        log.append(("start", kind))

    def poll(self):
        return self.why

    def stop(self):
        self.stopped = True
        self.log.append(("stop", self.kind))


class RunCase(unittest.TestCase):
    """A Runner on a fake clock. The plant reads the fans' level from the hold file, through the fan service's own
    reader, so the hold file is what the run is checked by."""

    def setUp(self):
        self.t = 0.0
        self.plant = Plant()
        self.events = []
        self.loads = []
        self.stop = threading.Event()
        self.hook = None
        self.act = {"inflight": 0, "last": 0.0, "fresh": True}
        self.errs = []
        self.levels_seen = []
        for p in (C.result_path(), C.last_path(), C.log_path(), C.progress_path(), o1fan.tuning_path(), C.abort_path()):
            self.addCleanup(lambda p=p: os.path.exists(p) and os.unlink(p))
            if os.path.exists(p):
                os.unlink(p)
        os.makedirs(os.path.dirname(C.progress_path()), exist_ok=True)

    def wall(self):
        return WALL0 + self.t

    def wait(self, s):
        self.t += s
        return self.stop.is_set()

    def fan_status(self):
        if self.hook:
            r = self.hook(self.t)
            if r is not None:
                return r
        level = o1fan.read_tuning(now=self.wall())
        self.levels_seen.append(level)
        load = self.loads[-1].kind if self.loads and not self.loads[-1].stopped else None
        self.plant.step(level if level is not None else 100, load)
        return {"at": int(self.wall()), "phase": "tuning" if level else "idle20", "temps": self.plant.rows(), "hot": []}

    def load(self, kind):
        def make(model):
            ld = FakeLoad(kind, self.events)
            self.loads.append(ld)
            return ld
        return make

    def runner(self, **kw):
        args = dict(model="small:8b", clock=lambda: self.t, wall=self.wall, wait=self.wait, log=self.events.append,
                    fan_status=self.fan_status, gpu_busy=lambda: 90, activity=lambda now: self.act,
                    errors=lambda since: self.errs, query=self.load("query"), burn=self.load("burn"),
                    unload=lambda m: self.events.append(("unload", m)), refuse=lambda: None, stop=self.stop)
        args.update(kw)
        return C.Runner(**args)


class TestRun(RunCase):
    def test_a_whole_run_saves_the_levels_the_fan_service_then_uses(self):
        r = self.runner()
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state, "done")
        res = json.load(open(C.result_path()))
        self.assertEqual((res["v"], res["quiet_pct"], res["heavy_pct"]), (1, 40, 90))
        self.assertEqual(res["found"], {"query": 30, "burn": 80})
        self.assertEqual(res["model"], "small:8b")
        self.assertEqual(res["margin_pct"], C.MARGIN_PCT)
        self.assertTrue(res["blocks"])
        self.assertEqual({c["label"] for c in res["ceilings"]}, {"CPU", "GPU", "NVMe"})
        self.assertEqual(oct(os.stat(C.result_path()).st_mode & 0o777), "0o644")
        self.assertFalse(os.path.exists(o1fan.tuning_path()))          # the fans are given back
        self.assertNotIn(None, self.levels_seen)                       # the hold was fresh the whole run
        self.assertEqual(self.levels_seen[0], 100)
        self.assertIn(30, self.levels_seen)
        kinds = [e for e in self.events if isinstance(e, tuple)]
        self.assertEqual(kinds[:4], [("start", "query"), ("stop", "query"), ("unload", "small:8b"), ("start", "burn")])
        self.assertEqual(kinds[-2:], [("stop", "burn"), ("unload", "small:8b")])
        prog = json.load(open(C.progress_path()))
        self.assertEqual((prog["state"], prog["result"]["quiet_pct"]), ("done", 40))
        self.assertEqual(json.load(open(C.last_path()))["state"], "done")
        log = json.load(open(C.log_path()))
        self.assertGreater(len(log["samples"]), 200)
        self.assertEqual(set(log["samples"][0]["c"]), {"CPU", "GPU", "NVMe"})
        lv = o1work.levels(cfg_path=os.path.join(U.PREFIX, "nowhere.json"), cal_path=C.result_path())
        self.assertEqual(lv, {"quiet": 40, "heavy": 90, "source": "calibration"})
        # never what a model was asked: the prompt is in no file
        for p in (C.result_path(), C.progress_path(), C.last_path(), C.log_path()):
            self.assertNotIn("garden", open(p).read())

    def test_the_progress_while_running(self):
        seen = []

        def hook(t):
            if 400 < t < 403:
                seen.append(json.load(open(C.progress_path())))
        self.hook = hook
        self.runner().run()
        p = seen[0]
        self.assertEqual((p["state"], p["stage"], p["model"]), ("running", "long", "small:8b"))
        self.assertGreater(p["left"], C.BURN_S)
        self.assertTrue(p["blocks"])
        self.assertEqual({t["label"] for t in p["temps"]}, {"CPU", "GPU", "NVMe"})
        self.assertTrue(all("ceiling" in t for t in p["temps"]))

    def stopped_by(self, hook=None, **kw):
        self.hook = hook
        r = self.runner(**kw)
        rc = r.run()
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(C.result_path()))             # nothing saved
        self.assertFalse(os.path.exists(o1fan.tuning_path()))          # the fans given back
        self.assertTrue(all(ld.stopped for ld in self.loads))
        self.assertEqual(json.load(open(C.last_path()))["state"], r.state)
        return r

    def test_the_abort_is_at_once(self):
        def hook(t):
            if t >= 300 and not self.stop.is_set():
                self.stop.set()
        r = self.runner()
        r.why_stop = "stopped from the panel"
        self.hook = hook
        self.assertEqual(r.run(), 1)
        self.assertEqual((r.state, r.reason), ("aborted", "stopped from the panel"))
        self.assertLess(self.t, 300 + 2 * C.TICK_S)
        self.assertFalse(os.path.exists(o1fan.tuning_path()))
        self.assertFalse(os.path.exists(C.result_path()))

    def test_heat_at_a_limit_stops_it(self):
        r = self.stopped_by(lambda t: {"at": int(self.wall()), "phase": "hot", "temps": self.plant.rows(),
                                       "hot": ["CPU 81 C (limit 80)"]} if t > 200 else None)
        self.assertEqual(r.state, "failed")
        self.assertIn("CPU 81 C", r.reason)

    def test_a_reading_at_its_limit_stops_it_even_before_the_fan_service_says_so(self):
        r = self.stopped_by(lambda t: {"at": int(self.wall()), "phase": "tuning",
                                       "temps": [{"label": "GPU", "c": 85.0, "limit": 85}]} if t > 200 else None)
        self.assertIn("GPU reached its 85 C limit", r.reason)

    def test_the_fan_service_going_quiet_stops_it(self):
        self.hook = None
        r = self.stopped_by(lambda t: {} if t > 200 else None)
        self.assertIn("fan service stopped reporting", r.reason)

    def test_a_request_stops_it(self):
        def hook(t):
            if t > 500:
                self.act = {"inflight": 1, "last": self.wall(), "fresh": True}
        r = self.stopped_by(hook)
        self.assertIn("request", r.reason)

    def test_an_xid_stops_it(self):
        def hook(t):
            if t > 900:
                self.errs = ["an NVIDIA Xid error"]
        r = self.stopped_by(hook)
        self.assertIn("NVIDIA Xid", r.reason)
        self.assertLess(self.t, 900 + C.ERRORS_EVERY_S + 2 * C.TICK_S)

    def test_a_load_that_fails_stops_it(self):
        def hook(t):
            if t > 1000 and self.loads:
                self.loads[-1].why = "the burn stopped by itself: ABORTED: the card reached 90 C"
        r = self.stopped_by(hook)
        self.assertIn("90 C", r.reason)

    def test_a_refusal_runs_nothing(self):
        r = self.runner(refuse=lambda: "the server is answering a request")
        self.assertEqual(r.run(), 2)
        self.assertEqual(self.loads, [])
        self.assertFalse(os.path.exists(o1fan.tuning_path()))
        p = json.load(open(C.progress_path()))
        self.assertEqual((p["state"], p["reason"]), ("refused", "the server is answering a request"))

    def test_a_stalled_progress_file_and_stop_post(self):
        C.write_json_atomic(C.progress_path(), {"at": int(time.time()) - 60, "state": "running"})
        self.assertEqual(C.read_progress()["state"], "stalled")
        self.assertFalse(C.running())
        C.write_json_atomic(o1fan.tuning_path(), {"at": int(time.time()), "pct": 40})
        C.write_json_atomic(C.abort_path(), {"at": 1})
        C.stop_post(log=lambda *_: None)
        self.assertFalse(os.path.exists(o1fan.tuning_path()))
        self.assertFalse(os.path.exists(C.abort_path()))
        p = json.load(open(C.progress_path()))
        self.assertEqual((p["state"], p["reason"]), ("aborted", "stopped from the panel"))
        self.assertEqual(json.load(open(C.last_path()))["state"], "aborted")

    def test_the_abort_unit_marks_then_stops(self):
        calls = []
        self.assertEqual(C.request_abort(systemctl=lambda: calls.append(os.path.exists(C.abort_path())) or 0), 0)
        self.assertEqual(calls, [True])

    def test_status_text(self):
        C.write_json_atomic(C.result_path(), {"v": 1, "at": WALL0, "quiet_pct": 40, "heavy_pct": 60, "model": "m",
                                              "found": {"query": 30, "burn": 50}})
        txt = C.status_text()
        self.assertIn("quiet 40%, heavy 60%", txt)
        self.assertIn("found 30% under the query, 50% under the burn", txt)


class TestRefusal(unittest.TestCase):
    def setUp(self):
        self.models = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.models, True)
        man = C.manifest("small:8b", self.models)
        os.makedirs(os.path.dirname(man))
        open(man, "w").write("{}")
        self.now = WALL0
        self.kw = dict(fan_status=lambda: {"phase": "idle20", "temps": [{"label": "CPU", "c": 40, "limit": 80}]},
                       activity=lambda now: {"inflight": 0, "last": now - 600}, busy=lambda: [],
                       tools=lambda: [], is_installed=lambda m: True, bench=lambda: "/x/llama-bench",
                       gpu_busy=lambda: 3, now=self.now, models_dir=self.models)

    def why(self, **over):
        kw = dict(self.kw)
        kw.update(over)
        return C.refusal("small:8b", **kw)

    def test_all_clear(self):
        self.assertIsNone(self.why())

    def test_each_reason(self):
        for over, want in (
                ({"fan_status": lambda: None}, "fan service isn't running"),
                ({"fan_status": lambda: {"phase": "calibrating", "temps": []}}, "still measuring"),
                ({"fan_status": lambda: {"phase": "hot", "temps": []}}, "at its limit"),
                ({"fan_status": lambda: {"phase": "idle20", "temps": [{"label": "NVMe", "c": 1, "limit": 70}]}},
                 "no processor temperature"),
                ({"activity": lambda now: {"inflight": None, "last": 0}}, "couldn't tell"),
                ({"activity": lambda now: {"inflight": 2, "last": now}}, "answering a request"),
                ({"activity": lambda now: {"inflight": 0, "last": now - 30}}, "30 s ago"),
                ({"busy": lambda: ["a model is being downloaded"]}, "not now: a model is being downloaded"),
                ({"busy": lambda: ["Claude Code is working"]}, "Claude Code is working"),
                ({"tools": lambda: ["gpu-burn.sh"]}, "gpu-burn.sh is running"),
                ({"tools": lambda: None}, "couldn't look"),
                ({"is_installed": lambda m: False}, "small:8b isn't installed"),
                ({"is_installed": lambda m: None}, "Ollama isn't answering"),
                ({"models_dir": "/nonexistent"}, "isn't installed"),
                ({"bench": lambda: None}, "llama-bench"),
                ({"gpu_busy": lambda: None}, "no reading from the graphics card")):
            with self.subTest(want):
                self.assertIn(want, self.why(**over))

    def test_its_own_unit_is_not_a_reason(self):
        mine = dict(o1sleep.BUSY_UNITS)[C.UNIT]
        self.assertIsNone(self.why(busy=lambda: [mine]))
        self.assertIn("pull", self.why(busy=lambda: [mine, "a pull"]))

    def test_the_model(self):
        self.assertEqual(C.model_name({}), "gemma4:12b")
        self.assertEqual(C.model_name({"fan_cal_model": "gpt-oss:20b"}), "gpt-oss:20b")
        for bad in ("x; rm -rf /", "", 5, None, "a" * 90):
            self.assertEqual(C.model_name({"fan_cal_model": bad}), "gemma4:12b", bad)
        self.assertTrue(C.manifest("gemma4:12b", "/srv/models").endswith("library/gemma4/12b"))
        self.assertTrue(C.manifest("nomic", "/srv/models").endswith("library/nomic/latest"))

    def test_llama_bench_is_found_where_gpu_burn_looks(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        b = os.path.join(d, "alice", "llama-bench", "b1234", "llama-bench")
        os.makedirs(os.path.dirname(b))
        open(b, "w").write("#!/bin/sh\n")
        self.assertIsNone(C.find_bench({}, d + "/*/llama-bench/*/llama-bench", d + "/none/*"))
        os.chmod(b, 0o755)
        self.assertEqual(C.find_bench({}, d + "/*/llama-bench/*/llama-bench", d + "/none/*"), b)
        self.assertIsNone(C.find_bench({"O1_LLAMA_BENCH": "/nonexistent"}))

    def test_hardware_errors_from_the_kernel_log(self):
        class R:
            stdout = ("Oct 23 kernel: NVRM: Xid (PCI:0000:0b:00): 79, GPU has fallen off the bus\n"
                      "Oct 23 kernel: usb 1-2: new device\nOct 23 kernel: mce: [Hardware Error]: CPU 3\n"
                      "Oct 23 kernel: NVRM: Xid (PCI:0000:0b:00): 13\n")
        seen = []
        kinds = C.hw_errors(WALL0, run=lambda cmd, **kw: seen.append(cmd) or R())
        self.assertEqual(kinds, ["an NVIDIA Xid error", "a machine-check (hardware) error"])
        self.assertIn("@%d" % WALL0, seen[0])
        self.assertEqual(C.hw_errors(0, run=lambda *a, **k: (_ for _ in ()).throw(OSError())), [])


class TestQueryLoad(unittest.TestCase):
    def setUp(self):
        self.stub = Stub(U.free_port())
        self.addCleanup(self.stub.close)

    def test_it_asks_again_and_again_and_stops_when_told(self):
        self.stub.delay = 0.0
        q = C.QueryLoad("small:8b", base="http://127.0.0.1:%d" % self.stub.port, timeout=5)
        end = time.time() + 5
        while q.answers < 3 and time.time() < end:
            time.sleep(0.05)
        q.stop()
        self.assertGreaterEqual(q.answers, 3)
        self.assertIsNone(q.poll())
        self.assertFalse(q.thread.is_alive())
        gen = [c for c in self.stub.calls if c[1] == "/api/generate"]
        asks = [c for c in gen if "prompt" in c[2]]
        # the questions stream; anything else is the model let go (keep_alive 0), which can land in the
        # list before the test looks, under load
        self.assertGreaterEqual(len(asks), 3)
        self.assertTrue(all(c[2].get("stream") is True and c[2]["model"] == "small:8b" for c in asks))
        self.assertTrue(all(c[2].get("keep_alive") in (0, "0") and c[2]["model"] == "small:8b"
                            for c in gen if c not in asks))

    def test_a_model_ollama_doesnt_have_fails_after_three_tries(self):
        q = C.QueryLoad("absent:1b", base="http://127.0.0.1:%d" % self.stub.port, timeout=5)
        q.thread.join(15)
        self.assertIn("404", q.poll())

    def test_installed(self):
        base = "http://127.0.0.1:%d" % self.stub.port
        self.assertTrue(C.installed("small:8b", base))
        self.assertFalse(C.installed("absent:1b", base))
        self.assertIsNone(C.installed("small:8b", "http://127.0.0.1:1"))


class TestBurnLoad(unittest.TestCase):
    """A stand-in for gpu-burn.sh that leaves a child in a process group of its own, as GNU timeout does."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def script(self, body):
        p = os.path.join(self.d, "burn.sh")
        open(p, "w").write("#!/usr/bin/env bash\n" + body)
        return p

    def test_it_says_why_it_ended_by_itself(self):
        b = C.BurnLoad("m", script=self.script('echo "ollama1 gpu burn"\necho "ABORTED: the card reached 90 C"\nexit 1\n'))
        b.p.wait(10)
        self.assertEqual(b.poll(), "the burn stopped by itself: ABORTED: the card reached 90 C")
        b.stop()
        self.assertIsNone(b.poll())

    def test_stop_takes_the_whole_tree(self):
        pidfile = os.path.join(self.d, "child.pid")
        b = C.BurnLoad("m", script=self.script(
            'python3 -c "import os,time; os.setpgid(0,0); open(\'%s\',\'w\').write(str(os.getpid())); time.sleep(60)" &\n'
            'wait\n' % pidfile))
        end = time.time() + 10
        while not os.path.exists(pidfile) and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.2)
        child = int(open(pidfile).read())
        self.assertIsNone(b.poll())
        if os.path.isdir("/proc"):
            self.assertIn(child, C.children(b.p.pid))
            b.stop()
            time.sleep(0.3)
            self.assertFalse(os.path.exists("/proc/%d" % child))
        else:                                   # no /proc here (a Mac): only the script itself can be found
            b.stop()
            os.kill(child, 9)
        self.assertIsNotNone(b.p.poll())


class TestTheWiring(unittest.TestCase):
    def unit(self, name):
        return open(os.path.join(U.SYSTEMD, name)).read()

    def test_the_run_unit(self):
        u = self.unit("ollama1-fancal.service")
        for line in ("ExecStart=/usr/local/lib/ollama1/bin/ollama1-fancal run",
                     "ExecStopPost=/usr/local/lib/ollama1/bin/ollama1-fancal stop-post", "KillMode=control-group",
                     "NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/run/ollama1 /var/lib/ollama1",
                     "IPAddressAllow=localhost", "IPAddressDeny=any", "ProtectHome=read-only", "Type=oneshot"):
            self.assertIn("\n" + line + "\n", u, line)
        self.assertNotIn("\n[Install]", u)
        mins = int(u.split("TimeoutStartSec=")[1].split("min")[0])
        self.assertGreater(mins * 60, C.SHORT_S + C.LONG_S + C.BURN_MAX_S + 600)

    def test_the_abort_unit(self):
        u = self.unit("ollama1-fancal-abort.service")
        self.assertIn("\nExecStart=/usr/local/lib/ollama1/bin/ollama1-fancal abort\n", u)
        self.assertNotIn("\n[Install]", u)

    def test_sleep_waits_for_it(self):
        self.assertIn(("ollama1-fancal.service", "the fans are being calibrated"), o1sleep.BUSY_UNITS)
        self.assertEqual(o1sleep.busy_reasons(active_units=lambda pats: ["ollama1-fancal.service"],
                                              setup_lock="/nonexistent", hold_locks="/nonexistent"),
                         ["the fans are being calibrated"])

    def test_the_program(self):
        p = os.path.join(U.BIN, "ollama1-fancal")
        self.assertTrue(os.access(p, os.X_OK))
        r = subprocess.run([sys.executable, p, "status"], capture_output=True, text=True,
                           env=dict(os.environ, OLLAMA1_PREFIX=U.PREFIX))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("in force:", r.stdout)

    def test_the_burn_and_the_hold_agree(self):
        self.assertGreaterEqual(o1fan.TUNING_FRESH_S, 3 * C.TICK_S)
        self.assertEqual(C.ERRORS_EVERY_S, 30)
        self.assertEqual(C.DEFAULT_MODEL, "gemma4:12b")
        burn = open(os.path.join(U.TOOLS, "gpu-burn.sh")).read()
        self.assertIn("MODEL=${2:-gemma4:12b}", burn)                  # the same default model as the burn tools


if __name__ == "__main__":
    unittest.main()
