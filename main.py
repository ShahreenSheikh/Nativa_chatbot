"""FastAPI entry point for the NativaCare chatbot."""
import os
import re
import uuid
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
import db
db.init_db()
import payments
import invoice_pdf
CLINIC_NAME=os.getenv("CLINIC_NAME","NativaCare")
ADMIN_API_KEY=os.getenv("ADMIN_API_KEY","").strip()
def require_admin(x_admin_key:str=Header(default="")):
    if ADMIN_API_KEY and x_admin_key!=ADMIN_API_KEY:raise HTTPException(status_code=401,detail="Missing or invalid X-Admin-Key header")
UPLOADS_DIR=Path(os.getenv("UPLOADS_DIR","uploads"));UPLOADS_DIR.mkdir(parents=True,exist_ok=True)
app=FastAPI(title=f"{CLINIC_NAME} — Midwifery Chatbot",version="0.1.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
_STATIC_DIR=Path(__file__).parent/"static"
if _STATIC_DIR.exists():app.mount("/static",StaticFiles(directory=str(_STATIC_DIR)),name="static")
# Public clinic media used by chat responses.
_ASSETS_DIR=Path(__file__).parent/"assets"
if _ASSETS_DIR.exists():app.mount("/assets",StaticFiles(directory=str(_ASSETS_DIR)),name="assets")
app.mount("/uploads",StaticFiles(directory=str(UPLOADS_DIR)),name="uploads")
_DASHBOARD_DIR=Path(__file__).parent/"dashboard"
if _DASHBOARD_DIR.exists():app.mount("/dashboard",StaticFiles(directory=str(_DASHBOARD_DIR),html=True),name="dashboard")
_WIDGET_PATH=Path(__file__).parent/"widget.js"
@app.get("/widget.js",include_in_schema=False)
def widget_js():
    if not _WIDGET_PATH.exists():raise HTTPException(status_code=404,detail="widget.js not found")
    return Response(content=_WIDGET_PATH.read_text(encoding="utf-8"),media_type="application/javascript",headers={"Cache-Control":"no-cache, no-store, must-revalidate"})
@app.get("/",include_in_schema=False)
def root():
    index=_STATIC_DIR/"index.html"
    if index.exists():return FileResponse(index)
    return JSONResponse({"status":"ok","service":f"{CLINIC_NAME} — Midwifery Chatbot","version":"0.1.0","note":"static/index.html not found — UI disabled, API still works","docs":"/docs"})
@app.get("/health")
def health():return {"status":"ok","service":f"{CLINIC_NAME} — Midwifery Chatbot","version":"0.1.0"}
@app.get("/status")
def status():
    import whatsapp_service as wa
    return {"website_chat":{"status":"connected"},"whatsapp":{"status":"connected" if (wa.WHATSAPP_ACCESS_TOKEN and wa.WHATSAPP_PHONE_NUMBER_ID) else "not_connected"},"calendar":{"status":"connected" if os.getenv("GOOGLE_CALENDAR_ID") else "not_connected"},"email":{"status":"connected" if os.getenv("RESEND_API_KEY") else "not_connected"},"payment_enabled":os.getenv("PAYMENT_ENABLED","false").lower() in ("true","1","yes","on")}
@app.post("/chat")
async def chat(req:ChatRequest):
    try:
        result=await get_ai_response(session_id=req.session_id,user_message=req.message,source=req.source or "website");result["session_id"]=req.session_id;return result
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
