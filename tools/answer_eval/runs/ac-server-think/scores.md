# Scores — ac-server-think

## ac-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 2.8 | 3.0 | 2.8 | 3.5 | 5.2 | 6.2 | **2.5** | 25/10/7 | 0 |
| compare | 4 | 3.5 | 2.5 | 3.5 | 4.0 | 4.8 | 4.8 | **3.0** | 11/11/5 | 0 |
| health | 6 | 4.7 | 4.0 | 4.0 | 3.7 | 4.3 | 4.0 | **3.8** | 3/7/9 | 1 |
| travel | 5 | 2.8 | 2.6 | 3.4 | 2.8 | 4.4 | 4.0 | **2.4** | 15/19/15 | 0 |
| money | 5 | 3.6 | 2.6 | 3.8 | 4.4 | 5.4 | 5.4 | **3.0** | 12/17/5 | 0 |
| reasoning | 3 | 7.7 | 7.3 | 5.7 | 6.7 | 6.7 | 7.0 | **6.3** | 2/2/1 | 0 |
| technical | 1 | 4.0 | 3.0 | 4.0 | 4.0 | 5.0 | 4.0 | **4.0** | 2/4/3 | 0 |
| followup | 3 | 3.0 | 2.3 | 3.0 | 3.7 | 5.3 | 5.3 | **2.7** | 11/7/3 | 0 |
| ALL | 33 | 3.8 | 3.3 | 3.7 | 3.9 | 5.0 | 5.1 | **3.3** | 81/77/48 | 1 |

Lowest overall:
- q01.json: overall 1 — Search returned generic accelerator-comparison/leaderboard pages instead of current Nvidia/Google/Anthropic announcements, and the gpt-oss:20b draft filled the gaps from pre-2025 training memory plus 
- q01.t2.json: overall 1 — No search ran (searched=False) and the gpt-oss:20b draft doubled down on the fabricated turn-1 picture with even more invented specs and budgets; it never admitted it cannot see Claude's answer or tha
- q03.json: overall 1 — The web search failed outright (it returned Google Flow, rent.com, a running-workout video and AnythingLLM), and gpt-oss:20b invented DigitalOcean CPU tiers and prices and cited a 'DigitalOcean Pricin
- q05.json: overall 1 — Search returned a generic comparison blog, a Pluralsight explainer, the claude.ai sign-in page and the Feb 2025 Claude 3.7 Sonnet post, so nothing current; gpt-oss:20b answered from pre-2025 memory an
- q11.json: overall 1 — Error line: the only seated writer (gpt-oss:20b on Ollama1) returned a ServerError after 9.8s. Thinking seats one writer, so there was no fallback draft and nothing to compose, even though search retu
