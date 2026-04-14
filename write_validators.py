"""
write_validators.py — Validation rules for the Safe Write Gateway.

Each validator returns (is_valid: bool, errors: list[dict], context: dict).
The context dict carries pre-fetched DB data to avoid duplicate queries in preview.
"""

import re
import logging
import time
import connector
from app.holded.client import (
    PROYECTO_PRODUCT_ID, PROYECTO_PRODUCT_NAME,
    SHOOTING_DATES_PRODUCT_ID, SHOOTING_DATES_PRODUCT_NAME,
)

logger = logging.getLogger(__name__)

# ── Status Transition Maps ────────────────────────────────────────────

INVOICE_TRANSITIONS = {
    0: {1},           # draft → issued
    1: {2, 3, 4, 5},  # issued → partial, paid, overdue, cancelled
    2: {3, 4, 5},      # partial → paid, overdue, cancelled
    4: {2, 3, 5},      # overdue → partial, paid, cancelled
    3: set(),           # paid → terminal
    5: set(),           # cancelled → terminal
}

ESTIMATE_TRANSITIONS = {
    0: {1},       # draft → pending
    1: {2, 3},    # pending → accepted, rejected
    2: {4},       # accepted → invoiced
    3: set(),     # rejected → terminal
    4: set(),     # invoiced → terminal
}

VALID_TAX_RATES = {0, 4, 10, 21}
VALID_CONTACT_TYPES = {"client", "supplier", "debtor", "creditor", "lead"}
VALID_TAX_OPERATIONS = {"general", "intra", "impexp", "nosujeto", "receq", "exento"}
VALID_PRODUCT_KINDS = {"simple", "pack"}
HOLDED_ID_PATTERN = re.compile(r'^[a-f0-9]{24}$')


# ── Input Sanitization ───────────────────────────────────────────────

def _sanitize_text(value, max_length=500):
    """Strip whitespace and HTML tags, enforce max length."""
    if not value:
        return value
    value = str(value).strip()
    # Iteratively strip HTML tags to handle nested constructs like <scr<script>ipt>
    prev = None
    while prev != value:
        prev = value
        value = re.sub(r'<[^>]+>', '', value)
    return value[:max_length]


def _validate_holded_id(value, field_name):
    """Validate Holded MongoDB ObjectId format."""
    if not value:
        return {"field": field_name, "msg": f"{field_name} is required"}
    if not HOLDED_ID_PATTERN.match(str(value)):
        return {"field": field_name, "msg": f"{field_name} must be a 24-char hex string (Holded ID)"}
    return None


def _validate_email(email):
    """Basic email format validation."""
    if not email:
        return None  # optional
    if len(str(email)) > 254:  # RFC 5321 max email length
        return {"field": "email", "msg": "Email too long (max 254 chars)"}
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, str(email)):
        return {"field": "email", "msg": f"Invalid email format: {email}"}
    return None


def _validate_amount(value, field_name, min_val=0, max_val=999999.99):
    """Validate numeric amount is within range."""
    if value is None:
        return None
    try:
        v = float(value)
    except (ValueError, TypeError):
        return {"field": field_name, "msg": f"{field_name} must be numeric"}
    if v < min_val or v > max_val:
        return {"field": field_name, "msg": f"{field_name} must be between {min_val} and {max_val}"}
    return None


def _validate_date(value, field_name):
    """Validate Unix timestamp is reasonable (2020 to ~2 years ahead)."""
    if value is None:
        return None  # optional
    try:
        ts = int(value)
    except (ValueError, TypeError):
        return {"field": field_name, "msg": f"{field_name} must be an integer (Unix timestamp)"}
    max_ts = int(time.time()) + (2 * 365 * 86400)
    if ts < 1577836800 or ts > max_ts:  # 2020-01-01 to ~2 years ahead
        return {"field": field_name, "msg": f"{field_name} timestamp out of range"}
    return None


# ── DB Lookup Helpers ────────────────────────────────────────────────

def _row_to_dict(cursor, row):
    """Convert a DB row (tuple or dict-like) to a plain dict.

    Works for both SQLite (tuple rows) and PostgreSQL (RealDictRow).
    Uses cursor.description to get column names for tuple rows.
    """
    if row is None:
        return None
    if hasattr(row, 'keys'):
        return dict(row)
    # SQLite tuple row — build dict from cursor.description
    return {desc[0]: row[i] for i, desc in enumerate(cursor.description)}


def _fetch_contact(contact_id):
    """Fetch contact from local DB, with API fallback. Returns dict or None."""
    import logging
    _logger = logging.getLogger(__name__)
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('SELECT * FROM contacts WHERE id = ?'), (contact_id,))
        row = cursor.fetchone()
        result = _row_to_dict(cursor, row)
    finally:
        connector.release_db(conn)
    if result:
        return result
    # Fallback: fetch from Holded API
    _logger.warning(f"Contact {contact_id} not in local DB, fetching from Holded API")
    api_data = connector.fetch_data(f"/invoicing/v1/contacts/{contact_id}")
    if api_data and not api_data.get("error") and api_data.get("id"):
        return {
            "id": api_data.get("id"),
            "name": api_data.get("name", ""),
            "code": api_data.get("code", ""),
            "email": api_data.get("email", ""),
            # Fiscal fields needed for contact completeness check
            "vatnumber": api_data.get("vatnumber", ""),
            "bill_country": (api_data.get("billAddress") or {}).get("countryCode", "")
                or (api_data.get("billAddress") or {}).get("country", "")
                or api_data.get("country", ""),
        }
    return None


def _fetch_products_batch(product_ids):
    """Fetch multiple products in one query. Returns {id: dict}."""
    if not product_ids:
        return {}
    # Cap to prevent oversized IN clauses (items already limited to 100)
    product_ids = list(set(product_ids))[:100]
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        placeholders = ','.join([connector._q('?')] * len(product_ids))
        cursor.execute(f'SELECT * FROM products WHERE id IN ({placeholders})', tuple(product_ids))
        rows = cursor.fetchall()
        result = {}
        for r in rows:
            d = _row_to_dict(cursor, r)
            if d:
                result[d['id']] = d
        return result
    finally:
        connector.release_db(conn)


_VALID_DOC_TABLES = frozenset({"invoices", "estimates", "purchase_invoices"})

def _fetch_document(doc_type, doc_id):
    """Fetch document from local DB, with API fallback. Returns dict or None."""
    import logging
    _logger = logging.getLogger(__name__)
    table_map = {"invoice": "invoices", "estimate": "estimates",
                 "purchase": "purchase_invoices"}
    table = table_map.get(doc_type)
    if not table or table not in _VALID_DOC_TABLES:
        return None
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q(f'SELECT * FROM {table} WHERE id = ?'), (doc_id,))
        row = cursor.fetchone()
        result = _row_to_dict(cursor, row)
    finally:
        connector.release_db(conn)
    if result:
        return result
    # Fallback: fetch from Holded API
    _logger.warning(f"Document {doc_type}/{doc_id} not in local DB, fetching from Holded API")
    api_endpoint = f"/invoicing/v1/documents/{doc_type}/{doc_id}"
    api_data = connector.fetch_data(api_endpoint)
    if api_data and not api_data.get("error") and api_data.get("id"):
        # Map API response to internal format
        return {
            "id": api_data.get("id"),
            "contact_id": api_data.get("contact"),
            "status": api_data.get("status", 0),
            "date": api_data.get("date"),
            "desc": api_data.get("desc", ""),
            "notes": api_data.get("notes", ""),
            "total": api_data.get("total", 0),
            "subtotal": api_data.get("subtotal", 0),
        }
    return None


# ── Validators ───────────────────────────────────────────────────────

def validate_create_invoice(params):
    """Validate create_invoice parameters. Returns (is_valid, errors, context)."""
    return _validate_create_document(params, "invoice")


def validate_create_estimate(params):
    """Validate create_estimate parameters. Returns (is_valid, errors, context)."""
    return _validate_create_document(params, "estimate")


def _validate_retention(items, contact, products_map):
    """Enforce IRPF retention rules for Spanish contacts.
    - Products (rental equipment): 19% IRPF
    - Services (fees): 15% IRPF
    - Items with price=0 (Proyect REF, Shooting Dates): exempt
    For non-Spanish contacts: no retention required.
    """
    country = (contact.get("bill_country") or contact.get("country") or "").upper()
    if country not in ("ES", "ESPAÑA", "SPAIN"):
        return []

    errors = []
    for idx, item in enumerate(items):
        price = float(item.get("price", 0) or 0)
        if price == 0:
            continue  # exempt: tracking items (Proyect REF, Shooting Dates)

        pid = item.get("product_id", "")
        name = (item.get("name") or "").strip().lower()

        # Skip special tracking items by name too
        if name.startswith(PROYECTO_PRODUCT_NAME) or name.startswith(SHOOTING_DATES_PRODUCT_NAME):
            continue

        retention = float(item.get("retention", 0) or 0)
        is_product = pid in products_map if pid else False

        if is_product:
            # Equipment rental → 19% IRPF
            if retention != 19:
                errors.append({
                    "field": f"items[{idx}].retention",
                    "msg": (f"Item '{item.get('name', '')}' is a product (rental equipment) "
                            f"for Spanish contact — requires 19% IRPF retention, got {retention}%.")
                })
        else:
            # Service/fee → 15% IRPF
            if retention != 15:
                errors.append({
                    "field": f"items[{idx}].retention",
                    "msg": (f"Item '{item.get('name', '')}' is a service/fee "
                            f"for Spanish contact — requires 15% IRPF retention, got {retention}%.")
                })

    return errors


def _validate_required_items(items):
    """Check that Proyect REF and Shooting Dates items are present.
    Both are mandatory for estimates and invoices.
    Detection: by product_id (reliable) or by name (fallback, case-insensitive).
    """
    errors = []
    has_proyect_ref = False
    has_shooting_dates = False
    proyect_ref_has_desc = False

    for item in items:
        pid = item.get("product_id", "")
        name = (item.get("name") or "").strip().lower()

        if pid == PROYECTO_PRODUCT_ID or name.startswith(PROYECTO_PRODUCT_NAME):
            has_proyect_ref = True
            desc = (item.get("desc") or item.get("description") or "").strip()
            if desc:
                proyect_ref_has_desc = True

        if pid == SHOOTING_DATES_PRODUCT_ID or name.startswith(SHOOTING_DATES_PRODUCT_NAME):
            has_shooting_dates = True

    if not has_proyect_ref:
        errors.append({
            "field": "items",
            "msg": (f"Missing required 'Proyect REF' item. "
                    f"Add an item with product_id='{PROYECTO_PRODUCT_ID}' "
                    f"and the project code (e.g. CLIENT-DDMMYYYY) in the description field.")
        })
    elif not proyect_ref_has_desc:
        errors.append({
            "field": "items",
            "msg": "Proyect REF item found but description is empty. Set the project code (e.g. LLUMM-160426) in the desc field."
        })

    if not has_shooting_dates:
        errors.append({
            "field": "items",
            "msg": (f"Missing required 'Shooting Dates' item. "
                    f"Add an item with product_id='{SHOOTING_DATES_PRODUCT_ID}' "
                    f"and the shooting date(s) in the description field.")
        })

    return errors


def _validate_create_document(params, doc_type):
    """Shared validation for invoice/estimate creation."""
    errors = []
    context = {"doc_type": doc_type}

    # Contact validation
    contact_id = params.get("contact_id")
    id_err = _validate_holded_id(contact_id, "contact_id")
    if id_err:
        errors.append(id_err)
    else:
        contact = _fetch_contact(contact_id)
        if not contact:
            errors.append({"field": "contact_id", "msg": f"Contact '{contact_id}' not found in database"})
        else:
            context["contact"] = contact

            # Contact fiscal completeness check
            contact_name = contact.get("name", "Unknown")
            vatnumber = contact.get("vatnumber", "") or contact.get("code", "") or ""
            bill_country = contact.get("bill_country") or contact.get("country") or ""

            if not vatnumber:
                errors.append({
                    "field": "contact_id",
                    "msg": f"Contact '{contact_name}' is missing NIF/CIF (vatnumber). Update in Holded before creating documents."
                })
            if not bill_country:
                errors.append({
                    "field": "contact_id",
                    "msg": f"Contact '{contact_name}' is missing country. Update in Holded before creating documents."
                })

    # Items validation
    items = params.get("items", [])
    if not items:
        errors.append({"field": "items", "msg": "At least one line item is required"})
    elif len(items) > 100:
        errors.append({"field": "items", "msg": "Maximum 100 items per document"})
    else:
        product_ids = [i.get("product_id") for i in items if i.get("product_id")]
        products_map = _fetch_products_batch(product_ids) if product_ids else {}
        context["products"] = products_map

        for idx, item in enumerate(items):
            name = item.get("name")
            if not name or not str(name).strip():
                errors.append({"field": f"items[{idx}].name", "msg": "Item name is required"})

            units = item.get("units", 1)
            err = _validate_amount(units, f"items[{idx}].units", min_val=0.01)
            if err:
                errors.append(err)

            price = item.get("price", 0)
            err = _validate_amount(price, f"items[{idx}].price", min_val=0)
            if err:
                errors.append(err)

            tax = item.get("tax")
            if tax is not None:
                try:
                    tax_int = int(tax)
                except (ValueError, TypeError):
                    errors.append({"field": f"items[{idx}].tax", "msg": "Tax must be numeric"})
                    continue
                if tax_int not in VALID_TAX_RATES:
                    errors.append({"field": f"items[{idx}].tax", "msg": f"Tax must be one of {VALID_TAX_RATES}"})

            pid = item.get("product_id")
            if pid and pid not in products_map:
                errors.append({"field": f"items[{idx}].product_id", "msg": f"Product '{pid}' not found"})

        # Required items check (Proyect REF + Shooting Dates)
        req_errors = _validate_required_items(items)
        errors.extend(req_errors)

        # IRPF retention check for Spanish contacts
        contact = context.get("contact")
        if contact:
            ret_errors = _validate_retention(items, contact, products_map)
            errors.extend(ret_errors)

    # Date validation (optional)
    err = _validate_date(params.get("date"), "date")
    if err:
        errors.append(err)

    return (len(errors) == 0, errors, context)


def validate_create_contact(params):
    """Validate create_contact parameters."""
    errors = []
    context = {}

    name = _sanitize_text(params.get("name"))
    if not name:
        errors.append({"field": "name", "msg": "Contact name is required"})

    ctype = params.get("type")
    if ctype and ctype not in VALID_CONTACT_TYPES:
        errors.append({"field": "type", "msg": f"Type must be one of {VALID_CONTACT_TYPES}"})

    err = _validate_email(params.get("email"))
    if err:
        errors.append(err)

    tax_op = params.get("taxOperation")
    if tax_op and tax_op not in VALID_TAX_OPERATIONS:
        errors.append({"field": "taxOperation", "msg": f"Must be one of {VALID_TAX_OPERATIONS}"})

    # Duplicate check — api.py sends "vat", gateway maps to "code"
    code = params.get("code") or params.get("vat", "")
    if name:
        conn = connector.get_db()
        try:
            cursor = connector._cursor(conn)
            cursor.execute(connector._q(
                'SELECT id, name FROM contacts WHERE name = ? AND code = ?'
            ), (name, code))
            existing = cursor.fetchone()
            if existing:
                errors.append({"field": "name", "msg": f"Contact '{name}' with code '{code}' already exists"})
        finally:
            connector.release_db(conn)

    return (len(errors) == 0, errors, context)


def validate_update_document_status(params):
    """Validate status transition for a document."""
    errors = []
    context = {}

    doc_type = params.get("doc_type", "invoice")
    doc_id = params.get("doc_id")

    id_err = _validate_holded_id(doc_id, "doc_id")
    if id_err:
        errors.append(id_err)
        return (False, errors, context)

    doc = _fetch_document(doc_type, doc_id)
    if not doc:
        errors.append({"field": "doc_id", "msg": f"Document {doc_type}/{doc_id} not found"})
        return (False, errors, context)

    context["document"] = doc
    old_status = doc.get("status", 0)
    new_status = params.get("status")

    if new_status is None:
        errors.append({"field": "status", "msg": "New status is required"})
        return (False, errors, context)

    try:
        new_status = int(new_status)
    except (ValueError, TypeError):
        errors.append({"field": "status", "msg": "Status must be an integer"})
        return (False, errors, context)

    transitions = ESTIMATE_TRANSITIONS if doc_type == "estimate" else INVOICE_TRANSITIONS
    allowed = transitions.get(old_status, set())

    if new_status not in allowed:
        status_labels = {0: "draft", 1: "issued/pending", 2: "partial/accepted",
                         3: "paid/rejected", 4: "overdue/invoiced", 5: "cancelled"}
        errors.append({
            "field": "status",
            "msg": f"Cannot transition from {status_labels.get(old_status, old_status)} to {status_labels.get(new_status, new_status)}"
        })

    return (len(errors) == 0, errors, context)


def validate_send_document(params):
    """Validate send_document parameters."""
    errors = []
    context = {}

    doc_type = params.get("doc_type")
    doc_id = params.get("doc_id")

    if doc_id:
        id_err = _validate_holded_id(doc_id, "doc_id")
        if id_err:
            errors.append(id_err)
            return (False, errors, context)
        doc = _fetch_document(doc_type, doc_id)
        if not doc:
            errors.append({"field": "doc_id", "msg": f"Document {doc_type}/{doc_id} not found"})
        else:
            context["document"] = doc
            if doc.get("status") == 0:
                errors.append({"field": "status", "msg": "Cannot send a draft document"})

    emails = params.get("emails", [])
    if isinstance(emails, str):
        emails = [e.strip() for e in emails.split(",")]
    if not emails:
        errors.append({"field": "emails", "msg": "At least one email is required"})
    elif len(emails) > 10:
        errors.append({"field": "emails", "msg": "Maximum 10 recipients per send"})
    for email in emails:
        err = _validate_email(email)
        if err:
            errors.append(err)

    return (len(errors) == 0, errors, context)


def validate_convert_estimate_to_invoice(params):
    """Validate estimate-to-invoice conversion."""
    errors = []
    context = {}

    estimate_id = params.get("estimate_id")
    id_err = _validate_holded_id(estimate_id, "estimate_id")
    if id_err:
        errors.append(id_err)
        return (False, errors, context)

    estimate = _fetch_document("estimate", estimate_id)
    if not estimate:
        errors.append({"field": "estimate_id", "msg": f"Estimate '{estimate_id}' not found"})
        return (False, errors, context)

    context["estimate"] = estimate

    # Always fetch items fresh from Holded API for convert operations.
    # DB items have account stored as human-readable text ("Nombre (num)"),
    # but Holded API needs the account ID. Fresh API items have the correct ID.
    import logging
    _logger = logging.getLogger(__name__)
    items = connector.fetch_estimate_fresh(estimate_id)
    if not items:
        _logger.warning(f"API returned no items for estimate {estimate_id}, falling back to local DB")
        conn = connector.get_db()
        try:
            cursor = connector._cursor(conn)
            cursor.execute(connector._q(
                'SELECT * FROM estimate_items WHERE estimate_id = ?'
            ), (estimate_id,))
            rows = cursor.fetchall()
            items = [_row_to_dict(cursor, r) for r in rows]
            items = [i for i in items if i]  # filter None
        finally:
            connector.release_db(conn)
    if not items:
        errors.append({"field": "items", "msg": "Estimate has no line items (checked API + DB)"})
        return (False, errors, context)

    context["estimate_items"] = items

    # Check estimate isn't already invoiced
    if estimate.get("status") == 4:
        errors.append({"field": "status", "msg": "Estimate is already invoiced"})

    # Fetch contact for the invoice creation
    contact_id = estimate.get("contact_id")
    if contact_id:
        contact = _fetch_contact(contact_id)
        if contact:
            context["contact"] = contact
        else:
            errors.append({"field": "contact_id", "msg": f"Contact '{contact_id}' not found in database"})
    else:
        errors.append({"field": "contact_id", "msg": "Estimate has no associated contact"})

    return (len(errors) == 0, errors, context)


# ── Validator Registry ───────────────────────────────────────────────

def validate_approve_invoice(params):
    """Validate approve_invoice: doc_id must exist and be a draft invoice (status 0)."""
    errors = []
    context = {}

    doc_id = params.get("doc_id")
    id_err = _validate_holded_id(doc_id, "doc_id")
    if id_err:
        errors.append(id_err)
        return (False, errors, context)

    doc = _fetch_document("invoice", doc_id)
    if not doc:
        errors.append({"field": "doc_id", "msg": f"Invoice {doc_id} not found in local DB"})
        return (False, errors, context)

    context["document"] = doc
    status = doc.get("status", 0)
    if status != 0:
        status_labels = {0: "draft", 1: "approved", 2: "partial", 3: "paid", 4: "overdue", 5: "cancelled"}
        errors.append({
            "field": "status",
            "msg": f"Invoice is already {status_labels.get(status, status)} — only draft invoices can be approved"
        })

    return (len(errors) == 0, errors, context)


VALIDATORS = {
    "create_invoice": validate_create_invoice,
    "create_estimate": validate_create_estimate,
    "create_contact": validate_create_contact,
    "update_document_status": validate_update_document_status,
    "send_document": validate_send_document,
    "convert_estimate_to_invoice": validate_convert_estimate_to_invoice,
    "approve_invoice": validate_approve_invoice,
}


def validate(operation, params):
    """Run the validator for an operation. Returns (is_valid, errors, context)."""
    validator = VALIDATORS.get(operation)
    if not validator:
        return (True, [], {})  # No validator = pass through
    return validator(params)
