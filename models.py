"""
Pydantic models for the NativaCare midwifery chatbot.

Kept intentionally similar to the Dubai bot for consistency, but with
midwife/service-slot fields instead of doctor/specialty fields.
"""

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str
    message: str
    source: Optional[str] = "website"


class TimeSlot(BaseModel):
    """A single bookable time slot."""
    start_time: str  # "HH:MM" 24h
    end_time: str    # "HH:MM" 24h
    midwife_id: str
    midwife_name: str


class DayAvailability(BaseModel):
    """All available slots for one date."""
    date: str        # "YYYY-MM-DD"
    weekday: str     # "Mon" / "Tue" / ...
    slots: List[TimeSlot]


class AppointmentLead(BaseModel):
    """One appointment record. Same field names as Dubai's AppointmentLead
    where they overlap, so logging / saving code is shared-shape."""
    session_id: str
    patient_name: str = ""
    email: str = ""
    phone: str = ""
    service_id: str = ""
    service_name: str = ""
    midwife_id: str = ""
    midwife_name: str = ""
    location_type: str = ""        # "home" / "clinic" / "online"
    patient_address: str = ""      # required for home visits
    appointment_date: str = ""     # YYYY-MM-DD — for multi-visit programs,
                                    # this is the FIRST (earliest) visit;
                                    # the rest are in additional_visits.
    appointment_time: str = ""     # HH:MM (start time)
    duration_minutes: int = 60
    price_aed: str = ""            # TOTAL price for the whole booking —
                                    # for a multi-visit program this is the
                                    # program price, not a per-visit price.
    package_id: str = ""           # if booked from a package
    insurance: str = ""            # placeholder; not used in v1
    language: str = "en"
    source: str = "website"
    status: str = "pending"
    notes: str = ""
    payment_status: str = ""       # "" / "pending_verification" / "verified"
    created_at: str = ""
    additional_visits: list = []
    # For multi-visit programs (e.g. "Postnatal Recovery Program — 3
    # visits"): each entry is {"date": "YYYY-MM-DD", "time": "HH:MM",
    # "midwife_id": str, "midwife_name": str} for visit 2, 3, ... N. Empty
    # for an ordinary single-visit booking — every existing code path that
    # only knows about one appointment_date/time keeps working unchanged.


class AppointmentConfirmation(BaseModel):
    """Returned to the front-end after a successful booking."""
    booking_id: str
    patient_name: str
    service_name: str
    midwife_name: str
    appointment_date: str
    appointment_time: str
    duration_minutes: int
    location_type: str
    patient_address: Optional[str] = None
    price_aed: Optional[str] = None
    calendar_link: Optional[str] = None