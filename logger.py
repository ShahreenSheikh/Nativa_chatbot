"""
Minimal in-memory chat log. Returns the most recent 100 turns.

Same caveat as the Dubai bot: this resets on server restart. For production
logging, swap _CHAT_LOGS for a database write.
"""

from datetime import datetime

_CHAT_LOGS: list = []


def save_chat_log(session_id: str, user_message: str, ai_response: str):
    _CHAT_LOGS.append({
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "user_message": user_message,
        "ai_response": ai_response,
    })


def get_chat_logs():
    return _CHAT_LOGS[-100:]
