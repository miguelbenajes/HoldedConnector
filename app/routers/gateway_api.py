"""
Gateway API router — POST /api/gateway/estimate.

Service-to-service endpoint for Gaffer SP3 Quote Engine to create
estimates in Holded via the Write Gateway. Auth: BRAIN_INTERNAL_KEY Bearer token.
Extracted from api.py (Fase 4 router split, Task 4).
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from write_gateway import gateway
import os
import logging
import time

logger = logging.getLogger(__name__)
router = APIRouter()


class GatewayEstimateItem(BaseModel):
    name: str
    units: float = 1
    subtotal: float = 0
    tax: float = 21
    desc: Optional[str] = None


class GatewayEstimateBody(BaseModel):
    contact_id: str = Field(..., description="Holded contact ID")
    items: List[GatewayEstimateItem] = Field(..., min_length=1)
    desc: Optional[str] = None
    date: Optional[str] = None
    notes: Optional[str] = None


@router.post("/api/gateway/estimate")
def gateway_create_estimate(request: Request, body: GatewayEstimateBody):
    """
    REST wrapper around Write Gateway's create_estimate operation.
    For service-to-service calls (Gaffer → Holded).
    Auth: BRAIN_INTERNAL_KEY Bearer token.
    """
    from fastapi import HTTPException

    # Auth check
    auth_header = request.headers.get("authorization", "")
    expected_key = os.environ.get("BRAIN_INTERNAL_KEY", "")
    if not expected_key or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    import hmac as _hmac
    if not _hmac.compare_digest(auth_header[7:], expected_key):
        raise HTTPException(status_code=401, detail="Invalid key")

    # Remap 'subtotal' → 'price' so _build_holded_payload picks it up correctly
    gateway_items = []
    for item in body.items:
        d = item.model_dump(exclude_none=True)
        if "subtotal" in d:
            d["price"] = d.pop("subtotal")
        gateway_items.append(d)

    params = {
        "contact_id": body.contact_id,
        "items": gateway_items,
    }
    if body.desc:
        params["desc"] = body.desc
    # Holded requires Unix timestamp; default to now if not provided
    params["date"] = body.date if body.date else str(int(time.time()))
    if body.notes:
        params["notes"] = body.notes

    result = gateway.execute("create_estimate", params, source="gaffer", skip_confirm=True)

    if result.get("success"):
        return {
            "success": True,
            "entity_id": result.get("entity_id", ""),
            "doc_number": result.get("doc_number", ""),
        }
    else:
        raise HTTPException(
            status_code=500,
            detail=result.get("error") or result.get("errors", "Unknown error"),
        )
