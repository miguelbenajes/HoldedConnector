"""High-level Holded write operations."""
import json
import logging
import time
import requests
from app.db.connection import HEADERS, BASE_URL, SAFE_MODE, _num
from app.holded.client import post_data, put_data

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
