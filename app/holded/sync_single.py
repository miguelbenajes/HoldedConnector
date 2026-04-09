"""Single-entity sync: refresh one entity from Holded API. Used by write_gateway sync-back."""
import logging
import requests
from app.db.connection import get_db, release_db, _cursor
from app.holded.client import fetch_data, _extract_project_code
from app.holded.upsert import _upsert_single_document, _upsert_single_contact, _upsert_single_product

logger = logging.getLogger(__name__)


def sync_single_document(doc_type, doc_id):
    """Fetch one document from Holded and upsert into local DB. Returns tables updated."""
    table_map = {
        "invoice":  ("invoices", "invoice_items", "invoice_id"),
        "estimate": ("estimates", "estimate_items", "estimate_id"),
        "purchase": ("purchase_invoices", "purchase_items", "purchase_id"),
    }
    if doc_type not in table_map:
        logger.error(f"Unknown doc_type for sync: {doc_type}")
        return []

    table, items_table, fk_col = table_map[doc_type]
    data = fetch_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}")
    if not data:
        logger.warning(f"Sync-back: no data returned for {doc_type}/{doc_id}")
        return []

    conn = get_db()
    _notify_project_code = None
    _success = False
    try:
        cursor = _cursor(conn)
        _upsert_single_document(cursor, data, table, items_table, fk_col)
        conn.commit()
        _notify_project_code = _extract_project_code(data.get('products'))
        _success = True
    except Exception as e:
        logger.error(f"Sync-back failed for {doc_type}/{doc_id}: {e}")
        conn.rollback()
    finally:
        release_db(conn)

    if not _success:
        return []

    # Notify Brain — non-blocking, after DB transaction is closed
    if _notify_project_code:
        from skills.job_tracker import BRAIN_API_URL, BRAIN_INTERNAL_KEY
        try:
            requests.post(
                f"{BRAIN_API_URL}/internal/job-review",
                json={"project_code": _notify_project_code, "trigger": "holded_sync"},
                headers={"x-api-key": BRAIN_INTERNAL_KEY},
                timeout=5,
            )
        except Exception:
            pass  # Non-critical — cron will pick it up

    return [table, items_table]


def sync_single_contact(contact_id):
    """Fetch one contact from Holded and upsert into local DB."""
    data = fetch_data(f"/invoicing/v1/contacts/{contact_id}")
    if not data:
        return []
    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_contact(cursor, data)
        conn.commit()
        return ["contacts"]
    except Exception as e:
        logger.error(f"Sync-back failed for contact/{contact_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)


def sync_single_product(product_id):
    """Fetch one product from Holded and upsert into local DB."""
    data = fetch_data(f"/invoicing/v1/products/{product_id}")
    if not data:
        return []
    conn = get_db()
    try:
        cursor = _cursor(conn)
        _upsert_single_product(cursor, data)
        conn.commit()
        return ["products"]
    except Exception as e:
        logger.error(f"Sync-back failed for product/{product_id}: {e}")
        conn.rollback()
        return []
    finally:
        release_db(conn)
