# Job Tracker — Obsidian Job Dossier System

**Date:** 2026-03-12
**Status:** Approved
**Scope:** Holded Connector + Brain + Obsidian vault

---

## Problem

When invoicing a completed job, Miguel must manually gather: the original quote, shooting dates, expenses, receipts, and client contact info from multiple sources. There is no single place to see everything needed to close a job.

## Solution

An automated job dossier system that creates and maintains an Obsidian note for each job (identified by `project_code`). The note aggregates the quote PDF, shooting dates, expenses, client email, and an invoicing checklist — so closing a job is a single-page review.

---

## Architecture Overview

```
Holded (quote/invoice)
    ↓ sync
connector.py (extract project_code + shooting_dates)
    ↓
jobs table (source of truth)
    ↓
skills/job_tracker.py (render note + fetch PDF)
    ↓ Brain /internal/obsidian-write
Obsidian vault: ACCOUNTS/SEGUIMIENTO_TRABAJOS/YYYY/QT_YYYY/CODE.md
```

### Key Principle

The `jobs` DB table is the **source of truth**. The Obsidian note is a **rendered view** that can be regenerated at any time. This decouples data capture (fast, during sync) from note rendering (slower, depends on Brain API).

---

## Components

### 1. Database: `jobs` table

Uses dialect tokens (`_serial`, `_real`, `_now`) to support both SQLite and PostgreSQL, matching the existing `init_db()` pattern in `connector.py`.

```sql
CREATE TABLE IF NOT EXISTS jobs (
    project_code TEXT PRIMARY KEY,
    client_id TEXT,
    client_name TEXT,
    client_email TEXT,
    status TEXT DEFAULT 'open',           -- open | shooting | invoiced | closed
    shooting_dates_raw TEXT,              -- raw string from Holded: "17/3-21/3"
    shooting_dates TEXT,                  -- JSON array: ["2026-03-17", ...]
    quarter TEXT,                         -- "1T_2026" (from first shooting date, fallback doc date)
    estimate_id TEXT,
    estimate_number TEXT,
    invoice_id TEXT,
    invoice_number TEXT,
    note_path TEXT,                       -- vault path once created
    pdf_hash TEXT,                        -- MD5 of last downloaded PDF (skip re-download)
    created_at TEXT DEFAULT {_now},
    updated_at TEXT DEFAULT {_now}
);
```

### 2. Database: `job_note_queue` table

```sql
CREATE TABLE IF NOT EXISTS job_note_queue (
    id {_serial},
    project_code TEXT NOT NULL,
    action TEXT NOT NULL,                 -- 'create' | 'update' | 'update_pdf' | 'update_expenses'
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    created_at TEXT DEFAULT {_now},
    processed_at TEXT
);
```

Max 5 retries. Failed items logged but not retried further. Processed items older than 30 days are purged during `flush_note_queue()`.

**Index** (PostgreSQL only): `CREATE INDEX IF NOT EXISTS idx_job_queue_pending ON job_note_queue (created_at) WHERE processed_at IS NULL AND retry_count < 5;`

### 3. Document table changes

Add to `invoices`, `estimates`, `purchase_invoices` via ALTER TABLE in `init_db()`:

```python
# SQLite path (try/except pattern):
("invoices",          "shooting_dates_raw", "TEXT"),
("purchase_invoices", "shooting_dates_raw", "TEXT"),
("estimates",         "shooting_dates_raw", "TEXT"),

# PostgreSQL path (ADD COLUMN IF NOT EXISTS):
"ALTER TABLE invoices ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
"ALTER TABLE purchase_invoices ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
"ALTER TABLE estimates ADD COLUMN IF NOT EXISTS shooting_dates_raw TEXT",
```

Note: only `shooting_dates_raw` on document tables (the raw string). Parsed dates live in the `jobs` table, which is the single source of truth for job metadata.

### 4. Skill: `skills/job_tracker.py`

Single-file module. No external dependencies beyond what's already in requirements.txt (requests, psycopg2).

#### Functions

**`parse_shooting_dates(raw: str, reference_year: int) -> list[str]`**

Flexible date parser. Input formats:

| Input | Output |
|-------|--------|
| `17/3-21/3` | `["2026-03-17", "2026-03-18", ..., "2026-03-21"]` |
| `17/3, 19/3, 21/3` | `["2026-03-17", "2026-03-19", "2026-03-21"]` |
| `17-18/3` | `["2026-03-17", "2026-03-18"]` |
| `17/3` | `["2026-03-17"]` |
| `17/3-2/4` | `["2026-03-17", ..., "2026-04-02"]` |
| `17, 18, 21/3` | `["2026-03-17", "2026-03-18", "2026-03-21"]` |
| `28/12-3/1` | `["2026-12-28", ..., "2027-01-03"]` (cross-year: end < start → bump year) |

Strategy:
1. Split by `,` → segments
2. Each segment: check for `-` range or single date
3. Parse `day/month` format, inherit month from rightmost explicit month
4. Year from `reference_year` parameter (document date year)
5. Cross-year ranges: if range end date < start date, increment year on end date
6. Return sorted, deduplicated ISO date strings
7. On parse failure → return empty list, log warning (never crash)

**`get_quarter(date_str: str) -> str`**

Maps ISO date to quarter string: `"2026-03-17"` → `"1T_2026"`. Months 1-3 → 1T, 4-6 → 2T, 7-9 → 3T, 10-12 → 4T.

**`sanitize_for_path(text: str) -> str`**

Strips characters unsafe for file paths: `/`, `\`, `..`, `<`, `>`, `:`, `"`, `|`, `?`, `*`. Replaces spaces with `-`. Prevents path traversal.

**`sanitize_for_markdown(text: str) -> str`**

Escapes markdown-active characters in user-supplied strings: `[`, `]`, `(`, `)`, `|`, `` ` ``. Prevents markdown injection.

**`ensure_job(project_code: str, doc_data: dict, cursor) -> dict`**

Upserts the `jobs` table row. `doc_data` contains fields extracted during sync:

```python
doc_data = {
    "client_id": "...",
    "client_name": "...",
    "shooting_dates_raw": "17/3-21/3",
    "estimate_id": "...",            # or invoice_id — NOT mutually exclusive
    "estimate_number": "QUOTE-26/0014",
    "invoice_id": "...",
    "invoice_number": "INV-26/0042",
    "doc_date": 1742169600,          # unix timestamp for year reference
}
```

Logic:
1. Parse shooting dates → derive quarter (fallback: quarter from doc_date, then current date)
2. Look up client email from `contacts` table by `client_id`
3. Upsert into `jobs` table — **merge strategy for multi-doc fields:**
   - `estimate_id`/`invoice_id`: update if new value is non-None (don't overwrite with None). **Only the latest estimate/invoice is stored** — if a revised quote replaces the old one, the job note points to the current version. This is intentional: the job note is a working document for invoicing, not an archive of all revisions.
   - `shooting_dates_raw`: always update to latest value
   - `status`: never overwritten by ensure_job (only by explicit PATCH, or auto-transition to `invoiced` when invoice_id is set)
4. Queue note sync: insert into `job_note_queue` (action: 'create' if new, 'update' if existing)
5. Return the job row

**`render_job_note(job: dict, expenses: list[dict]) -> str`**

Generates the full markdown note from job data + expenses. Pure function, no I/O. All user-supplied strings (client_name, project_code) run through `sanitize_for_markdown()`.

**`sync_job_to_obsidian(project_code: str) -> bool`**

1. Read job from `jobs` table
2. Fetch expenses (purchase_invoices with matching project_code)
3. Render note markdown
4. Fetch PDF from Holded if hash changed (or first time)
   - PDF size cap: skip embed if > 5MB, keep link only
   - Compute MD5 hash, compare with `jobs.pdf_hash`, skip if same
5. Call Brain internal API to write attachment (if PDF changed):
   `POST {BRAIN_API_URL}/internal/obsidian-write` with `x-api-key` header
   Body: `{"path": "...", "content": <base64 PDF>, "binary": true}`
6. Call Brain internal API to write note:
   `POST {BRAIN_API_URL}/internal/obsidian-write`
   Body: `{"path": "...", "content": <markdown>, "append": false}`
7. Update `jobs.note_path` and `jobs.pdf_hash`
8. Return True on success, False on failure

**`flush_note_queue() -> int`**

Process all pending items in `job_note_queue`:
1. SELECT where `processed_at IS NULL AND retry_count < 5` ORDER BY created_at
2. For each: call `sync_job_to_obsidian()`
3. On success: set `processed_at = NOW()`
4. On failure: increment `retry_count`, store error in `last_error`
5. Purge processed items older than 30 days
6. Return count of successfully processed items

Config read from environment:
```python
BRAIN_API_URL = os.getenv("BRAIN_API_URL", "http://localhost:3100")
BRAIN_INTERNAL_KEY = os.getenv("BRAIN_INTERNAL_KEY", "")
```

### 5. Brain: new internal endpoint

**File:** `services/brain/src/routes/internal.ts`

New route following the existing `scraper-report` pattern:

```typescript
// POST /internal/obsidian-write
// Headers: x-api-key: BRAIN_INTERNAL_KEY
// Body: { path: string, content: string, append?: boolean, binary?: boolean }
//
// If binary=true, content is base64-encoded file data (for PDFs).
// Otherwise, content is markdown text.
//
// Uses writeNote() for text (which handles CouchDB sync internally).
// For binary files, writes directly to filesystem — CouchDB LiveSync
// only handles text/markdown, so PDFs sync via filesystem only.

app.post("/internal/obsidian-write", async (c) => {
    const key = c.req.header("x-api-key");
    if (key !== process.env.BRAIN_INTERNAL_KEY) return c.json({ error: "Unauthorized" }, 401);

    const { path, content, append, binary } = await c.req.json();

    if (binary) {
        // Write binary file (PDF) directly to vault filesystem
        // No CouchDB push — LiveSync only handles text notes, not binary attachments.
        // PDFs sync to iOS via iCloud/filesystem if vault is in iCloud,
        // or are available via the PDF proxy URL in the note.
        const buf = Buffer.from(content, "base64");
        const fullPath = safePath(path);
        await fs.mkdir(dirname(fullPath), { recursive: true });
        await fs.writeFile(fullPath, buf);
    } else {
        // writeNote() handles filesystem write + CouchDB push internally
        await writeNote({ path, content, append: append ?? false });
    }

    return c.json({ success: true, path });
});
```

### 6. Connector integration: `connector.py`

#### New constants

```python
SHOOTING_DATES_PRODUCT_ID = "69b2cfcd0df77ff4010e4ac8"
SHOOTING_DATES_PRODUCT_NAME = "shooting dates:"  # lowercase for case-insensitive
```

#### New helper

```python
def _extract_shooting_dates(products):
    """Like _extract_project_code but for Shooting Dates product."""
    for prod in (products or []):
        pid = prod.get('productId')
        name = (prod.get('name') or '').strip().lower()
        if pid == SHOOTING_DATES_PRODUCT_ID or name == SHOOTING_DATES_PRODUCT_NAME:
            return (prod.get('desc') or '').strip() or None
    return None
```

#### Sync integration

In **both** `sync_documents()` and `_upsert_single_document()`, after the items loop (extending the existing project_code extraction). The code is identical in both locations — `_upsert_single_document()` is the shared path used by WriteGateway's sync-back:

```python
project_code = _extract_project_code(item.get('products'))
shooting_raw = _extract_shooting_dates(item.get('products'))

# Update document row
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

#### Queue flush after sync

At the end of `sync_all()` (the function that calls all sync_* functions):

```python
# After all syncs complete, flush pending Obsidian note queue
try:
    from skills.job_tracker import flush_note_queue
    count = flush_note_queue()
    if count:
        logger.info(f"[JOB_TRACKER] Flushed {count} job notes to Obsidian")
except Exception as e:
    logger.error(f"[JOB_TRACKER] Queue flush failed: {e}")
```

### 7. WriteGateway integration

**No direct hook needed.** The existing `_sync_back_async()` already calls `connector.sync_single_document()`, which runs `_upsert_single_document()`. Since we add the `ensure_job()` call to `_upsert_single_document()`, the WriteGateway path is covered automatically:

```
WriteGateway.execute() → create_estimate
    ↓
_sync_back_async() → connector.sync_single_document("estimate", entity_id)
    ↓
_upsert_single_document() → extracts project_code + shooting_dates → ensure_job()
    ↓
job_note_queue populated → flushed on next sync or manual trigger
```

For immediate note creation after WriteGateway (optional), add to `_sync_back_async()`:

```python
# After sync-back completes, flush queue for this job only
try:
    from skills.job_tracker import flush_note_queue
    flush_note_queue()
except Exception:
    pass  # queue will be flushed later
```

### 8. API endpoints: `api.py`

| Endpoint | Method | Purpose | Auth |
|----------|--------|---------|------|
| `/api/jobs` | GET | List jobs. Query params: `status`, `quarter`, `limit` | Triple-auth (added to PERMISSION_MATRIX, roles: admin, viewer) |
| `/api/jobs` | POST | Create job. Body: `{project_code, client_name, shooting_dates_raw}` | Triple-auth (admin only) |
| `/api/jobs/{code}` | GET | Job detail + expenses list | Triple-auth (admin, viewer) |
| `/api/jobs/{code}` | PATCH | Update status, shooting_dates, invoice_id | Triple-auth (admin only) |
| `/api/jobs/{code}/sync-note` | POST | Force re-render + push to Obsidian | Triple-auth (admin only) |
| `/api/jobs/flush-queue` | POST | Process pending Obsidian note queue | Triple-auth (admin only) |

All endpoints added to `PERMISSION_MATRIX` in `auth.py` following existing patterns.

### 9. Brain integration

Brain's `holded_query` skill already calls holded-connector API. Add job-related queries:

```typescript
// In services/brain/src/skills/holded.ts
// New query types for holded_query tool:
"open_jobs"     → GET /api/jobs?status=open
"job_detail"    → GET /api/jobs/{code}
"create_job"    → POST /api/jobs
"update_job"    → PATCH /api/jobs/{code}
```

When a user tells Brain "open a new job for NETFLIX-260315":
1. Brain calls `POST /api/jobs` with project_code + any known details
2. Holded connector creates the job row + queues note creation
3. Brain can optionally create the Holded quote too via `holded_query`

### 10. Status lifecycle

```
open → shooting → invoiced → closed
  ↑       |           |
  └───────┘           |   (reopen if needed)
  └───────────────────┘
```

| Transition | Trigger | Who |
|------------|---------|-----|
| → open | Job created (ensure_job) | Automatic |
| open → shooting | First shooting date reached, or manual | Brain reminder / user via PATCH |
| shooting → invoiced | Invoice created with same project_code | Automatic (ensure_job detects invoice_id) |
| invoiced → closed | Manual confirmation (all done) | User via PATCH |
| any → open | Reopen if needed | User via PATCH |

The `invoiced` transition is automatic: when `ensure_job()` receives an `invoice_id` and current status is `open` or `shooting`, it sets status to `invoiced`.

---

## Obsidian Note Template

**Path:** `ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}/{code}.md`
**Attachments:** `ACCOUNTS/SEGUIMIENTO_TRABAJOS/{year}/{quarter}/attachments/{code}_{doc_number}.pdf`

Quarter derived from first shooting date. Fallback: document date. Fallback: current date.

```markdown
---
project_code: {code}
client: {client_name}
client_email: {client_email}
status: {status}
shooting_dates: {shooting_dates_parsed}
created: {created_at}
quote_id: {estimate_id}
quote_number: {estimate_number}
invoice_id: {invoice_id}
invoice_number: {invoice_number}
tags: [coyote, job, seguimiento]
---

# {code}

> **Client:** {client_name}
> **Shooting:** {shooting_dates_raw}
> **Status:** {status_emoji} {status}

---

## Quote
![[{code}_{estimate_number}.pdf]]
[Download latest PDF]({pdf_proxy_url})

## Expenses & Tickets

| Date | Concept | Amount | Source |
|------|---------|--------|--------|
{expense_rows}

**Total expenses:** €{total_expenses}

## Email Thread
- **Job ref for emails:** Include `REF: {code}` in subject line
- **Client contact:** {client_email}

## Notes


## Invoicing Checklist
- [ ] All shooting dates completed
- [ ] Expenses reviewed
- [ ] Equipment returned / damages checked
- [ ] Final invoice created
```

Status emojis: open → 🟢, shooting → 🎬, invoiced → 📄, closed → ✅

PDF proxy URL format: `https://holded.coyoterent.com/api/entities/estimates/{estimate_id}/pdf`

---

## Trigger Summary

| Event | System | Action |
|-------|--------|--------|
| Quote created in Holded UI | sync_documents() | Detect project_code → ensure_job() → queue note |
| Quote created by AI agent | WriteGateway → _sync_back_async → _upsert_single_document | ensure_job() → queue note |
| Brain "open new job" | POST /api/jobs | ensure_job() → queue note → immediate flush |
| Quote modified | sync_documents() | Update job → queue note update |
| Purchase with project_code | sync_documents() | Queue expense update on existing note |
| Manual re-sync | POST /api/jobs/{code}/sync-note | Force re-render + push |
| Queue flush | End of sync_all() + POST /api/jobs/flush-queue | Process all pending notes |

---

## Environment Variables

Add to holded-connector `.env` and `.env.example`:

```bash
# Brain API — for Obsidian vault writes via job tracker
BRAIN_API_URL=http://localhost:3100        # Brain server URL
BRAIN_INTERNAL_KEY=your_secret_here        # Must match Brain's BRAIN_INTERNAL_KEY
```

Brain already has `BRAIN_INTERNAL_KEY` configured for its `/internal/*` routes.

---

## Security

- **Path sanitization:** `project_code` stripped of path-unsafe chars before building vault paths via `sanitize_for_path()`
- **Markdown injection:** Client names and codes escaped via `sanitize_for_markdown()` before note rendering
- **PDF size cap:** Skip embedding if PDF > 5MB (link only)
- **Brain API auth:** `x-api-key` header with `BRAIN_INTERNAL_KEY` (matches existing Brain internal route pattern)
- **API auth:** All `/api/jobs/*` endpoints use triple-auth middleware + PERMISSION_MATRIX roles
- **SQL:** All queries parameterized (no f-string interpolation of user data)
- **Date parser:** Returns empty list on unparseable input, never raises
- **No FK constraints on jobs table:** Intentional — Holded IDs are TEXT, orphaned references are acceptable (job note still valid even if estimate is deleted)

## Performance

- **PDF fetch is queued** — never blocks sync_documents()
- **Hash-based skip** — PDFs only re-downloaded when content changes (MD5 comparison)
- **Batch queue flush** — all pending notes processed in one pass after sync_all()
- **Retry with cap** — failed Obsidian writes retry up to 5 times, then stop
- **Queue cleanup** — processed items purged after 30 days during flush
- **Partial index** on job_note_queue for pending items (PostgreSQL)

## Error Handling

- Brain API unreachable → queued for retry, sync continues normally
- PDF fetch timeout → note created without embedded PDF, link still works
- Date parse failure → empty shooting_dates, quarter falls back to doc date
- Duplicate project_code → upsert (ON CONFLICT UPDATE), no crash
- Multiple docs with same project_code → job row merges data (estimate_id + invoice_id can coexist)
- WriteGateway creates doc → sync_back triggers ensure_job() automatically, no extra hook needed

---

## File Changes Summary

| File | Change |
|------|--------|
| `connector.py` | Add SHOOTING_DATES constants, `_extract_shooting_dates()`, `shooting_dates_raw` columns, `jobs` + `job_note_queue` tables in `init_db()`, ALTER TABLE migrations, job trigger in sync, queue flush in `sync_all()` |
| `skills/job_tracker.py` | **New file** — core skill: date parser, sanitizers, ensure_job, render_job_note, sync_job_to_obsidian, flush_note_queue |
| `api.py` | Add `/api/jobs/*` endpoints (6 routes) |
| `auth.py` | Add job endpoints to PERMISSION_MATRIX |
| `services/brain/src/routes/internal.ts` | **New route** `/internal/obsidian-write` for note + binary file writes |
| `services/brain/src/skills/holded.ts` | Add job query types to holded_query |
| `.env.example` | Add BRAIN_API_URL, BRAIN_INTERNAL_KEY |
| `CLAUDE.md` | Document job tracker skill, API endpoints, env vars |

---

**Last Updated:** 2026-03-12
