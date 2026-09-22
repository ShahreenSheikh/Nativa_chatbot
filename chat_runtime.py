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
# The original clinic flyers are served by the backend. We return media
# separately from reply text so website/widget clients can render real images.
ALL_FLYERS=["/assets/flyer1.jpg","/assets/flyer2.jpg","/assets/flyer3.jpg","/assets/flyer4.jpg"]
GREETING_ALIASES={"hi","hey","hiya","hello","salam","assalamualaikum","assalamu alaikum","good morning","good afternoon","good evening"}

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
def _format(t,source):
    t=_normalize_phone(_clean_location(t or ""))
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

    # Context fix: after Breastfeeding & Lactation options have been shown,
    # a short reply such as "antenatal" / typo "antental" means the
    # Antenatal Breastfeeding Preparation option from THAT list. It must not
    # jump to the unrelated Antenatal Education & Preparation family.
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

    # Let capable clients display the actual clinic flyers as images. Do not
    # attach them to greetings; attach them when a service is being discussed.
    if active_key and result.get("reply"):
        result["flyers"]=ALL_FLYERS
    else:
        result["flyers"]=[]
    result["service_context"]=active_key
    _persist(session_id);return result
