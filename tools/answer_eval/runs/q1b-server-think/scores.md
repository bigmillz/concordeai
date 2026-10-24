# Scores — q1b-server-think

## q1b-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 3.3 | 4.2 | 3.0 | 3.7 | 5.3 | 5.7 | **2.8** | 8/10/7 | 0 |
| compare | 4 | 3.8 | 3.5 | 2.8 | 3.8 | 4.8 | 4.5 | **2.8** | 4/9/3 | 0 |
| health | 6 | 5.2 | 4.8 | 4.0 | 4.2 | 5.0 | 4.5 | **4.2** | 3/11/11 | 0 |
| travel | 5 | 2.6 | 2.6 | 3.2 | 3.4 | 5.2 | 4.6 | **2.6** | 11/16/7 | 0 |
| money | 5 | 4.8 | 5.0 | 3.6 | 4.2 | 5.4 | 5.2 | **4.0** | 4/10/8 | 0 |
| reasoning | 3 | 6.7 | 6.7 | 5.3 | 5.7 | 5.7 | 7.0 | **5.7** | 2/4/1 | 0 |
| technical | 1 | 4.0 | 3.0 | 4.0 | 4.0 | 5.0 | 4.0 | **3.0** | 1/4/2 | 0 |
| followup | 3 | 1.7 | 2.0 | 2.0 | 1.7 | 4.7 | 4.7 | **1.7** | 11/5/3 | 0 |
| ALL | 33 | 4.0 | 4.1 | 3.4 | 3.8 | 5.2 | 5.1 | **3.4** | 44/69/42 | 0 |

Lowest overall:
- q01.t2.json: overall 1 — No search ran this turn, so gpt-oss:20b took the user's framing at face value, invented what Claude said and made up TPU specs from memory. The reviewer flagged the pod and scale errors, but the merge
- q06.json: overall 1 — Search returned a recall story, a 2022 CBS piece and three recipe pages, never BLS/FRED, so the gpt-oss draft led with a December 2022 figure and then said no current price exists; the review only cor
- q17.t2.json: overall 1 — No search ran. The gpt-oss draft gave an unanchored 'Centro' price, the ministral critic suggested 'Bela Vista' sub-neighborhoods (São Paulo), and the merge rewrote the answer around São Paulo, losing
- q01.json: overall 2 — Search brought back generic TPU-vs-GPU comparison blogs and a 2023 Decrypt piece rather than the Vera Rubin, Ironwood, TPU 8 or Anthropic compute announcements, so the gpt-oss:20b draft filled the gap
- q03.json: overall 2 — Search returned third-party pricing aggregators and an ad link instead of DigitalOcean's GPU pricing doc. gpt-oss:20b invented a $2.50/hr GPU price and a 32 GB RAM floor for 7B, and the merge kept the
