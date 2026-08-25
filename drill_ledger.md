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
