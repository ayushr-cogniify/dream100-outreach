"""
Knowledge-base loader.

Lets you supply the company knowledge base as ANY of:
    .md / .txt   plain text / markdown
    .csv         comma-separated (each row flattened to "col: value" lines)
    .xlsx / .xls Excel (every sheet, every row)
    .docx        Word (paragraphs + tables)
    .pdf         PDF (extracted text)

Both engines call load_knowledge_base(). If a file type needs a library that
isn't installed, you get a one-line "pip install X" message instead of a crash.
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path

# Extensions we know how to read, in the order we auto-detect them.
SUPPORTED = [".md", ".txt", ".csv", ".xlsx", ".xls", ".docx", ".pdf"]


def find_default_kb(directory: Path) -> Path | None:
    """Look for a file named company_kb.<ext> (any supported ext) in a folder."""
    for ext in SUPPORTED:
        candidate = directory / f"company_kb{ext}"
        if candidate.exists():
            return candidate
    return None


def load_knowledge_base(path: str | Path) -> str:
    """Read any supported file into a single plain-text string for the prompt."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Knowledge base not found: {path}")

    ext = path.suffix.lower()
    if ext in (".md", ".txt"):
        return path.read_text(encoding="utf-8")
    if ext == ".csv":
        return _load_csv(path)
    if ext in (".xlsx", ".xls"):
        return _load_excel(path)
    if ext == ".docx":
        return _load_docx(path)
    if ext == ".pdf":
        return _load_pdf(path)
    raise ValueError(f"Unsupported knowledge-base type '{ext}'. Supported: {', '.join(SUPPORTED)}")


def _load_csv(path: Path) -> str:
    lines: list[str] = []
    with open(path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(_csv.DictReader(f), 1):
            lines.append(f"--- Row {i} ---")
            lines.extend(f"{k}: {v}" for k, v in row.items())
    return "\n".join(lines)


def _load_excel(path: Path) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise SystemExit("Reading Excel needs openpyxl:  pip install openpyxl")
    wb = load_workbook(path, read_only=True, data_only=True)
    out: list[str] = []
    for ws in wb.worksheets:
        out.append(f"=== Sheet: {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                out.append(" | ".join(cells))
    return "\n".join(out)


def _load_docx(path: Path) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        raise SystemExit("Reading Word needs python-docx:  pip install python-docx")
    d = docx.Document(str(path))
    out = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:                      # include table content too
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                out.append(" | ".join(cells))
    return "\n".join(out)


def _load_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("Reading PDF needs pypdf:  pip install pypdf")
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)
