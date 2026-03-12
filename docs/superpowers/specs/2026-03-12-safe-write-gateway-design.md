# Safe Write Gateway — Design Specification

> **Date:** 2026-03-12
> **Status:** Reviewed — Pending user approval
> **Scope:** holded-connector service
> **Goal:** 100% safe write operations for invoices, estimates, products, and contacts via a centralized gateway with validation, preview, confirmation, audit, and post-write sync.

---

## 1. Problem Statement

The holded-connector is Miguel's core invoicing system (Holded ERP). The AI agent can create invoices, estimates, contacts, and modify document status via Holded API. Current gaps:

1. **No local DB update after write** — Creating an invoice in Holded doesn't touch the local `invoices` table until the next full sync
2. **No pre-validation** — Can create an invoice for a non-existent contact_id or invalid product
3. **No audit trail** — No record of who created what, when, and why
4. **No transaction safety** — If Holded write succeeds but post-processing fails, no recovery
5. **No contextual warnings** — No visibility into related state (unpaid invoices, low stock, duplicates)
6. **REST endpoints untracked** — Amortization CRUD and web_include toggle have no audit trail

---

## 2. Design Principles

1. **Holded is the single source of truth** — Write to Holded first, then sync the full record back to local DB
2. **Never bypass the gateway** — All write operations (AI, REST, CLI) route through `WriteGateway`
3. **Tiered confirmation** — AI agent writes require user confirmation; REST writes are audit-logged only
4. **Full reversibility** — Every write stores enough info to generate an undo action
5. **Validate before execute** — Block invalid operations before they reach Holded
6. **Fail safe** — If any post-write step fails (sync, audit), the write still succeeds but errors are logged and surfaced
7. **Defense in depth** — Multiple independent safety layers (validation, confirmation, audit, rate limiting) so no single failure compromises safety
8. **Least privilege** — AI agent tools only expose the minimum operations needed; dangerous operations (bulk delete, schema changes) are never exposed

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────┐
│                    WriteGateway                      │
│                                                      │
│  ┌───────────┐  ┌────────────┐  ┌────────────────┐  │
│  │ 1.Validate│→ │ 2.Preview  │→ │ 3.Confirm/Log  │  │
│  │           │  │  + Warn    │  │  (tier-based)  │  │
│  └───────────┘  └────────────┘  └───────┬────────┘  │
│                                         │            │
│  ┌───────────┐  ┌────────────┐  ┌───────▼────────┐  │
│  │ 6.Audit   │← │ 5.Sync     │← │ 4.Execute on   │  │
│  │   Log     │  │   Back     │  │   Holded API   │  │
│  └───────────┘  └────────────┘  └────────────────┘  │
└─────────────────────────────────────────────────────┘

Entry points:
  - ai_agent.py   → gateway.execute(source="ai_agent")    → confirmation required
  - api.py        → gateway.execute(source="rest_api")     → audit-only
  - CLI scripts   → gateway.execute(source="cli_script")   → audit-only
```

---

## 4. Supported Operations

### Phase 1 — Existing AI Tools (day one)

These 6 operations have existing tool implementations in `ai_agent.py` and will be migrated to the gateway first:

| Operation | Entity Type | Holded Endpoint | Local Tables Affected | Status |
|-----------|-------------|-----------------|----------------------|--------|
| `create_invoice` | invoice | `POST /documents/invoice` | `invoices`, `invoice_items` | Exists |
| `create_estimate` | estimate | `POST /documents/estimate` | `estimates`, `estimate_items` | Exists |
| `create_contact` | contact | `POST /contacts` | `contacts` | Exists |
| `update_document_status` | invoice/estimate/purchase | `PUT /documents/{type}/{id}` (general update, status field only) | `invoices`, `estimates`, or `purchase_invoices` | Exists |
| `send_document` | invoice/estimate | `POST /documents/{type}/{id}/send` | (none — email only) | Exists |
| `upload_file` | file | (local only) | `ai_history`, filesystem | Exists |

### Phase 2 — New AI Tools (planned)

These operations will be implemented as new AI tools + gateway integration after Phase 1 is stable:

| Operation | Entity Type | Holded Endpoint | Local Tables Affected | Status |
|-----------|-------------|-----------------|----------------------|--------|
| `create_product` | product | `POST /products` | `products` | Planned |
| `create_purchase` | purchase | `POST /documents/purchase` | `purchase_invoices`, `purchase_items` | Planned |
| `update_contact` | contact | `PUT /contacts/{id}` | `contacts` | Planned |
| `update_product` | product | `PUT /products/{id}` | `products` | Planned |
| `delete_document` | invoice/estimate/purchase | `DELETE /documents/{type}/{id}` | doc table + items table | Planned |
| `delete_contact` | contact | `DELETE /contacts/{id}` | `contacts` | Planned |
| `delete_product` | product | `DELETE /products/{id}` | `products`, `pack_components` | Planned |
| `pay_document` | invoice | `POST /documents/{type}/{id}/pay` | `invoices`, `payments` | Planned |

### Local-only operations (no Holded API call, but still audited via REST tier)

| Operation | Entity Type | Local Tables Affected |
|-----------|-------------|----------------------|
| `create_amortization` | amortization | `amortizations` |
| `update_amortization` | amortization | `amortizations` |
| `delete_amortization` | amortization | `amortizations`, `amortization_purchases` |
| `toggle_web_include` | product | `products` |
| `link_amortization_purchase` | amortization_purchase | `amortization_purchases` |

---

## 5. Pipeline Stages

### Stage 1: Validate

Each operation has a validation function that checks preconditions against the local DB. Validation **blocks** the operation if any check fails.

#### Validation Rules by Operation

**create_invoice / create_estimate:**
- `contact_id` must exist in `contacts` table
- `items` array must have at least 1 item
- Each item: `units > 0`, `price >= 0`
- Each item with `product_id`: must exist in `products` table
- Tax percentage must be a valid rate (0, 4, 10, 21 for Spain)
- `date` must be a valid Unix timestamp (optional — Holded defaults to current date if omitted)

**create_contact:**
- `name` is required and non-empty
- `type` must be one of: `client`, `supplier`, `debtor`, `creditor`, `lead`
- `email` format validation (if provided)
- Duplicate check: no existing contact with same `name` + `code` combination
- `taxOperation` (if provided) must be: `general`, `intra`, `impexp`, `nosujeto`, `receq`, `exento`

**create_product:**
- `name` is required and non-empty
- `sku` uniqueness check (if provided)
- `price >= 0`
- `kind` must be `simple` or `pack`

**update_document_status:**
- Document must exist in local DB
- Status transition must be valid (see transition rules below)

**send_document:**
- Document must exist in local DB
- Document status must not be draft (0)
- At least one valid email address

**delete_document / delete_contact / delete_product:**
- Entity must exist in local DB
- Orphan check: can't delete contact with open (non-cancelled) invoices
- Can't delete product referenced in non-cancelled invoices

**update_contact:**
- Contact must exist in local DB
- `name` must remain non-empty (if provided)
- `email` format validation (if provided)
- `type` must be valid enum (if provided)
- Duplicate check: no other contact with same `name` + `code` (if changing either)

**update_product:**
- Product must exist in local DB
- `name` must remain non-empty (if provided)
- `sku` uniqueness check (if changing)
- `price >= 0` (if provided)
- `kind` cannot change from `pack` to `simple` if pack_components exist

**pay_document:**
- Document must exist
- `amount > 0`
- `amount <= payments_pending` (can't overpay)
- Document status must not be cancelled (5) or draft (0)

#### Status Transition Rules

```
Invoice / Purchase:
  draft(0)     → issued(1)
  issued(1)    → partial(2), paid(3), overdue(4), cancelled(5)
  partial(2)   → paid(3), overdue(4), cancelled(5)
  overdue(4)   → partial(2), paid(3), cancelled(5)
  paid(3)      → (terminal, no transitions)
  cancelled(5) → (terminal, no transitions)

Estimate (statuses 0-4 only, no cancelled state in Holded):
  draft(0)     → pending(1)
  pending(1)   → accepted(2), rejected(3)
  accepted(2)  → invoiced(4)
  rejected(3)  → (terminal)
  invoiced(4)  → (terminal)
```

### Stage 2: Preview + Warnings

After validation passes, build a rich preview object for the user/audit.

#### Preview Object Structure

```python
{
    "operation": "create_invoice",
    "preview": {
        "contact": {
            "id": "abc123",
            "name": "Camera Rent SL",
            "code": "B12345678",
            "type": "client"
        },
        "items": [
            {
                "name": "RED Komodo 6K",
                "product_id": "prod_001",
                "units": 3,
                "price": 150.00,
                "tax_pct": 21,
                "discount_pct": 0,
                "line_subtotal": 450.00,
                "line_tax": 94.50,
                "line_total": 544.50
            }
        ],
        "subtotal": 450.00,
        "total_tax": 94.50,
        "total_discount": 0.00,
        "grand_total": 544.50,
        "currency": "EUR",
        "date": "2026-03-12",
        "due_date": "2026-04-12"
    },
    "warnings": [
        {
            "level": "critical",
            "code": "UNPAID_INVOICES",
            "msg": "Contact has 3 unpaid invoices totaling EUR 4,200.00"
        },
        {
            "level": "warning",
            "code": "LOW_STOCK",
            "msg": "Product 'RED Komodo 6K' stock: 2 (requested: 3)"
        },
        {
            "level": "info",
            "code": "FIRST_INVOICE",
            "msg": "First invoice for this contact"
        }
    ],
    "reversibility": {
        "can_reverse": true,
        "method": "DELETE",
        "endpoint": "/invoicing/v1/documents/invoice/{id}",
        "conditions": "Can be deleted while status is draft (0)",
        "time_window": "Before document is issued or paid"
    }
}
```

#### Warning Rules

| Code | Level | Trigger |
|------|-------|---------|
| `UNPAID_INVOICES` | critical | Contact has >0 unpaid invoices |
| `OVERDUE_INVOICES` | critical | Contact has overdue invoices |
| `LOW_STOCK` | warning | Product stock < requested units |
| `ZERO_STOCK` | warning | Product stock = 0 |
| `DUPLICATE_RECENT` | warning | Similar document created in last 24h (same contact + similar total, checks both creation time and document date) |
| `HIGH_AMOUNT` | warning | Total exceeds EUR 5,000 (configurable threshold) |
| `FIRST_INVOICE` | info | First invoice for this contact |
| `CONTACT_IS_SUPPLIER` | info | Creating sales doc for a supplier-type contact |
| `PRODUCT_IS_PACK` | info | Line item references a pack product |
| `NO_DUE_DATE` | info | Invoice created without due date |

### Stage 3: Confirm or Log (Tier-Based)

| Source | Behavior |
|--------|----------|
| `ai_agent` | Return preview + warnings to the existing `pending_actions` confirmation flow. User must approve in the chat UI. If critical warnings exist, highlight them prominently. |
| `rest_api` | No confirmation pause. Write audit entry with preview data before execution. |
| `cli_script` | Print preview to terminal. If `--yes` flag passed, proceed. Otherwise prompt `y/n`. |

### Stage 4: Execute on Holded API

```python
def _execute(self, operation, params):
    if connector.SAFE_MODE:
        return {"status": 1, "id": "SAFE_MODE_DRY_RUN", "dry_run": True}

    endpoint, method, payload = self._build_request(operation, params)

    if method == "POST":
        return connector.post_data(endpoint, payload)
    elif method == "PUT":
        return connector.put_data(endpoint, payload)
    elif method == "DELETE":
        return connector.delete_data(endpoint)
```

**Prerequisites — connector.py changes required:**

1. **Fix `post_data()` / `put_data()` response handling:** Currently returns `None` on any non-200 response. Holded API returns HTTP 201 for successful creates (not 200). Must accept both 200 and 201 as success. Must return structured error objects (not None) so the gateway can distinguish 4xx vs 429 vs 5xx.

2. **Add `delete_data(endpoint)` helper:** Does not exist yet. Must follow the same pattern as `post_data()` / `put_data()` with SAFE_MODE intercept.

**Refactored error return from connector helpers:**
```python
# Instead of returning None on error:
return {"error": True, "status_code": response.status_code, "detail": response.text}
# Success:
return {"error": False, **response.json()}
```

**Error handling in gateway:**
- HTTP 4xx → Return validation error, do NOT retry
- HTTP 429 → Retry with exponential backoff (max 3 attempts)
- HTTP 5xx → Return server error, log for investigation
- Timeout → Return timeout error, do NOT assume failure (check Holded state on next sync)
- `None` response → Treat as network error, log for investigation

### Stage 5: Targeted Sync

After successful Holded write, immediately fetch the full record back:

```python
def _sync_back(self, operation, entity_id, entity_type):
    """Fetch the created/modified entity from Holded and upsert into local DB."""

    sync_map = {
        "invoice":  ("/invoicing/v1/documents/invoice/{id}", "invoices", "invoice_items", "invoice_id"),
        "estimate": ("/invoicing/v1/documents/estimate/{id}", "estimates", "estimate_items", "estimate_id"),
        "purchase": ("/invoicing/v1/documents/purchase/{id}", "purchase_invoices", "purchase_items", "purchase_id"),
        "contact":  ("/invoicing/v1/contacts/{id}", "contacts", None, None),
        "product":  ("/invoicing/v1/products/{id}", "products", None, None),
    }

    endpoint_tpl, main_table, items_table, fk_col = sync_map[entity_type]
    endpoint = endpoint_tpl.format(id=entity_id)

    # Fetch full record from Holded
    data = connector.fetch_data(endpoint)

    # Upsert into main table
    # NOTE: The upsert logic must be extracted from the existing bulk sync
    # functions (sync_documents, sync_contacts, sync_products) into reusable
    # single-entity helpers: _upsert_document(), _upsert_contact(), _upsert_product().
    # This extraction is non-trivial — the bulk sync functions contain inline
    # SQL with dialect handling, field mapping, and edge cases (e.g., _num(),
    # extract_ret(), account mapping). The extracted helpers must preserve all
    # of this logic exactly.
    _upsert_entity(data, main_table)

    # Upsert items if applicable (DELETE + INSERT pattern, same as bulk sync)
    if items_table and fk_col:
        _sync_items(data, entity_id, items_table, fk_col)

    return [main_table] + ([items_table] if items_table else [])
```

For delete operations: remove from local DB tables after Holded confirms deletion.

For status updates: re-fetch the document and update the local record.

#### Sync-Back Performance Optimizations

1. **Async sync-back:** The sync-back fetch runs after the Holded API returns success. It should NOT block the response to the user. Run it in a background thread and update the audit log when complete. The user sees the success immediately; the local DB updates within 1-2 seconds.

2. **Connection pooling for sync-back:** Single-entity fetches should reuse the existing DB connection from the gateway pipeline rather than opening a new one. Pass the connection through the pipeline context.

3. **Skip sync-back for send_document:** Sending an email doesn't change any data — no need to fetch the document back.

4. **Skip sync-back for delete operations:** After a successful delete, just DELETE from local DB. No need to fetch from Holded (the entity is gone).

5. **Conditional sync-back for status updates:** Only re-fetch if the status change affects calculated fields (e.g., `payments_pending`). For simple draft→issued transitions, update the status column directly without a Holded fetch.

### Stage 6: Audit Log

#### Table Schema

```sql
CREATE TABLE IF NOT EXISTS write_audit_log (
    id              {_serial},
    timestamp       TEXT DEFAULT {_now},
    source          TEXT NOT NULL,           -- 'ai_agent' | 'rest_api' | 'cli_script'
    operation       TEXT NOT NULL,           -- 'create_invoice', 'update_status', etc.
    entity_type     TEXT NOT NULL,           -- 'invoice', 'contact', 'product', etc.
    entity_id       TEXT,                    -- Holded ID (NULL if failed before creation)
    payload_sent    TEXT,                    -- Full JSON payload sent to Holded API
    response_received TEXT,                 -- Full JSON response from Holded API
    preview_data    TEXT,                    -- Full preview object (JSON)
    warnings        TEXT,                    -- Warnings array (JSON)
    status          TEXT NOT NULL,           -- 'pending' | 'success' | 'failed' | 'cancelled' | 'dry_run' | 'timeout'
    tables_synced   TEXT,                    -- JSON array: ["invoices", "invoice_items"]
    reverse_action  TEXT,                    -- JSON: {"method": "DELETE", "endpoint": "..."}
    reverse_payload TEXT,                    -- JSON payload needed to undo (if applicable)
    user_confirmed  BOOLEAN,                -- true/false/null (null = no confirmation needed)
    error_detail    TEXT,                    -- Error message if failed
    safe_mode       BOOLEAN DEFAULT FALSE,  -- Was this a dry-run?
    conversation_id TEXT                    -- AI chat conversation_id (NULL for REST/CLI)
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON write_audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON write_audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_operation ON write_audit_log(operation);
CREATE INDEX IF NOT EXISTS idx_audit_status ON write_audit_log(status);
```

#### Audit Entry Lifecycle

1. **Pre-execution:** Insert audit row with `status='pending'`, payload, preview, warnings
2. **Post-execution success:** Update to `status='success'`, add response + entity_id + tables_synced + reverse_action
3. **Post-execution failure:** Update to `status='failed'`, add error_detail
4. **User cancelled:** Update to `status='cancelled'`
5. **Dry run:** Insert with `status='dry_run'`, `safe_mode=true`

---

## 6. File Structure

```
holded-connector/
├── write_gateway.py          -- NEW: WriteGateway class, pipeline orchestration
├── write_validators.py       -- NEW: Validation rules per operation type
├── write_preview.py          -- NEW: Preview builder + warning generator
├── ai_agent.py               -- MODIFIED: Write tools delegate to gateway
├── api.py                    -- MODIFIED: REST write endpoints delegate to gateway
├── connector.py              -- MODIFIED: Add write_audit_log to init_db(),
│                                          add targeted sync helpers,
│                                          add delete_data() helper
```

### New Files

#### `write_gateway.py`
- `WriteGateway` class with `execute(operation, params, source)` method
- Pipeline orchestration: validate → preview → confirm/log → execute → sync → audit
- Operation registry mapping operation names to their config (endpoint, method, entity_type, tables)
- Error handling and recovery logic
- Safe mode integration
- **Pipeline context:** A `dict` passed through all stages to share DB lookups (contact data, product data) — avoids duplicate queries between validate and preview
- **Single DB connection per pipeline:** Opens one connection at start, passes through all stages, commits at end. No per-stage connection overhead.

#### `write_validators.py`
- One validation function per operation type
- `validate_create_invoice(params)` → returns `(is_valid, errors[])`
- `validate_create_contact(params)` → returns `(is_valid, errors[])`
- Status transition map and validator
- Shared helpers: `_contact_exists(id)`, `_product_exists(id)`, `_document_exists(type, id)`
- **Performance:** Validation and preview share DB lookups. Validation fetches the contact/product rows and passes them forward to the preview stage via a shared `context` dict — avoiding duplicate queries.

#### `write_preview.py`
- `build_preview(operation, params, context)` → returns preview dict (receives pre-fetched data from validation)
- `generate_warnings(operation, params, context)` → returns warnings array
- Contact enrichment (resolve name, check unpaid invoices)
- Product enrichment (check stock, resolve pack info)
- Line item calculation (subtotals, taxes, grand total)
- Reversibility assessment per operation type
- **Performance:** Warning queries (unpaid invoices count, recent duplicates) run as a single batch query with `UNION ALL`, not N+1 individual queries. Product stock checks for all line items run as a single `WHERE id IN (...)` query.

### Modified Files

#### `connector.py`
- Add `write_audit_log` table to `init_db()`
- Add `delete_data(endpoint)` helper (mirrors `post_data` / `put_data` pattern)
- Add `sync_single_document(doc_type, doc_id)` — fetch one doc and upsert
- Add `sync_single_contact(contact_id)` — fetch one contact and upsert
- Add `sync_single_product(product_id)` — fetch one product and upsert
- Add `insert_audit_log(...)` and `update_audit_log(...)` helpers

#### `ai_agent.py`
- Replace direct `connector.post_data()` calls in write tools with `gateway.execute()`
- Enhance `_describe_write_action()` to use gateway's preview object
- Pass preview + warnings to the confirmation flow SSE events

#### `api.py`
- Wrap amortization CRUD endpoints with `gateway.execute(source="rest_api")`
- Wrap `web_include` toggle with `gateway.execute(source="rest_api")`
- Add `GET /api/audit-log` endpoint (list recent audit entries)
- Add `GET /api/audit-log/{id}` endpoint (full audit detail)
- Add `POST /api/audit-log/{id}/reverse` endpoint (execute reverse action with confirmation)

---

## 7. Gateway API

### Core Method

```python
class WriteGateway:
    def execute(self, operation: str, params: dict, source: str) -> dict:
        """
        Execute a write operation through the safety pipeline.

        Args:
            operation: Operation name (e.g., 'create_invoice')
            params: Operation parameters
            source: 'ai_agent' | 'rest_api' | 'cli_script'

        Returns:
            For AI source: preview + warnings dict (for confirmation flow)
            For REST/CLI source: execution result dict

        Raises:
            ValidationError: If validation fails
            HoldedAPIError: If Holded API call fails
        """
```

### Return Values

**AI agent (confirmation needed):**
```python
{
    "needs_confirmation": True,
    "preview": { ... },        # Full preview object
    "warnings": [ ... ],       # Warning array
    "state_id": "uuid",        # For pending_actions
    "reversibility": { ... }   # Undo info
}
```

**AI agent (after confirmation):**
```python
{
    "success": True,
    "entity_id": "holded_id",
    "entity_type": "invoice",
    "doc_number": "F260001",
    "tables_synced": ["invoices", "invoice_items"],
    "audit_id": 42,
    "safe_mode": False
}
```

**REST endpoint:**
```python
{
    "success": True,
    "entity_id": "holded_id",
    "audit_id": 42,
    "tables_synced": ["invoices", "invoice_items"]
}
```

**Validation failure:**
```python
{
    "success": False,
    "errors": [
        {"field": "contact_id", "msg": "Contact 'xyz' not found in database"},
        {"field": "items[0].units", "msg": "Units must be > 0"}
    ]
}
```

---

## 8. Integration with Existing Confirmation Flow

The current `pending_actions` system in `ai_agent.py` stays. The gateway enhances it:

### Current Flow (unchanged structure)
```
1. Claude calls write tool → tool is in WRITE_TOOLS set
2. Generate state_id, store in pending_actions (5-min TTL)
3. Return confirmation_needed SSE event to frontend
4. User clicks Confirm/Cancel in chat UI
5. POST /api/ai/confirm with state_id
6. Execute tool and continue agent loop
```

### Enhanced Flow (gateway integrated)
```
1. Claude calls write tool → tool is in WRITE_TOOLS set
2. Gateway.execute(source="ai_agent") runs Stage 1 (validate) + Stage 2 (preview)
   - If validation fails → return error to Claude, no confirmation needed
   - If validation passes → return preview + warnings
3. Generate state_id, store preview in pending_actions (5-min TTL)
4. Return confirmation_needed SSE event WITH preview + warnings
5. Frontend renders rich preview (contact info, line items, totals, warnings)
6. User clicks Confirm/Cancel
7. POST /api/ai/confirm with state_id
8. Gateway runs Stage 4 (execute) → Stage 5 (sync) → Stage 6 (audit)
9. Result returned to agent loop
   NOTE: confirm_action must preserve role-filtered tools (get_tools_for_role())
   when resuming the agent loop after confirmation
```

### Frontend Changes Required

The chat confirmation dialog needs to render:
- Contact name + code (not just ID)
- Line items table with calculated totals
- Warning badges (critical = red, warning = yellow, info = blue)
- Grand total prominently displayed
- "This action can be reversed" or "This action is irreversible" indicator

---

## 9. Reversibility Matrix

| Operation | Reversible? | Reverse Action | Conditions |
|-----------|------------|----------------|------------|
| `create_invoice` | Yes | `DELETE /documents/invoice/{id}` | While status = draft |
| `create_estimate` | Yes | `DELETE /documents/estimate/{id}` | While status = draft |
| `create_contact` | Yes | `DELETE /contacts/{id}` | No linked invoices |
| `create_product` | Yes | `DELETE /products/{id}` | No linked invoice items |
| `update_document_status` | Partial | `PUT /documents/{type}/{id}` with old status | Only some transitions reversible |
| `send_document` | No | — | Email cannot be unsent |
| `delete_document` | No | — | Holded deletion is permanent |
| `pay_document` | Partial | Delete payment via Holded | If payment ID known |
| `toggle_web_include` | Yes | Toggle back | Always |
| `create_amortization` | Yes | `DELETE amortizations WHERE id=X` | Always (local only) |

---

## 10. Error Recovery

### Scenario: Holded write succeeds, sync-back fails
- Audit log records `status='success'` with `sync_error` in `error_detail`
- Next full sync will pick up the record
- No data loss — Holded has the record

### Scenario: Holded write succeeds, audit log fails
- Operation completes normally
- Error logged to `server.log`
- Missing audit entry is acceptable (edge case)

### Scenario: Holded API timeout
- Do NOT assume the write failed — Holded may have processed it
- Audit log records `status='timeout'`
- Log warning for manual review
- Next sync will reveal if the entity was created

### Scenario: Validation passes but Holded rejects
- Possible if local DB is stale (contact deleted in Holded but not synced)
- Audit log records `status='failed'` with Holded error
- Recommend user run a full sync, then retry

---

## 11. Security

### 11.1 Input Sanitization

All parameters entering the gateway are sanitized before reaching Holded API or local DB:

| Field Type | Sanitization |
|------------|-------------|
| `contact_id`, `product_id`, entity IDs | Regex: `/^[a-f0-9]{24}$/` (Holded MongoDB ObjectId format). Reject anything else. |
| `name`, `desc`, text fields | Strip leading/trailing whitespace. Max length 500 chars. No HTML tags (strip with `bleach` or regex). |
| `email` | RFC 5322 validation via `email-validator` library. |
| `amount`, `price`, `units` | Must be numeric. Reject NaN, Infinity. Clamp to reasonable range (0–999,999.99). |
| `date` (Unix timestamp) | Must be integer. Reject dates before 2020 or more than 1 year in the future. |
| `status` | Integer, must be in allowed set per doc type. |
| `tax` | Must be in `{0, 4, 10, 21}` (Spanish tax rates). |
| `items` array | Max 100 items per document. Reject empty names. |

### 11.2 Rate Limiting (Write Operations)

Gateway-level rate limiting, independent of the existing 10 req/min API rate limit:

| Scope | Limit | Window | Action on exceed |
|-------|-------|--------|-----------------|
| AI agent writes | 5 writes | per minute | Block + warn in chat |
| AI agent writes | 20 writes | per hour | Block + warn in chat |
| REST API writes | 30 writes | per minute | HTTP 429 |
| Same entity writes | 3 writes | per 5 minutes | Block + warn "rapid modification detected" |
| Delete operations | 5 deletes | per hour | Block + require manual confirmation even for REST |

Implementation: In-memory counter dict keyed by `(source, operation)` with sliding window. No external dependencies.

### 11.3 AI Agent Guardrails

The AI agent has additional constraints beyond the gateway:

| Guard | Description |
|-------|-------------|
| **No bulk operations** | AI tools can only create/modify ONE entity per call. No batch endpoints exposed. |
| **No schema access** | AI cannot run DDL (CREATE/ALTER/DROP TABLE). `query_database` tool already blocks non-SELECT. |
| **Amount ceiling** | AI cannot create invoices exceeding EUR 10,000 without explicit user override in the confirmation dialog. |
| **Daily write budget** | Max 50 AI-initiated writes per day. Counter resets at midnight. Configurable via `WRITE_AI_DAILY_BUDGET`. |
| **No self-confirmation** | AI cannot confirm its own pending actions — only the human user can. This is enforced by the `pending_actions` flow (state_id required from frontend). |
| **Payload integrity** | The payload stored at confirmation time is the payload executed. AI cannot modify the action between preview and execution — the gateway re-validates but uses the stored payload, not a new one from the agent. |

### 11.4 Audit Log Integrity

| Protection | Description |
|------------|-------------|
| **Append-only** | No UPDATE or DELETE on `write_audit_log` except for status transitions (`pending` → `success`/`failed`). No endpoint exposes audit deletion. |
| **Tamper detection** | Each audit entry stores a SHA-256 hash of `(id, timestamp, operation, entity_id, payload_sent)`. Hash is verified on read by the audit viewer. |
| **Retention** | Audit entries are never auto-deleted. `WRITE_AUDIT_RETENTION_DAYS` controls the default query window, not deletion. |

### 11.5 Prompt Injection Defense

The AI agent constructs write operations from user chat input. A malicious or accidental prompt could attempt to:

| Attack Vector | Defense |
|---------------|---------|
| **"Create 1000 invoices"** | AI tools create ONE entity per call. Even if the agent loops, the daily budget (50) and rate limit (5/min) cap the damage. |
| **"Create invoice for €0.01 to drain stock"** | Minimum amount validation: `price >= 1.00` for invoice items (configurable via `WRITE_MIN_ITEM_PRICE`). Zero-price items allowed only for `kind='service'`. |
| **"Delete all contacts"** | Delete tools process one entity at a time. Rate limit: 5 deletes/hour. No bulk delete tool exists. |
| **"Update status to 99"** | Status must be in the allowed set per doc type. Invalid values rejected at validation. |
| **"Create contact with SQL in name"** | All text fields are parameterized queries (never string-interpolated). Holded API also rejects invalid input. |
| **"Send invoice to attacker@evil.com"** | `send_document` requires confirmation. The preview shows the recipient email prominently. User must approve. |
| **Agent tool confusion** | Tools have strict JSON schemas. The gateway re-validates params independently of what the AI thinks it passed — the AI's interpretation doesn't bypass validation. |

### 11.6 Replay & TOCTOU Protection

| Risk | Defense |
|------|---------|
| **Replay attack on pending_actions** | `state_id` is a UUID, used exactly once (popped from `pending_actions` dict after use). Cannot be replayed. |
| **TOCTOU (time-of-check-time-of-use)** | Validation runs at preview time (Stage 2). Between preview and execution (up to 5 min), the state could change (e.g., contact deleted). The gateway **re-validates** at execution time (Stage 4) using the same validators. If re-validation fails, the action is blocked and the user is informed. |
| **Expired pending action** | `pending_actions` entries expire after 5 minutes. `_cleanup_pending()` removes stale entries. Confirming an expired action returns an error, not a silent failure. |
| **Concurrent modification** | `_pending_lock` (threading.Lock) prevents race conditions on the `pending_actions` dict. The gateway also uses a per-entity lock during execution to prevent two writes to the same entity_id simultaneously. |

### 11.7 Holded API Key Protection

| Protection | Description |
|------------|-------------|
| **Never in logs** | API key is masked in all log output (`HOLDED_***`). |
| **Never in audit** | Audit `payload_sent` strips the `key` header before storage. |
| **Never in previews** | Preview objects contain no authentication data. |
| **Never in error responses** | Error details from Holded are sanitized before returning to frontend. |

---

## 12. Performance Budget & Monitoring

### 12.1 Latency Targets

The gateway adds overhead to every write. These are the target latencies per stage:

| Stage | Target Latency | Notes |
|-------|---------------|-------|
| 1. Validate | < 50ms | 1-3 DB queries (batched) |
| 2. Preview + Warn | < 100ms | Reuses validation context, batch warning queries |
| 3. Confirm/Log | < 10ms | In-memory dict operation + audit INSERT |
| 4. Execute (Holded API) | 500-2000ms | Network call, outside our control |
| 5. Sync-back | 500-1500ms | **Async** — does not block response |
| 6. Audit update | < 10ms | Single UPDATE query |
| **Total (user-perceived)** | **< 2.2s** | Stages 1-4 + 6 (sync-back is async) |

Without the gateway, a raw Holded API write takes 500-2000ms. The gateway adds ~170ms of overhead (validate + preview + audit) — acceptable for the safety it provides.

### 12.2 Pipeline Instrumentation

Every gateway execution logs timing per stage to `server.log`:

```
[GATEWAY] create_invoice | validate:32ms preview:67ms execute:1203ms sync:async audit:4ms | total:1306ms | status:success
```

If any stage exceeds 2x its target latency, log a warning:

```
[GATEWAY][SLOW] preview:245ms (target:100ms) for create_invoice — check warning query performance
```

### 12.3 Audit Log Size Management

The audit log stores full payloads and previews. Estimated row size:

| Field | Avg Size |
|-------|----------|
| `payload_sent` | ~500 bytes (typical invoice JSON) |
| `response_received` | ~200 bytes |
| `preview_data` | ~1 KB (includes resolved contact, items, warnings) |
| **Total per row** | ~2 KB |

At 50 writes/day max: ~100 KB/day, ~36 MB/year. No size concern for PostgreSQL.

For SQLite dev mode: monthly cleanup of entries older than `WRITE_AUDIT_RETENTION_DAYS` via a scheduled task (not auto-delete — move to archive table).

### 12.4 Connection Efficiency

| Optimization | Impact |
|-------------|--------|
| Single DB connection per pipeline | Eliminates 4-5 connection handshakes per write |
| Shared validation→preview context | Eliminates 2-3 duplicate queries per write |
| Batch `IN (...)` queries for items | Reduces N product lookups to 1 query |
| Async sync-back | Reduces user-perceived latency by 500-1500ms |
| Re-use existing `fetch_data()` for sync-back | No new HTTP client needed |

---

## 14. Configuration

New settings (can be stored in `settings` table or `.env`):

| Setting | Default | Description |
|---------|---------|-------------|
| `WRITE_HIGH_AMOUNT_THRESHOLD` | 5000 | EUR amount that triggers HIGH_AMOUNT warning |
| `WRITE_AI_AMOUNT_CEILING` | 10000 | EUR max for AI-created documents without override |
| `WRITE_DUPLICATE_WINDOW_HOURS` | 24 | Hours to check for duplicate documents |
| `WRITE_AUDIT_RETENTION_DAYS` | 365 | Default audit query window (entries never deleted) |
| `WRITE_CONFIRM_CLI` | true | Whether CLI scripts require y/n confirmation |
| `WRITE_AI_DAILY_BUDGET` | 50 | Max AI-initiated writes per day |
| `WRITE_RATE_LIMIT_PER_MIN` | 5 | AI write rate limit per minute |
| `WRITE_MIN_ITEM_PRICE` | 1.00 | Minimum price per invoice item (0 allowed for services) |

---

## 15. Testing Strategy

### Unit Tests
- Each validator function: valid inputs pass, invalid inputs return correct errors
- Preview builder: correct calculations (subtotals, taxes, totals)
- Warning generator: each warning triggers on correct conditions
- Status transition map: valid transitions pass, invalid blocked

### Integration Tests
- Full pipeline with SAFE_MODE=true: validate → preview → execute (dry run) → audit
- Targeted sync: mock Holded response, verify local DB updated
- Audit log: verify all fields populated correctly

### Safety Tests
- Attempt invalid status transitions → blocked
- Attempt duplicate contact creation → blocked
- Attempt invoice for non-existent contact → blocked
- Attempt overpayment → blocked
- Verify audit log written even on failure
- Verify reverse actions are correctly stored

### Security Tests
- Input sanitization: inject HTML/SQL in name fields → stripped/parameterized
- Entity ID format: pass non-ObjectId strings → rejected
- Rate limiting: burst 10 writes in 1 second → 5 succeed, 5 blocked with 429
- Daily budget: exhaust 50 writes → subsequent writes blocked
- Amount ceiling: AI creates invoice > EUR 10,000 → blocked without override
- Replay: reuse state_id after confirmation → rejected
- TOCTOU: delete contact between preview and confirmation → re-validation blocks execution
- Concurrent: two writes to same entity simultaneously → one succeeds, one waits
- Audit integrity: verify SHA-256 hash on stored audit entries
- API key: verify key never appears in logs, audit, or error responses
- Minimum price: create invoice item with price=0.001 → rejected for non-service items

### Performance Tests
- Pipeline latency: full write cycle < 2.2s (excluding Holded API time)
- Validation stage: < 50ms with 10 line items
- Preview stage: < 100ms with warning queries
- Async sync-back: verify response returns before sync completes
- Batch queries: 20-item invoice uses single product lookup query, not 20

---

## 16. Migration Plan

### Phase 0 — Prerequisites (connector.py fixes)
1. Fix `post_data()` / `put_data()` to accept HTTP 201 as success
2. Refactor `post_data()` / `put_data()` to return structured error objects instead of `None`
3. Add `delete_data(endpoint)` helper with SAFE_MODE intercept
4. Extract single-entity upsert logic from bulk sync functions into reusable helpers:
   - `_upsert_document(data, table, items_table, fk_col)` — from `sync_documents()`, handles all doc types (invoice, estimate, purchase)
   - `_upsert_contact(data)` — from `sync_contacts()`
   - `_upsert_product(data)` — from `sync_products()`

### Phase 1 — Foundation (new files, zero existing code impact)
5. Add `write_audit_log` table to `init_db()` — new table, safe
6. Create `write_gateway.py` — WriteGateway class with full pipeline
7. Create `write_validators.py` — validation rules for all operations
8. Create `write_preview.py` — preview builder + warning generator
9. Add targeted sync helpers to `connector.py` — `sync_single_document()`, `sync_single_contact()`, `sync_single_product()`
10. Add `insert_audit_log()` / `update_audit_log()` helpers to `connector.py`

### Phase 2 — Migrate existing AI tools (one at a time)
11. `create_estimate` → gateway (lowest risk, test thoroughly)
12. `create_invoice` → gateway
13. `create_contact` → gateway
14. `update_invoice_status` → gateway
15. `send_document` → gateway
16. `upload_file` → gateway (local-only, audit trail only)

### Phase 3 — REST audit integration
17. Wrap amortization CRUD endpoints with gateway audit logging
18. Wrap `web_include` toggle with gateway audit logging
19. Add `GET /api/audit-log` and `GET /api/audit-log/{id}` endpoints
20. Add `POST /api/audit-log/{id}/reverse` endpoint

### Phase 4 — Frontend enhancement
21. Update chat confirmation dialog to render rich preview + warnings
22. Add audit log viewer to the dashboard

### Phase 5 — New AI tools (after Phase 2 is stable)
23. Implement `create_product` tool + gateway integration
24. Implement `create_purchase` tool + gateway integration
25. Implement `pay_document` tool + gateway integration
26. Implement delete tools (document, contact, product) + gateway integration
27. Implement update tools (contact, product) + gateway integration

### Production readiness
28. Enable in production — SAFE_MODE still works as before, gateway adds safety on top
29. Monitor audit log for first 2 weeks, adjust warning thresholds
