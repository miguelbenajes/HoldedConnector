"""
auth.py — Authentication module for holded-connector.

Supports three auth methods (checked in order by middleware in api.py):
  1. Legacy Bearer token (HOLDED_CONNECTOR_TOKEN) — Brain inter-service calls
  2. Supabase Bearer JWT — API clients sending Authorization header
  3. Supabase cookie — Panel users via nginx proxy (browser auto-sends cookies)

Auth flow for cookie path (most common — panel users):
  Browser → GET /panel/holded/api/summary
    → browser auto-sends cookies (same domain: coyoterent.com)
    → nginx strips /panel/holded → GET /api/summary → localhost:8000
    → this module reads sb-<ref>-auth-token cookie from request
    → validates JWT via JWKS (RS256) or JWT secret (HS256 fallback)
    → looks up user role in panel.panel_users
    → checks route permission against PERMISSION_MATRIX
    → returns user or raises error

Dependencies: PyJWT, cryptography, httpx
"""

import os
import re
import json
import hmac
import time
import logging
import base64
from typing import Optional, NamedTuple
from urllib.parse import unquote

import jwt
import httpx

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────

SUPABASE_PROJECT_REF = os.getenv("SUPABASE_PROJECT_REF", "mpgfivufawurjnpyvacf")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
HOLDED_CONNECTOR_TOKEN = os.getenv("HOLDED_CONNECTOR_TOKEN", "")
JWKS_URL = f"https://{SUPABASE_PROJECT_REF}.supabase.co/auth/v1/.well-known/jwks.json"
JWKS_CACHE_TTL = 3600  # 1 hour

# ── JWKS Cache ────────────────────────────────────────────────────────────

_jwks_cache: Optional[dict] = None
_jwks_cache_time: float = 0


def _fetch_jwks() -> dict:
    """Fetch JWKS from Supabase and cache for 1 hour.
    Falls back to cached keys on network failure."""
    global _jwks_cache, _jwks_cache_time
    now = time.time()

    if _jwks_cache and (now - _jwks_cache_time) < JWKS_CACHE_TTL:
        return _jwks_cache

    try:
        resp = httpx.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_cache_time = now
        logger.info("JWKS refreshed from Supabase")
        return _jwks_cache
    except Exception as e:
        logger.warning("Failed to fetch JWKS: %s — using cached keys", e)
        if _jwks_cache:
            return _jwks_cache
        raise RuntimeError("JWKS unavailable and no cached keys") from e


def _get_signing_key(token: str) -> tuple:
    """Get the public key and algorithm matching the token's kid claim.

    Supabase JWKS may use EC (ES256) or RSA (RS256) keys.
    Returns (public_key, algorithm_string) tuple.
    """
    jwks = _fetch_jwks()
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")

    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            kty = key_data.get("kty", "").upper()
            if kty == "EC":
                return jwt.algorithms.ECAlgorithm.from_jwk(key_data), "ES256"
            elif kty == "RSA":
                return jwt.algorithms.RSAAlgorithm.from_jwk(key_data), "RS256"
            elif kty == "OKP":
                return jwt.algorithms.OKPAlgorithm.from_jwk(key_data), "EdDSA"
            else:
                raise jwt.InvalidTokenError(f"Unsupported key type: {kty}")

    raise jwt.InvalidTokenError(f"No matching key found for kid={kid}")


# ── JWT Validation ────────────────────────────────────────────────────────


def validate_supabase_jwt(token: str) -> dict:
    """Validate a Supabase JWT and return the decoded payload.

    Tries JWKS first (auto-detects ES256/RS256), then HS256 as fallback.
    Returns decoded payload with 'sub' (auth user ID), 'email', etc.
    Raises jwt.InvalidTokenError on failure.
    """
    # Try JWKS (primary — production Supabase uses ES256 or RS256)
    try:
        public_key, algorithm = _get_signing_key(token)
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[algorithm],
            audience="authenticated",
            options={"verify_exp": True},
        )
        return payload
    except Exception as e:
        logger.debug("JWKS validation failed (%s): %s — trying HS256", type(e).__name__, e)

    # Fallback to HS256 with JWT secret (local dev, some Supabase configs)
    if SUPABASE_JWT_SECRET:
        try:
            payload = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_exp": True},
            )
            return payload
        except Exception as e:
            logger.debug("HS256 validation failed: %s", e)

    raise jwt.InvalidTokenError("JWT validation failed for all methods")


# ── Cookie Extraction ─────────────────────────────────────────────────────


def extract_jwt_from_cookies(cookie_header: str) -> Optional[str]:
    """Extract Supabase access token from the sb-<ref>-auth-token cookie.

    @supabase/ssr stores the session as a base64-encoded JSON object in
    httpOnly cookies. For large sessions, it chunks across multiple cookies:
      sb-<ref>-auth-token.0, sb-<ref>-auth-token.1, etc.

    This function handles both single and chunked cookie formats.
    """
    if not cookie_header:
        return None

    cookie_name = f"sb-{SUPABASE_PROJECT_REF}-auth-token"

    # Parse cookie header into dict
    cookies = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    # Try single cookie first
    token_data = cookies.get(cookie_name)

    # Try chunked cookies (.0, .1, .2, ...)
    if not token_data:
        chunks = []
        i = 0
        while f"{cookie_name}.{i}" in cookies:
            chunks.append(cookies[f"{cookie_name}.{i}"])
            i += 1
        if chunks:
            token_data = "".join(chunks)

    if not token_data:
        return None

    # Decode to get access_token from session JSON
    #
    # @supabase/ssr cookie format (v0.5+):
    #   - Plain JSON: '{"access_token":"eyJ...",...}'
    #   - Base64-URL prefixed: 'base64-eyJhY2Nlc3NfdG9rZW4iOi...'
    #     (base64url-encoded JSON, no padding, with 'base64-' prefix)
    #   - Chunked: value split across .0, .1, .2 cookies (reassembled above)
    try:
        decoded = unquote(token_data)

        # Try plain JSON first (older @supabase/ssr or small sessions)
        if decoded.startswith("{"):
            session = json.loads(decoded)
            return session.get("access_token")

        # Handle 'base64-' prefixed format (current @supabase/ssr)
        if decoded.startswith("base64-"):
            b64_data = decoded[7:]  # Strip 'base64-' prefix
            # base64url decode: replace URL-safe chars, add padding
            b64_standard = b64_data.replace("-", "+").replace("_", "/")
            b64_padded = b64_standard + "=" * (-len(b64_standard) % 4)
            session_json = base64.b64decode(b64_padded).decode("utf-8")
            session = json.loads(session_json)
            return session.get("access_token")

        # Fallback: try raw base64url without prefix
        b64_standard = decoded.replace("-", "+").replace("_", "/")
        b64_padded = b64_standard + "=" * (-len(b64_standard) % 4)
        session_json = base64.b64decode(b64_padded).decode("utf-8")
        session = json.loads(session_json)
        return session.get("access_token")

    except Exception as e:
        logger.warning("Failed to parse Supabase cookie: %s", e)
        return None


# ── Legacy Token ──────────────────────────────────────────────────────────


BRAIN_INTERNAL_KEY = os.getenv("BRAIN_INTERNAL_KEY", "")


def is_legacy_token(token: str) -> bool:
    """Check if the token matches a known inter-service key.
    Accepts HOLDED_CONNECTOR_TOKEN (Brain) or BRAIN_INTERNAL_KEY (Gaffer).
    Uses HMAC comparison to prevent timing attacks."""
    if HOLDED_CONNECTOR_TOKEN and hmac.compare_digest(token, HOLDED_CONNECTOR_TOKEN):
        return True
    if BRAIN_INTERNAL_KEY and hmac.compare_digest(token, BRAIN_INTERNAL_KEY):
        return True
    return False


# ── User Lookup ───────────────────────────────────────────────────────────


class PanelUser(NamedTuple):
    """Authenticated panel user from panel.panel_users."""
    id: str
    auth_id: str
    email: str
    name: str
    role: str
    is_active: bool


# Separate connection to the main Supabase project for panel.panel_users.
# The holded-connector's DATABASE_URL points to the holded-specific project,
# but panel_users lives on the main project (mpgfivufawurjnpyvacf).
PANEL_DATABASE_URL = os.getenv("PANEL_DATABASE_URL", "")


def _get_panel_connection():
    """Get a connection to the main Supabase project (panel schema)."""
    if not PANEL_DATABASE_URL:
        raise RuntimeError("PANEL_DATABASE_URL not set — cannot look up panel users")
    import psycopg2
    conn = psycopg2.connect(PANEL_DATABASE_URL)
    return conn


def lookup_user(auth_id: str, get_connection=None) -> Optional[PanelUser]:
    """Look up a panel user by their Supabase auth ID.

    Uses PANEL_DATABASE_URL to connect to the main project where
    panel.panel_users lives (separate from the holded DB).

    Args:
        auth_id: UUID from the JWT 'sub' claim (= auth.users.id)
        get_connection: Unused (kept for API compatibility)

    Returns:
        PanelUser or None if not found.
    """
    conn = _get_panel_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, auth_id, email, name, role, is_active
               FROM panel.panel_users
               WHERE auth_id = %s""",
            (auth_id,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return PanelUser(
            id=str(row[0]),
            auth_id=str(row[1]),
            email=row[2],
            name=row[3],
            role=row[4],
            is_active=row[5],
        )
    finally:
        conn.close()


# ── Public Paths ──────────────────────────────────────────────────────────

# Paths that don't require authentication
PUBLIC_PATHS = {"/health", "/", "/favicon.ico", "/api/config"}
PUBLIC_PREFIXES = ("/static/",)


def is_public_path(path: str) -> bool:
    """Check if a request path is public (no auth required)."""
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


# ── Permission Matrix ─────────────────────────────────────────────────────
# Maps (method_pattern, path_pattern) → set of allowed roles.
# Checked in order — first match wins. Default: DENY.

PERMISSION_MATRIX = [
    # Dashboard — all roles
    ("GET", r"^/api/(summary|stats(/.*)?)", {"admin", "accountant", "operator"}),

    # Invoices — read
    ("GET", r"^/api/(entities/invoices|recent)", {"admin", "accountant"}),
    # Invoices — write
    ("(POST|PUT|PATCH|DELETE)", r"^/api/agent/(invoice|send)", {"admin"}),

    # Purchases — read
    ("GET", r"^/api/(entities/purchases|invoices/unpaid|purchases/search)", {"admin", "accountant"}),

    # Contacts — read
    ("GET", r"^/api/entities/contacts", {"admin", "accountant"}),
    # Contacts — write
    ("POST", r"^/api/agent/contact", {"admin"}),

    # Products — read
    ("GET", r"^/api/(entities/products|products/(web|.*/pack-info))", {"admin", "accountant", "operator"}),
    # Products — write
    ("PATCH", r"^/api/entities/products/.*/web-include", {"admin", "operator"}),

    # Estimates — read
    ("GET", r"^/api/entities/estimates", {"admin", "accountant"}),
    # Smart estimates — write (includes contact resolution + product creation)
    ("POST", r"^/api/agent/estimate/smart", {"admin"}),
    # Estimates — write
    ("POST", r"^/api/agent/estimate", {"admin"}),

    # AI chat — read + interact
    ("(GET|POST)", r"^/api/ai/(chat|history|favorites|conversations)", {"admin", "accountant", "operator"}),
    # AI — write actions (confirm, delete)
    ("(POST|DELETE)", r"^/api/ai/(confirm|history|favorites)", {"admin", "operator"}),

    # Reports — read
    ("GET", r"^/api/reports/", {"admin", "accountant"}),

    # Analysis — read
    ("GET", r"^/api/analysis/", {"admin", "accountant"}),
    # Analysis — write
    ("POST", r"^/api/analysis/", {"admin"}),

    # Amortizations — read
    ("GET", r"^/api/amortizations", {"admin", "accountant"}),
    # Amortizations — write
    ("(POST|PUT|DELETE)", r"^/api/amortizations", {"admin"}),

    # Sync management
    ("(GET|POST)", r"^/api/sync", {"admin"}),

    # Settings
    ("(GET|POST|PUT|PATCH)", r"^/api/(config|ai/config)", {"admin"}),

    # Files
    ("(GET|POST|DELETE)", r"^/api/files/", {"admin", "operator"}),

    # Backup
    ("(GET|POST)", r"^/api/backup/", {"admin"}),

    # Schema
    ("GET", r"^/api/schema$", {"admin", "accountant"}),

    # Tickets
    ("POST", r"^/api/tickets/upload", {"admin", "operator"}),

    # User management
    ("(GET|POST|PUT|PATCH|DELETE)", r"^/api/users/", {"admin"}),

    # PDFs
    ("GET", r"^/api/entities/.*/pdf", {"admin", "accountant"}),

    # Jobs — job tracker
    ("GET", r"^/api/jobs", {"admin", "accountant"}),
    ("POST", r"^/api/jobs", {"admin"}),
    ("PATCH", r"^/api/jobs/", {"admin"}),
]


def check_permission(role: str, method: str, path: str) -> bool:
    """Check if a role has permission for the given HTTP method + path.

    Uses PERMISSION_MATRIX — first matching rule wins.
    Default: DENY (returns False if no rule matches).
    """
    for method_pattern, path_pattern, allowed_roles in PERMISSION_MATRIX:
        if re.match(method_pattern, method, re.IGNORECASE) and re.match(path_pattern, path):
            return role in allowed_roles
    # Default deny
    logger.warning("No permission rule matched: %s %s (role=%s) → DENIED", method, path, role)
    return False
