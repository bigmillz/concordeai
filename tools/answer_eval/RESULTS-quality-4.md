# Answer bench: quality-4 against 7.0 beta 2 (2026-11-10)

The short version: on this Mac's Fast tier (Gemma 4 26B on MLX, one writer), all 33 turns, three samples a side,
each graded blind by Opus judges in up to three waves, **quality-4 as first ported (quality-3's five changes) is
not better than main overall: −0.08 (se 0.16)**. What it changes is the shape, and that holds on every sample:
**concise 5.4 → 6.3 (+0.87, se 0.16), fit 4.2 → 4.5 (+0.31, se 0.20)**, headings in 22–26 answers a run → 0–2,
tables 9–14 → 2–5, the pipeline's words ("the provided sources do not list") 8–12 → 0–1, answers about 30 %
shorter, 1.2 s faster to finish (se 0.5), first words unchanged. It also carried **more invented figures: 0.28
high-severity flags an answer against main's 0.17**. The ablations pin that on the ESTIMATE WHEN ASKED rule:
the same build without it, graded beside both, scored **+0.26 (se 0.23) over the build with it and +0.17 (se
0.24) over main**, with main's rate of high flags. So the rule is dropped and quality-4 ships the rest. Every
number here is measured; nothing is estimated.

## What was measured

- **Config**: `run_bench.py --config local-fast --warmup`, port 9914, a windowless dev copy with no cloud keys and
  no `servers.json`: the owner's server (Ollama1) was off when the runs were planned (a dev copy with the real
  pairing saw it "offline"), so the A/B is this Mac's. Tier Fast = **Gemma 4 26B** on every one of the 264 turns.
  `as_of` stays 2026-10-24 (the fresh sheets are two weeks stale; both sides are hit alike).
- **Code**: base = main d804e0f (7.0 beta 2, build 279), extracted with `git archive`. Branch = `quality-4` 41da588
  (quality-3's 2fd493c + e7fda07 on hotfix-drip, plus the 6b560 wiring), frozen the same way. The ablations are the
  same frozen branch with a dev copy's knob: `q4-ab-shape` = `MILLENAI_Q3_OFF=shape,estimate,followup`,
  `q4-noest` = `MILLENAI_Q3_OFF=estimate`.
- **Order**: br-a, base-a, base-b, br-b (ABBA), then ab-shape, base-c, br-c, then noest. Each run held
  `engines.lock`; the scorecard baseline ran between br-b and wave 2. Another helper's gauntlet ran during br-a,
  and this branch's own gauntlet during ab-shape, base-c, br-c and noest, so their timings carry that load
  (both sides of wave 2 alike).
- **Grading**: Opus judge subagents with RUBRIC.md's prompt, blind and by turn (`blind_batches.py`): each judge got
  every run's answer to three turns, run names replaced by shuffled codes, and the step card's "Sharpened · n leaks
  reworded" detail removed from the metadata (it named the branch). Wave 1: base a, b and branch a, b. Wave 2: base
  a, c, branch a, c, ab-shape. Wave 3: base a, c, branch a, c, noest. The anchors moved +0.06 (se 0.10) and +0.21
  (se 0.11) between waves 1 and 2. Judgments: `judgments/<run>/opus-blind-q4w{1,2,3}.jsonl`.
- **A bench fix found on the way**: `run_bench.py` didn't know the USAGE, DONE and RUBIN frames (6b519, 6b528),
  so the recorded answers carried them as text and `first_text_s` fired on the first USAGE frame, about 1 s in,
  before any word. Fixed; `refix_frames.py` cleaned q4-br-a and q4-base-a, and every timing below is the app's
  own first-word clock from the DONE frame (`app_first_s`).

## Scores

### Per run and wave (overall, 1–10, and the six parts)

| wave | run | all | correct | grounded | depth | fit | structure | concise | high flags |
|---|---|---|---|---|---|---|---|---|---|
| 1 | q4-base-a | 4.24 | 4.7 | 5.4 | 4.0 | 4.2 | 5.5 | 5.4 | 6 |
| 1 | q4-base-b | 4.21 | 4.7 | 5.4 | 3.8 | 4.2 | 5.7 | 5.4 | 5 |
| 1 | q4-br-a | 4.00 | 4.5 | 5.1 | 3.9 | 4.5 | 5.6 | 6.3 | 11 |
| 1 | q4-br-b | 4.24 | 4.7 | 5.5 | 3.9 | 4.7 | 5.8 | 6.5 | 6 |
| 2 | q4-base-a | 4.30 | 4.8 | 5.9 | 3.9 | 4.2 | 5.5 | 5.5 | 4 |
| 2 | q4-base-c | 4.12 | 4.6 | 5.8 | 4.2 | 4.2 | 5.5 | 5.4 | 3 |
| 2 | q4-br-a | 4.21 | 4.6 | 5.7 | 3.9 | 4.5 | 5.6 | 6.3 | 7 |
| 2 | q4-br-c | 4.12 | 4.4 | 5.5 | 3.9 | 4.5 | 5.5 | 6.2 | 9 |
| 2 | q4-ab-shape | 4.06 | 4.5 | 5.4 | 4.1 | 4.1 | 5.3 | 5.0 | 4 |
| 3 | q4-base-a | 4.21 | 4.9 | 5.8 | 4.0 | 4.2 | 5.7 | 5.4 | 7 |
| 3 | q4-base-c | 4.18 | 4.7 | 5.8 | 4.1 | 4.2 | 5.5 | 5.4 | 8 |
| 3 | q4-br-a | 4.15 | 4.7 | 5.6 | 4.0 | 4.4 | 5.7 | 6.3 | 10 |
| 3 | q4-br-c | 4.06 | 4.4 | 5.6 | 3.9 | 4.5 | 5.8 | 6.2 | 12 |
| 3 | **q4-noest** | **4.36** | 4.8 | 5.8 | 3.9 | 4.5 | 5.8 | **6.4** | **7** |

### Paired by turn (mean over the side's runs, 33 turns)

| comparison | overall | notes |
|---|---|---|
| wave 1: base a,b → branch a,b | −0.11 (se 0.17) | better/worse 12/14 |
| wave 2: base a,c → branch a,c | −0.05 (se 0.21) | better/worse 14/16 |
| wave 3: base a,c → branch a,c | −0.09 (se 0.20) | grounded −0.15 (se 0.19), high flags/answer +0.11 (se 0.08) |
| **all waves: 3 base samples (6 gradings) → 3 branch samples (6 gradings)** | **−0.08 (se 0.16)** | correct −0.18 (0.16), grounded −0.16 (0.13), depth −0.11 (0.14), fit **+0.31 (0.20)**, structure +0.10 (0.15), concise **+0.87 (0.16)**; high flags 0.17 → 0.28 an answer |
| wave 2: branch a,c → branch without shape, estimate, follow-up | −0.11 (se 0.25) | concise falls back to 5.0, headings back on 24 answers: the shape rule is what makes the style |
| wave 3: branch a,c → **branch without estimate** | **+0.26 (se 0.23)** | grounded +0.20 (0.19), high flags/answer −0.12 (0.09) |
| wave 3: base a,c → **branch without estimate** (= quality-4 as shipped) | **+0.17 (se 0.24)** | grounded +0.05, high flags −0.02: main's rate |

By category, all waves (branch with the estimate rule): reasoning +1.28 (3 turns), health +0.11, money +0.07,
travel −0.10, compare −0.12, fresh −0.28, follow-ups −1.33 (3 turns; the follow-ups ride each run's own first
answer, and the shape-off ablation lost them too, so it is not the follow-up note).

### What the judges flagged that the estimate rule invited

High-severity flags only the branch carried, in the turns that ask for a figure no source states: "a furnished
one-bedroom runs approximately R$ 6.635 or $1.200" (a euro figure relabelled as dollars, q17.t2), "for a two-week
stay you would roughly need £1,223" above the source's £945 a month (q19), Medellín's rent in reais at Lisbon's
level (q09), trade wages of "$80,000 and $130,000+" against BLS medians near $60k (q22), and "R$ 4,500 and R$
7,500 per month (roughly $2,500–$4,200 USD, using 1 USD = 5.5167 BRL)", three times off (q17.t2). The last one is
a conversion the post-pass should have fixed: it read neither a range written with "and" nor a rate written
"1 USD = 5.5167 BRL". Both forms are now read (6b560).

## Timing (the app's own clock, all 33 turns, medians of per-turn medians)

| | first words | total |
|---|---|---|
| main, 3 runs | 17.6 s | 25.5 s |
| quality-4 as first ported, 3 runs | 17.9 s (+0.13 s, se 0.29) | 24.0 s (−1.22 s, se 0.54) |
| quality-4 as shipped (no estimate), 1 run | 17.6 s (+0.25 s, se 0.29) | 23.1 s (−2.53 s, se 0.50) |

No error answer in 264 turns.

## Verdict

Ship quality-4 without the estimate rule: the post-pass, the shape, the follow-up note and the vendor's page
stay. Its gain is the shape the owner's standing instructions ask for (direct, no headings, no recap), measured on
every sample, at no cost to correctness that the bench can see and slightly faster answers. It is **not** a
quality release: overall +0.17 (se 0.24) is under one standard error, and the low scores (q01–q03, q07, q19,
2–3 of 10) are search and model knowledge, which no rule here touches. The server path (where most of the owner's
answers go) was not measured: the server was off when the A/B started.

## After the bench: the server path

The scorecard's first server runs found what this bench (local) could not: the owner's gemma4:12b reasons
hidden by default, comes in 25–35 s, and can run out of its 8,192 context, leaving answers cut or empty (on main
too: 3 of 20 empty in one scorecard run). quality-4 now treats an empty server answer as a failure before the
first word, so this Mac's copy answers. Turning the reasoning off (`think: false`) made it three times quicker
but the local judge graded it 8.2 → 5.9 on the same 12 questions, so that is a dev copy's pilot
(`MILLENAI_Q4_NOTHINK=1`) and the owner's call. NOTES 6b560 and `SCORECARD.md` have the numbers.
