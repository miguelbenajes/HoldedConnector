# Holded Connector — API Reference

> Reference for all REST API endpoints. For consumers: Brain v2, Gaffer, job-automation.ts, WEB-COYOTE.
>
> Auto-generated Swagger UI also available at `GET /docs` (requires auth).

## Authentication

All endpoints require one of:

| Method | Header | Used by |
|--------|--------|---------|
| Bearer token | `Authorization: Bearer <BRAIN_INTERNAL_KEY>` | Brain v2, Gaffer, automation scripts |
| Supabase JWT | `Authorization: Bearer <supabase_jwt>` | Frontend (cookie-based also works) |
| Legacy token | `Authorization: Bearer <LEGACY_API_TOKEN>` | Deprecated, read-only |

---

## Write Endpoints (via Safe Write Gateway)

All write endpoints route through a **6-stage safety pipeline**:
1. **Rate limit** — per-source limits (rest_api: 30/min, gaffer: 15/min)
2. **Validate** — input sanitization, ID format, business rules
3. **Preview** — calculate totals, generate warnings
4. **Execute** — call Holded API
5. **Sync-back** — async refresh of local DB from Holded
6. **Audit** — HMAC-signed log entry in `write_audit_log`

Feature flag: `USE_GATEWAY_FOR_AGENT=false` disables gateway routing (requires restart).

---

### POST /api/agent/invoice

Create a draft invoice in Holded.

**Request:**
```json
{
  "contact_id": "69d21b231cb1e76f1203c0ac",
  "desc": "Photography services March 2026",
  "items": [
    {"name": "Full day shoot", "units": 2, "subtotal": 500, "tax": 21},
    {"name": "Post-production", "units": 1, "subtotal": 300, "tax": 21}
  ]
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `contact_id` | string | yes | Holded 24-char hex ID |
| `desc` | string | no | Max 500 chars |
| `items` | array | yes | Line items (see Item schema below) |

**Response (success):**
```json
{"success": true, "id": "69d69e3db7da1569710102c6", "safe_mode": false}
```

**Response (error):**
```json
{"success": false, "error": "Failed to create invoice: Contact not found", "safe_mode": false}
```

---

### POST /api/agent/estimate

Create a draft estimate. Same schema as invoice.

**Request:** Same as `/api/agent/invoice`

**Response:** Same format — `{success, id, safe_mode}`

---

### PUT /api/agent/estimate/{estimate_id}

Update an estimate's line items. Used by job-automation.ts after user confirmation.

**Path:** `estimate_id` — 24-char hex string (e.g. `69d2277861af9abe440265a2`)

**Request:**
```json
{
  "contact_id": "69d21b231cb1e76f1203c0ac",
  "items": [
    {"name": "Updated service", "units": 3, "subtotal": 200, "tax": 21}
  ]
}
```

**Response (success):**
```json
{"success": true, "estimate_id": "69d2277861af9abe440265a2", "id": "69d2277861af9abe440265a2"}
```

> Both `estimate_id` and `id` returned for backwards compatibility with job-automation.ts.

---

### POST /api/agent/contact

Create a new contact in Holded. Gateway validates for duplicates (name + VAT).

**Request:**
```json
{
  "name": "Empresa Test SL",
  "email": "info@empresa.com",
  "phone": "+34612345678",
  "vatnumber": "B12345678",
  "type": "client"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | Max 200 chars |
| `email` | string | no | Max 200 chars |
| `phone` | string | no | Max 50 chars |
| `vatnumber` | string | no | NIF/CIF/VAT. Mapped to Holded `code` field |
| `type` | string | no | `client` (default), `supplier`, `debtor`, `creditor`, `lead` |

**Response:** `{success, id, safe_mode}`

---

### PUT /api/agent/invoice/{invoice_id}/approve

**CRITICAL: Submits invoice to Hacienda/SII. IRREVERSIBLE and legally binding.**

Approves a draft invoice (status 0 → 1). Assigns invoice number and locks editing.

**Required header:** `X-Confirm-Hacienda: true`

**Rate limit:** Max 1 approval per minute.

**Request:** No body required.

```bash
curl -X PUT "http://localhost:8000/api/agent/invoice/{id}/approve" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Confirm-Hacienda: true"
```

**Response (success):**
```json
{
  "success": true,
  "info": "Invoice approved",
  "hacienda_warning": true,
  "hacienda_detail": "Invoice submitted to Hacienda/SII. This is irreversible.",
  "audit_id": 4
}
```

**Response (missing header):** HTTP 403
```json
{"error": "Approve requires X-Confirm-Hacienda: true header (Hacienda/SII submission)"}
```

**Response (rate limited):** HTTP 429
```json
{"success": false, "error": "Rate limit: max 1 invoice approval per minute"}
```

---

### PUT /api/agent/invoice/{invoice_id}/status

Update invoice status (paid, cancelled, etc.).

**Request:**
```json
{"status": 3}
```

| Status | Meaning | Transitions from |
|--------|---------|------------------|
| 0 | Draft | — |
| 1 | Approved/Issued | 0 (WARNING: Hacienda submission) |
| 2 | Partial payment | 1 |
| 3 | Paid | 1, 2, 4 |
| 4 | Overdue | 1, 2 |
| 5 | Cancelled | 1, 2, 4 |

> Use `/approve` endpoint for 0→1 transition (has safety controls).

**Response:** `{success, safe_mode}`

---

### POST /api/agent/send/{doc_type}/{doc_id}

Send a document by email via Holded.

**Path params:**
- `doc_type`: `invoice`, `estimate`, `purchase`, `creditnote`, `proforma`
- `doc_id`: 24-char hex ID

**Request:**
```json
{
  "emails": ["client@example.com"],
  "subject": "Your invoice F-2026001",
  "body": "Please find attached your invoice."
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `emails` | array | no | Max 20 recipients |
| `subject` | string | no | Max 300 chars |
| `body` | string | no | Max 5000 chars |

**Response:** `{success, safe_mode}`

---

### POST /api/agent/convert-estimate

Convert an estimate to a draft invoice (borrador). Multi-step operation: creates invoice from estimate items, then marks estimate as invoiced.

**Request:**
```json
{"estimate_id": "69d2277861af9abe440265a2"}
```

**Response (success):**
```json
{"success": true, "invoice_id": "69d69e3db7da1569710102c6"}
```

---

### POST /api/gateway/estimate

Service-to-service endpoint for Gaffer. Creates estimate via gateway with `source="gaffer"`.

**Auth:** `Authorization: Bearer <BRAIN_INTERNAL_KEY>` (required)

**Request:**
```json
{
  "contact_id": "69d21b231cb1e76f1203c0ac",
  "items": [
    {"name": "Camera rental", "units": 3, "subtotal": 150, "tax": 21, "desc": "RED Komodo"}
  ],
  "desc": "Project NETFLIX-15042026",
  "date": "2026-04-15",
  "notes": "Shooting dates: 15-17 April"
}
```

**Response:**
```json
{"success": true, "entity_id": "...", "doc_number": "E260046"}
```

> Note: This endpoint returns `entity_id` (not `id`), unlike `/api/agent/*` endpoints.

---

## Read Endpoints

### GET /api/entities/{type}

List entities. Types: `invoices`, `estimates`, `contacts`, `products`, `purchases`.

**Query params:** `?search=<term>&limit=<n>&offset=<n>`

### GET /api/entities/{type}/{id}/items

Get line items for a document.

### GET /api/entities/{doc_type}/{doc_id}/pdf

Download document as PDF (proxied from Holded API).

### GET /api/invoices/unpaid

List unpaid invoices (status 1 or 4).

### GET /api/summary

Financial summary: total income, expenses, balance, top clients, monthly trends.

### GET /api/treasury

List bank accounts from Holded (id, name, IBAN).

### GET /api/products/web

Products with `web_include=1` for the website catalog.

### GET /api/agent/contact/{contact_id}

Full contact details + missing field warnings.

### PUT /api/agent/contact/{contact_id}

Update contact fields (name, email, vatnumber, phone, address, etc.).

---

## Job Tracker

### GET /api/jobs

List jobs. Query: `?status=open|shooting|invoiced|closed&quarter=Q1-2026`

### POST /api/jobs

Create/update a job (upsert by project_code). Used by Brain for job lifecycle.

---

## Item Schema

Line items in create/update document requests:

```json
{
  "name": "Camera rental - RED Komodo",
  "units": 3,
  "subtotal": 150.00,
  "tax": 21,
  "desc": "3-day rental, includes accessories",
  "discount": 0,
  "sku": "CAM-001"
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | Product/service name |
| `units` | number | no | Default: 1 |
| `subtotal` | number | yes | Unit price (EUR, ex-VAT) |
| `tax` | number | no | Tax rate %. Default: 21. Valid: 0, 4, 10, 21 |
| `desc` | string | no | Line item description |
| `discount` | number | no | Discount % |
| `sku` | string | no | Product SKU |
| `retention` | number | no | IRPF retention % (negative tax, e.g. -15) |
| `account` | string | no | Accounting code (e.g. "70500000") |

---

## Error Handling

All endpoints return consistent error format:

```json
{"success": false, "error": "Human-readable error message", "safe_mode": false}
```

Gateway-specific errors may include `audit_id` for traceability.

HTTP status codes:
- `200` — Success (check `success` field)
- `400` — Bad request (missing/invalid fields)
- `403` — Auth failed or missing required header
- `422` — Pydantic validation error (malformed request body)
- `429` — Rate limited

---

## Audit Trail

Every write operation via the gateway creates an entry in `write_audit_log`:
- `source`: who initiated (`rest_api`, `ai_agent`, `gaffer`, `brain_confirmed`)
- `operation`: what was done (`create_invoice`, `approve_invoice`, etc.)
- `status`: result (`pending`, `success`, `failed`, `dry_run`)
- `entity_id`: Holded entity ID created/modified
- `checksum`: HMAC-SHA256 tamper detection (if `AUDIT_HMAC_SECRET` is set)

---

## Holded API Limitations

Known limitations of the Holded API that affect consumers:

### Estimate status changes DO NOT WORK

The Holded API **accepts** PUT requests to change estimate status (returns `{"status": 1, "info": "Updated"}`) but **does not actually change the status**. Verified 2026-04-08: all estimates remain in their original status after the PUT call.

**Workaround:** Estimate lifecycle is managed through:
- **Create** → automatically draft (status 0)
- **Send to client** → via `POST /api/agent/send/estimate/{id}`
- **Convert to invoice** → via `POST /api/agent/convert-estimate` (marks estimate as invoiced)
- **Cancel** → only via Holded web UI (no API support)

### Approved invoices cannot be PUT-edited

Once an invoice is approved (status 1, submitted to Hacienda/SII), the Holded API rejects all PUT requests with `"Approved documents cannot be edited"`.

**To mark as paid:** Use the payment endpoint:
```bash
POST /api/documents/invoice/{id}/pay
{"date": 1744142400, "amount": 605.00, "treasury": "main", "desc": "Payment received"}
```

### Status field is unreliable

The `status` field returned by the Holded API does not always reflect the real document state. The connector derives the actual status from multiple fields: `approvedAt` (approval), `paymentsPending` (paid vs unpaid), `dueDate` (overdue), and API `status==3` (cancelled).

---

*Last updated: 2026-04-08 — Fase 3 (Gateway Bypass Migration)*
