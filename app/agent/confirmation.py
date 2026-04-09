"""Write confirmation flow: pending actions, confirm/reject, action descriptions."""
import time
import json
import logging
import threading
import uuid

import anthropic
import connector

logger = logging.getLogger(__name__)

# Pending confirmations: { state_id: { messages, tool_block, conversation_id, expires_at } }
pending_actions = {}
_pending_lock = threading.Lock()


def _cleanup_pending():
    with _pending_lock:
        now = time.time()
        expired = [k for k, v in pending_actions.items() if v["expires_at"] < now]
        for k in expired:
            del pending_actions[k]


def confirm_action(state_id, confirmed):
    from app.agent.history import save_history
    from app.agent.executors import TOOL_EXECUTORS
    from app.agent.tools import TOOL_DEFINITIONS
    from app.agent.chat import _get_api_key

    _cleanup_pending()
    with _pending_lock:
        state = pending_actions.pop(state_id, None)
    if not state:
        return {"type": "error", "content": "This action has expired. Please try again."}

    conversation_id = state["conversation_id"]
    tool_block = state["tool_block"]
    tool_name = tool_block["name"]
    tool_input = tool_block["input"]

    if not confirmed:
        save_history(conversation_id, "user", state["user_message"])
        save_history(conversation_id, "assistant", f"Action cancelled: {tool_name}")
        return {"type": "message", "content": "Action cancelled.", "conversation_id": conversation_id}

    # Execute the write tool
    executor = TOOL_EXECUTORS.get(tool_name)
    if not executor:
        return {"type": "error", "content": f"Unknown tool: {tool_name}"}

    result = executor(tool_input)

    # Resume the agent loop with the tool result
    messages = state["messages"]
    messages.append({"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": tool_block["id"],
        "content": json.dumps(result, default=str)
    }]})

    api_key = _get_api_key()
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=state["model"],
            max_tokens=4096,
            system=state["system_prompt"],
            tools=TOOL_DEFINITIONS,
            messages=messages
        )

        final_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

        save_history(conversation_id, "user", state["user_message"])
        save_history(conversation_id, "assistant", final_text, [{"tool": tool_name, "confirmed": True}])

        return {
            "type": "message",
            "content": final_text,
            "conversation_id": conversation_id,
            "tool_calls_summary": [{"tool": tool_name, "description": "Confirmed and executed"}]
        }
    except Exception as e:
        logger.error(f"Agent error after confirmation: {e}", exc_info=True)
        return {"type": "error", "content": "An error occurred after confirmation. Please try again.", "conversation_id": conversation_id}


def _describe_write_action(tool_name, tool_input):
    if tool_name == "create_estimate":
        items = tool_input.get("items", [])
        total = sum(i.get("units", 1) * i.get("price", 0) for i in items)
        return f"Create an estimate with {len(items)} items (subtotal: {total:,.2f} EUR)"
    elif tool_name == "create_invoice":
        items = tool_input.get("items", [])
        total = sum(i.get("units", 1) * i.get("price", 0) for i in items)
        return f"Create an invoice with {len(items)} items (subtotal: {total:,.2f} EUR)"
    elif tool_name == "send_document":
        emails = tool_input.get("emails", ["contact's email on file"])
        return f"Send {tool_input.get('doc_type', 'document')} {tool_input.get('doc_id', '?')} to {', '.join(emails)}"
    elif tool_name == "create_contact":
        return f"Create contact: {tool_input.get('name', 'Unknown')}"
    elif tool_name == "update_invoice_status":
        status_labels = {0: "borrador", 1: "aprobada", 2: "partial", 3: "paid", 4: "overdue", 5: "cancelled"}
        new_status = tool_input.get("status")
        st = status_labels.get(new_status, str(new_status))
        hacienda_warn = " ⚠️ ENVIARÁ A HACIENDA" if new_status == 1 else ""
        return f"Update {tool_input.get('doc_type')} {tool_input.get('doc_id')} status to {st}{hacienda_warn}"
    elif tool_name == "convert_estimate_to_invoice":
        return f"Convert estimate {tool_input.get('estimate_id', '?')} to invoice (borrador)"
    return f"Execute {tool_name}"
