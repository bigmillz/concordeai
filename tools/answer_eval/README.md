# Answer eval bench

A measuring stick for ConcordeAI's answers: thirty questions in the owner's own
register (plus three follow-up turns), a reference sheet per question with the
facts a great answer must get right as of the bench date, a runner that drives a
dev copy of the app over HTTP exactly as the page does, and a rubric with a
reproducible grading procedure. `BASELINE.md` holds the first results.

Files:

- `questions.json` — the question set; `as_of` is the bench date every sheet
  was checked against. Each turn has `reference.key_facts`, `traps` (wrong or
  stale claims), `sources` (the URLs checked) and `ideal` (the shape of a great
  answer).
- `persona.json` — the asker's standing preferences, posted to the copy's
  `/api/prefs` before the run so answers are judged the way the real app would
  write them. Paraphrased from the owner's own settings; no memory facts.
- `run_bench.py` — the runner (stdlib only). Starts a windowless dev copy from
  this checkout, asks every question through `POST /api/chat`, records the
  stream, stops the copy and deletes its folder.
- `RUBRIC.md` — the six scores, the hallucination flags, the judge prompt.
- `judge_packet.py` — one judging packet per answer, and batches for a judge.
- `score.py` — aggregates `judgments/<run>/` into a table by category.
- `stats.py` — timings, first-visible-text, councils, compositors and skipped
  seats per run, with no judging needed.
- `runs/<run>/` — one JSON record per turn, `summary.json`, `answers.md`,
  `devcopy.log`, `scores.md`.

## Running the bench

Every run that loads real models shares this machine's RAM and the server's
card with everything else running, so the runner takes a lock for the whole
run: `engines.lock` for a local config, `server-gpu.lock` (with an `owner`
file) for a server config, both mkdir locks under `--lock-dir` (default
`$EVAL_LOCK_DIR`, else this session's scratchpad). It waits until the lock is
free and removes it when done, including on an error.

One command per config, from the repo root of the checkout to measure:

```bash
# the owner's server alone, Thinking (what the "AI hardware leaders" chat used)
python3 tools/answer_eval/run_bench.py --config server-think --name <run-name>
# the owner's server alone, Fast
python3 tools/answer_eval/run_bench.py --config server-fast --name <run-name>
# this Mac's MLX models, Thinking / Fast
python3 tools/answer_eval/run_bench.py --config local-think --name <run-name>
python3 tools/answer_eval/run_bench.py --config local-fast --name <run-name>
```

Useful flags: `--only q01,q07` (a subset), `--limit 5`, `--redo` (re-ask what
is already recorded; without it a re-run resumes and re-asks only error
answers), `--port 9893` (default 9894; never 8889 or 9901-9903), `--attach`
(use a copy already running on the port, no start/stop), `--keep-home`,
`--persona ''` (no preferences), `--repo <path>` (another checkout).

The dev copy runs on the app's venv Python (`~/Library/Application
Support/MillenAI/venv/bin/python3`, which has `ddgs` and `psutil`; a bare
Python silently loses web search) in its own `MILLENAI_HOME`
(`~/Library/Application Support/MillenAI-evalbench-<port>`), windowless, with
no cloud keys. A server config copies only `servers.json` (its secrets are
inline) from the real data folder into that home, mode 0600, never prints it,
and deletes it when the run ends. The tier is `srv:<id>:think` / `srv:<id>:fast`
for the first paired server (`--server-name` picks one).

Each record keeps: the request sent, the response headers (`X-Models`,
`X-Web-Search`, `X-Eta`), every `STATUS`/`STEP`/`RUN`/`DRAFT`/`SOURCES` frame
with its time, the drafts (model and text, as the page shows them), the
sources, the final answer, `first_byte_s`, `first_text_s`, `elapsed_s`,
`chars` and two tok/s figures (`rate_ui_tok_s` is what the app's meter shows:
chars/4 over the whole wait).

## Grading

```bash
python3 tools/answer_eval/judge_packet.py <run-name>        # packets/ and batches/
# give RUBRIC.md's judge prompt + batches/<run-name>/batch-N.md to the judge;
# save its JSON lines as judgments/<run-name>/batch-N.jsonl
python3 tools/answer_eval/score.py <run-name> [<other-run>]  # table by category
```

The judge is a Claude model run from the development session (a Fable
subagent), or the engineer reading the packet. It never uses the owner's paid
cloud API keys. Judging rules are in `RUBRIC.md`; an answer that is an error
line scores 1 on everything.

## Refreshing the reference sheets

The sheets are dated. Before a new comparison, re-check the `fresh` questions
(hardware, prices, releases, rates) against their sources and move `as_of`;
then grade every run you want to compare against the same sheets.
