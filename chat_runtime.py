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

# =============================================================================
# WhatsApp-only additions. The website chat behaves exactly as before.
#  - a chat that staff are handling gets NO reply of any kind
#  - no more "Sorry, I didn't catch which one": the question is answered, then the options repeat
#  - when a customer asks about one service and the answer has no price, the real price is added
# =============================================================================
from logger import save_chat_log as _save_chat_log

try:
    import db as _db_mod
except Exception:
    _db_mod = None

import contextvars
_WA_CTX = contextvars.ContextVar("wa_ctx", default=False)
_orig_is_price_question = _is_price_question


def _is_price_question(m):
    """WhatsApp: match whole words only. The old check matched "fee" inside "breastfeeding",
    so any breastfeeding message was answered with just a price line."""
    if not _WA_CTX.get():
        return _orig_is_price_question(m)
    return bool(re.search(r"\b(price|prices|pricing|cost|costs|how much|fee|fees|charge|charges|aed)\b",
                          (m or "").lower()))


_CATCH_VARIANT = "Sorry, I didn't catch which one — "
_CATCH_LANG = "I didn't catch that. Please reply with one of the languages we offer"
_QUESTION_STARTS = ("what", "how", "do ", "does ", "can ", "is ", "are ", "which", "when",
                    "where", "why", "tell me", "could", "should", "will ")


def _human_mode_on(session_id):
    try:
        conv = _db_mod.get_conversation(session_id) if _db_mod else None
        return bool(conv and conv.get("human_mode"))
    except Exception:
        return False


def _is_price_question_wa(m):
    token = _WA_CTX.set(True)
    try:
        return _is_price_question(m)
    finally:
        _WA_CTX.reset(token)


def _looks_like_question(m):
    t = (m or "").strip().lower()
    return "?" in t or t.startswith(_QUESTION_STARTS) or _is_price_question_wa(t)


async def _soften_reprompt(session, user_message, reply, source):
    """Replace the blunt 'I didn't catch that' with a helpful answer + the same options."""
    if _CATCH_VARIANT in reply:
        head, options = reply.split(_CATCH_VARIANT, 1)
        options = options[:1].upper() + options[1:]
    else:
        head, options = "", ("Which language would you like for your session? "
                             + " or ".join(getattr(_ai, "SUPPORTED_LANGUAGES", ["English", "Arabic"])) + ".")
    answer = ""
    if session is not None and _looks_like_question(user_message):
        try:
            answer = (await _ai.compose_reply(session, user_message, {"intent": "general", "slots": {}}) or "").strip()
        except Exception:
            answer = ""
    parts = [x for x in (head.strip(), _format(answer, source), options) if x]
    return "\n\n".join(parts)


# ---- keeping track of the topic (the service being discussed) --------------------------------
_KEY_WORDS = {"nanny_training": ["nanny", "caregiver"], "breastfeeding_support": ["breastfeeding", "lactation"],
              "postnatal_support": ["postnatal", "postpartum", "recovery"],
              "antenatal_preparation": ["antenatal", "prenatal", "pregnancy"]}
_CONFUSED = ("i don't have", "i do not have", "i'm not sure", "i am not sure", "can't answer", "cannot answer",
             "not able to find", "don't have enough", "no information")


def _service_rows(key, services):
    out = []
    for s in services:
        hay = " ".join(str(s.get(k, "") or "") for k in ("service_name", "family_name", "category", "keywords")).lower()
        if any(k in hay for k in _KEY_WORDS.get(key, [])):
            out.append(s)
    return out


async def _topic_facts(key):
    """Real catalog facts for the service being discussed (no extra AI call)."""
    try:
        services = await _ai.get_services()
    except Exception:
        return ""
    lines = []
    for s in _service_rows(key, services):
        bits = [str(s.get("service_name", ""))]
        if s.get("default_price_aed"):
            bits.append("AED " + str(s["default_price_aed"]))
        if s.get("duration_minutes"):
            bits.append(str(s["duration_minutes"]) + " min")
        if s.get("location_type"):
            bits.append(str(s["location_type"]).replace("_", " "))
        line = "- " + " | ".join(b for b in bits if b)
        desc = (s.get("short_desc") or s.get("description") or "").strip()
        if desc:
            line += ": " + desc[:180]
        if line not in lines:
            lines.append(line)
        if len(lines) >= 5:
            break
    return "\n".join(lines)


def _ensure_topic_context():
    """Give the answering step the facts of the current service, so follow-up questions stay on topic."""
    current = _ai._build_compose_context
    if getattr(current, "_topic_wrapped", False):
        return

    async def wrapped(intent, slots, session=None):
        text = await current(intent, slots, session)
        try:
            key = (session or {}).get("last_discussed_service_key")
            if _WA_CTX.get() and key in SERVICE_LABELS:
                facts = await _topic_facts(key)
                text += ("\n\nCURRENT TOPIC: the customer is talking about " + SERVICE_LABELS[key] + "."
                         + ("\nFacts:\n" + facts if facts else "")
                         + "\nAnswer their follow-up questions about THIS service. If they name a different service, "
                           "switch to that one. Do not bring up other services unless asked. If their question is "
                           "unclear, ask ONE short clarifying question instead of guessing.")
        except Exception:
            pass
        return text

    wrapped._topic_wrapped = True
    _ai._build_compose_context = wrapped


def _new_session(session_id):
    from datetime import datetime
    return {"id": session_id, "history": [], "lead": {}, "last_booking": None, "past_bookings": [],
            "state": _ai.STATE_BROWSING, "awaiting_field": None, "email_asked": False,
            "demo_mode": False, "last_seen": datetime.utcnow()}


def record_conversation_turn(session_id, role, text):
    """Remember what was said while staff were handling a chat, so the bot has the context later."""
    if not text:
        return
    try:
        _restore(session_id)
        s = _ai._sessions.get(session_id)
        if s is None:
            s = _ai._sessions[session_id] = _new_session(session_id)
        s.setdefault("history", []).append({"role": role, "content": str(text)[:500]})
        s["history"] = s["history"][-12:]
        _persist(session_id)
    except Exception as e:
        print(f"[Context] could not record a turn: {e}")


async def handback_session(session_id, service_key=None, note="", followup_text=""):
    """Turn the bot back ON for a chat staff were handling, with context, so the booking can continue.
    Returns the follow-up text to send to the customer."""
    try:
        try:
            _db_mod.set_human_mode(session_id, False, channel="whatsapp")
        except TypeError:
            _db_mod.set_human_mode(session_id, False)
    except Exception as e:
        print(f"[Handback] ERROR turning the bot on: {e}")
    _restore(session_id)
    s = _ai._sessions.get(session_id)
    if s is None:
        s = _ai._sessions[session_id] = _new_session(session_id)
    key = service_key if service_key in SERVICE_LABELS else s.get("last_discussed_service_key")
    label = SERVICE_LABELS.get(key, "")
    if key:
        s["last_discussed_service_key"] = key
        try:
            rows = _service_rows(key, await _ai.get_services())
            if rows:  # lets "book it" go straight into the normal booking steps
                s["last_recommended"] = [{"service_id": rows[0]["service_id"],
                                          "service_name": rows[0].get("family_name") or rows[0].get("service_name", label)}]
        except Exception:
            pass
    if note:
        s.setdefault("history", []).append({"role": "assistant", "content": "[Staff note for the assistant: " + note[:400] + "]"})
    text = (followup_text or "").strip()
    if not text:
        text = ("Thank you for your patience! Would you like to book " + label + "? Reply 'book it' and I'll show the available times."
                if label else "Thank you for your patience! I'm here again if you'd like to continue with your booking. Which service would you like to book?")
    s.setdefault("history", []).append({"role": "assistant", "content": text})
    s["history"] = s["history"][-12:]
    _persist(session_id)
    return text


_core_get_ai_response = get_ai_response  # the original function, defined above


async def get_ai_response(session_id: str, user_message: str, source: str = "website"):
    if source != "whatsapp":
        return await _core_get_ai_response(session_id, user_message, source)

    # 1) Staff are handling this chat: say nothing at all (not even price/empathy/emergency text).
    if _human_mode_on(session_id):
        try:
            _save_chat_log(session_id, user_message, "[human_mode — awaiting staff reply]")
        except Exception:
            pass
        record_conversation_turn(session_id, "user", user_message)
        return {"reply": "", "flyers": [], "human_mode": True}

    _ensure_topic_context()
    token = _WA_CTX.set(True)
    try:
        result = await _core_get_ai_response(session_id, user_message, source)
    finally:
        _WA_CTX.reset(token)
    if result.get("human_mode"):
        result["reply"] = ""
        result["flyers"] = []
        return result

    reply = result.get("reply") or ""
    session = _ai._sessions.get(session_id)

    # 1b) Price asked with no service in mind used to produce an EMPTY reply (customer got nothing).
    if not reply and _is_price_question_wa(user_message):
        try:
            reply = (await _ai.compose_reply(session, user_message, {"intent": "price_question", "slots": {}}) or "").strip()
        except Exception:
            reply = ""
        if not reply:
            reply = "Which service would you like the price for? Reply with the service name and I'll share it."
        reply = _format(reply, source)

    # 2) No more "Sorry, I didn't catch which one" / "I didn't catch that".
    if reply and (_CATCH_VARIANT in reply or _CATCH_LANG in reply):
        reply = await _soften_reprompt(session, user_message, reply, source)

    # 3) Asked about one service but the answer shows no price: add the real price from the catalog.
    service_key = detect_service_key(user_message)
    if (reply and service_key and not _is_service_list_question(user_message)
            and not re.search(r"\bAED\s*\d", reply, re.I)
            and result.get("state") in (_ai.STATE_BROWSING, _ai.STATE_BOOKED, _ai.STATE_SERVICE_SELECTING)):
        try:
            price_text = await _price_fallback(service_key)
        except Exception:
            price_text = ""
        if re.search(r"\bAED\s*\d", price_text or ""):
            reply = reply.rstrip() + "\n\n" + _format(price_text, source)

    # 4) Confused? Ask ONE short question instead of guessing or going silent.
    low = reply.lower()
    key = (session or {}).get("last_discussed_service_key")
    ask = (f"Just to make sure I help with the right thing: is this about {SERVICE_LABELS[key]}, or a different service?"
           if key in SERVICE_LABELS else
           "Could you tell me a little more, or which service you mean (Antenatal, Postnatal, Breastfeeding or Nanny Training)?")
    if not reply:
        reply = ask
    elif (any(p in low for p in _CONFUSED) and "contact us" not in low and not reply.rstrip().endswith("?")):
        reply = reply.rstrip() + "\n\n" + ask

    result["reply"] = reply
    return result