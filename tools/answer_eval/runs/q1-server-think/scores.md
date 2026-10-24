# Scores — q1-server-think

## q1-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 3.5 | 3.8 | 3.0 | 3.3 | 4.8 | 5.2 | **3.0** | 11/12/6 | 0 |
| compare | 4 | 3.5 | 3.8 | 2.5 | 3.5 | 3.5 | 4.8 | **2.8** | 8/10/4 | 0 |
| health | 6 | 4.3 | 4.3 | 3.3 | 3.7 | 4.7 | 4.2 | **3.3** | 2/12/8 | 1 |
| travel | 5 | 2.2 | 2.6 | 2.2 | 2.4 | 2.8 | 3.8 | **1.8** | 9/9/4 | 1 |
| money | 5 | 3.8 | 3.6 | 3.4 | 4.0 | 4.4 | 4.2 | **3.4** | 5/6/4 | 1 |
| reasoning | 3 | 7.7 | 7.3 | 6.0 | 6.3 | 7.0 | 7.3 | **6.3** | 2/1/0 | 0 |
| technical | 1 | 3.0 | 3.0 | 4.0 | 4.0 | 5.0 | 5.0 | **3.0** | 2/3/2 | 0 |
| followup | 3 | 3.3 | 3.3 | 3.3 | 3.7 | 5.0 | 5.0 | **3.0** | 14/2/2 | 0 |
| ALL | 33 | 3.8 | 3.9 | 3.3 | 3.7 | 4.5 | 4.8 | **3.2** | 53/55/30 | 3 |

Lowest overall:
- q01.t2.json: overall 1 — No search ran on the follow-up, so gpt-oss:20b answered from memory, doubled down on the RTX 5090 and filled in invented specs; the ministral review itself proposed more wrong numbers, and the merge n
- q16.json: overall 1 — Both seated drafts (gpt-oss:20b and ministral-3:14b) returned ServerError from the Ollama1 server after 22s, so the person saw only an error line; the search results were also off-target (Bible naggin
- q20.json: overall 1 — Search never surfaced the EIU or Mercer pages (it returned US-only lists, population, air quality, even a badminton stream). The gpt-oss draft invented a Numbeo top ten. The ministral review deleted D
- q21.json: overall 1 — The answer the person saw was empty (0 chars, no first visible text). The pipeline produced a 3414-char draft and polish ran, but the verify step never ran and nothing reached the screen, so the merge
- q22.json: overall 1 — The person saw an empty answer: the gpt-oss:20b draft (which itself had a wrong ATC median of $102,010 and an off-topic 'remote marketing manager') was flagged by the ministral reviewer and the polish
