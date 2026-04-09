"""Sync runner — orchestrates full Holded data sync.

Runs each sync step sequentially and tracks status for the API.
"""

import logging
import threading
from datetime import datetime

import connector

logger = logging.getLogger(__name__)

# ── Status tracking (thread-safe) ──────────────────────────────────────────
sync_status = {"running": False, "last_result": None, "last_time": None, "errors": []}
_sync_lock = threading.Lock()


def run_sync():
    """Run full sync from Holded API to local DB."""
    sync_status["errors"] = []
    try:
        connector.init_db()
        steps = [
            ("accounts", connector.sync_accounts),
            ("contacts", connector.sync_contacts),
            ("products", connector.sync_products),
            ("invoices", connector.sync_invoices),
            ("purchases", connector.sync_purchases),
            ("estimates", connector.sync_estimates),
            ("projects", connector.sync_projects),
            ("payments", connector.sync_payments),
        ]
        for step_name, step_fn in steps:
            try:
                step_fn()
            except Exception as e:
                logger.error(f"Sync step '{step_name}' failed: {e}", exc_info=True)
                sync_status["errors"].append(f"{step_name} failed")

        # Flush pending job notes to Obsidian
        try:
            from skills.job_tracker import flush_note_queue
            count = flush_note_queue()
            if count:
                logger.info(f"[JOB_TRACKER] Flushed {count} job notes to Obsidian")
        except Exception as e:
            logger.error(f"[JOB_TRACKER] Queue flush failed: {e}")
    finally:
        sync_status["running"] = False
        sync_status["last_time"] = datetime.now().isoformat()
        sync_status["last_result"] = "error" if sync_status["errors"] else "success"
