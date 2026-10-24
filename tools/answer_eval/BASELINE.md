# Answer bench — baseline, 2026-10-24

The short version: on the owner's own setup (Thinking on Ollama1 alone) the app scores **3.7 / 10 overall** across 28 graded turns, **3.9 on "correct and current"** and **3.3 on "grounded"**, with **96 high-severity hallucinations** — about 3.4 invented or wrong specifics per answer. The only category that works is arithmetic (6.3). Fresh-fact questions score 2.0 and follow-ups 2.3. The local Thinking tier on this Mac did no better and failed outright on half its questions. The "AI hardware leaders" failure reproduced exactly: the same four junk sources, the same stale H200 answer, this time with an invented "Anthropic Infernet" chip.

## Setup

- Branch `evalbench` from `origin/main` at a99681a (the build-277 code); no app code changed.
- Bench date for every reference sheet: 2026-10-24 (`questions.json`: 30 questions, 33 turns, each with the key facts, the URLs checked, and the traps).
- Dev copies per CLAUDE.md (`MILLENAI_DEV=1`, own `MILLENAI_HOME`, windowless, the app venv's Python) driven by `run_bench.py` with the same `POST /api/chat` body the page sends; `persona.json` (the owner's standing preferences, paraphrased) posted to `/api/prefs` first.
- **server-think**: tier `srv:<Ollama1>:think` — the server alone, as in the owner's chat. Council every time: `gpt-oss:20b`, `ministral-3:14b`, `gemma4:12b`; the merge and the "double-checking" reflection pass ran on the server (compositor `gpt-oss:20b` 20x, `ministral-3:14b` 8x).
- **local-think**: tier `Thinking` on this M4 Pro (48 GB). The tier wanted `Gemma 4 26B`, `Qwen 3.8 27B`, `Qwen 3.6 35B MoE` with Gemma 4 26B composing.
- Grading: Fable 5.1 judge subagents, five to seven packets each, strictly per `RUBRIC.md`; no cloud API keys of the owner's were used. Judgments are in `judgments/<run>/`, one JSON object per answer, reproducible with `judge_packet.py` + the prompt in `RUBRIC.md`.

## Caveats that shaped the numbers

- **Server rows q03–q07 are excluded (pending re-run).** The coordinator's llama-bench job overlapped the server at 01:16:58–01:19:00 and unloaded the resident models; per the correction, those five rows were discarded and queued for a re-run, which is waiting for the server-GPU lock. The server table below is 28 of 33 turns.
- **The local run shared this Mac with three other dev copies and their engines.** Memory pressure made `Qwen 3.8 27B` miss the 120-second per-model cap on every load (18 of 18 attempts) and the 26B/35B seats were refused for "low memory" 17 times, so the tier either errored or silently seated `Ministral 3 14B`, `Gemma 4 12B` and even `Gemma 4 E2B`. The mechanism is the app's (see cause 7), but the error rate is inflated by the contention. The local table is partial and will be finished when the pass completes; a resume pass re-asks the error rows.
- The judge grades the raw stream text as the page would show it (the `[[PLACES]]` trailer stripped). One answer (q18) was graded twice for that reason; the second grade stands.

## Scores

### server-think (Ollama1 alone, Thinking) — 28 turns

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low |
|---|---|---|---|---|---|---|---|---|---|
| fresh (q01, q02; q03–q06 pending) | 2 | 1.5 | 1.0 | 2.5 | 4.0 | 4.0 | 4.5 | **2.0** | 18/5/2 |
| compare (q08–q10; q07 pending) | 3 | 3.7 | 2.7 | 4.0 | 4.3 | 5.0 | 4.3 | **3.7** | 13/15/3 |
| health | 6 | 5.0 | 4.3 | 4.5 | 5.2 | 5.8 | 5.0 | **4.3** | 8/11/10 |
| travel | 5 | 3.0 | 2.8 | 3.6 | 3.8 | 5.0 | 4.0 | **3.2** | 21/12/3 |
| money | 5 | 3.2 | 2.2 | 4.2 | 4.6 | 5.6 | 5.6 | **3.2** | 17/12/7 |
| reasoning | 3 | 7.0 | 6.7 | 6.0 | 6.7 | 6.3 | 6.7 | **6.3** | 3/3/1 |
| technical | 1 | 6.0 | 6.0 | 5.0 | 6.0 | 8.0 | 6.0 | **5.0** | 1/1/1 |
| follow-up turns | 3 | 2.3 | 2.0 | 2.7 | 3.7 | 4.7 | 4.0 | **2.3** | 15/10/3 |
| **all** | 28 | 3.9 | 3.3 | 4.1 | 4.7 | 5.4 | 4.9 | **3.7** | **96/69/30** |

Timing (`stats.py`): wall time mean 86 s, median 83 s, max 156 s; **first visible text median 79 s** (nothing streams while three drafts, a reflection pass and the merge run); answers mean 1,925 characters; the app's meter averages 5.8 tok/s. 24 of 28 turns searched, always exactly 4 snippet sources.

Best answers: q27 package-vs-separate arithmetic (8), q15 buzz-or-shave (6), q28 hybrid break-even (6). Everything that needed a current fact or a number from the world scored 2–4.

### local-think (this Mac, Thinking) — partial, 5 graded of 20 recorded

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| compare | 1 | 6.0 | 5.0 | 4.0 | 5.0 | 5.0 | 4.0 | **5.0** | 0/2/1 | 0 |
| health | 3 | 5.0 | 4.7 | 3.7 | 3.7 | 4.7 | 4.3 | **4.0** | 2/2/9 | 0 |
| follow-up | 1 | 1 | 1 | 1 | 1 | 1 | 1 | **1.0** | 0/0/0 | 1 |
| **graded so far** | 5 | 4.4 | 4.0 | 3.2 | 3.4 | 4.0 | 3.6 | **3.6** | 2/4/10 | 1 |

Of the 20 turns recorded so far, **9 are error answers** ("⚠️ Backend error — RuntimeError: none of the selected models answered"), which score 1 on everything once graded. Usable answers took a mean of 224 s with first visible text at 217 s (meter 2.6 tok/s — the owner's "2.5 tok/s"). Where it answered, local invented less (Gemma composes more conservatively than the server's 14B/20B) but had less depth: single-draft answers shipped with no merge, two with garbled tokens ("El Poblាប់", "electingtes").

## The five worst answers (server-think)

**1. q01.t2 — "I feel like Claude's response beat you. explain more" — overall 1, 15 flags (9 high).** No search ran on the follow-up. The three drafts were asked to explain a comparison none of them could see; gpt-oss invented an "Anthropic Infernet" ASIC with "≈10% lower latency", gemma wrote about RLHF prose style, and the compositor (`ministral-3:14b`) fused them into a 4,365-character essay with a table: "InferNet … reducing cache misses by **~40%**", "fabric reduces memory traffic by **~60%**", "a **30% latency drop** for 32K-token prompts", "H200 ships to early adopters (5,000+ by mid-2026)". It never says it can't see Claude's reply and never admits the first answer was wrong. The owner's real chat did the same thing with a different model ("Claude correctly identified the Rubin and TPU v8 architectures" — a fabricated account of what Claude said).

**2. q01 — the GPU question — overall 2, 11 flags (8 high).** The query was the raw question, so DuckDuckGo returned the identical four junk pages the owner saw (claude.ai sign-in, veoaifree.com, a Pluralsight "what is Claude" explainer, a YouTube how-to). All three drafts came from training memory: gpt-oss "The Nvidia H200 remains the most powerful GPU… March 2024 press release", ministral "the latest in the pipeline is the NVIDIA BH200, announced for 2027", gemma "Blackwell B200… TPU v5p". The merge (`ministral-3:14b`) chose the stalest draft and improved on it: "**the NVIDIA H200 (Blackwell architecture)** … **up to 1,500 TFLOPS of FP8** … 16-chip unit", "Claude 3.5 Sonnet (Anthropic's latest) runs on … **the Anthropic Infernet**, a TPU-like chip optimized for sparse attention". Blackwell Ultra, Vera Rubin, Ironwood, Trainium, the TPU deal: absent.

**3. q02 — newest iPhone — overall 2, 14 flags (10 high).** Here the search worked (apple.com/iphone, WIRED's guide, Macworld's "Pro Max, Duo ranked") and two of three drafts were right ("the iPhone 18 Pro and 18 Pro Max are the newest flagship models as of October 2026"). The compositor (`ministral-3:14b`) kept its own wrong draft — "The newest iPhone you can buy right now is the **iPhone 17 Pro Max** (Macworld, **Oct 22, 2026**)… No iPhone 18 or iPhone Duo is in retail stores yet" — then invented a price table ("iPhone 17 Pro $999"), "Trade-in credit for an iPhone 14 Pro is $600 max (Apple, Oct 1, 2026)", "the A17 Pro chip in the 17 Pro Max", "wait for the iPhone 18 (expected late 2027)", and inverted the verdict. The citations are the other drafts' sources re-attached to claims they never made.

**4. q20 — most liveable cities — overall 2, 7 flags (6 high).** Search returned Numbeo, Business Insider, IQAir and WorldPopulationReview pages, never the EIU index. gpt-oss's own draft collapsed to 31 characters, so the gpt-oss compositor built on ministral's draft, which fabricated "**Mercer Quality of Living City Rankings 2026 mid-year** … still puts Vienna at the top" (Mercer's latest is 2024; the EIU 2026 index has Copenhagen first), a non-existent "Oxford Economics Quality of Life Index 2026 … elevates Tokyo to the lead", and "PM2.5 level is 7 µg/m³ (IQAir, May 2026), far below the 18 µg/m³ recorded in New York". Gemma's draft had Mercer dated correctly and named Copenhagen; it was discarded.

**5. q26 — Amazon in South America — overall 2, 10 flags (5 high).** Sources: Wikipedia's Amazon page, an AmazonGlobal help page, a Prime Now shopper video, a NYT labour podcast. The drafts disagreed three ways (Brazil+Chile+Colombia / Brazil+Colombia+Mexico / "no local fulfilment anywhere"); the compositor (`gpt-oss:20b`) kept its own, the most wrong: "Amazon runs a full-service marketplace only in **Brazil, Chile, and Colombia** … In Chile the site launched in 2021 … In Colombia Amazon.com.co opened in 2018", plus "three fulfillment centers—São Paulo, Manaus, and Rio" and "Prime members pay about $30 per month" (Brazil has a dozen-plus FCs; Prime is R$19.90). No Amazon store exists in Chile or Colombia; the 2023 plans were shelved.

Honourable mention: **q04 (Fed funds rate)**, graded in the pending set but worth a line because all three drafts were right (3.75–4.00%, +25 bp in September, 12–0) and the merge still added "the rate was cut from 5.25%-5.50%" in July 2023, "held steady at ~3.25%-3.50% for most of 2026", "~70% odds of another 25 bps hike at the November 2026 meeting (CME FedWatch, Oct 23, 2026)", "The Fed's next statement drops Sunday, October 26" and "next FOMC meeting … November 1-2" (it is October 27–28). Correct drafts in, five fabrications out.

## Systemic causes, ranked by impact

Attribution comes from the judges' `what_went_wrong` lines (28 answers): search is named in 28, the compositor/merge in 23, stale model knowledge in 15.

**1. The merge is the main error amplifier, and the weakest model often holds the pen.** In 23 of 28 answers the judge traced wrong material to the compositor. It picks its own draft over correct peers (q02: two right drafts, wrong answer; q20 and q26: the only correct-direction draft discarded), it "resolves" disagreements by inventing (q04: clean drafts, five fabrications), and it adds dated citations the data never contained — 24 flagged attributions such as "(Macworld, Oct 22, 2026)", "(CME FedWatch, Oct 23, 2026)", "Numbeo 2026 (June)", "a 2023 meta-analysis (12 trials, 1,200 participants)", "arXiv 2511.05502, June 2026", "a 2024 Gartner survey and a 2023 Deloitte study", "(BLS, 2026) — on all five lines". Two prompt rules collide to produce this: the web block demands "A CURRENT PRICE… comes from the data WITH its date and source in the same sentence" while the data is four snippets, and `SYNTH_INSTRUCTION` says "If the drafts disagree on a fact, state the correct information plainly" — a 14B picks confidently and wrongly. `server_compositor`'s resident-first rule handed the pen to `ministral-3:14b` (within 70% of gpt-oss's size, already loaded) on 8 of 28 answers, including 6 of the first 11; those eight average overall 2.5 against 4.2 for the gpt-oss merges. The "double-checking the drafts" reflection runs on the same model, so it checks nothing. The merge also leaked machinery twice ("The $1,300/month claim (Answer 2) is overblown"; a raw `[[PLACES]]` trailer).

**2. Search is the raw question, four snippets, no page read, no filter — so it almost never brings the fact.** `query = strip_greeting(prompt)` goes to DuckDuckGo as typed and `run_search` keeps the top four titles and snippets. Results: for the GPU question, claude.ai and a free-Claude spam site; for DigitalOcean pricing, flow.google.com and rent.com; for Airbnb pricing, a Fallout 4 video and a Brawl Stars APK site; for jobs, Pinterest and a "CEO day in my life" video; for the Fed, four real pages but only their titles. The judges found no usable primary source in roughly 23 of 24 searched answers, and where one did come back (apple.com for q02, the law-firm guides for q24, carrysizer for q21) the merge ignored its figures. Search also fires on pure arithmetic (q27 spent 156 s, q28 79 s, both on flight-booking home pages). Every number the owner saw therefore came from model memory.

**3. Stale model knowledge is presented as current with no date awareness.** The system prompt pins today's date, yet the models' priors win whenever search is empty: "Claude 3.5 Sonnet (Anthropic's latest)", "Claude 3.7 Sonnet … OpenAI's latest flagship is the o1 series" (local q05), "the 2026 exemption of $12.92 million" (2023's), "Mercer 2026 … Vienna", "LATAM – one cabin bag max 12 kg", "Ansible Tower", "Ollama … llama.cpp is the engine". Nothing tells the model "your knowledge stops in 2024–25; when the search didn't return it, say so and date what you know", so a 2024 fact ships as "right now". Fresh questions scored 2.0; three of the five worst are stale facts dressed as current.

**4. Follow-ups don't search and inherit the previous turn's fictions.** The follow-up path searches only when the message "leans on the last turn" with a place-like entity; "explain more" and "pick one and price it" ran with no sources (q01.t2, q17.t2, q11.t2 all `searched=False`). The models then elaborate the first answer's inventions (Infernet grew precise percentages) or price from priors ("R$6,200 … includes electricity, water, gas"). Follow-up turns averaged 2.3.

**5. The persona and shape rules are half-honoured.** The asker's standing preferences say direct, no recap, one best option, short; `length` is 4/5. Answers lead with a verdict but then run 2–2.5x the sheet's length, end with a "Bottom line" paragraph in most batches, list four unranked options when one was asked for (q17, q19), and use tables and `###` headings on chat questions (q02, q09, q22) despite the system prompt's shape law. `fit` averaged 4.7 and `concise` 4.9 even where the facts were right.

**6. Nothing streams for a minute or more, and the meter makes it look worse.** The pipeline is drafts → reflection → merge, all before the first visible character: median 79 s to first text on the server, 217 s locally (the owner's 127 s and 2.5 tok/s are typical). A 20B model drafting at 30–50 tok/s shows as 5.8 tok/s because the meter divides the final answer by the whole wait.

**7. Local Thinking collapses under memory pressure instead of degrading.** (Local only.) `resolve_tier` seats what fits at request time, but a 17–20 GB load under pressure takes longer than `LOCAL_CAP` (120 s, which includes the load), so `Qwen 3.8 27B` timed out on all 18 attempts and the 26B/35B seats were refused "low memory" 17 times. When every seat fails the answer is "⚠️ Backend error — RuntimeError: none of the selected models answered" (9 of 20 turns) rather than a fallback to the smaller model that would have answered; when some fail, the tier silently seats `Ministral 3 14B`, `Gemma 4 12B` or `Gemma 4 E2B` and ships a single draft with no merge — the user sees "Thinking" either way. The contention here came from other dev copies, but the owner hits the same cliff with a browser and the app open.

## What I would have answered

The same persona, the same date, from the sources in the reference sheets. Each is the shape the rubric rewards: dated, verdict first, no invented specific.

**q01.** As of October 2026: the biggest accelerator you can buy in volume is Nvidia's **Blackwell Ultra (B300 / GB300 NVL72)** — 288 GB HBM3e per GPU, about 1.5x a GB200 rack. The next step, **Vera Rubin**, is in full production and the first racks ship this fall (Google Cloud's A5X on Vera Rubin NVL72 goes GA in November; Nvidia showed containerised Rubin pods on 21 Oct for early 2027). Google's current TPU is **Ironwood (v7)**, GA since April — 4.6 PFLOPS FP8 and 192 GB per chip in 9,216-chip pods — with the eighth generation (TPU 8t training, 8i inference) announced in April but not yet broadly available. AMD's **MI450 "Helios"** racks start shipping in H2 2026 (OpenAI, Meta, Azure, Oracle). Chip vs rack matters: a Rubin NVL72 rack is the most powerful thing; a B300 is the most powerful GPU you can get today. Claude runs on three families, per Anthropic: **AWS Trainium** (over a million Trainium2 chips; Amazon is the primary training partner; Trainium3 later this year), **Google TPUs** (the October 2025 deal for up to a million TPUs, more than a gigawatt online in 2026) and **Nvidia GPUs** through cloud partners; Anthropic started an in-house chip team in August. OpenAI: mostly Nvidia via Microsoft, Oracle and CoreWeave, adding AMD MI450 from H2 2026 and Broadcom custom silicon. Google: its own TPUs for Gemini. Meta: Nvidia plus AMD and its MTIA. xAI: Nvidia.

**q01.t2.** I can't see Claude's reply, so I can't compare — but I can tell you where mine was wrong. The H200, TPU v5p and MI300X are 2023 parts, two generations back; "Infernet" doesn't exist; and Claude 3.5 Sonnet is a 2024 model. The search returned pages about Claude the product, not chips, so I answered from memory that stops before 2025. The current picture is Blackwell Ultra shipping now, Vera Rubin arriving this fall, Ironwood GA with TPU 8 announced, MI450 in H2 2026, and Anthropic on Trainium, TPUs and Nvidia GPUs.

**q02.** The **iPhone 18 Pro ($1,199, 256 GB) and 18 Pro Max ($1,299)**, announced 9 September 2026, plus the folding **iPhone Duo at $1,999**. Both Pros are $100 up on last year because of the memory shortage, and there's no base iPhone 18 yet — Apple split the line and the non-Pro model comes in spring 2027; the iPhone 17 stays at $899. From a 14 the jump is real (chip, cameras, battery, screen), but the sane moves are the $899 iPhone 17 or waiting for the spring iPhone 18, unless you want the Pro cameras. Your 14 still gets iOS updates for a couple of years.

**q20.** The EIU Global Liveability Index 2026 (June, 173 cities): **Copenhagen** first for the second year (98/100), then Vienna, Melbourne, Sydney, Zurich, Geneva, Osaka, Adelaide, Vancouver, Tokyo; Honolulu is the top US city at 25th. Mercer's Quality of Living ranking hasn't had a new edition since 2024 (Zurich, Vienna, Geneva). Monocle, Numbeo and InterNations measure lifestyle, crowd-sourced prices and expat satisfaction respectively, and Oxford Economics' Global Cities Index is economics-weighted (New York first). None of them measures where you'd be happy, and all of them ignore cost of living.

**q26.** Only **Brazil**: amazon.com.br (books from 2012, full retail from 2017, FBA since 2020, Prime, a dozen-plus fulfilment centres). The 2022 leak of Colombia and Chile marketplaces for 2023 never happened — only Prime Video channels launched there, and an AWS region is due in Chile by end-2026 — so there is no amazon.cl or amazon.com.co store. Elsewhere you can order from amazon.com with international shipping and import duties, slowly. What people use: **Mercado Libre** everywhere (about $50 billion GMV in 2025, its own logistics and Mercado Pago), plus Shopee, Magalu, Casas Bahia and Shein/Temu in Brazil (20% tax on sub-$50 parcels since 2024), Falabella and Linio in Chile, Peru and Colombia.

**q04 (the merge-made-it-worse case).** 3.75%–4.00%. On 16 September 2026 the FOMC raised the range by 25 bp, 12–0 — the first hike since 2023 — citing inflation that "remains high" against solid growth. The next meeting is 27–28 October; nothing has happened since September.

## How to rerun

See `README.md`. One command per config from the repo root:

```bash
python3 tools/answer_eval/run_bench.py --config server-think --name <run>
python3 tools/answer_eval/run_bench.py --config server-fast  --name <run>
python3 tools/answer_eval/run_bench.py --config local-think  --name <run>
python3 tools/answer_eval/run_bench.py --config local-fast   --name <run>
python3 tools/answer_eval/judge_packet.py <run>      # packets + batches for the judge
python3 tools/answer_eval/score.py <run> baseline-server-think   # side by side
python3 tools/answer_eval/stats.py <run>
```
