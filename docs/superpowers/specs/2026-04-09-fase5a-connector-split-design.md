# Fase 5a: Split connector.py Holded API Layer — Design Spec

## Context

connector.py is a 2048-line facade that does too much: Holded API client, sync engine, upsert logic, file management, analysis, and amortizations. Fase 1 already extracted the DB layer to `app/db/`. Fase 5a extracts the Holded API layer to `app/holded/`, leaving connector.py as a thinner facade (~1140 lines) with domain logic and re-exports.

## Scope

**In scope:** Move Holded API client, sync engine, upsert functions, sync-single, and write wrappers to `app/holded/`.

**Out of scope:** File management, analysis domain, amortization domain stay in connector.py for now (Fase 5c or later).

## Architecture

```
app/
  holded/
    __init__.py          # Re-exports for clean imports
    client.py            # Low-level Holded API: fetch, post, put, delete
    sync.py              # Bulk sync: sync_invoices, sync_contacts, etc.
    upsert.py            # DB upsert logic: _upsert_single_document/contact/product
    sync_single.py       # Single-entity sync: sync_single_document/contact/product
    write_wrappers.py    # High-level writes: create_invoice, create_estimate, etc.
connector.py             # Facade: __getattr__ proxy + domain logic (file, analysis, amortization)
```

### Module Dependencies

```
app/holded/client.py     → app.db.connection (SAFE_MODE, HEADERS, BASE_URL)
app/holded/sync.py       → client.py, upsert.py, app.db.connection
app/holded/upsert.py     → app.db.connection (_q, _num, _row_val helpers)
app/holded/sync_single.py→ client.py, upsert.py, app.db.connection
app/holded/write_wrappers.py → client.py, app.db.connection (SAFE_MODE)
connector.py             → app.holded.* (via __getattr__ or explicit imports)
```

**Import rule:** `app/holded/` modules NEVER import from `connector.py`. They import from `app.db.connection` and from each other. connector.py imports from `app.holded.*` to re-export.

## Module Details

### app/holded/client.py (~190 lines)

Holded API low-level client. All HTTP calls to Holded go through here.

**Move from connector.py:**
- `BASE_URL`, `HEADERS` constants (L69-78)
- `extract_ret(prod)` (L80-92)
- `fetch_data(endpoint, params)` (L94-146) — GET with pagination, retry, timeout
- `post_data(endpoint, payload)` (L795-815) — POST with SAFE_MODE
- `put_data(endpoint, payload)` (L817-837) — PUT with SAFE_MODE
- `delete_data(endpoint)` (L839-861) — DELETE
- `holded_put(endpoint, data)` (L946-956) — legacy PUT alias

**Imports needed:** `requests`, `os`, `logging`, `time`, `json`, `app.db.connection` (for `SAFE_MODE`)

**HEADERS and BASE_URL** currently read from `os.environ` at module level. Keep this pattern — they're constants set at startup.

### app/holded/sync.py (~350 lines)

Bulk sync engine. Pulls all entities from Holded API and upserts to local DB.

**Move from connector.py:**
- `_extract_project_code(products)` (L147-157)
- `_extract_shooting_dates(products)` (L159-168)
- `sync_documents(doc_type, table, items_table, fk_column)` (L170-325) — core sync with pagination, upsert, job tracker
- `sync_invoices()`, `sync_purchases()`, `sync_estimates()` (L326-334) — thin wrappers
- `sync_accounts()` (L336-356) — ledger accounts sync
- `sync_products()` (L358-399) — products + pack_components sync
- `sync_contacts()` (L401-436) — contacts sync
- `sync_projects()` (L438-464) — projects sync
- `sync_payments()` (L466-496) — payments sync

**Imports needed:** `app.holded.client` (fetch_data), `app.holded.upsert`, `app.db.connection` (get_db, release_db, _q, _num, _row_val, _fetch_one_val)

**Note:** `sync_documents` imports `skills.job_tracker` lazily inside a try/except. Keep this pattern.

### app/holded/upsert.py (~210 lines)

DB upsert logic for synced entities. Handles SQLite vs PostgreSQL dialects.

**Move from connector.py:**
- `_upsert_single_document(cursor, doc, table, items_table, fk_column)` (L498-637)
- `_upsert_single_contact(cursor, contact)` (L638-671)
- `_upsert_single_product(cursor, product)` (L673-704)

**Imports needed:** `app.db.connection` (_q, _num, _USE_SQLITE, _row_val), `json`, `logging`

**These functions receive a cursor** (not a connection) — they don't manage transactions. The caller (sync.py) manages the connection and commit.

### app/holded/sync_single.py (~90 lines)

Sync a single entity by ID from Holded API to local DB. Used by write_gateway's sync-back.

**Move from connector.py:**
- `sync_single_document(doc_type, doc_id)` (L706-755)
- `sync_single_contact(contact_id)` (L757-774)
- `sync_single_product(product_id)` (L776-793)

**Imports needed:** `app.holded.client` (fetch_data), `app.holded.upsert`, `app.db.connection` (get_db, release_db)

### app/holded/write_wrappers.py (~150 lines)

High-level write operations. Used by agent_writes router (legacy direct path) and connector facade.

**Move from connector.py:**
- `create_invoice(invoice_data)` (L863-871)
- `update_estimate(estimate_id, estimate_data)` (L873-879)
- `fetch_estimate_fresh(estimate_id)` (L881-932)
- `create_contact(contact_data)` (L934-944)
- `create_estimate(estimate_data)` (L958-966)
- `send_document(doc_type, doc_id, send_data)` (L968-975)
- `create_product(product_data)` (L977-985)

**Imports needed:** `app.holded.client` (post_data, put_data, fetch_data), `app.db.connection` (SAFE_MODE), `logging`

### app/holded/__init__.py (~15 lines)

Re-exports for clean imports:
```python
from app.holded.client import fetch_data, post_data, put_data, delete_data, holded_put, extract_ret, HEADERS, BASE_URL
from app.holded.sync import sync_invoices, sync_purchases, sync_estimates, sync_accounts, sync_products, sync_contacts, sync_projects, sync_payments
from app.holded.sync_single import sync_single_document, sync_single_contact, sync_single_product
from app.holded.write_wrappers import create_invoice, create_estimate, create_contact, create_product, update_estimate, send_document, fetch_estimate_fresh
```

### connector.py changes

After extraction, connector.py's `__getattr__` proxy adds `app.holded` as a lookup source:

```python
# In __getattr__:
# 1. Check app.db.connection (existing)
# 2. Check app.holded (new)
# 3. Raise AttributeError
```

All existing callers (`import connector; connector.post_data(...)`) continue to work unchanged.

## Verification

After each module extraction:
1. AST parse all files
2. `python3 -m pytest tests/ -q` (96 tests must pass)
3. `python3 -c "import connector; connector.sync_invoices; connector.post_data; print('facade OK')"`

Final:
- connector.py should be ~1140 lines (down from 2048)
- `app/holded/` should total ~900 lines
- All routers that import `connector` still work (facade preserves API)
- `write_gateway.py` imports `connector.post_data` — still works via facade

## Risk

| Risk | Mitigation |
|------|-----------|
| Circular imports | app/holded/ never imports connector.py — only app.db.* and peer modules |
| Broken facade | __getattr__ proxy catches all attribute lookups and delegates |
| Shared constants (HEADERS, BASE_URL) | Move to client.py, re-export via __init__.py |
| SAFE_MODE used in write_wrappers | Import from app.db.connection (already the canonical source) |
