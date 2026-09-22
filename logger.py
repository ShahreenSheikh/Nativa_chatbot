"""Chat log API. Uses PostgreSQL when DATABASE_URL is configured; keeps the
old in-memory fallback for local development."""
from datetime import datetime
try:
    import persistent_store as _ps
except Exception:
    _ps = None

_CHAT_LOGS=[]
_next_id=1

def save_chat_log(session_id: str, user_message: str, ai_response: str):
    global _next_id
    if _ps and _ps.ENABLED:
        return _ps.save_chat_log(session_id,user_message,ai_response)
    _CHAT_LOGS.append({"id":_next_id,"timestamp":datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),"session_id":session_id,"user_message":user_message,"ai_response":ai_response})
    _next_id+=1

def get_chat_logs(limit: int=100, offset: int=0):
    if _ps and _ps.ENABLED:return _ps.get_chat_logs(limit=limit,offset=offset)
    return _CHAT_LOGS[-limit:]

def get_chat_logs_for_session(session_id: str, after_id: int=0) -> list:
    if _ps and _ps.ENABLED:return _ps.get_chat_logs_for_session(session_id,after_id)
    return [x for x in _CHAT_LOGS if x["session_id"]==session_id and x["id"]>after_id]
