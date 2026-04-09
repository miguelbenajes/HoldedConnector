"""
Treasury & Payment router — /api/treasury and /api/documents/{doc_type}/{doc_id}/pay.

Handles fetching bank accounts from Holded API and registering payments
against invoices/purchases.
Extracted from api.py (Fase 4 router split, Task 3).
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import connector
import requests
import os
import re as _re_mod
import logging
import time

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/treasury")
def get_treasury_accounts():
    """Fetch bank/treasury accounts from Holded API."""
    try:
        url = f"{connector.BASE_URL}/invoicing/v1/treasury"
        response = requests.get(url, headers=connector.HEADERS, timeout=15)
        if response.status_code == 200:
            accounts = response.json()
            if not isinstance(accounts, list):
                return JSONResponse(status_code=502, content={"error": "Unexpected response format from Holded"})
            # Return only safe fields
            return [
                {
                    "id": a.get("id", ""),
                    "name": a.get("name", ""),
                    "type": a.get("type", ""),
                    "iban": a.get("iban", ""),
                    "bankname": a.get("bankname", ""),
                }
                for a in accounts
            ]
        return JSONResponse(
            status_code=response.status_code,
            content={"error": f"Holded API returned {response.status_code}"}
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Treasury fetch failed: {e}")
        return JSONResponse(status_code=502, content={"error": "Failed to reach Holded API"})


class PayDocumentBody(BaseModel):
    date: int = Field(..., description="Payment date as Unix timestamp")
    amount: float = Field(..., gt=0, le=999999.99, description="Payment amount in EUR")
    treasury: str = Field(..., min_length=1, max_length=64, description="Treasury/bank account ID from Holded")
    desc: str = Field("", max_length=500, description="Payment description")


@router.post("/api/documents/{doc_type}/{doc_id}/pay")
def pay_document(doc_type: str, doc_id: str, body: PayDocumentBody):
    """Register a payment against an invoice/purchase in Holded."""
    allowed_types = {"invoice", "purchase"}
    if doc_type not in allowed_types:
        return JSONResponse(status_code=400, content={"error": "doc_type must be 'invoice' or 'purchase'"})
    if not _re_mod.match(r'^[a-f0-9]{24}$', doc_id):
        return JSONResponse(status_code=400, content={"error": "Invalid document ID format"})

    # Validate date is reasonable (2020–01–01 to ~2 years ahead)
    max_ts = int(time.time()) + (2 * 365 * 86400)
    if body.date < 1577836800 or body.date > max_ts:
        return JSONResponse(status_code=400, content={"error": "Payment date out of valid range"})

    payload = {
        "date": body.date,
        "amount": body.amount,
        "treasury": body.treasury,
        "desc": body.desc,
    }
    result = connector.post_data(f"/invoicing/v1/documents/{doc_type}/{doc_id}/pay", payload)
    if result and not result.get("error"):
        return {"success": True, "result": result, "safe_mode": connector.SAFE_MODE}
    detail = result.get("detail", "Unknown error") if result else "No response"
    return JSONResponse(status_code=502, content={"success": False, "error": detail})
