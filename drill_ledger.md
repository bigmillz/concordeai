# Drill ledger — what the loop tried and what actually moved

## cycle 0 — 2026-08-24T16:00Z — baseline era (pre-ledger, from session)
Fixed before the loop formally started, each verified live:
- cloud.json corrupt (1 stray byte) — ALL cloud providers silently dead;
  repaired, backup kept. Likely the single biggest quality factor.
- funnel echoed picks back (`"\n".join(picks)` fallback + wrong system
  prompt) -> verdict voice, MERGE_RANK ladder, honest no-model text.
- funnel re-asked the same question (no memory of asked questions) ->
  `asked` list both ends; 4/4 distinct questions on same-seed replay.
- web voice hedged by instruction (sources-only + delete-real-names) ->
  volatile facts source-bound, stable knowledge free, non-answer = worst.
- supermarkets unreachable (amenity= only) -> shop= union in Overpass.
- venue lookup ate a 60-word conversational message and invented a venue
  name ("...Sitting Down in Som") -> 14-word classifier gate, 6-term
  entity guard, word-boundary cap. Messy class added to the bank.
next (ranked):
1. LOCALITY BUG: "myrtle-broadway" answered with Myrtle Beach SC — the
   search planner never receives the user's locality. Find where locq /
   owner locality could feed _plan_queries and the research path.
2. Baseline all four modes WITH cloud restored (everything above was
   measured local-only or partially).
3. Judge pass on funnel stage-question quality (options concrete?
   typed answers steering?) now that repetition is fixed.

## cycle 1 — 2026-08-25T02:40Z — first scored batch (cloud ON)
batch: seed 20260825, 22 records. scores: chat 3.97 · web 4.00 ·
messy 4.85 · funnel 3.37. meter 44%.
landed (6): funnel stages = one axis per stage, options never a final
answer; summary names beyond the picks + quotes only user's words +
flags skipped/conflicting axes; chat memory-fact gate (only when it
changes advice); engagement-bait closers banned (fork stated up front
instead); linear flow-diagrams banned; from-memory freshness flags.
verified: worst funnel (trip) replays clean — verdict names a thing
not in any option, honest tie-backs.
reverted: none. gauntlet 119/120 — the 1 fail ("every tier resolves",
Cloud Only=[]) was ALL FOUR providers quota-resting at once from batch
pressure, recovered minutes later; re-run pending. NOT an edit
regression.
next (ranked):
1. QUOTA RESILIENCE — the grind can rest every provider at once; then
   drills score the LOCAL fallback and real users get Corolla answers
   mid-conversation. Space cycles 60-90min; consider surfacing "cloud
   resting — answers may be simpler" in-app; note Patrick's API tier
   is the hard lever.
2. LOCALITY into search planning (myrtle-broadway ≠ Myrtle Beach SC).
3. Re-judge funnels + chat on fresh seed (did 6 edits move scores?).

## RC3 cut — 2026-08-25T17:45Z — v260 (per Patrick)
Carries cycles 0-1: cloud repair, funnel verdict/axis fixes, chat
voice fixes, venue-eater fix, messy bank. Gauntlet 120/120 at cut.
Cycle 2 next: fresh-seed re-judge (did the 6 edits move funnel/chat?),
then locality.

## cycle 2 — 2026-08-25T18:55Z — fresh seed 20260826, RC3 tree
scores: chat 4.37 (was 3.97 ✓) · web 3.57 (was 4.00, harder seed:
movies punt 2.6, weekend-forecast gap 3.4) · messy 4.35 · funnel 3.23
(was 3.37 — flat). CONFIRMED WINS from cycle 1: zero engagement-bait
closers (was 5/6), funnel verdicts all specific with zero pick-echoes
(was 4/6 echoes). ROOT CAUSE of flat funnels found: all six ran while
Groq (the single "active" provider) was quota-resting → every stage
fell to local 4-bit silently. funnel_stage/summary used cloud_conf()
alone, never the ladder.
landed (6): funnel stages + summary walk compositor_ladder (one
resting provider = one rung, not the whole funnel); reworded-axis rule
(texture asked once is texture asked); summary can't shrink stated
budgets or launder inventions as "since you want" (suggestion voice +
no invented logistics — a demolished hotel got recommended as
bookable); chat volatile-number age flags hardened (never "the current
model" from memory); one-structure-per-comparison rule; never OPEN
with a profile fact. Instrumentation: funnel stages now record which
engine served them (judges kept inferring from timing).
next (ranked):
1. Re-drill funnels with ladder routing live — expect the cycle-1
   prompt fixes to finally bite through a strong engine.
2. Web mode: "this weekend" forecast gap (answers today when asked
   about Saturday) + movies punt (zero titles from a web-mode lookup —
   query planner never searched showtimes).
3. Locality into search planning (standing).

## cycle 4 — 2026-08-26T01:50Z — the ladder's first scored test
scores: chat 4.83 (3.97→4.37→3.90→4.83 — TWO perfect 5s, zero
closers, zero mojibake; essentially at the bar) · funnel 3.73 (ladder
routing +0.5, first movement in three cycles) · messy 3.90 · web 2.47
(the crisis mode — now ONE class: live-status questions. Movies punt,
pharmacy no-verdict, weekend forecast answering Tue-Thu, and an L-train
question answered from INDIAN RAILWAYS).
landed: 3 chat polish rules (per-figure age flags, no diagram after a
prose walkthrough of the same steps, no product-family absolutes).
Plus the web dig hit gold — two ANCIENT bugs, not regressions:
- osm_places queried node[...] only: chain pharmacies/supermarkets/
  restaurants are mapped as building WAYS, so open-now was blind to
  exactly the venues people ask about. nwr + out center; supermarket
  bushwick 0→8 venues (two 24/7), coffee williamsburg 0→8.
- _OSM_KINDS prefix stubs (\bpharmac\b, \bpastr\b, \bspeakeas\b,
  \bbrewer\b) can NEVER match their words — trailing \b after a stub.
  Pharmacy/brewery/speakeasy/pastry lookups have never once had OSM
  data. Fixed with \w*; pharmacy bushwick 0→8 venues incl. CVS
  Mo-Su 08:00-22:00, Duane Reade to 23:00.
- Overpass empty results no longer cached (30-min empty-poisoning).
next (ranked):
1. Judge cycle 5 with OSM actually feeding open-now — expect web to
   finally move.
2. Transit-status query planning ("is the L train running" must search
   MTA status, never generic) + weekend-forecast honesty (verdict
   sentence when the feed doesn't reach the weekend).
3. Movies/showtimes: needs a listings-shaped search plan.
4. HDR skies wiring (map ready, 89/89 clips).

## cycle 5 — 2026-08-26T03:45Z — the surge
scores: chat 4.73 (4.83 then 4.73 — TWO consecutive ≥4.5: CHAT MEETS
THE STOP CONDITION, drops to maintenance cadence) · messy 4.55 (back
at the bar, needs one hold) · funnel 4.33 (3.23→3.73→4.33 — ladder +
axis rules compounding) · web 3.40 (2.47→3.40).
THE MADDENING ONE: coffee-near-williamsburg finally produced the
perfect answer shape — verdict, OSM-credited hours — around VIRGINIA
venues. Geocoder is fixed (OSM data was Brooklyn); the WEB SNIPPETS
carry same-named wrong-city venues and the model blended them.
landed (3): VENUE DATA authority rule (when the OSM block exists,
venue names come ONLY from it — snippets are colour, never names);
staleness flags must be world-facing ("~$20 — check current"), the
meta "as of my last data" is banned (record 6 proved the model knows
the clean form); say-each-thing-once (no Quick-version recap blocks).
next (ranked):
1. Judge cycle 6 — expect web to jump with venue-name authority.
2. Weekend-forecast reach (feed stops Thursday; verdict-first honesty
   or extended fetch) + movies/showtimes search plan.
3. Messy hold + funnel hold — both one clean batch from done.
4. HDR skies wiring (still queued).
