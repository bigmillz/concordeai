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

## The grade is a report card; the order is the dial — do not conflate them

**The letter on the page is `scorer.report_card()`** (2026-09-21, per Patrick, after a
Thanksgiving-eve search came back all F on the value grade: "every plate shows up as an F
when they're definitely not ... grades that each flight gets objectively in each category
and not just price"). Six categories, each on its OWN absolute rubric, and the letter is
their weighted grade-point average: price 30% (ticket and the party's bags against the
route's fair fare, the modelled reference fare with its season and holiday), speed 15%
(door to door against the nonstop reference), getting there 10% (each end's chosen way in,
on its fare and time), comfort 20% (pitch on the long leg, the cabin, wifi and power as
published, the reviewed carrier rating, a red-eye), routing 15% (nonstop, or the
connection's hours, margin, misconnect exposure and whether it is one ticket), reliability
10% (the on-time record of the worst leg; no record reads as a B). The hover card shows
the six letters, their weights and one sentence each. Price is the heaviest weight and
still under a third, so one dear ticket is not an F and one bargain is not an A;
`test_scorer.py` asserts both from the weights, and that a nonstop is an A+ for routing.

- **It is still ABSOLUTE.** Nothing in the card looks at the other results, so the same
  flight earns the same card whatever else the search returned; `test_scorer.py` re-cards
  every option against subsets of the result set. Deriving anything from a percentile of
  what came back is the obvious-looking change that quietly makes every grade relative.
- **The value grade still exists.** `scorer.grade()` is effective cost at the reference
  profile against par, on `Tuning.grade_bands`; the ledger tests and the fixtures'
  expectations are written against it, and `_view` ships it as `value_grade`. It is not
  shown. Do not relitigate which is "right": the card is what Patrick asked to see, and the
  value model is what ranks.
- **The ORDER is effective cost at the user's target** (cheapest / fastest / comfort).
  A C can and should outrank an A when the user asked for cheapest. Switching target
  re-orders the list and never moves a letter. The card never ranks anything.

**Par is a specification, not a percentile — and it is MODELLED for any route on Earth**
(since 2026-09-20). `concorde-travel/par.py` builds the route's reference itinerary — nonstop,
main cabin with one bag, a carrier at the baseline rating, 31-inch pitch, from the city
centre, leaving when that market actually flies (an overnight eastbound across an ocean,
mid-morning otherwise) — and scores it through the real scorer at the reference profile. The
only modelled input is the reference **fare**: distance through marginal per-mile yields that
fall with stage length, the competitive market the route sits in (transatlantic, intra-EU,
domestic-NA, … 26 classes), and the month. `par_for()` takes an origin, a destination and a
date and **nothing else**. Beyond nonstop range (~8,800 mi) the reference allows one
connection, or no real flight could reach it.

**Par knows holiday weeks** (2026-09-21, after a Thanksgiving-eve search came back all F against
a par built for an ordinary November): `par.holiday_factor()` multiplies the reference fare
for Thanksgiving week (×1.55, the Wednesday before and the Sunday after ×1.75) on any route
with a US end, Christmas week ×1.35 and New Year ×1.25 everywhere, Easter weekend ×1.25 on
European routes, and the US summer holiday weekends ×1.2. The basis names the holiday and the
page's par sentence says so. A fair fare on the day before Thanksgiving IS higher; that is a
property of the date, so it belongs in par, never in the grade bands. **The hover card's six
category letters average to the overall** by construction (`PULL` equals the number of
factors, so the mean of the parts' indices is the whole's index); before, the parts read a
grade better than the whole.

Two consequences that look wrong and are not: **par is seasonal** — a $500 ticket in July is
a better deal than the same ticket in November and the letter says so; it is still absolute,
because the same route on the same date has one par whatever the inventory. And **the
curated `routes` table in `enrichment/ground.json` is empty on purpose** — a row there
overrides the model, and the eight numbers it used to hold were guesses that put 57% of a
real search at A+. The basis ships with every search (`_par` in the scenario, `fixture.par`
on the wire, one sentence on the page). Calibrating par off the distribution of a search's
results is the obvious-looking change that quietly makes every grade relative, which is the
one thing the grade must never be; `mutate_adapter.py` seeds exactly that fault and the
adapter suite catches it.

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
AA 100 · JFK→LHR · $480 ticket · $710 all in
  Getting there and back
    + $95  EWR at 6:10am: no viable transit, car fare
  Add-ons
    + $70  Two checked bags not included
  Comfort and risk
    + $85  4h10m at CDG arriving 1:40am, terminal closed
    + $60  Older cabin, connectivity unusable over the Atlantic (73% of last 60)
    − $80  Nonstop, no misconnect exposure · credit
```

If a number cannot be explained in one line of that form, it does not belong in the model.

**The bill on the page is MONEY ONLY** (per Patrick, 2026-09-21: "we can't tack on money
because of the pitch of the seat", and "the person knows how long it is already"). The
scorer still prices time, comfort and risk in dollars — that is the model, and it still
decides the ORDER at the dial and makes the GRADE — but those lines never appear as
charges on the bill. Mock 10 keeps them off-bill (`r.q`, and the time term inside
`r.rank`) and shows them two other ways: the pros and cons, and the grade's hover card,
which gives each part (price, speed, getting there, comfort, routing, reliability) its own
letter, read on the grade's own bands from that part's pull against par's same part. The
ledger format below is the scorer's, which the draft page still prints in full.

**Added costs are "+"; only a credit is "−", and it says "credit"** (per Patrick,
2026-09-20: a minus in front of a cost reads as a discount). **The bill is grouped**
by what the money is for — ticket, getting there and back, staying over, add-ons,
your time, comfort and risk — with a subtotal per group, and it is **editable**: the
add-ons the user's own choices imply are put in automatically (a wifi pass when they
asked for wifi and this fare charges for it, a hotel when a layover runs overnight, a
car when they said rideshare), every one of them has an × that takes it off every
flight, and "add an expense" at the bottom adds more, from five extra bags priced off
this fare's own ladder to a lounge pass to a line of their own. Nothing is charged
silently and nothing is removed silently: a removed line leaves a chip to put it back.

**Every estimate is marked** (2026-09-24, per Patrick, a standing rule: "anything that's an estimate, keep me in the
loop on it, but also do the asterisks", and never make anyone dig through terms to learn it). A figure that is a
typical price rather than this fare's own, a quote, or a published price carries a small asterisk (`EST` in mock-10,
`sup.est`), and the panel it sits in ends with one plain italic line saying what the asterisk means and that the
traveller can remove the line and add their own: the bill (`.estnote`, "Estimate, not a quote. Found the real
price? Remove the line with × and add your own."), the results (`.estfoot`), the Flight Fixer. Marked today: an
add-on price that is not the airline's own published figure (below), hotel nights (a Google Hotels median is an average, not a quote), rides the
ground model worked out rather than a curated fare (`mode.estimated`), bags priced on the default ladder because
the airline publishes none, the times out the door, the modelled fair fare, and the Fixer's odds and hotel nights.
A bill line carries `est:true`; `hasEst(r)` says whether a bill holds any. Tell Patrick about every new estimate.

**Add-on prices are the airlines' own where they publish one** (2026-09-25, per Patrick: "if you have any other way
to come up with more accurate prices for those, then I'm all ears"). `enrichment/addons.json` holds wifi, seat choice,
extra-legroom, priority boarding and lounge prices per airline, by haul, researched from the airlines' own pages and
re-checked by a second reader (sources, dates and corrections in each row), and insurance as a share of the ticket
(4 to 10%, 6.5% typical: the insurers' trade association and Squaremouth's own sales). `addons.page_table()` strips
the sources and `build.py` inlines the prices as `__ADDONS__`; `addonPrice` in mock-10 reads the airline flying the
longest flight, short-haul being a trip inside one border or with no flight over four hours. A price the airline
publishes exactly, or the top of its published range (`upto`), carries no asterisk; a "from" price, a figure from a
secondary source, free wifi on only part of a fleet and a price in doubt (Air Canada's lounge page names no currency)
keep it; an airline that does not sell the thing (Delta's Sky Club day passes, Virgin's Clubhouses) says so and prices
the typical alternative, marked; everything else falls back to the typical price (`typical` in the file, or the
page's flat figure), marked. A price published for one haul never stands in for the other: Air Canada's free wifi is
within North America, so across the Atlantic the typical pass stands. Wifi free to members of a free loyalty
programme is $0 on the bill, naming the programme (Delta, United on Starlink, Southwest, Air Canada, Air France,
KLM in Europe, SAS; JetBlue for everyone). Most airline pages refuse automated readers and the Internet Archive was
not reachable from the research session, so American, United, BA, Lufthansa, Air France, KLM, Iberia, Aer Lingus,
Turkish and Icelandair's seat and lounge prices are still the typical ones: a person with a browser can fill them
in. `tests/test_records.py` runs the page's own pricing under node, and mutants guard each rule.

**Ride fares were checked against the operators and regulators** (2026-09-25). `ground.json`'s transit fares were
read from the operators' own pages and converted at the ECB rate of the day (the Paris airport ticket EUR 14, the
Elizabeth line GBP 14.60, Lisbon's metro with its card, Madrid's with the EUR 3 airport supplement at the top of its
range); the Aerobus and the Schiphol night train run all night; the Malpensa Express's last train from the airport is
22:09. The official taxi tariffs showed the fare bands under the real fare in four countries, which the "lean high"
rule forbids: Spain and Italy moved to the upper band, the Netherlands to the high band, Turkey to the mid band
(`ground.py`, with the tariffs in its comment), and `test_records.py` checks the model against Madrid's, Barcelona's
and Istanbul's official fares. Rideshare prices have no published source anywhere and stay estimates.

**The airline extras checklist** (2026-09-25, per Patrick) is a claude.ai artifact, https://claude.ai/artifact/96dDdGVmSX8aqQSquH9TnU,
with a `db` collection `rows`: 143 prices the site still guesses (seat choice, extra legroom, wifi, lounge day passes,
priority boarding for 31 airlines, and the 12 unverified bag-fee rows), each with the airline's own page, what to
read there, what the site uses now, and the second reader's note. Patrick types the published price into a row
(`entered`: amount, currency, exact / from / up to, haul or bag piece, note, or not sold); read them back with the
ArtifactData tool (`list` on `rows`) and write them into `enrichment/addons.json` or `fares.json` with their URL and date.

**Hotel prices are real averages** (2026-09-24, per Patrick): `GET /hotels?near=IATA&date=` or `?q=place&lat=&lon=&date=`
(outside the door) asks Google Hotels through SerpApi (`live.serp_hotels`, the same plan and counters as the flight
supplement) and answers `server.hotel_summary`: the MEDIAN nightly rate, taxes in, of up to twenty hotels (never
holiday rentals) within 10 km of the airport, with the middle half's range, or nothing when fewer than three are
priced. Cached twelve hours per place and night; an uncached lookup counts against `CONCORDEGO_HOTEL_CALLS` (10 a
day site-wide, so the flights keep most of the SerpApi day; raise it with the plan) and a dozen a day per address.
The page asks for the overnight-layover hotel (`hotelNight`, the night being the landing date, or the evening before
when landing in the small hours) and the "Hotel at destination" add-on (`destHotel`, near the address or the city);
until the answer arrives, or opened as a file, the flat $180 stands, marked. First real answer: 18 hotels near
Heathrow on 18 Nov 2026, median $91 a night.

**US DOT on-time records** (2026-09-24, per Patrick, who approved downloading a year of the Bureau of Transportation
Statistics' "Reporting Carrier On-Time Performance" files). `concorde-travel/ontime.py build ZIP...` boils twelve
monthly zips (about 30 MB each, downloaded to a scratch folder and deleted after, never committed) into
`enrichment/ontime.json.gz`: each flight number's record on each route (scheduled, arrived within 15 minutes with
cancelled and diverted counted as late as DOT counts them, cancelled, median and 90th-percentile arrival delay;
kept at 20 or more flights a year) plus each airline's route across all its numbers, and the Flight Fixer's delay
table (above). Only US airlines within the US are in it. `adapter.join_ontime` puts the record on every segment it
covers, in place of the feeds' abstain (a regional flight is looked up under its parent's regional airlines, from
`points.json`'s `regional` table), so the grade's reliability part reads a real on-time share and the missed-connection
risk real delay percentiles; a flight DOT does not cover keeps its priced abstain. Rebuild monthly-ish with the newest
twelve months (the files lag about two months). `tests/test_records.py` fences hotels and records offline.

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
python3 concorde-travel/par.py JFK LHR 2026-11-18   # the reference itinerary and its ledger, any route
python3 fixtures/validate.py                  # fixture schema + semantics
python3 concorde-travel/tests/test_scorer.py  # the fixtures' own assertions, executed
python3 concorde-travel/tests/test_narrator.py   # the narrator's guard rails (no key needed)
python3 concorde-travel/tests/test_points.py     # award charts and the page's transfer planner, offline
python3 concorde-travel/tests/test_records.py    # hotel averages and US DOT on-time records, offline
python3 concorde-travel/points.py JFK LHR DL business 2026-11-18   # what the published charts say for one flight
python3 concorde-travel/tests/test_adapter.py    # live normalisation, replayed offline
python3 concorde-travel/tests/test_live.py       # quota, cache and key handling, offline
python3 concorde-travel/live.py status           # key, quota and cache state
python3 concorde-travel/live.py probe            # validate a key with a REAL search
python3 concorde-travel/live.py provider duffel  # switch provider profile
python3 concorde-travel/tests/mutate_adapter.py  # break the adapters on purpose, 26 faults
python3 concorde-travel/capture.py JFK LHR 2026-11-12  # real search -> scrubbed sample + report
python3 concorde-travel/tests/test_faststart.py
python3 concorde-travel/tests/test_timeline.py   # needs a running server; skips without one
python3 concorde-travel/adapter.py               # normalise the recorded payload, print coverage
python3 concorde-travel/ui/mock/data.py [capture]  # score a capture into the mockups' slim.json
python3 concorde-travel/ui/mock/build.py         # inline it into mock-N.html
python3 concorde-travel/ground.py "Shoreditch" LHR 09:00   # ground estimate for anywhere
python3 concorde-travel/places.py "Shoreditch, London EC2A"  # what a typed place resolves to
```

**Ports 8884–8930 are the model engines' — never bind there.** ConcordeGo uses 9897.

**Public address:** the domain is **flyconcordefly.com, with the e** (a day was lost to
`flyconcordfly.com`, a zone that does not exist; Cloudflare's answers were all correct).
`concorde-travel/deploy/launch.sh` does the whole thing from a laptop in one command (doctl
makes or finds the droplet; Cloudflare's API makes or finds a remotely-managed tunnel,
sets its ingress, reads its token and writes the proxied CNAME; the installer runs over
ssh; the keys go over the ssh channel into `/etc/concordego.env`, never a command line;
`access.sh` adds the door; then DNS and HTTPS are checked). It needs
`DIGITALOCEAN_ACCESS_TOKEN`, Cloudflare credentials that can see the zone, and `ALLOW`. The
sandbox this was written in reaches neither cloud and holds no such keys, so its first real
run is on Patrick's laptop; it was dry-run against stubbed doctl, ssh and Cloudflare.
`concorde-travel/deploy/droplet.sh BRANCH TUNNEL_TOKEN` is what it runs on the box (an Ubuntu
droplet: systemd, a venv for the `anthropic` SDK, an hourly `concordego-update.timer` that
resets the checkout to `origin/BRANCH` and restarts the service when HEAD moved; since 2026-09-24 every git and pip step in it runs as the `concordego` user through `runuser`, root only restarts the service, and there is no `safe.directory` exception anywhere, so root's git refuses the checkout: root must never run git in a tree the web user can edit, whose `.git/config` could name a hook or an fsmonitor, ufw with
ssh only, and cloudflared run from a tunnel token made in the Zero Trust dashboard, whose
public hostname owns the DNS record so no script here touches DNS). **The box is locked
down three ways** (2026-09-21): a DigitalOcean cloud firewall named `concordego` in front
of it (inbound TCP 22 only, everything outbound; the site needs no open web port because
the tunnel dials out; `launch.sh` creates it with doctl, or attaches a new droplet to the
existing one), ufw on the box with ssh only, and sshd by key only, root included
(`/etc/ssh/sshd_config.d/50-concordego.conf`). Security updates are unattended
(`/etc/apt/apt.conf.d/52concordego-unattended`: the security channels only, unused kernels
and dependencies removed, an automatic reboot at 09:30 UTC, 05:30 New York, only when a
kernel asks); `droplet.sh` writes all of that on a fresh box. For a full update beyond the security channels,
`concorde-travel/deploy/update-system.sh` (2026-09-24, run by hand for now) does update, full-upgrade keeping
edited config files, autoremove --purge, autoclean and snap refresh, waits for the nightly updater's lock,
logs to `/var/log/update-system/`, and reboots only with `--reboot`; `--dry-run` changes nothing. From a laptop it is
copied to a temp file and run with stdin closed (piped into `bash -s`, a package's install script can read the
rest of it off stdin): `ssh root@67.207.85.212 'f=$(mktemp) && cat > "$f" && bash "$f" --reboot < /dev/null; rc=$?;
rm -f "$f"; exit $rc' < concorde-travel/deploy/update-system.sh`; better, copy it and start it under
`systemd-run`, as its header says, because an ssh session that drops mid-run takes the run with it (2026-09-24,
while a 453 MB droplet with no swap built a kernel's boot image; the script now adds a temporary 1 GB swap file
on a box that small). **System updates from the admin page** (2026-09-24, per Patrick: check nightly, never
install automatically, two buttons): `deploy/install-sysupdate.sh`, run by `droplet.sh`, installs a root-owned
copy of the script in `/usr/local/sbin` (the checkout belongs to the web user; re-run the installer to update
it), `concordego-updates-check.timer` (07:00 UTC and 3 minutes after boot, `--check`: installs nothing, writes
`/var/lib/concordego-sys/status.json`), and `concordego-sysupdate.path`, which starts the update when the page
writes the ONE word `update` or `reboot` to `/var/lib/concordego/sysupdate-request`. The web process never runs
anything as root; root never writes where the web user can. "Pending" counts what an upgrade would install now;
Ubuntu's phased updates are listed apart as held back. A **Memory** card (2026-09-25, per Patrick: "keep me in the loop on memory on the server in case we need to bump it
up") shows the box's free memory, the service's size now and at its peak, and swap, from `/proc` (`server.memory_status`);
it turns orange under `MEM_WARN_MB` (120 MB) free. The page shows an orange "Updates pending (N)" button
opening Update (site stays up) and Update and restart (shown when a kernel or C library is involved or a reboot
is waiting). Every admin POST must carry `X-ConcordeGo-Admin: 1`, which only the page's own fetch sets, and the
POST body is read before any refusal (an unread body on a kept-alive connection became the next request line).
The security-only unattended upgrades of 2026-09-21 are unchanged.

**Every key lives in `/etc/concordego.env` on the droplet** (0600, read by systemd, never in
the repo, the plist, a command line or chat): `CONCORDEGO_FLIGHT_KEY` (Duffel),
`ANTHROPIC_API_KEY` (narrator and wish box), `CONCORDEGO_PEXELS_KEY` (tile photos),
`CONCORDEGO_SERPAPI_KEY` (the Delta supplement), `CONCORDEGO_AERODATABOX_KEY` (the Flight
Fixer's tracking feed), plus `CONCORDEGO_OWNERS`,
`CONCORDEGO_ACCESS_TEAM` and `CONCORDEGO_PUBLIC=1`. To add or change one, edit the file ON
THE SERVER and restart, from a laptop Terminal:

```bash
ssh -t root@67.207.85.212 'read -rsp "Key: " k && echo && sed -i "/^CONCORDEGO_SERPAPI_KEY=/d" /etc/concordego.env && printf "CONCORDEGO_SERPAPI_KEY=%s\n" "$k" >> /etc/concordego.env && systemctl restart concordego && echo done'
```

It prompts without echoing, so the key never reaches a command line, shell history or the screen; it drops the old
line first (an `echo >>` leaves two); and `read` trims stray spaces. A hidden prompt DOES keep keyboard codes: on
2026-09-24 the laptop copy of a new AeroDataBox key arrived with four Option/Control-arrow codes (`\x1b[1;7C` and
kin) in front of it. Check the value's length afterwards, never the value (`cut -d= -f2- | awk '{print length}'`;
a RapidAPI key is 50), and compare copies by a sha256 prefix.

No spaces around the `=`, no quotes round the value, and `export` in lower case if the line
carries it at all (the laptop's `~/.concordego/env` is sourced by a shell, and one `Export`
with a capital E silently broke every line after it, 2026-09-21). Editing a copy on the
laptop changes nothing (2026-09-21: an hour went on a key that was never on the box).

**The AeroDataBox key** (the Flight Fixer's tracking feed) comes from RapidAPI, not from
AeroDataBox itself: subscribe to AeroDataBox at rapidapi.com (the free tier covers a few
hundred calls a month, the first paid tier a few thousand), copy the `X-RapidAPI-Key` the
subscription shows, and set it as `CONCORDEGO_AERODATABOX_KEY` on the droplet with the
command above. The server sends it as the RapidAPI header, never in an address; `live.py
status` reports `aerodatabox.key_configured`; and `live.py flight DL5048 2026-09-21` spends
one call to prove it works (it did, on 2026-09-21). Its allowance is `aerodatabox_quota` in
`cloud.json`, 40 a day and 300 a month by default, with a five-minute cache. A key pasted into a
chat is a key to roll. `launch.sh` carries the same variables from the laptop's environment
to a new box over the ssh channel; `live.py status` on the box says which keys it sees
(`serpapi.key_configured`, never the value), and `live.py serp JFK LHR 2026-11-18` spends
one SerpApi call to prove the key works. `concorde-travel/go-live.sh`
(a Mac, LaunchAgents) is the laptop fallback. Either runs `server.py` with
`CONCORDEGO_ROOT=mock-10`, so `/` serves the current interface mockup, at
**go.flyconcordefly.com**, on its own tunnel so it never touches the AI app's. **`/admin`** on
the served site (owners only: an email in `CONCORDEGO_OWNERS`, or the local machine on a
developer's box; on the public box `CONCORDEGO_PUBLIC=1`, written by the installer, makes
every request remote, so a request that somehow arrives without the proxy's headers is a
stranger, never the owner. The Access application for `/api` allows exactly the owners'
emails; an incognito window that opens it has an Access session of its own, or the WARP
client signed in for the team, and the admin page now prints who it takes you for) shows the live commit, asks GitHub what is
new, updates now (`git reset --hard origin/<branch>` in the running checkout, then exit: the
supervisor restarts it, so the service unit lists the checkout in `ReadWritePaths`) and
tails the journal (`concordego` user is in `systemd-journal`) or `CONCORDEGO_LOG`. Owners
are not metered. **Keys never go in the
LaunchAgent plist** (world-readable): the agent sources `~/.concordego/env` (0600, written
empty by `go-live.sh`) before starting, so `ANTHROPIC_API_KEY` and the Duffel token go
there (or the token in `~/.concordego/cloud.json`); the droplet's equivalent is
`/etc/concordego.env`. The Mac script runs `server.py` on its own venv at
`~/.concordego/venv` with the `anthropic` SDK installed into it, because Homebrew's python
refuses `pip install` (PEP 668); the tunnel is looked up by exact name from cloudflared's
JSON, since grepping its table once missed an existing tunnel and the create that followed
failed. **The door is Cloudflare
Access**: `concorde-travel/access.sh` creates the app, a one-time-PIN sign-in (Google too
once that provider exists) and an email allowlist through the Cloudflare API. A request
that arrives through the tunnel carries `Cf-Connecting-Ip`, and Access adds
`Cf-Access-Authenticated-User-Email` for whoever signed in; the origin listens on loopback
only, so that header cannot be forged from outside. `server.py` spends nothing for an
anonymous visitor (a live flight search, the narrator, the wish box, a sky download), gives
a signed-in person a daily allowance (`CONCORDEGO_USER_SEARCHES` 20, `CONCORDEGO_USER_WISHES`
200, counted in `~/.concordego/users.json`), and lets the machine's owner spend freely.
Recorded results serve to anyone. `/api/whoami` tells the page who it is talking to.

**Mock 10 runs real searches when served** (`/api/search`): what the wizard holds (the
address, the destination, the date, travellers, bags) goes to the server, which resolves
the places, fetches (the provider with a key, the recording without one, and the lede says
which), and returns everything the page reads via `data.build_slim()` — the same builder
that writes `slim.json`, so a live search and the checked-in sample are one shape. The page
rebuilds every derived table from the response (`loadData`); a typo comes back to the form
with the sentence from `places.py`. Opened as a file, or by anyone Access did not sign in,
the inlined recording is all there is.

**A round trip is three steps, and the last is one link** (per Patrick, 2026-09-21: the old
sheet dead-ended on one leg). The details sheet's button selects the leg ("Select this
outbound"), the stepper advances, and the return is its own search the other way round
(`fetchLeg`: the destination city as origin, the origin address as destination, the return
date) landing in `LEGDATA[1]`; with no live search possible the outbound recording is
mirrored (`mirrorData`) and the lede says so. Picks are snapshots (`S.legPick`), so going
back to a leg and choosing again is cheap. The book step (`bookHTML`) shows both legs and
their picks, cash or points, and the link: one airline for the whole trip gets one link to
its own site (`DEEP` fills the airports and dates in for the few whose search pages take
them in the address; the rest open the site with the itinerary to enter, copyable); two
airlines get two links and a sentence saying two tickets is normal; a points leg gets the
transfer walkthrough. The airline's own domain comes from the feed's conditions-of-carriage
URL, falling back to `SITES`.

**Round-trip fares** (2026-09-24, per Patrick). The return search carries the chosen outbound (`pairFor`, validated
field by field in `server._round_trip_totals`), and Google is asked for the round trip in two calls
(`live.serp_round_trip`: the outbounds, then the returns for that outbound by its `departure_token`). Where a return
sold with that outbound costs less than the two one-ways, `data.build_slim` makes its ticket what the round trip adds
to the outbound, BEFORE scoring, and the card, the sheet and the book step say it is one round-trip ticket (one link,
on the outbound airline's site). A new outbound clears the returns priced for the old one. The feed is not asked: a
two-slice Duffel request returns every outbound-and-return pair at once, and on its first live test that reply grew
past the 453 MB droplet's memory and the kernel killed the service. `live.MAX_RESPONSE_BYTES` (15 MB; a one-way reply
is at most about 5 MB) now refuses any reply that size before it is parsed. First live result: with AA100 out,
BA117 home was $965 one-way and $526 as part of an $819 round trip; the cheapest trip that day was still two one-ways.

**Other countries' sites are LINKS, never prices** (2026-09-24, per Patrick, "option 2"). The old panel was a demo:
its prices came from a seeded formula and it never searched another country. SerpApi cannot make it real: asking
Google as India or the UK returns the US fares converted (every flight moved by the same percentage), and no lawful
feed we can get sells a point-of-sale choice. "Show where to check" (`#rgo`, `marketsHTML`) now lists the top three
flights with a chip per country (`MARKETS`: UK, Mexico, Brazil, South Africa, India, Türkiye, Australia) linking the
airline's own site for that country, only where the address pattern was checked (`MARKET_SITES`, five airlines);
every other airline gets its main site and "switch the country". Each country carries its catch (the UK's departure
tax, a foreign card's fee). It shows no price and costs no call. Google already prices at the departure city, so a
return leg that starts abroad is already that country's fare. **Travelpayouts' `market` parameter was TESTED and does
not give other-market prices** (2026-09-24, a real token, `CONCORDEGO_TRAVELPAYOUTS_TOKEN` on the droplet, partner ID
781552): across 22 market codes and eight routes the Aviasales Data API returned the same flights at the same prices;
`in` and `za` returned exactly what an invented code `xx` returns, even for Delhi to London; the only differences
were `us` rows seen more recently and `ru` rubles-conversion noise. Its data is also sparse (nine NYC to London fares
for a whole month) and days old. Skyscanner's Travel API does price by market, but it takes established businesses
with over 100,000 visitors a month, forbids background sweeps and caching, and is free (they pay commission).

**Who sells this fare** (2026-09-24, per Patrick). Google's cheapest price is sometimes a travel agency's. The details
sheet has "Check who sells it" (`sellersHTML`) when the flight came through Google (`e.seller_token`, from
`data.build_slim`) on a live search (`seller_query` on the response) and is not a round-trip fare. It POSTs
`/sellers` (`/api/sellers` inside the door), one SerpApi booking-options call, cached, on its own allowance
(`CONCORDEGO_ANON_SELLERS` 5 a day per address, `CONCORDEGO_USER_SELLERS` 30 signed in). `server.sellers_request`
validates the token, airports, date and flight numbers before spending anything; keeps only sellers of the whole
one-way fare; marks a seller whose flight numbers differ as a codeshare (listed apart, never used for the price);
and passes Google's "Go" link on only when it points at https://www.google.com/. The page lists the airline first,
each agency with how much less it asks, and the agency caveat. Once checked, the bill's ticket group gets a line:
the airline's own price by default, or, with "Include travel agency prices" ticked (`AGENCIES`, this browser only),
the cheapest agency as a credit that names it. A difference under a dollar adds no line.

**A served page never shows made-up results** (per Patrick, 2026-09-21). Anyone gets
`CONCORDEGO_ANON_SEARCHES` (4) real searches a day, counted per address at `/search`, which
lives outside `/api` so Access lets it through; past that the page opens the account pop-up
with the wait in hours ("today's free searches are used up"). The recording stands in only
when the page is opened as a file, or on a developer's machine with no key; a served
request with no key is refused with a sentence rather than faked. The wish box, the price
check and the fuller allowance need a sign-in: the page greys them and a tap opens the
pop-up that says what an account gives (searches 20 a day, wishes 200, price checks 2, a
watchlist of 5 routes, coming) and sends the person to `/signin`, which Cloudflare Access
protects; `access.sh` makes the applications — `/api/*`, `/signin` and `/admin` behind the
email allowlist, the page itself open with a bypass policy — and the server counts each
signed-in person's day. `/whoami`, `/locate` and `/photos` sit outside the door too. **Access stamps the email only on
requests to an application that demanded a sign-in**, and passes the open part of the site
through unstamped, so a signed-in person looked anonymous there and was sent round to sign
in again (2026-09-21). The server therefore reads Access's own cookie, `CF_Authorization`, a
JWT signed with the team's key, and verifies it in the stdlib (RS256 against the team's
published keys at `<team>.cloudflareaccess.com/cdn-cgi/access/certs`, cached an hour; the
issuer; the expiry) before trusting its email. `CONCORDEGO_ACCESS_TEAM` names the team;
`launch.sh` reads it from the account and writes it to the droplet's env. **There is ONE
protected Access application, `/api`**, with the sign-in (`/api/signin`) and the admin page
(`/api/admin`) inside it: a browser fetch cannot complete a second application's login
redirect, which is what broke the admin page when it was its own application. `/signin` and
`/admin` redirect into it. Unlimited use is bound to an email, never a key (a test key
existed for an hour and was removed as a second way in): `CONCORDEGO_OWNERS` are unlimited and
may open the admin page; the admin page's own list (`~/.concordego/unlimited.json`) makes
other signed-in emails unlimited without admin access. Signed-in people get a Log out button (Access's own logout) and their three most recent
trips as chips under the form (`recent` in `users.json`, five deep, survives the day's
rollover).

**Three things a signed-in person carries** (2026-09-21, per Patrick). **Status is a
credit in the order**: a held tier (own programme, or an alliance tier the operator belongs
to) gives a flight that keeps it earning a credit at what the traveller says keeping it is
worth (`S.statusWorth`, shown since 2026-09-23 as "Prefer airlines where I have status: Off / A little / Some / Strongly", which are $0 / $30 / $60 / $150, Some by default; per Patrick the dollar picker was unreadable), named on the row
("Keeps your SkyTeam Elite Plus") and in the ledger, never on the money bill and never in
the grade. **The flying personality** is seven chip questions (`QUIZ`) that become a persona:
a point on the dial plus its own priced terms (`personaLines`: a nervous flyer's
connections, red-eyes and low-rated carriers; a foodie's long daytime layover in a food
city as a credit; wifi and power; "never" red-eyes or connections; the lounge question went on 2026-09-24,
per Patrick, because lounge access was guessed from the fare's name), offered as a
"Me" preset beside cheapest, fastest and comfort. It ranks by the same arithmetic, every
term named on the row, and the grade never sees it. Saved to the person's profile on the
server (`/api/profile`, `users.json[email].profile`, sent back by `/whoami`) or this
browser's storage without an account, and sent up on sign-in. **The profile** is behind
the avatar (the account button becomes the person's initial): who you are, the allowance,
the personality, Log out. **A first-launch tour** (`TOUR`, once per browser, when the splash
lifts) says the premise in a breath, how it works in three lines, and hands off to the quiz.

**The Flight Fixer** (`rescue.py`, `/rescue` outside the door on the free allowance,
`/api/rescue` inside it; the fourth trip type beside round trip, one way and multi-city,
whose form is the search's own fields with the required ones marked and a line saying the
more you add the surer the call). It also puts two numbers on the day (`rescue.odds()`), each with its own line
chart open on the result and both FALLING through the day (per Patrick, 2026-09-21: at nine there is a day of
chances, at ten to midnight there are not): **the chance of making the updated flight** (if it slips to hour h, the
chance it still goes today; anything past midnight is lost). **For a US domestic flight it comes from US DOT
records** (2026-09-24, per Patrick; `ontime.py`, `rescue._dot_model`): of the flights from the same airport,
scheduled in the same three-hour block, that were already at least as late, the share that left within each
further stretch of time. The files cannot date a cancellation, so how many already-late flights ended cancelled is
MEASURED another way (2026-09-25, per Patrick: "22 to 52% is pretty wide"): every row carries the plane's tail number,
so `ontime.build` follows each plane through its day, and a flight whose plane landed from its previous leg too late
for it to leave sooner (landing + a 25-minute turn, `TURN`, a floor) was at least that late whatever happened next.
Among those flights (`kn`, by threshold, per airport and three-hour block) the share cancelled (`kc`) is the
cancellation rate of flights already that late, with its 95% Wilson interval (`ontime.cancel_rate`); the airport's
own block when it has `MIN_KNOWN` (40) such flights, else the country's same block. The Fixer counts that share
against leaving: a best figure (`p_mid`) with the margin as its range (`p` low, `p_hi` high), and the page prints
"about 65% (59–69%)" with the band shaded. On the same JFK evening case the old every-or-none bounds said 34% to 81%.
Measured on the full year (2025-08 to 2026-07): of 158 JFK flights 6-9 PM whose plane was 3 h late, 31 were
cancelled (20%, 14-27%); O'Hare runs about 30%, the country about 11%. Most cancellations are never "a late flight
that got cancelled": in August 2025, 16% had no plane assigned at all and 24% lost their plane to its cancelled
previous flight. A table built before this step falls back to the old bounds. The result says "from US DOT records*"
and the basis names the measurement. Anywhere else
(abroad, or a foreign airline) the old model stands, labelled an estimate: a delayed flight leaves when the airline
says 55% of the time, the rest slipping by the hour, the cancellation risk growing with the delay and jumping
after 21:00 and 23:00. And **the chance you fly today at all** (at hour h,
the flight's own chance or one of the day's own alternatives still leaving 75 minutes on,
each given an even chance of taking you, capped at 97% because a seat on sale is not a seat
in hand). Times on the form are 12-hour with an AM/PM toggle; a 24-hour entry picks its half
by itself. The alternatives on the result open the details sheet, with a warning to ask the
airline's staff first, and "See all alternative flights" shows the day's results as an
ordinary search narrowed by a visible "leaves after" rule. **A next-day option carries the
night**: the median nightly rate of real hotels within 10 km of the airport it leaves from, tonight, from Google
Hotels (`server.hotels_request`, below) for the two airports tomorrow's flights leave from most; where that lookup
does not answer, the typical rate from `enrichment/hotels.json` (65 airports and country defaults, DRAFTED from
general knowledge on 2026-09-21, never a live price, and the row says so); and the ground model's own rideshare estimate for a hotel 4 km
out, there at this hour and back two hours before the flight, both itemised and both under
"more" on the row. One row per flight, the cheapest fare standing for it. **The tracking
feed is AeroDataBox** (2026-09-21, through RapidAPI; `live.flight_status(number, date)` on its
own key `CONCORDEGO_AERODATABOX_KEY`, or `aerodatabox_key` in `cloud.json`, its own counters
in `quota-aerodatabox.json`, 40 a day and 300 a month by default, a five-minute cache so a
refresh costs one call, the key in the RapidAPI header and redacted from everything that
escapes; no flight known that day is an empty list, an answer, not an error). `GET /flight?
number=&date=&origin=&destination=` sits outside the door on a dozen lookups a day per
address; "Look it up" beside the Flight field fills the scheduled and revised clocks,
flips Delayed or Cancelled, and prints what the airline posted (`#rs-tracked`), marked
observed; the call reads the feed itself when a flight number is given
(`rescue.from_tracking()`, `apply_tracking()`), and the odds' basis opens with the airline's
own status and says the rest is still modelled. The shape was VERIFIED with a real call on
2026-09-21 (DL 5048, LGA to CLT, Expected, 3h32 behind); `adapter_samples/aerodatabox-dl5048.json`
is synthesized and says so. AeroDataBox's status word "Expected" with a revised time later
than the schedule reads as delayed (a quarter hour or more); "Canceled" and
"CanceledUncertain" as cancelled; "Departed"/"EnRoute"/"Arrived"/"Diverted" as flown. A
FlightAware AeroAPI adapter behind the same `from_tracking()` is the swap if lag or coverage
ever bites. The helper's model text: The situation comes in as chips and clocks: delayed or cancelled,
the scheduled and new times, whether rebooking was offered and when it lands, the fare, a
checked bag, what was paid, what an hour is worth. Clocks are read in the AIRPORT's zone,
taken from the results, never the browser's. One live search for the day (and tomorrow after
17:00) runs through the ordinary pipeline, and the arithmetic makes the call: each
alternative costs its ticket less the refund the rules say you may expect (US DOT 2024: a
cancellation, or a delay of three hours at home and six abroad; elsewhere, cancelled yes,
delayed ask), less the hours it saves at your rate, plus a hotel night when the only way on
leaves tomorrow morning. Switch when an alternative comes out $25 or more ahead, stay
otherwise, wait when nothing leaves in time. The rights list is what the rules say you MAY
ask for (refund, rebooking, EU261 by distance, care, the bag) and the advice is the model
behind the narrator's guard rails plus its own: no figure the brief did not give, no airport
it did not name, and no promise about rights ("you are owed" is rejected), the template
otherwise. `tests/test_rescue.py` fences all of it offline, on the checked-in sample.

**The wish box's model is Claude Opus 5** (`concorde-travel/wish.py`, `/api/wish`), the same
model as the narrator: one call at low effort with a strict JSON schema over the page's own
vocabulary (avoid / want / tilt / max / after / before / note), the vocabulary cached as the
system prompt. The model picks WHICH rules; the server drops anything outside the
vocabulary or naming an airline the search did not return, builds every label itself, and
suppresses an `ask` that carries a figure (hard rule 1: it never ranks, never prices). With
no key, no package or a failed call it answers `fallback` and the page's own pattern parser
takes over, so the box never goes dead. `tests/test_wish.py` fences this offline.

Three things about the scorer that look like fussiness and are not, each with a comment in
`scorer.py` saying why: money is **integer cents end to end** (float accumulation makes
ties sort by rounding noise); timestamps carry an **explicit UTC offset** and no timezone
database is ever consulted (01:30 on 2026-11-01 at JFK happens twice); and **unknown is
never zero** (otherwise the least-documented itinerary accumulates the fewest penalties and
wins). Every constant lives in `scorer.Tuning`, never in a fixture — a fixture carrying the
curve it is meant to be testing is a fixture that tests nothing.

**The test suites are mutation-tested.** `test_scorer.py` catches 12 of 12 seeded faults and
`validate.py` 10 of 10; `tests/mutate_adapter.py` is an executable runner for the adapters
and must stay at every mutant caught (101 of 101 on 2026-09-25, the points charts, planner and pool, hotels, DOT records and the measured cancellation rate, the Fixer, the fleet table, the add-on prices, a flight's own Duffel extras, the lounge rules, the meals table and the ride-fare bands among them; it runs `test_points.py`, `test_records.py` and `test_rescue.py` too, and `MUTATE_ONLY=planner` runs just the mutants whose description holds that word) with **zero skips** — a skipped mutant never ran
and is not a pass. If a change makes a suite pass that should not, the suite has lost a guard. **The runner works in
a scratch copy outside the checkout** (since 2026-09-24): run in place, Google Drive's sync raced its
write-and-restore cycle and left two mutants in `par.py` after a run that reported every fault caught. If a SKIP
ever appears, first check that the file still holds the original line. **The enrichment JSON files are written with two-space
indent and characters unescaped** (`json.dump(d, fh, indent=2, ensure_ascii=False)` plus a closing newline): several
mutants anchor on their exact text, so re-indenting one turns every such mutant into a SKIP and the diff into the whole
file (2026-09-25).

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

**Three providers, and they do not know the same things.** `from_feed()` dispatches on the
payload's own shape, so a profile pointed at the wrong provider fails as a parse error
rather than as a plausible scenario built from the wrong keys.

Measured against the real Kiwi payload, that feed supplies the skeleton and the price and
very little else:

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

**Amadeus closes three of those four** — it names the operating carrier (and its *absence*
means the marketing carrier flies it, which is information, not a gap), it carries an
equipment code, and it carries a fare brand with an included-bag count. It cannot see
self-transfer or virtual interlining at all, which is why both adapters are kept rather
than one replacing the other.

**Duffel is the default, and the only one you can still sign up for**
(`app.duffel.com/join`, instant free sandbox). Amadeus decommissioned its Self-Service
portal on 2026-07-17 and disabled those keys; Kiwi closed Tequila signups in 2024. Both
profiles are kept for their schemas — Amadeus Enterprise still speaks Flight Offers Search
v2, and Kiwi is the only feed here that sees self-transfer at all. **Do not re-recommend
Amadeus Self-Service, or Google Flights as a primary source**; see the table in
`docs/design.md`. Google Flights has one job here, below.

**Delta comes through Google Flights, scraped by proxy through SerpApi** (2026-09-21, per
Patrick: "to bandaid the delta problem"). Duffel lists 141 airlines and Delta is not one
of them, so `live.serp_search()` asks SerpApi's `google_flights` engine for the route, one
way, in dollars, with `include_airlines` set to the carriers the feed cannot sell
(`serpapi_carriers` in `cloud.json`, `["DL"]` by default), on its own key
(`CONCORDEGO_SERPAPI_KEY`, or `serpapi_key` in `cloud.json`) and its own counters
(`quota-serpapi.json`, 40 a day and 240 a month by default: the free plan is 250), with the
same discipline as the feed: cache before quota, quota reserved before the call, a miss
never cached, the key redacted from everything that escapes, and SerpApi's 200-with-an-
`error`-sentence treated as the failure it is. `adapter.from_serpapi()` turns the response
into the scorer's shape: Google gives local clock times with NO offset (attached from the
curated airports, or the zones the Duffel payload named through `adapter.duffel_geo()`; an
airport with neither DROPS), an aircraft NAME not a code (the cabin claim abstains), legroom
in inches (pitch), amenity sentences ("Wi-Fi for a fee" is a published amenity, never an
observed one), one whole-dollar price (cents ×100), no fare brand and no bag allowance (priced
as the carrier's no-bag brand from `fares.json`, and the scenario note says so: unknown is
never cheap), no operating carrier, and a booking token only Google can redeem (the booking
line goes to the airline's own site). An itinerary with a leg on another carrier is not
Delta's and is dropped. `adapter.merge_scenarios()` adds the options under the feed's par
and profiles, so a Delta row is graded and ranked like every other; `_feed.supplements`
says how many came from where. `adapter_samples/serpapi-jfk-lhr.json` is SYNTHESIZED from
the published schema (no key was on the machine) and says so; `live.py serp JFK LHR DATE`
makes a real one. Three mutants in `mutate_adapter.py` cover it. It is a bandaid:
scraping by proxy can break without notice, and it cannot book.

**Since 2026-09-24 the supplement asks for EVERY airline** (`serpapi_carriers` defaults to `["*"]`; a list
narrows it again), after a price check against Google Flights on five round trips found the feed missing whole
airlines (JetBlue, Southwest, Ryanair; United, Spirit and often Frontier on US domestic routes, where Duffel returned
only American, Alaska and Frontier) and the cheaper fare brands of others (United, TAP, Vueling, Ryanair). Still one
call per search. `merge_scenarios` drops a Google itinerary the feed already sells at the same price or less (same
carrier, number and departure minute per segment, `_itin_key`) and keeps a cheaper one; the page's one-card-per-flight
grouping then shows the flight once. Google's local clocks for an airport no table knows are stamped from the flight's
own duration (`_offset_by_duration`: one end's offset known, the other's is arithmetic, rounded to the quarter hour),
so a connection through such an airport is kept; with neither end known it still drops. And `data.build_slim` always
puts the five cheapest tickets in the pool (`CHEAPEST_TICKETS`) whatever their rank: the ranking may demote a $159
Frontier connection below a $201 nonstop, but it must be on the page with its reasons, never absent. Mutants cover all
three (42 now). The check itself is `concorde-travel/tests/parity_google.py` (not an offline suite: it spends real searches),
run on the droplet as the service user with `systemd-run --uid=concordego -p EnvironmentFile=/etc/concordego.env
--wait --pipe`; it keeps its own HOME, so the site's allowances are untouched, and spends two or three SerpApi calls a
trip (`PARITY_NO_RT=1` skips the round-trip call).

**The price signal and the nearby days cost no new key** (2026-09-22, per Patrick: a paid feed
has to earn its keep every month, not once). **The price signal** is read off the SerpApi
response the Delta supplement already fetches: Google Flights ships `price_insights` (the
route's typical range and a 60-point daily low history) with every search, and
`adapter.price_signal()` turns it into ONE verdict over today's cheapest ticket, `book`
(at or under the low end of the range, or 10% or more under the recent median when there is
no range), `wait` (above the range, or 20% over the median) or `typical`, with the reason in a
line and the history in cents. `search_request` hangs it on the response as `signal` for a
live search only; a recording, or a search with no SerpApi key (a developer's Mac), carries
`null` and the page shows nothing, never a made-up verdict. The page prints the word, the
sentence, and behind "Price history" a sparkline of the sixty lows with a dot for today's
cheapest here. It never ranks or grades anything (hard rule 1 holds: a signal is a sentence
about the date, not a term in the model). **Nearby days** (`#flexgo`, POST `/flex`, `/api/flex`
inside the door) runs the same search for the three days either side, skipping days already
past, through `search_request` itself, so each day's number is the ordinary pipeline's
cheapest at the user's dial, ride and bags included, and the strip names the flight, its
ticket and its grade; the searched day is marked, the cheapest days highlighted, and a tap
sets the date and searches it. Seven searches a click is real quota: the sweep is metered as
searches for the person and capped site-wide at `CONCORDEGO_FLEX_PER_DAY` (40 days a day), and
it runs sequentially on the feed's own 2s floor, so it takes most of a minute; the strip says
so while it prices. Nearby AIRPORTS need no button: a city resolves to its metro code already,
so JFK, EWR and LGA (or all five of London's) are weighed in one search, which is the product.
`test_adapter.py` fences the signal offline on the synthesized SerpApi sample, which now
carries a `price_insights` block.

**A flight's own bag and seat prices, through Duffel** (2026-09-25, per Patrick: "go ahead with Duffel's per flight
lookups for the airlines that we can do that for"). Opening a flight's details asks `/extras?offer=&owner=` (`/api/extras`
inside the door; `server.extras_request`, `live.duffel_extras`, `adapter.duffel_extras_summary`): the offer with
`return_available_services=true` and its seat maps, one lookup per opened flight, cached 25 minutes (an offer lives
about 30), on its own counters (`quota-duffel-extras.json`, 300 a day), 40 a day per address and `CONCORDEGO_EXTRAS_CALLS`
(400) site-wide. By Duffel's services agreement only offer REQUESTS count as searches, so these are believed free (Duffel
does not say so outright). Only what the airline PRICED counts: a seat with no service on sale is unavailable, never
free; an empty services list is unknown, never free bags; extra legroom is the cheapest seat named or disclosed as
extra legroom, exit, plus or comfort, on EVERY flight of the trip or unknown. What comes back replaces the published or
typical price on the bill, unmarked (`bagLadder`, `addonPrice` in mock-10), in dollars at the ECB rate. It is asked only
for the airlines Duffel quotes extras for (`EXTRAS_AIRLINES`, from Duffel's airline pages: bags and seats on UA, BA, AF,
KL, LH, LX, OS, SN; seats on AA; bags on EK and TP; `CONCORDEGO_EXTRAS_AIRLINES` overrides). **On the live account on
2026-09-25 it priced nothing**: 47 lookups across New York to London, Los Angeles and Paris found no bag service on any
airline and no priced seat (American's seat map names Main Cabin Extra, Preferred and Standard seats and prices none;
the rest refused with Travelport's `seat_map_unavailable`). Every offer came through Travelport; none of Duffel's direct
connections (BA, UA, AF, KL, LH NDC) appeared at all, which is an account setting in Duffel's dashboard for Patrick to
check. `adapter_samples/duffel-extras-aa-unpriced.json` is that real American reply; `duffel-extras-synthesized.json`
is built from Duffel's documented shapes and says so.

**Duffel CAN quote a bag price, but never on the search response.** `available_services` was
empty on all 172 offers of the real capture — it is populated only by
`GET /air/offers/{id}`, a second call per offer. The code path is built and tested (a quote
beats the curated table, `coverage()` reports it as a *strength*), but on `/api/live` today
every bag is priced from `fares.json` or the pessimistic default. **An empty list means
unknown, not free.** Fetching details for the top N offers is the obvious next step and is
not built.

**Duffel carries per-segment cabin amenities, and no other feed here does** —
`segment.passengers[].cabin.amenities` gave `wifi{available,cost}`, `seat{type,legroom,pitch}`
and `power{available}` on **269 of 272** segments of the real capture. This is enrichment
layer 5, the "hardest, biggest differentiator", arriving without a curated join.

**What is taken from it, and what is deliberately not.** `seat.pitch` becomes
`claims.seat_pitch_inches` — a plain number in the schema, not a hedged claim, because a
fare's pitch is a published cabin-layout fact and the scorer already prices it against a
31-inch norm (which the real data's own mode confirms). `power` is carried the same way.
**Wifi is NOT mapped onto `connectivity_oceanic`**: that field is an *observed* claim with a
frequency, sample size and window, and an airline saying "wifi: available" is a marketing
attribute with no source and no as-of date. Manufacturing a frequency for it is precisely
what hard rule 2 exists to stop, so it is kept raw under `_feed.wifi_published` for the
narrator and the page, and the connectivity term goes on abstaining. A mutant that turns a
published amenity into an observed certainty is in `mutate_adapter.py`.

**Two Duffel-specific traps, each with a comment in `adapter.py`:**
- **`fare_brand_name` is prose, not a code** ("Basic Economy", "Economy Light") where
  Amadeus returns `BASIC`. `_brand_key()` matches a curated token as a **whole word** —
  substring matching makes "Economy Light" hit an `ECONOMY` row, and `SURPLUS` contains
  `PLUS`.
- **A `duffel_test_` token returns the fictional carrier Duffel Airways (ZZ)** with
  invented prices. The adapter handles it — an uncurated carrier keeps its option and loses
  its rating — and adds a scenario note, but a grade over test inventory means nothing.

**`scorer._bag_cost` raises on an unpriced piece, and the traveller's bag load is
unbounded.** A published schedule stops at two or three pieces, so a four-bag party used to
take the whole search down on both feeds. `_extend_tiers()` pads the ladder by repeating
the **dearest** published tier — extrapolating downwards would make heavy loads look cheap
on exactly the fares that publish no fourth-bag price.

**An equipment code is not a cabin.** `aircraft.code` names a *type*; one carrier's 789 may
be several configurations, and the frame is swapped after booking regardless. It is joined
against `enrichment/fleets.json` into a **hedged claim with an observed frequency**, and a
type with no curated row **abstains**. Better data is what hard rule 2 is for, not a reason
to relax it.

**`enrichment/fleets.json` was CHECKED against published sources on 2026-09-25** (per Patrick: "run the research
pass"). All 92 draft rows were researched by four agents against dated sources (airline fleet pages, reports and
press releases where they could be read; Airbus Orders & Deliveries; aeroLOPA seat maps; the aviation press) and
every figure re-checked by a second agent who opened the sources. 90 rows remain: ITA's A330-300 and Austrian's
777-300ER are gone (no longer flown) and Etihad's A350-1000 moved from `EY:359` to `EY:351`. Each row keeps its
audit trail under `_evidence` (frame count and source, the layouts with their frame counts, the wifi system,
every source with its date and whether it is the airline's or the manufacturer's, what changed from the draft, and
the second reader's corrections); the second reader's unresolved doubts are in `_unchecked_by_the_second_reader`.
`needs_primary_source` stays true on 24 of the 165 claims, where no airline or manufacturer source backs the row
(many airline fleet pages refuse automated readers, so aeroLOPA and the press carry the count: all of BA's rows,
Air France's 777-200ER, Iberia's A330-200 and others) or confidence was low. Where nobody publishes the split or
the wifi count, the claim ABSTAINS (3 cabin claims: Air France's 777-200ER and both Royal Air Maroc 787s; 12 wifi
claims: ITA, SAS, LOT, Royal Air Maroc, Avianca, Turkish's A330-300, Delta's E175) rather than guessing. Rows with no
wifi at all (SWISS's A321neo, TAP's A320neo and A330-200, Austrian's 767, Brussels's A330-300) say "No wifi" with
`outcome: false`. `test_adapter.py` fences the table's shape and two mutants guard it. Refits move monthly (BA's
787-9 and A380 Club Suite, Lufthansa's Allegris, United's Elevated 787-9s, American's Flagship Suites): re-check the
rows whose `_evidence` names a refit in progress. `enrichment/fares.json` is still a populated draft for the rows
the bag-fee research did not reach. A claim from an unreviewed table is *more* dangerous than an abstain — an
abstain announces itself in the ledger, a draft row prints a confident dollar figure. Two things about their content:

- **A fleet row's frequency is the share of the carrier's FRAMES of that type in the MOST COMMON configuration**
  (since 2026-09-25; the rows used to name the BETTER configuration, which made American's 777-300ER, one refitted
  jet in twenty, read as a coin flip when it is 95% the older cabin), with `sample_size` the frame count and the
  window saying so. That is the honest quantity available without tail-number observations, and it is what the
  scorer's "cabin may vary" cost measures: uncertainty, not quality (the cabin's quality is the per-cabin airline
  rating). The earlier draft claimed "the last 60 transatlantic departures" for numbers nobody had counted, which is
  the manufactured observation hard rule 2 forbids.
- **A fare row carries `feed_name`** — the wording the feed uses ("Basic Economy", "Partner
  Main", alternatives separated by `/`) — and `_brand_key()` matches that exactly before it
  walks words, so `UA:BASIC` and `UA:ECONOMY` no longer depend on which row comes first. Never
  key a carrier on a word its other brands share. A row's tier count says how many bags the
  brand includes (three tiers is a no-bag brand, two includes one, one includes two), and the
  fees lean to the airport price where prepaid is cheaper.

**Bag fees by route, and the airlines Google brings in** (2026-09-24, per Patrick, after the every-airline
supplement put Ryanair, Southwest, JetBlue and the Asian carriers on the page at the $100 placeholder). 69 airlines'
published fees were researched from their own baggage pages and each re-checked against its sources by a second agent;
the rows carry `sources`, `confidence`, `verdict` and `needs_primary_source` where the check could not confirm them (19).
Every fee is one way, per piece, the price when the bag is added during booking online at the top of the airline's
stated range (never the ultra-low-cost airport penalty, never a member price). A row's `short_haul` block replaces its
tiers, included bags and carry-on when the trip starts and ends in one country or covers at most 3,500 km
(`adapter._short_haul`; unknown reads long-haul): the table used to hold only transatlantic fees, so JetBlue's domestic
bag was $75 instead of $49. American, United, Delta, Alaska and JetBlue carry the domestic block on every economy row
(domestic Main includes no bag); the European and Canadian full-service airlines on their lowest row only.
`includes_carry_on: false` marks a lowest fare with no full-size cabin bag (the budget airlines, United Basic), which
the Google adapter now passes on, so the Free carry-on want is honest for Google rows. An unknown short-haul fare takes
`_default_short_haul` ($76 / $76 / $150), below the long-haul default. Spirit stopped flying on 2026-05-02 and has no
row; Eurowings, airBaltic and TAP publish only "from" prices and stay on the default. Fees change about yearly; the
research method is in the commit that added these rows.

`adapter.coverage()` reports all of this per search and the page prints it. **A grade
computed over mostly-abstained enrichment is not the same object as one over a curated
route** — do not quietly drop that strip to make the results look more confident.

**Cabins are a floor, flagged, never a filter** (2026-09-24, per Patrick, after a Charlotte to Cuenca first-class search
came back empty: Duffel had no first-class offers and Google no first-class results, while Google's business answer
mixed first, business and premium economy by leg). A first-class search also asks both providers for business
(Duffel always, it is cheap; Google when first returns fewer than five), and a business search that comes back thin
(under three) also asks for first; `server._merge_duffel` and `_merge_serp` list each flights-cabin-price once, mark
Google's second-cabin rows so no seller check is offered on them (their booking token answers the other query), and
keep a price history only from the cabin asked for. The cabin asked for rides in the scenario's `query.cabin`
(schema), and `scorer._cabin_short` prices every leg flown BELOW it at the gap between the two cabins' hourly worth
(`Tuning.cabin_hour_cents`: economy 0, premium economy $20, business $65, first $90 an hour, times the comfort
weight), off the bill, in the order and the grade; a leg ABOVE it costs nothing (business search, domestic first
leg). The page always names it first among the cons ("Business on 1 of 3 flights", "Business, not first class") and
on the flight's hover card. `adapter.cabin_norm` puts every feed's cabin words into one vocabulary: Google's
"Business Class" used to read as `business_class`, matched nothing, and graded as economy. **The grade's comfort
part weighs every flight's seat by its time in the air** (a business long-haul with two economy hops is not a
business trip) **and takes points off for flights below the cabin asked for** (`_CABIN_SHORT_POINTS` 2.0 at a full
shortfall, the share blending flights and time, each weighted by how far below: a business search that is economy on
two of three flights loses most of a letter, business throughout on a first-class search about half a letter, first
on a business search nothing), per Patrick: such trips mislead, and people book them and are disappointed. **Airports no table knows are placed in time from Duffel's places**
(`server.airport_geo`, cached a year, twelve a search): a Google-only trip through Charlotte, Houston, Quito and
Cuenca used to be dropped whole, because Google gives local clocks with no zone and Duffel had returned nothing to
borrow one from.
**An empty feed hands the search to Google**: when Duffel sells nothing on a route and Google has trips,
`data.build_slim` builds the search from Google's own scenario (`from_serpapi`) instead of answering "No flights
found", which is what that Charlotte to Cuenca search did.

**Airline ratings come from published scores** (2026-09-24, per Patrick, who asked whether AI could give "a general
consensus for each airline": it can AUTHOR the table once, never answer at search time, which would let the same
search reorder tomorrow). `enrichment/carrier_scores.json` holds each airline's published figures, gathered by
research agents from the publishers' own pages and re-checked figure by figure by a second agent (no number needed a
correction): Skytrax economy stars by haul (skytraxratings.com; many pages date from 2020 to 2022, and the basis says
which year), the AirHelp Score 2025, and for US and Canadian airlines ACSI 2026 and J.D. Power 2026 economy.
`concorde-travel/rate_airlines.py` turns them into `carriers.json` ratings by fixed arithmetic: each score on 0..1 by a
fixed absolute scale, weighted (Skytrax .45, AirHelp .15, ACSI .20, J.D. Power .20; AirHelp is light because half of
it is punctuality, which the grade's reliability part already has, and claim handling), then one linear map fitted
to the 14 ratings a person reviewed, which are kept as they are; Skytrax's low-cost and leisure scales are moved onto
the full-service one by a fitted offset (low-cost stars minus 1.5). 72 airlines are rated; LEVEL, Lufthansa City and
Air Premia have no published score and stay unrated. Where Skytrax rates short-haul economy differently (BA, ITA, TAP,
Brussels, Qatar, ANA, Cathay), the row has a `short_haul` block that `adapter._carrier_rating` uses on a short-haul trip.
Skytrax rates American's economy 3 stars on both hauls, so American has no split, though its narrowbodies lack
seatback screens until the 2028 deliveries. **Ratings are per CABIN** (2026-09-24, per Patrick: "Lufthansa's first class is amazing"
is not a statement about its economy): a row's `cabins` block rates premium economy, business and first by the same
arithmetic from that cabin's Skytrax stars by haul (`skytrax_cabins` in carrier_scores.json), a cited reviewer
consensus when there is one (`reviews`: 35 airline-cabins, one agent's 1-5 reading of 2024-2026 reviews from The
Points Guy, One Mile at a Time, Head for Points, Executive Traveller and the like, two or three citations each, every
citation opened by a second agent; averaged equally with the Skytrax stars, which are often from 2020, so a new
product such as American's Flagship Suite counts), and the 2026 Skytrax World Airline Awards cabin categories (`awards`: +0.5 star for the
top three, +0.25 for 4 to 10; economy takes its own category the same way), with the airline-wide AirHelp, ACSI and
J.D. Power scores. `adapter._carrier_rating` uses the block for the cabin flown on the trip's LONGEST flight, so a
first-class search on Lufthansa is rated 0.89 and its economy 0.75; the grade's comfort part and the ranking's
carrier line both read it. Formula rows carry `reviewed: false`, a `basis` sentence the flight's card
prints, and their sources; `test_adapter.py` asserts the table is exactly what the script computes, so a hand edit to
a formula row fails. Refresh `carrier_scores.json` when AirHelp, ACSI or J.D. Power publish a new edition, then run
the script with `--write`. A formula row is a DRAFT like the fleet and fare rows: it wants a person's pass.

`enrichment/` is the curated moat: airports (zones with DST transition dates, MCT, service
hours, inter-terminal), carriers (reviewed ratings), ground access by origin and hour, fleets
and fare brands, and an (empty) route-par override table.

**The curated tables are an OVERRIDE LAYER, not a gate** (changed 2026-09-20, when the
brief went global). Uncurated no longer means dropped; it means estimated, and *said out
loud*. Removing the gate without adding the reporting is how a Bogotá layover briefly
scored as a free walk-through, so the two always ship together:

- **Origin** — never drops. `ground.py` estimates from coordinates, the curated table wins
  wherever it exists, and every mode carries `support`: `curated` / `modelled` (we have fare
  rates for that country) / `assumed` (we do not, and the user is told so in a sentence).
- **Timezone** — `stamp()` falls back to the feed's own IANA zone via stdlib `zoneinfo`. The
  curated DST table stays authoritative so FIXTURES stay hermetic; that was always its real
  job. With no curated zone *and* no feed zone, it still drops — assuming UTC would shift a
  whole itinerary in exactly the places we know least about.
- **Borders** — `border_of()` is curated union, then the feed's country code, then nothing.
  Two different countries is a border unless a union says otherwise, which needs no curation
  to be right anywhere on Earth.
- **Layover facts** — an uncurated airport has no MCT, no service hours, no terminal
  geography. It is kept and modelled on defaults, and `coverage()` **names the airports**.

**Ground estimates lean high on purpose.** The job is stopping the $100 surprise, and an
estimate that comes in under the real fare manufactures the surprise it exists to prevent.
Same principle as "unknown is never cheap". Calibrated against real fares in 8 cities across
5 countries; 6 of 8 land inside the real range. Known weak: Stockholm reads high (fixed-price
airport taxis), Narita transit reads low (the N'EX is a premium train).

**Rideshare fares cannot be bought at any price** — Uber's estimate endpoints are
partner-gated, same story as Kiwi and Amadeus. **Transit fares CAN** — GTFS-Fares v2 is a
real standard and Navitia serves US and EU. If one half of this gets replaced with real
data, it is the transit half, and it is the only half that can be.

**21 airports are curated.** The 12 European connection points a real JFK–LHR search returns
were added on 2026-09-19 (FRA MUC ZRH GVA FCO WAW CPH DUS KEF DUB SNN IST), taking the real
capture from 96 to **146 of 172** offers scored. **BOS, IAD, ATL, BOG, CMN, TLV and AUH are
deliberately still uncurated** — a JFK–LHR search returns connections through all of them and
every one is outside the transatlantic scope. Newly added rows carry
`needs_primary_source: true`; the nine older ones do not.

Three things about that table that are load-bearing:

- **"No summer time" is not "not curated yet."** Iceland has never observed it and Turkey
  abolished it in 2016, so those zones carry `observes_dst: false` and `offset_for()` returns
  standard time for any date. A zone that merely runs out of curated years still returns
  `None` and drops the itinerary. Conflating them either deletes every Icelandic connection
  or silently scores an uncurated year an hour out for half of it.
- **Immigration is a BORDER crossing, not a Schengen membership test.** Every airport names
  its union (`schengen`/`uk`/`ie`/`tr`/`us`) and `crosses_border()` compares them. The old
  Schengen-only rule scored a US→Dublin or US→Istanbul layover as a free walk-through.
- **`services[].hours_local` is PARSED**, so it is `HH:MM-HH:MM` or `24h` — never prose. Put
  prose in a `note` key beside it. A typo there used to surface as `int('al')` four frames
  down, naming neither the airport nor the value; `window_covers()` now raises with both.

## Searching anywhere: places, and the form that used to be decorative

`concorde-travel/places.py` turns what somebody typed into a code a flight API
accepts. **It resolves offline, from a table of ~180 spellings.** Duffel has a
Places endpoint that should be wired in eventually, but resolving a destination
must not depend on the network: a search that fails because an autocomplete call
timed out is a worse failure than one saying "try an airport code".

- **City codes beat airport codes** for a destination. Someone flying to London
  wants all five airports weighed against each other — that is the whole product
  — so a bare city name resolves to the metro code (`LON`, not `LHR`).
- **Addresses are parsed from the END.** Real input is
  `"Wyckoff Ave & Myrtle Ave, Bushwick, Brooklyn 11237"`, and the city is the
  last part, not the first. Postcodes are stripped: a token carrying a digit is
  never a city, and `"london ec2a"` matching nothing is how the most normal input
  in the world used to fail.
- **Nothing is ever guessed.** An unrecognised place comes back as a sentence for
  the user, because searching the wrong continent is worse than being asked to
  type three letters. A mutant that guesses `NYC` is caught.

**The place fields autocomplete** (2026-09-21): `/suggest?q=&kind=from|to` (outside the
door) answers from the offline table first (`places.search`, proper name first, the matched
spelling shown beside it), then Duffel's places endpoint (airports and cities the feed can
sell, with the key), then, for the origin, Nominatim addresses (only when what was typed
looks like one; a bare postal code keeps only hits that carry it). External answers are
cached a week under `~/.concordego/suggest/` and capped per day. What a pick puts in the
field always resolves: a name with its code in brackets ("Honolulu (HNL)", which
`places.resolve` reads), or an address.

**The shortlist tiles' show** (2026-09-21, third pass, per Patrick: "go right to left and
update ... make the panning smoother, like a drift ... a single image that spans all 3
frames, randomly"): a beat every 6.5s; on the beat the frames update RIGHT TO LEFT, 420ms
apart, each behind a white flash over its whole frame (above the scrim and the caption),
the picture changing at the flash's peak; most beats deal three new pictures, home and away
alternating; about one beat in three, at random, one picture is laid across all three frames
as a strip, every frame scaling about the same point on the page so the slices stay one
picture. Pictures come off a shuffled DECK, so nothing repeats until every one has shown.
Each picture drifts for as long as it is up: one slow zoom with a slide, 20s long and linear
though a picture lives 6.5s, the end points chosen per picture inside the 16px of slack, and
a re-paint picks the drift up where it was. The swaps run on timers, never
`requestAnimationFrame`, which a hidden tab never fires; and `build.py` syntax-checks every
built page with node, because one duplicate `const` killed the whole client silently, and since 2026-09-25 also
opens mock 10 in headless Chrome and stops the build on any error while it loads: a script that parses can still die
at load when a shared `const` is read before its line runs (the lounge chip's rules were read by `prepWants`, which
runs at load, above where they were defined).

**The destination field takes an address too** (2026-09-21, per Patrick: "so the person can get
to a hotel or wherever they're staying"). `server.geo_place()` geocodes anything with a digit
or three comma parts through Nominatim, walking inward when the full line misses (the street
without its number, then the town), and never caches a miss; `place_near()` picks the airport
the flights go to when the city in the address is not in the table: the provider's places
within 150 km, the nearest airport's METRO code first (an address by City airport is a London
search), else the nearest curated airport. The point rides through `dest_point` into
`adapter.ground_both_ends()`, so the arrival ride is priced from the airport to that door by
the same model as the outbound ride, and the page names the metro (`query.destination`) with
the address beside it (`query.destination_full`). Autocomplete offers address rows on both
sides. The wish thread's `.thread` rule sets `display`, so it needs its own `[hidden]` rule
(the CLAUDE.md trap, met again); and the shortlist's picture swap runs on timers, never
`requestAnimationFrame`, which a hidden tab never fires.

**The origin field does two different jobs** and both are sent: `origin_address`
is where the ground leg starts, and the city inside it decides which airports to
search. Sending only one is why every search used to be a hardcoded JFK–LHR no
matter what anybody typed.

**Unresolvable input is refused BEFORE a metered call is spent.** A search that
burns quota and then discovers the destination was a typo is a search that cost
money to fail.

## The live API: key, quota, cache

`concorde-travel/live.py` is the only module that holds a credential. It answers one
question — may we call the provider right now, and what came back — and hands the payload
to the adapter. The scorer never sees it.

**Setting a key**: `export CONCORDEGO_FLIGHT_KEY=...` (plus `CONCORDEGO_FLIGHT_SECRET` on
an OAuth2 provider), or put them in `~/.concordego/cloud.json` (written 0600, created empty
by `live.py status`). The env vars win, so neither half need touch disk. `/api/live` uses
the API when credentials are configured and the recorded sample when they are not — the
interface never offers a button that cannot work.

**The provider's wire format lives in that config, not in code.** The Amadeus and Kiwi
profiles are written from documentation and are **unverified against the live service** — no
credential for either was available on the machine this was built on; Duffel's is verified
(below), and `live.py status` prints each profile's note. If the parameter names are wrong,
fix the profile; you should not have to edit Python to correct someone else's query string.
Protocols are the exception: OAuth2 is a mechanism, not a parameter name, so it lives in
code.

**OAuth2 costs nothing when it fails.** The token is minted *before* quota is reserved,
because a mint is not the metered call and a mistyped secret is the likeliest day-one
mistake. The token is cached with a one-way fingerprint of the credential pair, so a rotated
key invalidates it; a 401 on the search drops it, since it may be a retired token rather
than a bad credential. The secret and the bearer token are redacted everywhere the key is,
and `status()` reports that a token is held and its remaining life, never the token.

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

**No flight API is reachable from the sandbox these adapters were written in** — the agent
proxy answers 403 to CONNECT for `api.duffel.com` and every other provider host, which is
why all three profiles are `UNVERIFIED` and why a key pasted into a session here would be
useless rather than merely risky. `concorde-travel/capture.py` is the way across that gap:
run on a machine that *can* reach the provider, it exercises the real `live.search()` path,
scrubs the payload for credentials **before** writing it, saves it to `adapter_samples/`,
and prints a pasteable report. A failed call is a useful result — the report is written for
that case too, because the likeliest first outcome is a 4xx from a wrong parameter name.

**`kiwi-jfk-lhr.json` and `duffel-jfk-lhr.json` are real captures; `amadeus-jfk-lhr.json` is
not** — it is synthesised from the published schema, says so in its `_provenance`, and a test
asserts it. The Duffel capture is a real JFK–LHR search **trimmed from 172 offers to 23**
covering the distinct shapes; its `_provenance.selection` names why each was kept.

**The Duffel profile is VERIFIED** — a real `duffel_test_` key returned 172 offers on the
first call, so its parameter names, POST body, `Duffel-Version` header and static switches
are correct against the live service. Amadeus and Kiwi remain unverified.

## The results timeline

**The journey bar's colours** (2026-09-24, per Patrick). Each block is coloured by how good that stretch is, on the
page's red-to-green scale, except the airport block, which is time rather than a verdict and wears its own blue
(grey read as "something is wrong"). **Airport time** is how long before takeoff the traveller is at the airport:
`par.airport_minutes`, an hour when the trip stays inside one border and an hour and a half when any flight crosses
one (airlines close bag drop 45 and 60 minutes out), or the curated airport's median when longer; the adapter
(`_airport_block`) and par use the same rule, so the speed grade still compares like with like. **Layovers** use
`LAYOVER` in the page: inside one border red at 45 minutes or less, green from 1h15 to 2h30, red from 5 hours;
at a connection that crosses a border red at an hour or less, green from 1h45 to 3 hours, red from 6 hours; amber
between, and the airport's own minimum connection, an overnight in the terminal and a shut terminal still force
red. **Flights** are coloured by the detour and the airline's rating; an unrated airline is coloured on the detour
alone and its card says "Airline not rated yet", never "bad". **Inside each block is a glyph, not a word** (per Patrick,
2026-09-24: "airport" did not fit): a car for a ride (a train when it is public transit), a suitcase for the airport, a
plane then "· time" for a flight, a chair for a layover (a bed for an overnight one) then the airport code and time.
`GLYPH` holds them (Material Icons paths, Apache 2.0) in the bar's dark ink; every full-size block is a CSS size
container, so each piece appears only once the block is wide enough for it (glyph from 16px, the words from 40 to
72px) and nothing is ever cut mid-word. The mini bars in the rows carry no glyphs. **Every bar is drawn to one
scale** (per Patrick, 2026-09-24): the longest door-to-door trip the whole search returned, filtered out or not
(`ctx.maxDoor` over `ALL`), fills its line and every other bar, the podium's full-size ones included, is that
fraction of it; the clock labels under a short bar keep their room (`min-width:max-content`). **An arrival block**
follows the last flight, blue like the airport block, with the suitcase: `par.arrival_minutes`, 15 minutes to walk
off and out, 20 more for baggage claim when the traveller checks a bag, 35 more for passport control and customs when
the LAST flight crosses a border (a border crossed earlier was cleared in the layover), and none of that 35 from a US
preclearance airport into the US (`adapter.US_PRECLEARANCE`: the Canadian gateways, Dublin, Shannon, Nassau, Bermuda,
Aruba, Abu Dhabi). It is the option's `arrival_process_minutes` (schema), counted in door to door by the scorer and
named in its time line, and par's reference traveller, who checks the one bag its fare includes, gets it too; a
fixture without the field models none. The account and Admin buttons sit at the right end of the top bar
(`.baracct`), and the search summary gives way first so they keep one row.
**The details sheet** (2026-09-24, per Patrick) opens with the journey bar drawn across the whole sheet (this trip's
own length, not the search's longest), carries a **price history chart** above the booking box when a live search
brought Google's price insights (the lowest fare on the route for that date, any airline, day by day for about 60
days, the usual range as a band, this flight's fare marked; the trend is the least-squares slope as the change over
the last 30 days, "steady" under 4%), and shows an **airline as five stars** (the rating times five, one decimal,
partly filled) with no sources listed: per Patrick, travellers do not care about the math. The class is `rstars`:
`.stars` is the sky's full-screen canvas, and a clash once shrank the sky to 80 by 16 pixels.

The results section is mock-2 from the design round: **every option on ONE
clock.** A bar that always fills its row tells you how a trip is divided but not
when it happens, so two flights look identical when one leaves at eight and the
other at midnight. Positioning each ribbon on a shared window is the entire
reason to draw this rather than print numbers — you look down the column and see
that leaving around noon costs four more hours than leaving at eight, without
reading a figure.

Four things about it that are load-bearing:

- **The axis and every track share one set of gutters.** If they disagree the
  picture lies. `.axis` margin and `.barwrap` padding + column widths are the
  same number, and `test_timeline.py` measures it.
- **The axis is in the TRAVELLER'S local time**, read off `depart_iso`'s offset —
  not UTC, and not the browser's zone. "I want to leave around noon" means noon
  where they are standing, and labelling it UTC makes the one question the
  timeline exists to answer unanswerable.
- **The ribbon starts at departure minus ALL pre-flight time** — the ride *and*
  the airport hour. Counting only the first block put every ribbon up to an hour
  adrift of its own tick while still looking plausible in a screenshot.
- **Figures sit beside the track, never on it.** Numbers blending into the bars
  was the one thing Patrick asked to be fixed about this design.

`tests/test_timeline.py` measures all of that in a real browser and **skips
rather than fails** when playwright or a server is absent — the product is
stdlib-only and this is the single test that is not, so it must never be the
reason a clean checkout cannot run its suite. Start a server first; it does not
start one, same as the desktop app's gauntlet.

## Interface mockups

`concorde-travel/ui/mock/` holds nine directions for the interface, at
`http://127.0.0.1:9897/mock` or by opening the files directly. **09 Concourse
is the converged direction** (2026-09-20, after Patrick picked Shortlist and the
Dial from round two): the sentence-form search with round trip / one way /
multi-city, the price–speed–comfort triangle with a "get me there now" preset
(earliest arrival, price shown but not ranked), eighteen **wants that charge rather
than filter** (a flight lacking one is priced for it and sinks, reason shown;
a want nothing can meet says so instead of sinking everything; the refund term and
CO₂ per offer came on 2026-09-21, the last charged as the gap to the cleanest option
at $60 a tonne, unknown never counted as clean. On 2026-09-22, per Patrick, seat power
went (every transatlantic fare has it, so it separated nothing) and four came in: same
airport on a connection, bag included, free carry-on (the fare's own allowance,
`carry_on` from `data.build_slim`; a feed that does not say reads as not included) and
top-rated airline (rating at or above .76, the page's own "well-rated" line). Wifi became free wifi the same day (`wifi_cost === 'free'`, the long leg deciding as in `data.py`): a fare that sells a pass gets the pass on its bill, one with none ranks lower. Free
seat choice was considered and left out: Duffel and Google never report seat
selection on a search, the adapters record "paid", so it would match nothing. Lounge access went on 2026-09-24
(per Patrick: it was guessed from the fare's name, and many travellers have it from a card or status); a lounge
day pass is still an add-on anyone can put on the bill. Lie-flat seat reads the seat itself since 2026-09-24
(`e.lie_flat` from `data.build_slim`: Duffel's seat type `full_flat`, `full_flat_pod` or `private_suite` on the
longest flight, or Google's "Lie flat seat" / "Individual suite" extensions), no longer the cabin's name, which had
marked TAP's Executive and Icelandair's Saga Premium recliners as flat. **Three came in on 2026-09-25** (per Patrick):
Lounge access came back as an opt-in chip (never added unless picked; picked, a ticket that already gets you in meets it
and the rest carry a day pass on the bill: `loungeAccess`, business or first on a trip that crosses a border, a lie-flat
premium flight at home, JetBlue's BlueHouse at JFK and Boston only, Icelandair's Saga Premium at Keflavik, Aer Lingus
AerSpace, or a status tier's published lounge rule in `LOUNGE_STATUS`, researched from the airlines' and alliances' own
pages and re-checked on 2026-09-25: most elite tiers only on an international trip and their alliance's flights,
AAdvantage and Atmos elites only on trips that leave North America, Delta Medallions no Sky Club in Main Cabin and
nothing on a Basic or Light fare, Turkish's rules unconfirmed so it claims nothing; never the fare's name), Overnight
flight beside Daytime flight (leaves at 17:00 or later and lands the next morning before noon; picking one clears the
other, `OPPOSITE`), and Meals served (`e.meal === 'meal'`: the published catering of the airline OPERATING the longest
flight, for its cabin and length, `enrichment/meals.json` through `adapter.meal_for`; a snack, food for sale or no
published policy is not a meal). `meals.json` holds 209 rules for 71 airlines, researched from the airlines' own
food-and-drink pages, re-checked, turned into rules by a second pass and checked again against the source text
(2026-09-25). For meals, LONG haul is the longest flight crossing an IATA area, or covering over 3,500 km and running 8
hours or more (`adapter.meal_long_haul`, areas from the airport table or the country): New York to Dublin is long, New
York to Sao Paulo is long, and Seattle to Costa Rica, Canada to Hawaii and Frankfurt to Cape Verde get the short-haul
product. Long-haul rules carry no minimum time (every such flight is past the published cut-offs; the invented floors
that first came out of the conversion would have left New York to Dublin unknown). Rules are conservative: a meal only
on named routes, on "select flights" or by time of day is not promised; a Basic or Light fare's own rule wins; an
airline with no rule is unknown. The second checker could not read six airlines' rows (OS, SN, EI, IB, UX, VY), which
the file says. The eighteen sit in a fixed grid, six across when the panel is wider than 880px and three otherwise (a
container query on `.wants`), so every row is full, and on a phone a long label wraps rather than being cut. The wish box's
vocabulary in `wish.py` lists every want the grid does), a **wish box**
typed or spoken (the browser's own speech recognition) that becomes visible,
removable rules and weights, advanced windows / cabin / stops / alliances /
airlines-to-leave-out (these hide, and say what they hid), and **points
balances** priced on the airlines' published award charts (below). **The three cards are a podium** (2026-09-21, per Patrick: "2nd, 1st, 3rd, with 1st being a
little larger ... the podium in auto racing", then "taller rather than wider"): second on the
left, first in the middle, third on the right, all one width, first place taller at both
ends (a taller picture, more room under the buttons), the three centred on one line. The DOM
is the visual order, so the tiles' right-to-left show and the strip across them read the row
as seen; under 980px it is one column in rank order again. **Step 3 is dropdown rows** (per Patrick, 2026-09-23, replacing the chip rows of
2026-09-21): three sections, Card points, Airline miles and Status, each a list of rows,
a program picked from an alphabetical dropdown, a balance beside it (a level dropdown
for status), an × to remove the row, and "+ Add another" under the list. A program
already picked in a section is not offered again in it. The rows (`S.prow`) only say
what is shown; balances and levels stay in `S.pts` and `S.status`, which the points
pricing, the status bags and status credit read. A balance follows its row when the
program is changed; a level does not, since the tiers differ. The wallet is saved in the browser
(`concordego.wallet`) and, signed in, in the profile (`/api/profile` merges `wallet` and `persona`
separately; `server._clean_wallet` keeps whole numbers and short level names only).
**Every programme and every status is offered, always** (2026-09-21, per Patrick: gated
chips "kept disappearing"; what a person holds does not change with the search); a
programme with no award on these flights says so on the card. One tier per
alliance (Star, oneworld, SkyTeam) stands in for every partner.

**Points and miles are real data** (2026-09-24, per Patrick: "if a person spends the time putting in all their
miles and points and airline status, then that should work to their advantage", and no sample data).
`enrichment/points.json` holds the banks' transfer tables (Amex, Chase, Citi in two card tiers, Capital One, Bilt,
Wells Fargo, Marriott: ratio, minimum, block, timing, fees, and transfer bonuses with end dates, which lapse by
themselves), 28 programmes with The Points Guy's monthly valuations (unknown is `null`, never zero), 20 PUBLISHED
award charts, status bag rules, and the geography the charts need. It was researched and checked by a second reader on
2026-09-24 (sources and as-of dates in each chart; a secondary source says so); change a figure only with its source.
`points.py` prices each flight in the programmes whose chart covers it, in its own cabin, deterministic, no network:
Virgin on Delta (region table and Virgin's season calendar), Air France-KLM, SkyTeam, El Al, SAA/WestJet, Virgin
Australia; BA on its own flights (route tiers, peak shown, off-peak as `low`) and one partner (per flight); Iberia;
Qatar on partners and on American; AAdvantage on partners (region, Europe off-peak outbound only); Atmos (floors,
nonstop); Aeroplan on partners (zones, miles flown); ANA, Turkish (own additive through Istanbul, Star partners),
Miles & More (partners, half the round-trip chart), Singapore. Rules it holds to, each with a test and a mutant:
**a programme with no chart (Delta, United, Flying Blue, AAdvantage and Virgin on their own flights, Aeroplan on
United) is named as able to book, never priced**; where a chart's rule is unclear the higher price is used (an Air
France connection: journey or flight by flight); a connection through a third region is not a two-region award;
British Airways' carrier charges are flagged whichever programme books its flight; a regional airline (CityLine,
Delta Connection) books as its parent. `data.build_slim` puts `awards` and `award_bookable` on every flight and
always keeps the `BEST_AWARDS` (6) flights where a chart buys the most ticket per mile in the pool, since the pool
is cut before anyone's points are known (a $10,494 Delta One that 47,500 Virgin points buy was left out).
**The page plans the transfer** (`planAward` in mock-10, from `points.page_table()` inlined as `__POINTS__` by
`build.py`): miles already in the programme first, then Avios from another Avios account, then card points
cheapest per arriving mile, with each bank's block size, minimum, running bonus, Marriott's miles per 60,000 and
Amex's excise fee; a round trip's later legs plan with what the earlier legs left. Points beat cash when the ticket
costs more than the points are worth at those valuations plus the published fees ("before taxes and charges");
then the flight ranks as if its ticket cost what the points are worth (a credit in the order, never on the money
bill), the card shows a points column with the plan, "Best use of your points" lists the top three above the
podium, and the step-by-step guide says check the seat first, the moves, the timing, the cash at booking, what the
bank's own travel site would take instead, and the programmes with no chart (with the balance you hold there). A
flight whose carrier charges can eat the saving is never recommended on points. **A held status takes bag fees off
the bill** (`bagPerk`: the airline's own tier on its own flights, the alliance tier on partners, Star Gold not on
Lufthansa Economy Light). Award-seat availability is not known here: no award search API sells to a site like this
without a commercial agreement (seats.aero, AwardTool, point.me), and that is Patrick's decision.
`tests/test_points.py` runs the charts and the page's own planner (under node). It wears the Concorde family's own
tokens (`--bg #101013`, Michroma / Space Grotesk / Plex Mono, the app's amber,
blue and purple for the three targets). Round two (05–08: Shortlist, Board,
Lines, Dial) and round one (01–04) are kept for the record.

**The shortlist tiles show photos of both ends of the trip when served** (2026-09-21):
`/photos?place=` asks Pexels (free, 200 an hour, a credit line) with
`CONCORDEGO_PEXELS_KEY`, caches each place for a month under `~/.concordego/photos/`, and
allows `CONCORDEGO_PHOTO_CALLS` (150) uncached lookups a day site-wide. Pexels has no
subject filter, so each place is searched four ways (skyline, street, architecture,
landmark) and any photo whose own alt text describes a person, a face, a costume or a pet
is dropped: the place and its culture, never somebody's family album (per Patrick). The page asks for
the origin's neighbourhood and city and for the destination, alternates them across the
tiles, and keeps the stand-in artwork until they arrive or when there is no key. Every
photo carries its place and credit in the tile's caption.

**Airline logos come from the feed.** Duffel's airline objects carry
`logo_symbol_url` and `logo_lockup_url` (an SVG per carrier on
`assets.duffel.com`); `data.py` collects them into `airlines`. The page draws a
brand-coloured monogram and lays the real logo over it when it loads, so a
viewer that cannot reach the host (a claude.ai artifact, the sandbox) sees the
monogram and nobody sees a broken image. The live page should use the same
URLs straight from the offer. For a mockup that must show the logos where that
host is unreachable, `logos.py` bakes each SVG into `slim.json` as a data URI
(run it on a machine with internet, then `build.py`).

**The sky behind mock 10 is the website's own** (`bigmillz/concorde-site`, `index.html`,
ported line for line on 2026-09-21 after Patrick said the site's zoom was "a thousand times
better" than the drawn star it replaced): a starfield on three parallax depths with glinting
hero stars, cirrus, a satellite and meteors, and for the first seven seconds the warp, a
cubic spool-up to full power at 1.6s, held to 2.6s, then a long glide out, with the camera
zoom and the debris burst. On an HDR display the site's tagged flare loops
(`ui/mock/assets/boom-*`, served by `server.py` beside the nameplates) fade in with it. The
title card sits over the sky as a clear layer, holds the page at opacity 0, and lifts on the
**warp's own clock** (the end of full power), not the wall's, so on a slow machine the page
still arrives as the glide begins; the html `splashing` class is set in the markup so nothing
flashes before the script runs, with an inline 8s watchdog. Seen once this session, the sky
starts in its ambient drift. The desktop app's "no white dots" rule is the app's; the site
has stars, and this is the site's.

Each `mock-N.src.html` carries a `__DATA__` token; `build.py` inlines
`slim.json` and writes `mock-N.html`. **Edit the `.src.html`, never the built
file** — and rebuild after. They are standalone on purpose: a mockup you have to
start a server to look at is a mockup nobody looks at.

**`slim.json` is made by `data.py`**, not by hand: it runs a capture through the
adapter and the real scorer at the modelled par and writes every number the
pages use, plus two things the live page does not have — the pool sorted under
each target, and `grid`, the effective cost of every option at **66 weightings**
across the triangle between the three targets, so the Dial can re-rank live
with the scorer's own numbers rather than a JavaScript imitation of them (hard
rule 1 in the browser). The pool is the best ten under each target plus an even
sample of the rest, so the page carries the connections and the C-to-F grades a
real search returns. The checked-in file was built from the untrimmed 172-offer
capture, which is not in the repo; rebuilding from the trimmed sample gives 23
options and says so in `_provenance`.

Some inventory is **synthetic** because the capture came from a test token (the
fictional carrier ZZ is filtered out of the mock data; a cluster of four
carriers sharing one departure time is the test feed, not the adapter).

Verify a change with a headless render rather than by eye — Chromium is
pre-installed, and a page that throws still screenshots fine.
