# Answer rubric and judge prompt

One answer is graded on six things, each 1-10, plus a list of every hallucinated
claim. The judge reads the packet that `judge_packet.py` writes for the answer:
the question (with any earlier turns), the standing preferences the asker set,
the reference sheet (key facts as of the bench date, the sources checked, the
traps), the answer, and the run's metadata (which models drafted, which pages
the search returned, how long it took).

The reference sheet is the yardstick, not the ceiling: an answer may know a
correct fact the sheet lacks, and the judge may credit it when it is right and
verifiable. The sheet's `traps` are claims that are wrong or stale as of the
bench date; an answer that asserts one is wrong, whatever the model believed.

## The six scores

| key | what it measures | 2 | 5 | 8 | 10 |
|---|---|---|---|---|---|
| `correct_current` | Facts, numbers, names and dates are right **as of the bench date** | mostly stale or wrong; the central fact is wrong | the gist is right but a key figure/name is stale or missing | all key facts right, one minor slip or omission | every key fact right and dated; nothing stale |
| `grounded` | No invented numbers, dates, names, studies, quotes or sources; uncertainty is stated as such | several invented specifics presented as fact | one or two unverifiable specifics, or hedged guesses dressed as facts | specifics traceable to the sources or to well-known facts; uncertainty marked | every figure attributable; what isn't known is said plainly |
| `depth` | Depth and usefulness: the answer actually resolves the question with the detail a smart friend who knows the field would give | generic, could have been written without reading the question | covers the surface, misses the angle that matters | the important angles with concrete detail and a clear verdict | nothing a great answer needs is missing; anticipates the obvious follow-up |
| `fit` | Fits the person and the question: the asker's register, their stated preferences (direct, lead with the answer, flag what changed, one best option), the implicit situation | ignores the ask (answers a different question, lectures, lists five when asked for one) | answers the question but in a generic voice; preamble or recap | matches the ask and the preferences; a clear recommendation where one was wanted | reads as written for this person: their situation, their vocabulary, their decision |
| `structure` | Clear structure: scannable, the verdict up front, lists only where the content is a list, no headings on a short answer | a wall of text or a slide deck of headings/tables for a chat question | readable but buries the answer or over-formats | verdict first, short paragraphs, bold on the few things that matter | effortless to scan; every structural choice earns its place |
| `concise` | Length matches the question; no filler, repetition, restating, summaries or offers to help | twice the length it needs, with padding | some padding or repetition | tight; maybe one spare sentence | nothing to cut |

Scoring rules:

- **An error line is a 1 on every score.** "⚠️ Backend error", "That answer didn't
  come through", an empty answer, or an answer that only apologises for not
  having data when the data existed, score 1/1/1/1/1/1 and `overall` 1.
- **A wrong central fact caps `correct_current` at 3** even if the rest is right.
- **Every invented specific** (a number, date, price, name, study, quote or URL
  that is wrong or cannot be traced) costs `grounded` at least 2 points, and
  caps it at 4 when there are three or more.
- Do not average the six into `overall`. `overall` is "how good is this answer
  for this person", 1-10, weighted toward correctness and usefulness: a
  beautifully written wrong answer is a 2 or 3.
- Judge the final answer the person saw. Drafts are context for `what_went_wrong`
  only.

## Hallucination flags

List **every** claim in the answer that is wrong as of the bench date, invented,
or unverifiable-but-asserted. For each: the claim (quoted or closely
paraphrased), why it is wrong (cite the reference fact or source), and a
severity:

- `high` — a specific wrong number, date, name, price, law, study or quote
  presented as fact, or a wrong central verdict.
- `medium` — a specific that cannot be verified and is probably invented, or a
  stale fact presented as current without a date.
- `low` — a vague overclaim ("studies show", "experts agree") with nothing
  behind it.

A stale fact stated with its date ("the H100 was the flagship in 2023") is not a
hallucination. A fact the sheet lacks but which is verifiably true is not one
either; say so in `notes` if it affected the score.

## Judge prompt

Paste this at the top of a judging session, then the packet(s). The judge is a
model that has NOT seen the drafts or sources ahead of the answer and that grades
only what the packet contains plus its own knowledge of the world as of the
bench date. It must not use the asker's paid cloud API keys; a Claude subagent
run from the development session, or the engineer reading the packet, are both
fine.

```
You are grading answers from a local-model assistant against a reference sheet.
Today is {as_of}. Read RUBRIC.md's six scores, the scoring rules and the
hallucination severities, then grade each packet below.

For every packet, output ONE JSON object on its own line, nothing else, with
exactly these keys:

{"file": "<the packet's file name>",
 "run": "<the run name>",
 "scores": {"correct_current": n, "grounded": n, "depth": n, "fit": n,
            "structure": n, "concise": n},
 "overall": n,
 "hallucinations": [{"claim": "...", "why": "...", "severity": "high|medium|low"}],
 "what_went_wrong": "one or two sentences: the main failure, naming search,
                     drafts, merge, or model knowledge where the metadata shows it",
 "great_answer_would": "one sentence: what the best answer does differently",
 "notes": "anything that affected a score, incl. correct facts the sheet lacked"}

Scores are integers 1-10. Be strict and specific. Quote the answer when you
flag a claim. Never reward confident tone; reward being right, grounded and
useful to this person. If the answer is an error line, score all ones.
```

## Reproducing a grading run

1. `python3 tools/answer_eval/judge_packet.py <run-name>` writes
   `packets/<run-name>/<file>.md`, one per recorded turn, and `batches/` of up
   to eight packets each for a judge.
2. Give the judge the prompt above plus one batch; save its JSON lines to
   `judgments/<run-name>/<file>.json` (one object per file, or paste the lines
   into `judgments/<run-name>/batch-N.jsonl`; `score.py` reads both).
3. `python3 tools/answer_eval/score.py <run-name> [<other-run> ...]` prints the
   table by category and the hallucination counts, and writes
   `runs/<run-name>/scores.md`.

The bench date (`as_of`) and every reference fact live in `questions.json`;
when the sheets are refreshed, grade every run again, because "current" moved.
