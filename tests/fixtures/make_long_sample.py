#!/usr/bin/env python3
"""Writes tests/fixtures/long_sample.md: a long, realistic markdown file for
the gauntlet's document checks (NOTES 6b522). A fictional household's
relocation plan, about 200,000 characters of headings, lists, tables, code
and a running log. Everything in it is invented: the town, the people, the
firms, the figures. Deterministic: the same bytes on every run (a fixed
seed, no clock, no environment), so the checks can rely on its markers.

Markers the checks read:
  - near the start: "The storage unit code is GRANITE-7."
  - in a code block: a literal </document> the wrapper must neutralise
  - in the last section: "The final walkthrough is booked for 14 March."

Run from the repo root:  python3 tests/fixtures/make_long_sample.py
"""
import datetime
import os
import random

R = random.Random(522)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "long_sample.md")
TARGET = 200_000

FROM, TO = "Ashford Vale", "Port Calder"
PEOPLE = ["Ines", "Tomas", "Mira", "Bo"]
FIRMS = ["Harbourline Movers", "Calder Keys Lettings", "Northgate Storage",
         "Tidewater Utilities", "Saltmarsh Broadband", "Quayside Removals"]
ROOMS = ["kitchen", "living room", "study", "main bedroom", "kids' room",
         "garage", "loft", "bathroom", "hallway", "garden shed"]
ITEMS = ["bookshelf", "desk", "sofa", "piano", "bicycle", "washing machine",
         "dining table", "wardrobe", "lamp", "rug", "crate of records",
         "tool chest", "plant stand", "mirror", "filing cabinet"]
TASKS = ["book the van", "measure the doorways", "cancel the gym",
         "forward the post", "photograph the meters", "label the boxes",
         "clean the oven", "back up the laptops", "return the library books",
         "renew the parking permit", "check the boiler", "pack the fragile set",
         "collect the school records", "transfer the GP registration",
         "arrange the key handover", "defrost the freezer"]
VERBS = ["confirmed", "postponed", "rebooked", "checked", "priced", "queried",
         "signed off", "noted", "chased", "settled"]
START = datetime.date(2027, 1, 4)


def para(n=4):
    out = []
    for _ in range(n):
        who = R.choice(PEOPLE)
        firm = R.choice(FIRMS)
        task = R.choice(TASKS)
        out.append("%s %s that we need to %s before the move, and %s %s the "
                   "quote at %d.%02d a day." % (
                       who, R.choice(["said", "noted", "pointed out", "agreed"]),
                       task, firm, R.choice(VERBS), R.randint(40, 400),
                       R.randint(0, 99)))
    return " ".join(out)


def table(rows, cols):
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "\n".join("| " + " | ".join(str(c) for c in r) + " |"
                            for r in rows) + "\n"


def section_overview():
    return ("# Relocation plan: %s to %s\n\n"
            "This is the working plan for moving the household from %s to %s "
            "over the winter. It is a living document: the log at the end grows "
            "every day.\n\n"
            "The storage unit code is GRANITE-7.\n\n"
            "## Goals\n\n"
            "- Keep the kids in school with no more than two missed days\n"
            "- Move the piano without a crane\n"
            "- Stay inside a budget of 18,400 for the whole move\n"
            "- Have broadband working on the first evening\n\n"
            "## People\n\n" % (FROM, TO, FROM, TO)
            + "\n".join("- **%s**: %s" % (p, r) for p, r in zip(
                PEOPLE, ["budget and contracts", "packing and the van",
                         "schools and records", "the garden and the shed"]))
            + "\n\n")


def section_timeline():
    rows = []
    for w in range(1, 17):
        rows.append(["Week %d" % w, R.choice(TASKS), R.choice(PEOPLE),
                     R.choice(["planned", "in progress", "done", "blocked"])])
    return "## Timeline\n\n" + table(rows, ["When", "Task", "Owner", "State"]) + "\n"


def section_budget():
    rows, total = [], 0
    for firm in FIRMS:
        amt = R.randint(300, 4200)
        total += amt
        rows.append([firm, R.choice(["deposit", "quote", "invoice"]), amt])
    rows.append(["**Total**", "", total])
    return "## Budget\n\n" + table(rows, ["Firm", "Kind", "Amount"]) + "\n"


def section_manifest():
    lines = ["## Shipping manifest\n", "```yaml"]
    for i in range(40):
        lines.append("- id: box-%03d\n  room: %s\n  contents: [%s, %s]\n  fragile: %s"
                     % (i + 1, R.choice(ROOMS), R.choice(ITEMS), R.choice(ITEMS),
                        R.choice(["true", "false"])))
    lines.append("```\n")
    return "\n".join(lines) + "\n"


def section_script():
    return ("## Label printer script\n\n"
            "The export tool wraps each label in XML; its output ends like this, "
            "which is why the plan carries a literal closing tag:\n\n"
            "```bash\n"
            "#!/bin/sh\n"
            "# print one label per box from the manifest\n"
            "for b in $(seq -w 1 40); do\n"
            "  printf '<document name=\"box-%s\">%s</document>\\n' \"$b\" \"$(date +%%F)\"\n"
            "done > labels.xml\n"
            "echo '</document>' >> labels.xml   # the closing tag the printer wants\n"
            "```\n\n")


def section_housing():
    out = ["## Housing options in %s\n" % TO]
    for i, street in enumerate(["Ferry Lane", "Rope Walk", "Signal Hill",
                                "Lantern Row", "Kiln Street"], 1):
        out.append("### Option %d: %d %s\n" % (i, R.randint(2, 90), street))
        out.append("- Rent: %d a month\n- Bedrooms: %d\n- Walk to school: %d min\n"
                   "- Notes: %s\n" % (R.randint(1100, 2400), R.randint(2, 4),
                                      R.randint(4, 25), para(1)))
    return "\n".join(out) + "\n"


def section_log_day(n):
    d = START + datetime.timedelta(days=n - 1)
    out = ["### Log, day %d (%s)\n" % (n, d.isoformat()), para(R.randint(2, 4)), ""]
    if R.random() < 0.5:
        out.append("Done today:\n")
        for _ in range(R.randint(2, 5)):
            out.append("- [x] %s (%s)" % (R.choice(TASKS), R.choice(PEOPLE)))
        out.append("")
    if R.random() < 0.3:
        out.append(table([[R.choice(ITEMS), R.choice(ROOMS), R.randint(1, 9)]
                          for _ in range(3)], ["Item", "Room", "Boxes"]))
    if R.random() < 0.2:
        out.append("> %s\n" % para(1))
    return "\n".join(out) + "\n"


def main():
    parts = [section_overview(), section_timeline(), section_budget(),
             section_housing(), section_manifest(), section_script(),
             "## Daily log\n\n"]
    size = sum(len(p) for p in parts)
    end = ("## Handover\n\n"
           "The final walkthrough is booked for 14 March. Bring the meter "
           "photos, both sets of keys and the signed inventory.\n")
    n = 0
    while size < TARGET - len(end):
        n += 1
        p = section_log_day(n)
        parts.append(p)
        size += len(p)
    parts.append(end)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(parts))
    print("%s: %d characters, %d log days" % (OUT, size + len(end), n))


if __name__ == "__main__":
    main()
