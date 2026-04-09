"""
AI Chat router — /api/ai/* endpoints (11 endpoints).

Handles AI chat (streaming + non-streaming), conversation history,
favorites, confirmation flow, and AI configuration.
Extracted from api.py (Fase 4 router split, Task 11).
"""
from fastapi import APIRouter, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
import json
import ai_agent
import connector
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic Models ─────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., max_length=8000)
    conversation_id: Optional[str] = Field(None, max_length=100)

class ConfirmRequest(BaseModel):
    pending_state_id: str = Field(..., max_length=100)
    confirmed: bool

class FavoriteRequest(BaseModel):
    query: str
    label: Optional[str] = None

class AIConfigUpdate(BaseModel):
    claudeApiKey: Optional[str] = Field(None, max_length=200)


# ── Endpoints ───────────────────────────────────────────────────────────

@router.post("/api/ai/chat")
async def ai_chat(body: ChatRequest, request: Request):
    if not ai_agent.check_rate_limit():
        return {"type": "error", "content": "Rate limit exceeded. Please wait a moment."}
    user_role = getattr(getattr(request.state, "user", None), "role", "admin")
    result = ai_agent.chat(body.message, body.conversation_id, user_role=user_role)
    return result

@router.post("/api/ai/chat/stream")
async def ai_chat_stream(body: ChatRequest, request: Request):
    if not ai_agent.check_rate_limit():
        async def error_gen():
            yield f"event: error\ndata: {json.dumps({'content': 'Rate limit exceeded.'})}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    user_role = getattr(getattr(request.state, "user", None), "role", "admin")

    def sse_generator():
        for event in ai_agent.chat_stream(body.message, body.conversation_id, user_role=user_role):
            evt = event.get("event", "message")
            data = event.get("data", "{}")
            yield f"event: {evt}\ndata: {data}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@router.post("/api/ai/confirm")
async def ai_confirm(body: ConfirmRequest):
    result = ai_agent.confirm_action(body.pending_state_id, body.confirmed)
    return result

@router.get("/api/ai/history")
async def ai_history(conversation_id: Optional[str] = None):
    return ai_agent.get_history(conversation_id)

@router.delete("/api/ai/history")
async def ai_clear_history(conversation_id: Optional[str] = None):
    ai_agent.clear_history(conversation_id)
    return {"status": "success"}

@router.get("/api/ai/conversations")
async def ai_conversations():
    return ai_agent.get_conversations()

@router.get("/api/ai/favorites")
async def ai_favorites():
    return ai_agent.get_favorites()

@router.post("/api/ai/favorites")
async def ai_add_favorite(body: FavoriteRequest):
    fav_id = ai_agent.add_favorite(body.query, body.label)
    return {"status": "success", "id": fav_id}

@router.delete("/api/ai/favorites/{fav_id}")
async def ai_remove_favorite(fav_id: int):
    ai_agent.remove_favorite(fav_id)
    return {"status": "success"}

@router.get("/api/ai/config")
async def ai_config():
    has_key = bool(ai_agent._get_api_key())
    return {"hasKey": has_key, "model": ai_agent._get_model(), "safeMode": connector.SAFE_MODE}

@router.post("/api/ai/config")
async def ai_config_update(body: AIConfigUpdate):
    if body.claudeApiKey:
        key = body.claudeApiKey.strip()
        if not key.startswith("sk-ant-"):
            return {"status": "error", "message": "Invalid API key format"}
        connector.save_setting("claude_api_key", key)
    return {"status": "success"}
