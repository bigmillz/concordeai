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
