# Fase 5b: Split ai_agent.py — Design Spec

## Context

ai_agent.py is 1630 lines handling: tool definitions (JSON schemas), 19 tool executors, system prompt building, conversation history, chat/streaming, rate limiting, and write confirmation. Splitting into `app/agent/` with 6 focused modules.

## Architecture

```
app/agent/
  __init__.py        # Re-exports for facade
  tools.py           # Tool JSON schemas + get_tools_for_role() (~330 lines)
  executors.py       # 19 exec_* functions (~655 lines)
  prompt.py          # build_system_prompt() + load_skills() (~100 lines)
  history.py         # load_history, save_history, get_history, clear_history (~65 lines)
  chat.py            # chat(), chat_stream(), rate limiting, _get_api_key, _get_model (~335 lines)
  confirmation.py    # pending_actions, confirm_action, describe_write_action, _cleanup_pending (~85 lines)
ai_agent.py          # Facade: re-exports from app/agent/* (~30 lines)
```

## Import Rule

`app/agent/` modules import from `app.db.connection`, `connector`, `app.holded.*`, and peer `app.agent.*` modules. The facade `ai_agent.py` re-exports for backwards compat.

## Key Dependencies

- `executors.py` → `connector` (DB queries), `write_gateway` (gateway.execute), `reports` (PDF generation)
- `chat.py` → `tools.py`, `executors.py`, `prompt.py`, `history.py`, `confirmation.py`, `anthropic` API
- `confirmation.py` → standalone (no agent imports, uses threading for pending_actions TTL)
- `prompt.py` → `connector` (DB schema query), `tools.py` (tool list)
- `history.py` → `connector` (DB read/write)

## Dead Code

`_prepare_line_items` (~L603-615 in current ai_agent.py) is dead code. Delete during extraction, don't move.
