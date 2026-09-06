"""
FastAPI entry point for the NativaCare chatbot.

Endpoints:
  GET  /                        Health / metadata
  POST /chat                    Main chat endpoint
  GET  /appointments            List of bookings (for clinic-side review)
  GET  /chat-logs                Recent chat logs
  GET  /availability             Direct availability query (debug / UI use)
  GET  /webhook/whatsapp         Meta's webhook verification handshake
  POST /webhook/whatsapp         Receives inbound WhatsApp messages, replies
                                  through the same get_ai_response() brain
                                  that powers /chat — also handles inbound
                                  images (payment screenshots)

  Manual payment (see payments.py / db.py):
  GET  /pay/{reference}          Patient-facing payment page (bank details
                                  + screenshot upload, no auth — this is
                                  the link shown in the invoice message)
  GET  /invoices                 [admin] List invoices (dashboard Payments page)
  GET  /invoices/{id}            [admin] One invoice, incl. proof screenshots
  POST /invoices/{id}/proof      Upload a payment screenshot (public — patient)
  GET  /invoices/by-reference/{reference}  Look up an invoice (public — patient)
  POST /invoices/{id}/approve    [admin] Approve -> creates the appointment
  POST /invoices/{id}/reject     [admin] Reject -> notifies the patient

  Human takeover (see ai.py human_mode check / whatsapp_service.py):
  GET  /conversations/human-mode        [admin] List conversations staff have taken over
  POST /conversations/{id}/takeover     [admin] Turn takeover on/off for one conversation
  POST /conversations/{id}/reply        [admin] Staff sends a message as themselves

  [admin] routes require the X-Admin-Key header to match ADMIN_API_KEY —
  see the require_admin() dependency below. Unprotected until that env
  var is set (see startup warning).

WhatsApp uses Meta's official Cloud API directly (no BSP, no BSP markup —
see whatsapp_service.py for details and required .env variables).
"""

import os
import re
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from fastapi import (
    FastAPI, HTTPException, Query, Request, Response, UploadFile, File, Form,
    Header, Depends,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from models import ChatRequest
from ai import get_ai_response, record_staff_reply
from database import get_saved_appointments
from logger import get_chat_logs, get_chat_logs_for_session
from availability import get_availability, get_next_available_days
from whatsapp_service import (
    send_whatsapp_message,
    verify_webhook_signature,
    parse_incoming_messages,
    parse_message_echoes,
    download_media,
    WHATSAPP_VERIFY_TOKEN,
)
import db
db.init_db()  # safety net — also called from database.py, but calling it
              # again here is harmless (SQLAlchemy's create_all only makes
              # tables that don't already exist) and guarantees the schema
              # exists even if database.py is ever swapped out.
import payments
import invoice_pdf

CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")

# Staff-only endpoints (patient lists, invoice approve/reject, conversation
# takeover) are protected by this shared key — set it before your first
# real deployment. Without it, anyone who finds your backend URL could
# approve fake payments, read patient contact details, or take over a
# WhatsApp conversation. While it's unset, everything still works exactly
# as before (nothing is blocked) so local testing is unaffected — but a
# loud warning prints on startup so this isn't accidentally forgotten.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
if ADMIN_API_KEY:
    print("[Admin auth] ADMIN_API_KEY is set — staff-only endpoints require "
          "the X-Admin-Key header.")
else:
    print("[Admin auth] WARNING: ADMIN_API_KEY is NOT set. Staff-only "
          "endpoints (appointments, chat logs, invoice approval, "
          "conversation takeover) are UNPROTECTED — anyone with your "
          "backend URL can use them. Fine for local testing; set "
          "ADMIN_API_KEY before deploying anywhere patients or staff "
          "actually use.")


def require_admin(x_admin_key: str = Header(default="")):
    """FastAPI dependency guarding staff-only routes. A no-op (always
    passes) when ADMIN_API_KEY isn't configured, so this never breaks
    local development — it only starts enforcing once you set the key."""
    if ADMIN_API_KEY and x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Key header")

# Where uploaded payment screenshots are stored. On Railway this needs a
# persistent volume mounted at this path — same caveat as NATIVACARE_DB_PATH
# in db.py — otherwise screenshots vanish on redeploy, same problem we're
# trying to solve for the rest of the data.
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", "uploads"))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

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

# Serve uploaded payment screenshots so the dashboard's Payments page can
# show them inline for staff to review.
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# Serve the staff dashboard (1_dashboard.html / 2_appointments.html /
# 3_ai_chatbot_conversations.html / 4_payments.html) from the same backend
# that runs the chatbot. This is what makes "Connect" automatic: each
# dashboard page's JS defaults its backend URL to window.location.origin
# when nothing is saved yet, and since the dashboard is now same-origin
# with the API, that just works with zero configuration. Staff can still
# override the URL manually (e.g. if the dashboard is ever hosted
# separately from the backend) — that override is saved in the browser's
# localStorage, not lost on every reload the way the original mockups'
# window.storage calls silently were (that API only exists inside Claude's
# own artifact preview, not in a real deployed browser).
_DASHBOARD_DIR = Path(__file__).parent / "dashboard"
if _DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")

# The embeddable chat widget for the CLIENT's real website (nativacare.com
# or wherever) — a single self-contained JS file, served from here so it's
# always in sync with this deployment. Embedding is one line:
#   <script src="https://your-backend-url/widget.js" defer></script>
# The widget reads its own <script> src to find this backend automatically
# — no per-environment edits to widget.js are ever needed.
_WIDGET_PATH = Path(__file__).parent / "widget.js"


@app.get("/widget.js", include_in_schema=False)
def widget_js():
    if not _WIDGET_PATH.exists():
        raise HTTPException(status_code=404, detail="widget.js not found")
    return Response(
        content=_WIDGET_PATH.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


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


@app.get("/status")
def status():
    """Lightweight integration status for the dashboard's Channels card —
    real config checks, not hardcoded. Nothing here is a secret (no keys
    are returned, just whether each is set), so it's safe to expose
    without auth. WhatsApp will flip from "not_connected" to "connected"
    automatically here the moment WHATSAPP_ACCESS_TOKEN and
    WHATSAPP_PHONE_NUMBER_ID are set on the backend — no dashboard code
    change needed when that day comes.
    """
    import whatsapp_service as wa
    return {
        "website_chat": {"status": "connected"},
        "whatsapp": {
            "status": "connected" if (wa.WHATSAPP_ACCESS_TOKEN and wa.WHATSAPP_PHONE_NUMBER_ID) else "not_connected",
        },
        "calendar": {"status": "connected" if os.getenv("GOOGLE_CALENDAR_ID") else "not_connected"},
        "email": {"status": "connected" if os.getenv("RESEND_API_KEY") else "not_connected"},
        "payment_enabled": os.getenv("PAYMENT_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
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


@app.get("/chat/{session_id}/poll", include_in_schema=False)
def poll_chat(session_id: str, after_id: int = 0):
    """Public, no-auth endpoint the website chat widget polls periodically
    while a chat is open. This is what makes human takeover actually work
    for website patients, not just WhatsApp: when staff reply from the
    dashboard during takeover (see /conversations/{id}/reply), that reply
    is logged here same as any bot turn, and the widget picks it up on its
    next poll and renders it — without the patient needing to send a new
    message first to "unstick" the conversation.

    Only returns entries whose user_message is literally "[staff]" (i.e.
    a direct staff reply) — normal bot turns are already shown synchronously
    in the widget's own /chat response, so echoing them here too would
    just duplicate every message in the transcript.
    """
    entries = get_chat_logs_for_session(session_id, after_id=after_id)
    staff_messages = [
        {"id": e["id"], "message": e["ai_response"], "timestamp": e["timestamp"]}
        for e in entries
        if e["user_message"] == "[staff]"
    ]
    return {"messages": staff_messages}


@app.get("/appointments", dependencies=[Depends(require_admin)])
def appointments():
    return {"appointments": get_saved_appointments()}


class AppointmentStatusRequest(BaseModel):
    status: str  # confirmed / cancelled / pending / rejected


@app.patch("/appointments/{appointment_id}/status", dependencies=[Depends(require_admin)])
def update_appointment_status(appointment_id: int, body: AppointmentStatusRequest):
    """Backs the Confirm/Cancel controls on the dashboard's Appointments
    page. Straightforward status update — approving a payment invoice
    (POST /invoices/{id}/approve) is the richer flow that also creates
    the calendar event and sends emails; this is just for adjusting an
    already-created appointment's status by hand."""
    db.update_appointment_status(appointment_id, body.status)
    return {"id": appointment_id, "status": body.status}


@app.get("/chat-logs", dependencies=[Depends(require_admin)])
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


# ---------------------------------------------------------------------------
# WhatsApp webhook (Meta Cloud API)
# ---------------------------------------------------------------------------

@app.get("/webhook/whatsapp", include_in_schema=False)
def whatsapp_verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Meta's one-time webhook verification handshake.

    When you register this URL in the Meta App Dashboard (WhatsApp ->
    Configuration -> Webhook), Meta calls this endpoint once with a
    verify_token and challenge string. We echo the challenge back only if
    the verify_token matches WHATSAPP_VERIFY_TOKEN in your .env — this is
    what proves to Meta (and to you) that you control this URL.
    """
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook/whatsapp", include_in_schema=False)
async def whatsapp_incoming(request: Request):
    """Receives inbound WhatsApp messages and replies through the same
    conversational brain (get_ai_response) that already powers /chat.

    Each WhatsApp sender gets a distinct session keyed off their WhatsApp
    ID, so a patient's WhatsApp conversation and website conversation stay
    separate (they'd need to identify themselves again on each channel —
    same as the current website/chat behavior between browser sessions).
    """
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    payload = await request.json()
    messages = parse_incoming_messages(payload)

    # Automatic takeover: if staff replied directly from the WhatsApp
    # Business App on their phone (Meta's "Coexistence" feature — see
    # parse_message_echoes' docstring for the setup this requires), stop
    # the bot from replying to that customer without anyone needing to
    # touch the dashboard. This runs BEFORE the normal messages loop below
    # so a customer's message arriving in the same webhook call as a
    # staff echo is still correctly held for staff, not answered by the
    # bot in the same request.
    echoes = parse_message_echoes(payload)
    for echo in echoes:
        customer_wa_id = echo.get("to") or ""
        if not customer_wa_id:
            continue
        session_id = f"whatsapp_{customer_wa_id}"
        conv = db.get_conversation(session_id)
        if not conv or not conv.get("human_mode"):
            db.set_human_mode(session_id, True, channel="whatsapp", contact=customer_wa_id)
            print(f"[WhatsApp] Auto-takeover: staff replied via the Business App "
                  f"to {customer_wa_id} — bot now silent for this conversation.")

    for msg in messages:
        wa_id = msg.get("from") or ""
        if not wa_id:
            continue

        # Image message — treat it as a payment screenshot if there's an
        # open invoice for this sender. This is the WhatsApp-side
        # equivalent of POST /invoices/{id}/proof below; same downstream
        # effect (mark invoice "submitted", staff review on the dashboard).
        if msg.get("type") == "image":
            await _handle_whatsapp_payment_screenshot(wa_id, msg)
            continue

        if msg.get("type") != "text":
            await send_whatsapp_message(
                wa_id,
                "I can only read text messages and payment screenshots "
                "right now — could you type your question or request?",
            )
            continue

        # Owner commands — "approve NC-XXXXXXXX" / "reject NC-XXXXXXXX",
        # sent from the clinic owner's own WhatsApp number (the one
        # OWNER_WHATSAPP_NUMBER points at, same number the payment-proof
        # notification goes to). Intercepted before the normal bot flow
        # so the owner can approve/reject a screenshot straight from the
        # notification they just received, without opening the dashboard.
        # Anything from this number that ISN'T an approve/reject command
        # falls through to the normal bot conversation as usual.
        if payments.OWNER_WHATSAPP_NUMBER and wa_id == payments.OWNER_WHATSAPP_NUMBER:
            handled = await _handle_owner_whatsapp_command(wa_id, msg.get("text", ""))
            if handled:
                continue

        try:
            result = await get_ai_response(
                session_id=f"whatsapp_{wa_id}",
                user_message=msg.get("text", ""),
                source="whatsapp",
            )
            reply = result.get("reply", "")
            if reply:
                await send_whatsapp_message(wa_id, reply)
        except Exception as e:
            print(f"[WhatsApp Route Error]: {e}")
            await send_whatsapp_message(
                wa_id,
                "Sorry, something went wrong on our end. Please try again "
                "in a moment or call us at +971 50 729 7197.",
            )

    # Always ack 200 quickly regardless of outcome above, so Meta doesn't
    # retry/backoff on us — retries would just re-send the same reply.
    return {"status": "received"}


async def _handle_whatsapp_payment_screenshot(wa_id: str, msg: dict):
    """Download an inbound WhatsApp image and attach it as payment proof
    to that sender's most recent open invoice, if any."""
    invoices = [
        inv for inv in db.list_invoices()
        if inv.get("channel") == "whatsapp"
        and inv.get("contact") == wa_id
        and inv.get("status") in ("pending_proof", "submitted")
    ]
    if not invoices:
        await send_whatsapp_message(
            wa_id,
            "Thanks for the image — but I don't see an active booking "
            "waiting on payment for this number. If you're trying to "
            "complete a booking, let's pick that back up — what would "
            "you like to book?",
        )
        return

    invoice = invoices[0]  # most recent, since list_invoices() is newest-first
    media_id = msg.get("media_id", "")
    content, mime_type = await download_media(media_id) if media_id else (None, None)
    if not content:
        await send_whatsapp_message(
            wa_id,
            "I couldn't download that image — could you try resending it?",
        )
        return

    ext = ".jpg"
    if mime_type and "/" in mime_type:
        ext = "." + mime_type.split("/")[-1].split(";")[0]
    filename = f"{invoice['reference']}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = UPLOADS_DIR / filename
    file_path.write_bytes(content)

    db.add_payment_proof(invoice["id"], f"/uploads/{filename}", source="whatsapp")
    db.update_invoice_status(invoice["id"], "submitted")
    await payments.notify_owner_of_payment_proof(invoice)

    await send_whatsapp_message(
        wa_id,
        f"Got it — we've received your payment screenshot for reference "
        f"{invoice['reference']} and our team will verify it shortly. "
        f"We'll message you here as soon as it's confirmed.",
    )


_OWNER_COMMAND_RE = re.compile(
    r"^\s*(approve|reject)\s+(NC-[A-Z0-9]+)\s*(?:[:\-]\s*(.*))?\s*$",
    re.IGNORECASE,
)


async def _handle_owner_whatsapp_command(wa_id: str, text: str) -> bool:
    """Parses "approve NC-XXXXXXXX" / "reject NC-XXXXXXXX" (optionally
    "reject NC-XXXXXXXX: blurry screenshot" for a reason) from the clinic
    owner's WhatsApp number — the same one-click convenience as the email
    Approve/Reject links, for staff who'd rather just reply to the
    WhatsApp notification directly. Returns True if this message was
    recognized as a command (handled either way, including "invoice not
    found"), False if it wasn't a command at all — in which case the
    caller lets it fall through to the normal bot conversation."""
    match = _OWNER_COMMAND_RE.match((text or "").strip())
    if not match:
        return False

    action, reference, reason = match.group(1).lower(), match.group(2).upper(), match.group(3) or ""
    invoice = db.get_invoice_by_reference(reference)
    if not invoice:
        await send_whatsapp_message(wa_id, f"Couldn't find an invoice with reference {reference}.")
        return True

    if action == "approve":
        if invoice["status"] == "approved":
            await send_whatsapp_message(wa_id, f"{reference} was already approved.")
            return True
        result = await payments.approve_invoice(invoice["id"])
        if result.get("status") in ("approved", "already_approved"):
            await send_whatsapp_message(wa_id, f"✅ Approved {reference} — appointment created and patient notified.")
        else:
            await send_whatsapp_message(wa_id, f"Something went wrong approving {reference} — please use the dashboard instead.")
    else:  # reject
        if invoice["status"] in ("approved", "rejected"):
            await send_whatsapp_message(wa_id, f"{reference} was already {invoice['status']}.")
            return True
        await payments.reject_invoice(invoice["id"], reason=reason)
        await send_whatsapp_message(wa_id, f"❌ Rejected {reference}" + (f" ({reason})" if reason else "") + " — patient notified.")

    return True


# ---------------------------------------------------------------------------
# Manual payment — invoices, screenshot upload, staff approve/reject
# ---------------------------------------------------------------------------

@app.get("/pay/{reference}", response_class=HTMLResponse, include_in_schema=False)
def pay_page(reference: str):
    """Patient-facing upload page — the link shown in the invoice message
    (built from FRONTEND_BASE_URL, see payments.py). Deliberately a plain
    server-rendered page, not part of the staff dashboard: no admin key,
    no heavy JS framework, just enough to work on a patient's phone
    straight from a WhatsApp/email link. Looks up the invoice by its
    reference code client-side, so it always shows current status even if
    opened again after approval/rejection.
    """
    safe_ref = "".join(c for c in reference if c.isalnum() or c == "-")[:32]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{CLINIC_NAME} — Payment</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #F9F8F6;
         color: #1a1c1e; margin: 0; padding: 24px 16px; }}
  .card {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 20px;
           border: 1px solid #E5E7EB; padding: 24px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .ref {{ font-family: monospace; background: #F3F3F6; padding: 2px 8px; border-radius: 6px; }}
  .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #F0F0F0; font-size: 14px; }}
  .row span:first-child {{ color: #6b6b6b; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 3px 10px;
            border-radius: 999px; margin-top: 8px; }}
  .b-pending_proof {{ background: #e8e8ea; color: #4d4638; }}
  .b-submitted {{ background: #D4C294; color: #3f2e00; }}
  .b-approved {{ background: #d9e8c7; color: #3e4b33; }}
  .b-rejected {{ background: #ffdad6; color: #93000a; }}
  input[type=file] {{ width: 100%; margin: 16px 0; padding: 12px; border: 1px dashed #ccc; border-radius: 12px; box-sizing: border-box; }}
  button {{ width: 100%; padding: 14px; border: none; border-radius: 12px; background: #765a14;
            color: #fff; font-weight: 700; font-size: 15px; }}
  button:disabled {{ opacity: 0.5; }}
  #msg {{ margin-top: 12px; font-size: 14px; text-align: center; }}
  #loading, #notfound {{ text-align: center; padding: 24px; }}
</style></head>
<body>
<div class="card">
  <div id="loading">Loading your invoice…</div>
  <div id="notfound" style="display:none">We couldn't find an invoice with reference <span class="ref">{safe_ref}</span>. Please check the link or contact us.</div>
  <div id="content" style="display:none">
    <h1>{CLINIC_NAME}</h1>
    <p style="color:#6b6b6b; margin-top:0;">Reference <span class="ref">{safe_ref}</span></p>
    <div id="statusBadge"></div>
    <div id="details" style="margin-top:16px"></div>
    <div id="uploadArea" style="display:none">
      <input type="file" id="fileInput" accept="image/*" capture="environment">
      <button id="submitBtn">Upload screenshot</button>
    </div>
    <p style="margin-top:14px;"><a href="/invoices/by-reference/{safe_ref}/pdf" target="_blank" style="color:#765a14; font-weight:600; text-decoration:none;">📄 Download invoice (PDF)</a></p>
    <div id="msg"></div>
  </div>
</div>
<script>
  const ref = "{safe_ref}";
  let invoiceId = null;

  async function load(){{
    try{{
      const res = await fetch('/invoices/by-reference/' + encodeURIComponent(ref));
      if(!res.ok) throw new Error('not found');
      const data = await res.json();
      const inv = data.invoice;
      invoiceId = inv.id;
      document.getElementById('loading').style.display = 'none';
      document.getElementById('content').style.display = 'block';

      const labels = {{pending_proof: 'Awaiting your payment', submitted: 'Under review', approved: 'Confirmed', rejected: 'Needs a new screenshot'}};
      document.getElementById('statusBadge').innerHTML =
        '<span class="badge b-' + inv.status + '">' + (labels[inv.status] || inv.status) + '</span>';

      const lead = inv.lead || {{}};
      document.getElementById('details').innerHTML = `
        <div class="row"><span>Service</span><span>${{lead.service_name || '—'}}</span></div>
        <div class="row"><span>Amount</span><span>AED ${{inv.amount_aed || '—'}}</span></div>
      `;

      if(inv.status === 'pending_proof' || inv.status === 'rejected'){{
        document.getElementById('uploadArea').style.display = 'block';
        if(inv.status === 'rejected' && inv.reject_reason){{
          document.getElementById('msg').textContent = 'Previous attempt: ' + inv.reject_reason;
        }}
      }} else if(inv.status === 'submitted'){{
        document.getElementById('msg').textContent = "We've got your screenshot — our team is verifying it now.";
      }} else if(inv.status === 'approved'){{
        document.getElementById('msg').textContent = "Payment verified — your booking is confirmed!";
      }}
    }}catch(e){{
      document.getElementById('loading').style.display = 'none';
      document.getElementById('notfound').style.display = 'block';
    }}
  }}

  document.getElementById('submitBtn')?.addEventListener('click', async () => {{
    const input = document.getElementById('fileInput');
    if(!input.files.length){{ alert('Choose a screenshot first'); return; }}
    const btn = document.getElementById('submitBtn');
    btn.disabled = true; btn.textContent = 'Uploading…';
    const form = new FormData();
    form.append('file', input.files[0]);
    try{{
      const res = await fetch('/invoices/' + invoiceId + '/proof', {{ method: 'POST', body: form }});
      if(!res.ok) throw new Error('upload failed');
      document.getElementById('msg').textContent = "Thanks — we've received it and will confirm shortly.";
      document.getElementById('uploadArea').style.display = 'none';
    }}catch(e){{
      document.getElementById('msg').textContent = 'Upload failed — please try again.';
      btn.disabled = false; btn.textContent = 'Upload screenshot';
    }}
  }});

  load();
</script>
</body></html>"""


@app.get("/invoices", dependencies=[Depends(require_admin)])
def list_invoices(status: str | None = Query(None)):
    """List invoices for the dashboard's Payments page. Filter by status:
    pending_proof / submitted / approved / rejected. Omit for all."""
    return {"invoices": db.list_invoices(status=status)}


@app.get("/invoices/{invoice_id}", dependencies=[Depends(require_admin)])
def get_invoice(invoice_id: int):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice["proofs"] = db.list_proofs_for_invoice(invoice_id)
    return {"invoice": invoice}


@app.post("/invoices/{invoice_id}/proof")
async def upload_payment_proof(invoice_id: int, file: UploadFile = File(...)):
    """Website upload of a payment screenshot. Tied to the invoice ID, not
    the chat session — so this works even if the chat that generated the
    invoice has since expired or the tab was closed and reopened days
    later (e.g. from the link in the invoice's confirmation email, or by
    reference number via a support page)."""
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice["status"] in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"Invoice already {invoice['status']}")

    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Please upload an image (or PDF) of your payment confirmation")

    ext = Path(file.filename or "").suffix or ".jpg"
    filename = f"{invoice['reference']}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = UPLOADS_DIR / filename
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")
    file_path.write_bytes(content)

    db.add_payment_proof(invoice_id, f"/uploads/{filename}", source="website")
    updated = db.update_invoice_status(invoice_id, "submitted")
    await payments.notify_owner_of_payment_proof(updated)
    return {"status": "received", "invoice": updated}


@app.get("/invoices/by-reference/{reference}")
def get_invoice_by_reference(reference: str):
    """Look up an invoice by its reference code — for a patient who lost
    the chat but kept their reference number (e.g. from the confirmation
    email or a screenshot of the invoice message itself)."""
    invoice = db.get_invoice_by_reference(reference)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice["proofs"] = db.list_proofs_for_invoice(invoice["id"])
    return {"invoice": invoice}


@app.get("/invoices/by-reference/{reference}/pdf", include_in_schema=False)
def get_invoice_pdf(reference: str):
    """A real, downloadable PDF invoice — free to generate (fpdf2 runs
    entirely in-process, no external API, no per-document cost). Public
    like the JSON lookup above and for the same reason: the reference
    code is an unguessable random value that only the patient (and staff)
    ever see, so it's safe to use as the access key without also
    requiring a login."""
    invoice = db.get_invoice_by_reference(reference)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice["_account"] = payments._account_by_label(invoice.get("bank_account_label", ""))
    pdf_bytes = invoice_pdf.generate_invoice_pdf(invoice)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="invoice-{reference}.pdf"'},
    )


class RejectRequest(BaseModel):
    reason: str = ""


@app.post("/invoices/{invoice_id}/approve", dependencies=[Depends(require_admin)])
async def approve_invoice(invoice_id: int):
    result = await payments.approve_invoice(invoice_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Invoice not found")
    return result


@app.post("/invoices/{invoice_id}/reject", dependencies=[Depends(require_admin)])
async def reject_invoice(invoice_id: int, body: RejectRequest):
    result = await payments.reject_invoice(invoice_id, reason=body.reason)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Invoice not found")
    return result


def _decision_page(title: str, message: str, ok: bool = True) -> str:
    """Tiny standalone confirmation page for the one-click email
    approve/reject links below — no dashboard, no admin key, just enough
    to tell staff what happened when they tap the link on their phone."""
    color = "#3e4b33" if ok else "#93000a"
    bg = "#d9e8c7" if ok else "#ffdad6"
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #F9F8F6;
         margin: 0; padding: 40px 20px; display: flex; justify-content: center; }}
  .card {{ max-width: 400px; background: {bg}; color: {color}; border-radius: 20px;
           padding: 28px; text-align: center; }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  p {{ font-size: 14px; margin: 0; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""


@app.get("/invoices/{invoice_id}/approve-via-link", response_class=HTMLResponse, include_in_schema=False)
async def approve_invoice_via_link(invoice_id: int, token: str):
    """One-click approval from the staff notification email — no admin
    dashboard login needed. Safe because `token` is a per-invoice secret
    that only ever appears in the email sent to staff (see
    email_service.send_payment_proof_notification), never shown to the
    patient — it's a completely separate value from the invoice
    `reference`, which the patient does see."""
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return _decision_page("Not found", "This invoice no longer exists.", ok=False)
    if not invoice.get("action_token") or token != invoice["action_token"]:
        return _decision_page("Invalid link", "This approval link is invalid or has expired.", ok=False)
    if invoice["status"] == "approved":
        return _decision_page("Already approved", f"Invoice {invoice['reference']} was already approved.")

    result = await payments.approve_invoice(invoice_id)
    if result.get("status") not in ("approved", "already_approved"):
        return _decision_page("Something went wrong", "Could not approve this invoice — please use the dashboard instead.", ok=False)
    return _decision_page(
        "Approved ✓",
        f"Invoice {invoice['reference']} approved and the appointment has been created.",
    )


@app.get("/invoices/{invoice_id}/reject-via-link", response_class=HTMLResponse, include_in_schema=False)
async def reject_invoice_via_link(invoice_id: int, token: str, reason: str = ""):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return _decision_page("Not found", "This invoice no longer exists.", ok=False)
    if not invoice.get("action_token") or token != invoice["action_token"]:
        return _decision_page("Invalid link", "This rejection link is invalid or has expired.", ok=False)
    if invoice["status"] in ("approved", "rejected"):
        return _decision_page("Already decided", f"Invoice {invoice['reference']} was already {invoice['status']}.")

    await payments.reject_invoice(invoice_id, reason=reason)
    return _decision_page(
        "Rejected",
        f"Invoice {invoice['reference']} was rejected and the patient has been notified.",
        ok=True,
    )


# ---------------------------------------------------------------------------
# Human takeover — staff replying directly (mainly for WhatsApp, since bot
# and staff share one number; see ai.py's human_mode check for how the bot
# stays silent while this is active)
# ---------------------------------------------------------------------------

class TakeoverRequest(BaseModel):
    enabled: bool
    channel: str = ""   # "whatsapp" or "website" — needed the first time
    contact: str = ""   # wa_id for whatsapp, so staff replies know where to send


class StaffReplyRequest(BaseModel):
    message: str


@app.get("/conversations/human-mode", dependencies=[Depends(require_admin)])
def list_human_mode():
    return {"conversations": db.list_human_mode_conversations()}


@app.post("/conversations/{session_id}/takeover", dependencies=[Depends(require_admin)])
def set_takeover(session_id: str, body: TakeoverRequest):
    db.set_human_mode(session_id, body.enabled, channel=body.channel, contact=body.contact)
    return {"session_id": session_id, "human_mode": body.enabled}


@app.post("/conversations/{session_id}/reply", dependencies=[Depends(require_admin)])
async def staff_reply(session_id: str, body: StaffReplyRequest):
    """Staff sends a message as themselves, from the dashboard. Requires
    takeover to already be enabled for this conversation — this endpoint
    doesn't auto-enable it, so a message can't accidentally get sent while
    the bot is also live and might reply at the same time."""
    conv = db.get_conversation(session_id)
    if not conv or not conv.get("human_mode"):
        raise HTTPException(status_code=400, detail="Enable takeover for this conversation first")

    record_staff_reply(session_id, body.message)

    if conv.get("channel") == "whatsapp" and conv.get("contact"):
        result = await send_whatsapp_message(conv["contact"], body.message)
        return {"status": "sent", "channel": "whatsapp", "detail": result}

    # Website: record_staff_reply() already wrote this into the chat log
    # above. The patient's widget polls GET /chat/{session_id}/poll every
    # few seconds while their chat is open and will pick this up and
    # render it automatically — no separate push infrastructure needed.
    # (If they've closed the tab, this waits in the log for their next
    # poll when they reopen it — same graceful-degradation as WhatsApp
    # messages waiting for delivery.)
    return {"status": "sent", "channel": conv.get("channel", "website")}