# B2B Outreach Demo — AI drafts, humans send

A proof-of-concept for the workflow we sketched out: use AI to do the
**research + writing** for personalized sales outreach, but keep a **human
"send" step** so LinkedIn never sees a bot on your account.

## The flow

```
contacts.csv          company_kb.md
(scraped exec info)   (your internal positioning/strategy docs)
        \                   /
         \                 /
          v               v
        generate_outreach.py   ->  Claude reads both, writes a tailored
                                    email + LinkedIn message per person
                    |
                    v
        outreach_review.csv    <-  every row = "PENDING REVIEW"
                    |
                    v
        A HUMAN reviews, approves, and sends manually.
        The AI never logs into LinkedIn or email.
```

That last step is the whole safety story: automation-based scraping/sending is
what gets accounts banned. Here the AI only produces a spreadsheet.

## Files

| File | What it is |
|------|-----------|
| `contacts.csv` | Sample "scraped" executives (VPs/SVPs/Directors). Replace with your real list. |
| `company_kb.md` | Your internal sales knowledge base — positioning, proof points, tone rules. |
| `generate_outreach.py` | The pipeline. Reads the two files, drafts outreach, writes the review sheet. |
| `outreach_review.csv` | **Output.** The spreadsheet a human reviews before sending. |

## Run it

```bash
# See the whole pipeline for free (no API key, template drafts):
python3 generate_outreach.py --dry-run

# Real personalized drafts (needs a Claude API key):
export ANTHROPIC_API_KEY=sk-ant-...
python3 -m pip install anthropic pydantic
python3 generate_outreach.py

# Just the first 2 contacts:
python3 generate_outreach.py --limit 2
```

## How to make it yours
1. Replace `contacts.csv` with your real scraped/exported contacts.
2. Rewrite `company_kb.md` with your product, proof points, and tone rules.
3. Run it, open `outreach_review.csv`, review, approve, send.

## Notes on the design
- **Prompt caching**: the knowledge base is sent once and cached, so contacts
  2..N are much cheaper than contact 1.
- **Structured output**: each contact returns clean fields (subject, body,
  LinkedIn message, rationale) — no parsing of free-form text.
- **Fail-soft**: if a draft can't be generated for one contact, that row is
  flagged and the run continues.
- **Human-in-the-loop is enforced by shape**: the script only writes a sheet.
  Sending is a separate, manual, human action by design.
