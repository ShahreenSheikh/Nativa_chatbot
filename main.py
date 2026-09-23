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
from whatsapp_service import send_whatsapp_message,verify_webhook_signature,parse_incoming_messages,parse_message_echoes,download_media,WHATSAPP_VERIFY_TOKEN
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
_SERVICE_FLYERS={"postnatal":"/assets/flyer1.jpg","antenatal":"/assets/flyer2.jpg","nanny":"/assets/flyer3.jpg","breastfeeding":"/assets/flyer4.jpg"};_ALL_FLYERS=list(_SERVICE_FLYERS.values())

def _select_flyers(user_message:str,reply:str):
    user=(user_message or "").lower();answer=(reply or "").lower()
    # Flyers are for service discovery/selection only. Never repeat them once
    # booking has moved to language/date/time/contact/payment steps.
    booking_markers=("in which language would you like","available days","reply with a date","available times","reply with a time","what is your name","phone number","email address","booking summary","confirm your booking","payment","bank transfer")
    if any(x in answer for x in booking_markers):return []
    broad=("all services","all your services","what services","which services","services do you offer","services you offer","other services","what other services","show me all","everything you offer")
    if any(p in user for p in broad):return _ALL_FLYERS
    groups={"breastfeeding":("breastfeed","latch","latching","lactation","colostrum","milk supply","nursing"),"antenatal":("antenatal","prenatal","pregnancy preparation","birth preparation","childbirth education"),"postnatal":("postnatal","postpartum","after birth","recovery support","new mother recovery"),"nanny":("nanny","caregiver training","care giver training","newborn training")}
    selected=[]
    for key,words in groups.items():
        if any(w in user for w in words):selected.append(_SERVICE_FLYERS[key])
    # Show a flyer on a service-selection response even when the user's wording
    # was generic (e.g. "tell me about it"), but don't inherit it forever.
    if not selected and any(x in answer for x in ("has a few options","which would you like","service options")):
        for key,words in groups.items():
            if any(w in answer for w in words):selected.append(_SERVICE_FLYERS[key])
    return list(dict.fromkeys(selected))

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
        result=await get_ai_response(session_id=req.session_id,user_message=req.message,source=req.source or "website");result["session_id"]=req.session_id;result["reply"]=_normalize_language_prompt(result.get("reply",""))
        if (req.source or "website")=="website":result["flyers"]=_select_flyers(req.message,result.get("reply",""))
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

# Payment proof uploads: images <=10 MB; PDFs <=15 MB. Bytes are stored in
# PostgreSQL so Railway redeploys cannot delete the proof.
_IMAGE_TYPES={"image/jpeg","image/png"};_PDF_TYPE="application/pdf";_IMAGE_MAX=10*1024*1024;_PDF_MAX=15*1024*1024
@app.post("/invoices/{invoice_id}/proof")
async def upload_payment_proof(invoice_id:int,file:UploadFile=File(...)):
    invoice=db.get_invoice(invoice_id)
    if not invoice:raise HTTPException(status_code=404,detail="Invoice not found")
    ctype=(file.content_type or "").lower();name=file.filename or "payment-proof"
    ext=Path(name).suffix.lower()
    if ctype not in _IMAGE_TYPES|{_PDF_TYPE} or (ctype==_PDF_TYPE and ext!=".pdf") or (ctype in _IMAGE_TYPES and ext not in {".jpg",".jpeg",".png"}):
        raise HTTPException(status_code=415,detail="Payment proof must be JPG, JPEG, PNG or PDF.")
    limit=_PDF_MAX if ctype==_PDF_TYPE else _IMAGE_MAX
    data=await file.read(limit+1)
    if len(data)>limit:raise HTTPException(status_code=413,detail=("PDF is too large. Maximum size is 15 MB." if ctype==_PDF_TYPE else "Image is too large. Maximum size is 10 MB."))
    # lightweight signature validation; do not trust MIME/extension alone
    valid=(ctype==_PDF_TYPE and data.startswith(b"%PDF-")) or (ctype=="image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or (ctype=="image/jpeg" and data.startswith(b"\xff\xd8\xff"))
    if not valid:raise HTTPException(status_code=415,detail="The uploaded file does not match its declared file type.")
    proof=db.add_payment_proof(invoice_id,source="website",file_bytes=data,content_type=ctype,original_filename=name)
    db.update_invoice_status(invoice_id,"submitted")
    updated=db.get_invoice(invoice_id)
    try:await payments.notify_owner_of_payment_proof(updated)
    except Exception as e:print(f"[Payment proof notification] {e}")
    return {"status":"submitted","proof":{"id":proof["id"],"file_path":proof["file_path"],"content_type":ctype,"original_filename":name,"file_size":len(data)}}

@app.get("/payment-proofs/{proof_id}")
def payment_proof_file(proof_id:int):
    proof=db.get_payment_proof(proof_id)
    if not proof or not proof.get("file_data_b64"):raise HTTPException(status_code=404,detail="Payment proof not found")
    try:data=base64.b64decode(proof["file_data_b64"])
    except Exception:raise HTTPException(status_code=500,detail="Stored payment proof is invalid")
    filename=proof.get("original_filename") or f"payment-proof-{proof_id}"
    disposition="inline" if proof.get("content_type")==_PDF_TYPE else "inline"
    return Response(content=data,media_type=proof.get("content_type") or "application/octet-stream",headers={"Content-Disposition":f'{disposition}; filename="{filename.replace(chr(34),"")}"',"Cache-Control":"private, max-age=300"})

@app.get("/invoices",dependencies=[Depends(require_admin)])
def invoices(status:str=""):
    return {"invoices":db.list_invoices(status or None)}
@app.get("/invoices/{invoice_id}",dependencies=[Depends(require_admin)])
def invoice_detail(invoice_id:int):
    inv=db.get_invoice(invoice_id)
    if not inv:raise HTTPException(status_code=404,detail="Invoice not found")
    proofs=db.list_proofs_for_invoice(invoice_id)
    # Never send stored base64 bytes in dashboard JSON.
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
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request:Request):
    raw=await request.body()
    if not verify_webhook_signature(raw,request.headers.get("x-hub-signature-256","")):raise HTTPException(status_code=403,detail="Invalid signature")
    try:payload=await request.json()
    except Exception:return {"status":"ok"}
    for msg in parse_incoming_messages(payload):
        sender=msg.get("from","");text=msg.get("text","")
        if sender and text:
            result=await get_ai_response("wa_"+sender,text,"whatsapp")
            if result.get("reply"):await send_whatsapp_message(sender,result["reply"])
    return {"status":"ok"}