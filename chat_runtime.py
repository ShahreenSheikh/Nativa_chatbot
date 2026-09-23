"""Production wrapper around ai.get_ai_response."""
import re
import ai as _ai
from service_intents import detect_service_key,is_context_followup,needs_empathy
try: import persistent_store as _ps
except Exception: _ps=None
CONTACT_NUMBER="+971 50 7297197"
SERVICE_LABELS={"nanny_training":"Nanny Training / Newborn Care Training","breastfeeding_support":"Breastfeeding Support","postnatal_support":"Postnatal Recovery Support","antenatal_preparation":"Antenatal Preparation & Education"}
SERVICE_HELP={
"nanny_training":"We have something that can help you with this. Our newborn care and nanny training covers practical areas such as newborn sleep, soothing, feeding support, safe handling and everyday newborn care.",
"breastfeeding_support":"We have something that can help you with this. Our breastfeeding and lactation support can help with latching, positioning, feeding cues and other common feeding difficulties.",
"postnatal_support":"We have something that can help you through this. Our postnatal recovery support provides personalised guidance for physical recovery, feeding, wellbeing and the challenges that can come after birth.",
"antenatal_preparation":"We have something that can help you feel more prepared and supported. Our antenatal preparation provides practical guidance for pregnancy, labour, birth, breastfeeding and the early days with your baby."
}
ALL_FLYERS=["/assets/flyer1.jpg","/assets/flyer2.jpg","/assets/flyer3.jpg","/assets/flyer4.jpg"]
GREETING_ALIASES={"hi","hey","hiya","hello","salam","assalamualaikum","assalamu alaikum","good morning","good afternoon","good evening"}
SERVICE_LIST_PATTERNS=(
    "what other services", "what services", "other services", "services do you have",
    "services you have", "what else do you offer", "what else you offer",
    "what do you offer", "show me your services", "show your services",
    "list your services", "service list", "all services", "other options do you have",
)

def _restore(sid):
    if _ps and _ps.ENABLED and sid not in _ai._sessions:
        saved=_ps.load_session(sid)
        if saved:_ai._sessions[sid]=saved
def _persist(sid):
    if _ps and _ps.ENABLED and sid in _ai._sessions:_ps.save_session(sid,_ai._sessions[sid])
def _normalize_phone(t):
    if not t:return t
    for p in [r"\+?971\s*\(?0?\)?\s*50\s*729\s*7197",r"\+?971\s*50\s*7297\s*197",r"\+?971507297197"]:t=re.sub(p,CONTACT_NUMBER,t,flags=re.I)
    return t
def _clean_location(t):
    if not t:return t
    t=re.sub(r"(?im)^\s*(?:Address|Clinic address)\s*:[^\n]*\n?","",t)
    t=re.sub(r"\bat the clinic\b","as a home-based or virtual service",t,flags=re.I)
    t=re.sub(r"\bvisit (?:our|the) (?:clinic|office)\b","use a home-based or virtual appointment",t,flags=re.I)
    return t
def _remove_demo_notice(t):
    if not t:return t
    t=re.sub(r"(?is)^\s*\[?Note:\s*this is a demo deployment with placeholder\s+data\s*[—-]\s*please verify any details with the clinic\.\]?\s*", "", t)
    return t.lstrip()
def _format(t,source):
    t=_remove_demo_notice(_normalize_phone(_clean_location(t or "")))
    if source!="whatsapp":
        t=t.replace("**","");t=re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)",r"\1",t)
    return t
def _empathy(message,reply,service_key=None):
    if not needs_empathy(message):return reply
    existing=(reply or "").lower()[:500]
    prefix=""
    if not any(x in existing for x in ("i'm sorry","i’m sorry","that sounds","understandably","sorry you're","sorry you’re")):
        prefix="I'm sorry you're dealing with this. That can feel difficult and overwhelming. "
    help_line=SERVICE_HELP.get(service_key,"")
    if help_line and not any(x in existing for x in ("we have something that can help","can help you with this","can help you through this","can help you feel more prepared")):
        prefix+=help_line+" "
    return prefix+(reply or "")
def _is_price_question(m):return any(x in (m or "").lower() for x in ("price","cost","how much","fee","charges","aed"))
def _is_service_list_question(m):
    text=re.sub(r"\s+"," ",(m or "").strip().lower())
    return any(p in text for p in SERVICE_LIST_PATTERNS)
async def _price_fallback(service_key):
    if not service_key:return ""
    labels={"nanny_training":["nanny","caregiver"],"breastfeeding_support":["breastfeeding","lactation"],"postnatal_support":["postnatal","postpartum","recovery"],"antenatal_preparation":["antenatal","prenatal","pregnancy"]}
    services=await _ai.get_services();matches=[]
    for s in services:
        hay=" ".join(str(s.get(k,"") or "") for k in ("service_name","family_name","category","keywords")).lower()
        if any(k in hay for k in labels.get(service_key,[])) and s.get("default_price_aed"):
            matches.append((s.get("service_name"),str(s.get("default_price_aed"))))
    unique=[]
    for x in matches:
        if x not in unique:unique.append(x)
    if len(unique)==1:return f"The current price for {unique[0][0]} is AED {unique[0][1]}."
    if unique:return "Current options are: "+"; ".join(f"{n} — AED {p}" for n,p in unique[:6])+"."
    return "I don't have a confirmed numeric price for that service in the current catalog, so I don't want to guess. Please contact us at +971 50 7297197 for the current price."

async def get_ai_response(session_id: str,user_message: str,source: str="website"):
    _restore(session_id);session=_ai._sessions.get(session_id)
    normalized=(user_message or "").strip().lower().rstrip("!.,")
    previous=(session or {}).get("last_discussed_service_key")
    service_key=detect_service_key(user_message)

    # Global navigation intent: users can ask to browse the clinic's other
    # services even while they are inside a variant picker. The core state
    # machine otherwise interprets any message there as an attempted variant
    # choice and repeats "Sorry, I didn't catch which one". Exit that picker
    # cleanly and ask the core renderer for the real service catalog instead.
    browse_all_services=_is_service_list_question(user_message)
    if session and browse_all_services:
        session["state"]=_ai.STATE_BROWSING
        session.pop("candidate_service",None)
        session.pop("variant_family",None)
        session.pop("awaiting_field",None)
        session["lead"]={}
        service_key=None
        previous=None
        session.pop("last_discussed_service_key",None)
        user_message="what services do you offer?"
        normalized=user_message

    if previous=="breastfeeding_support" and normalized in {"antenatal","antental","prenatal","antenatal prep","antental prep"}:
        service_key="breastfeeding_support"
        user_message="antenatal breastfeeding preparation"
        normalized=user_message

    if session and service_key:session["last_discussed_service_key"]=service_key
    message_for_ai="hello" if normalized in GREETING_ALIASES else user_message
    if session and not service_key and is_context_followup(user_message) and previous:
        message_for_ai=f"{message_for_ai} (context: this refers to {SERVICE_LABELS.get(previous,previous)})"

    result=await _ai.get_ai_response(session_id,message_for_ai,source);session=_ai._sessions.get(session_id)
    if session:
        detected=service_key or previous
        if detected:session["last_discussed_service_key"]=detected
    reply=result.get("reply","")
    active_key=service_key or (session or {}).get("last_discussed_service_key") or previous
    if _is_price_question(user_message) and not re.search(r"\bAED\s*\d",reply or "",re.I):reply=await _price_fallback(active_key)
    reply=_empathy(user_message,reply,active_key)
    result["reply"]=_format(reply,source)

    # A general catalog question should not inherit flyers from the service
    # the user was previously viewing. This also prevents duplicate flyers
    # when navigating away from a specific service.
    if active_key and result.get("reply") and not browse_all_services:
        result["flyers"]=ALL_FLYERS
    else:
        result["flyers"]=[]
    result["service_context"]=active_key
    _persist(session_id);return result
