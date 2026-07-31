#!/usr/bin/env python3
"""
B2B outreach pipeline — proof of concept.

The workflow (from the brainstorm):
    scraped contacts + internal docs  ->  AI drafts personalized outreach
    ->  everything lands in a spreadsheet for a HUMAN to review before sending.

The AI never touches LinkedIn or email directly. That "spreadsheet buffer" is
the whole point: it keeps your real accounts safe from automation bans, because
a person does the actual sending after reviewing each draft.

Run it:
    python generate_outreach.py              # real drafts via the Claude API
    python generate_outreach.py --dry-run    # no API call; template stand-ins
    python generate_outreach.py --limit 2    # only process the first 2 contacts

Auth: the Claude SDK reads ANTHROPIC_API_KEY, or an `ant auth login` profile.
You do not pass a key to this script.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import List

from kb_loader import find_default_kb, load_knowledge_base

HERE = Path(__file__).parent
CONTACTS_CSV = HERE / "contacts.csv"
OUTPUT_CSV = HERE / "outreach_review.csv"

MODEL = "claude-haiku-4-5"


# --- The structured shape we force the model to return, one per contact -------
try:
    from pydantic import BaseModel, Field

    class Outreach(BaseModel):
        email_subject: str = Field(description="Under 8 words, specific, no clickbait.")
        email_body: str = Field(description="Under 120 words. Leads with their problem.")
        linkedin_message: str = Field(description="Under 300 characters, human-sounding.")
        personalization_rationale: str = Field(
            description="1 sentence: which profile detail drove the personalization."
        )
except ImportError:  # pydantic missing — only needed for the real (non-dry-run) path
    Outreach = None


def load_contacts(limit: int | None) -> List[dict]:
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit else rows


def build_user_prompt(contact: dict) -> str:
    return (
        "Draft outreach for this prospect. Follow every rule in the knowledge base.\n\n"
        f"Name: {contact['name']}\n"
        f"Title: {contact['title']}\n"
        f"Company: {contact['company']}\n"
        f"What we scraped from their profile: {contact['scraped_profile_notes']}\n"
    )


def generate_with_claude(contacts: List[dict], kb_text: str) -> List[dict]:
    """Call Claude once per contact. The knowledge base is cached so we only pay
    to process it once across the whole run, not once per contact."""
    import anthropic

    if Outreach is None:
        sys.exit("pydantic is required for the real run: pip install anthropic pydantic")

    client = anthropic.Anthropic()  # resolves key/profile from the environment

    # The KB is identical for every contact -> put it in a cached system block.
    system = [
        {"type": "text", "text": "You are an expert B2B SDR writing hyper-personalized cold outreach."},
        {"type": "text", "text": kb_text, "cache_control": {"type": "ephemeral"}},
    ]

    results = []
    for i, contact in enumerate(contacts, 1):
        print(f"  [{i}/{len(contacts)}] drafting for {contact['name']} @ {contact['company']}...")
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": build_user_prompt(contact)}],
            output_format=Outreach,
        )
        draft = resp.parsed_output
        if draft is None:  # e.g. a safety refusal — flag it instead of crashing the run
            results.append(_row(contact, "GENERATION FAILED", "", "", "model returned no structured output"))
            continue
        results.append(
            _row(contact, draft.email_subject, draft.email_body,
                 draft.linkedin_message, draft.personalization_rationale)
        )
        cached = resp.usage.cache_read_input_tokens or 0
        if i == 1 and cached == 0:
            print("     (knowledge base written to cache; later contacts read it cheaply)")
    return results


def generate_dry_run(contacts: List[dict], kb_text: str) -> List[dict]:
    """No API call — shows the pipeline shape and output format for free."""
    results = []
    for contact in contacts:
        first = contact["name"].split()[0]
        detail = contact["scraped_profile_notes"].split(".")[0]
        results.append(_row(
            contact,
            email_subject=f"[DRY RUN] {contact['company']} + a quick idea",
            email_body=(f"[DRY RUN — no AI was called] Hi {first}, noticed: {detail}. "
                        "This is where Claude would write a ~120-word personalized email."),
            linkedin_message=f"[DRY RUN] Hi {first} — saw your note on {detail.lower()}. Worth a quick chat?",
            rationale=f"Would personalize around: {detail}",
        ))
    return results


def _row(contact, email_subject, email_body, linkedin_message, rationale) -> dict:
    return {
        "name": contact["name"],
        "title": contact["title"],
        "company": contact["company"],
        "linkedin_url": contact["linkedin_url"],
        "email_subject": email_subject,
        "email_body": email_body,
        "linkedin_message": linkedin_message,
        "personalization_rationale": rationale,
        "status": "PENDING REVIEW",   # <-- the human-in-the-loop gate
        "approved_by": "",
        "sent": "FALSE",
    }


def write_output(rows: List[dict]) -> None:
    fields = ["name", "title", "company", "linkedin_url", "email_subject",
              "email_body", "linkedin_message", "personalization_rationale",
              "status", "approved_by", "sent"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate reviewable B2B outreach drafts.")
    ap.add_argument("--dry-run", action="store_true", help="skip the API; emit template drafts")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N contacts")
    ap.add_argument("--kb", type=str, default=None,
                    help="knowledge base file (.md/.txt/.csv/.xlsx/.docx/.pdf). Default: auto-detect company_kb.*")
    args = ap.parse_args()

    contacts = load_contacts(args.limit)
    kb_path = Path(args.kb) if args.kb else find_default_kb(HERE)
    if not kb_path:
        raise SystemExit("No knowledge base found. Add company_kb.md (or .docx/.xlsx/.csv/...) or pass --kb PATH.")
    kb_text = load_knowledge_base(kb_path)
    print(f"Loaded {len(contacts)} contacts and knowledge base {kb_path.name} ({len(kb_text)} chars).\n")

    if args.dry_run:
        print("DRY RUN — no API calls.\n")
        rows = generate_dry_run(contacts, kb_text)
    else:
        print(f"Generating drafts with {MODEL}...\n")
        rows = generate_with_claude(contacts, kb_text)

    write_output(rows)
    print(f"\nDone. {len(rows)} drafts written to {OUTPUT_CSV.name}")
    print("Next step: a human opens the sheet, reviews each row, sets status to")
    print("APPROVED, and sends manually. The AI never touches LinkedIn or email.")


if __name__ == "__main__":
    main()
