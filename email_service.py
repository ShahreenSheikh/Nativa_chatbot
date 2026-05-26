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


async def _send_email(to: str, subject: str, html: str) -> dict:
    if not RESEND_API_KEY or not FROM_EMAIL or not to:
        print(f"[Email] Skipped (to={to or 'missing'}, "
              f"key={'set' if RESEND_API_KEY else 'missing'}, "
              f"from={'set' if FROM_EMAIL else 'missing'})")
        return {"status": "skipped_demo"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": FROM_EMAIL,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                },
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


async def send_patient_confirmation(lead: dict) -> dict:
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

    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px;">
      <h2>Your {CLINIC_NAME} appointment is confirmed</h2>
      <p>Dear {patient_name},</p>
      <p>Thank you for booking with {CLINIC_NAME}. Here are your details:</p>
      <table style="border-collapse: collapse;">
        <tr><td style="padding: 4px 12px 4px 0;"><b>Service:</b></td><td>{service}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Midwife:</b></td><td>{midwife}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Date:</b></td><td>{date}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Time:</b></td><td>{time}</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Duration:</b></td><td>{duration} minutes</td></tr>
        <tr><td style="padding: 4px 12px 4px 0;"><b>Location:</b></td><td>{location}</td></tr>
      </table>
      {price_line}
      <p>Our team will reach out if any further information is needed.</p>
      <p>To reschedule or cancel, please call {CLINIC_PHONE} or reply to this email.</p>
      <hr/>
      <p style="font-size: 12px; color: #888;">
        For medical emergencies, call Abu Dhabi emergency services on 998 or
        visit the nearest hospital. This is not an emergency service.
      </p>
    </div>
    """
    return await _send_email(email, f"Your {CLINIC_NAME} appointment", html)


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
