"""Conversation history CRUD."""
import json
import logging
import connector

logger = logging.getLogger(__name__)


def load_history(conversation_id, limit=20):
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(
            connector._q("SELECT role, content FROM ai_history WHERE conversation_id=? AND role IN ('user', 'assistant') ORDER BY timestamp DESC LIMIT ?"),
            (conversation_id, limit)
        )
        rows = cursor.fetchall()
        rows = list(rows)
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        connector.release_db(conn)


def save_history(conversation_id, role, content, tool_calls=None):
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        cursor.execute(
            connector._q("INSERT INTO ai_history (role, content, conversation_id, tool_calls) VALUES (?, ?, ?, ?)"),
            (role, content, conversation_id, json.dumps(tool_calls) if tool_calls else None)
        )
        conn.commit()
    finally:
        connector.release_db(conn)


def get_history(conversation_id=None):
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        if conversation_id:
            cursor.execute(
                connector._q("SELECT role, content, timestamp FROM ai_history WHERE conversation_id=? ORDER BY timestamp ASC"),
                (conversation_id,)
            )
        else:
            cursor.execute("SELECT role, content, timestamp FROM ai_history ORDER BY timestamp DESC LIMIT 50")
        rows = [dict(r) for r in cursor.fetchall()]
        return rows
    finally:
        connector.release_db(conn)


def clear_history(conversation_id=None):
    conn = connector.get_db()
    try:
        cursor = connector._cursor(conn)
        if conversation_id:
            cursor.execute(connector._q("DELETE FROM ai_history WHERE conversation_id=?"), (conversation_id,))
        else:
            cursor.execute("DELETE FROM ai_history")
        conn.commit()
    finally:
        connector.release_db(conn)
