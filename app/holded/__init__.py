"""Holded API module — HTTP client, upsert helpers, and bulk sync engine.

Re-exports the public surface for convenient imports:
    from app.holded import fetch_data, sync_invoices
"""

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
from app.holded.upsert import (
    _upsert_single_document,
    _upsert_single_contact,
    _upsert_single_product,
)
