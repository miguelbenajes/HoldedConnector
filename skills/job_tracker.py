"""
Job Tracker Skill — Obsidian Job Dossier System
================================================
Creates and maintains an Obsidian note per project (identified by project_code).
Each note aggregates: quote PDF, shooting dates, expenses, client email, invoicing checklist.

Architecture:
  connector.py (sync) → jobs table (source of truth) → render note → Brain API → Obsidian vault

Path convention:
  ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}/{code}.md
  ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}/attachments/{code}_{doc_number}.pdf
"""
import os
import re
import json
import logging
import hashlib
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
BRAIN_API_URL = os.getenv("BRAIN_API_URL", "http://localhost:3100")
BRAIN_INTERNAL_KEY = os.getenv("BRAIN_INTERNAL_KEY", "")

# ── Date Parser ────────────────────────────────────────────────────────────────

def parse_shooting_dates(raw, reference_year):
    """Parse flexible shooting date strings into sorted ISO date list.

    Supported formats:
      17/3           → single date
      17/3-21/3      → range (same or cross month)
      17-18/3        → short range (shared month)
      17/3, 19/3     → comma-separated
      17, 18, 21/3   → list with trailing month
      28/12-3/1      → cross-year range

    Args:
        raw: date string from Holded line item description
        reference_year: year from the parent document date

    Returns:
        Sorted list of ISO date strings ["2026-03-17", ...]. Empty list on failure.
    """
    if not raw or not isinstance(raw, str):
        return []

    raw = raw.strip()
    if not raw:
        return []

    try:
        dates = set()
        segments = [s.strip() for s in raw.split(",")]

        # First pass: find the rightmost explicit month (for inheriting)
        last_month = None
        for seg in reversed(segments):
            m = re.search(r'(\d{1,2})/(\d{1,2})', seg)
            if m:
                last_month = int(m.group(2))
                break

        if last_month is None:
            return []

        for seg in segments:
            if not seg:
                continue

            # Check for range (contains - that's not inside a d/m pattern)
            range_match = re.match(
                r'^(\d{1,2}(?:/\d{1,2})?)\s*-\s*(\d{1,2}(?:/\d{1,2})?)$', seg
            )
            if range_match:
                start_str, end_str = range_match.group(1), range_match.group(2)
                start_date = _parse_single(start_str, last_month, reference_year)
                end_date = _parse_single(end_str, last_month, reference_year)
                if start_date and end_date:
                    # Cross-year: if end < start, bump end year
                    if end_date < start_date:
                        end_date = end_date.replace(year=end_date.year + 1)
                    current = start_date
                    while current <= end_date:
                        dates.add(current)
                        current += timedelta(days=1)
                    # Update last_month from this segment
                    if '/' in end_str:
                        last_month = int(end_str.split('/')[1])
                    elif '/' in start_str:
                        last_month = int(start_str.split('/')[1])
            else:
                # Single date
                d = _parse_single(seg.strip(), last_month, reference_year)
                if d:
                    dates.add(d)

        return [d.isoformat() for d in sorted(dates)]

    except Exception as e:
        logger.warning(f"Failed to parse shooting dates '{raw}': {e}")
        return []


def _parse_single(s, default_month, year):
    """Parse a single date token like '17/3' or '17' into a date object."""
    s = s.strip()
    if not s:
        return None

    if '/' in s:
        parts = s.split('/')
        try:
            day, month = int(parts[0]), int(parts[1])
            return date(year, month, day)
        except (ValueError, IndexError):
            return None
    else:
        try:
            day = int(s)
            return date(year, default_month, day)
        except (ValueError, TypeError):
            return None


# ── Quarter Helper ─────────────────────────────────────────────────────────────

def get_quarter(date_str):
    """Map ISO date string to quarter: '2026-03-17' → '1T_2026'."""
    d = date.fromisoformat(date_str)
    q = (d.month - 1) // 3 + 1
    return f"{q}T_{d.year}"


# ── Sanitizers ─────────────────────────────────────────────────────────────────

def sanitize_for_path(text):
    """Strip path-unsafe characters. Prevents path traversal."""
    if not text:
        return ""
    text = text.replace("..", "")
    text = re.sub(r'[/\\<>:"|?*]', '', text)
    text = text.replace(" ", "-")
    text = re.sub(r'-{2,}', '-', text)
    return text.strip("-")


def sanitize_for_markdown(text):
    """Escape markdown-active characters in user-supplied strings."""
    if not text:
        return ""
    for ch in ['\\', '`', '*', '_', '{', '}', '[', ']', '(', ')', '#', '+', '-', '.', '!', '|']:
        text = text.replace(ch, '\\' + ch)
    return text


# ── Note Renderer ──────────────────────────────────────────────────────────────

STATUS_EMOJI = {"open": "🟢", "shooting": "🎬", "invoiced": "📄", "closed": "✅"}

HOLDED_PDF_BASE = os.getenv("HOLDED_CONNECTOR_URL", "https://holded.coyoterent.com")


def render_job_note(job, expenses=None):
    """Render an Obsidian markdown note from job data + expenses.

    Pure function, no I/O. All user strings are sanitized for markdown.

    Args:
        job: dict with job fields from the jobs table
        expenses: list of dicts with date, name, amount, doc_number

    Returns:
        Complete markdown string including YAML frontmatter.
    """
    expenses = expenses or []
    code = job.get("project_code", "")
    client = sanitize_for_markdown(job.get("client_name") or "")
    email = job.get("client_email") or ""
    status = job.get("status") or "open"
    emoji = STATUS_EMOJI.get(status, "")
    raw_dates = job.get("shooting_dates_raw") or ""
    parsed_dates = job.get("shooting_dates") or "[]"
    estimate_id = job.get("estimate_id") or ""
    estimate_num = job.get("estimate_number") or ""
    invoice_id = job.get("invoice_id") or ""
    invoice_num = job.get("invoice_number") or ""
    created = job.get("created_at") or ""

    # PDF section
    safe_code = sanitize_for_path(code)
    safe_doc_num = sanitize_for_path(estimate_num or invoice_num or "")
    pdf_filename = f"{safe_code}_{safe_doc_num}.pdf" if safe_doc_num else ""

    doc_type = "estimates" if estimate_id else "invoices"
    doc_id = estimate_id or invoice_id
    pdf_url = f"{HOLDED_PDF_BASE}/api/entities/{doc_type}/{doc_id}/pdf" if doc_id else ""

    quote_section = ""
    if pdf_filename:
        quote_section = f"![[{pdf_filename}]]\n"
    if pdf_url:
        quote_section += f"[Download latest PDF]({pdf_url})\n"
    if not quote_section:
        quote_section = "*No quote attached yet*\n"

    # Expenses table
    expense_rows = ""
    total_expenses = 0.0
    for exp in expenses:
        exp_date = ""
        if exp.get("date"):
            try:
                exp_date = datetime.fromtimestamp(exp["date"]).strftime("%Y-%m-%d")
            except (OSError, ValueError):
                exp_date = str(exp["date"])
        exp_name = sanitize_for_markdown(exp.get("name") or "")
        exp_amount = float(exp.get("amount") or 0)
        exp_doc = exp.get("doc_number") or ""
        total_expenses += exp_amount
        expense_rows += f"| {exp_date} | {exp_name} | €{exp_amount:.2f} | {exp_doc} |\n"

    if not expense_rows:
        expense_rows = "| — | *No expenses yet* | — | — |\n"

    return f"""---
project_code: {code}
client: {job.get("client_name") or ""}
client_email: {email}
status: {status}
shooting_dates: {parsed_dates}
created: {created}
quote_id: {estimate_id}
quote_number: {estimate_num}
invoice_id: {invoice_id}
invoice_number: {invoice_num}
tags: [coyote, job, seguimiento]
---

# {code}

> **Client:** {client}
> **Shooting:** {raw_dates}
> **Status:** {emoji} {status}

---

## Quote
{quote_section}
## Expenses & Tickets

| Date | Concept | Amount | Source |
|------|---------|--------|--------|
{expense_rows}
**Total expenses:** €{total_expenses:.2f}

## Email Thread
- **Job ref for emails:** Include `REF: {code}` in subject line
- **Client contact:** {email}

## Notes


## Invoicing Checklist
- [ ] All shooting dates completed
- [ ] Expenses reviewed
- [ ] Equipment returned / damages checked
- [ ] Final invoice created
"""
