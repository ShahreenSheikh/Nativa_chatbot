"""
WhatsApp Cloud API integration (Meta's official API) for NativaCare.

Handles the two things WhatsApp needs from us:
  1. Sending replies back to a patient's WhatsApp number (send_whatsapp_message)
  2. Verifying that inbound webhook calls really came from Meta
     (verify_webhook_signature)

This talks directly to Meta's Graph API — no third-party BSP (Twilio,
360dialog, Wati, etc.) and no BSP markup. Meta's Cloud API itself has no
subscription fee; the only costs are per-message fees for business-
initiated Marketing/Utility/Authentication messages, which this bot
doesn't send — every message here is a reply inside a patient-initiated
conversation, which is free under Meta's pricing.

Required .env variables (all from the Meta App Dashboard -> WhatsApp ->
API Setup, during the test-number phase):
    WHATSAPP_ACCESS_TOKEN     temporary (24h) or permanent System User token
    WHATSAPP_PHONE_NUMBER_ID  the numeric ID of the sending number (NOT the
                              phone number itself — find it under API Setup)
    WHATSAPP_VERIFY_TOKEN     any string you make up yourself; entered in
                              both your .env and the Meta webhook config
                              screen when you register the webhook URL
    WHATSAPP_APP_SECRET       optional but recommended — enables signature
                              verification on inbound webhook calls
    WHATSAPP_API_VERSION      optional, defaults to a recent Graph API version
                              (check developers.facebook.com/docs/graph-api/
                              changelog for the current one before going live)

If WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID are missing, sends are
skipped with a clear log line instead of crashing — same "skipped_demo"
pattern as calendar_service.py and email_service.py in this project.

Public API:
    send_whatsapp_message(to, body)          -> dict
    verify_webhook_signature(raw_body, sig)  -> bool
    parse_incoming_messages(payload)         -> list[dict]
    download_media(media_id)                 -> bytes | None
"""

import os
import hmac
import hashlib
import httpx
from dotenv import load_dotenv

load_dotenv()

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")

GRAPH_URL = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"

# Startup diagnostics — same pattern as calendar_service.py / email_service.py
# so a glance at the uvicorn log tells you what's actually configured.
if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
    print("[WhatsApp startup] WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID "
          "not fully set. The webhook can still be verified and receive "
          "messages, but replies will be skipped (logged only) until both "
          "are set.")
else:
    masked = (WHATSAPP_ACCESS_TOKEN[:8] + "..." + WHATSAPP_ACCESS_TOKEN[-4:]
              if len(WHATSAPP_ACCESS_TOKEN) > 16 else "***")
    print(f"[WhatsApp startup] Configured. Phone number ID: "
          f"{WHATSAPP_PHONE_NUMBER_ID} (token: {masked})")

if not WHATSAPP_VERIFY_TOKEN:
    print("[WhatsApp startup] WARNING: WHATSAPP_VERIFY_TOKEN is empty — "
          "Meta's webhook verification handshake (GET /webhook/whatsapp) "
          "will fail until you set this to match the value you enter in "
          "the Meta dashboard's webhook configuration screen.")


async def send_whatsapp_message(to: str, body: str) -> dict:
    """Send a plain-text WhatsApp message. Returns a status dict, never raises.

    Args:
        to: the recipient's WhatsApp ID (the "from" field on their inbound
            message — usually their phone number in international format
            with no leading +, e.g. "9715xxxxxxx")
        body: the reply text
    """
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print(f"[WhatsApp] Skipped send to {to} (not configured): {body[:60]!r}")
        return {"status": "skipped_demo"}

    url = f"{GRAPH_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        # WhatsApp text messages cap at 4096 characters
        "text": {"body": body[:4096]},
    }
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            res.raise_for_status()
            return {"status": "sent", "response": res.json()}
    except Exception as e:
        print(f"[WhatsApp] Send failed to {to}: {e}")
        return {"status": "failed", "error": str(e)}


async def send_whatsapp_document(to: str, link: str, filename: str, caption: str = "") -> dict:
    """Send a document (e.g. the PDF invoice) via a public link — simpler
    than WhatsApp's two-step media-upload flow, and works fine here since
    the invoice PDF endpoint is already public (see
    /invoices/by-reference/{reference}/pdf in main.py). `link` must be a
    real, internet-reachable HTTPS URL — WhatsApp's servers fetch it
    themselves, so this will silently fail if `link` points at
    localhost/127.0.0.1 (same real-vs-local constraint as the email
    image embedding — only works once FRONTEND_BASE_URL is a real public
    address, e.g. after deploying to Railway)."""
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print(f"[WhatsApp] Skipped document send to {to} (not configured): {filename!r}")
        return {"status": "skipped_demo"}

    url = f"{GRAPH_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"link": link, "filename": filename, "caption": caption[:1024]},
    }
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            res.raise_for_status()
            return {"status": "sent", "response": res.json()}
    except Exception as e:
        print(f"[WhatsApp] Document send failed to {to}: {e}")
        return {"status": "failed", "error": str(e)}


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify the X-Hub-Signature-256 header Meta sends on every webhook call.

    Returns True if verification passes OR if WHATSAPP_APP_SECRET isn't set
    yet (so the test-number phase still works before you've filled that
    in) — but logs a warning in that case so it's not silently insecure
    once a real patient-facing number goes live.
    """
    if not WHATSAPP_APP_SECRET:
        print("[WhatsApp] WARNING: WHATSAPP_APP_SECRET not set — skipping "
              "webhook signature verification. Fine for early testing, but "
              "set this before going live with a real patient-facing number, "
              "otherwise anyone who finds your webhook URL could post fake "
              "messages to it.")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.split("sha256=", 1)[1]
    return hmac.compare_digest(expected, provided)


def parse_incoming_messages(payload: dict) -> list:
    """Extract simple {from, type, text, media_id, id} dicts from a raw
    WhatsApp webhook payload.

    Meta's payload nests messages several levels deep and also sends
    "statuses" updates (delivered/read receipts, not actual messages) —
    those are naturally skipped here since we only look under
    value.messages. Image messages (used for payment screenshots) carry
    their content as a media_id, not inline bytes — call download_media()
    with that id to fetch the actual file. Other non-text types (audio,
    location, etc.) are still returned with the real "type" and empty
    text/media_id, so the caller can decide how to respond.
    """
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                msg_type = msg.get("type", "")
                text = ""
                media_id = ""
                mime_type = ""
                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "")
                elif msg_type == "image":
                    img = msg.get("image", {})
                    media_id = img.get("id", "")
                    mime_type = img.get("mime_type", "")
                    text = img.get("caption", "")
                out.append({
                    "from": msg.get("from", ""),
                    "type": msg_type,
                    "text": text,
                    "media_id": media_id,
                    "mime_type": mime_type,
                    "id": msg.get("id", ""),
                })
    return out


def parse_message_echoes(payload: dict) -> list:
    """Extract {to, type, text, id, timestamp} dicts for messages staff
    sent directly from the WhatsApp Business App on their phone — this is
    what Meta's "Coexistence" feature (a WhatsApp number active in both
    the Business App and the Cloud API at once) uses to notify your
    webhook that a human just replied outside the API.

    This is what makes automatic takeover detection possible: when this
    fires for a given customer, main.py enables human_mode for that
    conversation immediately, no dashboard click needed. Requires two
    things neither of which are code — enabling Coexistence for your
    number in Meta's WhatsApp Manager, AND subscribing your app to the
    "smb_message_echoes" webhook field in the Meta App Dashboard (separate
    from the "messages" field you're presumably already subscribed to).
    Without both, this webhook payload shape never arrives at all and
    this function will just never find anything to return — that's the
    first thing to check if automatic takeover doesn't seem to work.

    Official payload shape (per Meta's smb_message_echoes reference):
        entry[].changes[].field == "smb_message_echoes"
        entry[].changes[].value.message_echoes[] == [{from, to, id,
            timestamp, type, <type>: {...}}]
    Note "to" here is the CUSTOMER's number (who staff just replied to) —
    "from" is your own business number, which is not useful for figuring
    out which conversation to silence.
    """
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "smb_message_echoes":
                continue
            value = change.get("value", {})
            for echo in value.get("message_echoes", []):
                echo_type = echo.get("type", "")
                text = ""
                if echo_type == "text":
                    text = echo.get("text", {}).get("body", "")
                out.append({
                    "to": echo.get("to", ""),
                    "type": echo_type,
                    "text": text,
                    "id": echo.get("id", ""),
                    "timestamp": echo.get("timestamp", ""),
                })
    return out


async def download_media(media_id: str) -> tuple:
    """Download a WhatsApp media object (e.g. an image) by its media_id.

    This is a two-step Graph API dance: first resolve the media_id to a
    short-lived download URL, then fetch the bytes from that URL with the
    same access token. Returns (bytes, mime_type) or (None, None) on any
    failure — never raises, since a failed download shouldn't crash the
    webhook handler (the sender just gets told to resend).
    """
    if not WHATSAPP_ACCESS_TOKEN:
        print("[WhatsApp] Cannot download media — WHATSAPP_ACCESS_TOKEN not set")
        return None, None
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient() as client:
            meta_res = await client.get(f"{GRAPH_URL}/{media_id}", headers=headers, timeout=10.0)
            meta_res.raise_for_status()
            meta = meta_res.json()
            url = meta.get("url", "")
            mime_type = meta.get("mime_type", "")
            if not url:
                return None, None
            file_res = await client.get(url, headers=headers, timeout=20.0)
            file_res.raise_for_status()
            return file_res.content, mime_type
    except Exception as e:
        print(f"[WhatsApp] Media download failed for {media_id}: {e}")
        return None, None