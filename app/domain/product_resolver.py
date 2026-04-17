"""Product resolver for the Smart Estimate system.

Handles three distinct item scenarios when building estimate line items:
  1. Catalog lookup  — {"name": "D850"}
     → fuzzy-searches the Holded product catalog and uses the catalog price.
  2. Loose line      — {"name": "Taxi", "price": 45, "item_type": "expense"}
     → bypasses catalog lookup; passed through as-is.
  3. Create-if-missing — {"name": "Aputure", "price": 180, "create_if_missing": true}
     → searches catalog first; creates a new Holded product if not found.

Cache strategy: both /invoicing/v1/products and /invoicing/v1/services are
fetched on first call and cached for 60 seconds.

NOTE: /invoicing/v1/services may not exist on all Holded plan tiers. The fetch
is wrapped in try/except and only produces a warning — the resolver degrades
gracefully to products-only if the endpoint is absent.

Consumed by:
  - smart_estimate.py (orchestrator) to resolve line items before building the payload
"""

import time
import logging
from difflib import SequenceMatcher
from typing import Optional

from app.holded.client import fetch_data, post_data

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────

_CACHE_TTL = 60  # seconds

_products_cache: Optional[list] = None
_cache_fetched_at: float = 0.0


# ── Fuzzy thresholds ──────────────────────────────────────────────────────────

_EXACT_THRESHOLD = 0.80    # Score at or above this → unambiguous match
_FUZZY_MIN = 0.40          # Score below this → ignored for suggestions
_TOP_SUGGESTIONS = 5       # Max suggestions on ambiguous/not-found


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _get_all_products() -> list:
    """Return all Holded catalog items (products + services), cached 60s.

    Merges results from:
      - /invoicing/v1/products  — physical products (always available)
      - /invoicing/v1/services  — service items (may not exist on all plans)

    The /invoicing/v1/services endpoint is optional. If it returns an error or
    is absent, a warning is logged and the resolver continues with products only.
    """
    global _products_cache, _cache_fetched_at

    now = time.monotonic()
    if _products_cache is not None and (now - _cache_fetched_at) < _CACHE_TTL:
        return _products_cache

    logger.info("product_resolver: refreshing product cache from Holded API")

    # Always fetch products — required
    try:
        products = fetch_data("/invoicing/v1/products")
        if not isinstance(products, list):
            logger.warning("product_resolver: /invoicing/v1/products returned non-list: %s", type(products))
            products = []
    except Exception as exc:
        logger.error("product_resolver: failed to fetch products: %s", exc)
        products = []

    # Optionally fetch services — graceful degradation if endpoint absent
    # NOTE: /invoicing/v1/services is not available on all Holded plan tiers.
    # If it fails (404, 403, exception), we log a warning and proceed without it.
    services = []
    try:
        result = fetch_data("/invoicing/v1/services")
        if isinstance(result, list):
            services = result
        else:
            logger.warning(
                "product_resolver: /invoicing/v1/services returned non-list (%s) — skipping services",
                type(result),
            )
    except Exception as exc:
        logger.warning(
            "product_resolver: /invoicing/v1/services unavailable (%s) — continuing without services",
            exc,
        )

    combined = products + services
    _products_cache = combined
    _cache_fetched_at = now
    logger.info(
        "product_resolver: cached %d items (%d products, %d services)",
        len(combined), len(products), len(services),
    )
    return _products_cache


def clear_products_cache() -> None:
    """Invalidate the product cache, forcing a fresh fetch on next call."""
    global _products_cache, _cache_fetched_at
    _products_cache = None
    _cache_fetched_at = 0.0
    logger.debug("product_resolver: cache cleared")


# ── Fuzzy matching helpers ────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase and strip for consistent fuzzy comparison."""
    return (text or "").lower().strip()


def _score_product(query: str, product: dict) -> float:
    """Compute match score between query and a product dict.

    Scoring rules (highest applies):
    1. Exact name match              → 1.0
    2. Query is substring of name    → 0.92 boost (contains match)
    3. Name is substring of query    → 0.88 boost (reverse contains)
    4. SequenceMatcher ratio         → direct value

    Args:
        query:   Normalized query string.
        product: Raw Holded product dict.

    Returns:
        Float in [0.0, 1.0].
    """
    name = _normalize(product.get("name", ""))
    sku = _normalize(product.get("sku", ""))

    if not name:
        return 0.0

    # Exact match
    if query == name or (sku and query == sku):
        return 1.0

    # Contains boost — query found inside product name
    if len(query) >= 4 and query in name:
        return 0.92

    # Reverse contains — product name found inside query
    if len(name) >= 4 and name in query:
        return 0.88

    # Token overlap — all query words appear in product name
    # Handles "Sigma 24-70" matching "Sigma Art 24-70mm f2.8"
    query_tokens = set(query.split())
    name_tokens = set(name.split())
    if len(query_tokens) >= 2 and query_tokens.issubset(name_tokens):
        return 0.90

    # Partial token overlap — check if key tokens match (ignoring suffixes like "mm", "f2.8")
    matched = sum(1 for qt in query_tokens if any(qt in nt or nt.startswith(qt) for nt in name_tokens))
    if len(query_tokens) >= 2 and matched == len(query_tokens):
        return 0.85

    return SequenceMatcher(None, query, name).ratio()


def _find_best_match(query: str, products: list) -> tuple:
    """Find the best matching product for a query string.

    Args:
        query:    Raw (un-normalized) search string.
        products: Full catalog list from _get_all_products().

    Returns:
        (best_product_dict, best_score_float) or (None, 0.0) if empty catalog.
    """
    if not products:
        return None, 0.0

    q = _normalize(query)
    best_product = None
    best_score = 0.0

    for product in products:
        score = _score_product(q, product)
        if score > best_score:
            best_score = score
            best_product = product

    return best_product, best_score


def _find_similar(query: str, products: list, limit: int = _TOP_SUGGESTIONS) -> list:
    """Return up to `limit` products most similar to query, sorted by score desc.

    Only includes products scoring >= _FUZZY_MIN. Used to populate error
    suggestions when the best match is below the confidence threshold.

    Args:
        query:    Raw search string.
        products: Full catalog list.
        limit:    Maximum number of results to return.

    Returns:
        List of dicts: [{"id": ..., "name": ..., "price": ..., "_score": ...}, ...]
    """
    q = _normalize(query)
    scored = [
        (p, _score_product(q, p))
        for p in products
    ]
    scored = [(p, s) for p, s in scored if s >= _FUZZY_MIN]
    scored.sort(key=lambda x: x[1], reverse=True)

    return [
        {
            "id":     p.get("id", ""),
            "name":   p.get("name", ""),
            "price":  p.get("price", 0),
            "sku":    p.get("sku", ""),
            "_score": round(s, 3),
        }
        for p, s in scored[:limit]
    ]


# ── Product creation ──────────────────────────────────────────────────────────

def _create_product(name: str, price: float) -> dict:
    """Create a new simple product in Holded and invalidate the local cache.

    Args:
        name:  Product name (required by Holded API).
        price: Unit price (EUR, excl. VAT).

    Returns:
        On success: {"ok": True, "product_id": "...", "name": name, "price": price}
        On failure: {"ok": False, "error": "create_failed", "detail": "..."}
    """
    payload = {
        "name":  name.strip(),
        "price": price,
        "kind":  "simple",
    }
    response = post_data("/invoicing/v1/products", payload)

    if response.get("error"):
        logger.error("product_resolver: create_product failed for '%s': %s", name, response)
        return {
            "ok":     False,
            "error":  "create_failed",
            "detail": response.get("detail", str(response)),
        }

    new_id = response.get("id", "")
    logger.info("product_resolver: created new product '%s' id=%s", name, new_id)

    # Invalidate cache so subsequent resolves see the new product
    clear_products_cache()

    return {"ok": True, "product_id": new_id, "name": name, "price": price}


# ── Per-item resolution ───────────────────────────────────────────────────────

def _resolve_single(item: dict, products: list) -> dict:
    """Resolve a single input item dict to a resolved line-item dict.

    Item scenarios:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │ Scenario A — Catalog lookup (no price, no item_type=expense, no        │
    │              create_if_missing):                                        │
    │   Input : {"name": "D850"}                                              │
    │   Output: resolved catalog entry with catalog price + product id        │
    │                                                                         │
    │ Scenario B — Loose line item (has price AND item_type=expense):         │
    │   Input : {"name": "Taxi", "price": 45, "item_type": "expense"}         │
    │   Output: passed through unchanged (no catalog lookup)                  │
    │                                                                         │
    │ Scenario C — Create-if-missing (create_if_missing=True):               │
    │   Input : {"name": "Aputure", "price": 180, "create_if_missing": True} │
    │   Output: catalog match if found, else new product created              │
    └─────────────────────────────────────────────────────────────────────────┘

    Returns a dict with "ok" key. On success, includes at least:
      name, price, product_id (when resolved from catalog or created).
    On failure, includes "error" and "suggestions" (if applicable).
    """
    name = (item.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "empty_name", "item": item}

    price = item.get("price")
    item_type = item.get("item_type", "")
    create_if_missing = item.get("create_if_missing", False)

    # ── Scenario B: Loose line item ──────────────────────────────────────────
    # Condition: explicit price + item_type specified.
    # Any item with both price AND item_type is a loose line — no catalog lookup.
    # Covers: expense (taxi), service (fee), rental (equipment not in catalog), passthrough.
    if price is not None and item_type in ("expense", "passthrough", "service", "rental"):
        logger.debug("product_resolver: loose line item '%s' (type=%s)", name, item_type)
        return {
            "ok":        True,
            "name":      name,
            "units":     item.get("units", 1),
            "price":     price,
            "item_type": item_type,
            "_scenario": "loose",
        }

    # ── Scenarios A + C: Catalog search ─────────────────────────────────────
    best_product, best_score = _find_best_match(name, products)

    if best_product is not None and best_score >= _EXACT_THRESHOLD:
        logger.info(
            "product_resolver: '%s' → '%s' (score=%.2f)",
            name, best_product.get("name"), best_score,
        )
        return {
            "ok":         True,
            "name":       best_product.get("name", name),
            "units":      item.get("units", 1),
            "price":      best_product.get("price", price or 0),
            "item_type":  "rental",  # catalog products are rental equipment
            "product_id": best_product.get("id", ""),
            "sku":        best_product.get("sku", ""),
            "_score":     round(best_score, 3),
            "_scenario":  "catalog",
        }

    # ── Below threshold: create or fail ─────────────────────────────────────
    if create_if_missing:
        logger.info(
            "product_resolver: '%s' not found (score=%.2f), creating new product",
            name, best_score,
        )
        create_result = _create_product(name, price or 0)
        if create_result["ok"]:
            return {
                "ok":         True,
                "name":       name,
                "units":      item.get("units", 1),
                "price":      price or 0,
                "item_type":  "rental",
                "product_id": create_result["product_id"],
                "_scenario":  "created",
            }
        # Creation failed — surface error
        return create_result

    # ── Not found, no fallback ───────────────────────────────────────────────
    suggestions = _find_similar(name, products)
    logger.info(
        "product_resolver: '%s' not matched (score=%.2f), %d suggestions",
        name, best_score, len(suggestions),
    )
    return {
        "ok":          False,
        "error":       "not_found",
        "query":       name,
        "suggestions": suggestions,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_products(inputs: list) -> dict:
    """Resolve a list of input item dicts to resolved line items.

    Iterates over each input, applies _resolve_single, and collects results.
    If any item fails to resolve, the overall response is {"ok": False} and
    the "errors" list contains the failed items with their error details.
    Successful items are always included in "items".

    Args:
        inputs: List of item dicts. Each dict must have at least "name".
                Optional keys: price, item_type, create_if_missing.

    Returns:
        {
            "ok": True,
            "items": [resolved_item, ...]          # all resolved successfully
        }
        or:
        {
            "ok": False,
            "items": [resolved_item, ...],         # items that DID resolve
            "errors": [{"query": ..., ...}, ...]   # items that DID NOT resolve
        }

    Example:
        result = resolve_products([
            {"name": "D850"},
            {"name": "Taxi", "price": 45, "item_type": "expense"},
            {"name": "Aputure", "price": 180, "create_if_missing": True},
        ])
        if result["ok"]:
            items = result["items"]
    """
    if not inputs:
        return {"ok": True, "items": []}

    # Fetch once — all single-item resolutions share the same snapshot
    products = _get_all_products()

    resolved_items = []
    errors = []

    for item in inputs:
        result = _resolve_single(item, products)
        if result.get("ok"):
            resolved_items.append(result)
        else:
            errors.append(result)

    if errors:
        logger.warning(
            "product_resolver: %d/%d items failed to resolve",
            len(errors), len(inputs),
        )
        return {"ok": False, "items": resolved_items, "errors": errors}

    return {"ok": True, "items": resolved_items}
