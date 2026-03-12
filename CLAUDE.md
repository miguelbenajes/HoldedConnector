# HoldedConnector - Claude Development Notes

## Project Overview
FastAPI + Vanilla JS financial dashboard that syncs data from Holded API to PostgreSQL (Supabase) and includes an AI-powered virtual assistant built with Claude tool_use.

**Repo:** https://github.com/miguelbenajes/HoldedConnector (private)

---

## Architecture Highlights

### Backend Stack
- **FastAPI** (Python 3.9+) — API server on port 8000
- **PostgreSQL (Supabase)** — Primary cloud database (production)
- **SQLite** (holded.db) — Local dev fallback (when `DATABASE_URL` is not set)
- **Anthropic Claude API** — AI agent (claude-sonnet-4-20250514)
- **Holded API** — Sync invoices, purchases, estimates, contacts, products

### Frontend Stack
- **Vanilla JavaScript** — No frameworks
- **Chart.js v4** — Inline charts in chat
- **Dark/Light theme** — Glassmorphic UI with theme toggle

### AI Agent
- **Tool use (function calling)** — 19 tools total
- **Streaming responses** — SSE (`text/event-stream`)
- **Write confirmation** — User approval for write operations
- **Safe Mode** — Dry-run write operations (env: `HOLDED_SAFE_MODE=true`)

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

**Backend:** Dual-mode — PostgreSQL (Supabase) when `DATABASE_URL` is set, SQLite otherwise.

### Core Tables
- `invoices` — Sales invoices (status: 0=draft, 1=issued, 2=partial, 3=paid, 4=overdue, 5=cancelled)
- `purchase_invoices` — Expenses/purchases (same status codes)
- `estimates` — Presupuestos (status: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced)
- `contacts` — Clients & suppliers
- `products` — Inventory (price, stock, sku, kind: 'simple'|'pack', web_include: 0|1 default 1)
- `pack_components` — Pack composition (pack_id, component_id, quantity) — refreshed on sync
- `payments` — Payment records
- `projects` — Project tracking
- `ledger_accounts` — Chart of accounts
- `invoice_items` / `purchase_items` / `estimate_items` — Line items (SERIAL PK, include `project_id` + `kind`)
- `invoices` / `purchase_invoices` / `estimates` — include `tags` (JSON array as TEXT) + `notes`
- `projects` — Synced from Holded; line items reference projects via `project_id`

### AI-Related Tables
- `ai_history` — Conversation messages (id, role, content, timestamp, conversation_id, tool_calls)
- `ai_favorites` — Saved queries (id, query, label, created_at)
- `settings` — Key-value configuration (key TEXT PRIMARY KEY, value TEXT)

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

### Write Tools (6) — Require User Confirmation
1. **create_estimate** — Draft presupuesto
2. **create_invoice** — Sales invoice
3. **send_document** — Email via Holded's API
4. **create_contact** — New client/supplier
5. **update_invoice_status** — Mark invoice as paid, cancelled, etc.
6. **upload_file** — Register uploaded file for analysis

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

### Amortizations Endpoints
- `GET /api/products/{id}/pack-info` — Pack composition or pack membership
- `GET /api/amortizations` — List all with calculated revenue/profit/ROI (includes pack-attributed revenue)
- `GET /api/amortizations/summary` — Global totals (invested, recovered, profit, ROI%)
- `POST /api/amortizations` — Add product to tracking
- `PUT /api/amortizations/{id}` — Update price/date/notes
- `DELETE /api/amortizations/{id}` — Remove from tracking

---

## Frontend Features

### Chat Panel (Floating FAB)
- **Location:** Bottom-right corner, FAB opens slide-in panel
- **Width:** 420px (desktop), 100% (mobile)
- **Features:**
  - Streaming text display (token by token)
  - Inline Chart.js charts (bar, line, doughnut, pie)
  - Tool use visualization ("Using query_database...")
  - Write confirmation dialog
  - Favorite button on responses
  - Download links for PDF reports
  - File upload for CSV/Excel analysis

### History & Favorites Drawer
- **Trigger:** Button in chat header
- **Tabs:** History (past conversations) / Favorites (saved queries)
- **Data:** Fetched on drawer open, cached in JS

### Dashboard Features
- Live search across entity tables
- Invoice subtabs (all/unpaid/overdue)
- Aging widget for receivables
- Column resizer on data tables
- Dark/light theme toggle

### Frontend View Routing
- `showView(name)` in app.js maps special views via `specialViews` dict
- Entity views auto-route to `view-entity` + `loadEntityData()`
- Custom views (overview, setup, amortizations) need explicit entry in `specialViews`

---

## Configuration

### Environment Variables (.env)
```bash
HOLDED_API_KEY=your_key_here            # Holded API key
HOLDED_SAFE_MODE=true                   # Dry-run mode for writes
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
- `claude_api_key` — Saved Claude key
- `ai_model` — Default: claude-sonnet-4-20250514
- `holded_api_key` — Saved Holded key
- `uploads_dir` / `reports_dir` — Custom file paths

---

## Key Implementation Details

### Streaming Architecture
```python
def chat_stream(user_message, conversation_id):
    # Generator yielding SSE events:
    # "tool_start", "tools_used", "charts", "text_delta", "done",
    # "confirmation_needed", "error"
```
Frontend consumes via `ReadableStream` + SSE parsing.

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

### Holded API Field Reference (discovered from live responses)
- Invoice/estimate/purchase top-level: `tags` (array), `notes`, `customFields` (array), `docNumber`
- Line item fields: `projectid` (lowercase — not camelCase `projectId`), `kind`, `costPrice`, `desc`
- Store tags as: `json.dumps(item.get('tags') or [])` — requires `import json` in connector.py
- Holded purchases API times out on page 2 consistently — not a code bug, all records are on page 1
- To inspect all available API fields: fetch a tiny time window → `params={'starttmp': X, 'endtmp': X+100000}`

### Project Tracking (added 2026-03-03)
- **Tags** on documents (`invoices.tags`, etc.) — easiest: tag whole document in Holded with job code
- **`project_id`** on line items — finer control, assign per line in Holded
- Query by tag: `WHERE tags LIKE '%"CODE"%'` or parse JSON in Python
- Query by project: `JOIN projects p ON ii.project_id = p.id`
- Workflow: create project in Holded → assign to line items or tag document → sync → query

### Project Code System (added 2026-03-12)
- **Product "Proyect REF:"** (ID: `69b2b35f75ae381d8f05c133`) — a €0 product in Holded
- Add it as a line item on any quote/invoice; the **description field** carries the project code
- **Format convention:** `CLIENT-YYMMDD` (e.g. `MEDIASET-260315`)
- During sync, `_extract_project_code()` detects the item by productId or name (case-insensitive fallback)
- Extracted code stored in `project_code` column on `invoices`, `estimates`, `purchase_invoices`
- Line item descriptions (`desc`) are synced on all 3 items tables
- Query: `SELECT * FROM invoices WHERE project_code = 'NETFLIX-260315'`
- If the "Proyect REF:" item is removed, `project_code` is cleared to NULL on next sync

### Sync Functions Pattern
```python
# SQLite path:
cursor.execute("INSERT OR REPLACE INTO contacts (...) VALUES (?, ?)", (a, b))

# PostgreSQL path:
cursor.execute("""INSERT INTO contacts (...) VALUES (%s, %s)
    ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, ...""", (a, b))
```
Items tables (invoice_items, etc.) use DELETE + INSERT pattern.

---

## Data Cleaning & Linking Tools

**Purpose:** Clean up invoice_items without product_id, link them to actual products, enable ROI tracking.

### 1. `inventory_matcher.py` — Fuzzy Match Invoice Concepts to Products
- Reads invoice_items where product_id IS NULL
- Fuzzy matches against products table (≥60% similarity threshold)
- Outputs Excel with two sheets: MATCHED (for reference) + NOT_MATCHED (candidates for creation)
- Uses `openpyxl` for formatted output with checkboxes and editable fields
- **Usage:** `/usr/bin/python3 inventory_matcher.py` → generates `products_to_import.xlsx`

### 2. `link_matched_products.py` — Link Items + Auto-Create Amortizations
- Reads Excel MATCHED sheet (openpyxl)
- Bulk updates invoice_items.product_id in single transaction
- Creates amortizations for linked products (ON CONFLICT safe)
- **Gotcha:** String trimming matters — `'California sun bounce '` ≠ `'California sun bounce'` (trailing spaces)
- **Usage:** `/usr/bin/python3 link_matched_products.py`
- **Impact:** 204 items linked, revenue visibility increased 7x (€16k → €114k)

### 3. `migrate_amortizations.py` — SQLite → Supabase Migration
- One-time migration of manually-curated amortizations from holded.db
- Maps old SQLite AUTOINCREMENT IDs → new PostgreSQL SERIAL IDs
- Sets purchase_item_id=NULL (SERIAL IDs don't port between databases)
- Safe: Handles duplicates gracefully (ON CONFLICT DO NOTHING)
- **Usage:** `/usr/bin/python3 migrate_amortizations.py` (ran once on 2026-03-02)

### 4-7. Product Management Suite (`product-management/` folder)
Self-contained toolset for classification, linking, and import workflows:

**4. `classify_products.py` — Classify Unmatched Products (Phase 2)**
- Reads 307 NOT_MATCHED products from `products_to_import.xlsx`
- Categorizes into 4 types: Real Products (177), Services (46), Expenses (64), Administrative (20)
- Uses keyword-based classification with configurable thresholds
- Outputs `products_classified.xlsx` with separate sheets per category
- **Usage:** `cd product-management && python3 classify_products.py`

**5. `generate_products_for_import.py` — Master Import File Generator (Phase 3)**
- Reads classified products and generates comprehensive Excel for data entry
- Creates 5 sheets: Real Products, Expenses (with project_id), Services-Fees, Administrative, Reference
- Includes OT (overtime) detection for service entries (pattern: "OT xx hours on xx day")
- Outputs `products_for_import.xlsx` ready for user review and manual data entry
- **Usage:** `cd product-management && python3 generate_products_for_import.py`

**6. `apply_product_corrections.py` — Apply User Decisions & Link to Inventory (Phase 4)**
- Reads user corrections from `products_for_import.xlsx`
- Applies reclassifications (e.g., Real Product ↔ Service ↔ Expense)
- Links products to existing inventory when appropriate
- Creates amortizations for linked products (e.g., ALQ MACBOOK → Macbook Pro Max M3)
- Regenerates `products_for_import.xlsx` with corrected classifications
- **Usage:** `cd product-management && python3 apply_product_corrections.py`

**7. `product_mappings.yaml` — Learning File for Classification Patterns**
- Documents classification rules, keywords, and patterns learned from user decisions
- Stores product linking rules (e.g., ALQ prefix → equipment rental)
- Records all reclassifications and rationale for future automation
- Enables scripts to apply learned patterns to new unmatched products
- **Purpose:** Improve automation accuracy over time; reference for similar classification tasks
- **Format:** YAML with sections for rules, examples, automations, reference data

**See `product-management/README.md` for full workflow documentation.**

### 8. `link_holded_to_knowledge.py` — Map Holded Products → Knowledge DB
- Interactive CLI tool to create 1:1 mappings between Holded products and `knowledge.product_models`
- Fuzzy matches product names, shows top 8 candidates with similarity scores
- Stores mappings in `knowledge.holded_product_links` table (Supabase)
- Supports auto-link mode (`a`) for ≥80% matches, skip (`s`), quit (`q`)
- Re-runnable: skips already-linked products on each run
- **Requires:** `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` in `.env`, `pip install supabase`
- **Usage:** `/usr/bin/python3 link_holded_to_knowledge.py`
- **Impact:** 71 products linked (2026-03-09), enables merged catalog in `apps/web`

---

## File Structure

```
holded-connector/
├── api.py              # FastAPI server, all HTTP endpoints
├── connector.py        # DB abstraction, Holded API sync, all data access
├── ai_agent.py         # Claude tool_use agent, 19 tools, streaming
├── reports.py          # PDF/Excel report generation
├── inventory_matcher.py         # Generate Excel with fuzzy-matched products (phase 1)
├── link_matched_products.py     # Bulk link invoice_items to products + create amortizations
├── migrate_amortizations.py     # SQLite→Supabase migration for amortizations (40 items)
├── backfill_packs.py            # One-time: populate pack_components + migrate pack amortizations
├── link_holded_to_knowledge.py  # Interactive: map Holded products → knowledge.product_models
├── requirements.txt    # Python dependencies
├── .env                # Local config (not in git)
├── .env.example        # Config template
├── CLAUDE.md           # This file
├── README.md           # Project readme
├── product-management/ # Product classification & import suite
│   ├── README.md       # Product management workflow documentation
│   ├── classify_products.py         # Phase 2: Classify 307 NOT_MATCHED products
│   ├── match_expenses_to_inventory.py # Phase 2: Fuzzy match expenses to inventory
│   ├── generate_products_for_import.py # Phase 3: Generate master import file
│   ├── apply_product_corrections.py    # Phase 4: Apply user corrections & link to inventory
│   ├── product_mappings.yaml           # Learned classification rules & patterns
│   ├── products_classified.xlsx        # Output: Classification by category
│   ├── products_final_review.xlsx      # Output: Expenses matched to inventory
│   └── products_for_import.xlsx        # Output: Master file ready for import
├── docs/plans/         # Migration/design documents
├── skills/             # AI skill templates
└── static/
    ├── index.html      # Main HTML (single-page app)
    ├── app.js          # All frontend logic (~2400 lines)
    ├── style.css       # All styles (~1600 lines)
    ├── hdate.js        # Calendar/date picker component
    ├── manifest.json   # PWA manifest
    ├── sw.js           # Service worker
    └── icons/          # PWA icons
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

## Migration Status

### Completed
- [x] `connector.py` — Full dual-backend (SQLite/PostgreSQL) with all helpers
- [x] `reports.py` — Migrated to `connector.get_db()`
- [x] Supabase — 20 tables created, full data sync verified
- [x] PWA — Installable on desktop and mobile
- [x] Dark/light theme toggle
- [x] Amortizations migration — 40 items SQLite → Supabase (`migrate_amortizations.py`)
- [x] Invoice linking — 207 items linked (`inventory_matcher.py` + `link_matched_products.py`)
- [x] Data cleaning — Revenue impact: €16k → €114k (7x increase from proper product linking)
- [x] Project tracking — `tags` + `project_id` + `notes` added to all doc/item tables (2026-03-03)
- [x] 307 NOT_MATCHED products classified + reviewed (`product-management/` suite complete)
- [x] `process_reviewed_items.py` — reads user-reviewed CSV, creates amortizations with revenue data
- [x] Website integration — `web_include` field on products + `/api/products/web` endpoint (2026-03-09)
- [x] Knowledge DB linking — 71 products mapped via `link_holded_to_knowledge.py` → `knowledge.holded_product_links` (2026-03-09)
- [x] Merged catalog endpoint in `apps/web` — `GET /api/products/catalog` joins knowledge specs + Holded pricing (2026-03-09)

### Pending (Tasks 8+)
- [ ] Create 34 new real products + fill cost prices (`products_processed.xlsx` Sheets 1 & 2)
- [ ] Create 40 services (fee type) + 61 expenses in Holded (`products_processed.xlsx` Sheets 3 & 4)
- [ ] `api.py` — Still has ~3 raw `sqlite3.connect()` calls (lines ~211, ~622, ~1120, ~1191)
- [ ] `ai_agent.py` — Still has ~22 raw `sqlite3.connect()` calls (all exec_* functions)
- [ ] Docker deployment (Dockerfile, docker-compose.yml)
- [ ] n8n integration workflows

---

## Known Limitations

1. **Raw sqlite3 in api.py/ai_agent.py** — These files bypass the abstraction layer (migration pending)
2. **No Real Streaming in Agent Loop** — Tool calls are non-streaming (full response before text)
3. **Simple SQL Validation** — Regex-based, not foolproof
4. **No Authentication** — Anyone with server access can use the AI
5. **Rate Limiting** — 10 requests/min per IP (basic)
6. **SAFE_MODE Simulation** — Doesn't actually call Holded, returns fake ID

---

## PWA & Deployment

### PWA (Progressive Web App)
- `static/manifest.json` — App name, icons, theme, display mode
- `static/sw.js` — Service worker (cache-first for static, network-first for API)
- `static/icons/icon-192.png`, `icon-512.png` — App icons

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

---

## Obsidian Vault Sync (MANDATORY)

See global `~/.claude/CLAUDE.md` for full rules. Document Holded connector changes (API, sync, tools, schema) in `Coyote AI/` via `mcp__obsidian__*` tools.

---

**Last Updated:** 2026-03-09
**Latest Milestone:** Website integration — `web_include` flag + `/api/products/web` price feed; 71 products linked to knowledge DB via `holded_product_links`; merged catalog endpoint live in `apps/web`
