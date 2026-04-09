# Fase 4: Split api.py into 10 Routers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split api.py (2215 lines, 63 endpoints) into 10 domain-focused routers under `app/routers/`, leaving api.py as a thin ~120-line entry point.

**Architecture:** Each router is a FastAPI `APIRouter` with no prefix (paths stay identical). Endpoints are moved as-is — zero behavior changes. Pydantic models move to the router that uses them (or `_shared.py` if shared). Module-level state (sync_status, analysis_status, scheduler) stays in api.py.

**Tech Stack:** FastAPI APIRouter, Python 3.11

**Spec:** `docs/superpowers/specs/2026-04-09-fase4-router-split-design.md`
**Source map:** 63 endpoints mapped with line numbers in the exploration phase.

---

## File Structure

```
app/
  routers/
    __init__.py           # empty
    _shared.py            # _assert_valid_table, shared helpers
    jobs.py               # 7 endpoints: /api/jobs/*, /api/estimates/without-ref
    amortizations.py      # 18 endpoints: /api/amortizations/*, /api/analysis/*, /api/audit-log/*, /api/purchases/search, /api/products/{id}/pack-info
    files.py              # 7 endpoints: /api/reports/*, /api/files/*, /api/tickets/*
    entities.py           # 16 endpoints: /api/entities/*, /api/products/web, /api/recent
    dashboard.py          # 6 endpoints: /api/summary, /api/stats/*, /api/invoices/unpaid
    ai.py                 # 11 endpoints: /api/ai/*
    agent_writes.py       # 12 endpoints: /api/agent/*
    treasury.py           # 2 endpoints: /api/treasury, /api/documents/*/pay
    sync.py               # 5 endpoints: /health, /api/sync/*, /api/config, /api/schema
    gateway_api.py        # 1 endpoint: /api/gateway/estimate
api.py                    # Thin entry point: ~120 lines (middleware, startup, router registration)
```

---

## Execution Order

Routers are extracted from least to most risky. After each task:
1. AST parse api.py + the new router
2. Run `pytest tests/ -q` (88 tests must pass)
3. Commit

| Task | Router | Endpoints | Risk |
|------|--------|-----------|------|
| 1 | Setup: `__init__.py`, `_shared.py` | 0 | None |
| 2 | `jobs.py` | 7 | Low |
| 3 | `treasury.py` | 2 | Low |
| 4 | `gateway_api.py` | 1 | Low |
| 5 | `files.py` | 7 | Low |
| 6 | `sync.py` | 5 | Low |
| 7 | `amortizations.py` | 18 | Medium |
| 8 | `entities.py` | 16 | Medium |
| 9 | `dashboard.py` | 6 | Medium |
| 10 | `agent_writes.py` | 12 | Medium |
| 11 | `ai.py` | 11 | High |
| 12 | Cleanup api.py + final verification | 0 | Low |

---

### Task 1: Setup router directory and shared utilities

**Files:**
- Create: `app/routers/__init__.py`
- Create: `app/routers/_shared.py`

- [ ] **Step 1: Create directory and __init__.py**

```bash
mkdir -p app/routers
touch app/routers/__init__.py
```

- [ ] **Step 2: Create _shared.py with table validation helper**

```python
"""Shared utilities for API routers."""
import re

_VALID_TABLE_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


def assert_valid_table(name: str) -> None:
    """Raise ValueError if table name contains unexpected characters."""
    if not _VALID_TABLE_RE.match(name):
        raise ValueError(f"Invalid table name: {name!r}")
```

- [ ] **Step 3: Verify import**

Run: `cd services/holded-connector && python3 -c "from app.routers._shared import assert_valid_table; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add app/routers/
git commit -m "chore: setup app/routers directory with shared helpers"
```

---

### Task 2: Extract routers/jobs.py (7 endpoints)

**Files:**
- Create: `app/routers/jobs.py`
- Modify: `api.py` — remove job endpoints, add `include_router`

**Endpoints to move (current api.py line numbers):**
- `GET /api/jobs` (~L1993)
- `POST /api/jobs` (~L2029)
- `GET /api/jobs/{code}` (~L2066)
- `PATCH /api/jobs/{code}` (~L2100)
- `POST /api/jobs/{code}/sync-note` (~L2145)
- `POST /api/jobs/flush-queue` (~L2197)
- `GET /api/estimates/without-ref` (~L2157)

- [ ] **Step 1: Create `app/routers/jobs.py`**

The router file must:
- `from fastapi import APIRouter, Request`
- `from fastapi.responses import JSONResponse`
- `import connector, re as _re_mod, logging`
- `from app.db.connection import db_context`
- `router = APIRouter()`
- Move all 7 endpoint functions from api.py, replacing `@app.` with `@router.`
- Import `ensure_job`, `flush_note_queue`, `sync_job_to_obsidian` from `skills.job_tracker` inside the functions that use them (they're already imported lazily in current api.py)

Copy each endpoint function verbatim from api.py, only changing `@app.get` → `@router.get`, `@app.post` → `@router.post`, `@app.patch` → `@router.patch`.

- [ ] **Step 2: Register router in api.py**

Add to api.py after the existing imports:
```python
from app.routers import jobs as jobs_router
```

Add before static files mount:
```python
app.include_router(jobs_router.router)
```

- [ ] **Step 3: Remove moved endpoints from api.py**

Delete the 7 endpoint functions and their associated code blocks from api.py. Keep any Pydantic models or helpers that are still used by other endpoints.

- [ ] **Step 4: Verify**

```bash
python3 -c "import ast; ast.parse(open('api.py').read()); ast.parse(open('app/routers/jobs.py').read()); print('OK')"
python3 -m pytest tests/ -q
```
Expected: 88 passed

- [ ] **Step 5: Commit**

```bash
git add app/routers/jobs.py api.py
git commit -m "refactor: extract jobs router (7 endpoints)"
```

---

### Task 3: Extract routers/treasury.py (2 endpoints)

**Files:**
- Create: `app/routers/treasury.py`
- Modify: `api.py`

**Endpoints to move:**
- `GET /api/treasury` (~L1923)
- `POST /api/documents/{doc_type}/{doc_id}/pay` (~L1960)

**Pydantic model to move:** `PayDocumentBody` (~L1953)

- [ ] **Step 1: Create `app/routers/treasury.py`**

Imports needed:
```python
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import connector, requests, logging, os, re as _re_mod
```

Move `PayDocumentBody` model + both endpoints. Replace `@app.` with `@router.`.

- [ ] **Step 2: Register in api.py, remove moved code**

- [ ] **Step 3: Verify (AST + pytest)**

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor: extract treasury router (2 endpoints)"
```

---

### Task 4: Extract routers/gateway_api.py (1 endpoint)

**Files:**
- Create: `app/routers/gateway_api.py`
- Modify: `api.py`

**Endpoints to move:**
- `POST /api/gateway/estimate` (~L1869)

**Pydantic models to move:** `GatewayEstimateItem` (~L1855), `GatewayEstimateBody` (~L1862)

- [ ] **Step 1: Create `app/routers/gateway_api.py`**

Imports needed:
```python
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from write_gateway import gateway
import os, logging
```

Move models + endpoint. The endpoint has its own Bearer token auth check (BRAIN_INTERNAL_KEY) — this must move with it.

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract gateway_api router (1 endpoint)"
```

---

### Task 5: Extract routers/files.py (7 endpoints)

**Files:**
- Create: `app/routers/files.py`
- Modify: `api.py`

**Endpoints to move:**
- `GET /api/reports/excel` (~L398)
- `GET /api/reports/download/{filename}` (~L416)
- `POST /api/tickets/upload` (~L430)
- `GET /api/files/config` (~L990)
- `POST /api/files/config` (~L1002)
- `POST /api/files/upload` (~L1017)
- `GET /api/files/list` (~L1071)

**Pydantic model to move:** `DirectoryConfig` (~L998)

**Module-level constants to copy:** `REPORTS_DIR`, `UPLOADS_DIR` (or import from connector)

- [ ] **Step 1: Create `app/routers/files.py`**

Imports needed:
```python
from fastapi import APIRouter, UploadFile, File, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import connector, reports, os, time, io, logging
import pandas as pd
```

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract files router (7 endpoints)"
```

---

### Task 6: Extract routers/sync.py (5 endpoints)

**Files:**
- Create: `app/routers/sync.py`
- Modify: `api.py`

**Endpoints to move:**
- `GET /health` (~L144)
- `POST /api/sync` (~L354)
- `GET /api/sync/status` (~L363)
- `GET /api/config` (~L367)
- `POST /api/config` (~L377)
- `GET /api/schema` (~L1435)

**Pydantic model to move:** `ConfigUpdate` (~L374)

**Shared state needed:** `sync_status`, `_sync_lock` — import from api.py via lazy import, or pass as module reference.

- [ ] **Step 1: Create `app/routers/sync.py`**

Imports needed:
```python
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import connector, requests, logging, os
from app.db.connection import db_context
from app.routers._shared import assert_valid_table
```

**Key design:** The `POST /api/sync` endpoint calls `run_sync()` which stays in api.py. Use lazy import:
```python
@router.post("/api/sync")
def sync_data(background_tasks: BackgroundTasks):
    from api import sync_status, _sync_lock, run_sync
    # ... rest of endpoint
```

Similarly `GET /api/sync/status`:
```python
@router.get("/api/sync/status")
def get_sync_status():
    from api import sync_status
    return sync_status
```

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract sync router (5 endpoints)"
```

---

### Task 7: Extract routers/amortizations.py (18 endpoints)

**Files:**
- Create: `app/routers/amortizations.py`
- Modify: `api.py`

**Endpoints to move:**
- 7x amortization CRUD: `/api/amortizations`, `/api/amortizations/summary`, `/api/amortizations/types`, POST/PUT/DELETE amortizations, `/api/products/{id}/pack-info`
- 3x purchase links: POST/PUT/DELETE `/api/amortizations/purchases/*`
- 1x search: `GET /api/purchases/search`
- 4x analysis: `/api/analysis/status`, `/api/analysis/run`, `/api/analysis/matches`, `/api/analysis/matches/{id}/confirm`, `/api/analysis/categories`, `/api/analysis/invoices`
- 2x audit log: `GET /api/audit-log`, `GET /api/audit-log/{id}`

**Pydantic models to move:** `AmortizationCreate`, `AmortizationUpdate`, `PurchaseLinkCreate`, `PurchaseLinkUpdate`, `MatchConfirm`

**Module-level to move:** `VALID_PRODUCT_TYPES`

**Shared state:** `analysis_status`, `_analysis_lock` — lazy import from api.py. `run_analysis_job()` stays in api.py.

- [ ] **Step 1: Create `app/routers/amortizations.py`**

Imports needed:
```python
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
import connector, logging, json
from app.db.connection import db_context
from write_validators import _row_to_dict
```

For analysis endpoints that need `run_analysis_job` or `analysis_status`:
```python
@router.post("/api/analysis/run")
def trigger_analysis(background_tasks: BackgroundTasks, batch_size: int = Query(10)):
    from api import run_analysis_job, analysis_status, _analysis_lock
    # ... rest of endpoint
```

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract amortizations router (18 endpoints)"
```

---

### Task 8: Extract routers/entities.py (16 endpoints)

**Files:**
- Create: `app/routers/entities.py`
- Modify: `api.py`

**Endpoints to move:**
- `GET /api/recent` (~L627)
- `GET /api/entities/contacts` (~L649)
- `GET /api/entities/contacts/{id}/history` (~L656)
- `GET /api/entities/products` (~L669)
- `GET /api/products/web` (~L676)
- `PATCH /api/entities/products/{id}/web-include` (~L692)
- `GET /api/entities/products/{id}/history` (~L712)
- `GET /api/entities/invoices` (~L733)
- `GET /api/entities/invoices/{id}/items` (~L740)
- `GET /api/entities/purchases` (~L747)
- `GET /api/entities/purchases/{id}/items` (~L754)
- `GET /api/entities/estimates` (~L761)
- `GET /api/entities/estimates/{id}/items` (~L768)
- `GET /api/entities/estimates/{id}/items/fresh` (~L775)
- `GET /api/entities/{doc_type}/{doc_id}/pdf` (~L787)

**Pydantic model to move:** `WebIncludeToggle` (~L689)

- [ ] **Step 1: Create `app/routers/entities.py`**

Imports needed:
```python
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import connector, requests, logging, os, re as _re_mod
from app.db.connection import db_context
from app.routers._shared import assert_valid_table
```

The PDF proxy endpoint uses `requests` to fetch from Holded API and `connector.HEADERS`.

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract entities router (16 endpoints)"
```

---

### Task 9: Extract routers/dashboard.py (6 endpoints)

**Files:**
- Create: `app/routers/dashboard.py`
- Modify: `api.py`

**Endpoints to move:**
- `GET /api/summary` (~L458)
- `GET /api/stats/date-range` (~L484)
- `GET /api/invoices/unpaid` (~L511)
- `GET /api/stats/monthly` (~L552)
- `GET /api/stats/range` (~L596)
- `GET /api/stats/top-contacts` (~L613)

- [ ] **Step 1: Create `app/routers/dashboard.py`**

Imports needed:
```python
from fastapi import APIRouter, Query
from typing import Optional
import connector, logging
from app.db.connection import db_context
from app.routers._shared import assert_valid_table
```

The summary endpoint uses `_assert_valid_table` for SQL injection prevention when iterating table names.

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract dashboard router (6 endpoints)"
```

---

### Task 10: Extract routers/agent_writes.py (12 endpoints)

**Files:**
- Create: `app/routers/agent_writes.py`
- Modify: `api.py`

**Endpoints to move:**
- `POST /api/agent/invoice` (~L1508)
- `POST /api/agent/estimate` (~L1529)
- `PUT /api/agent/estimate/{id}` (~L1548)
- `POST /api/agent/contact` (~L1571)
- `PUT /api/agent/invoice/{id}/approve` (~L1601)
- `GET /api/agent/contact/{id}` (~L1658)
- `PUT /api/agent/contact/{id}` (~L1718)
- `PUT /api/agent/invoice/{id}/status` (~L1766)
- `POST /api/agent/send/{type}/{id}` (~L1788)
- `POST /api/agent/convert-estimate` (~L1829)

**Pydantic models to move:** `CreateDocumentBody`, `CreateContactBody`, `UpdateStatusBody`, `SendDocumentBody`, `UpdateContactBody`, `ConvertEstimateBody`

**Module-level to move from api.py:**
```python
_USE_GATEWAY = os.getenv("USE_GATEWAY_FOR_AGENT", "true").lower() == "true"
_approve_limiter = RateLimiter()

def _gw_error(result, default="Operation failed"):
    errs = result.get("errors")
    return errs[0].get("msg", default) if errs else default
```

- [ ] **Step 1: Create `app/routers/agent_writes.py`**

Imports needed:
```python
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import connector, os, logging, re as _re_mod, requests
from app.db.connection import db_context
from app.domain.item_builder import build_holded_items, build_holded_items_with_accounts
from write_gateway import gateway, RateLimiter
```

This is the highest-touch router — the Fase 3 migration code lives here. Move carefully, preserving all feature flag logic, rate limiting, and response format adapters.

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract agent_writes router (12 endpoints)"
```

---

### Task 11: Extract routers/ai.py (11 endpoints)

**Files:**
- Create: `app/routers/ai.py`
- Modify: `api.py`

**Endpoints to move:**
- `POST /api/ai/chat` (~L910)
- `POST /api/ai/chat/stream` (~L918)
- `POST /api/ai/confirm` (~L935)
- `GET /api/ai/history` (~L940)
- `DELETE /api/ai/history` (~L944)
- `GET /api/ai/conversations` (~L949)
- `GET /api/ai/favorites` (~L953)
- `POST /api/ai/favorites` (~L961)
- `DELETE /api/ai/favorites/{id}` (~L966)
- `GET /api/ai/config` (~L971)
- `POST /api/ai/config` (~L979)

**Pydantic models to move:** `ChatRequest`, `ConfirmRequest`, `FavoriteRequest`, `AIConfigUpdate`

- [ ] **Step 1: Create `app/routers/ai.py`**

Imports needed:
```python
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
import ai_agent, connector, logging
```

The streaming endpoint returns `StreamingResponse(ai_agent.chat_stream(...), media_type="text/event-stream")`. This must be preserved exactly.

- [ ] **Step 2-4: Register, remove, verify, commit**

```bash
git commit -m "refactor: extract ai router (11 endpoints)"
```

---

### Task 12: Final cleanup and verification

**Files:**
- Modify: `api.py` — final cleanup
- No new files

- [ ] **Step 1: Verify api.py is now ~120 lines**

```bash
wc -l api.py
```
Expected: ~100-130 lines

api.py should contain ONLY:
1. Imports
2. `app = FastAPI()`
3. Auth middleware (~60 lines)
4. CORS middleware
5. Module-level state: `sync_status`, `_sync_lock`, `analysis_status`, `_analysis_lock`
6. Background helpers: `run_sync()`, `run_analysis_job()`, `_claude_categorize()`, `_daily_scheduler()`
7. Startup/shutdown hooks
8. 10x `app.include_router(...)` 
9. Static files mount
10. `GET /` root route

- [ ] **Step 2: Remove dead imports from api.py**

Remove any imports that are no longer used (e.g., `pydantic`, `Field`, `Query`, etc. if all models moved out).

- [ ] **Step 3: Full test suite**

```bash
python3 -m pytest tests/ -v
```
Expected: 88 passed

- [ ] **Step 4: Line count verification**

```bash
wc -l api.py app/routers/*.py
```
Expected: api.py ~120, total routers ~2100, sum ~2215 (matches original)

- [ ] **Step 5: AST parse all files**

```bash
python3 -c "
import ast, glob
for f in ['api.py'] + glob.glob('app/routers/*.py'):
    ast.parse(open(f).read())
    print(f'OK: {f}')
"
```

- [ ] **Step 6: Commit and push**

```bash
git add api.py app/routers/
git commit -m "refactor: final cleanup — api.py reduced from 2215 to ~120 lines (Fase 4)"
git push
```

- [ ] **Step 7: Deploy to Oracle server**

```bash
ssh coyote-server 'cd /opt/holded-connector && git pull && docker compose up -d --build holded-api'
```

- [ ] **Step 8: Smoke test deployed server**

```bash
ssh coyote-server 'curl -s http://localhost:8000/health'
# Test one endpoint from each router:
ssh coyote-server 'curl -s http://localhost:8000/api/jobs -H "Authorization: Bearer $TOKEN" | head -c 100'
ssh coyote-server 'curl -s http://localhost:8000/api/summary -H "Authorization: Bearer $TOKEN" | head -c 100'
ssh coyote-server 'curl -s http://localhost:8000/api/entities/contacts -H "Authorization: Bearer $TOKEN" | head -c 100'
```

---

## Notes

- **Circular imports:** Tasks 6, 7 use lazy imports (`from api import ...`) for shared state. This is acceptable for Commit 1 and gets cleaned up when helpers move to `app/domain/` in Commit 2.
- **No behavior changes:** Every endpoint must produce identical responses. This is a pure structural refactor.
- **Line numbers are approximate:** They shift as earlier tasks remove code. Always search for the function name, not the line number.
- **Holded API rule:** POST uses "items", GET returns "products". This applies to write_gateway.py and connector.py — files we do NOT touch in this plan.
