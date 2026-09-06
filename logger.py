"""
Minimal in-memory chat log. Returns the most recent 100 turns.

Same caveat as the Dubai bot: this resets on server restart. For production
logging, swap _CHAT_LOGS for a database write.

Each entry has an incrementing "id" so the website chat widget can poll
"give me anything after id X" during human takeover — this is what lets a
staff reply typed on the dashboard actually reach the patient's open chat
window without them needing to send a new message first.
"""

from datetime import datetime

_CHAT_LOGS: list = []
_next_id = 1


def save_chat_log(session_id: str, user_message: str, ai_response: str):
    global _next_id
    _CHAT_LOGS.append({
        "id": _next_id,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "user_message": user_message,
        "ai_response": ai_response,
    })
    _next_id += 1


def get_chat_logs():
    return _CHAT_LOGS[-100:]


def get_chat_logs_for_session(session_id: str, after_id: int = 0) -> list:
    """Used by the website chat widget's polling loop during human
    takeover. Returns entries for this session with id > after_id, so the
    widget only receives messages it hasn't already rendered."""
    return [
        entry for entry in _CHAT_LOGS
        if entry["session_id"] == session_id and entry["id"] > after_id
    ]
