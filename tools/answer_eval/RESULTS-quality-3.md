# Answer bench: quality-3 against fresh-1 (2026-11-08)

The short version: on this Mac's Fast tier (Gemma 4 26B on MLX, one writer, one pass), all 33 turns, **quality-3
scores 4.06 against 3.91 for fresh-1** (three branch runs against two base runs, paired by turn: **+0.15, standard
error 0.14**; wave 1 alone, two against two in one grading: **+0.23, se 0.14**; wave 2a, run c beside the anchors:
+0.26, se 0.23). That is one standard error, not two: **the overall gain is not clearly beyond noise.** the branch runs agree with each
other (4.18, 4.09) while the two base runs are 0.54 apart (3.64, 4.18), which is the bench's run noise. What moved
on every run is the shape: **concise 5.4 → 6.3, structure 5.4 → 5.9, fit 4.4 → 4.8**, headings on 52 → 4 answers'
worth, tables in 10 → 3 answers, the machinery's words ("the provided data does not list") 6–9 → 0 per run,
**high-severity hallucinations 38 → 30** over 66 answers. First visible text is unchanged (median 20–21 s base,
15–21 s branch) and no run had an error answer. Every number below is **measured**; nothing is estimated.

## What was measured

- **Config**: `run_bench.py --config local-fast --warmup`, port 9898, a windowless dev copy with no cloud keys and
  no `servers.json` (the owner's server is down and was sent nothing), tier Fast = **Gemma 4 26B** (MLX) on every
  turn of every run. All 30 questions, 33 turns. `as_of` stays 2026-10-24 (see the caveat below).
- **Code**: base = `fresh-1` 686e70a (main 672d6f1 + the free fresh-facts fixes), extracted with `git archive`.
  Branch = `quality-3` 2fd493c. Each run held `engines.lock` (owner `quality-3`) for its own length only and
  released it between runs; another helper's runs took the lock in between.
- **Grading**: Opus judge subagents with RUBRIC.md's prompt and no cloud keys, **blind and by turn**
  (`blind_batches.py`): each judge got every run's answer to one turn with the run names replaced by shuffled
  codes, three turns per judge. Wave 1 graded base a/b and branch a/b together (`judgments/<run>/opus-blind-w1.jsonl`).
  Wave 2a graded branch run c with `q3-base-a` and `q3-br-a` graded again as anchors (`opus-blind-w2a.jsonl`);
  the anchors moved +0.09 (se 0.11) and −0.12 (se 0.09) between waves, so the two waves read alike.
  WAVE2B_NOTE
- **Runs** (`runs/<name>/`):

  | run | code | what differs |
  |---|---|---|
  | `q3-base-a`, `-b`, `-c` | 686e70a | fresh-1, three samples |
  | `q3-br-a`, `-b`, `-c` | 2fd493c | all five fixes |
  | `q3-ab-polish` | 2fd493c, `MILLENAI_Q3_OFF=polish` | no post-pass |
  | `q3-ab-shape` | `…=shape,estimate,followup` | no shape, estimate or follow-up rules |
  | `q3-ab-vendor` | `…=vendor` | the old ranking (no vendor mark, listings and threads kept) |

### A caveat on the fresh category

The reference sheets are dated 2026-10-24 and these runs searched the live web on 2026-11-08. The engines now
return the FOMC statement of 28 October (q04), a model OpenAI posted on 26 October (q05) and an ECB rate of
6 November (q17.t2), and the rubric makes the judges mark those **wrong as of the bench date**. Base and branch are
hit alike, so the paired comparison stands, but the fresh column (3.5–4.2) understates both, and q04 in
particular (base 3, 4 against branch 2, 2) is a date artefact, not a regression: the branch answers state the
28 October decision and its 10–2 vote plainly, which is what the shape rules ask for. Refreshing the sheets is
the next bench chore.

### How much to trust a difference

- **Run noise**: the two base runs, same code, graded in the same wave, differ by 0.54 overall (3.64 against
  4.18); the two branch runs by 0.09. One run against one run can't separate anything under about 0.5.
- The paired comparison is therefore the mean of the base runs against the mean of the branch runs, turn by
  turn, with its standard error over the 33 turns.

## Scores

### Wave 1: two base runs against two branch runs (overall, 1–10)

| run | fresh (6) | travel (5) | follow-ups (3) | all (33) | correct | grounded | depth | fit | structure | concise | high flags |
|---|---|---|---|---|---|---|---|---|---|---|---|
| q3-base-a | 3.50 | 3.40 | 2.67 | 3.64 | 4.5 | 5.4 | 3.5 | 4.1 | 5.2 | 5.2 | 18 |
| q3-base-b | 4.17 | 3.60 | 3.67 | 4.18 | 5.0 | 5.5 | 4.2 | 4.7 | 5.6 | 5.5 | 20 |
| **base, mean** | 3.83 | 3.50 | 3.17 | **3.91** | 4.7 | 5.5 | 3.9 | 4.4 | 5.4 | 5.4 | **38** |
| q3-br-a | 3.50 | 4.20 | 3.67 | 4.18 | 5.0 | 5.8 | 4.0 | 4.9 | 5.8 | 6.4 | 14 |
| q3-br-b | 3.50 | 3.80 | 3.67 | 4.09 | 4.8 | 5.7 | 4.2 | 4.7 | 6.0 | 6.2 | 16 |
| **branch, mean** | 3.50 | 4.00 | 3.67 | **4.14** | 4.9 | 5.7 | 4.1 | 4.8 | 5.9 | 6.3 | **30** |

Paired over the 33 turns (mean of two against mean of two):

| category | n | base | branch | change |
|---|---|---|---|---|
| fresh | 6 | 3.83 | 3.50 | −0.33 (q04 is the date artefact above: −1.5 of it) |
| compare | 4 | 3.88 | 4.12 | +0.25 |
| health | 6 | 3.83 | 3.75 | −0.08 |
| travel | 5 | 3.50 | 4.00 | **+0.50** |
| money | 5 | 3.20 | 3.80 | **+0.60** |
| reasoning | 3 | 6.17 | 6.83 | **+0.67** |
| technical | 1 | 6.00 | 6.00 | 0.00 |
| follow-ups | 3 | 3.17 | 3.67 | **+0.50** |
| **all** | 33 | **3.91** | **4.14** | **+0.23 (se 0.14)**; better/worse/same 15/9/9; high flags per answer −0.12 |

Per turn, base a,b / branch a,b: q01 2,3/3,3 · q01.t2 2,2/2,2 · q02 3,4/3,2 · q03 1,3/2,2 · q04 3,4/2,2 ·
q05 4,3/4,4 · q06 8,8/7,8 · q07 3,3/5,4 · q08 6,6/6,5 · q09 2,3/5,2 · q10 3,5/3,3 · q11 5,4/3,6 · q11.t2 2,3/3,4 ·
q12 3,2/4,3 · q13 3,3/2,4 · q14 3,4/3,3 · q15 4,5/3,6 · q16 5,5/5,3 · q17 3,5/5,6 · q17.t2 4,6/6,5 · q18 3,3/3,4 ·
q19 3,2/3,2 · q20 4,6/6,4 · q21 4,2/4,3 · q22 2,2/3,4 · q23 5,3/4,4 · q24 4,5/3,5 · q25 3,3/4,4 · q26 3,2/4,3 ·
q27 3,8/8,7 · q28 9,9/9,8 · q29 3,5/5,4 · q30 5,7/6,6.

### Wave 2a: branch run c beside two anchors (overall, 1–10)

| run | fresh (6) | travel (5) | follow-ups (3) | all (33) | correct | grounded | depth | fit | structure | concise | high flags |
|---|---|---|---|---|---|---|---|---|---|---|---|
| q3-base-a (again) | 3.83 | 3.80 | 2.33 | 3.73 | 4.4 | 5.4 | 3.7 | 4.1 | 5.1 | 5.2 | 17 |
| q3-br-a (again) | 3.17 | 4.40 | 3.67 | 4.06 | 4.8 | 5.7 | 3.9 | 4.8 | 5.9 | 6.3 | 12 |
| q3-br-c | 3.50 | 3.60 | 3.67 | 3.91 | 4.5 | 5.6 | 4.1 | 4.8 | 6.2 | 6.1 | 21 |

- base-a → br-a, br-c (wave 2a alone): **+0.26 (se 0.23)**, better/worse/same 13/11/9.
- br-a → br-c (the same code, run noise): −0.15 (se 0.23); br-c carries 21 high flags against br-a's 12, the
  widest spread between two branch runs, and most of it is q01, q04 and q05 (the date artefact above) and q09.
- **Two base runs (wave 1) → three branch runs (a, b in wave 1, c in wave 2a): +0.15 (se 0.14)**, better/worse/same
  18/11/4, high flags per answer −0.06.

WAVE2B_SECTION

### What the answers look like (counted over the 33 answers of each run)

| | base a | base b | branch a | branch b |
|---|---|---|---|---|
| markdown headings | 54 | 50 | 5 | 2 |
| answers with a table | 11 | 10 | 3 | 3 |
| "the provided data / the sources do not provide / search results" | 6 | 9 | 0 | 0 |
| "as of" | 23 | 40 | 35 | 46 |
| refusals ("cannot give an exact", "check the listings", "you would need to check") | 4 | 1 | 6 | 4 |
| mean length (chars) | 1,752 | 1,739 | 1,242 | 1,290 |
| answers the post-pass changed (a RESET re-send) | – | – | 12 | 7 |

### Timing and errors

| run | first visible text median (mean) | wall time median (mean) | error answers |
|---|---|---|---|
| q3-base-a | 20 s (21) | 29 s (29) | 0 |
| q3-base-b | 21 s (21) | 32 s (33) | 0 |
| q3-br-a | 21 s (21) | 27 s (27) | 0 |
| q3-br-b | 15 s (16) | 21 s (22) | 0 |
| q3-br-c | 16 s (16) | 22 s (22) | 0 |
WAVE2B_TIMING

The post-pass runs after the stream (under a millisecond) and re-sends a changed answer after a RESET, so it
costs no first-text time. The shorter answers are where the wall time went.

## The candidates, one by one

### 1. The writer's own errors: a deterministic post-pass. KEPT (ablation: ABL_POLISH)

- **What**: `answer_polish`, after the single writer's stream. Every `A op B [op C] = D` the answer states is
  re-computed from its own figures and the result replaced when it is off by more than the figures' own rounding
  (an integer under 100 is a count and exact; "583 gallons × $3.40 = $1,983" stands, from 583.33) plus 5% for an
  approximation; a chain is judged first against last and its middle sum's one worked-out figure re-derived; a
  partial equation ("(flight $1,350 + 11 × $95 = $1,045)"), a year, a share, a sign flip and fenced code are left
  alone. Each dollar figure beside a reais figure is checked against the one rate the answer or the evidence's FX
  block states and replaced when more than 6% off. The pipeline's words are reworded in place with the grammar
  kept. A leading heading and a closing recap go.
- **Measured over every recorded bench answer of rounds 0–3** (16 runs × 33): the arithmetic rule fires on 2
  answers (both gpt-oss's real errors on q27), the conversion rule on 2 (q17.t2, both real: $454 for R$2,250 at a
  stated 5.40), never on a right figure; the leak rewrite on 54 answers.
- **On this config**: Gemma's arithmetic was right in all 66 base answers (q27 and q28 are correct to the cent),
  so the arithmetic rule never fired in the branch runs; the leak rewrite fired on 12, 7 and 9 answers (the three branch runs). The
  judges found no change the post-pass made wrong; the "garbled" headlines they flag ("R$ 4,400–$800", "With
  sceanrio 2 tax") are in answers the post-pass never touched (0 RESETs) and are the writer's own.
- The critic's "mandatory corrections" don't apply here: this config has no critic. A different local final
  writer wasn't tried: the mandate fixes Fast = Gemma 4 26B, and no evidence from this round points at the model.

### 2. Source ranking: the vendor's own page, listings and threads. KEPT (ablation: ABL_VENDOR)

- **What**: the maker's or seller's own page on the question is flagged (the host's own label is the name, or
  the name sits in a primary host the hints named), seated ahead of the fill, marked "THE VENDOR'S OWN SITE" in
  the pack, and the dating rule puts it with official data over any reseller's figure. A listing's page (airbnb,
  vrbo, cozycozy, hostelworld) and an engine's paid link are junk; booking and price aggregators, forum threads
  and a site's front page rank down 2; a query's seat goes only to a hit worth half the best; a nameless part
  of the question always carries the lead's name.
- **Measured at the evidence level** (no models; the grounded search on q01–q03 and q17–q21, before and after):
  the junk is gone from the packs (a Da Nang listing, a speed test, an Airbnb unit, a TripAdvisor thread, an
  accent-reduction school, a glass tumbler, Omio's and Kayak's front pages, a bing ad), and the vendor flag lands
  only on the vendor (digitalocean.com; the first cut flagged llm-stats.com for "LLM" and spanish-fluency-club.com
  for "Spanish", which was fixed). Reference key facts found in the pack: 8–9 of 90–102 both before and after —
  the engines decide that, not the ranking.
- **On answers**: q17 (neighbourhoods) 3,5 → 5,6 and q17.t2 4,6 → 6,5; q03 (DigitalOcean) 1,3 → 2,2: the docs
  pricing page is in the pack and marked, but the GPU price page never comes back from the engines, so "fastest"
  stays unanswered. "Claude runs mainly on TPUs" is still in every q01 answer: the TPU-deal blogs are what the
  engines return for "Anthropic Claude GPU TPU".

### 3. The query plan. DROPPED: not a problem on this config, measured

The plan landed on **26 of 28** grounded searches in the base run, in 0.5–1.6 s: with `--warmup` the local engine
is up and idle, so the local planner runs at once. Nothing to fix here; the half-landing was the server's seat.

### 4. Depth and fit: the shape rides the grounded turn. KEPT (ablation: ABL_SHAPE)

- **What**: on this config the single writer never saw the answer brief (it rides the revise and final passes),
  so the grounded turn carries `GROUND_SHAPE` after the dating rules: the first sentence settles the question;
  every part answered with the evidence's specifics in full; one pick when asked which, a list written out when
  asked; prose with bold, no headings or tables on a chat answer, no recap; the leak phrases banned. And
  `GROUND_ESTIMATE`: a figure asked for and not in the sources is built from the evidence's rate or unit price
  and known typical ranges, shown in one line, dated, with where to confirm; a bare refusal is never acceptable.
- **Measured**: concise 5.4 → 6.3, structure 5.4 → 5.9, fit 4.4 → 4.8 (every run); headings 52 → 4, tables
  10 → 3, length −28%. q17.t2 (the rent): three of the four branch-and-base answers in the same wave gave
  "Lagoa, about R$4,400 / $800" inside the reference range, against round 2's refusals; q19 (the two-week total)
  still gives fares alone on every run, and the refusal count by regex didn't fall (4,1 → 6,4), so the estimate
  rule lands sometimes, not reliably.

### 5. Travel. +0.50 (5 turns)

q17 and q17.t2 carry it (the junk out of the pack, the pick and the price). q18, q19 and q21 are 2–4 on every
run: the engines return listicles, the writer asks questions back (q18), gives fares without living costs (q19),
and finds GOL's limits only sometimes (q21).

### 6. Follow-ups. +0.50 (3 turns)

`GROUND_FOLLOWUP` tells a follow-up with nothing new to look up not to restate the earlier answer. q11.t2 2,3 →
3,4 and q17.t2 4,6 → 6,5; q01.t2 stays 2 on every run: all four answers say they can't see Claude's reply
(right), then restate turn 1, because no new search runs and turn 1's pack has nothing newer to add.

## Where it still fails

1. **q01 and q01.t2 (2–3 on every run).** The engines return TPU-deal blogs and a 2020 Ampere press release;
   no answer names Blackwell Ultra, Vera Rubin or Trainium. A fix needs a source the engines don't list.
2. **q02, q03 (2–4).** The vendor's price page (Apple's, DigitalOcean's GPU pricing) isn't in what comes back;
   the pack has the comparison pages and the droplet page only.
3. **Health (3.8, flat).** Not searched for the figures that matter (q12's fixes, q11.t2's alcohol line); the
   writer's 2024 memory fills them.
4. **q19 and q18**: the writer still won't total a trip or pick a city without a figure in the evidence.
5. **The reference sheets are two weeks stale** for the fresh turns (above).

## Verdict

**Merge as a step; not a quality release.** The overall gain (+0.15 to +0.26, one standard error) is not clearly
beyond this bench's noise, so "answers clearly better" can't honestly be claimed. What can: the answers are shaped
the way the person asked on every run (structure +0.5, concise +0.9, fit +0.4; headings and tables mostly gone;
no pipeline words), the post-pass costs nothing and has not been caught making anything wrong, first text is
unchanged, there were no error answers in 165 turns, and the three ranking changes keep listings, threads, ads
and front pages out of the evidence. Fresh facts and health did not move, and q01–q03 are still 2–3: those are
the engines' results, not the writing, and this round's levers don't reach them. "Not close yet" to Claude's
answers on the fresh and travel questions.

## What's left, ranked

1. Refresh `questions.json` (`as_of`) for the fresh turns before the next comparison; q04/q05 are measuring
   the calendar, not the answers.
2. Fresh facts for q01–q03 need a source the two engines don't return (the vendors' product pages); the free
   feeds (6b520) cover launches, not prices or chips.
3. The estimate rule lands on a rent, not on a trip total: a worked example in the turn (fare + days × daily cost)
   may do what the rule alone doesn't.
4. Health turns don't search for the specific fix the person asked for; `ground_hints` could route "what fixes
   it" to a clinical source.
5. A second writer for Thinking on this Mac (Qwen3.8-27B or Ministral-3-14B are cached) is untried; the
   candidate list's "different local final writer" needs its own two paired runs.

## Reproduce

```bash
L=<scratchpad>/engines.lock; until mkdir "$L"; do sleep 30; done; echo quality-3 > "$L/owner"
"$VENV" tools/answer_eval/run_bench.py --config local-fast --name q3-br-a --repo . --port 9898 --no-lock --redo --warmup
# the base: --repo <a git archive of 686e70a>; an ablation: --env MILLENAI_Q3_OFF=polish
rm -f "$L/owner"; rmdir "$L"
BLIND_DIR=q3w1 python3 tools/answer_eval/blind_batches.py make q3-base-a q3-base-b q3-br-a q3-br-b
# judges: Opus subagents, RUBRIC.md's prompt, blind/q3w1/<turn>.md -> blind/q3w1/out-<turn>.jsonl
BLIND_DIR=q3w1 BLIND_OUT=opus-blind-w1.jsonl python3 tools/answer_eval/blind_batches.py split
python3 tools/answer_eval/paired.py q3-base-a,q3-base-b q3-br-a,q3-br-b
```
