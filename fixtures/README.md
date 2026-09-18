# Fixtures

Build step one. The scorer does not exist yet; these files define what it will be
held to.

```bash
python3 fixtures/validate.py        # structural + semantic checks over every fixture
```

## The shape

One file is one **scenario**: a traveller, a query, N competing itineraries, and the
assertions the scorer must satisfy. Not one itinerary per file — because the whole
product is a comparison, and because the same option set has to be scoreable under
several weightings (cheapest / fastest / comfort are the same model at different
weights, hard rule 3).

`schema.json` is the contract. `validate.py` enforces it plus the things a JSON Schema
cannot say.

## Six decisions worth not relitigating

**A fixture carries inputs only.** Nothing the scorer computes may appear: no
`effective_cost`, no elapsed minutes, no layover duration, no totals, no grade. A
fixture holding its own answer tests nothing. `validate.py` fails on a list of
forbidden field names for this reason.

**Time is local wall clock plus an explicit numeric UTC offset**, one RFC3339 string
per endpoint: `2026-03-12T06:10:00-04:00`. Not UTC alone — layover quality and ground
access both need the local clock, and recovering it would need a timezone database at
scoring time, which is I/O and versioned external state, so a tzdata update could
silently move a golden number. Not local-plus-zone-name either, and this is what
settles it: **01:30 on 2026-11-01 at JFK happens twice**, so a zone name cannot say
which instant a westbound arrival landed at. The IANA name is carried as
non-authoritative prose metadata; the offset is the truth.

**Money is integer minor units.** Cents, everywhere, never a float. "Same data = same
order, every time" does not survive floating-point accumulation — two itineraries that
should tie exactly would differ in the last bits and sort by rounding noise instead of
by the documented tie-break.

**Unknown is explicit and is not zero.** Every enrichment term can carry an `abstain`
object with a reason and a fallback policy instead of a value. Without it the
least-documented itinerary accumulates the fewest penalties and wins — the ranking
would reward missing data. This matters immediately: BTS covers US reporting carriers,
so BA, VS, LH, AF, KL, IB, AY and SK on JFK/EWR–Europe have no free per-flight-number
record, and neither does any intra-Europe leg.

**A claim is never a fact.** Hard rule 2 is structural, not a prompt instruction: any
statement about equipment, cabin, connectivity or punctuality is a `hedged_claim` with
`observed_frequency`, `sample_size`, `observation_window`, `source` and `as_of`. The
scorer may only turn that into an expected value; the narrator may only render it
hedged. There is no field in which to put "has wifi".

**Baggage keys off structured entitlements, never off the fare's name.** Two carriers
in the corpus both sell a fare called Basic; one includes a checked bag and one does
not. Fee tiers index from the first piece *beyond* the included allowance, reset per
passenger, and are assessed **per ticketing boundary** — which is why a self-transfer
pays twice.

## The three fixtures

### `01-ground-access-swing.json`
Bushwick → London. A $455 Newark departure at 06:10 against a $612 JFK departure at
19:25. Pins down that ground access resolves from the origin geocode and the *local*
departure hour against a **list** of modes, and that an infeasible mode names the leg
that breaks rather than making a blanket claim.

Dated 2026-03-12 on purpose: the US moved to DST on March 8 and the EU does not until
March 29, so the NYC–London gap is **four** hours here, not five. A scorer assuming a
constant offset gets every elapsed time in the file wrong by 60 minutes.

### `02-cdg-night-layover.json`
New York → Milan three ways, each with a layover within ten minutes of the same length:
CDG at 01:40, LHR at midday, AMS at midday. If the scorer prices them similarly, the
layover model is a duration scalar wearing a costume and the product has no moat.

The AMS layover is expected to score as a **credit** — long enough and civil enough to
go into the city. A model that cannot express "this layover is a feature" is not
modelling layovers.

### `03-bags-flip-the-fare.json`
New York → Madrid four ways. Every headline price is wrong once two bags are counted,
the order almost completely inverts, and the cheapest fare on the page finishes last —
a self-transfer that pays for the same two bags twice, where the intra-Europe leg's
first-bag fee is *higher* than the ocean crossing's.

## What these deliberately do not cover

Round trips (the brief's formula is written one-way and has four ground legs in
reality), parties larger than one (the terms scale differently — tickets and bags per
person, a car not at all, three subway fares yes), award availability (no cash-inventory
API knows whether a saver seat exists), and arrival-side time-of-day value. Each is a
real gap, listed in `docs/design.md` under *Corrections and gaps*.

## Every number here is dated and most are not primary

`as_of` is on every fixture and `effective_date` on every fare component, because three
of these numbers moved inside the corpus's likely lifetime. Where a value came from an
aggregator rather than the operator, it carries `needs_primary_source: true` — the JFK
AirTrain fare and the NJ Transit airport fare both do, and both are load-bearing.
Re-verify those before anyone treats a grade from this corpus as real.
