# Drill rubric — what a good ConcordeAI answer is (6b260)

Score every transcript record 1-5 on each axis; anything ≤3 gets a
one-line diagnosis naming the prompt/code at fault. The judge is
Claude reading `drill_runs/<stamp>/transcript.jsonl`.

## Chat + Web
1. **Commits.** Names real, concrete things (places, products, numbers,
   steps). "There's a place that showed up, try it" = 1.
2. **Answers the asked question.** "Open now?" needs an open/closed
   verdict or an honest one-clause "how to check", not a biography of
   the store.
3. **Honest grounding.** Volatile facts (hours, prices) cited or
   flagged; stable knowledge used freely; nothing invented. Hedging on
   stable facts scores as low as inventing volatile ones.
4. **Register + shape.** Sounds like a sharp local friend; scannable;
   no "In conclusion", no restating the question, no meta ("my data",
   "search results").
5. **Right size.** Simple ask, tight answer; meaty ask, real depth.

## Funnel
1. **Stages narrow.** Every question materially narrows; options are
   concrete and distinct; no "Which direction?" filler.
2. **Typed answers respected.** A typed answer steers the next stage
   exactly like a clicked one.
3. **The verdict is a verdict.** Names ONE specific real thing (breed,
   dish, ETF, town), not a category, never an echo of the picks.
4. **Reasoning shows.** Why THIS pick, tied to their answers + reqs.
5. **Next step.** Ends with one plain, doable instruction.

## Process
- Collector: `python3 drill.py --batch N` against a live 9894.
- Judge: read the transcript, score, write `scores.md` beside it with
  per-record scores + the 2-3 highest-leverage prompt edits.
- Apply edits to millenai.py, run the gauntlet (must stay green),
  commit (NO releases — standing rule), restart the server, next batch.
- Stop a theme when it holds ≥4.5 average across two consecutive
  batches with different seeds.
