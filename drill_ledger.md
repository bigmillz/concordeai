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

## cycle 10 — 2026-09-12T02:20Z — app 1b79a1a
batch: seed 20260903, 22 records (6 chat / 10 web / 6 funnel).
scores: chat 4.67 (HOLDS, 4th straight) · web 4.22 · funnel 3.97
(sighted). All 6 sub-4 records adversarially re-checked: 6/6 upheld.
verdict on cycle 9's edits: the user-message clock HELD — 10/10 web
records clock-correct, incl. "dinner friday"=tonight and an open-now
verdict citing 1:20 PM exactly. Any-engine stage retry: no empty
stages this batch.
landed (3):
- _VENUE_RX += shops/stores/joints/venues -> "coffee shops near
  williamsburg" left _loc="shops williamsburg", the home bias missed,
  Nominatim answered VIRGINIA (web[4] 3.4, recheck upheld). Generic
  container nouns now strip like venue words.
- _LISTINGS_RX + what's-on strictness branch -> movies question
  punted with ZERO titles and a homework list despite sources_n=1
  (web[9] 2.2). Listings queries must commit to known current
  releases; referral-instead-of-answer banned.
- FUNNEL_SUMMARY_SYS three hard rules -> trip verdict invented "The
  Clam House" B&B + a nonexistent Calais-Lubec ferry, violating both
  stated reqs (funnel[4] 2.0); laptop verdict shipped a discontinued
  2022 config AND overrode the clicked "14-inch" pick with a 15.6"
  machine (funnel[5] 3.4). Verdicts: REAL only, picks binding, stated
  reqs bind.
also this cycle (trial-run, committed 1b79a1a): build-only page ETag
let WKWebView 304 a weeks-old cached page while APP_BUILD held still
by rule — ETag now carries source mtime, with a gauntlet tripwire.
Stroked AI cap-height compensated (.865em/.06em, CSS + AppKit).
next (ranked): 1) chat truncation — longest gen (135s) cut mid-
sentence (chat[2] 3.8, recheck upheld): find the output-token cap on
the long path. 2) weekend forecast horizon — Friday "this weekend"
fetched Sat only, plus "this feed" register meta (web[6] 3.8).
3) stage-time reconciliation — off-axis typed answers ("with
sprinkles" for portion) sail through; only the verdict recovers.
4) local-Gemma stage echo — dinner stages 3+4 re-asked stages 2+1.
5) home hedge — answers hedge "if you're in NYC" while sibling
records use 11221.

## cycle 11 — 2026-09-12T06:4xZ — app f43187e
batch: seed 20260911, 22 records (6 chat / 10 web / 6 funnel),
collected Fri ~10:22 PM ET. scores: chat 4.33 (DIP — new classes) ·
web 4.06 · funnel 4.27 (sighted). 4/4 rechecks upheld.
verdict on cycle 10's edits — ALL THREE HELD:
- williamsburg -> BROOKLYN (4.8, "per OpenStreetMap", minutes-left
  computed at 10:30 PM). venue-noun fix confirmed.
- movies named four real dated titles w/ verdicts (4.4); Fandango as
  coda, not referral. listings fix confirmed.
- funnel verdicts real in 3/4 trials; the clicked "16-inch" STAYED a
  16-inch (prior override pattern did not recur).
new sinks (confirmed): weather fabrication — wttr.in died silently,
answer invented "71°F and sunny right now" at 10:30 PM, sources_n=0
(web[6] 2.0); no-data fallback opens with capability meta and 200
words of how-to-check (L train, web[5] 3.2); funnel constraint-fit
fabrication — told strawberry+frozen+"with sprinkles", verdict gave
Nerds Gummy Clusters sprinkles they don't have (funnel[2] 2.8).
chat's dip = persona HEDGE-GUESSING ("a writer or an engineer", "if
you live in a dense area like Bushwick" — profile facts as guesses),
explainer drift (H2s + flow blocks on a whistling lesson), hedge-tic
on stable facts ("Python 3.14 (~check current)").
landed (3, tagged 6b266):
- weather_snippets gets the two-rung ladder (wttr.in -> open-meteo
  via home-biased _geocode) + weekday-labeled forecast lines; and an
  is_weather strictness branch when BOTH rungs fail: no invented
  numbers, one clause + seasonal pattern only.
- universal ban in the searched-answer block: never describe your own
  data access; one clause of where-to-check, then real knowledge.
- FUNNEL_SUMMARY_SYS rule 4, "No fake fits": impossible constraint
  sets get the closest REAL option with the miss named.
next (ranked): 1) chat persona rule — a profile fact is known (state
once, plainly, when it changes advice) or omitted; never hedge-guess
a persona. 2) explainer-drift cap: no tables/flow blocks on simple
how-tos. 3) stage-time reconciliation of off-axis typed answers
(three cycles running). 4) route funnel stage generation off local
Gemma (it wrote the off-taxonomy + redundant stages; the all-kimi
record was flawless). 5) hedge-tic scoping to prices/hours only.

## cycle 12 — 2026-09-12T07:5xZ — app 16b8062
batch: seed 20260912, 22 records, collected Sat ~1:50-2:40 AM ET (a
2 AM batch — the harshest open-now clock yet). scores: chat 4.70
(back above bar) · web 4.68 (FIRST BATCH ABOVE 4.5 — needs one more
to close) · funnel 4.13. 2/2 rechecks upheld.
verdict on cycle 11's edits:
- persona hedge-guessing and the (~check current) tic: GONE from
  chat entirely. explainer drift milder (no flow blocks; H2s twice).
- 2 AM clock PERFECT in all ten web records ("barely past 2am...
  supermarkets are essentially never open"; "closed at midnight,
  reopen at noon today"; L train "at 2:19 on a Saturday morning").
- meta ban HALF-held: canonical forms gone, softened forms slipped
  ("I can't confirm live status", "nothing here verifies").
- no-fake-fits held for PRODUCTS but not LOGISTICS: verdict invented
  an NJ Transit route to Jim Thorpe, PA (funnel[0] 2.8, upheld).
- pick override recurred SUBTLER: clicked "Handmade pasta" over
  "Gnocchi/Dumplings", verdict served Ricotta Gnudi — a dumpling
  (funnel[4] 3.4, upheld). local-Gemma stages named the weak link a
  third straight cycle (malformed "High-engagement engagement",
  reworded re-asks; the all-cloud runs were 5.0 twice running).
landed (3, tagged 6b267):
- _stage_ok() quality gate on EVERY generated stage (cloud or
  local): rejects <2 options, doubled-word labels, and questions
  sharing >=60% content words with one already asked; a failed gate
  walks the cloud ladder once more (the 6b263 retry, widened).
- FUNNEL_SUMMARY_SYS: picks binding LITERALLY (an instance of the
  clicked option, not an adjacent category — gnudi is not handmade
  pasta); FEASIBILITY IS AN ATTRIBUTE (never assert an unvouchable
  route/service; name the leg to check).
- web: meta ban extended to softened forms; negative open-now
  verdicts must still commit (ONE named likely-open place or the
  earliest reopen; "best X" still gets a best + runner-up).
next (ranked): 1) chat structure cap — H2s/tables only when a
document is asked for; bold labels + lists otherwise. 2) "One fork"
prompt-vocabulary leak — the memory rules say "state the fork";
model echoes it verbatim; reword the instruction. 3) derived-number
consistency pass (protein 130-160 vs own 0.7-1 g/lb rule). 4)
stage-time reconciliation of off-axis typed answers (4th cycle —
funnel[2] showed the verdict-time repair pattern to standardize).
5) weather ladder still unexercised by a seed — needs a weather
draw to verify live.

## cycle 13 — 2026-09-14 — app f1aa923 (collected Sat 3:40-4:35 AM ET)
batch: seed 20260913, 22 records. scores: chat 4.77 (holds) · web
4.28 (did NOT close) · funnel 4.00. Rechecks: NONE completed — the
verify agents hit the session limit; the sub-4 records below stand
unverified (weighted accordingly).
verdict on cycle 12's edits:
- stage gate: no malformed labels in the batch (held) — but SEMANTIC
  mismatch slips through: option sets that don't answer their own
  question, 3x (dinner "how heavy" -> Vegetable-Forward/Cheese-Centric).
- literal picks: FAILED by reclassification — clicked "Conservative"
  got an all-equity index fund "conservative-leaning" (funnel[1]
  3.4); "Under $50" monthly answered with one-time adoption fees
  (funnel[5] 3.6).
- feasibility-as-attribute: FAILED again — "NJ Transit bus or
  regional rail" to Cape May; no rail serves Cape May (funnel[4] 3.0).
- meta ban (softened forms): HELD everywhere. negative-verdict commit:
  HELD in all three all-closed records (Mr. Kiwi named; reopen times).
web's two dragons (both 2.0-class, unverified): movies shipped
snippet DESCRIPTORS as titles ("The Zombie Sequel", "Al Pacino True
Crime Project") plus a listings punt (web[4] 1.8); weather said "87°F
and sunny right now" at 3:30 AM — current above the day's own high,
"sunny" pre-dawn — with the honesty branch never reached (a rung
returned data; likely stale daytime cache) (web[8] 2.0).
chat: persona-guessing and the (~check current) tic stay gone; H2/flow
scaffolding recurs 2/6; a new micro-class — confident false absolutes
used to simplify ("a will is all-or-nothing at death").
landed this cycle: none from the drill — the day went to Patrick's
RC4 feedback list (6b268/269: recipe, clean-now, prune, wizard box,
greetings, chat search, centered lockup).
next (ranked, cycle 14): 1) weather SANITY: gate sky words on
is_day, reject a "current" above the day's high, treat contradiction
as re-fetch, and log weather as a source (sources_n=0 hid it). 2)
listings ENTITY commitment: an item may only be reported under a
proper name — descriptors are not titles; re-fetch or commit to the
one verifiable film. 3) funnel picks by MEANING: a clicked option
binds its category semantics (Conservative != all-equity; a monthly
budget != a one-time fee) and transit claims need a named, real
line. 4) chat structure cap (H2/tables/flow only for documents).
5) "simplify by omission, never by a false absolute."

## cycle 14 — 2026-09-15 — app d713bd1 (collected Mon ~1:35-2:28 AM ET)
batch: seed 20260914, 22 records. scores: chat 3.80 (DIP) · web 4.20 ·
funnel 3.07 (DIP). Rechecks 5/6 upheld; web[9] disputed on a time
premise the verifier took from the run-dir stamp — drill.py's stamps
are NOT wall clock (a launch at 1:35 AM ET stamped 16:58Z the day
before); `date` at collection end read Monday 2:29 AM ET, so the
2 AM premise stands. (Fix the stamp: it misleads verifiers.)
confounds on the chat dip: prefs.json was rewritten at 1:57 AM,
INSIDE the batch window; the judge model changed with Patrick's
/model switch (5 -> 5.1) between cycles 13 and 14. Same engine
(Gemma 4 26B, tier Fast) and same memory file in both cycles. The
objective finding survives either way: tables in 5/6 and H2s in 4/6
simple answers, 330-480 words each; persona hedge-guessing back 2/6.
verdict on cycle 13's edits:
- weather sanity: HELD (current 72°F < the day's 77°F high; "patchy
  rain nearby", no "sunny"; sources_n=1 — the feed is a source now).
  NEW: "this weekend" at 2 AM Monday resolved to the weekend just
  ENDED (Sunday's past numbers + Mon/Tue); feels-like unchecked.
- names-only listings: not drawn this seed.
- picks in meaning: FAILED 3/6 — '4+ hours' answered with a 1.5-h
  Bordentown; 'Social companion' -> solitary Syrian hamsters, with
  'a pair' contradicted by 'two separate habitats'; a shelf KitKat
  sold as 'frozen-style' with invented sprinkles (funnel[0] 2.0,
  [2] 2.0, [3] 2.6 — all upheld).
- named transit: FAILED — 'NJ Transit Trenton Line' does not exist.
- all-closed -> option/reopen: HELD 4/4; best-X ranked: HELD.
- data-scope meta is BACK in 4/5 local records in impersonal form
  ('every coffee spot on the list', 'names per Yelp's list', 'none
  of the four pharmacies listed') and universal negatives ('Nothing's
  open right now') asserted from 1-4-item lists that omit the late-
  night category (24h pharmacy chains, diners, bodegas, Dunkin').
landed AFTER this batch (cycle 15, tagged 6b271): the shape law on
the default length rung; remembered facts plainly or not at all; a
second-call VERDICT AUDIT on every funnel (rules failed three cycles
running — structure now). On trial in cycle 15.
next (ranked, cycle 16): 1) "this weekend" resolves to the NEXT
Sat/Sun on Mon-Fri (compute the dates in code; label the forecast
lines "this Saturday"); feels-like bounded. 2) universal-negative
guard: an all-closed verdict from a thin list must name the late-
night category fallback (24h pharmacy chain / diner / bodega) and
never say "nothing's open". 3) strip impersonal scope meta ('the
list', 'per Yelp's list' as a sentence) — cite inline only. 4) fix
drill.py's run stamp to wall-clock ISO.
