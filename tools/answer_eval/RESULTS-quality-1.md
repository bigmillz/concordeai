# Answer bench — quality-1 against the baseline, 2026-10-24

The short version: the two answer builds together (`quality-1`, after one round of integration fixes) cut
**high-severity hallucinations by 60%** (111 to 44) and **first visible text from 78 s to 49 s** (median),
and raised "grounded" from 2.9 to 4.1 and "correct and current" from 3.3 to 4.0. **The overall score barely
moved: 3.1 to 3.4**, which is inside this bench's noise. The answers are now less wrong, but not yet more
useful: depth and fit went down, because one 20B writer drafts alone and the search still rarely brings back
the primary source. Every number below is **measured** unless it is marked *estimated*.

## What was measured

- **Config**: tier `srv:<Ollama1>:think` (the server alone), the same 30 questions and 33 turns
  (`questions.json`, `as_of` 2026-10-24), `persona.json`, the bench runner as `BASELINE.md` describes, on a
  windowless dev copy on port 9893. Each whole config ran inside one hold of the server-GPU lock.
- **Runs** (`runs/<name>/`, `judgments/<name>/`):

  | run | code | notes |
  |---|---|---|
  | `baseline-server-think` | a99681a (origin/main) | the helper's baseline, graded by Fable judges (as published) |
  | `baseline-server-think-opus` | same answers | **re-graded by Opus judges**, so every comparison below uses one judge |
  | `sg-server-think` | `bench-sg` (search-ground + the refused-model fix) | grounded search alone |
  | `ac-server-think` | `bench-ac` (answer-craft + the refused-model fix) | the answer craft alone |
  | `q1-server-think` | quality-1 at 8c7bfc0 | the first integration |
  | `q1b-server-think` | quality-1 at 6623d12 | after integration round 2: **this is the branch** |

- **Grading**: Opus judge subagents, six batches of up to six packets per run (`judge_packet.py --batch 6`),
  the judge prompt from `RUBRIC.md`, no cloud keys. The baseline was graded by Fable judges; Opus is stricter
  on these answers (the same 33 baseline answers: Fable 3.6, Opus 3.1, high-severity flags 110 against 111),
  so **compare against the Opus column**. The published 3.6 is shown for reference only.

### Two things changed on the server between the baseline and these runs

1. **Ollama1 gained two models**, `qwen3.8:27b` and `devstral-small-2:24b` (it listed 8 models for the
   baseline and 10 to 11 now). The gateway doesn't report its card's size, so every server mode seated
   `qwen3.8:27b` first; the card refused it (`gpu_fit`) and the app then marked the whole server down. The
   first attempt at this bench answered **"None of Ollama1's models answered. qwen3.8:27b doesn't fit in
   Ollama1's graphics memory."** on six of six turns. **This is happening in the shipped app too**: any server
   mode on Ollama1 fails the same way until it is fixed or the model is removed. quality-1 fixes it (6b516): a
   refused model is remembered and no longer counts as fitting, and the server stays up. So that the two
   branches could be measured on the same server, the same fix was cherry-picked onto bench-only branches
   `bench-sg` and `bench-ac` (nothing else differs from `search-ground` and `answer-craft`).
2. **A warm-up turn** (`run_bench.py --warmup`, one throwaway question, not recorded) runs before q01, so the
   copy learns what the card refuses before q01. With it, every run seated the baseline's own council
   (`gpt-oss:20b`, `ministral-3:14b`, `gemma4:12b`) on all 33 turns. It also means q01 starts with warm
   models; the baseline's q01 did not.

### How much to trust a difference

One run per config, sampled at temperature 0.75. Paired over the same 33 turns against the Opus-graded
baseline, the overall change is within noise for every config (q1b: +0.27, standard error 0.24; better on
15 turns, worse on 11). **The hallucination drop is not noise**: q1b has fewer high-severity flags than the
baseline on 23 of 33 turns and more on 3 (mean −2.0 per answer).

## Scores

### Overall, by category (1–10, mean of the judges' `overall`)

| category | n | baseline (Fable) | **baseline (Opus)** | search-ground | answer-craft | quality-1 (first) | **quality-1** |
|---|---|---|---|---|---|---|---|
| fresh | 6 | 2.5 | **1.5** | 2.2 | 2.5 | 3.0 | **2.8** |
| compare | 4 | 3.8 | **3.0** | 3.0 | 3.0 | 2.8 | **2.8** |
| health | 6 | 4.3 | **4.0** | 3.8 | 3.8 | 3.3 | **4.2** |
| travel | 5 | 3.2 | **2.8** | 2.6 | 2.4 | 1.8 | **2.6** |
| money | 5 | 3.2 | **2.8** | 3.4 | 3.0 | 3.4 | **4.0** |
| reasoning | 3 | 6.3 | **6.0** | 4.7 | 6.3 | 6.3 | **5.7** |
| technical | 1 | 5.0 | **5.0** | 4.0 | 4.0 | 3.0 | **3.0** |
| follow-up turns | 3 | 2.3 | **2.0** | 3.0 | 2.7 | 3.0 | **1.7** |
| **all** | 33 | 3.6 | **3.1** | 3.2 | 3.3 | 3.2 | **3.4** |

### The six scores, all 33 turns

| run | correct | grounded | depth | fit | structure | concise | overall | error answers |
|---|---|---|---|---|---|---|---|---|
| baseline (Opus) | 3.3 | 2.9 | 3.9 | 4.2 | 5.7 | 5.2 | 3.1 | 0 |
| search-ground | 3.5 | 3.4 | 3.6 | 4.0 | 5.4 | 5.3 | 3.2 | 1 |
| answer-craft | 3.8 | 3.3 | 3.7 | 3.9 | 5.0 | 5.1 | 3.3 | 1 |
| quality-1 (first) | 3.8 | 3.9 | 3.3 | 3.7 | 4.5 | 4.8 | 3.2 | 3 |
| **quality-1** | **4.0** | **4.1** | 3.4 | 3.8 | 5.2 | 5.1 | **3.4** | 0 |

(search-ground's "error" is q09, an answer that only said it had no data, which the rubric scores as one.)

### Hallucinations (judges' flags)

| category | baseline (Opus) | search-ground | answer-craft | quality-1 (first) | **quality-1** |
|---|---|---|---|---|---|
| fresh | 36 | 30 | 25 | 11 | **8** |
| compare | 14 | 6 | 11 | 8 | **4** |
| health | 8 | 6 | 3 | 2 | **3** |
| travel | 18 | 17 | 15 | 9 | **11** |
| money | 18 | 9 | 12 | 5 | **4** |
| reasoning | 3 | 2 | 2 | 2 | **2** |
| technical | 1 | 2 | 2 | 2 | **1** |
| follow-up | 13 | 15 | 11 | 14 | **11** |
| **high, all** | **111** | 87 | 81 | 53 | **44** |
| high / medium / low | 111 / 97 / 47 | 87 / 73 / 53 | 81 / 77 / 48 | 53 / 55 / 30 | **44 / 69 / 42** |

## Timing (all 33 turns)

| run | wall time median (mean) | first visible text median (mean) | answer length mean | final writer's own pace |
|---|---|---|---|---|
| baseline | 83 s (85) | 78 s (80) | 1,837 chars | not measured (meter 5.6 tok/s) |
| search-ground | 86 s (84) | 81 s (81) | 1,566 | — |
| answer-craft | 50 s (50) | 34 s (34) | 1,703 | median 158 tok/s |
| quality-1 (first) | 53 s (52) | 40 s (39) | 1,302 | median 160 tok/s |
| **quality-1** | **63 s (63)** | **49 s (48)** | 1,619 | median 159 tok/s |

- Grounded search costs nothing visible on the server: it runs beside the first draft (search-ground 86 s
  against 83 s).
- The answer craft is where the time went: one draft instead of three, and the reflection pass gone.
- quality-1 round 2 is 10 s slower than round 1: the final writer now asks for a longer context (16K instead
  of 8K tokens, a reload and a longer prompt read), and the fact-check ran out of its 45 s budget on 4 of 28
  answers (0 of 30 in round 1).
- **First visible text is not the final text**: the fact-check re-sends a corrected answer after a RESET at the
  end (20 of 33 answers in quality-1). The person sees text at ~49 s and its corrected form at ~63 s.

## The GPU question (q01), before and after

**Before** (baseline, 88 s, first text at 83 s). Sources: `claude.ai` (the sign-in page), `veoaifree.com`,
`pluralsight.com`, `youtube.com`. The answer:

> the most powerful GPU right now is the NVIDIA H200 (Blackwell architecture), which delivers up to 1,500
> TFLOPS of FP8 throughput when fully configured as a 16-chip unit … Claude 3.5 Sonnet (Anthropic's latest)
> runs on a hybrid stack of H100s, H200s, and Anthropic's own ASICs—the Anthropic Infernet

Graded 1 (Opus) / 2 (Fable), 8 high-severity flags.

**After** (quality-1, 64 s, first text at 51 s). Eight sources: `chip.computer`, `clusterbid.com`,
`theverge.com`, `dailytechintel.com` ("Unveiling the Epic Anthropic TPU Deal Driving $30B"),
`tomshardware.com` (GPU hierarchy), `decrypt.co` (a 2023 Google–Anthropic piece), `spoonai.me` ("Anthropic
Hits $30B Revenue, Signs Largest-Ever Compute…", 2026-04-08), `cloudminister.com`. The answer, with its
citation marks:

> **NVIDIA Blackwell** and **AMD MI350** are the highest-spec GPUs still in production in 2026, and they
> remain the main workhorses for training and inference across all major AI frameworks [2].
> Google's **fifth-generation TPUs**—the Ironwood line—have closed the performance gap with GPUs and are the
> backbone of Anthropic's **Claude 3.5 Sonnet** inference workloads [6][8].

Graded 2, 2 high-severity flags: Ironwood is the seventh generation, and Claude 3.5 Sonnet is a 2024 model.
No invented chip, no invented spec, and Anthropic's TPU deal is found; but no Blackwell Ultra, no Vera Rubin,
no Trainium, and still nothing from nvidia.com, cloud.google.com or anthropic.com. The fact-check ran:
"Checked 7 claims, all held".

For the record, the branches alone: search-ground named "the NVIDIA Blackwell B100" and "Claude 3.5 Sonnet
runs exclusively on Google Cloud's Ironwood TPUs (fifth-gen TPU v5)" (graded 2, 7 high); answer-craft, on the
old four-snippet search, invented H200 specs and monthly prices and cited "an Anthropic Sep 2026 tech talk"
that doesn't exist (graded 1, 11 high); the first quality-1 named the consumer RTX 5090 as the most powerful
AI GPU, from four "best GPU for AI" guides (graded 2).

## Where each branch helped and where it hurt

- **search-ground alone** cut high-severity flags 111 to 87 and lifted "grounded" 2.9 to 3.4, but overall
  only 3.1 to 3.2. **It made reasoning worse (6.0 to 4.7)**: it now searches arithmetic too, and in q27 the
  merge cited the search pages and flipped the verdict ("package beats separate by $5", backwards; graded 3,
  baseline 8). It also gave the image path a "What the photos show" section about pictures from the sources
  (q09). Without the craft, the old merge still picks and invents.
- **answer-craft alone** halved the time (first text 78 s to 34 s) and cut high flags to 81, overall 3.3. It
  made two things worse: its one writer is a single point of failure (q11: the server dropped the stream and
  the answer was an error line; fixed in quality-1), and **the critic invents**: its "MISSING" notes asked for
  examples and figures that the writer then made up ("Joe Rogan, David Beckham and the cast of The
  Mandalorian", "the figure comes from 3–5 listings", "ferry connections"). 17 flags in 13 answers name the reviewer, either as where the invention came from or as having flagged it to no effect.
- **quality-1 as first merged** had **three empty or error answers**: q21 and q22 came back with nothing, because
  the final writer's prompt (evidence pack + brief + uncut draft + notes, *estimated* ~31 KB) filled the
  server's default 8,192-token context and gpt-oss spent the rest reasoning; q16 hit a server hiccup on both
  seats. And the fact-check spliced hedges into sentences ("specifics., but no source addresses this,",
  "(no evidence supports Bolivia)"), which cost structure (5.7 to 4.5).
- **quality-1 after round 2** fixed all three: no empty answers, structure back to 5.2. The ledger of that
  run shows the final writer's prompt at 5,365 tokens with 2,054 out on q01 (measured), i.e. 7.4K of 8.2K.
- **What got worse and stayed worse**: depth (3.9 to 3.4) and fit (4.2 to 3.8). One gpt-oss draft is thinner
  than three; the grounding rules make it say "no source gives a price" rather than reach (q06 eggs and q09
  cost of living answered with no current figure, though the data exists); follow-ups fell to 1.7.

## The five biggest remaining failures (quality-1)

1. **Follow-ups (q01.t2, q17.t2: both 1).** No search runs on "explain more" or "pick one and price it". q01.t2
   still never says it can't see Claude's reply and invents what Claude said; on q17.t2 the critic suggested
   "Bela Vista" (a São Paulo district) and the final writer rewrote the Florianópolis answer around São Paulo.
   Cause: the follow-up path doesn't ground (6b514 left it as it was), and the critic's notes steer.
2. **The fact exists but search doesn't reach it (q06 eggs: 1; q20 cities: 2; q03 DigitalOcean: 2; q05 latest
   models: 2).** BLS/FRED, the EIU index, DigitalOcean's GPU pricing page and the vendors' launch posts never
   came back from the two engines that answer from this network (bing and ddgs's "auto"); aggregators and
   listicles did. The planner's model angles never landed: all 28 logged plans in each of the three grounded runs have
   `model=[]` (*estimated* cause: the 5 s plan deadline passes while the server loads the writer).
3. **The reviewer is right and the writer ignores it (q01, q03, q05, q08, q11).** In five judged answers the
   critic flagged the exact error ("Ironwood is not 5th-gen", "$2.50/hr unsupported", "Opus 4.7 not
   confirmed as latest") and gpt-oss kept it. Cause: the final writer is the same model that wrote the draft,
   reading notes in a long prompt.
4. **Stale knowledge still fills every gap (q01 Claude 3.5 Sonnet, q05 Opus 4.7, q24 the 2024 estate
   exemption).** When the evidence is thin, the 20B model's 2024 memory is what's left; the brief's "say what
   is uncertain" turns some of it into hedges, not into current facts.
5. **Thin answers for the person (depth 3.4, fit 3.8).** Answers lead with a verdict but miss the angle that
   matters (q29: no prices in reais, no 90-day rule; q30: no "bigger 4-bit beats smaller 16-bit"), five of six
   health answers still end with the recap the persona forbids, and tables and headings still appear on short
   answers.

## What to fix next, ranked

1. **Search that reaches primary sources.** Biggest lever: fresh 2.8, travel 2.6, and most "it said no data"
   answers trace to it. Options, cheapest first: per-category site hints in `plan_queries` (bls.gov/FRED for
   prices, federalreserve.gov, the vendor's own newsroom for "latest", eiu.com for rankings, the airline's
   own page for baggage); let the model's plan arrive (start it before the writer's load, or give the rules'
   queries a second round once the plan lands); then a search API with freshness and site filters. A paid API
   is Patrick's call, with its cost shown against its use.
2. **Ground the follow-ups**, carrying the earlier turn's topic into the plan, and add one rule to the brief:
   another assistant's reply the person mentions is not visible; say so and don't describe it.
3. **Make the critic's WRONG notes stick**: hand the notes to the fact-check as claims to check (it already
   rewrites sentences in place), or let the critic's model write the final when the writer is also the
   drafter; and drop MISSING notes that name a place or entity not in the question or the evidence.
4. **Win back depth without the invention**: give the writer the evidence's numbers explicitly ("use these
   figures"), try gpt-oss at a higher reasoning effort for the final only (needs the capability probe noted in
   6b515), and measure a two-draft Thinking (writer plus a second draft from ministral) against one writer
   plus critic on this bench.
5. **Skip search for pure arithmetic and self-contained reasoning** (q27, q28): it costs time and lets the
   merge cite irrelevant pages (search-ground's reasoning 4.7).
6. **Let the server kit report the card's size** (`vram_bytes` is null on Ollama1), so a too-big model is
   never tried; and decide whether `qwen3.8:27b` (doesn't fit a 16 GB card) should stay on Ollama1.
7. **Time**: the round-2 context costs ~10 s; ask for 12K rather than 16K when the prompt allows, and show the
   fact-check's correction as an edit rather than a full re-send.

## Reproduce

```bash
L=<scratchpad>/server-gpu.lock; until mkdir "$L"; do sleep 30; done; echo quality1-bench > "$L/owner"
"$VENV" tools/answer_eval/run_bench.py --config server-think --name q1b-server-think --repo . --port 9893 --no-lock --warmup
rm -rf "$L"
python3 tools/answer_eval/judge_packet.py q1b-server-think --batch 6 --label opus
# judges: RUBRIC.md's prompt + batches/q1b-server-think/opus-N.md -> judgments/q1b-server-think/opus-N.jsonl
python3 tools/answer_eval/score.py baseline-server-think-opus sg-server-think ac-server-think q1-server-think q1b-server-think
python3 tools/answer_eval/stats.py q1b-server-think
```
