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
import time as _time
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
# Shared cross-session cache
# ---------------------------------------------------------------------------
# The per-session cache above only helps WITHIN one conversation — every
# brand new chat still started completely cold, meaning its first message
# that happened to need a given tab (services, midwife_schedule_overrides,
# whichever) paid a full live network round-trip to Google Sheets right
# then, with an 8s timeout in the worst case. Since different conversations
# need different tabs at different points, this showed up as the bot
# randomly stalling on what looked like an arbitrary message — it wasn't
# random at all, it was just "whichever tab this session hasn't needed
# yet."
#
# This shared cache sits in front of that: the first request from ANY
# session to need a tab fetches it once and stores it here with a
# timestamp; every other session (and every later message in the same
# session) reuses it until it goes stale. TTL is short enough that a sheet
# edit still shows up within a few minutes, not instantly — reasonable
# for catalog data like prices and schedules that don't change every
# second.
_SHARED_CACHE: dict[str, tuple[float, list]] = {}
_SHARED_CACHE_TTL_SECONDS = 300  # 5 minutes


def _shared_cache_get(tab: str) -> list | None:
    entry = _SHARED_CACHE.get(tab)
    if entry is None:
        return None
    fetched_at, data = entry
    if _time.time() - fetched_at > _SHARED_CACHE_TTL_SECONDS:
        return None  # stale, treat as a miss
    return data


def _shared_cache_set(tab: str, data: list):
    _SHARED_CACHE[tab] = (_time.time(), data)


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

    Checks the per-session cache first (fastest — no lock, no timestamp
    math), then the shared cross-session cache (still fast, avoids the
    network entirely for any tab another session has fetched recently),
    and only hits the real network as a last resort."""
    if _CURRENT_CACHE is not None and tab in _CURRENT_CACHE:
        return _CURRENT_CACHE[tab]

    shared = _shared_cache_get(tab)
    if shared is not None:
        if _CURRENT_CACHE is not None:
            _CURRENT_CACHE[tab] = shared
        return shared

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

    _shared_cache_set(tab, result)
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

def _normalize_location_type(raw: str) -> str:
    """The rest of the codebase expects one of exactly: "home", "clinic",
    "online", "clinic_or_home", "clinic_or_online" — but the real sheet
    has values like "home,clinic" (comma-separated, human-friendly to
    type). Without this, a comma-separated value matches NONE of the
    exact-string checks in ai.py, silently causing location_type to never
    get set at all — which meant a home-visit service could complete an
    entire booking with no address ever collected. This normalizes
    whatever reasonable format ends up in the sheet into the canonical
    token the booking logic actually checks against."""
    if not raw:
        return raw
    parts = {p.strip().lower() for p in raw.replace("/", ",").split(",") if p.strip()}
    if not parts:
        return raw
    has_home = "home" in parts
    has_clinic = "clinic" in parts
    has_online = "online" in parts
    if has_home and has_clinic:
        return "clinic_or_home"
    if has_clinic and has_online:
        return "clinic_or_online"
    if has_home:
        return "home"
    if has_clinic:
        return "clinic"
    if has_online:
        return "online"
    return raw  # unrecognized value — pass through unchanged rather than guess


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
            "location_type": _normalize_location_type(_clean(r.get("location_type"), 30)),
            "visits_required": _clean(r.get("visits_required"), 10),
            "family_name": _clean(r.get("family_name")),
            "variant_label": _clean(r.get("variant_label"), 40),
            # family_name groups tier variants of the same underlying
            # service (e.g. Nanny Training "Half Day" and "Full Day" both
            # share family_name="Nanny Training") so the bot can show ONE
            # line when listing services, then reveal the specific tiers
            # (via variant_label, e.g. "Half Day") only once that family
            # is picked. Leave both blank for a standalone service with
            # no variants — it behaves exactly as before.
            # For multi-visit programs (e.g. "Postnatal Recovery Program —
            # 3 visits") — leave blank in the sheet for an ordinary
            # single-visit service; ai.py treats missing/blank as 1.
            # Renamed from "available" to "active" for consistency with the
            # midwives and packages tables. If your sheet still has an
            # "available" column, either rename it in the sheet OR both
            # keys are checked here for a graceful transition period.
            "active": _truthy(r.get("active", r.get("available", "TRUE"))),
            "demo_notice": _clean(r.get("demo_notice")),
            # BUGFIX: these three sheet columns exist and are filled in
            # (short_desc/long_desc have real content, keywords has real
            # search terms like "low milk supply support", "tongue tie
            # guidance") but were never actually read into this dict at
            # all. short_desc/long_desc weren't causing a visible bug
            # only because every existing caller does
            # `s.get("short_desc") or s.get("description")`, and
            # description happens to duplicate the same text — but
            # keywords had no such fallback and was silently unused
            # everywhere, meaning a descriptive query ("my baby won't
            # latch") never got matched against any service via its
            # keywords at all, regardless of how well the sheet's
            # keywords column was filled in.
            "short_desc": _clean(r.get("short_desc")),
            "long_desc": _clean(r.get("long_desc"), 4000),
            "keywords": _clean(r.get("keywords"), 500),
        }
        for r in rows
        if _clean(r.get("service_name"))
    ]
    services = [s for s in data if s["active"]]
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
# Appointment storage — persisted to SQLite via db.py (see that module for
# schema). Previously this was a plain Python list (`_leads_store`) that
# was wiped on every server restart/redeploy, which also meant the
# availability engine (get_existing_bookings_for, below) could silently
# forget real bookings and double-book a slot after a redeploy. Function
# signatures here are unchanged so ai.py / availability.py needed no edits.
# ---------------------------------------------------------------------------

import db as _db
_db.init_db()


def save_appointment(lead: AppointmentLead):
    lead.created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    _db.create_appointment(lead.model_dump())
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
    return _db.list_appointments()


def get_existing_bookings_for(midwife_id: str, date: str) -> list:
    """Return existing bookings for a given midwife on a given date. Used
    by the availability engine to remove already-booked slots. Now reads
    from SQLite instead of an in-process list, so it stays correct across
    restarts and multiple worker processes."""
    return _db.list_appointments_for(midwife_id, date)


# ===========================================================================
# DEMO FALLBACK DATA
# All marked with [DEMO]. Bot will use this if no sheet is configured.
# These mirror the CSV structure described in the build spec.
# ===========================================================================

_DEMO_SERVICES = [
    {"service_id": "S001", "service_name": "Preconception Consultation",
     "category": "Preconception", "description": "Initial consultation for couples planning pregnancy.",
     "default_price_aed": "400", "duration_minutes": "60",
     "location_type": "clinic_or_online", "active": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S002", "service_name": "My 10 Lunar Month Pregnancy",
     "category": "Pregnancy", "description": "Comprehensive pregnancy tracking and education.",
     "default_price_aed": "800", "duration_minutes": "90",
     "location_type": "clinic_or_home", "active": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S004", "service_name": "Routine Antenatal Visit",
     "category": "Pregnancy", "description": "Standard antenatal check-up.",
     "default_price_aed": "350", "duration_minutes": "45",
     "location_type": "clinic_or_home", "active": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S005", "service_name": "Hypnobirthing Class",
     "category": "Workshop", "description": "Birth preparation using hypnobirthing techniques.",
     "default_price_aed": "500", "duration_minutes": "90",
     "location_type": "clinic", "active": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S013", "service_name": "Routine Postnatal Care",
     "category": "Postnatal", "description": "Standard postnatal home visit.",
     "default_price_aed": "400", "duration_minutes": "60",
     "location_type": "home", "active": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S014", "service_name": "Breastfeeding Support",
     "category": "Postnatal", "description": "One-to-one breastfeeding consultation.",
     "default_price_aed": "350", "duration_minutes": "60",
     "location_type": "home", "active": True,
     "demo_notice": "[DEMO] Replace with real price"},
    {"service_id": "S017", "service_name": "My Baby Care",
     "category": "Newborn", "description": "Newborn care education and hands-on support.",
     "default_price_aed": "400", "duration_minutes": "90",
     "location_type": "home", "active": True,
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