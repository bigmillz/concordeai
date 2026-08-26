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

## cycle 6 — 2026-08-26T05:10Z — messy done, web climbing, funnels regress under severity
scores: chat 4.87 (third straight ≥4.5 — maintenance confirmed; the
world-facing flags and no-recap rules verifiably landed) · messy 4.75
(4.55 then 4.75 — TWO consecutive: MESSY MEETS THE STOP CONDITION) ·
web 3.80 (2.47→3.40→3.80; pharmacy scored 4.6 with the model shape) ·
funnel 3.97 (full-severity hold check FAILED: the STAGE GENERATOR
itself invents persona facts — "your $1,500 budget", "your studio
apartment" for bare questions — both kimi and Gemma, so the shared
prompt; my earlier user-words-only rule policed only the summary).
landed (3): FUNNEL_SYS never-presuppose rule (stages ASK, never
assume); banned-meta broadened ("the data"/"in the data"/"what I
turned up"/"I can't pull" — cost pharmacy 2 register points); OSM
endpoint LADDER (overpass-api.de throttled under drill pressure and
returned zero venues at a corner with nine pizzerias — kumi mirror is
rung two; verified: pizza @ myrtle-broadway 0→8 venues, OMG Pizza
24/7 open now).
next (ranked):
1. Cycle 7: funnel re-hold under severity + web with OSM ladder.
2. Movies/showtimes search plan (only remaining web sink class).
3. Chat maintenance watch: one-directional size drift + identical
   verdict/bullets/quip skeleton (judge flagged sameness — do NOT
   over-tune a done mode; revisit only if maintenance batch dips).
4. HDR skies wiring.

## cycle 7 — 2026-08-26T06:15Z — the funnel was framed
scores: chat 4.97 (maintenance pass — judge: "do not churn") · messy
4.80 (third straight) · web 3.80 (flat: movies 2.0 and coffee 2.2
remain; pharmacy verdict-shaped at 3.8) · funnel 3.63 worst 1.6.
THE FINDING: the funnel judge suspected "the collector's hidden
persona leaking" — inverted but right. The drill sends REQS
("studio apartment, active", "vegetarian, small kitchen", "under
$400", "video editing under $1500", "im 28, 10 years") that were
NEVER LOGGED in records. Every "fabricated user fact" docked across
cycles 2, 6 and 7 was a stated requirement the funnel HONORED. Funnel
scores have been systematically understated for three cycles; my
cycle-6 no-presuppose edit fought a phantom (harmless, kept).
REAL funnel faults confirmed: local-Gemma stages emit garbage when
the ladder exhausts — the literal "Which direction?" with an EMPTY
options array, and a 1.6 "weekend trip" verdict landing in Bay Ridge.
landed (2): reqs_given + typed_given now logged in every funnel
record (judges see the full contract); empty-options local stage
earns exactly one retry up the ladder before defeat.
web notes for cycle 8 edits: OSM authority must not read as
exhaustive (an area-wide "tonight's a wash" from eight rows) —
one-clause call-ahead hedge on area negatives; credit OSM ONCE, not
per line; movies/showtimes search plan still the top web sink.
next: cycle 8 with sighted funnel judges — expect funnel's true
level to surface; then movies plan; then HDR skies.

## cycle 8 — 2026-08-26T08:30Z — sighted judges, and the Beijing clock
scores (funnel judged WITH the reqs contract): funnel 4.27 (was 3.63
blind — the exoneration confirmed; zero fabrication dockings, worst
3.0) · chat 4.93 · messy 4.75 (both holding done) · web 3.13.
THE SMOKING GUN: web answers assert the WRONG WEEKDAY. Runs stamped
Tuesday ~6pm EDT answered "it's Wednesday"; the coffee record's
"noon-8pm... kicks in later" is only possible on a weekday MORNING —
consistent with UTC+8. That is KIMI'S BEIJING CLOCK: the date
injection carried only a date, so a Beijing-hosted model filled in
its own "today" and overrode the pipeline's correct data. Every
open-now verdict computed from the wrong day.
landed (3): the clock injection now binds weekday + local time
("RIGHT NOW for the user it is Tuesday, August 25, 6:14PM... never
assert a different weekday, even if your own clock disagrees");
sources credited ONCE at first use; area-wide negatives must scope to
the listed venues + one call-ahead clause.
ops note CORRECTED in cycle 9: the "Drive served drill.py stale"
theory was WRONG — drill_funnel has three return paths and the
reqs_given patch had landed on the never-finished ERROR path only, so
successful funnels never carried it. Patched the success path; the
lesson is about multi-exit functions and lazy anchors, not Drive.
next: cycle 9 — web with a correct clock is the last big lever;
movies plan still queued; funnel needs 4.5×2 sighted.

## cycle 9 — 2026-08-26T09:30Z — the clock instruction lost; the clock moves
scores: chat 4.90 · messy 4.75 (both holding done) · funnel 3.90
(sighted; the empty-options "Which direction?" came from GEMINI-FLASH
this time — my retry only fired on local engines; kimi stages
uniformly fine) · web 3.40 — PROBE FAILED: the system-prompt clock
binding lost to Kimi's Beijing calendar AGAIN ("Wednesday-morning
rush hour" at Tue 7:47pm; a pizza verdict INVERTED — "nothing's open
this early" for a noon-midnight shop at 7:48pm). Also: Yelp marks
Norbert's Pizza closed since early 2026 while its own site (our
## cycle 9 — 2026-08-26T09:35Z — the clock instruction lost; the clock moves into the question
scores: chat 4.90 · messy 4.75 (both holding done) · funnel 3.90
sighted (empty "Which direction?" stage came from GEMINI-FLASH this
time — the retry only fired on local engines; now fires on any) ·
web 3.40 with the probe verdict: THE SYSTEM-PROMPT CLOCK BINDING
FAILED. "Wednesday morning rush hour" at Tuesday 7:47pm; a pizza
verdict INVERTED by the wrong clock ("nothing's open this early" for
a noon-midnight shop at 7:48pm — it was OPEN). Kimi's own calendar
beat our instruction twice running.
landed (2): the moment now rides the USER MESSAGE in web mode
("[for time-sensitive parts: it is Tuesday 7:47PM where I am]") —
the one place no model ignores; empty-options stage retry fires for
ANY engine. Also caught by a judge doing real verification: Norbert's
Pizza is marked closed on Yelp since early 2026 while its own site
says open — stale-single-source risk, noted for the venue-data
trust rules if it recurs.
correction: cycle 7's "Drive served drill.py stale" theory was wrong
— the reqs_given patch had landed on drill_funnel's error path only
(three return exits, lazy anchor). Success path patched; cycle 10
records will carry the contract.
next: cycle 10 = user-message clock on trial. Funnel needs 4.5×2
sighted. Movies plan still queued.
