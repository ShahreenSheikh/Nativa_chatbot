"""
Availability engine for NativaCare.

Given a service_id and a date, find which midwives are free at which time
slots. Handles:
  - Weekly schedule per midwife
  - Date-specific overrides (vacation, extra hours)
  - Break blocks (lunch) subtracted from work blocks
  - Existing bookings (from in-memory store) subtracted, with a configurable
    buffer applied on both sides
  - Service-specific duration (a 90-min hypnobirthing class needs 90 min
    of continuous free time)
  - Slot granularity (default 30 min)
  - Today's date: past slots are dropped

Returns slots sorted by start time. If multiple midwives are free for the
same time, each appears as a separate slot — the chat layer decides how to
display them.

NOTE ON CALENDAR: This engine considers ONLY in-memory bookings made through
the bot itself. Manual Google Calendar entries are NOT checked. To upgrade
to real Calendar-aware availability, add a get_calendar_busy(midwife_id,
date) helper and call it where _existing_busy_intervals() is called.

Public API:
    get_availability(service_id, date_str, ...) -> DayAvailability
    get_next_available_days(service_id, num_days=7) -> list[DayAvailability]
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, date as date_cls
from typing import Optional

from models import DayAvailability, TimeSlot
from database import (
    get_services, get_midwives, get_midwife_services,
    get_weekly_schedule, get_schedule_overrides,
    get_clinic_info, get_existing_bookings_for,
)

# Order matches Python's date.weekday() (Mon = 0 ... Sun = 6)
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Day name aliases for parsing schedule rows
_DAY_ALIASES = {
    "mon": "Mon", "monday": "Mon",
    "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
    "wed": "Wed", "weds": "Wed", "wednesday": "Wed",
    "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
    "fri": "Fri", "friday": "Fri",
    "sat": "Sat", "saturday": "Sat",
    "sun": "Sun", "sunday": "Sun",
}


# ---------------------------------------------------------------------------
# Tiny utilities — minutes-of-day arithmetic
# ---------------------------------------------------------------------------

def _to_minutes(time_str: str) -> Optional[int]:
    """'HH:MM' -> minutes past midnight, or None if unparseable."""
    if not time_str:
        return None
    try:
        h_str, m_str = time_str.strip().split(":")
        h, m = int(h_str), int(m_str)
        if 0 <= h <= 24 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        return None
    return None


def _to_hhmm(minutes: int) -> str:
    """Minutes past midnight -> 'HH:MM'."""
    h, m = divmod(int(minutes), 60)
    return f"{h:02d}:{m:02d}"


def _parse_date(date_str: str) -> Optional[date_cls]:
    """Permissive parser. Accepts 'YYYY-MM-DD' or 'YYYY/MM/DD'."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _normalize_day(day_str: str) -> Optional[str]:
    """Loose day-of-week parser. 'monday' -> 'Mon', etc."""
    if not day_str:
        return None
    return _DAY_ALIASES.get(day_str.strip().lower())


# ---------------------------------------------------------------------------
# Interval algebra
# An "interval" here is a (start_min, end_min) tuple, inclusive of start,
# exclusive of end. We represent a list of disjoint intervals as a sorted
# list of such tuples.
# ---------------------------------------------------------------------------

def _merge_intervals(intervals: list) -> list:
    """Merge overlapping/adjacent intervals. Returns sorted disjoint list."""
    if not intervals:
        return []
    cleaned = sorted(
        (s, e) for s, e in intervals if s is not None and e is not None and e > s
    )
    if not cleaned:
        return []
    merged = [cleaned[0]]
    for s, e in cleaned[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return merged


def _subtract_interval(intervals: list, sub_s: int, sub_e: int) -> list:
    """Subtract [sub_s, sub_e) from a list of disjoint intervals.

    Splits an interval in two if the subtraction creates a hole."""
    if sub_e <= sub_s:
        return list(intervals)
    out = []
    for s, e in intervals:
        if sub_e <= s or sub_s >= e:
            # No overlap
            out.append((s, e))
        else:
            # Some overlap — keep left and right slivers if any
            if s < sub_s:
                out.append((s, sub_s))
            if sub_e < e:
                out.append((sub_e, e))
    return out


def _subtract_many(intervals: list, holes: list) -> list:
    """Subtract a list of holes from a list of intervals."""
    result = list(intervals)
    for hs, he in holes:
        result = _subtract_interval(result, hs, he)
    return result


# ---------------------------------------------------------------------------
# Build work intervals for one midwife on one date
# ---------------------------------------------------------------------------

def _work_intervals_for_midwife_on_date(
    midwife_id: str,
    on_date: date_cls,
    weekly: list,
    overrides: list,
) -> list:
    """Compute (start_min, end_min) work intervals for this midwife on this
    date. Returns empty list if midwife isn't working that day at all."""
    weekday = _WEEKDAY_NAMES[on_date.weekday()]
    date_str = on_date.strftime("%Y-%m-%d")

    # Step 1: gather work blocks and break blocks from the weekly schedule
    work_blocks: list = []
    break_blocks: list = []
    for row in weekly:
        if row.get("midwife_id") != midwife_id:
            continue
        if _normalize_day(row.get("day_of_week", "")) != weekday:
            continue
        s = _to_minutes(row.get("start_time"))
        e = _to_minutes(row.get("end_time"))
        if s is None or e is None or e <= s:
            continue
        if row.get("block_type", "work").lower() == "break":
            break_blocks.append((s, e))
        else:
            work_blocks.append((s, e))

    # Step 2: apply overrides for this specific date
    day_unavailable = False
    extra_blocks: list = []
    partial_unavailable: list = []
    for row in overrides:
        if row.get("midwife_id") != midwife_id:
            continue
        if row.get("date") != date_str:
            continue
        otype = (row.get("override_type") or "").strip().lower()
        s_raw = row.get("start_time") or ""
        e_raw = row.get("end_time") or ""
        if otype == "unavailable":
            if not s_raw or not e_raw:
                day_unavailable = True
                break
            s = _to_minutes(s_raw)
            e = _to_minutes(e_raw)
            if s is not None and e is not None and e > s:
                partial_unavailable.append((s, e))
        elif otype == "extra":
            s = _to_minutes(s_raw)
            e = _to_minutes(e_raw)
            if s is not None and e is not None and e > s:
                extra_blocks.append((s, e))

    if day_unavailable:
        return []

    # Step 3: merge work + extras, then subtract breaks and partial unavailability
    merged = _merge_intervals(work_blocks + extra_blocks)
    merged = _subtract_many(merged, break_blocks)
    merged = _subtract_many(merged, partial_unavailable)
    # Filter zero-length leftovers
    return [(s, e) for s, e in merged if e > s]


# ---------------------------------------------------------------------------
# Subtract existing bookings (with buffer)
# ---------------------------------------------------------------------------

def _existing_busy_intervals(
    midwife_id: str,
    date_str: str,
    buffer_minutes: int,
) -> list:
    """Return (start_min - buffer, end_min + buffer) intervals for every
    booking this midwife already has on this date."""
    busy: list = []
    for booking in get_existing_bookings_for(midwife_id, date_str):
        s = _to_minutes(booking.get("appointment_time", ""))
        if s is None:
            continue
        try:
            duration = int(booking.get("duration_minutes") or 60)
        except (TypeError, ValueError):
            duration = 60
        e = s + duration
        busy.append((max(0, s - buffer_minutes), e + buffer_minutes))
    return busy


# ---------------------------------------------------------------------------
# Subdivide free intervals into bookable slots for a given service duration
# ---------------------------------------------------------------------------

def _slots_in_interval(
    s: int,
    e: int,
    service_duration: int,
    granularity: int,
    earliest_start: int,
) -> list:
    """Return slot start times (in minutes-of-day) for [s, e) that fit
    `service_duration` and align to `granularity`."""
    # Round s up to the next multiple of granularity
    aligned = s
    if granularity > 0 and aligned % granularity != 0:
        aligned += granularity - (aligned % granularity)
    aligned = max(aligned, earliest_start)
    slots = []
    cur = aligned
    while cur + service_duration <= e:
        slots.append(cur)
        cur += granularity if granularity > 0 else service_duration
    return slots


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_availability(
    service_id: str,
    date_str: str,
    preferred_midwife_id: Optional[str] = None,
) -> DayAvailability:
    """Compute all available slots for a given service on a given date.

    Returns DayAvailability with `slots = []` if nothing is available
    (rather than raising)."""
    on_date = _parse_date(date_str)
    if not on_date:
        return DayAvailability(date=date_str, weekday="", slots=[])

    weekday = _WEEKDAY_NAMES[on_date.weekday()]

    # Don't allow past dates
    today = datetime.now().date()
    if on_date < today:
        return DayAvailability(date=date_str, weekday=weekday, slots=[])

    # Load reference data in parallel
    services, midwives, links, weekly, overrides, clinic = await asyncio.gather(
        get_services(),
        get_midwives(),
        get_midwife_services(),
        get_weekly_schedule(),
        get_schedule_overrides(),
        get_clinic_info(),
    )

    # Service lookup
    svc = next((s for s in services if s.get("service_id") == service_id), None)
    if not svc:
        return DayAvailability(date=date_str, weekday=weekday, slots=[])

    try:
        duration = int(svc.get("duration_minutes") or 60)
    except (TypeError, ValueError):
        duration = 60

    try:
        granularity = int(clinic.get("default_slot_granularity_minutes") or 30)
    except (TypeError, ValueError):
        granularity = 30

    try:
        buffer_min = int(clinic.get("buffer_between_appointments_minutes") or 15)
    except (TypeError, ValueError):
        buffer_min = 15

    # If date == today, drop slots starting in the past
    earliest_start = 0
    if on_date == today:
        now = datetime.now()
        earliest_start = now.hour * 60 + now.minute

    # Find midwives qualified for this service
    qualifying_ids = {
        link.get("midwife_id") for link in links
        if link.get("service_id") == service_id and link.get("midwife_id")
    }
    if preferred_midwife_id:
        if preferred_midwife_id not in qualifying_ids:
            return DayAvailability(date=date_str, weekday=weekday, slots=[])
        qualifying_ids = {preferred_midwife_id}

    midwife_lookup = {m.get("midwife_id"): m for m in midwives if m.get("active", True)}

    all_slots: list = []
    for midwife_id in qualifying_ids:
        midwife = midwife_lookup.get(midwife_id)
        if not midwife:
            continue
        midwife_name = midwife.get("midwife_name") or midwife_id

        # 1. Raw work intervals
        intervals = _work_intervals_for_midwife_on_date(
            midwife_id, on_date, weekly, overrides
        )
        if not intervals:
            continue

        # 2. Subtract existing bookings (+ buffer)
        busy = _existing_busy_intervals(midwife_id, date_str, buffer_min)
        intervals = _subtract_many(intervals, busy)

        # 3. Generate slots that fit the service duration
        for s, e in intervals:
            slot_starts = _slots_in_interval(
                s, e, duration, granularity, earliest_start
            )
            for slot_start in slot_starts:
                all_slots.append(TimeSlot(
                    start_time=_to_hhmm(slot_start),
                    end_time=_to_hhmm(slot_start + duration),
                    midwife_id=midwife_id,
                    midwife_name=midwife_name,
                ))

    # Sort: by start_time, then by midwife name (deterministic)
    all_slots.sort(key=lambda s: (s.start_time, s.midwife_name))

    return DayAvailability(date=date_str, weekday=weekday, slots=all_slots)


async def get_next_available_days(
    service_id: str,
    num_days: int = 7,
    start_from: Optional[str] = None,
    preferred_midwife_id: Optional[str] = None,
) -> list[DayAvailability]:
    """Look forward up to `num_days` and return only those days with at
    least one available slot. Useful for 'show me the next few free days'."""
    if start_from:
        start_date = _parse_date(start_from) or datetime.now().date()
    else:
        start_date = datetime.now().date()

    # Run availability checks in parallel
    tasks = []
    for i in range(num_days):
        d = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        tasks.append(get_availability(service_id, d, preferred_midwife_id))
    results = await asyncio.gather(*tasks)
    return [r for r in results if r.slots]


# ---------------------------------------------------------------------------
# Convenience helpers for the chat layer
# ---------------------------------------------------------------------------

def format_slots_human(day: DayAvailability, max_lines: int = 30) -> str:
    """Format a DayAvailability for display in a chat message.

    Example output:
        Tuesday, June 7
        - 09:00 with Najat
        - 09:30 with Najat
        - 10:00 with Najat, Fiona
        ...
    """
    if not day.slots:
        return f"No availability on {day.date}."

    # Group slots that share the same start time across midwives
    by_start: dict = {}
    for slot in day.slots:
        by_start.setdefault(slot.start_time, []).append(slot.midwife_name)

    # Sort start times
    times = sorted(by_start.keys())
    on_date = _parse_date(day.date)
    # Cross-platform: "%-d" is Linux/Mac only and crashes on Windows.
    # Use "%d" (zero-padded) and strip the leading zero by hand.
    header = day.date
    if on_date:
        header = on_date.strftime("%A, %B %d").replace(" 0", " ")

    lines = [header]
    for t in times[:max_lines]:
        names = ", ".join(sorted(set(by_start[t])))
        lines.append(f"- {t} with {names}")
    if len(times) > max_lines:
        lines.append(f"... and {len(times) - max_lines} more. Ask for more times if needed.")
    return "\n".join(lines)


def find_slot(day: DayAvailability, time_str: str,
              preferred_midwife_name: Optional[str] = None) -> Optional[TimeSlot]:
    """Look up a specific slot by start time. If multiple midwives are free
    at that time, prefer one matching `preferred_midwife_name`, else return
    the first."""
    matching = [s for s in day.slots if s.start_time == time_str]
    if not matching:
        return None
    if preferred_midwife_name:
        for s in matching:
            if s.midwife_name.lower() == preferred_midwife_name.lower():
                return s
    return matching[0]