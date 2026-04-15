"""High-level Holded write operations."""
import hashlib
import json
import logging
import os
import re
import time
import requests
from app.db.connection import HEADERS, BASE_URL, SAFE_MODE, _num
from app.holded.client import post_data, put_data, post_multipart, fetch_data

# Holded MongoDB ObjectId: 24-char hex string
_HOLDED_ID_RE = re.compile(r'^[a-f0-9]{24}$')

logger = logging.getLogger(__name__)


def create_invoice(invoice_data):
    logger.info(f"Creating invoice for contact {invoice_data.get('contactId')}...")
    result = post_data("/invoicing/v1/documents/invoice", invoice_data)
    if result and not result.get("error") and result.get('status') == 1:
        logger.info(f"Invoice created successfully: {result.get('id')}")
        return result.get('id')
    if result and result.get("error"):
        return result
    return None


def update_estimate(estimate_id, estimate_data):
    logger.info(f"Updating estimate {estimate_id}...")
    result = put_data(f"/invoicing/v1/documents/estimate/{estimate_id}", estimate_data)
    if result and result.get('status') == 1:
        logger.info(f"Estimate {estimate_id} updated successfully.")
        return True
    return False


def fetch_estimate_fresh(estimate_id):
    """Read estimate items directly from Holded API (not local DB).
    Used by Brain executor and verifier to avoid stale cache."""
    endpoint = f"/invoicing/v1/documents/estimate/{estimate_id}"
    max_retries = 3
    for attempt in range(max_retries):
        try:
            url = f"{BASE_URL}{endpoint}"
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 200:
                data = response.json()
                raw_items = data.get("products") or []
                items = []
                for it in raw_items:
                    item = {
                        "name": it.get("name", ""),
                        "units": _num(it.get("units")) or 1,
                        "subtotal": _num(it.get("price")) or _num(it.get("subtotal")) or 0,
                        "price": _num(it.get("price")) or _num(it.get("subtotal")) or 0,
                        "desc": it.get("desc", ""),
                        "productId": it.get("productId", ""),
                        "product_id": it.get("productId", ""),
                    }
                    if it.get("tax") is not None:
                        item["tax"] = it["tax"]
                    if it.get("taxes"):
                        item["taxes"] = it["taxes"]
                    if it.get("retention"):
                        item["retention"] = it["retention"]
                    if it.get("discount"):
                        item["discount"] = it["discount"]
                    if it.get("account"):
                        item["account"] = it["account"]
                    items.append(item)
                logger.info(f"Fresh read estimate {estimate_id}: {len(items)} items from Holded API")
                return items
            elif response.status_code == 429:
                wait = 2 * (attempt + 1)
                logger.warning(f"Rate limit (429) reading estimate {estimate_id}, waiting {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            else:
                logger.error(f"Holded API error reading estimate {estimate_id}: {response.status_code} - {response.text[:200]}")
                raise Exception(f"Holded API error: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            raise Exception(f"Holded API timeout reading estimate {estimate_id}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error reading estimate {estimate_id}: {e}")
    raise Exception(f"Failed to read estimate {estimate_id} after {max_retries} retries")


def create_contact(contact_data):
    logger.info(f"Creating contact {contact_data.get('name')}...")
    result = post_data("/invoicing/v1/contacts", contact_data)
    if result and not result.get("error") and result.get('status') == 1:
        logger.info(f"Contact created successfully: {result.get('id')}")
        return result.get('id')
    if result and result.get("error"):
        return result
    return None


def create_estimate(estimate_data):
    logger.info(f"Creating estimate for contact {estimate_data.get('contactId')}...")
    logger.info(f"[ESTIMATE PAYLOAD] {json.dumps(estimate_data)}")
    result = post_data("/invoicing/v1/documents/estimate", estimate_data)
    logger.info(f"[ESTIMATE RESULT] {result}")
    if result and result.get('status') == 1:
        logger.info(f"Estimate created successfully: {result.get('id')}")
        return result.get('id')
    return result.get('id') if result else None


def send_document(doc_type, doc_id, send_data=None):
    logger.info(f"Sending {doc_type} {doc_id}...")
    payload = send_data or {}
    result = post_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}/send", payload)
    if result:
        logger.info(f"Document {doc_id} sent successfully.")
        return True
    return False


def create_product(product_data):
    logger.info(f"Creating product {product_data.get('name')}...")
    result = post_data("/invoicing/v1/products", product_data)
    if result and result.get('status') == 1:
        logger.info(f"Product created successfully: {result.get('id')}")
        return result.get('id')
    return None


# ── Purchase (expense) operations ──────────────────────────────────────────

def create_purchase(purchase_data):
    """Create a purchase (supplier invoice / expense) in Holded.

    Always created as draft. Never approveDoc.
    Triggers sync-back after creation.

    purchase_data: {
        contactId: str,             # existing supplier ID
        contactName: str,           # OR supplier name (creates new if needed)
        date: int (unix timestamp),
        dueDate: int (optional),
        desc: str (optional),
        notes: str (optional),
        items: [{name, units, subtotal, tax, desc?, account?}],
        tags: [str] (optional),
    }

    Returns: purchase ID (str) on success, dict with 'error' on failure, None on unknown error.
    """
    contact_info = purchase_data.get('contactId') or purchase_data.get('contactName', 'unknown')
    logger.info(f"Creating purchase for {contact_info}...")
    logger.info(f"[PURCHASE PAYLOAD] {json.dumps(purchase_data)}")
    result = post_data("/invoicing/v1/documents/purchase", purchase_data)
    logger.info(f"[PURCHASE RESULT] {result}")
    if result and not result.get("error") and result.get('status') == 1:
        purchase_id = result.get('id')
        logger.info(f"Purchase created successfully: {purchase_id}")
        return purchase_id
    if result and result.get("error"):
        return result
    return None


# ── Document attachment operations ─────────────────────────────────────────

# Allowed MIME types for document attachments
ALLOWED_ATTACH_TYPES = {"image/jpeg", "image/png", "application/pdf"}
# Max file size: 20MB (confirmed by probe 2026-04-11)
MAX_ATTACH_SIZE = 20 * 1024 * 1024


def attach_file_to_document(doc_type, doc_id, filename, file_bytes, content_type):
    """Attach a file to a Holded document.

    Uses POST /invoicing/v1/documents/{docType}/{docId}/attach (multipart/form-data).

    IMPORTANT (probe 2026-04-11):
    - Endpoint is /attach (NOT /attachments)
    - Response: 201 {"status": 1, "info": "Attachment uploaded"} — NO attachment ID
    - No list/delete API available — fire-and-forget
    - Attachments visible only in Holded web UI

    Args:
        doc_type: invoice, estimate, or purchase
        doc_id: 24-char hex Holded document ID
        filename: Original filename
        file_bytes: Raw file content
        content_type: MIME type

    Returns: dict with result or {'error': True, ...}
    """
    # Validate doc_type
    allowed_doc_types = {"invoice", "estimate", "purchase", "creditnote", "proform"}
    if doc_type not in allowed_doc_types:
        return {"error": True, "detail": f"Invalid doc_type '{doc_type}'. Allowed: {allowed_doc_types}"}

    # Validate doc_id format — prevent path injection into Holded API URL
    if not _HOLDED_ID_RE.match(str(doc_id or "")):
        return {"error": True, "detail": "Invalid doc_id (must be 24-char hex Holded ID)"}

    # Validate content type
    if content_type not in ALLOWED_ATTACH_TYPES:
        return {"error": True, "detail": f"File type '{content_type}' not allowed. Allowed: {ALLOWED_ATTACH_TYPES}"}

    # Validate file size
    if len(file_bytes) > MAX_ATTACH_SIZE:
        mb = len(file_bytes) / (1024 * 1024)
        return {"error": True, "detail": f"File too large ({mb:.1f}MB). Max: {MAX_ATTACH_SIZE // (1024*1024)}MB"}

    # Validate magic bytes
    if not _validate_file_magic(file_bytes, content_type):
        return {"error": True, "detail": f"File content doesn't match declared type '{content_type}'"}

    # Sanitize filename
    safe_filename = _sanitize_filename(filename)

    logger.info(f"Attaching {safe_filename} ({content_type}, {len(file_bytes)} bytes) to {doc_type} {doc_id}")

    endpoint = f"/invoicing/v1/documents/{doc_type}/{doc_id}/attach"
    result = post_multipart(endpoint, file_bytes, safe_filename, content_type)

    if result and not result.get("error"):
        logger.info(f"File attached successfully to {doc_type} {doc_id}")
        # Compute hash for local tracking
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        result["file_hash"] = file_hash
        result["filename"] = safe_filename
    else:
        logger.error(f"Failed to attach file to {doc_type} {doc_id}: {result}")

    return result


def compute_file_hash(file_bytes):
    """Compute SHA-256 hash of file content for duplicate detection."""
    return hashlib.sha256(file_bytes).hexdigest()


def _validate_file_magic(file_bytes, content_type):
    """Validate file content matches declared MIME type via magic bytes."""
    if len(file_bytes) < 4:
        return False

    magic_map = {
        "image/jpeg": [b'\xff\xd8\xff'],
        "image/png": [b'\x89PNG'],
        "application/pdf": [b'%PDF'],
    }

    expected = magic_map.get(content_type)
    if not expected:
        return False

    return any(file_bytes.startswith(m) for m in expected)


def _sanitize_filename(filename):
    """Sanitize filename: strip path, limit chars, alphanumeric + safe chars only."""
    # Strip directory components
    name = os.path.basename(filename)
    # Keep only safe characters
    name = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    # Limit length
    if len(name) > 100:
        base, ext = os.path.splitext(name)
        name = base[:100 - len(ext)] + ext
    return name or "attachment"
