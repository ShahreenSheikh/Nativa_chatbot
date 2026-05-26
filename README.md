# NativaCare Chatbot — First Draft

Midwifery booking chatbot for NativaCare in Abu Dhabi. Built on the same
architecture as the Dubai Healthcare bot but with a slot-picker booking flow
and per-midwife scheduling.

## Status: First draft — DEMO DATA

Every default value in this codebase is a **placeholder marked `[DEMO]`**.
Prices, schedules, midwife qualifications — none of it has been verified
with NativaCare. The bot displays a "demo deployment" warning in its
greeting while `clinic_info.demo_mode = TRUE`. Replace the demo data
before any real patient sees this.

## Files

| File | Purpose |
|------|---------|
| `models.py` | Pydantic shapes for requests, bookings, slots |
| `credentials.py` | Google service-account credential loader |
| `logger.py` | In-memory chat log |
| `database.py` | Sheet-backed data layer (8 tabs) with demo fallbacks |
| `availability.py` | Slot picker — work blocks + overrides + buffer + bookings |
| `calendar_service.py` | Write-only Google Calendar integration |
| `email_service.py` | Patient + clinic email confirmations (Resend) |
| `ai.py` | Conversational AI — LLM understand+compose + state machine |
| `main.py` | FastAPI entry point |

## Setup

### 1. Install dependencies

```
pip install fastapi uvicorn python-dotenv pydantic openai httpx dateparser google-api-python-client google-auth
```

### 2. Environment variables

Create a `.env` file in the project root:

```env
# Required for LLM
GROQ_API_KEY=...
GROQ_MODEL=llama-3.3-70b-versatile

# Required for sheet-backed data (without it, demo fallback is used)
GOOGLE_SHEET_ID=...

# Required for Google Calendar writes (otherwise skipped)
GOOGLE_CALENDAR_ID=...
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
# OR in cloud (Railway etc.)
GOOGLE_CREDENTIALS_JSON={...full JSON...}

# Required for emails (otherwise skipped)
RESEND_API_KEY=...
FROM_EMAIL=bookings@nativacare.com
CLINIC_EMAIL=info@nativacare.com

# Optional overrides
CLINIC_NAME=NativaCare
CLINIC_PHONE=+971507297197
CLINIC_TIMEZONE=Asia/Dubai
```

### 3. Set up the data sheet

Create a Google Sheet with 8 tabs (paste the CSV content provided
separately into each):
`services`, `midwives`, `midwife_services`, `midwife_schedule_weekly`,
`midwife_schedule_overrides`, `packages`, `faqs`, `clinic_info`.

Share it as "Anyone with the link can view" — no auth needed for the
CSV export to work. Put the sheet ID in `GOOGLE_SHEET_ID`.

### 4. Run

```
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs for the Swagger UI.

## How the booking flow works

1. **User says they want to book.**
2. **Bot shows service menu** grouped by category. User picks (or LLM
   extracts service from their message).
3. **Bot shows the next 7 days** with at least one available slot.
4. **User picks a day.** Bot shows all available time slots for that day,
   each labeled with the midwife who'd see them.
5. **User picks a time.** The bot locks the slot to the assigned midwife.
6. **Bot asks for location preference** if the service can be either
   clinic or home.
7. **Bot collects name, phone, email, (address for home visits).**
8. **Bot shows confirmation summary.** User says yes or makes a change.
9. **Bot commits**: saves to memory, writes Google Calendar event, emails
   patient + clinic.

## Known limitations of v1

- **In-memory session and bookings.** Restarting the server loses active
  sessions and any pending bookings not yet emailed.
- **No Calendar read.** The bot only writes to Calendar; manual entries
  the clinic team adds aren't checked when computing availability. To
  fix: add a `get_calendar_busy()` function in `calendar_service.py` and
  call it from `availability.py` alongside `get_existing_bookings_for`.
- **No WhatsApp.** Easy to add later (mirror Dubai's `/webhook/whatsapp`).
- **No Arabic / French.** Detection function exists but always returns
  `"en"`. Translation is a Phase 2 task.
- **No travel time between home visits.** The configured buffer
  (default 15 min) applies regardless of location. Real travel time
  between Abu Dhabi areas isn't modeled.
- **No payment.** The bot confirms a booking; payment happens separately
  via the clinic's normal flow.

## What to verify with the clinic before launch

1. **All pricing.** Every price is invented.
2. **Midwife schedules.** Working days, hours, lunch times.
3. **Service-to-midwife mapping.** Which midwife actually offers which
   service.
4. **Service durations.** Currently 45–120 min based on heuristic.
5. **Home visit area coverage.** Currently soft-warned for keywords
   outside common Abu Dhabi names.
6. **Buffer between appointments.** 15 min may not be enough for travel
   between home visits.
7. **Cancellation policy.** Currently described as "24 hours notice" in
   FAQs — verify.

## Demo-mode warning

`database.py` includes a `demo_mode` flag in `clinic_info`. While it's
`TRUE`, the bot prepends a warning to its greeting:

> [Note: this is a demo deployment with placeholder data — please verify
> any details with the clinic.]

Set `demo_mode` to `FALSE` in the sheet only after every other column has
been verified.

## Architecture notes

The bot uses a **two-call LLM architecture**:

1. **Understand**: extracts intent + slot values as strict JSON. No DB
   context sent — keeps tokens low. Retried once on bad JSON, then falls
   back to regex.
2. **Compose**: drafts open-ended replies using a *targeted* DB slice
   (only the rows relevant to the user's question). Skipped entirely
   during booking flow, which uses deterministic templates.

This minimises hallucination on facts (prices, names) and keeps the cost
per turn low. Booking-flow turns make zero LLM calls.

## Phase 2 candidates

In rough priority order:

1. **Real client data** — replace every `[DEMO]` value.
2. **Calendar read** for true conflict-free availability.
3. **Reschedule / cancel** flow (currently only book).
4. **Arabic + French** translation layer.
5. **WhatsApp** webhook.
6. **Persistent storage** for sessions and bookings.
7. **Multi-booking** in one conversation (deferred from Dubai for the
   same reasons; revisit when real usage shows demand).

## Services-diff crawler

`crawl_services.py` is a one-shot script that compares the services
listed on `nativacare.com/services/` against the services in the Google
Sheet, and reports differences. It never writes to the sheet — humans
review changes and update manually.

Run it any time:

```
python crawl_services.py            # print diff (and email if configured)
python crawl_services.py --quiet    # print only on changes — good for cron
python crawl_services.py --dry-run  # never email, just print
```

Exit codes: `0` = no changes, `1` = changes detected, `2` = error. Useful
if you want a scheduler to alert only on non-zero exits.

Optional email notifications use the same Resend config as the bot
(`RESEND_API_KEY`, `FROM_EMAIL`). Set `CRAWLER_NOTIFY_EMAIL` separately
if you want notifications going to a different inbox than `CLINIC_EMAIL`.

Deployment notes for cron / GitHub Actions / Railway are at the bottom of
`crawl_services.py`.

## Test chat UI

A polished chat interface is included at `static/index.html`. When the
server is running, visit `http://localhost:8000/` in a browser. The page
auto-sends a greeting and is fully usable to test the bot end-to-end.

The HTML file is self-contained — no build step. You can also open it
directly from the filesystem (double-click) and it will talk to a bot
running on `localhost:8000`. The file uses `window.location.origin` when
served by FastAPI, and falls back to `http://localhost:8000` when opened
as a file.
