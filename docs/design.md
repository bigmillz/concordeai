# ConcordeGo — design

**Product name: ConcordeGo. Frozen spelling.** Not "Concorde Go", not "ConcordeGO",
not "Concorde Travel". Directory and config name: `concorde-travel`.

Part of the Concorde product family: the consumer assistant, Concorde AI, Concorde VPN,
and ConcordeGo.

This document is the source of truth for the product. It is written to survive a session
boundary: anything decided here should not need to be re-argued from scratch.

---

## The concept

Every booking platform exposes the same filters over the same inventory: stops, departure
window, airline, price. They differ in interface, not in judgment.

**ConcordeGo is not a search engine — it is a re-ranker.** Inventory comes from a
third-party API. The product is the enrichment and scoring layer on top of it. We are not
competing on having flights nobody else has; we are competing on knowing which of the same
flights is actually the right one for this person, leaving this address, at this hour.

---

## The model

One number per itinerary, in dollars:

```
effective_cost =
    ticket_price
  + baggage_cost(actual bag load, fare family)
  + ground_access_cost(user's geocoded origin, airport, local departure time)
  + (door_to_door_hours * user_hourly_value)
  + comfort_penalties - comfort_credits
  + risk_adjustment(misconnect probability, downside severity)
```

Cheapest, fastest and most comfortable are **not separate rankings**. They are this same
model evaluated at different `user_hourly_value` and different comfort weightings. A
"cheapest" search is this formula with a low hourly value; a "fastest" search is the same
formula with a high one. There is one ranking function and there will only ever be one.

---

## Enrichment layers, easiest first

1. **Ground access.** Geocode the *actual* origin, not the city. Bushwick to EWR at 6am is
   a ~$90 car because transit doesn't run at that hour; Bushwick to JFK is $2.90. Model
   the time-of-day dependency or don't bother — a static airport-to-city-centre matrix is
   worse than nothing because it is confidently wrong.
2. **Baggage and fare families.** Basic economy plus two checked bags often beats the main
   cabin fare it appeared to undercut. This is pure arithmetic and a guaranteed win.
3. **Layover quality.** Not a number — a *situation*: which airport, which terminal, the
   **local clock time**, whether you re-clear immigration and security, whether the
   terminal changes, the margin over the minimum connection time, and what is actually
   open at that hour. Four hours at SIN is a feature. Four hours at CDG arriving 1:40am is
   a punishment.
4. **Reliability.** US DOT/BTS on-time data is free and is keyed by flight number. Model
   **downside severity separately from probability**: misconnecting the last flight of the
   day is a hotel night, not a delay.
5. **Aircraft, cabin and connectivity.** Hardest, and the biggest differentiator.

---

## Hard rules

These are settled. Do not relitigate them.

1. **The LLM narrates, it does not rank.** Scoring is deterministic arithmetic over
   structured data. The model only parses intent into weights on the way in, and writes the
   human-readable ledger on the way out. Same query + same data = same order, every time.
2. **No unhedged aircraft claims.** Schedule feeds give an equipment code, not a subfleet.
   One carrier's 777-200 fleet may have three cabin configurations with three connectivity
   systems, and equipment gets swapped after booking. So: *"flown as a 789 with the
   refreshed cabin on 84% of the last 60 departures"* — never *"you will have wifi."*
3. **One currency.** Everything resolves to dollars. If you are writing a second ranking
   function, stop.
4. **Show the demotions.** Anything pushed down gets an itemized reason with dollar values
   and a one-click override. Hidden ranking logic feels like a kickback.
5. **Fixtures before APIs.** No live flight API dependency in the scorer or its tests.

---

## The output format is the product

An itemized ledger per option:

```
AA 100 · JFK→LHR · $480 ticket · $710 effective
  − $95  EWR at 6:10am: no viable transit, car fare
  − $85  4h10m at CDG arriving 1:40am, terminal closed
  − $60  Older cabin, connectivity unusable over the Atlantic (73% of last 60)
  − $70  Two checked bags not included
  + $80  Nonstop, no misconnect exposure
```

Every line is a dollar figure, a cause, and — where the claim is probabilistic — its
evidence. The ledger *is* the interface. If a number cannot be explained in one line of
this form, it should not be in the model.

---

## Scope

NYC origin, transatlantic long-haul only.

- Origins: JFK, EWR, LGA
- Destinations: ~12 European arrival airports
- ~40 aircraft configurations

**Do not generalize the enrichment data until the scorer works end to end.** That curated
data is the moat, and curating it broadly before the scorer proves it is how the project
dies.

---

## Build order

1. Fixtures
2. Scorer
3. Enrichment DB
4. Intent parser
5. Ledger narrator
6. Live inventory adapter

Stack undecided at the time of writing; the only hard constraint is that **the scorer must
be trivially testable in isolation** — pure function, structured data in, ledger out, no
network, no clock, no I/O.

---

# The interface

Draft, from Patrick's brief. Fable revises this later. The end goal, in one sentence:

> The user dumps in the address they're coming from, the address they're going to, any
> credit card and loyalty points they want to use, selects a few common options, and gets
> flights ranked as if they'd sat down with a travel agent who knows absolutely everything.

Shape is broadly Google Flights — a slim horizontal control panel on top, results below —
because that shape is learned and we are not in the business of teaching a new one. The
*results* are where we look nothing like anyone else.

## The control panel, top to bottom

### Row 0 — trip type tabs

`★ Guide me` · `Round trip` · `One way` · `Multi-city`

**Guide me** is first and carries a star. Later it becomes a step-by-step wizard that asks
every relevant question and walks the user to the right flight. It is *not* a separate
search path — the wizard's only output is the same profile object the manual form builds,
so both paths feed one scorer. (Multi-city is in scope for the tab strip and out of scope
for the scorer until NYC→Europe round trips work end to end.)

### Row 1 — origin and destination

Address-level, not city-level. Placeholder text says so explicitly: *city, airport code,
or — better — a street address, neighbourhood or postal code.* The more specific the user
is, the better we rank, and the placeholder is where we teach that.

This is what makes the ground-access layer real. Brooklyn → EWR is an Uber fare and an hour;
Brooklyn → JFK may be a subway ride. That difference can outrank a cheaper ticket, and it
is invisible on every competing product.

The same applies on the **arrival** side: the trip does not end at the arrival airport, and
a destination address lets us charge the far-side ground access too.

### Row 2 — budget and points

- **Maximum budget** — a hard ceiling. Over it, an option is filtered (and disclosed as
  filtered; see *Filters are demotions* below).
- **Target budget** — a soft anchor. Not a filter; it shifts the weights.
- **Points and miles** — loyalty programmes and credit-card currencies, with balances.
- **Incidental budget** — deliberate padding the user is willing to spend: a lounge day
  pass they don't have access to, a change fee on a cheap carrier, an extra bag, seat
  selection. This is the budget for things that are *worth buying*, and it lets the scorer
  propose them instead of pretending every itinerary is bought bare.

### Row 3 — options

Practical, not the usual "depart between 8 and 10am" pickers that make users strangle their
own funnel before they've seen anything. Candidates:

- Allow red-eyes *(and show what the arrival morning costs)*
- Long layovers welcome if I can leave the airport *(a 9-hour layover in Lisbon is a
  feature; it needs transit rules, bag rules and daylight to qualify)*
- Checked bags: 0 / 1 / 2 *(drives the cheapest arithmetic win we have)*
- Must have an overhead bin *(kills several transatlantic Basic fares outright)*
- Party size *(three travellers make a car to EWR cheaper than three subway fares — this
  changes the ground-access answer, not just the ticket total)*
- Which NYC airports I'll actually use
- Self-transfer / separate tickets OK? *(no rebooking duty on a misconnect — a large risk
  term, and users do not know this)*
- Minimum connection time I'm comfortable with
- Wifi matters / doesn't
- Lounge access matters *(ties to the incidental budget)*
- What's waiting for me on arrival *(a 9am meeting the next morning is a downside-severity
  multiplier, not a preference)*

### Row 4 — the target

One clear, three-way choice: **Fastest · Cheapest · Most comfortable**.

This re-orders the list. **It does not change the grade** — see *Grade vs. order* below,
which is the single most important structural decision on this page.

## After search — the dream-trip prompt

On submit, a popup: *"Anything that would make this your ideal flight?"* — free text with a
**dictate** button, because this is exactly the thing people say out loud and never type:

> "I'd really like a bulkhead seat, I'd like to fly a 787 if possible, and I really don't
> want to fly American."

An LLM parses that into two things and only two things:

1. **Weights** — bounded, validated numbers on the existing comfort terms (aircraft
   preference, seat type, carrier affinity).
2. **Hard filters** — e.g. exclude American.

It does not rank. See hard rule 1.

## The recommendation box

Above the results, generated human-readable prose over the results that actually came back:

> *You can save 30%, but you'd fly from Newark — an hour from your address and about a $75
> rideshare at that hour.*

This is the narrator, and it reads the ledger. It does not compute anything.

## The results

**Top three picks**, then a light separator, then everything else.

### The bar

One horizontal bar per itinerary. **Total bar length is total door-to-door trip time**, so a
ten-hour trip is literally twice the bar of a five-hour one. Segments, left to right:

1. Time from the user's address to the airport
2. Airport wait — typical security/check-in backup at *that* airport at *that* hour
3. Flight
4. Layover
5. Flight *(repeat 4–5 per connection)*
6. Arrival airport to the final address

Each segment is coloured on its own scale — green for normal, through amber, to red for
unusual. A twenty-hour layover is red. An hour of security at EWR at 6am is amber. The
colour means *this segment is costing you time or dignity beyond what's normal for its
kind*, and it is scaled per segment type, not globally: two hours of flying is not the same
news as two hours of layover.

Left of the bar: airline logo and the basic line. Right of the bar: **the grade**.

### The grade

Grade-school letters, `A+` through `F`, coloured green→red to match.

### Expanding a row

A disclosure arrow flips the row open to reveal:

- Flight numbers, aircraft, terminals, times
- **The good and the bad** — two itemized lists. Bad: very long layover, congested airport,
  older cabin, bags not included. Good: high on-time rate, top-rated airport, nonstop.
- **Book** — a popup of places to book with approximate prices, favouring booking direct
  with the airline, then reputable OTAs.

The best estimated price and its source show on the collapsed row too. Nobody should have
to expand a row to learn the price.

### Points and miles pricing

If the user gave us balances, an award option prices as **taxes + points**, not dollars:

> *$12.40 + 100,000 Chase points* instead of *$1,000*

including transfers — if they hold Chase points and the award is on a Chase transfer
partner, we say so. See *Points are a currency, not an exception* below for how this stays
inside hard rule 3.

---

## Four tensions with the hard rules, and how they resolve

The UI brief, read literally, breaks rules 1, 3 and 4. Each resolves cleanly, and the
resolutions are load-bearing — they are why this stays one deterministic product instead of
an LLM with a flight-shaped skin.

### 1. Grade vs. order — one function, two calls

Patrick: *"the grading letter will remain unchanged"* when the user switches to Cheapest,
but bargains move to the top.

That reads like two ranking functions, which rule 3 forbids. It isn't:

- **The grade** is `effective_cost` evaluated at a **fixed reference profile** — a neutral
  traveller with a standard hourly value and standard comfort weights — expressed against
  the ticket price. It answers *"is this a good deal in the abstract?"* and it is the same
  for every user, which is what makes it trustworthy.
- **The order** is `effective_cost` evaluated at **this user's profile**.

Same function, two sets of weights. A dirt-cheap junk itinerary with a nine-hour layover
sorts first under Cheapest and still shows a `D`. That is the honest and useful behaviour,
and it costs us no second model.

### 2. The AI does not rank — it authors data, offline

Patrick: *"a higher ranked airline based on what AI thinks, for example Emirates would rank
higher than Frontier."*

If that judgment happens at query time, the same search returns a different order tomorrow
and rule 1 is dead. So it doesn't happen at query time. **Carrier quality is a curated
number in the enrichment DB**, authored once — with LLM help, reviewed by a human, version
stamped — and from then on it is just data the scorer multiplies. Emirates outranks Frontier
because a row says so, not because a model felt it.

This is the general pattern, and it applies to every "use AI to…" in the brief: the model
writes the table, the arithmetic reads it. What the model does *live* is exactly two things
— parse intent into bounded weights on the way in, and narrate the ledger on the way out.

### 3. Points are a currency, not an exception

Rule 3 says everything resolves to dollars, and the brief wants prices quoted in points.
Both hold:

- Every points currency has a **cents-per-point valuation** (a default we ship, overridable
  per user). An award option's `effective_cost` uses `taxes + points × cpp`, in dollars, and
  is ranked against cash options on exactly the same axis.
- The **display** is dual — `$12.40 + 100,000 Chase points` — because that is what the user
  is actually paying. Display and ranking are different layers.
- **Transfer routes are a curated graph**, not live LLM arithmetic: partner pairs, transfer
  ratios, minimum increments, and — the one everybody forgets — **transfer latency**. Points
  that take three days to move are not available for a flight tomorrow, and an itinerary
  priced on them is a lie.

Known gap, flagged now: **award availability is not in cash inventory.** No cash flight API
tells us whether a saver award seat exists on that flight. Until we have an award feed, the
points path can only ever say *"if a saver seat is available, this costs 100k Chase + $12"* —
and the UI must say "if", or we are shipping a promise we cannot keep.

### 4. Filters are demotions — rule 4 does not get an exception

Patrick: *"if they don't want to fly American, we're not gonna show them American flights."*

Rule 4 says anything pushed down gets an itemized reason and a one-click override. A filter
is the most aggressive possible push-down, so it needs the *most* disclosure, not the least.

The resolution is a single quiet line under the results:

> *3 options hidden — 2 American Airlines (you excluded), 1 over your $900 maximum. Show*

One line, one click to undo. The user's exclusion is respected; nothing is secretly
disappeared. This is the difference between a filter and a kickback, and it costs one row of
UI.

---

## Two more structural notes

**The bar, the grade, the good/bad lists and the narrator all read the same ledger.** They
are four renderings of one object, never four calculations. If the bar's layover segment
turns red while the ledger has no layover penalty, we have grown a second model by accident
and the product is lying in one of two places. The ledger is computed once; everything on
screen is a projection of it.

**The bar measures time; the grade measures dollars.** They are allowed to disagree — a
long, cheap, comfortable trip is a long bar and a good grade. Nobody should ever sort by bar
length; it is a disclosure, not a ranking.

**Colour is never the only channel.** Green→red segments and A+→F grades both need a second
signal — the letter itself, a label on hover, a pattern — or the whole ranking is invisible
to a colourblind user.

---

# Stack

**Python 3, stdlib only. No build tooling, no `node_modules`, no framework.**
Same shape as the AI app, for reasons that turned out to be concrete rather than
sentimental.

The decision was made by the video background, of all things. The backdrop needs Apple's
ATV aerials, and those cannot be played from a browser: sylvan puts the `moov` atom after
370MB of `mdat`, so nothing starts until the whole file lands, and phobos is http-only.
`millenai.py` already solved this — download once, rewrite the file so `moov` precedes
`mdat`, cache it, stream it same-origin with Range support — in about 120 lines of pure
stdlib, with no ffmpeg dependency. That code ported over unchanged and is tested. Choosing
a different language would have meant reimplementing a solved, load-bearing, byte-level
problem for no gain.

Everything else agrees with that choice:

- **The scorer is arithmetic over a few hundred itineraries.** Microseconds in any
  language. Performance is not the constraint here and choosing for it would be choosing
  for the wrong thing — the constraints are the enrichment data and the honesty of the
  ledger.
- **The scorer stays a pure function**: `score(itinerary, profile, prefs) -> ledger`. No
  clock, no network, no I/O. Import the module, hand it a fixture dict, assert on the
  ledger. That is the "trivially testable in isolation" constraint, met by construction.
- **Fixtures are JSON**, hand-writable and diff-readable, validated against the schema at
  load. Nothing about them is language-specific — the corpus outlives any rewrite.
- **The enrichment DB is SQLite** when it arrives, which is also stdlib.
- **One language across server, scorer and tooling**, which for a solo shipper is worth
  more than any micro-benchmark.

The honest risk: the UI is already a large single HTML file, and that is exactly how
`millenai.py` grew to 16.8k lines. If the interface keeps growing, split the page into real
files served from `concorde-travel/ui/` — the server already serves that directory — rather
than letting it become a second monolith. That is a decision to revisit deliberately, not
to drift into.

# Where the code is

```
concorde-travel/
  server.py              stdlib HTTP server: serves the UI, plus the sky pipeline
                         (download -> fast-start remux -> cache -> Range-serve)
  ui/index.html          the whole draft interface — markup, styles, fixture data,
                         the stand-in scorer and the render layer
  tests/
    test_faststart.py    guards the byte-level remux (stco/co64 offset shifting)
fixtures/                the fixture corpus
docs/design.md           this file
```

Run it:

```bash
python3 concorde-travel/server.py        # http://127.0.0.1:9897
CONCORDEGO_PORT=9898 python3 concorde-travel/server.py
python3 concorde-travel/tests/test_faststart.py
```

Port note: **8884–8930 belong to the model engines** — never bind there. ConcordeGo uses
9897.

The first load warms one clip in the background (a few hundred MB) and shows the same
loading bar the AI app does; a canvas-painted night skyline carries the page until it
lands, and is also what you get if you open `ui/index.html` as a bare `file://` with no
server. Cached clips live in `~/.concordego/sky`, last six kept, LRU by last-played.

## What the draft interface actually demonstrates

- The ledger, the bar, the grade, the good/bad lists and the recommendation prose are
  **five renderings of one computed object**. Nothing on screen is independently guessed.
- **Grade vs. order**: switching Fastest / Cheapest / Most comfortable re-orders the list
  and never moves a letter. The F stays an F at the top of the Cheapest list.
- **Overrides**: every demotion has a "Don't count this" that removes the line and
  re-totals, per hard rule 4.
- **Filters are disclosed**, not silent — excluded options collapse to one line with a
  reason and a way back.
- **Hedged claims only**: no aircraft or connectivity statement appears without its
  observed frequency and sample size.

Everything in it is fixture data. There is no flight API anywhere in this repo.

---

# Corrections and gaps

Research done while building the fixture corpus contradicted several things in the brief.
The brief above is left as written — it is the product direction and it is right about the
product. These are the factual corrections, kept separately so neither overwrites the
other.

## Corrections

**The subway is $3.00, not $2.90.** It changed on 2026-01-04. Every fixture now carries an
`effective_date` beside each fare, because the MTA moved it twice inside this corpus's
likely lifetime.

**"Bushwick to JFK is $2.90" understates the realistic path by about 4×.** $3.00 buys only
the Lefferts Blvd route — the JFK AirTrain is free everywhere *except* Jamaica and Howard
Beach — and that route is three transfers, one of them a bus, roughly 90 minutes. The
default one-transfer path is **$11.75** ($3.00 + $8.75 AirTrain) at about 78 minutes, and a
car is $62 at about 37. All three are carried in the fixture, because which one wins is
exactly the judgment the scorer exists to make.

**"Transit doesn't run at that hour" is wrong, and the wrong reason is more dangerous than
a wrong number.** The NYC subway runs 24 hours; the L runs all night at ~12-minute
headways; PATH is 24/7. What actually breaks a 6:10am Newark departure is the **New Jersey
end** — NJ Transit's Newark Liberty Airport rail stop is served roughly 05:00–01:00, and
that departure needs you at the terminal by about 04:30. Infeasibility is computed from the
weakest link and the ledger names that link. A blanket "no transit" is precisely the
confidently-wrong static rule the brief warns against two lines later.

**The ~$90 car is right for that hour and wrong as a constant.** The published all-hours
route average for Bushwick→EWR is $116. The 4:15am pickup a 6:10 departure actually
requires has no surge, which is what puts it in the $80–95 band.

**Outbound tolls to New Jersey are $0.** Every Port Authority Hudson and Staten Island
crossing is tolled NY-bound only. A symmetric toll model overcharges this trip by about $15
and would wrongly demote every EWR itinerary in the corpus. Only the Verrazzano is
bidirectional.

**LGA cannot be a transatlantic origin.** The perimeter rule bars nonstops beyond 1,500
miles, so LGA has zero European nonstop service and cannot have any. It is worth keeping as
a ground-access case — the free 24-hour Q70 makes it the only NYC airport with a $3.00
all-hours path — but not as an origin in this scope.

**The 1:40am CDG arrival in the worked ledger cannot be scheduled.** CDG has a night
movement ban: arrivals prohibited on-block 00:30–05:29 local, departures 00:00–04:59. The
latest legal scheduled arrival is 00:29. Worse, no NYC–CDG nonstop could produce it anyway
— eastbound transatlantic is structurally either a redeye landing 06:00–13:30 or a day
flight landing 20:00–22:30, so that window is empty of scheduled arrivals by construction.
The example is real as a **delay**, which is how the fixture builds it: a legal scheduled
00:10, an actual on-block of 01:40. The validator rejects any scheduled arrival inside a
curfew.

**What makes that layover punishing is not the hour.** It is that a US→CDG→Schengen
passenger clears immigration at CDG, which ejects them from the 2E transit zone at an hour
when 2F security is not open and landside Terminal 2 will not re-admit them. Neither
airside nor able to leave. A 2E→2E connection at the same minute stays airside all night
and is a completely different object — so a layover is keyed by (airport, arrival hall,
departure hall, local clock, onward Schengen status), never by duration.

**"Many transatlantic Basic fares include a checked bag" is no longer true.** Of thirteen
carriers in scope, exactly one — TAP — bundles a bag into the fare it calls Basic. The rest
are bag-free at their cheapest tier; the industry closed that gap in 2024–2026. The brief's
bags conclusion still holds, but not for the stated reason, and anything built on pre-2024
intuition will be wrong.

**United's transatlantic Basic does not bar the overhead bin.** True at launch, not the
2026 rule — transatlantic Basic carries a full-size carry-on. What survives is real but
different: it boards last, so bin space is often gone. That is a comfort line about
boarding group, not a baggage exclusion, and conflating them invents a fee that does not
exist.

**American's bag fee is keyed to ticket ISSUE date, not travel date** ($75 before
2026-05-17, $85 on or after 2026-05-18). A fixture carrying only a travel date cannot
resolve the right fee.

**The worked ledger does not reconcile.** It shows a ground-access line only on the option
being demoted. If ground access is an absolute term rather than a delta, every option shows
its ground line or the numbers do not add up to the stated total.

## Gaps in the model

Real, not fatal, listed so they are decisions rather than oversights.

**The formula is silently single-passenger.** Tickets and bags scale per person; a car to
EWR does not scale at all; three subway fares do; and a party of four does not value one
elapsed hour at 4×. There is no party-size policy anywhere in the model.

**The formula is one-directional and the product sells round trips.**
`ground_access_cost(origin, airport, local departure time)` is written singular. A round
trip has four ground legs at four different local hours, and arriving at JFK at 11:20pm is
a different ground problem from leaving it at 6am.

**`ticket_price` is not a scalar.** It is a priced offer: a party, one or more tickets, a
source currency with a pinned FX rate, a validity window. Collapsing it early is the root
of most price-side bugs — and with a non-USD fare, "one currency" is a lie at the boundary
unless the rate is pinned in the fixture.

**Risk cannot be computed from the inputs the formula names.** Severity is dominated by
*ticketing topology*, which appears nowhere: a protected misconnect is a rebooking, a
self-transfer misconnect is a whole new ticket at the walk-up fare.

**An overnight that forces a hotel is cash, not discomfort.** It currently lives inside
`comfort_penalties` and belongs beside `baggage_cost`.

**EU261 and US DOT compensation are a real dollar offset to severity** and the model has no
term for them.

**Refundability and change fees are option value**, not a price term and not a comfort
term — a contingent outflow whose size depends on a user-supplied probability, exactly like
`user_hourly_value` is a user-supplied rate.

**Loyalty earning is a real dollar term and is missing.** Basic Economy commonly earns zero
redeemable miles and zero status credit, which for a status-chasing traveller can exceed
the fare gap the re-ranker is agonising over. Its absence biases systematically toward
Basic.

**Award availability is not in cash inventory.** No cash flight API knows whether a saver
seat exists, so the points path can only ever say *"if a saver seat is open, this costs
100k + $12"* — and the interface must say "if".

---

# The scorer

Build step two, done. `concorde-travel/scorer.py`, ~500 lines, stdlib only.

```python
from scorer import score, score_all, grade
ledger = score(scenario, option, "reference")
```

`score()` is a pure function of `(scenario, option, profile)`. No clock, no network, no
file reads, no imports from the rest of the project. Hand it the same dict twice and it
returns the same integers twice, on any machine, forever. That is hard rule 1 — and it is
the only reason an LLM can be let near this product at all: the model shapes the weights
going in and writes the prose coming out, and neither can move a row.

Run `python3 concorde-travel/scorer.py` to print every fixture's ledger under every
profile.

## What it does that a simpler thing would not

**Ground access is a choice, not a lookup.** Each end carries a list of modes and the
scorer picks the one that minimises `fare + duration × hourly` for *this* traveller. The
same Bushwick→JFK query resolves to the $3.00 three-transfer route at a low hourly value
and a $62 car at a high one. An infeasible mode is rejected with the leg that breaks it
named in the ledger, never a blanket "no transit".

**Baggage is a reduction over ticket topology.** Fees index from the first piece *beyond*
the included allowance, tiers reset per passenger, and the whole calculation runs once per
ticketing boundary — which is why the self-transfer in fixture 03 pays $368 for two bags
and the intra-Europe leg costs more than the ocean crossing.

**Layover time is priced by whether it can be used.** This was the one real modelling
mistake found while building: charging every layover hour at one rate made a Schiphol
afternoon score like a night on the floor at CDG. The fraction of the hourly rate now
depends on the situation — dead 0.70, stuck-in-the-terminal 0.35, able-to-leave 0.10 —
and the same 250 minutes now prices at **+$36 at AMS, −$68 at LHR and −$340 at CDG**.

**A closed lounge is worth nothing.** Service windows are strict `HH:MM-HH:MM`, matched
against the local clock at landing, with an eligibility flag. Crediting a lounge the
passenger cannot enter is how a ledger starts lying.

**Carrier quality is curated data, read not decided.** `option.carrier_rating` is a
reviewed number with a source and an as-of date. The model may *author* that table; only
the arithmetic may read it. A rating decided at query time would return a different order
tomorrow.

**Unknown costs something.** Abstentions are priced, so the least-documented itinerary
does not win by saying nothing.

## The grade, once more

`grade()` scores the option at the **reference** profile against the route's
`route_par_cents` and maps the ratio onto bands. It does not take the caller's profile.
In fixture 01 the budget carrier tops the Cheapest list and sits last everywhere else,
and its `B+` does not move — which is the behaviour the whole target selector exists for.

## Tests

```bash
python3 concorde-travel/tests/test_scorer.py    # 81 checks
```

It executes each fixture's own `expect` block — which lines appear, with what sign, what
their evidence must mention, what must *not* appear, and what order the options land in
under each profile — plus properties no single fixture can state: determinism,
reconciliation to the cent, an **independent** grade oracle, relational comparisons
between fixtures' lines, and the model's own invariants.

**Both suites are mutation-tested**: 12 of 12 seeded scorer faults caught, 10 of 10
fixture faults. Two of those guards exist because a mutant walked through first — the
grade-stability check was asking the same broken function the same question twice, and the
"don't credit an excursion into a shut airport" guard had nothing exercising it.

## Known gaps

- **Cascading misconnects.** Each layover's risk is computed independently, so missing the
  first connection in fixture 02 does not propagate to the second. Real, and it understates
  multi-stop risk.
- **No tight-buffer self-transfer in the corpus.** Fixture 03's has a 4h50m buffer, so its
  misconnect probability sits at the floor and the severity term never dominates. The
  fixture that would make it dominate is owed.
- Round trips, parties larger than one, and arrival-time-of-day value remain unmodelled —
  see *Corrections and gaps* above.
- ~~The draft interface still runs its own JavaScript stand-in.~~ Done — see below.


---

# The interface, wired

The page owns no model. It posts the form to `/api/score` and draws what comes back.
About 380 lines of JavaScript fixture data and a duplicate scorer were deleted from
`ui/index.html`; two implementations of one model is two models, and they drift.

```
GET  /api/fixtures   the corpus, for the scenario picker
POST /api/score      {fixture, profile, bags, filters, weights, prefs, incidental_cents}
                  -> {options: [...], hidden: [...]}
```

`server.py` reshapes a `Ledger` and a `timeline()` into what the interface draws and
computes nothing of its own. The moment it starts deciding what a layover is worth, there
are two scorers again.

**`timeline()` lives in the scorer, not the page.** It returns the door-to-door bar as
blocks of time derived from the same numbers the ledger used — including the *same* ground
mode, via one `chosen_ground()` both call. A test asserts the bar's minutes sum to the
ledger's door-to-door figure under every profile, which is the property that stops the bar
becoming a second opinion about the same trip.

Overrides stay client-side: switching a line off removes it and re-adds the total, and
never round-trips to the scorer. A demotion the user has dismissed is a presentation
decision, not a different model.

## Copy

Labels are labels. Anything that needed a sentence became a hover note on a small `i`
rather than a paragraph under the field — "Incidentals" with *"padding for extras worth
buying: a lounge pass, a change fee, a seat with legroom"* on hover, instead of forty words
of explanation the user reads once and never again. Five explanatory paragraphs became six
tooltips.


---

# Interface, second pass

**Incidentals is gone from the form.** It was a dollar box asking the user to price
something they have no way to price. The scorer still has the term and still reads
`incidental_allowance_cents` from the scenario — a lounge credit is gated on it — the page
just stopped asking. Budget is now two fields, Maximum and Target, each with a hover note.

**"What you'll put up with" became "Features"**: ten square tiles, icon over a word, five to
a row and three at phone width. Every tile maps to something the scorer already reads —
eight bounded weights and two hard filters — because a control that changes nothing is
worse than no control.

| Tile | What it does |
|---|---|
| Nonstop, One ticket | hard filters (`nonstop_only`, `no_self_transfer`) |
| Lie-flat | hard filter (`min_cabin`) |
| Red-eyes fine | `prefs.redeye_ok` |
| Short layovers, A day in town | `weights.layover`, `weights.excursion` |
| Fast wi-fi, Legroom, Lounge, Bags included | `weights.wifi / pitch / lounge / bags` |

*Overhead bin* was dropped: every aircraft has one, and transatlantic Basic Economy has
carried a full-size cabin bag for years. What survives of that idea is a boarding-group
line in the ledger, which is a different thing.

*Lie-flat* filters every option out of the current corpus, which is all economy — and says
so, in the disclosure row. That is the honest answer and it exercises the filter path.

**Make a wish** replaces the old dream-trip modal. A starred button beside Search flights
opens a chromeless card — no title bar, no close box, the shape the desktop app uses for
its update sheet. It explains what is about to happen, then **Let's go** starts the
microphone: the pitch is replaced by a live transcript with a pulsing Recording indicator,
and **Grant it** applies what was heard. Without a speech engine the same card becomes a
text box rather than a dead end.

**The bar lost its inline labels.** They clipped on narrow segments and were noise on wide
ones. Hovering a segment now highlights it, dims its neighbours and shows one short line —
`Flight · BA 178 · 7h30m`, `Layover · CDG · 6h35m · lands 01:40`. Flight blocks are blue;
the green-to-red ramp is reserved for the parts of a trip that cost you time. The tooltip
text comes from `Leg.tip` in the scorer, so it cannot drift from the bar it describes.


---

# Rideshare, and the Guide me wizard

## Rideshare, yes or no

A three-way beside the budget numbers: **Yes** prices a car both ends, **No** prices public
transport, **Either** lets the arithmetic pick (the default, and usually the better answer).

Ground modes now carry `mode_kind` — `transit | rideshare | drive | walk` — because
deciding this by pattern-matching the prose in `mode` would be the same mistake as reading
"closes by 23:30" as a service window. "Subway to Penn, NJ Transit, AirTrain Newark" is
transit, and nothing should have to parse that string to know it.

The preference **narrows** the choice, it does not override it. Ask for public transport on
the 06:10 Newark departure and you still get the car, with the ledger saying why: *"You
asked for transit: NJ Transit's Newark Liberty Airport rail stop is served roughly
05:00–01:00."* A silent substitution would be the worst of both.

Time follows automatically, because the rideshare mode carries its own door-to-door
figure. On the JFK option: **+$134 in fares, −47 minutes door to door, +$107 effective.**
That trade is the whole point of asking.

Rideshare options were added to every scenario that lacked one, so the control is never
inert. Two tests guard it — that the preference is honoured where it can be, and that the
ledger says so where it cannot.

## Guide me

Six steps, one question each, big answers: where you are headed, how many bags, what an
hour of your time is worth today, how you are getting to the airport, what matters on
board, and what would ruin the trip. A single-answer step advances on click; multi-answer
steps wait for Next. "Skip the questions" is always available.

**It is not a second search.** The last step writes every answer into the same `STATE` and
the same form controls the manual path uses, switches to the Round trip tab so the user can
*see* what was filled in on their behalf, and hands over to the same `render()`. A wizard
that scored in secret would be a second product — and the first thing that would drift.

The route cards are built from the corpus: `Bushwick → London · EWR/JFK → LHR · 3 options`.
A step whose options are empty is dropped rather than shown blank.


---

# The ledger narrator

Build step five, done. `concorde-travel/narrator.py`.

Hard rule 1 says the LLM narrates and does not rank. This module is the whole of the
"narrates" half, and it is built so the rule holds by construction rather than by good
intentions.

## The model does not do arithmetic

`brief()` precomputes **every number the prose is allowed to contain** — the saving
percentage, the gap, each effective cost, each grade, each duration — and hands the model
a closed list of them. The model picks words. It cannot divide two figures and quote the
result, because a figure it computed would not be in the allowed set.

That inversion is the whole design. The usual approach hands a model the data and asks it
to be careful; this one makes carelessness structurally detectable.

## Verify, then fall back

Every generated sentence is checked: each dollar figure, percentage, duration and airport
code must be one the arithmetic produced, and a list of certainty phrases — *"you will
have"*, *"guaranteed"*, *"is equipped with"* — is banned outright, because hard rule 2
forbids unhedged equipment claims. Anything failing is discarded and the deterministic
template ships.

The template is the **floor, not a degraded mode**. No key, no package, no network, or a
failed check, and the page still says something true. Nothing external is load-bearing.

## Why there is no temperature

`temperature` is removed on Claude Opus 5 and returns a 400 — so determinism cannot be
bought with sampling settings even in principle. Verification carries it instead, which is
the sturdier answer anyway: a checked sentence at any temperature beats an unchecked one at
zero.

Narration is the one non-deterministic surface in ConcordeGo, and the only one where that
is safe. Prose cannot reorder a list.

## The one dependency, and why it is optional

The narrator uses the official `anthropic` SDK rather than hand-rolled HTTP. It is imported
**lazily, inside the call**, so `pip install anthropic` buys better prose and its absence
costs nothing: the scorer, the fixtures and every test remain stdlib-only and offline. That
keeps the stdlib commitment where it matters — the deterministic core — without hand-rolling
an API client next to a maintained one.

## Wiring

`/api/score` carries the template narration inline, so the page never paints an empty box.
`/api/narrate` re-scores the same request and upgrades the prose if a model is reachable;
the page swaps it in when it arrives, keyed by a sequence number so a slow answer cannot
overwrite a newer search. The UI's own `narrate()` is gone — same lesson as the scorer.

The strapline under the box changes with the source, because the reader should know which
they are looking at: *"Written from the ledger"* for the template, *"Written by a model from
the ledger, and checked against it — every figure here was computed, not phrased"* for the
model.

## Tests

```bash
python3 concorde-travel/tests/test_narrator.py     # 42 checks, no key or network needed
```

The verifier is tested adversarially with sentences that are fluent, plausible and wrong: a
dollar figure nobody computed, a percentage nobody computed, an airport not in the brief, an
unhedged wifi promise, a guarantee about the aircraft, an invented duration, an essay.

It also tests the **false-positive** direction, which caught a real bug: the money regex
swallowed a trailing sentence comma, so `"at $455, $933 effective"` read as an invented
`$455,` and the verifier rejected its own template. Every narration would have silently
degraded to the fallback — and nobody would have noticed, because the fallback is fine.


---

# The live inventory adapter

Build step six, done — the last one. `concorde-travel/adapter.py`.

A third-party search goes in, a scenario the scorer already reads comes out. Everything
downstream is untouched: `server.py` runs fixtures and live inventory through one
`_score_scenario()`, so a live search cannot be judged by different rules than the corpus.

## Hard rule 5, enforced rather than trusted

The scorer and its tests never import the adapter, and `test_adapter.py` asserts exactly
that. The adapter's own tests replay `adapter_samples/kiwi-jfk-lhr.json` — a real Kiwi.com
response captured once — so the dependency is recorded, not live.

## What a real feed actually gives you

This was measured against that payload rather than guessed at, and it reshaped the module:

| The feed has | The scorer needs |
|---|---|
| `"2026-11-12T20:00:00"` | an explicit UTC offset — 01:30 on 1 Nov at JFK happens twice |
| `carrier: "KL"`, `flightNumber: "KL6101"` | the **operating** carrier; that route is a codeshare and KLM does not fly it |
| nothing | an equipment code, for any cabin or connectivity claim |
| `baggage: {checkedBag: 2}` | a fee schedule and a fare brand |

So the feed supplies the skeleton and the price. **Everything that makes this product
different is the join against `enrichment/`**, and where that join misses the adapter
abstains loudly rather than filling in.

Eleven of thirteen segments in the sample are codeshares. Every aircraft claim abstains —
hard rule 2 is not a stylistic preference at this point, the data required to break it does
not exist.

## Coverage is part of the answer

`coverage()` reports what was known and what was guessed, and the page prints it above the
results:

> **Live inventory.** Grades are indicative only — most enrichment is missing.
> · no aircraft, cabin or connectivity claim can be made: the feed carries no equipment code
> · 11 of 13 segments are codeshares, so even the operating carrier is unknown
> · no on-time record joined for any segment
> · no baggage fee schedule: the feed prices bags inline

A grade computed over mostly-abstained enrichment is not the same object as one over a
curated route. Presenting both with equal confidence would be the same dishonesty hard rule
4 exists to prevent, one layer up.

## Ticket topology, recovered

Kiwi encodes ticket boundaries in its itinerary id: chunks sharing a prefix are one
e-ticket, a new prefix is a new one. That is the self-transfer signal the baggage and risk
terms need and it is otherwise invisible. A three-segment two-ticket itinerary correctly
gets **both** kinds of layover — one inside a ticket that keeps its through-check, one
crossing the boundary that loses it. My first test demanded every layover on a self-transfer
be unprotected; the test was wrong, not the adapter.

## The enrichment store

`enrichment/` is build step three, extracted from where the fixtures had inlined it:
airports (zones with curated DST transition dates, MCT, service hours, inter-terminal
moves), carriers (reviewed ratings with a source and date), ground access by origin and
hour, route par.

The DST dates are curated rather than read from a timezone database on purpose — the scorer
must run with no I/O and no versioned external state, and a tzdata update that silently
moved a golden number would be very hard to notice.

**An uncurated airport drops the itinerary rather than being guessed.** That is the scope
discipline from the brief, working. When BCN turned up in a real search it was curated
properly; the guard that catches this now tests the mechanism with a stripped enrichment
rather than relying on some airport happening to be missing.

## Wired to a live API

`concorde-travel/live.py` holds the credential, the quota and the cache. `/api/live` calls
the provider when a key is configured and replays the recorded sample when it is not.


---

# Key, quota and cache

One module holds a credential — `concorde-travel/live.py` — and it decides one thing: may we
call the provider right now. The scorer never imports it.

## Setting a key

`export CONCORDEGO_FLIGHT_KEY=...`, or `~/.concordego/cloud.json` (0600, created empty by
`live.py status`). The environment wins, so a key need never touch disk. With no key the
live option stays on the recorded search rather than offering a control that cannot work.

**The provider's wire format is config, not code.** The shipped Tequila profile comes from
published documentation and is **unverified against the live service**: every Kiwi host was
unreachable from where this was built, so the honest thing was to make the query string
editable rather than pretend it had been tested. Correcting a parameter name should not mean
editing Python.

## Four decisions with consequences

**The probe uses the runtime payload.** `probe()` runs a real `search()` rather than some
cheaper validation call. A probe shaped differently from the real request will validate a key
that then fails on every actual search — the exact trap recorded in CLAUDE.md, and the test
suite asserts `probe()` contains no `urlopen` of its own.

**Cache before quota.** A cached answer costs nothing, so it must not spend a call. Getting
this the wrong way round is how a cache stops being a cache.

**Quota is reserved before the call, not counted after.** A request that times out mid-flight
has already consumed the provider's allowance; counting afterwards undercounts exactly when
it matters. Only a connection that never reached them is refunded, and the tests check both
directions — a 403 counts, a DNS failure does not.

**The key never leaves the module.** `redact()` runs over every string that escapes,
including the provider's own error body, which is where a key most plausibly comes back at
you. A test asserts the secret appears in nothing returned from the no-key, HTTP-error,
network-error and status paths.

## Proven end to end without a real key

A stand-in provider on localhost — checking the auth header, and 403ing a bare
`Python-urllib` User-Agent the way real provider edges do — was used to walk the whole chain:

```
call 1   -> reached the provider, 7 options scored, top JFK-LHR $530 / $958 effective, A+
call 2   -> served from cache, day allowance unchanged
3 fresh  -> allowance spent, fourth refused: "daily limit reached: 3 of 3 calls used today"
provider -> saw exactly 3 requests; the cache hit never reached it
```

The User-Agent guard is proven by that run: the stand-in refuses `Python-urllib` and the
real call got a 200.

## A second provider, and the state of the market

**Read this before shopping for a key, because three of the obvious answers are gone.**

| Provider | Self-serve today? | |
| --- | --- | --- |
| Amadeus Self-Service | **No** | Portal decommissioned 2026-07-17, keys disabled. Enterprise only now, which is a sales process. |
| Kiwi Tequila | **No** | Self-serve closed May 2024; invite-only partner program. |
| Google Flights | **Never existed** | QPX Express retired 2018-04-10. Everything sold as one scrapes the consumer page and returns *less* structured data than Kiwi. |
| Travelpayouts | Yes, free | But the free tier is **cached price data** — no aircraft, no fare brand, no operating carrier. The real-time search API needs 50,000 MAU. |
| Sabre / Travelport | No | Account-rep activation. |
| **Duffel** | **Yes — the default** | ~1 minute at `app.duffel.com/join`, instant sandbox, permanently free test mode. Live prices need identity verification. |

The Amadeus profile is kept for its **schema**, not its availability: Amadeus Enterprise
still speaks Flight Offers Search v2, so `from_amadeus` applies to anyone with that access,
and it remains the reference for what a rich feed looks like. It is not a key you can go
and get.

**Duffel is the self-serve path and is now the default provider.** `from_duffel` is built.
Its Offer schema carries `operating_carrier` and `marketing_carrier` per segment,
`aircraft`, `fare_brand_name`, per-passenger `baggages`, and fare `conditions` — plus one
thing neither other feed has, below. Its test mode is a fictional carrier with fake prices,
so the curated joins will not light up against it, but the *shape* is real.

### What the live service actually returned

A real `duffel_test_` key, JFK–LHR, 172 offers in 3.4s, first call. **The profile was
correct as written** — parameter names, POST body, `Duffel-Version`, static switches, all of
it — which is the first time any of the three has been verified against a live service.

Three findings from that capture, one of them a correction to the section below.

**`available_services` was empty on all 172 offers.** The bag-quote capability is real but
lives on `GET /air/offers/{id}`, a second call per offer, not on the search response. The
code path is built and tested; on `/api/live` today every bag is priced from `fares.json` or
the pessimistic default. Fetching details for the top N offers is the obvious next step.

**`segment.passengers[].cabin.amenities` on 269 of 272 segments.** Wifi
(`available` + `cost`), seat (`type`, `legroom`, `pitch`) and `power`. This is enrichment
layer 5 — the one the brief calls "hardest, biggest differentiator" — arriving in the feed
without a curated join, and it is the layer I had said was an ATPCO enterprise deal.

Observed values: pitch 28–32 inches (mode 31, which is exactly `Tuning.pitch_norm_inches`),
legroom `less` on 64 segments, wifi `paid` 151 / `free` 66 / `n/a` 52.

**`ngs_shelf` 1–3 on every slice** — IATA's standardised fare shelf, a better signal than
parsing prose brand names. Not yet used.

### Taking the amenities without breaking hard rule 2

`seat.pitch` is taken **directly** as `claims.seat_pitch_inches`. That field is a plain
number in the schema rather than a hedged claim, and deliberately so: a fare's pitch is a
published cabin-layout fact, not an observation of what turned up, and the scorer already
prices it linearly against a 31-inch norm. `power` is carried the same way. Both slots
already existed in the schema — it anticipated exactly this distinction.

**Wifi is not taken.** Mapping `wifi.available: true` onto `connectivity_oceanic` would be
the single easiest way to break hard rule 2 in this entire codebase, because it looks like
an upgrade. That field is an *observed* claim — value, observed frequency, sample size,
window, source, as-of — and an airline publishing "wifi: available" supplies none of those.
It is a statement about the product sold, not the metal that shows up, and equipment is
swapped after booking regardless. Manufacturing a frequency to fit the shape is exactly the
bookkeeping the rule exists to forbid.

So wifi is kept raw under `_feed.wifi_published` for the narrator and the interface, the
connectivity term goes on abstaining, and `mutate_adapter.py` carries a fault that turns a
published amenity into an observed certainty — if that ever stops being caught, the guard
is gone.

### The grade is saturated, and par is the reason

Scoring all 96 normalisable options at the reference profile:

```
effective cost   min $710   p25 $839   median $956   p75 $1185   p90 $3626
route par        $1050  (curated with no data behind it)
grades           A+:55  A:9  A-:6  B+:4  B:5  B-:2  C+:2  C:2  F:11
```

**57% of a real market gets the top grade.** A grade that half the inventory earns is not
telling anyone anything, and the grade is the headline element of the interface.

Par is meant to be "the all-in effective cost a CLEAN option achieves on this route". The
cheapest clean option here is $710 and the p25 is $839, so $1050 is roughly 30% too generous.
Candidate redistributions:

```
par $1050   A+:55  A:9   A-:6   B+:4   B:5   B-:2   C+:2  C:2   F:11
par  $900   A+:30  A:10  A-:13  B+:6   B:10  B-:3   C+:5  C:4   C-:3  D:1   F:11
par  $800   A+:12  A:5   A-:16  B+:7   B:14  B-:6   C+:10 C:5   C-:6  D:3   F:12
par  $750   A:14   A-:3  B+:16  B:6    B-:15 C+:10  C:7   C-:6  D:5   F:14
```

**That table was the wrong way to choose par, and the recommendation it produced is
withdrawn.** Every one of those numbers was read off the distribution of a single search.
Calibrate an absolute yardstick from a relative distribution and you get a relative
yardstick with extra steps: run the same method on a different date, against different
demand, and par moves — so the same flight earns a B in one search and a D in another. That
is precisely the property the grade must not have.

### Par is a specification

Par should be the effective cost of a **defined reference itinerary** on the route: nonstop,
main cabin with one checked bag, a carrier at the curated baseline rating, the 31-inch
transatlantic pitch norm, the overnight departure that is simply what this route is. It is
computable, auditable, explainable to a user in one sentence, and it moves only when the
route's structure moves or the specification is deliberately changed — never because the
market had a cheap Tuesday.

Scored against the real capture, that reference produces:

```
reference at a $350 ticket  ->  par $790 effective
reference at a $400 ticket  ->  par $840
reference at a $450 ticket  ->  par $890
reference at a $500 ticket  ->  par $940

the $450 reference, itemised - this is what par MEANS:
   Ticket                                    $450
   11h17m door to door at $35 an hour        $395
   LHR to London                             $92
   Nonstop, no misconnect exposure          −$80
   No on-time record for any segment         $30
   Bushwick to JFK at 13:09                   $3
   EFFECTIVE                                 $890
```

So `route_par_cents` should carry its basis beside it — the reference ticket price and the
date it was set — rather than being a bare number with no story. Choosing the reference
ticket price is still a product decision, but it is now a decision about *what a normal
fare on this route is*, which is answerable, rather than about where to put a letter.

### The absolute-grade property is now guarded

`test_scorer.py` re-grades every option against four different result sets (alone, with the
dearest half, with the cheapest half, reversed) and fails if any letter moves. A seeded
fault that computes par from the 25th percentile of the results is caught by it. Nothing
computes par from the result set today; the test exists so that nobody improves it into
doing so.

### The one field where a feed beats the moat

Duffel's `available_services` lists an extra checked bag as a **bookable service with a real
price**. That is the actual number the traveller would pay, so it outranks the curated
`fares.json` table — the only place in this product where the feed knows better. For those
options the bags arithmetic stops being an estimate, and `coverage()` reports it as a
**strength** rather than a gap, which is a category the coverage strip did not previously
have.

Two catches, both handled and both commented:

- It prices **per unit up to a `maximum_quantity`** and says nothing beyond, so pieces past
  the cap are topped up from the curated table and marked as estimates.
- An **empty list means unknown, not free.** Reading it as free is the mistake that loses
  money, and it is the same failure as treating an abstain as zero.

### Two things Duffel does differently, and one crash they exposed

**`fare_brand_name` is prose, not a code.** Duffel returns "Basic Economy" and "Economy
Light" where Amadeus returns `BASIC` and `LIGHT`. An exact uppercase match handles Amadeus
and misses Duffel entirely, which silently sends every option to the pessimistic default and
makes the curated table dead weight. `_brand_key()` matches a curated token as a **whole
word** — "Basic Economy" is a BASIC fare, "Economy Light" is a LIGHT one. Whole words
because a substring match makes "Economy Light" hit an `ECONOMY` row, and `SURPLUS`
contains `PLUS`.

**A `duffel_test_` token returns Duffel Airways (ZZ)** with invented prices and schedules.
The adapter handles it — an uncurated carrier keeps its option and loses its rating, a null
`aircraft` abstains — and adds a scenario note saying the inventory is fiction. A grade over
test-mode inventory means nothing and the page should not imply otherwise.

**The crash.** `scorer._bag_cost` raises on a piece it cannot price, deliberately: a fixture
that forgets to price a bag is a broken fixture. But a live traveller's bag load comes from
the request and is unbounded, while a published fee schedule stops after two or three
pieces — so a four-bag party took the entire search down, on the Amadeus path as well, which
had shipped that way. `_extend_tiers()` pads the ladder by repeating the **dearest**
published tier. Pessimistic on purpose: extrapolating downwards would make a heavy load look
cheap on exactly the fares that decline to publish a fourth-bag price. A test now walks
loads 0–9 on every feed.

### Mutation testing became executable

CLAUDE.md has claimed a mutation-testing standard since the scorer was written, but the runs
were ad hoc and nothing let the next person check it. `tests/mutate_adapter.py` now applies
13 real faults — reading a bag load off the fare, extrapolating a ladder from the cheapest
tier, guessing an offset for an uncurated airport, turning a hedged cabin claim into a
certainty — and fails if `test_adapter.py` misses any.

Each mutant is scoped to **one named function**, which turned out to matter: three adapters
share near-identical lines, so a whole-file anchor matches several places, and a runner that
silently skips an ambiguous anchor reports a better score than it earned. That is exactly
what happened while building this — an Amadeus run read 7/10 when the three "failures" were
skips. The runner now prints skips loudly and exits non-zero on any.

The Amadeus profile carries the three fields the scorer has to abstain on with Kiwi:

| What the scorer needs | Kiwi | Amadeus |
| --- | --- | --- |
| UTC offset on a timestamp | no | no — the zone join is load-bearing for both |
| Operating carrier | no — 11 of 13 sample segments were codeshares | yes, and its **absence means the marketing carrier flies it** |
| Equipment code | no | yes — a *type*, which is not a cabin |
| Fare brand + included bags | no | yes |
| Bag fee schedule | no | no — joined from `enrichment/fares.json` |
| On-time record | no | no — a separate feed entirely |
| Self-transfer / virtual interlining | **yes** | **no** |

That last row is why both adapters are kept. Amadeus is GDS content on one ticket, so the
two-ticket itineraries the risk and baggage terms were built to price simply do not appear
in it. That is a coverage loss, not a simplification.

### An equipment code is not a cabin

This is the part it would be easy to get wrong. Amadeus returns `aircraft.code`, and the
temptation is to treat that as having solved hard rule 2. It has not: one carrier's 789 can
be two or three configurations with two or three connectivity systems, and the frame is
swapped after booking anyway. So the code is joined against `enrichment/fleets.json` and
becomes a **hedged claim with an observed frequency** — which is exactly what the scorer's
cabin-uncertainty term is built to price — and a type with no curated row **abstains**
rather than being talked up from the type alone. Better data is what hard rule 2 is *for*,
not a reason to relax it.

`fleets.json` and `fares.json` are both marked `_review: DRAFT` and every fleet row carries
`needs_primary_source: true`. `coverage()` counts them and the page colours the strip amber,
because a claim from an unreviewed table is **more** dangerous than an abstain: an abstain
announces itself in the ledger, a draft row prints a confident dollar figure. These two
files need a human pass before any number built on them is shown to a stranger.

### Unknown is never cheap, in the bag table too

A fare brand with no curated row falls back to `fares._default`, set deliberately at the
**pessimistic** end of transatlantic fees. Giving an undocumented fare the benefit of the
doubt would let the least-documented itinerary accumulate the fewest penalties and win,
which is the failure the whole abstain discipline exists to prevent. The fallback is
flagged per ticket and counted by `coverage()`.

### The traveller's bag load comes from the request

`from_amadeus(..., checked_bags=n)` takes the load from the caller, never from the fare.
Reading it off the fare makes every basic-economy ticket score as though the passenger
travelled hand-baggage-only — which is precisely the comparison this product exists to make.

## OAuth2, and what it changes about the credential

Amadeus does not take a static header key. It takes a client id and a client secret,
exchanged at `/v1/security/oauth2/token` for a bearer token good for about half an hour.
Four consequences, each with a comment in `live.py`:

- **The token is minted before quota is reserved.** A token mint is not the metered call, so
  a mistyped secret — the likeliest day-one mistake — must cost nothing. Reserving first
  would burn a search on every attempt.
- **The token is cached, and the cache is fingerprinted.** A SHA-256 of the credential pair
  is stored beside the token so a rotated key invalidates it. The fingerprint is one-way, so
  `token.json` never becomes a second place a secret lives.
- **A 401 or 403 on the search drops the cached token**, because it may be a retired token
  rather than a bad credential, and the next call should re-mint rather than replay.
- **The secret and the bearer token are redacted** everywhere the key already was, including
  out of the provider's own error echo. `status()` reports whether a token is held and how
  long it has left, never the token.

Credentials come from `CONCORDEGO_FLIGHT_KEY` / `CONCORDEGO_FLIGHT_SECRET` first, so neither
half need ever touch disk. `live.py provider amadeus|kiwi-tequila` switches profiles, keeps
any key already set, and drops a token minted for the old provider.

Note that the sandbox (`test.api.amadeus.com`) and production (`api.amadeus.com`) take the
same credential *shape* but not the same credentials, and the sandbox serves a limited,
cached slice of content. The profile ships pointed at the sandbox.

## Both feeds, proven end to end without a real key

A stand-in Amadeus on localhost — validating the grant, requiring the bearer header, and
403ing a bare `Python-urllib` User-Agent the way real provider edges do — walked the whole
OAuth2 chain:

```
probe    -> minted a token, ran a REAL search, 7 itineraries, 2 calls left of 3
call 1   -> api,   day allowance 3 -> 1
call 2   -> cache, allowance unchanged, age 0s
scored   -> AA6175 $918 A+ / DL1 $950 A+ / BA112 $972 A+
            "ground access, carrier quality and fare brands are curated;
             the aircraft claims are DRAFT and not yet reviewed"
3 spent  -> fourth refused by name: "daily limit reached: 3 of 3 calls used today"
bad secret -> HTTP 401 minting a token, and the allowance did not move
```

The User-Agent guard is proven by that run: the stand-in refuses `Python-urllib` outright,
and the real call got through.

## A third transport: POST with a JSON body

Kiwi and Amadeus search over a query string. Duffel POSTs a JSON body, which is a third
shape after the static header key and OAuth2. The split stays where it was: the **method and
the body's structure** are protocol and live in code; the **field names inside it** stay in
the profile as a `body_template`, so a renamed key is still fixed in config rather than in
Python. A list holding exactly `["$passengers"]` expands to one passenger object per adult —
enough for a search body without inventing a template language.

Duffel also needs a `Duffel-Version: v2` header, which the profile carries as
`extra_headers`, and `return_offers=true` as a static query switch. A stand-in that rejects
a request missing any of those proved all three actually go on the wire.

## Proven end to end without a real key, twice

A stand-in Duffel on localhost — requiring the bearer token, the version header, a
well-formed POST body, and a non-default User-Agent, with each guard verified to actually
reject — walked the chain:

```
probe    -> 6 offers, 2 calls left of 3
call 1/2 -> api, then cache with the allowance untouched
scored   -> AA6175 $918 A+ · BA112 Basic $972 A+ · BA112 Plus $979 A+ · KL642 $1,119 A-
            "+ 3 of 5 options carry an airline-quoted checked-bag price,
               so their bag arithmetic is not an estimate"
3 spent  -> fourth refused by name
bad token-> HTTP 401, counted (the provider answered), token redacted from the error
provider -> saw exactly 3 searches and 1 denial; the cache hit never reached it
```

The same was done for the Amadeus OAuth2 chain, recorded above.

## What is still not done

There is no live key in this repo and none can be added from here. The plumbing is complete
and tested for all three providers; what remains is a credential in the config and one
`live.py probe` to confirm the parameter names — the one thing that genuinely could not be
checked offline.

Duffel is the one to try first: `app.duffel.com/join`, then
`export CONCORDEGO_FLIGHT_KEY=duffel_test_...` and `live.py probe`. Expect Duffel Airways
and invented prices — that is test mode working, not the adapter failing.

Two files also want a human before they are trusted: `enrichment/fleets.json` and
`enrichment/fares.json` are drafts, marked as such in the files, surfaced by `coverage()`
and coloured amber in the interface. The mechanism is right; the numbers need a source.

`adapter_samples/amadeus-jfk-lhr.json` is **synthesised from the published schema, not
captured** — its own `_provenance` block says so, and a test asserts it. It proves the
adapter handles the documented shape; it cannot prove the documented shape is what the
service returns. Replace it with a real capture on the first successful probe.
