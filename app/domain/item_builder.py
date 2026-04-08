"""Unified item builder for Holded API payloads.

Consolidates 4 duplicated item-building implementations into 2 public functions:
  - build_holded_items(): gateway + agent create paths
  - build_holded_items_with_accounts(): update_estimate path (with account resolution)

Extracted from api.py (L1494-1632) and write_gateway.py (L149-162) during Fase 2 refactor.
"""

from write_validators import _sanitize_text

# ── Account number → Holded internal ID mapping ─────────────────────────────
# Materialized from api.py:1534-1538
ACCOUNT_IDS = {
    "75200000": "69cccc2bd9d45db3170b99a6",  # Arrendamiento de equipos (Alquiler)
    "75900000": "69cccc2bd9d45db3170b99a8",  # Prestación de servicios (Servicio)
    "70500000": "69cccc2bd9d45db3170b99a4",  # Venta de productos (Producto)
    "70000000": "69cccc2bd9d45db3170b99a3",  # Ventas de mercaderías (default)
}

# Retention % → account number mapping
# Key 0 maps to "70500000" (NOT "70505000" — verified against production data)
RETENTION_TO_ACCOUNT = {
    19: "75200000",   # 19% retention → Alquiler
    15: "75900000",   # 15% retention → Servicio
    0:  "70500000",   # 0% retention (with price > 0) → Producto
}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _build_core_item(item: dict, *, sanitize: bool = True) -> dict:
    """Shared core: name, units, subtotal, optional fields."""
    p = {
        "name": _sanitize_text(item.get("name"), 200) if sanitize else item.get("name", ""),
        "units": item.get("units", 1),
        "subtotal": item.get("price", item.get("subtotal", 0)),
    }
    for field in ("discount", "sku", "serviceId", "productId"):
        if field in item:
            p[field] = item[field]
    if "desc" in item:
        p["desc"] = _sanitize_text(item["desc"], 500) if sanitize else item["desc"]
    return p


def _apply_taxes(p: dict, item: dict, *, apply_default_iva: bool = False) -> dict:
    """Apply tax logic with priority: explicit taxes > retention mapping > tax field > default IVA.

    Retention handling: if item has retention, build a taxes array combining IVA + retention key.
    This matches the proven logic from api.py:1504-1511 (production-verified).
    """
    if "taxes" in item:
        p["taxes"] = item["taxes"]
    elif "retention" in item and item["retention"]:
        ret_key = f"s_ret_{int(item['retention'])}"
        p["taxes"] = [f"s_iva_{item.get('tax', 21)}", ret_key]
        p.pop("tax", None)
    elif "tax" in item:
        p["tax"] = item["tax"]

    # Default: if no taxes specified at all, apply IVA 21%
    if apply_default_iva and "taxes" not in p and "tax" not in p:
        p["taxes"] = ["s_iva_21"]

    return p


def _resolve_account(item: dict):
    """Resolve Holded account ID from item's account number or retention type.

    Priority: explicit account number > retention-based inference > None (Holded default).
    Materialized from api.py:1541-1555.
    """
    acc = item.get("account", "")
    if acc in ACCOUNT_IDS:
        return ACCOUNT_IDS[acc]
    ret = item.get("retention", 0)
    account_num = RETENTION_TO_ACCOUNT.get(ret)
    if account_num:
        # 0% retention only maps to Producto if item has a positive price
        if ret == 0 and item.get("price", item.get("subtotal", 0)) <= 0:
            return None
        return ACCOUNT_IDS[account_num]
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def build_holded_items(items, *, sanitize=True, apply_default_iva=False):
    """Build Holded-compatible item list for create_invoice/create_estimate.

    Used by:
      - write_gateway._build_holded_payload (gateway path)
      - api.py agent_create_invoice / agent_create_estimate (agent path)

    Args:
        items: List of item dicts with name, units, price/subtotal, tax, retention, etc.
        sanitize: Strip HTML and enforce max lengths (True for user-facing, False for internal).
        apply_default_iva: If True, add IVA 21% when no tax/taxes specified.
    """
    return [
        _apply_taxes(_build_core_item(item, sanitize=sanitize), item, apply_default_iva=apply_default_iva)
        for item in items
    ]


def build_holded_items_with_accounts(items, *, sanitize=True):
    """Build items with account resolution for update_estimate/Brain agent paths.

    Same as build_holded_items(apply_default_iva=True) but also resolves
    Holded account IDs from retention type or explicit account number.

    Used by:
      - api.py agent_update_estimate
      - write_gateway update_estimate_items branch
    """
    result = []
    for item in items:
        p = _build_core_item(item, sanitize=sanitize)
        p = _apply_taxes(p, item, apply_default_iva=True)
        account_id = _resolve_account(item)
        if account_id:
            p["account"] = account_id
        result.append(p)
    return result
