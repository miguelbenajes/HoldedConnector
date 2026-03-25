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
import base64
import logging
import hashlib
from datetime import date, datetime, timedelta

import requests as http_requests

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
                    # Safety: cap at 60 days to prevent runaway ranges
                    if (end_date - start_date).days > 60:
                        continue
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

HOLDED_PDF_BASE = os.getenv("HOLDED_CONNECTOR_URL", "http://localhost:8000")


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

    # Quote YAML values to prevent injection via colons/newlines in user data
    def _yq(v):
        """Quote a YAML value if it contains special characters."""
        s = str(v).replace('\n', ' ').replace('\r', '')
        if any(c in s for c in [':', '#', '{', '}', '[', ']', ',', '&', '*', '?', '|', '-', '<', '>', '=', '!', '%', '@', '`', '"']):
            return f'"{s.replace(chr(34), chr(39))}"'
        return s

    return f"""---
project_code: {_yq(code)}
client: {_yq(job.get("client_name") or "")}
client_email: {_yq(email)}
status: {status}
shooting_dates: {parsed_dates}
created: {_yq(created)}
quote_id: {_yq(estimate_id)}
quote_number: {_yq(estimate_num)}
invoice_id: {_yq(invoice_id)}
invoice_number: {_yq(invoice_num)}
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

## Estimate Changes

%%--- MANUAL CONTENT BELOW - preserved on sync ---%%

### Items to Add
<!-- + Product x2 @180€  or  + Transport 150€  or  free text -->


### Items to Remove
<!-- - Product name (reason)  -->


### Other Notes


## Invoicing Checklist
- [ ] Facturar al cliente
- [ ] Gastos revisados
- [ ] Equipo devuelto / daños comprobados
"""


# ── Job Management ─────────────────────────────────────────────────────────────

def _get_connector():
    """Lazy import of connector module to avoid circular imports."""
    import connector
    return connector


def _row_to_dict(row, cursor):
    """Convert a DB row to dict if it isn't already (SQLite returns tuples)."""
    if isinstance(row, dict):
        return row
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


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

    # Validate project_code
    if not project_code or len(project_code) > 50:
        raise ValueError(f"Invalid project_code: {project_code!r}")

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
        existing = _row_to_dict(existing, cursor)

        est_id = doc_data.get("estimate_id") or existing.get("estimate_id")
        est_num = doc_data.get("estimate_number") or existing.get("estimate_number")
        inv_id = doc_data.get("invoice_id") or existing.get("invoice_id")
        inv_num = doc_data.get("invoice_number") or existing.get("invoice_number")

        status = existing.get("status", "open")
        if inv_id and status in ("open", "shooting"):
            status = "invoiced"

        # Detect if anything actually changed before updating
        new_client_id = client_id or existing.get("client_id")
        new_client_name = doc_data.get("client_name") or existing.get("client_name")
        new_client_email = client_email or existing.get("client_email")
        new_shooting_raw = shooting_raw or existing.get("shooting_dates_raw")

        changed = (
            new_client_id != existing.get("client_id")
            or new_client_name != existing.get("client_name")
            or new_client_email != existing.get("client_email")
            or new_shooting_raw != existing.get("shooting_dates_raw")
            or parsed_json != existing.get("shooting_dates")
            or quarter != existing.get("quarter")
            or est_id != existing.get("estimate_id")
            or est_num != existing.get("estimate_number")
            or inv_id != existing.get("invoice_id")
            or inv_num != existing.get("invoice_number")
            or status != existing.get("status")
        )

        if changed:
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
                new_client_id, new_client_name, new_client_email,
                new_shooting_raw, parsed_json, quarter,
                est_id, est_num, inv_id, inv_num,
                status, now_str, project_code,
            ))
        action = "update" if changed else None
    else:
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

    # Queue note sync only on create or actual changes
    if action:
        cursor.execute(conn_mod._q("""
            INSERT INTO job_note_queue (project_code, action) VALUES (?, ?)
        """), (project_code, action))

    # Fetch and return the row
    cursor.execute(conn_mod._q(
        "SELECT * FROM jobs WHERE project_code = ?"
    ), (project_code,))
    return _row_to_dict(cursor.fetchone(), cursor)


# ── Obsidian Sync ──────────────────────────────────────────────────────────────

MAX_PDF_SIZE = 5 * 1024 * 1024  # 5MB


MANUAL_MARKER = "%%--- MANUAL CONTENT BELOW - preserved on sync ---%%"


def compute_notes_hash(note_content):
    """Compute SHA256 of the ## Notes section between MANUAL_MARKER and ## Invoicing Checklist.
    Returns None if no notes section or empty."""
    if not note_content or MANUAL_MARKER not in note_content:
        return None
    parts = note_content.split(MANUAL_MARKER, 1)
    if len(parts) < 2:
        return None
    notes_section = parts[1].split("## Invoicing Checklist", 1)[0].strip()
    if not notes_section:
        return None
    return hashlib.sha256(notes_section.encode()).hexdigest()


def _brain_read(path):
    """Read a note from Obsidian vault via Brain's /internal/obsidian-read endpoint.

    Args:
        path: vault-relative path (without .md extension)

    Returns:
        Note content string, or None if not found or error.
    """
    if not BRAIN_INTERNAL_KEY:
        return None

    try:
        resp = http_requests.post(
            f"{BRAIN_API_URL}/internal/obsidian-read",
            json={"path": path},
            headers={"x-api-key": BRAIN_INTERNAL_KEY},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("found"):
                return data.get("content", "")
        return None
    except Exception:
        return None


def _extract_section(content, heading, next_headings):
    """Extract content of a markdown section between heading and next heading.

    Args:
        content: full markdown string
        heading: section heading to find (e.g. "## Expenses & Tickets")
        next_headings: list of possible next section headings

    Returns:
        Section content (without the heading line itself), or None if not found.
    """
    lines = content.split("\n")
    start_idx = None
    end_idx = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == heading:
            start_idx = i + 1
        elif start_idx is not None and stripped in next_headings:
            end_idx = i
            break

    if start_idx is None:
        return None

    section = "\n".join(lines[start_idx:end_idx])
    return section


def _has_real_content(section_text, empty_markers):
    """Check if a section has real user content beyond the default template.

    Args:
        section_text: the extracted section content
        empty_markers: list of strings that indicate the section is still template-default

    Returns:
        True if the section contains real user-added content.
    """
    if not section_text:
        return False
    for marker in empty_markers:
        if marker in section_text:
            return False
    return True


def _replace_section(content, heading, next_headings, new_section_body):
    """Replace the body of a markdown section, keeping heading and next section intact.

    Args:
        content: full markdown string
        heading: section heading (e.g. "## Expenses & Tickets")
        next_headings: possible next section headings
        new_section_body: replacement content (without heading)

    Returns:
        Modified content string.
    """
    lines = content.split("\n")
    start_idx = None
    end_idx = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == heading:
            start_idx = i + 1
        elif start_idx is not None and stripped in next_headings:
            end_idx = i
            break

    if start_idx is None:
        return content

    result = lines[:start_idx] + new_section_body.split("\n") + lines[end_idx:]
    return "\n".join(result)


# Sections that can be manually edited above the MANUAL_MARKER
_EDITABLE_SECTIONS = [
    {
        "heading": "## Expenses & Tickets",
        "next": ["## Email Thread", "## Estimate Changes", "## Notes"],
        "empty_markers": ["*No expenses yet*"],
    },
    {
        "heading": "## Email Thread",
        "next": ["## Estimate Changes", "## Notes", MANUAL_MARKER],
        "empty_markers": [],  # compare-based: keep existing if it differs from new
        "preserve_if_different": True,
    },
]


def _preserve_manual_content(new_content, existing_content):
    """Merge new generated content with manually-written content from existing note.

    Two-level preservation:
      1. Sections above MANUAL_MARKER (Expenses, Email Thread): if the existing note
         has user-edited content and the new render has only defaults, keep the existing.
      2. Everything after MANUAL_MARKER: always preserved from existing note.
    """
    if not existing_content:
        return new_content

    merged = new_content

    # ── Preserve editable sections above the marker ──
    for sec in _EDITABLE_SECTIONS:
        existing_section = _extract_section(existing_content, sec["heading"], sec["next"])
        new_section = _extract_section(merged, sec["heading"], sec["next"])

        if existing_section is None:
            continue

        if sec.get("preserve_if_different"):
            # For sections like Email Thread: preserve existing only if user ADDED content
            # (more non-empty lines than the system render). If the system render has
            # equal or more lines, it's a legitimate update — use the new version.
            if existing_section and new_section:
                existing_lines = [l for l in existing_section.strip().splitlines() if l.strip()]
                new_lines = [l for l in new_section.strip().splitlines() if l.strip()]
                if len(existing_lines) > len(new_lines):
                    merged = _replace_section(merged, sec["heading"], sec["next"], existing_section)
            continue

        existing_has_content = _has_real_content(existing_section, sec["empty_markers"])
        new_has_content = _has_real_content(new_section, sec["empty_markers"])

        # Keep existing manual content when new render only has defaults
        if existing_has_content and not new_has_content:
            merged = _replace_section(merged, sec["heading"], sec["next"], existing_section)

    # ── Preserve everything after MANUAL_MARKER ──
    if MANUAL_MARKER in existing_content and MANUAL_MARKER in merged:
        manual_part = existing_content.split(MANUAL_MARKER, 1)[1]
        generated_part = merged.split(MANUAL_MARKER, 1)[0]
        merged = generated_part + MANUAL_MARKER + manual_part

    return merged


def _brain_write(path, content, append=False, binary=False):
    """Write to Obsidian vault via Brain's /internal/obsidian-write endpoint.

    Args:
        path: vault-relative path (e.g. "ACCOUNTS/SEGUIMIENTO_TRABAJOS/2026/1T_2026/CODE")
        content: markdown string or base64-encoded binary data
        append: if True, append to existing note
        binary: if True, treat content as base64 binary (for PDFs)

    Returns:
        True on success, False on failure.
    """
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
        # Log status only, not response body (may contain internal details)
        logger.error(f"[JOB_TRACKER] Brain write failed: HTTP {resp.status_code}")
        return False
    except Exception as e:
        logger.error(f"[JOB_TRACKER] Brain unreachable: {e}")
        return False


def sync_job_to_obsidian(project_code):
    """Render and push a job note + PDF to Obsidian vault via Brain API.

    Flow:
      1. Load job row + expenses from DB (release connection quickly)
      2. Render markdown note via render_job_note()
      3. Fetch PDF from Holded API (if estimate/invoice exists), push as binary
      4. Push note via _brain_write()
      5. Update job.note_path on success (short DB operation)

    Returns:
        True on success, False on failure.
    """
    conn_mod = _get_connector()

    # ── Phase 1: Load data from DB (fast, release connection immediately) ──
    conn = conn_mod.get_db()
    try:
        cur = conn_mod._cursor(conn)

        cur.execute(conn_mod._q("SELECT * FROM jobs WHERE project_code = ?"), (project_code,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"[JOB_TRACKER] Job not found: {project_code}")
            return False
        job = _row_to_dict(row, cur)

        cur.execute(conn_mod._q("""
            SELECT pi.date, pi."desc" as name, pi.amount, pi.doc_number
            FROM purchase_invoices pi
            WHERE pi.project_code = ?
            ORDER BY pi.date
        """), (project_code,))
        expenses = [_row_to_dict(r, cur) for r in cur.fetchall()]
    finally:
        conn_mod.release_db(conn)

    # ── Phase 2: Render + push (no DB connection held during I/O) ──
    quarter = job.get("quarter") or get_quarter(date.today().isoformat())
    year = quarter.split("_")[1] if "_" in quarter else str(date.today().year)
    safe_code = sanitize_for_path(project_code)
    base_path = f"ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}"
    note_path = f"{base_path}/{safe_code}"

    # Read existing note to preserve manual content
    existing_content = _brain_read(note_path)

    # Compute and store notes_hash from existing note
    if existing_content:
        new_hash = compute_notes_hash(existing_content)
        if new_hash:
            conn2 = conn_mod.get_db()
            try:
                cur2 = conn_mod._cursor(conn2)
                cur2.execute(conn_mod._q(
                    "UPDATE jobs SET notes_hash = ? WHERE project_code = ?"
                ), (new_hash, project_code))
                conn2.commit()
            finally:
                conn_mod.release_db(conn2)

    note_content = render_job_note(job, expenses)
    note_content = _preserve_manual_content(note_content, existing_content)
    attach_dir = f"{base_path}/attachments"

    # Fetch PDF if we have an estimate or invoice
    doc_id = job.get("estimate_id") or job.get("invoice_id")
    doc_type = "estimate" if job.get("estimate_id") else "invoice"
    doc_num = sanitize_for_path(job.get("estimate_number") or job.get("invoice_number") or "")
    new_pdf_hash = None

    if doc_id:
        try:
            api_key = conn_mod.API_KEY
            pdf_resp = http_requests.get(
                f"https://api.holded.com/api/invoicing/v1/documents/{doc_type}/{doc_id}/pdf",
                headers={"key": api_key},
                timeout=30,
            )
            if pdf_resp.status_code == 200:
                pdf_data = None
                try:
                    json_data = pdf_resp.json()
                    if "data" in json_data:
                        pdf_data = base64.b64decode(json_data["data"])
                except Exception:
                    pdf_data = pdf_resp.content

                if pdf_data:
                    new_pdf_hash = hashlib.sha256(pdf_data).hexdigest()
                    if new_pdf_hash != job.get("pdf_hash") and len(pdf_data) <= MAX_PDF_SIZE:
                        pdf_b64 = base64.b64encode(pdf_data).decode()
                        pdf_filename = f"{safe_code}_{doc_num}.pdf"
                        _brain_write(f"{attach_dir}/{pdf_filename}", pdf_b64, binary=True)
        except Exception as e:
            logger.warning(f"[JOB_TRACKER] PDF fetch failed for {project_code}: {e}")

    success = _brain_write(note_path, note_content)

    # ── Phase 3: Quick DB update on success ──
    if success or new_pdf_hash:
        conn = conn_mod.get_db()
        try:
            cur = conn_mod._cursor(conn)
            if success:
                cur.execute(conn_mod._q(
                    "UPDATE jobs SET note_path = ? WHERE project_code = ?"
                ), (f"{note_path}.md", project_code))
            if new_pdf_hash and new_pdf_hash != job.get("pdf_hash"):
                cur.execute(conn_mod._q(
                    "UPDATE jobs SET pdf_hash = ? WHERE project_code = ?"
                ), (new_pdf_hash, project_code))
            conn.commit()
        finally:
            conn_mod.release_db(conn)

    return success


def flush_note_queue():
    """Process all pending items in job_note_queue.

    Iterates through unprocessed queue items (retry_count < 5), calls
    sync_job_to_obsidian for each, and marks as processed on success.
    Failed items get their retry_count incremented.
    Old processed items (30+ days) are purged.

    Returns:
        Count of successfully processed items.
    """
    conn_mod = _get_connector()
    conn = conn_mod.get_db()
    processed = 0
    try:
        cur = conn_mod._cursor(conn)

        cur.execute(conn_mod._q("""
            SELECT id, project_code, action FROM job_note_queue
            WHERE processed_at IS NULL AND retry_count < 5
            ORDER BY created_at
        """))
        pending = cur.fetchall()

        for item in pending:
            item = _row_to_dict(item, cur)

            item_id = item["id"]
            code = item["project_code"]

            try:
                success = sync_job_to_obsidian(code)
                if success:
                    if conn_mod._USE_SQLITE:
                        cur.execute(
                            "UPDATE job_note_queue SET processed_at = datetime('now') WHERE id = ?",
                            (item_id,))
                    else:
                        cur.execute(
                            "UPDATE job_note_queue SET processed_at = NOW() WHERE id = %s",
                            (item_id,))
                    processed += 1
                else:
                    cur.execute(conn_mod._q(
                        "UPDATE job_note_queue SET retry_count = retry_count + 1, last_error = ? WHERE id = ?"
                    ), ("sync_job_to_obsidian returned False", item_id))
            except Exception as e:
                cur.execute(conn_mod._q(
                    "UPDATE job_note_queue SET retry_count = retry_count + 1, last_error = ? WHERE id = ?"
                ), (re.sub(r'https?://[^\s]+', '[redacted]', str(e)[:500]), item_id))

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
