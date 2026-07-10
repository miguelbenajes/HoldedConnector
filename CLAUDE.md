# HoldedConnector - Claude Development Notes

## Project Overview
FastAPI + Vanilla JS financial dashboard syncing Holded API data to PostgreSQL (Supabase), with an AI virtual assistant built on Claude tool_use.

**Repo:** https://github.com/miguelbenajes/HoldedConnector (private)

---

## Architecture Highlights

### Stack
- **Backend:** FastAPI (Python 3.9+, port 8000) · PostgreSQL/Supabase (production) · SQLite `holded.db` (dev fallback when `DATABASE_URL` unset) · Anthropic Claude API (claude-sonnet-4-20250514) · Holded API sync
- **Frontend:** Vanilla JS (no frameworks) · Chart.js v4 inline charts · glassmorphic UI with dark/light theme toggle
- **AI agent:** tool use (19 tools) · SSE streaming (`text/event-stream`) · write confirmation · Safe Mode dry-run writes

### Database Abstraction Layer
All DB access goes through `connector.py` helpers — **never use raw `sqlite3.connect()` or `psycopg2.connect()`** in other files.

```python
# Core helpers (connector.py)
DATABASE_URL = os.getenv("DATABASE_URL")
_USE_SQLITE  = not DATABASE_URL          # True = SQLite dev mode, False = PostgreSQL

get_db()            # Returns sqlite3 or psycopg2 connection
_cursor(conn)       # Returns Row/RealDictCursor for dict-like access
_q(sql)             # Converts ? placeholders to %s for PostgreSQL
_num(val)           # Sanitizes empty strings to None (PG rejects "" in NUMERIC)
_row_val(row, key)  # Extracts value from dict or tuple row
_fetch_one_val(c,k) # Fetches single scalar from either cursor type
```

**PostgreSQL Gotchas (important for future changes):**
- `desc` is a reserved keyword — always quote as `"desc"` in SQL
- Empty strings fail in NUMERIC columns — use `_num()` for all numeric fields
- `GROUP BY` is strict — all non-aggregated columns must be listed
- `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (pk) DO UPDATE SET ...`
- `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`
- `cursor.lastrowid` returns OID in psycopg2 — use `RETURNING id` instead
- `GROUP_CONCAT(x, ',')` → `STRING_AGG(x, ',')`
- `datetime('now')` → `NOW()`
- `AUTOINCREMENT` → `SERIAL`
- `REAL` → `NUMERIC`
- PG NUMERIC returns `decimal.Decimal` in Python — cast to `float()` before arithmetic with floats

---

## Database Schema

### Core Tables
- `invoices` — Sales invoices (status: 0=draft, 1=pending, 3=paid, 4=overdue, 5=cancelled — derived from Holded API fields, not raw status)
- `purchase_invoices` — Expenses/purchases (same status codes)
- `estimates` — Presupuestos (status: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced)
- `contacts` — Clients & suppliers
- `products` — Inventory (price, stock, sku, kind: 'simple'|'pack', web_include: 0|1 default 1)
- `pack_components` — Pack composition (pack_id, component_id, quantity) — refreshed on sync
- `payments` — Payment records
- `projects` — Project tracking (synced from Holded; line items reference via `project_id`)
- `ledger_accounts` — Chart of accounts
- `invoice_items` / `purchase_items` / `estimate_items` — Line items (SERIAL PK, include `project_id` + `kind`)
- Doc tables (`invoices`/`purchase_invoices`/`estimates`) also include `tags` (JSON array as TEXT) + `notes`

### AI-Related Tables
- `ai_history` — Conversation messages (id, role, content, timestamp, conversation_id, tool_calls)
- `ai_favorites` — Saved queries (id, query, label, created_at)
- `settings` — Key-value configuration (key TEXT PRIMARY KEY, value TEXT)

### Job Tracker Tables
- `jobs` — One row per project code (PK: project_code), tracks client, shooting dates, quarter, estimate/invoice refs, Obsidian note path, status (open/shooting/invoiced/closed)
- `job_note_queue` — Pending Obsidian note sync queue (retry_count, processed_at)

### Analysis Tables
- `amortizations` — Rental ROI tracking (product_id UNIQUE, purchase_price, purchase_date, notes)
- `purchase_analysis` — AI-categorized purchases (purchase_id UNIQUE, category, subcategory, confidence)
- `inventory_matches` — Purchase-to-product matching (purchase_id + product_id UNIQUE)
- `amortization_purchases` — Cost allocation for amortizations
- `product_type_rules` — Configurable tax/expense rules by product type
- `sync_logs` — Sync execution history (for n8n integration)

---

## AI Agent Tools (19 Total)

### Read-Only Tools (8)
1. **query_database** — Execute SELECT queries with SQL injection prevention
2. **get_contact_details** — Fuzzy search contacts with transaction history
3. **get_product_pricing** — Product catalog + historical sale/purchase prices + margin analysis
4. **get_financial_summary** — Income/expenses/balance + top clients + monthly trends
5. **get_document_details** — Full invoice/purchase/estimate with line items
6. **get_overdue_invoices** — Find overdue invoices, sorted by amount
7. **get_upcoming_payments** — Payments in next N days
8. **get_amortization_status** — ROI tracking data for amortized products

### Write Tools (7) — Require User Confirmation
1. **create_estimate** — Draft presupuesto
2. **create_invoice** — Sales invoice (always as borrador — NEVER approveDoc)
3. **send_document** — Email via Holded's API
4. **create_contact** — New client/supplier
5. **update_invoice_status** — Mark invoice as paid, cancelled, etc. (**CRITICAL:** status 0→1 submits to Hacienda via SII — irreversible)
6. **convert_estimate_to_invoice** — Convert presupuesto to factura (borrador). Supports natural language: "convierte el último presupuesto a factura"
7. **upload_file** — Register uploaded file for analysis

### Utility Tools (5)
1. **generate_report** — PDF report with analysis
2. **compare_periods** — Period-over-period analysis with % changes
3. **render_chart** — Generate inline Chart.js visualizations
4. **analyze_file** — Analyze uploaded CSV/Excel files
5. **list_files** — List files in uploads/reports directory

---

## API Endpoints

### AI Chat Endpoints
- `POST /api/ai/chat` — Non-streaming chat (legacy)
- `POST /api/ai/chat/stream` — **SSE streaming** (primary, token-by-token)
- `POST /api/ai/confirm` — Confirm write operation
- `GET /api/ai/history?conversation_id=<uuid>` — Load conversation
- `DELETE /api/ai/history?conversation_id=<uuid>` — Clear conversation
- `GET /api/ai/conversations` — List past conversations (max 20)
- `GET /api/ai/favorites` — List saved queries
- `POST /api/ai/favorites` — Save query as favorite
- `DELETE /api/ai/favorites/<id>` — Remove favorite
- `GET /api/ai/config` — Check Claude key, model, safe mode
- `POST /api/ai/config` — Save Claude API key

### Data Endpoints
- `GET /api/summary` — Total income/expenses/balance
- `GET /api/stats/monthly` — Monthly trends
- `GET /api/stats/date-range` — Custom date range stats
- `GET /api/entities/<type>` — List (invoices, contacts, products, etc.)
- `GET /api/entities/<type>/<id>/items` — Line items
- `GET /api/entities/<type>/<id>/pdf` — PDF proxy
- `GET /api/invoices/unpaid` — Unpaid invoices list
- `POST /api/sync` — Manual sync from Holded

### File Endpoints
- `GET /api/files/config` — Current uploads/reports directory paths
- `POST /api/files/config` — Update directory paths
- `POST /api/files/upload` — Upload CSV/Excel file
- `GET /api/files/list` — List files in directory

### Website Integration Endpoints
- `GET /api/products/web` — Products with `web_include=1` (id, name, sku, price, stock, kind) — consumed by `apps/web` catalog
- `PATCH /api/entities/products/{id}/web-include` — Toggle `web_include` flag (`{"web_include": true|false}`)

### Treasury & Payment Endpoints
- `GET /api/treasury` — Fetch bank accounts from Holded API (id, name, type, iban, bankname)
- `POST /api/documents/{docType}/{docId}/pay` — Register payment in Holded (body: `{date, amount, treasury, desc}`)
- `POST /api/agent/convert-estimate` — Convert estimate to draft invoice via Safe Write Gateway (body: `{estimate_id}`)

### Job Tracker Endpoints
- `GET /api/jobs` — List jobs (filter: status, quarter)
- `POST /api/jobs` — Create job (Brain entry point)
- `GET /api/jobs/{code}` — Job detail + expenses
- `PATCH /api/jobs/{code}` — Update status/dates/invoice
- `POST /api/jobs/{code}/sync-note` — Force re-render to Obsidian
- `POST /api/jobs/flush-queue` — Process pending note queue

### Amortizations Endpoints
- `GET /api/products/{id}/pack-info` — Pack composition or pack membership
- `GET /api/amortizations` — List all with calculated revenue/profit/ROI (includes pack-attributed revenue)
- `GET /api/amortizations/summary` — Global totals (invested, recovered, profit, ROI%)
- `POST /api/amortizations` — Add product to tracking
- `PUT /api/amortizations/{id}` — Update price/date/notes
- `DELETE /api/amortizations/{id}` — Remove from tracking

---

## Frontend Features

### Chat Panel & Dashboard (UI)
- FAB chat (bottom-right, 420px desktop / 100% mobile): streaming, inline Chart.js, tool-use visualization, write confirmation dialog, favorites, PDF links, CSV/Excel upload
- History & Favorites drawer (chat header button, fetched on open + cached in JS)
- Dashboard: live search, invoice subtabs (all/unpaid/overdue), aging widget, column resizer, theme toggle

### Entity Table Action Buttons
Header "New" button per entity view links to Holded web. Row-level: PDF viewer + Holded edit link (all docs); "Facturar" on estimates (status != 4) → `POST /api/agent/convert-estimate`; "Pagar" on invoices (status 1/2/4) → payment modal (bank selector, amount pre-filled)

### Hacienda / SII Safety (CRITICAL)
- Approving an invoice (borrador→aprobada, status 0→1) submits it to Hacienda via SII — **irreversible and legally binding**
- All invoices are created as borrador — gateway NEVER sets `approveDoc`
- Estimates/quotes are safe to approve — they do NOT go to Hacienda
- `update_invoice_status` tool shows critical warning when status=1 is requested

### Frontend View Routing
`showView(name)` in app.js: entity views auto-route to `view-entity` + `loadEntityData()`; custom views (overview, setup, amortizations) need explicit entry in the `specialViews` dict

---

## Configuration

### Environment Variables (.env)
```bash
HOLDED_API_KEY=your_key_here            # Holded API key
HOLDED_SAFE_MODE=false                  # Dry-run mode for writes (currently live)
ANTHROPIC_API_KEY=sk-ant-...            # Claude API key (optional, can set in UI)

# PostgreSQL (Supabase) — leave blank for SQLite dev mode
# Use Session Pooler connection string (not Transaction Pooler — psycopg2 incompatible)
DATABASE_URL=postgresql://postgres.[ref]:[pass]@aws-0-eu-west-1.pooler.supabase.com:5432/postgres

# Production
ALLOWED_ORIGINS=https://yourdomain.com  # CORS restriction (default: *)
UPLOADS_DIR=/var/data/uploads           # Custom upload path (default: ./uploads)
REPORTS_DIR=/var/data/reports            # Custom reports path (default: ./reports)

# Supabase Knowledge DB — for linking products to knowledge.product_models
SUPABASE_URL=https://mpgfivufawurjnpyvacf.supabase.co
SUPABASE_SERVICE_KEY=sb_secret_...      # Service role key
```

### Settings Table (runtime config)
Keys: `claude_api_key`, `ai_model` (default claude-sonnet-4-20250514), `holded_api_key`, `uploads_dir` / `reports_dir`.

---

## Key Implementation Details

### Streaming Architecture
`chat_stream()` yields SSE events (`tool_start`, `tools_used`, `charts`, `text_delta`, `done`, `confirmation_needed`, `error`); frontend consumes via `ReadableStream`.

### Write Confirmation Flow
1. Agent calls write tool → generates state_id, stores in `pending_actions` (5 min TTL)
2. Frontend receives `confirmation_needed` event
3. User sees action details, clicks Confirm/Cancel
4. `POST /api/ai/confirm` with state_id + confirmed boolean
5. If confirmed, tool executes and agent continues

### DB Schema Migrations
- `init_db()` runs on every server start via `@app.on_event("startup")`
- Uses dialect tokens: `_serial` (SERIAL vs AUTOINCREMENT), `_real` (NUMERIC vs REAL), `_now` (NOW() vs datetime('now'))
- All tables use `CREATE TABLE IF NOT EXISTS`
- **Never** add a table without adding it to `init_db()` in connector.py

### Project Tracking
- **Tags** on documents (whole-doc job code) or **`project_id`** on line items (finer control). Workflow: create project in Holded → tag/assign → sync → query
- Query by tag: `WHERE tags LIKE '%"CODE"%'`; by project: `JOIN projects p ON ii.project_id = p.id`

### Project Code System
- €0 product **"Proyect REF:"** (ID `69b2b35f75ae381d8f05c133`) as line item; its **description** carries the code, format `CLIENT-DDMMYYYY` (e.g. `MEDIASET-15032026`)
- On sync, `_extract_project_code()` detects it (productId or name, case-insensitive) → `project_code` column on `invoices` / `estimates` / `purchase_invoices` (NULL if item removed). Line-item `desc` synced on all 3 items tables. Query: `WHERE project_code = 'NETFLIX-15032026'`

### Sync Functions Pattern
Upserts follow the PG gotchas mapping above (`INSERT OR REPLACE` → `ON CONFLICT ... DO UPDATE`). Items tables (invoice_items, etc.) use DELETE + INSERT pattern.

---

## Data Cleaning & Linking Tools

Maintenance/one-off scripts — details in each script's docstring:
- `inventory_matcher.py` — fuzzy-match unlinked invoice_items → Excel · `link_matched_products.py` — bulk-link + create amortizations
- `migrate_amortizations.py` / `backfill_packs.py` — one-time migrations, already run
- `link_holded_to_knowledge.py` — interactive map to `knowledge.product_models`
- `product-management/` — classification & import suite (rules in `product_mappings.yaml`; see its `README.md`)

---

## File Structure

```
holded-connector/
├── api.py              # FastAPI server, all HTTP endpoints
├── connector.py        # DB abstraction, Holded API sync, all data access
├── ai_agent.py         # Claude tool_use agent, 19 tools, streaming
├── write_gateway.py / write_validators.py / write_preview.py  # Safe Write Gateway: 6-stage pipeline · validation · previews
├── auth.py             # Triple-auth middleware (Supabase cookie + JWT + legacy token)
├── reports.py          # PDF/Excel report generation
├── *.py + product-management/  # Data cleaning scripts — see "Data Cleaning & Linking Tools"
├── skills/job_tracker.py  # Job dossier system: date parser, note renderer, Obsidian sync
└── static/             # SPA: index.html, app.js (~2400 lines), style.css (~1600 lines), hdate.js, PWA assets
```

---

## Common Commands

### Server Management
```bash
cd /Users/miguel/IA\ SHARED/holded-connector
nohup python3 api.py > server.log 2>&1 &   # Start
lsof -ti:8000 | xargs kill -9              # Stop
tail -f server.log                          # Logs
```

### Git Workflow
```bash
git add <files>
git commit -m "type: message"
PATH="$HOME/bin:$PATH" git push
```

### Verify Supabase Connection
```bash
/usr/bin/python3 -c "
import connector
connector.init_db()
conn = connector.get_db()
cur = connector._cursor(conn)
cur.execute('SELECT count(*) as c FROM invoices')
print('Invoices:', connector._fetch_one_val(cur, 'c'))
conn.close()
"
```

---

## Known Limitations

- Raw sqlite3 in api.py/ai_agent.py bypasses the abstraction layer (migration pending)
- Agent loop tool calls are non-streaming (full response before text)
- SQL validation is regex-based, not foolproof
- Rate limiting: 10 req/min per IP (basic) + gateway limits per source
- SAFE_MODE doesn't actually call Holded, returns fake ID

---

## PWA & Deployment

PWA: `static/manifest.json` + `static/sw.js` (cache-first static, network-first API) + `static/icons/`.

### Quick Deploy
```bash
git clone https://github.com/miguelbenajes/HoldedConnector.git
cd HoldedConnector
pip install -r requirements.txt
cp .env.example .env  # Configure DATABASE_URL + API keys
python3 api.py
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Port 8000 already in use | `lsof -ti:8000 \| xargs kill -9` |
| "API key not configured" | Set ANTHROPIC_API_KEY or paste in UI |
| Charts not rendering | Check Chart.js CDN link in index.html |
| Streaming hangs | Restart server, check server.log |
| `syntax error at or near "desc"` | Column `desc` is a PG reserved word — quote as `"desc"` in SQL |
| `invalid input for type numeric: ""` | Use `_num()` to sanitize — Holded API returns empty strings for some numeric fields |
| `INSERT OR REPLACE` fails on PG | Use `INSERT ... ON CONFLICT (pk) DO UPDATE SET` pattern |
| `cursor.lastrowid` returns wrong value | Use `RETURNING id` for PostgreSQL inserts needing new PK |
| New table missing after code change | Add `CREATE TABLE IF NOT EXISTS` in `init_db()`, restart server |
| PWA not installable | Needs HTTPS in production (localhost works without) |
| Invoice items not linking in bulk UPDATE | String trimming: `'text '` with trailing space ≠ `'text'` — use TRIM() or .strip() in code |
| ON CONFLICT silently skips duplicates | Not an error — check rowcount to verify inserts. Use rowcount=0 to detect skips |
| Fuzzy matching missing valid matches | Threshold ≥60% is configurable — adjust `difflib.get_close_matches(cutoff=...)` if needed |
| Holded purchases page 2 timeout | Expected API flakiness — page 1 captures all records, safe to ignore |
| `NameError: json not defined` in connector.py | Add `import json` at top — needed for `json.dumps(tags)` |
| `projectid` not found on line items | Holded uses lowercase `projectid` not camelCase `projectId` |
| Invoice shows as draft but is approved | Holded API `status` field is unreliable — check `approvedAt` timestamp. Fixed in `sync_documents()` (2026-03-13) |

---

## Obsidian Vault Sync (MANDATORY)

See global `~/.claude/CLAUDE.md` for full rules. Document Holded connector changes (API, sync, tools, schema) in `Coyote AI/` via `mcp__obsidian__*` tools.
