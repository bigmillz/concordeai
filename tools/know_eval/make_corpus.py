#!/usr/bin/env python3
"""A fictional person's notes and chats, for measuring "It knows your life".

Everything here is INVENTED: Mara Lindqvist, a UX researcher moving from
Gothenburg to Lisbon in 2026, changing jobs, seeing a few doctors, and
building a side project (a birdsong app). Nothing is anyone's real data.
The repo is public, so it never may be.

Writes:
  <out>/notes/            ~200 files: .md .txt .docx .html .eml .pdf
  <out>/chats.json        ~100 chats in the app's shape (seeded through /api/chats/ops)
  <out>/questions.json    30 questions, each with the keys of the documents that answer it
                          ("file:<relative path>" or "chat:<chat id>") and a reference answer

Usage: make_corpus.py <out folder>
"""
import json
import os
import random
import re
import sys
import time
import zipfile
import zlib

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "corpus")
random.seed(530)

# a day in 2026 as a timestamp, local noon
def day(m, d, y=2026):
    return time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))

# ---- the facts that questions ask about (each with the file or chat that holds it)
FACTS = [
    # (slug, kind, title, date, body, question, reference answer)
    ("lawyer-choice", "md", "Lawyer options for the move", (6, 14),
     "Three immigration lawyers quoted for the D7 visa paperwork:\n\n## Quotes\n- Costa & Ferreira, Lisbon: 1,800 EUR flat, two weeks, speaks Swedish (Ana Costa)\n- Nordic Relocation, Stockholm office: 2,600 EUR, hand-holds everything, slow replies\n- Doing it myself with the SEF checklist: 0 EUR but the NIF and bank steps worry me\n\n## Decision\nWent with Costa & Ferreira. Ana answered within an hour both times and the flat fee covers the appeal if SEF asks for more papers. Paid the 900 EUR deposit on June 14.",
     "What did I decide about the lawyer for the move?",
     "Costa & Ferreira (Ana Costa) in Lisbon, 1,800 EUR flat fee; deposit of 900 EUR paid June 14, chosen over Nordic Relocation and doing it herself."),
    ("d7-docs", "txt", "D7 visa document checklist", (6, 20),
     "D7 checklist (from Ana):\n- passport valid 6+ months (mine runs to 2031, fine)\n- proof of passive income: Nordea statements Jan-Jun, the freelance contract with Hallon Studio\n- Portuguese NIF (got it July 2 through the lawyer's rep, number kept in 1Password)\n- Portuguese bank account: Millennium bcp, opened July 9\n- criminal record certificate from Polisen, ordered June 22, arrived July 1\n- health insurance covering Portugal: Allianz, 92 EUR/month\n- 12 months rent contract or proof of accommodation\nAppointment at the consulate in Stockholm: August 18, 10:30.",
     "When is my consulate appointment and what insurance did I pick for the visa?",
     "August 18 at 10:30 at the consulate in Stockholm; Allianz health insurance at 92 EUR a month."),
    ("flat-lisbon", "md", "Flat hunt in Lisbon", (7, 25),
     "# Flat hunt in Lisbon\n\nSaw six flats over the weekend of July 25-26.\n\n## Shortlist\n1. Campo de Ourique, Rua Ferreira Borges: 2 rooms, 1,450 EUR, 3rd floor no lift, quiet\n2. Arroios, Rua Pascoal de Melo: 2 rooms, 1,300 EUR, noisy street, big balcony\n3. Alvalade: 1,550 EUR, near the metro, landlord wants 3 months deposit\n\n## Decision\nSigned for Campo de Ourique on August 2. 1,450 EUR, two months deposit, 12-month contract starting September 1. Landlord is Senhora Beatriz Moura. The stairs will be good for me, says the physio.",
     "Which flat did I sign for in Lisbon and what is the rent?",
     "The Campo de Ourique flat on Rua Ferreira Borges, 1,450 EUR a month, 12-month contract from September 1, signed August 2; landlord Beatriz Moura."),
    ("job-offer", "md", "Offer from Tangerine Health", (5, 9),
     "# Offer from Tangerine Health\n\nSenior UX researcher, remote-first (Lisbon hub), reporting to Joana Pires.\n\n- Base 68,000 EUR\n- 10% bonus, paid in March\n- 25 days holiday plus Portuguese public holidays\n- Equipment budget 2,500 EUR\n- Start date proposed: October 1, 2026\n\nCountered on May 11 asking for 72,000 and a later start. They came back with 71,000 and October 1 firm. Accepted May 13.\n\nNotice at Volvo Cars handed in May 15; last day there August 31.",
     "What salary did I accept at Tangerine Health and when do I start?",
     "71,000 EUR base (after countering 68,000 to 72,000), 10% bonus, start October 1, 2026; accepted May 13."),
    ("volvo-exit", "txt", "Leaving Volvo Cars", (8, 28),
     "Exit checklist, Volvo Cars UX team\n- handover doc to Priya finished Aug 26\n- laptop and badge returned Aug 28 at reception Torslanda\n- pension: the ITP1 stays with Alecta; no action, letter arrives in September\n- unused holiday: 11 days paid out with the August salary\n- reference letter promised by Henrik by Sept 10\nLast day: August 31. Farewell fika was Thursday the 27th.",
     "How many unused holiday days did Volvo pay out when I left?",
     "11 days, paid out with the August salary; last day was August 31."),
    ("knee-physio", "md", "Knee: physio plan", (3, 12),
     "# Knee: physio plan\n\nSeen Dr. Lena Hagström (orthopaedics, Sahlgrenska) on March 12 for the left knee. Diagnosis: patellofemoral pain, no tear on the MRI from March 3.\n\nPlan from the physio (Jonas at Rehab Väst):\n- quad and glute strengthening 3x a week, 12 weeks\n- no running until mid-June, cycling is fine\n- stairs are good, not bad\n- review June 11\n\nJune 11 review: pain down from 6/10 to 2/10. Cleared to run from July, starting 2 km every other day.",
     "What did the doctor say about my knee and when was I cleared to run again?",
     "Patellofemoral pain, no tear on the MRI (Dr. Lena Hagström, March 12); cleared to run from July after the June 11 review, starting with 2 km every other day."),
    ("dentist", "eml", "Dentist appointment confirmed", (9, 3),
     "Hi Mara,\n\nThis confirms your appointment at Clínica Dentária Avenida on Tuesday, September 22 at 09:15 with Dr. Rui Tavares for a check-up and hygiene session. Please bring your Allianz card. The fee for the check-up is 60 EUR.\n\nKind regards,\nSofia\nClínica Dentária Avenida",
     "When is my dentist appointment in Lisbon and who is the dentist?",
     "Tuesday, September 22 at 09:15 at Clínica Dentária Avenida with Dr. Rui Tavares; check-up fee 60 EUR."),
    ("allergy", "md", "Allergy test results", (4, 22),
     "# Allergy test results (April 22)\n\nSkin prick test at Capio Vårdcentral Lundby. Positive for birch pollen (strong) and cat (mild). Negative for grass, dust mite, dog.\n\nDr. Omar Said suggested Aerius (desloratadine) 5 mg daily from mid-April to end of May, and to consider immunotherapy next winter if Lisbon has birch (it mostly doesn't, which is a point for the move).",
     "What am I allergic to according to my test, and what did the doctor suggest?",
     "Birch pollen (strong) and cat (mild); negative for grass, dust mite and dog. Dr. Omar Said suggested Aerius (desloratadine) 5 mg daily mid-April to end of May, and maybe immunotherapy next winter."),
    ("birdsong-stack", "md", "Larksong: tech choices", (2, 8),
     "# Larksong: tech choices\n\nThe side project: an app that names the bird singing outside your window.\n\n## Decided\n- On-device model: BirdNET-Analyzer weights converted to Core ML, 48 kHz mono, 3-second windows\n- iOS first, SwiftUI; Android later if anyone cares\n- No accounts, no server. Recordings never leave the phone.\n- Name: Larksong (larksong.app registered Feb 8, 11 EUR/year at Porkbun)\n\n## Rejected\n- A Flask backend for inference: too costly and the privacy story dies\n- React Native: Core ML bridging pain",
     "What did I decide about the backend for my bird app?",
     "No backend or server at all: on-device inference with BirdNET weights converted to Core ML; a Flask backend was rejected for cost and privacy. The app is Larksong, iOS first in SwiftUI."),
    ("birdsong-beta", "txt", "Larksong beta notes", (8, 15),
     "Larksong TestFlight beta, build 14, sent Aug 15 to 23 testers.\nFeedback so far:\n- blackbird vs song thrush confusion (7 reports) -> need more thrush samples\n- battery: 3-hour listen used 18% on an iPhone 14, acceptable\n- Teodor wants a 'walk mode' that logs species along a route\nDecided: ship 1.0 in October after the move, 4.99 EUR one-time, no subscription.",
     "What price did I decide for Larksong 1.0 and when does it ship?",
     "4.99 EUR one-time, no subscription; shipping in October after the move (decided after TestFlight build 14 on Aug 15)."),
    ("movers", "html", "Moving company quotes", (7, 30),
     "<h1>Moving company quotes</h1><p>Gothenburg to Lisbon, about 12 cubic metres, no piano.</p><ul><li>Alfa Flytt: 3,900 EUR, 10-14 days, insurance included up to 20,000 EUR</li><li>EuroMove Iberia: 3,200 EUR, 3 weeks, insurance extra (180 EUR)</li><li>Selling most things and flying with 4 suitcases: ~900 EUR</li></ul><p>Booked Alfa Flytt for pickup on September 7. They deliver between September 17 and 21. Paid 1,000 EUR deposit.</p>",
     "Which moving company did I book and when do they pick up?",
     "Alfa Flytt, 3,900 EUR with insurance to 20,000 EUR; pickup September 7, delivery September 17-21; deposit 1,000 EUR."),
    ("car-sale", "txt", "Selling the Polestar", (8, 9),
     "Sold the Polestar 2 (2022, 61,000 km) to Hedin Bil in Mölndal on August 9 for 289,000 SEK. Loan with Volvofinans paid off (remaining 142,000 SEK), so 147,000 SEK landed in Nordea on August 12. Keeping the winter tyres for Elin.",
     "How much did I get for the Polestar and what was left after the loan?",
     "289,000 SEK from Hedin Bil on August 9; after the 142,000 SEK loan, 147,000 SEK landed in Nordea on August 12."),
    ("portuguese", "md", "Portuguese lessons", (4, 5),
     "# Portuguese lessons\n\nStarted with Inês on italki, April 5. Two 45-minute lessons a week, 22 EUR each. Goal: A2 by the move, B1 by Christmas.\n\nJune check: can order, ask directions, talk about work slowly. Inês says I overuse 'pois'. Switching one lesson a week to conversation only.",
     "Who is my Portuguese teacher and what is my language goal?",
     "Inês on italki (two 45-minute lessons a week at 22 EUR); goal A2 by the move and B1 by Christmas."),
    ("elin-birthday", "chat", "Elin's birthday plan", (9, 10),
     None, "What did I plan for Elin's birthday?",
     "A weekend in Porto on October 17-18: the Yeatman hotel one night, dinner at Cantinho do Avillez, and the port cellar tour at Taylor's, around 600 EUR all in."),
    ("tax-advice", "chat", "Tax when moving to Portugal", (6, 30),
     None, "What did I learn about taxes when moving to Portugal?",
     "The NHR regime closed to new arrivals; the IFICI/NHR 2.0 may apply to her as a researcher-type role, so she decided to ask the accountant Carla Mendes (Lisbon, 150 EUR consultation) before October, and to tell Skatteverket she is moving with the SKV 7665 form."),
    ("sleep-doc", "chat", "Sleep problems and the doctor", (5, 20),
     None, "What did the doctor suggest for my sleep?",
     "Dr. Said suggested no screens after 22:00, magnesium glycinate 300 mg, fixed wake time 06:45, and a sleep diary for 4 weeks; melatonin only short term; a referral if still bad after 6 weeks."),
    ("gym-lisbon", "chat", "Gyms near Campo de Ourique", (9, 14),
     None, "Which gym did I pick in Lisbon?",
     "Fitness Hut Campo de Ourique, 29.90 EUR a month on the annual plan, five minutes from the flat; chosen over Holmes Place (89 EUR) and Solinca."),
    ("priya-handover", "docx", "Handover to Priya", (8, 26),
     "Handover: UX research, Volvo Cars infotainment team\nOwner from September 1: Priya Raman\n\nOpen studies\n1. Voice assistant diary study: 14 participants, fieldwork ends September 12, synthesis template in Dovetail project VA-2026\n2. Charging map usability: round 2 planned for October, protocol v3 in Figma\n3. Driver monitoring acceptance survey: 1,200 responses, analysis half done, scripts in the research repo under dms-survey\n\nPeople\n- Henrik Blom: manager, approves recruitment budgets\n- Sara Ek: design lead, wants Friday readouts\n- Recruitment agency: Respondent.io account under the team email\n\nThings I wish someone had told me\n- book the Torslanda lab six weeks ahead\n- the Chinese market readouts need the Shanghai team awake: 09:00 CET at the latest",
     "What open studies did I hand over to Priya?",
     "The voice assistant diary study (14 participants, fieldwork ends Sept 12), the charging map usability round 2 (October), and the driver monitoring acceptance survey (1,200 responses, analysis half done)."),
    ("rent-contract", "pdf", "Rental contract summary", (8, 2),
     "Rental contract summary. Property: Rua Ferreira Borges, Campo de Ourique, Lisbon, 3rd floor. Landlord: Beatriz Moura. Tenant: Mara Lindqvist. Rent 1450 EUR per month, due on the 1st. Deposit two months, 2900 EUR, paid August 2. Term 12 months from September 1 2026 to August 31 2027, renewable. Utilities not included. Notice period 60 days. Pets allowed with written consent.",
     "What is the notice period in my rental contract?",
     "60 days; the 12-month contract runs September 1, 2026 to August 31, 2027 with a 2,900 EUR deposit."),
    ("bike", "txt", "Bike for Lisbon", (9, 20),
     "Bought a used Brompton C Line (2021, black) from a seller in Alcântara on September 20 for 850 EUR. Lisbon hills are brutal; the physio says cycling is fine for the knee. Registered it with the police anti-theft registry the same day.",
     "What bike did I buy in Lisbon and what did it cost?",
     "A used 2021 Brompton C Line, black, for 850 EUR on September 20 from a seller in Alcântara."),
    ("mum-visit", "md", "Mum's visit in November", (9, 28),
     "# Mum's visit\n\nMum (Birgitta) flies Gothenburg to Lisbon on November 12, TAP TP 1795, lands 14:35, back on the 19th. She wants Sintra, Belém and a fado night. Booked Sintra for Saturday the 14th (Pena Palace tickets 9:30). Her knee is worse than mine: no Alfama walking tour.",
     "When does my mum arrive and what did I book for her visit?",
     "November 12 on TAP TP 1795, landing 14:35, leaving the 19th; Sintra with Pena Palace tickets at 9:30 on Saturday the 14th; a fado night and Belém planned."),
    ("insurance-home", "eml", "Your home insurance is active", (9, 2),
     "Dear Ms Lindqvist,\n\nYour home contents insurance (policy PT-44817-H) for Rua Ferreira Borges is active from September 1, 2026. Annual premium: 196 EUR, charged to your Millennium bcp account. Covered contents: 25,000 EUR. Bicycle cover up to 1,000 EUR is included.\n\nFidelidade Seguros",
     "How much is my home insurance in Lisbon and does it cover the bike?",
     "196 EUR a year with Fidelidade (policy PT-44817-H), contents to 25,000 EUR; bicycle cover is included up to 1,000 EUR."),
    ("book-club", "chat", "Book club picks", (7, 12),
     None, "What book did my book club pick for September?",
     "Olga Tokarczuk's 'Flights' for September; 'The Years' by Annie Ernaux was August's; meetings are the last Thursday on Zoom now that she is leaving Gothenburg."),
    ("research-method", "chat", "Diary study incentives", (4, 28),
     None, "What incentive did I decide on for the diary study participants?",
     "Participants get 1,200 SEK in gift cards for the two-week diary study, plus 300 SEK for the exit interview; recruited through Respondent.io; 14 people."),
    ("laptop", "chat", "New laptop for the job", (9, 24),
     None, "Which laptop did I decide to buy with the equipment budget?",
     "A MacBook Pro 14 M5 with 48 GB RAM, about 2,900 EUR, from the 2,500 EUR Tangerine equipment budget plus her own 400 EUR; chosen over the MacBook Air 15 because of the Core ML work for Larksong."),
    ("coffee", "md", "Coffee places, Campo de Ourique", (9, 15),
     "# Coffee near the flat\n\n- Comoba: best flat white, laptop friendly before 11\n- Hello, Kristof: Belgian, great pastries, no laptops\n- A Padaria Portuguesa: fine, cheap, everywhere\n\nWorking from Comoba on Tuesdays.",
     "Where do I like to work from near the flat?",
     "Comoba in Campo de Ourique (best flat white, laptop friendly before 11), on Tuesdays."),
    ("eye-test", "eml", "Your optician appointment", (10, 1),
     "Hi Mara,\n\nYour eye test at MultiOpticas Campo de Ourique is booked for October 8 at 17:00. Please bring your current glasses. The test is free with a frame purchase, otherwise 25 EUR.\n\nMultiOpticas",
     "When is my eye test?",
     "October 8 at 17:00 at MultiOpticas Campo de Ourique; 25 EUR unless she buys frames."),
    ("sunset-run", "txt", "Running routes Lisbon", (9, 27),
     "Running again: 2 km every other day as Jonas said. Route: flat along the river from Cais do Sodré to Belém, 7 km one way, back by tram 15. Knee fine at 3 km on Sept 27. Plan: 5 km by end of October.",
     "How is my knee doing with running now?",
     "Fine at 3 km on September 27; running 2 km every other day as the physio said, aiming for 5 km by end of October along the river route."),
    ("whisky-gift", "chat", "Gift for Henrik", (8, 20),
     None, "What did I get Henrik as a leaving gift?",
     "A bottle of Mackmyra Svensk Rök and a framed photo of the team at the Torslanda lab, about 900 SEK."),
    ("budget", "md", "Lisbon monthly budget", (9, 5),
     "# Lisbon monthly budget (EUR)\n\n| Item | EUR |\n|---|---|\n| Rent | 1450 |\n| Utilities + internet | 140 |\n| Food | 450 |\n| Gym | 30 |\n| Portuguese lessons | 180 |\n| Insurance (health + home) | 108 |\n| Transport (Navegante pass) | 40 |\n| Fun | 300 |\n\nTotal about 2,700. Net salary should be about 3,900 after IRS, so saving 1,200 a month for the Larksong launch fund.",
     "What is my monthly budget in Lisbon and how much am I saving?",
     "About 2,700 EUR a month (rent 1,450, food 450, utilities 140, lessons 180, fun 300, etc.) against roughly 3,900 net, so about 1,200 EUR saved a month."),
    ("larksong-name", "chat", "Naming the bird app", (2, 5),
     None, "Why did I name the bird app Larksong?",
     "Because 'Birdly' and 'Chirp' were taken on the App Store, 'Larksong' was free as a .app domain, and larks sing at dawn, when the app is most used; registered larksong.app."),
]

# noise: everyday notes about other things, so the index has to pick
NOISE_TOPICS = [
    ("Groceries", "Oat milk, eggs, bananas, rye bread, coffee beans, lentils, tomatoes."),
    ("Recipe: lentil soup", "Onion, carrot, celery, 300 g red lentils, cumin, 1 litre stock, lemon at the end."),
    ("Meeting notes", "Weekly sync. Recruitment slipping a week. Sara wants the readout Friday. Action: book the lab."),
    ("Podcast ideas", "An episode on diary studies; one on research repos; interview Priya."),
    ("Reading list", "Flights, The Years, Klara and the Sun, Design for the Real World."),
    ("Workout log", "Squats 3x10 at 40 kg, step-ups 3x12 each leg, bridges 3x15. Knee 2/10."),
    ("Plant care", "Water the monstera Sundays. Fiddle leaf fig hates the draught from the balcony door."),
    ("Film notes", "Watched Aftersun again. The home video frames carry the whole film."),
    ("Random thoughts", "Why do all onboarding flows look the same? Because they copy each other."),
    ("Travel ideas", "Azores in spring. Madeira levadas. The Douro by train."),
    ("Swedish paperwork", "Flyttanmälan to Skatteverket, forward the post with Adressändring, cancel the Chalmers alumni fee."),
    ("Links to read", "Core ML audio preprocessing guide. Dovetail synthesis templates. Figma variables."),
]

CHAT_NOISE_QA = [
    ("How do I convert a 48 kHz WAV to mel spectrograms in Swift?", "Use AVAudioEngine to capture, then a Core ML model with a built-in mel front end; or compute with vDSP: frame into 3-second windows, apply a Hann window, FFT with vDSP_fft_zrip, map to 64 mel bins."),
    ("Give me a quick recipe for bacalhau à brás", "Shred salted cod (soaked 24 h), fry onion and garlic in olive oil, add matchstick potatoes, stir in beaten eggs off the heat, finish with parsley and black olives."),
    ("What is the difference between a diary study and a longitudinal survey?", "A diary study captures in-the-moment entries over days or weeks, qualitative and self-paced; a longitudinal survey asks the same structured questions at fixed points, quantitative and comparable across waves."),
    ("Explain the Portuguese past tenses simply", "Pretérito perfeito for finished actions (falei), imperfeito for habits and background (falava), and the compound perfeito for repeated recent actions (tenho falado)."),
    ("How do I set up a Figma variable for spacing?", "Create a collection, add number variables like space/100 = 8, bind them to auto-layout padding and gaps, and switch modes for compact and comfortable."),
    ("Is cycling fine for patellofemoral pain?", "Usually yes: low impact, keep the saddle high enough that the knee never bends past 90 degrees, and avoid heavy gears on hills early on."),
    ("Write a short toast for a colleague leaving", "To Henrik: thanks for the budgets you found and the ones you pretended not to notice. Skål."),
    ("What should I pack for a week in Lisbon in November?", "Layers, a light rain jacket, good shoes for the hills and cobbles, sunglasses still, and a plug adapter for Swedish devices (same Type F, no adapter needed)."),
    ("How does Core ML handle audio models?", "Core ML runs the converted model on the Neural Engine; you feed MLMultiArray inputs. Audio preprocessing stays in your code unless the model bundles it."),
    ("Suggest names for a bird identification app", "Larksong, Chirp, Birdly, Warble, Aviary, Dawn Chorus, Fieldnote."),
]


def md_file(path, title, date, body):
    with open(path, "w", encoding="utf-8") as fh:
        if not body.lstrip().startswith("#"):
            fh.write("# %s\n\n" % title)
        fh.write(body + "\n")
    os.utime(path, (date, date))


def txt_file(path, title, date, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(title + "\n\n" + body + "\n")
    os.utime(path, (date, date))


def html_file(path, title, date, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><html><head><title>%s</title><style>body{font:14px sans-serif}</style>"
                 "<script>var x=1;</script></head><body>%s</body></html>" % (title, body))
    os.utime(path, (date, date))


def eml_file(path, title, date, body):
    stamp = time.strftime("%a, %d %b %Y %H:%M:%S +0100", time.localtime(date))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("From: sender@example.invalid\nTo: mara@example.invalid\nSubject: %s\nDate: %s\n"
                 "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n%s\n" % (title, stamp, body))
    os.utime(path, (date, date))


def docx_file(path, title, date, body):
    paras = "".join("<w:p><w:r><w:t xml:space=\"preserve\">%s</w:t></w:r></w:p>"
                    % p.replace("&", "&amp;").replace("<", "&lt;") for p in [title] + body.split("\n"))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s</w:body></w:document>' % paras)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", doc)
    os.utime(path, (date, date))


def pdf_file(path, title, date, body):
    """A one-page PDF whose text is a FlateDecode content stream with Tj
    operators and WinAnsi (standard) font encoding: the text layer a real
    exporter writes for plain Latin text."""
    lines = [title, ""]
    for para in body.split("\n"):
        words, cur = para.split(), ""
        for w in words:
            if len(cur) + len(w) > 80:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        lines.append(cur)

    def pdfesc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 50 780 Td 14 TL\n" + "\n".join("(%s) Tj T*" % pdfesc(ln) for ln in lines) + "\nET"
    stream = zlib.compress(content.encode("latin-1", "replace"))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offs:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    with open(path, "wb") as fh:
        fh.write(out)
    os.utime(path, (date, date))


WRITERS = {"md": md_file, "txt": txt_file, "html": html_file, "eml": eml_file, "docx": docx_file, "pdf": pdf_file}

CHAT_BODIES = {
    "elin-birthday": [
        ("Elin's birthday is October 17. We'll both be in Lisbon by then. Ideas for a weekend that isn't too expensive?",
         "Porto is the obvious one: two hours by train. One night at the Yeatman (around 280 EUR), dinner at Cantinho do Avillez, and the Taylor's port cellar tour on Sunday morning. All in about 600 EUR for two."),
        ("OK let's do Porto, 17-18. Book the Yeatman for one night and the Taylor's tour. Dinner at Cantinho do Avillez on the Saturday.",
         "Noted: Porto, October 17-18, the Yeatman one night, Cantinho do Avillez Saturday dinner, Taylor's tour Sunday. Trains from Santa Apolónia run hourly; the 09:09 gets you in for lunch.")],
    "tax-advice": [
        ("I'm moving from Sweden to Portugal in September and start a Portuguese job in October. What do I need to know about tax?",
         "Three things. First, Sweden: tell Skatteverket you are moving with form SKV 7665 (flyttanmälan to abroad); you may stay tax resident in Sweden for the year if you keep strong ties. Second, Portugal: you become tax resident after 183 days or with a home there; the old NHR regime closed to new arrivals in 2024, and its successor (IFICI, sometimes called NHR 2.0) covers certain research and innovation roles, which a UX research job may or may not qualify for. Third, the Sweden-Portugal tax treaty was terminated by Sweden from 2022, so double taxation relief is by Swedish domestic rules."),
        ("That's murky. Should I get an accountant?",
         "Yes, before you start in October. A Lisbon accountant who does expat cases is 120 to 200 EUR for a consultation; Carla Mendes (Mendes Contabilidade) was mentioned in the Lisbon expats forum at 150 EUR."),
        ("OK, I'll book Carla Mendes before October and send the SKV 7665 this week.",
         "Good plan: Carla Mendes before October, SKV 7665 this week. Keep your Nordea statements and the Tangerine contract ready for her.")],
    "sleep-doc": [
        ("I saw Dr. Said about my sleep. He said: no screens after 22:00, magnesium glycinate 300 mg before bed, a fixed wake time of 06:45 even on weekends, and a sleep diary for 4 weeks. Melatonin only short term. Referral to a sleep clinic if it's still bad after 6 weeks. Can you make me a simple diary template?",
         "Here is a one-line-per-night diary: Date, bed time, minutes to fall asleep, wakes, final wake, out of bed, caffeine after 14:00 (y/n), screens after 22:00 (y/n), magnesium (y/n), rating 1-5. Fill it each morning at 06:45 when you get up.")],
    "gym-lisbon": [
        ("Gyms within walking distance of Campo de Ourique? I want squat racks and not a lot of money.",
         "Three options: Fitness Hut Campo de Ourique (29.90 EUR a month on the annual plan, racks, five minutes away), Holmes Place Amoreiras (89 EUR, pool, posh), Solinca Campo de Ourique (about 45 EUR, smaller)."),
        ("Fitness Hut then, the annual plan.", "Fitness Hut Campo de Ourique, annual plan at 29.90 EUR a month. Bring your NIF and a Portuguese IBAN to sign up.")],
    "book-club": [
        ("Book club picks: August was The Years by Annie Ernaux. We need a September book. Something with travel.",
         "Flights by Olga Tokarczuk fits: fragments about travel and bodies, Man Booker International winner, about 400 pages."),
        ("Flights it is for September. We'll meet on Zoom now since I'm leaving Gothenburg, last Thursday of the month.",
         "September pick: Flights by Olga Tokarczuk. Meetings on Zoom, last Thursday of the month.")],
    "research-method": [
        ("Incentives for a two-week diary study in Sweden, 14 participants, plus an exit interview. What's fair?",
         "For two weeks of daily entries, 1,000 to 1,500 SEK in gift cards is the usual range; the exit interview adds 300 to 500 SEK. Pay part at the midpoint to keep people going."),
        ("Going with 1,200 SEK gift card for the diary and 300 SEK for the exit interview. Recruiting through Respondent.io.",
         "Set: 1,200 SEK for the two-week diary, 300 SEK for the exit interview, 14 participants via Respondent.io. Budget about 21,000 SEK plus fees.")],
    "laptop": [
        ("Tangerine gives me 2,500 EUR for equipment. MacBook Air 15 or MacBook Pro 14? I also do Core ML work for my bird app.",
         "For Core ML conversion and audio model testing, the Pro 14 with the M5 and 48 GB is worth it: about 2,900 EUR. The Air 15 (about 1,900 EUR) is fine for research work alone."),
        ("Pro 14, 48 GB. I'll put in the extra 400 myself.", "MacBook Pro 14, M5, 48 GB RAM, about 2,900 EUR: 2,500 from the Tangerine budget and 400 of your own.")],
    "whisky-gift": [
        ("Leaving gift for my manager Henrik. He likes whisky and the team photo from the Torslanda lab. Budget 1,000 SEK.",
         "A bottle of Mackmyra Svensk Rök (about 600 SEK) and the team photo framed (about 300 SEK) comes to around 900 SEK and is personal without being much."),
        ("Done, Mackmyra Svensk Rök plus the framed photo.", "Henrik's gift: Mackmyra Svensk Rök and the framed Torslanda team photo, about 900 SEK.")],
    "larksong-name": [
        ("Names for my bird song app. Birdly and Chirp are taken on the App Store. I want something that works as a .app domain.",
         "Larksong is free as larksong.app, and larks sing at dawn, when people would use the app most. Other free ones: Warble (warble.app taken), Dawn Chorus (long), Fieldnote (not about birds)."),
        ("Larksong. Registering larksong.app now.", "Larksong it is; larksong.app registered.")],
}


def chat_turns(key):
    return [m for q, a in CHAT_BODIES[key] for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]


def main():
    notes = os.path.join(OUT, "notes")
    os.makedirs(notes, exist_ok=True)
    for sub in ("move", "health", "work", "larksong", "life", "inbox", "scans"):
        os.makedirs(os.path.join(notes, sub), exist_ok=True)
    questions, chats = [], []
    nfiles = 0
    folder_of = {"md": "move", "txt": "life", "html": "move", "eml": "inbox", "docx": "work", "pdf": "scans"}
    for slug, kind, title, (m, d), body, q, ref in FACTS:
        date = day(m, d)
        if kind == "chat":
            cid = "c" + re.sub(r"[^a-z0-9]", "", slug.lower()).ljust(26, "x")[:26]
            chats.append({"id": cid, "title": title, "ts": int(date * 1000), "lane": "ai",
                          "messages": chat_turns(slug)})
            questions.append({"id": slug, "q": q, "expect": ["chat:" + cid], "ref": ref, "date": "%d-%02d-%02d" % (2026, m, d)})
        else:
            sub = "health" if slug in ("knee-physio", "dentist", "allergy", "eye-test", "sunset-run") else (
                "larksong" if slug.startswith("birdsong") else folder_of[kind])
            fname = slug + "." + kind
            path = os.path.join(notes, sub, fname)
            WRITERS[kind](path, title, date, body)
            nfiles += 1
            questions.append({"id": slug, "q": q, "expect": ["file:notes/%s/%s" % (sub, fname)], "ref": ref,
                              "date": "%d-%02d-%02d" % (2026, m, d)})
    # noise files: ~200 across kinds and dates
    kinds = ["md", "txt", "md", "txt", "html", "eml", "docx", "md"]
    for i in range(200):
        t, b = NOISE_TOPICS[i % len(NOISE_TOPICS)]
        k = kinds[i % len(kinds)]
        date = day(1 + (i * 7) % 10, 1 + (i * 3) % 27)
        extra = " ".join(random.choice(["Also", "Then", "Note", "Later", "Maybe"]) + " " + random.choice(
            ["call the bank.", "buy a lamp.", "read the Figma doc.", "the plant needs water.", "rain again.",
             "fika with Elin.", "renew the pass.", "check the tram times.", "stretch the knee.", "answer Priya."])
            for _ in range(random.randint(3, 12)))
        body = b + "\n\n" + extra
        sub = random.choice(["life", "work", "move"])
        WRITERS[k](os.path.join(notes, sub, "%s-%03d.%s" % (re.sub(r"[^a-z]+", "-", t.lower()).strip("-"), i, k)),
                   "%s %d" % (t, i), date, body)
        nfiles += 1
    # noise chats
    for i in range(90):
        q, a = CHAT_NOISE_QA[i % len(CHAT_NOISE_QA)]
        date = day(1 + (i * 5) % 10, 1 + (i * 11) % 27)
        cid = "c" + ("noise%04d" % i).ljust(26, "y")[:26]
        chats.append({"id": cid, "title": q[:48], "ts": int(date * 1000), "lane": "ai",
                      "messages": [{"role": "user", "content": q + (" (%d)" % i if i >= len(CHAT_NOISE_QA) else "")},
                                   {"role": "assistant", "content": a}]})
    with open(os.path.join(OUT, "chats.json"), "w", encoding="utf-8") as fh:
        json.dump(chats, fh, indent=1)
    with open(os.path.join(OUT, "questions.json"), "w", encoding="utf-8") as fh:
        json.dump({"as_of": "2026-10-05", "person": "Mara Lindqvist (fictional)", "questions": questions}, fh, indent=1)
    print("files", nfiles, "chats", len(chats), "questions", len(questions), "->", OUT)


if __name__ == "__main__":
    main()
