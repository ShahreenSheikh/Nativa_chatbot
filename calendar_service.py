"""
Google Calendar integration for NativaCare. Write-only.

Creates a calendar event when a booking is committed. Does NOT read existing
events — availability is computed from the sheet + in-memory bookings (see
availability.py). If you want real Calendar-aware availability, that's a
follow-up: add a read function here, then call it from availability.py.

If GOOGLE_CALENDAR_ID is missing or the credentials aren't set up, this
returns a "skipped_demo" status instead of crashing — same pattern as the
Dubai bot, so dev/demo deployments work without Calendar configured.

Public API:
    create_appointment_event(lead_dict) -> dict
"""

import os
from datetime import datetime, timedelta
import dateparser
from dotenv import load_dotenv

load_dotenv()

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")
DEFAULT_TZ = os.getenv("CLINIC_TIMEZONE", "Asia/Dubai")


def create_appointment_event(lead: dict) -> dict:
    """Create a Google Calendar event for a confirmed booking.

    Takes a plain dict (from AppointmentLead.model_dump() or equivalent).
    Returns a status dict — never raises.
    """
    if not CALENDAR_ID:
        print("[Calendar] GOOGLE_CALENDAR_ID missing. Demo mode only.")
        return {"status": "skipped_demo", "reason": "GOOGLE_CALENDAR_ID missing"}

    try:
        from googleapiclient.discovery import build
        from credentials import get_credentials

        # Parse start time
        date_str = lead.get("appointment_date", "")
        time_str = lead.get("appointment_time", "")
        if not date_str or not time_str:
            return {"status": "failed", "error": "Missing date or time"}

        parsed = dateparser.parse(
            f"{date_str} {time_str}",
            settings={
                "TIMEZONE": DEFAULT_TZ,
                "RETURN_AS_TIMEZONE_AWARE": True,
            },
        )
        if not parsed:
            return {"status": "failed",
                    "error": f"Could not parse '{date_str} {time_str}'"}

        # Duration
        try:
            duration = int(lead.get("duration_minutes") or 60)
        except (TypeError, ValueError):
            duration = 60

        start_dt = parsed
        end_dt = start_dt + timedelta(minutes=duration)

        # Build event body — note: real newlines, not "\\n"
        patient_name = lead.get("patient_name", "") or "Patient"
        midwife_name = lead.get("midwife_name", "") or "TBD"
        service_name = lead.get("service_name", "") or "Appointment"
        location_type = lead.get("location_type", "") or ""
        patient_address = lead.get("patient_address", "")
        phone = lead.get("phone", "")
        email = lead.get("email", "")
        price = lead.get("price_aed", "")
        notes = lead.get("notes", "")

        # Location field: if home visit and we have an address, put it there
        location_field = ""
        if location_type == "home" and patient_address:
            location_field = patient_address
        elif location_type == "clinic":
            location_field = "C2 Bateen Tower, 10th Floor, Abu Dhabi"
        elif location_type == "online":
            location_field = "Online"

        description_parts = [
            f"Patient: {patient_name}",
            f"Service: {service_name}",
            f"Midwife: {midwife_name}",
            f"Phone: {phone}" if phone else "",
            f"Email: {email}" if email else "",
            f"Location: {location_type}" if location_type else "",
            f"Address: {patient_address}" if patient_address else "",
            f"Price (AED): {price}" if price else "",
            f"Notes: {notes}" if notes else "",
            f"Booked via {CLINIC_NAME} chatbot",
        ]
        description = "\n".join(p for p in description_parts if p)

        event = {
            "summary": f"{service_name} — {patient_name} ({midwife_name})",
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": DEFAULT_TZ},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": DEFAULT_TZ},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 60}],
            },
        }
        if location_field:
            event["location"] = location_field

        # If patient email looks valid, invite them so they get a reminder too
        if email and "@" in email:
            event["attendees"] = [{"email": email}]

        service = build(
            "calendar", "v3",
            credentials=get_credentials(
                ["https://www.googleapis.com/auth/calendar"]
            ),
        )
        created = service.events().insert(
            calendarId=CALENDAR_ID, body=event,
            sendUpdates="all" if email else "none",
        ).execute()

        print(f"[Calendar] Event created: {created.get('htmlLink')}")
        return {
            "status": "created",
            "link": created.get("htmlLink"),
            "event_id": created.get("id"),
        }

    except Exception as e:
        print(f"[Calendar] Error creating appointment: {e}")
        return {"status": "failed", "error": str(e)}
