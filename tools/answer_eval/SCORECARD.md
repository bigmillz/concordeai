# The per-build scorecard

A small, fixed check that quality and speed only go up from one build to the next (6b560). It is not the
answer bench (`README.md`): the bench is 33 hard, mostly fresh turns graded by Claude judges, for deciding
whether a change is better. The scorecard is 20 short, mostly timeless questions graded by one fixed local
judge, for catching a build that got worse.

## What it asks

`scorecard.json`, set version 1: facts (4, one of them a live exchange rate), reasoning (4), code (4),
writing (4, one asks for a README heading), advice (2, one "give me one pick"), one question about the
person's own notes (a small `apartment.md` the run adds under Settings › Your stuff) and one asked of the
paired server alone (skipped when no server answers). Each has a reference sheet for the judge, plus
`must`/`never` patterns for a facts check that needs no model.

## What it records

`scorecard/history.json`, one entry per run, and `scorecard/runs/<label>/` (every answer, its timings, the
judge's grade and reason, the copy's log):

- **quality**: the judge's overall grade, 1–10, mean over the graded questions, with its standard error;
  and the four parts (correct, complete, concise, fit) and a mean per category
- **time to first words**: median seconds from sending to the first visible word
- **total time**: median seconds per answer, and the sum
- **errors**: answers that were an error line or never finished
- **facts**: how many `must`/`never` patterns held

## The gate

Each run is compared with the last run of the same set version, the same `where` (server or local) and the
same judge, over the questions both answered. It **fails** when:

- quality fell by more than one standard error of the per-question differences (paired), or
- the median time to first words grew by more than 15 %, or
- there are more error answers than last time.

Total time is shown, not gated.

## Running it

From the repo root, on the app's venv:

```bash
VENV="$HOME/Library/Application Support/MillenAI/venv/bin/python3"
"$VENV" tools/answer_eval/scorecard.py run --label build-280      # ask, judge, record, compare
"$VENV" tools/answer_eval/scorecard.py show                       # the history
"$VENV" tools/answer_eval/scorecard.py show build-280             # one run against the one before it
SCORECARD=1 ./release.sh beta                                     # the release step: stops the cut on a FAIL
```

A run takes about 15–30 minutes* on this Mac (one dev copy on port 9916, the judge on 9917; it waits for the
bench's `engines.lock`). It starts a dev copy of this checkout with no cloud keys and spends nothing.
`--where local` keeps the server out; `--where server` insists on it. `--judge packets` writes blind
packets for a Claude judge in a development session instead, and `scorecard.py ingest <label> <jsonl>` adds
its grades (a Claude judge is a different judge: its runs are compared only with each other).

The judge is `mlx-community/Qwen3.6-35B-A3B-4bit`, loaded with mlx_lm after the copy has stopped, at
temperature 0, with thinking off. Keep it fixed: a new judge model starts a new baseline.

It is never part of the plain gauntlet, which stays offline-safe; the gauntlet only reads these files.

\* Estimated from the beta 2 baseline's wall time on this Mac; a server run or a cold model load moves it.

## Baseline

7.0 beta 2 (build 279), this Mac, Fast (Gemma 4 26B), the owner's server off: see `scorecard/history.json`
(`build-279`) and NOTES 6b560.
