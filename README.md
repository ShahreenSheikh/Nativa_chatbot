# NativaCare Chatbot

Midwifery booking chatbot for NativaCare in Abu Dhabi. Built on the same
architecture as the Dubai Healthcare bot but with a slot-picker booking flow
and per-midwife scheduling. Reachable on both the website and WhatsApp.

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
| `logger.py` | In-memory chat log (last 100 turns, for the dashboard) |
| `database.py` | Sheet-backed catalog data (8 tabs) + appointment read/write (now via `db.py`) |
| `db.py` | SQLite persistence — invoices, appointments, payment proofs, conversation takeover state |
| `payments.py` | Manual bank-transfer invoices — create, approve, reject |
| `availability.py` | Slot picker — work blocks + overrides + buffer + bookings |
| `calendar_service.py` | Write-only Google Calendar integration |
| `email_service.py` | Patient + clinic email confirmations (Resend) |
| `whatsapp_service.py` | WhatsApp Cloud API — sending replies, verifying inbound webhooks, downloading images |
| `ai.py` | Conversational AI — LLM understand+compose + state machine |
| `main.py` | FastAPI entry point (website chat, WhatsApp webhook, invoices, dashboard, admin auth) |
| `dashboard/` | Staff dashboard — 4 static HTML pages, served by `main.py` at `/dashboard` |
| `Procfile` | Railway/Heroku-style start command |

## Setup

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Environment variables

Copy `env.example` to `.env` and fill in real values — every variable is
documented there (what it's for, what breaks if it's blank). The short
version of what's required vs optional is at the bottom of that file.

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

This flow is identical whether the patient is on the website chat or
WhatsApp — both channels call the same `get_ai_response()` function in
`ai.py`.

## WhatsApp integration

`main.py` exposes two routes for Meta's **WhatsApp Cloud API** — connected
directly, with no third-party BSP (Twilio, 360dialog, etc.) and no BSP
markup:

- `GET /webhook/whatsapp` — Meta's one-time verification handshake
- `POST /webhook/whatsapp` — receives inbound messages, routes them
  through `get_ai_response()`, sends the reply back via
  `whatsapp_service.send_whatsapp_message()`

Each WhatsApp sender gets their own session, keyed as `whatsapp_{wa_id}`,
kept separate from website chat sessions.

**Current state: connected to Meta's test number, working for verified
test recipients only.** In this test/development phase:
- Only phone numbers manually added and verified in Meta's "To" list can
  message the bot and receive replies (capped at 5 numbers by Meta).
- Messages from anyone not on that list won't get a reply.

**To open it up to real patients**, two things still need to happen on
the Meta side (no code changes needed):
1. **Business verification** (Meta's "Step 3" in the WhatsApp setup
   wizard) — proves NativaCare is a real, legitimate business.
2. **Publish the app** — lifts the test-mode recipient restriction.

At that point the clinic's real number can also be migrated in to
replace the temporary test number, so patients message the number they
already know.

**One setup gotcha worth documenting:** registering the Callback URL in
the Meta dashboard is not always enough on its own — the WhatsApp
Business Account (WABA) also needs to be explicitly subscribed to the
app via a direct API call:

```
curl -X POST "https://graph.facebook.com/v21.0/{WABA_ID}/subscribed_apps" \
  -H "Authorization: Bearer {ACCESS_TOKEN}"
```

A successful response looks like `{"success": true}`. Without this,
Meta will generate webhook events (visible in the dashboard's "Check
test webhooks" panel) but never actually deliver them to the app.

## Manual payment — bank transfer + staff-approved screenshot

When `PAYMENT_ENABLED=true`, the bot inserts a payment step between the
booking confirmation ("yes") and the actual calendar commit — no payment
gateway involved, just a bank transfer your staff verify by eye:

1. The bot generates an **invoice**: a reference code + your bank
   details (from `.env`), persisted to SQLite immediately — independent
   of the chat session, so it survives the tab closing, the session
   expiring, or the server restarting.
2. The invoice message shows the bank details **inline in the chat**
   (never only as a download) plus a link to `/pay/{reference}` — a
   simple mobile-friendly page where the patient uploads a screenshot of
   the transfer. The same thing happens automatically if they send an
   image on WhatsApp instead.
3. Staff review the screenshot on the dashboard's **Payments** page and
   approve or reject it. Approving is what actually creates the
   appointment, writes the calendar event, and sends confirmation
   emails — nothing commits before that.
4. The patient can check status any time by asking the bot ("did you get
   my screenshot?") or reopening the `/pay/{reference}` link — both read
   from the persisted invoice, not the live session.

Nothing here touches money directly — there's no payment gateway
integration, just a verified-by-a-human bank transfer. `PAYMENT_ENABLED`
defaults to `false`; when off, bookings commit on "yes" exactly as
before, no payment step.

**Before switching it on:** fill in `BANK_NAME` / `BANK_ACCOUNT_NUMBER` /
`BANK_IBAN` in `.env` — while blank, the invoice message shows empty
lines where the account details should be.

## Dashboard

Four static HTML pages, served by this same backend at `/dashboard`
(mount folder: `dashboard/`):

| Page | What it's for |
|------|----------------|
| `1_dashboard.html` | KPI overview, channel status (website/WhatsApp) |
| `2_appointments.html` | Calendar view, confirm/cancel bookings |
| `3_ai_chatbot_conversations.html` | Live conversation log + human takeover |
| `4_payments.html` | Review and approve/reject payment screenshots |

Each page auto-connects to this backend when served from the same
origin (no manual URL entry needed). If you ever host the dashboard
separately from the backend, paste the backend URL into the "Backend"
field on any page — it's saved per browser in `localStorage`.

### Human takeover (WhatsApp)

WhatsApp's Cloud API only lets one thing send from a given number at a
time — the bot or a human, not both via different apps simultaneously.
So "handing off to a human" here means a staff member clicks "Take over"
on the Conversations page: the bot goes silent for that conversation,
and the staff member's replies (typed into the same box) go out over
the same WhatsApp number the patient's already talking to. Click "Hand
back to bot" to resume automated replies.

**Known limitation:** this works live for WhatsApp. For website chat,
takeover logs the staff reply but there's no push channel to the
browser yet, so the patient won't see it until they send another
message or reload — a real-time channel for website takeover is a
Phase 2 item.

## Admin auth

Every staff-only dashboard endpoint (appointment/invoice lists, approve/
reject, conversation takeover) requires a matching `X-Admin-Key` header,
checked against `ADMIN_API_KEY` in `.env`. Patient-facing endpoints
(`/chat`, `/pay/{reference}`, the WhatsApp webhook) are never gated by
this — patients shouldn't need a login.

**While `ADMIN_API_KEY` is blank, every dashboard endpoint is open to
anyone with the backend URL** — fine for local testing, not fine once
the URL is reachable by anyone besides you. Set it before deploying, and
enter the same value into each dashboard page's "Admin key" field (once
per browser/device — it's saved in `localStorage`).

## Known limitations of v1

- **No Calendar read.** The bot only writes to Calendar; manual entries
  the clinic team adds aren't checked when computing availability. To
  fix: add a `get_calendar_busy()` function in `calendar_service.py` and
  call it from `availability.py` alongside `get_existing_bookings_for`.
- **WhatsApp is test-mode only** until business verification + app
  publish are complete (see above).
- **No Arabic / French.** Detection function exists but always returns
  `"en"`. Translation is a Phase 2 task.
- **No travel time between home visits.** The configured buffer
  (default 15 min) applies regardless of location. Real travel time
  between Abu Dhabi areas isn't modeled.
- **Website human takeover has no live push.** See "Human takeover"
  above — works live for WhatsApp, logs-only for website chat.

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

The bot uses a **two-call LLM architecture**, powered by **Cerebras
Cloud** (`gpt-oss-120b`, OpenAI-compatible endpoint):

1. **Understand**: extracts intent + slot values as strict JSON. No DB
   context sent — keeps tokens low. Retried once on bad JSON, then falls
   back to regex.
2. **Compose**: drafts open-ended replies using a *targeted* DB slice
   (only the rows relevant to the user's question). Skipped entirely
   during booking flow, which uses deterministic templates.

This minimises hallucination on facts (prices, names) and keeps the cost
per turn low. Booking-flow turns make zero LLM calls.

Cerebras states it does not retain or train on inference API inputs and
outputs. Before going live with real patient data, confirm whether a
signed Data Processing Agreement is needed for UAE PDPL / DoH compliance
— the self-serve tier may not include one by default.

## Phase 2 candidates

In rough priority order:

1. **Real client data** — replace every `[DEMO]` value.
2. **Calendar read** for true conflict-free availability.
3. **WhatsApp business verification + publish** to open it up beyond the
   5 test-mode recipients.
4. **Live push for website human takeover** (currently WhatsApp-only —
   see "Human takeover" above).
5. **Reschedule / cancel** flow (currently only book).
6. **Arabic + French** translation layer.
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

## Deploying (Railway)

1. **Push this folder to a git repo** (GitHub, GitLab, etc.) — Railway
   deploys from a repo, not a local upload. Double-check `.env`,
   `*.db`, `/uploads/`, and `service_account.json` are all gitignored
   (they are, in this repo's `.gitignore`) before pushing.
2. **Create a new Railway project** from that repo. Railway auto-detects
   Python via `requirements.txt`; the included `Procfile` tells it how
   to start the app (`uvicorn main:app --host 0.0.0.0 --port $PORT`) —
   Railway assigns `$PORT` itself, don't hardcode a port anywhere.
3. **Attach a persistent volume** and mount it at, say, `/data`. Without
   this, the SQLite database and uploaded screenshots are wiped on every
   redeploy. Then set in Railway's environment variables:
   ```
   NATIVACARE_DB_PATH=/data/nativacare.db
   UPLOADS_DIR=/data/uploads
   ```
4. **Set every variable from your `.env` in Railway's dashboard** (under
   the service's "Variables" tab) — Railway doesn't read your local
   `.env` file, it needs its own copy of each value.
5. **Set `FRONTEND_BASE_URL`** to the Railway-assigned URL (e.g.
   `https://your-app.up.railway.app`) once you have it — this is what
   makes the `/pay/{reference}` link in invoice messages work.
6. **Set `GOOGLE_CREDENTIALS_JSON`** (the full service account JSON as
   one line) instead of `GOOGLE_SERVICE_ACCOUNT_FILE` — there's no local
   file on Railway to point at.
7. **Confirm `ADMIN_API_KEY` is set** before sharing the URL with
   anyone — see "Admin auth" above.
8. **Point the dashboard at it.** Once deployed, open
   `https://your-app.up.railway.app/dashboard/1_dashboard.html` — it
   should auto-connect (same-origin). Enter your `ADMIN_API_KEY` into
   the "Admin key" field on each page, once per browser.
9. **Register the webhook URL with Meta** (`https://your-app.up.railway.app/webhook/whatsapp`)
   when you're ready to connect WhatsApp — see "WhatsApp integration"
   above for the setup steps and the WABA-subscription gotcha.
