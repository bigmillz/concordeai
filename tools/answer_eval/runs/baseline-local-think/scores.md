# Scores — baseline-local-think

## baseline-local-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| compare | 1 | 6.0 | 5.0 | 4.0 | 5.0 | 5.0 | 4.0 | **5.0** | 0/2/1 | 0 |
| health | 3 | 5.0 | 4.7 | 3.7 | 3.7 | 4.7 | 4.3 | **4.0** | 2/2/9 | 0 |
| followup | 1 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** | 0/0/0 | 1 |
| ALL | 5 | 4.4 | 4.0 | 3.2 | 3.4 | 4.0 | 3.6 | **3.6** | 2/4/10 | 1 |

Lowest overall:
- q11.t2.json: overall 1 — Error line: two of three seats (Qwen 3.6 35B MoE, Gemma 4 26B) were refused for low memory at 4s and the last, Qwen 3.8 27B, timed out at 124s, so no draft existed and the app surfaced 'RuntimeError: 
- q11.json: overall 2 — Qwen 3.8 27B never answered (too slow at 125s), so the only draft came from Gemma 4 E2B, a ~2B-active model, and it shipped verbatim with no compositor; search gave it a supplement-marketing site, a r
- q14.json: overall 4 — Search was junk (a LifeHack listicle, a bedding brand's blog, a 2017 BBC piece, a supplement site's page), Qwen 3.8 27B timed out, and the compositor Gemma 4 12B worked from its own and Gemma 4 E2B's 
- q09.json: overall 5 — Search returned four generic cost-of-living calculator landing pages (livingcost.io, livingcost.org, Numbeo's bare comparison.jsp, moneywithsense), none about these cities, so all three drafts ran on 
- q12.json: overall 6 — Search returned junk (a YouTube clip, a TikTok page, a sweat-proof undershirt vendor's blog and Sure deodorant marketing); Qwen 3.8 27B timed out and Ministral 3 14B was refused for low memory, so Gem
