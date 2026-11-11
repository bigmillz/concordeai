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
- **errors**: answers that were an error line, empty, or never finished
- **facts**: how many `must`/`never` patterns held

## The gate

Each run is compared with the last run of the same set version, the same `where` (local or server) and the
same judge, over the questions both answered in the build's modes. It **fails** when:

- quality fell by more than one standard error of the per-question differences (paired), or
- the median time to first words grew by more than 15 % (over the questions the same model answered in both
  runs), or
- there are more error answers than last time.

The question asked of the server alone is recorded and shown but never gated: its state is the server's, not
the build's (in three runs of main it was a busy error, a wrong answer and an empty one). Total time is shown,
not gated.

**Where.** The release step runs `--where local`: the modes answer on this Mac, whose median first words held
within about 5 % across six bench runs, and the server-only question goes to the paired server when it answers.
`--where server` (or `auto`, the command line's default) copies the real pairing in so the modes answer on the
server as in the app; that measures the owner's setup, but a thinking server model's first words moved by a
third between two runs of main an hour apart (37.5 and 25.7 s) and the server falls back to this Mac when it misses a deadline, so it informs rather than
gates.

## Running it

From the repo root, on the app's venv:

```bash
VENV="$HOME/Library/Application Support/MillenAI/venv/bin/python3"
"$VENV" tools/answer_eval/scorecard.py run --label build-280 --where local   # ask, judge, record, compare
"$VENV" tools/answer_eval/scorecard.py show                                 # the history
"$VENV" tools/answer_eval/scorecard.py show build-280                       # one run against the one before it
"$VENV" tools/answer_eval/scorecard.py judge build-280                      # grade a recorded run again
SCORECARD=1 ./release.sh beta                                               # the release step: stops the cut on a FAIL
```

A local run takes about six minutes (365–369 s for the two below, a server run 11–12), the judge included, plus any wait for another
run's engines: one dev copy on port 9916, the judge on 9917, and it waits for the bench's `engines.lock`. It
starts a dev copy of this checkout with no cloud keys and spends nothing. `--judge packets` writes blind
packets for a Claude judge in a development session instead, and `scorecard.py ingest <label> <jsonl>` adds
its grades (a Claude judge is a different judge: its runs are compared only with each other).

The judge is `mlx-community/Qwen3.6-35B-A3B-4bit`, loaded with mlx_lm after the copy has stopped, at
temperature 0, with thinking off. It is lenient (most answers 8–10), so it catches a real fall, not a tenth.
Keep it fixed: a new judge model starts a new baseline.

It is never part of the plain gauntlet, which stays offline-safe; the gauntlet only reads these files.

## Baselines (2026-11-10)

| run | code | where | quality (se) | correct | complete | concise | fit | first words | total | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| `build-279-local` | 7.0 beta 2, main d804e0f | local (Gemma 4 26B) | **8.15 (0.58)** | 8.55 | 9.0 | 8.65 | 8.6 | **12.6 s** | 14.4 s | **0** |
| `quality-4-local` | quality-4 16eab7b | local | 8.25 (0.58) | 8.6 | 8.95 | 8.45 | 8.75 | 12.9 s | 15.1 s | 0 gated (the server-only case 1) |
| `build-279` | main | server, 9 of 20 on gemma4:12b | 8.6 (0.47) | 8.9 | 9.3 | 8.15 | 9.15 | 18.6 s | 20.3 s | 1 |
| `quality-4-16eab7b` | quality-4 | server, 12 of 20 on gemma4:12b | 8.75 (0.48) | 8.8 | 9.3 | 8.45 | 8.85 | 26.3 s | 27.2 s | 0 |
| `build-279-b` | main again | server, 20 of 20 on gemma4:12b | 6.85 (0.74) | 7.75 | 7.95 | 6.75 | 7.0 | 27.6 s | 29.3 s | 3 (empty answers) |

`quality-4-local` against `build-279-local`: quality +0.16 (se 0.55) over 19 questions, first words +1 %, errors
0 against 0: passes. The two pilot runs of NOTES 6b560 (`quality-4-e698872`, `quality-4-21bad97`) are kept under
`runs/` and left out of the history.
