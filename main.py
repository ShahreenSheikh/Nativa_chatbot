"""
FastAPI entry point for the NativaCare chatbot.

Endpoints:
  GET  /                   Health / metadata
  POST /chat               Main chat endpoint
  GET  /appointments       List of bookings (for clinic-side review)
  GET  /chat-logs          Recent chat logs
  GET  /availability       Direct availability query (debug / UI use)

No WhatsApp webhook in v1 — that requires Meta business setup and is
deferred. The hook can be added later by mirroring the Dubai bot's
/webhook/whatsapp routes.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from models import ChatRequest
from ai import get_ai_response
from database import get_saved_appointments
from logger import get_chat_logs
from availability import get_availability, get_next_available_days

CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")

app = FastAPI(
    title=f"{CLINIC_NAME} — Midwifery Chatbot",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the test-frontend assets from /static (and use static/index.html at /).
# If the static folder doesn't exist (e.g. running from a deployment that
# doesn't ship the frontend), the API still works — just no chat UI.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def root():
    """Serve the chat UI if available; otherwise fall back to JSON status."""
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({
        "status": "ok",
        "service": f"{CLINIC_NAME} — Midwifery Chatbot",
        "version": "0.1.0",
        "note": "static/index.html not found — UI disabled, API still works",
        "docs": "/docs",
    })


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": f"{CLINIC_NAME} — Midwifery Chatbot",
        "version": "0.1.0",
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        result = await get_ai_response(
            session_id=req.session_id,
            user_message=req.message,
            source=req.source or "website",
        )
        result["session_id"] = req.session_id
        return result
    except Exception as e:
        print(f"[Route Error]: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/appointments")
def appointments():
    return {"appointments": get_saved_appointments()}


@app.get("/chat-logs")
def chat_logs():
    return {"chat_logs": get_chat_logs()}


@app.get("/availability")
async def availability(
    service_id: str = Query(..., description="Service ID (e.g. S004)"),
    date: str | None = Query(None, description="YYYY-MM-DD; omit to look 7 days ahead"),
    midwife_id: str | None = Query(None),
):
    """Direct availability lookup for the UI or admin debugging."""
    if date:
        day = await get_availability(service_id, date, midwife_id)
        return {"day": day.model_dump()}
    days = await get_next_available_days(service_id, 7, preferred_midwife_id=midwife_id)
    return {"days": [d.model_dump() for d in days]}
