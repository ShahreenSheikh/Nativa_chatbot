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

# Startup diagnostic so we can see what was actually loaded from .env.
# If this prints the wrong value, your .env file isn't where dotenv is
# looking — usually because uvicorn is launched from a different folder
# than the .env file. If GOOGLE_CALENDAR_ID looks like a URL (starts
# with "http"), it's wrong — Google's API wants the Calendar ID, not
# the calendar's web URL. Get it from Calendar settings → "Integrate
# calendar" → "Calendar ID" field.
if not CALENDAR_ID:
    print("[Calendar startup] GOOGLE_CALENDAR_ID is EMPTY in env. "
          "Bookings will be saved but no Calendar events will be created.")
elif CALENDAR_ID.startswith("http") or "/" in CALENDAR_ID or " " in CALENDAR_ID:
    print(f"[Calendar startup] WARNING: GOOGLE_CALENDAR_ID looks invalid "
          f"({CALENDAR_ID!r}). It should be an ID like "
          f"'xxx@group.calendar.google.com', NOT a URL. Calendar writes "
          f"will fail with 404 errors.")
else:
    # Show first 12 chars + last 30 chars so you can tell it loaded
    # correctly without exposing the full ID to logs.
    masked = CALENDAR_ID
    if len(CALENDAR_ID) > 50:
        masked = CALENDAR_ID[:12] + "..." + CALENDAR_ID[-30:]
    print(f"[Calendar startup] Configured with calendar: {masked}")


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
        payment_status = lead.get("payment_status", "")

        # Location field: if home visit and we have an address, put it there
        location_field = ""
        if location_type == "home" and patient_address:
            location_field = patient_address
        elif location_type == "clinic":
            location_field = "C2 Bateen Tower, 10th Floor, Abu Dhabi"
        elif location_type == "online":
            location_field = "Online"

        # Payment status line — only present if the bot collected payment.
        # Flags to clinic that payment needs manual verification (until
        # real Stripe webhook integration lands).
        payment_line = ""
        if payment_status == "pending_verification":
            payment_line = "⚠️ PAYMENT: pending verification (patient reported paid via chatbot placeholder)"
        elif payment_status == "verified":
            payment_line = "✅ PAYMENT: verified"

        description_parts = [
            f"Patient: {patient_name}",
            f"Service: {service_name}",
            f"Midwife: {midwife_name}",
            f"Phone: {phone}" if phone else "",
            f"Email: {email}" if email else "",
            f"Location: {location_type}" if location_type else "",
            f"Address: {patient_address}" if patient_address else "",
            f"Price (AED): {price}" if price else "",
            payment_line if payment_line else "",
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

        # NOTE: We do NOT add the patient as a Google Calendar attendee.
        # Google blocks service accounts from inviting attendees without
        # Domain-Wide Delegation of Authority (a Workspace-admin-level
        # config that's overkill for a clinic chatbot). The error you'd
        # see is:
        #   "Service accounts cannot invite attendees without Domain-Wide
        #    Delegation of Authority."
        # Instead, the patient's email is included in the event description
        # (above) so the clinic can see who to contact. The patient gets
        # their booking confirmation via Resend (email_service.py),
        # NOT via Google Calendar invite.

        service = build(
            "calendar", "v3",
            credentials=get_credentials(
                ["https://www.googleapis.com/auth/calendar"]
            ),
        )
        created = service.events().insert(
            calendarId=CALENDAR_ID, body=event,
            # No attendees, so no need for sendUpdates either
            sendUpdates="none",
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


def create_appointment_events(lead: dict) -> list:
    """Create one calendar event per visit for a booking — a plain single
    event for an ordinary booking, or one event per date for a multi-visit
    program (see lead["additional_visits"], set by ai.py's multi-visit
    scheduling loop). Each additional visit reuses the same patient/
    service/midwife details from `lead`, just with its own date/time.

    Returns a list of status dicts (same shape as create_appointment_event
    returns for one event) — always at least one entry for the primary
    visit, even if every visit fails, so callers can inspect what
    happened per visit rather than getting a single pass/fail flag for
    the whole program."""
    additional = lead.get("additional_visits") or []
    total_visits = len(additional) + 1

    primary_lead = lead
    if total_visits > 1:
        # Tag the first event too, so all N events look consistent to
        # staff browsing the calendar — otherwise only events 2..N
        # mention they're part of a program and the first looks like an
        # unrelated one-off booking.
        primary_lead = dict(lead)
        original_notes = primary_lead.get("notes", "")
        primary_lead["notes"] = (
            f"Visit 1 of {total_visits} — part of a multi-visit program."
            + (f"\n{original_notes}" if original_notes else "")
        )
    statuses = [create_appointment_event(primary_lead)]

    for i, visit in enumerate(additional, start=2):
        visit_lead = dict(lead)
        visit_lead["appointment_date"] = visit.get("date", "")
        visit_lead["appointment_time"] = visit.get("time", "")
        if visit.get("midwife_name"):
            visit_lead["midwife_name"] = visit["midwife_name"]
        if visit.get("midwife_id"):
            visit_lead["midwife_id"] = visit["midwife_id"]
        # Tag which visit number this is in the calendar description, so
        # staff looking at the calendar can tell it's part of a program
        # rather than a stray duplicate-looking booking.
        original_notes = visit_lead.get("notes", "")
        visit_lead["notes"] = (
            f"Visit {i} of {total_visits} — part of a multi-visit program."
            + (f"\n{original_notes}" if original_notes else "")
        )
        statuses.append(create_appointment_event(visit_lead))

    return statuses