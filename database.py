"""
Sheet-backed data layer for the NativaCare midwifery chatbot.

Reads 8 tabs from a Google Sheet (exported as CSV via the gviz/tq URL — no
authentication needed for public-read sheets, same pattern as Dubai bot):
    services, midwives, midwife_services, midwife_schedule_weekly,
    midwife_schedule_overrides, packages, faqs, clinic_info

If GOOGLE_SHEET_ID is missing or the fetch fails, every getter returns a
DEMO fallback so the bot still works for development. All demo data is
clearly tagged with "[DEMO]" so a real patient seeing it would notice.

Public API (used by other modules):
    get_services()                  -> list[dict]
    get_midwives()                  -> list[dict]
    get_midwife_services()          -> list[dict]   (link table)
    get_weekly_schedule()           -> list[dict]
    get_schedule_overrides()        -> list[dict]
    get_packages()                  -> list[dict]
    get_faqs(language="en")         -> list[dict]
    get_clinic_info()               -> dict
    save_appointment(lead)          -> None  (in-memory append for now)
    get_saved_appointments()        -> list[dict]
"""

import os
import csv
import httpx
from datetime import datetime
from dotenv import load_dotenv

from models import AppointmentLead

load_dotenv()

SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

# Startup log — print at module load so you can see at a glance which
# data source the bot is using when uvicorn boots.
if SHEET_ID:
    print(f"[Sheets] Configured. Reading data from sheet ID "
          f"{SHEET_ID[:8]}...{SHEET_ID[-4:]} (length {len(SHEET_ID)}).")
else:
    print("[Sheets] GOOGLE_SHEET_ID is NOT set. Using DEMO fallback data. "
          "Add GOOGLE_SHEET_ID to .env to read from your real sheet.")


# ---------------------------------------------------------------------------
# Per-session cache hook
# ---------------------------------------------------------------------------
# ai.py calls set_session_cache(session_dict) at the start of every request
# and set_session_cache(None) before returning. While set, every call to
# _fetch_tab(tab) caches its result on that session, so repeated fetches
# within one chat reuse the data instead of hitting Google Sheets again.
#
# The cache is strictly per-session: a new chat (new session_id) starts a
# fresh cache, so the bot picks up sheet edits when the user starts a new
# conversation. Sessions expire after their TTL, so caches don't linger.

_CURRENT_CACHE: dict | None = None


def set_session_cache(cache_holder: dict | None):
    """Point the fetcher at a per-session cache dict. Call with None to
    clear. The `cache_holder` is the session dict itself; we'll create a
    sub-dict under "_cache" so we don't clutter session top-level keys."""
    global _CURRENT_CACHE
    if cache_holder is None:
        _CURRENT_CACHE = None
    else:
        cache_holder.setdefault("_cache", {})
        _CURRENT_CACHE = cache_holder["_cache"]


# ---------------------------------------------------------------------------
# Low-level fetcher
# ---------------------------------------------------------------------------

def _url(tab: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={tab}"
    )


async def _fetch_tab(tab: str) -> list:
    """Fetch one tab and parse as CSV. Returns [] on any failure.

    Uses the per-session cache when one is set. Cache hits avoid the
    network round-trip entirely."""
    if _CURRENT_CACHE is not None and tab in _CURRENT_CACHE:
        return _CURRENT_CACHE[tab]

    if not SHEET_ID:
        print(f"[Sheets] GOOGLE_SHEET_ID missing. Using fallback for '{tab}'.")
        result: list = []
    else:
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(_url(tab), timeout=8.0)
                res.raise_for_status()
            lines = res.text.strip().splitlines()
            result = list(csv.DictReader(lines))
        except Exception as e:
            print(f"[Sheets] Could not fetch '{tab}': {e}")
            result = []

    if _CURRENT_CACHE is not None:
        _CURRENT_CACHE[tab] = result
    return result


def _clean(value, limit=500):
    return str(value or "").strip()[:limit]


def _truthy(value) -> bool:
    return _clean(value).lower() in ("true", "yes", "1", "y")


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

async def get_services() -> list:
    rows = await _fetch_tab("services")
    data = [
        {
            "service_id": _clean(r.get("service_id")),
            "service_name": _clean(r.get("service_name")),
            "category": _clean(r.get("category")),
            "description": _clean(r.get("description")),
            "default_price_aed": _clean(r.get("default_price_aed")),
            "duration_minutes": _clean(r.get("duration_minutes"), 10),
            "location_type": _clean(r.get("location_type"), 30),
            "available": _truthy(r.get("available", "TRUE")),
            "demo_notice": _clean(r.get("demo_notice")),
        }
        for r in rows
        if _clean(r.get("service_name"))
    ]
    services = [s for s in data if s["available"]]
    return services or _DEMO_SERVICES


# ---------------------------------------------------------------------------
# Midwives
# ---------------------------------------------------------------------------

async def get_midwives() -> list:
    rows = await _fetch_tab("midwives")
    data = [
        {
            "midwife_id": _clean(r.get("midwife_id")),
            "midwife_name": _clean(r.get("midwife_name")),
            "qualifications": _clean(r.get("qualifications"), 400),
            "languages": _clean(r.get("languages")),
            "years_experience": _clean(r.get("years_experience"), 10),
            "bio_short": _clean(r.get("bio_short"), 600),
            "active": _truthy(r.get("active", "TRUE")),
            "demo_notice": _clean(r.get("demo_notice")),
        }
        for r in rows
        if _clean(r.get("midwife_name"))
    ]
    midwives = [m for m in data if m["active"]]
    return midwives or _DEMO_MIDWIVES


# ---------------------------------------------------------------------------
# Midwife <-> Service link table (per-midwife price overrides)
# ---------------------------------------------------------------------------

async def get_midwife_services() -> list:
    rows = await _fetch_tab("midwife_services")
    data = [
        {
            "midwife_id": _clean(r.get("midwife_id")),
            "service_id": _clean(r.get("service_id")),
            "price_override_aed": _clean(r.get("price_override_aed"), 20),
            "notes": _clean(r.get("notes")),
        }
        for r in rows
        if _clean(r.get("midwife_id")) and _clean(r.get("service_id"))
    ]
    return data or _DEMO_MIDWIFE_SERVICES


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

async def get_weekly_schedule() -> list:
    rows = await _fetch_tab("midwife_schedule_weekly")
    data = [
        {
            "midwife_id": _clean(r.get("midwife_id")),
            "day_of_week": _clean(r.get("day_of_week"), 5),
            "start_time": _clean(r.get("start_time"), 10),
            "end_time": _clean(r.get("end_time"), 10),
            # block_type is "work" or "break"; default to "work" if blank
            "block_type": _clean(r.get("block_type"), 20).lower() or "work",
            "notes": _clean(r.get("notes")),
        }
        for r in rows
        if _clean(r.get("midwife_id")) and _clean(r.get("day_of_week"))
    ]
    return data or _DEMO_WEEKLY_SCHEDULE


async def get_schedule_overrides() -> list:
    rows = await _fetch_tab("midwife_schedule_overrides")
    data = [
        {
            "midwife_id": _clean(r.get("midwife_id")),
            "date": _clean(r.get("date"), 12),
            "start_time": _clean(r.get("start_time"), 10),
            "end_time": _clean(r.get("end_time"), 10),
            # "unavailable" or "extra"
            "override_type": _clean(r.get("override_type"), 20).lower(),
            "notes": _clean(r.get("notes")),
        }
        for r in rows
        if _clean(r.get("midwife_id")) and _clean(r.get("date"))
    ]
    return data  # no demo fallback needed; absence = no overrides


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

async def get_packages() -> list:
    rows = await _fetch_tab("packages")
    data = [
        {
            "package_id": _clean(r.get("package_id")),
            "package_name": _clean(r.get("package_name")),
            "included_services": _clean(r.get("included_services"), 200),
            "total_price_aed": _clean(r.get("total_price_aed"), 20),
            "validity_days": _clean(r.get("validity_days"), 10),
            "description": _clean(r.get("description"), 600),
            "active": _truthy(r.get("active", "TRUE")),
            "demo_notice": _clean(r.get("demo_notice")),
        }
        for r in rows
        if _clean(r.get("package_name"))
    ]
    packages = [p for p in data if p["active"]]
    return packages or _DEMO_PACKAGES


# ---------------------------------------------------------------------------
# FAQs
# ---------------------------------------------------------------------------

async def get_faqs(language: str = "en") -> list:
    rows = await _fetch_tab("faqs")
    data = [
        {
            "faq_id": _clean(r.get("faq_id")),
            "category": _clean(r.get("category")),
            "question": _clean(r.get("question"), 400),
            "answer": _clean(r.get("answer"), 1200),
            "language": _clean(r.get("language", language), 5).lower() or "en",
            "source": _clean(r.get("source"), 20),
        }
        for r in rows
        if _clean(r.get("question"))
    ]
    filtered = [f for f in data if f["language"] == language.lower()]
    return filtered or _DEMO_FAQS


# ---------------------------------------------------------------------------
# Clinic info
# ---------------------------------------------------------------------------

async def get_clinic_info() -> dict:
    rows = await _fetch_tab("clinic_info")
    info = {}
    for r in rows:
        key = _clean(r.get("field"), 60)
        if key:
            info[key] = _clean(r.get("value"), 800)
    if info:
        return info
    return _DEMO_CLINIC_INFO


# ---------------------------------------------------------------------------
# Appointment storage (in-memory; swap for DB later)
# ---------------------------------------------------------------------------

_leads_store: list[AppointmentLead] = []


def save_appointment(lead: AppointmentLead):
    lead.created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    _leads_store.append(lead)
    print("\n" + "=" * 40)
    print(f"NEW NATIVACARE APPOINTMENT — {lead.patient_name}")
    print(f"Service: {lead.service_name}")
    print(f"Midwife: {lead.midwife_name}")
    print(f"When: {lead.appointment_date} at {lead.appointment_time}")
    print(f"Location: {lead.location_type}"
          + (f" ({lead.patient_address})" if lead.patient_address else ""))
    print(f"Phone: {lead.phone}")
    print(f"Email: {lead.email}")
    print("=" * 40 + "\n")


def get_saved_appointments() -> list:
    return [l.model_dump() for l in _leads_store]


def get_existing_bookings_for(midwife_id: str, date: str) -> list:
    """Return existing bookings (from this server's memory) for a given
    midwife on a given date. Used by the availability engine to remove
    already-booked slots."""
    return [
        l.model_dump() for l in _leads_store
        if l.midwife_id == midwife_id
        and l.appointment_date == date
        and l.status not in ("cancelled", "rejected")
    ]


# ===========================================================================
# DEMO FALLBACK DATA
# All marked with [DEMO]. Bot will use this if no sheet is configured.
# These mirror the CSV structure described in the build spec.
# ===========================================================================

_DEMO_SERVICES = [
    {"service_id": "S001", "service_name": "Preconception Consultation",
     "category": "Preconception", "description": "Initial consultation for couples planning pregnancy.",
     "default_price_aed": "400", "duration_minutes": "60",
     "location_type": "clinic_or_online", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S002", "service_name": "My 10 Lunar Month Pregnancy",
     "category": "Pregnancy", "description": "Comprehensive pregnancy tracking and education.",
     "default_price_aed": "800", "duration_minutes": "90",
     "location_type": "clinic_or_home", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S004", "service_name": "Routine Antenatal Visit",
     "category": "Pregnancy", "description": "Standard antenatal check-up.",
     "default_price_aed": "350", "duration_minutes": "45",
     "location_type": "clinic_or_home", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S005", "service_name": "Hypnobirthing Class",
     "category": "Workshop", "description": "Birth preparation using hypnobirthing techniques.",
     "default_price_aed": "500", "duration_minutes": "90",
     "location_type": "clinic", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S013", "service_name": "Routine Postnatal Care",
     "category": "Postnatal", "description": "Standard postnatal home visit.",
     "default_price_aed": "400", "duration_minutes": "60",
     "location_type": "home", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S014", "service_name": "Breastfeeding Support",
     "category": "Postnatal", "description": "One-to-one breastfeeding consultation.",
     "default_price_aed": "350", "duration_minutes": "60",
     "location_type": "home", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S017", "service_name": "My Baby Care",
     "category": "Newborn", "description": "Newborn care education and hands-on support.",
     "default_price_aed": "400", "duration_minutes": "90",
     "location_type": "home", "available": True,
     "demo_notice": "[DEMO] Replace with real price"},
]

_DEMO_MIDWIVES = [
    {"midwife_id": "M001", "midwife_name": "Najat",
     "qualifications": "[DEMO] Founder; Senior Registered Midwife",
     "languages": "English Arabic French", "years_experience": "20",
     "bio_short": "[DEMO] Founder of NativaCare. Specialist in prenatal preparation and birth support.",
     "active": True, "demo_notice": "[DEMO] Verify with clinic"},
    {"midwife_id": "M002", "midwife_name": "Hani",
     "qualifications": "[DEMO] Registered Midwife; postnatal specialist",
     "languages": "English Arabic", "years_experience": "15",
     "bio_short": "[DEMO] Specialist in postnatal recovery.",
     "active": True, "demo_notice": "[DEMO] Verify with clinic"},
    {"midwife_id": "M003", "midwife_name": "Fiona",
     "qualifications": "Registered Midwife; Degree in Midwifery; Specialist Perinatal Mental Health",
     "languages": "English", "years_experience": "12",
     "bio_short": "Specialist in low and high risk pregnancies and perinatal mental health.",
     "active": True, "demo_notice": "From website"},
]

_DEMO_MIDWIFE_SERVICES = [
    # Najat (M001) — preconception, pregnancy, workshops
    {"midwife_id": "M001", "service_id": "S001", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S002", "price_override_aed": "900",  "notes": "[DEMO] Senior tier"},
    {"midwife_id": "M001", "service_id": "S003", "price_override_aed": "2800", "notes": "[DEMO] Senior caseload"},
    {"midwife_id": "M001", "service_id": "S004", "price_override_aed": "400",  "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S005", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S006", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S007", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S008", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S009", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S010", "price_override_aed": "650",  "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S011", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S012", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S013", "price_override_aed": "450",  "notes": "[DEMO]"},
    {"midwife_id": "M001", "service_id": "S023", "price_override_aed": "",     "notes": "[DEMO]"},

    # Hani (M002) — postnatal and newborn care
    {"midwife_id": "M002", "service_id": "S004", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S013", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S014", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S015", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S016", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S017", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S018", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S019", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S020", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S021", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S022", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M002", "service_id": "S023", "price_override_aed": "",     "notes": "[DEMO]"},

    # Fiona (M003) — antenatal, mental health, generalist
    {"midwife_id": "M003", "service_id": "S001", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S002", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S003", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S004", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S005", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S006", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S007", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S009", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S010", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S011", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S012", "price_override_aed": "",     "notes": "[DEMO] Mental health specialty"},
    {"midwife_id": "M003", "service_id": "S013", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S014", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S015", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S016", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S017", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S018", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S019", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S020", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S021", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S022", "price_override_aed": "",     "notes": "[DEMO]"},
    {"midwife_id": "M003", "service_id": "S023", "price_override_aed": "",     "notes": "[DEMO]"},
]

_DEMO_WEEKLY_SCHEDULE = [
    # Najat: Mon-Thu 9-5 with lunch, Fri half-day
    {"midwife_id": "M001", "day_of_week": "Mon", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Mon", "start_time": "12:00", "end_time": "13:00", "block_type": "break", "notes": "Lunch"},
    {"midwife_id": "M001", "day_of_week": "Mon", "start_time": "13:00", "end_time": "17:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Tue", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Tue", "start_time": "13:00", "end_time": "17:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Wed", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Wed", "start_time": "13:00", "end_time": "17:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Thu", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Thu", "start_time": "13:00", "end_time": "17:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M001", "day_of_week": "Fri", "start_time": "09:00", "end_time": "13:00", "block_type": "work", "notes": "Half day"},
    # Hani: weekends + Mon-Wed mornings
    {"midwife_id": "M002", "day_of_week": "Mon", "start_time": "10:00", "end_time": "14:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M002", "day_of_week": "Tue", "start_time": "10:00", "end_time": "14:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M002", "day_of_week": "Wed", "start_time": "10:00", "end_time": "14:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M002", "day_of_week": "Sat", "start_time": "10:00", "end_time": "17:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M002", "day_of_week": "Sun", "start_time": "10:00", "end_time": "17:00", "block_type": "work", "notes": ""},
    # Fiona: Tue-Sat, longer days
    {"midwife_id": "M003", "day_of_week": "Tue", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Tue", "start_time": "13:00", "end_time": "18:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Wed", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Wed", "start_time": "13:00", "end_time": "18:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Thu", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Thu", "start_time": "13:00", "end_time": "18:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Fri", "start_time": "09:00", "end_time": "12:00", "block_type": "work", "notes": ""},
    {"midwife_id": "M003", "day_of_week": "Sat", "start_time": "10:00", "end_time": "15:00", "block_type": "work", "notes": ""},
]

_DEMO_PACKAGES = [
    {"package_id": "P001", "package_name": "Pregnancy Foundation",
     "included_services": "S001 S002 S005", "total_price_aed": "1500",
     "validity_days": "180",
     "description": "[DEMO] Preconception consult plus pregnancy tracking plus hypnobirthing.",
     "active": True, "demo_notice": "[DEMO] Replace with real package"},
    {"package_id": "P003", "package_name": "Postnatal Recovery Bundle",
     "included_services": "S013 S014", "total_price_aed": "1000",
     "validity_days": "90",
     "description": "[DEMO] Three postnatal home visits plus breastfeeding support.",
     "active": True, "demo_notice": "[DEMO] Replace with real package"},
    {"package_id": "P004", "package_name": "Newborn Starter",
     "included_services": "S017", "total_price_aed": "1000",
     "validity_days": "60",
     "description": "[DEMO] Baby care home visits.",
     "active": True, "demo_notice": "[DEMO] Replace with real package"},
]

_DEMO_FAQS = [
    {"faq_id": "F001", "category": "Antenatal",
     "question": "How soon should I start the first antenatal class?",
     "answer": "In general we start around 4 months of pregnancy. Some women start earlier — it depends on your expectations and needs.",
     "language": "en", "source": "website"},
    {"faq_id": "F003", "category": "Pregnancy",
     "question": "Is exercise safe during pregnancy?",
     "answer": "Moderate exercise like walking, swimming or prenatal yoga is generally safe and beneficial.",
     "language": "en", "source": "website"},
    {"faq_id": "F008", "category": "Logistics",
     "question": "Where are you located?",
     "answer": "We are at C2 Bateen Tower, 10th Floor, Al Hirdiyah Street, Marina, Abu Dhabi.",
     "language": "en", "source": "bot-essential"},
    {"faq_id": "F009", "category": "Logistics",
     "question": "Do you do home visits?",
     "answer": "Yes — many services are offered as home visits across Abu Dhabi.",
     "language": "en", "source": "bot-essential"},
    {"faq_id": "F013", "category": "Booking",
     "question": "How do I book?",
     "answer": "You can book through this chat, by phone at +971 50 729 7197, or by email at info@nativacare.com.",
     "language": "en", "source": "bot-essential"},
    {"faq_id": "F015", "category": "Emergency",
     "question": "What if I have an emergency?",
     "answer": "For medical emergencies call Abu Dhabi emergency services on 998 or go to the nearest hospital. We are not an emergency service.",
     "language": "en", "source": "bot-essential"},
]

_DEMO_CLINIC_INFO = {
    "clinic_name": "NativaCare",
    "tagline": "The first Abu Dhabi community midwives services",
    "address": "C2 Bateen Tower, 6 Al Hirdiyah Street, 10th Floor, Abu Dhabi",
    "phone": "+971507297197",
    "email": "info@nativacare.com",
    "website": "https://nativacare.com",
    "working_hours_general": "Sun-Fri 9am-6pm (per midwife schedule varies)",
    "languages_supported": "English Arabic French",
    "emergency_number": "998",
    "service_areas": "[DEMO] Abu Dhabi - all major areas",
    "default_appointment_duration_minutes": "60",
    "default_slot_granularity_minutes": "30",
    "buffer_between_appointments_minutes": "15",
    "currency": "AED",
    "timezone": "Asia/Dubai",
    "demo_mode": "TRUE",
    "demo_warning": ("This deployment uses placeholder pricing, schedules and "
                     "policies. Verify all details with the clinic before booking."),
}