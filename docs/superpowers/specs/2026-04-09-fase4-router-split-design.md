# Fase 4: Split api.py into Routers — Design Spec

## Context

`api.py` is a 2215-line monolith with 63 endpoints, 21 Pydantic models, middleware, startup/shutdown hooks, and background threads. This makes it hard to navigate, test, and modify safely. Fase 4 splits it into 10 domain-focused routers while preserving all behavior.

**Fases 0-3 are deployed and stable.** This refactor is purely structural — zero behavior changes.

## Approach

**Two commits:**
1. **Commit 1 (router split):** Move endpoints from api.py to `app/routers/*.py`. api.py becomes a thin entry point (~120 lines) with middleware, startup/shutdown, static mount, and `include_router()` calls. Pure code movement — verifiable by diff.
2. **Commit 2 (factory pattern):** Extract app creation to `app/factory.py`. api.py becomes a 5-line `create_app()` caller. Helpers extracted to `app/domain/`.

This spec covers **Commit 1 only**. Commit 2 is a follow-up.

## Router Architecture

### Directory Structure

```
app/
  routers/
    __init__.py
    jobs.py              # 7 endpoints — job tracker, note queue
    amortizations.py     # 18 endpoints — ROI tracking, analysis, matches, audit log
    files.py             # 7 endpoints — reports, uploads, file config
    entities.py          # 16 endpoints — list/get invoices, contacts, products, PDF proxy
    dashboard.py         # 6 endpoints — summary, stats, unpaid invoices
    ai.py                # 11 endpoints — chat, streaming, history, favorites, AI config
    agent_writes.py      # 12 endpoints — all /api/agent/* write endpoints
    treasury.py          # 2 endpoints — bank accounts, pay document
    sync.py              # 5 endpoints — sync, config, health, schema
    gateway_api.py       # 1 endpoint — Gaffer service-to-service
```

### What Stays in api.py

After the split, api.py retains only:

```python
# api.py (~120 lines)
# 1. Imports
# 2. FastAPI app creation
# 3. Auth middleware (lines 54-112) — stays here, applies to all routes
# 4. CORS middleware (lines 121-126)
# 5. Module-level state: sync_status, analysis_status, scheduler thread
# 6. Background helpers: run_sync(), run_analysis_job(), _daily_scheduler()
# 7. Startup/shutdown hooks
# 8. Router registration (10x app.include_router)
# 9. Static files mount (must be LAST)
# 10. Root route GET / (serves index.html)
```

**Key decision:** Background helpers (`run_sync`, `run_analysis_job`, `_daily_scheduler`, `_claude_categorize`) stay in api.py for Commit 1 because they use module-level state (`sync_status`, `analysis_status`, locks). They move to `app/domain/` in Commit 2.

### Router Registration

```python
from app.routers import (
    jobs, amortizations, files, entities, dashboard,
    ai, agent_writes, treasury, sync, gateway_api,
)

app.include_router(sync.router)          # /health, /api/sync, /api/config, /api/schema
app.include_router(dashboard.router)     # /api/summary, /api/stats/*
app.include_router(entities.router)      # /api/entities/*, /api/products/web, /api/recent
app.include_router(files.router)         # /api/files/*, /api/reports/*, /api/tickets/*
app.include_router(ai.router)            # /api/ai/*
app.include_router(amortizations.router) # /api/amortizations/*, /api/analysis/*, /api/audit-log/*
app.include_router(treasury.router)      # /api/treasury, /api/documents/*/pay
app.include_router(agent_writes.router)  # /api/agent/*
app.include_router(gateway_api.router)   # /api/gateway/*
app.include_router(jobs.router)          # /api/jobs/*, /api/estimates/without-ref
```

No prefix on routers — each endpoint keeps its exact current path.

## Router Details

### 1. routers/jobs.py (7 endpoints)

**Endpoints:**
- `GET /api/jobs` — list jobs
- `POST /api/jobs` — create job (Brain entry point)
- `GET /api/jobs/{code}` — job detail + expenses
- `PATCH /api/jobs/{code}` — update job status/dates
- `POST /api/jobs/{code}/sync-note` — force Obsidian sync
- `POST /api/jobs/flush-queue` — process note queue
- `GET /api/estimates/without-ref` — estimates missing project_code

**Imports needed:** `connector`, `db_context`, `skills.job_tracker` (ensure_job, flush_note_queue, sync_job_to_obsidian)

**Pydantic models:** None (uses raw request dicts)

**State:** None (stateless)

### 2. routers/amortizations.py (18 endpoints)

**Endpoints:**
- 7x amortization CRUD (`/api/amortizations/*`)
- 3x purchase links (`/api/amortizations/purchases/*`)
- 1x pack info (`/api/products/{id}/pack-info`)
- 4x analysis (`/api/analysis/*`)
- 1x search purchases (`/api/purchases/search`)
- 2x audit log (`/api/audit-log`, `/api/audit-log/{id}`)

**Imports needed:** `connector`, `db_context`, `write_validators._row_to_dict`

**Pydantic models (move here):** `AmortizationCreate`, `AmortizationUpdate`, `PurchaseLinkCreate`, `PurchaseLinkUpdate`, `MatchConfirm`

**State:** `analysis_status`, `_analysis_lock` — **passed from api.py** via module-level reference or dependency injection.

**Design decision:** Analysis endpoints need `run_analysis_job()` which lives in api.py (Commit 1). The router calls it via import: `from api import run_analysis_job`. This circular import is resolved because the router is imported after api.py defines the function. In Commit 2, `run_analysis_job` moves to `app/domain/analysis.py`.

### 3. routers/files.py (7 endpoints)

**Endpoints:**
- `GET /api/reports/excel` — download Excel report
- `GET /api/reports/download/{filename}` — download PDF
- `POST /api/tickets/upload` — legacy upload
- `GET /api/files/config` — get directory paths
- `POST /api/files/config` — set directory paths
- `POST /api/files/upload` — upload CSV/Excel
- `GET /api/files/list` — list files

**Imports needed:** `connector`, `reports`, `pandas`, `os`, `io`, `time`

**Pydantic models (move here):** `DirectoryConfig`

**State:** None

### 4. routers/entities.py (16 endpoints)

**Endpoints:**
- List: invoices, purchases, estimates, contacts, products (5)
- Items: invoice items, purchase items, estimate items, estimate items fresh (4)
- History: contact history, product history (2)
- Special: recent activity, web products, PDF proxy, web-include toggle, search purchases (4)
- Note: `get_estimates_without_ref` goes to jobs.py instead (domain fit)

**Imports needed:** `connector`, `db_context`, `requests` (for PDF proxy)

**Pydantic models (move here):** `WebIncludeToggle`

**State:** None

### 5. routers/dashboard.py (6 endpoints)

**Endpoints:**
- `GET /api/summary` — totals
- `GET /api/stats/date-range` — date picker range
- `GET /api/stats/monthly` — monthly aggregates
- `GET /api/stats/range` — custom range stats
- `GET /api/stats/top-contacts` — top 10 clients/suppliers
- `GET /api/invoices/unpaid` — unpaid with aging

**Imports needed:** `db_context`, `_assert_valid_table` (from api.py or extract to shared util)

**Pydantic models:** None

**State:** None

**Note:** `_assert_valid_table` and `_VALID_TABLE_RE` are used by dashboard and entities. Extract to `app/routers/_shared.py` or keep in api.py and import.

### 6. routers/ai.py (11 endpoints)

**Endpoints:**
- `POST /api/ai/chat` — non-streaming (legacy)
- `POST /api/ai/chat/stream` — SSE streaming (primary)
- `POST /api/ai/confirm` — confirm write
- `GET /api/ai/history` — load conversation
- `DELETE /api/ai/history` — clear conversation
- `GET /api/ai/conversations` — list conversations
- `GET /api/ai/favorites` — list favorites
- `POST /api/ai/favorites` — save favorite
- `DELETE /api/ai/favorites/{id}` — remove favorite
- `GET /api/ai/config` — check Claude config
- `POST /api/ai/config` — save Claude key

**Imports needed:** `ai_agent`, `connector`

**Pydantic models (move here):** `ChatRequest`, `ConfirmRequest`, `FavoriteRequest`, `AIConfigUpdate`

**State:** None (ai_agent manages its own state internally)

### 7. routers/agent_writes.py (12 endpoints)

**Endpoints:**
- `POST /api/agent/invoice` — create invoice
- `POST /api/agent/estimate` — create estimate
- `PUT /api/agent/estimate/{id}` — update estimate items
- `POST /api/agent/contact` — create contact
- `PUT /api/agent/invoice/{id}/approve` — approve (Hacienda)
- `PUT /api/agent/invoice/{id}/status` — update status
- `POST /api/agent/send/{type}/{id}` — send document
- `POST /api/agent/convert-estimate` — convert to invoice
- `GET /api/agent/contact/{id}` — get contact details
- `PUT /api/agent/contact/{id}` — update contact

**Imports needed:** `connector`, `gateway`, `RateLimiter`, `build_holded_items`, `build_holded_items_with_accounts`, `db_context`, `requests`

**Pydantic models (move here):** `CreateDocumentBody`, `CreateContactBody`, `UpdateStatusBody`, `SendDocumentBody`, `UpdateContactBody`, `ConvertEstimateBody`

**Module-level (move here):**
- `_USE_GATEWAY` — feature flag
- `_approve_limiter` — RateLimiter instance
- `_gw_error()` — gateway error helper

### 8. routers/treasury.py (2 endpoints)

**Endpoints:**
- `GET /api/treasury` — bank accounts from Holded
- `POST /api/documents/{doc_type}/{doc_id}/pay` — register payment

**Imports needed:** `connector`, `requests`

**Pydantic models (move here):** `PayDocumentBody`

**State:** None

### 9. routers/sync.py (5 endpoints)

**Endpoints:**
- `GET /health` — health check
- `POST /api/sync` — trigger sync
- `GET /api/sync/status` — sync status
- `GET /api/config` — Holded config check
- `POST /api/config` — update Holded config
- `GET /api/schema` — DB introspection

**Imports needed:** `connector`, `db_context`, `requests`

**Pydantic models (move here):** `ConfigUpdate`

**State:** `sync_status`, `_sync_lock` — passed from api.py. `run_sync()` stays in api.py for Commit 1.

### 10. routers/gateway_api.py (1 endpoint)

**Endpoints:**
- `POST /api/gateway/estimate` — Gaffer service-to-service

**Imports needed:** `gateway`, `os` (BRAIN_INTERNAL_KEY)

**Pydantic models (move here):** `GatewayEstimateItem`, `GatewayEstimateBody`

**State:** None

## Shared Utilities

Extract to `app/routers/_shared.py`:

```python
"""Shared utilities for routers."""
import re

_VALID_TABLE_RE = re.compile(r'^[a-z_][a-z0-9_]*$')

def assert_valid_table(name: str) -> None:
    """Raise ValueError if table name contains unexpected characters."""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
```

Used by: dashboard.py, entities.py, sync.py (schema endpoint).

## Circular Import Prevention

**Risk:** Router imports api.py function (e.g., `run_analysis_job`) → api.py imports router → circular.

**Solution for Commit 1:** Routers that need api.py functions use lazy imports inside the endpoint function:
```python
@router.post("/api/analysis/run")
def trigger_analysis():
    from api import run_analysis_job  # lazy import avoids circular
    ...
```

**Commit 2 eliminates this** by moving helpers to `app/domain/`.

## Verification Plan

After each router extraction:
1. `python3 -c "import ast; ast.parse(open('api.py').read()); print('OK')"` — syntax check
2. `python3 -m pytest tests/ -v` — 88 tests must pass
3. `curl /health` on deployed server — smoke test
4. Spot-check 2-3 endpoints per router with curl

Final verification:
- `wc -l api.py` — should be ~120 lines (down from 2215)
- `wc -l app/routers/*.py` — should total ~2100 lines
- All 63 endpoints respond identically (same paths, same response format)
- No import errors on startup

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Circular imports | Lazy imports in Commit 1, proper extraction in Commit 2 |
| Missing import in router | Each router gets explicit imports at top, verified by AST parse |
| Shared state broken | sync_status and analysis_status stay in api.py, routers reference via import |
| Endpoint path changes | No prefixes on routers — paths stay identical |
| Auth middleware broken | Middleware stays in api.py, applies to all routes globally |
| Static files override routes | Mount remains LAST (after all routers) |

## Rules from Bug Fix Session (cozy-coalescing-blum)

These rules apply during the split — do NOT introduce regressions:

1. **Holded API: POST uses "items", GET returns "products". NEVER confuse.** Already fixed in write_gateway.py — don't touch that file.
2. **DB reads with API fallback:** `_fetch_document`, `_fetch_contact`, and `validate_convert_estimate` now have API fallbacks. These live in write_validators.py — don't touch that file.
3. **fetch_estimate_fresh** now includes tax/taxes/retention/discount/account fields. Lives in connector.py — don't touch.
4. **None of these files (write_gateway.py, write_validators.py, connector.py) are modified in Fase 4.** Only api.py is split.

## Out of Scope

- Factory pattern (Commit 2)
- Extracting `run_sync`, `run_analysis_job` to domain modules (Commit 2)
- Splitting connector.py or ai_agent.py (Fase 5)
- Adding new endpoints or changing behavior
