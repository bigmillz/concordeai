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
