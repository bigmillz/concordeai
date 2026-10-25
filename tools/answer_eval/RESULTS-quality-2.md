# Answer bench: quality-2 against quality-1 and the baseline (2026-10-25)

The short version: across three full runs, quality-2 scores **3.5 overall** (3.4, 3.8 and 3.5), against
**3.4** for quality-1 and **3.1** for the baseline, all graded by Opus judges. **That is inside this bench's
noise against quality-1** (paired, +0.06 to +0.39, standard error about 0.25 to 0.3). Some gains repeat on
all three runs:

- high-severity hallucinations: **44 → 35–38**
- "grounded": **4.1 → 4.4–4.7**
- arithmetic no longer searches; q28 went **5 → 9** on all three runs
- follow-ups: **1.7 → 2.0–2.7**
- searched answers with at least one primary source: **8 of 29 → 17–22 of 30**

Depth barely moved (**3.4 → 3.5**), fit rose a little (**3.8 → 4.0–4.1**). First text stayed at a **51 s median** (49 s
in quality-1; 55 s on the slowest run). Fresh facts and travel are still poor. Every number below is
**measured**. Where a figure is estimated, it is marked *, with the reason in a footnote.

## What was measured

- **Config**: the same as `RESULTS-quality-1.md`: tier `srv:<Ollama1>:think` (the server alone, the owner's
  setup), the 30 questions and 33 turns of `questions.json` (`as_of` 2026-10-24), `persona.json` posted to
  `/api/prefs`, `run_bench.py --warmup`, a windowless dev copy on port 9893, no cloud keys. Each run held the
  server-GPU lock for its whole length (owner `quality2`).
- **Grading**: Opus judge subagents, six batches of up to six packets per run (`judge_packet.py --batch 6
  --label opus`), the judge prompt from `RUBRIC.md`, no cloud keys. They are the same judges and prompt as the
  quality-1 and baseline-Opus columns.
- **Runs** (`runs/<name>/`, `judgments/<name>/`):

  | run | code | what differs |
  |---|---|---|
  | `baseline-server-think-opus` | a99681a | the baseline's answers, Opus-graded (from quality-1) |
  | `q1b-server-think` | quality-1 6623d12 | quality-1 as measured (from quality-1) |
  | `q2-server-think` | quality-2 fd0039a | all six fixes, first cut (plan waited 5 s) |
  | `q2b-server-think` | quality-2 29c3dab | + the plan waited 8 s, a looping writer hands over to the critic's seat |
  | `q2c-server-think` | quality-2 467f2cd | + the latest dated page ranks up, a stale source can't correct, no pointer-only answers |

  The final branch (2a97eba) differs from 467f2cd in three places. The test namespaces are updated, and the new
  state is declared machine-wide. The final writer's reasoning level now rides its messages instead of a `with`
  block; it is off by default, so behaviour is the same. The planner skips reasoning models, which doesn't
  change the Thinking council here, because its plan seat is ministral. None of these changes what a run on
  this config does.
- `smoke1` (fd0039a minus the last edits, six turns) holds the measurement of the final writer at `think: "high"`.

### How much to trust a difference

There is one sample per run at temperature 0.75. The three quality-2 runs on near-identical code moved by up
to 0.33 overall between runs, which is as large as any gain below. Paired over the 33 turns:

| comparison | mean overall change | std. error | better / worse / same | high flags: fewer / more on |
|---|---|---|---|---|
| baseline → quality-1 | +0.27 | 0.24 | 15 / 11 / 7 | 23 / 3 |
| baseline → q2 | +0.33 | 0.28 | 15 / 10 / 8 | 25 / 1 |
| baseline → q2b | **+0.67** | 0.34 | 19 / 9 / 5 | 24 / 5 |
| baseline → q2c | +0.36 | 0.31 | 13 / 8 / 12 | 24 / 5 |
| quality-1 → q2 | +0.06 | 0.27 | 10 / 9 / 14 | 11 / 9 |
| quality-1 → q2b | +0.39 | 0.29 | 12 / 6 / 15 | 12 / 9 |
| quality-1 → q2c | +0.09 | 0.23 | 11 / 7 / 15 | 12 / 10 |
| q2b → q2c (same fixes, two small edits) | −0.30 | 0.27 | 10 / 9 / 14 | 8 / 10 |

## Scores

### Overall, by category (1–10, mean of the judges' `overall`)

| category | n | baseline (Opus) | quality-1 | q2 | q2b | q2c | **quality-2, mean of 3** |
|---|---|---|---|---|---|---|---|
| fresh | 6 | 1.5 | 2.8 | 2.2 | 4.0 | 2.8 | **3.0** |
| compare | 4 | 3.0 | 2.8 | 3.0 | 3.0 | 2.8 | **2.9** |
| health | 6 | 4.0 | 4.2 | 3.8 | 3.7 | 4.2 | **3.9** |
| travel | 5 | 2.8 | 2.6 | 2.8 | 2.8 | 2.8 | **2.8** |
| money | 5 | 2.8 | 4.0 | 4.0 | 4.0 | 4.2 | **4.1** |
| reasoning | 3 | 6.0 | 5.7 | 6.7 | 7.0 | 5.7 | **6.4** |
| technical | 1 | 5.0 | 3.0 | 3.0 | 3.0 | 3.0 | **3.0** |
| follow-up turns | 3 | 2.0 | 1.7 | 2.7 | 2.7 | 2.0 | **2.4** |
| **all** | 33 | **3.1** | **3.4** | 3.4 | 3.8 | 3.5 | **3.5** |

### The six scores, all 33 turns

| run | correct | grounded | depth | fit | structure | concise | overall | error answers |
|---|---|---|---|---|---|---|---|---|
| baseline (Opus) | 3.3 | 2.9 | 3.9 | 4.2 | 5.7 | 5.2 | 3.1 | 0 |
| quality-1 | 4.0 | 4.1 | 3.4 | 3.8 | 5.2 | 5.1 | 3.4 | 0 |
| q2 | 4.0 | 4.4 | 3.5 | 4.0 | 5.2 | 5.4 | 3.4 | 2 |
| q2b | 4.3 | 4.7 | 3.5 | 4.1 | 5.7 | 5.7 | 3.8 | 0 |
| q2c | 3.9 | 4.4 | 3.5 | 4.0 | 5.5 | 5.4 | 3.5 | 0 |

q2's two error answers:

- **q21**: the writer looped and the answer was "None of Ollama1's models answered". Fixed in q2b: a looping
  writer now hands the draft to the critic's seat; q2b recovered such a draft on one turn.
- **q17.t2**: an answer that only apologised for the missing rent, which the rubric scores as an error. The brief
  now bans pointer-only answers, but q2b and q2c still scored this turn 2.

### Hallucinations (judges' flags, all 33 turns)

| | baseline (Opus) | quality-1 | q2 | q2b | q2c |
|---|---|---|---|---|---|
| high / medium / low | 111 / 97 / 47 | 44 / 69 / 42 | **35** / 37 / 18 | **38** / 44 / 26 | **37** / 39 / 22 |

## Timing (all 33 turns)

| run | wall time median (mean) | first visible text median (mean) | answer length mean |
|---|---|---|---|
| baseline | 83 s (85) | 78 s (80) | 1,837 chars |
| quality-1 | 63 s (63) | 49 s (48) | 1,586 |
| q2 | 63 s (63) | **51 s** (50) | 1,190 |
| q2b | 70 s (81) | **55 s** (64) | 1,333 |
| q2c | 64 s (63) | **51 s** (49) | 1,286 |

- **The 60 s first-text target holds on the median, not on every turn.** q2b's mean of 64 s comes from a few
  slow turns: q01 took 124 s, with the critic at 33 s and the final writer at 30 s against the usual ~15 s and
  ~12 s. The server answered slower during that run.
- The grounding now takes 15–18 s, against 11–15 s in quality-1. The primary-source queries are slow
  (10–13 s beside ten other engine calls) and are given 11 s; a plan that lands extends the search by up to 5 s.
- **Arithmetic is two to three times faster**: q27 and q28 took 19–32 s, against 59–66 s when they searched.

## What changed, fix by fix, and what it did

### 1. No search for pure arithmetic: done, measured

**The fix.** A rule classifier (`reasoning_parts`, `self_contained_reasoning`) splits the question into parts:

- a premise: a statement with the person's own figures
- an ask: arithmetic, or a judgement on those figures
- context
- a part that wants the world

A question with three or more figures, an ask, and no world part never searches, and the writer is told the
figures are premises. On a mixed question the planner drops the premises and the sums. In q29 the old
queries were "Two Airbnbs one 4.6 stars 212 reviews"; now only the Florianópolis pricing part is searched.

**What it did.**

- q27 and q28 searched 0 of 6 times.
- q28 scored 9, 9, 9 (quality-1: 5, from a bogus [7] citation).
- q27 scored 8, 9 and 5. The 5 in q2c is gpt-oss multiplying 11 × 106.40 wrong; the critic missed it.
- On the 33 bench turns the classifier has no false positive. It also says "search" for "Is $4,000 a month
  enough to live in Lisbon?" and "Prius or Civic at $3.40 gas", as it should.

### 2. The critic: done, partly effective

**The fix.**

- The critic now labels each line WRONG, UNSUPPORTED, CONTRADICTS, MISSING or STRUCTURE.
- It may only quote the draft and the evidence, and it gets the person's standing instructions.
- `critic_filter` drops every line that brings a number or a proper noun absent from the evidence, the
  drafts and the question. In the replay of quality-1's notes, "Bela Vista" and "a July 2026 guide lists
  $320–$420" are dropped.
- It also drops an UNSUPPORTED note on a figure the evidence states, such as quality-1's q04 "first hike since
  2023".

**What it did.**

- It dropped **29, 25 and 32 lines** in the three runs.
- High flags that a judge traced to the reviewer went from 5 in quality-1 to 3, 11 and 7. That metric is
  noisy, so it doesn't show the reviewer inventing less.
- The reviewer still misleads. In q2's q01 it "corrected" a right B300 to the A100 from a 2020 Ampere press
  release, which the site hint had brought in. Since q2c, a source more than a year older than the newest in
  the pack can't back a correction.

### 3. Corrections stick: done, but there are few of them

**The fix.**

- A WRONG note becomes a mandatory correction when the evidence states its corrected figure in one sentence
  with the claim, from a source that isn't stale.
- An UNSUPPORTED note becomes one when the quoted figure appears nowhere in the evidence or the question.
- The corrections go to the final prompt as a "CORRECTIONS TO APPLY" block.
- After writing, a string check looks for the wrong words. Any it still finds go to the fact-check as
  "KNOWN ERRORS". The card shows "Applied the review's corrections · n of n".

**What it did.** There were 6, 3 and 2 corrections, and **the writer applied every one** by the string check;
none needed the fact-check. The bar is strict on purpose, so most of the critic's right catches never become
corrections and stay as plain notes. Judges still describe a flagged error that survived in 3–4 answers per
run (quality-1: 5 by the judges' own notes, 1 by the same regex*).

### 4. Grounded follow-ups: done, measured

**The fix.**

- A turn's evidence is kept by the person's own words for 3 hours.
- A follow-up keeps the earlier sources, with sources 1–4 numbered as before.
- It searches only what it adds. The earlier question's names ride its parts ("Florianópolis furnished
  one-bedroom month reais dollars"), and "explain more" searches nothing.
- When the person mentions another assistant's reply that isn't pasted, the turn says the model can't see it.

**What it did.**

- All 3 follow-ups kept their sources on every run.
- q01.t2 opened with "I don't see Claude's reply" on two of three runs, scoring 3 and 3, against 1 in quality-1.
  On q2c the model ignored the note and scored 1.
- q17.t2 is still bad (1, 2, 2). The rent figure isn't in what the engines return, and the writer says so.

### 5. The query plan: cause found and fixed; primary sources now reached

**The cause.** It was neither a timeout nor the parse. On a mode on one server alone (the owner's
"Ollama1 Only, Thinking"), the planner's server pick, `server_side_pick`, came back **None** at 0.00 s. It was
logged live on q01 and q04: `pick=None secs=0.00`. That server's models aren't among the modes' candidates, so
the "fast" pick had nothing to choose from; 19 of 28 quality-1 plans show `plan=0.0`. Even with a pick, a
reasoning model at 120 tokens would have written no query.

**The fix, and what it did.**

- The plan now runs on the council's own non-reasoning seat. In Thinking that is the critic's ministral, which
  the last fact-check left on the card.
- It asks for the context that model is loaded with, so the card doesn't reload: the plan took 7.5 s before
  this change.
- The plan seat answered on **31 of 31** turns. Through the gateway, with about 3 s of round trip, it takes
  5–8 s. At a 5 s wait 2 of 30 plans landed; at 8 s, **14 of 28** (q2b). q2c's dev log came out empty, so its
  count isn't known.
- Separately, **primary sources by question type** (`ground_hints`), with no paid API. Each rule sends a
  `site:` query to one engine, first and with an 11 s budget. They cover the statistics agency (FRED and BLS)
  for a price, the Fed's statement for its rate, BLS's Occupational Outlook Handbook for pay, the vendor's own
  site for its products, the airline for its baggage, the IRS for estate tax, travel.state.gov for visas,
  NIH and Cleveland Clinic for health, Numbeo for living costs, the EIU for liveability, and Wikipedia for
  where a company operates. A page on the hinted host ranks up when it speaks to the question.
- **Searched answers with a primary-host source: quality-1 8 of 29 (11 sources); q2, q2b and q2c 20, 22
  and 17 of 30 (48, 42 and 35 sources).**

**The limit.** Several primary sites refuse a script's read from this network: BLS 403/503, NIH ODS 403,
Mayo Clinic 403, Azul 403, Numbeo 503, OpenAI 403, travel.state.gov 403; FRED and LATAM time out. Those pages
stand in the pack on their search snippet only. For q06 (eggs) the FRED series is found every time, but the
September value isn't in its snippet. q06 scored 2, 8 and 2: the 8 is q2b, where a news page with the figure
landed.

Two more ranking fixes:

- A compact date in a URL is read now: the Fed's `…/monetary20260916a.htm`.
- For a question about now, a page from this month or last ranks above an older 2026 one. q2's q04 answered
  with July's statement (graded 2); q2c answered with September's (7).

### 6. Depth and fit: small gains only

**The fix.**

- The grounded turn now carries "KEY FIGURES IN THAT EVIDENCE": the pack's sentences with a number and a word
  of the question, each with its source number.
- The rules let a slow-moving figure the evidence lacks (a fare, a rent, a dose, a limit, a pay range) come
  from memory as a dated "typically" range.
- The brief now says three things. Follow the person's standing instructions to the letter. Name the best
  option instead of listing five. Answer with a figure, never with only "check the listings".
- A closing "Bottom line" or "In short" paragraph is trimmed when the answer has three or more paragraphs.
- The critic gets the persona and may flag STRUCTURE.

**What it did.** Depth 3.4 → 3.5, fit 3.8 → 4.0–4.1, concise 5.1 → 5.4–5.7. Answers got shorter: 1,586 → about
1,250 characters.

**A higher reasoning effort for the final writer: tried, too slow, left off.** `think: "high"` on gpt-oss
reaches the gateway, which passes `think` through. On the GPU question it spent about 55 s reasoning before
344 visible tokens: the writer's stage went from about 12 s to 60 s and first text to 107 s (`smoke1/q01`). The
mechanism stays (`FINAL_THINK`, `MILLENAI_FINAL_THINK` in a dev copy), set to the model's own default.

## Where it still fails (all three runs)

1. **The newest facts.** q01 scored 2, 3 and 2:
   - in q2 the draft named the B300 and Trainium, but the reviewer "corrected" it toward the A100
   - in q2c the answer led with the consumer RTX 5090

   q02 misses the iPhone 18 on two of three runs. The engines' pages for "most powerful GPU" are
   listicles. The vendor `site:` queries bring back old posts as readily as new ones: an Ampere launch, the
   TPU v5e. The 20B model's 2024 memory fills the rest.
2. **Travel and comparisons (2.8 and 2.9).**
   - q07, q19 and q20 lack the figures (gas, fares, the EIU ranking) or pick badly.
   - q17 invents geography around Florianópolis.
   - q21 still says LATAM and GOL publish no size limits: their pages refuse the read.
3. **The writer itself.** gpt-oss:20b miscomputes (q27 in q2c), keeps a correctly flagged error (q11.t2: a
   "2017 glycinate trial"), and treats Pat as a non-US citizen for estate tax (q24). One 20B writer is the
   ceiling of this config.
4. **Follow-ups that need a number the web doesn't hand over** (q17.t2) are still 1–2.

## Verdict: ship or not?

**Not as a quality release. It is safe to merge as a step, but it doesn't make the answers clearly better.**

Arguments for:

- High-severity hallucinations fell again: 44 → 35–38, with fewer on 24–25 of 33 turns against the baseline.
- Grounded rose: 4.1 → 4.4–4.7.
- Arithmetic answers in a third of the time and no longer cites junk.
- Follow-ups carry their sources.
- The query plan finally runs, and primary sources appear in two to three times as many answers.
- No new error answers on the last two runs, and first text keeps its ~51 s median.

Arguments against:

- The overall gain over quality-1 (+0.06, +0.39, +0.09) is inside the run-to-run noise (0.3).
- Fresh and travel, the categories the owner cares most about, are not reliably better.
- q2b's slow server pushed the mean first text to 64 s.
- The reviewer can still mis-steer, now less often.

If shipped, ship quality-1 and quality-2 together, described as "fewer invented specifics", not "better
answers". The next real lever is the source of fresh facts, ranked below.

## What's left, ranked

1. **Fresh facts need sources that answer a script.** The `site:` hints reach the right hosts, but half of them
   refuse a read and the rest return old posts. Options:
   - date-restricted queries (the engines' own time filter, if ddgs exposes it)
   - FRED's CSV and the BLS API, both free: unreachable from this network today, so check from the server
   - reading the snippet's figure better
   - a search API with freshness. That is paid, so it's Patrick's call, shown as cost against use.
2. **A second writer or a stronger one for Thinking.** The 20B writer's arithmetic and judgement errors are
   now the main failure on grounded turns. Options:
   - a deterministic arithmetic check (re-run the sums the answer states)
   - test ministral, not gpt-oss, as the final writer when the critic flagged errors
3. **The plan's latency.** It lands 50% of the time at 8 s. Starting it before route resolution, or caching the
   plan per question, would land it more often without adding time.
4. **Corrections**: loosen the backing rule carefully, e.g. accept a correction whose figure is in the evidence
   and whose quote shares the sentence's subject. Each loosening needs a bench run, because a bad correction
   (the A100) costs more than a missed one.
5. **Make `stats.py` independent of the dev log**: q2c's log came back empty. Record the plan outcome in the
   STEP frames.

## Reproduce

```bash
L=<scratchpad>/server-gpu.lock; until mkdir "$L"; do sleep 30; done; echo quality2 > "$L/owner"
"$VENV" tools/answer_eval/run_bench.py --config server-think --name q2c-server-think --repo . --port 9893 --no-lock --redo --warmup
rm -rf "$L"
python3 tools/answer_eval/judge_packet.py q2c-server-think --batch 6 --label opus
# judges: RUBRIC.md's prompt + batches/q2c-server-think/opus-N.md -> judgments/q2c-server-think/opus-N.jsonl
python3 tools/answer_eval/score.py baseline-server-think-opus q1b-server-think q2-server-think q2b-server-think q2c-server-think
python3 tools/answer_eval/stats.py q1b-server-think q2-server-think q2b-server-think q2c-server-think
```

\* The regex count ("review…flag…kept/stayed/survived") over the judges' `what_went_wrong` lines finds 1 in
quality-1, and 3, 4 and 3 in quality-2. quality-1's RESULTS counted 5 by reading the notes. The two methods
differ, so compare the quality-2 runs with each other, not with quality-1.
