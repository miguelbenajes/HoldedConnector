# Job Tracker Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automated Obsidian job dossier system — one note per project with quote PDF, shooting dates, expenses, and invoicing checklist.

**Architecture:** `connector.py` extracts `project_code` + `shooting_dates` from Holded line items during sync → upserts into `jobs` table → queues Obsidian note creation → `flush_note_queue()` pushes notes + PDFs to Obsidian via Brain's `/internal/obsidian-write` endpoint.

**Tech Stack:** Python (FastAPI), PostgreSQL/SQLite, Brain (Hono/TypeScript), Obsidian vault

**Spec:** `docs/superpowers/specs/2026-03-12-job-tracker-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `skills/job_tracker.py` | **Create** | Date parser, sanitizers, ensure_job, render_note, sync_to_obsidian, flush_queue |
| `tests/test_job_tracker.py` | **Create** | Unit tests for date parser, sanitizers, render_note, ensure_job |
| `connector.py` | Modify | SHOOTING_DATES constants, `_extract_shooting_dates()`, DB tables + migrations, sync integration, queue flush |
| `api.py` | Modify | 6 new `/api/jobs/*` endpoints |
| `auth.py` | Modify | Add job routes to PERMISSION_MATRIX |
| `write_gateway.py` | Modify | Add `flush_note_queue()` call in `_sync_back_async()` |
| `.env.example` | Modify | Add BRAIN_API_URL, BRAIN_INTERNAL_KEY |
| `services/brain/src/routes/internal.ts` | Modify | Add `/internal/obsidian-write` route |
| `services/brain/src/skills/holded.ts` | Modify | Add job query types |
| `CLAUDE.md` | Modify | Document job tracker |

---

## Chunk 1: Core Skill — Date Parser & Sanitizers

### Task 1: Date Parser

**Files:**
- Create: `skills/job_tracker.py`
- Create: `tests/test_job_tracker.py`

- [ ] **Step 1: Create `skills/__init__.py`**

```bash
touch "skills/__init__.py"
```

- [ ] **Step 2: Write failing tests for `parse_shooting_dates`**

Create `tests/test_job_tracker.py`:

```python
"""Unit tests for skills/job_tracker.py — date parser, sanitizers, note renderer."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.job_tracker import parse_shooting_dates


class TestParseShootingDates:
    """Flexible date parser for Holded 'Shooting Dates:' line item descriptions."""

    def test_single_date(self):
        assert parse_shooting_dates("17/3", 2026) == ["2026-03-17"]

    def test_range_same_month(self):
        result = parse_shooting_dates("17/3-21/3", 2026)
        assert result == ["2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20", "2026-03-21"]

    def test_short_range_shared_month(self):
        assert parse_shooting_dates("17-18/3", 2026) == ["2026-03-17", "2026-03-18"]

    def test_comma_separated(self):
        assert parse_shooting_dates("17/3, 19/3, 21/3", 2026) == [
            "2026-03-17", "2026-03-19", "2026-03-21"
        ]

    def test_list_with_trailing_month(self):
        assert parse_shooting_dates("17, 18, 21/3", 2026) == [
            "2026-03-17", "2026-03-18", "2026-03-21"
        ]

    def test_cross_month_range(self):
        result = parse_shooting_dates("30/3-2/4", 2026)
        assert result == ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02"]

    def test_cross_year_range(self):
        result = parse_shooting_dates("28/12-3/1", 2026)
        assert result[0] == "2026-12-28"
        assert result[-1] == "2027-01-03"
        assert len(result) == 7  # 28,29,30,31 Dec + 1,2,3 Jan

    def test_empty_string(self):
        assert parse_shooting_dates("", 2026) == []

    def test_none(self):
        assert parse_shooting_dates(None, 2026) == []

    def test_garbage_input(self):
        assert parse_shooting_dates("not a date", 2026) == []

    def test_whitespace_handling(self):
        assert parse_shooting_dates("  17/3 - 19/3  ", 2026) == [
            "2026-03-17", "2026-03-18", "2026-03-19"
        ]

    def test_deduplication(self):
        result = parse_shooting_dates("17/3, 17/3", 2026)
        assert result == ["2026-03-17"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py -v`
Expected: FAIL (import error — `skills.job_tracker` doesn't exist yet)

- [ ] **Step 4: Implement `parse_shooting_dates` in `skills/job_tracker.py`**

Create `skills/job_tracker.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add skills/__init__.py skills/job_tracker.py tests/test_job_tracker.py
git commit -m "feat(job-tracker): add shooting date parser with tests"
```

---

### Task 2: Sanitizers & Quarter Helper

**Files:**
- Modify: `skills/job_tracker.py`
- Modify: `tests/test_job_tracker.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_job_tracker.py`:

```python
from skills.job_tracker import get_quarter, sanitize_for_path, sanitize_for_markdown


class TestGetQuarter:
    def test_q1(self):
        assert get_quarter("2026-02-15") == "1T_2026"

    def test_q2(self):
        assert get_quarter("2026-05-01") == "2T_2026"

    def test_q3(self):
        assert get_quarter("2026-09-30") == "3T_2026"

    def test_q4(self):
        assert get_quarter("2026-12-31") == "4T_2026"

    def test_boundary_march(self):
        assert get_quarter("2026-03-31") == "1T_2026"

    def test_boundary_april(self):
        assert get_quarter("2026-04-01") == "2T_2026"


class TestSanitizeForPath:
    def test_normal(self):
        assert sanitize_for_path("NETFLIX-260312") == "NETFLIX-260312"

    def test_spaces(self):
        assert sanitize_for_path("MY PROJECT") == "MY-PROJECT"

    def test_path_traversal(self):
        result = sanitize_for_path("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result

    def test_special_chars(self):
        result = sanitize_for_path('test<>:"|?*file')
        assert "<" not in result
        assert ">" not in result

    def test_empty(self):
        assert sanitize_for_path("") == ""


class TestSanitizeForMarkdown:
    def test_normal_text(self):
        assert sanitize_for_markdown("Hello World") == "Hello World"

    def test_brackets(self):
        result = sanitize_for_markdown("[link](url)")
        assert "[" not in result or "\\[" in result

    def test_pipe(self):
        result = sanitize_for_markdown("col1 | col2")
        assert "\\|" in result
```

- [ ] **Step 2: Run tests — expect failures**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py -v -k "Quarter or Sanitize"`
Expected: FAIL (functions not defined)

- [ ] **Step 3: Implement in `skills/job_tracker.py`**

Add after the date parser section:

```python
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
    # Remove path traversal patterns
    text = text.replace("..", "")
    # Remove unsafe chars
    text = re.sub(r'[/\\<>:"|?*]', '', text)
    # Spaces to dashes
    text = text.replace(" ", "-")
    # Collapse multiple dashes
    text = re.sub(r'-{2,}', '-', text)
    return text.strip("-")


def sanitize_for_markdown(text):
    """Escape markdown-active characters in user-supplied strings."""
    if not text:
        return ""
    for ch in ['\\', '`', '*', '_', '{', '}', '[', ']', '(', ')', '#', '+', '-', '.', '!', '|']:
        text = text.replace(ch, '\\' + ch)
    return text
```

- [ ] **Step 4: Run tests — expect pass**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add skills/job_tracker.py tests/test_job_tracker.py
git commit -m "feat(job-tracker): add quarter helper and sanitizers with tests"
```

---

### Task 3: Note Renderer

**Files:**
- Modify: `skills/job_tracker.py`
- Modify: `tests/test_job_tracker.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_job_tracker.py`:

```python
from skills.job_tracker import render_job_note


class TestRenderJobNote:
    def test_basic_render(self):
        job = {
            "project_code": "NETFLIX-260315",
            "client_name": "Netflix Spain",
            "client_email": "prod@netflix.com",
            "status": "open",
            "shooting_dates_raw": "15/3-18/3",
            "shooting_dates": '["2026-03-15", "2026-03-16", "2026-03-17", "2026-03-18"]',
            "created_at": "2026-03-12",
            "estimate_id": "abc123",
            "estimate_number": "QUOTE-26/0015",
            "invoice_id": None,
            "invoice_number": None,
        }
        result = render_job_note(job, expenses=[])
        assert "NETFLIX-260315" in result
        assert "Netflix Spain" in result
        assert "prod@netflix.com" in result
        assert "project_code: NETFLIX-260315" in result  # frontmatter
        assert "## Quote" in result
        assert "## Expenses" in result
        assert "## Invoicing Checklist" in result

    def test_with_expenses(self):
        job = {
            "project_code": "TEST-260101",
            "client_name": "Test Client",
            "client_email": "",
            "status": "shooting",
            "shooting_dates_raw": "1/1",
            "shooting_dates": '["2026-01-01"]',
            "created_at": "2026-01-01",
            "estimate_id": None,
            "estimate_number": None,
            "invoice_id": None,
            "invoice_number": None,
        }
        expenses = [
            {"date": 1704067200, "name": "Taxi", "amount": 25.0, "doc_number": "EXP-001"},
            {"date": 1704153600, "name": "Lunch", "amount": 18.5, "doc_number": "EXP-002"},
        ]
        result = render_job_note(job, expenses)
        assert "Taxi" in result
        assert "25" in result
        assert "43.5" in result or "43.50" in result  # total

    def test_status_emoji(self):
        for status, emoji in [("open", "🟢"), ("shooting", "🎬"), ("invoiced", "📄"), ("closed", "✅")]:
            job = {
                "project_code": "T-1", "client_name": "", "client_email": "",
                "status": status, "shooting_dates_raw": "", "shooting_dates": "[]",
                "created_at": "", "estimate_id": None, "estimate_number": None,
                "invoice_id": None, "invoice_number": None,
            }
            result = render_job_note(job, [])
            assert emoji in result
```

- [ ] **Step 2: Run tests — expect failures**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py::TestRenderJobNote -v`
Expected: FAIL

- [ ] **Step 3: Implement `render_job_note` in `skills/job_tracker.py`**

Add after sanitizers section:

```python
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
```

- [ ] **Step 4: Run tests — expect pass**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add skills/job_tracker.py tests/test_job_tracker.py
git commit -m "feat(job-tracker): add Obsidian note renderer with tests"
```

---

## Chunk 2: Database & Connector Integration

### Task 4: Database Tables & Shooting Dates Extraction

**Files:**
- Modify: `connector.py` (lines ~20, ~550, ~600, ~845)

- [ ] **Step 1: Add constants after existing PROYECTO constants**

In `connector.py`, after `PROYECTO_PRODUCT_NAME = "proyect ref:"` (line ~20), add:

```python
SHOOTING_DATES_PRODUCT_ID = "69b2cfcd0df77ff4010e4ac8"
SHOOTING_DATES_PRODUCT_NAME = "shooting dates:"  # lowercase for case-insensitive
```

- [ ] **Step 2: Add `_extract_shooting_dates` helper**

After the existing `_extract_project_code()` function (near line ~745), add:

```python
def _extract_shooting_dates(products):
    """Scan line items for 'Shooting Dates:' product and return its desc.
    Detection: by productId (reliable) or by name (fallback, case-insensitive)."""
    for prod in (products or []):
        pid = prod.get('productId')
        name = (prod.get('name') or '').strip().lower()
        if pid == SHOOTING_DATES_PRODUCT_ID or name == SHOOTING_DATES_PRODUCT_NAME:
            return (prod.get('desc') or '').strip() or None
    return None
```

- [ ] **Step 3: Add `jobs` and `job_note_queue` tables in `init_db()`**

In `connector.py`, inside `init_db()`, after the last `CREATE TABLE IF NOT EXISTS` (before the ALTER TABLE migration section), add:

```python
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS jobs (
            project_code TEXT PRIMARY KEY,
            client_id TEXT,
            client_name TEXT,
            client_email TEXT,
            status TEXT DEFAULT 'open',
            shooting_dates_raw TEXT,
            shooting_dates TEXT,
            quarter TEXT,
            estimate_id TEXT,
            estimate_number TEXT,
            invoice_id TEXT,
            invoice_number TEXT,
            note_path TEXT,
            pdf_hash TEXT,
            created_at TEXT DEFAULT {_now},
            updated_at TEXT DEFAULT {_now}
        )
    ''')

    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS job_note_queue (
            id {_serial},
            project_code TEXT NOT NULL,
            action TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TEXT DEFAULT {_now},
            processed_at TEXT
        )
    ''')
```

- [ ] **Step 4: Add ALTER TABLE migrations for `shooting_dates_raw`**

In the SQLite migration block (the `for tbl, col, defn in [...]` list), add:

```python
            ("invoices",          "shooting_dates_raw", "TEXT"),
            ("purchase_invoices", "shooting_dates_raw", "TEXT"),
            ("estimates",         "shooting_dates_raw", "TEXT"),
```

In the PostgreSQL migration block (the `for stmt in [...]` list), add:

```python
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
            "ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
            "ALTER TABLE estimates ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
```

Also add the partial index for PostgreSQL (inside the PG-only block):

```python
        try:
            cursor.execute("""CREATE INDEX IF NOT EXISTS idx_job_queue_pending
                ON job_note_queue (created_at) WHERE processed_at IS NULL AND retry_count < 5""")
        except Exception:
            pass
```

- [ ] **Step 5: Test migration applies**

Run:
```bash
cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -c "
import connector
connector.init_db()
conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute('SELECT project_code FROM jobs LIMIT 1')
print('jobs table: OK')
cur.execute('SELECT project_code, action FROM job_note_queue LIMIT 1')
print('job_note_queue table: OK')
cur.execute('SELECT shooting_dates_raw FROM invoices LIMIT 1')
print('invoices.shooting_dates_raw: OK')
cur.execute('SELECT shooting_dates_raw FROM estimates LIMIT 1')
print('estimates.shooting_dates_raw: OK')
conn.close()
print('All migrations applied')
"
```
Expected: All OK

- [ ] **Step 6: Commit**

```bash
git add connector.py
git commit -m "feat(job-tracker): add jobs tables, shooting dates extraction, DB migrations"
```

---

### Task 5: Sync Integration — Shooting Dates + ensure_job()

**Files:**
- Create: `skills/job_tracker.py` (add `ensure_job` function)
- Modify: `connector.py` (sync_documents + _upsert_single_document)
- Modify: `tests/test_job_tracker.py`

- [ ] **Step 1: Write failing test for `ensure_job`**

Add to `tests/test_job_tracker.py`:

```python
from skills.job_tracker import ensure_job
import connector


class TestEnsureJob:
    """Test job upsert logic. Requires DB connection."""

    def setup_method(self):
        connector.init_db()
        self.conn = connector.get_db()
        self.cur = connector._cursor(self.conn)

    def teardown_method(self):
        # Clean up test data
        self.cur.execute(connector._q("DELETE FROM jobs WHERE project_code LIKE 'TEST-%'"))
        self.cur.execute(connector._q("DELETE FROM job_note_queue WHERE project_code LIKE 'TEST-%'"))
        self.conn.commit()
        connector.release_db(self.conn)

    def test_create_new_job(self):
        doc_data = {
            "client_id": "contact123",
            "client_name": "Test Client",
            "shooting_dates_raw": "17/3-21/3",
            "estimate_id": "est123",
            "estimate_number": "QUOTE-26/TEST",
            "invoice_id": None,
            "invoice_number": None,
            "doc_date": 1742169600,  # 2025-03-17 approx
        }
        result = ensure_job("TEST-260317", doc_data, self.cur)
        self.conn.commit()
        assert result["project_code"] == "TEST-260317"
        assert result["status"] == "open"
        assert result["estimate_id"] == "est123"

    def test_update_existing_job_with_invoice(self):
        # First create
        doc_data = {
            "client_id": "c1", "client_name": "Client",
            "shooting_dates_raw": "1/4", "estimate_id": "est1",
            "estimate_number": "Q-1", "invoice_id": None,
            "invoice_number": None, "doc_date": 1742169600,
        }
        ensure_job("TEST-UPDATE", doc_data, self.cur)
        self.conn.commit()

        # Then update with invoice
        doc_data2 = {
            "client_id": "c1", "client_name": "Client",
            "shooting_dates_raw": "1/4", "estimate_id": None,
            "estimate_number": None, "invoice_id": "inv1",
            "invoice_number": "INV-1", "doc_date": 1742169600,
        }
        result = ensure_job("TEST-UPDATE", doc_data2, self.cur)
        self.conn.commit()

        assert result["estimate_id"] == "est1"  # preserved
        assert result["invoice_id"] == "inv1"    # added
        assert result["status"] == "invoiced"    # auto-transition
```

- [ ] **Step 2: Run tests — expect failures**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py::TestEnsureJob -v`
Expected: FAIL

- [ ] **Step 3: Implement `ensure_job` in `skills/job_tracker.py`**

Add after the note renderer section:

```python
# ── Job Management ─────────────────────────────────────────────────────────────

# Import connector helpers at function level to avoid circular imports
def _get_connector():
    import connector
    return connector


def ensure_job(project_code, doc_data, cursor):
    """Upsert a job in the jobs table. Creates or updates based on project_code.

    Merge strategy:
      - estimate_id/invoice_id: update only if new value is non-None
      - shooting_dates: always update to latest
      - status: auto-transition to 'invoiced' when invoice_id set; otherwise no change

    Args:
        project_code: job identifier (e.g. 'NETFLIX-260315')
        doc_data: dict with client_id, client_name, shooting_dates_raw,
                  estimate_id, estimate_number, invoice_id, invoice_number, doc_date
        cursor: DB cursor (from connector.get_db())

    Returns:
        Dict with the job row data.
    """
    conn_mod = _get_connector()

    # Parse shooting dates
    ref_year = datetime.now().year
    if doc_data.get("doc_date"):
        try:
            ref_year = datetime.fromtimestamp(doc_data["doc_date"]).year
        except (OSError, ValueError):
            pass

    shooting_raw = doc_data.get("shooting_dates_raw") or ""
    parsed_dates = parse_shooting_dates(shooting_raw, ref_year)
    parsed_json = json.dumps(parsed_dates)

    # Derive quarter from first shooting date, fallback to doc date, then current
    quarter = None
    if parsed_dates:
        quarter = get_quarter(parsed_dates[0])
    elif doc_data.get("doc_date"):
        try:
            dt = datetime.fromtimestamp(doc_data["doc_date"])
            quarter = get_quarter(dt.strftime("%Y-%m-%d"))
        except (OSError, ValueError):
            pass
    if not quarter:
        quarter = get_quarter(date.today().isoformat())

    # Look up client email from contacts table
    client_email = ""
    client_id = doc_data.get("client_id")
    if client_id:
        try:
            cursor.execute(conn_mod._q(
                "SELECT email FROM contacts WHERE id = ?"
            ), (client_id,))
            row = cursor.fetchone()
            if row:
                client_email = (row["email"] if isinstance(row, dict) else row[0]) or ""
        except Exception:
            pass

    # Check if job exists
    cursor.execute(conn_mod._q(
        "SELECT * FROM jobs WHERE project_code = ?"
    ), (project_code,))
    existing = cursor.fetchone()

    if existing:
        # Convert to dict if needed
        if not isinstance(existing, dict):
            cols = [d[0] for d in cursor.description]
            existing = dict(zip(cols, existing))

        # Merge: don't overwrite non-None fields with None
        est_id = doc_data.get("estimate_id") or existing.get("estimate_id")
        est_num = doc_data.get("estimate_number") or existing.get("estimate_number")
        inv_id = doc_data.get("invoice_id") or existing.get("invoice_id")
        inv_num = doc_data.get("invoice_number") or existing.get("invoice_number")

        # Auto-transition to invoiced
        status = existing.get("status", "open")
        if inv_id and status in ("open", "shooting"):
            status = "invoiced"

        now_str = datetime.now().isoformat()
        cursor.execute(conn_mod._q("""
            UPDATE jobs SET
                client_id = ?, client_name = ?, client_email = ?,
                shooting_dates_raw = ?, shooting_dates = ?, quarter = ?,
                estimate_id = ?, estimate_number = ?,
                invoice_id = ?, invoice_number = ?,
                status = ?, updated_at = ?
            WHERE project_code = ?
        """), (
            client_id or existing.get("client_id"),
            doc_data.get("client_name") or existing.get("client_name"),
            client_email or existing.get("client_email"),
            shooting_raw or existing.get("shooting_dates_raw"),
            parsed_json, quarter,
            est_id, est_num, inv_id, inv_num,
            status, now_str, project_code,
        ))
        action = "update"
    else:
        # Insert new job
        now_str = datetime.now().isoformat()
        cursor.execute(conn_mod._q("""
            INSERT INTO jobs (
                project_code, client_id, client_name, client_email,
                status, shooting_dates_raw, shooting_dates, quarter,
                estimate_id, estimate_number, invoice_id, invoice_number,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """), (
            project_code, client_id,
            doc_data.get("client_name"), client_email,
            "open", shooting_raw, parsed_json, quarter,
            doc_data.get("estimate_id"), doc_data.get("estimate_number"),
            doc_data.get("invoice_id"), doc_data.get("invoice_number"),
            now_str, now_str,
        ))
        action = "create"

    # Queue note sync
    cursor.execute(conn_mod._q("""
        INSERT INTO job_note_queue (project_code, action) VALUES (?, ?)
    """), (project_code, action))

    # Fetch and return the row
    cursor.execute(conn_mod._q(
        "SELECT * FROM jobs WHERE project_code = ?"
    ), (project_code,))
    row = cursor.fetchone()
    if not isinstance(row, dict):
        cols = [d[0] for d in cursor.description]
        row = dict(zip(cols, row))
    return row
```

- [ ] **Step 4: Run tests — expect pass**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -m pytest tests/test_job_tracker.py -v`
Expected: All PASS

- [ ] **Step 5: Integrate into `sync_documents()` and `_upsert_single_document()` in `connector.py`**

In **both** functions, replace the existing project_code UPDATE block with:

```python
            # Extract project code + shooting dates from line items
            project_code = _extract_project_code(item.get('products'))
            shooting_raw = _extract_shooting_dates(item.get('products'))
            cursor.execute(_q(f'UPDATE {table} SET project_code = ?, shooting_dates_raw = ? WHERE id = ?'),
                           (project_code, shooting_raw, doc_id))

            # If this doc has a project code, ensure job exists
            if project_code:
                from skills.job_tracker import ensure_job
                doc_data = {
                    "client_id": item.get('contact'),
                    "client_name": item.get('contactName'),
                    "shooting_dates_raw": shooting_raw,
                    "estimate_id": doc_id if table == 'estimates' else None,
                    "estimate_number": item.get('docNumber') if table == 'estimates' else None,
                    "invoice_id": doc_id if table == 'invoices' else None,
                    "invoice_number": item.get('docNumber') if table == 'invoices' else None,
                    "doc_date": item.get('date'),
                }
                ensure_job(project_code, doc_data, cursor)
```

Note: in `_upsert_single_document()`, use `doc` instead of `item` (the variable name for the document dict).

- [ ] **Step 6: Test module still loads and syncs**

Run:
```bash
cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -c "
import connector
print('Module loads OK')
print(f'SHOOTING_DATES_PRODUCT_ID = {connector.SHOOTING_DATES_PRODUCT_ID}')
"
```
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add connector.py skills/job_tracker.py tests/test_job_tracker.py
git commit -m "feat(job-tracker): add ensure_job with sync integration"
```

---

## Chunk 3: Obsidian Sync & Queue System

### Task 6: Brain `/internal/obsidian-write` Endpoint

**Files:**
- Modify: `services/brain/src/routes/internal.ts`

- [ ] **Step 1: Read current internal.ts to find insertion point**

Read: `services/brain/src/routes/internal.ts` — find the last route definition.

- [ ] **Step 2: Add `/internal/obsidian-write` route**

After the last existing route in `internal.ts`, add:

```typescript
  // ── Obsidian Write (used by holded-connector job tracker) ───────────
  app.post("/internal/obsidian-write", async (c) => {
    if (!validateKey(c)) return c.text("Unauthorized", 401);

    const { path, content, append, binary } = await c.req.json<{
      path: string;
      content: string;
      append?: boolean;
      binary?: boolean;
    }>();

    if (!path || !content) {
      return c.json({ error: "path and content required" }, 400);
    }

    if (binary) {
      // Write binary file (PDF) to vault filesystem directly
      // No CouchDB push — LiveSync only handles text notes
      const { promises: fs } = await import("fs");
      const { dirname } = await import("path");
      const fullPath = safePath(path);
      await fs.mkdir(dirname(fullPath), { recursive: true });
      const buf = Buffer.from(content, "base64");
      await fs.writeFile(fullPath, buf);
      console.log(`[internal/obsidian-write] Binary: ${path} (${buf.length} bytes)`);
    } else {
      await writeNote({ path, content, append: append ?? false });
      console.log(`[internal/obsidian-write] Note: ${path} (${content.length} chars)`);
    }

    return c.json({ success: true, path });
  });
```

Ensure `writeNote` and `safePath` are imported at the top of the file from `../skills/obsidian.js`.

- [ ] **Step 3: Build and verify Brain compiles**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npm run build`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain"
git add src/routes/internal.ts
git commit -m "feat(brain): add /internal/obsidian-write endpoint for job tracker"
```

---

### Task 7: `sync_job_to_obsidian` and `flush_note_queue`

**Files:**
- Modify: `skills/job_tracker.py`

- [ ] **Step 1: Implement `sync_job_to_obsidian` in `skills/job_tracker.py`**

Add after `ensure_job`:

```python
# ── Obsidian Sync ──────────────────────────────────────────────────────────────

import requests as http_requests  # avoid shadowing

MAX_PDF_SIZE = 5 * 1024 * 1024  # 5MB


def _brain_write(path, content, append=False, binary=False):
    """Write to Obsidian vault via Brain's /internal/obsidian-write endpoint."""
    if not BRAIN_INTERNAL_KEY:
        logger.warning("[JOB_TRACKER] BRAIN_INTERNAL_KEY not configured, skipping Obsidian write")
        return False

    try:
        resp = http_requests.post(
            f"{BRAIN_API_URL}/internal/obsidian-write",
            json={"path": path, "content": content, "append": append, "binary": binary},
            headers={"x-api-key": BRAIN_INTERNAL_KEY},
            timeout=30,
        )
        if resp.status_code == 200:
            return True
        logger.error(f"[JOB_TRACKER] Brain write failed: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logger.error(f"[JOB_TRACKER] Brain unreachable: {e}")
        return False


def sync_job_to_obsidian(project_code):
    """Render and push a job note + PDF to Obsidian vault via Brain API.

    Returns True on success, False on failure.
    """
    conn_mod = _get_connector()
    conn = conn_mod.get_db()
    try:
        cur = conn_mod._cursor(conn)

        # Load job
        cur.execute(conn_mod._q("SELECT * FROM jobs WHERE project_code = ?"), (project_code,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"[JOB_TRACKER] Job not found: {project_code}")
            return False
        if not isinstance(row, dict):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, row))

        job = row
        quarter = job.get("quarter") or get_quarter(date.today().isoformat())
        year = quarter.split("_")[1] if "_" in quarter else str(date.today().year)

        # Load expenses (purchases with matching project_code)
        cur.execute(conn_mod._q("""
            SELECT pi.date, pi."desc" as name, pi.amount, pi.doc_number
            FROM purchase_invoices pi
            WHERE pi.project_code = ?
            ORDER BY pi.date
        """), (project_code,))
        expenses = []
        for r in cur.fetchall():
            if not isinstance(r, dict):
                cols = [d[0] for d in cur.description]
                r = dict(zip(cols, r))
            expenses.append(r)

        # Render note
        note_content = render_job_note(job, expenses)

        # Build paths
        safe_code = sanitize_for_path(project_code)
        base_path = f"ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}"
        note_path = f"{base_path}/{safe_code}"
        attach_dir = f"{base_path}/attachments"

        # Fetch PDF if we have an estimate or invoice
        doc_id = job.get("estimate_id") or job.get("invoice_id")
        doc_type = "estimate" if job.get("estimate_id") else "invoice"
        doc_num = sanitize_for_path(job.get("estimate_number") or job.get("invoice_number") or "")

        if doc_id:
            try:
                api_key = conn_mod.API_KEY
                pdf_resp = http_requests.get(
                    f"https://api.holded.com/api/invoicing/v1/documents/{doc_type}/{doc_id}/pdf",
                    headers={"key": api_key},
                    timeout=30,
                )
                if pdf_resp.status_code == 200:
                    # Handle base64 or raw response
                    pdf_data = None
                    try:
                        json_data = pdf_resp.json()
                        if "data" in json_data:
                            import base64
                            pdf_data = base64.b64decode(json_data["data"])
                    except Exception:
                        pdf_data = pdf_resp.content

                    if pdf_data:
                        new_hash = hashlib.md5(pdf_data).hexdigest()
                        if new_hash != job.get("pdf_hash"):
                            if len(pdf_data) <= MAX_PDF_SIZE:
                                import base64
                                pdf_b64 = base64.b64encode(pdf_data).decode()
                                pdf_filename = f"{safe_code}_{doc_num}.pdf"
                                _brain_write(f"{attach_dir}/{pdf_filename}", pdf_b64, binary=True)

                            # Update hash
                            cur.execute(conn_mod._q(
                                "UPDATE jobs SET pdf_hash = ? WHERE project_code = ?"
                            ), (new_hash, project_code))
                            conn.commit()
            except Exception as e:
                logger.warning(f"[JOB_TRACKER] PDF fetch failed for {project_code}: {e}")

        # Write note
        success = _brain_write(note_path, note_content)

        if success:
            cur.execute(conn_mod._q(
                "UPDATE jobs SET note_path = ? WHERE project_code = ?"
            ), (f"{note_path}.md", project_code))
            conn.commit()

        return success

    finally:
        conn_mod.release_db(conn)


def flush_note_queue():
    """Process all pending items in job_note_queue.

    Returns count of successfully processed items.
    """
    conn_mod = _get_connector()
    conn = conn_mod.get_db()
    processed = 0
    try:
        cur = conn_mod._cursor(conn)

        # Fetch pending items
        cur.execute(conn_mod._q("""
            SELECT id, project_code, action FROM job_note_queue
            WHERE processed_at IS NULL AND retry_count < 5
            ORDER BY created_at
        """))
        pending = cur.fetchall()

        for item in pending:
            if not isinstance(item, dict):
                cols = [d[0] for d in cur.description]
                item = dict(zip(cols, item))

            item_id = item["id"]
            code = item["project_code"]

            try:
                success = sync_job_to_obsidian(code)
                if success:
                    cur.execute(conn_mod._q(
                        "UPDATE job_note_queue SET processed_at = NOW() WHERE id = ?"
                    ) if not conn_mod._USE_SQLITE else
                        "UPDATE job_note_queue SET processed_at = datetime('now') WHERE id = ?",
                        (item_id,))
                    processed += 1
                else:
                    cur.execute(conn_mod._q(
                        "UPDATE job_note_queue SET retry_count = retry_count + 1, last_error = ? WHERE id = ?"
                    ), ("sync_job_to_obsidian returned False", item_id))
            except Exception as e:
                cur.execute(conn_mod._q(
                    "UPDATE job_note_queue SET retry_count = retry_count + 1, last_error = ? WHERE id = ?"
                ), (str(e)[:500], item_id))

        # Purge old processed items (30+ days)
        try:
            if conn_mod._USE_SQLITE:
                cur.execute("DELETE FROM job_note_queue WHERE processed_at IS NOT NULL AND processed_at < datetime('now', '-30 days')")
            else:
                cur.execute("DELETE FROM job_note_queue WHERE processed_at IS NOT NULL AND processed_at::timestamp < NOW() - INTERVAL '30 days'")
        except Exception:
            pass

        conn.commit()
    finally:
        conn_mod.release_db(conn)

    return processed
```

- [ ] **Step 2: Commit**

```bash
git add skills/job_tracker.py
git commit -m "feat(job-tracker): add Obsidian sync and queue flush"
```

---

### Task 8: Queue Flush Integration

**Files:**
- Modify: `connector.py` (end of sync sequence, ~line 2532)
- Modify: `write_gateway.py` (`_sync_back_async`, ~line 230)

- [ ] **Step 1: Add queue flush after sync sequence in `connector.py`**

After `sync_payments()` (line ~2531), add:

```python
        # Flush pending job notes to Obsidian
        try:
            from skills.job_tracker import flush_note_queue
            count = flush_note_queue()
            if count:
                logger.info(f"[JOB_TRACKER] Flushed {count} job notes to Obsidian")
        except Exception as e:
            logger.error(f"[JOB_TRACKER] Queue flush failed: {e}")
```

- [ ] **Step 2: Add queue flush in `_sync_back_async` in `write_gateway.py`**

After the existing sync-back logic (after `connector.update_audit_log(audit_id, tables_synced=tables)`, ~line 227), add:

```python
            # Flush job note queue (if a job was created/updated by this sync)
            try:
                from skills.job_tracker import flush_note_queue
                flush_note_queue()
            except Exception:
                pass  # queue will be flushed on next full sync
```

- [ ] **Step 3: Test module loads**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -c "import connector; import write_gateway; print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add connector.py write_gateway.py
git commit -m "feat(job-tracker): integrate queue flush into sync and write gateway"
```

---

## Chunk 4: API Endpoints & Brain Integration

### Task 9: API Endpoints

**Files:**
- Modify: `api.py`
- Modify: `auth.py`

- [ ] **Step 1: Add job routes to PERMISSION_MATRIX in `auth.py`**

In the `PERMISSION_MATRIX` list, add:

```python
    # Jobs — job tracker
    ("GET", r"^/api/jobs", {"admin", "accountant"}),
    ("POST", r"^/api/jobs", {"admin"}),
    ("PATCH", r"^/api/jobs/", {"admin"}),
```

- [ ] **Step 2: Add `/api/jobs` endpoints in `api.py`**

Add at the end of the file (before the `if __name__` block):

```python
# ── Job Tracker Endpoints ──────────────────────────────────────────────────────

@app.get("/api/jobs")
def list_jobs(status: str = None, quarter: str = None, limit: int = 50):
    """List jobs, optionally filtered by status and/or quarter."""
    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        conditions = []
        params = []
        if status:
            conditions.append("status = " + connector._q("?")[0] if connector._USE_SQLITE else "%s")
            params.append(status)
        if quarter:
            conditions.append("quarter = " + connector._q("?")[0] if connector._USE_SQLITE else "%s")
            params.append(quarter)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(connector._q(f"SELECT * FROM jobs {where} ORDER BY updated_at DESC LIMIT ?"),
                    (*params, limit))
        rows = cur.fetchall()
        result = []
        for r in rows:
            if not isinstance(r, dict):
                cols = [d[0] for d in cur.description]
                r = dict(zip(cols, r))
            result.append(r)
        return result
    finally:
        connector.release_db(conn)


@app.post("/api/jobs")
def create_job(request: dict):
    """Create a new job (Brain's entry point)."""
    from skills.job_tracker import ensure_job
    project_code = request.get("project_code")
    if not project_code:
        return JSONResponse({"error": "project_code required"}, status_code=400)

    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        doc_data = {
            "client_id": request.get("client_id"),
            "client_name": request.get("client_name"),
            "shooting_dates_raw": request.get("shooting_dates_raw"),
            "estimate_id": request.get("estimate_id"),
            "estimate_number": request.get("estimate_number"),
            "invoice_id": request.get("invoice_id"),
            "invoice_number": request.get("invoice_number"),
            "doc_date": request.get("doc_date"),
        }
        result = ensure_job(project_code, doc_data, cur)
        conn.commit()

        # Immediate flush for Brain-created jobs
        try:
            from skills.job_tracker import flush_note_queue
            flush_note_queue()
        except Exception:
            pass

        return result
    finally:
        connector.release_db(conn)


@app.get("/api/jobs/{code}")
def get_job(code: str):
    """Get job detail with expenses."""
    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)
        cur.execute(connector._q("SELECT * FROM jobs WHERE project_code = ?"), (code,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        if not isinstance(row, dict):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, row))

        # Fetch expenses
        cur.execute(connector._q("""
            SELECT date, "desc" as name, amount, doc_number
            FROM purchase_invoices WHERE project_code = ?
            ORDER BY date
        """), (code,))
        expenses = []
        for r in cur.fetchall():
            if not isinstance(r, dict):
                cols = [d[0] for d in cur.description]
                r = dict(zip(cols, r))
            expenses.append(r)

        row["expenses"] = expenses
        return row
    finally:
        connector.release_db(conn)


@app.patch("/api/jobs/{code}")
def update_job(code: str, request: dict):
    """Update job fields (status, shooting_dates, invoice_id, etc.)."""
    conn = connector.get_db()
    try:
        cur = connector._cursor(conn)

        # Verify job exists
        cur.execute(connector._q("SELECT project_code FROM jobs WHERE project_code = ?"), (code,))
        if not cur.fetchone():
            return JSONResponse({"error": "Job not found"}, status_code=404)

        allowed = {"status", "shooting_dates_raw", "invoice_id", "invoice_number", "note_path"}
        updates = {k: v for k, v in request.items() if k in allowed}
        if not updates:
            return JSONResponse({"error": "No valid fields to update"}, status_code=400)

        set_clauses = ", ".join(f"{k} = {connector._q('?')}" for k in updates)
        values = list(updates.values()) + [code]
        cur.execute(connector._q(f"UPDATE jobs SET {set_clauses}, updated_at = ? WHERE project_code = ?"),
                    (*updates.values(), datetime.now().isoformat(), code))
        conn.commit()

        # Re-fetch
        cur.execute(connector._q("SELECT * FROM jobs WHERE project_code = ?"), (code,))
        row = cur.fetchone()
        if not isinstance(row, dict):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, row))
        return row
    finally:
        connector.release_db(conn)


@app.post("/api/jobs/{code}/sync-note")
def sync_job_note(code: str):
    """Force re-render and push job note to Obsidian."""
    from skills.job_tracker import sync_job_to_obsidian
    success = sync_job_to_obsidian(code)
    if success:
        return {"success": True, "message": f"Note synced for {code}"}
    return JSONResponse({"error": "Sync failed"}, status_code=502)


@app.post("/api/jobs/flush-queue")
def flush_job_queue():
    """Process all pending Obsidian note queue items."""
    from skills.job_tracker import flush_note_queue
    count = flush_note_queue()
    return {"success": True, "processed": count}
```

Add `from datetime import datetime` at the top of `api.py` if not already present, and ensure `JSONResponse` is imported from `fastapi.responses`.

- [ ] **Step 3: Test endpoints compile**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector" && /usr/bin/python3 -c "import api; print('API loads OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add api.py auth.py
git commit -m "feat(job-tracker): add /api/jobs endpoints with auth"
```

---

### Task 10: Brain holded.ts Integration

**Files:**
- Modify: `services/brain/src/skills/holded.ts`

- [ ] **Step 1: Read current holded.ts to find the query type switch**

Read: `services/brain/src/skills/holded.ts` — find where query types are routed.

- [ ] **Step 2: Add job query types**

In the switch/if-chain that routes query types, add:

```typescript
    case "open_jobs":
      return holdedFetch("/api/jobs?status=open");
    case "job_detail":
      return holdedFetch(`/api/jobs/${encodeURIComponent(args.code || "")}`);
    case "create_job":
      return holdedPost("/api/jobs", {
        project_code: args.code,
        client_name: args.client,
        shooting_dates_raw: args.dates,
      });
    case "update_job":
      return holdedPost(`/api/jobs/${encodeURIComponent(args.code || "")}`, args.data, "PATCH");
```

Add `holdedPost` helper if it doesn't exist (similar to `holdedFetch` but with POST/PATCH method).

- [ ] **Step 3: Build Brain**

Run: `cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain" && npm run build`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/brain"
git add src/skills/holded.ts
git commit -m "feat(brain): add job query types for holded_query tool"
```

---

### Task 11: Environment & Documentation

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add env vars to `.env.example`**

```bash
# Brain API — for Obsidian vault writes via job tracker
BRAIN_API_URL=http://localhost:3100
BRAIN_INTERNAL_KEY=
```

- [ ] **Step 2: Update CLAUDE.md**

Add to the file structure section:
```
├── skills/
│   ├── __init__.py
│   └── job_tracker.py           # Job dossier system: date parser, note renderer, Obsidian sync
```

Add to Migration Status > Completed:
```
- [x] Project code system — `project_code` + `shooting_dates` extracted from Holded line items (2026-03-12)
- [x] Job tracker — Obsidian dossier per project with PDF, expenses, checklist (2026-03-12)
```

Add to API Endpoints section:
```
### Job Tracker Endpoints
- `GET /api/jobs` — List jobs (filter: status, quarter)
- `POST /api/jobs` — Create job (Brain entry point)
- `GET /api/jobs/{code}` — Job detail + expenses
- `PATCH /api/jobs/{code}` — Update status/dates/invoice
- `POST /api/jobs/{code}/sync-note` — Force re-render to Obsidian
- `POST /api/jobs/flush-queue` — Process pending note queue
```

- [ ] **Step 3: Commit**

```bash
git add .env.example CLAUDE.md
git commit -m "docs: document job tracker endpoints, env vars, file structure"
```

---

## Chunk 5: End-to-End Test

### Task 12: Integration Test

- [ ] **Step 1: Create a test quote in Holded with Proyect REF: + Shooting Dates:**

```python
import os, requests, json, time
from dotenv import load_dotenv
load_dotenv()
key = os.getenv('HOLDED_API_KEY')
headers = {'key': key, 'Content-Type': 'application/json'}

payload = {
    'contactId': '699363252351babfda0eb6f8',  # Franca Creative Production
    'date': int(time.time()),
    'items': [
        {'productId': '69b2b35f75ae381d8f05c133', 'name': 'Proyect REF:',
         'desc': 'FRANCA-260315', 'units': 1, 'price': 0, 'tax': 0},
        {'productId': '69b2cfcd0df77ff4010e4ac8', 'name': 'Shooting Dates:',
         'desc': '15/3-18/3', 'units': 1, 'price': 0, 'tax': 0},
        {'productId': '6908779fb7776fc4850b0a1a', 'name': 'Profoto b30 Duo kit',
         'units': 2, 'price': 120, 'tax': 21},
    ]
}

r = requests.post('https://api.holded.com/api/invoicing/v1/documents/estimate',
                   headers=headers, json=payload)
print(r.json())
```

- [ ] **Step 2: Sync estimates and verify job was created**

```python
import connector
connector.init_db()
connector.sync_estimates()

conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute("SELECT * FROM jobs WHERE project_code = 'FRANCA-260315'")
row = cur.fetchone()
print(row)  # Should show job with shooting dates, quarter, etc.
conn.close()
```

- [ ] **Step 3: Manually trigger note sync (if Brain is available)**

```python
from skills.job_tracker import sync_job_to_obsidian
success = sync_job_to_obsidian("FRANCA-260315")
print(f"Sync: {'OK' if success else 'FAILED'}")
```

If Brain is not running locally, verify the note_content renders correctly:

```python
from skills.job_tracker import render_job_note
import connector
conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute(connector._q("SELECT * FROM jobs WHERE project_code = 'FRANCA-260315'"))
row = cur.fetchone()
if not isinstance(row, dict):
    cols = [d[0] for d in cur.description]
    row = dict(zip(cols, row))
note = render_job_note(row, [])
print(note)
conn.close()
```

- [ ] **Step 4: Delete test quote from Holded**

```python
import requests
r = requests.delete(f'https://api.holded.com/api/invoicing/v1/documents/estimate/{test_id}',
                     headers={'key': key})
print(r.json())
```

- [ ] **Step 5: Final commit + push**

```bash
git add -A
git commit -m "feat(job-tracker): complete implementation with e2e verification"
git push
```

---

## Summary

| Chunk | Tasks | Focus |
|-------|-------|-------|
| 1 | 1-3 | Core skill: date parser, sanitizers, note renderer (pure functions, TDD) |
| 2 | 4-5 | DB tables, migrations, sync integration, ensure_job |
| 3 | 6-8 | Brain endpoint, Obsidian sync, queue flush |
| 4 | 9-11 | API endpoints, auth, Brain integration, docs |
| 5 | 12 | End-to-end integration test |

**Dependency chain:** Chunk 1 → 2 → 3 → 4 → 5 (sequential — each builds on the previous)
