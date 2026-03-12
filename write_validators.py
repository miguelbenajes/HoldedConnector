"""
write_validators.py — Validation rules for the Safe Write Gateway.

Each validator returns (is_valid: bool, errors: list[dict], context: dict).
The context dict carries pre-fetched DB data to avoid duplicate queries in preview.
"""

import re
import logging
import time
import connector

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
    """Fetch contact from local DB. Returns dict or None."""
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q('SELECT * FROM contacts WHERE id = ?'), (contact_id,))
        row = cursor.fetchone()
        return _row_to_dict(cursor, row)
    finally:
        connector.release_db(conn)


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


def _fetch_document(doc_type, doc_id):
    """Fetch document from local DB. Returns dict or None."""
    table_map = {"invoice": "invoices", "estimate": "estimates",
                 "purchase": "purchase_invoices"}
    table = table_map.get(doc_type)
    if not table:
        return None
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q(f'SELECT * FROM {table} WHERE id = ?'), (doc_id,))
        row = cursor.fetchone()
        return _row_to_dict(cursor, row)
    finally:
        connector.release_db(conn)


# ── Validators ───────────────────────────────────────────────────────

def validate_create_invoice(params):
    """Validate create_invoice parameters. Returns (is_valid, errors, context)."""
    return _validate_create_document(params, "invoice")


def validate_create_estimate(params):
    """Validate create_estimate parameters. Returns (is_valid, errors, context)."""
    return _validate_create_document(params, "estimate")


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

    # Duplicate check
    code = params.get("code", "")
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


# ── Validator Registry ───────────────────────────────────────────────

VALIDATORS = {
    "create_invoice": validate_create_invoice,
    "create_estimate": validate_create_estimate,
    "create_contact": validate_create_contact,
    "update_document_status": validate_update_document_status,
    "send_document": validate_send_document,
    # upload_file: no Holded validation needed
}


def validate(operation, params):
    """Run the validator for an operation. Returns (is_valid, errors, context)."""
    validator = VALIDATORS.get(operation)
    if not validator:
        return (True, [], {})  # No validator = pass through
    return validator(params)
