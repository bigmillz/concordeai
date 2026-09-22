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
resets the checkout to `origin/BRANCH` and restarts the service when HEAD moved, ufw with
ssh only, and cloudflared run from a tunnel token made in the Zero Trust dashboard, whose
public hostname owns the DNS record so no script here touches DNS). **The box is locked
down three ways** (2026-09-21): a DigitalOcean cloud firewall named `concordego` in front
of it (inbound TCP 22 only, everything outbound; the site needs no open web port because
the tunnel dials out; `launch.sh` creates it with doctl, or attaches a new droplet to the
existing one), ufw on the box with ssh only, and sshd by key only, root included
(`/etc/ssh/sshd_config.d/50-concordego.conf`). Security updates are unattended
(`/etc/apt/apt.conf.d/52concordego-unattended`: the security channels only, unused kernels
and dependencies removed, an automatic reboot at 09:30 UTC, 05:30 New York, only when a
kernel asks); `droplet.sh` writes all of that on a fresh box.

**Every key lives in `/etc/concordego.env` on the droplet** (0600, read by systemd, never in
the repo, the plist, a command line or chat): `CONCORDEGO_FLIGHT_KEY` (Duffel),
`ANTHROPIC_API_KEY` (narrator and wish box), `CONCORDEGO_PEXELS_KEY` (tile photos),
`CONCORDEGO_SERPAPI_KEY` (the Delta supplement), `CONCORDEGO_AERODATABOX_KEY` (the Flight
Fixer's tracking feed), plus `CONCORDEGO_OWNERS`,
`CONCORDEGO_ACCESS_TEAM` and `CONCORDEGO_PUBLIC=1`. To add or change one, edit the file ON
THE SERVER and restart, from a laptop Terminal:

```bash
ssh root@67.207.85.212 "echo 'CONCORDEGO_SERPAPI_KEY=...' >> /etc/concordego.env && systemctl restart concordego"
```

No spaces around the `=`, no quotes round the value. Editing a copy on the laptop changes
nothing (2026-09-21: an hour went on a key that was never on the box). A key pasted into a
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
worth (`S.statusWorth`, a select beside the status chips, $60 by default), named on the row
("Keeps your SkyTeam Elite Plus") and in the ledger, never on the money bill and never in
the grade. **The flying personality** is eight chip questions (`QUIZ`) that become a persona:
a point on the dial plus its own priced terms (`personaLines`: a nervous flyer's
connections, red-eyes and low-rated carriers; a foodie's long daytime layover in a food
city as a credit; lounge, wifi and power; "never" red-eyes or connections), offered as a
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
more you add the surer the call). It also puts two numbers on the day (`rescue.odds()`), MODELLED and
labelled so, each with its own line chart open on the result and both FALLING through the
day (per Patrick, 2026-09-21: at nine there is a day of chances, at ten to midnight there
are not): **the chance of making the updated flight** (if it slips to hour h, the chance it
still goes today: a delayed flight leaves when the airline says 55% of the time, the rest
slipping by the hour; the cancellation risk grows with the delay and jumps after 21:00 and
23:00; anything past midnight is lost) and **the chance you fly today at all** (at hour h,
the flight's own chance or one of the day's own alternatives still leaving 75 minutes on,
each given an even chance of taking you, capped at 97% because a seat on sale is not a seat
in hand). Times on the form are 12-hour with an AM/PM toggle; a 24-hour entry picks its half
by itself. The alternatives on the result open the details sheet, with a warning to ask the
airline's staff first, and "See all alternative flights" shows the day's results as an
ordinary search narrowed by a visible "leaves after" rule. **A next-day option carries the
night**: a typical room rate near the airport it leaves from (`enrichment/hotels.json`, 65
airports and country defaults, DRAFTED from general knowledge on 2026-09-21, never a live
price, and the row says so) and the ground model's own rideshare estimate for a hotel 4 km
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
and must stay at 26/26 with **zero skips** — a skipped mutant never ran and is not a pass.
If a change makes a suite pass that should not, the suite has lost a guard.

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
makes a real one. Three mutants in `mutate_adapter.py` cover it (39 now). It is a bandaid:
scraping by proxy can break without notice, and it cannot book.

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

**`enrichment/fleets.json` and `enrichment/fares.json` are populated DRAFTS** (92 fleet rows
and 82 brand rows as of 2026-09-20, covering every carrier a real JFK–LHR search returns and
the narrowbodies that feed their hubs). Marked in the files, every fleet row carrying
`needs_primary_source: true`, counted by `coverage()`, amber in the interface. A claim from
an unreviewed table is *more* dangerous than an abstain — an abstain announces itself in the
ledger, a draft row prints a confident dollar figure. They need a human pass before any
number built on them goes in front of a stranger. Two things about their content:

- **A fleet row's frequency is the share of the carrier's FRAMES of that type in the
  described configuration**, with `sample_size` the frame count and the window saying so.
  That is the honest quantity available without tail-number observations; the earlier draft
  claimed "the last 60 transatlantic departures" for numbers nobody had counted, which is
  the manufactured observation hard rule 2 forbids. Where a refit is under way the share is
  put below the halfway point unless known to be further along.
- **A fare row carries `feed_name`** — the wording the feed uses ("Basic Economy", "Partner
  Main", alternatives separated by `/`) — and `_brand_key()` matches that exactly before it
  walks words, so `UA:BASIC` and `UA:ECONOMY` no longer depend on which row comes first. Never
  key a carrier on a word its other brands share. A row's tier count says how many bags the
  brand includes (three tiers is a no-bag brand, two includes one, one includes two), and the
  fees lean to the airport price where prepaid is cheaper.

`adapter.coverage()` reports all of this per search and the page prints it. **A grade
computed over mostly-abstained enrichment is not the same object as one over a curated
route** — do not quietly drop that strip to make the results look more confident.

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
built page with node, because one duplicate `const` killed the whole client silently.

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
(earliest arrival, price shown but not ranked), thirteen **wants that charge rather
than filter** (a flight lacking one is priced for it and sinks, reason shown;
a want nothing can meet says so instead of sinking everything; the three added
on 2026-09-21 are the ones the feed reports outright, the refund term, power at
the seat and CO₂ per offer, the last charged as the gap to the cleanest option at
$60 a tonne, unknown never counted as clean; carry-on, airport changes, seat
maps, meals, on-time and the rest were left out because the feed does not carry
them or every offer on the route passes), a **wish box**
typed or spoken (the browser's own speech recognition) that becomes visible,
removable rules and weights, advanced windows / cabin / stops / alliances /
airlines-to-leave-out (these hide, and say what they hid), and **points
balances** with a demo award chart and 1:1 transfer partners that say whether
points beat cash in cents per point. **The three cards are a podium** (2026-09-21, per Patrick: "2nd, 1st, 3rd, with 1st being a
little larger ... the podium in auto racing", then "taller rather than wider"): second on the
left, first in the middle, third on the right, all one width, first place taller at both
ends (a taller picture, more room under the buttons), the three centred on one line. The DOM
is the visual order, so the tiles' right-to-left show and the strip across them read the row
as seen; under 980px it is one column in rank order again. **Step 3 is rows of chips, not a form** (per
Patrick, 2026-09-21: "not welcome to the world of data entry"): a source, a
programme or a status is a chip until it is tapped, and only then a field, with
the most popular few showing and the rest behind a dashed "+N more" chip.
**Every programme and every status is offered, always** (2026-09-21, per Patrick: gated
chips "kept disappearing"; what a person holds does not change with the search); a
programme with no award on these flights says "no route" on the card. One tier per
alliance (Star, oneworld, SkyTeam) stands in for every partner. Marriott Bonvoy
is a source at 3:1 valued at its own 0.7¢, so a 3:1 move is never priced as 1:1. It wears the Concorde family's own
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
