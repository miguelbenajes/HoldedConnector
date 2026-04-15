"""Contact resolver for the Smart Estimate system.

Provides fuzzy search, by-ID lookup, fiscal validation, and a 60-second TTL
cache over Holded contacts. Single entry points: resolve_contact() and
resolve_contact_by_id(). All other functions are internal.

Cache strategy: _contacts_cache holds the full contact list fetched from
/invoicing/v1/contacts. It is invalidated on TTL expiry or by clear_contacts_cache().

Consumed by:
  - smart_estimate.py (orchestrator) to validate and fill contact fields
  - estimate router when creating estimates from natural language input
"""

import time
import logging
from difflib import SequenceMatcher
from typing import Optional

from app.holded.client import fetch_data

logger = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────────────────

_CACHE_TTL = 60  # seconds

_contacts_cache: Optional[list] = None
_cache_fetched_at: float = 0.0


# ── Fiscal fields expected for a valid invoice contact ───────────────────────
# These are the fields Holded requires to issue a legal document.
_FISCAL_REQUIRED_FIELDS = [
    "name",
    "vatnumber",
    "address",
    "city",
    "postalCode",
    "country",
]


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _get_all_contacts() -> list:
    """Return all Holded contacts, using the 60-second in-process cache.

    On cache miss (or after TTL expiry), fetches /invoicing/v1/contacts.
    Returns an empty list if the API call fails.
    """
    global _contacts_cache, _cache_fetched_at

    now = time.monotonic()
    if _contacts_cache is not None and (now - _cache_fetched_at) < _CACHE_TTL:
        return _contacts_cache

    logger.info("contact_resolver: refreshing contacts cache from Holded API")
    try:
        data = fetch_data("/invoicing/v1/contacts")
        _contacts_cache = data if isinstance(data, list) else []
        _cache_fetched_at = now
        logger.info("contact_resolver: cached %d contacts", len(_contacts_cache))
    except Exception as exc:
        logger.error("contact_resolver: failed to fetch contacts: %s", exc)
        # Return stale cache if available, empty list otherwise
        _contacts_cache = _contacts_cache or []

    return _contacts_cache


def clear_contacts_cache() -> None:
    """Invalidate the contacts cache, forcing a fresh fetch on next call."""
    global _contacts_cache, _cache_fetched_at
    _contacts_cache = None
    _cache_fetched_at = 0.0
    logger.debug("contact_resolver: cache cleared")


# ── Normalization ─────────────────────────────────────────────────────────────

def _build_contact_result(raw: dict) -> dict:
    """Normalize a raw Holded contact into the resolver output shape.

    Extracts the canonical fields used by the Smart Estimate system:
    id, name, trading name, VAT, address, city, postal code, country,
    contact type, and email.
    """
    # Holded stores the primary billing address inside the 'billing' block;
    # fall back to top-level fields when the sub-block is absent.
    billing = raw.get("billing") or {}

    return {
        "id":         raw.get("id", ""),
        "name":       raw.get("name", "").strip(),
        "tradeName":  raw.get("tradeName", "").strip(),
        "vatnumber":  raw.get("vatnumber", "").strip(),
        "address":    billing.get("address") or raw.get("address", ""),
        "city":       billing.get("city") or raw.get("city", ""),
        "postalCode": billing.get("postalCode") or raw.get("postalCode", ""),
        "country":    billing.get("country") or raw.get("country", ""),
        "type":       raw.get("type", ""),       # 'client' | 'supplier' | 'both'
        "email":      raw.get("email", ""),
    }


# ── Fuzzy helpers ─────────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio between two strings (case-insensitive)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _score_contact(query: str, contact: dict) -> float:
    """Score a contact against the search query.

    Checks both 'name' and 'tradeName'. Returns the highest score found.
    Exact match on either field returns 1.0.
    """
    q = query.lower().strip()
    name = contact.get("name", "").lower().strip()
    trade = contact.get("tradeName", "").lower().strip()

    # Exact match wins immediately
    if q == name or q == trade:
        return 1.0

    best = max(
        _similarity(q, name) if name else 0.0,
        _similarity(q, trade) if trade else 0.0,
    )
    return best


# ── Public API ────────────────────────────────────────────────────────────────

_EXACT_THRESHOLD = 0.85   # Score at or above this → unambiguous match
_FUZZY_THRESHOLD = 0.50   # Score below this → ignored
_TOP_MATCHES = 5           # Suggestions returned when score < _EXACT_THRESHOLD


def resolve_contact(client_name: str) -> dict:
    """Resolve a client name to a unique Holded contact.

    Three possible outcomes:
    1. Exact match (score >= 0.85):
       {"ok": True, "contact": {...normalized contact...}}
    2. Multiple candidates (best score in [0.50, 0.85)):
       {"ok": False, "error": "ambiguous", "matches": [...top 5...]}
    3. No match (best score < 0.50 or empty catalog):
       {"ok": False, "error": "not_found", "query": client_name}

    Args:
        client_name: Free-text client name as provided by the user.

    Returns:
        Dict with 'ok' key and either 'contact' or error fields.
    """
    if not client_name or not client_name.strip():
        return {"ok": False, "error": "empty_query"}

    contacts = _get_all_contacts()
    if not contacts:
        logger.warning("contact_resolver: contact catalog is empty")
        return {"ok": False, "error": "catalog_empty"}

    # Score all contacts
    scored = [
        (contact, _score_contact(client_name, contact))
        for contact in contacts
    ]

    # Sort descending by score
    scored.sort(key=lambda x: x[1], reverse=True)

    best_contact, best_score = scored[0]

    if best_score >= _EXACT_THRESHOLD:
        logger.info(
            "contact_resolver: matched '%s' → '%s' (score=%.2f)",
            client_name, best_contact.get("name"), best_score,
        )
        return {"ok": True, "contact": _build_contact_result(best_contact)}

    # Gather candidates above the minimum fuzzy threshold
    candidates = [
        {
            **_build_contact_result(c),
            "_score": round(score, 3),
        }
        for c, score in scored[:_TOP_MATCHES]
        if score >= _FUZZY_THRESHOLD
    ]

    if not candidates:
        logger.info(
            "contact_resolver: no match for '%s' (best=%.2f)", client_name, best_score
        )
        return {"ok": False, "error": "not_found", "query": client_name}

    logger.info(
        "contact_resolver: ambiguous match for '%s' — %d candidates (best=%.2f)",
        client_name, len(candidates), best_score,
    )
    return {
        "ok":      False,
        "error":   "ambiguous",
        "query":   client_name,
        "matches": candidates,
    }


def resolve_contact_by_id(client_id: str) -> dict:
    """Resolve a Holded contact directly by its internal ID.

    Useful when the caller already knows the Holded contact ID and just
    needs the normalized structure (e.g., for fiscal validation).

    Args:
        client_id: Holded contact UUID string.

    Returns:
        {"ok": True, "contact": {...}} or {"ok": False, "error": "not_found", "id": client_id}
    """
    if not client_id or not client_id.strip():
        return {"ok": False, "error": "empty_id"}

    contacts = _get_all_contacts()
    for contact in contacts:
        if contact.get("id") == client_id.strip():
            return {"ok": True, "contact": _build_contact_result(contact)}

    logger.info("contact_resolver: ID '%s' not found in catalog", client_id)
    return {"ok": False, "error": "not_found", "id": client_id}


def validate_contact_fiscal(contact: dict) -> list:
    """Check that a normalized contact has all required fiscal fields populated.

    'Populated' means the field is present and non-empty after stripping whitespace.

    Args:
        contact: Normalized contact dict (output of _build_contact_result).

    Returns:
        List of missing field names. Empty list means the contact is fiscally complete.

    Example:
        missing = validate_contact_fiscal(contact)
        if missing:
            raise ValueError(f"Contact missing fiscal fields: {missing}")
    """
    missing = []
    for field in _FISCAL_REQUIRED_FIELDS:
        value = contact.get(field, "")
        if not str(value).strip():
            missing.append(field)
    return missing
