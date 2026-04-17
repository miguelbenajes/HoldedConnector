"""Pure fiscal logic for the Smart Estimate system.

Provides IRPF, IVA, and account classification as pure functions — no API calls,
no DB access. Account IDs are imported from item_builder.py (single source of truth).

Consumed by:
  - Smart Estimate agent when building estimate payloads
  - Any future service that needs fiscal classification logic
"""

import re
from app.domain.item_builder import ACCOUNT_IDS, RETENTION_TO_ACCOUNT

# ── Tax regime constants ─────────────────────────────────────────────────────

SPAIN_CODES = {"ES", "ESPAÑA", "SPAIN", "ESP"}

EU_COUNTRIES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "FI", "FR",
    "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL",
    "PL", "PT", "RO", "SE", "SI", "SK",
}

# ── Rate tables ──────────────────────────────────────────────────────────────

# IRPF retention % by item type
IRPF_RATES = {
    "rental":      19,
    "service":     15,
    "expense":      0,
    "product":      0,
    "passthrough":  0,
}

# IVA (output tax) % by tax regime
IVA_RATES = {
    "spain":     21,
    "eu":         0,
    "extra_eu":   0,
    "unknown":   21,  # conservative fallback
}

# Build account_id lookup from RETENTION_TO_ACCOUNT + ACCOUNT_IDS (SSOT)
# Maps IRPF retention % → Holded account UUID
ACCOUNT_BY_IRPF = {
    ret: ACCOUNT_IDS[acc_num]
    for ret, acc_num in RETENTION_TO_ACCOUNT.items()
    if acc_num in ACCOUNT_IDS
}


# ── Date parsing patterns ────────────────────────────────────────────────────

# Range pattern: "25-26/05/2026" or "25-26/5/2026" — captures first date
_DATE_RANGE_RE = re.compile(
    r"(\d{1,2})-\d{1,2}/(\d{1,2})/(\d{4})"
)

# Simple date: "17/3/2026" or "16/04/2026"
_DATE_SIMPLE_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})"
)


# ── Public API ───────────────────────────────────────────────────────────────

def determine_tax_regime(country: str) -> str:
    """Classify a country string into a fiscal regime.

    Args:
        country: ISO-2 code, full name, or empty string.

    Returns:
        'spain' | 'eu' | 'extra_eu' | 'unknown'
    """
    if not country:
        return "unknown"

    normalized = country.strip().upper()

    if normalized in SPAIN_CODES:
        return "spain"

    if normalized in EU_COUNTRIES:
        return "eu"

    # Non-empty, non-Spain, non-EU
    return "extra_eu"


def compute_item_fiscality(item_type: str, tax_regime: str) -> dict:
    """Compute tax, IRPF retention, and Holded account ID for a line item.

    Args:
        item_type:   One of 'rental' | 'service' | 'expense' | 'product' | 'passthrough'
        tax_regime:  Output of determine_tax_regime()

    Returns:
        {
            "tax":        int   — IVA percentage (e.g. 21)
            "retention":  int   — IRPF retention % (e.g. 19, 15, 0)
            "account_id": str | None — Holded internal account UUID
        }
    """
    # For non-Spain regimes, no retention and no output IVA
    if tax_regime in ("eu", "extra_eu"):
        return {"tax": 0, "retention": 0, "account_id": None}

    retention = IRPF_RATES.get(item_type, 0)
    tax = IVA_RATES.get(tax_regime, 21)

    # Account ID by retention rate, with expense override
    # Expenses (taxi, hotel, SSD) repercutidos al cliente = prestación de servicios (75900000)
    if item_type == "expense":
        account_id = ACCOUNT_BY_IRPF.get(15)  # 75900000 — Prestación de servicios
    else:
        account_id = ACCOUNT_BY_IRPF.get(retention)

    return {
        "tax": tax,
        "retention": retention,
        "account_id": account_id,
    }


def generate_project_code(client_name: str, shooting_date: str) -> str:
    """Generate a canonical project code from client name and shooting date.

    Format: "PREFIX-DDMMYYYY"

    Rules:
    - PREFIX: first word of client name, uppercased, spaces→hyphens if multi-word
    - DATE: parsed from "DD/MM/YYYY" or range "DD-DD/MM/YYYY" (first date used)
    - Day and month are zero-padded to 2 digits

    Args:
        client_name:   e.g. "LLUMM", "HOFF BRAND", "llumm studios"
        shooting_date: e.g. "16/04/2026", "25-26/05/2026", "17/3/2026"

    Returns:
        e.g. "LLUMM-16042026", "HOFF-25052026"
    """
    # Build prefix from client name
    words = client_name.strip().upper().split()
    prefix = words[0] if words else "CLIENT"

    # Parse date — try range first, then simple
    day = month = year = None

    m = _DATE_RANGE_RE.search(shooting_date)
    if m:
        day, month, year = m.group(1), m.group(2), m.group(3)
    else:
        m = _DATE_SIMPLE_RE.search(shooting_date)
        if m:
            day, month, year = m.group(1), m.group(2), m.group(3)

    if day is None:
        # Fallback: return prefix with raw date if unparseable
        return f"{prefix}-{shooting_date}"

    date_str = f"{int(day):02d}{int(month):02d}{year}"
    return f"{prefix}-{date_str}"
