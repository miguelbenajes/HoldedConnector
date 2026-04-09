"""Holded API HTTP client. All external API calls go through here.

Handles: GET with pagination+retry, POST/PUT/DELETE with SAFE_MODE,
and Holded-specific constants (special product IDs used for project tracking).

Extracted from connector.py during Fase 5a refactor.
"""
import os
import json
import time
import logging
import requests
from app.db.connection import HEADERS, BASE_URL, SAFE_MODE

logger = logging.getLogger(__name__)

# ── Holded-specific constants (used by sync functions) ───────────────────────
PROYECTO_PRODUCT_ID = os.getenv("HOLDED_PROYECTO_PRODUCT_ID", "69b2b35f75ae381d8f05c133")
PROYECTO_PRODUCT_NAME = "proyect ref:"
SHOOTING_DATES_PRODUCT_ID = os.getenv("HOLDED_SHOOTING_DATES_PRODUCT_ID", "69b2cfcd0df77ff4010e4ac8")
SHOOTING_DATES_PRODUCT_NAME = "shooting dates:"


def extract_ret(prod):
    r = prod.get('retention')
    if r is not None and r != 0:
        return r
    taxes = prod.get('taxes')
    if isinstance(taxes, str): taxes = taxes.split(',')
    if isinstance(taxes, list):
        for t in taxes:
            st = str(t)
            if '_ret_' in st:
                try: return float(st.split('_ret_')[-1])
                except (ValueError, IndexError): pass
    return 0

def fetch_data(endpoint, params=None):
    if params is None: params = {}
    all_data = []
    page = 1
    max_retries = 5
    retry_delay = 5

    if 'limit' not in params:
        params['limit'] = 500

    while True:
        params['page'] = page
        url = f"{BASE_URL}{endpoint}"
        logger.info(f"Fetching {endpoint} (Page {page}, Limit {params['limit']})...")

        current_data = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=HEADERS, params=params, timeout=40)

                if response.status_code == 200:
                    current_data = response.json()
                    break
                elif response.status_code == 429:
                    logger.warning(f"Rate limit hit (429) for {endpoint}. Waiting {retry_delay}s... (Attempt {attempt+1})")
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                else:
                    logger.error(f"Error fetching {endpoint} (Page {page}): {response.status_code} - {response.text}")
                    if response.status_code >= 500:
                        time.sleep(2 * (attempt + 1))
                        continue
                    else:
                        break
            except Exception as e:
                logger.error(f"Exception fetching {endpoint} (Page {page}, Attempt {attempt+1}): {e}")
                time.sleep(2 * (attempt + 1))

        if current_data is None:
            logger.warning(f"Failed to fetch {endpoint} page {page} after retries. Returning partial data.")
            break

        if isinstance(current_data, list):
            all_data.extend(current_data)
            logger.info(f"Received {len(current_data)} items. Total: {len(all_data)}")
            if len(current_data) < params['limit']:
                break
            page += 1
        else:
            return current_data

    return all_data


def post_data(endpoint, payload):
    """POST to Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted POST to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "id": "SAFE_MODE_ID_TEST", "info": "Dry run successful", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 201):
            return response.json()
        else:
            logger.error(f"Error posting to {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout posting to {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error posting to {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}

def put_data(endpoint, payload):
    """PUT to Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted PUT to {endpoint}")
        logger.debug(f"[SAFE MODE] Payload: {payload}")
        return {"status": 1, "info": "Dry run successful", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.put(url, headers=HEADERS, json=payload, timeout=30)
        if response.status_code in (200, 201):
            return response.json()
        else:
            logger.error(f"Error putting to {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout putting to {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error putting to {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}

def delete_data(endpoint):
    """DELETE on Holded API. Returns dict with 'error' key on failure."""
    if SAFE_MODE:
        logger.info(f"[SAFE MODE] Intercepted DELETE to {endpoint}")
        return {"status": 1, "info": "Dry run successful (delete)", "dry_run": True}

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.delete(url, headers=HEADERS, timeout=30)
        if response.status_code in (200, 201, 204):
            try:
                return response.json()
            except ValueError:
                return {"status": 1, "info": "Deleted"}
        else:
            logger.error(f"Error deleting {endpoint}: {response.status_code} - {response.text}")
            return {"error": True, "status_code": response.status_code, "detail": response.text}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout deleting {endpoint}")
        return {"error": True, "status_code": 0, "detail": "Request timed out"}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error deleting {endpoint}: {e}")
        return {"error": True, "status_code": 0, "detail": str(e)}

def _extract_project_code(products):
    """Scan line items for the 'Proyect REF:' product and return its desc as project code.
    Returns the project code string, or None if not found.
    Detection: by productId (reliable) or by name (fallback, case-insensitive)."""
    for prod in (products or []):
        pid = prod.get('productId')
        name = (prod.get('name') or '').strip().lower()
        if pid == PROYECTO_PRODUCT_ID or name == PROYECTO_PRODUCT_NAME:
            return (prod.get('desc') or '').strip() or None
    return None


def _extract_shooting_dates(products):
    """Scan line items for 'Shooting Dates:' product and return its desc.
    Detection: by productId (reliable) or by name (fallback, case-insensitive)."""
    for prod in (products or []):
        pid = prod.get('productId')
        name = (prod.get('name') or '').strip().lower()
        if pid == SHOOTING_DATES_PRODUCT_ID or name == SHOOTING_DATES_PRODUCT_NAME:
            return (prod.get('desc') or '').strip() or None
    return None


def holded_put(endpoint, data):
    """PUT request to Holded API."""
    try:
        response = requests.put(f"{BASE_URL}{endpoint}", headers=HEADERS, json=data)
        if response.status_code == 200:
            return response.json()
        logger.error(f"Error putting to {endpoint}: {response.status_code} - {response.text[:200]}")
        return {"status": 0, "info": response.text[:200]}
    except Exception as e:
        logger.error(f"Exception putting to {endpoint}: {e}")
        return None
