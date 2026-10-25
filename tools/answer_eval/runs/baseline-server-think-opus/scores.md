# Scores — baseline-server-think-opus

## baseline-server-think-opus

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 1.7 | 1.3 | 2.8 | 3.2 | 5.3 | 5.2 | **1.5** | 36/19/7 | 0 |
| compare | 4 | 3.5 | 2.8 | 3.8 | 4.5 | 5.2 | 4.8 | **3.0** | 14/16/7 | 0 |
| health | 6 | 4.5 | 4.2 | 5.0 | 5.5 | 6.5 | 5.8 | **4.0** | 8/15/12 | 0 |
| travel | 5 | 2.6 | 2.6 | 3.6 | 3.2 | 5.0 | 4.2 | **2.8** | 18/17/7 | 0 |
| money | 5 | 2.8 | 2.2 | 4.0 | 3.8 | 5.8 | 5.2 | **2.8** | 18/13/5 | 0 |
| reasoning | 3 | 6.7 | 6.0 | 5.7 | 6.0 | 6.7 | 6.3 | **6.0** | 3/3/3 | 0 |
| technical | 1 | 5.0 | 6.0 | 5.0 | 6.0 | 7.0 | 7.0 | **5.0** | 1/1/1 | 0 |
| followup | 3 | 2.3 | 1.7 | 2.7 | 3.3 | 4.7 | 4.3 | **2.0** | 13/13/5 | 0 |
| ALL | 33 | 3.3 | 2.9 | 3.9 | 4.2 | 5.7 | 5.2 | **3.1** | 111/97/47 | 0 |

Lowest overall:
- q01.json: overall 1 — Search returned only claude.ai sign-in and 'what is Claude' pages (a listed trap), so all three drafts fell back to pre-2025 model memory; the ministral merge then invented an 'Anthropic Infernet' ASI
- q01.t2.json: overall 1 — No search ran, and the merge took the turn-1 hallucination as fact and expanded it into a fake technical deep-dive with invented percentages; it never says it can't see Claude's reply and never fixes 
- q02.json: overall 1 — Two drafts (gpt-oss, gemma) correctly said the iPhone 18 Pro/Pro Max were the newest as of Sept 2026, but the ministral merge discarded that for its own iPhone 17 framing and filled the table with las
- q03.json: overall 1 — Search did return DO's docs pricing page, which lists GPU Droplets, but the merged answer (ministral) said none exist and invented CPU droplet prices; the gemma draft correctly named H100 GPU Droplets
- q05.json: overall 1 — Search returned only generic 'Claude vs ChatGPT' blogs, claude.ai's sign-in page and the Claude 3.7 Sonnet launch post, so the drafts guessed from old memory (Claude 4.0, GPT-4 Turbo, 3.7 Sonnet, 'Opu
