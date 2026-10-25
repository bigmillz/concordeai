# Scores — q2-server-think

## q2-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 2.3 | 3.2 | 2.7 | 3.3 | 5.3 | 5.7 | **2.2** | 13/8/2 | 0 |
| compare | 4 | 3.2 | 4.0 | 3.0 | 4.0 | 5.0 | 6.2 | **3.0** | 5/7/2 | 0 |
| health | 6 | 4.8 | 5.2 | 4.2 | 4.0 | 5.0 | 4.8 | **3.8** | 3/5/6 | 0 |
| travel | 5 | 3.2 | 3.0 | 2.8 | 2.4 | 4.0 | 3.6 | **2.8** | 5/6/4 | 1 |
| money | 5 | 4.8 | 5.4 | 4.0 | 5.0 | 6.2 | 6.2 | **4.0** | 3/4/0 | 0 |
| reasoning | 3 | 8.0 | 8.0 | 5.7 | 6.7 | 7.0 | 7.3 | **6.7** | 0/1/2 | 0 |
| technical | 1 | 3.0 | 4.0 | 3.0 | 5.0 | 6.0 | 7.0 | **3.0** | 2/1/1 | 0 |
| followup | 3 | 2.7 | 2.7 | 3.0 | 3.3 | 4.3 | 4.3 | **2.7** | 4/5/1 | 1 |
| ALL | 33 | 4.0 | 4.4 | 3.5 | 4.0 | 5.2 | 5.4 | **3.4** | 35/37/18 | 2 |

Lowest overall:
- q17.t2.json: overall 1 — The answer only apologises for missing price data when the data existed (sheet: R$3,500-4,800 incl. condo, ~$700-950 at ~R$5.0-5.1/USD); the review flagged the draft's Jurerê pick as unsourced, so the
- q21.json: overall 1 — Error line: the only seated writer (gpt-oss:20b) produced degenerate output and there was no fallback, although search returned relevant pages (LATAM's carry-on page, carrysizer's LATAM/Azul/GOL guide
- q01.json: overall 2 — The gpt-oss:20b draft correctly named Blackwell Ultra B300, but the ministral-3:14b review 'corrected' it to the A100 from a 2020 Ampere press release in the search results, and the merge accepted it;
- q03.json: overall 2 — The draft had the right CPU answer (4 vCPU / 8 GB droplet) but the ministral-3:14b review struck it as unsourced and the final substituted an invented A100/V100 'GPU-Standard' droplet; search never re
- q04.json: overall 2 — Search returned the July 29 FOMC statement but not the September 16 one, and the drafts trusted it as the latest; the review even noted the October 27-28 meeting was next but missed the September meet
