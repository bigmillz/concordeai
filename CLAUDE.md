# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**ConcordeAI** — a local-first AI desktop app for macOS and Windows. It runs LLMs on the
user's own machine (MLX on Apple silicon, Ollama elsewhere), optionally blends in cloud
models, and composites several drafts into one answer.

Two things make this repo unlike most:

1. **The entire app is one file.** `millenai.py` (~16.8k lines) is the HTTP server, the
   model orchestration, *and* the whole client — HTML, CSS and JS live inside it as Python
   string constants. There is no bundler, no framework, no `node_modules`. Stdlib only
   (`http.server` + `socketserver.ThreadingTCPServer`), plus optional `psutil`, `ddgs`,
   `mlx_lm`, `pywebview`.
2. **`NOTES.md` (~3.9k lines) is the real documentation.** It is a chronological
   engineering log, one entry per build, recording what broke and why. Most non-obvious
   code has a `(6bNNN, per Patrick: …)` comment pointing at a NOTES entry. **Read the
   relevant entry before "fixing" something that looks odd — it usually isn't.** Append a
   new entry for each build you cut.

The repo lives in Google Drive, which has silently rolled `.git` back before. **Always
`git fetch` and compare against origin before releasing.**

**This repo also hosts a second, unrelated product: ConcordeGo**, the flight re-ranker
(directory/config name `concorde-travel`). It shares nothing with the desktop app — not
the process, not `millenai.py`, not the gauntlet. Its brief is
[`docs/design.md`](docs/design.md) and its working rules are at the bottom of this file.
Everything between here and there is about the desktop app only.

## Commands

Everything runs on the app's own venv. A bare/Homebrew Python is missing `psutil` and
`ddgs`, which silently degrades web search and `/api/setup` and sends you chasing ghosts.

```bash
VENV="$HOME/Library/Application Support/MillenAI/venv/bin/python3"
```

### Dev server

```bash
MILLENAI_PORT=9894 MILLENAI_KEY=smoketestkey123 MILLENAI_HEADLESS=1 \
  "$VENV" millenai.py > /tmp/dev.log 2>&1 &
```

`MILLENAI_HEADLESS=1` suppresses the window and `webbrowser.open`. Port convention:
**8889** desktop app, **9889** the always-on hosted instance, **9894** dev. Ports
**8884–8930 are reserved for model engines** — never bind a server there.

The page is assembled at request time but **the process reads `millenai.py` once at
import**, so *any edit needs a server restart*. Kill by port, never `pkill -f`:

```bash
kill $(lsof -tnP -iTCP:9894 -sTCP:LISTEN)
```

`pkill -f "MILLENAI_PORT=9894"` does **not** work — env assignments aren't in Python's
argv, so it kills the wrapper shell, orphans the server, and the next start fails to bind
while you unknowingly test stale code.

### Tests — the "gauntlet"

`tests_smoketest.py` is not pytest. It is a linear script of ~81 `check()` call sites (a
few sit in loops, so the scorecard reads a little higher) run against a **live server it
does not start**. Start one on 9894 first (above), then:

```bash
cd "/Users/patrickmiller/My Drive/Projects/Concorde/ConcordeAI"
"$VENV" tests_smoketest.py > /tmp/gauntlet.log 2>&1; echo "exit=$?"; tail -20 /tmp/gauntlet.log
```

**Never pipe the run into `tail`** — you get *tail's* exit code, and a red run has slipped
into a release that way. It must run from the repo root (it opens `millenai.py`
relatively). Full runs take minutes because they load real engines and stream real
answers. Exits 0 only on a clean sweep.

It asserts against three surfaces, which is why a UI change can fail a test:
- **live HTTP** (auth, owner-only lockdown, range serving, the risk classifier),
- **the served HTML** — many checks are exact CSS/JS substring assertions guarding
  against UI regressions,
- **the source text** (`_MILLENAI_SRC`) for server-side Python that never reaches the page.

**Running one check:** there is no selector. Assert over the wire instead:

```bash
curl -s -b 'millen_key=smoketestkey123' \
  'http://127.0.0.1:9894/api/remote/classify?cmd=rm%20-rf%20/'     # {"risk":"danger"}
curl -s -b 'millen_key=smoketestkey123' http://127.0.0.1:9894/ | grep -c 'barBreathe'
```

A test instance is **not isolated** — it shares `prefs.json`, `chats.json` and the 88xx
engine ports with the desktop app, so a run can evict the app's resident engines or flip
its prefs.

### Release

```bash
./release.sh 6.0.0        # explicit version, or patch|minor|major
```

Needs `gh`. In order it: bumps `APP_VERSION`/`APP_BUILD` **in `millenai.py`** (the only
place either is stored), builds the DMG and the Windows zip, commits, pushes, creates a
GitHub release, then updates the hosted instance. GitHub Actions
(`.github/workflows/windows-installer.yml`) attaches the x64 MSI a few minutes later.

Two things to internalise:
- **The release tag is `v<APP_BUILD>`, not the version** (`v256`). `APP_BUILD` is a
  monotonic counter and is what the in-app updater compares; the marketing version can
  move independently.
- **While `APP_BETA > 0` (or `APP_RC > 0`), releases publish as prereleases.**
  `/releases/latest` excludes prereleases, so stable users are held back and only
  the Beta channel (`update_channel` pref) is offered them. This is deliberate.
  Betas are numbered per version line from 1 (`6.1 beta 2`), never by build;
  `./release.sh beta` cuts the next one, a stable cut resets it to 0. Nightlies
  are the rolling `nightly` release built by GitHub Actions on every push.

## Architecture

### Request lifecycle (`/api/chat`)

One request owns one thread for the whole generation. Roughly:

1. Parse body; **alias retired tier names** (`Smart`/`Best` → `Fast`, `Power` → `Pro`) so
   old clients keep working.
2. Resolve the council from the tier. If it has >1 model, the projected compositor is
   moved to the **end** of the roster so the merge stage finds it already resident instead
   of reloading multi-GB weights.
3. Agents / images / attached docs each **hijack the council** (images force the vision
   model and disable search).
4. **Route resolution and MLX pre-warm happen *before* the web search**, on a background
   thread, so a disk load and a network fetch overlap — and headers go out immediately,
   because the Cloudflare heartbeat only starts after headers.
5. Search branches into weather / OSM places / deep research / plain search. Results land
   on a **thread-local** (`_tl_search`) that must be cleared per request, since keep-alive
   reuses threads and stale rows leak between questions.
6. Stream, then a `finally` block emits map/place pins, logs a line to `quality.jsonl`,
   and extracts memories on a daemon thread.

### The wire protocol

The response body is plain text with **out-of-band control frames**: `\0TAG:json\0`
inline in the stream (`NUL = chr(0)`). Tags include `STATUS`, `STEP`, `DRAFT`, `RUN`,
`SOURCES`, `PHOTOS`, `MAP`, `PLACES2`, `PLACEHINT`, `CTX`, `APPROVE`, and bare `RESET`
(throw away the answer already on screen). The client strips them so they never appear in
prose. Two custom headers matter: `X-Web-Search` and `X-Models`.

A **heartbeat thread** re-sends the last `STATUS` when the stream goes quiet >20s —
Cloudflare drops a silent proxied response after ~100s, and an engine swap is a
multi-minute silence.

### The client

`HTML_CONTENT` is a giant string with `__TOKEN__` placeholders substituted server-side at
request time (`__TIER_META__`, `__CHIP__`, `__APP_VER__`, `__MEM_LABEL__`, `__SKY_*__`,
etc). A gauntlet check fails on any unreplaced token. The page is served with
`ETag: "b<APP_BUILD>"` + `no-cache`, so browsers revalidate and a new build always lands.

Three lanes — **Chat**, **Code**, **Funnels** — share one composer; `uiMode` selects
which. Client state lives in `localStorage` under `millen.*` keys.

### Models and the council

- **`CATALOG`** declares every model (engine, port, memory, size). `MODEL_ROUTES` picks
  MLX on Apple silicon, else Ollama.
- **`TIERS`** (Fast / Thinking / Pro / Cloud Only) → `resolve_tier()` returns concrete
  models that are *downloaded and fit in RAM right now*.
- **`run_council()`**: local models draft **sequentially** — only one MLX engine can be
  resident, so parallel loads would thrash — while every healthy cloud key drafts **in
  parallel** on threads. Then optional reflection/peer review, then a **compositor**
  writes the single final answer. Per-model and whole-loop timeouts mean a straggler is
  simply absent rather than holding the answer hostage.
- **Two ladders, different orders**: `compositor_ladder()` is strength-first (Claude →
  Kimi → Gemini → Groq); `fast_cloud_ladder()` is *speed*-first (Groq → Gemini → Kimi →
  Claude-as-Haiku) for the single-answer path.
- Cloud keys live in `cloud.json`. Providers are validated *and* model-discovered on save,
  and rested on a cooldown when quota-limited — a resting provider is healthy, not broken.

### Remote SSH agent (Code lane)

A plan → run → read → repeat loop over `ssh`, **key-only** (`BatchMode=yes`; it cannot use
passwords by design). Every command is classified `read` / `write` / `danger`, and the
autonomy level (Manual / Auto / Full) decides what needs approval — `danger` always does.
Approval round-trips through an `APPROVE:` frame and blocks on a `threading.Event`.

Long jobs run detached under `systemd-run --no-block` and are polled, so a 40-minute
upgrade doesn't hit the per-command timeout; the job **writes its own exit code to a file**
because `--collect` deletes the unit before systemd can be asked. Reboots are a first-class
action: the agent issues one, waits for the box, reconnects and continues.

### Identity

The server binds **127.0.0.1 only**; remote access is always a tunnel. Requests carrying
`Cf-Connecting-Ip`/`X-Forwarded-For` are "remote" and get a different surface: they must
mint an identity (guest or PIN), see only their own chats, and are refused every
owner-only endpoint (downloads, updater, TTS, log access). Per-user data is separated by
`_data_base()`.

## Traps

These are the ones that have bitten repeatedly. Most are invisible from the code.

**Verification**
- The Browser preview pane is **Blink**; the shipped desktop app is **WKWebView**. A
  rendering fix verified only in the pane can be wrong in the app.
- The pane is a **hidden document**: `rAF` never fires and CSS transitions never advance,
  so working code looks dead. Inject `transition:none!important` and read the endpoint
  state, or use a `setTimeout` fallback beside every `rAF`.
- Screenshots and computed style lie. Measure — but measure the *right* quantity;
  `getBoundingClientRect` returns the box, not the ink. Use `document.elementFromPoint()`
  to prove a control is actually hittable (a `pointer-events` bug made chips look fine and
  do nothing).

**The single-file client**
- A shared `let` referenced by earlier code throws in the temporal dead zone and **kills
  the entire page silently**. Downstream console errors are noise from the one real abort.
- Several ids (`#about-card`, `#about-name`, …) are **duplicated across dialogs**, so a
  bare `querySelector` returns a hidden copy that measures 0×0. Scope measurement to the
  ancestor. Old id rules from previous layouts are still live and still outranking new ones.
- Appending text after an already-closed CSS comment leaves a stray `*/` that kills that
  rule **and everything below it**. Audit `<style>` blocks after any comment edit.
- The `hidden` attribute is only the UA stylesheet's `display:none` — any author rule
  setting `display` outranks it. Elements that declare `display` need their own `[hidden]`.
- Streaming replaces `innerHTML`, dropping every per-element listener. **Delegate** from a
  container instead.
- Duplicate declarations silently win by order — two CSS rules for the same thing, two
  media queries fighting over the sidebar.

**Editing and shipping**
- Anchor-based scripted edits fail **silently**: a non-matching anchor makes `.replace()` a
  no-op. Assert the anchor exists and that the content actually changed.
- Substring greps miss the real thing — sweeps need `grep -i`, and `"pro" in
  "llama-prompt-guard"` once seated a 22M safety classifier on every council.
- Swallowing an exception and returning `""` turns a dead component into "it had nothing
  to say" — a revoked key showed a green ✓ while every call 401'd. Relatedly, a **probe
  payload that differs from the runtime payload** will validate a key that then fails on
  every real call.
- A bare `Python-urllib` User-Agent gets 403'd by provider edges. "Works in curl, fails
  in-app" is usually a UA fingerprint, not logic.

## Repo hygiene

`.gitignore` covers build artifacts across all three brand generations
(`MillenAI`/`Concorde`/`ConcordeAI` — `.app` bundles, DMGs, Windows zips and
installers); the previously tracked `Concorde.app/` and `Concorde-*-Windows.zip`
were untracked in the ConcordeAI rename. Edit `millenai.py` at the repo root —
any `.app` copy of it is a build output, never the source. The repo is
`bigmillz/concordeai`; GitHub 301-redirects the old `concorde` and `MillenAI`
URLs, which is what keeps pre-rename installs updating.

---

# ConcordeGo (`concorde-travel`) — flight re-ranker

A second product in this repo, unrelated to the desktop app above. **Full brief:
[`docs/design.md`](docs/design.md) — read it before touching anything under
`concorde-travel/` or `fixtures/`.** What follows is the short form: the parts that, if
forgotten mid-session, cause work that has to be thrown away.

**Product name is ConcordeGo, frozen spelling.** Not "Concorde Go", not "ConcordeGO". The
directory and config name is `concorde-travel`. Part of the Concorde family (consumer
assistant, Concorde AI, Concorde VPN).

## What it is

Not a search engine — a **re-ranker**. Inventory comes from a third-party API; the product
is the enrichment and scoring layer on top. Every platform exposes the same filters over
the same inventory. They differ in interface, not judgment. We sell the judgment.

## The model — one number per itinerary, in dollars

```
effective_cost =
    ticket_price
  + baggage_cost(actual bag load, fare family)
  + ground_access_cost(user's geocoded origin, airport, local departure time)
  + (door_to_door_hours * user_hourly_value)
  + comfort_penalties - comfort_credits
  + risk_adjustment(misconnect probability, downside severity)
```

Cheapest / fastest / most comfortable are **not separate rankings** — they are this same
model at different `user_hourly_value` and comfort weightings.

## Hard rules — do not relitigate

1. **The LLM narrates, it does not rank.** Scoring is deterministic arithmetic over
   structured data. The model parses intent into weights on the way in and writes the
   ledger on the way out. Same query + same data = same order, every time.
2. **No unhedged aircraft claims.** Feeds give an equipment code, not a subfleet; one
   carrier's 777-200 may have three cabin configs and three connectivity systems, and
   equipment gets swapped after booking. "Flown as a 789 with the refreshed cabin on 84%
   of the last 60 departures" — never "you will have wifi."
3. **One currency.** Everything resolves to dollars. If you are writing a second ranking
   function, stop.
4. **Show the demotions.** Anything pushed down gets an itemized reason with dollar values
   and a one-click override. Hidden ranking logic feels like a kickback.
5. **Fixtures before APIs.** No live flight API dependency in the scorer or its tests.

## The output format is the product

An itemized ledger per option — a dollar figure, a cause, and the evidence where the claim
is probabilistic:

```
AA 100 · JFK→LHR · $480 ticket · $710 effective
  − $95  EWR at 6:10am: no viable transit, car fare
  − $85  4h10m at CDG arriving 1:40am, terminal closed
  − $60  Older cabin, connectivity unusable over the Atlantic (73% of last 60)
  − $70  Two checked bags not included
  + $80  Nonstop, no misconnect exposure
```

If a number cannot be explained in one line of that form, it does not belong in the model.

## Enrichment layers, easiest first

1. **Ground access** — geocode the *actual* origin, not the city, and model time of day.
   Bushwick→EWR at 6am is a ~$92 car; Bushwick→JFK is $11.75 the sensible way. **The
   original brief's numbers here were wrong and are corrected in `docs/design.md`** — the
   subway is $3.00 and runs 24h, so "transit doesn't run" is not the reason; NJ Transit's
   airport rail stop (~05:00–01:00) is what breaks the chain.
2. **Baggage / fare families** — basic economy plus two bags often loses to the main cabin
   fare it undercut. Pure arithmetic.
3. **Layover quality** — a situation, not a number: airport, terminal, **local clock
   time**, re-clearing immigration/security, terminal changes, margin over MCT, what is
   open at that hour. Four hours at SIN is a feature; four hours at CDG arriving 1:40am is
   a punishment.
4. **Reliability** — US DOT/BTS on-time data is free, by flight number. Model **downside
   severity separately from probability**: misconnecting the last flight of the day is a
   hotel night, not a delay.
5. **Aircraft / cabin / connectivity** — hardest, biggest differentiator.

## Scope

NYC origin, transatlantic long-haul only: **JFK and EWR** (LGA has zero European nonstops
and cannot have any — the perimeter rule bars flights beyond 1,500 miles; it survives only
as a ground-access case), ~12 European arrival airports, ~40 aircraft configs. **Do not
generalize the enrichment data until the scorer works end to end** — that curated data is
the moat.

## Build order

fixtures → scorer → enrichment DB → intent parser → ledger narrator → live inventory
adapter. **All six are done.** Stack is Python, stdlib only —
chosen in `docs/design.md`, and the reasoning there is worth reading before proposing
otherwise. The one exception is the narrator, which uses the official `anthropic` SDK,
imported lazily and entirely optional: with no key and no package the deterministic
template ships and nothing else changes.

## Working on it

```bash
python3 concorde-travel/server.py             # the draft UI, http://127.0.0.1:9897
python3 concorde-travel/scorer.py             # print every fixture's ledger, every profile
python3 fixtures/validate.py                  # fixture schema + semantics
python3 concorde-travel/tests/test_scorer.py  # the fixtures' own assertions, executed
python3 concorde-travel/tests/test_narrator.py   # the narrator's guard rails (no key needed)
python3 concorde-travel/tests/test_adapter.py    # live normalisation, replayed offline
python3 concorde-travel/tests/test_live.py       # quota, cache and key handling, offline
python3 concorde-travel/live.py status           # key, quota and cache state
python3 concorde-travel/live.py probe            # validate a key with a REAL search
python3 concorde-travel/tests/test_faststart.py
python3 concorde-travel/adapter.py               # normalise the recorded payload, print coverage
```

**Ports 8884–8930 are the model engines' — never bind there.** ConcordeGo uses 9897.

Three things about the scorer that look like fussiness and are not, each with a comment in
`scorer.py` saying why: money is **integer cents end to end** (float accumulation makes
ties sort by rounding noise); timestamps carry an **explicit UTC offset** and no timezone
database is ever consulted (01:30 on 2026-11-01 at JFK happens twice); and **unknown is
never zero** (otherwise the least-documented itinerary accumulates the fewest penalties and
wins). Every constant lives in `scorer.Tuning`, never in a fixture — a fixture carrying the
curve it is meant to be testing is a fixture that tests nothing.

**The test suites are mutation-tested.** Before changing either, know that `test_scorer.py`
catches 12 of 12 seeded faults and `validate.py` 10 of 10. If a change makes a suite pass
that should not, the suite has lost a guard.

**The narrator is the only non-deterministic surface, and it is fenced.** `narrator.brief()`
precomputes every number the prose may contain — the model picks words, never arithmetic —
and `narrator.verify()` rejects any sentence containing a figure, percentage, duration or
airport the brief did not supply, plus a list of unhedged certainty phrases (hard rule 2).
Anything rejected falls back to the deterministic template. Do not relax the verifier to
make nicer copy: it is the entire reason a model is allowed near this product. Note there
is no `temperature` to reach for — it is removed on Claude Opus 5 and returns a 400.

## The live adapter, and what a feed does not give you

`concorde-travel/adapter.py` turns a third-party search into a scenario the scorer already
reads — a live search and a hand-written fixture arrive at `score()` in the same shape, and
`server.py` runs both through one `_score_scenario()`.

**Hard rule 5 still holds, and is now enforced rather than trusted**: `test_adapter.py`
asserts the scorer and its tests never import the adapter, and the adapter's own tests
replay `adapter_samples/kiwi-jfk-lhr.json` — a real response captured once — instead of
calling anything.

Measured against that real payload, the feed supplies the skeleton and the price and very
little else:

- **No UTC offset on any timestamp.** The scorer's time model requires one, so the adapter
  attaches it from `enrichment/airports.json`. Without that join the feed cannot satisfy the
  schema at all.
- **Marketing carrier only.** `KL6101` on JFK–LHR is a codeshare; KLM does not fly it. A
  four-digit number under a two-letter code is the signature — 11 of 13 segments in the
  sample.
- **No equipment code anywhere**, so every aircraft, cabin and connectivity claim abstains.
  Hard rule 2 is not a preference here: the data needed to break it does not exist.
- **Baggage as a count already priced in**, and no fare brand — so the bags-versus-fare
  arithmetic, the cheapest real win in the product, has nothing to work with.

`adapter.coverage()` reports this per search and the page prints it. **A grade computed over
mostly-abstained enrichment is not the same object as one over a curated route** — do not
quietly drop that strip to make the results look more confident.

`enrichment/` is the curated moat: airports (zones with DST transition dates, MCT, service
hours, inter-terminal), carriers (reviewed ratings), ground access by origin and hour, and
route par. **An airport that is not curated drops the itinerary rather than being guessed**,
which is the scope discipline working, not a bug.

## The live API: key, quota, cache

`concorde-travel/live.py` is the only module that holds a credential. It answers one
question — may we call the provider right now, and what came back — and hands the payload
to the adapter. The scorer never sees it.

**Setting a key**: `export CONCORDEGO_FLIGHT_KEY=...`, or put one in
`~/.concordego/cloud.json` (written 0600, created empty by `live.py status`). The env var
wins, so a key need never touch disk. `/api/live` uses the API when a key is configured and
the recorded sample when it is not — the interface never offers a button that cannot work.

**The provider's wire format lives in that config, not in code.** The shipped Tequila
profile is written from documentation and is **unverified against the live service** — every
Kiwi host was unreachable from the machine this was built on. If the parameter names are
wrong, fix the profile; you should not have to edit Python to correct someone else's query
string.

Four behaviours that are deliberate, each with a comment in `live.py`:

- **The probe uses the runtime payload.** `probe()` runs a real `search()`. A validation
  call shaped differently from the real one blesses keys that then fail on every request —
  this repo has paid for that once already.
- **Cache before quota.** A cached answer costs nothing, so it must not spend a call.
- **Quota is reserved before the call, not counted after.** A request that dies mid-flight
  has already cost you. Only a connection that never reached the provider is refunded.
- **The key never leaves the module.** Not into a log, an error, the page, or git.
  `redact()` runs over everything that escapes, including the provider's own error echo.

Counters live in `~/.concordego/quota.json` and roll over by day and month on their own —
no cron, no cleanup job. Defaults are 100/day and 1000/month with a 2s floor between calls
and a 30-minute cache TTL; all four are config.
