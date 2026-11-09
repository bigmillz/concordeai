# Answer bench: fresh-1 against main (2026-10-25)

The short version: on this Mac's Fast tier (Gemma 4 26B on MLX), **fresh facts went from 4.2 to 4.8** out of 10
(three runs each, paired: **+0.56**), and **high-severity hallucinations fell from 40 to 17** over the same 39
answers. Travel didn't move (3.6 → 3.5, −0.13, inside noise). Over all 13 turns the paired change is
**+0.28 (standard error 0.18)**. Every fix is free: no paid search API, no cloud keys. Every number below is
**measured**; the one estimate is marked *.

## What was measured

- **Config**: `run_bench.py --config local-fast --warmup`, port 9896, a windowless dev copy with no cloud keys
  and no server (no `servers.json`), tier Fast = **Gemma 4 26B** (MLX) on every turn of every run. The server
  (Ollama1) is down and wasn't used. The fresh and travel questions only: q01–q06 and q17–q21 with their
  follow-ups (q01.t2, q17.t2), 13 turns. `as_of` stays 2026-10-24.
- **Code**: base = `origin/main` 672d6f1 (quality-1 + quality-2), extracted with `git archive`. Branch =
  `fresh-1` 349015d. Each run held `engines.lock` (owner `fresh-1`) for its whole length.
- **Grading**: Opus judge subagents with RUBRIC.md's prompt and no cloud keys, **blind and by turn**. Each judge
  got every run's answer to ONE turn, with the run names replaced by shuffled codes
  (`scratchpad/fresh1/blind.py`). That way one judge grades the same reference sheet consistently across the
  builds.
- **Runs** (`runs/<name>/`, `judgments/<name>/`):

  | run | code | what differs |
  |---|---|---|
  | `f1-base-a`, `-b`, `-c` | 672d6f1 | main, three samples |
  | `f1-br-a`, `-b`, `-c`, `-d` | 349015d | all four fixes |
  | `f1-ab-search` | 349015d, `MILLENAI_FRESH_OFF=tl,news` | no date filter, no news search |
  | `f1-ab-data` | `…=official,vendor` | no official data, no vendor feeds |
  | `f1-ab-vendor`, `-vendor-b` | `…=vendor` | no vendor feeds |
  | `f1-ab-write` | `…=dating,snippet` | no dating rules, no snippet-only marks |

  Wave 1 graded base a/b/c and branch a/b/c together. Wave 2 graded the four ablations with `f1-br-a` and
  `f1-base-a` graded again as anchors (`*-w2`). Wave 3 graded vendor-off ×2 against branch ×2 (`f1-br-a-w3`,
  `f1-br-d`).

### How much to trust a difference

- **Judge noise** (the same answers graded twice, in different waves): `f1-br-a` +0.15 (se 0.15) and +0.08
  (se 0.18); `f1-base-a` −0.08 (se 0.18). Two judges agree on 8 or 9 of 13 turns.
- **Run noise** (the same code, two samples, graded together): base vs base +0.08, 0.00 and −0.08; branch vs
  branch −0.38, −0.08 and +0.31. Each comparison has a standard error of 0.30–0.51. **One run against one run
  can't separate anything smaller than about 0.5.**
- So the main comparison is three runs against three, paired by turn, and every ablation is one run (two for
  the vendor feeds).

## Scores

### Wave 1: three base runs against three branch runs (overall, 1–10)

| run | fresh (6) | travel (5) | follow-ups (2) | all (13) | correct | grounded | depth | fit | high flags |
|---|---|---|---|---|---|---|---|---|---|
| f1-base-a | 4.17 | 3.80 | 2.00 | 3.69 | 3.9 | 5.0 | 3.8 | 4.2 | 15 |
| f1-base-b | 4.33 | 3.40 | 3.00 | 3.77 | 4.2 | 5.3 | 3.8 | 4.4 | 15 |
| f1-base-c | 4.17 | 3.60 | 2.50 | 3.69 | 4.2 | 5.5 | 3.7 | 4.3 | 10 |
| **base, mean** | **4.22** | **3.60** | **2.50** | **3.72** | 4.1 | 5.3 | 3.8 | 4.3 | **40** |
| f1-br-a | 4.67 | 3.80 | 3.50 | 4.15 | 4.8 | 5.6 | 4.0 | 4.4 | 4 |
| f1-br-b | 4.83 | 3.00 | 2.50 | 3.77 | 4.2 | 5.5 | 3.7 | 4.3 | 7 |
| f1-br-c | 4.83 | 3.60 | 3.00 | 4.08 | 4.7 | 5.7 | 4.1 | 4.3 | 6 |
| **branch, mean** | **4.78** | **3.47** | **3.00** | **4.00** | 4.6 | 5.6 | 3.9 | 4.3 | **17** |

Paired over the 13 turns (the mean of three against the mean of three):

| | mean overall change | std. error | better / worse / same | high flags per answer |
|---|---|---|---|---|
| all 13 turns | **+0.28** | 0.18 | 7 / 3 / 3 | **−0.59** |
| fresh (6) | **+0.56** | | | |
| travel (5) | −0.13 | | | |
| follow-ups (2) | +0.50 | | | |

Per turn, base runs / branch runs: q01 2,4,3 / 3,2,5 · q01.t2 2,3,2 / 3,2,4 · q02 4,2,3 / 4,3,4 ·
q03 2,2,3 / 2,3,2 · q04 6,7,8 / 7,8,6 · q05 4,5,2 / 5,5,5 · q06 7,6,6 / 7,8,7 · q17 5,3,5 / 6,4,2 ·
q17.t2 2,3,3 / 4,3,2 · q18 4,3,4 / 3,2,4 · q19 3,2,3 / 2,4,5 · q20 3,6,3 / 6,2,4 · q21 4,3,3 / 2,3,3.

`f1-br-d` (wave 3) scored fresh 5.33, travel 3.40, all 4.15. Over all four branch runs fresh averages
**4.92** and travel **3.45**.

### Answers now date their figures

"as of" in the 13 answers: base 0, 3 and 2; branch 22, 17 and 49; with the dating rules off (`f1-ab-write`) 1.

### Timing

First visible text has a median of 18–19 s on base and 18–20 s on the branch; wall time has a mean of 24–25 s on
base and 26–27 s on the branch. The official fetches and the feeds run beside the search, inside its budget.
They add about 1 s, from the extra news query and the joins.

## The four ideas, one by one

### 1. Date-restricted search: KEPT (measured −0.62 when removed, one run)

- **What**: on a question about now, the first engine's queries (bing via ddgs) carry ddgs's `timelimit="y"`.
  The second engine stays unfiltered, so a long-lived reference page still comes back. One news search
  (`DDGS().news(..., timelimit="m")`) runs on the lead query, and its rows carry their own date, which
  `_src_date` now prefers.
- **Measured from here**:
  - ddgs's DuckDuckGo backend returns nothing from this network.
  - Bing honours the filter on some queries. For "Fed interest rate decision" it turned
    tradingeconomics and polymarket into CNBC's "Fed rate decision September 2026" and the Fed's calendar.
  - bing news answers in 0.3–1.7 s with dated rows.
- **Evidence level** (no models; `scratchpad/fresh1/evh.py`, the share of sources dated within 12 months):
  - branch 70%
  - without the news search 65%
  - without the date filter 73% (the filter alone doesn't show at this level)
  - base 66%
- **Answers**: `f1-ab-search` scored −0.62 (se 0.45) against `f1-br-a` graded in the same wave (fresh −0.33,
  travel −1.20). That is one run, so it is suggestive only.

### 2. Free official data with no key: KEPT

Measured from this Mac on 2026-10-25, with the reader's browser User-Agent:

| endpoint | answers? | used for |
|---|---|---|
| BLS public API v1 (`api.bls.gov/publicAPI/v1/timeseries/data/`) | **yes**, 1.0 s (bls.gov pages and its RSS 403) | 14 average-price series (eggs, milk, bread, beef, chicken, coffee, bananas, butter, sugar, rice, gasoline, diesel, electricity, piped gas) + CPI-U headline and core; one POST for all 16 series, kept 12 h (the keyless API allows 25 queries a day) |
| Fed RSS `federalreserve.gov/feeds/press_monetary.xml` + the statement page + `fomccalendars.htm` | **yes**, 0.3–0.6 s | the latest FOMC decision sentence and vote, and the next meeting |
| New York Fed `markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json` | **yes**, 0.4 s | effective fed funds rate and target range, dated |
| Frankfurter `api.frankfurter.app/latest` (ECB reference rates) | **yes**, 1.0 s | USD against BRL, EUR, GBP, MXN, JPY, CAD, AUD, CHF, CNY, INR, THB, KRW |
| ECB `data-api.ecb.europa.eu` / `eurofxref-daily.xml` | yes | (Frankfurter serves the same rates, one call) |
| Treasury yield-curve XML (`home.treasury.gov/…/pages/xml`) | **yes**, 1.3 s | 3-month, 2-, 5-, 10- and 30-year par yields, dated |
| Treasury FiscalData API | yes | not used |
| Wikipedia REST summary + search API | yes (one 429 under a burst) | **not used**: "newest iPhone" searches to the iPhone 4 article; the summaries are leads, dated by their last edit, not the fact |
| FRED `fredgraph.csv` and `/data/*.txt` | **no**: timed out (12 s) 4 of 4 | dropped |
| EIA gas page | yes (HTML) | not needed: BLS carries gasoline |
| Open-Meteo | yes | already used for weather |

- **What**: `official_kinds()` picks the blocks by rules. A US price of a listed item gets BLS, but not for a
  price abroad or "how much protein in chicken". Inflation gets CPI; the Fed or the FOMC gets the Fed blocks; a
  currency word gets FX; Treasury yields get the curve. Each block is a short, dated source, seated first and
  marked "OFFICIAL DATA".
- **Evidence level** (key facts from the reference sheets found in the pack, 50 in all):
  - base 27.0
  - branch 30.5
  - **branch with official data off 27.0**, the whole gain
  - q04 2 → 4 of 4 (the decision, vote and next meeting); q06 2 → 3 of 3; q17.t2 2 → 3 of 3 (the exchange rate)
- **Answers**: with official and vendor data off (`f1-ab-data`), −0.15 (se 0.52), inside one run's noise.
  q04 and q06 were already 6–8 on base, because the search found nowflation and the FOMC page from here. The
  block makes them reliable rather than better on average. Kept for the evidence-level gain, at about zero
  cost.

### 3. Blocked pages: KEPT as snippet-only marks + vendor feeds; AMP and Wayback DROPPED

- **Snippet-only**: a source whose page wasn't read (refused, timed out, or past the read budget) is marked
  "SNIPPET ONLY". The writer must hedge a figure from it ("reportedly", "per <site>"). That is 1.9–2.1 sources
  per pack.
- **Vendor feeds** (the publisher's own RSS): OpenAI's RSS answers although every openai.com article 403s.
  Apple's, NVIDIA's, Google's, Google Cloud's, AMD's, AWS's and Microsoft's feeds and Anthropic's news page
  answer too. A question about now that names the vendor or its product gets the feed's items on it as one
  dated source. A product the question names must be in the item ("ChatGPT model" counts GPT-n). On a
  "latest" question, launches lead. A feed with nothing on the question is left out: "What is Claude using"
  for chips doesn't list Claude's models.
  - Measured with vendor feeds off: −0.08 (one run, wave 2), then **−0.23 (se 0.32) over two runs against two**
    (wave 3; fresh −0.42, high flags +0.23 per answer).
  - Inside noise both times but the same direction. Kept.
- **AMP**: `/amp` and `?amp=1` on the refused pages gave the same 403, 503 or timeout. Dropped.
- **Wayback Machine availability API**: 429 from this network on every call. Dropped.

### 4. Dating every figure: KEPT (measured −0.92 when removed, one run, with the snippet marks)

- **What**: the rules make the writer date every current figure "as of <month year>" from that source's own
  date line. The newer of two disagreeing sources wins, and official data beats an article. The key-figure
  lines carry "(source dated …)". On a question about now with no source dated within 12 months, the turn adds
  a "NOTHING RECENT" line naming the newest date.
- **Answers**: `f1-ab-write` (dating and snippet marks off) scored **−0.92 (se 0.46)** against `f1-br-a` in the
  same wave (travel −1.80), the largest single effect measured. "As of" went from 17–49 per run to 1.

## Where it still fails

1. **Travel (3.5)** is unchanged.
   - q18 and q19 want judgement and typical ranges, not fresh facts: none of the six runs picks one city or
     gives a two-week total.
   - q21's airline pages refuse or time out (voeazul 403, latamairlines timeout), so the 10 kg / 55×35×25 cm
     rule only arrives through blogs, sometimes as "8–10 kg".
2. **Turn 1 of the GPU question (q01) is still 2–5.** The packs are listicles. The NVIDIA feed item (Vera Rubin
   GA) lands only when an item matches the question's words. The 26B writer still says Claude "runs mainly on
   TPUs".
3. **DigitalOcean (q03) is still 2–3.** The docs pricing page reads fine, but the search seats a third-party
   "$2.50/hr" page above it.
4. **The writer refuses to estimate.** q17.t2 had the rate (R$5.0465) and still said "the sources do not
   list" a rent in two of three runs.
5. **BLS allows 25 keyless queries a day per address.** One question is one query (16 series at once), kept
   12 h, so a single user stays far under. A busy shared address would not*.

\* An estimate: it assumes a home connection, which no measurement here covers.

## Reproduce

```bash
L=<scratchpad>/engines.lock; until mkdir "$L"; do sleep 30; done; echo fresh-1 > "$L/owner"
"$VENV" tools/answer_eval/run_bench.py --config local-fast --name f1-br-a --repo . --port 9896 --no-lock --redo \
  --warmup --only q01,q02,q03,q04,q05,q06,q17,q18,q19,q20,q21
# an ablation: --env MILLENAI_FRESH_OFF=tl,news   (the base: --repo <a git archive of 672d6f1>)
rm -rf "$L"
python3 tools/answer_eval/blind_batches.py make f1-base-a f1-base-b f1-base-c f1-br-a f1-br-b f1-br-c
# judges: Opus subagents, RUBRIC.md's prompt, blind/wave/<turn>.md -> blind/wave/out-<turn>.jsonl
python3 tools/answer_eval/blind_batches.py split      # -> judgments/<run>/opus-blind.jsonl
python3 tools/answer_eval/paired.py f1-base-a,f1-base-b,f1-base-c f1-br-a,f1-br-b,f1-br-c
```

The evidence-level harness (no models: it imports `millenai.py`, runs the grounded search on the 13 turns and
looks for the reference sheets' key facts in the pack) stayed in the session scratchpad (`fresh1/evh.py`).
