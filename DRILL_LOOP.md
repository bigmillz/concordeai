# The drill loop — fully automated answer-quality improvement (6b260)

The standing machine that makes ConcordeAI's answers better while
nobody is watching. Claude (in the ConcordeAI session) is the
orchestrator, judge and editor; this file is the contract it re-reads
every cycle — context gets compacted over a days-long run, so THE
PROTOCOL LIVES HERE, NOT IN THE CONVERSATION.

## One cycle, six steps

1. **COLLECT** — with a live dev server on 9894 (CLAUDE.md), run
   `python3 drill.py --batch 6` with a FRESH seed. Transcripts land in
   `~/Library/Application Support/MillenAI/drill_runs/<stamp>/`.
2. **JUDGE** — score every record against `drill_rubric.md` (1-5 per
   axis) using a Workflow of parallel judge agents, each taking a
   slice, blind to what was recently edited. Verdict + diagnosis per
   record, naming the prompt/code at fault for anything ≤3.
3. **DECIDE** — pick the 1-3 highest-leverage edits. Check
   `drill_ledger.md` first: never re-try a reverted edit without new
   evidence, never oscillate. Prefer prompt edits; code edits allowed
   but every change must be gauntlet-clean.
4. **APPLY** — edit, `ast.parse` check, restart the server (the file
   is read once at import — CLAUDE.md), spot-check the specific
   failing case over the wire.
5. **GATE** — full gauntlet. Red → revert the edit, log the attempt in
   the ledger, move on. Green → commit with scores in the message.
   NO releases, NO version bumps, ever (standing rule; the human cuts).
6. **LOG + LOOP** — append the cycle to `drill_ledger.md` (scores by
   mode, edits landed/reverted, next hypotheses), update the FERRARI
   METER dashboard at ~/Library/Application Support/MillenAI/
   drill_runs/ferrari-meter.html (scores, %, cycle log, timestamp) and
   REPUBLISH it with the Artifact tool — same file path keeps the URL
   https://claude.ai/code/artifact/2229c32d-013b-4343-8020-8efc150be01c
   (favicon 🏎️, keep it). Meter % = mean over modes of
   min(avg/4.5, 1) × (0.5 single batch at bar, 1.0 two consecutive).
   The meter is ALSO served locally at http://127.0.0.1:9897
   (meter_server.py in drill_runs/, plain stdlib, auto-refreshes the
   open tab every 5 min). Each cycle: confirm it's alive
   (`lsof -tnP -iTCP:9897 -sTCP:LISTEN`) and restart it with nohup if
   not — Patrick has the link bookmarked.
   Then ScheduleWakeup the next cycle (~45-60 min).
   PUSH NOTIFICATIONS (promised to Patrick): send exactly one when the
   meter reaches 100% ("Ferrari bar reached — ready to cut"), and one
   if the loop stalls needing him (server won't start, repo conflict,
   budget floor). Never ping for ordinary progress.

## Anti-overfitting rules

- **Holdout**: questions marked HOLDOUT in drill.py are NEVER used to
  pick edits. Every 5th cycle, run them and compare against the tuned
  set — a growing gap means we're gaming the bank, not improving.
- **Grow the bank** every few cycles with fresh questions in whatever
  class last failed (paraphrase real usage — the repo is public, no
  verbatim personal content).
- **Same-seed replay** after a fix (prove the case moved), then fresh
  seeds (prove it generalises).
- **Stop condition** per mode: average ≥4.5 across two consecutive
  fresh-seed batches → maintenance (that mode only every 5th cycle).

## Budget + safety rails

- One judge Workflow per cycle, ≤6 agents. If session tokens run low,
  finish the cycle, commit, leave the ledger current, stop cleanly.
- Never edit `~/Library/MillenAI-live/repo` (the deployment trap).
- cloud.json is user data: back up before any repair, never log keys.
- The drill shares engines/prefs with the desktop app (CLAUDE.md) —
  Patrick using the app during a batch skews timings; scores care
  about content, not speed, so judge accordingly.

## Ledger format (drill_ledger.md)

    ## cycle N — <utc stamp> — app <git short hash>
    batch: <seeds/modes/counts>   scores: chat X.X · web X.X ·
    messy X.X · funnel X.X (axes in parentheses)
    landed: <edit -> evidence>    reverted: <edit -> why>
    next: <ranked hypotheses>
