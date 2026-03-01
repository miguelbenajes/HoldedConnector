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

---

## Database Schema

**Backend:** Dual-mode — PostgreSQL (Supabase) when `DATABASE_URL` is set, SQLite otherwise.

### Core Tables
- `invoices` — Sales invoices (status: 0=draft, 1=issued, 2=partial, 3=paid, 4=overdue, 5=cancelled)
- `purchase_invoices` — Expenses/purchases (same status codes)
- `estimates` — Presupuestos (status: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced)
- `contacts` — Clients & suppliers
- `products` — Inventory (price, stock, sku)
- `payments` — Payment records
- `projects` — Project tracking
- `ledger_accounts` — Chart of accounts
- `invoice_items` / `purchase_items` / `estimate_items` — Line items (SERIAL PK)

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

### Amortizations Endpoints
- `GET /api/amortizations` — List all with calculated revenue/profit/ROI
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

## File Structure

```
holded-connector/
├── api.py              # FastAPI server, all HTTP endpoints
├── connector.py        # DB abstraction, Holded API sync, all data access
├── ai_agent.py         # Claude tool_use agent, 19 tools, streaming
├── reports.py          # PDF/Excel report generation
├── requirements.txt    # Python dependencies
├── .env                # Local config (not in git)
├── .env.example        # Config template
├── CLAUDE.md           # This file
├── README.md           # Project readme
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

### Pending (Tasks 6-7)
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

---

**Last Updated:** 2026-03-01
**Latest Milestone:** Supabase migration (connector.py fully migrated, 20 tables in cloud)
