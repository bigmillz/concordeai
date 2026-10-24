# Scores — baseline-server-think

## baseline-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 2 | 1.5 | 1.0 | 2.5 | 4.0 | 4.0 | 4.5 | **2.0** | 18/5/2 | 0 |
| compare | 3 | 3.7 | 2.7 | 4.0 | 4.3 | 5.0 | 4.3 | **3.7** | 13/15/3 | 0 |
| health | 6 | 5.0 | 4.3 | 4.5 | 5.2 | 5.8 | 5.0 | **4.3** | 8/11/10 | 0 |
| travel | 5 | 3.0 | 2.8 | 3.6 | 3.8 | 5.0 | 4.0 | **3.2** | 21/12/3 | 0 |
| money | 5 | 3.2 | 2.2 | 4.2 | 4.6 | 5.6 | 5.6 | **3.2** | 17/12/7 | 0 |
| reasoning | 3 | 7.0 | 6.7 | 6.0 | 6.7 | 6.3 | 6.7 | **6.3** | 3/3/1 | 0 |
| technical | 1 | 6.0 | 6.0 | 5.0 | 6.0 | 8.0 | 6.0 | **5.0** | 1/1/1 | 0 |
| followup | 3 | 2.3 | 2.0 | 2.7 | 3.7 | 4.7 | 4.0 | **2.3** | 15/10/3 | 0 |
| ALL | 28 | 3.9 | 3.3 | 4.1 | 4.7 | 5.4 | 4.9 | **3.7** | 96/69/30 | 0 |

Lowest overall:
- q01.t2.json: overall 1 — No search ran, and the compositor ministral-3:14b misread 'Claude's response beat you' as 'why is Claude's hardware faster', fusing gpt-oss's fictional Infernet draft with gemma's RLHF-prose draft, in
- q01.json: overall 2 — Search failed outright (all four sources are claude.ai sign-in / 'what is Claude' pages, the exact trap on the sheet), so all three drafts came from stale training memory; the compositor ministral-3:1
- q02.json: overall 2 — Search returned usable sources (WIRED guide, apple.com, a Macworld 'Pro Max, Duo ranked' page) and two of three drafts (gpt-oss, gemma) correctly named the iPhone 18 Pro/Pro Max as newest with the Mac
- q20.json: overall 2 — gpt-oss:20b's own draft was degenerate (31 chars), so the gpt-oss:20b compositor built the answer from ministral's draft, which fabricated a 'Mercer 2026 mid-year' with Vienna first, and dressed it wi
- q26.json: overall 2 — Search returned nothing usable (the Amazon Wikipedia page, an AmazonGlobal help page, a Prime Now shopper YouTube video and a NYT labor podcast), so the country list came from model knowledge, and the
- q10.json: overall 3 — Search returned on-topic 2026 pages, but no draft excerpt mentions Ollama 0.19's MLX backend (gemma still calls llama.cpp 'the engine behind Ollama'), so the picture is stale model knowledge; the comp
- q11.t2.json: overall 3 — Each draft contributed an invented caution — gpt-oss:20b the 'reduces sertraline absorption' line, ministral-3:14b the '3-4 hours from alcohol / 2-3 cocktails' rule, gemma4:12b the 'additive sedation'
- q12.json: overall 3 — Search returned marketing and social video (YouTube, TikTok, Thompson Tee, Sure deodorant) rather than AAD or Cleveland Clinic, gemma4:12b returned an empty draft, and the compositor gpt-oss:20b kept 
