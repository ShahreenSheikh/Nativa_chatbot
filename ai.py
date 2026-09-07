"""
Conversational AI for the NativaCare midwifery chatbot.

Architecture inherited from the Dubai bot, with adaptations:

  - Two LLM calls per turn: `understand()` (NLU only, returns JSON) and
    `compose_reply()` (drafts open Q&A replies with a targeted DB slice).
    Booking-flow turns use deterministic templates and skip the LLM.

  - Six conversation states:
      BROWSING            free Q&A, no booking active
      SERVICE_SELECTING   helping user choose a service
      SLOT_PICKING        showing available slots, waiting for pick
      COLLECTING_DETAILS  slot picked, gathering name/phone/etc.
      AWAITING_CONFIRM    summary shown, awaiting yes/correction
      BOOKED              committed

  - Slot picking is the key difference from Dubai: the bot reads the
    availability engine, presents options to the user, and only accepts
    times that are actually free.

Public API (used by main.py):
    get_ai_response(session_id, user_message, source) -> dict
"""

import os
import re
import asyncio
import json as _json
import time as _time
from datetime import datetime, timedelta
from hashlib import md5
from typing import Optional
from dotenv import load_dotenv
from openai import OpenAI

from logger import save_chat_log
from models import AppointmentLead
from database import (
    get_services as _db_get_services,
    get_midwives as _db_get_midwives,
    get_midwife_services as _db_get_midwife_services,
    get_packages as _db_get_packages,
    get_faqs as _db_get_faqs,
    get_clinic_info as _db_get_clinic_info,
    save_appointment,
    set_session_cache,
)
from availability import (
    get_availability, get_next_available_days,
    format_slots_human, find_slot,
)


# Thin instrumented wrappers around the database getters. These log each
# fetch into the per-turn debug buffer for the side panel. The actual
# caching happens at the _fetch_tab level in database.py — see
# database.set_session_cache(). When the cache is warm, fetches resolve
# in 0ms because they never touch the network.

import time as _t_mod


def _row_count(result) -> int:
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        return 1
    return 0


async def _instrumented(tab_name: str, fn, *args, **kwargs):
    t0 = _t_mod.perf_counter()
    try:
        result = await fn(*args, **kwargs)
        elapsed = int((_t_mod.perf_counter() - t0) * 1000)
        # Heuristic: <5ms means we hit the in-memory cache, not the network
        label = tab_name + (" (cached)" if elapsed < 5 else "")
        _debug_log_fetch(label, _row_count(result), elapsed)
        return result
    except Exception:
        elapsed = int((_t_mod.perf_counter() - t0) * 1000)
        _debug_log_fetch(tab_name + " (error)", 0, elapsed)
        raise


async def get_services(*a, **k):         return await _instrumented("services", _db_get_services, *a, **k)
async def get_midwives(*a, **k):         return await _instrumented("midwives", _db_get_midwives, *a, **k)
async def get_midwife_services(*a, **k): return await _instrumented("midwife_services", _db_get_midwife_services, *a, **k)
async def get_packages(*a, **k):         return await _instrumented("packages", _db_get_packages, *a, **k)
async def get_faqs(*a, **k):             return await _instrumented("faqs", _db_get_faqs, *a, **k)
async def get_clinic_info(*a, **k):      return await _instrumented("clinic_info", _db_get_clinic_info, *a, **k)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_sessions: dict = {}
SESSION_TTL_SECONDS = 60 * 60

# LLM provider: Cerebras Cloud (OpenAI-compatible API).
#
# Matches the working Railway deployment. Cerebras runs gpt-oss-120b at
# ~1,800 tok/s on their wafer-scale hardware. Free tier is 1M tokens/day
# with no credit card; paid tier is prepaid credits.
#
# The .env variable accepts CEREBRAS_API_KEY (preferred) or any of the
# older names for backward compatibility. The base URL and model are
# hardcoded to Cerebras regardless of which env var name you use.
CEREBRAS_API_KEY = (
    os.getenv("CEREBRAS_API_KEY", "")
    or os.getenv("SAMBANOVA_API_KEY", "")
    or os.getenv("GROQ_API_KEY", "")
)
# Kept old alias for back-compat with anywhere the code references it
SAMBANOVA_API_KEY = CEREBRAS_API_KEY
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-oss-120b")
CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")

# Payment: manual bank transfer + staff-approved screenshot, not a real
# payment gateway. When PAYMENT_ENABLED=true, the bot inserts a payment
# step between the confirmation summary and the calendar booking commit:
# it issues an invoice (bank details + reference code, see payments.py)
# and the booking only commits once staff approve a submitted payment
# screenshot from the dashboard. When PAYMENT_ENABLED=false (default),
# the bot behaves exactly like before — booking commits on "yes" without
# any payment step.
PAYMENT_ENABLED = os.getenv("PAYMENT_ENABLED", "false").lower() in (
    "true", "1", "yes", "on"
)

# Kept for backward-compat with any code that still references GROQ_MODEL
GROQ_MODEL = LLM_MODEL

client = (
    OpenAI(
        api_key=CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1",
        # Explicit timeout — without this, the SDK's own default can leave
        # a single request hanging far longer than our own retry logic
        # assumes (call_llm_with_retry is designed around each attempt
        # failing fast, not hanging silently). max_retries=0 because we
        # already have our own retry-with-backoff wrapper; letting the SDK
        # ALSO retry underneath that would multiply worst-case latency.
        timeout=12.0, max_retries=0,
    )
    if CEREBRAS_API_KEY else None
)

# Startup diagnostic — visible in the uvicorn terminal so you can tell at
# a glance whether the LLM is configured. If this prints "NOT set", the
# bot will fall back to regex parsing and won't generate natural replies.
if CEREBRAS_API_KEY:
    masked = CEREBRAS_API_KEY[:8] + "..." + CEREBRAS_API_KEY[-4:] if len(CEREBRAS_API_KEY) > 16 else "***"
    print(f"[LLM startup] Configured: Cerebras / {LLM_MODEL} (key: {masked})")
else:
    print("[LLM startup] CEREBRAS_API_KEY NOT set. "
          "Bot will use regex fallback only — replies will be basic.")

# Payment startup diagnostic
if PAYMENT_ENABLED:
    print("[Payment startup] PAYMENT_ENABLED=true. Bot will issue a "
          "manual bank-transfer invoice and wait for staff approval "
          "before committing bookings.")
else:
    print("[Payment startup] PAYMENT_ENABLED=false. Bookings commit "
          "directly on 'yes' — no payment step.")


# ---------------------------------------------------------------------------
# Debug collector — populated per-turn, returned in the /chat response
# ---------------------------------------------------------------------------
# Module-global, reset at the top of every get_ai_response() call. Simpler
# than threading a debug dict through every function. Not safe for true
# concurrent use across sessions, but FastAPI handles one request per task
# and we only touch this in the awaited path of one request at a time.

_DEBUG_BUFFER: dict = {}


def _debug_reset():
    global _DEBUG_BUFFER
    _DEBUG_BUFFER = {
        "session_id": None,
        "state_before": None,
        "state_after": None,
        "data_fetched": [],   # list of {tab, rows, ms}
        "llm_calls": [],      # list of {step, model, prompt_chars, response_chars,
                              #          prompt, response, ms, error}
        "intent": None,
        "extracted_slots": {},
        "corrections": [],
        "events": [],         # short narrative trace: "applied slot service",
                              # "rejected doctor", etc.
    }


def _debug_log_fetch(tab: str, row_count: int, ms: int):
    _DEBUG_BUFFER.setdefault("data_fetched", []).append({
        "tab": tab, "rows": row_count, "ms": ms,
    })


def _debug_log_llm(step: str, sys_prompt: str, user_messages: list,
                   response_text: str, ms: int, error: Optional[str] = None):
    # Combine the user_messages array into a readable prompt for the panel
    user_block = "\n".join(
        f"[{m.get('role','?')}] {m.get('content','')}"
        for m in user_messages
    )
    _DEBUG_BUFFER.setdefault("llm_calls", []).append({
        "step": step,
        "model": GROQ_MODEL,
        "prompt_chars": len(sys_prompt) + len(user_block),
        "response_chars": len(response_text or ""),
        "system_prompt": sys_prompt,
        "user_prompt": user_block,
        "response": response_text,
        "ms": ms,
        "error": error,
    })


def _debug_event(text: str):
    _DEBUG_BUFFER.setdefault("events", []).append(text)


def _debug_snapshot() -> dict:
    return dict(_DEBUG_BUFFER)

# Abu Dhabi area keywords — used for a soft check on home-visit addresses.
# Not exhaustive; the bot accepts anything but logs a warning if no match.
ABU_DHABI_KEYWORDS = [
    "abu dhabi", "al bateen", "bateen", "saadiyat", "yas",
    "reem", "khalifa city", "mohamed bin zayed", "mbz", "al raha",
    "shahama", "khalidiyah", "khalidiya", "hudayriyat",
    "al maryah", "maryah", "al reem", "al mushrif", "mushrif",
    "al nahyan", "nahyan",
]
# NOTE: "marina" and "corniche" were removed from this list — both are
# common enough elsewhere (Dubai Marina and Sharjah's Corniche are each
# arguably more famous than any Abu Dhabi place using the same word) that
# keeping them here let addresses like "Dubai Marina" pass the check just
# because "marina" appeared in the text. The OTHER_EMIRATES exclusion
# list below is the real fix for this class of problem — it explicitly
# rejects anything naming another emirate, regardless of what area
# keywords also happen to appear in the same address.

OTHER_EMIRATES_KEYWORDS = [
    "dubai", "sharjah", "ajman", "fujairah", "ras al khaimah", "rak",
    "umm al quwain", "al ain",
    # Al Ain is technically part of Abu Dhabi emirate but is a distinct
    # city ~150km from the areas NativaCare's midwives actually cover —
    # treated as out-of-area here since a same-day home visit isn't
    # realistic. Adjust if that's not actually true for your coverage.
]


def _address_names_other_emirate(addr: str) -> bool:
    """True if the address explicitly names a DIFFERENT emirate/city —
    checked BEFORE the positive Abu Dhabi keyword match, so an address
    like "Dubai Marina" can't slip through just because it also contains
    an ambiguous word. This check wins even if an Abu Dhabi keyword is
    ALSO present (e.g. "Reem street, Dubai" is still Dubai)."""
    addr_lc = addr.lower()
    return any(kw in addr_lc for kw in OTHER_EMIRATES_KEYWORDS)


def is_too_generic_address(addr: str) -> bool:
    """Return True if the address is just a city/area name with no building
    or street detail. We can't dispatch a home visit to "abu dhabi" — the
    midwife needs to know WHERE in Abu Dhabi.

    Heuristic: looks generic if it has no digits AND is short (< 4 words),
    AND every word it does have is in our area-keyword list.
    """
    if not addr:
        return True
    cleaned = addr.strip().lower()
    if any(ch.isdigit() for ch in cleaned):
        return False  # has a number — likely a building/street number
    words = [w for w in cleaned.replace(",", " ").split() if w]
    if not words:
        return True
    if len(words) >= 4:
        return False  # 4+ words is probably specific enough
    # If every word is part of a known area keyword (e.g. "abu dhabi",
    # "al bateen"), the address is just the area name — not specific.
    area_words = set()
    for kw in ABU_DHABI_KEYWORDS:
        for w in kw.split():
            area_words.add(w)
    common_words = {"the", "in", "at", "uae", "emirates", "city"}
    if all(w in area_words or w in common_words for w in words):
        return True
    return False

# States
STATE_BROWSING = "browsing"
STATE_SERVICE_SELECTING = "service_selecting"
STATE_CONFIRMING_SERVICE = "confirming_service"
STATE_VARIANT_PICKING = "variant_picking"
# Between BROWSING and CONFIRMING_SERVICE for services that have tier
# variants (e.g. Nanny Training "Half Day"/"Full Day") — the user picked
# the family, now needs to pick which specific tier before we can
# confirm a single bookable service_id.
STATE_LANGUAGE_PICKING = "language_picking"
STATE_SLOT_PICKING = "slot_picking"
STATE_COLLECTING_DETAILS = "collecting_details"
STATE_AWAITING_CONFIRM = "awaiting_confirm"
STATE_AWAITING_PAYMENT = "awaiting_payment"
STATE_BOOKED = "booked"

# Supported session languages. When user is in STATE_LANGUAGE_PICKING,
# they must pick one of these before day/time selection continues.
# The bot then filters slots to only midwives who speak that language.
SUPPORTED_LANGUAGES = ["English", "Arabic", "French", "Urdu"]

# Aliases the user might type for each language. Lowercase; used to
# normalize free-text ("arabic please" → "Arabic") to canonical form.
LANGUAGE_ALIASES = {
    "english": "English", "eng": "English", "en": "English",
    "arabic": "Arabic", "ar": "Arabic", "arb": "Arabic",
    "french": "French", "fr": "French", "français": "French",
    "urdu": "Urdu", "ur": "Urdu", "urdo": "Urdu",
}


def normalize_language(raw: str) -> Optional[str]:
    """Normalize a user's language mention to canonical form.

    Returns "English"/"Arabic"/"French"/"Urdu" if a supported language is
    detected (case-insensitive, alias-aware), else None. Matches any
    supported language mentioned anywhere in the message.
    """
    if not raw:
        return None
    lc = raw.lower().strip()
    # Direct alias match
    if lc in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[lc]
    # Substring match — user might say "in arabic please"
    for alias, canonical in LANGUAGE_ALIASES.items():
        # Word boundary to avoid "french" matching "frenchness" etc.
        if re.search(rf"\b{re.escape(alias)}\b", lc):
            return canonical
    return None


def midwife_speaks(midwife: dict, language: str) -> bool:
    """Check whether a midwife speaks the given canonical language.

    The `languages` field on midwives is a free-text string in the Sheet,
    typically space- or comma-separated (e.g. "English Arabic French").
    Case-insensitive substring match against the language name.
    """
    if not language:
        return True  # no filter → all midwives ok
    langs = (midwife.get("languages") or "").lower()
    return language.lower() in langs

# Detail-collection fields, in the order they're asked
DETAIL_FIELDS = ["patient_name", "phone", "email", "patient_address"]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def detect_language(text: str) -> str:
    """v1: English only. Arabic detection is left here as a placeholder so a
    future translation pass can wire it in."""
    return "en"


def valid(value) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    return bool(s) and s.lower() not in {"none", "null", "tbd", "unknown", "-", "skip"}


def cleanup_old_sessions():
    now = datetime.utcnow()
    for sid in list(_sessions):
        last_seen = _sessions[sid].get("last_seen", now)
        if (now - last_seen).total_seconds() > SESSION_TTL_SECONDS:
            _sessions.pop(sid, None)


def shorten(text: str, max_words: int = 70) -> str:
    words = (text or "").split()
    return text if len(words) <= max_words else " ".join(words[:max_words])


def append_history(session: dict, user_msg: str, assistant_msg: str):
    session["history"].append({"role": "user", "content": user_msg})
    session["history"].append({"role": "assistant", "content": assistant_msg})
    session["history"] = session["history"][-12:]


def record_staff_reply(session_id: str, message: str):
    """Called from main.py when staff send a message from the dashboard
    during human takeover, so the bot's own history stays coherent if/when
    takeover is switched back off and it resumes the conversation."""
    session = _sessions.get(session_id)
    if session is not None:
        session["history"].append({"role": "assistant", "content": message})
        session["history"] = session["history"][-12:]
    save_chat_log(session_id, "[staff]", message)


# ---------------------------------------------------------------------------
# Track services the bot just recommended, so the user can refer to them
# implicitly ("book these", "yes book them", etc.)
# ---------------------------------------------------------------------------

async def _record_recommended_services(session: dict, reply_text: str):
    """Scan the LLM's reply for service-name mentions and store them on
    the session as `last_recommended`. Used by the book-intent handler
    when the user says 'these' / 'them' / 'those' without naming a service.

    Also records mentioned packages under `last_mentioned_packages` so
    that 'book it' after a package discussion is recognized as a
    package-booking attempt (and routed to phone-redirect).

    Checks FAMILY names (e.g. "Antenatal Preparation") as well as
    individual service names (e.g. "Antenatal Preparation & Education")
    — a real bug, not a hypothetical: once general-browsing replies
    started using the short family name instead of a specific variant's
    full name (see the "hide prices/tiers on first mention" change),
    this function never matched anything for a family-grouped service at
    all, since it only ever checked individual real service_name strings
    as substrings. "book it" right after the bot described a family by
    name then had nothing to resume, and fell through to the generic
    full-menu prompt instead — which is exactly what a patient going
    "postnatal recovery" -> "antenatal" -> "book it" would have hit."""
    if not reply_text:
        return
    services = await get_services()
    text_lc = reply_text.lower()
    mentioned = []
    matched_families = set()
    for s in services:
        family = s.get("family_name") or ""
        if family and family.lower() in text_lc and family not in matched_families:
            matched_families.add(family)
            mentioned.append({
                "service_id": s["service_id"],  # a real member of the family —
                "service_name": family,          # good enough to resolve the family later
            })
            continue
        name = s.get("service_name", "")
        if not name or len(name) < 4:
            continue
        if name.lower() in text_lc:
            mentioned.append({
                "service_id": s["service_id"],
                "service_name": name,
            })
    # Cap at 6 — anything more is a "we offer X services" list, not a
    # focused recommendation. Drop entirely so we don't accidentally
    # treat menu dumps as targeted recommendations.
    if 1 <= len(mentioned) <= 6:
        session["last_recommended"] = mentioned
        _debug_event(f"Recorded {len(mentioned)} recommended services from reply")
    else:
        # Clear stale recommendations if this turn didn't focus on a few
        session.pop("last_recommended", None)

    # Track mentioned packages
    try:
        packages = await get_packages()
    except Exception:
        packages = []
    pkg_mentions = []
    for p in packages:
        name = p.get("package_name", "")
        if not name or len(name) < 4:
            continue
        if name.lower() in text_lc:
            pkg_mentions.append(name)
    if 1 <= len(pkg_mentions) <= 3:
        # Few packages mentioned — likely focused discussion. Useful signal.
        session["last_mentioned_packages"] = pkg_mentions
        _debug_event(f"Recorded {len(pkg_mentions)} mentioned packages from reply")
    else:
        # Many packages = a list dump, not a focused mention
        session.pop("last_mentioned_packages", None)


_PRONOUN_BOOK_PATTERNS = [
    "book these", "book those", "book them", "book it",
    "book that one", "book that", "book this", "book this one",
    "book one of those", "book one of these", "book one of them",
    "let's book these", "let's book those", "let's book them",
    "yes book", "ok book", "okay book",
]


def references_recent_recommendations(user_msg: str) -> bool:
    """Does the user's message look like 'book the things you just mentioned'?"""
    if not user_msg:
        return False
    lc = user_msg.lower().strip()
    return any(p in lc for p in _PRONOUN_BOOK_PATTERNS)


# Shared package-reference detection. Used in CONFIRMING_SERVICE and
# AWAITING_CONFIRM to graceful-redirect when the user names a package
# (which isn't bookable through chat) instead of a single service.
_VAGUE_PACKAGE_SIGNALS = [
    "the package", "want the package", "package thing", "package instead",
    "switch to the package", "switch to package", "do the package",
    "starter package", "wellbeing package", "recovery package",
    "the bundle", "want the bundle", "do the bundle",
    "change the deal", "change the service", "different deal",
    "different service", "the other one", "the other thing",
    "other option", "another one",
]


_DENY_WITH_ALTERNATIVE_PREFIXES = [
    "no,", "no ", "not this", "not that", "nope,", "nope ",
    "no thanks,", "no thanks ", "instead", "actually ",
    "rather ", "i'd rather", "id rather",
]


def looks_like_deny_with_alternative(user_msg: str) -> bool:
    """Does the user start with a 'no' AND name an alternative?
    e.g. 'no, the newborn package' or 'no I want hypnobirthing'.
    Lets us distinguish from a clean 'no' (which is pure cancellation)."""
    if not user_msg:
        return False
    lc = user_msg.lower().strip()
    has_no = any(lc.startswith(p) for p in ("no ", "no,", "nope", "not "))
    if not has_no:
        return False
    # Length > 4 words suggests they named an alternative
    words = lc.split()
    return len(words) >= 3


async def references_package(user_msg: str) -> Optional[str]:
    """If the user mentioned a package by name (or used a vague package
    phrasing), return the package name (or 'package' as a generic). Else
    return None."""
    if not user_msg:
        return None
    msg_lc = user_msg.lower()

    # Match against actual package names from the catalog
    try:
        packages = await get_packages()
    except Exception:
        packages = []
    for p in packages:
        name = (p.get("package_name") or "").lower().strip()
        if not name:
            continue
        # Match if all significant words appear in the message (handles
        # "newborn starter" matching "Newborn Starter" exactly, and also
        # partial "starter package" matches via the signal list below).
        if name in msg_lc:
            return p.get("package_name")
        # Try matching distinctive substring (first 2 words)
        first_words = " ".join(name.split()[:2])
        if first_words and first_words in msg_lc:
            return p.get("package_name")

    # Fall back to vague package signals
    if any(s in msg_lc for s in _VAGUE_PACKAGE_SIGNALS):
        return "package"
    return None


# ---------------------------------------------------------------------------
# Category word detection — distinguish "workshop" (category) from a specific
# workshop name like "Hypnobirthing Class"
# ---------------------------------------------------------------------------
# These match the `category` column in the services tab. Singular and plural
# forms; we lowercase the user message and look for these as whole-ish tokens.
_CATEGORY_WORDS = {
    "preconception": "Preconception",
    "pregnancy services": "Pregnancy",
    "pregnancy service": "Pregnancy",
    "antenatal services": "Pregnancy",
    "workshop": "Workshop",
    "workshops": "Workshop",
    "classes": "Workshop",
    "wellbeing": "Wellbeing",
    "well being": "Wellbeing",
    "postnatal services": "Postnatal",
    "postnatal service": "Postnatal",
    "postnatal care": "Postnatal",
    "newborn services": "Newborn",
    "baby services": "Newborn",
    "baby care": "Newborn",
}


def _detect_category_word(message: str) -> Optional[str]:
    """Return the matching category name (e.g. 'Workshop') if the user's
    message looks like a category word rather than a specific service.
    Returns None if no match."""
    if not message:
        return None
    msg_lc = message.lower()
    # Sort by length descending so "pregnancy services" matches before "pregnancy"
    for phrase, category in sorted(_CATEGORY_WORDS.items(), key=lambda x: -len(x[0])):
        if phrase in msg_lc:
            return category
    return None


async def _service_explicit_in_message(message: str) -> bool:
    """Does the message contain an actual specific service name?
    Used to avoid the category-guard firing when the user named a
    specific service (e.g. 'I want the Hypnobirthing workshop').

    We match service names against the message text. A name only counts
    if it's at least two words OR is distinctive enough not to match
    common English (e.g. 'Hypnobirthing' is distinctive; 'Care' is not).
    """
    if not message:
        return False
    msg_lc = message.lower()
    try:
        services = await get_services()
    except Exception:
        return False
    for s in services:
        name = (s.get("service_name") or "").lower().strip()
        if not name:
            continue
        # Multi-word names: full substring match
        if len(name.split()) >= 2 and name in msg_lc:
            return True
        # Distinctive single words (rare in English): match on word boundary
        if name in ("hypnobirthing", "vbac", "preconception"):
            if name in msg_lc:
                return True
    return False


async def _services_in_category(category: str) -> list:
    """All bookable services in a given category."""
    bookable = await bookable_services()
    return [s for s in bookable
            if (s.get("category") or "").strip().lower() == category.lower()]


# ---------------------------------------------------------------------------
# Emergency detection — midwifery-specific cues
# ---------------------------------------------------------------------------
# Two layers:
#  1. The understand prompt may classify intent as "emergency". We trust
#     the LLM for nuanced cases but ALSO apply a negation/future-tense
#     guard below to catch obvious false positives.
#  2. A keyword regex short-circuits before any LLM call for clearly
#     active emergencies (heavy bleeding, water broke, etc.). We keep
#     this list TIGHT — broad terms like "contractions" or "labor" alone
#     are too common in normal midwifery conversation to escalate on.

# Strong emergency keywords — almost always describe an active emergency
# in the present moment. No future-tense escape applies here.
EMERGENCY_KEYWORDS = [
    "heavy bleeding", "bleeding a lot", "bleeding heavily",
    "water broke", "waters broke", "my water just broke",
    "baby not breathing", "baby is not breathing", "baby isn't breathing",
    "not breathing", "isn't breathing",
    "blue lips", "convulsions", "seizure",
    "unconscious", "passed out",
    "can't breathe", "cant breathe", "trouble breathing",
    "severe headache", "blurred vision", "rapid swelling",
    "can't feel baby", "cant feel baby", "no fetal movement",
    "baby isn't moving", "baby is not moving",
]

# Phrases that signal a NON-emergency even if other keywords appear nearby.
# Examples we want to NOT escalate:
#   "I'm about to give birth in a few weeks"   (future)
#   "Not in labor yet"                          (negation context)
#   "I want a hypnobirthing class for labor"   (planning)
# We DON'T list bare "not" as a marker because "the baby is not breathing"
# would be wrongly downgraded.
_NON_EMERGENCY_MARKERS = [
    "not in labor", "not in active",
    "not right now", "not yet", "not having",
    "soon", "next week", "next month", "next year",
    "in a few", "in a couple", "due in", "due date",
    "expecting in", "planning for", "preparing for",
    "want to learn", "want to know", "info about", "information about",
    "class about", "workshop about", "learn about", "tell me about",
    "what is", "what's", "education", "educational",
]


def is_emergency(message: str) -> bool:
    """Short-circuit emergency detector — returns True ONLY for clearly
    active emergencies. False positives here cause the bot to halt and
    refer to 998, so the bar must be high."""
    if not message:
        return False
    m = " " + message.lower() + " "
    if any(marker in m for marker in _NON_EMERGENCY_MARKERS):
        return False
    return any(k in m for k in EMERGENCY_KEYWORDS)


def emergency_reply() -> str:
    return ("This sounds urgent — please call Abu Dhabi emergency services "
            "on 998 or go to the nearest hospital immediately. NativaCare is "
            "not an emergency service.")


# ---------------------------------------------------------------------------
# Human-handoff detection
# ---------------------------------------------------------------------------
# When the user wants to talk to an actual person (because they want
# medical advice, complex case discussion, package booking, etc.), the
# bot should immediately give them the contact info and let them know
# their booking-in-progress is preserved. This fires in any state.
#
# Phrases are deliberately tight to avoid false-positives on legitimate
# booking language. Things like "talk about the workshop" don't trigger.

_HANDOFF_PHRASES = [
    "talk to someone", "talk to a person", "talk to a midwife",
    "talk to a real", "talk to staff", "talk to a human",
    "speak to someone", "speak to a person", "speak to a midwife",
    "speak to staff", "speak to a human", "speak to a real",
    "speak with someone", "speak with a person",
    "connect me", "put me through", "transfer me",
    "call me back", "call me",
    "speak to a doctor", "talk to a doctor",
    "speak to a real person", "talk to a real person",
    "speak to an agent", "talk to an agent",
    "speak to a representative", "talk to a representative",
    "i want a human", "real person please",
    "who can tell", "who can help me",
    "human assistant",
]


def wants_human_handoff(message: str) -> bool:
    """Does the user want to talk to an actual person, not the bot?"""
    if not message:
        return False
    lc = message.lower()
    return any(p in lc for p in _HANDOFF_PHRASES)


def handoff_reply(session: dict) -> str:
    """Build the response for a handoff request. If a booking is in
    progress, mention it briefly so the user knows their state isn't
    lost."""
    base = ("You can reach our team directly at "
            "+971 50 729 7197 or email info@nativacare.com. "
            "Someone will be able to help with anything I can't.")
    lead = session.get("lead") or {}
    candidate = session.get("candidate_service") or {}
    in_progress = (lead.get("service_name") or candidate.get("service_name"))
    if in_progress:
        base += (f"\n\nYour booking-in-progress for "
                 f"{in_progress} is saved — come back any time "
                 f"to finish it.")
    return base


# ---------------------------------------------------------------------------
# LLM Call 1: Understand (JSON intent + slot extraction)
# ---------------------------------------------------------------------------

UNDERSTAND_SYSTEM_PROMPT_TMPL = """You are an NLU module for the NativaCare midwifery chatbot in Abu Dhabi. Read the conversation and the user's latest message, then return STRICT JSON only — no prose, no markdown, no code fences.

Output schema:
{{
  "intent": one of:
    "book"              - user wants to start booking an appointment, OR
                          (CRITICAL) user has an ACTIVE booking in progress
                          (see SESSION STATE below) and is asking about
                          availability, timings, dates, slots — they want
                          to advance the booking. "what days does it
                          occur", "what do you have available", "show me
                          times", "when can I come" — when an active
                          service is set, all of these mean "book".
    "confirm"           - confirming (yes, ok, sure, proceed, that works)
    "deny"              - declining (no, cancel, stop, nevermind)
    "correction"        - user is changing a previously-given detail
    "pick_slot"         - user is picking a time slot from a list shown to them
    "pick_service"      - user is naming a service to book
    "service_list"      - asking what services are offered (ONLY when no
                          active service is set — otherwise see "book")
    "price_question"    - asking about cost / fees
    "midwife_list"      - asking about midwives / who works there
    "midwife_question"  - asking about a specific midwife
    "package_question"  - asking about packages / bundles
    "availability_question" - asking when a SPECIFIC midwife is available
                              (NOT when a service is — that's "book" if
                              the service is active, "service_list" if not)
    "faq"               - asking a general factual question (location, FAQs)
    "emergency"         - user describes an ACTIVE medical emergency
                          happening NOW (heavy bleeding NOW, water broke
                          NOW, baby not breathing NOW). General pregnancy
                          questions, planning ahead, or future events
                          ("about to give birth in 3 weeks", "due soon",
                          "expecting in October") are NOT emergencies —
                          use "general" for those.
    "greeting"          - ONLY the literal openers: hello / hi / hey /
                          salam / good morning. Anything longer or
                          ambiguous ("not right now", "soon", short
                          replies, etc.) is NOT a greeting — use
                          "general" or "answer" instead.
    "thanks"            - thanks / bye
    "answer"            - user is answering a question the bot just asked
    "general"           - other / open-ended question (DEFAULT if unsure)

  "slots": {{           // ONLY for slots explicitly mentioned in THIS message.
                       // Use null otherwise.
    "service_name": null | exact service name from CATALOG below,
    "midwife_name": null | exact midwife first name from ROSTER below,
    "package_name": null | exact package name from PACKAGES below,
    "appointment_date": null | "YYYY-MM-DD",
    "appointment_time": null | "HH:MM" (24h),
    "patient_name": null | the user's name,
    "phone": null | digits with optional + and spaces,
    "email": null | valid email,
    "patient_address": null | free-text address for home visit,
    "location_preference": null | "home" | "clinic" | "online"
  }},

  "corrections": []    // List of slot names the user is EXPLICITLY changing.
}}

SESSION STATE:
{session_state}

CATALOG (services):
{service_catalog}

ROSTER (midwives):
{midwife_roster}

PACKAGES:
{package_list}

Rules:
- Be lenient with typos.
- Today's date is {today}. "tomorrow" = next day. Resolve relative dates to YYYY-MM-DD.
- A user typing just a time like "10am" or "14:30" while we're showing slots is "pick_slot".
- A user typing just a service name with no clear intent is "pick_service" if a service catalog was offered, else "general".
- "yes" / "ok" / "sure" → intent: "confirm". Always.
- CRITICAL: If SESSION STATE shows an active service, questions about WHEN it's available (date/time/slots/scheduling) mean the user wants to advance to slot picking — classify as "book". But questions about WHAT the service is (its content, duration, how many sessions, structure, what it includes, "how does it work", "what's involved") are still "general" or "service_list" — DO NOT classify these as "book". Example: "what days are available?" → book. "how many days is it for?" → general (asking about program length, not scheduling).
- Return JSON only. No commentary."""


async def _build_understand_prompt(session: Optional[dict] = None) -> str:
    services, midwives, packages, clinic = await _gather(
        bookable_services, get_midwives, get_packages, get_clinic_info
    )
    svc_list = "\n".join(f"- {s['service_name']} (id: {s['service_id']})"
                         for s in group_services_by_family(services)[:30])
    mw_list = "\n".join(f"- {m['midwife_name']} (id: {m['midwife_id']})"
                        for m in midwives[:20])
    pkg_list = "\n".join(f"- {p['package_name']} (id: {p['package_id']})"
                         for p in packages[:15]) or "(none)"

    # Build a session-state block telling the LLM what's already booked
    # in the active lead. This is the single most important piece of
    # context for intent classification: "what's available" means very
    # different things depending on whether a service is set.
    state_block = "No active booking. State: BROWSING."
    if session:
        lead = session.get("lead") or {}
        state = session.get("state", "browsing")
        candidate = session.get("candidate_service") or {}
        active_parts = []
        if lead.get("service_name"):
            active_parts.append(f"Active service: {lead['service_name']}")
        if lead.get("midwife_name"):
            active_parts.append(f"Midwife: {lead['midwife_name']}")
        if lead.get("appointment_date"):
            active_parts.append(f"Date: {lead['appointment_date']}")
        if lead.get("appointment_time"):
            active_parts.append(f"Time: {lead['appointment_time']}")

        # CRITICAL: if a candidate service is pending confirmation, the
        # bot is asking yes/no. Surface this so the LLM classifies "yes"
        # as confirm and "no" as deny (instead of general/answer).
        if candidate.get("service_name"):
            state_block = (
                f"State: {state}. AWAITING USER CONFIRMATION for "
                f"\"{candidate['service_name']}\". The bot's previous "
                f"message asked the user to confirm booking this service. "
                f"Treat 'yes' / 'ok' / 'sure' as intent=confirm and "
                f"'no' / 'cancel' as intent=deny."
            )
        elif active_parts:
            state_block = (f"ACTIVE booking in progress. State: {state}.\n"
                           + "\n".join(f"  - {p}" for p in active_parts))
        else:
            state_block = f"No active booking. State: {state}."

    return UNDERSTAND_SYSTEM_PROMPT_TMPL.format(
        session_state=state_block,
        service_catalog=svc_list,
        midwife_roster=mw_list,
        package_list=pkg_list,
        today=datetime.now().strftime("%Y-%m-%d"),
    )


async def _gather(*coros):
    """Tiny helper: call a bunch of async fns in parallel and return their
    awaited results. Avoids importing asyncio.gather everywhere."""
    import asyncio
    return await asyncio.gather(*(c() for c in coros))


async def _llm_understand_call(messages: list, sys_prompt: str,
                               strict: bool = False) -> str:
    extra = ""
    if strict:
        extra = ("\n\nIMPORTANT: Your previous response was not valid JSON. "
                 "Return ONLY a single JSON object. No markdown. Start with "
                 "{ and end with }.")
    full_sys = sys_prompt + extra
    t0 = _time.perf_counter()
    try:
        # CRITICAL: this must be awaited via to_thread, not called directly.
        # call_llm_with_retry() is a synchronous function that makes a
        # blocking HTTP call to Cerebras (plus blocking time.sleep() on
        # retries) — calling it directly inside this async function would
        # freeze Python's single-threaded event loop for however long that
        # takes, stalling EVERY other request the server is handling at
        # that moment (every other conversation, dashboard polling,
        # webhooks, all of it), not just this one. Running it in a worker
        # thread via to_thread lets the event loop keep serving everyone
        # else while this one call is in flight. This was previously
        # causing exactly the "randomly hangs on any message" symptom —
        # any concurrent request could be the one blocking the whole
        # server, so which conversation appeared to "freeze" looked random
        # from the outside even though the actual cause was consistent.
        resp = await asyncio.to_thread(
            call_llm_with_retry,
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": full_sys}] + messages,
            temperature=0.0,
            # max_tokens raised from 400 to 600 — GPT OSS 120B occasionally
            # uses internal-reasoning tokens before producing the JSON
            # output, so a tight cap can leave the response empty.
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        elapsed = int((_time.perf_counter() - t0) * 1000)
        _debug_log_llm(
            step=("understand-retry" if strict else "understand"),
            sys_prompt=full_sys,
            user_messages=messages,
            response_text=text,
            ms=elapsed,
        )
        return text
    except Exception as e:
        elapsed = int((_time.perf_counter() - t0) * 1000)
        _debug_log_llm(
            step=("understand-retry" if strict else "understand"),
            sys_prompt=full_sys,
            user_messages=messages,
            response_text="",
            ms=elapsed,
            error=str(e),
        )
        raise


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return _json.loads(m.group(0))
    except Exception:
        return None


async def understand(session: dict, user_message: str) -> dict:
    """Run the LLM understand call. Returns parsed dict, or a regex fallback
    on failure."""
    if not client:
        return _fallback_understand(session, user_message)

    history = session.get("history", [])[-6:]
    messages = history + [{"role": "user", "content": user_message}]

    sys_prompt = await _build_understand_prompt(session)

    # First attempt
    try:
        raw = await _llm_understand_call(messages, sys_prompt, strict=False)
        parsed = _extract_json(raw)
        if _is_valid_understand_shape(parsed):
            return await _normalize_understand(parsed, user_message)
    except Exception as e:
        print(f"[Understand: attempt 1] {e}")

    # Retry stricter
    try:
        raw = await _llm_understand_call(messages, sys_prompt, strict=True)
        parsed = _extract_json(raw)
        if _is_valid_understand_shape(parsed):
            return await _normalize_understand(parsed, user_message)
    except Exception as e:
        print(f"[Understand: attempt 2] {e}")

    print("[Understand] falling back to regex classifier")
    return _fallback_understand(session, user_message)


def _is_valid_understand_shape(parsed) -> bool:
    """Check the parsed JSON has the basic shape we need.
    Tolerates `slots: null` by treating null as 'no slots extracted' instead
    of failing the validation — avoids burning a retry on a meaningless miss."""
    if not parsed or not isinstance(parsed, dict):
        return False
    if "intent" not in parsed:
        return False
    slots = parsed.get("slots")
    if slots is None:
        parsed["slots"] = {}  # coerce null → {}
        return True
    return isinstance(slots, dict)


VALID_INTENTS = {
    "book", "confirm", "deny", "correction", "pick_slot", "pick_service",
    "service_list", "price_question", "midwife_list", "midwife_question",
    "package_question", "availability_question", "faq", "emergency",
    "greeting", "thanks", "answer", "general",
}


async def _normalize_understand(d: dict, user_message: str) -> dict:
    """Validate slot values against the DB. Drop anything that doesn't match."""
    slots = d.get("slots") or {}
    cleaned = {}

    services, midwives, packages = await _gather(
        get_services, get_midwives, get_packages
    )
    svc_by_name = {s["service_name"].lower(): s for s in services}
    mw_by_name = {m["midwife_name"].lower(): m for m in midwives}
    pkg_by_name = {p["package_name"].lower(): p for p in packages}

    # service_name → resolve to id+name
    svc = slots.get("service_name")
    if isinstance(svc, str) and svc.strip().lower() in svc_by_name:
        match = svc_by_name[svc.strip().lower()]
        cleaned["service_id"] = match["service_id"]
        cleaned["service_name"] = match["service_name"]

    # midwife
    mw = slots.get("midwife_name")
    if isinstance(mw, str) and mw.strip().lower() in mw_by_name:
        match = mw_by_name[mw.strip().lower()]
        cleaned["midwife_id"] = match["midwife_id"]
        cleaned["midwife_name"] = match["midwife_name"]

    # package
    pkg = slots.get("package_name")
    if isinstance(pkg, str) and pkg.strip().lower() in pkg_by_name:
        match = pkg_by_name[pkg.strip().lower()]
        cleaned["package_id"] = match["package_id"]
        cleaned["package_name"] = match["package_name"]

    # Disambiguation guard — real bug report: a user typed exactly
    # "postnatal recovery" (matching the bookable service family shown to
    # them a message earlier) and the LLM extracted package_name
    # "Postnatal Recovery Bundle" instead — a real package that exists in
    # the catalog, but the word "bundle" never appeared anywhere in what
    # the user actually typed. The LLM had both a same-named service
    # family and a similarly-named package in its context and picked the
    # wrong one; _normalize_understand only validated that the proposed
    # package NAME exists somewhere in the catalog, never whether it was
    # actually the better match for the raw text.
    #
    # Fix: if the user's raw message is an exact (or near-exact) match for
    # a bookable service or service FAMILY name, and the LLM also
    # proposed a package that ISN'T that same literal text, prefer the
    # service — a verbatim match to real bookable-service text is a much
    # stronger signal than an LLM's package guess, especially when the
    # package name contains extra words (like "Bundle") the user never
    # typed at all.
    if cleaned.get("package_id"):
        msg_norm = re.sub(r"[^a-z0-9 ]", "", (user_message or "").lower()).strip()
        # Prefer an actually-bookable family member for the fallback —
        # a family can have sheet rows for a variant nobody's staffed to
        # deliver yet, and picking one of those as the "representative"
        # here would just walk the user into a dead end a few turns
        # later instead of into real, bookable options.
        bookable = await bookable_services()
        bookable_ids = {s["service_id"] for s in bookable}
        family_names = {
            (s.get("family_name") or "").lower(): s
            for s in services
            if s.get("family_name") and s["service_id"] in bookable_ids
        }
        exact_service_match = svc_by_name.get(msg_norm)
        if exact_service_match and exact_service_match["service_id"] not in bookable_ids:
            exact_service_match = None  # same reasoning — don't fall back to an unbookable single service either
        exact_family_match = family_names.get(msg_norm)
        if (exact_service_match or exact_family_match) and msg_norm != cleaned["package_name"].lower():
            _debug_event(
                f"Disambiguation: LLM proposed package "
                f"{cleaned['package_name']!r} but raw text {user_message!r} "
                f"exactly matches a bookable service/family instead — "
                f"preferring the service, dropping the package guess."
            )
            cleaned.pop("package_id", None)
            cleaned.pop("package_name", None)
            representative = exact_service_match or exact_family_match
            cleaned["service_id"] = representative["service_id"]
            cleaned["service_name"] = representative["service_name"]

    # date
    date_v = slots.get("appointment_date")
    if isinstance(date_v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_v):
        cleaned["appointment_date"] = date_v

    # time
    time_v = slots.get("appointment_time")
    if isinstance(time_v, str) and re.fullmatch(r"\d{1,2}:\d{2}", time_v):
        h, m = time_v.split(":")
        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
            cleaned["appointment_time"] = f"{int(h):02d}:{m}"

    # name
    name = slots.get("patient_name")
    if isinstance(name, str) and 2 <= len(name.strip()) <= 50:
        cleaned["patient_name"] = name.strip()[:50]

    # phone
    phone = slots.get("phone")
    if isinstance(phone, str) and len(re.sub(r"\D", "", phone)) >= 7:
        cleaned["phone"] = phone.strip()

    # email
    email = slots.get("email")
    if isinstance(email, str) and re.fullmatch(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email):
        cleaned["email"] = email

    # address (light validation only — addresses are messy)
    addr = slots.get("patient_address")
    if isinstance(addr, str) and 5 <= len(addr.strip()) <= 200:
        cleaned["patient_address"] = addr.strip()

    # location preference — only accept if grounded in the user's message.
    # The LLM sometimes infers "clinic" / "home" from the bot's previous
    # turn (e.g. bot said "only available at clinic", user replied "okay" —
    # LLM extracted location_preference: clinic from context). That's a
    # hallucination relative to what the user said and would silently set
    # an incorrect location, so we require the user to have mentioned it.
    loc = slots.get("location_preference")
    if loc in ("home", "clinic", "online"):
        msg_low = (user_message or "").lower()
        location_aliases = {
            "home": ["home", "house", "doorstep", "visit", "at my place",
                     "come to me", "my apartment", "my flat", "address"],
            "clinic": ["clinic", "office", "in person", "come in", "come to you",
                       "your place", "at the clinic"],
            "online": ["online", "virtual", "video", "zoom", "remote",
                       "phone call", "over the phone"],
        }
        aliases = location_aliases.get(loc, [])
        if not user_message or any(a in msg_low for a in aliases):
            cleaned["location_preference"] = loc
        # else: drop silently — the value isn't grounded in what the user said

    # corrections — only keep slots we accepted
    corrections = [
        c for c in (d.get("corrections") or [])
        if isinstance(c, str) and any(c in k for k in cleaned)
    ]

    intent = d.get("intent", "general")
    if intent not in VALID_INTENTS:
        intent = "general"

    return {"intent": intent, "slots": cleaned, "corrections": corrections}


def _fallback_understand(session: dict, user_message: str) -> dict:
    """Regex fallback when LLM is unavailable. Keeps the bot working at a
    degraded level — no slot extraction, just rough intent."""
    if is_emergency(user_message):
        return {"intent": "emergency", "slots": {}, "corrections": []}
    msg = user_message.lower().strip()
    if msg in {"yes", "ok", "okay", "sure", "proceed", "confirm", "yep"}:
        return {"intent": "confirm", "slots": {}, "corrections": []}
    if msg in {"no", "cancel", "stop", "nevermind", "nope"}:
        return {"intent": "deny", "slots": {}, "corrections": []}
    if msg in {"hi", "hello", "hey", "salam"}:
        return {"intent": "greeting", "slots": {}, "corrections": []}
    if any(s in msg for s in ("book", "appointment", "schedule")):
        return {"intent": "book", "slots": {}, "corrections": []}
    if any(s in msg for s in ("service", "what do you offer")):
        return {"intent": "service_list", "slots": {}, "corrections": []}
    if any(s in msg for s in ("price", "cost", "how much", "fee")):
        return {"intent": "price_question", "slots": {}, "corrections": []}
    return {"intent": "general", "slots": {}, "corrections": []}


# ---------------------------------------------------------------------------
# LLM Call 2: Compose (open Q&A with targeted DB slice)
# ---------------------------------------------------------------------------

COMPOSE_SYSTEM_PROMPT_TMPL = """You are a warm, professional midwife assistant for {clinic} in Abu Dhabi.

SAFETY: Never diagnose, prescribe, or replace a midwife or doctor. For emergencies say to call 998 or go to the nearest hospital.

STYLE:
- Warm, supportive, factual. Use ONLY the CONTEXT below — do not invent prices, midwives, or services.
- If a question can't be answered from CONTEXT, say so and offer to help with bookings.

PHRASING:
- When the user asks about timings, availability, schedules, or "when" — say you can show real available slots once they pick a service or class. Do NOT deflect to the phone for booking timings. Example: "I can show available slots once we pick a service — reply 'book Hypnobirthing' (or whichever) and I'll show times."
- When you need to refer the user to phone/email (e.g. service genuinely can't be arranged through chat), give the contact info directly in the reply: "Call +971507297197 or email info@nativacare.com."
- Don't offer to "help contact" — just give the info.
- When you offer to do something (book, show slots, etc.), tell the user the EXACT phrase to reply with. Example: instead of "would you like to proceed?", say "reply 'book it' and I'll show available times." Don't ask vague yes/no questions you can't act on.
- Do NOT offer to "show you available slots" or "show times" as a follow-up. If a service is active, suggest the user reply "book it" — the booking flow will show the slots, not you.
- CRITICAL — PACKAGES: Packages (Pregnancy Foundation, Full Caseload Care, Postnatal Recovery Bundle, Newborn Starter, Mummy and Baby Wellbeing, or anything in the PACKAGES section of the catalog) are NOT bookable through chat. NEVER tell the user to "reply 'book [package name]'" or say a package "is available" to book. When discussing a package, ALWAYS include the phone number: "To book the Newborn Starter, call +971507297197 or email info@nativacare.com." If the user wants a single service instead, suggest one from the bookable services list.
- When answering a "what services do you offer" style question, after listing the bookable services, add one short closing sentence mentioning that packages (bundles) are also available and that the user can ask about them — don't list the packages themselves unless asked specifically about packages.

LENGTH AND FORMAT:
- For LISTING questions ("what services do you offer", "who are your midwives", "what packages do you have"): list the items. Up to 140 words. Use a brief intro sentence followed by a bulleted list. Don't say "various" or "many" — list them.
- For OTHER questions: 3 short sentences max, ~70 words.

CONTEXT:
{context}"""


async def _build_compose_context(intent: str, slots: dict,
                                 session: Optional[dict] = None) -> str:
    """Return a tight DB slice for the open-Q&A LLM call."""
    parts = []
    clinic = await get_clinic_info()
    parts.append(f"Clinic: {clinic.get('clinic_name', CLINIC_NAME)}")
    parts.append(f"Address: {clinic.get('address', '')}")
    parts.append(f"Phone: {clinic.get('phone', '')}")
    if clinic.get("demo_mode", "").upper() == "TRUE":
        parts.append("(Note: this deployment uses demo data; verify with the clinic.)")

    # If a booking is in progress, surface the active service details FIRST.
    # The LLM was answering "how much would it cost?" with "I don't have
    # enough information" because the active lead wasn't in the context.
    lead = (session or {}).get("lead") or {}
    if lead.get("service_id"):
        services = await get_services()
        active = next(
            (s for s in services if s["service_id"] == lead["service_id"]),
            None,
        )
        if active:
            price = lead.get("price_aed") or active.get("default_price_aed") or "?"
            parts.append(
                f"\nACTIVE booking (the user is mid-booking THIS):\n"
                f"- Service: {active['service_name']}\n"
                f"- Price: AED {price}\n"
                f"- Duration: {active.get('duration_minutes', '?')} min\n"
                f"- Location: {active.get('location_type', '?')}\n"
                f"- Date: {lead.get('appointment_date', 'not picked yet')}\n"
                f"- Time: {lead.get('appointment_time', 'not picked yet')}\n"
                f"- Midwife: {lead.get('midwife_name', 'will be assigned')}"
            )

    need_services = intent in ("service_list", "price_question", "general", "faq")
    need_midwives = intent in ("midwife_list", "midwife_question", "general")
    need_packages = intent in ("package_question", "price_question", "general", "service_list")
    need_faqs = intent in ("faq", "general")
    # Only show actual prices when the user specifically asked about
    # price — for a general "what services do you offer" browse, prices
    # (and any hint of tiers/variants existing) are deliberately withheld
    # entirely so there's nothing for the model to leak. This was a real
    # bug: even with an instruction saying "don't mention the other tiers'
    # prices", the model still had a representative price sitting in its
    # context and used it anyway. Not giving it the data at all is a much
    # more reliable fix than asking it not to use data it can see.
    show_prices_in_list = intent == "price_question"

    # Always include the bookable-service list, even on intents that don't
    # need details — this way the LLM knows what we CAN'T offer if asked.
    bookable = await bookable_services()
    bookable_names = {s["service_name"] for s in bookable}
    parts.append("\nBookable services (only these can be booked):")
    if need_services and show_prices_in_list:
        # Group tier variants (e.g. Nanny Training "Half Day"/"Full Day")
        # under one line so the initial list doesn't overwhelm the user
        # with every combination up front — the specific tiers get shown
        # once they actually pick that family (see the variant-picking
        # step in the main state machine below).
        for s in group_services_by_family(bookable):
            if s.get("_variant_count", 1) > 1:
                parts.append(
                    f"- {s['service_name']} ({s.get('category', '')}): "
                    f"from AED {s.get('default_price_aed', '?')} "
                    f"({s['_variant_count']} options available — mention "
                    f"there are multiple options, don't list exact tiers "
                    f"or prices for the other options), "
                    f"location: {s.get('location_type', '')}"
                )
            else:
                parts.append(
                    f"- {s['service_name']} ({s.get('category', '')}): "
                    f"AED {s.get('default_price_aed', '?')}, "
                    f"{s.get('duration_minutes', '?')} min, "
                    f"location: {s.get('location_type', '')}"
                )
    elif need_services:
        # General browsing (service_list / general / faq intents, NOT a
        # specific price question) — name and a one-line description only.
        # No price, no duration, no mention that tiers/variants exist at
        # all. Full details (and, for a grouped family, the specific
        # tier choices) only appear once the user actually picks a
        # service — see render_service_confirmation / render_variant_choice.
        for s in group_services_by_family(bookable):
            desc = (s.get("short_desc") or s.get("description") or "").strip()
            if desc:
                first_sentence = desc.split(". ")[0].rstrip(".") + "."
                if len(first_sentence) > 130:
                    first_sentence = first_sentence[:127].rsplit(" ", 1)[0] + "..."
                parts.append(f"- {s['service_name']}: {first_sentence}")
            else:
                parts.append(f"- {s['service_name']}")
    else:
        # Compact list, just names — so the LLM can recognize what's offered
        # without bloating the prompt.
        for s in group_services_by_family(bookable):
            parts.append(f"- {s['service_name']}")

    # Tell the LLM about advertised-but-unbookable services explicitly, so
    # it doesn't keep talking about them in compose replies after they
    # came up in earlier turns.
    all_services = await get_services()
    unbookable_names = [
        s["service_name"] for s in all_services
        if s["service_name"] not in bookable_names
    ]
    if unbookable_names:
        parts.append(
            "\nServices we OFFER but don't have a midwife scheduled for "
            "right now (mention by name if asked 'what else do you offer'; "
            "say something like '... — for those, call +971507297197 to "
            "arrange'): "
            + ", ".join(unbookable_names[:15])
        )

    if need_midwives:
        midwives = await get_midwives()
        parts.append("\nMidwives:")
        for m in midwives[:10]:
            parts.append(
                f"- {m['midwife_name']}: {m.get('qualifications', '')}, "
                f"{m.get('years_experience', '?')} yrs, "
                f"languages: {m.get('languages', '')}"
            )

    if need_packages:
        packages = await get_packages()
        if packages:
            parts.append("\nPackages:")
            for p in packages[:10]:
                parts.append(
                    f"- {p['package_name']}: AED {p.get('total_price_aed', '?')}, "
                    f"{p.get('description', '')[:120]}"
                )

    if need_faqs:
        faqs = await get_faqs()
        if faqs:
            parts.append("\nFAQs:")
            for f in faqs[:10]:
                parts.append(f"- Q: {f['question']}\n  A: {f['answer'][:300]}")

    return "\n".join(parts)


async def compose_reply(session: dict, user_message: str,
                        understanding: dict) -> str:
    """LLM call 2 — friendly reply using targeted DB slice."""
    if not client:
        return ("I can help with services, prices, midwives, packages, "
                "or bookings. What would you like to know?")
    t0 = _time.perf_counter()
    sys_prompt = ""
    messages = []
    try:
        context = await _build_compose_context(
            understanding["intent"], understanding["slots"], session
        )
        sys_prompt = COMPOSE_SYSTEM_PROMPT_TMPL.format(
            clinic=CLINIC_NAME, context=context,
        )
        history = session.get("history", [])[-6:]
        # Build the user-side messages separately so we can log them cleanly
        user_messages = history + [{"role": "user", "content": user_message}]
        messages = [{"role": "system", "content": sys_prompt}] + user_messages
        # Same fix as _llm_understand_call above — must run in a worker
        # thread, not block the event loop directly.
        resp = await asyncio.to_thread(
            call_llm_with_retry,
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.3,
            # max_tokens raised from 320 to 600 because GPT OSS 120B sometimes
            # uses tokens for internal scaffolding before user-visible output.
            # 320 was producing truncated lists ("Vaginal Birth After Cesarean
            # Workshop - 120 min (clinic)" with no price) and occasionally
            # empty replies entirely when the model hit the limit during
            # reasoning. 600 is still bounded but gives more headroom.
            max_tokens=600,
        )
        raw_content = (resp.choices[0].message.content or "").strip()
        # GPT OSS 120B occasionally returns empty content (especially under
        # load or when max_tokens cuts off mid-reasoning). Treat that as an
        # error so the user gets the graceful "try again" fallback instead
        # of a silent empty reply that the UI renders as "(no reply)".
        if not raw_content:
            finish_reason = ""
            try:
                finish_reason = resp.choices[0].finish_reason or ""
            except (AttributeError, IndexError):
                pass
            raise RuntimeError(
                f"LLM returned empty content (finish_reason={finish_reason!r}). "
                f"Likely max_tokens was hit during model reasoning."
            )
        text = shorten(raw_content, max_words=180)
        elapsed = int((_time.perf_counter() - t0) * 1000)
        _debug_log_llm(
            step="compose",
            sys_prompt=sys_prompt,
            user_messages=user_messages,
            response_text=text,
            ms=elapsed,
        )
        # After the reply is generated, scan for any service names it
        # mentioned. Store them on the session so that if the user replies
        # "book those" or "book these classes", we know what they meant
        # without forcing them to repeat the names. See find_recommended_in_reply.
        await _record_recommended_services(session, text)
        return text
    except Exception as e:
        elapsed = int((_time.perf_counter() - t0) * 1000)
        _debug_log_llm(
            step="compose",
            sys_prompt=sys_prompt,
            user_messages=messages,
            response_text="",
            ms=elapsed,
            error=str(e),
        )
        print(f"[Compose error] {e}")
        # Honest error handling: tell the user what happened, give them
        # contact info, and note that any booking-in-progress is preserved.
        # State is NOT cleared — the user can retry in a moment or come
        # back later and pick up where they left off.
        return llm_error_reply(session, e)


def is_rate_limit_error(err: Exception) -> bool:
    """Detect the Groq rate-limit (429) error specifically, so we can give
    a clearer message ('try in a minute') instead of a generic 'try again'."""
    err_str = str(err).lower()
    return (
        "429" in err_str
        or "rate limit" in err_str
        or "rate_limit_exceeded" in err_str
        or "tokens per day" in err_str
        or "quota" in err_str
    )


def is_transient_error(err: Exception) -> bool:
    """Detect errors that are worth retrying because they typically clear
    in seconds. Cerebras 'queue_exceeded' is the main one — it means the
    provider's shared queue is briefly full, not that we've hit a quota.

    Returns True only for errors that might genuinely succeed on a retry.
    Returns False for hard errors (auth, daily quotas) where retrying
    just wastes time."""
    err_str = str(err).lower()
    # Cerebras-specific transient: shared queue is full right now
    if "queue_exceeded" in err_str or "queue exceeded" in err_str:
        return True
    if "too_many_requests" in err_str and "queue" in err_str:
        return True
    # Generic transient: high traffic / overload / temporary
    if "high traffic" in err_str or "try again soon" in err_str:
        return True
    if "service unavailable" in err_str or "503" in err_str:
        return True
    if "502" in err_str or "504" in err_str or "gateway" in err_str:
        return True
    # Network-level transient
    if "timeout" in err_str or "timed out" in err_str:
        return True
    if "connection" in err_str and ("reset" in err_str or "refused" in err_str):
        return True
    # Daily quota / rate-limit per-day are NOT transient — they don't
    # clear in seconds. Hard 429s on quota fall through to False.
    if "tokens per day" in err_str or "tpd" in err_str:
        return False
    if "tokens per hour" in err_str or "tph" in err_str:
        return False
    return False


def call_llm_with_retry(*, model: str, messages: list,
                        temperature: float, max_tokens: int,
                        response_format: dict = None,
                        max_attempts: int = 3):
    """Call client.chat.completions.create with auto-retry on transient
    errors (Cerebras queue_exceeded, 503, gateway, network timeouts).

    Retries are quick (1s, then 2s) to keep total latency under ~4s in
    the worst case. Hard errors (auth, quota exhausted, malformed
    request) are re-raised immediately — no point retrying them."""
    import time as _t
    backoffs = [1.0, 2.0]  # seconds between attempts after first failure
    last_err = None
    for attempt in range(max_attempts):
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            last_err = e
            if attempt >= max_attempts - 1:
                # Exhausted retries — surface the error to caller
                raise
            if not is_transient_error(e):
                # Permanent error — don't waste time retrying
                raise
            wait = backoffs[min(attempt, len(backoffs) - 1)]
            _debug_event(f"LLM transient error (attempt {attempt + 1}/{max_attempts}): {str(e)[:120]} — retrying in {wait}s")
            _t.sleep(wait)
    # Defensive: shouldn't reach here, but just in case
    if last_err:
        raise last_err
    return None


def llm_error_reply(session: dict, err: Exception) -> str:
    """Build an honest, useful reply when an LLM call fails.

    Always includes contact info. If a booking is in progress, mentions
    it so the user knows their state isn't lost. State is NOT cleared
    by this function — the caller should leave it alone too.
    """
    is_rate = is_rate_limit_error(err)
    if is_rate:
        opening = ("I'm temporarily at capacity and can't process that "
                   "right now. Please try again in a few minutes, or "
                   "reach our team directly at +971 50 729 7197 or "
                   "info@nativacare.com.")
    else:
        opening = ("Something went wrong on my end. Please try again, "
                   "or reach our team at +971 50 729 7197 or "
                   "info@nativacare.com.")

    lead = session.get("lead") or {}
    candidate = session.get("candidate_service") or {}
    in_progress = lead.get("service_name") or candidate.get("service_name")
    if in_progress:
        opening += (f"\n\nYour booking-in-progress for "
                    f"{in_progress} is saved — come back any time "
                    f"to continue.")
    return opening


# ---------------------------------------------------------------------------
# Booking-flow templates (deterministic, no LLM)
# ---------------------------------------------------------------------------

async def render_variant_choice(family_name: str, variants: list) -> str:
    """Shown when a picked service turns out to have multiple tiers (half
    day/full day, single visit/3-visit program, etc.) — lists the actual
    options with their real prices/durations AND a short description so
    the user can actually tell what they're choosing between, not just
    see a bare price/duration line, before moving into the normal
    single-service confirmation flow.

    Uses the full service_name (e.g. "Nanny Training (Half Day)") as the
    display label rather than the bare variant_label ("Half Day") — the
    bare label reads as ambiguous on its own once it's separated from the
    "Nanny Training has a few options:" header above it. variant_label
    is still what the matching logic in the state handler keys off of;
    this only changes what's SHOWN."""
    lines = [f"{family_name} has a few options:\n"]
    for v in variants:
        label = v.get("service_name") or v.get("variant_label", "")
        price = v.get("default_price_aed", "?")
        duration = v.get("duration_minutes", "")
        visits = v.get("visits_required", "")
        extra = f", {visits} visits" if visits and str(visits) not in ("", "1") else ""
        duration_str = f", {duration} min" if duration else ""
        lines.append(f"• {label} — AED {price}{duration_str}{extra}")

        desc = (v.get("short_desc") or v.get("description") or "").strip()
        if desc:
            # Keep it to roughly one sentence — this is a quick comparison
            # view, not the full service description (that's already
            # shown later, in render_service_confirmation, once a
            # specific variant is picked).
            first_sentence = desc.split(". ")[0].rstrip(".") + "."
            if len(first_sentence) > 140:
                first_sentence = first_sentence[:137].rsplit(" ", 1)[0] + "..."
            lines.append(f"  {first_sentence}")
        lines.append("")  # blank line between options for readability

    lines.append("Which would you like?")
    return "\n".join(lines)


async def bookable_services() -> list:
    """Return only services that have at least one active midwife mapped to
    them in `midwife_services`. Prevents the bot from advertising services
    nobody can actually book."""
    services, links, midwives = await _gather(
        get_services, get_midwife_services, get_midwives,
    )
    active_midwife_ids = {m["midwife_id"] for m in midwives if m.get("active", True)}
    bookable_service_ids = {
        link["service_id"] for link in links
        if link.get("midwife_id") in active_midwife_ids
    }
    return [s for s in services if s["service_id"] in bookable_service_ids]


def group_services_by_family(services: list) -> list:
    """Collapse tier variants of the same underlying service (e.g. Nanny
    Training "Half Day" AED 2900 / "Full Day" AED 4600, both sharing
    family_name="Nanny Training") into ONE representative entry — used
    whenever we're showing the top-level service list, so a user isn't
    shown 11 near-duplicate line items when there are really 4 distinct
    things to choose from. Standalone services (blank family_name) pass
    through unchanged, one entry each.

    The representative entry uses the LOWEST price among the family's
    variants (a natural "starting from" price) and gets an extra
    "_variant_count" key so callers can tell it's a group. The original
    per-variant rows are never mutated — resolving a family back to its
    specific variants happens separately, when the user actually picks
    that family (see the variant-picking step in the main state machine).
    """
    families: dict = {}
    standalone: list = []
    order: list = []

    for s in services:
        family = s.get("family_name") or ""
        if not family:
            standalone.append(s)
            continue
        if family not in families:
            families[family] = []
            order.append(family)
        families[family].append(s)

    grouped = []
    for family in order:
        variants = families[family]
        if len(variants) == 1:
            grouped.append(variants[0])
            continue
        cheapest = min(
            variants,
            key=lambda v: float(v.get("default_price_aed") or "inf")
            if str(v.get("default_price_aed") or "").replace(".", "", 1).isdigit()
            else float("inf"),
        )
        representative = dict(cheapest)
        representative["service_name"] = family
        representative["_variant_count"] = len(variants)
        representative["_variant_service_ids"] = [v["service_id"] for v in variants]
        grouped.append(representative)

    return standalone + grouped


def resolve_family_variants(services: list, family_name: str) -> list:
    """The reverse of the grouping above — given a family name, return
    every service row that belongs to it (for showing the actual tier
    choices once a family has been picked)."""
    return [s for s in services if (s.get("family_name") or "") == family_name]


_ORDINAL_WORDS = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
}
# Deliberately NOT including bare cardinal words ("one", "two", "three"...)
# — "one" in particular is a common English filler/pronoun ("the third
# ONE", "which ONE", "that ONE") far more often than it's actually used
# as a number. Including it caused "the third one" to match position 1
# (from "one") before ever reaching "third", since dict iteration found
# "one" first. A bare digit ("1", "2", "3"...) is still handled
# separately below and covers the same real use case without the
# ambiguity.


def match_variant(user_message: str, variants: list) -> Optional[dict]:
    """Resolve a user's free-text reply to one specific variant, given the
    options shown by render_variant_choice(). Rewritten after a real bug
    report: a message like "4 sessions" failed to match a label like
    "Signature Course (4 sessions)" two different ways at once —
    (1) the old exact-match check only tested whether the FULL LABEL was
    contained in the message, never the reverse (a short reply is often a
    fragment OF the longer label, not the other way around), and
    (2) the old word-fallback used bare substring checks, so the word
    "session" (from "Single Session") false-matched inside "sessions"
    (from the message) — a plural containing a singular as a substring is
    not the same word. This version fixes both, adds digits as matchable
    tokens (so "4" itself can disambiguate), and adds explicit
    ordinal/positional matching ("the third one", "option 2", a bare "3")
    as a final fallback rather than depending on the LLM to have already
    resolved that upstream.
    """
    if not variants:
        return None
    msg_lower = (user_message or "").lower().strip()
    if not msg_lower:
        return None

    labels = [(v, (v.get("variant_label") or v.get("service_name") or "").lower()) for v in variants]

    # 1. Bidirectional exact-label substring match — catches both "the
    #    full label was typed" and "a fragment of the label was typed".
    for v, label in labels:
        if label and (label in msg_lower or msg_lower in label):
            return v

    # 2. Word-boundary distinctive-word match. \b...\b prevents "session"
    #    (from "Single Session") from false-matching inside "sessions"
    #    (from the message) — regex now includes digits too, so a bare
    #    "4" can be the deciding token when that's genuinely what's
    #    distinctive between two options. Single-digit tokens ("4", "5")
    #    are explicitly allowed through the length filter even though
    #    it's normally >1 — for a family like "3-Visit Program" vs
    #    "5-Visit Program", the NUMBER is usually the actual distinguishing
    #    signal (not "visit" vs "visits", which is the same word in both
    #    labels and shouldn't be relied on to disambiguate at all).
    word_counts: dict = {}
    for _, label in labels:
        for w in set(re.findall(r"[a-z0-9]+", label)):
            if len(w) > 1 or w.isdigit():
                word_counts[w] = word_counts.get(w, 0) + 1
    distinctive_words = {w for w, count in word_counts.items() if count == 1}
    for v, label in labels:
        label_words = {w for w in re.findall(r"[a-z0-9]+", label) if len(w) > 1 or w.isdigit()}
        for w in label_words:
            if w in distinctive_words and re.search(r"\b" + re.escape(w) + r"\b", msg_lower):
                return v

    # 3. Ordinal / positional fallback — "the third one", "option 2",
    #    "number 1", or a bare digit like "3". Position is 1-indexed in
    #    what the user typed, matching the order variants were listed in
    #    render_variant_choice (same order as `variants` here).
    stripped = msg_lower.strip(" .!")
    if stripped.isdigit():
        idx = int(stripped) - 1
        if 0 <= idx < len(variants):
            return variants[idx]

    for word, idx in _ORDINAL_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", msg_lower) and idx < len(variants):
            return variants[idx]

    option_match = re.search(r"\b(?:option|number|choice)\s*#?\s*(\d+)\b", msg_lower)
    if option_match:
        idx = int(option_match.group(1)) - 1
        if 0 <= idx < len(variants):
            return variants[idx]

    return None


async def render_service_menu() -> str:
    services = await bookable_services()
    if not services:
        return ("I'm having trouble loading the service list. Please call us "
                "at +971 50 729 7197.")
    lines = ["Which service would you like to book?"]
    # Group by category for readability, and collapse tier variants
    # (half day/full day, single-visit/3-visit/5-visit, etc.) into one
    # line per family so this menu doesn't list near-duplicates. Each
    # line also gets a short description now — price/duration/tier
    # choices only appear once a specific service is actually picked
    # (render_service_confirmation for a standalone service, or
    # render_variant_choice for a family), matching the same
    # "name + description first, details after" pattern used in the
    # general-browsing compose reply and the variant picker.
    display_services = group_services_by_family(services)
    by_cat: dict = {}
    for s in display_services:
        by_cat.setdefault(s.get("category", "Other"), []).append(s)
    # Only show category headers when there's genuinely more than one —
    # with no "category" column filled in on the sheet, everything falls
    # under the generic "Other" default, and a single "Other:" header
    # just adds noise without grouping anything meaningfully.
    show_headers = len(by_cat) > 1
    for cat in by_cat:
        if show_headers:
            lines.append(f"\n{cat}:")
        for s in by_cat[cat][:8]:
            suffix = " (multiple options)" if s.get("_variant_count", 1) > 1 else ""
            lines.append(f"  • {s['service_name']}{suffix}")
            desc = (s.get("short_desc") or s.get("description") or "").strip()
            if desc:
                first_sentence = desc.split(". ")[0].rstrip(".") + "."
                if len(first_sentence) > 130:
                    first_sentence = first_sentence[:127].rsplit(" ", 1)[0] + "..."
                lines.append(f"    {first_sentence}")
    return "\n".join(lines)


async def is_service_bookable(service_id: str) -> bool:
    """Check whether at least one active midwife is mapped to this service."""
    links, midwives = await _gather(get_midwife_services, get_midwives)
    active_ids = {m["midwife_id"] for m in midwives if m.get("active", True)}
    return any(
        l.get("service_id") == service_id and l.get("midwife_id") in active_ids
        for l in links
    )


async def validate_midwife_for_booking(midwife_name: str, service_id: str,
                                       date_str: str, time_str: str) -> dict:
    """Check if a midwife (by name) is a valid choice for the given
    service, date, and time. Returns a dict with:
      - ok: bool — overall yes/no
      - midwife_id: str — resolved ID if the name matched (else "")
      - midwife_name: str — canonical name from the roster
      - reason: str — if not ok, why ("unknown_midwife" |
        "service_mismatch" | "no_slot_at_time")
      - alternatives: list — context-dependent suggestions
        (other midwives for that service; or other times this midwife
        has available)

    This is used when the user requests a midwife change at the booking
    summary, OR when re-validating before a final commit.
    """
    result = {
        "ok": False, "midwife_id": "", "midwife_name": "",
        "reason": "", "alternatives": [],
    }
    if not midwife_name:
        result["reason"] = "no_name"
        return result

    midwives, links = await _gather(get_midwives, get_midwife_services)

    # Resolve name to canonical roster entry (case-insensitive)
    target_lc = midwife_name.strip().lower()
    matched = next(
        (m for m in midwives
         if (m.get("midwife_name") or "").lower() == target_lc
         and m.get("active", True)),
        None,
    )
    if not matched:
        result["reason"] = "unknown_midwife"
        # Alternatives: list all active midwives for this service
        active_ids = {m["midwife_id"] for m in midwives if m.get("active", True)}
        svc_midwife_ids = {l["midwife_id"] for l in links
                           if l.get("service_id") == service_id
                           and l.get("midwife_id") in active_ids}
        result["alternatives"] = [
            m["midwife_name"] for m in midwives
            if m["midwife_id"] in svc_midwife_ids
        ]
        return result

    result["midwife_id"] = matched["midwife_id"]
    result["midwife_name"] = matched["midwife_name"]

    # Check: does this midwife offer this service?
    offers_service = any(
        l.get("service_id") == service_id
        and l.get("midwife_id") == matched["midwife_id"]
        for l in links
    )
    if not offers_service:
        result["reason"] = "service_mismatch"
        # Alternatives: midwives who DO offer this service
        active_ids = {m["midwife_id"] for m in midwives if m.get("active", True)}
        svc_midwife_ids = {l["midwife_id"] for l in links
                           if l.get("service_id") == service_id
                           and l.get("midwife_id") in active_ids}
        result["alternatives"] = [
            m["midwife_name"] for m in midwives
            if m["midwife_id"] in svc_midwife_ids
        ]
        return result

    # Check: is this midwife available at the requested date+time?
    if date_str and time_str:
        try:
            day = await get_availability(service_id, date_str)
        except Exception:
            day = None
        if day and day.slots:
            slot_match = next(
                (s for s in day.slots
                 if s.start_time == time_str
                 and matched["midwife_name"] in (s.midwife_name or "")),
                None,
            )
            if not slot_match:
                result["reason"] = "no_slot_at_time"
                # Alternatives: times this midwife IS available on this day
                this_midwife_slots = [
                    s.start_time for s in day.slots
                    if matched["midwife_name"] in (s.midwife_name or "")
                ]
                result["alternatives"] = this_midwife_slots[:6]
                return result

    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# Service-confirmation gate
# ---------------------------------------------------------------------------
# The architectural safety net: before we commit a service to lead.service_id,
# the user must explicitly confirm it. Until then, the service lives in
# session["candidate_service"] as {service_id, service_name}.
#
# This is the single most important guard against wrong-service bookings.
# Every path that previously went straight to slot picking now routes
# through STATE_CONFIRMING_SERVICE, which asks "is this right?"

async def render_service_confirmation(service_id: str,
                                      service_name: str) -> str:
    """Build the 'is this the service you want?' prompt."""
    services = await get_services()
    svc = next((s for s in services if s["service_id"] == service_id), None)
    if not svc:
        return f"Just to confirm — you'd like to book {service_name}? Reply 'yes' to continue or tell me what you'd prefer instead."

    price = svc.get("default_price_aed", "?")
    duration = svc.get("duration_minutes", "?")
    loc_type = svc.get("location_type", "")
    loc_phrase = {
        "clinic": "at the clinic",
        "home": "as a home visit",
        "online": "online",
        "clinic_or_home": "at the clinic or as a home visit",
        "clinic_or_online": "at the clinic or online",
    }.get(loc_type, "")
    parts = [f"Just to confirm — you'd like to book the **{service_name}**"]
    detail_bits = []
    if price != "?":
        detail_bits.append(f"AED {price}")
    if duration != "?":
        detail_bits.append(f"{duration} min")
    if loc_phrase:
        detail_bits.append(loc_phrase)
    if detail_bits:
        parts.append(f"({', '.join(detail_bits)})")
    parts.append("?")
    description = (svc.get("description") or "").strip()
    desc_line = f"\n\n{description}" if description else ""
    return (
        " ".join(parts)
        + desc_line
        + "\n\nReply 'yes' to continue, or tell me which service you'd prefer instead."
    )


async def enter_service_confirmation(session: dict, service_id: str,
                                      service_name: str) -> str:
    """Move the session into CONFIRMING_SERVICE state with the given
    candidate. The service is NOT yet committed to lead — that happens
    only after the user says yes.

    Family-aware: if service_id belongs to a family with multiple
    bookable tiers, routes into VARIANT_PICKING instead of jumping
    straight to a single-service confirmation. This matters because this
    function has several callers beyond the main staging block (e.g. the
    "book it" resumption path after a general/FAQ answer, and "book
    <name>" direct extraction) — without this check here too, those
    paths would silently confirm whichever ONE family member happened to
    be on hand instead of asking which tier the user actually wants."""
    # Defensive: don't enter confirmation for an unbookable service.
    if not await is_service_bookable(service_id):
        return await render_unbookable_service_message(service_id, service_name)

    bookable = await bookable_services()
    matched = next((s for s in bookable if s["service_id"] == service_id), None)
    family_name = (matched or {}).get("family_name") or ""
    if family_name:
        variants = resolve_family_variants(bookable, family_name)
        if len(variants) > 1:
            session["variant_family"] = family_name
            session["state"] = STATE_VARIANT_PICKING
            _debug_event(
                f"enter_service_confirmation: {service_name} belongs to "
                f"family '{family_name}' with {len(variants)} variants — "
                f"routing to VARIANT_PICKING instead of a direct confirm."
            )
            return await render_variant_choice(family_name, variants)

    session["candidate_service"] = {
        "service_id": service_id,
        "service_name": service_name,
    }
    session["state"] = STATE_CONFIRMING_SERVICE
    _debug_event(f"Entered CONFIRMING_SERVICE for {service_name} ({service_id})")
    return await render_service_confirmation(service_id, service_name)


def commit_candidate_service(session: dict):
    """Promote candidate_service into lead.service_id. Called after the
    user confirms."""
    candidate = session.get("candidate_service") or {}
    if not candidate:
        return False
    lead = session.setdefault("lead", {})
    lead["service_id"] = candidate["service_id"]
    lead["service_name"] = candidate["service_name"]
    # Clear slot-dependent fields that may have been set for a different
    # service — the user is committing to THIS service now
    lead.pop("midwife_id", None)
    lead.pop("midwife_name", None)
    lead.pop("appointment_date", None)
    lead.pop("appointment_time", None)
    lead.pop("duration_minutes", None)
    lead.pop("price_aed", None)
    lead.pop("location_type", None)
    session.pop("candidate_service", None)
    _debug_event(f"Committed candidate service: {candidate.get('service_name')}")
    return True


async def render_unbookable_service_message(service_id: str,
                                            service_name: str) -> str:
    """When the user has chosen a service that no midwife is currently
    scheduled for, explain clearly and suggest related bookable services
    in the same category. This is a real production scenario (a midwife
    on leave, a service paused, etc.), so the wording should be honest
    about scheduling rather than implying a system limitation."""
    services = await get_services()
    svc = next((s for s in services if s["service_id"] == service_id), None)
    category = svc.get("category") if svc else None

    suggestions = []
    if category:
        bookable = await bookable_services()
        suggestions = [s["service_name"] for s in bookable
                       if s.get("category") == category][:5]

    msg = (f"I don't have a midwife scheduled for {service_name} right now. "
           f"Please call us at +971 50 729 7197 or email info@nativacare.com "
           f"and we'll arrange it for you.")
    if suggestions:
        msg += "\n\nIn the meantime, these similar services can be booked through chat:\n"
        msg += "\n".join(f"  • {s}" for s in suggestions)
    return msg


def render_language_prompt(service_name: str = "") -> str:
    """Return the prompt asking user which language they'd like their
    session in. Called when transitioning into LANGUAGE_PICKING state."""
    prefix = ""
    if service_name:
        prefix = f"Great — {service_name} it is. "
    return (
        prefix + "In which language would you like your session?\n\n"
        + "\n".join(f"  • {lang}" for lang in SUPPORTED_LANGUAGES)
        + "\n\nReply with a language (e.g. 'English' or 'Arabic')."
    )


async def midwives_speaking(language: str) -> list[dict]:
    """Return active midwives who speak the given language (canonical form).
    Empty if none. Used for language-filtered slot filtering."""
    midwives = await get_midwives()
    return [m for m in midwives
            if m.get("active", True) and midwife_speaks(m, language)]


async def render_next_available_days_by_language(
    service_id: str, language: str,
    num_days: int = 7,
    service_name: str = "",
) -> str:
    """Same as render_next_available_days, but only counts slots from
    midwives who speak the requested language.

    If a day has zero slots after filtering, it's omitted from the list.
    If NO days have any slots in this language, we tell the user honestly
    and offer the phone number rather than showing empty days.
    """
    if not await is_service_bookable(service_id):
        return await render_unbookable_service_message(
            service_id, service_name or service_id
        )
    days = await get_next_available_days(service_id, num_days=num_days)
    if not days:
        return ("I couldn't find any open slots in the next "
                f"{num_days} days. Please call us at +971 50 729 7197 to "
                "discuss options.")

    # Filter each day's slots by language, keep only days with 1+ slot
    filtered = []
    for d in days:
        matching = [s for s in d.slots
                    if _slot_matches_language(s, language)]
        if matching:
            filtered.append((d, len(matching)))

    if not filtered:
        return (f"I don't have any {language}-language slots available for "
                f"{service_name or 'this service'} in the next {num_days} "
                f"days. Please call us at +971 50 729 7197 to discuss "
                f"other options, or reply with a different language.")

    lines = [f"Available days for {language} sessions:"]
    for d, count in filtered:
        on_date = datetime.strptime(d.date, "%Y-%m-%d")
        nice = on_date.strftime("%A, %B %d")
        lines.append(f"  • {nice} ({count} slot{'s' if count != 1 else ''} available)")
    lines.append("\nReply with a date (e.g. 'May 18' or 'Tuesday').")
    return "\n".join(lines)


def _slot_matches_language(slot, language: str) -> bool:
    """Check whether a slot's midwife speaks the given language.

    Uses the slot's `midwife_name` to look up the midwife's languages.
    Cached midwife lookup would be nicer but this is called for every
    slot in the day list, and the midwives list is small (2-5 entries)
    so linear search is fine.
    """
    if not language:
        return True
    # This function is called with a slot object that has midwife_name
    # but not the full midwife record. To check languages, we need to
    # look up the midwife. For efficiency we memoize per-call via a
    # class attribute — refreshed each render pass.
    # (Actual lookup happens via _get_midwife_languages_cached below.)
    langs = _get_midwife_languages_cached(slot.midwife_name)
    return language.lower() in langs.lower()


# Small in-memory cache of midwife name → languages string. Refreshed
# each time midwives are re-fetched by the top of a request.
_MIDWIFE_LANG_CACHE = {}


def _get_midwife_languages_cached(midwife_name: str) -> str:
    """Look up a midwife's languages string from the cache. Returns
    empty string if not found (fail-open: unknown midwife = no filter)."""
    return _MIDWIFE_LANG_CACHE.get(midwife_name, "")


async def _refresh_midwife_lang_cache():
    """Refresh the midwife-language cache from the Sheet. Called at the
    start of each language-filtered render pass."""
    global _MIDWIFE_LANG_CACHE
    midwives = await get_midwives()
    _MIDWIFE_LANG_CACHE = {
        m["midwife_name"]: (m.get("languages") or "")
        for m in midwives if m.get("midwife_name")
    }


async def render_next_available_days(service_id: str, num_days: int = 7,
                                     service_name: str = "") -> str:
    # Defensive check: is this service actually bookable at all?
    if not await is_service_bookable(service_id):
        return await render_unbookable_service_message(
            service_id, service_name or service_id
        )
    days = await get_next_available_days(service_id, num_days=num_days)
    if not days:
        return ("I couldn't find any open slots in the next "
                f"{num_days} days. Please call us at +971 50 729 7197 to "
                "discuss options.")
    lines = ["Which day works for you?"]
    for d in days:
        on_date = datetime.strptime(d.date, "%Y-%m-%d")
        nice = on_date.strftime("%A, %B %d")
        lines.append(f"  • {nice} ({len(d.slots)} slots available)")
    lines.append("\nReply with a date (e.g. 'May 18' or 'Tuesday').")
    return "\n".join(lines)


async def render_slots_for_day(service_id: str, date_str: str,
                               language: Optional[str] = None) -> str:
    day = await get_availability(service_id, date_str)
    if not day.slots:
        return (f"No slots available on {date_str}. Would you like to try "
                "another day?")
    # Language filter: if requested, keep only slots with midwives who
    # speak that language. Requires the midwife-language cache to be
    # populated (call _refresh_midwife_lang_cache() before this).
    if language:
        await _refresh_midwife_lang_cache()
        filtered_slots = [s for s in day.slots
                          if _slot_matches_language(s, language)]
        if not filtered_slots:
            return (f"No {language}-language slots available on {date_str}. "
                    f"Reply with a different day, or a different language.")
        # Rebuild a filtered day view for format_slots_human
        from models import DayAvailability
        day = DayAvailability(
            date=day.date, weekday=day.weekday, slots=filtered_slots,
        )
    return ("Here are the available times:\n\n"
            + format_slots_human(day, max_lines=30)
            + "\n\nReply with a time (e.g. '10:00' or '2pm').")


# ---------------------------------------------------------------------------
# Time-of-day filtering (Path B from the "evening slots" bug)
# ---------------------------------------------------------------------------
# Lets users ask "any evening slots?" / "morning preferred" / "afternoons
# only" and get answers filtered by bucket rather than the bot ignoring
# the preference. Buckets are:
#   morning   06:00–11:59
#   afternoon 12:00–16:59
#   evening   17:00–21:59
# Boundaries are documented choices, not based on anything formal.

_TIME_OF_DAY_BUCKETS = {
    "morning": (6, 12),     # [6:00, 12:00)
    "afternoon": (12, 17),  # [12:00, 17:00)
    "evening": (17, 22),    # [17:00, 22:00)
}

# Phrase → bucket name. Order doesn't matter since we check all.
_TOD_PHRASES = {
    "morning": "morning", "mornings": "morning", "before noon": "morning",
    "early": "morning",
    "afternoon": "afternoon", "afternoons": "afternoon", "midday": "afternoon",
    "lunchtime": "afternoon",
    "evening": "evening", "evenings": "evening", "after work": "evening",
    "late": "evening", "night": "evening", "tonight": "evening",
}


def detect_time_of_day(message: str) -> Optional[str]:
    """Return 'morning' / 'afternoon' / 'evening' if the message contains
    a time-of-day reference, else None."""
    if not message:
        return None
    lc = message.lower()
    # Match longest phrases first to avoid 'late evening' triggering 'late' twice
    for phrase, bucket in sorted(_TOD_PHRASES.items(), key=lambda x: -len(x[0])):
        if phrase in lc:
            return bucket
    return None


# Day-type filtering: weekend (Sat/Sun) vs weekday (Mon-Fri).
# Different from time-of-day buckets — this filters WHICH DAYS, not which
# hours within a day. Used when the user says "show me weekend slots"
# or "any weekday options."
_WEEKEND_PHRASES = [
    "weekend", "weekends", "saturday or sunday", "sat or sun",
    "this weekend", "next weekend",
]
_WEEKDAY_PHRASES = [
    "weekday", "weekdays", "during the week", "in the week",
    "work week", "mon to fri", "monday to friday",
]


def detect_day_type(message: str) -> Optional[str]:
    """Return 'weekend' or 'weekday' if the message filters by day type,
    else None."""
    if not message:
        return None
    lc = message.lower()
    # Check weekend FIRST because "this weekend" / "next weekend" should
    # match before any generic "week" substring in weekday phrases.
    if any(p in lc for p in _WEEKEND_PHRASES):
        return "weekend"
    if any(p in lc for p in _WEEKDAY_PHRASES):
        return "weekday"
    return None


# Named-weekday safety net. The LLM resolves phrases like "saturday" into
# an actual YYYY-MM-DD itself (see the "Resolve relative dates" rule in
# the understand prompt) — but it can get this wrong, especially when a
# message mixes a weekday name with a number that could be misread as a
# day-of-month instead of a time (real example: "saturday at 10" was
# resolved to "the 10th" — which happened to be a Thursday — instead of
# the actual next Saturday). Since checking "does this date really fall
# on the weekday the user named" is simple, deterministic arithmetic, we
# don't need to trust the LLM for it at all — this catches and silently
# corrects that entire class of mistake regardless of why the LLM got it
# wrong.
_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def detect_named_weekday(message: str) -> Optional[int]:
    """Return 0-6 (Monday=0) if the message names a specific day of the
    week, else None. Deliberately distinct from detect_day_type() above,
    which only detects the generic categories "weekend"/"weekday" — this
    is for an exact day name like "saturday" or "next friday"."""
    if not message:
        return None
    lc = message.lower()
    for name, idx in _WEEKDAY_NAMES.items():
        if re.search(r"\b" + name + r"\b", lc):
            return idx
    return None


def correct_date_for_named_weekday(message: str, date_str: Optional[str]) -> Optional[str]:
    """If the message names a specific weekday (e.g. "saturday") and
    date_str doesn't actually fall on that weekday, return the corrected
    date — the next real occurrence of the named weekday from today.
    Returns date_str unchanged if there's nothing to correct, or None
    was passed through if date_str was falsy."""
    if not date_str:
        return date_str
    target_weekday = detect_named_weekday(message)
    if target_weekday is None:
        return date_str
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str
    if parsed.weekday() == target_weekday:
        return date_str  # already correct, nothing to do

    today = datetime.now()
    days_ahead = (target_weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # "saturday" said on a Saturday means NEXT Saturday
    corrected = today + timedelta(days=days_ahead)
    corrected_str = corrected.strftime("%Y-%m-%d")
    _debug_event(
        f"Weekday mismatch corrected: LLM extracted {date_str} "
        f"({parsed.strftime('%A')}) but message named "
        f"{list(_WEEKDAY_NAMES.keys())[target_weekday]} — using "
        f"{corrected_str} instead"
    )
    return corrected_str


async def render_days_by_type(service_id: str, day_type: str,
                              service_name: str = "") -> str:
    """List days in the next 7 days that match the requested type
    (weekend = Sat/Sun, weekday = Mon-Fri)."""
    from datetime import datetime as _dt
    days = await get_next_available_days(service_id, num_days=14)
    # weekday() returns 0=Mon ... 5=Sat, 6=Sun
    weekend_indices = {5, 6}
    matching = []
    for d in days:
        try:
            parsed = _dt.strptime(d.date, "%Y-%m-%d")
            is_weekend = parsed.weekday() in weekend_indices
            if (day_type == "weekend" and is_weekend) or \
               (day_type == "weekday" and not is_weekend):
                if d.slots:  # only include days with availability
                    matching.append((d.date, parsed, len(d.slots)))
        except ValueError:
            continue
    if not matching:
        sn = service_name or "this service"
        nothing_word = ("weekend" if day_type == "weekend" else "weekday")
        return (f"No {nothing_word} slots are available for {sn} in the "
                f"next two weeks. Please call us at +971 50 729 7197 "
                f"to discuss alternatives.")
    lines = [f"{'Weekend' if day_type == 'weekend' else 'Weekday'} availability:"]
    for d, parsed, n in matching[:7]:
        pretty = parsed.strftime("%A, %B %d").replace(" 0", " ")
        lines.append(f"  • {pretty} ({n} slot{'s' if n != 1 else ''} available)")
    lines.append("\nReply with a day.")
    return "\n".join(lines)


_OTHER_DAY_PATTERNS = [
    "other day", "other days", "another day", "different day",
    "any other day", "any other days", "different days",
    "show me other", "show me another", "show me different",
    "change the day", "change day", "what days", "which days",
    "go back", "different date",
]


def wants_different_day(message: str) -> bool:
    """User wants to step back from the slot list to the day list."""
    if not message:
        return False
    lc = message.lower()
    return any(p in lc for p in _OTHER_DAY_PATTERNS)


def _slot_in_bucket(time_str: str, bucket: str) -> bool:
    """Is HH:MM in the given bucket?"""
    try:
        hour = int(time_str.split(":")[0])
    except (ValueError, IndexError):
        return False
    lo, hi = _TIME_OF_DAY_BUCKETS[bucket]
    return lo <= hour < hi


async def render_slots_for_day_filtered(service_id: str, date_str: str,
                                        bucket: str) -> str:
    """Show only slots in the requested time-of-day bucket. If the day
    has no slots in that bucket, suggest other days that do."""
    day = await get_availability(service_id, date_str)
    if not day.slots:
        return (f"No slots available on {date_str}. Would you like to try "
                "another day?")

    in_bucket = [s for s in day.slots if _slot_in_bucket(s.start_time, bucket)]

    if in_bucket:
        # Build a filtered DayAvailability-like view for format_slots_human.
        # IMPORTANT: copy weekday from the source — it's a required field
        # on the model. Forgetting it crashes the request handler.
        from models import DayAvailability
        filtered = DayAvailability(
            date=day.date,
            weekday=day.weekday,
            slots=in_bucket,
        )
        return (f"Here are the {bucket} slots:\n\n"
                + format_slots_human(filtered, max_lines=30)
                + "\n\nReply with a time (e.g. '10:00' or '2pm'), or ask for "
                  "another day if none of these work.")

    # No matches on this day — look forward 7 days for the same bucket
    days = await get_next_available_days(service_id, num_days=7)
    bucket_days = []
    for d in days:
        if any(_slot_in_bucket(s.start_time, bucket) for s in d.slots):
            bucket_days.append(d.date)

    if not bucket_days:
        return (f"No {bucket} slots are available in the next 7 days. "
                f"{date_str} has {len(day.slots)} other slot(s) if you'd "
                f"like to see them, or call us at +971 50 729 7197 to "
                f"arrange something else.")

    # Found some — name them
    from datetime import datetime as _dt
    pretty_days = []
    for d in bucket_days[:5]:
        try:
            parsed = _dt.strptime(d, "%Y-%m-%d")
            pretty_days.append(parsed.strftime("%A, %B %d").replace(" 0", " "))
        except ValueError:
            pretty_days.append(d)
    return (f"No {bucket} slots on {date_str}. Days with {bucket} availability:\n"
            + "\n".join(f"  • {p}" for p in pretty_days)
            + "\n\nReply with one of these days.")


async def render_nearest_slots(service_id: str, date_str: str,
                               target_time: str, prefix: str = "") -> str:
    """User asked for a time that isn't available. Find slots within a
    reasonable window (90 min) on either side and offer them.

    `prefix` is prepended to the reply (e.g. "12:00 isn't available — ").
    """
    day = await get_availability(service_id, date_str)
    if not day.slots:
        return prefix + f"No slots available on {date_str}."

    try:
        h, m = target_time.split(":")
        target_minutes = int(h) * 60 + int(m)
    except (ValueError, IndexError):
        # Bad target — fall back to showing all
        return prefix + ("Here are the available times:\n\n"
                         + format_slots_human(day, max_lines=30)
                         + "\n\nReply with a time.")

    # Score each slot by absolute distance from target, in minutes
    def minutes_of(t: str) -> int:
        h2, m2 = t.split(":")
        return int(h2) * 60 + int(m2)

    scored = [(abs(minutes_of(s.start_time) - target_minutes), s)
              for s in day.slots]
    scored.sort(key=lambda x: x[0])

    # Take up to 4 nearest within 90 minutes
    near = [(dist, slot) for dist, slot in scored if dist <= 90][:4]
    if not near:
        # Nothing within 90 min — fall back to full list
        return prefix + ("That's outside our hours that day. "
                         + "Here are the available times:\n\n"
                         + format_slots_human(day, max_lines=30)
                         + "\n\nReply with a time.")

    lines = [prefix + f"Closest available times to {target_time}:"]
    for _, slot in near:
        lines.append(f"  • {slot.start_time}")
    lines.append("\nReply with one of these times, or ask for a different day.")
    return "\n".join(lines)


# Fuzzy-time markers — "around 12", "near 2pm", "about noon", "roughly 3"
_FUZZY_TIME_MARKERS = [
    "around ", "near ", "about ", "roughly ", "approximately ",
    "close to ", "or so", "ish", "preferably",
]


def is_fuzzy_time_request(message: str) -> bool:
    if not message:
        return False
    lc = message.lower()
    return any(m in lc for m in _FUZZY_TIME_MARKERS)


async def render_days_filtered(service_id: str, bucket: str,
                               service_name: str = "") -> str:
    """When the user asked for a time-of-day before picking a day —
    list the days that have slots in that bucket."""
    days = await get_next_available_days(service_id, num_days=7)
    bucket_days = []
    for d in days:
        matching = [s for s in d.slots if _slot_in_bucket(s.start_time, bucket)]
        if matching:
            bucket_days.append((d.date, len(matching)))

    if not bucket_days:
        sn = service_name or "this service"
        return (f"No {bucket} slots are available for {sn} in the next 7 "
                f"days. Please call us at +971 50 729 7197 to discuss "
                f"alternatives, or reply with another time of day "
                f"(morning / afternoon).")

    from datetime import datetime as _dt
    lines = [f"Days with {bucket} availability:"]
    for d, n in bucket_days[:7]:
        try:
            parsed = _dt.strptime(d, "%Y-%m-%d")
            pretty = parsed.strftime("%A, %B %d").replace(" 0", " ")
        except ValueError:
            pretty = d
        lines.append(f"  • {pretty} ({n} slot{'s' if n != 1 else ''} available)")
    lines.append("\nReply with a day.")
    return "\n".join(lines)


def render_address_prompt(service_name: str) -> str:
    return (f"The {service_name} is a home visit. What's your address in "
            "Abu Dhabi? (Building / area is enough; we'll confirm the rest.)")


def render_location_choice(service_name: str) -> str:
    return (f"The {service_name} can be at the clinic or as a home visit. "
            "Which would you prefer? Reply 'clinic' or 'home'.")


def render_next_detail_question(session: dict) -> Optional[str]:
    """Determine the next required field and return its question, or None
    if all required details are filled."""
    lead = session.get("lead", {})
    location_type = lead.get("location_type", "")

    if not valid(lead.get("patient_name")):
        session["awaiting_field"] = "patient_name"
        return "May I have your full name?"
    if not valid(lead.get("phone")):
        session["awaiting_field"] = "phone"
        return "Please share your UAE phone number."
    if not session.get("email_asked"):
        session["email_asked"] = True
        session["awaiting_field"] = "email"
        return ("Please share your email for confirmation, or type 'skip' "
                "if you don't have one.")
    if (location_type == "home"
            and not valid(lead.get("patient_address"))):
        session["awaiting_field"] = "patient_address"
        return ("What's the address for the home visit? Building / area in "
                "Abu Dhabi is fine.")
    session["awaiting_field"] = None
    return None


def ready_for_summary(session: dict) -> bool:
    lead = session.get("lead", {})
    required = ["service_id", "midwife_id", "appointment_date",
                "appointment_time", "patient_name", "phone"]
    if not all(valid(lead.get(k)) for k in required):
        return False
    if not session.get("email_asked"):
        return False
    if lead.get("location_type") == "home" and not valid(lead.get("patient_address")):
        return False
    return True


def booking_summary(session: dict) -> str:
    lead = session.get("lead", {})
    price = lead.get("price_aed") or "—"
    email = lead.get("email") if valid(lead.get("email")) else "Not provided"
    address = lead.get("patient_address") or "—"

    visits = lead.get("scheduled_visits") or []
    if len(visits) > 1:
        visit_lines = []
        for i, v in enumerate(visits, start=1):
            try:
                nice = datetime.strptime(v["date"], "%Y-%m-%d").strftime("%A, %B %d, %Y")
            except (ValueError, TypeError):
                nice = v.get("date", "")
            visit_lines.append(
                f"  Visit {i}: {nice} at {v.get('time','')} with {v.get('midwife_name') or 'your midwife'}"
            )
        schedule_block = "• Schedule (" + str(len(visits)) + " visits):\n" + "\n".join(visit_lines)
    else:
        on_date = lead.get("appointment_date", "")
        try:
            nice_date = datetime.strptime(on_date, "%Y-%m-%d").strftime("%A, %B %d, %Y")
        except (ValueError, TypeError):
            nice_date = on_date
        schedule_block = (
            f"• Date: {nice_date}\n"
            f"• Time: {lead.get('appointment_time', '')} "
            f"({lead.get('duration_minutes', 60)} min)"
        )

    return (
        "Please review your appointment details:\n\n"
        f"• Service: {lead.get('service_name', '')}\n"
        f"• Language: {lead.get('language', 'English')}\n"
        f"{schedule_block}\n"
        f"• Location: {lead.get('location_type', '').replace('_', ' ')}\n"
        f"• Address: {address}\n"
        f"• Name: {lead.get('patient_name', '')}\n"
        f"• Phone: {lead.get('phone', '')}\n"
        f"• Email: {email}\n"
        f"• Estimated price: AED {price}"
        + (" (covers all visits)" if len(visits) > 1 else "") + "\n\n"
        "Reply 'yes' to confirm, or tell me what to change "
        "(e.g. 'change time to 11am')."
    )


# ---------------------------------------------------------------------------
# Resolving a picked time into a real available slot
# ---------------------------------------------------------------------------

async def pick_slot_for_lead(session: dict, time_str: str) -> bool:
    """Try to lock in the slot at `time_str` on the lead's current date.
    Returns True if the slot was found and assigned; False otherwise.
    Sets lead.midwife_id, midwife_name, appointment_time, duration_minutes,
    location_type, price_aed."""
    lead = session.get("lead", {})
    service_id = lead.get("service_id")
    date_str = lead.get("appointment_date")
    if not service_id or not date_str:
        return False
    day = await get_availability(service_id, date_str)
    slot = find_slot(day, time_str,
                     preferred_midwife_name=lead.get("midwife_name"))
    if not slot:
        return False

    lead["midwife_id"] = slot.midwife_id
    lead["midwife_name"] = slot.midwife_name
    lead["appointment_time"] = slot.start_time

    # Look up service details for duration + price + location
    services, links = await _gather(get_services, get_midwife_services)
    svc = next((s for s in services if s["service_id"] == service_id), None)
    if svc:
        lead["duration_minutes"] = int(svc.get("duration_minutes") or 60)
        loc_type = svc.get("location_type", "")
        # If the service is single-location, FORCE location_type to match.
        # The user can't get a "home" Hypnobirthing if it's clinic-only,
        # even if a stale location_preference says otherwise.
        if loc_type in ("home", "clinic", "online"):
            lead["location_type"] = loc_type
            # Clear stale preference that would conflict
            if lead.get("location_preference") and lead["location_preference"] != loc_type:
                lead["location_preference"] = loc_type
        # Pricing: prefer the per-midwife override, else default
        price = ""
        for link in links:
            if (link["midwife_id"] == slot.midwife_id
                    and link["service_id"] == service_id
                    and valid(link.get("price_override_aed"))):
                price = link["price_override_aed"]
                break
        if not price:
            price = svc.get("default_price_aed", "")
        lead["price_aed"] = price
    return True


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

async def create_payment_invoice(session: dict, source: str) -> str:
    """Create a persisted manual-payment invoice for the current booking
    and return the chat message to show.

    This replaces the old placeholder Stripe-link flow. Instead of a fake
    checkout URL and a trust-based "type 'paid'" fallback, this writes a
    real Invoice row (see payments.py / db.py) immediately — independent
    of this chat session — with the bank details and a reference code.
    That way the booking survives the session expiring, the tab closing,
    or the server restarting; approval later reads from that row, not
    from this in-memory session.
    """
    import payments

    lead = session.get("lead", {})
    contact = lead.get("phone") or lead.get("email") or ""
    invoice = payments.create_invoice_for_lead(
        session_id=session.get("id", ""),
        channel=source,
        contact=contact,
        lead=dict(lead),
    )
    # Keep the reference on the session too, so if the user comes back
    # mid-session we can still show/re-check it without hitting the DB
    # by session_id (channel/contact is the durable lookup key; this is
    # just a same-session convenience).
    session["lead"]["invoice_id"] = invoice["id"]
    session["lead"]["invoice_reference"] = invoice["reference"]
    return payments.format_invoice_message(invoice)


async def commit_appointment(session: dict, source: str) -> str:
    from calendar_service import create_appointment_events
    from email_service import (send_patient_confirmation,
                               send_clinic_notification)

    lead_data = session.get("lead", {})
    if str(lead_data.get("email", "")).lower() == "skip":
        lead_data["email"] = ""

    lead = AppointmentLead(
        session_id=session.get("id", "unknown"),
        patient_name=lead_data.get("patient_name", ""),
        email=lead_data.get("email", ""),
        phone=lead_data.get("phone", ""),
        service_id=lead_data.get("service_id", ""),
        service_name=lead_data.get("service_name", ""),
        midwife_id=lead_data.get("midwife_id", ""),
        midwife_name=lead_data.get("midwife_name", ""),
        location_type=lead_data.get("location_type", ""),
        patient_address=lead_data.get("patient_address", ""),
        appointment_date=lead_data.get("appointment_date", ""),
        appointment_time=lead_data.get("appointment_time", ""),
        duration_minutes=int(lead_data.get("duration_minutes") or 60),
        price_aed=str(lead_data.get("price_aed") or ""),
        package_id=lead_data.get("package_id", ""),
        payment_status=lead_data.get("payment_status", ""),
        language=lead_data.get("language", "en"),
        source=source,
        status="confirmed",
        additional_visits=lead_data.get("scheduled_visits", [])[1:],
        # scheduled_visits[0] is the same visit already in
        # appointment_date/time above (it's sorted first) — only the
        # REST are "additional". For an ordinary single-visit booking,
        # scheduled_visits has exactly one entry, so this is just [].
    )
    save_appointment(lead)

    session["last_booking"] = dict(lead_data)
    session.setdefault("past_bookings", []).append(dict(lead_data))
    session["state"] = STATE_BOOKED
    session["lead"] = {}
    session["awaiting_field"] = None
    session["email_asked"] = False

    lead_dict = lead.model_dump()
    cal_statuses = create_appointment_events(lead_dict)
    patient_email_status = await send_patient_confirmation(lead_dict)
    clinic_email_status = await send_clinic_notification(lead_dict)

    session["appointment_status"] = {
        "calendar": cal_statuses,
        "patient_email": patient_email_status,
        "clinic_email": clinic_email_status,
    }

    last = session["last_booking"]
    visits = last.get("scheduled_visits") or []

    if len(visits) > 1:
        visit_lines = []
        for i, v in enumerate(visits, start=1):
            try:
                nice = datetime.strptime(v["date"], "%Y-%m-%d").strftime("%A, %B %d")
            except (ValueError, TypeError):
                nice = v.get("date", "")
            visit_lines.append(f"  Visit {i}: {nice} at {v.get('time','')}")
        schedule_text = "\n".join(visit_lines)
        return (
            f"🎉 Your {last.get('service_name', 'program')} is confirmed — "
            f"{len(visits)} visits scheduled:\n\n{schedule_text}\n\n"
            f"📧 Confirmation email sent to {last.get('email') or 'you'}\n"
            f"💬 Our manager has been notified — they'll reach out shortly "
            f"if any details need to be finalized.\n\n"
            f"Thank you for choosing us!"
        )

    try:
        nice_date = datetime.strptime(
            last["appointment_date"], "%Y-%m-%d"
        ).strftime("%A, %B %d")
    except (KeyError, ValueError):
        nice_date = last.get("appointment_date", "")

    return (
        f"🎉 Your {last.get('service_name', 'appointment')} is confirmed for "
        f"{nice_date} at {last.get('appointment_time', '')}.\n\n"
        f"📧 Confirmation email sent to {last.get('email') or 'you'}\n"
        f"💬 Our manager has been notified — they'll reach out shortly "
        f"if any details need to be finalized.\n\n"
        f"Thank you for choosing us!"
    )


# ---------------------------------------------------------------------------
# Response payload
# ---------------------------------------------------------------------------

def response_payload(reply: str, session: dict,
                     booking_made: bool = False) -> dict:
    lead = session.get("lead") or session.get("last_booking") or {}
    is_booked = session.get("state") == STATE_BOOKED or booking_made

    # Capture debug snapshot before returning. Includes which session this
    # was, what state we ended in, and everything _DEBUG_BUFFER collected
    # during this turn.
    debug = _debug_snapshot()
    debug["session_id"] = session.get("id")
    debug["state_after"] = session.get("state")
    debug["lead"] = dict(lead)

    return {
        "reply": reply,
        "language": "en",
        "lead_captured": is_booked,
        "booking_made": is_booked,
        "state": session.get("state"),
        "service_name": lead.get("service_name"),
        "midwife_name": lead.get("midwife_name"),
        "appointment_date": lead.get("appointment_date"),
        "appointment_time": lead.get("appointment_time"),
        "patient_name": lead.get("patient_name"),
        "phone": lead.get("phone"),
        "email": lead.get("email"),
        "patient_address": lead.get("patient_address"),
        "location_type": lead.get("location_type"),
        "price_aed": lead.get("price_aed"),
        "appointment_status": session.get("appointment_status", {}),
        "debug": debug,
    }


# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------

async def get_ai_response(session_id: str, user_message: str,
                          source: str = "website") -> dict:
    _debug_reset()
    cleanup_old_sessions()

    if not session_id:
        session_id = f"demo_{int(_time.time())}_{md5(user_message.encode()).hexdigest()[:8]}"

    if session_id not in _sessions:
        # Check demo-mode for the greeting prefix
        clinic = await get_clinic_info()
        demo_mode = clinic.get("demo_mode", "").upper() == "TRUE"
        _sessions[session_id] = {
            "id": session_id,
            "history": [],
            "lead": {},
            "last_booking": None,
            "past_bookings": [],
            "state": STATE_BROWSING,
            "awaiting_field": None,
            "email_asked": False,
            "demo_mode": demo_mode,
            "last_seen": datetime.utcnow(),
        }

    session = _sessions[session_id]
    session["last_seen"] = datetime.utcnow()
    set_session_cache(session)
    state = session["state"]
    _DEBUG_BUFFER["state_before"] = state
    _DEBUG_BUFFER["user_message"] = user_message

    # Quick emergency short-circuit before any LLM call
    if is_emergency(user_message):
        _debug_event("Emergency keyword detected — short-circuit")
        reply = emergency_reply()
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # Human-takeover short-circuit: staff have explicitly taken this
    # conversation over from the dashboard (see main.py POST
    # /conversations/{id}/takeover — this exists mainly for WhatsApp,
    # where the bot and staff share one number). While active, the bot
    # stays silent and just logs the message — staff see it on the
    # dashboard and reply themselves via POST /conversations/{id}/reply,
    # which sends through the same channel the patient is using. This is
    # different from wants_human_handoff() below, which is the bot
    # proactively telling the patient to call/email; this is staff
    # actively driving the conversation themselves.
    import db as _db
    conv = _db.get_conversation(session_id)
    if conv and conv.get("human_mode"):
        _debug_event("Human takeover active — bot silent, logging only")
        append_history(session, user_message, "[handled by staff]")
        save_chat_log(session_id, user_message, "[human_mode — awaiting staff reply]")
        payload = response_payload("", session)
        payload["human_mode"] = True
        return payload

    # Human-handoff short-circuit: user wants to talk to a person, not
    # the bot. Give them contact info immediately, preserve any
    # booking-in-progress so they can come back.
    if wants_human_handoff(user_message):
        _debug_event("Human handoff requested — short-circuit with contact info")
        reply = handoff_reply(session)
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # --- LLM Call 1: Understand ---
    understanding = await understand(session, user_message)
    intent = understanding["intent"]
    new_slots = understanding["slots"]
    _DEBUG_BUFFER["intent"] = intent
    _DEBUG_BUFFER["extracted_slots"] = dict(new_slots)
    _DEBUG_BUFFER["corrections"] = list(understanding.get("corrections", []))

    if intent == "emergency":
        # Apply the same non-emergency guard the keyword detector uses.
        # LLM tends to escalate broadly in healthcare contexts — that's
        # safer by default, but produces false positives on phrases like
        # "about to give birth soon" where the user is planning, not in
        # active emergency.
        msg_lc = " " + (user_message or "").lower() + " "
        if any(marker in msg_lc for marker in _NON_EMERGENCY_MARKERS):
            _debug_event("LLM said emergency but message contains non-emergency markers — downgraded to general")
            intent = "general"
            understanding["intent"] = "general"
            _DEBUG_BUFFER["intent"] = "general"
        else:
            reply = emergency_reply()
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

    # Apply non-conflicting slots into the active lead.
    #
    # Service-confirmation rule (architectural safeguard):
    #   service_id / service_name slots NEVER go directly into lead.
    #   They go into session["candidate_service"] instead, and are only
    #   promoted to lead.service_id after the user confirms in
    #   STATE_CONFIRMING_SERVICE. This prevents wrong-service bookings
    #   caused by LLM mis-extraction or stale conversation context.
    lead = session.setdefault("lead", {})
    msg_lower = user_message.lower()

    # Stage the proposed service as a candidate. If we already have an
    # active service in the lead (e.g. user is mid-booking), only stage a
    # NEW service when it's clearly grounded in the user's words.
    proposed_svc_id = new_slots.get("service_id")
    proposed_svc_name = new_slots.get("service_name")
    if proposed_svc_id and proposed_svc_name:
        cur_lead_svc = lead.get("service_id")
        cur_candidate = (session.get("candidate_service") or {}).get("service_id")
        # Only treat it as new if it differs from BOTH the current lead and
        # any existing candidate
        if proposed_svc_id != cur_lead_svc and proposed_svc_id != cur_candidate:
            skip = {"and", "or", "the", "a", "an", "for", "of", "my",
                    "with", "to", "in", "on"}
            words = [w.lower() for w in re.findall(r"[A-Za-z]+", proposed_svc_name)
                     if len(w) > 2 and w.lower() not in skip]
            grounded = any(w in msg_lower for w in words)
            if grounded or not cur_lead_svc:
                # Before staging this as a normal single-service candidate,
                # check whether it's actually a family with multiple tier
                # variants (half day/full day, single visit/3-visit
                # program, etc.) — if so, the user needs to pick a specific
                # tier first, rather than us silently booking whichever
                # variant happened to be the LLM's representative pick.
                #
                # Uses bookable_services() (already filtered to services
                # with an active midwife assigned), NOT the raw
                # get_services() — otherwise a family with, say, 2 sheet
                # rows but only 1 actually staffed would still show BOTH
                # as pickable options, letting someone select a tier
                # nobody can actually deliver.
                all_services = await bookable_services()
                matched_service = next(
                    (s for s in all_services if s["service_id"] == proposed_svc_id),
                    None,
                )
                family_name = (matched_service or {}).get("family_name") or ""
                variants = resolve_family_variants(all_services, family_name) if family_name else []

                if len(variants) > 1:
                    session["variant_family"] = family_name
                    session["state"] = STATE_VARIANT_PICKING
                    _debug_event(
                        f"Service {proposed_svc_name} belongs to family "
                        f"'{family_name}' with {len(variants)} variants — "
                        f"asking user to pick a tier before confirming."
                    )
                    reply = await render_variant_choice(family_name, variants)
                    append_history(session, user_message, reply)
                    save_chat_log(session_id, user_message, reply)
                    return response_payload(reply, session)

                # Set as candidate, NOT as the committed service
                session["candidate_service"] = {
                    "service_id": proposed_svc_id,
                    "service_name": proposed_svc_name,
                }
                _debug_event(
                    f"Staged candidate service: {proposed_svc_name} "
                    f"(grounded={grounded})"
                )
                # BUGFIX: STATE_BROWSING has no dedicated handler further
                # down in this function — without explicitly transitioning
                # here, session["state"] stays "browsing" forever after
                # this point (candidate_service alone doesn't advance it).
                # Every later message then falls through every specific
                # `if state == ...` check below (none match "browsing")
                # and lands on the generic conversational fallback, which
                # just uses the LLM to talk *about* booking without ever
                # running the real deterministic flow — slot locking,
                # commit_appointment(), and the payment gate never
                # execute. This was silently producing fake "confirmed"
                # replies with no real booking or invoice behind them.
                if state == STATE_BROWSING:
                    session["state"] = STATE_CONFIRMING_SERVICE
                    _debug_event(
                        f"Auto-entered CONFIRMING_SERVICE from BROWSING "
                        f"for {proposed_svc_name} ({proposed_svc_id})"
                    )
                    reply = await render_service_confirmation(
                        proposed_svc_id, proposed_svc_name
                    )
                    append_history(session, user_message, reply)
                    save_chat_log(session_id, user_message, reply)
                    return response_payload(reply, session)
            else:
                _debug_event(
                    f"Rejected service proposal {proposed_svc_name} "
                    f"(not grounded in user message and active service exists)"
                )

    # Apply the OTHER slots normally. Skip service_id/service_name since
    # those are handled above via candidate_service.
    #
    # Special case: when state == AWAITING_CONFIRM, never let the slot-
    # applier silently change midwife_id / midwife_name. The user is at
    # the booking summary and a midwife change requires validation (does
    # this midwife offer this service? are they free at this time?).
    # The AWAITING_CONFIRM state handler picks up the proposed midwife
    # from new_slots and validates explicitly.
    blocked_keys = set()
    if state == STATE_AWAITING_CONFIRM:
        blocked_keys.update(("midwife_id", "midwife_name"))

    for key, value in new_slots.items():
        if not value:
            continue
        if key in ("service_id", "service_name"):
            continue  # handled above
        if key in blocked_keys:
            continue  # handled by state handler with validation

        # Weekday safety net (see correct_date_for_named_weekday) — catches
        # the LLM resolving "saturday" to a date that isn't actually a
        # Saturday, before it ever reaches the lead or gets shown to the
        # user as a fake confirmation.
        if key == "appointment_date":
            value = correct_date_for_named_weekday(user_message, value)

        # Only set if not already set, or if it's an answer-style update
        if not valid(lead.get(key)):
            lead[key] = value
        elif intent in ("correction", "answer", "pick_slot") and lead.get(key) != value:
            lead[key] = value

    # =====================================================================
    # State: VARIANT_PICKING — user picked a service family with multiple
    # tiers (half day/full day, single visit/3-visit program, etc.) and
    # now needs to pick a specific one before we can confirm a real
    # bookable service_id.
    # =====================================================================
    if state == STATE_VARIANT_PICKING:
        family_name = session.get("variant_family", "")
        # Same fix as the staging block above — must use bookable_services()
        # here too, since this is the code that actually resolves the
        # user's typed choice against the option list. Using the
        # unfiltered list would let someone select a tier with no midwife
        # assigned to it.
        all_services = await bookable_services()
        variants = resolve_family_variants(all_services, family_name)

        if intent == "deny" or msg_lower.strip() in ("cancel", "nevermind", "never mind"):
            session["state"] = STATE_BROWSING
            session.pop("variant_family", None)
            reply = "No problem — let me know if you'd like to see the options again or pick something else."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Resolve the user's reply to a specific variant — see
        # match_variant()'s docstring for the real bugs this replaced
        # (a directional substring-match miss, and "session" false-
        # matching inside "sessions"). This one function now also
        # understands ordinals ("the third one") and "option N" phrasing
        # directly, instead of only a bare digit.
        matched = match_variant(user_message, variants)

        if not matched:
            reply = await render_variant_choice(family_name, variants)
            reply = "Sorry, I didn't catch which one — " + reply[0].lower() + reply[1:]
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Matched a specific variant — stage it as the real candidate
        # service and proceed exactly like a normal single-service pick.
        session["candidate_service"] = {
            "service_id": matched["service_id"],
            "service_name": matched["service_name"],
        }
        session.pop("variant_family", None)
        session["state"] = STATE_CONFIRMING_SERVICE
        reply = await render_service_confirmation(matched["service_id"], matched["service_name"])
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: CONFIRMING_SERVICE — bot asked "is this the right service?"
    # =====================================================================
    if state == STATE_CONFIRMING_SERVICE:
        # ----- Package reference: redirect to phone -----
        # The user named a package (or used a package phrasing) instead
        # of confirming the current candidate. Packages aren't bookable
        # through chat, so we redirect honestly.
        pkg_name = await references_package(user_message)
        if pkg_name:
            _debug_event(f"Package reference in CONFIRMING_SERVICE: {pkg_name}")
            cand = session.get("candidate_service") or {}
            cur_svc = cand.get("service_name", "your selected service")
            label = pkg_name if pkg_name != "package" else "packages"
            reply = (f"{label.capitalize()} need to be arranged by phone — "
                     f"call us at +971 50 729 7197 or email "
                     f"info@nativacare.com.\n\n"
                     f"Should I keep your booking for the **{cur_svc}**? "
                     f"Reply 'yes' to continue with that, or 'cancel' to "
                     f"start fresh.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # ----- Variant switch: user is trying to switch tiers, not just
        # confirm/deny (e.g. confirming "Half Day" but replying "actually
        # full day") -----
        # This was a real gap: neither "full day" alone nor "3 visits"
        # alone is real catalog text the understand() LLM call would ever
        # extract as a service_name (only the FAMILY name, e.g. "Nanny
        # Training", appears in its catalog) — so a bare tier-switch
        # attempt fell through every check below with nothing recognizing
        # it, and the user got the exact same "please confirm X" message
        # right back, stuck confirming the option they were trying to
        # change. Checked before the deny/confirm logic since a
        # correction like this isn't really a "no" either.
        cand = session.get("candidate_service") or {}
        cand_family = ""
        if cand.get("service_id"):
            bookable_for_switch = await bookable_services()
            cand_service = next(
                (s for s in bookable_for_switch if s["service_id"] == cand["service_id"]), None,
            )
            cand_family = (cand_service or {}).get("family_name") or ""
        if cand_family:
            family_variants = resolve_family_variants(bookable_for_switch, cand_family)
            if len(family_variants) > 1:
                switch_match = match_variant(user_message, family_variants)
                if switch_match and switch_match["service_id"] != cand["service_id"]:
                    _debug_event(
                        f"Variant switch in CONFIRMING_SERVICE: "
                        f"{cand.get('service_name')} -> {switch_match['service_name']}"
                    )
                    session["candidate_service"] = {
                        "service_id": switch_match["service_id"],
                        "service_name": switch_match["service_name"],
                    }
                    reply = await render_service_confirmation(
                        switch_match["service_id"], switch_match["service_name"]
                    )
                    append_history(session, user_message, reply)
                    save_chat_log(session_id, user_message, reply)
                    return response_payload(reply, session)


        # "No, [something else]" should NOT silently re-show the current
        # confirmation. If we have a clear new candidate from the LLM,
        # the re-confirm branch below handles it. Otherwise, acknowledge
        # the no and ask what they want instead.
        if looks_like_deny_with_alternative(user_message) and not new_slots.get("service_id"):
            _debug_event("Deny-with-alternative in CONFIRMING_SERVICE (no new candidate extracted)")
            session.pop("candidate_service", None)
            session["state"] = STATE_SERVICE_SELECTING
            reply = ("OK — which service would you like instead? "
                     "You can reply with the service name, or 'list' to "
                     "see all options.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        if intent == "deny":
            session["state"] = STATE_BROWSING
            session.pop("candidate_service", None)
            session["lead"] = {}
            reply = "Cancelled. Let me know if you'd like to start over."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # User confirmed — commit the candidate service to the lead and
        # advance to LANGUAGE PICKING (not straight to slot picking).
        # NativaCare offers sessions in multiple languages; asking language
        # first lets us filter the day/slot lists to only midwives who
        # speak the requested language.
        if intent == "confirm":
            committed = commit_candidate_service(session)
            if not committed:
                # No candidate — something went wrong; restart selection
                session["state"] = STATE_SERVICE_SELECTING
                reply = await render_service_menu()
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

            # Multi-visit programs (e.g. "Postnatal Recovery Program — 3
            # visits") need N appointment dates scheduled, not one. Reads
            # a "visits_required" column from the services sheet — a
            # normal single-visit service just doesn't set it, so this
            # defaults to 1 and nothing about the existing single-visit
            # flow changes at all.
            services = await get_services()
            svc = next((s for s in services if s["service_id"] == lead["service_id"]), None)
            visits_required = 1
            if svc:
                try:
                    visits_required = max(1, int(svc.get("visits_required") or 1))
                except (TypeError, ValueError):
                    visits_required = 1
            lead["visits_required"] = visits_required
            lead["scheduled_visits"] = []

            session["state"] = STATE_LANGUAGE_PICKING
            reply = render_language_prompt(lead.get("service_name", ""))
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # User named a different service this turn — stage the new
        # candidate and re-confirm. (We detect "new this turn" via the
        # presence of service_id in new_slots from the LLM.)
        if new_slots.get("service_id"):
            cand = session.get("candidate_service") or {}
            if cand.get("service_id"):
                reply = await render_service_confirmation(
                    cand["service_id"],
                    cand.get("service_name", ""),
                )
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

        # Side question (info, price, etc.) — answer it, stay in
        # CONFIRMING_SERVICE so the user can still say yes/no after.
        # We include "book" here too because the LLM sometimes
        # misclassifies info questions about the active service as "book"
        # (the CRITICAL rule in the understand prompt over-triggers on
        # words like "days"/"time"). If intent is "book" but NO new
        # service was named, treat as a side question rather than
        # re-showing the confirmation.
        info_intents = ("price_question", "service_list", "midwife_list",
                        "midwife_question", "package_question",
                        "availability_question", "faq", "general", "book")
        if intent in info_intents:
            reply = await compose_reply(session, user_message, understanding)
            # Append a gentle reminder of the pending confirmation
            cand = session.get("candidate_service") or {}
            if cand:
                reply += (f"\n\n(Still ready to book {cand.get('service_name')} "
                          "when you are — reply 'yes' to continue.)")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Anything else — re-show the confirmation
        cand = session.get("candidate_service") or {}
        if cand:
            reply = await render_service_confirmation(
                cand["service_id"], cand.get("service_name", "")
            )
        else:
            # No candidate? Fall back to the menu
            session["state"] = STATE_SERVICE_SELECTING
            reply = await render_service_menu()
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: LANGUAGE_PICKING — user has confirmed service, now picks
    # the session language. On success, we filter the day list to only
    # midwives who speak that language, then advance to SLOT_PICKING.
    # =====================================================================
    if state == STATE_LANGUAGE_PICKING:
        # Cancel
        if intent == "deny":
            session["state"] = STATE_BROWSING
            session["lead"] = {}
            reply = "Cancelled. Let me know if you'd like to start over."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Try to extract a language from the user's message
        lang = normalize_language(user_message)
        if not lang:
            # Didn't recognize a language — re-prompt (softly)
            reply = ("I didn't catch that. Please reply with one of the "
                     "languages we offer: "
                     + ", ".join(SUPPORTED_LANGUAGES) + ".")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Check that at least one midwife speaks this language
        speakers = await midwives_speaking(lang)
        if not speakers:
            reply = (f"I don't currently have any midwives available for "
                     f"{lang}-language sessions. Please pick from: "
                     + ", ".join(SUPPORTED_LANGUAGES) + ", or call us at "
                     f"+971 50 729 7197 to discuss options.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # All good — save language to lead, refresh cache, show days
        lead["language"] = lang
        await _refresh_midwife_lang_cache()
        session["state"] = STATE_SLOT_PICKING
        reply = await render_next_available_days_by_language(
            lead["service_id"], lang,
            service_name=lead.get("service_name", ""),
        )
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: AWAITING_CONFIRM
    # =====================================================================
    if state == STATE_AWAITING_CONFIRM:
        if intent == "confirm":
            # Re-validate the slot before committing. Protects against:
            #   - Another session having taken this slot since we showed it
            #   - The midwife's schedule changing in the sheet
            #   - Long-pending sessions where availability shifted
            # If the slot is no longer available, tell the user honestly
            # and re-show the day list instead of committing a bad booking.
            svc_id = lead.get("service_id")
            mw_name = lead.get("midwife_name")
            date_str = lead.get("appointment_date")
            time_str = lead.get("appointment_time")
            if svc_id and mw_name and date_str and time_str:
                check = await validate_midwife_for_booking(
                    mw_name, svc_id, date_str, time_str,
                )
                if not check["ok"]:
                    _debug_event(
                        f"Slot re-validation FAILED at commit: "
                        f"{check['reason']} for {mw_name} on {date_str} {time_str}"
                    )
                    # Reset to slot-picking so the user can pick another time
                    lead.pop("appointment_time", None)
                    lead.pop("appointment_date", None)
                    lead.pop("midwife_id", None)
                    lead.pop("midwife_name", None)
                    session["state"] = STATE_SLOT_PICKING
                    if check["reason"] == "no_slot_at_time":
                        reply = (
                            f"That time isn't available anymore — looks like "
                            f"the slot was taken since I showed it to you. "
                            f"Sorry about that. Let me show you the current "
                            f"availability.\n\n"
                            + await render_next_available_days(
                                svc_id, service_name=lead.get("service_name", "")
                            )
                        )
                    else:
                        reply = (
                            f"Something has changed in the schedule for this "
                            f"slot. Let me show you the latest availability.\n\n"
                            + await render_next_available_days(
                                svc_id, service_name=lead.get("service_name", "")
                            )
                        )
                    append_history(session, user_message, reply)
                    save_chat_log(session_id, user_message, reply)
                    return response_payload(reply, session)

            # Payment gate — if PAYMENT_ENABLED, move to AWAITING_PAYMENT
            # instead of committing directly. The slot is NOT reserved here
            # (per the "only reserve after payment" design decision) — the
            # calendar event and email get created only after staff
            # approve the uploaded payment proof (see payments.py).
            if PAYMENT_ENABLED:
                _debug_event("Payment gate: PAYMENT_ENABLED=true, routing to AWAITING_PAYMENT")
                session["state"] = STATE_AWAITING_PAYMENT
                reply = await create_payment_invoice(session, source)
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

            _debug_event(f"Payment gate: PAYMENT_ENABLED=false (skipping payment step, committing directly)")
            reply = await commit_appointment(session, source)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session, booking_made=True)
        if intent == "deny":
            session["state"] = STATE_BROWSING
            session["lead"] = {}
            session["email_asked"] = False
            reply = "Cancelled. Let me know if you'd like to start over."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Midwife-correction path: user wants to swap to a different midwife.
        # The slot-applier was prevented from updating midwife fields in this
        # state (see slot-applier blocked_keys logic), so we handle it here
        # with proper validation. Refuses to swap if the new midwife doesn't
        # offer this service, or isn't free at this date/time.
        proposed_mw = new_slots.get("midwife_name")
        if (proposed_mw
                and proposed_mw.strip().lower() != (lead.get("midwife_name") or "").strip().lower()):
            _debug_event(f"Midwife-correction request: {lead.get('midwife_name')} → {proposed_mw}")
            check = await validate_midwife_for_booking(
                proposed_mw,
                lead.get("service_id", ""),
                lead.get("appointment_date", ""),
                lead.get("appointment_time", ""),
            )
            if check["ok"]:
                # Valid swap — apply and re-show summary
                lead["midwife_id"] = check["midwife_id"]
                lead["midwife_name"] = check["midwife_name"]
                _debug_event(f"Midwife swapped to {check['midwife_name']} (validated)")
                reply = ("Got it — switched to "
                         f"{check['midwife_name']}.\n\n"
                         + booking_summary(session))
            else:
                # Invalid — explain honestly and offer alternatives
                reason = check["reason"]
                if reason == "unknown_midwife":
                    alt = check.get("alternatives") or []
                    alt_text = (", ".join(alt) if alt
                                else "no one available for this service")
                    reply = (f"I don't recognize \"{proposed_mw}\" as one of "
                             f"our midwives. For "
                             f"{lead.get('service_name', 'this service')}, "
                             f"you can choose: {alt_text}. Reply with a "
                             f"name, or 'yes' to keep your current booking "
                             f"with {lead.get('midwife_name', '')}.")
                elif reason == "service_mismatch":
                    alt = check.get("alternatives") or []
                    alt_text = (", ".join(alt) if alt
                                else "no one currently scheduled")
                    reply = (f"{check.get('midwife_name', proposed_mw)} doesn't "
                             f"offer {lead.get('service_name', 'this service')}. "
                             f"For this service you can choose: {alt_text}. "
                             f"Or reply 'yes' to keep your booking with "
                             f"{lead.get('midwife_name', '')}.")
                elif reason == "no_slot_at_time":
                    alt = check.get("alternatives") or []
                    if alt:
                        alt_text = ", ".join(alt)
                        reply = (f"{check.get('midwife_name', proposed_mw)} isn't "
                                 f"available at {lead.get('appointment_time', '')} "
                                 f"on {lead.get('appointment_date', '')}. "
                                 f"Times {check['midwife_name']} has that day: "
                                 f"{alt_text}. Reply with a different time, "
                                 f"or 'yes' to keep "
                                 f"{lead.get('midwife_name', '')}.")
                    else:
                        reply = (f"{check.get('midwife_name', proposed_mw)} has "
                                 f"no availability on "
                                 f"{lead.get('appointment_date', '')}. "
                                 f"Reply 'yes' to keep your booking with "
                                 f"{lead.get('midwife_name', '')}, or tell me "
                                 f"a different day.")
                else:
                    reply = (f"I can't switch to {proposed_mw} for this "
                             f"appointment. Reply 'yes' to keep your "
                             f"current booking with "
                             f"{lead.get('midwife_name', '')}.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Service-correction path: user wants to switch from one service
        # to another while at the summary. Two flavors:
        #
        # (a) They named a specific NEW service — swap it in and re-route
        #     to slot picking (date/time/midwife depend on the service).
        #     Keep patient name/phone/email so they don't re-enter them.
        #
        # (b) They said something vague like "change the deal" / "want
        #     the package" — we can't auto-resolve. Acknowledge honestly
        #     and offer to either keep this booking or cancel & call for
        #     a package.

        # (a) Did the LLM extract a new service that differs from the lead?
        new_svc_id = new_slots.get("service_id")
        cur_svc_id = lead.get("service_id")
        if new_svc_id and new_svc_id != cur_svc_id:
            _debug_event(f"Service correction in AWAITING_CONFIRM: {cur_svc_id} -> {new_svc_id}")
            # Verify the new service is bookable before switching
            if await is_service_bookable(new_svc_id):
                # Clear the old booking's lead data (the user is changing
                # service, so date/time/midwife/etc. no longer apply)
                preserved = {
                    k: lead.get(k) for k in ("patient_name", "phone", "email")
                    if lead.get(k)
                }
                session["lead"] = preserved
                # Route through the confirmation gate
                reply = await enter_service_confirmation(
                    session, new_svc_id,
                    new_slots.get("service_name") or new_svc_id,
                )
            else:
                reply = (f"I'd need to handle {new_slots.get('service_name', 'that service')} "
                         f"by phone — call us at +971 50 729 7197. "
                         f"Should I keep your current {lead.get('service_name', 'booking')} "
                         f"booking, or cancel it? Reply 'yes' to confirm the "
                         f"current booking, or 'cancel' to start fresh.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # (b) Vague change-references and package references.
        # Use the shared helper so both real package names ("Newborn
        # Starter") and vague phrasings ("the bundle") are caught.
        pkg_name = await references_package(user_message)
        if pkg_name:
            _debug_event(f"Package reference in AWAITING_CONFIRM: {pkg_name}")
            label = pkg_name if pkg_name != "package" else "Packages"
            reply = (f"{label} need to be arranged by phone — "
                     f"call us at +971 50 729 7197 or "
                     f"email info@nativacare.com.\n\n"
                     f"Should I keep your current {lead.get('service_name', 'booking')} "
                     f"booking for {lead.get('appointment_date', '')} at "
                     f"{lead.get('appointment_time', '')}? Reply 'yes' to "
                     "confirm it as-is, or 'cancel' to start fresh.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Any other input — re-show summary
        reply = booking_summary(session)
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: AWAITING_PAYMENT — invoice has been issued, waiting on staff
    # to approve an uploaded payment screenshot.
    #
    # Nothing here commits a booking anymore — that only happens when
    # staff approve the invoice on the dashboard (payments.approve_invoice,
    # triggered from main.py, independent of this session). This state
    # exists just to (a) let the patient check status or cancel, and
    # (b) re-show the invoice details if they lost them. We deliberately
    # no longer trust a typed "paid" — that was a placeholder for testing
    # before real proof-of-payment existed.
    #
    # Slot is NOT reserved during this state. If staff reject the invoice
    # or the patient never pays, the slot stays available for others.
    # =====================================================================
    if state == STATE_AWAITING_PAYMENT:
        import db as _db
        msg_lc = (user_message or "").strip().lower()
        lead = session.get("lead", {})
        invoice_id = lead.get("invoice_id")

        # Cancellation — user changed their mind, drop the booking
        if intent == "deny" or msg_lc in ("cancel", "cancelled", "nevermind",
                                          "never mind"):
            if invoice_id:
                _db.update_invoice_status(invoice_id, "rejected", reject_reason="patient cancelled")
            session["state"] = STATE_BROWSING
            session["lead"] = {}
            session["email_asked"] = False
            reply = ("Booking cancelled — no charge was made. Let me know "
                     "if you'd like to start over.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        status_keywords = ("status", "check", "any update", "confirmed yet",
                           "did you get", "did you receive")
        if invoice_id and any(k in msg_lc for k in status_keywords):
            invoice = _db.get_invoice(invoice_id)
            inv_status = (invoice or {}).get("status", "pending_proof")
            if inv_status == "approved":
                reply = "Good news — your payment was verified and your booking is confirmed. Check your email for the confirmation."
            elif inv_status == "submitted":
                reply = "We've received your screenshot and it's waiting for our team to verify it — we'll confirm shortly."
            elif inv_status == "rejected":
                reason = (invoice or {}).get("reject_reason", "")
                reply = ("We weren't able to verify that payment"
                         + (f" ({reason})" if reason else "")
                         + f". Please send a clearer screenshot, or contact us — your reference is {invoice['reference']}.")
            else:
                reply = f"Still waiting on a payment screenshot for reference {invoice['reference']}."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Anything else — re-show the invoice details so they always have
        # the reference/account number handy, without re-creating a new
        # invoice (that would orphan the first one).
        if invoice_id:
            invoice = _db.get_invoice(invoice_id)
            if invoice:
                import payments
                invoice["_account"] = payments._default_account()
                reply = payments.format_invoice_message(invoice)
            else:
                reply = "I couldn't find your invoice — let's start the booking again."
                session["state"] = STATE_BROWSING
                session["lead"] = {}
        else:
            reply = await create_payment_invoice(session, source)
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: SLOT_PICKING — user is choosing a time from a list
    # =====================================================================
    if state == STATE_SLOT_PICKING:
        # Cancellation
        if intent == "deny":
            session["state"] = STATE_BROWSING
            session["lead"] = {}
            reply = "Booking cancelled. Let me know if there's anything else."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Date correction: user named a different day while one is already
        # set in the lead. Always update — regardless of intent classification.
        # Without this guard, "tuesday" said when lead has Monday gets
        # rejected by the slot-applier's "only-update-on-correction" rule,
        # and the user sees Monday slots again. Real user typed "tuesday"
        # twice in a transcript before it took.
        # We only intercept if it's purely a date pick (no time also being
        # set this turn) — otherwise let the date+time flow downstream lock
        # the slot directly.
        new_date = new_slots.get("appointment_date")
        new_date = correct_date_for_named_weekday(user_message, new_date)
        new_time = new_slots.get("appointment_time")
        current_date = lead.get("appointment_date")
        if (new_date and new_date != current_date and not new_time
                and lead.get("service_id")):
            _debug_event(
                f"Date correction mid-slot-picking: {current_date} → {new_date}"
            )
            lead["appointment_date"] = new_date
            lead.pop("appointment_time", None)
            reply = await render_slots_for_day(
                lead["service_id"], new_date,
                language=lead.get("language"),
            )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Day-type filter: "weekend slots" / "any weekday options" / etc.
        # List days of the requested type with available slots. Runs
        # before wants_different_day because "weekend" alone would also
        # match the broader different-day patterns.
        day_type = detect_day_type(user_message)
        if day_type and lead.get("service_id"):
            _debug_event(f"Day-type filter requested: {day_type}")
            # Clear current date so the user lands on the new selection
            lead.pop("appointment_date", None)
            lead.pop("appointment_time", None)
            reply = await render_days_by_type(
                lead["service_id"], day_type,
                service_name=lead.get("service_name", ""),
            )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # User wants to see a different day — clear the current date and
        # re-render the day list. Detects "other day", "another day",
        # "different day", "what days", etc.
        if wants_different_day(user_message) and lead.get("appointment_date"):
            _debug_event("User wants different day — clearing date and re-rendering day list")
            lead.pop("appointment_date", None)
            lead.pop("appointment_time", None)
            reply = await render_next_available_days(
                lead["service_id"],
                service_name=lead.get("service_name", ""),
            )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Time-of-day preference: user said "evening" / "morning" / "afternoon"
        # / "any other day with evening slots" / similar. Filter the slot
        # display by that bucket, OR if no date is set yet, list days that
        # have slots in that bucket.
        tod = detect_time_of_day(user_message)
        if tod and lead.get("service_id"):
            _debug_event(f"Time-of-day filter requested: {tod}")
            if lead.get("appointment_date"):
                # User has a day picked but wants only certain hours
                reply = await render_slots_for_day_filtered(
                    lead["service_id"], lead["appointment_date"], tod
                )
            else:
                # No day picked yet — show days that have slots in this bucket
                reply = await render_days_filtered(
                    lead["service_id"], tod,
                    service_name=lead.get("service_name", ""),
                )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Side question mid-booking: answer it, do NOT re-show the day list.
        # The user can pick a date/time on a later turn when they're ready.
        if intent in ("price_question", "service_list", "midwife_list",
                      "midwife_question", "package_question",
                      "availability_question", "faq", "general"):
            reply = await compose_reply(session, user_message, understanding)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # If the user gave a date but no time, show slots for that date
        if lead.get("appointment_date") and not lead.get("appointment_time"):
            reply = await render_slots_for_day(
                lead["service_id"], lead["appointment_date"],
                language=lead.get("language"),
            )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # If we have both date + time, try to lock in the slot
        if lead.get("appointment_date") and lead.get("appointment_time"):
            requested_time = lead["appointment_time"]
            ok = await pick_slot_for_lead(session, requested_time)
            if not ok:
                lead.pop("appointment_time", None)
                # If the user said "around X" / "near X" / "about X", or
                # in general the time isn't an exact match, suggest the
                # nearest available slots instead of re-dumping the full
                # list. Much more useful for fuzzy time asks.
                reply = await render_nearest_slots(
                    lead["service_id"],
                    lead["appointment_date"],
                    requested_time,
                    prefix=f"{requested_time} isn't available. ",
                )
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

            # Slot locked — multi-visit programs need N dates, not one.
            # visits_required defaults to 1 for every existing service, so
            # this block is a no-op for the single-visit flow (it always
            # falls straight through to "this IS the last/only visit").
            visits_required = lead.get("visits_required", 1)
            scheduled = lead.setdefault("scheduled_visits", [])
            this_visit = {
                "date": lead["appointment_date"],
                "time": lead["appointment_time"],
                "midwife_id": lead.get("midwife_id", ""),
                "midwife_name": lead.get("midwife_name", ""),
            }

            if visits_required > 1 and len(scheduled) + 1 < visits_required:
                # More visits still needed — record this one, clear the
                # date/time so the next round of slot-picking starts
                # fresh, and loop back showing the next available days.
                # midwife_name is deliberately LEFT SET as a preference —
                # pick_slot_for_lead() already prefers it via
                # find_slot(preferred_midwife_name=...), so the program
                # tries to keep the same midwife across every visit
                # without forcing it if she's unavailable on a later date.
                scheduled.append(this_visit)
                visit_num = len(scheduled)
                try:
                    nice_date = datetime.strptime(this_visit["date"], "%Y-%m-%d").strftime("%A, %B %d")
                except (ValueError, TypeError):
                    nice_date = this_visit["date"]
                lead.pop("appointment_date", None)
                lead.pop("appointment_time", None)
                day_list = await render_next_available_days(
                    lead["service_id"], service_name=lead.get("service_name", ""),
                )
                reply = (
                    f"Visit {visit_num} of {visits_required} confirmed — "
                    f"{nice_date} at {this_visit['time']} with "
                    f"{this_visit['midwife_name'] or 'your midwife'}.\n\n"
                    f"Now let's schedule visit {visit_num + 1} of {visits_required}:\n\n"
                    f"{day_list}"
                )
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

            # This is the last (or only) visit — finalize the full visit
            # list. For an ordinary single-visit booking this just wraps
            # the one visit already sitting in appointment_date/time and
            # changes nothing else below.
            scheduled.append(this_visit)
            visit_confirmation_prefix = ""
            if visits_required > 1:
                scheduled.sort(key=lambda v: (v["date"], v["time"]))
                lead["scheduled_visits"] = scheduled
                first = scheduled[0]
                lead["appointment_date"] = first["date"]
                lead["appointment_time"] = first["time"]
                lead["midwife_id"] = first["midwife_id"]
                lead["midwife_name"] = first["midwife_name"]
                # The other visits (1..N-1) already got their own "Visit X
                # of N confirmed" message further up before looping back
                # for the next date — without this, the FINAL visit would
                # silently roll straight into the next question (location
                # choice / name) with no acknowledgment it was locked in
                # at all, which reads as if the bot ignored the message.
                try:
                    nice_date = datetime.strptime(this_visit["date"], "%Y-%m-%d").strftime("%A, %B %d")
                except (ValueError, TypeError):
                    nice_date = this_visit["date"]
                visit_confirmation_prefix = (
                    f"Visit {visits_required} of {visits_required} confirmed — "
                    f"{nice_date} at {this_visit['time']} with "
                    f"{this_visit['midwife_name'] or 'your midwife'}. "
                    f"All {visits_required} visits are now scheduled!\n\n"
                )

            # Figure out next step: location choice or details
            service = next(
                (s for s in await get_services()
                 if s["service_id"] == lead["service_id"]),
                None,
            )
            if service:
                loc_type = service.get("location_type", "")
                if loc_type in ("clinic_or_home", "clinic_or_online"):
                    if not lead.get("location_type"):
                        session["state"] = STATE_COLLECTING_DETAILS
                        reply = visit_confirmation_prefix + render_location_choice(lead.get("service_name", "service"))
                        append_history(session, user_message, reply)
                        save_chat_log(session_id, user_message, reply)
                        return response_payload(reply, session)

            session["state"] = STATE_COLLECTING_DETAILS
            q = render_next_detail_question(session)
            reply = visit_confirmation_prefix + (q or "Almost done — anything to add?")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # No date yet — show day list
        reply = await render_next_available_days(lead["service_id"], service_name=lead.get("service_name", ""))
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: COLLECTING_DETAILS
    # =====================================================================
    if state == STATE_COLLECTING_DETAILS:
        # Side question → answer it but don't re-prompt
        if intent in ("price_question", "service_list", "midwife_list",
                      "midwife_question", "package_question",
                      "availability_question", "faq", "general"):
            reply = await compose_reply(session, user_message, understanding)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Email-skip check MUST run BEFORE the deny handler below. When the
        # bot is waiting for an email, "skip" / "no" / "no email" are valid
        # answers to the email prompt (user doesn't want to share one),
        # NOT booking cancellations. The LLM often classifies "skip" as
        # deny (because it maps to "no thanks / stop"), so we intercept
        # here based on context: if awaiting_field is "email", these words
        # mean "skip this field" not "cancel the whole booking."
        if (session.get("awaiting_field") == "email"
                and user_message.lower().strip() in ("skip", "no", "no email",
                                                     "no thanks", "no thank you",
                                                     "dont have one", "don't have one",
                                                     "n/a", "na")):
            lead["email"] = "skip"
            # Fall through to the rest of the handler which will advance
            # to the next detail field (or move to AWAITING_CONFIRM if all
            # fields collected)

        if intent == "deny":
            session["state"] = STATE_BROWSING
            session["lead"] = {}
            session["email_asked"] = False
            reply = "Booking cancelled. Let me know if there's anything else."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Handle bare-text answers (name, address)
        awaiting = session.get("awaiting_field")
        if awaiting == "patient_name" and not valid(lead.get("patient_name")):
            text = user_message.strip()
            if 2 <= len(text) <= 50 and not text.isdigit() and "@" not in text:
                lead["patient_name"] = text
        elif awaiting == "patient_address" and not valid(lead.get("patient_address")):
            text = user_message.strip()
            if 5 <= len(text) <= 200:
                lead["patient_address"] = text
        elif awaiting == "phone" and not valid(lead.get("phone")):
            candidate_phone = user_message.strip()
            digits = re.sub(r"\D", "", candidate_phone)
            # UAE-only, matching the fact that NativaCare only operates in
            # Abu Dhabi — a plausible-length international number (e.g. a
            # UK or US mobile) used to be accepted here just because it
            # was the right number of digits. Now requires an actual UAE
            # shape: local (0 + 8-9 more digits, e.g. 050XXXXXXX mobile or
            # 02XXXXXXX Abu Dhabi landline) or international (971 + 8-9
            # more digits, optionally with a 00 international prefix).
            normalized = digits[2:] if digits.startswith("00") else digits
            looks_uae_local = normalized.startswith("0") and 9 <= len(normalized) <= 10
            looks_uae_intl = normalized.startswith("971") and 11 <= len(normalized) <= 12
            if looks_uae_local or looks_uae_intl:
                lead["phone"] = candidate_phone
            else:
                phone_rejection_count = session.get("phone_rejection_count", 0) + 1
                session["phone_rejection_count"] = phone_rejection_count
                reply = (
                    f"\"{candidate_phone}\" doesn't look like a UAE phone "
                    f"number — we currently only serve clients in Abu Dhabi, "
                    f"so we'll need a UAE mobile or landline number "
                    f"(e.g. 050 123 4567 or +971 50 123 4567)."
                )
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)
        if lead.get("phone"):
            session.pop("phone_rejection_count", None)

        # Handle location choice. If the user explicitly says "clinic"
        # (or similar) while in home-visit flow, switch — useful when
        # we've rejected their address as out-of-area.
        msg_low = user_message.lower()
        if (not lead.get("location_type")):
            if "home" in msg_low or "visit" in msg_low or "address" in msg_low:
                lead["location_type"] = "home"
            elif "clinic" in msg_low or "in person" in msg_low:
                lead["location_type"] = "clinic"
            elif "online" in msg_low or "virtual" in msg_low or "video" in msg_low:
                lead["location_type"] = "online"
        elif (lead.get("location_type") == "home"
              and (msg_low.strip() == "clinic" or "in person" in msg_low
                   or "switch to clinic" in msg_low or "the clinic" in msg_low)):
            # User wants to switch from home to clinic
            _debug_event("User switched home → clinic mid-flow")
            lead["location_type"] = "clinic"
            lead["patient_address"] = ""
            session.pop("address_rejection_count", None)

        # Out-of-area address: REJECT and re-prompt instead of accepting.
        # Two checks, in order:
        #   1. Does it explicitly name a DIFFERENT emirate/city? If so,
        #      reject outright — this catches "Dubai Marina" even though
        #      an old, since-removed version of ABU_DHABI_KEYWORDS
        #      contained the ambiguous word "marina" that would have
        #      matched it.
        #   2. Otherwise, does it contain any actual Abu Dhabi area
        #      keyword? If neither, we don't know where it is — reject
        #      and ask again rather than guessing.
        # After 2 rejections, escalate to phone rather than blocking
        # forever.
        addr = lead.get("patient_address") or ""
        addr_is_out_of_area = addr and (
            _address_names_other_emirate(addr)
            or not any(k in addr.lower() for k in ABU_DHABI_KEYWORDS)
        )
        if lead.get("location_type") == "home" and addr_is_out_of_area:
            rejection_count = session.get("address_rejection_count", 0) + 1
            session["address_rejection_count"] = rejection_count
            # Clear the rejected address so we re-collect it
            lead["patient_address"] = ""
            session["awaiting_field"] = "patient_address"

            if rejection_count >= 2:
                # User has tried twice. Stop blocking and escalate to phone.
                session["state"] = STATE_BROWSING
                session["lead"] = {}
                session.pop("address_rejection_count", None)
                reply = (f"Home visits are currently Abu Dhabi only, and "
                         f"\"{addr}\" doesn't look like an Abu Dhabi address. "
                         f"Please call us at +971 50 729 7197 to discuss "
                         f"options. I'm happy to help with anything else.")
            else:
                reply = (f"Our home visits are Abu Dhabi only, and \"{addr}\" "
                         f"doesn't look like an Abu Dhabi address. Could you "
                         f"give an Abu Dhabi address (building or area name)? "
                         f"Or reply 'clinic' to switch to a clinic visit.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Too-generic address: in Abu Dhabi but missing building/street.
        # "abu dhabi" alone isn't enough — the midwife needs to know where
        # to actually go. Ask for a specific building or area.
        if (lead.get("location_type") == "home" and addr
                and is_too_generic_address(addr)):
            rejection_count = session.get("address_rejection_count", 0) + 1
            session["address_rejection_count"] = rejection_count
            lead["patient_address"] = ""
            session["awaiting_field"] = "patient_address"
            if rejection_count >= 2:
                session["state"] = STATE_BROWSING
                session["lead"] = {}
                session.pop("address_rejection_count", None)
                reply = (f"I'd need a more specific address than \"{addr}\" "
                         f"to arrange a home visit. Please call us at "
                         f"+971 50 729 7197 to book this one. Happy to "
                         f"help with anything else.")
            else:
                reply = (f"\"{addr}\" is a bit too general — could you give "
                         f"a building name, street, or specific area "
                         f"(e.g. 'Bateen Tower' or 'Al Raha Gardens villa "
                         f"12')? Or reply 'clinic' to switch to a clinic "
                         f"visit.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Successful address (in Abu Dhabi) — clear rejection count
        if lead.get("patient_address"):
            session.pop("address_rejection_count", None)

        # Check if ready for summary
        if ready_for_summary(session):
            session["state"] = STATE_AWAITING_CONFIRM
            reply = booking_summary(session)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        q = render_next_detail_question(session)
        reply = q or booking_summary(session)
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: SERVICE_SELECTING
    # =====================================================================
    if state == STATE_SERVICE_SELECTING:
        if intent == "deny":
            session["state"] = STATE_BROWSING
            session["lead"] = {}
            reply = "No problem. Let me know if there's anything else."
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Package check — same as in BROWSING/book. The user often lands
        # in SERVICE_SELECTING from a prior "book" attempt, then types a
        # package name. We need to detect and redirect to phone before
        # falling through to the menu.
        #
        # Three ways to detect:
        #  (a) lead has a package_id set (LLM extracted it earlier)
        #  (b) user's current message names a package
        #  (c) user said "book it" / pronoun-book after a package was just discussed
        pkg_name = None
        if lead.get("package_name"):
            pkg_name = lead["package_name"]
        elif lead.get("package_id"):
            pkg_name = lead["package_id"]  # better than nothing
        if not pkg_name:
            pkg_name = await references_package(user_message)
        if not pkg_name:
            recent_packages = session.get("last_mentioned_packages") or []
            if (recent_packages
                    and references_recent_recommendations(user_message)):
                pkg_name = recent_packages[0]
                _debug_event(f"Pronoun-book after package discussion in SERVICE_SELECTING: {pkg_name}")

        if pkg_name:
            _debug_event(f"Package reference in SERVICE_SELECTING: {pkg_name}")
            label = pkg_name if pkg_name != "package" else "Packages"
            reply = (f"{label} need to be arranged by phone — call us at "
                     f"+971 50 729 7197 or email info@nativacare.com. "
                     f"Would you like to book a single service through chat "
                     f"instead? Reply with the service name (e.g. 'book "
                     f"Hypnobirthing').")
            # Clear package fields and last_mentioned so future turns don't
            # keep re-triggering this redirect
            lead.pop("package_id", None)
            lead.pop("package_name", None)
            session.pop("last_mentioned_packages", None)
            session["state"] = STATE_BROWSING
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Side question mid-flow: answer it without re-showing the menu.
        # We check this BEFORE the service-id branch so that even if the
        # LLM also picked up a service in the slots (e.g. user said
        # "tell me about hypnobirthing" — service extracted AND it's a
        # question), the side-question wins.
        info_intents = ("price_question", "service_list", "midwife_list",
                        "midwife_question", "package_question",
                        "availability_question", "faq", "general")
        if intent in info_intents:
            reply = await compose_reply(session, user_message, understanding)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        if lead.get("service_id"):
            # Already-committed service (shouldn't usually happen — service
            # commits go through CONFIRMING_SERVICE — but kept as a safety net).
            # If language not yet picked, route through LANGUAGE_PICKING first
            # (matches the normal flow). Otherwise go straight to slot picking.
            if not lead.get("language"):
                session["state"] = STATE_LANGUAGE_PICKING
                reply = render_language_prompt(lead.get("service_name", ""))
            else:
                session["state"] = STATE_SLOT_PICKING
                reply = await render_next_available_days_by_language(
                    lead["service_id"], lead["language"],
                    service_name=lead.get("service_name", ""),
                )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Did the slot-applier stage a candidate service from this message?
        # If so, route through the confirmation gate.
        cand = session.get("candidate_service") or {}
        if cand.get("service_id"):
            reply = await enter_service_confirmation(
                session, cand["service_id"], cand.get("service_name", "")
            )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Category word like "wellbeing" / "workshops" — list services in
        # that category instead of re-showing the full menu. Mirrors the
        # check in the BROWSING/book handler.
        category_word = _detect_category_word(user_message)
        if category_word and not await _service_explicit_in_message(user_message):
            in_category = await _services_in_category(category_word)
            if in_category:
                _debug_event(f"Category '{category_word}' in SERVICE_SELECTING — listing {len(in_category)} services")
                lines = [f"Our {category_word.title()} services:"]
                for s in in_category:
                    lines.append(f"  • {s['service_name']}")
                lines.append("\nWhich would you like to book?")
                reply = "\n".join(lines)
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

        # No service id extracted AND not a side question — re-show the menu.
        reply = await render_service_menu()
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # =====================================================================
    # State: BROWSING or BOOKED — general conversation
    # =====================================================================

    # Promote ambiguous "yes" / "ok" / "sure" replies into book intent
    # when the bot's previous turn offered to book or show slots. The
    # compose LLM frequently says things like "would you like to book?"
    # or "I can show you available slots" — without this promotion, the
    # user's "yes" falls through to compose again and we get a vague
    # reprompt loop.
    if intent == "confirm" and state in (STATE_BROWSING, STATE_BOOKED):
        last_assistant = ""
        for msg in reversed(session.get("history", [])):
            if msg.get("role") == "assistant":
                last_assistant = msg.get("content", "").lower()
                break
        # If the bot offered to book / show slots / proceed, treat "yes"
        # as a book intent. The book branch will then route to either
        # slot picking (if a service is set) or the service menu.
        booking_offers = [
            "would you like to book", "want to book", "shall i book",
            "show you available slots", "show you the available",
            "show you times", "show you the times", "show you slots",
            "ready to book", "proceed with booking",
            "would you like me to book", "would you like me to show",
        ]
        if any(p in last_assistant for p in booking_offers):
            _debug_event("Confirm in BROWSING promoted to book (prior turn offered booking)")
            intent = "book"
            _DEBUG_BUFFER["intent"] = intent
        else:
            # No clear booking offer — fall through to compose for a normal reply
            pass

    if intent == "book":
        # Multi-booking guard: detect "book all three", "two appointments",
        # "multiple sessions", etc. We don't support multi-booking in v1,
        # so reply with a graceful explanation and offer to start with one.
        msg_lc = (user_message or "").lower()
        multi_signals = [
            "all three", "all 3", "all of them", "both of them",
            "multiple appointments", "multiple sessions", "several sessions",
            "two appointments", "three appointments", "2 appointments",
            "3 appointments", "book three", "book two", "book multiple",
            "book several", "book a few", "book all",
        ]
        if any(s in msg_lc for s in multi_signals):
            _debug_event("Multi-booking request detected — offered to start with one")
            reply = ("I can help you book — though I'll need to take one "
                     "appointment at a time. Which would you like to start "
                     "with? If you tell me the first service, we'll get "
                     "that locked in, and then we can come back for the "
                     "next one.")
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Package check: detect when the user is trying to book a package
        # (which isn't bookable through chat) and redirect to phone. Same
        # check used in CONFIRMING_SERVICE and AWAITING_CONFIRM. Must run
        # BEFORE the category guard because some package names contain
        # category words (e.g. "Newborn Starter" contains "newborn").
        pkg_name = await references_package(user_message)

        # If the user didn't name a package directly, check whether a
        # package was discussed in the previous turn AND the user used a
        # pronoun-book pattern ("ok book it", "book it"). That counts as
        # a package-booking attempt too.
        if not pkg_name:
            recent_packages = session.get("last_mentioned_packages") or []
            if (recent_packages
                    and references_recent_recommendations(user_message)):
                # Use the most recently mentioned package
                pkg_name = recent_packages[0]
                _debug_event(f"Pronoun-book after package discussion: {pkg_name}")

        if pkg_name:
            _debug_event(f"Package reference in BROWSING/book: {pkg_name}")
            label = pkg_name if pkg_name != "package" else "Packages"
            reply = (f"{label} need to be arranged by phone — call us at "
                     f"+971 50 729 7197 or email info@nativacare.com. "
                     f"Would you like to book a single service through chat "
                     f"instead? Reply with the service name (e.g. 'book "
                     f"Hypnobirthing').")
            # Clear so a later message isn't matched against stale data
            session.pop("last_mentioned_packages", None)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Category-word guard: if the user typed a CATEGORY ("workshop",
        # "wellbeing", "postnatal services") rather than a specific service
        # name, list services in that category and ask which one. This
        # prevents the LLM from silently picking a single service from a
        # category mention (which was causing wrong-service bookings).
        category_word = _detect_category_word(user_message)
        # Apply only when the user clearly typed a category AND we don't
        # already have a service the user previously confirmed.
        if category_word and not await _service_explicit_in_message(user_message):
            in_category = await _services_in_category(category_word)
            if in_category:
                _debug_event(f"Category word '{category_word}' detected — listing {len(in_category)} services")
                # Clear any speculative service the LLM auto-picked this turn
                if lead.get("service_id") and not session.get("service_confirmed"):
                    lead.pop("service_id", None)
                    lead.pop("service_name", None)
                session["state"] = STATE_SERVICE_SELECTING
                lines = [f"Our {category_word.title()} services:"]
                for s in in_category:
                    lines.append(f"  • {s['service_name']}")
                lines.append("\nWhich would you like to book?")
                reply = "\n".join(lines)
                append_history(session, user_message, reply)
                save_chat_log(session_id, user_message, reply)
                return response_payload(reply, session)

        # New booking
        if state == STATE_BOOKED:
            preserved_slots = {
                k: v for k, v in lead.items()
                if k in ("service_id", "service_name", "midwife_id",
                         "midwife_name", "package_id")
            }
            session["lead"] = preserved_slots
            session["email_asked"] = False

        if lead.get("service_id"):
            # Existing committed service (e.g. coming back from BOOKED with
            # preserved fields). If language not yet picked, ask that first.
            if not lead.get("language"):
                session["state"] = STATE_LANGUAGE_PICKING
                reply = render_language_prompt(lead.get("service_name", ""))
            else:
                session["state"] = STATE_SLOT_PICKING
                reply = await render_next_available_days_by_language(
                    lead["service_id"], lead["language"],
                    service_name=lead.get("service_name", ""),
                )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # Did the slot-applier stage a candidate service this turn?
        # ("book Hypnobirthing" extracts service_name into candidate)
        cand = session.get("candidate_service") or {}
        if cand.get("service_id"):
            reply = await enter_service_confirmation(
                session, cand["service_id"], cand.get("service_name", "")
            )
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # No specific service in the lead. Before falling back to the full
        # menu, check whether the user is referring to services the bot
        # just recommended ("book these classes", "book them", etc.).
        recommended = session.get("last_recommended") or []
        if recommended and references_recent_recommendations(user_message):
            _debug_event(f"User referenced last_recommended ({len(recommended)} items)")
            if len(recommended) == 1:
                # One candidate — route through confirmation gate
                reply = await enter_service_confirmation(
                    session,
                    recommended[0]["service_id"],
                    recommended[0]["service_name"],
                )
            else:
                # Multiple — ask which one
                session["state"] = STATE_SERVICE_SELECTING
                names = [r["service_name"] for r in recommended]
                if len(names) == 2:
                    name_list = f"{names[0]} or {names[1]}"
                else:
                    name_list = ", ".join(names[:-1]) + f", or {names[-1]}"
                reply = (f"Sure — which would you like to start with: "
                         f"{name_list}?")
            # Once we've used the recommendations, clear them so a later
            # message isn't matched against stale recommendations.
            session.pop("last_recommended", None)
            append_history(session, user_message, reply)
            save_chat_log(session_id, user_message, reply)
            return response_payload(reply, session)

        # No service id and no candidate recommendations — full menu.
        session["state"] = STATE_SERVICE_SELECTING
        reply = await render_service_menu()
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    if intent == "greeting":
        clinic = await get_clinic_info()
        prefix = ""
        if clinic.get("demo_mode", "").upper() == "TRUE":
            prefix = ("[Note: this is a demo deployment with placeholder "
                      "data — please verify any details with the clinic.]\n\n")
        reply = (prefix + f"Hello! Welcome to {CLINIC_NAME}. "
                 "I can help with our services, pricing, midwives, packages, "
                 "or booking an appointment. What can I help with today?")
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    if intent == "thanks":
        reply = "You're welcome. Take care."
        append_history(session, user_message, reply)
        save_chat_log(session_id, user_message, reply)
        return response_payload(reply, session)

    # All other intents → LLM compose
    reply = await compose_reply(session, user_message, understanding)
    append_history(session, user_message, reply)
    save_chat_log(session_id, user_message, reply)
    return response_payload(reply, session)
