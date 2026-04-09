"""AI Agent package — re-exports for backwards compatibility."""
from app.agent.chat import (
    chat, chat_stream, check_rate_limit,
    _get_api_key, _get_model,
    get_favorites, add_favorite, remove_favorite, get_conversations,
)
from app.agent.confirmation import confirm_action, pending_actions
from app.agent.history import get_history, clear_history, load_history, save_history
from app.agent.prompt import build_system_prompt
from app.agent.tools import get_tools_for_role
