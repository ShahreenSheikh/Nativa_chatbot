"""FastAPI entry point for the NativaCare chatbot."""
import os,re,uuid,base64
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI,HTTPException,Query,Request,Response,UploadFile,File,Form,Header,Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse,JSONResponse,HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from models import ChatRequest
from ai import get_ai_response,record_staff_reply
from database import get_saved_appointments
from logger import get_chat_logs,get_chat_logs_for_session
from availability import get_availability,get_next_available_days
from whatsapp_service import send_whatsapp_message,send_whatsapp_document,verify_webhook_signature,parse_incoming_messages,parse_message_echoes,download_media,WHATSAPP_VERIFY_TOKEN,send_whatsapp_image
import db; db.init_db()
import payments,invoice_pdf
CLINIC_NAME=os.getenv("CLINIC_NAME","NativaCare");ADMIN_API_KEY=os.getenv("ADMIN_API_KEY","").strip()
def require_admin(x_admin_key:str=Header(default="")):
    if ADMIN_API_KEY and x_admin_key!=ADMIN_API_KEY:raise HTTPException(status_code=401,detail="Missing or invalid X-Admin-Key header")
UPLOADS_DIR=Path(os.getenv("UPLOADS_DIR","uploads"));UPLOADS_DIR.mkdir(parents=True,exist_ok=True)
app=FastAPI(title=f"{CLINIC_NAME} — Midwifery Chatbot",version="0.1.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
_STATIC_DIR=Path(__file__).parent/"static"
if _STATIC_DIR.exists():app.mount("/static",StaticFiles(directory=str(_STATIC_DIR)),name="static")
_ASSETS_DIR=Path(__file__).parent/"assets"
if _ASSETS_DIR.exists():app.mount("/assets",StaticFiles(directory=str(_ASSETS_DIR)),name="assets")
app.mount("/uploads",StaticFiles(directory=str(UPLOADS_DIR)),name="uploads")
_DASHBOARD_DIR=Path(__file__).parent/"dashboard"
if _DASHBOARD_DIR.exists():app.mount("/dashboard",StaticFiles(directory=str(_DASHBOARD_DIR),html=True),name="dashboard")
_WIDGET_PATH=Path(__file__).parent/"widget.js"
_SERVICE_FLYERS={"postnatal":"/assets/flyer1.jpg","antenatal":"/assets/flyer2.jpg","nanny":"/assets/flyer3.jpg","breastfeeding":"/assets/flyer4.jpg"}
_SERVICE_PRICE_PDFS={"antenatal":"/assets/NativaCare_Antenatal_Education.pdf","postnatal":"/assets/NativaCare_Postnatal_Recovery.pdf","breastfeeding":"/assets/NativaCare_Breastfeeding_Support.pdf","nanny":"/assets/NativaCare_Nanny_Training.pdf"}
# Send each service information flyer followed by its matching pricing/details PDF.
_ALL_FLYERS=[
    path
    for key in ("postnatal","antenatal","nanny","breastfeeding")
    for path in (_SERVICE_FLYERS[key],_SERVICE_PRICE_PDFS[key])
]

# Last numbered service menu shown per conversation. This lets patients say
# "4", "option 4", "4th one", "fourth one", etc. and reliably refer to
# the exact menu they just saw instead of asking the LLM to guess.
_NUMBERED_SERVICE_OPTIONS={}
_ORDINAL_WORDS={"first":1,"one":1,"second":2,"two":2,"third":3,"three":3,"fourth":4,"four":4,"fifth":5,"five":5,"sixth":6,"six":6,"seventh":7,"seven":7,"eighth":8,"eight":8,"ninth":9,"nine":9,"tenth":10,"ten":10}

def _numbered_choice(message:str):
    text=(message or "").lower().strip()
    # Deliberately only accept short choice-like replies so a phone number,
    # date, price, or normal sentence containing a number is never rewritten.
    if len(text)>35:return None
    m=re.fullmatch(r"(?:option|number|no\.?|the)?\s*#?\s*(\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:one|option))?",text)
    if m:return int(m.group(1))
    m=re.fullmatch(r"(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)(?:\s+(?:one|option))?",text)
    if m:return _ORDINAL_WORDS.get(m.group(1))
    return None

def _resolve_numbered_service_choice(session_id:str,message:str):
    idx=_numbered_choice(message)
    options=_NUMBERED_SERVICE_OPTIONS.get(session_id) or []
    if idx and 1<=idx<=len(options):return options[idx-1]
    return message

def _number_service_menu(session_id:str,reply:str):
    """Number service/service-variant bullets and remember their labels.

    We intentionally do NOT number language, date/time, payment or contact
    prompts. The menu must contain a service-selection cue and bullet rows.
    """
    text=reply or ""
    lc=text.lower()
    service_cues=("which would you like","which service","services:","service options","has a few options","would you like to book")
    exclude=("in which language would you like","available times","available days","reply with a date","reply with a time","phone number","email address","booking summary","payment")
    if not any(c in lc for c in service_cues) or any(x in lc for x in exclude):
        return text
    lines=text.splitlines();options=[];out=[]
    for line in lines:
        stripped=line.strip()
        if stripped.startswith("•"):
            label=stripped[1:].strip()
            # Keep the complete visible option text so the AI can resolve the
            # exact service/variant, while removing trailing descriptive prose
            # only when it is clearly on a separate indented line (handled by
            # the existing formatter, not here).
            if label:
                options.append(label)
                indent=line[:len(line)-len(line.lstrip())]
                out.append(f"{indent}{len(options)}. {label}")
                continue
        out.append(line)
    if options:
        _NUMBERED_SERVICE_OPTIONS[session_id]=options
        return "\n".join(out)
    return text

def _select_flyers(user_message:str,reply:str,state_after:str="",service_name:str=""):
    """Return only the selected service's information flyer or pricing PDF.

    Service catalogues are text-only. A customer selection sends the
    information image; a specific pricing question sends only its PDF.
    Never infer a selection merely because the assistant listed services.
    """
    user=(user_message or "").lower()
    answer=(reply or "").lower()
    groups={
        "breastfeeding":("breastfeeding","breastfeed","latch","latching","lactation",
                         "colostrum","milk supply"),
        "antenatal":("antenatal","prenatal","pregnancy preparation",
                     "birth preparation","childbirth education"),
        "postnatal":("postnatal","postpartum","after birth","recovery support",
                     "new mother recovery"),
        "nanny":("nanny training","caregiver training","care giver training",
                 "newborn training","newborn care training"),
    }
    price_question=bool(re.search(
        r"\b(price|pricing|cost|costs|fee|fees|how much|rate|rates)\b", user
    ))
    broad=("all services","all your services","what services","which services",
           "services do you offer","services you offer","other services",
           "what other services","show me all","everything you offer")
    if any(p in user for p in broad):
        return []

    selected=[]
    for key,words in groups.items():
        if any(w in user for w in words):
            selected.append(key)

    # Resolve pronoun follow-ups only when the current response identifies
    # exactly one service (e.g. "how much would it cost?" after selection).
    if not selected and service_name:
        service_lc=str(service_name).lower()
        for key,words in groups.items():
            if any(w in service_lc for w in words):
                selected.append(key)
    if not selected and (price_question or re.fullmatch(
        r"\s*(yes|yeah|yep|sure|okay|ok|that's the one|that one|i want that)\s*[.!]?\s*",
        user
    )):
        for key,words in groups.items():
            if any(w in answer for w in words):
                selected.append(key)

    # Do not send a batch when the reply mentions multiple services.
    if len(selected) != 1:
        return []
    key=selected[0]
    if price_question:
        return [_SERVICE_PRICE_PDFS[key]]
    return [_SERVICE_FLYERS[key]]

def _normalize_language_prompt(reply):
    if "In which language would you like your session?" not in (reply or ""):return reply
    prefix=reply.split("In which language would you like your session?",1)[0]
    return prefix+"In which language would you like your session?\n\n  • English\n  • Arabic (upon availability)\n  • French\n\nReply with 'English', 'Arabic', or 'French'."

@app.get("/widget.js",include_in_schema=False)
def widget_js():
    if not _WIDGET_PATH.exists():raise HTTPException(status_code=404,detail="widget.js not found")
    return Response(content=_WIDGET_PATH.read_text(encoding="utf-8"),media_type="application/javascript",headers={"Cache-Control":"no-cache, no-store, must-revalidate"})
@app.get("/",include_in_schema=False)
def root():
    index=_STATIC_DIR/"index.html"
    return FileResponse(index) if index.exists() else JSONResponse({"status":"ok","service":f"{CLINIC_NAME} — Midwifery Chatbot","docs":"/docs"})
@app.get("/health")
def health():return {"status":"ok","service":f"{CLINIC_NAME} — Midwifery Chatbot","version":"0.1.0"}
@app.post("/chat")
async def chat(req:ChatRequest):
    try:
        resolved=_resolve_numbered_service_choice(req.session_id,req.message)
        result=await get_ai_response(session_id=req.session_id,user_message=resolved,source=req.source or "website")
        result["session_id"]=req.session_id
        result["reply"]=_normalize_language_prompt(result.get("reply",""))
        result["reply"]=_number_service_menu(req.session_id,result.get("reply",""))
        # Return attachment metadata to every client, including the WhatsApp QA simulator.\n        # This only describes the files to attach; it does not send WhatsApp messages.\n        # Keep simulator previews consistent with WhatsApp: the same PDF
        # is shown only once per session in any 15-minute window.
        flyer_paths = _select_flyers(resolved,result.get("reply",""),result.get("state",""),result.get("service_name") or "")
        sent = _FLYERS_SENT.setdefault(req.session_id, {})
        now = time.time()
        result["flyers"] = []
        for path in flyer_paths:
            if now - sent.get(path, 0) < _PDF_RESEND_TTL:
                continue
            result["flyers"].append(path)
            sent[path] = now
        # Match WhatsApp exactly: suppress the separate pricing text only
        # when a pricing PDF is actually included in this response. If the
        # 15-minute attachment cooldown prevents sending it, keep the text
        # fallback rather than returning an empty simulator reply.
        if any(str(path).lower().endswith(".pdf") for path in result["flyers"]):
            result["reply"] = ""
        return result
    except Exception as e:print(f"[Route Error]: {e}");raise HTTPException(status_code=500,detail="Internal server error")
@app.get("/chat/{session_id}/poll",include_in_schema=False)
def poll_chat(session_id:str,after_id:int=0):
    entries=get_chat_logs_for_session(session_id,after_id=after_id);return {"messages":[{"id":e["id"],"message":e["ai_response"],"timestamp":e["timestamp"]} for e in entries if e["user_message"]=="[staff]"]}
@app.get("/appointments",dependencies=[Depends(require_admin)])
def appointments():return {"appointments":get_saved_appointments()}
class AppointmentStatusRequest(BaseModel):status:str
@app.patch("/appointments/{appointment_id}/status",dependencies=[Depends(require_admin)])
def update_appointment_status(appointment_id:int,body:AppointmentStatusRequest):db.update_appointment_status(appointment_id,body.status);return {"id":appointment_id,"status":body.status}
@app.get("/chat-logs",dependencies=[Depends(require_admin)])
def chat_logs():return {"chat_logs":get_chat_logs()}
@app.get("/availability")
async def availability(service_id:str,date:str):return await get_availability(service_id,date)
@app.get("/availability/next")
async def availability_next(service_id:str,num_days:int=7):return await get_next_available_days(service_id,num_days=num_days)

_IMAGE_TYPES={"image/jpeg","image/png"};_PDF_TYPE="application/pdf";_IMAGE_MAX=10*1024*1024;_PDF_MAX=15*1024*1024
@app.post("/invoices/{invoice_id}/proof")
async def upload_payment_proof(invoice_id:int,file:UploadFile=File(...)):
    invoice=db.get_invoice(invoice_id)
    if not invoice:raise HTTPException(status_code=404,detail="Invoice not found")
    ctype=(file.content_type or "").lower();name=file.filename or "payment-proof"
    ext=Path(name).suffix.lower()
    if ctype not in _IMAGE_TYPES|{_PDF_TYPE} or (ctype==_PDF_TYPE and ext!=".pdf") or (ctype in _IMAGE_TYPES and ext not in {".jpg",".jpeg",".png"}):raise HTTPException(status_code=415,detail="Payment proof must be JPG, JPEG, PNG or PDF.")
    limit=_PDF_MAX if ctype==_PDF_TYPE else _IMAGE_MAX;data=await file.read(limit+1)
    if len(data)>limit:raise HTTPException(status_code=413,detail=("PDF is too large. Maximum size is 15 MB." if ctype==_PDF_TYPE else "Image is too large. Maximum size is 10 MB."))
    valid=(ctype==_PDF_TYPE and data.startswith(b"%PDF-")) or (ctype=="image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or (ctype=="image/jpeg" and data.startswith(b"\xff\xd8\xff"))
    if not valid:raise HTTPException(status_code=415,detail="The uploaded file does not match its declared file type.")
    proof=db.add_payment_proof(invoice_id,source="website",file_bytes=data,content_type=ctype,original_filename=name);db.update_invoice_status(invoice_id,"submitted");updated=db.get_invoice(invoice_id)
    try:await payments.notify_owner_of_payment_proof(updated)
    except Exception as e:print(f"[Payment proof notification] {e}")
    return {"status":"submitted","proof":{"id":proof["id"],"file_path":proof["file_path"],"content_type":ctype,"original_filename":name,"file_size":len(data)}}
@app.get("/payment-proofs/{proof_id}")
def payment_proof_file(proof_id:int):
    proof=db.get_payment_proof(proof_id)
    if not proof or not proof.get("file_data_b64"):raise HTTPException(status_code=404,detail="Payment proof not found")
    try:data=base64.b64decode(proof["file_data_b64"])
    except Exception:raise HTTPException(status_code=500,detail="Stored payment proof is invalid")
    filename=proof.get("original_filename") or f"payment-proof-{proof_id}";disposition="inline"
    return Response(content=data,media_type=proof.get("content_type") or "application/octet-stream",headers={"Content-Disposition":f'{disposition}; filename="{filename.replace(chr(34),"")}"',"Cache-Control":"private, max-age=300"})
@app.get("/invoices",dependencies=[Depends(require_admin)])
def invoices(status:str=""):return {"invoices":db.list_invoices(status or None)}
@app.get("/invoices/{invoice_id}",dependencies=[Depends(require_admin)])
def invoice_detail(invoice_id:int):
    inv=db.get_invoice(invoice_id)
    if not inv:raise HTTPException(status_code=404,detail="Invoice not found")
    proofs=db.list_proofs_for_invoice(invoice_id)
    for p in proofs:p.pop("file_data_b64",None)
    inv["proofs"]=proofs;return {"invoice":inv}
@app.post("/invoices/{invoice_id}/approve",dependencies=[Depends(require_admin)])
async def approve_payment(invoice_id:int):return await payments.approve_invoice(invoice_id)
class RejectBody(BaseModel):reason:str=""
@app.post("/invoices/{invoice_id}/reject",dependencies=[Depends(require_admin)])
async def reject_payment(invoice_id:int,body:RejectBody):return await payments.reject_invoice(invoice_id,body.reason)
@app.get("/webhook/whatsapp")
def verify_whatsapp_webhook(request:Request):
    q=request.query_params
    if q.get("hub.mode")=="subscribe" and q.get("hub.verify_token")==WHATSAPP_VERIFY_TOKEN:return Response(content=q.get("hub.challenge","") ,media_type="text/plain")
    raise HTTPException(status_code=403,detail="Verification failed")
import asyncio
import time

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    raw = await request.body()
    if not verify_webhook_signature(raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}
    # Answer Meta straight away; the AI work runs in the background so Meta does not
    # time out and re-send the same message (which would cause double replies).
    task = asyncio.create_task(_process_whatsapp_payload(payload))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WhatsApp helpers (used only by the webhook above)
# ---------------------------------------------------------------------------
_PUBLIC_BASE = (os.getenv("PUBLIC_BASE_URL") or os.getenv("FRONTEND_BASE_URL") or "").rstrip("/")
_GREETING_WORDS = {"hi", "hii", "hey", "hiya", "hello", "salam", "assalamualaikum",
                   "assalamu alaikum", "good morning", "good afternoon", "good evening"}
_GREETING_FLYER_TTL = 24 * 3600   # flyers 1-4 are sent once per customer per day with the greeting
_SERVICE_FLYER_TTL = 15 * 60        # each information flyer has a per-customer 15-minute cooldown
_PDF_RESEND_TTL = 15 * 60           # do not resend the same PDF to a customer within 15 minutes
_SEEN_MSG_IDS: dict = {}          # webhook message id -> time (duplicate guard)
_BOT_MSG_IDS: dict = {}           # ids of messages the BOT sent (never treated as staff)
_FLYERS_SENT: dict = {}           # session id -> {flyer path: time sent}
_SESSION_LOCKS: dict = {}         # one lock per customer so their messages stay in order
_BG_TASKS: set = set()
if not _PUBLIC_BASE:
    print("[WhatsApp] WARNING: PUBLIC_BASE_URL / FRONTEND_BASE_URL is not set - flyers cannot be sent on WhatsApp.")


def _prune(d: dict, ttl: float):
    now = time.time()
    for k in [k for k, t in d.items() if now - t > ttl]:
        d.pop(k, None)


def _seen_before(message_id: str) -> bool:
    if not message_id:
        return False
    _prune(_SEEN_MSG_IDS, 6 * 3600)
    if message_id in _SEEN_MSG_IDS:
        return True
    _SEEN_MSG_IDS[message_id] = time.time()
    return False


def _mask(number: str) -> str:
    return "***" + number[-4:] if number else ""


def _is_greeting(text: str) -> bool:
    return re.sub(r"[^a-z ]", "", (text or "").lower()).strip() in _GREETING_WORDS


def _flyer_urls(session_id: str, paths: list, ttl: float) -> list:
    """Public URLs of the flyers still to be sent to this customer (and marks them as sent)."""
    if not _PUBLIC_BASE:
        return []
    sent = _FLYERS_SENT.setdefault(session_id, {})
    now = time.time()
    urls = []
    for p in paths:
        # Every individual flyer and pricing PDF has its own per-customer
        # 15-minute cooldown, independent of the other service materials.
        effective_ttl = _PDF_RESEND_TTL
        if now - sent.get(p, 0) < effective_ttl:
            continue
        sent[p] = now
        urls.append(_PUBLIC_BASE + p)
    return urls


async def _send_flyers(to: str, urls: list):
    for u in urls:
        if u.lower().endswith(".pdf"):
            filename = Path(u.split("?", 1)[0]).name
            # The PDF itself is the complete pricing response; no caption.
            r = await send_whatsapp_document(to, u, filename=filename, caption="")
        else:
            r = await send_whatsapp_image(to, u)
        if r.get("status") != "sent":
            print(f"[WhatsApp] Service material not sent ({r.get('status')}); carrying on")


def _remember_bot_message(sent: dict):
    try:
        mid = ((sent.get("response") or {}).get("messages") or [{}])[0].get("id")
        if mid:
            _prune(_BOT_MSG_IDS, 6 * 3600)
            _BOT_MSG_IDS[mid] = time.time()
    except Exception:
        pass


async def _handle_staff_echoes(payload: dict):
    """A person replied from the WhatsApp Business app: the bot stops for that chat."""
    for echo in parse_message_echoes(payload):
        customer = echo.get("to", "")
        echo_id = echo.get("id", "")
        if not customer or echo_id in _BOT_MSG_IDS or _seen_before(echo_id):
            continue
        sid = "wa_" + customer
        try:
            from logger import save_chat_log as _save_chat_log
            staff_text = echo.get("text") or f"[{echo.get('type', 'message')}]"
            _save_chat_log(sid, "[staff]", staff_text)
            from chat_runtime import record_conversation_turn
            record_conversation_turn(sid, "assistant", "[Staff] " + staff_text)
        except Exception as e:
            print(f"[Takeover] could not log staff message: {e}")
        try:
            try:
                db.set_human_mode(sid, True, channel="whatsapp", contact=customer)
            except TypeError:
                db.set_human_mode(sid, True)
            print(f"[Takeover] staff replied to {_mask(customer)}: bot stopped for this chat")
        except Exception as e:
            print(f"[Takeover] ERROR - could not stop the bot for {_mask(customer)}: {e}")


async def _handle_customer_message(msg: dict):
    sender = msg.get("from", "")
    text = msg.get("text", "")
    if not sender:
        return
    if _seen_before(msg.get("id", "")):
        print(f"[WhatsApp] Duplicate delivery ignored ({_mask(sender)})")
        return
    sid = "wa_" + sender
    lock = _SESSION_LOCKS.setdefault(sid, asyncio.Lock())
    async with lock:
        # Payment screenshots sent directly in WhatsApp are not accepted as
        # payment proof. Direct customers to the secure invoice upload page.
        # Respect staff takeover: once a human is handling this chat, the bot
        # must remain completely silent, including for image messages.
        if msg.get("type") == "image":
            try:
                conversation = db.get_conversation(sid) or {}
                if conversation.get("human_mode"):
                    return
            except Exception as e:
                print(f"[WhatsApp] Could not check human mode for image from {_mask(sender)}: {e}")
                return
            fallback = (
                "Thank you for sharing the screenshot! To ensure your payment "
                "is recorded and reviewed by our team, please upload it using "
                "the secure payment submission link we shared with you. "
                "Payments and bookings are only confirmed after our team has reviewed them."
            )
            _remember_bot_message(await send_whatsapp_message(sender, fallback))
            return
        if not text:
            return
        resolved = _resolve_numbered_service_choice(sid, text)
        result = await get_ai_response(sid, resolved, "whatsapp")
        reply = result.get("reply")
        if not reply:
            return  # staff are handling this chat (or nothing to say)
        reply = _normalize_language_prompt(reply)
        reply = _number_service_menu(sid, reply)
        greeting = _is_greeting(text)
        # Service catalogues and recommendations are text-only. Attach a
        # single service flyer only after the customer selects that service;
        # attach its pricing PDF only when they ask about its price.
        state_after = result.get("state", "")
        selected_material = _select_flyers(
            resolved, reply, state_after, result.get("service_name") or ""
        )
        before_urls = (
            _flyer_urls(sid, selected_material, _SERVICE_FLYER_TTL)
            if not greeting else []
        )
        if before_urls:
            await _send_flyers(sender, before_urls)
        # Match the simulator: suppress pricing text only when a pricing PDF
        # was actually sent. If the 15-minute cooldown blocks the PDF, retain
        # the normal text fallback.
        if before_urls and any(str(url).lower().endswith(".pdf") for url in before_urls):
            return
        _remember_bot_message(await send_whatsapp_message(sender, reply))


async def _process_whatsapp_payload(payload: dict):
    try:
        await _handle_staff_echoes(payload)
    except Exception as e:
        print(f"[WhatsApp] echo handling error: {e}")
    try:
        messages = parse_incoming_messages(payload)
    except Exception as e:
        print(f"[WhatsApp] could not read payload: {e}")
        return
    for msg in messages:
        try:
            await _handle_customer_message(msg)
        except Exception as e:
            print(f"[WhatsApp] error handling one message: {e}")


# ---------------------------------------------------------------------------
# Hand a chat back to the bot (until the dashboard has a button for it).
# Open https://YOUR-APP/docs -> POST /admin/whatsapp/handback -> "Try it out".
# ---------------------------------------------------------------------------
class HandbackBody(BaseModel):
    phone: str = ""      # the customer's WhatsApp number, digits only, e.g. 971501234567
    service: str = ""    # optional: nanny_training / breastfeeding_support / postnatal_support / antenatal_preparation
    note: str = ""       # optional context for the bot, e.g. "agreed postnatal visit, home, Sunday afternoon"
    message: str = ""    # optional text to send the customer now (otherwise a short default follow-up)
    send: bool = True


@app.post("/admin/whatsapp/handback")
async def whatsapp_handback(body: HandbackBody, x_admin_key: str = Header(default="")):
    import hmac
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Set ADMIN_API_KEY in Railway first.")
    if not hmac.compare_digest(x_admin_key or "", ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Key header")
    digits = re.sub(r"\D", "", body.phone)
    if len(digits) < 8:
        raise HTTPException(status_code=400, detail="phone must be the customer's WhatsApp number")
    from chat_runtime import handback_session
    text = await handback_session("wa_" + digits, body.service or None, body.note, body.message)
    status = "not_sent"
    if body.send:
        sent = await send_whatsapp_message(digits, text)
        _remember_bot_message(sent)
        status = sent.get("status", "failed")
    print(f"[Handback] bot turned back on for {_mask(digits)} (message {status})")
    return {"session_id": "wa_" + digits, "bot": "on", "message_sent": status, "message": text}