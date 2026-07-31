#!/usr/bin/env python3
"""
Dream 100 — local web app.

A small FastAPI server that wraps the outreach engine so you can drive it from a
browser: upload a knowledge base (any format), upload a contacts CSV, configure
accounts, generate the scheduled campaign, review it, and download the queue.

Run:
    python3 app.py
    # then open http://127.0.0.1:8000

Nothing is sent to LinkedIn. The app produces the reviewable, scheduled plan.
"""

from __future__ import annotations

import csv
import io
import os
import datetime as dt
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from kb_loader import SUPPORTED, load_knowledge_base
from dream100_engine import (
    DAILY_LIMITS, SEQUENCE, build_campaign, peak_invites_per_account_day,
)

HERE = Path(__file__).parent
UPLOADS = HERE / "uploads"
OUTPUT = HERE / "output"
UPLOADS.mkdir(exist_ok=True)
OUTPUT.mkdir(exist_ok=True)

app = FastAPI(title="Dream 100 Outreach Engine")

# Simple single-user local state: remember the last uploaded files + result.
STATE: dict = {"kb_path": None, "contacts_path": None, "rows": []}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status() -> dict:
    return {
        "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "kb": Path(STATE["kb_path"]).name if STATE["kb_path"] else None,
        "contacts": Path(STATE["contacts_path"]).name if STATE["contacts_path"] else None,
        "supported_kb": SUPPORTED,
        "sequence": [s["step"] for s in SEQUENCE],
        "daily_limits": DAILY_LIMITS,
    }


@app.post("/api/upload_kb")
async def upload_kb(file: UploadFile = File(...)) -> JSONResponse:
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED:
        return JSONResponse({"error": f"Unsupported type {ext}. Allowed: {', '.join(SUPPORTED)}"}, status_code=400)
    dest = UPLOADS / f"kb{ext}"
    dest.write_bytes(await file.read())
    try:
        text = load_knowledge_base(dest)
    except Exception as e:  # bad file, missing parser lib, etc.
        return JSONResponse({"error": str(e)}, status_code=400)
    STATE["kb_path"] = str(dest)
    return JSONResponse({"filename": file.filename, "chars": len(text),
                         "preview": text[:600]})


def _normalize_contacts(raw: str):
    """Accept either our native schema OR a LinkedIn 'Connections' export and
    return rows in the native schema. Skips preamble lines, blank rows, and rows
    with no name/URL. Returns (contacts, skipped, source_label)."""
    lines = raw.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        low = line.lower()
        if ("first name" in low and "last name" in low) or "linkedin_url" in low or low.startswith("name,"):
            header_idx = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    fields = [f.strip() for f in (reader.fieldnames or [])]
    is_linkedin = "First Name" in fields or "URL" in fields

    contacts, skipped = [], 0
    for row in reader:
        row = {(k.strip() if k else k): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if is_linkedin:
            name = f"{row.get('First Name','') or ''} {row.get('Last Name','') or ''}".strip()
            title = row.get("Position", "") or ""
            company = row.get("Company", "") or ""
            url = row.get("URL", "") or ""
        else:
            name = row.get("name", "") or ""
            title = row.get("title", "") or ""
            company = row.get("company", "") or ""
            url = row.get("linkedin_url", "") or ""
        notes = row.get("scraped_profile_notes", "") or ""
        if not notes:  # connection exports have no activity — synthesize a minimal note
            if title and company:
                notes = f"{title} at {company}"
            elif title or company:
                notes = title or f"Works at {company}"
        if not name or not url:
            skipped += 1
            continue
        contacts.append({"name": name, "title": title, "company": company,
                         "linkedin_url": url, "scraped_profile_notes": notes})
    return contacts, skipped, ("LinkedIn connections export" if is_linkedin else "standard CSV")


@app.post("/api/upload_contacts")
async def upload_contacts(file: UploadFile = File(...)) -> JSONResponse:
    raw = (await file.read()).decode("utf-8", errors="replace")
    contacts, skipped, source = _normalize_contacts(raw)
    if not contacts:
        return JSONResponse({"error": "No usable rows found. Upload a LinkedIn 'Connections' export "
                             "or a CSV with columns name,title,company,linkedin_url,scraped_profile_notes."},
                            status_code=400)
    dest = UPLOADS / "contacts.csv"
    fields = ["name", "title", "company", "linkedin_url", "scraped_profile_notes"]
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(contacts)
    STATE["contacts_path"] = str(dest)
    return JSONResponse({"filename": file.filename, "count": len(contacts), "skipped": skipped,
                         "source": source, "synthesized_notes": source.startswith("LinkedIn"),
                         "sample": [c["name"] for c in contacts[:5]]})


@app.post("/api/generate")
def generate(accounts: int = Form(6), start: str = Form(""), dry_run: bool = Form(True),
             limit: int = Form(25)) -> JSONResponse:
    if not STATE["kb_path"] or not STATE["contacts_path"]:
        return JSONResponse({"error": "Upload both a knowledge base and a contacts CSV first."}, status_code=400)
    kb_text = load_knowledge_base(STATE["kb_path"])
    with open(STATE["contacts_path"], newline="", encoding="utf-8") as f:
        contacts = list(csv.DictReader(f))
    total_available = len(contacts)
    if limit and limit > 0:
        contacts = contacts[:limit]           # cost cap: only process the first N
    start_date = dt.date.fromisoformat(start) if start else dt.date.today()

    if not dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        return JSONResponse({"error": "Live mode needs ANTHROPIC_API_KEY set. Use dry run, or set the key and restart."}, status_code=400)

    try:
        rows = build_campaign(contacts, kb_text, accounts=accounts, start=start_date, dry_run=dry_run)
    except Exception as e:
        return JSONResponse({"error": f"Generation failed: {e}"}, status_code=500)

    STATE["rows"] = rows
    # persist a downloadable copy
    fields = ["scheduled_at", "account", "channel", "step", "contact_name",
              "title", "company", "linkedin_url", "message", "status"]
    with open(OUTPUT / "outreach_queue.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    worst = peak_invites_per_account_day(rows)
    return JSONResponse({
        "count": len(rows),
        "contacts": len(contacts),
        "total_available": total_available,
        "steps": len(SEQUENCE),
        "accounts": accounts,
        "peak_invites_per_account_day": worst,
        "invite_cap": DAILY_LIMITS["linkedin_invite"],
        "cap_ok": worst <= DAILY_LIMITS["linkedin_invite"],
        "rows": rows,
    })


@app.get("/api/download")
def download() -> FileResponse:
    return FileResponse(OUTPUT / "outreach_queue.csv", filename="outreach_queue.csv",
                        media_type="text/csv")


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")     # hosts set 0.0.0.0
    port = int(os.environ.get("PORT", "8000"))     # Render/HF inject $PORT
    print(f"Dream 100 app running at  http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
