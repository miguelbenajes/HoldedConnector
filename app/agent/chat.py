"""Chat and streaming handlers for the AI agent."""
import json
import time
import uuid
import os
import logging
import threading

import anthropic
import connector
from app.agent.tools import get_tools_for_role, WRITE_TOOLS, TOOL_DEFINITIONS
from app.agent.executors import TOOL_EXECUTORS
from app.agent.prompt import build_system_prompt
from app.agent.history import load_history, save_history
from app.agent.confirmation import (
    pending_actions, _pending_lock, _cleanup_pending, _describe_write_action,
)
from write_gateway import gateway

logger = logging.getLogger(__name__)

# Rate limiting: { ip: [timestamps] }
_rate_limits = {}
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60
_rate_limit_lock = threading.Lock()


def check_rate_limit(ip="local"):
    now = time.time()
    with _rate_limit_lock:
        if ip not in _rate_limits:
            _rate_limits[ip] = []
        _rate_limits[ip] = [t for t in _rate_limits[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_limits[ip]) >= RATE_LIMIT_MAX:
            return False
        _rate_limits[ip].append(now)
        # Prune stale IPs every 100 checks to prevent unbounded growth
        if len(_rate_limits) > 100:
            stale = [k for k, v in _rate_limits.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]
            for k in stale:
                del _rate_limits[k]
        return True


def _get_api_key():
    key = connector.get_setting("claude_api_key")
    if key:
        return key
    return os.getenv("ANTHROPIC_API_KEY", "")

def _get_model():
    model = connector.get_setting("ai_model")
    if model and model.startswith("claude"):
        return model
    return "claude-sonnet-4-20250514"


def chat(user_message, conversation_id=None, user_role="admin"):
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    api_key = _get_api_key()
    if not api_key:
        return {
            "type": "error",
            "content": "Claude API key not configured. Add it in Settings or set ANTHROPIC_API_KEY env var.",
            "conversation_id": conversation_id
        }

    client = anthropic.Anthropic(api_key=api_key)
    model = _get_model()

    # Load history and build messages
    history = load_history(conversation_id, limit=20)
    system_prompt = build_system_prompt()
    messages = history + [{"role": "user", "content": user_message}]

    role_tools = get_tools_for_role(user_role)
    tool_calls_summary = []
    charts = []

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            tools=role_tools,
            messages=messages
        )

        # Agent loop: handle tool_use
        max_iterations = 10
        iteration = 0
        while response.stop_reason == "tool_use" and iteration < max_iterations:
            iteration += 1
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input
                    logger.info(f"Agent calling tool: {tool_name}")

                    # If write tool, pause for confirmation
                    if tool_name in WRITE_TOOLS:
                        state_id = str(uuid.uuid4())
                        _cleanup_pending()

                        # Build description for user
                        desc = _describe_write_action(tool_name, tool_input)

                        # Run gateway validation + preview for write operations
                        gateway_result = None
                        op_name = tool_name
                        if op_name == "update_invoice_status":
                            op_name = "update_document_status"
                        gateway_result = gateway.execute(op_name, tool_input, source="ai_agent", conversation_id=conversation_id)

                        with _pending_lock:
                            pending_actions[state_id] = {
                                "messages": messages + [{"role": "assistant", "content": [b.model_dump() for b in response.content]}],
                                "tool_block": block.model_dump(),
                                "conversation_id": conversation_id,
                                "expires_at": time.time() + 300,
                                "model": model,
                                "system_prompt": system_prompt,
                                "user_message": user_message,
                                "gateway_preview": gateway_result,  # Safe Write Gateway preview data
                            }

                        return {
                            "type": "confirmation_needed",
                            "action": {
                                "tool": tool_name,
                                "description": desc,
                                "details": tool_input,
                                "preview": gateway_result.get("preview") if gateway_result else None,
                                "warnings": gateway_result.get("warnings") if gateway_result else None,
                                "reversibility": gateway_result.get("reversibility") if gateway_result else None,
                            },
                            "pending_state_id": state_id,
                            "conversation_id": conversation_id
                        }

                    # Execute read/utility tool
                    executor = TOOL_EXECUTORS.get(tool_name)
                    if executor:
                        result = executor(tool_input)
                    else:
                        result = {"error": f"Unknown tool: {tool_name}"}

                    if tool_name == "render_chart":
                        charts.append(result)
                    tool_calls_summary.append({"tool": tool_name, "description": tool_input.get("explanation", tool_name)})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str)
                    })

            # Continue the conversation with tool results
            messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})
            messages.append({"role": "user", "content": tool_results})

            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=role_tools,
                messages=messages
            )

        # Extract final text
        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

        # Guard: tool loop hit max iterations without a final text response
        if not final_text and iteration >= max_iterations:
            logger.warning(f"Tool loop hit max iterations ({max_iterations}) for conversation {conversation_id}")
            final_text = "I needed more steps to complete this request. Please try a simpler query."

        # Save to history
        save_history(conversation_id, "user", user_message)
        save_history(conversation_id, "assistant", final_text, tool_calls_summary or None)

        result = {
            "type": "message",
            "content": final_text,
            "conversation_id": conversation_id,
            "tool_calls_summary": tool_calls_summary
        }
        if charts:
            result["charts"] = charts
        return result

    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        return {"type": "error", "content": "AI service is temporarily unavailable. Please try again.", "conversation_id": conversation_id}
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        return {"type": "error", "content": "An internal error occurred. Please try again.", "conversation_id": conversation_id}


def chat_stream(user_message, conversation_id=None, user_role="admin"):
    """Generator that yields SSE events for streaming responses."""
    _cleanup_pending()  # Clean expired confirmations on every chat call
    if not conversation_id:
        conversation_id = str(uuid.uuid4())

    api_key = _get_api_key()
    if not api_key:
        yield {"event": "error", "data": json.dumps({"content": "Claude API key not configured.", "conversation_id": conversation_id})}
        return

    client = anthropic.Anthropic(api_key=api_key)
    model = _get_model()

    history = load_history(conversation_id, limit=20)
    system_prompt = build_system_prompt()
    messages = history + [{"role": "user", "content": user_message}]
    role_tools = get_tools_for_role(user_role)
    tool_calls_summary = []
    charts = []

    try:
        max_iterations = 10
        iteration = 0
        needs_tool_loop = True

        while needs_tool_loop and iteration < max_iterations:
            needs_tool_loop = False
            iteration += 1

            # Use streaming for the final text response, non-streaming for tool loops
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=role_tools,
                messages=messages
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input
                        logger.info(f"[stream] Agent calling tool: {tool_name}")
                        yield {"event": "tool_start", "data": json.dumps({"tool": tool_name})}

                        if tool_name in WRITE_TOOLS:
                            state_id = str(uuid.uuid4())
                            _cleanup_pending()
                            desc = _describe_write_action(tool_name, tool_input)

                            # Run gateway validation + preview for write operations
                            gateway_result = None
                            op_name = tool_name
                            if op_name == "update_invoice_status":
                                op_name = "update_document_status"
                            gateway_result = gateway.execute(op_name, tool_input, source="ai_agent", conversation_id=conversation_id)

                            with _pending_lock:
                                pending_actions[state_id] = {
                                    "messages": messages + [{"role": "assistant", "content": [b.model_dump() for b in response.content]}],
                                    "tool_block": block.model_dump(),
                                    "conversation_id": conversation_id,
                                    "expires_at": time.time() + 300,
                                    "model": model,
                                    "system_prompt": system_prompt,
                                    "user_message": user_message,
                                    "gateway_preview": gateway_result,  # Safe Write Gateway preview data
                                }
                            yield {"event": "confirmation_needed", "data": json.dumps({
                                "action": {
                                    "tool": tool_name,
                                    "description": desc,
                                    "details": tool_input,
                                    "preview": gateway_result.get("preview") if gateway_result else None,
                                    "warnings": gateway_result.get("warnings") if gateway_result else None,
                                    "reversibility": gateway_result.get("reversibility") if gateway_result else None,
                                },
                                "pending_state_id": state_id,
                                "conversation_id": conversation_id
                            })}
                            return

                        executor = TOOL_EXECUTORS.get(tool_name)
                        if executor:
                            result = executor(tool_input)
                        else:
                            result = {"error": f"Unknown tool: {tool_name}"}

                        if tool_name == "render_chart":
                            charts.append(result)
                        tool_calls_summary.append({"tool": tool_name, "description": tool_input.get("explanation", tool_name)})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str)
                        })

                messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})
                messages.append({"role": "user", "content": tool_results})
                needs_tool_loop = True
                continue

            # Final response - stream it
            # We already have the full response, extract text
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text += block.text

            # Send meta info first
            if tool_calls_summary:
                yield {"event": "tools_used", "data": json.dumps(tool_calls_summary)}
            if charts:
                yield {"event": "charts", "data": json.dumps(charts)}

            # Stream text in chunks
            chunk_size = 20
            for i in range(0, len(final_text), chunk_size):
                chunk = final_text[i:i+chunk_size]
                yield {"event": "text_delta", "data": json.dumps({"text": chunk})}

            yield {"event": "done", "data": json.dumps({"conversation_id": conversation_id})}

            save_history(conversation_id, "user", user_message)
            save_history(conversation_id, "assistant", final_text, tool_calls_summary or None)

        # Guard: if tool loop hit max iterations without producing a final response
        if needs_tool_loop and iteration >= max_iterations:
            logger.warning(f"[stream] Tool loop hit max iterations ({max_iterations}) for conversation {conversation_id}")
            yield {"event": "text_delta", "data": json.dumps({"text": "I needed more steps to complete this request. Please try a simpler query."})}
            yield {"event": "done", "data": json.dumps({"conversation_id": conversation_id})}

    except anthropic.APIError as e:
        logger.error(f"[stream] Claude API error: {e}")
        yield {"event": "error", "data": json.dumps({"content": "AI service is temporarily unavailable. Please try again."})}
    except Exception as e:
        logger.error(f"[stream] Agent error: {e}", exc_info=True)
        yield {"event": "error", "data": json.dumps({"content": "An internal error occurred. Please try again."})}


# ─── Favorites ──────────────────────────────────────────────────────

def get_favorites():
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute("SELECT id, query, label, created_at FROM ai_favorites ORDER BY created_at DESC")
        rows = [dict(r) for r in cursor.fetchall()]
        return rows
    finally:
        connector.release_db(conn)

def add_favorite(query, label=None):
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        if connector._USE_SQLITE:
            cursor.execute(connector._q("INSERT INTO ai_favorites (query, label) VALUES (?, ?)"),
                           (query, label or query[:50]))
            conn.commit()
            fav_id = cursor.lastrowid
        else:
            cursor.execute("INSERT INTO ai_favorites (query, label) VALUES (%s, %s) RETURNING id",
                           (query, label or query[:50]))
            conn.commit()
            fav_id = connector._fetch_one_val(cursor, 'id')
        return fav_id
    finally:
        connector.release_db(conn)

def remove_favorite(fav_id):
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q("DELETE FROM ai_favorites WHERE id = ?"), (fav_id,))
        conn.commit()
    finally:
        connector.release_db(conn)

def get_conversations():
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(connector._q("""
            SELECT conversation_id, MIN(timestamp) as started, MAX(timestamp) as last_msg,
                   COUNT(*) as msg_count,
                   (SELECT content FROM ai_history h2 WHERE h2.conversation_id = ai_history.conversation_id AND h2.role = 'user' ORDER BY h2.timestamp ASC LIMIT 1) as first_message
            FROM ai_history
            WHERE conversation_id IS NOT NULL AND conversation_id != 'default'
            GROUP BY conversation_id
            ORDER BY last_msg DESC
            LIMIT 20
        """))
        rows = [dict(r) for r in cursor.fetchall()]
        return rows
    finally:
        connector.release_db(conn)
