# Fase 5a: Split connector.py Holded API Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract ~900 lines of Holded API code from connector.py to `app/holded/` (5 modules), leaving connector.py as a thinner facade (~1140 lines).

**Architecture:** Each module in `app/holded/` has one responsibility (HTTP client, bulk sync, upsert, single sync, write wrappers). Modules import from `app.db.connection` and peer modules — never from connector.py. connector.py's `__getattr__` proxy re-exports everything for backwards compatibility.

**Tech Stack:** Python 3.11, requests, SQLite/PostgreSQL dual-mode

**Spec:** `docs/superpowers/specs/2026-04-09-fase5a-connector-split-design.md`

---

## File Structure

```
app/holded/
  __init__.py          # Re-exports
  client.py            # HTTP client (fetch, post, put, delete)
  sync.py              # Bulk sync (sync_invoices, etc.)
  upsert.py            # DB upsert (_upsert_single_document, etc.)
  sync_single.py       # Single-entity sync
  write_wrappers.py    # High-level writes (create_invoice, etc.)
connector.py           # Facade (modified: add app.holded to __getattr__)
```

## Execution Order

| Task | Module | Lines moved | Risk |
|------|--------|-------------|------|
| 1 | `client.py` + `__init__.py` | ~190 | Low — self-contained |
| 2 | `upsert.py` | ~210 | Low — pure DB logic |
| 3 | `sync.py` | ~350 | Medium — largest module |
| 4 | `sync_single.py` | ~90 | Low — small, isolated |
| 5 | `write_wrappers.py` | ~150 | Low — thin wrappers |
| 6 | Update connector.py facade | ~20 | Medium — wire up __getattr__ |
| 7 | Final verification + deploy | 0 | Low |

---

### Task 1: Extract app/holded/client.py + __init__.py

**Files:**
- Create: `app/holded/__init__.py`
- Create: `app/holded/client.py`
- Modify: `connector.py` — remove moved functions, update imports

- [ ] **Step 1: Create app/holded/ directory and __init__.py**

```bash
mkdir -p app/holded
touch app/holded/__init__.py
```

- [ ] **Step 2: Create app/holded/client.py**

Move from connector.py (L69-146, L795-861, L946-956):
- Constants: `PROYECTO_PRODUCT_ID`, `PROYECTO_PRODUCT_NAME`, `SHOOTING_DATES_PRODUCT_ID`, `SHOOTING_DATES_PRODUCT_NAME`
- `extract_ret(prod)` (L80-92)
- `fetch_data(endpoint, params=None)` (L94-146)
- `post_data(endpoint, payload)` (L795-815)
- `put_data(endpoint, payload)` (L817-837)
- `delete_data(endpoint)` (L839-861)
- `holded_put(endpoint, data)` (L946-956)

The file must import HEADERS, BASE_URL, SAFE_MODE from `app.db.connection`:

```python
"""Holded API HTTP client. All external API calls go through here."""
import os
import json
import time
import logging
import requests
from app.db.connection import HEADERS, BASE_URL, SAFE_MODE

logger = logging.getLogger(__name__)

# Holded product IDs for project tracking
PROYECTO_PRODUCT_ID = os.getenv("HOLDED_PROYECTO_PRODUCT_ID", "69b2b35f75ae381d8f05c133")
PROYECTO_PRODUCT_NAME = "proyect ref:"
SHOOTING_DATES_PRODUCT_ID = os.getenv("HOLDED_SHOOTING_DATES_PRODUCT_ID", "69b2cfcd0df77ff4010e4ac8")
SHOOTING_DATES_PRODUCT_NAME = "shooting dates:"
```

Then paste each function verbatim from connector.py. Functions reference `HEADERS`, `BASE_URL`, `SAFE_MODE` — these now come from the import above instead of module-level snapshots.

- [ ] **Step 3: Update __init__.py with client re-exports**

```python
"""Holded API integration layer."""
from app.holded.client import (
    fetch_data, post_data, put_data, delete_data, holded_put, extract_ret,
    HEADERS, BASE_URL, SAFE_MODE,
    PROYECTO_PRODUCT_ID, PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID, SHOOTING_DATES_PRODUCT_NAME,
)
```

- [ ] **Step 4: Remove moved functions from connector.py**

Remove the function bodies of `extract_ret`, `fetch_data`, `post_data`, `put_data`, `delete_data`, `holded_put` and the Holded constants block (L69-78). Add import at the top of connector.py:

```python
from app.holded.client import (
    fetch_data, post_data, put_data, delete_data, holded_put, extract_ret,
    PROYECTO_PRODUCT_ID, PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID, SHOOTING_DATES_PRODUCT_NAME,
)
```

Keep `HEADERS`, `BASE_URL`, `SAFE_MODE` in connector.py's module-level snapshots (L49-53) — they're used by domain functions that stay in connector.py.

- [ ] **Step 5: Verify**

```bash
python3 -c "import ast; ast.parse(open('connector.py').read()); ast.parse(open('app/holded/client.py').read()); print('OK')"
python3 -c "from app.holded.client import fetch_data, post_data; print('import OK')"
python3 -c "import connector; connector.post_data; connector.fetch_data; print('facade OK')"
python3 -m pytest tests/ -q
```
Expected: 96 passed

- [ ] **Step 6: Commit**

```bash
git add app/holded/ connector.py
git commit -m "refactor: extract app/holded/client.py (Holded API HTTP client)"
```

---

### Task 2: Extract app/holded/upsert.py

**Files:**
- Create: `app/holded/upsert.py`
- Modify: `connector.py` — remove moved functions
- Modify: `app/holded/__init__.py` — add re-exports

- [ ] **Step 1: Create app/holded/upsert.py**

Move from connector.py (L498-704):
- `_upsert_single_document(cursor, doc, table, items_table, fk_column)` (L498-637)
- `_upsert_single_contact(cursor, contact)` (L638-671)
- `_upsert_single_product(cursor, product)` (L673-704)

```python
"""DB upsert logic for Holded entities. Handles SQLite vs PostgreSQL dialects."""
import json
import logging
from app.db.connection import _q, _num, _USE_SQLITE, _row_val

logger = logging.getLogger(__name__)
```

These functions receive a `cursor` parameter — they don't manage connections or transactions.

- [ ] **Step 2: Remove from connector.py, add import**

```python
from app.holded.upsert import _upsert_single_document, _upsert_single_contact, _upsert_single_product
```

- [ ] **Step 3: Update __init__.py**

Add upsert re-exports (these are internal, prefixed with `_`, but sync.py needs them).

- [ ] **Step 4: Verify + commit**

```bash
python3 -m pytest tests/ -q
git commit -m "refactor: extract app/holded/upsert.py (DB upsert logic)"
```

---

### Task 3: Extract app/holded/sync.py

**Files:**
- Create: `app/holded/sync.py`
- Modify: `connector.py` — remove moved functions
- Modify: `app/holded/__init__.py` — add re-exports

This is the LARGEST module (~350 lines).

- [ ] **Step 1: Create app/holded/sync.py**

Move from connector.py (L147-496):
- `_extract_project_code(products)` (L147-157)
- `_extract_shooting_dates(products)` (L159-168)
- `sync_documents(doc_type, table, items_table, fk_column)` (L170-325)
- `sync_invoices()`, `sync_purchases()`, `sync_estimates()` (L326-334)
- `sync_accounts()` (L336-356)
- `sync_products()` (L358-399)
- `sync_contacts()` (L401-436)
- `sync_projects()` (L438-464)
- `sync_payments()` (L466-496)

```python
"""Bulk sync engine: pull all entities from Holded API and upsert to local DB."""
import json
import time
import logging
from app.db.connection import get_db, release_db, _q, _num, _row_val, _fetch_one_val, _USE_SQLITE
from app.holded.client import fetch_data, PROYECTO_PRODUCT_ID, PROYECTO_PRODUCT_NAME, SHOOTING_DATES_PRODUCT_ID, SHOOTING_DATES_PRODUCT_NAME, extract_ret
from app.holded.upsert import _upsert_single_document, _upsert_single_contact, _upsert_single_product

logger = logging.getLogger(__name__)
```

**IMPORTANT:** `sync_documents` has a lazy import of `skills.job_tracker` inside a try/except. Keep this exactly as-is.

**IMPORTANT:** `sync_documents` calls `_upsert_single_document` which is now in `app.holded.upsert` — imported above.

- [ ] **Step 2: Remove from connector.py, add import**

```python
from app.holded.sync import (
    sync_documents, sync_invoices, sync_purchases, sync_estimates,
    sync_accounts, sync_products, sync_contacts, sync_projects, sync_payments,
    _extract_project_code, _extract_shooting_dates,
)
```

- [ ] **Step 3: Update __init__.py with sync re-exports**

- [ ] **Step 4: Verify + commit**

```bash
python3 -m pytest tests/ -q
git commit -m "refactor: extract app/holded/sync.py (bulk sync engine)"
```

---

### Task 4: Extract app/holded/sync_single.py

**Files:**
- Create: `app/holded/sync_single.py`
- Modify: `connector.py`
- Modify: `app/holded/__init__.py`

- [ ] **Step 1: Create app/holded/sync_single.py**

Move from connector.py (L706-793):
- `sync_single_document(doc_type, doc_id)` (L706-755)
- `sync_single_contact(contact_id)` (L757-774)
- `sync_single_product(product_id)` (L776-793)

```python
"""Single-entity sync: refresh one entity from Holded API. Used by write_gateway sync-back."""
import logging
from app.db.connection import get_db, release_db
from app.holded.client import fetch_data
from app.holded.upsert import _upsert_single_document, _upsert_single_contact, _upsert_single_product

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Remove from connector.py, add import**

- [ ] **Step 3: Verify + commit**

```bash
python3 -m pytest tests/ -q
git commit -m "refactor: extract app/holded/sync_single.py"
```

---

### Task 5: Extract app/holded/write_wrappers.py

**Files:**
- Create: `app/holded/write_wrappers.py`
- Modify: `connector.py`
- Modify: `app/holded/__init__.py`

- [ ] **Step 1: Create app/holded/write_wrappers.py**

Move from connector.py (L863-985):
- `create_invoice(invoice_data)` (L863-871)
- `update_estimate(estimate_id, estimate_data)` (L873-879)
- `fetch_estimate_fresh(estimate_id)` (L881-932)
- `create_contact(contact_data)` (L934-944)
- `create_estimate(estimate_data)` (L958-966)
- `send_document(doc_type, doc_id, send_data)` (L968-975)
- `create_product(product_data)` (L977-985)

```python
"""High-level Holded write operations. Used by agent_writes router and connector facade."""
import logging
import requests
from app.db.connection import HEADERS, BASE_URL, SAFE_MODE, _num
from app.holded.client import post_data, put_data, fetch_data

logger = logging.getLogger(__name__)
```

**IMPORTANT:** `fetch_estimate_fresh` makes a direct `requests.get()` call to the Holded API (not through `fetch_data`). It needs `HEADERS` and `BASE_URL` imported.

- [ ] **Step 2: Remove from connector.py, add import**

- [ ] **Step 3: Update __init__.py with all write wrapper re-exports**

Final `__init__.py`:
```python
"""Holded API integration layer."""
from app.holded.client import (
    fetch_data, post_data, put_data, delete_data, holded_put, extract_ret,
    PROYECTO_PRODUCT_ID, PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID, SHOOTING_DATES_PRODUCT_NAME,
)
from app.holded.upsert import _upsert_single_document, _upsert_single_contact, _upsert_single_product
from app.holded.sync import (
    sync_documents, sync_invoices, sync_purchases, sync_estimates,
    sync_accounts, sync_products, sync_contacts, sync_projects, sync_payments,
)
from app.holded.sync_single import sync_single_document, sync_single_contact, sync_single_product
from app.holded.write_wrappers import (
    create_invoice, create_estimate, create_contact, create_product,
    update_estimate, send_document, fetch_estimate_fresh,
)
```

- [ ] **Step 4: Verify + commit**

```bash
python3 -m pytest tests/ -q
git commit -m "refactor: extract app/holded/write_wrappers.py"
```

---

### Task 6: Update connector.py facade

**Files:**
- Modify: `connector.py` — update `__getattr__` to proxy `app.holded.*`

- [ ] **Step 1: Update __getattr__ to include app.holded**

Replace the current `__getattr__` with one that checks both `app.db.connection` and `app.holded`:

```python
import app.holded as _holded

_MUTABLE_CONN_ATTRS = ('API_KEY', 'DATABASE_URL', '_pool')

def __getattr__(name):
    """Proxy attribute lookups to canonical modules."""
    if name in _MUTABLE_CONN_ATTRS:
        return getattr(_conn, name)
    # Check app.holded for any Holded API functions
    try:
        return getattr(_holded, name)
    except AttributeError:
        pass
    raise AttributeError(f"module 'connector' has no attribute {name!r}")
```

- [ ] **Step 2: Clean up redundant imports in connector.py**

Now that `__getattr__` delegates to `app.holded`, remove the explicit imports added in Tasks 1-5. The facade pattern means any `connector.X` call will find `X` in `app.holded` via `__getattr__`.

However, keep explicit imports for functions used **within connector.py itself** (by the domain logic that stays). For example, if `get_amortizations()` in connector.py calls `fetch_data()`, it needs either an explicit import or to go through `app.holded.client.fetch_data`.

The cleanest approach: keep the explicit re-export imports from Tasks 1-5 for functions used within connector.py. Let `__getattr__` handle only external callers requesting functions that connector.py doesn't use itself.

- [ ] **Step 3: Verify facade works for all consumers**

```bash
python3 -c "
import connector
# DB layer (from app.db)
connector.get_db; connector.init_db; connector.save_setting
# Holded client (from app.holded.client)
connector.fetch_data; connector.post_data; connector.put_data
# Sync (from app.holded.sync)
connector.sync_invoices; connector.sync_contacts; connector.sync_products
# Sync single (from app.holded.sync_single)
connector.sync_single_document; connector.sync_single_contact
# Write wrappers (from app.holded.write_wrappers)
connector.create_invoice; connector.create_estimate; connector.fetch_estimate_fresh
# Domain logic (still in connector.py)
connector.get_amortizations; connector.get_uploads_dir
# Mutable globals
connector.API_KEY
print('ALL OK')
"
```

- [ ] **Step 4: Verify line count and test suite**

```bash
wc -l connector.py app/holded/*.py
python3 -m pytest tests/ -q
```
Expected: connector.py ~1140 lines, app/holded/ ~900 lines total, 96 tests pass

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor: connector.py facade proxies to app/holded/ (Fase 5a complete)"
```

---

### Task 7: Final verification + deploy

- [ ] **Step 1: AST parse all files**

```bash
python3 -c "
import ast, glob
for f in ['connector.py', 'api.py', 'write_gateway.py'] + glob.glob('app/holded/*.py') + glob.glob('app/routers/*.py'):
    ast.parse(open(f).read())
    print(f'OK: {f}')
"
```

- [ ] **Step 2: Full test suite**

```bash
python3 -m pytest tests/ -v
```
Expected: 96 passed

- [ ] **Step 3: Push and deploy**

```bash
git push
ssh coyote-server 'cd /opt/holded-connector && git pull && docker compose up -d --build holded-api'
```

- [ ] **Step 4: Smoke test**

```bash
ssh coyote-server 'curl -s http://localhost:8000/health'
# Trigger sync to verify holded API layer works
ssh coyote-server 'curl -s -X POST http://localhost:8000/api/sync -H "Authorization: Bearer $TOKEN"'
```

---

## Notes

- **Line numbers are approximate** — they shift as earlier tasks remove code. Always search for function names.
- **Import rule:** `app/holded/` modules import from `app.db.connection` and peer modules. NEVER from `connector.py`.
- **connector.py remains the public facade** — all existing `import connector; connector.X()` calls continue to work.
- **write_gateway.py** imports `connector.post_data` and `connector.put_data` — works via facade.
- **Routers** import `connector` — works via facade.
