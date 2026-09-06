"""
Email notifications for NativaCare bookings.

Uses Resend API (same as Dubai bot). If RESEND_API_KEY or FROM_EMAIL is
missing, both functions return a "skipped_demo" status instead of failing —
so dev deployments don't require email setup.

Public API:
    send_patient_confirmation(lead_dict) -> dict
    send_clinic_notification(lead_dict)  -> dict
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
CLINIC_EMAIL = os.getenv("CLINIC_EMAIL", os.getenv("AGENT_EMAIL", ""))
CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")
CLINIC_PHONE = os.getenv("CLINIC_PHONE", "+971507297197")
# Backend's own public URL (same variable payments.py uses for the /pay/
# link) — needed here to build the one-click Approve/Reject links in the
# staff notification email. Blank while testing locally just means those
# buttons are omitted; staff use the dashboard instead.
BACKEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "").rstrip("/")


async def _send_email(to: str, subject: str, html: str, attachments: list = None) -> dict:
    if not RESEND_API_KEY or not FROM_EMAIL or not to:
        print(f"[Email] Skipped (to={to or 'missing'}, "
              f"key={'set' if RESEND_API_KEY else 'missing'}, "
              f"from={'set' if FROM_EMAIL else 'missing'})")
        return {"status": "skipped_demo"}

    payload = {
        "from": FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if attachments:
        # Resend expects base64-encoded content per attachment:
        # [{"filename": "...", "content": "<base64 string>"}]
        payload["attachments"] = attachments

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10.0,
            )
            res.raise_for_status()
            return {"status": "sent", "response": res.json()}
    except Exception as e:
        print(f"[Email] Failed: {e}")
        return {"status": "failed", "error": str(e)}


def _format_location(lead: dict) -> str:
    loc_type = (lead.get("location_type") or "").lower()
    addr = lead.get("patient_address") or ""
    if loc_type == "home" and addr:
        return f"Home visit at {addr}"
    if loc_type == "clinic":
        return "Clinic visit (C2 Bateen Tower, 10th Floor, Abu Dhabi)"
    if loc_type == "online":
        return "Online session (link will be sent separately)"
    if loc_type == "clinic_or_home":
        return "At the clinic" if not addr else f"Home visit at {addr}"
    if loc_type == "clinic_or_online":
        return "At the clinic"
    return loc_type or "To be confirmed"


async def send_patient_confirmation(lead: dict, pdf_bytes: bytes = None, pdf_filename: str = "invoice.pdf") -> dict:
    """Send a patient-facing booking confirmation email."""
    email = lead.get("email") or ""
    if not email or "@" not in email:
        return {"status": "skipped_no_email"}

    patient_name = lead.get("patient_name") or "there"
    service = lead.get("service_name") or "appointment"
    midwife = lead.get("midwife_name") or "your midwife"
    date = lead.get("appointment_date") or "TBD"
    time = lead.get("appointment_time") or "TBD"
    duration = lead.get("duration_minutes") or 60
    price = lead.get("price_aed") or ""
    location = _format_location(lead)

    price_line = f"<p><b>Estimated price:</b> AED {price}</p>" if price else ""

    additional_visits = lead.get("additional_visits") or []
    if additional_visits:
        # Multi-visit program — list every visit instead of just the
        # single date/time row, and note the price covers all of them.
        visit_rows = f"""
        <tr><td style="padding: 4px 12px 4px 0;"><b>Visit 1:</b></td><td>{date} at {time} with {midwife}</td></tr>"""
        for i, v in enumerate(additional_visits, start=2):
            v_midwife = v.get("midwife_name") or midwife
            visit_rows += f"""
        <tr><td style="padding: 4px 12px 4px 0;"><b>Visit {i}:</b></td><td>{v.get('date','TBD')} at {v.get('time','TBD')} with {v_midwife}</td></tr>"""
        schedule_block = f"""
      <table style="border-collapse: collapse;">
        <tr><td style="padding: 4px 12px 4px 0;"><b>Service:</b></td><td>{service}</td></tr>
        {visit_rows}
        <tr><td style="padding: 4px 12px 4px 0;"><b>Duration per visit:</b></td><td>{duration} minutes</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Location:</b></td><td>{location}</td></tr>
      </table>"""
        price_line = f"<p><b>Total price (covers all {len(additional_visits) + 1} visits):</b> AED {price}</p>" if price else ""
    else:
        schedule_block = f"""
      <table style="border-collapse: collapse;">
        <tr><td style="padding: 4px 12px 4px 0;"><b>Service:</b></td><td>{service}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Midwife:</b></td><td>{midwife}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Date:</b></td><td>{date}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Time:</b></td><td>{time}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Duration:</b></td><td>{duration} minutes</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Location:</b></td><td>{location}</td></tr>
      </table>"""

    attachment_note = (
        '<p>📎 Your paid invoice is attached to this email as a PDF for your records.</p>'
        if pdf_bytes else ""
    )
    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px;">
      <h2>Your {CLINIC_NAME} appointment is confirmed</h2>
      <p>Dear {patient_name},</p>
      <p>Thank you for booking with {CLINIC_NAME}. Here are your details:</p>
      {schedule_block}
      {price_line}
      {attachment_note}
      <p>Our team will reach out if any further information is needed.</p>
      <p>To reschedule or cancel, please call {CLINIC_PHONE} or reply to this email.</p>
      <hr/>
      <p style="font-size: 12px; color: #888;">
        For medical emergencies, call Abu Dhabi emergency services on 998 or
        visit the nearest hospital. This is not an emergency service.
      </p>
    </div>
    """
    attachments = None
    if pdf_bytes:
        import base64
        attachments = [{"filename": pdf_filename, "content": base64.b64encode(pdf_bytes).decode("ascii")}]
    return await _send_email(email, f"Your {CLINIC_NAME} appointment", html, attachments=attachments)


async def send_clinic_notification(lead: dict) -> dict:
    """Send the clinic team a new-lead notification."""
    if not CLINIC_EMAIL:
        return {"status": "skipped_no_clinic_email"}

    patient_name = lead.get("patient_name") or "(name not provided)"
    phone = lead.get("phone") or "(no phone)"
    email = lead.get("email") or "(no email)"
    service = lead.get("service_name") or ""
    midwife = lead.get("midwife_name") or ""
    date = lead.get("appointment_date") or ""
    time = lead.get("appointment_time") or ""
    duration = lead.get("duration_minutes") or 60
    location = _format_location(lead)
    price = lead.get("price_aed") or ""
    notes = lead.get("notes") or ""
    source = lead.get("source") or "website"

    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px;">
      <h2>New booking — {service} for {patient_name}</h2>
      <table style="border-collapse: collapse;">
        <tr><td style="padding: 4px 12px 4px 0;"><b>Patient:</b></td><td>{patient_name}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Phone:</b></td><td>{phone}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Email:</b></td><td>{email}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Service:</b></td><td>{service}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Assigned midwife:</b></td><td>{midwife}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Date / time:</b></td><td>{date} at {time} ({duration} min)</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Location:</b></td><td>{location}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Price (AED):</b></td><td>{price or '-'}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Source:</b></td><td>{source}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Notes:</b></td><td>{notes or '-'}</td></tr>
      </table>
    </div>
    """
    return await _send_email(
        CLINIC_EMAIL,
        f"New {service} booking — {patient_name}",
        html,
    )


async def send_payment_proof_notification(invoice: dict, proof: dict = None) -> dict:
    """Email the clinic when a patient uploads a payment screenshot —
    otherwise staff would only find out by happening to check the
    dashboard's Payments page. Fires from both the website upload
    endpoint and the WhatsApp image handler (see main.py).

    `proof` (optional) is the most recent PaymentProof row for this
    invoice — when provided and it's an image, it's embedded directly in
    the email so staff can actually see the screenshot and decide without
    opening the dashboard first, which is the whole point of also having
    one-click Approve/Reject links below."""
    if not CLINIC_EMAIL:
        return {"status": "skipped_no_clinic_email"}

    lead = invoice.get("lead", {}) or {}
    patient_name = lead.get("patient_name") or invoice.get("patient_name") or "(name not provided)"
    service = lead.get("service_name") or ""
    reference = invoice.get("reference", "")
    amount = invoice.get("amount_aed", "")
    channel = invoice.get("channel", "")
    invoice_id = invoice.get("id", "")
    token = invoice.get("action_token", "")

    screenshot_html = ""
    if proof and proof.get("file_path"):
        file_path = proof["file_path"]
        is_image = file_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".heic"))
        if BACKEND_BASE_URL:
            proof_url = f"{BACKEND_BASE_URL}{file_path}"
            if is_image:
                screenshot_html = f"""
      <div style="margin: 16px 0;">
        <img src="{proof_url}" alt="Payment screenshot" style="max-width: 100%; border-radius: 12px; border: 1px solid #ddd;">
      </div>"""
            else:
                screenshot_html = f'<p style="margin: 12px 0;"><a href="{proof_url}">View uploaded proof (not an image — click to open)</a></p>'
        else:
            # Can't build an absolute URL without knowing the backend's
            # own public address — most email clients block relative
            # image paths anyway, so this would just show a broken image.
            screenshot_html = (
                '<p style="font-size: 12px; color: #888;">'
                "(Screenshot uploaded — set FRONTEND_BASE_URL in .env to "
                "show it directly in this email; for now, view it on the "
                "dashboard.)</p>"
            )

    action_buttons = ""
    if BACKEND_BASE_URL and invoice_id and token:
        approve_url = f"{BACKEND_BASE_URL}/invoices/{invoice_id}/approve-via-link?token={token}"
        reject_url = f"{BACKEND_BASE_URL}/invoices/{invoice_id}/reject-via-link?token={token}"
        action_buttons = f"""
      <div style="margin-top: 20px;">
        <a href="{approve_url}" style="display: inline-block; background: #3e4b33; color: #fff;
           padding: 10px 22px; border-radius: 999px; text-decoration: none; font-weight: bold;
           margin-right: 10px;">Approve</a>
        <a href="{reject_url}" style="display: inline-block; background: #93000a; color: #fff;
           padding: 10px 22px; border-radius: 999px; text-decoration: none; font-weight: bold;">Reject</a>
      </div>
      <p style="font-size: 12px; color: #888; margin-top: 12px;">
        These links approve/reject directly from this email — no login needed.
      </p>"""

    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px;">
      <h2>Payment screenshot uploaded — {service} for {patient_name}</h2>
      <p>A patient has just uploaded a payment screenshot. Please review and
      approve or reject it{' below, or ' if action_buttons else ' '}on the dashboard's Payments page.</p>
      <table style="border-collapse: collapse;">
        <tr><td style="padding: 4px 12px 4px 0;"><b>Patient:</b></td><td>{patient_name}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Service:</b></td><td>{service}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Amount (AED):</b></td><td>{amount}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Reference:</b></td><td>{reference}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Received via:</b></td><td>{channel}</td></tr>
      </table>
      {screenshot_html}
      {action_buttons}
    </div>
    """
    return await _send_email(
        CLINIC_EMAIL,
        f"Payment screenshot received — {reference}",
        html,
    )
