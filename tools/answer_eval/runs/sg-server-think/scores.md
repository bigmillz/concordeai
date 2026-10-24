# Scores — sg-server-think

## sg-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 2.2 | 2.2 | 3.0 | 3.7 | 4.7 | 4.7 | **2.2** | 30/14/10 | 0 |
| compare | 4 | 3.5 | 3.5 | 3.0 | 3.5 | 4.5 | 5.0 | **3.0** | 6/6/7 | 1 |
| health | 6 | 4.2 | 3.8 | 4.2 | 4.5 | 5.7 | 5.2 | **3.8** | 6/13/11 | 0 |
| travel | 5 | 3.0 | 2.6 | 3.2 | 3.2 | 5.2 | 5.2 | **2.6** | 17/19/9 | 0 |
| money | 5 | 3.8 | 4.0 | 3.6 | 4.0 | 5.6 | 5.6 | **3.4** | 9/6/8 | 0 |
| reasoning | 3 | 5.0 | 5.3 | 5.0 | 5.7 | 7.0 | 7.0 | **4.7** | 2/3/3 | 0 |
| technical | 1 | 4.0 | 3.0 | 5.0 | 4.0 | 5.0 | 4.0 | **4.0** | 2/4/4 | 0 |
| followup | 3 | 3.3 | 3.0 | 3.3 | 4.0 | 6.0 | 5.7 | **3.0** | 15/8/1 | 0 |
| ALL | 33 | 3.5 | 3.4 | 3.6 | 4.0 | 5.4 | 5.3 | **3.2** | 87/73/53 | 1 |

Lowest overall:
- q01.t2.json: overall 1 — No search ran on the follow-up, so all three drafts reasoned from the wrong first answer and model memory; the merge kept the fabricated 'why Claude won' framing and invented a page of specs and per-t
- q03.json: overall 1 — Search returned third-party pricing mirrors and LLM-hosting listicles, not DigitalOcean's docs pricing page; drafts latched onto a '$2.50/hr GPU Droplet' figure, and the ministral merge invented GPU m
- q05.json: overall 1 — Search returned early-2026 Claude timelines and an OpenAI Images 2.5 piece, nothing on Fable 5.1, Opus 5.5 or GPT-6; drafts reported Opus 4.7 (and gemma said GPT-5), and the ministral merge picked an 
- q06.json: overall 1 — Search returned one 2022 CBS article plus recipe pages, never BLS/FRED. The merge re-dated the 2022 figures to 2025 and pulled ministral's invented $6.90 'today' into a confident and backwards verdict
- q09.json: overall 1 — Search returned off-target pages (Thailand, Germany, Boston, a Polish rome2rio page) and the gpt-oss merge chose its own no-data draft over ministral's ranked one. It then appended invented photo desc
