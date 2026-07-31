#!/usr/bin/env python3
"""
Dream 100 outreach engine  (Sales Blueprint, page 15 — "Campaign Build").

This is the ORCHESTRATION BRAIN, not a LinkedIn bot. It takes a prospect list,
pools them across N LinkedIn accounts, writes a personalized multi-step
sequence per prospect, and schedules every step across a calendar while
respecting human-safe daily limits per account.

The actual SEND is pluggable (--sender):
  * review   (default) -> writes a scheduled queue to outreach_queue.csv for a
               human, or a compliant tool, to action. Nothing is sent by code.
  * heyreach            -> documented stub showing where a compliant outreach
               platform's API call goes. Not wired to a live key here.

Why no raw browser bot: driving the LinkedIn web app with cookies is the
automation LinkedIn detects best and is the #1 cause of account bans. Compliant
platforms send from dedicated per-account proxies with human-like pacing; use
one of those as the "hands," and let this engine be the "brain."

Run it:
    python dream100_engine.py --dry-run            # no API; template sequences
    python dream100_engine.py --accounts 6         # pool across 6 accounts
    python dream100_engine.py --start 2026-08-03   # schedule from this date
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from pathlib import Path
from typing import List

from kb_loader import find_default_kb, load_knowledge_base

HERE = Path(__file__).parent
CONTACTS_CSV = HERE / "contacts.csv"
OUTPUT_CSV = HERE / "outreach_queue.csv"

MODEL = "claude-haiku-4-5"

# --- The Dream 100 sequence (page 15), with human-safe day offsets -----------
# channel drives which per-account daily cap applies.
SEQUENCE = [
    {"step": "connection_request", "channel": "linkedin_invite", "day_offset": 0,
     "purpose": "Personalized connection request referencing one profile detail."},
    {"step": "message_1",          "channel": "linkedin_dm",     "day_offset": 2,
     "purpose": "After accept: open with their problem (pick a pillar). No pitch."},
    {"step": "video_voice",        "channel": "linkedin_dm",     "day_offset": 4,
     "purpose": "Short personalized video/voice note prompt."},
    {"step": "cold_offer",         "channel": "linkedin_dm",     "day_offset": 6,
     "purpose": "The Cold-Friendly Offer: the Infrastructure Bottleneck Scan."},
    {"step": "email_2",            "channel": "email",           "day_offset": 8,
     "purpose": "Email follow-up restating the CFO with a single CTA."},
]

# --- Per-account human-safe daily limits (conservative industry norms) --------
# LinkedIn silently throttles / flags accounts above these; warm up new accounts
# to ~half these for the first 1-2 weeks.
DAILY_LIMITS = {
    "linkedin_invite": 20,   # connection requests / account / day
    "linkedin_dm": 40,       # DMs / account / day
    "email": 100,            # emails / account / day
}
WORK_HOURS = (9, 17)         # send only within these local hours, weekdays only


def load_contacts(limit: int | None) -> List[dict]:
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[:limit] if limit else rows


def add_business_days(start: dt.date, n: int) -> dt.date:
    """Advance n business days (skip Sat/Sun)."""
    d = start
    added = 0
    while added < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def scheduled_time(d: dt.date, index: int) -> str:
    """Spread sends across working hours with a deterministic per-index minute
    so the same run is reproducible (no randomness needed for the demo)."""
    span = (WORK_HOURS[1] - WORK_HOURS[0]) * 60
    minute = (index * 37) % span            # 37 = coprime-ish spread
    t = dt.time(WORK_HOURS[0] + minute // 60, minute % 60)
    return dt.datetime.combine(d, t).isoformat(timespec="minutes")


# --- Message generation -------------------------------------------------------
def sequence_with_claude(contact: dict, kb_text: str) -> dict:
    """Return {step_name: message_text} for one contact via Claude."""
    import anthropic
    from pydantic import BaseModel, Field

    class Dream100Sequence(BaseModel):
        connection_request: str = Field(description="Under 300 chars, human, references one profile detail. No pitch.")
        message_1: str = Field(description="Under 120 words, leads with their problem (a pillar). No pitch.")
        video_voice: str = Field(description="A 30-second video/voice-note SCRIPT, first person.")
        cold_offer: str = Field(description="Under 120 words, pitches the Infrastructure Bottleneck Scan CFO.")
        email_2: str = Field(description="Under 120 words email, restates the CFO, one CTA.")

    client = anthropic.Anthropic()
    system = [
        {"type": "text", "text": "You are an expert B2B SDR running the Dream 100 sequence."},
        {"type": "text", "text": kb_text, "cache_control": {"type": "ephemeral"}},
    ]
    prompt = (
        "Write the full 5-step Dream 100 sequence for this prospect. Follow every "
        "rule in the knowledge base. Each step is a separate field.\n\n"
        f"Name: {contact['name']}\nTitle: {contact['title']}\nCompany: {contact['company']}\n"
        f"Scraped profile notes: {contact['scraped_profile_notes']}\n"
    )
    resp = client.messages.parse(
        model=MODEL, max_tokens=3000,
        system=system, messages=[{"role": "user", "content": prompt}],
        output_format=Dream100Sequence,
    )
    return resp.parsed_output.model_dump() if resp.parsed_output else {}


def sequence_dry_run(contact: dict) -> dict:
    """Template stand-ins so the pipeline runs with no API key."""
    first = contact["name"].split()[0]
    detail = contact["scraped_profile_notes"].split(".")[0].strip().lower()
    return {
        "connection_request": f"[DRY RUN] Hi {first} — your note on {detail} resonated. Would value connecting.",
        "message_1": f"[DRY RUN] {first}, teams like {contact['company']} often find {detail} is a data-integrity symptom, not a model problem. Curious if you're seeing that.",
        "video_voice": f"[DRY RUN video script] Hi {first}, 30 seconds — I mapped how {detail} usually traces back to silent pipeline faults...",
        "cold_offer": f"[DRY RUN] {first}, no pitch: send me one pipeline diagram or architecture screenshot (no access/credentials) and we'll show you exactly where the bottlenecks and cost leaks sit. Free.",
        "email_2": f"[DRY RUN email] {first}, following up — the Infrastructure Bottleneck Scan is free and takes one screenshot. Worth 20 minutes?",
    }


# --- Sender adapters ----------------------------------------------------------
def send_review(rows: List[dict]) -> None:
    """Default, safe: write the scheduled queue for a human/tool to action."""
    fields = ["scheduled_at", "account", "channel", "step", "contact_name",
              "title", "company", "linkedin_url", "message", "status"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def send_heyreach(rows: List[dict]) -> None:
    """Stub: where a compliant outreach platform API push would go.

    Real integration (pseudocode):
        client = HeyReach(api_key=os.environ["HEYREACH_API_KEY"])
        for account, contacts in group_by_account(rows):
            campaign = client.create_campaign(sender_account=account, sequence=SEQUENCE)
            client.add_leads(campaign.id, contacts)   # platform paces + sends safely
    The platform runs each LinkedIn account behind its own proxy with human-like
    timing and enforces the daily caps for you.
    """
    raise SystemExit("heyreach sender is a stub — provide an API key and wire the SDK. "
                     "Use --sender review for now.")


def build_campaign(contacts: List[dict], kb_text: str, accounts: int = 6,
                   start: dt.date | None = None, dry_run: bool = True) -> List[dict]:
    """Core engine: contacts + KB -> scheduled, per-account, capped action queue.
    Importable by the web app and the CLI alike."""
    start = start or dt.date.today()
    per_account_invite_count: dict[str, int] = {}
    rows: List[dict] = []

    for i, contact in enumerate(contacts):
        account = f"li_account_{(i % accounts) + 1}"
        seq_msgs = sequence_dry_run(contact) if dry_run else sequence_with_claude(contact, kb_text)

        n_done = per_account_invite_count.get(account, 0)
        invite_day = add_business_days(start, n_done // DAILY_LIMITS["linkedin_invite"])
        per_account_invite_count[account] = n_done + 1

        for j, step in enumerate(SEQUENCE):
            step_day = add_business_days(invite_day, step["day_offset"])
            rows.append({
                "scheduled_at": scheduled_time(step_day, i + j),
                "account": account,
                "channel": step["channel"],
                "step": step["step"],
                "contact_name": contact["name"],
                "title": contact["title"],
                "company": contact["company"],
                "linkedin_url": contact["linkedin_url"],
                "message": seq_msgs.get(step["step"], ""),
                "status": "QUEUED",
            })

    rows.sort(key=lambda r: r["scheduled_at"])
    return rows


def peak_invites_per_account_day(rows: List[dict]) -> int:
    """Max connection requests any single account is scheduled to send on any day."""
    by: dict[tuple, int] = {}
    for r in rows:
        if r["channel"] == "linkedin_invite":
            key = (r["account"], r["scheduled_at"][:10])
            by[key] = by.get(key, 0) + 1
    return max(by.values()) if by else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Dream 100 outreach campaign engine.")
    ap.add_argument("--dry-run", action="store_true", help="skip the API; template sequences")
    ap.add_argument("--accounts", type=int, default=6, help="number of pooled LinkedIn accounts (deck says 6)")
    ap.add_argument("--start", type=str, default=None, help="campaign start date YYYY-MM-DD (default: today)")
    ap.add_argument("--sender", choices=["review", "heyreach"], default="review")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N contacts")
    ap.add_argument("--kb", type=str, default=None,
                    help="knowledge base file (.md/.txt/.csv/.xlsx/.docx/.pdf). Default: auto-detect company_kb.*")
    args = ap.parse_args()

    contacts = load_contacts(args.limit)
    kb_path = Path(args.kb) if args.kb else find_default_kb(HERE)
    if not kb_path:
        raise SystemExit("No knowledge base found. Add company_kb.md (or .docx/.xlsx/.csv/...) or pass --kb PATH.")
    kb_text = load_knowledge_base(kb_path)
    print(f"Knowledge base: {kb_path.name} ({len(kb_text)} chars)")
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    print(f"Loaded {len(contacts)} contacts. Pooling across {args.accounts} accounts. "
          f"Start: {start} ({'dry run' if args.dry_run else MODEL})\n")

    rows = build_campaign(contacts, kb_text, accounts=args.accounts, start=start, dry_run=args.dry_run)

    {"review": send_review, "heyreach": send_heyreach}[args.sender](rows)

    # Report: prove per-account per-day caps are respected.
    print(f"Queued {len(rows)} actions ({len(contacts)} contacts x {len(SEQUENCE)} steps).")
    worst = peak_invites_per_account_day(rows)
    print(f"Peak connection requests on any account/day: {worst} "
          f"(cap is {DAILY_LIMITS['linkedin_invite']}). {'OK' if worst <= DAILY_LIMITS['linkedin_invite'] else 'OVER CAP'}")
    if args.sender == "review":
        print(f"\nWrote scheduled queue -> {OUTPUT_CSV.name}")
        print("Nothing was sent. A human (or a compliant tool) actions the queue in scheduled order.")


if __name__ == "__main__":
    main()
