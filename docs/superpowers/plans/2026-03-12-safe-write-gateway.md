# Safe Write Gateway — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a centralized WriteGateway that all write operations pass through, providing validation, preview, confirmation, execution, sync-back, and audit logging — making Holded writes 100% safe.

**Architecture:** A 6-stage pipeline (validate → preview → confirm/log → execute → sync → audit) implemented as a `WriteGateway` class. AI agent writes require user confirmation; REST writes are audit-logged only. Holded remains the single source of truth — after every write, the gateway fetches the full record back to keep the local DB in sync.

**Tech Stack:** Python 3.9+, FastAPI, PostgreSQL (Supabase) / SQLite, Holded REST API, Anthropic Claude API

**Spec:** `docs/superpowers/specs/2026-03-12-safe-write-gateway-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `write_gateway.py` | WriteGateway class — pipeline orchestration, operation registry, rate limiting, execution |
| `write_validators.py` | Validation rules per operation, status transition maps, input sanitization |
| `write_preview.py` | Preview builder, warning generator, line item calculations, reversibility assessment |
| `tests/test_write_validators.py` | Unit tests for all validators |
| `tests/test_write_preview.py` | Unit tests for preview builder and warnings |
| `tests/test_write_gateway.py` | Integration tests for full pipeline |

### Modified Files
| File | Changes |
|------|---------|
| `connector.py` | Add `write_audit_log` table to `init_db()`, refactor `post_data()`/`put_data()` to return structured errors, add `delete_data()`, add single-entity sync helpers, add audit log helpers |
| `ai_agent.py` | Replace write tool executors with gateway calls, enhance confirmation flow with preview data |
| `api.py` | Wrap amortization/web_include endpoints with gateway audit, add audit log endpoints |

---

## Chunk 1: Prerequisites — connector.py Foundation

### Task 1: Refactor `post_data()` to accept HTTP 201 and return structured errors

**Files:**
- Modify: `connector.py:936-948`

- [ ] **Step 1: Read current `post_data()` implementation**

Current code at lines 936-948:
```python
def post_data(endpoint, payload):
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted POST to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "id": "SAFE_MODE_ID_TEST", "info": "Dry run successful"}

    url = f"{BASE_URL}{endpoint}"
    response = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Error posting to {endpoint}: {response.status_code} - {response.text}")
        return None
```

- [ ] **Step 2: Refactor to accept 200+201 and return structured errors**

Replace lines 936-948 with:
```python
def post_data(endpoint, payload):
    """POST to Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted POST to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "id": "SAFE_MODE_ID_TEST", "info": "Dry run successful", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 201):
            return response.json()
        else:
            logger.error(f"Error posting to {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout posting to {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error posting to {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}
```

- [ ] **Step 3: Verify existing callers handle the new error format**

Check all callers of `post_data()` in `ai_agent.py`. Current callers check `if result:` which still works — a dict with `{"error": True}` is truthy, but they check `result.get("id")` or `result.get("status")` which won't have the error key. Need to verify no caller breaks.

Search for `post_data(` in `ai_agent.py` and `connector.py`. Each caller that currently does `if result:` should be updated to `if result and not result.get("error"):`.

- [ ] **Step 4: Update callers in ai_agent.py**

In `exec_create_estimate()` (line ~618), `exec_create_invoice()` (line ~637), `exec_create_contact()` (line ~687), `exec_send_document()` (line ~660) — update the result check:

```python
# Before:
if result:
    return {"success": True, "id": result.get("id", "SAFE_MODE"), ...}

# After:
if result and not result.get("error"):
    return {"success": True, "id": result.get("id", "SAFE_MODE"), ...}
return {"success": False, "error": result.get("detail", "Unknown error") if result else "No response"}
```

- [ ] **Step 5: Commit**

```bash
git add connector.py ai_agent.py
git commit -m "refactor: post_data returns structured errors, accepts HTTP 201"
```

---

### Task 2: Refactor `put_data()` to match `post_data()` pattern

**Files:**
- Modify: `connector.py:950-962`

- [ ] **Step 1: Refactor `put_data()`**

Replace lines 950-962 with:
```python
def put_data(endpoint, payload):
    """PUT to Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted PUT to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "info": "Dry run successful", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.put(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 201):
            return response.json()
        else:
            logger.error(f"Error putting to {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout putting to {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error putting to {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}
```

- [ ] **Step 2: Update callers in ai_agent.py**

`exec_update_invoice_status()` (line ~817) — same pattern:
```python
if result and not result.get("error"):
```

- [ ] **Step 3: Commit**

```bash
git add connector.py ai_agent.py
git commit -m "refactor: put_data returns structured errors, accepts HTTP 201"
```

---

### Task 3: Add `delete_data()` helper

**Files:**
- Modify: `connector.py` (after `put_data`, ~line 963)

- [ ] **Step 1: Add `delete_data()` function**

Insert after `put_data()`:
```python
def delete_data(endpoint):
    """DELETE on Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted DELETE to {endpoint}")
        return {"status": 1, "info": "Dry run successful (delete)", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.delete(url, headers=HEADERS, timeout=30)
        if response.status_code in (200, 201, 204):
            try:
                return response.json()
            except ValueError:
                return {"status": 1, "info": "Deleted"}
        else:
            logger.error(f"Error deleting {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout deleting {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error deleting {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}
```

- [ ] **Step 2: Commit**

```bash
git add connector.py
git commit -m "feat: add delete_data() helper with SAFE_MODE support"
```

---

### Task 4: Add `write_audit_log` table to `init_db()`

**Files:**
- Modify: `connector.py` (inside `_init_db_inner()`, after sync_logs table ~line 473)

- [ ] **Step 1: Add the table creation SQL**

Insert after the `sync_logs` CREATE TABLE block:
```python
    # ── Write Audit Log ──────────────────────────────────────────────
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS write_audit_log (
            id              {_serial},
            timestamp       TEXT DEFAULT ({_now}),
            source          TEXT NOT NULL,
            operation       TEXT NOT NULL,
            entity_type     TEXT NOT NULL,
            entity_id       TEXT,
            payload_sent    TEXT,
            response_received TEXT,
            preview_data    TEXT,
            warnings        TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            tables_synced   TEXT,
            reverse_action  TEXT,
            reverse_payload TEXT,
            user_confirmed  BOOLEAN,
            error_detail    TEXT,
            safe_mode       BOOLEAN DEFAULT FALSE,
            conversation_id TEXT,
            checksum        TEXT,
            duration_ms     INTEGER
        )
    ''')

    # Audit log indexes
    if not _USE_SQLITE:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON write_audit_log(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_entity ON write_audit_log(entity_type, entity_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_operation ON write_audit_log(operation)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_status ON write_audit_log(status)')
```

- [ ] **Step 2: Add audit log helper functions**

Add at end of `connector.py` (before final line):
```python
# ── Write Audit Log Helpers ──────────────────────────────────────────

def insert_audit_log(source, operation, entity_type, payload_sent=None,
                     preview_data=None, warnings=None, status='pending',
                     safe_mode=False, conversation_id=None):
    """Insert a new audit log entry. Returns the new row ID."""
    conn = get_db()
    try:
        cursor = _cursor(conn)
        cursor.execute(_q('''
            INSERT INTO write_audit_log
                (source, operation, entity_type, payload_sent, preview_data,
                 warnings, status, safe_mode, conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''), (source, operation, entity_type,
               json.dumps(payload_sent) if payload_sent else None,
               json.dumps(preview_data) if preview_data else None,
               json.dumps(warnings) if warnings else None,
               status, safe_mode, conversation_id))
        if _USE_SQLITE:
            audit_id = cursor.lastrowid
        else:
            cursor.execute('SELECT lastval()')
            audit_id = cursor.fetchone()[0]
        conn.commit()
        return audit_id
    except Exception as e:
        logger.error(f"Failed to insert audit log: {e}")
        conn.rollback()
        return None
    finally:
        release_db(conn)


def update_audit_log(audit_id, **kwargs):
    """Update an existing audit log entry. Accepts any column as kwarg."""
    if not audit_id:
        return
    json_fields = {'payload_sent', 'response_received', 'preview_data',
                   'warnings', 'tables_synced', 'reverse_action', 'reverse_payload'}
    conn = get_db()
    try:
        cursor = _cursor(conn)
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f'{k} = {_q("?")}')
            if k in json_fields and v is not None and not isinstance(v, str):
                vals.append(json.dumps(v))
            else:
                vals.append(v)
        vals.append(audit_id)
        cursor.execute(_q(f'UPDATE write_audit_log SET {", ".join(sets)} WHERE id = ?'), vals)
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update audit log {audit_id}: {e}")
        conn.rollback()
    finally:
        release_db(conn)
```

- [ ] **Step 3: Restart server to verify table creation**

```bash
/usr/bin/python3 -c "
import connector
connector.init_db()
conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute('SELECT count(*) as c FROM write_audit_log')
print('Audit log rows:', connector._fetch_one_val(cur, 'c'))
conn.close()
print('OK: write_audit_log table created')
"
```
Expected: `Audit log rows: 0` and `OK: write_audit_log table created`

- [ ] **Step 4: Commit**

```bash
git add connector.py
git commit -m "feat: add write_audit_log table and audit helper functions"
```

---

### Task 5: Extract single-entity sync helpers

**Files:**
- Modify: `connector.py` (add new functions after sync functions, ~line 880)

- [ ] **Step 1: Add `_upsert_single_document()` helper**

This extracts the per-document upsert logic from `sync_documents()` (lines 713-769) into a reusable function:

```python
def _upsert_single_document(cursor, doc, table, items_table, fk_column):
    """Upsert a single document + its line items into local DB.

    Reuses the same field mapping and SQL patterns as sync_documents().
    `doc` is a single Holded document dict (from GET /documents/{type}/{id}).
    """
    import json as _json

    doc_id = doc.get('id')
    if not doc_id:
        return

    vals = (
        doc_id,
        doc.get('contact'),
        doc.get('contactName'),
        doc.get('desc', ''),
        doc.get('date'),
        _num(doc.get('total')),
        doc.get('status', 0),
        _num(doc.get('paymentsPending')),
        _num(doc.get('paymentsTotal')),
        doc.get('dueDate'),
        doc.get('docNumber', ''),
        _json.dumps(doc.get('tags') or []),
        doc.get('notes', '')
    )

    if _USE_SQLITE:
        cursor.execute(f'''
            INSERT OR REPLACE INTO {table}
                (id, contact_id, contact_name, "desc", date, amount, status,
                 payments_pending, payments_total, due_date, doc_number, tags, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute(f'''
            INSERT INTO {table}
                (id, contact_id, contact_name, "desc", date, amount, status,
                 payments_pending, payments_total, due_date, doc_number, tags, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                contact_id=EXCLUDED.contact_id, contact_name=EXCLUDED.contact_name,
                "desc"=EXCLUDED."desc", date=EXCLUDED.date, amount=EXCLUDED.amount,
                status=EXCLUDED.status, payments_pending=EXCLUDED.payments_pending,
                payments_total=EXCLUDED.payments_total, due_date=EXCLUDED.due_date,
                doc_number=EXCLUDED.doc_number, tags=EXCLUDED.tags, notes=EXCLUDED.notes
        ''', vals)

    # Items: delete + re-insert
    cursor.execute(_q(f'DELETE FROM {items_table} WHERE {fk_column} = ?'), (doc_id,))
    for prod in doc.get('products', []):
        retention = extract_ret(prod) if 'extract_ret' in dir() else _num(prod.get('retention'))
        acc_id = prod.get('accountCode') or prod.get('accountName') or prod.get('account')
        cursor.execute(_q(f'''
            INSERT INTO {items_table}
                ({fk_column}, product_id, name, sku, units, price, subtotal,
                 discount, tax, retention, account, project_id, kind)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        '''), (doc_id, prod.get('productId'), prod.get('name'), prod.get('sku'),
               _num(prod.get('units')), _num(prod.get('price')), _num(prod.get('subtotal')),
               _num(prod.get('discount')), _num(prod.get('tax')), _num(retention), acc_id,
               prod.get('projectid'), prod.get('kind')))
```

- [ ] **Step 2: Add `_upsert_single_contact()` helper**

```python
def _upsert_single_contact(cursor, contact):
    """Upsert a single contact into local DB."""
    cid = contact.get('id')
    if not cid:
        return

    vals = (
        cid,
        contact.get('name', ''),
        contact.get('email', ''),
        contact.get('type', ''),
        contact.get('code', ''),
        contact.get('tradeName', ''),
        contact.get('phone', ''),
        contact.get('mobile', '')
    )

    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR REPLACE INTO contacts
                (id, name, email, type, code, vat, phone, mobile)
            VALUES (?,?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute('''
            INSERT INTO contacts
                (id, name, email, type, code, vat, phone, mobile)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, email=EXCLUDED.email, type=EXCLUDED.type,
                code=EXCLUDED.code, vat=EXCLUDED.vat, phone=EXCLUDED.phone,
                mobile=EXCLUDED.mobile
        ''', vals)
```

- [ ] **Step 3: Add `_upsert_single_product()` helper**

```python
def _upsert_single_product(cursor, product):
    """Upsert a single product into local DB."""
    pid = product.get('id')
    if not pid:
        return

    vals = (
        pid,
        product.get('name', ''),
        product.get('desc', ''),
        _num(product.get('price')),
        _num(product.get('stock')),
        product.get('sku', ''),
        product.get('kind', 'simple')
    )

    if _USE_SQLITE:
        cursor.execute('''
            INSERT OR REPLACE INTO products
                (id, name, "desc", price, stock, sku, kind)
            VALUES (?,?,?,?,?,?,?)
        ''', vals)
    else:
        cursor.execute('''
            INSERT INTO products
                (id, name, "desc", price, stock, sku, kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
                name=EXCLUDED.name, "desc"=EXCLUDED."desc", price=EXCLUDED.price,
                stock=EXCLUDED.stock, sku=EXCLUDED.sku, kind=EXCLUDED.kind
        ''', vals)
```

- [ ] **Step 4: Add high-level sync-back functions**

```python
def sync_single_document(doc_type, doc_id):
    """Fetch one document from Holded and upsert into local DB. Returns tables updated."""
    table_map = {
        "invoice":  ("invoices", "invoice_items", "invoice_id"),
        "estimate": ("estimates", "estimate_items", "estimate_id"),
        "purchase": ("purchase_invoices", "purchase_items", "purchase_id"),
    }
    if doc_type not in table_map:
        logger.error(f"Unknown doc_type for sync: {doc_type}")
        return []

    table, items_table, fk_col = table_map[doc_type]
    data = fetch_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}")
    if not data:
        logger.warning(f"Sync-back: no data returned for {doc_type}/{doc_id}")
        return []

    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_document(cursor, data, table, items_table, fk_col)
        conn.commit()
        return [table, items_table]
    except Exception as e:
        logger.error(f"Sync-back failed for {doc_type}/{doc_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)


def sync_single_contact(contact_id):
    """Fetch one contact from Holded and upsert into local DB."""
    data = fetch_data(f"/invoicing/v1/contacts/{contact_id}")
    if not data:
        return []
    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_contact(cursor, data)
        conn.commit()
        return ["contacts"]
    except Exception as e:
        logger.error(f"Sync-back failed for contact/{contact_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)


def sync_single_product(product_id):
    """Fetch one product from Holded and upsert into local DB."""
    data = fetch_data(f"/invoicing/v1/products/{product_id}")
    if not data:
        return []
    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_product(cursor, data)
        conn.commit()
        return ["products"]
    except Exception as e:
        logger.error(f"Sync-back failed for product/{product_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)
```

- [ ] **Step 5: Commit**

```bash
git add connector.py
git commit -m "feat: add single-entity sync helpers for gateway sync-back"
```

---

## Chunk 2: Core Gateway — Validators, Preview, Gateway Class

### Task 6: Create `write_validators.py`

**Files:**
- Create: `write_validators.py`

- [ ] **Step 1: Create the validators module**

```python
"""
write_validators.py — Validation rules for the Safe Write Gateway.

Each validator returns (is_valid: bool, errors: list[dict], context: dict).
The context dict carries pre-fetched DB data to avoid duplicate queries in preview.
"""

import re
import logging
import connector

logger = logging.getLogger(__name__)

# ── Status Transition Maps ────────────────────────────────────────────

INVOICE_TRANSITIONS = {
    0: {1},           # draft → issued
    1: {2, 3, 4, 5},  # issued → partial, paid, overdue, cancelled
    2: {3, 4, 5},      # partial → paid, overdue, cancelled
    4: {2, 3, 5},      # overdue → partial, paid, cancelled
    3: set(),           # paid → terminal
    5: set(),           # cancelled → terminal
}

ESTIMATE_TRANSITIONS = {
    0: {1},       # draft → pending
    1: {2, 3},    # pending → accepted, rejected
    2: {4},       # accepted → invoiced
    3: set(),     # rejected → terminal
    4: set(),     # invoiced → terminal
}

VALID_TAX_RATES = {0, 4, 10, 21}
VALID_CONTACT_TYPES = {"client", "supplier", "debtor", "creditor", "lead"}
VALID_TAX_OPERATIONS = {"general", "intra", "impexp", "nosujeto", "receq", "exento"}
VALID_PRODUCT_KINDS = {"simple", "pack"}
HOLDED_ID_PATTERN = re.compile(r'^[a-f0-9]{24}$')


# ── Input Sanitization ───────────────────────────────────────────────

def _sanitize_text(value, max_length=500):
    """Strip whitespace and HTML tags, enforce max length."""
    if not value:
        return value
    value = str(value).strip()
    value = re.sub(r'<[^>]+>', '', value)  # strip HTML
    return value[:max_length]


def _validate_holded_id(value, field_name):
    """Validate Holded MongoDB ObjectId format."""
    if not value:
        return {"field": field_name, "msg": f"{field_name} is required"}
    if not HOLDED_ID_PATTERN.match(str(value)):
        return {"field": field_name, "msg": f"{field_name} must be a 24-char hex string (Holded ID)"}
    return None


def _validate_email(email):
    """Basic email format validation."""
    if not email:
        return None  # optional
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return {"field": "email", "msg": f"Invalid email format: {email}"}
    return None


def _validate_amount(value, field_name, min_val=0, max_val=999999.99):
    """Validate numeric amount is within range."""
    if value is None:
        return None
    try:
        v = float(value)
    except (ValueError, TypeError):
        return {"field": field_name, "msg": f"{field_name} must be numeric"}
    if v < min_val or v > max_val:
        return {"field": field_name, "msg": f"{field_name} must be between {min_val} and {max_val}"}
    return None


def _validate_date(value, field_name):
    """Validate Unix timestamp is reasonable (2020-2027)."""
    if value is None:
        return None  # optional
    try:
        ts = int(value)
    except (ValueError, TypeError):
        return {"field": field_name, "msg": f"{field_name} must be an integer (Unix timestamp)"}
    if ts < 1577836800 or ts > 1798761600:  # 2020-01-01 to 2027-01-01
        return {"field": field_name, "msg": f"{field_name} timestamp out of range (2020-2027)"}
    return None


# ── DB Lookup Helpers ────────────────────────────────────────────────

def _fetch_contact(contact_id):
    """Fetch contact from local DB. Returns dict or None."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('SELECT * FROM contacts WHERE id = ?'), (contact_id,))
        row = cursor.fetchone()
        if row:
            return dict(row) if hasattr(row, 'keys') else None
        return None
    finally:
        connector.release_db(conn)


def _fetch_product(product_id):
    """Fetch product from local DB. Returns dict or None."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('SELECT * FROM products WHERE id = ?'), (product_id,))
        row = cursor.fetchone()
        if row:
            return dict(row) if hasattr(row, 'keys') else None
        return None
    finally:
        connector.release_db(conn)


def _fetch_products_batch(product_ids):
    """Fetch multiple products in one query. Returns {id: dict}."""
    if not product_ids:
        return {}
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        placeholders = ','.join([connector._q('?')] * len(product_ids))
        cursor.execute(f'SELECT * FROM products WHERE id IN ({placeholders})', tuple(product_ids))
        rows = cursor.fetchall()
        return {dict(r)['id']: dict(r) for r in rows} if rows else {}
    finally:
        connector.release_db(conn)


def _fetch_document(doc_type, doc_id):
    """Fetch document from local DB. Returns dict or None."""
    table_map = {"invoice": "invoices", "estimate": "estimates",
                 "purchase": "purchase_invoices"}
    table = table_map.get(doc_type)
    if not table:
        return None
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q(f'SELECT * FROM {table} WHERE id = ?'), (doc_id,))
        row = cursor.fetchone()
        return dict(row) if row and hasattr(row, 'keys') else None
    finally:
        connector.release_db(conn)


# ── Validators ───────────────────────────────────────────────────────

def validate_create_invoice(params):
    """Validate create_invoice parameters. Returns (is_valid, errors, context)."""
    return _validate_create_document(params, "invoice")


def validate_create_estimate(params):
    """Validate create_estimate parameters. Returns (is_valid, errors, context)."""
    return _validate_create_document(params, "estimate")


def _validate_create_document(params, doc_type):
    """Shared validation for invoice/estimate creation."""
    errors = []
    context = {"doc_type": doc_type}

    # Contact validation
    contact_id = params.get("contact_id")
    id_err = _validate_holded_id(contact_id, "contact_id")
    if id_err:
        errors.append(id_err)
    else:
        contact = _fetch_contact(contact_id)
        if not contact:
            errors.append({"field": "contact_id", "msg": f"Contact '{contact_id}' not found in database"})
        else:
            context["contact"] = contact

    # Items validation
    items = params.get("items", [])
    if not items:
        errors.append({"field": "items", "msg": "At least one line item is required"})
    elif len(items) > 100:
        errors.append({"field": "items", "msg": "Maximum 100 items per document"})
    else:
        product_ids = [i.get("product_id") for i in items if i.get("product_id")]
        products_map = _fetch_products_batch(product_ids) if product_ids else {}
        context["products"] = products_map

        for idx, item in enumerate(items):
            name = item.get("name")
            if not name or not str(name).strip():
                errors.append({"field": f"items[{idx}].name", "msg": "Item name is required"})

            units = item.get("units", 1)
            err = _validate_amount(units, f"items[{idx}].units", min_val=0.01)
            if err:
                errors.append(err)

            price = item.get("price", 0)
            err = _validate_amount(price, f"items[{idx}].price", min_val=0)
            if err:
                errors.append(err)

            tax = item.get("tax")
            if tax is not None and int(tax) not in VALID_TAX_RATES:
                errors.append({"field": f"items[{idx}].tax", "msg": f"Tax must be one of {VALID_TAX_RATES}"})

            pid = item.get("product_id")
            if pid and pid not in products_map:
                errors.append({"field": f"items[{idx}].product_id", "msg": f"Product '{pid}' not found"})

    # Date validation (optional)
    err = _validate_date(params.get("date"), "date")
    if err:
        errors.append(err)

    return (len(errors) == 0, errors, context)


def validate_create_contact(params):
    """Validate create_contact parameters."""
    errors = []
    context = {}

    name = _sanitize_text(params.get("name"))
    if not name:
        errors.append({"field": "name", "msg": "Contact name is required"})

    ctype = params.get("type")
    if ctype and ctype not in VALID_CONTACT_TYPES:
        errors.append({"field": "type", "msg": f"Type must be one of {VALID_CONTACT_TYPES}"})

    err = _validate_email(params.get("email"))
    if err:
        errors.append(err)

    tax_op = params.get("taxOperation")
    if tax_op and tax_op not in VALID_TAX_OPERATIONS:
        errors.append({"field": "taxOperation", "msg": f"Must be one of {VALID_TAX_OPERATIONS}"})

    # Duplicate check
    code = params.get("code", "")
    if name:
        conn = connector.get_db()
        try:
            cursor = connector._cursor(conn)
            cursor.execute(connector._q(
                'SELECT id, name FROM contacts WHERE name = ? AND code = ?'
            ), (name, code))
            existing = cursor.fetchone()
            if existing:
                errors.append({"field": "name", "msg": f"Contact '{name}' with code '{code}' already exists"})
        finally:
            connector.release_db(conn)

    return (len(errors) == 0, errors, context)


def validate_update_document_status(params):
    """Validate status transition for a document."""
    errors = []
    context = {}

    doc_type = params.get("doc_type", "invoice")
    doc_id = params.get("doc_id")

    id_err = _validate_holded_id(doc_id, "doc_id")
    if id_err:
        errors.append(id_err)
        return (False, errors, context)

    doc = _fetch_document(doc_type, doc_id)
    if not doc:
        errors.append({"field": "doc_id", "msg": f"Document {doc_type}/{doc_id} not found"})
        return (False, errors, context)

    context["document"] = doc
    old_status = doc.get("status", 0)
    new_status = params.get("status")

    if new_status is None:
        errors.append({"field": "status", "msg": "New status is required"})
        return (False, errors, context)

    try:
        new_status = int(new_status)
    except (ValueError, TypeError):
        errors.append({"field": "status", "msg": "Status must be an integer"})
        return (False, errors, context)

    transitions = ESTIMATE_TRANSITIONS if doc_type == "estimate" else INVOICE_TRANSITIONS
    allowed = transitions.get(old_status, set())

    if new_status not in allowed:
        status_labels = {0: "draft", 1: "issued/pending", 2: "partial/accepted",
                         3: "paid/rejected", 4: "overdue/invoiced", 5: "cancelled"}
        errors.append({
            "field": "status",
            "msg": f"Cannot transition from {status_labels.get(old_status, old_status)} to {status_labels.get(new_status, new_status)}"
        })

    return (len(errors) == 0, errors, context)


def validate_send_document(params):
    """Validate send_document parameters."""
    errors = []
    context = {}

    doc_type = params.get("doc_type")
    doc_id = params.get("doc_id")

    if doc_id:
        doc = _fetch_document(doc_type, doc_id)
        if not doc:
            errors.append({"field": "doc_id", "msg": f"Document {doc_type}/{doc_id} not found"})
        else:
            context["document"] = doc
            if doc.get("status") == 0:
                errors.append({"field": "status", "msg": "Cannot send a draft document"})

    emails = params.get("emails", [])
    if isinstance(emails, str):
        emails = [e.strip() for e in emails.split(",")]
    if not emails:
        errors.append({"field": "emails", "msg": "At least one email is required"})
    for email in emails:
        err = _validate_email(email)
        if err:
            errors.append(err)

    return (len(errors) == 0, errors, context)


# ── Validator Registry ───────────────────────────────────────────────

VALIDATORS = {
    "create_invoice": validate_create_invoice,
    "create_estimate": validate_create_estimate,
    "create_contact": validate_create_contact,
    "update_document_status": validate_update_document_status,
    "send_document": validate_send_document,
    # upload_file: no Holded validation needed
}


def validate(operation, params):
    """Run the validator for an operation. Returns (is_valid, errors, context)."""
    validator = VALIDATORS.get(operation)
    if not validator:
        return (True, [], {})  # No validator = pass through
    return validator(params)
```

- [ ] **Step 2: Commit**

```bash
git add write_validators.py
git commit -m "feat: add write_validators.py with input sanitization and validation rules"
```

---

### Task 7: Create `write_preview.py`

**Files:**
- Create: `write_preview.py`

- [ ] **Step 1: Create the preview module**

```python
"""
write_preview.py — Preview builder and warning generator for the Safe Write Gateway.

Builds rich preview objects with calculated totals and contextual warnings.
Receives pre-fetched data from validation context to avoid duplicate queries.
"""

import logging
import connector

logger = logging.getLogger(__name__)


# ── Line Item Calculations ───────────────────────────────────────────

def _calculate_items(items, products_map=None):
    """Calculate line-by-line totals for invoice/estimate items."""
    calculated = []
    for item in items:
        units = float(item.get("units", 1))
        price = float(item.get("price", 0))
        tax_pct = float(item.get("tax", 21))
        discount_pct = float(item.get("discount", 0))

        line_subtotal = units * price
        line_discount = line_subtotal * (discount_pct / 100)
        taxable = line_subtotal - line_discount
        line_tax = taxable * (tax_pct / 100)
        line_total = taxable + line_tax

        product_id = item.get("product_id")
        product_data = products_map.get(product_id) if products_map and product_id else None

        calculated.append({
            "name": item.get("name", ""),
            "product_id": product_id,
            "units": units,
            "price": price,
            "tax_pct": tax_pct,
            "discount_pct": discount_pct,
            "line_subtotal": round(line_subtotal, 2),
            "line_discount": round(line_discount, 2),
            "line_tax": round(line_tax, 2),
            "line_total": round(line_total, 2),
            "stock": product_data.get("stock") if product_data else None,
            "kind": product_data.get("kind") if product_data else None,
        })

    return calculated


# ── Warning Generator ────────────────────────────────────────────────

def _get_contact_warnings(contact, doc_type="invoice"):
    """Generate warnings related to a contact."""
    warnings = []
    if not contact:
        return warnings

    contact_id = contact.get("id")
    contact_type = contact.get("type", "")

    # Check unpaid/overdue invoices in a single batch query
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('''
            SELECT
                COUNT(*) FILTER (WHERE status IN (1,2)) as unpaid_count,
                COALESCE(SUM(CASE WHEN status IN (1,2) THEN payments_pending ELSE 0 END), 0) as unpaid_total,
                COUNT(*) FILTER (WHERE status = 4) as overdue_count,
                COALESCE(SUM(CASE WHEN status = 4 THEN payments_pending ELSE 0 END), 0) as overdue_total,
                COUNT(*) as total_invoices
            FROM invoices WHERE contact_id = ?
        '''), (contact_id,))
        row = cursor.fetchone()
        if row:
            r = dict(row) if hasattr(row, 'keys') else {}
            unpaid = r.get('unpaid_count', 0)
            unpaid_total = r.get('unpaid_total', 0)
            overdue = r.get('overdue_count', 0)
            overdue_total = r.get('overdue_total', 0)
            total = r.get('total_invoices', 0)

            if overdue and overdue > 0:
                warnings.append({
                    "level": "critical",
                    "code": "OVERDUE_INVOICES",
                    "msg": f"Contact has {overdue} overdue invoice(s) totaling EUR {float(overdue_total):,.2f}"
                })
            if unpaid and unpaid > 0:
                warnings.append({
                    "level": "critical",
                    "code": "UNPAID_INVOICES",
                    "msg": f"Contact has {unpaid} unpaid invoice(s) totaling EUR {float(unpaid_total):,.2f}"
                })
            if total == 0:
                warnings.append({
                    "level": "info",
                    "code": "FIRST_INVOICE",
                    "msg": "First invoice for this contact"
                })
    except Exception as e:
        logger.warning(f"Warning query failed for contact {contact_id}: {e}")
    finally:
        connector.release_db(conn)

    # Contact type mismatch
    if doc_type in ("invoice", "estimate") and contact_type == "supplier":
        warnings.append({
            "level": "info",
            "code": "CONTACT_IS_SUPPLIER",
            "msg": "Creating sales document for a supplier-type contact"
        })

    return warnings


def _get_item_warnings(calculated_items, high_amount_threshold=5000):
    """Generate warnings related to line items and totals."""
    warnings = []
    grand_total = sum(i["line_total"] for i in calculated_items)

    for item in calculated_items:
        stock = item.get("stock")
        if stock is not None:
            if stock == 0:
                warnings.append({
                    "level": "warning",
                    "code": "ZERO_STOCK",
                    "msg": f"Product '{item['name']}' has zero stock"
                })
            elif stock < item["units"]:
                warnings.append({
                    "level": "warning",
                    "code": "LOW_STOCK",
                    "msg": f"Product '{item['name']}' stock: {stock} (requested: {item['units']})"
                })

        if item.get("kind") == "pack":
            warnings.append({
                "level": "info",
                "code": "PRODUCT_IS_PACK",
                "msg": f"'{item['name']}' is a pack product"
            })

    if grand_total > high_amount_threshold:
        warnings.append({
            "level": "warning",
            "code": "HIGH_AMOUNT",
            "msg": f"Total EUR {grand_total:,.2f} exceeds threshold of EUR {high_amount_threshold:,.2f}"
        })

    return warnings


def _check_duplicate_recent(contact_id, grand_total, window_hours=24):
    """Check for similar documents created recently."""
    import time
    cutoff = int(time.time()) - (window_hours * 3600)
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('''
            SELECT doc_number, amount, date FROM invoices
            WHERE contact_id = ? AND date >= ? AND ABS(amount - ?) < ?
            ORDER BY date DESC LIMIT 3
        '''), (contact_id, cutoff, grand_total, grand_total * 0.1))
        rows = cursor.fetchall()
        if rows:
            return {
                "level": "warning",
                "code": "DUPLICATE_RECENT",
                "msg": f"Found {len(rows)} similar invoice(s) in last {window_hours}h for this contact"
            }
        return None
    except Exception:
        return None
    finally:
        connector.release_db(conn)


# ── Reversibility Assessment ─────────────────────────────────────────

REVERSIBILITY = {
    "create_invoice": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/documents/invoice/{id}",
        "conditions": "While status is draft (0)",
    },
    "create_estimate": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/documents/estimate/{id}",
        "conditions": "While status is draft (0)",
    },
    "create_contact": {
        "can_reverse": True,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/contacts/{id}",
        "conditions": "No linked invoices",
    },
    "send_document": {
        "can_reverse": False,
        "conditions": "Email cannot be unsent",
    },
    "update_document_status": {
        "can_reverse": False,
        "conditions": "Status transitions are generally one-way",
    },
    "upload_file": {
        "can_reverse": True,
        "conditions": "File can be deleted from filesystem",
    },
}


# ── Public API ───────────────────────────────────────────────────────

def build_preview(operation, params, context=None):
    """Build a rich preview object for a write operation.

    Args:
        operation: e.g. 'create_invoice'
        params: operation parameters
        context: pre-fetched data from validation stage

    Returns:
        dict with 'preview', 'warnings', 'reversibility' keys
    """
    context = context or {}
    preview = {"operation": operation}
    warnings = []

    if operation in ("create_invoice", "create_estimate"):
        contact = context.get("contact", {})
        products_map = context.get("products", {})
        items = params.get("items", [])
        calculated = _calculate_items(items, products_map)

        subtotal = sum(i["line_subtotal"] for i in calculated)
        total_discount = sum(i["line_discount"] for i in calculated)
        total_tax = sum(i["line_tax"] for i in calculated)
        grand_total = sum(i["line_total"] for i in calculated)

        preview["contact"] = {
            "id": contact.get("id", params.get("contact_id")),
            "name": contact.get("name", "Unknown"),
            "code": contact.get("code", ""),
            "type": contact.get("type", ""),
        }
        preview["items"] = calculated
        preview["subtotal"] = round(subtotal, 2)
        preview["total_discount"] = round(total_discount, 2)
        preview["total_tax"] = round(total_tax, 2)
        preview["grand_total"] = round(grand_total, 2)
        preview["currency"] = "EUR"

        warnings.extend(_get_contact_warnings(contact, operation.split("_")[1]))
        warnings.extend(_get_item_warnings(calculated))

        dup = _check_duplicate_recent(contact.get("id"), grand_total)
        if dup:
            warnings.append(dup)

        if not params.get("due_date") and operation == "create_invoice":
            warnings.append({"level": "info", "code": "NO_DUE_DATE", "msg": "Invoice created without due date"})

    elif operation == "create_contact":
        preview["contact_name"] = params.get("name", "")
        preview["contact_type"] = params.get("type", "client")
        preview["email"] = params.get("email", "")

    elif operation == "update_document_status":
        doc = context.get("document", {})
        status_labels = {0: "draft", 1: "issued", 2: "partial", 3: "paid", 4: "overdue", 5: "cancelled"}
        preview["document"] = {
            "id": params.get("doc_id"),
            "doc_number": doc.get("doc_number", ""),
            "current_status": status_labels.get(doc.get("status"), "unknown"),
            "new_status": status_labels.get(params.get("status"), "unknown"),
        }

    elif operation == "send_document":
        doc = context.get("document", {})
        preview["document"] = {
            "id": params.get("doc_id"),
            "doc_number": doc.get("doc_number", ""),
            "type": params.get("doc_type"),
        }
        preview["recipients"] = params.get("emails", [])

    reversibility = REVERSIBILITY.get(operation, {"can_reverse": False, "conditions": "Unknown"})

    return {
        "operation": operation,
        "preview": preview,
        "warnings": warnings,
        "reversibility": reversibility,
    }
```

- [ ] **Step 2: Commit**

```bash
git add write_preview.py
git commit -m "feat: add write_preview.py with warning generator and line item calculations"
```

---

### Task 8: Create `write_gateway.py`

**Files:**
- Create: `write_gateway.py`

- [ ] **Step 1: Create the gateway module**

```python
"""
write_gateway.py — Safe Write Gateway for Holded Connector.

All write operations (AI agent, REST API, CLI scripts) route through this
gateway. Provides a 6-stage pipeline: validate → preview → confirm/log →
execute → sync-back → audit.

Usage:
    from write_gateway import gateway
    result = gateway.execute("create_invoice", params, source="ai_agent")
"""

import hashlib
import json
import logging
import threading
import time

import connector
import write_validators
import write_preview

logger = logging.getLogger(__name__)


# ── Operation Registry ───────────────────────────────────────────────

OPERATIONS = {
    "create_invoice": {
        "method": "POST",
        "endpoint": "/invoicing/v1/documents/invoice",
        "entity_type": "invoice",
        "sync_type": "document",
        "sync_doc_type": "invoice",
    },
    "create_estimate": {
        "method": "POST",
        "endpoint": "/invoicing/v1/documents/estimate",
        "entity_type": "estimate",
        "sync_type": "document",
        "sync_doc_type": "estimate",
    },
    "create_contact": {
        "method": "POST",
        "endpoint": "/invoicing/v1/contacts",
        "entity_type": "contact",
        "sync_type": "contact",
    },
    "update_document_status": {
        "method": "PUT",
        "endpoint": "/invoicing/v1/documents/{doc_type}/{doc_id}",
        "entity_type": "document",
        "sync_type": "document",
    },
    "send_document": {
        "method": "POST",
        "endpoint": "/invoicing/v1/documents/{doc_type}/{doc_id}/send",
        "entity_type": "document",
        "sync_type": None,  # No sync needed
    },
    "upload_file": {
        "method": None,  # Local only
        "entity_type": "file",
        "sync_type": None,
    },
}


# ── Rate Limiting ────────────────────────────────────────────────────

class RateLimiter:
    """In-memory sliding window rate limiter."""

    def __init__(self):
        self._windows = {}  # key: (scope, window_seconds) → list of timestamps
        self._lock = threading.Lock()

    def check(self, scope, limit, window_seconds):
        """Returns True if allowed, False if rate limited."""
        now = time.time()
        key = (scope, window_seconds)
        with self._lock:
            if key not in self._windows:
                self._windows[key] = []
            # Clean old entries
            self._windows[key] = [t for t in self._windows[key] if t > now - window_seconds]
            if len(self._windows[key]) >= limit:
                return False
            self._windows[key].append(now)
            return True


_rate_limiter = RateLimiter()

# Daily budget counter
_daily_budget = {"date": "", "count": 0, "lock": threading.Lock()}


def _check_daily_budget(max_budget=50):
    """Check and increment daily write budget for AI agent. Returns True if allowed."""
    today = time.strftime("%Y-%m-%d")
    with _daily_budget["lock"]:
        if _daily_budget["date"] != today:
            _daily_budget["date"] = today
            _daily_budget["count"] = 0
        if _daily_budget["count"] >= max_budget:
            return False
        _daily_budget["count"] += 1
        return True


# ── Audit Checksum ───────────────────────────────────────────────────

def _compute_checksum(audit_id, timestamp, operation, entity_id, payload):
    """SHA-256 checksum for audit log tamper detection."""
    data = f"{audit_id}|{timestamp}|{operation}|{entity_id}|{json.dumps(payload, sort_keys=True)}"
    return hashlib.sha256(data.encode()).hexdigest()


# ── Payload Builders ─────────────────────────────────────────────────

def _build_holded_payload(operation, params):
    """Convert gateway params to Holded API payload format."""
    if operation in ("create_invoice", "create_estimate"):
        items = params.get("items", [])
        products = []
        for item in items:
            p = {"name": item.get("name"), "units": item.get("units", 1),
                 "subtotal": item.get("price")}
            if "tax" in item:
                p["tax"] = item["tax"]
            if "desc" in item:
                p["desc"] = item["desc"]
            products.append(p)

        payload = {"contact": params.get("contact_id"), "products": products}
        if params.get("desc"):
            payload["desc"] = params["desc"]
        if params.get("date"):
            payload["date"] = params["date"]
        if params.get("notes"):
            payload["notes"] = params["notes"]
        return payload

    elif operation == "create_contact":
        payload = {"name": params.get("name")}
        for field in ("email", "type", "phone", "mobile", "code"):
            if params.get(field):
                payload[field] = params[field]
        if params.get("vat"):
            payload["code"] = params["vat"]
        return payload

    elif operation == "update_document_status":
        return {"status": params.get("status")}

    elif operation == "send_document":
        payload = {"emails": params.get("emails")}
        if params.get("subject"):
            payload["subject"] = params["subject"]
        if params.get("message"):
            payload["message"] = params["message"]
        return payload

    return {}


def _resolve_endpoint(operation, params):
    """Resolve endpoint template with params."""
    op = OPERATIONS[operation]
    endpoint = op.get("endpoint", "")
    if "{doc_type}" in endpoint:
        endpoint = endpoint.replace("{doc_type}", params.get("doc_type", "invoice"))
    if "{doc_id}" in endpoint:
        endpoint = endpoint.replace("{doc_id}", params.get("doc_id", ""))
    return endpoint


# ── Sync-Back (Async) ────────────────────────────────────────────────

def _sync_back_async(operation, entity_id, params, audit_id):
    """Run sync-back in a background thread."""
    def _do_sync():
        try:
            op = OPERATIONS.get(operation, {})
            sync_type = op.get("sync_type")
            if not sync_type:
                return

            tables = []
            if sync_type == "document":
                doc_type = op.get("sync_doc_type") or params.get("doc_type", "invoice")
                tables = connector.sync_single_document(doc_type, entity_id)
            elif sync_type == "contact":
                tables = connector.sync_single_contact(entity_id)
            elif sync_type == "product":
                tables = connector.sync_single_product(entity_id)

            if tables and audit_id:
                connector.update_audit_log(audit_id, tables_synced=tables)
                logger.info(f"[GATEWAY] Sync-back complete for {operation}/{entity_id}: {tables}")
        except Exception as e:
            logger.error(f"[GATEWAY] Sync-back failed for {operation}/{entity_id}: {e}")
            if audit_id:
                connector.update_audit_log(audit_id, error_detail=f"Sync-back failed: {e}")

    thread = threading.Thread(target=_do_sync, daemon=True)
    thread.start()


# ── Gateway Class ────────────────────────────────────────────────────

class WriteGateway:
    """Centralized write gateway with 6-stage safety pipeline."""

    def execute(self, operation, params, source="ai_agent",
                conversation_id=None, skip_confirm=False):
        """Execute a write operation through the safety pipeline.

        Args:
            operation: Operation name (e.g., 'create_invoice')
            params: Operation parameters
            source: 'ai_agent' | 'rest_api' | 'cli_script'
            conversation_id: Chat conversation ID (for audit trail)
            skip_confirm: If True, skip confirmation (used after user confirms)

        Returns:
            dict with result data. For AI source without skip_confirm:
            returns preview for confirmation flow.
        """
        start_time = time.time()

        if operation not in OPERATIONS and operation not in (
            "create_amortization", "update_amortization", "delete_amortization",
            "toggle_web_include", "link_amortization_purchase",
        ):
            return {"success": False, "errors": [{"field": "operation", "msg": f"Unknown operation: {operation}"}]}

        # ── Rate Limiting ────────────────────────────────────
        if source == "ai_agent":
            if not _rate_limiter.check(f"ai_{source}", 5, 60):
                return {"success": False, "errors": [{"field": "rate_limit", "msg": "Rate limit exceeded: max 5 AI writes per minute"}]}
            if not _check_daily_budget():
                return {"success": False, "errors": [{"field": "daily_budget", "msg": "Daily write budget exhausted (max 50 AI writes per day)"}]}

        # ── Stage 1: Validate ────────────────────────────────
        is_valid, errors, context = write_validators.validate(operation, params)
        if not is_valid:
            return {"success": False, "errors": errors}

        # ── Stage 2: Preview + Warnings ──────────────────────
        preview_result = write_preview.build_preview(operation, params, context)

        # ── Stage 3: Confirm or Log ──────────────────────────
        if source == "ai_agent" and not skip_confirm:
            # Return preview for confirmation flow — execution happens later
            return {
                "needs_confirmation": True,
                "preview": preview_result["preview"],
                "warnings": preview_result["warnings"],
                "reversibility": preview_result["reversibility"],
            }

        # ── Insert audit log (pending) ───────────────────────
        holded_payload = _build_holded_payload(operation, params) if operation in OPERATIONS else None
        audit_id = connector.insert_audit_log(
            source=source,
            operation=operation,
            entity_type=OPERATIONS.get(operation, {}).get("entity_type", "unknown"),
            payload_sent=holded_payload,
            preview_data=preview_result,
            warnings=preview_result.get("warnings"),
            status="pending",
            safe_mode=connector.SAFE_MODE,
            conversation_id=conversation_id,
        )

        # ── Stage 4: Execute on Holded API ───────────────────
        if operation not in OPERATIONS or OPERATIONS[operation].get("method") is None:
            # Local-only operation — mark success immediately
            duration = int((time.time() - start_time) * 1000)
            connector.update_audit_log(audit_id, status="success", duration_ms=duration)
            return {"success": True, "audit_id": audit_id, "local_only": True}

        # Re-validate before execution (TOCTOU protection)
        is_valid2, errors2, _ = write_validators.validate(operation, params)
        if not is_valid2:
            connector.update_audit_log(audit_id, status="failed",
                                        error_detail=f"Re-validation failed: {errors2}")
            return {"success": False, "errors": errors2, "audit_id": audit_id}

        endpoint = _resolve_endpoint(operation, params)
        method = OPERATIONS[operation]["method"]

        if method == "POST":
            result = connector.post_data(endpoint, holded_payload)
        elif method == "PUT":
            result = connector.put_data(endpoint, holded_payload)
        elif method == "DELETE":
            result = connector.delete_data(endpoint)
        else:
            result = None

        # ── Handle result ────────────────────────────────────
        duration = int((time.time() - start_time) * 1000)

        if not result:
            connector.update_audit_log(audit_id, status="failed",
                                        error_detail="No response from Holded API",
                                        duration_ms=duration)
            return {"success": False, "error": "No response from Holded API", "audit_id": audit_id}

        if result.get("error"):
            status = "timeout" if "timed out" in str(result.get("detail", "")) else "failed"
            connector.update_audit_log(audit_id, status=status,
                                        error_detail=result.get("detail"),
                                        response_received=result,
                                        duration_ms=duration)
            return {"success": False, "error": result.get("detail"), "audit_id": audit_id}

        # Success
        entity_id = result.get("id", "")
        is_dry_run = result.get("dry_run", False)

        # Build reverse action
        rev = preview_result.get("reversibility", {})
        reverse_action = None
        if rev.get("can_reverse") and entity_id:
            reverse_action = {
                "method": rev.get("method"),
                "endpoint": rev.get("endpoint", "").replace("{id}", entity_id),
            }

        # Compute checksum
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        checksum = _compute_checksum(audit_id, ts, operation, entity_id, holded_payload)

        connector.update_audit_log(
            audit_id,
            status="dry_run" if is_dry_run else "success",
            entity_id=entity_id,
            response_received=result,
            reverse_action=reverse_action,
            checksum=checksum,
            duration_ms=duration,
        )

        # ── Stage 5: Sync-back (async) ───────────────────────
        if not is_dry_run and entity_id:
            _sync_back_async(operation, entity_id, params, audit_id)

        # ── Stage 6: Audit already updated above ─────────────

        # Log pipeline timing
        logger.info(
            f"[GATEWAY] {operation} | duration:{duration}ms | "
            f"status:{'dry_run' if is_dry_run else 'success'} | "
            f"entity:{entity_id}"
        )

        return {
            "success": True,
            "entity_id": entity_id,
            "entity_type": OPERATIONS[operation]["entity_type"],
            "doc_number": result.get("invoiceNum", ""),
            "audit_id": audit_id,
            "safe_mode": is_dry_run,
        }


# ── Singleton ────────────────────────────────────────────────────────

gateway = WriteGateway()
```

- [ ] **Step 2: Commit**

```bash
git add write_gateway.py
git commit -m "feat: add WriteGateway with 6-stage pipeline, rate limiting, and audit"
```

---

## Chunk 3: Integration — AI Agent + REST API

### Task 9: Integrate gateway into AI agent write tools

**Files:**
- Modify: `ai_agent.py`

- [ ] **Step 1: Add gateway import**

At top of `ai_agent.py`, add:
```python
from write_gateway import gateway
```

- [ ] **Step 2: Replace `exec_create_estimate()` with gateway call**

Replace the function body at lines ~606-622:
```python
def exec_create_estimate(params):
    """Create estimate via WriteGateway."""
    result = gateway.execute("create_estimate", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
            "safe_mode": result.get("safe_mode", False),
            "message": "Estimate created (dry run)" if result.get("safe_mode") else "Estimate created successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}
```

- [ ] **Step 3: Replace `exec_create_invoice()` with gateway call**

Same pattern at lines ~625-641:
```python
def exec_create_invoice(params):
    """Create invoice via WriteGateway."""
    result = gateway.execute("create_invoice", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
            "safe_mode": result.get("safe_mode", False),
            "message": "Invoice created (dry run)" if result.get("safe_mode") else "Invoice created successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}
```

- [ ] **Step 4: Replace `exec_create_contact()` with gateway call**

At lines ~679-690:
```python
def exec_create_contact(params):
    """Create contact via WriteGateway."""
    result = gateway.execute("create_contact", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "id": result.get("entity_id", ""),
            "safe_mode": result.get("safe_mode", False),
            "message": "Contact created (dry run)" if result.get("safe_mode") else "Contact created successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}
```

- [ ] **Step 5: Replace `exec_update_invoice_status()` with gateway call**

At lines ~803-822:
```python
def exec_update_invoice_status(params):
    """Update document status via WriteGateway."""
    result = gateway.execute("update_document_status", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "safe_mode": result.get("safe_mode", False),
            "message": "Status updated (dry run)" if result.get("safe_mode") else "Status updated successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}
```

- [ ] **Step 6: Replace `exec_send_document()` with gateway call**

At lines ~644-667:
```python
def exec_send_document(params):
    """Send document via WriteGateway."""
    result = gateway.execute("send_document", params, source="ai_agent", skip_confirm=True)
    if result.get("success"):
        return {
            "success": True,
            "safe_mode": result.get("safe_mode", False),
            "message": "Document sent (dry run)" if result.get("safe_mode") else "Document sent successfully",
        }
    return {"success": False, "error": result.get("error") or result.get("errors", "Unknown error")}
```

- [ ] **Step 7: Enhance confirmation flow with gateway preview**

In the section where `state_id` is generated (lines ~1266-1282), replace the `_describe_write_action()` call with gateway validation + preview:

```python
if tool_name in WRITE_TOOLS:
    # Run gateway validation + preview (stages 1-2)
    gateway_result = gateway.execute(
        operation=tool_name.replace("update_invoice_status", "update_document_status"),
        params=tool_input,
        source="ai_agent",
        conversation_id=conversation_id,
    )

    # If validation failed, return error to Claude (no confirmation needed)
    if gateway_result.get("success") is False:
        tool_result_content = json.dumps(gateway_result, default=str)
        # Continue agent loop with the validation error
        # (existing code handles tool_result messages)
        ...  # append tool_result and continue

    # If needs confirmation, store preview
    if gateway_result.get("needs_confirmation"):
        state_id = str(uuid.uuid4())
        _cleanup_pending()

        desc = _describe_write_action(tool_name, tool_input)  # Keep as fallback text

        with _pending_lock:
            pending_actions[state_id] = {
                "messages": messages + [{"role": "assistant", "content": [b.model_dump() for b in response.content]}],
                "tool_block": block.model_dump(),
                "conversation_id": conversation_id,
                "expires_at": time.time() + 300,
                "model": model,
                "system_prompt": system_prompt,
                "user_message": user_message,
                "gateway_preview": gateway_result,  # NEW: store preview
            }

        return {
            "type": "confirmation_needed",
            "action": {
                "tool": tool_name,
                "description": desc,
                "details": tool_input,
                "preview": gateway_result.get("preview"),      # NEW
                "warnings": gateway_result.get("warnings"),    # NEW
                "reversibility": gateway_result.get("reversibility"),  # NEW
            },
            "pending_state_id": state_id,
            "conversation_id": conversation_id,
        }
```

- [ ] **Step 8: Commit**

```bash
git add ai_agent.py
git commit -m "feat: integrate WriteGateway into all AI agent write tools"
```

---

### Task 10: Add audit log REST endpoints

**Files:**
- Modify: `api.py`

- [ ] **Step 1: Add audit log list endpoint**

Add after the existing amortization endpoints:
```python
@app.get("/api/audit-log")
def list_audit_log(limit: int = 50, offset: int = 0, operation: str = None,
                   entity_type: str = None, status: str = None):
    """List recent write audit log entries."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        where = []
        vals = []
        if operation:
            where.append(connector._q("operation = ?"))
            vals.append(operation)
        if entity_type:
            where.append(connector._q("entity_type = ?"))
            vals.append(entity_type)
        if status:
            where.append(connector._q("status = ?"))
            vals.append(status)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        vals.extend([limit, offset])

        cursor.execute(connector._q(f'''
            SELECT id, timestamp, source, operation, entity_type, entity_id,
                   status, safe_mode, duration_ms, error_detail
            FROM write_audit_log {where_sql}
            ORDER BY id DESC LIMIT ? OFFSET ?
        '''), tuple(vals))
        rows = cursor.fetchall()
        return [dict(r) for r in rows] if rows else []
    finally:
        connector.release_db(conn)


@app.get("/api/audit-log/{audit_id}")
def get_audit_log_detail(audit_id: int):
    """Get full audit log entry with preview, warnings, and payload."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q(
            'SELECT * FROM write_audit_log WHERE id = ?'
        ), (audit_id,))
        row = cursor.fetchone()
        if not row:
            return {"error": "Audit entry not found"}, 404
        entry = dict(row)
        # Parse JSON fields
        for field in ('payload_sent', 'response_received', 'preview_data',
                      'warnings', 'tables_synced', 'reverse_action', 'reverse_payload'):
            if entry.get(field) and isinstance(entry[field], str):
                try:
                    entry[field] = json.loads(entry[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        return entry
    finally:
        connector.release_db(conn)
```

- [ ] **Step 2: Wrap amortization endpoints with audit logging**

For the POST amortization endpoint (~line 1059), add audit logging after the existing logic:
```python
# At the end of the existing POST handler, before return:
from write_gateway import gateway
connector.insert_audit_log(
    source="rest_api",
    operation="create_amortization",
    entity_type="amortization",
    payload_sent=body.dict(),
    status="success",
    entity_id=str(result_id),
)
```

Apply the same pattern to PUT and DELETE amortization endpoints and the web_include PATCH endpoint.

- [ ] **Step 3: Add `import json` if not already present at top of api.py**

- [ ] **Step 4: Commit**

```bash
git add api.py
git commit -m "feat: add audit log endpoints and REST audit logging"
```

---

### Task 11: Verify full pipeline end-to-end

- [ ] **Step 1: Restart the server**

```bash
cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector"
lsof -ti:8000 | xargs kill -9 2>/dev/null
nohup /usr/bin/python3 api.py > server.log 2>&1 &
sleep 2
tail -5 server.log
```

- [ ] **Step 2: Verify audit table was created**

```bash
/usr/bin/python3 -c "
import connector
conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute('SELECT count(*) as c FROM write_audit_log')
print('Audit entries:', connector._fetch_one_val(cur, 'c'))
connector.release_db(conn)
"
```

- [ ] **Step 3: Test validation — create invoice with invalid contact**

```bash
/usr/bin/python3 -c "
from write_gateway import gateway
result = gateway.execute('create_invoice', {
    'contact_id': 'invalid_id_here',
    'items': [{'name': 'Test', 'price': 100, 'units': 1}]
}, source='rest_api')
print('Result:', result)
assert result['success'] == False
assert any('contact_id' in e.get('field','') for e in result.get('errors', []))
print('PASS: Invalid contact rejected')
"
```

- [ ] **Step 4: Test preview — create invoice with valid contact (SAFE_MODE)**

```bash
/usr/bin/python3 -c "
from write_gateway import gateway
import connector
# Get a real contact ID
conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute('SELECT id FROM contacts LIMIT 1')
row = cur.fetchone()
contact_id = row[0] if row else None
connector.release_db(conn)
if not contact_id:
    print('SKIP: No contacts in DB')
else:
    result = gateway.execute('create_invoice', {
        'contact_id': contact_id,
        'items': [{'name': 'Test Camera Rental', 'price': 150, 'units': 2, 'tax': 21}]
    }, source='ai_agent')
    print('Result:', result)
    assert result.get('needs_confirmation') == True
    assert 'preview' in result
    assert 'warnings' in result
    print('PASS: Preview generated for confirmation')
"
```

- [ ] **Step 5: Test audit log endpoint**

```bash
curl -s http://localhost:8000/api/audit-log | python3 -m json.tool | head -20
```

- [ ] **Step 6: Commit all working state**

```bash
git add -A
git commit -m "feat: Safe Write Gateway — full pipeline working (Phase 1 complete)"
```

- [ ] **Step 7: Push to GitHub**

```bash
PATH="$HOME/bin:$PATH" git push
```

---

## Chunk 4: Testing

### Task 12: Create test directory and test files

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_write_validators.py`
- Create: `tests/test_write_preview.py`
- Create: `tests/test_write_gateway.py`

- [ ] **Step 1: Create test directory**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 2: Create `tests/test_write_validators.py`**

```python
"""Tests for write_validators.py — input sanitization and validation rules."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import write_validators as wv


# ── Input Sanitization Tests ─────────────────────────────────────────

def test_sanitize_text_strips_html():
    assert wv._sanitize_text("<b>hello</b>") == "hello"
    assert wv._sanitize_text("<script>alert(1)</script>") == "alert(1)"

def test_sanitize_text_strips_whitespace():
    assert wv._sanitize_text("  hello  ") == "hello"

def test_sanitize_text_enforces_max_length():
    long = "a" * 600
    assert len(wv._sanitize_text(long)) == 500

def test_validate_holded_id_valid():
    assert wv._validate_holded_id("5ab391071d6d820034294783", "id") is None

def test_validate_holded_id_invalid():
    err = wv._validate_holded_id("not-a-valid-id", "id")
    assert err is not None
    assert "24-char hex" in err["msg"]

def test_validate_holded_id_empty():
    err = wv._validate_holded_id("", "id")
    assert err is not None

def test_validate_email_valid():
    assert wv._validate_email("test@example.com") is None

def test_validate_email_invalid():
    err = wv._validate_email("not-an-email")
    assert err is not None

def test_validate_email_optional():
    assert wv._validate_email(None) is None
    assert wv._validate_email("") is None

def test_validate_amount_valid():
    assert wv._validate_amount(100, "price") is None
    assert wv._validate_amount(0, "price") is None

def test_validate_amount_negative():
    err = wv._validate_amount(-1, "price")
    assert err is not None

def test_validate_amount_too_high():
    err = wv._validate_amount(1000001, "price")
    assert err is not None

def test_validate_date_valid():
    assert wv._validate_date(1710000000, "date") is None

def test_validate_date_too_old():
    err = wv._validate_date(1000000000, "date")  # 2001
    assert err is not None

def test_validate_date_optional():
    assert wv._validate_date(None, "date") is None


# ── Status Transition Tests ──────────────────────────────────────────

def test_invoice_valid_transitions():
    assert 1 in wv.INVOICE_TRANSITIONS[0]  # draft → issued
    assert 3 in wv.INVOICE_TRANSITIONS[1]  # issued → paid
    assert 5 in wv.INVOICE_TRANSITIONS[2]  # partial → cancelled

def test_invoice_terminal_states():
    assert len(wv.INVOICE_TRANSITIONS[3]) == 0  # paid = terminal
    assert len(wv.INVOICE_TRANSITIONS[5]) == 0  # cancelled = terminal

def test_estimate_transitions():
    assert 1 in wv.ESTIMATE_TRANSITIONS[0]  # draft → pending
    assert 2 in wv.ESTIMATE_TRANSITIONS[1]  # pending → accepted
    assert 4 in wv.ESTIMATE_TRANSITIONS[2]  # accepted → invoiced
    assert len(wv.ESTIMATE_TRANSITIONS[3]) == 0  # rejected = terminal


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
```

- [ ] **Step 3: Create `tests/test_write_preview.py`**

```python
"""Tests for write_preview.py — preview builder and warnings."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import write_preview as wp


def test_calculate_items_basic():
    items = [{"name": "Camera", "price": 100, "units": 2, "tax": 21}]
    result = wp._calculate_items(items)
    assert len(result) == 1
    assert result[0]["line_subtotal"] == 200.00
    assert result[0]["line_tax"] == 42.00
    assert result[0]["line_total"] == 242.00

def test_calculate_items_with_discount():
    items = [{"name": "Camera", "price": 100, "units": 1, "tax": 21, "discount": 10}]
    result = wp._calculate_items(items)
    assert result[0]["line_subtotal"] == 100.00
    assert result[0]["line_discount"] == 10.00
    assert result[0]["line_tax"] == 18.90  # (100-10) * 0.21
    assert result[0]["line_total"] == 108.90  # 90 + 18.90

def test_calculate_items_zero_tax():
    items = [{"name": "Service", "price": 50, "units": 1, "tax": 0}]
    result = wp._calculate_items(items)
    assert result[0]["line_tax"] == 0
    assert result[0]["line_total"] == 50.00

def test_item_warnings_high_amount():
    items = [{"name": "Expensive", "line_total": 6000, "units": 1, "stock": None, "kind": None,
              "line_subtotal": 6000, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items, high_amount_threshold=5000)
    codes = [w["code"] for w in warnings]
    assert "HIGH_AMOUNT" in codes

def test_item_warnings_zero_stock():
    items = [{"name": "Camera", "line_total": 100, "units": 1, "stock": 0, "kind": "simple",
              "line_subtotal": 100, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items)
    codes = [w["code"] for w in warnings]
    assert "ZERO_STOCK" in codes

def test_item_warnings_low_stock():
    items = [{"name": "Camera", "line_total": 100, "units": 5, "stock": 2, "kind": "simple",
              "line_subtotal": 100, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items)
    codes = [w["code"] for w in warnings]
    assert "LOW_STOCK" in codes

def test_item_warnings_pack():
    items = [{"name": "Kit", "line_total": 100, "units": 1, "stock": 10, "kind": "pack",
              "line_subtotal": 100, "line_discount": 0, "line_tax": 0}]
    warnings = wp._get_item_warnings(items)
    codes = [w["code"] for w in warnings]
    assert "PRODUCT_IS_PACK" in codes

def test_reversibility_invoice():
    assert wp.REVERSIBILITY["create_invoice"]["can_reverse"] is True

def test_reversibility_send():
    assert wp.REVERSIBILITY["send_document"]["can_reverse"] is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
```

- [ ] **Step 4: Create `tests/test_write_gateway.py`**

```python
"""Tests for write_gateway.py — rate limiting and pipeline logic."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from write_gateway import RateLimiter, _compute_checksum


def test_rate_limiter_allows_within_limit():
    rl = RateLimiter()
    for i in range(5):
        assert rl.check("test", 5, 60) is True

def test_rate_limiter_blocks_over_limit():
    rl = RateLimiter()
    for i in range(5):
        rl.check("test_block", 5, 60)
    assert rl.check("test_block", 5, 60) is False

def test_rate_limiter_separate_scopes():
    rl = RateLimiter()
    for i in range(5):
        rl.check("scope_a", 5, 60)
    # Different scope should still allow
    assert rl.check("scope_b", 5, 60) is True

def test_checksum_deterministic():
    c1 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 1})
    c2 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 1})
    assert c1 == c2

def test_checksum_changes_with_data():
    c1 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 1})
    c2 = _compute_checksum(1, "2026-03-12", "create_invoice", "abc", {"test": 2})
    assert c1 != c2


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
```

- [ ] **Step 5: Run all tests**

```bash
cd "/Users/miguel/IA SHARED/COYOTE-IA-PROYECT/services/holded-connector"
/usr/bin/python3 -m pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "test: add unit tests for validators, preview, and gateway"
```

- [ ] **Step 7: Final push**

```bash
PATH="$HOME/bin:$PATH" git push
```

---

## Summary

| Chunk | Tasks | What it builds |
|-------|-------|---------------|
| **1: Prerequisites** | Tasks 1-5 | Refactored connector helpers, audit table, single-entity sync |
| **2: Core Gateway** | Tasks 6-8 | Validators, preview builder, WriteGateway class |
| **3: Integration** | Tasks 9-11 | AI agent + REST API integration, end-to-end verification |
| **4: Testing** | Task 12 | Unit tests for all components |

**Total: 12 tasks, ~50 steps**

After completing all 4 chunks, the system has:
- All AI writes validated before reaching Holded
- Rich previews with contact, items, totals, and warnings
- Full audit trail with SHA-256 tamper detection
- Rate limiting (5/min, 50/day for AI)
- Async sync-back keeping local DB in sync
- TOCTOU protection via re-validation at execution time
- Audit log REST endpoints for visibility
