# HoldedConnector - Claude Development Notes

## Project Overview
FastAPI + Vanilla JS financial dashboard that syncs data from Holded API and includes an AI-powered virtual assistant built with Claude tool_use.

**Repo:** https://github.com/miguelbenajes/HoldedConnector (private)

---

## Architecture Highlights

### Backend Stack
- **FastAPI** (Python 3.9+) — API server on port 8000
- **SQLite** (holded.db) — Local data storage
- **Anthropic Claude API** — AI agent (claude-sonnet-4-20250514)
- **Holded API** — Sync invoices, purchases, estimates, contacts, products

### Frontend Stack
- **Vanilla JavaScript** — No frameworks
- **Chart.js v4** — Inline charts in chat
- **Dark theme** — Glassmorphic UI with Tailwind-like colors

### AI Agent
- **Tool use (function calling)** — 15 tools total
- **Streaming responses** — SSE (`text/event-stream`)
- **Write confirmation** — User approval for operations
- **Safe Mode** — Dry-run write operations (env: `HOLDED_SAFE_MODE=true`)

---

## Database Schema

### Core Tables
- `invoices` — Sales invoices (status: 0=draft, 1=issued, 2=partial, 3=paid, 4=overdue, 5=cancelled)
- `purchase_invoices` — Expenses/purchases (same status codes)
- `estimates` — Presupuestos (status: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced)
- `contacts` — Clients & suppliers
- `products` — Inventory
- `payments` — Payment records
- `projects` — Project tracking
- `ledger_accounts` — Chart of accounts

### AI-Related Tables
- `ai_history` — Conversation messages (columns: id, role, content, timestamp, conversation_id, tool_calls)
- `ai_favorites` — Saved queries (columns: id, query, label, created_at)
- `settings` — Configuration (key TEXT PRIMARY KEY, value TEXT)

---

## AI Agent Tools (15 Total)

### Read-Only Tools (6)
1. **query_database** — Execute SELECT queries with SQL injection prevention
2. **get_contact_details** — Fuzzy search contacts with transaction history
3. **get_product_pricing** — Product catalog + historical sale/purchase prices + margin analysis
4. **get_financial_summary** — Income/expenses/balance + top clients + monthly trends
5. **get_document_details** — Full invoice/purchase/estimate with line items
6. **get_overdue_invoices** — (NEW) Find overdue invoices, sorted by amount

### Write Tools (5) — Require User Confirmation
1. **create_estimate** — Draft presupuesto
2. **create_invoice** — Sales invoice
3. **send_document** — Email via Holded's API
4. **create_contact** — New client/supplier
5. **update_invoice_status** — (NEW) Mark invoice as paid, cancelled, etc.

### Utility Tools (4)
1. **generate_report** — PDF report with analysis
2. **get_upcoming_payments** — (NEW) Payments in next N days
3. **compare_periods** — (NEW) Period-over-period analysis with % changes
4. **render_chart** — (NEW) Generate inline Chart.js visualizations

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
- `GET /api/entities/<type>` — List (invoices, contacts, products, etc.)
- `GET /api/entities/<type>/<id>/items` — Line items
- `GET /api/entities/<type>/<id>/pdf` — PDF proxy
- `POST /api/sync` — Manual sync from Holded

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
  - Favorite button (⭐) on responses
  - Download links for PDF reports

### History & Favorites Drawer
- **Trigger:** 📋 button in chat header
- **Tabs:**
  - **History:** Past conversations, click to load
  - **Favorites:** Saved queries with ⭐, click to re-execute
- **Data:** Fetched on drawer open, cached in JS

### Welcome Screen (New Chat)
- Suggested queries: "Revenue this month", "Top clients", "Income vs Expenses chart", "Overdue invoices"
- Auto-hidden when typing

---

## Configuration

### Environment Variables (.env)
```bash
HOLDED_API_KEY=sk_...              # Holded API key
HOLDED_SAFE_MODE=true              # Dry-run mode for writes
ANTHROPIC_API_KEY=sk-ant-...       # Claude API key (optional, can set in UI)
```

### Settings Table
- `claude_api_key` — Saved Claude key (encrypted recommended)
- `ai_model` — Default: claude-sonnet-4-20250514
- `holded_api_key` — Saved Holded key

---

## Key Implementation Details

### Streaming Architecture
```python
def chat_stream(user_message, conversation_id):
    # Generator that yields SSE events:
    # - "tool_start" (using X tool)
    # - "tools_used" (summary after completion)
    # - "charts" (inline chart data if render_chart called)
    # - "text_delta" (20-char chunks)
    # - "done" (final)
    # - "confirmation_needed" (write op)
    # - "error"
```

Frontend consumes via `ReadableStream`:
```javascript
const reader = res.body.getReader();
// Parse SSE events, accumulate text, render charts
```

### Write Confirmation Flow
1. Agent calls write tool → generates state_id, stores in `pending_actions` (5 min TTL)
2. Frontend receives `confirmation_needed` event
3. User sees action details in modal, clicks Confirm/Cancel
4. `POST /api/ai/confirm` with state_id + confirmed boolean
5. If confirmed, tool executes and agent continues
6. If cancelled, operation aborts

### SQL Injection Prevention
```python
def _validate_sql(sql):
    # Only allows SELECT
    # Blocks INSERT/UPDATE/DELETE/DROP outside string literals
    # Simple heuristic: split on quotes, check dangerous keywords in outside text
```

---

## Recent Changes (Milestone 3: `1eb826b`)

### Backend (+400 lines)
- **ai_agent.py:**
  - Added `chat_stream()` generator for SSE
  - 5 new tools: get_overdue_invoices, get_upcoming_payments, compare_periods, update_invoice_status, render_chart
  - Favorites table & functions: get_favorites(), add_favorite(), remove_favorite()
  - Conversations function: get_conversations() → groups by UUID, shows first message & count

- **api.py:**
  - New endpoints: `/api/ai/chat/stream`, `/api/ai/conversations`, `/api/ai/favorites` (GET/POST/DELETE)
  - SSE generator wraps `chat_stream()` into FastAPI StreamingResponse

### Frontend (+300 lines)
- **app.js:**
  - Streaming consumer: `ReadableStream` + SSE parsing
  - Chart renderer: `renderInlineChart()` uses Chart.js
  - Drawer functions: `toggleHistoryDrawer()`, `loadConversations()`, `loadFavorites()`
  - Favorite management: `addFavorite()`, `removeFavorite()`
  - Toast notification on favorite save

- **index.html:**
  - Added drawer panel with tabs (History/Favorites)
  - New suggested queries in welcome screen

- **style.css (+80 lines):**
  - `.chat-drawer` — History/favorites panel
  - `.drawer-tabs`, `.drawer-item` — Tab & item styling
  - `.chat-chart-wrapper`, `.chat-chart-container` — Chart styles
  - `.chat-toast` — Toast animation

---

## Common Commands

### Server Management
```bash
# Start server
cd /Users/miguel/IA\ SHARED/holded-connector
nohup python3 api.py > server.log 2>&1 &

# Stop server
lsof -ti:8000 | xargs kill -9

# View logs
tail -f server.log
```

### Git Workflow
```bash
# Commit changes
git add ai_agent.py api.py static/app.js static/index.html static/style.css
git commit -m "Message here..."

# Push
PATH="$HOME/bin:$PATH" git push

# View commits
git log --oneline -5
```

### Testing AI Agent
```python
import ai_agent
events = list(ai_agent.chat_stream('How many invoices?', 'test-conv'))
for e in events:
    print(e['event'], ':', e['data'][:100])
```

---

## Known Limitations

1. **No Real Streaming in Agent Loop** — Tool calls are non-streaming (full response before text)
2. **In-Memory Favorites/History** — No persistence across server restarts (stored in SQLite but UI state is session-based)
3. **Simple SQL Validation** — Regex-based, not foolproof
4. **No Authentication** — Anyone with server access can use AI
5. **Rate Limiting** — 10 requests/min per IP (basic)
6. **SAFE_MODE Simulation** — Doesn't actually call Holded, just returns fake ID

---

## Future Enhancements

- [ ] Real-time notifications for overdue invoices
- [ ] Scheduled AI reports (email weekly summary)
- [ ] Multi-language support (currently: Spanish/English)
- [ ] Holded webhook integration for live sync
- [ ] Mobile app (React Native)
- [ ] Dark/light theme toggle
- [ ] User authentication & roles
- [ ] Chat search & filtering

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Port 8000 already in use | `lsof -ti:8000 \| xargs kill -9` |
| "API key not configured" | Set ANTHROPIC_API_KEY or paste in UI |
| Charts not rendering | Check Chart.js CDN link in index.html |
| Favorites not saving | Check SQLite permissions, ai_favorites table |
| Streaming hangs | Restart server, check server.log for errors |
| SAFE_MODE not working | Verify `HOLDED_SAFE_MODE=true` in .env |

---

## Contact & Resources

- **Repo:** https://github.com/miguelbenajes/HoldedConnector
- **Holded API Docs:** https://www.holdedapp.com/api
- **Anthropic Claude API:** https://console.anthropic.com
- **Chart.js Docs:** https://www.chartjs.org/

---

**Last Updated:** 2026-02-17
**Latest Commit:** 1eb826b (Milestone 3: Streaming + Charts + History + 5 new tools)
