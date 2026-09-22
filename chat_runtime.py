"""Production wrapper around ai.get_ai_response.
Adds durable session restoration, contextual pronoun resolution, flyer-derived
intent hints, empathetic phrasing, channel formatting, and brand/contact rules
without duplicating the large booking state machine in ai.py.
"""
import re
import ai as _ai
from service_intents import detect_service_key, is_context_followup, needs_empathy
try: import persistent_store as _ps
except Exception: _ps=None

CONTACT_NUMBER="971 50 7297197"
SERVICE_LABELS={
 "nanny_training":"Nanny Training / Newborn Care Training",
 "breastfeeding_support":"Breastfeeding Support",
 "postnatal_support":"Postnatal Recovery Support",
 "antenatal_preparation":"Antenatal Preparation & Education",
}

def _restore(session_id):
    if not (_ps and _ps.ENABLED) or session_id in _ai._sessions:return
    saved=_ps.load_session(session_id)
    if saved:_ai._sessions[session_id]=saved

def _persist(session_id):
    if _ps and _ps.ENABLED and session_id in _ai._sessions:
        _ps.save_session(session_id,_ai._sessions[session_id])

def _normalize_phone(text):
    if not text:return text
    # Normalize every known NativaCare number variant to the client-approved format.
    patterns=[r"\+?971\s*50\s*729\s*7197",r"\+?971\s*\(?0?\)?\s*50\s*729\s*7197",r"\+?971\s*50\s*7297\s*197",r"\+?971507297197"]
    for p in patterns:text=re.sub(p,CONTACT_NUMBER,text,flags=re.I)
    return text

def _strip_physical_office_claims(text):
    if not text:return text
    # NativaCare no longer has a physical office. Avoid leaking stale sheet/site copy.
    text=re.sub(r"(?im)^\s*(?:Address|Clinic address)\s*:[^\n]*\n?","",text)
    text=re.sub(r"\bat the clinic\b","as a home-based or virtual service",text,flags=re.I)
    text=re.sub(r"\bvisit (?:our|the) (?:clinic|office)\b","use a home-based or virtual appointment",text,flags=re.I)
    return text

def _channel_format(text,source):
    text=_normalize_phone(_strip_physical_office_claims(text or ""))
    if source!="whatsapp":
        # Website widget does not render Markdown consistently; remove raw emphasis markers.
        text=text.replace("**","")
        text=re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)",r"\1",text)
    return text

def _empathy_prefix(message,reply):
    if not needs_empathy(message):return reply
    low=(reply or "").lower()
    if any(x in low[:180] for x in ("i'm sorry","i’m sorry","that sounds","understandably","sorry you're","sorry you’re")):return reply
    return "I'm sorry you're dealing with this. That can feel difficult and overwhelming. " + (reply or "")

async def get_ai_response(session_id: str,user_message: str,source: str="website"):
    _restore(session_id)
    session=_ai._sessions.get(session_id)
    service_key=detect_service_key(user_message)
    if session and service_key:session["last_discussed_service_key"]=service_key
    # Resolve "it/this/that" against the last service before NLU sees the turn.
    if session and not service_key and is_context_followup(user_message):
        previous=session.get("last_discussed_service_key")
        if previous:
            user_message_for_ai=f"{user_message} (context: this refers to {SERVICE_LABELS.get(previous,previous)})"
        else:user_message_for_ai=user_message
    else:user_message_for_ai=user_message
    result=await _ai.get_ai_response(session_id,user_message_for_ai,source)
    session=_ai._sessions.get(session_id)
    if session:
        detected=service_key or session.get("last_discussed_service_key")
        if detected:session["last_discussed_service_key"]=detected
    reply=result.get("reply","")
    reply=_empathy_prefix(user_message,reply)
    result["reply"]=_channel_format(reply,source)
    _persist(session_id)
    return result
