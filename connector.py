"""Holded Connector — thin facade + business logic.

DB primitives (get_db, _cursor, _q, etc.) live in app.db.connection.
Schema (init_db) lives in app.db.schema.
Settings (reload_config, get/save_setting) live in app.db.settings.

This module re-exports everything for backwards compatibility:
    import connector
    connector.get_db()      # works
    connector.API_KEY       # works (via __getattr__ for mutables)
    connector.init_db()     # works
"""

import os
import json
import logging
import requests
import time

# ── DB layer imports (canonical location: app.db.*) ──────────────────────────
import app.db.connection as _conn
import app.db.schema as _schema
import app.db.settings as _settings

logger = logging.getLogger(__name__)

# ── Function re-exports (safe — functions look up globals at call time) ──────
get_db = _conn.get_db
release_db = _conn.release_db
_cursor = _conn._cursor
_q = _conn._q
_num = _conn._num
_row_val = _conn._row_val
_fetch_one_val = _conn._fetch_one_val
insert_audit_log = _conn.insert_audit_log
update_audit_log = _conn.update_audit_log
db_context = _conn.db_context

init_db = _schema.init_db

reload_config = _settings.reload_config
get_setting = _settings.get_setting
save_setting = _settings.save_setting

# ── Shared-reference globals (used by business functions in this file) ───────
# HEADERS is a dict — shared reference, mutations from reload_config propagate.
# BASE_URL, DB_NAME, _USE_SQLITE, SAFE_MODE are immutable after init — snapshots are safe.
# API_KEY can change via reload_config → accessed through __getattr__ by external callers.
HEADERS = _conn.HEADERS
BASE_URL = _conn.BASE_URL
DB_NAME = _conn.DB_NAME
_USE_SQLITE = _conn._USE_SQLITE
SAFE_MODE = _conn.SAFE_MODE

# ── Module-level __getattr__ for mutable globals (external callers) ──────────
# When other modules do `connector.API_KEY`, this resolves to the canonical
# value in connection.py, even after reload_config() changes it.
# _USE_SQLITE and SAFE_MODE are module-level snapshots (never change), so they
# don't need __getattr__. API_KEY and DATABASE_URL can change via reload_config.
_MUTABLE_CONN_ATTRS = ('API_KEY', 'DATABASE_URL', '_pool')

def __getattr__(name):
    """Proxy mutable globals to their canonical module."""
    if name in _MUTABLE_CONN_ATTRS:
        return getattr(_conn, name)
    raise AttributeError(f"module 'connector' has no attribute {name!r}")


# ── Holded-specific constants + HTTP client (canonical: app.holded.client) ───
from app.holded.client import (
    fetch_data,
    post_data,
    put_data,
    delete_data,
    holded_put,
    extract_ret,
    _extract_project_code,
    _extract_shooting_dates,
    PROYECTO_PRODUCT_ID,
    PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID,
    SHOOTING_DATES_PRODUCT_NAME,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Business logic (Holded API sync, data access, amortizations, analysis)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Bulk sync functions (canonical: app.holded.sync) ────────────────────────
from app.holded.sync import (
    sync_documents,
    sync_invoices,
    sync_purchases,
    sync_estimates,
    sync_accounts,
    sync_products,
    sync_contacts,
    sync_projects,
    sync_payments,
)

# ---------------------------------------------------------------------------
# Single-entity sync helpers — canonical: app.holded.sync_single
# ---------------------------------------------------------------------------
from app.holded.sync_single import sync_single_document, sync_single_contact, sync_single_product


# ── Write wrappers (canonical: app.holded.write_wrappers) ───────────────────
from app.holded.write_wrappers import (
    create_invoice,
    update_estimate,
    fetch_estimate_fresh,
    create_contact,
    create_estimate,
    send_document,
    create_product,
)

# ── File management (canonical: app.domain.file_management) ────────────────
from app.domain.file_management import (
    get_uploads_dir,
    get_reports_dir,
    set_uploads_dir,
    set_reports_dir,
    list_uploaded_files,
)

# ── Purchase analysis (canonical: app.domain.purchase_analysis) ─────────────
from app.domain.purchase_analysis import (
    CATEGORY_RULES,
    categorize_by_rules,
    get_unanalyzed_purchases,
    save_purchase_analysis,
    get_analyzed_invoices,
    get_analysis_stats,
)

# ── Inventory matching (canonical: app.domain.inventory_matching) ──────────
from app.domain.inventory_matching import (
    find_inventory_in_purchases,
    save_inventory_match,
    get_pending_matches,
    confirm_inventory_match,
)

# ── Amortization tracking (canonical: app.domain.amortization) ─────────────
from app.domain.amortization import (
    _recalc_purchase_price,
    get_amortization_purchases,
    add_amortization_purchase,
    update_amortization_purchase,
    delete_amortization_purchase,
    get_product_type_rules,
    get_pack_info,
    get_amortizations,
    add_amortization,
    update_amortization,
    delete_amortization,
    get_amortization_summary,
)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not _conn.API_KEY:
        logger.error("HOLDED_API_KEY not found in .env file.")
    else:
        if _conn.SAFE_MODE:
            logger.warning("SAFE MODE (DRY RUN) ACTIVE - No data will be modified in Holded")

        init_db()
        sync_contacts()
        sync_products()
        sync_invoices()
        sync_estimates()
        sync_purchases()
        sync_projects()
        sync_payments()

        # Flush pending job notes to Obsidian
        try:
            from skills.job_tracker import flush_note_queue
            count = flush_note_queue()
            if count:
                logger.info(f"[JOB_TRACKER] Flushed {count} job notes to Obsidian")
        except Exception as e:
            logger.error(f"[JOB_TRACKER] Queue flush failed: {e}")

        logger.info("Synchronization complete!")
