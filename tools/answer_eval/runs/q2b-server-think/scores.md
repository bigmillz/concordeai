# Scores — q2b-server-think

## q2b-server-think

| category | n | correct | grounded | depth | fit | structure | concise | overall | halluc. high/med/low | error answers |
|---|---|---|---|---|---|---|---|---|---|---|
| fresh | 6 | 4.5 | 5.3 | 3.5 | 4.5 | 5.8 | 6.2 | **4.0** | 9/6/2 | 0 |
| compare | 4 | 3.5 | 3.8 | 2.8 | 3.5 | 5.8 | 6.0 | **3.0** | 7/5/1 | 0 |
| health | 6 | 4.5 | 4.7 | 3.7 | 3.8 | 5.0 | 4.8 | **3.7** | 3/11/8 | 0 |
| travel | 5 | 3.4 | 4.2 | 3.2 | 3.2 | 5.2 | 4.4 | **2.8** | 7/8/6 | 0 |
| money | 5 | 4.4 | 4.4 | 3.4 | 4.2 | 6.2 | 6.0 | **4.0** | 3/7/5 | 0 |
| reasoning | 3 | 7.3 | 7.7 | 6.0 | 7.0 | 8.0 | 8.7 | **7.0** | 1/1/1 | 0 |
| technical | 1 | 4.0 | 3.0 | 4.0 | 3.0 | 4.0 | 3.0 | **3.0** | 2/3/1 | 0 |
| followup | 3 | 3.0 | 4.0 | 2.7 | 3.3 | 5.0 | 5.7 | **2.7** | 6/3/2 | 0 |
| ALL | 33 | 4.3 | 4.7 | 3.5 | 4.1 | 5.7 | 5.7 | **3.8** | 38/44/26 | 0 |

Lowest overall:
- q19.json: overall 1 — Search pulled NYC-centric pages (Numbeo New York, a Marmande-to-NYC Google Flights page), and the gpt-oss draft refused to use model knowledge, so the answer is a do-it-yourself method plus irrelevant
- q03.json: overall 2 — Search returned third-party pricing blogs, not DigitalOcean's GPU pricing doc, and the draft refused to give any GPU figure or the 8 GB CPU option, leaving a non-answer.
- q05.json: overall 2 — Search surfaced the Sonnet product page and a GPT-5.6 comparison blog, not the models overview or GPT-6 launch posts, so both 'latest' models are wrong and the coding pick rests on superseded benchmar
- q07.json: overall 2 — Search returned generic calculators and fare-ad pages, the read stage got 0 full pages, and the writer took a calculator's 500-mile placeholder figure at face value and then scaled it wrong. The revie
- q09.json: overall 2 — Search went Florianópolis-heavy (flight booking pages, Floripa vs Hanoi/Goa) and found nothing on Medellín or Lisbon. The writer then refused to use model knowledge and misread an index. The reviewer 
